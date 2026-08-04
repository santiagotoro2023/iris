"""Fetch the datasets openWakeWord's training pipeline needs (SPEC.md 24).

Everything lands in /work/datasets, which is ./data/wakeword-training on the host.
Each step is skipped if its output already exists, so an interrupted download is
resumed by simply running this again.

Sizes are dominated by the pre-computed negative features: 17.3 GB, not the ~2 GB
upstream implies. The audio corpora are deliberately small samples, as in the
upstream notebook. More background audio makes a more robust model, so `--hours`
raises it.
"""
import argparse
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import numpy as np
import scipy.io.wavfile
from tqdm import tqdm

ROOT = Path("/work/datasets")
FEATURES = "https://huggingface.co/datasets/davidscripka/openwakeword_features/resolve/main"


def done(path: Path, minimum: int = 1) -> bool:
    if path.is_dir():
        return len(list(path.iterdir())) >= minimum
    return path.exists() and path.stat().st_size > 0


def wget(url: str, target: Path) -> None:
    if done(target):
        print(f"  have {target.name}")
        return
    print(f"  downloading {target.name} ...")
    # -c so a killed download resumes rather than restarting several GB.
    subprocess.run(["wget", "-cq", "--show-progress", "-O", str(target), url], check=True)


def write_wavs(rows, out: Path, limit: int | None = None) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for i, row in enumerate(tqdm(rows, total=limit)):
        if limit and i >= limit:
            break
        audio = row["audio"]
        name = Path(audio["path"]).stem + ".wav"
        scipy.io.wavfile.write(out / name, 16000,
                               (audio["array"] * 32767).astype(np.int16))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=2.0,
                    help="hours of background music to mix in")
    args = ap.parse_args()
    ROOT.mkdir(parents=True, exist_ok=True)
    os.chdir(ROOT)
    import datasets

    print("1/4 pre-computed features (~2.2 GB, the bulk of this)")
    wget(f"{FEATURES}/openwakeword_features_ACAV100M_2000_hrs_16bit.npy",
         ROOT / "ACAV100M.npy")
    wget(f"{FEATURES}/validation_set_features.npy", ROOT / "validation_set_features.npy")

    print("2/4 room impulse responses (reverb, so it works across a room)")
    if not done(ROOT / "mit_rirs", 100):
      try:
        rirs = datasets.load_dataset("davidscripka/MIT_environmental_impulse_responses",
                                     split="train", streaming=True)
        write_wavs(rirs, ROOT / "mit_rirs")
      except Exception as e:
        print(f"  skipped: {e}")

    # Household sounds, not music: dogs, doors, traffic, appliances, people. The
    # upstream notebook used an AudioSet tar that no longer exists (that repository
    # is parquet now, and the pinned `datasets` cannot stream the new layout).
    # ESC-50 is a direct, ungated zip of exactly this kind of audio.
    print("3/4 environmental noise from ESC-50")
    if not done(ROOT / "esc50_16k", 100):
      try:
        zipped = ROOT / "esc50.zip"
        wget("https://github.com/karolpiczak/ESC-50/archive/refs/heads/master.zip",
             zipped)
        # stdlib rather than the unzip binary: adding an apt package would
        # invalidate the torch layer and force a half-hour rebuild.
        with zipfile.ZipFile(zipped) as z:
            z.extractall(ROOT)
        clips = sorted((ROOT / "ESC-50-master" / "audio").glob("*.wav"))
        out = ROOT / "esc50_16k"
        out.mkdir(exist_ok=True)
        for clip in tqdm(clips):
            # Already 16-bit wav, but at 44.1 kHz; ffmpeg is the least fussy resampler
            # and is in this image for piper-sample-generator anyway.
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(clip),
                            "-ar", "16000", "-ac", "1", str(out / clip.name)],
                           check=True)
        zipped.unlink(missing_ok=True)
        shutil.rmtree(ROOT / "ESC-50-master", ignore_errors=True)
      except Exception as e:
        print(f"  skipped: {e}")

    print(f"4/4 background music from the Free Music Archive ({args.hours} h)")
    clips = int(args.hours * 3600 // 30)          # FMA clips are 30 s each
    if not done(ROOT / "fma", clips // 2):
      try:
        fma = datasets.load_dataset("rudraml/fma", name="small", split="train",
                                    streaming=True).cast_column(
                                        "audio", datasets.Audio(sampling_rate=16000))
        write_wavs(fma, ROOT / "fma", limit=clips)
      except Exception as e:
        print(f"  skipped: {e}")

    total = sum(f.stat().st_size for f in ROOT.rglob("*") if f.is_file())
    print(f"\nready: {total / 2**30:.1f} GB in {ROOT}")
    for d in ("mit_rirs", "esc50_16k", "fma"):
        n = len(list((ROOT / d).glob("*.wav"))) if (ROOT / d).is_dir() else 0
        print(f"  {d:14} {n} clips")


if __name__ == "__main__":
    sys.exit(main())
