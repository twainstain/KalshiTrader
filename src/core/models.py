"""Core Kalshi domain models.

Shapes follow `docs/kalshi_scanner_execution_plan.md` §2 verbatim — any drift
must be matched there. `MarketQuote` is the Kalshi book snapshot as emitted
by `KalshiMarketSource`; `Opportunity` is produced by `KalshiFairValueStrategy`
(P1 populates `p_yes` / `ci_width` / `hypothetical_*` fields — no trading
path); `ExecutionResult` + `OpportunityStatus` track the lifecycle that P2
will exercise.

Every financial field is Decimal. `_coerce_decimals` auto-converts int/float
values passed by tests or replay fixtures via `Decimal(str(v))` (the `str`
intermediate avoids IEEE-754 noise). Non-financial fields — strings, bools,
timestamps, raw dicts — are listed in `_NON_DECIMAL_FIELDS` and skipped.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from decimal import Decimal
from enum import Enum


D = Decimal
ZERO = D("0")
ONE = D("1")
BPS_DIVISOR = D("10000")


SUPPORTED_VENUES: tuple[str, ...] = ("kalshi",)
SUPPORTED_COMPARATORS: tuple[str, ...] = (
    "above", "below", "between", "exactly", "at_least",
)


class OpportunityStatus(str, Enum):
    DETECTED = "detected"
    PRICED = "priced"
    APPROVED = "approved"
    REJECTED = "rejected"
    SIMULATION_APPROVED = "simulation_approved"
    SIMULATED = "simulated"
    SIMULATION_FAILED = "simulation_failed"
    SUBMITTED = "submitted"
    INCLUDED = "included"
    REVERTED = "reverted"
    NOT_INCLUDED = "not_included"
    DRY_RUN = "dry_run"


_NON_DECIMAL_FIELDS = frozenset({
    "venue", "market_ticker", "series_ticker", "event_ticker",
    "comparator", "warning_flags", "raw",
    "fee_included", "quote_timestamp_us",
    "recommended_side", "status", "quote",
    "success", "reason", "opportunity",
})


def _coerce_decimals(instance: object) -> None:
    for f in fields(instance):  # type: ignore[arg-type]
        if f.name in _NON_DECIMAL_FIELDS:
            continue
        val = getattr(instance, f.name)
        if isinstance(val, bool):
            continue
        if isinstance(val, (int, float)):
            object.__setattr__(instance, f.name, D(str(val)))


@dataclass(frozen=True)
class MarketQuote:
    """Kalshi book snapshot. See execution plan §2.1."""
    venue: str
    market_ticker: str
    series_ticker: str
    event_ticker: str
    best_yes_ask: Decimal
    best_no_ask: Decimal
    best_yes_bid: Decimal
    best_no_bid: Decimal
    book_depth_yes_usd: Decimal
    book_depth_no_usd: Decimal
    fee_bps: Decimal
    expiration_ts: Decimal
    strike: Decimal
    comparator: str
    reference_price: Decimal
    reference_60s_avg: Decimal
    time_remaining_s: Decimal
    quote_timestamp_us: int
    fee_included: bool = False
    warning_flags: tuple[str, ...] = ()
    raw: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.venue not in SUPPORTED_VENUES:
            raise ValueError(
                f"venue={self.venue!r} not in SUPPORTED_VENUES={SUPPORTED_VENUES}"
            )
        if self.fee_included:
            raise ValueError(
                "fee_included must be False for Kalshi quotes — fees are charged "
                "on top at fill (execution plan §1)."
            )
        if self.comparator not in SUPPORTED_COMPARATORS:
            raise ValueError(
                f"comparator={self.comparator!r} not in {SUPPORTED_COMPARATORS}"
            )
        _coerce_decimals(self)


@dataclass(frozen=True)
class Opportunity:
    """Output of `KalshiFairValueStrategy`. Skeleton per execution plan §2.2."""
    quote: MarketQuote
    p_yes: Decimal
    ci_width: Decimal
    recommended_side: str
    hypothetical_fill_price: Decimal
    hypothetical_size_contracts: Decimal
    expected_edge_bps_after_fees: Decimal
    status: OpportunityStatus = OpportunityStatus.DETECTED
    no_data_haircut_bps: Decimal = ZERO

    def __post_init__(self) -> None:
        if self.recommended_side not in ("yes", "no", "none"):
            raise ValueError(
                f"recommended_side={self.recommended_side!r} must be yes|no|none"
            )
        _coerce_decimals(self)


@dataclass(frozen=True)
class ExecutionResult:
    success: bool
    reason: str
    realized_pnl_usd: Decimal
    opportunity: Opportunity

    def __post_init__(self) -> None:
        _coerce_decimals(self)
