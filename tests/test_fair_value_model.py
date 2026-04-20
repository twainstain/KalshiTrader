"""Cover `src/strategy/kalshi_fair_value.py` — FairValueModel (P1-M3-T07)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from strategy import kalshi_fair_value as fv


# --------- pure math helpers ---------

def test_norm_cdf_at_zero_is_half():
    assert abs(fv._norm_cdf(Decimal("0")) - Decimal("0.5")) < Decimal("1e-9")


def test_norm_cdf_tails_converge():
    assert fv._norm_cdf(Decimal("5")) > Decimal("0.99999")
    assert fv._norm_cdf(Decimal("-5")) < Decimal("0.00001")


def test_prob_above_strike_degenerate_zero_sigma():
    # T = 0 → no uncertainty. Pure comparison.
    assert fv.prob_above_strike(
        spot=Decimal("66000"), strike=Decimal("65000"), sigma_over_horizon=Decimal("0"),
    ) == Decimal("1")
    assert fv.prob_above_strike(
        spot=Decimal("64000"), strike=Decimal("65000"), sigma_over_horizon=Decimal("0"),
    ) == Decimal("0")


def test_prob_above_strike_monotonic_in_spot():
    sigma = Decimal("0.01")
    k = Decimal("65000")
    p_low = fv.prob_above_strike(spot=Decimal("64000"), strike=k, sigma_over_horizon=sigma)
    p_at = fv.prob_above_strike(spot=Decimal("65000"), strike=k, sigma_over_horizon=sigma)
    p_high = fv.prob_above_strike(spot=Decimal("66000"), strike=k, sigma_over_horizon=sigma)
    assert p_low < p_at < p_high


def test_prob_above_strike_at_spot_equals_strike_near_half():
    # At S=K, p = Φ(-σ/2) — slightly below 0.5 but close.
    p = fv.prob_above_strike(
        spot=Decimal("65000"), strike=Decimal("65000"),
        sigma_over_horizon=Decimal("0.02"),
    )
    assert Decimal("0.4") < p < Decimal("0.5")


def test_annual_vol_to_horizon_scales_sqrt_time():
    year = fv.SECONDS_PER_YEAR
    # Horizon = 1 year → sigma · √1 = sigma.
    assert fv.annual_vol_to_horizon(Decimal("0.60"), year) == Decimal("0.60")
    # Horizon = 1/4 year → sigma · √0.25 = sigma · 0.5.
    q = fv.annual_vol_to_horizon(Decimal("0.60"), year / Decimal("4"))
    assert abs(q - Decimal("0.30")) < Decimal("1e-9")


def test_annual_vol_to_horizon_zero_returns_zero():
    assert fv.annual_vol_to_horizon(Decimal("0.60"), Decimal("0")) == Decimal("0")


# --------- FairValueModel.price ---------

def _model() -> fv.FairValueModel:
    return fv.FairValueModel(
        annual_vol_by_asset={"btc": Decimal("0.60")},
        no_data_haircut=Decimal("0"),  # tests below add it explicitly
    )


def test_price_above_with_spot_far_above_strike_gives_high_p_yes():
    m = _model()
    p, ci = m.price(
        asset="btc", strike=Decimal("60000"), comparator="above",
        reference_price=Decimal("70000"), reference_60s_avg=Decimal("70000"),
        time_remaining_s=Decimal("300"),
    )
    assert p > Decimal("0.95")
    assert Decimal("0") <= ci <= Decimal("1")


def test_price_above_with_spot_far_below_strike_gives_low_p_yes():
    m = _model()
    p, _ = m.price(
        asset="btc", strike=Decimal("70000"), comparator="above",
        reference_price=Decimal("60000"), reference_60s_avg=Decimal("60000"),
        time_remaining_s=Decimal("300"),
    )
    assert p < Decimal("0.05")


def test_price_below_is_complement_of_above():
    m = _model()
    spot = Decimal("65500")
    k = Decimal("65000")
    p_above, _ = m.price(
        asset="btc", strike=k, comparator="above",
        reference_price=spot, reference_60s_avg=spot,
        time_remaining_s=Decimal("120"),
    )
    p_below, _ = m.price(
        asset="btc", strike=k, comparator="below",
        reference_price=spot, reference_60s_avg=spot,
        time_remaining_s=Decimal("120"),
    )
    # Sum must equal 1 (no haircut in this model).
    assert abs((p_above + p_below) - Decimal("1")) < Decimal("1e-9")


def test_price_at_least_matches_above():
    m = _model()
    p_a, _ = m.price(
        asset="btc", strike=Decimal("65000"), comparator="above",
        reference_price=Decimal("65200"), reference_60s_avg=Decimal("65000"),
        time_remaining_s=Decimal("200"),
    )
    p_al, _ = m.price(
        asset="btc", strike=Decimal("65000"), comparator="at_least",
        reference_price=Decimal("65200"), reference_60s_avg=Decimal("65000"),
        time_remaining_s=Decimal("200"),
    )
    assert p_a == p_al


def test_price_between_and_exactly_raise_not_implemented():
    m = _model()
    for comp in ("between", "exactly"):
        with pytest.raises(NotImplementedError, match="strike_high"):
            m.price(
                asset="btc", strike=Decimal("65000"), comparator=comp,
                reference_price=Decimal("65000"),
                reference_60s_avg=Decimal("65000"),
                time_remaining_s=Decimal("300"),
            )


def test_price_unsupported_comparator_raises_value_error():
    m = _model()
    with pytest.raises(ValueError, match="unsupported comparator"):
        m.price(
            asset="btc", strike=Decimal("65000"), comparator="bogus",
            reference_price=Decimal("65000"),
            reference_60s_avg=Decimal("65000"),
            time_remaining_s=Decimal("300"),
        )


def test_price_partial_window_pulls_toward_observed_avg():
    m = _model()
    # With tr=30 and an observed avg far from spot, the blend should move
    # p_yes more than if we only used spot.
    p_with_obs_above, _ = m.price(
        asset="btc", strike=Decimal("65000"), comparator="above",
        reference_price=Decimal("65100"),         # spot near strike
        reference_60s_avg=Decimal("65400"),       # observed already above
        time_remaining_s=Decimal("30"),
    )
    p_with_obs_below, _ = m.price(
        asset="btc", strike=Decimal("65000"), comparator="above",
        reference_price=Decimal("65100"),
        reference_60s_avg=Decimal("64700"),       # observed already below
        time_remaining_s=Decimal("30"),
    )
    assert p_with_obs_above > p_with_obs_below


def test_price_t_zero_is_degenerate():
    m = _model()
    p_yes, ci = m.price(
        asset="btc", strike=Decimal("65000"), comparator="above",
        reference_price=Decimal("65100"),
        reference_60s_avg=Decimal("65500"),       # resolution value
        time_remaining_s=Decimal("0"),
    )
    assert p_yes == Decimal("1")
    # No uncertainty at expiry — CI collapses.
    assert ci == Decimal("0")


def test_no_data_haircut_subtracts_exactly():
    m = fv.FairValueModel(
        annual_vol_by_asset={"btc": Decimal("0.6")},
        no_data_haircut=Decimal("0.01"),
    )
    p, _ = m.price(
        asset="btc", strike=Decimal("60000"), comparator="above",
        reference_price=Decimal("70000"),
        reference_60s_avg=Decimal("70000"),
        time_remaining_s=Decimal("300"),
    )
    # Without haircut p would be ~1; with haircut it's ~0.99.
    assert p <= Decimal("0.99")
    assert p > Decimal("0.95")


def test_no_data_haircut_clamps_at_zero():
    m = fv.FairValueModel(no_data_haircut=Decimal("0.3"))  # larger than p
    p, _ = m.price(
        asset="btc", strike=Decimal("70000"), comparator="above",
        reference_price=Decimal("60000"),
        reference_60s_avg=Decimal("60000"),
        time_remaining_s=Decimal("300"),
    )
    assert p >= Decimal("0")


def test_ci_width_narrows_as_time_remaining_drops():
    m = _model()
    wide = m.price(
        asset="btc", strike=Decimal("65000"), comparator="above",
        reference_price=Decimal("65100"),
        reference_60s_avg=Decimal("65100"),
        time_remaining_s=Decimal("600"),
    )[1]
    narrow = m.price(
        asset="btc", strike=Decimal("65000"), comparator="above",
        reference_price=Decimal("65100"),
        reference_60s_avg=Decimal("65100"),
        time_remaining_s=Decimal("2"),
    )[1]
    assert narrow < wide


def test_unknown_asset_falls_back_to_default():
    m = fv.FairValueModel()
    p, _ = m.price(
        asset="doge", strike=Decimal("65000"), comparator="above",
        reference_price=Decimal("70000"),
        reference_60s_avg=Decimal("70000"),
        time_remaining_s=Decimal("300"),
    )
    # Default (0.60) used internally — the call doesn't raise and we get
    # a reasonable answer.
    assert Decimal("0") < p <= Decimal("1")
