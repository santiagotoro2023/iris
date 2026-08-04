"""Typed things: devices and integrations (SPEC.md 33).

One generic store for anything the user adds instances of. A type declares its
fields; the UI renders the add-form from that declaration and the API validates
against it. Adding a new kind of camera, microphone, mailbox or messenger is one
`register(...)` call and no client work at all, which is the same bargain
`settings.setting(...)` makes for single values.

    register(Type(
        kind="device", name="camera", label="Camera",
        fields=[Field("url", "Stream URL", secret=True, required=True)],
        actions={"describe": ...},
    ))

Secret fields never travel back to a client. They are stored as given, in Postgres,
on a volume that is already gitignored and whose backups are AES encrypted.
ponytail: no separate encryption at rest; add a key-wrapped column if this ever
holds anything more valuable than a home camera password.
"""
import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable

import psycopg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field as PField

import activity
import auth

DATABASE_URL = os.environ.get("DATABASE_URL", "")
MASK = "••••••••"


@dataclass
class Field:
    name: str
    label: str
    type: str = "text"            # text | password | number | boolean | choice
    required: bool = False
    secret: bool = False          # never sent to a client once stored
    default: Any = ""
    help: str = ""
    choices: list[str] = field(default_factory=list)

    def as_json(self) -> dict:
        out = {"name": self.name, "label": self.label, "type": self.type,
               "required": self.required, "secret": self.secret,
               "default": self.default, "help": self.help}
        if self.choices:
            out["choices"] = self.choices
        return out


@dataclass
class Type:
    kind: str                     # "device" or "integration"
    name: str                     # stable id, e.g. "camera"
    label: str                    # shown in the picker
    description: str = ""
    fields: list[Field] = field(default_factory=list)
    # name -> coroutine(instance, user) -> dict. Rendered as buttons on the instance.
    actions: dict[str, Callable] = field(default_factory=dict)
    action_labels: dict[str, str] = field(default_factory=dict)
    # A field whose value fills in the rest, so common cases need one choice rather
    # than five lookups. {"Outlook": {"host": ..., "port": ...}, ...}
    preset_field: str = ""
    presets: dict[str, dict] = field(default_factory=dict)

    def as_json(self) -> dict:
        return {"type": self.name, "label": self.label,
                "description": self.description,
                "fields": [f.as_json() for f in self.fields],
                "preset_field": self.preset_field,
                "presets": self.presets,
                "actions": [{"name": a, "label": self.action_labels.get(a, a)}
                            for a in self.actions]}


TYPES: dict[str, Type] = {}


def register(spec: Type) -> Type:
    TYPES[f"{spec.kind}:{spec.name}"] = spec
    return spec


def types_of(kind: str) -> list[Type]:
    return [t for key, t in TYPES.items() if key.startswith(f"{kind}:")]


def spec_for(kind: str, name: str) -> Type:
    spec = TYPES.get(f"{kind}:{name}")
    if not spec:
        known = ", ".join(t.name for t in types_of(kind)) or "none"
        raise HTTPException(400, f"unknown {kind} type {name!r}; known: {known}")
    return spec


# --------------------------------------------------------------- storage ----

async def _connect():
    return await psycopg.AsyncConnection.connect(DATABASE_URL, autocommit=True)


