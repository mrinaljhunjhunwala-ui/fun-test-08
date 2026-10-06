"""
research/score_variants_momentum_vol.py — does volatility-scaling the
Momentum pillar's absolute-return thresholds improve the composite?

Audit 2026-10-03 #18: _score_momentum buckets the raw 5/20/60-day returns on
fixed thresholds (e.g. r20d > 15% = full points), so a stock that moves 4% a
day reaches the top buckets on noise that a 1%-a-day large cap never shows.
This study tests the obvious fix before anyone touches production:

  BASE     : production composite (score90 from research/score_efficacy.py's
             observations.csv, with the same regime handling).
  VOLSCALE : the abs-returns part of Momentum recomputed on returns scaled by
             (reference vol / stock vol), clipped to [0.5, 2.0], where vol is
             the stdev of daily returns over the 60 sessions to the date and
             the reference is the cross-sectional median. Same buckets, same
             15-of-25 weight. Dates where production replaces abs-returns with
             the bear mean-reversion variant (trend_down / risk_off) are left
             unchanged, so the variant only moves what it is meant to move.

PRODUCTION SCORING IS NOT MODIFIED. Input is an observations.csv written by
`py -m research.score_efficacy --period 10y --regime-weights` (production
regime handling); prices are re-fetched to rebuild r5/r20/r60 and vol.

Run:
    py -m research.score_variants_momentum_vol --obs research/output/observations.csv
Outputs (research/output/):
    momvol_summary.csv     IC20/IC60, decile spread, monotonicity: base vs variant
    momvol_by_regime.csv   the same per live regime and train/holdout half
    momvol_by_volq.csv     mean score + IC by volatility quintile (the fairness claim)
"""
from __future__ import annotations

import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict

import numpy as np
import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
OUT_DIR = os.path.join(_ROOT, "research", "output")

_BEAR = {"trend_down", "risk_off"}
_VOL_WINDOW = 60
_CLIP = (0.5, 2.0)


def _abs_points(r5: float, r20: float, r60: float) -> float:
    """Production _score_momentum abs buckets (0-25) for given returns in %."""
    p5 = 5.0 if r5 > 3 else 4.0 if r5 > 1 else 3.0 if r5 > 0 else 2.0 if r5 > -2 else 0.0
    p20 = (10.0 if r20 > 15 else 8.0 if r20 > 8 else 7.0 if r20 > 4 else 5.0 if r20 > 2
           else 4.0 if r20 > 0 else 2.0 if r20 > -5 else 0.0)
    if not np.isfinite(r60):
        p60 = 5.0
    else:
        p60 = (10.0 if r60 > 25 else 8.0 if r60 > 15 else 6.0 if r60 > 8 else 5.0 if r60 > 3
               else 3.0 if r60 > 0 else 1.0 if r60 > -10 else 0.0)
    return p5 + p20 + p60


def _features(ticker: str, dates: pd.Series, period: str) -> pd.DataFrame:
    from data.fetcher import fetch_single
    df = fetch_single(ticker, period=period)
    if df is None or df.empty:
        return pd.DataFrame()
    c = df["Close"].astype(float)
    c.index = pd.to_datetime(c.index).tz_localize(None).normalize()
    f = pd.DataFrame({
        "r5": c.pct_change(5) * 100, "r20": c.pct_change(20) * 100, "r60": c.pct_change(60) * 100,
        "vol": c.pct_change().rolling(_VOL_WINDOW).std() * 100,
    })
    f = f.reindex(pd.to_datetime(dates).dt.normalize().unique())
    f["ticker"] = ticker
    return f.rename_axis("date").reset_index()


def _ic(a, b) -> float:
    from scipy.stats import spearmanr
    m = np.isfinite(a) & np.isfinite(b)
    return round(float(spearmanr(a[m], b[m]).correlation), 4) if m.sum() > 50 else float("nan")


