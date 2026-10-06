"""
research/fundamental_growth_regime.py — Revenue growth by LIVE regime label.

Question: does point-in-time revenue growth (the strongest signal in
docs/FUNDAMENTAL_QUALITY_REPORT.md) keep ranking stocks cross-sectionally in
range / trend_down / risk_off markets, where the price-based composite carries
little information?

READ-ONLY research. No production change. Educational only.

Reuse (no reimplementation):
  • research.fundamental_quality._fetch_statements / _metrics_at — the same
    180-day reporting-lag point-in-time logic (statements with
    period_end ≤ t − 180d), same CAGR growth definition.
  • research.portfolio_fit_efficacy._fetch_closes — 5y daily closes.
  • analysis.regime.classify_history on ^NSEI + ^INDIAVIX — the LIVE regime
    label (rolling-window only, so the label at t uses data ≤ t).

Universe: nifty500 (data.universe) — today's constituents (survivorship bias).
Sampling: monthly (every 21st session) from 2022-10 to last − 126 sessions.

Statistics are PER-DATE cross-sectional Spearman ICs; significance is computed
over the IC time series (Newey-West t with lags = horizon/21 sample periods,
and a circular moving-block bootstrap with block = horizon/21). Pooled
observations are never used for significance.

Run:
  py -m research.fundamental_growth_regime                 # fetch + analyse
  py -m research.fundamental_growth_regime --from-cache    # analyse cached panel
  py -m research.fundamental_growth_regime --limit 40      # quick check
Outputs (research/output/):
  fgr_panel.csv, fgr_ic_by_regime.csv, fgr_quintiles.csv, fgr_by_year.csv,
  fgr_split.csv, fgr_dates.csv
"""
from __future__ import annotations

import argparse
import math
import os
import sys
import time
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

OUT_DIR = os.path.join(_ROOT, "research", "output")
PANEL_CSV = os.path.join(OUT_DIR, "fgr_panel.csv")

SAMPLE_STEP = 21
HORIZONS = {"fwd_3m": 63, "fwd_6m": 126}
METRICS = ["rev_g", "eps_g", "roe"]
MIN_XSEC = 30            # min stocks per date for an IC
SPLIT_DATE = "2024-07-01"
N_BOOT = 2000


# ─────────────────────────────────────────────────────────────────────────────
# Panel build (network)
# ─────────────────────────────────────────────────────────────────────────────

def _live_regimes() -> Optional[pd.Series]:
    from analysis.regime import classify_history
    from data.fetcher import fetch_single
    nifty = fetch_single("^NSEI", period="5y")
    vix = fetch_single("^INDIAVIX", period="5y")
    if nifty is None or nifty.empty:
        return None
    n = nifty["Close"].astype(float)
    n.index = pd.to_datetime(n.index)
    v = None
    if vix is not None and not vix.empty:
        v = vix["Close"].astype(float)
        v.index = pd.to_datetime(v.index)
    return classify_history(n, v, None)


def build_panel(limit: int = 0, workers: int = 6) -> pd.DataFrame:
    from data.universe import get_universe
    from research.fundamental_quality import _fetch_statements, _metrics_at
    from research.portfolio_fit_efficacy import _fetch_closes

    tickers = sorted(get_universe("nifty500"))
    if limit:
        tickers = tickers[:limit]
    t0 = time.time()
    print(f"FUNDAMENTAL GROWTH x REGIME | {len(tickers)} tickers")
    stmts = _fetch_statements(tickers, workers=workers)
    print(f"Fundamentals usable: {len(stmts)}/{len(tickers)} [{time.time()-t0:.0f}s]")
    closes = _fetch_closes(tickers, workers=12)
    print(f"Closes: {closes.shape} [{time.time()-t0:.0f}s]")
    regimes = _live_regimes()
    if regimes is None or regimes.empty:
        print("Live regime labels unavailable — cannot run.")
        return pd.DataFrame()
    print("Live regime label counts (daily):", regimes.value_counts().to_dict())

    didx = closes.index
    start = int(didx.searchsorted(pd.Timestamp("2022-10-01")))
    last = len(didx) - max(HORIZONS.values()) - 1
    positions = list(range(start, last, SAMPLE_STEP))
    rows: List[Dict] = []
    for pos in positions:
        t = didx[pos]
        ri = regimes.index.asof(t)
        reg = str(regimes.loc[ri]) if pd.notna(ri) else "unknown"
        for tk, s in stmts.items():
            if tk not in closes.columns:
                continue
            c0 = closes[tk].iloc[pos]
            if not np.isfinite(c0) or c0 <= 0:
                continue
            m = _metrics_at(s, t)
            if all(m.get(k) is None for k in METRICS):
                continue
            row = {"date": str(t)[:10], "ticker": tk, "regime": reg,
                   **{k: m.get(k) for k in METRICS}}
            for name, h in HORIZONS.items():
                c1 = closes[tk].iloc[pos + h]
                row[name] = (c1 / c0 - 1) * 100 if np.isfinite(c1) else np.nan
            rows.append(row)
    df = pd.DataFrame(rows)
    os.makedirs(OUT_DIR, exist_ok=True)
    df.to_csv(PANEL_CSV, index=False, encoding="utf-8")
    print(f"Panel: {len(df)} rows, {df['date'].nunique() if len(df) else 0} dates "
          f"[{time.time()-t0:.0f}s] -> {PANEL_CSV}")
    return df


