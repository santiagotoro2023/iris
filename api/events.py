"""Camera events, off the MQTT bus that has been idle since Phase 0 (SPEC.md 37).

Frigate publishes every detection to `frigate/events`, and subscribing to it is what
turns a detection into something IRiS can mention, brief on, or fire a rule from.
That is the meeting point of 33, 34 and 37 the spec asks for.

There is no camera on this machine yet, so nothing here is allowed to depend on one:
the subscriber runs whenever the broker is reachable, and a hand-published event
flows the whole way through. That is how this was built and how it is checked.

    docker compose exec mqtt mosquitto_pub -t frigate/events -m \\
      '{"type":"new","after":{"id":"1754380000.1-abc","camera":"front_door",
        "label":"person","score":0.87,"start_time":1754380000,"has_snapshot":true}}'

paho runs its network loop on a thread of its own, so a message is parsed there and
then handed back to the asyncio loop with `run_coroutine_threadsafe`. Nothing on that
thread may touch Redis: the client belongs to the loop that created it.
"""
import asyncio
import json
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends

import auth
import frigate
import registry
import settings

router = APIRouter(prefix="/events", tags=["cameras"])

TOPIC = "frigate/events"
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
KEY = "events:frigate"
MAX_LINES = 12                  # a busy evening is not worth reading out in full

settings.setting(
    "events.enabled", type="boolean", default=True,
    title="Record camera events",
    description="Keep what Frigate detects, so IRiS can say what happened while you "
                "were out. Off stops recording them, and anything watching the "
                "cameras then has nothing to report.")
settings.setting(
    "events.keep", type="integer", minimum=50, maximum=5000, default=500,
    title="Camera events kept",
    description="Only the most recent are kept. One camera on a busy street can "
                "produce hundreds in an evening.")


# ---------------------------------------------------------------- parsing ----

def _parse(payload: bytes) -> dict | None:
    """One Frigate message into the fields worth keeping, or None to ignore it.

    `update` arrives several times a second for as long as an object is tracked, so
    only the first and the last state of an event are stored. The `end` is the one
    worth waiting for: it is what carries the duration and the final snapshot.
    """
    message = json.loads(payload)
    if message.get("type") not in ("new", "end"):
        return None
    data = message.get("after") or message.get("before") or {}
    ident = str(data.get("id") or "")
    if not ident:
        return None
    return {
        "id": ident,
        "camera": str(data.get("camera") or ""),
        "label": str(data.get("label") or "object"),
        "score": round(float(data.get("top_score") or data.get("score") or 0), 3),
        "start_time": float(data.get("start_time") or time.time()),
        # Absent until the object leaves, which is how an event still in progress is
        # told apart from one that is over.
        "end_time": float(data["end_time"]) if data.get("end_time") else None,
        "has_snapshot": bool(data.get("has_snapshot")),
        "has_clip": bool(data.get("has_clip")),
        # Frigate publishes no "zones" key: it publishes the zones the object is in
        # right now and the ones it has entered. The second is the one that survives
        # an object walking back out again.
        "zones": list(data.get("entered_zones") or data.get("current_zones") or []),
        "snapshot": frigate.snapshot_url(ident) if data.get("has_snapshot") else "",
    }


# ---------------------------------------------------------------- storage ----

async def _store(event: dict) -> None:
    import chat
    # Checked here rather than by disconnecting from the broker: the setting can
    # change while IRiS is running, and a subscriber that needs a restart to notice
    # is the worse trade of the two.
    if not settings.get("events.enabled"):
        return
    if chat._redis is None:
        print("[events] dropped an event: Redis is not connected yet", flush=True)
        return
    # ponytail: a capped Redis list. These are ephemeral by nature and cost nothing
    # to lose; Postgres is the upgrade path if they ever need searching or keeping
    # for longer than the cap holds.
    keep = max(1, settings.get("events.keep"))
    await chat._redis.lpush(KEY, json.dumps(event))
    await chat._redis.ltrim(KEY, 0, keep - 1)
    print(f"[events] {event['label']} on {event['camera'] or 'a camera'} "
          f"({'ended' if event['end_time'] else 'started'})", flush=True)


