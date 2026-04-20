"""Cover `src/execution/kalshi_shadow_evaluator.py` + run loop (P1-M4-T07)."""

from __future__ import annotations

import sqlite3
import threading
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from core.models import MarketQuote
from execution.kalshi_shadow_evaluator import (
    KalshiShadowEvaluator,
    ShadowConfig,
)
from strategy.kalshi_fair_value import (
    FairValueModel,
    KalshiFairValueStrategy,
    StrategyConfig,
)


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    import migrate_db as m
    url = f"sqlite:///{tmp_path}/shadow.db"
    m.migrate(url)
    conn = sqlite3.connect(url.removeprefix("sqlite:///"))
    yield conn, url
    conn.close()


def _mq(**overrides) -> MarketQuote:
    base = dict(
        venue="kalshi",
        market_ticker="KXBTC15M-T",
        series_ticker="KXBTC15M",
        event_ticker="KXBTC15M-E",
        best_yes_ask=Decimal("0.55"),
        best_no_ask=Decimal("0.45"),
        best_yes_bid=Decimal("0.54"),
        best_no_bid=Decimal("0.44"),
        book_depth_yes_usd=Decimal("500"),
        book_depth_no_usd=Decimal("500"),
        fee_bps=Decimal("35"),
        expiration_ts=Decimal("1746000000"),
        strike=Decimal("65000"),
        comparator="above",
        reference_price=Decimal("66000"),   # spot above strike → yes
        reference_60s_avg=Decimal("66000"),
        time_remaining_s=Decimal("30"),
        quote_timestamp_us=1_746_000_000_000_000,
    )
    base.update(overrides)
    return MarketQuote(**base)


@pytest.fixture
def evaluator(db):
    conn, _ = db
    market = MagicMock()
    market.get_quotes.return_value = [_mq()]
    market.is_healthy.return_value = True
    reference = MagicMock()
    reference.get_spot.return_value = Decimal("66000")
    reference.get_60s_avg.return_value = Decimal("66000")

    strategy = KalshiFairValueStrategy(
        FairValueModel(no_data_haircut=Decimal("0")),
        StrategyConfig(
            min_edge_bps_after_fees=Decimal("50"),
            max_ci_width=Decimal("0.50"),
        ),
    )
    return KalshiShadowEvaluator(
        market_source=market,
        reference_source=reference,
        strategy=strategy,
        market_meta_by_ticker={
            "KXBTC15M-T": {
                "series_ticker": "KXBTC15M", "event_ticker": "E",
                "strike": "65000", "comparator": "above",
                "expiration_ts": 1_746_000_000, "asset": "btc",
            },
        },
        asset_by_ticker={"KXBTC15M-T": "btc"},
        conn=conn,
        resolution_lookup=None,
        now_us=lambda: 1_746_000_000_000_000,
    )


# ----------------------------------------------------------------------
# tick() writes shadow_decisions
# ----------------------------------------------------------------------

def test_tick_writes_one_decision_row(evaluator, db):
    conn, _ = db
    result = evaluator.tick()
    assert result["written"] == 1
    row = conn.execute(
        "SELECT market_ticker, recommended_side, realized_outcome "
        "FROM shadow_decisions"
    ).fetchone()
    assert row[0] == "KXBTC15M-T"
    assert row[1] == "yes"  # spot far above strike → yes edge
    assert row[2] is None  # not reconciled yet


def test_tick_skips_when_asset_not_mapped(db):
    conn, _ = db
    market = MagicMock()
    market.get_quotes.return_value = [_mq(market_ticker="UNKNOWN-T")]
    reference = MagicMock()
    reference.get_spot.return_value = Decimal("1")
    reference.get_60s_avg.return_value = Decimal("1")

    ev = KalshiShadowEvaluator(
        market_source=market,
        reference_source=reference,
        strategy=KalshiFairValueStrategy(FairValueModel()),
        market_meta_by_ticker={},
        asset_by_ticker={},  # empty map — skip
        conn=conn,
    )
    ev.tick()
    n = conn.execute("SELECT COUNT(*) FROM shadow_decisions").fetchone()[0]
    assert n == 0


def test_tick_writes_nothing_when_strategy_rejects(db):
    conn, _ = db
    market = MagicMock()
    # Book too thin — strategy rejects.
    market.get_quotes.return_value = [
        _mq(book_depth_yes_usd=Decimal("1"), book_depth_no_usd=Decimal("1")),
    ]
    ev = KalshiShadowEvaluator(
        market_source=market,
        reference_source=MagicMock(get_spot=lambda a: None, get_60s_avg=lambda a: None),
        strategy=KalshiFairValueStrategy(
            FairValueModel(), StrategyConfig(min_book_depth_usd=Decimal("500")),
        ),
        market_meta_by_ticker={"KXBTC15M-T": {
            "series_ticker": "KXBTC15M", "event_ticker": "E",
            "strike": "65000", "comparator": "above",
            "expiration_ts": 1_746_000_000, "asset": "btc",
        }},
        asset_by_ticker={"KXBTC15M-T": "btc"},
        conn=conn,
    )
    result = ev.tick()
    assert result["written"] == 0


