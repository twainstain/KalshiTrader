"""Kalshi market source — REST discovery + WebSocket orderbook feed.

Emits `MarketQuote` objects built from Kalshi L1+L2 book state, with
Kalshi-specific fields (expiration_ts, strike, comparator, reference_price,
reference_60s_avg, time_remaining_s) populated from live feeds.

Tasks covered (implementation-tasks P1-M1):
- T01 Class skeleton with start/stop/get_quotes/is_healthy.
- T02 `make_client()` lazy-imports `kalshi_python_sync.KalshiClient` from env.
- T03 `discover_active_crypto_markets()` queries `/series` + `/markets`.
- T04 WS subscription loop scaffold (signed handshake + book state).
- T05 `book_to_market_quote()` pure mapping (the main testable surface).
- T06 `lifecycle_tag()` pure mapping from Kalshi market status.
- T07 Retry via `RetryPolicy`, `warning_flags=("stale_book",)`, 429 pacing.

The live validation (client.get_balance() on demo) is gated on P-02 and
runs as an integration smoke — unit tests mock the client.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from core.models import MarketQuote, SUPPORTED_COMPARATORS
from platform_adapters import (
    CircuitBreaker,
    CircuitBreakerConfig,
    KalshiAPIError,
    RetryPolicy,
)


logger = logging.getLogger(__name__)


# Kalshi's two prod + demo REST endpoints.
REST_HOSTS = {
    "prod": "https://api.elections.kalshi.com/trade-api/v2",
    "demo": "https://demo-api.kalshi.co/trade-api/v2",
}
WS_HOSTS = {
    "prod": "wss://api.elections.kalshi.com/orderbook_delta",
    # Demo WS URL is not documented as of 2026-04-19 — leave blank so the
    # code fails loudly if someone tries WS on demo before we confirm it.
    "demo": "",
}

# CRYPTO15M series tickers per CLAUDE.md / strategy plan. Verified at runtime
# against `/series?category=crypto` before use (see discover_active_crypto_markets).
EXPECTED_CRYPTO_SERIES: tuple[str, ...] = (
    "KXBTC15M",
    "KXETH15M",
    "KXSOL15M",
)


# ---------------------------------------------------------------------------
# Pure helpers — zero external deps, trivially testable.
# ---------------------------------------------------------------------------

def parse_dollar_string(s: str | Decimal | int | float) -> Decimal:
    """Parse a Kalshi dollar string like "0.4200" to Decimal.

    Kalshi uses 4-decimal strings on the book. Passing a non-string coerces
    via `str()` first to preserve the 4-decimal precision and avoid IEEE-754.
    """
    if isinstance(s, Decimal):
        return s
    return Decimal(str(s))


def book_depth_usd(side: Sequence[Sequence[Any]], levels: int = 5) -> Decimal:
    """Sum of price×qty over the top `levels` entries of a book side.

    Kalshi book arrays are sorted ascending; the last element is the best
    bid. We take the top `levels` entries from the end of the array (the
    most-competitive bids).

    The semantic is "resting-order notional on this side", NOT "liquidity
    available to buy the OTHER side". A trader buying YES must cross NO
    bids — measure that by passing the NO bids as `side`.
    """
    if not side:
        return Decimal("0")
    top = side[-levels:]
    total = Decimal("0")
    for entry in top:
        if len(entry) < 2:
            continue
        price = parse_dollar_string(entry[0])
        qty = parse_dollar_string(entry[1])
        total += price * qty
    return total


def _best_level_price(side: Sequence[Sequence[Any]]) -> Decimal:
    """Return the best (highest) bid price on a side, or 0 if empty."""
    if not side:
        return Decimal("0")
    return parse_dollar_string(side[-1][0])


def book_to_market_quote(
    *,
    book: dict,
    market_ticker: str,
    series_ticker: str,
    event_ticker: str,
    strike: Decimal | str | int | float,
    comparator: str,
    expiration_ts: Decimal | int | float,
    time_remaining_s: Decimal | int | float,
    reference_price: Decimal | str | int | float,
    reference_60s_avg: Decimal | str | int | float,
    fee_bps: Decimal | str | int | float,
    quote_timestamp_us: int,
    depth_levels: int = 5,
    warning_flags: tuple[str, ...] = (),
) -> MarketQuote:
    """Map a Kalshi book snapshot + context to a `MarketQuote`.

    Book shape (per Kalshi docs §3.4):
        {"yes": [[price, qty], ...], "no": [[price, qty], ...]}
    Both arrays ascend; last element = best bid. Binary markets store only
    bids — asks are derived: YES_ask = 1 − NO_bid, NO_ask = 1 − YES_bid.
    """
    yes_side = book.get("yes", []) or []
    no_side = book.get("no", []) or []

    best_yes_bid = _best_level_price(yes_side)
    best_no_bid = _best_level_price(no_side)

    # When a side is empty, derived ask becomes 1.0 (trivially un-fillable).
    one = Decimal("1")
    best_yes_ask = one - best_no_bid if no_side else one
    best_no_ask = one - best_yes_bid if yes_side else one

    return MarketQuote(
        venue="kalshi",
        market_ticker=market_ticker,
        series_ticker=series_ticker,
        event_ticker=event_ticker,
        best_yes_ask=best_yes_ask,
        best_no_ask=best_no_ask,
        best_yes_bid=best_yes_bid,
        best_no_bid=best_no_bid,
        book_depth_yes_usd=book_depth_usd(yes_side, depth_levels),
        book_depth_no_usd=book_depth_usd(no_side, depth_levels),
        fee_bps=parse_dollar_string(fee_bps),
        expiration_ts=parse_dollar_string(expiration_ts),
        strike=parse_dollar_string(strike),
        comparator=comparator,
        reference_price=parse_dollar_string(reference_price),
        reference_60s_avg=parse_dollar_string(reference_60s_avg),
        time_remaining_s=parse_dollar_string(time_remaining_s),
        quote_timestamp_us=int(quote_timestamp_us),
        fee_included=False,
        warning_flags=warning_flags,
        raw={"book": book},
    )


def lifecycle_tag(status: str, time_remaining_s: float) -> str:
    """Bucket a Kalshi market into a lifecycle tag used by the strategy.

    Mapping per CLAUDE.md + Kalshi docs §3.6:
        initialized                            → opening
        active + time_remaining_s > 60         → active
        active + 0 < time_remaining_s <= 60    → final_minute
        active + time_remaining_s <= 0         → closed  (race window)
        closed / inactive                      → closed
        determined / disputed / amended / finalized → settled

    Unknown statuses map to "active" so the scanner doesn't silently drop
    markets — it logs a warning upstream and keeps quoting.
    """
    s = (status or "").lower()
    if s == "initialized":
        return "opening"
    if s == "active":
        try:
            remaining = float(time_remaining_s)
        except (TypeError, ValueError):
            return "active"
        if remaining <= 0:
            return "closed"
        if remaining <= 60:
            return "final_minute"
        return "active"
    if s in ("closed", "inactive"):
        return "closed"
    if s in ("determined", "disputed", "amended", "finalized", "settled"):
        return "settled"
    return "active"


# ---------------------------------------------------------------------------
# Client + discovery (T02, T03) — network-touching, unit-tested via mocks.
# ---------------------------------------------------------------------------

def _load_pem(path: Path) -> bytes:
    return path.read_bytes()


def make_client(
    *,
    env: str | None = None,
    api_key_id: str | None = None,
    private_key_path: str | Path | None = None,
    client_factory: Callable[..., Any] | None = None,
) -> Any:
    """Build a `KalshiClient` from env or explicit args.

    The SDK is imported lazily so unit tests (and `pytest tests/` without
    `pip install -e .`) don't need it on disk. `client_factory` is the
    override seam for tests — when set, skips the import entirely.
    """
    import os

    env = env or os.environ.get("KALSHI_ENV", "demo")
    if env not in REST_HOSTS:
        raise ValueError(f"KALSHI_ENV={env!r} must be one of {tuple(REST_HOSTS)}")

    key_id = api_key_id or os.environ.get("KALSHI_API_KEY_ID", "")
    if not key_id:
        raise RuntimeError("KALSHI_API_KEY_ID is unset — see .env.example")

    pem_path = Path(private_key_path or os.environ.get("KALSHI_PRIVATE_KEY_PATH", ""))
    if not str(pem_path) or not pem_path.is_file():
        raise RuntimeError(
            f"KALSHI_PRIVATE_KEY_PATH does not resolve to a file: {pem_path!r}"
        )

    pem_bytes = _load_pem(pem_path)
    host = REST_HOSTS[env]

    if client_factory is None:
        try:
            from kalshi_python_sync import KalshiClient, Configuration  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "kalshi_python_sync is not installed — run `pip install -e .`"
            ) from e
        cfg = Configuration(host=host, api_key_id=key_id, private_key_pem=pem_bytes)
        return KalshiClient(cfg)

    return client_factory(host=host, api_key_id=key_id, private_key_pem=pem_bytes)


def discover_active_crypto_markets(
    client: Any,
    *,
    expected_series: Iterable[str] = EXPECTED_CRYPTO_SERIES,
) -> list[dict]:
    """Query `/series?category=crypto` + `/markets?status=active` per series.

    Returns Kalshi's raw market dicts (no translation) for caller-side
    flexibility. Series filter is defensive: we log when our expected
    tickers diverge from what Kalshi advertises so resolution-rule
    assumptions stay honest (per CLAUDE.md load-bearing finding #1).
    """
    series_resp = _call(client, "get_series", params={"category": "crypto"})
    advertised: set[str] = set()
    for s in (series_resp.get("series") if isinstance(series_resp, dict) else []) or []:
        ticker = s.get("ticker") if isinstance(s, dict) else None
        if ticker:
            advertised.add(ticker)

    expected = set(expected_series)
    missing = expected - advertised
    surprises = advertised - expected
    if missing:
        logger.warning("expected series missing from Kalshi: %s", sorted(missing))
    if surprises:
        logger.info("additional crypto series on Kalshi: %s", sorted(surprises))

    markets: list[dict] = []
    for series in sorted(expected & advertised) or sorted(expected):
        resp = _call(
            client,
            "get_markets",
            params={"series_ticker": series, "status": "active"},
        )
        for m in (resp.get("markets") if isinstance(resp, dict) else []) or []:
            markets.append(m)
    return markets


def _call(client: Any, method_name: str, **kwargs) -> Any:
    """Best-effort attribute resolution on `KalshiClient`.

    The SDK exposes method names that sometimes don't match REST path (e.g.
    `get_series` vs `list_series`). We try the canonical name first, then
    common variants, then fall back to raising `KalshiAPIError` so callers
    get a consistent exception type.
    """
    for attr in (method_name, method_name.replace("get_", "list_")):
        fn = getattr(client, attr, None)
        if callable(fn):
            try:
                return fn(**(kwargs.get("params") or {}))
            except Exception as e:  # surface all SDK errors uniformly
                raise KalshiAPIError(
                    f"{attr} failed: {e}", status=getattr(e, "status", None),
                    response_body=str(e),
                ) from e
    raise KalshiAPIError(
        f"KalshiClient has no attribute {method_name!r} or variants",
        status=None,
    )


# ---------------------------------------------------------------------------
# KalshiMarketSource — T01 skeleton + T04 WS scaffold + T07 failure modes.
# ---------------------------------------------------------------------------

@dataclass
class KalshiMarketConfig:
    env: str = "demo"
    depth_levels: int = 5
    stale_book_seconds: float = 3.0
    reconnect_backoff_initial_s: float = 1.0
    reconnect_backoff_max_s: float = 60.0
    rate_limit_rps_read: int = 20
    rate_limit_rps_write: int = 10


@dataclass
class _BookEntry:
    """Per-ticker cached state."""
    book: dict = field(default_factory=lambda: {"yes": [], "no": []})
    last_update_us: int = 0
    seq: int = 0
    status: str = "active"
    time_remaining_s: float = 0.0


class KalshiMarketSource:
    """Read-only Kalshi market feed for Phase 1.

    Start/stop manages a WS connection thread and an in-memory book cache.
    `get_quotes()` returns the latest `MarketQuote` list built from cached
    state plus a reference-price snapshot passed by the caller. `is_healthy()`
    reports WS connected + books fresh + circuit-breaker closed.

    The actual WS loop is scaffolded: `_ws_loop()` holds the subscribe/consume
    logic guarded by `RetryPolicy` for reconnects, but the live handshake
    against Kalshi is gated on P-02 (demo key) and covered only by an
    integration smoke test. Unit tests exercise the public surface with a
    fake WS driver.
    """

    def __init__(
        self,
        config: KalshiMarketConfig | None = None,
        *,
        client: Any | None = None,
        retry_policy: RetryPolicy | None = None,
        breaker: CircuitBreaker | None = None,
        now_us: Callable[[], int] | None = None,
    ) -> None:
        self._config = config or KalshiMarketConfig()
        self._client = client
        self._retry_policy = retry_policy or RetryPolicy(
            max_retries=5, delay_seconds=self._config.reconnect_backoff_initial_s,
            require_re_evaluation=False,
        )
        self._breaker = breaker or CircuitBreaker(
            CircuitBreakerConfig(
                max_stale_book_seconds=self._config.stale_book_seconds,
            )
        )
        self._now_us = now_us or (lambda: int(time.time() * 1_000_000))
        self._books: dict[str, _BookEntry] = {}
        self._books_lock = threading.Lock()
        self._ws_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._connected = False

    # ---- public surface ----

    def start(self) -> None:
        if self._ws_thread and self._ws_thread.is_alive():
            return
        self._stop_event.clear()
        self._ws_thread = threading.Thread(
            target=self._ws_loop, name="kalshi-ws", daemon=True,
        )
        self._ws_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._ws_thread:
            self._ws_thread.join(timeout=5.0)
        self._connected = False

    def is_healthy(self) -> bool:
        if not self._connected:
            return False
        allowed, _ = self._breaker.allows_execution()
        if not allowed:
            return False
        if not self._has_fresh_book():
            return False
        return True

    def get_quotes(
        self,
        *,
        reference_price_by_asset: dict[str, Decimal],
        reference_60s_avg_by_asset: dict[str, Decimal],
        fee_bps_by_ticker: dict[str, Decimal],
        market_meta_by_ticker: dict[str, dict],
    ) -> list[MarketQuote]:
        """Snapshot all cached books as `MarketQuote`s.

        Callers supply the reference prices + fee schedule + per-market
        metadata (strike, comparator, expiration_ts) out-of-band because
        `KalshiMarketSource` is book-only by design — the strategy composes
        references from `CryptoReferenceSource`.
        """
        out: list[MarketQuote] = []
        now_us = self._now_us()
        with self._books_lock:
            entries = list(self._books.items())
        for ticker, entry in entries:
            meta = market_meta_by_ticker.get(ticker)
            if not meta:
                continue
            asset = meta.get("asset", "btc").lower()
            warnings = self._freshness_warnings(entry.last_update_us, now_us)
            q = book_to_market_quote(
                book=entry.book,
                market_ticker=ticker,
                series_ticker=meta["series_ticker"],
                event_ticker=meta["event_ticker"],
                strike=meta["strike"],
                comparator=meta["comparator"],
                expiration_ts=meta["expiration_ts"],
                time_remaining_s=entry.time_remaining_s,
                reference_price=reference_price_by_asset.get(asset, Decimal("0")),
                reference_60s_avg=reference_60s_avg_by_asset.get(asset, Decimal("0")),
                fee_bps=fee_bps_by_ticker.get(ticker, Decimal("0")),
                quote_timestamp_us=now_us,
                depth_levels=self._config.depth_levels,
                warning_flags=warnings,
            )
            if meta["comparator"] not in SUPPORTED_COMPARATORS:
                continue
            out.append(q)
        return out

    # ---- internals (exposed for tests) ----

    def _freshness_warnings(self, last_update_us: int, now_us: int) -> tuple[str, ...]:
        if last_update_us <= 0:
            return ("stale_book",)
        age_s = (now_us - last_update_us) / 1_000_000
        if age_s > self._config.stale_book_seconds:
            return ("stale_book",)
        return ()

    def _has_fresh_book(self) -> bool:
        if not self._books:
            return False
        now_us = self._now_us()
        with self._books_lock:
            ages_us = [now_us - e.last_update_us for e in self._books.values()]
        if not ages_us:
            return False
        max_age_s = max(ages_us) / 1_000_000
        return max_age_s <= self._config.stale_book_seconds

    def apply_snapshot(self, ticker: str, book: dict, *, seq: int = 0) -> None:
        """Test-visible seam: replace a ticker's full book."""
        with self._books_lock:
            entry = self._books.setdefault(ticker, _BookEntry())
            entry.book = {"yes": list(book.get("yes", [])),
                          "no":  list(book.get("no", []))}
            entry.seq = seq
            entry.last_update_us = self._now_us()

    def apply_delta(self, ticker: str, side: str, price: str, qty: str,
                    *, seq: int = 0) -> None:
        """Test-visible seam: update a single level (price=qty; 0 removes).

        Live WS delivers a `delta` stream; this method applies one entry.
        Not used in production yet — the WS loop will call it once live.
        """
        if side not in ("yes", "no"):
            raise ValueError(f"side must be yes|no, got {side!r}")
        with self._books_lock:
            entry = self._books.setdefault(ticker, _BookEntry())
            levels = [list(e) for e in entry.book.get(side, [])]
            price_s = str(price)
            qty_d = parse_dollar_string(qty)
            # Remove existing level at this price.
            levels = [l for l in levels if parse_dollar_string(l[0]) != parse_dollar_string(price_s)]
            if qty_d > 0:
                levels.append([price_s, str(qty)])
                levels.sort(key=lambda l: parse_dollar_string(l[0]))
            entry.book[side] = levels
            entry.seq = seq
            entry.last_update_us = self._now_us()

    def update_lifecycle(self, ticker: str, *, status: str,
                         time_remaining_s: float) -> None:
        with self._books_lock:
            entry = self._books.setdefault(ticker, _BookEntry())
            entry.status = status
            entry.time_remaining_s = time_remaining_s

    # ---- WS loop (scaffold) ----

    def _ws_loop(self) -> None:
        """Reconnect-with-backoff WS loop.

        Actual WS handshake + message parsing is left as a TODO tied to
        P1-M1-T04 integration smoke once a demo key is provisioned. The
        loop structure — backoff, breaker integration, stop signaling —
        is in place so drop-in WS plumbing stays small.
        """
        backoff = self._config.reconnect_backoff_initial_s
        while not self._stop_event.is_set():
            try:
                allowed, reason = self._breaker.allows_execution()
                if not allowed:
                    logger.warning("breaker tripped (%s); pausing WS loop", reason)
                    self._stop_event.wait(self._config.reconnect_backoff_max_s)
                    continue

                self._connected = True
                self._breaker.record_fresh_book()
                # Placeholder for: ws = await connect(WS_HOSTS[env], headers=...)
                # while not stop_event: consume messages, apply_delta(), etc.
                # Exits only when `stop()` fires or exception thrown.
                if self._stop_event.wait(1.0):
                    break

            except Exception as e:  # pragma: no cover — live-path only
                self._connected = False
                self._breaker.record_api_error()
                logger.exception("WS loop error; reconnecting in %.1fs", backoff)
                if self._stop_event.wait(backoff):
                    break
                backoff = min(backoff * 2, self._config.reconnect_backoff_max_s)
            else:
                backoff = self._config.reconnect_backoff_initial_s

        self._connected = False
