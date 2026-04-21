"""Coverage for `scripts/kalshi_contract_terms_pull.py`."""

from __future__ import annotations

import importlib

import pytest
import requests


@pytest.fixture(scope="module")
def ctp():
    return importlib.import_module("kalshi_contract_terms_pull")


def test_parse_s3_listing_extracts_keys_and_token(ctp):
    xml_text = """
    <ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
      <Contents><Key>contract_terms/CRYPTO15M.pdf</Key></Contents>
      <Contents><Key>contract_terms/FEDDECISION.pdf</Key></Contents>
      <NextContinuationToken>token-2</NextContinuationToken>
    </ListBucketResult>
    """
    keys, token = ctp.parse_s3_listing(xml_text)
    assert keys == [
        "contract_terms/CRYPTO15M.pdf",
        "contract_terms/FEDDECISION.pdf",
    ]
    assert token == "token-2"


def test_download_contract_term_uses_existing_file_without_http(ctp, tmp_path):
    dest_dir = tmp_path / "terms"
    dest_dir.mkdir()
    existing = dest_dir / "FEDDECISION.pdf"
    existing.write_bytes(b"pdf-bytes")

    class ExplodingSession:
        def get(self, *args, **kwargs):
            raise AssertionError("network should not be used when file already exists")

    path, num_bytes, sha256 = ctp.download_contract_term(
        "https://kalshi-public-docs.s3.amazonaws.com/contract_terms/FEDDECISION.pdf",
        session=ExplodingSession(),
        dest_dir=dest_dir,
    )
    assert path == existing
    assert num_bytes == 9
    assert len(sha256) == 64


def test_download_contract_term_retries_then_succeeds(ctp, tmp_path, monkeypatch):
    dest_dir = tmp_path / "terms"
    calls = {"count": 0}

    class Response:
        content = b"pdf-bytes"

        def raise_for_status(self):
            return None

    class FlakySession:
        def get(self, *args, **kwargs):
            calls["count"] += 1
            if calls["count"] < 3:
                raise requests.ReadTimeout("slow read")
            return Response()

    monkeypatch.setattr(ctp.time, "sleep", lambda *_args, **_kwargs: None)

    path, num_bytes, sha256 = ctp.download_contract_term(
        "https://kalshi-public-docs.s3.amazonaws.com/contract_terms/FEDDECISION.pdf",
        session=FlakySession(),
        dest_dir=dest_dir,
        max_attempts=3,
        retry_backoff_seconds=0.01,
    )

    assert path.name == "FEDDECISION.pdf"
    assert path.read_bytes() == b"pdf-bytes"
    assert num_bytes == 9
    assert len(sha256) == 64
    assert calls["count"] == 3


def test_main_skips_failed_download_and_commits_successes(ctp, tmp_path, monkeypatch):
    db_path = tmp_path / "research.db"
    conn, is_postgres = ctp._open_connection(f"sqlite:///{db_path}")
    try:
        conn.execute(
            "CREATE TABLE kalshi_contract_terms ("
            "pdf_url TEXT PRIMARY KEY, "
            "series_ticker_guess TEXT NOT NULL, "
            "local_path TEXT NOT NULL, "
            "bytes INTEGER NOT NULL, "
            "sha256 TEXT NOT NULL, "
            "fetched_ts BIGINT NOT NULL"
            ")"
        )
        conn.commit()
    finally:
        conn.close()
    assert is_postgres is False

    monkeypatch.setattr(
        ctp,
        "iter_contract_term_urls",
        lambda **_kwargs: iter(
            [
                "https://kalshi-public-docs.s3.amazonaws.com/contract_terms/BAD.pdf",
                "https://kalshi-public-docs.s3.amazonaws.com/contract_terms/GOOD.pdf",
            ]
        ),
    )

    def fake_download(url, **_kwargs):
        if url.endswith("BAD.pdf"):
            raise requests.ReadTimeout("slow read")
        out = tmp_path / "GOOD.pdf"
        out.write_bytes(b"ok")
        return out, 2, "a" * 64

    monkeypatch.setattr(ctp, "download_contract_term", fake_download)

    rc = ctp.main(
        [
            "--database-url",
            f"sqlite:///{db_path}",
            "--dest-dir",
            str(tmp_path / "terms"),
            "--max-attempts",
            "1",
            "--commit-every",
            "1",
        ]
    )

    assert rc == 0
    conn, _ = ctp._open_connection(f"sqlite:///{db_path}")
    try:
        row = conn.execute(
            "SELECT pdf_url, series_ticker_guess FROM kalshi_contract_terms ORDER BY pdf_url"
        ).fetchall()
    finally:
        conn.close()
    assert row == [
        (
            "https://kalshi-public-docs.s3.amazonaws.com/contract_terms/GOOD.pdf",
            "GOOD",
        )
    ]
