"""Calibrate σ_15min per asset from Kalshi `expiration_value` chains.

Pulls `(close_ts, expiration_value)` from `kalshi_historical_markets` per
series, builds consecutive log-return series, and prints per-asset σ along
with its annualized equivalent. Use the output to update
`DEFAULT_SIGMA_15MIN` in `src/strategy/kalshi_fair_value.py`.

Usage:
    python3.11 scripts/calibrate_sigma.py
    python3.11 scripts/calibrate_sigma.py --asset btc
    python3.11 scripts/calibrate_sigma.py --emit-python

The `--emit-python` flag prints a copy-paste snippet suitable for the
`DEFAULT_SIGMA_15MIN` constant.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sqlite3
import statistics
import sys
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse


logger = logging.getLogger(__name__)


ASSETS = {"KXBTC15M": "btc", "KXETH15M": "eth", "KXSOL15M": "sol"}
# 15-min intervals per calendar year. Kalshi runs 24/7 so no trading-day
# adjustment — 365 * 24 * 4 = 35,040.
INTERVALS_PER_YEAR = 365 * 24 * 4


def open_connection(url: str) -> sqlite3.Connection:
    parsed = urlparse(url)
    if parsed.scheme in ("sqlite", ""):
        raw = parsed.path or url.removeprefix("sqlite://")
        if raw.startswith("//"):
            path = Path(raw[1:])
        elif raw.startswith("/"):
            path = Path(raw.lstrip("/"))
        else:
            path = Path(raw)
        return sqlite3.connect(str(path))
    raise ValueError(
        "calibrate_sigma.py supports sqlite URLs only — point at a fresh DB "
        "instead of postgres for now."
    )


def returns_for(conn: sqlite3.Connection, series: str) -> list[float]:
    """Consecutive log-returns from settled expiration_value rows."""
    rows = conn.execute(
        "SELECT expiration_value FROM kalshi_historical_markets "
        "WHERE series_ticker = ? AND expiration_value IS NOT NULL "
        "  AND expiration_value != '' "
        "ORDER BY close_ts",
        (series,),
    ).fetchall()
    vals = [float(r[0]) for r in rows if r[0] and float(r[0]) > 0]
    rets: list[float] = []
    for i in range(1, len(vals)):
        if vals[i - 1] > 0 and vals[i] > 0:
            rets.append(math.log(vals[i] / vals[i - 1]))
    return rets


def robust_sigma(rets: list[float]) -> tuple[float, float, int]:
    """Return (population_stdev, MAD-scaled stdev, N) for comparison.

    MAD-scaled σ ≈ 1.4826 · median(|r - median|). Less sensitive to tail
    outliers than the raw population stdev.
    """
    if len(rets) < 2:
        return 0.0, 0.0, len(rets)
    stdev = statistics.pstdev(rets)
    med = statistics.median(rets)
    mad = statistics.median(abs(r - med) for r in rets)
    return stdev, 1.4826 * mad, len(rets)


def annualized_pct(sigma_15min: float) -> float:
    """Crude annualization — σ · √intervals_per_year."""
    return sigma_15min * math.sqrt(INTERVALS_PER_YEAR)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Calibrate σ_15min from expiration_value chain.")
    parser.add_argument("--asset", default="all", choices=("btc", "eth", "sol", "all"))
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--emit-python", action="store_true",
                        help="Print a copy-paste DEFAULT_SIGMA_15MIN snippet.")
    parser.add_argument("--emit-json", action="store_true",
                        help="Print the sigma map as JSON.")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    url = (args.database_url or os.environ.get("DATABASE_URL")
           or "sqlite:///data/kalshi.db")
    conn = open_connection(url)

    target_series = ASSETS if args.asset == "all" else {
        {"btc": "KXBTC15M", "eth": "KXETH15M", "sol": "KXSOL15M"}[args.asset]:
            args.asset,
    }

    sigma_by_asset: dict[str, float] = {}
    try:
        print("Series      N returns  σ_15min (stdev)   σ_15min (MAD·1.4826)  Annualized (stdev)")
        print("-" * 88)
        for series, asset in target_series.items():
            rets = returns_for(conn, series)
            sigma, sigma_mad, n = robust_sigma(rets)
            sigma_by_asset[asset] = sigma
            ann = annualized_pct(sigma)
            print(f"{series:<10} {n:>9}  {sigma*100:>15.4f}%  {sigma_mad*100:>19.4f}%   {ann*100:>14.2f}%")
    finally:
        conn.close()

    if args.emit_python:
        print("\n# Paste this into src/strategy/kalshi_fair_value.py → DEFAULT_SIGMA_15MIN")
        print("DEFAULT_SIGMA_15MIN: dict[str, Decimal] = {")
        for a, s in sigma_by_asset.items():
            print(f'    "{a}": Decimal("{s:.5f}"),')
        print("}")

    if args.emit_json:
        print()
        print(json.dumps({a: f"{s:.6f}" for a, s in sigma_by_asset.items()}, indent=2))

    return 0


if __name__ == "__main__":
    sys.exit(main())
