# Kalshi Ideas leader-tape analysis (Apr 2026)

**Research date:** 2026-04-20.

This is a working note summarizing what we learned by pulling and scoring nine top Kalshi Ideas profiles' tapes against actual market settlements, with the explicit goal of asking: **does anything they do transfer to the Phase 1 crypto fair-value scanner**, and **what should we deliberately not copy?**

**Cross-links** — read these for context:
- [`kalshi_crypto_fair_value_scanner_plan.md`](kalshi_crypto_fair_value_scanner_plan.md) — strategy doc; load-bearing finding #2 (statistical fair-value, not arbitrage; near-expiry is the only structural edge) is directly tested here.
- [`kalshi_scanner_execution_plan.md`](kalshi_scanner_execution_plan.md) — architecture / how-to.
- [`kalshi_scanner_implementation_tasks.md`](kalshi_scanner_implementation_tasks.md) — task list; this note is upstream of any "should we add an X strategy" decision.

This note is **not legal advice** and does **not** authorize trading any strategy described. It scores other people's public tapes; sample windows are short (days, not months) and survivorship bias is severe — we only see profitable users by definition.

---

## 1. What was pulled

Kalshi exposes per-profile trades and metrics through its "Ideas" social surface. The pull stack is `scripts/kalshi_ideas_pull.py`, which now supports `--leaderboard-time-period {daily,weekly,monthly,all_time}` (a single repeatable flag); see also `scripts/migrate_db.py` for the four backing tables.

**Leaderboard windows captured** (top-N per category):
- `monthly` × `profits` × top 20
- `monthly` × `num_markets_traded` × top 20
- `weekly` × `profits` × top 20
- `weekly` × `num_markets_traded` × top 20
- `daily` × `profits` × top 20
- `daily` × `num_markets_traded` × top 20

**Profiles with full trade tape pulled (nine):**

| Profile | Best LB rank captured | Trades pulled | Window |
| :--- | ---: | ---: | --- |
| `best.gun2` | monthly profits #29 ($693k) | 3,152 | 2025-05-25 → 2026-04-13 |
| `ColeBartiromoDOLLARSCHOLAR` | monthly profits #70 ($296k) | 25,027 | 2026-03-26 → 2026-04-20 |
| `vica` | weekly profits #8 ($47.9k) | 10,035 | 2026-03-24 → 2026-04-20 |
| `cust` | weekly num_markets #9 (1,575) | 10,000 | 2026-02-28 → 2026-04-20 |
| `warrenh3` | weekly profits #26 ($25.0k) | 4,489 | 2026-02-07 → 2026-04-20 |
| `flag.cheek` | weekly profits #24 ($27.4k) | 3,840 | 2026-04-05 → 2026-04-20 |
| `far.bike` | daily profits #21 ($28.0k) | 1,020 | (recent week) |
| `moonmoon` | weekly profits #23 ($27.7k) | 315 | 2025-10-06 → 2026-04-20 |
| `weatherman.allday` | weekly num_markets #4 (2,926) | 25,128 | 2026-04-14 → 2026-04-20 |

Total: 53,153+ unique trade rows, 670 settled tickers (settlements pulled via the public `GET /trade-api/v2/markets/{ticker}` endpoint, sequentially with 100ms sleep — 5.5 req/s sustained, no 429s).

## 2. How realized P&L is computed

For each fill we reconstruct the user's actual position from Kalshi's `taker_side`/`taker_action`/`maker_action` fields, including the **opposite-leg-cross convention** (when `taker_action == maker_action`, the maker traded the complement leg at `100 − price`). Per fill:

- `pnl = (payout − price) × sign × count`
- `payout = $1.00` if the side held matches `settled_result`, else `$0.00`
- `sign = +1` for buys, `−1` for sells (closes)
- `risk = price × count`

This is **gross of fees**. Kalshi's quadratic fee schedule (`fee = round(0.07 × count × p × (1 − p))` per side per fill) is material: a typical 50¢ binary fill carries ~1.75% taker fee, so a +5% gross return on $200k is roughly +1% net. Net-of-fees is reported per profile only where the gross return is large enough that the fee correction matters to the conclusion.

