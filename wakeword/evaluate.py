"""Score a wake word model before trusting it (SPEC.md 24).

Run inside the api container, which already has openWakeWord and can reach the
voice service:

    docker compose cp wakeword/evaluate.py api:/app/evaluate.py
    docker compose exec -T api python /app/evaluate.py hey_iris "hey iris"

It has to land in /app, not /tmp: it imports `wake`, which lives beside it.

A model is only useful if the gap between the two columns is wide. The negatives
matter more than the positives: a wake word that fires on ordinary conversation is
worse than one that occasionally needs saying twice.
"""
import io
import sys
import wave

import httpx
import numpy as np

import wake

# Several voices, because a model that only answers to one is overfitted. These are
# Piper en_GB speakers, none of which the training pipeline ever saw.
VOICES = ["en_GB-cori-high", "en_GB-alba-medium", "en_GB-northern_english_male-medium"]

NEGATIVES = [
    "The capital of Switzerland is Bern, not Zurich.",
    "I need to book a train to Geneva tomorrow morning.",
    "Can you turn the heating up by two degrees please.",
    "The iris of the eye controls how much light gets in.",
    "There is a lovely iris growing by the back door.",
    "Hey Chris, are you coming to the thing on Saturday?",
]


def speak(text: str, voice: str) -> np.ndarray:
    r = httpx.post("http://tts:8002/speak", timeout=300, data={
        "text": text, "engine": "piper", "speaker": voice, "language": "en",
        "speed": "1.0", "expressiveness": "0.25", "device": "cpu",
        "idle_unload": "300"})
    r.raise_for_status()
    w = wave.open(io.BytesIO(r.content), "rb")
    a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)
    a /= 32768
    if w.getnchannels() == 2:
        a = a.reshape(-1, 2).mean(axis=1)
    n = int(len(a) * 16000 / w.getframerate())
    a = np.interp(np.linspace(0, len(a) - 1, n), np.arange(len(a)), a)
    return (a * 32767).astype(np.int16)


def peak(model, audio: np.ndarray) -> float:
    """Highest score anywhere in the clip, with silence either side so the phrase
    is not clipped at a frame boundary."""
    pad = np.concatenate([np.zeros(16000, dtype=np.int16), audio,
                          np.zeros(8000, dtype=np.int16)])
    best = 0.0
    for i in range(0, len(pad) - wake.FRAME, wake.FRAME):
        best = max(best, max(model.predict(pad[i:i + wake.FRAME]).values()))
    model.reset()
    return best


def main() -> int:
    name = sys.argv[1] if len(sys.argv) > 1 else "hey_iris"
    phrase = sys.argv[2] if len(sys.argv) > 2 else name.replace("_", " ")
    path = wake.catalogue().get(name)
    if not path:
        print(f"no such wake word model: {name}")
        return 1
    model, _ = wake._load(path)

    print(f"model: {name}   phrase: {phrase!r}\n")
    hits = []
    print("positives (should be high)")
    for voice in VOICES:
        try:
            s = peak(model, speak(f"{phrase}.", voice))
        except Exception as e:
            print(f"  {voice:38} unavailable ({type(e).__name__})")
            continue
        hits.append(s)
        print(f"  {voice:38} {s:.3f}")

    print("\nnegatives (should be near zero)")
    worst = 0.0
    for text in NEGATIVES:
        s = peak(model, speak(text, VOICES[0]))
        worst = max(worst, s)
        print(f"  {s:.3f}  {text}")

    if hits:
        print(f"\nweakest positive {min(hits):.3f}   worst negative {worst:.3f}")
        verdict = ("usable" if min(hits) > 0.5 and worst < 0.3 else
                   "marginal" if min(hits) > 0.4 else "not usable")
        print(f"verdict: {verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
