"""Kraken WebSocket ticker feed.

Subscribes to `ticker` channel on `wss://ws.kraken.com/v2` for USD pairs.
Parallel implementation to `coinbase_ws.CoinbaseWSReference` with the same
thread-safe latest-price cache semantics.

Kraken v2 message shape:
    {"channel": "ticker",
     "data": [{"symbol": "BTC/USD", "last": 76078.9, "bid": ..., "ask": ...}]}

For the BasketWSReference, Kraken matters most on BNB (Coinbase BNB is
low-volume and not a CF constituent). HYPE is Coinbase-only; not on Kraken.

Usage:
    ws = KrakenWSReference(assets=("btc", "bnb"))
    ws.start()
    ws.get_price("bnb")  # Decimal | None
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

import ops_events


logger = logging.getLogger(__name__)


WS_URL = "wss://ws.kraken.com/v2"

# Kraken uses "BTC/USD", "ETH/USD", "DOGE/USD" style in v2. No HYPE.
PAIR_BY_ASSET = {
    "btc":  "BTC/USD",
    "eth":  "ETH/USD",
    "sol":  "SOL/USD",
    "xrp":  "XRP/USD",
    "doge": "DOGE/USD",
    "bnb":  "BNB/USD",
    # hype: not available on Kraken as of 2026-04-20
}
ASSET_BY_PAIR = {v: k for k, v in PAIR_BY_ASSET.items()}


class KrakenWSReference:
    """Background-thread WS subscriber → latest-price cache."""

    def __init__(
        self,
        assets: tuple[str, ...] = tuple(PAIR_BY_ASSET),
        *,
        ws_url: str = WS_URL,
        reconnect_initial_s: float = 1.0,
        reconnect_max_s: float = 30.0,
        connect_factory: Callable | None = None,
    ) -> None:
        self._assets = tuple(a for a in assets if a in PAIR_BY_ASSET)
        self._ws_url = ws_url
        self._reconnect_initial = reconnect_initial_s
        self._reconnect_max = reconnect_max_s
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
            target=self._run, name="kraken-ws", daemon=True,
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
        except Exception:  # noqa: BLE001
            logger.exception("kraken WS loop crashed")

    async def _main_loop(self) -> None:
        backoff = self._reconnect_initial
        while not self._stop_event.is_set():
            try:
                await self._run_once()
                backoff = self._reconnect_initial
            except Exception as e:  # noqa: BLE001
                logger.warning("kraken WS error: %s — reconnecting in %.1fs",
                               e, backoff)
                ops_events.emit(
                    "kraken_ws", "warn",
                    f"WS disconnected — reconnecting in {backoff:.1f}s",
                    {"error": str(e), "backoff_s": backoff},
                )
                self._connected = False
                if self._stop_event.is_set():
                    break
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._reconnect_max)

    async def _run_once(self) -> None:
        factory = self._connect_factory or _default_connect
        pairs = [PAIR_BY_ASSET[a] for a in self._assets]
        sub = {
            "method": "subscribe",
            "params": {"channel": "ticker", "symbol": pairs},
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
        if data.get("channel") != "ticker":
            return
        items = data.get("data") or []
        if not isinstance(items, list):
            return
        ts_us = int(time.time() * 1_000_000)
        with self._lock:
            for item in items:
                if not isinstance(item, dict):
                    continue
                pair = item.get("symbol")
                asset = ASSET_BY_PAIR.get(pair)
                if not asset:
                    continue
                raw_price = item.get("last") if item.get("last") is not None \
                    else item.get("bid")
                if raw_price is None:
                    continue
                try:
                    price = Decimal(str(raw_price))
                except Exception:  # noqa: BLE001
                    continue
                self._latest[asset] = (price, ts_us)


async def _default_connect(url: str, sub: dict, stop_event: threading.Event):
    import websockets
    async with websockets.connect(url, ping_interval=30, ping_timeout=15) as ws:
        await ws.send(json.dumps(sub))
        async for msg in ws:
            if stop_event.is_set():
                break
            yield msg
