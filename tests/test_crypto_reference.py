"""Cover `src/market/crypto_reference.py` — T13 acceptance."""

from __future__ import annotations

import sqlite3
from decimal import Decimal

import pytest

from market import crypto_reference as cr


# --------- reject_outliers ---------

def test_reject_outliers_drops_far_values():
    vals = [Decimal("100"), Decimal("101"), Decimal("99"), Decimal("500")]
    kept = cr.reject_outliers(vals, max_dev_pct=Decimal("0.05"))
    # median = 100.5, 5% band = ~5.025 → 500 is way outside; 99/100/101 kept.
    assert Decimal("500") not in kept
    assert Decimal("99") in kept
    assert Decimal("101") in kept


def test_reject_outliers_short_list_passes_through():
    # Too few points to establish a median → keep everything.
    vals = [Decimal("100"), Decimal("9999")]
    assert cr.reject_outliers(vals) == vals


def test_reject_outliers_zero_median_short_circuits():
    vals = [Decimal("0"), Decimal("0"), Decimal("5")]
    # Median is 0 so we can't compute % deviation → keep everything.
    assert cr.reject_outliers(vals) == vals


# --------- aggregate_basket ---------

def test_aggregate_basket_returns_median_of_accepted():
    prices = {
        "coinbase": Decimal("65000"),
        "kraken": Decimal("65002"),
        "bitstamp": Decimal("64998"),
        "bogus": Decimal("100000"),  # dropped by outlier rejection
    }
    agg = cr.aggregate_basket(prices)
    assert agg is not None
    assert agg == Decimal("65000")


def test_aggregate_basket_needs_at_least_two_after_rejection():
    prices = {"only_one": Decimal("65000")}
    assert cr.aggregate_basket(prices) is None


def test_aggregate_basket_empty_returns_none():
    assert cr.aggregate_basket({}) is None


# --------- rolling_average ---------

def _tick(asset: str, ts_us: int, price: str, src: str) -> cr.ReferenceTick:
    return cr.ReferenceTick(asset=asset, ts_us=ts_us, price=Decimal(price), src=src)


def test_rolling_average_window_boundary_inclusive_end_exclusive_start():
    now_us = 60_000_000  # 60 s
    ticks = [
        _tick("btc", 1, "100", "a"),         # just inside
        _tick("btc", now_us, "200", "b"),     # inclusive end
        _tick("btc", now_us + 1, "999", "c"),  # after end — excluded
        _tick("btc", 0, "99", "d"),           # exactly start — excluded (exclusive)
    ]
    avg = cr.rolling_average(ticks, window_end_us=now_us, window_seconds=60)
    assert avg == Decimal("150")  # (100 + 200) / 2


def test_rolling_average_empty_window_returns_none():
    assert cr.rolling_average([], window_end_us=0) is None


def test_rolling_average_60s_window_default():
    now_us = 5_000_000_000  # arbitrary
    inside = _tick("btc", now_us - 30_000_000, "100", "a")
    outside = _tick("btc", now_us - 61_000_000, "1000", "b")
    avg = cr.rolling_average([inside, outside], window_end_us=now_us)
    assert avg == Decimal("100")


# --------- BasketReferenceSource ---------

class _FakeClock:
    def __init__(self, start: int = 2_000_000_000):
        self.t_us = start

    def __call__(self) -> int:
        return self.t_us

    def advance(self, seconds: float) -> None:
        self.t_us += int(seconds * 1_000_000)


def test_basket_source_aggregates_across_constituents():
    clock = _FakeClock()
    src = cr.BasketReferenceSource(assets=("btc",), now_us=clock)
    src.start()
    src.record_tick(_tick("btc", clock(), "65000", "coinbase"))
    src.record_tick(_tick("btc", clock(), "65002", "kraken"))
    src.record_tick(_tick("btc", clock(), "64998", "bitstamp"))
    assert src.get_spot("btc") == Decimal("65000")


def test_basket_source_get_spot_falls_back_to_single_venue():
    """Regression: `aggregate_basket` requires ≥2 venues; when only one
    constituent has reported, `get_spot` must return that single venue's
    latest price rather than None (the previous behavior broke the live
    shadow runner where Coinbase is the only reference venue wired)."""
    src = cr.BasketReferenceSource(assets=("btc",))
    src.start()
    src.record_tick(_tick("btc", 1, "65000", "coinbase"))
    # With only one constituent, aggregate_basket returns None.
    agg = cr.aggregate_basket({"coinbase": Decimal("65000")})
    assert agg is None
    # But get_spot must fall back to the single-venue latest price.
    assert src.get_spot("btc") == Decimal("65000")


