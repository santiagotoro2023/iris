"""Voice I/O (SPEC.md Phase 2).

Speech-to-text today. TTS is blocked on the voice-source decision, which SPEC.md
marks as a mandatory ASK USER because it defines IRiS's voice permanently.

Clients never talk to the STT service directly — everything routes through the
API, same rule as Ollama.
"""
import os
import re
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

_speakers_cache: tuple[str, float, list[str]] = ("", 0.0, [])


def _tts_speakers() -> list[str]:
    """Voices for whichever engine is selected. Santiago chose a built-in voice rather
    than a cloned one (SPEC.md 5), so this list is the voice choice."""
    global _speakers_cache
    engine = (settings.get("voice.engine")
              if "voice.engine" in settings.REGISTRY else "piper")
    now = time.monotonic()
    if _speakers_cache[0] != engine or now - _speakers_cache[1] > 300:
        try:
            r = httpx.get(f"{TTS_URL}/speakers", params={"engine": engine}, timeout=30)
            names = list(r.json().get("speakers", []))
        except Exception:
            names = _speakers_cache[2]  # keep the last good list, never empty the dropdown
        if names:
            _speakers_cache = (engine, now, names)
    current = (settings.get("voice.tts_speaker")
               if "voice.tts_speaker" in settings.REGISTRY else None)
    listed = list(_speakers_cache[2])
    for extra in (current, os.environ.get("TTS_SPEAKER", "en_GB-jenny_dioco-medium")):
        if extra and extra not in listed:
            listed.append(extra)
    # British voices first — the whole reason for offering Piper.
    return sorted(listed, key=lambda n: (not n.startswith("en_GB"), n))

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
    "voice.engine", type="string", enum=["piper", "xtts"],
    default=os.environ.get("TTS_ENGINE", "piper"),
    title="Voice engine",
    description="piper: CPU only, ~30x realtime, has explicitly British voices, and "
                "never competes with the language model for VRAM. xtts: more expressive "
                "but needs 1.65 GB of GPU memory it cannot have while the language "
                "model is loaded, so it falls back to CPU and becomes too slow to keep "
                "up with playback.")
settings.setting(
    "voice.tts_speaker", type="string", enum=_tts_speakers,
    default=os.environ.get("TTS_SPEAKER", "en_GB-jenny_dioco-medium"),
    title="Voice",
    description="Built-in speaker for the selected engine. en_GB voices are listed "
                "first. Use the preview button to audition one.")
settings.setting(
    "voice.tts_language", type="string", enum=["en", "de", "fr", "it", "es"],
    default="en", title="Speaking language",
    description="XTTS renders the same voice in each supported language.")
settings.setting(
    "voice.tts_speed", type="number", minimum=0.5, maximum=1.6, default=1.2,
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


# Spoken text is not written text (SPEC.md 17). Markdown read aloud, "dot" for a bare
# period, and AG/IT pronounced as words are all defects.
_CODE_FENCE = re.compile(r"```.*?```", re.S)
_INLINE_CODE = re.compile(r"`([^`]*)`")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_EMPHASIS = re.compile(r"(\*{1,3}|_{2,3})(.+?)\1", re.S)
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.M)
_BULLET = re.compile(r"^\s*[-*+]\s+", re.M)
# "1." at the start of a line is the usual source of a spoken "dot".
_ORDERED = re.compile(r"^\s*(\d{1,2})[.)]\s+", re.M)
_ACRONYM = re.compile(r"\b([A-Z]{2,6})\b")
_LEFTOVER_MD = re.compile(r"[*_#>|]+")
_WS = re.compile(r"[ \t]+")
_BLANKS = re.compile(r"\n{2,}")


# All-caps words that are spoken, not spelled.
_SAID_AS_WORD = {"RAM", "LAN", "WAN", "NAS", "NATO", "NASA", "ASCII", "JSON", "SCUBA"}


def _spell_acronym(m: re.Match) -> str:
    """AG -> "A G", DHCP -> "D H C P", but SIDMAR and NASA stay as words.

    No perfect rule exists, so: spell it if it is very short or has no vowels —
    those are nearly always initialisms — and otherwise leave it alone.
    """
    word = m.group(1)
    if word in _SAID_AS_WORD:
        return word
    if len(word) <= 3 or not set(word) & set("AEIOUY"):
        return " ".join(word)
    return word


def speech_text(text: str) -> str:
    """Turn a written reply into something worth listening to."""
    text = _CODE_FENCE.sub(" ", text)
    text = _INLINE_CODE.sub(r"\1", text)
    text = _LINK.sub(r"\1", text)
    for _ in range(3):                      # ***bold italic*** nests
        text = _EMPHASIS.sub(r"\2", text)
    text = _HEADING.sub("", text)
    text = _BULLET.sub("", text)
    # Read "1. Discovery" as "1, Discovery" — a pause, not the word "dot".
    text = _ORDERED.sub(r"\1, ", text)
    text = _LEFTOVER_MD.sub(" ", text)

    text = _ACRONYM.sub(_spell_acronym, text)

    text = _BLANKS.sub(". ", text)
    text = text.replace("\n", " ")
    text = _WS.sub(" ", text).strip()

    if text and text[-1] not in ".!?…,;:":
        text += "."
    # XTTS clips the tail of an utterance; a trailing pause gives it room to finish.
    return text + " …" if text else text


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
        "text": speech_text(body.text),
        "engine": settings.get("voice.engine"),
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
