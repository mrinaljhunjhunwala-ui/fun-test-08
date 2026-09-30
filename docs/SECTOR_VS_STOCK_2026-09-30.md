# Does the composite score pick sectors or stocks? — historical replay, 2026-09-30

**Why this study:** the live Verdict Calibration page (5-session horizon, ~6 days of
data) suggested most of STRONG BUY's edge over NIFTY came from the pick's *sector*
rather than the stock beating its sector. That live sample is tiny and is reserved
for the pre-registered October test, so it must not be used for tuning. This study
asks the same question on **history** instead.

**Method** (`research/sector_vs_stock.py`, read-only w.r.t. the model):

- The **production** composite score is replayed walk-forward, weekly, over the
  app's universe (483 usable tickers), **with production regime handling** (the
  live regime label and the bear-regime scoring on by default since #168/#198).
- Each forward return is split: **market** (universe median) + **sector part**
  (sector median − market) + **stock part** (stock − its sector median).
- **Sector timing IC**: per week, rank correlation between each sector's mean score
  and its forward sector part. **Stock selection IC**: per week, rank correlation
  between score and stock part. Averaged over weeks, with t-stats over weeks.
- **Spread**: top-minus-bottom score quintile forward return, split into its sector
  and stock parts.
- **Hold-out**: weeks whose forward window reaches 2026-08-29 (the first live
  verdict) are excluded, so nothing here overlaps the pre-registered test.

Reading guide: an IC of 0.03–0.05 is typical of a useful equity signal; |t| ≥ 2 is
unlikely by chance (t-stats are optimistic at 20/60 days because weekly samples
overlap).

## Results — 5 years (919 weeks)

| Slice | Horizon | Sector-timing IC (t) | Stock-selection IC (t) | Spread | of which sector | of which stock |
|---|---|---|---|---|---|---|
| **All** | 20d | 0.014 (0.9) | 0.008 (1.2) | 1.03 | 0.42 | 0.61 |
| **All** | 60d | 0.017 (1.0) | 0.012 (2.0) | 1.94 | 0.83 | 1.11 |
| Trend up | 20d | 0.062 (2.9) | 0.019 (2.5) | 1.68 | 0.70 | 0.99 |
| Trend up | 60d | 0.049 (2.2) | 0.034 (4.2) | 3.52 | 1.47 | 2.05 |
| Risk-off | 20d | 0.137 (2.4) | 0.075 (3.6) | 2.81 | 0.74 | 2.07 |
| Trend down | 20d | −0.036 (−1.0) | −0.028 (−1.4) | −0.64 | −0.23 | −0.41 |
| Trend down | 60d | −0.024 (−0.6) | −0.019 (−1.1) | −1.46 | −0.74 | −0.72 |
| Range | 60d | −0.026 (−0.8) | −0.022 (−2.4) | −0.07 | 0.23 | −0.30 |
| First half | 60d | 0.076 (3.1) | 0.029 (3.6) | 3.40 | 1.35 | 2.05 |
| **Second half** | 60d | 0.000 (0.0) | −0.003 (−0.3) | 0.65 | 0.36 | 0.28 |

The 2-year run (171 weeks) agrees on direction: positive in trend-up weeks
(sector IC 0.22, t 4.0 at 20d; stock IC 0.07, t 3.4 at 60d), flat to negative in
trend-down weeks, and weak overall.

## What it says

1. **"Sectors, not stocks" is not supported historically.** Across 5 years the
   score's top-vs-bottom spread comes from *both*: roughly 40% sector, 60% stock
   selection at 20–60 days. The live read-out was a handful of days in one falling
   market — too small to generalise.
2. **The score's edge depends heavily on the market regime.**
   - **Trend up:** it works on both counts (sector timing and stock selection,
     t 2–4).
   - **Risk-off:** with the bear-regime scoring that production now uses, it also
     works (in the pre-#168 replay this regime was *inverted* — that fix did its
     job).
   - **Trend down and range:** no edge, slightly negative. This is the regime the
     live ledger has been sampling since late August, which fits the weak live
     numbers.
3. **The bigger concern is decay, not sector vs stock.** The signal is clearly
   present in the older half of the 5-year window and **close to zero in the more
   recent half**, at every horizon. Something in the last ~2.5 years — market
   structure, or a component that stopped working — has eroded it.

## Suggested next research (none of this changes scoring yet)

- **Explain the decay:** rerun the per-component attribution
  (`research/score_efficacy.py` factor attribution) split by first/second half to
  see which pillar (technical / momentum / volume / RS) lost its edge.
- **Trend-down / range weeks** are where the score adds nothing today; a
  regime-specific variant should be tested there the way #168 was for risk-off —
  on history, with a train/holdout split, before any production change.
- **Descriptive UI idea** (no scoring change): show "historically low-information
  regime" next to verdicts logged in trend-down/range markets, so readers weigh
  them accordingly.
- Keep the pre-registered live test untouched; if a variant is adopted later, the
  live evidence count restarts from its deploy date.

## Limits

Today's universe (survivorship); historical scores lack the live delivery-% input;
baskets are medians of the app's universe, not official indices; small sectors
blur the split (their basket is partly the scored stocks themselves); overlapping
weekly windows make 20/60-day t-stats optimistic.

Reproduce: `py -m research.sector_vs_stock --period 5y --tag 5y` (and `--period 2y`);
`--no-regime` reproduces the pre-#168 behaviour. Outputs go to `research/output/`
(gitignored).

*Research only — describes the model's historical behaviour; not investment advice.*
