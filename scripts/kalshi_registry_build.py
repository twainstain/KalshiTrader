"""Build a heuristic multi-category lag-opportunity registry.

This script joins `kalshi_series` with any downloaded contract terms,
computes a pre-measurement lag-priority score, and writes the result to:

- `config/kalshi_series_registry.json`
- `docs/kalshi_lag_opportunity_ranking.md`
- `kalshi_lag_candidates` (latest snapshot)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


_THIS = Path(__file__).resolve()
_REPO = _THIS.parent.parent
for rel in ("src", "scripts"):
    p = _REPO / rel
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


from research.series_registry import (  # noqa: E402
    SeriesRegistryEntry,
    build_registry,
    render_opportunity_markdown,
    to_registry_json,
)


logger = logging.getLogger(__name__)


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
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        return conn, False
    if parsed.scheme in ("postgres", "postgresql"):
        import psycopg2
        import psycopg2.extras

        conn = psycopg2.connect(url)
        conn.cursor_factory = psycopg2.extras.RealDictCursor
        return conn, True
    raise ValueError(f"Unsupported DATABASE_URL scheme: {parsed.scheme!r}")


def fetch_rows(conn: Any, is_postgres: bool, sql: str) -> list[dict[str, Any]]:
    if is_postgres:
        with conn.cursor() as cur:
            cur.execute(sql)
            return [dict(row) for row in cur.fetchall()]
    rows = conn.execute(sql).fetchall()
    return [dict(row) for row in rows]


def upsert_lag_candidate(
    conn: Any,
    is_postgres: bool,
    *,
    entry: SeriesRegistryEntry,
    built_ts: int,
) -> None:
    payload = entry.to_db_row(built_ts=built_ts)
    sql = (
        "INSERT OR REPLACE INTO kalshi_lag_candidates "
        "(series_ticker, category, title, source_type, source_agency, source_url, "
        " publish_schedule_utc, ltt_to_expiry_s, strategy_hypothesis, "
        " lag_priority_score, priority_band, notes, raw_json, built_ts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    if is_postgres:
        sql = (
            "INSERT INTO kalshi_lag_candidates "
            "(series_ticker, category, title, source_type, source_agency, source_url, "
            " publish_schedule_utc, ltt_to_expiry_s, strategy_hypothesis, "
            " lag_priority_score, priority_band, notes, raw_json, built_ts) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (series_ticker) DO UPDATE SET "
            "category = EXCLUDED.category, "
            "title = EXCLUDED.title, "
            "source_type = EXCLUDED.source_type, "
            "source_agency = EXCLUDED.source_agency, "
            "source_url = EXCLUDED.source_url, "
            "publish_schedule_utc = EXCLUDED.publish_schedule_utc, "
            "ltt_to_expiry_s = EXCLUDED.ltt_to_expiry_s, "
            "strategy_hypothesis = EXCLUDED.strategy_hypothesis, "
            "lag_priority_score = EXCLUDED.lag_priority_score, "
            "priority_band = EXCLUDED.priority_band, "
            "notes = EXCLUDED.notes, "
            "raw_json = EXCLUDED.raw_json, "
            "built_ts = EXCLUDED.built_ts"
        )
        with conn.cursor() as cur:
            cur.execute(sql, payload)
        return
    conn.execute(sql, payload)


def write_registry_json(entries: Sequence[SeriesRegistryEntry], path: str | Path) -> None:
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = to_registry_json(entries)
    out_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def write_markdown(entries: Sequence[SeriesRegistryEntry], path: str | Path, *, research_date: str) -> None:
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        render_opportunity_markdown(entries, research_date=research_date)
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the Kalshi lag-opportunity registry.")
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--output-json", default="config/kalshi_series_registry.json")
    parser.add_argument("--output-markdown", default="docs/kalshi_lag_opportunity_ranking.md")
    parser.add_argument("--research-date", default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    db_url = args.database_url or os.environ.get("DATABASE_URL") or "sqlite:///data/kalshi.db"
    conn, is_postgres = _open_connection(db_url)
    built_ts = int(time.time() * 1_000_000)
    research_date = args.research_date or time.strftime("%Y-%m-%d")

    try:
        series_rows = fetch_rows(
            conn,
            is_postgres,
            "SELECT series_ticker, category, title, frequency, contract_terms_url, raw_json "
            "FROM kalshi_series ORDER BY category, series_ticker",
        )
        contract_rows = fetch_rows(
            conn,
            is_postgres,
            "SELECT pdf_url, series_ticker_guess, local_path FROM kalshi_contract_terms",
        )
        entries = build_registry(series_rows, contract_rows)
        if args.limit is not None:
            entries = entries[: args.limit]

        for entry in entries:
            upsert_lag_candidate(conn, is_postgres, entry=entry, built_ts=built_ts)
        if not is_postgres:
            conn.commit()

        write_registry_json(entries, args.output_json)
        write_markdown(entries, args.output_markdown, research_date=research_date)
    finally:
        conn.close()

    logger.info("done: %d lag candidates written", len(entries))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
