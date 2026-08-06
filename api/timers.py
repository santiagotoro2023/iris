"""Timers and alarms (SPEC.md 55).

In Postgres rather than Redis, because an alarm that does not survive a restart is not
an alarm. The proactive engine and ntfy already exist, so "remind me in 20 minutes" is
a small thing sitting on top of both: it rings in the browser and it arrives on the
phone, which between them covers the two places a person actually is.

The scheduler ticks every five seconds. A minute-resolution loop would be simpler and
would also mean a twenty minute timer goes off at twenty minutes and fifty seconds,
which is a broken timer.
"""
import asyncio
import json
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field as PField

import activity
import auth
import settings

DATABASE_URL = os.environ.get("DATABASE_URL", "")
router = APIRouter(prefix="/timers", tags=["timers"])

REPEATS = ["", "daily", "weekdays", "weekly"]
MAX_DURATION_SECONDS = 30 * 86400

settings.setting(
    "timers.snooze_minutes", type="integer", minimum=1, maximum=120, default=9,
    title="Snooze for (minutes)", order=10,
    description="How long the snooze button on a ringing alarm puts it off for.")
settings.setting(
    "timers.push", type="boolean", default=True,
    title="Send timers to your phone", order=11,
    description="Uses whatever push integration is set up. The browser rings anyway "
                "when the page is open.")


def _now() -> datetime:
    return datetime.now(ZoneInfo(settings.get("general.timezone")))


# ------------------------------------------------------------------ parsing ----

# "1h30m", "90s", "2 hours 15 minutes", "an hour and a half". Written out rather than
# pulled from a library because the whole grammar is three units.
_UNITS = {"s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
          "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
          "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
          "d": 86400, "day": 86400, "days": 86400}
_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
          "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "twelve": 12,
          "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40, "forty-five": 45,
          "fortyfive": 45, "sixty": 60, "half": 0}
_CHUNK = re.compile(r"([\d.]+|[a-z-]+)\s*([a-z]+)?")
# "an hour and a half" is a trailing modifier on the quantity before it, which the
# left-to-right scan below cannot see. Normalised to "1.5 hours" first. "half an
# hour" needs none of this, because there the half comes first.
_AND_A_HALF = re.compile(
    r"\b(an?|one|\d+(?:\.\d+)?)\s+(second|minute|hour|day)s?\s+and\s+a\s+half\b")


def _fold_halves(text: str) -> str:
    def whole(m: re.Match) -> str:
        base = 1.0 if m.group(1) in ("a", "an", "one") else float(m.group(1))
        return f"{base + 0.5} {m.group(2)}s"
    return _AND_A_HALF.sub(whole, text)


def parse_duration(text: str) -> int:
    """Seconds, or 0 when nothing in it was a duration.

    "half an hour" is handled by looking at the unit that follows rather than by
    special-casing the phrase, so "half a day" works for free.
    """
    lowered = _fold_halves((text or "").strip().lower()).replace("and", " ")
    total = 0
    pending: float | None = None
    half = False
    for value, unit in _CHUNK.findall(lowered):
        if value == "half":
            half = True
            continue
        if unit in _UNITS or value in _UNITS:
            seconds = _UNITS.get(unit) or _UNITS[value]
            if unit in _UNITS:
                try:
                    count = float(value)
                except ValueError:
                    count = _WORDS.get(value, 0)
            else:
                count = pending if pending is not None else 1
            if half:
                count = (count or 1) * 0.5
                half = False
            total += int(count * seconds)
            pending = None
            continue
        try:
            pending = float(value)
        except ValueError:
            pending = _WORDS.get(value)
    return min(total, MAX_DURATION_SECONDS)


_MERIDIEM = re.compile(r"\b(\d{1,2})\s*(am|pm)\b")


