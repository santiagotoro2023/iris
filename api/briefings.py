"""Briefings, built from widgets (SPEC.md 34, extended in 55).

The daily briefing started as one hard-coded list of sections behind five boolean
settings. That was wrong in three ways at once: there could only ever be one of them,
the order was whatever `gather()` happened to do, and "include the weather" was the
only thing that could be said about the weather.

A briefing is now a **named, ordered list of widgets**, each with its own settings and
its own sentence budget, and there can be as many briefings as there are reasons to
want one. Santiago asked for exactly this: a Default Briefing at 07:00, others he can
name and schedule differently, and per-widget control over what each one says.

Two things are deliberate and load-bearing:

**The facts are gathered by calling the tools, and only the wording is left to the
model.** Asking an 8B model to "go and check everything" means it sometimes decides it
already knows, and a briefing that quietly invents your morning is worse than none.

**The prompt is built section by section with an explicit sentence budget per
section.** A flat wall of notes made the model drop the weather entirely on its way to
the news, and telling it to "cover weather, appointments, mail and the journey" made it
invent an appointment with the local council and a visa interview. Sections carry their
own instructions, and the rule is stated in both directions: a section that is here
must not be dropped, a subject that is not here must not be mentioned.
"""
import asyncio
import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

import psycopg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field as PField

import activity
import auth
import registry
import settings

DATABASE_URL = os.environ.get("DATABASE_URL", "")
router = APIRouter(prefix="/briefings", tags=["proactive"])

HOURS = [f"{h:02d}:00" for h in range(24)]
RECURRENCES = ["daily", "weekdays", "weekly", "monthly", "never"]
DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday",
             "Sunday"]


# ---------------------------------------------------------------- widgets ----

@dataclass
class Gathered:
    """What a widget found. `notes` empty drops the section entirely, which is the
    right answer for "no mailbox is configured": a briefing should not apologise for
    a feature that was never switched on."""
    notes: str = ""
    sources: list = field(default_factory=list)


@dataclass
class Widget:
    """Declared in code, rendered by the client from `fields`, exactly the bargain
    `registry.Type` makes for devices. Adding a widget is one `register(...)` call and
    no client work at all."""
    name: str
    label: str
    description: str
    gather: Callable
    fields: list = field(default_factory=list)
    default_sentences: int = 2
    # Settings key that must be truthy for this widget to be offered at all.
    needs: str = ""
    # A widget with nothing to say in sentences: the greeting is an instruction, not
    # a paragraph, and giving it a budget invites the model to pad it.
    wordless: bool = False

    def as_json(self) -> dict:
        return {"name": self.name, "label": self.label,
                "description": self.description,
                "default_sentences": self.default_sentences,
                "wordless": self.wordless,
                "fields": [f.as_json() for f in self.fields]}


WIDGETS: dict[str, Widget] = {}


def register(widget: Widget) -> Widget:
    WIDGETS[widget.name] = widget
    return widget


def catalogue() -> list[dict]:
    """Only widgets whose feature is switched on. Offering a commute widget on a
    machine with transit disabled is offering something that can only disappoint."""
    return [w.as_json() for w in WIDGETS.values()
            if not w.needs or settings.get(w.needs)]


def _cfg(config: dict, name: str, default=""):
    value = config.get(name, default)
    return value if value not in (None, "") else default


# -- greeting ----------------------------------------------------------------

async def _gather_greeting(config: dict) -> Gathered:
    import proactive
    return Gathered(notes=f"Greet them with exactly: {proactive.greeting(_now())}")


register(Widget(
    name="greeting", label="Greeting",
    description="Opens the briefing with the right greeting for the hour. Worked out "
                "here, not by the model, which opened with 'Good morning' at 22:18.",
    gather=_gather_greeting, default_sentences=0, wordless=True))


# -- weather -----------------------------------------------------------------

