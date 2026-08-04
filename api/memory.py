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
import re
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
    "memory.auto_capture", type="boolean", default=True,
    title="Learn from conversations",
    description="After each exchange, quietly pick out anything durable worth keeping "
                "and remember it. Without this, memory only fills when IRiS thinks to "
                "store something, which it often does not.")
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


# --------------------------------------------------------------- capture ----

CAPTURE_SYSTEM = """You extract durable facts for a personal assistant's long-term \
memory. You are not talking to anyone; you only produce a list.

Output ONE fact per line, each a standalone sentence that will still make sense in six \
months without the surrounding conversation. Write them in the third person about the \
user. At most 3 lines. No numbering, no bullets, no commentary.

STORE ONLY: stable preferences, personal or biographical details, their hardware or \
software setup, decisions they have made, projects they are working on, people and \
places that recur in their life.

NEVER STORE: questions they asked, facts you looked up for them, general knowledge, \
anything about yourself, or passing conversational filler.

Record only what was actually said. Do not infer, generalise or embellish: "they are \
adjusting to a new routine" is not a fact if nobody said it. Prefer one solid line to \
three padded ones.

If nothing in the exchange is durable, reply with exactly: NONE"""

MIN_CAPTURE_CHARS = 40


async def _complete(system: str, user: str) -> str:
    """A plain, tool-free, persona-free completion. Extraction is a different job
    from being IRiS, and giving it the persona made it answer in character."""
    async with httpx.AsyncClient(timeout=180) as c:
        r = await c.post(f"{OLLAMA_URL}/api/chat", json={
            "model": settings.get("llm.model"),
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "stream": False, "think": False,
            "options": {"temperature": 0}})
    if r.status_code != 200:
        raise RuntimeError(f"ollama: {r.text[:200]}")
    return (r.json().get("message") or {}).get("content", "")


# Words that carry no evidence either way, so their presence must not vouch for a
# sentence and their absence must not condemn it.
_FILLER = {"user", "they", "their", "them", "then", "there", "that", "this", "with",
           "have", "been", "will", "would", "about", "into", "from", "when", "what",
           "which", "these", "those", "also", "just", "very", "some", "more", "than",
           "does", "prefers", "wants", "uses", "said", "says", "always", "never"}


def _grounded(fact: str, source: str) -> bool:
    """Reject facts the conversation does not support.

    An 8B model pads a list no matter how the prompt is worded: asked about a move
    to Winterthur it also produced "they are adjusting to a new daily routine",
    which nobody said. Arguing with the prompt did not fix it; requiring the
    distinctive words to actually appear does, and it cannot be talked out of.
    """
    words = {w for w in re.findall(r"[a-z]{4,}", fact.lower()) if w not in _FILLER}
    if not words:
        return False
    have = set(re.findall(r"[a-z]{4,}", source.lower()))
    return sum(w in have for w in words) / len(words) >= 0.5


def _usable(line: str) -> bool:
    line = line.strip(" -*\u2022\t")
    if len(line) < 12 or line.upper().startswith("NONE"):
        return False
    # An 8B model reliably narrates ("Here are the facts:") no matter what it is told.
    return not line.rstrip().endswith(":")


async def capture(exchange: list[dict], user_id: int) -> list[str]:
    """Learn from one user/assistant exchange. Never raises: this runs detached from
    the reply and a failure here must not surface as a broken conversation."""
    try:
        if not settings.get("memory.enabled") or not settings.get("memory.auto_capture"):
            return []
        text = "\n".join(f"{m['role']}: {m.get('content') or ''}"
                          for m in exchange if m.get("role") in ("user", "assistant"))
        if len(text) < MIN_CAPTURE_CHARS:
            return []
        reply = await _complete(CAPTURE_SYSTEM, text[:6000])
        facts = [ln.strip(" -*\u2022\t") for ln in reply.splitlines() if _usable(ln)]
        facts = [f for f in facts if _grounded(f, text)][:3]
        stored = []
        for fact in facts:
            await remember(fact, user_id, source="learned")
            stored.append(fact)
        return stored
    except Exception as e:
        print(f"[memory] capture failed: {e}", flush=True)
        return []
