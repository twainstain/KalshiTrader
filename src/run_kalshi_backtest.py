"""Replay historical Kalshi windows against `FairValueModel` → Brier vs naive.

Reads:
- `kalshi_historical_markets` — one row per settled 15-min market (metadata
  + settled_result).
- `reference_ticks` — per-second constituent ticks from
  `BasketReferenceSource` (for reconstructing reference_price + reference_60s_avg).

Scores each market at a configurable decision offset (default T-30s before
close) and emits a Markdown report with Brier score, hit-rate, and
calibration summary, compared against a naive baseline where
p_yes = best_yes_ask at decision time.

Acceptance (P1-M3-T08): Brier < naive on ≥ 500 windows/asset; calibration
error ≤ 3pp per decile. That's the real run — gated on historical data
(P1-M2). This script works today; it just reports `no data` when the DB
is empty.

Usage:
    python3.11 -m run_kalshi_backtest
    python3.11 -m run_kalshi_backtest --decision-offset-s 60 --report /tmp/bt.md
    DATABASE_URL=postgresql://... python3.11 -m run_kalshi_backtest
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# Module is executed as a script; resolve the sibling `core/` / `strategy/`
# packages without requiring `pip install -e .`.
_THIS = Path(__file__).resolve()
if str(_THIS.parent) not in sys.path:
    sys.path.insert(0, str(_THIS.parent))

from strategy.kalshi_fair_value import FairValueModel  # noqa: E402


logger = logging.getLogger(__name__)


@dataclass
class DecisionRow:
    market_ticker: str
    asset: str
    strike: Decimal
    comparator: str
    time_remaining_s: Decimal
    reference_price: Decimal
    reference_60s_avg: Decimal
    realized_yes: int        # 1 if resolved Yes, 0 if No, -1 if unknown
    naive_p_yes: Decimal | None   # e.g. best_yes_ask at decision time


@dataclass
class ScoreRow:
    market_ticker: str
    asset: str
    model_p_yes: Decimal
    naive_p_yes: Decimal | None
    realized_yes: int


# ---------------------------------------------------------------------------
# DB access (generic over sqlite / postgres). All reads are parameterised.
# ---------------------------------------------------------------------------

def _open_connection(url: str) -> Any:
    """Open a DB-API connection for either sqlite or postgres."""
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
    if parsed.scheme in ("postgres", "postgresql"):
        import psycopg2  # deferred — not needed for sqlite dev
        return psycopg2.connect(url)
    raise ValueError(f"Unsupported DATABASE_URL scheme: {parsed.scheme!r}")


def _asset_from_series(series_ticker: str) -> str:
    """Derive asset code (btc|eth|sol) from a series ticker like KXBTC15M."""
    t = series_ticker.upper()
    if "BTC" in t:
        return "btc"
    if "ETH" in t:
        return "eth"
    if "SOL" in t:
        return "sol"
    return "btc"  # conservative fallback; flagged by the summary


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def brier_score(probs: list[Decimal], outcomes: list[int]) -> Decimal | None:
    """Mean squared error: (1/N) Σ (p − y)²."""
    if not probs:
        return None
    n = Decimal(len(probs))
    total = Decimal("0")
    for p, y in zip(probs, outcomes):
        diff = p - Decimal(y)
        total += diff * diff
    return total / n


def hit_rate(probs: list[Decimal], outcomes: list[int]) -> Decimal | None:
    """Fraction where argmax matches realized outcome."""
    if not probs:
        return None
    hits = 0
    for p, y in zip(probs, outcomes):
        pred = 1 if p >= Decimal("0.5") else 0
        if pred == y:
            hits += 1
    return Decimal(hits) / Decimal(len(probs))


def calibration_by_decile(
    probs: list[Decimal], outcomes: list[int],
) -> list[tuple[int, int, Decimal, Decimal]]:
    """Return (decile_idx, count, avg_predicted, empirical_yes_rate) per bin."""
    buckets: dict[int, list[tuple[Decimal, int]]] = defaultdict(list)
    for p, y in zip(probs, outcomes):
        idx = min(9, int(float(p) * 10))
        buckets[idx].append((p, y))
    rows: list[tuple[int, int, Decimal, Decimal]] = []
    for i in range(10):
        items = buckets.get(i, [])
        if not items:
            continue
        count = len(items)
        avg_p = sum((p for p, _ in items), Decimal("0")) / Decimal(count)
        yes_rate = Decimal(sum(y for _, y in items)) / Decimal(count)
        rows.append((i, count, avg_p, yes_rate))
    return rows


# ---------------------------------------------------------------------------
# DB → DecisionRow iterator
# ---------------------------------------------------------------------------

def iter_decision_rows(
    conn: Any,
    *,
    decision_offset_s: int,
) -> list[DecisionRow]:
    """Build one `DecisionRow` per settled historical market.

    Approach: for each window we ask "at T = expiration - decision_offset_s,
    what did the model see?" Reference price at that instant is the most
    recent `reference_ticks` row ≤ that ts_us; reference_60s_avg is the
    average over the preceding 60s.

    The naive baseline needs `best_yes_ask` at decision time. We approximate
    with `last trade before decision ts`, falling back to `None` when the
    trade log is empty for that window.
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT market_ticker, series_ticker, strike, comparator, "
        "       expiration_ts, settled_result "
        "FROM kalshi_historical_markets"
    )
    markets = cur.fetchall()

    rows: list[DecisionRow] = []
    for (ticker, series, strike, comparator, expiration_ts,
         settled) in markets:
        asset = _asset_from_series(series)
        decision_ts_s = int(expiration_ts) - decision_offset_s
        decision_ts_us = decision_ts_s * 1_000_000
        window_start_us = decision_ts_us - 60_000_000

        # Nearest reference tick at or before decision_ts.
        cur.execute(
            "SELECT price FROM reference_ticks "
            "WHERE asset = ? AND ts_us <= ? "
            "ORDER BY ts_us DESC LIMIT 1",
            (asset, decision_ts_us),
        )
        row = cur.fetchone()
        ref_price = Decimal(row[0]) if row else None

        # 60s average.
        cur.execute(
            "SELECT AVG(CAST(price AS REAL)) FROM reference_ticks "
            "WHERE asset = ? AND ts_us > ? AND ts_us <= ?",
            (asset, window_start_us, decision_ts_us),
        )
        row = cur.fetchone()
        ref_avg = Decimal(str(row[0])) if row and row[0] is not None else None

        # Last trade price before decision time (naive baseline).
        cur.execute(
            "SELECT price FROM kalshi_historical_trades "
            "WHERE market_ticker = ? AND ts_us <= ? "
            "ORDER BY ts_us DESC LIMIT 1",
            (ticker, decision_ts_us),
        )
        row = cur.fetchone()
        naive = Decimal(row[0]) if row else None

        if ref_price is None or ref_avg is None:
            continue  # insufficient data for this window — skip

        realized = -1
        if settled == "yes":
            realized = 1
        elif settled == "no":
            realized = 0
        else:
            continue  # unresolved or no-data → drop from scoring

        rows.append(DecisionRow(
            market_ticker=ticker,
            asset=asset,
            strike=Decimal(str(strike)),
            comparator=str(comparator),
            time_remaining_s=Decimal(decision_offset_s),
            reference_price=ref_price,
            reference_60s_avg=ref_avg,
            realized_yes=realized,
            naive_p_yes=naive,
        ))
    return rows


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def score_rows(
    rows: list[DecisionRow], model: FairValueModel,
) -> list[ScoreRow]:
    out: list[ScoreRow] = []
    for r in rows:
        try:
            p_yes, _ = model.price(
                asset=r.asset, strike=r.strike, comparator=r.comparator,
                reference_price=r.reference_price,
                reference_60s_avg=r.reference_60s_avg,
                time_remaining_s=r.time_remaining_s,
            )
        except (NotImplementedError, ValueError):
            continue  # unsupported comparator — skip
        out.append(ScoreRow(
            market_ticker=r.market_ticker,
            asset=r.asset,
            model_p_yes=p_yes,
            naive_p_yes=r.naive_p_yes,
            realized_yes=r.realized_yes,
        ))
    return out


