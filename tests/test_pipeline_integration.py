"""End-to-end pipeline integration test (P2-M3-T05).

Wires together:
  - mocked market + reference sources
  - KalshiShadowEvaluator (real)
  - PureLagStrategy (real)
  - RiskEngine + KalshiPaperExecutor (real)
  - SQLite `paper_fills` / `paper_settlements` / `shadow_decisions`
  - Dashboard reads via FastAPI TestClient

Asserts: decisions persist, paper_fills populate, settlements settle,
dashboard surfaces the flow.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from core.models import MarketQuote
from dashboards.kalshi import create_app
from execution.kalshi_shadow_evaluator import KalshiShadowEvaluator, ShadowConfig
from risk.kalshi_rules import (
    BookDepthRule,
    MinEdgeAfterFeesRule,
    RiskContext,
    RiskEngine,
    TimeWindowRule,
)
from strategy.pure_lag import PureLagConfig, PureLagStrategy

# Import from src (ensures we exercise the public entrypoint's builder, too).
from run_kalshi_shadow import build_paper_executor_bridge


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path):
    import migrate_db as m
    url = f"sqlite:///{tmp_path}/pipeline.db"
    m.migrate(url)
    conn = sqlite3.connect(url.removeprefix("sqlite:///"))
    conn.row_factory = sqlite3.Row
    yield conn, url
    conn.close()


def _mq(**overrides) -> MarketQuote:
    base = dict(
        venue="kalshi",
        market_ticker="KXBTC15M-T1",
        series_ticker="KXBTC15M",
        event_ticker="KXBTC15M-E",
        best_yes_ask=Decimal("0.35"),
        best_no_ask=Decimal("0.60"),
        best_yes_bid=Decimal("0.33"),
        best_no_bid=Decimal("0.58"),
        book_depth_yes_usd=Decimal("500"),
        book_depth_no_usd=Decimal("500"),
        fee_bps=Decimal("35"),
        expiration_ts=Decimal("1746000000"),
        strike=Decimal("65000"),
        comparator="above",
        reference_price=Decimal("66050"),   # ≥ 10 bps above strike → passes StrikeProximityRule
        reference_60s_avg=Decimal("66050"),
        time_remaining_s=Decimal("30"),
        quote_timestamp_us=1_746_000_000_000_000,
    )
    base.update(overrides)
    return MarketQuote(**base)


# ---------------------------------------------------------------------------
# Integration
# ---------------------------------------------------------------------------


class TestPipelineIntegration:
    def test_decision_flows_to_paper_fill_and_settlement(self, db):
        """Full round-trip: strategy → shadow_decisions → paper executor → paper_fills → reconcile → paper_settlements."""
        conn, _ = db
        now_us = 1_746_000_000_000_000

        # Strategy: pure_lag primed so it fires.
        strategy = PureLagStrategy(
            PureLagConfig(
                move_threshold_bps=Decimal("3"),
                rolling_window_us=5_000_000,
                min_edge_bps_after_fees=Decimal("100"),
                min_book_depth_usd=Decimal("50"),
                time_window_seconds=(5, 900),
                hypothetical_size_contracts=Decimal("10"),
                min_fill_price=Decimal("0.10"),
            ),
            now_us=lambda: now_us,
        )
        # Seed the rolling-price history so we have a move signal.
        # Baseline = 66000; latest = 66050 → +7.57 bps, triggers at 3 bps threshold.
        strategy.record_reference_tick("btc", Decimal("66000"))
        strategy.record_reference_tick("btc", Decimal("66000"))
        strategy.record_reference_tick("btc", Decimal("66050"))

        # Bridge: paper executor + hooks.
        executor, decision_hook, reconcile_hook = build_paper_executor_bridge(
            conn=conn, is_postgres=False, strategy_label="pure_lag",
            now_us=lambda: now_us,
        )

        # Market source: emits one quote.
        market = MagicMock()
        quote = _mq()
        market.get_quotes.return_value = [quote]
        market.is_healthy.return_value = True

        reference = MagicMock()
        reference.get_spot.return_value = Decimal("66050")
        reference.get_60s_avg.return_value = Decimal("66050")

        resolution = MagicMock(return_value={"result": "yes"})

        evaluator = KalshiShadowEvaluator(
            market_source=market,
            reference_source=reference,
            strategy=strategy,
            asset_by_ticker={"KXBTC15M-T1": "btc"},
            conn=conn,
            resolution_lookup=resolution,
            config=ShadowConfig(reconcile_delay_s=0),
            now_us=lambda: now_us,
            strategy_label="pure_lag",
            decision_hook=decision_hook,
            reconcile_hook=reconcile_hook,
        )

        # Tick 1: strategy fires, shadow_decisions + paper_fills should land.
        result = evaluator.tick()
        assert result["written"] == 1

        dec_rows = list(conn.execute("SELECT * FROM shadow_decisions").fetchall())
        assert len(dec_rows) == 1
        assert dec_rows[0]["strategy_label"] == "pure_lag"

        fill_rows = list(conn.execute("SELECT * FROM paper_fills").fetchall())
        assert len(fill_rows) == 1
        assert fill_rows[0]["market_ticker"] == "KXBTC15M-T1"
        assert fill_rows[0]["strategy_label"] == "pure_lag"
        assert fill_rows[0]["side"] == "yes"

        # Tick 2: advance clock past expiration. Stop emitting quotes so the
        # strategy doesn't refire — we only want to exercise the reconciler.
        evaluator._now_us = lambda: 1_746_000_040_000_000
        market.get_quotes.return_value = []
        evaluator.tick()

        settled_rows = list(conn.execute(
            "SELECT * FROM paper_settlements"
        ).fetchall())
        assert len(settled_rows) == 1
        assert settled_rows[0]["outcome"] == "yes"
        # yes bought at 0.35 × 10 = (1 − 0.35) × 10 − fees ≈ $6.477
        pnl = Decimal(settled_rows[0]["realized_pnl_usd"])
        assert pnl > Decimal("6.0")
        assert pnl < Decimal("6.5")

    def test_risk_rejection_prevents_paper_fill(self, db):
        """If RiskEngine rejects the opportunity, no paper_fill row is written."""
        conn, _ = db
        now_us = 1_746_000_000_000_000

        # Build a paper executor with a rule that always rejects.
        from execution.kalshi_paper_executor import KalshiPaperExecutor

        engine = RiskEngine([MinEdgeAfterFeesRule(min_bps=Decimal("999999"))])
        executor = KalshiPaperExecutor(
            risk_engine=engine, conn=conn,
            strategy_label="pure_lag", now_us=lambda: now_us,
        )

        def decision_hook(quote, opp):
            from risk.kalshi_rules import RiskContext
            ctx = RiskContext(
                now_us=now_us,
                last_reference_tick_us=now_us - 500_000,
                open_positions=executor.open_positions(),
                daily_realized_pnl_usd=executor.daily_realized_pnl(),
                position_notional_by_strike_usd=executor.notional_by_strike(),
            )
            executor.submit(opp, ctx)

        strategy = PureLagStrategy(
            PureLagConfig(
                move_threshold_bps=Decimal("3"),
                rolling_window_us=5_000_000,
                min_edge_bps_after_fees=Decimal("100"),
                min_book_depth_usd=Decimal("50"),
                time_window_seconds=(5, 900),
                hypothetical_size_contracts=Decimal("10"),
                min_fill_price=Decimal("0.10"),
            ),
            now_us=lambda: now_us,
        )
        strategy.record_reference_tick("btc", Decimal("66000"))
        strategy.record_reference_tick("btc", Decimal("66050"))

        market = MagicMock()
        market.get_quotes.return_value = [_mq()]
        market.is_healthy.return_value = True
        reference = MagicMock()
        reference.get_spot.return_value = Decimal("66050")
        reference.get_60s_avg.return_value = Decimal("66050")

        evaluator = KalshiShadowEvaluator(
            market_source=market, reference_source=reference,
            strategy=strategy,
            asset_by_ticker={"KXBTC15M-T1": "btc"},
            conn=conn, now_us=lambda: now_us,
            strategy_label="pure_lag",
            decision_hook=decision_hook,
        )
        evaluator.tick()

        # Shadow decision still written (research record-keeping).
        assert conn.execute(
            "SELECT COUNT(*) FROM shadow_decisions"
        ).fetchone()[0] == 1
        # But no paper fill — risk engine rejected.
        assert conn.execute(
            "SELECT COUNT(*) FROM paper_fills"
        ).fetchone()[0] == 0

    def test_per_asset_execution_disabled_blocks_paper_fill(self, db):
        """Regression (P2): setting `execution_enabled[btc] = False` via the
        dashboard's flags must block paper fills for BTC. Previously
        `decision_hook` read `getattr(opp.quote, "asset", None)` — but
        `MarketQuote` has no `asset` field, so `asset` was always empty and
        the per-asset gate was silently inert. Fix derives asset from
        `quote.series_ticker` via `ASSET_FROM_SERIES`.
        """
        from types import SimpleNamespace

        conn, _ = db
        now_us = 1_746_000_000_000_000

        # Flags poller returns a RuntimeFlags-like object. BTC is explicitly
        # disabled; ETH is enabled (default). execution_kill_switch is off.
        class _Flags:
            execution_kill_switch = False
            def is_asset_execution_enabled(self, asset: str) -> bool:
                return asset.lower() != "btc"

        flags = _Flags()
        poller = SimpleNamespace(get=lambda: flags)

        # Collect decision_hook events through an EventLogger stub.
        recorded: list[tuple[str, dict]] = []
        ev = SimpleNamespace(
            record=lambda event_type, **fields: recorded.append((event_type, fields)),
        )

        _, decision_hook, _ = build_paper_executor_bridge(
            conn=conn, is_postgres=False, strategy_label="pure_lag",
            now_us=lambda: now_us,
            event_logger=ev, flags_poller=poller,
        )

        # Build a minimal opportunity for BTC. MarketQuote has no `asset`
        # field; asset must be inferred from series_ticker.
        from core.models import Opportunity, OpportunityStatus
        quote_btc = _mq()  # series_ticker=KXBTC15M → asset=btc → blocked
        opp_btc = Opportunity(
            quote=quote_btc, p_yes=Decimal("0.60"), ci_width=Decimal("0.1"),
            recommended_side="yes", hypothetical_fill_price=Decimal("0.35"),
            hypothetical_size_contracts=Decimal("10"),
            expected_edge_bps_after_fees=Decimal("500"),
            status=OpportunityStatus.PRICED,
        )

        decision_hook(quote_btc, opp_btc)

        # Paper fill must NOT land: BTC execution is disabled.
        assert conn.execute(
            "SELECT COUNT(*) FROM paper_fills"
        ).fetchone()[0] == 0

        # The hook must record the per-asset skip event with asset='btc'
        # (not empty string — that was the old bug).
        skipped = [fields for (etype, fields) in recorded
                   if etype == "execution_disabled_for_asset"]
        assert len(skipped) == 1
        assert skipped[0]["asset"] == "btc"

        # Sanity: ETH opp should NOT be blocked by the per-asset gate.
        quote_eth = _mq(market_ticker="KXETH15M-T", series_ticker="KXETH15M")
        opp_eth = Opportunity(
            quote=quote_eth, p_yes=Decimal("0.60"), ci_width=Decimal("0.1"),
            recommended_side="yes", hypothetical_fill_price=Decimal("0.35"),
            hypothetical_size_contracts=Decimal("10"),
            expected_edge_bps_after_fees=Decimal("500"),
            status=OpportunityStatus.PRICED,
        )
        decision_hook(quote_eth, opp_eth)
        # The paper executor may still reject ETH for a risk reason, but the
        # per-asset-disabled event should NOT fire for it.
        eth_skipped = [
            fields for (etype, fields) in recorded
            if etype == "execution_disabled_for_asset"
            and fields.get("asset") == "eth"
        ]
        assert eth_skipped == []

    def test_event_logger_emits_decision_and_paper_fill(self, db, tmp_path):
        """Full pipeline → event log captures `decision` + `paper_fill` lines."""
        import json as _json
        from observability.event_log import EventLogger

        conn, _ = db
        now_us = 1_746_000_000_000_000
        event_log_path = tmp_path / "events.jsonl"
        ev = EventLogger(path=event_log_path, rotate_daily=False,
                         now_us=lambda: now_us)

        executor, decision_hook, reconcile_hook = build_paper_executor_bridge(
            conn=conn, is_postgres=False, strategy_label="pure_lag",
            now_us=lambda: now_us, event_logger=ev,
        )

        strategy = PureLagStrategy(
            PureLagConfig(
                move_threshold_bps=Decimal("3"),
                rolling_window_us=5_000_000,
                min_edge_bps_after_fees=Decimal("100"),
                min_book_depth_usd=Decimal("50"),
                time_window_seconds=(5, 900),
                hypothetical_size_contracts=Decimal("10"),
                min_fill_price=Decimal("0.10"),
            ),
            now_us=lambda: now_us,
        )
        strategy.record_reference_tick("btc", Decimal("66000"))
        strategy.record_reference_tick("btc", Decimal("66050"))

        market = MagicMock()
        market.get_quotes.return_value = [_mq()]
        market.is_healthy.return_value = True
        reference = MagicMock()
        reference.get_spot.return_value = Decimal("66050")
        reference.get_60s_avg.return_value = Decimal("66050")
        # Disable MagicMock's auto-attr for the latency getter.
        reference.get_last_tick_us.return_value = None

        evaluator = KalshiShadowEvaluator(
            market_source=market, reference_source=reference,
            strategy=strategy,
            asset_by_ticker={"KXBTC15M-T1": "btc"},
            conn=conn, now_us=lambda: now_us,
            strategy_label="pure_lag",
            decision_hook=decision_hook,
            reconcile_hook=reconcile_hook,
            event_logger=ev,
        )
        evaluator.tick()

        # Both events should have landed in the JSONL file.
        lines = event_log_path.read_text().splitlines()
        types = [_json.loads(l)["event_type"] for l in lines]
        assert "decision" in types
        assert "paper_fill" in types
        # Decision event carries latency + edge fields.
        dec = next(_json.loads(l) for l in lines
                   if _json.loads(l)["event_type"] == "decision")
        assert dec["strategy_label"] == "pure_lag"
        assert dec["asset"] == "btc"
        assert dec["side"] == "yes"

    def test_event_logger_emits_risk_reject(self, db, tmp_path):
        """Risk-rejected decisions emit a `risk_reject` event, no `paper_fill`."""
        import json as _json
        from observability.event_log import EventLogger
        from execution.kalshi_paper_executor import KalshiPaperExecutor

        conn, _ = db
        now_us = 1_746_000_000_000_000
        event_log_path = tmp_path / "events.jsonl"
        ev = EventLogger(path=event_log_path, rotate_daily=False,
                         now_us=lambda: now_us)

        # Build a paper executor with an impossibly-high edge rule → always rejects.
        engine = RiskEngine([MinEdgeAfterFeesRule(min_bps=Decimal("999999"))])
        executor = KalshiPaperExecutor(
            risk_engine=engine, conn=conn,
            strategy_label="pure_lag", now_us=lambda: now_us,
        )

        def decision_hook(quote, opp):
            ctx = RiskContext(
                now_us=now_us,
                last_reference_tick_us=now_us - 500_000,
                open_positions=executor.open_positions(),
                daily_realized_pnl_usd=executor.daily_realized_pnl(),
                position_notional_by_strike_usd=executor.notional_by_strike(),
            )
            result = executor.submit(opp, ctx)
            if result.success:
                ev.record("paper_fill", market_ticker=opp.quote.market_ticker)
            else:
                ev.record("risk_reject", market_ticker=opp.quote.market_ticker,
                          reason=result.reason)

        strategy = PureLagStrategy(
            PureLagConfig(
                move_threshold_bps=Decimal("3"),
                rolling_window_us=5_000_000,
                min_edge_bps_after_fees=Decimal("100"),
                min_book_depth_usd=Decimal("50"),
                time_window_seconds=(5, 900),
                hypothetical_size_contracts=Decimal("10"),
                min_fill_price=Decimal("0.10"),
            ),
            now_us=lambda: now_us,
        )
        strategy.record_reference_tick("btc", Decimal("66000"))
        strategy.record_reference_tick("btc", Decimal("66050"))

        market = MagicMock()
        market.get_quotes.return_value = [_mq()]
        market.is_healthy.return_value = True
        reference = MagicMock()
        reference.get_spot.return_value = Decimal("66050")
        reference.get_60s_avg.return_value = Decimal("66050")
        reference.get_last_tick_us.return_value = None

        KalshiShadowEvaluator(
            market_source=market, reference_source=reference,
            strategy=strategy,
            asset_by_ticker={"KXBTC15M-T1": "btc"},
            conn=conn, now_us=lambda: now_us,
            strategy_label="pure_lag",
            decision_hook=decision_hook,
            event_logger=ev,
        ).tick()

        lines = event_log_path.read_text().splitlines()
        types = [_json.loads(l)["event_type"] for l in lines]
        assert "decision" in types
        assert "risk_reject" in types
        assert "paper_fill" not in types

    def test_dashboard_reflects_pipeline_state(self, db):
        """After a pipeline tick runs, the dashboard /kalshi/paper page shows the fill."""
        conn, url = db
        now_us = 1_746_000_000_000_000

        executor, decision_hook, reconcile_hook = build_paper_executor_bridge(
            conn=conn, is_postgres=False, strategy_label="pure_lag",
            now_us=lambda: now_us,
        )

        strategy = PureLagStrategy(
            PureLagConfig(
                move_threshold_bps=Decimal("3"),
                rolling_window_us=5_000_000,
                min_edge_bps_after_fees=Decimal("100"),
                min_book_depth_usd=Decimal("50"),
                time_window_seconds=(5, 900),
                hypothetical_size_contracts=Decimal("10"),
                min_fill_price=Decimal("0.10"),
            ),
            now_us=lambda: now_us,
        )
        strategy.record_reference_tick("btc", Decimal("66000"))
        strategy.record_reference_tick("btc", Decimal("66050"))

        market = MagicMock()
        market.get_quotes.return_value = [_mq()]
        market.is_healthy.return_value = True
        reference = MagicMock()
        reference.get_spot.return_value = Decimal("66050")
        reference.get_60s_avg.return_value = Decimal("66050")

        evaluator = KalshiShadowEvaluator(
            market_source=market, reference_source=reference,
            strategy=strategy,
            asset_by_ticker={"KXBTC15M-T1": "btc"},
            conn=conn, now_us=lambda: now_us,
            strategy_label="pure_lag",
            decision_hook=decision_hook,
            reconcile_hook=reconcile_hook,
        )
        evaluator.tick()

        # Flush the writer connection so the read-only dashboard sees the row.
        conn.commit()

        app = create_app(database_url=url)
        with TestClient(app) as client:
            r = client.get("/api/overview")
            assert r.status_code == 200
            data = r.json()
            labels = {s["strategy_label"] for s in data["per_strategy"]}
            assert "pure_lag" in labels

            r = client.get("/kalshi/paper")
            assert r.status_code == 200
            # The paper summary shows n_fills >= 1.
            assert ">1<" in r.text or ">0<" not in r.text.split("Paper fills")[1][:200]
