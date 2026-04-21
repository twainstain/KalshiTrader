"""Cover `src/strategy/pure_lag.py`."""

from __future__ import annotations

from decimal import Decimal

from core.models import MarketQuote
from strategy.pure_lag import PureLagStrategy, PureLagConfig, _AssetRollingPrice


# ----------------------------------------------------------------------
# _AssetRollingPrice
# ----------------------------------------------------------------------

def test_rolling_price_mean_over_window():
    rp = _AssetRollingPrice(window_us=10_000_000)  # 10 s
    rp.record(1_000_000, Decimal("100"))
    rp.record(2_000_000, Decimal("110"))
    rp.record(3_000_000, Decimal("120"))
    assert rp.rolling_mean(3_000_000) == Decimal("110")


def test_rolling_price_evicts_old_ticks():
    rp = _AssetRollingPrice(window_us=5_000_000)  # 5 s
    rp.record(1_000_000, Decimal("100"))
    rp.record(10_000_000, Decimal("200"))  # 9s after → evicts first
    assert rp.latest() == Decimal("200")
    assert rp.rolling_mean(10_000_000) == Decimal("200")


def test_rolling_price_empty_returns_none():
    rp = _AssetRollingPrice(window_us=10_000_000)
    assert rp.rolling_mean(0) is None
    assert rp.latest() is None


# ----------------------------------------------------------------------
# PureLagStrategy
# ----------------------------------------------------------------------

def _quote(**overrides):
    defaults = dict(
        venue="kalshi", market_ticker="KXBTC15M-X", series_ticker="KXBTC15M",
        event_ticker="KXBTC15M-EV", best_yes_ask=Decimal("0.40"),
        best_no_ask=Decimal("0.55"), best_yes_bid=Decimal("0.39"),
        best_no_bid=Decimal("0.54"), book_depth_yes_usd=Decimal("1000"),
        book_depth_no_usd=Decimal("1000"), fee_bps=Decimal("35"),
        expiration_ts=Decimal("1800"), strike=Decimal("65000"),
        comparator="above", reference_price=Decimal("65100"),
        reference_60s_avg=Decimal("65050"), time_remaining_s=Decimal("30"),
        quote_timestamp_us=0,
    )
    defaults.update(overrides)
    return MarketQuote(**defaults)


def _now_factory(ts):
    """Return a callable that returns `ts` each time (mutable via nonlocal)."""
    state = {"t": ts}
    return lambda: state["t"], state


def test_evaluate_returns_none_without_reference_ticks():
    s = PureLagStrategy(PureLagConfig())
    assert s.evaluate(_quote(), asset="btc") is None


def test_evaluate_returns_none_when_time_window_excluded():
    s = PureLagStrategy(PureLagConfig())
    s.record_reference_tick("btc", Decimal("65000"))
    # time_remaining_s below lo=120
    q = _quote(time_remaining_s=Decimal("60"))
    assert s.evaluate(q, asset="btc") is None


def test_evaluate_returns_none_when_book_too_thin():
    s = PureLagStrategy(PureLagConfig())
    s.record_reference_tick("btc", Decimal("65000"))
    q = _quote(book_depth_yes_usd=Decimal("0"), book_depth_no_usd=Decimal("0"))
    assert s.evaluate(q, asset="btc") is None


def test_evaluate_returns_none_when_move_below_threshold():
    now_fn, state = _now_factory(10_000_000)
    s = PureLagStrategy(PureLagConfig(move_threshold_bps=Decimal("5")),
                       now_us=now_fn)
    # Feed flat price history — no move.
    for i in range(10):
        state["t"] = i * 1_000_000
        s.record_reference_tick("btc", Decimal("65000"))
    state["t"] = 10_000_000
    assert s.evaluate(_quote(), asset="btc") is None


def test_evaluate_yes_on_upward_move():
    now_fn, state = _now_factory(0)
    s = PureLagStrategy(
        PureLagConfig(
            move_threshold_bps=Decimal("5"),
            min_edge_bps_after_fees=Decimal("100"),  # 100 bps = 1c
        ),
        now_us=now_fn,
    )
    # Old price at t=0, new price ~20 bps higher at t=1s
    state["t"] = 0
    s.record_reference_tick("btc", Decimal("65000"))
    state["t"] = 1_000_000
    s.record_reference_tick("btc", Decimal("65130"))  # +20 bps
    opp = s.evaluate(_quote(best_yes_ask=Decimal("0.40"), fee_bps=Decimal("35")),
                     asset="btc")
    assert opp is not None
    assert opp.recommended_side == "yes"
    assert opp.hypothetical_fill_price == Decimal("0.40")


