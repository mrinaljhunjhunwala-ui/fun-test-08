# Score Efficacy — bear-regime Technical + RS neutral (2026-09-28)

**Question:** in bear regimes (trend_down / risk_off), the composite ranked stocks
backwards (docs/SCORE_EFFICACY_2026-09-24.md). Which pillars cause it, and does holding
them at neutral fix it without hurting other regimes?

**Answer:** Technical and RS rank backwards in bear weeks. Holding both at their neutral
midpoints (Technical 20/40, RS 5/10) during bear weeks improves the bear-week ranking in
**both** halves of a 10-year sample, at both 20 and 60 days. The change is **adopted**,
on the same opt-out flag as the #168 bear momentum mode (`NSE_USE_REGIME_WEIGHTS=0`).

## Method

- **Data:** `py -m research.score_efficacy --period 10y --regime-weights`, run on
  2026-09-28 with production scoring as of `main` at `52e2c60` (no scoring-path changes
  between it and `bde9fce`, which this branch is based on): 484 of 504 Nifty-500
  tickers usable, 178,482 weekly observations, 2017-07-17 to 2026-07-02.
  - `10y` is a new fetcher period (Yahoo `range=10y`, daily bars). `max` returns
    **monthly** bars from Yahoo, which silently cut an earlier attempt to 140 tickers.
- **Metric:** Spearman IC of the score against forward 20d / 60d returns, computed per
  ISO week (weeks with ≥ 50 names), then averaged; t = mean / (sd / √n).
- **Regime:** the modal `live_regime` of each week (`analysis.regime.classify_history`).
- **Variants:** evaluated by adjusting the recorded pillar columns. They are exact
  for this change: the pillars add up, and RS points are `RS_Score / 10`.

## Per-regime IC by component (10y, 20d)

| Regime (weeks) | Composite | Technical | Momentum | Volume | RS |
|---|---|---|---|---|---|
| trend_up (266) | +0.044 | +0.042 | +0.046 | +0.006 | +0.055 |
| range (99) | −0.011 | −0.007 | −0.011 | −0.022 | +0.008 |
| trend_down (33) | +0.003 | **−0.027** | +0.058 | −0.041 | **−0.036** |
| risk_off (58) | −0.022 | −0.018 | +0.004 | −0.007 | −0.035 |

Every trend-following baseline (20-day momentum, RSI) is also negative in bear weeks.
In those weeks, recent strength predicts worse returns.

## Variants, bear weeks only, split by period

Bear weeks per half: 40 in 2017–20 and 56 in 2021–26 (trend_down + risk_off).

| Variant | 20d 2017–20 | 20d 2021–26 | 60d 2017–20 | 60d 2021–26 | All weeks 20d |
|---|---|---|---|---|---|
| Current | −0.024 | −0.005 | +0.006 | −0.011 | +0.0209 (t 3.8) |
| Technical neutral | −0.030 | +0.037 | −0.027 | +0.019 | +0.0254 |
| RS neutral | +0.008 | +0.003 | +0.059 | −0.003 | +0.0245 |
| **Technical + RS neutral (adopted)** | **+0.026** | **+0.061** | **+0.029** | **+0.039** | **+0.0329 (t 6.4)** |
| Technical + RS + Volume neutral | +0.040 | +0.074 | +0.012 | +0.053 | +0.0355 |
| Technical flipped + RS neutral | +0.016 | +0.059 | −0.024 | +0.040 | +0.0318 |

**Why "Technical + RS neutral" and not the higher-scoring rows:**
- It was chosen on the earlier 2022–26 sample (`obs_base`, 26 bear weeks) **before** the
  10-year run existed. The 2017–20 half is therefore a genuine out-of-sample check, and
  it passes: every cell improves.
- The Volume and "flipped" rows were picked after seeing this data.
- Flipping Technical is a directional bet, and it hurts 60d in 2017–20.
- Neutralising only removes a signal that points the wrong way.

## Caveats

- **Survivorship:** the universe is today's Nifty 500, so older periods favour
  survivors.
- **Overlapping windows:** forward-return windows overlap across weekly samples, so the
  t-stats are optimistic. The same caveat applies to every earlier efficacy report.
- **risk_off vs trend_down:** the change treats them the same. risk_off still ranks
  slightly negative after the change in some slices; there are too few weeks to split
  them further.
- **Bear rankings are now mildly right, not strong.** The broad regime story is
  unchanged: the score works best in uptrends.

## What changed in code

- `analysis/score.py`:
  - **Technical:** when the regime is bear and `NSE_USE_REGIME_WEIGHTS` is not
    disabled, `score_dataframe` sets it to 20.0. The raw value is kept in the
    breakdown as `tech_pts_raw`, with `tech_neutralised=True`.
  - **RS:** `_score_momentum`'s bear branch sets it to 5.0, with `rs_pts_raw` and
    `rs_neutralised=True` in the breakdown.
- `CompositeScore.technical_raw` keeps the pre-neutralisation technical points. It is read by the
  thesis engine, so Bull/Bear/Risk bullets still describe the stock's real technicals, and by
  page 04's verdict-ledger tags, so `tech_high`/`tech_low` are still logged in bear regimes
  and the ledger can keep checking this decision. Both were flagged by the
  verdict-regression review.
- `data/fetcher.py`: new `10y` period.
- `data/composite_golden_snapshot.json`: only the three `__bear` cases changed.
