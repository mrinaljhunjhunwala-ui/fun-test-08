"""
research/score_variants_reversal.py — does a range-regime reversal tilt
survive costs, beat the composite when blended into it, and hold among
liquid names?

Follow-up to docs/REGIME_ALT_FACTORS_2026-10-06.md (#294), which found that
the 20-session reversal (minus the 20-session return) is the only simple
factor with a positive weekly IC20 in `range` regimes (+0.043, NW t 2.8)
where the composite is ~0 (-0.011). That gross spread was ~0.9 pts per 20
sessions, close to costs, so this study answers three questions:

  1. COSTS  — non-overlapping 20-session rebalances (every 4th week, all 4
              phase offsets averaged) of the top decile: gross and net excess
              over the equal-weight cross-section, net long-short spread,
              and turnover. Cost = COST_ROUNDTRIP_PCT (0.30%) per replaced
              name per leg.
  2. BLEND  — score variants, applied ONLY on rows labelled `range`:
                BASE   production score90
                REVR   momentum pillar (0-25) replaced by 25 x the week's
                       cross-sectional percentile of rev20
                HALF   momentum pillar replaced by 50/50 momentum + that rev20 term
              Other regimes keep BASE, so any difference comes from range rows.
  3. ROBUST — the same metrics within the more liquid half of each week's
              cross-section (60-session turnover above the weekly median), as a
              survivorship proxy: the repo has no point-in-time index-
              membership list, and survivorship bias concentrates in small,
              illiquid names. Plus a by-year table to show V-shaped years
              (2020) explicitly.
  4. ADOPTION RULE — the rule docs/DECAY_ATTRIBUTION_2026-10-01.md (#235) set in
              advance, where short-term reversal already FAILED on 5 years: a
              variant must beat score90 with t > 2 in BOTH folds of alternating
              calendar quarters. Measured as the paired weekly IC20 difference
              (variant - score90) on range rows, Newey-West t (4 lags).

The cross-sectional percentile uses only same-week data that is already
known (past returns), so there is no look-ahead.

PRODUCTION SCORING IS NOT MODIFIED. Input is an observations.csv written by
`py -m research.score_efficacy --period 10y --regime-weights`; prices are
re-fetched for rev20 and turnover.

Run:
    py -m research.score_variants_reversal --obs research/output/observations.csv
Outputs (research/output/):
    revvar_ic.csv         weekly IC20/IC60 (NW t) per regime x variant x universe
    revvar_portfolio.csv  non-overlapping top-decile gross/net excess, L/S net, turnover
    revvar_by_year.csv    range-regime weekly IC20 per variant x year
    revvar_folds.csv      adoption rule: paired IC20 difference vs score90 per fold
    revvar_features.csv   cached rev20/turnover per (ticker, date); reused by --features
"""
from __future__ import annotations

import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
OUT_DIR = os.path.join(_ROOT, "research", "output")

from research.score_regime_alt_factors import _MIN_XS, _nw_t, _weekly_ic  # noqa: E402

COST = 0.30                       # % per round trip per name (score_efficacy COST_ROUNDTRIP_PCT)
VARIANTS = ["score90", "REVR", "HALF", "rev20"]
_TARGET = "range"


def _features(ticker: str, dates: pd.Series, period: str) -> pd.DataFrame:
    from data.fetcher import fetch_single
    df = fetch_single(ticker, period=period)
    if df is None or df.empty:
        return pd.DataFrame()
    df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
    c = df["Close"].astype(float)
    v = df["Volume"].astype(float) if "Volume" in df else pd.Series(np.nan, index=df.index)
    f = pd.DataFrame({"rev20": -c.pct_change(20),
                      "turnover": (c * v).rolling(60).mean()})
    f = f.reindex(pd.to_datetime(dates).dt.normalize().unique())
    f["ticker"] = ticker
    return f.rename_axis("date").reset_index()