def test_tick_registers_reconcile_for_scored_markets(evaluator):
    evaluator.tick()
    pending = evaluator.pending_reconciles
    assert "KXBTC15M-T" in pending
    assert pending["KXBTC15M-T"].expiration_ts == 1_746_000_000


# ----------------------------------------------------------------------
# Reconciler
# ----------------------------------------------------------------------

def _build_reconcile_evaluator(db, resolve_fn, *, now_us: int | None = None):
    conn, _ = db
    market = MagicMock()
    market.get_quotes.return_value = [_mq()]
    reference = MagicMock()
    reference.get_spot.return_value = Decimal("66000")
    reference.get_60s_avg.return_value = Decimal("66000")

    default_now_us = now_us if now_us is not None else 1_746_000_000_000_000
    ev = KalshiShadowEvaluator(
        market_source=market,
        reference_source=reference,
        strategy=KalshiFairValueStrategy(
            FairValueModel(no_data_haircut=Decimal("0")),
            StrategyConfig(
                min_edge_bps_after_fees=Decimal("50"),
                max_ci_width=Decimal("0.50"),
            ),
        ),
        market_meta_by_ticker={"KXBTC15M-T": {
            "series_ticker": "KXBTC15M", "event_ticker": "E",
            "strike": "65000", "comparator": "above",
            "expiration_ts": 1_746_000_000, "asset": "btc",
        }},
        asset_by_ticker={"KXBTC15M-T": "btc"},
        conn=conn,
        resolution_lookup=resolve_fn,
        config=ShadowConfig(reconcile_delay_s=30, reconcile_max_attempts=3),
        now_us=lambda: default_now_us,
    )
    return ev, conn


def test_reconciler_writes_realized_outcome(db):
    """When resolution_lookup returns 'yes', rows get realized_outcome='yes'."""
    resolve = MagicMock(return_value={"result": "yes"})
    # Push now past expiration + delay so reconcile_pending runs.
    now_us = (1_746_000_000 + 60) * 1_000_000
    ev, conn = _build_reconcile_evaluator(db, resolve, now_us=now_us)
    ev.tick()  # writes 1 row + registers reconcile
    result = ev.tick()  # now reconcile fires
    assert result["reconciled"] >= 1
    rows = conn.execute(
        "SELECT realized_outcome, realized_pnl_usd FROM shadow_decisions"
    ).fetchall()
    assert any(r[0] == "yes" for r in rows)
    # P/L for side=yes, fill=0.55, size=10, outcome=yes → (1-0.55)*10 = 4.50
    pnls = [Decimal(str(r[1])) for r in rows if r[1] is not None]
    assert Decimal("4.5") in pnls


def test_reconciler_handles_no_outcome_correctly(db):
    """When resolution_lookup returns 'no', yes-side decisions lose their fill."""
    resolve = MagicMock(return_value={"result": "no"})
    now_us = (1_746_000_000 + 60) * 1_000_000
    ev, conn = _build_reconcile_evaluator(db, resolve, now_us=now_us)
    ev.tick()
    ev.tick()
    pnl = conn.execute(
        "SELECT realized_pnl_usd FROM shadow_decisions WHERE realized_outcome='no'"
    ).fetchone()
    # side=yes, fill=0.55, size=10, outcome=no → (0-0.55)*10 = -5.5
    assert Decimal(str(pnl[0])) == Decimal("-5.5")


def test_reconciler_treats_no_data_as_no(db):
    """no_data → resolves to No (CRYPTO15M.pdf §0.5)."""
    resolve = MagicMock(return_value={"result": "no_data"})
    now_us = (1_746_000_000 + 60) * 1_000_000
    ev, conn = _build_reconcile_evaluator(db, resolve, now_us=now_us)
    ev.tick()
    ev.tick()
    row = conn.execute(
        "SELECT realized_outcome, realized_pnl_usd FROM shadow_decisions"
    ).fetchone()
    assert row[0] == "no_data"
    # Same P/L as `no` — yes-side buyer loses their fill.
    assert Decimal(str(row[1])) == Decimal("-5.5")


