# -*- coding: utf-8 -*-
"""
Indian Share Market Trading Model
CLI entry point — backtests, ML prediction, sector rotation.

Usage examples
──────────────
# Basic backtest
python main.py --mode backtest --strategy rsi_macd --tickers RELIANCE.NS TCS.NS INFY.NS

# Full NIFTY 50 standard backtest
python main.py --mode backtest --strategy rsi_macd --index nifty50

# Phase 2a: Optimise parameters first, then backtest
python main.py --mode backtest --strategy rsi_macd --tickers RELIANCE.NS TCS.NS --optimize

# Phase 2b: Equal-weight portfolio backtest
python main.py --mode backtest --strategy rsi_macd --index nifty50 --portfolio

# Phase 3a: Sector rotation — score sectors, pick top 3, run portfolio
python main.py --mode sector --strategy rsi_macd --n-sectors 3

# Score a single stock (composite 0–90 with plain-English narrative)
python main.py --mode score --tickers RELIANCE.NS

# Score all NIFTY100 stocks
python main.py --mode score --index nifty100

# Portfolio health check (loads CSV, scores every holding)
python main.py --mode portfolio --portfolio-csv portfolio.csv

# 4-screen stock screener (watchlist builder)
python main.py --mode screen --index nifty200

# Update trailing stops on open paper trades
python main.py --mode trail
"""

import sys, os
# Force UTF-8 output on Windows (prevents Unicode print errors)
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    os.environ.setdefault("PYTHONUTF8", "1")

import argparse
from data.fetcher import NIFTY50_TICKERS
from strategies.rsi_macd import RSIMACDStrategy
from strategies.momentum import MomentumStrategy

STRATEGIES = {
    "rsi_macd": RSIMACDStrategy,
    "momentum": MomentumStrategy,
}


