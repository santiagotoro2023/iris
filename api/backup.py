"""Encrypted backups (SPEC.md Phase 3, §26).

Everything IRiS knows lives in three places, so a backup is all three or it is
worthless: Postgres (users, settings, audit log), Qdrant (memories) and Redis
(conversations). They are pulled over the network from this service, which is the
only one that can already reach all three.

The archive is encrypted with AES-256 before it touches the disk. `./backup/` sits
next to `./data/` in the repo directory and is gitignored: this repo is public
(SPEC.md 4), and an archive of every conversation is the exact thing that must never
be committed.

Scheduling is stateless on purpose. Rather than remembering when it last ran, it
asks the backup directory whether today already has one, so a restart cannot cause a
double run and a machine that was off overnight backs up as soon as it is on.
"""
import asyncio
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException

import activity
import auth
import memory
import settings

BACKUP_DIR = Path(os.environ.get("BACKUP_DIR", "/backup"))
DATABASE_URL = os.environ.get("DATABASE_URL", "")
KEY = os.environ.get("IRIS_BACKUP_KEY", "")
PREFIX = "iris-"
SUFFIX = ".tar.gz.enc"

router = APIRouter(prefix="/backup", tags=["backup"])

settings.setting(
    "backup.enabled", type="boolean", default=True,
    title="Nightly backup",
    description="Encrypt and archive everything IRiS knows once a day: memories, "
                "conversations, settings and accounts.")
settings.setting(
    "backup.at", type="string",
    enum=[f"{h:02d}:00" for h in range(24)], default="03:00",
    title="Backup time",
    description="Local time the daily backup runs. If the machine was off, it runs "
                "at the next opportunity instead of skipping the day.")
settings.setting(
    "backup.keep", type="integer", minimum=1, maximum=365, default=14,
    title="Backups to keep",
    description="Older archives are deleted after a successful new one. Each is "
                "roughly the size of your memories and conversations.")
settings.setting(
    "backup.include_conversations", type="boolean", default=True,
    title="Include conversations",
    description="Back up chat history as well as memories and settings. Turn off if "
                "you would rather transcripts were not archived at all.")


def _stamp() -> str:
    tz = ZoneInfo(settings.get("general.timezone"))
    return datetime.now(tz).strftime("%Y%m%d-%H%M%S")


def _today() -> str:
    tz = ZoneInfo(settings.get("general.timezone"))
    return datetime.now(tz).strftime("%Y%m%d")


def archives() -> list[Path]:
    if not BACKUP_DIR.is_dir():
        return []
    return sorted((p for p in BACKUP_DIR.glob(f"{PREFIX}*{SUFFIX}")), reverse=True)


# ------------------------------------------------------------- collecting ----

def _dump_postgres(into: Path) -> str:
    """Plain SQL, so a restore needs nothing but psql."""
    out = into / "postgres.sql"
    r = subprocess.run(["pg_dump", "--clean", "--if-exists", "--no-owner",
                        "--dbname", DATABASE_URL],
                       capture_output=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError(f"pg_dump: {r.stderr.decode()[:300]}")
    out.write_bytes(r.stdout)
    return f"postgres.sql ({len(r.stdout)} bytes)"


async def _dump_qdrant(into: Path) -> str:
    """Qdrant makes its own snapshots; ask for one and pull it down."""
    base = f"{memory.QDRANT_URL}/collections/{memory.COLLECTION}"
    async with httpx.AsyncClient(timeout=600) as c:
        if (await c.get(base)).status_code != 200:
            return "qdrant: no memories yet"
        r = await c.post(f"{base}/snapshots")
        if r.status_code >= 300:
            raise RuntimeError(f"qdrant snapshot: {r.text[:200]}")
        name = r.json()["result"]["name"]
        data = (await c.get(f"{base}/snapshots/{name}")).content
        (into / "qdrant.snapshot").write_bytes(data)
        # Qdrant keeps every snapshot it has ever made; do not leave them piling up.
        await c.delete(f"{base}/snapshots/{name}")
    return f"qdrant.snapshot ({len(data)} bytes)"


async def _dump_redis(into: Path) -> str:
    """Conversations only. Sessions are deliberately left out: restoring a login
    token from a month ago is a liability, not a feature."""
    r = aioredis.from_url(auth.REDIS_URL, decode_responses=True)
    try:
        out: dict[str, object] = {}
        async for key in r.scan_iter("chat:*"):
            out[key] = (await r.hgetall(key) if await r.type(key) == "hash"
                        else await r.get(key))
    finally:
        await r.aclose()
    (into / "redis-chat.json").write_text(json.dumps(out))
    return f"redis-chat.json ({len(out)} keys)"


# -------------------------------------------------------------- producing ----

def _encrypt(tar_bytes: bytes, target: Path) -> None:
    """AES-256-CBC with PBKDF2. openssl is already a hard requirement of setup.sh
    for generating the database password, so this adds no dependency and a restore
    needs no tooling that is not on every Linux box."""
    if not KEY:
        raise RuntimeError("IRIS_BACKUP_KEY is not set; re-run ./setup.sh")
    r = subprocess.run(
        ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-salt",
         "-pass", "env:IRIS_BACKUP_KEY", "-out", str(target)],
        input=tar_bytes, capture_output=True, env={**os.environ}, timeout=600)
    if r.returncode != 0:
        target.unlink(missing_ok=True)
        raise RuntimeError(f"openssl: {r.stderr.decode()[:300]}")


