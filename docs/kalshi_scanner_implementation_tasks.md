# Kalshi Scanner — Implementation Task Tracker

**Research date:** 2026-04-19
**Structure:** Two phases — Phase 1 (Scanner / Feasibility Research, zero money at risk) → Phase 1→2 Gate → Phase 2 (Execution, real money).
**Status:** Pre-implementation — all tasks `[ ]`.
**Companion docs:** [`kalshi_crypto_fair_value_scanner_plan.md`](./kalshi_crypto_fair_value_scanner_plan.md) (strategy), [`kalshi_scanner_execution_plan.md`](./kalshi_scanner_execution_plan.md) (architecture / how-to).
**Repo:** `/Users/tamir.wainstain/src/KalshiTrader/` — everything (docs + code + tests + configs + deploy) lives here. All paths below are relative to this repo unless prefixed.

> **Not investment, legal, or tax advice.**

## 0. Status legend

- `[ ]` — not started
- `[~]` — in progress
- `[x]` — done (acceptance criterion verified)
- `[!]` — blocked (note the blocker in the task body)
- `[-]` — cancelled / superseded

## 1. Phase goals

**Phase 1 — Feasibility:** Prove or disprove a capturable timing-lag edge between CF Benchmarks reference prices and the Kalshi crypto 15-min orderbook. Collect historical + live data; run a shadow evaluator that records hypothetical decisions without trading; produce a feasibility report. **No orders. No real money at risk.**

**Phase 2 — Execution:** Only if Phase 1 proves edge. Risk rules, paper executor, live executor (three-opt-in gated), custom dashboard, paper-in-prod for 4 weeks, live small-size for 2 weeks, stepped scale.

## 2. Progress summary

| Phase | Milestone | Status | Done / Total |
|---|---|---|---|
| — | Open architectural decisions (repo bootstrap + platform primitives) | pending | 0 / 2 |
| — | Prerequisites | not started | 0 / 5 |
| P1 | M0 Repo prep | not started | 0 / 6 |
| P1 | M1 Live data collection | not started | 0 / 13 |
| P1 | M2 Historical data collection | not started | 0 / 6 |
| P1 | M3 Fair-value model + backtest | not started | 0 / 8 |
| P1 | M4 Live shadow evaluator | not started | 0 / 7 |
| P1 | M5 Feasibility analysis + report | not started | 0 / 5 |
| — | **Phase 1 → Phase 2 Gate** | **pending** | **0 / 1** |
| P2 | M1 Risk rules + Paper executor | not started | 0 / 13 |
| P2 | M2 Live executor | not started | 0 / 2 |
| P2 | M3 Custom dashboard + pipeline | not started | 0 / 5 |
| P2 | M4 Paper in prod (4 weeks) | not started | 0 / 3 |
| P2 | M5 Live small size (2 weeks) | not started | 0 / 5 |
| P2 | M6 Scale | not started | 0 / 4 |
| — | Cross-cutting | not started | 0 / 4 |
| **Total** | | **not started** | **0 / 89** |

## 3. Kalshi API reference card (source of truth)

Fetched 2026-04-19 from `docs.kalshi.com`.

### 3.1 Base URLs

| | Prod | Demo |
|---|---|---|
| REST API | `https://api.elections.kalshi.com/trade-api/v2` | `https://demo-api.kalshi.co/trade-api/v2` |
| WebSocket | `wss://api.elections.kalshi.com/` | *not documented — verify before P2-M4* |
| Web dashboard | `https://kalshi.com` | `https://demo.kalshi.co/` |

Credentials are **not shared** between demo and prod.

### 3.2 Authentication — RSA-PSS with SHA-256

- Keys generated at `kalshi.com/account/profile` → "API Keys" → "Create New API Key".
- Required headers: `KALSHI-ACCESS-KEY` (Key ID), `KALSHI-ACCESS-TIMESTAMP` (ms string), `KALSHI-ACCESS-SIGNATURE` (base64 RSA-PSS(SHA-256, MGF1(SHA-256), salt_length=DIGEST_LENGTH)).
- **Signed message format:** `{timestamp_ms}{METHOD}{path_without_query}`. Example: `1699564800000GET/trade-api/v2/portfolio/balance`.
- WebSocket handshake uses the same API-key headers.

