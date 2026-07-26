# syntax=docker/dockerfile:1

# ---- build ----------------------------------------------------------------
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src

RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install ".[full]"

# ---- runtime --------------------------------------------------------------
FROM python:3.12-slim

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data

# zbar is needed for local QR decoding; fonts for Persian/Unicode rendering.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libzbar0 \
        fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 selfbot

COPY --from=builder /opt/venv /opt/venv
COPY --chown=selfbot:selfbot assets /app/assets

WORKDIR /app
USER selfbot

VOLUME ["/data"]

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import selfbot; import sys; sys.exit(0)"

ENTRYPOINT ["python", "-m", "selfbot"]
