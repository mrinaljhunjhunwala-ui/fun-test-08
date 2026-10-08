"""
🏦 FII / DII Flows — page 19

Foreign Institutional Investors (FIIs) and Domestic Institutional
Investors (DIIs) drive Nifty direction more than any single technical
signal. This page shows their daily cash-market net-buy/sell numbers,
the 5-day and 20-day cumulative flow trend, and the classic
buy/sell-side combinations traders read as regime hints.

Data is FREE — pulled from NSE India's own JSON when possible, falls back
to NSE's exchange-only endpoint if the all-exchange one fails. Every successful
fetch is persisted, so the page renders even on a day when both sources
are unreachable (we just show a "last known" banner).
"""
from __future__ import annotations

import os
import sys
import pathlib

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

_ROOT = str(pathlib.Path(__file__).resolve().parents[2])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from analysis import fii_dii as _fd     # noqa: E402
from dashboard.shared.design import apply_design  # noqa: E402
from dashboard.shared.nav import render_sidebar  # noqa: E402
from dashboard.shared.chart_helpers import (  # noqa: E402
    render_top_bar, PLOT_COLORS as _PC, diverging_colors as _div,
    finite_abs_peak as _finite_peak,
)

# FIX: this page previously called st.set_page_config, which violates the
# CLAUDE.md rule "only dashboard/app.py may call set_page_config; a second
# call is a Streamlit crash". In practice the page loaded standalone in
# smoke tests but crashed as soon as a user navigated to it AFTER visiting
# any other page (Streamlit raises StreamlitAPIException on the second
# set_page_config call in a session). Removed. The page-level page-icon
# and page-title now live only in the shared sidebar+topbar treatment.
#
# Also adding apply_design / render_sidebar / render_top_bar so this page
# picks up the same theme + nav chrome every other page has. Previously
# this page was rendering bare Streamlit, which is why it looked out of
# place next to every other page even when it worked.
apply_design()
render_sidebar(current="FII / DII Flows")
render_top_bar()

st.markdown('<h1 class="page-title-serif">FII / DII <em>Cash-Market Flows</em></h1>', unsafe_allow_html=True)

st.caption(
    "Institutional net-buy/sell in the cash segment. FIIs move the tape; "
    "DIIs often absorb their selling on quality names. Regime hints below."
)

# ── Refresh (lazy — skips network if today already stored) ────────────────────
_c1, _c2 = st.columns([1, 4])
with _c1:
    _do_force = st.button("🔄 Refresh from source", type="secondary",
                          help="Bypass the same-day cache and re-fetch from "
                               "NSE. Usually not needed.")

with st.spinner("Fetching latest FII/DII data…"):
    _status = _fd.refresh(force=_do_force)

_src_labels = {
    "nse":          "NSE India (live)",
    "nse_exchange_only": "NSE exchange-only figures (fallback)",
    "cache":        "Local cache (today already stored)",
    "unavailable":  "⚠️ Both sources unreachable — showing last known data",
}
with _c2:
    _msg = _src_labels.get(_status["source"], _status["source"])
    if _status["source"] == "unavailable":
        st.warning(f"{_msg}. Latest available: **{_status.get('latest_date') or '—'}**")
    else:
        st.caption(
            f"Source: **{_msg}** · Latest date: **{_status.get('latest_date') or '—'}** "
            f"· Rows written this refresh: **{_status['rows_written']}**"
        )

# ── History load ─────────────────────────────────────────────────────────────
_days_choice = st.select_slider(
    "History window",
    options=[10, 30, 60, 90, 180, 365],
    value=60, format_func=lambda d: f"{d}d",
)
_df = _fd.load_history(days=_days_choice)

if _df.empty:
    st.info(
        "No FII/DII history yet. Click **🔄 Refresh from source** to fetch. "
        "If both NSE endpoints are blocked from Streamlit Cloud's IP "
        "range, the fetch will fail silently — the ledger will still populate "
        "on the days it succeeds."
    )
    st.stop()

_df["date"] = pd.to_datetime(_df["date"])
# Postgres returns NULL nets as object-dtype None; coerce every flow column so
# sums / abs / comparisons below never hit None or numeric strings.
for _nc in ("fii_buy", "fii_sell", "fii_net", "dii_buy", "dii_sell", "dii_net"):
    if _nc in _df.columns:
        _df[_nc] = pd.to_numeric(_df[_nc], errors="coerce")
_df = _df.sort_values("date").reset_index(drop=True)

# ── Overview strip ────────────────────────────────────────────────────────────
_latest = _df.iloc[-1]
_5d = _df.tail(5)
_20d = _df.tail(20)