### 3.3 Official Python SDKs

- **Sync:** `pip install kalshi_python_sync` — `KalshiClient`, `Configuration(host=..., api_key_id=..., private_key_pem=...)`.
- **Async:** `pip install kalshi_python_async`.
- **Deprecated:** `kalshi-python` — do not use.

### 3.4 Orderbook response shape

- Binary markets store **only bids**. Asks derived: `YES ask @ X = NO bid @ (1 − X)`.
- Prices: dollar strings, 4-decimal precision (e.g. `"0.4200"`).
- Quantities: fixed-point strings (e.g. `"13.00"`).
- Arrays sorted ascending; best bid is the last element.

### 3.5 WebSocket channels

Channel: `orderbook_delta` at `wss://api.elections.kalshi.com/orderbook_delta`.

Subscribe message:
```json
{"id": 1, "cmd": "subscribe", "params": {
   "channels": ["orderbook_delta"],
   "market_tickers": ["KXBTC15M-...", "KXETH15M-...", "KXSOL15M-..."]
}}
```

### 3.6 Market lifecycle

`initialized → active → {inactive → active}* → closed → determined → {disputed → amended}* → finalized`.

### 3.7 Rate limits (per second, per tier)

| Tier | Read | Write |
|---|---|---|
| Basic | 20 | 10 |
| Advanced | 30 | 30 |
| Premier | 100 | 100 |
| Prime | 400 | 400 |

Writes = order-mutating endpoints only. Current tier: `GET /account/api-limits`.

### 3.8 Key REST endpoints

| Purpose | Method + Path | Auth? | Phase |
|---|---|---|---|
| List series | `GET /series` | public | P1 |
| Get series | `GET /series/{ticker}` | public | P1 |
| List events | `GET /events` | public | P1 |
| Get event | `GET /events/{ticker}` | public | P1 |
| List markets | `GET /markets?series_ticker=…&status=…` | public | P1 |
| Get market | `GET /markets/{ticker}` | public | P1 |
| Orderbook | `GET /markets/{ticker}/orderbook` | public | P1 |
| Candlesticks | `GET /markets/{ticker}/candlesticks` | public | P1 |
| Historical markets | `GET /historical/markets` | auth | P1 |
| Historical trades | `GET /historical/trades` | auth | P1 |
| Exchange schedule | `GET /exchange/schedule` | public | P1 |
| Series fee changes | `GET /exchange/series-fee-changes` | public | P1 |
| API limits | `GET /account/api-limits` | auth | P1 |
| Balance | `GET /portfolio/balance` | auth | P2 |
| Positions | `GET /portfolio/positions` | auth | P2 |
| Fills | `GET /portfolio/fills` | auth | P2 |
| Settlements | `GET /portfolio/settlements` | auth | P1 / P2 |
| Create order | `POST /portfolio/orders` | auth | P2 |
| Cancel order | `DELETE /portfolio/orders/{id}` | auth | P2 |

### 3.9 Pricing + fee conventions

- Prices in dollar strings; parse to `Decimal` directly.
- Quantities in fixed-point dollar-strings; parse to `Decimal`.
- Fees: trade fee (round up $0.0001) + rounding fee (≤$0.0099) − per-order rebate. Net ≥ $0. Fetch rates via `GET /exchange/series-fee-changes`.

## 4. Open architectural decisions (resolve before P1-M0)

Track status here; resolutions feed every downstream task.

