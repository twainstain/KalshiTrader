"""Multi-venue WS basket reference.

Combines latest-prices from several `*WSReference` sources (Coinbase,
Kraken, etc.) into a single per-asset price via median of non-stale venues.
Addresses the basket-vs-single-venue tracking error flagged in
`docs/kalshi_crypto_multi_asset_report.md` §7 — particularly for BNB where
Coinbase is not a CF Benchmarks constituent.
"""

from __future__ import annotations

import statistics
import time
from decimal import Decimal
from typing import Protocol


class _WSLike(Protocol):
    """Duck-typed interface shared by Coinbase + Kraken WS references."""
    def get_price(self, asset: str) -> Decimal | None: ...
    def get_age_us(self, asset: str) -> int: ...


class BasketWSReference:
    """Median-aggregate over N venue WS sources.

    Caller supplies the individual sources (already started). This class is
    just a read-surface wrapper — no thread of its own, no lifecycle.

    staleness_threshold_us: venues with last update older than this are
    excluded from the median. Default 5s.
    """

    def __init__(
        self,
        sources: dict[str, _WSLike],
        *,
        staleness_threshold_us: int = 5_000_000,
    ) -> None:
        # `sources` keyed by label (e.g. "coinbase", "kraken"). Value is a
        # WS reference exposing get_price/get_age_us.
        self._sources = dict(sources)
        self._staleness_us = staleness_threshold_us

    def get_price(self, asset: str) -> Decimal | None:
        fresh = self._fresh_prices(asset)
        if not fresh:
            return None
        # Median — robust to a single outlier. For 2 venues Python statistics
        # returns the average of the two, which is fine.
        return Decimal(str(statistics.median(float(p) for p in fresh.values())))

    def fresh_venues(self, asset: str) -> int:
        return len(self._fresh_prices(asset))

    def snapshot(self, asset: str) -> dict[str, Decimal]:
        """Per-venue latest-price snapshot (regardless of staleness)."""
        out: dict[str, Decimal] = {}
        for label, src in self._sources.items():
            p = src.get_price(asset)
            if p is not None:
                out[label] = p
        return out

    def _fresh_prices(self, asset: str) -> dict[str, Decimal]:
        out: dict[str, Decimal] = {}
        for label, src in self._sources.items():
            p = src.get_price(asset)
            if p is None:
                continue
            if src.get_age_us(asset) > self._staleness_us:
                continue
            out[label] = p
        return out


def make_basket_fetcher(basket: BasketWSReference, rest_fallback=None):
    """Return a `reference_fetcher(asset) -> Decimal | None` for the
    LiveDataCoordinator.

    Falls back to `rest_fallback` when no venues are fresh."""
    def _fetch(asset: str) -> Decimal | None:
        price = basket.get_price(asset)
        if price is not None:
            return price
        if rest_fallback is not None:
            return rest_fallback(asset)
        return None
    return _fetch
