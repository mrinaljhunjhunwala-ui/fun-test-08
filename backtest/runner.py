"""
backtest/runner.py
Runs backtests across multiple tickers and generates a consolidated report.
Uses backtesting.py for execution, plus STT/brokerage cost modeling.

Optimizations applied:
  - Parallelizes per-ticker backtest loop with ThreadPoolExecutor (8 workers)
  - Preserves result ordering (collected by ticker, reassembled in input order)
  - Per-ticker error handling: one failure doesn't abort the batch
  - All failures logged at WARNING level (no silent drops)
  - Chart generation restored: plots last-input ticker's results (if plot=True)
"""

import pandas as pd
import numpy as np
from backtesting import Backtest
from typing import Type, List, Dict, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
import logging

from data.fetcher import fetch_single
from utils.indicators import add_all_indicators

_log = logging.getLogger("backtest.runner")

# ── Indian market costs (trading/costs.py) ──────────────────────────────────
# FIX COST-MODEL (audit 2026-10-03 #23): the old constants charged STT on the
# sell side only (delivery STT is 0.1% on BOTH legs) and left out stamp duty
# and DP charges. Every fill is now priced by trading.costs: the exact
# per-fill callable where backtesting.py supports one, else a flat per-side
# rate at a representative ₹2L ticket. backtesting.py charges `commission`
# on every fill (FIX BT-COMMISSION), so either form is per side.
from trading import costs as _costs  # noqa: E402

COMMISSION     = _costs.backtest_commission_arg()
TOTAL_COST     = _costs.representative_round_trip_pct()      # disclosure figure
PER_SIDE_COST  = TOTAL_COST / 2


