"""DB migrations for Phase-1 Kalshi research tables.

Works against SQLite (default `data/kalshi.db`) or Postgres via
`DATABASE_URL=postgresql://...`. Schema is intentionally plain SQL so both
backends accept it — no ORMs, no backend-specific column types. Tables are
exactly the five listed in execution plan §2.4 plus the `shadow_decisions`
columns enumerated in implementation-tasks P1-M4-T03.

Run:
    python3.11 scripts/migrate_db.py
    DATABASE_URL=postgresql://... python3.11 scripts/migrate_db.py

Idempotent: all statements use `IF NOT EXISTS`.
"""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
from pathlib import Path
from urllib.parse import urlparse


logger = logging.getLogger(__name__)


# Ordering: `reference_ticks` before `shadow_decisions` for clarity, but
# there are no FKs yet — Phase 1 keeps ingestion loose to catch real vs.
# synthesized mismatches during analysis.
SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS kalshi_historical_markets (
        market_ticker TEXT PRIMARY KEY,
        series_ticker TEXT NOT NULL,
        event_ticker  TEXT NOT NULL,
        strike        TEXT NOT NULL,
        comparator    TEXT NOT NULL,
        open_ts       BIGINT NOT NULL,
        close_ts      BIGINT NOT NULL,
        expiration_ts BIGINT NOT NULL,
        settled_result TEXT,
        settlement_ts BIGINT,
        expiration_value TEXT,
        last_price    TEXT,
        volume        TEXT,
        raw_json      TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_khm_series ON kalshi_historical_markets (series_ticker)",
    "CREATE INDEX IF NOT EXISTS idx_khm_expiration ON kalshi_historical_markets (expiration_ts)",
    "CREATE INDEX IF NOT EXISTS idx_khm_close_ts ON kalshi_historical_markets (close_ts)",
    # Idempotent column adds — SQLite raises if column already exists, so
    # wrap each in an expression SQL_ALTER_SAFE below. We still declare the
    # target set here so a fresh DB gets the latest shape from the CREATE.
    # For already-migrated DBs, run_safe_alters() handles the add.

    """
    CREATE TABLE IF NOT EXISTS kalshi_historical_trades (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        market_ticker TEXT NOT NULL,
        ts_us         BIGINT NOT NULL,
        price         TEXT NOT NULL,
        qty           TEXT NOT NULL,
        taker_side    TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_kht_ticker_ts ON kalshi_historical_trades (market_ticker, ts_us)",

    """
    CREATE TABLE IF NOT EXISTS kalshi_live_book_snapshots (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        market_ticker TEXT NOT NULL,
        ts_us         BIGINT NOT NULL,
        seq           BIGINT,
        yes_bids_json TEXT NOT NULL,
        no_bids_json  TEXT NOT NULL,
        warning_flags TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_klbs_ticker_ts ON kalshi_live_book_snapshots (market_ticker, ts_us)",

    """
    CREATE TABLE IF NOT EXISTS reference_ticks (
        id     INTEGER PRIMARY KEY AUTOINCREMENT,
        asset  TEXT NOT NULL,
        ts_us  BIGINT NOT NULL,
        price  TEXT NOT NULL,
        src    TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_rt_asset_ts ON reference_ticks (asset, ts_us)",

    """
    CREATE TABLE IF NOT EXISTS shadow_decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        market_ticker  TEXT   NOT NULL,
        ts_us          BIGINT NOT NULL,
        p_yes          TEXT   NOT NULL,
        ci_width       TEXT   NOT NULL,
        reference_price     TEXT NOT NULL,
        reference_60s_avg   TEXT NOT NULL,
        time_remaining_s    TEXT NOT NULL,
        best_yes_ask        TEXT NOT NULL,
        best_no_ask         TEXT NOT NULL,
        book_depth_yes_usd  TEXT NOT NULL,
        book_depth_no_usd   TEXT NOT NULL,
        recommended_side             TEXT NOT NULL,
        hypothetical_fill_price      TEXT NOT NULL,
        hypothetical_size_contracts  TEXT NOT NULL,
        expected_edge_bps_after_fees TEXT NOT NULL,
        fee_bps_at_decision          TEXT NOT NULL,
        realized_outcome             TEXT,
        realized_pnl_usd             TEXT,
        latency_ms_ref_to_decision   TEXT,
        latency_ms_book_to_decision  TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sd_ticker ON shadow_decisions (market_ticker)",
    "CREATE INDEX IF NOT EXISTS idx_sd_ts ON shadow_decisions (ts_us)",
    "CREATE INDEX IF NOT EXISTS idx_sd_outcome ON shadow_decisions (realized_outcome)",

    """
    CREATE TABLE IF NOT EXISTS kalshi_ideas_profiles (
        profile_slug      TEXT PRIMARY KEY,
        username          TEXT NOT NULL DEFAULT '',
        display_name      TEXT NOT NULL DEFAULT '',
        social_id         TEXT NOT NULL DEFAULT '',
        bio               TEXT NOT NULL DEFAULT '',
        profile_image_url TEXT NOT NULL DEFAULT '',
        profile_image_path TEXT NOT NULL DEFAULT '',
        pending_profile_image_path TEXT NOT NULL DEFAULT '',
        total_trades      TEXT NOT NULL DEFAULT '',
        total_predictions TEXT NOT NULL DEFAULT '',
        correct_predictions TEXT NOT NULL DEFAULT '',
        win_rate          TEXT NOT NULL DEFAULT '',
        profit_usd        TEXT NOT NULL DEFAULT '',
        follower_count    BIGINT NOT NULL DEFAULT 0,
        following_count   BIGINT NOT NULL DEFAULT 0,
        posts_count       BIGINT NOT NULL DEFAULT 0,
        profile_view_count BIGINT NOT NULL DEFAULT 0,
        joined_at         TEXT NOT NULL DEFAULT '',
        top_categories_json TEXT NOT NULL DEFAULT '[]',
        blocked           INTEGER NOT NULL DEFAULT 0,
        inner_circle_enabled INTEGER NOT NULL DEFAULT 0,
        inner_circle_viewer_status TEXT NOT NULL DEFAULT '',
        metrics_volume    BIGINT NOT NULL DEFAULT 0,
        metrics_volume_fp TEXT NOT NULL DEFAULT '',
        metrics_pnl       BIGINT NOT NULL DEFAULT 0,
        metrics_num_markets_traded BIGINT NOT NULL DEFAULT 0,
        metrics_raw_json  TEXT NOT NULL DEFAULT '',
        raw_json          TEXT NOT NULL,
        fetched_at_us     BIGINT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_kip_social_id ON kalshi_ideas_profiles (social_id)",

    """
    CREATE TABLE IF NOT EXISTS kalshi_ideas_leaderboard_entries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        category            TEXT NOT NULL,
        time_period         TEXT NOT NULL DEFAULT '',
        rank                INTEGER NOT NULL,
        profile_slug        TEXT NOT NULL,
        username            TEXT NOT NULL DEFAULT '',
        display_name        TEXT NOT NULL DEFAULT '',
        social_id           TEXT NOT NULL DEFAULT '',
        profile_image_path  TEXT NOT NULL DEFAULT '',
        metric_value        TEXT NOT NULL DEFAULT '',
        profit_usd          TEXT NOT NULL DEFAULT '',
        winning_streak      TEXT NOT NULL DEFAULT '',
        total_predictions   TEXT NOT NULL DEFAULT '',
        correct_predictions TEXT NOT NULL DEFAULT '',
        is_anonymous        INTEGER NOT NULL DEFAULT 0,
        raw_json            TEXT NOT NULL,
        fetched_at_us       BIGINT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_kile_category_rank ON kalshi_ideas_leaderboard_entries (category, rank)",
    "CREATE INDEX IF NOT EXISTS idx_kile_slug ON kalshi_ideas_leaderboard_entries (profile_slug)",
    "CREATE INDEX IF NOT EXISTS idx_kile_fetched_at ON kalshi_ideas_leaderboard_entries (fetched_at_us)",
    "CREATE INDEX IF NOT EXISTS idx_kile_period_category_rank ON kalshi_ideas_leaderboard_entries (time_period, category, rank)",

    """
    CREATE TABLE IF NOT EXISTS kalshi_ideas_trades (
        trade_id         TEXT PRIMARY KEY,
        profile_slug     TEXT NOT NULL,
        social_id        TEXT NOT NULL DEFAULT '',
        market_id        TEXT NOT NULL DEFAULT '',
        ticker           TEXT NOT NULL DEFAULT '',
        price_dollars    TEXT NOT NULL DEFAULT '',
        count_fp         TEXT NOT NULL DEFAULT '',
        taker_side       TEXT NOT NULL DEFAULT '',
        maker_action     TEXT NOT NULL DEFAULT '',
        taker_action     TEXT NOT NULL DEFAULT '',
        maker_nickname   TEXT NOT NULL DEFAULT '',
        taker_nickname   TEXT NOT NULL DEFAULT '',
        maker_social_id  TEXT NOT NULL DEFAULT '',
        taker_social_id  TEXT NOT NULL DEFAULT '',
        related_role     TEXT NOT NULL DEFAULT '',
        created_ts_us    BIGINT NOT NULL,
        raw_json         TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_kit_slug_ts ON kalshi_ideas_trades (profile_slug, created_ts_us)",
    "CREATE INDEX IF NOT EXISTS idx_kit_ticker_ts ON kalshi_ideas_trades (ticker, created_ts_us)",

    """
    CREATE TABLE IF NOT EXISTS kalshi_series (
        series_ticker TEXT PRIMARY KEY,
        category TEXT NOT NULL,
        title TEXT,
        frequency TEXT,
        contract_terms_url TEXT,
        raw_json TEXT NOT NULL,
        fetched_ts BIGINT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ks_category ON kalshi_series(category)",
    "CREATE INDEX IF NOT EXISTS idx_ks_fetched_ts ON kalshi_series(fetched_ts)",

    """
    CREATE TABLE IF NOT EXISTS kalshi_contract_terms (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        pdf_url TEXT UNIQUE NOT NULL,
        series_ticker_guess TEXT,
        local_path TEXT,
        bytes INTEGER,
        sha256 TEXT,
        fetched_ts BIGINT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_kct_guess ON kalshi_contract_terms(series_ticker_guess)",
    "CREATE INDEX IF NOT EXISTS idx_kct_fetched_ts ON kalshi_contract_terms(fetched_ts)",

    """
    CREATE TABLE IF NOT EXISTS kalshi_lag_candidates (
        series_ticker TEXT PRIMARY KEY,
        category TEXT NOT NULL,
        title TEXT NOT NULL DEFAULT '',
        source_type TEXT NOT NULL DEFAULT 'unknown',
        source_agency TEXT NOT NULL DEFAULT '',
        source_url TEXT NOT NULL DEFAULT '',
        publish_schedule_utc TEXT NOT NULL DEFAULT '',
        ltt_to_expiry_s INTEGER NOT NULL DEFAULT 0,
        strategy_hypothesis TEXT NOT NULL DEFAULT '',
        lag_priority_score INTEGER NOT NULL DEFAULT 0,
        priority_band TEXT NOT NULL DEFAULT '',
        notes TEXT NOT NULL DEFAULT '',
        raw_json TEXT NOT NULL DEFAULT '',
        built_ts BIGINT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_klc_score ON kalshi_lag_candidates(lag_priority_score DESC)",
    "CREATE INDEX IF NOT EXISTS idx_klc_category_score ON kalshi_lag_candidates(category, lag_priority_score DESC)",

    # P2-M1: paper-mode executor persistence. Every submit/reconcile writes
    # a row so notebooks can compute realized paper edge per strategy /
    # per asset / per time-bucket without replaying from shadow_decisions.
    """
    CREATE TABLE IF NOT EXISTS paper_fills (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        market_ticker TEXT NOT NULL,
        strategy_label TEXT NOT NULL DEFAULT '',
        filled_at_us BIGINT NOT NULL,
        side TEXT NOT NULL,
        fill_price TEXT NOT NULL,
        size_contracts TEXT NOT NULL,
        fees_paid_usd TEXT NOT NULL,
        notional_usd TEXT NOT NULL,
        expected_edge_bps_after_fees TEXT NOT NULL,
        p_yes TEXT NOT NULL,
        ci_width TEXT NOT NULL,
        reference_price TEXT NOT NULL,
        reference_60s_avg TEXT NOT NULL,
        time_remaining_s TEXT NOT NULL,
        strike TEXT NOT NULL,
        comparator TEXT NOT NULL,
        fee_bps_at_decision TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_pf_ticker ON paper_fills (market_ticker)",
    "CREATE INDEX IF NOT EXISTS idx_pf_strategy ON paper_fills (strategy_label)",
    "CREATE INDEX IF NOT EXISTS idx_pf_filled_at ON paper_fills (filled_at_us)",

    """
    CREATE TABLE IF NOT EXISTS paper_settlements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fill_id BIGINT NOT NULL,
        market_ticker TEXT NOT NULL,
        settled_at_us BIGINT NOT NULL,
        outcome TEXT NOT NULL,
        realized_pnl_usd TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ps_ticker ON paper_settlements (market_ticker)",
    "CREATE INDEX IF NOT EXISTS idx_ps_fill_id ON paper_settlements (fill_id)",
    "CREATE INDEX IF NOT EXISTS idx_ps_settled_at ON paper_settlements (settled_at_us)",

    # P2-M2: live executor persistence. Each submit/poll/cancel/settle
    # writes a row so reconciliation notebooks can confirm realized-P/L
    # matches Kalshi's /portfolio/settlements. Discrepancies surface here
    # rather than being buried in logs.
    """
    CREATE TABLE IF NOT EXISTS live_orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id TEXT NOT NULL DEFAULT '',
        client_order_id TEXT NOT NULL,
        market_ticker TEXT NOT NULL,
        strategy_label TEXT NOT NULL DEFAULT '',
        submitted_at_us BIGINT NOT NULL,
        side TEXT NOT NULL,
        price TEXT NOT NULL,
        size_contracts INTEGER NOT NULL,
        status TEXT NOT NULL,
        filled_at_us BIGINT,
        fill_price TEXT,
        fill_quantity INTEGER,
        fees_paid_usd TEXT,
        canceled_at_us BIGINT,
        cancel_reason TEXT,
        expected_edge_bps_after_fees TEXT NOT NULL DEFAULT '',
        p_yes TEXT NOT NULL DEFAULT '',
        reference_price TEXT NOT NULL DEFAULT '',
        strike TEXT NOT NULL DEFAULT '',
        comparator TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS uidx_lo_client_id ON live_orders (client_order_id)",
    "CREATE INDEX IF NOT EXISTS idx_lo_ticker ON live_orders (market_ticker)",
    "CREATE INDEX IF NOT EXISTS idx_lo_status ON live_orders (status)",
    "CREATE INDEX IF NOT EXISTS idx_lo_submitted_at ON live_orders (submitted_at_us)",

    """
    CREATE TABLE IF NOT EXISTS live_settlements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_row_id BIGINT NOT NULL,
        market_ticker TEXT NOT NULL,
        settled_at_us BIGINT NOT NULL,
        outcome TEXT NOT NULL,
        computed_pnl_usd TEXT NOT NULL,
        kalshi_reported_pnl_usd TEXT,
        discrepancy_usd TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ls_ticker ON live_settlements (market_ticker)",
    "CREATE INDEX IF NOT EXISTS idx_ls_order_row_id ON live_settlements (order_row_id)",
    "CREATE INDEX IF NOT EXISTS idx_ls_settled_at ON live_settlements (settled_at_us)",

    # Ops-events: structured error / warning / notable-info capture that
    # the dashboard's /kalshi/ops tails. Cheap enough that any emit site
    # in the process can write without async overhead (short-lived conn,
    # WAL-safe). Level is a free-form string but dashboard filtering
    # assumes 'info' / 'warn' / 'error'.
    """
    CREATE TABLE IF NOT EXISTS ops_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_us       BIGINT NOT NULL,
        source      TEXT NOT NULL,
        level       TEXT NOT NULL,
        message     TEXT NOT NULL,
        extras_json TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ops_ts ON ops_events (ts_us)",
    "CREATE INDEX IF NOT EXISTS idx_ops_level_ts ON ops_events (level, ts_us)",

    # Minute-bucketed phase_timing rollups. Raw phase_timing events stay
    # in `logs/events_YYYY-MM-DD.jsonl` (one line per phase exit). A
    # background writer (scripts/rollup_phase_timings.py) re-aggregates
    # the trailing 2h of JSONL every minute and upserts into this table.
    # Dashboard queries here instead of scanning JSONL — window-aware
    # and cross-day cheap.
    #
    # `bucket_seconds` is carried explicitly so we can later add hourly
    # or daily rollups alongside the minute-resolution default without
    # re-migrating. UNIQUE(bucket_ts_us, bucket_seconds, phase) makes the
    # upsert safe.
    """
    CREATE TABLE IF NOT EXISTS phase_timing_rollup (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bucket_ts_us     BIGINT  NOT NULL,
        bucket_seconds   INTEGER NOT NULL,
        phase            TEXT    NOT NULL,
        count            INTEGER NOT NULL,
        errors           INTEGER NOT NULL DEFAULT 0,
        total_elapsed_ms REAL    NOT NULL DEFAULT 0,
        p50_ms           REAL,
        p95_ms           REAL,
        p99_ms           REAL,
        max_ms           REAL
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS uidx_ptr_bucket_phase "
    "ON phase_timing_rollup (bucket_ts_us, bucket_seconds, phase)",
    "CREATE INDEX IF NOT EXISTS idx_ptr_bucket ON phase_timing_rollup (bucket_ts_us)",
    "CREATE INDEX IF NOT EXISTS idx_ptr_phase_bucket "
    "ON phase_timing_rollup (phase, bucket_ts_us)",
)


ALL_TABLES: tuple[str, ...] = (
    "kalshi_historical_markets",
    "kalshi_historical_trades",
    "kalshi_live_book_snapshots",
    "reference_ticks",
    "shadow_decisions",
    "kalshi_ideas_profiles",
    "kalshi_ideas_leaderboard_entries",
    "kalshi_ideas_trades",
    "kalshi_series",
    "kalshi_contract_terms",
    "kalshi_lag_candidates",
    "paper_fills",
    "paper_settlements",
    "live_orders",
    "live_settlements",
    "ops_events",
    "phase_timing_rollup",
)


def _database_url(override: str | None = None) -> str:
    if override:
        return override
    return os.environ.get("DATABASE_URL", "")


def _translate_for_sqlite(stmt: str) -> str:
    # Postgres `BIGINT` is a SQLite alias to INTEGER — no translation needed.
    # We only need to strip `AUTOINCREMENT` gymnastics if they appear on
    # Postgres, but our schema uses `INTEGER PRIMARY KEY AUTOINCREMENT` which
    # SQLite recognises natively and Postgres does not — keep `sqlite`-first
    # spelling and rewrite for Postgres instead.
    return stmt


def _translate_for_postgres(stmt: str) -> str:
    return (
        stmt
        .replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
    )


# ALTER statements run separately — they may already have been applied on a
# previous migration and SQLite's `ADD COLUMN` isn't natively idempotent.
SAFE_ALTER_STATEMENTS: tuple[str, ...] = (
    "ALTER TABLE kalshi_historical_markets ADD COLUMN settlement_ts BIGINT",
    "ALTER TABLE kalshi_historical_markets ADD COLUMN expiration_value TEXT",
    "ALTER TABLE kalshi_historical_markets ADD COLUMN last_price TEXT",
    "ALTER TABLE kalshi_ideas_profiles ADD COLUMN profile_image_path TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE kalshi_ideas_profiles ADD COLUMN pending_profile_image_path TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE kalshi_ideas_profiles ADD COLUMN follower_count BIGINT NOT NULL DEFAULT 0",
    "ALTER TABLE kalshi_ideas_profiles ADD COLUMN following_count BIGINT NOT NULL DEFAULT 0",
    "ALTER TABLE kalshi_ideas_profiles ADD COLUMN posts_count BIGINT NOT NULL DEFAULT 0",
    "ALTER TABLE kalshi_ideas_profiles ADD COLUMN profile_view_count BIGINT NOT NULL DEFAULT 0",
    "ALTER TABLE kalshi_ideas_profiles ADD COLUMN joined_at TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE kalshi_ideas_profiles ADD COLUMN top_categories_json TEXT NOT NULL DEFAULT '[]'",
    "ALTER TABLE kalshi_ideas_profiles ADD COLUMN blocked INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE kalshi_ideas_profiles ADD COLUMN inner_circle_enabled INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE kalshi_ideas_profiles ADD COLUMN inner_circle_viewer_status TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE kalshi_ideas_profiles ADD COLUMN metrics_volume BIGINT NOT NULL DEFAULT 0",
    "ALTER TABLE kalshi_ideas_profiles ADD COLUMN metrics_volume_fp TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE kalshi_ideas_profiles ADD COLUMN metrics_pnl BIGINT NOT NULL DEFAULT 0",
    "ALTER TABLE kalshi_ideas_profiles ADD COLUMN metrics_num_markets_traded BIGINT NOT NULL DEFAULT 0",
    "ALTER TABLE kalshi_ideas_profiles ADD COLUMN metrics_raw_json TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE kalshi_ideas_leaderboard_entries ADD COLUMN time_period TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE kalshi_ideas_leaderboard_entries ADD COLUMN profile_image_path TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE kalshi_ideas_leaderboard_entries ADD COLUMN metric_value TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE kalshi_ideas_leaderboard_entries ADD COLUMN is_anonymous INTEGER NOT NULL DEFAULT 0",
    """CREATE TABLE IF NOT EXISTS coinbase_trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        asset TEXT NOT NULL,
        ts_us BIGINT NOT NULL,
        price TEXT NOT NULL,
        size TEXT NOT NULL,
        side TEXT NOT NULL,
        trade_id BIGINT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_cb_trades_asset_ts ON coinbase_trades (asset, ts_us)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uidx_cb_trades_id ON coinbase_trades (asset, trade_id)",
    # 2026-04-20: per-strategy decision tagging for side-by-side comparison
    # (e.g., stat_model vs pure_lag runs sharing the shadow_decisions table).
    "ALTER TABLE shadow_decisions ADD COLUMN strategy_label TEXT",
    "CREATE INDEX IF NOT EXISTS idx_sd_strategy_label ON shadow_decisions (strategy_label)",
)


def _run_safe_alters(executor) -> None:
    """Apply each ALTER, swallowing duplicate-column errors."""
    for stmt in SAFE_ALTER_STATEMENTS:
        try:
            executor(stmt)
        except Exception as e:  # noqa: BLE001 — idempotent by design
            msg = str(e).lower()
            if "duplicate column" in msg or "already exists" in msg:
                continue
            raise


def migrate(url: str) -> None:
    """Apply all schema statements against the DB at `url`.

    Supports `sqlite:///...` (absolute or relative path) and
    `postgresql://...`. Anything else raises `ValueError`.
    """
    parsed = urlparse(url)
    if parsed.scheme in ("sqlite", ""):
        # SQLAlchemy convention: sqlite:///rel.db (3 slashes) is relative,
        # sqlite:////abs.db (4 slashes) is absolute. urlparse gives us
        # `/rel.db` or `//abs.db`, so: exactly one leading slash → relative,
        # two+ → absolute (keep first slash after stripping one).
        raw = parsed.path or url.removeprefix("sqlite://")
        if raw.startswith("//"):
            db_path = Path(raw[1:])  # absolute path
        elif raw.startswith("/"):
            db_path = Path(raw.lstrip("/"))  # relative to CWD
        else:
            db_path = Path(raw)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path))
        try:
            for stmt in SCHEMA_STATEMENTS:
                conn.execute(_translate_for_sqlite(stmt))
            _run_safe_alters(conn.execute)
            conn.commit()
        finally:
            conn.close()
        logger.info("sqlite migration complete: %s", db_path)
        return

    if parsed.scheme in ("postgres", "postgresql"):
        import psycopg2  # deferred import — not needed for sqlite dev

        conn = psycopg2.connect(url)
        try:
            with conn.cursor() as cur:
                for stmt in SCHEMA_STATEMENTS:
                    cur.execute(_translate_for_postgres(stmt))
                _run_safe_alters(lambda s: cur.execute(_translate_for_postgres(s)))
            conn.commit()
        finally:
            conn.close()
        logger.info("postgres migration complete: %s", url)
        return

    raise ValueError(f"Unsupported DATABASE_URL scheme: {parsed.scheme!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Kalshi P1 DB migrations.")
    parser.add_argument(
        "--database-url",
        default=None,
        help="Override DATABASE_URL (default: env var, else sqlite:///data/kalshi.db).",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    url = args.database_url or _database_url() or "sqlite:///data/kalshi.db"
    migrate(url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
