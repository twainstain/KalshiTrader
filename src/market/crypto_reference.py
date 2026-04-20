"""Crypto reference-price sources for the Kalshi fair-value scanner.

Two implementations share a single `CryptoReferenceSource` Protocol:

- `BasketReferenceSource` (P1 default): subscribes to CF Benchmarks
  constituent exchanges per asset (BTC → BRTI constituents, ETH → CF ETH RTI
  constituents, SOL → CF SOL RTI constituents) and aggregates per-second
  using a trimmed median with outlier rejection. Acts as a proxy for the
  licensed RTI feed.

- `LicensedCFBenchmarksSource` (P2 upgrade): stub — returns immediately when
  `CF_BENCHMARKS_API_KEY` is blank. Reserved for the licensed Real-Time
  Index feed once subscribed.

Both sources write every tick to the `reference_ticks` table via
`insert_tick()` so the replay/lag analyses in P1-M5 can reconstruct
exactly what the scanner saw.

The 60s rolling average is the payoff-driving quantity (CRYPTO15M.pdf §0.5),
so `get_60s_avg(asset)` is the primary read surface.
"""

from __future__ import annotations

import logging
import statistics
import threading
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Iterable, Protocol


logger = logging.getLogger(__name__)


SUPPORTED_ASSETS: tuple[str, ...] = ("btc", "eth", "sol")

# CF Benchmarks constituent exchanges per asset, as of 2026-04-19.
# **Verify against cfbenchmarks.com methodology PDFs before relying in P2.**
# Constituent lists evolve — the authoritative source is CF's methodology doc
# per asset (e.g. https://www.cfbenchmarks.com/data/indices/BRTI for BTC).
CF_CONSTITUENTS: dict[str, tuple[str, ...]] = {
    "btc": ("coinbase", "bitstamp", "kraken", "lmax", "itbit"),
    "eth": ("coinbase", "bitstamp", "kraken", "lmax", "itbit"),
    "sol": ("coinbase", "kraken", "bitstamp"),
}


@dataclass(frozen=True)
class ReferenceTick:
    asset: str
    price: Decimal
    ts_us: int
    src: str


class CryptoReferenceSource(Protocol):
    """Minimal reference-source interface used by the scanner + strategy."""

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def is_healthy(self) -> bool: ...
    def get_spot(self, asset: str) -> Decimal | None: ...
    def get_60s_avg(self, asset: str) -> Decimal | None: ...


# ---------------------------------------------------------------------------
# Aggregation helpers — pure functions, trivially testable.
# ---------------------------------------------------------------------------

def reject_outliers(
    prices: Iterable[Decimal],
    *,
    max_dev_pct: Decimal = Decimal("0.01"),
) -> list[Decimal]:
    """Drop values more than `max_dev_pct` (fractional) from the median.

    Default 1% is conservative for crypto reference prices — at $65k BTC a
    1% band is $650, which catches clearly-bogus constituent ticks (wedged
    REST snapshots, exchange outages publishing zeros) without rejecting
    normal intra-second jitter.
    """
    vals = list(prices)
    if len(vals) < 3:
        return vals  # too few to establish a robust median
    med = statistics.median(vals)
    if med <= 0:
        return vals
    threshold = med * max_dev_pct
    return [v for v in vals if abs(v - med) <= threshold]


def aggregate_basket(
    ticks_by_src: dict[str, Decimal],
    *,
    max_dev_pct: Decimal = Decimal("0.01"),
) -> Decimal | None:
    """Combine per-exchange prices into a single reference tick.

    Trimmed median after outlier rejection. Returns None if < 2 constituents
    remain after filtering (an aggregation built from one exchange is not
    meaningful enough to rely on).
    """
    vals = list(ticks_by_src.values())
    accepted = reject_outliers(vals, max_dev_pct=max_dev_pct)
    if len(accepted) < 2:
        return None
    return statistics.median(accepted)


def rolling_average(
    ticks: Iterable[ReferenceTick],
    *,
    window_end_us: int,
    window_seconds: int = 60,
) -> Decimal | None:
    """Compute the simple average over ticks with ts in (end−window, end]."""
    window_start_us = window_end_us - window_seconds * 1_000_000
    in_window = [
        t.price for t in ticks
        if window_start_us < t.ts_us <= window_end_us
    ]
    if not in_window:
        return None
    total = sum(in_window, Decimal("0"))
    return total / Decimal(len(in_window))


# ---------------------------------------------------------------------------
# Persistence helper (T12).
# ---------------------------------------------------------------------------

def insert_tick(conn: Any, tick: ReferenceTick) -> None:
    """Write a `ReferenceTick` row to `reference_ticks`.

    `conn` is a DB-API connection (sqlite3 or psycopg2). The caller owns
    commit cadence; batching is desirable at high tick rates.
    """
    conn.execute(
        "INSERT INTO reference_ticks (asset, ts_us, price, src) VALUES (?, ?, ?, ?)",
        (tick.asset, int(tick.ts_us), str(tick.price), tick.src),
    )