## 3. Profile-by-profile findings

### 3.1 `best.gun2` — concentrated cross-sport directional

| | |
| :--- | :--- |
| Window | 2025-05-25 → 2026-04-13 (~11 months) |
| Distinct markets | 94 |
| Settled% | 91% |
| $ risked | $13.2M |
| Realized P&L | **+$1.71M (+14.1%)** |
| Maker / taker | 12% / 88% |
| Median fills per (ticker, minute) | 4, max 68 |

**Where the money came from** (top series by P&L): NBA totals +54%, NHL games +36%, MotoGP Women +29%, Indian Wells Men +27%, NBA games +24%, ATP matches +66%.

**Execution shape:** ~99% of fills are opens (buys); only 70 of 3,152 are sells. He picks a small set of games per week, sweeps the book to a single limit price within a one-minute window (the `KXNBAGAME-26APR07CHABOS-CHA` ticket: 22 fills at the same minute, all at 36¢), and holds to settlement. Average yes entry 53¢, no entry 52¢ — coinflip pricing, edge comes from **game selection**, not from price-improvement. No detectable lag-scalping behaviour.

**Single hot week (Apr 1–7, 2026)** scored independently from the tape: 5 wins / 5 losses, +$406k gross / +$343k net of estimated fees on $1.91M risked. Hit rate 50%; average win +$201k vs average loss −$120k → **asymmetric payoff, not directional accuracy**, is the engine.

### 3.2 `ColeBartiromoDOLLARSCHOLAR` — tennis specialist + maker

| | |
| :--- | :--- |
| Window | ~3 weeks |
| Distinct markets | 838 |
| Settled% | 24% |
| $ risked | $10.2M |
| Realized P&L | +$773k (**+24.2%**) on settled subset |
| Maker / taker | **80% / 20%** |
| Median fills per (ticker, minute) | 2, max 99 |

**Where the money came from**: ATP Match +$554k (+36%), WTA Match +$178k (+13%), Government Shutdown Length +$127k (+52%), ATP Challenger +$128k (+6%).

**Execution shape:** This is the **only profile in the sample that is a passive maker by construction** (80% of his fills he was the resting order). Average yes entry 34¢ / no entry 89¢ — both are the same trade economically (heavy-favorite fade). He sits resting bids cheap on the underdog side and lets takers cross him.

In-tape closed-position P&L is **negative** (−$76k on 94 round-trips) but **resolution P&L** more than recovers it. So Cole loses small on the scalps and wins on the bets that resolve in his favor — confirms a **value-dog selection model**, not a market-making rebate strategy.

### 3.3 `far.bike` — European-soccer pure specialist

| | |
| :--- | :--- |
| Window | ~1 week |
| Distinct markets | 115 |
| Settled% | **97%** |
| $ risked | $275k |
| Realized P&L | +$69k (**+25.5%**) |
| Maker / taker | 0% / 100% |
| Median fills per (ticker, minute) | 5, max 45 |

**Where the money came from**: UEL game +105%, Bundesliga +245%, AFC Champions League +226%, Denmark Superliga +100%, EPL +80%, UEL total +103%.

**Highest-confidence signal in the sample** because settled% is 97% (almost everything resolved inside our window). Pure taker, sweeps book at a target price, holds to whistle. Uniformly positive across multiple leagues — not one lucky tournament. **Domain expertise in European football, then directional bets at favourable prices.** No timing edge, no scalping, no maker rebates.

### 3.4 `warrenh3` — esports-combo + NCAAB longshot specialist

| | |
| :--- | :--- |
| Window | ~10 weeks |
| Distinct markets | 1,606 |
| Settled% | 14% |
| $ risked | $1.94M |
| Realized P&L | +$280k (**+39.6%**) on settled subset |
| Maker / taker | 0% / 100% |
| Median fills per (ticker, minute) | 1, max 28 |
| Avg yes entry | **14.5¢** |

