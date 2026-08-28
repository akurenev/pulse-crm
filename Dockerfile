# syntax=docker/dockerfile:1.7

ARG NODE_VERSION=22
ARG PYTHON_VERSION=3.13

FROM node:${NODE_VERSION}-alpine AS frontend-builder
WORKDIR /src/frontend

# The production image talks to the same-origin FastAPI API. Standalone Vite
# builds keep demo mode unless this build argument is supplied explicitly.
ARG VITE_API_MODE=remote
ENV VITE_API_MODE=${VITE_API_MODE}

COPY frontend/package.json frontend/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm \
    npm ci

COPY frontend/ ./
RUN npm run build


FROM python:${PYTHON_VERSION}-slim AS backend-builder
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /src

COPY backend/ ./backend/
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip wheel --wheel-dir=/wheels ./backend


FROM python:${PYTHON_VERSION}-slim AS runtime
LABEL org.opencontainers.image.title="Pulse CRM" \
      org.opencontainers.image.source="https://github.com/akurenev/pulse-crm" \
      org.opencontainers.image.licenses="Apache-2.0"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PULSE_STATIC_DIR=/app/backend/static \
    PORT=8000

RUN groupadd --system pulse \
    && useradd --system --gid pulse --home-dir /app pulse

WORKDIR /app

COPY --from=backend-builder /wheels/ /tmp/wheels/
RUN python -m pip install --no-cache-dir /tmp/wheels/* \
    && rm -rf /tmp/wheels

# Keep migrations and package metadata in the image; the Python package itself is
# installed from the wheel above.
COPY --chown=pulse:pulse backend/ ./backend/
COPY --from=frontend-builder --chown=pulse:pulse /src/frontend/dist/ ./backend/static/
COPY --chown=pulse:pulse scripts/docker-entrypoint.sh scripts/run_migrations.py ./scripts/
RUN chmod +x /app/scripts/docker-entrypoint.sh

USER pulse
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.getenv('PORT', '8000') + '/health/live', timeout=3)" || exit 1

ENTRYPOINT ["/app/scripts/docker-entrypoint.sh"]
CMD ["sh", "-c", "exec uvicorn app.main:app --app-dir /app/backend --host 0.0.0.0 --port \"${PORT:-8000}\" --workers 1 --proxy-headers --forwarded-allow-ips '*' "]
