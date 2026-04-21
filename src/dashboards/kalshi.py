"""Phase-1 observability dashboard.

Read-only FastAPI app that reads `data/kalshi.db` and renders HTML
overviews of the running shadow scanner. Scoped to what we need during
the NO-GO re-evaluation window (2026-04-20 → ≥500 `pure_lag` reconciled
decisions): decision counts, rolling P/L, per-asset/per-strategy
breakdowns, feed freshness.

**Not** the full P2-M3 dashboard — that requires live `/portfolio/*`
integration + risk-rejection counters + WS health + rate-limit headroom.
This is the Phase-1-observer variant: strictly read-only, strictly DB,
strictly localhost.

Run:
    PYTHONPATH=src python3.11 src/run_dashboard.py
    open http://localhost:8000/
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

import ops_events
import phase_timing_rollup
import runtime_flags


# ---------------------------------------------------------------------------
# HTTP Basic auth middleware
# ---------------------------------------------------------------------------


def _basic_auth_middleware_factory(username: str, password: str):
    """Return an ASGI middleware class bound to `username`/`password`.

    Enforces HTTP Basic on every route. Comparison is constant-time
    (`hmac.compare_digest`) to avoid credential-oracle side channels.
    Missing / malformed `Authorization` header → 401 with a
    `WWW-Authenticate` challenge so browsers show the native prompt.

    Intentionally stateless: no session, no cookies. The browser resends
    the header on every request; same cost as not having auth at all
    for our traffic volume.
    """
    import base64
    import hmac

    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import Response

    class BasicAuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):  # type: ignore[override]
            header = request.headers.get("authorization", "")
            if not header.startswith("Basic "):
                return self._challenge()
            try:
                decoded = base64.b64decode(
                    header[6:], validate=True,
                ).decode("utf-8", errors="replace")
            except (ValueError, UnicodeDecodeError):
                return self._challenge()
            user, _, pwd = decoded.partition(":")
            if not (
                hmac.compare_digest(user, username)
                and hmac.compare_digest(pwd, password)
            ):
                return self._challenge()
            return await call_next(request)

        @staticmethod
        def _challenge() -> "Response":
            return Response(
                "401 Unauthorized\n",
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="kalshi"'},
            )

    return BasicAuthMiddleware


async def _urlencoded_form(request: Request) -> dict[str, str]:
    """Parse an application/x-www-form-urlencoded body without pulling in
    `python-multipart` (which `fastapi.Form` requires). Enough for our
    one-field-per-control POSTs.
    """
    from urllib.parse import parse_qs
    raw = (await request.body()).decode("utf-8", errors="replace")
    parsed = parse_qs(raw, keep_blank_values=True)
    return {k: v[0] for k, v in parsed.items()}


# Time-window tabs shown across overview / performance / ops. `all` means
# no lower bound. The cutoff is computed relative to `MAX(ts_us)` in
# `shadow_decisions` (not wall clock) so rollups stay anchored to when the
# scanner was last producing data — same pattern the ArbitrageTrader
# main_dashboard uses, and stable for replay-based analysis.
WINDOWS: tuple[str, ...] = ("5m", "15m", "1h", "4h", "24h", "7d", "all")
DEFAULT_WINDOW = "1h"

_WINDOW_SECONDS: dict[str, int] = {
    "5m": 5 * 60,
    "15m": 15 * 60,
    "1h": 60 * 60,
    "4h": 4 * 60 * 60,
    "24h": 24 * 60 * 60,
    "7d": 7 * 24 * 60 * 60,
}


def _resolve_window(window: str | None) -> str:
    return window if window in WINDOWS else DEFAULT_WINDOW


def _anchor_now_us(conn: sqlite3.Connection) -> int:
    """Use the most recent decision as 'now' so windows are stable in replay.
    Falls back to wall-clock when the table is empty."""
    row = conn.execute("SELECT MAX(ts_us) AS ts FROM shadow_decisions").fetchone()
    if row and row["ts"] is not None:
        return int(row["ts"])
    return int(time.time() * 1_000_000)


def _window_cutoff_us(conn: sqlite3.Connection, window: str) -> int | None:
    """Return the ts_us lower bound for `window`, or None when `all`."""
    window = _resolve_window(window)
    if window == "all":
        return None
    return _anchor_now_us(conn) - _WINDOW_SECONDS[window] * 1_000_000


def _parse_time_param_us(value: str | None) -> int | None:
    """Coerce a URL time param into microseconds-since-epoch.

    Accepts:
      - ISO-8601 with or without a `Z` suffix (`2026-04-20T14:30:00Z`).
      - A numeric epoch in seconds (`1776691800`), milliseconds
        (`1776691800000`), or microseconds (`1776691800000000`) —
        magnitude is used to disambiguate (seconds < 10^12, ms < 10^15,
        otherwise microseconds).
    Returns `None` when `value` is `None` or an empty string.
    Raises `ValueError` when `value` is non-empty but unparseable so
    the endpoint layer can surface a 422.
    """
    if value is None or value == "":
        return None
    # Numeric form first (catches "1776691800", "1776691800.5", etc).
    try:
        num = float(value)
    except ValueError:
        num = None
    if num is not None:
        if num < 1_000_000_000_000:          # < 10^12  → seconds
            return int(num * 1_000_000)
        if num < 1_000_000_000_000_000:      # < 10^15  → milliseconds
            return int(num * 1_000)
        return int(num)                      #           → microseconds
    # ISO-8601 fallback — `fromisoformat` accepts offset-bearing strings
    # on 3.11; normalize `Z` for older interpreters.
    from datetime import datetime, timezone
    normalized = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(
            f"unparseable timestamp: {value!r} (expect ISO-8601 or unix epoch)"
        ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.astimezone(timezone.utc).timestamp() * 1_000_000)


def _time_bounds_us(
    conn: sqlite3.Connection,
    *,
    window: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> tuple[int | None, int | None]:
    """Resolve (lo_us, hi_us) for a request.

    When either `start` or `end` is supplied, those override `window`
    (an explicit range is always more specific than a bucket). When both
    are absent, fall back to `window`'s lower bound (upper bound stays
    open so the behavior matches the pre-range call sites).
    """
    if start or end:
        return (_parse_time_param_us(start), _parse_time_param_us(end))
    return (_window_cutoff_us(conn, window or DEFAULT_WINDOW), None)


def _range_clause(
    lo_us: int | None, hi_us: int | None, column: str = "ts_us",
) -> tuple[str, list[Any]]:
    """Return (sql_fragment, params) for an optional `[lo, hi]` filter on
    `column`. Fragment is either `""` (both None) or a ` AND ...` suffix
    suitable to paste onto an existing WHERE clause.
    """
    parts: list[str] = []
    params: list[Any] = []
    if lo_us is not None:
        parts.append(f"{column} >= ?")
        params.append(lo_us)
    if hi_us is not None:
        parts.append(f"{column} <= ?")
        params.append(hi_us)
    if not parts:
        return ("", [])
    return (" AND " + " AND ".join(parts), params)


@dataclass(frozen=True)
class WalletSnapshot:
    """Minimal shape returned by a `balance_fetcher`.

    All fields optional so a partial snapshot (e.g. balance only) still
    renders. Values are stringified to keep Decimal handling out of the
    fetcher contract — formatters coerce via `_cell`.
    """
    balance_usd: str | None = None
    positions_count: int | None = None
    notional_usd: str | None = None
    source: str = "kalshi"
    error: str | None = None


BalanceFetcher = Callable[[], WalletSnapshot | None]


# ---------------------------------------------------------------------------
# DB access — read-only, short-lived connections
# ---------------------------------------------------------------------------


def _open_readonly(url: str) -> sqlite3.Connection:
    """Open a SQLite DB in read-only mode."""
    parsed = urlparse(url)
    if parsed.scheme not in ("sqlite", ""):
        raise ValueError(
            f"dashboard only supports sqlite:// urls, got {url!r}"
        )
    raw = parsed.path or url.removeprefix("sqlite://")
    if raw.startswith("//"):
        path = Path(raw[1:])
    elif raw.startswith("/"):
        path = Path(raw.lstrip("/"))
    else:
        path = Path(raw)
    if not path.is_file():
        raise FileNotFoundError(f"DB not found: {path}")
    conn = sqlite3.connect(
        f"file:{path}?mode=ro", uri=True, check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Aggregates — small query helpers
# ---------------------------------------------------------------------------


_LEGACY_STRATEGY_LABEL = "stat_model_legacy"


def _fetch_overview(
    conn: sqlite3.Connection, *, window: str = DEFAULT_WINDOW,
    start_us: int | None = None, end_us: int | None = None,
) -> dict[str, Any]:
    window = _resolve_window(window)
    if start_us is None and end_us is None:
        lo_us = _window_cutoff_us(conn, window)
        hi_us: int | None = None
    else:
        lo_us, hi_us = start_us, end_us
    now_us = _anchor_now_us(conn)

    where_clauses = ["recommended_side != 'none'"]
    where_params: list[Any] = []
    if lo_us is not None:
        where_clauses.append("ts_us >= ?")
        where_params.append(lo_us)
    if hi_us is not None:
        where_clauses.append("ts_us <= ?")
        where_params.append(hi_us)
    where_sql = " WHERE " + " AND ".join(where_clauses)

    # Positional `?` order: SELECT-coalesce, WHERE cutoff?, GROUP-coalesce.
    rows = conn.execute(
        f"""
        SELECT COALESCE(NULLIF(strategy_label, ''), ?) AS strategy_label,
               COUNT(*) AS total,
               COUNT(realized_outcome) AS reconciled,
               SUM(CASE WHEN CAST(realized_pnl_usd AS REAL) > 0 THEN 1 ELSE 0 END) AS wins,
               ROUND(SUM(CAST(realized_pnl_usd AS REAL)), 2) AS pnl,
               ROUND(AVG(CAST(realized_pnl_usd AS REAL)), 4) AS pnl_per_dec
          FROM shadow_decisions
          {where_sql}
         GROUP BY COALESCE(NULLIF(strategy_label, ''), ?)
         ORDER BY strategy_label
        """,
        [_LEGACY_STRATEGY_LABEL, *where_params, _LEGACY_STRATEGY_LABEL],
    ).fetchall()
    per_strategy = [dict(r) for r in rows]

    # Totals card = sum across strategies in-window. Cheap to compute in
    # Python rather than round-tripping another query.
    totals = {
        "total": sum((s.get("total") or 0) for s in per_strategy),
        "reconciled": sum((s.get("reconciled") or 0) for s in per_strategy),
        "wins": sum((s.get("wins") or 0) for s in per_strategy),
        "pnl": round(sum((s.get("pnl") or 0.0) for s in per_strategy), 2),
    }
    totals["pnl_per_dec"] = (
        round(totals["pnl"] / totals["total"], 4) if totals["total"] else 0.0
    )

    # Latest reference tick per asset (always "now" — window is irrelevant
    # for feed-freshness).
    latest_ref = conn.execute(
        "SELECT asset, MAX(ts_us) AS ts_us FROM reference_ticks GROUP BY asset ORDER BY asset"
    ).fetchall()
    ref_freshness = []
    for r in latest_ref:
        row = dict(r)
        ts = row.get("ts_us")
        row["age_seconds"] = None if ts is None else (now_us - int(ts)) / 1_000_000
        ref_freshness.append(row)

    latest_dec = conn.execute(
        "SELECT MAX(ts_us) AS ts_us FROM shadow_decisions"
    ).fetchone()

    return {
        "window": window,
        "now_us": now_us,
        "start_us": lo_us,
        "end_us": hi_us,
        "per_strategy": per_strategy,
        "totals": totals,
        "reference_freshness": ref_freshness,
        "latest_decision_ts_us": (latest_dec["ts_us"] if latest_dec else None),
    }


def _fetch_decisions(
    conn: sqlite3.Connection, *, strategy: str | None, limit: int,
    start_us: int | None = None, end_us: int | None = None,
) -> list[dict[str, Any]]:
    where = "WHERE recommended_side != 'none'"
    params: list[Any] = []
    if strategy:
        where += " AND strategy_label = ?"
        params.append(strategy)
    if start_us is not None:
        where += " AND ts_us >= ?"
        params.append(start_us)
    if end_us is not None:
        where += " AND ts_us <= ?"
        params.append(end_us)
    params.append(limit)
    sql = f"""
        SELECT market_ticker, ts_us, strategy_label, recommended_side,
               hypothetical_fill_price, hypothetical_size_contracts,
               expected_edge_bps_after_fees, time_remaining_s,
               realized_outcome, realized_pnl_usd
          FROM shadow_decisions
          {where}
         ORDER BY ts_us DESC
         LIMIT ?
    """
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _fetch_per_asset(
    conn: sqlite3.Connection, *, window: str = DEFAULT_WINDOW,
    start_us: int | None = None, end_us: int | None = None,
) -> list[dict[str, Any]]:
    window = _resolve_window(window)
    if start_us is None and end_us is None:
        lo_us = _window_cutoff_us(conn, window)
        hi_us: int | None = None
    else:
        lo_us, hi_us = start_us, end_us
    where_clauses = ["realized_outcome IS NOT NULL", "recommended_side != 'none'"]
    where_params: list[Any] = []
    if lo_us is not None:
        where_clauses.append("ts_us >= ?")
        where_params.append(lo_us)
    if hi_us is not None:
        where_clauses.append("ts_us <= ?")
        where_params.append(hi_us)
    where_sql = " WHERE " + " AND ".join(where_clauses)
    rows = conn.execute(
        f"""
        SELECT COALESCE(NULLIF(strategy_label, ''), ?) AS strategy_label,
               SUBSTR(market_ticker, 1, 8) AS series,
               COUNT(*) AS decisions,
               SUM(CASE WHEN CAST(realized_pnl_usd AS REAL) > 0 THEN 1 ELSE 0 END) AS wins,
               ROUND(SUM(CAST(realized_pnl_usd AS REAL)), 2) AS pnl
          FROM shadow_decisions
          {where_sql}
         GROUP BY COALESCE(NULLIF(strategy_label, ''), ?), series
         ORDER BY strategy_label, pnl DESC
        """,
        [_LEGACY_STRATEGY_LABEL, *where_params, _LEGACY_STRATEGY_LABEL],
    ).fetchall()
    return [dict(r) for r in rows]


def _fetch_paper_summary(
    conn: sqlite3.Connection, *,
    start_us: int | None = None, end_us: int | None = None,
) -> dict[str, Any]:
    fills_clause, fills_params = _range_clause(start_us, end_us, "filled_at_us")
    settle_clause, settle_params = _range_clause(
        start_us, end_us, "settled_at_us",
    )
    # `_range_clause` emits a leading ` AND ...`; tables here have no other
    # WHERE predicate so rewrite the leading AND to a leading WHERE.
    fills_where = fills_clause.replace(" AND ", " WHERE ", 1) if fills_clause else ""
    settle_where = settle_clause.replace(" AND ", " WHERE ", 1) if settle_clause else ""
    fills = conn.execute(
        f"""
        SELECT COUNT(*) AS n_fills,
               ROUND(SUM(CAST(notional_usd AS REAL)), 2) AS total_notional
          FROM paper_fills
          {fills_where}
        """,
        fills_params,
    ).fetchone()
    settlements = conn.execute(
        f"""
        SELECT COUNT(*) AS n_settlements,
               ROUND(SUM(CAST(realized_pnl_usd AS REAL)), 2) AS total_pnl
          FROM paper_settlements
          {settle_where}
        """,
        settle_params,
    ).fetchone()
    return {
        "fills": dict(fills) if fills else {"n_fills": 0, "total_notional": 0},
        "settlements": dict(settlements) if settlements else {"n_settlements": 0, "total_pnl": 0},
    }


def _fetch_live_summary(
    conn: sqlite3.Connection, *,
    start_us: int | None = None, end_us: int | None = None,
) -> dict[str, Any]:
    orders_clause, orders_params = _range_clause(
        start_us, end_us, "submitted_at_us",
    )
    settle_clause, settle_params = _range_clause(
        start_us, end_us, "settled_at_us",
    )
    orders_where = orders_clause.replace(" AND ", " WHERE ", 1) if orders_clause else ""
    settle_where = settle_clause.replace(" AND ", " WHERE ", 1) if settle_clause else ""
    orders = conn.execute(
        f"""
        SELECT status, COUNT(*) AS n
          FROM live_orders
          {orders_where}
         GROUP BY status
        """,
        orders_params,
    ).fetchall()
    settlements = conn.execute(
        f"""
        SELECT COUNT(*) AS n_settlements,
               ROUND(SUM(CAST(computed_pnl_usd AS REAL)), 2) AS total_computed,
               SUM(CASE WHEN CAST(discrepancy_usd AS REAL) != 0 THEN 1 ELSE 0 END) AS discrepancies
          FROM live_settlements
          {settle_where}
        """,
        settle_params,
    ).fetchone()
    return {
        "orders_by_status": [dict(r) for r in orders],
        "settlements": dict(settlements) if settlements else {
            "n_settlements": 0, "total_computed": 0, "discrepancies": 0,
        },
    }


def _percentile(values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile. `pct` is in [0, 100]."""
    if not values:
        return None
    vs = sorted(values)
    k = max(0, min(len(vs) - 1, int(round((pct / 100.0) * (len(vs) - 1)))))
    return vs[k]


