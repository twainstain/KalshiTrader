# Kalshi MVE (multi-game-extended) combo-market research note

**Research date:** 2026-04-20.

This note follows up on [`kalshi_ideas_leader_analysis.md`](kalshi_ideas_leader_analysis.md) §4-Pattern-B: "Combo / multi-leg markets are systematically mispriced." The parent doc observed that `KXMVESPORTSMULTIGAMEEXTENDED` showed up in three of four winners' top-five P&L series and called for a separate research note. This is that note.

**Cross-links** — read for context:
- [`kalshi_ideas_leader_analysis.md`](kalshi_ideas_leader_analysis.md) — leader-tape scoring that surfaced the MVE pattern.
- [`kalshi_crypto_fair_value_scanner_plan.md`](kalshi_crypto_fair_value_scanner_plan.md) — Phase-1 scanner plan; this note is upstream of any "should we add an MVE-style strategy" decision (strategy C / structural bucket).
- [`kalshi_scanner_implementation_tasks.md`](kalshi_scanner_implementation_tasks.md) — task list; §7 of this note proposes additions.

Not legal advice; no authorization to trade. Sample window is ~5 months of Kalshi Ideas tapes (2025-11-27 → 2026-04-20); all profiles leaderboard-selected so survivorship bias is severe.

---

## 1. What MVE markets are (Kalshi authoritative)

