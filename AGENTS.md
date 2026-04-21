# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## What this repo is

This repo is the **Kalshi crypto fair-value scanner** project: docs, code, tests, configs, and deploy scripts all live here. Self-contained — no sibling repo. The project is structured as **two serial phases** with an explicit gate in between:

- **Phase 1 — Scanner / Feasibility Research (zero money at risk).** Collect historical + live Kalshi data + our basket-of-exchanges reference ticks; run a shadow evaluator that records hypothetical decisions without submitting orders; produce a feasibility report with measured lag, realized-if-traded edge, hit rate, and capacity estimate.
- **Phase 1 → Phase 2 Gate.** Phase 2 begins only if the feasibility report clears pre-committed thresholds.
- **Phase 2 — Execution (real money, tiny → scaled).** Risk rules, paper executor, live executor (three-opt-in gated), custom dashboard with live-account API calls, paper-in-prod for 4 weeks, live small-size for 2 weeks, then stepped scale.

Older commits include a prior Python/Solidity **DEX cross-venue arbitrage** implementation (unrelated to Kalshi). Most of its source was deleted in the 2026-04-19 cleanup; the working tree shows those deletions pending. Do **not** revive DEX-specific modules (`contracts/`, `src/market/onchain_market.py`, `src/market/subgraph_market.py`, arb scanner code, etc.). You **may** selectively restore **generic infrastructure** from git history when bootstrapping Phase 1 — e.g. `pyproject.toml`, `docker-compose.yml`, `scripts/migrate_db.py`, persistence layer, platform-adapter / circuit-breaker / retry / base-pipeline primitives — adapting them to the Kalshi-only scope. Decide case-by-case; don't wholesale revert the deletion.

**`README.md` is stale.** It describes the prior EVM cross-DEX arbitrage bot (Quick Start, CLI options, 12-chain support, FlashArbExecutor, 202-test suite). That is archival; do not cite as current. This file (`AGENTS.md`) wins when they disagree. `README.md` will be rewritten once Phase 1 code lands.

## What lives in `docs/`

Read the relevant doc before answering user questions in its domain. Current set (working tree):

- **`kalshi_crypto_fair_value_scanner_plan.md`** — strategy / edge-thesis doc. Covers: reality check (not arbitrage — statistical fair-value; near-expiry as the primary structural edge), strategy taxonomy (A: near-expiry structural / B: feed-lag scalping / C: implied-vol), **authoritative resolution mechanics from `CRYPTO15M.pdf`** (§0.5: CF Benchmarks source agency, 60s averaging, threshold-binary payouts, no-data-resolves-No tail risk, $0.001 tick, $25k position accountability), module layout, and the **two-phase program** (§1 and §7) — Phase 1 feasibility-research, Phase 1→2 gate with pre-committed thresholds, Phase 2 execution. **Primary anchor doc.**
- **`kalshi_scanner_execution_plan.md`** — architecture / how-to companion. Translates strategy into concrete file paths, commands, and acceptance tests inside the sibling `ArbitrageTrader` platform. Covers: ArbitrageTrader platform rules (PYTHONPATH, `platform_adapters.py`, Decimal-only, three-opt-in live gate, paper default, pre-commit ritual), domain-model extensions (new `MarketQuote` fields, `SUPPORTED_VENUES`, Phase-1 research tables), **phased directory layout (§4) flagging P1 vs P2 modules**, milestone sequence (P1-M0 → P1-M5 → Gate → P2-M1 → P2-M6), validate-after-code-changes + validate-deployment rituals (§7 / §8), Invariants block (§9), persistence + shared-Postgres safety (§2.4), deploy details, Kalshi pitfalls. **Read before writing code in `ArbitrageTrader`.**
- **`kalshi_scanner_implementation_tasks.md`** — explicit, trackable task list organized into Phase 1 / Phase 1→2 Gate / Phase 2. 87 numbered tasks (`P1-M0-T01` through `P2-M6-T04`), each with stable ID, acceptance criterion, and status marker (`[ ] [~] [x] [!] [-]`). Includes a **Kalshi API reference card** (base URLs, RSA-PSS signing, SDK names, orderbook shape, WS channels, rate limits, lifecycle states, endpoint list with phase tags) pulled from `docs.kalshi.com` on 2026-04-19. **Working doc during implementation** — update statuses as tasks complete; progress summary at §2.
- **`crypto_arbitrage_feasibility_research.md`** — background landscape doc covering DEX-centric and cross-venue (CEX, perps, options, stablecoin, prediction-market) arb categories, ending with a ranked solo-operator recommendation. Skeptical framing; research date 2026-04-19. Useful as context on why the Kalshi fair-value strategy was selected over alternatives.
- **`prediction_market_arbitrage_video_breakdown_and_opinion.md`** — breakdown + honest opinion on a YouTube cross-venue arbitrage video (`zAEFF6qDSLk`), calling out sales-pitch framing and naming the real failure modes (resolution-rule mismatch the biggest). Relevant here as the resolution-rule lesson directly informs the Kalshi scanner's handling of CF Benchmarks resolution mechanics.

A handful of earlier research docs were removed during the 2026-04-19 repo cleanup. The strategy and execution plans may still reference those filenames; treat any such link as archival (git history only) and do not recreate the docs unprompted.

When extending any doc, keep the existing tone and section structure.

## Load-bearing findings (use these when answering)

These are the findings most likely to be wrong in older docs or in assumptions a user brings in. Flag them when relevant.

