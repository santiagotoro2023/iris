FROM python:3.12-slim
WORKDIR /app
# pg_dump for backups; ffmpeg pulls a frame from a camera's RTSP stream.
RUN apt-get update && apt-get install -y --no-install-recommends postgresql-client ffmpeg \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir \
      fastapi==0.115.6 httpx==0.28.1 \
      "psycopg[binary]==3.2.3" jsonschema==4.23.0 redis==5.2.1 \
      python-multipart==0.0.20 pypdf==5.1.0 python-docx==1.1.2 pillow==11.0.0 \
      "uvicorn[standard]==0.34.0"   # [standard] carries the WebSocket protocol

# Wake word detection (SPEC.md Phase 2). openWakeWord 0.6.0 declares a hard
# tflite-runtime dependency that has no Python 3.12 wheel, so a plain install
# silently resolves back to 0.4.0 and its incompatible older API. Install the deps
# explicitly and the package itself with --no-deps: tflite is never imported when
# inference_framework="onnx". Weights are ~18 MB and baked in, because
# download_models(target_directory=...) leaves the bundled Silero VAD pointing at
# the site-packages path it computed at import time.
RUN pip install --no-cache-dir \
      onnxruntime==1.20.1 scipy==1.14.1 scikit-learn==1.5.2 tqdm==4.67.1 \
      requests==2.32.3 \
 && pip install --no-cache-dir --no-deps openwakeword==0.6.0 \
 && python -c "import openwakeword.utils as u; u.download_models()"

# Every module, rather than a hand-kept list: that list went stale twice, and a
# missing module fails at import on container start rather than at build.
COPY *.py ./
COPY static ./static
COPY entrypoint.sh /entrypoint.sh
CMD ["/entrypoint.sh"]
