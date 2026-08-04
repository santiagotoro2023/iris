"""Auth, users, sessions and API keys (SPEC.md Phase 1C).

Passwords are hashed with stdlib scrypt. Sessions live in Redis so all three
frontend surfaces share one token and Phase 5 can hand a conversation from
phone to desktop mid-sentence. API keys are issued here for Phase 6's inbound
webhook layer.
"""
import hashlib
import hmac
import os
import secrets
from datetime import datetime, timezone

import psycopg
import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from starlette.requests import HTTPConnection

import activity
import settings

DATABASE_URL = os.environ.get("DATABASE_URL", "")
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379")
# Tailscale carries traffic over its own encrypted tunnel and serves plain HTTP,
# so Secure would break login there. Set to 1 when a real TLS terminator is in front.
COOKIE_SECURE = os.environ.get("IRIS_COOKIE_SECURE", "0") == "1"
COOKIE_NAME = "iris_session"

SEED_USERNAME = "creator"
SEED_PASSWORD = "1234"          # forced to change on first login (SPEC.md 4)
ROLES = ["creator", "admin", "user"]

MAX_FAILED_LOGINS = 10
LOCKOUT_SECONDS = 900

router = APIRouter(prefix="/auth", tags=["auth"])
_redis: aioredis.Redis | None = None

settings.setting(
    "auth.session_hours", type="integer", minimum=1, maximum=8760, default=720,
    title="Session length (hours)",
    description="How long a login stays valid before it must be repeated.")


# ------------------------------------------------------------- passwords ----

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1,
                        dklen=32, maxmem=64 * 1024 * 1024)
    return f"scrypt${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_hex, dk_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex),
                            n=2**14, r=8, p=1, dklen=32, maxmem=64 * 1024 * 1024)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk.hex(), dk_hex)


def _hash_key(raw: str) -> str:
    """API keys are high-entropy random strings, so a fast digest is appropriate."""
    return hashlib.sha256(raw.encode()).hexdigest()


# --------------------------------------------------------------- storage ----

async def _connect():
    return await psycopg.AsyncConnection.connect(DATABASE_URL, autocommit=True)


async def init() -> None:
    global _redis
    _redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    async with await _connect() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id            SERIAL PRIMARY KEY,
                username      TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role          TEXT NOT NULL DEFAULT 'user',
                must_change   BOOLEAN NOT NULL DEFAULT FALSE,
                created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
            )""")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS api_keys (
                id           SERIAL PRIMARY KEY,
                label        TEXT NOT NULL,
                key_hash     TEXT NOT NULL UNIQUE,
                created_by   INTEGER REFERENCES users(id) ON DELETE CASCADE,
                created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
                last_used_at TIMESTAMPTZ
            )""")
        cur = await conn.execute("SELECT count(*) FROM users")
        (count,) = await cur.fetchone()
        if not count:
            await conn.execute(
                "INSERT INTO users (username, password_hash, role, must_change) "
                "VALUES (%s, %s, 'creator', TRUE)",
                (SEED_USERNAME, hash_password(SEED_PASSWORD)))


async def _fetch_user(where: str, param) -> dict | None:
    async with await _connect() as conn:
        cur = await conn.execute(
            f"SELECT id, username, password_hash, role, must_change FROM users WHERE {where}",
            (param,))
        row = await cur.fetchone()
    if not row:
        return None
    return dict(zip(("id", "username", "password_hash", "role", "must_change"), row))


# -------------------------------------------------------------- sessions ----

async def _new_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    ttl = settings.get("auth.session_hours") * 3600
    await _redis.setex(f"session:{token}", ttl, str(user_id))
    return token


async def _session_user(token: str) -> dict | None:
    user_id = await _redis.get(f"session:{token}")
    return await _fetch_user("id = %s", int(user_id)) if user_id else None


def _token_from(conn: HTTPConnection) -> str | None:
    """Cookie for the web UI, bearer for the native app — one session store either way.

    Typed as HTTPConnection, not Request, so the same gate works on a WebSocket:
    cookies and headers are defined on the shared base, and FastAPI fills an
    HTTPConnection parameter on both scopes. A browser cannot set an Authorization
    header on a WebSocket, so hands-free listening authenticates by cookie.
    """
    token = conn.cookies.get(COOKIE_NAME)
    if token:
        return token
    header = conn.headers.get("authorization", "")
    return header[7:] if header.lower().startswith("bearer ") else None


async def current_user(conn: HTTPConnection) -> dict:
    token = _token_from(conn)
    user = await _session_user(token) if token else None
    if not user:
        raise HTTPException(401, "not authenticated")
    return user


async def active_user(user: dict = Depends(current_user)) -> dict:
    """A user who still owes a password change may only change their password."""
    if user["must_change"]:
        raise HTTPException(403, "password change required")
    return user


def require_role(*allowed: str):
    async def dep(user: dict = Depends(active_user)) -> dict:
        if user["role"] not in allowed:
            raise HTTPException(403, f"requires role: {' or '.join(allowed)}")
        return user
    return dep


async def verify_api_key(raw: str) -> dict | None:
    """For Phase 6's inbound webhooks. Records use so stale keys are visible."""
    async with await _connect() as conn:
        cur = await conn.execute(
            "UPDATE api_keys SET last_used_at = now() WHERE key_hash = %s RETURNING created_by",
            (_hash_key(raw),))
        row = await cur.fetchone()
    return await _fetch_user("id = %s", row[0]) if row else None


# ----------------------------------------------------------------- views ----

class Credentials(BaseModel):
    username: str
    password: str


class PasswordChange(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8)


class NewUser(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=8)
    role: str = "user"


