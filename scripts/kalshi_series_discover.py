"""Discover Kalshi series across categories and persist them to the DB.

This is the R1-T01 backbone for the multi-category lag-research plan. It uses
the public `/series` endpoint so we can inventory the Kalshi universe without
waiting on authenticated trading flows.
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

import requests


_THIS = Path(__file__).resolve()
_REPO = _THIS.parent.parent
for rel in ("src", "scripts"):
    p = _REPO / rel
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


logger = logging.getLogger(__name__)


HOST = "https://api.elections.kalshi.com/trade-api/v2"


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


def extract_contract_terms_url(series_row: dict[str, Any]) -> str:
    direct_keys = (
        "contract_terms_url",
        "rulebook_url",
        "rules_primary_url",
        "rules_url",
    )
    for key in direct_keys:
        value = series_row.get(key)
        if value:
            return str(value)

    for nested_key in ("contract_terms", "rules", "documents", "settlement_rules"):
        nested = series_row.get(nested_key)
        if isinstance(nested, dict):
            for key in ("url", "primary_url", "pdf_url"):
                value = nested.get(key)
                if value:
                    return str(value)
    return ""


def normalize_series_row(series_row: dict[str, Any]) -> dict[str, str]:
    return {
        "series_ticker": str(series_row.get("ticker") or series_row.get("series_ticker") or "").strip(),
        "category": str(series_row.get("category") or "").strip(),
        "title": str(
            series_row.get("title")
            or series_row.get("name")
            or series_row.get("subtitle")
            or ""
        ).strip(),
        "frequency": str(series_row.get("frequency") or series_row.get("interval") or "").strip(),
        "contract_terms_url": extract_contract_terms_url(series_row).strip(),
        "raw_json": json.dumps(series_row, default=str, sort_keys=True),
    }


def upsert_series(
    conn: Any,
    is_postgres: bool,
    row: dict[str, str],
    *,
    fetched_ts: int,
) -> None:
    if not row["series_ticker"]:
        return
    payload = (
        row["series_ticker"],
        row["category"],
        row["title"],
        row["frequency"],
        row["contract_terms_url"],
        row["raw_json"],
        fetched_ts,
    )
    sql = (
        "INSERT OR REPLACE INTO kalshi_series "
        "(series_ticker, category, title, frequency, contract_terms_url, raw_json, fetched_ts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)"
    )
    if is_postgres:
        sql = (
            "INSERT INTO kalshi_series "
            "(series_ticker, category, title, frequency, contract_terms_url, raw_json, fetched_ts) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (series_ticker) DO UPDATE SET "
            "category = EXCLUDED.category, "
            "title = EXCLUDED.title, "
            "frequency = EXCLUDED.frequency, "
            "contract_terms_url = EXCLUDED.contract_terms_url, "
            "raw_json = EXCLUDED.raw_json, "
            "fetched_ts = EXCLUDED.fetched_ts"
        )
        with conn.cursor() as cur:
            cur.execute(sql, payload)
        return
    conn.execute(sql, payload)


def paginate_series(
    *,
    category: str | None = None,
    limit: int = 200,
    max_pages: int = 1_000,
    pause_s: float = 0.1,
    session: requests.Session | None = None,
    host: str = HOST,
) -> Iterable[dict[str, Any]]:
    session = session or requests.Session()
    params: dict[str, Any] = {"limit": limit}
    if category:
        params["category"] = category
    for _ in range(max_pages):
        resp = session.get(f"{host}/series", params=params, timeout=30.0)
        resp.raise_for_status()
        payload = resp.json() if resp.content else {}
        rows = payload.get("series", []) if isinstance(payload, dict) else []
        for row in rows:
            if isinstance(row, dict):
                yield row
        cursor = payload.get("cursor", "") if isinstance(payload, dict) else ""
        if not cursor:
            return
        params["cursor"] = cursor
        if pause_s:
            time.sleep(pause_s)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Discover Kalshi series into kalshi_series.")
    parser.add_argument(
        "--category",
        action="append",
        default=[],
        help="Optional category filter; can repeat. Omit to pull the full series list.",
    )
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--max-pages", type=int, default=1_000)
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    categories = args.category or [None]
    db_url = args.database_url or os.environ.get("DATABASE_URL") or "sqlite:///data/kalshi.db"
    conn, is_postgres = (None, False) if args.dry_run else _open_connection(db_url)
    fetched_ts = int(time.time() * 1_000_000)
    totals: dict[str, int] = {}

    try:
        for category in categories:
            count = 0
            for raw_row in paginate_series(
                category=category,
                limit=args.limit,
                max_pages=args.max_pages,
            ):
                normalized = normalize_series_row(raw_row)
                if not normalized["series_ticker"]:
                    continue
                if conn is not None:
                    upsert_series(conn, is_postgres, normalized, fetched_ts=fetched_ts)
                count += 1
            key = category or "all"
            totals[key] = count
            logger.info("category=%s yielded %d series rows", key, count)
            if conn is not None and not is_postgres:
                conn.commit()
    finally:
        if conn is not None:
            conn.close()

    logger.info("done: %s", totals)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
