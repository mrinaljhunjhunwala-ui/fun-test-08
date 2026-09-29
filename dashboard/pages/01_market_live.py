"""Market Live - NSE Smart Investor (multipage page; body verbatim from app.py).

LAYOUT (2026-09-29): artboard 06 of the 2026-09-12 "proposed layout" mockup.
A dense live-price table (LTP, change, volume, day range, VWAP) with an index
rail beside it. The table and rail are one @st.fragment that re-runs every
30 s while NSE is open, so rows flash when their price ticks; the old
whole-page <meta refresh> started a new session on every reload, so nothing
could be compared between ticks. Market-wide breadth, the sector heatmap, a
mover drill-down and news follow below.
"""
import os, sys
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
import datetime as _dt
import logging

import pandas as pd
import streamlit as st

from dashboard.shared.design import apply_design
from dashboard.shared.nav import render_sidebar
from dashboard.shared.cache import get_display_name
from dashboard.shared.chart_helpers import (
    PLOT_COLORS,
    diverging_colors,
    finite_abs_peak,
    index_rail_data,
    render_top_bar,
    tick_pulse_tracker,
)
from dashboard.shared.ui_components import (
    empty_state,
    gate_strip,
    mini_index,
    section_header,
    ticker_table,
)

_log = logging.getLogger("dashboard.market_live")


def _sector_of(ticker: str) -> str:
    try:
        from data.universe import get_sector
        return get_sector(ticker)
    except Exception:
        return "Other"

apply_design()
render_sidebar(current="Live Ticker")
render_top_bar()

# ───────────────────────── page body (de-indented from app.py) ─────────────────────────
from utils.market_hours import market_status as _ms_fn  # noqa: E402
from utils.news import get_market_news, get_stock_news  # noqa: E402

_ms = _ms_fn()
_LIVE = bool(_ms.get("is_open"))
_TICK_S = 30   # table + rail refresh cadence while NSE is open

# ── Header: title + session meta + refresh ─────────────────────────────────
col_h1, col_h2 = st.columns([5, 1], vertical_alignment="bottom")
with col_h1:
    _dot = ("var(--bull)" if _LIVE else "var(--amber)" if _ms.get("is_pre") else "var(--bear)")
    st.markdown(
        '<h1 class="page-title-serif">Live <em>Ticker</em></h1>'
        f'<div class="lt-meta"><span><span class="lt-dot" style="background:{_dot}"></span>'
        f'NSE {str(_ms["status"]).lower()} · {_ms["time_ist"]}</span>'
        f'<span>{_ms["day"]}</span><span>{_ms["detail"]}</span></div>',
        unsafe_allow_html=True,
    )
with col_h2:
    # FIX MKT1: was a blanket st.cache_data.clear() — wiped every other
    # page's cached data (Top Picks, watchlist scans, etc.) along with this
    # page's own snapshot. "Refresh Now" only means this page's data.
    # _load_nifty_snapshot is defined further down this script (Streamlit
    # pages run top-to-bottom on every interaction), so the click is just
    # captured here and the actual .clear() happens right before the
    # function is first called below, once it actually exists.
    _mkt_refresh_clicked = st.button("🔄 Refresh Now")

