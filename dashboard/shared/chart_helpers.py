"""dashboard/shared/chart_helpers.py - charts, live top bar, index explorer."""
from __future__ import annotations
import os, sys, sqlite3, warnings, io, json, math, datetime
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
import plotly.io as pio
from plotly.subplots import make_subplots
from typing import Optional
import streamlit as st
# FIX WARN1 — narrowed from a blanket `filterwarnings("ignore")` so numpy's
# RuntimeWarnings (invalid value / divide by zero / all-NaN slice) stay visible.
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", category=PendingDeprecationWarning)
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


# ─────────────────────────────────────────────────────────────────────────────
# Macro / Breadth helpers  (for new pages 7–9)
# ─────────────────────────────────────────────────────────────────────────────

import logging as _logging
_log = _logging.getLogger("dashboard.chart_helpers")

# FIX MI1 — Yahoo macro fetch now uses the same crumb-authenticated session as
# _index_strip_data() instead of a bare urllib.request.urlopen() call.
# PROBLEM: the old version of load_macro_data() hit Yahoo's v8 chart API with
# no crumb/cookie and only a User-Agent header. Yahoo frequently 401s/429s
# naked requests like that — when all 4 of Gold/Brent/USD-INR/DXY failed
# silently (each wrapped in its own try/except), macro_df was left with only
# the 3 Stooq-backed index columns. That's not enough columns/rows for a
# meaningful 30-day correlation matrix, and any column with too few
# overlapping non-NaN rows could throw pct_change()/corr() output full of NaN,
# rendering as a blank or broken heatmap with no visible error message.
def _yahoo_chart_close(sym: str, range_: str = "3mo") -> pd.Series:
    """Fetch a Yahoo Finance daily close series using crumb-authenticated session.

    FIX MI1: reuses the same _get_yf_crumb() helper that _index_strip_data()
    already relies on, instead of an unauthenticated bare request. Falls back
    to an unauthenticated attempt only if crumb retrieval itself fails.
    """
    import urllib.parse
    import urllib.request

    try:
        from data.fetcher import _get_yf_crumb
        _opener, _crumb = _get_yf_crumb()
    except Exception as _e:
        _log.debug("chart_helpers._yahoo_chart_close crumb unavailable: %s", _e)
        _opener, _crumb = None, ""

    _qs = f"&crumb={urllib.parse.quote(_crumb)}" if _crumb else ""
    _open = _opener.open if _opener else urllib.request.urlopen

    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/"
           f"{urllib.parse.quote(sym)}?interval=1d&range={range_}{_qs}")
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    with _open(req, timeout=10) as r:
        raw = json.loads(r.read())
    res = raw["chart"]["result"][0]
    ts  = res["timestamp"]
    cl  = res["indicators"]["quote"][0]["close"]
    df  = pd.DataFrame({"Close": cl}, index=pd.to_datetime(ts, unit="s")).dropna()
    return df["Close"]


@st.cache_data(ttl=600)
def load_macro_data():
    """
    Fetch 3-month daily history for macro instruments.
    NSE indices via fetch_single() (Stooq first).
    Commodities/FX via Yahoo Finance JSON history (crumb-authenticated — FIX MI1).
    """
    from data.fetcher import fetch_single

    data = {}

    # Indian index series — Stooq handles these reliably
    index_map = {
        "Nifty 50":  "^NSEI",
        "BankNifty": "^NSEBANK",
        "India VIX": "^INDIAVIX",
    }
    for name, sym in index_map.items():
        try:
            df = fetch_single(sym, period="3mo")
            if not df.empty:
                data[name] = df["Close"]
        except Exception as _e:
            _log.debug("chart_helpers.%s degraded: %s", "load_macro_data", _e)
            pass

    # Commodities / FX — Yahoo Finance JSON history (FIX MI1: now crumb-authenticated)
    commodity_map = {
        "Gold ($/oz)": "GC=F",
        "Brent Crude": "BZ=F",
        "USD/INR":     "USDINR=X",
        "DXY":         "DX-Y.NYB",
    }
    for name, sym in commodity_map.items():
        try:
            series = _yahoo_chart_close(sym, range_="3mo")
            if not series.empty:
                data[name] = series
        except Exception as _e:
            _log.debug("chart_helpers.load_macro_data degraded for %s: %s", name, _e)
            pass

    return pd.DataFrame(data).dropna(how="all")


