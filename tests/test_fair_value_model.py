"""Cover the refactored `FairValueModel` — up/down single-parameter model."""

from __future__ import annotations

import math
from decimal import Decimal

import pytest

from strategy import kalshi_fair_value as fv


# --------- pure math helpers ---------

def test_norm_cdf_at_zero_is_half():
    assert abs(fv._norm_cdf(Decimal("0")) - Decimal("0.5")) < Decimal("1e-9")


def test_norm_cdf_tails():
    assert fv._norm_cdf(Decimal("5")) > Decimal("0.99999")
    assert fv._norm_cdf(Decimal("-5")) < Decimal("0.00001")


def test_sigma_over_horizon_scales_sqrt_time():
    s = Decimal("0.002")
    # Full window → full σ.
    assert fv.sigma_over_horizon(s, fv.WINDOW_SECONDS) == s
    # Quarter window → σ · 0.5.
    q = fv.sigma_over_horizon(s, fv.WINDOW_SECONDS / Decimal("4"))
    assert abs(q - s * Decimal("0.5")) < Decimal("1e-9")
    # Zero time → zero σ.
    assert fv.sigma_over_horizon(s, Decimal("0")) == Decimal("0")


def test_sigma_over_horizon_saturates_at_full_window():
    s = Decimal("0.005")
    # Asking for T > window returns the full σ.
    assert fv.sigma_over_horizon(s, Decimal("1800")) == s


def test_prob_return_nonneg_sign_behavior():
    sigma = Decimal("0.002")
    # Positive observed → prob > 0.5.
    assert fv.prob_return_nonneg(
        log_return_observed=Decimal("0.001"), sigma_remaining=sigma,
    ) > Decimal("0.5")
    # Negative observed → prob < 0.5.
    assert fv.prob_return_nonneg(
        log_return_observed=Decimal("-0.001"), sigma_remaining=sigma,
    ) < Decimal("0.5")
    # Zero observed → prob = 0.5.
    p0 = fv.prob_return_nonneg(
        log_return_observed=Decimal("0"), sigma_remaining=sigma,
    )
    assert abs(p0 - Decimal("0.5")) < Decimal("1e-9")


def test_prob_return_nonneg_degenerate_zero_sigma():
    # At σ=0 (no time left), pure comparison wins.
    assert fv.prob_return_nonneg(
        log_return_observed=Decimal("0.0001"), sigma_remaining=Decimal("0"),
    ) == Decimal("1")
    assert fv.prob_return_nonneg(
        log_return_observed=Decimal("-0.0001"), sigma_remaining=Decimal("0"),
    ) == Decimal("0")


# --------- FairValueModel.price ---------

def _mk_model(**kw):
    return fv.FairValueModel(
        sigma_15min_by_asset={"btc": Decimal("0.002"), "eth": Decimal("0.003")},
        no_data_haircut=kw.pop("no_data_haircut", Decimal("0")),
        **kw,
    )


def test_price_above_spot_far_above_strike_gives_high_p_yes():
    m = _mk_model()
    p, _ = m.price(
        asset="btc", strike=Decimal("65000"), comparator="at_least",
        reference_price=Decimal("66000"),    # ~154 bps above
        reference_60s_avg=Decimal("66000"),
        time_remaining_s=Decimal("30"),
    )
    assert p > Decimal("0.95")


def test_price_above_spot_far_below_strike_gives_low_p_yes():
    m = _mk_model()
    p, _ = m.price(
        asset="btc", strike=Decimal("66000"), comparator="at_least",
        reference_price=Decimal("65000"),
        reference_60s_avg=Decimal("65000"),
        time_remaining_s=Decimal("30"),
    )
    assert p < Decimal("0.05")


def test_price_equal_strike_and_spot_yields_half():
    m = _mk_model()
    p, _ = m.price(
        asset="btc", strike=Decimal("65000"), comparator="at_least",
        reference_price=Decimal("65000"),
        reference_60s_avg=Decimal("65000"),
        time_remaining_s=Decimal("300"),
    )
    assert abs(p - Decimal("0.5")) < Decimal("0.01")


def test_price_below_is_complement_of_above():
    m = _mk_model()
    spot = Decimal("65500")
    k = Decimal("65000")
    p_above, _ = m.price(
        asset="btc", strike=k, comparator="at_least",
        reference_price=spot, reference_60s_avg=spot,
        time_remaining_s=Decimal("60"),
    )
    p_below, _ = m.price(
        asset="btc", strike=k, comparator="below",
        reference_price=spot, reference_60s_avg=spot,
        time_remaining_s=Decimal("60"),
    )
    assert abs((p_above + p_below) - Decimal("1")) < Decimal("1e-9")


