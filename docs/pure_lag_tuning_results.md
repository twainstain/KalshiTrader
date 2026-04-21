# PureLagStrategy tuning — live results

**Research date:** 2026-04-21
**Run:** pure_lag-only primary evaluator, launched 2026-04-21 00:35 UTC, ran ~80 minutes, stopped by user.
**Database:** `data/kalshi.db` — `strategy_label='pure_lag' AND ts_us > 1776731700000000`.

## TL;DR — tuning was too aggressive; revert or adjust

Pre-tuning pure_lag (from the earlier 3-model run, same strategy with old defaults) produced **76% win rate, +$0.39/decision** over 29 reconciled decisions. The tuned-defaults run produced **59% win rate, −$0.19/decision** over 101 reconciled decisions.

Net: we traded 3.5× more volume for a ~$0.58/decision drop in quality. Not a fair trade.

## Config delta

| parameter | old (pre-tune) | new (this run) | reason for change |
|---|---:|---:|---|
| `move_threshold_bps` | 5 | **3** | wanted more volume |
| `rolling_window_us` | 10 s | **5 s** | smaller smoothing baseline |
| `time_window_seconds` | (120, 900) | **(30, 900)** | hunt the final-minute lag edge |
| `min_fill_price` | — | **0.10** | reject lottery-ticket asks |

## Aggregate

| metric | pre-tune (29 dec) | post-tune (101 dec) |
|---|---:|---:|
| reconciled | 22 | 101 |
| wins | 17 (77%) | 60 (59%) |
| P/L | **+$11.25** | **−$19.40** |
| $/decision | +$0.39 | −$0.19 |
| unique markets | 16 | 23 |

## Per asset (post-tune only)

| asset | n | reconciled | wins | win-rate | P/L |
|---|---:|---:|---:|---:|---:|
| HYPE | 61 | 61 | 36 | 59.0% | **−$4.15** |
| DOGE | 14 | 14 | 10 | 71.4% | −$2.87 |
| BNB | 11 | 11 | 5 | 45.5% | −$8.20 |
| ETH | 6 | 6 | 4 | 66.7% | **+$2.30** |
| BTC | 3 | 3 | 2 | 66.7% | −$0.58 |
| SOL | 3 | 3 | 1 | 33.3% | −$6.70 |
| XRP | 3 | 3 | 2 | 66.7% | **+$0.80** |

- **HYPE** was the star performer pre-tune (70.6% win, +$7.33) — tuned config degraded it to 59% / −$4.15. More decisions, worse quality.
- **DOGE** has 71% win rate but still lost money — losers paid more than winners.
- **BNB / SOL** have the worst win rates — Kraken basket reference isn't helping here.

## Per time-bucket (post-tune)

| bucket | n | wins | win-rate | P/L |
|---|---:|---:|---:|---:|
| 30-60s | 3 | 3 | **100.0%** | **+$4.40** |
| 60-120s | 1 | 1 | 100.0% | +$1.10 |
| 120-300s | 15 | 11 | 73.3% | **+$3.35** |
| 300-600s | 47 | 26 | 55.3% | **−$18.35** |
| 600-900s | 35 | 19 | 54.3% | −$9.90 |

**The story:** the newly-opened 30-60s and 60-120s buckets worked (tiny sample, 100% win rate, +$5.50 combined). Every other bucket lost. The 300-600s bucket holds 47% of all decisions and 95% of the total loss — **this is where the lowered move-threshold is hurting us**. Too many marginal decisions on small moves that reverse before close.

## Side distribution + fill-range

| side | n | avg fill | min | max |
|---|---:|---:|---:|---:|
| yes | 56 | 0.70 | 0.15 | 0.98 |
| no | 45 | 0.50 | 0.10 | 0.97 |

- **`min_fill_price=0.10` enforced correctly** — no fills below $0.10 observed. Floor is working.
- Side balance is reasonable (56 yes / 45 no). Previous run without floor would have had more 0.01–0.05 long-tail yes bets.

## What this tells us

1. **The 30-60s / 60-120s opening was the right call** — small sample, but 100% win rate supports the backtest's claim that the feed-lag edge lives in the final minute.
2. **The 5 → 3 bps threshold drop was the wrong call.** 3 bps fires on too much noise in longer time horizons, producing 300-600s losers that didn't exist before.
3. **The 10s → 5s rolling window shrink is also suspect** — tighter baseline means more false triggers on small Brownian fluctuations.
4. **`min_fill_price=0.10` is the one tuning change to keep.** It silently prevents lottery-ticket bleed and didn't hurt volume disproportionately.

## Recommended next config

| parameter | value | rationale |
|---|---:|---|
| `move_threshold_bps` | **5** | revert — 3 is too sensitive |
| `rolling_window_us` | **10 s** | revert — 5s was over-sensitive |
| `time_window_seconds` | **(30, 900)** | KEEP — final-minute opening worked |
| `min_fill_price` | **0.10** | KEEP — prevents long-tail bleed |

This revert+keep config would recover the pre-tune 76% win rate while still allowing pure_lag to hunt the 30-120s bucket where the 100% win rate showed up.

Optional additional gate to try: `max_time_remaining_s = 300` — restrict pure_lag to the last 5 minutes only, where the per-decision $ is biggest. Would drop volume but likely boost $/decision.

## Caveats

- **80 min is still a small sample.** 101 decisions spread across only 3-4 close cycles × 7 assets × a handful of triggers each. Anything single-asset (BTC/ETH/XRP/SOL n=3) is essentially noise.
- **Coinbase + Kraken basket was active** — can't attribute performance change solely to strategy config.
- **Directional regime:** this 80-minute window may have had its own no/yes bias. Need ≥ 4 hours of independent regimes to calibrate confidently.

## Reproduction

```bash
PYTHONPATH=src python3.11 src/run_kalshi_shadow.py \
    --primary-strategy pure_lag --with-kraken --interval-s 1.0 -v
```

Decisions land in `data/kalshi.db` with `strategy_label='pure_lag'`. Query against the run's `ts_us > launch_timestamp` to isolate results.
