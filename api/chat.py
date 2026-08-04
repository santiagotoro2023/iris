"""Conversations, stored server-side per user.

State lives in Redis rather than the browser so Phase 5 can hand a conversation
from phone to desktop mid-sentence (SPEC.md Phase 5).
"""
import asyncio
import json
import time
import uuid

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

import activity
import auth
import memory
import reasoning

router = APIRouter(prefix="/chat", tags=["chat"])
_redis: aioredis.Redis | None = None

MAX_CONVERSATIONS = 200


async def init() -> None:
    global _redis
    _redis = aioredis.from_url(auth.REDIS_URL, decode_responses=True)


def _msgs_key(uid: int, cid: str) -> str:
    return f"chat:{uid}:msgs:{cid}"


def _index_key(uid: int) -> str:
    return f"chat:{uid}:index"


class Send(BaseModel):
    content: str = Field(min_length=1)
    conversation_id: str | None = None
    think: bool | None = None
    # A quick command selected in the composer. Steers the turn; the directive is
    # never stored, so the transcript reads as what the user actually typed.
    command: str | None = None
    # [{name, kind, text}] from /files/analyze. Stored alongside the message so the
    # model still has the file on later turns, and the UI can show it collapsed.
    attachments: list[dict] | None = None


def _user_message(body: "Send") -> dict:
    msg = {"role": "user", "content": body.content}
    if body.command:
        # Recorded, not applied: the transcript keeps what was typed, and the chat
        # shows underneath it what context the message carried.
        msg["command"] = body.command
    if body.attachments:
        msg["attachments"] = [
            {"name": a.get("name", "file"), "kind": a.get("kind", "text"),
             "text": str(a.get("text", ""))[:40_000]}
            for a in body.attachments]
    return msg


async def _load(uid: int, cid: str) -> list[dict]:
    raw = await _redis.get(_msgs_key(uid, cid))
    return json.loads(raw) if raw else []


async def _save(uid: int, cid: str, messages: list[dict], title: str | None = None) -> None:
    await _redis.set(_msgs_key(uid, cid), json.dumps(messages))
    meta = await _redis.hget(_index_key(uid), cid)
    meta = json.loads(meta) if meta else {"id": cid, "title": title or "New conversation"}
    if title:
        meta["title"] = title
    meta["updated"] = time.time()
    await _redis.hset(_index_key(uid), cid, json.dumps(meta))


async def compact(days: int) -> dict:
    """Drop raw transcripts past the retention window (SPEC.md 6, 30-day rolling).

    Only the transcript is raw. Anything durable in it was already distilled into a
    memory when the exchange happened, so what expires here is the verbatim record,
    not what IRiS learned. 0 keeps everything forever.
    """
    if days <= 0:
        return {"removed": 0, "kept": 0}
    cutoff = time.time() - days * 86400
    removed = kept = 0
    async for index in _redis.scan_iter("chat:*:index"):
        uid = index.split(":")[1]
        for cid, raw in (await _redis.hgetall(index)).items():
            try:
                updated = json.loads(raw).get("updated", 0)
            except ValueError:
                updated = 0
            # A conversation with no timestamp predates that field; treat it as old
            # rather than immortal, or it can never be compacted.
            if updated and updated >= cutoff:
                kept += 1
                continue
            await _redis.delete(_msgs_key(int(uid), cid))
            await _redis.hdel(index, cid)
            removed += 1
    return {"removed": removed, "kept": kept}


@router.get("/conversations")
async def conversations(user: dict = Depends(auth.active_user)):
    raw = await _redis.hgetall(_index_key(user["id"]))
    items = [json.loads(v) for v in raw.values()]
    items.sort(key=lambda m: m.get("updated", 0), reverse=True)
    return items[:MAX_CONVERSATIONS]


@router.get("/conversations/{cid}")
async def conversation(cid: str, user: dict = Depends(auth.active_user)):
    messages = await _load(user["id"], cid)
    if not messages:
        raise HTTPException(404, "no such conversation")
    return {"id": cid, "messages": messages}


@router.delete("/conversations/{cid}")
async def delete_conversation(cid: str, user: dict = Depends(auth.active_user)):
    await _redis.delete(_msgs_key(user["id"], cid))
    await _redis.hdel(_index_key(user["id"]), cid)
    await activity.record("chat.delete", f"conversation {cid[:8]}", user["username"])
    return {"ok": True}


@router.post("/stream")
async def stream_message(body: Send, user: dict = Depends(auth.active_user)):
    """Newline-delimited JSON, one event per line, so the client can render text as it
    arrives instead of waiting for the whole reply (SPEC.md 16)."""
    uid = user["id"]
    cid = body.conversation_id or uuid.uuid4().hex
    history = await _load(uid, cid) if body.conversation_id else []
    history.append(_user_message(body))
    title = None if body.conversation_id else body.content.strip()[:60]
    if body.command:
        # Applied to the copy the model sees, not to what is stored.
        history = history[:-1] + [{**history[-1],
                                   "content": reasoning.apply_command(body.command,
                                                                      body.content)}]

    async def events():
        yield json.dumps({"type": "start", "conversation_id": cid}) + "\n"
        final = None
        try:
            async for event in reasoning.stream(history, think=body.think,
                                                user_id=uid):
                if event["type"] == "done":
                    final = event
                yield json.dumps(event) + "\n"
        except Exception as e:
            yield json.dumps({"type": "error", "detail": str(e)}) + "\n"

        if final:
            # Persist only once the turn completed, so an aborted stream cannot
            # leave a half-written conversation behind.
            await _save(uid, cid, final["messages"], title)
            used = [m["tool_name"] for m in final["messages"] if m.get("role") == "tool"]
            await activity.record(
                "chat.message",
                f"{len(body.content)} chars, streamed"
                + (f", tools: {', '.join(used)}" if used else ""),
                user["username"])
            # Detached: learning from the exchange is a second model call, and the
            # user is already reading the reply. It must never delay or break it.
            asyncio.create_task(memory.capture(final["messages"][-2:], uid))

    return StreamingResponse(events(), media_type="application/x-ndjson",
                             headers={"cache-control": "no-cache",
                                      "x-accel-buffering": "no"})


@router.post("/message")
async def send(body: Send, user: dict = Depends(auth.active_user)):
    uid = user["id"]
    cid = body.conversation_id or uuid.uuid4().hex
    history = await _load(uid, cid) if body.conversation_id else []

    history.append(_user_message(body))
    result = await reasoning.run(history, think=body.think)

    # run() returns the full transcript including any tool turns, so the stored
    # conversation shows what IRiS actually did, not just what it said.
    messages = result["messages"]
    title = None if body.conversation_id else body.content.strip()[:60]
    await _save(uid, cid, messages, title)

    used = [m["tool_name"] for m in messages if m.get("role") == "tool"]
    await activity.record(
        "chat.message",
        f"{len(body.content)} chars" + (f", tools: {', '.join(used)}" if used else ""),
        user["username"])

    return {"conversation_id": cid, "message": result["message"], "messages": messages}
