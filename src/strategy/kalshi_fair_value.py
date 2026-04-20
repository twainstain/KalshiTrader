"""Kalshi fair-value model + strategy (P1-M3).

`FairValueModel.price(...)` returns a calibrated probability `p_yes` that a
Kalshi crypto 15-min binary resolves Yes, plus a confidence interval width
`ci_width`. Two regimes:

- **time_remaining_s > 60:** the averaging window hasn't started yet.
  Project the reference index at the midpoint of the window using
  geometric Brownian motion (σ per-asset, risk-neutral drift ≈ 0 at these
  horizons). p_yes for `above K` uses the closed-form Φ.
- **time_remaining_s ≤ 60:** the averaging window has begun. Blend the
  observed partial `reference_60s_avg` (weight = seconds_observed / 60)
  with a projection of the remaining seconds. For small remainders the
  partial observation dominates, so p_yes collapses toward 0/1 as
  time_remaining_s → 0.

A `no_data_haircut` is subtracted from p_yes to account for the CF
Benchmarks outage tail (CRYPTO15M.pdf §0.5: missing data resolves to No).
Default 0.005 = 50 bps.

Comparators supported: `above`, `below`, `at_least` (identical to `above`
for continuous distributions). `between` and `exactly` require a second
strike the current `MarketQuote` shape doesn't carry — they raise
`NotImplementedError` with a clear hint.

`KalshiFairValueStrategy` wraps the model and emits `Opportunity` objects
for the shadow evaluator (P1-M4). It chooses `recommended_side` by
comparing model p_yes to the best ask on each side net of fees; rejects
when CI is too wide, book depth is too thin, or edge after fees falls
below the configured floor. In Phase 1 this is a scoring decision only
— no orders.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from core.models import (
    BPS_DIVISOR,
    MarketQuote,
    ONE,
    Opportunity,
    OpportunityStatus,
    ZERO,
)


# Seconds in a (trading) year; 365 × 86 400. We don't adjust for trading
# hours because Kalshi CRYPTO15M runs 24/7.
SECONDS_PER_YEAR = Decimal("31536000")

# Fallback annualized log-return volatilities when a config isn't supplied.
# These are reasonable defaults circa 2026 but **must** be recalibrated from
# actual CF Benchmarks history before P1 feasibility analysis (P1-M5).
DEFAULT_ANNUAL_VOL: dict[str, Decimal] = {
    "btc": Decimal("0.60"),
    "eth": Decimal("0.75"),
    "sol": Decimal("0.95"),
}


# ---------------------------------------------------------------------------
# Pure math helpers.
# ---------------------------------------------------------------------------

def _norm_cdf(x: Decimal) -> Decimal:
    """Standard normal CDF via math.erf. Returns Decimal to stay in-regime."""
    # Decimal has no native erf; delegate to math and re-wrap. The precision
    # loss (≤ 17 decimal digits) is negligible against the noise floor of
    # the inputs — reference prices are 4-decimal and vol is an estimate.
    return Decimal(str(0.5 * (1.0 + math.erf(float(x) / math.sqrt(2.0)))))


def prob_above_strike(
    *,
    spot: Decimal,
    strike: Decimal,
    sigma_over_horizon: Decimal,
) -> Decimal:
    """P(S_T > K) under geometric Brownian motion with drift=0.

    `sigma_over_horizon` = σ · √T (already scaled for the horizon). When
    zero (degenerate: no time remaining), returns 1 if spot > strike else 0.
    Division-by-zero is the whole question at T=0; we answer it directly.
    """
    if sigma_over_horizon <= 0:
        return ONE if spot > strike else ZERO
    if spot <= 0 or strike <= 0:
        return ZERO
    # d = (ln(S/K) - σ²·T / 2) / (σ·√T)
    ln_s_k = Decimal(str(math.log(float(spot) / float(strike))))
    sigma_sq = sigma_over_horizon * sigma_over_horizon
    d = (ln_s_k - sigma_sq / Decimal("2")) / sigma_over_horizon
    return _norm_cdf(d)


def annual_vol_to_horizon(annual_vol: Decimal, horizon_seconds: Decimal) -> Decimal:
    """Convert an annualized σ to σ · √(horizon / year)."""
    if horizon_seconds <= 0:
        return ZERO
    ratio = horizon_seconds / SECONDS_PER_YEAR
    return annual_vol * Decimal(str(math.sqrt(float(ratio))))


# ---------------------------------------------------------------------------
# FairValueModel
# ---------------------------------------------------------------------------

@dataclass
class FairValueModel:
    """Pricer for Kalshi crypto 15-min binaries.

    Configure via `annual_vol_by_asset` to override the defaults when the
    P1-M5 calibration lands. `no_data_haircut` shaves a fixed probability
    off every p_yes to model the CF Benchmarks outage tail.
    """
    annual_vol_by_asset: dict[str, Decimal] | None = None
    no_data_haircut: Decimal = Decimal("0.005")
    min_sigma_horizon: Decimal = Decimal("1e-6")  # floor to avoid NaN at T≈0

    # --- public API ---

    def price(
        self,
        *,
        asset: str,
        strike: Decimal,
        comparator: str,
        reference_price: Decimal,
        reference_60s_avg: Decimal,
        time_remaining_s: Decimal,
    ) -> tuple[Decimal, Decimal]:
        """Return `(p_yes, ci_width)` for the given market.

        `reference_60s_avg` is only used when `time_remaining_s ≤ 60` — the
        partial observation of the averaging window.
        """
        tr = Decimal(str(time_remaining_s))
        strike = Decimal(str(strike))
        spot = Decimal(str(reference_price))

        if comparator in ("above", "at_least"):
            direction = "above"
        elif comparator == "below":
            direction = "below"
        elif comparator in ("between", "exactly"):
            raise NotImplementedError(
                f"comparator={comparator!r} needs a secondary strike which "
                f"MarketQuote doesn't currently carry — add `strike_high` "
                f"before enabling these markets."
            )
        else:
            raise ValueError(f"unsupported comparator: {comparator!r}")

        if tr > Decimal("60"):
            # Full window not yet sampled — project to the window midpoint.
            horizon_s = tr - Decimal("30")  # midpoint of [close-60, close]
            sigma = self._sigma_over_horizon(asset, horizon_s)
            p_above = prob_above_strike(
                spot=spot, strike=strike, sigma_over_horizon=sigma,
            )
        elif tr > 0:
            observed_s = Decimal("60") - tr
            remaining_s = tr
            # Weighted mix: observed partial avg (weight = observed_s / 60)
            # combined with a projected remaining segment whose expected
            # value is approximately spot (drift-free) with variance scaled
            # by remaining_s / 60 to reflect the averaging.
            w_observed = observed_s / Decimal("60")
            w_remaining = remaining_s / Decimal("60")
            # Expected resolution value if spot were frozen:
            blended_mean = (
                Decimal(str(reference_60s_avg)) * w_observed
                + spot * w_remaining
            )
            # Variance of the remaining average over a `remaining_s` horizon.
            sigma_base = self._sigma_over_horizon(asset, remaining_s)
            # Scale by w_remaining — only the remaining fraction contributes
            # to the final 60s-average's variance.
            sigma = sigma_base * w_remaining
            # Approximate P(blended > K) as P(X > K) with X ~ N(mean, sigma²·spot²).
            # Translate to the multiplicative GBM frame: effective spot = blended_mean.
            p_above = prob_above_strike(
                spot=blended_mean, strike=strike, sigma_over_horizon=sigma,
            )
        else:
            # Closed: resolution is the observed 60s avg, no uncertainty.
            observed = Decimal(str(reference_60s_avg))
            p_above = ONE if observed > strike else ZERO

        p_yes = p_above if direction == "above" else (ONE - p_above)

        # no-data haircut — subtract a probability mass that represents the
        # tail where CF Benchmarks can't resolve; under that outcome the
        # market goes No regardless. Clamp to [0, 1].
        p_yes = p_yes - self.no_data_haircut
        if p_yes < ZERO:
            p_yes = ZERO
        if p_yes > ONE:
            p_yes = ONE

        ci_width = self._ci_width(
            p_yes=p_yes, sigma_over_horizon=self._sigma_over_horizon(asset, tr),
        )
        return p_yes, ci_width

    # --- internals ---

    def _sigma_over_horizon(self, asset: str, horizon_s: Decimal) -> Decimal:
        vol_map = self.annual_vol_by_asset or DEFAULT_ANNUAL_VOL
        annual = vol_map.get(asset.lower(), DEFAULT_ANNUAL_VOL.get(asset.lower(), Decimal("0.60")))
        h = horizon_s if horizon_s > 0 else Decimal("0")
        sigma = annual_vol_to_horizon(annual, h)
        if sigma < self.min_sigma_horizon:
            return self.min_sigma_horizon
        return sigma

    def _ci_width(self, *, p_yes: Decimal, sigma_over_horizon: Decimal) -> Decimal:
        """Proxy confidence width. Bernoulli-like spread scaled by σ.

        The resolution is a Bernoulli outcome, so the "noise floor" is
        √(p(1-p)). We scale by σ (normalized by a small constant) so that
        longer horizons widen the CI, collapsing to ~0 as T → 0.
        """
        bern_sd = Decimal(str(math.sqrt(float(p_yes * (ONE - p_yes)))))
        # Normalize sigma: 0.05 chosen so a typical 15m-window produces
        # CI widths in the 0.05-0.20 range. Recalibrate in P1-M5.
        scale = sigma_over_horizon / Decimal("0.05")
        if scale > ONE:
            scale = ONE
        return bern_sd * scale


# ---------------------------------------------------------------------------
# KalshiFairValueStrategy
# ---------------------------------------------------------------------------

@dataclass
class StrategyConfig:
    """Rejection thresholds for Phase-1 shadow decisions.

    None of these hit the wire; they score decisions for post-hoc analysis
    in the feasibility report. P2 risk rules impose additional constraints.
    """
    min_edge_bps_after_fees: Decimal = Decimal("100")  # 1% net edge floor
    max_ci_width: Decimal = Decimal("0.15")
    min_book_depth_usd: Decimal = Decimal("200")
    # Phase-1 window gate: only score opportunities inside the final minute
    # by default. Set to (0, 900) to score the entire 15m.
    time_window_seconds: tuple[int, int] = (0, 900)
    hypothetical_size_contracts: Decimal = Decimal("10")


class KalshiFairValueStrategy:
    """Emit an `Opportunity` per approvable quote; `None` otherwise."""

    def __init__(
        self,
        model: FairValueModel,
        config: StrategyConfig | None = None,
    ) -> None:
        self.model = model
        self.config = config or StrategyConfig()

    def evaluate(self, quote: MarketQuote, *, asset: str) -> Opportunity | None:
        cfg = self.config

        # Time-window gate (inclusive-exclusive on the upper end).
        tr = quote.time_remaining_s
        lo, hi = cfg.time_window_seconds
        if not (Decimal(str(lo)) <= tr < Decimal(str(hi))):
            return None

        # Book-depth gate — need SOME liquidity on at least one side.
        if (
            quote.book_depth_yes_usd < cfg.min_book_depth_usd
            and quote.book_depth_no_usd < cfg.min_book_depth_usd
        ):
            return None

        try:
            p_yes, ci_width = self.model.price(
                asset=asset,
                strike=quote.strike,
                comparator=quote.comparator,
                reference_price=quote.reference_price,
                reference_60s_avg=quote.reference_60s_avg,
                time_remaining_s=quote.time_remaining_s,
            )
        except NotImplementedError:
            return None

        if ci_width > cfg.max_ci_width:
            return None

        # Fee-adjusted edge. `fee_bps` on Kalshi quotes is per-side, not
        # round-trip — we apply it once (buying YES or buying NO).
        fee = quote.fee_bps / BPS_DIVISOR
        yes_edge = p_yes - quote.best_yes_ask - fee  # favorable if > 0
        no_edge = (ONE - p_yes) - quote.best_no_ask - fee

        # Pick the side with the larger positive edge.
        side, edge = "none", ZERO
        if yes_edge > no_edge and yes_edge > ZERO:
            side, edge = "yes", yes_edge
        elif no_edge > ZERO:
            side, edge = "no", no_edge

        edge_bps = edge * BPS_DIVISOR
        if edge_bps < cfg.min_edge_bps_after_fees or side == "none":
            return None

        fill_price = (
            quote.best_yes_ask if side == "yes" else quote.best_no_ask
        )

        return Opportunity(
            quote=quote,
            p_yes=p_yes,
            ci_width=ci_width,
            recommended_side=side,
            hypothetical_fill_price=fill_price,
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
            asset = asset_by_ticker.get(q.market_ticker)
            if asset is None:
                continue
            opp = self.evaluate(q, asset=asset)
            if opp is not None:
                out.append(opp)
        return out
