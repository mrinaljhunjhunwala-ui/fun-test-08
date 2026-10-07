"""
backtest/optimizer.py — Phase 2a
Parameter optimisation for Indian equity trading strategies.

Samples multiple NSE tickers, grid-searches the best parameter
combination (maximising Sharpe Ratio), then returns a consensus
set of parameters for use in subsequent backtests.

Saves results to best_params.json in the working directory.
"""

import json
import warnings
import numpy as np
import pandas as pd
from backtesting import Backtest
from typing import Type, List, Dict, Optional

from data.fetcher import fetch_single
from utils.indicators import add_all_indicators

# FIX WARN1 — this was a blanket `warnings.filterwarnings("ignore")`. Called at
# import time from a library module, that switches off EVERY warning for the
# whole process, for every caller — including numpy's RuntimeWarning on
# "invalid value encountered in divide" / "All-NaN slice encountered", which is
# precisely the signal that would have surfaced the NaN-propagation bugs this
# codebase has had to rediscover by hand (see FIX IND1/IND2 in
# utils/indicators.py and FIX SCORE-NAN in analysis/score.py). Narrowed to the
# third-party API-churn categories that motivated the line in the first place,
# so genuine numerical warnings stay visible.
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", category=PendingDeprecationWarning)

PARAMS_FILE = "best_params.json"

# Indian market costs — same model as backtest/runner.py (trading/costs.py).
from backtest.runner import BT_KWARGS, COMMISSION  # noqa: E402

# ── Parameter grids ────────────────────────────────────────────────────────────
PARAM_GRIDS = {
    "RSIMACDStrategy": {
        "rsi_oversold":   range(25, 50, 5),
        "rsi_overbought": range(55, 80, 5),
        "atr_stop_mult":  [1.5, 2.0, 2.5],
        "atr_tp_mult":    [2.5, 3.0, 4.0],
    },
    "MomentumStrategy": {
        "momentum_threshold": [0.03, 0.05, 0.08, 0.10],
        "momentum_lookback":  [10, 15, 20, 30],
        "atr_stop_mult":      [1.0, 1.5, 2.0],
    },
}

CONSTRAINTS = {
    "RSIMACDStrategy": lambda p: p.rsi_oversold < p.rsi_overbought,
    "MomentumStrategy": None,
}


# Audit 2026-10-03: the grid search fits parameters on the same period the
# follow-on backtest reports, so those numbers are in-sample and overstated.
IN_SAMPLE_WARNING = (
    "WARNING: --optimize fits parameters on the same period it then reports - "
    "results below are in-sample and overstate what to expect out of sample; "
    "check them on a later period."
)
IN_SAMPLE_LABEL = "Results: in-sample (optimised on this same period)"


def in_sample_banner(optimised: bool) -> str:
    """Return the warning + label lines to print before optimised results ('' if not optimised)."""
    if not optimised:
        return ""
    return f"\n  {IN_SAMPLE_WARNING}\n  {IN_SAMPLE_LABEL}\n"


def _count_combos(grid: Dict) -> int:
    n = 1
    for v in grid.values():
        n *= len(list(v))
    return n


