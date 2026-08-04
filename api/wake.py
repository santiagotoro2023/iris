"""Wake word and turn-taking (SPEC.md Phase 2).

The microphone belongs to the browser, so the browser streams 16 kHz mono PCM up
this WebSocket and this module is only the *ear*: it watches for the wake word,
decides when a turn has ended, and hands back a transcript. Everything after that
— reasoning, speech, storage — is the ordinary chat path, which hands-free mode
therefore reuses whole instead of growing a second pipeline.

openWakeWord runs here rather than in the stt service because the WebSocket has to
terminate where the session cookie is understood, and splitting the two would mean
proxying every 80 ms frame through a second hop for nothing.
"""
import asyncio
import glob
import io
import json
import os
import wave

import numpy as np
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

import activity
import auth
import settings
import voice

SAMPLE_RATE = 16000
FRAME = 1280              # 80 ms — openWakeWord's native step
VAD_FRAME = 480           # 30 ms — Silero's native step
FRAME_SECONDS = FRAME / SAMPLE_RATE

# Barge-in is judged on sustained speech, not one loud frame, or a cough stops IRiS.
BARGE_SECONDS = 0.32
# Enough pre-roll that an interruption is not clipped to "...op talking".
PREROLL_FRAMES = 13       # ~1 s
# openWakeWord fires at the END of the phrase, so a wake needs no pre-roll at all.
REFRACTORY_SECONDS = 1.5

CUSTOM_DIR = os.environ.get("WAKE_MODEL_DIR", "/wakewords")

router = APIRouter(prefix="/voice", tags=["voice"])

_bundled_cache: dict[str, str] | None = None


def _bundled() -> dict[str, str]:
    """Wake models baked into the image. Import is deferred: openWakeWord drags in
    onnxruntime and scipy, and a missing package must degrade to 'no wake words'
    rather than stop the whole API from starting."""
    global _bundled_cache
    if _bundled_cache is None:
        try:
            import openwakeword
            # MODELS points at the .tflite copy, which has no Python 3.12 runtime.
            # Both formats are downloaded, so use the ONNX sibling of each.
            found = {}
            for name, spec in openwakeword.MODELS.items():
                onnx = os.path.splitext(spec["model_path"])[0] + ".onnx"
                if os.path.exists(onnx):
                    found[name] = onnx
            _bundled_cache = found
        except Exception as e:
            print(f"[wake] openWakeWord unavailable: {e}", flush=True)
            _bundled_cache = {}
    return dict(_bundled_cache)


def catalogue() -> dict[str, str]:
    """Selectable wake words: the bundled set plus anything dropped into the volume.

    Rescanned on every call, so a newly trained model appears in the dropdown
    without a restart (SPEC.md 3.4 — configure in the UI, never on the CLI)."""
    custom = {os.path.splitext(os.path.basename(p))[0]: p
              for p in sorted(glob.glob(os.path.join(CUSTOM_DIR, "*.onnx")))}
    return {**_bundled(), **custom}


def _wake_models() -> list[str]:
    """Never return an empty list: an empty enum would make the stored value fail
    validation on the next settings write."""
    names = set(catalogue())
    if "voice.wake_model" in settings.REGISTRY:
        names.add(settings.get("voice.wake_model"))
        names.add(settings.REGISTRY["voice.wake_model"]["default"])
    return sorted(names)


settings.setting(
    "voice.hands_free", type="boolean", default=False,
    title="Hands-free listening",
    description="Keep the microphone open and wait for the wake word. Off by default "
                "because it holds the microphone for as long as the page is open.")
settings.setting(
    "voice.wake_model", type="string", enum=_wake_models, default="computer",
    title="Wake word",
    description="Which phrase wakes IRiS. There is no public model for 'IRiS' yet, so "
                "the default is 'computer': not somebody's name, and it scores 0.99 on "
                "its own phrase against 0.001 on unrelated speech. A trained model "
                "dropped into data/wakewords appears here on its own.")
settings.setting(
    "voice.wake_sensitivity", type="number", minimum=0.1, maximum=0.95, default=0.5,
    title="Wake word sensitivity",
    description="Score a phrase must reach to count as the wake word. Lower wakes more "
                "readily and misfires more; higher needs a clearer say of it.")
settings.setting(
    "voice.end_silence", type="number", minimum=0.3, maximum=3.0, default=0.8,
    title="End of turn silence (seconds)",
    description="How long you have to stop talking before IRiS decides your turn is "
                "over. Raise it if it cuts you off mid-thought.")
settings.setting(
    "voice.max_utterance", type="integer", minimum=3, maximum=60, default=15,
    title="Longest utterance (seconds)",
    description="Hard limit on one turn, so a stuck microphone cannot record forever.")
settings.setting(
    "voice.wake_timeout", type="number", minimum=1.0, maximum=15.0, default=6.0,
    title="Give up after wake (seconds)",
    description="If nothing is said after the wake word, IRiS stops listening again.")
settings.setting(
    "voice.barge_in", type="boolean", default=True,
    title="Interrupt while speaking",
    description="Talking over IRiS stops it and captures what you said. Relies on the "
                "browser cancelling the echo of its own voice; turn off if it keeps "
                "interrupting itself on loudspeakers.")


def _load(model_path: str):
    """Blocking: builds three ONNX sessions. Called via a thread."""
    from openwakeword.model import Model
    from openwakeword.vad import VAD
    return (Model(wakeword_models=[model_path], inference_framework="onnx"), VAD())