_NIFTY50_TICKERS = (
    "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "ICICIBANK.NS", "BHARTIARTL.NS",
    "INFY.NS", "SBIN.NS", "HINDUNILVR.NS", "ITC.NS", "LT.NS",
    "BAJFINANCE.NS", "HCLTECH.NS", "MARUTI.NS", "SUNPHARMA.NS", "ADANIENT.NS",
    "KOTAKBANK.NS", "TITAN.NS", "ONGC.NS", "NTPC.NS", "AXISBANK.NS",
    "WIPRO.NS", "ULTRACEMCO.NS", "ASIANPAINT.NS", "BAJAJFINSV.NS", "POWERGRID.NS",
    "M&M.NS", "NESTLEIND.NS", "JSWSTEEL.NS", "TMPV.NS", "TATASTEEL.NS",
    "TECHM.NS", "GRASIM.NS", "BPCL.NS", "ADANIPORTS.NS", "CIPLA.NS",
    "BRITANNIA.NS", "EICHERMOT.NS", "DRREDDY.NS", "HINDALCO.NS", "COALINDIA.NS",
    "DIVISLAB.NS", "TATACONSUM.NS", "SBILIFE.NS", "APOLLOHOSP.NS", "HDFCLIFE.NS",
    "INDUSINDBK.NS", "HEROMOTOCO.NS", "BAJAJ-AUTO.NS", "ETERNAL.NS", "SHRIRAMFIN.NS",
)


