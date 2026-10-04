"""
analysis/sector_strength.py
Relative sector-strength ranking from constituent momentum.

Ranks the app's 16 NSE sectors by the average ~1-month price momentum of their
most-liquid constituents (taken from data.universe). The result is a DataFrame
indexed by sector with a "Rank" column (1 = strongest), shaped exactly for
score_stock(sector_scores_df=...). A stock in a leading sector then earns a
sentiment tailwind; a laggard sector earns a headwind.

Why constituent momentum (not NSE sector indices):
    Only ~8 of the app's 16 sectors map to clean NSE sector indices. Averaging
    the momentum of each sector's own liquid names covers all 16 uniformly and
    reuses the existing fetch layer (Angel One → Yahoo → Stooq).
"""

from __future__ import annotations

import concurrent.futures as _cf
import logging
from typing import Dict, List, Optional

import pandas as pd

_log = logging.getLogger("analysis.sector_strength")


def _ticker_momentum(ticker: str, lookback: int = 21) -> Optional[float]:
    """~1-month (21 trading day) % return for one ticker, or None on failure."""
    try:
        from data.fetcher import fetch_single
        df = fetch_single(ticker, period="3mo")
        close = df["Close"].dropna()
        if len(close) < lookback + 1:
            return None
        return float(close.iloc[-1] / close.iloc[-(lookback + 1)] - 1) * 100.0
    except Exception as e:
        _log.debug("_ticker_momentum: failed for %s: %s", ticker, e)
        return None


def rank_sectors(top_n_per_sector: int = 3, lookback: int = 21) -> pd.DataFrame:
    """
    Rank all sectors by average constituent momentum.

    Args:
        top_n_per_sector : how many (most-liquid) names to sample per sector
        lookback         : momentum lookback in trading days (21 ≈ 1 month)

    Returns:
        DataFrame indexed by sector name with columns:
            Momentum : float  (avg % move of sampled constituents)
            Rank     : int    (1 = strongest sector)
        Sectors with no successful fetch are left out (unranked), so the
        frame can be shorter than list_sectors() — or empty on a full outage.
    """
    from data.universe import list_sectors, get_tickers_by_sector

    sectors = list_sectors()

    # Build the unique fetch set (a name can't belong to two sectors here)
    sector_tickers: Dict[str, List[str]] = {
        s: get_tickers_by_sector(s)[:top_n_per_sector] for s in sectors
    }
    all_tickers = sorted({t for ts in sector_tickers.values() for t in ts})

    # Fetch momentum for every sampled ticker in parallel
    moms: Dict[str, Optional[float]] = {}
    try:
        with _cf.ThreadPoolExecutor(max_workers=10) as ex:
            futs = {ex.submit(_ticker_momentum, t, lookback): t for t in all_tickers}
            for f in _cf.as_completed(futs):
                moms[futs[f]] = f.result()
    except Exception as e:
        _log.warning("rank_sectors: parallel momentum fetch pool failed, falling back to serial: %s", e)
        for t in all_tickers:
            moms[t] = _ticker_momentum(t, lookback)

    # Average per sector (ignoring failed fetches)
    rows = []
    for s in sectors:
        vals = [moms.get(t) for t in sector_tickers[s]]
        vals = [v for v in vals if v is not None]
        if not vals:
            # FIX SECTOR-NODATA (audit 2026-10-03): a sector whose every fetch
            # failed used to enter the ranking at Momentum 0.0, so a full
            # outage ranked sectors 1-22 alphabetically and handed out
            # tailwinds/headwinds with no data. Leave it unranked instead —
            # score_stock scores an unranked sector neutral.
            continue
        avg = round(sum(vals) / len(vals), 2)
        rows.append({"Sector": s, "Momentum": avg, "_n": len(vals)})

    if not rows:
        return pd.DataFrame(columns=["Momentum", "_n", "Rank"])

    df = (
        pd.DataFrame(rows)
        .sort_values("Momentum", ascending=False)
        .reset_index(drop=True)
    )
    df["Rank"] = range(1, len(df) + 1)
    return df.set_index("Sector")


if __name__ == "__main__":   # quick manual check
    import warnings
    warnings.filterwarnings("ignore")
    print(rank_sectors().to_string())
