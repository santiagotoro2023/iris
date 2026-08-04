"""IRiS API.

Everything routes through here — nothing talks to Ollama directly (SPEC.md Phase 1).
Runtime configuration comes from the settings service, never from a file the user
cannot reach (SPEC.md 3.1).
"""
import asyncio
import hashlib
import os
import time
import zoneinfo
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import activity
import auth
import backup
import cameras
import chat
import devices
import files
import frigate
import integrations
import memory
import persona
import places
import proactive
import registry
import reasoning
import settings
import voice
import wake

OLLAMA_URL = reasoning.OLLAMA_URL
_tags_cache: tuple[float, list[str]] = (0.0, [])


def _installed_models() -> list[str]:
    """Models actually pulled into Ollama, so the dropdown only offers usable ones."""
    global _tags_cache
    now = time.monotonic()
    if now - _tags_cache[0] > 30:  # ponytail: 30s cache; add invalidation if pulls get frequent
        try:
            r = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=2)
            names = sorted(m["model"] for m in r.json().get("models", []))
        except Exception:
            names = _tags_cache[1]  # keep the last good list rather than emptying the dropdown
        _tags_cache = (now, names)
    # Always include the active and default values: a stopped Ollama must never make
    # the stored configuration fail validation.
    return sorted({*_tags_cache[1], settings.get("llm.model"),
                   settings.REGISTRY["llm.model"]["default"]})


settings.setting(
    "llm.model", type="string", enum=_installed_models,
    default=os.environ.get("IRIS_MODEL", "qwen3:8b"),
    title="Model", description="Ollama model tag used for reasoning. "
                               "Lists the models currently pulled on this machine.")
settings.setting(
    "llm.think", type="string", enum=["never", "always", "model-default"],
    default={"true": "always", "false": "never"}.get(
        os.environ.get("IRIS_THINK", "").strip().lower(), "model-default"),
    title="Reasoning mode",
    description="Reason before answering. 'always' is far more accurate on hard "
                "questions and far slower; 'never' keeps replies conversational.")
settings.setting(
    "llm.search_policy", type="string",
    enum=["aggressive", "balanced", "sparing", "off"], default="aggressive",
    title="Web search",
    description="How readily IRiS checks the web. Training data goes stale, searches are "
                "fast, so 'aggressive' is the default: it looks things up rather than "
                "trusting its memory.")
settings.setting(
    "general.timezone", type="string",
    enum=lambda: sorted(zoneinfo.available_timezones()),
    default=os.environ.get("IRIS_TZ", "Europe/Zurich"),
    title="Timezone", description="IANA timezone used for times IRiS reports.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _ASSET_VERSION
    _ASSET_VERSION = _stamp_assets()
    await settings.init()
    await auth.init()
    await activity.init()
    await chat.init()
    await registry.init()
    asyncio.create_task(backup.scheduler())
    asyncio.create_task(memory.compactor())
    asyncio.create_task(proactive.scheduler())
    yield


_static = Path(__file__).parent / "static"

app = FastAPI(title="IRiS", lifespan=lifespan)

async def _gated(request: Request, user: dict = Depends(auth.active_user)) -> dict:
    """Auth gate that also stamps the actor, so the audit log knows who acted
    without every module having to resolve the session again."""
    request.state.username = user["username"]
    return user


app.include_router(auth.router)
# Everything below requires a logged-in user whose password is current.
_gate = [Depends(_gated)]
app.include_router(settings.router, dependencies=_gate)
app.include_router(activity.router, dependencies=_gate)
app.include_router(chat.router)   # its own routes depend on active_user individually
app.include_router(voice.router)  # same
app.include_router(files.router)  # same
# Its own /voice prefix: a WebSocket cannot go on a router carrying a Request-typed
# dependency, so the listener authenticates itself inside the handler.
app.include_router(wake.router)
app.include_router(memory.router)
app.include_router(backup.router)
app.include_router(registry.make_router('device'))
app.include_router(registry.make_router('integration'))
app.include_router(places.router)
app.include_router(proactive.router)
app.include_router(frigate.router)


class InferRequest(BaseModel):
    messages: list[dict]
    model: str | None = None
    think: bool | None = None


@app.get("/commands")
async def commands(_: dict = Depends(auth.active_user)):
    """The composer's quick-command menu, built from what is switched on."""
    return {"commands": reasoning.quick_commands()}


@app.get("/healthz")
async def healthz():
    """Unauthenticated liveness probe — deliberately reveals nothing about the config."""
    return {"ok": True}


@app.get("/health")
async def health(_: dict = Depends(auth.active_user)):
    async with httpx.AsyncClient(timeout=5) as c:
        try:
            ok = (await c.get(f"{OLLAMA_URL}/api/tags")).status_code == 200
        except Exception:
            ok = False
    return {"ollama_ok": ok,
            "model": settings.get("llm.model"),
            "think": settings.get("llm.think"),
            "tools": sorted(reasoning.TOOLS)}


@app.post("/infer")
async def infer(req: InferRequest, user: dict = Depends(auth.active_user)):
    return await reasoning.run(req.messages, model=req.model, think=req.think,
                               user_id=user["id"])


_ASSET_VERSION = "dev"


def _stamp_assets() -> str:
    """Content hash of the shared assets, appended to their URLs.

    `no-cache` only helps once a browser has revalidated at least once; a copy cached
    before that header existed sticks around and silently runs old code (it did, and
    it showed up as unstyled white buttons). A versioned URL is a different resource,
    so there is nothing stale to serve.
    """
    digest = hashlib.md5()
    for name in ("app.css", "app.js", "logo.svg"):
        path = _static / name
        if path.exists():
            digest.update(path.read_bytes())
    return digest.hexdigest()[:8]


def _page(name: str) -> HTMLResponse:
    html = (_static / name).read_text()
    # logo.svg is stamped too: the tab favicon is cached hardest of all.
    for asset in ("app.css", "app.js", "logo.svg"):
        html = html.replace(f'"{asset}"', f'"{asset}?v={_ASSET_VERSION}"')
    # The shell itself is never cached, so a new version is always picked up.
    return HTMLResponse(html, headers={"cache-control": "no-store"})


@app.get("/", include_in_schema=False)
async def index():
    return _page("index.html")


@app.get("/login.html", include_in_schema=False)
async def login_page():
    return _page("login.html")


class RevalidatingStatic(StaticFiles):
    """Without an explicit Cache-Control browsers cache the shell heuristically and
    keep running old code after an update. `no-cache` still allows 304s — it only
    requires revalidation (SPEC.md Phase 5 ships updates as bundle pulls)."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["cache-control"] = "no-cache"
        return response


# Mounted last so it cannot shadow the API routes above.
if _static.is_dir():
    app.mount("/", RevalidatingStatic(directory=_static, html=True), name="ui")
