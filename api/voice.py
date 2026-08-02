"""Voice I/O (SPEC.md Phase 2).

Speech-to-text today. TTS is blocked on the voice-source decision, which SPEC.md
marks as a mandatory ASK USER because it defines IRiS's voice permanently.

Clients never talk to the STT service directly — everything routes through the
API, same rule as Ollama.
"""
import os

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

import activity
import auth
import settings

STT_URL = os.environ.get("STT_URL", "http://stt:8001")
MAX_AUDIO_BYTES = 25 * 1024 * 1024

router = APIRouter(prefix="/voice", tags=["voice"])

settings.setting(
    "voice.stt_model", type="string",
    enum=["large-v3", "large-v2", "medium", "small", "base", "tiny",
          "distil-large-v3"],
    default=os.environ.get("STT_MODEL", "large-v3"),
    title="Speech model",
    description="Larger is more accurate and uses more VRAM, which is shared with "
                "the language model.")
settings.setting(
    "voice.stt_device", type="string", enum=["cuda", "cpu"],
    default=os.environ.get("STT_DEVICE", "cuda"),
    title="Speech processing device",
    description="Move to CPU if the GPU is short of memory; slower but frees VRAM.")
settings.setting(
    "voice.stt_compute", type="string",
    enum=["int8", "int8_float16", "float16", "float32"],
    default=os.environ.get("STT_COMPUTE", "int8_float16"),
    title="Speech precision",
    description="int8 uses the least VRAM. float16 is the most accurate on GPU.")
settings.setting(
    "voice.stt_language", type="string",
    enum=["auto", "en", "de", "fr", "it", "es"],
    default="auto", title="Speech language",
    description="'auto' detects per utterance — the right choice when switching "
                "between English and German.")
settings.setting(
    "voice.vad", type="boolean", default=True,
    title="Filter silence",
    description="Drop non-speech before transcribing. Usually improves accuracy.")
settings.setting(
    "voice.stt_idle_unload", type="integer", minimum=0, maximum=3600, default=300,
    title="Release speech model after (seconds)",
    description="Speech and language models share one GPU. Holding the speech model "
                "costs the language model roughly 2.3x its speed, so it is released "
                "when idle and reloaded on the next recording. 0 keeps it loaded.")


@router.get("/status")
async def status(_: dict = Depends(auth.active_user)):
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{STT_URL}/health")
        return {"stt_ok": r.status_code == 200, **r.json()}
    except Exception as e:
        return {"stt_ok": False, "error": str(e)}


@router.post("/transcribe")
async def transcribe(audio: UploadFile = File(...),
                     user: dict = Depends(auth.active_user)):
    data = await audio.read()
    if not data:
        raise HTTPException(400, "empty audio")
    if len(data) > MAX_AUDIO_BYTES:
        raise HTTPException(413, "audio too large")

    form = {
        "model": settings.get("voice.stt_model"),
        "device": settings.get("voice.stt_device"),
        "compute": settings.get("voice.stt_compute"),
        "language": settings.get("voice.stt_language"),
        "vad": str(settings.get("voice.vad")).lower(),
        "idle_unload": str(settings.get("voice.stt_idle_unload")),
    }
    try:
        # Model loads can take a while on first use or after a settings change.
        async with httpx.AsyncClient(timeout=300) as c:
            r = await c.post(f"{STT_URL}/transcribe", data=form,
                             files={"audio": (audio.filename or "audio.webm", data,
                                              audio.content_type or "audio/webm")})
    except Exception as e:
        raise HTTPException(502, f"stt unreachable: {e}")
    if r.status_code != 200:
        raise HTTPException(502, f"stt: {r.text[:300]}")

    result = r.json()
    await activity.record(
        "voice.transcribe",
        f"{result.get('duration', '?')}s audio, detected {result.get('language')}",
        user["username"])
    return result
