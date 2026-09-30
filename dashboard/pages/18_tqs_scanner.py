"""
dashboard/pages/18_tqs_scanner.py
Trend Quality Score (TQS) — Universe Scanner.

Score a universe of tickers by TQS, rank them, show a sortable table +
heatmap. The single-stock DEEP DIVE mode this page used to have was folded
into 04_analyze_stock.py's "🌊 TQS breakdown" expander (Analysis
consolidation #2) so a user gets both the composite verdict AND the TQS
pillar decomposition in one place — no need to visit two pages.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from dashboard.shared.nav import render_sidebar
from dashboard.shared.design import apply_design


# ── Page config ───────────────────────────────────────────────────────────────
apply_design()
render_sidebar()

st.markdown('<h1 class="page-title-serif">Trend <em>Quality Score</em></h1>', unsafe_allow_html=True)

st.caption(
    "Measures trend **health and persistence** across 4 pillars (max 90 pts). "
    "Validated baseline: +0.41 rank correlation with staying in an uptrend next month."
)

# FIX SCR-XREF — see the matching note in 06_smart_screener.py.
with st.expander("↔️ Also see: Smart Screener · Tomorrow's Watchlist", expanded=False):
    st.markdown(
        "TQS is a *different scoring engine* to the composite score used by "
        "the other two screener-family pages — it emphasises trend PERSISTENCE "
        "over signal fires. Use them together, not instead of each other:\n\n"
        "- **Smart Screener** — interactive 4-screen scan (oversold, momentum, "
        "breakout, pullback) with a composite score per hit.\n"
        "- **Tomorrow's Watchlist** — the same scan pre-computed on last close, "
        "no wait time."
    )

# ── Lazy import engine ────────────────────────────────────────────────────────
@st.cache_resource(show_spinner=False)
def _load_engine():
    from analysis.trend_quality_score import (
        score_ticker, 
        scan_universe, 
        add_indicators, 
        fetch_data, 
        _score_all_pillars
    )
    return score_ticker, scan_universe, add_indicators, fetch_data, _score_all_pillars

try:
    score_ticker, scan_universe, add_indicators, fetch_data, _score_all_pillars = _load_engine()
    ENGINE_OK = True
except Exception as e:
    st.error(f"TQS engine failed to load: {e}")
    ENGINE_OK = False
    st.stop()


# ── Default configurations ───────────────────────────────────────────────────
DEFAULT_TICKERS = [
    "RELIANCE.NS", "TCS.NS",        "HDFCBANK.NS",   "INFY.NS",
    "ICICIBANK.NS","HINDUNILVR.NS",  "ITC.NS",        "SBIN.NS",
    "BHARTIARTL.NS","KOTAKBANK.NS",  "AXISBANK.NS",   "WIPRO.NS",
    "MARUTI.NS",   "TITAN.NS",       "ASIANPAINT.NS", "NESTLEIND.NS",
    "SUNPHARMA.NS","HCLTECH.NS",     "LT.NS",         "ULTRACEMCO.NS",
]

from dashboard.shared.tokens import COLORS as TOKENS, ordinal_ramp  # noqa: E402

# Ordinal ramps (best -> worst) blended from bull -> amber -> bear tokens so
# every level is visibly distinct. Colour is redundant with the label text
# (grade / signal are printed in the cell), so it never carries meaning alone.
_SIGNAL_ORDER = ("STRONG TREND", "TRENDING", "NEUTRAL", "WEAK", "AVOID")
_GRADE_ORDER = ("A+", "A", "B", "C", "D", "F")
SIGNAL_COLOUR = dict(zip(_SIGNAL_ORDER, ordinal_ramp(len(_SIGNAL_ORDER))))
GRADE_COLOUR = dict(zip(_GRADE_ORDER, ordinal_ramp(len(_GRADE_ORDER))))


# ═════════════════════════════════════════════════════════════════════════════
# UNIVERSE SCANNER (Deep Dive tab moved into Analyze Stock — see file docstring)
# ═════════════════════════════════════════════════════════════════════════════
if True:  # top-level guard kept minimal so the following block stays indented as before
    st.markdown('<div class="t-h2" style="margin:14px 0 6px 0">'
                'Universe Scanner</div>',
                unsafe_allow_html=True)

    # FIX TQS1 — this page previously had no way to scan a real universe at
    # all: the only options were a hardcoded 20-ticker blue-chip sample
    # (DEFAULT_TICKERS) or manually typing out hundreds of symbols by hand
    # into a text box, unlike every other scanning page in the app (Market
    # Live, Screener, Top Picks), which all offer a one-click Nifty
    # 50/100/500/Total Market selector via data.universe.get_universe(). That
    # made TQS feel much more limited in scope than the rest of the platform
    # for no real reason — the scoring engine itself has nothing 20-ticker
    # specific about it.
    _tqs_universe_choice = st.selectbox(
        "Universe",
        ["Nifty 50", "Nifty 100", "Nifty 500", "Nifty Total Market (~745)", "Custom (paste below)"],
        index=0,
        key="tqs_universe_choice",
        help="Pick a preset to auto-fill the ticker list, or choose Custom to paste your own.",
    )
    _TQS_UNIVERSE_MAP = {
        "Nifty 50": "nifty50", "Nifty 100": "nifty100",
        "Nifty 500": "nifty500", "Nifty Total Market (~745)": "niftytotalmarket",
    }
    if _tqs_universe_choice in _TQS_UNIVERSE_MAP:
        from data.universe import get_universe as _tqs_get_universe
        _tqs_preset_tickers = _tqs_get_universe(_TQS_UNIVERSE_MAP[_tqs_universe_choice])
        st.caption(f"{len(_tqs_preset_tickers)} tickers in this universe.")
    else:
        _tqs_preset_tickers = DEFAULT_TICKERS

    col1, col2 = st.columns([3, 1])
    with col1:
        if _tqs_universe_choice == "Custom (paste below)":
            raw_input = st.text_area(
                "Tickers (comma or newline separated)",
                value=", ".join(DEFAULT_TICKERS),
                height=100,
            )
        else:
            raw_input = ", ".join(_tqs_preset_tickers)
            with st.expander(f"Preview tickers ({len(_tqs_preset_tickers)})"):
                st.caption(raw_input)
    with col2:
        period = st.selectbox("Period", ["1y", "2y", "5y"], index=0, key="scan_period")
        min_tqs = st.slider("Min TQS filter", 0, 90, 0, step=5)
        if _tqs_universe_choice != "Custom (paste below)" and len(_tqs_preset_tickers) > 100:
            st.caption(
                f"⚠️ Scanning {len(_tqs_preset_tickers)} tickers takes a few "
                f"minutes even parallelised. Grab a coffee ☕"
            )
        run_scan = st.button("▶ Run Scan", width="stretch", type="primary")

    if run_scan:
        tickers = [t.strip().upper() for t in raw_input.replace("\n", ",").split(",") if t.strip()]
        if not tickers:
            st.warning("Enter at least one ticker.")
        else:
            rows = []
            prog = st.progress(0, text="Scanning…")
            import logging as _tqs_log
            import concurrent.futures as _tqs_cf
            _tqs_done = 0

            # FIX TQS1 (companion) — score_ticker/scan_universe are both a
            # plain serial for-loop with no parallelisation, fine for a
            # 20-ticker sample but impractical for a real universe (500+
            # tickers would take many minutes one at a time). Parallelising
            # here matches the pattern already used elsewhere in the app
            # (Command Centre's Top Picks, backtest/runner.py) so a broader
            # universe is actually practical to scan, not just selectable.
            with _tqs_cf.ThreadPoolExecutor(max_workers=16) as _tqs_ex:
                _tqs_futures = {_tqs_ex.submit(score_ticker, t, period=period): t for t in tickers}
                for _tqs_fut in _tqs_cf.as_completed(_tqs_futures):
                    t = _tqs_futures[_tqs_fut]
                    try:
                        r = _tqs_fut.result()
                        rows.append(r.as_dict())
                    except Exception as _tqs_e:
                        _tqs_log.getLogger("dashboard.tqs_scanner").debug(
                            "score_ticker(%s) failed: %s — skipped", t, _tqs_e
                        )
                    _tqs_done += 1
                    prog.progress(_tqs_done / len(tickers), text=f"Scored {_tqs_done}/{len(tickers)}")
            prog.empty()

            if not rows:
                st.error("No data returned for any ticker.")
            else:
                df_scan = (
                    pd.DataFrame(rows)
                    .sort_values("tqs", ascending=False)
                    .reset_index(drop=True)
                )
                df_scan = df_scan[df_scan["tqs"] >= min_tqs]
                st.session_state["tqs_scan"] = df_scan

    # ── Display results ───────────────────────────────────────────────────────
    if "tqs_scan" in st.session_state:
        df_scan = st.session_state["tqs_scan"]
        
        if df_scan.empty:
            st.info("No tickers match the active minimum TQS filter.")
        else:
            total = len(df_scan)
            strong = (df_scan["signal"] == "STRONG TREND").sum()
            trending = (df_scan["signal"] == "TRENDING").sum()
            avoid = (df_scan["signal"] == "AVOID").sum()

            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Stocks Scored", total)
            m2.metric("Strong Trend", strong)
            m3.metric("Trending", trending)
            m4.metric("No trend", avoid)

            st.divider()

            # Color styling helper functions
            def _colour_signal(val):
                c = SIGNAL_COLOUR.get(str(val).upper(), TOKENS["dim"])
                return f"color: {c}; font-weight: 600"

            def _colour_grade(val):
                c = GRADE_COLOUR.get(str(val).upper(), TOKENS["dim"])
                return f"color: {c}; font-weight: 700"

            def _bar_tqs(val):
                try:
                    val_float = float(val)
                    pct = max(0.0, min(100.0, (val_float / 90.0) * 100))
                except (ValueError, TypeError):
                    pct = 0
                return f"background: linear-gradient(90deg, rgba(59, 130, 246, 0.4) {pct:.0f}%, transparent {pct:.0f}%)"

            display_cols = [
                "ticker", "close", "tqs", "grade", "signal",
                "p1_strength", "p2_persistence", "p3_momentum", "p4_confirmation",
                "rsi", "sharpe_20", "obv_z",
            ]
            
            # Sub-select columns present in DataFrame to avoid KeyError issues
            actual_cols = [col for col in display_cols if col in df_scan.columns]
            
            # Create a clean, renamed dataframe first to simplify styled subset references
            df_display = df_scan[actual_cols].rename(columns={
                "ticker": "Ticker", "close": "Close", "tqs": "TQS",
                "grade": "Grade", "signal": "Signal",
                "p1_strength": "P1 Strength", "p2_persistence": "P2 Persist",
                "p3_momentum": "P3 Momentum", "p4_confirmation": "P4 Volume",
                "rsi": "RSI", "sharpe_20": "Sharpe20", "obv_z": "OBV-Z",
            })

            # Format and apply styling safely
            styled = (
                df_display.style
                .map(_colour_signal, subset=["Signal"] if "Signal" in df_display.columns else [])
                .map(_colour_grade,  subset=["Grade"] if "Grade" in df_display.columns else [])
                .apply(lambda col: [_bar_tqs(v) for v in col], subset=["TQS"] if "TQS" in df_display.columns else [])
                .format({
                    # Display-only relabel (CLAUDE.md rule 1): engine value
                    # "AVOID" stays internal; colour lookup keys off the raw value.
                    "Signal": lambda v: "NO TREND" if str(v).upper() == "AVOID" else v,
                    "Close": "₹{:,.2f}", "TQS": "{:.1f}",
                    "P1 Strength": "{:.1f}", "P2 Persist": "{:.1f}",
                    "P3 Momentum": "{:.1f}", "P4 Volume": "{:.1f}",
                    "RSI": "{:.1f}", "Sharpe20": "{:.2f}", "OBV-Z": "{:.2f}",
                }, na_rep="-")
            )
            st.dataframe(styled, use_container_width=True, height=500)

            # ── Pillar breakdown — top 10 ─────────────────────────────────────
            st.markdown('<div class="t-h2" style="margin:14px 0 6px 0">'
                        'Pillar breakdown '
                        '<span style="color:var(--dim);font-weight:400;'
                        'font-size:12px">top 10</span></div>',
                        unsafe_allow_html=True)
            top10 = df_scan.head(10)
            fig = go.Figure()
            pillars = ["p1_strength", "p2_persistence", "p3_momentum", "p4_confirmation"]
            labels  = ["P1 Strength", "P2 Persistence", "P3 Momentum", "P4 Volume"]
            from dashboard.shared.chart_helpers import PLOT_COLORS as _PC
            colours = [_PC["accent"], _PC["azure"], _PC["amber"], _PC["dim"]]

            for pillar, label, colour in zip(pillars, labels, colours):
                if pillar in top10.columns:
                    fig.add_trace(go.Bar(
                        name=label,
                        x=top10["ticker"],
                        y=top10[pillar],
                        marker_color=colour,
                    ))

            fig.update_layout(
                barmode="stack",
                xaxis_title="Ticker",
                yaxis_title="Score",
                yaxis_range=[0, 90],
                legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
                margin=dict(t=40, b=40, l=10, r=10),
                height=380,
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)"
            )
            st.plotly_chart(fig, use_container_width=True)

            # ── P2 · 4-pillar radar — one ticker vs the top-10 median ─────────
            # The table gives four numbers side by side; the radar shows the
            # SHAPE (balanced vs one-pillar-carried) at a glance. Axes are each
            # pillar's share of its 22.5-pt max so all four spokes are 0-100%.
            _have = [p for p in pillars if p in df_scan.columns]
            if len(_have) == 4 and not df_scan.empty:
                _PMAX = 22.5
                _r_pick = st.selectbox(
                    "Pillar shape for", df_scan["ticker"].tolist(), index=0,
                    key="tqs_radar_pick",
                )
                _r_row = df_scan[df_scan["ticker"] == _r_pick].iloc[0]
                _theta = labels + labels[:1]
                _vals = [max(0.0, min(float(_r_row[p]) / _PMAX * 100, 100)) for p in _have]
                _med = [float(top10[p].median()) / _PMAX * 100 for p in _have]
                _rfig = go.Figure()
                _rfig.add_trace(go.Scatterpolar(
                    r=_med + _med[:1], theta=_theta, name="Top-10 median",
                    line=dict(color=_PC["dim"], dash="dot", width=1.5),
                ))
                _rfig.add_trace(go.Scatterpolar(
                    r=_vals + _vals[:1], theta=_theta, name=_r_pick,
                    fill="toself", fillcolor="rgba(200,138,63,0.18)",
                    line=dict(color=_PC["accent"], width=2),
                    hovertemplate="%{theta}: %{r:.0f}% of max<extra></extra>",
                ))
                _rfig.update_layout(
                    polar=dict(bgcolor="rgba(0,0,0,0)",
                               radialaxis=dict(range=[0, 100], ticksuffix="%",
                                               showline=False, tickfont=dict(size=9))),
                    legend=dict(orientation="h", y=-0.1),
                    height=360, margin=dict(t=30, b=30, l=40, r=40),
                    paper_bgcolor="rgba(0,0,0,0)",
                )
                st.plotly_chart(_rfig, use_container_width=True)

            # ── Download ──────────────────────────────────────────────────────
            st.download_button(
                "⬇ Download CSV",
                data=df_scan.to_csv(index=False),
                file_name="tqs_scan.csv",
                mime="text/csv",
                use_container_width=False
            )
    else:
        # F5 empty-state kit -- see dashboard/shared/ui_components.py.
        from dashboard.shared.ui_components import empty_state as _empty
        st.markdown(
            _empty(
                title="Ready to scan",
                hint="Pick a universe above and click **▶ Run Scan** to "
                     "score every ticker on the 4-pillar Trend Quality Score "
                     "(strength · persistence · momentum · confirmation).",
                icon="🌊",
            ),
            unsafe_allow_html=True,
        )


