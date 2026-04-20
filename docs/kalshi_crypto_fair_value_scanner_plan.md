# Kalshi Crypto Fair-Value Scanner — Build & Deploy Plan

**Research date:** 2026-04-19 (resolution mechanics re-verified against Kalshi `CRYPTO15M.pdf`, document-creation date 2026-03-25).

> Not investment, legal, or tax advice. This is a build plan for a research-grade scanner, intended to be validated in paper-trading before any live capital is deployed.

## 0. Reality check up front

Before committing engineering time, the thesis must stand on its own merits — and the honest version is narrower than "arbitrage."

- **This is not arbitrage.** Kalshi 15-min crypto markets are binary **threshold** contracts with real resolution risk (not a single Up/Down flip — see §0.5). The trade is **fair-value / statistical**: our probability estimate vs Kalshi's book.
- **The only structurally-capturable edge for an individual is near-expiry.** The last 30–60 seconds of each 15-min window, when the 60-second averaging window is already partly observed, is where outcome determinism ramps up faster than the book sometimes converges.
- **Mid-window fair-value pricing is market-maker territory.** Kalshi's crypto books are provisioned by sophisticated MMs pricing against the CF Benchmarks Real-Time Indices (per-asset; BTC = BRTI) in real time. We do not out-model them mid-window; we look for the seconds where they lag.
- **AWS us-east-1 helps but is not colocation.** Co-located MMs have tens-to-hundreds of microseconds to Kalshi matching. Our realistic floor on a t3.small / small Fargate task is low-single-digit milliseconds if we're careful. That's good enough to hit stale quotes sometimes; it is not good enough to win every race.
- **Expectation anchor:** if this works, realistic returns are 10–30% annualized on deployed capital, capacity-capped in low-5-figures of working balance. Not life-changing. The plan below is gated behind paper-trading measurement because the prior probability the edge exists at size is moderate, not high.

Background analysis in `prediction_market_arbitrage_video_breakdown_and_opinion.md`. (Two earlier cross-references — `polymarket_winning_strategies.md` and `polymarket_kalshi_15m_btc_convergence_measurement.md` — were removed from the working tree during the 2026-04-19 repo cleanup; their content is archival in git history.)

## 0.5 Resolution mechanics (authoritative)

From Kalshi's published contract terms for the 15-min crypto series (`CRYPTO15M.pdf`, document-creation date 2026-03-25). These are the facts the whole scanner is predicated on; re-verify on any material spec change.

