"""Paper-mode Kalshi executor (P2-M1-T11).

Virtual fills at `opp.hypothetical_fill_price` — no orders are sent to
Kalshi; zero $ at risk. DB-backed via `paper_fills` / `paper_settlements`
when a `conn` is passed. Default mode for the scanner.

Lifecycle:
  1. `submit(opp, ctx)` — RiskEngine gate (if configured), then records
     a `PaperFill`. Returns an `ExecutionResult` carrying the fill or
     the first rejection reason.
  2. `reconcile(ticker, outcome)` — marks every open fill for that
     ticker as settled, computes realized P/L per Kalshi's $1-per-contract
     binary payout, bumps the daily-P/L accumulator.

Read-only snapshots (`open_positions()`, `daily_realized_pnl()`,
`notional_by_strike()`) feed `RiskContext` so the pipeline can rebuild
the rule-input state each tick without the executor mutating it.

The live variant is in `kalshi_live_executor.py` — same public surface
but talks to the REST API. Splitting them into sibling modules keeps the
paper-default clear at file-level (importing this file alone carries no
live-trading risk).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable

from core.models import (
    ExecutionResult,
    Opportunity,
    ZERO,
)
from risk.kalshi_rules import RiskContext, RiskEngine

from execution._executor_common import (
    binary_payoff,
    fees_for,
    utc_day_bucket,
)
from observability.timing import timed_phase


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PaperFill:
    """A hypothetical fill recorded by the paper executor.

    `fill_id` is populated after persistence to a DB (matches
    `paper_fills.id`). When the executor is running without a DB it
    stays `None` — settlements then use in-memory object identity.
    """
    opportunity: Opportunity
    filled_at_us: int
    fill_price: Decimal
    size_contracts: Decimal
    fees_paid_usd: Decimal
    fill_id: int | None = None


@dataclass(frozen=True)
class PaperSettlement:
    """Outcome + realized P/L for a previously filled paper position."""
    fill: PaperFill
    outcome: str                 # "yes" | "no" | "no_data"
    realized_pnl_usd: Decimal
    settled_at_us: int


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------


_INSERT_FILL_SQLITE = """
INSERT INTO paper_fills (
    market_ticker, strategy_label, filled_at_us,
    side, fill_price, size_contracts, fees_paid_usd, notional_usd,
    expected_edge_bps_after_fees, p_yes, ci_width,
    reference_price, reference_60s_avg, time_remaining_s,
    strike, comparator, fee_bps_at_decision
)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_INSERT_SETTLEMENT_SQLITE = """
INSERT INTO paper_settlements (
    fill_id, market_ticker, settled_at_us, outcome, realized_pnl_usd
)
VALUES (?, ?, ?, ?, ?)
"""


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


