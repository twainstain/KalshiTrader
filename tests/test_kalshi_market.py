"""Cover `src/market/kalshi_market.py`.

≥15 assertions per P1-M1-T08 acceptance criterion.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from market import kalshi_market as km
from core.models import MarketQuote
from platform_adapters import KalshiAPIError


# --------- parse_dollar_string ---------

def test_parse_dollar_string_from_str():
    assert km.parse_dollar_string("0.4200") == Decimal("0.4200")


def test_parse_dollar_string_from_float_uses_str_path():
    # str(0.1) == "0.1" → avoids IEEE-754 noise.
    assert km.parse_dollar_string(0.1) == Decimal("0.1")


def test_parse_dollar_string_returns_decimal_unchanged():
    d = Decimal("0.42")
    assert km.parse_dollar_string(d) is d


# --------- book_depth_usd ---------

def test_book_depth_usd_empty_side():
    assert km.book_depth_usd([]) == Decimal("0")


def test_book_depth_usd_sums_top_n_levels():
    # Ascending Kalshi book — last entry is best bid.
    side = [
        ["0.10", "5"],   # worst
        ["0.20", "10"],
        ["0.30", "20"],  # best
    ]
    # levels=2 → take last two: 0.20*10 + 0.30*20 = 2.0 + 6.0 = 8.0
    assert km.book_depth_usd(side, levels=2) == Decimal("8.0")
    # levels=10 (more than side length) → sum everything = 0.5 + 2.0 + 6.0 = 8.5
    assert km.book_depth_usd(side, levels=10) == Decimal("8.5")


# --------- book_to_market_quote (T05) ---------

def _quote_from_book(**overrides) -> MarketQuote:
    book = overrides.pop("book", {
        "yes": [["0.01", "10"], ["0.40", "50"], ["0.41", "100"]],
        "no":  [["0.50", "20"], ["0.58", "80"], ["0.59", "25"]],
    })
    defaults = dict(
        book=book,
        market_ticker="KXBTC15M-T-65000",
        series_ticker="KXBTC15M",
        event_ticker="KXBTC15M-E",
        strike="65000",
        comparator="above",
        expiration_ts=1_746_000_000,
        time_remaining_s=45,
        reference_price="64999.5",
        reference_60s_avg="64995.1",
        fee_bps="35",
        quote_timestamp_us=1_746_000_000_000_000,
    )
    defaults.update(overrides)
    return km.book_to_market_quote(**defaults)


def test_book_to_quote_derives_asks_from_opposite_bids():
    q = _quote_from_book()
    # best YES bid = 0.41 (last yes entry)
    # best NO bid = 0.59 (last no entry)
    assert q.best_yes_bid == Decimal("0.41")
    assert q.best_no_bid == Decimal("0.59")
    assert q.best_yes_ask == Decimal("1") - Decimal("0.59")  # 0.41
    assert q.best_no_ask == Decimal("1") - Decimal("0.41")   # 0.59


def test_book_to_quote_computes_depth_both_sides():
    q = _quote_from_book()
    # yes side top-5 (only 3 levels present): 0.01*10 + 0.40*50 + 0.41*100 = 0.1 + 20 + 41 = 61.1
    assert q.book_depth_yes_usd == Decimal("61.1")
    # no side top-5 (only 3 levels): 0.50*20 + 0.58*80 + 0.59*25 = 10 + 46.4 + 14.75 = 71.15
    assert q.book_depth_yes_usd > 0
    assert q.book_depth_no_usd == Decimal("71.15")


def test_book_to_quote_fee_included_always_false():
    q = _quote_from_book()
    assert q.fee_included is False  # ground rule §1


def test_book_to_quote_empty_yes_side_maps_yes_ask_to_one():
    q = _quote_from_book(book={"yes": [], "no": [["0.60", "10"]]})
    # Empty YES side → derived NO ask = 1 (un-fillable); YES ask from NO bid.
    assert q.best_yes_bid == Decimal("0")
    assert q.best_no_ask == Decimal("1")
    assert q.best_yes_ask == Decimal("1") - Decimal("0.60")


def test_book_to_quote_preserves_raw_book():
    q = _quote_from_book()
    assert "book" in q.raw
    assert q.raw["book"]["yes"][-1] == ["0.41", "100"]


def test_book_to_quote_propagates_warning_flags():
    q = _quote_from_book(warning_flags=("stale_book",))
    assert q.warning_flags == ("stale_book",)


# --------- lifecycle_tag (T06) ---------

def test_lifecycle_tag_initialized_is_opening():
    assert km.lifecycle_tag("initialized", time_remaining_s=899) == "opening"


def test_lifecycle_tag_active_above_60_is_active():
    assert km.lifecycle_tag("active", time_remaining_s=120) == "active"


def test_lifecycle_tag_active_at_or_below_60_is_final_minute():
    assert km.lifecycle_tag("active", time_remaining_s=60) == "final_minute"
    assert km.lifecycle_tag("active", time_remaining_s=5) == "final_minute"


def test_lifecycle_tag_active_negative_remaining_is_closed():
    assert km.lifecycle_tag("active", time_remaining_s=-1) == "closed"


def test_lifecycle_tag_closed_and_inactive():
    assert km.lifecycle_tag("closed", time_remaining_s=0) == "closed"
    assert km.lifecycle_tag("inactive", time_remaining_s=0) == "closed"


def test_lifecycle_tag_settled_statuses():
    for s in ("determined", "disputed", "amended", "finalized", "settled"):
        assert km.lifecycle_tag(s, time_remaining_s=0) == "settled"


def test_lifecycle_tag_unknown_status_defaults_active():
    # Fail-open so the scanner never silently drops a market.
    assert km.lifecycle_tag("bogus_state", time_remaining_s=100) == "active"


# --------- make_client (T02) ---------

def test_make_client_rejects_unknown_env():
    with pytest.raises(ValueError, match="KALSHI_ENV"):
        km.make_client(env="staging", api_key_id="x", private_key_path="/tmp/x.pem")


def test_make_client_requires_api_key_id(monkeypatch):
    monkeypatch.delenv("KALSHI_API_KEY_ID", raising=False)
    with pytest.raises(RuntimeError, match="KALSHI_API_KEY_ID"):
        km.make_client(env="demo", private_key_path="/tmp/x.pem")


def test_make_client_requires_existing_pem(tmp_path, monkeypatch):
    monkeypatch.setenv("KALSHI_API_KEY_ID", "key-id-x")
    with pytest.raises(RuntimeError, match="KALSHI_PRIVATE_KEY_PATH"):
        km.make_client(env="demo", private_key_path=str(tmp_path / "missing.pem"))


def test_make_client_uses_factory_when_provided(tmp_path, monkeypatch):
    pem = tmp_path / "kalshi.pem"
    pem.write_bytes(b"-----BEGIN FAKE-----\n")
    monkeypatch.setenv("KALSHI_API_KEY_ID", "key-id-x")

    captured: dict = {}

    def fake_factory(*, host: str, api_key_id: str, private_key_pem: bytes):
        captured.update(host=host, api_key_id=api_key_id, pem=private_key_pem)
        return MagicMock(name="FakeClient")

    client = km.make_client(env="demo", private_key_path=str(pem),
                            client_factory=fake_factory)
    assert captured["host"] == km.REST_HOSTS["demo"]
    assert captured["api_key_id"] == "key-id-x"
    assert captured["pem"] == b"-----BEGIN FAKE-----\n"
    assert client is not None


# --------- discover_active_crypto_markets (T03) ---------

def test_discover_passes_crypto_filter_and_collects_markets():
    client = MagicMock()
    client.get_series.return_value = {
        "series": [
            {"ticker": "KXBTC15M"},
            {"ticker": "KXETH15M"},
            {"ticker": "KXDAILY"},  # surprise
        ]
    }
    client.get_markets.side_effect = [
        {"markets": [{"ticker": "KXBTC15M-T1"}]},
        {"markets": [{"ticker": "KXETH15M-T1"}, {"ticker": "KXETH15M-T2"}]},
    ]
    out = km.discover_active_crypto_markets(
        client, expected_series=("KXBTC15M", "KXETH15M"),
    )
    tickers = [m["ticker"] for m in out]
    assert "KXBTC15M-T1" in tickers
    assert "KXETH15M-T2" in tickers
    # Crypto-only filter: daily series was NOT queried for markets.
    assert client.get_markets.call_count == 2


def test_discover_raises_kalshi_error_on_sdk_failure():
    client = MagicMock()
    client.get_series.side_effect = RuntimeError("boom")
    # Make the fallback variant (list_series) also unavailable so _call raises.
    del client.list_series
    with pytest.raises(KalshiAPIError):
        km.discover_active_crypto_markets(client)


# --------- KalshiMarketSource ---------

def _mk_source(**overrides):
    cfg = km.KalshiMarketConfig(**overrides)
    source = km.KalshiMarketSource(cfg, now_us=_FakeClock())
    return source


class _FakeClock:
    def __init__(self, start_us: int = 1_000_000_000_000):
        self.t_us = start_us

    def __call__(self) -> int:
        return self.t_us

    def advance(self, seconds: float) -> None:
        self.t_us += int(seconds * 1_000_000)


def test_source_start_stop_lifecycle():
    src = _mk_source()
    assert src.is_healthy() is False  # no books yet
    src.start()
    # Thread is running but no books have arrived; still unhealthy.
    assert src.is_healthy() is False
    src.stop()


def test_source_apply_snapshot_and_get_quotes():
    clock = _FakeClock()
    src = km.KalshiMarketSource(km.KalshiMarketConfig(), now_us=clock)
    src.apply_snapshot("KXBTC15M-T", {
        "yes": [["0.40", "10"], ["0.41", "5"]],
        "no":  [["0.58", "8"], ["0.59", "4"]],
    })
    src.update_lifecycle("KXBTC15M-T", status="active", time_remaining_s=45)

    quotes = src.get_quotes(
        reference_price_by_asset={"btc": Decimal("64999.50")},
        reference_60s_avg_by_asset={"btc": Decimal("64995.10")},
        fee_bps_by_ticker={"KXBTC15M-T": Decimal("35")},
        market_meta_by_ticker={
            "KXBTC15M-T": {
                "series_ticker": "KXBTC15M",
                "event_ticker": "KXBTC15M-E",
                "strike": "65000",
                "comparator": "above",
                "expiration_ts": 1_746_000_000,
                "asset": "btc",
            },
        },
    )
    assert len(quotes) == 1
    q = quotes[0]
    assert q.market_ticker == "KXBTC15M-T"
    assert q.best_yes_bid == Decimal("0.41")
    assert q.reference_price == Decimal("64999.50")


def test_source_marks_stale_book_via_warning_flag():
    clock = _FakeClock()
    src = km.KalshiMarketSource(km.KalshiMarketConfig(stale_book_seconds=1.0),
                                now_us=clock)
    src.apply_snapshot("KXBTC15M-T", {"yes": [["0.40", "5"]], "no": [["0.60", "5"]]})
    src.update_lifecycle("KXBTC15M-T", status="active", time_remaining_s=45)
    clock.advance(3.0)  # > stale_book_seconds

    quotes = src.get_quotes(
        reference_price_by_asset={"btc": Decimal("1")},
        reference_60s_avg_by_asset={"btc": Decimal("1")},
        fee_bps_by_ticker={"KXBTC15M-T": Decimal("0")},
        market_meta_by_ticker={
            "KXBTC15M-T": {
                "series_ticker": "KXBTC15M", "event_ticker": "E",
                "strike": "1", "comparator": "above",
                "expiration_ts": 0, "asset": "btc",
            },
        },
    )
    assert quotes[0].warning_flags == ("stale_book",)


def test_source_skips_tickers_without_meta():
    src = km.KalshiMarketSource()
    src.apply_snapshot("KXBTC15M-unknown", {"yes": [], "no": []})
    quotes = src.get_quotes(
        reference_price_by_asset={},
        reference_60s_avg_by_asset={},
        fee_bps_by_ticker={},
        market_meta_by_ticker={},  # no entry for our ticker
    )
    assert quotes == []


def test_source_apply_delta_updates_level_and_removes_on_zero_qty():
    src = km.KalshiMarketSource()
    src.apply_snapshot("T", {"yes": [["0.40", "10"]], "no": [["0.60", "5"]]})
    src.apply_delta("T", "yes", "0.41", "7")
    # After adding: yes side has [0.40, 0.41] sorted ascending.
    with src._books_lock:
        book = src._books["T"].book
    assert [l[0] for l in book["yes"]] == ["0.40", "0.41"]

    src.apply_delta("T", "yes", "0.40", "0")  # remove
    with src._books_lock:
        book = src._books["T"].book
    assert [l[0] for l in book["yes"]] == ["0.41"]


def test_source_apply_delta_rejects_bad_side():
    src = km.KalshiMarketSource()
    with pytest.raises(ValueError, match="yes|no"):
        src.apply_delta("T", "maybe", "0.40", "5")


def test_source_breaker_trips_on_api_errors():
    src = km.KalshiMarketSource(
        km.KalshiMarketConfig(),
        breaker=km.CircuitBreaker(
            km.CircuitBreakerConfig(max_api_errors=2, api_error_window_seconds=60.0)
        ),
    )
    src._breaker.record_api_error()
    src._breaker.record_api_error()
    allowed, _ = src._breaker.allows_execution()
    assert allowed is False
