"""
trading/paper_trader.py — Phase 4
Paper trading engine with SQLite persistence.

Simulates order execution (no real money) for NSE equities using
delayed yfinance quotes. All trades are logged to trades.db (SQLite).

Workflow:
    1. scan_tickers() fires signals
    2. PaperTrader.execute_signal() records the "order" to SQLite
    3. PaperTrader.mark_to_market() updates open positions daily
    4. TelegramAlerter sends entry / exit alerts
"""

import sqlite3
import os
import csv
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional

import numpy as np
import pandas as pd

from utils.sql import read_sql_df
from utils.telegram import TelegramAlerter

DB_FILE    = "trades.db"
JOURNAL_CSV = str(Path.home() / "trading_journal.csv")  # trade-journal skill format

_CREATE_TRADES = """
CREATE TABLE IF NOT EXISTS trades (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker      TEXT    NOT NULL,
    strategy    TEXT    NOT NULL,
    action      TEXT    NOT NULL,          -- BUY or SELL
    price       REAL    NOT NULL,
    quantity    INTEGER NOT NULL,
    sl          REAL,
    tp          REAL,
    trail_stop  REAL,                      -- current trailing stop level
    capital     REAL,
    reason      TEXT,
    timestamp   TEXT    NOT NULL,
    status      TEXT    DEFAULT 'OPEN',    -- OPEN | CLOSED | STOPPED
    exit_price  REAL,
    exit_reason TEXT,
    exit_time   TEXT,
    pnl         REAL,
    pnl_pct     REAL
);
"""

_CREATE_PORTFOLIO = """
CREATE TABLE IF NOT EXISTS portfolio_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    date            TEXT    NOT NULL,
    total_capital   REAL,
    invested        REAL,
    cash            REAL,
    open_positions  INTEGER,
    total_pnl       REAL,
    logged_at       TEXT    NOT NULL
);
"""


