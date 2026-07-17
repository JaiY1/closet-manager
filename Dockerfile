FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    U2NET_HOME=/models/u2net

WORKDIR /app

# Native libs needed by Pillow / onnxruntime / rembg
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 libgl1 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

# Pre-download the ML models at build time so the first request isn't slow and
# runtime needs no network for them: ChromaDB's MiniLM embedder + rembg's u2net.
RUN python -c "from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2; ONNXMiniLM_L6_V2()" \
    && python -c "from rembg import new_session; new_session()"

COPY . .

# All mutable data lives on a mounted volume so redeploys don't wipe it.
ENV DATA_DIR=/data \
    PORT=8080 \
    SECURE_COOKIES=1
VOLUME ["/data"]
EXPOSE 8080

CMD ["gunicorn", "-c", "gunicorn.conf.py", "app:app"]
