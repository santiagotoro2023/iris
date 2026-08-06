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
    for extra in (current, os.environ.get("TTS_SPEAKER", "en_GB-cori-high")):
        if extra and extra not in listed:
            listed.append(extra)
    # British voices first — the whole reason for offering Piper.
    return sorted(listed, key=lambda n: (not n.startswith("en_GB"), n))

settings.setting(
    "voice.stt_model", type="string",
    enum=["large-v3", "large-v2", "medium", "small", "base", "tiny",
          "distil-large-v3"],
    default=os.environ.get("STT_MODEL", "large-v3"),
    title="Speech model", order=81,
    description="Larger is more accurate and uses more of the GPU.")
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
    "voice.speak_replies", type="boolean", default=True,
    title="Speak replies aloud", order=1,
    description="Play answers automatically. The speaker button on a reply always "
                "works either way.")
settings.setting(
    "voice.engine", type="string", enum=["piper", "xtts"],
    default=os.environ.get("TTS_ENGINE", "piper"),
    title="Voice engine", order=80,
    description="Piper is fast, British, and stays out of the GPU's way. XTTS is more "
                "expressive but too slow here to keep up with playback.")
settings.setting(
    "voice.tts_speaker", type="string", enum=_tts_speakers,
    default=os.environ.get("TTS_SPEAKER", "en_GB-cori-high"),
    title="Voice", order=2,
    description="British voices are listed first. Press preview to hear one.")
settings.setting(
    "voice.tts_language", type="string", enum=["en", "de", "fr", "it", "es"],
    default="en", title="Speaking language",
    description="XTTS renders the same voice in each supported language.")
settings.setting(
    "voice.tts_speed", type="number", minimum=0.5, maximum=1.6, default=1.1,
    title="Speaking pace", order=3,
    description="1.0 is natural. Lower is more deliberate, higher is brisker.")
settings.setting(
    "voice.expressiveness", type="number", minimum=0.0, maximum=1.0, default=0.25,
    title="Expressiveness", order=4,
    description="Low is level and matter-of-fact. High gets excitable.")
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
# A bare address read aloud is "h t t p s colon slash slash..." for twenty seconds.
_URL = re.compile(r"(?:https?://|www\.)\S+")
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


_UNITS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
          "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
          "sixteen", "seventeen", "eighteen", "nineteen"]
_TENS = {2: "twenty", 3: "thirty", 4: "forty", 5: "fifty"}
# Colon, dot or "h", because the model writes 16:00, 08.30 and 8h30 interchangeably.
# A trailing am/pm is swallowed so "8:30 AM" does not become "eight thirty A M A M".
_CLOCK = re.compile(
    # The space belongs to the optional group: consumed on its own it welded the
    # meridiem to the next word, giving "eight thirty AMUhr".
    r"\b([01]?\d|2[0-3])\s*([:.h])\s*([0-5]\d)\b(?:\s*([ap])\.?m\.?\b)?", re.I)


def _spoken_number(n: int) -> str:
    if n < 20:
        return _UNITS[n]
    tens, unit = divmod(n, 10)
    return _TENS[tens] + (f" {_UNITS[unit]}" if unit else "")


def _spoken_clock(m: re.Match) -> str:
    """07:23 read as "zero seven twenty three" is not how anyone says a time."""
    raw_hour, sep, raw_minute = m.group(1), m.group(2), m.group(3)
    # A dot is only a time separator when it cannot be a decimal: 08.30 and 16.00
    # are times, 8.50 is a price and must be left alone.
    if sep == "." and not (raw_hour.startswith("0") or int(raw_hour) >= 13):
        return m.group(0)
    hour, minute = int(raw_hour), int(raw_minute)
    said = (m.group(4) or "").lower()
    # If they wrote the meridiem, believe it: "8:30 PM" is not half past eight in
    # the morning.
    if said == "p" and hour < 12:
        hour += 12
    elif said == "a" and hour == 12:
        hour = 0
    suffix = "AM" if hour < 12 else "PM"
    twelve = hour % 12 or 12
    if minute == 0:
        return f"{_spoken_number(twelve)} o'clock {suffix}"
    if minute < 10:
        return f"{_spoken_number(twelve)} oh {_spoken_number(minute)} {suffix}"
    return f"{_spoken_number(twelve)} {_spoken_number(minute)} {suffix}"