class PaperTrader:
    """
    In-memory + SQLite paper trading engine.

    Args:
        initial_capital:   Starting cash in INR (default ₹10,00,000)
        risk_pct:          Fraction of cash to risk per trade (default 2%)
        db_path:           SQLite file path
        telegram_token:    Optional Telegram bot token
        telegram_chat_id:  Optional Telegram chat ID
    """

    def __init__(
        self,
        initial_capital:  float = 1_000_000,
        risk_pct:         float = 0.02,
        db_path:          str   = DB_FILE,
        telegram_token:   Optional[str] = None,
        telegram_chat_id: Optional[str] = None,
    ):
        self.capital   = initial_capital
        self.cash      = initial_capital
        self.risk_pct  = risk_pct
        self.db_path   = db_path
        self.positions: Dict[str, Dict] = {}   # ticker → open position

        self.alerter = TelegramAlerter(
            token   = telegram_token,
            chat_id = telegram_chat_id,
        )

        self._init_db()
        self._load_state()
        print(f"  [PaperTrader] Initialised  |  Capital=₹{initial_capital:,.0f}  |  DB={db_path}  "
              f"|  Cash=₹{self.cash:,.0f}  |  Open positions={len(self.positions)}")

    # ── Database setup ────────────────────────────────────────────────────────

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(_CREATE_TRADES)
            conn.execute(_CREATE_PORTFOLIO)
            conn.commit()

    def _load_state(self) -> None:
        """FIX PT1: reconstruct self.cash and self.positions from every trade
        ever recorded in trades.db, so state is consistent across separate
        process invocations rather than resetting on every run.

        Ledger logic: every trade (any status) deducted `price * quantity`
        from cash at BUY time. A CLOSED/STOPPED trade later added back
        `exit_price * quantity` when it was exited. OPEN trades are
        rebuilt into self.positions so the duplicate-buy guard and the
        SELL path both see positions opened in earlier runs.
        """
        try:
            with self._conn() as conn:
                rows = read_sql_df(
                    "SELECT ticker, price, quantity, sl, tp, strategy, status, exit_price "
                    "FROM trades",
                    conn,
                )
        except Exception as e:
            print(f"  [PaperTrader] _load_state: could not read trades.db ({e}) — starting fresh")
            return

        if rows.empty:
            return

        dup_open_tickers = set()
        for _, r in rows.iterrows():
            ticker   = str(r["ticker"])
            quantity = int(r["quantity"])
            cost     = float(r["price"]) * quantity
            self.cash -= cost

            if r["status"] in ("CLOSED", "STOPPED"):
                if pd.notna(r["exit_price"]):
                    self.cash += float(r["exit_price"]) * quantity
            elif r["status"] == "OPEN":
                if ticker in self.positions:
                    dup_open_tickers.add(ticker)  # pre-existing duplicate OPEN rows — keep latest
                self.positions[ticker] = {
                    "quantity":    quantity,
                    "entry_price": float(r["price"]),
                    "sl":          float(r["sl"]) if pd.notna(r["sl"]) else None,
                    "tp":          float(r["tp"]) if pd.notna(r["tp"]) else None,
                    "strategy":    r["strategy"],
                }

        if dup_open_tickers:
            print(f"  [PaperTrader] WARNING: multiple OPEN rows found for {sorted(dup_open_tickers)} "
                  f"— using the most recent row for in-memory position tracking; "
                  f"older duplicate rows remain OPEN in trades.db and should be reviewed manually.")

    def _conn(self):
        return sqlite3.connect(self.db_path)

    # ── Core operations ───────────────────────────────────────────────────────

    def execute_signal(self, signal: Dict) -> Optional[int]:
        """
        Execute a signal dict (from trading/signals.py scan).
        Returns the trade ID (int) if opened, or None if skipped.
        """
        ticker   = signal["ticker"]
        action   = signal["action"].upper()
        price    = signal["price"]
        sl       = signal.get("sl")
        tp       = signal.get("tp")
        strategy = signal.get("strategy", "")
        reason   = signal.get("reason",   "")
        now      = datetime.now().isoformat()

        # ── BUY ───────────────────────────────────────────────────────────────
        if action == "BUY":
            if ticker in self.positions:
                print(f"  [PaperTrader] {ticker} already open — skipping duplicate BUY")
                return None

            # Position sizing: risk_pct of cash ÷ risk per share
            risk_per_share = price - sl if sl else price * 0.03
            if risk_per_share <= 0:
                print(f"  [PaperTrader] {ticker}: invalid risk ({risk_per_share:.2f}) — skip")
                return None

            risk_amount = self.cash * self.risk_pct
            quantity    = max(1, int(risk_amount / risk_per_share))
            cost        = quantity * price

            if cost > self.cash:
                quantity = max(1, int(self.cash * 0.95 / price))
                cost     = quantity * price

            if cost > self.cash:
                print(f"  [PaperTrader] {ticker}: insufficient cash (need ₹{cost:,.0f}, have ₹{self.cash:,.0f})")
                return None

            self.cash -= cost
            self.positions[ticker] = {
                "quantity": quantity, "entry_price": price,
                "sl": sl, "tp": tp, "strategy": strategy,
            }

            with self._conn() as conn:
                cur = conn.execute(
                    "INSERT INTO trades (ticker,strategy,action,price,quantity,sl,tp,capital,reason,timestamp) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (ticker, strategy, "BUY", price, quantity, sl, tp, cost, reason, now)
                )
                trade_id = cur.lastrowid
                conn.commit()

            print(f"  [PaperTrader] 🟢 BUY  {ticker}  qty={quantity}  ₹{price:.2f}  cost=₹{cost:,.0f}")
            self.alerter.send_signal(ticker, "BUY", price, sl=sl, tp=tp, strategy=strategy, reason=reason)
            return trade_id

        # ── SELL ──────────────────────────────────────────────────────────────
        elif action == "SELL":
            if ticker not in self.positions:
                return None

            pos        = self.positions.pop(ticker)
            quantity   = pos["quantity"]
            entry_p    = pos["entry_price"]
            proceeds   = quantity * price
            pnl        = proceeds - quantity * entry_p
            pnl_pct    = (price / entry_p - 1) * 100
            self.cash += proceeds

            with self._conn() as conn:
                conn.execute(
                    "UPDATE trades SET status='CLOSED', exit_price=?, exit_time=?, pnl=?, pnl_pct=? "
                    "WHERE ticker=? AND status='OPEN' ORDER BY id DESC LIMIT 1",
                    (price, now, pnl, pnl_pct, ticker)
                )
                conn.commit()

            icon = "💚" if pnl >= 0 else "❤️"
            print(
                f"  [PaperTrader] {icon} SELL {ticker}  qty={quantity}  "
                f"₹{price:.2f}  P&L={pnl:+,.0f} ({pnl_pct:+.2f}%)"
            )
            self.alerter.send_signal(ticker, "SELL", price, strategy=strategy, reason=reason)
            return None

        return None

    # ── Portfolio summary ─────────────────────────────────────────────────────

    def get_portfolio_summary(self) -> Dict:
        """Return current portfolio snapshot."""
        invested = sum(
            pos["quantity"] * pos["entry_price"]
            for pos in self.positions.values()
        )
        return {
            "date":            datetime.now().strftime("%d %b %Y"),
            "total_capital":   self.capital,
            "cash":            self.cash,
            "invested":        invested,
            "open_positions":  len(self.positions),
            "open_tickers":    list(self.positions.keys()),
        }

    def log_portfolio(self):
        """Log current portfolio state to SQLite."""
        s   = self.get_portfolio_summary()
        now = datetime.now().isoformat()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO portfolio_log (date,total_capital,invested,cash,open_positions,total_pnl,logged_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (s["date"], s["total_capital"], s["invested"],
                 s["cash"], s["open_positions"], 0, now)
            )
            conn.commit()

    def print_summary(self):
        """Print current portfolio to console."""
        s = self.get_portfolio_summary()
        print(f"\n  ┌─ PAPER PORTFOLIO ───────────────────────────────────┐")
        print(f"  │  Date          : {s['date']:<34}│")
        print(f"  │  Total Capital : ₹{s['total_capital']:>12,.0f}                    │")
        print(f"  │  Cash          : ₹{s['cash']:>12,.0f}                    │")
        print(f"  │  Invested      : ₹{s['invested']:>12,.0f}                    │")
        print(f"  │  Open Positions: {s['open_positions']:>3}  {', '.join(s['open_tickers'][:4]):<27}│")
        print(f"  └─────────────────────────────────────────────────────┘")

    # ── Trade history ─────────────────────────────────────────────────────────

    def get_trade_history(self) -> pd.DataFrame:
        """Return all trades from SQLite as DataFrame."""
        with self._conn() as conn:
            df = read_sql_df("SELECT * FROM trades ORDER BY id", conn)
        return df

    def print_trade_history(self):
        df = self.get_trade_history()
        if df.empty:
            print("  No trades recorded yet.")
            return
        closed = df[df["status"] == "CLOSED"]
        total_pnl = closed["pnl"].sum() if not closed.empty else 0
        wins      = (closed["pnl"] > 0).sum() if not closed.empty else 0
        print(f"\n  Trade History: {len(df)} total  |  {len(closed)} closed  |  "
              f"Win rate: {wins/max(len(closed),1)*100:.0f}%  |  Total P&L: Rs.{total_pnl:+,.0f}")
        cols = ["ticker", "action", "price", "quantity", "sl", "tp", "trail_stop",
                "exit_price", "exit_reason", "pnl", "pnl_pct", "status", "timestamp"]
        existing = [c for c in cols if c in df.columns]
        print(df[existing].to_string(index=False))

    # ── Trailing Stop Updater  (trailing-stops skill hybrid approach) ──────────

    def update_trailing_stops(self, atr_mult: float = 2.0) -> List[Dict]:
        """
        Update trailing stops for ALL open paper trades.

        Hybrid approach (from trailing-stops skill):
            profit < 1R  → keep original stop
            profit ≥ 1R  → move to breakeven
            profit ≥ 2R  → ATR trail: highest close − ATR × atr_mult
            profit ≥ 3R  → tighter ATR trail (mult × 0.75)

        For each open trade:
            1. Fetch latest price via yfinance
            2. Compute new trailing stop
            3. If new_stop > current_stop → ratchet up in DB
            4. If current_price < new_stop → close trade (stopped out)

        Returns list of stop-update dicts.
        """
        from data.fetcher import fetch_single
        from utils.indicators import add_atr

        updates = []
        with self._conn() as conn:
            open_trades = read_sql_df(
                "SELECT * FROM trades WHERE status='OPEN' AND action='BUY'", conn
            )

        if open_trades.empty:
            print("  [TrailStop] No open trades to update.")
            return updates

        print(f"\n  [TrailStop] Checking {len(open_trades)} open position(s)…")

        for _, row in open_trades.iterrows():
            ticker     = row["ticker"]
            entry_p    = float(row["price"])
            quantity   = int(row["quantity"])
            curr_sl    = float(row["sl"]) if row["sl"] else entry_p * 0.97
            trade_id   = int(row["id"])

            try:
                df     = fetch_single(ticker, period="3mo")
                df     = add_atr(df)
                curr_p = float(df["Close"].iloc[-1])
                atr    = float(df["ATR"].iloc[-1])

                # R = initial risk per share
                initial_risk = entry_p - curr_sl
                if initial_risk <= 0:
                    initial_risk = entry_p * 0.02

                profit_r = (curr_p - entry_p) / (initial_risk + 1e-6)

                # Highest close since entry (last 60 bars as proxy)
                highest_close = float(df["Close"].tail(60).max())

                if profit_r < 1.0:
                    new_sl = curr_sl          # keep original stop
                    reason = "< 1R — keep original"
                elif profit_r < 2.0:
                    new_sl = entry_p          # move to breakeven
                    reason = "≥ 1R — moved to breakeven"
                elif profit_r < 3.0:
                    new_sl = highest_close - atr_mult * atr
                    reason = f"≥ 2R — ATR trail ({atr_mult}×ATR)"
                else:
                    tight_mult = atr_mult * 0.75
                    new_sl     = highest_close - tight_mult * atr
                    reason     = f"≥ 3R — tight ATR trail ({tight_mult:.2f}×ATR)"

                new_sl = max(new_sl, curr_sl)  # ratchet: never lower the stop

                update = {
                    "ticker":     ticker,
                    "trade_id":   trade_id,
                    "curr_price": round(curr_p, 2),
                    "entry":      round(entry_p, 2),
                    "old_sl":     round(curr_sl, 2),
                    "new_sl":     round(new_sl, 2),
                    "profit_r":   round(profit_r, 2),
                    "reason":     reason,
                    "stopped":    False,
                }

                if curr_p <= new_sl:
                    # Stopped out
                    pnl     = (curr_p - entry_p) * quantity
                    pnl_pct = (curr_p / entry_p - 1) * 100
                    now     = datetime.now().isoformat()
                    with self._conn() as conn:
                        conn.execute(
                            "UPDATE trades SET status='STOPPED', exit_price=?, exit_reason=?, "
                            "exit_time=?, pnl=?, pnl_pct=? WHERE id=?",
                            (curr_p, "Trailing stop hit", now, pnl, pnl_pct, trade_id)
                        )
                        conn.commit()
                    self.alerter.send_signal(ticker, "SELL", curr_p,
                                             strategy=str(row.get("strategy", "")),
                                             reason="Trailing stop hit")
                    icon = "💚" if pnl >= 0 else "❤️"
                    print(f"  {icon} STOPPED {ticker:<18}  Rs.{curr_p:.2f}  "
                          f"P&L=Rs.{pnl:+,.0f} ({pnl_pct:+.2f}%)")
                    self.cash += curr_p * quantity
                    if ticker in self.positions:
                        del self.positions[ticker]
                    update["stopped"] = True
                else:
                    # Update trailing stop in DB
                    with self._conn() as conn:
                        conn.execute(
                            "UPDATE trades SET sl=?, trail_stop=? WHERE id=?",
                            (new_sl, new_sl, trade_id)
                        )
                        conn.commit()
                    moved = new_sl > curr_sl
                    icon  = "↑" if moved else "—"
                    print(f"  {icon} {ticker:<22}  price=Rs.{curr_p:.2f}  "
                          f"stop: Rs.{curr_sl:.2f} → Rs.{new_sl:.2f}  ({reason})")

                updates.append(update)

            except Exception as e:
                print(f"  ⚠️  {ticker}: trail stop error — {e}")

        return updates

    # ── CSV Trade Journal  (trade-journal skill format) ───────────────────────

    def export_journal_csv(self, path: str = JOURNAL_CSV) -> str:
        """
        Export all closed/stopped trades to a CSV in the format
        defined by the trade-journal skill (compatible with performance_summary()).

        Returns the file path written.
        """
        df = self.get_trade_history()
        closed = df[df["status"].isin(["CLOSED", "STOPPED"])].copy()
        if closed.empty:
            print(f"  [Journal] No closed trades to export.")
            return path

        closed["date_entry"]      = pd.to_datetime(closed["timestamp"]).dt.strftime("%Y-%m-%d")
        closed["time_entry"]      = pd.to_datetime(closed["timestamp"]).dt.strftime("%H:%M")
        closed["symbol"]          = closed["ticker"]
        closed["setup_type"]      = closed["strategy"].fillna("Unknown")
        closed["timeframe"]       = "Daily"
        closed["direction"]       = "Long"
        closed["entry_price"]     = closed["price"]
        closed["stop_price"]      = closed["sl"]
        closed["target_price"]    = closed["tp"]
        closed["planned_shares"]  = closed["quantity"]
        closed["planned_risk_pct"]= 2.0
        closed["date_exit"]       = pd.to_datetime(closed["exit_time"], errors="coerce").dt.strftime("%Y-%m-%d")
        closed["exit_price"]      = closed["exit_price"]
        closed["exit_reason"]     = closed["exit_reason"].fillna("Manual")
        closed["actual_shares"]   = closed["quantity"]

        # FIX COST-MODEL (audit #23): was a flat 0.17% of the ENTRY value only.
        # Round-trip delivery charges per trade from trading.costs (STT both
        # legs, stamp duty, exchange/SEBI, GST, DP, brokerage).
        from trading.costs import charges_for_trades
        closed["charges"]   = charges_for_trades(closed)
        closed["gross_pnl"] = closed["pnl"]
        closed["net_pnl"]   = (closed["pnl"] - closed["charges"]).round(2)

        risk_pershare = (closed["price"] - closed["sl"].fillna(closed["price"] * 0.97)).abs()
        closed["planned_rr"] = (
            (closed["tp"] - closed["price"]).abs() / risk_pershare.replace(0, np.nan)
        ).round(2)
        closed["actual_rr"] = (
            (closed["exit_price"] - closed["price"]) / risk_pershare.replace(0, np.nan)
        ).round(2)

        closed["rule_violations"]  = "None"
        closed["notes"]            = ""
        closed["market_condition"] = ""
        closed["emotion"]          = "Calm"

        journal_cols = [
            "date_entry","time_entry","symbol","setup_type","timeframe","direction",
            "entry_price","stop_price","target_price","planned_shares","planned_risk_pct",
            "date_exit","exit_price","exit_reason","actual_shares","gross_pnl","charges",
            "net_pnl","planned_rr","actual_rr","rule_violations","notes",
            "market_condition","emotion"
        ]
        journal = closed[[c for c in journal_cols if c in closed.columns]]
        journal.to_csv(path, index=False)
        print(f"  [Journal] Exported {len(journal)} trade(s) to {path}")
        return path

    def performance_summary(self, period_days: int = 90) -> None:
        """Print key performance metrics from closed trades."""
        df = self.get_trade_history()
        closed = df[df["status"].isin(["CLOSED", "STOPPED"])].copy()
        if closed.empty:
            print("  No closed trades yet — run --mode scan to generate signals.")
            return

        closed["pnl"] = pd.to_numeric(closed["pnl"], errors="coerce").fillna(0)
        total     = len(closed)
        wins      = (closed["pnl"] > 0).sum()
        losses    = (closed["pnl"] < 0).sum()
        win_rate  = wins / max(total, 1) * 100
        avg_win   = closed[closed["pnl"] > 0]["pnl"].mean() if wins  > 0 else 0
        avg_loss  = closed[closed["pnl"] < 0]["pnl"].mean() if losses > 0 else 0
        total_pnl = closed["pnl"].sum()
        payoff    = abs(avg_win / avg_loss) if avg_loss != 0 else float("inf")
        expectancy= (win_rate/100 * avg_win) + ((1 - win_rate/100) * avg_loss)

        from trading.costs import charges_for_trades       # FIX COST-MODEL
        charges   = float(charges_for_trades(closed).sum())
        net_pnl   = total_pnl - charges

        print(f"\n  {'='*50}")
        print(f"  PAPER TRADE PERFORMANCE SUMMARY")
        print(f"  {'='*50}")
        print(f"  Total trades    : {total}")
        print(f"  Wins / Losses   : {wins} / {losses}  ({win_rate:.1f}% win rate)")
        print(f"  Total Gross P&L : Rs.{total_pnl:+,.0f}")
        print(f"  Est. Charges    : Rs.{charges:,.0f}")
        print(f"  Total Net P&L   : Rs.{net_pnl:+,.0f}")
        print(f"  Avg Win         : Rs.{avg_win:,.0f}")
        print(f"  Avg Loss        : Rs.{avg_loss:,.0f}")
        print(f"  Payoff Ratio    : {payoff:.2f}:1")
        print(f"  Expectancy      : Rs.{expectancy:,.0f}/trade")
        print(f"  {'='*50}")

        if total > 0:
            best  = closed.loc[closed["pnl"].idxmax()]
            worst = closed.loc[closed["pnl"].idxmin()]
            print(f"  Best  trade : {best['ticker']}  Rs.{best['pnl']:+,.0f}")
            print(f"  Worst trade : {worst['ticker']}  Rs.{worst['pnl']:+,.0f}")
        print()
