# Multi-category Kalshi lag scanner + leaderboard research plan

**Research date:** 2026-04-20
**Status:** Draft — pending promotion to `docs/kalshi_multi_category_lag_research.md`
**Parent docs:** `docs/kalshi_crypto_fair_value_scanner_plan.md`, `docs/kalshi_scanner_execution_plan.md`, `docs/kalshi_scanner_implementation_tasks.md`
**Scope:** Research-only. No live trading. No paper executor. All outputs are shadow decisions, notebooks, and markdown reports. Phase-2 execution remains gated on `P1-GATE`.

## 0. Goal and research hypothesis

Determine, across **every** Kalshi market category (not just crypto), whether there exists a tradeable time-lag between the authoritative source-data publication and Kalshi's book reprice — and cross-reference that signal with the public Kalshi social leaderboard to see whether top-ranked traders' positions provide additional corroboration. Produce a ranked opportunity list, measure lag distributions on the top candidates, and recommend which category × signal combinations to promote into Phase-2 paper execution.

Two independent candidate signals:

1. **Lag signal** — source-data publication observed before Kalshi book reprice. Widest measurable gap wins.
2. **Smart-money signal** — top leaderboard traders (`kalshi.com/social/leaderboard`, profiles like `kalshi.com/ideas/profiles/ColeBartiromoDOLLARSCHOLAR` and `.../REAKT`) opening positions before the broader book follows. Corroboration gate or standalone.

Hypothesis to test, not assume. A negative result per category is a legitimate deliverable.

## 1. What's already known (don't redo)

- Code in `src/market/kalshi_market.py`, `scripts/kalshi_public_pull.py`, `src/run_kalshi_shadow.py` is crypto-hardcoded (`EXPECTED_CRYPTO_SERIES`, `ASSET_FROM_SERIES`, `SERIES_BY_ASSET`).
- Microsecond-precision DB schema exists: `kalshi_live_book_snapshots`, `reference_ticks`, `shadow_decisions`, `kalshi_historical_trades`, `coinbase_trades`.
- `shadow_decisions.latency_ms_{ref,book}_to_decision` columns are `NULL` today (`src/execution/kalshi_shadow_evaluator.py:233–234`), with explicit comment "populated in P1-M5 when timestamps are captured."
- Three strategies (`stat_model` / `partial_avg` / `pure_lag`) are already running side-by-side for crypto via a `strategy_label` column — multi-strategy plumbing exists and should be reused.
- Contract-terms S3 bucket (`https://kalshi-public-docs.s3.amazonaws.com/contract_terms/`) is publicly listable; 1000+ PDFs. Known PDFs: `CRYPTO15M.pdf`, `FEDDECISION.pdf`, `FED.pdf`, `10Y2Y.pdf`, `10Y3M.pdf`, `RAINM.pdf`, `NOWDATASNOW.pdf`, `NBATOTAL.pdf`, `PRES.pdf`, `APPROVE.pdf`, `538CALL.pdf`, `538STATES.pdf`, `2028RUN.pdf`, `AAPLPRICE.pdf`, `AAPLLAUNCH.pdf`, `AAPLCEOCHANGE.pdf`, `AAARATING.pdf`, `ACHIEVEMENTS.pdf`, `STREAMRANK.pdf`, `1SONG.pdf`, `TIME.pdf`, `WEALTHY.pdf`.
- Best a priori scheduled-release candidates for lag arb: **CPI** (BLS 8:30 AM ET release day), **NFP** (BLS 8:30 AM ET first Friday), **FOMC** (Fed 2:00 PM ET FOMC day; Kalshi LTT 1:55 PM, expiry 2:05 PM).
- Sports: event-driven not scheduled; Kalshi uses a STATSCORE feed.
- Weather: NWS CLI daily climate report; no published exact HHMM; weaker lag-arb target.
- No public measurements exist of Kalshi book reprice lag for any category.

## 2. Task ID convention

`R{milestone}-T{seq}`. R0 = access + discovery. R1 = universe inventory. R2 = refactor. R3 = per-category lag. R4 = opportunity ranking. R5 = leaderboard. R6 = unified evaluator. R7 = feasibility report.

Status markers: `[ ]` not started, `[~]` in progress, `[x]` complete, `[!]` blocked, `[-]` abandoned.

---

