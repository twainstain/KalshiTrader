"""Coverage for `scripts/kalshi_series_discover.py`."""

from __future__ import annotations

import importlib
import sqlite3

import pytest

import migrate_db as m


@pytest.fixture(scope="module")
def sd():
    return importlib.import_module("kalshi_series_discover")


@pytest.fixture
def seeded_db(tmp_path):
    url = f"sqlite:///{tmp_path}/series.db"
    m.migrate(url)
    return url, sqlite3.connect(url.removeprefix("sqlite:///"))


def test_normalize_series_row_extracts_nested_contract_terms_url(sd):
    row = sd.normalize_series_row(
        {
            "ticker": "FEDDECISION",
            "category": "Economics",
            "title": "Fed Decision",
            "frequency": "scheduled",
            "contract_terms": {
                "url": "https://kalshi-public-docs.s3.amazonaws.com/contract_terms/FEDDECISION.pdf"
            },
        }
    )
    assert row["series_ticker"] == "FEDDECISION"
    assert row["contract_terms_url"].endswith("FEDDECISION.pdf")


def test_paginate_series_follows_cursor(sd):
    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload
            self.content = b"x"

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class FakeSession:
        def __init__(self):
            self.calls = []

        def get(self, url, params=None, timeout=None):
            self.calls.append((url, dict(params or {})))
            if params and params.get("cursor") == "next-page":
                return FakeResponse({"series": [{"ticker": "B"}], "cursor": ""})
            return FakeResponse({"series": [{"ticker": "A"}], "cursor": "next-page"})

    session = FakeSession()
    rows = list(sd.paginate_series(session=session, limit=5, pause_s=0.0))
    assert [row["ticker"] for row in rows] == ["A", "B"]
    assert session.calls[1][1]["cursor"] == "next-page"


def test_upsert_series_roundtrip(sd, seeded_db):
    _, conn = seeded_db
    row = {
        "series_ticker": "FEDDECISION",
        "category": "Economics",
        "title": "Fed Decision",
        "frequency": "scheduled",
        "contract_terms_url": "https://kalshi-public-docs.s3.amazonaws.com/contract_terms/FEDDECISION.pdf",
        "raw_json": "{}",
    }
    sd.upsert_series(conn, False, row, fetched_ts=123)
    conn.commit()
    out = conn.execute(
        "SELECT series_ticker, category, title, frequency FROM kalshi_series"
    ).fetchone()
    assert out == ("FEDDECISION", "Economics", "Fed Decision", "scheduled")