async def _gather_weather(config: dict) -> Gathered:
    import places
    where = _cfg(config, "place", "Home")
    if where == "Home":
        place = settings.get("location.home")
    elif where == "Work":
        place = settings.get("location.work")
    elif where == "Somewhere else":
        place = _cfg(config, "custom")
    else:
        place = ""          # "Where I am": the live fix beats any stored label
    if where in ("Home", "Work") and not place:
        return Gathered(notes=f"{where} is not set under Settings, so its forecast "
                              f"could not be looked up.")
    text = await places.weather(place)
    if _cfg(config, "days", "Today and tomorrow") == "Today":
        text = "\n".join(l for l in text.splitlines() if not l.startswith("- tomorrow"))
    return Gathered(notes=text)


register(Widget(
    name="weather", label="Weather",
    description="The forecast, for wherever you choose: home, work, where you are "
                "standing, or a place you name.",
    gather=_gather_weather, default_sentences=2, needs="location.enabled",
    fields=[
        registry.Field("place", "Where", type="choice", default="Home",
                       choices=["Home", "Work", "Where I am", "Somewhere else"],
                       help="Home and Work come from Settings. 'Where I am' uses the "
                            "live position, which is right when you travel."),
        registry.Field("custom", "That place", help="Used only for 'Somewhere else'."),
        registry.Field("days", "How far ahead", type="choice",
                       default="Today and tomorrow",
                       choices=["Today", "Today and tomorrow"]),
    ]))


# -- news --------------------------------------------------------------------

# Santiago asked to be able to pick who reports the news. A name is only useful if it
# maps to something a search can be restricted to, so each carries its domain.
SOURCES: dict[str, str] = {
    "SRF": "srf.ch", "20 Minuten": "20min.ch", "Blick": "blick.ch", "NZZ": "nzz.ch",
    "Tages-Anzeiger": "tagesanzeiger.ch", "Watson": "watson.ch",
    "SwissInfo": "swissinfo.ch", "RTS": "rts.ch", "Le Temps": "letemps.ch",
    "Luzerner Zeitung": "luzernerzeitung.ch",
    "Reuters": "reuters.com", "BBC": "bbc.com", "AP": "apnews.com",
    "The Guardian": "theguardian.com", "Al Jazeera": "aljazeera.com", "DW": "dw.com",
    "France 24": "france24.com", "NYT": "nytimes.com", "Financial Times": "ft.com",
    "Bloomberg": "bloomberg.com",
}
# A one-day window starves the engines before the age filter ever runs: "Switzerland
# news" over a day returned four results, none of them carrying a date. The window is
# wide and the age filter is what actually decides freshness.
NEWS_WINDOW = "week"
NEWS_MAX_AGE_DAYS = 2


async def _gather_news(config: dict) -> Gathered:
    import reasoning
    scope = _cfg(config, "scope", "World")
    region = settings.get("location.region") or "Switzerland"
    if scope == "World":
        label, queries = "the world", ["world news", "international news today"]
    elif scope == "A topic":
        topic = _cfg(config, "topic")
        if not topic:
            return Gathered(notes="A news topic was not filled in, so nothing was "
                                  "looked up for it.")
        label, queries = topic, [topic, f"{topic} news"]
    else:
        label, queries = region, [region, f"{region} news"]

    chosen = [s.strip() for s in _cfg(config, "sources").split(",") if s.strip()]
    domains = [SOURCES.get(s, s) for s in chosen]
    wanted = int(_cfg(config, "stories", 4) or 4)

    async def run(query: str) -> list[dict]:
        try:
            found = await asyncio.to_thread(reasoning.hits, query, "news", 10,
                                            NEWS_WINDOW, NEWS_MAX_AGE_DAYS)
        except Exception as e:
            print(f"[briefings] news search {query!r} failed: {e}", flush=True)
            return []
        return [] if isinstance(found, str) else found

    # Every chosen source, and the open search as well. Restricting to a domain that
    # published nothing today must not empty the section: the open results are the
    # floor, the chosen ones are the preference.
    plans = [f"site:{d} {q}" for d in domains for q in queries[:1]] + queries
    batches = await asyncio.gather(*(run(q) for q in plans))

    seen, merged = set(), []
    for batch in batches:
        for item in batch:
            url = item.get("url") or ""
            if url and url not in seen:
                seen.add(url)
                item["_preferred"] = any(d in url for d in domains)
                merged.append(item)
    if not merged:
        return Gathered()
    # Preferred domains first, then newest. Sorting by date alone would let an open
    # result outrank the outlet that was explicitly asked for.
    merged.sort(key=lambda i: (i.get("_preferred", False), i.get("_at", 0)),
                reverse=True)
    merged = merged[:wanted]

    # Title plus the story's first line. Titles alone were all the model had, so all
    # it could do was read them back, and a briefing that repeats a headline and stops
    # has told you nothing a ticker would not.
    stories = [f"{(i.get('title') or '').strip()}. "
               f"{' '.join((i.get('content') or '').split())[:320]}".strip()
               for i in merged]
    return Gathered(
        notes="\n".join(f"- {s}" for s in stories),
        sources=[{"role": "tool", "tool_name": "web_search",
                  "label": f"Headlines from {label}", "display": "sources",
                  "content": reasoning.format_hits(merged)}])


