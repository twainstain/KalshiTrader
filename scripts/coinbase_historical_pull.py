"""Backfill `reference_ticks` from Coinbase 1-minute candlesticks.

Lets the Phase-1 backtest score historical Kalshi markets without having
to run the reference daemon forward in wall-clock time. Minimum Coinbase
candlestick granularity is 60 seconds — that's coarser than the real-time
index the P2 scanner will eventually use, but sufficient for coarse lag
and calibration analysis in the feasibility report.

For each asset we paginate 300-candle windows of Coinbase
`/products/{product}/candles?granularity=60` across the target range and
write one `reference_tick` per candle using the candle's close price.

Usage:
    # Range inferred from kalshi_historical_markets rows.
    python3.11 scripts/coinbase_historical_pull.py --asset all

    # Explicit override.
    python3.11 scripts/coinbase_historical_pull.py --asset btc \\
        --start 2026-04-18T00:00:00Z --end 2026-04-20T00:00:00Z
"""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

import requests

_THIS = Path(__file__).resolve()
_REPO = _THIS.parent.parent
for rel in ("src",):
    p = _REPO / rel
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from market.crypto_reference import (  # noqa: E402
    ReferenceTick,
    insert_tick,
    insert_tick_postgres,
)


logger = logging.getLogger(__name__)


PRODUCT_BY_ASSET = {
    "btc": "BTC-USD",
    "eth": "ETH-USD",
    "sol": "SOL-USD",
}
CANDLES_URL = "https://api.exchange.coinbase.com/products/{product}/candles"
MAX_CANDLES_PER_REQUEST = 300
DEFAULT_GRANULARITY_S = 60


# ---------------------------------------------------------------------------
# Range inference
# ---------------------------------------------------------------------------

def infer_range_from_kalshi(
    conn: Any, asset: str,
) -> tuple[int, int] | None:
    """Return (min_close_ts, max_close_ts) across markets for the given asset."""
    series_prefix = {
        "btc": "KXBTC", "eth": "KXETH", "sol": "KXSOL",
    }.get(asset.lower())
    if not series_prefix:
        return None
    cur = conn.cursor()
    cur.execute(
        "SELECT MIN(close_ts), MAX(close_ts) FROM kalshi_historical_markets "
        "WHERE series_ticker LIKE ? AND close_ts > 0",
        (f"{series_prefix}%",),
    )
    row = cur.fetchone()
    if not row or row[0] is None:
        return None
    return int(row[0]), int(row[1])


# ---------------------------------------------------------------------------
# Coinbase fetch + normalize
# ---------------------------------------------------------------------------

