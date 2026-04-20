# Kalshi Crypto 15-Min — Cross-Asset Feasibility Report

**Research date:** 2026-04-20
**Scope:** 30 days (Mar 21 – Apr 20 2026), **7 assets × 2,822 settled markets each = 19,754 scored markets**, joined against 295,720 Coinbase 1-minute candles.
**Resolution reference:** Kalshi's own `expiration_value` field (ground-truth 60s-avg of the CF Benchmarks RTI for each asset). Coinbase 1-minute closes used as the per-asset proxy.
**Data pulls:** `scripts/kalshi_public_pull.py --asset all --days 30` + `scripts/coinbase_historical_pull.py --asset all --start 2026-03-20 ...`.
**Cross-links:** [`kalshi_phase1_smoke_test_findings.md`](./kalshi_phase1_smoke_test_findings.md), [`kalshi_crypto_fair_value_tracking_error_report.md`](./kalshi_crypto_fair_value_tracking_error_report.md).

> Not investment, legal, or tax advice.

## 0. TL;DR

- All 7 Kalshi 15-minute crypto markets (BTC, ETH, SOL, XRP, DOGE, BNB, HYPE) resolve against CF Benchmarks Real-Time Indices (RTIs) via the same methodology. A single-parameter statistical model applies uniformly.
- Coinbase's 1-minute close tracks the RTI within **3-7 bps median** across all 7 assets. Agreement with Kalshi's binary resolution is **89-91%** universally.
- Per-asset **σ_15min**: BTC 0.23% (lowest / 43% annualized) → HYPE 0.38% (highest / 71% annualized). These fit the statistical model cleanly.
- The "coin-flip zone" where edge lives is `|Coinbase − strike| < 5 bps`. In that zone, agreement drops to **59-69%** across all assets — ~32-41% of those markets resolve contrary to Coinbase's directional read, and some of that is likely exploitable feed-lag.
- **Volume varies 200×** from BTC's median of 200k contracts per market down to BNB's 974. Capacity, not edge, is the gating factor for most assets other than BTC.
- **Three asset buckets emerge:** (1) BTC — institutional-grade depth, highest MM competition, smallest absolute edge. (2) ETH/SOL/XRP — midsize, tractable. (3) DOGE/BNB/HYPE — retail-scale size, more ATM markets per day, but thinner books and worse Coinbase proxy (especially HYPE).

## 1. Methodology recap

Kalshi 15-min crypto "up-down" markets on `/bitcoin-price-up-down/` (and asset equivalents) ask a single binary: **"Is the 60s-avg RTI at close ≥ the 60s-avg RTI at the prior 15-min boundary?"** The contract returns:

- `floor_strike` — the prior 15-min boundary's 60s-avg RTI (the "target")
- `expiration_value` — the current window's 60s-avg RTI (ground truth)
- `result` — `yes` if `expiration_value ≥ floor_strike`, else `no`
- `settlement_ts` — typically ~14 seconds after `close_time`

Every market is the same question, so a single-parameter Gaussian (log-returns over 15 minutes) prices them all. Per-asset σ_15min is calibrated from 2,831 consecutive expiration_value log-returns.

For the tracking-error study, we compare Kalshi's published `expiration_value` to Coinbase's 1-minute close at the market's `close_time`. This is a **direct** measurement — no inference from resolution outcomes.

## 2. CF Benchmarks RTIs used by Kalshi

Confirmed from `rules_primary` on each series' sample market (all verbatim from the API):

| Series ticker | Asset | CF Benchmarks RTI name |
|---|---|---|
| KXBTC15M | BTC | **BRTI** |
| KXETH15M | ETH | ETHUSDRTI |
| KXSOL15M | SOL | SOLUSDRTI |
| KXXRP15M | XRP | XRPUSDRTI |
| KXDOGE15M | DOGE | DOGEUSDRTI |
| KXBNB15M | BNB | BNBUSDRTI |
| KXHYPE15M | HYPE | **UHYPEUSDRTI** (note "U" prefix — likely denoting a specific basket methodology) |

All seven use the same rule text: simple average of 60s of RTI prices ending at the 15-minute boundary. Same no-data-resolves-No tail risk applies across all.

## 3. Per-asset σ_15min calibration

