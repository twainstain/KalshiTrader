# KalshiTrader

Kalshi crypto fair-value scanner — a research-grade scanner that measures the timing lag between CF Benchmarks reference prices and the Kalshi 15-min BTC / ETH / SOL orderbook, and, if the edge proves out, trades it at small size.

Built on top of the generic [`trading-platform`](https://github.com/twainstain/trading-platform) primitives (CircuitBreaker, RetryPolicy, BasePipeline, PriorityQueue) via a git submodule at `lib/trading_platform/`.

> Not investment, legal, or tax advice. See `docs/` for the research disclaimers and the CFTC-regulated-venue notes.

## Structure

Two serial phases with an explicit gate in between:

- **Phase 1 — Scanner / Feasibility Research (zero money at risk).** Collect historical + live Kalshi data; collect our basket-of-exchanges reference ticks; run a live **shadow evaluator** that scores hypothetical decisions to a DB **without submitting any orders**; produce a feasibility report with measured lag, realized-if-traded edge, hit rate, and capacity.
- **Phase 1 → Phase 2 Gate.** Pre-committed thresholds. No Phase-2 work begins without a pass.
- **Phase 2 — Execution.** Risk rules, paper executor, live executor (three-opt-in gated), custom dashboard with live-account API calls, paper-in-prod for 4 weeks, live small-size for 2 weeks, then stepped scale.

## Where things are

- **`docs/`** — strategy plan, execution plan, per-task runbook, research reports.
  1. [`docs/kalshi_crypto_fair_value_scanner_plan.md`](./docs/kalshi_crypto_fair_value_scanner_plan.md) — strategy & thesis (read first).
  2. [`docs/kalshi_scanner_execution_plan.md`](./docs/kalshi_scanner_execution_plan.md) — architecture, platform conventions, milestone walkthrough.
  3. [`docs/kalshi_scanner_implementation_tasks.md`](./docs/kalshi_scanner_implementation_tasks.md) — status-tracked task list (P1-M0 through P2-M6).
- **`src/`** — Kalshi-specific Python code (Phase-1 modules first; Phase-2 follows after the feasibility gate). _Not yet bootstrapped._
- **`tests/`** — pytest suite. _Not yet bootstrapped._
- **`lib/trading_platform/`** — generic platform primitives (git submodule). _Not yet added._
- **`CLAUDE.md`** — orientation doc for future Claude Code sessions, including open architectural decisions pending before code lands.

## Quick status (2026-04-19)

Pre-implementation. Docs complete, P1-M0 pending. See `docs/kalshi_scanner_implementation_tasks.md` § "Progress summary".
