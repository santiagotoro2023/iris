#!/usr/bin/env bash
# Train a custom openWakeWord model (SPEC.md 24).
#
#   ./wakeword/train.sh "hey iris"           train and install as hey_iris
#   ./wakeword/train.sh "hey iris" --clean   also delete the ~6 GB of training data
#
# The result is installed into ./data/wakewords/, where it appears in the Wake word
# dropdown without a restart. Nothing here runs during a normal install: setup.sh
# never calls it and `docker compose up` never builds this image.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")/.."

PHRASE="${1:-}"
[ -n "$PHRASE" ] || { sed -n '2,7p' "$0" | sed 's/^# \?//'; exit 1; }
NAME="$(echo "$PHRASE" | tr '[:upper:] ' '[:lower:]_' | tr -cd '[:alnum:]_')"
DATA="$PWD/data/wakeword-training"

log() { printf '\033[38;5;208m::\033[0m %s\n' "$*"; }

command -v nvidia-smi >/dev/null || log "No GPU detected; this will be extremely slow."

log "Building the training image (torch + speechbrain + Piper, a few GB)..."
docker build -t iris-wakeword-train ./wakeword

mkdir -p "$DATA" data/wakewords data/wakeword-out
sed -e "s|__PHRASE__|$PHRASE|" -e "s|__NAME__|$NAME|" \
    wakeword/config.template.yml > "$DATA/$NAME.yml"

# -t only when there is a terminal, so an unattended run does not fail on "input
# device is not a TTY".
TTY=""; [ -t 0 ] && TTY="-it"
run() { docker run --rm $TTY --device nvidia.com/gpu=all \
          -v "$DATA:/work/datasets" -v "$PWD/data/wakeword-out:/work/out" \
          -v "$PWD/wakeword:/work/scripts:ro" iris-wakeword-train "$@"; }

log "Fetching training data (~6 GB, resumable — safe to interrupt)..."
run python3 /work/scripts/fetch_data.py

CFG="/work/datasets/$NAME.yml"
# Three stages, run separately so an interrupted run resumes at the stage it
# reached rather than regenerating 30,000 clips from scratch.
log "1/3 generating synthetic speech for \"$PHRASE\"..."
run python3 -m openwakeword.train --training_config "$CFG" --generate_clips
log "2/3 augmenting (reverb and background noise) and computing features..."
run python3 -m openwakeword.train --training_config "$CFG" --augment_clips
log "3/3 training..."
# The final tflite export needs a TensorFlow stack we deliberately do not install;
# the ONNX file we actually use is written before that step, so a failure there is
# expected and only matters if the .onnx is missing.
run python3 -m openwakeword.train --training_config "$CFG" --train_model || true

OUT="data/wakeword-out/$NAME.onnx"
[ -f "$OUT" ] || { echo "Training produced no $OUT — see the log above." >&2; exit 1; }
cp "$OUT" "data/wakewords/$NAME.onnx"
log "Installed data/wakewords/$NAME.onnx — pick \"$NAME\" in Settings > Wake word."

if [ "${2:-}" = "--clean" ]; then
  log "Removing $DATA and data/wakeword-out..."
  rm -rf "$DATA" data/wakeword-out
fi