def _public(user: dict) -> dict:
    return {"id": user["id"], "username": user["username"],
            "role": user["role"], "must_change_password": user["must_change"]}


@router.post("/login")
async def login(body: Credentials, response: Response):
    fail_key = f"login_fail:{body.username.lower()}"
    # ponytail: per-account lockout, so a known username can be locked out on purpose
    # for LOCKOUT_SECONDS. Acceptable while the only route in is Tailscale; switch to
    # per-IP throttling or a proof-of-work delay if IRiS is ever exposed more widely.
    if int(await _redis.get(fail_key) or 0) >= MAX_FAILED_LOGINS:
        raise HTTPException(429, "too many failed attempts, try again later")

    user = await _fetch_user("lower(username) = lower(%s)", body.username)
    if not user or not verify_password(body.password, user["password_hash"]):
        # One counter per account, so guessing "1234" cannot run unbounded.
        await _redis.incr(fail_key)
        await _redis.expire(fail_key, LOCKOUT_SECONDS)
        raise HTTPException(401, "invalid username or password")

    await _redis.delete(fail_key)
    await activity.record("auth.login", f"role {user['role']}", user["username"])
    token = await _new_session(user["id"])
    response.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax",
                        secure=COOKIE_SECURE,
                        max_age=settings.get("auth.session_hours") * 3600)
    return {"token": token, "user": _public(user)}


@router.post("/logout")
async def logout(request: Request, response: Response):
    token = _token_from(request)
    if token:
        await _redis.delete(f"session:{token}")
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}


@router.get("/me")
async def me(user: dict = Depends(current_user)):
    return _public(user)


@router.post("/password")
async def change_password(body: PasswordChange, request: Request,
                          user: dict = Depends(current_user)):
    if not verify_password(body.current_password, user["password_hash"]):
        raise HTTPException(401, "current password is incorrect")
    if verify_password(body.new_password, user["password_hash"]):
        raise HTTPException(400, "new password must differ from the current one")

    async with await _connect() as conn:
        await conn.execute(
            "UPDATE users SET password_hash = %s, must_change = FALSE WHERE id = %s",
            (hash_password(body.new_password), user["id"]))

    # Drop every other session for this user; a password change should evict
    # anyone who may already be holding a token.
    keep = _token_from(request)
    async for key in _redis.scan_iter("session:*"):
        if key.split(":", 1)[1] != keep and await _redis.get(key) == str(user["id"]):
            await _redis.delete(key)
    return {"ok": True}


@router.get("/users")
async def list_users(_: dict = Depends(require_role("creator", "admin"))):
    async with await _connect() as conn:
        cur = await conn.execute(
            "SELECT id, username, role, must_change, created_at FROM users ORDER BY id")
        rows = await cur.fetchall()
    return [{"id": i, "username": u, "role": r, "must_change_password": m,
             "created_at": c.isoformat()} for i, u, r, m, c in rows]


@router.post("/users")
async def create_user(body: NewUser, _: dict = Depends(require_role("creator", "admin"))):
    if body.role not in ROLES:
        raise HTTPException(400, f"role must be one of {ROLES}")
    try:
        async with await _connect() as conn:
            cur = await conn.execute(
                "INSERT INTO users (username, password_hash, role, must_change) "
                "VALUES (%s, %s, %s, TRUE) RETURNING id",
                (body.username, hash_password(body.password), body.role))
            (new_id,) = await cur.fetchone()
    except psycopg.errors.UniqueViolation:
        raise HTTPException(409, "username already exists")
    return {"id": new_id, "username": body.username, "role": body.role}


@router.delete("/users/{user_id}")
async def delete_user(user_id: int, actor: dict = Depends(require_role("creator", "admin"))):
    if user_id == actor["id"]:
        raise HTTPException(400, "you cannot delete your own account")
    async with await _connect() as conn:
        cur = await conn.execute(
            "DELETE FROM users WHERE id = %s AND role <> 'creator' RETURNING id", (user_id,))
        if not await cur.fetchone():
            raise HTTPException(404, "no such user, or the creator account")
    return {"ok": True}


@router.get("/apikeys")
async def list_api_keys(user: dict = Depends(require_role("creator", "admin"))):
    async with await _connect() as conn:
        cur = await conn.execute(
            "SELECT id, label, created_at, last_used_at FROM api_keys ORDER BY id")
        rows = await cur.fetchall()
    return [{"id": i, "label": l, "created_at": c.isoformat(),
             "last_used_at": u.isoformat() if u else None} for i, l, c, u in rows]


@router.post("/apikeys")
async def create_api_key(body: dict, user: dict = Depends(require_role("creator", "admin"))):
    label = (body.get("label") or "").strip()
    if not label:
        raise HTTPException(400, "label is required")
    raw = "iris_" + secrets.token_urlsafe(32)
    async with await _connect() as conn:
        cur = await conn.execute(
            "INSERT INTO api_keys (label, key_hash, created_by) VALUES (%s, %s, %s) RETURNING id",
            (label, _hash_key(raw), user["id"]))
        (key_id,) = await cur.fetchone()
    # Only ever returned here; the database stores a digest, so a lost key is regenerated.
    return {"id": key_id, "label": label, "key": raw,
            "note": "Copy this now — it is not shown again."}


@router.delete("/apikeys/{key_id}")
async def delete_api_key(key_id: int, _: dict = Depends(require_role("creator", "admin"))):
    async with await _connect() as conn:
        cur = await conn.execute("DELETE FROM api_keys WHERE id = %s RETURNING id", (key_id,))
        if not await cur.fetchone():
            raise HTTPException(404, "no such API key")
    return {"ok": True}
