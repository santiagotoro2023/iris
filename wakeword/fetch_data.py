"""Fetch the datasets openWakeWord's training pipeline needs (SPEC.md 24).

Everything lands in /work/datasets, which is ./data/wakeword-training on the host.
Each step is skipped if its output already exists, so an interrupted download is
resumed by simply running this again.

Sizes are dominated by the two pre-computed feature files (~2.2 GB together); the
audio corpora are deliberately small samples, which is what the upstream notebook
does too. More background audio makes a more robust model, so `--hours` raises it.
"""
import argparse
import os
import subprocess
import sys
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
        rirs = datasets.load_dataset("davidscripka/MIT_environmental_impulse_responses",
                                     split="train", streaming=True)
        write_wavs(rirs, ROOT / "mit_rirs")

    print("3/4 background noise from AudioSet")
    if not done(ROOT / "audioset_16k", 100):
        tar = ROOT / "bal_train09.tar"
        wget("https://huggingface.co/datasets/agkphysics/AudioSet/resolve/main/data/"
             "bal_train09.tar", tar)
        subprocess.run(["tar", "-xf", str(tar)], cwd=ROOT, check=True)
        flac = sorted(str(p) for p in (ROOT / "audio").glob("**/*.flac"))
        ds = datasets.Dataset.from_dict({"audio": flac}).cast_column(
            "audio", datasets.Audio(sampling_rate=16000))
        write_wavs(ds, ROOT / "audioset_16k")
        tar.unlink(missing_ok=True)

    print(f"4/4 background music from the Free Music Archive ({args.hours} h)")
    clips = int(args.hours * 3600 // 30)          # FMA clips are 30 s each
    if not done(ROOT / "fma", clips // 2):
        fma = datasets.load_dataset("rudraml/fma", name="small", split="train",
                                    streaming=True).cast_column(
                                        "audio", datasets.Audio(sampling_rate=16000))
        write_wavs(fma, ROOT / "fma", limit=clips)

    total = sum(f.stat().st_size for f in ROOT.rglob("*") if f.is_file())
    print(f"\nready: {total / 2**30:.1f} GB in {ROOT}")
    for d in ("mit_rirs", "audioset_16k", "fma"):
        print(f"  {d:14} {len(list((ROOT / d).glob('*.wav')))} clips")


if __name__ == "__main__":
    sys.exit(main())
