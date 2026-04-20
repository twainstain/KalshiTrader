"""Pull Kalshi trade tape for specified markets → `kalshi_historical_trades`.

Hits the **public** `GET /trade-api/v2/markets/trades?ticker=X` endpoint
(no auth) with cursor pagination. Microsecond-precision `created_time`
per trade is parsed into `ts_us`.

Usage:
    # Specific tickers (full tape):
    python3.11 scripts/kalshi_trades_pull.py --ticker KXBTC15M-26APR200900-00

    # All settled markets already in kalshi_historical_markets, series-filtered:
    python3.11 scripts/kalshi_trades_pull.py --from-db --asset btc --limit 5

    # Dry-run (count + first/last trade timestamp per market):
    python3.11 scripts/kalshi_trades_pull.py --ticker KXBTC15M-...-00 --dry-run
"""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

import requests


logger = logging.getLogger(__name__)


HOST = "https://api.elections.kalshi.com/trade-api/v2"


def iso_to_us(s: str) -> int:
    if not s:
        return 0
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return int(dt.astimezone(timezone.utc).timestamp() * 1_000_000)


def open_connection(url: str) -> sqlite3.Connection:
    parsed = urlparse(url)
    if parsed.scheme in ("sqlite", ""):
        raw = parsed.path or url.removeprefix("sqlite://")
        if raw.startswith("//"):
            path = Path(raw[1:])
        elif raw.startswith("/"):
            path = Path(raw.lstrip("/"))
        else:
            path = Path(raw)
        conn = sqlite3.connect(str(path), timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn
    raise ValueError(f"sqlite URLs only: {url!r}")


def paginate_trades(
    ticker: str, *, page_size: int = 200, pause_s: float = 0.05,
    session: requests.Session | None = None,
) -> Iterable[dict]:
    session = session or requests.Session()
    params: dict[str, Any] = {"ticker": ticker, "limit": page_size}
    while True:
        try:
            resp = session.get(f"{HOST}/markets/trades", params=params, timeout=15.0)
        except requests.RequestException as e:
            logger.warning("%s: transport error %s — retrying", ticker, e)
            time.sleep(1.0); continue
        if resp.status_code == 429:
            logger.info("%s: 429 — backing off", ticker)
            time.sleep(2.0); continue
        if resp.status_code != 200:
            logger.warning("%s: %d %s", ticker, resp.status_code, resp.text[:200])
            break
        data = resp.json() if resp.content else {}
        for t in data.get("trades", []) or []:
            yield t
        cursor = data.get("cursor")
        if not cursor: break
        params["cursor"] = cursor
        if pause_s: time.sleep(pause_s)


def insert_trade(conn: sqlite3.Connection, t: dict) -> None:
    ticker = t.get("ticker", "")
    if not ticker: return
    ts_us = iso_to_us(t.get("created_time", ""))
    price = t.get("yes_price_dollars", "")
    qty = t.get("count_fp", "")
    side = (t.get("taker_side", "") or "").lower()
    conn.execute(
        "INSERT INTO kalshi_historical_trades (market_ticker, ts_us, price, qty, taker_side) "
        "VALUES (?, ?, ?, ?, ?)",
        (ticker, ts_us, str(price), str(qty), side),
    )


def pull_one(conn: sqlite3.Connection, ticker: str, *,
             dry_run: bool = False) -> int:
    n = 0
    earliest = None
    latest = None
    for t in paginate_trades(ticker):
        n += 1
        ts = t.get("created_time")
        if earliest is None or (ts and ts < earliest): earliest = ts
        if latest is None or (ts and ts > latest): latest = ts
        if not dry_run:
            insert_trade(conn, t)
    if not dry_run:
        conn.commit()
    logger.info("  %s: %d trades [%s … %s]", ticker, n, earliest or "-", latest or "-")
    return n


def tickers_from_db(conn: sqlite3.Connection, *, asset: str, limit: int | None) -> list[str]:
    series_prefix = {"btc":"KXBTC15M","eth":"KXETH15M","sol":"KXSOL15M"}.get(asset.lower())
    if not series_prefix:
        raise ValueError(f"bad asset {asset!r}")
    q = (
        "SELECT market_ticker FROM kalshi_historical_markets "
        "WHERE series_ticker = ? AND settled_result IN ('yes','no') "
        "ORDER BY close_ts DESC"
    )
    params: tuple[Any, ...] = (series_prefix,)
    if limit:
        q += " LIMIT ?"
        params = (series_prefix, limit)
    return [r[0] for r in conn.execute(q, params).fetchall()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pull Kalshi trades (public).")
    parser.add_argument("--ticker", action="append", default=[],
                        help="Specific market ticker(s) — can repeat.")
    parser.add_argument("--from-db", action="store_true",
                        help="Pull all settled markets of --asset from kalshi_historical_markets.")
    parser.add_argument("--asset", default="btc",
                        choices=("btc","eth","sol","xrp","doge","bnb","hype"))
    parser.add_argument("--limit", type=int, default=None,
                        help="Cap --from-db tickers (smoke test).")
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--dry-run", action="store_true")
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

    url = (args.database_url or os.environ.get("DATABASE_URL") or "sqlite:///data/kalshi.db")
    conn = open_connection(url)

    try:
        tickers = list(args.ticker)
        if args.from_db:
            tickers += tickers_from_db(conn, asset=args.asset, limit=args.limit)
        if not tickers:
            logger.error("no tickers supplied — pass --ticker or --from-db")
            return 2
        logger.info("pulling %d market(s)", len(tickers))
        total = 0
        for ticker in tickers:
            total += pull_one(conn, ticker, dry_run=args.dry_run)
        logger.info("done: %d trades across %d markets", total, len(tickers))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
