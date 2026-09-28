"""Overview - NSE Smart Investor (merged: former Market Overview + Market Internals).

MERGE NOTE: this page replaces the two separate pages "Market Overview"
(pages/05_market_overview.py) and "Market Internals" (pages/09_market_internals.py).
All logic is unchanged from those two pages — just reorganized as tabs under
one page so related market-context views live in one place. After deploying
this file, delete dashboard/pages/09_market_internals.py (its content now
lives in the "🌍 Macro" and "📈 Breadth" tabs below) — nav.py has already
been updated to match.
"""
import os, sys
import logging

_log = logging.getLogger("dashboard.overview")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from dashboard.shared.design import apply_design
from dashboard.shared.nav import render_sidebar
from dashboard.shared.chart_helpers import (
    _ROOT,
    _NIFTY50_TICKERS,
    render_top_bar,
    load_macro_data,
    compute_market_breadth,
    rdylgn_bg,
)
from dashboard.shared.tokens import COLORS as _TK  # noqa: E402
from dashboard.shared.cache import load_vix_data

apply_design()
render_sidebar(current="Market Breadth")
render_top_bar()

st.markdown('<h1 class="page-title-serif">Market <em>Breadth</em></h1>', unsafe_allow_html=True)

st.caption("Market snapshot, macro context, and breadth — everything for a market read in one place")

_tab_snapshot, _tab_macro, _tab_breadth = st.tabs(
    ["📊 Snapshot", "🌍 Macro", "📈 Breadth"]
)

