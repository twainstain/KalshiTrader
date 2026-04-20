"""Cover `src/market/coinbase_ws.py` — mocked WS, no live network."""

from __future__ import annotations

import json
import threading
import time
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from market.coinbase_ws import (
    CoinbaseWSReference,
    PRODUCT_BY_ASSET,
    ASSET_BY_PRODUCT,
    make_ws_reference_fetcher,
)


# ----------------------------------------------------------------------
# Constants + plain helpers
# ----------------------------------------------------------------------

def test_product_by_asset_has_7_assets():
    assert set(PRODUCT_BY_ASSET) == {"btc","eth","sol","xrp","doge","bnb","hype"}


def test_asset_by_product_is_inverse():
    for a, p in PRODUCT_BY_ASSET.items():
        assert ASSET_BY_PRODUCT[p] == a


def test_init_filters_unsupported_assets():
    ref = CoinbaseWSReference(assets=("btc", "doge", "sv-impossible"))
    assert ref._assets == ("btc", "doge")


# ----------------------------------------------------------------------
# _ingest — message parsing
# ----------------------------------------------------------------------

def test_ingest_updates_cache_on_ticker_message():
    ref = CoinbaseWSReference(assets=("btc",))
    ref._ingest(json.dumps({
        "type": "ticker", "product_id": "BTC-USD", "price": "65000.5",
    }))
    assert ref.get_price("btc") == Decimal("65000.5")


def test_ingest_dict_message_also_accepted():
    ref = CoinbaseWSReference()
    ref._ingest({"type": "ticker", "product_id": "ETH-USD", "price": "2500"})
    assert ref.get_price("eth") == Decimal("2500")


def test_ingest_ignores_non_ticker_type():
    ref = CoinbaseWSReference()
    ref._ingest(json.dumps({"type": "subscriptions", "channels": []}))
    assert ref.get_price("btc") is None


def test_ingest_ignores_unknown_product():
    ref = CoinbaseWSReference()
    ref._ingest(json.dumps({
        "type": "ticker", "product_id": "DOGE_SV-USD", "price": "1"
    }))
    assert ref.get_price("doge_sv") is None


def test_ingest_ignores_malformed_json():
    ref = CoinbaseWSReference()
    ref._ingest("not json")
    assert ref.get_price("btc") is None


def test_ingest_ignores_missing_price():
    ref = CoinbaseWSReference()
    ref._ingest(json.dumps({"type": "ticker", "product_id": "BTC-USD"}))
    assert ref.get_price("btc") is None


# ----------------------------------------------------------------------
# get_price / get_age_us
# ----------------------------------------------------------------------

def test_get_price_returns_none_when_unsubscribed():
    ref = CoinbaseWSReference(assets=("btc",))
    assert ref.get_price("btc") is None
    assert ref.get_price("eth") is None


def test_get_age_us_grows_with_wall_clock():
    ref = CoinbaseWSReference()
    ref._ingest(json.dumps({
        "type": "ticker", "product_id": "BTC-USD", "price": "1",
    }))
    a1 = ref.get_age_us("btc")
    time.sleep(0.01)
    a2 = ref.get_age_us("btc")
    assert a2 > a1


def test_get_age_us_huge_when_never_seen():
    ref = CoinbaseWSReference()
    assert ref.get_age_us("btc") > 10**15


def test_snapshot_returns_all_cached_assets():
    ref = CoinbaseWSReference()
    ref._ingest({"type": "ticker", "product_id": "BTC-USD", "price": "1"})
    ref._ingest({"type": "ticker", "product_id": "ETH-USD", "price": "2"})
    snap = ref.snapshot()
    assert "btc" in snap
    assert "eth" in snap
    assert snap["btc"][0] == Decimal("1")


# ----------------------------------------------------------------------
# Lifecycle — start/stop with mocked async connect factory
# ----------------------------------------------------------------------

def _fake_connect_factory(messages: list[str | dict]):
    """Return a coroutine-factory that yields `messages` once then stops."""
    async def _factory(url, sub, stop_event):
        for m in messages:
            if stop_event.is_set():
                break
            yield m if isinstance(m, str) else json.dumps(m)
    return _factory


def test_start_drains_ws_messages_into_cache():
    messages = [
        {"type": "subscriptions"},
        {"type": "ticker", "product_id": "BTC-USD", "price": "65000"},
        {"type": "ticker", "product_id": "ETH-USD", "price": "2500"},
    ]
    ref = CoinbaseWSReference(
        assets=("btc", "eth"),
        connect_factory=_fake_connect_factory(messages),
    )
    ref.start()
    # Wait for thread to drain messages (< 100 ms in practice).
    for _ in range(50):
        if ref.get_price("btc") is not None and ref.get_price("eth") is not None:
            break
        time.sleep(0.01)
    ref.stop(timeout=1.0)

    assert ref.get_price("btc") == Decimal("65000")
    assert ref.get_price("eth") == Decimal("2500")


def test_start_reconnects_on_factory_error(monkeypatch):
    """If the connect factory raises once, the main loop backs off and retries."""
    attempts = {"n": 0}

    async def flaky_factory(url, sub, stop_event):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("transient")
        # Second attempt delivers a tick, then exits.
        yield json.dumps({
            "type": "ticker", "product_id": "BTC-USD", "price": "99",
        })

    ref = CoinbaseWSReference(
        assets=("btc",),
        connect_factory=flaky_factory,
        reconnect_initial_s=0.01,
        reconnect_max_s=0.01,
    )
    ref.start()
    for _ in range(100):
        if ref.get_price("btc") is not None:
            break
        time.sleep(0.01)
    ref.stop(timeout=1.0)
    assert attempts["n"] >= 2
    assert ref.get_price("btc") == Decimal("99")


def test_stop_idempotent_on_never_started():
    ref = CoinbaseWSReference()
    # Should not raise even when no thread was started.
    ref.stop()


# ----------------------------------------------------------------------
# make_ws_reference_fetcher — the shim used by the shadow runner
# ----------------------------------------------------------------------

def test_fetcher_returns_ws_price_when_fresh():
    ws = CoinbaseWSReference()
    ws._ingest({"type": "ticker", "product_id": "BTC-USD", "price": "65000"})
    fetcher = make_ws_reference_fetcher(ws, staleness_threshold_us=10_000_000)
    assert fetcher("btc") == Decimal("65000")


def test_fetcher_returns_none_when_stale_and_no_fallback():
    ws = CoinbaseWSReference()
    ws._ingest({"type": "ticker", "product_id": "BTC-USD", "price": "65000"})
    # Force staleness: set threshold to 0 (any age is stale).
    fetcher = make_ws_reference_fetcher(ws, staleness_threshold_us=0)
    assert fetcher("btc") is None


def test_fetcher_falls_back_to_rest_when_stale():
    ws = CoinbaseWSReference()
    calls = []
    def rest(asset):
        calls.append(asset)
        return Decimal("64999")
    fetcher = make_ws_reference_fetcher(
        ws, staleness_threshold_us=0, rest_fallback=rest,
    )
    # WS has no data → fall back to rest.
    v = fetcher("btc")
    assert v == Decimal("64999")
    assert calls == ["btc"]


def test_fetcher_falls_back_when_no_ws_data():
    ws = CoinbaseWSReference()
    rest = MagicMock(return_value=Decimal("1"))
    fetcher = make_ws_reference_fetcher(ws, rest_fallback=rest)
    assert fetcher("btc") == Decimal("1")
    rest.assert_called_once_with("btc")