# ── Fetch broad NSE prices — Angel One (priority) → Yahoo → NSE fallback ──
@st.cache_data(ttl=60 if _ms["is_open"] else 3600, show_spinner=False)
def _load_nifty_snapshot():
    """
    Cloud-safe NSE broad snapshot (Nifty Total Market ~750 stocks).
    Priority: Angel One batch quotes → Yahoo Finance JSON API.
    """
    from data.universe import get_universe as _gu

    tickers_list = _gu("niftytotalmarket")   # ~750 liquid NSE stocks
    raw: dict = {}
    _source = "Yahoo Finance"

    # Tier 1: Angel One (real-time, preferred)
    import logging as _ml_log
    try:
        from data.angel_fetcher import (
            is_configured as _aoc,
            get_batch_quotes as _ao_batch,
        )
        if _aoc():
            _ao_raw = _ao_batch(tickers_list)
            if _ao_raw and sum(1 for v in _ao_raw.values() if v) > 10:
                raw     = _ao_raw
                _source = "Angel One (real-time)"
    except Exception as _ao_e:
        _ml_log.getLogger("dashboard.market_live").debug("Angel One batch fetch failed: %s", _ao_e)

    # Tier 2: Yahoo Finance JSON
    if not raw:
        from utils.live_price import get_live_prices_batch
        raw     = get_live_prices_batch(tickers_list, max_workers=20)
        _source = "Yahoo Finance"


    rows = []
    for t in tickers_list:
        q = raw.get(t)
        if not isinstance(q, dict) or not q.get("price"):
            continue
        try:
            chg = q.get("chg_pct", (q["price"] / q["prev_close"] - 1) * 100
                         if q.get("prev_close", 0) > 0 else 0.0)
            rows.append({
                "ticker":     t,
                "name":       get_display_name(t),
                "price":      q["price"],
                "prev_close": q.get("prev_close", q["price"]),
                "chg_pct":    chg,
                "vol_ratio":  1.0,
                "volume":     q.get("volume", 0),
                "high":       q.get("high") or None,
                "low":        q.get("low") or None,
                "vwap":       q.get("vwap") or None,
                "_source":    _source,
            })
        except Exception as _row_e:
            _ml_log.getLogger("dashboard.market_live").debug("Skipping ticker %s in snapshot: %s", t, _row_e)
            continue

    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).sort_values("chg_pct", ascending=False)
    return df


@st.cache_data(ttl=_TICK_S if _LIVE else 600, show_spinner=False)
def _load_view_quotes(tickers: tuple) -> dict:
    """Quotes for the table's current view (Nifty 50 / watchlist): a small
    batch on its own short cache so the table can tick without re-pulling
    the ~750-stock snapshot."""
    from utils.live_price import get_live_prices_batch
    return get_live_prices_batch(list(tickers), max_workers=12, max_wait_seconds=25)


def _board_rows(view: str) -> tuple:
    """(rows, source_label, requested) for the table. Rows are dicts in the
    shape ui_components.ticker_table reads, sorted by % change."""
    if view == "Top movers":
        # The ~750-stock snapshot (60 s cache while open). Only this view
        # waits on it; the default Nifty 50 view never does.
        with st.spinner("Loading the market-wide snapshot (~750 stocks)…"):
            snap = _load_nifty_snapshot()
        if snap.empty:
            return [], "—", 0
        pool = pd.concat([snap.head(15), snap.tail(15)]).drop_duplicates(subset=["ticker"])
        src = str(pool["_source"].iloc[0]) if "_source" in pool.columns else "Yahoo Finance"
        recs = [
            {"ticker": r["ticker"], "price": r["price"], "prev_close": r["prev_close"],
             "chg_pct": r["chg_pct"], "volume": r.get("volume"), "high": r.get("high"),
             "low": r.get("low"), "vwap": r.get("vwap")}
            for _, r in pool.iterrows()
        ]
        requested = len(pool)
    else:
        if view == "My watchlist":
            tickers = [t if str(t).endswith(".NS") else f"{t}.NS"
                       for t in st.session_state.get("watchlist", []) or []]
        else:
            from data.universe import get_universe
            tickers = get_universe("nifty50")
        requested = len(tickers)
        if not tickers:
            return [], "—", 0
        raw = _load_view_quotes(tuple(tickers))
        recs = [dict(raw[t], ticker=t) for t in tickers
                if isinstance(raw.get(t), dict) and raw[t].get("price")]
        # angel_fetcher's batch rows carry net_chg; the Yahoo tiers don't.
        src = "Angel One" if any("net_chg" in r for r in recs) else "Yahoo Finance"
    rows = []
    for q in recs:
        try:
            px = float(q["price"])
            prev = float(q.get("prev_close") or px)
        except (TypeError, ValueError):
            continue
        pct = q.get("chg_pct")
        rows.append({
            "ticker": q["ticker"], "sym": q["ticker"].replace(".NS", ""),
            "sub": _sector_of(q["ticker"]),
            "price": px, "chg": px - prev,
            "chg_pct": float(pct) if pct is not None else ((px / prev - 1) * 100 if prev else 0.0),
            "volume": q.get("volume"), "high": q.get("high") or None,
            "low": q.get("low") or None, "vwap": q.get("vwap") or None,
        })
    rows.sort(key=lambda r: r["chg_pct"], reverse=True)
    return rows, src, requested


