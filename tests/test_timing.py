"""Cover `src/observability/timing.py::timed_phase`."""

from __future__ import annotations

import json
import time

import pytest

from observability.event_log import EventLogger, NullEventLogger
from observability.timing import timed_phase


def _read(path):
    return [json.loads(l) for l in path.read_text().splitlines()]


class TestTimedPhaseHappyPath:
    def test_emits_event_with_elapsed_and_ok_true(self, tmp_path):
        ev = EventLogger(path=tmp_path / "e.jsonl", rotate_daily=False,
                         now_us=lambda: 1)
        with timed_phase(ev, "scanner.tick", strategy="pure_lag"):
            time.sleep(0.01)  # ~10 ms

        [row] = _read(tmp_path / "e.jsonl")
        assert row["event_type"] == "phase_timing"
        assert row["phase"] == "scanner.tick"
        assert row["ok"] is True
        assert row["elapsed_ms"] >= 10.0   # at least the sleep
        assert row["elapsed_ms"] <= 100.0  # sanity upper bound
        assert row["context"] == {"strategy": "pure_lag"}

    def test_no_context_means_no_context_field(self, tmp_path):
        ev = EventLogger(path=tmp_path / "e.jsonl", rotate_daily=False,
                         now_us=lambda: 1)
        with timed_phase(ev, "simple_phase"):
            pass
        row = _read(tmp_path / "e.jsonl")[0]
        assert "context" not in row


class TestTimedPhaseExceptionPath:
    def test_records_ok_false_and_rethrows(self, tmp_path):
        ev = EventLogger(path=tmp_path / "e.jsonl", rotate_daily=False,
                         now_us=lambda: 1)
        with pytest.raises(ValueError, match="boom"):
            with timed_phase(ev, "phase.raises"):
                raise ValueError("boom")

        row = _read(tmp_path / "e.jsonl")[0]
        assert row["ok"] is False
        assert row["error_type"] == "ValueError"

    def test_keyboard_interrupt_still_records(self, tmp_path):
        ev = EventLogger(path=tmp_path / "e.jsonl", rotate_daily=False,
                         now_us=lambda: 1)
        with pytest.raises(KeyboardInterrupt):
            with timed_phase(ev, "phase.interrupted"):
                raise KeyboardInterrupt()
        row = _read(tmp_path / "e.jsonl")[0]
        assert row["ok"] is False
        assert row["error_type"] == "KeyboardInterrupt"


class TestTimedPhaseNestedSpans:
    def test_nested_spans_emit_innermost_first(self, tmp_path):
        ev = EventLogger(path=tmp_path / "e.jsonl", rotate_daily=False,
                         now_us=lambda: 1)
        with timed_phase(ev, "outer"):
            with timed_phase(ev, "inner"):
                time.sleep(0.001)

        rows = _read(tmp_path / "e.jsonl")
        assert [r["phase"] for r in rows] == ["inner", "outer"]
        # Outer must be >= inner (it includes inner + any overhead).
        assert rows[1]["elapsed_ms"] >= rows[0]["elapsed_ms"]


class TestTimedPhaseWithNullLogger:
    def test_null_logger_no_op(self):
        """NullEventLogger must not raise; `with` block runs as normal."""
        ran = []
        with timed_phase(NullEventLogger(), "phase.x"):
            ran.append(1)
        assert ran == [1]

    def test_none_logger_no_op(self):
        """Passing None explicitly also disables timing safely."""
        ran = []
        with timed_phase(None, "phase.x"):
            ran.append(1)
        assert ran == [1]


class TestTimedPhaseFailSoft:
    def test_logger_record_failure_does_not_crash(self, tmp_path):
        """If EventLogger.record raises, the user's code still completes."""
        class BrokenLogger:
            def record(self, *_args, **_kw):
                raise RuntimeError("disk full")
        ran = []
        with timed_phase(BrokenLogger(), "phase.x"):
            ran.append(1)
        assert ran == [1]
