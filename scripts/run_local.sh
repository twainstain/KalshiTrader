#!/usr/bin/env bash
# Full local dev stack: shadow scanner (+ paper executor) + dashboard.
#
# Runs both in the foreground, tails their logs to stdout, and shuts
# down cleanly on Ctrl-C.
#
# Usage:
#   ./scripts/run_local.sh
#   STRATEGY=stat_model ./scripts/run_local.sh          # different strategy
#   PAPER_EXECUTOR=0 ./scripts/run_local.sh              # disable paper fills
#   DASHBOARD_PORT=9000 ./scripts/run_local.sh           # different port
#   INTERVAL_S=2.0 ./scripts/run_local.sh                # slower tick
#   WITH_KRAKEN=0 ./scripts/run_local.sh                 # Coinbase-only reference
#
# Env vars (defaults in []):
#   DB_URL=[sqlite:///data/kalshi.db]    DB connection string
#   STRATEGY=[pure_lag]                  stat_model | partial_avg | pure_lag
#   PAPER_EXECUTOR=[1]                   1 = route decisions through RiskEngine + paper executor
#   DASHBOARD_PORT=[8000]
#   DASHBOARD_HOST=[127.0.0.1]
#   INTERVAL_S=[1.0]                     scanner tick interval
#   WITH_KRAKEN=[1]                      add Kraken WS as 2nd reference
#   VERBOSE=[1]                          pass -v to scanner + dashboard

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

LOGS_DIR="$REPO_ROOT/logs"
mkdir -p "$LOGS_DIR"

# --- config -----------------------------------------------------------------
DB_URL="${DB_URL:-sqlite:///data/kalshi.db}"
STRATEGY="${STRATEGY:-pure_lag}"
PAPER_EXECUTOR="${PAPER_EXECUTOR:-1}"
DASHBOARD_PORT="${DASHBOARD_PORT:-8000}"
DASHBOARD_HOST="${DASHBOARD_HOST:-127.0.0.1}"
INTERVAL_S="${INTERVAL_S:-1.0}"
WITH_KRAKEN="${WITH_KRAKEN:-1}"
VERBOSE="${VERBOSE:-1}"

# --- warn if port already bound --------------------------------------------
if lsof -i ":${DASHBOARD_PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "✗ port ${DASHBOARD_PORT} is already in use." >&2
  echo "  something (probably another dashboard) is bound there." >&2
  echo "  stop it first, or override: DASHBOARD_PORT=9000 $0" >&2
  exit 2
fi

# --- apply migrations (idempotent, safe to re-run) -------------------------
echo "→ applying migrations to $DB_URL"
PYTHONPATH=src python3.11 scripts/migrate_db.py --database-url "$DB_URL" 2>&1 | tail -3

# --- logs + PID tracking ---------------------------------------------------
TS="$(date +%Y%m%d_%H%M%S)"
SCANNER_LOG="${LOGS_DIR}/scanner_${TS}.log"
DASHBOARD_LOG="${LOGS_DIR}/dashboard_${TS}.log"
PIDS=()

cleanup() {
  echo ""
  echo "→ shutting down…"
  for pid in "${PIDS[@]}"; do
    if kill -0 "$pid" 2>/dev/null; then
      kill -TERM "$pid" 2>/dev/null || true
    fi
  done
  # Give them a couple seconds to cleanly exit (esp. the scanner, which
  # closes WS connections), then SIGKILL any stragglers.
  sleep 2
  for pid in "${PIDS[@]}"; do
    if kill -0 "$pid" 2>/dev/null; then
      kill -KILL "$pid" 2>/dev/null || true
    fi
  done
  echo "  done. logs preserved at ${LOGS_DIR}/*_${TS}.log"
}
trap cleanup EXIT INT TERM

# --- build scanner args ----------------------------------------------------
SCANNER_ARGS=(
  --primary-strategy "$STRATEGY"
  --interval-s "$INTERVAL_S"
  --database-url "$DB_URL"
)
[[ "$PAPER_EXECUTOR" == "1" ]] && SCANNER_ARGS+=(--paper-executor)
[[ "$WITH_KRAKEN"    == "1" ]] && SCANNER_ARGS+=(--with-kraken)
[[ "$VERBOSE"        == "1" ]] && SCANNER_ARGS+=(-v)

# --- launch scanner --------------------------------------------------------
echo "→ starting scanner (strategy=${STRATEGY}, paper_executor=${PAPER_EXECUTOR}, interval=${INTERVAL_S}s)"
PYTHONPATH=src python3.11 src/run_kalshi_shadow.py "${SCANNER_ARGS[@]}" \
  >"$SCANNER_LOG" 2>&1 &
SCANNER_PID=$!
PIDS+=("$SCANNER_PID")

# --- launch dashboard ------------------------------------------------------
DASHBOARD_ARGS=(
  --database-url "$DB_URL"
  --host "$DASHBOARD_HOST"
  --port "$DASHBOARD_PORT"
)
[[ "$VERBOSE" == "1" ]] && DASHBOARD_ARGS+=(-v)

echo "→ starting dashboard (${DASHBOARD_HOST}:${DASHBOARD_PORT})"
PYTHONPATH=src python3.11 src/run_dashboard.py "${DASHBOARD_ARGS[@]}" \
  >"$DASHBOARD_LOG" 2>&1 &
DASHBOARD_PID=$!
PIDS+=("$DASHBOARD_PID")

# --- wait for dashboard to accept connections ------------------------------
for i in {1..20}; do
  if curl -s -o /dev/null "http://${DASHBOARD_HOST}:${DASHBOARD_PORT}/api/overview"; then
    break
  fi
  sleep 0.5
done

cat <<EOF

══════════════════════════════════════════════════════════════════════════
  Kalshi scanner running locally
──────────────────────────────────────────────────────────────────────────
  Dashboard:   http://${DASHBOARD_HOST}:${DASHBOARD_PORT}/
  Scanner PID: ${SCANNER_PID}   log: ${SCANNER_LOG}
  Dashboard PID: ${DASHBOARD_PID}   log: ${DASHBOARD_LOG}
  DB:          ${DB_URL}
──────────────────────────────────────────────────────────────────────────
  Stream scanner logs:
    tail -f ${SCANNER_LOG}
  Stream dashboard logs:
    tail -f ${DASHBOARD_LOG}
  Ctrl-C to stop both processes.
══════════════════════════════════════════════════════════════════════════

EOF

# --- block until a child dies or Ctrl-C ------------------------------------
# `wait -n` is bash 4+. macOS ships bash 3.2, so poll instead.
while true; do
  for pid in "${PIDS[@]}"; do
    if ! kill -0 "$pid" 2>/dev/null; then
      echo ""
      echo "✗ process $pid exited unexpectedly. see logs."
      exit 1
    fi
  done
  sleep 2
done
