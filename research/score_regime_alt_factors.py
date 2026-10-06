"""
research/score_regime_alt_factors.py — in range and falling markets, does any
simple price/volume factor rank NSE stocks better than the composite?

Production efficacy (10y, --regime-weights, 177k obs) gives the composite a
real rank IC20 in trend_up (+0.09) and almost none elsewhere (range +0.004,
trend_down -0.03, risk_off +0.02), so the score ships with a "low-information"
disclaimer outside uptrends. This study tests textbook cross-sectional factors
in those regimes, each computed from price/volume as of the observation date
(no look-ahead):

  rev5 / rev20   short-term reversal: minus the 5 / 20-session return
  lowvol         minus the stdev of daily returns over 60 sessions
  near52wh       close / 252-session high - 1 (0 = at the high)
  mom12_1        return from t-252 to t-21 (skips the last month)
  rs_score       RS_Score already on the observation row
  dma200         close / 200-session SMA - 1
  turnover       log of 60-session mean (close x volume)
  score90        the production composite (the benchmark)

Fundamentals are deliberately left out: the only point-in-time source in the
repo (research/fundamental_quality.py, 180-day lag on yfinance statements)
starts 2022-10 and serves restated values, too short and too biased for a
regime split.

Statistics — overlapping 20/60-day windows make pooled-observation p-values
meaningless, so everything significant is computed per DATE:
  * dates are bucketed to the week (W-FRI), since score_efficacy samples each
    ticker every 5th session from its own start;
  * per week: cross-sectional Spearman IC; then mean IC over weeks with a
    Newey-West t-stat (lags = horizon in weeks) and a moving-block bootstrap
    95% CI (block = horizon in weeks);
  * deciles are formed within each week on a week-demeaned forward return,
    then averaged: d10-d1 spread (pts) and monotonicity (Spearman of decile
    mean vs decile number);
  * distinct sample dates, weeks and episodes (runs of consecutive sample
    dates with the same label) are counted per regime, so thin evidence is visible.
The pooled IC is kept only to reconcile with score_efficacy's headline.

PRODUCTION SCORING IS NOT MODIFIED. Input is an observations.csv written by
`py -m research.score_efficacy --period 10y --regime-weights`; prices are
re-fetched to build the factors.

Run:
    py -m research.score_regime_alt_factors --obs research/output/observations.csv
Outputs (research/output/):
    altf_summary.csv     per regime x factor x horizon: weeks, episodes, pooled IC,
                         mean weekly IC, NW t, bootstrap CI, d10-d1, monotonicity,
                         train / holdout mean IC
    altf_by_year.csv     mean weekly IC20 per regime x factor x year
    altf_regimes.csv     dates / episodes / weeks / observations per regime
"""
from __future__ import annotations

import argparse
import math
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
OUT_DIR = os.path.join(_ROOT, "research", "output")

FACTORS = ["score90", "rs_score", "rev5", "rev20", "lowvol", "near52wh", "mom12_1", "dma200", "turnover"]
HORIZONS = {"fwd_20d": 4, "fwd_60d": 12}          # horizon in weeks (NW lags / bootstrap block)
_MIN_XS = 30                                       # min stocks in a week's cross-section
_N_BOOT = 1000


def _features(ticker: str, dates: pd.Series, period: str) -> pd.DataFrame:
    from data.fetcher import fetch_single
    df = fetch_single(ticker, period=period)
    if df is None or df.empty:
        return pd.DataFrame()
    df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
    c = df["Close"].astype(float)
    v = df["Volume"].astype(float) if "Volume" in df else pd.Series(np.nan, index=df.index)
    f = pd.DataFrame({
        "rev5": -c.pct_change(5),
        "rev20": -c.pct_change(20),
        "lowvol": -c.pct_change().rolling(60).std(),
        "near52wh": c / c.rolling(252).max() - 1,
        "mom12_1": c.shift(21) / c.shift(252) - 1,
        "dma200": c / c.rolling(200).mean() - 1,
        "turnover": np.log((c * v).rolling(60).mean().replace(0, np.nan)),
    })
    f = f.reindex(pd.to_datetime(dates).dt.normalize().unique())
    f["ticker"] = ticker
    return f.rename_axis("date").reset_index()


