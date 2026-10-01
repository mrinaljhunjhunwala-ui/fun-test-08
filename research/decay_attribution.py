"""
research/decay_attribution.py — why did the composite score's edge fade?

The 5y sector-vs-stock replay (docs/SECTOR_VS_STOCK_2026-09-30.md) showed the
score's weekly IC dropping to ~0 in the more recent half of the window. This
script asks two questions of that replay's saved observations (no network,
no re-scoring; read-only w.r.t. the model):

  1. Attribution — weekly cross-sectional rank IC of the score and of each
     pillar / raw feature vs forward excess return (stock − universe median),
     by half, by calendar year, by production regime, and by half × regime.
     If the IC inside each regime is stable across halves, the "decay" is a
     regime-MIX effect (more weeks in regimes where the model has no edge),
     not a broken signal.

  2. Variant screen for trend_down / range — candidate rankers built only from
     columns already in the observations, judged on two folds fixed BEFORE
     looking (alternating calendar quarters, A = even, B = odd). Adoption rule
     (pre-declared): a candidate must beat `current` at 20d with t > 2 in BOTH
     folds and the same sign. Anything else is not a production change.

Run (needs the obs CSV from `py -m research.sector_vs_stock --period 5y --tag 5y`):
    py -m research.decay_attribution --obs research/output/sector_vs_stock_obs_5y.csv
Output: research/output/decay_attribution_report.md
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(_ROOT, "research", "output")
MIN_STOCKS_PER_WEEK = 30
FEATURES = ("score90", "technical", "momentum", "volume", "rs_score",
            "bl_mom20", "bl_rsi", "bl_sma200d")
LOW_INFO_REGIMES = ("range", "trend_down")


def weekly_ic(obs: pd.DataFrame, col: str, target: str) -> Dict[str, float]:
    """Mean of per-week Spearman ICs, t-stat over weeks (weeks are the unit)."""
    def _one(g: pd.DataFrame) -> float:
        if len(g) < MIN_STOCKS_PER_WEEK or g[col].nunique() < 2:
            return np.nan
        return g[col].rank().corr(g[target].rank())
    s = obs.groupby("date")[[col, target]].apply(_one).dropna()
    n = len(s)
    sd = s.std() if n > 1 else np.nan
    t = s.mean() / sd * np.sqrt(n) if n > 2 and sd and np.isfinite(sd) and sd > 0 else np.nan
    return {"ic": float(s.mean()) if n else np.nan, "t": float(t) if np.isfinite(t) else np.nan,
            "weeks": n}


def add_targets(obs: pd.DataFrame) -> pd.DataFrame:
    o = obs.copy()
    for h in (20, 60):
        o[f"ex{h}"] = o[f"fwd_{h}d"] - o[f"market_{h}d"]
    d = pd.to_datetime(o["date"])
    q = d.dt.year * 4 + (d.dt.month - 1) // 3
    o["fold"] = np.where(q % 2 == 0, "A", "B")
    o["year"] = d.dt.year.astype(str)
    return o


def attribution(obs: pd.DataFrame, slice_cols=("half", "year", "live_regime"),
                target: str = "ex20") -> pd.DataFrame:
    rows: List[Dict] = []
    keys = [c for c in slice_cols if c in obs.columns]
    if {"half", "live_regime"} <= set(obs.columns):
        obs = obs.assign(half_x_regime=obs["half"] + " · " + obs["live_regime"])
        keys.append("half_x_regime")
    for key in keys:
        for val, g in obs.groupby(key):
            for f in FEATURES:
                if f in g.columns:
                    rows.append({"slice": key, "value": val, "feature": f, **weekly_ic(g, f, target)})
    return pd.DataFrame(rows)


def variants(o: pd.DataFrame) -> Dict[str, pd.Series]:
    pr = lambda c: o.groupby("date")[c].rank(pct=True)  # noqa: E731
    return {"current": o["score90"], "rs_only": o["rs_score"], "sma200_dist": o["bl_sma200d"],
            "mom20": o["bl_mom20"], "short_reversal": -o["bl_mom20"], "rsi_low": -o["bl_rsi"],
            "volume_pillar": o["volume"], "rs+sma200": pr("rs_score") + pr("bl_sma200d")}


def variant_screen(obs: pd.DataFrame, target: str = "ex20") -> pd.DataFrame:
    o = obs[obs["live_regime"].isin(LOW_INFO_REGIMES)].copy()
    vs = variants(o)
    rows = []
    for name, v in vs.items():
        o["_v"] = v
        for reg in LOW_INFO_REGIMES:
            for fold in ("A", "B"):
                g = o[(o["live_regime"] == reg) & (o["fold"] == fold)]
                rows.append({"regime": reg, "fold": fold, "variant": name, **weekly_ic(g, "_v", target)})
    res = pd.DataFrame(rows)
    cur = res[res.variant == "current"].set_index(["regime", "fold"])["ic"]
    res["beats_current"] = res.apply(lambda r: r["ic"] > cur.get((r["regime"], r["fold"]), np.nan), axis=1)
    return res


def adopted(screen: pd.DataFrame) -> List[str]:
    """Pre-declared rule: beats current, t > 2, in BOTH folds, per regime."""
    out = []
    for (reg, name), g in screen[screen.variant != "current"].groupby(["regime", "variant"]):
        if len(g) == 2 and g["beats_current"].all() and (g["t"] > 2).all():
            out.append(f"{reg}:{name}")
    return out


def _fmt(r) -> str:
    ic, t, w = r.ic, r.t, r.weeks
    return "n/a" if not np.isfinite(ic) else f"{ic:+.3f} (t {t:+.1f}, {int(w)}w)"


def render(att: pd.DataFrame, screen: pd.DataFrame, passed: List[str]) -> str:
    L = ["# Decay attribution — composite score, 5y replay", "",
         "Weekly rank IC vs 20-session excess return over the universe median.", ""]
    for key in att["slice"].unique():
        a = att[att.slice == key]
        L += [f"## By {key}", "", "| " + key + " | " + " | ".join(FEATURES) + " |",
              "|---" * (len(FEATURES) + 1) + "|"]
        for val, g in a.groupby("value"):
            m = {r.feature: _fmt(r) for r in g.itertuples()}
            L.append(f"| {val} | " + " | ".join(m.get(f, "") for f in FEATURES) + " |")
        L.append("")
    L += ["## Variant screen (trend_down / range, folds = alternating quarters)", "",
          "| regime | variant | fold A | fold B |", "|---|---|---|---|"]
    for (reg, name), g in screen.groupby(["regime", "variant"], sort=False):
        f = {r.fold: _fmt(r) for r in g.itertuples()}
        L.append(f"| {reg} | {name} | {f.get('A', '')} | {f.get('B', '')} |")
    L += ["", "**Adopted under the pre-declared rule:** " + (", ".join(passed) if passed else "none"), ""]
    return "\n".join(L)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Composite-score decay attribution (offline)")
    ap.add_argument("--obs", default=os.path.join(OUT_DIR, "sector_vs_stock_obs_5y.csv"))
    args = ap.parse_args(argv)
    if not os.path.exists(args.obs):
        print(f"obs CSV not found: {args.obs} — run research.sector_vs_stock --period 5y --tag 5y first")
        return 1
    obs = add_targets(pd.read_csv(args.obs))
    att = attribution(obs)
    screen = variant_screen(obs)
    passed = adopted(screen)
    os.makedirs(OUT_DIR, exist_ok=True)
    report = render(att, screen, passed)
    with open(os.path.join(OUT_DIR, "decay_attribution_report.md"), "w", encoding="utf-8") as fh:
        fh.write(report)
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
