"""Automations: things that speak when something happens (SPEC.md Phase 7, 55).

A briefing runs at a time. A rule runs when the world changes: mail from a particular
person arrives, rain is coming before you leave, an appointment is close enough that
you should be walking to the station, a camera saw somebody, the disk is filling up.

Two things this module is careful about, because both failure modes are worse than the
feature is good:

**Dedupe.** A trigger is a *check*, not an event queue, so without a key a rule fires
on every poll and IRiS becomes a machine that says the same thing every thirty seconds
until you switch it off. Each check returns a key with its facts, and a key that has
been seen is not delivered again.

**Isolation.** One slow IMAP login must not stop the weather rule, so checks run
concurrently and an exception in one is logged and skipped rather than ending the loop.
"""
import asyncio
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

import httpx
import psycopg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field as PField

import activity
import auth
import registry
import settings

DATABASE_URL = os.environ.get("DATABASE_URL", "")
router = APIRouter(prefix="/rules", tags=["proactive"])

# How many recent keys a rule remembers. Enough that a camera rule cannot forget an
# event still inside its own quiet window, small enough to stay a cheap JSON column.
SEEN_LIMIT = 200
DELIVERIES = ["chat", "push", "webhook"]


@dataclass
class Fired:
    facts: str
    key: str


@dataclass
class Trigger:
    name: str
    label: str
    description: str
    check: Callable                  # async (rule) -> Fired | None
    fields: list = field(default_factory=list)
    interval: int = 60               # seconds between checks
    needs: str = ""

    def as_json(self) -> dict:
        return {"name": self.name, "label": self.label,
                "description": self.description, "interval": self.interval,
                "fields": [f.as_json() for f in self.fields]}


TRIGGERS: dict[str, Trigger] = {}


def register(trigger: Trigger) -> Trigger:
    TRIGGERS[trigger.name] = trigger
    return trigger


def catalogue() -> list[dict]:
    return [t.as_json() for t in TRIGGERS.values()
            if not t.needs or settings.get(t.needs)]


def _now() -> datetime:
    return datetime.now(ZoneInfo(settings.get("general.timezone")))


def _cfg(rule: dict, name: str, default=""):
    value = (rule.get("config") or {}).get(name, default)
    return value if value not in (None, "") else default


# ------------------------------------------------------------- mail arrives ----

async def _check_mail(rule: dict) -> Fired | None:
    import integrations
    from_wanted = str(_cfg(rule, "from_contains")).lower()
    subject_wanted = str(_cfg(rule, "subject_contains")).lower()
    only = str(_cfg(rule, "mailbox")).lower()

    boxes = await registry.enabled_of_type("integration", "mailbox")
    if only:
        boxes = [b for b in boxes if only in b["name"].lower()]
    for box in boxes:
        # Structured headers, not the rendered summary: matching "from" against a
        # string that also holds the subject would fire on the wrong half.
        result = await integrations._check_mail(box)
        for message in result["messages"]:
            sender = (message.get("from") or "").lower()
            subject = (message.get("subject") or "").lower()
            if from_wanted and from_wanted not in sender:
                continue
            if subject_wanted and subject_wanted not in subject:
                continue
            return Fired(
                facts=f"New mail in {box['name']} from {message.get('from')}, "
                      f"subject: {message.get('subject')}.",
                key=f"mail|{message.get('from')}|{message.get('subject')}")
    return None


register(Trigger(
    name="mail_arrives", label="Mail arrives",
    description="When a message matching what you describe turns up. Leave a field "
                "blank to match anything.",
    check=_check_mail, interval=300,
    fields=[
        registry.Field("from_contains", "From contains",
                       help="Part of the sender, e.g. a name or a domain."),
        registry.Field("subject_contains", "Subject contains"),
        registry.Field("mailbox", "Only this mailbox",
                       help="Blank checks every mailbox under Integrations."),
    ]))


# ------------------------------------------------------------ weather turns ----

