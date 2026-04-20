# Crypto Arbitrage Feasibility Research

> **Research doc.** Not legal, tax, or investment advice. Verify any
> regulatory / fee claim against primary sources before acting.
> **Research date:** 2026-04-19. MEV market structure, fee schedules, and
> regulatory posture move fast — re-check anything older than a few
> weeks on fees / regulation, or a quarter on market mechanics.

## Context

Where does real profitable arbitrage edge still live in crypto markets
as of April 2026, and what does a "winning strategy" actually look like
in each category? This document covers **DEX-centric** categories first,
then **non-DEX / cross-venue** categories (CEX, perps, options,
stablecoins, prediction markets), and ends with a ranked
solo-operator recommendation.

## Honest Framing Up Front

Every category below has been professionalized. Most of the widely-quoted
"$X million of MEV" numbers are gross, pre-cost figures concentrated in
2–3 firms per niche. The solo-operator question is almost always *which
sub-niche is too small or too annoying for the top shops?*

Where a widely-cited figure could not be verified against primary or
semi-primary sources (notably the "$438K in 30 days Polymarket bot"
claim), it is flagged as unverified and not relied on.

---

## DEX-Centric Categories

### 1. Atomic cross-DEX arbitrage on a single chain

**State.** Atomic DEX↔DEX arb on Ethereum L1 is effectively a two-sided
auction for block space run through private orderflow channels. Over 90%
of arb transactions now route through private MEV-Boost bundles rather
than the public mempool, and two builders capture >90% of block auctions
as of early 2025. Searchers typically bid 80–95% of expected profit to
the builder/validator; the residual is thin. EigenPhi reported ~$3.37M
in 30-day atomic arbitrage profits (September 2025) — small relative to
volume competed for. An L1 atomic bot can spend ~132M gas across ~350
failed attempts per successful capture, making unprofitable overhead the
dominant cost. On L2s and alt-L1s (Base, Arbitrum, Optimism, BNB,
Polygon) builder concentration is lower and single-sequencer ordering
changes the game — that's where a solo operator still has a real shot.

**Accessible to solo?** L1 — no, unless vertically integrated with a
builder. L2s/alt-L1s — yes, margins thin.
**Capital.** Flash-loan compatible; only gas buffer.
**Net margin.** L1: 5–10% of gross after builder bids. L2s: bigger slice
of a smaller pie.

**Winning strategies**
- Don't compete on L1 head-to-head. Fight on L2s/alt-L1s where builder
  concentration is lower and latency edge still decays slowly.
- Co-locate the scanner next to sequencers/RPC endpoints (Base/Arbitrum
  sequencers, Solana Jito validator) and submit via each chain's native
  private orderflow route.
- Instrument *failure* cost, not just success profit. Your floor is set
  by gas you waste on failed bundles.
- Probabilistic bid shading: measure per-builder acceptance curves and
  bid the minimum viable share.

Sources: writings.flashbots.net/mev-and-the-limits-of-scaling ·
arxiv.org/html/2407.07474 · eigenphi.io ·
drops.dagstuhl.de/entities/document/10.4230/LIPIcs.AFT.2024.22

---

### 2. Cross-DEX with aggregator routing (be the solver)

**State.** CowSwap, 1inch Fusion, UniswapX and Across have pulled
retail orderflow out of the public AMM path into solver-based intent
auctions. CoW Protocol's solver auction is VCG-style; as of late 2025
the reward cap binds in only ~9% of auctions, implying ~91% are
genuinely competitive on price improvement. 1inch Fusion Resolvers fill
orders and keep the delta as profit (paying gas). These are now *the*
arbitrage venues — a classic Uniswap-v3 public-mempool arb bot is
increasingly front-run by solver inventory that already filled the
retail intent privately. The play has shifted: instead of arbing
aggregator-routed retail, *become* a solver.

**Accessible to solo?** As an arbitrageur against aggregator flow: no,
you're exit liquidity. As a solver: permissionless but requires a real
MM engine and inventory.
**Capital.** Meaningful solver inventory on Ethereum: 7–8 figures;
smaller on L2s.
**Net margin.** Successful solver teams earn six-figures USD/year; per-
auction margin is cents on the dollar.

