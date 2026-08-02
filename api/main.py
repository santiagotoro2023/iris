"""IRiS core reasoning API.

Everything routes through /infer — nothing talks to Ollama directly (SPEC.md Phase 1).
Runtime configuration comes from the settings service, never from a config file
the user cannot reach (SPEC.md 3.1).
"""
import os
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import settings

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama:11434")
MAX_TOOL_HOPS = 5

# Env vars seed the *defaults*; the settings service owns the live value.
_THINK_SEED = {"true": "always", "false": "never"}.get(
    os.environ.get("IRIS_THINK", "false").strip().lower(), "model-default")

settings.setting(
    "llm.model", type="string", default=os.environ.get("IRIS_MODEL", "qwen3:8b"),
    title="Model", description="Ollama model tag used for reasoning.")
settings.setting(
    "llm.think", type="string", enum=["never", "always", "model-default"],
    default=_THINK_SEED, title="Reasoning mode",
    description="Reason before answering. 'always' is far more accurate on hard "
                "questions and far slower; 'never' keeps replies conversational.")
settings.setting(
    "general.timezone", type="string", default=os.environ.get("IRIS_TZ", "Europe/Zurich"),
    title="Timezone", description="IANA timezone used for times IRiS reports.")

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
    return datetime.now(ZoneInfo(settings.get("general.timezone"))).isoformat(timespec="seconds")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await settings.init()
    yield


app = FastAPI(title="IRiS", lifespan=lifespan)
app.include_router(settings.router)


class InferRequest(BaseModel):
    messages: list[dict]
    model: str | None = None
    think: bool | None = None


@app.get("/health")
async def health():
    async with httpx.AsyncClient(timeout=5) as c:
        r = await c.get(f"{OLLAMA_URL}/api/tags")
    return {"ollama_ok": r.status_code == 200,
            "model": settings.get("llm.model"),
            "think": settings.get("llm.think"),
            "tools": sorted(TOOLS)}


@app.post("/infer")
async def infer(req: InferRequest):
    messages = list(req.messages)
    if req.think is None:
        # 'model-default' omits the flag entirely, which non-thinking models require.
        think = {"never": False, "always": True}.get(settings.get("llm.think"))
    else:
        think = req.think

    async with httpx.AsyncClient(timeout=600) as c:
        for _ in range(MAX_TOOL_HOPS):
            body = {
                "model": req.model or settings.get("llm.model"),
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


# Mounted last so it cannot shadow the API routes above.
_static = Path(__file__).parent / "static"
if _static.is_dir():
    app.mount("/", StaticFiles(directory=_static, html=True), name="ui")
