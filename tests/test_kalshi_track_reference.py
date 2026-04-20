"""Cover `scripts/kalshi_track_reference.py`."""

from __future__ import annotations

import importlib
import sqlite3
from unittest.mock import MagicMock

import pytest


@pytest.fixture(scope="module")
def kt():
    return importlib.import_module("kalshi_track_reference")


# --------- fetch_coinbase_tick ---------

def test_fetch_coinbase_returns_tick_on_200(kt):
    session = MagicMock()
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"price": "65000.50"}
    session.get.return_value = resp
    tick = kt.fetch_coinbase_tick("btc", session=session)
    assert tick is not None
    assert tick.asset == "btc"
    assert str(tick.price) == "65000.50"
    assert tick.src == "coinbase"


def test_fetch_coinbase_unknown_asset_returns_none(kt):
    session = MagicMock()
    tick = kt.fetch_coinbase_tick("doge", session=session)
    assert tick is None


def test_fetch_coinbase_error_status_returns_none(kt):
    session = MagicMock()
    resp = MagicMock(status_code=503, text="maintenance")
    session.get.return_value = resp
    tick = kt.fetch_coinbase_tick("btc", session=session)
    assert tick is None


def test_fetch_coinbase_transport_error_returns_none(kt):
    import requests
    session = MagicMock()
    session.get.side_effect = requests.ConnectionError("boom")
    assert kt.fetch_coinbase_tick("btc", session=session) is None


def test_fetch_coinbase_bad_json_returns_none(kt):
    session = MagicMock()
    resp = MagicMock(status_code=200, text="not json")
    resp.json.side_effect = ValueError("bad json")
    session.get.return_value = resp
    assert kt.fetch_coinbase_tick("btc", session=session) is None


def test_fetch_coinbase_missing_price_returns_none(kt):
    session = MagicMock()
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"time": "2026-04-20T00:00:00Z"}
    session.get.return_value = resp
    assert kt.fetch_coinbase_tick("btc", session=session) is None


# --------- run loop ---------

def test_run_writes_ticks_for_requested_iterations(kt, tmp_path, monkeypatch):
    import migrate_db as m
    url = f"sqlite:///{tmp_path}/rt.db"
    m.migrate(url)
    conn = sqlite3.connect(url.removeprefix("sqlite:///"))

    fake_session = MagicMock()
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"price": "65000.12"}
    fake_session.get.return_value = resp

    # Zero interval so the loop doesn't wait.
    n = kt.run(
        assets=("btc",), interval_s=0, iterations=3,
        conn=conn, is_postgres=False, session=fake_session,
    )
    assert n == 3
    rows = conn.execute("SELECT asset, price FROM reference_ticks").fetchall()
    assert len(rows) == 3
    assert all(asset == "btc" for asset, _ in rows)
    conn.close()


def test_run_skips_missing_ticks_but_continues(kt, tmp_path):
    import migrate_db as m
    url = f"sqlite:///{tmp_path}/rt2.db"
    m.migrate(url)
    conn = sqlite3.connect(url.removeprefix("sqlite:///"))

    fake_session = MagicMock()
    # First call → 503, second → 200, third → 200.
    responses = [
        MagicMock(status_code=503, text="down"),
        MagicMock(status_code=200),
        MagicMock(status_code=200),
    ]
    responses[1].json.return_value = {"price": "1"}
    responses[2].json.return_value = {"price": "2"}
    fake_session.get.side_effect = responses

    n = kt.run(
        assets=("btc",), interval_s=0, iterations=3,
        conn=conn, is_postgres=False, session=fake_session,
    )
    # Two successful writes despite one failure.
    assert n == 2
    conn.close()


def test_run_without_conn_still_accepts_ticks(kt):
    fake_session = MagicMock()
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"price": "1"}
    fake_session.get.return_value = resp
    n = kt.run(
        assets=("btc",), interval_s=0, iterations=2,
        conn=None, is_postgres=False, session=fake_session,
    )
    assert n == 2