**Winning strategies**
- Decide early: principal (inventory-bearing) solver or pure router.
  Start as a router on an L2.
- Instrument every auction you lose — "why" is the whole product.
- Combine solver + atomic-arb: same infra, two revenue streams (auction
  wins + backrunning your own settlements).
- Target long-tail pairs where top-3 solvers under-bid.

Sources: docs.cow.fi/cow-protocol/reference/core/auctions/rewards ·
docs.cow.fi/cow-protocol/concepts/introduction/solvers ·
help.1inch.com/en/articles/6796085

---

### 3. Cross-chain arbitrage (L1↔L2, L2↔L2, L1↔L1)

**State.** Intent-based bridges have compressed transfer latency enough
to make cross-chain arb tractable. Across Protocol moves ~$1B/month,
with USDC/ETH L2↔L2 transfers typically <60s and fees often <$0.04 —
the dominant relayer network for cross-rollup arb. LayerZero/Stargate
merged in late 2025; remains the choice for long-tail assets but
Stargate's variable fees (0.06%+, up to 1% at peak load) make it
unpredictable for thin-margin arb. Circle CCTP is slower than Across
but native-mint USDC. Directional cross-chain arb on majors is mostly
dead. The structural 2026 opportunity is being a *relayer* on Across
(fronting inventory against user intents), not carrying inventory
across a slow bridge.

**Accessible to solo?** Yes, skewed to the relayer role.
**Capital.** Inventory pre-positioned on every chain. Meaningful Across
relayer: $500k–$1M+ across 5–8 chains.
**Net margin.** Relayer fees: single-digit bps on majors; higher on
long-tail. Rebalancing is the hidden cost.

**Winning strategies**
- Run capital as an Across relayer on routes you already monitor for
  DEX arb; same inventory, two revenue streams.
- Model bridge finality as position-level risk with per-venue
  risk-weighted limits.
- Keep a "stress playbook" — biggest payoffs are depeg/outage events;
  have a manual approval path to size up.
- Isolate keys per chain; never share a private key across venues.

Sources: across.to · docs.across.to/concepts/intents-architecture-in-across ·
levex.com/en/blog/layerzero-stargate-merger

---

### 4. JIT liquidity, sandwich, backrun

**State.** Sandwich attacks are reportedly ~52% of 2025 MEV transaction
volume (~$290M gross) but against a shrinking user base — Flashbots
Protect, MEV Blocker, and default wallet RPCs route much of retail
around the public mempool. Flashbots Protect has protected ~$43B in DEX
volume for ~2.1M accounts. MEV-Share/orderflow auctions redirect ~90%
of captured backrun value to users (searcher keeps ~10%). JIT liquidity
is documented (~7,500 ETH over 20 months on Uniswap v3, a fraction of a
percent of TVL); v4 hooks narrow JIT edge further. Sandwiching retail
is increasingly toxic flow — wallets and builders are adversarial to
it. Backrunning via MEV-Share is the legitimate successor.

**Accessible to solo?** Sandwich: no (toxic, blocked). JIT: marginal,
needs tick expertise. Backrun via MEV-Share: yes, most accessible.
**Capital.** Backrun: flash-loan compatible. JIT: 6-figures on majors.
**Net margin.** Backrun: ~10% of bid. JIT: bounded at ~2× fee tier over
adverse selection.

**Winning strategies**
- Skip sandwiching. Build backrun-only on MEV-Share.
- For JIT: focus on high-volume low-toxicity pairs (ETH/USDC V3 1bps)
  and pair with existing solver/relayer inventory.
- Subscribe to MEV-Share hints programmatically; dedicated bundle
  simulator latency is the competitive surface.
- Measure toxicity of your own fills — it's the honest test of backrun
  vs sandwich.

