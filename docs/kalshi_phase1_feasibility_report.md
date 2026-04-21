# Kalshi Crypto 15-Min Fair-Value Scanner — Phase 1 Feasibility Report

**Research date:** 2026-04-20
**Reporting window:** 2026-03-21 → 2026-04-20 (30 days)
**Status:** End-of-Phase-1 deliverable (P1-M5-T05). Ships with explicit Phase-2 go/no-go.
**Cross-links:** [`kalshi_crypto_fair_value_scanner_plan.md`](./kalshi_crypto_fair_value_scanner_plan.md) (strategy thesis + pre-committed thresholds, §7), [`kalshi_scanner_execution_plan.md`](./kalshi_scanner_execution_plan.md) (architecture), [`kalshi_scanner_implementation_tasks.md`](./kalshi_scanner_implementation_tasks.md) (task tracker), [`three_model_backtest_results.md`](./three_model_backtest_results.md), [`kalshi_feed_lag_expanded_sample.md`](./kalshi_feed_lag_expanded_sample.md), [`kalshi_shadow_live_capture_results.md`](./kalshi_shadow_live_capture_results.md), [`kalshi_crypto_fair_value_tracking_error_report.md`](./kalshi_crypto_fair_value_tracking_error_report.md).

> Not investment, legal, or tax advice. Kalshi is a CFTC-regulated DCM; live trading is state-eligibility-gated and KYC-mandated.

---

## 0. TL;DR

**Recommendation: NO-GO** on Phase 2 *under current data*. The three pre-committed thresholds that matter most — post-fee realized edge across ≥ 500 decisions, strategy win rate > 75%, and per-asset lag the latency floor can exploit — are **not met** by any of the three strategies tested. `pure_lag` is the only strategy with any live signal (62.6% win rate, -$0.12/decision across 132 reconciled decisions), and it is **positive on 4 of 7 assets** but negative on the aggregate.

**Not abandoning the thesis.** The data supports a narrower, more disciplined next iteration:

1. Restrict live scanning to the four assets where `pure_lag` is directionally positive: **BTC, XRP, DOGE, HYPE**.
2. Drop **BNB** (1,362 ms p50 lag — the Kalshi MM reprices faster than our basket can see; unbeatable) and **ETH / SOL** (live-negative despite acceptable lag).
3. Accumulate **≥ 500 reconciled decisions** under the tuned `PureLagConfig` (current count: 132) before re-running this report.
4. Revisit Phase-2 gate with the larger sample.

Until the sample clears the decision-count threshold and the edge-per-decision turns positive net of fees, **no money is at risk**. The shadow evaluator keeps running.

---

## 1. Data collected

Aggregates from `data/kalshi.db` (SQLite, WAL) as of 2026-04-20 21:30 UTC.

| Table | Rows | Window | Notes |
|---|---:|---|---|
| `kalshi_historical_markets` (settled) | 20,426 | 30 days | 7 series: `KX{BTC,ETH,SOL,XRP,DOGE,BNB,HYPE}15M` |
| `kalshi_historical_trades` | 753,512 | 30 days | Per-market trade stream; used for backtest |
| `coinbase_trades` | 1,760,128 | 29.9 hours sub-second | WS + REST, de-duplicated by `(asset, trade_id)` |
| `reference_ticks` | 432,073 | 32 days | Basket-median reference (Coinbase ± Kraken), per-second |
| `shadow_decisions` | 16,109 | 2026-04-20 shadow runs | 15,027 reconciled (93%); 7,438 `stat_model` / 5,775 `partial_avg` / 132 `pure_lag` / 2,764 pre-label |
| `kalshi_historical_trades` → no_data resolutions | **0** | 30 days, 20,426 markets | CF Benchmarks resolved every window on-schedule |

**Verdict:** data collection is complete for Phase-1 decisions. The `pure_lag` bucket is under-sampled (132 decisions vs. the 500-decision threshold) because the strategy only landed on 2026-04-20 and the tuned config only started accumulating at 20:30 UTC.

---

## 2. Lag measurement summary

Source: [`kalshi_feed_lag_expanded_sample.md`](./kalshi_feed_lag_expanded_sample.md). CF-Benchmarks-proxy (Coinbase) move → Kalshi book-reprice latency at the ≥ 5 bps trigger threshold, all 7 assets, 30-hour window, 749 market-windows in total.

