"""Central settings registry and service (SPEC.md 3.1).

Modules call `setting()` at import time to register a parameter. Clients render
their settings UI from `GET /settings/schema`, so a setting added by a later
phase appears in every client with no UI work.

Values live in Postgres and are cached in memory; `/settings/stream` pushes
changes so devices stay in sync.
"""
import asyncio
import json
import os

import psycopg
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from jsonschema import Draft202012Validator
from psycopg.types.json import Jsonb

import activity

DATABASE_URL = os.environ.get("DATABASE_URL", "")

REGISTRY: dict[str, dict] = {}      # dotted key -> JSON Schema fragment (incl. default/title)
_overrides: dict[str, object] = {}  # only what differs from the registered default
_listeners: set[asyncio.Queue] = set()

router = APIRouter(prefix="/settings", tags=["settings"])


# Groups in the order somebody actually reaches for them. Anything unlisted lands
# after these, alphabetically.
GROUP_ORDER = ["llm", "persona", "voice", "memory", "location", "proactive",
               "cameras", "devices", "frigate", "backup", "auth", "vision", "general"]


def group_rank(group: str) -> int:
    return GROUP_ORDER.index(group) if group in GROUP_ORDER else len(GROUP_ORDER)


def setting(key: str, **schema) -> None:
    """Register one setting. Dotted keys group in the UI: 'llm.model' -> group 'llm'."""
    if "default" not in schema:
        raise ValueError(f"setting {key!r} must declare a default")
    if "title" not in schema:
        raise ValueError(f"setting {key!r} must declare a title (clients show it)")
    REGISTRY[key] = schema


def _resolve(spec: dict) -> dict:
    """Expand a callable `enum` into its current values.

    Choices that change at runtime — installed models, connected devices — are
    registered as a function so the dropdown reflects reality at request time
    rather than whatever was true at import.
    """
    if not callable(spec.get("enum")):
        return spec
    return {**spec, "enum": list(spec["enum"]())}


def ordered_keys() -> list[str]:
    """Most-reached-for first, obscure last. `order` defaults to 50, so a setting
    only needs a number when it should be pulled forward or pushed back."""
    def rank(key: str):
        spec = REGISTRY[key]
        group = key.split(".")[0] if "." in key else "general"
        return (group_rank(group), group, spec.get("order", 50),
                spec.get("title", key))
    return sorted(REGISTRY, key=rank)


def schema() -> dict:
    # Emitted in display order: the client renders whatever order it is given, and
    # JSON objects keep their insertion order all the way to the browser.
    return {"type": "object",
            "properties": {k: _resolve(REGISTRY[k]) for k in ordered_keys()},
            "additionalProperties": False}


def values() -> dict:
    return {k: _overrides.get(k, s["default"]) for k, s in REGISTRY.items()}


def get(key: str):
    """Read one setting. Falls back to the registered default."""
    return _overrides.get(key, REGISTRY[key]["default"])


async def _connect():
    return await psycopg.AsyncConnection.connect(DATABASE_URL, autocommit=True)


async def init() -> None:
    """Create the table and warm the cache. Called once at startup."""
    async with await _connect() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key        TEXT PRIMARY KEY,
                value      JSONB NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )""")
        cur = await conn.execute("SELECT key, value FROM settings")
        rows = await cur.fetchall()
    # Drop rows for settings no longer registered; they would fail schema validation.
    _overrides.update({k: v for k, v in rows if k in REGISTRY})


async def _broadcast(patch: dict) -> None:
    for q in list(_listeners):
        q.put_nowait(patch)


@router.get("/schema")
async def get_schema():
    return schema()


@router.get("/values")
async def get_values():
    return values()


async def apply(patch: dict, actor: str | None = None) -> dict:
    """Apply a partial update. Validates the *merged* result, so a patch cannot
    leave the configuration in a state the schema forbids.

    Split out of the endpoint so anything server-side that needs to change a
    setting goes through exactly the same validation, persistence and broadcast.
    """
    unknown = sorted(set(patch) - set(REGISTRY))
    if unknown:
        raise HTTPException(400, f"unknown settings: {unknown}")

    errors = sorted(Draft202012Validator(schema()).iter_errors(values() | patch), key=str)
    if errors:
        raise HTTPException(400, "; ".join(
            f"{'.'.join(map(str, e.path)) or '(root)'}: {e.message}" for e in errors))

    async with await _connect() as conn:
        for k, v in patch.items():
            await conn.execute(
                "INSERT INTO settings (key, value) VALUES (%s, %s) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
                (k, Jsonb(v)))

    # ponytail: in-process cache, correct for the single uvicorn worker we run.
    # Multiple workers would each hold a stale copy — move to LISTEN/NOTIFY if that changes.
    _overrides.update(patch)
    await _broadcast(patch)
    await activity.record(
        "settings.change",
        ", ".join(f"{k} = {v}" for k, v in patch.items()), actor)
    return values()


@router.put("/values")
async def put_values(patch: dict, request: Request):
    return await apply(
        patch, getattr(getattr(request, "state", None), "username", None))


@router.get("/stream")
async def stream():
    """Server-sent events: one JSON object per change, so open clients update live."""
    q: asyncio.Queue = asyncio.Queue()
    _listeners.add(q)

    async def events():
        try:
            yield f"data: {json.dumps(values())}\n\n"
            while True:
                patch = await q.get()
                yield f"data: {json.dumps(patch)}\n\n"
        finally:
            _listeners.discard(q)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"cache-control": "no-cache"})