Sources: writings.flashbots.net/2m-protect-users ·
docs.flashbots.net/flashbots-mev-share/introduction ·
blog.uniswap.org/jit-liquidity · eprint.iacr.org/2023/973.pdf

---

### 5. Liquidation bots (Aave, Compound, Morpho, Spark)

**State.** One of the "fairest" MEV categories — incentives explicit,
protocols *want* competition. Aave saw a rare ~$27M liquidation cascade
in March 2026 from a price glitch; liquidators captured ~499 ETH in
bonuses in a single event. P&L is spiky. Morpho Blue's open liquidation
bot repo and configurable Liquidation Incentive Factor have made it the
most approachable ecosystem for solo operators. Day-to-day Aave v3 on
majors is brutal (15+ labeled bots, compressed to gas + a few bps).
Edge is in (a) long-tail markets, (b) Morpho isolated markets on niche
chains, (c) oracle-update racing on low-activity chains.

**Accessible to solo?** Yes — especially on Morpho Blue and
non-Ethereum chains.
**Capital.** Zero with flash loans for self-contained; non-trivial for
multi-leg unwind.
**Net margin.** 10–50 bps on majors net; hundreds of bps on stress
events.

**Winning strategies**
- Subscribe directly to oracle updates (Chainlink, Chronicle, Redstone,
  Pyth) — health-factor recomputation is the real trigger.
- Focus on Morpho isolated markets: higher LIFs, thinner competition.
- Pre-build atomic unwind paths for every (collateral → debt) pair —
  stale swap routes are the #1 reason liquidations fail.
- Circuit-breaker against oracle glitches — liquidating off bad prices
  is how bots blow up.

Sources: docs.morpho.org/learn/concepts/liquidation ·
github.com/morpho-org/morpho-blue-liquidation-bot ·
docs.aave.com/developers/guides/liquidations

---

### 6. New-pool sniping (memecoins, V4 hooks)

**State.** Uniswap V4 is live across 12+ chains with hooks. Launch
ecosystem now includes hook-based launchpads like Flaunch on Base.
Commercial snipers (Maestro, Trojan, BananaGun, Photon on Solana)
capture most first-block liquidity adds on Ethereum and Base. V4 hooks
cut both ways: sophisticated launches can pre-sandwich snipers with
anti-MEV taxes, per-wallet caps, dynamic fees. Solana post-pump.fun
memecoin flow is structurally different (private Jito bundles +
co-lo). Honest framing: closer to gambling than arbitrage.

**Accessible to solo?** Technically yes; economic EV poor without an
edge on launch timing.
**Capital.** Small per snipe (hundreds to low thousands); expect to
lose many full positions.
**Net margin.** Heavy-tailed; average-case negative without edge.

**Winning strategies**
- Treat as a portfolio of options, not arbitrage. Max loss per entry is
  100%; size accordingly.
- Pre-simulate hook bytecode of any V4 pool — hooks can freeze sells or
  charge 99% tax; static honeypot checks aren't enough.
- Don't build from scratch; benchmark latency against existing sniper
  infra first.
- Positive-EV adjacent play: market-make the first 24h post-launch on
  survivors, not snipe block 0.

Sources: docs.uniswap.org/contracts/v4/concepts/hooks ·
blockworks.com/news/uniswap-v4-goes-live

---

## Non-DEX / Cross-Venue Categories

### 7. CEX ↔ CEX spot arbitrage

**State.** Average cross-exchange spread on major pairs has compressed
to 0.1–1% from 2–5% in earlier cycles; on Binance/Coinbase/OKX
BTC/USDT it's routinely single-digit bps, inside fee + latency cost.
Binance spot rate limits (300 connections per IP per 5 min; per-weight
caps) push operators to multi-IP, colocated infra. The real killer is
*withdrawal latency* — you can't move coins between venues fast
enough; you must pre-position inventory and rebalance. It's a capital-
inventory problem dominated by MM firms who already hold inventory for
other reasons.

