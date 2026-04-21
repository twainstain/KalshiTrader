"""Cover `src/observability/event_log.py`."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from observability.event_log import (
    EventLogger,
    NullEventLogger,
    _json_default,
    daily_log_path,
)


def _us(year: int, month: int, day: int, hour: int = 0, minute: int = 0,
        second: int = 0) -> int:
    """Helper: UTC datetime → microseconds since epoch."""
    return int(datetime(year, month, day, hour, minute, second,
                        tzinfo=timezone.utc).timestamp() * 1_000_000)


class TestDailyLogPath:
    def test_path_format(self, tmp_path):
        p = daily_log_path(tmp_path, now_us=_us(2026, 4, 20, 12))
        assert p.name == "events_2026-04-20.jsonl"
        assert p.parent == Path(tmp_path)

    def test_rotates_across_utc_midnight(self, tmp_path):
        day1 = daily_log_path(tmp_path, now_us=_us(2026, 4, 20, 23, 59, 59))
        day2 = daily_log_path(tmp_path, now_us=_us(2026, 4, 21, 0, 0, 0))
        assert day1.name == "events_2026-04-20.jsonl"
        assert day2.name == "events_2026-04-21.jsonl"

    def test_default_uses_wall_clock(self, tmp_path):
        # Exact name depends on current day; just assert format shape.
        p = daily_log_path(tmp_path)
        assert p.name.startswith("events_") and p.name.endswith(".jsonl")


class TestJsonDefault:
    def test_decimal_stringifies(self):
        assert _json_default(Decimal("0.55")) == "0.55"

    def test_tuple_becomes_list(self):
        assert _json_default(("a", "b")) == ["a", "b"]

    def test_set_becomes_list(self):
        out = _json_default({1, 2, 3})
        assert isinstance(out, list)
        assert sorted(out) == [1, 2, 3]

    def test_unknown_falls_back_to_str(self):
        class Weird:
            def __str__(self): return "weird"
        assert _json_default(Weird()) == "weird"


class TestEventLoggerExplicitPath:
    def test_writes_line_with_injected_ts_and_type(self, tmp_path):
        path = tmp_path / "events.jsonl"
        log = EventLogger(path=path, rotate_daily=False, now_us=lambda: 1_000_000)
        log.record("decision", asset="btc", side="yes")
        [line] = path.read_text().splitlines()
        row = json.loads(line)
        assert row == {
            "ts_us": 1_000_000, "event_type": "decision",
            "asset": "btc", "side": "yes",
        }

    def test_ts_us_reserved(self, tmp_path):
        log = EventLogger(path=tmp_path / "e.jsonl", rotate_daily=False)
        with pytest.raises(ValueError, match="reserved"):
            log.record("decision", ts_us=5)

    def test_event_type_double_raises_typeerror(self, tmp_path):
        """`event_type` is positional; passing it as kw raises Python's own TypeError."""
        log = EventLogger(path=tmp_path / "e.jsonl", rotate_daily=False)
        with pytest.raises(TypeError, match="event_type"):
            log.record("decision", event_type="other")

    def test_decimal_fields_serialize_as_strings(self, tmp_path):
        path = tmp_path / "e.jsonl"
        log = EventLogger(path=path, rotate_daily=False, now_us=lambda: 1)
        log.record("decision", edge_bps=Decimal("150"), price=Decimal("0.55"))
        row = json.loads(path.read_text().splitlines()[0])
        assert row["edge_bps"] == "150"
        assert row["price"] == "0.55"

    def test_multiple_events_append(self, tmp_path):
        path = tmp_path / "e.jsonl"
        log = EventLogger(path=path, rotate_daily=False, now_us=lambda: 1)
        log.record("decision", asset="btc")
        log.record("risk_reject", asset="eth")
        log.record("paper_fill", asset="xrp")
        lines = path.read_text().splitlines()
        assert [json.loads(l)["event_type"] for l in lines] \
            == ["decision", "risk_reject", "paper_fill"]


class TestEventLoggerDailyRotation:
    def test_writes_land_in_daily_file(self, tmp_path):
        ts = [_us(2026, 4, 20, 12)]
        log = EventLogger(base_dir=tmp_path, rotate_daily=True, now_us=lambda: ts[0])
        log.record("decision", asset="btc")

        expected = tmp_path / "events_2026-04-20.jsonl"
        assert expected.is_file()
        assert "btc" in expected.read_text()

    def test_rotation_moves_to_next_day(self, tmp_path):
        ts = [_us(2026, 4, 20, 23, 59)]
        log = EventLogger(base_dir=tmp_path, rotate_daily=True, now_us=lambda: ts[0])
        log.record("decision", asset="btc")
        ts[0] = _us(2026, 4, 21, 0, 1)
        log.record("decision", asset="eth")

        day1 = tmp_path / "events_2026-04-20.jsonl"
        day2 = tmp_path / "events_2026-04-21.jsonl"
        assert day1.is_file() and day2.is_file()
        assert "btc" in day1.read_text()
        assert "eth" in day2.read_text()

    def test_current_path_matches_now(self, tmp_path):
        log = EventLogger(base_dir=tmp_path, rotate_daily=True,
                          now_us=lambda: _us(2026, 4, 20, 12))
        assert log.current_path().name == "events_2026-04-20.jsonl"


class TestEventLoggerConcurrency:
    def test_many_threads_dont_corrupt_lines(self, tmp_path):
        # 20 threads × 50 events each = 1000 lines, all well-formed.
        log = EventLogger(path=tmp_path / "concurrent.jsonl",
                          rotate_daily=False, now_us=lambda: 42)
        N_THREADS, N_EACH = 20, 50

        def worker(tid: int) -> None:
            for i in range(N_EACH):
                log.record("decision", thread=tid, seq=i)

        threads = [threading.Thread(target=worker, args=(t,))
                   for t in range(N_THREADS)]
        for t in threads: t.start()
        for t in threads: t.join()

        lines = (tmp_path / "concurrent.jsonl").read_text().splitlines()
        assert len(lines) == N_THREADS * N_EACH
        # Every line parses as complete JSON.
        for line in lines:
            row = json.loads(line)
            assert row["event_type"] == "decision"


class TestEventLoggerConstructorValidation:
    def test_rejects_both_path_and_rotate(self, tmp_path):
        with pytest.raises(ValueError, match="can't set"):
            EventLogger(path=tmp_path / "x.jsonl", rotate_daily=True)

    def test_rejects_neither_path_nor_rotate(self, tmp_path):
        with pytest.raises(ValueError, match="either"):
            EventLogger(rotate_daily=False)


class TestNullEventLogger:
    def test_record_is_no_op(self):
        log = NullEventLogger()
        log.record("decision", asset="btc")  # must not raise
        assert log.current_path() == Path("/dev/null")
