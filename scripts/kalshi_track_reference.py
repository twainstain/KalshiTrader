"""Continuous reference-tick capture daemon (P1-M2-T04).

Polls Coinbase's public ticker API once per second per asset and writes
rows into `reference_ticks`. Simple polling for Phase 1 — good enough to
measure CF-Benchmarks-proxy-vs-Kalshi lag at second resolution. A WS-based
multi-exchange upgrade is a P2-M1 prerequisite when we care about
sub-second freshness.

Usage:
    python3.11 scripts/kalshi_track_reference.py --asset btc
    python3.11 scripts/kalshi_track_reference.py --asset all --interval 1.0
    python3.11 scripts/kalshi_track_reference.py --asset btc --iterations 10  # smoke test

Graceful shutdown: SIGINT / SIGTERM. Errors per tick are logged and
skipped — the loop doesn't die on transient REST 5xx. Reuses
`BasketReferenceSource.record_tick()` to keep aggregation logic in one place.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sqlite3
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

_THIS = Path(__file__).resolve()
_REPO = _THIS.parent.parent
for rel in ("src",):
    p = _REPO / rel
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from market.crypto_reference import (  # noqa: E402
    BasketReferenceSource,
    ReferenceTick,
    insert_tick,
    insert_tick_postgres,
)


logger = logging.getLogger(__name__)


COINBASE_TICKER_URL = "https://api.exchange.coinbase.com/products/{product}/ticker"

PRODUCT_BY_ASSET: dict[str, str] = {
    "btc": "BTC-USD",
    "eth": "ETH-USD",
    "sol": "SOL-USD",
}


def fetch_coinbase_tick(asset: str, *, session: requests.Session,
                       timeout_s: float = 3.0) -> ReferenceTick | None:
    """Fetch one tick from Coinbase. Returns `None` on any error (logged)."""
    product = PRODUCT_BY_ASSET.get(asset.lower())
    if not product:
        return None
    try:
        resp = session.get(
            COINBASE_TICKER_URL.format(product=product), timeout=timeout_s,
        )
    except requests.RequestException as e:
        logger.warning("coinbase %s request error: %s", asset, e)
        return None
    if resp.status_code != 200:
        logger.warning("coinbase %s %d: %s", asset, resp.status_code,
                       resp.text[:200])
        return None
    try:
        data = resp.json()
    except ValueError:
        logger.warning("coinbase %s non-json body", asset)
        return None
    price = data.get("price")
    if price is None:
        return None
    return ReferenceTick(
        asset=asset.lower(),
        price=Decimal(str(price)),
        ts_us=int(time.time() * 1_000_000),
        src="coinbase",
    )


def _open_connection(url: str) -> tuple[Any, bool]:
    """Return (conn, is_postgres)."""
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


def run(
    *,
    assets: tuple[str, ...],
    interval_s: float,
    iterations: int | None,
    conn: Any | None,
    is_postgres: bool,
    source: BasketReferenceSource | None = None,
    session: requests.Session | None = None,
) -> int:
    """Main poll loop. Returns number of ticks written."""
    source = source or BasketReferenceSource(assets=assets)
    source.start()
    session = session or requests.Session()

    stop = {"flag": False}

    def handle_signal(signum, _frame):
        logger.info("received signal %d — stopping after current tick", signum)
        stop["flag"] = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handle_signal)
        except ValueError:
            # Ignored when run from a non-main thread (e.g. pytest).
            pass

    written = 0
    i = 0
    while not stop["flag"]:
        for asset in assets:
            tick = fetch_coinbase_tick(asset, session=session)
            if tick is None:
                continue
            source.record_tick(tick)
            if conn is not None:
                try:
                    if is_postgres:
                        insert_tick_postgres(conn, tick)
                    else:
                        insert_tick(conn, tick)
                    conn.commit()
                except Exception as e:  # noqa: BLE001 — persistence is best-effort
                    logger.warning("persist tick for %s failed: %s", asset, e)
            written += 1
        i += 1
        if iterations is not None and i >= iterations:
            break
        if not stop["flag"]:
            time.sleep(interval_s)

    source.stop()
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Continuous reference-tick capture.")
    parser.add_argument("--asset", default="all",
                        choices=("btc", "eth", "sol", "all"))
    parser.add_argument("--interval", type=float, default=1.0,
                        help="Seconds between polls (default 1.0).")
    parser.add_argument("--iterations", type=int, default=None,
                        help="Stop after N poll rounds (default: run until SIGINT).")
    parser.add_argument("--database-url", default=None)
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

    if args.asset == "all":
        assets: tuple[str, ...] = ("btc", "eth", "sol")
    else:
        assets = (args.asset,)

    url = (args.database_url or os.environ.get("DATABASE_URL")
           or "sqlite:///data/kalshi.db")
    conn, is_pg = _open_connection(url)

    try:
        written = run(
            assets=assets, interval_s=args.interval,
            iterations=args.iterations, conn=conn, is_postgres=is_pg,
        )
    finally:
        conn.close()

    logger.info("exit: %d ticks written", written)
    return 0


if __name__ == "__main__":
    sys.exit(main())
