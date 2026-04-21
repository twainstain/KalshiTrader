"""Minute-bucketed rollup of JSONL `phase_timing` events into SQL.

Why: storing one SQL row per phase_timing event gets expensive fast
(~10 phases × ~0.5 Hz ticks = 500k rows/day). A minute-bucket rollup
reduces that to ~22k rows/day while keeping the dashboard's window
tabs (5m / 15m / 1h / 4h / 24h / 7d) useful.

Architecture:
  - Scanner writes raw phase_timing events to `logs/events_YYYY-MM-DD.jsonl`
    via `observability.event_log.EventLogger` (unchanged).
  - A separate process (`scripts/rollup_phase_timings.py`) runs every
    minute under cron/systemd. It:
      1. Reads the trailing N minutes of JSONL (default 120).
      2. Filters to `event_type == "phase_timing"` events.
      3. Groups by (bucket_ts_us, phase) and computes count / errors /
         p50 / p95 / p99 / max.
      4. Upserts into `phase_timing_rollup`. Re-running over the same
         window produces the same final state — idempotent by design.
  - Dashboard queries this table directly, window-aware like every
    other ops view.

Bucket semantics: `bucket_ts_us = floor(ts_us / 60_000_000) * 60_000_000`.
A closed bucket is one whose start time is strictly less than the
current minute boundary. Open (in-progress) buckets are excluded from
writes so the numbers in the dashboard never flicker.

Retention: not enforced here. The rollup script has a `--retain-days`
flag that issues a DELETE for rows older than the cutoff; operators can
set it to whatever fits their disk budget.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse


BUCKET_SECONDS_DEFAULT = 60
LOOKBACK_MINUTES_DEFAULT = 120


@dataclass(frozen=True)
class RollupRow:
    bucket_ts_us: int
    bucket_seconds: int
    phase: str
    count: int
    errors: int
    total_elapsed_ms: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float

    def as_tuple(self) -> tuple[Any, ...]:
        return (
            self.bucket_ts_us, self.bucket_seconds, self.phase,
            self.count, self.errors, self.total_elapsed_ms,
            self.p50_ms, self.p95_ms, self.p99_ms, self.max_ms,
        )


def _percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile. Copy of the dashboard helper — kept
    local so this module can be imported without FastAPI."""
    if not values:
        return 0.0
    vs = sorted(values)
    k = max(0, min(len(vs) - 1, int(round((pct / 100.0) * (len(vs) - 1)))))
    return vs[k]


