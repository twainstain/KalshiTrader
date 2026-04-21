# Kalshi Crypto Fair-Value Scanner — Execution Plan

**Research date:** 2026-04-19
**Strategy / thesis doc:** [`kalshi_crypto_fair_value_scanner_plan.md`](./kalshi_crypto_fair_value_scanner_plan.md) — read first.
**Task runbook (status-tracked):** [`kalshi_scanner_implementation_tasks.md`](./kalshi_scanner_implementation_tasks.md).

> Not investment, legal, or tax advice.

## 0. Purpose and scope

This doc is the **implementation playbook**. It translates strategy into concrete file paths, commands, and acceptance tests. The strategy plan owns the *why*; this doc owns the *how*.

**Two-phase structure:**

- **Phase 1 — Scanner / Feasibility Research (zero money at risk).** Historical + live Kalshi data capture, basket-of-exchanges reference ticks, **shadow evaluator** that records hypothetical decisions without submitting orders. Deliverable: `docs/kalshi_phase1_feasibility_report.md` with explicit Phase-2 go/no-go.
- **Phase 1 → Phase 2 Gate.** Pre-committed thresholds; no Phase-2 work starts without clearing them.
- **Phase 2 — Execution.** Risk rules, paper executor, live executor (three-opt-in gated), custom dashboard with live-account API calls, paper-in-prod for 4 weeks, live small-size for 2 weeks, stepped scale.

**Everything — docs, code, tests, configs, deploy scripts — lives in this repo** (`/Users/tamir.wainstain/src/KalshiTrader/`). No sibling-repo dependency.

### Open architectural decisions (resolve before P1-M0 edits in `src/`)

See `CLAUDE.md` → "Open architectural decisions". Two open calls that shape this plan's concrete commands:

1. **Platform primitives.** Tentative default: add generic `trading_platform` as a submodule at `lib/trading_platform/` and wire through a local `src/platform_adapters.py`.
2. **Repo shell bootstrap.** Tentative default: selectively restore scaffolding (`pyproject.toml`, `docker-compose.yml`, `scripts/migrate_db.py`, Postgres-side persistence, pytest harness) from git history — dropping DEX-specific modules (`contracts/`, `onchain_market.py`, etc.) — rather than starting from a blank layout.

Commands and paths below assume both defaults. Re-scan this doc if either flips.

## 1. Ground rules — MUST follow

Violating these is a reject.

- **Decimal, never float.** Every financial field on every model passes `Decimal` or a string literal. If auto-coercion is used (from the restored `core/models.py`), it's a migration aid only — new code passes `Decimal` directly.
- **`fee_included` is `False` on Kalshi quotes.** Kalshi book quotes are pre-fee; fees are charged on top at fill. Every `MarketQuote` for Kalshi must set `fee_included=False` and populate `fee_bps` from the live Kalshi fee schedule (dynamic — fetch at runtime via `GET /exchange/series-fee-changes`).
- **Paper is default. Three explicit opt-ins for live.** (a) `--execute` CLI flag, (b) `KALSHI_API_KEY_ID` populated and PEM path resolves to a valid key, (c) config `mode: "live"` + `dry_run: false`. Any two alone = paper with a warning.
- **`SUPPORTED_VENUES` is the Kalshi-discriminator, not `SUPPORTED_CHAINS`.** If the persistence layer inherits a `SUPPORTED_CHAINS` constant, don't add `"kalshi"` to it — leak risk into chain-only callers.
- **No direct `trading_platform.*` imports in `src/domain code/`.** If we adopt the submodule default, route every access through `src/platform_adapters.py`. Add adapters for new primitives; don't import the submodule directly.
- **Pre-commit validation ritual is non-negotiable.** See §7.
- **No `docker compose down -v` on the postgres service.** Destroys `pg-data`. See §2.4.

## 2. Domain-model + persistence plan

Build (or restore-and-adapt) the `MarketQuote → Opportunity → ExecutionResult` pipeline in `src/core/models.py`. Shape documented below.