## R0 — Access and ToS confirmation (≤1 day, read-only)

- [ ] **R0-T01.** Verify prod Kalshi API key hits `/series`, `/events`, `/markets`, `/markets/{t}/orderbook`, `/markets/trades` without error. **Acceptance:** one successful call logged per endpoint.
- [ ] **R0-T02.** Manually open `kalshi.com/social/leaderboard` in a browser; DevTools → Network → record which XHR/JSON endpoints populate the page; note fields, pagination, time-window selector. **Acceptance:** list of discovered endpoints in `docs/kalshi_leaderboard_data_model.md` §1.
- [ ] **R0-T03.** Same for two example profile pages (`.../ColeBartiromoDOLLARSCHOLAR`, `.../REAKT`): what trade/position/P&L data is public without login, data freshness, obvious JSON endpoints. **Acceptance:** profile-data-model findings in `docs/kalshi_leaderboard_data_model.md` §2.
- [ ] **R0-T04.** Read Kalshi TOS / robots.txt for posture on automated reads of public social pages. Record verbatim the clauses that bear on scraping. **Acceptance:** "can we fetch programmatically: yes / rate-limited-only / no" decision recorded in `docs/kalshi_leaderboard_data_model.md` §3. **If "no" → R5 drops to "one-time manual snapshots only."**
- [ ] **R0-T05.** Confirm the S3 `contract_terms/` bucket listing via `?list-type=2&prefix=contract_terms/` + pagination works unauthenticated. **Acceptance:** one successful paginated listing captured to a local fixture.

## R1 — Universe inventory and registry

Produce one source-of-truth registry every downstream phase reads from.

- [ ] **R1-T01.** `scripts/kalshi_series_discover.py` — paginate `GET /series` (no category filter); upsert into new `kalshi_series` table (`series_ticker` PK, `category`, `title`, `frequency`, `contract_terms_url`, `raw_json`, `fetched_ts`). **Acceptance:** script run populates ≥ N series across ≥ M categories; table indexed on `category`.
- [ ] **R1-T02.** `scripts/kalshi_contract_terms_pull.py` — paginate the S3 bucket; download each `contract_terms/*.pdf` to `data/contract_terms/`; upsert into new `kalshi_contract_terms` table (`pdf_url` UNIQUE, `series_ticker_guess`, `local_path`, `bytes`, `sha256`, `fetched_ts`). **Acceptance:** on second run, no duplicate downloads (idempotent); sha256 verified.
- [ ] **R1-T03.** `scripts/kalshi_registry_build.py` — join the two tables; for each series extract (via PDF-to-text) resolution source, source agency, publication schedule, LTT-to-expiry window. Output `config/kalshi_series_registry.json` keyed by series_ticker with fields `{ category, source_type, source_agency, source_url, publish_schedule_utc, ltt_to_expiry_s, strategy_hypothesis, notes }`. `source_type ∈ { scheduled_release, continuous_index, event_driven_scored, event_driven_news, daily_report, unknown }`. **Acceptance:** every active series has at minimum `category` and `source_type` set; `unknown` count < 10% of total.
- [ ] **R1-T04.** New migrations in `scripts/migrate_db.py` under `SAFE_ALTER_STATEMENTS`: `kalshi_series`, `kalshi_contract_terms` (schemas in §11). **Acceptance:** `python3.11 scripts/migrate_db.py` runs clean on fresh DB and idempotently on existing DB.
- [ ] **R1-T05.** Produce `docs/kalshi_series_registry_snapshot.md` — table of N series per category per source_type. Highlight which categories are populous enough to warrant per-category lag measurement. **Acceptance:** doc written, checked in.

## R2 — Refactor to category-agnostic

Remove crypto hardcodes so R3+ can run per-category without duplication. Reuse the three-strategy multi-strategy plumbing already in place.

