# Kalshi Phase-1 Smoke-Test Findings

**Research date:** 2026-04-20
**Status:** Demo-environment end-to-end plumbing verified. Demo resolution data found to be synthetic; prod historical pull required for real feasibility signal.
**Cross-links:** [`kalshi_scanner_implementation_tasks.md`](./kalshi_scanner_implementation_tasks.md) (task tracker), [`kalshi_scanner_execution_plan.md`](./kalshi_scanner_execution_plan.md), [`kalshi_crypto_fair_value_scanner_plan.md`](./kalshi_crypto_fair_value_scanner_plan.md).

> Not investment, legal, or tax advice.

## 1. What this doc covers

End-to-end smoke test of the Phase-1 data pipeline from Kalshi's demo REST API and Coinbase's public candles API, through the fair-value model (`src/strategy/kalshi_fair_value.py`), through the backtest runner (`src/run_kalshi_backtest.py`). The goal was to validate that every component works together on real bytes, not just unit-test fixtures. It succeeded at that goal — and surfaced an important limitation of the demo environment that shapes the Phase-1 plan.

## 2. What was pulled

All pulls ran against the **Kalshi demo environment** (no prod key yet; KYC in progress).

| Source | Endpoint | What | Volume | Time range |
|---|---|---|---:|---|
| Kalshi demo | `GET /historical/markets` (authenticated) | Settled 15-min crypto binaries, metadata + resolution | **17,924** | Nov 13 2025 – Feb 18 2026 |
|  · `KXBTC15M` | | BTC 15-min binaries | 7,159 | |
|  · `KXETH15M` | | ETH 15-min binaries | 7,189 | |
|  · `KXSOL15M` | | SOL 15-min binaries | 3,576 | |
| Coinbase | `GET /products/{BTC,ETH,SOL}-USD/candles?granularity=60` (public) | 1-minute close prices | **129,576** | Jan 20 – Feb 18 2026 (30 days) |
|  · BTC / ETH / SOL | | 1-min closes per asset | 43,192 each | |

Storage: SQLite (`data/kalshi.db`) tables `kalshi_historical_markets` and `reference_ticks`. Schema created by `scripts/migrate_db.py` per execution plan §2.4.

Scripts used:

- `scripts/kalshi_historical_pull.py` — authenticated, paginated `/historical/markets` walker (`src/kalshi_api.KalshiAPIClient`). RSA-PSS signed. Normalizes `greater_or_equal` → `at_least`, derives `series_ticker` from ticker prefix.
- `scripts/coinbase_historical_pull.py` — unauthenticated `/candles?granularity=60` backfill. 300-candle cursor-style pagination with `cursor_end = min_ts - 1` stepping backwards.

Total wall-clock for data pulls: ~55 seconds (17s Kalshi + 55s Coinbase across the 30-day range).

## 3. How the comparison works

For every Kalshi market that settled (`settled_result ∈ {yes, no}`), we:

1. Look up the most recent Coinbase `reference_ticks` row at or before the market's `close_ts`.
2. Compute `delta_bps = (cb_close − strike) / strike × 10000` (signed).
3. Apply the market's comparator (`above`, `at_least`, etc.) to `cb_close` vs `strike` → a predicted resolution.
4. Compare predicted vs actual Kalshi `settled_result`.

Agreement = Coinbase's close price would have resolved the market the same way Kalshi did.

**Note:** The basket-vs-single-venue caveat from `kalshi_crypto_fair_value_scanner_plan.md` §0.5 applies in reverse here. Kalshi resolves via CF Benchmarks RTIs (BRTI for BTC uses six constituent exchanges; ETH and SOL have their own baskets). Coinbase alone should track the basket within single-digit bps most of the time. Large or systematic disagreement therefore has two possible sources: Coinbase-vs-basket tracking error, or Kalshi resolution decoupled from any real external reference.

## 4. Headline numbers

### 4.1 Agreement summary per asset

| Series | Scored | Agree | Disagree | % Agree | Skipped (no CB data) |
|---|---:|---:|---:|---:|---:|
| KXBTC15M | 2,488 | 1,499 | 989 | **60.25%** | 4,671 |
| KXETH15M | 2,552 | 1,566 | 986 | **61.36%** | 4,637 |
| KXSOL15M | 2,484 | 1,428 | 1,056 | **57.49%** | 1,092 |

"Skipped" rows are Kalshi markets whose `close_ts` falls outside the 30-day Coinbase backfill window.

### 4.2 Percentiles of |cb_close − strike| / strike (bps) — all scored markets

| Series | N | p50 | p90 | p95 | p99 | max |
|---|---:|---:|---:|---:|---:|---:|
| KXBTC15M | 2,488 | 96 | 449 | 694 | 1,081 | 1,654 |
| KXETH15M | 2,552 | 135 | 614 | 797 | 1,163 | 1,737 |
| KXSOL15M | 2,484 | 153 | 718 | 984 | 1,420 | 2,239 |