def _fetch_phase_timings(
    events_dir: str | Path = "logs", *, max_lines: int = 100_000,
) -> dict[str, Any]:
    """Aggregate today's `phase_timing` events from the JSONL log.

    Reads the current day's `events_YYYY-MM-DD.jsonl`, filters to
    phase_timing events, groups by phase, and computes count /
    p50 / p95 / p99 / max / error_rate per phase. Cheap enough for
    tens of thousands of events — no indexing, no DB.
    """
    import json
    from observability.event_log import daily_log_path

    path = daily_log_path(events_dir)
    per_phase: dict[str, list[float]] = {}
    per_phase_errs: dict[str, int] = {}
    total = 0
    if not path.is_file():
        return {"phases": [], "source_path": str(path), "total_events": 0}
    with path.open("r") as f:
        for i, line in enumerate(f):
            if i >= max_lines:
                break
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("event_type") != "phase_timing":
                continue
            phase = row.get("phase")
            if not phase:
                continue
            ms = row.get("elapsed_ms")
            if not isinstance(ms, (int, float)):
                continue
            per_phase.setdefault(phase, []).append(float(ms))
            if not row.get("ok", True):
                per_phase_errs[phase] = per_phase_errs.get(phase, 0) + 1
            total += 1
    phases: list[dict[str, Any]] = []
    for phase, vals in sorted(per_phase.items()):
        n = len(vals)
        phases.append({
            "phase": phase,
            "count": n,
            "errors": per_phase_errs.get(phase, 0),
            "error_rate": round(per_phase_errs.get(phase, 0) / n, 4) if n else 0.0,
            "p50": _percentile(vals, 50),
            "p95": _percentile(vals, 95),
            "p99": _percentile(vals, 99),
            "max": max(vals) if vals else None,
        })
    # Sort by total time so the biggest budget-eaters land at the top.
    phases.sort(
        key=lambda p: (p["count"] * (p["p50"] or 0)), reverse=True,
    )
    return {"phases": phases, "source_path": str(path), "total_events": total}