Fit from the expiration_value chain per series (2,831 consecutive 15-min log-returns each).

| Asset | σ_15min (stdev) | σ_15min (MAD·1.4826) | Annualized (stdev) | Notes |
|---|---:|---:|---:|---|
| **BTC** | 0.232% | 0.152% | 43.5% | Lowest vol. Benchmark. |
| **BNB** | 0.201% | 0.136% | 37.6% | Lowest in the set — likely muted by some less-active BNB hours. |
| **XRP** | 0.261% | 0.189% | 48.9% | |
| **DOGE** | 0.292% | 0.207% | 54.7% | |
| **ETH** | 0.310% | 0.188% | 58.1% | |
| **SOL** | 0.312% | 0.204% | 58.4% | |
| **HYPE** | 0.379% | 0.319% | 70.9% | Highest vol — largest moves per window. |

**Observations:**

1. MAD (median absolute deviation × 1.4826) runs **33-35% lower than raw stdev** for most assets; returns are fat-tailed. For production, consider a robust σ estimate or explicit tail handling.
2. HYPE's σ is almost 2× BTC's. Expected — HYPE is newer, more volatile, thinner liquidity.
3. BNB vol is suspiciously low. Likely measuring a CF Benchmarks basket that is itself more stable than direct BNB price action (CF uses a basket of constituents, some with thin volume at certain hours).

**Implication for the model:** use per-asset σ, don't average. A BTC trader and a HYPE trader have different probability distributions for the same "1 bp up in 30 seconds" event.

Copy-paste snippet emitted by `scripts/calibrate_sigma.py --emit-python`:
```python
DEFAULT_SIGMA_15MIN: dict[str, Decimal] = {
    "btc":  Decimal("0.00232"),
    "eth":  Decimal("0.00310"),
    "sol":  Decimal("0.00312"),
    "xrp":  Decimal("0.00261"),
    "doge": Decimal("0.00292"),
    "bnb":  Decimal("0.00201"),
    "hype": Decimal("0.00379"),
}
```

## 4. Tracking error — Coinbase close vs CF Benchmarks RTI

For each settled market, we compute `|BRTI − Coinbase_close| / BRTI` in basis points, and aggregate per asset. This is the **direct** measurement of how well a Coinbase-only proxy tracks the actual resolution reference.

| Asset | N | p50 (bps) | p90 | p95 | p99 | max | Agreement % |
|---|---:|---:|---:|---:|---:|---:|---:|
| **BNB** | 2,822 | **3.12** | 9.75 | 12.54 | 23.90 | 53.54 | 89.58% |
| BTC | 2,822 | 3.29 | 10.90 | 14.77 | 25.47 | 68.86 | 89.16% |
| XRP | 2,822 | 3.80 | 12.10 | 16.33 | 29.58 | 61.10 | 90.72% |
| ETH | 2,822 | 3.90 | 12.98 | 18.76 | 36.89 | 114.25 | 90.36% |
| SOL | 2,822 | 4.48 | 14.13 | 20.16 | 36.88 | 127.25 | 89.09% |
| DOGE | 2,822 | 4.77 | 14.30 | 19.63 | 35.07 | 113.42 | 90.29% |
| **HYPE** | 2,822 | **6.53** | 19.29 | 25.02 | 40.95 | 97.04 | 90.08% |

**Observations:**

1. Tracking error is remarkably consistent across assets in **median terms** (3-7 bps). Agreement with Kalshi's binary result is essentially flat at **89-91%** across all seven.
2. **HYPE is the outlier on tracking error.** The RTI ticker is `UHYPEUSDRTI` (unusual prefix), suggesting a specific basket methodology, and Coinbase may not be a high-weight constituent. Needs investigation before using Coinbase-alone as a HYPE reference in live trading.
3. **Tails vary.** SOL and DOGE hit 100-130 bps max; BTC/BNB cap around 50-70 bps. Tails matter for tail-loss risk; they matter less for median-edge attribution.

## 5. Bucket agreement — Coinbase direction vs Kalshi result by distance-from-strike

The "coin-flip zone" analysis. For each settled market, bucket by `|Coinbase − strike| / strike` in bps, and compute agreement between "Coinbase direction" and Kalshi's actual resolution.

