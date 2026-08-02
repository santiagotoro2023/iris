"""IRiS core reasoning API.

Everything routes through /infer — nothing talks to Ollama directly (SPEC.md Phase 1).
"""
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama:11434")
MODEL = os.environ.get("IRIS_MODEL", "qwen3:8b")
TZ = os.environ.get("IRIS_TZ", "Europe/Zurich")
MAX_TOOL_HOPS = 5

# qwen3 reasons before answering unless told not to, which costs ~11s per turn against
# ~0.6s with it off (SPEC.md 10). Off by default; callers raise it per request when the
# question is worth the wait. Set IRIS_THINK to anything else to omit the flag entirely,
# which is what non-thinking models (e.g. qwen2.5) need.
DEFAULT_THINK = {"true": True, "false": False}.get(
    os.environ.get("IRIS_THINK", "false").strip().lower())

# name -> (ollama tool schema, callable). Later phases register integrations/search/vision here.
TOOLS: dict[str, tuple[dict, callable]] = {}


def tool(name: str, description: str, parameters: dict):
    schema = {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": parameters},
    }

    def register(fn):
        TOOLS[name] = (schema, fn)
        return fn

    return register


@tool("current_time", "Current date and time in the user's local timezone.",
      {"type": "object", "properties": {}})
def _current_time():
    return datetime.now(ZoneInfo(TZ)).isoformat(timespec="seconds")


app = FastAPI(title="IRiS")


class InferRequest(BaseModel):
    messages: list[dict]
    model: str | None = None
    think: bool | None = None


@app.get("/health")
async def health():
    async with httpx.AsyncClient(timeout=5) as c:
        r = await c.get(f"{OLLAMA_URL}/api/tags")
    return {"ollama_ok": r.status_code == 200, "model": MODEL,
            "think_default": DEFAULT_THINK, "tools": sorted(TOOLS)}


@app.post("/infer")
async def infer(req: InferRequest):
    messages = list(req.messages)
    think = DEFAULT_THINK if req.think is None else req.think
    async with httpx.AsyncClient(timeout=600) as c:
        for _ in range(MAX_TOOL_HOPS):
            body = {
                "model": req.model or MODEL,
                "messages": messages,
                "tools": [schema for schema, _ in TOOLS.values()],
                "stream": False,
            }
            if think is not None:
                body["think"] = think
            r = await c.post(f"{OLLAMA_URL}/api/chat", json=body)
            if r.status_code != 200:
                raise HTTPException(502, f"ollama: {r.text}")

            msg = r.json()["message"]
            calls = msg.get("tool_calls")
            if not calls:
                return {"message": msg, "messages": messages + [msg]}

            messages.append(msg)
            for call in calls:
                fn = call["function"]
                name = fn["name"]
                if name not in TOOLS:
                    result = f"error: no such tool {name!r}"
                else:
                    # A failing tool reports back to the model instead of 500ing the request,
                    # so it can recover or explain rather than the whole turn dying.
                    try:
                        result = str(TOOLS[name][1](**fn.get("arguments", {})))
                    except Exception as e:
                        result = f"error: {e}"
                messages.append({"role": "tool", "tool_name": name, "content": result})

    raise HTTPException(508, f"tool loop exceeded {MAX_TOOL_HOPS} hops")