The median Kalshi market has spot ~1% from strike at settlement; the 99th-percentile market has spot ~10% from strike. This is not a concentrated at-the-money regime — markets span a wide spread.

### 4.3 Percentiles of |cb_close − strike| on **disagreements only** (bps)

"Minimum tracking error that would have to exist between Coinbase and Kalshi's reference to flip the resolution relative to what Coinbase saw."

| Series | N | p50 | p90 | p95 | p99 | max |
|---|---:|---:|---:|---:|---:|---:|
| KXBTC15M | 989 | 124 | 474 | 705 | 1,046 | 1,496 |
| KXETH15M | 986 | 178 | 631 | 846 | 1,155 | 1,642 |
| KXSOL15M | 1,056 | 166 | 763 | 1,045 | 1,440 | 1,740 |

### 4.4 Agreement rate by |delta| bucket (bps)

| Series | [0,5) | [5,10) | [10,25) | [25,50) | [50,100) | [100,500) | [500,+∞) |
|---|---:|---:|---:|---:|---:|---:|---:|
| **KXBTC15M** | 67% | 71% | 76% | 64% | 55% | 55% | **57%** |
| **KXETH15M** | 54% | 76% | 77% | 78% | 60% | 54% | **56%** |
| **KXSOL15M** | 50% | 68% | 64% | 63% | 59% | 55% | **55%** |

## 5. The anomaly — demo resolution data appears synthetic

### 5.1 Expected shape if Coinbase tracked Kalshi's reference

Under any reasonable basket model, BRTI (and its ETH/SOL siblings) should track Coinbase within single-digit bps most of the time. If that's true, the **expected agreement rate by bucket** looks like:

- **[0, 5) bps:** ≈ 50-60%. These are genuine coin flips — basket-vs-Coinbase noise is on the same order as the distance to the strike, so ~half of markets flip.
- **[5, 25) bps:** rising quickly. Basket noise can still flip some, but most resolve with Coinbase.
- **[100, 500) bps:** ≈ 95%+. A 100 bp distance requires a 100 bp basket-vs-Coinbase divergence to flip, which is unusual.
- **[500, +∞) bps:** ≈ 99%+. A $500-bps-deep market essentially *cannot* flip due to basket tracking error alone.

### 5.2 What we actually measured

Deep-OTM/deep-ITM buckets (500+ bps from strike) show **55-57% agreement** across all three assets. That's not basket tracking error — a 500 bp divergence between Coinbase and BRTI does not happen in normal markets, and would never happen systematically.

55% is noticeably above 50%, so demo data isn't pure random noise. But it's far below the ≥95% we'd expect if resolutions were tied to real external prices.

### 5.3 Conclusion

**The Kalshi demo environment's `settled_result` field is not strongly coupled to real BTC/ETH/SOL price evolution during the market's 15-minute window.** Demo appears to exist to exercise the API surface (auth, pagination, schema) — not to replay real resolution outcomes. The ~55% agreement in deep-OTM buckets is consistent with demo resolutions being drawn from a distribution that has a weak correlation with the strike side Coinbase was on, but not the ~99% agreement that tracking against the real basket would produce.

### 5.4 How we'd confirm definitively (deferred pending prod access)

- Pull the same markets from **prod** `/historical/markets` and re-run §4.4. If prod's [500, +∞) bucket is ≥95% agreement across all three assets, the demo-is-synthetic hypothesis is confirmed and prod data is usable for feasibility.
- Spot-check several `raw_json` payloads of the same market ticker across prod vs demo. If close timestamps, open times, and tick size match but `settled_result` differs, that's direct evidence.

## 6. Implications for Phase 1

1. **No feasibility conclusions can be drawn from demo data.** The scanner, model, backtest, and analysis pipeline are end-to-end working, but the ground truth they're scored against is synthetic. `docs/kalshi_phase1_feasibility_report.md` cannot be produced until prod data lands.

2. **Prod access becomes the critical path.** KYC at `kalshi.com` is in flight (user-side, expected to clear within a few hours to days). Once the prod API key is generated:
   - `./scripts/store_kalshi_key.sh --env prod --key-id <uuid> --file <prod.key>`
   - Clear demo rows from SQLite (`DELETE FROM kalshi_historical_markets; DELETE FROM kalshi_historical_trades`).
   - Re-run `scripts/kalshi_historical_pull.py --days 30 --asset all` (expect 10-100× more markets than demo).
   - Re-run `scripts/coinbase_historical_pull.py` over the matching window.
   - Re-run the statistical comparison above. The [500, +∞) bucket should show ≥95% agreement on prod if real.