# ─────────────────────────────────────────────────────────────────────────────
# Statistics (pure)
# ─────────────────────────────────────────────────────────────────────────────

def per_date_ic(df: pd.DataFrame, metric: str, fwd: str) -> pd.DataFrame:
    out = []
    for d, g in df.groupby("date"):
        g = g.dropna(subset=[metric, fwd])
        if len(g) < MIN_XSEC:
            continue
        ic = g[metric].rank().corr(g[fwd].rank())
        out.append({"date": d, "regime": g["regime"].iloc[0], "ic": ic, "n": len(g)})
    return pd.DataFrame(out)


def newey_west_t(x: np.ndarray, lags: int) -> float:
    x = np.asarray(x, float)
    n = len(x)
    if n < 3:
        return float("nan")
    e = x - x.mean()
    var = e @ e / n
    for k in range(1, min(lags, n - 1) + 1):
        w = 1 - k / (lags + 1)
        var += 2 * w * (e[k:] @ e[:-k]) / n
    if var <= 0:
        return float("nan")
    return float(x.mean() / math.sqrt(var / n))


def block_bootstrap_ci(x: np.ndarray, block: int, n_boot: int = N_BOOT,
                       seed: int = 7) -> tuple:
    x = np.asarray(x, float)
    n = len(x)
    if n < 3:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    b = max(1, min(block, n))
    nb = int(math.ceil(n / b))
    means = np.empty(n_boot)
    for i in range(n_boot):
        starts = rng.integers(0, n, nb)
        idx = (starts[:, None] + np.arange(b)[None, :]).ravel()[:n] % n
        means[i] = x[idx].mean()
    return (float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5)))


def episodes(dates_regimes: pd.DataFrame) -> pd.Series:
    """Episodes = runs of consecutive sample dates carrying the same label."""
    s = dates_regimes.sort_values("date")["regime"].reset_index(drop=True)
    run_id = (s != s.shift()).cumsum()
    return pd.DataFrame({"regime": s, "run": run_id}).groupby("regime")["run"].nunique()


def summarise(ics: pd.DataFrame, lags: int, eps: pd.Series) -> Dict:
    x = ics.sort_values("date")["ic"].to_numpy()
    lo, hi = block_bootstrap_ci(x, block=max(lags, 1))
    return {"dates": len(x), "episodes": int(eps) if eps is not None else 0,
            "mean_ic": float(np.mean(x)) if len(x) else float("nan"),
            "hit_pct": float((x > 0).mean() * 100) if len(x) else float("nan"),
            "nw_t": newey_west_t(x, lags), "ci_lo": lo, "ci_hi": hi,
            "stocks_per_date": float(ics["n"].mean()) if len(x) else float("nan")}


def quintile_spread(df: pd.DataFrame, metric: str, fwd: str) -> pd.DataFrame:
    """Per-date quintile mean fwd return, then averaged across dates."""
    per = []
    for d, g in df.groupby("date"):
        g = g.dropna(subset=[metric, fwd])
        if len(g) < MIN_XSEC:
            continue
        q = pd.qcut(g[metric].rank(method="first"), 5, labels=[1, 2, 3, 4, 5])
        m = g.groupby(q, observed=True)[fwd].mean()
        per.append(m.rename(d))
    if not per:
        return pd.DataFrame()
    return pd.DataFrame(per)


