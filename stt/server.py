"""Speech-to-text service (SPEC.md Phase 2), faster-whisper behind a tiny HTTP API.

Model configuration arrives per request from the settings service, and the loaded
model is swapped only when it actually changes — so model/device/compute stay
configurable in the UI (SPEC.md 3.4) without a container restart.

Speaker labels (SPEC.md 29, "Still open in Phase 3") are done here too, with
SpeechBrain's ECAPA embeddings clustered over Whisper's own segments. Santiago chose
SpeechBrain over pyannote because pyannote is gated behind a HuggingFace licence and
nothing here can accept one on his behalf. It runs on the CPU always: the 8 GB card
is already shared by the language, speech and vision models (SPEC.md 15-16), and a
transcript with no speaker labels is a far smaller loss than a recording that fails
because the diarizer took the last of the VRAM.
"""
import asyncio
import gc
import importlib.util
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

# The GPU is shared with the language model, which holds ~5 GB and leaves too little
# for Whisper. Rather than failing the recording, fall back to the CPU and remember
# that for a while, so every following request does not pay the same failed attempt.
_cpu_until = 0.0
CPU_FALLBACK_SECONDS = 600

SAMPLE_RATE = 16000
DIARIZE_MODEL = os.environ.get("STT_DIARIZE_MODEL", "speechbrain/spkrec-ecapa-voxceleb")
# Downloaded on first use into the volume that already holds the Whisper weights, so
# a container recreate does not fetch it again.
SPEECHBRAIN_CACHE = os.environ.get("SPEECHBRAIN_CACHE", "/models/speechbrain")
# Cosine distance between two ECAPA embeddings, above which they are different
# people. Measured rather than guessed: on a seven minute recording of one person,
# 0.30 split him into fourteen speakers, 0.40 into five, 0.50 into two, and 0.60 into
# one. A room, a microphone and a codec all move this, so it stays a knob.
DIARIZE_THRESHOLD = float(os.environ.get("STT_DIARIZE_THRESHOLD", "0.60"))
# Under this, a segment is "mm" or "yes" and its embedding is mostly room noise.
MIN_SPEECH_SECONDS = 0.6
# More voice than this identifies a speaker no better and costs real CPU time.
MAX_EMBED_SECONDS = 10.0

_speaker = None
_speaker_used = 0.0
_speaker_error = ""


def _is_oom(err: Exception) -> bool:
    text = str(err).lower()
    return "out of memory" in text or "cuda failed" in text or "cublas" in text


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


def _release_speaker() -> None:
    """Caller holds _lock. Gives back RAM, not VRAM: this model is CPU only by
    design, and it is released on the same timer purely so an idle box stays idle."""
    global _speaker
    _speaker = None
    gc.collect()


def _get_speaker():
    """Lazy for the same reason the Whisper model is: most transcriptions never ask
    for speakers, and the first one that does can afford the second it costs."""
    global _speaker, _speaker_used
    with _lock:
        if _speaker is None:
            from speechbrain.inference.speaker import EncoderClassifier
            _speaker = EncoderClassifier.from_hparams(
                source=DIARIZE_MODEL, savedir=SPEECHBRAIN_CACHE,
                run_opts={"device": "cpu"})
        _speaker_used = time.monotonic()
        return _speaker


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
            if (_speaker is not None and _idle_unload > 0
                    and time.monotonic() - _speaker_used > _idle_unload):
                print(f"[stt] idle {_idle_unload}s, unloading the speaker model",
                      flush=True)
                _release_speaker()


threading.Thread(target=_idle_reaper, daemon=True).start()


def _diarization_installed() -> bool:
    """Cheap enough to answer on every health poll. Actually importing speechbrain
    drags torch in and takes seconds, so only look for the package."""
    return importlib.util.find_spec("speechbrain") is not None