register(Widget(
    name="news", label="News",
    description="Headlines with enough of the story to say what actually happened. "
                "Add it twice for world news and news from where you live.",
    gather=_gather_news, default_sentences=4,
    fields=[
        registry.Field("scope", "What news", type="choice", default="World",
                       choices=["World", "My region", "A topic"],
                       help="'My region' uses the region set under Settings."),
        registry.Field("topic", "That topic",
                       help="Used only for 'A topic', e.g. 'semiconductors'."),
        registry.Field("sources", "Preferred sources", type="choice", free=True,
                       choices=list(SOURCES),
                       help="Comma separated. These are searched first; anything they "
                            "did not cover is still filled in from elsewhere, so a "
                            "quiet day at one outlet does not empty the section."),
        registry.Field("stories", "How many stories", type="number", default=4),
    ]))


# -- calendar ----------------------------------------------------------------

async def _gather_calendar(config: dict) -> Gathered:
    import integrations
    if not (await registry.enabled_of_type("integration", "calendar")
            + await registry.enabled_of_type("integration", "calendar_ics")):
        return Gathered()
    return Gathered(notes=await integrations.check_all_calendars())


register(Widget(
    name="calendar", label="Calendar",
    description="What is on. Needs a calendar under Integrations.",
    gather=_gather_calendar, default_sentences=2))


# -- mail --------------------------------------------------------------------

async def _gather_mail(config: dict) -> Gathered:
    import integrations
    if not await registry.enabled_of_type("integration", "mailbox"):
        return Gathered()
    return Gathered(notes=await integrations.check_all_mail())


register(Widget(
    name="mail", label="Mail",
    description="Who wrote and what about. Needs a mailbox under Integrations.",
    gather=_gather_mail, default_sentences=2))


# -- commute -----------------------------------------------------------------

async def _gather_commute(config: dict) -> Gathered:
    import places
    start = _cfg(config, "origin", "Home")
    end = _cfg(config, "destination", "Work")
    origin = settings.get("location.home") if start == "Home" else ""
    if end == "Work":
        destination = settings.get("location.work")
    elif end == "Home":
        destination = settings.get("location.home")
    else:
        destination = _cfg(config, "custom")
    if not destination:
        return Gathered()
    when = _cfg(config, "arrive_by") or None
    return Gathered(notes=await places.journey(origin, destination, when,
                                               arrive_by=bool(when)))


register(Widget(
    name="commute", label="Commute",
    description="The journey you actually make, with times. Leave the arrival time "
                "blank for the next departures.",
    gather=_gather_commute, default_sentences=1, needs="location.enabled",
    fields=[
        registry.Field("origin", "From", type="choice", default="Home",
                       choices=["Home", "Where I am"]),
        registry.Field("destination", "To", type="choice", default="Work",
                       choices=["Work", "Home", "Somewhere else"]),
        registry.Field("custom", "That place",
                       help="Used only for 'Somewhere else'."),
        registry.Field("arrive_by", "Be there by",
                       help="e.g. 08:30. Blank gives the next departures instead."),
    ]))


# -- system ------------------------------------------------------------------

async def _gather_system(config: dict) -> Gathered:
    import system
    return Gathered(notes=await system.describe())


