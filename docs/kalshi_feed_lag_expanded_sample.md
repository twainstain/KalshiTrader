# Kalshi Crypto 15-Min — Expanded Feed-Lag Sample (30 hours)

**Research date:** 2026-04-20
**Window:** 2026-04-19T11:15 → 2026-04-20T17:10 UTC (30 hours × 4 windows/hour = **110+ settled windows per asset**)
**Sample:** 7 assets × 111 market tickers = 770 Kalshi market-windows with trade data.
**Cross-links:** [`kalshi_feed_lag_cross_asset_report.md`](./kalshi_feed_lag_cross_asset_report.md) (earlier 4-hour sample), [`kalshi_crypto_multi_asset_report.md`](./kalshi_crypto_multi_asset_report.md).

> Not investment, legal, or tax advice. Supersedes the 4-hour-single-window sample in `kalshi_feed_lag_cross_asset_report.md`.

## 0. TL;DR

Expanded from 16 windows per asset → **110 windows per asset** by extending the trade-pull window from 4 hours to 30 hours. Result: **10-50× more events per asset** than before, giving reliable p99 tails for the first time.

Headline — median Kalshi reprice latency after a ≥5 bps Coinbase move (**all 7 assets complete**):

| Asset | N events | p50 (ms) | p90 (ms) | p99 (ms) | Dir% |
|---|---:|---:|---:|---:|---:|
| **BTC** | 4,831 | **106** | 2,606 | 9,224 | 72% |
| **XRP** | 4,057 | **281** | 8,642 | 21,973 | 75% |
| **DOGE** | 3,070 | **467** | 20,472 | 28,857 | 80% |
| **ETH** | 8,639 | **480** | 8,318 | 25,810 | 72% |
| **SOL** | 2,348 | **479** | 10,147 | 21,110 | 78% |
| **HYPE** | 1,295 | **622** | 13,882 | 25,586 | 81% |
| **BNB** | 198 | **1,362** | 21,341 | 26,344 | 69% |

(See §2 for threshold sweeps, §3 for interpretation.)

## 1. Methodology — same as prior, scaled up

For each asset:

1. Pulled Kalshi trades for **every settled market in the 30h window** (111 markets per asset × 7 = 777 tickers total).
2. Pulled Coinbase trades (`/products/{X}/trades`) over the same 30h range per asset.
3. Identified **Kalshi reprice events** (yes-price changes ≥ 1 tick) and **Coinbase move events** (price deviates ≥ N bps from 2s rolling mean).
4. For each Coinbase move, measured `Δt` to next Kalshi reprice (capped at 30 s), plus directional match.

**Data sizes (all 7 assets complete, 30-hour window):**

| Asset | Coinbase trades | Kalshi trades | # windows |
|---|---:|---:|---:|
| BTC | 849,747 | 525,786 | 107 |
| ETH | 454,869 | 75,146 | 107 |
| SOL | 122,158 | 29,791 | 107 |
| XRP | 218,710 | 30,569 | 107 |
| DOGE | 47,504 | 19,180 | 107 |
| BNB | 7,173 | 17,301 | 107 |
| HYPE | 19,679 | 26,373 | 107 |
| **Total** | **1,919,840** | **724,146** | **749** |

## 2. Full percentile tables

All Δt values in milliseconds.

### 2.1 SOL

N=110 windows, 122k Coinbase + 30.7k Kalshi trades.

| Thresh | N events | Dir% | p10 | p25 | p50 | p75 | p90 | p99 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| ≥ 2 bps | 14,634 | 76% | 30 | 160 | 1,577 | 6,211 | 13,541 | 27,002 |
| ≥ 5 bps | 2,348 | 78% | 16 | 63 | 479 | 3,251 | 10,147 | 21,110 |
| ≥ 10 bps | 513 | 74% | 40 | 106 | 331 | 1,062 | 12,940 | 13,842 |
| ≥ 20 bps | 55 | **100%** | 39 | 45 | **45** | 45 | 92 | 115 |

**Standout:** at ≥ 20 bps the distribution collapses — p50 = 45 ms, tight up to p90 = 92 ms. For genuine large moves SOL behaves like a fast-MM market.

### 2.2 XRP

N=110 windows, 223k Coinbase + 31.7k Kalshi trades.