@st.cache_data(ttl=900)  # 15-min cache
def compute_market_breadth(tickers: tuple):
    """
    Fetch 1-year OHLCV for each ticker via Stooq (no rate limits) in parallel,
    then compute advance/decline, SMA positions, and 52-week extremes.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from data.fetcher import fetch_single

    tickers_list = list(tickers)

    def _fetch_one(t):
        try:
            return t, fetch_single(t, period="1y")
        except Exception as _e:
            _log.debug("chart_helpers.%s degraded: %s", "_fetch_one", _e)
            return t, None

    data_map = {}
    with ThreadPoolExecutor(max_workers=10) as pool:
        futs = {pool.submit(_fetch_one, t): t for t in tickers_list}
        for fut in as_completed(futs, timeout=45):
            try:
                t, df = fut.result(timeout=0)
                if df is not None and not df.empty:
                    data_map[t] = df
            except Exception as _e:
                _log.debug("chart_helpers.%s degraded: %s", "_fetch_one", _e)
                pass

    adv = dec = above_20 = above_50 = above_200 = near_hi = near_lo = counted = 0
    for t in tickers_list:
        try:
            df = data_map.get(t)
            if df is None or len(df) < 10:
                continue
            close = df["Close"]
            curr  = float(close.iloc[-1])
            prev  = float(close.iloc[-2])
            counted += 1
            if curr > prev:
                adv += 1
            else:
                dec += 1
            if len(df) >= 20 and curr > float(close.rolling(20).mean().iloc[-1]):
                above_20 += 1
            if len(df) >= 50 and curr > float(close.rolling(50).mean().iloc[-1]):
                above_50 += 1
            if len(df) >= 200 and curr > float(close.rolling(200).mean().iloc[-1]):
                above_200 += 1
            high52 = float(df["High"].max())
            low52  = float(df["Low"].min())
            if (high52 - curr) / max(high52, 1) * 100 < 5:
                near_hi += 1
            if (curr - low52) / max(low52, 1) * 100 < 5:
                near_lo += 1
        except Exception as _e:
            _log.debug("chart_helpers.%s degraded: %s", "_fetch_one", _e)
            continue

    n = max(counted, 1)
    return {
        "advance":       adv,
        "decline":       dec,
        "total":         counted,
        "ad_ratio":      round(adv / max(dec, 1), 2),
        "pct_above_20":  round(above_20  / n * 100, 1),
        "pct_above_50":  round(above_50  / n * 100, 1),
        "pct_above_200": round(above_200 / n * 100, 1),
        "near_52w_high": near_hi,
        "near_52w_low":  near_lo,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Shared chart builder
# ─────────────────────────────────────────────────────────────────────────────

def build_price_chart(df: pd.DataFrame, ticker: str,
                      period: Optional[str] = None) -> go.Figure:
    """
    4-panel trading chart: Price (candlestick + SMAs + BB) / Volume / RSI / MACD.
    Matches the layout of professional trading terminals.

    `period` — the chart period the caller is showing ("1d"/"5d"/"1m"/"6m"/"ytd"/
    "max"). When it's a SHORT window (≤ 1M) the 200-day SMA is dropped because it
    is either flat off-screen or fills only a fraction of the visible range —
    visual noise, not signal. Passing `period=None` keeps the legacy behaviour
    (always plot every SMA we have) so other callers stay unchanged.
    """
    _hide_sma200 = str(period or "").lower() in {"1d", "5d", "1m"}
    fig = make_subplots(
        rows=4, cols=1, shared_xaxes=True,
        row_heights=[0.52, 0.14, 0.17, 0.17],
        vertical_spacing=0.02,
        subplot_titles=[f"{ticker} — Price", "Volume", "RSI (14)", "MACD"],
    )

    fig.add_trace(go.Candlestick(
        x=df.index, open=df["Open"], high=df["High"],
        low=df["Low"], close=df["Close"],
        name="OHLC",
        increasing_line_color="#26a69a", increasing_fillcolor="#26a69a",
        decreasing_line_color="#ef5350", decreasing_fillcolor="#ef5350",
    ), row=1, col=1)

    if "BB_Upper" in df.columns:
        fig.add_trace(go.Scatter(
            x=df.index, y=df["BB_Upper"],
            line=dict(color="rgba(100,160,255,0.4)", dash="dash", width=1),
            name="BB Upper", showlegend=False,
        ), row=1, col=1)
        fig.add_trace(go.Scatter(
            x=df.index, y=df["BB_Lower"],
            fill="tonexty", fillcolor="rgba(100,160,255,0.06)",
            line=dict(color="rgba(100,160,255,0.4)", dash="dash", width=1),
            name="BB Lower", showlegend=False,
        ), row=1, col=1)

    for sma, color in [("SMA_20", "#FF9800"), ("SMA_50", "#2196F3"), ("SMA_200", "#9C27B0")]:
        if sma == "SMA_200" and _hide_sma200:
            continue   # dropped for ≤ 1M chart windows — see docstring
        if sma in df.columns:
            fig.add_trace(go.Scatter(
                x=df.index, y=df[sma], name=sma,
                line=dict(color=color, width=1.2),
            ), row=1, col=1)

    if "Volume" in df.columns:
        vol_colors = [
            "#26a69a" if c >= o else "#ef5350"
            for c, o in zip(df["Close"], df["Open"])
        ]
        fig.add_trace(go.Bar(
            x=df.index, y=df["Volume"],
            marker_color=vol_colors,
            name="Volume", showlegend=False,
            opacity=0.7,
        ), row=2, col=1)
        vol_ma = df["Volume"].rolling(20).mean()
        fig.add_trace(go.Scatter(
            x=df.index, y=vol_ma,
            line=dict(color="#FFD700", width=1.2, dash="dot"),
            name="Vol MA20", showlegend=False,
        ), row=2, col=1)

    if "RSI" in df.columns:
        fig.add_trace(go.Scatter(
            x=df.index, y=df["RSI"], name="RSI",
            line=dict(color="#CE93D8", width=1.5),
        ), row=3, col=1)
        for level, color in [(30, "#26a69a"), (70, "#ef5350"), (50, "rgba(150,150,150,0.5)")]:
            fig.add_hline(y=level, line_dash="dot", line_color=color, row=3, col=1)
        fig.add_hrect(y0=70, y1=100, fillcolor="rgba(239,83,80,0.06)",
                      line_width=0, row=3, col=1)
        fig.add_hrect(y0=0, y1=30, fillcolor="rgba(38,166,154,0.06)",
                      line_width=0, row=3, col=1)

    if "MACD" in df.columns and "MACD_Signal" in df.columns:
        fig.add_trace(go.Scatter(
            x=df.index, y=df["MACD"], name="MACD",
            line=dict(color="#2196F3", width=1.5),
        ), row=4, col=1)
        fig.add_trace(go.Scatter(
            x=df.index, y=df["MACD_Signal"], name="Signal",
            line=dict(color="#FF9800", width=1.5),
        ), row=4, col=1)
        if "MACD_Hist" in df.columns:
            hist_colors = ["#26a69a" if v >= 0 else "#ef5350" for v in df["MACD_Hist"]]
            fig.add_trace(go.Bar(
                x=df.index, y=df["MACD_Hist"], name="Hist",
                marker_color=hist_colors, opacity=0.6,
            ), row=4, col=1)

    _NSE_GRID = dict(gridcolor="rgba(255,255,255,.04)", linecolor="rgba(255,255,255,.06)")
    _NSE_TICK = dict(color="#4a5568", size=10)
    fig.update_layout(
        height=720,
        paper_bgcolor="#070c18",
        plot_bgcolor="#0a1020",
        xaxis_rangeslider_visible=False,
        font=dict(family="Inter, -apple-system, sans-serif", color="#8899bb", size=11),
        legend=dict(
            orientation="h", y=1.02, x=0,
            bgcolor="rgba(7,12,24,.85)",
            bordercolor="rgba(255,255,255,.06)", borderwidth=1,
            font=dict(color="#8899bb", size=11),
        ),
        margin=dict(l=0, r=60, t=40, b=0),
        hoverlabel=dict(
            bgcolor="#0d1526", bordercolor="rgba(255,255,255,.1)",
            font=dict(color="#f0f4ff", family="Inter", size=12),
        ),
        hovermode="x unified",
    )
    for row in range(1, 5):
        fig.update_xaxes(
            **_NSE_GRID, zeroline=False, tickfont=_NSE_TICK,
            row=row, col=1,
        )
        fig.update_yaxes(
            **_NSE_GRID, zeroline=False, tickfont=_NSE_TICK,
            side="right", row=row, col=1,
        )
    fig.update_yaxes(title_text="₹ Price",  title_font=dict(size=10,color="#4a5568"), row=1, col=1)
    fig.update_yaxes(title_text="Volume",   title_font=dict(size=10,color="#4a5568"), tickformat=".2s", row=2, col=1)
    fig.update_yaxes(title_text="RSI",      title_font=dict(size=10,color="#4a5568"), range=[0,100], row=3, col=1)
    fig.update_yaxes(title_text="MACD",     title_font=dict(size=10,color="#4a5568"), row=4, col=1)
    fig.update_xaxes(showspikes=True, spikethickness=1, spikecolor="#5a6a8a", spikedash="dot")
    fig.update_yaxes(showspikes=True, spikethickness=1, spikecolor="#5a6a8a")
    return fig


_INDEX_STRIP = [
    ("NIFTY 50",   "^NSEI"),      ("BANK NIFTY", "^NSEBANK"),
    ("NIFTY IT",   "^CNXIT"),     ("NIFTY AUTO",  "^CNXAUTO"),
    ("NIFTY FMCG", "^CNXFMCG"),   ("NIFTY PHARMA","^CNXPHARMA"),
    ("NIFTY METAL","^CNXMETAL"),  ("NIFTY ENERGY","^CNXENERGY"),
]


# Plotly colour params don't parse CSS custom properties, so charts need the
# token values as literals. Keep them HERE (mirrors design.py :root) so pages
# stay hex-free and a palette swap is a one-file change.
from dashboard.shared.tokens import COLORS as _TOKENS, hex_to_rgb as _hex_to_rgb  # noqa: E402

PLOT_COLORS = {k: _TOKENS[k] for k in ("bull", "bear", "amber", "accent", "azure", "dim", "faint")}


def diverging_colors(values, full_at: float | None = None) -> list:
    """Per-bar diverging colours: hue by sign (bull/bear), opacity by
    magnitude (0.35 → 1.0 at |v| >= full_at; full_at defaults to the max |v|).
    One encoding per channel: sign = hue, size = strength."""
    vals = []
    for v in values:
        try:
            f = float(v)
        except (TypeError, ValueError):
            f = 0.0
        vals.append(f if math.isfinite(f) else 0.0)
    try:
        peak = float(full_at) if full_at is not None else 0.0
    except (TypeError, ValueError):
        peak = 0.0
    if not math.isfinite(peak) or peak <= 0:
        # None / NaN / inf / <=0 → compute from data (NaN would otherwise
        # leak into "rgba(...,nan)" and make Plotly raise ValueError).
        peak = max((abs(v) for v in vals), default=0.0) or 1.0
    out = []
    for v in vals:
        r, g, b = _hex_to_rgb(_TOKENS["bull"] if v >= 0 else _TOKENS["bear"])
        a = 0.35 + 0.65 * min(abs(v) / peak, 1.0)
        out.append(f"rgba({r},{g},{b},{a:.2f})")
    return out


def finite_abs_peak(*series) -> float | None:
    """Largest finite |value| across the given series, coercing object dtype
    (e.g. Postgres NULL → None) to numeric. Returns None when nothing is
    finite so callers can render an empty state instead of a NaN chart."""
    parts = [pd.to_numeric(pd.Series(s), errors="coerce") for s in series]
    if not parts:
        return None
    peak = pd.concat(parts).abs().max()
    return float(peak) if pd.notna(peak) and math.isfinite(peak) else None


def rdylgn_bg(val: float, vmin: float, vmax: float) -> str:
    """Red-Yellow-Green cell background for a pandas Styler, stdlib-only.

    FIX BT1: pandas' own Styler.background_gradient() requires matplotlib
    as an optional dependency — it isn't in requirements.txt (nothing else
    in this app needs it), so every page that called it crashed with an
    ImportError on Streamlit Cloud despite working fine in local testing
    (matplotlib happened to be present in the dev sandbox, masking it).
    Shared here rather than duplicated per-page since three separate pages
    (08_backtest.py, 05_market_overview.py, 03_my_portfolio.py) each had
    their own background_gradient() call and would each hit the same crash.

    Usage: df.style.map(lambda v: rdylgn_bg(v, df[col].min(), df[col].max()),
    subset=[col]) — same visual result as background_gradient(cmap="RdYlGn").
    """
    if pd.isna(val) or vmax == vmin:
        return ""
    t = max(0.0, min(1.0, (val - vmin) / (vmax - vmin)))
    if t < 0.5:
        lt = t / 0.5
        rgb = (220 + (230 - 220) * lt, 50 + (220 - 50) * lt, 50 + (60 - 50) * lt)
    else:
        lt = (t - 0.5) / 0.5
        rgb = (230 + (50 - 230) * lt, 220 + (180 - 220) * lt, 60 + (90 - 60) * lt)
    r, g, b = (int(c) for c in rgb)
    return f"background-color: rgb({r},{g},{b}); color: #111"


@st.cache_data(ttl=5, show_spinner=False)        # 5-second freshness for live feel
def _index_strip_data():
    """Live value + day-change % for each Nifty index via Yahoo chart meta."""
    import json, urllib.parse, urllib.request
    try:
        from data.fetcher import _get_yf_crumb
        _opener, _crumb = _get_yf_crumb()
    except Exception as _e:
        _log.debug("chart_helpers.%s degraded: %s", "_index_strip_data", _e)
        _opener, _crumb = None, ""
    _qs = f"&crumb={urllib.parse.quote(_crumb)}" if _crumb else ""
    _open = _opener.open if _opener else urllib.request.urlopen
    out = []
    for label, sym in _INDEX_STRIP:
        try:
            url = (f"https://query1.finance.yahoo.com/v8/finance/chart/"
                   f"{urllib.parse.quote(sym)}?interval=1d&range=5d{_qs}")
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0", "Accept": "application/json"})
            with _open(req, timeout=6) as r:
                meta = json.loads(r.read())["chart"]["result"][0]["meta"]
            price = meta.get("regularMarketPrice")
            prev  = meta.get("chartPreviousClose") or meta.get("previousClose")
            if price and prev:
                out.append((label, float(price), (float(price) / float(prev) - 1) * 100))
        except Exception as _e:
            _log.debug("chart_helpers.%s degraded: %s", "_index_strip_data", _e)
            continue
    return out


_INDEX_RAIL = [
    ("Nifty 50", "^NSEI"), ("Bank Nifty", "^NSEBANK"),
    ("Nifty Midcap 50", "^NSEMDCP50"), ("India VIX", "^INDIAVIX"),
]


@st.cache_data(ttl=30, show_spinner=False)
def index_rail_data() -> list:
    """Live Ticker right rail: [{label, price, chg, chg_pct, spark}] per index.

    ``spark`` is the latest session's 5-minute closes (Yahoo chart API, the
    same crumb session _index_strip_data uses). An index that fails is left
    out rather than shown as zero.
    """
    import json
    import urllib.parse
    import urllib.request
    try:
        from data.fetcher import _get_yf_crumb
        _opener, _crumb = _get_yf_crumb()
    except Exception as _e:
        _log.debug("chart_helpers.index_rail_data crumb unavailable: %s", _e)
        _opener, _crumb = None, ""
    _qs = f"&crumb={urllib.parse.quote(_crumb)}" if _crumb else ""
    _open = _opener.open if _opener else urllib.request.urlopen
    out = []
    for label, sym in _INDEX_RAIL:
        try:
            url = (f"https://query1.finance.yahoo.com/v8/finance/chart/"
                   f"{urllib.parse.quote(sym)}?interval=5m&range=1d{_qs}")
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0", "Accept": "application/json"})
            with _open(req, timeout=6) as r:
                res = json.loads(r.read())["chart"]["result"][0]
            meta = res["meta"]
            price = meta.get("regularMarketPrice")
            prev = meta.get("chartPreviousClose") or meta.get("previousClose")
            if not (price and prev):
                continue
            closes = [float(c) for c in (res["indicators"]["quote"][0].get("close") or [])
                      if c is not None]
            out.append({"label": label, "price": float(price),
                        "chg": float(price) - float(prev),
                        "chg_pct": (float(price) / float(prev) - 1) * 100,
                        "spark": closes})
        except Exception as _e:
            _log.debug("chart_helpers.index_rail_data %s degraded: %s", sym, _e)
            continue
    return out


@st.cache_data(ttl=30, show_spinner=False)
def _ticker_tape_data():
    _names = ["RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "ICICIBANK.NS", "INFY.NS",
              "SBIN.NS", "BHARTIARTL.NS", "ITC.NS", "LT.NS", "AXISBANK.NS",
              "MARUTI.NS", "TMCV.NS", "SUNPHARMA.NS", "TITAN.NS"]
    try:
        from utils.live_price import get_live_prices_batch
        raw = get_live_prices_batch(_names, max_workers=10)
    except Exception as _e:
        _log.debug("chart_helpers.%s degraded: %s", "_ticker_tape_data", _e)
        raw = {}
    out = []
    for t in _names:
        q = raw.get(t)
        if isinstance(q, dict) and q.get("price"):
            out.append((t.replace(".NS", ""), float(q["price"]), float(q.get("chg_pct", 0.0))))
    return out


def tick_pulse_tracker(key: str):
    """Track one-shot .tick-pulse-{up,down} halos on values that tick between reruns.

    Returns a ``(pulse_cls, commit)`` pair. ``pulse_cls(symbol, value)`` returns
    ``" tick-pulse-up"`` / ``" tick-pulse-down"`` / ``""`` — never pulses on first
    sight of a symbol (nothing to compare) or when the value is unchanged. Callers
    concatenate the returned class onto the outer card ``<div class="...">``. After
    the render loop, ``commit()`` freezes only the symbols seen this pass into
    session state, so symbols that drop out of the visible set (e.g. a stock that
    stops being a top-5 mover) can't accumulate stale prev-values indefinitely.

    Session-state schema: st.session_state[key] is a ``{symbol: last_value}`` dict.
    Pair this with the CSS classes defined in ``design.py`` (see ``UX3 · live-tick
    one-shot pulse``). The pattern mirrors ``_live_top_bar``'s inline impl for the
    top-bar chips — extracted here so other surfaces can adopt it without duping
    the diff logic.
    """
    _prev = st.session_state.setdefault(key, {})
    _new: dict = {}

    def _pulse_cls(symbol, value) -> str:
        try:
            _v = float(value)
        except (TypeError, ValueError):
            return ""
        _new[symbol] = _v
        _p = _prev.get(symbol)
        if _p is None or _p == _v:
            return ""
        return " tick-pulse-up" if _v > _p else " tick-pulse-down"

    def _commit() -> None:
        st.session_state[key] = _new

    return _pulse_cls, _commit


@st.fragment(run_every="5s")     # auto-updates ONLY this bar every 5 s, no page reload
def _live_top_bar():
    try:
        _chips = ""
        try:
            from utils.vix import get_india_vix_regime as _ltb_vix
            _vinfo = _ltb_vix()
            _vv = _vinfo.get("vix")
            if _vv:
                _vcol = "#00d4aa" if _vv < 16 else "#ff9500" if _vv < 22 else "#ff4757"
                _chips += (
                    f'<div style="background:#0d1526;border:1px solid rgba(255,255,255,.05);'
                    f'border-left:3px solid {_vcol};border-radius:8px;padding:6px 12px;min-width:96px">'
                    f'<div style="font-size:9px;color:#4a5568;letter-spacing:.6px;font-weight:600">INDIA VIX</div>'
                    f'<div style="font-size:14px;font-weight:700;color:{_vcol}">{_vv:.1f} '
                    f'<span style="font-size:10px;color:#8899bb">{_vinfo.get("regime","").title()}</span></div></div>'
                )
        except Exception as _e:
            _log.debug("chart_helpers.%s degraded: %s", "_live_top_bar", _e)
            pass
        try:
            from utils.market_hours import market_status as _ltb_ms
            _msd = _ltb_ms()
            _scol = "#00d4aa" if _msd.get("is_open") else "#ff4757"
            _chips += (
                f'<div style="background:#0d1526;border:1px solid rgba(255,255,255,.05);'
                f'border-left:3px solid {_scol};border-radius:8px;padding:6px 12px;min-width:110px">'
                f'<div style="font-size:9px;color:#4a5568;letter-spacing:.6px;font-weight:600">MARKET</div>'
                f'<div style="font-size:13px;font-weight:700;color:{_scol}">{_msd.get("status","")}</div></div>'
            )
        except Exception as _e:
            _log.debug("chart_helpers.%s degraded: %s", "_live_top_bar", _e)
            pass

        _idx = _index_strip_data()
        # UX3 · live-tick pulse — remember last-seen value per index label
        # in session state so the next rerun can compare and fire a one-shot
        # halo on the chip when the value moves. Bounded to just the labels
        # we render this pass so stale entries can't accumulate.
        _prev = st.session_state.setdefault("_topbar_prev_vals", {})
        _new_prev = {}
        for _lbl, _val, _chg in (_idx or []):
            _c = "#00d4aa" if _chg >= 0 else "#ff4757"
            _a = "▲" if _chg >= 0 else "▼"
            # F7b · a11y — the chip is primary data (indices strip), so it
            # should be announced with a semantic label rather than hidden.
            # The arrow glyph is decorative once the direction is in the label.
            _dir = "up" if _chg >= 0 else "down"
            _aria = f"{_lbl}: {_val:,.0f}, {_dir} {abs(_chg):.2f} percent"
            # UX3 tick class — only when there's a previous value AND it
            # differs. First render never pulses (nothing to compare against).
            _tick_cls = ""
            _prev_val = _prev.get(_lbl)
            if _prev_val is not None and _val != _prev_val:
                _tick_cls = " tick-pulse-up" if _val > _prev_val else " tick-pulse-down"
            _new_prev[_lbl] = _val
            _chips += (
                f'<div class="topbar-chip{_tick_cls}" role="group" aria-label="{_aria}" '
                f'style="background:#0d1526;border:1px solid rgba(255,255,255,.05);'
                f'border-left:3px solid {_c};border-radius:8px;padding:6px 12px;min-width:118px">'
                f'<div style="font-size:9px;color:#4a5568;letter-spacing:.6px;font-weight:600">{_lbl}</div>'
                f'<div style="font-size:14px;font-weight:700;color:#f0f4ff">{_val:,.0f} '
                f'<span style="font-size:11px;color:{_c}"><span aria-hidden="true">{_a}</span>{abs(_chg):.2f}%</span></div></div>'
            )
        st.session_state["_topbar_prev_vals"] = _new_prev
        if _chips:
            st.markdown(
                f'<div role="region" aria-label="Live market indices" '
                f'style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px">{_chips}</div>',
                unsafe_allow_html=True,
            )
    except Exception as _e:
        _log.debug("chart_helpers.%s degraded: %s", "_live_top_bar", _e)
        pass

    try:
        _tt = _ticker_tape_data()
        if _tt:
            _tt_items = ""
            for _sym, _px, _chg in _tt:
                _tc = "#00d4aa" if _chg >= 0 else "#ff4757"
                _ta = "▲" if _chg >= 0 else "▼"
                _tt_items += (
                    f'<span style="margin:0 22px">'
                    f'<b style="color:#f0f4ff">{_sym}</b> '
                    f'<span style="color:#c8d0e0">₹{_px:,.2f}</span> '
                    f'<span style="color:{_tc}">{_ta}{abs(_chg):.2f}%</span></span>'
                )
            # F7b · a11y — the auto-scrolling ticker is decorative visual
            # cruft for screen readers (the same tickers are available in
            # structured form on Market Live). aria-hidden lets AT skip it
            # entirely rather than announce a garbled 20-symbol scroll.
            st.markdown(
                f'<div class="ticker-wrap" aria-hidden="true"><div class="ticker-content">{_tt_items}{_tt_items}</div></div>',
                unsafe_allow_html=True,
            )
    except Exception as _e:
        _log.debug("chart_helpers.%s degraded: %s", "_live_top_bar", _e)
        pass


_INDEX_CONSTITUENTS = {
    "NIFTY 50":     ("universe", "nifty50"),
    "BANK NIFTY":   ("sector",   "Banking"),
    "NIFTY IT":     ("sector",   "IT"),
    "NIFTY AUTO":   ("sector",   "Auto"),
    "NIFTY FMCG":   ("sector",   "FMCG"),
    "NIFTY PHARMA": ("sector",   "Pharma"),
    "NIFTY METAL":  ("sector",   "Metal"),
    "NIFTY ENERGY": ("sector",   "Energy"),
}

@st.cache_data(ttl=60, show_spinner=False)
def _index_constituent_rows(index_label: str):
    """Return [(ticker, price, chg%), …] for an index's constituents (live)."""
    try:
        kind, key = _INDEX_CONSTITUENTS.get(index_label, ("universe", "nifty50"))
        if kind == "universe":
            from data.universe import get_universe
            tickers = get_universe(key)
        else:
            from data.universe import get_tickers_by_sector
            tickers = get_tickers_by_sector(key)
        from utils.live_price import get_live_prices_batch
        raw = get_live_prices_batch(list(tickers), max_workers=12)
        rows = []
        for t in tickers:
            q = raw.get(t)
            if isinstance(q, dict) and q.get("price"):
                rows.append((t.replace(".NS", ""), float(q["price"]), float(q.get("chg_pct", 0.0))))
        rows.sort(key=lambda x: -x[2])   # biggest gainers first
        return rows
    except Exception as _e:
        _log.debug("chart_helpers.%s degraded: %s", "_index_constituent_rows", _e)
        return []