register(Widget(
    name="system", label="This machine",
    description="Measured GPU, memory, disk and service health. Real numbers, not "
                "invented ones.",
    gather=_gather_system, default_sentences=1))


# -- timers ------------------------------------------------------------------

async def _gather_timers(config: dict) -> Gathered:
    import timers
    text = await timers.upcoming_text(None)
    return Gathered(notes="" if text.startswith("Nothing") else text)


register(Widget(
    name="timers", label="Timers and alarms",
    description="What is set to go off. Silent when nothing is.",
    gather=_gather_timers, default_sentences=1))


# -- camera events -----------------------------------------------------------

async def _gather_events(config: dict) -> Gathered:
    import events
    hours = float(_cfg(config, "hours", 12) or 12)
    text = await events.describe(hours, "", _cfg(config, "objects", "person"))
    # "No camera is set up yet" is a fact about the install, not about the night, and
    # it does not belong in a briefing that runs every morning.
    return Gathered(notes="" if text.startswith("No camera is set up") else text)


register(Widget(
    name="camera_events", label="What the cameras saw",
    description="Overnight detections. Silent until a camera is added.",
    gather=_gather_events, default_sentences=1,
    fields=[
        registry.Field("hours", "How far back (hours)", type="number", default=12),
        registry.Field("objects", "What counts", default="person",
                       help="Comma separated, e.g. person,car. Blank means anything."),
    ]))


# -- note --------------------------------------------------------------------

async def _gather_note(config: dict) -> Gathered:
    text = _cfg(config, "text")
    return Gathered(notes=f"Standing note, include it as written: {text}"
                    if text else "")


register(Widget(
    name="note", label="Standing note",
    description="Something you want said every time, in your own words.",
    gather=_gather_note, default_sentences=1,
    fields=[registry.Field("text", "What to say", type="text",
                           help="e.g. 'the bins go out on Tuesday night'.")]))


# ---------------------------------------------------------------- storage ----

DEFAULT_SCHEDULE = {"enabled": True, "at": "07:00", "recurrence": "daily",
                    "days": [0, 1, 2, 3, 4], "day_of_month": 1}
DEFAULT_OPTIONS = {"greeting": True, "prose": True, "max_sentences": 0}


async def _connect():
    return await psycopg.AsyncConnection.connect(DATABASE_URL, autocommit=True)


def _now() -> datetime:
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo(settings.get("general.timezone")))


def _row(row: tuple) -> dict:
    return {"id": row[0], "name": row[1], "enabled": row[2], "is_default": row[3],
            "schedule": {**DEFAULT_SCHEDULE, **(row[4] or {})},
            "options": {**DEFAULT_OPTIONS, **(row[5] or {})},
            "widgets": row[6] or [],
            "created": row[7].timestamp() if row[7] else 0,
            "last_run": row[8].timestamp() if row[8] else 0}


_COLUMNS = ("id, name, enabled, is_default, schedule, options, widgets, created, "
            "last_run")


async def init() -> None:
    async with await _connect() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS briefings (
                id         BIGSERIAL PRIMARY KEY,
                name       TEXT NOT NULL UNIQUE,
                enabled    BOOLEAN NOT NULL DEFAULT TRUE,
                is_default BOOLEAN NOT NULL DEFAULT FALSE,
                schedule   JSONB NOT NULL DEFAULT '{}',
                options    JSONB NOT NULL DEFAULT '{}',
                widgets    JSONB NOT NULL DEFAULT '[]',
                created    TIMESTAMPTZ NOT NULL DEFAULT now(),
                last_run   TIMESTAMPTZ
            )""")
    await seed()


async def listing() -> list[dict]:
    async with await _connect() as conn:
        cur = await conn.execute(
            f"SELECT {_COLUMNS} FROM briefings ORDER BY is_default DESC, name")
        return [_row(r) for r in await cur.fetchall()]


async def get(ident) -> dict:
    async with await _connect() as conn:
        cur = await conn.execute(
            f"SELECT {_COLUMNS} FROM briefings WHERE id::text = %s", (str(ident),))
        row = await cur.fetchone()
    if not row:
        raise HTTPException(404, f"no briefing {ident!r}")
    return _row(row)


async def by_name(name: str) -> dict | None:
    """Exact, then case-insensitive, then substring. The model quotes a name back
    approximately, and refusing "the morning briefing" because the row says "Morning
    Briefing" would be a pedantry nobody asked for."""
    rows = await listing()
    if not rows:
        return None
    wanted = (name or "").strip().lower()
    if not wanted:
        return await default_briefing()
    for row in rows:
        if row["name"].lower() == wanted:
            return row
    for row in rows:
        if wanted in row["name"].lower() or row["name"].lower() in wanted:
            return row
    return None


