FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first so source edits do not invalidate this layer.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-editable --no-install-project

COPY src ./src
RUN uv sync --locked --no-dev --no-editable

RUN groupadd --system --gid 10001 pdl \
    && useradd --system --uid 10001 --gid pdl --no-create-home --shell /usr/sbin/nologin pdl \
    && mkdir -p /data \
    && chown pdl:pdl /data

ENV PATH="/app/.venv/bin:$PATH"
USER 10001:10001

EXPOSE 8787
VOLUME /data

# GATEWAY_PORT must match listen_port in the gateway config (default 8787).
ENV GATEWAY_PORT=8787
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen(f\"http://127.0.0.1:{os.environ.get('GATEWAY_PORT', '8787')}/healthz\", timeout=4)"]

CMD ["pdl-gateway"]
