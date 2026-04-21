#!/usr/bin/env bash
# Launch the Kalshi Phase-1 dashboard.
#
# Usage:
#   ./scripts/run_dashboard.sh              # reads data/kalshi.db, port 8000
#   PORT=9000 ./scripts/run_dashboard.sh    # custom port
#   DB_URL=sqlite:///tmp/other.db ./scripts/run_dashboard.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

DB_URL="${DB_URL:-sqlite:///data/kalshi.db}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"

echo "kalshi dashboard → http://${HOST}:${PORT}/"
echo "  db: ${DB_URL}"
PYTHONPATH=src python3.11 src/run_dashboard.py \
  --database-url "$DB_URL" --host "$HOST" --port "$PORT" "$@"
