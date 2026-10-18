# Multi-arch (Apple Silicon / Intel). Time comes from the host clock (macOS keeps it NTP-synced).
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 TZ=UTC UV_LINK_MODE=copy
RUN pip install --no-cache-dir "uv>=0.11,<0.12" && apt-get update \
    && apt-get install -y --no-install-recommends tzdata ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev
COPY config ./config
COPY scripts ./scripts
ENTRYPOINT ["uv", "run", "--no-sync", "cat"]