- [ ] **R2-T01.** Fill the two latency columns in `shadow_decisions`. `src/market/kalshi_market.py::book_to_market_quote()` adds `book_received_ts_us` captured the moment REST/WS decodes. `src/execution/kalshi_shadow_evaluator.py:233–234` replaces the two `None,` placeholders with computed ms-latency. **Acceptance:** extended test in `tests/test_kalshi_shadow_evaluator.py` asserts non-null latency on every written row; one live run confirms `SELECT count(*) FROM shadow_decisions WHERE latency_ms_ref_to_decision IS NULL AND ts_us > <ts>` returns 0.
- [ ] **R2-T02.** `src/market/kalshi_market.py` — replace `EXPECTED_CRYPTO_SERIES` + `discover_active_crypto_markets()` with `discover_active_markets(series_tickers: Iterable[str])` reading from registry. **Acceptance:** existing crypto run path still works end-to-end with registry input.
- [ ] **R2-T03.** `src/run_kalshi_shadow.py` — drop `ASSET_FROM_SERIES`; build `(category, strategy, reference_source)` triples from `config/kalshi_series_registry.json`. **Acceptance:** `python3.11 -m run_kalshi_shadow --iterations 3 --no-sleep` green against the registry-driven config.
- [ ] **R2-T04.** `scripts/kalshi_public_pull.py` and `scripts/kalshi_trades_pull.py` — accept `--series` / `--category` flags instead of hardcoded crypto assets. **Acceptance:** existing crypto pulls reproduce via the new flags.
- [ ] **R2-T05.** Create `src/reference/` package with common `ReferenceSource` protocol: `latest_value(market_ticker) -> (value, ts_us) | None` and `is_healthy() -> bool`. Move existing `src/market/crypto_reference.py::BasketReferenceSource` to `src/reference/crypto_basket.py`. **Acceptance:** `pytest -q` green; no import breakage.
- [ ] **R2-T06.** `src/reference/bls_release.py` — BLS poller for CPI and NFP release URLs (release times are UTC-fixed so polling is cheap around scheduled windows). Captures headline figure + release-ts. **Acceptance:** unit test hits a recorded HTML fixture and parses the expected figure + ts.
- [ ] **R2-T07.** `src/reference/fomc_statement.py` — poll `federalreserve.gov/newsevents/pressreleases/monetary.htm` on FOMC days; parse rate target + statement-ts. **Acceptance:** unit test on recorded fixture.
- [ ] **R2-T08.** `src/reference/nws_station.py` — poll NWS METAR for Kalshi-relevant stations (NYC Central Park, Chicago Midway, Miami Intl, Austin Bergstrom). **Acceptance:** unit test on recorded fixture; config-driven station list.
- [ ] **R2-T09.** `src/reference/statscore_sports.py` — stub with explicit TODO; real sports scoring requires paid feed or scoreboard scrape. Marked R6-optional. **Acceptance:** stub raises `NotImplementedError` and is excluded from default pipeline.

## R3 — Per-category lag measurement

For each top-tier category, produce a lag-distribution notebook. Notebooks are `.py` (not `.ipynb`) for diffable review.

- [ ] **R3-T01.** `notebooks/kalshi_lag_crypto.py` — reuse existing `coinbase_trades` + `kalshi_historical_trades`; methodology in §10. **Acceptance:** p50/p90/p95/p99 lag table per asset × full-window vs final-minute × distance-band published.
- [ ] **R3-T02.** `notebooks/kalshi_lag_cpi.py` — pair BLS CPI release events (historical archive) with Kalshi CPI-series trades around release. **Acceptance:** lag table across ≥ 6 historical CPI releases; direction-aware magnitude.
- [ ] **R3-T03.** `notebooks/kalshi_lag_nfp.py` — same for BLS Employment Situation. **Acceptance:** lag table across ≥ 6 historical NFP releases.
- [ ] **R3-T04.** `notebooks/kalshi_lag_fomc.py` — same for FOMC statements. **Acceptance:** lag table across ≥ 4 historical FOMC meetings, including the 2:00 → 2:05 PM ET narrow window.
- [ ] **R3-T05.** `notebooks/kalshi_lag_weather.py` — pair NWS station readings with Kalshi weather-series trades. **Acceptance:** lag table; expected wider-than-macro lag given no fixed release HHMM.
- [ ] **R3-T06.** `notebooks/kalshi_lag_companies.py` — pair earnings-release tsdelivery (SEC EDGAR / company IR) with Kalshi `AAPLPRICE`-style trades. **Acceptance:** lag table across ≥ 3 earnings events.
- [ ] **R3-T07.** `notebooks/kalshi_lag_commodities.py` — pair EIA / USDA / DOE public releases with Kalshi commodity-series trades. **Acceptance:** lag table across ≥ 3 release types.

