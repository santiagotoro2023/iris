"""Memory (SPEC.md Phase 3).

Facts worth keeping are embedded with bge-m3 and stored in Qdrant. Two things use
them, and the second is what actually makes memory work:

  1. A `remember` tool, so IRiS can deliberately store something it just learned.
  2. Automatic recall: every user turn is searched against the store and anything
     relevant is folded into the system turn before the model sees the question.
     Relying on the model to *decide* to search would mean it usually does not.

Qdrant speaks plain HTTP and httpx is already here, so there is no client library.
"""
import asyncio
import json
import os
import re
import time
import uuid

from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
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
    title="Memory", order=1,
    description="Off makes every conversation start from nothing.")
settings.setting(
    "memory.embed_model", type="string", enum=_embed_models, default=os.environ.get("IRIS_EMBED_MODEL", "bge-m3"),
    title="Embedding model", order=80,
    description="How memories are searched. Changing it makes existing memories "
                "unfindable until they are rebuilt.")
settings.setting(
    "memory.recall_count", type="integer", minimum=1, maximum=20, default=5,
    title="Memories to recall",
    description="How many relevant memories are put in front of IRiS on each turn. "
                "More context costs speed and can crowd out the actual question.")
settings.setting(
    "memory.min_score", type="number", minimum=0.2, maximum=0.95, default=0.42,
    title="Recall threshold", order=82,
    description="How close a memory must be to the question before it is used. Lower "
                "remembers more and drags in irrelevant things.")
settings.setting(
    "memory.retention_days", type="integer", minimum=0, maximum=3650, default=30,
    title="Keep transcripts for (days)", order=3,
    description="Conversations older than this are deleted nightly. What IRiS learned "
                "from them was already distilled into memories and is kept, so this "
                "expires the verbatim record, not the knowledge. 0 keeps everything.")
settings.setting(
    "memory.auto_capture", type="boolean", default=True,
    title="Learn from conversations", order=2,
    description="Quietly keep anything durable you mention, so you do not have to "
                "tell it to remember.")
settings.setting(
    "memory.dedup_score", type="number", minimum=0.8, maximum=1.0, default=0.93,
    title="Duplicate threshold", order=83,
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


@router.post("/ingest")
async def ingest(audio: UploadFile = File(...),
                 user: dict = Depends(auth.active_user)):
    """A recording in, searchable memory out (SPEC.md Phase 3 ingestion)."""
    import voice
    data = await audio.read()
    if not data:
        raise HTTPException(400, "empty audio")
    limit = 500 * 1024 * 1024
    if len(data) > limit:
        raise HTTPException(413, f"that recording is "
                                 f"{len(data) // 2**20} MB; the limit is "
                                 f"{limit // 2**20} MB")
    result = await voice.transcribe_bytes(data, audio.filename or "recording.webm",
                                          audio.content_type or "audio/webm")
    text = (result.get("text") or "").strip()
    if not text:
        raise HTTPException(422, "nothing audible in that recording")
    label = audio.filename or "recording"
    out = await ingest_transcript(text, user["id"], label)
    await activity.record(
        "memory.ingest",
        f"{label}: {result.get('duration', '?')}s, {out['chunks']} chunks, "
        f"{len(out['facts'])} facts", user["username"])
    return {"transcript": text, "language": result.get("language"), **out}


@router.delete("/{point_id}")
async def delete(point_id: str, user: dict = Depends(auth.active_user)):
    await forget(point_id)
    await activity.record("memory.forget", point_id, user["username"])
    return {"ok": True}


# --------------------------------------------------------------- capture ----

CAPTURE_SYSTEM = """You extract durable facts for a personal assistant's long-term \
memory. You are not talking to anyone; you only produce data.

Every fact you return MUST be accompanied by a quote: a span copied WORD FOR WORD from \
the conversation that states it. If you cannot copy an exact span that says it, the \
fact does not belong in the list. Do not paraphrase the quote, do not stitch pieces \
together, do not quote yourself.

STORE ONLY: stable preferences, personal or biographical details, their hardware or \
software setup, decisions they have made, projects they are working on, people and \
places that recur in their life. Write each fact in the third person as a standalone \
sentence that will still make sense in six months.

NEVER STORE: questions they asked, facts you looked up for them, general knowledge, \
anything about yourself, or conversational filler.

Most exchanges contain nothing durable. An empty list is the normal, correct answer \
and is always better than a padded one."""

CAPTURE_FORMAT = {
    "type": "object",
    "properties": {
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "fact": {"type": "string"},
                    "quote": {"type": "string",
                              "description": "Copied word for word from the "
                                             "conversation."},
                },
                "required": ["fact", "quote"],
            },
        },
    },
    "required": ["facts"],
}

MIN_CAPTURE_CHARS = 40
MAX_FACTS = 5


async def _complete(system: str, user: str, fmt: dict | None = None) -> str:
    """A plain, tool-free, persona-free completion. Extraction is a different job
    from being IRiS, and giving it the persona made it answer in character."""
    body = {"model": settings.get("llm.model"),
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "stream": False, "think": False, "options": {"temperature": 0}}
    if fmt:
        body["format"] = fmt
    async with httpx.AsyncClient(timeout=180) as c:
        r = await c.post(f"{OLLAMA_URL}/api/chat", json=body)
    if r.status_code != 200:
        raise RuntimeError(f"ollama: {r.text[:200]}")
    return (r.json().get("message") or {}).get("content", "")


