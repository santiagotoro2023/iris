"""Image and document analysis (SPEC.md Phase 6, pulled forward).

An upload becomes text: a picture is described by the vision model, a document has
its text extracted. That text then travels with the message, so the main model can
reason about the file on this turn and every later one without re-reading it.
"""
import base64
import io
import os

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

import activity
import auth
import settings

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama:11434")
MAX_BYTES = 20 * 1024 * 1024
MAX_DOC_CHARS = 20_000

IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif", "image/bmp"}
TEXT_SUFFIXES = {".txt", ".md", ".csv", ".json", ".yaml", ".yml", ".log", ".ini",
                 ".py", ".js", ".ts", ".sh", ".sql", ".html", ".css", ".xml", ".toml"}

router = APIRouter(prefix="/files", tags=["files"])

settings.setting(
    "vision.model", type="string",
    enum=["qwen2.5vl:3b", "qwen2.5vl:7b", "llava:7b", "moondream"],
    default=os.environ.get("IRIS_VISION_MODEL", "qwen2.5vl:3b"),
    title="Vision model",
    description="Describes uploaded images. Loading it evicts the language model from "
                "the GPU briefly, so smaller is smoother on 8 GB.")
settings.setting(
    "vision.prompt", type="string", format="multiline",
    default="Describe this image in detail. Include any text you can read verbatim, "
            "and note anything unusual or noteworthy.",
    title="Image prompt",
    description="What IRiS is asked when it looks at a picture.")


def _extract_pdf(data: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    return "\n\n".join((page.extract_text() or "") for page in reader.pages)


def _extract_docx(data: bytes) -> str:
    import docx
    document = docx.Document(io.BytesIO(data))
    parts = [p.text for p in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            parts.append(" | ".join(c.text for c in row.cells))
    return "\n".join(parts)


async def _describe_image(data: bytes, prompt: str) -> str:
    body = {"model": settings.get("vision.model"),
            "messages": [{"role": "user",
                          "content": prompt,
                          "images": [base64.b64encode(data).decode()]}],
            "stream": False}
    async with httpx.AsyncClient(timeout=300) as c:
        r = await c.post(f"{OLLAMA_URL}/api/chat", json=body)
    if r.status_code != 200:
        raise HTTPException(502, f"vision model: {r.text[:300]}")
    return (r.json().get("message") or {}).get("content", "").strip()


@router.post("/analyze")
async def analyze(file: UploadFile = File(...),
                  question: str = Form(""),
                  user: dict = Depends(auth.active_user)):
    data = await file.read()
    if not data:
        raise HTTPException(400, "empty file")
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "file too large (20 MB limit)")

    name = file.filename or "upload"
    lower = name.lower()
    ctype = (file.content_type or "").split(";")[0]
    suffix = "." + lower.rsplit(".", 1)[-1] if "." in lower else ""

    try:
        if ctype in IMAGE_TYPES or suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
            kind = "image"
            # A specific question beats a generic description.
            text = await _describe_image(
                data, question.strip() or settings.get("vision.prompt"))
        elif suffix == ".pdf" or ctype == "application/pdf":
            kind = "pdf"
            text = _extract_pdf(data)
        elif suffix == ".docx":
            kind = "document"
            text = _extract_docx(data)
        elif suffix in TEXT_SUFFIXES or ctype.startswith("text/"):
            kind = "text"
            text = data.decode("utf-8", errors="replace")
        else:
            raise HTTPException(415, f"unsupported file type: {ctype or suffix or '?'}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"could not read {name}: {e}")

    text = (text or "").strip()
    truncated = len(text) > MAX_DOC_CHARS
    if truncated:
        text = text[:MAX_DOC_CHARS] + "\n[...truncated]"
    if not text:
        text = "(no readable content)"

    await activity.record("files.analyze", f"{kind}: {name} ({len(data)//1024} KB)",
                          user["username"])
    return {"name": name, "kind": kind, "bytes": len(data),
            "truncated": truncated, "text": text}
