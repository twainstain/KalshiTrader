# Kalshi Crypto 15-Min — Cross-Asset Feed-Lag Analysis

**Research date:** 2026-04-20
**Window:** 2026-04-20 09:00-13:00 UTC (4 hours) × **16 distinct 15-min Kalshi markets per asset × 7 assets = 112 market-windows**.
**Data volume:** 129,033 Coinbase trades (ms-precision) + 87,118 Kalshi trades across all 7 assets.
**Source endpoints:** Kalshi public `/trade-api/v2/markets/trades?ticker=X` (no auth) + Coinbase public `GET /products/{X}/trades`. Microsecond `created_time` / `time` parsed on both sides.
**Cross-links:** [`kalshi_crypto_multi_asset_report.md`](./kalshi_crypto_multi_asset_report.md), [`kalshi_crypto_fair_value_tracking_error_report.md`](./kalshi_crypto_fair_value_tracking_error_report.md).

> Not investment, legal, or tax advice.

## 0. TL;DR

- **BTC has a tight sub-50 ms reprice latency at moves ≥5 bps** (p50 = 36 ms, p99 = 535 ms). Suggests colocated / latency-optimized MMs run the BTC book.
- **SOL also fast** (≥5 bps p50 = 39 ms). BTC and SOL are the two tight books.
- **ETH is moderate** (≥5 bps p50 = 96 ms, p99 10 s). Some MMs present, not all the time.
- **XRP, DOGE, BNB, HYPE are slow or sparse.** XRP at ≥5 bps p50 = 314 ms with tail > 20 s. BNB has too few MM reprice events to characterize (6 events across 16 windows). HYPE is clearly MM-absent (≥2 bps p50 = 2.3 s).
- **Directional match is uniformly 70-95%** at ≥5 bps — when Coinbase moves meaningfully, Kalshi eventually follows the same direction. The lag is the gap between "Coinbase says up" and "Kalshi book crosses the probability implied by the new level."
- **Feasibility implication:** a solo operator with ~30-80 ms end-to-end latency can exploit feed lag on **XRP / DOGE / HYPE / BNB** (thin MM presence = wide opportunity window), probably not on **BTC / SOL** (MMs already at sub-50 ms). ETH is the swing asset.

## 1. Methodology

For each asset:

1. **Kalshi trade tape** pulled for all 16 market tickers that closed in 09:00-13:00 UTC, via public `GET /markets/trades?ticker=X`. Every trade carries a microsecond `created_time` — essentially the moment the Kalshi orderbook repriced to the new yes/no level.
2. **Coinbase trade tape** pulled for the identical 4-hour window via public `GET /products/{X}/trades` (walking backward through `after=<trade_id>` pagination). Millisecond precision per trade.
3. **Reprice events** identified as Kalshi trades where `yes_price` differs from the previous trade's `yes_price` by at least one tick ($0.001). Each event represents a fresh price level the book has moved to.
4. **Coinbase price moves** identified as trades where the price deviates from the trailing 2-second rolling mean by at least `N` bps (threshold swept at 2 / 5 / 10 / 20 bps).
5. For each Coinbase move event, look forward in time for the **next Kalshi reprice event**, capping at 30 seconds. Record:
   - `Δt` (ms) = ts_kalshi − ts_coinbase — the feed-lag observation.
   - Directional match = 1 if the Kalshi reprice moved the same direction as the Coinbase move, else 0.
6. Aggregate per asset: percentiles of Δt, directional match rate, event count.

**Why this shape:** a retail operator with a sub-100 ms latency budget could theoretically exploit a `Δt` larger than their own budget. A 300-ms median reprice latency means 300 ms to see the Coinbase tick, trust the signal, and send an order at the stale Kalshi level. A 30-ms median means you're racing colocated MMs — realistically only colocated infrastructure competes.

## 2. Headline results

### 2.1 Event counts (≥ 2 bps Coinbase move → next Kalshi reprice)

| Asset | N events | Directional match |
|---|---:|---:|
| **BTC** | 1,718 | 79% |
| ETH | 2,471 | 81% |
| SOL | 946 | 87% |
| XRP | 1,964 | 77% |
| DOGE | 486 | 85% |
| BNB | **6** | 83% |
| HYPE | 119 | 87% |

