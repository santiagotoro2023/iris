"""Self-modification, through a review queue (SPEC.md Phase 8, §56).

IRiS can propose a change to itself. It cannot make one. Every proposal lands in a
queue with the current value beside the suggested one and the reason it gave, and
nothing moves until a person approves it. Anything applied can be undone in one press,
because the before-value is stored with the proposal rather than reconstructed.

**What it may propose is deliberately bounded to its own configuration**: a setting, a
line of its persona, the wording or trigger words of one of its tools, a quick command.
Not code.

That bound is a decision, not an oversight. The spec asks for human-approved diffs with
rollback, and for configuration that is exactly what this is. Extending it to source
would mean an 8B model writing Python that runs as root on a box holding every
conversation, every memory and every credential, on a repo that is public and a port
that may be forwarded; "a human approves the diff" is not much of a control when the
diff is forty lines of code at seven in the morning. If that is wanted later it should
be its own decision, with its own review, not a side effect of this one.
"""
import json
import os

import psycopg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field as PField

import activity
import auth
import settings

DATABASE_URL = os.environ.get("DATABASE_URL", "")
router = APIRouter(prefix="/proposals", tags=["self"])

KINDS = ["setting", "persona", "tool", "command"]
STATUSES = ["pending", "approved", "rejected", "reverted"]
MAX_PENDING = 25


async def _connect():
    return await psycopg.AsyncConnection.connect(DATABASE_URL, autocommit=True)


_COLUMNS = ("id, kind, target, field, before_value, after_value, reason, status, "
            "created, decided, decided_by")


def _row(row: tuple) -> dict:
    return {"id": row[0], "kind": row[1], "target": row[2], "field": row[3],
            "before": (row[4] or {}).get("v"), "after": (row[5] or {}).get("v"),
            "reason": row[6], "status": row[7],
            "created": row[8].timestamp() if row[8] else 0,
            "decided": row[9].timestamp() if row[9] else 0,
            "decided_by": row[10]}


