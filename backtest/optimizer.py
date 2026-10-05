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
from backtest.runner import COMMISSION  # noqa: E402

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
                          commission=COMMISSION, exclusive_orders=True)

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
