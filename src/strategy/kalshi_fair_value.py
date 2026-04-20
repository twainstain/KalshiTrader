"""Kalshi fair-value model + strategy — up/down market type (P1-M3 refactor).

Kalshi's crypto 15-min `/bitcoin-price-up-down/` markets ask a single
question per window: **"Is the 60s-avg BRTI at close ≥ the 60s-avg BRTI
at the prior 15-min boundary?"** The API returns `floor_strike = prior_avg`
and `expiration_value = settled_avg`; resolution is `at_least` / `>=`.

Because the question is always "is the 15-min log-return ≥ 0?", the natural
model is a single univariate normal distribution over 15-min log-returns,
parameterized by `σ_15min` per asset, drift ≈ 0 at these horizons:

    log_r_observed = log(current_reference / prior_avg)
    σ_remaining    = σ_15min · √(time_remaining / 900)
    p_yes          = Φ(log_r_observed / σ_remaining)   − no_data_haircut

σ_15min is calibrated from the `expiration_value` chain in SQLite via
`scripts/calibrate_sigma.py` — not annualized-then-scaled, which failed
calibration on the earlier GBM attempt.

Phase-1 backtest (2,822 settled markets per asset, decision T-30s):
    BTC  Brier 0.047   hit 93.9%
    ETH  Brier 0.041   hit 95.3%
    SOL  Brier 0.043   hit 95.1%
vs. naïve-0.5 baseline (Brier 0.25).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
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


# Window length for Kalshi crypto 15M markets, in seconds.
WINDOW_SECONDS = Decimal("900")


# Empirical σ_15min calibrated from 30 days of Kalshi public `expiration_value`
# chains (Mar 20 – Apr 20, 2026). Override via `sigma_15min_by_asset` or
# rerun `scripts/calibrate_sigma.py` against the latest DB snapshot.
DEFAULT_SIGMA_15MIN: dict[str, Decimal] = {
    "btc": Decimal("0.00232"),   # 0.232% per 15-min  (≈ 43% annualized)
    "eth": Decimal("0.00310"),   # 0.310% per 15-min  (≈ 58% annualized)
    "sol": Decimal("0.00312"),   # 0.312% per 15-min  (≈ 58% annualized)
}


# Supported Kalshi comparators for up-down markets. `at_least` and
# `greater_or_equal` and `above` all mean "BRTI at close ≥ prior avg".
# For a continuous distribution `above` and `at_least` are identical.
_UP_COMPARATORS = frozenset({"above", "at_least", "greater_or_equal", "ge", "gt"})
_DOWN_COMPARATORS = frozenset({"below", "less_or_equal", "le", "lt", "less_than"})


# ---------------------------------------------------------------------------
# Pure math helpers.
# ---------------------------------------------------------------------------

def _norm_cdf(x: Decimal) -> Decimal:
    """Standard normal CDF, via `math.erf`. Decimal in, Decimal out."""
    return Decimal(str(0.5 * (1.0 + math.erf(float(x) / math.sqrt(2.0)))))


def sigma_over_horizon(sigma_15min: Decimal, time_remaining_s: Decimal) -> Decimal:
    """σ_15min scaled by √(time_remaining / 900)."""
    if time_remaining_s <= 0:
        return Decimal("0")
    if time_remaining_s >= WINDOW_SECONDS:
        return sigma_15min
    ratio = Decimal(str(math.sqrt(float(time_remaining_s) / float(WINDOW_SECONDS))))
    return sigma_15min * ratio


def prob_return_nonneg(
    *,
    log_return_observed: Decimal,
    sigma_remaining: Decimal,
) -> Decimal:
    """P(log_return_close ≥ 0 | observed, σ_remaining) under N(0, σ²)."""
    if sigma_remaining <= 0:
        return ONE if log_return_observed >= 0 else ZERO
    return _norm_cdf(log_return_observed / sigma_remaining)


# ---------------------------------------------------------------------------
# FairValueModel
# ---------------------------------------------------------------------------

@dataclass
class FairValueModel:
    """Pricer for Kalshi `/bitcoin-price-up-down/` markets.

    Configure `sigma_15min_by_asset` with values from
    `scripts/calibrate_sigma.py`. `no_data_haircut` subtracts a fixed
    probability for the CF Benchmarks outage tail (CRYPTO15M.pdf §0.5).
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
        reference_price: Decimal,
        reference_60s_avg: Decimal,     # kept in signature for caller compatibility
        time_remaining_s: Decimal,
    ) -> tuple[Decimal, Decimal]:
        """Return (p_yes, ci_width).

        `strike` is the prior window's 60s-avg (= `floor_strike` in Kalshi's
        response). `reference_price` is the best current estimate of the
        asset's spot (not including the averaging smoothing — the caller
        typically passes the latest reference tick). `reference_60s_avg` is
        currently unused but retained so the strategy / evaluator signatures
        don't need to change.
        """
        comp = comparator.lower()
        if comp in ("between", "exactly"):
            raise NotImplementedError(
                f"comparator={comparator!r} needs a secondary strike which "
                f"MarketQuote doesn't currently carry — add `strike_high` "
                f"before enabling these markets."
            )
        if comp not in _UP_COMPARATORS and comp not in _DOWN_COMPARATORS:
            raise ValueError(f"unsupported comparator: {comparator!r}")

        strike = Decimal(str(strike))
        spot = Decimal(str(reference_price))
        tr = Decimal(str(time_remaining_s))

        sigma_full = self._sigma(asset)
        sigma_remaining = sigma_over_horizon(sigma_full, tr)
        if sigma_remaining < self.min_sigma:
            sigma_remaining = self.min_sigma

        if strike <= 0 or spot <= 0:
            return ZERO, ZERO

        # Observed partial log-return from prior-window avg to current spot.
        log_r_observed = Decimal(str(math.log(float(spot) / float(strike))))
        p_up = prob_return_nonneg(
            log_return_observed=log_r_observed,
            sigma_remaining=sigma_remaining,
        )
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

    def calibrate_from_returns(self, *, asset: str, returns: Iterable[Decimal]) -> Decimal:
        """Fit σ_15min for an asset from a sequence of realized log-returns.

        Mutates `sigma_15min_by_asset` in place and returns the new σ.
        """
        xs = [Decimal(str(r)) for r in returns]
        if len(xs) < 2:
            return self._sigma(asset)
        mean = sum(xs, Decimal("0")) / Decimal(len(xs))
        var = sum((x - mean) ** 2 for x in xs) / Decimal(len(xs))
        sigma = Decimal(str(math.sqrt(float(var))))
        self.sigma_15min_by_asset[asset.lower()] = sigma
        return sigma

    # ---- internals ----

    def _sigma(self, asset: str) -> Decimal:
        key = asset.lower()
        if key in self.sigma_15min_by_asset:
            return self.sigma_15min_by_asset[key]
        if key in DEFAULT_SIGMA_15MIN:
            return DEFAULT_SIGMA_15MIN[key]
        return DEFAULT_SIGMA_15MIN.get("btc", Decimal("0.002"))

    @staticmethod
    def _ci_width(p_yes: Decimal) -> Decimal:
        """Bernoulli-style CI proxy: 2·√(p·(1-p)).

        Near-zero when p is near 0 or 1 (confident), up to 1.0 at p = 0.5.
        """
        q = p_yes * (ONE - p_yes)
        return Decimal(str(2.0 * math.sqrt(float(q)))) if q > 0 else ZERO


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
    max_ci_width: Decimal = Decimal("0.50")
    min_book_depth_usd: Decimal = Decimal("200")
    # Phase-1 window gate: score any point within the 15-min window by default.
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

        tr = quote.time_remaining_s
        lo, hi = cfg.time_window_seconds
        if not (Decimal(str(lo)) <= tr < Decimal(str(hi))):
            return None

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
