# Kalshi Lag Research Findings

**Research date:** 2026-04-21

> This is a heuristic triage memo based on the first full registry build. It is not a measured lag report.

## What We Collected

- `9,756` candidate series were written to `kalshi_lag_candidates`.
- `2,884` contract-term PDFs were downloaded and indexed.
- `351` candidates landed in the `high` band.
- Every `high`-band candidate came from the `scheduled_release` source-type bucket.

## Main Takeaways

### 1. Fixed-time public releases are the strongest general lag bucket

The registry overwhelmingly ranked scheduled macro releases highest. The best-scoring clusters were:

- CPI / core CPI / related inflation prints
- Fed / FOMC / rate-decision markets
- GDP and jobs-related releases
- EIA-linked energy releases

This is the cleanest multi-category lag setup because:

- the release time is known in advance
- the source is public or near-public
- the market repricing window is likely concentrated into a narrow burst

Within the current run, `170` of the `351` high-priority rows were in `Economics`.

### 2. Earnings-mention markets look like the best secondary bucket

The next strongest cluster was scheduled company/earnings mention markets. Examples include:

- `KXEARNINGSMENTIONAAPL`
- `KXEARNINGSMENTIONAMD`
- `KXEARNINGSMENTIONCOINBASE`
- `KXEARNINGSMENTIONMSFT`
- `KXEARNINGSMENTIONNFLX`

These scored well because they inherit a company-scheduled release time and appear to resolve off structured post-release information. There were `88` high-priority rows in the `Mentions` category.

This is interesting, but it is operationally harder than macro:

- transcript / press-release timing can vary
- parsing correctness matters
- the fastest actors may be consuming specialized feeds

### 3. Crypto remains the best on-strategy continuous market family for this repo

The registry does not rank the crypto markets at the very top because the heuristic favors fixed-time releases. Even so, crypto is still the best fit for the core scanner plan.

The strongest crypto rows were continuous-index markets such as:

- `KXBTC`: score `79`
- `KXETH`: score `79`
- `KXSOL`: score `79`
- `KXBTC15M`: score `69`
- `KXETH15M`: score `75`
- `KXSOL15M`: score `69`

Why this still matters:

- `221` medium-priority rows were in `Crypto`, by far the largest `medium` bucket
- the source agency is clearer: `CF Benchmarks / market data`
- this matches the project’s primary thesis that the best solo-operator edge is in near-expiry crypto fair-value / lag capture rather than broad headline races

So the registry is telling us two different truths:

- the broadest generic lag opportunities are scheduled macro releases
- the best opportunity family for this repo’s intended trading system is still crypto near-expiry

### 4. Most event-driven headline markets look weak for a first pass

Large parts of the catalog are event-driven news or score-driven markets. They dominate the raw candidate count, but they did not rank well:

- `3,662` rows were `event_driven_news`
- `1,962` rows were `event_driven_scored`
- almost all of them stayed in `low`

This is a good sign that the triage is filtering toward markets with repeatable timing rather than noisy headline-chasing.

## Opportunity Ranking

### Best overall lag-research buckets

1. Macro scheduled releases
2. Fed / rates / employment / inflation subfamilies
3. Earnings-mention scheduled markets
4. Crypto continuous-index markets
5. Weather / commodity daily-report and continuous-index markets

### Best buckets for the current project

1. `KXBTC15M`, `KXETH15M`, `KXSOL15M`
2. parent crypto index markets like `KXBTC`, `KXETH`, `KXSOL`
3. one macro benchmark cohort for comparison, ideally CPI + NFP + Fed

## What I Would Not Prioritize Yet

- broad politics headline markets
- sports score-update markets
- unknown-source markets without an authoritative release path
- one-off company rumor or event markets

They may contain isolated opportunity, but they are not the cleanest first measurement targets.

## Recommended Next Steps

1. Start measurement on two tracks in parallel:
   - Track A: crypto near-expiry (`KXBTC15M`, `KXETH15M`, `KXSOL15M`)
   - Track B: one fixed-time macro cohort (`CPI`, `NFP`, `Fed`)
2. Replace heuristic source mapping with authoritative mappings for the top `100` scheduled-release tickers.
3. Add a release-calendar table with exact publication timestamps and expected source URLs.
4. Build a live capture harness that records:
   - Kalshi quote snapshots
   - source publication timestamps
   - first observed post-release reprices
   - estimated stale-window duration
5. Parse the matched contract-term PDFs more deeply so we store explicit resolution mechanics instead of only ticker-level associations.
6. Keep earnings-mention markets as a secondary research stream, not the first live execution target.
7. Treat the `high` bucket as a research queue, not a trade queue. Nothing here is deployable until we measure real stale windows and realized executable edge.

## Bottom Line

The collection run worked and the registry is directionally useful.

If we want the highest-probability general lag experiments, we should start with scheduled macro releases.

If we want the best fit for the system this repo is actually trying to build, we should keep crypto near-expiry as the primary track and use macro releases as a benchmark / comparison track rather than a full pivot.

## Inputs

- [kalshi_multi_category_lag_research.md](./kalshi_multi_category_lag_research.md)
- [kalshi_lag_opportunity_ranking.md](./kalshi_lag_opportunity_ranking.md)
- [kalshi_research_collection_summary.md](./kalshi_research_collection_summary.md)
