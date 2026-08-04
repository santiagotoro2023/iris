"""The reasoning loop and tool registry.

Split out of main so both /infer and the chat view run exactly the same path —
there is one place where IRiS thinks, not two that drift (SPEC.md Phase 1).
"""
import contextvars
import inspect
import json
import os
import re
from dataclasses import dataclass
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

@dataclass
class Tool:
    """A tool is its schema, its implementation, and how it announces itself.

    Presentation lives here rather than in the client because a tool that the UI has
    to be taught about separately is not a tool you can just add. `activity` is
    formatted with the call's own arguments, so the chat says what IRiS is actually
    doing rather than "Running transit".
    """
    schema: dict
    fn: callable
    activity: str = "Working"
    display: str = "text"       # text | sources | lines


# name -> Tool. Later phases register integrations here.
TOOLS: dict[str, Tool] = {}


class _Blanks(dict):
    """A missing argument leaves a gap, never a KeyError mid-reply."""
    def __missing__(self, key):
        return ""


# Always sent. Cheap, and wanted at unpredictable moments: the clock, a search, the
# memory pair, and where the user is, which SPEC.md 42 requires before anything
# location-dependent.
CORE_TOOLS = {"current_time", "web_search", "remember", "recall", "where_am_i"}

# Words that settle it without asking an embedding. Cheaper, and more predictable
# than similarity for the cases that matter most.
TRIGGERS: dict[str, tuple[str, ...]] = {
    "transit": ("bus", "train", "tram", "connection", "depart", "timetable",
                "journey", "travel", "get to", "get there", "how do i get"),
    "departures": ("bus", "train", "tram", "departure", "leaving", "next one",
                   "board", "platform"),
    "route_to": ("get to", "get there", "how do i get", "route", "directions",
                 "travel to", "way to"),
    "find_place": ("nearest", "nearby", "near me", "shop", "restaurant", "cafe",
                   "coffee", "pharmacy", "supermarket", "where is"),
    "weather": ("weather", "rain", "sunny", "forecast", "umbrella", "temperature",
                "cold", "warm", "snow"),
    "look_at_camera": ("camera", "door", "front door", "garden", "parcel",
                       "delivery", "who is", "outside"),
    "check_mail": ("mail", "email", "inbox", "message", "wrote", "replied"),
    "calendar": ("calendar", "appointment", "meeting", "schedule", "free",
                 "busy", "diary", "on today", "on tomorrow"),
    "analyze_file": ("file", "image", "picture", "photo", "document", "pdf",
                     "video", "attached", "upload", "screenshot"),
    "system_status": ("how are you", "what are you doing", "busy", "gpu", "vram",
                      "memory", "disk", "temperature", "load", "running",
                      "how do you feel", "yourself"),
}

_tool_vectors: dict[str, list[float]] | None = None


async def _relevant(query: str, budget: int) -> set[str]:
    """Rank the optional tools against the turn, so the model is handed a short list
    it can actually reason about.

    Eighteen tools and 16,000 characters of schema made an 8B model fill arguments
    from stale context and lose track of roles (SPEC.md 44). Cutting descriptions
    cost precision; choosing which tools to send costs none.
    """
    global _tool_vectors
    optional = [n for n in TOOLS if n not in CORE_TOOLS]
    chosen = {n for n, words in TRIGGERS.items()
              if n in TOOLS and any(w in query for w in words)}
    # A word match is evidence; similarity is a guess. When the words settle it,
    # padding the list out to a budget only puts a camera in front of a question
    # about the weather.
    if chosen:
        return chosen
    try:
        import memory
        if _tool_vectors is None:
            texts = [TOOLS[n].schema["function"]["description"] for n in optional]
            vectors = await memory.embed(texts)
            _tool_vectors = dict(zip(optional, vectors))
        wanted = (await memory.embed([query]))[0]

        def score(name: str) -> float:
            v = _tool_vectors.get(name)
            if not v:
                return 0.0
            dot = sum(a * b for a, b in zip(wanted, v))
            na = sum(a * a for a in wanted) ** 0.5
            nb = sum(b * b for b in v) ** 0.5
            return dot / (na * nb) if na and nb else 0.0

        for name in sorted(optional, key=score, reverse=True):
            if len(chosen) >= budget:
                break
            chosen.add(name)
    except Exception as e:
        # Never fewer tools because the embedder is down: fall back to all of them.
        print(f"[reasoning] tool selection unavailable: {e}", flush=True)
        return set(optional)
    return chosen