1. **Kalshi 15-min crypto markets resolve to CF Benchmarks Real-Time Indices** (Source Agency: CF Benchmarks; BTC uses BRTI, ETH and SOL use their respective CF RTIs). Not a single spot exchange. Series tickers appear to follow `KXBTC15M` / `KXETH15M` / `KXSOL15M` but must be verified at runtime against Kalshi's `/events` endpoint. Resolution uses a 60-second simple-average window ending at expiry; the payout is a threshold binary (`above | below | between | exactly | at least` a strike). Missing or incomplete data at expiry resolves affected strikes to **No**. Cross-check any resolution-related claim against the contract-terms PDF (`CRYPTO15M.pdf`).
2. **This is not arbitrage — it's statistical fair-value.** Kalshi 15-min markets are **binary contracts with real resolution risk**. The only structurally capturable individual edge is **near-expiry** (the last 30–60s of each 15-min window, when the averaging window is partly observed). Mid-window pricing is market-maker territory; professional MMs price against the CF Benchmarks Real-Time Index in real time. Solo edge is feed-lag, not continuous alpha.
3. **Kalshi is a CFTC-regulated US DCM.** KYC is mandatory. Regulatory, tax, and jurisdictional posture is not generic-prediction-market — never assume conventions from other prediction venues apply. Confirm eligibility for the user's state before any advice that implies live trading.
4. **Realistic latency floor** on AWS us-east-1 on a t3.small / small Fargate task is low-single-digit milliseconds. Good enough to hit stale quotes sometimes; **not** good enough to win every race against colocated MMs. Any latency-dependent framing should internalize this ceiling.
5. **Expectation anchor** from the scanner plan: if it works, realistic returns are 10–30% annualized on deployed capital, capacity-capped in the low five figures. The strategy is gated behind paper-trading measurement before live capital.

## Conventions when editing docs

- **Date-stamp web-sourced claims.** Use `**Research date:** YYYY-MM-DD` near the top or inline next to specific claims — the regulatory and fee picture moves fast, and readers need to spot staleness.
- **Cite sources at the bottom** of docs that pull from the web. Markdown-link format.
- **Cross-link rather than duplicate.** If content belongs in another doc in this folder, link to it and write a one-line summary here rather than copying.
- **Not legal advice.** Any doc touching jurisdiction, KYC, tax, or regulated-venue eligibility should say so explicitly.
- **Avoid emojis unless the user asks.** Existing docs use `⚠️` sparingly for warnings; don't add more without reason.

## Signup / platform links (so future Codex doesn't guess)

- Kalshi app: [kalshi.com](https://kalshi.com)
- Kalshi API docs: [docs.kalshi.com](https://docs.kalshi.com)
- Kalshi 15-min crypto contract terms (authoritative resolution-rule source): [kalshi-public-docs.s3.amazonaws.com/contract_terms/CRYPTO15M.pdf](https://kalshi-public-docs.s3.amazonaws.com/contract_terms/CRYPTO15M.pdf)
- CF Benchmarks (Source Agency for Kalshi crypto resolution): [cfbenchmarks.com](https://www.cfbenchmarks.com)
- CF Bitcoin Real-Time Index (BRTI): [cfbenchmarks.com/data/indices/BRTI](https://www.cfbenchmarks.com/data/indices/BRTI)

## Research work — common modes

Most user requests in this folder fall into a few shapes. Handle each accordingly:

- **"What does the research say about X?"** — search `docs/` first (`Grep` / `Read`). Answer from existing docs. If the answer isn't in the folder, web-research it, then offer to add a note or new doc.
- **"Write a doc on X."** — place in `docs/`. Follow the conventions above. Date-stamp, cite sources, cross-link existing docs.
- **"Update the doc on X."** — edit in place. If the change is material (fee schedule change, regulatory shift, new venue, resolution-rule clarification), update the `Research date` line.
- **"Is this still accurate?"** — check the research date. If older than ~a month for regulatory/fee material or ~a quarter for general market mechanics, web-research before answering.
- **"Implement X from the plan doc."** — code belongs in the sibling `ArbitrageTrader` repo, not here. Redirect and, if needed, `cd` into that repo to work.

Do not create code files (`.py`, `.sol`, configs, tests) in this repo — this is a research folder and the default output is Markdown under `docs/`.

## Implementation conventions

Code lives in this repo's `src/`, tests in `tests/`, scripts in `scripts/`, configs in `config/`, deploy in `deploy/`. Must-follow ground rules (Decimal-only, three-opt-in live gate, paper default, pre-commit ritual) live in `docs/kalshi_scanner_execution_plan.md` §1.

### Open architectural decisions (resolve before P1-M0 edits)

1. **Platform primitives (CircuitBreaker, RetryPolicy, BasePipeline, PriorityQueue).** Three options: (a) add the generic `trading_platform` repo as a submodule at `lib/trading_platform/` and wire through a local `src/platform_adapters.py` (matches the prior DEX-bot pattern — proven and decoupled); (b) fork a snapshot of those primitives into `src/lib/` so we own the code; (c) build lean standalone versions scoped to Kalshi. **Tentative default: (a) submodule.**
2. **Repo shell bootstrap.** The prior DEX implementation's `src/`, `tests/`, `scripts/`, `docker-compose.yml`, `pyproject.toml` are pending deletions in the working tree. Either (a) selectively restore from git history as a scaffold (Postgres service, migration script, test harness — drop DEX-specific modules) or (b) start fresh with a clean layout. **Tentative default: (a) selective restore** — the Postgres / migration / pytest / Dockerfile patterns are reusable and the DEX-specific files are easy to identify and drop.

Until these are resolved, P1-M0 tasks should not start modifying `src/` or `tests/`.