def parse_time(text: str) -> datetime | None:
    """When an alarm should go off.

    `places.parse_when` already turns "tomorrow 08:30" into a date and a time for the
    transit tool, so it is reused rather than written twice. It does not understand
    "7am" though, because a timetable is a 24-hour thing, so that is normalised here
    before handing it over.
    """
    import places
    lowered = (text or "").strip().lower()
    lowered = _MERIDIEM.sub(
        lambda m: f"{(int(m.group(1)) % 12) + (12 if m.group(2) == 'pm' else 0):02d}:00",
        lowered)
    date, clock = places.parse_when(lowered)
    if not clock:
        return None
    tz = ZoneInfo(settings.get("general.timezone"))
    today = _now()
    day = (datetime.strptime(date, "%Y-%m-%d").date() if date else today.date())
    hour, minute = (int(p) for p in clock.split(":"))
    when = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
    # No date said and the time has already gone means tomorrow. "Wake me at 6" at
    # eleven at night is not a request for an alarm four hours before it was set.
    if not date and when <= today:
        when += timedelta(days=1)
    return when


def next_occurrence(when: datetime, repeat: str) -> datetime | None:
    if repeat == "daily":
        return when + timedelta(days=1)
    if repeat == "weekly":
        return when + timedelta(days=7)
    if repeat == "weekdays":
        following = when + timedelta(days=1)
        while following.weekday() >= 5:
            following += timedelta(days=1)
        return following
    return None


def in_words(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds} second{'' if seconds == 1 else 's'}"
    minutes, hours = seconds // 60, seconds // 3600
    if seconds < 3600:
        return f"{minutes} minute{'' if minutes == 1 else 's'}"
    rest = minutes % 60
    return (f"{hours} hour{'' if hours == 1 else 's'}"
            + (f" {rest} minute{'' if rest == 1 else 's'}" if rest else ""))


# ------------------------------------------------------------------ storage ----

async def _connect():
    return await psycopg.AsyncConnection.connect(DATABASE_URL, autocommit=True)


_COLUMNS = "id, user_id, label, kind, due, repeat, fired, created"


def _row(row: tuple) -> dict:
    return {"id": row[0], "user_id": row[1], "label": row[2], "kind": row[3],
            "due": row[4].timestamp(), "due_text": row[4].astimezone(
                ZoneInfo(settings.get("general.timezone"))).strftime("%H:%M on %a %d %b"),
            "repeat": row[5], "fired": row[6],
            "created": row[7].timestamp() if row[7] else 0}


