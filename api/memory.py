"""Memory (SPEC.md Phase 3).

Facts worth keeping are embedded with bge-m3 and stored in Qdrant. Two things use
them, and the second is what actually makes memory work:

  1. A `remember` tool, so IRiS can deliberately store something it just learned.
  2. Automatic recall: every user turn is searched against the store and anything
     relevant is folded into the system turn before the model sees the question.
     Relying on the model to *decide* to search would mean it usually does not.

Qdrant speaks plain HTTP and httpx is already here, so there is no client library.
"""
import os
import time
import uuid

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import activity
import auth
import settings

QDRANT_URL = os.environ.get("QDRANT_URL", "http://qdrant:6333")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama:11434")
COLLECTION = "memories"
MAX_TEXT = 2000

router = APIRouter(prefix="/memory", tags=["memory"])

_dim: int | None = None


def _embed_models() -> list[str]:
    """Embedding models actually pulled, so the dropdown only offers usable ones."""
    import main
    known = {os.environ.get("IRIS_EMBED_MODEL", "bge-m3")}
    try:
        known.update(m for m in main._installed_models()
                     if any(k in m for k in ("embed", "bge", "minilm", "nomic")))
    except Exception:
        pass
    if "memory.embed_model" in settings.REGISTRY:
        known.add(settings.get("memory.embed_model"))
    return sorted(known)


settings.setting(
    "memory.enabled", type="boolean", default=True,
    title="Memory",
    description="Remember things between conversations. Off makes every conversation "
                "start from nothing.")
settings.setting(
    "memory.embed_model", type="string", enum=_embed_models, default=os.environ.get("IRIS_EMBED_MODEL", "bge-m3"),
    title="Embedding model",
    description="Turns text into the vector memories are searched by. bge-m3 is "
                "multilingual, which matters for switching between English and German. "
                "Changing this makes existing memories unsearchable until they are "
                "rebuilt, because the vectors are a different shape.")
settings.setting(
    "memory.recall_count", type="integer", minimum=1, maximum=20, default=5,
    title="Memories to recall",
    description="How many relevant memories are put in front of IRiS on each turn. "
                "More context costs speed and can crowd out the actual question.")
settings.setting(
    "memory.min_score", type="number", minimum=0.2, maximum=0.95, default=0.42,
    title="Recall threshold",
    description="How close a memory must be to the question before it is recalled. "
                "Measured with bge-m3 on full-sentence questions: genuine matches "
                "score down to 0.43 and unrelated ones up to 0.37. The bands are "
                "close, so this is worth tuning by eye once there are real memories.")
settings.setting(
    "memory.dedup_score", type="number", minimum=0.8, maximum=1.0, default=0.93,
    title="Duplicate threshold",
    description="A new memory this similar to an existing one replaces it instead of "
                "piling up near-identical copies.")


async def embed(texts: list[str]) -> list[list[float]]:
    model = settings.get("memory.embed_model")
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post(f"{OLLAMA_URL}/api/embed",
                         json={"model": model, "input": texts})
    if r.status_code != 200:
        raise HTTPException(502, f"embedding failed ({model}): {r.text[:200]}")
    return r.json()["embeddings"]


async def _ensure_collection(dim: int) -> None:
    """Created on first use rather than at startup, so a cold Qdrant or a missing
    embedding model cannot stop the API from booting."""
    global _dim
    if _dim == dim:
        return
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{QDRANT_URL}/collections/{COLLECTION}")
        if r.status_code == 200:
            have = r.json()["result"]["config"]["params"]["vectors"]["size"]
            if have != dim:
                raise HTTPException(
                    500, f"stored memories are {have}-dimensional but "
                         f"{settings.get('memory.embed_model')} produces {dim}. "
                         f"Switch the embedding model back, or clear the memories.")
        else:
            await c.put(f"{QDRANT_URL}/collections/{COLLECTION}",
                        json={"vectors": {"size": dim, "distance": "Cosine"}})
    _dim = dim


