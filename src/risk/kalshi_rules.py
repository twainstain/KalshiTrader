"""Kalshi risk rules (P2-M1-T01 through T10).

Each rule inspects an `Opportunity` plus a `RiskContext` (runtime state the
opportunity alone doesn't carry — account positions, last feed tick, etc.)
and returns a `RuleVerdict`. The `RiskEngine` runs a list of rules and
produces verdicts for each; rejection is fail-closed — any rule rejecting
blocks the opportunity.

The rules are intentionally pure data-in / data-out: no I/O, no side
effects, no clock reads. Callers (the executor, the pipeline) are
responsible for populating `RiskContext` with fresh values each tick.

Defaults match `docs/kalshi_scanner_implementation_tasks.md` P2-M1:
  - `MinEdgeAfterFeesRule`: 100 bps
  - `TimeWindowRule`: [5 s, 60 s]
  - `CIWidthRule`: max 0.15
  - `OpenPositionsRule`: max 3 concurrent
  - `DailyLossRule`: $250/day stop
  - `ReferenceFeedStaleRule`: reject if no tick in ≥ 3 s
  - `BookDepthRule`: min $200 top-of-book
  - `NoDataResolveNoRule`: reject YES when CF Benchmarks health degraded
  - `PositionAccountabilityRule`: per-strike cap $2,500 (1/10 of Kalshi's $25k)
  - `StrikeProximityRule`: min 10 bps between reference and strike (guards
    against the coin-flip zone where resolution noise dominates edge)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable, Protocol

from core.models import BPS_DIVISOR, Opportunity, ZERO


D = Decimal


@dataclass(frozen=True)
class RuleVerdict:
    """One rule's output. `reason` is empty on approval."""
    approved: bool
    rule_name: str
    reason: str = ""


@dataclass
class RiskContext:
    """Runtime state rules read beyond the opportunity itself.

    Callers must pass a fresh instance per evaluation — no rule mutates it.
    Fields default to "safe" values so a partially-populated context doesn't
    silently skip a rule (e.g. missing `last_reference_tick_us` → stale).
    """
    now_us: int
    last_reference_tick_us: int | None = None
    open_positions: int = 0
    daily_realized_pnl_usd: Decimal = ZERO
    position_notional_by_strike_usd: dict[str, Decimal] = field(default_factory=dict)
    cf_benchmarks_degraded: bool = False


class RiskRule(Protocol):
    @property
    def name(self) -> str: ...
    def check(self, opp: Opportunity, ctx: RiskContext) -> RuleVerdict: ...


# ---------------------------------------------------------------------------
# Individual rules
# ---------------------------------------------------------------------------


@dataclass
class MinEdgeAfterFeesRule:
    """P2-M1-T02. Reject unless `expected_edge_bps_after_fees >= min_bps`.

    Defaults to 100 bps (1 %) above fees. Fee is assumed already baked into
    `opp.expected_edge_bps_after_fees` by the strategy.
    """
    min_bps: Decimal = D("100")
    name: str = "min_edge_after_fees"

    def check(self, opp: Opportunity, ctx: RiskContext) -> RuleVerdict:
        if opp.expected_edge_bps_after_fees < self.min_bps:
            return RuleVerdict(
                approved=False,
                rule_name=self.name,
                reason=(
                    f"edge {opp.expected_edge_bps_after_fees} bps < "
                    f"min {self.min_bps} bps"
                ),
            )
        return RuleVerdict(approved=True, rule_name=self.name)


@dataclass
class TimeWindowRule:
    """P2-M1-T03. Reject unless `time_remaining_s in [min_s, max_s]`.

    Defaults [5, 300] (widened 2026-04-21 from [5, 60]): Kalshi opens one
    market per asset at a time and they all close synchronously, so the
    narrower [5, 60] gave only 220 eligible seconds/hour. At [5, 300] the
    scanner has continuous coverage (5 min × 4 cycles = 20 min/hr per
    asset). Lower bound of 5 s is kept — markets expiring in < 5 s don't
    leave enough time for cancel-on-timeout to land.
    """
    min_s: Decimal = D("5")
    max_s: Decimal = D("300")
    name: str = "time_window"

    def check(self, opp: Opportunity, ctx: RiskContext) -> RuleVerdict:
        tr = opp.quote.time_remaining_s
        if tr < self.min_s or tr > self.max_s:
            return RuleVerdict(
                approved=False,
                rule_name=self.name,
                reason=(
                    f"time_remaining_s={tr} outside [{self.min_s}, {self.max_s}]"
                ),
            )
        return RuleVerdict(approved=True, rule_name=self.name)


