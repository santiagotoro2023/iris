"""The tool library, made visible and configurable (SPEC.md 44).

`reasoning._relevant` already hands the model a short list of tools per turn
instead of injecting all eighteen, which is what fixed the regression in SPEC.md
44. What it never did was let anybody see that or change it: the descriptions and
the trigger words that decide the routing lived only in the source.

This module is the override table and the API over it, and nothing else. Only
overrides are stored, never a copy of the declaration, so a tool whose wording
improves in a later version is not frozen at whatever happened to be declared the
first time the page was opened. `reasoning` reads the four accessors below on
every turn, so the table is cached in memory and refreshed on write rather than
queried per message.
"""
import json
import os
import re

import psycopg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field as PField

import activity
import auth
import settings

DATABASE_URL = os.environ.get("DATABASE_URL", "")
router = APIRouter(prefix="/tools", tags=["tools"])

CORE_WARNING = "Disabling a core tool removes a capability IRiS relies on"

# Coarse grouping for the UI, by tool name. A newly registered tool that is not
# listed still appears, under "Other", so forgetting this line cannot hide a tool.
GROUPS = {
    "web_search": "Knowledge",
    "current_time": "Knowledge",
    "remember": "Memory",
    "recall": "Memory",
    "transit": "Places",
    "departures": "Places",
    "route_to": "Places",
    "find_place": "Places",
    "weather": "Places",
    "where_am_i": "Places",
    "check_mail": "Integrations",
    "calendar": "Integrations",
    "analyze_file": "Files",
    "look_at_camera": "Home",
    "system_status": "System",
}
GROUP_ORDER = ["Knowledge", "Places", "Integrations", "Memory", "Files", "Home",
               "System", "Other"]

# name -> {"enabled": bool, "description": str, "triggers": str}. Absent means no
# override at all, which is not the same as an override that happens to match the
# declaration: only the former follows the code when the code changes.
_prefs: dict[str, dict] = {}


async def _connect():
    return await psycopg.AsyncConnection.connect(DATABASE_URL, autocommit=True)


async def load() -> None:
    """Create the tables and warm the caches. Called once at startup."""
    async with await _connect() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS tool_prefs (
                name        TEXT PRIMARY KEY,
                enabled     BOOLEAN NOT NULL DEFAULT TRUE,
                description TEXT NOT NULL DEFAULT '',
                triggers    TEXT NOT NULL DEFAULT '',
                updated     TIMESTAMPTZ NOT NULL DEFAULT now()
            )""")
        # Added after the table shipped, so it is a migration rather than part of the
        # CREATE: an existing install must not need its overrides dropped.
        await conn.execute("ALTER TABLE tool_prefs ADD COLUMN IF NOT EXISTS "
                           "parameters JSONB NOT NULL DEFAULT '{}'")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS quick_commands (
                name      TEXT PRIMARY KEY,
                label     TEXT NOT NULL DEFAULT '',
                hint      TEXT NOT NULL DEFAULT '',
                directive TEXT NOT NULL DEFAULT '',
                enabled   BOOLEAN NOT NULL DEFAULT TRUE,
                custom    BOOLEAN NOT NULL DEFAULT TRUE,
                updated   TIMESTAMPTZ NOT NULL DEFAULT now()
            )""")
        cur = await conn.execute(
            "SELECT name, enabled, description, triggers, parameters FROM tool_prefs")
        rows = await cur.fetchall()
        cur = await conn.execute(
            "SELECT name, label, hint, directive, enabled, custom FROM quick_commands "
            "ORDER BY name")
        commands = await cur.fetchall()
    _prefs.clear()
    _prefs.update({name: {"enabled": on, "description": desc, "triggers": trig,
                          "parameters": params or {}}
                   for name, on, desc, trig, params in rows})
    _commands.clear()
    _commands.update({name: {"name": name, "label": label, "hint": hint,
                             "directive": directive, "enabled": on, "custom": custom}
                      for name, label, hint, directive, on, custom in commands})


# ------------------------------------------------------- what reasoning asks ----

def enabled(name: str) -> bool:
    """Unknown tools are on. A tool registered by a later phase must work before
    anybody has visited the page that would create its row."""
    return _prefs.get(name, {}).get("enabled", True)


def description_for(name: str, declared: str) -> str:
    return _prefs.get(name, {}).get("description") or declared