def _fetch_phase_rollup(
    conn: sqlite3.Connection,
    *,
    window: str = DEFAULT_WINDOW,
    start_us: int | None = None,
    end_us: int | None = None,
    events_dir: str | Path = "logs",
) -> dict[str, Any]:
    """Window-aware phase-timing aggregates.

    Reads the `phase_timing_rollup` minute-bucket table when it has data
    in the requested window, otherwise falls back to the today-only
    JSONL aggregator. The fallback preserves the page even on fresh
    deploys (no rollup script yet) or sqlite DBs that were migrated
    before the table existed.

    Percentile caveat: when aggregating across minute buckets we
    approximate the cross-bucket p50/p95/p99 as `MAX(per-bucket-pN)`.
    This is an upper bound, not an exact percentile — noted on the page.
    `max_ms` and `count` are exact.
    """
    if start_us is None and end_us is None:
        cutoff = _window_cutoff_us(conn, window)
        upper = None
    else:
        cutoff, upper = start_us, end_us
    source = "rollup"
    rows: list[dict[str, Any]] = []
    try:
        rows = phase_timing_rollup.fetch(conn, since_us=cutoff, until_us=upper)
    except sqlite3.OperationalError as e:
        # Pre-migration DB — surface gracefully and fall back.
        if "no such table" not in str(e).lower():
            raise
        rows = []

    if not rows:
        # Rollup empty: legacy today's-JSONL aggregate so the page is
        # still useful on a fresh deploy. `source` flips to 'jsonl' so
        # the template can note the caveat.
        legacy = _fetch_phase_timings(events_dir)
        return {
            "window": window,
            "source": "jsonl",
            "phases": legacy["phases"],
            "total_events": legacy["total_events"],
            "source_path": legacy.get("source_path", ""),
        }

    # Shape to the same contract `_render_phases` expects.
    phases = [
        {
            "phase": r["phase"],
            "count": r["count"],
            "errors": r["errors"],
            "error_rate": r["error_rate"],
            "p50": r.get("p50_ms"),
            "p95": r.get("p95_ms"),
            "p99": r.get("p99_ms"),
            "max": r.get("max_ms"),
        }
        for r in rows
    ]
    total_events = sum(p["count"] for p in phases)
    return {
        "window": window,
        "source": source,
        "phases": phases,
        "total_events": total_events,
        "source_path": "phase_timing_rollup",
    }


def _fetch_ops(
    conn: sqlite3.Connection, *, window: str = DEFAULT_WINDOW,
    start_us: int | None = None, end_us: int | None = None,
) -> dict[str, Any]:
    """Latency percentiles, decisions/min rate, feed staleness.

    Latency is computed in Python since SQLite has no built-in percentile
    aggregate. For Phase-1 row volumes (~tens of thousands per day) this
    is cheap — well under 50ms for 24h windows in practice.
    """
    window = _resolve_window(window)
    if start_us is None and end_us is None:
        lo_us = _window_cutoff_us(conn, window)
        hi_us: int | None = None
    else:
        lo_us, hi_us = start_us, end_us
    now_us = _anchor_now_us(conn)

    where = ["recommended_side != 'none'"]
    params: list[Any] = []
    if lo_us is not None:
        where.append("ts_us >= ?")
        params.append(lo_us)
    if hi_us is not None:
        where.append("ts_us <= ?")
        params.append(hi_us)
    where_sql = " WHERE " + " AND ".join(where)

    rows = conn.execute(
        f"""
        SELECT latency_ms_ref_to_decision  AS ref_lat,
               latency_ms_book_to_decision AS book_lat
          FROM shadow_decisions
          {where_sql}
        """,
        params,
    ).fetchall()
    ref_lats = [float(r["ref_lat"]) for r in rows if r["ref_lat"] not in (None, "")]
    book_lats = [float(r["book_lat"]) for r in rows if r["book_lat"] not in (None, "")]

    def _summary(vs: list[float]) -> dict[str, Any]:
        return {
            "count": len(vs),
            "p50": _percentile(vs, 50),
            "p95": _percentile(vs, 95),
            "p99": _percentile(vs, 99),
            "max": max(vs) if vs else None,
        }

    # Decisions/min over the window — uses the same `ts_us` bounds as the
    # latency rows, so the rate reflects actual in-window scanner activity.
    span_rows = conn.execute(
        f"SELECT MIN(ts_us) AS lo, MAX(ts_us) AS hi, COUNT(*) AS n "
        f"FROM shadow_decisions {where_sql}",
        params,
    ).fetchone()
    dec_per_min: float | None = None
    if span_rows and span_rows["n"] and span_rows["lo"] and span_rows["hi"]:
        span_s = (int(span_rows["hi"]) - int(span_rows["lo"])) / 1_000_000
        if span_s > 0:
            dec_per_min = round(int(span_rows["n"]) / span_s * 60.0, 2)

    last_ref = conn.execute(
        "SELECT MAX(ts_us) AS ts FROM reference_ticks"
    ).fetchone()
    last_dec = conn.execute(
        "SELECT MAX(ts_us) AS ts FROM shadow_decisions"
    ).fetchone()

    def _age(ts: Any) -> float | None:
        if ts is None or ts == "":
            return None
        return (now_us - int(ts)) / 1_000_000

    events = _fetch_ops_events(
        conn, window=window, start_us=lo_us, end_us=hi_us, limit=50,
    )
    by_level = {"info": 0, "warn": 0, "error": 0}
    for e in events:
        lvl = e.get("level", "info")
        by_level[lvl] = by_level.get(lvl, 0) + 1

    return {
        "window": window,
        "now_us": now_us,
        "ref_to_decision_ms": _summary(ref_lats),
        "book_to_decision_ms": _summary(book_lats),
        "decisions_per_min": dec_per_min,
        "last_decision_age_s": _age(last_dec["ts"] if last_dec else None),
        "last_reference_tick_age_s": _age(last_ref["ts"] if last_ref else None),
        "total_decisions_in_window": span_rows["n"] if span_rows else 0,
        "events": events,
        "events_by_level": by_level,
    }


def _fetch_ops_events(
    conn: sqlite3.Connection, *, window: str = DEFAULT_WINDOW,
    start_us: int | None = None, end_us: int | None = None,
    min_level: str | None = None, limit: int = 50,
) -> list[dict[str, Any]]:
    """Pull recent ops_events rows respecting the selected window.

    The table is missing on older DBs (pre-`ops_events` migration) —
    swallow the `no such table` error so the ops page still renders on
    legacy DBs. This is the only table-missing case we tolerate; it
    never produces silently-wrong data, just an empty list.
    """
    if start_us is None and end_us is None:
        lo_us = _window_cutoff_us(conn, window)
        hi_us: int | None = None
    else:
        lo_us, hi_us = start_us, end_us
    try:
        return ops_events.read(
            conn, since_us=lo_us, until_us=hi_us,
            min_level=min_level, limit=limit,
        )
    except sqlite3.OperationalError as e:
        if "no such table" in str(e).lower():
            return []
        raise


def _fetch_health(
    conn: sqlite3.Connection, *, now_us: int,
    start_us: int | None = None, end_us: int | None = None,
) -> dict[str, Any]:
    range_clause, range_params = _range_clause(start_us, end_us)
    where_sql = range_clause.replace(" AND ", " WHERE ", 1) if range_clause else ""
    ref = conn.execute(
        f"""
        SELECT asset, MAX(ts_us) AS last_tick_us, COUNT(*) AS total_ticks
          FROM reference_ticks
          {where_sql}
         GROUP BY asset
         ORDER BY asset
        """,
        range_params,
    ).fetchall()
    dec = conn.execute(
        f"""
        SELECT strategy_label, MAX(ts_us) AS last_ts_us, COUNT(*) AS total
          FROM shadow_decisions
          {where_sql}
         GROUP BY strategy_label
         ORDER BY strategy_label
        """,
        range_params,
    ).fetchall()

    def _stale(row: dict, key: str) -> dict:
        ts = row.get(key)
        row["age_seconds"] = None if not ts else (now_us - int(ts)) / 1_000_000
        return row

    return {
        "reference": [_stale(dict(r), "last_tick_us") for r in ref],
        "decisions": [_stale(dict(r), "last_ts_us") for r in dec],
        "now_us": now_us,
    }


# ---------------------------------------------------------------------------
# HTML rendering — stdlib-templated, no frameworks
# ---------------------------------------------------------------------------


_BASE_CSS = """
  body { font-family: -apple-system, BlinkMacSystemFont, sans-serif;
         background: #0f1115; color: #e8e8e8; margin: 0; padding: 24px; }
  h1, h2 { color: #fff; font-weight: 600; }
  a { color: #58a6ff; text-decoration: none; }
  a:hover { text-decoration: underline; }
  nav { margin-bottom: 24px; padding: 12px 0; border-bottom: 1px solid #222; }
  nav a { margin-right: 16px; }
  table { border-collapse: collapse; width: 100%; margin: 16px 0; font-size: 13px; }
  th, td { padding: 6px 10px; text-align: left; border-bottom: 1px solid #222; }
  th { background: #161922; color: #9fb0c9; font-weight: 500; }
  tr:hover { background: #161922; }
  .card { background: #161922; padding: 16px 20px; border-radius: 8px;
          margin-bottom: 16px; border: 1px solid #222; }
  .pos { color: #3fb950; }
  .neg { color: #f85149; }
  .muted { color: #7a8499; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
          gap: 16px; }
  .big { font-size: 24px; font-weight: 600; }
  code { background: #0f1115; padding: 2px 6px; border-radius: 4px; font-size: 12px; }
  .refresh { font-size: 11px; color: #7a8499; }
  .tabs { font-size: 12px; color: #7a8499; margin: 0 0 16px 0; }
  .tabs a.tab { display: inline-block; padding: 3px 10px; margin-right: 4px;
                border: 1px solid #222; border-radius: 9999px; color: #9fb0c9; }
  .tabs a.tab.active { background: #1f2633; color: #fff; border-color: #2d3749; }
  .tabs a.tab:hover { text-decoration: none; background: #161922; }
  .kv { display: flex; justify-content: space-between; padding: 4px 0;
        border-bottom: 1px dashed #222; font-size: 13px; }
  .kv:last-child { border-bottom: none; }
  .kv .k { color: #9fb0c9; }
  .kv .v { font-family: ui-monospace, Menlo, monospace; }
  .range-form { display: flex; align-items: center; gap: 10px;
                padding: 8px 0 12px 0; font-size: 12px;
                border-bottom: 1px solid #222; margin-bottom: 16px;
                flex-wrap: wrap; }
  .range-form label { color: #9fb0c9; display: inline-flex; gap: 6px;
                       align-items: center; }
  .range-form input[type="datetime-local"] {
      background: #0f1115; color: #e8e8e8;
      border: 1px solid #2d3749; border-radius: 6px;
      padding: 4px 8px; font: inherit;
      color-scheme: dark; }
  .range-form button {
      background: #1f2633; color: #fff;
      border: 1px solid #2d3749; border-radius: 6px;
      padding: 4px 12px; cursor: pointer; }
  .range-form button:hover { background: #2a3243; }
  .range-form .range-clear { color: #7a8499; }
  .range-form .range-current { margin-left: 8px;
      font-family: ui-monospace, Menlo, monospace; font-size: 11px; }
"""


