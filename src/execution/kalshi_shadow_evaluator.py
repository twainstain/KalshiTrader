"""Phase-1 shadow evaluator.

Consumes live `MarketQuote`s from `KalshiMarketSource` plus spot / 60s-avg
references from `CryptoReferenceSource`, scores each via
`KalshiFairValueStrategy`, and records every hypothetical decision as a
row in `shadow_decisions`. **No Executor is wired** — the trading path is
structurally absent in Phase 1 per `kalshi_scanner_execution_plan.md` §1.

A post-window reconciler polls `/markets/{ticker}` at `expiration_ts + 30s`
and updates the realized columns (`realized_outcome`, `realized_pnl_usd`)
so the P1-M5 analysis notebooks can compute Brier + hit-rate + capacity
from real resolutions.

`KalshiShadowEvaluator` is engine-style, not callback-style: each
`tick()` invocation does one pass over (quotes → decisions → DB write →
reconciliation). `run_kalshi_shadow.py` calls `tick()` in a loop; tests
call it once with a fixture.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Iterable, Protocol

from core.models import (
    BPS_DIVISOR,
    MarketQuote,
    Opportunity,
    OpportunityStatus,
    ZERO,
)
from observability.timing import timed_phase
from strategy.kalshi_fair_value import (
    FairValueModel,
    KalshiFairValueStrategy,
    StrategyConfig,
)


logger = logging.getLogger(__name__)


# Duck-typed protocols — actual instances can be `KalshiMarketSource`,
# `BasketReferenceSource`, or test doubles. We don't import the real
# classes to keep the evaluator light and composable.
class _MarketSource(Protocol):
    def get_quotes(self, **kwargs) -> list[MarketQuote]: ...
    def is_healthy(self) -> bool: ...


class _ReferenceSource(Protocol):
    def get_spot(self, asset: str) -> Decimal | None: ...
    def get_60s_avg(self, asset: str) -> Decimal | None: ...
    # Optional — older reference sources may not expose this. The evaluator
    # `getattr`s it rather than requiring it so legacy doubles keep working.
    # Returns microsecond timestamp of the most recent tick, or None.
    # def get_last_tick_us(self, asset: str) -> int | None: ...


class _ResolutionLookup(Protocol):
    """Given a market ticker at/after expiration, return a settlement dict.

    Returning `None` means the market hasn't settled yet. The evaluator
    falls back gracefully and will retry on the next tick.
    """
    def __call__(self, ticker: str) -> dict | None: ...


@dataclass
class ShadowConfig:
    # Extra metadata the evaluator attaches to each row.
    fee_bps_default: Decimal = Decimal("35")
    # Seconds after expiration before we try to reconcile.
    reconcile_delay_s: int = 30
    # Maximum reconciler attempts per market before giving up.
    reconcile_max_attempts: int = 5


# ---------------------------------------------------------------------------
# SQL statements — sqlite-dialect, works on Postgres if `?` → `%s`.
# Using `?` placeholders keeps the single-engine default path simple; the
# postgres variant is wired below for symmetry with `crypto_reference.py`.
# ---------------------------------------------------------------------------

SQL_INSERT = """
INSERT INTO shadow_decisions (
    market_ticker, ts_us,
    p_yes, ci_width, reference_price, reference_60s_avg, time_remaining_s,
    best_yes_ask, best_no_ask, book_depth_yes_usd, book_depth_no_usd,
    recommended_side, hypothetical_fill_price, hypothetical_size_contracts,
    expected_edge_bps_after_fees, fee_bps_at_decision,
    latency_ms_ref_to_decision, latency_ms_book_to_decision,
    strategy_label
)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

SQL_UPDATE_REALIZED = """
UPDATE shadow_decisions
   SET realized_outcome = ?, realized_pnl_usd = ?
 WHERE market_ticker = ? AND ts_us = ?
"""

SQL_SELECT_UNRECONCILED = """
SELECT DISTINCT s.market_ticker, s.ts_us
  FROM shadow_decisions s
 WHERE s.realized_outcome IS NULL
"""


# ---------------------------------------------------------------------------
# Evaluator
# ---------------------------------------------------------------------------

@dataclass
class _PendingReconcile:
    """Track retry state for markets whose settlement hasn't landed yet."""
    expiration_ts: int
    attempts: int = 0


