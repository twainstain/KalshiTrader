"""Cover `src/run_kalshi_backtest.py` — DB iter + scoring + report."""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

import run_kalshi_backtest as bt
import migrate_db as m
from strategy.kalshi_fair_value import FairValueModel


@pytest.fixture
def seeded_db(tmp_path):
    """A sqlite DB with 1 BTC window: spot far above strike, resolved Yes."""
    url = f"sqlite:///{tmp_path}/bt.db"
    m.migrate(url)
    path = url.removeprefix("sqlite:///")
    conn = sqlite3.connect(path)

    expiration_ts = 1_746_000_000
    decision_ts_us = (expiration_ts - 30) * 1_000_000
    window_start_us = decision_ts_us - 60_000_000

    conn.execute(
        "INSERT INTO kalshi_historical_markets "
        "(market_ticker, series_ticker, event_ticker, strike, comparator, "
        " open_ts, close_ts, expiration_ts, settled_result, volume, raw_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("KXBTC15M-W1", "KXBTC15M", "KXBTC15M-E1", "65000", "above",
         expiration_ts - 900, expiration_ts, expiration_ts,
         "yes", "100", "{}"),
    )
    # Drop 60+ reference ticks spread across the 60s window ending at decision.
    for i, dt in enumerate(range(0, 60)):
        ts = window_start_us + dt * 1_000_000 + 500_000
        conn.execute(
            "INSERT INTO reference_ticks (asset, ts_us, price, src) "
            "VALUES (?, ?, ?, ?)",
            ("btc", ts, "66000", "coinbase"),
        )
    # One trade record for the naive baseline.
    conn.execute(
        "INSERT INTO kalshi_historical_trades "
        "(market_ticker, ts_us, price, qty, taker_side) VALUES (?,?,?,?,?)",
        ("KXBTC15M-W1", decision_ts_us - 1_000_000, "0.95", "5", "yes"),
    )
    conn.commit()
    yield url, conn
    conn.close()


def test_iter_decision_rows_returns_one_row_with_expected_refs(seeded_db):
    _, conn = seeded_db
    rows = bt.iter_decision_rows(conn, decision_offset_s=30)
    assert len(rows) == 1
    r = rows[0]
    assert r.market_ticker == "KXBTC15M-W1"
    assert r.asset == "btc"
    assert r.strike == Decimal("65000")
    assert r.comparator == "above"
    assert r.realized_yes == 1
    assert r.naive_p_yes == Decimal("0.95")
    assert r.reference_price == Decimal("66000")


def test_brier_score_basic_math():
    # Perfect predictions → Brier = 0.
    assert bt.brier_score([Decimal("1"), Decimal("0")], [1, 0]) == Decimal("0")
    # Max-wrong → Brier = 1.
    assert bt.brier_score([Decimal("1"), Decimal("0")], [0, 1]) == Decimal("1")


def test_brier_score_empty_returns_none():
    assert bt.brier_score([], []) is None


def test_hit_rate_counts_argmax_matches():
    # 2 correct predictions, 1 wrong.
    hits = bt.hit_rate(
        [Decimal("0.9"), Decimal("0.2"), Decimal("0.6")],
        [1, 0, 0],
    )
    assert abs(hits - Decimal("2") / Decimal("3")) < Decimal("1e-9")


def test_calibration_groups_into_deciles():
    probs = [Decimal("0.05"), Decimal("0.15"), Decimal("0.95")]
    outcomes = [0, 0, 1]
    cal = bt.calibration_by_decile(probs, outcomes)
    # Three bins active: decile 0, 1, and 9.
    indices = {row[0] for row in cal}
    assert 0 in indices and 1 in indices and 9 in indices


def test_score_rows_applies_model_and_preserves_realized(seeded_db):
    _, conn = seeded_db
    rows = bt.iter_decision_rows(conn, decision_offset_s=30)
    model = FairValueModel(no_data_haircut=Decimal("0"))
    scored = bt.score_rows(rows, model)
    assert len(scored) == 1
    s = scored[0]
    assert s.realized_yes == 1
    # Spot 66k, strike 65k, T=30s → model expects high p_yes.
    assert s.model_p_yes > Decimal("0.9")


def test_render_report_empty_db_shows_hint():
    out = bt.render_report([], decision_offset_s=30)
    assert "No scorable rows found" in out


def test_render_report_populated_shows_summary_table(seeded_db):
    _, conn = seeded_db
    rows = bt.iter_decision_rows(conn, decision_offset_s=30)
    scored = bt.score_rows(rows, FairValueModel())
    report = bt.render_report(scored, decision_offset_s=30)
    assert "## Summary by asset" in report
    assert "## Calibration" in report
    assert "btc" in report


def test_main_runs_with_seeded_db_and_writes_report(seeded_db, tmp_path):
    url, _ = seeded_db
    report_path = tmp_path / "bt.md"
    rc = bt.main([
        "--database-url", url,
        "--decision-offset-s", "30",
        "--report", str(report_path),
    ])
    assert rc == 0
    assert report_path.is_file()
    text = report_path.read_text()
    assert "Kalshi fair-value backtest" in text
    assert "btc" in text


def test_asset_from_series_handles_known_tickers():
    assert bt._asset_from_series("KXBTC15M") == "btc"
    assert bt._asset_from_series("KXETH15M") == "eth"
    assert bt._asset_from_series("KXSOL15M") == "sol"
    # Unknown falls back to btc (conservative).
    assert bt._asset_from_series("KXDOGE15M") == "btc"