async def default_briefing() -> dict | None:
    rows = await listing()
    return next((r for r in rows if r["is_default"]), rows[0] if rows else None)


def _default_widgets() -> list[dict]:
    """What the first briefing contains. Built from the old per-section booleans if
    they are still stored, so an existing install keeps the briefing it had."""
    def on(key: str, fallback: bool = True) -> bool:
        try:
            return bool(settings.get(key))
        except Exception:
            return fallback

    wanted = [("greeting", {}, True)]
    if on("proactive.weather") and on("location.enabled"):
        wanted.append(("weather", {"place": "Home", "days": "Today and tomorrow"},
                       True))
    if on("proactive.calendar"):
        wanted.append(("calendar", {}, True))
    if on("proactive.include_mail"):
        wanted.append(("mail", {}, True))
    if on("proactive.commute") and on("location.enabled"):
        wanted.append(("commute", {"origin": "Home", "destination": "Work"}, True))
    if on("proactive.news"):
        wanted.append(("news", {"scope": "World", "stories": 4}, True))
        wanted.append(("news", {"scope": "My region", "stories": 4}, True))
    return [{"id": f"w{i}", "type": kind, "config": config,
             "sentences": WIDGETS[kind].default_sentences}
            for i, (kind, config, keep) in enumerate(wanted, 1) if keep]


async def seed() -> None:
    async with await _connect() as conn:
        cur = await conn.execute("SELECT count(*) FROM briefings")
        if (await cur.fetchone())[0]:
            return
        at = "07:00"
        try:
            at = settings.get("proactive.briefing_at") or at
        except Exception:
            pass
        await conn.execute(
            "INSERT INTO briefings (name, is_default, schedule, options, widgets) "
            "VALUES (%s, TRUE, %s, %s, %s)",
            ("Default Briefing",
             json.dumps({**DEFAULT_SCHEDULE, "at": at, "recurrence": "daily"}),
             json.dumps(DEFAULT_OPTIONS),
             json.dumps(_default_widgets())))
    print("[briefings] seeded the Default Briefing", flush=True)


# ---------------------------------------------------------------- compose ----

RULES = """\
Write the briefing from the sections below.

Cover every section that is here, in the order given, and nothing else. A section
that is here must not be dropped. A subject that is NOT here must not be mentioned at
all: no appointments if there is no calendar section, no mail if there is no mail
section, no trains if there is no journey section. Inventing one is worse than a
short briefing.

Each section says how many sentences it gets. Respect that. It is a budget, not a
target: nothing to report means one short sentence, not padding to fill the count.

Use ONLY what the sections contain. Do not apologise for what is missing, do not
state the date or the time back to them unless something depends on it, and do not
include links, which are attached separately.

No headers, no bullet points, no sign-off, no offer of further help.
"""

NEWS_RULE = ("Say what happened and why it matters, in your own words, using the "
             "detail after each headline. Never repeat a headline verbatim.")