- [x] **A-01. Platform primitives.** **Resolved 2026-04-19: option (a) — add `trading_platform` as submodule at `lib/trading_platform/` + local `src/platform_adapters.py`.** Upstream URL: `https://github.com/twainstain/trading-platform.git` (same as the DEX-bot repo uses).
- [ ] **A-02. Repo shell bootstrap.** Choose: (a) selective restore from git history (keep generic scaffolding like `pyproject.toml`, `docker-compose.yml`, `scripts/migrate_db.py`, persistence, pytest; drop DEX-specific modules); (b) start fresh. **Working tree check (2026-04-19):** `src/`, `tests/`, `scripts/`, `lib/` all absent from disk — DEX code has been deleted from the working tree, only pending the cleanup commit. This effectively makes option (b) the default-by-state. Confirm or execute (a) before P1-M0-T01.

## 5. Prerequisites (complete before P1-M0)

- [ ] **P-01.** User has KYC'd Kalshi account; state is not on the blocklist.
- [ ] **P-02.** User has generated a **demo** API key pair at `demo.kalshi.co/account/profile`. Private key PEM saved securely (outside repo).
- [ ] **P-03.** User has decided Phase-1 research budget and committed to zero-money-at-risk.
- [ ] **P-04.** User has read this repo's `CLAUDE.md` + `kalshi_scanner_execution_plan.md` §1 ground rules.
- [ ] **P-05.** Working tree is in a known-clean state: pending deletions from the 2026-04-19 cleanup are either committed (removing the DEX code) or the "selective restore" (A-02) has been executed and committed. Either way, `git status` before P1-M0 should be tidy.

---

# PHASE 1 — SCANNER / FEASIBILITY RESEARCH

**Goal:** Prove or disprove the timing-lag edge by collecting historical and live data, scoring hypothetical decisions against realized outcomes, and producing a feasibility report. **No orders.**

**Estimated duration:** 6–8 weeks total (incl. 4-week live shadow-evaluator run).

**End-of-phase artifact:** `docs/kalshi_phase1_feasibility_report.md` with go/no-go decision.

## P1-M0 — Repo prep (1 day, assuming A-01 + A-02 resolved)

- [ ] **P1-M0-T01.** Execute A-02's choice. If "selective restore": `git checkout <pre-cleanup-commit> -- pyproject.toml docker-compose.yml Dockerfile scripts/migrate_db.py src/persistence/ src/env.py src/core/models.py tests/conftest.py` (tune list per what's needed); then `git rm` all DEX-specific modules not in the scope. If "start fresh": create empty `src/`, `tests/`, `scripts/` layouts.
- [ ] **P1-M0-T02.** Execute A-01's choice. If "submodule": `git submodule add <URL> lib/trading_platform`. Wire a minimal `src/platform_adapters.py` re-exporting `CircuitBreaker`, `CircuitBreakerConfig`, `RetryPolicy`, `PriorityQueue`, plus a `KalshiAPIError` local class.
- [ ] **P1-M0-T03.** Update `pyproject.toml` deps: add `kalshi_python_sync`, `websockets>=12`, `pandas>=2.0`, `pyarrow>=15.0`. Keep `psycopg2-binary`, `fastapi`, `uvicorn`, `requests`, `python-dotenv`, `pytest`. Install: `pip install -e .`.
- [ ] **P1-M0-T04.** Append Kalshi env vars to `.env.example`: `KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY_PATH`, `KALSHI_ENV` (`demo`|`prod`), `CF_BENCHMARKS_API_KEY` (blank for P1).
- [ ] **P1-M0-T05.** In `src/core/models.py`, add `SUPPORTED_VENUES = ("kalshi",)` alongside `SUPPORTED_CHAINS` (or create the file if A-02 = "start fresh"). Add the Kalshi-specific `MarketQuote` fields per execution plan §2.1.
- [ ] **P1-M0-T06.** Add Phase-1 research tables to `scripts/migrate_db.py`: `kalshi_historical_markets`, `kalshi_historical_trades`, `kalshi_live_book_snapshots`, `reference_ticks`, `shadow_decisions` (schema in execution plan §2.4 + P1-M4-T03 for `shadow_decisions`). Migration runs clean on SQLite and Postgres dump.