async def run_backup(actor: str = "schedule") -> dict:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    contents: list[str] = []

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        contents.append(await asyncio.to_thread(_dump_postgres, work))
        contents.append(await _dump_qdrant(work))
        if settings.get("backup.include_conversations"):
            contents.append(await _dump_redis(work))

        (work / "manifest.json").write_text(json.dumps({
            "created": datetime.now(ZoneInfo(settings.get("general.timezone"))).isoformat(),
            "contents": contents,
            "embed_model": settings.get("memory.embed_model"),
            "restore": "./restore.sh <this file>",
        }, indent=2))

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for item in sorted(work.iterdir()):
                tar.add(item, arcname=item.name)
        target = BACKUP_DIR / f"{PREFIX}{_stamp()}{SUFFIX}"
        await asyncio.to_thread(_encrypt, buf.getvalue(), target)

    # Prune only after a good one exists, so a failing backup never eats the history.
    keep = settings.get("backup.keep")
    removed = 0
    for old in archives()[keep:]:
        old.unlink(missing_ok=True)
        removed += 1

    result = {"file": target.name, "bytes": target.stat().st_size,
              "seconds": round(time.monotonic() - started, 1),
              "contents": contents, "pruned": removed}
    await activity.record("backup.run",
                          f"{target.name}, {result['bytes'] // 1024} KiB", actor)
    return result


async def scheduler() -> None:
    """Checked every minute rather than slept until, so changing the time in the UI
    takes effect immediately instead of at the next firing."""
    while True:
        try:
            if settings.get("backup.enabled") and KEY:
                now = datetime.now(ZoneInfo(settings.get("general.timezone")))
                due = now.strftime("%H:%M") >= settings.get("backup.at")
                have_today = any(p.name.startswith(f"{PREFIX}{_today()}")
                                 for p in archives())
                if due and not have_today:
                    print(f"[backup] running daily backup", flush=True)
                    await run_backup()
        except Exception as e:
            print(f"[backup] failed: {e}", flush=True)
        await asyncio.sleep(60)


# ------------------------------------------------------------------- http ----

@router.get("")
async def listing(_: dict = Depends(auth.require_role("creator", "admin"))):
    free = shutil.disk_usage(BACKUP_DIR).free if BACKUP_DIR.is_dir() else 0
    return {
        "configured": bool(KEY),
        "free_bytes": free,
        "backups": [{"name": p.name, "bytes": p.stat().st_size,
                     "created": p.stat().st_mtime} for p in archives()],
    }


@router.post("/run")
async def run_now(user: dict = Depends(auth.require_role("creator", "admin"))):
    try:
        return await run_backup(user["username"])
    except Exception as e:
        raise HTTPException(500, str(e))


@router.delete("/{name}")
async def delete(name: str,
                 user: dict = Depends(auth.require_role("creator", "admin"))):
    # Never let a crafted name walk out of the backup directory.
    target = BACKUP_DIR / Path(name).name
    if not target.name.startswith(PREFIX) or not target.name.endswith(SUFFIX):
        raise HTTPException(400, "not a backup file")
    target.unlink(missing_ok=True)
    await activity.record("backup.delete", target.name, user["username"])
    return {"ok": True}