def optimize_strategy(
    tickers: List[str],
    strategy_cls: Type,
    period: str = "2y",
    cash: float = 1_000_000,
    sample_size: int = 5,
) -> Dict:
    """
    Grid-search best parameters across a sample of tickers.
    Consensus is computed as: mode (integer params) or mean (float params).

    Args:
        tickers:     Full ticker list — we draw the first `sample_size` from it
        strategy_cls: Strategy class to optimise
        period:      yfinance period string (e.g. "2y")
        cash:        Starting capital in INR
        sample_size: Tickers to optimise on (5 is fast; 10+ is thorough)

    Returns:
        dict of best parameters  e.g. {"rsi_oversold": 30, "rsi_overbought": 65, ...}
        Returns {} if optimisation fails (caller falls back to strategy defaults).
    """
    strategy_name = strategy_cls.__name__
    grid          = PARAM_GRIDS.get(strategy_name)
    constraint    = CONSTRAINTS.get(strategy_name)

    if not grid:
        print(f"  [optimizer] No parameter grid defined for {strategy_name}.")
        return {}

    sample = tickers[:sample_size]
    combos = _count_combos(grid)

    print(f"\n{'='*60}")
    print(f"  PHASE 2a  —  PARAMETER OPTIMISATION")
    print(f"  Strategy  : {strategy_name}")
    print(f"  Sample    : {len(sample)} of {len(tickers)} tickers")
    for k, v in grid.items():
        print(f"  {k:<25}: {list(v)}")
    print(f"  Combos    : {combos:,} per ticker  ({combos * len(sample):,} total evals)")
    print(f"  Objective : Maximise Sharpe Ratio")
    print(f"{'='*60}\n")

    ticker_results: List[Dict] = []
    # FIX BT1 — used to be a bare List[Dict] of successful results, then
    # zipped back against sample[:len(ticker_results)] below to build the
    # per_ticker report. That silently misattributes every entry once any
    # ticker in the middle of `sample` fails or is skipped (< 100 rows):
    # sample[:N] takes the first N *tickers*, not the N that actually
    # succeeded, so a failed ticker B before a successful C shifts every
    # subsequent (ticker, params) pairing by one. Track the actual ticker
    # alongside its result so the mapping can't drift.
    succeeded_tickers: List[str] = []

    for ticker in sample:
        print(f"  Optimising {ticker}...", end="  ", flush=True)
        try:
            df = fetch_single(ticker, period=period)
            df = add_all_indicators(df)
            df.dropna(inplace=True)

            if len(df) < 100:
                print("SKIP (< 100 rows after indicator warm-up)")
                continue

            bt = Backtest(df, strategy_cls, cash=cash,
                          commission=COMMISSION, exclusive_orders=True, **BT_KWARGS)

            opt_kwargs: Dict = {k: v for k, v in grid.items()}
            opt_kwargs["maximize"]       = "Sharpe Ratio"
            opt_kwargs["return_heatmap"] = False
            if constraint:
                opt_kwargs["constraint"] = constraint

            stats = bt.optimize(**opt_kwargs)

            # Extract the winning parameters from the optimised strategy instance
            best: Dict = {}
            for param in grid:
                try:
                    best[param] = getattr(stats._strategy, param)
                except AttributeError:
                    pass

            if best:
                ticker_results.append(best)
                succeeded_tickers.append(ticker)
                sh  = stats.get("Sharpe Ratio", float("nan"))
                ret = stats.get("Return [%]",   float("nan"))
                print(
                    f"done  Sharpe={sh:.2f}  Return={ret:+.1f}%  best={best}"
                    if not np.isnan(sh) else f"done  best={best}"
                )
            else:
                print("done  (could not extract params from stats._strategy)")

        except Exception as e:
            print(f"ERROR — {e}")

    if not ticker_results:
        print("\n  Optimisation produced no results — strategy defaults will be used.\n")
        return {}

    # ── Build consensus ────────────────────────────────────────────────────────
    df_r = pd.DataFrame(ticker_results)
    consensus: Dict = {}
    for col in df_r.columns:
        vals = df_r[col].dropna()
        if all(isinstance(v, (int, np.integer)) for v in vals):
            consensus[col] = int(vals.mode()[0])
        else:
            consensus[col] = round(float(vals.mean()), 2)

    print(f"\n  ── Consensus Best Parameters ──────────────────────────────")
    for k, v in consensus.items():
        print(f"    {k:<25}  {v}")

    # ── Persist to JSON ────────────────────────────────────────────────────────
    payload = {
        "strategy":         strategy_name,
        "period":           period,
        "tickers_used":     len(ticker_results),
        "consensus_params": consensus,
        "per_ticker": [
            {"ticker": t, "params": p}
            for t, p in zip(succeeded_tickers, ticker_results)
        ],
    }
    with open(PARAMS_FILE, "w") as f:
        json.dump(payload, f, indent=2, default=str)

    print(f"\n  Saved  →  {PARAMS_FILE}\n")
    return consensus


# ── Holdout (walk-forward-lite) ────────────────────────────────────────────────
# --optimize --holdout FRAC: fit params on the first (1-FRAC) of each ticker's
# bars, then report the best params run on the last FRAC only. Indicators are
# computed on the FULL frame before slicing (they only look backwards, so no
# look-ahead) so the holdout's first bars aren't NaN warm-up.
DEFAULT_HOLDOUT = 0.3
MIN_HOLDOUT_BARS = 60
OOS_LABEL = "OUT-OF-SAMPLE (holdout window - params never saw these bars)"
IS_LABEL = "IN-SAMPLE (fit window)"


def split_holdout(df: pd.DataFrame, frac: float):
    """Split a time-ordered frame into (fit, test); test is the last `frac` of bars."""
    if not 0 < frac < 1:
        raise ValueError(f"holdout fraction must be in (0, 1), got {frac}")
    n_test = int(round(len(df) * frac))
    cut = len(df) - n_test
    return df.iloc[:cut], df.iloc[cut:]


def prepare_frame(ticker: str, period: str) -> pd.DataFrame:
    """One fetch per ticker; indicators on the full frame; OHLCV-complete rows."""
    df = add_all_indicators(fetch_single(ticker, period=period))
    return df.dropna(subset=["Open", "High", "Low", "Close", "Volume"])


