"""The proactive engine (SPEC.md Phase 7).

IRiS speaking first, rather than only when spoken to. The daily briefing is the
first and most obviously useful shape of that, and it is deliberately built the
sober way round: the facts are gathered by calling the tools directly, and only the
*wording* is left to the model. Asking the model to "go and check everything" means
an 8B model sometimes decides it already knows, and a briefing that quietly invents
your morning is worse than no briefing.

Delivery lands where a person will actually see it: a new conversation in the chat,
exactly as if IRiS had messaged first, plus every configured webhook.
"""
import asyncio
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends

import activity
import auth
import integrations
import registry
import settings

router = APIRouter(prefix="/proactive", tags=["proactive"])

HOURS = [f"{h:02d}:00" for h in range(24)]

settings.setting(
    "proactive.enabled", type="boolean", default=True,
    title="Speak first", order=1,
    description="Let IRiS start a conversation, rather than only answering. The daily "
                "briefing is written whether or not you have the page open, so it is "
                "waiting when you get up.")
settings.setting(
    "proactive.briefing_at", type="string", enum=HOURS, default="07:00",
    title="Daily briefing at", order=2,
    description="When the briefing appears in the chat.")
settings.setting(
    "proactive.quiet_from", type="string", enum=HOURS, default="22:00",
    title="Quiet hours start",
    description="Nothing is delivered between these times. A briefing due inside "
                "quiet hours waits until they end.")
settings.setting(
    "proactive.quiet_to", type="string", enum=HOURS, default="07:00",
    title="Quiet hours end")
settings.setting(
    "proactive.include_mail", type="boolean", default=True,
    title="Include mail in the briefing",
    description="Only has an effect once a mailbox is set up under Integrations.")
settings.setting(
    "proactive.news", type="boolean", default=True,
    title="Include the news",
    description="A few headlines from the world and from your region, found by web "
                "search. The sources are attached to the briefing so you can see "
                "where each came from.")
settings.setting(
    "proactive.calendar", type="boolean", default=True,
    title="Include the calendar",
    description="Today's appointments. Only has an effect once a calendar is set up "
                "under Integrations.")
settings.setting(
    "proactive.weather", type="boolean", default=True,
    title="Include the weather",
    description="Today's forecast for Home. Needs Home set under Settings.")
settings.setting(
    "proactive.commute", type="boolean", default=True,
    title="Include the commute",
    description="Next departures from Home to Work. Needs both set under Settings.")


def greeting(when: datetime) -> str:
    """Worked out here, not by the model. Told to "greet them" with the time sitting
    in front of it, an 8B model still opened with "Good morning" at 22:18."""
    hour = when.hour
    if hour < 5:
        return "You're up late."
    if hour < 12:
        return "Good morning."
    if hour < 18:
        return "Good afternoon."
    return "Good evening."


def _now() -> datetime:
    return datetime.now(ZoneInfo(settings.get("general.timezone")))


def in_quiet_hours(when: datetime | None = None) -> bool:
    """Quiet hours normally wrap midnight, so a plain comparison is wrong for the
    common case rather than the rare one."""
    now = (when or _now()).strftime("%H:%M")
    start, end = settings.get("proactive.quiet_from"), settings.get("proactive.quiet_to")
    if start == end:
        return False
    if start < end:
        return start <= now < end
    return now >= start or now < end


# Two phrasings per section, merged. One query is one engine's idea of the news:
# "Switzerland news" with a one-day window returned four undated results and nothing
# survived the freshness filter, so the whole Swiss half of the briefing vanished.
NEWS_MAX_AGE_DAYS = 2
NEWS_PER_SECTION = 4