_DATETIME_LOCAL_LEN = len("YYYY-MM-DDTHH:MM")


def _to_datetime_local_value(raw: str | None) -> str:
    """Coerce a `start`/`end` URL value into the `YYYY-MM-DDTHH:MM` form the
    `<input type="datetime-local">` control accepts.

    Accepts anything `_parse_time_param_us` accepts; on failure returns `""`
    so the input renders empty rather than breaking the page.
    """
    if not raw:
        return ""
    try:
        us = _parse_time_param_us(raw)
    except ValueError:
        return ""
    if us is None:
        return ""
    from datetime import datetime, timezone
    dt = datetime.fromtimestamp(us / 1_000_000, tz=timezone.utc)
    # Use UTC; browsers render in local tz but submit what the user sees —
    # which means the server sees "user's local time as if UTC". Users who
    # want explicit UTC can type the ISO string with Z directly.
    return dt.strftime("%Y-%m-%dT%H:%M")


def _qs(extra: dict[str, str | None]) -> str:
    """Serialize a dict of string query params, dropping None/empty values."""
    from urllib.parse import urlencode
    kept = {k: v for k, v in extra.items() if v}
    return ("?" + urlencode(kept)) if kept else ""


_NAV_LINKS = [
    ("/kalshi",             "Overview"),
    ("/kalshi/decisions",   "Decisions"),
    ("/kalshi/performance", "Performance"),
    ("/kalshi/ops",         "Ops"),
    ("/kalshi/phases",      "Phases"),
    ("/kalshi/health",      "Health"),
    ("/kalshi/paper",       "Paper"),
    ("/kalshi/live",        "Live"),
]


def _nav(start: str | None = None, end: str | None = None) -> str:
    """Render the top nav + the date/time range filter form.

    `start` / `end` are the raw URL values for the current request; they
    persist across tabs (nav links carry them forward) and prefill the
    datetime-local inputs so the active range is visible.
    """
    from html import escape
    qs = _qs({"start": start, "end": end})
    links_html = "\n".join(
        f'      <a href="{path}{qs}">{label}</a>'
        for path, label in _NAV_LINKS
    )
    start_val = escape(_to_datetime_local_value(start))
    end_val = escape(_to_datetime_local_value(end))
    raw_start_preview = escape(start or "")
    raw_end_preview = escape(end or "")
    # The form submits GET to the CURRENT page (`location.pathname`) with
    # the range and any preserved `window`/`strategy`/`limit` params. We
    # use a tiny inline-JS onsubmit so we don't need a server-side hidden
    # field telling the form where it is.
    return f"""
    <nav>
{links_html}
      <span class="refresh">auto-refresh 10s</span>
    </nav>
    <form class="range-form" method="get" onsubmit="return _kalshiApplyRange(event)">
      <label>Start <input type="datetime-local" name="start" value="{start_val}"></label>
      <label>End   <input type="datetime-local" name="end"   value="{end_val}"></label>
      <button type="submit">Apply range</button>
      <a class="range-clear" href="#" onclick="return _kalshiClearRange()">Clear</a>
      <span class="range-current muted" title="raw URL values">
        {('start=' + raw_start_preview) if raw_start_preview else ''}
        {('&end=' + raw_end_preview) if raw_end_preview else ''}
      </span>
    </form>
    <script>
      function _kalshiApplyRange(ev) {{
        ev.preventDefault();
        const f = ev.target;
        const url = new URL(window.location.href);
        // Drop existing start/end so only the new values take effect.
        url.searchParams.delete('start');
        url.searchParams.delete('end');
        const s = f.start.value, e = f.end.value;
        // datetime-local emits `YYYY-MM-DDTHH:MM`; append `Z` so the server
        // interprets it as UTC (matches the value we prefilled).
        if (s) url.searchParams.set('start', s + 'Z');
        if (e) url.searchParams.set('end',   e + 'Z');
        window.location.href = url.toString();
        return false;
      }}
      function _kalshiClearRange() {{
        const url = new URL(window.location.href);
        url.searchParams.delete('start');
        url.searchParams.delete('end');
        window.location.href = url.toString();
        return false;
      }}
    </script>
    """


def _window_tabs(base_path: str, current: str) -> str:
    """Render the `5m · 15m · … · all` tab strip for window-aware pages."""
    current = _resolve_window(current)
    pills = []
    for w in WINDOWS:
        cls = "tab active" if w == current else "tab"
        pills.append(f'<a class="{cls}" href="{base_path}?window={w}">{w}</a>')
    return f'<div class="tabs">window: {" ".join(pills)}</div>'


def _fmt_age(seconds: float | None) -> str:
    if seconds is None:
        return '<span class="muted">—</span>'
    if seconds < 0:
        seconds = 0.0
    if seconds < 60:
        return f"{seconds:.1f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


# "EST" in trading parlance means America/New_York — i.e. auto-switches
# to EDT during DST. Using ZoneInfo keeps the label accurate year-round
# (the rendered suffix will read "EDT" in summer, "EST" in winter).
_EST_TZ_NAME = "America/New_York"


def _fmt_ts_est(us: Any) -> str:
    """Render a microsecond epoch as `YYYY-MM-DD HH:MM:SS TZ` in New York
    time. Returns an em-dash span for None / empty / unparseable input so
    table cells stay readable instead of raising into a 500."""
    if us is None or us == "":
        return '<span class="muted">—</span>'
    try:
        ts_us = int(us)
    except (TypeError, ValueError):
        return '<span class="muted">—</span>'
    from datetime import datetime, timezone
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(_EST_TZ_NAME)
    except Exception:  # noqa: BLE001 — tz database missing → fall back to UTC
        tz = timezone.utc
    dt = datetime.fromtimestamp(ts_us / 1_000_000, tz=timezone.utc).astimezone(tz)
    return dt.strftime("%Y-%m-%d %H:%M:%S %Z")


def _page(title: str, body_html: str, *, refresh_s: int = 10,
          start: str | None = None, end: str | None = None) -> str:
    return f"""<!doctype html>
<html><head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="{refresh_s}">
<title>{title} — Kalshi Scanner</title>
<style>{_BASE_CSS}</style>
</head><body>
<h1>Kalshi Scanner — {title}</h1>
{_nav(start=start, end=end)}
{body_html}
</body></html>"""


def _cell(v: Any, *, money: bool = False, bps: bool = False) -> str:
    if v is None or v == "":
        return '<span class="muted">—</span>'
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    cls = "pos" if f > 0 else ("neg" if f < 0 else "muted")
    if money:
        return f'<span class="{cls}">${f:+,.2f}</span>'
    if bps:
        return f'<span class="{cls}">{f:+.0f} bps</span>'
    return str(v)


def _render_wallet_card(w: WalletSnapshot | None) -> str:
    if w is None:
        return """
        <div class="card">
          <h2>Wallet</h2>
          <div class="muted">Not configured. Set <code>KALSHI_API_KEY_ID</code>
          + <code>KALSHI_PRIVATE_KEY_PATH</code> to enable.</div>
        </div>"""
    if w.error:
        return f"""
        <div class="card">
          <h2>Wallet</h2>
          <div class="neg">fetch failed: {w.error}</div>
          <div class="muted">source: {w.source}</div>
        </div>"""
    return f"""
    <div class="card">
      <h2>Wallet</h2>
      <div class="big">{_cell(w.balance_usd, money=True)}</div>
      <div class="muted">
        {w.positions_count if w.positions_count is not None else '—'} positions •
        notional {_cell(w.notional_usd, money=True)} •
        <span class="muted">source: {w.source}</span>
      </div>
    </div>"""


def _render_totals_card(t: dict[str, Any], window: str) -> str:
    n = t.get("total") or 0
    w = t.get("wins") or 0
    wr = (w / n * 100) if n else 0.0
    return f"""
    <div class="card">
      <h2>Totals ({window})</h2>
      <div class="big">{_cell(t.get('pnl'), money=True)}</div>
      <div class="muted">
        {n} decisions • {t.get('reconciled', 0)} reconciled •
        {w} wins ({wr:.1f}%) • per-dec {_cell(t.get('pnl_per_dec'), money=True)}
      </div>
    </div>"""


