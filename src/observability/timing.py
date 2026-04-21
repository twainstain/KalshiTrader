"""Per-phase latency timing — context manager that emits JSONL events.

Every scanner and executor method that matters is wrapped in a
`timed_phase(...)` block. On exit, a `phase_timing` event lands in the
JSONL log with:

    {"ts_us": <end_us>,
     "event_type": "phase_timing",
     "phase": "scanner.snapshot_books",
     "elapsed_ms": 12.345,
     "ok": true,
     "context": {"tickers": 42}}

The dashboard's `/kalshi/phases` route reads these back and computes
p50/p95/p99 per phase. Aggregation is dashboard-side (not runtime) so
the scanner never pays for stats it might not need.

Design notes:
  - Uses `time.monotonic_ns()` for the elapsed measurement (monotonic
    clock, nanosecond precision) and `time.time()` in microseconds for
    the event timestamp (wall clock, to sort against other events).
  - Exceptions inside the `with` block propagate — the timer records
    `ok=False` and an `error_type` field before re-raising. The caller's
    behavior is preserved; observability is additive.
  - `NullEventLogger` is a valid argument; the timer becomes a no-op.
"""

from __future__ import annotations

import sys
import time
from contextlib import contextmanager
from typing import Any, Iterator


@contextmanager
def timed_phase(
    event_logger: Any,
    phase: str,
    **context: Any,
) -> Iterator[None]:
    """Record elapsed time + outcome of a code block as a `phase_timing` event.

    Usage:
        with timed_phase(ev, "scanner.tick", strategy="pure_lag"):
            evaluator.tick()

    Exceptions propagate — the event still records with `ok=False` and the
    exception's type name. Call-site observability is additive, not
    destructive.
    """
    if event_logger is None:
        # No logger wired — still yield so the calling code runs, but
        # don't waste the clock read.
        yield
        return

    start_ns = time.monotonic_ns()
    try:
        yield
    finally:
        elapsed_ns = time.monotonic_ns() - start_ns
        elapsed_ms = elapsed_ns / 1_000_000.0
        # Inspect the current exception (if any) via sys.exc_info(). This
        # catches BOTH `Exception` subclasses and BaseException (KI, SysExit)
        # without needing a bare `except:` — timing records for every
        # interrupted phase too.
        exc_type = sys.exc_info()[0]
        ok = exc_type is None
        payload: dict[str, Any] = {
            "phase": phase,
            "elapsed_ms": round(elapsed_ms, 4),
            "ok": ok,
        }
        if exc_type is not None:
            payload["error_type"] = exc_type.__name__
        if context:
            payload["context"] = context
        try:
            event_logger.record("phase_timing", **payload)
        except Exception:  # noqa: BLE001 — never let timing crash a caller
            pass
