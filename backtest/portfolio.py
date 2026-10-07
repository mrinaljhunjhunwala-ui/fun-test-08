"""
backtest/portfolio.py — Phase 2b
Equal-weight portfolio backtesting across multiple NSE tickers.

Capital = total_cash / n_tickers per position.
Each ticker runs independently; results are aggregated into
portfolio-level metrics: total return, alpha vs. buy-and-hold,
average Sharpe, worst drawdown, trade statistics.
"""

import numpy as np
import pandas as pd
from backtesting import Backtest
from typing import Type, List, Dict, Optional

from data.fetcher import fetch_single
from utils.indicators import add_all_indicators

# Indian market costs — same model as backtest/runner.py (trading/costs.py).
from backtest.runner import BT_KWARGS, COMMISSION, PER_SIDE_COST, TOTAL_COST  # noqa: E402,F401
from backtest.runner import numeric_frame  # noqa: E402


def run_portfolio_backtest(
    tickers: List[str],
    strategy_cls: Type,
    period: str = "2y",
    total_cash: float = 1_000_000,
    strategy_params: Optional[Dict] = None,
    commission=COMMISSION,
    save_chart: bool = True,
) -> pd.DataFrame:
    """
    Equal-weight portfolio backtest across all supplied tickers.

    Args:
        tickers:         NSE/BSE ticker list
        strategy_cls:    Strategy class to run
        period:          Data period (yfinance string)
        total_cash:      Total capital in INR — split equally across tickers
        strategy_params: Optimised param dict from Phase 2a (None = defaults)
        commission:      per-fill charges: trading.costs callable or a per-side rate
        save_chart:      Save HTML chart for the best-Sharpe ticker

    Returns:
        DataFrame of per-ticker metrics, indexed by Ticker.
    """
    n = len(tickers)
    per_ticker_cash = total_cash / n
    params_label = f"optimised: {strategy_params}" if strategy_params else "default params"

    print(f"\n{'='*60}")
    print(f"  PHASE 2b  —  PORTFOLIO BACKTEST")
    print(f"  Strategy  : {strategy_cls.__name__}  ({params_label})")
    print(f"  Universe  : {n} tickers")
    print(f"  Total     : ₹{total_cash:>12,.0f}")
    print(f"  Per ticker: ₹{per_ticker_cash:>12,.0f}  ({100/n:.1f}% weight each)")
    print(f"{'='*60}\n")

    records: List[Dict] = []
    failed:  List[str]  = []
    best_sharpe = -np.inf
    best_bt_state = None   # (ticker, Backtest, stats) for chart

    for i, ticker in enumerate(tickers, 1):
        print(f"  [{i:>2}/{n}] {ticker:<20}", end="  ", flush=True)
        try:
            df = fetch_single(ticker, period=period)
            df = numeric_frame(add_all_indicators(df))
            df.dropna(inplace=True)

            if len(df) < 60:
                print("SKIP (< 60 rows after indicator warm-up)")
                failed.append(ticker)
                continue

            bt = Backtest(df, strategy_cls, cash=per_ticker_cash,
                          commission=commission, exclusive_orders=True, **BT_KWARGS)

            stats = bt.run(**(strategy_params or {}))

            ret  = stats["Return [%]"]
            bhr  = stats["Buy & Hold Return [%]"]
            sh   = stats["Sharpe Ratio"]
            dd   = stats["Max. Drawdown [%]"]
            wr   = stats["Win Rate [%]"]
            nt   = int(stats["# Trades"])
            pf   = stats.get("Profit Factor", float("nan"))
            final_val = per_ticker_cash * (1 + ret / 100)

            # Track best-Sharpe ticker for chart
            if not np.isnan(sh) and sh > best_sharpe:
                best_sharpe    = sh
                best_bt_state  = (ticker, bt)

            records.append({
                "Ticker":       ticker,
                "Weight (%)":   round(100 / n, 1),
                "Alloc (Rs)":   round(per_ticker_cash),
                "Final (Rs)":   round(final_val),
                "Return (%)":   round(ret, 2),
                "B&H (%)":      round(bhr, 2),
                "Alpha (%)":    round(ret - bhr, 2),
                "Sharpe":       round(sh, 2)  if not np.isnan(sh) else np.nan,
                "Max DD (%)":   round(dd, 2),
                "Win Rate (%)": round(wr, 2)  if not np.isnan(wr) else 0.0,
                "# Trades":     nt,
                "Profit Factor":round(pf, 2)  if not np.isnan(pf) else 0.0,
            })

            sh_str  = f"Sh={sh:.2f}" if not np.isnan(sh) else "Sh=N/A"
            print(f"done  Return={ret:+.1f}%  {sh_str}  Trades={nt}")

        except Exception as e:
            print(f"ERROR  —  {e}")
            failed.append(ticker)

    if not records:
        print("\n  No results — all tickers failed.")
        return pd.DataFrame()

    report = pd.DataFrame(records).set_index("Ticker")

    # ── Optional: save chart for best-Sharpe ticker ────────────────────────────
    if save_chart and best_bt_state:
        bticker, b_bt = best_bt_state
        fname = f"portfolio_best_{bticker.replace('.', '_')}.html"
        b_bt.plot(filename=fname, open_browser=False)
        print(f"\n  Chart saved  →  {fname}  (best Sharpe: {best_sharpe:.2f})")

    # ── Portfolio-level aggregation ────────────────────────────────────────────
    active      = report[report["# Trades"] > 0]
    total_alloc = per_ticker_cash * len(records)
    total_final = report["Final (Rs)"].sum()
    port_return = (total_final / total_alloc - 1) * 100
    port_bh     = report["B&H (%)"].mean()
    port_alpha  = port_return - port_bh
    # FIX BT2 — used to be report["Sharpe"].replace(0, np.nan).mean(), which
    # silently excluded any ticker whose Sharpe was stored as 0.0. But 0.0 was
    # being used to mean BOTH "no valid Sharpe" (NaN, e.g. zero trades) AND
    # any real Sharpe that rounds to 0.00 — so genuinely flat-but-valid
    # results got dropped from the average right alongside actual no-data
    # placeholders, silently inflating the reported average. "Sharpe" is now
    # stored as a real NaN for the placeholder case, so a plain .mean() (which
    # already skips NaN) is both correct and simpler.
    avg_sharpe  = report["Sharpe"].mean()
    worst_dd    = report["Max DD (%)"].min()
    avg_wr      = active["Win Rate (%)"].mean() if len(active) > 0 else 0.0
    total_trades= int(report["# Trades"].sum())
    n_win       = int((report["Return (%)"] > 0).sum())
    n_lose      = int((report["Return (%)"] <= 0).sum())
    n_active    = len(active)

    # ── Print summary table ────────────────────────────────────────────────────
    print(f"\n{'─'*72}")
    print("  PORTFOLIO SUMMARY — PER TICKER")
    print(f"{'─'*72}")
    disp_cols = ["Return (%)", "B&H (%)", "Alpha (%)", "Sharpe",
                 "Max DD (%)", "Win Rate (%)", "# Trades"]
    print(report[disp_cols].to_string())
    print(f"{'─'*72}")

    # ── Print portfolio box ────────────────────────────────────────────────────
    print(f"""
  ╔══════════════════════════════════════════════════════════╗
  ║           PORTFOLIO METRICS  —  PHASE 2b                 ║
  ╠══════════════════════════════════════════════════════════╣
  ║  Total Capital Deployed  : Rs {total_alloc:>12,.0f}           ║
  ║  Portfolio Value (end)   : Rs {total_final:>12,.0f}           ║
  ║  Portfolio Return        :    {port_return:>+9.2f}%              ║
  ║  Avg Buy & Hold Return   :    {port_bh:>+9.2f}%              ║
  ║  Alpha (strategy - B&H)  :    {port_alpha:>+9.2f}%              ║
  ║  Avg Sharpe Ratio        :    {avg_sharpe:>9.2f}               ║
  ║  Worst Single Drawdown   :    {worst_dd:>+9.2f}%              ║
  ║  Avg Win Rate (active)   :    {avg_wr:>9.2f}%              ║
  ║  Total Trades            :    {total_trades:>9,}               ║
  ║  Winners / Losers        :    {n_win:>4} / {n_lose:<4}                    ║
  ║  Active tickers          :    {n_active:>3} / {len(records):<3}  ({len(failed)} failed)       ║
  ╚══════════════════════════════════════════════════════════╝""")

    # ── Save CSV ───────────────────────────────────────────────────────────────
    out = "portfolio_results.csv"
    report.to_csv(out)
    print(f"\n  Results saved  →  {out}")
    if failed:
        print(f"  Failed tickers : {', '.join(failed)}")

    return report