def test_reconciler_skips_when_resolution_not_ready(db):
    """If lookup returns None, don't apply; retries bump attempts."""
    resolve = MagicMock(return_value=None)
    now_us = (1_746_000_000 + 60) * 1_000_000
    ev, conn = _build_reconcile_evaluator(db, resolve, now_us=now_us)
    ev.tick()  # writes decision + first reconcile attempt (→ attempts=1)
    # After the market expires, the live source stops emitting the ticker.
    # Mirror that here so re-registration doesn't reset `attempts`.
    ev._market_source.get_quotes.return_value = []
    ev.tick()  # second reconcile attempt → attempts=2
    row = conn.execute(
        "SELECT realized_outcome FROM shadow_decisions"
    ).fetchone()
    assert row[0] is None
    assert "KXBTC15M-T" in ev.pending_reconciles
    assert ev.pending_reconciles["KXBTC15M-T"].attempts >= 2


def test_reconciler_gives_up_after_max_attempts(db):
    resolve = MagicMock(return_value=None)
    now_us = (1_746_000_000 + 60) * 1_000_000
    ev, conn = _build_reconcile_evaluator(db, resolve, now_us=now_us)
    ev._config = ShadowConfig(reconcile_delay_s=30, reconcile_max_attempts=2)
    ev.tick()  # writes + attempts=1
    ev._market_source.get_quotes.return_value = []
    ev.tick()  # attempts=2 → pop
    assert "KXBTC15M-T" not in ev.pending_reconciles


def test_reconciler_waits_until_expiration_plus_delay(db):
    """If the market hasn't passed expiration+delay yet, skip."""
    resolve = MagicMock(return_value={"result": "yes"})
    # now is BEFORE expiration + 30s → skip.
    now_us = (1_746_000_000 - 10) * 1_000_000
    ev, conn = _build_reconcile_evaluator(db, resolve, now_us=now_us)
    ev.tick()  # writes decision
    ev.tick()  # reconcile would run but expiration not yet reached
    resolve.assert_not_called()


# ----------------------------------------------------------------------
# P/L math
# ----------------------------------------------------------------------

def test_pnl_side_none_is_zero():
    assert KalshiShadowEvaluator._compute_pnl(
        outcome="yes", side="none",
        fill_price=Decimal("0.5"), size=Decimal("10"),
    ) == Decimal("0")


def test_pnl_winning_yes():
    assert KalshiShadowEvaluator._compute_pnl(
        outcome="yes", side="yes",
        fill_price=Decimal("0.40"), size=Decimal("5"),
    ) == Decimal("3.00")  # (1 - 0.40) * 5


def test_pnl_losing_no():
    assert KalshiShadowEvaluator._compute_pnl(
        outcome="yes", side="no",
        fill_price=Decimal("0.60"), size=Decimal("5"),
    ) == Decimal("-3.00")  # (0 - 0.60) * 5


# ----------------------------------------------------------------------
# run_loop control flow
# ----------------------------------------------------------------------

def test_run_loop_respects_iterations(db):
    import run_kalshi_shadow as rks
    conn, _ = db
    market = MagicMock()
    market.get_quotes.return_value = []
    reference = MagicMock()
    reference.get_spot.return_value = None
    reference.get_60s_avg.return_value = None

    ev = KalshiShadowEvaluator(
        market_source=market,
        reference_source=reference,
        strategy=KalshiFairValueStrategy(FairValueModel()),
        market_meta_by_ticker={},
        asset_by_ticker={},
        conn=conn,
    )
    totals = rks.run_loop(
        evaluator=ev, coordinator=None,
        iterations=5, interval_s=0, no_sleep=True,
    )
    assert totals["ticks"] == 5
    assert totals["written"] == 0


def test_run_loop_stops_on_stop_event(db):
    import run_kalshi_shadow as rks
    conn, _ = db
    market = MagicMock()
    market.get_quotes.return_value = []
    reference = MagicMock()
    reference.get_spot.return_value = None
    reference.get_60s_avg.return_value = None

    ev = KalshiShadowEvaluator(
        market_source=market, reference_source=reference,
        strategy=KalshiFairValueStrategy(FairValueModel()),
        market_meta_by_ticker={}, asset_by_ticker={},
        conn=conn,
    )
    stop = threading.Event()
    stop.set()  # set before run_loop starts — should exit immediately
    totals = rks.run_loop(
        evaluator=ev, coordinator=None,
        iterations=None, interval_s=0, no_sleep=True,
        stop_event=stop,
    )
    assert totals["ticks"] == 0


# ----------------------------------------------------------------------
# Regression tests — bugs found during 2026-04-20 live run
# ----------------------------------------------------------------------