_CONDITIONS = ["Rain likely", "Frost", "Hot", "Snow"]


async def _check_weather(rule: dict) -> Fired | None:
    import places
    condition = _cfg(rule, "condition", "Rain likely")
    threshold = float(_cfg(rule, "threshold", 60) or 60)
    hours = int(float(_cfg(rule, "hours_ahead", 12) or 12))
    place = _cfg(rule, "place") or settings.get("location.home")

    coords = (places._fixed_coordinates() if not place
              else await places._coordinates(place))
    if not coords:
        # Not silent: a rule that can never fire should say why rather than look
        # like a rule that is simply patient.
        raise RuntimeError(f"could not place {place or 'your position'} on the map")
    lat, lon = coords
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(places.WEATHER_URL, params={
            "latitude": lat, "longitude": lon,
            "hourly": "temperature_2m,precipitation_probability,snowfall",
            "timezone": settings.get("general.timezone"), "forecast_days": 2})
    if r.status_code != 200:
        raise RuntimeError(f"the forecast returned HTTP {r.status_code}")
    hourly = r.json().get("hourly") or {}
    times = hourly.get("time") or []
    now = _now()
    window = [i for i, stamp in enumerate(times)
              if now <= datetime.fromisoformat(stamp).replace(tzinfo=now.tzinfo)
              <= now + timedelta(hours=hours)]
    if not window:
        return None

    def series(name: str) -> list[float]:
        values = hourly.get(name) or []
        return [values[i] for i in window if i < len(values) and values[i] is not None]

    if condition == "Rain likely":
        chance = max(series("precipitation_probability"), default=0)
        if chance >= threshold:
            facts = (f"Rain is likely near {place or 'you'}: up to {int(chance)}% "
                     f"chance in the next {hours} hours.")
        else:
            return None
    elif condition == "Snow":
        snow = max(series("snowfall"), default=0)
        if snow <= 0:
            return None
        facts = f"Snow is forecast near {place or 'you'} in the next {hours} hours."
    elif condition == "Frost":
        coldest = min(series("temperature_2m"), default=99)
        if coldest > threshold:
            return None
        facts = (f"It is due to drop to {round(coldest)}C near {place or 'you'} "
                 f"within {hours} hours.")
    else:
        hottest = max(series("temperature_2m"), default=-99)
        if hottest < threshold:
            return None
        facts = (f"It is due to reach {round(hottest)}C near {place or 'you'} "
                 f"within {hours} hours.")
    # Once a day at most: the forecast does not stop saying it is going to rain just
    # because it has been mentioned.
    return Fired(facts=facts, key=f"weather|{condition}|{now:%Y%m%d}")


register(Trigger(
    name="weather_turns", label="Weather turns",
    description="When rain, frost, heat or snow is coming. Checked against the hourly "
                "forecast, and said once a day at most.",
    check=_check_weather, interval=900, needs="location.enabled",
    fields=[
        registry.Field("place", "Where", help="Blank uses Home."),
        registry.Field("condition", "What to watch for", type="choice",
                       default="Rain likely", choices=_CONDITIONS),
        registry.Field("threshold", "Threshold", type="number", default=60,
                       help="Percent chance for rain, degrees for frost and heat."),
        registry.Field("hours_ahead", "How far ahead (hours)", type="number",
                       default=12),
    ]))


# ---------------------------------------------------------- calendar coming ----