Verification:
```bash
python3.11 -m pytest tests/ -q
python3.11 -c "import kalshi_python_sync, websockets, pandas, pyarrow; print('ok')"
python3.11 scripts/migrate_db.py
```

## P1-M1 — Live data collection (3–5 days)

### KalshiMarketSource (read-only)

- [ ] **P1-M1-T01.** Create `src/market/kalshi_market.py` with `KalshiMarketSource` stub (methods: `start`, `stop`, `get_quotes`, `is_healthy`).
- [ ] **P1-M1-T02.** Wire `KalshiClient` from `kalshi_python_sync`. Read `KALSHI_API_KEY_ID` + PEM path from env. Validate: `client.get_balance()` on demo returns a balance.
- [ ] **P1-M1-T03.** Implement `_discover_active_markets()` — `GET /markets?status=active` filtered to crypto-15M series. First verify exact series tickers via `GET /series?category=crypto` and log.
- [ ] **P1-M1-T04.** Implement WS subscription to `wss://api.elections.kalshi.com/orderbook_delta` with signed handshake; subscribe to tickers from T03; maintain in-memory L1 + L2 book state.
  - Verify: snapshot + ≥ 10 deltas in 60 s on a busy market.
- [ ] **P1-M1-T05.** Map book state to `MarketQuote` with Kalshi-specific fields (execution plan §2.1). Prices → `Decimal` directly from dollar strings.
- [ ] **P1-M1-T06.** Implement lifecycle tag (`opening | active | final_minute | closed | settled`) from `close_time` / `status` / `time_remaining_s`. 15-min boundary: unsubscribe settled, subscribe new.
- [ ] **P1-M1-T07.** Failure modes: WS reconnect with exponential backoff via `RetryPolicy`; stale-book `warning_flags=("stale_book",)`; 429 token-bucket pacing.
- [ ] **P1-M1-T08.** Write `tests/test_kalshi_market.py` — ≥ 15 assertions covering subscription, snapshot/delta parsing, lifecycle transitions, stale detection, reconnect.

### CryptoReferenceSource

- [ ] **P1-M1-T09.** Create `src/market/crypto_reference.py` with `CryptoReferenceSource` Protocol + `BasketReferenceSource` class.
- [ ] **P1-M1-T10.** Implement `BasketReferenceSource` per asset — subscribe to CF Benchmarks constituent exchanges (verify current list from `cfbenchmarks.com` methodology per asset; distinct for BTC / ETH / SOL). Aggregate per-second using CF's published methodology.
- [ ] **P1-M1-T11.** Add `LicensedCFBenchmarksSource` stub, gated on `CF_BENCHMARKS_API_KEY`. No-op if blank.
- [ ] **P1-M1-T12.** Persist every reference tick to `reference_ticks` (asset, ts_us, price, src).
- [ ] **P1-M1-T13.** Write `tests/test_crypto_reference.py` — rolling-average math, outlier rejection, 60 s window edge cases.

Verification:
```bash
python3.11 -m pytest tests/test_kalshi_market.py tests/test_crypto_reference.py -q
```

## P1-M2 — Historical data collection (2–3 days)

- [ ] **P1-M2-T01.** Create `scripts/kalshi_historical_pull.py` — CLI pulling past N days of crypto 15M markets via `GET /historical/markets` (cursor pagination). Params: `--days N --asset {btc,eth,sol,all} --db-url`.
- [ ] **P1-M2-T02.** Extend the script to pull `GET /historical/trades` for discovered market tickers — every fill with ts / price / qty / taker side.
- [ ] **P1-M2-T03.** Pull candlesticks via `GET /markets/{ticker}/candlesticks?period_interval=1` (1 min) per window — for book / midpoint cross-check.
- [ ] **P1-M2-T04.** Stand up continuous basket-proxy capture daemon (systemd unit or EC2 cron). Must run for the full Phase-1 shadow-evaluator window so historical-vs-realtime comparison has aligned coverage. Writes to `reference_ticks`.
- [ ] **P1-M2-T05.** Confirm the P1-M0-T06 migration covers all 5 tables; add integration test that inserts + selects a row in each.
- [ ] **P1-M2-T06.** Run historical pull for last 30 days of crypto-15M windows. Sanity check: ≈ 96 windows/day × 30 days × 3 assets ≈ 8,600 historical-market rows.

