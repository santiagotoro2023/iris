FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir \
      fastapi==0.115.6 uvicorn==0.34.0 httpx==0.28.1 \
      "psycopg[binary]==3.2.3" jsonschema==4.23.0 redis==5.2.1 \
      python-multipart==0.0.20
COPY main.py settings.py auth.py reasoning.py persona.py activity.py chat.py voice.py ./
COPY static ./static
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