If tracking error were zero, deep-OTM markets would agree 100% and at-the-money markets would agree ~50%. Real markets show a **smooth rise from ~60-70% at the strike to 100% at 25-100+ bps away**.

| Bucket (bps) | BTC | ETH | SOL | XRP | DOGE | BNB | HYPE |
|---|---:|---:|---:|---:|---:|---:|---:|
| [0, 5)   | 68% | 69% | 64% | 69% | 69% | 69% | **59%** |
| [5, 10)  | 90% | 89% | 86% | 89% | 83% | 92% | 77% |
| [10, 25) | 98% | 97% | 97% | 98% | 97% | 98% | 94% |
| [25, 50) | 99% | 99% | 99% | 99% | 99% | 99% | 99% |
| [50, 100)| 100% | 100% | 100% | 100% | 100% | 100% | 100% |
| [100, 500)| 100% | 100% | 100% | 100% | 100% | 100% | 100% |
| [500, +∞)| (N ≤ 4 — ignore) | | | | | | |

**Observations:**

1. **ATM zone (0-5 bps) is the edge zone for all assets.** 30-40% of these resolve contrary to Coinbase's read. A fraction of that 30-40% is basket-vs-Coinbase tracking error (irreducible with Coinbase-alone) and a fraction is feed-lag (potentially exploitable).
2. **HYPE shows a softer curve** — 59% at [0,5), 77% at [5,10), and only 94% at [10,25). That extra 3-6 pp of "error" vs the other assets is the cost of Coinbase being a poorer basket proxy for HYPE. Either replace Coinbase reference with a true HYPE basket, or price HYPE more conservatively (wider ci_width threshold).
3. **Above 25 bps, all assets agree ≥99%.** Deep-OTM / deep-ITM markets are resolved regardless of basket noise. These aren't edge markets; they're scoring calibration markets.

## 6. Volume + liquidity per asset

Liquidity gate: even with edge, a market that trades 100 contracts is not tradeable at scale.

| Asset | Median volume (contracts) | p90 volume | Median last_price | Volume rank |
|---|---:|---:|---:|---:|
| **BTC** | 201,670 | 307,386 | $0.98 | **1 (deepest)** |
| ETH | 16,738 | 30,248 | $0.56 | 2 |
| SOL | 5,803 | 11,259 | $0.95 | 3 |
| XRP | 4,485 | 10,107 | $0.02 | 4 |
| HYPE | 1,843 | 5,213 | $0.64 | 5 |
| DOGE | 1,748 | 3,970 | $0.12 | 6 |
| **BNB** | 974 | 2,616 | $0.86 | **7 (thinnest)** |

BTC has **~200× the median volume of BNB**. Per-market liquidity is the primary constraint for anything except BTC. Caveat: `volume` here is settled-market lifetime volume, which understates the within-window volume available at the decision moment — the intra-window book depth is what matters for a scanner and wasn't captured in this analysis.

**Median last_price is informative for bias:** BTC/SOL median yes-price of $0.95-0.98 means the market is typically very one-sided by 30s before close — directional bias resolved early. XRP's $0.02 median yes-price is the opposite interpretation (strong bias in the *other* direction at close). These biases give an operator a natural read: "if I can't beat the market's final-minute certainty, I can't trade."

## 7. Identified opportunities

Per asset, ranked by overall Phase-1 attractiveness:

### 7.1 BTC — Best fundamental setup, highest competition

**Positives:**
- Deepest market; ~200k contracts/window of liquidity.
- Tightest tracking error at median (3.3 bps vs BRTI), good Coinbase-as-proxy fit.
- Lowest σ (0.23%) — tightest price action means more markets hug the strike → more at-the-money opportunities.

**Negatives:**
- Most attention from institutional MMs. Expected median book-reprice latency from the (single-window, cautionary) BTC analysis: ~100-1,000 ms; a retail latency budget of 150ms+ might beat the slow half of the day and lose the fast half.
- Largest fee drag at small positions (since size is limited but fees are fixed-bps).

**Recommendation:** Run the shadow evaluator on BTC first. It's the market with the most data per decision. If the feasibility signal shows up on BTC, it will show up more clearly on thinner books.