def _wav(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


class _Turn:
    """The listening state machine for one connection.

    States: sleeping -> (wake) -> waiting -> (speech) -> capturing -> transcribe.
    While IRiS is speaking the wake word is deliberately NOT watched for; only
    barge-in is, so IRiS saying its own name cannot wake it.
    """

    def __init__(self, oww, vad):
        self.oww, self.vad = oww, vad
        self.state = "sleeping"
        self.speaking = False          # IRiS is playing audio
        self.pcm = bytearray()         # the utterance being captured
        self.pre: list[bytes] = []     # rolling pre-roll
        self.vbuf = np.zeros(0, dtype=np.int16)
        self.silence = 0.0
        self.elapsed = 0.0
        self.voiced = 0.0
        self.cooldown = 0.0

    def _voice_score(self, frame: np.ndarray) -> float:
        """Silero only accepts whole 30 ms frames, and 80 ms is not a multiple of it,
        so the remainder is carried to the next call rather than dropped."""
        self.vbuf = np.concatenate([self.vbuf, frame])
        usable = len(self.vbuf) // VAD_FRAME * VAD_FRAME
        if not usable:
            return 0.0
        chunk, self.vbuf = self.vbuf[:usable], self.vbuf[usable:]
        return float(self.vad.predict(chunk))

    def _begin(self, preroll: bool) -> None:
        self.pcm = bytearray(b"".join(self.pre) if preroll else b"")
        self.silence = self.elapsed = self.voiced = 0.0

    def feed(self, frame: np.ndarray) -> str | None:
        """One 80 ms frame in, an event name out (or None). Pure state, no I/O."""
        raw = frame.tobytes()
        self.pre.append(raw)
        del self.pre[:-PREROLL_FRAMES]
        self.cooldown = max(0.0, self.cooldown - FRAME_SECONDS)

        if self.state in ("waiting", "capturing"):
            self.pcm += raw
            self.elapsed += FRAME_SECONDS

        speech = self._voice_score(frame) if self._needs_vad() else 0.0
        threshold = settings.get("voice.wake_sensitivity")

        if self.speaking:
            if self.state == "capturing":
                return None                       # already listening to the barge-in
            if not settings.get("voice.barge_in"):
                return None
            self.voiced = self.voiced + FRAME_SECONDS if speech > 0.5 else 0.0
            if self.voiced >= BARGE_SECONDS:
                self.state = "capturing"
                self._begin(preroll=True)
                return "barge_in"
            return None

        if self.state == "sleeping":
            if self.cooldown > 0:
                return None
            scores = self.oww.predict(frame)
            if scores and max(scores.values()) >= threshold:
                self.state = "waiting"
                self._begin(preroll=False)
                return "wake"
            return None

        if self.state == "waiting":
            if speech > 0.5:
                self.state = "capturing"
                self.silence = 0.0
                return "listening"
            if self.elapsed >= settings.get("voice.wake_timeout"):
                return self.sleep()
            return None

        # capturing
        self.silence = 0.0 if speech > 0.5 else self.silence + FRAME_SECONDS
        if self.silence >= settings.get("voice.end_silence"):
            return "done"
        if self.elapsed >= settings.get("voice.max_utterance"):
            return "done"
        return None

    def _needs_vad(self) -> bool:
        return self.state in ("waiting", "capturing") or (
            self.speaking and settings.get("voice.barge_in"))

    def sleep(self) -> str:
        """Back to watching for the wake word, without re-firing on the audio that
        is still sitting in openWakeWord's buffers."""
        self.state = "sleeping"
        self.pcm = bytearray()
        self.cooldown = REFRACTORY_SECONDS
        try:
            self.oww.reset()
        except Exception:
            pass
        return "idle"


@router.websocket("/listen")
async def listen(ws: WebSocket):
    # active_user raises HTTPException, which Starlette can only answer with an HTTP
    # response — invalid on a WebSocket scope. Translate it to a policy close.
    try:
        user = await auth.active_user(await auth.current_user(ws))
    except HTTPException:
        await ws.close(code=1008)
        return
    await ws.accept()

    name = settings.get("voice.wake_model")
    path = catalogue().get(name)
    if not path:
        await ws.send_json({"type": "error",
                            "detail": f"wake word model '{name}' is not installed"})
        await ws.close()
        return

    try:
        oww, vad = await asyncio.to_thread(_load, path)
    except Exception as e:
        await ws.send_json({"type": "error", "detail": f"wake model failed to load: {e}"})
        await ws.close()
        return

    turn = _Turn(oww, vad)
    await ws.send_json({"type": "ready", "wake_word": name})
    await activity.record("voice.listen", f"hands-free on, waiting for {name}",
                          user["username"])

    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if msg.get("text") is not None:
                # Only the client knows when playback actually starts and stops.
                ctl = json.loads(msg["text"])
                if ctl.get("type") == "speaking":
                    turn.speaking = bool(ctl.get("value"))
                    if not turn.speaking:
                        turn.voiced = 0.0
                continue
            data = msg.get("bytes")
            if not data or len(data) < FRAME * 2:
                continue

            event = turn.feed(np.frombuffer(data[:FRAME * 2], dtype=np.int16))
            if event in ("wake", "listening", "idle", "barge_in"):
                await ws.send_json({"type": event})
            elif event == "done":
                await ws.send_json({"type": "thinking"})
                audio = _wav(bytes(turn.pcm))
                turn.sleep()
                try:
                    result = await voice.transcribe_bytes(audio, "utterance.wav",
                                                          "audio/wav")
                except HTTPException as e:
                    await ws.send_json({"type": "error", "detail": str(e.detail)})
                    continue
                text = (result.get("text") or "").strip()
                if text:
                    await ws.send_json({"type": "transcript", "text": text,
                                        "language": result.get("language")})
                    await activity.record("voice.listen", f"heard: {text[:120]}",
                                          user["username"])
                else:
                    await ws.send_json({"type": "idle"})
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"[wake] listener failed: {e}", flush=True)
