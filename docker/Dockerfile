FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 TZ=UTC UV_LINK_MODE=copy
RUN pip install --no-cache-dir uv==0.11.* && apt-get update && apt-get install -y --no-install-recommends chrony tzdata \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev
COPY config ./config
COPY scripts ./scripts
ENTRYPOINT ["uv", "run", "--no-sync", "cat"]