def test_price_at_least_matches_above_and_greater_or_equal():
    m = _mk_model()
    p_al, _ = m.price(
        asset="btc", strike=Decimal("65000"), comparator="at_least",
        reference_price=Decimal("65100"),
        reference_60s_avg=Decimal("65000"),
        time_remaining_s=Decimal("60"),
    )
    p_ge, _ = m.price(
        asset="btc", strike=Decimal("65000"), comparator="greater_or_equal",
        reference_price=Decimal("65100"),
        reference_60s_avg=Decimal("65000"),
        time_remaining_s=Decimal("60"),
    )
    assert p_al == p_ge


def test_price_between_and_exactly_raise_not_implemented():
    m = _mk_model()
    for comp in ("between", "exactly"):
        with pytest.raises(NotImplementedError, match="strike_high"):
            m.price(
                asset="btc", strike=Decimal("65000"), comparator=comp,
                reference_price=Decimal("65000"),
                reference_60s_avg=Decimal("65000"),
                time_remaining_s=Decimal("60"),
            )


def test_price_unsupported_comparator_raises_value_error():
    m = _mk_model()
    with pytest.raises(ValueError, match="unsupported comparator"):
        m.price(
            asset="btc", strike=Decimal("65000"), comparator="bogus",
            reference_price=Decimal("65000"),
            reference_60s_avg=Decimal("65000"),
            time_remaining_s=Decimal("60"),
        )


def test_price_t_zero_is_degenerate():
    m = _mk_model()
    # Any positive spot above strike at T=0 → yes. Below → no.
    p_up, _ = m.price(
        asset="btc", strike=Decimal("65000"), comparator="at_least",
        reference_price=Decimal("65100"),
        reference_60s_avg=Decimal("65100"),
        time_remaining_s=Decimal("0"),
    )
    assert p_up == Decimal("1")
    p_dn, _ = m.price(
        asset="btc", strike=Decimal("65100"), comparator="at_least",
        reference_price=Decimal("65000"),
        reference_60s_avg=Decimal("65000"),
        time_remaining_s=Decimal("0"),
    )
    assert p_dn == Decimal("0")


def test_no_data_haircut_subtracts_exactly():
    m = fv.FairValueModel(
        sigma_15min_by_asset={"btc": Decimal("0.002")},
        no_data_haircut=Decimal("0.01"),
    )
    # spot far above strike → raw p ~ 1; with 1 pp haircut → ~0.99.
    p, _ = m.price(
        asset="btc", strike=Decimal("65000"), comparator="at_least",
        reference_price=Decimal("70000"),
        reference_60s_avg=Decimal("70000"),
        time_remaining_s=Decimal("30"),
    )
    assert p <= Decimal("0.99")
    assert p > Decimal("0.95")


def test_no_data_haircut_clamps_at_zero():
    m = fv.FairValueModel(
        sigma_15min_by_asset={"btc": Decimal("0.002")},
        no_data_haircut=Decimal("0.5"),   # larger than raw p
    )
    p, _ = m.price(
        asset="btc", strike=Decimal("70000"), comparator="at_least",
        reference_price=Decimal("60000"),
        reference_60s_avg=Decimal("60000"),
        time_remaining_s=Decimal("30"),
    )
    assert p >= Decimal("0")


def test_ci_width_near_zero_when_p_confident():
    m = _mk_model()
    _, ci = m.price(
        asset="btc", strike=Decimal("65000"), comparator="at_least",
        reference_price=Decimal("70000"),   # way above → p ~ 1
        reference_60s_avg=Decimal("70000"),
        time_remaining_s=Decimal("30"),
    )
    assert ci < Decimal("0.01")


def test_ci_width_near_one_at_coin_flip():
    m = _mk_model()
    _, ci = m.price(
        asset="btc", strike=Decimal("65000"), comparator="at_least",
        reference_price=Decimal("65000"),
        reference_60s_avg=Decimal("65000"),
        time_remaining_s=Decimal("600"),   # long time, spot = strike
    )
    assert ci > Decimal("0.95")


def test_unknown_asset_falls_back_to_default():
    # No explicit override; falls back to DEFAULT_SIGMA_15MIN['btc'].
    m = fv.FairValueModel(no_data_haircut=Decimal("0"))
    p, _ = m.price(
        asset="doge", strike=Decimal("65000"), comparator="at_least",
        reference_price=Decimal("65100"),
        reference_60s_avg=Decimal("65100"),
        time_remaining_s=Decimal("60"),
    )
    assert Decimal("0") < p <= Decimal("1")


def test_calibrate_from_returns_updates_sigma():
    m = fv.FairValueModel()
    sigma = m.calibrate_from_returns(asset="btc", returns=[
        Decimal("0.001"), Decimal("-0.002"), Decimal("0.0015"),
        Decimal("-0.0005"), Decimal("0"),
    ])
    assert sigma > 0
    # Subsequent price() picks up the new σ.
    assert m._sigma("btc") == sigma


def test_calibrate_from_empty_returns_is_noop():
    m = fv.FairValueModel(sigma_15min_by_asset={"btc": Decimal("0.002")})
    sigma = m.calibrate_from_returns(asset="btc", returns=[])
    # Nothing to fit → keeps existing value.
    assert sigma == Decimal("0.002")