### 7.2 ETH — Solid second tier

**Positives:**
- Medium depth (~16k contracts), tight tracking error (3.9 bps).
- σ = 0.31% — more volatility than BTC, more ATM opportunities per day.

**Negatives:**
- 10× less volume than BTC → slower MM cadence (previous analysis showed p50 reprice ~8 seconds on one ETH window). Might be "too slow to be interesting" — MMs may not even be in most ETH markets.

**Recommendation:** Pair with BTC in the same shadow evaluator run. The ETH-specific "slow MM" observation is a **potential retail edge niche** — if books are stale for seconds, any reasonable operator can arb them.

### 7.3 SOL / XRP / DOGE — Possible second-tier candidates

**Positives:**
- Tracking error 3.8-4.8 bps (fine for coarse scoring).
- σ 0.26-0.31% — healthy ATM flow.
- Lower MM competition than BTC.

**Negatives:**
- Volume 1.7k-5.8k per window — limits size to low thousands of dollars per decision.

**Recommendation:** Include in Phase 1 data capture for parallel evaluation, but size expectations should be modest. Treat as "also-ran" assets for the first six weeks; promote only if the shadow evaluator shows edge.

### 7.4 BNB — Interesting despite low volume

**Positives:**
- Tightest tracking error in the set (3.12 bps p50).
- Lowest σ after BTC (0.20%). Calm price action.
- Lightest MM presence (volume only 974 median).

**Negatives:**
- Volume too thin for anything beyond smoke testing.
- BNB's CF Benchmarks RTI (BNBUSDRTI) basket is likely different from Coinbase's BNB price discovery — even though Coinbase lists BNB, it's probably not the primary venue. The 3.12 bps median is surprisingly good given this.

**Recommendation:** Include in Phase 1 to exercise the pipeline; de-prioritize size. BNB is a good "does our logic work end-to-end?" asset due to volume floor.

### 7.5 HYPE — Caution flag

**Positives:**
- Highest σ (0.38%). Most ATM markets per day → most candidate decisions.
- Modest volume (~1.8k contracts) — on par with DOGE.

**Negatives:**
- **Widest tracking error (6.5 bps p50, 25 bps p95).** Coinbase-vs-UHYPEUSDRTI is a rough proxy. The "U" prefix hints at a unique basket construction; we probably shouldn't use Coinbase-alone for HYPE.
- Bucket agreement drops 3-6 pp earlier than other assets — the ATM fog extends further out.

**Recommendation:** **Do not trade HYPE on Coinbase reference alone.** Either (a) license the UHYPEUSDRTI feed from CF Benchmarks, or (b) build a HYPE basket matching CF's constituents (involves finding the constituents first). Until then, HYPE is an **observational market** — track but don't score for execution.

## 8. Cross-asset summary

### 8.1 Top 3 actionable candidates for Phase 1 paper-in-prod

1. **BTC** — deep book, clear pattern, best calibrated signal.
2. **ETH** — fewer MMs visible, larger edge window per decision.
3. **SOL or XRP** — capacity-limited but tractable.

### 8.2 Assets to defer

- **HYPE** — reference-feed mismatch makes it unscorable without a proper basket.
- **BNB** — volume is too thin for meaningful realized P/L, though the feed is clean.

### 8.3 Asset universe summary

| Metric | Best | Worst |
|---|---|---|
| Volume (depth) | BTC | BNB |
| Tracking error (p50) | BNB (3.12 bps) | HYPE (6.53 bps) |
| σ_15min (biggest edge) | HYPE (0.38%) | BNB (0.20%) |
| ATM agreement % (coin-flip) | ~68-69% for most | HYPE (59%) |
| Deep-OTM agreement | ≥99% universally | — |

## 9. Risks and caveats (same set as the prior tracking-error report, applied here uniformly)

