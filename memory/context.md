# KalshiTrader — Session Context

**Last updated:** 2026-04-19
**Purpose:** Orient a future session quickly. Not a substitute for the docs; complements them.

## What this project is

Kalshi crypto fair-value scanner. Research-grade tool that measures the timing lag between CF Benchmarks reference prices and the Kalshi 15-min BTC / ETH / SOL orderbook, and — if the edge proves out — trades it at small size.

**Two-phase structure** (per strategy plan):

- **Phase 1 — Feasibility Research (zero money at risk).** Historical + live data capture, shadow evaluator records hypothetical decisions, feasibility report produced.
- **Phase 1 → Phase 2 Gate.** Pre-committed thresholds.
- **Phase 2 — Execution.** Risk rules, paper executor, live executor (three-opt-in gated), custom dashboard, paper-in-prod → live small → stepped scale.

## Status (2026-04-19)

- **Pre-implementation.** Docs are complete; no `src/` code yet beyond this memory file.
- Repo pivoted from prior DEX/Polymarket work. Local history reset to a fresh single root commit; pre-pivot state preserved at local tag `legacy-pre-kalshi` (not pushed).
- Remote: `https://github.com/twainstain/KalshiTrader.git`.

## Resolved architectural decisions

- **A-01 Platform primitives:** `trading_platform` will be added as a git submodule at `lib/trading_platform/`. Upstream: `https://github.com/twainstain/trading-platform.git`. Access via a local `src/platform_adapters.py` (mirrors the prior DEX-bot pattern). No direct `trading_platform.*` imports from domain code.
- **A-02 Repo shell bootstrap:** Start fresh. No selective restore from old history. `pyproject.toml`, `Dockerfile`, `docker-compose.yml`, `scripts/migrate_db.py`, persistence layer, etc. get written fresh when P1-M0 begins.

## Pending

- **Prerequisites (P-01 → P-05):** user-side — KYC'd Kalshi account, demo API key pair at `demo.kalshi.co/account/profile`, Phase-1 budget committed, docs read. See `docs/kalshi_scanner_implementation_tasks.md` §5.
- **Next concrete task:** P1-M0-T01 — execute A-02 (fresh `src/`, `tests/`, `scripts/`, `pyproject.toml`, `.env.example`, `docker-compose.yml`) + P1-M0-T02 (`git submodule add lib/trading_platform`).

## Key references within the repo

- `CLAUDE.md` — repo-wide orientation + open decisions tracker.
- `README.md` — project intro + doc pointers.
- `docs/kalshi_crypto_fair_value_scanner_plan.md` — strategy & thesis (**read first for any question on "what" or "why"**).
- `docs/kalshi_scanner_execution_plan.md` — architecture, platform conventions, milestone walkthrough (**read for "how" questions**).
- `docs/kalshi_scanner_implementation_tasks.md` — status-tracked task list (P1-M0 → P2-M6). **Working doc during implementation; update statuses as tasks complete.**
- `docs/crypto_arbitrage_feasibility_research.md` — broader landscape context (why Kalshi over alternatives).
- `docs/prediction_market_arbitrage_video_breakdown_and_opinion.md` — failure-mode taxonomy.

## Kalshi API quick reference (extracted 2026-04-19)

- **Prod REST:** `https://api.elections.kalshi.com/trade-api/v2`
- **Demo REST:** `https://demo-api.kalshi.co/trade-api/v2`
- **Prod WS:** `wss://api.elections.kalshi.com/` (orderbook at `/orderbook_delta`)
- **Auth:** RSA-PSS SHA-256 — headers `KALSHI-ACCESS-KEY` / `KALSHI-ACCESS-TIMESTAMP` (ms) / `KALSHI-ACCESS-SIGNATURE` (base64). Signed payload: `{ts_ms}{METHOD}{path_without_query}`.
- **Python SDK:** `kalshi_python_sync` (sync) or `kalshi_python_async`. Deprecated: `kalshi-python` — **do not use**.
- **Contract terms (authoritative):** `https://kalshi-public-docs.s3.amazonaws.com/contract_terms/CRYPTO15M.pdf` — Source Agency CF Benchmarks; 60 s simple-average at close; threshold-binary payouts; no-data → resolves No.
- **Pricing conventions:** Prices in dollar strings (`"0.4200"` = $0.42); quantities fixed-point (`"13.00"`). Orderbook stores **only bids**; asks derived (`YES ask @ X = NO bid @ (1 − X)`).
- **Rate limits:** per second — Basic 20r/10w → Prime 400r/400w. Writes = order-mutating endpoints only.

## Critical guardrails (do not violate)

- **Paper is default. Three explicit opt-ins for Phase-2 live:** `--execute` flag AND `KALSHI_API_KEY_ID` populated AND config `mode: "live"` + `dry_run: false`.
- **Decimal, never float** for any financial field.
- **`fee_included=False`** on every Kalshi `MarketQuote`.
- **No direct `trading_platform.*` imports** in `src/` once the submodule lands — route everything through `src/platform_adapters.py`.
- **No `docker compose down -v`** on the postgres service — destroys `pg-data`.
- **This repo only.** Implementation lives here; do not edit the sibling `/Users/tamir.wainstain/src/ArbitrageTrader/` (separate project).

## Sibling repos (for reference only — do not edit from here)

- `/Users/tamir.wainstain/src/ArbitrageTrader/` — the prior DEX-bot project. Unrelated now. Keep as reference for AT's `platform_adapters.py` patterns and test conventions when writing analogous Kalshi code.
- `/Users/tamir.wainstain/src/trading-platform/` (if cloned locally) — the generic primitives used via submodule. Submodule URL: `https://github.com/twainstain/trading-platform.git`.
