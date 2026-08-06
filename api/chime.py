"""The sound a timer makes (SPEC.md 55).

Santiago picked a warm chime: three descending bell partials with a long soft decay,
noticeable without being startling.

Synthesised rather than shipped. A WAV is a binary blob in a public repository for
something that is forty lines of arithmetic, and generating it means the character of
the sound is a thing that can be changed rather than an asset that has to be sourced.
No numpy either: it is not in the api image and a few hundred thousand samples of
pure Python is a fifth of a second, once, at startup.

A struck bell is not a sine wave. Its partials are *inharmonic*, which is why a bell
sounds like a bell and a sine wave sounds like a hearing test, and the higher ones die
away faster, which is why a bell is bright at the strike and warm afterwards.
"""
import math
import struct
import wave
from pathlib import Path

VERSION = 1                      # bump to make an existing file regenerate
RATE = 44100
# Partials of a struck bell, as multiples of the fundamental, each with its own gain
# and how fast it dies. The higher ones decay faster: that is what turns a bright
# strike into a warm tail rather than a sustained chord.
PARTIALS = [(1.00, 1.00, 0.85), (2.00, 0.45, 0.60), (2.41, 0.30, 0.45),
            (3.00, 0.18, 0.35), (4.16, 0.09, 0.25)]
# Three strikes, descending. Rising reads as a question, descending as a statement.
STRIKES = [(0.00, 880.0), (0.42, 740.0), (0.84, 587.33)]
# Long enough for the last strike to actually ring out. The first attempt stopped at
# 2.6s with the fundamental still at a third of its amplitude, which is a sound that
# is cut off rather than one that decays, and it was audible as a click on the loop.
LENGTH = 3.5                     # seconds
ATTACK = 0.004                   # a strike with no attack at all clicks
PEAK = 0.707                     # about -3 dBFS


def _samples() -> list[float]:
    total = int(RATE * LENGTH)
    out = [0.0] * total
    for offset, root in STRIKES:
        start = int(offset * RATE)
        for i in range(start, total):
            t = (i - start) / RATE
            attack = min(1.0, t / ATTACK) if ATTACK else 1.0
            value = 0.0
            for ratio, gain, decay in PARTIALS:
                value += gain * math.exp(-t / decay) * math.sin(
                    2 * math.pi * root * ratio * t)
            out[i] += value * attack
    loudest = max((abs(v) for v in out), default=1.0) or 1.0
    return [v * PEAK / loudest for v in out]


def write_chime(path: Path) -> Path:
    frames = b"".join(struct.pack("<h", int(max(-1.0, min(1.0, v)) * 32767))
                      for v in _samples())
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(frames)
    path.with_suffix(".stamp").write_text(str(VERSION))
    return path


def ensure(path: Path) -> Path:
    """Generate it once. Regenerated only when the recipe above changes, so a restart
    is not a fifth of a second of arithmetic for no reason."""
    stamp = path.with_suffix(".stamp")
    try:
        if path.exists() and stamp.read_text().strip() == str(VERSION):
            return path
    except OSError:
        pass
    print(f"[chime] generating {path.name}", flush=True)
    return write_chime(path)


if __name__ == "__main__":
    import sys
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "alarm.wav")
    write_chime(target)
    data = _samples()
    assert len(data) == int(RATE * LENGTH), len(data)
    assert abs(max(abs(v) for v in data) - PEAK) < 1e-6, "not normalised"
    # The tail must actually be a tail: the last tenth of a second is near silence,
    # or the sound is a beep that stops rather than a bell that rings out.
    assert max(abs(v) for v in data[-RATE // 10:]) < 0.05 * PEAK, "no decay"
    # The strike is where the energy is; a chime that peaks at the end is inverted.
    assert max(abs(v) for v in data[:RATE // 10]) > 0.5 * PEAK, "no strike"
    print(f"wrote {target} ({target.stat().st_size} bytes), self-check passed")
