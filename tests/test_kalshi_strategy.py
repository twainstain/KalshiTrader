"""Cover `KalshiFairValueStrategy` with the refactored up/down model."""

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
        strike=Decimal("65000"),            # prior window's 60s-avg
        comparator="at_least",
        reference_price=Decimal("65500"),   # 77 bps above strike → Yes-favored
        reference_60s_avg=Decimal("65500"),
        time_remaining_s=Decimal("30"),
        quote_timestamp_us=1_746_000_000_000_000,
    )
    base.update(overrides)
    return MarketQuote(**base)


def _strategy(**cfg_overrides) -> KalshiFairValueStrategy:
    model = FairValueModel(
        sigma_15min_by_asset={"btc": Decimal("0.002")},
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
    # Put spot = strike + long time → p_yes ≈ 0.5 → Bernoulli CI ≈ 1.
    s = _strategy(max_ci_width=Decimal("0.10"))
    q = _mq(
        reference_price=Decimal("65000"),
        reference_60s_avg=Decimal("65000"),
        time_remaining_s=Decimal("600"),
    )
    assert s.evaluate(q, asset="btc") is None


def test_rejects_when_edge_below_floor():
    s = _strategy(min_edge_bps_after_fees=Decimal("100000"))
    q = _mq()
    assert s.evaluate(q, asset="btc") is None


def test_rejects_unsupported_comparator_gracefully():
    s = _strategy()

    class AlwaysRaises:
        def price(self, **_kw):
            raise NotImplementedError("between needs strike_high")

    s.model = AlwaysRaises()  # type: ignore[assignment]
    q = _mq()
    assert s.evaluate(q, asset="btc") is None


# ---- approvals ----

def test_picks_yes_when_p_above_ask():
    # spot ≈ 77 bps above strike, T=30s → p_yes near 1.
    s = _strategy(min_edge_bps_after_fees=Decimal("100"))
    opp = s.evaluate(_mq(), asset="btc")
    assert opp is not None
    assert opp.recommended_side == "yes"
    assert opp.hypothetical_fill_price == Decimal("0.55")
    assert opp.status == OpportunityStatus.PRICED
    assert opp.expected_edge_bps_after_fees >= Decimal("100")


def test_picks_no_when_spot_far_below_strike():
    # 50 bps below strike → p_yes ≈ 0 → edge on No side.
    s = _strategy(min_edge_bps_after_fees=Decimal("100"))
    q = _mq(
        reference_price=Decimal("64500"),
        reference_60s_avg=Decimal("64500"),
        best_yes_ask=Decimal("0.55"),
        best_no_ask=Decimal("0.40"),
    )
    opp = s.evaluate(q, asset="btc")
    assert opp is not None
    assert opp.recommended_side == "no"
    assert opp.hypothetical_fill_price == Decimal("0.40")


def test_opportunity_populates_all_fields():
    s = _strategy(min_edge_bps_after_fees=Decimal("100"))
    opp = s.evaluate(_mq(), asset="btc")
    assert opp is not None
    assert opp.quote.market_ticker == "KXBTC15M-T"
    assert Decimal("0") <= opp.p_yes <= Decimal("1")
    assert opp.ci_width >= Decimal("0")
    assert opp.hypothetical_size_contracts == s.config.hypothetical_size_contracts
    assert opp.no_data_haircut_bps == Decimal("0.005") * BPS_DIVISOR


def test_evaluate_many_respects_asset_map():
    s = _strategy(min_edge_bps_after_fees=Decimal("100"))
    quotes = [_mq(market_ticker="A-1"), _mq(market_ticker="B-1")]
    out = s.evaluate_many(quotes, asset_by_ticker={"A-1": "btc"})
    assert len(out) == 1
    assert out[0].quote.market_ticker == "A-1"


def test_evaluate_many_empty_when_no_asset_mapping():
    s = _strategy()
    quotes = [_mq(market_ticker="A-1")]
    out = s.evaluate_many(quotes, asset_by_ticker={})
    assert out == []
