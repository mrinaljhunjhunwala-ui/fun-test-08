"""
analysis/sector_baskets.py — sector peer baskets from daily closes.

Yahoo only serves history for three NSE sector indices (Bank, IT, Pharma), so
the calibration page's sector baseline is built from the app's own universe
instead: for each sector in data/universe.SECTOR_MAP, the basket's daily
return is the MEDIAN of its constituents' close-to-close returns — robust to
the small splits/bonuses the corporate-action filter below misses and to a
single name's results-day spike (a mean would be dragged by either).

Closes come from the NSE bhavcopy already stored in nse_delivery_daily
(unadjusted), so a split or bonus shows up as a huge one-day move — any daily
return beyond MAX_ABS_DAILY_RET is dropped as a probable corporate action
(NSE price bands cap most genuine moves at 20%; band-free F&O names can
exceed it, and those rare days are dropped too). Known residual biases:
price-only (no dividends), today's universe (survivorship), and the pick is
itself one of its basket's names.

Pure: no Streamlit, no DB, no network — callers pass the closes in.
"""
from __future__ import annotations

import datetime as _dt
from typing import Dict, Optional

import pandas as pd

MAX_ABS_DAILY_RET = 0.20
MIN_CONSTITUENTS = 5


def basket_daily_returns(closes: pd.DataFrame, sector_of: Dict[str, str], *,
                         max_abs_ret: float = MAX_ABS_DAILY_RET,
                         min_constituents: int = MIN_CONSTITUENTS) -> pd.DataFrame:
    """Daily median-of-constituents return per sector.

    closes: columns symbol, date (ISO str or date), close.
    sector_of: symbol → sector (symbols without a sector are ignored).
    Returns a DataFrame indexed by date (datetime.date, ascending), one column
    per sector; a cell is NaN when fewer than `min_constituents` names had a
    usable return that day. Returns are computed on the table's own date grid:
    a date missing from the whole table is spanned close-to-close by the next
    one; a single symbol missing a date just has no return for the next one.
    """
    if closes is None or closes.empty:
        return pd.DataFrame()
    df = closes[["symbol", "date", "close"]].copy()
    df["sector"] = df["symbol"].map(sector_of)
    df = df[df["sector"].notna()]
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df[df["close"] > 0]
    if df.empty:
        return pd.DataFrame()
    df["date"] = pd.to_datetime(df["date"]).dt.date
    grid = sorted(df["date"].unique())
    wide = df.pivot_table(index="date", columns="symbol", values="close", aggfunc="last")
    wide = wide.reindex(grid)
    rets = wide / wide.shift(1) - 1.0                 # NaN unless closes on both grid days
    rets = rets.where(rets.abs() <= max_abs_ret)      # drop probable corporate actions
    by_sector = {}
    sym_sector = {s: sector_of[s] for s in rets.columns}
    for sector in sorted(set(sym_sector.values())):
        cols = [s for s, sec in sym_sector.items() if sec == sector]
        block = rets[cols]
        med = block.median(axis=1)
        med[block.notna().sum(axis=1) < min_constituents] = float("nan")
        by_sector[sector] = med
    # Keep the first grid day (all NaN) so it can anchor a window's start.
    return pd.DataFrame(by_sector, index=wide.index)


def basket_return(daily: pd.DataFrame, sector: str,
                  start: _dt.date, end: _dt.date) -> Optional[float]:
    """Compounded basket return (%) from the close of `start` to the close of
    `end`. Both dates must be days in the close grid and every grid day after
    `start` up to `end` must have a basket return; otherwise None rather than
    a silently partial number."""
    if daily is None or daily.empty or sector not in daily.columns or end <= start:
        return None
    if start not in daily.index or end not in daily.index:
        return None
    window = daily.loc[(daily.index > start) & (daily.index <= end), sector]
    if window.empty or window.isna().any():
        return None
    return float(((1.0 + window).prod() - 1.0) * 100.0)