def _render_overview(d: dict[str, Any], wallet: WalletSnapshot | None) -> str:
    cards = [_render_totals_card(d.get("totals", {}), d.get("window", DEFAULT_WINDOW))]
    for s in d["per_strategy"]:
        label = s.get("strategy_label") or _LEGACY_STRATEGY_LABEL
        cards.append(f"""
        <div class="card">
          <h2>{label}</h2>
          <div class="big">{_cell(s.get('pnl'), money=True)}</div>
          <div class="muted">
            {s.get('total', 0)} decisions • {s.get('reconciled', 0)} reconciled •
            {s.get('wins', 0)} wins •
            per-dec {_cell(s.get('pnl_per_dec'), money=True)}
          </div>
        </div>""")
    cards.append(_render_wallet_card(wallet))

    ref_rows = "".join(
        f"<tr><td>{r['asset']}</td><td>{_fmt_age(r.get('age_seconds'))}</td>"
        f"<td>{_fmt_ts_est(r.get('ts_us'))}</td>"
        f"<td class=\"muted\">{r.get('ts_us') or '—'}</td></tr>"
        for r in d["reference_freshness"]
    )
    return f"""
    {_window_tabs('/kalshi', d.get('window', DEFAULT_WINDOW))}
    <div class="grid">{''.join(cards)}</div>
    <div class="card">
      <h2>Reference feed</h2>
      <table><thead><tr><th>asset</th><th>age</th>
      <th>last tick</th><th>ts_us</th></tr></thead>
      <tbody>{ref_rows}</tbody></table>
    </div>
    """


def _render_decisions(rows: list[dict], strategy: str | None) -> str:
    filter_note = f" (filter: <code>{strategy}</code>)" if strategy else ""
    head = ("<tr><th>datetime (ET)</th><th>ts_us</th><th>strategy</th>"
            "<th>ticker</th><th>side</th>"
            "<th>fill</th><th>size</th><th>edge (bps)</th>"
            "<th>t−remaining</th><th>outcome</th><th>P/L</th></tr>")
    tr = []
    for r in rows:
        tr.append(
            f"<tr><td>{_fmt_ts_est(r.get('ts_us'))}</td>"
            f"<td class=\"muted\">{r.get('ts_us','')}</td>"
            f"<td>{r.get('strategy_label') or '(none)'}</td>"
            f"<td>{r.get('market_ticker','')}</td>"
            f"<td>{r.get('recommended_side','')}</td>"
            f"<td>{r.get('hypothetical_fill_price','')}</td>"
            f"<td>{r.get('hypothetical_size_contracts','')}</td>"
            f"<td>{_cell(r.get('expected_edge_bps_after_fees'), bps=True)}</td>"
            f"<td>{r.get('time_remaining_s','')}</td>"
            f"<td>{r.get('realized_outcome') or '<span class=muted>pending</span>'}</td>"
            f"<td>{_cell(r.get('realized_pnl_usd'), money=True)}</td></tr>"
        )
    body = (
        f'<p>Showing {len(rows)} most-recent decisions{filter_note}. '
        'Filter by strategy: '
        '<a href="/kalshi/decisions?strategy=pure_lag">pure_lag</a> • '
        '<a href="/kalshi/decisions?strategy=stat_model">stat_model</a> • '
        '<a href="/kalshi/decisions?strategy=partial_avg">partial_avg</a> • '
        '<a href="/kalshi/decisions">all</a></p>'
        f'<div class="card"><table><thead>{head}</thead>'
        f'<tbody>{"".join(tr)}</tbody></table></div>'
    )
    return body


def _render_performance(rows: list[dict], window: str) -> str:
    head = ("<tr><th>strategy</th><th>series</th><th>decisions</th><th>wins</th>"
            "<th>win-rate</th><th>total P/L</th></tr>")
    tr = []
    for r in rows:
        n = r["decisions"] or 0
        w = r["wins"] or 0
        wr = (w / n * 100) if n else 0
        tr.append(
            f"<tr><td>{r['strategy_label'] or _LEGACY_STRATEGY_LABEL}</td>"
            f"<td>{r['series']}</td>"
            f"<td>{n}</td>"
            f"<td>{w}</td>"
            f"<td>{wr:.1f}%</td>"
            f"<td>{_cell(r.get('pnl'), money=True)}</td></tr>"
        )
    return (
        f"{_window_tabs('/kalshi/performance', window)}"
        '<div class="card">'
        '<h2>Per strategy × series (reconciled only)</h2>'
        f'<table><thead>{head}</thead><tbody>{"".join(tr)}</tbody></table>'
        '</div>'
    )


def _render_phase_timings_card(d: dict[str, Any]) -> str:
    """Compact top-phases card for inclusion on the ops page.

    The full per-phase table lives on `/kalshi/phases`; this is the
    summary teaser so operators see the biggest budget-eaters without
    switching tabs.
    """
    rows = d.get("phases", []) or []
    if not rows:
        return (
            '<div class="card"><h2>Phase timings</h2>'
            '<div class="muted">No <code>phase_timing</code> events yet. '
            'Runner emits these as it executes scanner phases — '
            'aggregated at <a href="/kalshi/phases">/kalshi/phases</a>.'
            '</div></div>'
        )
    # Top 5 by total budget (count × p50). Rows already sorted by the fetcher.
    top = rows[:5]
    body_rows = []
    for r in top:
        err_cls = "neg" if r.get("errors") else "muted"
        body_rows.append(
            f'<tr><td><code>{r["phase"]}</code></td>'
            f'<td>{r.get("count", 0)}</td>'
            f'<td class="{err_cls}">{r.get("errors", 0)}</td>'
            f'<td>{(r.get("p50") or 0):.2f}</td>'
            f'<td>{(r.get("p95") or 0):.2f}</td></tr>'
        )
    head = ("<tr><th>phase</th><th>count</th><th>errors</th>"
            "<th>p50 ms</th><th>p95 ms</th></tr>")
    return (
        '<div class="card">'
        '<h2>Phase timings (top 5)</h2>'
        f'<div class="muted" style="font-size:11px;">'
        f'{d.get("total_events", 0)} events · '
        'full breakdown on <a href="/kalshi/phases">/kalshi/phases</a></div>'
        f'<table><thead>{head}</thead><tbody>{"".join(body_rows)}</tbody></table>'
        '</div>'
    )


def _render_controls(
    flags: runtime_flags.RuntimeFlags | None, *, allow_write: bool,
) -> str:
    """Render the runtime-flags card.

    When `allow_write` is False the card shows current state read-only
    plus a hint on how to enable writes. When True, each row becomes a
    form POSTing to `/kalshi/ops/flags/*`. All toggles are bi-directional
    — including the execution kill-switch, which can be engaged AND
    released from the UI. Every change lands in `ops_events` for audit.
    """
    if flags is None:
        flags = runtime_flags.RuntimeFlags()
    def _row(label: str, on: bool, form_action: str | None, key: str) -> str:
        badge = ('<span class="pos">ON</span>' if on
                 else '<span class="neg">OFF</span>')
        if not allow_write or form_action is None:
            return (f'<div class="kv"><span class="k">{label}</span>'
                    f'<span class="v">{badge}</span></div>')
        # Desired state is the opposite of current.
        desired = "false" if on else "true"
        btn_label = "disable" if on else "enable"
        return (
            f'<form method="post" action="{form_action}" '
            f'style="display:flex;justify-content:space-between;'
            f'padding:4px 0;border-bottom:1px dashed #222;">'
            f'<span class="k">{label}</span>'
            f'<span class="v">'
            f'{badge}&nbsp;'
            f'<input type="hidden" name="name" value="{key}">'
            f'<input type="hidden" name="enabled" value="{desired}">'
            f'<button type="submit">{btn_label}</button>'
            f'</span></form>'
        )

    scan_rows = "".join(
        _row(
            f"scan · {asset}",
            flags.scan_enabled.get(asset, True),
            "/kalshi/ops/flags/scan",
            asset,
        )
        for asset in runtime_flags.ASSETS
    )
    strat_rows = "".join(
        _row(
            f"strategy · {strat}",
            flags.strategy_enabled.get(strat, True),
            "/kalshi/ops/flags/strategy",
            strat,
        )
        for strat in runtime_flags.STRATEGIES
    )
    exec_rows = "".join(
        _row(
            f"execution · {asset}",
            flags.execution_enabled.get(asset, True),
            "/kalshi/ops/flags/execution",
            asset,
        )
        for asset in runtime_flags.ASSETS
    )

    # Kill-switch: bi-directional. When engaged, the button REVIVES
    # execution; when idle, it ENGAGES. The 'neg'/'pos' badge makes the
    # current state unmistakable at a glance.
    if allow_write:
        if flags.execution_kill_switch:
            kill_control = (
                '<form method="post" action="/kalshi/ops/flags/unkill" '
                'style="display:flex;justify-content:space-between;'
                'padding:4px 0;border-bottom:1px dashed #222;">'
                '<span class="k">execution kill-switch</span>'
                '<span class="v"><span class="neg">ENGAGED</span>&nbsp;'
                '<button type="submit">REVIVE EXECUTION</button>'
                '</span></form>'
            )
        else:
            kill_control = (
                '<form method="post" action="/kalshi/ops/flags/kill" '
                'style="display:flex;justify-content:space-between;'
                'padding:4px 0;border-bottom:1px dashed #222;">'
                '<span class="k">execution kill-switch</span>'
                '<span class="v"><span class="pos">OFF</span>&nbsp;'
                '<button type="submit">KILL EXECUTION</button>'
                '</span></form>'
            )
    else:
        state = ('<span class="v neg">ENGAGED</span>'
                 if flags.execution_kill_switch
                 else '<span class="v pos">OFF</span>')
        kill_control = (
            f'<div class="kv"><span class="k">execution kill-switch</span>'
            f'{state}</div>'
        )

    write_hint = (
        '<div class="muted" style="font-size:11px;">write mode active · '
        f'last updated {flags.updated_at_us} µs by {flags.updated_by}</div>'
        if allow_write else
        '<div class="muted" style="font-size:11px;">read-only · set '
        '<code>DASHBOARD_ALLOW_WRITE=1</code> + restart to enable controls</div>'
    )

    return f"""
    <div class="card">
      <h2>Controls</h2>
      {write_hint}
      <h3 style="margin:12px 0 4px 0;font-size:13px;color:#9fb0c9;">Asset scan</h3>
      {scan_rows}
      <h3 style="margin:12px 0 4px 0;font-size:13px;color:#9fb0c9;">Strategies</h3>
      {strat_rows}
      <h3 style="margin:12px 0 4px 0;font-size:13px;color:#9fb0c9;">Execution — per asset</h3>
      {exec_rows}
      <h3 style="margin:12px 0 4px 0;font-size:13px;color:#9fb0c9;">Execution — global kill-switch</h3>
      {kill_control}
    </div>
    """


