"""Phase-1 shadow-evaluator entrypoint (P1-M4-T05).

Long-running loop that periodically:
- Discovers active Kalshi crypto 15M markets via `/series` + `/markets`.
- Fetches L1 + L2 orderbook snapshots per ticker via
  `GET /markets/{ticker}/orderbook`.
- Polls Coinbase for the current BTC/ETH/SOL spot price.
- Scores each book via `KalshiFairValueStrategy`.
- Writes every approvable decision to `shadow_decisions`.
- Every N ticks, attempts to reconcile markets whose expiration has passed
  + `reconcile_delay_s` (default 30s) — reading their `result` from
  `GET /markets/{ticker}` and updating the realized columns.

**No orders are ever submitted.** No order-mutating SDK methods are
imported. Phase-1 is read-only by construction.

Usage:
    # One-shot smoke test — three ticks with no sleeping:
    PYTHONPATH=src python3.11 -m run_kalshi_shadow --iterations 3 --no-sleep

    # Production-style long-run (SIGINT to stop):
    PYTHONPATH=src python3.11 -m run_kalshi_shadow --interval-s 5
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sqlite3
import sys
import threading
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlparse


_THIS = Path(__file__).resolve()
if str(_THIS.parent) not in sys.path:
    sys.path.insert(0, str(_THIS.parent))

from core.models import MarketQuote  # noqa: E402
from execution.kalshi_shadow_evaluator import (  # noqa: E402
    KalshiShadowEvaluator,
    ShadowConfig,
)
from market.crypto_reference import BasketReferenceSource, ReferenceTick  # noqa: E402
from market.kalshi_market import (  # noqa: E402
    KalshiMarketConfig,
    KalshiMarketSource,
    book_to_market_quote,
)
from strategy.kalshi_fair_value import FairValueModel, KalshiFairValueStrategy  # noqa: E402


logger = logging.getLogger(__name__)


# Lifted from the strategy plan. Kept here (not in platform_adapters) so the
# entrypoint can be swapped for a Polymarket/etc. variant without dragging
# Kalshi-specific constants along.
ASSET_FROM_SERIES = {
    "KXBTC15M": "btc",
    "KXETH15M": "eth",
    "KXSOL15M": "sol",
}


# ---------------------------------------------------------------------------
# Live-data coordinator — pulls books + references per tick.
# Injectable for tests via `LiveDataCoordinator.build(...)`.
# ---------------------------------------------------------------------------

class LiveDataCoordinator:
    """Per-tick side-effect bundle: discover markets, snapshot books, poll ref.

    Separated from `KalshiShadowEvaluator` so the evaluator stays a pure
    quotes-in / decisions-out engine. The coordinator is the part that
    touches the network.
    """

    def __init__(
        self,
        *,
        rest_client: Any,
        reference_fetcher: Callable[[str], Decimal | None],
        market_source: KalshiMarketSource,
        reference_source: BasketReferenceSource,
        market_limit_per_series: int = 50,
    ) -> None:
        self._rest = rest_client
        self._fetch_reference = reference_fetcher
        self._market_source = market_source
        self._reference_source = reference_source
        self._market_limit = market_limit_per_series
        self._market_meta: dict[str, dict] = {}
        self._asset_by_ticker: dict[str, str] = {}
        self._fee_bps_by_ticker: dict[str, Decimal] = {}

    def discover(self) -> None:
        """Refresh the active-markets catalog. Called less often than per-tick."""
        for series, asset in ASSET_FROM_SERIES.items():
            try:
                resp = self._rest.request(
                    "GET", "/markets",
                    params={"series_ticker": series, "status": "active",
                            "limit": self._market_limit},
                    authenticated=False,
                )
            except Exception as e:  # noqa: BLE001 — log + keep going
                logger.warning("discover %s failed: %s", series, e)
                continue
            for m in (resp or {}).get("markets", []) or []:
                ticker = m.get("ticker")
                if not ticker:
                    continue
                from scripts_compat import parse_iso_or_epoch  # (inline below)
                self._market_meta[ticker] = {
                    "series_ticker": series,
                    "event_ticker": m.get("event_ticker", ""),
                    "strike": m.get("strike_price") or m.get("floor_strike") or 0,
                    "comparator": (m.get("strike_type") or "above").lower(),
                    "expiration_ts": parse_iso_or_epoch(
                        m.get("expiration_time") or m.get("close_time")
                    ),
                    "asset": asset,
                }
                self._asset_by_ticker[ticker] = asset

    def snapshot_books(self) -> None:
        """Pull the latest orderbook for every known ticker."""
        for ticker in list(self._market_meta.keys()):
            try:
                resp = self._rest.request(
                    "GET", f"/markets/{ticker}/orderbook",
                    authenticated=False,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("book fetch %s failed: %s", ticker, e)
                continue
            book = (resp or {}).get("orderbook") or {}
            self._market_source.apply_snapshot(ticker, book)
            status = (self._market_meta[ticker].get("status") or "active")
            exp = int(self._market_meta[ticker].get("expiration_ts") or 0)
            time_remaining = max(0, exp - int(time.time()))
            self._market_source.update_lifecycle(
                ticker, status=status, time_remaining_s=time_remaining,
            )

    def sample_reference(self) -> None:
        """Poll Coinbase once per asset and feed the basket source."""
        for asset in set(ASSET_FROM_SERIES.values()):
            price = self._fetch_reference(asset)
            if price is None:
                continue
            self._reference_source.record_tick(ReferenceTick(
                asset=asset, price=price, ts_us=int(time.time() * 1_000_000),
                src="coinbase_live",
            ))

    @property
    def market_meta(self) -> dict[str, dict]:
        return self._market_meta

    @property
    def asset_by_ticker(self) -> dict[str, str]:
        return self._asset_by_ticker

    @property
    def fee_bps_by_ticker(self) -> dict[str, Decimal]:
        return self._fee_bps_by_ticker


# Re-export a tiny helper used above without a circular-import dance.
class scripts_compat:  # noqa: N801 — deliberately lowercase to signal "proxy"
    @staticmethod
    def parse_iso_or_epoch(val: Any) -> int:
        if val is None or val == "":
            return 0
        if isinstance(val, (int, float)):
            return int(val)
        if isinstance(val, str):
            from datetime import datetime, timezone
            try:
                dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
                return int(dt.astimezone(timezone.utc).timestamp())
            except ValueError:
                try:
                    return int(float(val))
                except ValueError:
                    return 0
        return 0


# ---------------------------------------------------------------------------
# Reference fetcher — Coinbase /ticker. Separated for testability.
# ---------------------------------------------------------------------------

def default_coinbase_fetcher(asset: str) -> Decimal | None:
    """Default implementation — blocking HTTP. Replace with WS in P2."""
    import requests
    product = {"btc": "BTC-USD", "eth": "ETH-USD", "sol": "SOL-USD"}.get(asset.lower())
    if not product:
        return None
    try:
        resp = requests.get(
            f"https://api.exchange.coinbase.com/products/{product}/ticker",
            timeout=3.0,
        )
    except Exception:
        return None
    if resp.status_code != 200:
        return None
    try:
        p = resp.json().get("price")
    except ValueError:
        return None
    return Decimal(str(p)) if p is not None else None


# ---------------------------------------------------------------------------
# Resolution lookup — reads settled result via GET /markets/{ticker}
# ---------------------------------------------------------------------------

def build_resolution_lookup(rest_client: Any) -> Callable[[str], dict | None]:
    def _lookup(ticker: str) -> dict | None:
        try:
            resp = rest_client.request(
                "GET", f"/markets/{ticker}", authenticated=False,
            )
        except Exception:
            return None
        return (resp or {}).get("market") or resp
    return _lookup


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

def open_connection(url: str) -> tuple[Any, bool]:
    parsed = urlparse(url)
    if parsed.scheme in ("sqlite", ""):
        raw = parsed.path or url.removeprefix("sqlite://")
        if raw.startswith("//"):
            path = Path(raw[1:])
        elif raw.startswith("/"):
            path = Path(raw.lstrip("/"))
        else:
            path = Path(raw)
        path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(str(path)), False
    if parsed.scheme in ("postgres", "postgresql"):
        import psycopg2
        return psycopg2.connect(url), True
    raise ValueError(f"Unsupported DATABASE_URL scheme: {parsed.scheme!r}")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def build_evaluator(
    *, conn: Any, is_postgres: bool,
    rest_client: Any,
    reference_fetcher: Callable[[str], Decimal | None],
) -> tuple[KalshiShadowEvaluator, LiveDataCoordinator]:
    """Wire everything together. Not called from tests."""
    market_source = KalshiMarketSource(KalshiMarketConfig())
    reference_source = BasketReferenceSource(assets=tuple(set(ASSET_FROM_SERIES.values())))
    reference_source.start()
    strategy = KalshiFairValueStrategy(FairValueModel())

    coordinator = LiveDataCoordinator(
        rest_client=rest_client,
        reference_fetcher=reference_fetcher,
        market_source=market_source,
        reference_source=reference_source,
    )
    evaluator = KalshiShadowEvaluator(
        market_source=market_source,
        reference_source=reference_source,
        strategy=strategy,
        market_meta_by_ticker=coordinator.market_meta,  # shared reference
        asset_by_ticker=coordinator.asset_by_ticker,    # shared reference
        fee_bps_by_ticker=coordinator.fee_bps_by_ticker,
        conn=conn, is_postgres=is_postgres,
        resolution_lookup=build_resolution_lookup(rest_client),
        config=ShadowConfig(),
    )
    return evaluator, coordinator


def run_loop(
    *,
    evaluator: KalshiShadowEvaluator,
    coordinator: LiveDataCoordinator | None,
    iterations: int | None,
    interval_s: float,
    no_sleep: bool,
    stop_event: threading.Event | None = None,
    discover_every: int = 30,
) -> dict[str, int]:
    """Drive the evaluator. Returns per-tick aggregate counts."""
    stop = stop_event or threading.Event()

    def _handler(signum, _frame):
        logger.info("signal %d received — stopping after current tick", signum)
        stop.set()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handler)
        except ValueError:
            # pytest / threads — non-main thread can't install handlers.
            pass

    totals = {"ticks": 0, "written": 0, "reconciled": 0}
    i = 0
    while not stop.is_set():
        if coordinator is not None and (i % discover_every == 0):
            coordinator.discover()
        if coordinator is not None:
            coordinator.snapshot_books()
            coordinator.sample_reference()
        result = evaluator.tick()
        totals["written"] += result.get("written", 0)
        totals["reconciled"] += result.get("reconciled", 0)
        totals["ticks"] += 1
        i += 1
        if iterations is not None and i >= iterations:
            break
        if not no_sleep and not stop.is_set():
            stop.wait(interval_s)
    return totals


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase-1 shadow evaluator.")
    parser.add_argument("--iterations", type=int, default=None,
                        help="Stop after N ticks. Default: run until SIGINT.")
    parser.add_argument("--interval-s", type=float, default=2.0)
    parser.add_argument("--no-sleep", action="store_true",
                        help="Skip sleeping between ticks (smoke-test mode).")
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--dry-run", action="store_true",
                        help="Don't open a DB connection; skip persistence.")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    # Lazy import — avoids requiring the SDK in pure-unit-test runs.
    from kalshi_rest import KalshiRestClient
    rest_client = KalshiRestClient.from_env()

    url = (args.database_url or os.environ.get("DATABASE_URL")
           or "sqlite:///data/kalshi.db")
    conn = None
    is_pg = False
    if not args.dry_run:
        conn, is_pg = open_connection(url)

    evaluator, coordinator = build_evaluator(
        conn=conn, is_postgres=is_pg,
        rest_client=rest_client,
        reference_fetcher=default_coinbase_fetcher,
    )
    try:
        totals = run_loop(
            evaluator=evaluator, coordinator=coordinator,
            iterations=args.iterations, interval_s=args.interval_s,
            no_sleep=args.no_sleep,
        )
        logger.info("exit totals: %s", totals)
    finally:
        if conn is not None:
            conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
