FROM python:3.11-slim AS base

WORKDIR /app

# System deps for cryptography + psycopg2 build.
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential libssl-dev libffi-dev libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml /app/
COPY lib/ /app/lib/
RUN pip install --no-cache-dir -U pip && \
    pip install --no-cache-dir -e . && \
    if [ -d lib/trading_platform ]; then pip install --no-cache-dir -e lib/trading_platform || true; fi

COPY src/ /app/src/
COPY scripts/ /app/scripts/
COPY config/ /app/config/

ENV PYTHONPATH=/app/src:/app/lib/trading_platform/src:/app/scripts

# Default: shadow + paper executor. Override CMD for live.
CMD ["python3.11", "src/run_kalshi_shadow.py", "--primary-strategy", "pure_lag", "--paper-executor", "--with-kraken", "--interval-s", "1.0"]