def insert_tick_postgres(conn: Any, tick: ReferenceTick) -> None:
    """Postgres variant — uses `%s` placeholders instead of `?`."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO reference_ticks (asset, ts_us, price, src) "
            "VALUES (%s, %s, %s, %s)",
            (tick.asset, int(tick.ts_us), str(tick.price), tick.src),
        )


# ---------------------------------------------------------------------------
# BasketReferenceSource (T09–T10).
# ---------------------------------------------------------------------------

@dataclass
class _PerAssetState:
    recent_ticks: deque[ReferenceTick] = field(default_factory=lambda: deque(maxlen=1024))
    latest_by_src: dict[str, ReferenceTick] = field(default_factory=dict)


class BasketReferenceSource:
    """Per-asset aggregation of CF Benchmarks constituent exchanges.

    Designed to be fed by an out-of-band ingester (one per exchange) via
    `record_tick()`. This class is feed-agnostic so tests can drive it
    deterministically without websockets. The actual exchange adapters
    land in P2 (or before, if latency analysis needs them earlier).

    Aggregation policy matches CF Benchmarks' simple-median approach for
    P1 — the licensed feed (T11) is the upgrade path when accuracy
    matters for live trading (P2).
    """

    def __init__(
        self,
        assets: Iterable[str] = SUPPORTED_ASSETS,
        *,
        constituents: dict[str, tuple[str, ...]] | None = None,
        max_dev_pct: Decimal = Decimal("0.01"),
        stale_seconds: float = 5.0,
        now_us: Callable[[], int] | None = None,
    ) -> None:
        self._assets = tuple(a.lower() for a in assets)
        self._constituents = constituents or CF_CONSTITUENTS
        self._max_dev_pct = max_dev_pct
        self._stale_seconds = stale_seconds
        import time as _t
        self._now_us = now_us or (lambda: int(_t.time() * 1_000_000))
        self._lock = threading.Lock()
        self._state: dict[str, _PerAssetState] = {a: _PerAssetState() for a in self._assets}
        self._running = False

    # ---- lifecycle ----

    def start(self) -> None:
        self._running = True

    def stop(self) -> None:
        self._running = False

    def is_healthy(self) -> bool:
        if not self._running:
            return False
        now_us = self._now_us()
        threshold_us = int(self._stale_seconds * 1_000_000)
        with self._lock:
            for asset, st in self._state.items():
                if not st.latest_by_src:
                    return False
                most_recent = max(t.ts_us for t in st.latest_by_src.values())
                if now_us - most_recent > threshold_us:
                    return False
        return True

    # ---- ingestion ----

    def record_tick(self, tick: ReferenceTick) -> None:
        """Feed a single constituent-exchange tick into the aggregator."""
        asset = tick.asset.lower()
        if asset not in self._state:
            return  # unsupported asset — silently drop so tests stay simple
        with self._lock:
            st = self._state[asset]
            st.latest_by_src[tick.src] = tick
            st.recent_ticks.append(tick)

    # ---- read surface ----

    def get_spot(self, asset: str) -> Decimal | None:
        asset = asset.lower()
        with self._lock:
            st = self._state.get(asset)
            if not st or not st.latest_by_src:
                return None
            prices = {s: t.price for s, t in st.latest_by_src.items()}
        agg = aggregate_basket(prices, max_dev_pct=self._max_dev_pct)
        if agg is not None:
            return agg
        # Single-source fallback: if we only have one venue (or outlier
        # rejection left fewer than 2), return that venue's latest price
        # rather than None. `aggregate_basket` refuses to aggregate < 2
        # exchanges, which is correct for the basket-vs-CF-benchmarks
        # tracking analysis, but overly strict for a scanner that has only
        # one reference source wired.
        if prices:
            return next(iter(prices.values()))
        return None

    def get_60s_avg(self, asset: str) -> Decimal | None:
        asset = asset.lower()
        now_us = self._now_us()
        with self._lock:
            st = self._state.get(asset)
            if not st:
                return None
            ticks = list(st.recent_ticks)
        # Use only the aggregated basket per distinct ts bucket to avoid
        # over-weighting seconds where many exchanges ticked simultaneously.
        # Simplification for P1: raw average of all constituent ticks in
        # window. P2 should match CF's true weighted methodology.
        return rolling_average(ticks, window_end_us=now_us, window_seconds=60)

    # ---- test seams ----

    def snapshot_state(self, asset: str) -> dict[str, ReferenceTick]:
        asset = asset.lower()
        with self._lock:
            st = self._state.get(asset)
            return dict(st.latest_by_src) if st else {}


# ---------------------------------------------------------------------------
# LicensedCFBenchmarksSource (T11) — stub.
# ---------------------------------------------------------------------------

class LicensedCFBenchmarksSource:
    """Licensed CF Benchmarks RTI feed — P2 upgrade.

    Stub: returns `None` for every read and reports unhealthy when the API
    key is blank (P1 default). The real implementation subscribes to CF's
    WebSocket or REST feed once a commercial license is provisioned.
    """

    def __init__(self, api_key: str = "") -> None:
        self._api_key = api_key
        self._running = False

    @property
    def is_licensed(self) -> bool:
        return bool(self._api_key)

    def start(self) -> None:
        if not self._api_key:
            logger.info("CF_BENCHMARKS_API_KEY unset — licensed feed is a no-op")
            return
        # Real implementation lands in P2.
        self._running = True

    def stop(self) -> None:
        self._running = False

    def is_healthy(self) -> bool:
        return False  # never healthy until the real client lands

    def get_spot(self, asset: str) -> Decimal | None:
        return None

    def get_60s_avg(self, asset: str) -> Decimal | None:
        return None
