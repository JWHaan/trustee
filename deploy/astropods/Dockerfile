# One image recipe for both hosted agents. Each astropods.yml passes `args: AGENT: northwind|tidewater`, so the
# two blueprints build two images that differ only in which agent they start. The build context is the repository
# root. Astropods routes to port 80 for an agent that serves its own HTTP surface.
FROM python:3.12-slim
ARG AGENT
ENV AGENT=${AGENT} \
    PORT=80 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY src/ ./src/
RUN pip install --quiet uv && uv sync --frozen --no-dev

# Port 80 needs CAP_NET_BIND_SERVICE; the container runs unprivileged, so grant it to the interpreter only.
RUN apt-get update -qq \
    && apt-get install -y -qq --no-install-recommends libcap2-bin curl \
    && setcap 'cap_net_bind_service=+ep' "$(readlink -f /app/.venv/bin/python)" \
    && apt-get clean
RUN useradd --system --uid 10001 --create-home trustee
USER trustee

EXPOSE 80
CMD ["sh", "-c", "exec /app/.venv/bin/python -m trustee.network.hosted \"$AGENT\""]