def _summ(stats) -> Dict:
    return {
        "Return (%)": round(float(stats["Return [%]"]), 2),
        "Sharpe": round(float(stats["Sharpe Ratio"]), 2),
        "Max DD (%)": round(float(stats["Max. Drawdown [%]"]), 2),
        "Win Rate (%)": round(float(stats["Win Rate [%]"]), 2),
        "# Trades": int(stats["# Trades"]),
    }


def _range(df: pd.DataFrame) -> str:
    def fmt(x):
        return x.strftime("%Y-%m-%d") if hasattr(x, "strftime") else str(x)
    return f"{fmt(df.index[0])} -> {fmt(df.index[-1])}" if len(df) else "-"


def fit_and_test(df: pd.DataFrame, strategy_cls: Type, grid: Dict, frac: float,
                 cash: float = 1_000_000, constraint=None) -> Dict:
    """Optimise `grid` on the fit window of `df`, run the best params on the holdout.

    Returns {best, fit_range, test_range, n_fit, n_test, in_sample, out_of_sample,
    skipped}; out_of_sample is None (and `skipped` says why) when the holdout
    has fewer than MIN_HOLDOUT_BARS bars.
    """
    # backtesting's optimiser copies columns into shared memory, which fails on
    # pandas string dtypes ('StringDtype' has no .base) — keep numeric columns.
    df = df.select_dtypes(include="number")
    fit_df, test_df = split_holdout(df, frac)
    out = {"best": {}, "fit_range": _range(fit_df), "test_range": _range(test_df),
           "n_fit": len(fit_df), "n_test": len(test_df),
           "in_sample": None, "out_of_sample": None, "skipped": None}
    kw: Dict = dict(cash=cash, commission=COMMISSION, exclusive_orders=True, **BT_KWARGS)
    opt_kwargs: Dict = dict(grid)
    opt_kwargs["maximize"] = "Sharpe Ratio"
    if constraint:
        opt_kwargs["constraint"] = constraint
    is_stats = Backtest(fit_df, strategy_cls, **kw).optimize(**opt_kwargs)
    out["best"] = {p: getattr(getattr(is_stats._strategy, p), "item",
                              lambda v=getattr(is_stats._strategy, p): v)()
                   for p in grid if hasattr(is_stats._strategy, p)}
    out["in_sample"] = _summ(is_stats)
    if len(test_df) < MIN_HOLDOUT_BARS:
        out["skipped"] = (f"holdout has only {len(test_df)} bars (< {MIN_HOLDOUT_BARS}) "
                          "- out-of-sample skipped")
        return out
    oos_stats = Backtest(test_df, strategy_cls, **kw).run(**out["best"])
    out["out_of_sample"] = _summ(oos_stats)
    return out


def format_holdout_report(ticker: str, res: Dict) -> str:
    """Side-by-side IN-SAMPLE / OUT-OF-SAMPLE block for one ticker."""
    lines = [f"\n  {ticker}   best params (fit window): {res['best']}",
             f"  {IN_SAMPLE_WARNING}",
             f"    {'Metric':<14}{IS_LABEL:>34}   {'OUT-OF-SAMPLE (holdout)':>30}",
             f"    {'Window':<14}{res['fit_range']:>34}   {res['test_range']:>30}",
             f"    {'Bars':<14}{res['n_fit']:>34}   {res['n_test']:>30}"]
    oos = res["out_of_sample"] or {}
    for k, v in (res["in_sample"] or {}).items():
        lines.append(f"    {k:<14}{v:>34}   {oos.get(k, 'n/a'):>30}")
    if res["skipped"]:
        lines.append(f"    NOTE: {res['skipped']}")
    else:
        lines.append(f"    Reported result = {OOS_LABEL}")
    return "\n".join(lines)


def run_holdout_backtest(tickers: List[str], strategy_cls: Type, period: str = "2y",
                         cash: float = 1_000_000, holdout: float = DEFAULT_HOLDOUT,
                         grid: Optional[Dict] = None, constraint=None) -> Dict[str, Dict]:
    """Per-ticker fit-then-holdout run; prints and returns {ticker: fit_and_test result}."""
    name = strategy_cls.__name__
    grid = grid if grid is not None else PARAM_GRIDS.get(name)
    if constraint is None:
        constraint = CONSTRAINTS.get(name)
    if not grid:
        print(f"  [optimizer] No parameter grid defined for {name}.")
        return {}
    print(f"\n{'='*60}\n  OPTIMISE + HOLDOUT  —  fit on first {1 - holdout:.0%}, "
          f"report last {holdout:.0%}\n{'='*60}")
    results: Dict[str, Dict] = {}
    for t in tickers:
        try:
            res = fit_and_test(prepare_frame(t, period), strategy_cls, grid, holdout,
                               cash=cash, constraint=constraint)
            results[t] = res
            print(format_holdout_report(t, res))
        except Exception as e:  # one bad ticker must not abort the batch
            print(f"\n  {t}: ERROR — {e}")
    return results
