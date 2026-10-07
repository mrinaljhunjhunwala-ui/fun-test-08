# Revenue growth: why +0.14 became −0.02 (2026-10-07)

**Question.** `docs/FUNDAMENTAL_QUALITY_REPORT.md` (commit e0dd2c9, 2026-06-11) reported revenue growth
at pooled Spearman ρ ≈ +0.141 against 6-month forward return. PR #301
(`docs/REVENUE_GROWTH_BY_REGIME_2026-10-06.md`) found a per-date IC of about −0.02 on nifty500,
2022-10 → 2026-04. Both studies used the same `_fetch_statements` and `_metrics_at`. This note works out
why the numbers differ.

> Produced by `research/revenue_growth_reconcile.py`. The panel was rebuilt today: nifty500
> (504 tickers), monthly dates, 180-day point-in-time lag, the same `_growth_cagr`, statements fetched
> per provider and merged with the service's rules. Read-only, no production change, educational only.
> Regenerate with `py -m research.revenue_growth_reconcile` (or `--from-cache`).

## What changed between the two studies

1. **Statement source.** The original ran on Yahoo-only statements. The Screener.in merge-fill landed
   on 2026-08-30 (a5b607c), 11 weeks after the report. Today a median ticker has 4 Yahoo annual
   statements and 8 or more from Screener.
2. **Growth definition drifted with the source.** This is the main finding. `_growth_cagr` annualises
   from the **oldest** usable statement to the newest. With Yahoo's roughly 4 years of statements
   (the oldest year often field-sparse), that gave a 1–3 year growth rate. Once Screener added 10 or
   more years of history, the same code returns a **5–10 year CAGR**, which is a different factor.
   The code didn't change, but the input depth did.
3. **Window.** The original's revenue-growth dates were 2023-07 → 2025-05 (8 dates with at least 30
   names). #301 adds 2022-10 → 2023-06 (Screener history makes those dates computable) and
   2025-06 → 2026-04.
4. **Universe.** The original covered 209 tickers (whoever `score_variants` and Yahoo covered that
   day). Today's run covers nifty500.
5. **Statistic.** The original used a pooled Spearman across all rows. #301 used the mean of per-date
   ICs with a Newey-West t.

The rebuild independently reproduces the original's **8 usable dates** on Yahoo-only data, which
confirms the source attribution.

## Waterfall (per-date mean IC vs fwd 6m unless noted)

| Step | Source · definition · window · universe | Pooled ρ | Mean IC | Dates | NW t | Δ |
|---|---|---|---|---|---|---|
| 0 | **Original report** (Yahoo as of Jun-2026, 209 tk, orig window) | **+0.141** | +0.165 | 8 | n/a | |
| 1 | Yahoo **today**, CAGR, orig window, nifty500 | +0.071 | +0.076 | 8 | 2.61 | **−0.09** universe + Yahoo vintage/restatements (not separable) |
| 2 | … extend window to 2022-10 → 2026-04 | +0.034 | +0.044 | 17 | 1.78 | **−0.03** window (2025-26 weaker) |
| 3 | … switch to Yahoo+Screener merged, same code (long-history CAGR) | +0.005 | −0.037 | 41 | −1.19 | **−0.08** source depth → definition drift |
| 4 | #301 as reported (full Screener coverage) | | ≈ −0.02 | 41 | | +0.02 residual (this run got Screener for only 108/504 tickers; see limits) |

Definition controls on the merged panel (these isolate cause 2 from cause 1):

| Merged definition | Window | Mean IC | NW t | Dates |
|---|---|---|---|---|
| CAGR, full history (#301) | orig / full | −0.047 / −0.037 | −0.97 / −1.19 | 23 / 41 |
| Last 4 statements (Yahoo-depth) | orig / full | +0.025 / −0.005 | 1.21 / −0.21 | 23 / 41 |
| **YoY (last 2)** | orig / full | **+0.073 / +0.021** | **4.51** / 0.76 | 11 / 29 |
| Yahoo YoY | orig / full | +0.076 / +0.051 | 2.61 / 2.23 | 8 / 17 |

On the statistic: for the Yahoo variants, pooled and per-date results differ by 0.01 at most, so the
choice of statistic doesn't explain the gap. It does matter for the merged long CAGR in the original
window: pooled +0.024 against per-date −0.047. Pooling there picks up time-series co-movement, so
per-date ICs are the right measure.

On universe: on today's data, "Yahoo-covered" and "all" are effectively the same set, because Yahoo
returned statements for all 504 tickers. The 209-versus-500 effect can't be separated from Yahoo's
data vintage (see step 1).

## Verdict

- **The −0.02 is largely an artifact of definition drift.** Once Screener deepened statement history,
  `_growth_cagr` (oldest-to-newest) turned "recent revenue growth" into a 5–10 year CAGR. That long
  CAGR has no cross-sectional information here (IC ≈ −0.04, t ≈ −1).
- **The +0.14 was overstated but not fabricated.** On today's data, recent growth (Yahoo, or merged YoY)
  gives a per-date IC of **about +0.07 to +0.08** in the original window (NW t 2.6–4.5, 8–11 dates).
  That is about half the reported number. The rest comes from the original's 209-name universe and
  Yahoo's June-2026 statement values, which can't be reproduced now.
- **The signal is window-specific and fading.** Extending to 2022-10 → 2026-04 roughly halves it again
  (Yahoo YoY +0.051, t 2.2; merged YoY +0.021, t 0.8). It weakens in 2025–26.
- **Overall:** short-horizon revenue growth is a weak positive signal, around +0.02 to +0.05 IC over
  the full window. It is not the program's "strongest signal" at +0.14, and the 6-month forward
  windows overlap, so fewer than 10 periods are effectively independent. Any reuse should pin the
  definition to YoY (or a fixed lookback) rather than oldest-to-newest. #301's regime conclusions were
  drawn on the long CAGR, so they apply to that factor and not to recent growth.

## What can't be determined

- The original 209-ticker list and its observation CSV. `variant_observations.csv` and
  `fundamental_quality_observations.csv` no longer exist on any checkout, so step 1 can't be split
  into its universe and Yahoo-vintage parts.
- How much Yahoo restated values between June and October 2026.
- Exact parity with #301's merged panel. Screener.in timed out for about 400 of 504 tickers during this
  run (108 returned statements), so the merged rows here lean more on Yahoo than #301's did. The
  sign and size (−0.037 vs −0.02) still agree.
- Statistical weight: 8–41 monthly dates with 6-month overlapping returns. Treat every t-value as
  descriptive.
