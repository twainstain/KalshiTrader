"""Coverage for `scripts/kalshi_registry_build.py`."""

from __future__ import annotations

import importlib
import json
import sqlite3

import pytest

import migrate_db as m


@pytest.fixture(scope="module")
def rb():
    return importlib.import_module("kalshi_registry_build")


@pytest.fixture
def seeded_db(tmp_path):
    url = f"sqlite:///{tmp_path}/registry.db"
    m.migrate(url)
    conn = sqlite3.connect(url.removeprefix("sqlite:///"))
    conn.execute(
        "INSERT INTO kalshi_series "
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
    conn.execute(
        "INSERT INTO kalshi_contract_terms "
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
    return url


def test_main_builds_registry_outputs_and_db_rows(rb, seeded_db, tmp_path):
    output_json = tmp_path / "registry.json"
    output_md = tmp_path / "ranking.md"
    rc = rb.main(
        [
            "--database-url",
            seeded_db,
            "--output-json",
            str(output_json),
            "--output-markdown",
            str(output_md),
            "--research-date",
            "2026-04-21",
        ]
    )
    assert rc == 0
    payload = json.loads(output_json.read_text())
    assert payload["CPIYOY"]["source_type"] == "scheduled_release"
    assert "Kalshi Lag Opportunity Ranking" in output_md.read_text()

    conn = sqlite3.connect(seeded_db.removeprefix("sqlite:///"))
    try:
        row = conn.execute(
            "SELECT source_type, source_agency, priority_band FROM kalshi_lag_candidates "
            "WHERE series_ticker='CPIYOY'"
        ).fetchone()
    finally:
        conn.close()
    assert row == ("scheduled_release", "BLS", "high")