3. **What is meaningful from this exercise.** The pipeline works: RSA-PSS signing, cursor pagination, ISO-8601 parsing, comparator normalization, Coinbase backfill, DB roundtrip, fair-value scoring, Brier / calibration reporting, percentile analytics. When prod data arrives, running the full analysis is a one-line command.

4. **Subsequent work that is _not_ blocked on prod data.** The live shadow evaluator (P1-M4) can be built now — it reads live book + live reference and writes hypothetical decisions to `shadow_decisions`. Orders are structurally absent in Phase 1, so wiring it against demo (or forward-running on prod once available) is straightforward. Similarly the `src/execution/`, `src/pipeline/`, `src/persistence/` packages can land as skeleton code now, with the data layer swapped at run-time.

## 7. Code changes made during smoke-testing

Summarized here for audit; per-task status is in the implementation tracker.

- `src/kalshi_api.py` — direct-requests + RSA-PSS client that sidesteps `kalshi_python_sync` 3.2.0 pydantic bugs on `/historical/*`. Signs `{ts_ms}{METHOD}{path_without_query}` per Kalshi docs §3.2.
- `src/market/kalshi_market.py:make_client()` — works around two bugs in `kalshi_python_sync.ApiClient.set_kalshi_auth` (missing `KalshiAuth` import + path-vs-content confusion). Reads PEM bytes and assigns `client.kalshi_auth` directly.
- `scripts/kalshi_historical_pull.py` — adds `_to_epoch_s()` ISO-8601 parser, `_derive_series_ticker()` prefix-extraction, `COMPARATOR_MAP` for `greater_or_equal → at_least`.
- `scripts/coinbase_historical_pull.py` — 1-minute candle paginator with `cursor_end = oldest_ts − 1` stepping (earlier bug: `len(data) < 300` as an exit condition caused premature termination).
- `src/strategy/kalshi_fair_value.py` — unchanged; re-validated against real bytes.
- `src/run_kalshi_backtest.py` — unchanged; renders per-asset Brier + pooled calibration deciles.

## 8. Observations on the model (to revisit with prod data)

Even allowing for demo-data contamination, two structural observations about the fair-value model surfaced:

- **Overconfidence at 30s horizon.** Default `annual_vol_by_asset` scaled by `√(30s / 1yr)` produces a 30-second σ of ~0.06% of spot. This is tighter than real crypto microstructure at minute horizons. The model pushes p_yes to ~0 or ~1 for almost any spot-strike gap, producing a pathological bimodal calibration (decile 0 ≈ 3,500 markets; decile 9 ≈ 550; middle deciles ≈ 15).
- **Calibration approach for prod.** When prod data lands, replace the hard-coded `DEFAULT_ANNUAL_VOL` with a per-asset, per-horizon calibration learned from realized reference-price moves in `reference_ticks` over the same window. Specifically: compute the empirical distribution of 30-second (and 60-second) log-returns per asset and fit `σ_30s` directly. The fair-value model's `_sigma_over_horizon` hook accepts this without structural changes.

## 9. Test coverage at the time of these findings

**150 / 150 unit tests pass.** Suites:

```
tests/test_models.py                15
tests/test_platform_adapters.py      7
tests/test_migrate_db.py             6
tests/test_kalshi_market.py         29
tests/test_crypto_reference.py      19
tests/test_fair_value_model.py      17
tests/test_kalshi_strategy.py       12
tests/test_run_kalshi_backtest.py   10
tests/test_kalshi_api.py           18
tests/test_kalshi_historical_pull.py 9
tests/test_kalshi_track_reference.py 9
```

End-to-end smoke against demo + Coinbase verifies the pipeline byte-by-byte where units mock.

## 10. Sources

- [Kalshi API — Quick Start, Authenticated Requests](https://docs.kalshi.com/getting_started/quick_start_authenticated_requests)
- [Kalshi API — Demo Environment](https://docs.kalshi.com/getting_started/demo_env)
- [Kalshi CRYPTO15M Contract Terms (PDF)](https://kalshi-public-docs.s3.amazonaws.com/contract_terms/CRYPTO15M.pdf)
- [CF Benchmarks — BRTI methodology](https://www.cfbenchmarks.com/data/indices/BRTI)
- [Coinbase Exchange API — Candles](https://docs.cdp.coinbase.com/exchange/reference/exchangerestapi_getproductcandles)

## 11. Next steps (summary)

- **Unblocked now:** proceed to P1-M4 (live shadow evaluator scaffold + tests) — code-only, data-swappable.
- **Blocked on prod API key:** re-run sections 4 and 5 against prod; generate `docs/kalshi_phase1_feasibility_report.md`; generate `docs/kalshi_crypto_fair_value_tracking_error_report.md`.
- **Optional upgrades** (discuss when prod lands): multi-exchange basket (Kraken, Bitstamp) to match BRTI constituents; per-asset realized-vol calibration for the fair-value model.