**Per-category acceptance decision** (same threshold across all):
- `p50 < 500 ms` → MM-dominated; no solo-operator edge; mark category `abandon`.
- `p50 ∈ [500 ms, 5 s]` → tight but possibly tractable; mark `marginal`.
- `p50 > 5 s` → clear opportunity; mark `pursue`.

## R4 — Opportunity ranking and deep-dive selection

This is the key deliverable the user called out: **after investigation, identify opportunities and pick what to deep-dive on.**

- [ ] **R4-T01.** `docs/kalshi_lag_opportunity_ranking.md` — one table ranking every measured category on columns: `category | p50 lag | p95 lag | daily vol USD | est capacity/day at $500 notional | source access (free/paid) | signal quality | ranking | recommendation`. **Acceptance:** every category from R3 has a row; explicit ranking 1..N.
- [ ] **R4-T02.** Top-3 recommendation memo inside the same doc: per top-3 category, one-paragraph recommendation (why it ranks; what the minimum-viable signal looks like; estimated effort to operationalize). **Acceptance:** user checkpoint — user picks which 2–3 categories advance into R6 unified evaluator. **This is the explicit decision point.**

## R5 — Smart-money / leaderboard track (parallel to R3)

Gated on R0-T04 returning "yes" or "rate-limited-only."

- [ ] **R5-T01.** `docs/kalshi_leaderboard_data_model.md` — discovery findings from R0 tasks: endpoints, fields, freshness, pagination, TOS posture. **Acceptance:** doc written before any puller code.
- [ ] **R5-T02.** `scripts/kalshi_leaderboard_pull.py` — polite-paced snapshot puller (e.g. 1 req / 2 s; hourly cron). Writes to new `kalshi_leaderboard_snapshots` table (`snapshot_ts`, `username`, `rank`, `metric`, `metric_value`, `time_window`). **Acceptance:** runs for 24 h under cron without triggering Kalshi rate-limits; DB shows ≥ 24 snapshots.
- [ ] **R5-T03.** `scripts/kalshi_profile_pull.py` — per-username pull of open positions + recent trades. Writes to new `kalshi_leader_positions` (`username`, `snapshot_ts`, `market_ticker`, `side`, `size`, `avg_price`) and `kalshi_leader_trades` (`username`, `trade_ts_us`, `market_ticker`, `side`, `size`, `price`, `trade_id`) tables. **Acceptance:** successful pull on top 10 leaderboard users.
- [ ] **R5-T04.** `src/strategy/kalshi_smart_money.py` — `SmartMoneySignal` with two modes:
  - **Copy-watch:** when a top-N user opens a new position > $X, emit a score on that market within Y minutes.
  - **Corroboration:** for any market the lag signal flags, score whether ≥ K top-N users hold same-direction exposure; act as gate / booster.
  **Acceptance:** `tests/test_kalshi_smart_money.py` covers both modes on fixture snapshots.
- [ ] **R5-T05.** `notebooks/kalshi_leader_edge_analysis.py` — retrospective analysis: for top-N leaderboard users, compute the realized edge of their historical trades (using `kalshi_leader_trades` + historical market resolutions). Rank by risk-adjusted return. **Acceptance:** table of top-N users with `n_trades | realized_edge_cents | hit_rate | brier | category_focus`. If no user beats break-even by a meaningful margin, flag the whole track as low-value before R5-T04 investment.

## R6 — Unified multi-category shadow evaluator

Reuse the existing `strategy_label`-driven multi-strategy plumbing.

- [ ] **R6-T01.** Extend `src/execution/kalshi_shadow_evaluator.py` to accept a list of `(category, strategy, reference_source)` triples; per tick, iterate and tag each decision with `category` + `signal_type` (new columns). **Acceptance:** unit tests cover ≥ 2 categories + ≥ 2 signal types in one `tick()` call.
- [ ] **R6-T02.** Add migrations: `ALTER TABLE shadow_decisions ADD COLUMN category TEXT`, `ALTER TABLE shadow_decisions ADD COLUMN signal_type TEXT`. **Acceptance:** `scripts/migrate_db.py` idempotent on existing DB.
- [ ] **R6-T03.** `src/run_kalshi_shadow.py` — build triples list from registry + R4-T02 selected categories. **Acceptance:** live shadow run over 24 h populates rows for every selected category × signal_type.
- [ ] **R6-T04.** Dashboard-lite: one `psql`-style query cookbook in `docs/kalshi_shadow_query_cookbook.md` — recipes for per-category hit rate, per-signal-type edge, smart-money corroboration impact. **Acceptance:** every recipe returns rows against the live DB.

