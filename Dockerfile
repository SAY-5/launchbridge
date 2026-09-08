# syntax=docker/dockerfile:1.7
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY launchbridge ./launchbridge
COPY fakes ./fakes
COPY smoke ./smoke
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


FROM python:3.12-slim AS runtime

ARG GIT_SHA=unknown
LABEL org.opencontainers.image.title="launchbridge" \
      org.opencontainers.image.source="https://github.com/SAY-5/launchbridge" \
      org.opencontainers.image.revision="${GIT_SHA}"

RUN groupadd --system --gid 1001 app \
    && useradd --system --uid 1001 --gid app --home /app app

WORKDIR /app
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app launchbridge ./launchbridge
COPY --chown=app:app fakes ./fakes
COPY --chown=app:app smoke ./smoke
COPY --chown=app:app alembic.ini destinations.yaml ./

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    GIT_SHA="${GIT_SHA}"

USER app
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz')"]

CMD ["uvicorn", "launchbridge.app:app", "--host", "0.0.0.0", "--port", "8080"]
