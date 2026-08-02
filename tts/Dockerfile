FROM nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04

RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-pip ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Torch first, from the cu124 index. The default PyPI wheel targets a newer CUDA than
# driver 550 supports and fails at runtime with "driver is too old (found 12040)";
# installing it up front stops coqui-tts pulling the incompatible one. SPEC.md 16.
RUN pip3 install --no-cache-dir --index-url https://download.pytorch.org/whl/cu124 \
      torch==2.6.0 torchaudio==2.6.0

# coqui-tts is the maintained fork of the archived Coqui TTS package.
RUN pip3 install --no-cache-dir \
      coqui-tts==0.25.1 \
      fastapi==0.115.6 uvicorn==0.34.0 python-multipart==0.0.20

ENV COQUI_TOS_AGREED=1
ENV TTS_HOME=/models
WORKDIR /app
COPY server.py .
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8002"]