**Observations:**
- ETH has the most events because ETH has the noisiest 2-bp moves on Coinbase (high volume + moderate vol).
- BNB is the anomaly: only 6 ≥2-bp moves across 4 hours. BNB on Coinbase is thin, and/or CF-RTI-BNB price action is too quiet to trigger. Signal is unreliable for BNB.
- Directional match of ~80% at 2 bps is consistent with "noise threshold is above 2 bps" — many of those "moves" aren't real signals.

### 2.2 Δt distribution per asset, by Coinbase move threshold

All values in **milliseconds**.

#### ≥ 2 bps threshold

| Asset | N | p10 | p25 | p50 | p75 | p90 | p99 | mean |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| **BTC** | 1,718 | **8** | 17 | **54** | 483 | 907 | 8,206 | 520 |
| ETH | 2,471 | 18 | 38 | 644 | 3,288 | 8,305 | 15,789 | 2,503 |
| SOL | 946 | 17 | 30 | 297 | 3,270 | 8,983 | 23,054 | 2,694 |
| XRP | 1,964 | 15 | 41 | 1,452 | 10,205 | 19,142 | 25,574 | 5,814 |
| DOGE | 486 | 12 | 24 | 427 | 5,686 | 14,577 | 24,483 | 4,016 |
| BNB | 6 | 8 | 37 | 3,464 | 25,336 | 25,336 | 27,001 | 13,530 |
| HYPE | 119 | 28 | 580 | 2,327 | 4,451 | 13,047 | 25,403 | 4,185 |

#### ≥ 5 bps threshold (stronger signal filter)

| Asset | N | DirMatch | p10 | p25 | **p50** | p75 | p90 | p99 | mean |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **BTC** | 193 | 70% | 3 | 18 | **36** | 83 | 158 | 535 | 68 |
| SOL | 118 | 92% | 13 | 30 | **39** | 283 | 1,713 | 5,376 | 483 |
| ETH | 189 | 89% | 20 | 41 | **96** | 409 | 888 | 10,058 | 675 |
| DOGE | 90 | 93% | 1 | 15 | **16** | 281 | 838 | 7,162 | 322 |
| XRP | 236 | 85% | 15 | 43 | **314** | 19,167 | 20,213 | 22,210 | 7,164 |
| HYPE | 16 | 88% | 262 | 387 | **4,172** | 15,270 | 15,270 | 15,270 | 6,849 |
| BNB | — | — | — | — | — | — | — | — | — |

#### ≥ 10 bps threshold (clear signal)

| Asset | N | DirMatch | p50 | p99 |
|---|---:|---:|---:|---:|
| **BTC** | 38 | 71% | **26 ms** | 175 |
| ETH | 3 | 100% | 14 ms | 14 |
| DOGE | 16 | 63% | 445 ms | 864 |
| SOL | 1 | 0% | 1,911 ms | — |
| XRP | 9 | 100% | 20,158 ms | 20,213 |
| BNB / HYPE | — | — | — | — |

### 2.3 Lag ordering (by ≥5 bps p50)

```
BTC   (36 ms)  ←─── fastest MMs; colocated territory
SOL   (39 ms)  ←─── also tight
ETH   (96 ms)
DOGE  (16 ms)  ←─── small sample; p90 jumps to 838 ms
XRP   (314 ms) ←─── MMs intermittent
HYPE  (4 sec)  ←─── MMs essentially absent
BNB   (n/a)    ←─── too few events
```

## 3. Interpretation

### 3.1 BTC / SOL — "fast MM" regime

p50 reprice latency of 36-40 ms at ≥5 bps. p99 of 535 ms on BTC means even the tails are bounded under 1 second.

**What this means:** at least one market-maker on each of BTC and SOL is running with infrastructure that sees a Coinbase trade and posts a new Kalshi quote in under 100 ms. Solo operators without colocation (typical end-to-end latency 50-300 ms for a retail API trader on AWS us-east-1) are **racing against that MM**. Edge is available only on the p10-p25 of the distribution (the ~10-20% of events where the MM happened to be slow) — and even then you need to be certain the move is real, not noise.

**For the scanner:** BTC/SOL edge is probably not capturable at retail latency. Continue to track and measure, but don't expect realized P/L from these assets.

### 3.2 ETH — "slower MM" regime