async def init() -> None:
    async with await _connect() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS timers (
                id      BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                label   TEXT NOT NULL DEFAULT '',
                kind    TEXT NOT NULL,
                due     TIMESTAMPTZ NOT NULL,
                repeat  TEXT NOT NULL DEFAULT '',
                fired   BOOLEAN NOT NULL DEFAULT FALSE,
                created TIMESTAMPTZ NOT NULL DEFAULT now()
            )""")
        await conn.execute("CREATE INDEX IF NOT EXISTS timers_due "
                           "ON timers (fired, due)")


async def _insert(user_id: int, kind: str, label: str, due: datetime,
                  repeat: str = "") -> dict:
    async with await _connect() as conn:
        cur = await conn.execute(
            "INSERT INTO timers (user_id, label, kind, due, repeat) "
            f"VALUES (%s, %s, %s, %s, %s) RETURNING {_COLUMNS}",
            (user_id, label.strip(), kind, due, repeat))
        return _row(await cur.fetchone())


async def upcoming(user_id: int | None = None) -> list[dict]:
    async with await _connect() as conn:
        if user_id is None:
            cur = await conn.execute(
                f"SELECT {_COLUMNS} FROM timers WHERE NOT fired ORDER BY due")
        else:
            cur = await conn.execute(
                f"SELECT {_COLUMNS} FROM timers WHERE NOT fired AND user_id = %s "
                "ORDER BY due", (user_id,))
        return [_row(r) for r in await cur.fetchall()]


async def cancel(ident, user_id: int | None = None) -> dict | None:
    """By id, or by what it was called. The model cancels "the oven timer", not 47."""
    rows = await upcoming(user_id)
    wanted = str(ident).strip().lower()
    found = next((r for r in rows if str(r["id"]) == wanted), None)
    if not found:
        found = next((r for r in rows if r["label"].lower() == wanted), None)
    if not found:
        found = next((r for r in rows if wanted and wanted in r["label"].lower()), None)
    if not found:
        return None
    async with await _connect() as conn:
        await conn.execute("DELETE FROM timers WHERE id = %s", (found["id"],))
    return found


async def snooze(ident: int, minutes: int | None = None) -> dict:
    minutes = minutes or settings.get("timers.snooze_minutes")
    async with await _connect() as conn:
        cur = await conn.execute(
            "UPDATE timers SET due = now() + %s * interval '1 minute', fired = FALSE "
            f"WHERE id = %s RETURNING {_COLUMNS}", (minutes, ident))
        row = await cur.fetchone()
    if not row:
        raise HTTPException(404, f"no timer {ident}")
    return _row(row)


# --------------------------------------------------------------- what tools say ----

async def set_timer_text(duration: str, label: str, user_id: int | None) -> str:
    if user_id is None:
        return "error: no user in context"
    seconds = parse_duration(duration)
    if not seconds:
        return (f"I could not read {duration!r} as a length of time. Try something "
                f"like '20 minutes' or '1h30m'.")
    row = await _insert(user_id, "timer", label, _now() + timedelta(seconds=seconds))
    return (f"Timer set for {in_words(seconds)}"
            + (f" ({row['label']})" if row["label"] else "")
            + f", going off at {row['due_text']}.")


async def set_alarm_text(when: str, label: str, repeat: str,
                         user_id: int | None) -> str:
    if user_id is None:
        return "error: no user in context"
    if repeat not in REPEATS:
        return f"repeat must be one of: {', '.join(r or 'once' for r in REPEATS)}."
    due = parse_time(when)
    if not due:
        return (f"I could not read {when!r} as a time. Try something like '06:30' or "
                f"'tomorrow 7am'.")
    row = await _insert(user_id, "alarm", label, due, repeat)
    return (f"Alarm set for {row['due_text']}"
            + (f" ({row['label']})" if row["label"] else "")
            + (f", repeating {repeat}." if repeat else "."))


async def upcoming_text(user_id: int | None) -> str:
    rows = await upcoming(user_id)
    if not rows:
        return "Nothing set."
    now = _now().timestamp()
    lines = []
    for r in rows:
        left = in_words(r["due"] - now)
        name = r["label"] or r["kind"]
        lines.append(f"- {name}: {r['due_text']}, {left} from now"
                     + (f", repeating {r['repeat']}" if r["repeat"] else ""))
    return "\n".join(lines)


async def cancel_text(ident: str, user_id: int | None) -> str:
    found = await cancel(ident, user_id)
    if not found:
        return f"Nothing set matching {ident!r}."
    return f"Cancelled {found['label'] or found['kind']}, which was due at " \
           f"{found['due_text']}."


# ---------------------------------------------------------------- ringing ----

# Anything with an open page gets an event. A set rather than a list so a browser that
# reconnects does not leave a dead queue filling up behind it.
_listeners: set[asyncio.Queue] = set()


async def _ring(row: dict) -> None:
    import integrations
    for queue in list(_listeners):
        try:
            queue.put_nowait(row)
        except asyncio.QueueFull:
            # A page that has not read its queue in a hundred events is not a page
            # anybody is looking at. Dropping the event beats blocking the scheduler.
            print("[timers] a listener queue was full, dropping the event", flush=True)
    if settings.get("timers.push"):
        what = row["label"] or ("Alarm" if row["kind"] == "alarm" else "Timer")
        try:
            await integrations.notify(f"{what} - {row['due_text']}", event="timer")
        except Exception as e:
            print(f"[timers] push failed: {e}", flush=True)


async def scheduler() -> None:
    await asyncio.sleep(7)
    while True:
        try:
            async with await _connect() as conn:
                cur = await conn.execute(
                    f"UPDATE timers SET fired = TRUE WHERE NOT fired AND due <= now() "
                    f"RETURNING {_COLUMNS}")
                due = [_row(r) for r in await cur.fetchall()]
            for row in due:
                await _ring(row)
                following = next_occurrence(
                    datetime.fromtimestamp(row["due"],
                                           ZoneInfo(settings.get("general.timezone"))),
                    row["repeat"])
                if following:
                    await _insert(row["user_id"], row["kind"], row["label"],
                                  following, row["repeat"])
                await activity.record(
                    "timer.fired", f"{row['label'] or row['kind']} at "
                                   f"{row['due_text']}", "iris")
        except Exception as e:
            print(f"[timers] scheduler failed: {e}", flush=True)
        await asyncio.sleep(5)


# ------------------------------------------------------------------- http ----

class NewTimer(BaseModel):
    kind: str = PField("timer", pattern="^(timer|alarm)$")
    label: str = PField("", max_length=80)
    duration: str = ""
    when: str = ""
    repeat: str = ""


class Snooze(BaseModel):
    minutes: int | None = PField(None, ge=1, le=1440)


@router.get("")
async def get_all(user: dict = Depends(auth.active_user)):
    return {"timers": await upcoming(user["id"]),
            "snooze_minutes": settings.get("timers.snooze_minutes")}


@router.post("")
async def create(body: NewTimer, user: dict = Depends(auth.active_user)):
    if body.kind == "timer":
        seconds = parse_duration(body.duration)
        if not seconds:
            raise HTTPException(400, f"could not read {body.duration!r} as a length "
                                     f"of time")
        row = await _insert(user["id"], "timer", body.label,
                            _now() + timedelta(seconds=seconds))
    else:
        due = parse_time(body.when)
        if not due:
            raise HTTPException(400, f"could not read {body.when!r} as a time")
        if body.repeat not in REPEATS:
            raise HTTPException(400, f"repeat must be one of: "
                                     f"{', '.join(r or 'once' for r in REPEATS)}")
        row = await _insert(user["id"], "alarm", body.label, due, body.repeat)
    await activity.record(f"{row['kind']}.set",
                          f"{row['label'] or row['kind']} at {row['due_text']}",
                          user["username"])
    return row


@router.delete("/{ident}")
async def remove(ident: int, user: dict = Depends(auth.active_user)):
    found = await cancel(ident, user["id"])
    if not found:
        raise HTTPException(404, f"no timer {ident}")
    await activity.record("timer.cancel", found["label"] or found["kind"],
                          user["username"])
    return {"ok": True}


@router.post("/{ident}/snooze")
async def snooze_one(ident: int, body: Snooze,
                     user: dict = Depends(auth.active_user)):
    return await snooze(ident, body.minutes)


@router.get("/stream")
async def stream(_: dict = Depends(auth.active_user)):
    """One SSE event per firing, so an open page rings without polling."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    _listeners.add(queue)

    async def events():
        try:
            while True:
                try:
                    row = await asyncio.wait_for(queue.get(), timeout=25)
                    yield f"data: {json.dumps(row)}\n\n"
                except asyncio.TimeoutError:
                    # Proxies close a silent stream. A comment is not an event, so the
                    # client never sees it, but the connection stays up.
                    yield ": keepalive\n\n"
        finally:
            _listeners.discard(queue)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"cache-control": "no-cache",
                                      "x-accel-buffering": "no"})


if __name__ == "__main__":
    # Duration parsing is the part a person will hit wrong wording on first.
    assert parse_duration("20 minutes") == 1200
    assert parse_duration("1h30m") == 5400
    assert parse_duration("90s") == 90
    assert parse_duration("2 hours 15 minutes") == 8100
    assert parse_duration("an hour and a half") == 5400, parse_duration(
        "an hour and a half")
    assert parse_duration("half an hour") == 1800, parse_duration("half an hour")
    assert parse_duration("three days") == 259200
    assert parse_duration("soon") == 0
    assert parse_duration("") == 0
    assert in_words(90) == "1 minute"
    assert in_words(3600) == "1 hour"
    assert in_words(5400) == "1 hour 30 minutes"
    assert in_words(45) == "45 seconds"
    print("timers self-check passed")
