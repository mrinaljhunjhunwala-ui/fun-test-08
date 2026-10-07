# Falling-market ranking: new candidates for range and trend_down (2026-10-07)

Research only (audit item 8). Production scoring (`analysis/score.py`) and the golden files are unchanged.
This is educational material, not investment advice.

## Question

The composite score90 ranks stocks in `trend_up` (weekly IC20 +0.039, t 3.8). It carries close to no
information in `range` (−0.011), and in `trend_down` it has only a thin signal resting on 9 episodes
(+0.037). Do any candidates **not already tested** (see RESEARCH_LOG: #290, #294, #299, #301, #306)
rank better in those regimes?

## Data and method

- **Observations.** The 10y production-mode `observations.csv`: 177,289 rows, 483 nifty500 tickers,
  2017-07-27 → 2026-07-09. This is the same vintage as #294 and #299. The study ran from a frozen
  `git archive origin/main` snapshot (f112129) in scratch, not from the worktree.
- **Prices.** Closes were re-fetched for every ticker plus NIFTY 50 (`^NSEI`). Every factor is
  computed as of the observation date.
- **Script.** `research/falling_market_ranking.py`.

| factor | definition | new? |
|---|---|---|
| `lowbeta` | minus 120-session beta vs NIFTY 50 | new |
| `lowbeta_sn` | lowbeta minus week × sector median | new |
| `lowvol_sn` | minus 60-session stdev, sector-neutral | new form (raw in #294) |
| `near52wh_sn` | close / 252-session high − 1, sector-neutral (George-Hwang) | new form (raw in #294) |
| `rs_sec20` | 20-session return minus week × sector median | reference (= −rev20_within of #299) |
| `rs_sec60` | 60-session return minus week × sector median | new |
| `blend_rev` | pct-rank of score90 in trend_up; sector-neutral reversal (−rs_sec20) elsewhere | new (regime switch) |
| `blend_def` | score90 in trend_up; mean pct-rank of (−rs_sec20, lowbeta) elsewhere | new (regime switch) |

Both blends were fixed before any results were seen. A sector-neutral row needs at least 5 stocks in
its sector that week.

**Quality proxies (ROE stability, low leverage) are not tested.** The repo has no point-in-time
fundamentals history over the 10y window. `fundamental_quality.py` starts in 2022-10 and serves
restated yfinance values, so it covers almost no range or trend_down weeks (#301 had only 2
trend_down dates). Testing quality needs a point-in-time fundamentals source first.

**Statistics.**
- IC is the per-week Spearman rank correlation, with at least 30 stocks per week. Significance is a
  Newey-West t with 1, 4 or 12 lags for the 5, 20 and 60-session horizons. Train and holdout halves
  are split by week.
- The #235 rule compares each factor's weekly IC20 with score90's, as a paired difference. Weeks are
  split into two folds by alternating calendar quarter, and a factor passes only if NW t > 2 in
  **both** folds.
- Costs: turnover is measured on the top decile, between rebalances 4 weeks apart. It is charged at
  the Indian delivery round trip from `trading.costs.representative_round_trip_pct()`, which is
  **0.258%** at a ₹2L order.
- "top" is the long-only top decile minus the week mean. "L/S" is d10 − d1, charged cost on both
  legs. Returns are in percentage points per 20 sessions.

Evidence is thin. Range has 113 weeks (about 26 real episodes). trend_down has 44 weeks (about 9
episodes, mostly 2018–19). risk_off has 63 weeks.

## Results: weekly IC (NW t)

### range (112–113 weeks)

| factor | IC5 | IC20 | t20 | train / hold (20d) | IC60 |
|---|---|---|---|---|---|
| score90 | −0.018 | −0.011 | −0.6 | −0.000 / −0.021 | +0.026 |
| lowbeta | +0.017 | −0.006 | −0.3 | +0.025 / −0.036 | +0.002 |
| lowbeta_sn | +0.006 | −0.003 | −0.2 | +0.016 / −0.022 | +0.001 |
| lowvol_sn | +0.025 | +0.018 | 0.9 | +0.049 / −0.013 | +0.015 |
| near52wh_sn | +0.004 | +0.004 | 0.2 | +0.031 / −0.022 | +0.042 (t 1.9) |
| rs_sec20 | −0.042 | **−0.050** | −3.8 | −0.064 / −0.036 | −0.023 |
| rs_sec60 | −0.025 | −0.035 | −2.0 | −0.016 / −0.053 | −0.014 |
| **blend_rev** | +0.042 | **+0.050** | **3.8** | +0.064 / +0.036 | +0.023 |
| blend_def | +0.040 | +0.029 | 1.7 | +0.061 / −0.003 | +0.017 |

### trend_down (43–44 weeks, ~9 episodes)

| factor | IC5 | IC20 | t20 | train / hold (20d) | IC60 (t) |
|---|---|---|---|---|---|
| score90 | +0.043 | +0.037 | 2.0 | +0.050 / +0.023 | +0.030 (2.2) |
| lowbeta | −0.047 | −0.056 | −2.2 | −0.045 / −0.066 | −0.078 (−5.8) |
| lowbeta_sn | −0.030 | −0.042 | −2.4 | −0.025 / −0.058 | −0.051 (−4.2) |
| lowvol_sn | −0.013 | −0.019 | −0.8 | +0.019 / −0.055 | −0.076 (−3.3) |
| near52wh_sn | −0.037 | −0.013 | −0.5 | −0.013 / −0.014 | +0.001 |
| rs_sec20 | −0.081 | −0.076 | −2.9 | −0.074 / −0.077 | −0.054 |
| rs_sec60 | −0.047 | −0.046 | −1.5 | −0.065 / −0.029 | −0.040 |
| **blend_rev** | +0.081 | **+0.076** | **2.9** | +0.074 / +0.077 | +0.054 (1.6) |
| blend_def | +0.035 | +0.013 | 0.9 | +0.020 / +0.007 | −0.020 |

**risk_off (63 weeks, reference).** score90 IC20 is +0.021 (t 2.4) and blend_rev is +0.067 (t 3.0).
Every defensive factor is negative: lowbeta −0.064, lowbeta_sn −0.071, lowvol_sn −0.075.

**Whole sample (all regimes, 459 weeks).** score90 IC20 is +0.027 (t 3.5), **blend_rev +0.048
(t 6.9)** and blend_def +0.031 (t 4.0).

## #235 two-fold rule (paired IC20 diff vs score90; t > 2 needed in both folds)

| regime | factor | fold A diff (t) | fold B diff (t) | passes |
|---|---|---|---|---|
| range | lowbeta | −0.013 (−0.5) | +0.019 (0.8) | no |
| range | lowvol_sn | +0.010 (0.3) | +0.041 (1.9) | no |
| range | near52wh_sn | +0.000 (0.0) | +0.024 (1.4) | no |
| range | rs_sec60 | −0.001 (−0.1) | −0.047 (−3.8) | no |
| range | blend_rev | +0.083 (2.00) | +0.039 (1.0) | no |
| range | blend_def | +0.035 (1.0) | +0.040 (1.6) | no |
| trend_down | lowbeta | −0.074 (−1.9) | −0.111 (−2.4) | no |
| trend_down | near52wh_sn | −0.017 (−0.5) | −0.079 (−1.7) | no |
| trend_down | blend_rev | +0.020 (0.9) | +0.065 (1.6) | no |
| trend_down | blend_def | −0.026 (−1.1) | −0.015 (−0.5) | no |
| ALL | blend_rev | +0.026 (2.5) | +0.014 (1.3) | no |
| ALL | blend_def | +0.003 (0.4) | +0.003 (0.4) | no |

No candidate passes in any regime. The full table is in `research/output/fallmkt_folds.csv`.

## Turnover and after-cost spread (20 sessions, pts)

| regime | factor | turnover | top gross | top net (t) | L/S gross | L/S net (t) |
|---|---|---|---|---|---|---|
| range | score90 | 0.79 | −0.30 | −0.50 (−1.6) | +0.08 | −0.33 (−0.7) |
| range | lowbeta | 0.39 | −0.59 | −0.69 (−2.2) | −0.50 | −0.70 (−1.0) |
| range | lowvol_sn | 0.46 | −0.08 | −0.20 (−0.6) | −0.27 | −0.51 (−0.7) |
| range | near52wh_sn | 0.50 | −0.21 | −0.34 (−1.5) | −0.65 | −0.91 (−1.7) |
| range | rs_sec60 | 0.63 | −0.01 | −0.17 (−0.5) | −1.37 | −1.70 (−2.6) |
| range | **blend_rev** | 0.89 | +0.89 | **+0.66 (2.4)** | +1.43 | +0.97 (2.0) |
| range | blend_def | 0.75 | +0.12 | −0.07 (−0.3) | +0.63 | +0.25 (0.4) |
| trend_down | score90 | 0.91 | −0.10 | −0.33 (−1.2) | +0.54 | +0.08 (0.1) |
| trend_down | lowbeta | 0.33 | −1.20 | −1.29 (−3.9) | −1.58 | −1.75 (−2.0) |
| trend_down | near52wh_sn | 0.52 | +0.10 | −0.03 (−0.1) | −0.66 | −0.92 (−0.9) |
| trend_down | **blend_rev** | 0.89 | +1.91 | **+1.68 (3.7)** | +2.78 | +2.33 (2.2) |
| trend_down | blend_def | 0.78 | +0.25 | +0.05 (0.1) | −0.10 | −0.50 (−0.6) |

Costs exclude slippage and market impact. At about 90% turnover every 20 sessions, a reversal book
pays roughly 0.23 pts per period in charges alone.

## Findings

1. **Defensive ranking does not work in Indian falling markets.**
   - Low beta and sector-neutral low volatility have a negative IC20 in trend_down (−0.04 to −0.06)
     and in risk_off (−0.06 to −0.08).
   - The effect is strongest at 60 sessions (lowbeta −0.078, t −5.8). High-beta names fell further,
     then rebounded harder within the forward window.
   - Survivorship in the current-constituents universe exaggerates this. Still, the sign is the
     opposite of the textbook low-volatility anomaly, so this is not a candidate.
   - In range, every defensive form is about zero.
2. **Sector-neutral 52-week-high proximity is about zero in range and trend_down.** The only hint is
   a range IC60 of +0.042 (t 1.9), and that comes from the train half alone (+0.086 train, −0.004
   holdout).
3. **Relative strength vs the sector ranks backwards.** rs_sec20 is the mirror image of #299's
   sector-neutral reversal. The new 60-session version is also negative, in range (−0.035, t −2.0)
   and in trend_down (−0.046). Stocks that led their sector gave back that lead in non-trending
   markets.
4. **The regime-switched reversal blend (`blend_rev`) is the strongest result, but it is #299's
   signal, not a new one.** Outside trend_up, the blend *is* sector-neutral reversal.
   - It nearly doubles the whole-sample IC20, from +0.027 to +0.048.
   - It is the first form to show a **net-of-cost** top-decile edge. In range that edge is +0.66 pts
     per 20 sessions (t 2.4). In trend_down it is +1.68 (t 3.7), but that rests on only about 9
     episodes.
   - It still **fails the #235 rule**. In range, fold A has t 2.00 (not above 2) and fold B 1.0. In
     trend_down the folds have t 0.9 and 1.6. Over the whole sample they have 2.5 and 1.3.
5. **The defensive blend (`blend_def`) adds nothing over the reversal blend.** Mixing in lowbeta
   weakens it in every regime.
6. **Quality could not be tested**, because there is no point-in-time fundamentals history.

## Verdict

**No new candidate ranks stocks in range or trend_down markets.** Low beta, sector-neutral low
volatility, sector-neutral 52-week-high proximity and 60-day relative strength vs the sector are all
flat or backwards. None passes the #235 rule. **Production is unchanged, and the low-information
disclaimer stays.**

The only live lead is the one already logged: sector-neutral reversal, used as a regime switch. This
study adds two facts about it:
- it clears costs on the long-only top decile in range (t 2.4);
- it still fails the two-fold rule.

That supports keeping the prospective re-test already scheduled for late 2027. That re-test should
use no tuning and take `blend_rev` / rev20_within as its primary form. It does not support a
production proposal now.

## Reproduce

```
git archive origin/main | tar -x -C <snapshot>      # then copy observations.csv into <snapshot>/research/output
cd <snapshot> && py -m research.falling_market_ranking
```

Outputs are `research/output/fallmkt_{ic,folds,cost}.csv`, plus `fallmkt_prices.pkl` (the price cache).
