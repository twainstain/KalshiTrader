"""Lightweight structured-event emitter for operational signals.

The dashboard's `/kalshi/ops` view tails this table so retries, 5xx
bursts, WS disconnects and similar are visible without having to tail a
log file. Any process-local code path can `emit(...)` without touching
a DB connection itself — emit sites stay thin; the registered *sink*
handles persistence.

Design notes:
- Module-level sink registry keeps emit sites free of wiring. The main
  process (`run_kalshi_shadow.py`) calls `set_sink(db_sink(url))` once
  at startup; modules call `emit(source, level, message, extras)`.
- `db_sink` opens a short-lived connection per emit — cheap on SQLite
  under WAL and avoids cross-thread-connection pitfalls. Postgres
  backends get the same interface via the same helper.
- `emit` is fire-and-forget: any sink failure is swallowed and logged
  to `logging`. Broken telemetry must never take down the trading loop.
- Levels are strings. Dashboard filtering assumes `info` / `warn` /
  `error`, but this layer accepts anything — telemetry, not schema.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from typing import Any, Callable
from urllib.parse import urlparse


logger = logging.getLogger(__name__)

Sink = Callable[[str, str, str, dict | None], None]


# Valid levels. Enforced only at the sink edge; emit() still accepts any
# string so call sites don't break on a typo, but unknown levels are
# normalized to 'info' before persistence.
_KNOWN_LEVELS: frozenset[str] = frozenset({"info", "warn", "error"})


_lock = threading.Lock()
_sink: Sink | None = None


def set_sink(sink: Sink | None) -> None:
    """Install the process-wide sink. Pass `None` to disable emits.

    Thread-safe. Idempotent on identical values.
    """
    global _sink
    with _lock:
        _sink = sink


def current_sink() -> Sink | None:
    return _sink


def emit(
    source: str,
    level: str,
    message: str,
    extras: dict[str, Any] | None = None,
) -> None:
    """Emit a single ops event. Never raises.

    `source` is a short identifier of the emitter ("kalshi_rest",
    "basket_ws", "runner"). `level` is one of 'info' / 'warn' / 'error'.
    `extras` is an optional JSON-serializable context blob.
    """
    sink = _sink
    if sink is None:
        return
    try:
        sink(source, level, message, extras)
    except Exception as exc:  # noqa: BLE001 — telemetry must not crash caller
        logger.warning("ops_events emit failed: %s", exc)


def _normalize_level(level: str) -> str:
    return level if level in _KNOWN_LEVELS else "info"


def _sqlite_path_from_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("sqlite", ""):
        raise ValueError(f"ops_events db_sink only supports sqlite:// urls, got {url!r}")
    raw = parsed.path or url.removeprefix("sqlite://")
    if raw.startswith("//"):
        return raw[1:]
    return raw.lstrip("/") if raw.startswith("/") else raw


def db_sink(database_url: str, *, now_us: Callable[[], int] | None = None) -> Sink:
    """Return a sink that writes events to `database_url`.

    Currently sqlite-only — the runner speaks sqlite by default. Writes
    use short-lived connections so the sink is safe to call from any
    thread; WAL keeps concurrent writers from blocking each other under
    Phase-1 volumes.
    """
    path = _sqlite_path_from_url(database_url)
    _now = now_us or (lambda: int(time.time() * 1_000_000))

    def _write(source: str, level: str, message: str, extras: dict | None) -> None:
        conn = sqlite3.connect(path, isolation_level=None, timeout=5.0)
        try:
            # WAL so reads from the dashboard don't block this writer.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "INSERT INTO ops_events (ts_us, source, level, message, extras_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    _now(),
                    source,
                    _normalize_level(level),
                    message,
                    json.dumps(extras, default=str) if extras else "",
                ),
            )
        finally:
            conn.close()

    return _write


def read(
    conn: sqlite3.Connection,
    *,
    since_us: int | None = None,
    until_us: int | None = None,
    min_level: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Read recent events, most-recent first.

    `min_level='warn'` includes warn+error; `min_level='error'` only errors.
    """
    where: list[str] = []
    params: list[Any] = []
    if since_us is not None:
        where.append("ts_us >= ?")
        params.append(since_us)
    if until_us is not None:
        where.append("ts_us <= ?")
        params.append(until_us)
    if min_level == "warn":
        where.append("level IN ('warn','error')")
    elif min_level == "error":
        where.append("level = 'error'")
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    params.append(limit)

    sql = (
        "SELECT id, ts_us, source, level, message, extras_json "
        f"FROM ops_events{where_sql} "
        "ORDER BY ts_us DESC LIMIT ?"
    )
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    # Parse extras_json lazily so callers that don't need it skip the cost.
    for r in rows:
        r["extras"] = json.loads(r["extras_json"]) if r["extras_json"] else {}
    return rows
