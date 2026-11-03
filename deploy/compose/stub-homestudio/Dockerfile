FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev

COPY app.py ./

RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin stub
ENV PATH="/app/.venv/bin:$PATH"
USER 10001

EXPOSE 8765
CMD ["python", "app.py"]