**"MVE" = Multivariate Event**, Kalshi's internal term for **Combos** — the parlay-style product launched in 2025. All MVE tickers are prefixed `KXMVE…`. Confirmed against Kalshi's own docs ([Kalshi API changelog, Feb 12 2026](https://docs.kalshi.com/changelog) — "…multivariate event (KXMVE-prefixed) tickers").

Mechanics, verbatim from Kalshi sources:

- **Resolution:** "Combos allow you to trade custom combinations of events in a single position. Each combo is a unique market with its own dedicated order book… resolve based on the product of underlying positions, with payouts maxing out at $1.00 per contract." If all legs resolve yes → $1.00; if any leg resolves no → $0.00. A leg can also settle to an intermediate value (e.g., player DNP settles to last traded price), and the combo pays `product(leg_values)` (example from Kalshi help: 3-leg combo with legs $1.00 × $1.00 × $0.70 = $0.70 per contract).
- **Pricing mechanism: Request-For-Quote (RFQ), not continuous book.** Users submit quote requests; market-maker participants respond with prices; fills aren't guaranteed — if no one quotes, the order doesn't execute ([Kalshi Combos help](https://help.kalshi.com/en/articles/13823820-combos), [Market FAQs](https://help.kalshi.com/en/articles/13823821-market-faqs)).
- **Finality:** "Once placed and filled, combos cannot be canceled."
- **Settlement timing:** 1–12 hours after the last underlying position resolves (not at the moment of final leg resolution).
- **API surface:** `GET /events/multivariate` ([API reference](https://docs.kalshi.com/api-reference/events/get-multivariate-events)); real-time lifecycle via the `multivariate_market_lifecycle` WebSocket channel (added Mar 19 2026); MVE markets are **excluded** from `market_lifecycle_v2` as of Feb 12 2026. Events carry `collection_ticker` / `series_ticker` / `strike_date` / `mutually_exclusive` / `product_metadata` fields, and per-leg breakdowns are available in RFQ metadata (added Sep 15 2025).
- **Coverage:** Initially NFL/NBA at launch; now expanded to NFL, NBA, college football, college basketball, MLB, NHL, soccer, tennis, esports, and cross-category combinations.

Two MVE sub-series appear in our pulled data:

| Series ticker | UI name | Settled in DB | Yes-resolution rate | Fills in our tape |
| :--- | :--- | ---: | ---: | ---: |
| `KXMVESPORTSMULTIGAMEEXTENDED` | "MVE Sport Multi Game" ([sample market](https://kalshi.com/markets/kxmvesportsmultigameextended/mve-sport-mutli-game/kxmvesportsmultigameextended-s2025ff5029f07e7)) | 124 | 35.5% (44/124) | 1,103 |
| `KXMVECROSSCATEGORY` | "MVE Cross Category" ([sample market](https://kalshi.com/markets/kxmvecrosscategory/mve-cross-category/kxmvecrosscategory-s2026fef03399c12)) | 65 | 30.8% (20/65) | 932 |

Kalshi also lists a sport-specific variant (`KXMVENBAMULTIGAMEEXTENDED` — "MVE NBA Multi Game") that isn't in our operator tapes but is visible on the public market list.

Each MVE ticker is **one binary market** (confirmed: 189 settled MVE markets in DB, 189 distinct `event_ticker`s, 1:1 mapping). So an MVE is **a single yes/no contract whose yes condition is a product of 2+ underlying leg outcomes** — not a strike-spread family. The per-leg decomposition lives in RFQ metadata that we don't yet ingest.

Listings per day in our window: ~0.85 esports MVE settlements/day, ~0.45 cross-category/day.

## 2. Headline P&L: the edge is in MVE_ESPORTS, not MVE_CROSS

Rebuilding each profile's MVE exposure against Kalshi-settled outcomes (same methodology as the parent doc — `taker_side`/`taker_action`/`maker_action` reconstruction; gross of fees):

| Profile | Series | Fills | Settled | Settled risk | P&L | Return | Avg entry | Hit rate |
| :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| warrenh3 | MVE_ESPORTS | 672 | 115 | $283,608 | **+$157,110** | **+55.4%** | 17.1¢ | 34.8% |
| ColeBartiromoDOLLARSCHOLAR | MVE_ESPORTS | 258 | 12 | $37,011 | +$49,473 | +133.7% ¹ | 37.7¢ | 100% ¹ |
| moonmoon | MVE_ESPORTS | 103 | 87 | $216,374 | +$29,478 | +13.6% | 44.7¢ | 41.4% |
| flag.cheek | MVE_ESPORTS | 61 | 58 | $11,149 | +$1,929 | +17.3% | 9.0¢ | 12.1% |
| far.bike | MVE_ESPORTS | 9 | 8 | $5,756 | +$2,494 | +43.3% | 10.4¢ | 12.5% |
| warrenh3 | MVE_CROSS | 734 | 77 | $146,492 | **−$21,652** | **−14.8%** | 15.3¢ | 15.6% |
| moonmoon | MVE_CROSS | 37 | 36 | $90,817 | +$1,011 | +1.1% | 47.1¢ | 47.2% |
| flag.cheek | MVE_CROSS | 26 | 26 | $3,133 | +$3,678 | +117.4% ² | 13.3¢ | 26.9% |
| far.bike | MVE_CROSS | 9 | 8 | $5,073 | −$2,310 | −45.5% ² | 14.0¢ | 12.5% |
| Cole | MVE_CROSS | 126 | 0 | — | — | (all open) | 40.2¢ | — |

¹ Cole's esports sample is 12 settled — tape is mostly open. Headline ignore-worthy.
² flag.cheek and far.bike MVE_CROSS samples are <30 settled fills — noise-band.

**Aggregate settled numbers** (strips out Cole's unsettled):

| Series | Settled risk | Gross P&L | Gross return |
| :--- | ---: | ---: | ---: |
| MVE_ESPORTS | ~$554k | ~+$240k | **+43%** |
| MVE_CROSS | ~$246k | **~−$19k** | **−8%** |

**Two different markets.** MVE_ESPORTS pays 5-of-5 profiles net-positive. MVE_CROSS is negative in aggregate across the same operators using the same longshot playbook. **The "combo markets are mispriced" generalization from the parent doc is too broad — only the esports variant has persistent edge in this sample.**

## 3. Where the edge lives: yes-side longshot buys only

Splitting by side (yes-buy vs no-buy, corrected for opposite-leg-cross):

| Series | Side | Fills | Settled | Risk | P&L | Return | Avg entry |
| :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: |
| MVE_ESPORTS | **YES-buy** | 1,085 | 277 | $528,849 | **+$251,954** | **+47.6%** | 23.9¢ |
| MVE_ESPORTS | NO-buy | 18 | 3 | $25,049 | −$11,470 | (tiny) | 28.1¢ |
| MVE_CROSS | YES-buy | 928 | 146 | $245,168 | **−$20,690** | **−8.4%** | 19.7¢ |
| MVE_CROSS | NO-buy | 4 | 1 | $348 | +$1,416 | (tiny) | 61.0¢ |

**The signal is unambiguous on MVE_ESPORTS yes-buys.** Operators buy yes at 24¢ average (market-implied probability 24%); actual base rate across all 124 settled esports MVEs is **35.5%**. Expected return under random selection: (35.5 − 24) / 24 = **+47.9%**. Observed on 277 settled fills: **+47.6%**. The match is within sampling error, which means:

1. The edge is **the base rate itself**, not operator selection skill. Anyone buying yes at 24¢ on a random MVE_ESPORTS listing over this window captured the same return.
2. **The market persistently underprices yes on MVE_ESPORTS by ~11 points of implied probability.**
3. Selection does not seem to add; we don't see individual operators beating the base-rate expected return meaningfully (warrenh3 at 55.4% is above but he's also picking at 17¢ entries, where the same base-rate calc gives (35.5 − 17.1)/17.1 = **+108%** — he's *underperforming* the base-rate EV on his lower-priced entries).

Contrast MVE_CROSS: avg yes entry 19.7¢ (implied 19.7%), actual base rate 30.8%. Base-rate EV = **+56%**. Observed: **−8.4%**. Here there **is** negative selection — the subset of CROSS markets the tracked operators pile into resolves yes at a much lower rate than the full population. Warrenh3's 15.6% MVE_CROSS hit rate against a 30.8% population rate is the clearest case.

## 4. Why this might be — hypotheses, not conclusions

We don't have per-leg decomposition (each MVE event collapses to one market ticker in our DB), so the following are hypotheses to test, not claims.

### H1. Positive-correlation mispricing on esports combos

Combo markets that multiply individual-leg probabilities under **independence** systematically underprice yes when legs are positively correlated. In esports (same tournament, same day, similar team strength distributions, same meta/patch), positive correlation across legs is plausible — a favorite meta or dominant team pushes all legs in the same direction. If the Kalshi market-maker prices at `prod(p_leg)` but true joint is `prod(p_leg) + ρ·tail_mass`, yes is chronically cheap.

### H2. Thin liquidity + low-priority market-maker attention

MVE markets are low-volume compared to NBA/NFL/tennis. If the Kalshi MM models legs with generic priors (rather than per-event analyst input), the longshot tail gets coarse-approximated and left cheap. This is consistent with **esports** (small specialist audience, low MM attention) showing edge while **cross-category** (probably popular events mashed together, more eyeballs) does not.

### H3. MVE_CROSS is structurally different

The CROSS suffix suggests the legs span unrelated markets (e.g., a sports leg + a weather leg). Under actual independence, yes is not underpriced — it's priced roughly correctly. The −8.4% operator return on CROSS suggests the same longshot-yes template *doesn't* generalize. If H3 is right, **MVE_CROSS is not the opportunity; MVE_ESPORTS is — and any crypto analogue would depend on whether the legs are correlated.**

### H4. Resolution-rule ambiguity paid to yes

If MVE resolution rules have edge cases that resolve yes on ambiguity (analogous to `CRYPTO15M`'s no-data-resolves-no tail), that bias would show up only in esports where the underlying data providers are messier. We haven't pulled the MVE contract-terms PDF; this is speculation. Flagged for follow-up.

### H5 (most likely, added after Kalshi-doc review). RFQ responders price independence; operators pick correlated-legs combos

Now that we know MVE is RFQ-priced — a small number of market-makers quote against an algorithmically generated product of leg probabilities — the 11-point esports yes-underpricing is consistent with RFQ responders pricing legs as if independent. Whoever builds the combo picks which legs go in it (the combo builder is user-driven per [Kalshi Combos](https://help.kalshi.com/en/articles/13823820-combos)). If a builder picks esports legs that are positively correlated (same tournament/meta), they know `joint_p > prod(p_leg)` while the RFQ quoter does not.

This reframes the "edge" substantially:
- **It's not market-wide mispricing; it's adversarial combo construction** against an RFQ quoter using a simpler joint-probability model than the builder.
- **The builder's edge is in the selection of legs**, which our previous "operators don't add selection skill" reading missed — they added selection skill at construction time, but the constructed market looks homogeneous to us because every combo is a single ticker.
- **If Kalshi's RFQ engine upgrades its correlation model**, this edge closes overnight. It's fragile to platform changes, not just to new entrants.
- **MVE_CROSS underperforms because cross-category legs are ~genuinely independent** — `prod(p_leg)` is close to correct and there's nothing to harvest. Consistent with observed -8.4% on that subset.

## 5. Fees and position-sizing caveats

Quadratic fees (`round(0.07 × count × p × (1 − p))` per side per fill) hurt mid-priced binaries more than tail-priced ones:
- at 24¢: fee ≈ 0.07 × 0.24 × 0.76 = **1.28¢ per contract per side**, = 5.3% of notional round-trip
- at 9¢: fee ≈ 0.07 × 0.09 × 0.91 = 0.57¢, = 6.3% of notional round-trip (but on a higher-convexity bet)

For MVE_ESPORTS yes-buys at 24¢, fees eat ~5% of round-trip notional. On +47.6% gross the **net is ~+42%**, still large. For warrenh3's 17¢ entries (avg book size unknown but small per-fill), fees are proportionally similar. **The edge survives fees in this sample window by a wide margin**, but only because of the headline gross number.

Position accountability from `CRYPTO15M.pdf` ($25k per-market) does not directly apply — MVE is a separate series — but Kalshi's general position limits should be verified before any scaling discussion.

## 6. Capacity estimate

- 124 esports MVEs settled in ~145 days = **~0.85 markets/day**.
- Top operator (warrenh3) risks ~$2.5k per fill and fills 4-5 times per typical market — rough sizing ceiling we observed. He deployed $2.8M total risk into MVE_ESPORTS over the window and pulled +$157k.
- If the 11-point yes underpricing persists, realistic solo-operator capacity is **five-figure annual gross P&L** on a low-five-figure daily bankroll. Capacity is capped by MVE listing cadence, not by own-book depth.
- Sample-size caveat: 277 settled yes-buys. The 35.5% base rate has a ~95% CI of roughly ±8 points at this N. The edge could shrink to ~3 points on longer sample.

## 7. Recommendations

Each item here is concrete enough to drop into `kalshi_scanner_implementation_tasks.md` as a Phase-2 task or research task.

1. **Do not build an MVE strategy inside Phase 1.** Phase 1 is the BTC15M / crypto-15m scanner. MVE is adjacent and the Phase-1 gate is the gate that must clear before *anything* else gets funded. Note it as a Phase-2 candidate, not a Phase-1 distraction.
2. **Before touching MVE with any code, pull the contract-terms PDF** for MVE series (analogue to `CRYPTO15M.pdf` for crypto). We're reasoning about pricing without knowing the actual leg structure, which is weak. Task: `fetch MVE contract terms from Kalshi public S3 or docs and drop into docs/`.
3. **Extend the historical-markets ingest to capture event-level `raw_json`** — our current pull has empty `raw_json` for MVE markets, so we can't see leg composition, leg count, or per-leg strike. That's the biggest gap in this analysis.
4. **Run a null-strategy backtest:** "buy yes at any MVE_ESPORTS open at or below 30¢, hold to settlement" over the 124 settled markets. If the return clusters around +47%, H1 is supported and operator selection adds nothing — which would mean the strategy is literally base-rate arbitrage, not model-based.
5. **Survey Kalshi for a crypto-MVE analogue.** If Kalshi lists anything like `KXBTC-ETH-JOINT` or strike-spread multi-leg baskets on CF-Benchmarks-resolved crypto, the same independence-mispricing hypothesis might apply and would fall directly in this repo's scope. Check `/trade-api/v2/series` for `KX*MVE*` or `*COMBO*`.
6. **Do not adopt MVE_CROSS as part of this claim.** The parent doc implied it by grouping "combo markets"; our data shows CROSS loses money at the hands of the same operators running the same playbook. Fence the recommendation to `KXMVESPORTSMULTIGAMEEXTENDED` explicitly.
7. **Track `KXMVESPORTSMULTIGAMEEXTENDED` base rate over time** as a monitoring signal. If the yes-rate converges toward the implied-price rate (i.e., the mispricing closes), the edge is gone. This is cheap: a weekly batch job pulling new settlements against existing series. Good "hit by MMs" canary.

## 8. Open questions / next pulls

1. **What is the actual leg structure of an MVE esports event?** (scrape a listing page or the contract PDF, not just the ticker).
2. **What ρ (cross-leg correlation) would explain a 11-point mispricing?** Back-of-envelope: if legs are AA with marginal p = 0.7 and true joint = 0.35 vs independent 0.7² = 0.49, that implies ρ ≈ −0.3 — which is wrong sign for H1. If joint = 0.35 and independent = 0.24 (as the market prices), then ρ ≈ +0.2, consistent. Need leg count to finalize.
3. **Fee parameters per MVE market** — caching market-level `fee_type`/`fee_multiplier` would let us replace the quadratic approximation with per-market truth.
4. **Does the base-rate edge survive out-of-sample?** Re-run this analysis monthly for 3 months. If it persists, it's a listing-process structural feature. If it decays, it was a sample-window artifact.
5. **Is there a seller-side edge in MVE_CROSS?** No-buys are a tiny sample (4 settled), but if MVE_CROSS yes is systematically overpriced at 19.7¢ with 30.8% true rate on *unselected* tapes, passive no-makers could capture the other side. Would need a wider population pull to test.

---

## Sources

### Kalshi official (authoritative — fetched 2026-04-20)

- [Kalshi Combos help article](https://help.kalshi.com/en/articles/13823820-combos) — product/resolution/RFQ description, player-DNP handling, settlement timing.
- [Kalshi Market FAQs](https://help.kalshi.com/en/articles/13823821-market-faqs) — combo processing via RFQ, settlement review after underlying events finish.
- [Kalshi API changelog](https://docs.kalshi.com/changelog) — explicit `KXMVE-prefixed` terminology (Feb 12 2026); `multivariate_market_lifecycle` WS channel (Mar 19 2026); `occurrence_datetime` (Apr 16 2026); RFQ per-leg metadata (Sep 15 2025); initial MVE API (Nov 6 2025).
- [GET /events/multivariate API reference](https://docs.kalshi.com/api-reference/events/get-multivariate-events) — endpoint, params (`series_ticker`, `collection_ticker`, `with_nested_markets`), response fields (`mutually_exclusive`, `strike_date`, `product_metadata`).
- [Sample KXMVESPORTSMULTIGAMEEXTENDED market page](https://kalshi.com/markets/kxmvesportsmultigameextended/mve-sport-mutli-game/kxmvesportsmultigameextended-s2025ff5029f07e7).
- [Sample KXMVECROSSCATEGORY market page](https://kalshi.com/markets/kxmvecrosscategory/mve-cross-category/kxmvecrosscategory-s2026fef03399c12).
- [Sample KXMVENBAMULTIGAMEEXTENDED market page](https://kalshi.com/markets/kxmvenbamultigameextended/mve-nba-multi-game) — sport-specific variant not in our tape.
- Kalshi Trade API (settlements, public): `https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}`.

### Third-party reference

- [SportsGrid — Kalshi Combos vs parlays](https://www.sportsgrid.com/prediction-market/what-are-kalshi-combos) — CFTC-regulated vs gaming-regulated distinction, always-available exit.
- [RotoGrinders — Kalshi Combos full rollout](https://rotogrinders.com/articles/kalshi-fully-launches-combos-feature-for-parlay-style-prediction-markets-4177461) — launch context.

### Repo-internal

- `data/kalshi.db` — tables `kalshi_ideas_trades` (2,035 MVE fills across 5 operators) and `kalshi_historical_markets` (189 settled MVE markets).
- Methodology: opposite-leg-cross reconstruction as in [`kalshi_ideas_leader_analysis.md`](kalshi_ideas_leader_analysis.md) §2.
- Kalshi fee schedule: `kalshi_scanner_execution_plan.md` §Kalshi pitfalls.
- [`CRYPTO15M.pdf`](https://kalshi-public-docs.s3.amazonaws.com/contract_terms/CRYPTO15M.pdf) — referenced for resolution-mechanics analogy only; MVE contract-terms PDF not yet located (Kalshi's combos are defined via product-rule settlement in the help article rather than a per-series PDF).
