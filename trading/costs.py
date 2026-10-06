"""trading/costs.py — Indian equity trading charges, one model for every caller.

Audit 2026-10-03 #23: backtests charged STT on the sell side only (delivery
STT is 0.1% on BOTH legs) and ignored stamp duty and DP charges; the paper
trader charged a flat 0.17% of the ENTRY value only; Paper Trades showed
gross P&L. Backtests, the paper trader and the Paper Trades page now all
price a round trip with this module.

Rates (checked 2026-10-06):
  * STT — delivery 0.1% buy + 0.1% sell; intraday 0.025% sell only.
  * NSE transaction charge 0.00297%; SEBI fee ₹10 per crore (0.0001%).
  * Stamp duty — buy side only: delivery 0.015%, intraday 0.003%.
  * GST 18% on brokerage + transaction charge + SEBI fee (and DP charge).
  * Brokerage — Angel One (Nov-2025 pricing): lower of ₹20 or 0.1% per
    executed order, minimum ₹5; same for delivery and intraday.
  * DP charge — ₹20 per scrip on a delivery SELL (+ GST).
Brokerage is the only broker-specific line; pass a different CostSchedule
for another broker.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

DELIVERY = "DELIVERY"
INTRADAY = "INTRADAY"


@dataclass(frozen=True)
class CostSchedule:
    stt_delivery: float = 0.001          # each leg
    stt_intraday_sell: float = 0.00025   # sell leg only
    exchange_txn: float = 0.0000297      # NSE equity cash, each leg
    sebi_fee: float = 0.000001           # ₹10 / crore, each leg
    stamp_delivery_buy: float = 0.00015
    stamp_intraday_buy: float = 0.00003
    gst: float = 0.18
    brokerage_pct: float = 0.001         # 0.1% ...
    brokerage_cap: float = 20.0          # ... capped at ₹20 per order
    brokerage_min: float = 5.0           # ... with a ₹5 floor
    dp_per_sell: float = 20.0            # delivery sells, per scrip
    as_of: str = "2026-10 (Angel One Nov-2025 brokerage)"


DEFAULT = CostSchedule()


def _brokerage(value: float, s: CostSchedule) -> float:
    if value <= 0:
        return 0.0
    return max(s.brokerage_min, min(s.brokerage_cap, s.brokerage_pct * value))


def leg_charges(value: float, side: str, product: str = DELIVERY,
                s: CostSchedule = DEFAULT) -> Dict[str, float]:
    """Charges for one executed order of `value` rupees. side: BUY / SELL."""
    value = abs(float(value or 0.0))
    side = str(side).upper()
    product = str(product).upper()
    if value <= 0:
        return {"stt": 0.0, "exchange": 0.0, "sebi": 0.0, "stamp": 0.0,
                "brokerage": 0.0, "dp": 0.0, "gst": 0.0, "total": 0.0}
    intraday = product == INTRADAY
    if intraday:
        stt = s.stt_intraday_sell * value if side == "SELL" else 0.0
        stamp = s.stamp_intraday_buy * value if side == "BUY" else 0.0
        dp = 0.0
    else:
        stt = s.stt_delivery * value
        stamp = s.stamp_delivery_buy * value if side == "BUY" else 0.0
        dp = s.dp_per_sell if side == "SELL" else 0.0
    exch = s.exchange_txn * value
    sebi = s.sebi_fee * value
    brk = _brokerage(value, s)
    gst = s.gst * (brk + exch + sebi + dp)
    total = stt + exch + sebi + stamp + brk + dp + gst
    return {"stt": stt, "exchange": exch, "sebi": sebi, "stamp": stamp,
            "brokerage": brk, "dp": dp, "gst": gst, "total": total}


def round_trip(entry_price: float, exit_price: float, qty: float,
               product: str = DELIVERY, side: str = "BUY",
               s: CostSchedule = DEFAULT) -> Dict[str, float]:
    """Charges for opening and closing a position. side is the OPENING side
    (BUY for a long, SELL for a short)."""
    qty = abs(float(qty or 0))
    open_side = str(side).upper()
    close_side = "SELL" if open_side == "BUY" else "BUY"
    a = leg_charges(float(entry_price) * qty, open_side, product, s)
    b = leg_charges(float(exit_price) * qty, close_side, product, s)
    out = {k: a[k] + b[k] for k in a}
    out["turnover"] = (float(entry_price) + float(exit_price)) * qty
    return out


def net_pnl(entry_price: float, exit_price: float, qty: float,
            product: str = DELIVERY, side: str = "BUY",
            s: CostSchedule = DEFAULT) -> Dict[str, float]:
    """Gross P&L, charges and net P&L for a closed position."""
    qty = abs(float(qty or 0))
    sign = 1.0 if str(side).upper() == "BUY" else -1.0
    gross = sign * (float(exit_price) - float(entry_price)) * qty
    charges = round_trip(entry_price, exit_price, qty, product, side, s)["total"]
    return {"gross": gross, "charges": charges, "net": gross - charges}


def charges_for_trades(trades, product: str = DELIVERY, s: CostSchedule = DEFAULT,
                       entry_col: str = "price", exit_col: str = "exit_price",
                       qty_col: str = "quantity", side_col: str = "action"):
    """Round-trip charges per closed trade row (pandas Series aligned to `trades`).
    Rows without an exit price or quantity get 0. side_col BUY/SELL is the
    opening side; missing means BUY (long)."""
    import pandas as pd

    def _row(r) -> float:
        try:
            ep, xp, q = float(r[entry_col]), float(r[exit_col]), float(r[qty_col])
        except (TypeError, ValueError, KeyError):
            return 0.0
        if not (ep > 0 and xp > 0 and q > 0):
            return 0.0
        side = str(r.get(side_col) or "BUY").upper()
        side = "SELL" if side.startswith("S") else "BUY"
        return round_trip(ep, xp, q, product, side, s)["total"]

    if trades is None or len(trades) == 0:
        return pd.Series(dtype=float)
    return trades.apply(_row, axis=1).round(2)


def representative_round_trip_pct(order_value: float = 200_000.0, product: str = DELIVERY,
                                  s: CostSchedule = DEFAULT) -> float:
    """Round-trip charges as a fraction of one side's value, at a typical
    ticket size (flat-rate fallback / disclosures)."""
    rt = round_trip(order_value, order_value, 1, product, "BUY", s)
    return rt["total"] / order_value


def backtest_commission(order_size: float, price: float) -> float:
    """backtesting.py `commission=` callable: rupee charges for one fill.
    Positive size = buy, negative = sell; delivery product."""
    side = "BUY" if order_size > 0 else "SELL"
    return leg_charges(abs(order_size) * price, side, DELIVERY)["total"]


def backtest_commission_arg():
    """The `commission=` argument for backtesting.Backtest.

    backtesting.py >= 0.6 charges `commission` on EVERY fill and accepts a
    callable, so the exact per-fill charges are passed. 0.5.x (what the
    `<0.6` requirement installs on CI / Streamlit Cloud) accepts only a float
    and charges it ONCE per round trip — verified 2026-10-06: commission=0.001
    on a ₹1L flat round trip cost exactly ₹100. So for 0.5.x the FULL
    round-trip rate (at a ₹2L ticket) is passed, not half of it.
    (FIX BT-COMMISSION of 2026-09-24 halved it assuming per-fill charging,
    which only 0.6+ does — deployed backtests charged half the round trip.)
    """
    if supports_callable_commission():
        return backtest_commission
    return representative_round_trip_pct()


def supports_callable_commission() -> bool:
    """backtesting.py accepts a per-fill commission callable from 0.6 on
    (0.5.0 accepts only a float)."""
    try:
        import backtesting
        major, minor = (int(x) for x in str(backtesting.__version__).split(".")[:2])
        return (major, minor) >= (0, 6)
    except Exception:
        return False
