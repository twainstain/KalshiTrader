"""Live-mode Kalshi executor (P2-M2-T01).

Submits real Kalshi orders via `KalshiAPIClient`. **Only instantiated
when the three-opt-in `LiveGateConfig` passes** — paper remains the
codebase default and this module is the sole place where capital goes
at risk.

Layered guard-rails (every one must pass before POST /portfolio/orders):
  1. `LiveGateConfig.is_live_approved` — `--execute` flag AND
     `KALSHI_API_KEY_ID` populated AND config `mode: "live"` AND
     `dry_run: false`. Construction raises `RuntimeError` otherwise.
  2. `RiskEngine.decide()` — 10 rules (see `risk/kalshi_rules.py`).
  3. `CircuitBreaker.allows_execution()` — cross-request error budget.
  4. `RetryPolicy` — idempotency-key-safe retries for transient 5xx.

Lifecycle:
  1. `submit(opp, ctx)` — gate → RiskEngine → breaker → generate
     idempotency key → `POST /portfolio/orders`. On success, record a
     `resting` `LiveOrder` and persist to `live_orders`.
  2. `poll_pending()` — called by the run loop each tick:
       (a) any resting order older than `cancel_timeout_s` gets
           `DELETE /portfolio/orders/{id}`.
       (b) remaining resting orders hit `GET /portfolio/fills`; on
           fill, update DB + record breaker success.
  3. `reconcile(ticker, outcome)` — `GET /portfolio/settlements`,
     compute local P/L against the fill record, flag discrepancies in
     the DB and log.

The paper variant is in `kalshi_paper_executor.py` — same public surface
but virtual fills. Splitting them keeps the risk surface clear at
file-level: importing `kalshi_paper_executor` cannot accidentally route
to live.
"""

from __future__ import annotations

import logging
import time
import uuid
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
    utc_day_bucket,
)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LiveGateConfig:
    """Three-opt-in gate wired at construction.

    Every field must align for `is_live_approved` to be true, and the
    caller builds this explicitly from argv / env / config — no default
    can silently authorize live trading.
    """
    execute_flag: bool
    api_key_id_present: bool
    config_mode_live: bool
    dry_run: bool

    @property
    def is_live_approved(self) -> bool:
        """All three opt-ins aligned AND dry_run must be False."""
        return (
            self.execute_flag
            and self.api_key_id_present
            and self.config_mode_live
            and not self.dry_run
        )


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------


_INSERT_LIVE_ORDER_SQLITE = """
INSERT INTO live_orders (
    order_id, client_order_id, market_ticker, strategy_label,
    submitted_at_us, side, price, size_contracts, status,
    expected_edge_bps_after_fees, p_yes,
    reference_price, strike, comparator
)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_UPDATE_LIVE_ORDER_FILLED_SQLITE = """
UPDATE live_orders
   SET status = ?, filled_at_us = ?, fill_price = ?,
       fill_quantity = ?, fees_paid_usd = ?,
       order_id = COALESCE(NULLIF(?, ''), order_id)
 WHERE id = ?
"""

_UPDATE_LIVE_ORDER_CANCELED_SQLITE = """
UPDATE live_orders
   SET status = ?, canceled_at_us = ?, cancel_reason = ?
 WHERE id = ?
