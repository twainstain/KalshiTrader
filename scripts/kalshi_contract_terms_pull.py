"""Inventory Kalshi contract-term PDFs from the public S3 bucket."""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

import requests


_THIS = Path(__file__).resolve()
_REPO = _THIS.parent.parent
for rel in ("src", "scripts"):
    p = _REPO / rel
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


logger = logging.getLogger(__name__)


BUCKET_BASE_URL = "https://kalshi-public-docs.s3.amazonaws.com"
LIST_URL = f"{BUCKET_BASE_URL}/?list-type=2&prefix=contract_terms/"
DEFAULT_LIST_TIMEOUT = (10.0, 30.0)
DEFAULT_FILE_TIMEOUT = (10.0, 60.0)


def _open_connection(url: str) -> tuple[Any, bool]:
    parsed = urlparse(url)
    if parsed.scheme in ("sqlite", ""):
        raw = parsed.path or url.removeprefix("sqlite://")
        if raw.startswith("//"):
            path = Path(raw[1:])
        elif raw.startswith("/"):
            path = Path(raw.lstrip("/"))
        else:
            path = Path(raw)
        path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(str(path)), False
    if parsed.scheme in ("postgres", "postgresql"):
        import psycopg2

        return psycopg2.connect(url), True
    raise ValueError(f"Unsupported DATABASE_URL scheme: {parsed.scheme!r}")


def parse_s3_listing(xml_text: str) -> tuple[list[str], str]:
    root = ET.fromstring(xml_text)
    keys: list[str] = []
    for elem in root.findall(".//{*}Contents/{*}Key"):
        if elem.text:
            keys.append(elem.text)
    token = root.findtext(".//{*}NextContinuationToken") or ""
    return keys, token


def _guess_series_ticker_from_url(url: str) -> str:
    stem = Path(urlparse(url).path).stem.upper()
    return "".join(ch for ch in stem if ch.isalnum())


def iter_contract_term_urls(
    *,
    session: requests.Session | None = None,
    max_pages: int = 1_000,
    max_attempts: int = 3,
    retry_backoff_seconds: float = 1.0,
) -> Iterable[str]:
    session = session or requests.Session()
    continuation_token = ""
    for _ in range(max_pages):
        params = {"list-type": "2", "prefix": "contract_terms/"}
        if continuation_token:
            params["continuation-token"] = continuation_token
        resp = None
        for attempt in range(1, max_attempts + 1):
            try:
                resp = session.get(BUCKET_BASE_URL + "/", params=params, timeout=DEFAULT_LIST_TIMEOUT)
                resp.raise_for_status()
                break
            except requests.RequestException:
                if attempt >= max_attempts:
                    raise
                logger.warning(
                    "retrying contract-term listing page after attempt %d/%d",
                    attempt,
                    max_attempts,
                    exc_info=True,
                )
                time.sleep(retry_backoff_seconds * attempt)
        assert resp is not None
        keys, continuation_token = parse_s3_listing(resp.text)
        for key in keys:
            if key.lower().endswith(".pdf"):
                yield f"{BUCKET_BASE_URL}/{key}"
        if not continuation_token:
            return


def download_contract_term(
    url: str,
    *,
    session: requests.Session | None = None,
    dest_dir: str | Path = "data/contract_terms",
    max_attempts: int = 3,
    retry_backoff_seconds: float = 1.0,
) -> tuple[Path, int, str]:
    session = session or requests.Session()
    dest_root = Path(dest_dir)
    dest_root.mkdir(parents=True, exist_ok=True)
    path = dest_root / Path(urlparse(url).path).name
    if path.is_file():
        data = path.read_bytes()
    else:
        resp = None
        for attempt in range(1, max_attempts + 1):
            try:
                resp = session.get(url, timeout=DEFAULT_FILE_TIMEOUT)
                resp.raise_for_status()
                break
            except requests.RequestException:
                if attempt >= max_attempts:
                    raise
                logger.warning(
                    "retrying contract term download for %s after attempt %d/%d",
                    url,
                    attempt,
                    max_attempts,
                    exc_info=True,
                )
                time.sleep(retry_backoff_seconds * attempt)
        assert resp is not None
        data = resp.content
        path.write_bytes(data)
    sha256 = hashlib.sha256(data).hexdigest()
    return path, len(data), sha256


def upsert_contract_term(
    conn: Any,
    is_postgres: bool,
    *,
    pdf_url: str,
    series_ticker_guess: str,
    local_path: str,
    num_bytes: int,
    sha256: str,
    fetched_ts: int,
) -> None:
    payload = (
        pdf_url,
        series_ticker_guess,
        local_path,
        num_bytes,
        sha256,
        fetched_ts,
    )
    sql = (
        "INSERT OR REPLACE INTO kalshi_contract_terms "
        "(pdf_url, series_ticker_guess, local_path, bytes, sha256, fetched_ts) "
        "VALUES (?, ?, ?, ?, ?, ?)"
    )
    if is_postgres:
        sql = (
            "INSERT INTO kalshi_contract_terms "
            "(pdf_url, series_ticker_guess, local_path, bytes, sha256, fetched_ts) "
            "VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (pdf_url) DO UPDATE SET "
            "series_ticker_guess = EXCLUDED.series_ticker_guess, "
            "local_path = EXCLUDED.local_path, "
            "bytes = EXCLUDED.bytes, "
            "sha256 = EXCLUDED.sha256, "
            "fetched_ts = EXCLUDED.fetched_ts"
        )
        with conn.cursor() as cur:
            cur.execute(sql, payload)
        return
    conn.execute(sql, payload)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pull Kalshi contract-term PDFs.")
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--dest-dir", default="data/contract_terms")
    parser.add_argument("--max-files", type=int, default=None)
    parser.add_argument("--max-pages", type=int, default=1_000)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--retry-backoff-seconds", type=float, default=1.0)
    parser.add_argument("--commit-every", type=int, default=25)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    db_url = args.database_url or os.environ.get("DATABASE_URL") or "sqlite:///data/kalshi.db"
    conn, is_postgres = (None, False) if args.dry_run else _open_connection(db_url)
    fetched_ts = int(time.time() * 1_000_000)
    count = 0
    failed_count = 0

    try:
        with requests.Session() as session:
            for url in iter_contract_term_urls(
                session=session,
                max_pages=args.max_pages,
                max_attempts=args.max_attempts,
                retry_backoff_seconds=args.retry_backoff_seconds,
            ):
                try:
                    path, num_bytes, sha256 = download_contract_term(
                        url,
                        session=session,
                        dest_dir=args.dest_dir,
                        max_attempts=args.max_attempts,
                        retry_backoff_seconds=args.retry_backoff_seconds,
                    )
                except requests.RequestException:
                    failed_count += 1
                    logger.error("skipping contract term after repeated download failures: %s", url, exc_info=True)
                    continue
                if conn is not None:
                    upsert_contract_term(
                        conn,
                        is_postgres,
                        pdf_url=url,
                        series_ticker_guess=_guess_series_ticker_from_url(url),
                        local_path=str(path),
                        num_bytes=num_bytes,
                        sha256=sha256,
                        fetched_ts=fetched_ts,
                    )
                    if count % max(args.commit_every, 1) == 0:
                        conn.commit()
                count += 1
                if args.max_files is not None and count >= args.max_files:
                    break
        if conn is not None and not is_postgres:
            conn.commit()
    finally:
        if conn is not None:
            conn.close()

    logger.info("done: %d contract terms processed, %d failed", count, failed_count)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