async def _all() -> list[dict]:
    import chat
    if chat._redis is None:
        return []
    out = []
    for item in await chat._redis.lrange(KEY, 0, -1):
        try:
            out.append(json.loads(item))
        except ValueError:
            print("[events] skipped a stored event that would not parse", flush=True)
    return out


def _filter(events: list[dict], after: float, camera: str = "",
            objects: str = "") -> list[dict]:
    """Newest first, one entry per event.

    Both the start and the end of an event are stored, so the same id is in the list
    twice and the copy carrying the end wins. Camera names go through `frigate.slug`
    because the registry holds "Front door" and Frigate publishes "front_door": a
    plain comparison matches nothing at all, silently.
    """
    wanted = {o.strip().lower() for o in objects.split(",") if o.strip()}
    key = frigate.slug(camera) if camera.strip() else ""
    picked: dict[str, dict] = {}
    for event in events:
        if (event.get("start_time") or 0) < after:
            continue
        if key and frigate.slug(event.get("camera") or "") != key:
            continue
        if wanted and (event.get("label") or "").lower() not in wanted:
            continue
        held = picked.get(event.get("id") or "")
        if held is None or (event.get("end_time") or 0) > (held.get("end_time") or 0):
            picked[event.get("id") or ""] = event
    return sorted(picked.values(), key=lambda e: e.get("start_time") or 0,
                  reverse=True)


async def since(timestamp: float, camera: str = "", objects: str = "") -> list[dict]:
    """Everything that started after `timestamp`, a unix time."""
    return _filter(await _all(), timestamp, camera, objects)


async def recent(hours: float = 12, camera: str = "", objects: str = "") -> list[dict]:
    return await since(time.time() - hours * 3600, camera, objects)


# ------------------------------------------------------------- in prose ----

def _duration(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds} second{'' if seconds == 1 else 's'}"
    minutes = seconds // 60
    return f"{minutes} minute{'' if minutes == 1 else 's'}"


def _sentence(event: dict) -> str:
    zone = ZoneInfo(settings.get("general.timezone"))
    started = datetime.fromtimestamp(event.get("start_time") or 0, zone)
    # The day is only worth saying when it is not today, which is the common case
    # for a briefing looking twelve hours back over a morning.
    when = started.strftime("%H:%M" if started.date() == datetime.now(zone).date()
                            else "%a %H:%M")
    where = (event.get("camera") or "camera").replace("_", " ")
    line = f"{event.get('label') or 'something'} at the {where} at {when}"
    end = event.get("end_time")
    if end:
        return f"{line}, {_duration(end - (event.get('start_time') or end))}."
    return f"{line}, still going."


async def describe(hours: float = 12, camera: str = "", objects: str = "") -> str:
    """Written to be read out, not rendered."""
    found = await recent(hours, camera, objects)
    if not found:
        if not await registry.enabled_of_type("device", "camera"):
            # A different fact from "nothing happened", and the only one of the two
            # that anybody can act on.
            return "No camera is set up yet."
        return f"No camera events in the last {hours:g} hours."
    lines = [_sentence(e) for e in found[:MAX_LINES]]
    if len(found) > MAX_LINES:
        lines.append(f"And {len(found) - MAX_LINES} earlier ones.")
    return "\n".join(lines)


# -------------------------------------------------------------- listening ----

_client = None                  # kept so the network thread is not garbage collected
_connected = False


def _on_connect(client, userdata, flags, reason_code, properties=None) -> None:
    """Subscribing from the callback rather than after `connect()`.

    A reconnect starts a fresh session with the broker, and a subscription made once
    at startup does not come back with it: the client stays connected and receives
    nothing, which looks exactly like a quiet camera.
    """
    global _connected
    _connected = not reason_code.is_failure
    if not _connected:
        print(f"[events] broker refused the connection: {reason_code}", flush=True)
        return
    client.subscribe(TOPIC, qos=0)
    print(f"[events] listening to {TOPIC} on {frigate.MQTT_HOST}", flush=True)