def test_evaluate_no_on_downward_move():
    now_fn, state = _now_factory(0)
    s = PureLagStrategy(
        PureLagConfig(
            move_threshold_bps=Decimal("5"),
            min_edge_bps_after_fees=Decimal("100"),
        ),
        now_us=now_fn,
    )
    state["t"] = 0
    s.record_reference_tick("btc", Decimal("65000"))
    state["t"] = 1_000_000
    s.record_reference_tick("btc", Decimal("64870"))  # -20 bps
    opp = s.evaluate(_quote(best_no_ask=Decimal("0.40"), fee_bps=Decimal("35")),
                     asset="btc")
    assert opp is not None
    assert opp.recommended_side == "no"
    assert opp.hypothetical_fill_price == Decimal("0.40")


def test_evaluate_returns_none_when_edge_below_threshold():
    now_fn, state = _now_factory(0)
    s = PureLagStrategy(
        PureLagConfig(
            move_threshold_bps=Decimal("5"),
            # Ask=0.95 → (1-0.95) - 0.0035 = 465 bps... set min higher.
            min_edge_bps_after_fees=Decimal("10000"),
        ),
        now_us=now_fn,
    )
    state["t"] = 0
    s.record_reference_tick("btc", Decimal("65000"))
    state["t"] = 1_000_000
    s.record_reference_tick("btc", Decimal("65130"))
    q = _quote(best_yes_ask=Decimal("0.95"))
    assert s.evaluate(q, asset="btc") is None


def test_evaluate_asset_case_insensitive():
    now_fn, state = _now_factory(0)
    s = PureLagStrategy(PureLagConfig(), now_us=now_fn)
    state["t"] = 0
    s.record_reference_tick("BTC", Decimal("65000"))
    state["t"] = 1_000_000
    s.record_reference_tick("btc", Decimal("65130"))
    opp = s.evaluate(_quote(), asset="BTC")
    assert opp is not None


def test_evaluate_many_skips_unmapped_tickers():
    now_fn, state = _now_factory(0)
    s = PureLagStrategy(PureLagConfig(), now_us=now_fn)
    state["t"] = 0
    s.record_reference_tick("btc", Decimal("65000"))
    state["t"] = 1_000_000
    s.record_reference_tick("btc", Decimal("65130"))
    q_btc = _quote(market_ticker="KXBTC15M-X")
    q_unk = _quote(market_ticker="KXFOO15M-X")
    opps = s.evaluate_many([q_btc, q_unk],
                           asset_by_ticker={"KXBTC15M-X": "btc"})
    assert len(opps) == 1
    assert opps[0].quote.market_ticker == "KXBTC15M-X"


def test_config_defaults_calibrated_2026_04_21():
    """Live-run calibrated defaults — see docstring in PureLagConfig.

    time_window tightened to (5, 60) on 2026-04-21 to match the RiskEngine's
    TimeWindowRule — decisions outside that window were always risk-rejected
    before becoming paper fills, making them pure noise in shadow_decisions.
    """
    cfg = PureLagConfig()
    assert cfg.move_threshold_bps == Decimal("3")
    assert cfg.rolling_window_us == 5_000_000
    assert cfg.min_edge_bps_after_fees == Decimal("100")
    assert cfg.time_window_seconds == (5, 60)
    assert cfg.min_fill_price == Decimal("0.10")