def _add_variants(d: pd.DataFrame) -> pd.DataFrame:
    rev_pts = d.groupby("week")["rev20"].rank(pct=True) * 25.0
    base_ex_mom = d["score90"] - d["momentum"]
    tgt = (d["live_regime"] == _TARGET) & rev_pts.notna()
    d["REVR"] = np.where(tgt, base_ex_mom + rev_pts, d["score90"])
    d["HALF"] = np.where(tgt, base_ex_mom + 0.5 * d["momentum"] + 0.5 * rev_pts, d["score90"])
    return d


def _portfolio(g: pd.DataFrame, col: str, offset: int) -> dict:
    """Top/bottom decile, rebalanced every 4th week starting at `offset`."""
    weeks = np.sort(g["week"].unique())[offset::4]
    prev_top, prev_bot = set(), set()
    rows = []
    for w in weeks:
        x = g[g["week"] == w][["ticker", col, "fwd_20d"]].dropna()
        x = x.drop_duplicates("ticker")
        if len(x) < _MIN_XS:
            continue
        r = x[col].rank(pct=True, method="first")
        top, bot = x[r > 0.9], x[r <= 0.1]
        tset, bset = set(top.ticker), set(bot.ticker)
        t_to = 1.0 if not prev_top else 1 - len(tset & prev_top) / len(tset)
        b_to = 1.0 if not prev_bot else 1 - len(bset & prev_bot) / len(bset)
        mkt = x["fwd_20d"].mean()
        ex = top["fwd_20d"].mean() - mkt
        ls = top["fwd_20d"].mean() - bot["fwd_20d"].mean()
        rows.append({"ex_gross": ex, "ex_net": ex - t_to * COST,
                     "ls_gross": ls, "ls_net": ls - (t_to + b_to) * COST, "turnover": t_to})
        prev_top, prev_bot = tset, bset
    return pd.DataFrame(rows).assign(offset=offset) if rows else pd.DataFrame()


