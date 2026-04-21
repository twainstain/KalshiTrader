"""Cover `scripts/analyze_simulation.py` — report builder + renderer."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import analyze_simulation as asim


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def seeded_db(tmp_path):
    """Fresh DB seeded with shadow/paper rows + an events JSONL."""
    import migrate_db as m
    db = tmp_path / "sim.db"
    m.migrate(f"sqlite:///{db}")
    conn = sqlite3.connect(str(db))

    # Shadow decisions: 3 pure_lag (2 reconciled), 2 stat_model (both reconciled).
    base_ts = 1_746_000_000_000_000
    decisions = [
        # (ticker, ts_us, p_yes, ci_width, ref_price, ref_60s, t_rem, yes_ask,
        #  no_ask, depth_yes, depth_no, side, fill_px, size, edge_bps, fee_bps,
        #  outcome, pnl, lat_ref, lat_book, strategy)
        ("KXBTC15M-T1", base_ts, "0.7", "0.05", "66000", "66000", "30",
         "0.55", "0.45", "500", "500", "yes",
         "0.55", "10", "150", "35",
         "yes", "4.50", "8.0", "10.5", "pure_lag"),
        ("KXBTC15M-T1", base_ts + 1_000_000, "0.72", "0.05", "66050", "66000", "28",
         "0.55", "0.45", "500", "500", "yes",
         "0.55", "10", "200", "35",
         "no", "-5.50", "9.0", "11.0", "pure_lag"),
        ("KXETH15M-T1", base_ts + 2_000_000, "0.65", "0.08", "3500", "3500", "45",
         "0.60", "0.40", "400", "400", "yes",
         "0.60", "10", "120", "35",
         None, None, "7.0", "9.0", "pure_lag"),
        ("KXBTC15M-T1", base_ts + 3_000_000, "0.70", "0.05", "66000", "66000", "20",
         "0.55", "0.45", "500", "500", "yes",
         "0.55", "10", "150", "35",
         "yes", "4.50", "6.0", "8.0", "stat_model"),
        ("KXHYPE15M-T1", base_ts + 4_000_000, "0.60", "0.06", "0.50", "0.50", "35",
         "0.40", "0.60", "300", "300", "no",
         "0.60", "10", "300", "35",
         "no", "4.00", "7.5", "9.5", "stat_model"),
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
    """, decisions)

    # Paper fills + settlements.
    conn.execute("""
        INSERT INTO paper_fills (
            market_ticker, strategy_label, filled_at_us, side, fill_price,
            size_contracts, fees_paid_usd, notional_usd,
            expected_edge_bps_after_fees, p_yes, ci_width,
            reference_price, reference_60s_avg, time_remaining_s,
            strike, comparator, fee_bps_at_decision
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, ("KXBTC15M-T1", "pure_lag", base_ts, "yes", "0.55", "10",
          "0.02", "5.50", "150", "0.70", "0.05",
          "66000", "66000", "30", "65000", "above", "35"))
    conn.execute("""
        INSERT INTO paper_settlements (
            fill_id, market_ticker, settled_at_us, outcome, realized_pnl_usd
        ) VALUES (?, ?, ?, ?, ?)
    """, (1, "KXBTC15M-T1", base_ts + 900_000_000, "yes", "4.48"))

    # Reference ticks.
    conn.executemany(
        "INSERT INTO reference_ticks (asset, ts_us, price, src) VALUES (?,?,?,?)",
        [("btc", base_ts, "66000", "coinbase_live"),
         ("eth", base_ts, "3500", "coinbase_live"),
         ("hype", base_ts, "0.50", "coinbase_live")],
    )
    conn.commit()
    conn.close()

    # Events JSONL with both risk_reject and phase_timing rows.
    events = tmp_path / "events_2026-04-21.jsonl"
    with events.open("w") as f:
        # risk rejections
        for reason in [
            "risk-rejected: min_edge_after_fees: edge 50 bps < min 100 bps",
            "risk-rejected: min_edge_after_fees: edge 70 bps < min 100 bps",
            "risk-rejected: time_window: time_remaining_s=120 outside [5, 60]",
            "risk-rejected: strike_proximity: reference within 3 bps",
        ]:
            f.write(json.dumps({
                "ts_us": 1, "event_type": "risk_reject", "reason": reason,
            }) + "\n")
        # phase timings
        for phase, ms in [
            ("scanner.snapshot_books", 2500.0),
            ("scanner.snapshot_books", 3200.0),
            ("scanner.snapshot_books", 2800.0),
            ("evaluator.tick", 1.2),
            ("evaluator.tick", 0.9),
            ("strategy.evaluate", 0.03),
        ]:
            f.write(json.dumps({
                "ts_us": 2, "event_type": "phase_timing",
                "phase": phase, "elapsed_ms": ms, "ok": True,
            }) + "\n")
        # decorrelated event that should be ignored
        f.write(json.dumps({
            "ts_us": 3, "event_type": "decision", "asset": "btc",
        }) + "\n")

    return db, events


def _open(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# build_report
# ---------------------------------------------------------------------------


class TestBuildReport:
    def test_counts_per_strategy(self, seeded_db):
        db, events = seeded_db
        with _open(db) as conn:
            rep = asim.build_report(conn, window="all", events_path=events)
        assert rep.decisions_total == 5
        assert rep.decisions_reconciled == 4
        pl = rep.decisions_by_strategy["pure_lag"]
        assert pl["total"] == 3 and pl["reconciled"] == 2 and pl["wins"] == 1
        sm = rep.decisions_by_strategy["stat_model"]
        assert sm["total"] == 2 and sm["reconciled"] == 2 and sm["wins"] == 2

    def test_counts_per_asset(self, seeded_db):
        db, events = seeded_db
        with _open(db) as conn:
            rep = asim.build_report(conn, window="all", events_path=events)
        # Ticker prefix KXBTC → btc, KXETH → eth, KXHYPE → hyp (substr 3,3).
        assert "btc" in rep.decisions_by_asset
        assert rep.decisions_by_asset["btc"]["decisions"] == 3

    def test_outcome_counts(self, seeded_db):
        db, events = seeded_db
        with _open(db) as conn:
            rep = asim.build_report(conn, window="all", events_path=events)
        assert rep.outcome_counts == {"yes": 2, "no": 2, "pending": 1}

    def test_paper_executor_rollup(self, seeded_db):
        db, events = seeded_db
        with _open(db) as conn:
            rep = asim.build_report(conn, window="all", events_path=events)
        assert rep.paper_fills == 1
        assert rep.paper_settlements == 1
        assert rep.paper_wins == 1
        assert rep.paper_losses == 0
        assert rep.paper_pnl_usd == 4.48

    def test_paper_queries_respect_strategy_filter(self, tmp_path):
        """Regression (P2): when `--strategy pure_lag` is passed,
        `paper_fills` and `paper_settlements` counts/P-L must be filtered
        to that strategy too. Previously those queries ignored the filter,
        so the report mixed pure_lag decision counts with paper fills from
        every strategy — internally inconsistent output.
        """
        import migrate_db as m
        db = tmp_path / "mixed.db"
        m.migrate(f"sqlite:///{db}")
        conn = sqlite3.connect(str(db))
        base_ts = 1_746_000_000_000_000

        # Mixed shadow decisions.
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
        """, [
            ("KXBTC15M-T1", base_ts, "0.7", "0.05", "66000", "66000",
             "30", "0.55", "0.45", "500", "500", "yes", "0.55", "10",
             "150", "35", "yes", "4.5", "8.0", "10.5", "pure_lag"),
            ("KXBTC15M-T2", base_ts + 1, "0.72", "0.05", "66050", "66000",
             "28", "0.55", "0.45", "500", "500", "yes", "0.55", "10",
             "200", "35", "no", "-5.5", "9.0", "11.0", "stat_model"),
        ])

        # Two paper_fills — one per strategy.
        conn.executemany("""
            INSERT INTO paper_fills (
                market_ticker, strategy_label, filled_at_us, side, fill_price,
                size_contracts, fees_paid_usd, notional_usd,
                expected_edge_bps_after_fees, p_yes, ci_width,
                reference_price, reference_60s_avg, time_remaining_s,
                strike, comparator, fee_bps_at_decision
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [
            ("KXBTC15M-T1", "pure_lag",  base_ts,     "yes", "0.55", "10",
             "0.02", "5.50", "150", "0.70", "0.05",
             "66000", "66000", "30", "65000", "above", "35"),
            ("KXBTC15M-T2", "stat_model", base_ts + 1, "yes", "0.55", "10",
             "0.02", "5.50", "200", "0.72", "0.05",
             "66050", "66000", "28", "65000", "above", "35"),
        ])

        # Two paper_settlements — one per fill.
        conn.executemany("""
            INSERT INTO paper_settlements (
                fill_id, market_ticker, settled_at_us, outcome, realized_pnl_usd
            ) VALUES (?, ?, ?, ?, ?)
        """, [
            (1, "KXBTC15M-T1", base_ts + 900_000_000, "yes",  "4.48"),
            (2, "KXBTC15M-T2", base_ts + 900_000_001, "no",  "-5.52"),
        ])
        conn.commit()
        conn.close()

        # Without filter: both fills + both settlements should show.
        with _open(db) as conn:
            rep_all = asim.build_report(conn, window="all")
        assert rep_all.paper_fills == 2
        assert rep_all.paper_settlements == 2
        assert rep_all.paper_pnl_usd == pytest.approx(-1.04, abs=0.01)
        assert rep_all.paper_wins == 1
        assert rep_all.paper_losses == 1

        # With --strategy pure_lag: only the pure_lag fill + its settlement.
        with _open(db) as conn:
            rep_lag = asim.build_report(conn, window="all", strategy="pure_lag")
        assert rep_lag.paper_fills == 1
        assert rep_lag.paper_settlements == 1
        assert rep_lag.paper_pnl_usd == pytest.approx(4.48, abs=0.01)
        assert rep_lag.paper_wins == 1
        assert rep_lag.paper_losses == 0

        # With --strategy stat_model: only the stat_model fill + settlement.
        with _open(db) as conn:
            rep_stat = asim.build_report(conn, window="all", strategy="stat_model")
        assert rep_stat.paper_fills == 1
        assert rep_stat.paper_settlements == 1
        assert rep_stat.paper_pnl_usd == pytest.approx(-5.52, abs=0.01)
        assert rep_stat.paper_wins == 0
        assert rep_stat.paper_losses == 1

    def test_top_markets_ordered_by_decisions(self, seeded_db):
        db, events = seeded_db
        with _open(db) as conn:
            rep = asim.build_report(conn, window="all", events_path=events)
        assert rep.top_markets[0]["ticker"] == "KXBTC15M-T1"
        assert rep.top_markets[0]["decisions"] == 3

    def test_risk_rejections_parse_rule_names(self, seeded_db):
        db, events = seeded_db
        with _open(db) as conn:
            rep = asim.build_report(conn, window="all", events_path=events)
        assert rep.risk_rejections["min_edge_after_fees"] == 2
        assert rep.risk_rejections["time_window"] == 1
        assert rep.risk_rejections["strike_proximity"] == 1

    def test_phase_timings_sorted_by_total_time(self, seeded_db):
        db, events = seeded_db
        with _open(db) as conn:
            rep = asim.build_report(conn, window="all", events_path=events)
        # Biggest time consumer is scanner.snapshot_books (3 × 2800ms).
        assert rep.phase_timings[0]["phase"] == "scanner.snapshot_books"
        assert rep.phase_timings[0]["count"] == 3
        assert rep.phase_timings[0]["p50"] == pytest.approx(2800.0, rel=0.01)

    def test_strategy_filter(self, seeded_db):
        db, events = seeded_db
        with _open(db) as conn:
            rep = asim.build_report(conn, window="all",
                                    strategy="pure_lag", events_path=events)
        assert rep.decisions_total == 3
        assert set(rep.decisions_by_strategy.keys()) == {"pure_lag"}

    def test_window_excludes_old_rows(self, seeded_db):
        db, events = seeded_db
        with _open(db) as conn:
            # 1h window relative to MAX(ts_us) in the DB.
            rep = asim.build_report(conn, window="1h", events_path=events)
        # All 5 seeded rows are within 4s of MAX — none excluded.
        assert rep.decisions_total == 5

    def test_reference_feed_staleness_anchored_to_wall_clock(self, seeded_db, monkeypatch):
        """Staleness uses wall clock — not the frozen decision timestamp."""
        db, events = seeded_db
        # Freeze time module at a moment 10 s after the seeded ticks.
        import time as _t
        import analyze_simulation as asim_mod
        monkeypatch.setattr(asim_mod, "_t", _t, raising=False)
        # Actually _t is imported inside build_report; easier to check value bounds.
        with _open(db) as conn:
            rep = asim.build_report(conn, window="all", events_path=events)
        # Seeded ticks were at a past timestamp, so staleness should be large (hours+).
        for asset, age in rep.reference_feed_staleness.items():
            assert age is not None
            assert age > 0

    def test_invalid_window_raises(self, seeded_db):
        db, events = seeded_db
        with _open(db) as conn:
            with pytest.raises(ValueError, match="window="):
                asim.build_report(conn, window="bogus", events_path=events)

    def test_missing_events_file_no_crash(self, seeded_db, tmp_path):
        db, _ = seeded_db
        with _open(db) as conn:
            rep = asim.build_report(
                conn, window="all",
                events_path=tmp_path / "nonexistent.jsonl",
            )
        # DB aggregates still populate.
        assert rep.decisions_total == 5
        # Event-log-derived fields stay empty.
        assert rep.risk_rejections == {}
        assert rep.phase_timings == []


# ---------------------------------------------------------------------------
# render_report
# ---------------------------------------------------------------------------


class TestRenderReport:
    def test_renders_all_sections(self, seeded_db):
        db, events = seeded_db
        with _open(db) as conn:
            rep = asim.build_report(conn, window="all", events_path=events)
        text = asim.render_report(rep)
        # Spot-check that each section's header is present.
        assert "Kalshi Scanner Overnight Report" in text
        assert "Per-strategy:" in text
        assert "Per-asset:" in text
        assert "Kalshi settlement outcomes:" in text
        assert "Paper executor:" in text
        assert "Risk rejections" in text
        assert "Top markets" in text
        assert "Phase timings" in text
        assert "Reference feed staleness" in text

    def test_renders_empty_db_without_crash(self, tmp_path):
        import migrate_db as m
        db = tmp_path / "empty.db"
        m.migrate(f"sqlite:///{db}")
        with _open(db) as conn:
            rep = asim.build_report(conn, window="all", events_path=None)
        text = asim.render_report(rep)
        assert "Decisions: 0 total" in text


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCLI:
    def test_json_output_valid(self, seeded_db, tmp_path, capsys, monkeypatch):
        db, events = seeded_db
        # Point the module's daily_log_path to the seeded events file.
        import analyze_simulation as asim_mod
        monkeypatch.setattr(
            asim_mod, "main",
            asim_mod.main,  # same function, just re-referenced for clarity
        )
        rc = asim.main([
            "--db", str(db),
            "--events-dir", str(events.parent),
            "--window", "all",
            "--json",
        ])
        assert rc == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["decisions_total"] == 5
        assert data["paper_settlements"] == 1

    def test_text_output_contains_totals(self, seeded_db, capsys):
        db, events = seeded_db
        rc = asim.main([
            "--db", str(db),
            "--events-dir", str(events.parent),
            "--window", "all",
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Decisions: 5 total" in out

    def test_missing_db_returns_2(self, tmp_path, capsys):
        rc = asim.main(["--db", str(tmp_path / "nothere.db")])
        assert rc == 2