## R7 — Feasibility report and go/no-go

Hooks into the existing P1-M5-T05 deliverable.

- [ ] **R7-T01.** Extend `docs/kalshi_phase1_feasibility_report.md` with per-category + per-signal-type conclusions: does each combination clear the pre-committed Phase-1→2 thresholds (post-fee edge > 1.5¢, ≥ 500 decisions, hit rate materially above 50%, capacity > $X/day)? **Acceptance:** report written; explicit go/no-go per combination.
- [ ] **R7-T02.** Cross-signal analysis: does the smart-money signal add, subtract, or wash versus the lag signal in isolation? **Acceptance:** one-paragraph conclusion supported by a 2×2 table (lag-only / smart-money-only / both / neither edge).
- [ ] **R7-T03.** Recommendation to user: which 1–2 (category × signal-combo) to advance into Phase-2 paper executor. **Acceptance:** recommendation captured; user decision recorded inline with date.

---

## 8. Sequencing and parallelism

```
R0 (access + ToS)
  └─ R1 (inventory) ─ R2 (refactor + latency cols)
        ├── R3 (per-category lag) ──────────────┐
        └── R5 (leaderboard, parallel) ─────────┤
                                                ▼
                                         R4 (opportunity ranking)  ← checkpoint with user
                                                ▼
                                         R6 (unified evaluator)
                                                ▼
                                         R7 (feasibility report)
                                                ▼
                                         P1-GATE (existing)
```

Est. elapsed time: ≈ 1 week to R4's opportunity-ranking deliverable (the "identify opportunities" output). ≈ 3–4 weeks for the full plan through R7.

## 9. Out of scope

- Any live trading / paper executor / risk rules. Gated on `P1-GATE` via R7 output.
- Front-running leaderboard users. Smart-money signal is corroboration only.
- Sports-scoring paid feed. R6-optional; only if sports ranks top-3 in R4.
- Licensed CF Benchmarks BRTI feed for crypto. Existing Coinbase-only reference is acceptable per the tracking-error report.

## 10. Methodology note — lag measurement algorithm

For each paired (source-publish-event, market) tuple:

1. Identify the exact source-publish timestamp (`ts_src_us`) — BLS release JSON, Fed statement scrape, NWS METAR tick, or Coinbase trade depending on category.
2. Identify the first meaningful Kalshi reprice — either a Kalshi trade on that market with `|price - price_before_event| ≥ THRESHOLD_CENTS`, or a book snapshot where top-of-book crosses THRESHOLD_CENTS.
3. `lag_ms = (ts_kalshi_us - ts_src_us) / 1000`.
4. Filter negative lags (Kalshi-led moves) and lags > 60 s (stale pairings).
5. Aggregate per (category, window-split, distance-from-strike band): median / p90 / p95 / p99 / histogram.

Direction- and magnitude-aware: a lag only matters if the source move was large enough that Kalshi was obliged to reprice. Threshold per category tuned during R3.

## 11. New DB tables and columns

Append to `SAFE_ALTER_STATEMENTS` in `scripts/migrate_db.py`:

