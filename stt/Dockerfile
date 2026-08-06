FROM nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04

RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-pip ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install --no-cache-dir \
      faster-whisper==1.1.0 requests==2.32.3 \
      fastapi==0.115.6 uvicorn==0.34.0 python-multipart==0.0.20

# Speaker labels (SPEC.md 29). SpeechBrain rather than pyannote, which is gated
# behind a HuggingFace licence nothing here can accept on Santiago's behalf. It runs
# on the CPU always, so it never takes VRAM from the language model, which means the
# CPU build of torch is the right one and saves about 2 GB of image.
# Three pins, each for a break that only shows up at the first transcription rather
# than at build time: torchaudio 2.9 dropped list_audio_backends, which speechbrain
# calls on import, and huggingface_hub 0.26 dropped the use_auth_token argument
# speechbrain passes when it fetches the model.
RUN pip3 install --no-cache-dir \
      --extra-index-url https://download.pytorch.org/whl/cpu \
      torch==2.5.1 torchaudio==2.5.1 "huggingface_hub==0.25.2" \
      speechbrain==1.0.2 scikit-learn==1.5.2

WORKDIR /app
COPY server.py .
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8001"]
