"""
analysis/hedging.py
Market-sensitivity (beta) analysis for Indian equity portfolios.

Functions
─────────
    calculate_stock_beta(ticker, period)           → float
    calculate_portfolio_beta(holdings, period)     → dict

All data is fetched from yfinance (EOD daily bars).
Benchmark: Nifty 50 (^NSEI)

FIX HEDGE-AUDIT (2026-10-03) — hedge_sizing() / suggest_hedge() were removed:
nothing called them, they sized with a stale 25-unit Nifty lot (65 since Jan
2026) and they emitted "Short N Nifty Futures" instructions (rule 1).
"""

from __future__ import annotations

import logging
import numpy as np
import pandas as pd
from typing import Dict, List, Optional

from data.fetcher import fetch_single

_log = logging.getLogger("analysis.hedging")

# NSE Nifty 50 — beta benchmark
_NIFTY = "^NSEI"


# ─────────────────────────────────────────────────────────────────────────────
# Single-stock beta vs Nifty
# ─────────────────────────────────────────────────────────────────────────────

def calculate_stock_beta(
    ticker: str,
    period: str = "1y",
    nifty_df: Optional[pd.DataFrame] = None,
) -> float:
    """
    Compute rolling beta of a single stock vs Nifty 50.

    Beta = Cov(stock_returns, nifty_returns) / Var(nifty_returns)

    Args:
        ticker    : yfinance symbol (e.g. 'TCS.NS')
        period    : data window (default '1y')
        nifty_df  : pre-fetched Nifty daily close DataFrame (avoids re-fetch)

    Returns:
        beta (float), or np.nan on failure.
    """
    try:
        stock_df = fetch_single(ticker, period=period)
        if stock_df.empty or len(stock_df) < 50:
            return np.nan

        if nifty_df is None:
            nifty_df = fetch_single(_NIFTY, period=period)

        stock_ret = stock_df["Close"].pct_change().dropna()
        nifty_ret = nifty_df["Close"].pct_change().dropna()

        # Align on common dates
        common_idx = stock_ret.index.intersection(nifty_ret.index)
        if len(common_idx) < 30:
            return np.nan

        s = stock_ret.loc[common_idx]
        n = nifty_ret.loc[common_idx]

        cov_matrix = np.cov(s.values, n.values)
        beta = cov_matrix[0, 1] / cov_matrix[1, 1] if cov_matrix[1, 1] != 0 else np.nan
        return round(float(beta), 3)

    except Exception as e:
        _log.debug("calculate_stock_beta: failed for %s, returning NaN: %s", ticker, e)
        return np.nan


# ─────────────────────────────────────────────────────────────────────────────
# Portfolio beta
# ─────────────────────────────────────────────────────────────────────────────

def calculate_portfolio_beta(
    holdings: List[Dict],
    period: str = "1y",
) -> Dict:
    """
    Compute the weighted-average beta of a portfolio vs Nifty 50.

    Args:
        holdings : list of dicts with keys:
                       ticker   (str)   — yfinance symbol
                       value_rs (float) — current market value in Rs
        period   : data lookback for beta calculation

    Returns dict:
        {
            "portfolio_beta"    : float, or None when no holding's beta is measurable,
            "beta_coverage_pct" : % of book value whose beta could be measured,
            "total_value_rs"    : float,
            "holdings_beta"     : [{"ticker", "value_rs", "weight", "beta", "contrib"},...],
            "interpretation"    : str,
            "needs_hedge"       : bool,
        }

    FIX BETA-NAN (2026-10-03): a holding whose beta can't be measured (no
    history, or the Nifty fetch failed) used to add 0.0 to the weighted sum,
    so a network blip reported the whole book as beta 0.0 "defensive". Unknown
    betas are now excluded and the measured weights renormalised; with none
    measurable the book beta is None.
    """
    if not holdings:
        return {"portfolio_beta": 1.0, "total_value_rs": 0.0,
                "holdings_beta": [], "interpretation": "Empty portfolio",
                "needs_hedge": False}

    # Fetch Nifty once and reuse
    try:
        nifty_df = fetch_single(_NIFTY, period=period)
    except Exception as e:
        _log.warning("calculate_portfolio_beta: Nifty fetch failed, betas will be unavailable: %s", e)
        nifty_df = None

    total_value = sum(h["value_rs"] for h in holdings if "value_rs" in h)
    if total_value <= 0:
        total_value = 1.0

    holding_details = []
    weighted_beta = 0.0
    measured_weight = 0.0

    for h in holdings:
        tkr    = h.get("ticker", "")
        val    = float(h.get("value_rs", 0))
        weight = val / total_value
        beta   = calculate_stock_beta(tkr, period=period, nifty_df=nifty_df)

        known   = beta is not None and not np.isnan(beta)
        contrib = weight * beta if known else 0.0
        if known:
            weighted_beta += contrib
            measured_weight += weight

        holding_details.append({
            "ticker":  tkr,
            "value_rs": round(val, 0),
            "weight":   round(weight * 100, 2),   # as %
            "beta":     beta,
            "contrib":  round(contrib, 4),
        })

    coverage_pct = round(measured_weight * 100, 1)

    if measured_weight <= 0:
        return {
            "portfolio_beta":    None,
            "beta_coverage_pct": 0.0,
            "total_value_rs":    round(total_value, 0),
            "holdings_beta":     holding_details,
            "interpretation":    "Beta unavailable — no holding had enough price history "
                                 "aligned with Nifty 50 to measure it.",
            "needs_hedge":       False,
        }

    pb = round(weighted_beta / measured_weight, 3)

    if pb < 0.5:
        interpretation = f"Low beta ({pb}) — portfolio is defensive / lower-volatility than market."
    elif pb < 0.9:
        interpretation = f"Moderate beta ({pb}) — portfolio moves slightly less than market."
    elif pb < 1.2:
        interpretation = f"Market beta ({pb}) — portfolio tracks Nifty closely."
    elif pb < 1.5:
        interpretation = f"High beta ({pb}) — portfolio amplifies market moves."
    else:
        interpretation = f"Very high beta ({pb}) — portfolio moves well beyond the market in both directions."
    if coverage_pct < 99.5:
        interpretation += f" Measured on {coverage_pct:.0f}% of portfolio value."

    needs_hedge = pb > 1.2

    return {
        "portfolio_beta":  pb,
        "beta_coverage_pct": coverage_pct,
        "total_value_rs":  round(total_value, 0),
        "holdings_beta":   holding_details,
        "interpretation":  interpretation,
        "needs_hedge":     needs_hedge,
    }
