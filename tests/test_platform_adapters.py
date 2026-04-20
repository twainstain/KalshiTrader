"""Cover `src/platform_adapters.py` — re-exports + Kalshi-flavored wrappers."""

from __future__ import annotations

import pytest

import platform_adapters as pa


def test_reexports_present():
    # Generic primitives accessible without importing trading_platform directly.
    assert pa.RetryPolicy is not None
    assert pa.RetryResult is not None
    assert callable(pa.execute_with_retry)
    assert callable(pa.config_hash)
    assert pa.PriorityQueue is not None
    assert pa.QueuedItem is not None
    assert pa.BreakerState is not None


def test_kalshi_api_error_carries_status_and_body():
    err = pa.KalshiAPIError("boom", status=429, response_body='{"error":"rate_limited"}')
    assert str(err) == "boom"
    assert err.status == 429
    assert err.response_body == '{"error":"rate_limited"}'
    # Transport-layer errors (no HTTP response) are distinguishable.
    nonet = pa.KalshiAPIError("ws handshake failed")
    assert nonet.status is None
    assert nonet.response_body == ""


def test_circuit_breaker_config_maps_kalshi_fields_to_platform():
    cfg = pa.CircuitBreakerConfig(
        max_api_errors=7, api_error_window_seconds=90.0,
        max_stale_book_seconds=15.0,
        max_order_rejects=4, reject_window_seconds=120.0,
        cooldown_seconds=60.0,
    )
    plat = cfg._to_platform()
    assert plat.max_errors == 7
    assert plat.error_window_seconds == 90.0
    assert plat.max_stale_seconds == 15.0
    assert plat.max_failures == 4
    assert plat.failure_window_seconds == 120.0
    assert plat.cooldown_seconds == 60.0


def test_circuit_breaker_default_state_allows_execution():
    brk = pa.CircuitBreaker()
    allowed, reason = brk.allows_execution()
    assert allowed is True
    assert reason == ""
    assert brk.state == pa.BreakerState.CLOSED


def test_circuit_breaker_trips_on_repeated_api_errors():
    cfg = pa.CircuitBreakerConfig(max_api_errors=2, api_error_window_seconds=60.0,
                                  cooldown_seconds=60.0)
    brk = pa.CircuitBreaker(cfg)
    brk.record_api_error()
    assert brk.allows_execution()[0] is True
    brk.record_api_error()
    # Two errors in window → trip.
    allowed, _ = brk.allows_execution()
    assert allowed is False
    assert brk.state == pa.BreakerState.OPEN


def test_circuit_breaker_record_success_and_fresh_book_callable():
    brk = pa.CircuitBreaker()
    brk.record_success()
    brk.record_fresh_book()
    # No assertions beyond "don't raise" — these are thin pass-throughs and
    # the platform breaker owns the state machine.
    allowed, _ = brk.allows_execution()
    assert allowed is True


def test_priority_queue_basic_push_pop():
    q = pa.PriorityQueue(max_size=10)
    q.push("low", priority=1.0)
    q.push("high", priority=9.0)
    popped = q.pop()
    assert popped.item == "high"