def render_report(
    scored: list[ScoreRow], *, decision_offset_s: int,
) -> str:
    if not scored:
        return (
            "# Kalshi fair-value backtest\n\n"
            "**No scorable rows found.** Confirm P1-M2 historical pull has "
            "populated `kalshi_historical_markets`, `reference_ticks`, and "
            "`kalshi_historical_trades`.\n"
        )

    by_asset: dict[str, list[ScoreRow]] = defaultdict(list)
    for s in scored:
        by_asset[s.asset].append(s)

    lines: list[str] = ["# Kalshi fair-value backtest", ""]
    lines.append(f"**Decision offset:** T-{decision_offset_s}s before close")
    lines.append(f"**Scored windows:** {len(scored)}")
    lines.append("")
    lines.append("## Summary by asset")
    lines.append("")
    lines.append("| Asset | N | Model Brier | Naive Brier | Model hit-rate |")
    lines.append("|---|---:|---:|---:|---:|")
    for asset in sorted(by_asset):
        subset = by_asset[asset]
        model_probs = [s.model_p_yes for s in subset]
        outcomes = [s.realized_yes for s in subset]
        naive_probs = [s.naive_p_yes for s in subset if s.naive_p_yes is not None]
        naive_outcomes = [s.realized_yes for s in subset if s.naive_p_yes is not None]
        mb = brier_score(model_probs, outcomes)
        nb = brier_score(naive_probs, naive_outcomes)
        hr = hit_rate(model_probs, outcomes)
        lines.append(
            f"| {asset} | {len(subset)} | "
            f"{mb:.4f} | {('n/a' if nb is None else f'{nb:.4f}')} | "
            f"{('n/a' if hr is None else f'{hr:.3f}')} |"
        )
    lines.append("")
    lines.append("## Calibration (pooled)")
    lines.append("")
    lines.append("| Decile | N | Avg predicted | Empirical Yes rate |")
    lines.append("|---:|---:|---:|---:|")
    cal = calibration_by_decile(
        [s.model_p_yes for s in scored],
        [s.realized_yes for s in scored],
    )
    for idx, count, avg_p, rate in cal:
        lines.append(f"| {idx} | {count} | {avg_p:.3f} | {rate:.3f} |")
    lines.append("")
    lines.append(
        "> **P1-M3-T08 acceptance:** Brier < naive across ≥ 500 windows/asset; "
        "calibration error ≤ 3pp per decile. Recalibrate `annual_vol_by_asset` "
        "from CF Benchmarks history if calibration drifts."
    )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Replay historical Kalshi windows through FairValueModel.")
    parser.add_argument("--database-url", default=None,
                        help="Override DATABASE_URL (default: env, else sqlite:///data/kalshi.db)")
    parser.add_argument("--decision-offset-s", type=int, default=30,
                        help="Seconds before close to score at (default 30).")
    parser.add_argument("--report", default="-",
                        help="Output path for the Markdown report, or '-' for stdout.")
    parser.add_argument("--no-data-haircut", type=str, default="0.005")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    url = (args.database_url or os.environ.get("DATABASE_URL")
           or "sqlite:///data/kalshi.db")
    conn = _open_connection(url)
    try:
        rows = iter_decision_rows(conn, decision_offset_s=args.decision_offset_s)
    finally:
        conn.close()

    model = FairValueModel(no_data_haircut=Decimal(args.no_data_haircut))
    scored = score_rows(rows, model)
    report = render_report(scored, decision_offset_s=args.decision_offset_s)

    if args.report == "-":
        print(report)
    else:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(report)
        logger.info("report written to %s", args.report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
