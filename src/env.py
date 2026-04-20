"""Env-var loader.

Reads `.env` at repo root (if present) via python-dotenv, then exposes the
Kalshi + persistence settings the rest of the codebase needs. Import-time
side effect: `load_dotenv()` is called once. Missing required vars raise
`RuntimeError` only when the accessor is called — constants stay importable
for tests.
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # dotenv is a soft dep for test environments
    pass


_REPO_ROOT = Path(__file__).resolve().parent.parent


def kalshi_env() -> str:
    return os.environ.get("KALSHI_ENV", "demo")


def kalshi_api_key_id() -> str:
    val = os.environ.get("KALSHI_API_KEY_ID", "")
    if not val:
        raise RuntimeError("KALSHI_API_KEY_ID is unset — see .env.example")
    return val


def kalshi_private_key_path() -> Path:
    val = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")
    if not val:
        raise RuntimeError("KALSHI_PRIVATE_KEY_PATH is unset — see .env.example")
    p = Path(val).expanduser()
    if not p.is_file():
        raise RuntimeError(f"KALSHI_PRIVATE_KEY_PATH does not resolve to a file: {p}")
    return p


def cf_benchmarks_api_key() -> str:
    return os.environ.get("CF_BENCHMARKS_API_KEY", "")


def database_url() -> str:
    return os.environ.get("DATABASE_URL", f"sqlite:///{_REPO_ROOT}/data/kalshi.db")
