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


def test_discover_prunes_tickers_that_drop_out_of_open_status():
    """Regression: when a ticker stops appearing in /markets?status=open
    (15-min window closed), it must be removed from `_market_meta` and
    `_asset_by_ticker` so `snapshot_books()` stops hitting its orderbook
    endpoint. Previously, the evaluator kept writing decisions on the stale
    book with last-known `time_remaining_s`.
    """
    rest = MagicMock()
    call_count = {"n": 0}

    def _fake(method, path, **kwargs):
        series = kwargs["params"]["series_ticker"]
        if call_count["n"] == 0:
            # First pass: BTC has ticker A.
            if series == "KXBTC15M":
                return {"markets": [_market_payload(
                    ticker="KXBTC15M-A", event_ticker="KXBTC15M-A-EV",
                )]}
            return {"markets": []}
        else:
            # Second pass: BTC's ticker A has rolled out; B is now open.
            if series == "KXBTC15M":
                return {"markets": [_market_payload(
                    ticker="KXBTC15M-B", event_ticker="KXBTC15M-B-EV",
                )]}
            return {"markets": []}

    rest.request.side_effect = _fake
    coord = _mk_coord(rest)

    coord.discover()
    assert "KXBTC15M-A" in coord.market_meta
    assert coord.asset_by_ticker["KXBTC15M-A"] == "btc"

    call_count["n"] = 1
    coord.discover()
    # A must be pruned; B must be present.
    assert "KXBTC15M-A" not in coord.market_meta
    assert "KXBTC15M-A" not in coord.asset_by_ticker
    assert "KXBTC15M-B" in coord.market_meta
    assert coord.asset_by_ticker["KXBTC15M-B"] == "btc"


def test_discover_does_not_prune_on_rest_failure_for_that_series():
    """If /markets fails for a series (HTTP exception), existing tickers
    for that series must be preserved — transient network errors shouldn't
    wipe catalog state. Only an explicit empty-response prunes."""
    rest = MagicMock()
    call_count = {"n": 0}

    def _fake(method, path, **kwargs):
        series = kwargs["params"]["series_ticker"]
        if call_count["n"] == 0 and series == "KXBTC15M":
            return {"markets": [_market_payload(ticker="KXBTC15M-OLD")]}
        if call_count["n"] == 1 and series == "KXBTC15M":
            raise RuntimeError("transient network error")
        return {"markets": []}

    rest.request.side_effect = _fake
    coord = _mk_coord(rest)
    coord.discover()
    assert "KXBTC15M-OLD" in coord.market_meta

    call_count["n"] = 1
    coord.discover()
    # Transient failure preserves the ticker; it's not in the response but
    # the response itself was an exception, not an empty list.
    assert "KXBTC15M-OLD" in coord.market_meta


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
    ev.pure_lag_partner = None
    ev.partners = []

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
    ev.pure_lag_partner = None
    ev.partners = []
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
    ev.pure_lag_partner = None
    ev.partners = []  # no side-by-side partner in this test
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


# ----------------------------------------------------------------------
# LiveDataCoordinator.snapshot_books — parallel fetch (2026-04-21)
# ----------------------------------------------------------------------

def _dispatching_rest_slow(market_payload, per_ticker_books, delay_s=0.05):
    """REST mock that sleeps per orderbook call — lets us measure parallel
    speedup by comparing wall-clock to (N × delay_s)."""
    import time as _t
    rest = MagicMock()

    def _fake(method, path, **kwargs):
        if path == "/markets":
            series = kwargs["params"]["series_ticker"]
            return {"markets": [{**market_payload,
                                 "ticker": f"{series}-26APR201500-00"}]}
        if "/orderbook" in path:
            _t.sleep(delay_s)  # simulate network latency
            ticker = path.rsplit("/", 2)[-2]  # .../markets/{ticker}/orderbook
            return per_ticker_books.get(ticker, {})
        return {}

    rest.request.side_effect = _fake
    return rest


