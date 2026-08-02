"""The reasoning loop and tool registry.

Split out of main so both /infer and the chat view run exactly the same path —
there is one place where IRiS thinks, not two that drift (SPEC.md Phase 1).
"""
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from fastapi import HTTPException

import settings

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama:11434")
MAX_TOOL_HOPS = 5

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
    return datetime.now(ZoneInfo(settings.get("general.timezone"))).isoformat(timespec="seconds")


def resolve_think(explicit: bool | None) -> bool | None:
    if explicit is not None:
        return explicit
    # 'model-default' omits the flag, which non-thinking models require.
    return {"never": False, "always": True}.get(settings.get("llm.think"))


def _run_tool(call: dict) -> tuple[str, str]:
    fn = call["function"]
    name = fn["name"]
    if name not in TOOLS:
        return name, f"error: no such tool {name!r}"
    # A failing tool reports back to the model instead of 500ing the request, so it
    # can recover or explain rather than the whole turn dying.
    try:
        return name, str(TOOLS[name][1](**fn.get("arguments", {})))
    except Exception as e:
        return name, f"error: {e}"


async def stream(messages: list[dict], model: str | None = None,
                 think: bool | None = None):
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

    async with httpx.AsyncClient(timeout=600) as c:
        for _ in range(MAX_TOOL_HOPS):
            body = {"model": model or settings.get("llm.model"),
                    "messages": messages,
                    "tools": [schema for schema, _ in TOOLS.values()],
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
                        parts.append(msg["content"])
                        yield {"type": "delta", "text": msg["content"]}
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
                name, result = _run_tool(call)
                messages.append({"role": "tool", "tool_name": name, "content": result})
                yield {"type": "tool", "name": name, "content": result}

    yield {"type": "error", "detail": f"tool loop exceeded {MAX_TOOL_HOPS} hops"}


async def run(messages: list[dict], model: str | None = None,
              think: bool | None = None) -> dict:
    """Collect `stream` to completion. Same path, so the two cannot drift."""
    async for event in stream(messages, model=model, think=think):
        if event["type"] == "done":
            return {"message": event["message"], "messages": event["messages"]}
        if event["type"] == "error":
            raise HTTPException(502, event["detail"])
    raise HTTPException(502, "reasoning ended without a reply")
