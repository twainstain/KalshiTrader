"""Cover `scripts/kalshi_historical_pull.py`."""

from __future__ import annotations

import importlib
import sqlite3
from unittest.mock import MagicMock

import pytest


@pytest.fixture(scope="module")
def hp():
    # Module lives under scripts/ which the conftest places on sys.path.
    return importlib.import_module("kalshi_historical_pull")


@pytest.fixture
def seeded_db(tmp_path):
    import migrate_db as m
    url = f"sqlite:///{tmp_path}/hp.db"
    m.migrate(url)
    return url, sqlite3.connect(url.removeprefix("sqlite:///"))


def test_series_for_asset_all_union(hp):
    out = hp._series_for_asset("all")
    assert set(out) == {"KXBTC15M", "KXETH15M", "KXSOL15M"}


def test_series_for_asset_unknown_raises(hp):
    with pytest.raises(ValueError, match="unknown asset"):
        hp._series_for_asset("doge")


def test_upsert_market_roundtrip(hp, seeded_db):
    _, conn = seeded_db
    market = {
        "ticker": "KXBTC15M-W1",
        "series_ticker": "KXBTC15M",
        "event_ticker": "E",
        "strike_price": 65000,
        "strike_type": "above",
        "open_time": 1_700_000_000,
        "close_time": 1_700_000_900,
        "expiration_time": 1_700_000_900,
        "result": "yes",
        "volume": 50,
    }
    hp.upsert_market(conn, market)
    conn.commit()
    row = conn.execute(
        "SELECT market_ticker, series_ticker, strike, settled_result "
        "FROM kalshi_historical_markets"
    ).fetchone()
    assert row == ("KXBTC15M-W1", "KXBTC15M", "65000", "yes")


def test_upsert_market_idempotent(hp, seeded_db):
    _, conn = seeded_db
    market = {"ticker": "T", "series_ticker": "KXBTC15M", "strike_price": 1}
    hp.upsert_market(conn, market)
    hp.upsert_market(conn, market)  # no DUP key error
    conn.commit()
    n = conn.execute(
        "SELECT COUNT(*) FROM kalshi_historical_markets"
    ).fetchone()[0]
    assert n == 1


def test_upsert_market_skips_when_no_ticker(hp, seeded_db):
    _, conn = seeded_db
    hp.upsert_market(conn, {"series_ticker": "KXBTC15M"})
    conn.commit()
    n = conn.execute(
        "SELECT COUNT(*) FROM kalshi_historical_markets"
    ).fetchone()[0]
    assert n == 0


def test_insert_trade_roundtrip(hp, seeded_db):
    _, conn = seeded_db
    hp.insert_trade(conn, {
        "ticker": "KXBTC15M-W1",
        "ts_us": 1_700_000_000_000_000,
        "yes_price": "0.42",
        "count": "5",
        "taker_side": "yes",
    })
    conn.commit()
    row = conn.execute(
        "SELECT market_ticker, price, qty, taker_side "
        "FROM kalshi_historical_trades"
    ).fetchone()
    assert row == ("KXBTC15M-W1", "0.42", "5", "yes")


def test_pull_markets_writes_via_upsert(hp, seeded_db):
    _, conn = seeded_db
    fake_client = MagicMock()
    fake_client.historical_markets.return_value = iter([
        {"ticker": "A", "series_ticker": "KXBTC15M", "strike_price": 1},
        {"ticker": "B", "series_ticker": "KXBTC15M", "strike_price": 2},
    ])
    out = hp.pull_markets(
        fake_client, series=("KXBTC15M",),
        min_close_ts=0, max_close_ts=1,
        conn=conn, dry_run=False,
    )
    assert [m["ticker"] for m in out] == ["A", "B"]
    n = conn.execute(
        "SELECT COUNT(*) FROM kalshi_historical_markets"
    ).fetchone()[0]
    assert n == 2


def test_pull_markets_dry_run_doesnt_persist(hp, seeded_db):
    _, conn = seeded_db
    fake_client = MagicMock()
    fake_client.historical_markets.return_value = iter([
        {"ticker": "A", "series_ticker": "KXBTC15M"},
    ])
    hp.pull_markets(
        fake_client, series=("KXBTC15M",),
        min_close_ts=0, max_close_ts=1,
        conn=conn, dry_run=True,
    )
    n = conn.execute(
        "SELECT COUNT(*) FROM kalshi_historical_markets"
    ).fetchone()[0]
    assert n == 0


def test_pull_trades_iterates_markets_and_writes(hp, seeded_db):
    _, conn = seeded_db
    fake_client = MagicMock()
    # Two markets × two trades each.
    fake_client.historical_trades.side_effect = [
        iter([{"ticker": "A", "yes_price": "0.40", "count": "1",
               "ts_us": 100, "taker_side": "yes"},
              {"ticker": "A", "yes_price": "0.41", "count": "2",
               "ts_us": 200, "taker_side": "no"}]),
        iter([{"ticker": "B", "yes_price": "0.60", "count": "3",
               "ts_us": 300, "taker_side": "yes"}]),
    ]
    markets = [{"ticker": "A", "series_ticker": "X"},
               {"ticker": "B", "series_ticker": "X"}]
    n = hp.pull_trades(fake_client, markets=markets, conn=conn, dry_run=False)
    assert n == 3
    count = conn.execute(
        "SELECT COUNT(*) FROM kalshi_historical_trades"
    ).fetchone()[0]
    assert count == 3
