# Three-Model Backtest Results

**Research date:** 2026-04-20

Markets processed: 777  —  total decisions: 1,346,053

Each decision = one Kalshi historical trade that the model would have taken (edge ≥ 100 bps after fees) at the recorded trade price. P/L scored against the settled_result with a 35 bps taker fee.

---

## Aggregate per model

| model | decisions | wins | win-rate | mean-edge (bps) | total P/L ($) | $/decision |
|---|---:|---:|---:|---:|---:|---:|
| stat_model | 670,212 | 372,426 | 55.6% | – | +20463.26 | +0.0305 |
| partial_avg | 659,679 | 379,412 | 57.5% | – | +33056.92 | +0.0501 |
| pure_lag | 16,162 | 10,736 | 66.4% | – | +1951.97 | +0.1208 |

## Per asset × model

| model | asset | decisions | win-rate | total P/L ($) | $/decision |
|---|---|---:|---:|---:|---:|
| stat_model | bnb | 16,949 | 56.0% | -2248.21 | -0.1326 |
| stat_model | btc | 490,561 | 55.2% | +30678.19 | +0.0625 |
| stat_model | doge | 18,388 | 57.5% | -1250.15 | -0.0680 |
| stat_model | eth | 73,122 | 55.2% | -185.15 | -0.0025 |
| stat_model | hype | 25,554 | 58.6% | -3456.01 | -0.1352 |
| stat_model | sol | 16,545 | 53.1% | -2125.41 | -0.1285 |
| stat_model | xrp | 29,093 | 59.2% | -950.01 | -0.0327 |
| partial_avg | bnb | 16,854 | 57.6% | -1987.02 | -0.1179 |
| partial_avg | btc | 483,062 | 56.9% | +37949.37 | +0.0786 |
| partial_avg | doge | 18,029 | 61.1% | -517.34 | -0.0287 |
| partial_avg | eth | 72,251 | 56.7% | +966.85 | +0.0134 |
| partial_avg | hype | 25,268 | 61.6% | -2597.01 | -0.1028 |
| partial_avg | sol | 16,025 | 57.9% | -1187.27 | -0.0741 |
| partial_avg | xrp | 28,190 | 63.3% | +429.33 | +0.0152 |
| pure_lag | bnb | 180 | 60.6% | -12.16 | -0.0675 |
| pure_lag | btc | 8,891 | 64.1% | +1453.31 | +0.1635 |
| pure_lag | doge | 799 | 69.7% | +131.70 | +0.1648 |
| pure_lag | eth | 3,299 | 66.2% | +190.34 | +0.0577 |
| pure_lag | hype | 1,218 | 69.0% | -22.92 | -0.0188 |
| pure_lag | sol | 773 | 74.0% | +60.33 | +0.0780 |
| pure_lag | xrp | 1,002 | 77.1% | +151.35 | +0.1511 |

## Per time-bucket × model

| model | bucket | decisions | win-rate | total P/L ($) | $/decision |
|---|---|---:|---:|---:|---:|
| stat_model | 0-30 | 11,401 | 57.9% | -234.64 | -0.0206 |
| stat_model | 30-60 | 19,729 | 50.9% | -1533.91 | -0.0777 |
| stat_model | 60-120 | 53,676 | 52.1% | -2424.63 | -0.0452 |
| stat_model | 120-300 | 155,160 | 53.8% | -1450.43 | -0.0093 |
| stat_model | 300-600 | 208,493 | 57.6% | +13029.82 | +0.0625 |
| stat_model | 600-900 | 221,753 | 56.0% | +13077.06 | +0.0590 |
| partial_avg | 0-30 | 10,097 | 78.4% | +1946.29 | +0.1928 |
| partial_avg | 30-60 | 17,949 | 64.1% | +1035.22 | +0.0577 |
| partial_avg | 60-120 | 51,479 | 56.1% | -450.07 | -0.0087 |
| partial_avg | 120-300 | 152,075 | 55.8% | +1341.05 | +0.0088 |
| partial_avg | 300-600 | 207,143 | 58.7% | +15285.42 | +0.0738 |
| partial_avg | 600-900 | 220,936 | 56.4% | +13899.02 | +0.0629 |
| pure_lag | 0-30 | 51 | 98.0% | +3.66 | +0.0718 |
| pure_lag | 30-60 | 519 | 78.6% | +102.40 | +0.1973 |
| pure_lag | 60-120 | 382 | 60.5% | +36.13 | +0.0946 |
| pure_lag | 120-300 | 2,251 | 64.7% | +285.23 | +0.1267 |
| pure_lag | 300-600 | 7,588 | 67.7% | +921.97 | +0.1215 |
| pure_lag | 600-900 | 5,371 | 64.3% | +602.58 | +0.1122 |

---

## Methodology

- `kalshi_historical_markets` filtered to settled 15-min BTC/ETH/SOL/XRP/DOGE/BNB/HYPE markets whose `close_ts` falls inside the Coinbase-trade coverage window.
- For every `kalshi_historical_trades` event on each market, we reconstruct (a) current spot from the latest `coinbase_trades` entry and (b) the partial close-60s-avg from the Coinbase ticks in `[close-60s, min(now, close)]`.
- Each model sees the same (spot, strike, comparator, time_remaining, observed_window) tuple and decides whether to take the trade at the recorded Kalshi price. Models diverge on `p_yes` and therefore on which side they'd take and whether the edge clears 100 bps.
- P/L uses a 35 bps taker fee. A winning yes pays `1 - fill_price`; a losing yes loses `fill_price` plus fee.
- `pure_lag` fed sub-second Coinbase ticks to populate its rolling window; `min_edge_bps_after_fees=100` matches the other two models for comparability.

## Caveats

1. **Book reconstruction is approximate** — we treat each historical trade as a quote we could have hit at the recorded price. The actual book could have had wider spread; this assumption is generous to all three models equally.
2. **Coinbase ≠ CF Benchmarks** — the partial-avg's observed window is computed from Coinbase trades, not the actual CF RTI. For a tight backtest this is the closest proxy; real-life we'd use the basket (Coinbase + Kraken + Binance).
3. **pure_lag is sensitive to feed lag.** The historical `coinbase_trades` are post-WS timestamps, so the measured lag is ~0 in backtest — this understates the real-life edge. Treat `pure_lag` rows here as an upper-bound on the strategy; live data has given weaker numbers.