async def init() -> None:
    async with await _connect() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS proposals (
                id           BIGSERIAL PRIMARY KEY,
                kind         TEXT NOT NULL,
                target       TEXT NOT NULL,
                field        TEXT NOT NULL DEFAULT '',
                before_value JSONB NOT NULL DEFAULT '{}',
                after_value  JSONB NOT NULL DEFAULT '{}',
                reason       TEXT NOT NULL DEFAULT '',
                status       TEXT NOT NULL DEFAULT 'pending',
                created      TIMESTAMPTZ NOT NULL DEFAULT now(),
                decided      TIMESTAMPTZ,
                decided_by   TEXT
            )""")


# ------------------------------------------------------------ what changes ----
# Each kind knows how to read its current value and how to write a new one. Keeping
# both halves together is what makes revert exactly as reliable as apply: the same
# writer puts the old value back.

def _current(kind: str, target: str, field: str):
    import reasoning
    import toolkit
    if kind == "setting":
        if target not in settings.REGISTRY:
            raise HTTPException(400, f"there is no setting called {target!r}")
        return settings.get(target)
    if kind == "persona":
        return settings.get("persona.prompt")
    if kind == "tool":
        if target not in reasoning.TOOLS:
            raise HTTPException(400, f"there is no tool called {target!r}")
        declared = reasoning.TOOLS[target].schema["function"]
        if field == "triggers":
            return ", ".join(toolkit.triggers_for(
                target, tuple(reasoning.TRIGGERS.get(target, ()))))
        return toolkit.description_for(target, declared["description"])
    if kind == "command":
        found = toolkit.custom_command(target) or reasoning.QUICK_COMMANDS.get(target)
        if not found:
            raise HTTPException(400, f"there is no quick command called {target!r}")
        return found["directive"]
    raise HTTPException(400, f"unknown kind {kind!r}; known: {', '.join(KINDS)}")


async def _write(kind: str, target: str, field: str, value) -> None:
    import toolkit
    if kind == "setting":
        await settings.apply({target: value}, actor="iris (approved)")
    elif kind == "persona":
        await settings.apply({"persona.prompt": value}, actor="iris (approved)")
    elif kind == "tool":
        pref = toolkit._prefs.get(target, {})
        declared = _declared_tool(target)
        # Blank clears the override rather than storing an empty instruction, which
        # is what makes reverting to the shipped wording actually restore it.
        description = pref.get("description", "")
        triggers = pref.get("triggers", "")
        if field == "triggers":
            triggers = "" if value == declared["triggers"] else value
        else:
            description = "" if value == declared["description"] else value
        await toolkit._store(target, pref.get("enabled", True), description, triggers,
                             pref.get("parameters") or {})
    elif kind == "command":
        current = toolkit.custom_command(target)
        if not current:
            raise HTTPException(400, "only a custom quick command can be rewritten; "
                                     "a built-in one can be switched off but not "
                                     "reworded")
        await toolkit._store_command({**current, "directive": value})


def _coerce(kind: str, target: str, value):
    """Give the value the type the thing it is going into actually has.

    The tool takes `value` as a string on purpose: an 8B model handles one argument
    type far better than a union, and it will say "7" for a number every time. The
    settings service validates against the registered JSON Schema and rightly refuses
    a string where an integer belongs, so the conversion is this side's job. It
    happens at propose time so the queued diff shows 7 rather than "7".
    """
    if kind != "setting" or target not in settings.REGISTRY:
        return value
    declared = settings.REGISTRY[target].get("type")
    text = str(value).strip()
    try:
        if declared == "integer":
            return int(float(text))
        if declared == "number":
            return float(text)
        if declared == "boolean":
            return text.lower() in ("1", "true", "yes", "on")
    except ValueError:
        raise HTTPException(400, f"{target} takes a {declared}, and {value!r} is not "
                                 f"one")
    return value


def _declared_tool(name: str) -> dict:
    import reasoning
    fn = reasoning.TOOLS[name].schema["function"]
    return {"description": fn["description"],
            "triggers": ", ".join(reasoning.TRIGGERS.get(name, ()))}


# ---------------------------------------------------------------- the tool ----

async def propose(kind: str, target: str, value: str, reason: str,
                  field: str = "") -> str:
    """What the `propose_change` tool calls. Returns what to say back."""
    kind = (kind or "").strip().lower()
    if kind not in KINDS:
        return f"I can only propose changes to: {', '.join(KINDS)}."
    if not str(value).strip():
        return "A proposal needs a new value."
    if not reason.strip():
        return "A proposal needs a reason. It is the only thing the reviewer has."

    before = _current(kind, target, field)
    value = _coerce(kind, target, value)
    if str(before) == str(value):
        return f"That is already what {target} says. Nothing to propose."

    async with await _connect() as conn:
        cur = await conn.execute(
            "SELECT count(*) FROM proposals WHERE status = 'pending'")
        if (await cur.fetchone())[0] >= MAX_PENDING:
            return (f"There are already {MAX_PENDING} proposals waiting to be "
                    f"reviewed. I am not going to add to a queue nobody has read.")
        cur = await conn.execute(
            "INSERT INTO proposals (kind, target, field, before_value, after_value, "
            f"reason) VALUES (%s, %s, %s, %s, %s, %s) RETURNING {_COLUMNS}",
            (kind, target, field, json.dumps({"v": before}), json.dumps({"v": value}),
             reason.strip()))
        row = _row(await cur.fetchone())
    await activity.record("proposal.propose", f"{kind} {target}: {reason[:120]}",
                          "iris")
    return (f"Proposed, not applied: {kind} {target}"
            f"{' (' + field + ')' if field else ''}. It is waiting for you under "
            f"Customize, Self. Nothing changes until you approve it.")


# ------------------------------------------------------------------- queue ----

async def listing(status: str = "") -> list[dict]:
    async with await _connect() as conn:
        if status:
            cur = await conn.execute(
                f"SELECT {_COLUMNS} FROM proposals WHERE status = %s "
                "ORDER BY id DESC LIMIT 200", (status,))
        else:
            cur = await conn.execute(
                f"SELECT {_COLUMNS} FROM proposals ORDER BY id DESC LIMIT 200")
        return [_row(r) for r in await cur.fetchall()]


async def get(ident) -> dict:
    async with await _connect() as conn:
        cur = await conn.execute(
            f"SELECT {_COLUMNS} FROM proposals WHERE id::text = %s", (str(ident),))
        row = await cur.fetchone()
    if not row:
        raise HTTPException(404, f"no proposal {ident!r}")
    return _row(row)


async def _decide(ident, status: str, by: str) -> dict:
    async with await _connect() as conn:
        await conn.execute(
            "UPDATE proposals SET status = %s, decided = now(), decided_by = %s "
            "WHERE id = %s", (status, by, ident))
    return await get(ident)


@router.get("")
async def get_all(status: str = "", _: dict = Depends(auth.active_user)):
    rows = await listing(status)
    return {"proposals": rows,
            "pending": sum(1 for r in rows if r["status"] == "pending")}


@router.post("/{ident}/approve")
async def approve(ident: str,
                  user: dict = Depends(auth.require_role("creator", "admin"))):
    """Apply it, and keep the old value so it can be undone.

    The before-value is re-read here rather than trusted from when the proposal was
    made: something else may have changed it in between, and reverting to a value that
    stopped being current an hour ago would be its own small disaster.
    """
    row = await get(ident)
    if row["status"] != "pending":
        raise HTTPException(409, f"that proposal was already {row['status']}")
    live = _current(row["kind"], row["target"], row["field"])
    async with await _connect() as conn:
        await conn.execute("UPDATE proposals SET before_value = %s WHERE id = %s",
                           (json.dumps({"v": live}), row["id"]))
    await _write(row["kind"], row["target"], row["field"], row["after"])
    await activity.record("proposal.approve",
                          f"{row['kind']} {row['target']}", user["username"])
    return await _decide(row["id"], "approved", user["username"])


@router.post("/{ident}/reject")
async def reject(ident: str,
                 user: dict = Depends(auth.require_role("creator", "admin"))):
    row = await get(ident)
    if row["status"] != "pending":
        raise HTTPException(409, f"that proposal was already {row['status']}")
    await activity.record("proposal.reject",
                          f"{row['kind']} {row['target']}", user["username"])
    return await _decide(row["id"], "rejected", user["username"])


@router.post("/{ident}/revert")
async def revert(ident: str,
                 user: dict = Depends(auth.require_role("creator", "admin"))):
    """One press, back to what it was. This is the whole safety story."""
    row = await get(ident)
    if row["status"] != "approved":
        raise HTTPException(409, "only an approved proposal can be undone")
    await _write(row["kind"], row["target"], row["field"], row["before"])
    await activity.record("proposal.revert",
                          f"{row['kind']} {row['target']}", user["username"])
    return await _decide(row["id"], "reverted", user["username"])


@router.delete("/{ident}")
async def remove(ident: str,
                 user: dict = Depends(auth.require_role("creator", "admin"))):
    row = await get(ident)
    async with await _connect() as conn:
        await conn.execute("DELETE FROM proposals WHERE id = %s", (row["id"],))
    return {"ok": True}