def parameters_for(name: str, declared: dict) -> dict:
    """Declared parameters with any edited argument descriptions folded in.

    Only the `description` of a property is overridable. Types, required-ness and the
    property names themselves stay with the code: an argument the model is told to
    fill but the function does not take is not a customisation, it is a crash, and
    "you can edit the instructions" should not mean "you can edit the signature".
    """
    edits = _prefs.get(name, {}).get("parameters") or {}
    if not edits:
        return declared
    properties = declared.get("properties") or {}
    patched = {}
    for prop, spec in properties.items():
        text = str(edits.get(prop, "")).strip()
        patched[prop] = {**spec, "description": text} if text else spec
    return {**declared, "properties": patched}


def triggers_for(name: str, declared: tuple) -> tuple:
    raw = _prefs.get(name, {}).get("triggers", "")
    if not raw.strip():
        return declared
    # Matched with `word in query` against a lowercased message, so the override has
    # to be lowercased here or a capitalised entry would simply never fire.
    return tuple(w.strip().lower() for w in raw.split(",") if w.strip())


def why(name: str, matched: str = "", score: float | None = None) -> str:
    """One short phrase for the tool banner: why this tool was offered at all.

    Pure formatting. Core tools are checked first because they are never ranked,
    so a word match on one of them would be a coincidence, not the reason.
    """
    import reasoning
    if name in reasoning.CORE_TOOLS:
        return "always available"
    if matched:
        return f'matched the word "{matched}"'
    if score is not None:
        return f"closest match for this message ({score:.2f})"
    return "offered for this message"


# ------------------------------------------------------------------ storage ----

def _forget_vectors() -> None:
    """The similarity ranking embeds every tool description once per process. An
    edited description would otherwise keep being ranked by its old wording until
    the next restart, which reads as the edit having done nothing."""
    import reasoning
    reasoning._tool_vectors = None


async def _store(name: str, on: bool, description: str, triggers: str,
                 parameters: dict) -> None:
    async with await _connect() as conn:
        await conn.execute(
            "INSERT INTO tool_prefs (name, enabled, description, triggers, parameters)"
            " VALUES (%s, %s, %s, %s, %s) ON CONFLICT (name) DO UPDATE SET "
            "enabled = EXCLUDED.enabled, description = EXCLUDED.description, "
            "triggers = EXCLUDED.triggers, parameters = EXCLUDED.parameters, "
            "updated = now()",
            (name, on, description, triggers, json.dumps(parameters)))
    # ponytail: in-process cache, correct for the single uvicorn worker we run,
    # same bargain settings.py makes. Move to LISTEN/NOTIFY if that ever changes.
    _prefs[name] = {"enabled": on, "description": description, "triggers": triggers,
                    "parameters": parameters}
    _forget_vectors()


def _entry(name: str) -> dict:
    import reasoning
    spec = reasoning.TOOLS[name]
    function = spec.schema["function"]
    declared_description = function["description"]
    declared_triggers = list(reasoning.TRIGGERS.get(name, ()))
    pref = _prefs.get(name, {})
    return {
        "name": name,
        "label": name.replace("_", " ").title(),
        "group": GROUPS.get(name, "Other"),
        "description": description_for(name, declared_description),
        "declared_description": declared_description,
        "parameters": parameters_for(name, function.get("parameters", {})),
        "declared_parameters": function.get("parameters", {}),
        "activity": spec.activity,
        "display": spec.display,
        "core": name in reasoning.CORE_TOOLS,
        "enabled": enabled(name),
        "triggers": list(triggers_for(name, tuple(declared_triggers))),
        "declared_triggers": declared_triggers,
        "overridden": bool(pref.get("description") or pref.get("triggers")
                           or pref.get("parameters")
                           or not pref.get("enabled", True)),
    }


def _rank(entry: dict) -> tuple:
    group = entry["group"]
    index = GROUP_ORDER.index(group) if group in GROUP_ORDER else len(GROUP_ORDER)
    return (index, entry["label"])


def _known(name: str) -> None:
    import reasoning
    if name not in reasoning.TOOLS:
        raise HTTPException(404, f"no tool called {name!r}")


# --------------------------------------------------------------------- http ----

class ToolEdit(BaseModel):
    enabled: bool | None = None
    # Blank clears the override and puts the declared wording back; the client
    # cannot express "no description at all", and should not be able to.
    description: str | None = PField(None, max_length=2000)
    triggers: str | None = PField(None, max_length=1000)
    # {argument name: its description}. Anything not naming a real argument is
    # dropped rather than stored, so a renamed argument cannot leave a ghost behind.
    parameters: dict[str, str] | None = None