@st.fragment(run_every=_TICK_S if _LIVE else None)
def _live_board():
    """Ticker table + index rail. Re-runs on its own every _TICK_S seconds
    while NSE is open; each row flashes once when its LTP changed since the
    previous pass (tick_pulse_tracker keeps the last-seen prices)."""
    _view = st.segmented_control(
        "Table view", ["Nifty 50", "Top movers", "My watchlist"],
        default="Nifty 50", key="lt_view", label_visibility="collapsed",
    ) or "Nifty 50"
    _rows, _src, _requested = _board_rows(_view)
    _pulse, _commit = tick_pulse_tracker(f"_lt_prev::{_view}")
    for _r in _rows:
        _cls = _pulse(_r["ticker"], _r["price"])
        _r["flash"] = "up" if "up" in _cls else "down" if "down" in _cls else ""
    _commit()

    _tbl, _rail = st.columns([3.4, 1], gap="medium")
    with _tbl:
        if _rows:
            st.markdown(ticker_table(_rows), unsafe_allow_html=True)
        elif _view == "My watchlist" and not _requested:
            st.markdown(empty_state("Your watchlist is empty",
                                    hint="Add tickers from the sidebar or any Analyze page."),
                        unsafe_allow_html=True)
        else:
            st.markdown(empty_state("Prices unavailable",
                                    hint="No quote source answered. The table retries on the "
                                         "next refresh."), unsafe_allow_html=True)
    with _rail:
        _idx = []
        try:
            _idx = index_rail_data()
        except Exception as _ir_e:
            _log.debug("index rail failed: %s", _ir_e)
        for _i in _idx:
            st.markdown(mini_index(_i["label"], _i["price"], _i["chg"], _i["chg_pct"],
                                   _i.get("spark"), invert=_i["label"] == "India VIX"),
                        unsafe_allow_html=True)
        _now = _dt.datetime.now().strftime("%H:%M:%S")
        _cad = f"every {_TICK_S} s" if _LIVE else "paused · closed"
        st.markdown(
            '<div class="mini-index sunk"><div class="mi-name">Feed</div>'
            f'<div class="mi-kv"><div><div class="mi-name">Source</div>{_src.split(" (")[0]}</div>'
            f'<div><div class="mi-name">Priced</div>{len(_rows)} / {_requested}</div></div>'
            f'<div class="mi-kv"><div><div class="mi-name">Updated</div>{_now}</div>'
            f'<div><div class="mi-name">Refresh</div>{_cad}</div></div></div>',
            unsafe_allow_html=True,
        )


# Now that both loaders exist, honor an earlier "Refresh Now" click
# (captured above, where the button renders). FIX MKT1: clears only this
# page's caches, the snapshot and the table's quotes.
if _mkt_refresh_clicked:
    _load_nifty_snapshot.clear()
    _load_view_quotes.clear()
    st.rerun()

# Table first: it needs ~50 quotes, the breadth snapshot below needs ~750.
_live_board()

with st.spinner("Loading NSE market snapshot (~750 stocks)…"):
    snap = _load_nifty_snapshot()

if snap.empty:
    st.warning("Could not fetch the market-wide snapshot. Try again in 30 seconds.")
