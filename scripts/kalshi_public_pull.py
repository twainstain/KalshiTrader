"""Pull settled Kalshi crypto 15-min markets from the **public** endpoint.

Uses `GET /trade-api/v2/markets?series_ticker=...&status=settled` — no auth
required — which returns full market objects including:

- `floor_strike`        — the prior-window 60s-avg BRTI (the "target")
- `expiration_value`    — the settled 60s-avg BRTI (ground truth)
- `result`              — `yes` / `no`
- `settlement_ts`       — ISO timestamp when settlement fired
- full book fields + volume

This is the real prod historical data we need for feasibility analysis —
no demo environment contamination, no waiting on KYC. Populates the new
`expiration_value`, `settlement_ts`, and `last_price` columns added by
the latest `scripts/migrate_db.py`.

Usage:
    python3.11 scripts/kalshi_public_pull.py --asset all --days 30
    python3.11 scripts/kalshi_public_pull.py --asset btc --max-markets 500
"""

from __future__ import annotations

import argparse
import json
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
SERIES_BY_ASSET: dict[str, tuple[str, ...]] = {
    "btc": ("KXBTC15M",),
    "eth": ("KXETH15M",),
    "sol": ("KXSOL15M",),
    "xrp": ("KXXRP15M",),
    "doge": ("KXDOGE15M",),
    "bnb": ("KXBNB15M",),
    "hype": ("KXHYPE15M",),
}

COMPARATOR_MAP: dict[str, str] = {
    "greater_or_equal": "at_least",
    "greater_than": "above",
    "less_or_equal": "below",
    "less_than": "below",
}


def _series_for_asset(asset: str) -> tuple[str, ...]:
    asset = asset.lower()
    if asset == "all":
        return tuple(s for v in SERIES_BY_ASSET.values() for s in v)
    if asset in SERIES_BY_ASSET:
        return SERIES_BY_ASSET[asset]
    raise ValueError(f"unknown asset {asset!r}")


def _asset_choices() -> tuple[str, ...]:
    return ("all",) + tuple(sorted(SERIES_BY_ASSET.keys()))


def _to_epoch_s(val: Any) -> int:
    if val is None or val == "":
        return 0
    if isinstance(val, (int, float)):
        return int(val)
    if isinstance(val, str):
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
# DB
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


def upsert_market(conn: Any, is_postgres: bool, m: dict) -> None:
    ticker = m.get("ticker")
    if not ticker:
        return
    series = m.get("event_ticker", "").split("-", 1)[0] or ""
    strike = (
        m.get("strike_price")
        or m.get("floor_strike")
        or m.get("cap_strike")
        or 0
    )
    raw_comp = (m.get("strike_type") or "above").lower()
    comparator = COMPARATOR_MAP.get(raw_comp, raw_comp)
    row = (
        ticker,
        series,
        m.get("event_ticker", ""),
        str(strike),
        comparator,
        _to_epoch_s(m.get("open_time")),
        _to_epoch_s(m.get("close_time")),
        _to_epoch_s(m.get("expiration_time") or m.get("close_time")),
        m.get("result", "") or m.get("status", ""),
        _to_epoch_s(m.get("settlement_ts") or m.get("updated_time")),
        str(m.get("expiration_value") or ""),
        str(m.get("last_price_dollars") or ""),
        str(m.get("volume_fp") or m.get("volume", 0)),
        json.dumps(m, default=str),
    )
    sql = (
        "INSERT OR REPLACE INTO kalshi_historical_markets "
        "(market_ticker, series_ticker, event_ticker, strike, comparator, "
        " open_ts, close_ts, expiration_ts, settled_result, settlement_ts, "
        " expiration_value, last_price, volume, raw_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    if is_postgres:
        sql = sql.replace("INSERT OR REPLACE", "INSERT").replace(
            "VALUES", "VALUES"  # no-op to preserve structure
        ) + " ON CONFLICT (market_ticker) DO UPDATE SET raw_json = EXCLUDED.raw_json"
        with conn.cursor() as cur:
            cur.execute(sql.replace("?", "%s"), row)
    else:
        conn.execute(sql, row)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def paginate_markets(
    series_ticker: str,
    *, status: str = "settled", page_size: int = 200,
    max_markets: int | None = None,
    pause_s: float = 0.1,
    session: requests.Session | None = None,
) -> Iterable[dict]:
    session = session or requests.Session()
    params: dict[str, Any] = {
        "series_ticker": series_ticker,
        "status": status,
        "limit": page_size,
    }
    collected = 0
    while True:
        try:
            resp = session.get(f"{HOST}/markets", params=params, timeout=15.0)
        except requests.RequestException as e:
            logger.warning("%s: transport error %s — retrying", series_ticker, e)
            time.sleep(1.0)
            continue
        if resp.status_code == 429:
            logger.info("%s: 429 — backing off 2s", series_ticker)
            time.sleep(2.0)
            continue
        if resp.status_code != 200:
            logger.warning("%s: %d %s", series_ticker, resp.status_code, resp.text[:200])
            break
        data = resp.json() if resp.content else {}
        for m in data.get("markets", []) or []:
            yield m
            collected += 1
            if max_markets and collected >= max_markets:
                return
        cursor = data.get("cursor")
        if not cursor:
            return
        params["cursor"] = cursor
        if pause_s:
            time.sleep(pause_s)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Public pull — settled crypto 15M markets.")
    parser.add_argument("--asset", default="all", choices=_asset_choices())
    parser.add_argument("--days", type=int, default=None,
                        help="Filter to markets whose close_ts is within the last N days.")
    parser.add_argument("--max-markets", type=int, default=None,
                        help="Per-series cap (for smoke tests).")
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    now_s = int(time.time())
    min_close_ts = (now_s - args.days * 86_400) if args.days else None

    url = (args.database_url or os.environ.get("DATABASE_URL")
           or "sqlite:///data/kalshi.db")
    conn, is_pg = (None, False) if args.dry_run else _open_connection(url)

    series_list = _series_for_asset(args.asset)
    totals: dict[str, int] = {}

    for series in series_list:
        count = 0
        out_of_range = 0
        logger.info("pulling settled markets for %s", series)
        for m in paginate_markets(series, max_markets=args.max_markets):
            close_ts = _to_epoch_s(m.get("close_time"))
            if min_close_ts is not None and close_ts < min_close_ts:
                out_of_range += 1
                # Endpoint returns DESC; once we're past the window we can stop.
                break
            if conn is not None:
                upsert_market(conn, is_pg, m)
            count += 1
        totals[series] = count
        logger.info("  %s: %d markets written (out-of-range skipped: %d)",
                    series, count, out_of_range)
        if conn is not None and not is_pg:
            conn.commit()

    if conn is not None:
        conn.close()
    logger.info("done: %s", totals)
    return 0


if __name__ == "__main__":
    sys.exit(main())