class TestSnapshotBooksParallel:
    def test_no_tickers_is_noop(self):
        """Empty market_meta → no workers spawned, no exception."""
        rest = _dispatching_rest(_market_payload(), orderbook_body={})
        coord = _mk_coord(rest)
        # No discover() call — _market_meta is empty.
        coord.snapshot_books()
        # Pool stays uninitialized when nothing to do.
        assert coord._snapshot_pool is None

    def test_parallel_fetch_faster_than_sequential(self):
        """With 7 tickers × 50ms each, parallel (10 workers) should finish
        in ~1 delay; sequential would take ~7 × delay."""
        import time as _t
        books = {
            f"KX{s}15M-26APR201500-00": {"orderbook_fp":
                {"yes_dollars": [["0.40", "5"]], "no_dollars": [["0.59", "5"]]}}
            for s in ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE")
        }
        rest = _dispatching_rest_slow(_market_payload(), books, delay_s=0.05)
        coord = _mk_coord(rest)
        coord.discover()   # populates 7 tickers
        assert len(coord._market_meta) == 7

        t0 = _t.monotonic()
        coord.snapshot_books()
        elapsed_s = _t.monotonic() - t0
        coord.close()

        # Sequential lower bound: 7 × 0.05s = 0.35s. Parallel upper bound:
        # ~0.15s (one wave with overhead). Assert the gap to prove
        # parallelism is actually happening.
        assert elapsed_s < 0.30, (
            f"snapshot_books took {elapsed_s:.3f}s — expected parallel "
            f"(< 0.30s), got sequential-ish timing"
        )

    def test_per_ticker_exception_does_not_block_others(self):
        """One failing ticker must not break the others."""
        rest = MagicMock()

        def _fake(method, path, **kwargs):
            if path == "/markets":
                series = kwargs["params"]["series_ticker"]
                return {"markets": [{**_market_payload(),
                                     "ticker": f"{series}-26APR201500-00"}]}
            if "/orderbook" in path:
                if "BTC" in path:
                    raise RuntimeError("503 synthetic")
                return {"orderbook_fp":
                        {"yes_dollars": [["0.50", "10"]],
                         "no_dollars":  [["0.49", "10"]]}}
            return {}
        rest.request.side_effect = _fake
        coord = _mk_coord(rest)
        coord.discover()
        coord.snapshot_books()
        coord.close()

        # BTC's book stays empty (fetch raised); any other asset that
        # succeeded has a parsed book.
        with coord._market_source._books_lock:
            books = dict(coord._market_source._books)
        btc = books.get("KXBTC15M-26APR201500-00")
        eth = books.get("KXETH15M-26APR201500-00")
        # BTC entry should exist only if a prior snapshot succeeded;
        # here it's the first pass, so BTC is absent.
        assert btc is None or btc.book == {"yes": [], "no": []}
        # ETH (and the other non-BTC assets) landed.
        assert eth is not None
        assert eth.book["yes"] == [["0.50", "10"]]

    def test_max_workers_is_configurable(self):
        """`snapshot_max_workers=1` forces sequential execution — useful for
        tests and for staying under Kalshi's Basic-tier read-rate limit."""
        rest = _dispatching_rest(_market_payload(), orderbook_body={
            "orderbook_fp": {"yes_dollars": [], "no_dollars": []},
        })
        coord = rks.LiveDataCoordinator(
            rest_client=rest,
            reference_fetcher=lambda asset: None,
            market_source=KalshiMarketSource(KalshiMarketConfig()),
            reference_source=BasketReferenceSource(
                assets=tuple(set(rks.ASSET_FROM_SERIES.values()))
            ),
            snapshot_max_workers=1,
        )
        coord.discover()
        coord.snapshot_books()
        # Pool was created and has exactly 1 worker.
        assert coord._snapshot_pool._max_workers == 1
        coord.close()

    def test_pool_reused_across_ticks(self):
        rest = _dispatching_rest(_market_payload(), orderbook_body={})
        coord = _mk_coord(rest)
        coord.discover()
        coord.snapshot_books()
        pool_ref_1 = coord._snapshot_pool
        coord.snapshot_books()
        pool_ref_2 = coord._snapshot_pool
        assert pool_ref_1 is pool_ref_2   # same pool, no thrash
        coord.close()

    def test_close_is_idempotent(self):
        rest = _dispatching_rest(_market_payload(), orderbook_body={})
        coord = _mk_coord(rest)
        coord.discover()
        coord.snapshot_books()
        coord.close()
        coord.close()  # second call must not raise
        assert coord._snapshot_pool is None

    def test_close_without_any_snapshot_is_safe(self):
        """`close()` before the first snapshot — pool was never created."""
        coord = _mk_coord(_dispatching_rest(_market_payload(), {}))
        coord.close()  # no crash
        assert coord._snapshot_pool is None
