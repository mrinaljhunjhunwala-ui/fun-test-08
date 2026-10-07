# Regime trigger vs market breadth (2026-10-07)

**Question:** since about 7 Sep 2026 the app labels NIFTY `trend_down`, which turns on bear
scoring (Technical held at 20/40, RS at 5/10, momentum switched to 5-day reversal, so the
composite tops out at 65 and there are no BUY calls). Is that label too sticky or wrong, and
would a breadth-based trigger have switched bear scoring off in late Sep / Oct 2026?

**Answer:** the label is laggy but **not wrong** this time, and no trigger change passes the
two-fold rule. Breadth confirms the downtrend: only 27% of NSE EQ stocks are above their 50 DMA
(10-day mean 28%), 40% are above the 200 DMA, 52-week lows have outnumbered highs on every day from
28 Sep to 5 Oct, and NIFTY made a new low on 1 Oct. One strong day (6 Oct: 1,907 advances vs 738
declines) does not change any of those readings. A breadth trigger would have turned bear
scoring on **later** (16 Sep, not 7 Sep), not switched it off. **Production unchanged.**

Script: `research/regime_breadth_study.py`. Breadth helper: `analysis/market_breadth.py`
(pure, unit-tested in `tests/test_market_breadth.py`).

## 1. How the live label reaches trend_down

- `analysis/score.py:526` `_live_regime_label()` calls `analysis.regime.snapshot_live()`
  (cached 30 min). It is only consulted when `NSE_USE_REGIME_WEIGHTS` is not disabled
  (`score.py:2073`). Bear set: `{"trend_down", "risk_off", "bear"}` (`score.py:544`).
- `analysis/regime.py:395` `snapshot_live()` fetches `^INDIAVIX` (1mo) and `^NSEI` (2y) and
  passes **breadth = None** (comment at `regime.py:420`). So breadth never contributes live.
- `_trend_component` (`regime.py:93`): NIFTY close vs its 200-day SMA; below −3% means "below".
- `_combine` (`regime.py:158`): VIX ≥ 22 means `risk_off`. Otherwise "below" with breadth
  unknown means `trend_down` (`regime.py:185`), "above" means `trend_up`, and anything else
  means `range`.
- Net rule live: **trend_down ⇔ NIFTY < 0.97 × SMA200 and VIX < 22.** It has no smoothing
  and no confirmation. It flickered on 3 Sep, then stayed on from 7 Sep.
- To exit, NIFTY must close above 0.97 × SMA200 ≈ **23,578** (as of 7 Oct, NIFTY 22,603,
  low 22,422 on 1 Oct). That is about +4.3% from here.

**Lag** (episodes of 5 or more sessions, 2018 to 2026, `lag_current.csv`): from the NIFTY low
inside an episode to the label switching off took a median of **6.5 sessions** (range 1 to 21).
By then NIFTY was 1.5% to 7.7% off the low (typically 3% to 5%). The breadth label's lag is
similar (median 6.5 sessions, +2% to 4%). Over the last 2 years (499 sessions), the current
label read trend_down 139 times, range 179, trend_up 167 and risk_off 14.

## 2. NSE breadth, last 30 sessions (sec_bhavdata_full, EQ series, about 2,650 stocks)

The data runs from 2019-08-23 to 2026-10-07 (1,732 sessions; the archive 404s before
about Sep 2019). DMAs and 52-week tests run on a split-consistent index chained from
`CLOSE / PREV_CLOSE`. NSE adjusts PREV_CLOSE on ex-dates, so bonuses don't read as crashes.
NSE serves the previous day's file on some holidays (for example 2 Oct), so the script
rejects any file whose DATE1 differs from the requested date.

