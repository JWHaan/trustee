# Two stages so the image ships a built UI without shipping node_modules: a Node
# stage builds the arena UI, and the Python stage serves the built files.

FROM node:22-alpine AS ui
WORKDIR /ui
COPY arena/ui/package.json arena/ui/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY arena/ui/ ./
RUN npm run build

FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app

# Dependencies come from uv.lock (--frozen), so `cryptography` (needed for
# mandate signatures) is pinned to the locked version. --no-dev skips pytest,
# ruff and mypy.
COPY pyproject.toml uv.lock ./
COPY src/ ./src/
RUN pip install --quiet uv && uv sync --frozen --no-dev

COPY --from=ui /ui/dist ./arena/ui/dist
ENV ARENA_UI_DIR=/app/arena/ui/dist

# Run as an unprivileged user. The app only writes to memory (or to paths the
# operator sets), so the image's files stay root-owned and read-only to it.
RUN useradd --system --uid 10001 --create-home trustee
USER trustee

EXPOSE 8000
CMD ["sh", "-c", "uv run --no-sync python -m uvicorn trustee.arena.api:app --host 0.0.0.0 --port ${PORT:-8000}"]