**Where the money came from**: KXMVESPORTSMULTIGAMEEXTENDED (esports multi-game extended) +$180k (+26%), NCAAB total +$44k (+40%), MLB game +$22k (+61%), NCAAB first-10-of-game +$12k (+69%), WTA match +$11k (+54%).

**Execution shape:** Wide market coverage (1,606 distinct), tiny per-bet sizes, average yes entry 14.5¢ — he's **buying long-tail "longshot Yes" contracts** across many markets and getting paid when even a small fraction hit. The +39.6% return is on only 14% of the tape settled; the unsettled $1.7M is open positions whose distribution we cannot yet evaluate, so the headline number has high variance bands.

### 3.5 `vica` — tennis-favorites long-only

| | |
| :--- | :--- |
| Window | ~4 weeks |
| Distinct markets | 584 |
| Settled% | 34% |
| $ risked | $4.93M |
| Realized P&L | +$160k (**+8.9%**) on settled subset |
| Maker / taker | 0% / 100% |
| Avg yes entry | **78¢** |

**Where the money came from**: ATP Challenger +$123k (+5%), WTA Match +$38k (+6%).

**Execution shape:** 9,294 of 9,998 fills are yes-buys, very few closes. Average yes entry 78¢ — **buys favorites in tennis matches**, big sizing, holds to result. The opposite of `warrenh3`'s longshot pattern. Returns are tighter (~5–6%) because favorites compound less per win.

### 3.6 `moonmoon` — selective MVE-combo + MLB

| | |
| :--- | :--- |
| Window | ~6 months but only 315 fills |
| Distinct markets | 175 |
| Settled% | 39% |
| $ risked | $352k |
| Realized P&L | +$31k (+10.1%) on settled subset |

Tiny tape, but consistent: KXMVESPORTSMULTIGAMEEXTENDED +$29k (+13%) carries the result. Same combo-market thesis as `warrenh3`. Highly selective entries — averages 1.8 fills per market.

### 3.7 `cust` — high-volume NBA-prop scalper

| | |
| :--- | :--- |
| Window | ~7 weeks |
| Distinct markets | 3,632 |
| Settled% | 17% |
| $ risked | $617k |
| Realized P&L | +$2k (**+0.7%**) on settled subset |
| Maker / taker | **81% / 19%** |
| Median fills per (ticker, minute) | 1, max 104 |

10,000 fills / 3,632 markets = thin per-market exposure. Top series: NBA player points, UFC method-of-victory, NBA spread/total, MLB home runs. **Two-way maker book, ~breakeven** — UFC MOV +27% is the only consistently positive series, everything else cancels. Rebates probably push him slightly net-positive but the strategy has no measurable selection edge.

### 3.8 `flag.cheek` — BTC15M scalp + concentrated IPL → losing in-window

| | |
| :--- | :--- |
| Window | 2026-04-14 → 2026-04-20 (~6 days) |
| Distinct markets | 150 |
| Settled% | **100%** |
| $ risked | $351k |
| Realized P&L | **−$121k (−34.5%)** |
| Maker / taker | 0% / 100% |
| Median fills per (ticker, minute) | 4, max 122 |

**This is the most important profile for Phase 1** because 58% of his risk and 82% of his fills are in `KXBTC15M` — the exact crypto-15-minute binary the scanner targets.

**BTC15M-specific scoring (3,095 settled fills):**

- Win rate: 54%.
- Time-to-expiry distribution: p10 = 232s, **p50 = 751s (12 min before expiry)**, p99 = 880s. He's trading in the **first 3 minutes of the 15-minute window**, *not* in the last 60 seconds where the partial-average edge actually lives.
- Realized BTC15M P&L: +$2,827 / +1.4% gross. After estimated quadratic fees (~$3k–$4k for this volume), net is **break-even to slightly negative**.
- Distance |BTC − strike| at fill: near-strike (|gap|<$75) returns +5%, far-strike (>$200) is −20% to −28% — pure mechanical, no strategic edge per side.