def announce(name: str, arguments: dict | None) -> str:
    spec = TOOLS.get(name)
    if not spec:
        return f"Running {name}"
    try:
        text = spec.activity.format_map(_Blanks(arguments or {}))
    except Exception:
        text = spec.activity
    return " ".join(text.split()).rstrip(",") or f"Running {name}"

# Quick commands (SPEC.md 33). A directive prepended to the turn, which is enough to
# aim an 8B model at the right tool without taking the decision away from it. Adding
# one is a dict entry: the UI builds its menu from /commands.
QUICK_COMMANDS: dict[str, dict] = {
    "transit": {
        "label": "Transit",
        "hint": "where to, or where from and to",
        "directive": "Use the transit and departures tools. If only one place is "
                     "named, it is the destination and the origin is left out, which "
                     "starts from where they are. Give times, not prose.",
        "needs": "location.enabled",
    },
    "camera": {
        "label": "Look at a camera",
        "hint": "which camera, or what you want to know",
        "directive": "Use look_at_camera. If no camera is named and only one is set "
                     "up, use that one. Report what is actually visible, briefly.",
        "needs": "cameras.enabled",
    },
    "mail": {
        "label": "Check mail",
        "hint": "optional: what you are looking for",
        "directive": "Use check_mail. Summarise who wrote and what about, one line "
                     "each. Do not invent messages that are not listed.",
    },
    "calendar": {
        "label": "Schedule",
        "hint": "optional: a day, or what you are looking for",
        "directive": "Use the calendar tool. List what is on with times, nothing "
                     "more. Do not invent appointments.",
    },
    "search": {
        "label": "Search the web",
        "hint": "what to look up",
        "directive": "Search the web for this before answering. Do not answer from "
                     "memory, and cite what you found.",
    },
    "remember": {
        "label": "Remember this",
        "hint": "what IRiS should keep",
        "directive": "Store this with the remember tool, exactly as the user means "
                     "it, then confirm in one short sentence.",
    },
    "weather": {
        "label": "Weather",
        "hint": "optional: a place",
        "directive": "Use the weather tool. Give the numbers and whether to expect "
                     "rain, in a sentence or two.",
        "needs": "location.enabled",
    },
    "find": {
        "label": "Find a place",
        "hint": "what to find, e.g. a pharmacy",
        "directive": "Use find_place. Give names and streets, nearest first.",
        "needs": "location.enabled",
    },
}


def quick_commands() -> list[dict]:
    """Only the ones whose feature is actually switched on."""
    out = []
    for name, spec in QUICK_COMMANDS.items():
        need = spec.get("needs")
        if need and not settings.get(need):
            continue
        out.append({"name": name, "label": spec["label"], "hint": spec["hint"]})
    return out


def apply_command(name: str, text: str) -> str:
    spec = QUICK_COMMANDS.get(name)
    if not spec:
        return text
    return f"{spec['directive']}\n\n{text}"


def tool(name: str, description: str, parameters: dict,
         activity: str = "", display: str = "text"):
    schema = {"type": "function",
              "function": {"name": name, "description": description,
                           "parameters": parameters}}

    def register(fn):
        TOOLS[name] = Tool(schema, fn, activity or f"Running {name}", display)
        return fn

    return register


@tool("current_time", "Current date and time in the user's local timezone.",
      {"type": "object", "properties": {}},
      activity="Checking the clock")
def _current_time():
    # An ISO string gets misread (it reported "3 PM" for 01:44), so spell it out.
    tz = settings.get("general.timezone")
    now = datetime.now(ZoneInfo(tz))
    return now.strftime(f"%H:%M on %A, %d %B %Y ({tz}, 24-hour clock)")


@tool("remember",
      "Store a durable fact about the user. Not passing chat, questions, or "
      "anything you looked up.",
      {"type": "object",
       "properties": {"text": {"type": "string",
                               "description": "The fact, written as a standalone "
                                              "sentence that will still make sense "
                                              "months from now."}},
       "required": ["text"]},
      activity="Remembering that")
