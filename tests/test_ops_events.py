"""Coverage for `ops_events` emit / sink / read + migration."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest

import ops_events


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_sink():
    """Every test starts with no sink — prevents cross-test pollution."""
    ops_events.set_sink(None)
    yield
    ops_events.set_sink(None)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Fresh DB with all tables migrated."""
    import migrate_db as m
    db = tmp_path / "ops.db"
    m.migrate(f"sqlite:///{db}")
    return db


# ---------------------------------------------------------------------------
# Migration: table + indexes exist
# ---------------------------------------------------------------------------


class TestMigration:
    def test_table_and_indexes_created(self, db_path: Path):
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            names = {
                r["name"] for r in conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type IN ('table','index')"
                )
            }
        finally:
            conn.close()
        assert "ops_events" in names
        assert "idx_ops_ts" in names
        assert "idx_ops_level_ts" in names

    def test_migration_idempotent(self, db_path: Path, tmp_path: Path):
        import migrate_db as m
        # Running migrate twice must not raise.
        m.migrate(f"sqlite:///{db_path}")
        m.migrate(f"sqlite:///{db_path}")


# ---------------------------------------------------------------------------
# emit() / set_sink() contract
# ---------------------------------------------------------------------------


class TestEmit:
    def test_emit_no_sink_is_noop(self):
        # Should not raise — this is the most important guarantee.
        ops_events.emit("runner", "info", "nothing wired", {"x": 1})

    def test_emit_delivers_to_sink(self):
        captured: list[tuple] = []
        ops_events.set_sink(
            lambda s, l, m, e: captured.append((s, l, m, e))
        )
        ops_events.emit("runner", "info", "hello", {"k": 1})
        assert captured == [("runner", "info", "hello", {"k": 1})]

    def test_emit_swallows_sink_exceptions(self):
        def broken(*_a, **_k):
            raise RuntimeError("sink down")
        ops_events.set_sink(broken)
        # Must not propagate — telemetry can never crash a caller.
        ops_events.emit("runner", "error", "oops")

    def test_set_sink_none_disables(self):
        captured: list[tuple] = []
        ops_events.set_sink(
            lambda *a: captured.append(a)
        )
        ops_events.emit("a", "info", "yes")
        ops_events.set_sink(None)
        ops_events.emit("a", "info", "no")
        assert len(captured) == 1

    def test_current_sink_reflects_registration(self):
        def s(*_a): ...
        ops_events.set_sink(s)
        assert ops_events.current_sink() is s


# ---------------------------------------------------------------------------
# db_sink — SQLite round-trip
# ---------------------------------------------------------------------------


class TestDBSink:
    def test_db_sink_writes_row(self, db_path: Path):
        sink = ops_events.db_sink(f"sqlite:///{db_path}")
        sink("kalshi_rest", "error", "500 from API", {"status": 500})
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT source, level, message, extras_json FROM ops_events"
            ).fetchone()
        finally:
            conn.close()
        assert row["source"] == "kalshi_rest"
        assert row["level"] == "error"
        assert row["message"] == "500 from API"
        assert json.loads(row["extras_json"]) == {"status": 500}

    def test_db_sink_normalizes_unknown_level(self, db_path: Path):
        sink = ops_events.db_sink(f"sqlite:///{db_path}")
        sink("x", "INVALID", "msg", None)
        conn = sqlite3.connect(str(db_path))
        try:
            level = conn.execute("SELECT level FROM ops_events").fetchone()[0]
        finally:
            conn.close()
        assert level == "info"  # normalized, never dropped.

    def test_db_sink_rejects_non_sqlite_url(self):
        with pytest.raises(ValueError):
            ops_events.db_sink("postgresql://foo/bar")

    def test_db_sink_concurrent_writes(self, db_path: Path):
        """Two threads writing simultaneously must both land safely (WAL)."""
        sink = ops_events.db_sink(f"sqlite:///{db_path}")

        def burst(source: str):
            for i in range(20):
                sink(source, "info", f"{source}-{i}", None)

        threads = [
            threading.Thread(target=burst, args=(src,))
            for src in ("a", "b", "c")
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        conn = sqlite3.connect(str(db_path))
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM ops_events"
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 60

    def test_db_sink_writes_empty_extras_as_empty_string(self, db_path: Path):
        sink = ops_events.db_sink(f"sqlite:///{db_path}")
        sink("x", "info", "no extras", None)
        conn = sqlite3.connect(str(db_path))
        try:
            extras = conn.execute(
                "SELECT extras_json FROM ops_events"
            ).fetchone()[0]
        finally:
            conn.close()
        assert extras == ""


# ---------------------------------------------------------------------------
# read() — filtering + ordering
# ---------------------------------------------------------------------------


class TestRead:
    @pytest.fixture
    def seeded_conn(self, db_path: Path):
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = [
            (1000, "kalshi_rest", "info",  "hello",  '{"a":1}'),
            (2000, "basket_ws",   "warn",  "stale",  ""),
            (3000, "kalshi_rest", "error", "500",    '{"s":500}'),
            (4000, "runner",      "info",  "started", ""),
        ]
        conn.executemany(
            "INSERT INTO ops_events (ts_us, source, level, message, extras_json) "
            "VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
        yield conn
        conn.close()

    def test_read_default_returns_newest_first(self, seeded_conn):
        rows = ops_events.read(seeded_conn)
        # Newest ts_us first.
        assert [r["ts_us"] for r in rows] == [4000, 3000, 2000, 1000]

    def test_read_since_us_filter(self, seeded_conn):
        rows = ops_events.read(seeded_conn, since_us=2500)
        assert {r["ts_us"] for r in rows} == {3000, 4000}

    def test_read_min_level_warn(self, seeded_conn):
        rows = ops_events.read(seeded_conn, min_level="warn")
        assert {r["level"] for r in rows} == {"warn", "error"}

    def test_read_min_level_error(self, seeded_conn):
        rows = ops_events.read(seeded_conn, min_level="error")
        assert all(r["level"] == "error" for r in rows)
        assert len(rows) == 1

    def test_read_limit_enforced(self, seeded_conn):
        rows = ops_events.read(seeded_conn, limit=2)
        assert len(rows) == 2

    def test_read_parses_extras_json(self, seeded_conn):
        rows = ops_events.read(seeded_conn, min_level="error")
        assert rows[0]["extras"] == {"s": 500}

    def test_read_handles_empty_extras(self, seeded_conn):
        rows = ops_events.read(seeded_conn)
        no_extras = [r for r in rows if r["ts_us"] == 2000][0]
        assert no_extras["extras"] == {}