# ═══════════════════════════════════════════════════════════════════════════
# TAB 1 — SNAPSHOT (formerly Market Overview: VIX, sector rotation, movers)
# ═══════════════════════════════════════════════════════════════════════════
with _tab_snapshot:
    _mo_refresh_clicked = st.button("🔄 Refresh Snapshot Data", type="primary", key="ov_snap_refresh")
    if _mo_refresh_clicked:
        # FIX MKT2 (preserved): targeted cache clears only, never a blanket
        # st.cache_data.clear() — that would also wipe Top Picks/watchlist
        # scans on other pages.
        load_vix_data.clear()

    # ── India VIX section ──────────────────────────────────────────────
    with st.spinner("Loading VIX & Nifty…"):
        try:
            vix_df, nifty_df = load_vix_data()
            if (vix_df is None or nifty_df is None or len(vix_df) < 2 or len(nifty_df) < 2
                    or "Close" not in vix_df or "Close" not in nifty_df):
                raise LookupError("India VIX / Nifty data unavailable right now — data providers unreachable.")
            curr_vix  = float(vix_df["Close"].iloc[-1])
            prev_vix   = float(vix_df["Close"].iloc[-2])
            vix_chg    = (curr_vix / prev_vix - 1) * 100
            vix_52w_hi = float(vix_df["High"].max())
            vix_52w_lo = float(vix_df["Low"].min())
            vix_rank   = (curr_vix - vix_52w_lo) / max(vix_52w_hi - vix_52w_lo, 0.01) * 100
            curr_nifty = float(nifty_df["Close"].iloc[-1])
            nifty_chg  = float(nifty_df["Close"].pct_change().iloc[-1]) * 100

            if curr_vix < 12:    regime, reg_color = "Extreme Complacency", "var(--accent)"
            elif curr_vix < 16:  regime, reg_color = "Low Volatility",       "var(--bull)"
            elif curr_vix < 22:  regime, reg_color = "Normal",                "var(--azure)"
            elif curr_vix < 28:  regime, reg_color = "Elevated Fear",         "var(--amber)"
            elif curr_vix < 35:  regime, reg_color = "High Fear",             "var(--bear)"
            else:                regime, reg_color = "PANIC / Crisis",         "var(--violet)"

            if curr_vix < 15:   opt_str = "BUY options (cheap premium)"
            elif curr_vix < 22: opt_str = "SPREADS (balanced IV)"
            elif curr_vix < 28: opt_str = "SELL premium with spreads"
            else:               opt_str = "SELL wide spreads / long if conviction"

            if nifty_chg > 0 and vix_chg > 0:
                div_txt = "⚠️ Warning: Nifty ↑ + VIX ↑ — fragile rally"
            elif nifty_chg < 0 and vix_chg < 0:
                div_txt = "🟢 Nifty ↓ + VIX ↓ — oversold bounce watch"
            elif nifty_chg > 0 and vix_chg < 0:
                div_txt = "✅ Healthy rally — fear leaving market"
            else:
                div_txt = "✅ Normal correction — fear rising with selling"

            st.markdown(

                '<div class="t-h2" style="margin:14px 0 6px 0">'

                '🌡️ Fear Gauge — India VIX</div>',

                unsafe_allow_html=True,

            )
            st.markdown(
                f'<div style="background:{reg_color};padding:12px 18px;border-radius:10px;'
                f'color:var(--ground);font-weight:700;font-size:18px;text-align:center;">'
                f'VIX {curr_vix:.2f}  ({vix_chg:+.1f}% today)  —  {regime}  |  '
                f'Options regime: {opt_str}'
                f'</div>',
                unsafe_allow_html=True
            )
            st.markdown(f"**Divergence signal:** {div_txt}")

            v_col1, v_col2, v_col3, v_col4 = st.columns(4)
            v_col1.metric("India VIX",    f"{curr_vix:.2f}", f"{vix_chg:+.1f}%")
            v_col2.metric("VIX Rank",     f"{vix_rank:.0f}%  (52w)")
            v_col3.metric("Nifty 50",     f"{curr_nifty:,.0f}", f"{nifty_chg:+.2f}%")
            v_col4.metric("52w VIX Range",f"{vix_52w_lo:.1f} – {vix_52w_hi:.1f}")

            fig_vix = go.Figure()
            fig_vix.add_trace(go.Scatter(
                x=vix_df.index, y=vix_df["Close"],
                name="India VIX", line=dict(color=_TK["bear"], width=2),
                fill="tozeroy", fillcolor="rgba(255,107,107,0.1)",
            ))
            for lo, hi, clr, lbl in [
                (0, 12, "rgba(76,175,80,.12)", "Safe"),
                (12, 22, "rgba(255,193,7,.12)", "Normal"),
                (22, 28, "rgba(255,87,34,.12)", "Caution"),
                (28, 100, "rgba(156,39,176,.12)", "Fear"),
            ]:
                fig_vix.add_hrect(y0=lo, y1=hi, fillcolor=clr,
                                  annotation_text=lbl, annotation_position="left",
                                  line_width=0)
            fig_vix.update_layout(
                title="India VIX — 1 Year",
                template="nse_pro", height=300,
                xaxis_rangeslider_visible=False,
                margin=dict(l=0, r=0, t=40, b=0),
            )
            st.plotly_chart(fig_vix, width="stretch")

        except Exception as e:
            st.warning(f"VIX load error: {e}")

    # ── Sector Rotation ──────────────────────────────────────────────
    st.markdown("---")
    st.markdown(
        '<div class="t-h2" style="margin:14px 0 6px 0">'
        '🔄 Sector Momentum Heatmap</div>',
        unsafe_allow_html=True,
    )

    @st.cache_data(ttl=1800)
    def get_sector_data():
        from strategies.sector_rotation import compute_sector_scores
        return compute_sector_scores(period="1y")

    if _mo_refresh_clicked:
        get_sector_data.clear()

    with st.spinner("Computing sector scores…"):
        try:
            scores = get_sector_data()
            if not scores.empty:
                s_col1, s_col2 = st.columns([1, 1])
                with s_col1:
                    disp = scores[["mom_20d", "mom_60d", "composite_score", "Rank"]].copy()
                    disp.columns = ["20d (%)", "60d (%)", "Score", "Rank"]
                    # FIX BT1: Styler.background_gradient() requires matplotlib,
                    # which isn't a project dependency — crashed on Streamlit
                    # Cloud. See dashboard/shared/chart_helpers.rdylgn_bg.
                    _mo_vmin, _mo_vmax = disp["Score"].min(), disp["Score"].max()
                    st.dataframe(
                        disp.style
                        .map(lambda v: rdylgn_bg(v, _mo_vmin, _mo_vmax), subset=["Score"])
                        .format("{:.2f}"),
                        width="stretch",
                    )
                with s_col2:
                    fig_bar = px.bar(
                        scores.reset_index(), x="Sector", y="composite_score",
                        color="composite_score", color_continuous_scale="RdYlGn",
                        title="Sector Scores",
                        labels={"composite_score": "Score (%)"},
                    )
                    fig_bar.update_layout(
                        template="nse_pro", height=340, showlegend=False,
                        margin=dict(l=0, r=0, t=40, b=0),
                    )
                    st.plotly_chart(fig_bar, width="stretch")
        except Exception as e:
            st.warning(f"Sector scores error: {e}")

    # ── Top movers from broad NSE universe ──────────────────────────
    st.markdown("---")
    st.markdown(
        '<div class="t-h2" style="margin:14px 0 6px 0">'
        '🚀 NSE Top Movers</div>',
        unsafe_allow_html=True,
    )
    st.caption("Scanning ~750 stocks across Nifty Total Market universe")

    @st.cache_data(ttl=180)
    def get_top_movers():
        """
        Fetch broad NSE movers using Yahoo JSON direct API (cloud-safe, no rate limits).
        Uses niftytotalmarket (~750 stocks) instead of Nifty50-only for a true market view.
        Falls back gracefully per-ticker if Yahoo JSON fails.
        """
        from data.universe import get_universe as _gu
        from utils.live_price import get_live_prices_batch

        tickers_list = _gu("niftytotalmarket")   # ~750 liquid NSE stocks
        raw = get_live_prices_batch(tickers_list, max_workers=20)

        rows = []
        for t in tickers_list:
            q = raw.get(t)
            if not isinstance(q, dict) or not q.get("price"):
                continue
            try:
                rows.append({
                    "Ticker":    t,
                    "Price":     round(q["price"],     2),
                    "Day (%)":   round(q["chg_pct"],   2),
                    "Prev":      round(q["prev_close"], 2),
                    "Vol Ratio": 1.0,
                })
            except Exception as e:
                _log.debug("overview: failed to build row for %s: %s", t, e)
                continue
        return pd.DataFrame(rows).sort_values("Day (%)", ascending=False) if rows else pd.DataFrame()

    if _mo_refresh_clicked:
        get_top_movers.clear()

    with st.spinner("Fetching NSE broad movers (~750 stocks)…"):
        movers = get_top_movers()
        if not movers.empty:
            top5 = movers.head(5)
            bot5 = movers.tail(5)
            m1, m2 = st.columns(2)

            def _mover_row(row, is_gain: bool):
                chg   = row["Day (%)"]
                price = row["Price"]
                tick  = row["Ticker"]
                short = tick.replace(".NS", "")
                color = "var(--bull)" if is_gain else "var(--bear)"
                sign  = "+" if is_gain else ""
                card_cls = "card-green" if is_gain else "card-red"
                st.markdown(
                    f'<div class="{card_cls}" style="padding:8px 14px;margin-bottom:4px">'
                    f'<b style="font-size:14px">{short}</b>'
                    f'<span style="float:right;font-size:13px">₹{price:,.2f} '
                    f'<b style="color:{color}">{sign}{chg:.2f}%</b></span>'
                    f'</div>',
                    unsafe_allow_html=True,
                )
                btn_a, btn_b, btn_c = st.columns([1, 1, 1])
                if btn_a.button("📊 Analyze", key=f"mover_analyze_{tick}",
                                use_container_width=True):
                    # FIX NAV1: use the canonical analyze_ticker handoff key
                    # (same as Command Centre / My Portfolio's "Analyze"
                    # buttons — see 04_analyze_stock.py FIX A8) rather than
                    # writing manual_ticker_input + last_analyzed directly.
                    # The latter happened to also auto-trigger in practice,
                    # but it's a second, untested parallel path doing the
                    # same job — one canonical hand-off key is less fragile
                    # and keeps every "Analyze" button in the app consistent.
                    st.session_state["analyze_ticker"] = tick
                    st.session_state["_goto_page"] = "🔍 Analyze Stock"
                    st.rerun()
                if btn_b.button("📝 Paper Trade", key=f"mover_trade_{tick}",
                                use_container_width=True):
                    st.session_state["_goto_page"] = "📂 Paper Trades"
                    st.session_state["pt_prefill_ticker"] = tick
                    st.rerun()
                if btn_c.button("＋ Watchlist", key=f"mover_wl_{tick}",
                                use_container_width=True):
                    if tick not in st.session_state.get("watchlist", []):
                        st.session_state.setdefault("watchlist", []).append(tick)
                    st.toast(f"{short} added to watchlist ✓")

            with m1:
                st.markdown("**📈 Top Gainers Today**")
                for _, row in top5.iterrows():
                    _mover_row(row, is_gain=True)
            with m2:
                st.markdown("**📉 Top Losers Today**")
                for _, row in bot5.iterrows():
                    _mover_row(row, is_gain=False)
        else:
            st.warning("Could not fetch mover data. Try refreshing in 30 seconds.")


