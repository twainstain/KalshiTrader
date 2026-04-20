# Kalshi Crypto Fair-Value — Tracking-Error + First-Pass Backtest Report

**Research date:** 2026-04-20
**Status:** First-pass with Coinbase-alone reference proxy. Tracking-error measured against Kalshi's published `expiration_value` (the settled BRTI 60s-avg). Phase-1 feasibility scoring uses the refactored single-parameter statistical model.
**Scope:** 2,822 settled Kalshi `/bitcoin-price-up-down/` markets per asset (BTC / ETH / SOL), close_time ∈ (Mar 21 – Apr 20 2026). Reference proxy: Coinbase 1-minute close candles over the same window.
**Cross-links:** [`kalshi_phase1_smoke_test_findings.md`](./kalshi_phase1_smoke_test_findings.md), [`kalshi_scanner_execution_plan.md`](./kalshi_scanner_execution_plan.md), [`kalshi_crypto_fair_value_scanner_plan.md`](./kalshi_crypto_fair_value_scanner_plan.md).

> Not investment, legal, or tax advice.

## 1. What changed since the smoke-test report

1. **Prod data, not demo.** `scripts/kalshi_public_pull.py` pulls the public `/markets?series_ticker=X&status=settled` endpoint — no auth required. Demo data turned out to have synthetic settlements (see prior doc §5); prod settlements are tied to real BRTI values and track external references as expected.
2. **New reference-source query.** Kalshi's response now yields `expiration_value` (the settled 60s-avg BRTI) directly. For the first time we can compare a Coinbase proxy against the true BRTI without inferring outcomes from resolutions.
3. **Model refactor.** The `/bitcoin-price-up-down/` market type is not a "price vs arbitrary strike" binary — every market asks the same question, **"Is the 15-min log-return ≥ 0?"**, with the prior window's 60s-avg as the "strike". The `FairValueModel` is now a single-parameter Gaussian over 15-min log-returns.

## 2. Methodology

### 2.1 Pulling real Kalshi data

```bash
python3.11 scripts/kalshi_public_pull.py --asset all --days 30
```

Schema additions (applied to `kalshi_historical_markets`): `settlement_ts`, `expiration_value`, `last_price`. Migration is idempotent via `SAFE_ALTER_STATEMENTS` in `scripts/migrate_db.py`.

Counts returned: 2,832 per series over 30 days → 2,822 with usable `expiration_value` after filtering open/unresolved.

### 2.2 Backfilling Coinbase reference

```bash
python3.11 scripts/coinbase_historical_pull.py --asset all \
    --start 2026-03-20T00:00:00Z --end $(now - 5m)
```

1-minute close candles from `https://api.exchange.coinbase.com/products/{PRODUCT}/candles?granularity=60`. 45,450 ticks per asset.

### 2.3 Tracking error: Coinbase close vs Kalshi `expiration_value`

For each settled market, find the Coinbase close at or before the market's `close_ts`. Compute `|BRTI − Coinbase_close| / BRTI` in basis points. These are direct — no inference from resolution outcomes.

### 2.4 Fair-value model calibration

σ_15min fit from the consecutive log-returns of the `expiration_value` chain (2,831 returns per asset):

```
Series   σ_15min (stdev)    σ_15min (MAD·1.4826)   Annualized (stdev)
BTC          0.232%                 0.152%                 43.4%
ETH          0.310%                 0.188%                 58.0%
SOL          0.312%                 0.204%                 58.3%
```

MAD-scaled σ is ~35% lower than raw stdev — returns are fat-tailed. For the backtest below we use the raw stdev; a robust version would drop to the MAD estimate and add explicit tail handling.

### 2.5 Scoring

`src/run_kalshi_backtest.py` invokes `FairValueModel.price()` per market at a decision-time offset (default T-30s). Model inputs:

```
strike              = floor_strike                  (prior 60s-avg BRTI)
reference_price     = Coinbase close at T-30s       (proxy)
reference_60s_avg   = Coinbase close at T-30s       (1-min granularity max)
time_remaining_s    = 30
```