class KalshiShadowEvaluator:
    """Compose market + reference + strategy → shadow_decisions.

    The evaluator doesn't own any I/O (WS, REST) — it takes the sources as
    constructor args and advances one frame per `tick()`. This keeps it
    test-friendly (mock every input) and ops-friendly (wrap in a supervisor
    that also handles signals, logging, metrics).
    """

    def __init__(
        self,
        *,
        market_source: _MarketSource,
        reference_source: _ReferenceSource,
        strategy: KalshiFairValueStrategy | None = None,
        market_meta_by_ticker: dict[str, dict] | None = None,
        asset_by_ticker: dict[str, str] | None = None,
        fee_bps_by_ticker: dict[str, Decimal] | None = None,
        conn: Any = None,
        is_postgres: bool = False,
        resolution_lookup: _ResolutionLookup | None = None,
        config: ShadowConfig | None = None,
        now_us: Callable[[], int] | None = None,
        strategy_label: str = "stat_model",
        decision_hook: Callable[[MarketQuote, Opportunity], None] | None = None,
        reconcile_hook: Callable[[str, str], None] | None = None,
        event_logger: Any = None,   # `observability.event_log.EventLogger`-shaped
    ) -> None:
        self._market_source = market_source
        self._reference_source = reference_source
        self._strategy = strategy or KalshiFairValueStrategy(FairValueModel())
        self._strategy_label = strategy_label
        # Don't use `x or {}` — when caller passes an empty-but-shared dict
        # (as the run-loop coordinator does, populating it later), `or` would
        # dereference to a fresh empty dict and break the shared-mutation
        # contract. Only `is None` should substitute a new dict.
        self._market_meta = {} if market_meta_by_ticker is None else market_meta_by_ticker
        self._asset_by_ticker = {} if asset_by_ticker is None else asset_by_ticker
        self._fee_bps = {} if fee_bps_by_ticker is None else fee_bps_by_ticker
        self._conn = conn
        self._is_postgres = is_postgres
        self._resolve = resolution_lookup
        self._config = config or ShadowConfig()
        self._now_us = now_us or (lambda: int(time.time() * 1_000_000))
        self._pending_reconcile: dict[str, _PendingReconcile] = {}
        self._decision_hook = decision_hook
        self._reconcile_hook = reconcile_hook
        # EventLogger is duck-typed — tests pass a stub with a `record()` method.
        # `None` is fine; `_emit()` short-circuits in that case.
        self._event_logger = event_logger

    # ---- per-tick ----

    def tick(self) -> dict[str, int]:
        """One pass: score quotes, write decisions, attempt reconciliation."""
        with timed_phase(self._event_logger, "evaluator.tick",
                         strategy=self._strategy_label):
            return self._tick_impl()

    def _tick_impl(self) -> dict[str, int]:
        written = 0
        reconciled = 0

        with timed_phase(self._event_logger, "evaluator.snapshot_references"):
            spot_by_asset = self._snapshot_references("spot")
            avg_by_asset = self._snapshot_references("60s_avg")

        with timed_phase(self._event_logger, "evaluator.get_quotes"):
            quotes = self._market_source.get_quotes(
                reference_price_by_asset=spot_by_asset,
                reference_60s_avg_by_asset=avg_by_asset,
                fee_bps_by_ticker=self._effective_fee_bps(),
                market_meta_by_ticker=self._market_meta,
            )

        for q in quotes:
            asset = self._asset_by_ticker.get(q.market_ticker)
            if asset is None:
                continue
            with timed_phase(self._event_logger, "strategy.evaluate",
                             strategy=self._strategy_label, asset=asset):
                opp = self._strategy.evaluate(q, asset=asset)
            if opp is None:
                continue
            with timed_phase(self._event_logger, "evaluator.persist_decision",
                             strategy=self._strategy_label,
                             ticker=q.market_ticker):
                persisted = self._persist_decision(q, opp, asset)
            if persisted:
                written += 1
                self._emit(
                    "decision",
                    strategy_label=self._strategy_label,
                    asset=asset,
                    market_ticker=q.market_ticker,
                    side=opp.recommended_side,
                    fill_price=opp.hypothetical_fill_price,
                    size_contracts=opp.hypothetical_size_contracts,
                    edge_bps=opp.expected_edge_bps_after_fees,
                    time_remaining_s=q.time_remaining_s,
                    p_yes=opp.p_yes,
                    ci_width=opp.ci_width,
                )
            if self._decision_hook is not None:
                with timed_phase(self._event_logger, "evaluator.decision_hook",
                                 strategy=self._strategy_label,
                                 ticker=q.market_ticker):
                    try:
                        self._decision_hook(q, opp)
                    except Exception as e:  # noqa: BLE001 — don't let a hook crash the tick
                        logger.warning("decision_hook failed: %s", e)
            self._register_reconcile(q)

        if self._conn is not None and self._resolve is not None:
            with timed_phase(self._event_logger, "evaluator.reconcile_pending",
                             strategy=self._strategy_label):
                reconciled = self._reconcile_pending()

        return {"written": written, "reconciled": reconciled}

    # ---- helpers ----

    def _snapshot_references(self, kind: str) -> dict[str, Decimal]:
        """Cache spot / 60s_avg for each asset we know about this tick."""
        out: dict[str, Decimal] = {}
        seen: set[str] = set()
        for asset in self._asset_by_ticker.values():
            if asset in seen:
                continue
            seen.add(asset)
            if kind == "spot":
                val = self._reference_source.get_spot(asset)
            else:
                val = self._reference_source.get_60s_avg(asset)
            if val is not None:
                out[asset] = val
        return out

    def _effective_fee_bps(self) -> dict[str, Decimal]:
        """Fill missing fee_bps entries with the configured default."""
        if not self._market_meta:
            return dict(self._fee_bps)
        out = dict(self._fee_bps)
        for ticker in self._market_meta:
            out.setdefault(ticker, self._config.fee_bps_default)
        return out

    def _persist_decision(
        self, quote: MarketQuote, opp: Opportunity, asset: str,
    ) -> bool:
        if self._conn is None:
            return False
        now_us = self._now_us()
        # latency_ms_book_to_decision: age of the Kalshi book snapshot.
        book_to_dec_ms = (now_us - int(quote.quote_timestamp_us)) / 1000.0
        # latency_ms_ref_to_decision: age of the most recent reference tick
        # for this asset. `get_last_tick_us` is optional on the reference
        # protocol (see docstring); older doubles return None and the
        # column stays NULL. We require an int return type — MagicMock's
        # auto-attribute behavior returns a MagicMock which isinstance-fails
        # and cleanly short-circuits to None.
        ref_ts_us: int | None = None
        getter = getattr(self._reference_source, "get_last_tick_us", None)
        if callable(getter):
            try:
                raw = getter(asset)
                if isinstance(raw, int):
                    ref_ts_us = raw
            except Exception:  # noqa: BLE001 — never crash the tick on latency capture
                ref_ts_us = None
        ref_to_dec_ms = (
            (now_us - ref_ts_us) / 1000.0 if ref_ts_us is not None else None
        )
        row = (
            quote.market_ticker, now_us,
            str(opp.p_yes), str(opp.ci_width),
            str(quote.reference_price), str(quote.reference_60s_avg),
            str(quote.time_remaining_s),
            str(quote.best_yes_ask), str(quote.best_no_ask),
            str(quote.book_depth_yes_usd), str(quote.book_depth_no_usd),
            opp.recommended_side,
            str(opp.hypothetical_fill_price),
            str(opp.hypothetical_size_contracts),
            str(opp.expected_edge_bps_after_fees),
            str(quote.fee_bps),
            None if ref_to_dec_ms is None else f"{ref_to_dec_ms:.3f}",
            f"{book_to_dec_ms:.3f}",
            self._strategy_label,
        )
        stmt = SQL_INSERT
        if self._is_postgres:
            stmt = stmt.replace("?", "%s")
            with self._conn.cursor() as cur:
                cur.execute(stmt, row)
        else:
            self._conn.execute(stmt, row)
        self._conn.commit()
        return True

    def _register_reconcile(self, quote: MarketQuote) -> None:
        ticker = quote.market_ticker
        exp_s = int(quote.expiration_ts)
        if ticker not in self._pending_reconcile:
            self._pending_reconcile[ticker] = _PendingReconcile(expiration_ts=exp_s)

    def _emit(self, event_type: str, **fields: Any) -> None:
        """Fire-and-forget event record. Swallows errors."""
        if self._event_logger is None:
            return
        try:
            self._event_logger.record(event_type, **fields)
        except Exception as e:  # noqa: BLE001 — never crash the tick
            logger.warning("event_logger.record failed: %s", e)

    def _reconcile_pending(self) -> int:
        """Try to settle outstanding markets whose expiry + delay has passed."""
        if not self._pending_reconcile or self._conn is None or self._resolve is None:
            return 0
        now_s = self._now_us() // 1_000_000
        ready = [
            (ticker, info)
            for ticker, info in self._pending_reconcile.items()
            if info.expiration_ts + self._config.reconcile_delay_s <= now_s
        ]
        done = 0
        for ticker, info in ready:
            info.attempts += 1
            try:
                resp = self._resolve(ticker)
            except Exception as e:  # noqa: BLE001 — defensive; we log and retry
                logger.warning("reconcile %s failed: %s", ticker, e)
                resp = None
            if not resp:
                if info.attempts >= self._config.reconcile_max_attempts:
                    logger.warning("reconcile %s: giving up after %d attempts",
                                   ticker, info.attempts)
                    self._pending_reconcile.pop(ticker, None)
                continue
            realized = (resp.get("result") or resp.get("settled_result")
                        or resp.get("status") or "")
            if realized.lower() not in ("yes", "no", "no_data"):
                continue  # not yet resolved; leave pending
            done += self._apply_realized(ticker, realized.lower())
            self._pending_reconcile.pop(ticker, None)
            self._emit(
                "reconcile",
                market_ticker=ticker,
                outcome=realized.lower(),
                strategy_label=self._strategy_label,
            )
            if self._reconcile_hook is not None:
                try:
                    self._reconcile_hook(ticker, realized.lower())
                except Exception as e:  # noqa: BLE001
                    logger.warning("reconcile_hook failed: %s", e)
        return done

    def _apply_realized(self, ticker: str, outcome: str) -> int:
        """Write the outcome to every unreconciled row for this ticker."""
        # Pull outstanding decisions for this ticker from the DB — handles
        # crashes mid-tick (we may have rows from prior sessions).
        rows = self._select_unreconciled_for(ticker)
        if not rows:
            return 0

        updated = 0
        for market_ticker, ts_us, side, fill_price, size_contracts in rows:
            pnl = self._compute_pnl(
                outcome=outcome,
                side=side,
                fill_price=Decimal(str(fill_price)),
                size=Decimal(str(size_contracts)),
            )
            stmt = SQL_UPDATE_REALIZED
            params = (outcome, str(pnl), market_ticker, ts_us)
            if self._is_postgres:
                stmt = stmt.replace("?", "%s")
                with self._conn.cursor() as cur:
                    cur.execute(stmt, params)
            else:
                self._conn.execute(stmt, params)
            updated += 1
        self._conn.commit()
        return updated

    def _select_unreconciled_for(self, ticker: str) -> list[tuple]:
        stmt = (
            "SELECT market_ticker, ts_us, recommended_side, "
            "       hypothetical_fill_price, hypothetical_size_contracts "
            "FROM shadow_decisions "
            "WHERE market_ticker = ? AND realized_outcome IS NULL"
        )
        if self._is_postgres:
            stmt = stmt.replace("?", "%s")
            with self._conn.cursor() as cur:
                cur.execute(stmt, (ticker,))
                return list(cur.fetchall())
        cursor = self._conn.execute(stmt, (ticker,))
        return list(cursor.fetchall())

    @staticmethod
    def _compute_pnl(*, outcome: str, side: str, fill_price: Decimal,
                     size: Decimal) -> Decimal:
        """Hypothetical realized P/L for a single decision.

        Kalshi binaries pay $1 per contract if the side wins, $0 otherwise.
        `fill_price` is the buy price the decision imagined paying.

        - `outcome == "no_data"` resolves to No regardless (CRYPTO15M.pdf §0.5).
        - `side == "none"` → zero P/L (decision flagged but not traded).
        """
        if side == "none":
            return ZERO
        effective_outcome = outcome if outcome != "no_data" else "no"
        won = (side == "yes" and effective_outcome == "yes") or \
              (side == "no" and effective_outcome == "no")
        payoff = Decimal("1") if won else Decimal("0")
        return (payoff - fill_price) * size

    # ---- introspection for tests ----

    @property
    def pending_reconciles(self) -> dict[str, _PendingReconcile]:
        return dict(self._pending_reconcile)
