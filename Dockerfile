FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy \
    DESK_DATA_DIR=/data DESK_INBOX_DIR=/data/inbox DESK_ARCHIVE_DIR=/data/archive \
    DESK_CONFIG_DIR=/app/config TZ=Europe/Berlin

RUN apt-get update && apt-get install -y --no-install-recommends sqlite3 tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /uvx /bin/

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY desk ./desk
COPY config ./config
COPY docs ./docs
COPY prompts ./prompts
COPY tests/fixtures ./tests/fixtures
RUN uv sync --frozen --no-dev

VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s --retries=3 CMD ["uv", "run", "python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status==200 else 1)"]

CMD ["uv", "run", "--no-sync", "uvicorn", "desk.web.app:app_factory", "--factory", "--host", "0.0.0.0", "--port", "8000"]
