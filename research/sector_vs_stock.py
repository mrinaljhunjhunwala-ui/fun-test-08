"""
research/sector_vs_stock.py — does the composite score pick SECTORS or STOCKS?

Question (from the 2026-09-30 calibration read-out): on the live ledger, most of
STRONG BUY's 5-session edge over NIFTY came from the pick's sector, not from the
stock beating its own sector. That ledger sample is tiny and pre-registered for
October, so it must not be used for tuning. This study asks the same question on
HISTORY instead, using the production score replayed walk-forward by
research/score_efficacy.py (read-only w.r.t. the model).

Each observation's forward return is split into three additive parts:

    stock return = market            (median of the whole universe)
                 + sector − market   (its sector's median minus the market)
                 + stock − sector    (what's left: stock selection)

and the score is tested against each part:

  • sector timing    — per week, rank-correlate each sector's MEAN score with its
                       forward (sector − market) return, across sectors;
  • stock selection  — per week, rank-correlate score with (stock − sector),
                       across all stocks;
  • top-vs-bottom    — top-quintile minus bottom-quintile of score, split into
                       its sector part and its stock part.

Every weekly information coefficient (IC) is averaged over weeks and given a
t-stat over weeks (weeks, not rows, are the independent unit). Results are also
split by production regime and by first/second half of the sample.

Hold-out: sample weeks whose forward window would end on/after
LIVE_LEDGER_START are dropped, so nothing here overlaps the period the
pre-registered live test (analysis.verdict_ledger.DECISION_RULE) is judged on.

Known limits (also printed in the report): today's universe (survivorship);
historical scores have no delivery-% input (the live score does); baskets are
price-only medians of that universe, not official indices.

Run (network: ~500 tickers of daily bars; several minutes):
    py -m research.sector_vs_stock              # 2y, full universe
    py -m research.sector_vs_stock --period 3y
    py -m research.sector_vs_stock --limit 60   # quick pipeline check

Outputs (research/output/):
    sector_vs_stock_obs.csv       one row per (ticker, week) with the split
    sector_vs_stock_summary.csv   IC / spread table by horizon and slice
    sector_vs_stock_report.md     plain-English summary
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

OUT_DIR = os.path.join(_ROOT, "research", "output")
HORIZONS = (5, 20, 60)
LIVE_LEDGER_START = dt.date(2026, 8, 29)     # first verdict_log row; hold out from here
MIN_SECTOR_NAMES = 5
MIN_SECTORS_PER_WEEK = 6
MIN_STOCKS_PER_WEEK = 30


# ─────────────────────────────────────────────────────────────────────────────
# Pure analysis (unit-tested in tests/test_sector_vs_stock.py)
# ─────────────────────────────────────────────────────────────────────────────

def _naive_daily(s: pd.Series) -> pd.Series:
    """Index → tz-naive midnight dates, so it lines up with obs['date']."""
    idx = pd.DatetimeIndex(s.index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    out = s.copy()
    out.index = idx.normalize()
    return out[~out.index.duplicated(keep="last")]


def forward_return_panel(closes: Dict[str, pd.Series], h: int) -> pd.DataFrame:
    """date × ticker forward return (%) over h trading rows of each series."""
    cols = {}
    for t, s in closes.items():
        s = pd.to_numeric(s, errors="coerce").dropna()
        s = s[~s.index.duplicated(keep="last")].sort_index()
        cols[t] = (s.shift(-h) / s - 1.0) * 100.0
    return pd.DataFrame(cols)


def sector_medians(panel: pd.DataFrame, sector_of: Dict[str, str],
                   min_names: int = MIN_SECTOR_NAMES) -> pd.DataFrame:
    """date × sector median forward return (NaN below min_names), plus a
    'MARKET' column = median across the whole universe that date."""
    out = {}
    for sector in sorted({s for s in sector_of.values() if s and s != "Other"}):
        cols = [t for t in panel.columns if sector_of.get(t) == sector]
        if not cols:
            continue
        block = panel[cols]
        med = block.median(axis=1)
        med[block.notna().sum(axis=1) < min_names] = np.nan
        out[sector] = med
    res = pd.DataFrame(out, index=panel.index)
    res["MARKET"] = panel.median(axis=1)
    return res


def decompose(obs: pd.DataFrame, sect: pd.DataFrame, h: int) -> pd.DataFrame:
    """Add market / sector_part / stock_part columns for horizon h to obs
    (needs columns date, sector, fwd_{h}d). Rows without a sector basket
    value that week get NaN parts."""
    o = obs.copy()
    d = pd.to_datetime(o["date"])
    mkt = sect["MARKET"].reindex(d).to_numpy()
    sec_vals = np.full(len(o), np.nan)
    for sector in o["sector"].dropna().unique():
        if sector not in sect.columns:
            continue
        m = (o["sector"] == sector).to_numpy()
        sec_vals[m] = sect[sector].reindex(d[m]).to_numpy()
    ret = pd.to_numeric(o[f"fwd_{h}d"], errors="coerce").to_numpy()
    o[f"market_{h}d"] = mkt
    o[f"sector_part_{h}d"] = sec_vals - mkt
    o[f"stock_part_{h}d"] = ret - sec_vals
    return o


def _ic_summary(ics: pd.Series) -> Dict[str, float]:
    ics = ics.dropna()
    n = len(ics)
    if n < 3:
        return {"ic_mean": np.nan, "ic_t": np.nan, "weeks": n, "pct_weeks_pos": np.nan}
    sd = ics.std(ddof=1)
    return {
        "ic_mean": float(ics.mean()),
        "ic_t": float(ics.mean() / (sd / np.sqrt(n))) if sd > 0 else np.nan,
        "weeks": n,
        "pct_weeks_pos": float((ics > 0).mean() * 100.0),
    }


def stock_selection_ic(o: pd.DataFrame, h: int) -> Dict[str, float]:
    """Per week: Spearman(score, stock − sector) across stocks; summarised."""
    col = f"stock_part_{h}d"
    per_week = []
    for _, g in o[["date", "score", col]].dropna().groupby("date"):
        if len(g) >= MIN_STOCKS_PER_WEEK:
            per_week.append(g["score"].rank().corr(g[col].rank()))
    return _ic_summary(pd.Series(per_week, dtype=float))


def sector_timing_ic(o: pd.DataFrame, h: int) -> Dict[str, float]:
    """Per week: Spearman(sector mean score, sector − market) across sectors."""
    col = f"sector_part_{h}d"
    per_week = []
    d = o[["date", "sector", "score", col]].dropna()
    for _, g in d.groupby("date"):
        s = g.groupby("sector").agg(score=("score", "mean"), part=(col, "first"))
        if len(s) >= MIN_SECTORS_PER_WEEK:
            per_week.append(s["score"].rank().corr(s["part"].rank()))
    return _ic_summary(pd.Series(per_week, dtype=float))


def quintile_split(o: pd.DataFrame, h: int) -> Dict[str, float]:
    """Top-minus-bottom score quintile (per week, then averaged over weeks):
    total forward return spread and its sector / stock parts."""
    cols = [f"fwd_{h}d", f"sector_part_{h}d", f"stock_part_{h}d"]
    rows = []
    for _, g in o[["date", "score", *cols]].dropna().groupby("date"):
        if len(g) < MIN_STOCKS_PER_WEEK:
            continue
        q = pd.qcut(g["score"].rank(method="first"), 5, labels=False)
        top, bot = g[q == 4], g[q == 0]
        rows.append([top[c].mean() - bot[c].mean() for c in cols])
    if not rows:
        return {"spread_total": np.nan, "spread_sector": np.nan, "spread_stock": np.nan,
                "weeks": 0}
    a = np.array(rows)
    return {"spread_total": float(a[:, 0].mean()), "spread_sector": float(a[:, 1].mean()),
            "spread_stock": float(a[:, 2].mean()), "weeks": len(rows)}


def summarise(o: pd.DataFrame, horizons=HORIZONS,
              slice_col: Optional[str] = None) -> pd.DataFrame:
    """One row per (horizon[, slice]) with sector-timing IC, stock-selection IC
    and the quintile spread split."""
    groups = [("all", o)] if slice_col is None else list(o.groupby(slice_col))
    out = []
    for label, g in groups:
        for h in horizons:
            if f"stock_part_{h}d" not in g.columns:
                continue
            st, sk, qs = sector_timing_ic(g, h), stock_selection_ic(g, h), quintile_split(g, h)
            out.append({
                "slice": label, "horizon": h,
                "sector_ic": st["ic_mean"], "sector_ic_t": st["ic_t"], "sector_weeks": st["weeks"],
                "stock_ic": sk["ic_mean"], "stock_ic_t": sk["ic_t"], "stock_weeks": sk["weeks"],
                "spread_total": qs["spread_total"], "spread_sector": qs["spread_sector"],
                "spread_stock": qs["spread_stock"],
            })
    return pd.DataFrame(out)


def hold_out_live_period(o: pd.DataFrame, trading_days: pd.DatetimeIndex,
                         h: int, cutoff: dt.date = LIVE_LEDGER_START) -> pd.DataFrame:
    """Drop rows whose h-row forward window ends on/after `cutoff`."""
    days = pd.DatetimeIndex(sorted(set(trading_days)))
    pos = days.get_indexer(pd.to_datetime(o["date"]))
    end_pos = pos + h
    ok = (pos >= 0) & (end_pos < len(days))
    end_dates = np.full(len(o), np.datetime64("NaT"), dtype="datetime64[ns]")
    end_dates[ok] = days[end_pos[ok]].values
    keep = ok & (end_dates < np.datetime64(cutoff))
    return o[keep]


# ─────────────────────────────────────────────────────────────────────────────
# Report
# ─────────────────────────────────────────────────────────────────────────────

def _fmt(v, nd=3):
    return "—" if v is None or (isinstance(v, float) and np.isnan(v)) else f"{v:.{nd}f}"


def render_report(summary: pd.DataFrame, meta: Dict[str, str]) -> str:
    lines = [
        "# Sector timing vs stock selection — composite score (historical replay)",
        "",
        f"Generated {meta.get('generated', '')} · period {meta.get('period', '')} · "
        f"{meta.get('tickers', '')} tickers · {meta.get('weeks', '')} weekly samples · "
        f"weeks ending on/after {LIVE_LEDGER_START} held out.",
        "",
        "**IC** = average weekly rank correlation between the score and the outcome "
        "(0 = no information; ~0.03–0.05 is typical of a useful equity signal). "
        "**t** = IC mean ÷ its standard error across weeks (|t| ≥ 2 ≈ unlikely by chance). "
        "**Spread** = top-minus-bottom score quintile forward return (percentage points), "
        "split into the part explained by sector and the part left to stock selection.",
        "",
        "| Slice | Horizon | Sector-timing IC (t) | Stock-selection IC (t) | "
        "Spread total | of which sector | of which stock |",
        "|---|---|---|---|---|---|---|",
    ]
    for _, r in summary.iterrows():
        lines.append(
            f"| {r['slice']} | {int(r['horizon'])}d | {_fmt(r['sector_ic'])} ({_fmt(r['sector_ic_t'], 1)}) | "
            f"{_fmt(r['stock_ic'])} ({_fmt(r['stock_ic_t'], 1)}) | {_fmt(r['spread_total'], 2)} | "
            f"{_fmt(r['spread_sector'], 2)} | {_fmt(r['spread_stock'], 2)} |")
    lines += [
        "",
        "Limits: today's universe (survivorship); historical scores lack the live "
        "delivery-% input; baskets are medians of the app's universe, not official "
        "indices; in small sectors the basket is partly the scored stocks themselves, "
        "so some stock-picking leaks into 'sector timing'; weekly samples still overlap "
        "at 20/60-day horizons (t-stats optimistic).",
        "",
        "Research only — describes the model's historical behaviour; not investment advice.",
    ]
    return "\n".join(lines) + "\n"


# ─────────────────────────────────────────────────────────────────────────────
# Main (network + replay)
# ─────────────────────────────────────────────────────────────────────────────

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--period", default="2y")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--tag", default="", help="suffix for output files, e.g. 5y")
    ap.add_argument("--no-regime", action="store_true",
                    help="score without the live regime label (pre-#168 behaviour); by "
                         "default the replay passes it, as production does")
    args = ap.parse_args(argv)
    sfx = f"_{args.tag}" if args.tag else ""

    from data.universe import SECTOR_MAP, get_universe
    from research import score_efficacy as se

    universe = list(get_universe("nifty500"))
    if args.limit:
        universe = universe[: args.limit]
    os.makedirs(OUT_DIR, exist_ok=True)
    t0 = time.time()
    print(f"Universe {len(universe)} | period {args.period} | hold-out from {LIVE_LEDGER_START}")

    from data.fetcher import fetch_single
    bench = fetch_single("^NSEI", period=args.period)
    live_regimes = None
    try:
        from analysis.regime import classify_history
        vix = fetch_single("^INDIAVIX", period=args.period)
        live_regimes = classify_history(bench["Close"].astype(float),
                                        vix["Close"].astype(float) if vix is not None and not vix.empty
                                        else None, None)
    except Exception as e:
        print(f"  regime labels unavailable ({type(e).__name__}: {e})")

    frames: Dict[str, pd.DataFrame] = {}
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(se._prepare_ticker, t, args.period, bench): t for t in universe}
        for k, f in enumerate(as_completed(futs), 1):
            try:
                df = f.result()
                if df is not None:
                    frames[futs[f]] = df
            except Exception:
                pass
            if k % 50 == 0:
                print(f"  fetched {k}/{len(universe)} ({len(frames)} usable) [{time.time()-t0:.0f}s]")
    print(f"Usable tickers {len(frames)} [{time.time()-t0:.0f}s]")

    rows: List[Dict] = []
    for k, (t, df) in enumerate(frames.items(), 1):
        r, _ = se._walk_forward(t, df, SECTOR_MAP.get(t, "Other"), None, live_regimes,
                                not args.no_regime)
        rows.extend(r)
        if k % 50 == 0:
            print(f"  scored {k}/{len(frames)} ({len(rows)} obs) [{time.time()-t0:.0f}s]")
    obs = pd.DataFrame(rows)
    if obs.empty:
        print("No observations — aborting.")
        return 1

    closes = {t: _naive_daily(df["Close"]) for t, df in frames.items()}
    trading_days = pd.DatetimeIndex(sorted(set().union(*[set(s.index) for s in closes.values()])))

    decomposed = obs
    kept_by_h = {}
    for h in HORIZONS:
        sect = sector_medians(forward_return_panel(closes, h), SECTOR_MAP)
        decomposed = decompose(decomposed, sect, h)
        kept_by_h[h] = len(hold_out_live_period(decomposed, trading_days, h))
    # one hold-out using the longest horizon keeps every row comparable across horizons
    final = hold_out_live_period(decomposed, trading_days, max(HORIZONS))
    final = final.assign(half=np.where(pd.to_datetime(final["date"]) <
                                       pd.to_datetime(final["date"]).quantile(0.5),
                                       "first half", "second half"))

    parts = [summarise(final)]
    if "live_regime" in final.columns:
        parts.append(summarise(final, slice_col="live_regime"))
    parts.append(summarise(final, slice_col="half"))
    summary = pd.concat(parts, ignore_index=True)

    final.to_csv(os.path.join(OUT_DIR, f"sector_vs_stock_obs{sfx}.csv"), index=False)
    summary.to_csv(os.path.join(OUT_DIR, f"sector_vs_stock_summary{sfx}.csv"), index=False)
    meta = {"generated": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
            "period": args.period + (" (no regime label)" if args.no_regime
                                     else " (production regime handling)"),
            "tickers": str(len(frames)), "weeks": str(final["date"].nunique())}
    report = render_report(summary, meta)
    with open(os.path.join(OUT_DIR, f"sector_vs_stock_report{sfx}.md"), "w", encoding="utf-8") as fh:
        fh.write(report)
    print(report)
    print(f"Done [{time.time()-t0:.0f}s] — rows kept after hold-out: {len(final)} "
          f"(per-horizon: {kept_by_h})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