def render_top_bar():
    """Live indices/ticker bar + index explorer. Call at top of each page."""
    try:
        _live_top_bar()
    except Exception as e:
        _log.debug("render_top_bar: live top bar failed (cosmetic, page continues): %s", e)
    with st.expander("📑 Open an index — see its stocks & day changes", expanded=False):
        _ix_pick = st.selectbox("Index", list(_INDEX_CONSTITUENTS.keys()),
                                key="ix_explorer_sel", label_visibility="collapsed")
        with st.spinner(f"Loading {_ix_pick} stocks…"):
            _ix_rows = _index_constituent_rows(_ix_pick)
        if _ix_rows:
            _ix_up = sum(1 for _, _, c in _ix_rows if c >= 0)
            st.caption(f"**{_ix_pick}** — {len(_ix_rows)} stocks · {_ix_up} up / {len(_ix_rows)-_ix_up} down")
            _ix_html = '<div style="display:flex;flex-wrap:wrap;gap:6px">'
            for _nm, _px, _ch in _ix_rows:
                _cc = "#00d4aa" if _ch >= 0 else "#ff4757"
                _ar = "▲" if _ch >= 0 else "▼"
                _ix_html += (
                    f'<div style="background:#0d1526;border:1px solid rgba(255,255,255,.05);'
                    f'border-left:3px solid {_cc};border-radius:7px;padding:6px 11px;min-width:120px">'
                    f'<div style="font-size:12px;font-weight:700;color:#f0f4ff">{_nm}</div>'
                    f'<div style="font-size:12px;color:#c8d0e0">₹{_px:,.2f} '
                    f'<span style="color:{_cc};font-weight:600">{_ar}{abs(_ch):.2f}%</span></div></div>'
                )
            _ix_html += '</div>'
            st.markdown(_ix_html, unsafe_allow_html=True)
        else:
            st.caption("Couldn't load constituents — try again in a moment.")