### 2.1 `MarketQuote`

Single dataclass covering Kalshi book snapshots. Fields:

- `venue: str` — always `"kalshi"` for this project.
- `market_ticker: str` — Kalshi market ticker verbatim (e.g. `KXBTC15M-26APR19-10:15-ABOVE-65000`).
- `series_ticker: str` — parent series (`KXBTC15M` / `KXETH15M` / `KXSOL15M`). Verify via live `/series` before assuming these.
- `event_ticker: str` — parent event from Kalshi metadata.
- `best_yes_ask: Decimal`, `best_no_ask: Decimal` — derived: `ask = 1 − opposite_best_bid`.
- `best_yes_bid: Decimal`, `best_no_bid: Decimal`.
- `book_depth_yes_usd: Decimal`, `book_depth_no_usd: Decimal` — computed from L2 state at configurable depth.
- `fee_bps: Decimal`, `fee_included: bool = False`.
- `expiration_ts: Decimal` — Unix epoch seconds (UTC).
- `strike: Decimal` — contract strike price.
- `comparator: str` — `above | below | between | exactly | at_least`.
- `reference_price: Decimal` — current CF RTI spot at quote time.
- `reference_60s_avg: Decimal` — rolling 60 s avg of the reference, pre-close.
- `time_remaining_s: Decimal` — seconds to expiration at quote time.
- `quote_timestamp_us: int` — microseconds since epoch (non-financial → int, not Decimal).
- `warning_flags: tuple[str, ...]` — e.g. `("stale_book",)`, `("cf_reference_degraded",)`.
- `raw: dict` — raw Kalshi book snapshot + metadata for debugging / replay.

### 2.2 `Opportunity`

Populated by `KalshiFairValueStrategy`. Fields (skeleton — extend as we build):

- `quote: MarketQuote`
- `p_yes: Decimal`, `ci_width: Decimal`
- `recommended_side: str` — `yes | no | none`
- `hypothetical_fill_price: Decimal`, `hypothetical_size_contracts: Decimal`
- `expected_edge_bps_after_fees: Decimal`
- `status: OpportunityStatus`
- `no_data_haircut_bps: Decimal` — applied haircut for the CF-Benchmarks outage tail.

### 2.3 `ExecutionResult` + `OpportunityStatus`

Restore the FSM pattern from git history: `detected → priced → approved → simulation_approved → simulated → submitted → included / reverted / not_included`. In Phase 1 we only use up to `simulation_approved` / `simulated` (paper path). Phase 2 adds `submitted → included / reverted / not_included`.

`ExecutionResult.reason` distinguishes "no-data resolves No" losses from other failures.

### 2.4 Persistence

**Stack:** SQLite at `data/kalshi.db` (local dev default) OR Postgres via `DATABASE_URL=postgresql://...` in prod. Postgres self-hosted via `docker-compose.yml` in this repo. Schema managed by `scripts/migrate_db.py`.

**Phase 1 research tables** (no `opportunities` table touched yet):

| Table | Purpose |
|---|---|
| `kalshi_historical_markets` | `/historical/markets` pull: metadata + settlement result per crypto-15M window. |
| `kalshi_historical_trades` | `/historical/trades` pull: every fill with ts / price / qty / taker side. |
| `kalshi_live_book_snapshots` | `KalshiMarketSource` live captures: yes_bids, no_bids, seq — for replay / lag analysis. |
| `reference_ticks` | `BasketReferenceSource` per-tick captures: asset, ts_us, price, src. |
| `shadow_decisions` | `KalshiShadowEvaluator` rows: inputs, hypothetical fill, expected edge, realized outcome, realized-if-traded P/L. |

**Phase 2:** add an `opportunities` table (restored / built) with Kalshi-specific columns: `expiration_ts`, `strike`, `comparator`, `reference_price`, `reference_60s_avg`, `venue_type`.

**Safety rule:** **Never `docker compose down -v` on the postgres service.** It destroys `pg-data` (all history). Use `docker compose down` (no `-v`) for routine restarts.

