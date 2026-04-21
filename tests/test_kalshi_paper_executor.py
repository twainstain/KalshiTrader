"""Cover `src/execution/kalshi_paper_executor.py` (P2-M1-T13).

Full paper-mode lifecycle:
  - submit without risk engine
  - submit with risk engine (approve + reject)
  - reconcile yes / no / no_data
  - open-positions counter
  - per-strike notional accumulation
  - daily-P/L accumulator bucketed by UTC day
  - DB persistence: paper_fills / paper_settlements rows written
  - KalshiLiveExecutor refuses when gate unsatisfied
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal

import pytest

from core.models import MarketQuote, Opportunity, OpportunityStatus, ZERO
from execution._executor_common import utc_day_bucket as _utc_day_bucket
from execution.kalshi_live_executor import (
    KalshiLiveExecutor,
    LiveGateConfig,
)
from execution.kalshi_paper_executor import (
    KalshiPaperExecutor,
    PaperFill,
    PaperSettlement,
)
from risk.kalshi_rules import (
    MinEdgeAfterFeesRule,
    OpenPositionsRule,
    RiskContext,
    RiskEngine,
    default_rules,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _mq(**overrides) -> MarketQuote:
    base = dict(
        venue="kalshi",
        market_ticker="KXBTC15M-T1",
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
        reference_price=Decimal("66000"),
        reference_60s_avg=Decimal("66000"),
        time_remaining_s=Decimal("30"),
        quote_timestamp_us=1_746_000_000_000_000,
    )
    base.update(overrides)
    return MarketQuote(**base)


def _opp(quote: MarketQuote | None = None, **overrides) -> Opportunity:
    base = dict(
        quote=quote or _mq(),
        p_yes=Decimal("0.70"),
        ci_width=Decimal("0.05"),
        recommended_side="yes",
        hypothetical_fill_price=Decimal("0.55"),
        hypothetical_size_contracts=Decimal("10"),
        expected_edge_bps_after_fees=Decimal("150"),
        status=OpportunityStatus.PRICED,
    )
    base.update(overrides)
    return Opportunity(**base)


def _ctx(now_us: int = 1_746_000_000_000_000, **overrides) -> RiskContext:
    base = dict(
        now_us=now_us,
        last_reference_tick_us=now_us - 500_000,
        open_positions=0,
        daily_realized_pnl_usd=Decimal("0"),
        position_notional_by_strike_usd={},
        cf_benchmarks_degraded=False,
    )
    base.update(overrides)
    return RiskContext(**base)


# ---------------------------------------------------------------------------
# submit()
# ---------------------------------------------------------------------------


class TestSubmit:
    def test_submit_without_risk_engine_records_fill(self) -> None:
        ex = KalshiPaperExecutor(now_us=lambda: 1_000_000)
        r = ex.submit(_opp())
        assert r.success is True
        assert r.reason == "paper-filled"
        assert ex.open_positions() == 1

    def test_submit_side_none_refused(self) -> None:
        ex = KalshiPaperExecutor()
        r = ex.submit(_opp(
            recommended_side="none",
            hypothetical_fill_price=Decimal("0"),
            hypothetical_size_contracts=Decimal("0"),
            expected_edge_bps_after_fees=Decimal("0"),
        ))
        assert not r.success
        assert "no recommended side" in r.reason
        assert ex.open_positions() == 0

    def test_submit_with_risk_engine_approves(self) -> None:
        engine = RiskEngine(default_rules())
        ex = KalshiPaperExecutor(risk_engine=engine, now_us=lambda: 1_000_000)
        r = ex.submit(_opp(), _ctx(now_us=1_000_000))
        assert r.success
        assert ex.open_positions() == 1

    def test_submit_with_risk_engine_rejects(self) -> None:
        # Edge below the 100-bps minimum → MinEdgeAfterFeesRule rejects.
        engine = RiskEngine([MinEdgeAfterFeesRule()])
        ex = KalshiPaperExecutor(risk_engine=engine)
        r = ex.submit(
            _opp(expected_edge_bps_after_fees=Decimal("50")),
            _ctx(),
        )
        assert not r.success
        assert "risk-rejected" in r.reason
        assert "min_edge_after_fees" in r.reason
        assert ex.open_positions() == 0

    def test_submit_with_engine_requires_ctx(self) -> None:
        engine = RiskEngine(default_rules())
        ex = KalshiPaperExecutor(risk_engine=engine)
        with pytest.raises(ValueError, match="RiskContext"):
            ex.submit(_opp())

    def test_submit_records_fees_on_notional(self) -> None:
        ex = KalshiPaperExecutor(now_us=lambda: 1)
        # $0.50 × 10 = $5 notional × 35 bps = $0.0175 fees
        opp = _opp(
            quote=_mq(fee_bps=Decimal("35")),
            hypothetical_fill_price=Decimal("0.50"),
            hypothetical_size_contracts=Decimal("10"),
        )
        ex.submit(opp)
        fills = ex._open_fills[opp.quote.market_ticker]
        assert fills[0].fees_paid_usd == Decimal("0.0175")


# ---------------------------------------------------------------------------
# reconcile()
# ---------------------------------------------------------------------------


class TestReconcile:
    def test_reconcile_yes_wins_yes_side(self) -> None:
        ex = KalshiPaperExecutor(now_us=lambda: 1)
        opp = _opp(
            recommended_side="yes",
            hypothetical_fill_price=Decimal("0.40"),
            hypothetical_size_contracts=Decimal("10"),
            quote=_mq(fee_bps=Decimal("0")),  # isolate P/L from fee math
        )
        ex.submit(opp)
        settlements = ex.reconcile(opp.quote.market_ticker, "yes")
        assert len(settlements) == 1
        # (1 − 0.40) × 10 = $6 gross, $0 fees → $6.
        assert settlements[0].realized_pnl_usd == Decimal("6.00")
        assert ex.open_positions() == 0

    def test_reconcile_no_loses_yes_side(self) -> None:
        ex = KalshiPaperExecutor(now_us=lambda: 1)
        opp = _opp(
            recommended_side="yes",
            hypothetical_fill_price=Decimal("0.40"),
            hypothetical_size_contracts=Decimal("10"),
            quote=_mq(fee_bps=Decimal("0")),
        )
        ex.submit(opp)
        settlements = ex.reconcile(opp.quote.market_ticker, "no")
        # (0 − 0.40) × 10 = −$4.
        assert settlements[0].realized_pnl_usd == Decimal("-4.00")

    def test_reconcile_no_data_resolves_no(self) -> None:
        """CRYPTO15M.pdf §0.5 — missing data at expiry resolves NO."""
        ex = KalshiPaperExecutor(now_us=lambda: 1)
        # YES bought → no_data should behave like "no" → loss.
        yes_opp = _opp(
            recommended_side="yes",
            hypothetical_fill_price=Decimal("0.40"),
            hypothetical_size_contracts=Decimal("10"),
            quote=_mq(fee_bps=Decimal("0")),
        )
        ex.submit(yes_opp)
        s = ex.reconcile(yes_opp.quote.market_ticker, "no_data")
        assert s[0].outcome == "no_data"
        assert s[0].realized_pnl_usd == Decimal("-4.00")

        # NO bought → no_data behaves like "no" → win.
        no_opp = _opp(
            quote=_mq(market_ticker="KXBTC15M-T2", fee_bps=Decimal("0")),
            recommended_side="no",
            hypothetical_fill_price=Decimal("0.40"),
            hypothetical_size_contracts=Decimal("10"),
        )
        ex.submit(no_opp)
        s2 = ex.reconcile(no_opp.quote.market_ticker, "no_data")
        assert s2[0].realized_pnl_usd == Decimal("6.00")

    def test_reconcile_fees_subtracted(self) -> None:
        ex = KalshiPaperExecutor(now_us=lambda: 1)
        opp = _opp(
            recommended_side="yes",
            hypothetical_fill_price=Decimal("0.40"),
            hypothetical_size_contracts=Decimal("10"),
            quote=_mq(fee_bps=Decimal("35")),  # fees > 0
        )
        ex.submit(opp)
        # $6 gross − $0.014 fees ($4 × 35 bps) = $5.986.
        s = ex.reconcile(opp.quote.market_ticker, "yes")
        assert s[0].realized_pnl_usd == Decimal("5.986")

    def test_reconcile_missing_ticker_returns_empty(self) -> None:
        ex = KalshiPaperExecutor()
        assert ex.reconcile("NOSUCH-T", "yes") == []

    def test_reconcile_rejects_bad_outcome(self) -> None:
        ex = KalshiPaperExecutor()
        with pytest.raises(ValueError, match="must be yes"):
            ex.reconcile("KXBTC15M-T1", "foobar")

    def test_reconcile_settles_multiple_fills(self) -> None:
        ex = KalshiPaperExecutor(now_us=lambda: 1)
        q = _mq(fee_bps=Decimal("0"))
        ex.submit(_opp(quote=q,
                       hypothetical_fill_price=Decimal("0.30"),
                       hypothetical_size_contracts=Decimal("5")))
        ex.submit(_opp(quote=q,
                       hypothetical_fill_price=Decimal("0.50"),
                       hypothetical_size_contracts=Decimal("5")))
        assert ex.open_positions() == 2
        s = ex.reconcile(q.market_ticker, "yes")
        assert len(s) == 2
        # (1 − 0.30)*5 + (1 − 0.50)*5 = 3.5 + 2.5 = 6.0
        total = sum((x.realized_pnl_usd for x in s), Decimal("0"))
        assert total == Decimal("6.00")
        assert ex.open_positions() == 0


# ---------------------------------------------------------------------------
# State snapshots (fed to RiskContext)
# ---------------------------------------------------------------------------


class TestStateSnapshots:
    def test_notional_by_strike_accumulates_and_clears(self) -> None:
        ex = KalshiPaperExecutor(now_us=lambda: 1)
        q1 = _mq(market_ticker="STRIKE-A")
        q2 = _mq(market_ticker="STRIKE-B")
        ex.submit(_opp(quote=q1,
                       hypothetical_fill_price=Decimal("0.50"),
                       hypothetical_size_contracts=Decimal("20")))
        ex.submit(_opp(quote=q2,
                       hypothetical_fill_price=Decimal("0.30"),
                       hypothetical_size_contracts=Decimal("10")))
        snap = ex.notional_by_strike()
        assert snap == {"STRIKE-A": Decimal("10.0"), "STRIKE-B": Decimal("3.0")}

        # Settle A → its entry drops from the snapshot.
        ex.reconcile("STRIKE-A", "yes")
        assert "STRIKE-A" not in ex.notional_by_strike()
        assert "STRIKE-B" in ex.notional_by_strike()

    def test_notional_snapshot_is_a_copy(self) -> None:
        ex = KalshiPaperExecutor(now_us=lambda: 1)
        ex.submit(_opp())
        snap = ex.notional_by_strike()
        snap["fake"] = Decimal("999")
        assert "fake" not in ex.notional_by_strike()

    def test_daily_pnl_bucketed_by_utc_day(self) -> None:
        # Monday 2026-04-20 00:00:05 UTC.
        day1_us = int(
            Decimal("1745107205") * Decimal("1000000")  # 2026-04-20 00:00:05 UTC
        )
        # Tuesday, one day later at the same wall-clock second.
        day2_us = day1_us + 86_400_000_000

        tick = [day1_us]
        ex = KalshiPaperExecutor(now_us=lambda: tick[0])
        q = _mq(fee_bps=Decimal("0"))
        opp = _opp(quote=q,
                   recommended_side="yes",
                   hypothetical_fill_price=Decimal("0.40"),
                   hypothetical_size_contracts=Decimal("10"))
        ex.submit(opp)
        ex.reconcile(q.market_ticker, "yes")  # +$6 on day1

        # Second trade settles on day2.
        tick[0] = day2_us
        ex.submit(_opp(quote=_mq(market_ticker="DAY2-T", fee_bps=Decimal("0")),
                       recommended_side="yes",
                       hypothetical_fill_price=Decimal("0.80"),
                       hypothetical_size_contracts=Decimal("5")))
        ex.reconcile("DAY2-T", "no")  # -$4 on day2

        assert ex.daily_realized_pnl(now_us=day1_us) == Decimal("6.00")
        assert ex.daily_realized_pnl(now_us=day2_us) == Decimal("-4.00")

    def test_utc_day_bucket_helper(self) -> None:
        # Sanity check — the bucket key format must be stable across runs.
        assert _utc_day_bucket(0) == "1970-01-01"
        assert _utc_day_bucket(86_400_000_000) == "1970-01-02"


# ---------------------------------------------------------------------------
# Integration: executor + RiskContext composition
# ---------------------------------------------------------------------------


class TestRiskContextIntegration:
    def test_open_positions_rule_blocks_after_cap(self) -> None:
        """Shows the intended round-trip: executor snapshot → RiskContext → engine."""
        engine = RiskEngine([OpenPositionsRule(max_concurrent=2)])
        ex = KalshiPaperExecutor(risk_engine=engine)

        def submit_with_current_state(opp):
            ctx = RiskContext(
                now_us=1,
                last_reference_tick_us=1,
                open_positions=ex.open_positions(),
                daily_realized_pnl_usd=ex.daily_realized_pnl(),
                position_notional_by_strike_usd=ex.notional_by_strike(),
            )
            return ex.submit(opp, ctx)

        # First two fills approved.
        r1 = submit_with_current_state(
            _opp(quote=_mq(market_ticker="A"))
        )
        r2 = submit_with_current_state(
            _opp(quote=_mq(market_ticker="B"))
        )
        assert r1.success and r2.success
        # Third rejected — cap reached.
        r3 = submit_with_current_state(
            _opp(quote=_mq(market_ticker="C"))
        )
        assert not r3.success
        assert "open_positions" in r3.reason


# ---------------------------------------------------------------------------
# DB persistence (paper_fills + paper_settlements)
# ---------------------------------------------------------------------------


@pytest.fixture
def persisted_db(tmp_path):
    import migrate_db as m
    url = f"sqlite:///{tmp_path}/paper.db"
    m.migrate(url)
    conn = sqlite3.connect(url.removeprefix("sqlite:///"))
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


class TestPersistence:
    def test_submit_writes_paper_fill_row(self, persisted_db) -> None:
        ex = KalshiPaperExecutor(
            conn=persisted_db,
            strategy_label="pure_lag",
            now_us=lambda: 1_746_000_000_000_000,
        )
        opp = _opp(
            quote=_mq(fee_bps=Decimal("35")),
            recommended_side="yes",
            hypothetical_fill_price=Decimal("0.55"),
            hypothetical_size_contracts=Decimal("10"),
            expected_edge_bps_after_fees=Decimal("150"),
        )
        r = ex.submit(opp)
        assert r.success

        rows = list(persisted_db.execute("SELECT * FROM paper_fills").fetchall())
        assert len(rows) == 1
        row = rows[0]
        assert row["market_ticker"] == "KXBTC15M-T1"
        assert row["strategy_label"] == "pure_lag"
        assert row["side"] == "yes"
        assert Decimal(row["fill_price"]) == Decimal("0.55")
        assert Decimal(row["size_contracts"]) == Decimal("10")
        assert Decimal(row["notional_usd"]) == Decimal("5.50")
        assert Decimal(row["expected_edge_bps_after_fees"]) == Decimal("150")
        assert Decimal(row["strike"]) == Decimal("65000")
        assert row["comparator"] == "above"
        # fill_id is populated on the returned PaperFill.
        fills = ex._open_fills["KXBTC15M-T1"]
        assert fills[0].fill_id == row["id"]

    def test_reconcile_writes_paper_settlement_row(self, persisted_db) -> None:
        ex = KalshiPaperExecutor(
            conn=persisted_db,
            strategy_label="pure_lag",
            now_us=lambda: 1_746_000_000_000_000,
        )
        opp = _opp(
            quote=_mq(fee_bps=Decimal("0")),
            recommended_side="yes",
            hypothetical_fill_price=Decimal("0.40"),
            hypothetical_size_contracts=Decimal("10"),
        )
        ex.submit(opp)
        ex.reconcile(opp.quote.market_ticker, "yes")

        settlements = list(
            persisted_db.execute("SELECT * FROM paper_settlements").fetchall()
        )
        assert len(settlements) == 1
        s = settlements[0]
        assert s["market_ticker"] == "KXBTC15M-T1"
        assert s["outcome"] == "yes"
        assert Decimal(s["realized_pnl_usd"]) == Decimal("6.00")

        # settlement's fill_id must match the fills table PK.
        fill_rows = list(persisted_db.execute("SELECT id FROM paper_fills").fetchall())
        assert s["fill_id"] == fill_rows[0]["id"]

    def test_multiple_fills_settle_with_correct_fk(self, persisted_db) -> None:
        ex = KalshiPaperExecutor(
            conn=persisted_db,
            strategy_label="stat_model",
            now_us=lambda: 1_000,
        )
        q = _mq(fee_bps=Decimal("0"))
        for _ in range(3):
            ex.submit(_opp(
                quote=q,
                hypothetical_fill_price=Decimal("0.50"),
                hypothetical_size_contracts=Decimal("5"),
            ))
        ex.reconcile(q.market_ticker, "yes")

        fills = list(persisted_db.execute(
            "SELECT id FROM paper_fills ORDER BY id"
        ).fetchall())
        settlements = list(persisted_db.execute(
            "SELECT fill_id FROM paper_settlements ORDER BY fill_id"
        ).fetchall())
        assert [f["id"] for f in fills] == [s["fill_id"] for s in settlements]

    def test_no_conn_means_no_persistence_calls(self) -> None:
        # With no `conn`, fill_id stays None and nothing is persisted.
        ex = KalshiPaperExecutor(now_us=lambda: 1)
        ex.submit(_opp())
        fills = ex._open_fills[_opp().quote.market_ticker]
        assert fills[0].fill_id is None


class TestMigrationCreatesTables:
    def test_fresh_db_has_paper_tables(self, tmp_path) -> None:
        import migrate_db as m
        url = f"sqlite:///{tmp_path}/fresh.db"
        m.migrate(url)
        conn = sqlite3.connect(url.removeprefix("sqlite:///"))
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            names = {r[0] for r in rows}
            assert "paper_fills" in names
            assert "paper_settlements" in names
        finally:
            conn.close()

    def test_migration_is_idempotent(self, tmp_path) -> None:
        import migrate_db as m
        url = f"sqlite:///{tmp_path}/idemp.db"
        m.migrate(url)
        m.migrate(url)  # second run must not error
        conn = sqlite3.connect(url.removeprefix("sqlite:///"))
        try:
            cnt = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name='paper_fills'"
            ).fetchone()[0]
            assert cnt == 1
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Live executor gate (P2-M2-T01 stub)
# ---------------------------------------------------------------------------


class TestLiveGate:
    def test_gate_refuses_when_any_opt_in_missing(self) -> None:
        # All four combinations where gate should fail.
        from unittest.mock import MagicMock
        for kwargs in [
            dict(execute_flag=False, api_key_id_present=True, config_mode_live=True, dry_run=False),
            dict(execute_flag=True, api_key_id_present=False, config_mode_live=True, dry_run=False),
            dict(execute_flag=True, api_key_id_present=True, config_mode_live=False, dry_run=False),
            dict(execute_flag=True, api_key_id_present=True, config_mode_live=True, dry_run=True),
        ]:
            gate = LiveGateConfig(**kwargs)
            assert not gate.is_live_approved
            with pytest.raises(RuntimeError, match="three-opt-in"):
                KalshiLiveExecutor(rest_client=MagicMock(), gate=gate)

    def test_gate_approves_when_all_four_aligned(self) -> None:
        """All opt-ins aligned → construction succeeds (no NotImplementedError)."""
        from unittest.mock import MagicMock
        gate = LiveGateConfig(
            execute_flag=True, api_key_id_present=True,
            config_mode_live=True, dry_run=False,
        )
        assert gate.is_live_approved
        # With a mock rest_client, construction should succeed and give us a
        # usable executor — this guards against accidental NotImplementedError
        # regressions in the live executor wiring.
        ex = KalshiLiveExecutor(rest_client=MagicMock(), gate=gate)
        assert ex.open_positions() == 0
