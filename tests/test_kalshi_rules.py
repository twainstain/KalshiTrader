"""Cover `src/risk/kalshi_rules.py` (P2-M1-T12).

≥3 asserts per rule: approve / reject / boundary or edge case. Plus an
end-to-end `RiskEngine` composition test that wires every default rule.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from core.models import MarketQuote, Opportunity, OpportunityStatus
from risk.kalshi_rules import (
    BookDepthRule,
    CIWidthRule,
    DailyLossRule,
    EngineDecision,
    MinEdgeAfterFeesRule,
    NoDataResolveNoRule,
    OpenPositionsRule,
    PositionAccountabilityRule,
    ReferenceFeedStaleRule,
    RiskContext,
    RiskEngine,
    RuleVerdict,
    StrikeProximityRule,
    TimeWindowRule,
    default_rules,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _mq(**overrides) -> MarketQuote:
    """Baseline Kalshi quote — well within every default rule."""
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


def _ctx(**overrides) -> RiskContext:
    base = dict(
        now_us=1_746_000_000_000_000,
        last_reference_tick_us=1_746_000_000_000_000 - 500_000,   # 0.5 s ago
        open_positions=0,
        daily_realized_pnl_usd=Decimal("0"),
        position_notional_by_strike_usd={},
        cf_benchmarks_degraded=False,
    )
    base.update(overrides)
    return RiskContext(**base)


# ---------------------------------------------------------------------------
# Individual rules
# ---------------------------------------------------------------------------


class TestMinEdgeAfterFeesRule:
    def test_approves_when_edge_meets_min(self) -> None:
        v = MinEdgeAfterFeesRule().check(_opp(), _ctx())
        assert v.approved and v.reason == ""

    def test_rejects_when_edge_below_min(self) -> None:
        opp = _opp(expected_edge_bps_after_fees=Decimal("50"))
        v = MinEdgeAfterFeesRule().check(opp, _ctx())
        assert not v.approved
        assert "50" in v.reason and "100" in v.reason

    def test_boundary_equal_to_min_is_approved(self) -> None:
        opp = _opp(expected_edge_bps_after_fees=Decimal("100"))
        v = MinEdgeAfterFeesRule().check(opp, _ctx())
        assert v.approved


class TestTimeWindowRule:
    def test_approves_mid_window(self) -> None:
        opp = _opp(quote=_mq(time_remaining_s=Decimal("30")))
        assert TimeWindowRule().check(opp, _ctx()).approved

    def test_rejects_too_early(self) -> None:
        opp = _opp(quote=_mq(time_remaining_s=Decimal("120")))
        v = TimeWindowRule().check(opp, _ctx())
        assert not v.approved
        assert "120" in v.reason

    def test_rejects_too_late(self) -> None:
        # Within the final 5s: no longer enough time for cancel-on-timeout.
        opp = _opp(quote=_mq(time_remaining_s=Decimal("3")))
        assert not TimeWindowRule().check(opp, _ctx()).approved

    def test_boundaries_are_inclusive(self) -> None:
        for tr in (Decimal("5"), Decimal("60")):
            opp = _opp(quote=_mq(time_remaining_s=tr))
            assert TimeWindowRule().check(opp, _ctx()).approved


class TestCIWidthRule:
    def test_approves_narrow_ci(self) -> None:
        assert CIWidthRule().check(_opp(ci_width=Decimal("0.05")), _ctx()).approved

    def test_rejects_wide_ci(self) -> None:
        v = CIWidthRule().check(_opp(ci_width=Decimal("0.20")), _ctx())
        assert not v.approved and "0.20" in v.reason

    def test_pure_lag_zero_ci_approved(self) -> None:
        # PureLag emits ci_width=0 — must pass this rule.
        assert CIWidthRule().check(_opp(ci_width=Decimal("0")), _ctx()).approved


class TestOpenPositionsRule:
    def test_approves_when_under_cap(self) -> None:
        assert OpenPositionsRule().check(_opp(), _ctx(open_positions=2)).approved

    def test_rejects_at_cap(self) -> None:
        v = OpenPositionsRule().check(_opp(), _ctx(open_positions=3))
        assert not v.approved and "3" in v.reason

    def test_rejects_over_cap(self) -> None:
        assert not OpenPositionsRule().check(_opp(), _ctx(open_positions=5)).approved


class TestDailyLossRule:
    def test_approves_when_flat(self) -> None:
        assert DailyLossRule().check(_opp(), _ctx(daily_realized_pnl_usd=Decimal("0"))).approved

    def test_approves_when_near_stop_not_breached(self) -> None:
        v = DailyLossRule().check(
            _opp(), _ctx(daily_realized_pnl_usd=Decimal("-249"))
        )
        assert v.approved

    def test_rejects_at_stop(self) -> None:
        v = DailyLossRule().check(
            _opp(), _ctx(daily_realized_pnl_usd=Decimal("-250"))
        )
        assert not v.approved

    def test_rejects_over_stop(self) -> None:
        assert not DailyLossRule().check(
            _opp(), _ctx(daily_realized_pnl_usd=Decimal("-500"))
        ).approved


class TestReferenceFeedStaleRule:
    def test_approves_when_fresh(self) -> None:
        ctx = _ctx(now_us=1_000_000_000,
                   last_reference_tick_us=1_000_000_000 - 1_000_000)  # 1 s
        assert ReferenceFeedStaleRule().check(_opp(), ctx).approved

    def test_rejects_when_stale(self) -> None:
        ctx = _ctx(now_us=1_000_000_000,
                   last_reference_tick_us=1_000_000_000 - 5_000_000)  # 5 s
        v = ReferenceFeedStaleRule().check(_opp(), ctx)
        assert not v.approved and "5" in v.reason

    def test_rejects_when_no_tick_recorded(self) -> None:
        ctx = _ctx(last_reference_tick_us=None)
        v = ReferenceFeedStaleRule().check(_opp(), ctx)
        assert not v.approved
        assert "no reference tick" in v.reason

    def test_boundary_exactly_at_limit_approved(self) -> None:
        # Exactly 3 s → still fresh (rule rejects only when > max).
        ctx = _ctx(now_us=1_000_000_000,
                   last_reference_tick_us=1_000_000_000 - 3_000_000)
        assert ReferenceFeedStaleRule().check(_opp(), ctx).approved


class TestBookDepthRule:
    def test_approves_when_buy_side_deep(self) -> None:
        q = _mq(book_depth_yes_usd=Decimal("500"), book_depth_no_usd=Decimal("50"))
        opp = _opp(quote=q, recommended_side="yes")
        assert BookDepthRule().check(opp, _ctx()).approved

    def test_rejects_when_buy_side_shallow(self) -> None:
        q = _mq(book_depth_yes_usd=Decimal("100"), book_depth_no_usd=Decimal("500"))
        opp = _opp(quote=q, recommended_side="yes")
        v = BookDepthRule().check(opp, _ctx())
        assert not v.approved and "100" in v.reason

    def test_ignores_opposite_side_depth(self) -> None:
        # Buying NO; YES can be empty.
        q = _mq(book_depth_yes_usd=Decimal("0"), book_depth_no_usd=Decimal("500"))
        opp = _opp(quote=q, recommended_side="no")
        assert BookDepthRule().check(opp, _ctx()).approved

    def test_side_none_bypasses(self) -> None:
        opp = _opp(recommended_side="none",
                   hypothetical_fill_price=Decimal("0"),
                   hypothetical_size_contracts=Decimal("0"),
                   expected_edge_bps_after_fees=Decimal("0"))
        assert BookDepthRule().check(opp, _ctx()).approved


class TestNoDataResolveNoRule:
    def test_approves_when_feed_healthy(self) -> None:
        assert NoDataResolveNoRule().check(
            _opp(recommended_side="yes"), _ctx(cf_benchmarks_degraded=False)
        ).approved

    def test_rejects_yes_when_feed_degraded(self) -> None:
        v = NoDataResolveNoRule().check(
            _opp(recommended_side="yes"), _ctx(cf_benchmarks_degraded=True)
        )
        assert not v.approved and "YES" in v.reason

    def test_approves_no_even_when_feed_degraded(self) -> None:
        # Buying NO benefits from no-data-resolves-No tail — allowed.
        assert NoDataResolveNoRule().check(
            _opp(recommended_side="no"), _ctx(cf_benchmarks_degraded=True)
        ).approved


class TestPositionAccountabilityRule:
    def test_approves_under_cap(self) -> None:
        opp = _opp(
            hypothetical_fill_price=Decimal("0.50"),
            hypothetical_size_contracts=Decimal("100"),  # $50 incoming
        )
        ctx = _ctx(position_notional_by_strike_usd={"KXBTC15M-T": Decimal("500")})
        # Total $550 < $2,500 cap.
        assert PositionAccountabilityRule().check(opp, ctx).approved

    def test_rejects_when_incoming_tips_over_cap(self) -> None:
        opp = _opp(
            hypothetical_fill_price=Decimal("0.90"),
            hypothetical_size_contracts=Decimal("1000"),  # $900 incoming
        )
        ctx = _ctx(position_notional_by_strike_usd={"KXBTC15M-T": Decimal("2000")})
        v = PositionAccountabilityRule().check(opp, ctx)
        assert not v.approved
        assert "2900" in v.reason

    def test_per_strike_independent(self) -> None:
        # Other strike at cap — this strike has no position.
        opp = _opp(
            hypothetical_fill_price=Decimal("0.50"),
            hypothetical_size_contracts=Decimal("100"),
        )
        ctx = _ctx(position_notional_by_strike_usd={"DIFFERENT-T": Decimal("2500")})
        assert PositionAccountabilityRule().check(opp, ctx).approved


class TestStrikeProximityRule:
    def test_approves_when_spot_far_from_strike(self) -> None:
        # BTC $66,000 strike, spot $66,100 → 15.15 bps gap > default 10 bps
        q = _mq(strike=Decimal("66000"), reference_price=Decimal("66100"))
        assert StrikeProximityRule().check(_opp(quote=q), _ctx()).approved

    def test_rejects_when_spot_inside_buffer(self) -> None:
        # Gap = (66005 − 66000)/66000 × 10000 ≈ 0.76 bps — well under 10 bps.
        q = _mq(strike=Decimal("66000"), reference_price=Decimal("66005"))
        v = StrikeProximityRule().check(_opp(quote=q), _ctx())
        assert not v.approved
        assert "0.76" in v.reason or "within" in v.reason

    def test_applies_symmetrically_below_strike(self) -> None:
        q = _mq(strike=Decimal("66000"), reference_price=Decimal("65995"))
        assert not StrikeProximityRule().check(_opp(quote=q), _ctx()).approved

    def test_bypasses_non_threshold_comparators(self) -> None:
        # `between`/`exactly`/`at_least` have different geometry — rule skips.
        for cmp in ("between", "exactly", "at_least"):
            q = _mq(
                strike=Decimal("66000"),
                reference_price=Decimal("66000"),  # would reject under above/below
                comparator=cmp,
            )
            assert StrikeProximityRule().check(_opp(quote=q), _ctx()).approved

    def test_zero_strike_fails_closed(self) -> None:
        # MarketQuote allows strike=0 (degenerate); rule must not divide-by-zero.
        q = _mq(strike=Decimal("0"), reference_price=Decimal("0"))
        v = StrikeProximityRule().check(_opp(quote=q), _ctx())
        assert not v.approved and "zero" in v.reason

    def test_custom_threshold_respected(self) -> None:
        # 50 bps threshold: 20 bps gap should now reject.
        q = _mq(strike=Decimal("66000"), reference_price=Decimal("66132"))  # ~20 bps
        rule = StrikeProximityRule(min_bps=Decimal("50"))
        assert not rule.check(_opp(quote=q), _ctx()).approved


# ---------------------------------------------------------------------------
# Engine composition
# ---------------------------------------------------------------------------


class TestRiskEngine:
    def test_all_default_rules_approve_baseline(self) -> None:
        engine = RiskEngine(default_rules())
        dec = engine.decide(_opp(), _ctx())
        assert dec.approved
        assert len(dec.verdicts) == 10
        assert all(v.approved for v in dec.verdicts)

    def test_single_rejection_blocks(self) -> None:
        # Edge below min → min_edge rule rejects; rest still approve.
        engine = RiskEngine(default_rules())
        dec = engine.decide(_opp(expected_edge_bps_after_fees=Decimal("50")), _ctx())
        assert not dec.approved
        assert len(dec.rejections) == 1
        assert dec.rejections[0].rule_name == "min_edge_after_fees"

    def test_multiple_rejections_all_reported(self) -> None:
        # Edge too low AND daily loss breached → two rejections.
        engine = RiskEngine(default_rules())
        dec = engine.decide(
            _opp(expected_edge_bps_after_fees=Decimal("50")),
            _ctx(daily_realized_pnl_usd=Decimal("-300")),
        )
        names = {v.rule_name for v in dec.rejections}
        assert {"min_edge_after_fees", "daily_loss"}.issubset(names)

    def test_engine_returns_rule_tuple(self) -> None:
        # Constructor accepts any iterable; exposes tuple for introspection.
        r1 = MinEdgeAfterFeesRule()
        r2 = TimeWindowRule()
        engine = RiskEngine([r1, r2])
        assert engine.rules == (r1, r2)

    def test_empty_engine_approves_everything(self) -> None:
        # Pathological edge case: no rules configured → trivially approved.
        engine = RiskEngine([])
        dec = engine.decide(_opp(), _ctx())
        assert dec.approved and dec.verdicts == ()
