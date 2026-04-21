"""Cover `src/strategy/partial_avg_fair_value.py`."""

from __future__ import annotations

from decimal import Decimal

from core.models import MarketQuote
from strategy.kalshi_fair_value import StrategyConfig
from strategy.partial_avg_fair_value import (
    PartialAvgFairValueModel,
    PartialAvgFairValueStrategy,
    WINDOW_AVG_SECONDS,
)


def _model(**overrides) -> PartialAvgFairValueModel:
    return PartialAvgFairValueModel(
        no_data_haircut=Decimal("0"),
        **overrides,
    )


# ----------------------------------------------------------------------
# _window_split
# ----------------------------------------------------------------------

def test_window_split_no_observation_long_remaining():
    m = PartialAvgFairValueModel()
    fut, tau = m._window_split(Decimal("300"), Decimal("0"))
    # Future window = full 60s; starts 240s from now.
    assert fut == WINDOW_AVG_SECONDS
    assert tau == Decimal("240")


def test_window_split_partial_observation():
    m = PartialAvgFairValueModel()
    fut, tau = m._window_split(Decimal("30"), Decimal("30"))
    # Already observed 30s; 30s remains, starting now.
    assert fut == Decimal("30")
    assert tau == Decimal("0")


def test_window_split_zero_observed_but_inside_window():
    m = PartialAvgFairValueModel()
    # T_remaining = 45s but observed_window_s = 0 (no tick data).
    # Damp window to 45s starting now.
    fut, tau = m._window_split(Decimal("45"), Decimal("0"))
    assert fut == Decimal("45")
    assert tau == Decimal("0")


# ----------------------------------------------------------------------
# _sigma_effective — damping behavior
# ----------------------------------------------------------------------

def test_sigma_effective_shrinks_as_window_fills():
    m = PartialAvgFairValueModel()
    sigma_full = Decimal("0.003")
    # Compute σ_eff at T_remaining = 120 (60 observed) vs T_remaining = 300 (no obs)
    fut_300, tau_300 = m._window_split(Decimal("300"), Decimal("0"))
    sig_300 = m._sigma_effective(
        sigma_full=sigma_full, future_s=fut_300, tau_fs=tau_300,
    )
    fut_30_obs, tau_30_obs = m._window_split(Decimal("30"), Decimal("30"))
    sig_30_obs = m._sigma_effective(
        sigma_full=sigma_full, future_s=fut_30_obs, tau_fs=tau_30_obs,
    )
    # At T=30 with 30s observed, σ_eff should be much smaller than at T=300
    # (unobserved) — the 60s-avg damping kicks in hard.
    assert sig_30_obs < sig_300 / 2


# ----------------------------------------------------------------------
# price() — correctness on synthetic cases
# ----------------------------------------------------------------------

def test_price_spot_above_strike_long_remaining():
    m = _model()
    # Spot 1% above strike, 10 min remaining, σ=1%/15min → strong yes lean.
    p_yes, ci = m.price(
        asset="btc", strike=Decimal("65000"), comparator="above",
        reference_price=Decimal("65650"), reference_60s_avg=Decimal("65500"),
        time_remaining_s=Decimal("600"),
    )
    assert p_yes > Decimal("0.80")


def test_price_spot_equal_strike_no_observation():
    m = _model()
    # Spot at strike, no observation → near 50/50.
    p_yes, _ = m.price(
        asset="btc", strike=Decimal("65000"), comparator="above",
        reference_price=Decimal("65000"), reference_60s_avg=Decimal("65000"),
        time_remaining_s=Decimal("600"),
    )
    assert abs(p_yes - Decimal("0.5")) < Decimal("0.05")


def test_price_partial_obs_dominates_spot():
    """Observed avg dominates near close — even if spot is at strike,
    a high observed avg should push p_yes well above 0.5.
    """
    m = _model()
    # T_remaining=30, 30s observed with observed_avg far above strike.
    # Spot is at strike but observed_avg is way above → blend pulls above.
    p_yes, _ = m.price(
        asset="btc", strike=Decimal("65000"), comparator="above",
        reference_price=Decimal("65000"),  # spot at strike
        reference_60s_avg=Decimal("65000"),
        time_remaining_s=Decimal("30"),
        observed_window_s=Decimal("30"),
        observed_window_avg=Decimal("65200"),  # 30 bps above strike
    )
    # Should be confidently yes.
    assert p_yes > Decimal("0.90")


