# Range-regime reversal: within-sector or sector rotation? (2026-10-06)

Script: `research/score_reversal_sector_neutral.py`. Inputs: `research/output/observations.csv` (10y, nifty500, 174,686 rows after the merge) plus the cached `revvar_features.csv` (rev20 = minus the 20-session return). No prices were re-fetched. **Production scoring is unchanged.** This is educational research, not advice.

## Question

In `range` regimes, rev20 has weekly IC20 of about +0.042 (NW t 2.7) and score90 has about -0.010. Does that come from picking stocks within a sector, or from last month's losing *sectors* bouncing?

## Decomposition

Weeks are W-FRI periods. Per week x sector (sectors with at least 5 stocks that week; 3.8% of range rows dropped):

- `rev20_within` = rev20 minus the week x sector median, which makes it sector-neutral.
- `sector_rev` = that sector median, given to every member. This is pure sector rotation.
- `rev20_filt` = raw rev20 on the same filtered sample. `score90_within` = score90 minus its week x sector median.

The stock-level IC needs at least 30 stocks in a week. NW lags are 4 (IC20) and 12 (IC60). The CI is a block bootstrap (block = 4/12, 1000 draws). Train and holdout are the first and second halves of the weeks.

## Range regime: stock-level weekly IC (112-113 distinct weeks)

| factor | IC20 | NW t | 95% CI | % wks > 0 | train / holdout IC20 (t) | IC60 | NW t |
|---|---|---|---|---|---|---|---|
| rev20 (raw) | +0.042 | 2.71 | [+0.012, +0.073] | 58 | +0.052 (2.15) / +0.032 (1.73) | +0.013 | 0.95 |
| rev20_filt | +0.040 | 2.54 | [+0.008, +0.072] | 57 | +0.049 (1.97) / +0.031 (1.64) | +0.010 | 0.67 |
| **rev20_within** | **+0.051** | **3.75** | [+0.024, +0.076] | **71** | +0.066 (3.19) / +0.037 (2.25) | +0.022 | 1.73 |
| sector_rev | -0.022 | -1.41 | [-0.052, +0.012] | 40 | -0.034 (-1.38) / -0.011 (-0.58) | -0.028 | -1.69 |
| score90 | -0.010 | -0.61 | [-0.045, +0.019] | 50 | -0.001 (-0.04) / -0.019 (-0.80) | +0.027 | 1.62 |
| score90_within | -0.019 | -1.48 | [-0.046, +0.005] | 42 | -0.015 (-0.84) / -0.023 (-1.25) | +0.015 | 1.15 |

Other regimes show the same pattern (full table in `altfsn_ic.csv`). rev20_within IC20 is +0.067 (t 2.83) in risk_off, +0.075 (t 2.84) in trend_down and 0.000 in trend_up. sector_rev is negative in every regime, and strongly so in trend_up (-0.043, t -4.34). That means sector *momentum* persists there, not reversal.

## Sector-level test (sector median rev20 vs sector median fwd return, Spearman across sectors per week)

| regime | horizon | weeks | IC | NW t | 95% CI | % > 0 | sectors/wk |
|---|---|---|---|---|---|---|---|
| range | fwd_20d | 137 | -0.053 | -1.47 | [-0.114, +0.024] | 45 | 18 |
| range | fwd_60d | 137 | -0.016 | -0.36 | [-0.071, +0.079] | 47 | 18 |
| ALL | fwd_20d | 454 | -0.059 | -2.78 | [-0.097, -0.019] | 43 | 18 |
| trend_up | fwd_20d | 293 | -0.084 | -3.56 | [-0.122, -0.035] | 39 | 18 |

Last month's losing sectors do not bounce in range weeks. The sign is the wrong way, although with 18 sectors per week the test is noisy.

## #235 two-fold rule (paired weekly IC20 diff vs score90, range, alternating quarters, NW t 4 lags)

| factor | fold A (wks, diff, t) | fold B (wks, diff, t) | passes |
|---|---|---|---|
| rev20 | 51, +0.078, 1.72 | 62, +0.031, 0.69 | no |
| rev20_within | 51, +0.084, 1.97 | 61, +0.039, 1.00 | **no** |
| sector_rev | 51, +0.026, 0.57 | 61, -0.047, -1.29 | no |

## Range IC20 by year

| factor | 2018 | 2019 | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 |
|---|---|---|---|---|---|---|---|---|---|
| rev20 | .031 | .119 | -.150 | .026 | .020 | .037 | .068 | .024 | .022 |
| rev20_within | .066 | .121 | -.090 | .047 | .010 | .030 | .068 | .026 | .053 |
| sector_rev | -.101 | .022 | -.165 | -.007 | .024 | .000 | -.008 | -.002 | -.052 |
| score90 | .006 | -.033 | .172 | .163 | -.014 | -.004 | -.054 | -.072 | .061 |

rev20_within is positive in 8 of 9 years. 2020 (the V-shaped recovery) is the one exception, and the loss is smaller than for raw rev20.

## Cross-section shape

In range weeks, the median week has about 322 stocks across 17 sectors (min 1, max 19; the extremes are thin weeks at the edges of the sample). 146 distinct range weeks have any row, 112 meet the 30-stock IC minimum, and 137 meet the 5-sector minimum for the sector test. The sector median explains only about 11% of the weekly cross-sectional variance of rev20, so most of rev20 is already within-sector.

## Caveats

- **Survivorship:** the universe is today's nifty500, and the repo has no point-in-time membership list. Survivors bias reversal upward, because losers that later recovered stayed in the index.
- **Sector labels are current, not point-in-time.** The sector split is coarse (about 18 buckets).
- 112 range weeks with overlapping 20-session windows is a small sample. NW t corrects the standard error but not the small number of independent regime episodes.
- No costs here. The earlier variant study (`docs/REVERSAL_VARIANT_2026-10-06.md`) found the raw spread close to round-trip costs.

## Conclusion

The range-regime reversal effect is **within-sector stock selection, not sector rotation**. Removing the sector median *raises* the IC20 from +0.042 to +0.051 (NW t 3.75, 71% of weeks positive, positive in both halves and in 8 of 9 years). The pure sector component carries a slightly *negative* IC (-0.022 stock-level; -0.053 at the sector level, t -1.47). So last month's laggard sectors keep lagging mildly instead of bouncing, and that part was diluting raw rev20. Even the cleaner sector-neutral signal still **fails the #235 two-fold rule** against score90 (fold t 1.97 and 1.00). It is also subject to survivorship and current-label caveats. This is a descriptive finding only, and production scoring is unchanged.