Verification:
```bash
python3.11 scripts/kalshi_historical_pull.py --days 30 --asset all
```

## P1-M3 — Fair-value model + backtest (4–6 days)

- [ ] **P1-M3-T01.** Create `src/strategy/kalshi_fair_value.py` with `FairValueModel` class.
- [ ] **P1-M3-T02.** Implement `time_remaining_s > 60` regime: Brownian projection of 60 s-avg at close; integrate over comparator's acceptance region.
- [ ] **P1-M3-T03.** Implement `time_remaining_s ≤ 60` regime: partial-observation conditional distribution of remaining averaged ticks.
- [ ] **P1-M3-T04.** Implement no-data haircut (`no_data_adjustment`, default 0.005) per strategy plan §0.5.
- [ ] **P1-M3-T05.** Implement `KalshiFairValueStrategy` satisfying `Strategy` protocol. In P1 it emits hypothetical decisions that feed the shadow evaluator (P1-M4) — **does not** emit live-executable `Opportunity` objects.
- [ ] **P1-M3-T06.** Create `src/run_kalshi_backtest.py` — replay entrypoint reading DB-stored Phase-1 data; scores Brier vs naive `p_yes = implied_mid_at_entry`; outputs report.
- [ ] **P1-M3-T07.** Unit tests: `tests/test_fair_value_model.py`, `tests/test_kalshi_strategy.py`.
- [ ] **P1-M3-T08.** Run backtest on ≥ 500 historical windows per asset. Acceptance: Brier < naive baseline; calibration error ≤ 3 pp in every decile.

Verification:
```bash
python3.11 -m pytest tests/test_fair_value_model.py tests/test_kalshi_strategy.py -q
python3.11 -m run_kalshi_backtest --data-from db --report /tmp/backtest.md
```

## P1-M4 — Live shadow evaluator (2–3 days)

Runs in prod against real Kalshi markets + real reference feed, records every hypothetical decision + realized outcome. **Never submits orders.**

- [ ] **P1-M4-T01.** Create `src/execution/kalshi_shadow_evaluator.py` with `KalshiShadowEvaluator`. Inputs: live `MarketQuote` stream + `FairValueModel`. Output: rows in `shadow_decisions`.
- [ ] **P1-M4-T02.** Wire `KalshiMarketSource` + `CryptoReferenceSource` + `FairValueModel` into the evaluator. **No `Executor` protocol wired** — trading path is structurally absent in P1.
- [ ] **P1-M4-T03.** Finalize `shadow_decisions` schema:
  - `id` (pk), `market_ticker`, `ts_us`
  - `p_yes`, `ci_width`, `reference_price`, `reference_60s_avg`, `time_remaining_s`
  - `best_yes_ask`, `best_no_ask`, `book_depth_yes_usd`, `book_depth_no_usd`
  - `recommended_side` (`yes|no|none`), `hypothetical_fill_price`, `hypothetical_size_contracts`
  - `expected_edge_bps_after_fees`, `fee_bps_at_decision`
  - `realized_outcome` (populated at settlement: `yes|no|no_data`)
  - `realized_pnl_usd` (populated at settlement, assuming hypothetical fill)
  - `latency_ms_ref_to_decision`, `latency_ms_book_to_decision`
  - Indexes on `market_ticker`, `ts_us`, `realized_outcome`.
