# KalshiTrader — Session Context

**Last updated:** 2026-04-19 ~22:00 (2-hour bucket)
**Purpose:** Orient a future session quickly. Not a substitute for the docs; complements them.
**Previous snapshot:** [`context_20260419_2200.md`](./context_20260419_2200.md) (pre-implementation state before P1-M0 landed).

## What this project is

Kalshi crypto fair-value scanner. Research-grade tool that measures the timing lag between CF Benchmarks reference prices and the Kalshi 15-min BTC / ETH / SOL orderbook, and — if the edge proves out — trades it at small size.

**Two-phase structure** (per strategy plan):

- **Phase 1 — Feasibility Research (zero money at risk).** Historical + live data capture, shadow evaluator records hypothetical decisions, feasibility report produced.
- **Phase 1 → Phase 2 Gate.** Pre-committed thresholds.
- **Phase 2 — Execution.** Risk rules, paper executor, live executor (three-opt-in gated), custom dashboard, paper-in-prod → live small → stepped scale.

## Status (2026-04-19)

- **P1-M0 scaffolding: complete (6/6).** Repo shell bootstrap, `trading_platform` submodule, `pyproject.toml`, `.env.example`, `src/core/models.py`, `scripts/migrate_db.py`. Migration runs green; `data/kalshi.db` created.
- **P1-M1 live data collection: code complete (11/13).** `KalshiMarketSource` with book→MarketQuote mapping, lifecycle tags, WS loop scaffold, `CircuitBreaker` / `RetryPolicy` wiring. `CryptoReferenceSource` Protocol + `BasketReferenceSource` with outlier rejection and 60s rolling average. `LicensedCFBenchmarksSource` stub. `reference_ticks` persistence helpers.
- **Tests: 78/78 passing.** `tests/test_models.py` (15), `tests/test_platform_adapters.py` (7), `tests/test_migrate_db.py` (6), `tests/test_kalshi_market.py` (29), `tests/test_crypto_reference.py` (19+).
- **Progress tracker:** 19/89 tasks complete — see `docs/kalshi_scanner_implementation_tasks.md`.

## Resolved architectural decisions

- **A-01 Platform primitives:** `trading_platform` added as submodule at `lib/trading_platform/` (commit `495b4c2`, head=`main`). All access goes through `src/platform_adapters.py` — no direct `trading_platform.*` imports in domain code.
- **A-02 Repo shell bootstrap:** Start fresh. Working tree was clean with no `src/` / `tests/` / `scripts/` on disk — option (b) was the default-by-state. New Kalshi-focused code authored directly rather than reviving the prior DEX scaffolding.

## Blocked on prerequisites (P-02 demo key)

Two unit-test-covered tasks need a demo API key to exercise the live path:

- **P1-M1-T02 live:** `client.get_balance()` sanity check on `demo-api.kalshi.co`. Code wired via `make_client()` with lazy SDK import.
- **P1-M1-T04 live:** WS handshake against `wss://api.elections.kalshi.com/orderbook_delta`. Loop structure + reconnect backoff + breaker + stop signaling are in place; only the handshake + message parsing remain.

**Unblocks on:** user generates demo key by logging into `https://demo.kalshi.co` → Account & security → API Keys → Create Key (Kalshi docs do **not** publish a direct `/account/profile` deep link for demo — use UI nav). Prod path, for later Phase 2, is `https://kalshi.com/account/profile` (direct link IS valid). Script `scripts/store_kalshi_key.sh` writes PEM to `~/.kalshi/<env>_private_key.pem` (chmod 600) and wires `KALSHI_API_KEY_ID` + `KALSHI_PRIVATE_KEY_PATH` into `.env`.

## Next concrete work

1. **If P-02 resolved:** run `pip install -e ".[dev]"`, then `make_client().get_balance()` smoke, then run `_ws_loop` against demo to capture a live snapshot + ≥10 deltas (T04 live verification). Also unblocks all of P1-M2 (historical data pull needs auth for `/historical/markets`).
2. **If P-02 not yet resolved, P1-M3 is independent:** fair-value model + backtest over replayed fixtures. Doesn't need any live connectivity. Entry points: `src/strategy/kalshi_fair_value.py` + `src/run_kalshi_backtest.py`.

## Key files (reality, not plan)