_o1, _o2, _o3, _o4 = st.columns(4)
_o1.metric("Latest FII net (₹ Cr)",
           f"{_latest.get('fii_net', 0):+,.0f}" if pd.notna(_latest.get("fii_net")) else "—",
           delta_color=("normal" if pd.isna(_latest.get("fii_net"))
                        else ("normal" if _latest["fii_net"] >= 0 else "inverse")))
_o2.metric("Latest DII net (₹ Cr)",
           f"{_latest.get('dii_net', 0):+,.0f}" if pd.notna(_latest.get("dii_net")) else "—")
_o3.metric("5-day FII cumulative", f"{_5d['fii_net'].sum():+,.0f}")
_o4.metric("20-day DII cumulative", f"{_20d['dii_net'].sum():+,.0f}")

# ── Regime interpretation ─────────────────────────────────────────────────────
# Restructured from a single ### heading into a proper regime card. Same
# 5-way logic as before, but now carries (title, sub-caption, tone) so the
# card can render a coloured rail matching the semantic meaning of the
# regime -- bull green for broad participation, bear red for distribution,
# amber for the two mixed regimes, neutral grey for undetermined.
_fii_5 = _5d["fii_net"].sum()
_dii_5 = _5d["dii_net"].sum()
if _fii_5 > 0 and _dii_5 > 0:
    _rg_icon, _rg_title, _rg_body, _rg_rail = (
        "🟢", "Broad participation",
        "Both FII and DII net buyers this week. Rallies tend to be persistent in this regime.",
        "var(--bull)",
    )
elif _fii_5 < 0 and _dii_5 > 0:
    _rg_icon, _rg_title, _rg_body, _rg_rail = (
        "🟠", "Domestic-supported dip",
        "FIIs selling, DIIs buying. Classic pullback profile — often a buy-on-dip "
        "regime for quality names but not for high-beta.",
        "var(--amber)",
    )
elif _fii_5 < 0 and _dii_5 < 0:
    _rg_icon, _rg_title, _rg_body, _rg_rail = (
        "🔴", "Distribution",
        "Both selling. Historically this regime has preceded weakness, "
        "with high-beta names typically most affected.",
        "var(--bear)",
    )
elif _fii_5 > 0 and _dii_5 < 0:
    _rg_icon, _rg_title, _rg_body, _rg_rail = (
        "🟡", "DII profit-taking rally",
        "FIIs buying, DIIs selling. Rallies in this regime have historically been shallower.",
        "var(--amber)",
    )
else:
    _rg_icon, _rg_title, _rg_body, _rg_rail = (
        "⚪", "Mixed",
        "No clear directional signal from institutional flows.",
        "var(--dim)",
    )

st.markdown("---")
st.markdown(
    f'<div style="background:var(--surface);border-left:4px solid {_rg_rail};'
    f'border-radius:var(--r-base);padding:14px 18px;margin:8px 0 12px 0">'
    f'<div style="font-size:11px;font-weight:700;color:var(--dim);'
    f'letter-spacing:.1em;text-transform:uppercase">'
    f'5-Day Regime Read</div>'
    f'<div style="font-family:var(--font-serif);font-size:22px;color:var(--ink);'
    f'font-weight:400;letter-spacing:-.005em;margin:2px 0 6px 0;line-height:1.2">'
    f'<span style="margin-right:8px">{_rg_icon}</span>'
    f'<em style="font-style:italic;color:{_rg_rail}">{_rg_title}</em></div>'
    f'<div style="font-size:13.5px;color:var(--ink-mid);line-height:1.55;max-width:70ch">'
    f'{_rg_body}</div>'
    f'<div style="font-family:var(--font-mono);font-size:11px;color:var(--faint);'
    f'margin-top:8px">FII 5d net {_fii_5:+,.0f} Cr · DII 5d net {_dii_5:+,.0f} Cr</div>'
    f'</div>',
    unsafe_allow_html=True,
)

# ── Chart 1 — daily bars + cumulative line ────────────────────────────────────
st.markdown("---")
st.markdown('<div class="t-h2" style="margin:14px 0 6px 0">'
            '📊 Daily net flows <span style="color:var(--dim);font-weight:400;'
            'font-size:12px">₹ Cr</span></div>',
            unsafe_allow_html=True)