async def _check_calendar(rule: dict) -> Fired | None:
    import integrations
    minutes = int(float(_cfg(rule, "minutes_before", 45) or 45))
    with_travel = bool(_cfg(rule, "with_travel", False))
    now = _now()
    cutoff = now + timedelta(minutes=minutes)

    cals = (await registry.enabled_of_type("integration", "calendar")
            + await registry.enabled_of_type("integration", "calendar_ics"))
    for cal in cals:
        result = await (integrations._check_ics(cal) if cal["type"] == "calendar_ics"
                        else integrations._check_calendar(cal))
        for event in result.get("events") or result.get("messages") or []:
            summary = event.get("subject") or event.get("summary") or ""
            when_text = event.get("from") or ""
            start = _event_start(event)
            if not start or not (now < start <= cutoff):
                continue
            facts = (f"{summary} starts at {start:%H:%M}, in "
                     f"{int((start - now).total_seconds() // 60)} minutes.")
            location = event.get("location") or _location_in(summary)
            if with_travel and location:
                import places
                try:
                    facts += "\n" + await places.route_to(
                        location, start.strftime("%H:%M"), arrive_by=True)
                except Exception as e:
                    facts += f"\nThe journey there could not be worked out: {e}"
            return Fired(facts=facts, key=f"calendar|{summary}|{when_text}|{start}")
    return None


def _event_start(event: dict) -> datetime | None:
    """The integrations layer hands back a rendered "today 16:00" for display. Parse
    that rather than reaching into its internals, which are not part of its
    interface."""
    text = (event.get("from") or "").strip()
    clock = text.split()[-1] if text else ""
    if ":" not in clock:
        return None
    try:
        hour, minute = (int(p) for p in clock.split(":")[:2])
    except ValueError:
        return None
    now = _now()
    day = now.date() + (timedelta(days=1) if "tomorrow" in text.lower()
                        else timedelta(0))
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=now.tzinfo)


def _location_in(summary: str) -> str:
    """Calendars often put the place in brackets after the title, which is where the
    integrations layer puts it too."""
    if "(" in summary and summary.rstrip().endswith(")"):
        return summary[summary.rindex("(") + 1:-1].strip()
    return ""


register(Trigger(
    name="calendar_soon", label="Appointment coming up",
    description="Shortly before something starts, optionally with how to get there.",
    check=_check_calendar, interval=120,
    fields=[
        registry.Field("minutes_before", "How long before (minutes)", type="number",
                       default=45),
        registry.Field("with_travel", "Work out the journey", type="boolean",
                       default=True,
                       help="Only when the appointment carries a location."),
    ]))


# -------------------------------------------------------------- camera saw ----

