"""Coverage for `phase_timing_rollup` — aggregation, idempotency, dashboard query."""

from __future__ import annotations

import datetime as _dt
import json
import sqlite3
from pathlib import Path

import pytest

import phase_timing_rollup as ptr


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    import migrate_db as m
    db = tmp_path / "rollup.db"
    m.migrate(f"sqlite:///{db}")
    return db


@pytest.fixture
def conn(db_path):
    c = sqlite3.connect(str(db_path))
    c.row_factory = sqlite3.Row
    yield c
    c.close()


# ---------------------------------------------------------------------------
# Migration
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
        assert "phase_timing_rollup" in names
        assert "uidx_ptr_bucket_phase" in names
        assert "idx_ptr_bucket" in names
        assert "idx_ptr_phase_bucket" in names

    def test_migration_idempotent(self, db_path: Path):
        import migrate_db as m
        m.migrate(f"sqlite:///{db_path}")
        m.migrate(f"sqlite:///{db_path}")


# ---------------------------------------------------------------------------
# aggregate_events
# ---------------------------------------------------------------------------


def _mk_ev(ts_us: int, phase: str, ms: float, ok: bool = True, **ctx) -> dict:
    ev = {
        "ts_us": ts_us,
        "event_type": "phase_timing",
        "phase": phase,
        "elapsed_ms": ms,
        "ok": ok,
    }
    if ctx:
        ev["context"] = ctx
    return ev


