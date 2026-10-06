"""
research/score_reversal_sector_neutral.py — is the range-regime reversal
effect WITHIN-SECTOR stock selection or SECTOR ROTATION?

Follow-up to docs/REGIME_ALT_FACTORS_2026-10-06.md and
docs/REVERSAL_VARIANT_2026-10-06.md: in `range` regimes the 20-session
reversal rev20 (minus the 20-session return) has weekly IC20 ~+0.04 while
score90 is ~0. This script splits rev20 into two parts:

  a) rev20          raw factor
  b) rev20_within   rev20 minus the week x sector median (sector-neutral);
                    only sectors with >= --min-sector-size stocks that week
  c) sector_rev     the week x sector median rev20 given to every member
                    (pure sector rotation)
  d) score90 and score90_within (sector-neutral composite), for reference

For each regime (and ALL), and for fwd_20d / fwd_60d: mean weekly Spearman
IC, Newey-West t (4 / 12 lags), block-bootstrap 95% CI, % weeks positive,
and a train/holdout split by week. Plus the #235 two-fold rule (alternating
calendar quarters, paired weekly IC20 difference rev20_within - score90,
NW t > 2 in both folds), a range by-year table, the cross-section shape
(sectors and stocks per week), and a sector-level test: per range week,
Spearman of sector median rev20 against sector median fwd_20d.

Rows (b)-(d) use the same filtered sample (sectors that are large enough),
so the raw factor is also reported on that sample (rev20_filt).

PRODUCTION SCORING IS NOT MODIFIED. Educational research only.

Run:
    py -m research.score_reversal_sector_neutral
Outputs (research/output/):
    altfsn_ic.csv          weekly IC per regime x factor x horizon
    altfsn_folds.csv       #235 rule: paired IC20 diff vs score90, range rows
    altfsn_by_year.csv     range IC20 per factor x year
    altfsn_shape.csv       sectors / stocks per week per regime
    altfsn_sector_ic.csv   sector-level weekly IC (sector median rev20 vs fwd)
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from research.score_regime_alt_factors import _MIN_XS, _block_ci, _nw_t, _weekly_ic  # noqa: E402

OUT_DIR = os.path.join(_ROOT, "research", "output")
FACTORS = ["rev20", "rev20_filt", "rev20_within", "sector_rev", "score90", "score90_within"]
HORIZONS = {"fwd_20d": 4, "fwd_60d": 12}
_TARGET = "range"
_MIN_SECTORS = 5                                   # min sectors in a week for the sector-level IC


def _decompose(d: pd.DataFrame, min_size: int) -> pd.DataFrame:
    key = ["week", "sector"]
    d = d.copy()
    d["sec_n"] = d.groupby(key)["rev20"].transform("count")
    ok = d["sec_n"] >= min_size
    d["sector_rev"] = d.groupby(key)["rev20"].transform("median").where(ok)
    d["rev20_within"] = (d["rev20"] - d["sector_rev"]).where(ok)
    d["rev20_filt"] = d["rev20"].where(ok)
    s_med = d.groupby(key)["score90"].transform("median")
    d["score90_within"] = (d["score90"] - s_med).where(ok)
    return d


def _ic_record(g: pd.DataFrame, col: str, fwd: str, lags: int, rng) -> dict:
    s = _weekly_ic(g, col, fwd)
    x = s.to_numpy()
    lo, hi = _block_ci(x, lags, rng)
    weeks = s.index.sort_values()
    cut = weeks[len(weeks) // 2] if len(weeks) else None
    tr = s[s.index < cut].to_numpy() if cut is not None else np.array([])
    ho = s[s.index >= cut].to_numpy() if cut is not None else np.array([])
    return {
        "weeks": len(x),
        "ic": round(float(x.mean()), 4) if len(x) else np.nan,
        "nw_t": round(_nw_t(x, lags), 2),
        "ci_lo": round(lo, 4), "ci_hi": round(hi, 4),
        "pct_pos": round(100 * float((x > 0).mean()), 1) if len(x) else np.nan,
        "ic_train": round(float(tr.mean()), 4) if len(tr) else np.nan,
        "t_train": round(_nw_t(tr, lags), 2),
        "ic_hold": round(float(ho.mean()), 4) if len(ho) else np.nan,
        "t_hold": round(_nw_t(ho, lags), 2),
    }


def _sector_ic(g: pd.DataFrame, fwd: str) -> pd.DataFrame:
    m = g.dropna(subset=["sector_rev", fwd])
    sec = m.groupby(["week", "sector"]).agg(sr=("sector_rev", "first"), f=(fwd, "median")).reset_index()

    def one(w):
        return w["sr"].corr(w["f"], method="spearman") if len(w) >= _MIN_SECTORS else np.nan
    ic = sec.groupby("week")[["sr", "f"]].apply(one)
    n = sec.groupby("week").size()
    return pd.DataFrame({"ic": ic, "n_sectors": n}).dropna()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--obs", default=os.path.join(OUT_DIR, "observations.csv"))
    ap.add_argument("--features", default=os.path.join(OUT_DIR, "revvar_features.csv"))
    ap.add_argument("--min-sector-size", type=int, default=5)
    args = ap.parse_args(argv)
    rng = np.random.default_rng(7)

    if not (os.path.exists(args.obs) and os.path.exists(args.features)):
        print(f"missing input: {args.obs} / {args.features}; nothing to do")
        return 0
    obs = pd.read_csv(args.obs, parse_dates=["date"])
    feats = pd.read_csv(args.features, parse_dates=["date"])
    obs["date"] = obs["date"].dt.normalize()
    feats["date"] = feats["date"].dt.normalize()
    d = obs.merge(feats[["ticker", "date", "rev20"]], on=["ticker", "date"], how="inner")
    d = d.dropna(subset=["sector", "rev20"])
    if d.empty:
        print("no rows after merge; nothing to do")
        return 0
    d["week"] = d["date"].dt.to_period("W-FRI")
    d = _decompose(d, args.min_sector_size)
    print(f"rows={len(d)} weeks={d.week.nunique()} range rows={(d.live_regime == _TARGET).sum()} "
          f"rows kept by sector filter={d.rev20_within.notna().sum()} min_xs={_MIN_XS}")

    groups = [("ALL", d)] + list(d.groupby("live_regime"))
    ic_rows, shape_rows = [], []
    for reg, g in groups:
        for col in FACTORS:
            for fwd, h in HORIZONS.items():
                ic_rows.append({"regime": reg, "factor": col, "horizon": fwd, **_ic_record(g, col, fwd, h, rng)})
        k = g.dropna(subset=["rev20_within"])
        per_w = k.groupby("week").agg(stocks=("ticker", "size"), sectors=("sector", "nunique"))
        shape_rows.append({
            "regime": reg, "weeks": len(per_w),
            "stocks_med": float(per_w["stocks"].median()) if len(per_w) else np.nan,
            "sectors_med": float(per_w["sectors"].median()) if len(per_w) else np.nan,
            "sectors_min": int(per_w["sectors"].min()) if len(per_w) else np.nan,
            "sectors_max": int(per_w["sectors"].max()) if len(per_w) else np.nan,
            "rows_dropped_pct": round(100 * float(g["rev20_within"].isna().mean()), 1),
            # share of rev20 cross-sectional variance explained by the sector median
            "sector_var_share": round(float((k.groupby("week")["sector_rev"].var()
                                             / k.groupby("week")["rev20_filt"].var()).median()), 3)
            if len(k) else np.nan,
        })

    rng_rows = d[d["live_regime"] == _TARGET]
    fold_rows = []
    base = _weekly_ic(rng_rows, "score90", "fwd_20d")
    for col in ("rev20", "rev20_within", "sector_rev"):
        diff = (_weekly_ic(rng_rows, col, "fwd_20d") - base).dropna()
        q = diff.index.year * 4 + diff.index.quarter
        for fold, s_ in (("A", diff[q % 2 == 0]), ("B", diff[q % 2 == 1])):
            fold_rows.append({"factor": col, "fold": fold, "weeks": len(s_),
                              "ic_diff": round(float(s_.mean()), 4), "nw_t": round(_nw_t(s_.to_numpy(), 4), 2)})
    fold_t = pd.DataFrame(fold_rows)
    if not fold_t.empty:
        passed = fold_t.groupby("factor")["nw_t"].apply(lambda t: bool((t > 2).all()))
        fold_t["passes_rule"] = fold_t["factor"].map(passed)

    yr_rows = []
    for col in FACTORS:
        ic = _weekly_ic(rng_rows, col, "fwd_20d")
        for y, s in ic.groupby(ic.index.year):
            yr_rows.append({"factor": col, "year": int(y), "weeks": len(s), "ic20": round(float(s.mean()), 4)})

    sec_rows = []
    for reg, g in groups:
        for fwd, h in HORIZONS.items():
            s = _sector_ic(g, fwd)
            x = s["ic"].to_numpy()
            lo, hi = _block_ci(x, h, rng)
            sec_rows.append({"regime": reg, "horizon": fwd, "weeks": len(x),
                             "ic": round(float(x.mean()), 4) if len(x) else np.nan,
                             "nw_t": round(_nw_t(x, h), 2), "ci_lo": round(lo, 4), "ci_hi": round(hi, 4),
                             "pct_pos": round(100 * float((x > 0).mean()), 1) if len(x) else np.nan,
                             "sectors_med": float(s["n_sectors"].median()) if len(s) else np.nan})

    os.makedirs(OUT_DIR, exist_ok=True)
    ic_t, yr_t = pd.DataFrame(ic_rows), pd.DataFrame(yr_rows)
    shape_t, sec_t = pd.DataFrame(shape_rows), pd.DataFrame(sec_rows)
    ic_t.to_csv(os.path.join(OUT_DIR, "altfsn_ic.csv"), index=False)
    fold_t.to_csv(os.path.join(OUT_DIR, "altfsn_folds.csv"), index=False)
    yr_t.to_csv(os.path.join(OUT_DIR, "altfsn_by_year.csv"), index=False)
    shape_t.to_csv(os.path.join(OUT_DIR, "altfsn_shape.csv"), index=False)
    sec_t.to_csv(os.path.join(OUT_DIR, "altfsn_sector_ic.csv"), index=False)
    pd.set_option("display.width", 240)
    pd.set_option("display.max_rows", 500)
    print("\n=== WEEKLY IC (stock level) ===\n", ic_t.to_string(index=False))
    print("\n=== ADOPTION RULE (#235): paired IC20 diff vs score90, range rows ===\n", fold_t.to_string(index=False))
    if not yr_t.empty:
        print("\n=== RANGE IC20 BY YEAR ===\n",
              yr_t.pivot_table(index="factor", columns="year", values="ic20").round(3).to_string())
    print("\n=== CROSS-SECTION SHAPE (after sector filter) ===\n", shape_t.to_string(index=False))
    print("\n=== SECTOR-LEVEL IC (sector median rev20 vs sector median fwd) ===\n", sec_t.to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
