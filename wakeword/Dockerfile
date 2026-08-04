# Training environment for custom wake words (SPEC.md 24).
#
# Separate from the api image on purpose: this pulls torch, speechbrain and a TTS
# stack that the running assistant has no use for. Built on demand by
# ./wakeword/train.sh, never part of `docker compose up`.
#
# Ubuntu 22.04 gives Python 3.10, which is what openWakeWord's training pipeline
# was written against — piper-phonemize has no 3.12 wheels.
FROM nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-pip python3-dev build-essential git wget ffmpeg libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

# Match the driver: a mismatched torch reports "driver is too old" and falls to CPU.
RUN pip3 install --no-cache-dir torch==2.6.0 torchaudio==2.6.0 \
      --index-url https://download.pytorch.org/whl/cu124

RUN pip3 install --no-cache-dir \
      numpy==1.26.4 scipy==1.14.1 tqdm==4.67.1 pyyaml==6.0.2 \
      torchinfo==1.8.0 torchmetrics==1.2.0 speechbrain==0.5.14 \
      audiomentations==0.33.0 torch-audiomentations==0.11.0 acoustics==0.2.6 \
      mutagen==1.47.0 pronouncing==0.2.0 datasets==2.14.6 \
      deep-phonemizer==0.0.19 webrtcvad-wheels==2.0.14 piper-phonemize==1.1.0 \
      onnx==1.17.0 onnxruntime==1.20.1 requests==2.32.3 scikit-learn==1.5.2

# datasets 2.14.6 (the version the upstream pipeline pins) calls pa.PyExtensionType,
# which pyarrow removed in 15. Its own layer so the large install above stays cached.
RUN pip3 install --no-cache-dir "pyarrow==14.0.2"

# openWakeWord itself, same --no-deps reasoning as the api image: its metadata
# demands a tflite-runtime that does not exist for this Python either.
RUN pip3 install --no-cache-dir --no-deps openwakeword==0.6.0 \
 && python3 -c "import openwakeword.utils as u; u.download_models()"

# The synthetic voice generator that produces the positive examples.
RUN git clone --depth 1 https://github.com/rhasspy/piper-sample-generator /opt/piper-sample-generator \
 && wget -q -O /opt/piper-sample-generator/models/en_US-libritts_r-medium.pt \
      https://github.com/rhasspy/piper-sample-generator/releases/download/v2.0.0/en_US-libritts_r-medium.pt

WORKDIR /work