async def gather(briefing: dict) -> tuple[list[str], list[dict]]:
    """Run every widget, in order, and keep what each one found.

    Widgets run concurrently because they are all network-bound and independent: a
    briefing that took the sum of a weather lookup, four searches, a CalDAV report and
    an IMAP login would take most of a minute. Order is restored afterwards, because
    order is the thing the user arranged.
    """
    chosen = [w for w in briefing.get("widgets") or []
              if w.get("type") in WIDGETS]

    async def one(entry: dict) -> Gathered:
        widget = WIDGETS[entry["type"]]
        try:
            return await widget.gather(entry.get("config") or {})
        except Exception as e:
            # Said out loud in the log, dropped from the briefing. A section that
            # failed is not a section that is empty, and the difference matters when
            # the mailbox has been refusing logins for a week.
            print(f"[briefings] widget {entry['type']} failed: {e}", flush=True)
            return Gathered()

    results = await asyncio.gather(*(one(w) for w in chosen))

    sections: list[str] = []
    sources: list[dict] = []
    number = 0
    for entry, found in zip(chosen, results):
        if not found.notes.strip():
            continue
        widget = WIDGETS[entry["type"]]
        sources.extend(found.sources)
        if widget.wordless:
            sections.append(found.notes)
            continue
        number += 1
        budget = int(entry.get("sentences") or widget.default_sentences) or 1
        extra = f" {NEWS_RULE}" if widget.name == "news" else ""
        sections.append(
            f"SECTION {number} - {widget.label} "
            f"(write {budget} sentence{'' if budget == 1 else 's'}).{extra}\n"
            f"{found.notes}")
    return sections, sources


async def compose(briefing: dict) -> tuple[str, list[dict]]:
    import memory
    import proactive
    import reasoning
    sections, sources = await gather(briefing)
    options = briefing.get("options") or {}
    if not any("SECTION" in s for s in sections):
        return f"{proactive.greeting(_now())} Nothing to report.", sources

    rules = RULES
    if options.get("greeting", True):
        rules += ("\nOpen with the exact greeting the sections give you, once, and "
                  "never a different one.\n")
    cap = int(options.get("max_sentences") or 0)
    if cap:
        rules += f"\nThe whole briefing must not exceed {cap} sentences.\n"
    prompt = rules + "\n" + "\n\n".join(sections)
    try:
        text = await memory._complete(prompt, "")
    except Exception as e:
        print(f"[briefings] wording failed, sending the notes: {e}", flush=True)
        return "\n\n".join(sections), sources
    clean = reasoning.strip_emoji(reasoning.strip_dashes(text.strip()))
    return clean or "\n\n".join(sections), sources


async def text_for(name: str = "") -> str:
    """What the `briefing` tool returns. Text only: the tool's own banner is where it
    appears, and the sources ride along on the conversation the scheduler delivers."""
    briefing = await by_name(name)
    if not briefing:
        have = ", ".join(b["name"] for b in await listing()) or "none"
        return (f"No briefing called {name!r}. Configured: {have}."
                if name else "No briefings are configured.")
    text, _ = await compose(briefing)
    return text


# -------------------------------------------------------------- scheduling ----

def due_now(briefing: dict, when: datetime) -> bool:
    """Whether this briefing should have gone out by now, today.

    Deliberately "by now" rather than "at exactly now": the loop ticks once a minute
    and a machine that was asleep at 07:00 must still brief when it wakes, which is
    the behaviour Santiago asked for. The period key below is what stops that turning
    into a second delivery.
    """
    schedule = briefing.get("schedule") or {}
    if not schedule.get("enabled", True):
        return False
    recurrence = schedule.get("recurrence", "daily")
    if recurrence == "never":
        return False
    if when.strftime("%H:%M") < (schedule.get("at") or "07:00"):
        return False
    weekday = when.weekday()
    if recurrence == "weekdays":
        return weekday < 5
    if recurrence == "weekly":
        return weekday in [int(d) for d in (schedule.get("days") or [])]
    if recurrence == "monthly":
        return when.day == int(schedule.get("day_of_month") or 1)
    return True


def period_key(briefing: dict, when: datetime) -> str:
    """One delivery per period, whatever the period is. ISO week for weekly, so the
    week that straddles New Year is one week and not two."""
    recurrence = (briefing.get("schedule") or {}).get("recurrence", "daily")
    if recurrence == "weekly":
        return when.strftime("%G-W%V")
    if recurrence == "monthly":
        return when.strftime("%Y%m")
    return when.strftime("%Y%m%d")


async def _already_ran(briefing_id: int, period: str) -> bool:
    import chat
    return bool(await chat._redis.get(f"proactive:briefed:{briefing_id}:{period}"))