@router.get("")
async def get_tools(_: dict = Depends(auth.active_user)):
    """Every registered tool with what it says, what it takes and why it fires."""
    import reasoning
    tools = sorted((_entry(name) for name in reasoning.TOOLS), key=_rank)
    return {"tools": tools, "budget": settings.get("llm.tool_budget")}


@router.patch("/{name}")
async def edit_tool(name: str, body: ToolEdit,
                    user: dict = Depends(auth.require_role("creator", "admin"))):
    _known(name)
    import reasoning
    current = _prefs.get(name, {})
    on = current.get("enabled", True) if body.enabled is None else body.enabled
    description = (current.get("description", "") if body.description is None
                   else body.description.strip())
    triggers = (current.get("triggers", "") if body.triggers is None
                else body.triggers.strip())
    parameters = dict(current.get("parameters") or {})
    if body.parameters is not None:
        declared = (reasoning.TOOLS[name].schema["function"]
                    .get("parameters", {}).get("properties") or {})
        parameters = {k: v.strip() for k, v in body.parameters.items()
                      if k in declared and v.strip()}
    await _store(name, on, description, triggers, parameters)

    said = []
    if body.enabled is not None:
        said.append("enabled" if on else "disabled")
    if body.description is not None:
        said.append("description set" if description else "description reset")
    if body.triggers is not None:
        said.append("triggers set" if triggers else "triggers reset")
    if body.parameters is not None:
        said.append(f"{len(parameters)} argument(s) reworded" if parameters
                    else "arguments reset")
    await activity.record("tools.edit", f"{name}: {', '.join(said) or 'no change'}",
                          user["username"])

    entry = _entry(name)
    # Santiago's machine, Santiago's call: a core tool can be switched off, but the
    # UI gets told what that costs rather than the request being refused.
    return {**entry, "warning": CORE_WARNING if entry["core"] and not on else ""}


@router.post("/{name}/reset")
async def reset_tool(name: str,
                     user: dict = Depends(auth.require_role("creator", "admin"))):
    """Drop every override for one tool, so it follows the code again."""
    _known(name)
    async with await _connect() as conn:
        await conn.execute("DELETE FROM tool_prefs WHERE name = %s", (name,))
    _prefs.pop(name, None)
    _forget_vectors()
    await activity.record("tools.reset", name, user["username"])
    return {**_entry(name), "warning": ""}


# --------------------------------------------------------- quick commands ----
# The composer's menu. A built-in lives in `reasoning.QUICK_COMMANDS` and only its
# enabled flag is stored here; a custom one is stored whole. Same bargain as the
# tools above: the code stays the source of truth for anything not overridden.

_commands: dict[str, dict] = {}


def command_enabled(name: str) -> bool:
    return _commands.get(name, {}).get("enabled", True)


def custom_commands() -> list[dict]:
    return [{"name": c["name"], "label": c["label"], "hint": c["hint"],
             "custom": True}
            for c in _commands.values() if c["custom"] and c["enabled"]]


def custom_command(name: str) -> dict | None:
    found = _commands.get(name)
    return found if found and found["custom"] else None


async def _store_command(row: dict) -> None:
    async with await _connect() as conn:
        await conn.execute(
            "INSERT INTO quick_commands (name, label, hint, directive, enabled, "
            "custom) VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (name) DO UPDATE SET "
            "label = EXCLUDED.label, hint = EXCLUDED.hint, "
            "directive = EXCLUDED.directive, enabled = EXCLUDED.enabled, "
            "updated = now()",
            (row["name"], row["label"], row["hint"], row["directive"],
             row["enabled"], row["custom"]))
    _commands[row["name"]] = row


class NewCommand(BaseModel):
    label: str = PField(min_length=1, max_length=40)
    hint: str = PField("", max_length=80)
    directive: str = PField(min_length=1, max_length=2000)


class CommandEdit(BaseModel):
    label: str | None = PField(None, max_length=40)
    hint: str | None = PField(None, max_length=80)
    directive: str | None = PField(None, max_length=2000)
    enabled: bool | None = None


def _slug(label: str) -> str:
    out = "".join(c if c.isalnum() else "_" for c in label.lower()).strip("_")
    return re.sub(r"_+", "_", out) or "command"


@router.get("/commands")
async def get_commands(_: dict = Depends(auth.active_user)):
    """Built-ins and custom ones in one list, so the UI shows a single table."""
    import reasoning
    out = []
    for name, spec in reasoning.QUICK_COMMANDS.items():
        out.append({"name": name, "label": spec["label"], "hint": spec["hint"],
                    "directive": spec["directive"], "custom": False,
                    "enabled": command_enabled(name),
                    "needs": spec.get("needs", "")})
    out.extend({**c, "needs": ""} for c in _commands.values() if c["custom"])
    return {"commands": out}