def _bucket_floor(ts_us: int, bucket_seconds: int) -> int:
    bucket_us = bucket_seconds * 1_000_000
    return (ts_us // bucket_us) * bucket_us


def aggregate_events(
    events: Iterable[dict[str, Any]],
    *,
    bucket_seconds: int = BUCKET_SECONDS_DEFAULT,
    since_us: int | None = None,
    exclude_from_us: int | None = None,
) -> list[RollupRow]:
    """Aggregate an iterable of JSON-decoded event dicts into rollup rows.

    `since_us`: lower bound on `ts_us` (inclusive). Events before this
    are dropped. Passing the latest bucket already in the DB makes the
    rollup incremental; passing `now - lookback` makes it a fixed-window
    re-aggregation.

    `exclude_from_us`: exclude any event whose *bucket* starts at or
    after this timestamp. Used to skip the in-progress bucket.
    """
    grouped: dict[tuple[int, str], tuple[list[float], int]] = {}
    for ev in events:
        if ev.get("event_type") != "phase_timing":
            continue
        ts_us = ev.get("ts_us")
        phase = ev.get("phase")
        ms = ev.get("elapsed_ms")
        if not isinstance(ts_us, int) or not isinstance(phase, str):
            continue
        if not isinstance(ms, (int, float)):
            continue
        if since_us is not None and ts_us < since_us:
            continue
        bucket = _bucket_floor(ts_us, bucket_seconds)
        if exclude_from_us is not None and bucket >= exclude_from_us:
            continue
        key = (bucket, phase)
        vs_errs = grouped.get(key)
        if vs_errs is None:
            vs_errs = ([], 0)
            grouped[key] = vs_errs
        vs, errs = vs_errs
        vs.append(float(ms))
        if not ev.get("ok", True):
            errs += 1
            grouped[key] = (vs, errs)

    rows: list[RollupRow] = []
    for (bucket, phase), (vs, errs) in grouped.items():
        if not vs:
            continue
        rows.append(RollupRow(
            bucket_ts_us=bucket,
            bucket_seconds=bucket_seconds,
            phase=phase,
            count=len(vs),
            errors=errs,
            total_elapsed_ms=round(sum(vs), 4),
            p50_ms=_percentile(vs, 50),
            p95_ms=_percentile(vs, 95),
            p99_ms=_percentile(vs, 99),
            max_ms=max(vs),
        ))
    rows.sort(key=lambda r: (r.bucket_ts_us, r.phase))
    return rows


def iter_jsonl(path: Path, *, max_lines: int = 10_000_000) -> Iterable[dict]:
    """Yield decoded JSON dicts from `path`, tolerating bad lines."""
    if not path.is_file():
        return
    with path.open("r") as f:
        for i, line in enumerate(f):
            if i >= max_lines:
                break
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except ValueError:
                # Skip malformed lines rather than crashing — event-log
                # writes are append-only but a crash mid-flush could leave
                # a truncated tail.
                continue


def collect_recent_events(
    events_dir: Path | str,
    *,
    since_us: int,
    now_us: int,
) -> Iterable[dict]:
    """Read events from today's (and yesterday's if the window crosses
    midnight UTC) JSONL files, yielding lines whose `ts_us >= since_us`.
    """
    import datetime as _dt
    base = Path(events_dir)
    dt_now = _dt.datetime.fromtimestamp(now_us / 1_000_000, tz=_dt.timezone.utc)
    paths: list[Path] = [base / f"events_{dt_now.strftime('%Y-%m-%d')}.jsonl"]
    dt_since = _dt.datetime.fromtimestamp(since_us / 1_000_000, tz=_dt.timezone.utc)
    if dt_since.date() != dt_now.date():
        paths.append(base / f"events_{dt_since.strftime('%Y-%m-%d')}.jsonl")
    for p in paths:
        for ev in iter_jsonl(p):
            ts = ev.get("ts_us")
            if isinstance(ts, int) and ts >= since_us:
                yield ev


def persist(conn: sqlite3.Connection, rows: Iterable[RollupRow]) -> int:
    """Upsert rollup rows. Returns the count written.

    Commits the write before returning — callers expect the upsert to be
    durable once this function succeeds (dashboard reads via a separate
    connection rely on this).
    """
    rows = list(rows)
    if not rows:
        return 0
    conn.executemany(
        """
        INSERT INTO phase_timing_rollup (
            bucket_ts_us, bucket_seconds, phase, count, errors,
            total_elapsed_ms, p50_ms, p95_ms, p99_ms, max_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(bucket_ts_us, bucket_seconds, phase) DO UPDATE SET
            count            = excluded.count,
            errors           = excluded.errors,
            total_elapsed_ms = excluded.total_elapsed_ms,
            p50_ms           = excluded.p50_ms,
            p95_ms           = excluded.p95_ms,
            p99_ms           = excluded.p99_ms,
            max_ms           = excluded.max_ms
        """,
        [r.as_tuple() for r in rows],
    )
    conn.commit()
    return len(rows)


def prune_older_than(
    conn: sqlite3.Connection, *, now_us: int, retain_days: int,
) -> int:
    """Delete rows with `bucket_ts_us` older than `retain_days` ago.
    Returns the count removed."""
    cutoff = now_us - retain_days * 86400 * 1_000_000
    cur = conn.execute(
        "DELETE FROM phase_timing_rollup WHERE bucket_ts_us < ?", (cutoff,),
    )
    return cur.rowcount or 0


def _sqlite_path_from_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("sqlite", ""):
        raise ValueError(
            f"phase_timing_rollup currently supports sqlite:// only — got {url!r}"
        )
    raw = parsed.path or url.removeprefix("sqlite://")
    if raw.startswith("//"):
        return raw[1:]
    return raw.lstrip("/") if raw.startswith("/") else raw


def run(
    events_dir: str | Path,
    database_url: str,
    *,
    bucket_seconds: int = BUCKET_SECONDS_DEFAULT,
    lookback_minutes: int = LOOKBACK_MINUTES_DEFAULT,
    retain_days: int | None = None,
    now_us: int | None = None,
) -> dict[str, int]:
    """Re-aggregate the trailing window and upsert. Idempotent.

    `now_us` override lets tests freeze the clock; production leaves it
    `None` and uses wall time.
    """
    now_us = now_us or int(time.time() * 1_000_000)
    since_us = now_us - lookback_minutes * 60 * 1_000_000
    current_bucket_start = _bucket_floor(now_us, bucket_seconds)

    events = collect_recent_events(events_dir, since_us=since_us, now_us=now_us)
    rows = aggregate_events(
        events,
        bucket_seconds=bucket_seconds,
        since_us=since_us,
        exclude_from_us=current_bucket_start,
    )

    path = _sqlite_path_from_url(database_url)
    conn = sqlite3.connect(path, isolation_level=None, timeout=10.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        written = persist(conn, rows)
        pruned = 0
        if retain_days is not None:
            pruned = prune_older_than(conn, now_us=now_us, retain_days=retain_days)
        return {
            "rows_written": written,
            "rows_pruned": pruned,
            "since_us": since_us,
            "current_bucket_start_us": current_bucket_start,
        }
    finally:
        conn.close()


def fetch(
    conn: sqlite3.Connection,
    *,
    since_us: int | None = None,
    until_us: int | None = None,
    bucket_seconds: int = BUCKET_SECONDS_DEFAULT,
    limit_phases: int | None = None,
) -> list[dict[str, Any]]:
    """Return per-phase aggregates over the window `[since_us, until_us]`.

    Aggregates across buckets: for each phase, emits count = sum of
    per-bucket counts, errors = sum, p50/p95/p99/max = MAX of the
    per-bucket percentiles (an upper bound — for true cross-bucket
    percentiles we'd need the raw values, which is exactly what we
    chose not to store). `max_ms` is exact.
    """
    where: list[str] = ["bucket_seconds = ?"]
    params: list[Any] = [bucket_seconds]
    if since_us is not None:
        where.append("bucket_ts_us >= ?")
        params.append(since_us)
    if until_us is not None:
        where.append("bucket_ts_us <= ?")
        params.append(until_us)
    where_sql = " WHERE " + " AND ".join(where)
    sql = f"""
        SELECT phase,
               SUM(count)       AS count,
               SUM(errors)      AS errors,
               SUM(total_elapsed_ms) AS total_elapsed_ms,
               -- Percentile-of-percentiles is an approximation; flagged
               -- in the docstring above. Good enough for "which phase
               -- is spiking today" dashboards.
               MAX(p50_ms)      AS p50_ms,
               MAX(p95_ms)      AS p95_ms,
               MAX(p99_ms)      AS p99_ms,
               MAX(max_ms)      AS max_ms
          FROM phase_timing_rollup
          {where_sql}
         GROUP BY phase
         ORDER BY (SUM(count) * COALESCE(MAX(p50_ms), 0)) DESC
    """
    if limit_phases is not None:
        sql += " LIMIT ?"
        params.append(limit_phases)
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    for r in rows:
        # Cast SUMs to ints where applicable.
        r["count"] = int(r["count"] or 0)
        r["errors"] = int(r["errors"] or 0)
        r["error_rate"] = (
            round(r["errors"] / r["count"], 4) if r["count"] else 0.0
        )
    return rows