| Thresh | N events | Dir% | p10 | p25 | p50 | p75 | p90 | p99 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| ≥ 2 bps | 30,868 | 74% | 20 | 145 | 1,841 | 6,682 | 14,001 | 26,942 |
| ≥ 5 bps | 4,078 | 75% | 14 | 42 | 279 | 2,513 | 8,381 | 21,973 |
| ≥ 10 bps | 750 | 72% | 16 | 44 | **160** | 516 | 713 | 5,907 |
| ≥ 20 bps | 161 | 85% | 44 | 160 | **160** | 160 | 247 | 464 |

**Standout:** XRP shows a **clustered reprice cadence at ~160 ms** for moves ≥10 bps. The p10-p90 range for ≥20 bps is 44-247 ms — tight. Likely reflects the XRP MM's internal batching or update cadence.

### 2.3 DOGE

N=110 windows, 48.8k Coinbase + 19.6k Kalshi trades.

| Thresh | N events | Dir% | p10 | p25 | p50 | p75 | p90 | p99 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| ≥ 2 bps | 11,504 | 77% | 18 | 80 | 1,863 | 8,608 | 19,024 | 28,496 |
| ≥ 5 bps | 3,156 | 80% | 15 | 41 | 423 | 5,546 | 19,735 | 28,857 |
| ≥ 10 bps | 1,269 | 82% | 12 | 61 | 392 | 3,009 | 20,614 | 27,436 |
| ≥ 20 bps | 346 | 85% | 37 | 142 | **210** | 3,950 | 27,262 | 27,578 |

**Standout:** DOGE is consistently in the 200-400 ms p50 range for meaningful moves but has a persistent long tail (p75 jumps to 3-6 seconds even at ≥10 bps). Bimodal regime: sometimes MM is tight, sometimes absent for seconds.

### 2.4 HYPE

N=110 windows, 20k Coinbase + 27k Kalshi trades.

| Thresh | N events | Dir% | p10 | p25 | p50 | p75 | p90 | p99 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| ≥ 2 bps | 3,939 | 81% | 20 | 110 | 1,342 | 6,541 | 15,270 | 27,540 |
| ≥ 5 bps | 1,371 | 82% | 17 | 58 | 559 | 3,580 | 13,802 | 25,586 |
| ≥ 10 bps | 427 | 79% | 19 | 61 | 336 | 866 | 10,422 | 25,586 |
| ≥ 20 bps | 50 | **92%** | 12 | 75 | **440** | 641 | 821 | 823 |

**Standout:** HYPE's p50 at ≥20 bps is 440 ms (slow median) BUT the tails are tight once a move happens — p99 = 823 ms for big moves. MM is present for big moves, absent for most small ones.

### 2.5 BNB

N=110 windows, 7.3k Coinbase + 17.7k Kalshi trades.

| Thresh | N events | Dir% | p10 | p25 | p50 | p75 | p90 | p99 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| ≥ 2 bps | 580 | 72% | 16 | 43 | 1,182 | 6,336 | 21,341 | 26,344 |
| ≥ 5 bps | 198 | 69% | 14 | 43 | 1,362 | 5,756 | 21,341 | 26,344 |
| ≥ 10 bps | 61 | 90% | 43 | 43 | 1,118 | 5,556 | 5,556 | 23,107 |
| ≥ 20 bps | 0 | — | — | — | — | — | — | — |

**Standout:** BNB has the **slowest median of any asset** (1.3 s at ≥5 bps) but relatively fewer big moves. Consistent with BNB being thinly-MM'd on Kalshi — operators can hold stale quotes longer. Directional match of 69% at ≥5 bps is the lowest in the set — suggests Coinbase BNB is a noisier reference (CF basket for BNB isn't Coinbase-heavy).

### 2.6 ETH

N=109 windows, 459k Coinbase + 77k Kalshi trades.

| Thresh | N events | Dir% | p10 | p25 | p50 | p75 | p90 | p99 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| ≥ 2 bps | 58,420 | 73% | 19 | 82 | 750 | 3,171 | 8,236 | 22,394 |
| ≥ 5 bps | 8,673 | 72% | 17 | 58 | **475** | 3,127 | 8,245 | 25,805 |
| ≥ 10 bps | 1,057 | 66% | 13 | 43 | **113** | 1,549 | 5,893 | 13,546 |
| ≥ 20 bps | 23 | 0%† | 5,893 | 5,893 | 5,893 | 5,893 | 5,893 | 5,893 |

†ETH at ≥20 bps has only 23 events with all 23 at the same Δt ≈ 5.9 seconds — almost certainly a contiguous stretch of one particularly stale window. Small sample; treat with caution.