def _tstat(x: pd.Series) -> float:
    x = x.dropna()
    return round(float(x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))), 2) if len(x) > 5 and x.std() > 0 else np.nan


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--obs", default=os.path.join(OUT_DIR, "observations.csv"))
    ap.add_argument("--period", default="10y")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--features", default="",
                    help="reuse a revvar_features.csv instead of re-fetching prices")
    args = ap.parse_args(argv)

    obs = pd.read_csv(args.obs, parse_dates=["date"])
    obs = obs.dropna(subset=["fwd_20d", "live_regime"])
    tickers = sorted(obs["ticker"].unique())
    if args.limit:
        tickers = tickers[: args.limit]
        obs = obs[obs["ticker"].isin(tickers)]
    print(f"REVERSAL VARIANT STUDY | obs={len(obs)} tickers={len(tickers)} "
          f"{obs.date.min().date()} -> {obs.date.max().date()}")

    if args.features and os.path.exists(args.features):
        feats = [pd.read_csv(args.features, parse_dates=["date"])]
        print(f"  features from {args.features}")
    else:
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
    os.makedirs(OUT_DIR, exist_ok=True)
    pd.concat(feats, ignore_index=True).to_csv(os.path.join(OUT_DIR, "revvar_features.csv"), index=False)
    obs["date"] = obs["date"].dt.normalize()
    d = obs.merge(pd.concat(feats, ignore_index=True), on=["ticker", "date"], how="inner")
    d["week"] = d["date"].dt.to_period("W-FRI")
    d = _add_variants(d)
    d["liquid"] = d["turnover"] > d.groupby("week")["turnover"].transform("median")
    print(f"rows={len(d)} weeks={d.week.nunique()} range rows={(d.live_regime == _TARGET).sum()}")

    universes = [("all", d), ("liquid_half", d[d["liquid"]])]
    ic_rows, pf_rows, yr_rows = [], [], []
    for uname, u in universes:
        for reg, g in [("ALL", u)] + list(u.groupby("live_regime")):
            for col in VARIANTS:
                rec = {"universe": uname, "regime": reg, "variant": col}
                for fwd, h in (("fwd_20d", 4), ("fwd_60d", 12)):
                    ic = _weekly_ic(g, col, fwd).to_numpy()
                    rec[f"weeks_{fwd}"] = len(ic)
                    rec[f"ic_{fwd}"] = round(float(ic.mean()), 4) if len(ic) else np.nan
                    rec[f"t_{fwd}"] = round(_nw_t(ic, h), 2)
                ic_rows.append(rec)
                if reg in (_TARGET, "ALL"):
                    p = pd.concat([_portfolio(g, col, o) for o in range(4)], ignore_index=True)
                    if p.empty:
                        continue
                    per_off = p.groupby("offset").mean(numeric_only=True)
                    pf_rows.append({
                        "universe": uname, "regime": reg, "variant": col,
                        "periods": len(p),
                        **{k: round(float(p[k].mean()), 3) for k in
                           ("ex_gross", "ex_net", "ls_gross", "ls_net", "turnover")},
                        "t_ex_net": _tstat(p["ex_net"]), "t_ls_net": _tstat(p["ls_net"]),
                        "ex_net_min_offset": round(float(per_off["ex_net"].min()), 3),
                        "ex_net_max_offset": round(float(per_off["ex_net"].max()), 3),
                    })
                if reg == _TARGET:
                    ic = _weekly_ic(g, col, "fwd_20d")
                    for y, s in ic.groupby(ic.index.year):
                        yr_rows.append({"universe": uname, "variant": col, "year": int(y),
                                        "weeks": len(s), "ic20": round(float(s.mean()), 4)})

    fold_rows = []
    for uname, u in universes:
        g = u[u["live_regime"] == _TARGET]
        base = _weekly_ic(g, "score90", "fwd_20d")
        for col in VARIANTS[1:]:
            diff = (_weekly_ic(g, col, "fwd_20d") - base).dropna()
            q = diff.index.year * 4 + diff.index.quarter
            for fold, s_ in (("A", diff[q % 2 == 0]), ("B", diff[q % 2 == 1])):
                fold_rows.append({"universe": uname, "variant": col, "fold": fold, "weeks": len(s_),
                                  "ic_diff": round(float(s_.mean()), 4), "nw_t": round(_nw_t(s_.to_numpy(), 4), 2)})
    fold_t = pd.DataFrame(fold_rows)
    if not fold_t.empty:
        passed = fold_t.groupby(["universe", "variant"])["nw_t"].apply(lambda t: bool((t > 2).all()))
        fold_t["passes_rule"] = fold_t.set_index(["universe", "variant"]).index.map(passed)

    ic_t, pf_t, yr_t = pd.DataFrame(ic_rows), pd.DataFrame(pf_rows), pd.DataFrame(yr_rows)
    fold_t.to_csv(os.path.join(OUT_DIR, "revvar_folds.csv"), index=False)
    ic_t.to_csv(os.path.join(OUT_DIR, "revvar_ic.csv"), index=False)
    pf_t.to_csv(os.path.join(OUT_DIR, "revvar_portfolio.csv"), index=False)
    yr_t.to_csv(os.path.join(OUT_DIR, "revvar_by_year.csv"), index=False)
    pd.set_option("display.width", 220)
    pd.set_option("display.max_rows", 500)
    print("\n=== WEEKLY IC ===\n", ic_t.to_string(index=False))
    print("\n=== NON-OVERLAPPING TOP-DECILE PORTFOLIOS (pts per 20 sessions) ===\n", pf_t.to_string(index=False))
    print("\n=== ADOPTION RULE (#235): paired IC20 diff vs score90, range rows ===\n",
          fold_t.to_string(index=False))
    if not yr_t.empty:
        print("\n=== RANGE IC20 BY YEAR ===\n",
              yr_t.pivot_table(index=["universe", "variant"], columns="year", values="ic20").round(3).to_string())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