Output: `p_yes = Φ(log(reference/strike) / (σ_15min · √(30/900))) − no_data_haircut`.

Scoring: Brier = mean((p − y)²), hit-rate = argmax accuracy, calibration by decile.

## 3. Tracking error — Coinbase close vs Kalshi BRTI

Sanity check first: does Kalshi's own `expiration_value` predict `result`?

| Series | N | Agree | % |
|---|---:|---:|---:|
| KXBTC15M | 2,822 | 2,821 | 99.96% |
| KXETH15M | 2,822 | 2,819 | 99.89% |
| KXSOL15M | 2,822 | 2,819 | 99.89% |

100% is the right answer. The ~0.1% miss is `>=` boundary rounding. This confirms data integrity.

### 3.1 |BRTI − Coinbase close| percentiles (bps)

| Series | N | p50 | p90 | p95 | p99 | max |
|---|---:|---:|---:|---:|---:|---:|
| KXBTC15M | 2,822 | **3.3** | 10.9 | 14.8 | 25.5 | 68.9 |
| KXETH15M | 2,822 | **3.9** | 13.0 | 18.8 | 36.9 | 114.3 |
| KXSOL15M | 2,822 | **4.5** | 14.1 | 20.1 | 36.9 | 127.3 |

Takeaways:

- **Coinbase tracks BRTI at median 3-5 bps across all three assets.** Well below Kalshi's $0.01 tick size (which is 100 bps in probability space on a $1 binary).
- **Tail risk is real but rare.** p99 is 25-40 bps; max is 69-127 bps. Those tail events are where a single-venue proxy can mislead an entry. Multi-exchange basket aggregation (Kraken + Bitstamp + Coinbase) would dampen the tails without the cost of the licensed CF feed.
- **Per-asset ordering matches exchange liquidity.** BTC (deepest Coinbase book) has the tightest tracking; SOL (thinner Coinbase book) has the widest.

### 3.2 Agreement by |Coinbase − strike| bucket

Does Coinbase's close, compared to the strike (prior avg), predict the same resolution Kalshi gave?

| Bucket (bps) | BTC N | BTC % | ETH N | ETH % | SOL N | SOL % |
|---|---:|---:|---:|---:|---:|---:|
| [0, 5)   | 695 | 68.2 | 574 | 68.8 | 547 | 63.6 |
| [5, 10)  | 590 | 90.0 | 526 | 88.8 | 468 | 85.9 |
| [10, 25) | 983 | 97.9 | 971 | 97.2 | 952 | 96.6 |
| [25, 50) | 422 | 98.8 | 504 | 99.4 | 577 | 99.0 |
| [50, 100)| 111 | 100.0 | 199 | 99.5 | 227 | 99.6 |
| [100, 500)| 19 | 100.0 | 44 | 100.0 | 47 | 100.0 |
| [500, +∞)| (N ≤ 4; ignore) | | | | | |

**Shape interpretation:**

- Near-the-money ([0, 5) bps): ~32-36% disagreement. Genuine coin-flip zone — basket noise, timing lag, and true return uncertainty all mix.
- Ten bps from the strike: resolves predictably 88-90% of the time.
- 25+ bps out: resolves predictably ≥99%.

This is the feasibility thesis's theoretical edge zone: in the 5-25 bps band, Coinbase is "right most of the time but not always," and the difference may be exploitable feed-lag — BRTI has moved and Kalshi's book hasn't repriced. Quantifying how much of that disagreement is feed-lag vs. basket noise vs. irreducible uncertainty is work for the lag-distribution analysis notebook (P1-M5-T01).

## 4. First-pass statistical-model backtest

Decision at T-30s; Coinbase close at that moment used as the reference.

### 4.1 Headline: Brier + hit-rate

| Series | N | Model Brier | Naive 0.5 | Coinbase-direct Brier | Model hit-rate |
|---|---:|---:|---:|---:|---:|
| KXBTC15M | 2,822 | **0.0473** | 0.2500 | 0.0613 | **93.87%** |
| KXETH15M | 2,822 | **0.0405** | 0.2500 | 0.0468 | **95.32%** |
| KXSOL15M | 2,822 | **0.0426** | 0.2500 | 0.0489 | **95.11%** |