```sql
CREATE TABLE IF NOT EXISTS kalshi_series (
  series_ticker TEXT PRIMARY KEY,
  category TEXT NOT NULL,
  title TEXT,
  frequency TEXT,
  contract_terms_url TEXT,
  raw_json TEXT NOT NULL,
  fetched_ts BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ks_category ON kalshi_series(category);

CREATE TABLE IF NOT EXISTS kalshi_contract_terms (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pdf_url TEXT UNIQUE NOT NULL,
  series_ticker_guess TEXT,
  local_path TEXT,
  bytes INTEGER,
  sha256 TEXT,
  fetched_ts BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS kalshi_leaderboard_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  snapshot_ts BIGINT NOT NULL,
  username TEXT NOT NULL,
  rank INTEGER NOT NULL,
  metric TEXT NOT NULL,
  metric_value TEXT NOT NULL,
  time_window TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_klb_snap_user ON kalshi_leaderboard_snapshots(snapshot_ts, username);

CREATE TABLE IF NOT EXISTS kalshi_leader_positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL,
  snapshot_ts BIGINT NOT NULL,
  market_ticker TEXT NOT NULL,
  side TEXT NOT NULL,
  size TEXT NOT NULL,
  avg_price TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_klp_user_snap ON kalshi_leader_positions(username, snapshot_ts);

CREATE TABLE IF NOT EXISTS kalshi_leader_trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL,
  trade_ts_us BIGINT NOT NULL,
  market_ticker TEXT NOT NULL,
  side TEXT NOT NULL,
  size TEXT NOT NULL,
  price TEXT NOT NULL,
  trade_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_klt_user_ts ON kalshi_leader_trades(username, trade_ts_us);

ALTER TABLE shadow_decisions ADD COLUMN category TEXT;
ALTER TABLE shadow_decisions ADD COLUMN signal_type TEXT;
```

## 12. Critical files

**Modify**
- `src/market/kalshi_market.py` — remove crypto hardcodes; add `book_received_ts_us`.
- `src/execution/kalshi_shadow_evaluator.py:233–234` — fill latency columns; accept multi-strategy + multi-category config.
- `src/run_kalshi_shadow.py` — drop `ASSET_FROM_SERIES`; build config from registry.
- `src/market/crypto_reference.py` → move to `src/reference/crypto_basket.py`.
- `scripts/migrate_db.py` — append §11 schemas.
- `scripts/kalshi_public_pull.py`, `scripts/kalshi_trades_pull.py` — accept `--series` / `--category`.

**Create**
- `scripts/kalshi_series_discover.py`, `scripts/kalshi_contract_terms_pull.py`, `scripts/kalshi_registry_build.py`.
- `config/kalshi_series_registry.json`.
- `src/reference/{bls_release,fomc_statement,nws_station,statscore_sports}.py`.
- `notebooks/kalshi_lag_{crypto,cpi,nfp,fomc,weather,companies,commodities}.py`.
- `docs/kalshi_series_registry_snapshot.md`, `docs/kalshi_lag_opportunity_ranking.md`, `docs/kalshi_leaderboard_data_model.md`, `docs/kalshi_shadow_query_cookbook.md`.
- `scripts/kalshi_leaderboard_pull.py`, `scripts/kalshi_profile_pull.py`.
- `src/strategy/kalshi_smart_money.py`, `tests/test_kalshi_smart_money.py`.
- `notebooks/kalshi_leader_edge_analysis.py`.

**Reuse (do not reimplement)**
- `FairValueModel`, `StrategyConfig` in `src/strategy/kalshi_fair_value.py` — pattern for per-category strategies.
- `BasketReferenceSource` — pattern for the `ReferenceSource` protocol.
- `KalshiShadowEvaluator.tick()` loop + `strategy_label` multi-strategy plumbing.
- `lib/trading_platform` primitives (CircuitBreaker, RetryPolicy, metrics) for per-category pollers.
- Existing microsecond-precision timestamp fields on every table.

## 13. Global verification

- `pytest -q` green at every milestone.
- After R2: one live `--iterations 3 --no-sleep` shadow run populates non-null latency columns.
- After R3: one `notebooks/kalshi_lag_<cat>.py` run per category emits the lag table.
- After R4: `docs/kalshi_lag_opportunity_ranking.md` exists with ranking + top-3 memo.
- After R5: `kalshi_leaderboard_snapshots` table has ≥ 24 h of hourly snapshots; smart-money unit tests green.
- After R6: `SELECT category, signal_type, count(*) FROM shadow_decisions WHERE ts_us > now() - interval '24 hours' GROUP BY 1,2` returns ≥ 4 rows.
- After R7: feasibility report written with explicit per-combination go/no-go; user decision on advance-to-P2 recorded.

## 14. Decision checkpoints

1. **End of R0** (ToS): drop R5 entirely or continue.
2. **End of R4** (opportunity ranking): user picks top 2–3 (category × signal-combo) to push through R6+.
3. **End of R5-T05** (leader edge analysis): if no leaderboard user beats break-even by a meaningful margin, abandon R5-T04 build.
4. **End of R7** (feasibility report): hands off to `P1-GATE`.