- `CLAUDE.md` — repo orientation. Has a stale "this is a research folder, no .py files" section; the "Implementation conventions" section supersedes it.
- `pyproject.toml` — deps + pytest pythonpath wiring (`src`, `lib/trading_platform/src`, `scripts`).
- `.env.example` — Kalshi keys + `DATABASE_URL`. User supplies `.env` (gitignored).
- `src/core/models.py` — `MarketQuote` / `Opportunity` / `ExecutionResult` / `OpportunityStatus` + `SUPPORTED_VENUES`. Decimal-only, frozen, validated.
- `src/platform_adapters.py` — Kalshi-flavored `CircuitBreaker` + `KalshiAPIError` + re-exports of `RetryPolicy`, `PriorityQueue`, etc.
- `src/market/kalshi_market.py` — read-only feed. Pure helpers (`book_to_market_quote`, `lifecycle_tag`, `parse_dollar_string`, `book_depth_usd`) + `KalshiMarketSource` class + WS scaffold.
- `src/market/crypto_reference.py` — `BasketReferenceSource` + `LicensedCFBenchmarksSource` + persistence helpers.
- `scripts/migrate_db.py` — idempotent SQLite+Postgres migrations for all 5 P1 tables.
- `tests/conftest.py` — adds `src/`, `lib/trading_platform/src/`, and `scripts/` to sys.path.

## Critical guardrails (do not violate)

- **Paper is default. Three explicit opt-ins for Phase-2 live:** `--execute` flag AND `KALSHI_API_KEY_ID` populated AND config `mode: "live"` + `dry_run: false`.
- **Decimal, never float** for any financial field. `MarketQuote.__post_init__` auto-coerces and enforces.
- **`fee_included=False`** on every Kalshi `MarketQuote` — the model raises `ValueError` if you try `True`.
- **No direct `trading_platform.*` imports** in `src/` — route everything through `src/platform_adapters.py`.
- **No `docker compose down -v`** on the postgres service — destroys `pg-data`.
- **This repo only.** Implementation lives here; do not edit the sibling `/Users/tamir.wainstain/src/ArbitrageTrader/` (separate project).

## Kalshi API quick reference (extracted 2026-04-19)

- **Prod REST:** `https://api.elections.kalshi.com/trade-api/v2`
- **Demo REST:** `https://demo-api.kalshi.co/trade-api/v2`
- **Prod WS:** `wss://api.elections.kalshi.com/` (orderbook at `/orderbook_delta`)
- **Auth:** RSA-PSS SHA-256 — headers `KALSHI-ACCESS-KEY` / `KALSHI-ACCESS-TIMESTAMP` (ms) / `KALSHI-ACCESS-SIGNATURE` (base64). Signed payload: `{ts_ms}{METHOD}{path_without_query}`.
- **Python SDK:** `kalshi_python_sync` (sync) or `kalshi_python_async`. Deprecated: `kalshi-python` — **do not use**.
- **Contract terms (authoritative):** `https://kalshi-public-docs.s3.amazonaws.com/contract_terms/CRYPTO15M.pdf` — Source Agency CF Benchmarks; 60 s simple-average at close; threshold-binary payouts; no-data → resolves No.
- **Pricing conventions:** Prices in dollar strings (`"0.4200"` = $0.42); quantities fixed-point (`"13.00"`). Orderbook stores **only bids**; asks derived (`YES ask @ X = NO bid @ (1 − X)`).
- **Rate limits:** per second — Basic 20r/10w → Prime 400r/400w. Writes = order-mutating endpoints only.
- **Crypto series tickers (expected):** `KXBTC15M`, `KXETH15M`, `KXSOL15M`. Verified at runtime in `discover_active_crypto_markets()`.

## Key references within the repo

- `CLAUDE.md` — repo-wide orientation + open decisions tracker (decisions now resolved).
- `README.md` — project intro + doc pointers (still stale per CLAUDE.md; will be rewritten post-Phase-1).
- `docs/kalshi_crypto_fair_value_scanner_plan.md` — strategy & thesis (**read first for any question on "what" or "why"**).
- `docs/kalshi_scanner_execution_plan.md` — architecture, platform conventions, milestone walkthrough (**read for "how" questions**).
- `docs/kalshi_scanner_implementation_tasks.md` — status-tracked task list (P1-M0 → P2-M6). **Working doc during implementation; update statuses as tasks complete.**
- `docs/crypto_arbitrage_feasibility_research.md` — broader landscape context (why Kalshi over alternatives).
- `docs/prediction_market_arbitrage_video_breakdown_and_opinion.md` — failure-mode taxonomy.

## Verification commands

```bash
python3.11 -m pytest tests/ -q           # expect 78 passed
python3.11 scripts/migrate_db.py         # expect "sqlite migration complete: data/kalshi.db"
# Once deps installed (pip install -e .[dev]) and P-02 resolved:
python3.11 -c "import kalshi_python_sync, websockets, pandas, pyarrow; print('ok')"
```

## Sibling repos (for reference only — do not edit from here)

- `/Users/tamir.wainstain/src/ArbitrageTrader/` — the prior DEX-bot project. Unrelated now. Keep as reference for AT's `platform_adapters.py` patterns and test conventions when writing analogous Kalshi code.
- `/Users/tamir.wainstain/src/trading-platform/` (if cloned locally) — the generic primitives used via submodule at `lib/trading_platform/`. Upstream: `https://github.com/twainstain/trading-platform.git`.