def test_price_partial_obs_below_strike_favors_no():
    m = _model()
    p_yes, _ = m.price(
        asset="btc", strike=Decimal("65000"), comparator="above",
        reference_price=Decimal("65000"),
        reference_60s_avg=Decimal("65000"),
        time_remaining_s=Decimal("30"),
        observed_window_s=Decimal("30"),
        observed_window_avg=Decimal("64800"),
    )
    assert p_yes < Decimal("0.10")


def test_price_down_comparator_flips():
    m = _model()
    p_up, _ = m.price(
        asset="btc", strike=Decimal("65000"), comparator="above",
        reference_price=Decimal("66000"), reference_60s_avg=Decimal("65500"),
        time_remaining_s=Decimal("600"),
    )
    p_down, _ = m.price(
        asset="btc", strike=Decimal("65000"), comparator="below",
        reference_price=Decimal("66000"), reference_60s_avg=Decimal("65500"),
        time_remaining_s=Decimal("600"),
    )
    # After haircut (0) p_up + p_down ≈ 1
    assert abs((p_up + p_down) - Decimal("1")) < Decimal("0.001")


def test_price_at_least_comparator_equivalent_to_above():
    m = _model()
    p_above, _ = m.price(
        asset="btc", strike=Decimal("65000"), comparator="above",
        reference_price=Decimal("65500"), reference_60s_avg=Decimal("65250"),
        time_remaining_s=Decimal("300"),
    )
    p_atleast, _ = m.price(
        asset="btc", strike=Decimal("65000"), comparator="at_least",
        reference_price=Decimal("65500"), reference_60s_avg=Decimal("65250"),
        time_remaining_s=Decimal("300"),
    )
    assert p_above == p_atleast


def test_price_between_not_supported():
    m = _model()
    import pytest
    with pytest.raises(NotImplementedError):
        m.price(
            asset="btc", strike=Decimal("65000"), comparator="between",
            reference_price=Decimal("65000"), reference_60s_avg=Decimal("65000"),
            time_remaining_s=Decimal("600"),
        )


def test_price_degrades_when_all_observed():
    """Edge case: T_remaining ≤ 0 with observed_window_s = 60 — full window
    observed. σ_effective → near 0, p_yes ≈ 1 if obs_avg > strike.
    """
    m = _model()
    p_yes, _ = m.price(
        asset="btc", strike=Decimal("65000"), comparator="above",
        reference_price=Decimal("65000"),
        reference_60s_avg=Decimal("65000"),
        time_remaining_s=Decimal("0.001"),  # effectively zero
        observed_window_s=Decimal("60"),
        observed_window_avg=Decimal("65100"),
    )
    assert p_yes > Decimal("0.99")


def test_price_applies_no_data_haircut():
    m = PartialAvgFairValueModel(no_data_haircut=Decimal("0.01"))
    p_yes, _ = m.price(
        asset="btc", strike=Decimal("65000"), comparator="above",
        reference_price=Decimal("70000"),  # far above strike
        reference_60s_avg=Decimal("68000"),
        time_remaining_s=Decimal("600"),
    )
    # Should be ≥ 0.98 without haircut; with 1% haircut, lower.
    assert p_yes < Decimal("0.999")


def test_price_no_negative_probability():
    m = PartialAvgFairValueModel(no_data_haircut=Decimal("0.5"))
    p_yes, _ = m.price(
        asset="btc", strike=Decimal("65000"), comparator="above",
        reference_price=Decimal("50000"),  # spot far below strike
        reference_60s_avg=Decimal("55000"),
        time_remaining_s=Decimal("600"),
    )
    assert p_yes >= Decimal("0")


# ----------------------------------------------------------------------
# Comparison vs FairValueModel — variance damping
# ----------------------------------------------------------------------

# ----------------------------------------------------------------------
# PartialAvgFairValueStrategy — live wrapper with tick buffer
# ----------------------------------------------------------------------

def _quote(**overrides):
    defaults = dict(
        venue="kalshi", market_ticker="KXBTC15M-X", series_ticker="KXBTC15M",
        event_ticker="KXBTC15M-E", best_yes_ask=Decimal("0.40"),
        best_no_ask=Decimal("0.55"), best_yes_bid=Decimal("0.39"),
        best_no_bid=Decimal("0.54"), book_depth_yes_usd=Decimal("500"),
        book_depth_no_usd=Decimal("500"), fee_bps=Decimal("35"),
        expiration_ts=Decimal("1800"), strike=Decimal("65000"),
        comparator="above", reference_price=Decimal("65100"),
        reference_60s_avg=Decimal("65050"), time_remaining_s=Decimal("600"),
        quote_timestamp_us=0,
    )
    defaults.update(overrides)
    return MarketQuote(**defaults)