def _nw_t(x: np.ndarray, lags: int) -> float:
    """Newey-West t-stat of the mean of a (serially correlated) series."""
    n = len(x)
    if n < 10:
        return float("nan")
    e = x - x.mean()
    s = e @ e / n
    for k in range(1, min(lags, n - 1) + 1):
        s += 2 * (1 - k / (lags + 1)) * (e[k:] @ e[:-k]) / n
    return float(x.mean() / math.sqrt(s / n)) if s > 0 else float("nan")


def _block_ci(x: np.ndarray, block: int, rng: np.random.Generator) -> tuple:
    n = len(x)
    if n < 10:
        return float("nan"), float("nan")
    block = max(1, min(block, n))
    nb = math.ceil(n / block)
    means = np.empty(_N_BOOT)
    for b in range(_N_BOOT):
        starts = rng.integers(0, n - block + 1, nb)
        means[b] = np.concatenate([x[s:s + block] for s in starts])[:n].mean()
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def _weekly_ic(g: pd.DataFrame, col: str, fwd: str) -> pd.Series:
    def one(w):
        m = w[[col, fwd]].dropna()
        return m[col].corr(m[fwd], method="spearman") if len(m) >= _MIN_XS else np.nan
    return g.groupby("week")[[col, fwd]].apply(one).dropna()


def _deciles(g: pd.DataFrame, col: str, fwd: str) -> tuple:
    m = g[["week", col, fwd]].dropna()
    m = m[m.groupby("week")[col].transform("size") >= _MIN_XS]
    if m.empty:
        return float("nan"), float("nan")
    m = m.assign(dm=m[fwd] - m.groupby("week")[fwd].transform("mean"),
                 dec=m.groupby("week")[col].rank(pct=True, method="first").mul(10).clip(upper=9.999).astype(int))
    dec = m.groupby("dec")["dm"].mean()
    mono = float(pd.Series(dec.values).corr(pd.Series(dec.index), method="spearman"))
    return round(float(dec.iloc[-1] - dec.iloc[0]), 2), round(mono, 3)


def _pooled_ic(g: pd.DataFrame, col: str, fwd: str) -> float:
    m = g[[col, fwd]].dropna()
    return round(float(m[col].corr(m[fwd], method="spearman")), 4) if len(m) > 50 else float("nan")


