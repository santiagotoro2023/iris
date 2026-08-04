"""Activity & audit log (SPEC.md 3.2).

IRiS records what it did, when and why, and shows it to the user rather than
burying it. Every module reports here; this is also how "why did you do that"
gets a real answer in Phase 8.
"""
import os

import psycopg
from fastapi import APIRouter, Depends

DATABASE_URL = os.environ.get("DATABASE_URL", "")
router = APIRouter(prefix="/activity", tags=["activity"])


async def _connect():
    return await psycopg.AsyncConnection.connect(DATABASE_URL, autocommit=True)


async def init() -> None:
    async with await _connect() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS activity (
                id      BIGSERIAL PRIMARY KEY,
                at      TIMESTAMPTZ NOT NULL DEFAULT now(),
                actor   TEXT,
                action  TEXT NOT NULL,
                detail  TEXT
            )""")
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS activity_at_idx ON activity (at DESC)")


async def record(action: str, detail: str = "", actor: str | None = None) -> None:
    """Never let logging break the thing being logged."""
    try:
        async with await _connect() as conn:
            await conn.execute(
                "INSERT INTO activity (actor, action, detail) VALUES (%s, %s, %s)",
                (actor, action, detail[:2000]))
    except Exception:
        pass


@router.delete("")
async def clear(days: int = 0):
    """Wipe the audit log. `days` keeps anything more recent than that."""
    async with await _connect() as conn:
        async with conn.cursor() as cur:
            if days > 0:
                await cur.execute("DELETE FROM activity WHERE at < now() - "
                                  "make_interval(days => %s)", (days,))
            else:
                await cur.execute("DELETE FROM activity")
            removed = cur.rowcount
    await record("activity.clear", f"{removed} entries removed")
    return {"removed": removed}


@router.get("")
async def recent(limit: int = 100):
    limit = max(1, min(limit, 500))
    async with await _connect() as conn:
        cur = await conn.execute(
            "SELECT at, actor, action, detail FROM activity ORDER BY id DESC LIMIT %s",
            (limit,))
        rows = await cur.fetchall()
    return [{"at": at.isoformat(), "actor": actor, "action": action, "detail": detail}
            for at, actor, action, detail in rows]