def speech_text(text: str) -> str:
    """Turn a written reply into something worth listening to."""
    text = _CODE_FENCE.sub(" ", text)
    text = _INLINE_CODE.sub(r"\1", text)
    text = _LINK.sub(r"\1", text)
    text = _URL.sub("", text)
    for _ in range(3):                      # ***bold italic*** nests
        text = _EMPHASIS.sub(r"\2", text)
    text = _HEADING.sub("", text)
    text = _BULLET.sub("", text)
    # Read "1. Discovery" as "1, Discovery" — a pause, not the word "dot".
    text = _ORDERED.sub(r"\1, ", text)
    text = _LEFTOVER_MD.sub(" ", text)

    # Before the acronym pass, which would otherwise see the AM/PM this adds.
    text = _CLOCK.sub(_spoken_clock, text)
    text = _ACRONYM.sub(_spell_acronym, text)

    # "CubeServ AG (a Swiss firm)" ran straight into the bracket with no breath.
    text = re.sub(r"(?<=[^\s,;:.!?])\s*\(", ", (", text)
    text = _BLANKS.sub(". ", text)
    lines = [ln.strip() for ln in text.split("\n")]
    lines = [ln if not ln or ln[-1] in ".!?…,;:" else ln + "." for ln in lines]
    text = " ".join(ln for ln in lines if ln)
    text = _WS.sub(" ", text).strip()

    if text and text[-1] not in ".!?…,;:":
        text += "."
    if not text:
        return text
    # XTTS clips the tail, so it needs trailing room. Piper does not, and the extra
    # ellipsis made it trail off into a mumble.
    return text + " …" if settings.get("voice.engine") == "xtts" else text


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
        "expressiveness": str(settings.get("voice.expressiveness")),
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


async def transcribe_bytes(data: bytes, filename: str = "audio.webm",
                           content_type: str = "audio/webm",
                           timeout: float = 300, timestamps: bool = False,
                           diarize: bool = False) -> dict:
    """One transcription path for the mic button and the hands-free listener alike,
    so the configured model, language and idle-unload apply identically to both.

    `timeout` is a parameter because the default is sized for a spoken sentence, and
    a seven-minute video pushed onto the CPU needs many minutes: a fixed 300s turned
    long videos into a silent "stt unreachable" and an answer written from the
    pictures alone.
    """
    form = {
        "model": settings.get("voice.stt_model"),
        "device": settings.get("voice.stt_device"),
        "compute": settings.get("voice.stt_compute"),
        "language": settings.get("voice.stt_language"),
        "vad": str(settings.get("voice.vad")).lower(),
        "idle_unload": str(settings.get("voice.stt_idle_unload")),
        "timestamps": str(timestamps).lower(),
        "diarize": str(diarize).lower(),
    }
    try:
        # Model loads can take a while on first use or after a settings change.
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(f"{STT_URL}/transcribe", data=form,
                             files={"audio": (filename, data, content_type)})
    except Exception as e:
        # str() on a timeout is the empty string, which made the log read
        # "stt unreachable:" and say nothing about why.
        raise HTTPException(502, f"stt unreachable: {e or type(e).__name__}")
    if r.status_code != 200:
        raise HTTPException(502, f"stt: {r.text[:300]}")
    return r.json()


@router.post("/transcribe")
async def transcribe(audio: UploadFile = File(...),
                     user: dict = Depends(auth.active_user)):
    data = await audio.read()
    if not data:
        raise HTTPException(400, "empty audio")
    if len(data) > MAX_AUDIO_BYTES:
        raise HTTPException(413, "audio too large")

    result = await transcribe_bytes(data, audio.filename or "audio.webm",
                                    audio.content_type or "audio/webm")
    await activity.record(
        "voice.transcribe",
        f"{result.get('duration', '?')}s audio, detected {result.get('language')}",
        user["username"])
    return result
