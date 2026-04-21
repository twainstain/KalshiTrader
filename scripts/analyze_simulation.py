"""One-shot overnight-run analysis report.

Intended workflow:

    # Before bed:
    ./scripts/run_local.sh     # runs scanner + paper executor + dashboard

    # In the morning:
    python3.11 scripts/analyze_simulation.py
    # or:
    python3.11 scripts/analyze_simulation.py --window 8h --strategy pure_lag

Pulls from the same SQLite DB + events JSONL as the dashboard, but
returns a single text report instead of HTML — easier to diff between
nights and to paste into incident notes.

What lands in the report:
  1. Run horizon (first / last decision timestamp)
  2. Shadow-decision counts: written, reconciled, per-asset,
     per-strategy, realized-outcome distribution (yes / no / no_data)
  3. Paper executor: fills, settlements, realized P/L, win rate
  4. Top-10 most-active markets
  5. Risk-engine rejection counters (from events.jsonl risk_reject lines)
  6. Phase-timing top offenders (from events.jsonl phase_timing)
  7. Data gaps (reference tick staleness, decision gaps > 60s)

Read-only: opens the DB read-only, never writes.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_WINDOW_SECONDS = {
    "5m": 5 * 60, "15m": 15 * 60, "1h": 3600, "4h": 4 * 3600,
    "8h": 8 * 3600, "12h": 12 * 3600, "24h": 24 * 3600, "all": None,
}


@dataclass
class SimulationReport:
    """Structured summary a CLI can render as text or JSON."""
    window: str
    horizon_start_us: int | None = None
    horizon_end_us: int | None = None
    decisions_total: int = 0
    decisions_reconciled: int = 0
    decisions_by_strategy: dict[str, dict[str, int]] = field(default_factory=dict)
    decisions_by_asset: dict[str, dict[str, Any]] = field(default_factory=dict)
    outcome_counts: dict[str, int] = field(default_factory=dict)
    paper_fills: int = 0
    paper_settlements: int = 0
    paper_pnl_usd: float = 0.0
    paper_wins: int = 0
    paper_losses: int = 0
    top_markets: list[dict[str, Any]] = field(default_factory=list)
    risk_rejections: dict[str, int] = field(default_factory=dict)
    phase_timings: list[dict[str, Any]] = field(default_factory=list)
    reference_feed_staleness: dict[str, float | None] = field(default_factory=dict)
    decision_gap_seconds_p99: float | None = None


# ---------------------------------------------------------------------------
# Core builders (pure functions — no I/O of their own)
# ---------------------------------------------------------------------------


def build_report(
    conn: sqlite3.Connection,
    *,
    window: str = "all",
    strategy: str | None = None,
    now_us: int | None = None,
    events_path: Path | None = None,
) -> SimulationReport:
    """Assemble the SimulationReport from an open DB connection + JSONL file."""
    if window not in _WINDOW_SECONDS:
        raise ValueError(f"window={window!r} must be one of {list(_WINDOW_SECONDS)}")

    rep = SimulationReport(window=window)

    # --- anchor clock + window cutoff -------------------------------------
    # For window cutoffs, we anchor to the LATEST decision (so a historical
    # replay window stays stable). For staleness warnings we anchor to
    # wall-clock (so "last tick was X seconds ago" reflects now, not the
    # last time the scanner was writing). Two different semantics, two
    # different anchors.
    import time as _t
    wall_now_us = int(_t.time() * 1_000_000)
    if now_us is None:
        row = conn.execute(
            "SELECT MAX(ts_us) AS ts FROM shadow_decisions"
        ).fetchone()
        now_us = int(row[0]) if row and row[0] is not None else wall_now_us
    span_s = _WINDOW_SECONDS[window]
    cutoff = None if span_s is None else (now_us - span_s * 1_000_000)

    _where_parts = ["recommended_side != 'none'"]
    _params: list[Any] = []
    if cutoff is not None:
        _where_parts.append("ts_us >= ?")
        _params.append(cutoff)
    if strategy:
        _where_parts.append("strategy_label = ?")
        _params.append(strategy)
    where_sql = " WHERE " + " AND ".join(_where_parts)

    # --- horizon ----------------------------------------------------------
    r = conn.execute(
        f"SELECT MIN(ts_us), MAX(ts_us), COUNT(*), COUNT(realized_outcome) "
        f"FROM shadow_decisions {where_sql}",
        _params,
    ).fetchone()
    rep.horizon_start_us = int(r[0]) if r[0] is not None else None
    rep.horizon_end_us = int(r[1]) if r[1] is not None else None
    rep.decisions_total = int(r[2] or 0)
    rep.decisions_reconciled = int(r[3] or 0)

    # --- per-strategy breakdown -------------------------------------------
    rows = conn.execute(
        f"""
        SELECT COALESCE(NULLIF(strategy_label, ''), '(unlabeled)') AS label,
               COUNT(*) AS total,
               COUNT(realized_outcome) AS reconciled,
               SUM(CASE WHEN CAST(realized_pnl_usd AS REAL) > 0 THEN 1 ELSE 0 END) AS wins,
               ROUND(SUM(CAST(realized_pnl_usd AS REAL)), 2) AS pnl
          FROM shadow_decisions {where_sql}
         GROUP BY label
        """,
        _params,
    ).fetchall()
    for label, total, rec, wins, pnl in rows:
        rep.decisions_by_strategy[label] = {
            "total": int(total or 0),
            "reconciled": int(rec or 0),
            "wins": int(wins or 0),
            "pnl_usd": float(pnl or 0.0),
        }

    # --- per-asset (via market_ticker prefix) ------------------------------
    rows = conn.execute(
        f"""
        SELECT SUBSTR(market_ticker, 3, 3) AS asset,
               COUNT(*) AS total,
               SUM(CASE WHEN CAST(realized_pnl_usd AS REAL) > 0 THEN 1 ELSE 0 END) AS wins,
               ROUND(SUM(CAST(realized_pnl_usd AS REAL)), 2) AS pnl
          FROM shadow_decisions {where_sql}
         GROUP BY asset
         ORDER BY pnl DESC
        """,
        _params,
    ).fetchall()
    for asset, total, wins, pnl in rows:
        rep.decisions_by_asset[str(asset).lower()] = {
            "decisions": int(total or 0),
            "wins": int(wins or 0),
            "pnl_usd": float(pnl or 0.0),
        }

    # --- outcome distribution --------------------------------------------
    rows = conn.execute(
        f"""
        SELECT COALESCE(realized_outcome, 'pending') AS outcome, COUNT(*)
          FROM shadow_decisions {where_sql}
         GROUP BY outcome
        """,
        _params,
    ).fetchall()
    rep.outcome_counts = {str(r[0]): int(r[1]) for r in rows}

    # --- paper executor ---------------------------------------------------
    # Mirror the `strategy_label` filter applied to `shadow_decisions` so the
    # report stays internally consistent: `--strategy pure_lag` should not
    # mix pure_lag decision counts against paper fills from every strategy
    # that ran in the same window. `paper_fills.strategy_label` is the
    # direct column; `paper_settlements` has no label of its own, so we
    # join through `paper_fills.id = paper_settlements.fill_id`.
    _fill_parts: list[str] = []
    _fill_params: list[Any] = []
    if cutoff is not None:
        _fill_parts.append("filled_at_us >= ?")
        _fill_params.append(cutoff)
    if strategy:
        _fill_parts.append("strategy_label = ?")
        _fill_params.append(strategy)
    _fill_where = (" WHERE " + " AND ".join(_fill_parts)) if _fill_parts else ""
    rep.paper_fills = int(conn.execute(
        "SELECT COUNT(*) FROM paper_fills" + _fill_where,
        _fill_params,
    ).fetchone()[0] or 0)

    _set_parts: list[str] = []
    _set_params: list[Any] = []
    _set_join = ""
    if cutoff is not None:
        _set_parts.append("ps.settled_at_us >= ?")
        _set_params.append(cutoff)
    if strategy:
        # Join `paper_fills` for the label filter. Use a LEFT JOIN so
        # orphaned settlements (if any) don't silently drop out of the
        # count when `--strategy` is unset.
        _set_join = " JOIN paper_fills pf ON pf.id = ps.fill_id"
        _set_parts.append("pf.strategy_label = ?")
        _set_params.append(strategy)
    _set_where = (" WHERE " + " AND ".join(_set_parts)) if _set_parts else ""
    srow = conn.execute(
        "SELECT COUNT(*), ROUND(SUM(CAST(ps.realized_pnl_usd AS REAL)),2), "
        "       SUM(CASE WHEN CAST(ps.realized_pnl_usd AS REAL) > 0 THEN 1 ELSE 0 END), "
        "       SUM(CASE WHEN CAST(ps.realized_pnl_usd AS REAL) < 0 THEN 1 ELSE 0 END) "
        "  FROM paper_settlements ps" + _set_join + _set_where,
        _set_params,
    ).fetchone()
    rep.paper_settlements = int(srow[0] or 0)
    rep.paper_pnl_usd = float(srow[1] or 0.0)
    rep.paper_wins = int(srow[2] or 0)
    rep.paper_losses = int(srow[3] or 0)

    # --- top markets ------------------------------------------------------
    rows = conn.execute(
        f"""
        SELECT market_ticker, COUNT(*) AS n,
               ROUND(SUM(CAST(realized_pnl_usd AS REAL)),2) AS pnl
          FROM shadow_decisions {where_sql}
         GROUP BY market_ticker
         ORDER BY n DESC LIMIT 10
        """,
        _params,
    ).fetchall()
    rep.top_markets = [
        {"ticker": t, "decisions": int(n or 0), "pnl_usd": float(p or 0.0)}
        for t, n, p in rows
    ]

    # --- reference feed staleness (anchored to wall clock, not window now)
    rows = conn.execute(
        "SELECT asset, MAX(ts_us) FROM reference_ticks GROUP BY asset"
    ).fetchall()
    for asset, ts in rows:
        age_s = None if ts is None else (wall_now_us - int(ts)) / 1_000_000
        rep.reference_feed_staleness[str(asset).lower()] = age_s

    # --- decision-gap p99 (spots where the scanner went silent) ---------
    if rep.decisions_total > 1:
        dts = [r[0] for r in conn.execute(
            f"SELECT ts_us FROM shadow_decisions {where_sql} ORDER BY ts_us",
            _params,
        ).fetchall()]
        gaps = [(dts[i] - dts[i - 1]) / 1_000_000 for i in range(1, len(dts))]
        if gaps:
            gaps.sort()
            k = max(0, int(round(0.99 * (len(gaps) - 1))))
            rep.decision_gap_seconds_p99 = round(gaps[k], 2)

    # --- events JSONL rollups (risk rejections + phase timings) ----------
    if events_path is not None and events_path.is_file():
        reject_counts: Counter[str] = Counter()
        phase_vals: dict[str, list[float]] = {}
        with events_path.open() as f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                et = row.get("event_type")
                if et == "risk_reject":
                    # The reason string begins with "risk-rejected: rulename: ..."
                    reason = str(row.get("reason") or "")
                    if ":" in reason:
                        # "risk-rejected: min_edge_after_fees: edge 50..."
                        _, _, rest = reason.partition(":")
                        rule, _, _ = rest.strip().partition(":")
                        reject_counts[rule.strip()] += 1
                    else:
                        reject_counts["(unknown)"] += 1
                elif et == "phase_timing":
                    phase = row.get("phase")
                    ms = row.get("elapsed_ms")
                    if phase and isinstance(ms, (int, float)):
                        phase_vals.setdefault(phase, []).append(float(ms))
        rep.risk_rejections = dict(reject_counts)
        phases = []
        for phase, vals in phase_vals.items():
            vals.sort()
            n = len(vals)
            def _pct(pct):
                k = max(0, min(n - 1, int(round(pct / 100 * (n - 1)))))
                return vals[k]
            phases.append({
                "phase": phase,
                "count": n,
                "p50": round(_pct(50), 3),
                "p95": round(_pct(95), 3),
                "p99": round(_pct(99), 3),
                "max": round(vals[-1], 3),
            })
        # Sort by count × p50 — biggest budget eaters first.
        phases.sort(key=lambda p: p["count"] * p["p50"], reverse=True)
        rep.phase_timings = phases[:10]

    return rep


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _fmt_ts(us: int | None) -> str:
    if us is None:
        return "—"
    return datetime.fromtimestamp(us / 1_000_000, tz=timezone.utc).isoformat()


def _fmt_money(v: float) -> str:
    return f"${v:+,.2f}" if v else "$0.00"


def render_report(rep: SimulationReport) -> str:
    lines = []
    lines.append("=" * 72)
    lines.append(f"Kalshi Scanner Overnight Report — window={rep.window}")
    lines.append("=" * 72)

    # Horizon
    if rep.horizon_start_us and rep.horizon_end_us:
        span_us = rep.horizon_end_us - rep.horizon_start_us
        lines.append(
            f"Run horizon: {_fmt_ts(rep.horizon_start_us)} → "
            f"{_fmt_ts(rep.horizon_end_us)} "
            f"({span_us / 3_600_000_000:.1f} hrs)"
        )
    lines.append(
        f"Decisions: {rep.decisions_total} total • {rep.decisions_reconciled} reconciled"
    )

    # Per strategy
    if rep.decisions_by_strategy:
        lines.append("")
        lines.append("Per-strategy:")
        for label, s in sorted(rep.decisions_by_strategy.items()):
            wr = (s["wins"] / s["reconciled"] * 100) if s["reconciled"] else 0
            lines.append(
                f"  {label:<14s} {s['total']:>6d} dec  "
                f"{s['reconciled']:>5d} reconciled  "
                f"{s['wins']:>5d} wins ({wr:4.1f}%)  "
                f"P/L {_fmt_money(s['pnl_usd'])}"
            )

    # Per asset
    if rep.decisions_by_asset:
        lines.append("")
        lines.append("Per-asset:")
        for asset, a in sorted(
            rep.decisions_by_asset.items(), key=lambda kv: -kv[1]["pnl_usd"]
        ):
            lines.append(
                f"  {asset:<6s} {a['decisions']:>5d} dec  "
                f"{a['wins']:>5d} wins  P/L {_fmt_money(a['pnl_usd'])}"
            )

    # Outcomes
    if rep.outcome_counts:
        lines.append("")
        lines.append("Kalshi settlement outcomes:")
        for o, c in sorted(rep.outcome_counts.items()):
            lines.append(f"  {o:<10s} {c}")

    # Paper executor
    lines.append("")
    lines.append("Paper executor:")
    wr = (rep.paper_wins / rep.paper_settlements * 100) if rep.paper_settlements else 0
    lines.append(
        f"  fills={rep.paper_fills}  settlements={rep.paper_settlements}  "
        f"wins={rep.paper_wins}  losses={rep.paper_losses}  "
        f"WR={wr:.1f}%  P/L={_fmt_money(rep.paper_pnl_usd)}"
    )

    # Risk rejections
    if rep.risk_rejections:
        lines.append("")
        lines.append("Risk rejections (events.jsonl):")
        for rule, n in sorted(
            rep.risk_rejections.items(), key=lambda kv: -kv[1]
        ):
            lines.append(f"  {rule:<30s} {n}")

    # Top markets
    if rep.top_markets:
        lines.append("")
        lines.append("Top markets (most decisions):")
        for m in rep.top_markets:
            lines.append(
                f"  {m['ticker']:<24s} {m['decisions']:>4d} dec  "
                f"P/L {_fmt_money(m['pnl_usd'])}"
            )

    # Phase timings
    if rep.phase_timings:
        lines.append("")
        lines.append("Phase timings (top 10 by total time):")
        lines.append(f"  {'phase':<40s} {'count':>7s} {'p50':>8s} {'p95':>8s} {'p99':>8s}")
        for p in rep.phase_timings:
            lines.append(
                f"  {p['phase']:<40s} {p['count']:>7d} "
                f"{p['p50']:>8.3f} {p['p95']:>8.3f} {p['p99']:>8.3f}"
            )

    # Data gaps
    if rep.reference_feed_staleness:
        lines.append("")
        lines.append("Reference feed staleness (seconds since last tick):")
        for asset, age in sorted(rep.reference_feed_staleness.items()):
            label = "—" if age is None else f"{age:.1f}s"
            warn = " ⚠ STALE" if age is not None and age > 60 else ""
            lines.append(f"  {asset:<6s} {label}{warn}")

    if rep.decision_gap_seconds_p99 is not None:
        lines.append("")
        lines.append(
            f"Decision-gap p99: {rep.decision_gap_seconds_p99:.1f}s "
            "(gaps > 60s suggest scanner hiccups)"
        )

    lines.append("=" * 72)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _open_readonly(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default="data/kalshi.db",
                        help="Path to SQLite DB (default: data/kalshi.db)")
    parser.add_argument("--window",
                        choices=list(_WINDOW_SECONDS.keys()),
                        default="all")
    parser.add_argument("--strategy", default=None,
                        help="Restrict to a single strategy_label.")
    parser.add_argument("--events-dir", default="logs",
                        help="Directory for events_*.jsonl.")
    parser.add_argument("--json", action="store_true",
                        help="Emit JSON instead of the text report.")
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.is_file():
        print(f"DB not found: {db_path}", file=sys.stderr)
        return 2

    from observability.event_log import daily_log_path
    events_path = daily_log_path(args.events_dir)

    with _open_readonly(db_path) as conn:
        rep = build_report(
            conn,
            window=args.window,
            strategy=args.strategy,
            events_path=events_path if events_path.is_file() else None,
        )

    if args.json:
        import dataclasses
        print(json.dumps(dataclasses.asdict(rep), indent=2, default=str))
    else:
        print(render_report(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
