"""Cover `scripts/migrate_db.py` — schema creation + insert/select per table."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import migrate_db as m


@pytest.fixture
def sqlite_url(tmp_path) -> str:
    return f"sqlite:///{tmp_path}/k.db"


@pytest.fixture
def sqlite_path(sqlite_url: str) -> str:
    return sqlite_url.removeprefix("sqlite:///")


def test_migrate_creates_all_phase1_tables(sqlite_url, sqlite_path):
    m.migrate(sqlite_url)
    conn = sqlite3.connect(sqlite_path)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    finally:
        conn.close()
    table_names = {r[0] for r in rows}
    for expected in m.ALL_TABLES:
        assert expected in table_names, f"missing table: {expected}"


def test_migrate_is_idempotent(sqlite_url, sqlite_path):
    m.migrate(sqlite_url)
    # Second run must not error (all IF NOT EXISTS).
    m.migrate(sqlite_url)
    conn = sqlite3.connect(sqlite_path)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
        ).fetchone()[0]
    finally:
        conn.close()
    # 5 domain tables; sqlite_sequence may or may not exist depending on inserts.
    assert count >= len(m.ALL_TABLES)


def test_insert_select_roundtrip_per_table(sqlite_url, sqlite_path):
    m.migrate(sqlite_url)
    conn = sqlite3.connect(sqlite_path)
    try:
        conn.execute(
            "INSERT INTO kalshi_historical_markets "
            "(market_ticker, series_ticker, event_ticker, strike, comparator, "
            " open_ts, close_ts, expiration_ts, settled_result, volume, raw_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("KXBTC15M-T", "KXBTC15M", "KXBTC15M-E", "65000", "above",
             1, 2, 3, "yes", "100", json.dumps({"k": "v"})),
        )
        conn.execute(
            "INSERT INTO kalshi_historical_trades "
            "(market_ticker, ts_us, price, qty, taker_side) VALUES (?,?,?,?,?)",
            ("KXBTC15M-T", 1_000, "0.42", "5", "yes"),
        )
        conn.execute(
            "INSERT INTO kalshi_live_book_snapshots "
            "(market_ticker, ts_us, seq, yes_bids_json, no_bids_json, warning_flags) "
            "VALUES (?,?,?,?,?,?)",
            ("KXBTC15M-T", 2_000, 7, "[]", "[]", ""),
        )
        conn.execute(
            "INSERT INTO reference_ticks (asset, ts_us, price, src) VALUES (?,?,?,?)",
            ("btc", 3_000, "64999.50", "coinbase"),
        )
        conn.execute(
            "INSERT INTO shadow_decisions "
            "(market_ticker, ts_us, p_yes, ci_width, reference_price, "
            " reference_60s_avg, time_remaining_s, best_yes_ask, best_no_ask, "
            " book_depth_yes_usd, book_depth_no_usd, recommended_side, "
            " hypothetical_fill_price, hypothetical_size_contracts, "
            " expected_edge_bps_after_fees, fee_bps_at_decision) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("KXBTC15M-T", 4_000, "0.5", "0.08", "64999.5", "64995.1", "45",
             "0.42", "0.58", "1500", "1800", "yes", "0.42", "50", "120", "35"),
        )
        conn.commit()

        assert conn.execute(
            "SELECT COUNT(*) FROM kalshi_historical_markets"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM kalshi_historical_trades"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM kalshi_live_book_snapshots"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM reference_ticks"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM shadow_decisions"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_main_defaults_to_sqlite(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    url = f"sqlite:///{tmp_path}/main_default.db"
    rc = m.main(["--database-url", url])
    assert rc == 0
    assert Path(url.removeprefix("sqlite:///")).is_file()


def test_main_rejects_unknown_scheme():
    with pytest.raises(ValueError, match="Unsupported"):
        m.migrate("mysql://bogus/nope")


def test_shadow_decisions_indexes_exist(sqlite_url, sqlite_path):
    m.migrate(sqlite_url)
    conn = sqlite3.connect(sqlite_path)
    try:
        idx = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='shadow_decisions'"
        ).fetchall()}
    finally:
        conn.close()
    assert "idx_sd_ticker" in idx
    assert "idx_sd_ts" in idx
    assert "idx_sd_outcome" in idx