# ═══════════════════════════════════════════════════════════════════════════
# TAB 2 — MACRO (formerly Market Internals' Macro Dashboard tab)
# ═══════════════════════════════════════════════════════════════════════════
with _tab_macro:
    st.caption(
        "Key rules: Crude ↑ → INR weakens (India imports 85%)  |  "
        "DXY ↑ → FII outflows from India  |  "
        "Gold ↑ → Risk-off globally  |  "
        "USD/INR ↑ → IT exporters benefit"
    )

    if st.button("🔄 Refresh Macro Data", type="primary", key="ov_macro_refresh"):
        load_macro_data.clear()

    with st.spinner("Fetching 7 macro instruments…"):
        try:
            macro_df = load_macro_data()

            _expected = ["Nifty 50", "BankNifty", "India VIX",
                         "Gold ($/oz)", "Brent Crude", "USD/INR", "DXY"]
            _missing  = [c for c in _expected if c not in macro_df.columns]

            if macro_df.empty:
                st.warning("Could not fetch macro data. Check internet connection.")
            else:
                if _missing:
                    st.info(
                        f"ℹ️ {len(_missing)} of {len(_expected)} instruments unavailable "
                        f"right now: **{', '.join(_missing)}**. Showing the "
                        f"{len(macro_df.columns)} that loaded successfully."
                    )

                st.markdown(

                    '<div class="t-h2" style="margin:14px 0 6px 0">'

                    'Current Levels & Daily Change</div>',

                    unsafe_allow_html=True,

                )
                card_cols = st.columns(min(len(macro_df.columns), 7))
                for i, col_name in enumerate(macro_df.columns):
                    series = macro_df[col_name].dropna()
                    if len(series) >= 2:
                        curr_v = float(series.iloc[-1])
                        prev_v = float(series.iloc[-2])
                        chg_v  = (curr_v / max(prev_v, 0.0001) - 1) * 100
                        fmt_v  = f"{curr_v:,.0f}" if curr_v > 500 else f"{curr_v:.2f}"
                        card_cols[i % 7].metric(col_name, fmt_v, f"{chg_v:+.2f}%")

                st.markdown("---")

                _usable_cols = [c for c in macro_df.columns if macro_df[c].dropna().shape[0] >= 30]

                if len(_usable_cols) < 2:
                    st.warning(
                        "⚠️ Not enough overlapping history across instruments right now "
                        "to plot 3-month performance or the correlation matrix "
                        f"(only {len(_usable_cols)} instrument(s) have sufficient data). "
                        "Try **Refresh Macro Data** in a moment."
                    )
                else:
                    _macro_use = macro_df[_usable_cols]

                    st.markdown(

                        '<div class="t-h2" style="margin:14px 0 6px 0">'

                        '3-Month Performance (Normalised to 100)</div>',

                        unsafe_allow_html=True,

                    )
                    first_valid = _macro_use.apply(
                        lambda s: s.dropna().iloc[0] if not s.dropna().empty else 1
                    )
                    norm_df = _macro_use.div(first_valid) * 100
                    _colors = [_TK[k] for k in ("bull", "azure", "bear", "amber", "accent", "violet", "ink-mid")]
                    fig_norm = go.Figure()
                    for i, col in enumerate(norm_df.columns):
                        fig_norm.add_trace(go.Scatter(
                            x=norm_df.index, y=norm_df[col], name=col,
                            line=dict(color=_colors[i % len(_colors)], width=2),
                        ))
                    fig_norm.add_hline(y=100, line_dash="dot", line_color="white", opacity=0.3)
                    fig_norm.update_layout(
                        template="nse_pro", height=380,
                        yaxis_title="Indexed (start = 100)",
                        legend=dict(orientation="h", y=1.02),
                        margin=dict(l=0, r=0, t=40, b=0),
                    )
                    st.plotly_chart(fig_norm, width="stretch")

                    st.markdown("---")

                    st.markdown(

                        '<div class="t-h2" style="margin:14px 0 6px 0">'

                        '30-Day Return Correlation Matrix</div>',

                        unsafe_allow_html=True,

                    )
                    rets_30 = _macro_use.pct_change().tail(30)
                    rets_30 = rets_30.dropna(axis=1, how="all")
                    if rets_30.shape[1] < 2:
                        st.warning(
                            "⚠️ Not enough recent daily returns to compute a "
                            "correlation matrix right now — try refreshing in a moment."
                        )
                    else:
                        corr_m = rets_30.corr().round(2)
                        fig_corr = px.imshow(
                            corr_m, text_auto=True, aspect="auto",
                            color_continuous_scale="RdBu_r", zmin=-1, zmax=1,
                            title="30-Day Daily Return Correlation",
                        )
                        fig_corr.update_layout(
                            template="nse_pro", height=420,
                            margin=dict(l=0, r=0, t=40, b=0),
                        )
                        st.plotly_chart(fig_corr, width="stretch")

                st.markdown("---")

                st.markdown(

                    '<div class="t-h2" style="margin:14px 0 6px 0">'

                    'India Market Impact Guide</div>',

                    unsafe_allow_html=True,

                )
                st.dataframe(pd.DataFrame([
                    {"Move": "Brent Crude ↑", "Sector Impact": "Aviation/Paint/Tyre/FMCG ↓",
                     "INR Effect": "INR weakens (imports 85%)", "Nifty Bias": "🔴 Bearish"},
                    {"Move": "Gold ↑",        "Sector Impact": "Jewellery mixed; gold ETFs ↑",
                     "INR Effect": "USD/INR rises if risk-off", "Nifty Bias": "🟡 Risk-off"},
                    {"Move": "DXY ↑",         "Sector Impact": "FII outflows from all EM",
                     "INR Effect": "INR weakens",              "Nifty Bias": "🔴 Bearish"},
                    {"Move": "DXY ↓",         "Sector Impact": "FII inflows to EM",
                     "INR Effect": "INR strengthens",          "Nifty Bias": "🟢 Bullish"},
                    {"Move": "USD/INR ↑",     "Sector Impact": "IT exporters (TCS/Infy/HCL) ↑; Auto ↓",
                     "INR Effect": "Higher import bill",       "Nifty Bias": "🟡 Mixed"},
                    {"Move": "USD/INR ↓",     "Sector Impact": "IT exporters ↓; Importers ↑",
                     "INR Effect": "Lower import costs",       "Nifty Bias": "🟡 Mixed"},
                ]), hide_index=True)

        except Exception as e:
            st.error(f"Macro data error: {e}")