def test_strategy_no_ticks_still_prices_via_model_defaults():
    """No reference ticks fed → strategy should still emit an Opportunity
    (degrades to forecast-only mode)."""
    s = PartialAvgFairValueStrategy(
        PartialAvgFairValueModel(no_data_haircut=Decimal("0")),
        StrategyConfig(
            min_edge_bps_after_fees=Decimal("50"),
            time_window_seconds=(0, 900),
        ),
    )
    # Spot 2% above strike → confident yes.
    q = _quote(best_yes_ask=Decimal("0.50"), reference_price=Decimal("66300"))
    opp = s.evaluate(q, asset="btc")
    assert opp is not None
    assert opp.recommended_side == "yes"


def test_strategy_uses_observed_ticks_to_flip_side():
    """With 30s observed far below strike, strategy should prefer no
    even though spot is at strike."""
    now_state = {"t": 1_000_000_000_000_000}
    s = PartialAvgFairValueStrategy(
        PartialAvgFairValueModel(no_data_haircut=Decimal("0")),
        StrategyConfig(
            min_edge_bps_after_fees=Decimal("50"),
            time_window_seconds=(0, 900),
        ),
        now_us=lambda: now_state["t"],
    )
    # Feed 30 ticks over the past 30s all well below strike.
    for k in range(30):
        now_state["t"] = 1_000_000_000_000_000 + k * 1_000_000
        s.record_reference_tick("btc", Decimal("64700"))
    # Now "now" is 30s into the close-60s window; time_remaining=30.
    now_state["t"] = 1_000_000_000_000_000 + 30 * 1_000_000
    q = _quote(
        reference_price=Decimal("65000"),
        best_yes_ask=Decimal("0.50"), best_no_ask=Decimal("0.45"),
        time_remaining_s=Decimal("30"),
    )
    opp = s.evaluate(q, asset="btc")
    assert opp is not None
    assert opp.recommended_side == "no"


def test_strategy_rejects_outside_time_window():
    s = PartialAvgFairValueStrategy(
        PartialAvgFairValueModel(),
        StrategyConfig(time_window_seconds=(120, 900)),
    )
    q = _quote(time_remaining_s=Decimal("60"))
    assert s.evaluate(q, asset="btc") is None


def test_strategy_rejects_thin_book():
    s = PartialAvgFairValueStrategy(
        PartialAvgFairValueModel(),
        StrategyConfig(min_book_depth_usd=Decimal("10000")),
    )
    q = _quote()  # depth is only 500
    assert s.evaluate(q, asset="btc") is None


def test_strategy_evaluate_many_filters_unmapped_tickers():
    s = PartialAvgFairValueStrategy(
        PartialAvgFairValueModel(no_data_haircut=Decimal("0")),
        StrategyConfig(
            min_edge_bps_after_fees=Decimal("50"),
            time_window_seconds=(0, 900),
        ),
    )
    q1 = _quote(market_ticker="KXBTC15M-X", reference_price=Decimal("66000"),
                best_yes_ask=Decimal("0.50"))
    q2 = _quote(market_ticker="KXFOO-X", reference_price=Decimal("66000"),
                best_yes_ask=Decimal("0.50"))
    opps = s.evaluate_many(
        [q1, q2], asset_by_ticker={"KXBTC15M-X": "btc"},
    )
    assert len(opps) == 1


def test_partial_avg_more_confident_than_stat_model_near_close():
    """Core hypothesis: with 30s observed, partial_avg should be more
    confident than naïve FairValueModel at the same spot/strike.
    """
    from strategy.kalshi_fair_value import FairValueModel
    stat = FairValueModel(no_data_haircut=Decimal("0"))
    partial = PartialAvgFairValueModel(no_data_haircut=Decimal("0"))
    # T=30, spot 50 bps above strike, no observation differing from spot.
    p_stat, _ = stat.price(
        asset="btc", strike=Decimal("65000"), comparator="above",
        reference_price=Decimal("65325"), reference_60s_avg=Decimal("65325"),
        time_remaining_s=Decimal("30"),
    )
    p_partial, _ = partial.price(
        asset="btc", strike=Decimal("65000"), comparator="above",
        reference_price=Decimal("65325"), reference_60s_avg=Decimal("65325"),
        time_remaining_s=Decimal("30"),
        observed_window_s=Decimal("30"),
        observed_window_avg=Decimal("65325"),
    )
    # Both confidently yes; partial_avg should be *more* confident due to
    # observed portion having zero variance.
    assert p_partial >= p_stat
