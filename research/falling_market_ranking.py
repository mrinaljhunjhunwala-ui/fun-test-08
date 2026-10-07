"""
research/falling_market_ranking.py — audit item 8: is there anything that ranks
NSE stocks in `range` and `trend_down` regimes, where the composite carries
almost no information (10y weekly IC20: range -0.01, trend_down +0.04 with
~9 episodes)?

Prior work (docs/RESEARCH_LOG.md) already tested, in raw form: lowvol (60d
stdev), near52wh, mom12_1, dma200, rs_score, turnover, rev5/rev20 (#294),
sector-neutral rev20 (#299), vol-scaled momentum (#290) and revenue growth
(#301/#306). This script tests only NEW candidates:

  lowbeta       minus 120-session beta of daily returns vs NIFTY 50 (^NSEI)
  lowbeta_sn    lowbeta minus its week x sector median          (sector-neutral)
  lowvol_sn     minus 60-session stdev, sector-neutral          (raw form was #294)
  near52wh_sn   close / 252-session high - 1, sector-neutral    (raw form was #294)
  rs_sec20      20-session return minus week x sector median    (= -rev20_within of #299; reference)
  rs_sec60      60-session return minus week x sector median
  blend_rev     regime switch: pct-rank of score90 in trend_up, pct-rank of
                sector-neutral reversal (-rs_sec20) in every other regime
  blend_def     regime switch: score90 in trend_up, mean pct-rank of
                (-rs_sec20, lowbeta) elsewhere
  score90       production composite (benchmark)

Sector-neutral = value minus the week x sector median, kept only for sectors
with >= 5 stocks that week. Both blends were fixed BEFORE looking at results
(the reversal leg is the #299 lead; the defensive leg is this study's a-priori
candidate), so they are not tuned on the outcome.

Quality proxies (ROE stability, leverage) are NOT tested: there is no
point-in-time fundamentals history over the 10y window (fundamental_quality.py
starts 2022-10 with restated yfinance values; see #294 / #301 / #306).

Statistics follow the RESEARCH_LOG conventions: per-week cross-sectional
Spearman IC (min 30 stocks), Newey-West t (lags = horizon in weeks: 1/4/12),
train/holdout halves by week, the #235 two-fold rule (alternating calendar
quarters; paired weekly IC20 diff vs score90, NW t > 2 in BOTH folds), and a
cost check: top-decile turnover between rebalances 4 weeks apart, long-only
top-decile excess return (vs the week mean) net of turnover x Indian delivery
round-trip cost (trading.costs.representative_round_trip_pct at Rs 2L), and
d10-d1 net of two legs.

PRODUCTION SCORING IS NOT MODIFIED. Educational research only.

Run (from a frozen git-archive snapshot):
    py -m research.falling_market_ranking --obs research/output/observations.csv
Outputs (research/output/):
    fallmkt_prices.pkl   close panel cache (re-used on later runs)
    fallmkt_ic.csv       regime x factor x horizon weekly IC
    fallmkt_folds.csv    #235 rule per regime x factor
    fallmkt_cost.csv     turnover / gross / net spreads per regime x factor
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

from research.score_regime_alt_factors import _MIN_XS, _nw_t, _weekly_ic  # noqa: E402

OUT_DIR = os.path.join(_ROOT, "research", "output")
NEW = ["lowbeta", "lowbeta_sn", "lowvol_sn", "near52wh_sn", "rs_sec20", "rs_sec60", "blend_rev", "blend_def"]
FACTORS = ["score90"] + NEW
HORIZONS = {"fwd_5d": 1, "fwd_20d": 4, "fwd_60d": 12}
REGIMES = ["trend_up", "range", "trend_down", "risk_off"]
_MIN_SEC = 5
_INDEX = "^NSEI"


def _closes(tickers, period: str, workers: int) -> pd.DataFrame:
    from data.fetcher import fetch_index, fetch_single
    out = {}

    def one(t):
        df = fetch_single(t, period=period)
        return None if df is None or df.empty else df["Close"].astype(float)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(one, t): t for t in tickers}
        for i, fu in enumerate(as_completed(futs), 1):
            try:
                s = fu.result()
                if s is not None:
                    out[futs[fu]] = s
            except Exception as e:  # noqa: BLE001 - research script, keep going
                print(f"  skip {futs[fu]}: {type(e).__name__}: {e}")
            if i % 100 == 0:
                print(f"  fetched {i}/{len(tickers)}")
    idx = fetch_index(_INDEX, period=period)
    if idx is not None and not idx.empty:
        out[_INDEX] = idx["Close"].astype(float)
    for k, s in out.items():
        s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
        out[k] = s[~s.index.duplicated(keep="last")]
    return pd.DataFrame(out).sort_index()


def _features(px: pd.DataFrame) -> pd.DataFrame:
    if _INDEX not in px:
        raise SystemExit("index series missing; cannot compute beta")
    r = px.pct_change(fill_method=None)
    m = r[_INDEX]
    stocks = [c for c in px.columns if c != _INDEX]
    cov = r[stocks].rolling(120, min_periods=100).cov(m)
    var = m.rolling(120, min_periods=100).var()
    f = {
        "lowbeta": -cov.div(var, axis=0),
        "lowvol": -r[stocks].rolling(60, min_periods=50).std(),
        "near52wh": px[stocks] / px[stocks].rolling(252, min_periods=200).max() - 1,
        "ret20": px[stocks].pct_change(20, fill_method=None),
        "ret60": px[stocks].pct_change(60, fill_method=None),
    }
    long = [v.stack().rename(k) for k, v in f.items()]
    out = pd.concat(long, axis=1)
    out.index.names = ["date", "ticker"]
    return out.reset_index()


def _sector_neutral(d: pd.DataFrame) -> pd.DataFrame:
    key = ["week", "sector"]
    ok = d.groupby(key)["ticker"].transform("count") >= _MIN_SEC
    for src, dst in (("lowbeta", "lowbeta_sn"), ("lowvol", "lowvol_sn"), ("near52wh", "near52wh_sn"),
                     ("ret20", "rs_sec20"), ("ret60", "rs_sec60")):
        d[dst] = (d[src] - d.groupby(key)[src].transform("median")).where(ok)
    pr = lambda c: d.groupby("week")[c].rank(pct=True)  # noqa: E731
    up = d["live_regime"] == "trend_up"
    rev = pr("rs_sec20").rsub(1)                      # sector-neutral reversal, as a pct rank
    d["blend_rev"] = np.where(up, pr("score90"), rev)
    d["blend_def"] = np.where(up, pr("score90"), (rev + pr("lowbeta")) / 2)
    return d


def _ic_row(g, col, fwd, lags) -> dict:
    s = _weekly_ic(g, col, fwd)
    x = s.to_numpy()
    cut = s.index.sort_values()[len(s) // 2] if len(s) else None
    tr = s[s.index < cut] if cut is not None else s
    ho = s[s.index >= cut] if cut is not None else s
    return {"weeks": len(x), "ic": round(float(x.mean()), 4) if len(x) else np.nan,
            "nw_t": round(_nw_t(x, lags), 2),
            "pct_pos": round(100 * float((x > 0).mean()), 1) if len(x) else np.nan,
            "ic_train": round(float(tr.mean()), 4) if len(tr) else np.nan,
            "ic_hold": round(float(ho.mean()), 4) if len(ho) else np.nan}


def _folds(g, col) -> list:
    diff = (_weekly_ic(g, col, "fwd_20d") - _weekly_ic(g, "score90", "fwd_20d")).dropna()
    q = diff.index.year * 4 + diff.index.quarter
    rows = []
    for fold, s in (("A", diff[q % 2 == 0]), ("B", diff[q % 2 == 1])):
        rows.append({"fold": fold, "weeks": len(s), "ic_diff": round(float(s.mean()), 4) if len(s) else np.nan,
                     "nw_t": round(_nw_t(s.to_numpy(), 4), 2)})
    return rows


def _cost(g, col, cost_pct) -> dict:
    """Top/bottom decile per week; turnover vs the week 4 weeks later (20-session rebalance)."""
    m = g[["week", "ticker", col, "fwd_20d"]].dropna()
    m = m[m.groupby("week")[col].transform("size") >= _MIN_XS]
    if m.empty:
        return {}
    m = m.assign(dm=m["fwd_20d"] - m.groupby("week")["fwd_20d"].transform("mean"),
                 pr=m.groupby("week")[col].rank(pct=True))
    top = m[m["pr"] > 0.9]
    bot = m[m["pr"] <= 0.1]
    sets = top.groupby("week")["ticker"].apply(set)
    to = []
    for w, s in sets.items():
        nxt = sets.get(w + 4)
        if nxt is not None and s:
            to.append(1 - len(s & nxt) / len(s))
    turn = float(np.mean(to)) if to else np.nan
    t_ex = top.groupby("week")["dm"].mean()
    ls = (t_ex - bot.groupby("week")["dm"].mean()).dropna()
    c = 100 * cost_pct * turn
    return {"turnover": round(turn, 3), "top_gross": round(float(t_ex.mean()), 2),
            "top_t": round(_nw_t(t_ex.to_numpy(), 4), 2), "top_net": round(float(t_ex.mean()) - c, 2),
            "top_net_t": round(_nw_t(t_ex.to_numpy() - c, 4), 2),
            "ls_gross": round(float(ls.mean()), 2), "ls_net": round(float(ls.mean()) - 2 * c, 2),
            "ls_net_t": round(_nw_t(ls.to_numpy() - 2 * c, 4), 2)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--obs", default=os.path.join(OUT_DIR, "observations.csv"))
    ap.add_argument("--prices", default=os.path.join(OUT_DIR, "fallmkt_prices.pkl"))
    ap.add_argument("--period", default="10y")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args(argv)

    if not os.path.exists(args.obs):
        print(f"missing input: {args.obs}; nothing to do")
        return 0
    obs = pd.read_csv(args.obs, parse_dates=["date"])
    obs = obs.dropna(subset=["fwd_20d", "live_regime", "sector"])
    obs["date"] = obs["date"].dt.normalize()
    if os.path.exists(args.prices):
        px = pd.read_pickle(args.prices)
    else:
        px = _closes(sorted(obs["ticker"].unique()), args.period, args.workers)
        os.makedirs(OUT_DIR, exist_ok=True)
        px.to_pickle(args.prices)
    if px.empty:
        print("no price data; nothing to do")
        return 0
    from trading.costs import representative_round_trip_pct
    cost = representative_round_trip_pct()

    d = obs.merge(_features(px), on=["date", "ticker"], how="inner")
    d["week"] = d["date"].dt.to_period("W-FRI")
    d = _sector_neutral(d)
    print(f"FALLING-MARKET RANKING | rows={len(d)} tickers={d.ticker.nunique()} weeks={d.week.nunique()} "
          f"{d.date.min().date()} -> {d.date.max().date()} | round-trip cost {100 * cost:.3f}%")

    groups = [("ALL", d)] + [(r, d[d["live_regime"] == r]) for r in REGIMES]
    ic, folds, costs = [], [], []
    for reg, g in groups:
        for col in FACTORS:
            for fwd, h in HORIZONS.items():
                ic.append({"regime": reg, "factor": col, "horizon": fwd, **_ic_row(g, col, fwd, h)})
            costs.append({"regime": reg, "factor": col, **_cost(g, col, cost)})
            if col != "score90":
                fr = _folds(g, col)
                ok = all(r["nw_t"] > 2 for r in fr)
                folds += [{"regime": reg, "factor": col, **r, "passes_rule": ok} for r in fr]
    ic_t, fold_t, cost_t = pd.DataFrame(ic), pd.DataFrame(folds), pd.DataFrame(costs)
    os.makedirs(OUT_DIR, exist_ok=True)
    ic_t.to_csv(os.path.join(OUT_DIR, "fallmkt_ic.csv"), index=False)
    fold_t.to_csv(os.path.join(OUT_DIR, "fallmkt_folds.csv"), index=False)
    cost_t.to_csv(os.path.join(OUT_DIR, "fallmkt_cost.csv"), index=False)
    pd.set_option("display.width", 240)
    pd.set_option("display.max_rows", 500)
    print("\n=== WEEKLY IC ===\n", ic_t.to_string(index=False))
    print("\n=== #235 RULE (paired IC20 diff vs score90) ===\n", fold_t.to_string(index=False))
    print("\n=== COST CHECK (fwd_20d, pts per 20 sessions) ===\n", cost_t.to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