async def _check_camera(rule: dict) -> Fired | None:
    import events as camera_events
    quiet = int(float(_cfg(rule, "quiet_minutes", 10) or 10))
    found = await camera_events.recent(
        hours=max(quiet, 1) / 60.0, camera=_cfg(rule, "camera"),
        objects=_cfg(rule, "objects", "person"))
    if not found:
        return None
    event = found[0]
    where = event.get("camera", "a camera")
    what = event.get("label", "something")
    # Keyed on the camera and a quiet-window bucket rather than the event id: a person
    # walking past a door produces forty events, and forty notifications is a fault.
    bucket = int(event.get("start_time", 0) // max(quiet * 60, 60))
    return Fired(facts=f"The {where} camera saw a {what}.",
                 key=f"camera|{where}|{what}|{bucket}")


register(Trigger(
    name="camera_event", label="A camera saw something",
    description="When a camera detects what you care about. Grouped so one visitor is "
                "one message, not forty.",
    check=_check_camera, interval=30, needs="events.enabled",
    fields=[
        registry.Field("camera", "Which camera", help="Blank watches all of them."),
        registry.Field("objects", "What counts", default="person",
                       help="Comma separated, e.g. person,car."),
        registry.Field("quiet_minutes", "Group events within (minutes)",
                       type="number", default=10),
    ]))


# ------------------------------------------------------------ system health ----

async def _check_system(rule: dict) -> Fired | None:
    import system
    disk_floor = float(_cfg(rule, "disk_below_gb", 10) or 10)
    gpu_ceiling = float(_cfg(rule, "gpu_above_c", 85) or 85)
    watch_services = bool(_cfg(rule, "service_down", True))

    snapshot = await system.snapshot()
    complaints = []
    free_gb = (snapshot.get("disk") or {}).get("free_gb", 0)
    if free_gb and free_gb < disk_floor:
        complaints.append(f"the disk is down to {free_gb:.1f} GB free")
    # No GPU at all is None, not a temperature of zero, so this must not read it
    # as a card running very cold indeed.
    gpu = snapshot.get("gpu") or {}
    if gpu.get("celsius") and gpu["celsius"] > gpu_ceiling:
        complaints.append(f"the GPU is at {int(gpu['celsius'])}C")
    if watch_services:
        down = [name for name, health in (snapshot.get("services") or {}).items()
                if not health.get("up")]
        if down:
            complaints.append(f"{', '.join(down)} unreachable")
    if not complaints:
        return None
    # Once a day per shape of complaint: a full disk stays full, and being told hourly
    # does not make it emptier.
    return Fired(facts="This machine: " + ", and ".join(complaints) + ".",
                 key=f"system|{'|'.join(sorted(complaints))[:80]}|{_now():%Y%m%d}")


register(Trigger(
    name="system_health", label="This machine is unwell",
    description="Disk filling up, GPU running hot, a service that stopped answering.",
    check=_check_system, interval=300,
    fields=[
        registry.Field("disk_below_gb", "Warn under (GB free)", type="number",
                       default=10),
        registry.Field("gpu_above_c", "Warn over (GPU C)", type="number", default=85),
        registry.Field("service_down", "Watch the services", type="boolean",
                       default=True),
    ]))


# ---------------------------------------------------------------- storage ----

async def _connect():
    return await psycopg.AsyncConnection.connect(DATABASE_URL, autocommit=True)


_COLUMNS = "id, name, trigger, enabled, config, action, state, created, fired"
DEFAULT_ACTION = {"deliver": ["chat"], "message": "", "briefing": ""}


def _row(row: tuple) -> dict:
    return {"id": row[0], "name": row[1], "trigger": row[2], "enabled": row[3],
            "config": row[4] or {}, "action": {**DEFAULT_ACTION, **(row[5] or {})},
            "state": row[6] or {},
            "created": row[7].timestamp() if row[7] else 0,
            "fired": row[8].timestamp() if row[8] else 0,
            "label": (TRIGGERS[row[2]].label if row[2] in TRIGGERS else row[2])}


async def init() -> None:
    async with await _connect() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS rules (
                id      BIGSERIAL PRIMARY KEY,
                name    TEXT NOT NULL UNIQUE,
                trigger TEXT NOT NULL,
                enabled BOOLEAN NOT NULL DEFAULT TRUE,
                config  JSONB NOT NULL DEFAULT '{}',
                action  JSONB NOT NULL DEFAULT '{}',
                state   JSONB NOT NULL DEFAULT '{}',
                created TIMESTAMPTZ NOT NULL DEFAULT now(),
                fired   TIMESTAMPTZ
            )""")


async def listing() -> list[dict]:
    async with await _connect() as conn:
        cur = await conn.execute(f"SELECT {_COLUMNS} FROM rules ORDER BY name")
        return [_row(r) for r in await cur.fetchall()]


async def get(ident) -> dict:
    async with await _connect() as conn:
        cur = await conn.execute(f"SELECT {_COLUMNS} FROM rules WHERE id::text = %s",
                                 (str(ident),))
        row = await cur.fetchone()
    if not row:
        raise HTTPException(404, f"no automation {ident!r}")
    return _row(row)


async def _remember(rule: dict, key: str) -> None:
    seen = [k for k in (rule["state"].get("seen") or []) if k != key]
    seen.append(key)
    state = {**rule["state"], "seen": seen[-SEEN_LIMIT:]}
    async with await _connect() as conn:
        await conn.execute(
            "UPDATE rules SET state = %s, fired = now() WHERE id = %s",
            (json.dumps(state), rule["id"]))


# --------------------------------------------------------------- delivering ----

WORDING = """\
Something happened that {who} asked to be told about. Say it in one or two sentences,
plainly, as a notification. No greeting, no sign-off, no offer of help, no speculation
beyond what is written here.

WHAT HAPPENED:
"""


async def _wording(rule: dict, facts: str) -> str:
    written = (rule["action"].get("message") or "").strip()
    if written:
        return written
    import memory
    import reasoning
    try:
        text = await memory._complete(
            WORDING.format(who=settings.get("persona.address")) + facts, "")
    except Exception as e:
        # The facts are the point. Losing the notification because the wording failed
        # would be the wrong way round.
        print(f"[rules] wording failed, sending the facts: {e}", flush=True)
        return facts
    return reasoning.strip_emoji(reasoning.strip_dashes(text.strip())) or facts


async def fire(rule: dict, found: Fired) -> dict:
    import briefings
    import integrations
    import proactive
    user = await proactive._first_user()
    if not user:
        raise RuntimeError("no user to deliver to")

    named = (rule["action"].get("briefing") or "").strip()
    if named:
        briefing = await briefings.by_name(named)
        if not briefing:
            raise RuntimeError(f"no briefing called {named!r}")
        return await briefings.run_and_deliver(briefing, user)

    text = await _wording(rule, found.facts)
    deliver = rule["action"].get("deliver") or ["chat"]
    result = {"text": text, "delivered": []}
    if "chat" in deliver:
        await proactive.deliver(text, user["id"], user["username"],
                                title=rule["name"])
        result["delivered"].append("chat")
    if "push" in deliver or "webhook" in deliver:
        sent = await integrations.notify(text, event="rule")
        result["delivered"].append(f"{sent} channel(s)")
    return result


# ------------------------------------------------------------------- loop ----

_last_checked: dict[int, float] = {}


async def _tick(rule: dict) -> None:
    import proactive
    trigger = TRIGGERS.get(rule["trigger"])
    if not trigger:
        return
    found = await trigger.check(rule)
    if not found:
        return
    if found.key in (rule["state"].get("seen") or []):
        return
    # Recorded before delivering: a crash between the two costs one notification,
    # where the other order costs one every thirty seconds until it is switched off.
    await _remember(rule, found.key)
    if proactive.in_quiet_hours():
        print(f"[rules] {rule['name']!r} fired inside quiet hours, not delivered",
              flush=True)
        return
    await fire(rule, found)
    await activity.record("rule.fired", rule["name"], "iris")
    print(f"[rules] {rule['name']!r} fired", flush=True)


async def scheduler() -> None:
    await asyncio.sleep(8)
    while True:
        try:
            if settings.get("proactive.enabled"):
                now = asyncio.get_event_loop().time()
                due = []
                for rule in await listing():
                    trigger = TRIGGERS.get(rule["trigger"])
                    if not rule["enabled"] or not trigger:
                        continue
                    if now - _last_checked.get(rule["id"], 0) < trigger.interval:
                        continue
                    _last_checked[rule["id"]] = now
                    due.append(rule)
                # Concurrently, and never letting one failure end the loop: a mailbox
                # that has stopped answering must not stop the weather rule.
                for rule, outcome in zip(due, await asyncio.gather(
                        *(_tick(r) for r in due), return_exceptions=True)):
                    if isinstance(outcome, Exception):
                        print(f"[rules] {rule['name']!r} failed: {outcome}", flush=True)
        except Exception as e:
            print(f"[rules] scheduler failed: {e}", flush=True)
        await asyncio.sleep(30)


# ------------------------------------------------------------------- http ----

class NewRule(BaseModel):
    name: str = PField(min_length=1, max_length=80)
    trigger: str
    config: dict = PField(default_factory=dict)
    action: dict = PField(default_factory=dict)
    enabled: bool = True


class EditRule(BaseModel):
    name: str | None = PField(None, min_length=1, max_length=80)
    config: dict | None = None
    action: dict | None = None
    enabled: bool | None = None


def _clean(trigger_name: str, config: dict) -> dict:
    trigger = TRIGGERS.get(trigger_name)
    if not trigger:
        known = ", ".join(TRIGGERS) or "none"
        raise HTTPException(400, f"unknown trigger {trigger_name!r}; known: {known}")
    spec = registry.Type(kind="rule", name=trigger_name, label=trigger.label,
                         fields=trigger.fields)
    return registry.validate(spec, config or {})


def _clean_action(action: dict) -> dict:
    merged = {**DEFAULT_ACTION, **(action or {})}
    merged["deliver"] = [d for d in merged.get("deliver") or [] if d in DELIVERIES]
    if not merged["deliver"] and not merged.get("briefing"):
        raise HTTPException(400, "an automation has to deliver somewhere: pick at "
                                 "least one of chat, push or webhook")
    merged["message"] = str(merged.get("message") or "")[:2000]
    merged["briefing"] = str(merged.get("briefing") or "")[:80]
    return merged


@router.get("/triggers")
async def get_triggers(_: dict = Depends(auth.require_role("creator", "admin"))):
    return {"triggers": catalogue()}


@router.get("")
async def get_all(_: dict = Depends(auth.require_role("creator", "admin"))):
    return {"rules": await listing(), "triggers": catalogue()}


@router.post("")
async def create(body: NewRule,
                 user: dict = Depends(auth.require_role("creator", "admin"))):
    config = _clean(body.trigger, body.config)
    action = _clean_action(body.action)
    try:
        async with await _connect() as conn:
            cur = await conn.execute(
                "INSERT INTO rules (name, trigger, enabled, config, action) "
                f"VALUES (%s, %s, %s, %s, %s) RETURNING {_COLUMNS}",
                (body.name.strip(), body.trigger, body.enabled, json.dumps(config),
                 json.dumps(action)))
            row = await cur.fetchone()
    except psycopg.errors.UniqueViolation:
        raise HTTPException(409, f"an automation called {body.name!r} already exists")
    await activity.record("rule.add", body.name, user["username"])
    return _row(row)


@router.patch("/{ident}")
async def update(ident: str, body: EditRule,
                 user: dict = Depends(auth.require_role("creator", "admin"))):
    current = await get(ident)
    config = (current["config"] if body.config is None
              else _clean(current["trigger"], body.config))
    action = (current["action"] if body.action is None
              else _clean_action({**current["action"], **body.action}))
    async with await _connect() as conn:
        await conn.execute(
            "UPDATE rules SET name = %s, enabled = %s, config = %s, action = %s "
            "WHERE id = %s",
            (body.name.strip() if body.name else current["name"],
             current["enabled"] if body.enabled is None else body.enabled,
             json.dumps(config), json.dumps(action), current["id"]))
    await activity.record("rule.edit", current["name"], user["username"])
    return await get(current["id"])


@router.delete("/{ident}")
async def remove(ident: str,
                 user: dict = Depends(auth.require_role("creator", "admin"))):
    current = await get(ident)
    async with await _connect() as conn:
        await conn.execute("DELETE FROM rules WHERE id = %s", (current["id"],))
    _last_checked.pop(current["id"], None)
    await activity.record("rule.remove", current["name"], user["username"])
    return {"ok": True}


@router.post("/{ident}/test")
async def test(ident: str, _: dict = Depends(auth.require_role("creator", "admin"))):
    """Run the check once and report what it would have said.

    Nothing is delivered and no dedupe key is recorded, so testing a rule cannot
    consume the notification it was about to send for real.
    """
    rule = await get(ident)
    trigger = TRIGGERS.get(rule["trigger"])
    if not trigger:
        raise HTTPException(400, f"unknown trigger {rule['trigger']!r}")
    try:
        found = await trigger.check(rule)
    except Exception as e:
        return {"fired": False, "error": f"{type(e).__name__}: {e}"}
    if not found:
        return {"fired": False, "detail": "Nothing to report right now."}
    already = found.key in (rule["state"].get("seen") or [])
    return {"fired": True, "facts": found.facts, "key": found.key,
            "already_sent": already,
            "text": await _wording(rule, found.facts)}
