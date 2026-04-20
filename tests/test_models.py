"""Cover the P1-M0 domain models."""

from __future__ import annotations

from decimal import Decimal

import pytest

from core.models import (
    BPS_DIVISOR,
    ExecutionResult,
    MarketQuote,
    ONE,
    Opportunity,
    OpportunityStatus,
    SUPPORTED_COMPARATORS,
    SUPPORTED_VENUES,
    ZERO,
)


def _quote(**overrides) -> MarketQuote:
    base = dict(
        venue="kalshi",
        market_ticker="KXBTC15M-26APR19-1015-ABOVE-65000",
        series_ticker="KXBTC15M",
        event_ticker="KXBTC15M-26APR19-1015",
        best_yes_ask=Decimal("0.4200"),
        best_no_ask=Decimal("0.5801"),
        best_yes_bid=Decimal("0.4199"),
        best_no_bid=Decimal("0.5800"),
        book_depth_yes_usd=Decimal("1500"),
        book_depth_no_usd=Decimal("1800"),
        fee_bps=Decimal("35"),
        expiration_ts=Decimal("1746000000"),
        strike=Decimal("65000"),
        comparator="above",
        reference_price=Decimal("64999.50"),
        reference_60s_avg=Decimal("64995.10"),
        time_remaining_s=Decimal("45"),
        quote_timestamp_us=1_746_000_000_000_000,
    )
    base.update(overrides)
    return MarketQuote(**base)


# ---- constants ----

def test_supported_venues_is_kalshi_only():
    assert SUPPORTED_VENUES == ("kalshi",)


def test_supported_comparators_matches_plan():
    assert set(SUPPORTED_COMPARATORS) == {"above", "below", "between", "exactly", "at_least"}


def test_bps_divisor_and_one_constants():
    assert BPS_DIVISOR == Decimal("10000")
    assert ONE == Decimal("1")
    assert ZERO == Decimal("0")


# ---- MarketQuote ----

def test_marketquote_happy_path_preserves_decimal():
    q = _quote()
    assert isinstance(q.best_yes_ask, Decimal)
    assert q.best_yes_ask == Decimal("0.4200")
    assert q.fee_included is False  # ground rule §1
    assert q.venue == "kalshi"
    assert q.warning_flags == ()
    assert q.raw == {}


def test_marketquote_coerces_float_inputs_via_str():
    # Tests + replay fixtures may pass floats; __post_init__ coerces.
    q = _quote(best_yes_ask=0.42, strike=65000)
    assert isinstance(q.best_yes_ask, Decimal)
    assert q.best_yes_ask == Decimal("0.42")  # str() path — no IEEE-754 noise
    assert isinstance(q.strike, Decimal)
    assert q.strike == Decimal("65000")


def test_marketquote_rejects_non_kalshi_venue():
    with pytest.raises(ValueError, match="venue="):
        _quote(venue="polymarket")


def test_marketquote_rejects_fee_included_true():
    with pytest.raises(ValueError, match="fee_included"):
        _quote(fee_included=True)


def test_marketquote_rejects_unknown_comparator():
    with pytest.raises(ValueError, match="comparator="):
        _quote(comparator="bogus")


def test_marketquote_is_frozen():
    q = _quote()
    with pytest.raises((AttributeError, Exception)):
        q.best_yes_ask = Decimal("0.99")  # type: ignore[misc]


def test_marketquote_quote_timestamp_us_is_int_not_decimal():
    q = _quote()
    assert isinstance(q.quote_timestamp_us, int)
    assert q.quote_timestamp_us == 1_746_000_000_000_000


def test_marketquote_accepts_warning_flags_tuple():
    q = _quote(warning_flags=("stale_book", "cf_reference_degraded"))
    assert q.warning_flags == ("stale_book", "cf_reference_degraded")


# ---- Opportunity ----

def test_opportunity_happy_path():
    q = _quote()
    opp = Opportunity(
        quote=q,
        p_yes=Decimal("0.48"),
        ci_width=Decimal("0.08"),
        recommended_side="yes",
        hypothetical_fill_price=Decimal("0.42"),
        hypothetical_size_contracts=Decimal("50"),
        expected_edge_bps_after_fees=Decimal("120"),
    )
    assert opp.status == OpportunityStatus.DETECTED
    assert opp.no_data_haircut_bps == ZERO
    assert opp.quote is q  # nested object not coerced


def test_opportunity_rejects_bad_side():
    q = _quote()
    with pytest.raises(ValueError, match="recommended_side"):
        Opportunity(
            quote=q,
            p_yes=Decimal("0.5"),
            ci_width=Decimal("0.1"),
            recommended_side="maybe",
            hypothetical_fill_price=Decimal("0.5"),
            hypothetical_size_contracts=Decimal("1"),
            expected_edge_bps_after_fees=Decimal("0"),
        )


def test_opportunity_status_enum_values():
    # Each status is a str so JSON serialization + DB writes are trivial.
    assert OpportunityStatus.DETECTED == "detected"
    assert OpportunityStatus.APPROVED == "approved"
    assert OpportunityStatus.SIMULATION_APPROVED.value == "simulation_approved"
    # P2 statuses present but unused in P1.
    assert OpportunityStatus.SUBMITTED == "submitted"
    assert OpportunityStatus.INCLUDED == "included"


# ---- ExecutionResult ----

def test_execution_result_happy_path():
    q = _quote()
    opp = Opportunity(
        quote=q,
        p_yes=Decimal("0.48"),
        ci_width=Decimal("0.08"),
        recommended_side="yes",
        hypothetical_fill_price=Decimal("0.42"),
        hypothetical_size_contracts=Decimal("50"),
        expected_edge_bps_after_fees=Decimal("120"),
    )
    r = ExecutionResult(
        success=True,
        reason="filled",
        realized_pnl_usd=Decimal("12.34"),
        opportunity=opp,
    )
    assert r.success is True
    assert r.reason == "filled"
    assert r.realized_pnl_usd == Decimal("12.34")
    assert r.opportunity is opp


def test_execution_result_coerces_int_pnl():
    q = _quote()
    opp = Opportunity(
        quote=q, p_yes=Decimal("0.5"), ci_width=Decimal("0.1"),
        recommended_side="none",
        hypothetical_fill_price=Decimal("0"),
        hypothetical_size_contracts=Decimal("0"),
        expected_edge_bps_after_fees=Decimal("0"),
    )
    r = ExecutionResult(success=False, reason="rejected",
                       realized_pnl_usd=0, opportunity=opp)
    assert isinstance(r.realized_pnl_usd, Decimal)
    assert r.realized_pnl_usd == ZERO
