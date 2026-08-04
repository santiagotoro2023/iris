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
    "proactive.enabled", type="boolean", default=False,
    title="Speak first",
    description="Let IRiS start a conversation on its own, rather than only "
                "answering. Off by default: it should be your decision that it may "
                "interrupt you.")
settings.setting(
    "proactive.briefing_at", type="string", enum=HOURS, default="07:00",
    title="Daily briefing at",
    description="When to put the morning briefing in the chat. Skipped entirely if "
                "the machine was off, rather than delivered at lunchtime.")
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
    "proactive.commute", type="boolean", default=True,
    title="Include the commute",
    description="Next departures from Home to Work. Needs both set under Settings.")


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


async def gather() -> list[str]:
    """The facts, from the tools, deterministically. Anything unavailable is simply
    left out rather than guessed at or apologised for."""
    import places
    parts: list[str] = []
    now = _now()
    parts.append(now.strftime("It is %H:%M on %A, %d %B %Y."))

    if settings.get("proactive.commute") and settings.get("location.enabled"):
        home, work = settings.get("location.home"), settings.get("location.work")
        if home and work:
            try:
                parts.append(await places.journey(home, work))
            except Exception as e:
                print(f"[proactive] commute lookup failed: {e}", flush=True)

    if settings.get("proactive.include_mail"):
        if await registry.enabled_of_type("integration", "mailbox"):
            try:
                parts.append(await integrations.check_all_mail())
            except Exception as e:
                print(f"[proactive] mail check failed: {e}", flush=True)

    return parts


BRIEFING_PROMPT = """\
Write the morning briefing from the notes below. Rules:

Use ONLY what is in the notes. Do not add weather, news, appointments or anything
else that is not there, and do not apologise for what is missing.
Keep it to a few sentences. No headers, no bullet points unless the notes contain a
list of things, no sign-off, no offer of further help.
Open by greeting them, once.

NOTES:
"""


async def compose() -> str:
    import memory
    facts = await gather()
    if len(facts) <= 1:
        # Only the date. There is nothing to brief, and saying so plainly beats
        # padding it out.
        return facts[0] + " Nothing else to report."
    try:
        text = await memory._complete(BRIEFING_PROMPT + "\n".join(facts), "")
    except Exception as e:
        print(f"[proactive] wording failed, sending the notes: {e}", flush=True)
        return "\n".join(facts)
    import reasoning
    return reasoning.strip_emoji(reasoning.strip_dashes(text.strip())) or \
        "\n".join(facts)


async def deliver(text: str, user_id: int, username: str,
                  title: str = "Briefing") -> dict:
    """Into the chat, where a person will actually see it, and out to every webhook."""
    import chat
    cid = uuid.uuid4().hex
    stamp = _now().strftime("%a %d %b, %H:%M")
    await chat._save(user_id, cid, [{"role": "assistant", "content": text}],
                     title=f"{title} — {stamp}")
    hooks = await integrations.notify(text, event="briefing")
    await activity.record("proactive.briefing",
                          f"{len(text)} chars, {hooks} webhook(s)", username)
    return {"conversation_id": cid, "text": text, "webhooks": hooks}


async def scheduler() -> None:
    """Stateless, like the backup scheduler: it asks what has already been delivered
    today rather than remembering. A restart cannot double-brief."""
    import chat
    last_date = ""
    while True:
        await asyncio.sleep(60)
        try:
            if not settings.get("proactive.enabled"):
                continue
            now = _now()
            today = now.strftime("%Y%m%d")
            if last_date == today:
                continue
            if now.strftime("%H:%M") < settings.get("proactive.briefing_at"):
                continue
            if in_quiet_hours(now):
                continue
            user = await _first_user()
            if not user:
                continue
            last_date = today
            text = await compose()
            await deliver(text, user["id"], user["username"])
            print(f"[proactive] briefing delivered to {user['username']}", flush=True)
        except Exception as e:
            print(f"[proactive] failed: {e}", flush=True)


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
        "sources": await gather(),
    }


@router.post("/briefing")
async def run_now(user: dict = Depends(auth.require_role("creator", "admin"))):
    return await deliver(await compose(), user["id"], user["username"])