## 3. Platform primitives wiring

Per the open decision, tentative default is `trading_platform` submodule at `lib/trading_platform/` with a local `src/platform_adapters.py` for domain naming.

Minimum adapters for Phase 1:

- `CircuitBreaker` + `CircuitBreakerConfig` — Kalshi-flavored config (e.g. `max_api_errors`, `api_error_window_seconds`, `max_stale_book_seconds`).
- `RetryPolicy` — re-export direct.
- `PriorityQueue` / `CandidateQueue` — wrap with Kalshi-appropriate priority (time-to-expiry, edge, volatility).
- `BasePipeline` subclass hook — for DB-batched stages 1–3, async stages 4–6.

Add **`KalshiAPIError`** as a local exception in `src/platform_adapters.py`.

If we go with the "standalone" option instead of submodule, build these modules under `src/lib/` with the same external API. Code that consumes them is identical either way.

## 4. Directory layout

Layout assumes "selective restore" bootstrap + `trading_platform` submodule. Adjust if the open decision goes another way.

```
KalshiTrader/
  CLAUDE.md                                # repo-wide guidance
  README.md                                # rewrite post-Phase-1
  pyproject.toml                           # pytest + runtime deps
  docker-compose.yml                       # postgres service for prod parity
  Dockerfile                               # container image
  .env.example                             # runtime env vars (Kalshi keys, DB URL, alerts)
  lib/
    trading_platform/                      # submodule (if submodule option chosen)
  docs/                                    # strategy / execution / tasks / reports
  src/
    core/
      models.py                            # MarketQuote, Opportunity, ExecutionResult, OpportunityStatus
    market/
      kalshi_market.py                     # [P1] KalshiMarketSource — WS + REST, read-only in P1
      crypto_reference.py                  # [P1] BasketReferenceSource + LicensedCFBenchmarksSource
    strategy/
      kalshi_fair_value.py                 # [P1] KalshiFairValueStrategy + FairValueModel
    execution/
      kalshi_shadow_evaluator.py           # [P1] records decisions; NEVER submits orders
      kalshi_executor.py                   # [P2] KalshiPaperExecutor + KalshiLiveExecutor (three-opt-in)
    risk/
      kalshi_rules.py                      # [P2] 9 rules
    pipeline/
      lifecycle.py                         # BasePipeline subclass (P2)
    persistence/
      db.py                                # SQLite / Postgres connection adapter
      repository.py                        # typed accessors for all tables
    dashboards/
      kalshi.py                            # [P2] FastAPI routes under /kalshi/*
    alerting/
      dispatcher.py                        # AlertDispatcher + build_dispatcher_from_env + URL helpers
      telegram.py                          # TelegramAlert backend (Bot API)
      discord.py                           # DiscordAlert backend (webhook, allowlisted events)
      gmail.py                             # GmailAlert backend (SMTP)
    platform_adapters.py                   # CircuitBreaker / RetryPolicy / Queue adapters + KalshiAPIError
    env.py                                 # env-var loader
    run_kalshi_shadow.py                   # [P1] shadow-evaluator prod entrypoint
    run_kalshi_backtest.py                 # [P1] replay entrypoint
    run_kalshi_event_driven.py             # [P2] trading entrypoint
  config/
    kalshi_shadow_config.json              # [P1] no executor wired
    kalshi_fair_value_config.json          # [P2] paper-default
    kalshi_fair_value_live.json            # [P2] live mode; mode-gated
  scripts/
    migrate_db.py                          # schema migrations
    kalshi_historical_pull.py              # [P1] historical REST pull
    kalshi_track_reference.py              # [P1] continuous reference-tick daemon
    deploy_prod.sh                         # [P2] deploy / status / logs / rollback
    run_local.sh                           # local-dev convenience
  tests/
    test_kalshi_market.py                  # P1
    test_crypto_reference.py               # P1
    test_fair_value_model.py               # P1
    test_kalshi_strategy.py                # P1
    test_kalshi_shadow_evaluator.py        # P1
    test_kalshi_rules.py                   # P2
    test_kalshi_executor_paper.py          # P2
    test_kalshi_executor_live.py           # P2
    test_kalshi_pipeline.py                # P2
    test_alerting.py                       # dispatcher + Telegram/Discord/Gmail backends
  deploy/
    cloudformation.yml                     # [P2] spot EC2, security group, IAM, ECR
```