def test_evaluate_rejects_fill_price_below_min():
    """Regression: lottery-ticket yes_ask below min_fill_price is rejected
    even if move_threshold and edge would otherwise trigger."""
    now_fn, state = _now_factory(0)
    s = PureLagStrategy(
        PureLagConfig(
            move_threshold_bps=Decimal("3"),
            rolling_window_us=5_000_000,
            min_edge_bps_after_fees=Decimal("100"),
            min_fill_price=Decimal("0.10"),
            time_window_seconds=(30, 900),
        ),
        now_us=now_fn,
    )
    state["t"] = 0
    s.record_reference_tick("btc", Decimal("65000"))
    state["t"] = 1_000_000
    s.record_reference_tick("btc", Decimal("65130"))   # +20 bps up → yes
    q = _quote(best_yes_ask=Decimal("0.03"), fee_bps=Decimal("35"))
    # 0.03 < 0.10 → reject.
    assert s.evaluate(q, asset="btc") is None


def test_evaluate_accepts_fill_price_at_or_above_min():
    now_fn, state = _now_factory(0)
    s = PureLagStrategy(
        PureLagConfig(
            move_threshold_bps=Decimal("3"),
            rolling_window_us=5_000_000,
            min_edge_bps_after_fees=Decimal("100"),
            min_fill_price=Decimal("0.10"),
            time_window_seconds=(30, 900),
        ),
        now_us=now_fn,
    )
    state["t"] = 0
    s.record_reference_tick("btc", Decimal("65000"))
    state["t"] = 1_000_000
    s.record_reference_tick("btc", Decimal("65130"))
    q = _quote(best_yes_ask=Decimal("0.15"), fee_bps=Decimal("35"))
    opp = s.evaluate(q, asset="btc")
    assert opp is not None
    assert opp.hypothetical_fill_price == Decimal("0.15")


def test_evaluate_accepts_t_inside_tightened_window():
    """With time_window=(5, 60), T=45s lands inside the final-minute window
    where the risk engine also accepts."""
    now_fn, state = _now_factory(0)
    s = PureLagStrategy(PureLagConfig(), now_us=now_fn)
    state["t"] = 0
    s.record_reference_tick("btc", Decimal("65000"))
    state["t"] = 1_000_000
    s.record_reference_tick("btc", Decimal("65130"))
    q = _quote(best_yes_ask=Decimal("0.30"), time_remaining_s=Decimal("45"))
    assert s.evaluate(q, asset="btc") is not None


def test_evaluate_rejects_t_above_max():
    """With time_window=(5, 60), anything t >= 60 is dropped before the
    risk engine would reject it anyway."""
    now_fn, state = _now_factory(0)
    s = PureLagStrategy(PureLagConfig(), now_us=now_fn)
    state["t"] = 0
    s.record_reference_tick("btc", Decimal("65000"))
    state["t"] = 1_000_000
    s.record_reference_tick("btc", Decimal("65130"))
    q = _quote(time_remaining_s=Decimal("60"))       # at the exclusive cap
    assert s.evaluate(q, asset="btc") is None
    q2 = _quote(time_remaining_s=Decimal("120"))     # well outside
    assert s.evaluate(q2, asset="btc") is None


def test_evaluate_rejects_t_below_min():
    """T < 5 s is too close to expiry — cancel-on-timeout can't land."""
    now_fn, state = _now_factory(0)
    s = PureLagStrategy(PureLagConfig(), now_us=now_fn)
    state["t"] = 0
    s.record_reference_tick("btc", Decimal("65000"))
    state["t"] = 1_000_000
    s.record_reference_tick("btc", Decimal("65130"))
    q = _quote(time_remaining_s=Decimal("3"))
    assert s.evaluate(q, asset="btc") is None


def test_rolling_window_is_5s_default():
    """Ticks older than 5 s should be evicted by the new default."""
    from strategy.pure_lag import _AssetRollingPrice
    rp = _AssetRollingPrice(window_us=PureLagConfig().rolling_window_us)
    rp.record(0, Decimal("100"))
    rp.record(6_000_000, Decimal("200"))  # 6 s later → evicts first
    assert rp.latest() == Decimal("200")
    assert rp.rolling_mean(6_000_000) == Decimal("200")


def test_record_reference_tick_creates_per_asset_state():
    s = PureLagStrategy(PureLagConfig())
    s.record_reference_tick("btc", Decimal("65000"))
    s.record_reference_tick("eth", Decimal("2500"))
    assert "btc" in s._per_asset
    assert "eth" in s._per_asset
    assert s._per_asset["btc"].latest() == Decimal("65000")
    assert s._per_asset["eth"].latest() == Decimal("2500")