| date | adv | dec | A/D | %>20 | %>50 | %>200 | 52wH | 52wL | upVol% | current | breadth |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 08-25 | 1176 | 1431 | 0.82 | 44.1 | 49.8 | 57.1 | 59 | 16 | 58 | range | range |
| 08-26 | 1451 | 1135 | 1.28 | 48.2 | 51.3 | 57.6 | 79 | 13 | 59 | range | range |
| 08-27 | 867 | 1716 | 0.51 | 43.7 | 49.4 | 57.3 | 61 | 23 | 34 | range | range |
| 08-28 | 1355 | 1231 | 1.10 | 45.2 | 49.9 | 57.0 | 84 | 29 | 51 | range | range |
| 08-31 | 902 | 1728 | 0.52 | 41.5 | 47.3 | 56.4 | 79 | 48 | 48 | range | range |
| 09-01 | 890 | 1722 | 0.52 | 34.4 | 43.8 | 55.5 | 57 | 32 | 31 | range | range |
| 09-02 | 915 | 1694 | 0.54 | 32.5 | 40.2 | 53.9 | 47 | 40 | 57 | range | range |
| 09-03 | 1631 | 960 | 1.70 | 37.4 | 45.0 | 55.0 | 76 | 25 | 68 | trend_down | range |
| 09-04 | 1405 | 1187 | 1.18 | 39.9 | 46.6 | 55.8 | 67 | 19 | 81 | range | range |
| 09-07 | 970 | 1648 | 0.59 | 37.1 | 42.5 | 54.3 | 86 | 39 | 76 | trend_down | range |
| 09-08 | 1181 | 1437 | 0.82 | 38.5 | 43.4 | 53.7 | 82 | 35 | 37 | trend_down | range |
| 09-09 | 938 | 1671 | 0.56 | 37.0 | 41.8 | 52.3 | 53 | 52 | 58 | trend_down | range |
| 09-10 | 934 | 1667 | 0.56 | 34.9 | 40.2 | 51.8 | 50 | 57 | 40 | trend_down | range |
| 09-11 | 952 | 1643 | 0.58 | 31.8 | 38.0 | 51.4 | 49 | 61 | 54 | trend_down | range |
| 09-15 | 371 | 2263 | 0.16 | 20.4 | 27.9 | 45.6 | 22 | 103 | 24 | trend_down | range |
| 09-16 | 1199 | 1418 | 0.85 | 19.3 | 28.3 | 44.7 | 21 | 74 | 43 | trend_down | trend_down |
| 09-17 | 1750 | 869 | 2.01 | 23.6 | 29.4 | 46.5 | 42 | 25 | 62 | trend_down | trend_down |
| 09-18 | 1775 | 839 | 2.12 | 32.8 | 34.9 | 49.8 | 64 | 29 | 71 | trend_down | trend_down |
| 09-21 | 1250 | 1382 | 0.90 | 32.0 | 35.3 | 49.0 | 65 | 29 | 61 | trend_down | trend_down |
| 09-22 | 1119 | 1500 | 0.75 | 32.9 | 35.1 | 49.3 | 52 | 31 | 71 | trend_down | trend_down |
| 09-23 | 1785 | 847 | 2.11 | 40.1 | 39.5 | 51.7 | 82 | 14 | 82 | trend_down | trend_down |
| 09-24 | 503 | 2140 | 0.24 | 31.4 | 31.8 | 47.8 | 33 | 50 | 25 | trend_down | trend_down |
| 09-25 | 1359 | 1261 | 1.08 | 33.8 | 35.4 | 48.0 | 53 | 42 | 59 | trend_down | trend_down |
| 09-28 | 452 | 2195 | 0.21 | 23.7 | 26.7 | 43.3 | 33 | 103 | 22 | trend_down | trend_down |
| 09-29 | 1040 | 1605 | 0.65 | 24.5 | 26.3 | 41.6 | 40 | 124 | 53 | trend_down | trend_down |
| 09-30 | 1206 | 1418 | 0.85 | 25.3 | 26.4 | 42.1 | 39 | 87 | 45 | trend_down | trend_down |
| 10-01 | 569 | 2068 | 0.28 | 18.6 | 22.5 | 39.2 | 32 | 150 | 27 | trend_down | trend_down |
| 10-05 | 1265 | 1385 | 0.91 | 19.5 | 22.9 | 39.6 | 34 | 121 | 66 | trend_down | trend_down |
| 10-06 | 1907 | 738 | 2.58 | 27.2 | 25.8 | 40.9 | 52 | 35 | 68 | trend_down | trend_down |
| 10-07 | 1046 | 1602 | 0.65 | 29.2 | 26.7 | 40.3 | 50 | 76 | 55 | trend_down | trend_down |

On 7 Oct the 10-day %>50 DMA was 28.4 and the 10-day A/D ratio 0.73. Both are deep in bear
territory.

## 3. Regime definitions compared

These thresholds were fixed before looking at results:

- **(a) current:** `classify_history(nifty, vix, None)`, the same as live.
- **(b) breadth:** VIX ≥ 22 means risk_off. Otherwise, a 10-day mean %>50 DMA below 40 **and**
  a 10-day A/D (Σadv/Σdec) below 1 means trend_down; a 10-day %>50 DMA above 60 means
  trend_up; anything else is range.
- **(c) combined:** the current label, but trend_down becomes range while the 10-day %>50 DMA
  is 50 or higher.

