"""Phase-1 shadow-evaluator entrypoint (P1-M4-T05).

Long-running loop that periodically:
- Discovers active Kalshi crypto 15M markets via `/series` + `/markets`.
- Fetches L1 + L2 orderbook snapshots per ticker via
  `GET /markets/{ticker}/orderbook`.
- Polls Coinbase for the current BTC/ETH/SOL spot price.
- Scores each book via `KalshiFairValueStrategy`.
- Writes every approvable decision to `shadow_decisions`.
- Every N ticks, attempts to reconcile markets whose expiration has passed
  + `reconcile_delay_s` (default 30s) — reading their `result` from
  `GET /markets/{ticker}` and updating the realized columns.

**No orders are ever submitted.** No order-mutating SDK methods are
imported. Phase-1 is read-only by construction.

Usage:
    # One-shot smoke test — three ticks with no sleeping:
    PYTHONPATH=src python3.11 -m run_kalshi_shadow --iterations 3 --no-sleep

    # Production-style long-run (SIGINT to stop):
    PYTHONPATH=src python3.11 -m run_kalshi_shadow --interval-s 5
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sqlite3
import sys
import threading
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlparse


_THIS = Path(__file__).resolve()
if str(_THIS.parent) not in sys.path:
    sys.path.insert(0, str(_THIS.parent))

from core.models import MarketQuote  # noqa: E402
from execution.kalshi_shadow_evaluator import (  # noqa: E402
    KalshiShadowEvaluator,
    ShadowConfig,
)
from execution.kalshi_paper_executor import KalshiPaperExecutor  # noqa: E402
from observability.event_log import EventLogger, NullEventLogger  # noqa: E402
from observability.timing import timed_phase  # noqa: E402
from market.coinbase_ws import CoinbaseWSReference, make_ws_reference_fetcher  # noqa: E402
from market.crypto_reference import BasketReferenceSource, ReferenceTick  # noqa: E402
from market.kalshi_market import (  # noqa: E402
    KalshiMarketConfig,
    KalshiMarketSource,
    book_to_market_quote,
)
from risk.kalshi_rules import RiskContext, RiskEngine, default_rules  # noqa: E402
from strategy.kalshi_fair_value import (  # noqa: E402
    FairValueModel, KalshiFairValueStrategy, StrategyConfig,
)
from strategy.pure_lag import PureLagStrategy, PureLagConfig  # noqa: E402
from strategy.partial_avg_fair_value import (  # noqa: E402
    PartialAvgFairValueModel, PartialAvgFairValueStrategy,
)


logger = logging.getLogger(__name__)


# Lifted from the strategy plan. Kept here (not in platform_adapters) so the
# entrypoint can be swapped for a Polymarket/etc. variant without dragging
# Kalshi-specific constants along.
ASSET_FROM_SERIES = {
    "KXBTC15M":  "btc",
    "KXETH15M":  "eth",
    "KXSOL15M":  "sol",
    "KXXRP15M":  "xrp",
    "KXDOGE15M": "doge",
    "KXBNB15M":  "bnb",
    "KXHYPE15M": "hype",
}


# Kalshi's public API returns `strike_type` values that our MarketQuote
# validation doesn't know (`greater_or_equal`). Normalize into the model's
# supported comparator set.
COMPARATOR_MAP = {
    "greater_or_equal": "at_least",
    "greater_than":     "above",
    "less_or_equal":    "below",
    "less_than":        "below",
    "ge": "at_least",
    "gt": "above",
    "le": "below",
    "lt": "below",
}


# ---------------------------------------------------------------------------
# Live-data coordinator — pulls books + references per tick.
# Injectable for tests via `LiveDataCoordinator.build(...)`.
# ---------------------------------------------------------------------------

from dataclasses import dataclass as _dataclass  # noqa: E402


@_dataclass(frozen=True)
class SnapshotTier:
    """One tier of the time-remaining → fetch-cadence policy.

    The coordinator picks the first tier whose `max_time_remaining_s`
    covers a ticker's `time_remaining_s`, then refuses to re-fetch that
    ticker until `interval_s` seconds have elapsed since its last fetch.

    Example: `SnapshotTier(max_time_remaining_s=30, interval_s=1.0)`
    fetches every market in the last 30 seconds on every scanner tick.
    """
    max_time_remaining_s: int
    interval_s: float


# Default cadence: the last 30 seconds of every 15-min cycle gets
# per-tick freshness (~1 Hz); anything further out refreshes every 10 s.
# That cuts the per-tick HTTP fan-out by ~10× during the 14-minute
# cold stretch in every 15-min cycle without losing edge-relevant data.
DEFAULT_SNAPSHOT_TIERS: tuple[SnapshotTier, ...] = (
    SnapshotTier(max_time_remaining_s=30,           interval_s=1.0),
    SnapshotTier(max_time_remaining_s=10**9,        interval_s=10.0),
)


class LiveDataCoordinator:
    """Per-tick side-effect bundle: discover markets, snapshot books, poll ref.

    Separated from `KalshiShadowEvaluator` so the evaluator stays a pure
    quotes-in / decisions-out engine. The coordinator is the part that
    touches the network.

    **Tiered snapshot cadence:** `_snapshot_books_impl` only fetches
    tickers whose previous fetch is older than the tier interval
    applicable to their current `time_remaining_s`. Default tiers keep
    near-expiry (≤30 s) markets on 1 Hz cadence and everything else on
    10 s cadence. Pass `snapshot_tiers=...` to override.
    """

    def __init__(
        self,
        *,
        rest_client: Any,
        reference_fetcher: Callable[[str], Decimal | None],
        market_source: KalshiMarketSource,
        reference_source: BasketReferenceSource,
        market_limit_per_series: int = 50,
        event_logger: Any = None,
        flags_poller: Any = None,
        snapshot_max_workers: int = 10,
        snapshot_tiers: tuple[SnapshotTier, ...] = DEFAULT_SNAPSHOT_TIERS,
    ) -> None:
        self._rest = rest_client
        self._fetch_reference = reference_fetcher
        self._market_source = market_source
        self._reference_source = reference_source
        self._market_limit = market_limit_per_series
        self._market_meta: dict[str, dict] = {}
        self._asset_by_ticker: dict[str, str] = {}
        self._fee_bps_by_ticker: dict[str, Decimal] = {}
        self._event_logger = event_logger
        # Optional runtime-flags poller; when absent every asset is scanned.
        self._flags_poller = flags_poller
        # Concurrency cap on parallel orderbook fetches. Bounded under
        # Kalshi's Basic-tier 20 r/s read limit — with 10 workers + a
        # handful of /markets discover calls per tick, peak rate ~12 r/s.
        # Lazy-init the pool so tests that only exercise discover() don't
        # pay the thread-start cost.
        self._snapshot_max_workers = max(1, int(snapshot_max_workers))
        self._snapshot_pool: Any = None
        # Tiered cadence state. Tiers are sorted ascending by
        # max_time_remaining_s so the first match wins.
        self._snapshot_tiers: tuple[SnapshotTier, ...] = tuple(
            sorted(snapshot_tiers, key=lambda t: t.max_time_remaining_s)
        )
        # Last fetch us per ticker. Tickers that were never fetched are
        # absent — `_is_due` treats absent as "due now".
        self._last_fetched_us: dict[str, int] = {}

    def discover(self) -> None:
        """Refresh the active-markets catalog. Called less often than per-tick."""
        with timed_phase(self._event_logger, "scanner.discover"):
            return self._discover_impl()

    def _discover_impl(self) -> None:
        flags = self._flags_poller.get() if self._flags_poller else None
        for series, asset in ASSET_FROM_SERIES.items():
            if flags is not None and not flags.is_asset_scan_enabled(asset):
                # Also drop any previously-discovered tickers for the asset
                # so the per-tick book snapshot loop stops hitting them.
                stale = [t for t, a in self._asset_by_ticker.items() if a == asset]
                for t in stale:
                    self._market_meta.pop(t, None)
                    self._asset_by_ticker.pop(t, None)
                continue
            try:
                # Kalshi's valid /markets status values are `open`, `unopened`,
                # `settled`. Use `open` (actively trading) for shadow scoring.
                resp = self._rest.request(
                    "GET", "/markets",
                    params={"series_ticker": series, "status": "open",
                            "limit": self._market_limit},
                    authenticated=False,
                )
            except Exception as e:  # noqa: BLE001 — log + keep going
                logger.warning("discover %s failed: %s", series, e)
                continue
            # Track which tickers the current discover call actually saw, so
            # we can prune tickers that rolled out of `status=open` (i.e. the
            # 15-min window closed). Without this, stale tickers linger in
            # `_market_meta` forever and `snapshot_books()` keeps hitting
            # their `/orderbook` endpoint — and when that fails, the evaluator
            # still sees the last pre-close book and writes decisions on an
            # already-closed market.
            seen: set[str] = set()
            for m in (resp or {}).get("markets", []) or []:
                ticker = m.get("ticker")
                if not ticker:
                    continue
                seen.add(ticker)
                raw_comparator = (m.get("strike_type") or "above").lower()
                comparator = COMPARATOR_MAP.get(raw_comparator, raw_comparator)
                # `close_time` is the 15-min window boundary (what we care
                # about for time_remaining). `expiration_time` in Kalshi's
                # response is actually 7 days later (a final-cutoff value);
                # **don't** use it for time_remaining computations.
                self._market_meta[ticker] = {
                    "series_ticker": series,
                    "event_ticker": m.get("event_ticker", ""),
                    "strike": m.get("strike_price") or m.get("floor_strike") or 0,
                    "comparator": comparator,
                    "expiration_ts": scripts_compat.parse_iso_or_epoch(
                        m.get("close_time")
                    ),
                    "asset": asset,
                }
                self._asset_by_ticker[ticker] = asset
            # Prune this series' tickers that weren't in the response. Only
            # touch entries whose `asset` matches — other series' tickers
            # might not have been refreshed this iteration.
            stale = [
                t for t, meta in self._market_meta.items()
                if meta.get("asset") == asset and t not in seen
            ]
            for t in stale:
                self._market_meta.pop(t, None)
                self._asset_by_ticker.pop(t, None)
                self._fee_bps_by_ticker.pop(t, None)

    def snapshot_books(self) -> None:
        """Pull the latest orderbook for every known ticker."""
        with timed_phase(self._event_logger, "scanner.snapshot_books",
                         tickers=len(self._market_meta)):
            return self._snapshot_books_impl()

    def _snapshot_books_impl(self) -> None:
        """Parallel, tier-filtered orderbook fetch.

        Only tickers "due" per their tier's `interval_s` are submitted
        this tick — a market at `t_rem=500 s` (cold tier: 10 s interval)
        gets re-fetched at most once per 10 s even though the scanner
        ticks every 1 s.

        Each due ticker's HTTP round-trip runs in its own worker thread.
        Per-ticker errors are swallowed + logged so one bad ticker doesn't
        stall the tick. `apply_snapshot` / `update_lifecycle` are already
        guarded by `_books_lock` so concurrent writes are safe.
        """
        all_tickers = list(self._market_meta.keys())
        if not all_tickers:
            return
        now_s = int(time.time())
        now_us = now_s * 1_000_000
        due = [t for t in all_tickers if self._is_due(t, now_s, now_us)]
        if not due:
            return
        pool = self._ensure_snapshot_pool()
        # `map` blocks on the first N submissions until workers are free,
        # which naturally throttles against Kalshi's read-rate limit.
        list(pool.map(self._fetch_and_apply_one, due))

    def _is_due(self, ticker: str, now_s: int, now_us: int) -> bool:
        """True iff this ticker's last fetch is older than its tier interval.

        Tickers never fetched (`_last_fetched_us` miss) are always due.
        The tier chosen is the first whose `max_time_remaining_s` covers
        the ticker's current `time_remaining`.
        """
        meta = self._market_meta.get(ticker)
        if meta is None:
            return False
        last_us = self._last_fetched_us.get(ticker)
        if last_us is None:
            return True
        exp_ts = int(meta.get("expiration_ts") or 0)
        t_rem = max(0, exp_ts - now_s)
        interval_s = self._tier_interval_for(t_rem)
        return (now_us - last_us) >= int(interval_s * 1_000_000)

    def _tier_interval_for(self, time_remaining_s: int) -> float:
        """Find the tier covering `time_remaining_s`. Falls back to 1 s if
        no tier matches (defensive — DEFAULT_SNAPSHOT_TIERS always has a
        catch-all)."""
        for tier in self._snapshot_tiers:
            if time_remaining_s <= tier.max_time_remaining_s:
                return tier.interval_s
        return 1.0

    def _ensure_snapshot_pool(self) -> Any:
        if self._snapshot_pool is None:
            from concurrent.futures import ThreadPoolExecutor
            self._snapshot_pool = ThreadPoolExecutor(
                max_workers=self._snapshot_max_workers,
                thread_name_prefix="kalshi-book-fetch",
            )
        return self._snapshot_pool

    def close(self) -> None:
        """Shut the thread pool down. Safe to call multiple times."""
        if self._snapshot_pool is not None:
            self._snapshot_pool.shutdown(wait=False, cancel_futures=True)
            self._snapshot_pool = None

    def _fetch_and_apply_one(self, ticker: str) -> None:
        """Fetch one orderbook + update lifecycle. Runs in a worker thread.

        Records `last_fetched_us` on success so tier throttling works.
        Failures don't bump the timer — a transient error means we'll retry
        on the next tick regardless of tier.
        """
        try:
            resp = self._rest.request(
                "GET", f"/markets/{ticker}/orderbook",
                authenticated=False,
            )
        except Exception as e:  # noqa: BLE001 — per-ticker isolation
            logger.warning("book fetch %s failed: %s", ticker, e)
            return
        # Kalshi's response uses `orderbook_fp` with `yes_dollars` /
        # `no_dollars` keys (fixed-point dollar-string format). Translate
        # into the `{yes: [...], no: [...]}` shape that our
        # `KalshiMarketSource.apply_snapshot` expects.
        raw = (resp or {}).get("orderbook_fp") or (resp or {}).get("orderbook") or {}
        book = {
            "yes": raw.get("yes_dollars") or raw.get("yes") or [],
            "no":  raw.get("no_dollars")  or raw.get("no")  or [],
        }
        self._market_source.apply_snapshot(ticker, book)
        meta = self._market_meta.get(ticker, {})
        status = meta.get("status") or "active"
        exp = int(meta.get("expiration_ts") or 0)
        time_remaining = max(0, exp - int(time.time()))
        self._market_source.update_lifecycle(
            ticker, status=status, time_remaining_s=time_remaining,
        )
        # Record successful fetch for tier throttling. Using
        # `int(time.time()*1e6)` rather than re-using the tick's now_us
        # is fine — workers may finish out of order and the resulting
        # skew is well under the smallest tier interval (1 s).
        self._last_fetched_us[ticker] = int(time.time() * 1_000_000)

    def sample_reference(self) -> None:
        """Poll Coinbase once per asset and feed the basket source.

        Also feeds the PureLagStrategy's per-asset rolling-price history if
        `lag_strategy` has been registered via `attach_lag_strategy()`.
        """
        with timed_phase(self._event_logger, "scanner.sample_reference"):
            return self._sample_reference_impl()

    def _sample_reference_impl(self) -> None:
        for asset in set(ASSET_FROM_SERIES.values()):
            price = self._fetch_reference(asset)
            if price is None:
                continue
            self._reference_source.record_tick(ReferenceTick(
                asset=asset, price=price, ts_us=int(time.time() * 1_000_000),
                src="coinbase_live",
            ))
            for strat in getattr(self, "_tick_sinks", ()):
                strat.record_reference_tick(asset, price)

    def attach_lag_strategy(self, lag_strategy: PureLagStrategy) -> None:
        """Register a PureLagStrategy so `sample_reference()` feeds its
        rolling-price history with every reference tick.
        """
        self.attach_tick_sink(lag_strategy)

    def attach_tick_sink(self, strategy) -> None:
        """Register any strategy with a `record_reference_tick(asset, price)`
        method so `sample_reference()` feeds it per tick.
        """
        if not hasattr(self, "_tick_sinks") or self._tick_sinks is None:
            self._tick_sinks = []
        self._tick_sinks.append(strategy)

    @property
    def market_meta(self) -> dict[str, dict]:
        return self._market_meta

    @property
    def asset_by_ticker(self) -> dict[str, str]:
        return self._asset_by_ticker

    @property
    def fee_bps_by_ticker(self) -> dict[str, Decimal]:
        return self._fee_bps_by_ticker


# Re-export a tiny helper used above without a circular-import dance.
class scripts_compat:  # noqa: N801 — deliberately lowercase to signal "proxy"
    @staticmethod
    def parse_iso_or_epoch(val: Any) -> int:
        if val is None or val == "":
            return 0
        if isinstance(val, (int, float)):
            return int(val)
        if isinstance(val, str):
            from datetime import datetime, timezone
            try:
                dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
                return int(dt.astimezone(timezone.utc).timestamp())
            except ValueError:
                try:
                    return int(float(val))
                except ValueError:
                    return 0
        return 0


# ---------------------------------------------------------------------------
# Reference fetcher — Coinbase /ticker. Separated for testability.
# ---------------------------------------------------------------------------

def default_coinbase_fetcher(asset: str) -> Decimal | None:
    """Default implementation — blocking HTTP. Replace with WS in P2."""
    import requests
    product = {
        "btc":  "BTC-USD",
        "eth":  "ETH-USD",
        "sol":  "SOL-USD",
        "xrp":  "XRP-USD",
        "doge": "DOGE-USD",
        "bnb":  "BNB-USD",
        "hype": "HYPE-USD",
    }.get(asset.lower())
    if not product:
        return None
    try:
        resp = requests.get(
            f"https://api.exchange.coinbase.com/products/{product}/ticker",
            timeout=3.0,
        )
    except Exception:
        return None
    if resp.status_code != 200:
        return None
    try:
        p = resp.json().get("price")
    except ValueError:
        return None
    return Decimal(str(p)) if p is not None else None


# ---------------------------------------------------------------------------
# Resolution lookup — reads settled result via GET /markets/{ticker}
# ---------------------------------------------------------------------------

def build_resolution_lookup(rest_client: Any) -> Callable[[str], dict | None]:
    def _lookup(ticker: str) -> dict | None:
        try:
            resp = rest_client.request(
                "GET", f"/markets/{ticker}", authenticated=False,
            )
        except Exception:
            return None
        return (resp or {}).get("market") or resp
    return _lookup


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

def open_connection(url: str) -> tuple[Any, bool]:
    parsed = urlparse(url)
    if parsed.scheme in ("sqlite", ""):
        raw = parsed.path or url.removeprefix("sqlite://")
        if raw.startswith("//"):
            path = Path(raw[1:])
        elif raw.startswith("/"):
            path = Path(raw.lstrip("/"))
        else:
            path = Path(raw)
        path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(str(path)), False
    if parsed.scheme in ("postgres", "postgresql"):
        import psycopg2
        return psycopg2.connect(url), True
    raise ValueError(f"Unsupported DATABASE_URL scheme: {parsed.scheme!r}")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def build_paper_executor_bridge(
    *, conn: Any, is_postgres: bool, strategy_label: str,
    now_us: Callable[[], int] | None = None,
    event_logger: Any = None,
    flags_poller: Any = None,
    alert_dispatcher: Any = None,
) -> tuple[KalshiPaperExecutor, Callable[[Any, Any], None], Callable[[str, str], None]]:
    """Build a paper executor + the (decision_hook, reconcile_hook) pair the
    shadow evaluator invokes. The risk engine uses the config-default rule set.

    `event_logger` (optional) — `EventLogger`-shaped. Emits `paper_fill`,
    `risk_reject`, and `paper_settle` events so the JSONL log captures the
    full decision → fill → settlement lineage.

    `alert_dispatcher` (optional) — `AlertDispatcher`-shaped. When supplied,
    paper fills / risk rejects / settlements additionally fan out to every
    registered alert backend (Telegram / Discord / Gmail). Never raises — a
    failing backend is logged and swallowed so the trading loop stays up.
    """
    risk_engine = RiskEngine(default_rules())
    executor = KalshiPaperExecutor(
        risk_engine=risk_engine,
        conn=conn, is_postgres=is_postgres,
        strategy_label=strategy_label,
        now_us=now_us,
    )
    import time as _t
    _get_now = now_us or (lambda: int(_t.time() * 1_000_000))

    def _record(event_type: str, **fields) -> None:
        if event_logger is None:
            return
        try:
            event_logger.record(event_type, **fields)
        except Exception as e:  # noqa: BLE001
            logger.warning("event_logger.record failed: %s", e)

    def _alert(method_name: str, *args, **kwargs) -> None:
        if alert_dispatcher is None:
            return
        try:
            getattr(alert_dispatcher, method_name)(*args, **kwargs)
        except Exception as e:  # noqa: BLE001 — telemetry must not crash caller
            logger.warning("alert_dispatcher.%s failed: %s", method_name, e)

    def decision_hook(quote, opp) -> None:
        # Runtime enforcement: short-circuit before touching the risk
        # engine or executor. Two levels of gate, evaluated in order:
        #   1. `execution_kill_switch` — global panic button.
        #   2. `execution_enabled[asset]` — per-asset granular toggle.
        # Both are operator-driven via the dashboard's controls card.
        if flags_poller is not None:
            cur = flags_poller.get()
            # `MarketQuote` has no `asset` field — derive via the series
            # prefix. Previously this used `getattr(opp.quote, "asset", None)`
            # which always returned empty, silently bypassing the per-asset
            # `execution_enabled` toggle.
            asset = ASSET_FROM_SERIES.get(opp.quote.series_ticker, "")
            if cur.execution_kill_switch:
                _record(
                    "execution_kill_switch_blocked",
                    strategy_label=strategy_label,
                    market_ticker=opp.quote.market_ticker,
                    asset=asset,
                )
                return
            if asset and not cur.is_asset_execution_enabled(asset):
                _record(
                    "execution_disabled_for_asset",
                    strategy_label=strategy_label,
                    market_ticker=opp.quote.market_ticker,
                    asset=asset,
                )
                return
        # Build a fresh RiskContext from the executor's snapshots each tick
        # (same round-trip pattern as TestRiskContextIntegration).
        ctx = RiskContext(
            now_us=_get_now(),
            # Reference tick ts isn't available here cheaply; use the quote's
            # timestamp as a proxy — the ReferenceFeedStaleRule then sees
            # the book-vs-clock gap rather than ref-vs-clock. Acceptable for
            # shadow/paper; live uses a real ReferenceSource.
            last_reference_tick_us=quote.quote_timestamp_us,
            open_positions=executor.open_positions(),
            daily_realized_pnl_usd=executor.daily_realized_pnl(),
            position_notional_by_strike_usd=executor.notional_by_strike(),
        )
        result = executor.submit(opp, ctx)
        if result.success:
            _record(
                "paper_fill",
                strategy_label=strategy_label,
                market_ticker=opp.quote.market_ticker,
                side=opp.recommended_side,
                fill_price=opp.hypothetical_fill_price,
                size_contracts=opp.hypothetical_size_contracts,
                edge_bps=opp.expected_edge_bps_after_fees,
            )
            _alert(
                "paper_fill",
                opp.quote.market_ticker, opp.recommended_side,
                opp.hypothetical_fill_price,
                opp.hypothetical_size_contracts,
                edge_bps=opp.expected_edge_bps_after_fees,
                strategy_label=strategy_label,
            )
        else:
            _record(
                "risk_reject",
                strategy_label=strategy_label,
                market_ticker=opp.quote.market_ticker,
                side=opp.recommended_side,
                reason=result.reason,
            )
            _alert(
                "risk_reject",
                opp.quote.market_ticker, opp.recommended_side, result.reason,
                strategy_label=strategy_label,
            )

    def reconcile_hook(ticker: str, outcome: str) -> None:
        settlements = executor.reconcile(ticker, outcome)
        for s in settlements:
            _record(
                "paper_settle",
                strategy_label=strategy_label,
                market_ticker=ticker,
                outcome=outcome,
                realized_pnl_usd=s.realized_pnl_usd,
                fill_price=s.fill.fill_price,
                size_contracts=s.fill.size_contracts,
            )
            _alert(
                "paper_settle", ticker, outcome, s.realized_pnl_usd,
                strategy_label=strategy_label,
            )

    return executor, decision_hook, reconcile_hook


def build_evaluator(
    *, conn: Any, is_postgres: bool,
    rest_client: Any,
    reference_fetcher: Callable[[str], Decimal | None],
    strategy_config: StrategyConfig | None = None,
    also_pure_lag: bool = False,
    also_partial_avg: bool = False,
    primary_strategy: str = "stat_model",
    paper_executor: bool = False,
    event_logger: Any = None,
    flags_poller: Any = None,
    alert_dispatcher: Any = None,
) -> tuple[KalshiShadowEvaluator, LiveDataCoordinator]:
    """Wire everything together. Not called from tests."""
    market_source = KalshiMarketSource(KalshiMarketConfig())
    reference_source = BasketReferenceSource(assets=tuple(set(ASSET_FROM_SERIES.values())))
    reference_source.start()
    # Shadow-evaluator defaults calibrated from
    # docs/kalshi_shadow_live_capture_results.md (2026-04-20 live run):
    #   • time_remaining < 60s: 0% win rate across all p_yes levels
    #   • time_remaining 60-120s: 5.5% win rate (still unprofitable)
    #   • time_remaining >=120s: break-even, >=300s: +$0.29/decision
    #   • edge <300bps loses money; 300-1200bps is the sweet spot.
    # So the scanner scores only the 120-900s window at ≥300bps edge.
    cfg = strategy_config or StrategyConfig(
        min_edge_bps_after_fees=Decimal("300"),   # below 300: negative EV per data
        max_ci_width=Decimal("1.0"),               # still permissive on CI
        min_book_depth_usd=Decimal("50"),
        time_window_seconds=(120, 900),            # skip final 2 min (0% wins)
    )
    coordinator = LiveDataCoordinator(
        rest_client=rest_client,
        reference_fetcher=reference_fetcher,
        event_logger=event_logger,
        market_source=market_source,
        reference_source=reference_source,
        flags_poller=flags_poller,
    )
    if primary_strategy == "pure_lag":
        lag_primary = PureLagStrategy(PureLagConfig())
        coordinator.attach_tick_sink(lag_primary)
        strategy = lag_primary
        label = "pure_lag"
    elif primary_strategy == "partial_avg":
        pa_primary = PartialAvgFairValueStrategy(
            PartialAvgFairValueModel(), cfg,
        )
        coordinator.attach_tick_sink(pa_primary)
        strategy = pa_primary
        label = "partial_avg"
    else:
        strategy = KalshiFairValueStrategy(FairValueModel(), cfg)
        label = "stat_model"
    decision_hook = None
    reconcile_hook = None
    if paper_executor:
        _, decision_hook, reconcile_hook = build_paper_executor_bridge(
            conn=conn, is_postgres=is_postgres, strategy_label=label,
            event_logger=event_logger,
            flags_poller=flags_poller,
            alert_dispatcher=alert_dispatcher,
        )
    evaluator = KalshiShadowEvaluator(
        market_source=market_source,
        reference_source=reference_source,
        strategy=strategy,
        market_meta_by_ticker=coordinator.market_meta,  # shared reference
        asset_by_ticker=coordinator.asset_by_ticker,    # shared reference
        fee_bps_by_ticker=coordinator.fee_bps_by_ticker,
        conn=conn, is_postgres=is_postgres,
        resolution_lookup=build_resolution_lookup(rest_client),
        config=ShadowConfig(),
        strategy_label=label,
        decision_hook=decision_hook,
        reconcile_hook=reconcile_hook,
        event_logger=event_logger,
    )
    # Side-by-side partner evaluators share the same coordinator + sources.
    # Each partner.tick() runs per loop iteration; strategy_label tags
    # the decision rows so all three are separable in `shadow_decisions`.
    evaluator.partners = []  # type: ignore[attr-defined]
    if also_pure_lag:
        lag_strategy = PureLagStrategy(PureLagConfig())
        coordinator.attach_tick_sink(lag_strategy)
        evaluator.partners.append(KalshiShadowEvaluator(  # type: ignore[attr-defined]
            market_source=market_source,
            reference_source=reference_source,
            strategy=lag_strategy,
            market_meta_by_ticker=coordinator.market_meta,
            asset_by_ticker=coordinator.asset_by_ticker,
            fee_bps_by_ticker=coordinator.fee_bps_by_ticker,
            conn=conn, is_postgres=is_postgres,
            resolution_lookup=build_resolution_lookup(rest_client),
            config=ShadowConfig(),
            strategy_label="pure_lag",
        ))
    if also_partial_avg:
        # Uses same StrategyConfig as stat_model so edge floors / depth
        # gates are identical — the sole difference is the pricing model.
        pa_strategy = PartialAvgFairValueStrategy(
            PartialAvgFairValueModel(), cfg,
        )
        coordinator.attach_tick_sink(pa_strategy)
        evaluator.partners.append(KalshiShadowEvaluator(  # type: ignore[attr-defined]
            market_source=market_source,
            reference_source=reference_source,
            strategy=pa_strategy,
            market_meta_by_ticker=coordinator.market_meta,
            asset_by_ticker=coordinator.asset_by_ticker,
            fee_bps_by_ticker=coordinator.fee_bps_by_ticker,
            conn=conn, is_postgres=is_postgres,
            resolution_lookup=build_resolution_lookup(rest_client),
            config=ShadowConfig(),
            strategy_label="partial_avg",
        ))
    return evaluator, coordinator


def run_loop(
    *,
    evaluator: KalshiShadowEvaluator,
    coordinator: LiveDataCoordinator | None,
    iterations: int | None,
    interval_s: float,
    no_sleep: bool,
    stop_event: threading.Event | None = None,
    discover_every: int = 30,
    flags_poller: Any = None,
) -> dict[str, int]:
    """Drive the evaluator. Returns per-tick aggregate counts.

    `flags_poller` (optional): when supplied, each loop tick reads the
    current flags. Strategies flagged disabled get their tick() skipped
    (evaluator still polls data; only scoring is suppressed). Assets
    flagged disabled get filtered out by the coordinator's discover().
    """
    stop = stop_event or threading.Event()

    def _handler(signum, _frame):
        logger.info("signal %d received — stopping after current tick", signum)
        stop.set()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handler)
        except ValueError:
            # pytest / threads — non-main thread can't install handlers.
            pass

    totals = {"ticks": 0, "written": 0, "reconciled": 0}
    partners = list(getattr(evaluator, "partners", []) or [])
    # Backwards-compat: older code set `pure_lag_partner` singular.
    legacy_partner = getattr(evaluator, "pure_lag_partner", None)
    if legacy_partner is not None and legacy_partner not in partners:
        partners.append(legacy_partner)
    i = 0
    while not stop.is_set():
        if coordinator is not None and (i % discover_every == 0):
            coordinator.discover()
        if coordinator is not None:
            coordinator.snapshot_books()
            coordinator.sample_reference()
        flags = flags_poller.get() if flags_poller is not None else None

        def _strategy_on(ev: Any) -> bool:
            if flags is None:
                return True
            label = getattr(ev, "_strategy_label", None) or getattr(ev, "strategy_label", None)
            return flags.is_strategy_enabled(label) if label else True

        if _strategy_on(evaluator):
            result = evaluator.tick()
            totals["written"] += result.get("written", 0)
            totals["reconciled"] += result.get("reconciled", 0)
        for p in partners:
            if not _strategy_on(p):
                continue
            r = p.tick()
            totals["written"] += r.get("written", 0)
            totals["reconciled"] += r.get("reconciled", 0)
        totals["ticks"] += 1
        i += 1
        if iterations is not None and i >= iterations:
            break
        if not no_sleep and not stop.is_set():
            stop.wait(interval_s)
    return totals


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase-1 shadow evaluator.")
    parser.add_argument("--iterations", type=int, default=None,
                        help="Stop after N ticks. Default: run until SIGINT.")
    parser.add_argument("--interval-s", type=float, default=2.0)
    parser.add_argument("--no-sleep", action="store_true",
                        help="Skip sleeping between ticks (smoke-test mode).")
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--dry-run", action="store_true",
                        help="Don't open a DB connection; skip persistence.")
    parser.add_argument("--no-ws", action="store_true",
                        help="Disable Coinbase WS; fall back to REST ticker polling.")
    parser.add_argument("--primary-strategy",
                        choices=("stat_model", "partial_avg", "pure_lag"),
                        default="stat_model",
                        help="Primary strategy of the main evaluator. "
                             "Decisions tagged with strategy_label=<choice>.")
    parser.add_argument("--also-pure-lag", action="store_true",
                        help="Run a PureLagStrategy evaluator side-by-side; "
                             "decisions tagged with strategy_label='pure_lag'.")
    parser.add_argument("--also-partial-avg", action="store_true",
                        help="Run a PartialAvgFairValueStrategy evaluator "
                             "side-by-side; tagged 'partial_avg'.")
    parser.add_argument("--with-kraken", action="store_true",
                        help="Add Kraken WS as a 2nd reference source "
                             "(basket-median with Coinbase). Helps BNB.")
    parser.add_argument("--paper-executor", action="store_true",
                        help="Route every approvable decision through the "
                             "RiskEngine + KalshiPaperExecutor. Fills go to "
                             "paper_fills; settlements to paper_settlements. "
                             "Zero $ at risk; paper is default.")
    parser.add_argument("--events-dir", default="logs",
                        help="Directory for the structured JSONL event log. "
                             "Default: logs/. Set to '' to disable.")
    parser.add_argument("--flags-path", default="config/runtime_flags.json",
                        help="Runtime-flags JSON file. Dashboard writes here, "
                             "runner polls it every 2s. Default: "
                             "config/runtime_flags.json.")
    parser.add_argument("--disable-alerts", action="store_true",
                        help="Skip building the alert dispatcher. By default "
                             "the runner builds one from env (TELEGRAM_*, "
                             "DISCORD_WEBHOOK_URL, GMAIL_*); only configured "
                             "backends attach, so this flag is mostly useful "
                             "for forcing silence in local dev.")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    # Lazy import — avoids requiring the SDK in pure-unit-test runs.
    from kalshi_api import KalshiAPIClient
    # For Phase-1 shadow evaluator we only hit PUBLIC endpoints (/markets,
    # /markets/{ticker}/orderbook). These live only on the prod host —
    # demo.kalshi.co doesn't carry the CRYPTO15M product families. Force
    # `env=prod` here regardless of `.env`; authenticated endpoints stay
    # untouched so the demo key is still what gets signed if we ever flip.
    rest_client = KalshiAPIClient.from_env(env="prod")

    url = (args.database_url or os.environ.get("DATABASE_URL")
           or "sqlite:///data/kalshi.db")
    conn = None
    is_pg = False
    if not args.dry_run:
        conn, is_pg = open_connection(url)
        # Install the ops-events sink so REST retries / WS reconnects /
        # other emitters land in the dashboard's `ops_events` table.
        # Postgres is supported by the runner but the sink is sqlite-only
        # for now — emits silently no-op on pg until we extend the sink.
        if not is_pg:
            import ops_events
            ops_events.set_sink(ops_events.db_sink(url))
            ops_events.emit("runner", "info", "scanner started",
                            {"pid": os.getpid(), "argv": sys.argv[1:]})

    # Coinbase WS for sub-second reference prices. Falls back to REST ticker
    # when the WS cache is older than `staleness_threshold_us` (5s default).
    # This addresses the "0% win rate at T<60s" finding from
    # docs/kalshi_shadow_live_capture_results.md §6.
    ws_sources: list = []
    fetcher = default_coinbase_fetcher
    if not args.no_ws:
        cb_ws = CoinbaseWSReference(
            assets=tuple(set(ASSET_FROM_SERIES.values())),
        )
        cb_ws.start()
        ws_sources.append(("coinbase", cb_ws))
        if args.with_kraken:
            from market.kraken_ws import KrakenWSReference
            kraken_ws = KrakenWSReference(
                assets=tuple(a for a in ASSET_FROM_SERIES.values() if a != "hype"),
            )
            kraken_ws.start()
            ws_sources.append(("kraken", kraken_ws))

        if len(ws_sources) >= 2:
            from market.basket_ws import BasketWSReference, make_basket_fetcher
            basket = BasketWSReference(dict(ws_sources))
            fetcher = make_basket_fetcher(basket, rest_fallback=default_coinbase_fetcher)
        else:
            fetcher = make_ws_reference_fetcher(
                cb_ws, staleness_threshold_us=5_000_000,
                rest_fallback=default_coinbase_fetcher,
            )

    if args.events_dir:
        event_logger: Any = EventLogger(
            base_dir=args.events_dir, rotate_daily=True,
        )
        logger.info("event log → %s", event_logger.current_path())
    else:
        event_logger = NullEventLogger()

    from runtime_flags import FlagsPoller
    flags_poller = FlagsPoller(args.flags_path, interval_s=2.0)
    logger.info("runtime flags → %s", args.flags_path)

    alert_dispatcher: Any = None
    if not args.disable_alerts:
        from alerting.dispatcher import build_dispatcher_from_env
        alert_dispatcher = build_dispatcher_from_env()
        logger.info("alert dispatcher → %d backend(s) configured",
                    alert_dispatcher.backend_count)

    evaluator, coordinator = build_evaluator(
        conn=conn, is_postgres=is_pg,
        rest_client=rest_client,
        reference_fetcher=fetcher,
        also_pure_lag=args.also_pure_lag,
        also_partial_avg=args.also_partial_avg,
        primary_strategy=args.primary_strategy,
        paper_executor=args.paper_executor,
        event_logger=event_logger,
        flags_poller=flags_poller,
        alert_dispatcher=alert_dispatcher,
    )
    try:
        totals = run_loop(
            evaluator=evaluator, coordinator=coordinator,
            iterations=args.iterations, interval_s=args.interval_s,
            no_sleep=args.no_sleep,
            flags_poller=flags_poller,
        )
        logger.info("exit totals: %s", totals)
    except Exception as exc:
        if alert_dispatcher is not None:
            try:
                alert_dispatcher.system_error("run_kalshi_shadow", repr(exc))
            except Exception:  # noqa: BLE001
                pass
        raise
    finally:
        try:
            coordinator.close()
        except Exception:  # noqa: BLE001 — shutdown hygiene; keep draining
            pass
        for _, ws in ws_sources:
            try:
                ws.stop(timeout=5.0)
            except Exception:
                pass
        if conn is not None:
            conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
