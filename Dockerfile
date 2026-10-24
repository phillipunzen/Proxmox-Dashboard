# syntax=docker/dockerfile:1

# --- Stage 1: build the SvelteKit SPA ---------------------------------------------------------
# Static files are platform independent: build once on the native builder platform.
FROM --platform=$BUILDPLATFORM node:22-alpine AS frontend
WORKDIR /src
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# --- Stage 2: Python runtime ------------------------------------------------------------------
FROM python:3.13-slim AS runtime
# FORWARDED_ALLOW_IPS: only trust X-Forwarded-For from these addresses (your reverse proxy).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    STATIC_DIR=/app/static \
    DATA_DIR=/data \
    FORWARDED_ALLOW_IPS=127.0.0.1

WORKDIR /app
COPY backend/pyproject.toml ./
COPY backend/app ./app
RUN pip install . && useradd --system --uid 10001 --home /app dashboard \
    && mkdir -p /data && chown dashboard:dashboard /data
COPY --from=frontend /src/build ./static

USER dashboard
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=4)" || exit 1

# One worker on purpose: the collector and the in-memory snapshot live in this process.
# --proxy-headers honours X-Forwarded-* only from FORWARDED_ALLOW_IPS (read by uvicorn).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
