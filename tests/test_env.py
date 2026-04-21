"""Cover `src/env.py` — accessor wrappers over os.environ + python-dotenv.

`env.py` calls `load_dotenv()` at import time; importing it here leaks
the developer's local `.env` contents into `os.environ` for the rest of
the pytest session (dashboard auth vars, DATABASE_URL, etc.). Snapshot
the env before the import and restore to that snapshot afterwards so
downstream tests (dashboards, pipeline integration) see a clean state.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

# Snapshot BEFORE importing env so whatever load_dotenv() adds can be undone.
_PRE_IMPORT_ENV = dict(os.environ)

import env as env_mod  # noqa: E402 — deliberate ordering for env isolation

# Restore to the pre-import snapshot: drop anything load_dotenv added, and
# re-set any key it might have overwritten. Subsequent test modules in the
# pytest session now see os.environ exactly as it was before this file ran.
for _k in list(os.environ):
    if _k not in _PRE_IMPORT_ENV:
        del os.environ[_k]
for _k, _v in _PRE_IMPORT_ENV.items():
    os.environ[_k] = _v


# ---------------------------------------------------------------------------
# kalshi_env
# ---------------------------------------------------------------------------

def test_kalshi_env_defaults_to_demo(monkeypatch):
    monkeypatch.delenv("KALSHI_ENV", raising=False)
    assert env_mod.kalshi_env() == "demo"


def test_kalshi_env_honors_env(monkeypatch):
    monkeypatch.setenv("KALSHI_ENV", "prod")
    assert env_mod.kalshi_env() == "prod"


# ---------------------------------------------------------------------------
# kalshi_api_key_id
# ---------------------------------------------------------------------------

def test_kalshi_api_key_id_raises_when_missing(monkeypatch):
    monkeypatch.delenv("KALSHI_API_KEY_ID", raising=False)
    with pytest.raises(RuntimeError, match="KALSHI_API_KEY_ID"):
        env_mod.kalshi_api_key_id()


def test_kalshi_api_key_id_raises_when_empty(monkeypatch):
    monkeypatch.setenv("KALSHI_API_KEY_ID", "")
    with pytest.raises(RuntimeError, match="KALSHI_API_KEY_ID"):
        env_mod.kalshi_api_key_id()


def test_kalshi_api_key_id_returns_value(monkeypatch):
    monkeypatch.setenv("KALSHI_API_KEY_ID", "abc-123")
    assert env_mod.kalshi_api_key_id() == "abc-123"


# ---------------------------------------------------------------------------
# kalshi_private_key_path
# ---------------------------------------------------------------------------

def test_private_key_path_raises_when_missing(monkeypatch):
    monkeypatch.delenv("KALSHI_PRIVATE_KEY_PATH", raising=False)
    with pytest.raises(RuntimeError, match="KALSHI_PRIVATE_KEY_PATH is unset"):
        env_mod.kalshi_private_key_path()


def test_private_key_path_raises_when_file_missing(monkeypatch, tmp_path):
    missing = tmp_path / "no_such_key.pem"
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", str(missing))
    with pytest.raises(RuntimeError, match="does not resolve to a file"):
        env_mod.kalshi_private_key_path()


def test_private_key_path_returns_path_when_file_exists(monkeypatch, tmp_path):
    key = tmp_path / "key.pem"
    key.write_text("-----BEGIN PRIVATE KEY-----\n")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", str(key))
    result = env_mod.kalshi_private_key_path()
    assert result == key
    assert result.is_file()


def test_private_key_path_expands_tilde(monkeypatch, tmp_path):
    # Fake HOME so `~/key.pem` resolves into tmp_path.
    monkeypatch.setenv("HOME", str(tmp_path))
    key = tmp_path / "key.pem"
    key.write_text("-----BEGIN PRIVATE KEY-----\n")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", "~/key.pem")
    result = env_mod.kalshi_private_key_path()
    assert result == key


# ---------------------------------------------------------------------------
# cf_benchmarks_api_key
# ---------------------------------------------------------------------------

def test_cf_benchmarks_key_defaults_empty(monkeypatch):
    monkeypatch.delenv("CF_BENCHMARKS_API_KEY", raising=False)
    assert env_mod.cf_benchmarks_api_key() == ""


def test_cf_benchmarks_key_returns_value(monkeypatch):
    monkeypatch.setenv("CF_BENCHMARKS_API_KEY", "sk_abc")
    assert env_mod.cf_benchmarks_api_key() == "sk_abc"


# ---------------------------------------------------------------------------
# database_url
# ---------------------------------------------------------------------------

def test_database_url_defaults_to_repo_sqlite(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    url = env_mod.database_url()
    assert url.startswith("sqlite:///")
    assert url.endswith("/data/kalshi.db")
    # Default points at repo-root/data/kalshi.db — sanity-check it resolves.
    path_part = url.removeprefix("sqlite:///")
    assert Path(path_part).name == "kalshi.db"


def test_database_url_honors_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pw@host:5432/db")
    assert env_mod.database_url() == "postgresql://user:pw@host:5432/db"