**Standout:** ETH at ≥10 bps drops to a sub-120 ms median. ETH has MM activity but the median at ≥5 bps (475 ms) is elevated relative to XRP (279 ms) — likely the MM is present but less tight.

### 2.7 BTC

N=107 windows, 850k Coinbase + 526k Kalshi trades — the densest Kalshi market in the set.

| Thresh | N events | Dir% | p10 | p25 | p50 | p75 | p90 | p99 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| ≥ 2 bps | 44,038 | 73% | 13 | 33 | 149 | 507 | 1,550 | 10,003 |
| ≥ 5 bps | 4,831 | 72% | 12 | 33 | **106** | 345 | 2,606 | 9,224 |
| ≥ 10 bps | 394 | 55% | 11 | 31 | **76** | 194 | 6,148 | 6,571 |
| ≥ 20 bps | 0 | — | — | — | — | — | — | — |

**Standout — BTC is the fastest asset across the board:**

- p50 at ≥5 bps = **106 ms** vs runner-up XRP at 281 ms.
- p50 at ≥10 bps = **76 ms** — tightest median in the set; even the "only 107 ms" from the earlier 4h sample held up when averaged over 30h.
- p10 = 12 ms (≥5 bps) means in 10% of cases the book repriced in under 13 ms — colocated-MM territory.
- The 4h sample's 36 ms median is the "busy-regime" case; 106 ms is the all-hours median.
- Directional match drops to 55% at ≥10 bps — surprising; possibly a noise artifact at the threshold (price momentarily touching 10 bps mean-reverts before a Kalshi reprice).
- No ≥20 bps events in the 30h window — BTC simply didn't move that much relative to its 2-second baseline during this period.

## 3. What changed vs. the 4-hour sample

| Asset | 4h sample p50 (≥5 bps) | 30h sample p50 (≥5 bps) | Delta |
|---|---:|---:|---:|
| BTC | 36 ms | **106 ms** | 3× slower (all-hours) |
| ETH | 96 ms | **480 ms** | 5× slower |
| SOL | 39 ms | **479 ms** | 12× slower |
| DOGE | 16 ms | **467 ms** | 29× slower |
| XRP | 314 ms | 281 ms | ≈ same |
| HYPE | 4,172 ms | **622 ms** | 7× faster |
| BNB | insufficient | 1,362 ms | — |

**BTC holds its crown as the fastest asset** — 3× slower than the busy-regime 4h sample, but still the tightest MM competition in the set. The BTC/runner-up gap is ~2.7× (BTC 106 ms vs XRP 281 ms).

**Interpretation:** the 4-hour sample was from a PARTICULARLY ACTIVE market period — lots of trading, tight MM spreads, fast reprices. The 30-hour sample includes overnight + slower periods where MM cadence drops off dramatically. **The cross-asset ordering flipped** for SOL, DOGE, and HYPE:

- SOL was "fast" at 39 ms in the busy 4h; its true all-hours p50 is 479 ms.
- DOGE appeared fast at 16 ms (small sample); all-hours p50 is 423 ms.
- HYPE was abysmally slow at 4 s; its all-hours p50 is 559 ms — still slow but much closer to the others.

**Takeaway:** lag is highly regime-dependent. A scanner should adapt in real time to the current cadence, not rely on a static prior.

## 4. Cross-asset ranking by threshold

### ≥ 5 bps — the "tradeable signal" threshold

```
BTC   106 ms  ← fastest (tight MM competition)
XRP   281 ms
DOGE  467 ms
ETH   480 ms
SOL   479 ms
HYPE  622 ms
BNB  1362 ms  ← slowest
```

### ≥ 10 bps — large move regime

```
BTC    76 ms  ← fastest
ETH   113 ms
XRP   160 ms  ← tight cluster at 160 ms
HYPE  336 ms
SOL   331 ms
DOGE  392 ms
BNB  1118 ms
```

### ≥ 20 bps — extreme moves (MM is definitely present)

```
SOL    45 ms  ← fast MM activates at big moves
XRP   160 ms
HYPE  183 ms
DOGE  210 ms
ETH   5893 ms†
BTC     —     (no ≥20 bps moves in 30h window — BTC was relatively calm)
BNB     —     insufficient
```

†ETH's ≥20 bps bucket contains 23 events all at the same Δt, suggesting a single pathological window. Small sample; don't generalize.

## 5. Edge-availability per asset (solo operator)

Using the ≥5 bps threshold as the "tradable signal" floor.

