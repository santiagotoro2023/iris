FROM nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04

RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-pip ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install --no-cache-dir \
      faster-whisper==1.1.0 requests==2.32.3 \
      fastapi==0.115.6 uvicorn==0.34.0 python-multipart==0.0.20

WORKDIR /app
COPY server.py .
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8001"]
