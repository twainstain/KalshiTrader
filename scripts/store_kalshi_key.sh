#!/usr/bin/env bash
# Store a Kalshi API key pair on this machine and wire it into .env.
#
# The PEM is written to ~/.kalshi/<env>_private_key.pem (chmod 600), the dir
# is created with mode 700 if missing, and the repo's .env is updated with
# KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_PATH, KALSHI_ENV — preserving any
# other vars already in .env.
#
# Usage:
#   scripts/store_kalshi_key.sh                           # interactive (clipboard)
#   scripts/store_kalshi_key.sh --env demo --key-id xxx   # still reads PEM from clipboard
#   scripts/store_kalshi_key.sh --file /tmp/key.pem       # read PEM from a file
#   scripts/store_kalshi_key.sh --stdin < key.pem         # read PEM from stdin
#   scripts/store_kalshi_key.sh --force ...               # overwrite an existing PEM
#
# Exits non-zero on: missing key ID, invalid PEM, PEM already exists without
# --force, or failure to write .env. Never echoes the PEM contents.

set -euo pipefail

# ---- defaults ----
ENV_NAME="demo"
KEY_ID=""
PEM_SOURCE="clipboard"   # clipboard | file | stdin
PEM_FILE=""
FORCE=0

# ---- arg parsing ----
while [[ $# -gt 0 ]]; do
  case "$1" in
    --env)      ENV_NAME="$2"; shift 2 ;;
    --key-id)   KEY_ID="$2"; shift 2 ;;
    --file)     PEM_SOURCE="file"; PEM_FILE="$2"; shift 2 ;;
    --stdin)    PEM_SOURCE="stdin"; shift ;;
    --force)    FORCE=1; shift ;;
    -h|--help)
      sed -n '2,22p' "$0"
      exit 0
      ;;
    *)
      echo "unknown arg: $1" >&2
      exit 2
      ;;
  esac
done

case "$ENV_NAME" in
  demo|prod) ;;
  *) echo "--env must be 'demo' or 'prod' (got '$ENV_NAME')" >&2; exit 2 ;;
esac

# ---- repo-root detection ----
if ! REPO_ROOT="$(git -C "$(dirname "$0")" rev-parse --show-toplevel 2>/dev/null)"; then
  echo "not inside a git repo (run from the KalshiTrader repo)" >&2
  exit 2
fi
ENV_FILE="$REPO_ROOT/.env"
ENV_EXAMPLE="$REPO_ROOT/.env.example"

# ---- prompt for key ID if absent ----
if [[ -z "$KEY_ID" ]]; then
  read -r -p "Kalshi API Key ID: " KEY_ID
fi
if [[ -z "$KEY_ID" ]]; then
  echo "key ID is required" >&2
  exit 2
fi

# ---- dest paths ----
KALSHI_DIR="$HOME/.kalshi"
PEM_DEST="$KALSHI_DIR/${ENV_NAME}_private_key.pem"

if [[ -e "$PEM_DEST" && "$FORCE" -ne 1 ]]; then
  echo "refusing to overwrite existing $PEM_DEST (pass --force)" >&2
  exit 1
fi

mkdir -p "$KALSHI_DIR"
chmod 700 "$KALSHI_DIR"

# ---- read PEM into memory ----
PEM_CONTENT=""
case "$PEM_SOURCE" in
  clipboard)
    if ! command -v pbpaste >/dev/null 2>&1; then
      echo "pbpaste not found — use --file or --stdin on non-macOS" >&2
      exit 2
    fi
    PEM_CONTENT="$(pbpaste)"
    ;;
  file)
    if [[ ! -r "$PEM_FILE" ]]; then
      echo "cannot read $PEM_FILE" >&2
      exit 2
    fi
    PEM_CONTENT="$(cat "$PEM_FILE")"
    ;;
  stdin)
    PEM_CONTENT="$(cat)"
    ;;
esac

# ---- validate PEM structure ----
# We never print the key body. Only structural markers.
if ! printf '%s' "$PEM_CONTENT" | head -1 | grep -q -- '-----BEGIN .* PRIVATE KEY-----'; then
  echo "input does not start with a PEM BEGIN marker" >&2
  exit 1
fi
if ! printf '%s' "$PEM_CONTENT" | tail -1 | grep -q -- '-----END .* PRIVATE KEY-----'; then
  echo "input does not end with a PEM END marker" >&2
  exit 1
fi

# ---- write PEM atomically ----
umask 077
TMP_PEM="$(mktemp "${PEM_DEST}.XXXXXX")"
# Ensure the temp file is cleaned up on failure.
trap 'rm -f "$TMP_PEM"' EXIT
printf '%s\n' "$PEM_CONTENT" > "$TMP_PEM"
chmod 600 "$TMP_PEM"
mv "$TMP_PEM" "$PEM_DEST"
trap - EXIT

# ---- update .env idempotently ----
if [[ ! -f "$ENV_FILE" ]]; then
  if [[ -f "$ENV_EXAMPLE" ]]; then
    cp "$ENV_EXAMPLE" "$ENV_FILE"
    echo "created $ENV_FILE from .env.example"
  else
    : > "$ENV_FILE"
  fi
fi
chmod 600 "$ENV_FILE"

set_env_var() {
  local key="$1" value="$2"
  # Use a Python one-liner for the rewrite — sed's escaping rules vs file
  # paths with slashes + ampersands + backslashes is a minefield. Python
  # handles it cleanly and is already a hard dep.
  python3 - "$ENV_FILE" "$key" "$value" <<'PY'
import os, sys
path, key, value = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    with open(path) as f:
        lines = f.readlines()
except FileNotFoundError:
    lines = []
seen = False
out = []
for line in lines:
    if line.startswith(f"{key}="):
        out.append(f"{key}={value}\n")
        seen = True
    else:
        out.append(line)
if not seen:
    if out and not out[-1].endswith("\n"):
        out[-1] = out[-1] + "\n"
    out.append(f"{key}={value}\n")
with open(path, "w") as f:
    f.writelines(out)
PY
}

set_env_var "KALSHI_API_KEY_ID" "$KEY_ID"
set_env_var "KALSHI_PRIVATE_KEY_PATH" "$PEM_DEST"
set_env_var "KALSHI_ENV" "$ENV_NAME"

# ---- summary ----
cat <<MSG

Stored Kalshi $ENV_NAME credentials.
  PEM:  $PEM_DEST  (chmod 600)
  .env: $ENV_FILE updated
    KALSHI_API_KEY_ID=<hidden>
    KALSHI_PRIVATE_KEY_PATH=$PEM_DEST
    KALSHI_ENV=$ENV_NAME

Back up the PEM to your password manager — Kalshi cannot recover it.

Next:
  pip install -e ".[dev]"
  python3.11 -c "import os, sys; sys.path.insert(0, 'src'); \\
    from market.kalshi_market import make_client; print(make_client().get_balance())"

If the key ever leaks, revoke it at https://${ENV_NAME}.kalshi.co/account/profile
and re-run this script with a fresh key pair (add --force to overwrite).
MSG
