"""Pull Coinbase trade tape for a time window → `coinbase_trades`.

Coinbase Exchange's `GET /products/{X}/trades` returns 100 most-recent
trades per page, **sorted newest-first**, with a `trade_id` cursor via
`?before=<trade_id>` for older pages. We walk backward until we pass the
target start timestamp, writing each trade into SQLite.

Timestamps are millisecond-precision (Coinbase uses `2026-04-20T12:59:45.428002Z`).

Usage:
    python3.11 scripts/coinbase_trades_pull.py \\
        --asset btc --start 2026-04-20T12:45:00Z --end 2026-04-20T13:00:00Z

    # For a specific 15-min Kalshi window (e.g., KXBTC15M-26APR200900-00
    # covers 12:45-13:00 UTC → 8:45-9:00 AM EDT):
    python3.11 scripts/coinbase_trades_pull.py --asset btc --window-minutes 15 \\
        --end 2026-04-20T13:00:00Z
"""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests


logger = logging.getLogger(__name__)


PRODUCT_BY_ASSET = {
    "btc":"BTC-USD","eth":"ETH-USD","sol":"SOL-USD",
    "xrp":"XRP-USD","doge":"DOGE-USD","bnb":"BNB-USD","hype":"HYPE-USD",
}
TRADES_URL = "https://api.exchange.coinbase.com/products/{product}/trades"


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z","+00:00")).astimezone(timezone.utc)


def iso_to_us(s: str) -> int:
    return int(parse_iso(s).timestamp() * 1_000_000)


def open_connection(url: str) -> tuple[sqlite3.Connection, bool]:
    parsed = urlparse(url)
    if parsed.scheme in ("sqlite",""):
        raw = parsed.path or url.removeprefix("sqlite://")
        if raw.startswith("//"):
            path = Path(raw[1:])
        elif raw.startswith("/"):
            path = Path(raw.lstrip("/"))
        else:
            path = Path(raw)
        conn = sqlite3.connect(str(path), timeout=60.0, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=60000")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn, False
    raise ValueError(f"sqlite URLs only: {url!r}")


def pull_coinbase_trades(
    *, asset: str, start_dt: datetime, end_dt: datetime,
    session: requests.Session | None = None,
    conn: sqlite3.Connection | None = None,
) -> int:
    product = PRODUCT_BY_ASSET.get(asset.lower())
    if not product: raise ValueError(f"bad asset {asset!r}")
    session = session or requests.Session()

    start_ts_us = int(start_dt.timestamp() * 1_000_000)
    end_ts_us = int(end_dt.timestamp() * 1_000_000)
    url = TRADES_URL.format(product=product)

    # Coinbase pagination gotcha: `before=X` returns trades **newer** than
    # trade_id X; `after=X` returns trades **older** than X. Counterintuitive.
    # To walk back in time we use `after=<oldest_seen_id>`.
    params: dict[str, Any] = {"limit": 100}
    total = 0
    oldest_trade_id = None
    stopped_reason = ""

    for _ in range(10_000):  # hard cap
        try:
            resp = session.get(url, params=params, timeout=10.0)
        except requests.RequestException as e:
            logger.warning("%s: transport %s — retry", asset, e); time.sleep(1.0); continue
        if resp.status_code == 429:
            logger.info("%s: 429 — back off", asset); time.sleep(2.0); continue
        if resp.status_code != 200:
            logger.warning("%s: %d %s", asset, resp.status_code, resp.text[:200])
            stopped_reason = f"status {resp.status_code}"
            break

        trades = resp.json() if resp.content else []
        if not isinstance(trades, list) or not trades:
            stopped_reason = "empty page"; break

        # Coinbase `trade_id` is monotonically increasing. `time` is ISO ms.
        page_oldest_ts = trades[-1].get("time", "")
        for t in trades:
            ts_us = iso_to_us(t.get("time", ""))
            if ts_us > end_ts_us: continue  # newer than our end — skip
            if ts_us < start_ts_us:
                stopped_reason = "past start"
                # done; we've walked back past the requested start.
                # Commit any partial-batch rows we added for this page.
                break
            if conn is not None:
                try:
                    conn.execute(
                        "INSERT INTO coinbase_trades "
                        "(asset, ts_us, price, size, side, trade_id) "
                        "VALUES (?,?,?,?,?,?)",
                        (asset.lower(), ts_us, str(t.get("price","")),
                         str(t.get("size","")), (t.get("side") or "").lower(),
                         int(t.get("trade_id", 0) or 0)),
                    )
                except sqlite3.IntegrityError:
                    # duplicate (asset, trade_id) — ignore.
                    pass
            total += 1

        # Check stop condition AFTER inserting the full page: if we saw any
        # trade older than start_ts_us, we're done.
        if stopped_reason == "past start":
            break

        # Paginate older: find minimum trade_id in page, then use `after=`.
        try:
            oldest_trade_id = min(int(t.get("trade_id", 0) or 0) for t in trades)
        except (TypeError, ValueError):
            stopped_reason = "no trade_id for cursor"; break
        if oldest_trade_id <= 0:
            stopped_reason = "no trade_id for cursor"; break
        if total and total % 1000 == 0:
            # Periodic progress log — at 1k trades per ~10 pages, this fires
            # every few seconds.
            logger.info("  … %s: %d trades pulled, oldest seen: %s",
                        asset, total, trades[-1].get("time", "?"))
        params = {"limit": 100, "after": oldest_trade_id}
        time.sleep(0.05)
    else:
        stopped_reason = "max iterations"

    if conn is not None:
        conn.commit()
    logger.info("%s: %d trades [%s → %s] (stopped: %s)",
                asset, total, start_dt.isoformat(), end_dt.isoformat(), stopped_reason)
    return total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pull Coinbase trades over a window.")
    parser.add_argument("--asset", default="btc",
                        choices=tuple(sorted(PRODUCT_BY_ASSET.keys())))
    parser.add_argument("--start", help="ISO UTC, e.g. 2026-04-20T12:45:00Z")
    parser.add_argument("--end", help="ISO UTC, e.g. 2026-04-20T13:00:00Z")
    parser.add_argument("--window-minutes", type=int, default=None,
                        help="Shortcut: use --end and walk back N minutes.")
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if not args.end:
        parser.error("--end is required")
    end_dt = parse_iso(args.end)
    if args.window_minutes:
        start_dt = end_dt - timedelta(minutes=args.window_minutes)
    elif args.start:
        start_dt = parse_iso(args.start)
    else:
        parser.error("--start or --window-minutes required")

    url = (args.database_url or os.environ.get("DATABASE_URL") or "sqlite:///data/kalshi.db")
    conn = None if args.dry_run else open_connection(url)[0]

    try:
        pull_coinbase_trades(asset=args.asset,
                             start_dt=start_dt, end_dt=end_dt,
                             conn=conn)
    finally:
        if conn is not None: conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
