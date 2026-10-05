"""
dashboard/shared/checklist_ui.py — 8-factor swing-trade go/no-go checklist.

Extracted verbatim from dashboard/pages/13_swing_checklist.py so it can be
embedded inside 04_analyze_stock.py as an expander (Analysis-page
consolidation #5). The standalone page is redundant once Analyze Stock
also renders this — one location instead of two.

`compute_checklist(sym, df_daily, df_weekly)` — pure computation. Takes
symbol + already-fetched (indicator-enriched) daily & weekly dataframes,
returns a list of check dicts + score / alignment summary / trade-plan levels.

`render_checklist_block(sym, df_daily, df_weekly)` — Streamlit UI. Wraps
the computation in an expander so pages can drop it in with one call.

No new dependencies; uses only helpers already in analysis.mtf /
strategies.sector_rotation / trading.signals — the same set the standalone
page used.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import pandas as pd
import streamlit as st

_log = logging.getLogger("dashboard.shared.checklist_ui")


@dataclass
class ChecklistResult:
    score: int                           # 0-8
    checks: List[Dict[str, Any]]         # full items with pass/fail/detail/tip
    summary: str                         # descriptive factor-alignment line (no advice)
    price: float
    rsi: float
    adx: float
    atr: float
    trade_plan: Optional[Dict[str, float]] = None   # entry/sl/tp/rr when score>=5
    error: Optional[str] = None


def compute_checklist(sym: str,
                      df_daily: pd.DataFrame,
                      df_weekly: pd.DataFrame) -> ChecklistResult:
    """
    Run all 8 checks and return a ChecklistResult. Never raises — errors
    are captured in `error`. Callers can render whether pass or fail.
    """
    try:
        from analysis.mtf import check_daily_weekly_alignment
        from strategies.sector_rotation import SECTORS, compute_sector_scores
        from trading.signals import get_india_vix_regime

        cur = df_daily.iloc[-1]
        price = float(cur["Close"])
        rsi   = float(cur.get("RSI", 50))
        adx   = float(cur.get("ADX", 0)) if not pd.isna(cur.get("ADX", 0)) else 0
        sma20  = float(cur.get("SMA_20", 0))
        sma50  = float(cur.get("SMA_50", 0))
        sma200 = float(cur.get("SMA_200", 0))
        atr    = float(cur.get("ATR", 0))
        # Same session-aware reading as the composite score (previous
        # session / projected while today's NSE session is open).
        from dashboard.shared.volume_display import reading as _vol_reading
        vol_r  = float(_vol_reading(df_daily).get("ratio") or 1.0)

        vix_r  = get_india_vix_regime()
        vix_ok = vix_r["allow_buy"]
        vix_val = vix_r.get("vix") or 0

        # Sector check
        try:
            sec_scores  = compute_sector_scores(period="1y")
            top3        = set(sec_scores.head(3).index.tolist()) if not sec_scores.empty else set()
            ticker_sec  = {t: s for s, ts in SECTORS.items() for t in ts}
            stock_sec   = ticker_sec.get(sym, "Unknown")
            sector_ok   = stock_sec in top3
            sector_str  = f"{stock_sec} ({'Top 3' if sector_ok else 'Not top 3'})"
        except Exception as e:
            sector_ok  = False
            sector_str = f"Sector check failed: {e}"

        # MTF check
        try:
            mtf     = check_daily_weekly_alignment(df_daily, df_weekly)
            mtf_ok  = mtf["alignment"] == "bullish"
            mtf_str = mtf["confirmation"]
        except Exception as e:
            mtf_ok  = False
            mtf_str = f"MTF check failed: {e}"

        checks: List[Dict[str, Any]] = [
            {"name": "1️⃣ VIX Regime", "pass": vix_ok,
             "detail": f"India VIX = {vix_val:.1f} | Regime: {vix_r.get('regime','?').upper()}",
             "tip": "VIX must be ≤ 28. High VIX = panic = avoid new longs."},
            {"name": "2️⃣ Long-Term Trend (SMA200)", "pass": price > sma200 > 0,
             "detail": f"Price ₹{price:.1f} {'>' if price > sma200 else '<'} SMA200 ₹{sma200:.1f}",
             "tip": "Price must be above 200-day SMA to confirm long-term uptrend."},
            {"name": "3️⃣ MA Stack (SMA20 > SMA50)", "pass": sma20 > sma50 > 0,
             "detail": f"SMA20 ₹{sma20:.1f} {'>' if sma20 > sma50 else '<'} SMA50 ₹{sma50:.1f}",
             "tip": "Moving-average alignment confirms short-term uptrend."},
            {"name": "4️⃣ RSI Zone (25–72)", "pass": 25 < rsi < 72,
             "detail": f"RSI = {rsi:.1f} | Ideal entry: 40–60",
             "tip": "RSI in healthy range — not overbought (>72) or in freefall (<25)."},
            {"name": "5️⃣ ADX Trend Strength", "pass": adx >= 20,
             "detail": f"ADX = {adx:.1f} | "
                       f"{'Trending ✅' if adx >= 25 else ('Weak trend' if adx >= 20 else 'Ranging ❌')}",
             "tip": "ADX ≥ 20 confirms trending environment. Below 20 = ranging/choppy."},
            {"name": "6️⃣ Multi-Timeframe Alignment", "pass": mtf_ok,
             "detail": mtf_str,
             "tip": "Both daily and weekly must be bullish for high-conviction swing entry."},
            {"name": "7️⃣ Sector in Top-3", "pass": sector_ok,
             "detail": sector_str,
             "tip": "Stocks in top-3 sectors by momentum have higher win rates."},
            {"name": "8️⃣ Volume Confirmation (≥ 1.2×)", "pass": vol_r >= 1.2,
             "detail": f"Volume Ratio = {vol_r:.2f}× avg | "
                       f"{'Above avg ✅' if vol_r >= 1.2 else 'Below avg ❌'}",
             "tip": "Volume ≥ 1.2× 20-day avg confirms participation behind the move."},
        ]

        score = sum(1 for c in checks if c["pass"])
        # Descriptive only (CLAUDE.md rule 1): how many factors align, never
        # what to do about it.
        summary = (f"✅ STRONG ALIGNMENT — {score} of {len(checks)} factors aligned."
                   if score >= 7 else
                   f"🟡 PARTIAL ALIGNMENT — {score} of {len(checks)} factors aligned."
                   if score >= 5 else
                   f"🔴 WEAK ALIGNMENT — only {score} of {len(checks)} factors aligned.")

        trade_plan = None
        if score >= 5 and atr > 0:
            sl = price - 2 * atr
            tp = price + 3 * atr
            trade_plan = {"entry": price, "sl": sl, "tp": tp,
                          "rr": (tp - price) / max(price - sl, 1e-6), "atr": atr}

        return ChecklistResult(score=score, checks=checks, summary=summary,
                                price=price, rsi=rsi, adx=adx, atr=atr,
                                trade_plan=trade_plan)
    except Exception as e:
        _log.warning("compute_checklist(%s) failed: %s: %s", sym, type(e).__name__, e)
        return ChecklistResult(score=0, checks=[], summary="", price=0, rsi=0, adx=0, atr=0,
                                error=f"{type(e).__name__}: {e}")


def render_checklist_expander(sym: str, df_daily: pd.DataFrame,
                              df_weekly: Optional[pd.DataFrame] = None,
                              *, expanded: bool = False) -> None:
    """
    Streamlit UI wrapper — computes then renders as an expander. Fetches
    the weekly dataframe if not supplied. Silently degrades on any error
    so the surrounding page is never broken.
    """
    try:
        if df_weekly is None:
            from data.fetcher import fetch_single
            from utils.indicators import add_all_indicators
            df_weekly = add_all_indicators(fetch_single(sym, period="2y", interval="1wk"))
            df_weekly = df_weekly.dropna(subset=["RSI"])
    except Exception as e:
        st.caption(f"🎯 Pre-trade checklist unavailable — weekly data fetch failed ({e}).")
        return

    result = compute_checklist(sym, df_daily, df_weekly)
    if result.error:
        st.caption(f"🎯 Pre-trade checklist unavailable — {result.error}")
        return

    title = f"🎯 Pre-trade go/no-go — {result.score}/8 factors passed"
    with st.expander(title, expanded=expanded):
        _card = ("card-green" if result.score >= 5
                 else "card-yellow" if result.score >= 3
                 else "card-red")
        st.markdown(
            f'<div class="{_card}">'
            f'<span class="score-big">{result.score}/8</span> &nbsp;&nbsp;'
            f'<span class="signal-big">{result.summary}</span><br>'
            f'<b>{sym.replace(".NS","")}</b> at ₹{result.price:.2f} | '
            f'RSI {result.rsi:.1f} | ADX {result.adx:.1f}'
            f'</div>',
            unsafe_allow_html=True,
        )
        st.markdown("**Factor detail:**")
        for c in result.checks:
            icon = "✅" if c["pass"] else "❌"
            color = "#4caf50" if c["pass"] else "#c77070"
            st.markdown(
                f"<div style='border-left:4px solid {color}; padding:6px 12px; "
                f"margin:4px 0; background:rgba(255,255,255,0.03); border-radius:4px;'>"
                f"<b>{icon} {c['name']}</b><br>"
                f"<span style='color:#ccc'>{c['detail']}</span><br>"
                f"<small style='color:#888'>{c['tip']}</small></div>",
                unsafe_allow_html=True,
            )
        if result.trade_plan:
            tp = result.trade_plan
            st.markdown(
                f'<div class="card-blue" style="margin-top:10px">'
                f'<b>Suggested trade plan:</b> '
                f'Entry ₹{tp["entry"]:.2f} · SL ₹{tp["sl"]:.2f} (2×ATR) · '
                f'TP ₹{tp["tp"]:.2f} (3×ATR) · R:R {tp["rr"]:.1f}x'
                f'</div>',
                unsafe_allow_html=True,
            )