- [ ] **P1-M4-T04.** Post-window reconciler: at `expiration_ts + 30s`, call public `GET /markets/{ticker}` for the settled result; update realized columns.
- [ ] **P1-M4-T05.** Create `src/run_kalshi_shadow.py` — long-running entry point (event-driven). Graceful shutdown on SIGINT/SIGTERM.
- [ ] **P1-M4-T06.** Deploy to EC2 (CPU-light). systemd unit `kalshi-shadow.service`. `scripts/run_local.sh` + a prod equivalent. Log rotation configured.
- [ ] **P1-M4-T07.** Integration test `tests/test_kalshi_shadow_evaluator.py` — mocked Kalshi WS + mocked reference feed; replay fixture; assert expected rows in `shadow_decisions`.

Verification:
```bash
python3.11 -m pytest tests/test_kalshi_shadow_evaluator.py -q
python3.11 -m run_kalshi_shadow --iterations 3 --no-sleep
# After 24h of prod run:
psql $DATABASE_URL -c "select count(*), avg(expected_edge_bps_after_fees) from shadow_decisions where ts_us > now() - interval '24 hours';"
```

## P1-M5 — Feasibility analysis + report (1 week)

- [ ] **P1-M5-T01.** `notebooks/kalshi_lag_analysis.ipynb` (or `.py`): CF-RTI-move → Kalshi-book-reprice lag distribution per asset. Median, p90, p95, p99 lag in ms; split by full-window vs final-minute.
- [ ] **P1-M5-T02.** `notebooks/kalshi_edge_analysis.ipynb`: realized-if-traded edge per `shadow_decisions` row; per-asset, per-strategy-sub; hit-rate; Brier.
- [ ] **P1-M5-T03.** `notebooks/kalshi_capacity_analysis.ipynb`: theoretical daily-$ capture at current book depth per candidate signal.
- [ ] **P1-M5-T04.** Produce `docs/kalshi_crypto_fair_value_tracking_error_report.md`: basket-proxy vs CF-RTI tracking error per asset. Go/no-go on licensed feed.
- [ ] **P1-M5-T05.** Produce **`docs/kalshi_phase1_feasibility_report.md`** — end-of-Phase-1 deliverable. Sections: (1) data collected; (2) lag measurement summary; (3) realized-edge summary; (4) capacity estimate; (5) risks realized vs anticipated; (6) **explicit Phase-2 go/no-go** with pre-committed thresholds.

## Phase 1 → Phase 2 GATE

- [ ] **P1-GATE.** Review the feasibility report with the user. Record decision inline (date, yes/no, reasoning).
  - **No-go:** freeze at end of Phase 1. Shadow evaluator keeps running for ongoing observation; no Phase-2 work.
  - **Go:** proceed.

---

# PHASE 2 — EXECUTION

**Gated on P1-GATE = Go.** Estimated duration ~8 weeks.

## P2-M1 — Risk rules + Paper executor (3–4 days)

