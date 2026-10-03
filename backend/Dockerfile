# syntax=docker/dockerfile:1

# One image for every backend role: `api` (default command), `worker` and `migrate`
# only differ in the command they run (see deploy/compose.yaml).

# ---------------------------------------------------------------------------
# base: shared Python settings
# ---------------------------------------------------------------------------
# Literal tag, not an ARG: Dependabot only updates literal FROM lines (.github/dependabot.yml).
FROM python:3.12-slim-trixie AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# ---------------------------------------------------------------------------
# builder: resolve dependencies into /opt/venv with uv (from uv.lock)
# ---------------------------------------------------------------------------
FROM base AS builder

ARG UV_VERSION=0.12.21
RUN pip install --no-cache-dir "uv==${UV_VERSION}"

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

COPY pyproject.toml uv.lock .python-version ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# dev-builder: same environment plus the dev dependency group (pytest, ruff, mypy)
FROM builder AS dev-builder
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project

# ---------------------------------------------------------------------------
# runtime-base: unprivileged user and data directory
# ---------------------------------------------------------------------------
FROM base AS runtime-base

# ffmpeg encodes speech to Opus/MP3 (app/ai/tts/encode.py). Piper itself comes from uv.lock
# (piper-tts wheels for amd64 and arm64); voices are downloaded into /data at runtime.
# Tesseract with German and English data recognises scanned attachments (app/search/,
# OLLAMAIL_SEARCH_OCR_MODE); Debian packages exist for amd64 and arm64.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
       tesseract-ocr tesseract-ocr-deu tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 10001 ollamail \
    && useradd --system --uid 10001 --gid ollamail --home-dir /app --no-create-home \
       --shell /usr/sbin/nologin ollamail \
    && mkdir -p /data \
    && chown ollamail:ollamail /data

# ORT_DISABLE_TELEMETRY: ONNX Runtime (used by Piper) would otherwise collect usage events.
ENV PATH="/opt/venv/bin:${PATH}" \
    OLLAMAIL_DATA_DIR=/data \
    ORT_DISABLE_TELEMETRY=1

EXPOSE 8000
VOLUME ["/data"]

# ---------------------------------------------------------------------------
# dev: hot reload, source is bind-mounted by deploy/compose.dev.yaml
# ---------------------------------------------------------------------------
FROM runtime-base AS dev

COPY --from=dev-builder /opt/venv /opt/venv
COPY . .

USER 10001:10001
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--root-path", "/api", \
     "--no-proxy-headers", "--reload", "--reload-dir", "app"]

# ---------------------------------------------------------------------------
# runtime (default target): production image
# ---------------------------------------------------------------------------
FROM runtime-base AS runtime

COPY --from=builder /opt/venv /opt/venv
# Source stays owned by root, so the app user cannot modify it.
COPY . .

USER 10001:10001

# The frontend proxy serves the API under /api and strips the prefix; --root-path keeps
# generated URLs (OpenAPI docs) correct. Only the proxy can reach this port.
# X-Forwarded-* is handled by the app (OLLAMAIL_FORWARDED_ALLOW_IPS), not by uvicorn.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--root-path", "/api", \
     "--no-proxy-headers"]