async def _section(label: str, queries: list[str]) -> dict | None:
    """Merge several searches into one source list, newest first, deduped by URL.

    The window is a week and the age filter is what actually decides freshness: a
    one-day window starves the engines of results before the filter ever runs.
    """
    import reasoning
    merged: list[dict] = []
    seen: set[str] = set()
    for query in queries:
        try:
            found = await asyncio.to_thread(
                reasoning.hits, query, "news", 10, "week", NEWS_MAX_AGE_DAYS)
        except Exception as e:
            print(f"[proactive] news search {query!r} failed: {e}", flush=True)
            continue
        if isinstance(found, str):
            continue
        for item in found:
            url = item.get("url") or ""
            if url and url not in seen:
                seen.add(url)
                merged.append(item)
    # An empty search is left out rather than reported as "no news today", which
    # would be a claim about the world instead of about the search.
    if not merged:
        return None
    merged.sort(key=lambda i: i.get("_at", 0), reverse=True)
    merged = merged[:NEWS_PER_SECTION]
    # Title plus the story's first line. Titles alone were all the model had, so all
    # it could do was read them back, and a briefing that repeats a headline and
    # stops has told you nothing a ticker would not.
    stories = [f"{(i.get('title') or '').strip()}. "
               f"{' '.join((i.get('content') or '').split())[:320]}".strip()
               for i in merged]
    return {"role": "tool", "tool_name": "web_search",
            "label": f"Headlines from {label}", "display": "sources",
            "content": reasoning.format_hits(merged), "titles": stories}


async def news() -> list[dict]:
    """Headlines as tool messages, so the briefing carries its sources exactly the
    way a live web search does and the same collapsed list renders them.

    Regional news is searched by region, not by home address: "Oetwil am See news"
    returns nothing at all, which is why only world headlines ever survived.
    """
    region = settings.get("location.region") or "Switzerland"
    sections = await asyncio.gather(
        _section("the world", ["world news", "international news today"]),
        _section(region, [region, f"{region} news"]),
    )
    return [s for s in sections if s]


async def gather() -> tuple[list[str], list[dict]]:
    """The facts, from the tools, deterministically. Anything unavailable is simply
    left out rather than guessed at or apologised for.

    Returns the notes the wording is built from, and the tool messages that carry
    the evidence behind them.
    """
    import places
    parts: list[str] = []
    sources: list[dict] = []
    now = _now()
    parts.append(now.strftime("It is %H:%M on %A, %d %B %Y."))
    parts.append(f"Greet them with exactly: {greeting(now)}")

    if settings.get("proactive.weather") and settings.get("location.enabled"):
        try:
            parts.append(await places.weather())
        except Exception as e:
            print(f"[proactive] weather lookup failed: {e}", flush=True)

    if settings.get("proactive.commute") and settings.get("location.enabled"):
        home, work = settings.get("location.home"), settings.get("location.work")
        if home and work:
            try:
                parts.append(await places.journey(home, work))
            except Exception as e:
                print(f"[proactive] commute lookup failed: {e}", flush=True)

    if settings.get("proactive.calendar"):
        if await registry.enabled_of_type("integration", "calendar"):
            try:
                parts.append(await integrations.check_all_calendars())
            except Exception as e:
                print(f"[proactive] calendar check failed: {e}", flush=True)

    if settings.get("proactive.include_mail"):
        if await registry.enabled_of_type("integration", "mailbox"):
            try:
                parts.append(await integrations.check_all_mail())
            except Exception as e:
                print(f"[proactive] mail check failed: {e}", flush=True)

    if settings.get("proactive.news"):
        sources = await news()
        for item in sources:
            if item["titles"]:
                parts.append(f"{item['label']}:\n"
                             + "\n".join(f"- {t}" for t in item["titles"]))

    return parts, sources


BRIEFING_PROMPT = """\
Write the briefing from the notes below.

Cover every section that IS in the notes, in the order given, and nothing else.
A section in the notes must not be dropped; a subject absent from the notes must
not be mentioned at all. If there is no calendar section, say nothing whatsoever
about appointments. If there is no mail section, say nothing about mail. If there
is no journey section, say nothing about trains or travel. Inventing one is worse
than a short briefing.

For the news, take the three or four most consequential stories across all the
headline sections and write a sentence or two on each SAYING WHAT HAPPENED AND WHY
IT MATTERS, in your own words, using the detail given after each headline. A
briefing that only repeats headlines is useless. Never repeat a headline verbatim.
Do not include links; they are attached separately.

Use ONLY what is in the notes. Do not add weather, news, appointments or anything
that is not there, and do not apologise for what is missing.
Open with the exact greeting the notes give you, once, and never a different one.
Do not state the date or the time back to them unless something depends on it.
Prose, not bullet points. No headers, no sign-off, no offer of further help.

NOTES:
"""


