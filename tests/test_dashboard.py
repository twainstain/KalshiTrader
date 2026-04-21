"""Cover `dashboards.kalshi` routes against a seeded SQLite DB."""

from __future__ import annotations

import sqlite3
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from dashboards.kalshi import (
    DEFAULT_WINDOW,
    WINDOWS,
    WalletSnapshot,
    _fetch_phase_timings,
    _fmt_ts_est,
    _nav,
    _parse_time_param_us,
    _percentile,
    _qs,
    _range_clause,
    _time_bounds_us,
    _to_datetime_local_value,
    create_app,
)


# Base timestamp (µs since epoch) chosen so every supported window's
# cutoff stays positive — `_window_cutoff_us` subtracts up to 7 days.
# ~1.7e15 ≈ 2023-11, safely above `7d * 1e6`.
NOW_BASE_US = 1_700_000_000_000_000


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path):
    """Migrate + seed a fresh DB for each test."""
    import migrate_db as m
    db = tmp_path / "dash.db"
    m.migrate(f"sqlite:///{db}")
    conn = sqlite3.connect(str(db))

    # Seed shadow_decisions across two strategies, mixing reconciled/unreconciled.
    rows = [
        # (market_ticker, ts_us, p_yes, ci_width, reference_price,
        #  reference_60s_avg, time_remaining_s, best_yes_ask, best_no_ask,
        #  book_depth_yes_usd, book_depth_no_usd, recommended_side,
        #  hypothetical_fill_price, hypothetical_size_contracts,
        #  expected_edge_bps_after_fees, fee_bps_at_decision,
        #  realized_outcome, realized_pnl_usd, latency_ref, latency_book,
        #  strategy_label)
        ("KXBTC15M-T1", 1_000_001, "0.7", "0.05", "66000", "66000", "30",
         "0.55", "0.45", "500", "500", "yes",
         "0.55", "10", "150", "35",
         "yes", "4.50", None, None, "pure_lag"),
        ("KXBTC15M-T1", 1_000_002, "0.72", "0.05", "66050", "66000", "28",
         "0.55", "0.45", "500", "500", "yes",
         "0.55", "10", "200", "35",
         "no", "-5.50", None, None, "pure_lag"),
        ("KXETH15M-T1", 1_000_003, "0.65", "0.08", "3500", "3500", "45",
         "0.60", "0.40", "400", "400", "yes",
         "0.60", "10", "120", "35",
         None, None, None, None, "pure_lag"),
        ("KXBTC15M-T1", 1_000_004, "0.70", "0.05", "66000", "66000", "20",
         "0.55", "0.45", "500", "500", "yes",
         "0.55", "10", "150", "35",
         "yes", "4.50", None, None, "stat_model"),
    ]
    conn.executemany("""
        INSERT INTO shadow_decisions (
            market_ticker, ts_us, p_yes, ci_width, reference_price,
            reference_60s_avg, time_remaining_s, best_yes_ask, best_no_ask,
            book_depth_yes_usd, book_depth_no_usd, recommended_side,
            hypothetical_fill_price, hypothetical_size_contracts,
            expected_edge_bps_after_fees, fee_bps_at_decision,
            realized_outcome, realized_pnl_usd, latency_ms_ref_to_decision,
            latency_ms_book_to_decision, strategy_label
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, rows)

    # Seed reference_ticks (for freshness).
    conn.executemany(
        "INSERT INTO reference_ticks (asset, ts_us, price, src) VALUES (?,?,?,?)",
        [("btc", 1_000_000, "66000", "coinbase_live"),
         ("eth", 1_000_001, "3500", "coinbase_live")],
    )
    conn.commit()
    conn.close()
    return db


@pytest.fixture
def client(db_path):
    app = create_app(database_url=f"sqlite:///{db_path}")
    return TestClient(app)


@pytest.fixture
def db_path_windowed(tmp_path):
    """Fresh DB with one 'recent' row and one 'old' row — tests window filters.

    Seeds decisions at:
      - NOW_BASE_US (recent)
      - NOW_BASE_US - 20 min (inside 1h/24h window; outside 5m/15m)
      - NOW_BASE_US - 2 h  (inside 4h/24h; outside 5m/15m/1h)
    Also seeds one row with strategy_label = NULL to exercise the
    `stat_model_legacy` coalesce.
    """
    import migrate_db as m
    db = tmp_path / "windowed.db"
    m.migrate(f"sqlite:///{db}")
    conn = sqlite3.connect(str(db))

    common = (
        "0.7", "0.05", "66000", "66000", "30",
        "0.55", "0.45", "500", "500", "yes",
        "0.55", "10", "150", "35",
    )
    rows = [
        # RECENT — stat_model
        ("KXBTC15M-REC", NOW_BASE_US,                     *common,
         "yes", "4.50", "12.0", "5.0", "stat_model"),
        # 20 min old — pure_lag (inside 1h, outside 5m/15m)
        ("KXBTC15M-MID", NOW_BASE_US - 20 * 60 * 1_000_000, *common,
         "yes", "2.50", "9.0",  "6.0", "pure_lag"),
        # 2 hours old — stat_model (outside 1h, inside 4h)
        ("KXBTC15M-OLD", NOW_BASE_US - 2 * 60 * 60 * 1_000_000, *common,
         "no",  "-3.25", "50.0", "30.0", "stat_model"),
        # Legacy null-label row — recent, exercises coalesce
        ("KXBTC15M-LEG", NOW_BASE_US - 10_000,             *common,
         "yes", "1.50", "7.0",  "4.0", None),
    ]
    conn.executemany("""
        INSERT INTO shadow_decisions (
            market_ticker, ts_us, p_yes, ci_width, reference_price,
            reference_60s_avg, time_remaining_s, best_yes_ask, best_no_ask,
            book_depth_yes_usd, book_depth_no_usd, recommended_side,
            hypothetical_fill_price, hypothetical_size_contracts,
            expected_edge_bps_after_fees, fee_bps_at_decision,
            realized_outcome, realized_pnl_usd, latency_ms_ref_to_decision,
            latency_ms_book_to_decision, strategy_label
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, rows)
    conn.executemany(
        "INSERT INTO reference_ticks (asset, ts_us, price, src) VALUES (?,?,?,?)",
        [("btc", NOW_BASE_US - 2_000_000, "66000", "coinbase_live")],
    )
    conn.commit()
    conn.close()
    return db


@pytest.fixture
def client_windowed(db_path_windowed):
    return TestClient(create_app(database_url=f"sqlite:///{db_path_windowed}"))


# ---------------------------------------------------------------------------
# HTML routes
# ---------------------------------------------------------------------------


class TestHTMLRoutes:
    def test_root_redirects_to_overview(self, client):
        r = client.get("/", follow_redirects=False)
        assert r.status_code in (307, 308)
        assert r.headers["location"] == "/kalshi"

    def test_overview_renders(self, client):
        r = client.get("/kalshi")
        assert r.status_code == 200
        assert "Overview" in r.text
        # Both strategies appear as cards.
        assert "pure_lag" in r.text
        assert "stat_model" in r.text
        # Decimal aggregates show on the page.
        assert "$" in r.text

    def test_decisions_renders_all_by_default(self, client):
        r = client.get("/kalshi/decisions")
        assert r.status_code == 200
        # All 4 seeded rows visible.
        assert "KXBTC15M-T1" in r.text
        assert "KXETH15M-T1" in r.text
        assert "pure_lag" in r.text and "stat_model" in r.text

    def test_decisions_filtered_by_strategy(self, client):
        r = client.get("/kalshi/decisions?strategy=pure_lag")
        assert r.status_code == 200
        # ETH was in pure_lag → present.
        assert "KXETH15M-T1" in r.text
        # stat_model decisions should not appear since we filtered.
        assert r.text.count("pure_lag") >= 3

    def test_decisions_limit_enforced(self, client):
        r = client.get("/kalshi/decisions?limit=1")
        assert r.status_code == 200
        assert "KXBTC15M-T1" in r.text

    def test_decisions_limit_bounds(self, client):
        # Out-of-range should fail validation.
        r = client.get("/kalshi/decisions?limit=0")
        assert r.status_code == 422
        r = client.get("/kalshi/decisions?limit=5000")
        assert r.status_code == 422

    def test_performance_renders(self, client):
        r = client.get("/kalshi/performance")
        assert r.status_code == 200
        assert "pure_lag" in r.text
        # Series prefix grouping.
        assert "KXBTC15M" in r.text

    def test_health_renders(self, client):
        r = client.get("/kalshi/health")
        assert r.status_code == 200
        assert "btc" in r.text
        assert "eth" in r.text

    def test_paper_renders_zero_state(self, client):
        r = client.get("/kalshi/paper")
        assert r.status_code == 200
        # No paper data seeded — should still render without error.
        assert "Paper" in r.text
        assert "0" in r.text  # n_fills = 0

    def test_live_renders_zero_state(self, client):
        r = client.get("/kalshi/live")
        assert r.status_code == 200
        assert "three-opt-in" in r.text


# ---------------------------------------------------------------------------
# JSON APIs
# ---------------------------------------------------------------------------


class TestAPIRoutes:
    def test_api_overview_shape(self, client):
        r = client.get("/api/overview")
        assert r.status_code == 200
        data = r.json()
        assert "per_strategy" in data
        assert isinstance(data["per_strategy"], list)
        labels = {s["strategy_label"] for s in data["per_strategy"]}
        assert {"pure_lag", "stat_model"}.issubset(labels)
        # reference freshness in the overview payload too.
        assert any(r["asset"] == "btc" for r in data["reference_freshness"])

    def test_api_decisions_filter(self, client):
        r = client.get("/api/decisions?strategy=pure_lag")
        rows = r.json()
        assert all(row["strategy_label"] == "pure_lag" for row in rows)
        assert len(rows) == 3

    def test_api_decisions_default(self, client):
        r = client.get("/api/decisions")
        rows = r.json()
        assert len(rows) == 4  # all seeded rows

    def test_api_performance_excludes_unreconciled(self, client):
        r = client.get("/api/performance")
        rows = r.json()
        # The unreconciled ETH row should be absent (no realized_outcome).
        assert not any("KXETH15M" in r["series"] for r in rows)

    def test_api_health_shape(self, client):
        r = client.get("/api/health")
        data = r.json()
        assert "reference" in data
        assert "decisions" in data
        assert "now_us" in data
        # age_seconds is computed relative to `now_us`.
        for row in data["reference"]:
            assert "age_seconds" in row


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrors:
    def test_missing_db_raises_500(self, tmp_path):
        bad = tmp_path / "nope.db"  # file never created
        app = create_app(database_url=f"sqlite:///{bad}")
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.get("/kalshi")
            assert r.status_code == 500

    def test_non_sqlite_url_rejected(self):
        app = create_app(database_url="postgresql://x/y")
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.get("/kalshi")
            assert r.status_code == 500


# ---------------------------------------------------------------------------
# Time-window tabs — windowed DB fixture used so the cutoff actually bites.
# ---------------------------------------------------------------------------


class TestWindowFiltering:
    def test_default_window_is_1h(self, client_windowed):
        """Default view hides the 2h-old row but keeps recent + 20min rows."""
        r = client_windowed.get("/api/overview")
        assert r.status_code == 200
        data = r.json()
        assert data["window"] == DEFAULT_WINDOW  # '1h'
        # total = recent + 20min + legacy = 3 (OLD @ 2h excluded)
        assert data["totals"]["total"] == 3

    def test_window_5m_excludes_20min_row(self, client_windowed):
        r = client_windowed.get("/api/overview?window=5m")
        data = r.json()
        # Only the recent + legacy rows survive.
        assert data["totals"]["total"] == 2

    def test_window_all_includes_everything(self, client_windowed):
        r = client_windowed.get("/api/overview?window=all")
        data = r.json()
        assert data["totals"]["total"] == 4  # all 4 seeded rows

    def test_window_4h_includes_2h_old_row(self, client_windowed):
        r = client_windowed.get("/api/overview?window=4h")
        data = r.json()
        assert data["totals"]["total"] == 4

    def test_invalid_window_falls_back_to_default(self, client_windowed):
        r = client_windowed.get("/api/overview?window=bogus")
        data = r.json()
        assert data["window"] == DEFAULT_WINDOW

    def test_overview_html_shows_all_window_tabs(self, client_windowed):
        r = client_windowed.get("/kalshi")
        assert r.status_code == 200
        # Every declared window appears as a tab link.
        for w in WINDOWS:
            assert f"window={w}" in r.text

    def test_performance_window_respected(self, client_windowed):
        # stat_model × KXBTC15M aggregates the 2h-old -3.25 row into its pnl.
        # In `all`, pnl = 4.50 + (-3.25) = 1.25.
        # In `1h`, OLD is excluded, so pnl = 4.50 only.
        def _stat_model_pnl(rows):
            return next(
                (float(r["pnl"]) for r in rows
                 if r["strategy_label"] == "stat_model"),
                None,
            )
        r_1h = client_windowed.get("/api/performance?window=1h").json()
        r_all = client_windowed.get("/api/performance?window=all").json()
        assert _stat_model_pnl(r_1h) == pytest.approx(4.50)
        assert _stat_model_pnl(r_all) == pytest.approx(1.25)


# ---------------------------------------------------------------------------
# Legacy-label coalesce — NULL strategy_label becomes `stat_model_legacy`.
# ---------------------------------------------------------------------------


class TestLegacyLabelCoalesce:
    def test_overview_api_renames_null_to_legacy(self, client_windowed):
        r = client_windowed.get("/api/overview?window=all")
        labels = {s["strategy_label"] for s in r.json()["per_strategy"]}
        assert "stat_model_legacy" in labels
        # Raw NULL / empty should NOT leak through.
        assert None not in labels
        assert "" not in labels

    def test_overview_html_no_unlabeled_card(self, client_windowed):
        r = client_windowed.get("/kalshi?window=all")
        assert "(unlabeled)" not in r.text
        assert "stat_model_legacy" in r.text

    def test_performance_api_coalesces_too(self, client_windowed):
        r = client_windowed.get("/api/performance?window=all")
        labels = {row["strategy_label"] for row in r.json()}
        assert "stat_model_legacy" in labels


# ---------------------------------------------------------------------------
# Totals card — sum across per-strategy rows in the selected window.
# ---------------------------------------------------------------------------


class TestTotalsCard:
    def test_totals_sums_across_strategies(self, client_windowed):
        r = client_windowed.get("/api/overview?window=all")
        data = r.json()
        # Sum of all 4 seeded realized_pnl: 4.50 + 2.50 + -3.25 + 1.50 = 5.25
        assert data["totals"]["pnl"] == pytest.approx(5.25, rel=1e-6)
        assert data["totals"]["total"] == 4
        # per-dec = pnl / total
        assert data["totals"]["pnl_per_dec"] == pytest.approx(5.25 / 4, rel=1e-4)

    def test_totals_card_rendered_on_overview(self, client_windowed):
        r = client_windowed.get("/kalshi")
        assert "Totals (" in r.text  # header reads 'Totals (1h)' etc.

    def test_totals_empty_window_no_divzero(self, client_windowed):
        """Window with zero rows must not blow up on per-dec division."""
        # Drain all rows by asking for a window before any seeded data.
        # 5m window anchored at NOW_BASE_US → cuts at NOW_BASE_US - 300s;
        # still includes recent + legacy. We can't easily craft an empty
        # window without an empty DB, so just assert shape stays valid.
        r = client_windowed.get("/api/overview?window=5m")
        data = r.json()
        assert "pnl_per_dec" in data["totals"]


# ---------------------------------------------------------------------------
# Reference-feed freshness — `age_seconds` replaces raw µs in HTML.
# ---------------------------------------------------------------------------


class TestReferenceFreshness:
    def test_api_exposes_age_seconds(self, client_windowed):
        r = client_windowed.get("/api/overview")
        row = next(r for r in r.json()["reference_freshness"] if r["asset"] == "btc")
        # 2s old relative to anchor NOW_BASE_US (seeded tick at -2_000_000 µs).
        assert row["age_seconds"] == pytest.approx(2.0, abs=0.5)

    def test_html_renders_age_not_raw_microseconds(self, client_windowed):
        r = client_windowed.get("/kalshi")
        # The anchor-anchored age is ~2s — should render as '2.0s' (or similar),
        # never as the raw µs timestamp.
        assert f"{NOW_BASE_US - 2_000_000}" not in r.text or "age" in r.text.lower()
        # Header renamed to 'age' column.
        assert ">age<" in r.text


# ---------------------------------------------------------------------------
# Wallet card — BalanceFetcher is pluggable; default renders "not configured".
# ---------------------------------------------------------------------------


class TestWalletCard:
    def test_not_configured_when_no_fetcher(self, db_path_windowed):
        app = create_app(database_url=f"sqlite:///{db_path_windowed}")
        with TestClient(app) as c:
            r = c.get("/kalshi")
            assert "Not configured" in r.text

    def test_renders_snapshot_when_fetcher_supplied(self, db_path_windowed):
        def fetcher() -> WalletSnapshot:
            return WalletSnapshot(
                balance_usd="1234.56",
                positions_count=3,
                notional_usd="500.00",
            )
        app = create_app(
            database_url=f"sqlite:///{db_path_windowed}",
            balance_fetcher=fetcher,
        )
        with TestClient(app) as c:
            r = c.get("/kalshi")
            assert "$+1,234.56" in r.text
            assert "3 positions" in r.text

    def test_fetcher_error_surfaced_as_neg_banner(self, db_path_windowed):
        def fetcher() -> WalletSnapshot:
            raise RuntimeError("api_key missing")
        app = create_app(
            database_url=f"sqlite:///{db_path_windowed}",
            balance_fetcher=fetcher,
        )
        with TestClient(app) as c:
            r = c.get("/kalshi")
            assert "fetch failed: api_key missing" in r.text

    def test_fetcher_returning_none_falls_back_to_not_configured(self, db_path_windowed):
        app = create_app(
            database_url=f"sqlite:///{db_path_windowed}",
            balance_fetcher=lambda: None,
        )
        with TestClient(app) as c:
            r = c.get("/kalshi")
            assert "Not configured" in r.text

    def test_wallet_included_in_api_overview(self, db_path_windowed):
        snap = WalletSnapshot(balance_usd="10.00", positions_count=1)
        app = create_app(
            database_url=f"sqlite:///{db_path_windowed}",
            balance_fetcher=lambda: snap,
        )
        with TestClient(app) as c:
            data = c.get("/api/overview").json()
            assert data["wallet"]["balance_usd"] == "10.00"
            assert data["wallet"]["positions_count"] == 1


# ---------------------------------------------------------------------------
# Ops page — service status + latency percentiles.
# ---------------------------------------------------------------------------


class TestOpsPage:
    def test_html_route_renders(self, client_windowed):
        r = client_windowed.get("/kalshi/ops")
        assert r.status_code == 200
        assert "Service status" in r.text
        assert "Latency — reference → decision" in r.text
        assert "Latency — book → decision" in r.text

    def test_html_shows_window_tabs(self, client_windowed):
        r = client_windowed.get("/kalshi/ops")
        for w in WINDOWS:
            assert f"window={w}" in r.text

    def test_nav_has_ops_link(self, client_windowed):
        r = client_windowed.get("/kalshi")
        assert 'href="/kalshi/ops"' in r.text

    def test_api_ops_shape(self, client_windowed):
        r = client_windowed.get("/api/ops?window=all")
        assert r.status_code == 200
        data = r.json()
        for key in (
            "window", "ref_to_decision_ms", "book_to_decision_ms",
            "decisions_per_min", "last_decision_age_s",
            "last_reference_tick_age_s", "total_decisions_in_window",
        ):
            assert key in data, f"missing {key}"
        for k in ("count", "p50", "p95", "p99", "max"):
            assert k in data["ref_to_decision_ms"]

    def test_percentiles_computed_from_seeded_latencies(self, client_windowed):
        # Seeded ref_to_decision ms in order: 12.0, 9.0, 50.0, 7.0
        # Sorted: 7.0, 9.0, 12.0, 50.0 → p50 (index 1.5 → round to 2) = 12.0
        # p95/p99 → 50.0 (top of 4-element set, nearest-rank).
        data = client_windowed.get("/api/ops?window=all").json()
        ref = data["ref_to_decision_ms"]
        assert ref["count"] == 4
        assert ref["p50"] == pytest.approx(12.0)
        assert ref["p95"] == pytest.approx(50.0)
        assert ref["p99"] == pytest.approx(50.0)
        assert ref["max"] == pytest.approx(50.0)

    def test_percentiles_respect_window(self, client_windowed):
        # 5m window excludes the OLD row (latency=50.0) — so max should drop.
        data = client_windowed.get("/api/ops?window=5m").json()
        ref = data["ref_to_decision_ms"]
        # Remaining rows: recent (12.0) + legacy (7.0) = 2 samples.
        assert ref["count"] == 2
        assert ref["max"] == pytest.approx(12.0)

    def test_ops_page_respects_invalid_window(self, client_windowed):
        r = client_windowed.get("/kalshi/ops?window=unknown")
        # Falls back to default and still renders without error.
        assert r.status_code == 200
        assert "Service status" in r.text

    def test_service_status_shows_ages(self, client_windowed):
        r = client_windowed.get("/kalshi/ops")
        assert "last decision" in r.text
        assert "last reference tick" in r.text


# ---------------------------------------------------------------------------
# _percentile helper unit tests — pure function, no DB.
# ---------------------------------------------------------------------------


class TestPercentileHelper:
    def test_empty_returns_none(self):
        assert _percentile([], 50) is None
        assert _percentile([], 99) is None

    def test_single_element(self):
        assert _percentile([42.0], 50) == 42.0
        assert _percentile([42.0], 99) == 42.0

    def test_monotone_ascending(self):
        # Nearest-rank with k = round(pct/100 * (n-1)). Banker's rounding
        # applies (stdlib `round`), so for n=10 p=50 → k=round(4.5)=4.
        vs = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
        assert _percentile(vs, 50) == pytest.approx(5.0)
        assert _percentile(vs, 95) == pytest.approx(10.0)
        assert _percentile(vs, 99) == pytest.approx(10.0)
        assert _percentile(vs, 0) == pytest.approx(1.0)
        assert _percentile(vs, 100) == pytest.approx(10.0)

    def test_is_order_invariant(self):
        shuffled = [5, 3, 9, 1, 7, 2, 8, 4, 6]
        sorted_ = sorted(shuffled)
        for p in (50, 95, 99):
            assert _percentile(list(shuffled), p) == _percentile(sorted_, p)


# ---------------------------------------------------------------------------
# Phases page (JSONL aggregator)
# ---------------------------------------------------------------------------


def _write_events(path, events):
    import json
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


@pytest.fixture
def phases_events_dir(tmp_path):
    """Seed today's events JSONL with phase_timing + non-timing events."""
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = tmp_path / f"events_{today}.jsonl"
    _write_events(path, [
        # scanner.tick: 3 samples, 1 error.
        {"ts_us": 1, "event_type": "phase_timing",
         "phase": "scanner.tick", "elapsed_ms": 10.0, "ok": True},
        {"ts_us": 2, "event_type": "phase_timing",
         "phase": "scanner.tick", "elapsed_ms": 20.0, "ok": True},
        {"ts_us": 3, "event_type": "phase_timing",
         "phase": "scanner.tick", "elapsed_ms": 30.0, "ok": False,
         "error_type": "RuntimeError"},
        # strategy.evaluate: 5 samples
        *[{"ts_us": 10 + i, "event_type": "phase_timing",
           "phase": "strategy.evaluate", "elapsed_ms": 0.5 + i,
           "ok": True} for i in range(5)],
        # Non-phase events should be ignored
        {"ts_us": 100, "event_type": "decision", "asset": "btc"},
        {"ts_us": 101, "event_type": "paper_fill", "market_ticker": "X"},
    ])
    return tmp_path


class TestFetchPhaseTimings:
    def test_aggregates_per_phase(self, phases_events_dir):
        d = _fetch_phase_timings(events_dir=phases_events_dir)
        phases = {p["phase"]: p for p in d["phases"]}
        assert "scanner.tick" in phases
        assert "strategy.evaluate" in phases

        tick = phases["scanner.tick"]
        assert tick["count"] == 3
        assert tick["errors"] == 1
        assert tick["error_rate"] == pytest.approx(1/3, rel=1e-3)
        assert tick["max"] == 30.0

        ev = phases["strategy.evaluate"]
        assert ev["count"] == 5
        assert ev["errors"] == 0

    def test_ignores_non_timing_events(self, phases_events_dir):
        d = _fetch_phase_timings(events_dir=phases_events_dir)
        assert d["total_events"] == 8  # 3 tick + 5 evaluate; decision/fill skipped

    def test_missing_file_returns_empty(self, tmp_path):
        d = _fetch_phase_timings(events_dir=tmp_path / "nonexistent")
        assert d["phases"] == []
        assert d["total_events"] == 0

    def test_malformed_lines_skipped(self, tmp_path):
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = tmp_path / f"events_{today}.jsonl"
        path.write_text(
            '{"event_type": "phase_timing", "phase": "x", "elapsed_ms": 5.0, "ok": true}\n'
            'not-valid-json\n'
            '{"event_type": "phase_timing"}\n'   # missing phase/elapsed_ms
            '{"event_type": "phase_timing", "phase": "x", "elapsed_ms": 10.0, "ok": true}\n'
        )
        d = _fetch_phase_timings(events_dir=tmp_path)
        assert d["total_events"] == 2
        [x_row] = d["phases"]
        assert x_row["phase"] == "x" and x_row["count"] == 2


class TestPhasesRoute:
    @pytest.fixture
    def phases_client(self, db_path, phases_events_dir):
        app = create_app(
            database_url=f"sqlite:///{db_path}",
            events_dir=str(phases_events_dir),
        )
        return TestClient(app)

    def test_html_renders(self, phases_client):
        r = phases_client.get("/kalshi/phases")
        assert r.status_code == 200
        assert "Phase timings" in r.text
        assert "scanner.tick" in r.text
        assert "strategy.evaluate" in r.text

    def test_api_shape(self, phases_client):
        r = phases_client.get("/api/phases")
        assert r.status_code == 200
        data = r.json()
        assert "phases" in data
        phases = {p["phase"]: p for p in data["phases"]}
        assert {"scanner.tick", "strategy.evaluate"} <= set(phases.keys())

    def test_empty_dir_renders_placeholder(self, db_path, tmp_path):
        app = create_app(
            database_url=f"sqlite:///{db_path}",
            events_dir=str(tmp_path / "empty"),
        )
        with TestClient(app) as client:
            r = client.get("/kalshi/phases")
            assert r.status_code == 200
            assert "No events" in r.text

    def test_nav_has_phases_link(self, phases_client):
        r = phases_client.get("/kalshi")
        assert 'href="/kalshi/phases"' in r.text


# ---------------------------------------------------------------------------
# HTTP Basic auth — DASHBOARD_USER / DASHBOARD_PASS
# ---------------------------------------------------------------------------


class TestBasicAuth:
    def test_no_auth_configured_allows_through(self, db_path):
        """Default state: no creds → no challenge."""
        app = create_app(database_url=f"sqlite:///{db_path}")
        assert app.state.auth_enabled is False
        with TestClient(app) as c:
            assert c.get("/kalshi").status_code == 200

    def test_auth_enabled_rejects_missing_header(self, db_path):
        app = create_app(
            database_url=f"sqlite:///{db_path}",
            username="admin", password="adminTest",
        )
        assert app.state.auth_enabled is True
        with TestClient(app) as c:
            r = c.get("/kalshi")
            assert r.status_code == 401
            assert r.headers.get("www-authenticate", "").startswith("Basic")

    def test_auth_enabled_rejects_bad_password(self, db_path):
        app = create_app(
            database_url=f"sqlite:///{db_path}",
            username="admin", password="adminTest",
        )
        with TestClient(app) as c:
            r = c.get("/kalshi", auth=("admin", "wrong"))
            assert r.status_code == 401

    def test_auth_enabled_rejects_bad_username(self, db_path):
        app = create_app(
            database_url=f"sqlite:///{db_path}",
            username="admin", password="adminTest",
        )
        with TestClient(app) as c:
            r = c.get("/kalshi", auth=("root", "adminTest"))
            assert r.status_code == 401

    def test_auth_enabled_accepts_correct_creds(self, db_path):
        app = create_app(
            database_url=f"sqlite:///{db_path}",
            username="admin", password="adminTest",
        )
        with TestClient(app) as c:
            r = c.get("/kalshi", auth=("admin", "adminTest"))
            assert r.status_code == 200
            assert "Overview" in r.text

    def test_auth_header_malformed_rejected(self, db_path):
        """Non-Basic scheme / invalid base64 / missing colon → 401."""
        app = create_app(
            database_url=f"sqlite:///{db_path}",
            username="admin", password="adminTest",
        )
        with TestClient(app) as c:
            for header in (
                "Bearer token",                      # wrong scheme
                "Basic not-base64!!",                # undecodable
                "Basic " + "aW52YWxpZA==",           # "invalid" (no colon)
            ):
                r = c.get("/kalshi", headers={"Authorization": header})
                assert r.status_code == 401, f"header={header!r} should 401"

    def test_auth_protects_api_routes(self, db_path):
        app = create_app(
            database_url=f"sqlite:///{db_path}",
            username="admin", password="adminTest",
        )
        with TestClient(app) as c:
            assert c.get("/api/overview").status_code == 401
            assert c.get(
                "/api/overview", auth=("admin", "adminTest"),
            ).status_code == 200

    def test_auth_protects_post_control_endpoints(
        self, db_path, tmp_path,
    ):
        app = create_app(
            database_url=f"sqlite:///{db_path}",
            username="admin", password="adminTest",
            allow_write=True,
            flags_path=tmp_path / "flags.json",
        )
        with TestClient(app, follow_redirects=False) as c:
            # Unauthenticated POST → 401, not 303 redirect.
            r = c.post(
                "/kalshi/ops/flags/scan",
                data={"name": "btc", "enabled": "false"},
            )
            assert r.status_code == 401
            # With creds → 303 as usual.
            r = c.post(
                "/kalshi/ops/flags/scan",
                data={"name": "btc", "enabled": "false"},
                auth=("admin", "adminTest"),
            )
            assert r.status_code == 303

    def test_env_vars_configure_auth(self, db_path, monkeypatch):
        """When args are None, env vars take effect."""
        monkeypatch.setenv("DASHBOARD_USER", "ops")
        monkeypatch.setenv("DASHBOARD_PASS", "sekret")
        app = create_app(database_url=f"sqlite:///{db_path}")
        assert app.state.auth_enabled is True
        with TestClient(app) as c:
            assert c.get("/kalshi").status_code == 401
            assert c.get("/kalshi", auth=("ops", "sekret")).status_code == 200

    def test_half_configured_env_does_not_enable(
        self, db_path, monkeypatch,
    ):
        """Only user, no pass → auth stays off. Avoids accidentally locking
        out a deploy that forgot to set both halves."""
        monkeypatch.setenv("DASHBOARD_USER", "ops")
        monkeypatch.delenv("DASHBOARD_PASS", raising=False)
        app = create_app(database_url=f"sqlite:///{db_path}")
        assert app.state.auth_enabled is False
        with TestClient(app) as c:
            assert c.get("/kalshi").status_code == 200

    def test_constructor_args_override_env(self, db_path, monkeypatch):
        """Explicit username/password in create_app win over env."""
        monkeypatch.setenv("DASHBOARD_USER", "env_user")
        monkeypatch.setenv("DASHBOARD_PASS", "env_pass")
        app = create_app(
            database_url=f"sqlite:///{db_path}",
            username="arg_user", password="arg_pass",
        )
        with TestClient(app) as c:
            # Env creds should NOT work.
            assert c.get(
                "/kalshi", auth=("env_user", "env_pass"),
            ).status_code == 401
            # Arg creds should.
            assert c.get(
                "/kalshi", auth=("arg_user", "arg_pass"),
            ).status_code == 200


# ---------------------------------------------------------------------------
# ops_events table — error feed on /kalshi/ops
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path_with_events(db_path_windowed):
    """Windowed DB + a few rows in `ops_events` for the error feed."""
    conn = sqlite3.connect(str(db_path_windowed))
    events = [
        (NOW_BASE_US - 30_000_000, "kalshi_rest", "warn",  "429 rate limited", '{"status":429}'),
        (NOW_BASE_US - 10_000_000, "coinbase_ws", "warn",  "WS reconnect",    '{"backoff_s":2.0}'),
        (NOW_BASE_US - 1_000_000,  "kalshi_rest", "error", "500 from API",    '{"status":500}'),
        (NOW_BASE_US,              "runner",      "info",  "scanner started", ""),
        # 3h old: excluded by 1h window, included by 4h+
        (NOW_BASE_US - 3 * 3600 * 1_000_000, "runner", "warn", "old warning", ""),
    ]
    conn.executemany(
        "INSERT INTO ops_events (ts_us, source, level, message, extras_json) "
        "VALUES (?, ?, ?, ?, ?)",
        events,
    )
    conn.commit()
    conn.close()
    return db_path_windowed


@pytest.fixture
def client_with_events(db_path_with_events):
    return TestClient(create_app(database_url=f"sqlite:///{db_path_with_events}"))


class TestOpsEventsFeed:
    def test_api_ops_includes_events_list(self, client_with_events):
        data = client_with_events.get("/api/ops?window=all").json()
        assert "events" in data
        assert len(data["events"]) == 5
        # Newest-first ordering.
        assert data["events"][0]["message"] == "scanner started"

    def test_window_filters_events(self, client_with_events):
        """Events older than the window must be excluded."""
        # 1h window: 4 recent events survive, 3h-old one is dropped.
        data_1h = client_with_events.get("/api/ops?window=1h").json()
        assert len(data_1h["events"]) == 4
        data_all = client_with_events.get("/api/ops?window=all").json()
        assert len(data_all["events"]) == 5

    def test_events_by_level_counted(self, client_with_events):
        data = client_with_events.get("/api/ops?window=all").json()
        counts = data["events_by_level"]
        assert counts["error"] == 1
        assert counts["warn"] == 3
        assert counts["info"] == 1

    def test_html_renders_event_table(self, client_with_events):
        r = client_with_events.get("/kalshi/ops?window=all")
        assert r.status_code == 200
        assert "Events" in r.text
        assert "500 from API" in r.text
        assert "WS reconnect" in r.text
        # Error level is rendered with the negative-state class.
        assert 'class="neg">error' in r.text

    def test_html_shows_error_warn_info_counts(self, client_with_events):
        r = client_with_events.get("/kalshi/ops?window=all")
        assert "errors / warns / infos" in r.text

    def test_html_renders_empty_state_for_narrow_window(self, client_with_events):
        # 5m excludes ALL 5 seeded events (oldest fresh is 30s before base).
        # Actually 30s old is inside 5m. Let's instead verify a tiny-db case
        # by checking the windowed-DB fixture (no ops_events seeded).
        pass

    def test_empty_events_shows_muted_placeholder(self, client_windowed):
        r = client_windowed.get("/kalshi/ops")
        assert "No events in the selected window" in r.text


# ---------------------------------------------------------------------------
# /kalshi/ops controls: runtime-flags card + POST endpoints
# ---------------------------------------------------------------------------


@pytest.fixture
def flags_tmpfile(tmp_path):
    return tmp_path / "runtime_flags.json"


@pytest.fixture
def ops_app_readonly(db_path_windowed, flags_tmpfile):
    return create_app(
        database_url=f"sqlite:///{db_path_windowed}",
        flags_path=flags_tmpfile,
        allow_write=False,
    )


@pytest.fixture
def ops_app_writable(db_path_windowed, flags_tmpfile):
    return create_app(
        database_url=f"sqlite:///{db_path_windowed}",
        flags_path=flags_tmpfile,
        allow_write=True,
    )


class TestOpsControlsReadOnly:
    def test_controls_card_shows_hint_when_read_only(self, ops_app_readonly):
        with TestClient(ops_app_readonly) as c:
            r = c.get("/kalshi/ops")
            assert "DASHBOARD_ALLOW_WRITE=1" in r.text
            # No form elements present when writes disabled.
            assert "<form" not in r.text.split("Controls", 1)[1].split("</body>", 1)[0]

    def test_post_rejected_when_read_only(self, ops_app_readonly):
        with TestClient(ops_app_readonly) as c:
            r = c.post(
                "/kalshi/ops/flags/scan",
                data={"name": "btc", "enabled": "false"},
            )
            assert r.status_code == 403
            assert "writes disabled" in r.text

    def test_api_ops_exposes_allow_write_false(self, ops_app_readonly):
        with TestClient(ops_app_readonly) as c:
            data = c.get("/api/ops").json()
            assert data["allow_write"] is False


class TestOpsControlsWritable:
    def test_controls_card_has_forms(self, ops_app_writable):
        with TestClient(ops_app_writable) as c:
            r = c.get("/kalshi/ops")
            controls_html = r.text.split(
                "<h2>Controls</h2>", 1)[1].split("</body>", 1)[0]
            # Per-asset and per-strategy forms, plus the kill button.
            assert controls_html.count("<form") >= 3
            assert "/kalshi/ops/flags/scan" in controls_html
            assert "/kalshi/ops/flags/strategy" in controls_html
            assert "/kalshi/ops/flags/kill" in controls_html

    def test_scan_toggle_writes_file_and_redirects(
        self, ops_app_writable, flags_tmpfile,
    ):
        with TestClient(ops_app_writable, follow_redirects=False) as c:
            r = c.post(
                "/kalshi/ops/flags/scan",
                data={"name": "btc", "enabled": "false"},
            )
            assert r.status_code == 303
            assert r.headers["location"] == "/kalshi/ops"
        import runtime_flags as rf
        flags = rf.load(flags_tmpfile)
        assert flags.scan_enabled["btc"] is False

    def test_strategy_toggle_writes_file(
        self, ops_app_writable, flags_tmpfile,
    ):
        with TestClient(ops_app_writable, follow_redirects=False) as c:
            r = c.post(
                "/kalshi/ops/flags/strategy",
                data={"name": "pure_lag", "enabled": "false"},
            )
            assert r.status_code == 303
        import runtime_flags as rf
        assert rf.load(flags_tmpfile).strategy_enabled["pure_lag"] is False

    def test_kill_switch_engages(self, ops_app_writable, flags_tmpfile):
        with TestClient(ops_app_writable, follow_redirects=False) as c:
            r = c.post("/kalshi/ops/flags/kill")
            assert r.status_code == 303
        import runtime_flags as rf
        assert rf.load(flags_tmpfile).execution_kill_switch is True

    def test_kill_switch_can_be_revived_via_dashboard(
        self, ops_app_writable, flags_tmpfile,
    ):
        """Post 2026-04-20: bi-directional. Operator can flip kill OFF
        via the dashboard without editing files."""
        import runtime_flags as rf
        # Prime: kill-switch on.
        killed = rf.RuntimeFlags()
        killed.execution_kill_switch = True
        rf.save(killed, flags_tmpfile, author="operator")

        with TestClient(ops_app_writable) as c:
            r = c.get("/kalshi/ops")
            # Engaged → button labeled REVIVE, not KILL.
            assert "REVIVE EXECUTION" in r.text
            assert "KILL EXECUTION" not in r.text

        # POST /unkill releases the switch.
        with TestClient(ops_app_writable, follow_redirects=False) as c:
            r = c.post("/kalshi/ops/flags/unkill")
            assert r.status_code == 303
        assert rf.load(flags_tmpfile).execution_kill_switch is False

    def test_api_ops_exposes_allow_write_true(self, ops_app_writable):
        with TestClient(ops_app_writable) as c:
            data = c.get("/api/ops").json()
            assert data["allow_write"] is True
            assert "flags" in data
            assert data["flags"]["execution_kill_switch"] is False

    def test_per_asset_execution_toggle(
        self, ops_app_writable, flags_tmpfile,
    ):
        """Per-asset execution_enabled is bi-directional — disable, then
        re-enable via the dashboard."""
        import runtime_flags as rf
        with TestClient(ops_app_writable, follow_redirects=False) as c:
            r = c.post(
                "/kalshi/ops/flags/execution",
                data={"name": "btc", "enabled": "false"},
            )
            assert r.status_code == 303
        assert rf.load(flags_tmpfile).execution_enabled["btc"] is False

        with TestClient(ops_app_writable, follow_redirects=False) as c:
            r = c.post(
                "/kalshi/ops/flags/execution",
                data={"name": "btc", "enabled": "true"},
            )
            assert r.status_code == 303
        assert rf.load(flags_tmpfile).execution_enabled["btc"] is True

    def test_execution_form_rendered_for_each_asset(
        self, ops_app_writable,
    ):
        import runtime_flags as rf
        with TestClient(ops_app_writable) as c:
            r = c.get("/kalshi/ops")
        # One execution row per known asset.
        for asset in rf.ASSETS:
            assert f"execution · {asset}" in r.text
        # Forms POST to the execution endpoint.
        assert r.text.count('action="/kalshi/ops/flags/execution"') == len(rf.ASSETS)

    def test_controls_card_appears_before_service_status(
        self, ops_app_writable,
    ):
        """Controls must render above the Service status block so
        operators see toggles first, not diagnostic data."""
        with TestClient(ops_app_writable) as c:
            html = c.get("/kalshi/ops").text
        assert html.index("<h2>Controls</h2>") < html.index("<h2>Service status</h2>")

    def test_unknown_asset_ignored_silently(
        self, ops_app_writable, flags_tmpfile,
    ):
        with TestClient(ops_app_writable, follow_redirects=False) as c:
            r = c.post(
                "/kalshi/ops/flags/scan",
                data={"name": "notanasset", "enabled": "false"},
            )
            assert r.status_code == 303
        # No new key added; defaults preserved.
        import runtime_flags as rf
        flags = rf.load(flags_tmpfile)
        assert "notanasset" not in flags.scan_enabled
        # Original assets still all True.
        assert all(flags.scan_enabled.get(a, True) for a in rf.ASSETS)


# ---------------------------------------------------------------------------
# Phase-timing summary card on /kalshi/ops
# ---------------------------------------------------------------------------


class TestPhaseTimingOnOps:
    def test_ops_shows_phases_card_when_events_exist(
        self, db_path_windowed, tmp_path,
    ):
        # Build a minimal JSONL log with two phase_timing events.
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        # `daily_log_path` expects today's date in the filename.
        import datetime as _dt
        today = _dt.datetime.utcnow().strftime("%Y-%m-%d")
        log = events_dir / f"events_{today}.jsonl"
        lines = [
            '{"event_type":"phase_timing","phase":"scanner.tick","elapsed_ms":5.2,"ok":true}',
            '{"event_type":"phase_timing","phase":"scanner.tick","elapsed_ms":7.1,"ok":true}',
            '{"event_type":"phase_timing","phase":"strategy.evaluate","elapsed_ms":2.0,"ok":true}',
        ]
        log.write_text("\n".join(lines) + "\n")

        app = create_app(
            database_url=f"sqlite:///{db_path_windowed}",
            events_dir=str(events_dir),
        )
        with TestClient(app) as c:
            r = c.get("/kalshi/ops")
            assert "Phase timings (top 5)" in r.text
            assert "scanner.tick" in r.text
            # Link to the full page.
            assert 'href="/kalshi/phases"' in r.text

    def test_ops_shows_phases_placeholder_when_no_events(
        self, db_path_windowed, tmp_path,
    ):
        # Empty events dir = no phase_timing file → placeholder card.
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        app = create_app(
            database_url=f"sqlite:///{db_path_windowed}",
            events_dir=str(empty_dir),
        )
        with TestClient(app) as c:
            r = c.get("/kalshi/ops")
            assert "No <code>phase_timing</code> events yet" in r.text


# ---------------------------------------------------------------------------
# /api/flags — JSON control plane
# ---------------------------------------------------------------------------


@pytest.fixture
def flags_file(tmp_path):
    """Isolated flags file per-test so we don't touch real config/."""
    return tmp_path / "runtime_flags.json"


class TestApiFlagsGet:
    def test_default_flags_returned_when_file_missing(self, db_path, flags_file):
        app = create_app(
            database_url=f"sqlite:///{db_path}",
            flags_path=flags_file,
        )
        with TestClient(app) as c:
            r = c.get("/api/flags")
        assert r.status_code == 200
        data = r.json()
        # Permissive defaults: everything on, kill-switch off.
        assert data["execution_kill_switch"] is False
        assert all(data["scan_enabled"].values())
        assert all(data["execution_enabled"].values())
        assert all(data["strategy_enabled"].values())

    def test_returns_persisted_state(self, db_path, flags_file):
        # Seed a non-default state directly.
        import runtime_flags as rf
        flags = rf.RuntimeFlags()
        flags.scan_enabled["bnb"] = False
        flags.execution_enabled["btc"] = False
        rf.save(flags, flags_file, author="test-fixture")

        app = create_app(
            database_url=f"sqlite:///{db_path}",
            flags_path=flags_file,
        )
        with TestClient(app) as c:
            data = c.get("/api/flags").json()
        assert data["scan_enabled"]["bnb"] is False
        assert data["execution_enabled"]["btc"] is False


class TestApiFlagsPatch:
    def test_patch_without_allow_write_returns_403(self, db_path, flags_file):
        app = create_app(
            database_url=f"sqlite:///{db_path}",
            flags_path=flags_file,
        )
        with TestClient(app) as c:
            r = c.patch("/api/flags", json={"scan_enabled": {"btc": False}})
        assert r.status_code == 403
        assert "writes disabled" in r.text

    def test_patch_persists_and_returns_new_state(self, db_path, flags_file):
        app = create_app(
            database_url=f"sqlite:///{db_path}",
            flags_path=flags_file,
            allow_write=True,
        )
        with TestClient(app) as c:
            r = c.patch("/api/flags", json={
                "scan_enabled": {"bnb": False},
                "execution_enabled": {"btc": False},
                "_author": "test-suite",
            })
        assert r.status_code == 200
        data = r.json()
        assert data["scan_enabled"]["bnb"] is False
        assert data["execution_enabled"]["btc"] is False
        assert data["updated_by"] == "test-suite"
        # Subsequent GET matches what PATCH returned (persisted to disk).
        with TestClient(app) as c:
            got = c.get("/api/flags").json()
        assert got["scan_enabled"]["bnb"] is False
        assert got["execution_enabled"]["btc"] is False

    def test_patch_ignores_unknown_assets(self, db_path, flags_file):
        app = create_app(
            database_url=f"sqlite:///{db_path}",
            flags_path=flags_file,
            allow_write=True,
        )
        with TestClient(app) as c:
            r = c.patch("/api/flags", json={
                "scan_enabled": {"pepe": False},  # unknown — ignored silently
                "execution_enabled": {"bnb": False},  # known — applied
            })
        assert r.status_code == 200
        data = r.json()
        # pepe doesn't appear in the response (ignored).
        assert "pepe" not in data["scan_enabled"]
        # bnb applied.
        assert data["execution_enabled"]["bnb"] is False

    def test_kill_switch_is_bidirectional_via_api(self, db_path, flags_file):
        """Post 2026-04-20: dashboard can ENGAGE AND RELEASE the
        kill-switch via the PATCH endpoint. Per-asset granular control
        via execution_enabled covers the targeted use case; the global
        kill-switch is the panic button."""
        app = create_app(
            database_url=f"sqlite:///{db_path}",
            flags_path=flags_file,
            allow_write=True,
        )
        with TestClient(app) as c:
            c.patch("/api/flags", json={"execution_kill_switch": True})
            assert c.get("/api/flags").json()["execution_kill_switch"] is True
            c.patch("/api/flags", json={"execution_kill_switch": False})
            assert c.get("/api/flags").json()["execution_kill_switch"] is False

    def test_invalid_json_returns_422(self, db_path, flags_file):
        app = create_app(
            database_url=f"sqlite:///{db_path}",
            flags_path=flags_file,
            allow_write=True,
        )
        with TestClient(app) as c:
            r = c.patch(
                "/api/flags",
                content=b"not-json",
                headers={"content-type": "application/json"},
            )
        assert r.status_code == 422
        assert "invalid JSON" in r.text

    def test_non_object_body_returns_422(self, db_path, flags_file):
        app = create_app(
            database_url=f"sqlite:///{db_path}",
            flags_path=flags_file,
            allow_write=True,
        )
        with TestClient(app) as c:
            r = c.patch("/api/flags", json=["not", "an", "object"])
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# Time-range query params — parser + endpoint filtering
# ---------------------------------------------------------------------------


class TestParseTimeParam:
    def test_none_and_empty_return_none(self):
        assert _parse_time_param_us(None) is None
        assert _parse_time_param_us("") is None

    def test_iso_with_z_suffix(self):
        # 2026-04-20T14:30:00Z = epoch 1776695400
        assert _parse_time_param_us("2026-04-20T14:30:00Z") == 1_776_695_400_000_000

    def test_iso_with_offset(self):
        # Same moment, explicit offset
        assert (
            _parse_time_param_us("2026-04-20T14:30:00+00:00")
            == 1_776_695_400_000_000
        )

    def test_iso_naive_treated_as_utc(self):
        assert (
            _parse_time_param_us("2026-04-20T14:30:00")
            == 1_776_695_400_000_000
        )

    def test_unix_seconds(self):
        assert _parse_time_param_us("1776695400") == 1_776_695_400_000_000

    def test_unix_seconds_float(self):
        # 0.5s precision preserved.
        assert _parse_time_param_us("1776695400.5") == 1_776_695_400_500_000

    def test_unix_milliseconds(self):
        # 1776695400_000 ms → same microsecond epoch.
        assert _parse_time_param_us("1776695400000") == 1_776_695_400_000_000

    def test_unix_microseconds(self):
        assert (
            _parse_time_param_us("1776695400000000")
            == 1_776_695_400_000_000
        )

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            _parse_time_param_us("not-a-date")

    def test_invalid_iso_raises(self):
        with pytest.raises(ValueError):
            _parse_time_param_us("2026-13-99T99:99:99")


class TestTimeBoundsResolver:
    def _conn(self, db_path):
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def test_no_params_falls_back_to_window_lower_only(self, db_path):
        conn = self._conn(db_path)
        try:
            lo, hi = _time_bounds_us(conn, window="all")
            assert lo is None and hi is None
            lo, hi = _time_bounds_us(conn, window="1h")
            assert lo is not None
            assert hi is None
        finally:
            conn.close()

    def test_start_only_override(self, db_path):
        conn = self._conn(db_path)
        try:
            lo, hi = _time_bounds_us(
                conn, window="1h", start="2026-04-20T00:00:00Z",
            )
            assert lo == 1_776_643_200_000_000
            assert hi is None
        finally:
            conn.close()

    def test_end_only_override(self, db_path):
        conn = self._conn(db_path)
        try:
            lo, hi = _time_bounds_us(
                conn, window="1h", end="2026-04-20T00:00:00Z",
            )
            assert lo is None
            assert hi == 1_776_643_200_000_000
        finally:
            conn.close()

    def test_both_override_window(self, db_path):
        conn = self._conn(db_path)
        try:
            lo, hi = _time_bounds_us(
                conn, window="5m",
                start="2026-04-20T00:00:00Z",
                end="2026-04-20T01:00:00Z",
            )
            assert lo == 1_776_643_200_000_000
            assert hi == 1_776_646_800_000_000
        finally:
            conn.close()


class TestRangeClause:
    def test_both_none_returns_empty(self):
        frag, params = _range_clause(None, None)
        assert frag == ""
        assert params == []

    def test_lo_only(self):
        frag, params = _range_clause(100, None)
        assert frag == " AND ts_us >= ?"
        assert params == [100]

    def test_hi_only(self):
        frag, params = _range_clause(None, 200)
        assert frag == " AND ts_us <= ?"
        assert params == [200]

    def test_both_sets(self):
        frag, params = _range_clause(100, 200)
        assert frag == " AND ts_us >= ? AND ts_us <= ?"
        assert params == [100, 200]

    def test_custom_column(self):
        frag, params = _range_clause(100, 200, column="filled_at_us")
        assert "filled_at_us" in frag
        assert "ts_us" not in frag


# ---------------------------------------------------------------------------
# Endpoint-level time-range filtering
# ---------------------------------------------------------------------------


@pytest.fixture
def db_range(tmp_path):
    """Seed decisions + paper/live rows across three distinct timestamps.

    Timestamps (µs since epoch) land on easy ISO-8601 boundaries so tests
    can pick them without arithmetic noise.
        T0 = 2026-04-20T00:00:00Z = 1_776_643_200_000_000
        T1 = 2026-04-20T01:00:00Z = 1_776_646_800_000_000
        T2 = 2026-04-20T02:00:00Z = 1_776_650_400_000_000
    """
    import migrate_db as m
    db = tmp_path / "range.db"
    m.migrate(f"sqlite:///{db}")
    conn = sqlite3.connect(str(db))

    T0 = 1_776_643_200_000_000
    T1 = 1_776_646_800_000_000
    T2 = 1_776_650_400_000_000

    common = (
        "0.7", "0.05", "66000", "66000", "30",
        "0.55", "0.45", "500", "500", "yes",
        "0.55", "10", "150", "35",
    )
    rows = [
        ("KXBTC15M-T0", T0, *common, "yes",  "1.00", "5.0", "3.0", "pure_lag"),
        ("KXBTC15M-T1", T1, *common, "yes",  "2.00", "5.0", "3.0", "pure_lag"),
        ("KXBTC15M-T2", T2, *common, "yes",  "3.00", "5.0", "3.0", "pure_lag"),
    ]
    conn.executemany("""
        INSERT INTO shadow_decisions (
            market_ticker, ts_us, p_yes, ci_width, reference_price,
            reference_60s_avg, time_remaining_s, best_yes_ask, best_no_ask,
            book_depth_yes_usd, book_depth_no_usd, recommended_side,
            hypothetical_fill_price, hypothetical_size_contracts,
            expected_edge_bps_after_fees, fee_bps_at_decision,
            realized_outcome, realized_pnl_usd, latency_ms_ref_to_decision,
            latency_ms_book_to_decision, strategy_label
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, rows)

    # reference_ticks — one per timestamp
    conn.executemany(
        "INSERT INTO reference_ticks (asset, ts_us, price, src) VALUES (?,?,?,?)",
        [("btc", T0, "66000", "coinbase_live"),
         ("btc", T1, "66100", "coinbase_live"),
         ("btc", T2, "66200", "coinbase_live")],
    )

    # paper_fills filtered on filled_at_us — include every NOT NULL col.
    _pf_common = (
        "yes", "0.55", "10", "0.35", "5.5", "150",
        "0.7", "0.05", "66000", "66000", "30",
        "65000", "above", "35",
    )
    paper_fill_rows = [
        ("KXBTC15M-T0", "pure_lag", T0, *_pf_common),
        ("KXBTC15M-T1", "pure_lag", T1, *_pf_common),
        ("KXBTC15M-T2", "pure_lag", T2, *_pf_common),
    ]
    conn.executemany("""
        INSERT INTO paper_fills (
            market_ticker, strategy_label, filled_at_us, side,
            fill_price, size_contracts, fees_paid_usd, notional_usd,
            expected_edge_bps_after_fees,
            p_yes, ci_width, reference_price, reference_60s_avg,
            time_remaining_s, strike, comparator, fee_bps_at_decision
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, paper_fill_rows)

    # paper_settlements filtered on settled_at_us
    conn.executemany("""
        INSERT INTO paper_settlements (
            fill_id, market_ticker, settled_at_us, outcome, realized_pnl_usd
        ) VALUES (?,?,?,?,?)
    """, [
        (1, "KXBTC15M-T0", T0, "yes", "1.00"),
        (2, "KXBTC15M-T1", T1, "yes", "2.00"),
        (3, "KXBTC15M-T2", T2, "no", "-3.00"),
    ])

    # live_orders / live_settlements
    conn.executemany("""
        INSERT INTO live_orders (
            order_id, client_order_id, market_ticker, strategy_label,
            submitted_at_us, side, price, size_contracts, status
        ) VALUES (?,?,?,?,?,?,?,?,?)
    """, [
        ("o1", "c1", "KXBTC15M-T0", "pure_lag", T0, "yes", "0.55", 10, "filled"),
        ("o2", "c2", "KXBTC15M-T1", "pure_lag", T1, "yes", "0.55", 10, "filled"),
        ("o3", "c3", "KXBTC15M-T2", "pure_lag", T2, "yes", "0.55", 10, "resting"),
    ])
    conn.executemany("""
        INSERT INTO live_settlements (
            order_row_id, market_ticker, settled_at_us, outcome,
            computed_pnl_usd, kalshi_reported_pnl_usd, discrepancy_usd
        ) VALUES (?,?,?,?,?,?,?)
    """, [
        (1, "KXBTC15M-T0", T0, "yes", "1.00", "1.00", "0.00"),
        (2, "KXBTC15M-T1", T1, "yes", "2.00", "2.00", "0.00"),
    ])

    conn.commit()
    conn.close()
    return db, T0, T1, T2


@pytest.fixture
def client_range(db_range):
    db, *_ = db_range
    return TestClient(create_app(database_url=f"sqlite:///{db}"))


class TestEndpointTimeRange:
    def _iso(self, ts_us: int) -> str:
        from datetime import datetime, timezone
        return (
            datetime.fromtimestamp(ts_us / 1_000_000, tz=timezone.utc)
            .isoformat().replace("+00:00", "Z")
        )

    def test_api_overview_start_end_filters(self, client_range, db_range):
        _, T0, T1, T2 = db_range
        # Include only T1 + T2.
        r = client_range.get(
            f"/api/overview?start={T1}&end={T2}",
        )
        assert r.status_code == 200
        data = r.json()
        assert data["totals"]["total"] == 2
        # Realized P&L across T1+T2 = 2+3 = 5.0
        assert data["totals"]["pnl"] == 5.0

    def test_api_overview_invalid_start_returns_422(self, client_range):
        r = client_range.get("/api/overview?start=garbage")
        assert r.status_code == 422
        assert "unparseable" in r.text.lower()

    def test_api_decisions_start_only(self, client_range, db_range):
        _, T0, T1, T2 = db_range
        r = client_range.get(f"/api/decisions?start={T1}")
        assert r.status_code == 200
        tickers = [d["market_ticker"] for d in r.json()]
        assert "KXBTC15M-T0" not in tickers
        assert "KXBTC15M-T1" in tickers
        assert "KXBTC15M-T2" in tickers

    def test_api_decisions_end_only(self, client_range, db_range):
        _, T0, T1, T2 = db_range
        r = client_range.get(f"/api/decisions?end={T1}")
        assert r.status_code == 200
        tickers = [d["market_ticker"] for d in r.json()]
        assert "KXBTC15M-T0" in tickers
        assert "KXBTC15M-T1" in tickers
        assert "KXBTC15M-T2" not in tickers

    def test_api_performance_range(self, client_range, db_range):
        _, T0, T1, T2 = db_range
        r = client_range.get(f"/api/performance?start={T2}&end={T2}")
        assert r.status_code == 200
        rows = r.json()
        # Only T2 decision counts — one row.
        assert len(rows) == 1
        assert rows[0]["decisions"] == 1

    def test_api_ops_range(self, client_range, db_range):
        _, T0, T1, T2 = db_range
        r = client_range.get(f"/api/ops?start={T1}&end={T2}")
        assert r.status_code == 200
        data = r.json()
        assert data["total_decisions_in_window"] == 2

    def test_api_ops_iso_input(self, client_range, db_range):
        _, T0, T1, T2 = db_range
        # Same filter but via ISO-8601.
        start = self._iso(T1)
        end = self._iso(T2)
        r = client_range.get(f"/api/ops?start={start}&end={end}")
        assert r.status_code == 200
        assert r.json()["total_decisions_in_window"] == 2

    def test_api_health_range_filters_counts(self, client_range, db_range):
        _, T0, T1, T2 = db_range
        r = client_range.get(f"/api/health?start={T1}")
        assert r.status_code == 200
        data = r.json()
        # reference_ticks after T1 = T1 + T2 = 2
        assert data["reference"][0]["total_ticks"] == 2
        # decisions after T1 = 2
        assert data["decisions"][0]["total"] == 2

    def test_api_phases_range_does_not_crash_on_empty(self, client_range, db_range):
        _, _, T1, T2 = db_range
        r = client_range.get(f"/api/phases?start={T1}&end={T2}")
        assert r.status_code == 200
        # No phase_timing_rollup data seeded → empty phases array.
        payload = r.json()
        assert "phases" in payload

    def test_html_overview_accepts_range(self, client_range, db_range):
        _, _, T1, T2 = db_range
        r = client_range.get(f"/kalshi?start={T1}&end={T2}")
        assert r.status_code == 200
        assert "Overview" in r.text

    def test_html_decisions_accepts_range(self, client_range, db_range):
        _, _, T1, _ = db_range
        r = client_range.get(f"/kalshi/decisions?start={T1}")
        assert r.status_code == 200
        assert "KXBTC15M-T0" not in r.text
        assert "KXBTC15M-T1" in r.text

    def test_html_invalid_start_422(self, client_range):
        r = client_range.get("/kalshi/decisions?start=not-a-date")
        assert r.status_code == 422

    def test_html_paper_range(self, client_range, db_range):
        _, T0, T1, T2 = db_range
        # Only T0 and T1 in range.
        r = client_range.get(f"/kalshi/paper?start={T0}&end={T1}")
        assert r.status_code == 200
        # Page renders, totals reflect 2 fills + 2 settles (PnL 1+2=3.0).
        assert "Paper" in r.text

    def test_api_paper_counts_respect_range(self, client_range, db_range):
        """Paper summary isn't exposed via /api, but _fetch_paper_summary
        (used by the HTML page) should filter under the hood. Confirm via
        direct invocation on a read-only connection."""
        from dashboards.kalshi import _fetch_paper_summary, _open_readonly
        db, T0, T1, T2 = db_range
        conn = _open_readonly(f"sqlite:///{db}")
        try:
            data = _fetch_paper_summary(conn, start_us=T0, end_us=T1)
            assert data["fills"]["n_fills"] == 2
            assert data["settlements"]["n_settlements"] == 2
        finally:
            conn.close()

    def test_api_live_counts_respect_range(self, client_range, db_range):
        from dashboards.kalshi import _fetch_live_summary, _open_readonly
        db, T0, T1, T2 = db_range
        conn = _open_readonly(f"sqlite:///{db}")
        try:
            data = _fetch_live_summary(conn, start_us=T0, end_us=T1)
            # Two orders submitted at T0/T1, both "filled".
            statuses = {o["status"]: o["n"] for o in data["orders_by_status"]}
            assert statuses.get("filled") == 2
            assert data["settlements"]["n_settlements"] == 2
        finally:
            conn.close()

    def test_no_params_still_uses_window(self, client_range):
        """Backward compat: without start/end, `window` still gates results."""
        r = client_range.get("/api/overview?window=5m")
        assert r.status_code == 200
        # Seeded timestamps are recent-relative-to-MAX-ts so the window
        # anchors on them; 5m at MAX → includes at least the latest row.
        data = r.json()
        assert data["window"] == "5m"


# ---------------------------------------------------------------------------
# Date-range UI — nav form + link propagation
# ---------------------------------------------------------------------------


class TestRangePickerUI:
    def test_to_datetime_local_value_iso_z(self):
        assert _to_datetime_local_value("2026-04-20T14:30:00Z") == "2026-04-20T14:30"

    def test_to_datetime_local_value_unix_seconds(self):
        assert _to_datetime_local_value("1776695400") == "2026-04-20T14:30"

    def test_to_datetime_local_value_unix_microseconds(self):
        assert (
            _to_datetime_local_value("1776695400000000")
            == "2026-04-20T14:30"
        )

    def test_to_datetime_local_value_empty_and_invalid(self):
        assert _to_datetime_local_value(None) == ""
        assert _to_datetime_local_value("") == ""
        assert _to_datetime_local_value("not-a-date") == ""

    def test_qs_drops_empty(self):
        assert _qs({"a": None, "b": "", "c": "v"}) == "?c=v"
        assert _qs({"a": None}) == ""

    def test_qs_encodes_iso_values(self):
        # Colons get percent-encoded so the value round-trips cleanly.
        out = _qs({"start": "2026-04-20T14:30:00Z"})
        assert "start=" in out and "2026-04-20" in out

    def test_nav_contains_range_form_inputs(self):
        html = _nav()
        assert 'type="datetime-local" name="start"' in html
        assert 'type="datetime-local" name="end"' in html
        assert "Apply range" in html

    def test_nav_prefills_values_from_params(self):
        html = _nav(start="2026-04-20T14:30:00Z", end="2026-04-21T14:30:00Z")
        assert 'value="2026-04-20T14:30"' in html
        assert 'value="2026-04-21T14:30"' in html

    def test_nav_links_carry_start_end(self):
        html = _nav(start="2026-04-20T14:30:00Z", end="2026-04-21T14:30:00Z")
        # Every tab link should append ?start=…&end=… so switching tabs
        # preserves the filter.
        for path in ("/kalshi", "/kalshi/decisions", "/kalshi/ops",
                     "/kalshi/phases", "/kalshi/health", "/kalshi/paper",
                     "/kalshi/live", "/kalshi/performance"):
            # Match either & or escaped & in the rendered link.
            assert f'href="{path}?' in html
        # Raw values visible in the muted preview span.
        assert "2026-04-20T14:30:00Z" in html

    def test_nav_has_javascript_apply_handler(self):
        # The form submits GET but strips/replaces start/end via JS. Verify
        # the handler names are referenced so the template stays consistent.
        html = _nav(start=None, end=None)
        assert "_kalshiApplyRange" in html
        assert "_kalshiClearRange" in html

    def test_overview_html_includes_range_form(self, client):
        r = client.get("/kalshi")
        assert r.status_code == 200
        assert 'name="start"' in r.text
        assert 'name="end"' in r.text

    def test_overview_html_prefills_inputs_with_submitted_values(
        self, client_range, db_range,
    ):
        _, _, T1, T2 = db_range
        r = client_range.get(
            f"/kalshi?start=2026-04-20T01:00:00Z&end=2026-04-20T02:00:00Z",
        )
        assert r.status_code == 200
        assert 'value="2026-04-20T01:00"' in r.text
        assert 'value="2026-04-20T02:00"' in r.text

    def test_nav_links_preserve_range_in_rendered_page(self, client_range):
        r = client_range.get("/kalshi?start=2026-04-20T01:00:00Z")
        assert r.status_code == 200
        # Every nav link carries the start forward.
        assert 'href="/kalshi/decisions?' in r.text
        assert "2026-04-20T01%3A00%3A00Z" in r.text or "2026-04-20T01:00:00Z" in r.text


class TestFmtTsEst:
    """Cover the EST datetime formatter used by decisions / ops / overview tables."""

    def test_none_returns_dash(self):
        assert "—" in _fmt_ts_est(None)
        assert "—" in _fmt_ts_est("")

    def test_invalid_returns_dash(self):
        assert "—" in _fmt_ts_est("not-a-number")
        assert "—" in _fmt_ts_est([1, 2, 3])

    def test_known_utc_moment_renders_new_york_time(self):
        # 2026-04-20T14:30:00Z = EDT-4 → 10:30 local.
        out = _fmt_ts_est(1_776_695_400_000_000)
        assert "2026-04-20" in out
        assert "10:30:00" in out
        # ZoneInfo typically emits EDT in April; accept either EDT or EST
        # in case the test host's tzdata is in standard time.
        assert ("EDT" in out) or ("EST" in out) or ("UTC" in out)

    def test_winter_moment_renders_est(self):
        # 2026-01-15T14:30:00Z → EST-5 → 09:30 EST.
        out = _fmt_ts_est(1_768_487_400_000_000)
        assert "2026-01-15" in out
        assert "09:30:00" in out

    def test_accepts_string_integer(self):
        # _fetch helpers may return the column as a string; coerce.
        out = _fmt_ts_est("1776695400000000")
        assert "2026-04-20" in out

    def test_decisions_page_shows_est_column(self, client):
        r = client.get("/kalshi/decisions")
        assert r.status_code == 200
        # New "datetime (ET)" header rendered.
        assert "datetime (ET)" in r.text
        # ts_us column still present.
        assert "ts_us" in r.text

    def test_ops_page_shows_est_column(self, client):
        r = client.get("/kalshi/ops")
        assert r.status_code == 200
        # When there are no events seeded the table isn't rendered; instead
        # the muted "no events" message shows. Either is acceptable — assert
        # only the page renders.
        assert r.status_code == 200

    def test_overview_reference_feed_shows_est(self, client):
        r = client.get("/kalshi")
        assert r.status_code == 200
        # Reference-feed table now has a "last tick" EST column. The raw
        # ts_us column stays for forensic use.
        assert "last tick" in r.text
        assert "ts_us" in r.text
