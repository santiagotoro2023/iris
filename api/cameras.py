"""Home cameras (SPEC.md Phase 4).

IRiS can look at a camera and say what it sees: a frame is pulled from the RTSP
stream with ffmpeg and handed to the vision model that already reads uploaded
images. That is the whole first rung, and it needs no NVR.

Frigate (SPEC.md 5) is the continuous-recording half — motion and object detection,
event history, retention. It is deliberately not here yet: it needs per-camera
tuning, a hardware-acceleration decision and the actual camera inventory, none of
which can be guessed. §30 records what is still open.

Stream URLs carry credentials, so they are admin-only and never leave this service
intact: everything that goes to a client has the password masked.
"""
import asyncio
import os
import re
import time

import psycopg
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

import activity
import auth
import files
import settings

DATABASE_URL = os.environ.get("DATABASE_URL", "")
SNAPSHOT_TIMEOUT = 25

router = APIRouter(prefix="/cameras", tags=["cameras"])

settings.setting(
    "cameras.enabled", type="boolean", default=True,
    title="Cameras",
    description="Let IRiS look at the cameras when asked. Off hides the tool entirely.")
settings.setting(
    "cameras.prompt", type="string", format="multiline",
    default="Describe what is visible in this security camera image. Say plainly what "
            "you see: people, vehicles, animals, parcels, open doors, anything out of "
            "place. If nothing is happening, say so in one sentence. Do not speculate "
            "about what might be outside the frame.",
    title="What to ask about a camera view",
    description="The question put to the vision model with each frame.")
settings.setting(
    "cameras.snapshot_cache", type="integer", minimum=0, maximum=300, default=10,
    title="Reuse a frame for (seconds)",
    description="Pulling a frame takes a few seconds and wakes the camera. Within "
                "this window the last frame is reused instead of fetching another.")


# ---------------------------------------------------------------- storage ----

async def _connect():
    return await psycopg.AsyncConnection.connect(DATABASE_URL, autocommit=True)


async def init() -> None:
    async with await _connect() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS cameras (
                id       BIGSERIAL PRIMARY KEY,
                name     TEXT NOT NULL UNIQUE,
                url      TEXT NOT NULL,
                enabled  BOOLEAN NOT NULL DEFAULT TRUE,
                created  TIMESTAMPTZ NOT NULL DEFAULT now()
            )""")


# Greedy to the LAST @ in the authority: a password containing : or @ is legal and
# common, and a lazy match leaks the tail of it ("user:____@ss:word@host").
_CREDENTIALS = re.compile(r"(?<=://)([^/]*?):([^/]*)@")


def mask(url: str) -> str:
    """rtsp://user:hunter2@host/stream -> rtsp://user:____@host/stream

    Camera passwords are usually reused across a household's devices, so they must
    not travel to a browser, into the activity log, or into a model's context.
    """
    return _CREDENTIALS.sub(lambda m: f"{m.group(1)}:____@", url)


def _public(row: tuple) -> dict:
    return {"id": row[0], "name": row[1], "url": mask(row[2]), "enabled": row[3],
            "created": row[4].timestamp()}


async def listing() -> list[dict]:
    async with await _connect() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT id, name, url, enabled, created FROM cameras "
                              "ORDER BY name")
            return [_public(r) for r in await cur.fetchall()]


async def _url_for(name_or_id: str) -> tuple[str, str]:
    async with await _connect() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT name, url FROM cameras WHERE enabled AND "
                "(lower(name) = lower(%s) OR id::text = %s)",
                (name_or_id, name_or_id))
            row = await cur.fetchone()
    if not row:
        raise HTTPException(404, f"no enabled camera called {name_or_id!r}")
    return row[0], row[1]


# --------------------------------------------------------------- capture ----

_cache: dict[str, tuple[float, bytes]] = {}


async def snapshot(name: str, url: str) -> bytes:
    """One JPEG from the stream.

    RTSP goes over TCP because UDP loses packets on wifi cameras and produces
    smeared frames that the vision model then earnestly describes as fog.
    """
    ttl = settings.get("cameras.snapshot_cache")
    hit = _cache.get(name)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]

    # -rtsp_transport is only a valid option for an RTSP input; passing it to an
    # http:// snapshot URL makes ffmpeg refuse the whole command.
    transport = ["-rtsp_transport", "tcp"] if url.startswith("rtsp") else []
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-nostdin", "-loglevel", "error", *transport, "-i", url,
        "-frames:v", "1", "-q:v", "3", "-f", "image2", "-",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), SNAPSHOT_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        raise HTTPException(504, f"{name} did not send a frame within "
                                 f"{SNAPSHOT_TIMEOUT}s")
    if not out:
        # Never let ffmpeg's error text through verbatim: it echoes the URL, password
        # and all.
        raise HTTPException(502, f"could not read a frame from {name}: "
                                 f"{mask(err.decode()[:200])}")
    _cache[name] = (time.monotonic(), out)
    return out


async def describe(name_or_id: str) -> dict:
    name, url = await _url_for(name_or_id)
    frame = await snapshot(name, url)
    text = await files._describe_image(frame, settings.get("cameras.prompt"))
    return {"camera": name, "description": text, "bytes": len(frame)}


# ------------------------------------------------------------------ http ----

class NewCamera(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    url: str = Field(min_length=1, max_length=500)
    enabled: bool = True


_admin = auth.require_role("creator", "admin")


@router.get("")
async def get_all(_: dict = Depends(_admin)):
    return {"cameras": await listing()}


@router.post("")
async def add(body: NewCamera, user: dict = Depends(_admin)):
    if not body.url.startswith(("rtsp://", "rtsps://", "http://", "https://")):
        raise HTTPException(400, "expected an rtsp:// or http:// stream URL")
    try:
        async with await _connect() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO cameras (name, url, enabled) VALUES (%s, %s, %s) "
                    "RETURNING id, name, url, enabled, created",
                    (body.name.strip(), body.url.strip(), body.enabled))
                row = await cur.fetchone()
    except psycopg.errors.UniqueViolation:
        raise HTTPException(409, f"a camera called {body.name!r} already exists")
    await activity.record("camera.add", body.name, user["username"])
    return _public(row)


@router.delete("/{camera_id}")
async def remove(camera_id: int, user: dict = Depends(_admin)):
    async with await _connect() as conn:
        await conn.execute("DELETE FROM cameras WHERE id = %s", (camera_id,))
    await activity.record("camera.remove", str(camera_id), user["username"])
    return {"ok": True}


@router.get("/{camera_id}/snapshot")
async def frame(camera_id: str, _: dict = Depends(_admin)):
    name, url = await _url_for(camera_id)
    return Response(await snapshot(name, url), media_type="image/jpeg",
                    headers={"cache-control": "no-store"})


@router.get("/{camera_id}/describe")
async def look(camera_id: str, user: dict = Depends(_admin)):
    out = await describe(camera_id)
    await activity.record("camera.look", f"{out['camera']}: {out['description'][:120]}",
                          user["username"])
    return out