else:
    _src = snap["_source"].iloc[0] if "_source" in snap.columns else "Yahoo Finance"

    # ── Market-wide breadth (the whole ~750-stock snapshot) ──────────────
    adv = int((snap["chg_pct"] > 0).sum())
    dec = int((snap["chg_pct"] < 0).sum())
    avg_chg = snap["chg_pct"].mean()
    # Rounded before the read so the label agrees with the printed share.
    _breadth_pct = round(adv / max(adv + dec, 1) * 100)
    if _breadth_pct >= 65:
        _b_read, _b_col = "Broad strength", "var(--bull)"
    elif _breadth_pct <= 35:
        _b_read, _b_col = "Broad weakness", "var(--bear)"
    else:
        _b_read, _b_col = "Mixed", "var(--amber)"
    _chg_col = "var(--bull)" if avg_chg >= 0 else "var(--bear)"
    st.markdown(section_header("Market breadth", f"{_src} · Nifty Total Market"),
                unsafe_allow_html=True)
    st.markdown(gate_strip([
        ("Stocks priced", f"{len(snap)}", "of ~750 tracked"),
        ("Advances / declines", f"{adv} / {dec}", f"{adv - dec:+d} net"),
        ("Average move", f'<span style="color:{_chg_col}">{avg_chg:+.2f}%</span>', "equal-weighted"),
        ("Share advancing", f"{_breadth_pct:.0f}%", "of stocks that moved"),
        ("Breadth read", f'<span style="color:{_b_col}">{_b_read}</span>',
         "≥65% up strong · ≤35% weak"),
    ]), unsafe_allow_html=True)

    top5 = snap.head(5)
    bot5 = snap.tail(5).iloc[::-1]

    # ── P2 · Sector heatmap — diverging palette (hue = sign, alpha = size) ──
    try:
        _sec = snap.assign(sector=snap["ticker"].map(_sector_of))
        _sec = _sec[_sec["sector"] != "Other"]
        _sec_agg = (_sec.groupby("sector")
                        .agg(chg=("chg_pct", "mean"), n=("ticker", "count"))
                        .reset_index())
        if not _sec_agg.empty:
            import plotly.graph_objects as _go
            from dashboard.shared.tokens import COLORS as _TOK
            _sec_agg["chg"] = pd.to_numeric(_sec_agg["chg"], errors="coerce")
            _peak = max(finite_abs_peak(_sec_agg["chg"]) or 0.0, 1.0)
            _fig = _go.Figure(_go.Treemap(
                labels=_sec_agg["sector"],
                parents=[""] * len(_sec_agg),
                values=_sec_agg["n"],
                customdata=_sec_agg[["chg", "n"]].values,
                marker=dict(colors=diverging_colors(_sec_agg["chg"], full_at=_peak),
                            line=dict(width=2, color=_TOK["ground"])),
                # Light ink on every tile: the alpha-scaled fills sit on the
                # dark ground, so even the strongest tile is dark enough for
                # --ink text (Plotly's auto-contrast picked near-black labels
                # that vanished on the darker, low-magnitude tiles).
                texttemplate="%{label}",
                textfont=dict(color=_TOK["ink"], size=12),
                textposition="middle center",
                hovertemplate="<b>%{label}</b><br>%{customdata[0]:+.2f}% avg"
                              " · %{customdata[1]} stocks<extra></extra>",
            ))
            _fig.update_layout(height=320, margin=dict(l=0, r=0, t=0, b=0),
                               paper_bgcolor="rgba(0,0,0,0)")
            st.markdown(section_header("Sector heatmap", "average move per sector"),
                        unsafe_allow_html=True)
            st.plotly_chart(_fig, width="stretch")
            st.caption("Tile size = stocks tracked · colour hue = direction, "
                       "intensity = size of the average move. Hover for %.")
    except Exception as _hm_e:
        logging.getLogger("dashboard.market_live").debug("sector heatmap failed: %s", _hm_e)

    @st.cache_data(ttl=300, show_spinner=False)
    def _explain_mover(ticker: str, chg_pct: float, vol_ratio: float) -> list:
        """Generate 2-4 plain-English reasons why a stock is moving."""
        reasons = []
        try:
            import math
            from data.fetcher import fetch_single
            df = fetch_single(ticker, period="3mo")
            df = df.dropna(subset=["Close"])
            if len(df) < 20:
                return reasons

            last    = df.iloc[-1]
            close   = float(last["Close"])
            high52  = float(df["High"].max())
            low52   = float(df["Low"].min())
            sma20   = df["Close"].rolling(20).mean().iloc[-1]
            sma50   = df["Close"].rolling(50).mean().iloc[-1] if len(df) >= 50 else close

            # RSI
            delta   = df["Close"].diff()
            gain    = delta.clip(lower=0).rolling(14).mean()
            loss    = (-delta.clip(upper=0)).rolling(14).mean()
            rs      = gain / loss
            rsi     = float((100 - 100 / (1 + rs)).iloc[-1])

            # Volume
            if vol_ratio >= 2.5:
                reasons.append(f"Massive volume surge ({vol_ratio:.1f}x average) — likely institutional activity")
            elif vol_ratio >= 1.5:
                reasons.append(f"Above-average volume ({vol_ratio:.1f}x) — elevated interest")

            # RSI
            if rsi > 72:
                reasons.append(f"RSI overbought ({rsi:.0f}) — strong momentum, watch for pullback")
            elif rsi < 30:
                reasons.append(f"RSI oversold ({rsi:.0f}) — heavy selling, potential bounce zone")
            elif 50 < rsi < 65 and chg_pct > 0:
                reasons.append(f"RSI healthy ({rsi:.0f}) — momentum building, not yet overbought")

            # 52-week position
            pct_from_high = (high52 - close) / high52 * 100
            pct_from_low  = (close - low52) / low52 * 100
            if pct_from_high < 2:
                reasons.append("At 52-week high — breakout territory")
            elif pct_from_low < 3:
                reasons.append("Near 52-week low — support zone / turnaround candidate")

            # Trend
            if not math.isnan(sma20) and not math.isnan(sma50):
                if close > sma20 > sma50:
                    reasons.append("Above SMA20 and SMA50 — uptrend intact")
                elif close < sma20 < sma50:
                    reasons.append("Below SMA20 and SMA50 — downtrend pressure")

            # News
            news = get_stock_news(ticker, max_articles=1)
            if news:
                h = news[0]["title"][:90]
                s = news[0]["sentiment"]
                icon = "📰" if s == "neutral" else ("🟢" if s == "positive" else "🔴")
                reasons.append(f"{icon} News: {h}…")

        except Exception as _expl_e:
            import logging as _expl_log
            _expl_log.getLogger("dashboard.market_live").debug("_explain_mover(%s) failed: %s", ticker, _expl_e)
        return reasons if reasons else ["No specific technical catalyst detected"]

    # ── Drill into any mover (one panel, no nested-expander clutter) ───────
    _drill_pool = pd.concat([top5, bot5]).drop_duplicates(subset=["ticker"])
    _drill_opts = ["— pick a stock —"] + [
        f"{r['ticker'].replace('.NS','')}  ({r['chg_pct']:+.2f}%)"
        for _, r in _drill_pool.iterrows()
    ]
    _drill_sel = st.selectbox("🔍 Drill into a mover", _drill_opts, key="ml_drill_sel")
    if _drill_sel != "— pick a stock —":
        _dt_label = _drill_sel.split("  (")[0].strip()
        _dt_full  = _dt_label if _dt_label.endswith(".NS") else _dt_label + ".NS"
        _drow = _drill_pool[_drill_pool["ticker"].str.replace(".NS", "") == _dt_label]
        if not _drow.empty:
            _dr = _drow.iloc[0]
            _dchg = _dr["chg_pct"]
            _dd1, _dd2, _dd3 = st.columns(3)
            _dd1.metric("Live Price", f"₹{_dr['price']:,.2f}", f"{_dchg:+.2f}%",
                        delta_color="normal" if _dchg >= 0 else "inverse")
            _dd2.metric("Prev Close", f"₹{_dr.get('prev_close', _dr['price']):,.2f}")
            _dd3.metric("Company", str(_dr.get("name", ""))[:20])
            with st.spinner("Reading the chart…"):
                for _rs in _explain_mover(_dt_full, _dchg, 1.0):
                    st.markdown(f"• {_rs}")
            _da, _db, _dc = st.columns(3)
            if _da.button("📊 Analyze", key=f"ml_an_{_dt_full}", width="stretch"):
                # FIX NAV1: canonical analyze_ticker hand-off (see
                # 04_analyze_stock.py FIX A8) instead of manual_ticker_input +
                # last_analyzed — traced through and confirmed the old path
                # did work here, but standardizing every "Analyze" button in
                # the app on one tested contract removes a whole class of
                # future navigation bugs rather than leaving three parallel
                # mechanisms doing the same job.
                st.session_state["analyze_ticker"] = _dt_full
                st.session_state["_goto_page"] = "🔍 Analyze Stock"
                st.rerun()
            if _db.button("📝 Paper Trade", key=f"ml_pt_{_dt_full}", width="stretch"):
                st.session_state["_goto_page"] ="📂 Paper Trades"
                st.session_state["pt_prefill_ticker"] = _dt_full
                st.rerun()
            if _dc.button("＋ Watchlist", key=f"ml_wl_{_dt_full}", width="stretch"):
                if _dt_full not in st.session_state.get("watchlist", []):
                    st.session_state.setdefault("watchlist", []).append(_dt_full)
                st.toast(f"{_dt_label} added to watchlist ✓")

    # ── Full NSE snapshot table ────────────────────────────────────────────
    with st.expander(f"📋 Full NSE Snapshot ({len(snap)} stocks)", expanded=False):
        disp = snap[["name", "ticker", "price", "chg_pct"]].copy()
        disp.columns = ["Company", "Ticker", "Price (₹)", "Change %"]
        disp["Ticker"]    = disp["Ticker"].str.replace(".NS", "")
        disp["Price (₹)"] = disp["Price (₹)"].map("₹{:,.2f}".format)
        disp["Change %"]  = disp["Change %"].map("{:+.2f}%".format)
        st.dataframe(disp, hide_index=True, width="stretch", height=400)
        # DT1 + DT2 · movers table attribution.
        try:
            from dashboard.shared.ui_components import data_as_of as _ml_asof
            import datetime as _ml_dt
            _ml_when = _ml_dt.datetime.now().strftime("%H:%M IST")
            st.markdown(
                _ml_asof(_ml_when, source="yfinance",
                         ttl_hint="live-price cache 60 s"),
                unsafe_allow_html=True,
            )
        except Exception:
            pass