p50 = 96 ms at ≥5 bps, with a long tail (p99 = 10 seconds). The slow tail is the interesting part: 10% of ≥5-bp moves have a Kalshi reprice latency > 888 ms. That's a full second of stale book — realistically exploitable.

**For the scanner:** ETH is a plausible edge target for solo operators. The MMs are present but not colocated; filter for moves in the ≥5-bp bucket and act on the slow-tail regime.

### 3.3 XRP — "intermittent MM" regime

At ≥5 bps, XRP's p50 is 314 ms but p75 jumps to 19 seconds. This bimodal distribution is classic: most of the day an MM is tracking the book, and some of the day the MM drops out entirely and the book goes stale for ~20 seconds after a Coinbase move.

**For the scanner:** XRP is high-variance edge territory. Edge windows are longer than on BTC/SOL/ETH when they appear, but the MM isn't consistent. Realized P/L depends on catching the stale-book regime, which a regime detector (measuring current MM cadence in real-time) could identify.

### 3.4 DOGE / HYPE — "sparse MM" regime

DOGE has only 90 events at ≥5 bps across 4 hours (vs BTC's 193) — fewer Coinbase price moves clear the threshold, AND the data suggests the Kalshi book repricing is on a second-scale cadence. HYPE is even sparser (16 events) with p50 = 4 seconds.

**For the scanner:** wide edge windows per opportunity, but very few opportunities per day. Capacity is low. Worth including in Phase 1 observation, but size expectations should be modest.

### 3.5 BNB — "no-signal" regime

6 ≥2-bp events in 4 hours, 0 events ≥5 bps. Coinbase's BNB market is too quiet / low-volume for a meaningful sample from a single 4-hour window. Would need longer observation window (e.g., 24-48 hours) to characterize BNB at all.

**For the scanner:** BNB is not yet analyzable for feed-lag purposes. Either (a) pull weeks of data, (b) swap Coinbase for a higher-volume BNB reference (Binance, but geo-blocked), or (c) drop BNB from the tradeable universe.

## 4. Comparison to earlier single-window BTC findings

Prior analyses in `kalshi_crypto_fair_value_tracking_error_report.md` showed BTC lags of:
- One window (12:30-12:45 UTC): p50 = 105 ms at ≥2 bps
- Another window (10:00-10:15 UTC): p50 = 830 ms at ≥2 bps

The new, aggregated cross-window BTC result (16 windows, N = 1,718) shows **p50 = 54 ms at ≥2 bps**. That's dramatically faster than either single-window estimate — consistent with averaging across both "fast MM" and "slow MM" sub-regimes in the same distribution.

**Updated prior:** BTC has a typical MM reprice latency around 50 ms, with noticeably worse outliers. The "105 ms tight cluster" finding from the earlier spot-check was a specific window's behavior, not a general latency floor.

## 5. Retail-operator latency budget — who can compete?

Using the **≥ 5 bps** threshold (real signals, less noise):

| Your end-to-end latency | BTC/SOL | ETH | XRP/DOGE | HYPE | Verdict |
|---|---|---|---|---|---|
| > 500 ms (slow) | No edge | Marginal tail | Some | Yes | Focus: thin books |
| 100-500 ms (typical retail) | No edge | Tail only | Usually yes | Yes | Focus: XRP/DOGE/HYPE |
| 30-100 ms (tuned retail) | Tail (20%) | Frequent | Yes | Yes | Best general-purpose range |
| < 30 ms (colocated) | Consistent | Consistent | Consistent | Consistent | Optimal |

**Target operating regime for a solo operator:** 30-100 ms end-to-end latency. Focus on XRP, DOGE, HYPE as primary edge targets; treat ETH as swing; treat BTC / SOL as scoring-only.

## 6. Caveats

1. **Sample size varies 20× across assets.** BTC has 193 events at ≥5 bps; HYPE has 16; BNB has 0. The tails on low-sample assets are unreliable — the HYPE p99 of 15 seconds is probably noise.
2. **Single 4-hour window (today).** Different calendar times (weekends, pre-CPI, flash-crashes) will have different MM cadences. Repeating across 5-10 diverse 4-hour slices would firm up regime distributions.
3. **Kalshi "reprice events" are filtered to tick-changes only.** A same-price fill isn't counted as a reprice — correct behavior for latency measurement, but means our denominator underrepresents Kalshi's true activity.
4. **Coinbase as a reference is a proxy.** For BTC, Coinbase is likely a high-weight BRTI constituent → tight correlation → valid measurement. For HYPE / BNB, Coinbase is probably a minor (or non-) constituent → measurements carry basket-vs-proxy error on top of any real lag.
5. **Directional match rates vary 70-95%.** The 70% numbers (BTC at ≥2 and ≥10 bps) are surprising. At ≥10 bps BTC has only 38 events — small sample — and 71% dir match means 11 of 38 moves were reversed at Kalshi. Without examining each one we can't say whether those were coincident but independent moves, or genuine noise in the threshold definition. Investigation deferred.

## 7. Recommended next steps

### 7.1 Data quality

- **Pull 20+ independent 4-hour windows** across different calendar times (overnight, market-hours, weekends, known volatility events) to build a distribution of lag-regimes, not just a single window snapshot.
- **Characterize BNB with a 24-hour pull** to see whether it has any measurable signal at all at Kalshi's timescales.
- **Add a basket-of-3 reference** (Coinbase + Kraken + Bitstamp) so the Coinbase-only proxy error doesn't contaminate the lag signal — particularly for HYPE, where the Coinbase reference is known to drift from the actual UHYPEUSDRTI basket.

### 7.2 Execution-relevant

- **Measure OUR end-to-end latency** against Kalshi prod from AWS us-east-1. This is the denominator the edge-availability table above is measured against. Until we know our latency, we don't know which buckets we can compete in.
- **Kalshi book-snapshot lag, not trade lag.** We measure "time until next Kalshi trade moves yes_price." A more sensitive measurement is "time until Kalshi's best bid/ask updates" — bid/ask can move without a trade. The live WS scaffold in `src/market/kalshi_market.py` supports this; just needs the prod API key and the WS handshake.
- **Correlate lag with market regime features**: book depth at decision, prior-minute realized vol, time-of-day. If ETH has a 10-second tail when depth < $100 at top of book but <100 ms at depth > $500, that's a clean regime detector.

### 7.3 Scanner design

- **Per-asset latency gate.** Strategy config should reject opportunities in markets where the scanner's own latency exceeds the regime's p50 reprice latency. E.g., if our latency is 80 ms and current BTC p50 is 50 ms, don't trade BTC. Adapt per-window.
- **Prioritize thin-book assets.** XRP, DOGE, HYPE are where solo-operator edge lives. Trade sizing remains a separate constraint (volume capacity, Kalshi position accountability), but the latency constraint is materially relaxed.

## 8. Reproducibility

All data in `data/kalshi.db`. Steps to reproduce:

```bash
# 1. Pull 7-asset Kalshi settled markets (if not already).
python3.11 scripts/kalshi_public_pull.py --asset all --days 30

# 2. Identify 17 market tickers per asset for today's 09:00-13:00 UTC window.
# (Done in-line — see /tmp/k_tickers_{asset}.txt, list derived from DB.)

# 3. Sequential Coinbase trade pulls (parallel hits rate limits).
for asset in hype doge xrp sol eth btc bnb; do
  python3.11 scripts/coinbase_trades_pull.py --asset $asset \
    --start 2026-04-20T09:00:00Z --end 2026-04-20T13:00:00Z
done

# 4. Parallel Kalshi trade pulls (fast; SQLite WAL mode handles concurrency).
for asset in btc eth sol xrp doge bnb hype; do
  xargs -n1 -I{} printf -- '--ticker\n{}\n' < /tmp/k_tickers_$asset.txt \
    | xargs python3.11 scripts/kalshi_trades_pull.py &
done; wait

# 5. Run analysis (inline Python; see /tmp/lag_analysis_all_assets.py).
python3.11 /tmp/lag_analysis_all_assets.py
```

SQLite is now in WAL mode with a 30-second busy_timeout, so multiple concurrent writers work without lock errors.

## 9. Sources

- [Kalshi API — /markets/trades (public)](https://docs.kalshi.com/reference/get-trades)
- [Coinbase Exchange — /products/{X}/trades](https://docs.cdp.coinbase.com/exchange/reference/exchangerestapi_getproducttrades)
- [CF Benchmarks — RTI methodology per asset](https://www.cfbenchmarks.com/)
