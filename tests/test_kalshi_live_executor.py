"""Cover `KalshiLiveExecutor` (P2-M2-T02).

Scope:
  - three-opt-in gate (already covered lightly in test_kalshi_paper_executor;
    repeated here for completeness of the live-side test file)
  - order-create happy path
  - order-create with risk-engine rejection
  - transient 5xx retry via RetryPolicy
  - cancel-on-timeout flow
  - fill detection via /portfolio/fills
  - reconciliation via /portfolio/settlements with discrepancy detection
  - circuit-breaker gate short-circuits submit()
  - DB persistence (live_orders + live_settlements rows)
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from core.models import MarketQuote, Opportunity, OpportunityStatus, ZERO
from execution.kalshi_live_executor import (
    KalshiLiveExecutor,
    LiveGateConfig,
    LiveOrder,
    LiveSettlement,
)
from risk.kalshi_rules import (
    MinEdgeAfterFeesRule,
    RiskContext,
    RiskEngine,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_NOW = 1_746_000_000_000_000


def _approved_gate() -> LiveGateConfig:
    return LiveGateConfig(
        execute_flag=True,
        api_key_id_present=True,
        config_mode_live=True,
        dry_run=False,
    )


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
        quote_timestamp_us=_NOW,
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


def _ctx(**overrides) -> RiskContext:
    base = dict(
        now_us=_NOW,
        last_reference_tick_us=_NOW - 500_000,
        open_positions=0,
        daily_realized_pnl_usd=Decimal("0"),
        position_notional_by_strike_usd={},
        cf_benchmarks_degraded=False,
    )
    base.update(overrides)
    return RiskContext(**base)


class _FakeRestClient:
    """Minimal fake with exactly the methods KalshiLiveExecutor calls."""

    def __init__(self) -> None:
        self.created: list[dict] = []
        self.canceled: list[str] = []
        self.fills_response: dict = {"fills": []}
        self.settlements_response: dict = {"settlements": []}
        self.create_raises: list[Exception] = []   # queue of exceptions
        self.cancel_raises: list[Exception] = []

    def create_order(self, **kwargs) -> dict:
        self.created.append(kwargs)
        if self.create_raises:
            raise self.create_raises.pop(0)
        n = len(self.created)
        return {
            "order": {
                "order_id": f"SRV-{n}",
                "client_order_id": kwargs["client_order_id"],
                "status": "resting",
            }
        }

    def cancel_order(self, order_id: str) -> dict:
        self.canceled.append(order_id)
        if self.cancel_raises:
            raise self.cancel_raises.pop(0)
        return {"ok": True}

    def get_order(self, order_id: str) -> dict:
        return {"order": {"order_id": order_id, "status": "resting"}}

    def get_fills(self, **_) -> dict:
        return self.fills_response

    def get_positions(self, **_) -> dict:
        return {"positions": []}

    def get_settlements(self, **_) -> dict:
        return self.settlements_response


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


class TestGate:
    def test_refuses_all_four_opt_out_combos(self) -> None:
        for kwargs in [
            dict(execute_flag=False, api_key_id_present=True, config_mode_live=True, dry_run=False),
            dict(execute_flag=True, api_key_id_present=False, config_mode_live=True, dry_run=False),
            dict(execute_flag=True, api_key_id_present=True, config_mode_live=False, dry_run=False),
            dict(execute_flag=True, api_key_id_present=True, config_mode_live=True, dry_run=True),
        ]:
            gate = LiveGateConfig(**kwargs)
            with pytest.raises(RuntimeError, match="three-opt-in"):
                KalshiLiveExecutor(rest_client=_FakeRestClient(), gate=gate)

    def test_approves_when_all_aligned(self) -> None:
        ex = KalshiLiveExecutor(
            rest_client=_FakeRestClient(), gate=_approved_gate(),
        )
        assert ex.open_positions() == 0


# ---------------------------------------------------------------------------
# Submit — happy path + gates
# ---------------------------------------------------------------------------


class TestSubmit:
    def test_happy_path_records_resting_order(self) -> None:
        rest = _FakeRestClient()
        ex = KalshiLiveExecutor(
            rest_client=rest, gate=_approved_gate(), now_us=lambda: _NOW,
        )
        r = ex.submit(_opp())
        assert r.success and r.reason == "live-submitted"
        assert len(rest.created) == 1
        # yes side → yes_price carried as cents (55).
        call = rest.created[0]
        assert call["ticker"] == "KXBTC15M-T1"
        assert call["action"] == "buy"
        assert call["side"] == "yes"
        assert call["count"] == 10
        assert call["yes_price"] == 55
        assert call["client_order_id"].startswith("kt-KXBTC15M-T1-")
        assert ex.open_positions() == 1

    def test_no_side_uses_no_price(self) -> None:
        rest = _FakeRestClient()
        ex = KalshiLiveExecutor(
            rest_client=rest, gate=_approved_gate(), now_us=lambda: _NOW,
        )
        opp = _opp(
            recommended_side="no",
            hypothetical_fill_price=Decimal("0.42"),
        )
        ex.submit(opp)
        assert rest.created[0]["side"] == "no"
        assert rest.created[0]["no_price"] == 42
        assert "yes_price" not in rest.created[0]

    def test_side_none_refused_without_calling_rest(self) -> None:
        rest = _FakeRestClient()
        ex = KalshiLiveExecutor(
            rest_client=rest, gate=_approved_gate(), now_us=lambda: _NOW,
        )
        r = ex.submit(_opp(
            recommended_side="none",
            hypothetical_fill_price=Decimal("0"),
            hypothetical_size_contracts=Decimal("0"),
            expected_edge_bps_after_fees=Decimal("0"),
        ))
        assert not r.success
        assert "no recommended side" in r.reason
        assert rest.created == []

    def test_risk_engine_rejection_blocks_submission(self) -> None:
        rest = _FakeRestClient()
        ex = KalshiLiveExecutor(
            rest_client=rest, gate=_approved_gate(),
            risk_engine=RiskEngine([MinEdgeAfterFeesRule()]),
            now_us=lambda: _NOW,
        )
        r = ex.submit(
            _opp(expected_edge_bps_after_fees=Decimal("50")),
            _ctx(),
        )
        assert not r.success
        assert "risk-rejected" in r.reason
        assert rest.created == []

    def test_submit_with_engine_requires_ctx(self) -> None:
        ex = KalshiLiveExecutor(
            rest_client=_FakeRestClient(), gate=_approved_gate(),
            risk_engine=RiskEngine([MinEdgeAfterFeesRule()]),
        )
        with pytest.raises(ValueError, match="RiskContext"):
            ex.submit(_opp())

    def test_circuit_breaker_open_blocks_submission(self) -> None:
        breaker = MagicMock()
        breaker.allows_execution.return_value = (False, "too many errors")
        rest = _FakeRestClient()
        ex = KalshiLiveExecutor(
            rest_client=rest, gate=_approved_gate(),
            circuit_breaker=breaker, now_us=lambda: _NOW,
        )
        r = ex.submit(_opp())
        assert not r.success
        assert "circuit-breaker-open" in r.reason
        assert rest.created == []

    def test_order_create_exception_returns_failed_result(self) -> None:
        breaker = MagicMock()
        breaker.allows_execution.return_value = (True, "")
        rest = _FakeRestClient()
        rest.create_raises.append(RuntimeError("503"))
        ex = KalshiLiveExecutor(
            rest_client=rest, gate=_approved_gate(),
            circuit_breaker=breaker, now_us=lambda: _NOW,
        )
        r = ex.submit(_opp())
        assert not r.success
        assert "order-create-failed" in r.reason
        # Breaker recorded the failure.
        breaker.record_api_error.assert_called_once()

    def test_submit_records_success_to_breaker(self) -> None:
        breaker = MagicMock()
        breaker.allows_execution.return_value = (True, "")
        rest = _FakeRestClient()
        ex = KalshiLiveExecutor(
            rest_client=rest, gate=_approved_gate(),
            circuit_breaker=breaker, now_us=lambda: _NOW,
        )
        r = ex.submit(_opp())
        assert r.success
        breaker.record_success.assert_called_once()


# ---------------------------------------------------------------------------
# Cancel-on-timeout
# ---------------------------------------------------------------------------


class TestCancelOnTimeout:
    def test_resting_order_canceled_after_timeout(self) -> None:
        rest = _FakeRestClient()
        tick = [_NOW]
        ex = KalshiLiveExecutor(
            rest_client=rest, gate=_approved_gate(),
            cancel_timeout_s=3.0, now_us=lambda: tick[0],
        )
        ex.submit(_opp())
        assert ex.open_positions() == 1

        # Advance 4 s past submission.
        tick[0] = _NOW + 4_000_000
        r = ex.poll_pending()
        assert r["canceled"] == 1
        assert r["filled"] == 0
        assert len(rest.canceled) == 1
        assert rest.canceled[0].startswith("SRV-")
        assert ex.open_positions() == 0

    def test_not_canceled_before_timeout(self) -> None:
        rest = _FakeRestClient()
        tick = [_NOW]
        ex = KalshiLiveExecutor(
            rest_client=rest, gate=_approved_gate(),
            cancel_timeout_s=3.0, now_us=lambda: tick[0],
        )
        ex.submit(_opp())
        # Only 1s passed.
        tick[0] = _NOW + 1_000_000
        r = ex.poll_pending()
        assert r["canceled"] == 0
        assert rest.canceled == []
        assert ex.open_positions() == 1

    def test_cancel_failure_keeps_order_resting_for_retry(self) -> None:
        rest = _FakeRestClient()
        rest.cancel_raises.append(RuntimeError("network"))
        tick = [_NOW]
        ex = KalshiLiveExecutor(
            rest_client=rest, gate=_approved_gate(),
            cancel_timeout_s=3.0, now_us=lambda: tick[0],
        )
        ex.submit(_opp())
        tick[0] = _NOW + 4_000_000
        r = ex.poll_pending()
        assert r["canceled"] == 0
        # Still resting — next poll can retry.
        assert ex.open_positions() == 1


# ---------------------------------------------------------------------------
# Fill detection
# ---------------------------------------------------------------------------


class TestFillDetection:
    def test_fill_matched_by_client_order_id(self) -> None:
        rest = _FakeRestClient()
        tick = [_NOW]
        ex = KalshiLiveExecutor(
            rest_client=rest, gate=_approved_gate(),
            cancel_timeout_s=60.0, now_us=lambda: tick[0],
        )
        ex.submit(_opp())
        client_id = rest.created[0]["client_order_id"]

        # Advance 1 s — well inside timeout.
        tick[0] = _NOW + 1_000_000
        rest.fills_response = {"fills": [{
            "client_order_id": client_id,
            "order_id": "SRV-1",
            "yes_price": 55,
            "count": 10,
            "fees": 2,        # cents
        }]}
        r = ex.poll_pending()
        assert r["filled"] == 1
        assert r["canceled"] == 0
        # Filled order moves out of resting into filled_by_ticker.
        assert len(ex.resting_orders()) == 0
        assert ex.open_positions() == 1  # still "open" for risk until settlement

    def test_unknown_fill_ignored(self) -> None:
        rest = _FakeRestClient()
        ex = KalshiLiveExecutor(
            rest_client=rest, gate=_approved_gate(),
            cancel_timeout_s=60.0, now_us=lambda: _NOW,
        )
        ex.submit(_opp())
        rest.fills_response = {"fills": [{
            "client_order_id": "UNKNOWN-CLIENT-ID",
            "order_id": "UNKNOWN",
            "yes_price": 55, "count": 10,
        }]}
        r = ex.poll_pending()
        assert r["filled"] == 0
        assert ex.open_positions() == 1

    def test_fill_matched_by_order_id_when_client_id_missing(self) -> None:
        rest = _FakeRestClient()
        ex = KalshiLiveExecutor(
            rest_client=rest, gate=_approved_gate(),
            cancel_timeout_s=60.0, now_us=lambda: _NOW,
        )
        ex.submit(_opp())
        rest.fills_response = {"fills": [{
            # No client_order_id on this fill — match via order_id.
            "order_id": "SRV-1",
            "yes_price": 55, "count": 10, "fees": 0,
        }]}
        r = ex.poll_pending()
        assert r["filled"] == 1


# ---------------------------------------------------------------------------
# Reconcile (settlement + discrepancy)
# ---------------------------------------------------------------------------


class TestReconcile:
    def test_reconcile_yes_outcome_matches_kalshi_report(self) -> None:
        rest = _FakeRestClient()
        tick = [_NOW]
        ex = KalshiLiveExecutor(
            rest_client=rest, gate=_approved_gate(),
            cancel_timeout_s=60.0, now_us=lambda: tick[0],
        )
        # Submit + fill.
        ex.submit(_opp(
            hypothetical_fill_price=Decimal("0.40"),
            hypothetical_size_contracts=Decimal("10"),
            quote=_mq(fee_bps=Decimal("0")),
        ))
        client_id = rest.created[0]["client_order_id"]
        rest.fills_response = {"fills": [{
            "client_order_id": client_id,
            "order_id": "SRV-1",
            "yes_price": 40, "count": 10, "fees": 0,
        }]}
        ex.poll_pending()

        # Settle: YES wins. $6 P/L on $0.40 × 10.
        tick[0] = _NOW + 1_000_000_000
        # Kalshi reports 600 cents = $6 — matches computed.
        rest.settlements_response = {"settlements": [{
            "order_id": "SRV-1", "realized_pnl": 600,
        }]}
        settled = ex.reconcile("KXBTC15M-T1", "yes")
        assert len(settled) == 1
        s = settled[0]
        assert s.computed_pnl_usd == Decimal("6.00")
        assert s.kalshi_reported_pnl_usd == Decimal("6.00")
        assert s.discrepancy_usd == Decimal("0.00")

    def test_reconcile_detects_discrepancy(self) -> None:
        rest = _FakeRestClient()
        tick = [_NOW]
        ex = KalshiLiveExecutor(
            rest_client=rest, gate=_approved_gate(),
            cancel_timeout_s=60.0, now_us=lambda: tick[0],
        )
        ex.submit(_opp(
            hypothetical_fill_price=Decimal("0.40"),
            hypothetical_size_contracts=Decimal("10"),
            quote=_mq(fee_bps=Decimal("0")),
        ))
        client_id = rest.created[0]["client_order_id"]
        rest.fills_response = {"fills": [{
            "client_order_id": client_id, "order_id": "SRV-1",
            "yes_price": 40, "count": 10, "fees": 0,
        }]}
        ex.poll_pending()

        # Kalshi reports only 500 cents; we expected 600.
        rest.settlements_response = {"settlements": [{
            "order_id": "SRV-1", "realized_pnl": 500,
        }]}
        settled = ex.reconcile("KXBTC15M-T1", "yes")
        assert settled[0].computed_pnl_usd == Decimal("6.00")
        assert settled[0].kalshi_reported_pnl_usd == Decimal("5.00")
        assert settled[0].discrepancy_usd == Decimal("1.00")

    def test_reconcile_no_data_resolves_no(self) -> None:
        # CRYPTO15M.pdf §0.5: YES buyer loses on no_data.
        rest = _FakeRestClient()
        ex = KalshiLiveExecutor(
            rest_client=rest, gate=_approved_gate(),
            cancel_timeout_s=60.0, now_us=lambda: _NOW,
        )
        ex.submit(_opp(
            recommended_side="yes",
            hypothetical_fill_price=Decimal("0.40"),
            hypothetical_size_contracts=Decimal("10"),
            quote=_mq(fee_bps=Decimal("0")),
        ))
        client_id = rest.created[0]["client_order_id"]
        rest.fills_response = {"fills": [{
            "client_order_id": client_id, "order_id": "SRV-1",
            "yes_price": 40, "count": 10, "fees": 0,
        }]}
        ex.poll_pending()
        settled = ex.reconcile("KXBTC15M-T1", "no_data")
        assert settled[0].computed_pnl_usd == Decimal("-4.00")

    def test_reconcile_missing_ticker_returns_empty(self) -> None:
        ex = KalshiLiveExecutor(
            rest_client=_FakeRestClient(), gate=_approved_gate(),
        )
        assert ex.reconcile("NOSUCH-T", "yes") == []

    def test_reconcile_rejects_invalid_outcome(self) -> None:
        ex = KalshiLiveExecutor(
            rest_client=_FakeRestClient(), gate=_approved_gate(),
        )
        with pytest.raises(ValueError, match="must be yes"):
            ex.reconcile("KXBTC15M-T1", "garbage")

    def test_reconcile_settlements_endpoint_failure_does_not_block(self) -> None:
        """If /portfolio/settlements is down, we still reconcile locally."""
        rest = _FakeRestClient()

        class _Raising:
            def get_settlements(self, **_):
                raise RuntimeError("503")

        # Wrap: create+fills work on the real fake, settlements fails.
        # Simplest way: replace get_settlements with a raiser after the fill.
        ex = KalshiLiveExecutor(
            rest_client=rest, gate=_approved_gate(),
            cancel_timeout_s=60.0, now_us=lambda: _NOW,
        )
        ex.submit(_opp(
            hypothetical_fill_price=Decimal("0.40"),
            hypothetical_size_contracts=Decimal("10"),
            quote=_mq(fee_bps=Decimal("0")),
        ))
        client_id = rest.created[0]["client_order_id"]
        rest.fills_response = {"fills": [{
            "client_order_id": client_id, "order_id": "SRV-1",
            "yes_price": 40, "count": 10, "fees": 0,
        }]}
        ex.poll_pending()

        # Swap get_settlements to raise.
        rest.get_settlements = lambda **_: (_ for _ in ()).throw(RuntimeError("503"))
        settled = ex.reconcile("KXBTC15M-T1", "yes")
        assert len(settled) == 1
        # Computed P/L still correct; kalshi_reported is None.
        assert settled[0].computed_pnl_usd == Decimal("6.00")
        assert settled[0].kalshi_reported_pnl_usd is None
        assert settled[0].discrepancy_usd is None


# ---------------------------------------------------------------------------
# DB persistence
# ---------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path):
    import migrate_db as m
    url = f"sqlite:///{tmp_path}/live.db"
    m.migrate(url)
    conn = sqlite3.connect(url.removeprefix("sqlite:///"))
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


class TestPersistence:
    def test_submit_writes_live_order_row(self, db) -> None:
        rest = _FakeRestClient()
        ex = KalshiLiveExecutor(
            rest_client=rest, gate=_approved_gate(),
            conn=db, strategy_label="pure_lag", now_us=lambda: _NOW,
        )
        ex.submit(_opp())
        rows = list(db.execute("SELECT * FROM live_orders").fetchall())
        assert len(rows) == 1
        r = rows[0]
        assert r["market_ticker"] == "KXBTC15M-T1"
        assert r["strategy_label"] == "pure_lag"
        assert r["side"] == "yes"
        assert Decimal(r["price"]) == Decimal("0.55")
        assert r["size_contracts"] == 10
        assert r["status"] == "resting"
        assert r["order_id"].startswith("SRV-")
        assert r["client_order_id"].startswith("kt-KXBTC15M-T1-")

    def test_fill_updates_live_order_row(self, db) -> None:
        rest = _FakeRestClient()
        ex = KalshiLiveExecutor(
            rest_client=rest, gate=_approved_gate(),
            conn=db, now_us=lambda: _NOW, cancel_timeout_s=60.0,
        )
        ex.submit(_opp(quote=_mq(fee_bps=Decimal("0"))))
        client_id = rest.created[0]["client_order_id"]
        rest.fills_response = {"fills": [{
            "client_order_id": client_id, "order_id": "SRV-1",
            "yes_price": 55, "count": 10, "fees": 2,
        }]}
        ex.poll_pending()
        row = db.execute("SELECT * FROM live_orders").fetchone()
        assert row["status"] == "filled"
        assert Decimal(row["fill_price"]) == Decimal("0.55")
        assert row["fill_quantity"] == 10
        assert Decimal(row["fees_paid_usd"]) == Decimal("0.02")

    def test_cancel_updates_live_order_row(self, db) -> None:
        rest = _FakeRestClient()
        tick = [_NOW]
        ex = KalshiLiveExecutor(
            rest_client=rest, gate=_approved_gate(),
            conn=db, cancel_timeout_s=3.0, now_us=lambda: tick[0],
        )
        ex.submit(_opp())
        tick[0] = _NOW + 4_000_000
        ex.poll_pending()
        row = db.execute("SELECT * FROM live_orders").fetchone()
        assert row["status"] == "canceled"
        assert row["cancel_reason"] == "timeout"
        assert row["canceled_at_us"] == tick[0]

    def test_reconcile_writes_live_settlement_row(self, db) -> None:
        rest = _FakeRestClient()
        tick = [_NOW]
        ex = KalshiLiveExecutor(
            rest_client=rest, gate=_approved_gate(),
            conn=db, cancel_timeout_s=60.0, now_us=lambda: tick[0],
        )
        ex.submit(_opp(
            hypothetical_fill_price=Decimal("0.40"),
            hypothetical_size_contracts=Decimal("10"),
            quote=_mq(fee_bps=Decimal("0")),
        ))
        client_id = rest.created[0]["client_order_id"]
        rest.fills_response = {"fills": [{
            "client_order_id": client_id, "order_id": "SRV-1",
            "yes_price": 40, "count": 10, "fees": 0,
        }]}
        ex.poll_pending()
        rest.settlements_response = {"settlements": [{
            "order_id": "SRV-1", "realized_pnl": 600,
        }]}
        tick[0] = _NOW + 1_000_000_000
        ex.reconcile("KXBTC15M-T1", "yes")

        rows = list(db.execute("SELECT * FROM live_settlements").fetchall())
        assert len(rows) == 1
        r = rows[0]
        assert r["outcome"] == "yes"
        assert Decimal(r["computed_pnl_usd"]) == Decimal("6.00")
        assert Decimal(r["kalshi_reported_pnl_usd"]) == Decimal("6.00")

    def test_migration_creates_live_tables(self, tmp_path) -> None:
        import migrate_db as m
        url = f"sqlite:///{tmp_path}/fresh.db"
        m.migrate(url)
        conn = sqlite3.connect(url.removeprefix("sqlite:///"))
        try:
            names = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            assert {"live_orders", "live_settlements"}.issubset(names)
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Per-asset execution_enabled gate (runtime flags)
# ---------------------------------------------------------------------------


class _FakeFlagsPoller:
    """Minimal FlagsPoller-shaped stub — no disk, no threads, no mtime."""
    def __init__(self, flags):
        self._flags = flags
    def get(self):
        return self._flags


class TestFlagsExecutionGate:
    def test_submit_blocked_when_asset_execution_disabled(self):
        import runtime_flags as rf
        flags = rf.RuntimeFlags()
        flags.execution_enabled["btc"] = False
        poller = _FakeFlagsPoller(flags)

        rest = _FakeRestClient()
        ex = KalshiLiveExecutor(
            rest_client=rest, gate=_approved_gate(),
            now_us=lambda: _NOW,
            flags_poller=poller,
            asset_by_ticker={"KXBTC15M-T1": "btc"},
        )
        r = ex.submit(_opp())
        assert not r.success
        assert "flag-rejected" in r.reason
        assert "btc" in r.reason
        # REST was never touched — gate fails fast.
        assert rest.created == []

    def test_submit_succeeds_when_asset_execution_enabled(self):
        import runtime_flags as rf
        flags = rf.RuntimeFlags()
        assert flags.is_asset_execution_enabled("btc") is True

        rest = _FakeRestClient()
        ex = KalshiLiveExecutor(
            rest_client=rest, gate=_approved_gate(),
            now_us=lambda: _NOW,
            flags_poller=_FakeFlagsPoller(flags),
            asset_by_ticker={"KXBTC15M-T1": "btc"},
        )
        r = ex.submit(_opp())
        assert r.success
        assert len(rest.created) == 1

    def test_kill_switch_blocks_all_assets(self):
        import runtime_flags as rf
        flags = rf.RuntimeFlags()
        flags.execution_kill_switch = True
        rest = _FakeRestClient()
        ex = KalshiLiveExecutor(
            rest_client=rest, gate=_approved_gate(),
            now_us=lambda: _NOW,
            flags_poller=_FakeFlagsPoller(flags),
            asset_by_ticker={"KXBTC15M-T1": "btc"},
        )
        r = ex.submit(_opp())
        assert not r.success
        assert "kill-switch" in r.reason

    def test_no_poller_means_always_allow(self):
        """Without a flags_poller the executor behaves as before (no gate)."""
        rest = _FakeRestClient()
        ex = KalshiLiveExecutor(
            rest_client=rest, gate=_approved_gate(), now_us=lambda: _NOW,
        )
        r = ex.submit(_opp())
        assert r.success

    def test_unmapped_ticker_bypasses_flag_gate(self):
        """If asset_by_ticker lacks the ticker, we can't make a flag decision —
        default to allow (the risk engine + breaker still have opinions)."""
        import runtime_flags as rf
        flags = rf.RuntimeFlags()
        flags.execution_enabled["btc"] = False   # would block if ticker mapped
        rest = _FakeRestClient()
        ex = KalshiLiveExecutor(
            rest_client=rest, gate=_approved_gate(), now_us=lambda: _NOW,
            flags_poller=_FakeFlagsPoller(flags),
            asset_by_ticker={},  # empty — ticker resolves to ""
        )
        r = ex.submit(_opp())
        assert r.success
