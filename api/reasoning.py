"""The reasoning loop and tool registry.

Split out of main so both /infer and the chat view run exactly the same path —
there is one place where IRiS thinks, not two that drift (SPEC.md Phase 1).
"""
import contextvars
import inspect
import json
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from fastapi import HTTPException

import memory
import persona
import settings

# Tools that act on behalf of somebody read it from here, rather than every tool
# signature growing a user argument the model would then try to fill in itself.
CURRENT_USER = contextvars.ContextVar("current_user_id", default=None)

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama:11434")
SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://searxng:8080")
MAX_TOOL_HOPS = 5

SEARCH_POLICY = {
    "aggressive": "SEARCH POLICY: aggressive. Search the web for essentially any question "
                  "of fact, including ones you believe you know. Your training data is "
                  "old and frequently wrong about releases, versions, companies and "
                  "people. Searching is cheap and fast. When in doubt, search.",
    "balanced":   "SEARCH POLICY: balanced. Search when a question involves anything "
                  "specific, recent or verifiable, and answer directly only for stable "
                  "general knowledge.",
    "sparing":    "SEARCH POLICY: sparing. Search only when you genuinely cannot answer "
                  "or the question is explicitly about current events.",
    "off":        "SEARCH POLICY: web search is disabled. Say so if a question needs it.",
}

# name -> (ollama tool schema, callable). Later phases register integrations here.
TOOLS: dict[str, tuple[dict, callable]] = {}


def tool(name: str, description: str, parameters: dict):
    schema = {"type": "function",
              "function": {"name": name, "description": description, "parameters": parameters}}

    def register(fn):
        TOOLS[name] = (schema, fn)
        return fn

    return register


@tool("current_time", "Current date and time in the user's local timezone.",
      {"type": "object", "properties": {}})
def _current_time():
    # An ISO string gets misread (it reported "3 PM" for 01:44), so spell it out.
    tz = settings.get("general.timezone")
    now = datetime.now(ZoneInfo(tz))
    return now.strftime(f"%H:%M on %A, %d %B %Y ({tz}, 24-hour clock)")


@tool("remember",
      "Store something durable about the user that is worth knowing in future "
      "conversations: a preference, a fact about them, their setup, a decision they "
      "made. Do NOT store passing chat, questions, or things you merely looked up.",
      {"type": "object",
       "properties": {"text": {"type": "string",
                               "description": "The fact, written as a standalone "
                                              "sentence that will still make sense "
                                              "months from now."}},
       "required": ["text"]})
async def _remember(text: str):
    user_id = CURRENT_USER.get()
    if user_id is None:
        return "error: no user in context"
    if not settings.get("memory.enabled"):
        return "Memory is switched off."
    out = await memory.remember(text, user_id, source="tool")
    return "Updated what I knew." if out["replaced"] else "Remembered."


@tool("recall",
      "Search your memory for what you already know about the user. Automatic recall "
      "already runs on every turn, so only use this when you need something specific "
      "that was not surfaced, such as an older detail.",
      {"type": "object",
       "properties": {"query": {"type": "string",
                                "description": "What you are trying to remember."}},
       "required": ["query"]})
async def _recall(query: str):
    user_id = CURRENT_USER.get()
    if user_id is None:
        return "error: no user in context"
    hits = await memory.recall(query, user_id, limit=8, min_score=0.35)
    if not hits:
        return f"Nothing remembered about {query!r}."
    return "\n".join(f"- {h['text']}" for h in hits)


@tool("look_at_camera",
      "Look at what a home camera can see right now and describe it. Use this "
      "whenever asked what is happening somewhere in the house, whether anyone is at "
      "a door, or whether something has been delivered.",
      {"type": "object",
       "properties": {"camera": {"type": "string",
                                 "description": "The camera's name, as configured."}},
       "required": ["camera"]})
async def _look_at_camera(camera: str):
    import cameras
    if not settings.get("cameras.enabled"):
        return "Cameras are switched off."
    out = await cameras.describe(camera)
    return f"{out['camera']}: {out['description']}"