def analyse(df: pd.DataFrame) -> Dict[str, pd.DataFrame]:
    dr = df[["date", "regime"]].drop_duplicates()
    eps = episodes(dr)
    ic_rows, q_rows, yr_rows, sp_rows = [], [], [], []
    for met in METRICS:
        for fwd, h in HORIZONS.items():
            lags = h // SAMPLE_STEP
            ics = per_date_ic(df, met, fwd)
            if ics.empty:
                continue
            qd = quintile_spread(df, met, fwd)
            groups = [("ALL", ics)] + list(ics.groupby("regime"))
            for reg, g in groups:
                e = eps.sum() if reg == "ALL" else eps.get(reg, 0)
                ic_rows.append({"metric": met, "horizon": fwd, "regime": reg,
                                **summarise(g, lags, e)})
                if not qd.empty:
                    qq = qd.loc[qd.index.isin(g["date"])]
                    if len(qq):
                        mq = qq.mean()
                        diffs = np.diff(mq.to_numpy())
                        q_rows.append({"metric": met, "horizon": fwd, "regime": reg,
                                       "dates": len(qq),
                                       **{f"Q{k}": round(float(mq.get(k, np.nan)), 2)
                                          for k in range(1, 6)},
                                       "Q5_Q1": round(float(mq.get(5) - mq.get(1)), 2),
                                       "monotone_steps": f"{int((diffs > 0).sum())}/4",
                                       "Q5gtQ1_dates_pct": round(float(
                                           ((qq[5] - qq[1]) > 0).mean() * 100), 1)})
                sub = g.assign(year=g["date"].str[:4])
                for yr, gy in sub.groupby("year"):
                    yr_rows.append({"metric": met, "horizon": fwd, "regime": reg,
                                    "year": yr, "dates": len(gy),
                                    "mean_ic": round(float(gy["ic"].mean()), 4)})
                for part, gp in [("train", g[g["date"] < SPLIT_DATE]),
                                 ("holdout", g[g["date"] >= SPLIT_DATE])]:
                    x = gp.sort_values("date")["ic"].to_numpy()
                    sp_rows.append({"metric": met, "horizon": fwd, "regime": reg,
                                    "part": part, "dates": len(x),
                                    "mean_ic": round(float(x.mean()), 4) if len(x) else np.nan,
                                    "nw_t": round(newey_west_t(x, lags), 2) if len(x) else np.nan})
    ic = pd.DataFrame(ic_rows)
    for c in ["mean_ic", "hit_pct", "nw_t", "ci_lo", "ci_hi", "stocks_per_date"]:
        if c in ic:
            ic[c] = ic[c].round(3)
    return {"ic": ic, "quintiles": pd.DataFrame(q_rows), "by_year": pd.DataFrame(yr_rows),
            "split": pd.DataFrame(sp_rows), "dates": dr.sort_values("date")}


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--from-cache", action="store_true",
                    help="analyse research/output/fgr_panel.csv without fetching")
    args = ap.parse_args(argv)

    if args.from_cache:
        if not os.path.exists(PANEL_CSV):
            print(f"{PANEL_CSV} missing — run without --from-cache first.")
            return 1
        df = pd.read_csv(PANEL_CSV)
    else:
        df = build_panel(limit=args.limit, workers=args.workers)
    if df.empty:
        print("No observations.")
        return 1
    df["date"] = df["date"].astype(str)

    res = analyse(df)
    os.makedirs(OUT_DIR, exist_ok=True)
    for k, name in [("ic", "fgr_ic_by_regime"), ("quintiles", "fgr_quintiles"),
                    ("by_year", "fgr_by_year"), ("split", "fgr_split"), ("dates", "fgr_dates")]:
        res[k].to_csv(os.path.join(OUT_DIR, f"{name}.csv"), index=False, encoding="utf-8")

    pd.set_option("display.width", 220)
    pd.set_option("display.max_rows", 200)
    print("\nSample dates per regime:", res["dates"]["regime"].value_counts().to_dict())
    print("Coverage: tickers", df["ticker"].nunique(), "| rows", len(df),
          "| rev_g non-null", f"{df['rev_g'].notna().mean()*100:.0f}%")
    print("\n=== Per-date Spearman IC by regime ===")
    print(res["ic"].to_string(index=False))
    print("\n=== Quintiles (per-date means, averaged) ===")
    print(res["quintiles"].to_string(index=False))
    print("\n=== Train / holdout (split", SPLIT_DATE, ") ===")
    print(res["split"].to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
