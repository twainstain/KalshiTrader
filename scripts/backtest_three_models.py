"""Replay historical Kalshi trades against the three models.

For every settled 15-min crypto market in `kalshi_historical_markets`,
for every historical Kalshi trade on that ticker in the trading window:

  1. Reconstruct the reference spot at the trade timestamp from
     `coinbase_trades` (latest trade ≤ ts).
  2. Reconstruct the partial close-60s-avg from `coinbase_trades`
     intersected with `[close_ts - 60, min(now, close_ts)]`.
  3. Ask each model: given this quote, would you take yes or no at the
     recorded trade price?
  4. Score realized P/L against `settled_result`.

Markets with no Coinbase coverage for the relevant windows are skipped.

Output: `docs/three_model_backtest_results.md` — per-model / per-asset /
per-time-bucket win rate, mean edge, total P/L.

Run:
    PYTHONPATH=src python3.11 scripts/backtest_three_models.py
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import sys
import time
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Iterable


SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from strategy.kalshi_fair_value import FairValueModel  # noqa: E402
from strategy.partial_avg_fair_value import (  # noqa: E402
    PartialAvgFairValueModel,
)
from strategy.pure_lag import PureLagStrategy, PureLagConfig  # noqa: E402

logger = logging.getLogger("backtest")


DB_PATH = Path("data/kalshi.db")

ASSET_BY_SERIES = {
    "KXBTC15M":  "btc",
    "KXETH15M":  "eth",
    "KXSOL15M":  "sol",
    "KXXRP15M":  "xrp",
    "KXDOGE15M": "doge",
    "KXBNB15M":  "bnb",
    "KXHYPE15M": "hype",
}

ZERO = Decimal("0")
ONE = Decimal("1")
BPS = Decimal("10000")
FEE_BPS = Decimal("35")  # Kalshi default taker fee

# Time-bucket edges (seconds remaining) — bucket assignment by `time_remaining_s`.
TIME_BUCKETS: list[tuple[str, int, int]] = [
    ("0-30",    0,   30),
    ("30-60",   30,  60),
    ("60-120",  60,  120),
    ("120-300", 120, 300),
    ("300-600", 300, 600),
    ("600-900", 600, 900),
]


@dataclass
class CoinbaseHistory:
    """Per-asset sorted arrays of (ts_us, price) — used by bisect lookups."""
    ts_us: list[int]
    price: list[Decimal]

    def latest_at(self, ts_us: int) -> Decimal | None:
        """Latest price at or before `ts_us`. None if no prior ticks."""
        if not self.ts_us:
            return None
        i = bisect_right(self.ts_us, ts_us)
        if i == 0:
            return None
        return self.price[i - 1]

    def avg_in_window(self, start_us: int, end_us: int) -> tuple[Decimal, int]:
        """Return (mean_price, n_ticks) in `[start_us, end_us]`. If no ticks
        fall in the window, returns (ZERO, 0).
        """
        if not self.ts_us:
            return ZERO, 0
        lo = bisect_right(self.ts_us, start_us - 1)
        hi = bisect_right(self.ts_us, end_us)
        slice_ = self.price[lo:hi]
        if not slice_:
            return ZERO, 0
        total = sum(slice_, ZERO)
        return total / Decimal(len(slice_)), len(slice_)


def load_coinbase(conn: sqlite3.Connection) -> dict[str, CoinbaseHistory]:
    """Load all coinbase_trades into per-asset sorted arrays."""
    out: dict[str, CoinbaseHistory] = {}
    cur = conn.execute(
        "SELECT asset, ts_us, price FROM coinbase_trades ORDER BY asset, ts_us"
    )
    cur_asset = None
    ts_buf: list[int] = []
    px_buf: list[Decimal] = []
    for asset, ts_us, price in cur:
        if asset != cur_asset and cur_asset is not None:
            out[cur_asset] = CoinbaseHistory(ts_buf, px_buf)
            ts_buf, px_buf = [], []
        cur_asset = asset
        ts_buf.append(int(ts_us))
        px_buf.append(Decimal(price))
    if cur_asset is not None:
        out[cur_asset] = CoinbaseHistory(ts_buf, px_buf)
    return out


def load_settled_markets(
    conn: sqlite3.Connection, coinbase_min_ts_us: int, coinbase_max_ts_us: int,
) -> list[tuple]:
    """Pull settled crypto 15-min markets with close_ts inside Coinbase coverage."""
    # close_ts is in seconds, coinbase ts in microseconds.
    lo_s = coinbase_min_ts_us // 1_000_000
    hi_s = coinbase_max_ts_us // 1_000_000
    cur = conn.execute(
        """
        SELECT market_ticker, series_ticker, strike, comparator, close_ts,
               settled_result, expiration_value
        FROM kalshi_historical_markets
        WHERE series_ticker IN ('KXBTC15M','KXETH15M','KXSOL15M',
                                'KXXRP15M','KXDOGE15M','KXBNB15M','KXHYPE15M')
          AND settled_result IN ('yes','no')
          AND close_ts BETWEEN ? AND ?
        """,
        (lo_s, hi_s),
    )
    return cur.fetchall()


def load_trades_for_market(
    conn: sqlite3.Connection, market_ticker: str,
) -> list[tuple[int, Decimal, str]]:
    """Pull historical Kalshi trades for one market, sorted by ts."""
    cur = conn.execute(
        """
        SELECT ts_us, price, taker_side FROM kalshi_historical_trades
        WHERE market_ticker = ? ORDER BY ts_us
        """,
        (market_ticker,),
    )
    return [(int(r[0]), Decimal(r[1]), r[2]) for r in cur]


def bucket_label(time_remaining_s: Decimal) -> str | None:
    t = float(time_remaining_s)
    for label, lo, hi in TIME_BUCKETS:
        if lo <= t < hi:
            return label
    return None


@dataclass
class DecisionScore:
    model: str
    asset: str
    bucket: str
    side: str             # "yes" | "no"
    fill_price: Decimal   # what we paid
    outcome: str          # "yes" | "no"
    pnl_usd: Decimal      # realized P/L per contract (after fees)
    p_yes_predicted: Decimal


def score_pnl(
    side: str, fill_price: Decimal, outcome: str, fee_bps: Decimal,
) -> Decimal:
    """Per-contract P/L. YES and NO contracts each cost `fill_price` and
    pay $1 on win, $0 on loss. The `side` label only picks which outcome
    counts as a win — the cost basis is symmetric:

      - buy YES at $0.40, resolves YES → profit $0.60 (= 1 - 0.40)
      - buy YES at $0.40, resolves NO  → loss   $0.40
      - buy NO  at $0.60, resolves NO  → profit $0.40 (= 1 - 0.60)
      - buy NO  at $0.60, resolves YES → loss   $0.60

    The earlier implementation used `(1 - fill_price)` as the NO cost basis,
    which scored buying NO at 0.60 as roughly +$0.60 on a win and -$0.40 on
    a loss (exactly the P/L of a YES-at-0.40 position). Fee notional is
    proportional to the contract's purchase price regardless of side.
    """
    fee = (fee_bps / BPS) * fill_price
    win = (
        (side == "yes" and outcome == "yes")
        or (side == "no" and outcome == "no")
    )
    if win:
        return (ONE - fill_price) - fee
    return -fill_price - fee


def run_backtest() -> None:  # noqa: C901  (long but linear)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    conn = sqlite3.connect(DB_PATH)

    logger.info("loading coinbase_trades …")
    cb = load_coinbase(conn)
    totals = {a: len(h.ts_us) for a, h in cb.items()}
    if not cb:
        raise RuntimeError("no coinbase_trades rows")
    cb_min = min(h.ts_us[0] for h in cb.values() if h.ts_us)
    cb_max = max(h.ts_us[-1] for h in cb.values() if h.ts_us)
    logger.info("coinbase tick counts: %s (window %.1fh)",
                totals, (cb_max - cb_min) / 1e6 / 3600)

    logger.info("loading settled markets in coverage window …")
    markets = load_settled_markets(conn, cb_min, cb_max)
    logger.info("markets: %d", len(markets))

    stat = FairValueModel()
    partial = PartialAvgFairValueModel()
    # PureLagStrategy is stateful; we'll reset per market.
    decisions: list[DecisionScore] = []

    processed = 0
    for (ticker, series, strike, comparator, close_ts, settled,
         expiration_value) in markets:
        asset = ASSET_BY_SERIES.get(series)
        if asset is None:
            continue
        asset_cb = cb.get(asset)
        if asset_cb is None or not asset_cb.ts_us:
            continue
        strike_d = Decimal(strike)
        close_ts_us = int(close_ts) * 1_000_000

        trades = load_trades_for_market(conn, ticker)
        if not trades:
            continue

        # Fresh pure_lag instance per market; we feed reference ticks lazily.
        lag = PureLagStrategy(PureLagConfig(
            move_threshold_bps=Decimal("5"),
            min_edge_bps_after_fees=Decimal("100"),
            time_window_seconds=(0, 900),  # backtest all windows
            min_book_depth_usd=Decimal("0"),
        ))
        # Feed the 60s of ticks before the earliest trade (so lag has a window).
        first_trade_ts = trades[0][0]
        lo_feed = first_trade_ts - 60_000_000
        feed_lo_i = bisect_ri(asset_cb.ts_us, lo_feed)
        feed_hi_i = bisect_ri(asset_cb.ts_us, first_trade_ts)
        for i in range(feed_lo_i, feed_hi_i):
            # Monkey-patch now() per tick to preserve ordering.
            lag._now_us = (lambda t=asset_cb.ts_us[i]: t)
            lag.record_reference_tick(asset, asset_cb.price[i])

        for trade_ts_us, trade_price, taker_side in trades:
            # Only score trades within [close-900, close] window.
            time_remaining_s = Decimal((close_ts_us - trade_ts_us) / 1_000_000)
            if time_remaining_s < 0 or time_remaining_s > 900:
                continue
            bucket = bucket_label(time_remaining_s)
            if bucket is None:
                continue

            spot = asset_cb.latest_at(trade_ts_us)
            if spot is None:
                continue

            # Partial observation window: [close-60, min(now, close)].
            obs_start = close_ts_us - 60_000_000
            obs_end = min(trade_ts_us, close_ts_us)
            if obs_end > obs_start:
                obs_avg, n_obs = asset_cb.avg_in_window(obs_start, obs_end)
                observed_s = Decimal((obs_end - obs_start) / 1_000_000)
                if n_obs == 0:
                    obs_avg = spot
                    observed_s = ZERO
            else:
                obs_avg = spot
                observed_s = ZERO

            # Map Kalshi trade to implied yes/no ask prices. `taker_side`
            # tells us which side paid — treat `trade_price` as that side's
            # ask. The opposite side's ask is `1 - trade_price`.
            if taker_side == "yes":
                yes_ask = trade_price
                no_ask = ONE - trade_price
            else:
                no_ask = trade_price
                yes_ask = ONE - trade_price

            # --- Stat model ---
            try:
                p_stat, _ = stat.price(
                    asset=asset, strike=strike_d, comparator=comparator,
                    reference_price=spot, reference_60s_avg=spot,
                    time_remaining_s=time_remaining_s,
                )
                s_side, s_edge, s_fill = choose_side(p_stat, yes_ask, no_ask)
                if s_side is not None and s_edge * BPS >= Decimal("100"):
                    decisions.append(DecisionScore(
                        model="stat_model", asset=asset, bucket=bucket,
                        side=s_side, fill_price=s_fill, outcome=settled,
                        pnl_usd=score_pnl(s_side, s_fill, settled, FEE_BPS),
                        p_yes_predicted=p_stat,
                    ))
            except NotImplementedError:
                pass

            # --- Partial-avg model ---
            try:
                p_par, _ = partial.price(
                    asset=asset, strike=strike_d, comparator=comparator,
                    reference_price=spot, reference_60s_avg=spot,
                    time_remaining_s=time_remaining_s,
                    observed_window_s=observed_s,
                    observed_window_avg=obs_avg,
                )
                pa_side, pa_edge, pa_fill = choose_side(p_par, yes_ask, no_ask)
                if pa_side is not None and pa_edge * BPS >= Decimal("100"):
                    decisions.append(DecisionScore(
                        model="partial_avg", asset=asset, bucket=bucket,
                        side=pa_side, fill_price=pa_fill, outcome=settled,
                        pnl_usd=score_pnl(pa_side, pa_fill, settled, FEE_BPS),
                        p_yes_predicted=p_par,
                    ))
            except NotImplementedError:
                pass

            # --- Pure-lag model ---
            # Feed any missed reference ticks up to trade_ts.
            lo_idx = bisect_ri(asset_cb.ts_us, lag._last_fed_ts_us if hasattr(lag, "_last_fed_ts_us") else 0)
            hi_idx = bisect_ri(asset_cb.ts_us, trade_ts_us)
            for i in range(lo_idx, hi_idx):
                lag._now_us = (lambda t=asset_cb.ts_us[i]: t)
                lag.record_reference_tick(asset, asset_cb.price[i])
            lag._last_fed_ts_us = trade_ts_us  # type: ignore[attr-defined]
            lag._now_us = (lambda t=trade_ts_us: t)

            quote = _fake_quote(
                ticker=ticker, series=series, strike=strike_d,
                comparator=comparator, yes_ask=yes_ask, no_ask=no_ask,
                time_remaining_s=time_remaining_s, trade_ts_us=trade_ts_us,
                spot=spot, obs_avg=obs_avg,
            )
            lag_opp = lag.evaluate(quote, asset=asset)
            if lag_opp is not None:
                decisions.append(DecisionScore(
                    model="pure_lag", asset=asset, bucket=bucket,
                    side=lag_opp.recommended_side,
                    fill_price=lag_opp.hypothetical_fill_price,
                    outcome=settled,
                    pnl_usd=score_pnl(
                        lag_opp.recommended_side,
                        lag_opp.hypothetical_fill_price, settled, FEE_BPS,
                    ),
                    p_yes_predicted=lag_opp.p_yes,
                ))

        processed += 1
        if processed % 50 == 0:
            logger.info("  processed %d markets, %d decisions so far",
                        processed, len(decisions))

    logger.info("total decisions: %d", len(decisions))
    report = build_report(decisions, markets_processed=processed)
    out = Path("docs/three_model_backtest_results.md")
    out.write_text(report, encoding="utf-8")
    logger.info("wrote %s", out)


def choose_side(
    p_yes: Decimal, yes_ask: Decimal, no_ask: Decimal,
) -> tuple[str | None, Decimal, Decimal]:
    """Pick the side with highest edge. Returns (side, edge, fill_price).

    edge_yes = p_yes - yes_ask - fee
    edge_no  = (1 - p_yes) - no_ask - fee
    """
    fee = FEE_BPS / BPS
    edge_yes = p_yes - yes_ask - fee
    edge_no = (ONE - p_yes) - no_ask - fee
    if edge_yes > edge_no and edge_yes > ZERO:
        return "yes", edge_yes, yes_ask
    if edge_no > ZERO:
        return "no", edge_no, no_ask
    return None, ZERO, ZERO


def _fake_quote(*, ticker, series, strike, comparator, yes_ask, no_ask,
                time_remaining_s, trade_ts_us, spot, obs_avg):
    """Build a MarketQuote for PureLagStrategy.evaluate()."""
    from core.models import MarketQuote
    return MarketQuote(
        venue="kalshi", market_ticker=ticker, series_ticker=series,
        event_ticker=series + "-EV",
        best_yes_ask=yes_ask, best_no_ask=no_ask,
        best_yes_bid=yes_ask - Decimal("0.01"),
        best_no_bid=no_ask - Decimal("0.01"),
        book_depth_yes_usd=Decimal("1000"),
        book_depth_no_usd=Decimal("1000"),
        fee_bps=FEE_BPS, expiration_ts=Decimal(trade_ts_us / 1_000_000),
        strike=strike, comparator=comparator,
        reference_price=spot, reference_60s_avg=obs_avg,
        time_remaining_s=time_remaining_s, quote_timestamp_us=trade_ts_us,
    )


def bisect_ri(arr: list[int], target: int) -> int:
    return bisect_right(arr, target)


def build_report(
    decisions: list[DecisionScore], *, markets_processed: int,
) -> str:
    by_model: dict[str, list[DecisionScore]] = defaultdict(list)
    for d in decisions:
        by_model[d.model].append(d)

    lines: list[str] = [
        "# Three-Model Backtest Results",
        "",
        "**Research date:** 2026-04-20",
        "",
        f"Markets processed: {markets_processed:,}  —  total decisions: {len(decisions):,}",
        "",
        "Each decision = one Kalshi historical trade that the model would have taken (edge ≥ 100 bps after fees) at the recorded trade price. P/L scored against the settled_result with a 35 bps taker fee.",
        "",
        "---",
        "",
        "## Aggregate per model",
        "",
        "| model | decisions | wins | win-rate | mean-edge (bps) | total P/L ($) | $/decision |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for model in ("stat_model", "partial_avg", "pure_lag"):
        ds = by_model.get(model, [])
        lines.append(_summary_row(model, ds))
    lines.extend([
        "",
        "## Per asset × model",
        "",
        "| model | asset | decisions | win-rate | total P/L ($) | $/decision |",
        "|---|---|---:|---:|---:|---:|",
    ])
    for model in ("stat_model", "partial_avg", "pure_lag"):
        by_asset = defaultdict(list)
        for d in by_model.get(model, []):
            by_asset[d.asset].append(d)
        for asset in sorted(by_asset):
            lines.append(_sub_row(model, asset, by_asset[asset]))
    lines.extend([
        "",
        "## Per time-bucket × model",
        "",
        "| model | bucket | decisions | win-rate | total P/L ($) | $/decision |",
        "|---|---|---:|---:|---:|---:|",
    ])
    for model in ("stat_model", "partial_avg", "pure_lag"):
        by_bucket = defaultdict(list)
        for d in by_model.get(model, []):
            by_bucket[d.bucket].append(d)
        for label, _, _ in TIME_BUCKETS:
            ds = by_bucket.get(label, [])
            if not ds:
                continue
            lines.append(_sub_row(model, label, ds))
    lines.extend([
        "",
        "---",
        "",
        "## Methodology",
        "",
        "- `kalshi_historical_markets` filtered to settled 15-min BTC/ETH/SOL/XRP/DOGE/BNB/HYPE markets whose `close_ts` falls inside the Coinbase-trade coverage window.",
        "- For every `kalshi_historical_trades` event on each market, we reconstruct (a) current spot from the latest `coinbase_trades` entry and (b) the partial close-60s-avg from the Coinbase ticks in `[close-60s, min(now, close)]`.",
        "- Each model sees the same (spot, strike, comparator, time_remaining, observed_window) tuple and decides whether to take the trade at the recorded Kalshi price. Models diverge on `p_yes` and therefore on which side they'd take and whether the edge clears 100 bps.",
        "- P/L uses a 35 bps taker fee. A winning yes pays `1 - fill_price`; a losing yes loses `fill_price` plus fee.",
        "- `pure_lag` fed sub-second Coinbase ticks to populate its rolling window; `min_edge_bps_after_fees=100` matches the other two models for comparability.",
        "",
        "## Caveats",
        "",
        "1. **Book reconstruction is approximate** — we treat each historical trade as a quote we could have hit at the recorded price. The actual book could have had wider spread; this assumption is generous to all three models equally.",
        "2. **Coinbase ≠ CF Benchmarks** — the partial-avg's observed window is computed from Coinbase trades, not the actual CF RTI. For a tight backtest this is the closest proxy; real-life we'd use the basket (Coinbase + Kraken + Binance).",
        "3. **pure_lag is sensitive to feed lag.** The historical `coinbase_trades` are post-WS timestamps, so the measured lag is ~0 in backtest — this understates the real-life edge. Treat `pure_lag` rows here as an upper-bound on the strategy; live data has given weaker numbers.",
    ])
    return "\n".join(lines) + "\n"


def _summary_row(model: str, ds: list[DecisionScore]) -> str:
    if not ds:
        return f"| {model} | 0 | 0 | – | – | – | – |"
    wins = sum(1 for d in ds if _is_win(d))
    pnl = sum((d.pnl_usd for d in ds), ZERO)
    mean_edge = ZERO  # not computed here — already filtered at 100 bps floor
    return (
        f"| {model} | {len(ds):,} | {wins:,} | "
        f"{wins / len(ds) * 100:.1f}% | – | "
        f"{float(pnl):+.2f} | {float(pnl) / len(ds):+.4f} |"
    )


def _sub_row(model: str, label: str, ds: list[DecisionScore]) -> str:
    wins = sum(1 for d in ds if _is_win(d))
    pnl = sum((d.pnl_usd for d in ds), ZERO)
    return (
        f"| {model} | {label} | {len(ds):,} | "
        f"{wins / len(ds) * 100:.1f}% | "
        f"{float(pnl):+.2f} | {float(pnl) / len(ds):+.4f} |"
    )


def _is_win(d: DecisionScore) -> bool:
    return (
        (d.side == "yes" and d.outcome == "yes") or
        (d.side == "no" and d.outcome == "no")
    )


if __name__ == "__main__":
    run_backtest()