@tool("web_search",
      "Search the live web. Use this whenever the answer depends on anything you are "
      "not certain of: current events, a specific company, person, product, place, "
      "price, version or date. Prefer searching over answering from memory.",
      {"type": "object",
       "properties": {"query": {"type": "string",
                                "description": "Search terms. Include distinguishing "
                                               "detail such as a place or industry."}},
       "required": ["query"]})
def _web_search(query: str):
    r = httpx.get(f"{SEARXNG_URL}/search",
                  params={"q": query, "format": "json"}, timeout=25)
    if r.status_code != 200:
        return f"search failed: HTTP {r.status_code}"
    results = (r.json().get("results") or [])[:6]
    if not results:
        return f"No results for {query!r}."
    lines = []
    for item in results:
        snippet = (item.get("content") or "").strip().replace("\n", " ")
        lines.append(f"- {item.get('title', '').strip()} <{item.get('url', '')}>\n"
                     f"  {snippet[:300]}")
    return "\n".join(lines)


# Santiago does not want em-dashes anywhere (SPEC.md 18). The persona forbids them; this
# is the safety net for when the model reaches for one anyway.
_EM_DASH = re.compile(r"(?<=\d)\s*[—–]\s*(?=\d)|\s*[—–]\s*")


def strip_dashes(text: str) -> str:
    # A dash between digits is a range ("51-200"), not a clause break. Turning it into
    # a comma invented a second number.
    return _EM_DASH.sub(
        lambda m: "-" if (m.group(0).strip() and m.start() and
                          text[m.start() - 1:m.start()].isdigit()) else ", ", text)


# Emojis are banned outright (SPEC.md 22). Same reasoning as the dashes: the persona
# forbids them, and this is what actually guarantees it. Pictographs and dingbats only,
# so ordinary punctuation and accented letters are untouched.
_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF"      # pictographs, faces, flags, symbols
    "☀-➿"               # misc symbols and dingbats
    "⬀-⯿"               # stars, arrows-as-symbols
    "️‍⃣]+")       # variation selector, ZWJ, keycap


def strip_emoji(text: str) -> str:
    """Bullets, arrows and accented letters are below this range and stay put."""
    return re.sub(r"[ \t]{2,}", " ", _EMOJI.sub("", text))


# Ollama only understands these keys; anything else we store for our own use must be
# stripped before the request, and attachment text folded into the content.
_MODEL_KEYS = {"role", "content", "images", "tool_calls", "tool_name", "thinking"}


def for_model(message: dict) -> dict:
    out = {k: v for k, v in message.items() if k in _MODEL_KEYS}
    attachments = message.get("attachments") or []
    if attachments:
        blocks = "\n\n".join(
            f"[Attached {a.get('kind', 'file')}: {a.get('name', 'file')}]\n"
            f"{a.get('text', '')}" for a in attachments)
        out["content"] = f"{out.get('content', '')}\n\n{blocks}".strip()
    return out


def resolve_think(explicit: bool | None) -> bool | None:
    if explicit is not None:
        return explicit
    # 'model-default' omits the flag, which non-thinking models require.
    return {"never": False, "always": True}.get(settings.get("llm.think"))


async def _run_tool(call: dict) -> tuple[str, str]:
    fn = call["function"]
    name = fn["name"]
    if name not in TOOLS:
        return name, f"error: no such tool {name!r}"
    # A failing tool reports back to the model instead of 500ing the request, so it
    # can recover or explain rather than the whole turn dying.
    try:
        result = TOOLS[name][1](**fn.get("arguments", {}))
        if inspect.isawaitable(result):          # memory tools reach the network
            result = await result
        return name, str(result)
    except Exception as e:
        return name, f"error: {e}"


