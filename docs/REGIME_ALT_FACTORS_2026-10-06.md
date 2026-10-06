# Regime alt-factors study: what ranks NSE stocks in range and falling markets? (2026-10-06)

Research only. Production scoring (`analysis/score.py`) and the golden files are unchanged.
This is educational material, not investment advice. Every figure below is a historical
rank correlation across a stock universe. None of it says what to do with any single stock.

## Question

The composite score90 has a real rank IC20 in `trend_up` regimes (about +0.09) and close to
none elsewhere, so outside uptrends it carries a "low-information" disclaimer. This study asks
whether any simple, explainable price/volume factor ranks NSE stocks' forward 20- and 60-session
returns better than the composite in `range`, `trend_down` and `risk_off` regimes.

## Data and method

- **Observations.** `py -m research.score_efficacy --period 10y --regime-weights --workers 6`,
  run from a frozen `git archive` snapshot of `origin/main` @ 701a6b9. That gives 177,289 rows
  from 483 nifty500 tickers, 2017-07-27 → 2026-07-09. Each ticker is sampled every 5th session.
- **Factors.** Built by `research/score_regime_alt_factors.py` from re-fetched prices, each as of
  the observation date with no look-ahead. Signs are set so that a positive IC means
  "a higher factor value ranked better":

  | factor | definition |
  |---|---|
  | `score90` | production composite (the benchmark) |
  | `rs_score` | RS_Score from the observation row |
  | `rev5`, `rev20` | minus the 5 / 20-session return (short-term reversal) |
  | `lowvol` | minus the 60-session stdev of daily returns |
  | `near52wh` | close / 252-session high − 1 |
  | `mom12_1` | return from t−252 to t−21 |
  | `dma200` | close / 200-session SMA − 1 |
  | `turnover` | log of the 60-session mean of close × volume |

- **Fundamentals are excluded.** The only point-in-time source in the repo is
  `research/fundamental_quality.py`, which applies a 180-day lag to yfinance statements. It starts
  in 2022-10 and serves restated values. That is too short and too biased to split by regime.
  `fundamentals_historical_variant.py` is annual and has the same restatement problem.
- **Significance is computed per date, not pooled.** Observations are bucketed into weeks (W-FRI),
  because each ticker is sampled every 5th session counted from its own start date. For each
  regime the steps are:
  1. take the rows carrying that row's own `live_regime` label;
  2. compute a cross-sectional Spearman IC for each week (minimum 30 stocks per week);
  3. average the weekly ICs and report a **Newey-West t-stat** (lags = horizon in weeks: 4 or 12)
     and a **moving-block bootstrap 95% CI** (block = horizon in weeks, 1,000 resamples).

  Deciles are formed within each week on week-demeaned forward returns. The **d10−d1** spread is
  in percentage points of *gross* 20- or 60-session return. **mono** is the Spearman correlation
  between decile mean and decile number (+1 means perfectly monotonic). The train/holdout split
  is by week, at 2022-01-15. The pooled-observation IC is shown only to reconcile with
  score_efficacy.

## How thin is the evidence?

| regime | sample dates | episodes (runs) | episodes ≥5 dates | weeks with IC | obs | span |
|---|---|---|---|---|---|---|
| trend_up | 1268 | 70 | 35 | 280 | 103,271 | 2017-07 → 2026-01 |
| range | 477 | 69 | 26 | 113 | 38,142 | 2018-02 → 2026-07 |
| risk_off | 273 | 32 | 12 | 63 | 20,285 | 2019-04 → 2026-04 |
| trend_down | 169 | 26 | 9 | 44 | 15,591 | 2018-10 → 2026-07 |

An "episode" is a run of consecutive sample dates that share the same label. Many runs last only
1–3 dates because the label flickers, so the "≥5 dates" column is the honest count. trend_down
rests on about **9 real episodes**, and 2018–19 dominate it. risk_off rests on about **12**.
Treat any trend_down or risk_off result as a hypothesis, not a finding.

## Sanity check: the composite, pooled IC20 by `live_regime`