def _render_ops_events_table(events: list[dict[str, Any]]) -> str:
    if not events:
        return (
            '<div class="card"><h2>Events</h2>'
            '<div class="muted">No events in the selected window.</div></div>'
        )
    rows = []
    for e in events:
        level = e.get("level", "info")
        cls = {"error": "neg", "warn": "", "info": "muted"}.get(level, "muted")
        # `extras` is decoded by ops_events.read; render as a small inline
        # key-value list when present, else dash.
        extras = e.get("extras") or {}
        extras_str = (
            " · ".join(f"{k}={v}" for k, v in list(extras.items())[:4])
            if extras else ""
        )
        rows.append(
            f'<tr><td>{_fmt_ts_est(e.get("ts_us"))}</td>'
            f'<td class="muted">{e.get("ts_us")}</td>'
            f'<td>{e.get("source", "")}</td>'
            f'<td class="{cls}">{level}</td>'
            f'<td>{e.get("message", "")}</td>'
            f'<td class="muted">{extras_str}</td></tr>'
        )
    head = ("<tr><th>datetime (ET)</th><th>ts_us</th><th>source</th>"
            "<th>level</th><th>message</th><th>extras</th></tr>")
    return (
        '<div class="card"><h2>Events</h2>'
        f'<table><thead>{head}</thead><tbody>{"".join(rows)}</tbody></table>'
        '</div>'
    )


def _render_ops(d: dict[str, Any]) -> str:
    def _lat_rows(summary: dict[str, Any]) -> str:
        def _f(v: Any) -> str:
            return f"{float(v):.1f} ms" if v is not None else "—"
        return (
            f'<div class="kv"><span class="k">samples</span>'
            f'<span class="v">{summary.get("count", 0)}</span></div>'
            f'<div class="kv"><span class="k">p50</span>'
            f'<span class="v">{_f(summary.get("p50"))}</span></div>'
            f'<div class="kv"><span class="k">p95</span>'
            f'<span class="v">{_f(summary.get("p95"))}</span></div>'
            f'<div class="kv"><span class="k">p99</span>'
            f'<span class="v">{_f(summary.get("p99"))}</span></div>'
            f'<div class="kv"><span class="k">max</span>'
            f'<span class="v">{_f(summary.get("max"))}</span></div>'
        )

    def _health_class(age_s: float | None, warn: float, crit: float) -> str:
        if age_s is None:
            return "muted"
        if age_s >= crit:
            return "neg"
        if age_s >= warn:
            return ""
        return "pos"

    last_dec = d.get("last_decision_age_s")
    last_ref = d.get("last_reference_tick_age_s")
    dec_rate = d.get("decisions_per_min")
    # Controls intentionally rendered FIRST — operators should see the
    # toggles before scrolling through diagnostic data. Then service
    # status → latency → phase timings → event feed.
    return f"""
    {_window_tabs('/kalshi/ops', d.get('window', DEFAULT_WINDOW))}
    {_render_controls(d.get('flags'), allow_write=d.get('allow_write', False))}
    <div class="grid">
      <div class="card">
        <h2>Service status</h2>
        <div class="kv"><span class="k">last decision</span>
          <span class="v {_health_class(last_dec, 10, 60)}">{_fmt_age(last_dec)}</span></div>
        <div class="kv"><span class="k">last reference tick</span>
          <span class="v {_health_class(last_ref, 10, 60)}">{_fmt_age(last_ref)}</span></div>
        <div class="kv"><span class="k">decisions / min (in window)</span>
          <span class="v">{('—' if dec_rate is None else f'{dec_rate:.2f}')}</span></div>
        <div class="kv"><span class="k">decisions in window</span>
          <span class="v">{d.get('total_decisions_in_window', 0)}</span></div>
        <div class="kv"><span class="k">errors / warns / infos</span>
          <span class="v">
            <span class="neg">{d.get('events_by_level', {}).get('error', 0)}</span> /
            <span>{d.get('events_by_level', {}).get('warn', 0)}</span> /
            <span class="muted">{d.get('events_by_level', {}).get('info', 0)}</span>
          </span></div>
      </div>
      <div class="card">
        <h2>Latency — reference → decision</h2>
        {_lat_rows(d.get('ref_to_decision_ms', {}))}
      </div>
      <div class="card">
        <h2>Latency — book → decision</h2>
        {_lat_rows(d.get('book_to_decision_ms', {}))}
      </div>
    </div>
    {_render_phase_timings_card(d.get('phase_timings', {}))}
    {_render_ops_events_table(d.get('events', []))}
    """


def _render_phases(d: dict[str, Any]) -> str:
    """Per-phase latency table: biggest-budget-eaters first."""
    window = d.get("window", DEFAULT_WINDOW)
    source = d.get("source", "jsonl")
    window_tabs = (
        _window_tabs("/kalshi/phases", window) if source == "rollup" else ""
    )
    rows = d.get("phases", []) or []
    if not rows:
        return f"""
        {window_tabs}
        <div class="card"><h2>Phase timings</h2>
        <p class="muted">No events in <code>{d.get("source_path","")}</code> yet.
        The scanner emits <code>phase_timing</code> events — let it run for
        a minute or two then refresh.</p></div>
        """
    head = (
        "<tr><th>phase</th><th>count</th><th>errors</th>"
        "<th>p50 ms</th><th>p95 ms</th><th>p99 ms</th><th>max ms</th></tr>"
    )
    body_rows = []
    for r in rows:
        err_cls = "neg" if r["errors"] else "muted"
        body_rows.append(
            f'<tr><td><code>{r["phase"]}</code></td>'
            f'<td>{r["count"]}</td>'
            f'<td class="{err_cls}">{r["errors"]}</td>'
            f'<td>{(r["p50"] or 0):.3f}</td>'
            f'<td>{(r["p95"] or 0):.3f}</td>'
            f'<td>{(r["p99"] or 0):.3f}</td>'
            f'<td>{(r["max"] or 0):.3f}</td></tr>'
        )
    if source == "rollup":
        source_note = (
            f'Source: minute-bucket rollup table · window <code>{window}</code> · '
            f'{d.get("total_events", 0)} events. '
            'p50/p95/p99 across buckets are upper bounds; max is exact.'
        )
    else:
        source_note = (
            f'Source: <code>{d.get("source_path", "")}</code> · '
            f'{d.get("total_events", 0)} events today. '
            'Rollup table empty — falling back to JSONL. Run '
            '<code>scripts/rollup_phase_timings.py</code> to populate.'
        )
    return f"""
    {window_tabs}
    <div class="card">
      <h2>Phase timings</h2>
      <p class="muted">{source_note}</p>
      <table><thead>{head}</thead><tbody>{"".join(body_rows)}</tbody></table>
    </div>
    """


def _render_health(d: dict[str, Any]) -> str:
    def _stale_class(age: float | None) -> str:
        if age is None:
            return "muted"
        if age > 60:
            return "neg"
        if age > 10:
            return ""
        return "pos"

    ref_rows = "".join(
        f"<tr><td>{r['asset']}</td><td>{r['total_ticks']}</td>"
        f'<td class="{_stale_class(r.get("age_seconds"))}">'
        f'{r["age_seconds"]:.1f}s</td></tr>'
        if r.get("age_seconds") is not None else
        f"<tr><td>{r['asset']}</td><td>{r['total_ticks']}</td><td>—</td></tr>"
        for r in d["reference"]
    )
    dec_rows = "".join(
        f"<tr><td>{r['strategy_label'] or '(none)'}</td><td>{r['total']}</td>"
        f'<td class="{_stale_class(r.get("age_seconds"))}">'
        f'{r["age_seconds"]:.1f}s</td></tr>'
        if r.get("age_seconds") is not None else
        f"<tr><td>{r['strategy_label']}</td><td>{r['total']}</td><td>—</td></tr>"
        for r in d["decisions"]
    )
    return f"""
    <div class="grid">
      <div class="card">
        <h2>Reference feed age</h2>
        <table><thead><tr><th>asset</th><th>total ticks</th><th>age</th></tr></thead>
        <tbody>{ref_rows}</tbody></table>
      </div>
      <div class="card">
        <h2>Decision freshness</h2>
        <table><thead><tr><th>strategy</th><th>total</th><th>age</th></tr></thead>
        <tbody>{dec_rows}</tbody></table>
      </div>
    </div>
    """


def _render_paper(d: dict[str, Any]) -> str:
    f = d["fills"]
    s = d["settlements"]
    return f"""
    <div class="grid">
      <div class="card">
        <h2>Paper fills</h2>
        <div class="big">{f.get('n_fills', 0)}</div>
        <div class="muted">total notional {_cell(f.get('total_notional'), money=True)}</div>
      </div>
      <div class="card">
        <h2>Paper settlements</h2>
        <div class="big">{s.get('n_settlements', 0)}</div>
        <div class="muted">realized P/L {_cell(s.get('total_pnl'), money=True)}</div>
      </div>
    </div>
    <p class="muted">Paper executor is wired but not yet attached to the run loop. Expect zeros here until P2-M3 pipeline integration.</p>
    """


