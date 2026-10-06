# Research log: what has been tested, and what we concluded

One entry per study, newest first. Each entry records the question, the data,
the answer and the decision, and links to the full report and script, so that
nobody re-runs a dead end or forgets a live lead. Educational research only:
nothing here is investment advice.

**How to add an entry:** append a row to the index, then add a section below it
using the same headings. Give the data vintage (main commit, period, obs/tickers)
so the result can be reproduced. Record negative results too, because they are
the most useful entries for avoiding repeat work.

## Standing conventions

- Generate observations from a frozen `git archive origin/main` snapshot, never
  from a worktree being edited.
- Report **per-week** (cross-sectional) IC with Newey-West t or a block-bootstrap
  CI. Pooled-observation p-values overstate significance, because the 20- and
  60-session windows overlap.
- Always state distinct dates and **episodes** per regime. Count runs of at
  least 5 sample dates, because the regime label flickers.
- **Adoption rule** (fixed in advance in #235): a variant must beat score90 with
  t > 2 in **both** alternating-calendar-quarter folds. It must also clear the
  golden-snapshot regression and a net-of-cost check before production.
- Universe is today's nifty500 list, so every result carries survivorship bias.
  Small and illiquid names are affected most.

## Index

| date | study | answer | decision | report |
|---|---|---|---|---|
| 2026-10-06 | Sector-neutral reversal (#299) | Range reversal is within-sector selection: rev20 minus sector median IC20 +0.051 (t 3.75, 71% weeks +, 8/9 years); sector part −0.022 (laggard sectors don't bounce; sector-level IC −0.053, t −1.5); still fails #235 rule (A t 1.97, B t 1.00) | Production unchanged; any future reversal re-test should use the sector-neutral form | [REVERSAL_SECTOR_NEUTRAL_2026-10-06](REVERSAL_SECTOR_NEUTRAL_2026-10-06.md) |
| 2026-10-06 | Range-regime reversal variant: costs, blend, liquidity, #235 rule | Direction consistent in both folds but no variant reaches t>2 in both; net-of-cost edge not significant (rev20 top-decile +0.51 pts/20s, t 1.6; liquid half +0.29, t 0.8); composite top decile lags range weeks net (−0.56, t −2.1) | Production unchanged, keep disclaimer; re-run after ~50 more range weeks | [REVERSAL_VARIANT_2026-10-06](REVERSAL_VARIANT_2026-10-06.md) |
| 2026-10-06 | Alt factors by regime (#294) | Only short-term reversal beats the composite outside trend_up (range IC20 +0.043, t 2.8); gross edge ~1 pt per 20 sessions | Variant study; keep disclaimer | [REGIME_ALT_FACTORS_2026-10-06](REGIME_ALT_FACTORS_2026-10-06.md) |
| 2026-10-06 | Momentum vol-scaling (#290) | No improvement | Production unchanged | `research/score_variants_momentum_vol.py` |
| 2026-10-01 | Decay attribution (#235) | Score fade = more range/down weeks, not a broken signal; no trend_down/range variant (incl. reversal) passed the 2-fold rule | Low-info regime caption | [DECAY_ATTRIBUTION_2026-10-01](DECAY_ATTRIBUTION_2026-10-01.md) |
| 2026-10-01 | Sector vs stock (#232) | How much of the score's edge is sector timing vs stock selection (see report) | — | [SECTOR_VS_STOCK_2026-09-30](SECTOR_VS_STOCK_2026-09-30.md) |
| 2026-09-28 | Bear-regime Technical + RS neutral | Both rank backwards in bear weeks; neutralising helps in both halves | Adopted (`NSE_USE_REGIME_WEIGHTS`) | [SCORE_EFFICACY_BEAR_2026-09-28](SCORE_EFFICACY_BEAR_2026-09-28.md) |
| 2026-09-26 | ADX direction (#172) | Slightly worse ranking | Not merged | [SCORE_EFFICACY_ADX_DIRECTION_2026-09-26](SCORE_EFFICACY_ADX_DIRECTION_2026-09-26.md) |
| 2026-09-24 | Score efficacy re-run (#167) | Weak positive IC (+0.020 at 20d, t 2.6), concentrated in trend_up | — | [SCORE_EFFICACY_2026-09-24](SCORE_EFFICACY_2026-09-24.md) |
| earlier | Fundamental quality | Revenue growth strongest signal (ρ ≈ +0.14 at 6m); ROE/leverage counter-productive 2022–25 | Revenue growth shown as observation, not blended into the score | [FUNDAMENTAL_QUALITY_REPORT](FUNDAMENTAL_QUALITY_REPORT.md) |
| earlier | 5-year regime study | Score measures trend quality (+0.40), not returns (+0.02–0.03) | Framing | [REGIME_STUDY_REPORT](REGIME_STUDY_REPORT.md) |
| earlier | Score variants: pattern / oversold-RSI | Pattern is dead weight; oversold-RSI is a trade-off | Pattern removed | [RESEARCH_SCORE_VARIANTS](RESEARCH_SCORE_VARIANTS.md) |

## Reference numbers (10y production-mode efficacy, origin/main @ 701a6b9, 2026-10-06)

177,289 obs, 483 nifty500 tickers, 2017-07 → 2026-07. Composite score90:

| live_regime | sample dates | episodes ≥5 dates | pooled IC20 | weekly IC20 (NW t) |
|---|---|---|---|---|
| trend_up | 1268 | 35 | +0.088 | +0.039 (3.8) |
| range | 477 | 26 | +0.019 | −0.011 (−0.6) |
| risk_off | 273 | 12 | +0.028 | +0.021 (2.4) |
| trend_down | 169 | 9 | +0.001 | +0.037 (2.0) |

## Open leads and ideas not yet tested

- **Point-in-time universe.** No historical index-membership list exists in the
  repo, so every study carries survivorship bias. Sourcing NSE's historical
  constituent files would make every result above more trustworthy.
- **Revenue growth by regime.** It is the strongest fundamental signal found, but
  it has never been split by live_regime. Its point-in-time data starts 2022-10,
  so the regime samples will be small.
- **Range-regime reversal, prospective re-test.** Re-run `research/score_variants_reversal.py`
  after about 50 more range weeks have accumulated (roughly late 2027), using the same rule and
  no tuning. Test the sector-neutral form (`research/score_reversal_sector_neutral.py`) as
  the primary candidate; #299 found it cleaner than raw rev20. See [REVERSAL_VARIANT_2026-10-06](REVERSAL_VARIANT_2026-10-06.md).