**Accessible to solo?** Marginal. Tier-2/3 exchanges occasionally show
real spreads but carry counterparty risk. Tier-1 vs Tier-1: dead for
retail.
**Capital.** Full inventory on every venue (n × position size).
Realistic floor $50k–$100k across venues.
**Net margin.** 5–20 bps gross in decent cases; often negative on
majors after fees.

**Winning strategies**
- Forget "buy on A, withdraw, sell on B". Pre-position and rebalance
  on a slow clock when spreads normalize.
- Focus on longer-tail coins on Tier-2 venues — real spreads exist,
  and so does delisting/solvency risk.
- Use full-depth orderbooks to compute *realized* executable spread
  post-fees, not top-of-book.
- Hedge inventory drift with a perp on one venue; spot-only CEX-CEX
  quietly becomes directional.

Sources: developers.binance.com/docs/binance-spot-api-docs/websocket-api/rate-limits ·
cryptowisser.com/guides/arbitrage-dexs-cexs-cross-chain-bridges

---

### 8. CEX ↔ DEX arbitrage

**State.** Single largest "dark" MEV category. The 2025 paper *The
Darkest of the MEV Dark Forest* (Aug 2023–Mar 2025) estimates ~$234M
extracted across 7.2M CEX-DEX arbitrages by 19 major searchers; by Q1
2025 only 11–14 remain active, and 3 firms (Wintermute, SCP, Kayle)
capture ~90% of volume. Profitability is tightly coupled to vertical
integration with block builders — non-integrated searchers can't
guarantee top-of-block when the CEX ticks.

**Accessible to solo?** No, with an asterisk. Core loop is a 3-firm
oligopoly. Adjacent sub-niches (small L2s, exotic perps vs obscure
DEXs, Deribit-implied vs thin onchain oracles) have crumbs.
**Capital.** Real entry: 7-figures inventory + builder relationship +
low-latency CEX feed (co-lo AWS ap-northeast-1 for Binance Tokyo).
**Net margin.** Single-digit bps per trade; mid-five-figures/day per
top firm in aggregate.

**Winning strategies**
- Don't fight Wintermute on ETH/USDC. Pick a long-tail CEX pair (top
  50 alt) on a mid-tier L2.
- Build a CEX-implied fair-value oracle off Binance/OKX/Bybit and
  stream it to your onchain executor — 70% of the edge.
- Only scale path is a builder integration (BuilderNet, beaverbuild,
  Titan); otherwise you permanently pay the top-of-block tax.
- Hedge DEX inventory on the CEX within the same bundle's epoch.

Sources: arxiv.org/abs/2507.13023 ·
drops.dagstuhl.de/storage/00lipics/lipics-vol354-aft2025/LIPIcs.AFT.2025.26

---

### 9. Triangular arbitrage on a single CEX

**State.** Effectively dead for retail on Tier-1 CEXs. Binance
triangles (BTC/ETH/USDT, ETH/USDC/USDT) are sub-bp; fees (0.1% taker,
0.075% with BNB) alone exceed gross edge in >99% of observations. A
2024 ScienceDirect paper concluded it is only marginally exploitable
with VIP-tier fees, colocated infra, and full orderbook depth
modelling. Public GitHub bots see ~0 profitable trades/day at retail
fee tiers. Niche remaining: smaller venues (KuCoin, Gate, MEXC) with
uneven pair-fee schedules and thin long-tail liquidity.

**Accessible to solo?** Nearly no. Better treated as a learning
exercise for low-latency execution.
**Capital.** Sub-$10k to test; meaningful revenue needs VIP tier
(usually $1M+ 30d volume) and colocation.
**Net margin.** Marginal/negative at retail fees; low single-digit bps
at VIP-3+, capacity-limited (<$10k/opportunity).

**Winning strategies**
- If you must: Tier-2 venues with uneven pair-fee schedules (taker on
  BTC/USDT, maker on ETH/USDT) on the fee-adjusted pair graph.
- Size to orderbook depth on the *tightest* leg, not the fattest.
- Pre-commit all three orders as IOC before computing edge; otherwise
  edge is gone.
- Use it as inventory-management infra for a venue you already
  market-make on, not a standalone strategy.