def test_shared_dict_regression_empty_init_does_not_create_fresh_dict(db):
    """Regression: `x or {}` returns a NEW {} when `x` is empty-but-shared,
    breaking the shared-reference contract that `LiveDataCoordinator` relies
    on. Must use `{} if x is None else x` so later mutations by the caller
    propagate into the evaluator's view.
    """
    conn, _ = db
    shared_meta = {}
    shared_asset_map = {}
    market = MagicMock()
    market.get_quotes.return_value = []
    reference = MagicMock()
    reference.get_spot.return_value = None
    reference.get_60s_avg.return_value = None

    ev = KalshiShadowEvaluator(
        market_source=market, reference_source=reference,
        strategy=KalshiFairValueStrategy(FairValueModel()),
        market_meta_by_ticker=shared_meta,          # EMPTY dict passed in
        asset_by_ticker=shared_asset_map,            # EMPTY dict passed in
        conn=conn,
    )
    # Mutate from the CALLER side (simulating coord.discover()).
    shared_meta["KX-X"] = {"asset": "btc"}
    shared_asset_map["KX-X"] = "btc"
    # The evaluator must see the mutation.
    assert "KX-X" in ev._market_meta
    assert "KX-X" in ev._asset_by_ticker
    # And the identity should be the same dict (shared reference).
    assert ev._market_meta is shared_meta
    assert ev._asset_by_ticker is shared_asset_map


def test_shared_dict_regression_none_substitutes_empty_dict(db):
    """Paranoid: None input should substitute a fresh {}, not pass through."""
    conn, _ = db
    ev = KalshiShadowEvaluator(
        market_source=MagicMock(), reference_source=MagicMock(),
        strategy=KalshiFairValueStrategy(FairValueModel()),
        market_meta_by_ticker=None,  # None → fresh {}
        asset_by_ticker=None,
        conn=conn,
    )
    assert ev._market_meta == {}
    assert ev._asset_by_ticker == {}
    assert ev._fee_bps == {}


def test_shadow_config_default_gates_reject_final_minute():
    """Regression: the default `build_evaluator` config must reject
    time_remaining < 120s. Prior data showed 0% win rate there.
    """
    from run_kalshi_shadow import build_evaluator
    import sqlite3
    conn = sqlite3.connect(":memory:")
    # Apply the shadow_decisions schema (minimum needed).
    conn.execute("""CREATE TABLE shadow_decisions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        market_ticker TEXT, ts_us BIGINT,
        p_yes TEXT, ci_width TEXT,
        reference_price TEXT, reference_60s_avg TEXT, time_remaining_s TEXT,
        best_yes_ask TEXT, best_no_ask TEXT,
        book_depth_yes_usd TEXT, book_depth_no_usd TEXT,
        recommended_side TEXT, hypothetical_fill_price TEXT,
        hypothetical_size_contracts TEXT,
        expected_edge_bps_after_fees TEXT, fee_bps_at_decision TEXT,
        realized_outcome TEXT, realized_pnl_usd TEXT,
        latency_ms_ref_to_decision TEXT, latency_ms_book_to_decision TEXT
    )""")
    ev, _ = build_evaluator(
        conn=conn, is_postgres=False,
        rest_client=MagicMock(),
        reference_fetcher=lambda a: None,
    )
    cfg = ev._strategy.config
    # Defaults calibrated from live-run findings:
    lo, hi = cfg.time_window_seconds
    assert lo >= 120, "final-minute window must be rejected by default"
    assert hi <= 900, "window must not exceed 15 minutes"
    # Edge floor comes from the same doc.
    assert cfg.min_edge_bps_after_fees >= Decimal("300")


def test_pnl_side_none_zero_even_on_win():
    assert KalshiShadowEvaluator._compute_pnl(
        outcome="yes", side="none",
        fill_price=Decimal("0.42"), size=Decimal("10"),
    ) == Decimal("0")


def test_pnl_yes_win_with_low_fill_pays_big():
    """The asymmetric-payoff finding: yes win at $0.05 pays $0.95."""
    p = KalshiShadowEvaluator._compute_pnl(
        outcome="yes", side="yes",
        fill_price=Decimal("0.05"), size=Decimal("10"),
    )
    assert p == Decimal("9.5")


def test_pnl_yes_loss_with_low_fill_loses_little():
    p = KalshiShadowEvaluator._compute_pnl(
        outcome="no", side="yes",
        fill_price=Decimal("0.05"), size=Decimal("10"),
    )
    assert p == Decimal("-0.5")


def test_pnl_no_side_no_data_resolves_as_no_win():
    """no_data → resolves to No. Buying no at 0.4, no_data outcome, wins."""
    p = KalshiShadowEvaluator._compute_pnl(
        outcome="no_data", side="no",
        fill_price=Decimal("0.4"), size=Decimal("10"),
    )
    # won (no side on a "no" outcome), pay = (1 - 0.4) * 10 = 6.0
    assert p == Decimal("6.0")