@dataclass
class CIWidthRule:
    """P2-M1-T04. Reject if `ci_width > max_width`.

    A wide CI means the fair-value model is uncertain — edge estimates are
    unreliable. Default 0.15. PureLag emits `ci_width=0`, so it bypasses.
    """
    max_width: Decimal = D("0.15")
    name: str = "ci_width"

    def check(self, opp: Opportunity, ctx: RiskContext) -> RuleVerdict:
        if opp.ci_width > self.max_width:
            return RuleVerdict(
                approved=False,
                rule_name=self.name,
                reason=f"ci_width={opp.ci_width} > max {self.max_width}",
            )
        return RuleVerdict(approved=True, rule_name=self.name)


@dataclass
class OpenPositionsRule:
    """P2-M1-T05. Reject if `open_positions >= max_concurrent`.

    Strict inequality: at exactly the cap, new positions are blocked. This
    limits simultaneous exposure across strikes during volatile minutes.
    """
    max_concurrent: int = 3
    name: str = "open_positions"

    def check(self, opp: Opportunity, ctx: RiskContext) -> RuleVerdict:
        if ctx.open_positions >= self.max_concurrent:
            return RuleVerdict(
                approved=False,
                rule_name=self.name,
                reason=(
                    f"open_positions={ctx.open_positions} >= "
                    f"max {self.max_concurrent}"
                ),
            )
        return RuleVerdict(approved=True, rule_name=self.name)


@dataclass
class DailyLossRule:
    """P2-M1-T06. Reject when daily realized P/L has breached `-stop_usd`.

    `ctx.daily_realized_pnl_usd` is cumulative for the UTC day; a negative
    value is a loss. Once realized ≤ -stop, new entries are frozen until
    the day rolls over (caller zeroes the accumulator at UTC midnight).
    """
    stop_usd: Decimal = D("250")
    name: str = "daily_loss"

    def check(self, opp: Opportunity, ctx: RiskContext) -> RuleVerdict:
        if ctx.daily_realized_pnl_usd <= -self.stop_usd:
            return RuleVerdict(
                approved=False,
                rule_name=self.name,
                reason=(
                    f"daily_pnl={ctx.daily_realized_pnl_usd} <= "
                    f"-stop {self.stop_usd}"
                ),
            )
        return RuleVerdict(approved=True, rule_name=self.name)


@dataclass
class ReferenceFeedStaleRule:
    """P2-M1-T07. Reject when reference feed hasn't ticked in `max_stale_s`.

    Fail-closed on missing `last_reference_tick_us` — if the caller forgot
    to populate it, we assume the feed is dead. Guards against the
    scanner submitting on a stale spot price that the MM already has.
    """
    max_stale_s: Decimal = D("3")
    name: str = "reference_feed_stale"

    def check(self, opp: Opportunity, ctx: RiskContext) -> RuleVerdict:
        if ctx.last_reference_tick_us is None:
            return RuleVerdict(
                approved=False,
                rule_name=self.name,
                reason="no reference tick recorded",
            )
        elapsed_us = ctx.now_us - ctx.last_reference_tick_us
        elapsed_s = D(elapsed_us) / D("1000000")
        if elapsed_s > self.max_stale_s:
            return RuleVerdict(
                approved=False,
                rule_name=self.name,
                reason=f"stale {elapsed_s}s > {self.max_stale_s}s",
            )
        return RuleVerdict(approved=True, rule_name=self.name)


@dataclass
class BookDepthRule:
    """P2-M1-T08. Reject if the side we're buying lacks `min_top_usd` depth.

    We check only the side matching `opp.recommended_side` — a shallow
    opposite side doesn't matter because we're not hitting it.
    Default $200: enough that a 10-contract fill (~$1-9 notional at typical
    binary prices) won't walk the book.
    """
    min_top_usd: Decimal = D("200")
    name: str = "book_depth"

    def check(self, opp: Opportunity, ctx: RiskContext) -> RuleVerdict:
        q = opp.quote
        if opp.recommended_side == "yes":
            depth = q.book_depth_yes_usd
        elif opp.recommended_side == "no":
            depth = q.book_depth_no_usd
        else:
            return RuleVerdict(approved=True, rule_name=self.name)
        if depth < self.min_top_usd:
            return RuleVerdict(
                approved=False,
                rule_name=self.name,
                reason=(
                    f"depth_{opp.recommended_side}=${depth} < "
                    f"min ${self.min_top_usd}"
                ),
            )
        return RuleVerdict(approved=True, rule_name=self.name)