async def init() -> None:
    async with await _connect() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS things (
                id       BIGSERIAL PRIMARY KEY,
                kind     TEXT NOT NULL,
                type     TEXT NOT NULL,
                name     TEXT NOT NULL,
                config   JSONB NOT NULL DEFAULT '{}',
                enabled  BOOLEAN NOT NULL DEFAULT TRUE,
                created  TIMESTAMPTZ NOT NULL DEFAULT now(),
                UNIQUE (kind, name)
            )""")
        # Cameras predate the registry. Move them across rather than orphaning them.
        await conn.execute("""
            INSERT INTO things (kind, type, name, config, enabled, created)
            SELECT 'device', 'camera', name, jsonb_build_object('url', url),
                   enabled, created
            FROM cameras
            WHERE EXISTS (SELECT 1 FROM information_schema.tables
                          WHERE table_name = 'cameras')
            ON CONFLICT (kind, name) DO NOTHING""")


def _row(row: tuple, redact: bool = True) -> dict:
    kind, type_name = row[1], row[2]
    config = dict(row[4] or {})
    spec = TYPES.get(f"{kind}:{type_name}")
    if redact and spec:
        for f in spec.fields:
            if f.secret and config.get(f.name):
                config[f.name] = MASK
    return {"id": row[0], "kind": kind, "type": type_name, "name": row[3],
            "config": config, "enabled": row[5], "created": row[6].timestamp(),
            "label": spec.label if spec else type_name}


async def listing(kind: str, redact: bool = True) -> list[dict]:
    async with await _connect() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, kind, type, name, config, enabled, created FROM things "
                "WHERE kind = %s ORDER BY name", (kind,))
            return [_row(r, redact) for r in await cur.fetchall()]


async def get(kind: str, ident: str | int, redact: bool = False) -> dict:
    async with await _connect() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, kind, type, name, config, enabled, created FROM things "
                "WHERE kind = %s AND (lower(name) = lower(%s) OR id::text = %s)",
                (kind, str(ident), str(ident)))
            row = await cur.fetchone()
    if not row:
        raise HTTPException(404, f"no {kind} called {ident!r}")
    return _row(row, redact)


async def enabled_of_type(kind: str, type_name: str) -> list[dict]:
    return [t for t in await listing(kind, redact=False)
            if t["type"] == type_name and t["enabled"]]


def validate(spec: Type, config: dict) -> dict:
    clean: dict[str, Any] = {}
    for f in spec.fields:
        value = config.get(f.name, f.default)
        if f.required and (value is None or value == ""):
            raise HTTPException(400, f"{f.label} is required")
        if f.type == "boolean":
            value = bool(value)
        elif f.type == "number" and value not in ("", None):
            try:
                value = float(value)
            except (TypeError, ValueError):
                raise HTTPException(400, f"{f.label} must be a number")
            # A port of 993.0 is not a port. Keep whole numbers whole.
            if value.is_integer():
                value = int(value)
        elif f.choices and value not in f.choices and value != "":
            raise HTTPException(400, f"{f.label} must be one of: "
                                     f"{', '.join(f.choices)}")
        clean[f.name] = value
    return clean


# ------------------------------------------------------------------ http ----

class NewThing(BaseModel):
    type: str
    name: str = PField(min_length=1, max_length=80)
    config: dict = PField(default_factory=dict)
    enabled: bool = True


class EditThing(BaseModel):
    name: str | None = None
    config: dict | None = None
    enabled: bool | None = None


def make_router(kind: str) -> APIRouter:
    """One router per kind, identical behaviour. The whole point is that adding a
    kind, or a type within one, needs no new endpoint and no new UI."""
    router = APIRouter(prefix=f"/{kind}s", tags=[f"{kind}s"])
    admin = auth.require_role("creator", "admin")

    @router.get("/types")
    async def get_types(_: dict = Depends(admin)):
        return {"types": [t.as_json() for t in types_of(kind)]}

    @router.get("")
    async def get_all(_: dict = Depends(admin)):
        return {kind + "s": await listing(kind)}

    @router.post("")
    async def create(body: NewThing, user: dict = Depends(admin)):
        spec = spec_for(kind, body.type)
        config = validate(spec, body.config)
        try:
            async with await _connect() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "INSERT INTO things (kind, type, name, config, enabled) "
                        "VALUES (%s, %s, %s, %s, %s) "
                        "RETURNING id, kind, type, name, config, enabled, created",
                        (kind, body.type, body.name.strip(), json.dumps(config),
                         body.enabled))
                    row = await cur.fetchone()
        except psycopg.errors.UniqueViolation:
            raise HTTPException(409, f"a {kind} called {body.name!r} already exists")
        await activity.record(f"{kind}.add", f"{body.type}: {body.name}",
                              user["username"])
        return _row(row)

    @router.patch("/{ident}")
    async def update(ident: str, body: EditThing, user: dict = Depends(admin)):
        current = await get(kind, ident, redact=False)
        spec = spec_for(kind, current["type"])
        config = current["config"]
        if body.config is not None:
            # A masked secret coming back means "unchanged", not "set it to dots".
            incoming = {k: v for k, v in body.config.items() if v != MASK}
            config = validate(spec, {**config, **incoming})
        async with await _connect() as conn:
            await conn.execute(
                "UPDATE things SET name = %s, config = %s, enabled = %s WHERE id = %s",
                (body.name or current["name"], json.dumps(config),
                 current["enabled"] if body.enabled is None else body.enabled,
                 current["id"]))
        await activity.record(f"{kind}.edit", current["name"], user["username"])
        return await get(kind, current["id"])

    @router.delete("/{ident}")
    async def remove(ident: str, user: dict = Depends(admin)):
        current = await get(kind, ident, redact=False)
        async with await _connect() as conn:
            await conn.execute("DELETE FROM things WHERE id = %s", (current["id"],))
        await activity.record(f"{kind}.remove", current["name"], user["username"])
        return {"ok": True}

    @router.post("/{ident}/{action}")
    async def run_action(ident: str, action: str, user: dict = Depends(admin)):
        thing = await get(kind, ident, redact=False)
        spec = spec_for(kind, thing["type"])
        fn = spec.actions.get(action)
        if not fn:
            raise HTTPException(404, f"{thing['type']} has no action {action!r}")
        if not thing["enabled"]:
            raise HTTPException(409, f"{thing['name']} is disabled")
        result = await fn(thing, user)
        await activity.record(f"{kind}.{action}", thing["name"], user["username"])
        return result

    return router