def _diarize(path: str, spans: list[dict], max_speakers: int,
             threshold: float = DIARIZE_THRESHOLD) -> str:
    """Label every span in place with "Speaker 1", "Speaker 2" and so on.

    Returns "" on success, or the reason it could not. It never raises: a transcript
    without speaker labels is worth far more than a failed transcription, so every
    way this can go wrong ends as a note in the response instead.
    """
    global _speaker_error
    try:
        import numpy as np
        import torch
        from faster_whisper.audio import decode_audio
        from sklearn.cluster import AgglomerativeClustering

        model = _get_speaker()
        # Decoded here rather than reusing Whisper's copy because faster-whisper does
        # not hand it back, and this is one ffmpeg pass over a file already on disk.
        audio = decode_audio(path, sampling_rate=SAMPLE_RATE)

        # ponytail: one forward pass per segment, on one CPU core. An hour of speech
        # is a few hundred passes of at most 10s each; batch them if that ever hurts.
        vectors, indexes = [], []
        for i, span in enumerate(spans):
            start = float(span["start"])
            end = min(float(span["end"]), start + MAX_EMBED_SECONDS)
            chunk = audio[int(start * SAMPLE_RATE):int(end * SAMPLE_RATE)]
            # Whisper timestamps can run a little past the end of the audio, so check
            # what was actually sliced rather than what the timestamps promised.
            if len(chunk) < int(MIN_SPEECH_SECONDS * SAMPLE_RATE):
                continue
            with torch.no_grad():
                vector = model.encode_batch(
                    torch.from_numpy(chunk).unsqueeze(0)).reshape(-1).numpy()
            vectors.append(vector / (float(np.linalg.norm(vector)) or 1.0))
            indexes.append(i)

        if not indexes:
            return "no segment long enough to identify a speaker"
        if len(indexes) == 1:
            labels = [0]
        else:
            matrix = np.vstack(vectors)
            labels = list(AgglomerativeClustering(
                n_clusters=None, distance_threshold=threshold,
                metric="cosine", linkage="average").fit_predict(matrix))
            if 0 < max_speakers < len(set(labels)):
                # The distance split further than the caller says is possible. Trust
                # the count they gave over a threshold that is only ever a guess.
                labels = list(AgglomerativeClustering(
                    n_clusters=min(max_speakers, len(matrix)),
                    metric="cosine", linkage="average").fit_predict(matrix))
    except Exception as e:
        _speaker_error = f"{type(e).__name__}: {e}"
        return _speaker_error
    _speaker_error = ""

    # Number the speakers by who talks first. Cluster ids come out in whatever order
    # the linkage happened to build, so "Speaker 1" would otherwise be arbitrary and
    # would change between two runs over the same recording.
    names: dict[int, str] = {}
    for i, label in zip(indexes, labels):
        if label not in names:
            names[label] = f"Speaker {len(names) + 1}"
        spans[i]["speaker"] = names[label]

    # The segments too short to embed get their neighbour's speaker. Guessing one
    # would invent a speaker change in the middle of somebody's sentence; dropping
    # them would lose words that were actually said.
    previous = ""
    for span in spans:
        if span.get("speaker"):
            previous = span["speaker"]
        elif previous:
            span["speaker"] = previous
    following = ""
    for span in reversed(spans):  # anything before the first labelled segment
        if span.get("speaker"):
            following = span["speaker"]
        elif following:
            span["speaker"] = following
    return ""


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
            "on_cpu_until": max(0.0, _cpu_until - time.monotonic()),
            "diarization": _diarization_installed(),
            "diarization_error": _speaker_error,
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
    timestamps: bool = Form(False),
    diarize: bool = Form(False),
    max_speakers: int = Form(0),
    diarize_threshold: float = Form(0.0),
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
    global _cpu_until
    on_cpu = device == "cpu" or time.monotonic() < _cpu_until

    def run(dev: str, comp: str):
        whisper = _get_model(model, dev, comp)
        segments, info = whisper.transcribe(
            path,
            language=None if language == "auto" else language,
            vad_filter=vad,
            beam_size=5,
        )
        spans = [{"start": round(s.start, 2), "end": round(s.end, 2),
                  "text": s.text.strip()} for s in segments]
        return "".join(" " + s["text"] for s in spans).strip(), spans, info

    try:
        try:
            text, spans, info = run("cpu" if on_cpu else device,
                                    "int8" if on_cpu else compute)
        except Exception as e:
            if on_cpu or not _is_oom(e):
                raise
            # The language model is holding the GPU. Say so once, in the log, and
            # get the transcription done anyway.
            print(f"[stt] GPU is full ({e}); falling back to CPU for "
                  f"{CPU_FALLBACK_SECONDS}s", flush=True)
            with _lock:
                _release()
            _cpu_until = time.monotonic() + CPU_FALLBACK_SECONDS
            on_cpu = True
            text, spans, info = run("cpu", "int8")
        # Speakers after the words, and on the same file, which is why the unlink
        # below waits for it. A failure here is a note, never an exception: labels
        # are worth much less than the transcript they would have decorated.
        speaker_note = ""
        if diarize:
            speaker_note = await asyncio.to_thread(
                _diarize, path, spans, max_speakers,
                diarize_threshold or DIARIZE_THRESHOLD)
    except Exception as e:
        raise HTTPException(500, f"transcription failed: {e}")
    finally:
        os.unlink(path)

    out = {"text": text, "language": info.language,
           "language_probability": round(info.language_probability, 3),
           "duration": round(info.duration, 2),
           "device": "cpu" if on_cpu else device}
    if timestamps:
        out["segments"] = spans
    if diarize:
        # Empty means it worked. Anything else is the reason it did not, and the
        # caller says so rather than quietly returning an unlabelled transcript.
        out["diarization"] = speaker_note
    return out