Sources: sciencedirect.com/science/article/pii/S154461232401537X ·
github.com/tiagosiebler/TriangularArbitrage

---

### 10. Funding-rate / basis arbitrage (cash-and-carry)

**State.** Industrialized into a $6B+ product called Ethena. USDe has
~$5.9B market cap as of April 2026 (top-3 non-fiat-backed stablecoin),
built on long-spot-ETH/BTC + short-perp delta-neutral yield. Aggregated
BTC+ETH perp funding averaged ~11% annualized in 2024, ~5% in 2025;
highly regime-dependent — spikes to 20%+ APR in bull markets, goes
negative on drawdowns. Solo operator can replicate on
Binance/OKX/Bybit/Hyperliquid; hard parts are (a) inventory efficiency
across venues, (b) collateral haircuts in volatility, (c) when funding
is juiciest (late-cycle bull), so is liquidation risk on the short.

**Accessible to solo?** Yes — genuinely accessible. One of few
categories where retail replicates institutional per-unit returns.
**Capital.** $10k works; $100k+ is where operational friction
amortizes. Delta-neutral leverage typically 2–5×.
**Net margin.** 5–15% APR normal; 15–30% frothy markets; negative
during deleveraging.

**Winning strategies**
- Run across ≥3 perp venues and rebalance to the best funding —
  top-venue vs median spread is 2–5% APR, and that's the alpha.
- Hard-code a bear-market rule: persistent negative funding for 48h →
  close and redeploy to T-bills/staking. This is a *conditional*
  trade, not evergreen.
- Watch short-side liquidation cascades; use ≤3× leverage with
  pre-funded margin buffers rather than squeezing APR.
- To scale: offer it as a vault on Hyperliquid/Drift/Jupiter perps
  rather than just running it for yourself.

Sources: docs.ethena.fi/how-usde-works ·
stablecoininsider.org/ethena-usde-q1-2026-report ·
hummingbot.org/strategies/v1-strategies/perpetual-market-making

---

### 11. Options / vol arbitrage

**State.** Deribit dominates crypto options (>85% of global volume,
~80% OI institutional). Deribit MMs (Galaxy, Wintermute, GSR, QCP) *are*
the vol surface; retail/semi-pro trades against their models. Onchain
options venues (Lyra/Derive, Aevo, Premia) are smaller but occasionally
misprice relative to Deribit — genuine retail-accessible arb when it
opens, though capacity is small. Cross-venue vol arb (Deribit vs Lyra
IV on same expiry) and probability arb (Deribit-implied tail
probability vs Polymarket binary on same event) are the niches.

**Accessible to solo?** Niche yes — if you can build a real vol surface
and monitor onchain options venues. Mainstream Deribit relative value
is professional-only.
**Capital.** $50k+ for meaningful sizes; Deribit portfolio margin helps.
**Net margin.** Strategy-category, not a spread. Cross-venue onchain
mispricings: 5–20% edge before decay; capacity low.

**Winning strategies**
- Build/fetch a Deribit-anchored vol surface; monitor
  Lyra/Derive/Aevo for same-strike/same-expiry dislocations >5 vol pts.
- Cross-use the vol surface for Polymarket probability arb —
  Deribit's implied P(BTC > `$120k` by year-end) is tradable against
  a Polymarket binary.
- Avoid straddle arb on Deribit itself — spreads are wide vs fiat.
- Focus on events (pre-FOMC, pre-ETF) where IV decouples from
  slower-updating onchain venues.

Sources: dev.to/xniiinx/probability-arbitrage-how-to-beat-polymarket-using-deribit-options ·
coinglass.com/options/Deribit

---

### 12. Stablecoin depeg arbitrage