async def _remember(text: str):
    user_id = CURRENT_USER.get()
    if user_id is None:
        return "error: no user in context"
    if not settings.get("memory.enabled"):
        return "Memory is switched off."
    out = await memory.remember(text, user_id, source="tool")
    return "Updated what I knew." if out["replaced"] else "Remembered."


@tool("recall",
      "Search your memory for an older detail. Relevant memories are already "
      "supplied each turn.",
      {"type": "object",
       "properties": {"query": {"type": "string",
                                "description": "What you are trying to remember."}},
       "required": ["query"]},
      activity="Searching what I remember about {query}", display="lines")
async def _recall(query: str):
    user_id = CURRENT_USER.get()
    if user_id is None:
        return "error: no user in context"
    hits = await memory.recall(query, user_id, limit=8, min_score=0.35)
    if not hits:
        return f"Nothing remembered about {query!r}."
    return "\n".join(f"- {h['text']}" for h in hits)


@tool("check_mail",
      "What has arrived in the user's mailboxes: new mail, waiting messages, "
      "whether someone replied.",
      {"type": "object", "properties": {}},
      activity="Looking in the mailbox", display="lines")
async def _check_mail():
    import integrations
    return await integrations.check_all_mail()


@tool("calendar",
      "The user's calendar: appointments, meetings, what they have on, whether "
      "they are free.",
      {"type": "object",
       "properties": {}},
      activity="Checking the calendar", display="lines")
async def _calendar():
    import integrations
    return await integrations.check_all_calendars()


@tool("transit",
      "Public transport times. Give only the destination; the origin defaults to "
      "the user's nearest stop. Accepts 'home' and 'work'.",
      {"type": "object",
       "properties": {
           "destination": {"type": "string", "description": "Where the journey ends."},
           "origin": {"type": "string",
                      "description": "Where it starts. OMIT THIS unless the user "
                                     "actually named a starting point: left out, it "
                                     "uses the nearest stop to where they are."},
           "when": {"type": "string",
                    "description": "When, in the user's own words: '08:30', "
                                   "'tomorrow 08:30'. Omit for now."},
           "arrive_by": {"type": "boolean",
                         "description": "TRUE when they want to BE there by that "
                                        "time, FALSE when they want to leave at it."}},
       "required": ["destination"]},
      activity="Checking the timetable to {destination}", display="lines")
async def _transit(destination: str, origin: str = "", when: str = "",
                   arrive_by: bool = False):
    import places
    return await places.journey(origin, destination, when or None, arrive_by)


@tool("departures",
      "The departure board at a stop: what is leaving soon, rather than a specific "
      "journey. Omit the station for the user's nearest.",
      {"type": "object",
       "properties": {"station": {"type": "string",
                                  "description": "Station or stop name. Omit for the "
                                                 "nearest stop to where they are."}}},
      activity="Reading the board at {station}", display="lines")
async def _departures(station: str = ""):
    import places
    return await places.departures(station)


@tool("route_to",
      "How to reach a named place: finds it, gives its address and distance, and "
      "plans public transport there. Use for 'how do I get to X' and 'how do I get "
      "there'.",
      {"type": "object",
       "properties": {
           "place": {"type": "string",
                     "description": "The place, shop, company or address to reach."},
           "when": {"type": "string",
                    "description": "When, in the user's own words: '08:30', "
                                   "'tomorrow 08:30', '2026-08-06 09:00'. Omit for "
                                   "now."},
           "arrive_by": {"type": "boolean",
                         "description": "TRUE when they said they want to BE there or "
                                        "ARRIVE by that time. FALSE when they said "
                                        "they want to LEAVE at it."}},
       "required": ["place"]},
      activity="Working out how to get to {place} {when}", display="lines")
async def _route_to(place: str, when: str = "", arrive_by: bool = False):
    import places
    return await places.route_to(place, when, arrive_by)


@tool("find_place",
      "Find shops, restaurants, amenities and addresses on the map. Use for "
      "'where is the nearest X', not a web search.",
      {"type": "object",
       "properties": {
           "query": {"type": "string",
                     "description": "What to look for, e.g. 'pharmacy', 'hardware "
                                    "shop', 'Bahnhofstrasse 12'."},
           "near": {"type": "string",
                    "description": "Optional town or address to search around."}},
       "required": ["query"]},
      activity="Looking on the map for {query} {near}", display="lines")
