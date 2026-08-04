"""Home cameras (SPEC.md Phase 4).

IRiS can look at a camera and say what it sees: a frame is pulled from the RTSP
stream with ffmpeg and handed to the vision model that already reads uploaded
images. That is the whole first rung, and it needs no NVR.

Frigate (SPEC.md 5) is the continuous-recording half — motion and object detection,
event history, retention. It is deliberately not here yet: it needs per-camera
tuning, a hardware-acceleration decision and the actual camera inventory, none of
which can be guessed. §30 records what is still open.

Stream URLs carry credentials, so they are admin-only and never leave this service
intact: the registry redacts secret fields and ffmpeg's own error text is masked.

Cameras are one device *type* (SPEC.md 33) rather than a table of their own, so a
microphone or anything later is the same machinery with different fields.
"""
import asyncio
import base64
import os
import re
import time

from fastapi import HTTPException

import files
import registry
import settings

SNAPSHOT_TIMEOUT = 25

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


# Credentials in a stream URL are usually reused across a household's devices, so
# they must not reach a browser, the activity log, or a model's context. Greedy to
# the LAST @ in the authority: a password containing : or @ is legal and common, and
# a lazy match leaks the tail of it ("user:____@ss:word@host").
_CREDENTIALS = re.compile(r"(?<=://)([^/]*?):([^/]*)@")


def mask(url: str) -> str:
    """rtsp://user:hunter2@host/stream -> rtsp://user:____@host/stream"""
    return _CREDENTIALS.sub(lambda m: f"{m.group(1)}:____@", url)


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


# ------------------------------------------------------------ as a device ----

async def _describe_action(thing: dict, user: dict | None = None) -> dict:
    url = thing["config"].get("url", "")
    frame = await snapshot(thing["name"], url)
    text = await files._describe_image(frame, settings.get("cameras.prompt"))
    return {"camera": thing["name"], "description": text, "bytes": len(frame)}


async def _snapshot_action(thing: dict, user: dict | None = None) -> dict:
    """Returned as a data URI so the generic device UI needs no special case for it."""
    frame = await snapshot(thing["name"], thing["config"].get("url", ""))
    return {"image": "data:image/jpeg;base64," + base64.b64encode(frame).decode()}


registry.register(registry.Type(
    kind="device", name="camera", label="Camera",
    description="IRiS can look at it and say what it sees.",
    preset_field="make",
    presets={
        "Hikvision": {"url": "rtsp://USER:PASSWORD@ADDRESS:554/Streaming/Channels/101"},
        "Dahua": {"url": "rtsp://USER:PASSWORD@ADDRESS:554/cam/realmonitor"
                         "?channel=1&subtype=0"},
        "Reolink": {"url": "rtsp://USER:PASSWORD@ADDRESS:554/h264Preview_01_main"},
        "UniFi Protect": {"url": "rtsps://ADDRESS:7441/STREAM-KEY?enableSrtp"},
        "Amcrest": {"url": "rtsp://USER:PASSWORD@ADDRESS:554/cam/realmonitor"
                           "?channel=1&subtype=0"},
        "Other": {},
    },
    fields=[
        registry.Field("make", "Make", type="choice", required=True, default="Other",
                       choices=["Hikvision", "Dahua", "Reolink", "UniFi Protect",
                                "Amcrest", "Other"],
                       help="Fills in the usual stream address for that make."),
        registry.Field("url", "Stream URL", type="password", required=True,
                       secret=True,
                       help="Replace the capitals with yours."),
    ],
    actions={"describe": _describe_action, "snapshot": _snapshot_action},
    action_labels={"describe": "look", "snapshot": "snapshot"},
))


async def describe(name_or_id: str) -> dict:
    """Used by the look_at_camera tool."""
    thing = await registry.get("device", name_or_id, redact=False)
    if thing["type"] != "camera":
        raise HTTPException(400, f"{thing['name']} is not a camera")
    if not thing["enabled"]:
        raise HTTPException(409, f"{thing['name']} is disabled")
    return await _describe_action(thing)


async def camera_names() -> list[str]:
    return [c["name"] for c in await registry.enabled_of_type("device", "camera")]
