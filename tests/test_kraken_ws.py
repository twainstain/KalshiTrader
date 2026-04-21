"""Cover `src/market/kraken_ws.py` — mocked WS, no live network."""

from __future__ import annotations

import json
from decimal import Decimal

from market.kraken_ws import (
    KrakenWSReference,
    PAIR_BY_ASSET,
    ASSET_BY_PAIR,
)


def test_pair_by_asset_no_hype():
    assert "hype" not in PAIR_BY_ASSET
    assert set(PAIR_BY_ASSET) == {"btc", "eth", "sol", "xrp", "doge", "bnb"}


def test_asset_by_pair_is_inverse():
    for a, p in PAIR_BY_ASSET.items():
        assert ASSET_BY_PAIR[p] == a


def test_init_filters_hype():
    ref = KrakenWSReference(assets=("btc", "hype", "bnb"))
    assert ref._assets == ("btc", "bnb")


def test_ingest_updates_cache_on_ticker_message():
    ref = KrakenWSReference(assets=("btc",))
    ref._ingest(json.dumps({
        "channel": "ticker",
        "data": [{"symbol": "BTC/USD", "last": 65000.5, "bid": 64999, "ask": 65001}],
    }))
    assert ref.get_price("btc") == Decimal("65000.5")


def test_ingest_falls_back_to_bid_if_last_missing():
    ref = KrakenWSReference(assets=("bnb",))
    ref._ingest(json.dumps({
        "channel": "ticker",
        "data": [{"symbol": "BNB/USD", "bid": 600, "ask": 601}],
    }))
    assert ref.get_price("bnb") == Decimal("600")


def test_ingest_ignores_non_ticker_channel():
    ref = KrakenWSReference()
    ref._ingest(json.dumps({"channel": "heartbeat", "data": []}))
    assert ref.get_price("btc") is None


def test_ingest_ignores_unknown_pair():
    ref = KrakenWSReference()
    ref._ingest(json.dumps({
        "channel": "ticker",
        "data": [{"symbol": "FAKE/USD", "last": 1}],
    }))
    assert ref.get_price("btc") is None


def test_ingest_ignores_malformed_json():
    ref = KrakenWSReference()
    ref._ingest("not json")
    assert ref.get_price("btc") is None


def test_ingest_ignores_missing_price_fields():
    ref = KrakenWSReference()
    ref._ingest(json.dumps({
        "channel": "ticker",
        "data": [{"symbol": "BTC/USD"}],
    }))
    assert ref.get_price("btc") is None


def test_ingest_ignores_malformed_data_shape():
    ref = KrakenWSReference()
    ref._ingest(json.dumps({"channel": "ticker", "data": "not a list"}))
    assert ref.get_price("btc") is None


def test_get_age_us_returns_max_when_empty():
    ref = KrakenWSReference()
    assert ref.get_age_us("btc") == 2**63 - 1


def test_get_age_us_returns_small_after_ingest():
    ref = KrakenWSReference()
    ref._ingest(json.dumps({
        "channel": "ticker",
        "data": [{"symbol": "ETH/USD", "last": 2500}],
    }))
    # age should be very small (sub-second)
    assert ref.get_age_us("eth") < 1_000_000


def test_snapshot_returns_dict_copy():
    ref = KrakenWSReference()
    ref._ingest(json.dumps({
        "channel": "ticker",
        "data": [{"symbol": "BTC/USD", "last": 65000}],
    }))
    snap = ref.snapshot()
    assert "btc" in snap
    assert snap["btc"][0] == Decimal("65000")


def test_multiple_assets_in_one_message():
    ref = KrakenWSReference()
    ref._ingest(json.dumps({
        "channel": "ticker",
        "data": [
            {"symbol": "BTC/USD", "last": 65000},
            {"symbol": "ETH/USD", "last": 2500},
        ],
    }))
    assert ref.get_price("btc") == Decimal("65000")
    assert ref.get_price("eth") == Decimal("2500")


def test_start_idempotent():
    ref = KrakenWSReference(assets=("btc",), connect_factory=_empty_factory)
    ref.start()
    t = ref._thread
    ref.start()
    assert ref._thread is t
    ref.stop(timeout=1.0)


async def _empty_factory(url, sub, stop_event):
    # Immediately yields nothing, simulating a quiet WS for lifecycle tests.
    if False:
        yield {}  # pragma: no cover
    return