def _render_live(d: dict[str, Any]) -> str:
    status_rows = "".join(
        f"<tr><td>{r['status']}</td><td>{r['n']}</td></tr>"
        for r in d["orders_by_status"]
    ) or "<tr><td colspan=2 class=muted>no live orders yet</td></tr>"
    s = d["settlements"]
    return f"""
    <div class="grid">
      <div class="card">
        <h2>Live orders by status</h2>
        <table><thead><tr><th>status</th><th>n</th></tr></thead>
        <tbody>{status_rows}</tbody></table>
      </div>
      <div class="card">
        <h2>Live settlements</h2>
        <div class="big">{s.get('n_settlements', 0)}</div>
        <div class="muted">computed P/L {_cell(s.get('total_computed'), money=True)}</div>
        <div class="muted">{s.get('discrepancies', 0)} rows with Kalshi/local P/L mismatch</div>
      </div>
    </div>
    <p class="muted">Live executor is behind the three-opt-in gate and is currently off. Phase-1 no-go branch: this stays at zero until the gate flips.</p>
    """


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(
    *,
    database_url: str = "sqlite:///data/kalshi.db",
    balance_fetcher: BalanceFetcher | None = None,
    events_dir: str = "logs",
    flags_path: str | Path | None = None,
    allow_write: bool | None = None,
    username: str | None = None,
    password: str | None = None,
) -> FastAPI:
    """Build the read-only dashboard app.

    `balance_fetcher`: optional callable returning a `WalletSnapshot` (or
    None). Kept as a pluggable callable so tests stay DB-only and prod
    can swap in a 30s-cached Kalshi REST wrapper. If omitted, the wallet
    card renders a 'not configured' state.

    `events_dir`: directory where the scanner writes daily
    `events_YYYY-MM-DD.jsonl` files. The `/kalshi/phases` route reads
    today's file to aggregate phase_timing events.

    `flags_path`: path to the runtime flags JSON (default
    `config/runtime_flags.json`).

    `allow_write`: enables the ops-page control POSTs. `None` (default)
    consults `DASHBOARD_ALLOW_WRITE=1` from the process env.

    `username` / `password`: HTTP Basic credentials. If BOTH are set the
    app attaches a Basic-auth middleware guarding every route. If either
    is `None` the constructor falls back to env vars `DASHBOARD_USER`
    and `DASHBOARD_PASS`. If neither env nor args are set, auth is
    disabled (dev-friendly on localhost).
    """
    import os
    app = FastAPI(title="Kalshi Phase-1 Dashboard", docs_url=None, redoc_url=None)
    app.state.database_url = database_url
    app.state.balance_fetcher = balance_fetcher
    app.state.events_dir = events_dir
    app.state.flags_path = Path(flags_path) if flags_path else runtime_flags.DEFAULT_PATH
    if allow_write is None:
        allow_write = os.environ.get("DASHBOARD_ALLOW_WRITE") == "1"
    app.state.allow_write = bool(allow_write)

    # Env fallback for auth. We require BOTH to be present to enable —
    # a half-configured deployment (user but no password, or vice versa)
    # silently dropping to unauthenticated would be a nasty surprise.
    env_user = os.environ.get("DASHBOARD_USER")
    env_pass = os.environ.get("DASHBOARD_PASS")
    if username is None:
        username = env_user
    if password is None:
        password = env_pass
    if username and password:
        app.add_middleware(
            _basic_auth_middleware_factory(username, password),
        )
        app.state.auth_enabled = True
    else:
        app.state.auth_enabled = False

    def _db() -> sqlite3.Connection:
        return _open_readonly(app.state.database_url)

    def _wallet() -> WalletSnapshot | None:
        fetcher = app.state.balance_fetcher
        if fetcher is None:
            return None
        try:
            return fetcher()
        except Exception as exc:  # noqa: BLE001 — UI must survive fetch errors
            return WalletSnapshot(error=str(exc))

    def _range_from_query(
        start: str | None, end: str | None,
    ) -> tuple[int | None, int | None] | HTMLResponse:
        """Parse start/end params; 422 HTML on malformed input."""
        try:
            return (_parse_time_param_us(start), _parse_time_param_us(end))
        except ValueError as exc:
            return HTMLResponse(
                f"<h1>422 — bad time param</h1><pre>{exc}</pre>",
                status_code=422,
            )

    def _range_from_query_json(
        start: str | None, end: str | None,
    ) -> tuple[int | None, int | None] | JSONResponse:
        try:
            return (_parse_time_param_us(start), _parse_time_param_us(end))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/kalshi")

    @app.get("/kalshi", response_class=HTMLResponse)
    def overview(
        window: str = Query(default=DEFAULT_WINDOW),
        start: str | None = Query(default=None),
        end: str | None = Query(default=None),
    ) -> HTMLResponse:
        parsed = _range_from_query(start, end)
        if isinstance(parsed, HTMLResponse):
            return parsed
        start_us, end_us = parsed
        with _db() as conn:
            data = _fetch_overview(
                conn, window=window, start_us=start_us, end_us=end_us,
            )
        return HTMLResponse(_page(
            "Overview", _render_overview(data, _wallet()),
            start=start, end=end,
        ))

    @app.get("/kalshi/decisions", response_class=HTMLResponse)
    def decisions(
        strategy: str | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=500),
        start: str | None = Query(default=None),
        end: str | None = Query(default=None),
    ) -> HTMLResponse:
        parsed = _range_from_query(start, end)
        if isinstance(parsed, HTMLResponse):
            return parsed
        start_us, end_us = parsed
        with _db() as conn:
            rows = _fetch_decisions(
                conn, strategy=strategy, limit=limit,
                start_us=start_us, end_us=end_us,
            )
        return HTMLResponse(_page(
            "Decisions", _render_decisions(rows, strategy),
            start=start, end=end,
        ))

    @app.get("/kalshi/performance", response_class=HTMLResponse)
    def performance(
        window: str = Query(default=DEFAULT_WINDOW),
        start: str | None = Query(default=None),
        end: str | None = Query(default=None),
    ) -> HTMLResponse:
        parsed = _range_from_query(start, end)
        if isinstance(parsed, HTMLResponse):
            return parsed
        start_us, end_us = parsed
        window = _resolve_window(window)
        with _db() as conn:
            rows = _fetch_per_asset(
                conn, window=window, start_us=start_us, end_us=end_us,
            )
        return HTMLResponse(_page(
            "Performance", _render_performance(rows, window),
            start=start, end=end,
        ))

    @app.get("/kalshi/ops", response_class=HTMLResponse)
    def ops(
        window: str = Query(default=DEFAULT_WINDOW),
        start: str | None = Query(default=None),
        end: str | None = Query(default=None),
    ) -> HTMLResponse:
        parsed = _range_from_query(start, end)
        if isinstance(parsed, HTMLResponse):
            return parsed
        start_us, end_us = parsed
        with _db() as conn:
            data = _fetch_ops(
                conn, window=window, start_us=start_us, end_us=end_us,
            )
            # Window-aware phase timings from the rollup table, with a
            # graceful JSONL fallback (see `_fetch_phase_rollup`).
            try:
                data["phase_timings"] = _fetch_phase_rollup(
                    conn, window=window, start_us=start_us, end_us=end_us,
                    events_dir=app.state.events_dir,
                )
            except Exception:  # noqa: BLE001 — keep ops page renderable
                data["phase_timings"] = {"phases": [], "total_events": 0}
        data["flags"] = runtime_flags.load(app.state.flags_path)
        data["allow_write"] = app.state.allow_write
        return HTMLResponse(_page(
            "Ops", _render_ops(data), refresh_s=5, start=start, end=end,
        ))

    # --- ops controls: POST endpoints for runtime-flag toggles --------
    def _require_write() -> HTMLResponse | None:
        if not app.state.allow_write:
            return HTMLResponse(
                "<h1>403 — writes disabled</h1>"
                "<p>Set <code>DASHBOARD_ALLOW_WRITE=1</code> and restart "
                "the dashboard process to enable runtime controls.</p>",
                status_code=403,
            )
        return None

    @app.post("/kalshi/ops/flags/scan", response_class=HTMLResponse)
    async def flags_scan(request: Request) -> Any:
        denied = _require_write()
        if denied is not None:
            return denied
        body = await _urlencoded_form(request)
        name = body.get("name", "")
        enabled = body.get("enabled", "false")
        current = runtime_flags.load(app.state.flags_path)
        patched = runtime_flags.apply_dashboard_patch(
            current, {"scan_enabled": {name: enabled.lower() == "true"}},
        )
        runtime_flags.save(patched, app.state.flags_path, author="dashboard")
        return RedirectResponse("/kalshi/ops", status_code=303)

    @app.post("/kalshi/ops/flags/strategy", response_class=HTMLResponse)
    async def flags_strategy(request: Request) -> Any:
        denied = _require_write()
        if denied is not None:
            return denied
        body = await _urlencoded_form(request)
        name = body.get("name", "")
        enabled = body.get("enabled", "false")
        current = runtime_flags.load(app.state.flags_path)
        patched = runtime_flags.apply_dashboard_patch(
            current, {"strategy_enabled": {name: enabled.lower() == "true"}},
        )
        runtime_flags.save(patched, app.state.flags_path, author="dashboard")
        return RedirectResponse("/kalshi/ops", status_code=303)

    @app.post("/kalshi/ops/flags/kill", response_class=HTMLResponse)
    async def flags_kill() -> Any:
        denied = _require_write()
        if denied is not None:
            return denied
        current = runtime_flags.load(app.state.flags_path)
        patched = runtime_flags.apply_dashboard_patch(
            current, {"execution_kill_switch": True},
        )
        runtime_flags.save(patched, app.state.flags_path, author="dashboard")
        # Audit: every kill / revive action lands in ops_events so the
        # feed shows who flipped it when, alongside the operator's
        # reason (inferred from surrounding events).
        try:
            sink = ops_events.db_sink(app.state.database_url)
            sink("dashboard", "warn",
                 "execution_kill_switch engaged via dashboard",
                 {"author": "dashboard"})
        except Exception:  # noqa: BLE001 — audit log is best-effort
            pass
        return RedirectResponse("/kalshi/ops", status_code=303)

    @app.post("/kalshi/ops/flags/unkill", response_class=HTMLResponse)
    async def flags_unkill() -> Any:
        denied = _require_write()
        if denied is not None:
            return denied
        current = runtime_flags.load(app.state.flags_path)
        patched = runtime_flags.apply_dashboard_patch(
            current, {"execution_kill_switch": False},
        )
        runtime_flags.save(patched, app.state.flags_path, author="dashboard")
        try:
            sink = ops_events.db_sink(app.state.database_url)
            sink("dashboard", "warn",
                 "execution_kill_switch RELEASED via dashboard",
                 {"author": "dashboard"})
        except Exception:  # noqa: BLE001
            pass
        return RedirectResponse("/kalshi/ops", status_code=303)

    @app.post("/kalshi/ops/flags/execution", response_class=HTMLResponse)
    async def flags_execution(request: Request) -> Any:
        denied = _require_write()
        if denied is not None:
            return denied
        body = await _urlencoded_form(request)
        name = body.get("name", "")
        enabled = body.get("enabled", "false").lower() == "true"
        current = runtime_flags.load(app.state.flags_path)
        patched = runtime_flags.apply_dashboard_patch(
            current, {"execution_enabled": {name: enabled}},
        )
        runtime_flags.save(patched, app.state.flags_path, author="dashboard")
        try:
            sink = ops_events.db_sink(app.state.database_url)
            sink("dashboard", "info",
                 f"execution_enabled[{name}] → {enabled} via dashboard",
                 {"asset": name, "enabled": enabled})
        except Exception:  # noqa: BLE001
            pass
        return RedirectResponse("/kalshi/ops", status_code=303)

    @app.get("/kalshi/health", response_class=HTMLResponse)
    def health(
        start: str | None = Query(default=None),
        end: str | None = Query(default=None),
    ) -> HTMLResponse:
        import time as _t
        parsed = _range_from_query(start, end)
        if isinstance(parsed, HTMLResponse):
            return parsed
        start_us, end_us = parsed
        with _db() as conn:
            data = _fetch_health(
                conn, now_us=int(_t.time() * 1_000_000),
                start_us=start_us, end_us=end_us,
            )
        return HTMLResponse(_page(
            "Health", _render_health(data), refresh_s=5, start=start, end=end,
        ))

    @app.get("/kalshi/paper", response_class=HTMLResponse)
    def paper(
        start: str | None = Query(default=None),
        end: str | None = Query(default=None),
    ) -> HTMLResponse:
        parsed = _range_from_query(start, end)
        if isinstance(parsed, HTMLResponse):
            return parsed
        start_us, end_us = parsed
        with _db() as conn:
            data = _fetch_paper_summary(conn, start_us=start_us, end_us=end_us)
        return HTMLResponse(_page(
            "Paper", _render_paper(data), start=start, end=end,
        ))

    @app.get("/kalshi/live", response_class=HTMLResponse)
    def live(
        start: str | None = Query(default=None),
        end: str | None = Query(default=None),
    ) -> HTMLResponse:
        parsed = _range_from_query(start, end)
        if isinstance(parsed, HTMLResponse):
            return parsed
        start_us, end_us = parsed
        with _db() as conn:
            data = _fetch_live_summary(conn, start_us=start_us, end_us=end_us)
        return HTMLResponse(_page(
            "Live", _render_live(data), start=start, end=end,
        ))

    @app.get("/kalshi/phases", response_class=HTMLResponse)
    def phases(
        window: str = Query(default=DEFAULT_WINDOW),
        start: str | None = Query(default=None),
        end: str | None = Query(default=None),
    ) -> HTMLResponse:
        parsed = _range_from_query(start, end)
        if isinstance(parsed, HTMLResponse):
            return parsed
        start_us, end_us = parsed
        with _db() as conn:
            data = _fetch_phase_rollup(
                conn, window=window, start_us=start_us, end_us=end_us,
                events_dir=app.state.events_dir,
            )
        return HTMLResponse(_page(
            "Phases", _render_phases(data), start=start, end=end,
        ))

    @app.get("/api/phases")
    def api_phases(
        window: str = Query(default=DEFAULT_WINDOW),
        start: str | None = Query(default=None),
        end: str | None = Query(default=None),
    ) -> JSONResponse:
        parsed = _range_from_query_json(start, end)
        if isinstance(parsed, JSONResponse):
            return parsed
        start_us, end_us = parsed
        with _db() as conn:
            return JSONResponse(_fetch_phase_rollup(
                conn, window=window, start_us=start_us, end_us=end_us,
                events_dir=app.state.events_dir,
            ))

    # --- Control-plane JSON API for runtime flags --------------------------
    # HTML forms under /kalshi/ops/flags/* already exist for browser use;
    # these endpoints are the machine-readable equivalents for scripts,
    # CI jobs, and external dashboards.
    @app.get("/api/flags")
    def api_flags_get() -> JSONResponse:
        flags = runtime_flags.load(app.state.flags_path)
        return JSONResponse(flags.to_dict())

    @app.patch("/api/flags")
    async def api_flags_patch(request: Request) -> JSONResponse:
        if not app.state.allow_write:
            return JSONResponse(
                {"error": "writes disabled",
                 "hint": "set DASHBOARD_ALLOW_WRITE=1 and restart"},
                status_code=403,
            )
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — invalid JSON
            return JSONResponse(
                {"error": "invalid JSON body"}, status_code=422,
            )
        if not isinstance(body, dict):
            return JSONResponse(
                {"error": "body must be a JSON object"}, status_code=422,
            )
        current = runtime_flags.load(app.state.flags_path)
        patched = runtime_flags.apply_dashboard_patch(current, body)
        runtime_flags.save(
            patched, app.state.flags_path,
            author=str(body.get("_author", "api")),
        )
        # Side-channel audit event for any kill-switch flip.
        if not current.execution_kill_switch and patched.execution_kill_switch:
            try:
                sink = ops_events.db_sink(app.state.database_url)
                sink("dashboard", "warn",
                     "execution_kill_switch engaged via /api/flags",
                     {"author": body.get("_author", "api")})
            except Exception:  # noqa: BLE001 — audit log is best-effort
                pass
        return JSONResponse(patched.to_dict())

    # JSON APIs — useful for scraping / external dashboards / debugging.
    @app.get("/api/overview")
    def api_overview(
        window: str = Query(default=DEFAULT_WINDOW),
        start: str | None = Query(default=None),
        end: str | None = Query(default=None),
    ) -> JSONResponse:
        parsed = _range_from_query_json(start, end)
        if isinstance(parsed, JSONResponse):
            return parsed
        start_us, end_us = parsed
        with _db() as conn:
            payload = _fetch_overview(
                conn, window=window, start_us=start_us, end_us=end_us,
            )
        w = _wallet()
        payload["wallet"] = None if w is None else {
            "balance_usd": w.balance_usd,
            "positions_count": w.positions_count,
            "notional_usd": w.notional_usd,
            "source": w.source,
            "error": w.error,
        }
        return JSONResponse(payload)

    @app.get("/api/decisions")
    def api_decisions(
        strategy: str | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=500),
        start: str | None = Query(default=None),
        end: str | None = Query(default=None),
    ) -> JSONResponse:
        parsed = _range_from_query_json(start, end)
        if isinstance(parsed, JSONResponse):
            return parsed
        start_us, end_us = parsed
        with _db() as conn:
            return JSONResponse(_fetch_decisions(
                conn, strategy=strategy, limit=limit,
                start_us=start_us, end_us=end_us,
            ))

    @app.get("/api/performance")
    def api_performance(
        window: str = Query(default=DEFAULT_WINDOW),
        start: str | None = Query(default=None),
        end: str | None = Query(default=None),
    ) -> JSONResponse:
        parsed = _range_from_query_json(start, end)
        if isinstance(parsed, JSONResponse):
            return parsed
        start_us, end_us = parsed
        with _db() as conn:
            return JSONResponse(_fetch_per_asset(
                conn, window=window, start_us=start_us, end_us=end_us,
            ))

    @app.get("/api/ops")
    def api_ops(
        window: str = Query(default=DEFAULT_WINDOW),
        start: str | None = Query(default=None),
        end: str | None = Query(default=None),
    ) -> JSONResponse:
        parsed = _range_from_query_json(start, end)
        if isinstance(parsed, JSONResponse):
            return parsed
        start_us, end_us = parsed
        with _db() as conn:
            payload = _fetch_ops(
                conn, window=window, start_us=start_us, end_us=end_us,
            )
        payload["flags"] = runtime_flags.load(app.state.flags_path).to_dict()
        payload["allow_write"] = app.state.allow_write
        return JSONResponse(payload)

    @app.get("/api/health")
    def api_health(
        start: str | None = Query(default=None),
        end: str | None = Query(default=None),
    ) -> JSONResponse:
        import time as _t
        parsed = _range_from_query_json(start, end)
        if isinstance(parsed, JSONResponse):
            return parsed
        start_us, end_us = parsed
        with _db() as conn:
            return JSONResponse(_fetch_health(
                conn, now_us=int(_t.time() * 1_000_000),
                start_us=start_us, end_us=end_us,
            ))

    return app
