"""Append-only JSONL event log for latency + debug analysis.

Design mirrors the pattern used in `/Users/tamir.wainstain/src/ArbitrageTrader/logs`
— one structured JSONL file per day alongside the plain-text log. Each
line is a self-contained JSON object: easy to grep, easy to replay,
easy to consume from pandas (`pd.read_json(path, lines=True)`).

Not a replacement for the `shadow_decisions` / `paper_fills` tables —
those remain the system of record for decisions and fills. The event
log captures **transient state** that never lands in the DB: risk-rule
rejections, feed-health blips, per-tick timing.

Example event line:
    {"ts_us": 1746000000123456, "event_type": "decision",
     "strategy_label": "pure_lag", "asset": "btc",
     "market_ticker": "KXBTC15M-T1", "side": "yes",
     "edge_bps": "150", "latency_book_ms": 12.3, "latency_ref_ms": 8.1}

Thread-safe via a single `threading.Lock`. One writer instance shared
across the whole process; re-entering `record()` from multiple threads
is fine. File handles are opened per-write rather than held open — the
write rate (≤ 100 events/sec in practice) doesn't justify the extra
complexity of a long-lived handle.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable


logger = logging.getLogger(__name__)


def _json_default(obj: Any) -> Any:
    """JSON-serialize Decimal → string, datetimes → ISO, tuples → list."""
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, (set, frozenset, tuple)):
        return list(obj)
    return str(obj)


def daily_log_path(base_dir: str | Path = "logs", *,
                   now_us: int | None = None) -> Path:
    """`logs/events_YYYY-MM-DD.jsonl` — rotates at UTC midnight.

    `now_us` override lets callers anchor to a frozen clock (tests).
    """
    if now_us is None:
        now_us = int(time.time() * 1_000_000)
    dt = datetime.fromtimestamp(now_us / 1_000_000, tz=timezone.utc)
    return Path(base_dir) / f"events_{dt.strftime('%Y-%m-%d')}.jsonl"


@dataclass
class EventLoggerConfig:
    base_dir: str = "logs"
    # Rotate daily by default. If `False`, the constructor's `path` is used
    # for every write (useful for one-off replays or tests).
    rotate_daily: bool = True


class EventLogger:
    """Append-only structured event writer.

    Two construction modes:
      - `EventLogger(path=…)` — fixed file; every `record()` appends there.
      - `EventLogger(base_dir=…, rotate_daily=True)` — `daily_log_path()`
        computes the current day's file on every write.

    `record(event_type, **fields)` merges `ts_us` (injected by `now_us`)
    and `event_type` into the JSON line. All values are stringified via
    `_json_default` so Decimal / datetime round-trip without special
    handling at call sites.
    """

    def __init__(
        self,
        *,
        path: str | Path | None = None,
        base_dir: str | Path = "logs",
        rotate_daily: bool = True,
        now_us: Callable[[], int] | None = None,
    ) -> None:
        if path is not None and rotate_daily:
            raise ValueError(
                "can't set `path` and `rotate_daily=True` together — "
                "daily rotation computes its own path"
            )
        if path is None and not rotate_daily:
            raise ValueError(
                "must set either `path` or `rotate_daily=True`"
            )
        self._explicit_path = Path(path) if path is not None else None
        self._base_dir = Path(base_dir)
        self._rotate_daily = rotate_daily
        self._now_us = now_us or (lambda: int(time.time() * 1_000_000))
        self._lock = threading.Lock()
        # Ensure dir exists up front — cheap, idempotent.
        self._base_dir.mkdir(parents=True, exist_ok=True)
        if self._explicit_path is not None:
            self._explicit_path.parent.mkdir(parents=True, exist_ok=True)

    def current_path(self, *, now_us: int | None = None) -> Path:
        """Return the file that `record()` would write to right now."""
        if self._explicit_path is not None:
            return self._explicit_path
        return daily_log_path(
            self._base_dir,
            now_us=now_us if now_us is not None else self._now_us(),
        )

    def record(self, event_type: str, **fields: Any) -> None:
        """Append one JSONL record. Callers pass strongly-typed fields.

        `ts_us` is injected by the logger — passing it in `fields` is a
        caller bug and will raise. (`event_type` is a positional-only
        concern — Python's own TypeError covers that case.)
        """
        if "ts_us" in fields:
            raise ValueError("`ts_us` is reserved — don't pass it")
        now_us = self._now_us()
        payload: dict[str, Any] = {
            "ts_us": now_us,
            "event_type": event_type,
        }
        payload.update(fields)
        try:
            line = json.dumps(payload, default=_json_default,
                              separators=(",", ":"))
        except Exception as e:  # noqa: BLE001 — never let logging crash a tick
            logger.warning("event_log serialize failed (%s): %r", e, payload)
            return
        path = self.current_path(now_us=now_us)
        with self._lock:
            try:
                with path.open("a") as f:
                    f.write(line + "\n")
            except OSError as e:
                logger.warning("event_log write failed to %s: %s", path, e)


class NullEventLogger:
    """Drop-in replacement that records nothing. Default when disabled."""

    def current_path(self, *, now_us: int | None = None) -> Path:
        return Path("/dev/null")

    def record(self, event_type: str, **fields: Any) -> None:  # noqa: D401
        return None