async def _find_place(query: str, near: str = ""):
    import places
    return await places.find_place(query, near)


@tool("analyze_file",
      "Read an uploaded picture, document or video: what it shows, what it says, "
      "what happens in it. An attachment marked as not yet read MUST be read with "
      "this before answering anything about it. A video is transcribed as well as "
      "watched, which takes a moment.",
      {"type": "object",
       "properties": {
           "file": {"type": "string",
                    "description": "The file's name. Omit for the most recent."},
           "question": {"type": "string", "description": "What to look for."}}},
      activity="Reading {file}", display="text")
async def _analyze_file(file: str = "", question: str = ""):
    import files
    return await files.analyse_stored(file or (files.recent(1) or [""])[0], question)


@tool("where_am_i",
      "Where the user is now, and their nearest stop. Required before anything "
      "that depends on where they are.",
      {"type": "object", "properties": {}},
      activity="Working out where you are", display="lines")
async def _where_am_i():
    import places
    return await places.whereabouts()


@tool("system_status",
      "This machine's measured GPU load and temperature, resident models, CPU, "
      "memory, disk, uptime and service health. Required for any question about how "
      "you are running, what you are doing, or how busy you are.",
      {"type": "object", "properties": {}},
      activity="Checking my own state", display="lines")
async def _system_status():
    import system
    return await system.describe()


@tool("weather",
      "Weather now and the forecast for today and tomorrow: what to wear, whether "
      "to take an umbrella.",
      {"type": "object",
       "properties": {"place": {"type": "string",
                                "description": "OMIT unless the user names a place in "
                                               "THIS message. Left out it uses where "
                                               "they are, which is almost always "
                                               "right."}}},
      activity="Checking the weather {place}", display="lines")
async def _weather(place: str = ""):
    import places
    return await places.weather(place)


@tool("look_at_camera",
      "Look at a camera now: who is at a door, whether a parcel arrived, what is "
      "happening in a room.",
      {"type": "object",
       "properties": {"camera": {"type": "string",
                                 "description": "The camera's name, as configured."}},
       "required": ["camera"]},
      activity="Looking at the {camera} camera")
async def _look_at_camera(camera: str):
    import cameras
    if not settings.get("cameras.enabled"):
        return "Cameras are switched off."
    out = await cameras.describe(camera)
    return f"{out['camera']}: {out['description']}"


@tool("web_search",
      "Search the live web. Use whenever the answer depends on anything you are "
      "not certain of.",
      {"type": "object",
       "properties": {"query": {"type": "string",
                                "description": "Search terms. Include distinguishing "
                                               "detail such as a place or industry."}},
       "required": ["query"]},
      activity='Searching the web for "{query}"', display="sources")
def _web_search(query: str):
    return search(query)


def search(query: str, category: str = "", limit: int = 6,
           time_range: str = "", max_age_days: int = 0) -> str:
    """One place that formats search hits, so anything rendering them as sources
    (the chat, the briefing) parses exactly the same shape.

    `time_range` is passed to SearXNG, and `max_age_days` additionally drops results
    that carry a publishedDate older than that. Both are needed: the first briefing
    asked for "top world news headlines today" and was handed a 2015 story about
    Charlie Hebdo, which is worse than no news at all.
    """
    params = {"q": query, "format": "json"}
    if category:
        params["categories"] = category
    if time_range:
        params["time_range"] = time_range
    r = httpx.get(f"{SEARXNG_URL}/search", params=params, timeout=25)
    if r.status_code != 200:
        return f"search failed: HTTP {r.status_code}"
    results = r.json().get("results") or []
    if max_age_days:
        cutoff = datetime.now(ZoneInfo("UTC")).timestamp() - max_age_days * 86400
        fresh = []
        for item in results:
            stamp = item.get("publishedDate")
            if not stamp:
                continue          # undated is not datable, and news must be dated
            try:
                when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                if when.tzinfo is None:
                    when = when.replace(tzinfo=ZoneInfo("UTC"))
            except ValueError:
                continue
            if when.timestamp() >= cutoff:
                fresh.append(item)
        results = fresh
    results = results[:limit]
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


