"""Coverage for `scripts/kalshi_collect_research.py`."""

from __future__ import annotations

import importlib
import json
import sqlite3

import pytest

import migrate_db as m


@pytest.fixture(scope="module")
def cr():
    return importlib.import_module("kalshi_collect_research")


@pytest.fixture
def seeded_db(tmp_path):
    url = f"sqlite:///{tmp_path}/collect.db"
    m.migrate(url)
    return url


def test_collect_analysis_summary_counts(cr, seeded_db):
    conn = sqlite3.connect(seeded_db.removeprefix("sqlite:///"))
    conn.row_factory = sqlite3.Row
    conn.execute(
        "INSERT INTO kalshi_lag_candidates "
        "(series_ticker, category, title, source_type, source_agency, source_url, "
        " publish_schedule_utc, ltt_to_expiry_s, strategy_hypothesis, lag_priority_score, "
        " priority_band, notes, raw_json, built_ts) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "CPIYOY",
            "Economics",
            "CPI",
            "scheduled_release",
            "BLS",
            "https://www.bls.gov",
            "13:30 UTC monthly BLS release",
            300,
            "scheduled_release_lag",
            90,
            "high",
            "base=scheduled_release",
            "{}",
            1,
        ),
    )
    conn.execute(
        "INSERT INTO kalshi_lag_candidates "
        "(series_ticker, category, title, source_type, source_agency, source_url, "
        " publish_schedule_utc, ltt_to_expiry_s, strategy_hypothesis, lag_priority_score, "
        " priority_band, notes, raw_json, built_ts) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "NBATOTAL",
            "Sports",
            "NBA Total",
            "event_driven_scored",
            "STATSCORE / official scoring",
            "https://www.statscore.com",
            "Event-driven / non-fixed",
            0,
            "score_update_lag",
            18,
            "low",
            "sports penalty",
            "{}",
            1,
        ),
    )
    conn.commit()

    summary = cr.collect_analysis_summary(conn, False, top_n=5)
    conn.close()

    assert summary["candidate_count"] == 2
    assert summary["high_priority_count"] == 1
    assert summary["category_counts"]["Economics"] == 1
    assert summary["source_type_counts"]["scheduled_release"] == 1
    assert summary["top_candidates"][0]["series_ticker"] == "CPIYOY"


def test_main_orchestrates_and_writes_outputs(cr, seeded_db, tmp_path, monkeypatch):
    calls: list[tuple[str, list[str]]] = []

    def fake_series_main(argv):
        calls.append(("series", list(argv)))
        conn = sqlite3.connect(seeded_db.removeprefix("sqlite:///"))
        conn.execute(
            "INSERT OR REPLACE INTO kalshi_series "
            "(series_ticker, category, title, frequency, contract_terms_url, raw_json, fetched_ts) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                "CPIYOY",
                "Economics",
                "Will CPI inflation come in above 3.0%?",
                "monthly",
                "https://kalshi-public-docs.s3.amazonaws.com/contract_terms/CPI.pdf",
                json.dumps({"ticker": "CPIYOY"}),
                1,
            ),
        )
        conn.commit()
        conn.close()
        return 0

    def fake_contract_main(argv):
        calls.append(("contracts", list(argv)))
        conn = sqlite3.connect(seeded_db.removeprefix("sqlite:///"))
        conn.execute(
            "INSERT OR REPLACE INTO kalshi_contract_terms "
            "(pdf_url, series_ticker_guess, local_path, bytes, sha256, fetched_ts) "
            "VALUES (?,?,?,?,?,?)",
            (
                "https://kalshi-public-docs.s3.amazonaws.com/contract_terms/CPI.pdf",
                "CPI",
                str(tmp_path / "CPI.pdf"),
                123,
                "abc",
                2,
            ),
        )
        conn.commit()
        conn.close()
        return 0

    def fake_registry_main(argv):
        calls.append(("registry", list(argv)))
        conn = sqlite3.connect(seeded_db.removeprefix("sqlite:///"))
        conn.execute(
            "INSERT OR REPLACE INTO kalshi_lag_candidates "
            "(series_ticker, category, title, source_type, source_agency, source_url, "
            " publish_schedule_utc, ltt_to_expiry_s, strategy_hypothesis, lag_priority_score, "
            " priority_band, notes, raw_json, built_ts) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "CPIYOY",
                "Economics",
                "CPI",
                "scheduled_release",
                "BLS",
                "https://www.bls.gov",
                "13:30 UTC monthly BLS release",
                300,
                "scheduled_release_lag",
                90,
                "high",
                "base=scheduled_release",
                "{}",
                3,
            ),
        )
        conn.commit()
        conn.close()
        return 0

    monkeypatch.setattr(cr.kalshi_series_discover, "main", fake_series_main)
    monkeypatch.setattr(cr.kalshi_contract_terms_pull, "main", fake_contract_main)
    monkeypatch.setattr(cr.kalshi_registry_build, "main", fake_registry_main)

    output_md = tmp_path / "summary.md"
    output_json = tmp_path / "summary.json"
    rc = cr.main(
        [
            "--database-url",
            seeded_db,
            "--analysis-output-markdown",
            str(output_md),
            "--analysis-output-json",
            str(output_json),
            "--research-date",
            "2026-04-21",
        ]
    )
    assert rc == 0
    assert [name for name, _ in calls] == ["series", "contracts", "registry"]
    assert "CPIYOY" in output_md.read_text()
    payload = json.loads(output_json.read_text())
    assert payload["candidate_count"] == 1
    assert payload["top_candidates"][0]["series_ticker"] == "CPIYOY"