**State.** Rare-event trade with enormous P&L tails. March 2023
USDC-SVB is canonical: USDC traded to ~$0.878 for ~36 hours, Curve
3pool went to 46/46/7 (USDC/DAI/USDT), arbitrageurs with Circle
redemption access locked 5–12% risk-free in days. UST 2022 is the
opposite tail: redemption failed, coin went to zero. 2026 stables at
risk: USDe (survived late-2025 deleveraging but CEX counterparty
risk), FRAX, PYUSD, a long list of neobank stables. Strategy needs
(a) direct redemption access or (b) willingness to hold through
recovery. Without redemption, you're speculating on re-peg, not
arbitraging.

**Accessible to solo?** Only opportunistically. Between events,
nothing to trade. During events, pre-built infra must be ready.
**Capital.** Scale-dependent. With Circle/Tether redemption: $100k
barely worth setup; $10M+ is where you matter.
**Net margin.** Event-gated: 1–12% in hours, or total loss on peg
failure.

**Winning strategies**
- Maintain KYC and redemption relationships (Circle, USD0, Agora)
  *before* events.
- Pre-build peg monitor (Curve imbalance, CEX vs `$1`, issuer
  attestation freshness) with 50bp alerts, not 5%.
- Size by P(failure) × 100% loss assumption — only risk capital you'd
  lose on a UST repeat.
- Don't arb algorithmic stables blind; model the mechanism (perp
  short dependence, token backing) before entering.

Sources: coindesk.com/business/2023/03/10/defi-protocol-curves-500m-stablecoin-pool-hammered ·
bis.org/publ/work1164.pdf

---

### 13. Prediction-market arbitrage (Polymarket, Kalshi)

**State.** Polymarket's March 2026 fee switch and the sports-contract
boom turned prediction markets into a real arbitrage venue.
Platform revenue target post-fee-switch is $800k–$1M/day on ~$9.55B
trailing-30d volume. Average arb opportunity *duration* reportedly
compressed from ~12.3s in 2024 to ~2.7s in late-2025/early-2026, with
73% of arb profits captured by sub-100ms bots. Cross-venue
Polymarket↔Kalshi arb exists (Oddpool, eventarb) when the same event
lists on both, but is limited by (a) geofence — Polymarket
international is US-geoblocked, Polymarket US is sports-only, Kalshi
has cease-and-desists in ~11 states — and (b) subtly different
resolution semantics. On the crypto side, Polymarket BTC/ETH
price-target markets are straightforward to arb against Deribit
options (see §11). **The widely-cited "$438k in 30 days" figure could
not be verified against primary sources; treat as anecdote.**

**Accessible to solo?** Yes — probably the single most accessible
category on this list for a technical solo operator in early 2026,
precisely because it's young, fragmented, and jurisdiction barriers
keep big shops out. That window is closing.
**Capital.** $5k–$50k sufficient; fees (max 1.8% taker at `$0.50`) set
the edge floor.
**Net margin.** Median arb spread ~0.3% on Polymarket (barely above
fees); opportunistic cross-venue 2–5% per trade, capacity-limited.

**Critical finding — resolution source.** 5-minute BTC Up/Down
markets on the international CLOB resolve against the **Chainlink
BTC/USD Data Stream**, not Binance spot, with on-chain auto-resolution
via Chainlink Automation and no UMA dispute window. Any "Binance →
Polymarket" framing is measuring a proxy, not the truth. See
`polymarket_winning_strategies.md` §3a and
`polymarket_crypto_arbitrage_feasibility.md`.

**Fee picture drift.** Polymarket restructured international CLOB fees
in March 2026 into a category- and probability-based model (crypto
~1.80% taker, sports ~0.75%, politics ~1.00%, geopolitics 0%), and
fees are probability-shaped (highest near p=0.50). Polymarket US DCM
uses a different coefficient-based model. Read `feeSchedule` from
market metadata at order time; do not hardcode. See
`polymarket_us_availability.md` §5 and
`polymarket_winning_strategies.md` §4.

**Winning strategies**
- Anchor on Chainlink Data Streams as the reference, not Binance, for
  Chainlink-resolved markets. Binance is a supplementary leading
  indicator at best.
- Target **near-expiry certainty trades** in the last 10–30 seconds
  of 5-minute BTC Up/Down markets — structural edge, not
  latency-dependent, survives competitive compression.
