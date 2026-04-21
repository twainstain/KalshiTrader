"""Cover `scripts/backtest_three_models.py` — focused on the P/L scoring
function whose symmetry was broken prior to 2026-04-21.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

# The script uses a top-level `sys.path.insert` to load modules from `src/`.
# Importing it here triggers that side-effect, which is fine for tests.
from backtest_three_models import score_pnl, BPS


# A binary-contract P/L identity we want to hold regardless of side:
#
#   For every (fill_price, outcome) pair where side matches the outcome,
#   the scorer must return ((1 - fill_price) - fee). For the mismatch,
#   it must return (-fill_price - fee). That is, both sides have
#   symmetric P/L — the only asymmetry is which outcome counts as a win.
#
# Regression: the prior implementation used `(1 - fill_price)` as the NO
# cost basis, so buying NO at 0.60 scored roughly +0.60 on a win and −0.40
# on a loss (the P/L of YES-at-0.40, not NO-at-0.60). See code comment in
# `score_pnl`.


# ----------------------------------------------------------------------
# YES side — behavior unchanged by the 2026-04-21 fix (sanity check)
# ----------------------------------------------------------------------

def test_yes_win_profit():
    # Buy YES at $0.40; resolves YES → profit = 1 - 0.40 - fee
    pnl = score_pnl("yes", Decimal("0.40"), "yes", Decimal("0"))
    assert pnl == Decimal("0.60")


def test_yes_loss():
    # Buy YES at $0.40; resolves NO → loss = -0.40 - fee
    pnl = score_pnl("yes", Decimal("0.40"), "no", Decimal("0"))
    assert pnl == Decimal("-0.40")


# ----------------------------------------------------------------------
# NO side — this is the P1 regression the prior implementation got wrong
# ----------------------------------------------------------------------

def test_no_win_profit_is_one_minus_fill_price():
    """Regression (P1): buy NO at $0.60; resolves NO → profit = 1 - 0.60."""
    pnl = score_pnl("no", Decimal("0.60"), "no", Decimal("0"))
    assert pnl == Decimal("0.40")


def test_no_loss_is_negative_fill_price():
    """Regression (P1): buy NO at $0.60; resolves YES → loss = -0.60."""
    pnl = score_pnl("no", Decimal("0.60"), "yes", Decimal("0"))
    assert pnl == Decimal("-0.60")


# ----------------------------------------------------------------------
# Symmetry property across sides
# ----------------------------------------------------------------------

@pytest.mark.parametrize("p_str", ["0.05", "0.15", "0.40", "0.50",
                                   "0.60", "0.85", "0.95"])
@pytest.mark.parametrize("outcome", ["yes", "no"])
def test_pnl_complementary_trades_sum_to_zero(p_str, outcome):
    """Binary-market identity: buying YES at `p` and NO at `(1-p)` together
    cost $1 and pay $1 (one of them always wins), so their combined P/L
    must be exactly zero (ignoring fees).

    Prior to the 2026-04-21 fix, this didn't hold for NO — buying NO at
    0.60 was miscredited with the P/L profile of YES-at-0.40.
    """
    p = Decimal(p_str)
    fee_bps = Decimal("0")
    yes_pnl = score_pnl("yes", p, outcome, fee_bps)
    no_pnl = score_pnl("no", Decimal("1") - p, outcome, fee_bps)
    assert yes_pnl + no_pnl == Decimal("0")


# ----------------------------------------------------------------------
# Fee proportional to fill_price on both sides (regression: fee notional
# used to be (1 - fill_price) on NO, making NO fees incorrectly cheap for
# expensive NO contracts).
# ----------------------------------------------------------------------

def test_fee_is_proportional_to_fill_price_on_yes():
    pnl = score_pnl("yes", Decimal("0.40"), "yes", Decimal("100"))  # 1% fee
    # profit = 0.60 - 0.40 * 0.01 = 0.596
    assert pnl == Decimal("0.596")


def test_fee_is_proportional_to_fill_price_on_no():
    """Regression: fee notional on NO must use `fill_price` (not 1-fill)."""
    pnl = score_pnl("no", Decimal("0.60"), "no", Decimal("100"))  # 1% fee
    # profit = (1 - 0.60) - 0.60 * 0.01 = 0.394
    assert pnl == Decimal("0.394")


def test_expected_value_is_negative_near_efficient_price():
    """Sanity: at fair price (fill ≈ p(outcome)), EV should be ≈ -fee.
    Buy YES at 0.50 with fair prior 0.50 — half wins +0.50, half loses -0.50,
    fee is 0.005. EV = -0.005.
    """
    fee_bps = Decimal("100")  # 1%
    win = score_pnl("yes", Decimal("0.50"), "yes", fee_bps)
    loss = score_pnl("yes", Decimal("0.50"), "no", fee_bps)
    ev = (win + loss) / Decimal("2")
    # EV = ( (0.50 - 0.005) + (-0.50 - 0.005) ) / 2 = -0.005
    assert ev == Decimal("-0.005")


# ----------------------------------------------------------------------
# Smoke: BPS constant consistency
# ----------------------------------------------------------------------

def test_bps_constant_is_ten_thousand():
    assert BPS == Decimal("10000")