async def remember(text: str, user_id: int, kind: str = "fact",
                   source: str = "chat") -> dict:
    """Store one durable fact, replacing a near-identical one rather than stacking
    another copy of it."""
    text = " ".join(text.split())[:MAX_TEXT]
    if not text:
        raise HTTPException(400, "empty memory")
    vector = (await embed([text]))[0]
    await _ensure_collection(len(vector))

    existing = await _search(vector, user_id, limit=1,
                            min_score=settings.get("memory.dedup_score"))
    point_id = existing[0]["id"] if existing else str(uuid.uuid4())

    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.put(
            f"{QDRANT_URL}/collections/{COLLECTION}/points",
            params={"wait": "true"},
            json={"points": [{"id": point_id, "vector": vector,
                              "payload": {"text": text, "kind": kind,
                                          "source": source, "user_id": user_id,
                                          "created": time.time()}}]})
    if r.status_code >= 300:
        raise HTTPException(502, f"qdrant: {r.text[:200]}")
    return {"id": point_id, "text": text, "replaced": bool(existing)}


async def _search(vector: list[float], user_id: int, limit: int,
                  min_score: float) -> list[dict]:
    body = {"vector": vector, "limit": limit, "with_payload": True,
            "score_threshold": min_score,
            "filter": {"must": [{"key": "user_id", "match": {"value": user_id}}]}}
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"{QDRANT_URL}/collections/{COLLECTION}/points/search",
                         json=body)
    if r.status_code == 404:
        return []                                  # nothing remembered yet
    if r.status_code >= 300:
        raise HTTPException(502, f"qdrant: {r.text[:200]}")
    return [{"id": p["id"], "score": round(p["score"], 3), **p["payload"]}
            for p in r.json()["result"]]


async def recall(query: str, user_id: int, limit: int | None = None,
                 min_score: float | None = None) -> list[dict]:
    if not settings.get("memory.enabled") or not query.strip():
        return []
    vector = (await embed([query]))[0]
    try:
        await _ensure_collection(len(vector))
        return await _search(vector, user_id,
                             limit or settings.get("memory.recall_count"),
                             settings.get("memory.min_score")
                             if min_score is None else min_score)
    except HTTPException:
        raise
    except Exception:
        return []          # memory is an enhancement; never break a reply over it


# bge-m3 scores a two-word fragment against almost anything at ~0.44, well inside
# the range a real match occupies, so "the weather" would drag in every memory
# stored. Short turns are also the ones least likely to need recalling anything.
MIN_RECALL_WORDS = 3


async def context_for(query: str, user_id: int) -> str:
    """The block folded into the system turn. Empty when nothing is relevant, so an
    ordinary question is not padded with noise."""
    if len(query.split()) < MIN_RECALL_WORDS:
        return ""
    try:
        hits = await recall(query, user_id)
    except Exception:
        return ""
    if not hits:
        return ""
    lines = "\n".join(f"- {h['text']}" for h in hits)
    return ("WHAT YOU ALREADY KNOW\n"
            "Things you have remembered about them. Use them when they are relevant "
            "and do not announce that you are consulting your memory.\n" + lines)


async def forget(point_id: str) -> None:
    async with httpx.AsyncClient(timeout=30) as c:
        await c.post(f"{QDRANT_URL}/collections/{COLLECTION}/points/delete",
                     params={"wait": "true"}, json={"points": [point_id]})


async def listing(user_id: int, limit: int = 200) -> list[dict]:
    """Everything remembered, newest first — the Memory tab (SPEC.md 14)."""
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"{QDRANT_URL}/collections/{COLLECTION}/points/scroll",
                         json={"limit": limit, "with_payload": True,
                               "filter": {"must": [{"key": "user_id",
                                                    "match": {"value": user_id}}]}})
    if r.status_code >= 300:
        return []
    points = [{"id": p["id"], **p["payload"]} for p in r.json()["result"]["points"]]
    return sorted(points, key=lambda p: p.get("created", 0), reverse=True)


# ------------------------------------------------------------------- http ----

class NewMemory(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_TEXT)
    kind: str = "fact"


@router.get("")
async def get_all(q: str = "", user: dict = Depends(auth.active_user)):
    if q:
        return {"memories": await recall(q, user["id"], limit=50, min_score=0.2)}
    return {"memories": await listing(user["id"])}


@router.post("")
async def add(body: NewMemory, user: dict = Depends(auth.active_user)):
    out = await remember(body.text, user["id"], body.kind, source="manual")
    await activity.record("memory.remember", body.text[:120], user["username"])
    return out


@router.delete("/{point_id}")
async def delete(point_id: str, user: dict = Depends(auth.active_user)):
    await forget(point_id)
    await activity.record("memory.forget", point_id, user["username"])
    return {"ok": True}