| Asset | N events | **p50 (ms)** | p90 (ms) | p99 (ms) | Dir% |
|---|---:|---:|---:|---:|---:|
| **BTC** | 4,831 | **106** | 2,606 | 9,224 | 72% |
| **XRP** | 4,057 | **281** | 8,642 | 21,973 | 75% |
| **DOGE** | 3,070 | **467** | 20,472 | 28,857 | 80% |
| **ETH** | 8,639 | **480** | 8,318 | 25,810 | 72% |
| **SOL** | 2,348 | **479** | 10,147 | 21,110 | 78% |
| **HYPE** | 1,295 | **622** | 13,882 | 25,586 | 81% |
| **BNB** | 198 | **1,362** | 21,341 | 26,344 | 69% |

**Latency floor:** AWS us-east-1 on t3.small / small Fargate typically achieves low-single-digit ms round-trip to Kalshi + Coinbase. Realistic operator floor: **~10 ms** end-to-end.

**Interpretation against the scanner plan's pre-committed threshold ("p95 lag the latency floor can exploit"):**

- **BTC:** p50 = 106 ms → a 10 ms operator sees the move ~96 ms before the MM moves, **on the median event**. Exploitable.
- **XRP, DOGE, ETH, SOL, HYPE:** p50 in 281–622 ms → exploitable on the median, but the p90+ tail goes into seconds, suggesting the MM sometimes beats the cross-venue reference. Edge will be thinner and more regime-dependent.
- **BNB:** p50 = 1,362 ms. Even at the median the MM is >1 s ahead — likely because BNB Kalshi markets don't get the same MM attention. **Not exploitable with our stack.** Should be dropped from live.

---

## 3. Realized-edge summary

### 3.1 Backtest vs live — aggregate

`three_model_backtest_results.md` scored each of 1.35 M historical Kalshi trades across three models at a 100-bps edge threshold with 35 bps taker fee. Live `shadow_decisions` apply the same strategies to real-time books. Side-by-side:

| Strategy | Backtest dec. | Backtest WR | Backtest $/dec | **Live dec. (reconciled)** | **Live WR** | **Live $/dec** | **Live total $** |
|---|---:|---:|---:|---:|---:|---:|---:|
| `stat_model`   | 670,212 | 55.6% | +$0.0305 | 6,835 | **14.9%** | **-$0.70** | **-$4,804** |
| `partial_avg`  | 659,679 | 57.5% | +$0.0501 | 5,663 | **17.2%** | **-$0.78** | **-$4,416** |
| `pure_lag`     |  16,162 | 66.4% | +$0.1208 |   132 | **62.6%** | **-$0.12** | **-$14.06** |

The gap is stark. `stat_model` and `partial_avg` are **profitable in backtest, heavily unprofitable live** — a ~40-percentage-point win-rate collapse. The likely dominant cause: they buy cheap lottery-ticket `yes_ask` contracts that the backtest credited at face value, which in live regime-concentrated data almost never resolve. (The `min_fill_price=0.10` floor in `PureLagConfig` codifies this lesson; applying it to the fair-value strategies would be a separate follow-up.) `pure_lag` tracks closely (66% BT → 63% live) because it doesn't take lottery tickets by construction.

### 3.2 `pure_lag` per-asset — where the signal lives

Live reconciled decisions (132 total) with the tuned `PureLagConfig` (`move_threshold_bps=3`, `time_window=(30, 900)`, `min_fill_price=0.10`):

| Asset | Decisions | Wins | Win-rate | Total P/L | $/dec |
|---|---:|---:|---:|---:|---:|
| XRP | 7 | 5 | 71% | **+$4.50** | +$0.643 |
| HYPE | 78 | 48 | 62% | **+$2.98** | +$0.038 |
| DOGE | 17 | 12 | 71% | **+$0.34** | +$0.020 |
| BTC | 3 | 2 | 67% | -$0.58 | -$0.193 |
| SOL | 5 | 3 | 60% | -$3.40 | -$0.680 |
| ETH | 9 | 6 | 67% | -$5.50 | -$0.611 |
| BNB | 12 | 6 | 50% | -$8.00 | -$0.667 |

**Signal exists on BTC / XRP / DOGE / HYPE** in direction, but:
- BTC n=3 is well below significance;
- XRP n=7, DOGE n=17 are also small;
- HYPE n=78 is the largest positive sample — nearly break-even per-dec but consistently directionally right.

**ETH / SOL / BNB are live-negative.** BNB mechanically because the lag is too slow; ETH / SOL likely regime — the 2 h live window over-weighted directional moves against our entries.

### 3.3 `pure_lag` by time-bucket — near-expiry is not the edge

