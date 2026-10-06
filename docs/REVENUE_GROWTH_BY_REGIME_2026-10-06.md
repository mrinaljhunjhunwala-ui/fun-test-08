# Revenue growth by live regime — 2026-10-06

**Research only. No production change. Educational, not investment advice.**

## Question

`docs/FUNDAMENTAL_QUALITY_REPORT.md` ranked point-in-time revenue growth as the strongest signal in the research program (pooled ρ ≈ +0.14 vs 6-month forward return). Does it keep ranking stocks within a date when the **live** NIFTY regime label (`analysis.regime.classify_history` on ^NSEI + ^INDIAVIX) is `range`, `trend_down` or `risk_off`? In those regimes the price-based composite carries little information.

## Method

- Script: `research/fundamental_growth_regime.py` (pure, no Streamlit). It imports `_fetch_statements` / `_metrics_at` from `research/fundamental_quality.py` without changing them, so the 180-day lag and CAGR definition are the same.
- Universe: today's nifty500 (504 names). 482 had ≥300 days of closes; 22 newer listings were dropped. All 504 returned statements (Screener.in had a few read time-outs, so those tickers fell back to the next provider).
- Sampling: monthly (every 21 sessions), 2022-10-03 → 2026-04-01. That gives **42 sample dates** with 366–482 stocks per date (mean 420 for rev_g). rev_g is non-null on 96% of rows.
- Regime label: the live label at each sample date. It uses rolling windows only, so there is no look-ahead.
- Statistics: we compute a cross-sectional Spearman IC per date against the 3m (63-session) and 6m (126-session) forward return. We then take the mean over dates, a Newey-West t with lags = horizon/21 (3 and 6), and a 95% circular block-bootstrap CI with block = horizon/21. Significance is never computed on pooled observations.
- "Episodes" counts runs of consecutive sample dates that carry the same label.
- The panel was built from a frozen `git archive` snapshot, not the working tree.

## How many dates each regime has

| regime | sample dates | episodes |
|---|---|---|
| trend_up | 29 | 3 |
| range | 10 | 5 |
| trend_down | 2 | 1 |
| risk_off | 1 | 1 |
| ALL | 42 | 10 |

`trend_down` and `risk_off` cannot be tested. With one or two dates in a single episode, there is no t-statistic and no CI.

## Revenue growth: per-date IC by regime

| horizon | regime | dates | episodes | mean IC | NW t | 95% block CI | hit % |
|---|---|---|---|---|---|---|---|
| 3m | ALL | 42 | 10 | −0.021 | −1.08 | [−0.058, +0.012] | 50 |
| 3m | trend_up | 29 | 3 | −0.038 | −1.51 | [−0.086, +0.007] | 38 |
| 3m | range | 10 | 5 | +0.012 | +0.87 | [−0.005, +0.031] | 80 |
| 3m | trend_down | 2 | 1 | +0.005 | n/a | n/a | 50 |
| 3m | risk_off | 1 | 1 | +0.097 | n/a | n/a | 100 |
| 6m | ALL | 42 | 10 | −0.022 | −0.79 | [−0.078, +0.028] | 52 |
| 6m | trend_up | 29 | 3 | −0.048 | −1.53 | [−0.111, +0.003] | 45 |
| 6m | range | 10 | 5 | +0.019 | +0.54 | [−0.030, +0.076] | 60 |
| 6m | trend_down | 2 | 1 | +0.064 | n/a | n/a | 100 |
| 6m | risk_off | 1 | 1 | +0.135 | n/a | n/a | 100 |

**Quintiles, 6m.** Each value is the mean forward return per quintile within a date, then averaged across dates.

| regime | dates | Q1 | Q2 | Q3 | Q4 | Q5 | Q5−Q1 | monotone steps |
|---|---|---|---|---|---|---|---|---|
| ALL | 42 | 16.5 | 13.1 | 12.9 | 13.6 | 13.1 | −3.4 | 1/4 |
| trend_up | 29 | 16.1 | 13.0 | 12.1 | 11.9 | 10.7 | −5.4 | 0/4 |
| range | 10 | 18.7 | 13.9 | 15.2 | 19.0 | 18.1 | −0.6 | 2/4 |

**By year (6m, ALL):** 2022 −0.015 (3 dates) · 2023 −0.120 (12) · 2024 +0.013 (12) · 2025 0.000 (12) · 2026 +0.131 (3).

**Train / holdout (split 2024-07-01, 6m):**

| regime | train mean IC (t) | holdout mean IC (t) |
|---|---|---|
| ALL | −0.069 (−1.97), 21 dates | +0.024 (+1.16), 21 dates |
| trend_up | −0.073 (−1.87), 17 dates | −0.014 (−0.54), 12 dates |
| range | −0.056, 4 dates | +0.070 (+3.24), 6 dates |

The sign of the range result flips between train and holdout, so it is not stable.

**Comparators, 6m ALL:** EPS growth +0.012 (t 0.68). ROE −0.017 (t −1.06). ROE is on only 30 dates because coverage is thinner. Neither ranks stocks in any regime.

## The headline result does not reproduce

On this panel, revenue growth's IC against 6m forward return is −0.029 pooled and −0.022 as a per-date mean, not +0.14. The quintiles are not monotone either: Q1 (lowest growth) did best. These are the likely reasons, and they were not separated here:

1. **Universe.** This run uses today's nifty500. The earlier report used the `variant_observations.csv` ticker grid.
2. **Window.** This run samples up to last − 126 sessions, so it includes 2025–26 dates. The earlier report stopped at last − 252.
3. **Fundamentals source.** The engine now tries Screener.in ahead of yfinance, so the depth and values of statement history may differ from the original run.
4. **Pooled vs per-date.** A pooled ρ mixes time-series co-movement into the cross-sectional effect. The per-date IC is the honest test of whether revenue growth ranks stocks.

## Caveats

- Statements may be restated. Providers serve current values, not the as-reported figures.
- The 180-day lag is conservative and fixed. It is not tied to actual filing dates.
- Survivorship: the universe is today's index constituents.
- Few dates per regime: range has 10 dates in 5 episodes, trend_down 2, risk_off 1. Adjacent monthly 6m windows overlap heavily.
- Within a regime, the IC dates are not contiguous, so the Newey-West and block-bootstrap results are approximate.

## Recommendation

**The evidence is too thin, and the signal does not hold outside uptrends. It does not hold inside them either on this panel.** Revenue growth shows no reliable cross-sectional ranking in any regime:

- Every CI includes zero.
- The range-regime sign flips between train and holdout.
- trend_down and risk_off have one episode each and cannot be tested.

Revenue growth is **not** a candidate regime-robust signal for a variant or display study at this time. A prerequisite is to reconcile the earlier +0.14 headline with this per-date null: re-run `fundamental_quality.py` on the same universe and window, and report its per-date IC. No production change.

Reproduce: `py -m research.fundamental_growth_regime`, or `--from-cache` once `research/output/fgr_panel.csv` exists.
