"""Shared helpers for `kalshi_paper_executor` + `kalshi_live_executor`.

Private (underscore prefix) — not for import outside the `execution`
package. Holds pure functions that both executors need but that don't
belong to either one conceptually.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal


def utc_day_bucket(us: int) -> str:
    """YYYY-MM-DD key for the daily-loss accumulator.

    Both executors bucket realized P/L per UTC day so `DailyLossRule` can
    read a single day's total regardless of how long the process has
    been running.
    """
    dt = datetime.fromtimestamp(us / 1_000_000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d")


def fees_for(fill_price: Decimal, size: Decimal, fee_bps: Decimal) -> Decimal:
    """Fees in dollars on contract notional.

    Convention: `MarketQuote.fee_bps` is expressed against the trade
    notional (fill price × size), not the payout. Paper pre-computes at
    fill time so settlement P/L can subtract it; live uses the same
    formula as an estimate, then overwrites with the value Kalshi returns
    on the fill.
    """
    notional = fill_price * size
    return notional * fee_bps / Decimal("10000")


def binary_payoff(outcome: str, side: str) -> Decimal:
    """$1 if the bought side wins, $0 otherwise.

    `no_data` resolves to NO per `CRYPTO15M.pdf` §0.5 — missing data at
    expiry short-circuits the payout to No-side. Both executors use this
    identical rule.
    """
    effective = outcome if outcome != "no_data" else "no"
    won = (side == "yes" and effective == "yes") or \
          (side == "no" and effective == "no")
    return Decimal("1") if won else Decimal("0")
