"""Text-to-speech service (SPEC.md Phase 2).

Two engines, selectable at runtime:

  piper — CPU only, ~30x realtime, explicitly labelled en_GB voices. Uses no VRAM,
          so it never competes with the language model.
  xtts  — GPU, more expressive, but 1.65 GB of VRAM it cannot have while qwen3:8b is
          resident on a 7.8 GiB card (SPEC.md 17). Falls back to CPU on OOM, which is
          ~3x slower than realtime and too slow to keep up with playback.

Santiago chose a built-in speaker rather than a cloned voice, which both engines honour.
"""
import gc
import io
import os
import subprocess
import threading
import time
import wave
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import Response

os.environ.setdefault("COQUI_TOS_AGREED", "1")  # XTTS v2 ships under the CPML

MODELS = Path(os.environ.get("TTS_HOME", "/models"))
XTTS_MODEL = os.environ.get("TTS_MODEL", "tts_models/multilingual/multi-dataset/xtts_v2")
DEFAULT_ENGINE = os.environ.get("TTS_ENGINE", "piper")
DEFAULT_SPEAKER = os.environ.get("TTS_SPEAKER", "en_GB-cori-high")
DEFAULT_DEVICE = os.environ.get("TTS_DEVICE", "cuda")
IDLE_UNLOAD = float(os.environ.get("TTS_IDLE_UNLOAD", "300"))

app = FastAPI(title="IRiS TTS")

_lock = threading.Lock()
_synth_lock = threading.Lock()      # one model object is not safe to drive concurrently
_xtts = None
_xtts_device: str | None = None
_piper: dict = {}                   # voice name -> loaded PiperVoice
_last_used = 0.0
_idle_unload = IDLE_UNLOAD


# ---------------------------------------------------------------- piper ----

def piper_voices() -> list[str]:
    """British voices are the point of this engine, so they sort first."""
    try:
        out = subprocess.run(["python3", "-m", "piper.download_voices"],
                             capture_output=True, text=True, timeout=60).stdout
    except Exception:
        out = ""
    names = [ln.strip() for ln in out.splitlines()
             if ln.strip().startswith("en_") and " " not in ln.strip()]
    local = [p.stem for p in MODELS.glob("*.onnx")]
    return sorted(set(names) | set(local),
                  key=lambda n: (not n.startswith("en_GB"), n))


def _piper_voice(name: str):
    from piper import PiperVoice
    if name not in _piper:
        onnx = MODELS / f"{name}.onnx"
        if not onnx.exists():
            subprocess.run(["python3", "-m", "piper.download_voices", name],
                           cwd=MODELS, capture_output=True, timeout=600)
        if not onnx.exists():
            raise RuntimeError(f"could not fetch voice {name!r}")
        _piper[name] = PiperVoice.load(str(onnx))
    return _piper[name]


def _piper_speak(text: str, voice: str, speed: float, expressiveness: float) -> bytes:
    from piper import SynthesisConfig
    v = _piper_voice(voice)
    buf = io.BytesIO()
    # Piper's defaults (0.667 / 0.8) read as excitable. Scaling them down flattens the
    # delivery, which is what IRiS should sound like.
    cfg = SynthesisConfig(length_scale=1.0 / speed,          # inverse of "speed"
                          noise_scale=0.20 + 0.60 * expressiveness,
                          noise_w_scale=0.25 + 0.75 * expressiveness)
    with wave.open(buf, "wb") as w:
        v.synthesize_wav(text, w, syn_config=cfg)
    return buf.getvalue()


# ----------------------------------------------------------------- xtts ----

def _release_xtts() -> None:
    """Free VRAM. Caller holds _lock."""
    global _xtts, _xtts_device
    if _xtts is not None:
        del _xtts
    _xtts, _xtts_device = None, None
    gc.collect()
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def _get_xtts(device: str):
    global _xtts, _xtts_device, _last_used
    with _lock:
        if _xtts_device != device:
            _release_xtts()
            from TTS.api import TTS
            _xtts = TTS(XTTS_MODEL).to(device)
            _xtts_device = device
        _last_used = time.monotonic()
        return _xtts


def xtts_speakers() -> list[str]:
    model = _get_xtts("cpu" if DEFAULT_DEVICE == "cpu" else DEFAULT_DEVICE)
    manager = getattr(model.synthesizer.tts_model, "speaker_manager", None)
    return sorted(manager.name_to_id.keys()) if manager else []


def _xtts_speak(text: str, speaker: str, language: str, speed: float,
                device: str) -> bytes:
    def render(dev: str) -> bytes:
        model = _get_xtts(dev)
        with _synth_lock:
            buf = io.BytesIO()
            model.tts_to_file(text=text, speaker=speaker, language=language,
                              speed=speed, file_path=buf)
            return buf.getvalue()

    try:
        return render(device)
    except Exception as e:
        if "out of memory" not in str(e).lower():
            raise
        # The GPU is shared with the language model. A slow reply beats no voice.
        print("[tts] CUDA OOM — falling back to CPU for this request", flush=True)
        with _lock:
            _release_xtts()
        return render("cpu")


# --------------------------------------------------------------- service ---

def _idle_reaper() -> None:
    while True:
        time.sleep(5)
        with _lock:
            if (_xtts is not None and _idle_unload > 0
                    and time.monotonic() - _last_used > _idle_unload):
                print(f"[tts] idle {_idle_unload}s — releasing XTTS", flush=True)
                _release_xtts()


threading.Thread(target=_idle_reaper, daemon=True).start()


@app.on_event("startup")
def preload():
    """Fetch the default voice so the first spoken reply is not a download."""
    try:
        if DEFAULT_ENGINE == "piper":
            _piper_voice(DEFAULT_SPEAKER)
        print(f"[tts] ready: {DEFAULT_ENGINE} / {DEFAULT_SPEAKER}", flush=True)
    except Exception as e:
        print(f"[tts] preload failed: {e}", flush=True)


@app.get("/health")
def health():
    return {"ok": True, "engine": DEFAULT_ENGINE, "default_speaker": DEFAULT_SPEAKER,
            "xtts_loaded": _xtts_device, "piper_loaded": sorted(_piper),
            "idle_unload_seconds": _idle_unload}


@app.get("/speakers")
def speakers(engine: str = DEFAULT_ENGINE):
    try:
        return {"engine": engine,
                "speakers": piper_voices() if engine == "piper" else xtts_speakers()}
    except Exception as e:
        raise HTTPException(500, f"could not list voices: {e}")


@app.post("/speak")
def speak(text: str = Form(...),
          engine: str = Form(DEFAULT_ENGINE),
          speaker: str = Form(DEFAULT_SPEAKER),
          language: str = Form("en"),
          speed: float = Form(1.0),
          expressiveness: float = Form(0.25),
          device: str = Form(DEFAULT_DEVICE),
          idle_unload: float = Form(-1.0)):
    global _idle_unload
    if idle_unload >= 0:
        _idle_unload = idle_unload  # settings service is the source of truth

    text = text.strip()
    if not text:
        raise HTTPException(400, "empty text")

    try:
        if engine == "piper":
            audio = _piper_speak(text, speaker, speed, expressiveness)
        else:
            audio = _xtts_speak(text, speaker, language, speed, device)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"synthesis failed: {e}")

    return Response(content=audio, media_type="audio/wav")