async def stream(messages: list[dict], model: str | None = None,
                 think: bool | None = None, user_id: int | None = None):
    """Run the tool-calling loop, yielding events as they happen.

    Nothing user-facing should wait for a whole response (SPEC.md 16), so this is the
    primitive; `run` collects it. Events:
      {"type": "delta", "text": ...}   incremental assistant text
      {"type": "tool",  "name", "content"}
      {"type": "done",  "message", "messages"}
      {"type": "error", "detail"}
    """
    messages = list(messages)
    think = resolve_think(think)
    CURRENT_USER.set(user_id)

    # IRiS is a character, not a chat completion (SPEC.md 17). The persona is sent with
    # every request but kept OUT of the stored transcript, so editing it takes effect on
    # existing conversations instead of being frozen in at creation time.
    policy = settings.get("llm.search_policy")
    system = None
    if not any(m.get("role") == "system" for m in messages):
        system = persona.system_message()
        if system:
            # Without today's date the model anchors on its training year: it searched
            # for "Subnautica 2 release status 2023" and then answered "as of 2023".
            today = datetime.now(ZoneInfo(settings.get("general.timezone")))
            stamp = today.strftime("Today is %A, %d %B %Y.")
            system = {**system, "content": "\n\n".join(
                [system["content"], stamp, SEARCH_POLICY[policy],
                 "Never put a year in a search query unless the user asked about that "
                 "year, and never describe your answer as being 'as of' any year."])}
            # Recall happens automatically. Leaving it to the model to decide to search
            # its own memory means it mostly does not.
            if user_id is not None and settings.get("memory.enabled"):
                asked = next((m.get("content") or "" for m in reversed(messages)
                              if m.get("role") == "user"), "")
                recalled = await memory.context_for(asked, user_id)
                if recalled:
                    system = {**system,
                              "content": system["content"] + "\n\n" + recalled}

    memory_off = user_id is None or not settings.get("memory.enabled")
    tools = [schema for name, (schema, _) in TOOLS.items()
             if not (name == "web_search" and policy == "off")
             and not (name in ("remember", "recall") and memory_off)
             and not (name == "look_at_camera"
                      and not settings.get("cameras.enabled"))]

    async with httpx.AsyncClient(timeout=600) as c:
        for _ in range(MAX_TOOL_HOPS):
            body = {"model": model or settings.get("llm.model"),
                    "messages": [for_model(m)
                                 for m in (([system] + messages) if system
                                           else messages)],
                    "tools": tools,
                    "stream": True}
            if think is not None:
                body["think"] = think

            parts: list[str] = []
            tool_calls: list[dict] = []
            async with c.stream("POST", f"{OLLAMA_URL}/api/chat", json=body) as r:
                if r.status_code != 200:
                    detail = (await r.aread()).decode()[:300]
                    yield {"type": "error", "detail": f"ollama: {detail}"}
                    return
                async for line in r.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    msg = chunk.get("message") or {}
                    if msg.get("content"):
                        piece = strip_emoji(strip_dashes(msg["content"]))
                        parts.append(piece)
                        yield {"type": "delta", "text": piece}
                    if msg.get("tool_calls"):
                        tool_calls.extend(msg["tool_calls"])
                    if chunk.get("done"):
                        break

            assistant: dict = {"role": "assistant", "content": "".join(parts)}
            if tool_calls:
                assistant["tool_calls"] = tool_calls
            messages.append(assistant)

            if not tool_calls:
                yield {"type": "done", "message": assistant, "messages": messages}
                return

            for call in tool_calls:
                fn = call.get("function", {})
                yield {"type": "tool_start", "name": fn.get("name", "?"),
                       "arguments": fn.get("arguments", {})}
                name, result = await _run_tool(call)
                messages.append({"role": "tool", "tool_name": name, "content": result})
                yield {"type": "tool", "name": name, "content": result}

    yield {"type": "error", "detail": f"tool loop exceeded {MAX_TOOL_HOPS} hops"}


async def run(messages: list[dict], model: str | None = None,
              think: bool | None = None, user_id: int | None = None) -> dict:
    """Collect `stream` to completion. Same path, so the two cannot drift."""
    async for event in stream(messages, model=model, think=think, user_id=user_id):
        if event["type"] == "done":
            return {"message": event["message"], "messages": event["messages"]}
        if event["type"] == "error":
            raise HTTPException(502, event["detail"])
    raise HTTPException(502, "reasoning ended without a reply")