def run_backtest(
    tickers: List[str],
    strategy_cls: Type,
    period: str = "2y",
    cash: float = 1_000_000,
    commission=COMMISSION,
    plot: bool = True,
    optimize: bool = False,
    strategy_params: dict = None,
) -> pd.DataFrame:
    """
    Run backtest for each ticker and print a consolidated performance table.
    Uses parallel execution (ThreadPoolExecutor, 8 workers) for per-ticker loops.
    One ticker's failure does not prevent other results from being collected.
    Chart generation: if plot=True, generates HTML chart for the last-input ticker (if successful).

    Args:
        tickers:         List of NSE/BSE ticker symbols
        strategy_cls:    Strategy class (from strategies/)
        period:          yfinance period string
        cash:            Starting capital in INR
        commission:      per-fill charges: trading.costs callable or a per-side rate
                         (default PER_SIDE_COST = half the Indian round trip)
        plot:            Show backtesting.py chart for the last ticker (default True)
        optimize:        Run parameter optimisation (slow — only for single ticker)
        strategy_params: Dict of parameter overrides from Phase 2a optimiser

    Returns:
        DataFrame with per-ticker performance metrics (input ticker order preserved)
    """
    results: Dict[str, Dict] = {}
    last_bt: Optional[Backtest] = None  # Backtesting instance for the last-input ticker (for plotting)
    _MAX_WORKERS = 8
    _TICKER_TIMEOUT = 60  # per-ticker wall-clock timeout in backtest execution
    
    def _backtest_one(ticker: str) -> Tuple[str, Optional[Dict], Optional[Backtest]]:
        """
        Run a single ticker's backtest.
        Returns (ticker, result_dict or None on failure, bt_instance_or_none).
        bt_instance is only populated for tickers[-1] (the last-input ticker);
        None for all others to avoid holding unnecessary Backtest objects in memory.
        """
        try:
            print(f"  Backtesting {ticker}...", end=" ", flush=True)
            df = fetch_single(ticker, period=period)
            df = add_all_indicators(df)
            # Only require OHLCV present — dropping rows with ANY indicator NaN can punch
            # mid-series gaps and distort bar timing. OHLCV is never NaN so bars stay
            # contiguous; the strategy handles its own indicator warm-up.
            df = df.dropna(subset=["Open", "High", "Low", "Close", "Volume"])

            if len(df) < 60:
                print(f"SKIP (only {len(df)} rows after indicator warmup)")
                return ticker, None, None

            bt = Backtest(
                df,
                strategy_cls,
                cash=cash,
                commission=commission,
                exclusive_orders=True,
            )

            if optimize and len(tickers) == 1:
                stats = bt.optimize(
                    rsi_oversold=range(25, 45, 5),
                    rsi_overbought=range(55, 75, 5),
                    maximize="Sharpe Ratio",
                    constraint=lambda p: p.rsi_oversold < p.rsi_overbought,
                )
            else:
                stats = bt.run(**(strategy_params or {}))

            result = {
                "Ticker":          ticker,
                "Return (%)":      round(stats["Return [%]"], 2),
                "Buy & Hold (%)":  round(stats["Buy & Hold Return [%]"], 2),
                "Sharpe":          round(stats["Sharpe Ratio"], 2),
                "Max Drawdown (%)":round(stats["Max. Drawdown [%]"], 2),
                "Win Rate (%)":    round(stats["Win Rate [%]"], 2),
                "# Trades":        int(stats["# Trades"]),
                "Profit Factor":   round(stats.get("Profit Factor", 0), 2),
            }
            print(f"✓  Return: {stats['Return [%]']:.1f}%  Sharpe: {stats['Sharpe Ratio']:.2f}")
            
            # Retain Backtest instance only for the last-input ticker (for plotting)
            bt_to_return = bt if ticker == tickers[-1] else None
            return ticker, result, bt_to_return

        except Exception as e:
            _log.warning("backtest failed for ticker=%s: %s: %s",
                        ticker, type(e).__name__, e)
            print(f"ERROR — {e}")
            return ticker, None, None

    print(f"  Backtesting {len(tickers)} ticker(s) in parallel ({_MAX_WORKERS} workers)...")

    # Parallelize backtest execution per ticker (not within a single ticker)
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        futs = {pool.submit(_backtest_one, t): t for t in tickers}
        for fut in as_completed(futs, timeout=_TICKER_TIMEOUT * len(tickers)):
            try:
                ticker, result, bt = fut.result(timeout=_TICKER_TIMEOUT)
                if result is not None:
                    results[ticker] = result
                # Retain Backtest instance for the last-input ticker (for plotting after loop)
                if bt is not None:
                    last_bt = bt
            except Exception as e:
                ticker_name = futs.get(fut, "unknown")
                _log.warning("backtest future failed for ticker=%s: %s: %s",
                            ticker_name, type(e).__name__, e)

    if not results:
        print("\n  No results to display.")
        return pd.DataFrame()

    # Reassemble results in input ticker order (preserve user's ordering)
    ordered_results = []
    for ticker in tickers:
        if ticker in results:
            ordered_results.append(results[ticker])

    report = pd.DataFrame(ordered_results).set_index("Ticker")

    # ── Summary table ────────────────────────────────────────────────────────
    print(f"\n{'─'*80}")
    print("  BACKTEST SUMMARY")
    print(f"{'─'*80}")
    print(report.to_string())
    print(f"{'─'*80}")
    print(f"\n  Average Return:   {report['Return (%)'].mean():.2f}%")
    print(f"  Average Sharpe:   {report['Sharpe'].mean():.2f}")
    print(f"  Average Drawdown: {report['Max Drawdown (%)'].mean():.2f}%")
    print(f"  Average Win Rate: {report['Win Rate (%)'].mean():.2f}%")
    print(f"  Total Tickers:    {len(report)}")

    # ── Chart generation for last-input ticker (if plot=True and backtest succeeded) ──
    if plot and last_bt is not None:
        try:
            last_ticker = tickers[-1]
            filename = f"backtest_{last_ticker.replace('.', '_')}.html"
            last_bt.plot(filename=filename, open_browser=False)
            print(f"\n  📊 Chart saved: {filename}")
        except Exception as e:
            _log.warning("plot generation failed for last ticker %s: %s: %s",
                        tickers[-1], type(e).__name__, e)
            print(f"\n  ⚠️  Chart generation failed for {tickers[-1]}: {e}")
    elif plot and last_bt is None:
        _log.warning("plot requested but last-input ticker %s failed or was skipped",
                    tickers[-1])
        print(f"\n  ⚠️  No chart generated — last ticker ({tickers[-1]}) backtest did not complete successfully")

    # ── Save to CSV ──────────────────────────────────────────────────────────
    out_path = "backtest_results.csv"
    report.to_csv(out_path)
    print(f"\n  Results saved to: {out_path}")

    return report