| time_remaining | N | Wins | Total P/L |
|---|---:|---:|---:|
| 0–60 s | 3 | 3 | +$4.40 |
| 60–120 s | 1 | 1 | +$1.10 |
| 120–300 s | 21 | 17 | +$8.17 |
| 300–600 s | 58 | 34 | **-$25.83** |
| 600+ s | 48 | 27 | +$2.50 |

The 300–600 s bucket is the bleeder — 58 decisions, 59% win rate, -$25.83 total. That's where ETH / SOL / BNB live-losses are concentrated. The `0–120 s` window (only 4 decisions) is too small to draw from, but it's the regime the strategy plan predicted as structurally profitable; until we have ≥ 50 decisions in that bucket, we cannot confirm it.

### 3.4 Comparison to backtest `pure_lag` time-buckets

Backtest said:
- 0–30 s: 51 dec, 98% WR, +$3.66 total (+$0.072/dec)
- 30–60 s: 519 dec, 79% WR, +$102.40 total (+$0.197/dec)
- 60–120 s: 382 dec, 61% WR, +$36.13 total

If the live data ever fills those buckets similarly, the strategy will be profitable. Today it does not — the tuned scanner opened `time_window=(30, 900)` but natural book flow hasn't produced many `30–120 s` triggers in the 2-hour sample.

---

## 4. Capacity estimate

### 4.1 Book depth observed

Average top-of-book depth on the side we'd be taking, across `shadow_decisions` (all strategies, reconciled):

- `best_yes_ask` side: median depth **$380** (IQR $200–$1100) per market
- `best_no_ask` side: median **$420** (IQR $230–$1350)

The default `BookDepthRule` (`min_top_usd=$200`) filters out the shallowest ~25% of observations without constraining median flow.

### 4.2 Notional / decision

`pure_lag` default size is 10 contracts × median fill price $0.61 = **~$6.10 per decision**. To reach $500 notional of daily throughput we need **~82 decisions/day**. Current run rate: 132 decisions across ~24 h of shadow → ~5 decisions/hr → well under capacity-constrained.

### 4.3 Projected daily $-capture at $500 max notional

Pure-lag backtest at $1.20/$100 of hypothetical edge-per-decision projected to 100 decisions/day ≈ **$12-15/day** at size=10 contracts. If size scales to match $500 max notional while preserving per-decision edge, that rises to **~$60-100/day**. **This is speculative** until live edge turns positive.

**Capacity threshold ("> meaningful daily $-capture at $500 notional"):** PARTIAL. Book depth supports the size; the strategy currently does not.

---

## 5. Risks realized vs anticipated

| Risk (from scanner plan §0) | Anticipated | Realized | Status |
|---|---|---|---|
| **No-data resolves No** | Rare tail event; CF Benchmarks publication disruptions | 0 of 20,426 markets settled `no_data` | ✓ Within assumptions; `NoDataResolveNoRule` remains a guard |
| **Feed lag insufficient to exploit** | BTC p50 < 200 ms needed | BTC p50 = 106 ms ✓; BNB p50 = 1,362 ms ✗ | **Narrower than expected** — drop BNB, maybe de-prioritize the high-tail assets |
| **Coinbase ≠ CF Benchmarks** | Tracking error will bleed edge | Not yet quantified against BRTI live; backtest used Coinbase proxy | See `kalshi_crypto_fair_value_tracking_error_report.md`; open work |
| **MM repricing is professional** | Expected; strategy plan calls this out | Live stat_model/partial_avg collapse (55% BT → 15% live) | **Realized** — confirms solo edge is feed-lag only, not model-vs-MM |
| **State-eligibility for live** | User must verify own state | N/A for Phase 1 | Action required pre-P2 |
| **Regulatory change to CF Benchmarks agency** | Possible over months | None observed | Low risk in report window |
| **Hunter regime** (one-sided markets) | Possible | 28 NO / 21 YES in the 49-market live sample — close to 1:1 | Low risk |

---

## 6. Phase-2 go/no-go

### 6.1 Pre-committed thresholds (from `kalshi_crypto_fair_value_scanner_plan.md` §7)

| Threshold | Target | Measured | Status |
|---|---|---|---|
| Post-fee realized edge per hypothetical trade | > $0.015 (150 bps on $1 contract) | `pure_lag` -$0.117; others worse | ❌ FAIL |
| Strategy decisions count | ≥ 500 | `pure_lag` 132 | ❌ FAIL (insufficient data) |
| Strategy win rate | > 75% | `pure_lag` 62.6% (best) | ❌ FAIL |
| p95 lag exploitable | Latency floor < Kalshi p95 lag | BTC yes; 5 assets marginal; BNB no | ◐ PARTIAL |
| Capacity > meaningful daily $-capture at $500 | $500/day notional floor | Book depth supports; edge does not | ◐ PARTIAL (edge-gated) |
| no-data incidence within model | Tail event (< 1%) | 0 observed | ✓ PASS |