## 4a. Alerting (cross-phase)

Every paper / live fill, risk rejection, settlement, and top-level runner crash should fan out to any configured operator-notification backend. Modelled on the ArbitrageTrader `src/alerting/` package.

**Module:** `src/alerting/`.

- `dispatcher.py` — `AlertDispatcher.alert(event_type, message, details)` iterates registered backends and swallows per-backend errors (a broken Telegram endpoint must never take down the trading loop). Kalshi-specific convenience helpers: `paper_fill`, `live_fill`, `risk_reject`, `paper_settle`, `system_error`, `daily_summary`. Exports `build_dispatcher_from_env()` which attaches only configured backends, plus URL helpers (`kalshi_market_url`, `dashboard_market_url`).
- `telegram.py` — `TelegramAlert` via Bot API. Env: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
- `discord.py` — `DiscordAlert` via webhook. Env: `DISCORD_WEBHOOK_URL`. Applies a server-side allowlist (`live_fill`, `system_error`, `paper_settle`, `daily_summary`) so noisy shadow-mode events (`paper_fill`, `risk_reject`) stay off Discord without every call site having to route per-backend.
- `gmail.py` — `GmailAlert` via SMTP. Env: `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, `GMAIL_RECIPIENT`.

**Wiring.** `run_kalshi_shadow.main()` calls `build_dispatcher_from_env()` (unless `--disable-alerts`) and threads the dispatcher through `build_evaluator(...)` → `build_paper_executor_bridge(...)`. Fills / risk-rejects / settlements fan out alongside the `EventLogger.record()` calls that already emit to `logs/events_*.jsonl`. If `run_loop` raises, `main()` best-effort calls `dispatcher.system_error("run_kalshi_shadow", repr(exc))` before re-raising.

Alerts are **additive, fire-and-forget telemetry.** They do not gate trading, do not carry risk semantics, and must never raise into the caller.

**Env vars** — add to `.env.example`:

```
# Telegram (immediate notifications)
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
# Discord (low-noise: live fills, errors, daily summary only)
DISCORD_WEBHOOK_URL=
# Gmail (full fan-out, including paper fills and risk rejects)
GMAIL_ADDRESS=
GMAIL_APP_PASSWORD=
GMAIL_RECIPIENT=
```

Any subset may be left blank — unconfigured backends aren't attached, so `backend_count == 0` is a valid (silent) state.

**Tests.** `tests/test_alerting.py` covers the dispatcher (routing, failure isolation, helpers), each backend (configured / unconfigured / API error / network error), the env-driven factory, and the `build_paper_executor_bridge` wiring (fills fan out to the dispatcher; a blowing-up dispatcher does not crash the hook).

## 5. Milestones — sequenced execution

Each milestone ends with a verification command. Per-task status tracking is in [`kalshi_scanner_implementation_tasks.md`](./kalshi_scanner_implementation_tasks.md).

## Phase 1 — Scanner / Feasibility Research

### P1-M0 — Repo prep (1 day)

- Resolve the two open architectural decisions (CLAUDE.md).
- If "selective restore": `git checkout` specific files from the last pre-cleanup commit; drop DEX-specific modules immediately.
- If submodule chosen: `git submodule add <trading_platform URL> lib/trading_platform`.
- Fill in `pyproject.toml` with deps: `kalshi_python_sync`, `websockets`, `pandas`, `pyarrow`, `psycopg2-binary`, `fastapi` (for P2), `requests`, `python-dotenv`, plus `pytest`.
- Append Kalshi keys to `.env.example`.
- Add `SUPPORTED_VENUES = ("kalshi",)` alongside `SUPPORTED_CHAINS` in `src/core/models.py`.
- Add initial migration in `scripts/migrate_db.py` for the five P1 research tables (§2.4).

Verification:
```bash
python3.11 -m pytest tests/ -q
python3.11 -c "import kalshi_python_sync, websockets, pandas, pyarrow; print('ok')"
```

### P1-M1 — Live data collection (3–5 days)

`src/market/kalshi_market.py`, `src/market/crypto_reference.py`, tests.

- `KalshiMarketSource(MarketSource)` — REST auth via `kalshi_python_sync.KalshiClient`, WS subscription to `orderbook_delta`, in-memory L1+L2 book, `MarketQuote` emission with Kalshi-specific fields populated.
- `BasketReferenceSource` — per-asset CF Benchmarks constituent aggregation, writes every tick to `reference_ticks`.
- `LicensedCFBenchmarksSource` — stub, gated on `CF_BENCHMARKS_API_KEY`.

### P1-M2 — Historical data collection (2–3 days)

- `scripts/kalshi_historical_pull.py` — paginates `/historical/markets`, `/historical/trades`, `/markets/{ticker}/candlesticks` for past 30+ days of crypto 15M series. Writes to the P1 tables.
- `scripts/kalshi_track_reference.py` — daemon, runs as systemd unit, writes to `reference_ticks` continuously.

### P1-M3 — Fair-value model + backtest (4–6 days)

`src/strategy/kalshi_fair_value.py`, `src/run_kalshi_backtest.py`, tests.

- `FairValueModel.price(strike, comparator, observed_ticks, time_remaining_s) -> (p_yes, ci_width)` with two regimes (>60s Brownian projection; ≤60s conditional distribution) and a calibrated no-data haircut.
- `KalshiFairValueStrategy` feeds the shadow evaluator in P1 (no `Opportunity` → trading path).
- Backtest entrypoint replays DB-stored Phase-1 data; target Brier < naive baseline on ≥ 500 windows per asset.

### P1-M4 — Live shadow evaluator (2–3 days)

`src/execution/kalshi_shadow_evaluator.py`, `src/run_kalshi_shadow.py`, tests.

- `KalshiShadowEvaluator` consumes live quotes + reference + model; writes `shadow_decisions` rows. **No `Executor` protocol wired — orders are structurally impossible in Phase 1.**
- Post-window reconciler: at `expiration_ts + 30s`, fetch final result via public `/markets/{ticker}`, update the realized columns.

### P1-M5 — Feasibility analysis + report (1 week)

Notebooks / scripts; no new `src/` modules.

Deliverables in `docs/` (this repo): `kalshi_crypto_fair_value_tracking_error_report.md` + the primary **`kalshi_phase1_feasibility_report.md`** with pre-committed Phase-2 go/no-go thresholds.

### Phase 1 → Phase 2 Gate

Review the feasibility report; record Go / No-go with date and reasoning in the task tracker.

## Phase 2 — Execution

### P2-M1 — Risk rules + paper executor (3–4 days)

`src/risk/kalshi_rules.py` (9 rules), `src/execution/kalshi_executor.py` (`KalshiPaperExecutor`), tests.

### P2-M2 — Live executor (1–2 days)

`KalshiLiveExecutor` with **three-opt-in gate**. Uses `RetryPolicy` for 5xx/transient, cancel-on-timeout, post-fill position reconciliation against `GET /portfolio/positions`.

### P2-M3 — Pipeline wiring + custom dashboard (3–4 days)

`src/run_kalshi_event_driven.py`, `src/pipeline/lifecycle.py` extension, `src/dashboards/kalshi.py`, configs, tests.

Dashboard routes (the user-directed "custom dashboard with more API calls"):

- `/kalshi` — active windows, book depths, strike grid, feed health, risk-rule rejection counters.
- `/kalshi/portfolio` — live `GET /portfolio/balance`, `GET /portfolio/positions`, `GET /portfolio/orders?status=resting`, `GET /portfolio/fills?limit=50`. 5 s poll; mark stale > 15 s.
- `/kalshi/decisions` — recent `shadow_decisions` + recent `opportunities` with drill-in.
- `/kalshi/performance` — rolling P/L, daily-loss accumulator, rolling Brier, per-asset breakdown.
- `/kalshi/health` — WS state, last tick per asset, rate-limit headroom, circuit-breaker state.

### P2-M4 — Paper in prod (4 weeks)

Deploy stack (`deploy/cloudformation.yml`, `scripts/deploy_prod.sh`, ECR). Run paper-in-prod. Daily entry in `docs/kalshi_paper_trading_daily_log.md`. End-of-phase report with go/no-go to P2-M5.

### P2-M5 — Live, small size (2 weeks) — gated on P2-M4

Live config: `mode: "live"`, `max_position_notional_usd: 100`, `daily_loss_stop_usd: 100`, `max_concurrent_positions: 1`. Secrets Manager migration. Daily reconciliation vs `GET /portfolio/fills`.

### P2-M6 — Scale — gated on P2-M5

Stepped increases ($250 → $500 → …), re-measuring edge at each step. Abort if edge drops > 30% vs prior step.

## 6. Deploy details

Self-contained in this repo.

- **CloudFormation:** `deploy/cloudformation.yml` — spot EC2, security group, IAM role for ECR + CloudWatch.
- **ECR:** repo `kalshi-scanner`. `Dockerfile` base image pins Python 3.11.
- **Deploy script:** `scripts/deploy_prod.sh` — `--deploy`, `--status`, `--logs`, `--rollback`. Refuses `--live` unless config has `mode: "live"` AND Secrets Manager secret is present (belt-and-suspenders).
- **Secrets:** Kalshi API Key ID + PEM in AWS Secrets Manager. IAM role on EC2 grants secret-read. No static `.env` in prod.
- **Health endpoint:** `/health/kalshi` — 200 if WS connected, reference feed fresh, DB reachable.
- **Latency sanity check before go-live:** `ping` + `traceroute` from EC2 to Kalshi API gateway. If p95 > 10 ms, upgrade instance or move to Fargate.

## 7. Validate after code changes

Before committing anything that touches `src/` or `tests/`, run **all three**:

```bash
python3.11 -m pytest tests/ -q
python3.11 -m run_kalshi_shadow --iterations 3 --no-sleep             # P1 path
python3.11 -m run_kalshi_event_driven --paper --iterations 3 --no-sleep  # P2 path once wired
```

Tests alone aren't enough — the smoke runs catch import/config breakage that unit tests mock away. P1-era commits may skip the `run_kalshi_event_driven` run until P2-M3 lands; note that explicitly in the commit message when skipped.

### Pre-commit checklist (copy-paste)

```
[ ] No float in financial fields (grep: `float\)` in src/*/kalshi_*.py)
[ ] `fee_included=False` on every Kalshi MarketQuote
[ ] Three-opt-in live gate honored in KalshiLiveExecutor (Phase 2 only)
[ ] python3.11 -m pytest tests/ -q                                    PASSES
[ ] python3.11 -m run_kalshi_shadow --iterations 3 --no-sleep         PASSES (P1)
[ ] python3.11 -m run_kalshi_event_driven --paper --iterations 3      PASSES (P2+)
[ ] Test count in tests/ has increased, not decreased
[ ] New config file (if any) has `mode: "paper"` as default
```

## 8. Validate Kalshi deployment

`scripts/deploy_prod.sh` ships. Deploy is **not done** until all three pass:

```bash
./scripts/deploy_prod.sh --status    # /health/kalshi + Kalshi WS + CF reference + container state
./scripts/deploy_prod.sh --logs      # no repeating errors, active-window scans occurring each second
```

Then browser-check the Kalshi dashboard (P2-M3). Must see:

- Active 15-min windows with book updates dated within the last ~10 s.
- No circuit-breaker `OPEN` banner.
- CF Benchmarks reference feed staleness < 3 s per asset.
- Risk-rule rejection counts sane — no single rule firing on >95% of candidates.
- Daily-loss accumulator < `daily_loss_stop_usd` and not trending vertical.
- Recent paper fills (paper mode) or recent live fills with reconciled positions (live mode).

**Failure-mode heuristic:** a green deploy with no fresh scans in the last minute usually means the consumer thread crashed silently — check logs for unhandled exceptions in the pipeline worker, and for Kalshi WS disconnect loops.

## 9. Invariants

The "rules you can't forget". Full ground-rules list is §1; below is the quick-reference.

- **Decimal, never float.** All financial fields pass `Decimal` or string literals.
- **`fee_included=False` on Kalshi quotes.** Fees are pre-fill on Kalshi; inverting this makes every opportunity look ~60–200 bps better than reality.
- **Paper default. Three explicit opt-ins for live** (see §1). Never flip the paper default.
- **`SUPPORTED_CHAINS` and `SUPPORTED_VENUES` are separate** (P1-M0).
- **No `trading_platform.*` direct imports in domain code** — if we adopt the submodule, go through `src/platform_adapters.py`.
- **No `docker compose down -v` on the postgres service** — destroys `pg-data`.

## 10. Testing conventions

- Every new module gets a matching `tests/test_<module>.py`. Test counts should trend up, never down.
- `test_kalshi_pipeline.py` is the P2 integration test: mocked Kalshi WS + mocked reference feed; replay recorded session; assert expected `Opportunity` sequence and paper fills.
- `test_kalshi_shadow_evaluator.py` is the P1 integration test.
- Replay-backtest entrypoint (`run_kalshi_backtest`) doubles as a regression test when run against frozen fixtures.
- Pre-commit ritual is §7; do not duplicate the command list here.

## 11. Pitfalls — Kalshi-specific, easy to get wrong

1. **"Up/Down" framing.** Kalshi 15-min crypto markets are threshold binaries, not a single Up/Down pair. The scanner must enumerate every live strike per window.
2. **BRTI is BTC-only.** ETH and SOL use different CF Benchmarks indices. Don't reuse the BRTI constituent list for ETH or SOL.
3. **60-second averaging ends at `<time>`, not starts.** Window close time is the *end* of the averaging window.
4. **"No data → No" resolution risk.** Model the haircut and add `NoDataResolveNoRule` (P2). Do not treat CF Benchmarks as 100% uptime.
5. **Kalshi is CFTC-regulated.** KYC, tax, state eligibility differ from generic prediction-market assumptions. Verify eligibility before live trading.
6. **Position Accountability at $25k.** Cap well below this; exceeding it attracts regulatory attention.
7. **Clock drift.** Kalshi windows are ET; CF RTIs publish on their own cadence. NTP-sync the host. UTC microsecond timestamps everywhere. Never rely on `time.time()` to nearest second.
8. **Fees are dynamic.** Fetch from `GET /exchange/series-fee-changes` at runtime. Do not hardcode.
9. **Paper and live rows in the same table.** Filter by `mode` column in every P/L query (P2).
10. **Platform-primitives boundary.** If we go with the submodule option, no `from trading_platform.x import y` in `src/domain_code/` — go through `platform_adapters.py`.

## 12. Cross-links

- [`kalshi_crypto_fair_value_scanner_plan.md`](./kalshi_crypto_fair_value_scanner_plan.md) — strategy, edge thesis, resolution mechanics.
- [`kalshi_scanner_implementation_tasks.md`](./kalshi_scanner_implementation_tasks.md) — per-task runbook with status markers.
- `CLAUDE.md` (this repo) — top-level framing + open architectural decisions.

## 13. Sources

- [Kalshi CRYPTO15M contract terms (PDF)](https://kalshi-public-docs.s3.amazonaws.com/contract_terms/CRYPTO15M.pdf).
- [Kalshi API docs](https://docs.kalshi.com).
- [CF Benchmarks — indices methodology](https://www.cfbenchmarks.com/).
