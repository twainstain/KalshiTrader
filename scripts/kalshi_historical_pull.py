"""P1-M2 historical data pull — markets + trades → SQLite/Postgres.

Pulls N days of settled Kalshi crypto 15-min markets plus per-market trade
fills. Writes to `kalshi_historical_markets` and `kalshi_historical_trades`.
Idempotent — rerunning with overlapping windows upserts by primary key /
skips existing trade IDs.

Usage:
    python3.11 scripts/kalshi_historical_pull.py --days 30 --asset all
    python3.11 scripts/kalshi_historical_pull.py --days 7 --asset btc --verbose
    python3.11 scripts/kalshi_historical_pull.py --days 1 --asset btc --dry-run

The script uses `src/kalshi_api.KalshiAPIClient` (direct requests + RSA-PSS
signing) rather than `kalshi_python_sync`, whose pydantic models reject the
demo environment's nullable-int fields.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

# Resolve src/ imports when executed as `scripts/...py` from repo root.
_THIS = Path(__file__).resolve()
_REPO = _THIS.parent.parent
for rel in ("src", "scripts"):
    p = _REPO / rel
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from kalshi_api import KalshiAPIClient  # noqa: E402


logger = logging.getLogger(__name__)


SERIES_BY_ASSET: dict[str, tuple[str, ...]] = {
    "btc": ("KXBTC15M",),
    "eth": ("KXETH15M",),
    "sol": ("KXSOL15M",),
}


# Kalshi's `strike_type` values map onto our SUPPORTED_COMPARATORS so the
# FairValueModel can price them uniformly.
COMPARATOR_MAP: dict[str, str] = {
    "greater_or_equal": "at_least",
    "greater_than": "above",
    "less_or_equal": "below",
    "less_than": "below",
    "ge": "at_least",
    "gt": "above",
    "le": "below",
    "lt": "below",
    "above": "above",
    "below": "below",
    "between": "between",
    "exactly": "exactly",
    "at_least": "at_least",
}


def _series_for_asset(asset: str) -> tuple[str, ...]:
    asset = asset.lower()
    if asset == "all":
        return tuple(s for tup in SERIES_BY_ASSET.values() for s in tup)
    if asset in SERIES_BY_ASSET:
        return SERIES_BY_ASSET[asset]
    raise ValueError(f"unknown asset {asset!r} — choose btc, eth, sol, or all")


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

def _open_connection(url: str) -> Any:
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
        return sqlite3.connect(str(path))
    if parsed.scheme in ("postgres", "postgresql"):
        import psycopg2
        return psycopg2.connect(url)
    raise ValueError(f"Unsupported DATABASE_URL scheme: {parsed.scheme!r}")


def _to_epoch_s(value: Any) -> int:
    """Coerce a Kalshi time field to Unix epoch seconds.

    Responses give either an ISO-8601 string (`"2026-02-18T03:47:15Z"`) or a
    numeric epoch. Handle both, defaulting to 0 on anything unparseable so
    the pull doesn't die mid-page.
    """
    if value is None or value == "":
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        # Try ISO-8601 first.
        from datetime import datetime, timezone
        try:
            # fromisoformat handles `...Z` only in Python 3.11+.
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return int(dt.astimezone(timezone.utc).timestamp())
        except ValueError:
            try:
                return int(float(value))
            except ValueError:
                return 0
    return 0


def _derive_series_ticker(market: dict) -> str:
    """Best-effort series_ticker extraction.

    Kalshi's historical-markets response doesn't always populate
    `series_ticker` directly; the series is the `-`-prefixed namespace of
    the market ticker (e.g. `KXBTC15M-26FEB180000-00` → `KXBTC15M`).
    """
    direct = market.get("series_ticker") or market.get("series")
    if direct:
        return str(direct)
    ticker = market.get("ticker") or market.get("market_ticker") or ""
    return ticker.split("-", 1)[0] if "-" in ticker else ""


def upsert_market(conn: Any, market: dict) -> None:
    """Upsert one row into `kalshi_historical_markets` keyed on market_ticker."""
    ticker = market.get("ticker") or market.get("market_ticker")
    if not ticker:
        return
    # Strike field name varies: some responses give `strike_type` + `floor_strike`
    # + `cap_strike`; simpler "ABOVE X" markets give `strike_price`.
    strike = (
        market.get("strike_price")
        or market.get("floor_strike")
        or market.get("cap_strike")
        or 0
    )
    raw_comparator = (
        market.get("strike_type") or market.get("comparator") or "above"
    ).lower()
    comparator = COMPARATOR_MAP.get(raw_comparator, raw_comparator)
    row = (
        ticker,
        _derive_series_ticker(market),
        market.get("event_ticker", ""),
        str(strike),
        comparator,
        _to_epoch_s(market.get("open_time")),
        _to_epoch_s(market.get("close_time")),
        _to_epoch_s(market.get("expiration_time") or market.get("close_time")),
        market.get("result", "") or market.get("status", ""),
        str(market.get("volume", 0)),
        json.dumps(market, default=str),
    )
    conn.execute(
        "INSERT OR REPLACE INTO kalshi_historical_markets "
        "(market_ticker, series_ticker, event_ticker, strike, comparator, "
        " open_ts, close_ts, expiration_ts, settled_result, volume, raw_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        row,
    )


def insert_trade(conn: Any, trade: dict) -> None:
    """Append one trade row (dedupe would need a unique column Kalshi doesn't expose)."""
    row = (
        trade.get("ticker") or trade.get("market_ticker", ""),
        int(float(trade.get("created_time_ms") or trade.get("ts_us") or 0) * 1000)
            if trade.get("created_time_ms") else int(trade.get("ts_us", 0) or 0),
        str(trade.get("yes_price") if trade.get("yes_price") is not None
            else trade.get("price", 0)),
        str(trade.get("count", 0)),
        (trade.get("taker_side") or "").lower(),
    )
    conn.execute(
        "INSERT INTO kalshi_historical_trades "
        "(market_ticker, ts_us, price, qty, taker_side) VALUES (?, ?, ?, ?, ?)",
        row,
    )


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def pull_markets(
    client: KalshiAPIClient, *,
    series: Iterable[str], min_close_ts: int, max_close_ts: int,
    conn: Any | None = None, dry_run: bool = False,
) -> list[dict]:
    collected: list[dict] = []
    for series_ticker in series:
        logger.info("pulling /historical/markets series=%s window=[%d, %d]",
                    series_ticker, min_close_ts, max_close_ts)
        count = 0
        for market in client.historical_markets(
            series_ticker=series_ticker,
            min_close_ts=min_close_ts, max_close_ts=max_close_ts,
        ):
            collected.append(market)
            count += 1
            if conn and not dry_run:
                upsert_market(conn, market)
        logger.info("  %s: %d markets", series_ticker, count)
        if conn and not dry_run:
            conn.commit()
    return collected


def pull_trades(
    client: KalshiAPIClient, *,
    markets: Iterable[dict],
    conn: Any | None = None, dry_run: bool = False,
) -> int:
    total = 0
    for m in markets:
        ticker = m.get("ticker") or m.get("market_ticker")
        if not ticker:
            continue
        count = 0
        for trade in client.historical_trades(ticker=ticker):
            total += 1
            count += 1
            if conn and not dry_run:
                insert_trade(conn, trade)
        if count:
            logger.info("  trades for %s: %d", ticker, count)
        if conn and not dry_run:
            conn.commit()
    return total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Kalshi P1 historical data pull.")
    parser.add_argument("--days", type=int, default=1,
                        help="Look back N days from now (default 1).")
    parser.add_argument("--asset", default="btc",
                        choices=("btc", "eth", "sol", "all"))
    parser.add_argument("--database-url", default=None,
                        help="Override DATABASE_URL.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch + print counts; don't write to DB.")
    parser.add_argument("--skip-trades", action="store_true",
                        help="Pull markets metadata only (T01); skip trades (T02).")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    try:
        from dotenv import load_dotenv  # soft dep
        load_dotenv()
    except ImportError:
        pass

    now_s = int(time.time())
    min_close_ts = now_s - args.days * 86_400
    max_close_ts = now_s

    client = KalshiAPIClient.from_env()
    series = _series_for_asset(args.asset)

    url = (args.database_url or os.environ.get("DATABASE_URL")
           or "sqlite:///data/kalshi.db")
    conn = None if args.dry_run else _open_connection(url)

    try:
        markets = pull_markets(
            client, series=series,
            min_close_ts=min_close_ts, max_close_ts=max_close_ts,
            conn=conn, dry_run=args.dry_run,
        )
        logger.info("pulled %d markets total", len(markets))

        if not args.skip_trades:
            n = pull_trades(client, markets=markets, conn=conn, dry_run=args.dry_run)
            logger.info("pulled %d trades total", n)
    finally:
        if conn is not None:
            conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
