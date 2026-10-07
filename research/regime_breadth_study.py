"""Regime trigger vs NSE market breadth (2026-10-07).

Questions
  1. How does the live regime label (analysis.regime, via score._live_regime_label)
     reach trend_down, and how late does it flip after a NIFTY turn?
  2. Daily NSE breadth from bhavcopy (sec_bhavdata_full, EQ series), 2019 onward.
  3. Three regime definitions, judged on the 10y observations:
       a. current   : analysis.regime.classify_history(nifty, vix, breadth=None)
       b. breadth   : VIX fear/panic -> risk_off, else breadth_regime()
                      (10d %>50DMA < 40 AND 10d A/D < 1 -> trend_down; > 60 -> trend_up)
       c. combined  : current, but trend_down -> range while 10d %>50DMA >= 50
     All thresholds are fixed here, before looking at results.
  4. Two-fold rule (#235): per-week IC of the mode a definition selects
     (bear mode proxy = 5-day reversal, bull mode proxy = 20-day momentum),
     minus the same for the current definition; t > 2 in both alternating-quarter folds.

Usage:
  py -m research.regime_breadth_study --start 2019-01-01 \
      --obs research/output/observations.csv --cache-dir data/cache/bhavcopy
  py -m research.regime_breadth_study --fetch-only      # just fill the bhavcopy cache

Educational research only; nothing here is investment advice.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import io
import os
import sys
import time

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from analysis.market_breadth import build_panels, compute_breadth, smoothed  # noqa: E402

OUT_DIR = os.path.join(ROOT, "research", "output", "regime_breadth")
DEFAULT_CACHE = os.path.join(ROOT, "data", "cache", "bhavcopy")
_MISSING = "_missing_dates.txt"
_BEAR = {"trend_down", "risk_off"}
_MIN_XS = 30

# Pre-declared thresholds (do not tune after seeing results).
BREADTH_WINDOW = 10
BEAR_PCT50, BULL_PCT50, BEAR_AD = 40.0, 60.0, 1.0
COMBINED_RELEASE_PCT50 = 50.0


# ── bhavcopy cache ──────────────────────────────────────────────────────────
def parse_full(csv_text: str) -> pd.DataFrame:
    """sec_bhavdata_full -> SYMBOL, SERIES, DATE, Open..Volume, PREV_CLOSE (EQ/BE).

    Columns are a superset of data.nse_bhavcopy_prices.parse_ohlcv, so the
    production reader can share the cache directory.
    """
    df = pd.read_csv(io.StringIO(csv_text), skipinitialspace=True, dtype=str)
    df.columns = [c.strip() for c in df.columns]
    need = {"SYMBOL", "SERIES", "DATE1", "PREV_CLOSE", "OPEN_PRICE", "HIGH_PRICE",
            "LOW_PRICE", "CLOSE_PRICE", "TTL_TRD_QNTY"}
    miss = need - set(df.columns)
    if miss:
        raise ValueError(f"bhavcopy schema drift: missing {sorted(miss)}")
    df = df.apply(lambda s: s.str.strip())
    df = df[df["SERIES"].str.upper().isin({"EQ", "BE"})]
    num = lambda c: pd.to_numeric(df[c].replace({"-": None, "": None}), errors="coerce")  # noqa: E731
    return pd.DataFrame({
        "SYMBOL": df["SYMBOL"].str.upper(), "SERIES": df["SERIES"].str.upper(),
        "DATE": pd.to_datetime(df["DATE1"], format="%d-%b-%Y").dt.date.astype(str),
        "Open": num("OPEN_PRICE"), "High": num("HIGH_PRICE"), "Low": num("LOW_PRICE"),
        "Close": num("CLOSE_PRICE"), "Volume": num("TTL_TRD_QNTY"),
        "PREV_CLOSE": num("PREV_CLOSE")})


def fill_cache(start: _dt.date, end: _dt.date, cache_dir: str, delay: float = 0.35) -> int:
    """Download missing sessions (weekdays; 404 = holiday, remembered). Returns files written."""
    from data.nse_delivery import _fetch_bhavcopy
    os.makedirs(cache_dir, exist_ok=True)
    mpath = os.path.join(cache_dir, _MISSING)
    missing = set(open(mpath).read().split()) if os.path.exists(mpath) else set()
    wrote, d, fails = 0, start, 0
    while d <= end:
        p = os.path.join(cache_dir, f"{d.isoformat()}.csv")
        ok_cached = os.path.exists(p) and "PREV_CLOSE" in open(p).readline()
        if d.weekday() < 5 and not ok_cached and d.isoformat() not in missing:
            try:
                fr = parse_full(_fetch_bhavcopy(d))
                # NSE serves the previous session's file on some holidays.
                if fr.empty or (fr["DATE"] != d.isoformat()).any():
                    raise ValueError(f"404-equivalent: file for {d} carries another session's date")
                fr.to_csv(p, index=False)
                wrote += 1
                fails = 0
            except Exception as e:  # 404 holiday, or rate limit
                msg = str(e)
                if "404" in msg:
                    if (_dt.date.today() - d).days > 3:
                        missing.add(d.isoformat())
                else:
                    fails += 1
                    print(f"  {d}: {type(e).__name__}: {msg[:100]}", flush=True)
                    if fails >= 5:
                        print("  5 consecutive non-404 failures: backing off 60s", flush=True)
                        time.sleep(60)
                        fails = 0
            time.sleep(delay)
            if wrote and wrote % 100 == 0:
                print(f"  ...{wrote} sessions cached (at {d})", flush=True)
                with open(mpath, "w") as fh:
                    fh.write("\n".join(sorted(missing)))
        d += _dt.timedelta(days=1)
    with open(mpath, "w") as fh:
        fh.write("\n".join(sorted(missing)))
    return wrote


def load_cache(cache_dir: str, start: _dt.date) -> pd.DataFrame:
    parts = []
    for f in sorted(os.listdir(cache_dir)):
        if not f.endswith(".csv") or f[:10] < start.isoformat():
            continue
        x = pd.read_csv(os.path.join(cache_dir, f))
        if "PREV_CLOSE" in x.columns and (x["DATE"].astype(str) == f[:10]).all():
            parts.append(x)
    return pd.concat(parts, ignore_index=True)


# ── regime definitions ──────────────────────────────────────────────────────
def _index_series(sym: str) -> pd.Series:
    from data.fetcher import fetch_single
    df = fetch_single(sym, period="10y")
    s = df["Close"].astype(float).dropna()
    s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
    return s[~s.index.duplicated(keep="last")]


def regime_frame(nifty: pd.Series, vix: pd.Series, breadth: pd.DataFrame) -> pd.DataFrame:
    from analysis.regime import _vix_component, classify_history
    cur = classify_history(nifty, vix, None)
    sm = smoothed(breadth, BREADTH_WINDOW).reindex(nifty.index).ffill(limit=3)
    vz = vix.reindex(nifty.index).ffill().apply(_vix_component)
    sma200 = nifty.rolling(200).mean()
    r = pd.DataFrame({"nifty": nifty, "sma200": sma200, "nifty_vs_sma200": nifty / sma200 - 1,
                      "vix": vix.reindex(nifty.index).ffill(), "current": cur,
                      "pct50_10d": sm["pct_above_50dma"], "ad_10d": sm["ad_ratio"]})
    b = pd.Series("range", index=r.index, dtype=object)
    b[r["pct50_10d"] > BULL_PCT50] = "trend_up"
    b[(r["pct50_10d"] < BEAR_PCT50) & (r["ad_10d"] < BEAR_AD)] = "trend_down"
    b[vz.isin(["fear", "panic"])] = "risk_off"
    b[r["pct50_10d"].isna()] = "unknown"
    r["breadth"] = b
    c = cur.copy()
    c[(cur == "trend_down") & (r["pct50_10d"] >= COMBINED_RELEASE_PCT50)] = "range"
    r["combined"] = c
    return r


def lag_table(reg: pd.DataFrame, col: str) -> pd.DataFrame:
    """Per bear episode (>= 5 sessions): NIFTY low inside it, and sessions from low to flip-off."""
    lab = reg[col].isin(_BEAR) & reg[col].ne("risk_off")
    runs = (lab != lab.shift()).cumsum()
    rows = []
    for _, g in reg[lab].groupby(runs[lab]):
        if len(g) < 5:
            continue
        low_d = g["nifty"].idxmin()
        idx = reg.index
        end_pos = idx.get_loc(g.index[-1])
        off = idx[end_pos + 1] if end_pos + 1 < len(idx) else None
        low_pos = idx.get_loc(low_d)
        rows.append({"start": g.index[0].date(), "end": g.index[-1].date(), "sessions": len(g),
                     "nifty_low_date": low_d.date(),
                     "sessions_low_to_off": (end_pos + 1 - low_pos) if off is not None else None,
                     "nifty_gain_low_to_off_pct": round((reg["nifty"].iloc[end_pos + 1] / g["nifty"].min() - 1)
                                                        * 100, 1) if off is not None else None})
    return pd.DataFrame(rows)


# ── evaluation on observations ─────────────────────────────────────────────
def _nw_t(x: np.ndarray, lags: int = 4) -> float:
    from research.score_regime_alt_factors import _nw_t as f
    return f(np.asarray(x, float), lags)


def weekly_ics(obs: pd.DataFrame, panels: dict) -> pd.DataFrame:
    adj = panels["adj"]
    r5 = (adj / adj.shift(5) - 1).stack().rename("ret5").reset_index()
    r5.columns = ["date", "sym", "ret5"]
    o = obs.copy()
    o["date"] = pd.to_datetime(o["date"]).dt.normalize()
    o["sym"] = o["ticker"].str.upper().str.removesuffix(".NS")
    o = o.merge(r5, on=["date", "sym"], how="left")
    o["mr"] = -o["ret5"]
    o["week"] = o["date"].dt.to_period("W").astype(str)

    def one(w):
        res = {"date": w["date"].iloc[0], "n": len(w)}
        for c in ("bl_mom20", "mr", "score90"):
            m = w[[c, "fwd_20d"]].dropna()
            res[c] = m[c].corr(m["fwd_20d"], method="spearman") if len(m) >= _MIN_XS else np.nan
        top = w[w["bl_mom20"] >= w["bl_mom20"].quantile(0.9)]
        res["topmom_ex20"] = top["fwd_20d"].mean() - w["fwd_20d"].mean()
        res["topmom_hit"] = (top["fwd_20d"] > 0).mean()
        return pd.Series(res)
    return o.groupby("week")[["date", "bl_mom20", "mr", "score90", "fwd_20d"]].apply(one)


def evaluate(wk: pd.DataFrame, reg: pd.DataFrame) -> tuple:
    w = wk.copy()
    lab = reg[["current", "breadth", "combined"]].reindex(pd.to_datetime(w["date"]), method="ffill")
    for c in lab:
        w[c] = lab[c].values
    w = w[w["breadth"] != "unknown"].dropna(subset=["bl_mom20", "mr"])
    q = pd.to_datetime(w["date"]).dt.year * 4 + (pd.to_datetime(w["date"]).dt.month - 1) // 3
    w["fold"] = np.where(q % 2 == 0, "A", "B")
    for d in ("current", "breadth", "combined"):
        w[f"ic_{d}"] = np.where(w[d].isin(_BEAR), w["mr"], w["bl_mom20"])
    rows, rule = [], []
    for d in ("current", "breadth", "combined"):
        bear = w[d].isin(_BEAR)
        rows.append({"definition": d, "weeks": len(w), "bear_weeks": int(bear.sum()),
                     "mom_ic_bear_wks": round(w.loc[bear, "bl_mom20"].mean(), 4),
                     "mom_ic_other_wks": round(w.loc[~bear, "bl_mom20"].mean(), 4),
                     "mr_ic_bear_wks": round(w.loc[bear, "mr"].mean(), 4),
                     "selected_mode_ic": round(w[f"ic_{d}"].mean(), 4),
                     "topmom_ex20_bear_wks": round(w.loc[bear, "topmom_ex20"].mean(), 2),
                     "topmom_ex20_other_wks": round(w.loc[~bear, "topmom_ex20"].mean(), 2)})
        if d == "current":
            continue
        diff = w[f"ic_{d}"] - w["ic_current"]
        flips = (w[d].isin(_BEAR) != w["current"].isin(_BEAR))
        for f in ("A", "B", "all"):
            m = (w["fold"] == f) if f != "all" else pd.Series(True, index=w.index)
            x = diff[m].values
            rule.append({"definition": d, "fold": f, "weeks": int(m.sum()),
                         "weeks_mode_differs": int((flips & m).sum()),
                         "mean_ic_gain": round(float(np.mean(x)), 4), "nw_t": round(_nw_t(x), 2)})
    # Weeks where a definition turns bear OFF while current is bear: was bull mode right?
    flip = []
    for d in ("breadth", "combined"):
        m = w["current"].isin(_BEAR) & ~w[d].isin(_BEAR)
        flip.append({"definition": d, "weeks_bear_switched_off": int(m.sum()),
                     "mom_ic": round(w.loc[m, "bl_mom20"].mean(), 4),
                     "mr_ic": round(w.loc[m, "mr"].mean(), 4),
                     "topmom_ex20": round(w.loc[m, "topmom_ex20"].mean(), 2),
                     "topmom_hit": round(w.loc[m, "topmom_hit"].mean(), 3)})
        m2 = ~w["current"].isin(_BEAR) & w[d].isin(_BEAR)
        flip.append({"definition": d + " (bear switched ON)", "weeks_bear_switched_off": int(m2.sum()),
                     "mom_ic": round(w.loc[m2, "bl_mom20"].mean(), 4),
                     "mr_ic": round(w.loc[m2, "mr"].mean(), 4),
                     "topmom_ex20": round(w.loc[m2, "topmom_ex20"].mean(), 2),
                     "topmom_hit": round(w.loc[m2, "topmom_hit"].mean(), 3)})
    return pd.DataFrame(rows), pd.DataFrame(rule), pd.DataFrame(flip), w


def action_hits(obs: pd.DataFrame, reg: pd.DataFrame) -> pd.DataFrame:
    o = obs[obs["action"].isin(["BUY", "WATCH"])].copy()
    o["date"] = pd.to_datetime(o["date"]).dt.normalize()
    lab = reg[["current", "breadth", "combined"]].reindex(o["date"], method="ffill")
    rows = []
    for d in ("current", "breadth", "combined"):
        o["lab"] = lab[d].values
        o2 = o[o["lab"].notna() & (o["lab"] != "unknown")]
        for (lb, act), g in o2.groupby(["lab", "action"]):
            rows.append({"definition": d, "label": lb, "action": act, "n": len(g),
                         "hit20": round((g["fwd_20d"] > 0).mean(), 3),
                         "mean_fwd20": round(g["fwd_20d"].mean(), 2)})
    return pd.DataFrame(rows)


def recent_check(panels: dict, reg: pd.DataFrame, universe: set, since: str) -> pd.DataFrame:
    """Sep-Oct 2026: forward return to the last session of high-20d-momentum names vs all."""
    adj = panels["adj"][[c for c in panels["adj"].columns if c in universe]]
    last = adj.index[-1]
    rows = []
    for d in adj.index[adj.index >= pd.Timestamp(since)][:-1]:
        mom = adj.loc[d] / adj.shift(20).loc[d] - 1
        fwd = adj.loc[last] / adj.loc[d] - 1
        rev = -(adj.loc[d] / adj.shift(5).loc[d] - 1)
        m = pd.DataFrame({"mom": mom, "fwd": fwd, "rev": rev}).dropna()
        top = m[m["mom"] >= m["mom"].quantile(0.9)]
        rows.append({"date": d.date(), "sessions_fwd": int((adj.index > d).sum()),
                     "current": reg["current"].get(d), "breadth": reg["breadth"].get(d),
                     "combined": reg["combined"].get(d),
                     "all_fwd_pct": round(m["fwd"].mean() * 100, 2),
                     "topmom_fwd_pct": round(top["fwd"].mean() * 100, 2),
                     "mom_ic": round(m["mom"].corr(m["fwd"], method="spearman"), 3),
                     "rev5_ic": round(m["rev"].corr(m["fwd"], method="spearman"), 3)})
    return pd.DataFrame(rows)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--start", default="2019-01-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--cache-dir", default=DEFAULT_CACHE)
    ap.add_argument("--obs", default=os.path.join(ROOT, "research", "output", "observations.csv"))
    ap.add_argument("--out", default=OUT_DIR)
    ap.add_argument("--delay", type=float, default=0.35)
    ap.add_argument("--fetch-only", action="store_true")
    ap.add_argument("--no-fetch", action="store_true")
    a = ap.parse_args(argv)
    start = _dt.date.fromisoformat(a.start)
    end = _dt.date.fromisoformat(a.end) if a.end else _dt.date.today()
    if not a.no_fetch:
        print(f"filling bhavcopy cache {start}..{end} -> {a.cache_dir}", flush=True)
        print(f"  wrote {fill_cache(start, end, a.cache_dir, a.delay)} new sessions", flush=True)
    if a.fetch_only:
        return 0
    os.makedirs(a.out, exist_ok=True)

    raw = load_cache(a.cache_dir, start)
    panels = build_panels(raw)
    br = compute_breadth(panels)
    br.to_csv(os.path.join(a.out, "breadth_daily.csv"))
    print(f"breadth: {len(br)} sessions {br.index[0].date()}..{br.index[-1].date()}", flush=True)

    nifty, vix = _index_series("^NSEI"), _index_series("^INDIAVIX")
    reg = regime_frame(nifty, vix, br)
    reg.to_csv(os.path.join(a.out, "regime_daily.csv"))
    for col in ("current", "breadth", "combined"):
        lag_table(reg, col).to_csv(os.path.join(a.out, f"lag_{col}.csv"), index=False)
    last30 = br.tail(30).join(reg[["current", "breadth", "combined", "nifty_vs_sma200",
                                   "pct50_10d", "ad_10d"]], how="left")
    last30.to_csv(os.path.join(a.out, "last30.csv"))

    if os.path.exists(a.obs):
        obs = pd.read_csv(a.obs, usecols=["ticker", "date", "score90", "action", "bl_mom20", "fwd_20d"])
        wk = weekly_ics(obs, panels)
        summ, rule, flip, w = evaluate(wk, reg)
        summ.to_csv(os.path.join(a.out, "definition_summary.csv"), index=False)
        rule.to_csv(os.path.join(a.out, "two_fold_rule.csv"), index=False)
        flip.to_csv(os.path.join(a.out, "flip_weeks.csv"), index=False)
        w.to_csv(os.path.join(a.out, "weekly.csv"), index=False)
        action_hits(obs, reg).to_csv(os.path.join(a.out, "action_hits.csv"), index=False)
        uni = set(obs["ticker"].str.upper().str.removesuffix(".NS"))
        print(summ.to_string(), rule.to_string(), flip.to_string(), sep="\n\n")
    else:
        uni = set(panels["adj"].columns)
        print(f"no observations at {a.obs}: skipping the IC evaluation")
    rc = recent_check(panels, reg, uni, "2026-08-01")
    rc.to_csv(os.path.join(a.out, "recent_2026.csv"), index=False)
    pd.set_option("display.width", 250)
    print(last30.round(2).to_string())
    print(rc.to_string())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