# ═══════════════════════════════════════════════════════════════════════════
# TAB 3 — BREADTH (formerly Market Internals' Market Breadth tab)
# ═══════════════════════════════════════════════════════════════════════════
with _tab_breadth:
    from dashboard.shared.ui_components import (
        breadth_gauge, empty_state, flow_bars, section_header, share_bars,
    )

    _bc1, _bc2 = st.columns([3, 1], vertical_alignment="center")
    _bc1.caption(
        "Participation, not price. Price up + breadth expanding = broad rally; "
        "price up + breadth shrinking = narrow move. Nifty 50 scan takes ~3 min, cached 15 min."
    )
    with _bc2:
        run_breadth = st.button("Compute breadth", type="primary", key="breadth_btn", width="stretch")
        if st.button("Clear cached scan", key="ov_breadth_refresh", width="stretch"):
            compute_market_breadth.clear()
            st.session_state.pop("ov_breadth", None)

    if run_breadth:
        with st.spinner("Scanning Nifty 50 breadth (~3 min)…"):
            st.session_state["ov_breadth"] = compute_market_breadth(_NIFTY50_TICKERS)
    breadth = st.session_state.get("ov_breadth")

    if not breadth or not breadth.get("total"):
        st.markdown(empty_state(
            "No breadth scan yet",
            "Press Compute breadth to scan the Nifty 50. Institutional flows below load from the local ledger.",
        ), unsafe_allow_html=True)
    else:
        _adv, _dec = breadth["advance"], breadth["decline"]
        pct200 = breadth["pct_above_200"]
        if pct200 >= 70:
            _b_label, _b_tone, _b_note = ("Strong breadth", "bull",
                                          "Most of the index trades above its 200-day average: a broad uptrend.")
        elif pct200 >= 50:
            _b_label, _b_tone, _b_note = ("Moderate breadth", "amber",
                                          "More than half above the 200-day average; leadership is narrower.")
        elif pct200 >= 30:
            _b_label, _b_tone, _b_note = ("Weakening breadth", "accent",
                                          "Over half below the 200-day average; historically a higher-drawdown regime.")
        else:
            _b_label, _b_tone, _b_note = ("Weak breadth", "bear",
                                          "Most of the index below the 200-day average: a broad downtrend.")

        st.markdown(section_header(
            "Market breadth",
            f"Nifty 50 · {breadth['total']} stocks counted · {breadth['pct_above_50']:.0f}% above 50-DMA",
        ), unsafe_allow_html=True)

        _g1, _g2 = st.columns([1.15, 1])
        _g1.markdown(
            '<div class="breadth-panel"><div class="breadth-lbl">Advance / decline</div>'
            f'<div class="breadth-h" role="heading" aria-level="3">{_b_label}</div>'
            f'<div class="gauge-row">{breadth_gauge(_adv / max(_adv + _dec, 1))}<div>'
            f'<div class="gauge-num">{_adv} : {_dec}</div>'
            f'<div class="gauge-sub">advances : declines · {breadth["ad_ratio"]:.2f} A/D ratio</div>'
            f'<div class="gauge-note">{_b_note}</div></div></div>'
            '<div class="b-stats">'
            f'<div class="b-stat"><div class="b-stat-k">Near 52-w high</div>'
            f'<div class="b-stat-v">{breadth["near_52w_high"]}</div></div>'
            f'<div class="b-stat"><div class="b-stat-k">Near 52-w low</div>'
            f'<div class="b-stat-v">{breadth["near_52w_low"]}</div></div>'
            '</div><div class="breadth-foot">'
            'Within 5% of the 52-week high / low.</div></div>',
            unsafe_allow_html=True,
        )

        def _ma_tone(v):
            return "var(--bull)" if v > 60 else ("var(--amber)" if v > 40 else "var(--bear)")

        _g2.markdown(
            '<div class="breadth-panel"><div class="breadth-lbl">Above moving averages</div>'
            '<div class="breadth-h" role="heading" aria-level="3">Trend health</div>'
            + share_bars([
                (f"Above {n}-DMA", breadth[k], f"{breadth[k]:.0f}%", _ma_tone(breadth[k]))
                for n, k in ((20, "pct_above_20"), (50, "pct_above_50"), (200, "pct_above_200"))
            ], absolute=True)
            + '<div class="breadth-foot">'
              '&gt;70% broad participation · &lt;40% weak.</div></div>',
            unsafe_allow_html=True,
        )

        with st.expander("Breadth interpretation guide"):
            st.dataframe(pd.DataFrame([
                {"% Above SMA200": "> 70%",  "Signal": "Strong Bull",    "Historically": "Broad uptrends; pullbacks have tended to be bought"},
                {"% Above SMA200": "50–70%", "Signal": "Healthy uptrend","Historically": "Healthy uptrend with rotation"},
                {"% Above SMA200": "30–50%", "Signal": "Sector chop",    "Historically": "Choppy; dispersion across sectors is high"},
                {"% Above SMA200": "< 30%",  "Signal": "Bear market",    "Historically": "Downtrend; rallies have tended to fade"},
            ]), hide_index=True)

    # ── Institutional flows: local FII/DII ledger only (no network here) ──────
    try:
        from analysis import fii_dii as _fd
        _flows = _fd.load_history(days=10)
    except Exception as _fe:
        _log.debug("breadth tab: FII/DII ledger unavailable: %s", _fe)
        _flows = pd.DataFrame()
    if _flows is not None and not _flows.empty and {"date", "fii_net", "dii_net"} <= set(_flows.columns):
        _flows = _flows.copy()
        _flows["date"] = pd.to_datetime(_flows["date"], errors="coerce")
        for _c in ("fii_net", "dii_net"):
            _flows[_c] = pd.to_numeric(_flows[_c], errors="coerce")
        _flows = _flows.dropna(subset=["date"]).sort_values("date")
    if _flows is None or _flows.empty or "fii_net" not in _flows.columns:
        st.markdown(
            '<div class="breadth-panel"><div class="breadth-lbl">Institutional flows</div>'
            + empty_state("No FII / DII history stored yet",
                          "Open the FII / DII Flows page to fetch it; this panel reads the local ledger.")
            + "</div>",
            unsafe_allow_html=True,
        )
    else:
        def _cr(v):
            return "—" if pd.isna(v) else f"{'+' if v >= 0 else '−'}₹{abs(v):,.0f} Cr"

        _fii_sum, _dii_sum = _flows["fii_net"].sum(), _flows["dii_net"].sum()
        _sessions = [
            (d.strftime("%d %b"), None if pd.isna(f) else float(f), None if pd.isna(x) else float(x))
            for d, f, x in zip(_flows["date"], _flows["fii_net"], _flows["dii_net"])
        ]
        st.markdown(
            f'<div class="breadth-panel"><div class="breadth-lbl">Institutional flows · '
            f'{len(_flows)}-session net</div>'
            '<div class="breadth-h" role="heading" aria-level="3">FII &amp; DII activity</div>'
            '<div class="flow-legend">Green = net-buy session, red = net-sell. '
            'FII solid (left), DII faded (right).</div>'
            + flow_bars(_sessions)
            + '<div class="b-stats">'
            f'<div class="b-stat"><div class="b-stat-k">FII net</div><div class="b-stat-v">{_cr(_fii_sum)}</div></div>'
            f'<div class="b-stat"><div class="b-stat-k">DII net</div><div class="b-stat-v">{_cr(_dii_sum)}</div></div>'
            "</div></div>",
            unsafe_allow_html=True,
        )