- Build probability-arb on the Polymarket ↔ Deribit axis (implied
  P(BTC > X by expiry) vs Polymarket binary) — crypto-native vol
  surfaces, no Binance-as-truth assumption.
- For cross-venue Polymarket ↔ Kalshi, model *resolution* carefully —
  YES on one venue and NO on the other for "same" event often resolve
  on different datasources; free money can dematerialize on a
  technicality.
- Respect jurisdiction. International CLOB is US-geoblocked;
  Polymarket US DCM has its own blocklist (AZ, IL, MA, MD, MI, MT,
  NJ, NV, OH as of 2026-04-19). VPN workarounds violate ToS. State
  AGs are active. Not legal advice.
- Measure before trading. Post-fee-restructure net edge must be
  re-validated on real captures — old `0.72%` flat-fee assumptions
  understate cost by 2–3× on crypto markets.

Sources: docs.polymarket.com/trading/fees ·
stateline.org/2026/03/06/kalshi-and-polymarket-are-skirting-laws-on-sports-betting-states-say ·
quantvps.com/blog/cross-market-arbitrage-polymarket

---

## Ranked Recommendation For A Solo Operator

Expected-edge ranking for 2026, for a capable solo operator without a
builder relationship or 8-figure inventory:

1. **Funding-rate / basis arb (§10)** — highest mechanical return for
   retail, directly replicable across Binance / OKX / Bybit /
   Hyperliquid, Ethena-validated at $6B scale.
2. **Polymarket / prediction-market arb (§13)** — most accessible
   niche right now. Window closes as shops arrive. Key finding:
   anchor on Chainlink Data Streams, not Binance, for 5-minute crypto
   markets. Category fees (~1.80% on crypto) raise the edge floor.
3. **Liquidation bots on Morpho / non-blue-chip chains (§5)** —
   atomic-transaction stack; thinner competition than blue-chip DEX
   arb.
4. **Cross-chain relayer on Across (§3)** — uses inventory already
   held across chains for other strategies; structural second revenue
   stream.
5. **Atomic cross-DEX on L2s (§1)** — L1 is saturated; focus L2
   sequencer/builder edges where concentration is lower.
6. **Becoming a solver (§2)** — medium-term; start as a router on an
   L2 before committing inventory.

Avoid as primary strategies for a solo operator: CEX-DEX (§8),
triangular CEX on Tier-1 venues (§9), sandwich (§4), large-venue vol
arb (§11), and memecoin sniping (§6) as anything other than a small
speculative side bet.

## What This Doc Deliberately Does Not Do

- Does not pick one strategy. That decision depends on jurisdiction,
  capital, and risk tolerance — all of which belong in a per-strategy
  design doc.
- Does not validate any specific "get-rich" case study. Where a
  widely-repeated figure was unverifiable, it is flagged as such.

## See Also

- `polymarket_winning_strategies.md` — bot-focused research on the
  spot→Polymarket repricing edge; Chainlink Data Streams resolution
  finding; implications of the March 2026 fee restructure.
- `polymarket_us_availability.md` — US legal/regulatory status,
  state-by-state blocklist, CFTC DCM picture, fee differences between
  Polymarket US DCM and international CLOB.
- `polymarket_crypto_arbitrage_feasibility.md` — deeper focus on the
  Polymarket-specific strategy and why the naive "Binance reference"
  framing is wrong.
- `polymarket_getting_started.md` — human-trader walkthrough for
  signup, KYC, funding, programmatic surface-area.
- `PolymarketBotGuide.md` — older Python-bot build guide; predates the
  March 2026 fee restructure and the Chainlink-resolution finding,
  read critically.

## Sources

