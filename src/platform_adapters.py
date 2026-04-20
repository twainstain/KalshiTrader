"""Kalshi-flavored adapters over `lib/trading_platform` primitives.

Domain code imports from this module instead of `trading_platform.*` directly,
per execution plan §1 / §3. The adapters rename generic primitives to Kalshi
terms (e.g. `record_api_error` on the breaker) and add a local
`KalshiAPIError` used by `KalshiClient` wrappers for 4xx/5xx surfaces.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from trading_platform.pipeline.queue import PriorityQueue, QueuedItem
from trading_platform.risk.circuit_breaker import (
    BreakerState,
    CircuitBreaker as _PlatformBreaker,
    CircuitBreakerConfig as _PlatformBreakerConfig,
)
from trading_platform.risk.retry import (
    RetryPolicy,
    RetryResult,
    config_hash,
    execute_with_retry,
)


__all__ = [
    "CircuitBreaker",
    "CircuitBreakerConfig",
    "BreakerState",
    "RetryPolicy",
    "RetryResult",
    "config_hash",
    "execute_with_retry",
    "PriorityQueue",
    "QueuedItem",
    "KalshiAPIError",
]


class KalshiAPIError(Exception):
    """Raised by `KalshiClient` wrappers on 4xx/5xx. Carries HTTP status.

    Status `None` means the request never reached Kalshi (connection error,
    WS handshake failure, timeout). Callers distinguish transient vs terminal
    by inspecting `status` + `response_body`.
    """

    def __init__(self, message: str, *, status: int | None = None,
                 response_body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.response_body = response_body


@dataclass
class CircuitBreakerConfig:
    """Kalshi-flavored config — thresholds are book-feed-centric.

    Kalshi term               → Platform term
    max_api_errors            → max_errors
    api_error_window_seconds  → error_window_seconds
    max_stale_book_seconds    → max_stale_seconds
    max_order_rejects         → max_failures (P2)
    reject_window_seconds     → failure_window_seconds (P2)
    """
    max_api_errors: int = 5
    api_error_window_seconds: float = 60.0
    max_stale_book_seconds: float = 30.0
    max_order_rejects: int = 3
    reject_window_seconds: float = 300.0
    cooldown_seconds: float = 300.0

    def _to_platform(self) -> _PlatformBreakerConfig:
        return _PlatformBreakerConfig(
            max_failures=self.max_order_rejects,
            failure_window_seconds=self.reject_window_seconds,
            max_errors=self.max_api_errors,
            error_window_seconds=self.api_error_window_seconds,
            max_stale_seconds=self.max_stale_book_seconds,
            cooldown_seconds=self.cooldown_seconds,
        )


class CircuitBreaker:
    """Wraps the platform breaker with Kalshi-specific method names.

    Kalshi method              → Platform method
    record_order_reject()      → record_failure()
    record_api_error()         → record_error()
    record_fresh_book()        → record_fresh_data()
    allows_execution()         → (not should_block(), trip_reason)
    """

    def __init__(self, config: CircuitBreakerConfig | None = None) -> None:
        self._config = config or CircuitBreakerConfig()
        self._breaker = _PlatformBreaker(self._config._to_platform())

    def record_order_reject(self) -> None:
        self._breaker.record_failure()

    def record_api_error(self) -> None:
        self._breaker.record_error()

    def record_fresh_book(self) -> None:
        self._breaker.record_fresh_data()

    def record_success(self) -> None:
        self._breaker.record_success()

    def allows_execution(self) -> tuple[bool, str]:
        blocked = self._breaker.should_block()
        reason = ""
        if blocked:
            reason = getattr(self._breaker, "last_trip_reason", "") or "blocked"
        return (not blocked, reason)

    @property
    def state(self) -> BreakerState:
        return self._breaker.state
