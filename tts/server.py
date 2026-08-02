"""Text-to-speech service (SPEC.md Phase 2).

XTTS v2 with one of its built-in speakers — Santiago chose option (b), a built-in
speaker shaped through pacing and delivery rather than a cloned voice.

Same VRAM discipline as the STT service: this GPU holds the language model too,
so the voice model is released when idle (SPEC.md 15).
"""
import gc
import io
import os
import threading
import time

import torch
from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import Response

os.environ.setdefault("COQUI_TOS_AGREED", "1")  # XTTS v2 ships under the CPML

MODEL_NAME = os.environ.get("TTS_MODEL", "tts_models/multilingual/multi-dataset/xtts_v2")
DEFAULT_SPEAKER = os.environ.get("TTS_SPEAKER", "Daisy Studious")
DEFAULT_DEVICE = os.environ.get("TTS_DEVICE", "cuda")
IDLE_UNLOAD = float(os.environ.get("TTS_IDLE_UNLOAD", "300"))

app = FastAPI(title="IRiS TTS")

_lock = threading.Lock()
# The client pipelines sentences, so requests overlap. One model object is not safe
# to drive concurrently, so synthesis is serialised — the win is that sentence N+1
# renders while N is still playing, not that two render at once (SPEC.md 16).
_synth_lock = threading.Lock()
_tts = None
_loaded_device: str | None = None
_last_used = 0.0
_idle_unload = IDLE_UNLOAD


def _release() -> None:
    """Free VRAM. Caller holds _lock."""
    global _tts, _loaded_device
    if _tts is not None:
        del _tts
    _tts, _loaded_device = None, None
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


def _get(device: str):
    global _tts, _loaded_device, _last_used
    with _lock:
        if _loaded_device != device:
            _release()
            from TTS.api import TTS  # imported lazily: pulls in torch, slow at boot
            _tts = TTS(MODEL_NAME).to(device)
            _loaded_device = device
        _last_used = time.monotonic()
        return _tts


def _idle_reaper() -> None:
    while True:
        time.sleep(5)
        with _lock:
            if (_tts is not None and _idle_unload > 0
                    and time.monotonic() - _last_used > _idle_unload):
                print(f"[tts] idle {_idle_unload}s — unloading", flush=True)
                _release()


threading.Thread(target=_idle_reaper, daemon=True).start()


def _speakers() -> list[str]:
    model = _get(DEFAULT_DEVICE)
    manager = getattr(model.synthesizer.tts_model, "speaker_manager", None)
    return sorted(manager.name_to_id.keys()) if manager else []


@app.on_event("startup")
def preload():
    """Fetch the weights now so the first spoken reply is not a 1.8 GB wait, then
    hand the VRAM back and let the first real request reload it."""
    try:
        _get(DEFAULT_DEVICE)
        with _lock:
            _release()
        print("[tts] weights ready", flush=True)
    except Exception as e:  # a bad default must not make the service unstartable
        print(f"[tts] preload failed: {e}", flush=True)


@app.get("/health")
def health():
    return {"ok": True, "loaded": _loaded_device, "model": MODEL_NAME,
            "idle_unload_seconds": _idle_unload, "default_speaker": DEFAULT_SPEAKER}


@app.get("/speakers")
def speakers():
    try:
        return {"speakers": _speakers()}
    except Exception as e:
        raise HTTPException(500, f"could not list speakers: {e}")


@app.post("/speak")
def speak(text: str = Form(...),
          speaker: str = Form(DEFAULT_SPEAKER),
          language: str = Form("en"),
          speed: float = Form(1.0),
          device: str = Form(DEFAULT_DEVICE),
          idle_unload: float = Form(-1.0)):
    global _idle_unload
    if idle_unload >= 0:
        _idle_unload = idle_unload  # settings service is the source of truth

    text = text.strip()
    if not text:
        raise HTTPException(400, "empty text")

    try:
        model = _get(device)
        with _synth_lock:
            buf = io.BytesIO()
            model.tts_to_file(text=text, speaker=speaker, language=language,
                              speed=speed, file_path=buf)
            audio = buf.getvalue()
    except Exception as e:
        raise HTTPException(500, f"synthesis failed: {e}")

    return Response(content=audio, media_type="audio/wav")
