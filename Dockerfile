# syntax=docker/dockerfile:1
# StoryBored server image. ComfyUI is NOT in here — the app talks to it purely
# over HTTP (see README "Works with any ComfyUI"); point COMFYUI_URL at yours.

# ---------- Stage 1: build the frontend ----------
FROM node:20-alpine AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ---------- Stage 2: runtime ----------
FROM python:3.11-slim

# The app resolves frontend/dist and workflows/ relative to the repo layout,
# so the image reproduces that layout under /app and installs the backend in
# editable mode (source stays at /app/backend).
WORKDIR /app

COPY backend/pyproject.toml ./backend/pyproject.toml
COPY backend/storybored/ ./backend/storybored/
RUN pip install --no-cache-dir -e ./backend

# engine packs + built UI
COPY workflows/ ./workflows/
COPY --from=frontend /build/dist ./frontend/dist/

# Non-root runtime user; /data is the single writable state dir (DATA_DIR).
RUN useradd --create-home --shell /usr/sbin/nologin storybored \
    && mkdir -p /data \
    && chown storybored:storybored /data

ENV STORYBORED_HOST=0.0.0.0 \
    STORYBORED_PORT=8600 \
    DATA_DIR=/data \
    STORYBORED_HOME=/app \
    PYTHONUNBUFFERED=1
# STORYBORED_HOST=0.0.0.0 is container-internal: the app must listen on the
# container interface for the port mapping to reach it. Actual network exposure
# is decided by the mapping — docker-compose.yml binds 127.0.0.1 by default.

VOLUME /data
EXPOSE 8600

USER storybored

# python:slim ships no curl/wget; the runtime python is the probe.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('STORYBORED_PORT','8600'),timeout=4)"

CMD ["python", "-m", "storybored"]