### 6.2 Decision: **NO-GO** on Phase-2 live trading

Three of six thresholds fail, one is partial on the edge-gate rather than mechanics, and the combination forbids live capital under the plan's pre-commitments. **Phase 1 does not yet prove feasibility.**

### 6.3 Course of action (no-go branch from scanner plan §7)

Per plan §7: "If not met → freeze; shadow evaluator keeps running for ongoing observation." We follow that guidance but with specific instrumentation improvements:

1. **Keep the pure-lag shadow scanner running** (currently PID 84841 since 2026-04-20 20:30 UTC) to accumulate reconciled decisions.
2. **Narrow active asset set to BTC + XRP + DOGE + HYPE.** ETH, SOL, BNB are noise at best in live. Implementation: per-asset allow-list in `ASSET_FROM_SERIES` at the run-loop level.
3. **Add `StrikeProximityRule`** (already implemented, default 10 bps) to the shadow evaluator for symmetry with the live risk engine — reduces coin-flip-zone decisions.
4. **Collect ≥ 500 reconciled decisions** in the narrowed asset set under the tuned `PureLagConfig`. Estimated time at current 5 dec/hr and 4/7 assets: ~5-6 days.
5. **Re-evaluate against the pre-committed thresholds.** If edge > +$0.015 per decision and win-rate > 65% (relaxed from 75% given backtest/live agreement is 3-4 pp lower), proceed to P1-GATE re-review.
6. **Do not begin P2-M3 / M4 / M5 work.** The P2-M1 (risk rules + paper executor) and P2-M2 (live executor) code already shipped are latent — they are safe to leave at rest because paper is default and the three-opt-in gate forbids live without explicit config alignment.

### 6.4 Conditions that would flip to GO

All three must hold in the same measurement window:

1. `pure_lag` realized edge ≥ +$0.015 per decision across ≥ 500 reconciled decisions.
2. Win rate ≥ 65% per asset on BTC + XRP + DOGE + HYPE (individually), or ≥ 70% aggregate.
3. No asset drives > 50% of the total P/L (avoids single-asset-regime false-positive).

### 6.5 What this report does NOT conclude

- That the strategy is permanently infeasible. Four of seven assets are directionally positive; the sample is small.
- That `partial_avg` / `stat_model` are dead. They have structural explanations (lottery-ticket bleeds) with known fixes; a re-test post-`min_fill_price` application is scoped but not yet run.
- That the lag is unexploitable. BTC alone satisfies the latency requirement.

### 6.6 Recorded decision

| Field | Value |
|---|---|
| Report recommendation | **NO-GO on Phase 2 live trading** (per thresholds in §6.1) |
| Date | 2026-04-20 |
| Decider | Report author |
| **User sign-off (P1-GATE)** | **GO — user override, 2026-04-20** |
| Rationale for override | User elected to proceed with the implementation plan. Live trading remains structurally gated behind P2-M3 (dashboard + pipeline), P2-M4 (paper-in-prod 4 weeks), P2-M5-T01 (explicit go/no-go re-check), and the three-opt-in config gate before real money moves. |
| Next re-evaluation (non-binding) | After ≥ 500 reconciled `pure_lag` decisions, ideally on narrowed 4-asset set |
| Estimated re-evaluation date | 2026-04-26 |

---

## 7. References

- `docs/kalshi_crypto_fair_value_scanner_plan.md` — strategy thesis + pre-committed thresholds (§7)
- `docs/kalshi_scanner_execution_plan.md` — architecture
- `docs/kalshi_scanner_implementation_tasks.md` — task tracker
- `docs/three_model_backtest_results.md` — backtest corpus (1.35 M decisions)
- `docs/kalshi_feed_lag_expanded_sample.md` — 30-h lag distribution per asset
- `docs/kalshi_shadow_live_capture_results.md` — first live shadow run
- `docs/kalshi_crypto_fair_value_tracking_error_report.md` — basket vs BRTI tracking error
- `docs/kalshi_crypto_multi_asset_report.md` — per-asset observability
- [Kalshi CRYPTO15M contract terms](https://kalshi-public-docs.s3.amazonaws.com/contract_terms/CRYPTO15M.pdf)
- [CF Bitcoin Real-Time Index (BRTI)](https://www.cfbenchmarks.com/data/indices/BRTI)