class TestAggregateEvents:
    def test_basic_grouping_into_one_bucket(self):
        BASE = 1_700_000_000_000_000  # µs
        evs = [
            _mk_ev(BASE + 1_000, "scanner.tick", 5.0),
            _mk_ev(BASE + 2_000, "scanner.tick", 7.0),
            _mk_ev(BASE + 3_000, "scanner.tick", 3.0),
        ]
        rows = ptr.aggregate_events(evs, bucket_seconds=60)
        assert len(rows) == 1
        r = rows[0]
        assert r.phase == "scanner.tick"
        assert r.count == 3
        assert r.p50_ms == 5.0
        assert r.max_ms == 7.0
        assert r.total_elapsed_ms == 15.0

    def test_bucket_boundary_splits_rows(self):
        # Events straddling a minute boundary should produce TWO rows.
        bs = 60
        bucket_us = bs * 1_000_000
        BASE = 1_700_000_000_000_000
        # Align BASE to a bucket boundary for clarity.
        BASE = (BASE // bucket_us) * bucket_us
        evs = [
            _mk_ev(BASE + 1_000, "tick", 1.0),
            _mk_ev(BASE + bucket_us + 1_000, "tick", 2.0),
        ]
        rows = ptr.aggregate_events(evs, bucket_seconds=bs)
        buckets = sorted(r.bucket_ts_us for r in rows)
        assert buckets == [BASE, BASE + bucket_us]

    def test_ignores_non_phase_timing_events(self):
        evs = [
            {"event_type": "decision", "ts_us": 1000, "elapsed_ms": 99},
            _mk_ev(1000, "tick", 1.0),
        ]
        rows = ptr.aggregate_events(evs)
        assert len(rows) == 1
        assert rows[0].phase == "tick"

    def test_error_count_tracks_ok_false(self):
        evs = [
            _mk_ev(1000, "tick", 1.0, ok=True),
            _mk_ev(1001, "tick", 2.0, ok=False),
            _mk_ev(1002, "tick", 3.0, ok=False),
        ]
        rows = ptr.aggregate_events(evs)
        assert rows[0].errors == 2
        assert rows[0].count == 3

    def test_since_us_filters_old_events(self):
        evs = [
            _mk_ev(1_000, "tick", 1.0),
            _mk_ev(2_000, "tick", 2.0),
            _mk_ev(3_000, "tick", 3.0),
        ]
        rows = ptr.aggregate_events(evs, since_us=2_000)
        assert rows[0].count == 2
        assert rows[0].max_ms == 3.0

    def test_exclude_from_us_drops_open_bucket(self):
        bs = 60
        bucket_us = bs * 1_000_000
        BASE = (1_700_000_000_000_000 // bucket_us) * bucket_us
        evs = [
            _mk_ev(BASE + 1_000, "tick", 1.0),                   # closed
            _mk_ev(BASE + bucket_us + 1_000, "tick", 2.0),       # open (excluded)
        ]
        rows = ptr.aggregate_events(
            evs, bucket_seconds=bs, exclude_from_us=BASE + bucket_us,
        )
        assert len(rows) == 1
        assert rows[0].bucket_ts_us == BASE

    def test_handles_missing_fields_gracefully(self):
        evs = [
            {"event_type": "phase_timing"},  # no ts_us / phase / ms
            {"event_type": "phase_timing", "ts_us": 1000, "phase": "x"},  # no ms
            _mk_ev(1001, "x", 5.0),
        ]
        rows = ptr.aggregate_events(evs)
        assert len(rows) == 1
        assert rows[0].count == 1

    def test_rows_sorted_by_bucket_then_phase(self):
        bs = 60
        bucket_us = bs * 1_000_000
        BASE = (1_700_000_000_000_000 // bucket_us) * bucket_us
        evs = [
            _mk_ev(BASE + bucket_us + 1, "z", 1.0),
            _mk_ev(BASE + 1, "a", 1.0),
            _mk_ev(BASE + 2, "b", 1.0),
        ]
        rows = ptr.aggregate_events(evs, bucket_seconds=bs)
        keys = [(r.bucket_ts_us, r.phase) for r in rows]
        assert keys == [(BASE, "a"), (BASE, "b"), (BASE + bucket_us, "z")]


# ---------------------------------------------------------------------------
# persist + idempotency
# ---------------------------------------------------------------------------


class TestPersist:
    def test_upsert_inserts_new_rows(self, conn):
        rows = [
            ptr.RollupRow(
                bucket_ts_us=1_000_000, bucket_seconds=60,
                phase="tick", count=3, errors=0,
                total_elapsed_ms=15.0,
                p50_ms=5.0, p95_ms=7.0, p99_ms=7.0, max_ms=7.0,
            )
        ]
        written = ptr.persist(conn, rows)
        assert written == 1
        count = conn.execute(
            "SELECT COUNT(*) FROM phase_timing_rollup"
        ).fetchone()[0]
        assert count == 1

    def test_upsert_updates_existing_row(self, conn):
        # Insert, then re-persist with different values for the same key.
        initial = ptr.RollupRow(
            1_000_000, 60, "tick", 1, 0, 5.0, 5.0, 5.0, 5.0, 5.0,
        )
        ptr.persist(conn, [initial])
        updated = ptr.RollupRow(
            1_000_000, 60, "tick", 10, 2, 100.0, 8.0, 12.0, 14.0, 20.0,
        )
        ptr.persist(conn, [updated])
        row = conn.execute(
            "SELECT count, errors, max_ms FROM phase_timing_rollup"
        ).fetchone()
        assert (row[0], row[1], row[2]) == (10, 2, 20.0)
        total = conn.execute(
            "SELECT COUNT(*) FROM phase_timing_rollup"
        ).fetchone()[0]
        assert total == 1  # upsert, not insert

    def test_persist_empty_is_noop(self, conn):
        assert ptr.persist(conn, []) == 0


# ---------------------------------------------------------------------------
# run — end-to-end JSONL → SQL
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")


class TestRun:
    @pytest.fixture
    def events_dir(self, tmp_path):
        return tmp_path / "events"

    def test_end_to_end_aggregates_jsonl_into_db(
        self, events_dir, db_path,
    ):
        # Freeze clock to a known bucket boundary.
        bs = 60
        bucket_us = bs * 1_000_000
        now_us = 1_776_000_000_000_000  # 2026-04-16 ish
        now_us = (now_us // bucket_us) * bucket_us + 30 * 1_000_000  # 30s into bucket
        current_bucket = (now_us // bucket_us) * bucket_us
        prev_bucket = current_bucket - bucket_us

        # Write today's JSONL (UTC date matching now_us).
        dt_today = _dt.datetime.fromtimestamp(
            now_us / 1_000_000, tz=_dt.timezone.utc,
        )
        log = events_dir / f"events_{dt_today.strftime('%Y-%m-%d')}.jsonl"
        _write_jsonl(log, [
            _mk_ev(prev_bucket + 1_000_000, "tick", 5.0),
            _mk_ev(prev_bucket + 2_000_000, "tick", 9.0),
            # An event in the OPEN bucket — must be excluded.
            _mk_ev(current_bucket + 5_000_000, "tick", 999.0),
        ])

        result = ptr.run(
            events_dir=str(events_dir),
            database_url=f"sqlite:///{db_path}",
            bucket_seconds=bs,
            lookback_minutes=120,
            now_us=now_us,
        )
        assert result["rows_written"] == 1

        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute(
                "SELECT bucket_ts_us, phase, count, max_ms "
                "FROM phase_timing_rollup"
            ).fetchall()
        finally:
            conn.close()
        assert len(rows) == 1
        assert rows[0][0] == prev_bucket
        assert rows[0][1] == "tick"
        assert rows[0][2] == 2
        # Open-bucket row (999ms) NOT included.
        assert rows[0][3] == 9.0

    def test_run_is_idempotent(self, events_dir, db_path):
        bs = 60
        bucket_us = bs * 1_000_000
        now_us = 1_776_000_000_000_000
        now_us = (now_us // bucket_us) * bucket_us + 30 * 1_000_000
        prev_bucket = (now_us // bucket_us) * bucket_us - bucket_us

        dt_today = _dt.datetime.fromtimestamp(
            now_us / 1_000_000, tz=_dt.timezone.utc,
        )
        log = events_dir / f"events_{dt_today.strftime('%Y-%m-%d')}.jsonl"
        _write_jsonl(log, [
            _mk_ev(prev_bucket + 1_000_000, "tick", 5.0),
        ])

        url = f"sqlite:///{db_path}"
        ptr.run(events_dir=str(events_dir), database_url=url,
                bucket_seconds=bs, now_us=now_us)
        ptr.run(events_dir=str(events_dir), database_url=url,
                bucket_seconds=bs, now_us=now_us)

        conn = sqlite3.connect(str(db_path))
        try:
            total = conn.execute(
                "SELECT COUNT(*) FROM phase_timing_rollup"
            ).fetchone()[0]
        finally:
            conn.close()
        assert total == 1  # 2 runs → still just one row (upserted)

    def test_run_reaggregates_updated_jsonl(
        self, events_dir, db_path,
    ):
        bs = 60
        bucket_us = bs * 1_000_000
        now_us = 1_776_000_000_000_000
        now_us = (now_us // bucket_us) * bucket_us + 30 * 1_000_000
        prev_bucket = (now_us // bucket_us) * bucket_us - bucket_us

        dt_today = _dt.datetime.fromtimestamp(
            now_us / 1_000_000, tz=_dt.timezone.utc,
        )
        log = events_dir / f"events_{dt_today.strftime('%Y-%m-%d')}.jsonl"
        url = f"sqlite:///{db_path}"

        # First run with 1 event.
        _write_jsonl(log, [_mk_ev(prev_bucket + 1_000_000, "tick", 5.0)])
        ptr.run(events_dir=str(events_dir), database_url=url,
                bucket_seconds=bs, now_us=now_us)

        # Append another event in the same closed bucket, re-run.
        # Overwrite (since _write_jsonl truncates) — in real life the
        # JSONL is append-only, but this test proves re-aggregation
        # picks up the updated contents either way.
        _write_jsonl(log, [
            _mk_ev(prev_bucket + 1_000_000, "tick", 5.0),
            _mk_ev(prev_bucket + 2_000_000, "tick", 9.0),
        ])
        ptr.run(events_dir=str(events_dir), database_url=url,
                bucket_seconds=bs, now_us=now_us)

        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT count, max_ms FROM phase_timing_rollup"
            ).fetchone()
        finally:
            conn.close()
        assert row[0] == 2     # count updated
        assert row[1] == 9.0   # max updated

    def test_retain_days_prunes_old_rows(self, events_dir, db_path):
        bs = 60
        bucket_us = bs * 1_000_000
        now_us = 1_776_000_000_000_000
        now_us = (now_us // bucket_us) * bucket_us + 30 * 1_000_000

        # Seed: an old bucket (5 days ago) + a recent closed bucket.
        old_bucket = now_us - 5 * 86400 * 1_000_000
        old_bucket = (old_bucket // bucket_us) * bucket_us
        recent_bucket = (now_us // bucket_us) * bucket_us - bucket_us

        conn = sqlite3.connect(str(db_path))
        try:
            ptr.persist(conn, [
                ptr.RollupRow(old_bucket, bs, "old_phase", 1, 0, 1.0, 1.0, 1.0, 1.0, 1.0),
                ptr.RollupRow(recent_bucket, bs, "new_phase", 1, 0, 1.0, 1.0, 1.0, 1.0, 1.0),
            ])
        finally:
            conn.close()

        # Run with retain_days=2 — should prune the 5-day-old row.
        dt_today = _dt.datetime.fromtimestamp(
            now_us / 1_000_000, tz=_dt.timezone.utc,
        )
        log = events_dir / f"events_{dt_today.strftime('%Y-%m-%d')}.jsonl"
        _write_jsonl(log, [])  # no new events — just testing prune path
        ptr.run(
            events_dir=str(events_dir),
            database_url=f"sqlite:///{db_path}",
            bucket_seconds=bs,
            retain_days=2,
            now_us=now_us,
        )
        conn = sqlite3.connect(str(db_path))
        try:
            phases = [
                r[0] for r in conn.execute(
                    "SELECT phase FROM phase_timing_rollup"
                ).fetchall()
            ]
        finally:
            conn.close()
        assert phases == ["new_phase"]


# ---------------------------------------------------------------------------
# fetch — dashboard query
# ---------------------------------------------------------------------------


class TestFetch:
    def test_fetch_aggregates_across_buckets(self, conn):
        # Seed three buckets for the same phase.
        rows = [
            ptr.RollupRow(1_000_000, 60, "tick", 3, 0, 15.0, 5.0, 7.0, 7.0, 7.0),
            ptr.RollupRow(2_000_000, 60, "tick", 2, 1, 8.0,  4.0, 4.0, 4.0, 4.0),
            ptr.RollupRow(3_000_000, 60, "tick", 1, 0, 2.0,  2.0, 2.0, 2.0, 2.0),
        ]
        ptr.persist(conn, rows)
        out = ptr.fetch(conn)
        assert len(out) == 1
        r = out[0]
        assert r["count"] == 6
        assert r["errors"] == 1
        # Stored rounded to 4 decimals — tolerate that precision.
        assert r["error_rate"] == pytest.approx(1 / 6, abs=1e-4)
        # Max-of-per-bucket (documented approximation)
        assert r["p95_ms"] == 7.0
        # Max is exact.
        assert r["max_ms"] == 7.0

    def test_fetch_since_us_filters(self, conn):
        ptr.persist(conn, [
            ptr.RollupRow(1_000_000, 60, "tick", 1, 0, 1.0, 1.0, 1.0, 1.0, 1.0),
            ptr.RollupRow(5_000_000, 60, "tick", 1, 0, 2.0, 2.0, 2.0, 2.0, 2.0),
        ])
        out = ptr.fetch(conn, since_us=3_000_000)
        assert out[0]["count"] == 1
        assert out[0]["max_ms"] == 2.0

    def test_fetch_sorts_by_budget(self, conn):
        """Phases with more total time (count × p50) come first."""
        ptr.persist(conn, [
            # quick phase: count=100, p50=1 → budget 100
            ptr.RollupRow(1_000_000, 60, "quick", 100, 0, 100.0, 1.0, 1.0, 1.0, 2.0),
            # slow phase: count=5, p50=200 → budget 1000
            ptr.RollupRow(1_000_000, 60, "slow",  5,   0, 1000.0, 200.0, 210.0, 215.0, 220.0),
        ])
        out = ptr.fetch(conn)
        assert [r["phase"] for r in out] == ["slow", "quick"]

    def test_fetch_bucket_seconds_filter(self, conn):
        # Seed rows at two different bucket sizes; fetch should isolate.
        ptr.persist(conn, [
            ptr.RollupRow(1_000_000, 60,   "tick", 1, 0, 1.0, 1.0, 1.0, 1.0, 1.0),
            ptr.RollupRow(1_000_000, 3600, "tick", 10, 0, 10.0, 1.0, 1.0, 1.0, 1.0),
        ])
        minute = ptr.fetch(conn, bucket_seconds=60)
        hour   = ptr.fetch(conn, bucket_seconds=3600)
        assert minute[0]["count"] == 1
        assert hour[0]["count"] == 10

    def test_fetch_empty_returns_empty_list(self, conn):
        assert ptr.fetch(conn) == []


# ---------------------------------------------------------------------------
# Dashboard integration
# ---------------------------------------------------------------------------


class TestDashboardIntegration:
    def test_ops_page_reads_rollup_when_seeded(self, db_path, tmp_path):
        """When the rollup table has rows, /kalshi/ops surfaces them via
        the phase-timings card — not the JSONL path."""
        from fastapi.testclient import TestClient
        from dashboards.kalshi import create_app

        # Seed a single closed-bucket row.
        conn = sqlite3.connect(str(db_path))
        try:
            ptr.persist(conn, [
                ptr.RollupRow(1_000_000, 60, "scanner.tick", 5, 0,
                              25.0, 5.0, 6.0, 7.0, 8.0),
            ])
            conn.commit()
        finally:
            conn.close()

        empty_events = tmp_path / "events"  # non-existent — forces SQL path
        app = create_app(
            database_url=f"sqlite:///{db_path}",
            events_dir=str(empty_events),
        )
        with TestClient(app) as c:
            r = c.get("/kalshi/ops?window=all")
            assert r.status_code == 200
            assert "scanner.tick" in r.text
            assert "Phase timings (top 5)" in r.text

    def test_ops_page_falls_back_to_jsonl_when_table_empty(
        self, db_path, tmp_path,
    ):
        """Fresh deploy with a JSONL file but no rollups yet — the
        phases card should still render using JSONL-sourced aggregates."""
        from fastapi.testclient import TestClient
        from dashboards.kalshi import create_app

        events_dir = tmp_path / "events"
        events_dir.mkdir()
        dt = _dt.datetime.utcnow()
        log = events_dir / f"events_{dt.strftime('%Y-%m-%d')}.jsonl"
        _write_jsonl(log, [
            _mk_ev(1000, "scanner.tick", 5.0),
            _mk_ev(2000, "scanner.tick", 7.0),
        ])

        app = create_app(
            database_url=f"sqlite:///{db_path}",
            events_dir=str(events_dir),
        )
        with TestClient(app) as c:
            r = c.get("/kalshi/ops?window=all")
            assert r.status_code == 200
            assert "scanner.tick" in r.text

    def test_phases_page_shows_window_tabs_when_using_rollup(
        self, db_path, tmp_path,
    ):
        from fastapi.testclient import TestClient
        from dashboards.kalshi import WINDOWS, create_app

        conn = sqlite3.connect(str(db_path))
        try:
            ptr.persist(conn, [
                ptr.RollupRow(1_000_000, 60, "tick", 1, 0,
                              1.0, 1.0, 1.0, 1.0, 1.0),
            ])
            conn.commit()
        finally:
            conn.close()

        app = create_app(
            database_url=f"sqlite:///{db_path}",
            events_dir=str(tmp_path / "no_events"),
        )
        with TestClient(app) as c:
            r = c.get("/kalshi/phases?window=all")
            assert r.status_code == 200
            # Window tabs present only when source == 'rollup'.
            for w in WINDOWS:
                assert f"window={w}" in r.text

    def test_phases_page_hides_window_tabs_on_jsonl_fallback(
        self, db_path, tmp_path,
    ):
        from fastapi.testclient import TestClient
        from dashboards.kalshi import create_app

        events_dir = tmp_path / "events"
        events_dir.mkdir()
        dt = _dt.datetime.utcnow()
        log = events_dir / f"events_{dt.strftime('%Y-%m-%d')}.jsonl"
        _write_jsonl(log, [_mk_ev(1000, "tick", 5.0)])

        app = create_app(
            database_url=f"sqlite:///{db_path}",
            events_dir=str(events_dir),
        )
        with TestClient(app) as c:
            r = c.get("/kalshi/phases")
            # No window tab strip when source is JSONL (no window to filter).
            # The "falling back to JSONL" message appears instead.
            assert "falling back to JSONL" in r.text

    def test_api_phases_respects_window(self, db_path, tmp_path):
        from fastapi.testclient import TestClient
        from dashboards.kalshi import create_app

        # Seed one old bucket (10 minutes ago) + one recent (30s ago).
        import time as _t
        now_us = int(_t.time() * 1_000_000)
        bs = 60
        bucket_us = bs * 1_000_000
        old_b    = ((now_us - 10 * 60 * 1_000_000) // bucket_us) * bucket_us
        recent_b = ((now_us - 30 * 1_000_000) // bucket_us) * bucket_us

        conn = sqlite3.connect(str(db_path))
        try:
            ptr.persist(conn, [
                ptr.RollupRow(old_b,    bs, "tick", 1, 0, 1.0, 1.0, 1.0, 1.0, 1.0),
                ptr.RollupRow(recent_b, bs, "tick", 5, 0, 25.0, 5.0, 6.0, 7.0, 8.0),
            ])
            conn.commit()
        finally:
            conn.close()

        # shadow_decisions is empty → dashboard window anchor falls back
        # to wall clock. 5m excludes the 10-minute-old row.
        app = create_app(
            database_url=f"sqlite:///{db_path}",
            events_dir=str(tmp_path / "no_events"),
        )
        with TestClient(app) as c:
            narrow = c.get("/api/phases?window=5m").json()
            wide   = c.get("/api/phases?window=all").json()
            assert narrow["phases"][0]["count"] == 5   # just the recent row
            assert wide["phases"][0]["count"] == 6     # both rows
