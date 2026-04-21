"""PureLagStrategy — reactive "Coinbase moved → take matching Kalshi side".

Zero statistical model: no σ, no Brownian projection, no CI width. Just
detect price moves in the reference feed and take the side of any Kalshi
book that hasn't repriced.

Hypothesis (per docs/kalshi_feed_lag_expanded_sample.md): during the
100-1400 ms window between a Coinbase price move and the Kalshi MM
reprice, a solo operator can buy the stale side at its pre-move price.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Deque, Iterable

from core.models import (
    BPS_DIVISOR,
    MarketQuote,
    ONE,
    Opportunity,
    OpportunityStatus,
    ZERO,
)


@dataclass
class PureLagConfig:
    """Thresholds for the PureLagStrategy.

    Calibrated 2026-04-20 from the 3-model live run, then tightened
    2026-04-21 to align with the risk engine:
      - `move_threshold_bps`: lowered 5 → 3 after observing the 5 bps
        threshold fired only ~5×/hour at 1 s sample rate.
      - `rolling_window_us`: shrunk 10 s → 5 s so moves are detected against
        a tighter baseline (less smoothing).
      - `time_window_seconds`: (5, 60) was too narrow — Kalshi opens one
        market per asset at a time and they all close synchronously, so
        `(5, 60)` gave only 220 eligible seconds/hour across ALL assets
        (a 55 s burst × 4 cycles). Widened 2026-04-21 to **(5, 300)** so
        the scanner has continuous coverage: 5 min × 4 cycles = 20 min of
        in-window sampling per 15-min cycle, across 7 assets = ~140 asset-
        minutes per hour of candidates. Still covers the final-minute
        partial-obs edge zone (per feasibility report §3.3). Paired with
        `RiskEngine.TimeWindowRule([5, 300])`.
      - `min_fill_price`: new floor, rejects lottery-ticket yes/no asks
        below $0.10 that the stat_model used to bleed on.
    """
    # Minimum Coinbase move (bps from rolling mean) to trigger a signal.
    move_threshold_bps: Decimal = Decimal("3")
    # Window over which the rolling mean is computed (microseconds).
    rolling_window_us: int = 5_000_000   # 5 s
    # Minimum edge after fees (after implicit 35 bps fee model).
    min_edge_bps_after_fees: Decimal = Decimal("100")
    min_book_depth_usd: Decimal = Decimal("50")
    time_window_seconds: tuple[int, int] = (5, 300)
    hypothetical_size_contracts: Decimal = Decimal("10")
    # Reject fills below this price — prevents accumulating tiny long-tail
    # bets on low-flow assets (XRP/DOG/ETH lottery tickets).
    min_fill_price: Decimal = Decimal("0.10")


class _AssetRollingPrice:
    """Rolling price history per asset for move-detection."""

    def __init__(self, window_us: int) -> None:
        self._window_us = window_us
        self._ticks: Deque[tuple[int, Decimal]] = deque()

    def record(self, ts_us: int, price: Decimal) -> None:
        self._ticks.append((ts_us, price))
        # Evict old ticks.
        cutoff = ts_us - self._window_us
        while self._ticks and self._ticks[0][0] < cutoff:
            self._ticks.popleft()

    def rolling_mean(self, now_us: int) -> Decimal | None:
        # Don't evict here — we may be asked for the mean at `now` before the
        # next record() call lands.
        cutoff = now_us - self._window_us
        vals = [p for (ts, p) in self._ticks if ts >= cutoff]
        if not vals:
            return None
        return sum(vals, Decimal("0")) / Decimal(len(vals))

    def latest(self) -> Decimal | None:
        return self._ticks[-1][1] if self._ticks else None


class PureLagStrategy:
    """Detect Coinbase move > threshold_bps → take matching Kalshi side.

    Signals persist — same strategy instance is called across many
    `evaluate()` invocations, each potentially with a different asset. The
    rolling-price history is per-asset, fed by `record_reference_tick`.
    """

    def __init__(
        self,
        config: PureLagConfig | None = None,
        *,
        now_us: Callable[[], int] | None = None,
    ) -> None:
        self.config = config or PureLagConfig()
        self._per_asset: dict[str, _AssetRollingPrice] = {}
        import time as _t
        self._now_us = now_us or (lambda: int(_t.time() * 1_000_000))

    # ---- ingestion of reference ticks (called from the run-loop) ----

    def record_reference_tick(self, asset: str, price: Decimal) -> None:
        asset = asset.lower()
        if asset not in self._per_asset:
            self._per_asset[asset] = _AssetRollingPrice(
                self.config.rolling_window_us
            )
        self._per_asset[asset].record(self._now_us(), price)

    # ---- Strategy.evaluate interface (duck-typed, same signature as
    #      KalshiFairValueStrategy.evaluate) ----

    def evaluate(self, quote: MarketQuote, *, asset: str) -> Opportunity | None:
        cfg = self.config

        # Time-window gate — keep parity with the stat strategy so the
        # side-by-side comparison controls for window regime.
        tr = quote.time_remaining_s
        lo, hi = cfg.time_window_seconds
        if not (Decimal(str(lo)) <= tr < Decimal(str(hi))):
            return None

        # Book-depth gate.
        if (
            quote.book_depth_yes_usd < cfg.min_book_depth_usd
            and quote.book_depth_no_usd < cfg.min_book_depth_usd
        ):
            return None

        # Rolling-price snapshot for this asset.
        rp = self._per_asset.get(asset.lower())
        if rp is None:
            return None
        now = self._now_us()
        mean = rp.rolling_mean(now)
        latest = rp.latest()
        if mean is None or latest is None or mean <= 0:
            return None

        move_bps = (latest - mean) / mean * BPS_DIVISOR
        if abs(move_bps) < cfg.move_threshold_bps:
            return None

        # Pick side based on direction of move.
        #   Move > 0 (price up) → prior avg was LOWER, so current spot is now
        #                          ABOVE strike, yes is more likely → buy yes.
        #   Move < 0 → buy no.
        fee = quote.fee_bps / BPS_DIVISOR
        if move_bps > 0:
            side = "yes"
            fill_price = quote.best_yes_ask
            # Pure-lag treats "book ask" as the price we pay; "1 - ask" is
            # implicit probability. Edge = (1 - ask_price) in pp, minus fees.
            # Conceptually: the scanner is buying at `ask`, expecting the
            # book to reprice higher before close.
            # For comparison with stat strategy we report the same field.
            # We use a heuristic edge = (1 - ask) - fee.  This is the raw
            # upside if the market resolves Yes.
            edge_after_fees = (ONE - fill_price) - fee
        else:
            side = "no"
            fill_price = quote.best_no_ask
            edge_after_fees = (ONE - fill_price) - fee

        # Reject lottery-ticket fills — cheap asks have extreme outcome skew
        # that backtests over-credited due to fee/payout accounting.
        if fill_price < cfg.min_fill_price:
            return None

        edge_bps = edge_after_fees * BPS_DIVISOR
        if edge_bps < cfg.min_edge_bps_after_fees:
            return None

        # For record-keeping we still fill a p_yes / ci_width. The lag strat
        # doesn't produce a real probability, so we record the book-implied
        # probability instead, which is the only meaningful number here.
        book_implied_p_yes = ONE - quote.best_no_ask
        return Opportunity(
            quote=quote,
            p_yes=book_implied_p_yes,
            ci_width=ZERO,
            recommended_side=side,
            hypothetical_fill_price=fill_price,
            hypothetical_size_contracts=cfg.hypothetical_size_contracts,
            expected_edge_bps_after_fees=edge_bps,
            status=OpportunityStatus.PRICED,
            no_data_haircut_bps=ZERO,
        )

    def evaluate_many(
        self, quotes: Iterable[MarketQuote],
        *, asset_by_ticker: dict[str, str],
    ) -> list[Opportunity]:
        out: list[Opportunity] = []
        for q in quotes:
            a = asset_by_ticker.get(q.market_ticker)
            if a is None:
                continue
            opp = self.evaluate(q, asset=a)
            if opp is not None:
                out.append(opp)
        return out