def _block(g: pd.DataFrame, col: str) -> Dict:
    out = {"n": len(g), "IC20": _ic(g[col].to_numpy(), g["fwd_20d"].to_numpy()),
           "IC60": _ic(g[col].to_numpy(), g["fwd_60d"].to_numpy())}
    if len(g) >= 200:
        q = pd.qcut(g[col].rank(method="first"), 10, labels=False)
        dec = g.groupby(q)["fwd_20d"].mean()
        out["d10_minus_d1_fwd20"] = round(float(dec.iloc[-1] - dec.iloc[0]), 2)
        out["monotonicity"] = round(float(pd.Series(dec.values).corr(pd.Series(range(10)), method="spearman")), 3)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--obs", default=os.path.join(OUT_DIR, "observations.csv"))
    ap.add_argument("--period", default="10y")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args(argv)

    obs = pd.read_csv(args.obs, parse_dates=["date"])
    obs = obs.dropna(subset=["fwd_20d"])
    tickers = sorted(obs["ticker"].unique())
    if args.limit:
        tickers = tickers[: args.limit]
        obs = obs[obs["ticker"].isin(tickers)]
    print(f"MOMENTUM VOL-SCALE STUDY | obs={len(obs)} tickers={len(tickers)} "
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
    f = pd.concat([x for x in feats if not x.empty], ignore_index=True)
    obs["date"] = obs["date"].dt.normalize()
    d = obs.merge(f, on=["ticker", "date"], how="inner").dropna(subset=["r5", "r20", "vol"])
    d = d[d["vol"] > 0]

    ref_vol = float(d["vol"].median())
    scale = (ref_vol / d["vol"]).clip(*_CLIP)
    old_abs = np.array([_abs_points(a, b, c) for a, b, c in zip(d.r5, d.r20, d.r60)])
    new_abs = np.array([_abs_points(a, b, c) for a, b, c in
                        zip(d.r5 * scale, d.r20 * scale, d.r60 * scale)])
    # RS present (score_efficacy fetches the benchmark): abs is 15 of 25.
    delta = (new_abs - old_abs) * (15.0 / 25.0)
    bear = d["live_regime"].isin(_BEAR).to_numpy()
    delta[bear] = 0.0                       # production uses Var M there; abs part not scored
    d["score_var"] = d["score90"] + delta
    d["delta"] = delta
    print(f"rows={len(d)} ref_vol={ref_vol:.2f}%/day  scale clip={_CLIP}  "
          f"rows moved={np.mean(delta != 0) * 100:.1f}%  mean delta={delta.mean():+.3f}")

    dates = sorted(d["date"].unique())
    d["half"] = np.where(d["date"] < dates[len(dates) // 2], "train", "holdout")
    d["volq"] = pd.qcut(d["vol"].rank(method="first"), 5, labels=["Q1 calm", "Q2", "Q3", "Q4", "Q5 volatile"])

    rows = [{"variant": v, **_block(d, c)} for v, c in (("BASE", "score90"), ("VOLSCALE", "score_var"))]
    summary = pd.DataFrame(rows)
    by = []
    for key in ("live_regime", "half"):
        for k, g in d.groupby(key):
            for v, c in (("BASE", "score90"), ("VOLSCALE", "score_var")):
                by.append({"group": f"{key}={k}", "variant": v, **_block(g, c)})
    by_regime = pd.DataFrame(by)
    vq = []
    for k, g in d.groupby("volq", observed=True):
        vq.append({"vol_quintile": k, "n": len(g), "avg_vol": round(g.vol.mean(), 2),
                   "mean_score_base": round(g.score90.mean(), 2), "mean_score_var": round(g.score_var.mean(), 2),
                   "IC20_base": _ic(g.score90.to_numpy(), g.fwd_20d.to_numpy()),
                   "IC20_var": _ic(g.score_var.to_numpy(), g.fwd_20d.to_numpy()),
                   "mean_fwd20": round(g.fwd_20d.mean(), 2)})
    by_volq = pd.DataFrame(vq)

    os.makedirs(OUT_DIR, exist_ok=True)
    summary.to_csv(os.path.join(OUT_DIR, "momvol_summary.csv"), index=False)
    by_regime.to_csv(os.path.join(OUT_DIR, "momvol_by_regime.csv"), index=False)
    by_volq.to_csv(os.path.join(OUT_DIR, "momvol_by_volq.csv"), index=False)
    pd.set_option("display.width", 200)
    print("\n=== SUMMARY ===\n", summary.to_string(index=False))
    print("\n=== BY REGIME / HALF ===\n", by_regime.to_string(index=False))
    print("\n=== BY VOLATILITY QUINTILE ===\n", by_volq.to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