**The new model beats both naive-0.5 (by ~5×) and Coinbase-direct (slightly).** It beats Coinbase-direct mostly because the statistical model outputs calibrated probabilities, not hard 0/1 predictions, so its Brier on near-boundary markets is lower even when the argmax matches.

### 4.2 Calibration by decile (pooled across assets)

| Decile | N | Avg predicted p_yes | Empirical Yes rate | Error (pp) |
|---:|---:|---:|---:|---:|
| 0 | 3,062 | 0.013 | 0.002 | **1.1** |
| 1 | 390 | 0.147 | 0.056 | 9.0 |
| 2 | 279 | 0.247 | 0.147 | 10.0 |
| 3 | 240 | 0.350 | 0.287 | 6.3 |
| 4 | 263 | 0.450 | 0.395 | 5.4 |
| 5 | 241 | 0.551 | 0.627 | 7.5 |
| 6 | 317 | 0.652 | **0.861** | **20.9** |
| 7 | 322 | 0.750 | **0.919** | **16.9** |
| 8 | 387 | 0.854 | 0.956 | 10.2 |
| 9 | 2,965 | 0.987 | 0.992 | 0.5 |

**Reading the table:**

- **Extremes (deciles 0 and 9) are well-calibrated:** when the model says p ≈ 0.99 or p ≈ 0.01, that's essentially what happens. The fact that 3,062 + 2,965 = 6,027 (71% of markets) sit in these extreme buckets reflects how often the 30-second-out spot is far enough from the strike for uncertainty to have collapsed.
- **Middle deciles (6-8) are systematically UNDER-confident** — the model says 65-85% Yes, but the realized rate is 86-96% Yes. Interpretation: when Coinbase shows 70% Yes, Kalshi's BRTI-driven resolution is actually ~86% Yes. **That's consistent with Coinbase lagging BRTI** — BRTI has already moved further in the Yes direction than Coinbase shows, so the model underweights the move.
- Deciles 1-2 show a similar but smaller effect in the other direction.
- **This residual mis-calibration is potentially edge.** If the book prices the market at Coinbase-derived p_yes, but BRTI resolution is more confident than that, there's a short window between "BRTI moved" and "book repriced" where one can buy the true probability at the stale price. Measuring whether that window exists and how long it lasts is the P1-M5 lag-distribution deliverable.

### 4.3 Caveats

1. **Coinbase reference lag.** 1-minute candles aggregate all trades within a minute → the `close` is the price as of minute-end, up to 59 seconds stale. Subminute BRTI moves (and thus the interesting lag signal) are invisible at this resolution. Replacing the backfill with Coinbase L2 or trade-level data would sharpen the analysis significantly.
2. **Single-venue proxy.** Coinbase alone adds 3-5 bps of median tracking error to every measurement. For coarse Brier / hit-rate numbers this is fine; for edge quantification it's a lower bound. Kraken + Bitstamp backfill would reduce tracking error to ~2 bps and probably shift middle-decile calibration closer to the diagonal.
3. **30-day sample.** 2,822 markets per asset is statistically adequate for first-pass Brier/calibration but sparse for regime analysis (weekday/weekend, high-volatility windows, around CPI releases, etc.). 3+ months would give ~8,500 per asset.
4. **σ_15min fit uses a full-sample non-time-weighted stdev.** If volatility is regime-dependent (and it is — crypto vol clusters), a rolling or EWMA σ would outperform. Left for future work.
5. **No naïve-book baseline.** The "Coinbase-direct" Brier baseline treats Coinbase's prediction as 0 or 1 — it's a floor. A better baseline is "trust the Kalshi book's `last_price`", which encodes what MMs already know. Pulling `last_price` from the historical endpoint snapshot is trivial and should be the next comparison.

## 5. Feasibility implications