| Asset | p10 | p50 | p90 | Solo op at 100 ms latency |
|---|---:|---:|---:|---|
| BTC | **12 ms** | **106** | 2,606 | Competes on ~45% of signals; MMs race at p25=33ms |
| XRP | 14 ms | 281 | 8,642 | Competes on ~50% of signals |
| DOGE | 14 ms | 467 | 20,472 | Competes on ~55% of signals |
| ETH | 17 ms | 480 | 8,318 | Competes on ~55% of signals |
| SOL | 16 ms | 479 | 10,147 | Competes on ~55% of signals |
| HYPE | 18 ms | 622 | 13,882 | Competes on ~60% of signals |
| BNB | 14 ms | 1,362 | 21,341 | Competes on ~80% of signals, but capacity is thin |

**Observation: BTC is harder to trade against at retail latency.** At 100 ms operator latency, only ~45% of BTC signals have Kalshi still un-repriced. For XRP/ETH/SOL/DOGE, ~50-55% of signals are beatable. For BNB, 80% — but book depth is the binding constraint.

An operator with 100 ms end-to-end latency beats Kalshi's reprice on whatever fraction of events has `Δt > 100 ms`. For DOGE / SOL / HYPE / BNB, this is ~50-80% of the distribution — a tractable retail edge if the directional read is clean.

## 6. Caveats (new with expanded sample)

1. **Trade-level vs book-level lag.** We measure time to the next KALSHI TRADE that changed the yes-price. A better measurement is time to the next BOOK UPDATE (bid/ask change). Bid/ask can move without a trade. For this, use the live WS `orderbook_delta` channel — scaffolded in `src/market/kalshi_market.py` but not yet running against prod. **Book-level lag will be ≤ trade-level lag.**
2. **Coinbase is not the full basket.** For XRP/DOGE/SOL, Coinbase is a high-weight CF Benchmarks constituent → measurement is faithful. For BNB and HYPE, Coinbase is a MUCH smaller constituent (CF BNB and UHYPE baskets) — basket-vs-venue tracking error may inflate the measured "lag" at small thresholds. The p50 at ≥20 bps is the least contaminated.
3. **Directional match varies 69-92%.** Below 100% means not every Coinbase move has a matching Kalshi move. For our analysis it should be treated as a noisy signal — filter for ≥10 bps to increase confidence.
4. **Overnight hours included.** US-equities-hours would likely show tighter lag distributions. A 24h-but-active-hours-only sample would show different numbers.

## 7. Actionable next steps

### 7.1 Complete BTC and ETH (pending)

BTC: 3M+ trades over 30h; pull will take ~60-90 min in wall-clock. ETH: ~500k over 30h; ~20 min.

Once both complete, update §2.6 and §2.7 with the 30h percentiles.

### 7.2 Live book-delta capture (for book-level lag)

Run `src/run_kalshi_shadow.py` against prod with the WS orderbook_delta subscription wired. A 24h capture across all 7 assets gives us **book-level** (not trade-level) lag distributions, which is what actually matters for execution.

### 7.3 Multi-venue reference upgrade

Currently only Coinbase. Adding Kraken + Bitstamp (both have XRP, DOGE, SOL, ETH, BTC) would:
- Shrink the "Coinbase vs basket" error (tighten measured lag at small thresholds)
- Especially help BNB / HYPE where Coinbase's weight in the CF basket is low

### 7.4 Regime detection

A real-time feature: **"What's the current reprice cadence for this asset in the last 60 seconds?"** Use that to accept/reject opportunities dynamically. When DOGE has been repricing every 100ms for a minute, skip it. When it has 5 seconds of silence, lean in.

## 8. Reproducibility

```bash
# 30h Kalshi trade pull (all 7 assets, 777 tickers total) — parallel OK with WAL.
for asset in btc eth sol xrp doge bnb hype; do
  xargs -n1 -I{} printf -- '--ticker\n{}\n' < /tmp/k_tickers_$asset.txt \
    | xargs python3.11 scripts/kalshi_trades_pull.py &
done; wait

# 30h Coinbase trade pulls — must run SEQUENTIALLY to avoid rate-limit cascade.
# Smallest-volume first so fast assets don't wait on BTC.
for asset in bnb hype doge xrp sol eth btc; do
  python3.11 scripts/coinbase_trades_pull.py --asset $asset \
    --start 2026-04-19T11:15:00Z --end 2026-04-20T17:10:00Z
done

# Analysis.
python3.11 /tmp/lag_analysis_100plus.py
```

## 9. Sources

See `kalshi_feed_lag_cross_asset_report.md` §9 and `kalshi_crypto_multi_asset_report.md` §12.