async def _mark_ran(briefing_id: int, period: str) -> None:
    import chat
    # Held for 40 days: long enough that a monthly briefing cannot repeat, short
    # enough that these do not accumulate forever.
    await chat._redis.set(f"proactive:briefed:{briefing_id}:{period}", "1",
                          ex=40 * 86400)


async def run_and_deliver(briefing: dict, user: dict) -> dict:
    import proactive
    text, sources = await compose(briefing)
    result = await proactive.deliver(text, user["id"], user["username"],
                                     title=briefing["name"], sources=sources)
    async with await _connect() as conn:
        await conn.execute("UPDATE briefings SET last_run = now() WHERE id = %s",
                           (briefing["id"],))
    return result


async def scheduler() -> None:
    """One loop for every briefing. Marked as delivered BEFORE composing, because
    composing takes twenty seconds of searches and a crash halfway through must not
    produce two briefings on the next tick."""
    import proactive
    await asyncio.sleep(6)          # let Redis and Postgres come up
    while True:
        try:
            if settings.get("proactive.enabled"):
                now = _now()
                if not proactive.in_quiet_hours(now):
                    user = await proactive._first_user()
                    for briefing in await listing() if user else []:
                        if not briefing["enabled"] or not due_now(briefing, now):
                            continue
                        period = period_key(briefing, now)
                        if await _already_ran(briefing["id"], period):
                            continue
                        await _mark_ran(briefing["id"], period)
                        late = now.strftime("%H:%M") != briefing["schedule"]["at"]
                        await run_and_deliver(briefing, user)
                        print(f"[briefings] delivered {briefing['name']!r} to "
                              f"{user['username']}"
                              + (" (catching up on a missed one)" if late else ""),
                              flush=True)
        except Exception as e:
            print(f"[briefings] scheduler failed: {e}", flush=True)
        await asyncio.sleep(60)


# ------------------------------------------------------------------- http ----

class NewBriefing(BaseModel):
    name: str = PField(min_length=1, max_length=80)
    schedule: dict | None = None
    options: dict | None = None
    widgets: list[dict] | None = None


class EditBriefing(BaseModel):
    name: str | None = PField(None, min_length=1, max_length=80)
    enabled: bool | None = None
    is_default: bool | None = None
    schedule: dict | None = None
    options: dict | None = None
    widgets: list[dict] | None = None


def _clean_widgets(widgets: list[dict]) -> list[dict]:
    """Validate each widget's config against its declared fields, exactly as the
    device registry does, so a briefing cannot store a shape its own gatherer will
    trip over at seven in the morning."""
    out = []
    for i, entry in enumerate(widgets or [], 1):
        kind = entry.get("type")
        widget = WIDGETS.get(kind)
        if not widget:
            raise HTTPException(400, f"unknown widget {kind!r}")
        spec = registry.Type(kind="widget", name=kind, label=widget.label,
                             fields=widget.fields)
        sentences = entry.get("sentences", widget.default_sentences)
        try:
            sentences = max(0, min(8, int(sentences)))
        except (TypeError, ValueError):
            sentences = widget.default_sentences
        out.append({"id": str(entry.get("id") or f"w{i}"), "type": kind,
                    "sentences": sentences,
                    "config": registry.validate(spec, entry.get("config") or {})})
    return out


def _clean_schedule(schedule: dict) -> dict:
    merged = {**DEFAULT_SCHEDULE, **(schedule or {})}
    if merged["recurrence"] not in RECURRENCES:
        raise HTTPException(400, f"recurrence must be one of: {', '.join(RECURRENCES)}")
    if merged["at"] not in HOURS and not _looks_like_time(merged["at"]):
        raise HTTPException(400, "the time must look like 07:00")
    merged["days"] = sorted({int(d) for d in (merged.get("days") or [])
                             if 0 <= int(d) <= 6})
    if merged["recurrence"] == "weekly" and not merged["days"]:
        raise HTTPException(400, "a weekly briefing needs at least one day")
    merged["day_of_month"] = max(1, min(28, int(merged.get("day_of_month") or 1)))
    merged["enabled"] = bool(merged.get("enabled", True))
    return merged