def test_basket_source_drops_unsupported_asset_silently():
    src = cr.BasketReferenceSource(assets=("btc",))
    src.record_tick(_tick("doge", 1, "0.1", "coinbase"))
    # Unsupported asset → no-op (tests shouldn't need to catch anything).
    assert src.get_spot("doge") is None


def test_basket_source_is_healthy_only_after_start_and_fresh_ticks():
    clock = _FakeClock()
    src = cr.BasketReferenceSource(assets=("btc",), stale_seconds=2.0, now_us=clock)
    assert src.is_healthy() is False  # not started
    src.start()
    assert src.is_healthy() is False  # no ticks yet
    src.record_tick(_tick("btc", clock(), "65000", "coinbase"))
    src.record_tick(_tick("btc", clock(), "65001", "kraken"))
    assert src.is_healthy() is True
    clock.advance(3.0)
    assert src.is_healthy() is False  # last tick older than stale_seconds


def test_basket_source_60s_avg_uses_window():
    clock = _FakeClock()
    src = cr.BasketReferenceSource(assets=("btc",), now_us=clock)
    src.start()
    # Drop ticks across 90s — only ticks within last 60s should count.
    src.record_tick(_tick("btc", clock() - 80_000_000, "60000", "coinbase"))
    src.record_tick(_tick("btc", clock() - 30_000_000, "65000", "coinbase"))
    src.record_tick(_tick("btc", clock(), "65200", "coinbase"))
    avg = src.get_60s_avg("btc")
    assert avg == Decimal("65100")  # (65000 + 65200) / 2


def test_basket_source_60s_avg_no_ticks_returns_none():
    src = cr.BasketReferenceSource(assets=("btc",))
    src.start()
    assert src.get_60s_avg("btc") is None


def test_basket_source_snapshot_state_returns_latest_per_src():
    src = cr.BasketReferenceSource(assets=("btc",))
    src.record_tick(_tick("btc", 1, "60000", "coinbase"))
    src.record_tick(_tick("btc", 2, "60100", "coinbase"))  # same src → overwrites
    src.record_tick(_tick("btc", 3, "60050", "kraken"))
    state = src.snapshot_state("btc")
    assert set(state.keys()) == {"coinbase", "kraken"}
    assert state["coinbase"].price == Decimal("60100")


def test_basket_source_get_last_tick_us_returns_max_across_sources():
    src = cr.BasketReferenceSource(assets=("btc",))
    # Before any tick → None.
    assert src.get_last_tick_us("btc") is None
    src.record_tick(_tick("btc", 1_000_000, "60000", "coinbase"))
    assert src.get_last_tick_us("btc") == 1_000_000
    # A later tick from a different source wins.
    src.record_tick(_tick("btc", 5_000_000, "60100", "kraken"))
    assert src.get_last_tick_us("btc") == 5_000_000
    # An earlier tick from coinbase does not rewind the max.
    src.record_tick(_tick("btc", 3_000_000, "60050", "coinbase"))
    assert src.get_last_tick_us("btc") == 5_000_000


def test_basket_source_get_last_tick_us_unknown_asset_returns_none():
    src = cr.BasketReferenceSource(assets=("btc",))
    src.record_tick(_tick("btc", 1, "60000", "coinbase"))
    assert src.get_last_tick_us("eth") is None


def test_licensed_source_get_last_tick_us_is_none():
    src = cr.LicensedCFBenchmarksSource(api_key="")
    assert src.get_last_tick_us("btc") is None


# --------- LicensedCFBenchmarksSource ---------

def test_licensed_source_unlicensed_is_no_op():
    src = cr.LicensedCFBenchmarksSource(api_key="")
    src.start()
    src.stop()
    assert src.is_licensed is False
    assert src.is_healthy() is False
    assert src.get_spot("btc") is None
    assert src.get_60s_avg("btc") is None


def test_licensed_source_with_key_flags_licensed_but_still_stub():
    src = cr.LicensedCFBenchmarksSource(api_key="cf-key-xyz")
    assert src.is_licensed is True
    # Real implementation deferred to P2; read surface still returns None.
    assert src.get_spot("btc") is None


# --------- insert_tick ---------

def test_insert_tick_writes_row(tmp_path):
    import scripts.migrate_db as _m  # ensure migrate_db importable
    import migrate_db as m
    url = f"sqlite:///{tmp_path}/ref.db"
    m.migrate(url)
    db_path = url.removeprefix("sqlite:///")
    conn = sqlite3.connect(db_path)
    try:
        cr.insert_tick(conn, _tick("btc", 123_456, "65000.50", "coinbase"))
        conn.commit()
        row = conn.execute(
            "SELECT asset, ts_us, price, src FROM reference_ticks"
        ).fetchone()
        assert row == ("btc", 123_456, "65000.50", "coinbase")
    finally:
        conn.close()
