"""Coinbase Exchange WebSocket ticker feed.

Subscribes to `ticker` channel on `wss://ws-feed.exchange.coinbase.com`
for one or more USD product_ids. Maintains a thread-safe `{asset: (price, ts_us)}`
latest-price map. `get_price(asset)` returns the freshest price.

Addresses Finding #2 from `docs/kalshi_shadow_live_capture_results.md` —
in the final 60s of a Kalshi window, 2-second REST polling misses the
intra-minute moves that the Kalshi book has already incorporated. WS
ticker updates on every trade (ms-precision).

Usage:
    ws = CoinbaseWSReference(assets=("btc","eth","sol","xrp","doge","bnb","hype"))
    ws.start()
    # ... elsewhere, in any thread:
    price = ws.get_price("btc")  # Decimal | None
    age_us = ws.get_age_us("btc")  # int (microseconds since last update)
    ws.stop()
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from decimal import Decimal
from typing import Callable


logger = logging.getLogger(__name__)


WS_URL = "wss://ws-feed.exchange.coinbase.com"

PRODUCT_BY_ASSET = {
    "btc":  "BTC-USD",
    "eth":  "ETH-USD",
    "sol":  "SOL-USD",
    "xrp":  "XRP-USD",
    "doge": "DOGE-USD",
    "bnb":  "BNB-USD",
    "hype": "HYPE-USD",
}
ASSET_BY_PRODUCT = {v: k for k, v in PRODUCT_BY_ASSET.items()}


class CoinbaseWSReference:
    """Background-thread WS subscriber → latest-price cache.

    Auto-reconnects with exponential backoff on disconnect. Every `ticker`
    message updates the cached price + timestamp for the corresponding
    asset. Readers use `get_price(asset)` / `get_age_us(asset)` from any
    thread.
    """

    def __init__(
        self,
        assets: tuple[str, ...] = tuple(PRODUCT_BY_ASSET),
        *,
        ws_url: str = WS_URL,
        reconnect_initial_s: float = 1.0,
        reconnect_max_s: float = 30.0,
        connect_factory: Callable | None = None,
    ) -> None:
        # Filter assets to ones we know how to subscribe to.
        self._assets = tuple(a for a in assets if a in PRODUCT_BY_ASSET)
        self._ws_url = ws_url
        self._reconnect_initial = reconnect_initial_s
        self._reconnect_max = reconnect_max_s
        # Seam for tests — a coroutine builder `async def f(url) -> AsyncIterator[str]`.
        self._connect_factory = connect_factory

        self._latest: dict[str, tuple[Decimal, int]] = {}
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._connected = False

    # ---- lifecycle ----

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="coinbase-ws", daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    # ---- read surface ----

    def get_price(self, asset: str) -> Decimal | None:
        with self._lock:
            rec = self._latest.get(asset.lower())
        return rec[0] if rec else None

    def get_age_us(self, asset: str) -> int:
        """Microseconds since the last update for `asset`, or `2**63-1`
        if we've never seen one."""
        with self._lock:
            rec = self._latest.get(asset.lower())
        if rec is None:
            return 2**63 - 1
        return int(time.time() * 1_000_000) - rec[1]

    def snapshot(self) -> dict[str, tuple[Decimal, int]]:
        with self._lock:
            return dict(self._latest)

    # ---- internals ----

    def _run(self) -> None:
        try:
            asyncio.run(self._main_loop())
        except Exception:  # noqa: BLE001 — thread must not die silently
            logger.exception("coinbase WS loop crashed")

    async def _main_loop(self) -> None:
        backoff = self._reconnect_initial
        while not self._stop_event.is_set():
            try:
                await self._run_once()
                backoff = self._reconnect_initial  # reset on clean exit
            except Exception as e:  # noqa: BLE001
                logger.warning("coinbase WS error: %s — reconnecting in %.1fs",
                               e, backoff)
                self._connected = False
                if self._stop_event.is_set():
                    break
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._reconnect_max)

    async def _run_once(self) -> None:
        """One connect → subscribe → read-loop cycle."""
        factory = self._connect_factory or _default_connect
        products = [PRODUCT_BY_ASSET[a] for a in self._assets]
        sub = {
            "type": "subscribe",
            "product_ids": products,
            "channels": ["ticker"],
        }
        async for msg in factory(self._ws_url, sub, self._stop_event):
            self._connected = True
            self._ingest(msg)
            if self._stop_event.is_set():
                break

    def _ingest(self, msg: str | bytes) -> None:
        try:
            data = json.loads(msg) if isinstance(msg, (str, bytes, bytearray)) else msg
        except ValueError:
            return
        if data.get("type") != "ticker":
            return
        product = data.get("product_id")
        asset = ASSET_BY_PRODUCT.get(product)
        if not asset:
            return
        price_str = data.get("price")
        if price_str is None:
            return
        try:
            price = Decimal(str(price_str))
        except Exception:  # noqa: BLE001
            return
        ts_us = int(time.time() * 1_000_000)
        with self._lock:
            self._latest[asset] = (price, ts_us)


async def _default_connect(url: str, sub: dict, stop_event: threading.Event):
    """Production WS driver — one subscribe + yield every message until stop.

    Returns an async generator that yields raw messages. On disconnect the
    generator exits naturally and the outer loop reconnects.
    """
    import websockets  # deferred import so test mode doesn't need the dep active
    async with websockets.connect(url, ping_interval=30, ping_timeout=15) as ws:
        await ws.send(json.dumps(sub))
        async for msg in ws:
            if stop_event.is_set():
                break
            yield msg


# ---------------------------------------------------------------------------
# A simple `reference_fetcher` shim for `run_kalshi_shadow.LiveDataCoordinator`.
# ---------------------------------------------------------------------------

def make_ws_reference_fetcher(
    ws: CoinbaseWSReference,
    *,
    staleness_threshold_us: int = 5_000_000,   # 5 s
    rest_fallback: Callable[[str], Decimal | None] | None = None,
) -> Callable[[str], Decimal | None]:
    """Return a fetcher compatible with `LiveDataCoordinator`.

    Prefers the WS cache when fresh. Falls back to `rest_fallback` if:
    - the WS has no price for this asset yet, OR
    - the last WS update is older than `staleness_threshold_us`.
    """
    def _fetch(asset: str) -> Decimal | None:
        price = ws.get_price(asset)
        if price is not None and ws.get_age_us(asset) <= staleness_threshold_us:
            return price
        if rest_fallback is not None:
            return rest_fallback(asset)
        return None
    return _fetch