| regime | this run | brief | match |
|---|---|---|---|
| trend_up | +0.0878 (n=103.3k) | +0.090 (n=103k) | ✔ |
| range | +0.0189 (n=38.1k) | +0.004 (n=38.9k) | ≈ |
| risk_off | +0.0278 (n=20.3k) | +0.017 (n=21.8k) | ≈ |
| trend_down | +0.0007 (n=15.6k) | −0.029 (n=13.4k) | ≈ |

trend_up reproduces. The other three differ by 0.01–0.03, and their regime row counts differ by
up to 2k rows. That points to a different data vintage (re-fetched NIFTY/VIX history moves some
regime boundaries) or a different main commit (#288/#289 changed the volume points), not to a
bug in this script. Every difference sits well inside the weekly bootstrap CIs below. The
conclusion "the composite is ≈0 outside trend_up" holds in both runs.

## Results: mean weekly IC (NW t) [95% block-bootstrap CI]

### range (113 weeks)

| factor | IC20 | t | CI | d10−d1 | mono | train / holdout | IC60 | t |
|---|---|---|---|---|---|---|---|---|
| score90 | −0.011 | −0.6 | [−0.046, 0.019] | 0.13 | −0.02 | 0.005 / −0.022 | 0.026 | 1.5 |
| **rev20** | **0.043** | **2.8** | [0.011, 0.073] | 0.88 | **0.98** | 0.058 / 0.032 | 0.015 | 1.1 |
| rev5 | 0.030 | 2.7 | [0.008, 0.056] | 1.09 | 0.94 | 0.038 / 0.025 | 0.003 | 0.2 |
| mom12_1 | 0.030 | 1.2 | [−0.025, 0.075] | 0.78 | 0.88 | 0.058 / 0.010 | 0.043 | 1.9 |
| near52wh | 0.012 | 0.5 | [−0.037, 0.061] | −0.25 | 0.14 | 0.041 / −0.008 | 0.050 | 2.2 |
| dma200 | 0.002 | 0.1 | [−0.047, 0.045] | −0.16 | 0.41 | 0.038 / −0.023 | 0.044 | 1.9 |
| rs_score | 0.002 | 0.1 | [−0.048, 0.044] | −0.28 | 0.13 | 0.032 / −0.019 | 0.037 | 1.8 |
| lowvol | 0.020 | 0.9 | [−0.026, 0.067] | −0.19 | −0.41 | 0.049 / 0.000 | 0.007 | 0.2 |
| turnover | 0.015 | 1.1 | [−0.011, 0.039] | −0.26 | −0.41 | 0.054 / −0.014 | −0.002 | −0.1 |

### risk_off (63 weeks)

| factor | IC20 | t | CI | d10−d1 | mono | train / holdout | IC60 | t |
|---|---|---|---|---|---|---|---|---|
| score90 | 0.021 | 2.4 | [0.002, 0.038] | 0.44 | 0.47 | 0.011 / 0.047 | 0.025 | 4.7 |
| **rev5** | **0.049** | **3.0** | [0.019, 0.075] | 0.47 | 0.73 | 0.031 / 0.095 | 0.033 | 2.4 |
| **rev20** | **0.050** | **2.3** | [0.012, 0.092] | 1.31 | 0.53 | 0.040 / 0.076 | 0.028 | 1.3 |
| lowvol | −0.088 | −3.0 | [−0.142, −0.025] | −5.99 | −0.95 | −0.069 / −0.136 | −0.079 | −2.0 |
| turnover | −0.063 | −2.5 | [−0.121, −0.020] | −6.07 | −0.99 | −0.060 / −0.072 | −0.153 | −5.0 |
| near52wh | −0.063 | −1.7 | [−0.142, 0.007] | −4.00 | −0.87 | −0.066 / −0.056 | −0.057 | −1.1 |
| rs_score | −0.013 | −0.4 | [−0.090, 0.055] | −0.72 | −0.66 | −0.011 / −0.020 | −0.021 | −0.5 |
| dma200 | −0.016 | −0.4 | [−0.089, 0.052] | −1.28 | −0.44 | −0.011 / −0.030 | −0.030 | −0.7 |
| mom12_1 | −0.006 | −0.2 | [−0.083, 0.057] | −1.06 | −0.52 | −0.024 / 0.039 | −0.001 | −0.0 |

The negative `lowvol` and `turnover` ICs mean that the most volatile and least liquid stocks
showed the largest rebounds after risk_off weeks: a d10−d1 of about −6 pts at 20 sessions and up
to −20 pts at 60 sessions. That is a high-beta rebound effect, and survivorship inflates it. The
universe is *today's* nifty500, so the small, volatile names that never recovered are missing.
It is not a robust ranking signal.

### trend_down (44 weeks, ~9 real episodes)

| factor | IC20 | t | CI | d10−d1 | mono | train / holdout | IC60 | t |
|---|---|---|---|---|---|---|---|---|
| score90 | 0.037 | 2.0 | [0.009, 0.077] | 0.70 | 0.70 | 0.116 / 0.024 | 0.030 | 2.2 |
| **rev20** | **0.076** | 2.3 | [0.012, 0.133] | 2.64 | 0.88 | 0.205 / 0.056 | 0.045 | 1.4 |
| **rev5** | **0.066** | 2.7 | [0.025, 0.110] | 2.08 | 0.95 | 0.208 / 0.043 | 0.050 | 2.6 |
| mom12_1 | 0.025 | 1.1 | [−0.018, 0.072] | 0.22 | 0.60 | 0.020 / 0.026 | 0.053 | 2.4 |
| rs_score | −0.026 | −0.9 | [−0.074, 0.037] | −0.93 | −0.33 | −0.096 / −0.015 | 0.009 | 0.3 |
| dma200 | −0.033 | −1.1 | [−0.079, 0.034] | −1.13 | −0.47 | −0.067 / −0.028 | −0.008 | −0.2 |
| near52wh | −0.020 | −0.6 | [−0.074, 0.049] | −1.38 | −0.18 | −0.059 / −0.013 | −0.018 | −0.4 |
| lowvol | −0.004 | −0.1 | [−0.056, 0.063] | −0.54 | −0.60 | 0.005 / −0.005 | −0.070 | −2.6 |
| turnover | −0.002 | −0.1 | [−0.045, 0.025] | −1.26 | −0.50 | 0.063 / −0.012 | −0.055 | −3.6 |

The composite already scores trend_down/risk_off with the bear mean-reversion momentum variant,
which explains why it is positive here. Reversal is stronger but driven by 2018–19: train is
0.21 and holdout 0.04–0.06.

### trend_up (reference, 280 weeks)

score90 IC20 0.039 (t 3.8), with d10−d1 1.96 and mono 0.99. rs_score (0.054), mom12_1 (0.067),
near52wh (0.063) and dma200 (0.058) are all comparable or slightly higher, and the reversal
factors are ≤0 there. The composite does its job in trend_up.

## Mean weekly IC20 by year

| regime | factor | 2018 | 2019 | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 |
|---|---|---|---|---|---|---|---|---|---|---|
| range | score90 | 0.006 | −0.030 | 0.163 | 0.163 | −0.018 | −0.006 | −0.054 | −0.073 | 0.060 |
| range | rev20 | 0.034 | 0.115 | −0.144 | 0.016 | 0.023 | 0.039 | 0.072 | 0.025 | 0.022 |
| range | rev5 | 0.032 | 0.058 | −0.104 | 0.253 | 0.016 | 0.049 | 0.055 | 0.007 | 0.011 |
| risk_off | score90 | — | 0.018 | 0.006 | 0.022 | 0.050 | — | 0.074 | — | 0.030 |
| risk_off | rev20 | — | −0.115 | 0.082 | 0.001 | 0.016 | — | 0.124 | — | 0.189 |
| risk_off | rev5 | — | −0.050 | 0.051 | 0.015 | 0.055 | — | 0.160 | — | 0.158 |
| trend_down | score90 | 0.046 | 0.185 | — | — | 0.029 | — | — | 0.071 | −0.009 |
| trend_down | rev20 | 0.264 | 0.145 | — | — | 0.010 | — | — | 0.080 | 0.068 |
| trend_down | rev5 | 0.207 | 0.208 | — | — | 0.024 | — | — | 0.100 | 0.017 |

Full tables for all factors and years are in `research/output/altf_by_year.csv` after a run.

In range regimes, rev20 was positive in 8 of 9 years. The exception is 2020, when the post-COVID
range rewarded momentum. Over 2024–2026, the period covered by the current disclaimer, rev20
stays positive while the composite is negative or flat.

## Findings

1. **Nothing that follows the trend works outside trend_up.** rs_score, 12-1 momentum, 52-week
   high and the 200-DMA all have near-zero or negative weekly IC in range, risk_off and trend_down.
   The composite's lack of information in those regimes is not something a different trend factor
   would fix.
2. **Short-term reversal is the one consistent candidate.** rev20 and rev5 have positive weekly
   IC20 in all three non-uptrend regimes. Their NW t-stats range from 2.3 to 3.0, their bootstrap
   CIs exclude zero, they are positive in both the train and holdout halves, and in range their
   deciles are near-monotonic (0.94–0.98). In range markets this is the only factor that beats the
   composite (0.043 vs −0.011).
3. **The effect is economically small.** The d10−d1 gross spread is about 0.9–1.3 pts per 20
   sessions in range and risk_off, against the 0.30% round-trip cost that score_efficacy assumes.
   Reversal portfolios also turn over almost entirely each period. A net-of-cost spread could be
   close to zero in a long-only top-decile use.
   Worked out for range: rev20's d10−d1 is +0.88 pts gross per 20 sessions. A long-short
   rebalance pays a round trip on both legs, each ~0.30% (score_efficacy's `COST_ROUNDTRIP_PCT`;
   the #286 cost model gives ~0.29% at ₹2L). That leaves a **net edge of roughly 0.3 pts per 20
   sessions, before slippage and impact**. Any variant study must therefore report net-of-cost
   decile spreads and turnover, not only IC.
   Reversal can also hurt in V-shaped markets. In 2020, the range-regime IC20 for rev20 was
   strongly negative (−0.144), because the post-COVID range rewarded momentum. A tilt toward
   reversal that switches on by regime will be wrong in exactly those rebounds.
4. **The risk_off "low-vol" and "liquidity" effects run in reverse** (volatile and illiquid names
   rebounded). Survivorship in a current-constituents universe distorts them, so they should not
   be productised.
5. **The trend_down evidence is too thin to act on.** There are about 9 real episodes, and the
   2018–19 drawdowns dominate.

## Recommendation

**Short-term reversal (rev20, with rev5 as a sensitivity check) is worth a production *variant
study* for the `range` regime, and secondarily for `risk_off`.** The study should test:
- reversal blended into the momentum pillar only when `live_regime == range`, using the same
  machinery as `score_variants_regime.py`;
- net-of-cost decile returns and turnover, using the single cost model from #286;
- a point-in-time (survivorship-free) universe, if one becomes available;
- whether to raise the range-regime disclaimer at all. Until a variant clears the golden-snapshot
  regression and a net-of-cost test, **keep the current low-information disclaimer**.

Nothing in this study justifies changing the trend_down or risk_off handling.

## Caveats

- Universe is today's nifty500 constituents, so results carry survivorship bias. This mainly
  affects the lowvol and turnover results.
- Regime labels are recomputed from re-fetched NIFTY/VIX data, which explains the small mismatch
  with the brief's regime counts.
- All returns are gross. Spreads are equal-weighted across stocks within each week.
- Eight factors × three regimes × two horizons is 48 tests. With that many tests, one or two
  |t|>2 results are expected by chance. Reversal shows up consistently across all three regimes,
  both halves and both lookbacks, which is the main reason to take it seriously.

## Reproduce

```bash
py -m research.score_efficacy --period 10y --regime-weights --workers 6
py -m research.score_regime_alt_factors --obs research/output/observations.csv
```

Run both from a frozen `git archive` snapshot. Outputs: `research/output/altf_summary.csv`,
`altf_by_year.csv` and `altf_regimes.csv`.