**The −$121k headline comes from the IPL cricket position** ($103k risk on KXIPLGAME), not crypto. His weekly leaderboard reading of +$27k is from earlier weeks; this week he ate a sports-side loss.

### 3.9 `weatherman.allday` — penny tail-harvester (not in profits leaderboard)

| | |
| :--- | :--- |
| Distinct markets | 24,237 |
| Trades | 25,128 |
| Side mix | **98% no-buys at avg 3.5¢** |
| $ risked | ~$13k |

Sprays minimum-size buys across thousands of niche markets — MLB stat props, crypto dailies, weather, esports, entertainment — almost always buying NO at 1–5¢. Edge thesis is "law of large numbers on tail mispricings" plus, more concretely, **leaderboard farming of `num_markets_traded`** (he ranks #4 weekly on activity but is not in the profits top-100). Worth noting because it shows the leaderboard categories measure different things; the activity board is largely a marketing channel.

---

## 4. Cross-profile patterns

Five patterns repeat across the winners. Numbered by frequency × confidence.

### Pattern A — Domain specialization > breadth

`far.bike` (EU football), `Cole` (tennis), `vica` (ATP/WTA), `warrenh3` (esports combos + NCAAB) — each makes money in **one or two niches** and stops there. The two profiles that try to cover everything (`cust` ~breakeven, `weatherman.allday` not-on-profits) underperform. **The actionable claim is: a Phase 1 scanner that targets 7 crypto pairs is broad relative to what the profitable operators do.** That's not necessarily wrong — crypto is one asset class — but the precedent is that *narrow + selection edge* beats *wide + execution edge*.

### Pattern B — Combo / multi-leg markets are systematically mispriced

`KXMVESPORTSMULTIGAMEEXTENDED` (esports multi-game extended) appears as a positive contributor in **three of the four winners' top-five P&L series** (warrenh3 +26%, moonmoon +13%, Cole +12%). A fourth (best.gun2) doesn't trade them. Combo markets are common-knowledge harder to price than single events — the pricing implication of independence assumptions on small-sample legs is the most likely source. **Adoptable idea (with adaptation):** if a similar mispricing exists in crypto (e.g., BTC × ETH joint binaries, or strike-spread baskets at 15m), it would fall under our Strategy C (implied-vol / structural) bucket.

> **Follow-up:** see [`kalshi_mve_combo_market_research.md`](kalshi_mve_combo_market_research.md) (2026-04-20) — re-scored across all 5 operators and 189 settled MVE markets. The edge is concentrated in `KXMVESPORTSMULTIGAMEEXTENDED` (+43% gross aggregate, 5/5 profiles positive); the sibling series `KXMVECROSSCATEGORY` is flat-to-negative on the same playbook, so the generalization above is too broad. Yes-side is where the edge lives: market prices yes at ~24¢, actual settled-yes rate is 35.5% — operators capture the base-rate spread, not a selection edge.

### Pattern C — "Sweep to my price, then stop"

Every taker-mode profile (`best.gun2`, `far.bike`, `vica`, `flag.cheek`) shows the same execution shape: when they decide to enter, they sweep the visible book at a single (or near-single) limit price within seconds, then go silent. `best.gun2`'s `KXNBAGAME-26APR07CHABOS-CHA` did 22 fills at 36¢ in one minute, then stopped. `far.bike`'s p90 fills-per-minute is 17. **This is a pre-computed-fair-value ordering style, not a reactive one.** It's directly compatible with the Phase 1 design (calculate fair, post limit, sweep to that price) and validates that the architecture choice is realistic.

### Pattern D — Asymmetric P&L distribution dominates win-rate

`best.gun2`'s scored hot week was 5–5 with average win +$201k vs average loss −$120k. Cole's in-tape closed P&L was negative; resolution P&L recovered it. The pattern is **buy at prices that imply a probability below the operator's model**, accept frequent losses, count on the convex payoff of the hits. Win rate is a useless metric; what matters is `mean_win × p_win − mean_loss × (1 − p_win)`.

### Pattern E — Nobody is making money on `KXBTC15M`

Three profiles touched it: `flag.cheek` (3,145 fills, +1.4% gross / break-even net), `vica` (859 fills, +7.9% on $55k — tiny side bet), `cust` (203 fills, no meaningful exposure). **The single profile that built a strategy around BTC15M (`flag.cheek`) is at break-even on the crypto leg before fees.** This is direct evidence for `kalshi_crypto_fair_value_scanner_plan.md` load-bearing finding #2: the crypto 15M market is efficient at the timescales we observed (mid-window, multi-minute-to-expiry). We saw no operator extracting structural edge there.

---

## 5. What we should adopt for Phase 1

Each item below is concrete enough to drop into `kalshi_scanner_implementation_tasks.md` as a task or a research note.

1. **Adopt the "sweep to fair, stop" execution model.** It is what every successful taker-mode operator does, and it matches the existing Phase-2 paper-executor design. Don't build a price-following or quote-improving executor.
2. **Define an explicit "fair value below market price" entry filter, not a quote-staleness filter.** The winners enter when their model says the market is wrong, not when the book is slow. Our existing shadow evaluator is already structured this way; keep it that way.
3. **Make near-expiry the *only* sanctioned crypto entry window.** The single profile with deep BTC15M exposure trades at p50 = 12 min to expiry and earns nothing net. The scanner should *refuse* to emit a decision earlier than (e.g.) the last 60s, where the partial 60s averaging window of CF Benchmarks BRTI is observable and where the only structural edge is documented (`CRYPTO15M.pdf` §0.5; `kalshi_crypto_fair_value_scanner_plan.md` §A).
4. **Park combo / cross-strike binaries as a Phase-2 candidate — do not trade now.** Three winners profit from MVE (Kalshi's Combos / multivariate events; `KXMVE…` prefix, RFQ-priced). Follow-up work in [`kalshi_mve_combo_market_research.md`](kalshi_mve_combo_market_research.md) showed the edge is concentrated in `KXMVESPORTSMULTIGAMEEXTENDED` yes-buys (+47.6% gross on 277 settled fills, ≈ base-rate spread of 35.5% yes vs 24¢ implied) and that `KXMVECROSSCATEGORY` is flat-to-negative. The edge is likely an RFQ-quoter artifact pricing legs as independent when they're positively correlated — fragile to a model upgrade on Kalshi's side. Off-strategy for this repo, thin sample, no crypto analogue listed. Cheap follow-ups (null-strategy backtest, fetch RFQ per-leg metadata) are fine; do not commit capital.
5. **Score by P&L distribution, not win rate.** Update the feasibility-report KPIs (`kalshi_scanner_implementation_tasks.md` Phase 1→2 gate) to require a payoff-asymmetry threshold (`mean_win / mean_loss ≥ X`) in addition to hit rate. Otherwise the gate can be passed by a 60%-win-rate strategy that loses money.

## 6. What we should explicitly NOT adopt

1. **Don't build a high-fill-count BTC15M scalper at multi-minute time-to-expiry.** It is precisely `flag.cheek`'s playbook and it nets to ~zero before fees. Burns capital, generates fee load, generates no edge.
2. **Don't make win-rate a primary KPI.** It misled the early-tape read on `best.gun2` (looked like an underdog buyer, was actually a coinflip-priced buyer winning by payoff asymmetry).
3. **Don't pursue activity-volume leaderboards as a marketing or validation signal.** `weatherman.allday` is rank #4 weekly by `num_markets_traded` and not in the profits top-100. The `num_markets_traded` board measures something orthogonal to profitability.
4. **Don't extrapolate single-week returns.** `flag.cheek`'s leaderboard read is +$27k weekly, the in-tape settled P&L for the same window is −$121k. Either Kalshi's "projected_pnl" metric is a forward-looking mark on open positions (likely), or there's a window-alignment gap; either way, **single LB snapshots are not a reliable strategy proxy.**
5. **Don't generalize from this sample to Kalshi at large.** All nine profiles are leaderboard-selected — survivorship bias is severe. We have no read on the median user, the median strategy, or the long tail of losers.

## 7. Bottom line — does this analysis hand us a winning strategy?

**No.** Honest read-out after the MVE follow-up:

- **No drop-in strategy for Phase-1 scope emerged.** The nine profitable operators win in sports/tennis/esports domains where we have no edge, no data pipeline, and no scoped ambition to compete.
- **The most relevant finding is negative.** `flag.cheek` ran a high-fill `KXBTC15M` scalper at p50 = 12 min to expiry and netted ~zero before fees. Nobody in the sample makes money on crypto 15M mid-window. That is direct evidence the scanner's Phase-1 thesis (near-expiry-only is the only sanctioned window) is correctly scoped, and that the "easy" mid-window playbook is a trap we should not waste paper-mode cycles on.
- **The MVE esports edge is real but off-strategy** (see §5 item 4 and the MVE follow-up doc). We are not adopting it.
- **What the analysis does contribute** is *shape constraints* for the Phase-1 scanner — execution style (sweep-to-fair-then-stop), KPI framing (payoff asymmetry, not win-rate), and a negative guardrail against the mid-window playbook. These sharpen the scanner; they do not replace its feasibility test.
- **The crypto scanner still has to prove its own edge.** If near-expiry crypto doesn't show measurable edge in P1 paper runs, this repo does not have a winning strategy, and the Phase 1→2 gate fails by design.

## 8. Open questions / next pulls

1. Pull `--leaderboard-time-period all_time` and re-run this analysis. Long-term leaders will have settled tape going back months — much higher confidence in the per-series returns.
2. Pull the **top 20 weekly profits** profiles' tapes (we have only 8 of the 60+ leaderboard-listed profiles). Specifically `REAKT`, `Mr.Gondorff`, `kordet26`, `user.x`, `ClayA`, `Soarin`, `mikeedges`, `pelagictrading` — all monthly $300k+ that we haven't pulled.
3. Re-score `flag.cheek`'s BTC15M tape against the **sub-second `coinbase_trades` table** for the Apr 19–20 subset only. If a sub-second lag edge exists, the 1-min reference can't see it; the tick table can.
4. Build a simple "combo-market discovery" pass over `kalshi_historical_markets` to count distinct multi-leg series. Confirms whether the MVE pattern is unique to esports or generalizes.
5. Add a fee-net P&L column to all of section 3 once we cache market-level fee parameters (some markets are `flat`, most are `quadratic` with `fee_multiplier=1`).

---

## Sources

- Kalshi Ideas social endpoints (undocumented public surface):
  - `https://api.elections.kalshi.com/v1/social/leaderboard?metric_name={projected_pnl|num_markets_traded}&limit=N&time_period={daily|weekly|monthly|all_time}`
  - `https://api.elections.kalshi.com/v1/social/profile?nickname={slug}`
  - `https://api.elections.kalshi.com/v1/social/profile/metrics?nickname={slug}`
  - `https://api.elections.kalshi.com/v1/social/trades?nickname={slug}&page_size=N&cursor=...`
- Kalshi Trade API (settlements): `https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}` (public, unauthenticated).
- Reference price: Coinbase 1-minute candles in `reference_ticks` (asset=`btc`, src=`coinbase_1m_historical`); sub-second tick trades in `coinbase_trades` for Apr 19–20 only.
- Kalshi 15-min crypto contract terms: [`kalshi-public-docs.s3.amazonaws.com/contract_terms/CRYPTO15M.pdf`](https://kalshi-public-docs.s3.amazonaws.com/contract_terms/CRYPTO15M.pdf).
- Repo-internal:
  - `scripts/kalshi_ideas_pull.py` — leaderboard / profile / trade ingester.
  - `scripts/migrate_db.py` — schema for `kalshi_ideas_*` tables.
  - `tests/test_kalshi_ideas_pull.py` — coverage including the new `test_main_fans_out_across_time_periods` that exercises `weekly + monthly` together.
