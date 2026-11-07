# Build stage: resolve and install dependencies with uv into a virtualenv.
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

# Runtime stage: slim image, non-root user, only the venv and app code.
FROM python:3.12-slim AS server
RUN useradd --create-home --uid 10001 flagship
WORKDIR /app
COPY --from=build --chown=flagship:flagship /app /app
RUN mkdir -p /app/data && chown flagship:flagship /app/data
USER flagship
ENV PATH="/app/.venv/bin:$PATH" HOST=0.0.0.0 PORT=8788 FLAGSHIP_DB=/app/data/flagship.db
EXPOSE 8788
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8788/healthz', timeout=2)"
CMD ["flagship", "serve"]

# Example app that uses the SDK: same venv, plus the example code.
FROM server AS shop
COPY --chown=flagship:flagship examples/shop ./examples/shop
ENV FLAGSHIP_CACHE=/app/data/flagship-cache.json
HEALTHCHECK NONE
CMD ["python", "examples/shop/app.py"]