@router.post("/commands")
async def add_command(body: NewCommand,
                      user: dict = Depends(auth.require_role("creator", "admin"))):
    import reasoning
    name = _slug(body.label)
    if name in reasoning.QUICK_COMMANDS or name in _commands:
        raise HTTPException(409, f"a command called {body.label!r} already exists")
    row = {"name": name, "label": body.label.strip(), "hint": body.hint.strip(),
           "directive": body.directive.strip(), "enabled": True, "custom": True}
    await _store_command(row)
    await activity.record("commands.add", body.label, user["username"])
    return row


@router.patch("/commands/{name}")
async def edit_command(name: str, body: CommandEdit,
                       user: dict = Depends(auth.require_role("creator", "admin"))):
    import reasoning
    builtin = reasoning.QUICK_COMMANDS.get(name)
    current = _commands.get(name)
    if not builtin and not current:
        raise HTTPException(404, f"no command called {name!r}")
    if builtin and (body.label or body.hint or body.directive):
        # Editing a built-in's wording would silently fork it from the code, which is
        # the failure mode the tool overrides above go to some trouble to avoid.
        raise HTTPException(
            400, "a built-in command can be switched off but not rewritten. Add a "
                 "custom command instead.")
    row = current or {"name": name, "label": builtin["label"],
                      "hint": builtin["hint"], "directive": builtin["directive"],
                      "enabled": True, "custom": False}
    row = {**row,
           "label": row["label"] if body.label is None else body.label.strip(),
           "hint": row["hint"] if body.hint is None else body.hint.strip(),
           "directive": (row["directive"] if body.directive is None
                         else body.directive.strip()),
           "enabled": row["enabled"] if body.enabled is None else body.enabled}
    await _store_command(row)
    await activity.record("commands.edit", name, user["username"])
    return row


@router.delete("/commands/{name}")
async def remove_command(name: str,
                         user: dict = Depends(auth.require_role("creator", "admin"))):
    if name not in _commands or not _commands[name]["custom"]:
        raise HTTPException(404, f"no custom command called {name!r}")
    async with await _connect() as conn:
        await conn.execute("DELETE FROM quick_commands WHERE name = %s", (name,))
    _commands.pop(name, None)
    await activity.record("commands.remove", name, user["username"])
    return {"ok": True}


if __name__ == "__main__":
    # The overrides are string handling, and string handling is where this quietly
    # goes wrong. Runnable without Postgres or the rest of the stack.
    import sys
    import types

    stub = types.ModuleType("reasoning")
    stub.CORE_TOOLS = {"web_search"}
    sys.modules.setdefault("reasoning", stub)

    assert enabled("nothing_registered_yet") is True
    assert description_for("weather", "declared") == "declared"
    assert triggers_for("weather", ("rain",)) == ("rain",)

    _prefs["weather"] = {"enabled": False, "description": "Mine",
                         "triggers": " Rain, umbrella ,, "}
    assert enabled("weather") is False
    assert description_for("weather", "declared") == "Mine"
    assert triggers_for("weather", ("rain",)) == ("rain", "umbrella")

    _prefs["weather"] = {"enabled": True, "description": "", "triggers": "   "}
    assert description_for("weather", "declared") == "declared"
    assert triggers_for("weather", ("rain",)) == ("rain",)

    assert why("web_search", matched="search") == "always available"
    assert why("weather", matched="rain") == 'matched the word "rain"'
    assert why("weather", score=0.7134) == "closest match for this message (0.71)"
    assert why("weather") == "offered for this message"

    # Argument descriptions are editable; the shape of the argument list is not.
    declared = {"type": "object",
                "properties": {"place": {"type": "string", "description": "orig"},
                               "days": {"type": "integer"}},
                "required": ["place"]}
    _prefs["weather"] = {"enabled": True, "description": "", "triggers": "",
                         "parameters": {"place": "mine", "invented": "ignored"}}
    patched = parameters_for("weather", declared)
    assert patched["properties"]["place"]["description"] == "mine"
    assert patched["properties"]["place"]["type"] == "string"
    assert "invented" not in patched["properties"]
    assert patched["required"] == ["place"]
    # No override at all must hand back the declaration itself, untouched.
    _prefs.pop("weather")
    assert parameters_for("weather", declared) is declared

    assert _slug("Brief me!") == "brief_me"
    print("toolkit self-check passed")
