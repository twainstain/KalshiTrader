"""Cover `src/run_kalshi_shadow.py` — LiveDataCoordinator + helpers.

Tests the glue code between the REST client, reference fetcher, market
source, reference source, and shadow evaluator. All network calls are
mocked; these tests verify the response-parsing and shared-state
behavior that got caught by bugs during the 2026-04-20 live run.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

import run_kalshi_shadow as rks
from market.crypto_reference import BasketReferenceSource
from market.kalshi_market import KalshiMarketConfig, KalshiMarketSource


# ----------------------------------------------------------------------
# scripts_compat.parse_iso_or_epoch
# ----------------------------------------------------------------------

def test_parse_iso_accepts_iso_z_suffix():
    assert rks.scripts_compat.parse_iso_or_epoch("2026-04-20T13:30:00Z") == 1776691800


def test_parse_iso_accepts_iso_with_offset():
    # Same moment in time via explicit offset rather than Z.
    v = rks.scripts_compat.parse_iso_or_epoch("2026-04-20T13:30:00+00:00")
    assert v == 1776691800


def test_parse_iso_accepts_numeric_epoch():
    assert rks.scripts_compat.parse_iso_or_epoch(1_776_691_800) == 1_776_691_800
    assert rks.scripts_compat.parse_iso_or_epoch("1776691800") == 1_776_691_800


def test_parse_iso_returns_zero_for_none_or_empty():
    assert rks.scripts_compat.parse_iso_or_epoch(None) == 0
    assert rks.scripts_compat.parse_iso_or_epoch("") == 0


def test_parse_iso_returns_zero_for_garbage():
    assert rks.scripts_compat.parse_iso_or_epoch("not-a-date") == 0


# ----------------------------------------------------------------------
# COMPARATOR_MAP — maps Kalshi's `strike_type` values into our supported set
# ----------------------------------------------------------------------

def test_comparator_map_covers_kalshi_variants():
    assert rks.COMPARATOR_MAP["greater_or_equal"] == "at_least"
    assert rks.COMPARATOR_MAP["greater_than"] == "above"
    assert rks.COMPARATOR_MAP["less_or_equal"] == "below"
    assert rks.COMPARATOR_MAP["less_than"] == "below"


def test_comparator_map_short_forms():
    assert rks.COMPARATOR_MAP["ge"] == "at_least"
    assert rks.COMPARATOR_MAP["gt"] == "above"
    assert rks.COMPARATOR_MAP["le"] == "below"
    assert rks.COMPARATOR_MAP["lt"] == "below"


# ----------------------------------------------------------------------
# ASSET_FROM_SERIES — all 7 assets enumerated
# ----------------------------------------------------------------------

def test_asset_from_series_has_all_seven_assets():
    expected = {"btc", "eth", "sol", "xrp", "doge", "bnb", "hype"}
    assert set(rks.ASSET_FROM_SERIES.values()) == expected


def test_asset_from_series_uses_kalshi_prefixes():
    for series in rks.ASSET_FROM_SERIES:
        assert series.startswith("KX")
        assert series.endswith("15M")


# ----------------------------------------------------------------------
# default_coinbase_fetcher — unknown assets, error paths
# ----------------------------------------------------------------------

def test_default_coinbase_fetcher_rejects_unknown_asset():
    assert rks.default_coinbase_fetcher("doge_sv") is None


def test_default_coinbase_fetcher_on_network_error(monkeypatch):
    class _Boom:
        def get(self, *args, **kwargs):
            raise __import__("requests").ConnectionError("boom")

    monkeypatch.setattr("requests.get", lambda *a, **kw: _Boom().get())
    # Should not raise; returns None on transport error.
    assert rks.default_coinbase_fetcher("btc") is None


def test_default_coinbase_fetcher_on_http_non_200(monkeypatch):
    def _get(*args, **kwargs):
        m = MagicMock()
        m.status_code = 502
        return m

    monkeypatch.setattr("requests.get", _get)
    assert rks.default_coinbase_fetcher("btc") is None


def test_default_coinbase_fetcher_happy_path(monkeypatch):
    def _get(*args, **kwargs):
        m = MagicMock()
        m.status_code = 200
        m.json.return_value = {"price": "42069.5"}
        return m

    monkeypatch.setattr("requests.get", _get)
    v = rks.default_coinbase_fetcher("btc")
    assert v == Decimal("42069.5")


# ----------------------------------------------------------------------
# LiveDataCoordinator.discover
# ----------------------------------------------------------------------

def _mk_coord(rest_client, reference_fetcher=None):
    """Coordinator with a BasketReferenceSource matching all 7 supported assets."""
    return rks.LiveDataCoordinator(
        rest_client=rest_client,
        reference_fetcher=reference_fetcher or (lambda asset: None),
        market_source=KalshiMarketSource(KalshiMarketConfig()),
        reference_source=BasketReferenceSource(
            assets=tuple(set(rks.ASSET_FROM_SERIES.values()))
        ),
    )


def _dispatching_rest(market_payload=None, orderbook_body=None):
    """Mock REST that dispatches by path pattern and reuses the same payload
    across every call — avoids side_effect list exhaustion."""
    rest = MagicMock()

    def _fake(method, path, **kwargs):
        if path == "/markets":
            series = kwargs["params"]["series_ticker"]
            # Return the supplied template with its ticker rewritten for this series.
            payload = dict(market_payload or {"ticker": f"{series}-TKR"})
            payload["ticker"] = f"{series}-26APR201500-00"
            return {"markets": [payload]}
        if "/orderbook" in path:
            return orderbook_body or {}
        return {}

    rest.request.side_effect = _fake
    return rest


def _market_payload(**overrides):
    base = {
        "ticker": "KXBTC15M-26APR201500-00",
        "event_ticker": "KXBTC15M-26APR201500",
        "strike_price": 65000,
        "strike_type": "greater_or_equal",
        "close_time": "2026-04-20T15:00:00Z",
        "expiration_time": "2026-04-27T15:00:00Z",  # 7-day sentinel — must NOT be used
    }
    base.update(overrides)
    return base


def test_discover_populates_market_meta_for_all_series():
    rest = _dispatching_rest(_market_payload())
    coord = _mk_coord(rest)
    coord.discover()
    # 7 assets → 7 markets discovered.
    assert len(coord.market_meta) == len(rks.ASSET_FROM_SERIES)
    for series, asset in rks.ASSET_FROM_SERIES.items():
        ticker = f"{series}-26APR201500-00"
        assert ticker in coord.market_meta
        assert coord.asset_by_ticker[ticker] == asset


def test_discover_normalizes_comparator():
    """`strike_type=greater_or_equal` → `at_least` (MarketQuote-supported)."""
    rest = _dispatching_rest(_market_payload())
    coord = _mk_coord(rest)
    coord.discover()
    meta = coord.market_meta["KXBTC15M-26APR201500-00"]
    assert meta["comparator"] == "at_least"


def test_discover_uses_close_time_not_expiration_time():
    """Regression: expiration_time was 7 days out; we need close_time."""
    # close_time and expiration_time are different; confirm parser picks close_time.
    close_iso = "2026-04-20T15:00:00Z"
    exp_iso   = "2026-04-27T15:00:00Z"
    rest = _dispatching_rest(_market_payload(
        close_time=close_iso, expiration_time=exp_iso,
    ))
    coord = _mk_coord(rest)
    coord.discover()
    meta = coord.market_meta["KXBTC15M-26APR201500-00"]
    expected = rks.scripts_compat.parse_iso_or_epoch(close_iso)
    wrong = rks.scripts_compat.parse_iso_or_epoch(exp_iso)
    assert meta["expiration_ts"] == expected
    assert meta["expiration_ts"] != wrong


def test_discover_survives_rest_failure_per_series():
    rest = MagicMock()
    calls = [0]

    def _fake(method, path, **kwargs):
        calls[0] += 1
        if calls[0] == 1:
            raise RuntimeError("boom")
        series = kwargs["params"]["series_ticker"]
        return {"markets": [_market_payload(
            ticker=f"{series}-X-0",
        )]}

    rest.request.side_effect = _fake
    coord = _mk_coord(rest)
    coord.discover()  # does not raise
    # 6 of 7 markets discovered (first one failed).
    assert len(coord.market_meta) == len(rks.ASSET_FROM_SERIES) - 1


def test_discover_uses_status_open_not_active():
    """Regression: status=active is not valid per Kalshi API."""
    rest = _dispatching_rest(_market_payload())
    coord = _mk_coord(rest)
    coord.discover()
    # Every /markets call's params must carry status=open.
    for call in rest.request.call_args_list:
        args, kwargs = call
        if call.args and call.args[1] == "/markets":
            assert kwargs["params"]["status"] == "open"


# ----------------------------------------------------------------------
# LiveDataCoordinator.snapshot_books — orderbook_fp parsing
# ----------------------------------------------------------------------

def test_snapshot_books_parses_orderbook_fp_format():
    """Regression: Kalshi returns `orderbook_fp.yes_dollars/no_dollars`
    (fixed-point), not `orderbook.yes/no`."""
    ob = {"orderbook_fp": {
        "yes_dollars": [["0.40", "100"], ["0.41", "50"]],
        "no_dollars":  [["0.58", "80"],  ["0.59", "20"]],
    }}
    rest = _dispatching_rest(_market_payload(), orderbook_body=ob)
    coord = _mk_coord(rest)
    coord.discover()
    coord.snapshot_books()

    # Check that at least one ticker's book got the parsed levels.
    sample_ticker = "KXBTC15M-26APR201500-00"
    with coord._market_source._books_lock:
        book = coord._market_source._books[sample_ticker].book
    assert len(book["yes"]) == 2
    assert len(book["no"]) == 2
    assert book["yes"][0] == ["0.40", "100"]


def test_snapshot_books_falls_back_to_legacy_orderbook_key():
    ob = {"orderbook": {
        "yes": [["0.42", "5"]],
        "no":  [["0.57", "7"]],
    }}
    rest = _dispatching_rest(_market_payload(), orderbook_body=ob)
    coord = _mk_coord(rest)
    coord.discover()
    coord.snapshot_books()

    with coord._market_source._books_lock:
        book = coord._market_source._books["KXBTC15M-26APR201500-00"].book
    assert book["yes"] == [["0.42", "5"]]


def test_snapshot_books_handles_empty_response():
    rest = _dispatching_rest(_market_payload(), orderbook_body={})
    coord = _mk_coord(rest)
    coord.discover()
    coord.snapshot_books()
    with coord._market_source._books_lock:
        book = coord._market_source._books["KXBTC15M-26APR201500-00"].book
    assert book == {"yes": [], "no": []}


# ----------------------------------------------------------------------
# LiveDataCoordinator.sample_reference
# ----------------------------------------------------------------------

def test_sample_reference_feeds_all_assets_into_basket_source():
    fetched = {}

    def fake_fetch(asset):
        fetched.setdefault(asset, 0)
        fetched[asset] += 1
        return Decimal("100")

    rest = MagicMock()
    coord = _mk_coord(rest, reference_fetcher=fake_fetch)
    coord.sample_reference()
    # The coordinator calls fetch_reference once per unique asset it tracks.
    assert set(fetched.keys()) == set(rks.ASSET_FROM_SERIES.values())
    # Each asset produced one ReferenceTick inside the basket source.
    for a in rks.ASSET_FROM_SERIES.values():
        state = coord._reference_source.snapshot_state(a)
        assert len(state) >= 1


def test_sample_reference_ignores_fetcher_returning_none():
    rest = MagicMock()
    coord = _mk_coord(rest, reference_fetcher=lambda a: None)
    coord.sample_reference()
    for a in rks.ASSET_FROM_SERIES.values():
        state = coord._reference_source.snapshot_state(a)
        assert state == {}


# ----------------------------------------------------------------------
# build_resolution_lookup
# ----------------------------------------------------------------------

def test_resolution_lookup_extracts_market_from_wrapper():
    rest = MagicMock()
    rest.request.return_value = {
        "market": {"ticker": "KXBTC15M-...", "status": "finalized", "result": "yes"}
    }
    fn = rks.build_resolution_lookup(rest)
    r = fn("KXBTC15M-whatever")
    assert r["status"] == "finalized"
    assert r["result"] == "yes"


def test_resolution_lookup_tolerates_unwrapped_market():
    """Some Kalshi responses return the market object directly."""
    rest = MagicMock()
    rest.request.return_value = {"ticker": "KXBTC15M-...", "status": "open"}
    fn = rks.build_resolution_lookup(rest)
    r = fn("KXBTC15M-whatever")
    assert r["status"] == "open"


def test_resolution_lookup_returns_none_on_error():
    rest = MagicMock()
    rest.request.side_effect = RuntimeError("boom")
    fn = rks.build_resolution_lookup(rest)
    assert fn("anything") is None


# ----------------------------------------------------------------------
# run_loop — SIGINT / iteration-cap behavior
# ----------------------------------------------------------------------

def test_run_loop_honors_iterations_cap(tmp_path):
    # Minimal mocks — evaluator returns zero every tick.
    ev = MagicMock()
    ev.tick.return_value = {"written": 0, "reconciled": 0}

    totals = rks.run_loop(
        evaluator=ev, coordinator=None,
        iterations=3, interval_s=0, no_sleep=True,
    )
    assert totals["ticks"] == 3
    assert ev.tick.call_count == 3


def test_run_loop_stops_on_stop_event():
    import threading
    ev = MagicMock()
    ev.tick.return_value = {"written": 0, "reconciled": 0}
    stop = threading.Event()
    stop.set()
    totals = rks.run_loop(
        evaluator=ev, coordinator=None,
        iterations=None, interval_s=0, no_sleep=True,
        stop_event=stop,
    )
    assert totals["ticks"] == 0
    assert ev.tick.call_count == 0


def test_run_loop_calls_coordinator_per_tick():
    coord = MagicMock()
    ev = MagicMock()
    ev.tick.return_value = {"written": 1, "reconciled": 0}
    totals = rks.run_loop(
        evaluator=ev, coordinator=coord,
        iterations=3, interval_s=0, no_sleep=True,
        discover_every=100,  # skip re-discovery during test
    )
    # discover at i=0 only
    assert coord.discover.call_count == 1
    # snapshot_books + sample_reference once per tick
    assert coord.snapshot_books.call_count == 3
    assert coord.sample_reference.call_count == 3
    assert totals["written"] == 3