1. **Coinbase ≠ CF Benchmarks basket.** Per-asset, Coinbase is a varying-weight constituent. Median error 3-7 bps is a **lower bound** on what a multi-exchange basket could achieve. The 40% cost estimate for a licensed CF feed remains the same recommendation: marginal, not critical, for Phase 1 iteration.
2. **1-minute candles resolution.** Sub-minute tracking error — the exact signal a feed-lag scanner would exploit — is invisible at this resolution. The trade-level `/markets/trades` + `/products/{X}/trades` endpoints we scaffolded in `scripts/kalshi_trades_pull.py` + `scripts/coinbase_trades_pull.py` are where the next-level analysis lives.
3. **σ fit is non-robust.** Raw stdev is inflated by fat-tail outliers (MAD is ~33% lower). If a calibration period includes unusual days (e.g., liquidation cascade), σ will overshoot. A rolling MAD or EWMA would be the production fix.
4. **Volume column is lifetime market volume**, not snapshot book depth at the decision moment. Per-decision size capacity requires the live shadow evaluator + real-time book snapshots.
5. **HYPE reference uncertainty** (see §7.5).

## 10. Recommendations

### 10.1 Immediate (no new data)

- **Update `DEFAULT_SIGMA_15MIN` in `src/strategy/kalshi_fair_value.py`** to include XRP, DOGE, BNB, HYPE.
- **Add `SUPPORTED_ASSETS`** across the stack: market discovery, strategy, evaluator, reference source all currently hardcode `(btc, eth, sol)`. Extend to seven.
- **Flag HYPE as observation-only** in the strategy config until a proper reference basket is wired.

### 10.2 Near-term (unblocks richer signal)

- Run the **shadow evaluator live** against BTC, ETH, SOL, XRP, DOGE, BNB — five minutes of wall-clock captures per-decision book depth, which we don't have from the public settled-markets endpoint.
- Run the **sub-second feed-lag analysis** across 20+ volatile windows per asset (via `scripts/kalshi_trades_pull.py` + `scripts/coinbase_trades_pull.py`). The single-window analyses so far showed 100-ms lags in one window and 800-ms in another — we need sample size to characterize the distribution.
- **Add Kraken + Bitstamp reference sources** and aggregate into a "basket of 3" proxy. Should halve median tracking error and tighten calibration (especially for HYPE if Kraken carries it).

### 10.3 Long-term (if Phase 1 proves out)

- **License CF Benchmarks RTIs** once edge is proven and size-capacity is understood. The feed cost is dwarfed by position-accountability caps at Kalshi for a serious size.
- **Investigate BNBUSDRTI / UHYPEUSDRTI constituents.** CF publishes the methodology PDFs. Build a direct aggregator if edge economics justify.
- **Multi-asset pipeline.** Extend the scanner to scan all 7 assets in parallel; route capacity to where edge × depth × latency is best in that hour.

## 11. Data reproducibility

All data in `data/kalshi.db`. Fresh pipeline:

```bash
# Migrate schema (idempotent).
python3.11 scripts/migrate_db.py

# Clear any prior data (demo etc.)
sqlite3 data/kalshi.db "DELETE FROM kalshi_historical_markets; DELETE FROM reference_ticks;"

# 30 days of settled markets across 7 Kalshi series (~25 seconds).
python3.11 scripts/kalshi_public_pull.py --asset all --days 30

# 30 days of Coinbase 1-min candles across 7 products (~2 minutes).
END=$(python3.11 -c "import datetime;print((datetime.datetime.utcnow()-datetime.timedelta(minutes=5)).strftime('%Y-%m-%dT%H:%M:%SZ'))")
python3.11 scripts/coinbase_historical_pull.py --asset all --start 2026-03-20T00:00:00Z --end "$END"

# Per-asset σ calibration.
python3.11 scripts/calibrate_sigma.py --emit-python

# Re-run this analysis (inline script in docs/kalshi_crypto_multi_asset_report.md §3-6).
```

## 12. Sources

- [Kalshi API — /markets listing](https://docs.kalshi.com/reference/get-markets)
- [Kalshi API — /markets/trades (public)](https://docs.kalshi.com/reference/get-trades)
- [Kalshi Contract Terms PDFs (per-asset)](https://kalshi-public-docs.s3.amazonaws.com/contract_terms/)
- [CF Benchmarks — BRTI methodology](https://www.cfbenchmarks.com/data/indices/BRTI) (per-asset methodology pages available for each RTI)
- [Coinbase Exchange — Candlesticks](https://docs.cdp.coinbase.com/exchange/reference/exchangerestapi_getproductcandles)
