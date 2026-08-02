"""Speech-to-text service (SPEC.md Phase 2), faster-whisper behind a tiny HTTP API.

Model configuration arrives per request from the settings service, and the loaded
model is swapped only when it actually changes — so model/device/compute stay
configurable in the UI (SPEC.md 3.4) without a container restart.
"""
import gc
import os
import tempfile
import threading
import time

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from faster_whisper import WhisperModel

DEFAULT_MODEL = os.environ.get("STT_MODEL", "large-v3")
DEFAULT_DEVICE = os.environ.get("STT_DEVICE", "cuda")
DEFAULT_COMPUTE = os.environ.get("STT_COMPUTE", "int8_float16")

app = FastAPI(title="IRiS STT")

_lock = threading.Lock()
_loaded: tuple[str, str, str] | None = None
_model: WhisperModel | None = None
_last_used = 0.0
_idle_unload = float(os.environ.get("STT_IDLE_UNLOAD", "300"))


def _release() -> None:
    """Free VRAM. Caller holds _lock."""
    global _model, _loaded
    if _model is not None:
        del _model
    _model, _loaded = None, None
    gc.collect()


def _get_model(name: str, device: str, compute: str) -> WhisperModel:
    """One model in memory at a time — VRAM is shared with the LLM (SPEC.md 10)."""
    global _loaded, _model, _last_used
    want = (name, device, compute)
    with _lock:
        if _loaded != want:
            _release()
            _model = WhisperModel(name, device=device, compute_type=compute,
                                  download_root="/models")
            _loaded = want
        _last_used = time.monotonic()
        return _model


def _idle_reaper() -> None:
    """Whisper resident on the GPU costs the LLM ~2.3x throughput (SPEC.md 15), so
    give the VRAM back when nobody is speaking. Reload costs a few seconds."""
    while True:
        time.sleep(5)
        with _lock:
            if (_model is not None and _idle_unload > 0
                    and time.monotonic() - _last_used > _idle_unload):
                print(f"[stt] idle {_idle_unload}s — unloading {_loaded}", flush=True)
                _release()


threading.Thread(target=_idle_reaper, daemon=True).start()


@app.on_event("startup")
def preload():
    """Fetch the weights at startup so the first transcription is not a multi-GB
    surprise, then release the VRAM and let the first real request re-load it."""
    try:
        _get_model(DEFAULT_MODEL, DEFAULT_DEVICE, DEFAULT_COMPUTE)
        with _lock:
            _release()
    except Exception as e:  # a bad default must not make the service unstartable
        print(f"[stt] preload failed: {e}", flush=True)


@app.get("/health")
def health():
    return {"ok": True, "loaded": _loaded, "idle_unload_seconds": _idle_unload,
            "default": [DEFAULT_MODEL, DEFAULT_DEVICE, DEFAULT_COMPUTE]}


@app.post("/transcribe")
async def transcribe(
    audio: UploadFile = File(...),
    model: str = Form(DEFAULT_MODEL),
    device: str = Form(DEFAULT_DEVICE),
    compute: str = Form(DEFAULT_COMPUTE),
    language: str = Form("auto"),
    vad: bool = Form(True),
    idle_unload: float = Form(-1.0),
):
    global _idle_unload
    if idle_unload >= 0:
        _idle_unload = idle_unload  # settings service is the source of truth
    data = await audio.read()
    if not data:
        raise HTTPException(400, "empty audio")

    suffix = os.path.splitext(audio.filename or "")[1] or ".webm"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        f.write(data)
        path = f.name
    try:
        whisper = _get_model(model, device, compute)
        segments, info = whisper.transcribe(
            path,
            language=None if language == "auto" else language,
            vad_filter=vad,
            beam_size=5,
        )
        text = "".join(s.text for s in segments).strip()
    except Exception as e:
        raise HTTPException(500, f"transcription failed: {e}")
    finally:
        os.unlink(path)

    return {"text": text, "language": info.language,
            "language_probability": round(info.language_probability, 3),
            "duration": round(info.duration, 2)}