def _looks_like_time(value: str) -> bool:
    try:
        hour, _, minute = str(value).partition(":")
        return 0 <= int(hour) <= 23 and 0 <= int(minute) <= 59
    except ValueError:
        return False


def describe_schedule(briefing: dict) -> str:
    """The schedule in words, for the UI and the activity log alike."""
    schedule = briefing.get("schedule") or {}
    if not schedule.get("enabled", True) or schedule.get("recurrence") == "never":
        return "only when asked"
    at = schedule.get("at", "07:00")
    recurrence = schedule.get("recurrence", "daily")
    if recurrence == "weekdays":
        return f"weekdays at {at}"
    if recurrence == "weekly":
        days = [DAY_NAMES[d] for d in schedule.get("days") or []]
        return f"{', '.join(days) or 'no day'} at {at}"
    if recurrence == "monthly":
        return f"day {schedule.get('day_of_month', 1)} of the month at {at}"
    return f"every day at {at}"


async def _clear_other_defaults(keep: int) -> None:
    async with await _connect() as conn:
        await conn.execute(
            "UPDATE briefings SET is_default = (id = %s)", (keep,))


@router.get("")
async def get_all(_: dict = Depends(auth.require_role("creator", "admin"))):
    rows = await listing()
    return {"briefings": [{**b, "schedule_text": describe_schedule(b)} for b in rows],
            "widgets": catalogue()}


@router.post("")
async def create(body: NewBriefing,
                 user: dict = Depends(auth.require_role("creator", "admin"))):
    widgets = _clean_widgets(body.widgets if body.widgets is not None
                             else _default_widgets())
    schedule = _clean_schedule(body.schedule or {})
    options = {**DEFAULT_OPTIONS, **(body.options or {})}
    try:
        async with await _connect() as conn:
            cur = await conn.execute(
                "INSERT INTO briefings (name, schedule, options, widgets) "
                f"VALUES (%s, %s, %s, %s) RETURNING {_COLUMNS}",
                (body.name.strip(), json.dumps(schedule), json.dumps(options),
                 json.dumps(widgets)))
            row = await cur.fetchone()
    except psycopg.errors.UniqueViolation:
        raise HTTPException(409, f"a briefing called {body.name!r} already exists")
    await activity.record("briefing.add", body.name, user["username"])
    return _row(row)


@router.patch("/{ident}")
async def update(ident: str, body: EditBriefing,
                 user: dict = Depends(auth.require_role("creator", "admin"))):
    current = await get(ident)
    widgets = (current["widgets"] if body.widgets is None
               else _clean_widgets(body.widgets))
    schedule = (current["schedule"] if body.schedule is None
                else _clean_schedule(body.schedule))
    options = {**current["options"], **(body.options or {})}
    async with await _connect() as conn:
        await conn.execute(
            "UPDATE briefings SET name = %s, enabled = %s, schedule = %s, "
            "options = %s, widgets = %s WHERE id = %s",
            (body.name.strip() if body.name else current["name"],
             current["enabled"] if body.enabled is None else body.enabled,
             json.dumps(schedule), json.dumps(options), json.dumps(widgets),
             current["id"]))
    if body.is_default:
        await _clear_other_defaults(current["id"])
    await activity.record("briefing.edit", current["name"], user["username"])
    return await get(current["id"])


@router.delete("/{ident}")
async def remove(ident: str,
                 user: dict = Depends(auth.require_role("creator", "admin"))):
    current = await get(ident)
    rows = await listing()
    if len(rows) <= 1:
        raise HTTPException(409, "that is the only briefing. Edit it rather than "
                                 "deleting it, or add another one first.")
    async with await _connect() as conn:
        await conn.execute("DELETE FROM briefings WHERE id = %s", (current["id"],))
    if current["is_default"]:
        # Something has to be the default, or "brief me" answers with nothing.
        remaining = await listing()
        await _clear_other_defaults(remaining[0]["id"])
    await activity.record("briefing.remove", current["name"], user["username"])
    return {"ok": True}


@router.post("/{ident}/run")
async def run_now(ident: str,
                  user: dict = Depends(auth.require_role("creator", "admin"))):
    return await run_and_deliver(await get(ident), user)
