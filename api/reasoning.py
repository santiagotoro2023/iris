"""The reasoning loop and tool registry.

Split out of main so both /infer and the chat view run exactly the same path —
there is one place where IRiS thinks, not two that drift (SPEC.md Phase 1).
"""
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


async def run(messages: list[dict], model: str | None = None,
              think: bool | None = None) -> dict:
    """Run the tool-calling loop. Returns {message, messages} with the full transcript."""
    messages = list(messages)
    think = resolve_think(think)

    async with httpx.AsyncClient(timeout=600) as c:
        for _ in range(MAX_TOOL_HOPS):
            body = {"model": model or settings.get("llm.model"),
                    "messages": messages,
                    "tools": [schema for schema, _ in TOOLS.values()],
                    "stream": False}
            if think is not None:
                body["think"] = think

            r = await c.post(f"{OLLAMA_URL}/api/chat", json=body)
            if r.status_code != 200:
                raise HTTPException(502, f"ollama: {r.text}")

            msg = r.json()["message"]
            if not msg.get("tool_calls"):
                return {"message": msg, "messages": messages + [msg]}

            messages.append(msg)
            for call in msg["tool_calls"]:
                fn = call["function"]
                name = fn["name"]
                if name not in TOOLS:
                    result = f"error: no such tool {name!r}"
                else:
                    # A failing tool reports back to the model instead of 500ing the
                    # request, so it can recover or explain rather than the turn dying.
                    try:
                        result = str(TOOLS[name][1](**fn.get("arguments", {})))
                    except Exception as e:
                        result = f"error: {e}"
                messages.append({"role": "tool", "tool_name": name, "content": result})

    raise HTTPException(508, f"tool loop exceeded {MAX_TOOL_HOPS} hops")