def _on_disconnect(client, userdata, flags, reason_code, properties=None) -> None:
    global _connected
    _connected = False
    print(f"[events] disconnected from the broker ({reason_code}), retrying",
          flush=True)


def _on_message(loop, message) -> None:
    """On paho's thread. Parses there, stores on the event loop."""
    try:
        event = _parse(message.payload)
    except Exception as e:
        print(f"[events] unreadable message on {message.topic}: {e}", flush=True)
        return
    if event:
        asyncio.run_coroutine_threadsafe(_store(event), loop)


async def listener() -> None:
    """Started once from main's lifespan; paho owns a thread from then on.

    `connect_async` plus `loop_start`, never `connect`: compose starts the broker and
    IRiS together, so a broker that is not up yet is the normal case, and `connect`
    would turn that into a permanent failure rather than a retry.
    """
    global _client
    try:
        # Imported here so the rest of this module, and anything that reads the
        # stored events, still works on an install without paho.
        import paho.mqtt.client as mqtt
    except ImportError as e:
        print(f"[events] camera events are off, paho-mqtt is missing: {e}", flush=True)
        return
    loop = asyncio.get_running_loop()
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="iris-events")
    client.on_connect = _on_connect
    client.on_disconnect = _on_disconnect
    client.on_message = lambda _c, _u, message: _on_message(loop, message)
    client.reconnect_delay_set(min_delay=1, max_delay=60)
    client.connect_async(frigate.MQTT_HOST, MQTT_PORT, keepalive=60)
    client.loop_start()
    _client = client


# ------------------------------------------------------------------ http ----

@router.get("")
async def feed(hours: float = 12, camera: str = "", objects: str = "",
               _: dict = Depends(auth.active_user)):
    """`listening` is part of the answer: an empty feed with no connection is a
    fault, an empty feed with one is a quiet house."""
    return {"events": await recent(hours, camera, objects),
            "listening": _connected,
            "cameras": [c["name"] for c in
                        await registry.enabled_of_type("device", "camera")]}


if __name__ == "__main__":
    # One synthetic event, shaped exactly as Frigate publishes it, through the two
    # pieces that have branches. No broker, no Redis, no camera:
    #   docker compose exec api python events.py
    NEW = json.dumps({"type": "new", "after": {
        "id": "1754380000.1-abc", "camera": "front_door", "label": "person",
        "score": 0.87, "start_time": 1754380000.0, "has_snapshot": True,
        "entered_zones": ["porch"]}}).encode()
    END = json.dumps({"type": "end", "after": {
        "id": "1754380000.1-abc", "camera": "front_door", "label": "person",
        "score": 0.91, "start_time": 1754380000.0, "end_time": 1754380008.0,
        "has_snapshot": True, "has_clip": True}}).encode()

    started, ended = _parse(NEW), _parse(END)
    assert _parse(json.dumps({"type": "update", "after": {"id": "x"}}).encode()) is None
    assert started["end_time"] is None and started["zones"] == ["porch"]
    assert started["snapshot"].endswith("/api/events/1754380000.1-abc/snapshot.jpg")
    one = _filter([started, ended], 0)
    assert len(one) == 1 and one[0]["end_time"] == 1754380008.0, one
    assert _filter([started, ended], 1754380001) == []
    assert _filter([started, ended], 0, camera="Front Door"), "slug, not the raw name"
    assert _filter([started, ended], 0, camera="garden") == []
    assert _filter([started, ended], 0, objects="car, dog") == []
    assert _filter([started, ended], 0, objects="person")
    assert _duration(8) == "8 seconds" and _duration(1) == "1 second"
    assert _duration(125) == "2 minutes"
    print("events: ok")
