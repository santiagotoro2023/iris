"""Voice I/O (SPEC.md Phase 2).

Speech-to-text today. TTS is blocked on the voice-source decision, which SPEC.md
marks as a mandatory ASK USER because it defines IRiS's voice permanently.

Clients never talk to the STT service directly — everything routes through the
API, same rule as Ollama.
"""
import os

import time

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

import activity
import auth
import settings

STT_URL = os.environ.get("STT_URL", "http://stt:8001")
TTS_URL = os.environ.get("TTS_URL", "http://tts:8002")
MAX_AUDIO_BYTES = 25 * 1024 * 1024
MAX_SPEAK_CHARS = 4000

router = APIRouter(prefix="/voice", tags=["voice"])

_speakers_cache: tuple[float, list[str]] = (0.0, [])


def _tts_speakers() -> list[str]:
    """XTTS's built-in speakers. Santiago chose option (b) — a built-in voice shaped
    by pacing and delivery — so this list is the voice choice (SPEC.md 5)."""
    global _speakers_cache
    now = time.monotonic()
    if now - _speakers_cache[0] > 300:
        try:
            r = httpx.get(f"{TTS_URL}/speakers", timeout=10)
            names = sorted(r.json().get("speakers", []))
        except Exception:
            names = _speakers_cache[1]  # keep the last good list, never empty the dropdown
        if names:
            _speakers_cache = (now, names)
    current = settings.get("voice.tts_speaker") if "voice.tts_speaker" in settings.REGISTRY else None
    return sorted({*_speakers_cache[1], *( [current] if current else [] ),
                   os.environ.get("TTS_SPEAKER", "Daisy Studious")})

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


settings.setting(
    "voice.speak_replies", type="boolean", default=False,
    title="Speak replies aloud",
    description="Play IRiS's answers as speech automatically. You can always play an "
                "individual reply with the speaker button.")
settings.setting(
    "voice.tts_speaker", type="string", enum=_tts_speakers,
    default=os.environ.get("TTS_SPEAKER", "Daisy Studious"),
    title="Voice",
    description="Built-in XTTS speaker. Delivery is shaped by pacing and speed rather "
                "than by cloning a recorded voice.")
settings.setting(
    "voice.tts_language", type="string", enum=["en", "de", "fr", "it", "es"],
    default="en", title="Speaking language",
    description="XTTS renders the same voice in each supported language.")
settings.setting(
    "voice.tts_speed", type="number", minimum=0.5, maximum=1.5, default=1.0,
    title="Speaking pace",
    description="1.0 is natural. Lower is more deliberate, higher is brisker.")
settings.setting(
    "voice.tts_device", type="string", enum=["cuda", "cpu"],
    default=os.environ.get("TTS_DEVICE", "cuda"),
    title="Voice processing device",
    description="Move to CPU to free VRAM for the language model; much slower.")
settings.setting(
    "voice.tts_idle_unload", type="integer", minimum=0, maximum=3600, default=300,
    title="Release voice model after (seconds)",
    description="Same trade-off as the speech model: held on the GPU it competes with "
                "the language model. 0 keeps it loaded.")


class SpeakRequest(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_SPEAK_CHARS)
    language: str | None = None


@router.get("/status")
async def status(_: dict = Depends(auth.active_user)):
    out: dict = {}
    async with httpx.AsyncClient(timeout=5) as c:
        for name, url in (("stt", STT_URL), ("tts", TTS_URL)):
            try:
                r = await c.get(f"{url}/health")
                out[f"{name}_ok"] = r.status_code == 200
                out[name] = r.json()
            except Exception as e:
                out[f"{name}_ok"] = False
                out[name] = {"error": str(e)}
    return out


@router.post("/speak")
async def speak(body: SpeakRequest, user: dict = Depends(auth.active_user)):
    form = {
        "text": body.text,
        "speaker": settings.get("voice.tts_speaker"),
        "language": body.language or settings.get("voice.tts_language"),
        "speed": str(settings.get("voice.tts_speed")),
        "device": settings.get("voice.tts_device"),
        "idle_unload": str(settings.get("voice.tts_idle_unload")),
    }
    try:
        # Synthesis can include a cold model load.
        async with httpx.AsyncClient(timeout=300) as c:
            r = await c.post(f"{TTS_URL}/speak", data=form)
    except Exception as e:
        raise HTTPException(502, f"tts unreachable: {e}")
    if r.status_code != 200:
        raise HTTPException(502, f"tts: {r.text[:300]}")

    await activity.record("voice.speak", f"{len(body.text)} chars as "
                                         f"{form['speaker']}", user["username"])
    return Response(content=r.content, media_type="audio/wav")


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
