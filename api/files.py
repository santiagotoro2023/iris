"""Image, document and video analysis (SPEC.md Phase 6, pulled forward; §40).

An upload becomes text: a picture is described by the vision model, a document has
its text extracted, a video is sampled into frames and its speech transcribed. That
text travels with the message, so the main model can reason about the file on this
turn and every later one without re-reading it.

Uploads are also kept, so each analyser is a tool the model can call again with a
different question. Looking at a picture is an action, and every action IRiS takes
should announce itself (§35).
"""
import asyncio
import base64
import io
import os
import re
import shutil
import time
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

import activity
import auth
import settings

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama:11434")
MAX_BYTES = 20 * 1024 * 1024
MAX_DOC_CHARS = 20_000

IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif", "image/bmp"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mpg", ".mpeg"}
TEXT_SUFFIXES = {".txt", ".md", ".csv", ".json", ".yaml", ".yml", ".log", ".ini",
                 ".py", ".js", ".ts", ".sh", ".sql", ".html", ".css", ".xml", ".toml"}

# Uploads live here so an analyser can be run again with a different question.
UPLOADS = Path(os.environ.get("UPLOAD_DIR", "/media/uploads"))
# No fixed cap: the only real limit is somewhere to put it. A margin is kept so an
# upload cannot fill the disk the databases live on.
DISK_MARGIN_BYTES = 5 * 1024 * 1024 * 1024


def _room_for(size: int) -> bool:
    try:
        free = shutil.disk_usage(UPLOADS if UPLOADS.is_dir() else "/").free
    except OSError:
        return True
    return size + DISK_MARGIN_BYTES < free

router = APIRouter(prefix="/files", tags=["files"])

settings.setting(
    "vision.model", type="string",
    enum=["qwen2.5vl:3b", "qwen2.5vl:7b", "llava:7b", "moondream"],
    default=os.environ.get("IRIS_VISION_MODEL", "qwen2.5vl:3b"),
    title="Vision model",
    description="Describes uploaded images. Loading it evicts the language model from "
                "the GPU briefly, so smaller is smoother on 8 GB.")
settings.setting(
    "vision.video_frames", type="integer", minimum=2, maximum=16, default=6,
    title="Frames to look at in a video",
    description="More frames catch more, and each one costs a pass of the vision "
                "model.")
settings.setting(
    "vision.keep_uploads_hours", type="integer", minimum=0, maximum=8760, default=72,
    title="Keep uploaded files for (hours)",
    description="How long a file stays available for follow-up questions. 0 deletes "
                "it as soon as it has been read.")
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


def _as_png(data: bytes) -> bytes:
    """The vision model rejects webp and friends outright, so normalise first."""
    from PIL import Image
    img = Image.open(io.BytesIO(data))
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


async def _describe_image(data: bytes, prompt: str) -> str:
    try:
        data = _as_png(data)
    except Exception:
        pass  # already a format the model accepts, or genuinely unreadable
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
    name = file.filename or "upload"
    lower = name.lower()
    ctype = (file.content_type or "").split(";")[0]
    suffix = "." + lower.rsplit(".", 1)[-1] if "." in lower else ""
    is_video = suffix in VIDEO_SUFFIXES or ctype.startswith("video/")
    if not _room_for(len(data)):
        raise HTTPException(413, f"no room for {len(data) // 2**20} MB on the disk")

    path = keep(name, data)

    try:
        if is_video:
            kind = "video"
            text = await describe_video(path, question)
        elif ctype in IMAGE_TYPES or suffix in IMAGE_SUFFIXES:
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


# ---------------------------------------------------------------- video ----

async def _run(*args: str, timeout: int = 300) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        raise HTTPException(504, "that took too long to read")
    if proc.returncode != 0 and not out:
        raise HTTPException(500, err.decode()[:200] or "could not read the file")
    return out


async def _duration(path: Path) -> float:
    try:
        out = await _run("ffprobe", "-v", "error", "-show_entries",
                         "format=duration", "-of", "csv=p=0", str(path), timeout=60)
        return float(out.decode().strip())
    except Exception:
        return 0.0