### DEX-centric
- [Flashbots — MEV and the limits of scaling](https://writings.flashbots.net/mev-and-the-limits-of-scaling)
- [Schlegel & Sui — arxiv.org/html/2407.07474](https://arxiv.org/html/2407.07474)
- [EigenPhi](https://eigenphi.io)
- [AFT 2024 — MEV auctions](https://drops.dagstuhl.de/entities/document/10.4230/LIPIcs.AFT.2024.22)
- [CoW Protocol auction rewards](https://docs.cow.fi/cow-protocol/reference/core/auctions/rewards)
- [CoW Protocol solvers](https://docs.cow.fi/cow-protocol/concepts/introduction/solvers)
- [1inch Fusion — how it works](https://help.1inch.com/en/articles/6796085-what-is-1inch-fusion-and-how-does-it-work)
- [Across Protocol](https://across.to)
- [Across — intents architecture](https://docs.across.to/concepts/intents-architecture-in-across)
- [LayerZero / Stargate merger](https://levex.com/en/blog/layerzero-stargate-merger)
- [Flashbots Protect — 2M users](https://writings.flashbots.net/2m-protect-users)
- [MEV-Share](https://docs.flashbots.net/flashbots-mev-share/introduction)
- [Uniswap — JIT liquidity](https://blog.uniswap.org/jit-liquidity)
- [JIT research — eprint.iacr.org/2023/973](https://eprint.iacr.org/2023/973.pdf)
- [Morpho liquidation docs](https://docs.morpho.org/learn/concepts/liquidation/)
- [Morpho Blue liquidation bot](https://github.com/morpho-org/morpho-blue-liquidation-bot)
- [Aave liquidation docs](https://docs.aave.com/developers/guides/liquidations)
- [Uniswap V4 — hooks](https://docs.uniswap.org/contracts/v4/concepts/hooks)
- [Uniswap V4 goes live — Blockworks](https://blockworks.com/news/uniswap-v4-goes-live)

### Non-DEX / cross-venue
- [Binance spot rate limits](https://developers.binance.com/docs/binance-spot-api-docs/websocket-api/rate-limits)
- [Cryptowisser arbitrage guide](https://www.cryptowisser.com/guides/arbitrage-dexs-cexs-cross-chain-bridges/)
- [The Darkest of the MEV Dark Forest — arxiv.org/abs/2507.13023](https://arxiv.org/abs/2507.13023)
- [AFT 2025 — CEX-DEX](https://drops.dagstuhl.de/storage/00lipics/lipics-vol354-aft2025/LIPIcs.AFT.2025.26/LIPIcs.AFT.2025.26.pdf)
- [Triangular arb exploitability — ScienceDirect](https://www.sciencedirect.com/science/article/pii/S154461232401537X)
- [Ethena — how USDe works](https://docs.ethena.fi/how-usde-works)
- [Ethena USDe overview](https://docs.ethena.fi/solution-overview/usde-overview)
- [Hummingbot — perpetual market making](https://hummingbot.org/strategies/v1-strategies/perpetual-market-making/)
- [Probability arb — Polymarket vs Deribit options](https://dev.to/xniiinx/probability-arbitrage-how-to-beat-polymarket-using-deribit-options)
- [Coinglass — Deribit options](https://www.coinglass.com/options/Deribit)
- [USDC depeg 2023 — CoinDesk](https://www.coindesk.com/business/2023/03/10/defi-protocol-curves-500m-stablecoin-pool-hammered-as-traders-flee-usdc)
- [BIS stablecoins working paper](https://www.bis.org/publ/work1164.pdf)
- [Polymarket — trading fees](https://docs.polymarket.com/trading/fees)
- [Kalshi / Polymarket state laws — Stateline](https://stateline.org/2026/03/06/kalshi-and-polymarket-are-skirting-laws-on-sports-betting-states-say/)
- [QuantVPS — cross-market arbitrage Polymarket](https://www.quantvps.com/blog/cross-market-arbitrage-polymarket)

### Polymarket / Chainlink finding
- [Chainlink BTC/USD Data Stream](https://data.chain.link/streams/btc-usd)
- [Chainlink Data Streams docs](https://docs.chain.link/data-streams)
- [Polymarket docs](https://docs.polymarket.com)
- [Polymarket RTDS — crypto prices](https://docs.polymarket.com/developers/RTDS/RTDS-crypto-prices)
