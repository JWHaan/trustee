# Two stages so the image ships a built UI without shipping node_modules.
# The plan's §4 assumed Render's Python runtime could also build a Vite app and
# that `uv run --extra all` existed; neither is true (there is no `all` extra),
# so the build is pinned here instead of guessed at deploy time.

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

COPY pyproject.toml uv.lock ./
COPY src/ ./src/
RUN pip install --quiet uv && uv sync --frozen --no-dev

COPY --from=ui /ui/dist ./arena/ui/dist
ENV ARENA_UI_DIR=/app/arena/ui/dist

EXPOSE 8000
CMD ["sh", "-c", "uv run --no-sync python -m uvicorn trustee.arena.api:app --host 0.0.0.0 --port ${PORT:-8000}"]