- **Source Agency: CF Benchmarks.** The reference data comes from CF Benchmarks, not directly from CME, not from a single spot exchange, not Chainlink, not UMA.
- **Underlying: simple average of the CF `<cryptocurrency>` Real-Time Index over the 60 seconds prior to `<time>`** (the market's expiration time). The contract terms name "Bitcoin Real-Time Index" (i.e. CF BRTI) as the example. ETH and SOL use the corresponding CF Benchmarks Real-Time Indices — exact names to verify against CF Benchmarks' site before coding the feed.
- **Payout criterion: threshold binary.** The Payout Criterion is that the index is `<above | below | between | exactly | at least> <price>` on `<date>` at `<time>`. Kalshi can list multiple strike levels per window. "Up / Down" is a colloquial simplification — implementation must read each live market's strike and comparator, not hard-code an Up/Down pair.
- **Resolution-data tail risk.** Contract terms state verbatim: *"If no data is available or incomplete on the Expiration Date at the Expiration Time, then affected strikes resolve to No."* This is an asymmetric risk for YES holders and an asymmetric windfall for NO holders. Must be modeled as a non-zero probability, especially for long-tail strikes and during CF Benchmarks outages.
- **Market Outcome Review Process** exists (Rulebook §6.3(d), §7.1, §7.2). Kalshi can review/delay settlement or move expiration earlier if the Payout Criterion triggers.
- **Settlement Value:** `$1.00` per contract (standard Kalshi).
- **Minimum Tick:** `$0.001` (0.1¢) — finer-grained than the 1¢ grid on Polymarket.
- **Position Accountability Level:** `$25,000 per strike, per Member` — soft cap that constrains single-strike scaling.
- **Settlement Date:** no later than the day after Expiration Date (barring review).

**Implications this plan has to internalize:**

1. `FairValueModel` must compute `P(60s-avg at T_close compared_to strike)` where `compared_to ∈ {above, below, between, exactly, at_least}` — not just `P(up)`.
2. Strike levels are dynamic: always pull live strikes from Kalshi's `/events` / `/markets` endpoints, don't assume one pair per window.
3. Add a `NoDataResolveNoRule` to the risk policy (§4.6) that down-weights or rejects YES entries during CF Benchmarks feed anomalies.
4. Reference-feed source-of-truth should be CF Benchmarks RTIs (via licensed feed if acquired, or the published methodology approximated via the constituent exchanges the CF index uses — see §4.2). Tracking error is measured against the CF RTI, not any single spot exchange.
5. Position sizing can stay well below the $25K accountability cap — noted in §6 risk config (`max_position_notional_usd: 500`).

## 1. Goal

**Two-phase program**, each with its own deliverable. Phase 2 only begins if Phase 1's feasibility report clears pre-committed thresholds.

### Phase 1 — Scanner / Feasibility Research (zero money at risk)

Build a **research instrument**, not a trader. The deliverable is data + a report, not a P/L curve.

1. Tracks live Kalshi 15-min crypto **threshold** markets for BTC, ETH, SOL (series tickers assumed `KXBTC15M` / `KXETH15M` / `KXSOL15M`; verify against Kalshi `/events` at runtime). Captures the full book into a research DB.
2. Pulls Kalshi historical markets + trades + candles (`/historical/*` endpoints) for the prior 30+ days into the same DB.
3. Captures a continuous stream of our basket-of-exchanges reference-price proxy for BTC / ETH / SOL, stored tick-by-tick.
4. Runs a **live shadow evaluator**: for each market update, runs the fair-value model and records the hypothetical decision + realized outcome to the DB. **No orders are submitted. No `Executor` is wired in.**
5. Produces the feasibility report: measured lag distribution (CF-RTI → Kalshi book), realized-if-traded edge, hit rate, capacity estimate, tail-risk incidence (especially "no-data resolves No").
6. Delivers an explicit Phase-2 go/no-go recommendation against pre-committed thresholds.

### Phase 2 — Execution (real money, tiny → scaled)

Only if Phase 1 says Go.

1. Adds the risk-rule engine (9 rules) and the Kalshi paper executor.
2. Adds the Kalshi live executor, gated by three explicit opt-ins.
3. Adds a custom dashboard with live-account API calls (balance, positions, open orders, fills).
4. Runs paper-in-prod for 4 weeks; confirms realized paper edge matches Phase-1 expectation.
5. Deploys live at very small size ($100 max notional, $100 daily-loss stop) for 2 weeks; daily manual reconciliation.
6. Steps up size only when each step's realized edge matches the prior step within tolerance.

Both phases deploy to AWS us-east-1. **Code lives in this repo** (`src/`, `tests/`, `scripts/`, `config/`, `deploy/`) — the project is self-contained with no sibling-repo dependency. Platform primitives (CircuitBreaker, RetryPolicy, BasePipeline, PriorityQueue) are TBD — see "Open architectural decisions" in `CLAUDE.md`. See [`kalshi_scanner_execution_plan.md`](./kalshi_scanner_execution_plan.md) for architecture and module layout, and [`kalshi_scanner_implementation_tasks.md`](./kalshi_scanner_implementation_tasks.md) for the per-task runbook.

## 2. Edge thesis

Three candidate sub-strategies, ranked by realism:

| Strategy | Window | Expected edge per trade | Win rate expectation | Capacity | Verdict |
|---|---|---|---|---|---|
| **A. Near-expiry structural** | Last 30–60s | 2–5¢ after fees | 75–90% | $200–$1,500 notional | **Primary target** |
| **B. Feed-lag scalping** | Any time the CF RTI updates | <2¢ before fees | 55–65% | Low, many attempts | Secondary; latency-constrained |
| **C. Implied-vol mispricing** | Mid-window | 1–3¢ after fees | 55–65% | $500–$2,000 notional | Speculative; MMs dominate |

Implementation focus: Strategy A first. B and C gated on measured edge from A.

## 3. Architecture — modules in this repo

Code lives in this repo. The scanner is built around protocol-shaped modules that compose into a 6-stage pipeline (detect → price → risk → simulate → submit → verify). Phase 1 stops at simulate (shadow evaluator, no orders); Phase 2 wires submit → verify.

Platform primitives (`CircuitBreaker`, `RetryPolicy`, `BasePipeline`, `PriorityQueue`) are **TBD** — see "Open architectural decisions" in `CLAUDE.md`. Tentative default: add the generic `trading_platform` repo as a submodule at `lib/trading_platform/` and wire through a local `src/platform_adapters.py`.

### Modules to create

```
src/
  market/
    kalshi_market.py           # KalshiMarketSource(MarketSource)
    crypto_reference.py        # CryptoReferenceSource (BRTI proxy)
    brti_licensed.py           # Optional: real BRTI if licensing acquired
  strategy/
    kalshi_fair_value.py       # KalshiFairValueStrategy + FairValueModel
  execution/
    kalshi_executor.py         # KalshiPaperExecutor + KalshiLiveExecutor
  risk/
    kalshi_rules.py            # Kalshi-specific RiskRules
  run_kalshi_fair_value.py     # Production entrypoint
config/
  kalshi_fair_value_config.json
tests/
  test_kalshi_market.py
  test_crypto_reference.py
  test_fair_value_model.py
  test_kalshi_strategy.py
  test_kalshi_executor_paper.py
```

### Wiring into existing lifecycle

- Reuse `CandidatePipeline` with stages: `detect` (scanner) → `price` (fair-value) → `risk` (rules) → `simulate` (paper) → `submit` (live executor) → `verify` (resolution check after window closes).
- Reuse `CircuitBreaker` from `lib/trading_platform` — replace the "revert tracking" interpretation with "Kalshi API error / stale-feed / stale-book" tracking.
- Reuse `MetricsCollector` and `AlertDispatcher` as-is. Metrics namespace: `kalshi.*`.
- Reuse the FastAPI dashboard — add a Kalshi view card.

## 4. Components

### 4.1 `KalshiMarketSource`

Responsibilities:

- Authenticate to Kalshi API (email+password or API key per current Kalshi auth; verify before coding).
- Subscribe to Kalshi websocket for orderbook deltas on each active 15-min Up/Down market for BTC/ETH/SOL.
- Maintain in-memory L1 and L2 orderbook state per market.
- Emit `MarketQuote` records compatible with existing platform typing — adapt the existing `MarketQuote` dataclass, or subclass as `PredictionMarketQuote(MarketQuote)`.
- Track market lifecycle: `opening`, `open`, `final_minute`, `closed`, `settled`.

Key files to reference:
- `src/core/models.py` — existing `MarketQuote` shape.
- `src/market/onchain_market.py` — reference impl for a `MarketSource`.
- `src/platform_adapters.py` — naming conventions.

Needs-to-verify before coding:
- Kalshi websocket URL, auth scheme, subscription message format.
- Orderbook message schema.
- Rate limits on market-data and trading APIs.

### 4.2 `CryptoReferenceSource`

Provides the "fair" BTC/ETH/SOL price feed that fair-value is computed against. The authoritative reference for each asset is its CF Benchmarks Real-Time Index (BTC: BRTI; ETH and SOL: the corresponding CF RTIs — verify exact names on `cfbenchmarks.com` before coding). Two implementations, chosen via config:

**`BasketReferenceSource` (default, free):**
- Subscribes to websocket trade feeds on the current CF RTI constituent exchanges for each asset (verify the constituent list per asset on CF Benchmarks' methodology pages — constituents differ across BTC / ETH / SOL and change over time).
- Computes a per-second proxy following CF Benchmarks' published methodology as closely as is public (trade-by-trade volume-weighted with outlier rejection per their spec; re-read the methodology doc before implementation).
- Maintains a 60-second rolling tick buffer per asset to compute the running average that Kalshi settles against.
- Records tracking error vs published CF RTI (once we have an archive) for calibration.

**`LicensedCFBenchmarksSource` (optional, paid):**
- Direct CF Benchmarks Real-Time Index websocket once licensing is acquired (one license likely covers BTC/ETH/SOL RTIs; confirm with CF Benchmarks sales).
- Gated behind config flag. Only enabled if the basket proxy is measurably insufficient.

Shared interface:

```python
class CryptoReferenceSource(Protocol):
    def get_current_price(self, asset: str) -> Decimal: ...
    def get_rolling_60s_average(self, asset: str, end_ts: float) -> Decimal: ...
    def get_tick_buffer(self, asset: str) -> Sequence[PricePoint]: ...
    def is_healthy(self) -> bool: ...
```

### 4.3 `FairValueModel`

Computes `P(CF-RTI 60s-avg at T_close compared_to strike | observed so far)` for each active market, where `compared_to` is the market's own comparator (`above | below | between | exactly | at_least`, per contract terms §0.5). Do not assume a single Up/Down comparator.

Inputs:
- Market `strike` and `comparator` (from Kalshi market metadata, per window).
- Observed CF-RTI tick buffer so far (per asset).
- `time_remaining` to window close.
- Realized volatility estimate (trailing N-minute).

Method:
- For `time_remaining > 60s`: project forward using a Brownian motion with drift-zero assumption, compute the distribution of the future 60-sec-avg given current state, integrate to get probability under the relevant comparator.
- For `time_remaining ≤ 60s`: partial observation of the averaging window collapses uncertainty quickly. Compute the conditional distribution of the remaining averaged ticks and integrate under the comparator.
- Apply a small downward adjustment to `p_yes` to account for the "no data → resolves No" tail risk (see §0.5) — calibrate the magnitude from observed CF Benchmarks uptime. YES entries eat this haircut; NO entries benefit from it.
- Returns both `p_yes` and a confidence interval. Tight CI = high-conviction setup.

Validate against: historical Kalshi market resolutions replayed with recorded CF-RTI ticks. Out-of-sample Brier score must beat naive "p_yes = implied_mid_at_entry" baseline.

### 4.4 `KalshiFairValueStrategy`

Glue layer matching the platform's `Strategy` protocol.

Per tick / per orderbook update:
1. For each active market, ask `FairValueModel` for `p_yes`.
2. Read current book `best_yes_ask` and `best_no_ask`.
3. Compute edge:
   - If `p_yes - best_yes_ask > min_edge_threshold` → candidate BUY YES.
   - If `(1 - p_yes) - best_no_ask > min_edge_threshold` → candidate BUY NO.
4. Apply pre-risk filters (time-in-window, CI width, market liquidity).
5. Emit `Opportunity` to the pipeline.

Config-driven thresholds: `min_edge_bps`, `min_time_in_final_window_s`, `max_time_in_final_window_s`, `max_ci_width`, `min_book_depth_usd`.

### 4.5 `KalshiExecutor`

Two impls:

**`KalshiPaperExecutor`** (default, reuses `PaperExecutor` pattern):
- Records every decision, the book snapshot at decision time, and the subsequent fill that *would* have occurred.
- Tracks simulated P/L after fees based on observed settlement outcome.
- Required for the paper-trading phase (§8).

**`KalshiLiveExecutor`** (gated):
- Submits authenticated orders via Kalshi trading API.
- Handles partial fills, order-reject retries via `RetryPolicy`, cancel-on-timeout, position reconciliation.
- Emits `ExecutionResult` compatible with the existing lifecycle.

Decimal math throughout, per platform convention.

### 4.6 `KalshiRiskPolicy`

Rule engine composed from platform `RiskPolicy` base:

- `MinEdgeAfterFeesRule` — reject if `edge < fee_roundtrip + safety_margin`.
- `TimeWindowRule` — only trade within configured `time_in_final_window_s` range.
- `CIWidthRule` — reject if model CI is too wide.
- `OpenPositionsRule` — cap number of concurrent trades.
- `DailyLossRule` — circuit-break after daily loss threshold.
- `ReferenceFeedStaleRule` — reject if crypto reference feed has stale ticks.
- `BookDepthRule` — reject if top-of-book depth is below size.
- `NoDataResolveNoRule` — per contract terms §0.5, missing / incomplete CF Benchmarks data at expiration causes affected strikes to resolve No. Reject new YES entries when CF Benchmarks status indicators signal degraded service, and lean NO-side otherwise. Size limit for YES-direction trades must already include this haircut in the model (§4.3); this rule is the belt-and-suspenders kill switch.
- `PositionAccountabilityRule` — cap per-strike open notional well below the $25,000 Kalshi Position Accountability Level to avoid any Member-level attention.

## 5. Data pipeline

```
[Coinbase WS, Kraken WS, Bitstamp WS, Gemini WS, LMAX WS]
        │
        └─▶ CryptoReferenceSource  ──▶ 60s rolling buffer ──▶ FairValueModel
                                                                     │
[Kalshi WS] ──▶ KalshiMarketSource  ──▶ Orderbook state ─────────────┤
                                                                     ▼
                                                    KalshiFairValueStrategy
                                                                     │
                                                                     ▼
                                          Opportunity ─▶ CandidatePipeline
                                                                     │
                                                    ┌────────────────┼────────────────┐
                                                    ▼                ▼                ▼
                                              RiskPolicy        PaperExec     (gated) LiveExec
                                                                     │                │
                                                                     ▼                ▼
                                                              MetricsCollector + AlertDispatcher
```

Latency targets (soft):
- Reference tick → `FairValueModel` update: < 5 ms
- Orderbook delta → strategy decision: < 5 ms
- Decision → order submitted: < 50 ms end-to-end on AWS us-east-1

Clock discipline:
- NTP sync the host (chrony on Ubuntu).
- All timestamps UTC, microsecond precision, logged.
- Kalshi windows are ET — convert once at market registration, store UTC.

## 6. Configuration

`config/kalshi_fair_value_config.json`:

```json
{
  "mode": "paper",
  "assets": ["BTC", "ETH", "SOL"],
  "kalshi": {
    "environment": "prod",
    "api_base": "https://trading-api.kalshi.com/trade-api/v2",
    "ws_url": "wss://trading-api.kalshi.com/trade-api/ws/v2",
    "series_filter": ["KXBTC15M", "KXETH15M", "KXSOL15M"]
  },
  "reference": {
    "source": "basket",
    "exchanges": ["coinbase", "kraken", "bitstamp", "gemini", "lmax"],
    "sanity_max_spread_bps": 25
  },
  "strategy": {
    "min_edge_bps": 300,
    "min_time_in_final_window_s": 5,
    "max_time_in_final_window_s": 60,
    "max_ci_width": 0.15,
    "min_book_depth_usd": 200
  },
  "risk": {
    "max_concurrent_positions": 3,
    "max_position_notional_usd": 500,
    "daily_loss_stop_usd": 250,
    "max_daily_trades": 50
  },
  "alerting": {
    "telegram_on_trade": true,
    "daily_gmail_summary": true
  }
}
```

Secrets in `.env`:

```
KALSHI_EMAIL=...
KALSHI_PASSWORD=...
KALSHI_API_KEY=...
KALSHI_API_SECRET=...
```

## 7. Phases and decision gates

The two-phase structure (§1) broken down with gates. Per-task runbook with status markers lives in [`kalshi_scanner_implementation_tasks.md`](./kalshi_scanner_implementation_tasks.md).

### Phase 1 — Scanner / Feasibility Research (6–8 weeks total)

**P1-M0. Repo prep** (0.5 day). Env + DB migration + dependency install.

**P1-M1. Live data collection** (3–5 days). `KalshiMarketSource` (WS + REST, read-only, writing book snapshots to DB) + `CryptoReferenceSource` (basket proxy writing per-tick to DB).

**P1-M2. Historical data collection** (2–3 days). `scripts/kalshi_historical_pull.py` pulls `/historical/markets`, `/historical/trades`, 1-min candles for the prior 30+ days. Continuous basket-proxy capture daemon started in parallel so historical and live coverage align.

**P1-M3. Fair-value model + backtest** (4–6 days). `FairValueModel` with the two regimes (time_remaining > 60s vs ≤ 60s); no-data haircut; `KalshiFairValueStrategy`. Backtest on DB-stored Phase-1 data. Target: Brier beats naive baseline on ≥ 500 windows per asset; calibration within 3pp per decile.

**P1-M4. Live shadow evaluator** (2–3 days). `KalshiShadowEvaluator` runs in prod continuously; writes every decision + realized outcome to `shadow_decisions`. **No `Executor` wired; no orders possible.** 4-week minimum run during P1-M5 analysis phase.

**P1-M5. Feasibility analysis + report** (1 week). Lag distribution, edge distribution, capacity estimate, tracking-error report, and the **feasibility report** — `docs/kalshi_phase1_feasibility_report.md`. Contains the Phase-2 go/no-go recommendation.

**Phase 1 → Phase 2 Gate.** Pre-committed thresholds for Go: post-fee realized edge > 1.5¢ per hypothetical trade across ≥ 500 decisions; Strategy-A win rate > 75%; p95 lag that our latency floor can actually exploit; capacity > meaningful daily $-capture at $500 max notional; no-data-resolves-No incidence within model assumptions. If not met → freeze; shadow evaluator keeps running for ongoing observation.

### Phase 2 — Execution (8 weeks total)

Only if P1-Gate says Go.

**P2-M1. Risk rules + paper executor** (3–4 days). All 9 rules; `KalshiPaperExecutor`.

**P2-M2. Live executor** (1–2 days). `KalshiLiveExecutor` with three-opt-in gate.

**P2-M3. Pipeline wiring + custom dashboard** (3–4 days). `run_kalshi_event_driven.py`; 5 dashboard routes with live account API calls (`/kalshi`, `/kalshi/portfolio`, `/kalshi/decisions`, `/kalshi/performance`, `/kalshi/health`).

**P2-M4. Paper in prod** (4 weeks). Deploy stack. Realized paper edge must match P1 expectation within tolerance.

**P2-M5. Live, small size** (2 weeks). `max_position_notional_usd = 100`, `daily_loss_stop_usd = 100`, 1 concurrent position. Daily human reconciliation vs `GET /portfolio/fills`.

**P2-M6. Scale.** Stepped increases ($250 → $500 → further), re-measuring edge at each step. Abort rule: edge drop > 30% vs prior step.

## 8. AWS deployment

Self-contained deploy stack in this repo. If P1-M0's "repo shell bootstrap" decision is "selective restore from git history", a lot of these building blocks come in as-is and get adapted; otherwise build fresh.

- **Runtime:** Docker image built via `Dockerfile`; push to ECR repo `kalshi-scanner`.
- **Host:** t3.small spot EC2 in us-east-1 is plenty for P1 (shadow evaluator is CPU-light). If latency measurements show > 10 ms from EC2 to Kalshi API gateway, upgrade to t3.medium (more consistent network) or switch to Fargate with enhanced networking — measure before spending.
- **CloudFormation:** `deploy/cloudformation.yml` — spot EC2, security group, IAM role for ECR + CloudWatch.
- **Secrets:** Kalshi private key + API Key ID in **AWS Secrets Manager**. Inject at container start via IAM role. Never static `.env` in prod.
- **Observability:** CloudWatch logs + our own `MetricsCollector` (TBD on primitives). Telegram / Discord / Gmail alerts via an `AlertDispatcher` (again TBD).
- **Healthcheck:** `/health/kalshi` — 200 if Kalshi WS connected, reference feed fresh (< 3 s), DB reachable.
- **Deploy script:** `scripts/deploy_prod.sh` (or `deploy.sh`) — `--deploy`, `--status`, `--logs`, `--rollback`.

Latency notes:
- Kalshi hosts on AWS us-east-1 per public info — colocation sanity-check with a `ping` + `traceroute` before architecting.
- Reference exchange WSs: Coinbase (us-east), Kraken (us-east), Bitstamp (EU), Gemini (us-east), LMAX (LDN/NY). Bitstamp and LMAX latency will be higher; treat their ticks as confirmation rather than primary.

## 9. Testing

Per the existing `tests/` pattern:

- **Unit tests** for: BasketReferenceSource aggregation logic, FairValueModel math, edge-computation logic, each RiskRule.
- **Integration tests** with mocked Kalshi WS and mocked reference feeds — replay recorded fixtures, assert expected trade decisions.
- **Replay backtest** entrypoint (`src/run_kalshi_backtest.py`) — consume recorded Phase 1 data, produce performance report.
- **Paper smoke test** for CI: `python -m run_kalshi_fair_value --paper --iterations 3 --no-sleep` must exit cleanly.
- Keep test count trending up; follow the pre-commit convention in this repo's `CLAUDE.md` / execution plan §7.

## 10. Open questions and prerequisites

- **Kalshi API auth shape in 2026.** Confirm email+password vs API key vs Ed25519 — docs may have moved. Do before Phase 1.
- **Exact series tickers.** `KXBTC15M` / `KXETH15M` / `KXSOL15M` are placeholders derived from Kalshi's KX prefix convention; confirm by hitting the live `/events` endpoint and filtering by crypto 15-min windows. Do before Phase 1.
- **CF Benchmarks Real-Time Index names for ETH and SOL.** The contract terms name BRTI for BTC by example; ETH and SOL have their own CF RTIs. Pull exact names from `cfbenchmarks.com`. Do before Phase 1.
- **CF RTI constituent exchange lists per asset.** Each CF RTI has a distinct constituent set that changes over time. Pull the current methodology for each asset before coding `BasketReferenceSource`.
- **Fee schedule.** Not in the contract terms PDF. Fetch from the Kalshi API at runtime and read dynamically — do not hardcode.
- **CF Benchmarks uptime / outage history.** Need a base rate to calibrate the `NoDataResolveNoRule` haircut. Check CF Benchmarks status page archives or ask sales for an SLA doc.
- **KYC and funding.** User must have a funded Kalshi account before Phase 3 paper trade (paper can predate funding, but live gate requires it).
- **US jurisdiction.** User must verify non-blocklisted state residency and legal eligibility.
- **CF RTI licensing.** Budget and timing for potential Phase 3 upgrade from basket proxy to licensed CF RTI feed.
- **Tax / reporting.** Every live trade is a reportable event. Out of scope for this doc; flag to user before Phase 4.

## 11. Critical files to create/modify

All in this repo:

- `src/market/kalshi_market.py` (new) — `KalshiMarketSource`, WS + REST.
- `src/market/crypto_reference.py` (new) — basket / licensed CF Benchmarks feed.
- `src/strategy/kalshi_fair_value.py` (new) — `KalshiFairValueStrategy` + `FairValueModel`.
- `src/execution/kalshi_shadow_evaluator.py` (new; Phase 1 only) — records shadow decisions, no orders.
- `src/execution/kalshi_executor.py` (new; Phase 2) — `KalshiPaperExecutor` + `KalshiLiveExecutor`.
- `src/risk/kalshi_rules.py` (new; Phase 2) — 9 rules.
- `src/run_kalshi_shadow.py` (new; Phase 1) — shadow-evaluator entry point.
- `src/run_kalshi_backtest.py` (new; Phase 1) — replay entry point.
- `src/run_kalshi_event_driven.py` (new; Phase 2) — trading entry point.
- `config/kalshi_shadow_config.json` (P1), `config/kalshi_fair_value_config.json` (P2), `config/kalshi_fair_value_live.json` (P2).
- `tests/test_kalshi_*.py` (new, several files across P1 and P2).
- `scripts/kalshi_historical_pull.py`, `scripts/kalshi_track_reference.py` (P1).
- `scripts/migrate_db.py` (new or restored; adds P1 research tables).
- `deploy/cloudformation.yml` (new or restored; adapted for Kalshi scanner).
- `scripts/deploy_prod.sh` (new or restored; `--deploy`, `--status`, `--logs`, `--rollback`).
- `.env.example` — add `KALSHI_*` keys.
- `CLAUDE.md` — keep updated as the scanner evolves.

**In `/Users/tamir.wainstain/src/KalshiTrader` (research repo — this repo):**

- `docs/kalshi_crypto_fair_value_scanner_plan.md` — this doc.
- `docs/kalshi_fair_value_paper_trading_report.md` — populated at end of Phase 3.
- `docs/kalshi_crypto_fair_value_tracking_error_report.md` — populated at end of Phase 1.

## 12. Verification at each phase

- **Phase 1:** Unit tests green. `docs/kalshi_crypto_fair_value_tracking_error_report.md` published with basket-vs-BRTI error distribution.
- **Phase 2:** Backtest notebook with per-strategy Brier + realized-edge plots. Calibration plot (predicted p_yes vs realized outcome, decile bins).
- **Phase 3:** Paper-trading report shows ≥200 trades, post-fee edge > 1.5¢, Brier beats baseline. Human review of 20 sampled trades for sanity.
- **Phase 4:** Small-size live report reconciles against Kalshi's trade history endpoint. No unexplained losses.
- **Phase 5:** Capacity curve plotted — edge vs size — to identify the practical ceiling.

## Cross-links

- `prediction_market_arbitrage_video_breakdown_and_opinion.md` — cross-venue framing and failure modes (resolution-rule mismatch is the single biggest).
- `crypto_arbitrage_feasibility_research.md` — broader crypto-arb landscape and solo-operator recommendation.

Earlier cross-references in this repo (`polymarket_winning_strategies.md`, `polymarket_us_availability.md`, `polymarket_kalshi_15m_btc_convergence_measurement.md`, `polymarket_chainlink_lag_capture_plan.md`) were removed from the working tree during the 2026-04-19 repo cleanup. Content lives in git history only.

## Sources

- [Kalshi CRYPTO15M contract terms (PDF, doc-date 2026-03-25)](https://kalshi-public-docs.s3.amazonaws.com/contract_terms/CRYPTO15M.pdf) — **authoritative** source for resolution mechanics in §0.5.
- [Kalshi API documentation](https://docs.kalshi.com)
- [Kalshi Help Center — Crypto Markets](https://help.kalshi.com/en/articles/13823838-crypto-markets)
- [CF Benchmarks — Bitcoin Real-Time Index (BRTI)](https://www.cfbenchmarks.com/data/indices/BRTI)
- [Good Money Guide — Kalshi 15-minute crypto prediction markets](https://goodmoneyguide.com/usa/kalshi-takes-on-crypto-options-trading-with-launch-of-15-minute-crypto-prediction-markets/)
- [Defirate — How Kalshi and Polymarket settle event contracts](https://defirate.com/prediction-markets/how-contracts-settle/)