def _iso(ts_s: int) -> str:
    return datetime.fromtimestamp(ts_s, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def fetch_candles(
    *, product: str, start_ts: int, end_ts: int,
    granularity_s: int = DEFAULT_GRANULARITY_S,
    session: requests.Session | None = None,
    timeout_s: float = 10.0,
) -> list[tuple[int, Decimal]]:
    """Return (ts_s, close_price) pairs between start_ts and end_ts.

    Paginates in 300-candle chunks. Coinbase returns candles in descending
    time order; we resort ascending before returning.
    """
    session = session or requests.Session()
    all_points: list[tuple[int, Decimal]] = []
    window_s = MAX_CANDLES_PER_REQUEST * granularity_s
    cursor_end = end_ts
    while cursor_end > start_ts:
        chunk_start = max(start_ts, cursor_end - window_s)
        params = {
            "start": _iso(chunk_start),
            "end": _iso(cursor_end),
            "granularity": granularity_s,
        }
        url = CANDLES_URL.format(product=product)
        try:
            resp = session.get(url, params=params, timeout=timeout_s)
        except requests.RequestException as e:
            logger.warning("coinbase %s request error: %s — retrying once", product, e)
            time.sleep(1.0)
            resp = session.get(url, params=params, timeout=timeout_s)
        if resp.status_code == 429:
            logger.info("coinbase rate limit — sleeping 2s")
            time.sleep(2.0)
            resp = session.get(url, params=params, timeout=timeout_s)
        if resp.status_code != 200:
            logger.warning("coinbase %s %d: %s",
                           product, resp.status_code, resp.text[:200])
            break
        data = resp.json()
        if not isinstance(data, list) or not data:
            break
        for row in data:
            # [timestamp, low, high, open, close, volume]
            if not isinstance(row, list) or len(row) < 5:
                continue
            ts_s = int(row[0])
            close = Decimal(str(row[4]))
            all_points.append((ts_s, close))
        # Coinbase returns DESC; step backwards past the oldest candle we saw.
        oldest_ts_s = min(r[0] for r in data if isinstance(r, list) and r)
        # Advance strictly past the oldest seen. Coinbase sometimes returns
        # fewer than MAX_CANDLES_PER_REQUEST even when more data exists in
        # earlier windows, so `len(data) < MAX` is NOT a valid stop signal.
        next_cursor_end = oldest_ts_s - 1
        if next_cursor_end >= cursor_end:
            break  # no progress — avoid infinite loop
        cursor_end = next_cursor_end
    all_points.sort(key=lambda t: t[0])
    # Dedupe (chunks can overlap by one candle).
    seen = set()
    deduped: list[tuple[int, Decimal]] = []
    for ts, price in all_points:
        if ts in seen:
            continue
        seen.add(ts)
        deduped.append((ts, price))
    return deduped


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _open_connection(url: str) -> tuple[Any, bool]:
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


def write_points(
    conn: Any, *, asset: str, points: Iterable[tuple[int, Decimal]],
    is_postgres: bool,
) -> int:
    """Write (ts_s, price) pairs as reference_ticks rows. Returns count."""
    n = 0
    for ts_s, price in points:
        tick = ReferenceTick(
            asset=asset.lower(),
            ts_us=int(ts_s) * 1_000_000,
            price=price,
            src="coinbase_1m_historical",
        )
        if is_postgres:
            insert_tick_postgres(conn, tick)
        else:
            insert_tick(conn, tick)
        n += 1
    conn.commit()
    return n


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def pull_asset(
    *, asset: str, conn: Any, is_postgres: bool,
    start_ts: int | None, end_ts: int | None,
    session: requests.Session | None = None,
) -> int:
    product = PRODUCT_BY_ASSET.get(asset.lower())
    if not product:
        logger.warning("unknown asset %r — skipping", asset)
        return 0
    if start_ts is None or end_ts is None:
        rng = infer_range_from_kalshi(conn, asset)
        if rng is None:
            logger.info("no Kalshi markets found for %s — skipping", asset)
            return 0
        start_ts, end_ts = rng
        # pad 60s so the 60s rolling avg has coverage at the window boundary.
        start_ts -= 90
        end_ts += 60
    logger.info("pulling %s candles [%s … %s]", product, _iso(start_ts), _iso(end_ts))
    points = fetch_candles(
        product=product, start_ts=start_ts, end_ts=end_ts, session=session,
    )
    n = write_points(conn, asset=asset, points=points, is_postgres=is_postgres)
    logger.info("%s: %d reference_ticks written", asset, n)
    return n


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill reference_ticks from Coinbase.")
    parser.add_argument("--asset", default="all",
                        choices=("btc", "eth", "sol", "all"))
    parser.add_argument("--start", default=None,
                        help="ISO start (e.g. 2026-04-18T00:00:00Z). Overrides auto-range.")
    parser.add_argument("--end", default=None,
                        help="ISO end. Overrides auto-range.")
    parser.add_argument("--database-url", default=None)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    def _parse_iso(s: str | None) -> int | None:
        if not s:
            return None
        return int(datetime.fromisoformat(s.replace("Z", "+00:00"))
                   .astimezone(timezone.utc).timestamp())

    start_ts = _parse_iso(args.start)
    end_ts = _parse_iso(args.end)

    url = (args.database_url or os.environ.get("DATABASE_URL")
           or "sqlite:///data/kalshi.db")
    conn, is_pg = _open_connection(url)
    try:
        assets = ("btc", "eth", "sol") if args.asset == "all" else (args.asset,)
        total = 0
        for a in assets:
            total += pull_asset(
                asset=a, conn=conn, is_postgres=is_pg,
                start_ts=start_ts, end_ts=end_ts,
            )
        logger.info("done: %d total reference_ticks written", total)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