# Bare and markdown-wrapped links alike. The tool banner beside the reply already
# carries every link, and pasting a 140-character maps URL into a sentence makes the
# sentence unreadable. Prompting against it did not hold (SPEC.md 45).
_MD_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_BARE_URL = re.compile(r"(?<![\w(])https?://[^\s<>)\]]+")


def strip_links(text: str) -> str:
    """Drop a bare URL; keep a markdown link, which the chat renders clickable.

    Stripping both left "Route: Google Maps" as dead text, which is worse than
    either. A titled link is short and useful; a raw 140-character maps URL in the
    middle of a sentence is not.
    """
    text = _BARE_URL.sub("", text)
    # "Full route: ." and "see  for details" are what removal leaves behind.
    text = re.sub(r"[ \t]*\(\s*\)", "", text)
    text = re.sub(r"^[ \t]*(full route|link|source|map|url)\s*:\s*$", "",
                  text, flags=re.I | re.M)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text)


# A link arrives across several tokens, so stripping each delta on its own would cut
# one in half. Hold back a trailing fragment that could still become a link.
# Covers a half-typed "[text", a half-typed "[text](url", and a bare "https://par".
_MAYBE_LINK = re.compile(
    r"(?:\[[^\]\n]*(?:\]\([^\s)]*)?|\bhttps?:[^\s<>)\]]*)$")
MAX_HELD = 300


def split_for_links(buffer: str) -> tuple[str, str]:
    """(safe to send now, keep for the next token)."""
    m = _MAYBE_LINK.search(buffer)
    if not m or len(buffer) - m.start() > MAX_HELD:
        return buffer, ""            # nothing pending, or it was never a link
    return buffer[:m.start()], buffer[m.start():]


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
        blocks = []
        for a in attachments:
            kind, name = a.get("kind", "file"), a.get("name", "file")
            text = a.get("text")
            if text:
                # Conversations from before uploads were read on demand.
                blocks.append(f"[Attached {kind}: {name}]\n{text}")
            else:
                blocks.append(
                    f"[Attached {kind}: {name}. It has NOT been read yet. Call "
                    f"analyze_file with file=\"{name}\" to read it before answering "
                    f"anything about it.]")
        out["content"] = f"{out.get('content', '')}\n\n" + "\n\n".join(blocks)
        out["content"] = out["content"].strip()
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
        result = TOOLS[name].fn(**fn.get("arguments", {}))
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

    # Which tools this turn gets. A tool already used in this conversation stays
    # available, or a follow-up ("and how do I get there?") loses the tool that
    # answered the question before it.
    asked = next((m.get("content") or "" for m in reversed(messages)
                  if m.get("role") == "user"), "").lower()
    already = {m.get("tool_name") for m in messages if m.get("role") == "tool"}
    picked = (CORE_TOOLS | already
              | await _relevant(asked, settings.get("llm.tool_budget")))

    tools = [spec.schema for name, spec in TOOLS.items()
             if name in picked
             and not (name == "web_search" and policy == "off")
             and not (name in ("remember", "recall") and memory_off)
             and not (name == "look_at_camera"
                      and not settings.get("cameras.enabled"))
             and not (name in ("transit", "departures", "find_place", "weather",
                               "route_to", "where_am_i")
                      and not settings.get("location.enabled"))]

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
            held = ""
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
                        held += strip_emoji(strip_dashes(msg["content"]))
                        ready, held = split_for_links(held)
                        piece = strip_links(ready)
                        if piece:
                            parts.append(piece)
                            yield {"type": "delta", "text": piece}
                    if msg.get("tool_calls"):
                        tool_calls.extend(msg["tool_calls"])
                    if chunk.get("done") and held:
                        piece = strip_links(held)
                        held = ""
                        if piece:
                            parts.append(piece)
                            yield {"type": "delta", "text": piece}
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
                       "label": announce(fn.get("name", "?"),
                                         fn.get("arguments")),
                       "arguments": fn.get("arguments", {})}
                name, result = await _run_tool(call)
                spec = TOOLS.get(name)
                label = announce(name, fn.get("arguments"))
                # Stored on the message too, so reopening a conversation shows the
                # same line rather than a bare tool name.
                messages.append({"role": "tool", "tool_name": name,
                                 "label": label,
                                 "display": spec.display if spec else "text",
                                 "content": result})
                yield {"type": "tool", "name": name, "label": label,
                       "display": spec.display if spec else "text",
                       "content": result}

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