def _flat(text: str) -> str:
    """Compare on words alone, so punctuation or spacing cannot break a real quote."""
    return " " + " ".join(re.findall(r"[a-z0-9]+", text.lower())) + " "


# Words that carry no evidence either way, so their presence must not vouch for a
# sentence and their absence must not condemn it.
_FILLER = {"user", "they", "their", "them", "then", "there", "that", "this", "with",
           "have", "been", "will", "would", "about", "into", "from", "when", "what",
           "which", "these", "those", "also", "just", "very", "some", "more", "than",
           "does", "prefers", "wants", "uses", "said", "says", "always", "never"}


def _grounded(fact: str, source: str) -> bool:
    """Does the source actually support this sentence?

    Used against a fact's own quote rather than the whole exchange, which is much
    tighter: a fact must be supported by the specific span the model pointed at.
    """
    words = {w for w in re.findall(r"[a-z]{4,}", fact.lower()) if w not in _FILLER}
    if not words:
        return False
    have = set(re.findall(r"[a-z]{4,}", source.lower()))
    return sum(w in have for w in words) / len(words) >= 0.5


def evidenced(facts: list[dict], source: str) -> list[str]:
    """Keep only facts whose quote is genuinely in the conversation.

    This is the root fix for an extractor that pads its list. Asking for fewer facts
    does not work: told "at most 3" it returns 3, and told not to infer or embellish
    it embellishes anyway, verbatim, on the next run. Requiring a copied span moves
    the question from "did it obey" to "is this string present", which is decidable
    here rather than by the model. A model can invent a fact; it cannot invent a
    quote that is already in the text.
    """
    flat_source = _flat(source)
    kept = []
    for item in facts:
        fact = (item.get("fact") or "").strip()
        quote = (item.get("quote") or "").strip()
        if len(fact) < 12 or len(quote) < 8:
            continue
        if _flat(quote).strip() not in flat_source:
            continue                       # the evidence is not in the conversation
        if not _grounded(fact, quote):
            continue                       # the quote does not support the fact
        kept.append(fact)
    return kept


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
        reply = await _complete(CAPTURE_SYSTEM, text[:6000], CAPTURE_FORMAT)
        try:
            candidates = json.loads(reply).get("facts") or []
        except (ValueError, AttributeError):
            return []
        stored = []
        for fact in evidenced(candidates, text)[:MAX_FACTS]:
            await remember(fact, user_id, source="learned")
            stored.append(fact)
        return stored
    except Exception as e:
        print(f"[memory] capture failed: {e}", flush=True)
        return []


async def compactor() -> None:
    """Nightly retention sweep (SPEC.md 6). Runs an hour after the backup, so a
    transcript is always archived before it is expired."""
    import chat
    while True:
        await asyncio.sleep(3600)
        try:
            now = datetime.now(ZoneInfo(settings.get("general.timezone")))
            if now.hour != (int(settings.get("backup.at").split(":")[0]) + 1) % 24:
                continue
            result = await chat.compact(settings.get("memory.retention_days"))
            if result["removed"]:
                print(f"[memory] compacted {result['removed']} transcripts", flush=True)
                await activity.record(
                    "memory.compact",
                    f"{result['removed']} transcripts past "
                    f"{settings.get('memory.retention_days')} days, "
                    f"{result['kept']} kept", "schedule")
        except Exception as e:
            print(f"[memory] compaction failed: {e}", flush=True)


# --------------------------------------------------------------- ingest ----

CHUNK_CHARS = 600
CHUNK_OVERLAP = 1          # sentences carried into the next chunk for continuity

_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def chunk(text: str, size: int = CHUNK_CHARS) -> list[str]:
    """Split a transcript on sentence boundaries, never mid-sentence.

    A chunk cut through a sentence embeds badly: half a thought is close to nothing.
    One sentence of overlap keeps a fact that straddles a boundary retrievable from
    either side.
    """
    sentences = [s.strip() for s in _SENTENCE.split(text.strip()) if s.strip()]
    chunks: list[str] = []
    current: list[str] = []
    for sentence in sentences:
        current.append(sentence)
        if sum(len(s) + 1 for s in current) >= size:
            chunks.append(" ".join(current))
            current = current[-CHUNK_OVERLAP:] if CHUNK_OVERLAP else []
    tail = " ".join(current)
    # The overlap is already in the previous chunk; only keep a tail that adds something.
    if tail and (not chunks or tail not in chunks[-1]):
        chunks.append(tail)
    return chunks


async def ingest_transcript(text: str, user_id: int, label: str,
                            when: float | None = None) -> dict:
    """Store a recording's transcript as searchable memory, and distil it as well.

    Two different things come out of one recording: the verbatim record, chunked so
    it can be searched, and any durable facts in it. The chunks are episodic and
    expire with the retention window's spirit; the facts are what IRiS actually
    learned.
    """
    pieces = chunk(text)
    if not pieces:
        return {"chunks": 0, "facts": []}
    vectors = await embed(pieces)
    await _ensure_collection(len(vectors[0]))
    created = when or time.time()
    points = [{"id": str(uuid.uuid4()), "vector": v,
               "payload": {"text": p, "kind": "transcript", "source": label,
                           "user_id": user_id, "created": created}}
              for p, v in zip(pieces, vectors)]
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.put(f"{QDRANT_URL}/collections/{COLLECTION}/points",
                        params={"wait": "true"}, json={"points": points})
    if r.status_code >= 300:
        raise HTTPException(502, f"qdrant: {r.text[:200]}")

    facts = await capture([{"role": "user", "content": text}], user_id)
    return {"chunks": len(pieces), "facts": facts}