**Data:** the 10y production-mode observations (177,289 rows, origin/main @ 701a6b9),
restricted to weeks with breadth: 341 weeks, 2019-11 → 2026-07.

**Proxies, applied identically everywhere:** bull mode is 20-day momentum (`bl_mom20`); bear
mode is 5-day reversal (−ret5 from bhavcopy). The observations only record bear-week scores
after neutralisation, so the raw Technical score cannot be recovered there.

**Metric:** weekly Spearman IC vs forward 20 days.

| definition | bear weeks | mom IC in bear wks | mom IC other wks | reversal IC in bear wks | IC of mode selected | top-decile mom excess fwd20, bear wks |
|---|---|---|---|---|---|---|
| current | 82 | −0.060 | +0.010 | +0.054 | +0.0209 | +0.38 |
| breadth | 106 | −0.050 | +0.013 | +0.057 | +0.0265 | +0.53 |
| combined | 70 | −0.065 | +0.009 | +0.056 | +0.0184 | +0.52 |

**Two-fold rule (#235)**, using the per-week gain over current in IC of the selected mode, with
alternating calendar quarters:

| definition | fold A gain (NW t) | fold B gain (NW t) | weeks mode differs | passes |
|---|---|---|---|---|
| breadth | +0.016 (1.84) | −0.005 (−0.84) | 62 | **no** |
| combined | −0.000 (−1.02) | −0.005 (−1.49) | 12 | **no** |

**Weeks where the alternative switches bear scoring OFF while current has it on:**

- breadth: 19 weeks. Momentum IC −0.052, reversal IC +0.036, top-momentum excess −0.78 pts.
- combined: 12 weeks. Momentum IC −0.029, reversal IC +0.043, top-momentum excess −0.43 pts.

In both cases bear mode was the **right** call in the weeks it would have been released.
Releasing bear mode early is therefore what the data argues **against**.

**BUY hit rates (20 days > 0) by label:**

- breadth trend_up: 58.2% hits, +3.38% mean forward return.
- current trend_up: 55.3%, +2.81%.
- The current label's range bucket is near coin-flip (51.0%, +0.04%).

Breadth separates the good BUY weeks better, which is a lead but not a pass.

### Late Sep / Oct 2026 specifically

- Breadth would **not** have switched bear scoring off. It agreed from 16 Sep onward.
- Between 7 and 15 Sep, breadth would have kept bear scoring **off** (range). Those 7
  sessions are the only part of this episode where the trigger was early.
- Was bear scoring right? It's too early to judge, because the forward window is only 1 to 22
  sessions. Since 7 Sep, 20-day momentum has had a **positive** cross-sectional IC to 7 Oct
  (+0.03 to +0.19 per day), and top-decile momentum names fell less than the market
  (for example, from 15 Sep: +0.9% vs −1.6%).
  - It is not evidence against the trigger. It is the known weakness of reversal scoring in
    an orderly, narrow decline.
  - Recheck once 20 sessions of forward data exist (about 4 Nov).

## 4. Verdict

- **Is the label wrong?** No. Every breadth measure confirms a broad decline. The user's
  impression of a strong market comes from single up-days (6 Oct) inside a downtrend, and
  from a minority of leaders.
- **Is it sticky?** It's laggy by design. It exits a median 6.5 sessions after the low, needing
  NIFTY about 3% to 5% off the low. The breadth trigger is no faster.
- **Trigger change supported by the data:** none. Breadth (b) shows a better overall
  selected-mode IC (+0.0265 vs +0.0209) and cleaner BUY weeks, but fails fold B. Combined (c)
  is worse in both folds.
- **Proposed (not adopted, separate reviewed change only):** pass live breadth into the regime
  as a **shadow** field, and log both labels to the verdict ledger, with no scoring effect:

  ```
  analysis.market_breadth.breadth_regime(breadth, window=10, bear_pct50=40, bull_pct50=60, bear_ad=1.0)
  ```

  Re-run this study after about 26 more weeks. If (b) then clears t > 2 in both folds,
  `snapshot_live()` should take `trend_down` from breadth, with VIX ≥ 22 still meaning
  risk_off. That is a change to `analysis/regime.py`, not to score weights.
- A breadth snapshot is cheap now. The bhavcopy files are already fetched daily, so the
  original "500-ticker scan" objection (`regime.py:420`) no longer applies.

## Caveats

- Bear/bull mode effects are proxied (momentum vs reversal IC), not re-scored end to end.
- Breadth covers all NSE EQ stocks, including small and illiquid names. The IC tests use the
  Nifty-500 observation universe, which carries survivorship bias.
- Weekly windows overlap, so the NW t-stats are optimistic.