"""

_INSERT_LIVE_SETTLEMENT_SQLITE = """
INSERT INTO live_settlements (
    order_row_id, market_ticker, settled_at_us, outcome,
    computed_pnl_usd, kalshi_reported_pnl_usd, discrepancy_usd
)
VALUES (?, ?, ?, ?, ?, ?, ?)
"""


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class LiveOrder:
    """In-memory mirror of a row in `live_orders`.

    Mutable on purpose — fills / cancels update the same object so the
    executor's `submit() → poll_pending()` cycle can track state without
    round-tripping through the DB.
    """
    opportunity: Opportunity
    client_order_id: str
    submitted_at_us: int
    side: str
    price: Decimal
    size_contracts: int
    status: str                  # "resting" | "filled" | "canceled" | "failed"
    order_id: str = ""           # populated by Kalshi after create
    fill_price: Decimal | None = None
    fill_quantity: int | None = None
    fees_paid_usd: Decimal | None = None
    filled_at_us: int | None = None
    canceled_at_us: int | None = None
    cancel_reason: str = ""
    db_row_id: int | None = None


@dataclass(frozen=True)
class LiveSettlement:
    order: LiveOrder
    outcome: str
    computed_pnl_usd: Decimal
    kalshi_reported_pnl_usd: Decimal | None
    discrepancy_usd: Decimal | None
    settled_at_us: int


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


class KalshiLiveExecutor:
    """Submit real Kalshi orders.

    This class is the ONLY place where capital goes at risk. The code
    path is protected at four layers (gate → risk → breaker → retry)
    and the default scanner never instantiates it — the paper executor
    is the default.
    """

    def __init__(
        self,
        *,
        rest_client: Any,
        gate: LiveGateConfig,
        risk_engine: RiskEngine | None = None,
        circuit_breaker: Any = None,              # platform_adapters.CircuitBreaker
        retry_policy: Any = None,                 # platform_adapters.RetryPolicy
        cancel_timeout_s: float = 3.0,
        now_us: Callable[[], int] | None = None,
        conn: object | None = None,
        is_postgres: bool = False,
        strategy_label: str = "",
        flags_poller: Any = None,
        asset_by_ticker: dict[str, str] | None = None,
    ) -> None:
        if not gate.is_live_approved:
            raise RuntimeError(
                "KalshiLiveExecutor refused: three-opt-in gate not satisfied. "
                f"execute_flag={gate.execute_flag}, "
                f"api_key_id_present={gate.api_key_id_present}, "
                f"config_mode_live={gate.config_mode_live}, "
                f"dry_run={gate.dry_run}"
            )
        self._rest = rest_client
        self._gate = gate
        self._risk_engine = risk_engine
        self._breaker = circuit_breaker
        self._retry = retry_policy
        self._cancel_timeout_us = int(cancel_timeout_s * 1_000_000)
        self._now_us = now_us or (lambda: int(time.time() * 1_000_000))
        self._conn = conn
        self._is_postgres = is_postgres
        self._strategy_label = strategy_label
        # Optional runtime-flags poller for per-asset execution toggling.
        # Evaluated on every submit; None means always-allow.
        self._flags_poller = flags_poller
        self._asset_by_ticker = asset_by_ticker or {}
        self._resting: dict[str, LiveOrder] = {}
        self._filled_by_ticker: dict[str, list[LiveOrder]] = {}
        self._notional_by_strike: dict[str, Decimal] = {}
        self._daily_pnl: dict[str, Decimal] = {}
        self._settlements: list[LiveSettlement] = []

    # ---- read-only snapshots (feed RiskContext) ----

    def open_positions(self) -> int:
        """Resting + filled-but-unsettled are both "open" for risk sizing."""
        return len(self._resting) + sum(
            len(v) for v in self._filled_by_ticker.values()
        )

    def daily_realized_pnl(self, now_us: int | None = None) -> Decimal:
        bucket = utc_day_bucket(now_us if now_us is not None else self._now_us())
        return self._daily_pnl.get(bucket, ZERO)

    def notional_by_strike(self) -> dict[str, Decimal]:
        return dict(self._notional_by_strike)

    def settlements(self) -> tuple[LiveSettlement, ...]:
        return tuple(self._settlements)

    def resting_orders(self) -> tuple[LiveOrder, ...]:
        return tuple(self._resting.values())

    # ---- submit ----

    def submit(
        self, opp: Opportunity, ctx: RiskContext | None = None,
    ) -> ExecutionResult:
        if opp.recommended_side == "none":
            return ExecutionResult(
                success=False, reason="opportunity has no recommended side",
                realized_pnl_usd=ZERO, opportunity=opp,
            )

        # Risk engine gate.
        if self._risk_engine is not None:
            if ctx is None:
                raise ValueError(
                    "RiskEngine is configured but no RiskContext was passed"
                )
            decision = self._risk_engine.decide(opp, ctx)
            if not decision.approved:
                reasons = "; ".join(
                    f"{v.rule_name}: {v.reason}" for v in decision.rejections
                )
                return ExecutionResult(
                    success=False, reason=f"risk-rejected: {reasons}",
                    realized_pnl_usd=ZERO, opportunity=opp,
                )

        # Per-asset execution flag gate (set via dashboard). Fails fast so
        # neither the risk engine nor the REST call is exercised when the
        # asset is dashboarded-off or the kill-switch is engaged.
        if self._flags_poller is not None:
            asset = self._asset_by_ticker.get(opp.quote.market_ticker, "")
            flags = self._flags_poller.get()
            if asset and not flags.is_asset_execution_enabled(asset):
                reason = (
                    "kill-switch engaged"
                    if flags.execution_kill_switch
                    else f"execution disabled for asset {asset!r}"
                )
                return ExecutionResult(
                    success=False,
                    reason=f"flag-rejected: {reason}",
                    realized_pnl_usd=ZERO, opportunity=opp,
                )

        # Circuit breaker gate.
        if self._breaker is not None:
            allowed, trip_reason = self._breaker.allows_execution()
            if not allowed:
                return ExecutionResult(
                    success=False,
                    reason=f"circuit-breaker-open: {trip_reason}",
                    realized_pnl_usd=ZERO, opportunity=opp,
                )

        now = self._now_us()
        client_order_id = self._make_client_order_id(opp, now)
        side = opp.recommended_side
        price_cents = int(
            (opp.hypothetical_fill_price * Decimal("100")).to_integral_value()
        )
        size = int(opp.hypothetical_size_contracts)

        order_kwargs: dict[str, Any] = dict(
            ticker=opp.quote.market_ticker,
            action="buy",
            side=side,
            count=size,
            client_order_id=client_order_id,
            order_type="limit",
        )
        if side == "yes":
            order_kwargs["yes_price"] = price_cents
        else:
            order_kwargs["no_price"] = price_cents

        try:
            resp = self._call_with_retry(
                lambda: self._rest.create_order(**order_kwargs)
            )
        except Exception as e:  # noqa: BLE001
            if self._breaker is not None:
                self._breaker.record_api_error()
            logger.warning("create_order failed: %s", e)
            return ExecutionResult(
                success=False, reason=f"order-create-failed: {e}",
                realized_pnl_usd=ZERO, opportunity=opp,
            )

        order_id = self._extract_order_id(resp)
        order = LiveOrder(
            opportunity=opp,
            client_order_id=client_order_id,
            submitted_at_us=now,
            side=side,
            price=opp.hypothetical_fill_price,
            size_contracts=size,
            status="resting",
            order_id=order_id,
        )
        self._resting[client_order_id] = order
        notional = opp.hypothetical_fill_price * Decimal(size)
        ticker = opp.quote.market_ticker
        self._notional_by_strike[ticker] = (
            self._notional_by_strike.get(ticker, ZERO) + notional
        )
        order.db_row_id = self._persist_order(order)
        if self._breaker is not None:
            self._breaker.record_success()
        logger.info(
            "live order submitted ticker=%s side=%s price=%s size=%s id=%s",
            ticker, side, order.price, size, order_id,
        )
        return ExecutionResult(
            success=True, reason="live-submitted",
            realized_pnl_usd=ZERO, opportunity=opp,
        )

    # ---- poll (fill detection + cancel-on-timeout) ----

    def poll_pending(self) -> dict[str, int]:
        if not self._resting:
            return {"canceled": 0, "filled": 0}
        now = self._now_us()
        canceled = 0
        filled = 0

        for cid in list(self._resting.keys()):
            order = self._resting[cid]
            age_us = now - order.submitted_at_us
            if age_us >= self._cancel_timeout_us:
                if self._cancel_order(order, reason="timeout"):
                    canceled += 1

        if self._resting:
            try:
                fills_resp = self._call_with_retry(
                    lambda: self._rest.get_fills(limit=100)
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("get_fills failed: %s", e)
                fills_resp = None
            if isinstance(fills_resp, dict):
                for f in fills_resp.get("fills", []) or []:
                    if self._match_and_apply_fill(f, now):
                        filled += 1

        return {"canceled": canceled, "filled": filled}

    # ---- reconcile (settlement + discrepancy detection) ----

    def reconcile(self, ticker: str, outcome: str) -> list[LiveSettlement]:
        if outcome.lower() not in ("yes", "no", "no_data"):
            raise ValueError(f"outcome={outcome!r} must be yes|no|no_data")
        filled = self._filled_by_ticker.pop(ticker, [])
        if not filled:
            return []
        outcome = outcome.lower()
        now = self._now_us()

        # Pull Kalshi's view of settlements for discrepancy check.
        kalshi_report: dict[str, Decimal] = {}
        try:
            resp = self._call_with_retry(
                lambda: self._rest.get_settlements(ticker=ticker, limit=100)
            )
            for s in (resp or {}).get("settlements", []) or []:
                ord_id = s.get("order_id") or s.get("trade_id") or ""
                if not ord_id:
                    continue
                pnl = s.get("realized_pnl")
                if pnl is not None:
                    # Kalshi reports cents; internal P/L is dollars.
                    kalshi_report[ord_id] = Decimal(str(pnl)) / Decimal("100")
        except Exception as e:  # noqa: BLE001
            logger.warning("get_settlements failed: %s", e)

        settled: list[LiveSettlement] = []
        for order in filled:
            side = order.side
            payoff = binary_payoff(outcome, side)
            fill_px = order.fill_price or order.price
            size = Decimal(order.fill_quantity or order.size_contracts)
            fees = order.fees_paid_usd or ZERO
            gross = (payoff - fill_px) * size
            computed = gross - fees
            reported = kalshi_report.get(order.order_id)
            diff = None if reported is None else (computed - reported)
            if diff is not None and abs(diff) > Decimal("0.01"):
                logger.warning(
                    "settlement discrepancy ticker=%s order=%s computed=%s "
                    "reported=%s diff=%s",
                    ticker, order.order_id, computed, reported, diff,
                )
            s = LiveSettlement(
                order=order, outcome=outcome, computed_pnl_usd=computed,
                kalshi_reported_pnl_usd=reported, discrepancy_usd=diff,
                settled_at_us=now,
            )
            self._persist_settlement(s)
            settled.append(s)
            self._settlements.append(s)
            bucket = utc_day_bucket(now)
            self._daily_pnl[bucket] = self._daily_pnl.get(bucket, ZERO) + computed

        self._notional_by_strike.pop(ticker, None)
        logger.info(
            "live settle ticker=%s outcome=%s count=%d",
            ticker, outcome, len(settled),
        )
        return settled

    # ---- internal helpers ----

    def _make_client_order_id(self, opp: Opportunity, now_us: int) -> str:
        """Idempotency key — stable across retries within a single submit.

        Kalshi requires uniqueness across all of the account's history; we
        embed a UUID4 plus the ticker to keep them short and readable.
        """
        return f"kt-{opp.quote.market_ticker}-{now_us}-{uuid.uuid4().hex[:8]}"

    @staticmethod
    def _extract_order_id(resp: Any) -> str:
        if isinstance(resp, dict):
            o = resp.get("order")
            if isinstance(o, dict):
                return str(o.get("order_id") or o.get("id") or "")
            return str(resp.get("order_id") or resp.get("id") or "")
        return ""

    def _call_with_retry(self, fn: Callable[[], Any]) -> Any:
        if self._retry is None:
            return fn()
        from platform_adapters import execute_with_retry  # deferred
        result = execute_with_retry(fn, self._retry)
        if not result.success:
            raise result.error if result.error else RuntimeError("retry failed")
        return result.value

    def _cancel_order(self, order: LiveOrder, *, reason: str) -> bool:
        try:
            self._call_with_retry(
                lambda: self._rest.cancel_order(order.order_id)
                if order.order_id else {}
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("cancel %s failed: %s", order.order_id, e)
            return False
        now = self._now_us()
        order.status = "canceled"
        order.canceled_at_us = now
        order.cancel_reason = reason
        self._resting.pop(order.client_order_id, None)
        ticker = order.opportunity.quote.market_ticker
        notional = order.price * Decimal(order.size_contracts)
        remaining = self._notional_by_strike.get(ticker, ZERO) - notional
        if remaining <= ZERO:
            self._notional_by_strike.pop(ticker, None)
        else:
            self._notional_by_strike[ticker] = remaining
        self._persist_cancel(order)
        return True

    def _match_and_apply_fill(self, f: dict, now_us: int) -> bool:
        client_id = f.get("client_order_id") or ""
        order_id = f.get("order_id") or ""
        order: LiveOrder | None = None
        if client_id and client_id in self._resting:
            order = self._resting[client_id]
        elif order_id:
            for o in self._resting.values():
                if o.order_id == order_id:
                    order = o
                    break
        if order is None:
            return False
        fill_px_cents = f.get("yes_price") if order.side == "yes" else f.get("no_price")
        fill_qty = f.get("count") or order.size_contracts
        fees_cents = f.get("fees") or 0
        order.status = "filled"
        order.filled_at_us = now_us
        order.fill_price = (
            Decimal(str(fill_px_cents)) / Decimal("100")
            if fill_px_cents is not None else order.price
        )
        order.fill_quantity = int(fill_qty)
        order.fees_paid_usd = Decimal(str(fees_cents)) / Decimal("100")
        self._resting.pop(order.client_order_id, None)
        ticker = order.opportunity.quote.market_ticker
        self._filled_by_ticker.setdefault(ticker, []).append(order)
        self._persist_fill_update(order)
        return True

    # ---- persistence ----

    def _persist_order(self, order: LiveOrder) -> int | None:
        if self._conn is None:
            return None
        q = order.opportunity.quote
        row = (
            order.order_id,
            order.client_order_id,
            q.market_ticker,
            self._strategy_label,
            order.submitted_at_us,
            order.side,
            str(order.price),
            order.size_contracts,
            order.status,
            str(order.opportunity.expected_edge_bps_after_fees),
            str(order.opportunity.p_yes),
            str(q.reference_price),
            str(q.strike),
            q.comparator,
        )
        stmt = _INSERT_LIVE_ORDER_SQLITE
        if self._is_postgres:
            stmt = stmt.replace("?", "%s") + " RETURNING id"
            with self._conn.cursor() as cur:
                cur.execute(stmt, row)
                rid = cur.fetchone()[0]
            self._conn.commit()
            return int(rid)
        cur = self._conn.execute(stmt, row)
        self._conn.commit()
        return int(cur.lastrowid)

    def _persist_fill_update(self, order: LiveOrder) -> None:
        if self._conn is None or order.db_row_id is None:
            return
        row = (
            order.status,
            order.filled_at_us,
            str(order.fill_price) if order.fill_price is not None else None,
            order.fill_quantity,
            str(order.fees_paid_usd) if order.fees_paid_usd is not None else None,
            order.order_id or "",
            order.db_row_id,
        )
        stmt = _UPDATE_LIVE_ORDER_FILLED_SQLITE
        if self._is_postgres:
            stmt = stmt.replace("?", "%s")
            with self._conn.cursor() as cur:
                cur.execute(stmt, row)
        else:
            self._conn.execute(stmt, row)
        self._conn.commit()

    def _persist_cancel(self, order: LiveOrder) -> None:
        if self._conn is None or order.db_row_id is None:
            return
        row = (
            order.status,
            order.canceled_at_us,
            order.cancel_reason,
            order.db_row_id,
        )
        stmt = _UPDATE_LIVE_ORDER_CANCELED_SQLITE
        if self._is_postgres:
            stmt = stmt.replace("?", "%s")
            with self._conn.cursor() as cur:
                cur.execute(stmt, row)
        else:
            self._conn.execute(stmt, row)
        self._conn.commit()

    def _persist_settlement(self, s: LiveSettlement) -> None:
        if self._conn is None or s.order.db_row_id is None:
            return
        row = (
            s.order.db_row_id,
            s.order.opportunity.quote.market_ticker,
            s.settled_at_us,
            s.outcome,
            str(s.computed_pnl_usd),
            str(s.kalshi_reported_pnl_usd) if s.kalshi_reported_pnl_usd is not None else None,
            str(s.discrepancy_usd) if s.discrepancy_usd is not None else None,
        )
        stmt = _INSERT_LIVE_SETTLEMENT_SQLITE
        if self._is_postgres:
            stmt = stmt.replace("?", "%s")
            with self._conn.cursor() as cur:
                cur.execute(stmt, row)
        else:
            self._conn.execute(stmt, row)
        self._conn.commit()
