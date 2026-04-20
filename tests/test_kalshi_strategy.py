"""Cover `src/strategy/kalshi_fair_value.py` — KalshiFairValueStrategy."""

from __future__ import annotations

from decimal import Decimal

import pytest

from core.models import BPS_DIVISOR, MarketQuote, Opportunity, OpportunityStatus
from strategy.kalshi_fair_value import (
    FairValueModel,
    KalshiFairValueStrategy,
    StrategyConfig,
)


def _mq(**overrides) -> MarketQuote:
    """A spot-above-strike quote with thick books on both sides."""
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
        reference_price=Decimal("66000"),      # spot well above strike
        reference_60s_avg=Decimal("66000"),
        time_remaining_s=Decimal("30"),
        quote_timestamp_us=1_746_000_000_000_000,
    )
    base.update(overrides)
    return MarketQuote(**base)


def _strategy(**cfg_overrides) -> KalshiFairValueStrategy:
    model = FairValueModel(
        annual_vol_by_asset={"btc": Decimal("0.60")},
        no_data_haircut=Decimal("0.005"),
    )
    cfg = StrategyConfig(**cfg_overrides)
    return KalshiFairValueStrategy(model, cfg)


# ---- rejections ----

def test_rejects_when_books_too_thin():
    s = _strategy(min_book_depth_usd=Decimal("1000"))
    q = _mq(book_depth_yes_usd=Decimal("50"), book_depth_no_usd=Decimal("50"))
    assert s.evaluate(q, asset="btc") is None


def test_rejects_when_time_remaining_out_of_window():
    s = _strategy(time_window_seconds=(0, 60))
    q = _mq(time_remaining_s=Decimal("300"))
    assert s.evaluate(q, asset="btc") is None


def test_rejects_when_ci_too_wide():
    # Place spot near strike so p_yes ≈ 0.5 → bernoulli-sd is at its max
    # (≈ 0.5) and ci_width is largest. Compare against an impossibly tight
    # threshold so rejection is unambiguous.
    s = _strategy(max_ci_width=Decimal("0.001"))
    q = _mq(
        reference_price=Decimal("65000"),
        reference_60s_avg=Decimal("65000"),
        time_remaining_s=Decimal("600"),
    )
    assert s.evaluate(q, asset="btc") is None


def test_rejects_when_edge_below_floor():
    s = _strategy(min_edge_bps_after_fees=Decimal("100000"))
    q = _mq()  # even with spot >> strike, 100% floor is impossible
    assert s.evaluate(q, asset="btc") is None


def test_rejects_unsupported_comparator_gracefully():
    s = _strategy()
    # MarketQuote validates comparator, so we use `below` as a supported
    # substitute and separately verify between/exactly in the model tests.
    # Here we just confirm that if the model raises NotImplementedError,
    # the strategy returns None instead of propagating.
    # Patch the model to raise.
    class AlwaysRaises:
        def price(self, **_kw):
            raise NotImplementedError("between needs strike_high")
    s.model = AlwaysRaises()  # type: ignore[assignment]
    q = _mq()
    assert s.evaluate(q, asset="btc") is None


# ---- approvals ----

def test_picks_yes_when_p_above_ask():
    # Spot far above strike → p_yes ~ 0.99+ (minus haircut); yes_ask = 0.55.
    # Edge = 0.99 - 0.55 - 0.0035 = ~43 pp = 4300 bps.
    s = _strategy(min_edge_bps_after_fees=Decimal("100"), max_ci_width=Decimal("0.30"))
    opp = s.evaluate(_mq(), asset="btc")
    assert opp is not None
    assert opp.recommended_side == "yes"
    assert opp.hypothetical_fill_price == Decimal("0.55")
    assert opp.status == OpportunityStatus.PRICED
    assert opp.expected_edge_bps_after_fees >= Decimal("100")


def test_picks_no_when_spot_far_below_strike():
    # Flip: spot far below strike → p_yes ~ 0 → no_edge = 1 - 0 - no_ask - fee ≈ 0.55.
    s = _strategy(min_edge_bps_after_fees=Decimal("100"), max_ci_width=Decimal("0.30"))
    q = _mq(
        reference_price=Decimal("60000"),
        reference_60s_avg=Decimal("60000"),
        best_yes_ask=Decimal("0.55"),
        best_no_ask=Decimal("0.40"),
    )
    opp = s.evaluate(q, asset="btc")
    assert opp is not None
    assert opp.recommended_side == "no"
    assert opp.hypothetical_fill_price == Decimal("0.40")


def test_opportunity_populates_all_fields():
    s = _strategy(min_edge_bps_after_fees=Decimal("100"), max_ci_width=Decimal("0.30"))
    opp = s.evaluate(_mq(), asset="btc")
    assert opp is not None
    assert opp.quote.market_ticker == "KXBTC15M-T"
    assert Decimal("0") <= opp.p_yes <= Decimal("1")
    assert opp.ci_width >= Decimal("0")
    assert opp.hypothetical_size_contracts == s.config.hypothetical_size_contracts
    # Haircut recorded in bps on the opp itself.
    assert opp.no_data_haircut_bps == Decimal("0.005") * BPS_DIVISOR


def test_evaluate_many_respects_asset_map():
    s = _strategy(min_edge_bps_after_fees=Decimal("100"), max_ci_width=Decimal("0.30"))
    quotes = [_mq(market_ticker="A-1"), _mq(market_ticker="B-1")]
    # Only A-1 is mapped; B-1 silently skipped.
    out = s.evaluate_many(quotes, asset_by_ticker={"A-1": "btc"})
    assert len(out) == 1
    assert out[0].quote.market_ticker == "A-1"


def test_evaluate_many_empty_when_no_asset_mapping():
    s = _strategy()
    quotes = [_mq(market_ticker="A-1")]
    out = s.evaluate_many(quotes, asset_by_ticker={})
    assert out == []
