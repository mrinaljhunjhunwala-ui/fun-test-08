"""
analysis/portfolio_posture.py — Per-holding posture analysis for Angel One
delivery holdings and intraday positions.

Runs the same six momentum filters analysis/momentum_scanner.py uses, but
applied *retroactively* to what the user already owns — so instead of "is
this a fresh breakout to enter", the question becomes "is the setup that
justified holding still intact, or is it deteriorating".

Output per holding is a DELIVERY_POSTURE label (HOLD / TRIM_WATCH /
EXIT_WATCH / ADD_WATCH) or per intraday position an INTRADAY_POSTURE label
(RUNNING / TRAIL_TIGHTER / STOP_HIT / TARGET_HIT). These are DESCRIPTIVE
tags — not recommendations. The dashboard renders them as coloured chips
with a plain-English reason so the user decides.

Pure module (no Streamlit) so tests can run offline and the alert pipeline
can call the same functions the page uses. Follows CLAUDE.md rule 4.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import pandas as pd

# Reuse the scanner's indicator functions and filter thresholds so the
# analysis stays consistent with what surfaced the pick in the first place.
from analysis.momentum_scanner import (
    DEFAULT_CONFIG,
    _atr,
    _relative_strength,
    _rolling_vwap,
    _rsi,
)

_log = logging.getLogger("analysis.portfolio_posture")


# ─────────────────────────────────────────────────────────────────────────────
# Posture labels
# ─────────────────────────────────────────────────────────────────────────────

DELIVERY_POSTURES = ("ADD_WATCH", "HOLD", "TRIM_WATCH", "EXIT_WATCH", "STOPPED_OUT")
INTRADAY_POSTURES = ("RUNNING", "TRAIL_TIGHTER", "STOP_HIT", "TARGET_HIT", "UNCLEAR")

# Tail of the all-quiet message; alerts/check_alerts.py matches on it to
# suppress the send, so both sides import this one constant.
NO_FLAGGED_POSTURES = "No postures flagged."


@dataclass
class HoldingPosture:
    """Per-holding delivery posture. Fields mirror what the page renders as a
    row so the DataFrame conversion is trivial."""
    symbol: str
    qty: int
    avg_price: float
    ltp: float
    pnl_pct: float
    posture: str                     # one of DELIVERY_POSTURES
    reason: str                      # plain-English one-liner
    filters_passing: int             # 0–6 of the momentum filters currently pass
    filters_detail: Dict[str, bool] = field(default_factory=dict)
    rsi: Optional[float]     = None
    sma50: Optional[float]   = None
    sma200: Optional[float]  = None
    atr: Optional[float]     = None
    rs_63d: Optional[float]  = None
    suggested_stop: Optional[float]     = None    # 2-ATR below LTP
    breakdown_below: Optional[float]    = None    # SMA50 (invalidation level)


@dataclass
class IntradayPosture:
    """Per-open-position intraday posture."""
    symbol: str
    qty: int
    avg_price: float
    ltp: float
    pnl_pct: float
    posture: str                     # one of INTRADAY_POSTURES
    reason: str
    vwap_today: Optional[float] = None
    stop_suggested: Optional[float] = None   # trailing stop suggestion
    target_pct_hit: Optional[float] = None


# ─────────────────────────────────────────────────────────────────────────────
# Delivery-holding analysis
# ─────────────────────────────────────────────────────────────────────────────

def analyse_delivery_holding(
    holding: Dict,
    df: pd.DataFrame,
    nifty_history: Optional[pd.DataFrame] = None,
    cfg=DEFAULT_CONFIG,
) -> Optional[HoldingPosture]:
    """Compute the posture for one Angel One delivery holding.

    Args:
        holding: dict from angel_fetcher.get_holdings() —
                 expects "symbol", "qty", "avg_price", "ltp".
        df:      2y daily OHLCV bars for the holding's ticker.
        nifty_history: 2y daily bars for ^NSEI (for RS ranking). Optional.
        cfg:     MomentumConfig — reuse the scanner's defaults.

    Returns:
        HoldingPosture, or None if data is insufficient (< min_bars_needed).
    """
    symbol = holding.get("symbol", "?")
    qty    = int(holding.get("qty", 0) or 0)
    avg    = float(holding.get("avg_price", 0) or 0)
    ltp_fb = float(holding.get("ltp", 0) or 0)

    if df is None or df.empty or len(df) < cfg.min_bars_needed:
        return None
    df = df[df["Close"] > 0].dropna(subset=["Close", "High", "Low", "Volume"])
    if len(df) < cfg.min_bars_needed:
        return None

    close = df["Close"]
    ltp   = float(close.iloc[-1]) if ltp_fb <= 0 else ltp_fb
    pnl_pct = ((ltp / avg) - 1) * 100 if avg > 0 else 0.0

    # ── Six momentum filters, computed for the CURRENT bar ────────────────
    sma_fast = float(close.rolling(cfg.trend_fast_sma).mean().iloc[-1])
    sma_slow = float(close.rolling(cfg.trend_slow_sma).mean().iloc[-1])
    prior_high_55 = float(close.iloc[-(cfg.breakout_lookback + 1):-1].max())
    vol_today = float(df["Volume"].iloc[-1])
    vol_avg   = float(df["Volume"].iloc[-(cfg.volume_avg_days + 1):-1].mean())
    rsi_val   = float(_rsi(close, cfg.rsi_period).iloc[-1])
    vwap_val  = float(_rolling_vwap(df).iloc[-1])
    atr_val   = float(_atr(df, cfg.atr_period).iloc[-1])

    # For a HOLDING (already owned), the "breakout" filter is not "fresh
    # breakout today"; it's "still above the 55-day breakout level" — that
    # keeps the invalidation criterion consistent with why the pick was
    # taken. If today's price is BELOW the prior 55d high computed from
    # today looking back, the setup has decayed.
    filters = {
        "trend":     ltp > sma_fast > sma_slow,
        "breakout":  ltp > prior_high_55,
        "volume":    vol_avg > 0 and vol_today >= 1.5 * vol_avg,
        "rsi_band":  cfg.rsi_min <= rsi_val <= cfg.rsi_max,
        "vwap":      ltp > vwap_val,
        "atr_ok":    atr_val > 0,   # always true for a real name — placeholder
    }
    n_pass = sum(1 for v in filters.values() if v)

    # ── Posture decision — descriptive, not directive ─────────────────────
    posture, reason = _classify_delivery(filters, pnl_pct, ltp, sma_fast, atr_val)

    return HoldingPosture(
        symbol=symbol,
        qty=qty,
        avg_price=round(avg, 2),
        ltp=round(ltp, 2),
        pnl_pct=round(pnl_pct, 2),
        posture=posture,
        reason=reason,
        filters_passing=n_pass,
        filters_detail=filters,
        rsi=round(rsi_val, 1),
        sma50=round(sma_fast, 2),
        sma200=round(sma_slow, 2),
        atr=round(atr_val, 2),
        rs_63d=(round(_relative_strength(
            close,
            nifty_history["Close"] if nifty_history is not None else None,
            cfg.rs_lookback,
        ) or 0.0, 2)),
        suggested_stop=round(ltp - 2 * atr_val, 2),
        breakdown_below=round(sma_fast, 2),
    )


def _classify_delivery(
    filters: Dict[str, bool],
    pnl_pct: float,
    ltp: float,
    sma50: float,
    atr: float,
) -> tuple[str, str]:
    """Turn six-filter state + P&L into a descriptive posture label.

    Priority order:
      1. If trend has broken (price < SMA50 < SMA200 zone) — EXIT_WATCH
      2. If price below SMA50 but SMA50 still > SMA200 — TRIM_WATCH
      3. If 5–6 filters pass AND positive P&L — ADD_WATCH
      4. If 3–4 filters pass — HOLD
      5. If 0–2 filters pass — TRIM_WATCH (setup decayed)
    """
    n_pass = sum(1 for v in filters.values() if v)

    if not filters["trend"] and ltp < sma50:
        # SMA50 breakdown is the classic momentum invalidation
        return (
            "EXIT_WATCH",
            f"Price ₹{ltp:,.2f} below SMA50 ₹{sma50:,.2f} — momentum thesis invalidated. "
            "P&L " + (f"+{pnl_pct:.1f}%" if pnl_pct >= 0 else f"{pnl_pct:.1f}%") + ".",
        )
    if n_pass >= 5 and pnl_pct >= 0:
        return (
            "ADD_WATCH",
            f"{n_pass}/6 filters passing + running position — thesis intact and extending.",
        )
    if n_pass <= 2:
        return (
            "TRIM_WATCH",
            f"Only {n_pass}/6 filters passing — setup materially decayed. "
            f"Invalidation below SMA50 ₹{sma50:,.2f} (currently {(ltp/sma50-1)*100:+.1f}%).",
        )
    return (
        "HOLD",
        f"{n_pass}/6 filters passing — thesis intact but not extending. "
        f"2-ATR below price is ₹{ltp - 2*atr:,.2f}.",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Intraday-position analysis
# ─────────────────────────────────────────────────────────────────────────────

def analyse_intraday_position(
    position: Dict,
    df: pd.DataFrame,
    *,
    stop_pct: float = 1.0,
    target_pct: float = 2.0,
) -> Optional[IntradayPosture]:
    """Compute intraday posture for one open Angel One day position.

    Uses SIMPLE percent-based stop/target because we don't have live
    tick data here — the alert and page path receive `df` as the daily
    2y bars from data.fetcher, and we can approximate today's session
    VWAP from that. Refine to true intraday when the fetcher's
    fetch_intraday path is wired in (deferred).

    Args:
        position:    dict from angel_fetcher.get_positions().day —
                     expects "symbol", "qty", "avg_price", "ltp", "side".
        df:          2y daily bars for the underlying (for ATR + VWAP proxy).
        stop_pct:    percent below avg_price that flips to STOP_HIT (long)
                     or above (short). Default 1 %.
        target_pct:  percent above avg_price that flips to TARGET_HIT.
    """
    symbol = position.get("symbol", "?")
    qty    = int(position.get("qty", 0) or 0)
    if qty == 0:
        return None
    avg    = float(position.get("avg_price", 0) or 0)
    ltp    = float(position.get("ltp", 0) or 0)
    side   = position.get("side", "LONG" if qty > 0 else "SHORT").upper()

    if avg <= 0 or ltp <= 0:
        return IntradayPosture(
            symbol=symbol, qty=qty, avg_price=avg, ltp=ltp, pnl_pct=0.0,
            posture="UNCLEAR",
            reason="Missing avg_price or LTP — cannot compute posture.",
        )

    pnl_pct = ((ltp / avg) - 1) * 100 * (1 if side == "LONG" else -1)

    vwap_val = None
    if df is not None and not df.empty:
        try:
            vwap_val = float(_rolling_vwap(df).iloc[-1])
        except Exception as _e:
            _log.debug("intraday vwap failed for %s: %s", symbol, _e)

    # Posture: stop / target / running
    if pnl_pct <= -stop_pct:
        posture, reason = (
            "STOP_HIT",
            f"Position down {pnl_pct:.2f}% — beyond intraday stop of {stop_pct:.1f}%.",
        )
    elif pnl_pct >= target_pct:
        posture, reason = (
            "TARGET_HIT",
            f"Position up {pnl_pct:.2f}% — beyond intraday target of {target_pct:.1f}%. "
            "Past the intraday target.",
        )
    elif pnl_pct >= target_pct * 0.6:
        posture, reason = (
            "TRAIL_TIGHTER",
            f"Position up {pnl_pct:.2f}% — through 60% of target. "
            f"Breakeven (entry) is ₹{avg:,.2f}.",
        )
    else:
        posture, reason = (
            "RUNNING",
            f"Position {pnl_pct:+.2f}% — within trade range, no action indicated.",
        )

    stop_suggested = (
        round(avg * (1 - stop_pct/100), 2) if side == "LONG"
        else round(avg * (1 + stop_pct/100), 2)
    )

    return IntradayPosture(
        symbol=symbol,
        qty=qty,
        avg_price=round(avg, 2),
        ltp=round(ltp, 2),
        pnl_pct=round(pnl_pct, 2),
        posture=posture,
        reason=reason,
        vwap_today=round(vwap_val, 2) if vwap_val is not None else None,
        stop_suggested=stop_suggested,
        target_pct_hit=round(pnl_pct / target_pct * 100, 1) if target_pct > 0 else None,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Batch entry — analyse a whole portfolio
# ─────────────────────────────────────────────────────────────────────────────

def analyse_holdings(
    holdings: List[Dict],
    fetcher: Callable[[str], Optional[pd.DataFrame]],
    nifty_history: Optional[pd.DataFrame] = None,
    skipped: Optional[List[Dict[str, str]]] = None,
) -> List[HoldingPosture]:
    """Analyse all delivery holdings. Skips names for which fetch fails
    (rather than raising) so one broken symbol doesn't kill the batch.

    Pass a list as `skipped` to receive {"symbol", "reason"} for every
    holding that produced no posture, so the page can say why."""
    out: List[HoldingPosture] = []
    need = DEFAULT_CONFIG.min_bars_needed
    for h in holdings or []:
        sym = h.get("symbol", "")
        try:
            df = fetcher(sym)
        except Exception as _fe:
            _log.debug("analyse_holdings: fetch failed for %s: %s", sym, _fe)
            df = None
        p = analyse_delivery_holding(h, df, nifty_history)
        if p is not None:
            out.append(p)
        elif skipped is not None:
            if df is None or df.empty:
                reason = "price history could not be fetched"
            else:
                reason = (f"only {len(df)} trading days of history "
                          f"(needs {need} for the 200-day average) — likely a recent listing")
            skipped.append({"symbol": sym, "reason": reason})
    return out


def analyse_positions(
    positions_day: List[Dict],
    fetcher: Callable[[str], Optional[pd.DataFrame]],
) -> List[IntradayPosture]:
    out: List[IntradayPosture] = []
    for pos in positions_day or []:
        try:
            df = fetcher(pos.get("symbol", ""))
        except Exception:
            df = None
        p = analyse_intraday_position(pos, df)
        if p is not None:
            out.append(p)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Alert-friendly summary
# ─────────────────────────────────────────────────────────────────────────────

def format_portfolio_message(
    holdings: List[HoldingPosture],
    positions: List[IntradayPosture],
    *,
    only_actionable: bool = True,
) -> str:
    """Format a per-portfolio message. If only_actionable is True (default
    for the alert path), skips HOLD/RUNNING rows and surfaces only the
    postures the user should look at."""
    urgent_delivery = [
        h for h in holdings
        if h.posture in ("EXIT_WATCH", "TRIM_WATCH", "ADD_WATCH")
        or not only_actionable
    ]
    urgent_intraday = [
        p for p in positions
        if p.posture in ("STOP_HIT", "TARGET_HIT", "TRAIL_TIGHTER")
        or not only_actionable
    ]

    if not urgent_delivery and not urgent_intraday:
        return (
            "📋 <b>Portfolio Posture</b>\n"
            "All delivery holdings currently HOLD, no intraday positions need attention. "
            + NO_FLAGGED_POSTURES
        )

    lines = ["📋 <b>Portfolio Posture</b>", ""]
    if urgent_delivery:
        lines.append("<b>Delivery — needs attention</b>")
        for h in urgent_delivery:
            lines.append(
                f"• <b>{h.symbol}</b> [{h.posture}] "
                f"P&L {h.pnl_pct:+.1f}% — {h.reason}"
            )
        lines.append("")
    if urgent_intraday:
        lines.append("<b>Intraday — needs attention</b>")
        for p in urgent_intraday:
            lines.append(
                f"• <b>{p.symbol}</b> [{p.posture}] "
                f"P&L {p.pnl_pct:+.2f}% — {p.reason}"
            )
        lines.append("")
    lines.append(
        "<i>Descriptive posture from same-recipe momentum filters. "
        "Not advice.</i>"
    )
    return "\n".join(lines)


__all__ = [
    "DELIVERY_POSTURES", "INTRADAY_POSTURES", "NO_FLAGGED_POSTURES",
    "HoldingPosture", "IntradayPosture",
    "analyse_delivery_holding", "analyse_intraday_position",
    "analyse_holdings", "analyse_positions",
    "format_portfolio_message",
]
