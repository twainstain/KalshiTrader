"""Cover `src/market/basket_ws.py`."""

from __future__ import annotations

from decimal import Decimal

from market.basket_ws import BasketWSReference, make_basket_fetcher


class _FakeWS:
    """Minimal WSLike stub for BasketWSReference tests."""
    def __init__(self, prices: dict[str, Decimal], age_us: int = 1000) -> None:
        self._prices = prices
        self._age = age_us

    def get_price(self, asset: str):
        return self._prices.get(asset.lower())

    def get_age_us(self, asset: str) -> int:
        return self._age


def test_single_venue_returns_price():
    basket = BasketWSReference({"coinbase": _FakeWS({"btc": Decimal("65000")})})
    assert basket.get_price("btc") == Decimal("65000")


def test_two_venues_returns_median():
    basket = BasketWSReference({
        "coinbase": _FakeWS({"btc": Decimal("65000")}),
        "kraken":   _FakeWS({"btc": Decimal("65100")}),
    })
    # Median of two values is their average.
    assert basket.get_price("btc") == Decimal("65050")


def test_three_venues_returns_true_median():
    basket = BasketWSReference({
        "a": _FakeWS({"btc": Decimal("65000")}),
        "b": _FakeWS({"btc": Decimal("65100")}),
        "c": _FakeWS({"btc": Decimal("70000")}),  # outlier
    })
    # Median robust to outlier.
    assert basket.get_price("btc") == Decimal("65100")


def test_stale_venue_excluded():
    basket = BasketWSReference(
        {
            "coinbase": _FakeWS({"btc": Decimal("65000")}, age_us=1000),
            "kraken":   _FakeWS({"btc": Decimal("99999")}, age_us=999_999_999),
        },
        staleness_threshold_us=5_000_000,
    )
    # Kraken excluded — median of just coinbase.
    assert basket.get_price("btc") == Decimal("65000")


def test_all_venues_stale_returns_none():
    basket = BasketWSReference(
        {"a": _FakeWS({"btc": Decimal("65000")}, age_us=999_999_999)},
        staleness_threshold_us=5_000_000,
    )
    assert basket.get_price("btc") is None


def test_venue_without_asset_excluded():
    basket = BasketWSReference({
        "coinbase": _FakeWS({"btc": Decimal("65000")}),
        "kraken":   _FakeWS({"eth": Decimal("2500")}),  # no btc
    })
    assert basket.get_price("btc") == Decimal("65000")


def test_fresh_venues_count():
    basket = BasketWSReference({
        "a": _FakeWS({"btc": Decimal("65000")}, age_us=1000),
        "b": _FakeWS({"btc": Decimal("65100")}, age_us=999_999_999),
        "c": _FakeWS({"btc": Decimal("65050")}, age_us=1000),
    })
    assert basket.fresh_venues("btc") == 2


def test_snapshot_shows_all_venues_regardless_of_staleness():
    basket = BasketWSReference({
        "a": _FakeWS({"btc": Decimal("65000")}, age_us=1000),
        "b": _FakeWS({"btc": Decimal("65100")}, age_us=999_999_999),
    })
    snap = basket.snapshot("btc")
    assert snap == {"a": Decimal("65000"), "b": Decimal("65100")}


def test_make_basket_fetcher_returns_basket_price():
    basket = BasketWSReference({
        "coinbase": _FakeWS({"btc": Decimal("65000")}),
    })
    fetcher = make_basket_fetcher(basket)
    assert fetcher("btc") == Decimal("65000")


def test_make_basket_fetcher_falls_back_to_rest():
    basket = BasketWSReference(
        {"a": _FakeWS({"btc": Decimal("0")}, age_us=999_999_999)},
        staleness_threshold_us=5_000_000,
    )
    called = []

    def rest_fallback(asset):
        called.append(asset)
        return Decimal("65000")

    fetcher = make_basket_fetcher(basket, rest_fallback=rest_fallback)
    price = fetcher("btc")
    assert price == Decimal("65000")
    assert called == ["btc"]


def test_make_basket_fetcher_no_fallback_returns_none():
    basket = BasketWSReference(
        {"a": _FakeWS({}, age_us=999_999_999)},
        staleness_threshold_us=5_000_000,
    )
    fetcher = make_basket_fetcher(basket, rest_fallback=None)
    assert fetcher("btc") is None