async def compose() -> tuple[str, list[dict]]:
    import memory
    facts, sources = await gather()
    if len(facts) <= 2:            # the time and the greeting, and nothing else
        return f"{greeting(_now())} Nothing to report.", sources
    try:
        text = await memory._complete(BRIEFING_PROMPT + "\n\n".join(facts), "")
    except Exception as e:
        print(f"[proactive] wording failed, sending the notes: {e}", flush=True)
        return "\n".join(facts), sources
    import reasoning
    clean = reasoning.strip_emoji(reasoning.strip_dashes(text.strip()))
    return clean or "\n".join(facts), sources


async def deliver(text: str, user_id: int, username: str,
                  title: str = "Briefing", sources: list[dict] | None = None) -> dict:
    """Into the chat, where a person will actually see it, and out to every webhook.

    The sources go in as tool messages ahead of the reply, so a briefing reads as an
    ordinary conversation: the same collapsed source list, with the same links.
    """
    import chat
    cid = uuid.uuid4().hex
    stamp = _now().strftime("%a %d %b, %H:%M")
    # `titles` is only for prompting; the stored message keeps the linked content.
    stored = [{k: v for k, v in s.items() if k != "titles"} for s in (sources or [])]
    await chat._save(user_id, cid,
                     stored + [{"role": "assistant", "content": text}],
                     title=f"{title} — {stamp}")
    hooks = await integrations.notify(text, event="briefing")
    await activity.record("proactive.briefing",
                          f"{len(text)} chars, {hooks} webhook(s)", username)
    return {"conversation_id": cid, "text": text, "webhooks": hooks}


BRIEFED_KEY = "proactive:briefed"


async def _already_briefed(day: str) -> bool:
    import chat
    return (await chat._redis.get(BRIEFED_KEY)) == day


async def _mark_briefed(day: str) -> None:
    import chat
    await chat._redis.set(BRIEFED_KEY, day)


async def scheduler() -> None:
    """Remembered in Redis, not in this process.

    Held in memory it did both wrong things at once: a restart re-briefed a day
    already briefed, and there was no way to tell a missed morning from a fresh one.
    Recorded outside the process, a restart is silent and a machine that was off at
    seven briefs as soon as it is on.
    """
    # A moment for Redis to be ready, since this starts with the app.
    await asyncio.sleep(5)
    while True:
        try:
            if settings.get("proactive.enabled"):
                now = _now()
                today = now.strftime("%Y%m%d")
                due = now.strftime("%H:%M") >= settings.get("proactive.briefing_at")
                if due and not in_quiet_hours(now) and not await _already_briefed(today):
                    user = await _first_user()
                    if user:
                        await _mark_briefed(today)
                        text, sources = await compose()
                        await deliver(text, user["id"], user["username"],
                                      sources=sources)
                        late = now.strftime("%H:%M") != settings.get(
                            "proactive.briefing_at")
                        print(f"[proactive] briefing delivered to {user['username']}"
                              + (" (catching up on a missed one)" if late else ""),
                              flush=True)
        except Exception as e:
            print(f"[proactive] failed: {e}", flush=True)
        await asyncio.sleep(60)


async def _first_user() -> dict | None:
    """Single user today; SPEC.md 4 keeps multi-user open, so this is the seam."""
    async with await auth._connect() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT id, username FROM users ORDER BY id LIMIT 1")
            row = await cur.fetchone()
    return {"id": row[0], "username": row[1]} if row else None


@router.get("")
async def status(_: dict = Depends(auth.active_user)):
    return {
        "enabled": settings.get("proactive.enabled"),
        "briefing_at": settings.get("proactive.briefing_at"),
        "quiet_now": in_quiet_hours(),
        "sources": (await gather())[0],
    }


@router.post("/briefing")
async def run_now(user: dict = Depends(auth.require_role("creator", "admin"))):
    text, sources = await compose()
    return await deliver(text, user["id"], user["username"], sources=sources)