def _episodes(labels: pd.Series) -> pd.Series:
    """Runs of consecutive (date-sorted) observations with the same label, counted per label."""
    runs = (labels != labels.shift()).cumsum()
    return labels.groupby(runs).first().value_counts()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--obs", default=os.path.join(OUT_DIR, "observations.csv"))
    ap.add_argument("--period", default="10y")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args(argv)
    rng = np.random.default_rng(7)

    obs = pd.read_csv(args.obs, parse_dates=["date"])
    obs = obs.dropna(subset=["fwd_20d", "live_regime"])
    tickers = sorted(obs["ticker"].unique())
    if args.limit:
        tickers = tickers[: args.limit]
        obs = obs[obs["ticker"].isin(tickers)]
    print(f"REGIME ALT-FACTOR STUDY | obs={len(obs)} tickers={len(tickers)} "
          f"{obs.date.min().date()} -> {obs.date.max().date()}")

    feats = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(_features, t, obs.loc[obs.ticker == t, "date"], args.period): t for t in tickers}
        for i, fu in enumerate(as_completed(futs), 1):
            try:
                feats.append(fu.result())
            except Exception as e:
                print(f"  skip {futs[fu]}: {type(e).__name__}: {e}")
            if i % 100 == 0:
                print(f"  fetched {i}/{len(tickers)}")
    feats = [x for x in feats if not x.empty]
    if not feats:
        print("no price data fetched; nothing to do")
        return 0
    obs["date"] = obs["date"].dt.normalize()
    d = obs.merge(pd.concat(feats, ignore_index=True), on=["ticker", "date"], how="inner")
    d["week"] = d["date"].dt.to_period("W-FRI")
    # Each row keeps its own live_regime (the label score_efficacy recorded), so
    # pooled ICs reconcile with its headline. A week whose rows straddle a regime
    # change contributes a partial cross-section to each label (min _MIN_XS stocks).
    d["regime"] = d["live_regime"]
    weeks = np.sort(d["week"].unique())
    split = weeks[len(weeks) // 2]
    d["half"] = np.where(d["week"] < split, "train", "holdout")
    print(f"rows={len(d)} weeks={len(weeks)} split at {split}")

    # Market-level label per trading date; episodes = runs over sorted dates.
    day_reg = d.groupby("date")["regime"].first().sort_index()
    eps = _episodes(day_reg)
    reg_rows = [{"regime": r, "dates": int((day_reg == r).sum()), "episodes": int(eps.get(r, 0)),
                 "weeks": int(d.loc[d["regime"] == r, "week"].nunique()),
                 "obs": int((d["regime"] == r).sum()),
                 "first": str(day_reg[day_reg == r].index.min().date()),
                 "last": str(day_reg[day_reg == r].index.max().date())}
                for r in sorted(day_reg.unique())]
    regimes = pd.DataFrame(reg_rows)

    rows, yrs = [], []
    groups = [("ALL", d)] + [(r, g) for r, g in d.groupby("regime")]
    for reg, g in groups:
        for col in FACTORS:
            for fwd, h in HORIZONS.items():
                ic = _weekly_ic(g, col, fwd)
                x = ic.to_numpy()
                lo, hi = _block_ci(x, h, rng)
                spread, mono = _deciles(g, col, fwd)
                half = {k: _weekly_ic(gg, col, fwd).mean() for k, gg in g.groupby("half")}
                rows.append({
                    "regime": reg, "factor": col, "horizon": fwd, "weeks": len(x),
                    "pooled_ic": _pooled_ic(g, col, fwd),
                    "mean_ic": round(float(x.mean()), 4) if len(x) else np.nan,
                    "nw_t": round(_nw_t(x, h), 2), "ci_lo": round(lo, 4), "ci_hi": round(hi, 4),
                    "pct_weeks_pos": round(float((x > 0).mean() * 100), 1) if len(x) else np.nan,
                    "d10_d1": spread, "mono": mono,
                    "ic_train": round(float(half.get("train", np.nan)), 4),
                    "ic_holdout": round(float(half.get("holdout", np.nan)), 4),
                })
                if fwd == "fwd_20d":
                    for y, s in ic.groupby(ic.index.year):
                        yrs.append({"regime": reg, "factor": col, "year": int(y),
                                    "weeks": len(s), "mean_ic20": round(float(s.mean()), 4)})
    summary = pd.DataFrame(rows)
    by_year = pd.DataFrame(yrs)

    os.makedirs(OUT_DIR, exist_ok=True)
    summary.to_csv(os.path.join(OUT_DIR, "altf_summary.csv"), index=False)
    by_year.to_csv(os.path.join(OUT_DIR, "altf_by_year.csv"), index=False)
    regimes.to_csv(os.path.join(OUT_DIR, "altf_regimes.csv"), index=False)
    pd.set_option("display.width", 220)
    pd.set_option("display.max_rows", 500)
    print("\n=== REGIMES ===\n", regimes.to_string(index=False))
    print("\n=== SUMMARY ===\n", summary.to_string(index=False))
    if not by_year.empty:
        print("\n=== MEAN WEEKLY IC20 BY YEAR ===\n",
              by_year.pivot_table(index=["regime", "factor"], columns="year", values="mean_ic20").round(3).to_string())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
