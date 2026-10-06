# Range-regime reversal variant: costs, blend, liquidity and the #235 rule (2026-10-06)

Research only. `analysis/score.py` and the golden files are unchanged.
This is educational material, not investment advice.

## Question

[REGIME_ALT_FACTORS_2026-10-06](REGIME_ALT_FACTORS_2026-10-06.md) (#294) found that the
20-session reversal (`rev20`, minus the 20-session return) was the only simple factor with a
positive weekly IC20 in `range` regimes. Its IC20 was +0.043 (t 2.8), against −0.011 for the
composite. Before the composite could use it, four questions needed an answer:

1. Does it **survive costs**?
2. Does it help when **blended into the score**?
3. Does it **hold among liquid names**? This serves as a survivorship proxy.
4. Does it pass the adoption rule fixed in advance in **#235**? That rule requires beating
   score90 with t > 2 in **both** folds of alternating calendar quarters. Reversal already
   failed it on a 5-year sample.

## Answer

**No. Nothing changes in production, and the low-information disclaimer stays.**
The reversal signal points the right way in both folds, but no variant reaches t > 2 in both.
After costs, any edge in range markets is statistically indistinguishable from zero, and it
is weaker still among liquid stocks.

## Data

- Observations: the same frozen snapshot as #294 (`origin/main` @ 701a6b9, 10y, `--regime-weights`).
- Prices were re-fetched by `research/score_variants_reversal.py`. Yahoo returned URLError for
  6 tickers, so 174,686 of 177,289 rows were used, including 37,582 in range markets.
- Cost: 0.30% per round trip per name per leg, the same as `COST_ROUNDTRIP_PCT`.
- Portfolios use the top decile, rebalanced every 4th week so that periods don't overlap. All
  four phase offsets are averaged, and t-stats are computed over the non-overlapping periods.

## Variants (applied only on rows labelled `range`; other regimes keep the composite)

| variant | definition |
|---|---|
| score90 | production composite |
| REVR | momentum pillar (0–25) replaced by 25 × the week's cross-sectional percentile of rev20 |
| HALF | momentum pillar replaced by 50% momentum + 50% of that reversal term |
| rev20 | the raw factor alone (reference) |

## 1. Weekly IC in range regimes (NW t)

| variant | all IC20 (t) | all IC60 (t) | liquid-half IC20 (t) | liquid-half IC60 (t) |
|---|---|---|---|---|
| score90 | −0.010 (−0.6) | 0.027 (1.6) | −0.019 (−0.9) | 0.005 (0.2) |
| REVR | 0.027 (2.2) | 0.037 (3.7) | 0.017 (1.2) | 0.021 (1.6) |
| HALF | 0.006 (0.4) | 0.037 (2.6) | −0.002 (−0.1) | 0.014 (0.6) |
| rev20 | 0.042 (2.7) | 0.013 (1.0) | 0.041 (2.1) | 0.019 (1.0) |

Over all regimes, REVR lifts the composite's weekly IC20 from 0.027 (t 3.6) to 0.035 (t 5.5).
Only range rows change, so the whole lift comes from range weeks.

## 2. After costs: top decile, non-overlapping 20-session periods (points per 20 sessions)

In the table, "excess" means the return above the equal-weight average of that week's stocks,
and L/S means long-short.

| universe | variant | excess gross | excess net | t | L/S net | t | turnover | net range across offsets |
|---|---|---|---|---|---|---|---|---|
| all | score90 | −0.31 | −0.56 | −2.1 | −0.43 | −1.1 | 83% | −0.96 … −0.20 |
| all | REVR | 0.25 | −0.03 | −0.1 | 0.08 | 0.3 | 91% | −0.84 … 0.49 |
| all | HALF | −0.30 | −0.55 | −2.3 | −0.55 | −1.6 | 84% | −1.43 … −0.06 |
| all | rev20 | 0.78 | 0.51 | 1.6 | 0.67 | 1.3 | 89% | 0.27 … 0.72 |
| liquid half | score90 | −0.40 | −0.65 | −2.2 | −0.80 | −1.6 | 83% | −1.18 … −0.25 |
| liquid half | REVR | 0.02 | −0.26 | −1.1 | −0.14 | −0.4 | 90% | −0.77 … 0.23 |
| liquid half | rev20 | 0.56 | 0.29 | 0.8 | 0.74 | 1.3 | 89% | −0.42 … 0.85 |

- Every variant replaces roughly 85–90% of its top decile every 20 sessions.
- The **composite's own top decile lagged the range-week cross-section net of costs**, by about
  −0.56 pts (t −2.1) per 20 sessions. This is why the disclaimer is warranted.
- Raw rev20 is the only line with a positive net excess (+0.51), but at t 1.6 it is not
  significant. Among liquid names it is +0.29 (t 0.8), and its sign depends on the rebalance
  offset (−0.42 to +0.85).
- Blending reversal in as REVR brings the composite's net excess up to about zero. That is
  better than the composite, but it is still not a positive edge.

## 3. Adoption rule from #235: paired IC20 difference vs score90, range rows

| universe | variant | fold A (t) | fold B (t) | passes |
|---|---|---|---|---|
| all | REVR | +0.039 (1.4) | +0.035 (1.3) | no |
| all | HALF | +0.016 (1.9) | +0.016 (1.6) | no |
| all | rev20 | +0.078 (1.7) | +0.031 (0.7) | no |
| liquid half | REVR | +0.052 (1.4) | +0.022 (0.6) | no |
| liquid half | HALF | +0.021 (2.1) | +0.012 (1.0) | no |
| liquid half | rev20 | +0.117 (2.1) | +0.009 (0.2) | no |

Every difference is positive in both folds, unlike #235's screen, where results flipped sign
between folds. So the direction is consistent, but the evidence is underpowered: about 50–60
range weeks per fold.

## 4. Range IC20 by year: V-shaped years

| variant | 2018 | 2019 | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 |
|---|---|---|---|---|---|---|---|---|---|
| score90 | 0.006 | −0.033 | 0.172 | 0.163 | −0.014 | −0.004 | −0.054 | −0.072 | 0.061 |
| REVR | −0.000 | 0.084 | 0.061 | 0.078 | 0.013 | 0.002 | −0.001 | −0.006 | 0.065 |
| rev20 | 0.031 | 0.119 | −0.150 | 0.026 | 0.020 | 0.037 | 0.068 | 0.024 | 0.022 |

In the 2020–21 V-shaped rebound, momentum dominated even in range weeks: the composite scored
0.17 while rev20 scored −0.15. REVR gives up most of that gain. It wins in 2019 and 2022–25,
when the composite was negative.

## Decision and what would change it

- **Production stays unchanged. Keep the range and trend_down disclaimer.** No variant passes
  the pre-registered rule, and no edge survives costs at a significant level.
- **Status: a live but underpowered lead, not a dead end.** The direction is consistent across
  folds, liquidity buckets and 6 of 9 years. Re-run this script when about 50 more range weeks
  have accumulated, which is roughly a year at the recent range share, or once a point-in-time
  universe exists. Use:
  `py -m research.score_variants_reversal --obs <fresh observations.csv>`
- Don't re-run it sooner, and don't loosen the rule. Repeated testing on the same data until
  something passes would be p-hacking.

## Reproduce

```bash
py -m research.score_efficacy --period 10y --regime-weights --workers 6
py -m research.score_variants_reversal --obs research/output/observations.csv
```

Run both from a frozen `git archive` snapshot. Outputs go to `research/output/`:
`revvar_ic.csv`, `revvar_portfolio.csv`, `revvar_folds.csv`, `revvar_by_year.csv`, and
`revvar_features.csv`, which `--features` can reuse to skip the re-fetch.