class KalshiPaperExecutor:
    """Paper-mode executor: virtual fills + settlement.

    Persistence is optional. Pass `conn` + `is_postgres` to write every
    fill to `paper_fills` and every settlement to `paper_settlements`.
    With no `conn`, state stays in-memory (handy for unit tests and
    short-lived simulations).
    """

    def __init__(
        self,
        *,
        risk_engine: RiskEngine | None = None,
        now_us: Callable[[], int] | None = None,
        conn: object | None = None,
        is_postgres: bool = False,
        strategy_label: str = "",
        event_logger: Any = None,
    ) -> None:
        self._risk_engine = risk_engine
        self._now_us = now_us or (lambda: int(time.time() * 1_000_000))
        self._conn = conn
        self._is_postgres = is_postgres
        self._strategy_label = strategy_label
        self._event_logger = event_logger
        # Keyed by market_ticker → list of open PaperFills (multiple per
        # ticker is possible if the scanner fires repeatedly in the same
        # 15-min window and OpenPositionsRule hasn't capped it).
        self._open_fills: dict[str, list[PaperFill]] = {}
        self._notional_by_strike: dict[str, Decimal] = {}
        self._daily_pnl: dict[str, Decimal] = {}
        self._settlements: list[PaperSettlement] = []

    # ---- read-only snapshots (feed RiskContext) ----

    def open_positions(self) -> int:
        return sum(len(fills) for fills in self._open_fills.values())

    def daily_realized_pnl(self, now_us: int | None = None) -> Decimal:
        bucket = utc_day_bucket(now_us if now_us is not None else self._now_us())
        return self._daily_pnl.get(bucket, ZERO)

    def notional_by_strike(self) -> dict[str, Decimal]:
        return dict(self._notional_by_strike)

    def settlements(self) -> tuple[PaperSettlement, ...]:
        return tuple(self._settlements)

    # ---- lifecycle ----

    def submit(
        self, opp: Opportunity, ctx: RiskContext | None = None,
    ) -> ExecutionResult:
        with timed_phase(self._event_logger, "paper_executor.submit",
                         strategy=self._strategy_label,
                         ticker=opp.quote.market_ticker):
            return self._submit_impl(opp, ctx)

    def _submit_impl(
        self, opp: Opportunity, ctx: RiskContext | None,
    ) -> ExecutionResult:
        if opp.recommended_side == "none":
            return ExecutionResult(
                success=False,
                reason="opportunity has no recommended side",
                realized_pnl_usd=ZERO, opportunity=opp,
            )

        if self._risk_engine is not None:
            if ctx is None:
                raise ValueError(
                    "RiskEngine is configured but no RiskContext was passed"
                )
            with timed_phase(self._event_logger, "paper_executor.risk_check",
                             strategy=self._strategy_label):
                decision = self._risk_engine.decide(opp, ctx)
            if not decision.approved:
                reasons = "; ".join(
                    f"{v.rule_name}: {v.reason}" for v in decision.rejections
                )
                return ExecutionResult(
                    success=False, reason=f"risk-rejected: {reasons}",
                    realized_pnl_usd=ZERO, opportunity=opp,
                )

        now = self._now_us()
        fees = fees_for(
            opp.hypothetical_fill_price,
            opp.hypothetical_size_contracts,
            opp.quote.fee_bps,
        )
        fill_id = self._persist_fill(opp, now, fees)
        fill = PaperFill(
            opportunity=opp,
            filled_at_us=now,
            fill_price=opp.hypothetical_fill_price,
            size_contracts=opp.hypothetical_size_contracts,
            fees_paid_usd=fees,
            fill_id=fill_id,
        )
        ticker = opp.quote.market_ticker
        self._open_fills.setdefault(ticker, []).append(fill)
        notional = fill.fill_price * fill.size_contracts
        self._notional_by_strike[ticker] = (
            self._notional_by_strike.get(ticker, ZERO) + notional
        )
        logger.debug(
            "paper fill %s side=%s price=%s size=%s fees=%s id=%s",
            ticker, opp.recommended_side, fill.fill_price,
            fill.size_contracts, fees, fill_id,
        )
        return ExecutionResult(
            success=True, reason="paper-filled",
            realized_pnl_usd=ZERO, opportunity=opp,
        )

    def reconcile(self, ticker: str, outcome: str) -> list[PaperSettlement]:
        if outcome.lower() not in ("yes", "no", "no_data"):
            raise ValueError(f"outcome={outcome!r} must be yes|no|no_data")
        fills = self._open_fills.pop(ticker, [])
        if not fills:
            return []
        outcome = outcome.lower()
        now = self._now_us()
        settled: list[PaperSettlement] = []
        for fill in fills:
            side = fill.opportunity.recommended_side
            payoff = binary_payoff(outcome, side)
            gross = (payoff - fill.fill_price) * fill.size_contracts
            realized = gross - fill.fees_paid_usd
            s = PaperSettlement(
                fill=fill, outcome=outcome,
                realized_pnl_usd=realized, settled_at_us=now,
            )
            self._persist_settlement(s)
            settled.append(s)
            self._settlements.append(s)
            bucket = utc_day_bucket(now)
            self._daily_pnl[bucket] = self._daily_pnl.get(bucket, ZERO) + realized
        self._notional_by_strike.pop(ticker, None)
        logger.debug("paper settle %s outcome=%s count=%d", ticker, outcome, len(settled))
        return settled

    # ---- persistence ----

    def _persist_fill(
        self, opp: Opportunity, now_us: int, fees: Decimal,
    ) -> int | None:
        if self._conn is None:
            return None
        q = opp.quote
        row = (
            q.market_ticker,
            self._strategy_label,
            now_us,
            opp.recommended_side,
            str(opp.hypothetical_fill_price),
            str(opp.hypothetical_size_contracts),
            str(fees),
            str(opp.hypothetical_fill_price * opp.hypothetical_size_contracts),
            str(opp.expected_edge_bps_after_fees),
            str(opp.p_yes),
            str(opp.ci_width),
            str(q.reference_price),
            str(q.reference_60s_avg),
            str(q.time_remaining_s),
            str(q.strike),
            q.comparator,
            str(q.fee_bps),
        )
        stmt = _INSERT_FILL_SQLITE
        if self._is_postgres:
            stmt = stmt.replace("?", "%s") + " RETURNING id"
            with self._conn.cursor() as cur:
                cur.execute(stmt, row)
                fill_id = cur.fetchone()[0]
            self._conn.commit()
            return int(fill_id)
        cur = self._conn.execute(stmt, row)
        self._conn.commit()
        return int(cur.lastrowid)

    def _persist_settlement(self, s: PaperSettlement) -> None:
        if self._conn is None or s.fill.fill_id is None:
            return
        row = (
            s.fill.fill_id,
            s.fill.opportunity.quote.market_ticker,
            s.settled_at_us,
            s.outcome,
            str(s.realized_pnl_usd),
        )
        stmt = _INSERT_SETTLEMENT_SQLITE
        if self._is_postgres:
            stmt = stmt.replace("?", "%s")
            with self._conn.cursor() as cur:
                cur.execute(stmt, row)
        else:
            self._conn.execute(stmt, row)
        self._conn.commit()
