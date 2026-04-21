"""Partial-observation fair-value model.

Unlike `kalshi_fair_value.FairValueModel` which uses current spot as its
best estimate of `close_60s_avg`, this model integrates over the CF 60-second
averaging window directly:

  1. The window `[T_close - 60, T_close]` is split into observed + future.
  2. Observed portion: if we have sub-second reference ticks in
     `[T_close - 60, now]`, we compute `observed_avg` from them (no variance).
  3. Future portion: under a Brownian-motion assumption on log-spot, the
     expected time-average over `[T_future_start, T_close]` is current spot.
  4. Blended point estimate:
        E[close_avg] = (observed_s / 60)·observed_avg + (future_s / 60)·spot
  5. Variance collapses as the window fills — only the future portion
     contributes, and the time-averaging further damps it:
        σ_effective = (future_s / 60)·σ_15min·√((τ_fs + future_s/3) / 900)
     where `τ_fs` is seconds from now until the future portion starts.

At `T_remaining = 30s`, `σ_effective` is ~71% lower than the naive
`FairValueModel` — the model becomes genuinely confident exactly where
the naïve spot-based model fails (see
`docs/kalshi_shadow_live_capture_results.md` §6).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal

from collections import deque
from typing import Callable, Deque, Iterable

from core.models import (
    BPS_DIVISOR,
    MarketQuote,
    ONE,
    Opportunity,
    OpportunityStatus,
    ZERO,
)
from strategy.kalshi_fair_value import (
    DEFAULT_SIGMA_15MIN,
    StrategyConfig,
    WINDOW_SECONDS,
    _DOWN_COMPARATORS,
    _UP_COMPARATORS,
    _norm_cdf,
)


WINDOW_AVG_SECONDS = Decimal("60")   # CF 60-second averaging window


@dataclass
class PartialAvgFairValueModel:
    """Pricer that explicitly blends observed + forecast portions of the
    60s close-averaging window. API-compatible with `FairValueModel.price`
    when `observed_window_s=0` is passed (degrades to naive spot model but
    with σ_effective that reflects the 60s averaging damping).
    """
    sigma_15min_by_asset: dict[str, Decimal] = field(default_factory=dict)
    no_data_haircut: Decimal = Decimal("0.005")
    min_sigma: Decimal = Decimal("1e-8")

    # ---- public API ----

    def price(
        self,
        *,
        asset: str,
        strike: Decimal,
        comparator: str,
        reference_price: Decimal,            # current spot
        reference_60s_avg: Decimal,          # unused (API parity)
        time_remaining_s: Decimal,
        observed_window_s: Decimal = ZERO,   # seconds of the close-60s window we've already seen
        observed_window_avg: Decimal | None = None,  # avg of observed ticks
    ) -> tuple[Decimal, Decimal]:
        """Return (p_yes, ci_width).

        Default-arg path (`observed_window_s=0`) is the pure-forecast
        regime — used when `time_remaining_s >= 60`. Caller wanting
        partial-observation credit passes the measured observed-portion
        metrics.
        """
        comp = comparator.lower()
        if comp in ("between", "exactly"):
            raise NotImplementedError(
                f"comparator={comparator!r} needs a secondary strike — "
                f"MarketQuote has no strike_high"
            )
        if comp not in _UP_COMPARATORS and comp not in _DOWN_COMPARATORS:
            raise ValueError(f"unsupported comparator: {comparator!r}")

        strike = Decimal(str(strike))
        spot = Decimal(str(reference_price))
        tr = Decimal(str(time_remaining_s))
        obs_s = max(ZERO, min(WINDOW_AVG_SECONDS, Decimal(str(observed_window_s))))
        if strike <= 0 or spot <= 0 or tr <= 0:
            # Degenerate: fall through to comparator sense on last known side
            if comp in _DOWN_COMPARATORS:
                return (ONE if spot < strike else ZERO, ZERO)
            return (ONE if spot >= strike else ZERO, ZERO)

        future_s, tau_fs = self._window_split(tr, obs_s)
        obs_avg = Decimal(str(observed_window_avg)) if observed_window_avg is not None else spot

        # Blended point estimate of close_60s_avg.
        e_close_avg = (
            (obs_s / WINDOW_AVG_SECONDS) * obs_avg
            + (future_s / WINDOW_AVG_SECONDS) * spot
        )

        # σ_effective scaled down by averaging damping.
        sigma_full = self._sigma(asset)
        sigma_effective = self._sigma_effective(
            sigma_full=sigma_full, future_s=future_s, tau_fs=tau_fs,
        )
        if sigma_effective < self.min_sigma:
            sigma_effective = self.min_sigma

        # log(E[close_avg]/strike) — sign controls which side the blend favors.
        log_r = Decimal(str(math.log(float(e_close_avg) / float(strike))))
        p_up = _norm_cdf(log_r / sigma_effective)

        if comp in _DOWN_COMPARATORS:
            p_yes = ONE - p_up
        else:
            p_yes = p_up

        p_yes = p_yes - self.no_data_haircut
        if p_yes < ZERO:
            p_yes = ZERO
        if p_yes > ONE:
            p_yes = ONE

        ci_width = self._ci_width(p_yes)
        return p_yes, ci_width

    # ---- math helpers ----

    @staticmethod
    def _window_split(
        time_remaining_s: Decimal, observed_window_s: Decimal,
    ) -> tuple[Decimal, Decimal]:
        """Return (future_s, tau_fs).

        future_s: seconds of the close-60s window that are still to come.
        tau_fs: seconds from now until the future portion starts.
        """
        # If observed_window_s > 0, we're inside the close-60s window already.
        if observed_window_s > ZERO:
            future_s = WINDOW_AVG_SECONDS - observed_window_s
            tau_fs = ZERO
            return future_s, tau_fs
        # Observed portion is empty. Future window is the full 60s ending at close.
        future_s = WINDOW_AVG_SECONDS
        tau_fs = time_remaining_s - WINDOW_AVG_SECONDS
        if tau_fs < ZERO:
            # time_remaining < 60 but caller passed observed_window_s=0 —
            # interpret that as no tick data; damp the window accordingly.
            future_s = time_remaining_s
            tau_fs = ZERO
        return future_s, tau_fs

    @staticmethod
    def _sigma_effective(
        *, sigma_full: Decimal, future_s: Decimal, tau_fs: Decimal,
    ) -> Decimal:
        """σ of the partially-observed close-60s avg."""
        if future_s <= 0:
            return Decimal("1e-8")
        # σ_per_s = σ_15min / √900. Var of time-avg over [tau_fs, tau_fs+future_s]
        # is σ_per_s² · (tau_fs + future_s/3). Multiply by (future_s/60) weight
        # of the future portion in E[close_avg].
        var_scalar = float(tau_fs) + float(future_s) / 3.0
        if var_scalar <= 0:
            return Decimal("1e-8")
        scale = math.sqrt(var_scalar / float(WINDOW_SECONDS))
        weight = float(future_s) / float(WINDOW_AVG_SECONDS)
        return sigma_full * Decimal(str(scale)) * Decimal(str(weight))

    def _sigma(self, asset: str) -> Decimal:
        key = asset.lower()
        if key in self.sigma_15min_by_asset:
            return self.sigma_15min_by_asset[key]
        if key in DEFAULT_SIGMA_15MIN:
            return DEFAULT_SIGMA_15MIN[key]
        return DEFAULT_SIGMA_15MIN.get("btc", Decimal("0.002"))

    @staticmethod
    def _ci_width(p_yes: Decimal) -> Decimal:
        q = p_yes * (ONE - p_yes)
        return Decimal(str(2.0 * math.sqrt(float(q)))) if q > 0 else ZERO


# ---------------------------------------------------------------------------
# PartialAvgFairValueStrategy — live wrapper with rolling tick buffer
# ---------------------------------------------------------------------------


class _AssetTickBuffer:
    """Rolling (ts_us, price) buffer per asset — retains `window_us` of ticks."""

    def __init__(self, window_us: int) -> None:
        self._window_us = window_us
        self._ticks: Deque[tuple[int, Decimal]] = deque()

    def record(self, ts_us: int, price: Decimal) -> None:
        self._ticks.append((ts_us, price))
        cutoff = ts_us - self._window_us
        while self._ticks and self._ticks[0][0] < cutoff:
            self._ticks.popleft()

    def avg_in_window(self, start_us: int, end_us: int) -> tuple[Decimal, int]:
        if not self._ticks:
            return ZERO, 0
        vals = [p for (ts, p) in self._ticks if start_us <= ts <= end_us]
        if not vals:
            return ZERO, 0
        return sum(vals, ZERO) / Decimal(len(vals)), len(vals)

    def latest(self) -> Decimal | None:
        return self._ticks[-1][1] if self._ticks else None


class PartialAvgFairValueStrategy:
    """Wraps `PartialAvgFairValueModel` with a per-asset rolling tick buffer.

    The run-loop must feed reference ticks via `record_reference_tick`. On
    `evaluate()`, the strategy computes the observed portion of the close-60s
    window from the buffer and passes it into the model.
    """

    def __init__(
        self,
        model: PartialAvgFairValueModel | None = None,
        config: StrategyConfig | None = None,
        *,
        tick_window_us: int = 120_000_000,   # 2 min — covers the 60s avg window + slack
        now_us: Callable[[], int] | None = None,
    ) -> None:
        self.model = model or PartialAvgFairValueModel()
        self.config = config or StrategyConfig()
        self._buffers: dict[str, _AssetTickBuffer] = {}
        self._tick_window_us = tick_window_us
        import time as _t
        self._now_us = now_us or (lambda: int(_t.time() * 1_000_000))

    def record_reference_tick(self, asset: str, price: Decimal) -> None:
        asset = asset.lower()
        if asset not in self._buffers:
            self._buffers[asset] = _AssetTickBuffer(self._tick_window_us)
        self._buffers[asset].record(self._now_us(), price)

    def evaluate(self, quote: MarketQuote, *, asset: str) -> Opportunity | None:
        cfg = self.config
        tr = quote.time_remaining_s
        lo, hi = cfg.time_window_seconds
        if not (Decimal(str(lo)) <= tr < Decimal(str(hi))):
            return None
        if (
            quote.book_depth_yes_usd < cfg.min_book_depth_usd
            and quote.book_depth_no_usd < cfg.min_book_depth_usd
        ):
            return None

        now_us = self._now_us()
        close_us = now_us + int(float(tr) * 1_000_000)
        obs_start = close_us - 60_000_000
        obs_end = min(now_us, close_us)
        observed_s = ZERO
        observed_avg: Decimal | None = None
        buf = self._buffers.get(asset.lower())
        if buf is not None and obs_end > obs_start:
            avg, n_obs = buf.avg_in_window(obs_start, obs_end)
            if n_obs > 0:
                observed_avg = avg
                observed_s = Decimal((obs_end - obs_start) / 1_000_000)

        try:
            p_yes, ci_width = self.model.price(
                asset=asset, strike=quote.strike, comparator=quote.comparator,
                reference_price=quote.reference_price,
                reference_60s_avg=quote.reference_60s_avg,
                time_remaining_s=tr,
                observed_window_s=observed_s,
                observed_window_avg=observed_avg,
            )
        except NotImplementedError:
            return None

        if ci_width > cfg.max_ci_width:
            return None

        fee = quote.fee_bps / BPS_DIVISOR
        yes_edge = p_yes - quote.best_yes_ask - fee
        no_edge = (ONE - p_yes) - quote.best_no_ask - fee
        side, edge = "none", ZERO
        if yes_edge > no_edge and yes_edge > ZERO:
            side, edge = "yes", yes_edge
        elif no_edge > ZERO:
            side, edge = "no", no_edge

        edge_bps = edge * BPS_DIVISOR
        if edge_bps < cfg.min_edge_bps_after_fees or side == "none":
            return None

        fill_price = quote.best_yes_ask if side == "yes" else quote.best_no_ask
        return Opportunity(
            quote=quote, p_yes=p_yes, ci_width=ci_width,
            recommended_side=side, hypothetical_fill_price=fill_price,
            hypothetical_size_contracts=cfg.hypothetical_size_contracts,
            expected_edge_bps_after_fees=edge_bps,
            status=OpportunityStatus.PRICED,
            no_data_haircut_bps=self.model.no_data_haircut * BPS_DIVISOR,
        )

    def evaluate_many(
        self, quotes: Iterable[MarketQuote], *, asset_by_ticker: dict[str, str],
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