async def describe_video(path: Path, question: str = "") -> str:
    """Sample frames across the whole video, and transcribe whatever is said.

    Evenly spaced rather than the first N seconds: a video's subject is rarely in
    its opening frame, and consecutive frames say almost the same thing.
    """
    frames = settings.get("vision.video_frames")
    seconds = await _duration(path)
    # The image prompt asks for detail, which is right for one picture and useless
    # for six: it produced 200 words a frame about colour calibration.
    prompt = (f"{question.strip()} Answer in one short sentence." if question.strip()
              else "In one short sentence, say what is visible here. No preamble, no "
                   "explanation, no offer of help.")
    parts: list[str] = []
    if seconds:
        parts.append(f"Video, {int(seconds // 60)}m {int(seconds % 60)}s long.")

    described = []
    for i in range(frames):
        at = (seconds * (i + 0.5) / frames) if seconds else i * 2
        try:
            jpeg = await _run("ffmpeg", "-nostdin", "-loglevel", "error",
                              "-ss", f"{at:.2f}", "-i", str(path),
                              "-frames:v", "1", "-q:v", "4", "-f", "image2", "-",
                              timeout=120)
            if not jpeg:
                continue
            text = await _describe_image(jpeg, prompt)
        except Exception as e:
            print(f"[files] frame at {at:.1f}s failed: {e}", flush=True)
            continue
        stamp = f"{int(at // 60):02d}:{int(at % 60):02d}"
        one_line = " ".join(text.split())[:220]
        if described and one_line == described[-1].split("] ", 1)[-1]:
            continue          # a static shot repeats; say it once
        described.append(f"[{stamp}] {one_line}")
    if described:
        parts.append("What is visible:\n" + "\n".join(described))

    try:
        wav = await _run("ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(path),
                         "-vn", "-ac", "1", "-ar", "16000", "-f", "wav", "-",
                         timeout=300)
        if wav and len(wav) > 1000:
            import voice
            result = await voice.transcribe_bytes(wav, "video.wav", "audio/wav")
            spoken = (result.get("text") or "").strip()
            if spoken:
                parts.append("What is said:\n" + spoken)
    except Exception as e:
        print(f"[files] video audio failed: {e}", flush=True)

    return "\n\n".join(parts) or "(nothing readable in that video)"


# -------------------------------------------------------------- storage ----

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def stored_path(name: str) -> Path:
    """Never let an uploaded name escape the upload directory."""
    return UPLOADS / (_SAFE.sub("_", Path(name).name) or "upload")


def keep(name: str, data: bytes) -> Path:
    UPLOADS.mkdir(parents=True, exist_ok=True)
    path = stored_path(name)
    path.write_bytes(data)
    prune()
    return path


def prune() -> None:
    hours = settings.get("vision.keep_uploads_hours")
    if not UPLOADS.is_dir():
        return
    cutoff = time.time() - hours * 3600
    for item in UPLOADS.iterdir():
        if item.is_file() and (hours == 0 or item.stat().st_mtime < cutoff):
            item.unlink(missing_ok=True)


def find(name: str) -> Path | None:
    """Match on the stored name, then on a loose contains, because the model will
    quote the filename back approximately."""
    if not UPLOADS.is_dir():
        return None
    exact = stored_path(name)
    if exact.is_file():
        return exact
    wanted = _SAFE.sub("_", name).lower()
    for item in sorted(UPLOADS.iterdir(),
                       key=lambda p: p.stat().st_mtime, reverse=True):
        if wanted and wanted in item.name.lower():
            return item
    return None


def recent(limit: int = 10) -> list[str]:
    if not UPLOADS.is_dir():
        return []
    return [p.name for p in sorted(UPLOADS.iterdir(),
                                   key=lambda p: p.stat().st_mtime,
                                   reverse=True)[:limit] if p.is_file()]


async def analyse_stored(name: str, question: str = "") -> str:
    """Used by the analysis tools: read a kept upload again, with a new question."""
    path = find(name)
    if not path:
        have = recent()
        return (f"No uploaded file matching {name!r}. "
                + (f"Available: {', '.join(have)}." if have else "Nothing uploaded."))
    suffix = path.suffix.lower()
    data = path.read_bytes()
    if suffix in VIDEO_SUFFIXES:
        return await describe_video(path, question)
    if suffix in IMAGE_SUFFIXES:
        return await _describe_image(
            data, question.strip() or settings.get("vision.prompt"))
    if suffix == ".pdf":
        text = _extract_pdf(data)
    elif suffix == ".docx":
        text = _extract_docx(data)
    else:
        text = data.decode("utf-8", errors="replace")
    text = text.strip()[:MAX_DOC_CHARS]
    return text or "(no readable content)"