# ── Market News (multi-source, with source badges) ─────────────────────────
st.markdown(section_header("Latest market news", "multi-source · keyword sentiment"),
            unsafe_allow_html=True)
with st.spinner("Aggregating news from multiple sources…"):
    mkt_news = get_market_news(max_articles=14)

if mkt_news:
    _srcs = sorted({a.get("publisher", "") for a in mkt_news if a.get("publisher")})
    st.caption(f"🗞️ Aggregated from **{len(_srcs)} sources**: {', '.join(_srcs)}")
    # P2 · publisher identity chips cycle through PLOT_COLORS tokens
    # (no raw hex in pages).
    _src_palette = [PLOT_COLORS[k] for k in ("azure", "accent", "bull", "amber", "bear", "dim")]
    _src_color = {s: _src_palette[i % len(_src_palette)] for i, s in enumerate(_srcs)}
    for article in mkt_news:
        _s   = article["sentiment"]
        _sc  = ("var(--bull)" if _s == "positive"
                else "var(--bear)" if _s == "negative" else "var(--dim)")
        _si  = "▲" if _s == "positive" else "▼" if _s == "negative" else "•"
        _pub = article.get("publisher", "—")
        _pc  = _src_color.get(_pub, PLOT_COLORS["dim"])
        st.markdown(
            f'<div style="background:var(--surface);border:1px solid var(--hairline-soft);'
            f'border-left:3px solid {_sc};border-radius:8px;padding:10px 14px;margin-bottom:6px">'
            f'<span style="background:{_pc}22;color:{_pc};border:1px solid {_pc};border-radius:5px;'
            f'padding:1px 8px;font-size:10px;font-weight:700">{_pub}</span>'
            f'<span style="font-size:10px;color:var(--faint)">&nbsp; · {article["time"]} · '
            f'<span style="color:{_sc};font-weight:600">{_si} {_s}</span></span><br>'
            f'<a href="{article["link"]}" target="_blank" style="color:var(--ink);'
            f'text-decoration:none;font-size:14px;font-weight:600">{article["title"]}</a></div>',
            unsafe_allow_html=True,
        )
else:
    st.info("News temporarily unavailable — refresh in a moment.")
