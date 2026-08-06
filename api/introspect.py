"""Self-inspection (SPEC.md Phase 8, §56).

IRiS answering questions about itself with facts rather than atmosphere. The persona
already forbids inventing a number, a name or a place, and §17 records what happens
without that: asked what it was doing it once said "running at 72% GPU, parsing sensor
data from the west wing". There is no west wing. `system_status` fixed the *measured*
half of that. This is the other half: what IRiS is made of, and what it has actually
done.

Both are **generated, never written down**. A hand-written architecture summary is
correct on the day it is written and quietly wrong a month later, which is a worse
failure than having none: it reads as authoritative. Everything below is derived from
the live registries, so a tool added tomorrow appears here tomorrow with no work.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends

import auth
import settings

router = APIRouter(prefix="/self", tags=["self"])

# The audit log records an action name and a detail. Turned into something a person
# would recognise, because "chat.message" is a log line and not an answer.
ACTION_WORDS = {
    "chat.message": "answered a message",
    "chat.delete": "deleted a conversation",
    "chat.export": "exported a conversation",
    "chat.rewind": "was asked to answer something again",
    "proactive.briefing": "delivered a briefing",
    "rule.fired": "an automation fired",
    "timer.fired": "a timer went off",
    "timer.set": "set a timer",
    "alarm.set": "set an alarm",
    "memory.remember": "remembered something",
    "memory.forget": "forgot something",
    "files.analyze": "read a file",
    "file.upload": "received a file",
    "voice.transcribe": "transcribed speech",
    "voice.speak": "spoke a reply",
    "settings.change": "a setting was changed",
    "tools.edit": "a tool's instructions were edited",
    "tools.reset": "a tool was reset to its defaults",
    "backup.run": "took a backup",
    "device.add": "a device was added",
    "integration.add": "an integration was added",
    "briefing.add": "a briefing was created",
    "briefing.edit": "a briefing was changed",
    "rule.add": "an automation was created",
    "proposal.propose": "proposed a change to itself",
    "proposal.approve": "a proposed change was approved",
    "proposal.reject": "a proposed change was rejected",
    "proposal.revert": "an applied change was undone",
}


def _now() -> datetime:
    return datetime.now(ZoneInfo(settings.get("general.timezone")))


def architecture() -> dict:
    """What IRiS is made of, counted from the live registries.

    Imported inside rather than at module scope: nearly every module here imports
    something that imports this one back, and the counts are wanted at call time
    anyway, not at import time.
    """
    import briefings
    import reasoning
    import registry
    import rules
    import toolkit

    tools = sorted(reasoning.TOOLS)
    groups: dict[str, list[str]] = {}
    for name in tools:
        groups.setdefault(toolkit.GROUPS.get(name, "Other"), []).append(name)

    setting_groups: dict[str, int] = {}
    for key in settings.REGISTRY:
        setting_groups[key.split(".")[0]] = setting_groups.get(key.split(".")[0], 0) + 1

    return {
        "model": settings.get("llm.model"),
        "reasoning_mode": settings.get("llm.think"),
        "vision_model": settings.get("vision.model"),
        "speech_model": settings.get("voice.stt_model"),
        "voice": settings.get("voice.tts_speaker"),
        "tools": tools,
        "tool_groups": groups,
        "tools_always_offered": sorted(reasoning.CORE_TOOLS),
        "tool_budget": settings.get("llm.tool_budget"),
        "disabled_tools": sorted(n for n in tools if not toolkit.enabled(n)),
        "edited_tools": sorted(n for n in tools if toolkit._prefs.get(n)),
        "briefing_widgets": sorted(briefings.WIDGETS),
        "automation_triggers": sorted(rules.TRIGGERS),
        "device_types": sorted(t.name for t in registry.types_of("device")),
        "integration_types": sorted(t.name for t in registry.types_of("integration")),
        "settings": setting_groups,
        "setting_count": len(settings.REGISTRY),
    }


async def live() -> dict:
    """The measured half: what is actually running right now."""
    import system
    snapshot = await system.snapshot()
    return {"uptime": snapshot.get("uptime"),
            "gpu": snapshot.get("gpu"),
            "models_loaded": snapshot.get("models_loaded"),
            "services": snapshot.get("services")}


def _shape(text: str, width: int = 78) -> str:
    return text


async def describe(topic: str = "") -> str:
    """The architecture as prose for the model to read out.

    Filtered by topic when one is given, because handing an 8B model everything it is
    made of and asking about the memory system means it answers about the cameras.
    """
    import briefings
    import reasoning
    import registry
    import rules

    facts = architecture()
    wanted = (topic or "").strip().lower()
    lines: list[str] = []

    def relevant(*words: str) -> bool:
        return not wanted or any(w in wanted or wanted in w for w in words)

    if relevant("model", "brain", "thinking", "llm", "reasoning"):
        lines.append(
            f"Thinking: {facts['model']} through Ollama, reasoning mode "
            f"'{facts['reasoning_mode']}'. Pictures go to {facts['vision_model']}, "
            f"speech to faster-whisper {facts['speech_model']}, and the voice is "
            f"{facts['voice']}. Everything runs on this machine; nothing is sent "
            f"anywhere else.")

    if relevant("tool", "tools", "capability", "can you", "do"):
        lines.append(
            f"Tools: {len(facts['tools'])} of them. Not all are offered every message: "
            f"{len(facts['tools_always_offered'])} are always available "
            f"({', '.join(facts['tools_always_offered'])}) and up to "
            f"{facts['tool_budget']} more are chosen per message from trigger words "
            f"and similarity.")
        for group, names in sorted(facts["tool_groups"].items()):
            lines.append(f"  {group}: {', '.join(names)}")
        if facts["disabled_tools"]:
            lines.append(f"  Switched off: {', '.join(facts['disabled_tools'])}.")
        if facts["edited_tools"]:
            lines.append(f"  Instructions edited by the user: "
                         f"{', '.join(facts['edited_tools'])}.")

    if relevant("memory", "remember", "recall"):
        lines.append(
            "Memory: facts are embedded with bge-m3 and stored in Qdrant, recalled "
            "into the system turn automatically rather than on request. Conversations "
            "are learned from with a grounding filter, so only what the user actually "
            "said becomes a memory. Raw transcripts expire on a rolling window; what "
            "was distilled from them does not.")

    if relevant("briefing", "briefings", "proactive", "speak first"):
        lines.append(
            f"Briefings: built from {len(facts['briefing_widgets'])} kinds of widget "
            f"({', '.join(facts['briefing_widgets'])}), each with its own settings and "
            f"sentence budget, each briefing with its own schedule.")

    if relevant("automation", "automations", "rule", "rules", "trigger"):
        lines.append(
            f"Automations: {len(facts['automation_triggers'])} things can set one off "
            f"({', '.join(facts['automation_triggers'])}). Each remembers what it has "
            f"already reported, so a condition that stays true is not a message every "
            f"thirty seconds.")

    if relevant("device", "devices", "camera", "integration", "integrations"):
        lines.append(
            f"Devices: {', '.join(facts['device_types'])}. Integrations: "
            f"{', '.join(facts['integration_types'])}. Both are one generic registry, "
            f"so adding a kind needs no new endpoint and no new UI.")

    if relevant("setting", "settings", "configure", "configuration"):
        top = sorted(facts["settings"].items(), key=lambda kv: -kv[1])[:6]
        lines.append(
            f"Settings: {facts['setting_count']} of them, all editable in the UI and "
            f"none in a file the user cannot reach. Largest groups: "
            + ", ".join(f"{group} ({count})" for group, count in top) + ".")

    if relevant("storage", "database", "where", "data", "postgres", "backup"):
        lines.append(
            "Storage: Postgres for settings, users, the audit log, briefings, "
            "automations and timers; Qdrant for memories; Redis for sessions and "
            "conversations. Backups are AES-256 archives on this machine.")

    if not lines:
        return (f"Nothing I know about myself matches {topic!r}. Ask about the model, "
                f"tools, memory, briefings, automations, devices, settings or storage.")
    return "\n".join(lines)


async def history(about: str = "", hours: float = 24, limit: int = 40) -> str:
    """What IRiS has actually done, from the audit log.

    The point of Phase 8 is that "why did you do that" gets a real answer. This is
    where the real answer comes from: a record written when the action happened, not
    a reconstruction afterwards.
    """
    import activity
    since = _now() - timedelta(hours=max(0.1, hours))
    async with await activity._connect() as conn:
        cur = await conn.execute(
            "SELECT at, actor, action, detail FROM activity WHERE at >= %s "
            "ORDER BY id DESC LIMIT 500", (since,))
        rows = await cur.fetchall()

    wanted = (about or "").strip().lower()
    out = []
    for at, actor, action, detail in rows:
        words = ACTION_WORDS.get(action, action.replace(".", " "))
        text = f"{words}{': ' + detail if detail else ''}"
        if wanted and wanted not in text.lower() and wanted not in action.lower():
            continue
        local = at.astimezone(ZoneInfo(settings.get("general.timezone")))
        out.append(f"- {local:%H:%M on %a %d %b}: {text}"
                   + (f" (asked by {actor})" if actor and actor != "iris" else ""))
        if len(out) >= limit:
            break

    if not out:
        window = f"the last {int(hours)} hours" if hours != 24 else "the last day"
        return (f"Nothing in the log about {about!r} in {window}."
                if wanted else f"Nothing recorded in {window}.")
    return "\n".join(out)


@router.get("")
async def get_self(_: dict = Depends(auth.active_user)):
    """Everything the Customize tab needs to show what IRiS is made of."""
    return {"architecture": architecture(), "live": await live(),
            "summary": await describe()}


@router.get("/history")
async def get_history(about: str = "", hours: float = 24,
                      _: dict = Depends(auth.active_user)):
    return {"text": await history(about, hours)}