- [ ] **P2-M1-T01.** Create `src/risk/kalshi_rules.py` with scaffolds for 9 rule classes.
- [ ] **P2-M1-T02.** `MinEdgeAfterFeesRule` — live fee schedule lookup; default 100 bps above fees. Fails closed.
- [ ] **P2-M1-T03.** `TimeWindowRule` — default `[5s, 60s]` of final window.
- [ ] **P2-M1-T04.** `CIWidthRule` — default max 0.15.
- [ ] **P2-M1-T05.** `OpenPositionsRule` — default max 3 concurrent.
- [ ] **P2-M1-T06.** `DailyLossRule` — default $250/day stop.
- [ ] **P2-M1-T07.** `ReferenceFeedStaleRule` — reject if no tick in ≥ 3 s.
- [ ] **P2-M1-T08.** `BookDepthRule` — default min $200 top-of-book.
- [ ] **P2-M1-T09.** `NoDataResolveNoRule` — reject YES when CF Benchmarks health degraded.
- [ ] **P2-M1-T10.** `PositionAccountabilityRule` — per-strike cap $2,500 (1/10 of Kalshi's $25k).
- [ ] **P2-M1-T11.** Create `src/execution/kalshi_executor.py` with `KalshiPaperExecutor` (default). Records decision → virtual fill → settlement → P/L.
- [ ] **P2-M1-T12.** Unit tests for each rule (`tests/test_kalshi_rules.py`). ≥ 3 asserts per rule: approve / reject / ambiguous.
- [ ] **P2-M1-T13.** Paper-executor test (`tests/test_kalshi_executor_paper.py`): full lifecycle including post-window reconciliation.

## P2-M2 — Live executor (2 days)

- [ ] **P2-M2-T01.** Add `KalshiLiveExecutor` in `src/execution/kalshi_executor.py`. Three-opt-in gate: `--execute` flag AND `KALSHI_API_KEY_ID` set AND config `mode: "live"` + `dry_run: false`. Uses `RetryPolicy` for 5xx/transient; cancel-on-timeout (default 3 s); post-fill reconciliation via `GET /portfolio/positions`.
- [ ] **P2-M2-T02.** `tests/test_kalshi_executor_live.py`: three-opt-in gate, order-create happy path, order-create reject, cancel-on-timeout, reconciliation discrepancy detection. Mocked `KalshiClient`.

## P2-M3 — Custom dashboard + pipeline wiring (3–4 days)

- [ ] **P2-M3-T01.** Create `src/run_kalshi_event_driven.py` — WS event → scanner → bounded queue → consumer thread → 6-stage pipeline. Reuses `BasePipeline` (per A-01).
- [ ] **P2-M3-T02.** Extend pipeline `verify` stage for Kalshi: at `expiration_ts + 30 s`, call `GET /portfolio/settlements` (authenticated) and write to the `opportunities` table's realized fields.
- [ ] **P2-M3-T03.** Add Kalshi dashboard routes in `src/dashboards/kalshi.py`:
  - `/kalshi` — active windows, book depths, strike grid, feed health, risk-rule rejection counters.
  - `/kalshi/portfolio` — `GET /portfolio/balance` / `positions` / `orders?status=resting` / `fills?limit=50`. 5 s poll; cache in-memory; mark stale > 15 s.
  - `/kalshi/decisions` — recent `shadow_decisions` + `opportunities` with drill-in.
  - `/kalshi/performance` — rolling P/L, daily-loss, rolling Brier, per-asset.
  - `/kalshi/health` — WS state, last tick per asset, rate-limit headroom, breaker state.
- [ ] **P2-M3-T04.** `config/kalshi_fair_value_config.json` — `mode: "paper"` default, full risk-rule config tree.
- [ ] **P2-M3-T05.** Integration test `tests/test_kalshi_pipeline.py` — mocked WS + mocked reference; replay; assert `Opportunity` sequence, paper fills, dashboard endpoints render.

Verification:
```bash
python3.11 -m pytest tests/ -q
python3.11 -m run_kalshi_event_driven --paper --iterations 3 --no-sleep
./scripts/run_local.sh
# Browser http://localhost:8000/kalshi and all subroutes render.
```

## P2-M4 — Paper in prod (4 weeks)

- [ ] **P2-M4-T01.** Create `deploy/cloudformation.yml` (separate stack if restored from git history, adapt for Kalshi); `scripts/deploy_prod.sh` with `--status` / `--logs` / `--deploy` / `--rollback`; ECR repo `kalshi-scanner`. First deploy with `KALSHI_ENV=demo` for 72 h, then `KALSHI_ENV=prod` with `mode: "paper"`.
- [ ] **P2-M4-T02.** Run paper in prod 4 weeks. Daily one-line entry in `docs/kalshi_paper_trading_daily_log.md`.
- [ ] **P2-M4-T03.** End-of-phase report `docs/kalshi_fair_value_paper_trading_report.md`. Decision: Go to P2-M5 only if realized paper edge matches feasibility-report expectation within tolerance.

## P2-M5 — Live, small size (2 weeks) — GATED on P2-M4-T03

- [ ] **P2-M5-T01.** Gate check.
- [ ] **P2-M5-T02.** Populate `config/kalshi_fair_value_live.json` — `mode: "live"`, `max_position_notional_usd: 100`, `daily_loss_stop_usd: 100`, `max_concurrent_positions: 1`.
- [ ] **P2-M5-T03.** User generates prod Kalshi API keys. PEM migrated to AWS Secrets Manager. IAM role on EC2 granted secret-read.
- [ ] **P2-M5-T04.** Deploy live: `./scripts/deploy_prod.sh --deploy --live`. Script refuses `--live` if config `mode != "live"` or Secrets Manager secret missing.
- [ ] **P2-M5-T05.** Daily reconciliation (fills vs `GET /portfolio/fills`) for 14 days. End-of-phase report `docs/kalshi_fair_value_live_small_size_report.md`.

## P2-M6 — Scale — GATED on P2-M5-T05

- [ ] **P2-M6-T01.** Gate check.
- [ ] **P2-M6-T02.** Step 1: raise `max_position_notional_usd` to $250, `daily_loss_stop_usd` to $250. 1 week. Abort if edge drops > 30% vs P2-M5.
- [ ] **P2-M6-T03.** Step 2: $500 / $500. 1 week. Same abort rule.
- [ ] **P2-M6-T04.** Steady-state monthly review: `docs/kalshi_monthly_review_{YYYY-MM}.md`.

## 6. Cross-cutting tasks

- [ ] **C-01.** Maintain a `claude_session/current.md` (this repo) after every meaningful change: active milestone, test count, blockers.
- [ ] **C-02.** Observability from P1-M4 onward: alerts for Kalshi WS disconnect > 30 s, reference-feed staleness > 5 s, daily-loss > 50% of stop, 429 burst, unhandled exception in pipeline consumer.
- [ ] **C-03.** Kalshi API changelog watch: re-read `docs.kalshi.com/changelog/` monthly; update §3 here + re-date-stamp.
- [ ] **C-04.** Sanity cron: every 2 weeks, re-fetch `GET /exchange/series-fee-changes`; confirm fee-schedule caller matches live values.

## 7. How to use this tracker

1. Start: `[ ]` → `[~]`; bump the progress-summary in-progress count.
2. Complete: verify acceptance → `[x]`; bump complete count; one-line note inline (date + PR link).
3. Blocked: `[!]` + "**Blocker:**" bullet. Unblock → remove blocker note → `[~]`.
4. Scope change: never delete — mark `[-]` with reason. IDs stay stable.
5. New work: add under appropriate milestone with next available ID. IDs don't get reused.

## 8. Cross-links

- [`kalshi_crypto_fair_value_scanner_plan.md`](./kalshi_crypto_fair_value_scanner_plan.md)
- [`kalshi_scanner_execution_plan.md`](./kalshi_scanner_execution_plan.md)
- `CLAUDE.md` (this repo)

## 9. Sources

- [Kalshi docs sitemap](https://docs.kalshi.com/llms.txt)
- [Kalshi auth quickstart](https://docs.kalshi.com/getting_started/quick_start_authenticated_requests)
- [Kalshi market-data quickstart](https://docs.kalshi.com/getting_started/quick_start_market_data)
- [Kalshi demo environment](https://docs.kalshi.com/getting_started/demo_env)
- [Kalshi orderbook responses](https://docs.kalshi.com/getting_started/orderbook_responses)
- [Kalshi rate limits](https://docs.kalshi.com/getting_started/rate_limits)
- [Kalshi market lifecycle](https://docs.kalshi.com/getting_started/market_lifecycle)
- [Kalshi fixed-point migration](https://docs.kalshi.com/getting_started/fixed_point_migration)
- [Kalshi fee rounding](https://docs.kalshi.com/getting_started/fee_rounding)
- [Kalshi Python SDK](https://docs.kalshi.com/sdks/python/quickstart)
- [Kalshi WS connection](https://docs.kalshi.com/websockets/websocket-connection)
- [Kalshi WS orderbook-delta](https://docs.kalshi.com/websockets/orderbook-updates)
- [Kalshi CRYPTO15M contract terms](https://kalshi-public-docs.s3.amazonaws.com/contract_terms/CRYPTO15M.pdf)