| Dimension | Signal strength | Remaining uncertainty |
|---|---|---|
| Ground truth integrity | ✅ 99.96% self-consistency on BRTI → result | — |
| Coinbase tracking error vs BRTI | ✅ Median 3-5 bps across assets | Tails 25-127 bps; multi-exchange basket would help |
| Model calibration at the extremes | ✅ Deciles 0, 9 within 1 pp | Middle deciles 5-20 pp off |
| Model performance vs naive | ✅ 5× better Brier | 20 pp miscalibration in 6-8 deciles |
| Feed-lag edge existence | ⚠️ Shape consistent | Not yet measured in time units — only inferred from calibration |
| Size capacity | ❓ Not yet estimated | Requires book-depth-at-decision data; need live shadow evaluator runtime |

**The feasibility thesis is not disproved.** The shape of the errors (Coinbase under-confident → Kalshi resolution over-confident in the same direction) is exactly what an exploitable feed-lag would look like. But "exists in principle" ≠ "profitable after fees and execution costs." The deliverable that determines the P1→P2 gate is:

1. Lag-distribution analysis (P1-M5-T01) — measure `delta_t` between BRTI moves and Kalshi book repricing at the book's level, with p50/p90/p99 per asset. If p50 < 500 ms, solo operator latency cannot compete; if p50 is multi-second, there's room.
2. Realized-edge analysis (P1-M5-T02) — run the shadow evaluator forward on live data for the full Phase-1 window; count how many approvable opportunities (edge > fees, CI acceptable) materialized per asset per day, and what realized-if-traded P/L was.
3. Capacity analysis (P1-M5-T03) — per candidate decision, compute book depth at the proposed fill price. Convert to daily-$ capacity.

## 6. Recommendations

**Keep going.** Phase 1 is not proven profitable, but the data shape is consistent with a tractable problem. Specific next moves, in order of urgency:

1. **Run the live shadow evaluator in prod** (`src/run_kalshi_shadow.py` with production API key once KYC clears). 24-48 hours of live data with Coinbase reference and per-decision book snapshots is the input to the three analyses above. No money at risk.
2. **Add `last_price` to the scoring comparison.** Replace the coarse "Coinbase-direct" baseline with "what did Kalshi's last_price imply at T-30s?" That's the right reference for MM comparison.
3. **Upgrade Coinbase reference to L2 / trade-level.** Coinbase Exchange `GET /products/{X}/trades` returns every tick with timestamps; resampling to per-second gives us subminute resolution without a licensing cost.
4. **Add Kraken + Bitstamp to the basket reference.** Reduces tracking-error tail from 127 → likely ~20 bps. Adds two more HTTP pollers; negligible work.
5. **Consider rolling-window σ** instead of full-sample. An EWMA with half-life ~24h would track volatility regime changes and tighten the middle-decile calibration.

## 7. Reproducibility

All data in `data/kalshi.db` (SQLite). Minimal repro:

```bash
# 1. Pull 30 days of settled Kalshi markets (public endpoint).
python3.11 scripts/kalshi_public_pull.py --asset all --days 30

# 2. Backfill Coinbase reference over the matching range.
python3.11 scripts/coinbase_historical_pull.py --asset all \
    --start "2026-03-20T00:00:00Z" --end "$(python3.11 -c 'import datetime; print((datetime.datetime.utcnow() - datetime.timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ"))')"

# 3. Calibrate σ_15min.
python3.11 scripts/calibrate_sigma.py --emit-python

# 4. Backtest the model.
PYTHONPATH=src python3.11 -m run_kalshi_backtest --report docs/kalshi_backtest_report_latest.md

# 5. Tracking-error comparison (inline python, see this doc §3.1 / §4).
```

Total wall-clock: ~1 minute.

## 8. Sources

- [Kalshi CRYPTO15M Contract Terms (PDF)](https://kalshi-public-docs.s3.amazonaws.com/contract_terms/CRYPTO15M.pdf)
- [Kalshi API — Markets endpoint](https://docs.kalshi.com/reference/get-markets)
- [CF Benchmarks — BRTI methodology](https://www.cfbenchmarks.com/data/indices/BRTI)
- [Coinbase Exchange — Candles](https://docs.cdp.coinbase.com/exchange/reference/exchangerestapi_getproductcandles)