def parse_args():
    p = argparse.ArgumentParser(
        description="Indian Share Market Algorithmic Trading Model",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Core arguments
    # FIX DEAD-LSTM — "lstm" mode removed. The dependency (torch >= 2.0) was
    # never actually installed by requirements.txt (see the commented line
    # there), so the mode's only real behaviour was to crash on the import.
    # LSTMs on daily OHLCV are a well-documented poor fit — daily samples are
    # too few and too noisy to train a recurrent network usefully — and the
    # module carried no offline artefacts or research indicating this one
    # was any different. Removed the mode, the flag, the branch, and
    # models/lstm.py itself. Nothing else in the app imported it.
    p.add_argument("--mode",      choices=["backtest", "ml", "sector",
                                           "scan", "screen", "paper", "trail",
                                           "score", "portfolio", "dashboard"],
                   default="backtest", help="Run mode")
    p.add_argument("--strategy",  choices=list(STRATEGIES.keys()),
                   default="rsi_macd", help="Trading strategy")
    p.add_argument("--tickers",   nargs="+",
                   default=["RELIANCE.NS", "TCS.NS", "INFY.NS"],
                   help="NSE tickers (space-separated, .NS suffix)")
    p.add_argument("--index",     choices=["nifty50", "nifty100", "nifty200", "nifty500"],
                   default=None,
                   help="Use a pre-defined universe instead of --tickers")
    p.add_argument("--period",    default="1y",
                   help="Data period: 1y | 2y | 3y | 5y | max")
    p.add_argument("--cash",      type=float, default=1_000_000,
                   help="Starting capital in INR (default 10 lakh)")
    p.add_argument("--portfolio-csv", default="portfolio.csv",
                   help="[portfolio mode] path to portfolio CSV file")

    # Phase 2 flags
    p.add_argument("--optimize",  action="store_true",
                   help="[Phase 2a] Grid-search best strategy parameters first")
    p.add_argument("--portfolio", action="store_true",
                   help="[Phase 2b] Equal-weight allocation across all tickers")

    # Phase 3 flags
    p.add_argument("--n-sectors", type=int, default=3,
                   help="[Phase 3a] Number of top sectors to invest in (default 3)")

    return p.parse_args()


def print_header(args, tickers):
    print(f"\n{'='*60}")
    print(f"  Indian Share Market Trading Model")
    print(f"  Mode      : {args.mode}")
    if args.mode != "sector":   # FIX DEAD-LSTM: mode "lstm" removed
        print(f"  Strategy  : {args.strategy}")
    if tickers:
        print(f"  Tickers   : {', '.join(tickers[:5])}{'  …' if len(tickers) > 5 else ''}")
        if len(tickers) > 5:
            print(f"              ({len(tickers)} total)")
    print(f"  Period    : {args.period}")
    print(f"  Capital   : Rs {args.cash:,.0f}")
    flags = []
    if args.optimize:                  flags.append("Phase 2a OPTIMISE")
    if args.portfolio:                 flags.append("Phase 2b PORTFOLIO")
    if args.mode == "sector":          flags.append(f"Phase 3a SECTOR-ROTATE (top {args.n_sectors})")
    # (Phase 3b LSTM mode removed — see FIX DEAD-LSTM above)
    if flags:
        print(f"  Active    : {' | '.join(flags)}")
    print(f"{'='*60}")


def _resolve_universe(args) -> list:
    """Return ticker list from --index or --tickers."""
    if args.index:
        from data.universe import get_universe
        return get_universe(args.index)
    return args.tickers


def main():
    args         = parse_args()
    tickers      = _resolve_universe(args)
    strategy_cls = STRATEGIES[args.strategy]

    print_header(args, tickers)

    # ── BACKTEST MODE ──────────────────────────────────────────────────────────
    if args.mode == "backtest":

        best_params = {}

        # Phase 2a  —  Parameter Optimisation
        if args.optimize:
            from backtest.optimizer import optimize_strategy
            best_params = optimize_strategy(
                tickers      = tickers,
                strategy_cls = strategy_cls,
                period       = args.period,
                cash         = args.cash,
            )
            from backtest.optimizer import in_sample_banner
            print(in_sample_banner(bool(best_params)))

        # Phase 2b  —  Portfolio backtest (equal-weight)
        if args.portfolio:
            from backtest.portfolio import run_portfolio_backtest
            run_portfolio_backtest(
                tickers         = tickers,
                strategy_cls    = strategy_cls,
                period          = args.period,
                total_cash      = args.cash,
                strategy_params = best_params or None,
            )

        else:
            # Standard per-ticker backtest (Phase 1 / baseline)
            from backtest.runner import run_backtest
            run_backtest(
                tickers         = tickers,
                strategy_cls    = strategy_cls,
                period          = args.period,
                cash            = args.cash,
                strategy_params = best_params or None,
            )

    # ── ML MODE (XGBoost baseline) ────────────────────────────────────────────
    elif args.mode == "ml":
        from models.predictor import train_and_evaluate
        train_and_evaluate(tickers=tickers, period=args.period)

    # ── SECTOR MODE — Phase 3a ────────────────────────────────────────────────
    elif args.mode == "sector":
        from strategies.sector_rotation import SECTORS
        from backtest.sector_runner import run_sector_rotation_backtest

        best_params = {}
        if args.optimize:
            from backtest.optimizer import optimize_strategy
            # Use first 5 tickers from Banking + IT as optimisation sample
            sample = (SECTORS["Banking"] + SECTORS["IT"])[:5]
            best_params = optimize_strategy(
                tickers      = sample,
                strategy_cls = strategy_cls,
                period       = args.period,
                cash         = args.cash,
            )
            from backtest.optimizer import in_sample_banner
            print(in_sample_banner(bool(best_params)))

        run_sector_rotation_backtest(
            strategy_cls    = strategy_cls,
            period          = args.period,
            total_cash      = args.cash,
            n_sectors       = args.n_sectors,
            strategy_params = best_params or None,
        )

    # (LSTM mode removed — see FIX DEAD-LSTM at the argparse block above)

    # ── SCAN MODE — Phase 4a: signal scanner + paper trade execution ─────────
    elif args.mode == "scan":
        from trading.signals import scan_tickers
        from trading.paper_trader import PaperTrader

        # "all" strategy = 4-screen approach (oversold/momentum/breakout/pullback)
        # otherwise respect --strategy flag for backward compat
        scan_strategy = "all" if args.strategy == "rsi_macd" else args.strategy
        signals = scan_tickers(
            tickers  = tickers,
            strategy = scan_strategy,
            period   = args.period,
        )

        trader = PaperTrader(initial_capital=args.cash)
        if signals:
            buy_sigs = [s for s in signals if s["action"] == "BUY"]
            print(f"\n  Executing {len(buy_sigs)} BUY signal(s) as paper trades…")
            for sig in buy_sigs:
                trader.execute_signal(sig)
            trader.print_summary()
            trader.log_portfolio()
            trader.export_journal_csv()
        else:
            print("\n  No signals to execute.")
        trader.performance_summary()

    # ── SCREEN MODE — 4-screen stock screener (watchlist builder) ────────────
    elif args.mode == "screen":
        from trading.signals import scan_tickers

        universe = tickers   # already resolved by _resolve_universe()
        print(f"\n  Running 4-screen stock screener on {len(universe)} tickers…")
        signals = scan_tickers(
            tickers  = universe,
            strategy = "all",
            period   = args.period,
        )
        if signals:
            print(f"\n  ── WATCHLIST ({len(signals)} setups) ───────────────────")
            for s in signals:
                _tp = s.get("tp")
                print(f"  {s['ticker']:<22}  [{s.get('screen',''):<20}]  "
                      f"Rs.{s['price']:,.2f}  SL:Rs.{s.get('sl',0):,.2f}  "
                      f"{'TP:Rs.' + str(_tp) if _tp is not None else 'TP:trail'}")

    # ── TRAIL MODE — update trailing stops on open paper trades ──────────────
    elif args.mode == "trail":
        from trading.paper_trader import PaperTrader
        trader = PaperTrader(initial_capital=args.cash)
        trader.update_trailing_stops()
        trader.print_summary()
        trader.export_journal_csv()

    # ── PAPER MODE — Phase 4: view paper trading log + performance ────────────
    elif args.mode == "paper":
        from trading.paper_trader import PaperTrader
        trader = PaperTrader(initial_capital=args.cash)
        trader.print_trade_history()
        trader.performance_summary()
        trader.export_journal_csv()

    # ── SCORE MODE — composite score for one or many stocks ──────────────────
    elif args.mode == "score":
        from analysis.score import score_stock
        from trading.signals import get_india_vix_regime
        vix_info = get_india_vix_regime()
        _vix_val = vix_info.get("vix")
        _vix_str = f"{_vix_val:.2f}" if _vix_val is not None else "N/A"
        print(f"\n  India VIX: {_vix_str}  [{vix_info.get('regime', '?')}]")
        print(f"  Scoring {len(tickers)} stock(s)…\n")
        results = []
        for t in tickers:
            try:
                cs = score_stock(t, period=args.period, vix_info=vix_info)
                results.append(cs)
                flag = {"STRONG BUY": "🟢🟢", "BUY": "🟢", "WATCHLIST": "🟡",
                        "HOLD": "🟡", "CAUTION": "🔴", "EXIT": "🔴🔴"}.get(cs.action, "")
                print(f"  {cs.ticker.replace('.NS',''):<14} ₹{cs.price:>8,.1f}  "
                      f"Score:{cs.score:>5.1f}/100 [{cs.grade}]  "
                      f"{flag} {cs.action:<12}  {cs.headline}")
            except Exception as e:
                print(f"  {t:<14} ERROR: {e}")
        print(f"\n  Scored {len(results)}/{len(tickers)} stocks successfully.")

    # ── PORTFOLIO MODE — load CSV, score every holding ────────────────────────
    elif args.mode == "portfolio":
        from pathlib import Path
        csv_file = args.portfolio_csv
        if not Path(csv_file).exists():
            # Create sample portfolio.csv if missing
            sample = (
                "ticker,quantity,avg_buy_price,date_bought\n"
                "RELIANCE,10,1350.00,2024-01-15\n"
                "TCS,5,3800.00,2024-03-10\n"
                "HDFCBANK,20,1600.00,2024-02-01\n"
                "INFY,15,1500.00,2024-01-20\n"
                "ICICIBANK,25,900.00,2024-04-05\n"
            )
            with open(csv_file, "w") as f:
                f.write(sample)
            print(f"\n  Created sample portfolio at '{csv_file}' — edit it then rerun.")
        else:
            from analysis.portfolio_manager import PortfolioManager
            pm = PortfolioManager(csv_file)
            summary = pm.mark_to_market()
            pm.print_summary(summary)
            pm.export_summary_csv(summary)

    # ── DASHBOARD MODE — Phase 5: launch Streamlit ────────────────────────────
    elif args.mode == "dashboard":
        import subprocess, sys
        dashboard_path = str(
            __import__("pathlib").Path(__file__).parent / "dashboard" / "app.py"
        )
        print(f"\n  Launching Streamlit dashboard…")
        print(f"  Open your browser at  http://localhost:8501\n")
        subprocess.run([sys.executable, "-m", "streamlit", "run", dashboard_path])


if __name__ == "__main__":
    main()