@dataclass
class NoDataResolveNoRule:
    """P2-M1-T09. Reject YES when CF Benchmarks feed is degraded.

    `CRYPTO15M.pdf` §0.5: missing data at expiry → resolves NO. If CF
    Benchmarks flags a publication issue during the 60-s averaging window,
    YES buyers carry asymmetric resolution-to-NO risk. This rule freezes
    YES entries until the feed recovers; NO-side entries are still allowed
    (they benefit from the no-data tail).
    """
    name: str = "no_data_resolves_no"

    def check(self, opp: Opportunity, ctx: RiskContext) -> RuleVerdict:
        if ctx.cf_benchmarks_degraded and opp.recommended_side == "yes":
            return RuleVerdict(
                approved=False,
                rule_name=self.name,
                reason="CF Benchmarks degraded, YES side blocked",
            )
        return RuleVerdict(approved=True, rule_name=self.name)


@dataclass
class StrikeProximityRule:
    """Reject when the reference spot is within `min_bps` of the strike.

    When spot ≈ strike at/near expiry, the 60-s CF Benchmarks average is
    near-50/50 regardless of our model — resolution noise swamps edge and
    our P/L is dominated by coin-flip variance. The buffer is a bps
    distance between reference and strike:

        abs(reference - strike) / strike * 10000  <  min_bps  →  reject

    Default 10 bps (0.1 %): for BTC at $66,000 that's a $66 zone around
    the strike where we stand down. Only `above` / `below` comparators
    are constrained — `between` / `exactly` / `at_least` have different
    geometry and bypass this check (for now).
    """
    min_bps: Decimal = D("10")
    name: str = "strike_proximity"

    _APPLIES: frozenset = frozenset({"above", "below"})

    def check(self, opp: Opportunity, ctx: RiskContext) -> RuleVerdict:
        q = opp.quote
        if q.comparator not in self._APPLIES:
            return RuleVerdict(approved=True, rule_name=self.name)
        if q.strike == ZERO:
            # Degenerate; fail closed rather than divide-by-zero.
            return RuleVerdict(
                approved=False,
                rule_name=self.name,
                reason="strike is zero",
            )
        gap = abs(q.reference_price - q.strike)
        gap_bps = gap / q.strike * BPS_DIVISOR
        if gap_bps < self.min_bps:
            return RuleVerdict(
                approved=False,
                rule_name=self.name,
                reason=(
                    f"reference ${q.reference_price} within {gap_bps:.2f} bps "
                    f"of strike ${q.strike} (min {self.min_bps} bps)"
                ),
            )
        return RuleVerdict(approved=True, rule_name=self.name)


@dataclass
class PositionAccountabilityRule:
    """P2-M1-T10. Per-strike notional cap.

    Kalshi publishes a $25k position-accountability threshold; we enforce
    1/10 of that ($2,500) per strike across open positions, including the
    notional the incoming opportunity would add. Keyed by
    `market_ticker` (each 15-min strike is a distinct market).
    """
    per_strike_cap_usd: Decimal = D("2500")
    name: str = "position_accountability"

    def check(self, opp: Opportunity, ctx: RiskContext) -> RuleVerdict:
        ticker = opp.quote.market_ticker
        existing = ctx.position_notional_by_strike_usd.get(ticker, ZERO)
        # Notional for a binary contract = fill_price × size (cost to enter).
        incoming = opp.hypothetical_fill_price * opp.hypothetical_size_contracts
        total = existing + incoming
        if total > self.per_strike_cap_usd:
            return RuleVerdict(
                approved=False,
                rule_name=self.name,
                reason=(
                    f"strike {ticker} notional ${total} > "
                    f"cap ${self.per_strike_cap_usd}"
                ),
            )
        return RuleVerdict(approved=True, rule_name=self.name)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EngineDecision:
    approved: bool
    verdicts: tuple[RuleVerdict, ...]

    @property
    def rejections(self) -> tuple[RuleVerdict, ...]:
        return tuple(v for v in self.verdicts if not v.approved)


class RiskEngine:
    """Applies every configured rule to each opportunity.

    All rules are evaluated — we don't short-circuit — so dashboards can
    count *every* rejection reason, not just the first. `decide()` returns
    an aggregate verdict: approved iff every rule approved.
    """

    def __init__(self, rules: Iterable[RiskRule]) -> None:
        self._rules = tuple(rules)

    @property
    def rules(self) -> tuple[RiskRule, ...]:
        return self._rules

    def decide(self, opp: Opportunity, ctx: RiskContext) -> EngineDecision:
        verdicts = tuple(r.check(opp, ctx) for r in self._rules)
        approved = all(v.approved for v in verdicts)
        return EngineDecision(approved=approved, verdicts=verdicts)


def default_rules() -> list[RiskRule]:
    """Rules wired with plan-default thresholds. Phase-2 config can override."""
    return [
        MinEdgeAfterFeesRule(),
        TimeWindowRule(),
        CIWidthRule(),
        OpenPositionsRule(),
        DailyLossRule(),
        ReferenceFeedStaleRule(),
        BookDepthRule(),
        NoDataResolveNoRule(),
        PositionAccountabilityRule(),
        StrikeProximityRule(),
    ]
