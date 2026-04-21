"""Collect multi-category lag research inputs and analyze the results.

This is the one-command runner for the first research loop:

1. migrate the DB
2. discover Kalshi series
3. inventory contract-term PDFs
4. build the heuristic lag-opportunity registry
5. emit a compact analysis summary from the resulting tables

It is intentionally orchestration-heavy and logic-light: the individual steps
still live in their own scripts so we can run them independently when needed.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


_THIS = Path(__file__).resolve()
_REPO = _THIS.parent.parent
for rel in ("src", "scripts"):
    p = _REPO / rel
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


import kalshi_contract_terms_pull
import kalshi_registry_build
import kalshi_series_discover
import migrate_db


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


def collect_analysis_summary(
    conn: Any,
    is_postgres: bool,
    *,
    top_n: int,
) -> dict[str, Any]:
    candidates = fetch_rows(
        conn,
        is_postgres,
        "SELECT series_ticker, category, source_type, source_agency, lag_priority_score, "
        "priority_band, strategy_hypothesis, publish_schedule_utc, notes "
        "FROM kalshi_lag_candidates ORDER BY lag_priority_score DESC, series_ticker",
    )
    category_counts = Counter(row["category"] or "unknown" for row in candidates)
    source_type_counts = Counter(row["source_type"] or "unknown" for row in candidates)
    high_priority = [row for row in candidates if (row.get("priority_band") or "") == "high"]

    return {
        "candidate_count": len(candidates),
        "high_priority_count": len(high_priority),
        "category_counts": dict(sorted(category_counts.items(), key=lambda item: (-item[1], item[0]))),
        "source_type_counts": dict(sorted(source_type_counts.items(), key=lambda item: (-item[1], item[0]))),
        "top_candidates": candidates[:top_n],
    }


def render_analysis_markdown(summary: dict[str, Any], *, research_date: str) -> str:
    lines = [
        "# Kalshi Research Collection Summary",
        "",
        f"**Research date:** {research_date}",
        "",
        f"- Candidate rows: `{summary['candidate_count']}`",
        f"- High-priority rows: `{summary['high_priority_count']}`",
        "",
        "## Top Candidates",
        "",
        "| Rank | Series | Category | Source Type | Agency | Score | Band | Hypothesis |",
        "| --- | --- | --- | --- | --- | ---: | --- | --- |",
    ]
    for rank, row in enumerate(summary["top_candidates"], start=1):
        lines.append(
            f"| {rank} | {row['series_ticker']} | {row['category'] or 'unknown'} | "
            f"{row['source_type'] or 'unknown'} | {row['source_agency'] or 'unknown'} | "
            f"{row['lag_priority_score']} | {row['priority_band'] or 'unknown'} | "
            f"{row['strategy_hypothesis'] or 'manual_review'} |"
        )

    lines.extend(["", "## Counts By Category", ""])
    for category, count in summary["category_counts"].items():
        lines.append(f"- `{category}`: `{count}`")

    lines.extend(["", "## Counts By Source Type", ""])
    for source_type, count in summary["source_type_counts"].items():
        lines.append(f"- `{source_type}`: `{count}`")

    if summary["top_candidates"]:
        lines.extend(["", "## Notes", ""])
        for row in summary["top_candidates"][:10]:
            lines.append(f"- `{row['series_ticker']}`: {row['notes']}")

    return "\n".join(lines) + "\n"


def write_analysis_outputs(
    summary: dict[str, Any],
    *,
    research_date: str,
    output_markdown: str | Path,
    output_json: str | Path | None = None,
) -> None:
    md_path = Path(output_markdown)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(render_analysis_markdown(summary, research_date=research_date))

    if output_json is not None:
        json_path = Path(output_json)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")


def _run_step(name: str, fn, argv: list[str]) -> None:
    logger.info("running %s", name)
    rc = fn(argv)
    if rc != 0:
        raise RuntimeError(f"{name} failed with exit code {rc}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Collect Kalshi lag research inputs and analyze the resulting registry."
    )
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--category", action="append", default=[])
    parser.add_argument("--series-limit", type=int, default=200)
    parser.add_argument("--series-max-pages", type=int, default=1_000)
    parser.add_argument("--contract-max-files", type=int, default=None)
    parser.add_argument("--contract-max-pages", type=int, default=1_000)
    parser.add_argument("--contract-dest-dir", default="data/contract_terms")
    parser.add_argument("--registry-output-json", default="config/kalshi_series_registry.json")
    parser.add_argument("--registry-output-markdown", default="docs/kalshi_lag_opportunity_ranking.md")
    parser.add_argument("--analysis-output-markdown", default="docs/kalshi_research_collection_summary.md")
    parser.add_argument("--analysis-output-json", default="data/kalshi_research_collection_summary.json")
    parser.add_argument("--top-n", type=int, default=25)
    parser.add_argument("--research-date", default=None)
    parser.add_argument("--skip-migrate", action="store_true")
    parser.add_argument("--skip-series-discovery", action="store_true")
    parser.add_argument("--skip-contract-terms", action="store_true")
    parser.add_argument("--skip-registry-build", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    db_url = args.database_url or os.environ.get("DATABASE_URL") or "sqlite:///data/kalshi.db"
    research_date = args.research_date or time.strftime("%Y-%m-%d")

    if not args.skip_migrate:
        migrate_db.migrate(db_url)

    if not args.skip_series_discovery:
        series_argv = [
            "--database-url",
            db_url,
            "--limit",
            str(args.series_limit),
            "--max-pages",
            str(args.series_max_pages),
        ]
        for category in args.category:
            series_argv.extend(["--category", category])
        if args.verbose:
            series_argv.append("--verbose")
        _run_step("kalshi_series_discover", kalshi_series_discover.main, series_argv)

    if not args.skip_contract_terms:
        contract_argv = [
            "--database-url",
            db_url,
            "--dest-dir",
            args.contract_dest_dir,
            "--max-pages",
            str(args.contract_max_pages),
        ]
        if args.contract_max_files is not None:
            contract_argv.extend(["--max-files", str(args.contract_max_files)])
        if args.verbose:
            contract_argv.append("--verbose")
        _run_step(
            "kalshi_contract_terms_pull",
            kalshi_contract_terms_pull.main,
            contract_argv,
        )

    if not args.skip_registry_build:
        registry_argv = [
            "--database-url",
            db_url,
            "--output-json",
            args.registry_output_json,
            "--output-markdown",
            args.registry_output_markdown,
            "--research-date",
            research_date,
        ]
        if args.verbose:
            registry_argv.append("--verbose")
        _run_step("kalshi_registry_build", kalshi_registry_build.main, registry_argv)

    conn, is_postgres = _open_connection(db_url)
    try:
        summary = collect_analysis_summary(conn, is_postgres, top_n=args.top_n)
    finally:
        conn.close()

    write_analysis_outputs(
        summary,
        research_date=research_date,
        output_markdown=args.analysis_output_markdown,
        output_json=args.analysis_output_json,
    )
    logger.info(
        "analysis summary written: candidates=%d high_priority=%d",
        summary["candidate_count"],
        summary["high_priority_count"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
