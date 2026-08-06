"""The proactive engine (SPEC.md Phase 7).

IRiS speaking first, rather than only when spoken to. This module is now the *delivery*
half of that and the quiet-hours policy around it. What is actually said comes from two
places that grew out of it:

- `briefings.py` — named, scheduled briefings built from widgets (SPEC.md 55)
- `rules.py` — things that speak when something happens rather than at a time

Delivery lands where a person will actually see it: a new conversation in the chat,
exactly as if IRiS had messaged first, plus every configured webhook and phone.
"""
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends

import activity
import auth
import integrations
import settings

router = APIRouter(prefix="/proactive", tags=["proactive"])

HOURS = [f"{h:02d}:00" for h in range(24)]

settings.setting(
    "proactive.enabled", type="boolean", default=True,
    title="Speak first", order=1,
    description="Let IRiS start a conversation, rather than only answering. Briefings "
                "are written whether or not you have the page open, so one is waiting "
                "when you get up.")
settings.setting(
    "proactive.quiet_from", type="string", enum=HOURS, default="22:00",
    title="Quiet hours start", order=2,
    description="Nothing is delivered between these times. Anything due inside quiet "
                "hours waits until they end.")
settings.setting(
    "proactive.quiet_to", type="string", enum=HOURS, default="07:00",
    title="Quiet hours end", order=3)


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


async def deliver(text: str, user_id: int, username: str,
                  title: str = "Briefing", sources: list[dict] | None = None) -> dict:
    """Into the chat, where a person will actually see it, and out to every webhook.

    The sources go in as tool messages ahead of the reply, so a briefing reads as an
    ordinary conversation: the same collapsed source list, with the same links.
    """
    import chat
    cid = uuid.uuid4().hex
    stamp = _now().strftime("%a %d %b, %H:%M")
    stored = [{k: v for k, v in s.items() if k != "titles"} for s in (sources or [])]
    await chat._save(user_id, cid,
                     stored + [{"role": "assistant", "content": text}],
                     title=f"{title} — {stamp}")
    hooks = await integrations.notify(text, event="briefing")
    await activity.record("proactive.briefing",
                          f"{title}, {len(text)} chars, {hooks} webhook(s)", username)
    return {"conversation_id": cid, "text": text, "webhooks": hooks}


async def _first_user() -> dict | None:
    """Single user today; SPEC.md 4 keeps multi-user open, so this is the seam."""
    async with await auth._connect() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT id, username FROM users ORDER BY id LIMIT 1")
            row = await cur.fetchone()
    return {"id": row[0], "username": row[1]} if row else None


@router.get("")
async def status(_: dict = Depends(auth.active_user)):
    import briefings
    rows = await briefings.listing()
    return {
        "enabled": settings.get("proactive.enabled"),
        "quiet_now": in_quiet_hours(),
        "briefings": [{"id": b["id"], "name": b["name"], "enabled": b["enabled"],
                       "is_default": b["is_default"],
                       "schedule": briefings.describe_schedule(b)} for b in rows],
    }


@router.post("/briefing")
async def run_now(user: dict = Depends(auth.require_role("creator", "admin"))):
    """The "brief me now" button. Runs the default briefing, so the endpoint means the
    same thing it always did even though what it runs is now configurable."""
    import briefings
    from fastapi import HTTPException
    briefing = await briefings.default_briefing()
    if not briefing:
        raise HTTPException(404, "no briefings are configured")
    return await briefings.run_and_deliver(briefing, user)