# P2 · diverging colour rule (dataviz skill): one encoding per channel.
# Previously one grouped chart used FOUR hues — FII green/red, DII blue/amber
# — so colour meant both "which investor" and "which sign". Now FII and DII
# get their own panel (identity = position), and within each panel hue is
# sign only (bull/bear) with opacity scaling by magnitude. Shared y-scale so
# bar heights compare across panels.
from plotly.subplots import make_subplots as _mk_sub
_fd_peak = _finite_peak(_df["fii_net"], _df["dii_net"])
if _fd_peak is None:
    st.caption("No net-flow values in this window yet — daily bars will "
               "appear once the ledger has non-empty FII/DII nets.")
else:
    _fig = _mk_sub(rows=2, cols=1, shared_xaxes=True,
                   vertical_spacing=0.08,
                   subplot_titles=("FII / FPI net", "DII net"))
    for _row, _col, _nm in ((1, "fii_net", "FII net"), (2, "dii_net", "DII net")):
        _fig.add_bar(
            x=_df["date"], y=_df[_col], name=_nm, showlegend=False,
            marker_color=_div(_df[_col].fillna(0), full_at=_fd_peak),
            hovertemplate="%{x|%d %b}<br>" + _nm + ": ₹%{y:,.0f} Cr<extra></extra>",
            row=_row, col=1,
        )
        _fig.add_hline(y=0, line_dash="dash", line_color=_PC["faint"], row=_row, col=1)
    _fig.update_layout(
        height=460, margin=dict(l=40, r=20, t=30, b=40),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
    )
    # shared_yaxes=True only links axes within a ROW; stacked rows need an
    # explicit match so FII and DII bar heights share one scale.
    _fig.update_yaxes(title_text="₹ Crore", matches="y")
    st.plotly_chart(_fig, width="stretch")

# DT1 + DT2 · flows are scraped from NSE — surface WHERE + WHEN.
try:
    from dashboard.shared.ui_components import data_as_of as _fd_asof
    import datetime as _fd_dt
    if pd.notna(_latest.get("date")):
        _fd_when = _fd_dt.datetime.fromisoformat(
            str(_latest["date"])).strftime("%d %b %Y")
    else:
        _fd_when = _fd_dt.datetime.now().strftime("%H:%M IST")
    st.markdown(
        _fd_asof(_fd_when, source="nse",
                 ttl_hint="daily provisional · updated after close"),
        unsafe_allow_html=True,
    )
except Exception:
    pass

# ── Chart 2 — cumulative ──────────────────────────────────────────────────────
st.markdown("---")
st.markdown('<div class="t-h2" style="margin:14px 0 6px 0">'
            '📈 Cumulative flow '
            '<span style="color:var(--dim);font-weight:400;font-size:12px">'
            'running sum over window</span></div>',
            unsafe_allow_html=True)
_cum = _df.copy()
_cum["fii_cum"] = _cum["fii_net"].fillna(0).cumsum()
_cum["dii_cum"] = _cum["dii_net"].fillna(0).cumsum()
# Series identity here is investor type, not sign, so neither line uses
# bull/bear hues (green would read as "positive"). Sign is read against the
# dashed zero line.
_fig2 = go.Figure()
_fig2.add_trace(go.Scatter(x=_cum["date"], y=_cum["fii_cum"], mode="lines",
                            line=dict(color=_PC["accent"], width=2), name="FII cumulative"))
_fig2.add_trace(go.Scatter(x=_cum["date"], y=_cum["dii_cum"], mode="lines",
                            line=dict(color=_PC["azure"], width=2), name="DII cumulative"))
_fig2.add_hline(y=0, line_dash="dash", line_color=_PC["faint"])
_fig2.update_layout(
    xaxis_title="", yaxis_title="₹ Crore (cumulative)",
    height=340, margin=dict(l=40, r=20, t=20, b=40),
    paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
    legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
)
st.plotly_chart(_fig2, width="stretch")

# ── Table ─────────────────────────────────────────────────────────────────────
st.markdown("---")
with st.expander("🗄 Full data table", expanded=False):
    _tbl = _df.copy().sort_values("date", ascending=False)
    _tbl["date"] = _tbl["date"].dt.strftime("%Y-%m-%d")
    for c in ("fii_buy", "fii_sell", "fii_net", "dii_buy", "dii_sell", "dii_net"):
        if c in _tbl.columns:
            _tbl[c] = _tbl[c].apply(lambda v: f"{v:+,.0f}" if pd.notna(v) else "—")
    st.dataframe(_tbl, hide_index=True, width="stretch")

st.caption(
    "**Sources:** NSE India `/api/fiidiiTradeReact` (primary, all exchanges), "
    "`/api/fiidiiTradeNse` (fallback, NSE only). All values in ₹ Crore, cash segment only. "
    "F&O positioning not included in this view."
)
