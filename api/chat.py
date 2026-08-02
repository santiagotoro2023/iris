"""Conversations, stored server-side per user.

State lives in Redis rather than the browser so Phase 5 can hand a conversation
from phone to desktop mid-sentence (SPEC.md Phase 5).
"""
import json
import time
import uuid

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

import activity
import auth
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
    history.append({"role": "user", "content": body.content})
    title = None if body.conversation_id else body.content.strip()[:60]

    async def events():
        yield json.dumps({"type": "start", "conversation_id": cid}) + "\n"
        final = None
        try:
            async for event in reasoning.stream(history, think=body.think):
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

    return StreamingResponse(events(), media_type="application/x-ndjson",
                             headers={"cache-control": "no-cache",
                                      "x-accel-buffering": "no"})


@router.post("/message")
async def send(body: Send, user: dict = Depends(auth.active_user)):
    uid = user["id"]
    cid = body.conversation_id or uuid.uuid4().hex
    history = await _load(uid, cid) if body.conversation_id else []

    history.append({"role": "user", "content": body.content})
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
