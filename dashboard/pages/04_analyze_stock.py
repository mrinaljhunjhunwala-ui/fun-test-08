"""Analyze Stock - NSE Smart Investor (multipage page; body verbatim from app.py).

LAYOUT (2026-09-29): artboard 02 of the 2026-09-12 "proposed layout" mockup.
Ticker head (serif symbol, tags, price, actions) -> tab strip with Overview
first. Overview = posture hero with pillar bars, combined read across three
horizons, then chart + thesis (left) beside sector-aware fundamentals,
overlay signals and reference levels (right). Secondary drill-downs live in
a "More detail" block of expanders at the foot of Overview; Fundamentals,
Valuation and Liquidity share one tab. Everything is computed before the
first render so the head can show liquidity / position context.


FIXES applied in this revision
───────────────────────────────
A1  "Paper Trade This Signal" bottom button replaced with _paper_trade_popover()
    so it gets the account selector, live-price re-anchor, qty override and
    proper R:R display — identical to every other paper trade button in the app.
    The old direct paper_open_trade() call with hardcoded qty=int(10000/entry)
    is gone.

A2  Live drift caption now gated on _ms_an.get("is_open") — the warning
    "Live ₹X vs analysis ₹X" no longer fires when the market is closed (Yahoo
    returns last EOD close as "live" outside hours, making the drift spurious).

A3  Conviction score now guards against _dc["total"] being None or 0. If
    deep confirmation is unavailable the conviction section shows
    "confirmation unavailable" and skips the adjustment rather than silently
    arithmetic-ing on a phantom 9.

A4  Earnings date label now handles negative _ed_days (results already
    announced) with an explicit branch: "Results Xd ago" in teal/gray,
    rather than falling through to the confusing "Unknown / gray" bucket.

A5  Portfolio fit CSV loading is now wrapped in @st.cache_data(ttl=300)
    keyed on file path + mtime so re-reading the CSV on every widget
    interaction is avoided.

A6  Sector rank metric guard: f"#{cs.sector_rank}" is now
    f"#{cs.sector_rank}" if cs.sector_rank else "—" to prevent "#None".

A7  df.iloc[-2] is now guarded with len(df) >= 2 to avoid IndexError on
    single-row dataframes (new listings, data gaps).

A8  Ticker handoff from My Portfolio (or anywhere else) via
    st.session_state["analyze_ticker"] is now actually consumed. Previously
    My Portfolio's "📊 Analyze" button set this key and navigated here, but
    this page never read it — so the search box stayed empty and the user
    had to manually retype the ticker. The prefilled ticker now forces the
    analysis to run immediately on arrival, and the session key is popped
    so it doesn't keep re-forcing on subsequent manual interactions.

A9  Portfolio Fit holdings source now reads load_manual_holdings() instead
    of a portfolio.csv path / Angel One tmp path, matching the My Portfolio
    page's move away from file-based holdings to manual entry.
"""

import logging
import os
import sys
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import streamlit as st

from analysis.fundamentals.service import default_service as _fund_service
from analysis.fundamentals import analytics as _fund_analytics

from dashboard.shared.design import apply_design
from dashboard.shared.nav import render_sidebar
from dashboard.shared.cache import (
    STOCK_SEARCH_MAP,
    _deep_confirmation,
    _plain_english,
    _trim_to_period,
    _validate_ticker,
    get_composite_score,
    get_display_name,
    load_ticker_df,
)
from dashboard.shared.ui_components import (
    chip_delta,
    chip_pill,
    chip_tag,
    empty_state,
    gate_strip,
    hero_verdict,
    kit_card,
    kv_list,
    level_strip,
    section_header,
    signal_table,
    stock_head,
    thesis_grid,
)
from dashboard.shared.trade_utils import (
    _display_label,            # Phase 2 UI honesty
    verdict_display_label,
    _paper_trade_popover,      # FIX A1: use popover instead of direct call
    load_manual_holdings,      # FIX A9: manual holdings replace CSV/Angel One path
)
from dashboard.shared.chart_helpers import (
    build_price_chart,
    render_top_bar,
)

apply_design()
render_sidebar(current="Analyze Stock")
render_top_bar()

_log = logging.getLogger("dashboard.analyze_stock")

# ─────────────────────────────────────────────────────────────────────────────
st.markdown(
    '<h1 class="page-title-serif">Analyze <em>Stock</em></h1>'
    '<p class="page-subtitle">Search by company name or ticker for the trend-quality score, '
    'chart, sector-aware fundamentals and a descriptive read of the setup.</p>',
    unsafe_allow_html=True,
)

# ─────────────────────────────────────────────────────────────────────────────
# MARKET CONTEXT strip — one labelled row of market-WIDE signals that used to
# be scattered across three different scroll depths on this page: the regime
# badge above (Nifty regime), the FII/DII 5-day flow block ~800 lines down,
# and the VIX Regime metric inside the score hero. Consolidating them here
# under an explicit "market-wide, same for every stock" label kills the "is
# this per-stock or per-market?" confusion the user raised for the Range /
# regime badge.
#
# Cached 30 min — FII/DII data only updates once per day at NSE bhavcopy time,
# and the regime doesn't flip intraday either, so re-computing on every rerun
# is pure waste.
# ─────────────────────────────────────────────────────────────────────────────
@st.cache_data(ttl=1800, show_spinner=False)
def _market_context_row():
    """Cheap dict of the three market-wide signals: fii_5d, dii_5d, regime_msg."""
    _row = {"fii_5d": None, "dii_5d": None, "regime_msg": None,
            "regime_severity": "neutral"}
    try:
        from analysis.fii_dii import load_history as _mc_fd
        _fd_df = _mc_fd(days=5)
        if not _fd_df.empty and len(_fd_df) >= 3:
            _fii_5 = float(_fd_df["fii_net"].fillna(0).sum())
            _dii_5 = float(_fd_df["dii_net"].fillna(0).sum())
            _row["fii_5d"] = _fii_5
            _row["dii_5d"] = _dii_5
            if _fii_5 > 0 and _dii_5 > 0:
                _row["regime_msg"] = ("Broad participation — FII + DII both net "
                                       "buyers this week. Rallies persist in this regime.")
                _row["regime_severity"] = "green"
            elif _fii_5 < 0 and _dii_5 > 0:
                _row["regime_msg"] = ("Domestic-supported dip — FII selling absorbed "
                                       "by DII. Historically dips here have been shallower; "
                                       "high-beta names remain more exposed.")
                _row["regime_severity"] = "amber"
            elif _fii_5 < 0 and _dii_5 < 0:
                _row["regime_msg"] = ("Distribution — both selling. Historically "
                                       "precedes weakness.")
                _row["regime_severity"] = "red"
            elif _fii_5 > 0 and _dii_5 < 0:
                _row["regime_msg"] = ("DII profit-taking rally — FII buying vs DII "
                                       "selling. Rallies have historically been shallower.")
                _row["regime_severity"] = "amber"
            else:
                _row["regime_msg"] = "Mixed — no clear institutional-flow signal."
    except Exception:
        pass
    return _row

# One muted line under the search: the market regime badge plus the 5-day
# FII / DII read. Market-wide, identical for every ticker (see the comment
# on _market_context_row above).
try:
    from dashboard.shared.ui_components import regime_badge as _ui_regime_badge

    @st.cache_data(ttl=1800, show_spinner=False)
    def _as_regime_snap():
        from analysis.regime import snapshot_live
        try:
            return snapshot_live().as_dict()
        except Exception:
            return None

    _as_reg = _as_regime_snap()
    _mc = _market_context_row()
    _mc_parts = []
    if _as_reg:
        _mc_parts.append(_ui_regime_badge(_as_reg.get("label", "unknown"),
                                          _as_reg.get("confidence", "low"), compact=True))
    if _mc["fii_5d"] is not None:
        _sev_color = {"green": "var(--bull)", "amber": "var(--amber)",
                      "red": "var(--bear)", "neutral": "var(--dim)"}[_mc["regime_severity"]]
        _mc_parts.append(
            f'<span>FII 5d <b style="color:{_sev_color}">₹{_mc["fii_5d"]:+,.0f} Cr</b></span>'
            f'<span>DII 5d <b style="color:{_sev_color}">₹{_mc["dii_5d"]:+,.0f} Cr</b></span>'
            f'<span style="font-family:var(--font-sans)">{_mc["regime_msg"]}</span>'
        )
    else:
        _mc_parts.append('<span>FII/DII flows unavailable; open the FII / DII Flows page once '
                         'to populate the local table.</span>')
    st.markdown(
        '<div class="health-strip"><b>Market · same for every stock</b>'
        + "".join(_mc_parts) + "</div>",
        unsafe_allow_html=True,
    )
except Exception as _mc_err:
    _log.debug("market context strip render failed: %s", _mc_err)

from dashboard.shared.disclosures import (  # noqa: E402
    render_score_methodology as _render_score_methodology,
    render_regime_reliability_note as _render_regime_note,
)


# ─────────────────────────────────────────────────────────────────────────────
# SPEED FIX (post FV-enrichment slowdown): the Final-Verdict banner at the top
# of the page and the Investment-Thesis section near the bottom each call
# generate_thesis()/assess_valuation() with slightly different inputs. On the
# SAME rerun both fire; on rerun of the SAME TICKER the whole page reruns and
# both fire again. The underlying fundamentals TTL cache (24h) makes the fetch
# cheap after the first hit, but the rule engines themselves are not free.
#
# These helpers memoise thesis + valuation-decision at the page level, keyed
# on ticker + a small fingerprint that captures the input flavour (banner uses
# composite only; bottom uses composite + deep + liquidity). Result: a rerun
# of the same ticker (period change, popover open, checkbox toggle) is instant
# on both surfaces, and the two thesis calls in a single run cache-hit each
# other whenever their input fingerprint matches.
# ─────────────────────────────────────────────────────────────────────────────
@st.cache_data(ttl=300, show_spinner=False)
def _cached_thesis_banner(ticker: str, cs_score: float, cs_action: str):
    """Banner-flavour thesis (composite only). Cached per (ticker, cs)."""
    from analysis.thesis import generate_thesis, build_inputs
    # cs is required by build_inputs; the caller passes the object separately
    # and we reconstruct via st.session_state to avoid an un-hashable arg.
    _cs = st.session_state.get("_as_cs_snap")
    return generate_thesis(build_inputs(ticker, composite=_cs))


@st.cache_data(ttl=300, show_spinner=False)
def _cached_thesis_full(ticker: str, cs_score: float, cs_action: str,
                        dc_total, liq_tier):
    """Bottom-flavour thesis (composite + deep + liquidity). dc_total / liq_tier
    are cheap hashable fingerprints — the full objects come from session_state
    so streamlit's cache_data can hash the key."""
    from analysis.thesis import generate_thesis, build_inputs
    _cs  = st.session_state.get("_as_cs_snap")
    _dc  = st.session_state.get("_as_dc_snap")
    _liq = st.session_state.get("_as_liq_snap")
    return generate_thesis(build_inputs(ticker, composite=_cs, deep=_dc, liquidity=_liq))


@st.cache_data(ttl=300, show_spinner=False)
def _cached_valuation(ticker: str, sector: str | None,
                      company_name: str | None = None):
    """Cached valuation-decision. Reuses the fundamentals TTL cache under it.

    Uses the SAME wiring as the (now-fixed) banner block below:
      * sector_classification.classify_sector (NOT get_sector_profile — that
        alias never existed and made the try-block silently return "insufficient
        evidence" for every ticker before the FV-BANNER-IMPORT fix)
      * fundamentals.analytics.compute_all → assess_valuation (analytics=None
        was the FV-BANNER-ANALYTICS bug — PEG/ROE-check inputs were missing so
        the engine abstained on every ticker)
    """
    from analysis.fundamentals.valuation import build_valuation_context
    from analysis.fundamentals.valuation_decision import assess_valuation
    from analysis.sector_classification import classify_sector
    from analysis.fundamentals import analytics as _fa
    _cf = _fund_service().get_fundamentals(ticker)
    if _cf is None:
        return None, None
    _spv = classify_sector(sector, name=company_name) if sector else None
    _val = build_valuation_context(_cf, sector_profile=_spv)
    _an  = _fa.compute_all(_cf)
    _va  = assess_valuation(_val, _an, _spv, cf=_cf)
    return _va, _cf


@st.cache_data(ttl=300, show_spinner=False)
def _cached_fund_card(ticker: str, sector: str | None, company_name: str | None = None):
    """Plain-dict inputs for the Overview's sector-aware fundamentals card.

    Metric applicability comes from analysis.sector_classification (CLAUDE.md
    rule 3): lenders get P/B + ROE highlighted and no leverage / EV/EBITDA
    rows; everyone else gets P/E (+ EV/EBITDA where meaningful).
    """
    from analysis.fundamentals.valuation import build_valuation_context
    from analysis.sector_classification import classify_sector
    _cf = _fund_service().get_fundamentals(ticker)
    if _cf is None:
        return None
    _sp = classify_sector(sector, name=company_name)
    _val = build_valuation_context(_cf, sector_profile=_sp)
    _an = _fund_analytics.compute_all(_cf, cagr_years=5)

    def _v(key):
        _r = _an.get(key)
        if _r is not None and getattr(_r, "available", False) and _r.value is not None:
            return float(_r.value)
        return None

    return {
        "group": _sp.group, "is_financial": bool(_sp.is_financial),
        "preferred": _val.preferred_valuation or _sp.preferred_valuation,
        "note": _sp.note, "provider": _cf.provider_name,
        "pe": _val.pe, "pb": _val.pb,
        "ev_ebitda": _val.ev_ebitda if (_val.ev_ebitda_applicable and _sp.ev_ebitda_meaningful) else None,
        "ev_ok": bool(_val.ev_ebitda_applicable and _sp.ev_ebitda_meaningful),
        "roe": _v("roe"), "roce": _v("roce"), "roce_ok": bool(_sp.roce_meaningful),
        "de": _v("debt_to_equity"), "de_ok": bool(_sp.leverage_warning_applies),
        "rev_cagr": _v("revenue_cagr"), "eps_cagr": _v("eps_cagr"),
    }


def _overview_chart(df):
    """Six-month close with 50 / 200 DMA for the Overview card (artboard 02).
    The full candlestick / volume / RSI / MACD chart stays on the Chart tab."""
    import plotly.graph_objects as go
    from dashboard.shared.tokens import COLORS, rgba

    d = df.tail(126)
    close = d["Close"].astype(float)
    floor = float(close.min()) * 0.985
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=d.index, y=[floor] * len(d), mode="lines",
                             line=dict(width=0), hoverinfo="skip", showlegend=False))
    fig.add_trace(go.Scatter(x=d.index, y=close, mode="lines", name="Close",
                             line=dict(color=COLORS["accent"], width=1.8),
                             fill="tonexty", fillcolor=rgba("accent", 0.10)))
    if "SMA_50" in d.columns and d["SMA_50"].notna().any():
        fig.add_trace(go.Scatter(x=d.index, y=d["SMA_50"], mode="lines", name="50 DMA",
                                 line=dict(color=COLORS["dim"], width=1.4)))
    if "SMA_200" in d.columns and d["SMA_200"].notna().any():
        fig.add_trace(go.Scatter(x=d.index, y=d["SMA_200"], mode="lines", name="200 DMA",
                                 line=dict(color=COLORS["faint"], width=1.4, dash="dot")))
    fig.add_trace(go.Scatter(x=[d.index[-1]], y=[float(close.iloc[-1])], mode="markers",
                             marker=dict(color=COLORS["accent"], size=7),
                             hoverinfo="skip", showlegend=False))
    grid = "rgba(255,255,255,0.05)"
    fig.update_layout(
        height=240, margin=dict(l=8, r=8, t=6, b=4),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="JetBrains Mono, monospace", size=10, color=COLORS["dim"]),
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.0, x=0, font=dict(size=10)),
        xaxis=dict(showgrid=False, zeroline=False),
        yaxis=dict(gridcolor=grid, zeroline=False, side="right", tickformat=",.0f"),
    )
    return fig


def _rs(value: float) -> str:
    """Rupee price: paise below ₹1,000, whole rupees above."""
    return f"₹{value:,.2f}" if abs(value) < 1000 else f"₹{value:,.0f}"


def _tqs_band(tqs: float) -> str:
    """Descriptive TQS band (the scanner's own signal() labels include
    "AVOID", which reads as an instruction on this page)."""
    if tqs >= 75:
        return "strong trend"
    if tqs >= 60:
        return "trending"
    if tqs >= 45:
        return "neutral"
    if tqs >= 30:
        return "weak"
    return "no trend"

# FIX A8: consume a ticker handed off from My Portfolio (or elsewhere) via
# session_state — must happen BEFORE the search widgets render so the
# selectbox/text_input default values stay untouched (we don't fight the
# widget state, we just override the *resolved* ticker and force the run).
_prefill_ticker = st.session_state.pop("analyze_ticker", None)
_prefill_active = bool(_prefill_ticker)

# ── Stock search ───────────────────────────────────────────────────────────
search_options = [
    f"{name}  ({sym.replace('.NS','')})"
    for name, sym in STOCK_SEARCH_MAP.items()
]
search_options_sorted = sorted(search_options)

_AS_PERIOD_MAP = {
    "1D": "1d", "5D": "5d", "1M": "1m",
    "6M": "6m", "YTD": "ytd", "Max": "max",
}
_AS_PLACEHOLDER = "— type to search —"

# BUGFIX: the dropdown and the manual ticker box were independent widgets
# with no relationship — picking a dropdown stock left old text sitting in
# the manual box (which silently took priority below), and typing a manual
# ticker left the dropdown showing a stale company name. Neither cleared on
# its own, so switching between the two required manually wiping whichever
# field you weren't using. These on_change callbacks make using one field
# automatically clear the other, and the explicit "✖ Clear" button below
# resets both at once.
#
# The clear-pending flag (rather than writing the widget keys directly from
# the button block) is required because Streamlit raises
# "cannot be modified after the widget ... is instantiated" if you assign to
# st.session_state for a widget's key anywhere after that widget has already
# been created in the same script run — and the Clear button sits below the
# selectbox/text_input in this layout. Setting a flag + st.rerun() defers the
# actual reset to the top of the next run, before either widget exists yet.
if st.session_state.pop("_as_clear_pending", False):
    st.session_state["stock_search_select"] = _AS_PLACEHOLDER
    st.session_state["manual_ticker_input"] = ""

def _as_on_dropdown_change():
    if st.session_state.get("stock_search_select", _AS_PLACEHOLDER) != _AS_PLACEHOLDER:
        st.session_state["manual_ticker_input"] = ""

def _as_on_manual_change():
    if st.session_state.get("manual_ticker_input", "").strip():
        st.session_state["stock_search_select"] = _AS_PLACEHOLDER

col_search, col_manual, col_clear, col_btn = st.columns([3, 2, 1, 1])
with col_search:
    selected_option = st.selectbox(
        "Search by company name or symbol",
        options=[_AS_PLACEHOLDER] + search_options_sorted,
        index=0,
        key="stock_search_select",
        on_change=_as_on_dropdown_change,
    )
with col_manual:
    manual_ticker = st.text_input(
        "Or type ticker directly",
        value="",
        placeholder="e.g. INFY or INFY.NS",
        key="manual_ticker_input",
        on_change=_as_on_manual_change,
    ).strip().upper()
with col_clear:
    st.write("")
    st.write("")
    if st.button("✖ Clear", key="as_clear_search", width="stretch"):
        st.session_state["_as_clear_pending"] = True
        st.rerun()
with col_btn:
    st.write("")
    st.write("")
    analyze_btn = st.button("🔍 Analyze", type="primary", key="analyze_btn")

if _prefill_active:
    st.caption(f"📥 Opened from My Portfolio — analyzing **{_prefill_ticker.replace('.NS','')}**.")

_mt_clean, _mt_err = _validate_ticker(manual_ticker)
if _mt_err:
    st.error(f"⚠️ {_mt_err}")
    st.stop()

ticker = ""
if _prefill_active:
    # FIX A8: a handed-off ticker always wins over stale widget state
    ticker = _prefill_ticker if _prefill_ticker.endswith(".NS") else _prefill_ticker + ".NS"
elif _mt_clean:
    ticker = _mt_clean + ".NS"
elif selected_option != _AS_PLACEHOLDER:
    raw_sym = selected_option.rsplit("(", 1)[-1].rstrip(")")
    ticker  = raw_sym + ".NS" if not raw_sym.endswith(".NS") else raw_sym

if not ticker:
    # FIX A9: fall back to whatever was last analyzed, instead of jumping
    # straight to the RELIANCE.NS default. This is required once the search
    # boxes auto-clear right after a successful Analyze (see FIX A9 below)
    # — without this fallback, the very next rerun after that (e.g. just
    # changing the chart period) would find both search widgets empty,
    # resolve to RELIANCE.NS, and silently swap away from the stock the
    # person just looked up.
    ticker = st.session_state.get("last_analyzed") or "RELIANCE.NS"

if analyze_btn or _prefill_active or (
    "last_analyzed" in st.session_state
    and st.session_state.last_analyzed == ticker
):
    st.session_state.last_analyzed = ticker
    # Recent-tickers ring buffer (5 max) — feeds the sidebar command bar
    # (dashboard/shared/nav._render_command_bar) with quick-jump chips
    # when the search box is empty. Newest first; dedup on ticker.
    _rt = st.session_state.get("__recent_tickers", [])
    _sym = ticker.replace(".NS", "")
    _rt = [_sym] + [t for t in _rt if t != _sym]
    st.session_state["__recent_tickers"] = _rt[:5]

    if analyze_btn:
        # FIX A9: the search boxes only ever cleared when explicitly
        # switching fields or clicking "✖ Clear" — never after actually
        # using them. Every time someone finished analyzing one stock,
        # the manual box still held the old ticker, so typing the next
        # search meant deleting the old text first. Clearing here, right
        # when Analyze is clicked, fixes that. get_composite_score is
        # cached, so recomputing for the same ticker on the next rerun
        # (see the FIX A9 fallback above) is cheap — the immediate
        # st.rerun() is required because the search widgets were already
        # instantiated earlier in *this* run, so they can't be blanked
        # until the top of the *next* run (the "_as_clear_pending" block
        # near the top of this file consumes the flag right before the
        # widgets are created).
        st.session_state["_as_clear_pending"] = True
        st.rerun()

    # F6 · loading skeleton preview above the spinner. Scoring can take
    # 3–10 s on a cold cache; the skeleton renders the verdict-card shape
    # so users see what's arriving instead of a naked spinner over blank.
    _skel_ph = st.empty()
    try:
        from dashboard.shared.ui_components import loading_skeleton as _f6_skel
        _skel_ph.markdown(_f6_skel(kind="hero"), unsafe_allow_html=True)
    except Exception:
        pass
    with st.spinner(f"Scoring {ticker}…"):

        try:
            cs = get_composite_score(ticker)
            _skel_ph.empty()

            # BUGFIX: get_composite_score() already catches fetch failures
            # internally and returns an UNAVAILABLE sentinel rather than
            # raising — but this page never checked for it, so execution kept
            # going straight into load_ticker_df(ticker) below, which has no
            # such protection and raises a raw
            # "ValueError: No data for X.NS. All sources failed: [...]" that
            # fell through to the generic exception handler at the bottom,
            # dumping a Python traceback at the user. An invalid/misspelled
            # ticker now gets a plain, friendly message and stops here.
            if cs.action == "UNAVAILABLE":
                st.error(
                    f"❌ **Couldn't find '{ticker.replace('.NS','')}' on NSE.** "
                    "Double-check the spelling, or search by company name above "
                    "(e.g. RELIANCE, INFY, TCS)."
                )
                st.stop()

            # ── Task 3.3: TQS x valuation sidecar overlay ───────────────────
            # Populate cs.overlay_score BEFORE the verdict card renders so it
            # picks up the Quality x Value stat in its footer row. Both
            # subsystems are Streamlit-cached elsewhere on the page, so this
            # is a warm call, not an extra fetch storm. Failures silently
            # leave overlay as None; the verdict card omits the stat cleanly.
            try:
                from analysis.score import compute_overlay_score as _ov_compute
                _ov_posture = None
                _ov_tqs     = None
                try:
                    _ov_va, _ = _cached_valuation(
                        ticker, getattr(cs, "sector", None), None)
                    if _ov_va is not None:
                        _ov_posture = getattr(_ov_va, "posture", None)
                except Exception as _ov_val_e:
                    import logging
                    logging.getLogger("dashboard.analyze_stock").debug(
                        "overlay: valuation fetch failed: %s", _ov_val_e)
                try:
                    from analysis.trend_quality_score import score_ticker as _ov_tqs_fn
                    _ov_tqs_res = _ov_tqs_fn(ticker, period="1y", last_n=1)
                    if hasattr(_ov_tqs_res, "tqs"):
                        _ov_tqs = float(_ov_tqs_res.tqs)
                except Exception as _ov_tqs_e:
                    import logging
                    logging.getLogger("dashboard.analyze_stock").debug(
                        "overlay: TQS fetch failed: %s", _ov_tqs_e)
                cs.overlay_score = _ov_compute(_ov_tqs, _ov_posture)
            except Exception as _ov_outer_e:
                import logging
                logging.getLogger("dashboard.analyze_stock").debug(
                    "overlay populate failed: %s", _ov_outer_e)

            # ════════════════════════════════════════════════════════════════
            # COMPUTE — everything the head, Overview and tabs read. Nothing
            # renders until the ticker head below.
            # ════════════════════════════════════════════════════════════════

            # Your position, if this ticker is in the My Portfolio holdings.
            # (load_manual_holdings returns a list of dicts since FIX A9.)
            _pctx = None
            try:
                _short = ticker.replace(".NS", "")
                for _h in load_manual_holdings() or []:
                    if str(_h.get("ticker", "")).upper().replace(".NS", "") == _short:
                        _hq = int(float(_h.get("quantity", 0) or 0))
                        _havg = float(_h.get("avg_buy_price", _h.get("avg_price", 0)) or 0.0)
                        if _hq > 0:
                            _pctx = {"shares_held": _hq, "avg_price": _havg}
                        break
            except Exception as _hp_e:
                _log.debug("portfolio ctx unavailable: %s", _hp_e)

            # Live price
            _an_live = None
            try:
                from utils.live_price import get_live_quote as _an_lq
                _anq = _an_lq(ticker)
                if isinstance(_anq, dict) and _anq.get("price"):
                    _an_live = float(_anq["price"])
            except Exception as _lq_e:
                import logging; logging.getLogger("dashboard.analyze_stock").debug("Live quote fetch failed for %s: %s", ticker, _lq_e)
            _an_drift = (
                abs(_an_live - cs.price) / cs.price * 100
                if (_an_live and cs.price)
                else 0.0
            )

            df = load_ticker_df(ticker)

            # FIX A7: guard against single-row dataframe (new listings / data gaps)
            if len(df) < 2:
                st.error(
                    f"⚠️ Insufficient price history for **{ticker.replace('.NS','')}** "
                    f"({len(df)} row(s) returned). The stock may be newly listed or "
                    "data is temporarily unavailable. Try again later."
                )
                st.stop()

            latest = df.iloc[-1]
            prev   = df.iloc[-2]   # safe — len(df) >= 2 guarded above

            # ── FinalVerdict: one combined read + the three horizon lenses ──
            # FIX FV-BANNER / FV-HORIZON / FV-BANNER-RICHER: same inputs and
            # aggregator as before; only the rendering moved into Overview.
            # The focus horizon comes from the Overview radio's session key.
            _fv = None
            _fv_va = None
            _fv_th = None
            _fv_tqs = None
            _fv_tqs_res = None
            _hz_verdicts = {}
            _fv_horizon = st.session_state.get("_fv_horizon", "medium")
            if _fv_horizon not in ("short", "medium", "long"):
                _fv_horizon = "medium"
            try:
                from analysis.final_verdict import combine as _fv_combine
                try:
                    from analysis.trend_quality_score import score_ticker as _fv_tqs_score
                    _fv_tqs_res = _fv_tqs_score(ticker, period="1y")
                    if hasattr(_fv_tqs_res, "tqs"):
                        _fv_tqs = float(_fv_tqs_res.tqs)
                    else:
                        _fv_tqs_res = None
                except Exception as _fv_tqs_e:
                    _fv_tqs_res = None
                    _log.debug("FinalVerdict TQS fetch failed for %s: %s", ticker, _fv_tqs_e)

                # FIX FV-BANNER-RICHER — pull valuation posture, thesis verdict,
                # and qualitative-flag severity up here so the top-of-page
                # verdict banner uses the FULL 5-gate read rather than reading
                # "insufficient data" for three of them. Each fetch is guarded
                # so a single failure still leaves that ONE gate as unknown;
                # confidence tracking then downgrades from "high" to "medium"
                # or "low" honestly.
                _fv_valuation = None
                _fv_val_guard  = None      # populated when posture is INSUFFICIENT_EVIDENCE
                _fv_val_reason = None
                _fv_thesis_verdict = None
                _fv_thesis_score   = None
                _fv_quality_score  = None
                _fv_quality_flags  = None
                _fv_th = None
                _fv_cf = None
                # SPEED FIX: route through page-level cached helpers so the
                # SAME banner on a rerun (period change, popover open, checkbox
                # toggle) is instant, and the fundamentals TTL cache is the
                # only thing that touches the network on a cold ticker.
                st.session_state["_as_cs_snap"] = cs   # for _cached_thesis_*
                try:
                    # SPEED FIX + FV-BANNER-{IMPORT,ANALYTICS}: route through the
                    # cached helper — it applies BOTH bug fixes (classify_sector
                    # instead of the non-existent get_sector_profile, and passing
                    # a real analytics dict so the engine doesn't abstain on
                    # every ticker) and memoises the whole computation, so a
                    # rerun of the same ticker is instant.
                    _fv_va, _fv_cf = _cached_valuation(
                        ticker, cs.sector,
                        company_name=getattr(cs, "company_name", None),
                    )
                    if _fv_va and getattr(_fv_va, "posture", None):
                        _fv_valuation  = _fv_va.posture
                        # When the engine intentionally abstained, capture the
                        # guard code + justification so the banner can explain
                        # WHY (e.g. cyclical trough) instead of the generic
                        # "insufficient evidence". Wired through combine().
                        _fv_val_guard  = getattr(_fv_va, "triggered_guard", None)
                        _fv_val_reason = getattr(_fv_va, "justification", None)
                except Exception as _fv_val_e:
                    import logging
                    logging.getLogger("dashboard.analyze_stock").debug(
                        "FinalVerdict valuation fetch failed for %s: %s", ticker, _fv_val_e)
                try:
                    # SPEED FIX + FV-BANNER-KWARG: cached helper already passes
                    # composite=cs (the fix from origin/main), and memoises so a
                    # rerun of the same ticker skips the engine entirely.
                    _fv_th = _cached_thesis_banner(ticker, cs.score, cs.action)
                    if _fv_th:
                        _fv_thesis_verdict = getattr(_fv_th, "verdict", None)
                        _fv_thesis_score   = getattr(_fv_th, "verdict_score", None)
                except Exception as _fv_th_e:
                    import logging
                    logging.getLogger("dashboard.analyze_stock").debug(
                        "FinalVerdict thesis fetch failed for %s: %s", ticker, _fv_th_e)
                try:
                    # SPEED FIX: qualitative-flags fetch does a live NSE + Google
                    # News + RSS scrape on cache miss (~2–3s wall-clock) — that
                    # is the biggest single cold-render cost the FV enrichment
                    # introduced. The flag strip further down the page fires the
                    # SAME get_cached_flags call anyway, so on a cold ticker we
                    # skip it in the banner (banner renders with flags-gate
                    # unknown → confidence downgrades from high to medium, which
                    # is honest) and only include it once the strip has warmed
                    # the 6h cache; the next rerun of the same ticker (period
                    # change, popover open) then gets the full 5-gate verdict
                    # essentially for free.
                    _flags_warm_key = f"_as_flags_warm::{ticker}"
                    from dashboard.shared.flags_ui import get_cached_flags as _gcf
                    _fv_flag_dicts = (
                        _gcf(ticker, company_name=getattr(cs, "company_name", None)) or []
                        if st.session_state.get(_flags_warm_key)
                        else []
                    )
                    if _fv_flag_dicts:
                        _sevs = {str(f.get("sentiment", "")).lower() for f in _fv_flag_dicts}
                        # QualitativeFlag.FlagSentiment uses "negative"/"positive"/"neutral";
                        # map to the {red, amber, green} shape _gate_quality expects.
                        if "negative" in _sevs:
                            _sev_final = "red"
                        elif "neutral" in _sevs:
                            _sev_final = "amber"
                        else:
                            _sev_final = "green"
                        _top = str((_fv_flag_dicts[0].get("message") or
                                    _fv_flag_dicts[0].get("headline") or ""))[:80]
                        _fv_quality_flags = {"severity": _sev_final, "top_flag": _top}
                except Exception as _fv_qf_e:
                    import logging
                    logging.getLogger("dashboard.analyze_stock").debug(
                        "FinalVerdict qualitative-flags fetch failed for %s: %s", ticker, _fv_qf_e)
                try:
                    # analysis.portfolio_fundamentals.compute_quality_score
                    # takes a fundamentals DICT, not a ticker — so reuse the
                    # cached fundamentals we already fetched for the valuation
                    # branch above rather than fetching a second time.
                    from analysis.portfolio_fundamentals import compute_quality_score as _cqs
                    if _fv_cf is not None:
                        # compute_quality_score reads roe / roce / revenue_cagr_5y /
                        # eps_cagr_5y (percent). Build that dict from the
                        # fundamentals analytics layer (AnalyticResult values are
                        # already in %). The old .to_metrics_dict() call pointed at
                        # a method that never existed, so quality_score was always None.
                        from analysis.fundamentals import analytics as _fa_q
                        _fv_an = _fa_q.compute_all(_fv_cf)
                        _fv_key_map = {"roe": "roe", "roce": "roce",
                                       "revenue_cagr": "revenue_cagr_5y",
                                       "eps_cagr": "eps_cagr_5y"}
                        _fv_metrics = {}
                        for _k_src, _k_dst in _fv_key_map.items():
                            _r = _fv_an.get(_k_src)
                            if _r is not None and getattr(_r, "available", False)                                     and getattr(_r, "value", None) is not None:
                                _fv_metrics[_k_dst] = float(_r.value)
                        if _fv_metrics:
                            _fv_qs = _cqs(_fv_metrics)
                            if _fv_qs and _fv_qs > 0:
                                _fv_quality_score = float(_fv_qs)
                except Exception as _fv_qs_e:
                    import logging
                    logging.getLogger("dashboard.analyze_stock").debug(
                        "FinalVerdict quality-score fetch failed for %s: %s", ticker, _fv_qs_e)

                _fv = _fv_combine(
                    composite_score=cs.score,
                    composite_action=cs.action,
                    tqs=_fv_tqs,
                    quality_score=_fv_quality_score,
                    quality_flags=_fv_quality_flags,
                    valuation_posture=_fv_valuation,
                    valuation_guard=_fv_val_guard,
                    valuation_guard_reason=_fv_val_reason,
                    thesis_verdict=_fv_thesis_verdict,
                    thesis_score=_fv_thesis_score,
                    horizon=_fv_horizon,
                )
                for _hz in ("short", "medium", "long"):
                    _hz_verdicts[_hz] = _fv_combine(
                        composite_score=cs.score,
                        composite_action=cs.action,
                        tqs=_fv_tqs,
                        quality_score=_fv_quality_score,
                        quality_flags=_fv_quality_flags,
                        valuation_posture=_fv_valuation,
                        valuation_guard=_fv_val_guard,
                        valuation_guard_reason=_fv_val_reason,
                        thesis_verdict=_fv_thesis_verdict,
                        thesis_score=_fv_thesis_score,
                        horizon=_hz,
                    )

                # ── VERDICT LEDGER — silent auto-log (Tier 1 #1/#2) ─────────
                # Every FinalVerdict the user sees on this page is persisted so
                # we can compute forward returns and calibration later. Idempotent
                # per (date, ticker, horizon, source) via a UNIQUE index — visiting
                # the same page 20 times a day writes ONE row per horizon. Failure
                # never surfaces (logger swallows and continues).
                try:
                    from analysis.verdict_ledger import log_verdict as _vl_log
                    # ── SIGNAL-TAG DERIVATION (Tier 1 #3) ──────────────
                    # Build a compact list of every sub-signal that fired for
                    # this verdict. The Calibration page grades each tag's own
                    # forward return so we can see WHICH rules are earning
                    # their weight — patterns like "BullEngulfing" vs
                    # thresholds like "tech_high" vs regime tags like
                    # "vix_calm". Small list, one row per tag in a linked
                    # table, indexed for fast group-by.
                    _tags: list = []
                    for _p in (getattr(cs, "patterns_detected", []) or []):
                        _tags.append(f"pattern.{str(_p)[:40]}")
                    # FIX BEAR-NEUTRAL: tag on the raw technical points so the
                    # ledger still sees tech_high/low in bear regimes.
                    _tech_raw = getattr(cs, "technical_raw", None)
                    _tech = float(_tech_raw if _tech_raw is not None
                                  else (getattr(cs, "technical_score", 0) or 0))
                    _mom  = float(getattr(cs, "momentum_score",  0) or 0)
                    _vol  = float(getattr(cs, "volume_score",    0) or 0)
                    _sent = float(getattr(cs, "sentiment_score", 0) or 0)
                    _rsi  = float(getattr(cs, "rsi",             50) or 50)
                    if _tech >= 32: _tags.append("tech_high")
                    elif _tech <= 15: _tags.append("tech_low")
                    if _mom  >= 18: _tags.append("momentum_high")
                    elif _mom  <= 8:  _tags.append("momentum_low")
                    if _vol  >= 11: _tags.append("volume_surge")
                    elif _vol  <= 4:  _tags.append("volume_weak")
                    if _sent >= 7:  _tags.append("sentiment_positive")
                    elif _sent <= 3:  _tags.append("sentiment_negative")
                    if _rsi  <= 30: _tags.append("rsi_oversold")
                    elif _rsi  >= 70: _tags.append("rsi_overbought")
                    _vixr = getattr(cs, "vix_regime", "") or ""
                    if _vixr: _tags.append(f"vix.{str(_vixr).lower()}")
                    if _fv_tqs is not None:
                        if _fv_tqs >= 60: _tags.append("tqs_strong")
                        elif _fv_tqs < 25: _tags.append("tqs_weak")
                    if _fv_valuation:
                        _tags.append(f"val.{str(_fv_valuation).lower()}")
                    if _fv_quality_flags and _fv_quality_flags.get("severity"):
                        _tags.append(f"flags.{_fv_quality_flags['severity']}")

                    _vl_log(ticker=ticker, final_verdict=_fv,
                            entry_price=float(getattr(cs, "entry", 0.0) or 0.0) or None,
                            composite_score=float(getattr(cs, "score", 0.0) or 0.0) or None,
                            thesis_score=_fv_thesis_score,
                            signal_tags=_tags,
                            source="analyze_page", horizon=_fv_horizon)
                except Exception as _vl_e:
                    import logging
                    logging.getLogger("dashboard.analyze_stock").debug(
                        "verdict_ledger log_verdict failed for %s: %s", ticker, _vl_e)
            except Exception as _fv_err:
                _fv = None
                _log.debug("FinalVerdict failed for %s: %s", ticker, _fv_err)

            # ── Multi-signal confirmation ──────────────────────────────────
            # F6 · skeleton — deep confirmation runs MTF + regime + hedge probes.
            _dc_skel = st.empty()
            try:
                from dashboard.shared.ui_components import (
                    loading_skeleton as _f6_skel_dc,
                )
                _dc_skel.markdown(_f6_skel_dc(kind="table"),
                                  unsafe_allow_html=True)
            except Exception:
                pass
            with st.spinner("Running deep confirmation…"):
                _dc = _deep_confirmation(ticker)
            _dc_skel.empty()

            # LAYOUT-REORDER: compute the liquidity context here too, so the
            # Investment Thesis section (moved up to run BEFORE Fundamentals
            # / Valuation / Liquidity render, since it's the "why", they're
            # the "context") can reference it. The Liquidity render block
            # further down reuses this same object — no double computation.
            _liq_ctx = None
            try:
                from analysis.liquidity import compute_liquidity as _cl_early
                _liq_ctx = _cl_early(df)
            except Exception as _liq_early_e:
                import logging
                logging.getLogger("dashboard.analyze_stock").debug(
                    "early liquidity compute failed for %s: %s", ticker, _liq_early_e)

            _wk_map = {
                "uptrend":  ("🟢 Uptrend",  "var(--bull)"),
                "downtrend":("🔴 Downtrend","var(--bear)"),
                "sideways": ("🟡 Sideways", "var(--amber)"),
                None:       ("—",           "var(--dim)"),
            }
            _wk_txt, _wk_c = _wk_map.get(_dc["weekly"], ("—", "var(--dim)"))
            _rs_c   = "var(--bull)" if (_dc["rs_pct"] or 0) > 0 else "var(--bear)"
            _rs_txt = (
                f'{_dc["rel_strength"].title()} ({_dc["rs_pct"]:+.1f}% vs Nifty)'
                if _dc["rel_strength"]
                else "—"
            )

            # FIX A4: handle negative _ed_days (results already announced)
            _ed_days = _dc["earnings_days"]
            # P2 · earnings status now renders through the shared chip_pill
            # (signal-badge) so it reads like every other status chip. Shape
            # glyph per state so urgency doesn't depend on colour alone.
            from dashboard.shared.ui_components import chip_pill as _an_chip
            if _ed_days is not None and _ed_days < 0:
                _ed_txt, _ed_tone = f"✓ Results {abs(_ed_days)}d ago", "neutral"   # event passed
            elif _ed_days is not None and 0 <= _ed_days <= 7:
                _ed_txt, _ed_tone = f"⚠ Results in {_ed_days}d · event risk", "bad"
            elif _ed_days is not None and 0 <= _ed_days <= 21:
                _ed_txt, _ed_tone = f"◆ Results in {_ed_days}d", "warn"
            elif _ed_days is not None:
                _ed_txt, _ed_tone = f"● Results in {_ed_days}d (clear)", "good"
            else:
                _ed_txt, _ed_tone = "Unknown", "neutral"
            _ed_badge = _an_chip(_ed_txt, _ed_tone)

            # FIX A3: guard against _dc["total"] being None or 0
            _bull = _dc.get("bull", 0)
            _tot  = _dc.get("total") or 0
            _confirmation_available = _tot > 0

            # Conviction score
            _conf_delta, _conf_reasons = 0, []
            if _confirmation_available:
                _agr_pct = _bull / _tot * 100
                _agr_c   = "var(--bull)" if _agr_pct >= 67 else "var(--amber)" if _agr_pct >= 40 else "var(--bear)"

                if _dc["weekly"] == "uptrend":
                    _conf_delta += 4;  _conf_reasons.append("+4 weekly uptrend")
                elif _dc["weekly"] == "downtrend":
                    _conf_delta -= 6;  _conf_reasons.append("−6 weekly downtrend")
                if _dc["rs_pct"] is not None:
                    if _dc["rs_pct"] > 3:
                        _conf_delta += 4; _conf_reasons.append(f"+4 leads Nifty ({_dc['rs_pct']:+.1f}%)")
                    elif _dc["rs_pct"] < -3:
                        _conf_delta -= 4; _conf_reasons.append(f"−4 lags Nifty ({_dc['rs_pct']:+.1f}%)")
                if _ed_days is not None and 0 <= _ed_days <= 7:
                    _conf_delta -= 6; _conf_reasons.append(f"−6 earnings in {_ed_days}d")
                if _agr_pct >= 80:
                    _conf_delta += 5; _conf_reasons.append(f"+5 strong agreement ({_bull}/{_tot})")
                elif _agr_pct <= 40:
                    _conf_delta -= 5; _conf_reasons.append(f"−5 weak agreement ({_bull}/{_tot})")
                _conf_delta = max(-15, min(15, _conf_delta))
                _conviction = max(0, min(100, cs.score + _conf_delta))
            else:
                # FIX A3: confirmation unavailable — use raw score, no adjustment
                _agr_pct    = 0
                _agr_c      = "var(--dim)"
                _conviction = cs.score
                _conf_delta = 0

            _cv_c    = "var(--bull)" if _conviction >= 65 else "var(--amber)" if _conviction >= 45 else "var(--bear)"
            _delta_c = "var(--bull)" if _conf_delta >= 0 else "var(--bear)"
            _delta_s = f"{_conf_delta:+d}" if _conf_delta else "±0"

            # Market status for the price note in the head.
            _ms_an = {}
            try:
                from utils.market_hours import market_status as _an_ms
                _ms_an = _an_ms() or {}
            except Exception as _ms_e:
                _log.debug("Market status check failed: %s", _ms_e)
            try:
                _dlabel = df.index[-1].strftime("%d-%b")
            except Exception:
                _dlabel = ""

            # ════════════════════════════════════════════════════════════════
            # RENDER — ticker head (artboard 02)
            # ════════════════════════════════════════════════════════════════
            _sym = ticker.replace(".NS", "")
            _company = str(getattr(cs, "company_name", "") or get_display_name(ticker) or _sym)
            _disp_price = _an_live if _an_live else cs.price
            _day_abs = float(latest["Close"]) - float(prev["Close"])
            day_chg = (latest["Close"] / prev["Close"] - 1) * 100
            _co_line = f"{_company} · NSE"
            if _pctx:
                _co_line += (f" · you hold {_pctx['shares_held']:,} @ "
                             f"₹{_pctx['avg_price']:,.2f}")
            _head_tags = [
                str(cs.sector) if getattr(cs, "sector", None) else "",
                # FIX A6: guard against cs.sector_rank being None
                f"Sector rank #{cs.sector_rank}" if getattr(cs, "sector_rank", None) else "",
                "F&amp;O" if getattr(cs, "is_fno", False) else "",
                (f"Liquidity · {_liq_ctx.liquidity_tier}"
                 if getattr(_liq_ctx, "liquidity_tier", None) else ""),
            ]
            if _ms_an.get("is_open"):
                _px_note = ("Live · official close settles after 3:30 PM" if _an_live
                            else "Market open · last daily close")
            else:
                _px_note = f"Settled EOD close{f' · {_dlabel}' if _dlabel else ''}"

            _hd_l, _hd_r = st.columns([9, 5], vertical_alignment="bottom")
            with _hd_l:
                st.markdown(
                    stock_head(_sym, _co_line, _head_tags, _disp_price,
                               chg=_day_abs, chg_pct=float(day_chg), note=_px_note),
                    unsafe_allow_html=True,
                )
                # Trading-status caveats (audit #20): new listing, no recent
                # trades, circuit lock, trade-to-trade / SME series. Display only.
                from dashboard.shared.edge_flags_ui import render_edge_flags
                render_edge_flags(ticker, df)
            with _hd_r:
                with st.container(key="as_actions"):
                    _ac1, _ac2, _ac3, _ac4 = st.columns([1.15, 1.15, 1.25, 0.45])
                    if _ac1.button("Watchlist", icon=":material/add:", key=f"as_wl_{ticker}", width="stretch"):
                        _wl = st.session_state.setdefault("watchlist", [])
                        if ticker not in _wl:
                            _wl.append(ticker)
                        st.toast(f"{_sym} added to watchlist ✓")
                    with _ac2:
                        # FIX A1: the shared popover (account selector, live
                        # re-anchor, qty override, R:R display).
                        _paper_trade_popover(
                            ticker,
                            entry   = cs.entry,
                            sl      = cs.stop_loss,
                            tp      = cs.target,
                            reason  = f"{_display_label(cs.action)} score={cs.score:.0f}/90: {cs.headline}",
                            key     = f"as_ptpop_{ticker}",
                            label   = "Paper Trade",
                        )
                    if _ac3.button("Size Position", key=f"as_size_{ticker}",
                                   type="primary", width="stretch",
                                   help="Open the Position Sizer with this stock's reference levels"):
                        st.session_state["ps_entry"] = round(float(cs.entry), 2)
                        st.session_state["ps_sl"] = round(float(cs.stop_loss), 2)
                        st.session_state["ps_tp"] = round(float(cs.target), 2)
                        st.switch_page("pages/12_position_sizer.py")
                    if _ac4.button("↻", key=f"as_re_{ticker}", width="stretch",
                                   help="Re-score: reload this ticker's price history"):
                        # FIX MKT3: clear only the price loader, not every
                        # page's cache.
                        load_ticker_df.clear()
                        st.rerun()

            # FIX A2: the drift banner only fires while the market is open
            # (outside hours Yahoo's "live" price is the last EOD close).
            try:
                if _ms_an.get("is_open") and _an_live and _an_drift >= 0.5:
                    _drift_key = f"_an_drift_dismiss_{ticker}_{int(_an_drift)}"
                    if not st.session_state.get(_drift_key):
                        from dashboard.shared.ui_components import degraded_banner as _an_banner
                        _db_c1, _db_c2 = st.columns([20, 1])
                        with _db_c1:
                            st.markdown(_an_banner(
                                f"Live price {_an_drift:.1f}% from last close",
                                detail=(
                                    f"Live <b>₹{_an_live:,.2f}</b> · indicators & levels "
                                    f"computed on the last daily close <b>₹{cs.price:,.2f}</b>"
                                    f"{f' ({_dlabel})' if _dlabel else ''}."
                                ),
                                fallback="Treat the reference levels as a guide until the close settles.",
                            ), unsafe_allow_html=True)
                        with _db_c2:
                            if st.button("✕", key=f"btn{_drift_key}",
                                         help="Dismiss for this ticker"):
                                st.session_state[_drift_key] = True
                                st.rerun()
            except Exception as _dr_e:
                _log.debug("drift banner failed: %s", _dr_e)

            # Tab strip. Overview is plain page flow (its horizon radio reruns
            # the whole page); every other tab body is an @st.fragment below
            # (Task 2.4 F3 PR C.2) so widgets inside a tab rerun only that tab.
            # Deep Dive now sits at the foot of Overview (audit Y4).
            (tab_ov, tab_fund, tab_chart, tab_thesis,
             tab_news, tab_ca, tab_pf) = st.tabs([
                "Overview", "Fundamentals", "Chart", "Thesis",
                "News & Flags", "Corp Actions", "Portfolio Fit",
            ])

            with tab_ov:
                # ── Posture hero with pillar bars ───────────────────────────
                # Descriptive posture noun (never buy/sell/hold, rule 1).
                # Thresholds match analysis/score.py's grade bands.
                _score = float(getattr(cs, "score", 0) or 0)
                if _score >= 70:
                    _posture, _qual, _tone = ("Constructive", "trend intact, momentum aligned", "good")
                elif _score >= 55:
                    _posture, _qual, _tone = ("Constructive", "with reservations", "accent")
                elif _score >= 40:
                    _posture, _qual, _tone = ("Neutral", "no clear direction on the tape", "neutral")
                elif _score >= 25:
                    _posture, _qual, _tone = ("Watchful", "trend structure weakening", "warn")
                else:
                    _posture, _qual, _tone = ("Weak", "trend structure broken", "bad")
                _narr = str(getattr(cs, "narrative", "") or "").strip()
                _hv_why = _narr.split("\n")[0] if _narr else ""
                if len(_hv_why) > 280:
                    # Cut at the last full sentence inside the budget, not mid-word.
                    _cut = _hv_why[:280].rfind(". ")
                    _hv_why = _hv_why[:_cut + 1] if _cut > 80 else _hv_why[:277].rstrip() + "…"
                if _hv_why:
                    _hv_why += " Descriptive read, not a recommendation."
                st.markdown(
                    hero_verdict(
                        posture=_posture,
                        posture_qualifier=_qual,
                        composite=_score,
                        max_score=90,
                        kicker="Posture · " + _company,
                        why=_hv_why,
                        tone=_tone,
                        pillars=[
                            ("Technical", getattr(cs, "technical_score", 0), 40),
                            ("Momentum",  getattr(cs, "momentum_score", 0), 25),
                            ("Volume",    getattr(cs, "volume_score", 0), 15),
                            ("Sentiment", getattr(cs, "sentiment_score", 0), 10),
                        ],
                    ),
                    unsafe_allow_html=True,
                )

                # One headline: the composite posture above. Confidence and the market-
                # regime caveat sit directly under it; every other lens is collapsed under
                # "Other lenses" below (audit Y4: eight verdicts on one page disagreed).
                _conf_bits = []
                if _fv is not None and getattr(_fv, "confidence", None):
                    _conf_bits.append(f"{str(_fv.confidence).lower()} data coverage")
                if _confirmation_available:
                    _conf_bits.append(f"{_bull} of {_tot} confirmation signals agree")
                if _conf_bits:
                    st.caption("Confidence · " + " · ".join(_conf_bits))
                _render_regime_note()

                # ── Context chips: earnings window + thesis freshness ───────
                _ctx_chips = [_ed_badge]
                try:
                    import trade_store as _tstore
                    import datetime as _dt_al
                    _dd_hist = _tstore.kv_get(f"deep_dive_history:{ticker}",
                                              default=None, user_id="default") or []
                    if _dd_hist:
                        _age_days = None
                        try:
                            _gen_dt = _dt_al.datetime.fromisoformat(
                                _dd_hist[-1].get("generated_at", ""))
                            _age_days = (_dt_al.datetime.now() - _gen_dt).days
                        except Exception:
                            pass
                        if _age_days is not None:
                            _fr_tone = ("good" if _age_days <= 90 else
                                        "warn" if _age_days <= 180 else "bad")
                            _fr_word = ("fresh" if _age_days <= 90 else
                                        "ageing" if _age_days <= 180 else "stale")
                            _ctx_chips.append(chip_pill(
                                f"Deep Dive {_age_days}d ago · {_fr_word}", _fr_tone,
                                title=_dd_hist[-1].get("doc_period_label") or "Untitled batch"))
                    else:
                        _ctx_chips.append(chip_pill(
                            "No Deep Dive saved", "neutral",
                            title="Deep Dive (foot of Overview) builds a research prompt you can run "
                                  "with your own annual report / concall PDFs."))
                except Exception as _fresh_e:
                    _log.debug("Thesis freshness check failed for %s: %s", ticker, _fresh_e)
                st.markdown(
                    '<div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center;'
                    'margin:2px 0 12px 0">' + "".join(_ctx_chips) + "</div>",
                    unsafe_allow_html=True,
                )
                if _ed_days is not None and 0 <= _ed_days <= 7:
                    st.warning(
                        f"**Results in {_ed_days} days.** Implied volatility usually rises into "
                        "the print, and a ±5-20% gap on results day is common."
                    )

                _ov_l, _ov_r = st.columns([1.35, 1], gap="medium")

                # ── Left: price card + thesis ───────────────────────────────
                with _ov_l:
                    st.markdown(section_header("Price · 6 month", "daily close · 50 &amp; 200 DMA"),
                                unsafe_allow_html=True)
                    try:
                        st.plotly_chart(_overview_chart(df), width="stretch",
                                        config={"displayModeBar": False})
                    except Exception as _oc_e:
                        _log.debug("overview chart failed for %s: %s", ticker, _oc_e)
                        st.caption("Chart unavailable; the Chart tab has the full view.")
                    _lvl_cells = []
                    _px_last = float(latest["Close"])
                    for _col, _lbl in (("SMA_50", "50-DMA"), ("SMA_200", "200-DMA")):
                        try:
                            _lv = float(latest.get(_col, float("nan")))
                        except (TypeError, ValueError):
                            _lv = float("nan")
                        if _lv == _lv and _lv > 0:
                            _lvl_cells.append((_lbl, _rs(_lv), f"price {(_px_last / _lv - 1) * 100:+.1f}%"))
                    try:
                        _low20 = float(df["Low"].tail(20).min())
                        _lvl_cells.append(("20-session low", _rs(_low20),
                                            f"{(_px_last / _low20 - 1) * 100:+.1f}% above"))
                        _hi_n = df["High"].tail(252)
                        _hi = float(_hi_n.max())
                        _lvl_cells.append(("52-wk high" if len(_hi_n) >= 240 else "Period high",
                                           _rs(_hi), f"price {(_px_last / _hi - 1) * 100:+.1f}%"))
                    except Exception as _lvl_e:
                        _log.debug("level cells failed for %s: %s", ticker, _lvl_e)
                    if _lvl_cells:
                        st.markdown(level_strip(_lvl_cells), unsafe_allow_html=True)


                # ── Right: fundamentals · overlay signals · reference levels ─
                with _ov_r:
                    _fc = None
                    try:
                        _fc = _cached_fund_card(ticker, getattr(cs, "sector", None),
                                                getattr(cs, "company_name", None))
                    except Exception as _fc_e:
                        _log.debug("fundamentals card failed for %s: %s", ticker, _fc_e)

                    def _x(v, unit="×", d=1):
                        return "—" if v is None else f"{v:,.{d}f}{unit}"

                    _post = str(getattr(_fv_va, "posture", "") or "")
                    _post_tone = {
                        "SUPPORTED": "good", "REASONABLE": "good", "STRETCHED": "warn",
                        "CYCLICAL_PEAK": "warn", "PRICING_IN_PERFECTION": "bad", "PEG_RICH": "bad",
                    }.get(_post, "neutral")
                    if _fc:
                        if _fc["is_financial"]:
                            _f_rows = [
                                ("P/B", _x(_fc["pb"]), True),
                                ("ROE", _x(_fc["roe"], "%"), True),
                                ("P/E", _x(_fc["pe"])),
                            ]
                            _f_note = (f"<b>Why P/B and ROE are highlighted:</b> {_fc['group']} is "
                                       f"assessed on {_fc['preferred']}. Leverage and EV/EBITDA "
                                       "don't describe a lender's balance sheet.")
                        else:
                            _f_rows = [("P/E", _x(_fc["pe"]), True)]
                            if _fc["ev_ok"]:
                                _f_rows.append(("EV / EBITDA", _x(_fc["ev_ebitda"]), True))
                            _f_rows.append(("P/B", _x(_fc["pb"])))
                            _f_rows.append(("ROE", _x(_fc["roe"], "%")))
                            if _fc["roce_ok"]:
                                _f_rows.append(("ROCE", _x(_fc["roce"], "%")))
                            if _fc["de_ok"]:
                                _f_rows.append(("Debt / Equity", _x(_fc["de"], "", 2)))
                            _f_note = (f"<b>Highlighted:</b> {_fc['group']} is valued on "
                                       f"{_fc['preferred']}. Banks, NBFCs and insurers switch to "
                                       "P/B and ROE.")
                        _f_rows.append(("Revenue CAGR (5y)", _x(_fc["rev_cagr"], "%")))
                        _f_rows.append(("EPS CAGR (5y)", _x(_fc["eps_cagr"], "%")))
                        if _post:
                            _f_rows.append(("Valuation posture",
                                            chip_pill(_post.replace("_", " ").title(), _post_tone)))
                        st.markdown(
                            kit_card("Fundamentals · sector-aware", kv_list(_f_rows, note=_f_note),
                                     sub=_fc["group"]),
                            unsafe_allow_html=True,
                        )
                    else:
                        st.markdown(
                            kit_card("Fundamentals · sector-aware", empty_state(
                                "Fundamentals unavailable",
                                hint="The provider returned nothing for this ticker; "
                                     "the Fundamentals tab shows why.")),
                            unsafe_allow_html=True,
                        )


                    # Reference levels: the engine's ATR stop and score-scaled
                    # target, the same numbers Paper Trade and Size Position use.
                    _sl_pct = (cs.stop_loss / cs.price - 1) * 100 if cs.price else 0.0
                    _tp_pct = (cs.target / cs.price - 1) * 100 if cs.price else 0.0
                    _l_rows = [
                        ("Entry (last close)", _rs(float(cs.entry))),
                        ("Stop", _rs(float(cs.stop_loss)) + " " + chip_delta(_sl_pct)),
                        ("Target", _rs(float(cs.target)) + " " + chip_delta(_tp_pct)),
                        ("Risk : reward", f"1 : {float(cs.risk_reward):.1f}"),
                    ]
                    # UX-FIX (stale-date): reassess date computed live from today.
                    if getattr(cs, "horizon", ""):
                        _hz_window_days = {"Intraday": 1, "Swing": 10,
                                           "Positional": 35, "Long-term": 120}
                        _hz_days = next((_d for _k, _d in _hz_window_days.items()
                                         if _k.lower() in str(cs.horizon).lower()), None)
                        if _hz_days:
                            import datetime as _hz_dt
                            _reassess = _hz_dt.date.today() + _hz_dt.timedelta(days=_hz_days)
                            _l_rows.append(("Horizon", f"{cs.horizon} · to {_reassess.strftime('%d %b')}"))
                        else:
                            _l_rows.append(("Horizon", str(cs.horizon)))
                    st.markdown(
                        kit_card(
                            "Reference levels",
                            kv_list(_l_rows, note=_plain_english(
                                cs.action, cs.entry, cs.stop_loss, cs.target, cs.risk_reward)),
                            sub="ATR stop · score-scaled target",
                        ),
                        unsafe_allow_html=True,
                    )
                    # RR AUDIT: flag setups under the common 1.5:1 threshold.
                    if cs.risk_reward < 1.5:
                        st.caption(
                            f"R:R {cs.risk_reward:.1f}:1 is below the 1.5:1 swing-trade minimum: "
                            "the stop is wide relative to the target."
                        )
                    elif cs.risk_reward < 2.0:
                        st.caption(
                            f"R:R {cs.risk_reward:.1f}:1 is acceptable but tight; a run to target "
                            "stays net-positive after typical slippage."
                        )

                # ── AI Co-Pilot panel (moved 2026-09-04, portfolio wiring 2026-09-06) ─
                # Full width under the two columns, still inside the first tab.
                # _pctx (your position) feeds the panel's Portfolio context.
                try:
                    from dashboard.shared.ai import (
                        collect_for_analyze_stock as _ai_collect,
                        render_chat_panel as _ai_panel,
                    )
                    from dashboard.shared.ai.context_builder import Portfolio as _AIPortfolio
                    _ai_sym = ticker.replace(".NS", "") if ticker else ""
                    if _ai_sym:
                        _ai_inputs = _ai_collect(_ai_sym)
                        _ai_portfolio = None
                        _pctx_local = locals().get("_pctx")
                        if _pctx_local:
                            _q  = _pctx_local.get("shares_held") or None
                            _av = _pctx_local.get("avg_price") or None
                            _upl = None
                            if _q and _av and getattr(cs, "price", None):
                                try:
                                    _upl = round((float(cs.price) / float(_av) - 1.0) * 100.0, 2)
                                except Exception:
                                    _upl = None
                            _ai_portfolio = _AIPortfolio(
                                avg_price=_av, quantity=_q, unrealised_pl_pct=_upl,
                            )
                        _ai_panel(_ai_sym, _ai_inputs, portfolio=_ai_portfolio)
                        st.session_state["_ai_panel_top_rendered"] = True
                except Exception as _ai_err:
                    import logging
                    logging.getLogger("dashboard.analyze_stock").debug(
                        "AI co-pilot panel render failed: %s", _ai_err)

                # ── Other lenses: each reads the stock differently ──────────────────
                st.markdown(section_header("Other lenses", "collapsed · each can disagree with the posture"),
                            unsafe_allow_html=True)
                with st.expander("🧭 Horizon reads · short / medium / long", expanded=False):
                    st.caption("Re-weights the same inputs per holding period and adds valuation, "
                               "quality and thesis gates, so it can differ from the chart-based posture.")
                    # ── Combined read across three horizons ─────────────────────
                    # 5-way posture per horizon (conviction >= 75 splits BUY into
                    # Strong Positive; <= 25 splits AVOID into Strong Negative).
                    def _posture_for(_hzv):
                        _v = _hzv.verdict
                        _c = int(getattr(_hzv, "conviction", 50) or 50)
                        if _v == "STRONG BUY" or (_v == "BUY" and _c >= 75):
                            return ("Strong Positive", "var(--bull)")
                        if _v == "BUY":
                            return ("Positive", "var(--bull)")
                        if _v == "WATCH":
                            return ("Neutral", "var(--accent)")
                        if _v == "AVOID" and _c <= 25:
                            return ("Strong Negative", "var(--bear)")
                        if _v == "AVOID":
                            return ("Negative", "var(--bear)")
                        return ("Neutral", "var(--dim)")

                    if _fv is not None:
                        _fv_horizon_labels = {
                            "short":  "Short-term · days–weeks",
                            "medium": "Medium · 1–6 months",
                            "long":   "Long-term · 1 year+",
                        }
                        st.radio(
                            "Focus horizon",
                            options=list(_fv_horizon_labels.keys()),
                            format_func=lambda k: _fv_horizon_labels[k],
                            index=list(_fv_horizon_labels.keys()).index(_fv_horizon),
                            horizontal=True,
                            key="_fv_horizon",
                            help=(
                                "Same subsystem readings, three lenses.\n"
                                "• Short: technical setup + trend dominant.\n"
                                "• Medium: technical + trend + regime, valuation informational.\n"
                                "• Long: quality + valuation + thesis dominate."
                            ),
                        )
                        _fv_colors = {
                            "STRONG BUY": "var(--bull)", "BUY": "var(--bull)",
                            "WATCH": "var(--accent)", "HOLD": "var(--dim)", "AVOID": "var(--bear)",
                        }
                        _cells = [(
                            f"Combined read · {_fv.horizon}",
                            f'<span style="color:{_fv_colors.get(_fv.verdict, "var(--dim)")}">'
                            f"{verdict_display_label(_fv.verdict)}</span>",
                            f"{_fv.confidence} confidence · conviction {_fv.conviction}/100",
                        )]
                        for _hz, _hz_title in (("short", "Short-term"), ("medium", "Medium-term"),
                                               ("long", "Long-term")):
                            _hzv = _hz_verdicts.get(_hz)
                            if _hzv is None:
                                continue
                            _hp, _hc = _posture_for(_hzv)
                            _cells.append((_hz_title, f'<span style="color:{_hc}">{_hp}</span>',
                                           f"conviction {_hzv.conviction}/100"))
                        _cells.append((
                            "Trend quality · TQS",
                            f"{_fv_tqs:.0f} / 90" if _fv_tqs is not None else "—",
                            _tqs_band(_fv_tqs) if _fv_tqs is not None else "unavailable",
                        ))
                        st.markdown(gate_strip(_cells), unsafe_allow_html=True)
                        st.caption(
                            f"{_fv.primary_reason} · Same subsystem inputs re-weighted per horizon; "
                            "independent of the chart window."
                        )
                        st.markdown("**Why — gate by gate**")
                        st.caption(
                            "Verdict combines every subsystem via a decision tree "
                            "(quality/thesis are vetoes, valuation/trend are dampers, "
                            "technical is the driver). Gates showing 'insufficient data' "
                            "are ones whose inputs weren't computed on this page — the "
                            "aggregator uses what's available and marks confidence "
                            "accordingly."
                        )
                        for _g in _fv.gates:
                            _pill = ("✅" if _g.passed is True else
                                     "❌" if _g.passed is False else "❓")
                            _effect_txt = _g.effect if _g.effect != "none" else ""
                            st.markdown(
                                f"{_pill} **{_g.name.title()}** "
                                f"{'(' + _effect_txt + ')' if _effect_txt else ''} — "
                                f"{_g.message}"
                            )
                        if _fv.subsystem_labels:
                            st.caption(
                                "Subsystem readings: " +
                                " · ".join(f"{k}: {v}" for k, v in _fv.subsystem_labels.items())
                            )
                with st.expander("📝 Thesis · bull / bear / risks", expanded=False):
                    st.caption("Rule-based count of bull and bear factors, fundamentals included; "
                               "a strong chart with weak fundamentals reads differently here.")
                    if _fv_th is not None:
                        st.markdown(
                            thesis_grid(
                                getattr(_fv_th, "bull_factors", []) or [],
                                getattr(_fv_th, "bear_factors", []) or [],
                                getattr(_fv_th, "key_risks", []) or [],
                                verdict_label=str(getattr(_fv_th, "verdict", "") or ""),
                                verdict_text=str(getattr(_fv_th, "verdict_rationale", "") or ""),
                            ),
                            unsafe_allow_html=True,
                        )
                    else:
                        st.caption("Thesis unavailable for this ticker right now.")
                with st.expander("📡 Signal agreement · conviction · overlays", expanded=False):
                    st.caption("Weekly trend, relative strength and positioning are sidecar reads; "
                               "they never change the composite and can point the other way.")
                    _wk_tone = {"uptrend": "good", "downtrend": "bad",
                                "sideways": "warn"}.get(_dc.get("weekly"), "neutral")
                    _o_rows = [("Weekly trend",
                                chip_pill(str(_dc.get("weekly") or "—").title(), _wk_tone))]
                    if _dc.get("rs_pct") is not None:
                        _o_rows.append(("RS vs Nifty 50", chip_delta(float(_dc["rs_pct"]))))
                    _rs_score = getattr(cs, "rs_score", None)
                    if _rs_score is not None:
                        _o_rows.append(("RS percentile", f"{float(_rs_score):.0f} / 100"))
                    _o_rows.append(("Signal agreement",
                                    f"{_bull} of {_tot} bullish" if _confirmation_available else "—"))
                    _o_rows.append((
                        "Conviction",
                        f'<span style="color:{_cv_c}">{_conviction:.0f}</span> / 100 '
                        + (chip_tag(f"base {cs.score:.0f} · {_delta_s}")
                           if _confirmation_available else chip_tag("no adjustment")),
                    ))
                    _pos_score = getattr(cs, "positioning_score", None)
                    if _pos_score is not None:
                        _o_rows.append(("Positioning (OI · PCR)", f"{float(_pos_score):.1f} / 10"))
                    _ov_score = getattr(cs, "overlay_score", None)
                    if _ov_score is not None:
                        _o_rows.append(("Quality × Value", f"{_ov_score} / 100"))
                    if getattr(cs, "vix_regime", None):
                        _o_rows.append(("VIX regime", str(cs.vix_regime).title()))
                    _o_note = ("Conviction adjustments: " + " · ".join(_conf_reasons)
                               if _conf_reasons else "")
                    st.markdown(
                        kit_card("Overlay signals", kv_list(_o_rows, note=_o_note),
                                 sub="sidecar · never rewrites composite"),
                        unsafe_allow_html=True,
                    )
                try:
                    if _fv_tqs_res is not None:
                        _p1 = float(getattr(_fv_tqs_res, "p1", 0.0))
                        _p2 = float(getattr(_fv_tqs_res, "p2", 0.0))
                        _p3 = float(getattr(_fv_tqs_res, "p3", 0.0))
                        _p4 = float(getattr(_fv_tqs_res, "p4", 0.0))
                        with st.expander(f"🌊 TQS breakdown — {_fv_tqs:.0f}/90 "
                                         f"({_tqs_band(_fv_tqs)})"
                                         if _fv_tqs is not None else "🌊 TQS breakdown",
                                         expanded=False):
                            st.caption("Trend persistence only; a high TQS can sit beside a neutral posture.")
                            _pc1, _pc2, _pc3, _pc4 = st.columns(4)
                            _pc1.metric("Strength /22.5",    f"{_p1:.1f}",
                                        help="Trend backbone — SMA stack, price above 200SMA, ADX regime.")
                            _pc2.metric("Persistence /22.5", f"{_p2:.1f}",
                                        help="How LONG the trend has held — days above 50SMA, sequence of higher-highs.")
                            _pc3.metric("Momentum /22.5",    f"{_p3:.1f}",
                                        help="Rate of change — RSI regime, MACD histogram, Sharpe 20-day.")
                            _pc4.metric("Confirmation /22.5", f"{_p4:.1f}",
                                        help="Volume / breadth confirmation — OBV z-score, volume regime.")
                            _weakest = min(_p1, _p2, _p3, _p4)
                            _weak_name = {_p1: "Strength", _p2: "Persistence",
                                          _p3: "Momentum", _p4: "Confirmation"}[_weakest]
                            st.caption(
                                f"Weakest pillar: **{_weak_name}** at {_weakest:.1f}/22.5. "
                                "The TQS in the combined-read strip is the sum of all four."
                            )
                except Exception as _tqs_pill_e:
                    import logging
                    logging.getLogger("dashboard.analyze_stock").debug(
                        "TQS pillar breakdown failed for %s: %s", ticker, _tqs_pill_e)
                st.caption("The pre-trade checklist is a pass/fail list for one entry, not a read "
                           "on the stock; it can fail on a constructive posture.")
                try:
                    from dashboard.shared.checklist_ui import render_checklist_expander
                    render_checklist_expander(ticker, df, df_weekly=None, expanded=False)
                except Exception as _chk_e:
                    import logging
                    logging.getLogger("dashboard.analyze_stock").debug(
                        "checklist expander failed for %s: %s", ticker, _chk_e)

                # ── More detail: drill-downs one click away ─────────────────
                st.markdown(section_header("More detail", "expand what you need"),
                            unsafe_allow_html=True)
                if _confirmation_available:
                    with st.expander(f"🔎 All {_tot} confirmation signals", expanded=False):
                        for _sname, _sok in _dc.get("signals", []):
                            st.markdown(
                                f'<div style="font-size:13px;color:var(--ink-mid);padding:2px 0">'
                                f'{"🟢" if _sok else "⚪"} {_sname}</div>',
                                unsafe_allow_html=True,
                            )
                try:
                    from analysis.verdict_ledger import load_ledger as _vl_hist
                    _vl_df = _vl_hist(ticker=ticker, limit=90)
                    if not _vl_df.empty and len(_vl_df) >= 2:
                        import plotly.graph_objects as _go2
                        import pandas as _pd2
                        _vl_df = _vl_df.sort_values("logged_at")
                        _vl_df["_dt"] = _pd2.to_datetime(_vl_df["logged_at"], errors="coerce")
                        with st.expander(f"📈 Verdict history for {ticker.replace('.NS','')} "
                                         f"({len(_vl_df)} entries)", expanded=False):
                            _vfig = _go2.Figure()
                            _vfig.add_trace(_go2.Scatter(
                                x=_vl_df["_dt"], y=_vl_df["conviction"], mode="lines+markers",
                                name="Conviction /100", line=dict(color="var(--bull)", width=2)))
                            if "composite_score" in _vl_df.columns:
                                _vfig.add_trace(_go2.Scatter(
                                    x=_vl_df["_dt"], y=_vl_df["composite_score"], mode="lines",
                                    name="Composite /90", line=dict(color="var(--accent)", width=1.5, dash="dot")))
                            _vfig.update_layout(
                                height=260, margin=dict(l=40, r=20, t=20, b=30),
                                paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                                legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
                                xaxis_title="", yaxis_title="",
                            )
                            st.plotly_chart(_vfig, width="stretch")

                            # One-liner interpretation of stability vs today
                            _latest_conv = float(_vl_df["conviction"].iloc[-1])
                            _mean_prior  = float(_vl_df["conviction"].iloc[:-1].mean()) \
                                if len(_vl_df) > 1 else _latest_conv
                            _delta = _latest_conv - _mean_prior
                            if abs(_delta) < 5:
                                _msg = "✅ Verdict has been **stable** for this stock — today's read is consistent with prior sessions."
                            elif _delta > 0:
                                _msg = f"📈 Verdict is **improving** — today's conviction is {_delta:+.0f} pts above the {len(_vl_df)-1}-session average."
                            else:
                                _msg = f"📉 Verdict is **deteriorating** — today's conviction is {_delta:+.0f} pts below the {len(_vl_df)-1}-session average."
                            st.caption(_msg + " Ledger captured silently on every prior open of this page.")
                except Exception as _vl_hist_e:
                    import logging
                    logging.getLogger("dashboard.analyze_stock").debug(
                        "verdict history render failed for %s: %s", ticker, _vl_hist_e)
                try:
                    from strategies.sector_rotation import SECTORS as _SEC_MAP
                    from analysis.score import score_stock as _score_stock_peer
                    from concurrent.futures import ThreadPoolExecutor as _TPE, wait as _fwait2
                    _peer_sec = None
                    for _s, _ts in _SEC_MAP.items():
                        if ticker in _ts:
                            _peer_sec = _s
                            break
                    if _peer_sec:
                        _peers = [t for t in _SEC_MAP[_peer_sec] if t != ticker][:5]
                        if _peers:
                            with st.expander(f"👥 {_peer_sec} peers ({len(_peers)}) — score comparison",
                                             expanded=False):
                                def _peer_row(_t):
                                    try:
                                        _cs2 = _score_stock_peer(_t, period="1y")
                                        return {"Ticker": _t.replace(".NS", ""),
                                                "Price ₹": round(_cs2.price, 2),
                                                "Score /90": round(_cs2.score, 1),
                                                "Grade": _cs2.grade,
                                                "Trend": _display_label(_cs2.action)}
                                    except Exception:
                                        return {"Ticker": _t.replace(".NS", ""),
                                                "Price ₹": None, "Score /90": None,
                                                "Grade": "—", "Trend": "n/a"}
                                _tpool = _TPE(max_workers=5)
                                try:
                                    _futs = {_tpool.submit(_peer_row, t): t for t in _peers}
                                    _done, _pending = _fwait2(list(_futs.keys()), timeout=8)
                                    _rows = []
                                    for _f in _done:
                                        try:
                                            _rows.append(_f.result(timeout=0))
                                        except Exception:
                                            pass
                                finally:
                                    _tpool.shutdown(wait=False, cancel_futures=True)
                                # Include the current ticker row for reference
                                _rows.insert(0, {"Ticker": ticker.replace(".NS", "") + " ← this",
                                                 "Price ₹": round(cs.price, 2),
                                                 "Score /90": round(cs.score, 1),
                                                 "Grade": cs.grade, "Trend": _display_label(cs.action)})
                                import pandas as _pd_pr
                                _pd_df = _pd_pr.DataFrame(_rows).sort_values(
                                    "Score /90", ascending=False, na_position="last")
                                st.dataframe(_pd_df, hide_index=True, width="stretch")
                                # One-line take on rank within sector
                                _live_scores = [r["Score /90"] for r in _rows
                                                if r["Score /90"] is not None]
                                if len(_live_scores) >= 2:
                                    _my_rank = sum(1 for s in _live_scores if s > cs.score) + 1
                                    st.caption(
                                        f"**{ticker.replace('.NS','')} ranks #{_my_rank} of "
                                        f"{len(_live_scores)}** on composite score within {_peer_sec}. "
                                        "Higher-scoring peers may be a better use of the same sector conviction."
                                    )
                except Exception as _peer_e:
                    import logging
                    logging.getLogger("dashboard.analyze_stock").debug(
                        "peer snapshot failed for %s: %s", ticker, _peer_e)
                with st.expander("🔬 Raw technical indicators (RSI · ADX · ATR · Vol · Stoch · VWAP%)",
                                 expanded=False):
                    ti_cols = st.columns(6)
                    # SH2 · oversold-RSI caveat driven by the live VIX regime.
                    # Fear-regime oversold labels can invert against the setup
                    # they usually suggest (regime study).
                    try:
                        from dashboard.shared.disclosures import (
                            oversold_reliability_suffix as _sh2_ovr,
                        )
                        _ovr_suf = _sh2_ovr(getattr(cs, "vix_regime", None))
                    except Exception:
                        _ovr_suf = ""
                    _rsi_val = latest.get("RSI", 50)
                    _stk_val = latest.get("Stoch_K", 50)
                    # Same volume reading as the composite score (previous
                    # session / projected while today's session is open).
                    from dashboard.shared.volume_display import (
                        reading as _vol_reading, vol_ratio_tile as _vol_ratio_tile,
                    )
                    _vol_r = _vol_reading(df)
                    _vol_tile = _vol_ratio_tile(_vol_r)
                    indicators_display = [
                        ("RSI (14)",  f"{_rsi_val:.1f}",
                         f"Oversold (<30){_ovr_suf}"   if _rsi_val < 30
                         else "Overbought (>70)" if _rsi_val > 70
                         else "Normal"),
                        ("ADX",       f"{latest.get('ADX', 0):.1f}",
                         "Trending (>25)" if latest.get("ADX", 0) > 25 else "Ranging"),
                        ("ATR",       f"₹{latest.get('ATR', 0):.1f}", "Daily move range"),
                        ("Vol Ratio", *_vol_tile),
                        ("Stoch K",   f"{_stk_val:.1f}",
                         f"Oversold{_ovr_suf}" if _stk_val < 20
                         else "Overbought" if _stk_val > 80 else ""),
                        ("VWAP %",   f"{latest.get('VWAP_Pct', 0):+.1f}%",
                         "Above VWAP" if latest.get("VWAP_Pct", 0) > 0 else "Below VWAP"),
                    ]
                    for (lbl, val, note), col in zip(indicators_display, ti_cols):
                        col.metric(lbl, val, note)
                    st.caption(
                        "Raw values — the **🎯 Pre-trade checklist** expander further up "
                        "translates each of these into a pass/fail read against the setup."
                    )
                with st.expander("↔️ Related per-ticker views: Deep Dive · Quality Watch",
                                 expanded=False):
                    st.markdown(
                        "- **Overview (this tab)** — posture, chart, sector-aware fundamentals "
                        "and reference levels up top; horizon reads, thesis, overlays, TQS and "
                        "the pre-trade checklist under Other lenses.\n"
                        "- **Deep Dive (below)** — prepares a full equity-research prompt (all of "
                        "this page's outputs + fundamentals + governance flags + thesis verdict) "
                        "to paste into a Claude conversation with the annual report / concall "
                        "PDFs attached. Save the write-up back with a date.\n"
                        "- **Quality Watch** — long-term-hold suitability lens (fundamental "
                        "quality flags, governance, event risk). Use for a name you plan to sit in."
                    )
                _render_score_methodology()

                # Deep Dive (was its own tab): behind a toggle, because building
                # the prompt context pulls fundamentals, flags and thesis.
                st.markdown(section_header("Deep Dive", "research prompt for your annual report / concall PDFs"),
                            unsafe_allow_html=True)

                @st.fragment
                def _frag_deep_dive():
                    from dashboard.shared.deep_dive_tab import render_deep_dive_tab
                    render_deep_dive_tab(ticker)
                if st.toggle("Build the Deep Dive research prompt", key=f"_dd_open_{ticker}"):
                    _frag_deep_dive()


            # ────────────────────────────────────────────────────────────────
            # Task 2.4 F3 PR C.2: each tab body wrapped in @st.fragment so
            # widget-driven reruns inside a tab only re-execute that tab's
            # body, not the other 6 tabs and not the outer script. Fragments
            # capture parent-scope vars (df_chart, cs, latest, _fv, ...) via
            # closure so no context needs to be threaded through as args.
            # ────────────────────────────────────────────────────────────────

            @st.fragment
            def _frag_chart():
                st.markdown(section_header("Price chart", "candles · SMA · Bollinger · volume · RSI · MACD"),
                            unsafe_allow_html=True)
                _ui_period = st.radio(
                    "Chart period",
                    list(_AS_PERIOD_MAP.keys()),
                    index=3,
                    horizontal=True,
                    key="analyze_period",
                )
                period = _AS_PERIOD_MAP[_ui_period]
                # UX-CLARITY: the period only re-slices the chart. The score,
                # verdict, horizon fit and every metric use a fixed lookback.
                st.caption(
                    "*Chart window only.* The score, verdict, horizon fit and every metric "
                    "use a fixed analytics lookback."
                )
                df_chart = _trim_to_period(df, period)
                st.plotly_chart(build_price_chart(df_chart, ticker, period=period),
                                width="stretch")
                pat_cols   = [c for c in df.columns if c.startswith("Pat_")]
                active_pats = [
                    c.replace("Pat_", "").replace("_", " ")
                    for c in pat_cols if latest.get(c, 0) == 1
                ]
                if active_pats:
                    # SH2 · evidence-gated candlestick display. Patterns are
                    # informational context, never a signal (40k-obs study removed
                    # them from the composite score). The caveat carries the
                    # volume-confirmation state so users can tell weak context
                    # from at-least-volume-supported context.
                    try:
                        from dashboard.shared.disclosures import (
                            evidence_gated_pattern_label as _sh2_pat,
                        )
                        from dashboard.shared.volume_display import reading as _pat_vol_reading
                        _pat_out = _sh2_pat(active_pats,
                                            _pat_vol_reading(df).get("ratio"))
                    except Exception:
                        _pat_out = None
                    if _pat_out:
                        _pat_head, _pat_cav = _pat_out
                        st.info(f"📍 **{_pat_head}**")
                        st.caption(_pat_cav)
                    else:
                        # Fallback keeps the surface visible if the helper import
                        # failed (defensive; the helper itself never raises).
                        st.info(f"📍 **Candlestick context (informational only):** "
                                f"{', '.join(active_pats)}")

                if latest.get("RSI_Bull_Div", 0):
                    st.success("📈 **Bullish RSI Divergence detected** — momentum improving despite lower price")
                if latest.get("RSI_Bear_Div", 0):
                    st.warning("📉 **Bearish RSI Divergence detected** — momentum fading despite higher price")
            with tab_chart:
                _frag_chart()

            @st.fragment
            def _frag_news():
                # ── News & Flags (merged) ─────────────────────────────────────
                # Flags used to render as a standalone strip further up the page,
                # sourced from the SAME feeds (NSE corp announcements + Google
                # News + NSE RSS) that this News section reads — so the user got
                # essentially the same headlines twice. Merged into one section:
                #   • summary badge line: N flags active · red/amber/green count
                #   • expander with the flag detail (headline, category, sentiment)
                #   • the news list below (unchanged)
                st.markdown(section_header("News &amp; flags", get_display_name(ticker)),
                            unsafe_allow_html=True)

                # Flag summary strip (from the same 6h-cached helper the pre-fix
                # standalone strip used). SPEED FIX: setting the warm marker here
                # lets the Final-Verdict banner include the flag gate on the NEXT
                # rerun of the same ticker without a fresh scrape.
                _flag_dicts = []
                try:
                    from dashboard.shared.flags_ui import get_cached_flags as _news_gcf
                    _flag_dicts = _news_gcf(
                        ticker, company_name=getattr(cs, "company_name", None)) or []
                    st.session_state[f"_as_flags_warm::{ticker}"] = True
                except Exception as _fl_e:
                    import logging
                    logging.getLogger("dashboard.analyze_stock").debug(
                        "flags fetch (news section) failed for %s: %s", ticker, _fl_e)

                if _flag_dicts:
                    _sev_counts = {"red": 0, "amber": 0, "green": 0}
                    for _f in _flag_dicts:
                        _s = str(_f.get("sentiment", "")).lower()
                        if _s == "negative": _sev_counts["red"]   += 1
                        elif _s == "neutral":  _sev_counts["amber"] += 1
                        elif _s == "positive": _sev_counts["green"] += 1
                    _top_color = ("var(--bear)" if _sev_counts["red"]
                                  else "var(--amber)" if _sev_counts["amber"] else "var(--bull)")
                    st.markdown(
                        f'<div style="background:var(--surface);border-left:4px solid {_top_color};'
                        f'border-radius:6px;padding:10px 14px;margin-bottom:10px">'
                        f'<b style="color:{_top_color};font-size:13px">'
                        f'🚩 {len(_flag_dicts)} qualitative flag'
                        f'{"s" if len(_flag_dicts) != 1 else ""} active</b> '
                        f'<span style="font-size:12px;color:var(--dim)">'
                        f'· 🔴 {_sev_counts["red"]} · 🟡 {_sev_counts["amber"]} '
                        f'· 🟢 {_sev_counts["green"]}</span>'
                        f'</div>',
                        unsafe_allow_html=True,
                    )
                    with st.expander("See flag detail", expanded=False):
                        for _f in _flag_dicts[:10]:
                            _s = str(_f.get("sentiment", "")).lower()
                            _dot = ("🔴" if _s == "negative"
                                    else "🟡" if _s == "neutral" else "🟢")
                            _msg = str(_f.get("headline") or _f.get("message") or "")[:220]
                            _cat = str(_f.get("category") or "")
                            _src = str(_f.get("source") or "")
                            _date = str(_f.get("date") or "")
                            # UX-FIX: flag detail was a bare one-liner with no way
                            # to jump to the source story. QualitativeFlag carries
                            # the URL in `.detail` (set from the news/RSS "link"
                            # field). Render the headline as a clickable link when
                            # a URL is present, and show the source + date so
                            # users can see who reported it and when.
                            _link = str(_f.get("detail") or "").strip()
                            _has_link = _link.startswith(("http://", "https://"))
                            _title_md = (f"[{_msg}]({_link})" if _has_link else _msg)
                            _meta_bits = []
                            if _cat:  _meta_bits.append(_cat)
                            if _src:  _meta_bits.append(_src)
                            if _date: _meta_bits.append(_date)
                            _meta = " · ".join(_meta_bits)
                            st.markdown(
                                f"- {_dot} **{_title_md}**  \n"
                                f"  <span style='font-size:11px;color:var(--dim)'>"
                                f"{_meta}</span>",
                                unsafe_allow_html=True,
                            )

                # F6 · news skeleton — RSS fetch is slow-ish and blocks the tab.
                _news_skel = st.empty()
                try:
                    from dashboard.shared.ui_components import (
                        loading_skeleton as _f6_skel_news,
                    )
                    _news_skel.markdown(_f6_skel_news(kind="card", count=3),
                                        unsafe_allow_html=True)
                except Exception:
                    pass
                with st.spinner("Loading news…"):
                    from utils.news import get_stock_news as _gsn
                    articles = _gsn(ticker, max_articles=6)
                _news_skel.empty()
                if articles:
                    for art in articles:
                        s      = art["sentiment"]
                        icon   = "🟢" if s == "positive" else ("🔴" if s == "negative" else "⚪")
                        impact = (
                            "Positive catalyst" if s == "positive"
                            else "Negative signal" if s == "negative"
                            else "Neutral update"
                        )
                        st.markdown(
                            f'{icon} **[{art["title"]}]({art["link"]})**  \n'
                            f'<span style="font-size:11px;color:var(--dim)">'
                            f'{art["publisher"]} · {art["time"]} · *{impact}*</span>',
                            unsafe_allow_html=True,
                        )
                elif not _flag_dicts:
                    st.info("No recent news or qualitative flags found for this stock.")

                # (Removed: "Trading Plan" box — the THIRD copy of the same
                # Signal/Score/Entry/SL/Target/RR block, after the score hero and
                # the deleted Action strip. The only content unique to it was the
                # "Entry zone ₹X — ₹Y" band (entry × 1.01) and the ATR footnote;
                # both are trivial and already implied by the metric cards. R:R
                # sizing warning now lives in the score-hero column, next to the
                # RR metric itself.)

                # (Paper Trade lives in the ticker head's action row.)
            with tab_news:
                _frag_news()

            @st.fragment
            def _frag_thesis():
                # ── Investment Thesis (structured) ─────────────────────────────
                # LAYOUT-REORDER: Thesis is the "WHY" — it belongs BEFORE the
                # Fundamentals / Valuation / Liquidity blocks, which are the
                # "CONTEXT" that feeds it. The Portfolio Fit block below then reads
                # the same `_th` object (unchanged), so the semantic ordering is
                # now: verdict → why (Thesis) → context (F/V/L) → fit.
                st.markdown(section_header("Investment thesis", "structured · rules-based"),
                            unsafe_allow_html=True)
                st.caption(
                    "Rules-based synthesis of the signals above — Bull / Bear / Risks with a "
                    "single verdict. Every point is traceable to its source. Not investment advice."
                )
                _th = None
                try:
                    # SPEED FIX: route through the page-level cached full-thesis
                    # helper so reruns of the SAME ticker (period toggle, popover
                    # open, checkbox flip) are instant. Fingerprint the dc + liq
                    # inputs with a cheap hashable summary so a materially-different
                    # input invalidates the cache; the full objects come from
                    # session_state so st.cache_data can hash the key.
                    st.session_state["_as_cs_snap"]  = cs
                    st.session_state["_as_dc_snap"]  = _dc
                    st.session_state["_as_liq_snap"] = _liq_ctx
                    _dc_total = None
                    if isinstance(_dc, dict):
                        _dc_total = _dc.get("total")
                    _liq_tier = getattr(_liq_ctx, "liquidity_tier", None)
                    _th = _cached_thesis_full(ticker, cs.score, cs.action,
                                              _dc_total, _liq_tier)
                    # Thesis verdict posture routed through the shared chip
                    # vocabulary (chip_pill) so every posture in the app shares
                    # one visual language -- see docs/UI_UX_DESIGN_2026-09.md §4.
                    # Note: 5-way scale keeps "Negative" as WARN amber (not BAD
                    # red), matching the existing color map -- BAD red is
                    # reserved for the "Strong Negative" step.
                    _v_tone = {
                        "Strong Positive": "good",   "Positive": "good",
                        "Neutral":         "neutral","Negative": "warn",
                        "Strong Negative": "bad",
                    }.get(_th.verdict, "neutral")
                    st.markdown(
                        f"<div style='font-size:1.05rem;display:flex;"
                        f"align-items:center;gap:8px;flex-wrap:wrap'>"
                        f"<span>Verdict:</span>"
                        f"{chip_pill(_th.verdict, tone=_v_tone)}"
                        f"<span style='color:var(--dim);font-family:var(--font-mono);"
                        f"font-size:12px'>score {_th.verdict_score:+d}</span></div>",
                        unsafe_allow_html=True,
                    )
                    st.caption(_th.verdict_rationale)

                    # UX-FIX: Bull / Bear / Risks used to render as three walls of
                    # dash-bullet text — no color coding, no visual weight, easy
                    # to skim past. Chip-card layout below: each factor becomes a
                    # coloured card (green = bull, red = bear, amber = risk) with
                    # the source tag as a subtle pill, so the user sees the shape
                    # of the thesis at a glance instead of reading paragraphs.
                    _CHIP_STYLES = {
                        "bull": ("var(--sunken)", "var(--bull)", "🟢"),
                        "bear": ("var(--sunken)", "var(--bear)", "🔴"),
                        "risk": ("var(--sunken)", "var(--amber)", "⚠️"),
                    }
                    def _factor_chips(_factors, _kind, _empty):
                        if not _factors:
                            st.caption(_empty); return
                        _bg, _border, _icon = _CHIP_STYLES[_kind]
                        for _f in _factors:
                            # Source label routed through the shared chip_tag
                            # so it reads the same as every other neutral
                            # provenance chip in the app.
                            _pill = (
                                chip_tag(_f.source)
                                if getattr(_f, "source", "") else ""
                            )
                            st.markdown(
                                f'<div style="background:{_bg};border-left:3px solid {_border};'
                                f'border-radius:6px;padding:8px 12px;margin:4px 0">'
                                f'<div style="color:var(--ink-mid);font-size:13px;line-height:1.4">'
                                f'{_icon} {_f.text}</div>'
                                f'<div style="margin-top:4px;font-size:11px;color:var(--dim)">'
                                f'{_pill} <span style="margin-left:6px">{getattr(_f, "evidence", "")}</span></div>'
                                f'</div>',
                                unsafe_allow_html=True,
                            )

                    _tc1, _tc2 = st.columns(2)
                    with _tc1:
                        st.markdown(
                            '<div style="color:var(--bull);font-weight:700;'
                            'letter-spacing:1px;text-transform:uppercase;font-size:12px;'
                            'margin-bottom:4px">🟢 Bull case</div>',
                            unsafe_allow_html=True,
                        )
                        _factor_chips(_th.bull_factors, "bull", "No bull factors triggered.")
                    with _tc2:
                        st.markdown(
                            '<div style="color:var(--bear);font-weight:700;'
                            'letter-spacing:1px;text-transform:uppercase;font-size:12px;'
                            'margin-bottom:4px">🔴 Bear case</div>',
                            unsafe_allow_html=True,
                        )
                        _factor_chips(_th.bear_factors, "bear", "No bear factors triggered.")
                    st.markdown(
                        '<div style="color:var(--amber);font-weight:700;'
                        'letter-spacing:1px;text-transform:uppercase;font-size:12px;'
                        'margin:12px 0 4px 0">⚠️ Key risks</div>',
                        unsafe_allow_html=True,
                    )
                    _factor_chips(_th.key_risks, "risk",
                                  "No specific risks flagged by the rules.")
                    for _tn in getattr(_th, "notes", []) or []:
                        st.info("ℹ️ " + _tn)
                    st.caption(
                        "Contributing subsystems: "
                        + (", ".join(_th.inputs_present) or "none available")
                        + ". Phase A1/D1 — explainable, sector-aware rules; no AI/LLM narration."
                    )
                except Exception as _th_e:
                    st.caption(f"⚠️ Thesis unavailable: {_th_e}")
            with tab_thesis:
                _frag_thesis()

            @st.fragment
            def _frag_fund():
                # ── Fundamentals ───────────────────────────────────────────────
                st.markdown(section_header("Fundamentals", "audited statements"),
                            unsafe_allow_html=True)
                try:
                    import datetime as _f_dt
                    _f_cf  = _fund_service().get_fundamentals(ticker)
                    _f_res = _fund_analytics.compute_all(_f_cf, cagr_years=5)
                    _f_fresh = "—"
                    if _f_cf.last_updated:
                        _f_hrs   = (_f_dt.datetime.now() - _f_cf.last_updated).total_seconds() / 3600
                        _f_fresh = "just now" if _f_hrs < 1 else f"{_f_hrs:.0f}h ago"
                    st.caption(
                        f"Provider: **{_f_cf.provider_name or '—'}**  ·  "
                        f"Statement date: **{_f_cf.statement_date or '—'}**  ·  "
                        f"Data freshness: **{_f_fresh}**"
                    )
                    if _f_cf.is_partial:
                        st.warning(
                            f"⚠️ **Partial data** — some fundamentals are unavailable for this stock "
                            f"from {_f_cf.provider_name or 'the provider'}. "
                            f"Missing: {', '.join(_f_cf.missing_fields) or 'n/a'}."
                        )

                    def _f_show(_col, _r):
                        # UX-FIX: the "confidence: high" caption was misread as
                        # "these numbers are trustworthy / this stock is good" —
                        # but it means "we have enough YEARS of data to compute
                        # this metric confidently", i.e. it is DATA-COVERAGE, not
                        # a quality/goodness signal. A stock with a bad ROE and
                        # 5 years of data still gets confidence=high. Relabel to
                        # "data:" and prepend a colour cue that reflects the
                        # METRIC's own health (green = good, red = poor) so the
                        # user sees the health of the NUMBER at a glance, and the
                        # data-completeness separately.
                        if _r.available and _r.value is not None:
                            _txt = f"{_r.value:,.1f}%" if _r.unit == "%" else f"{_r.value:,.2f}x"
                            # Heuristic health thresholds — kept intentionally
                            # simple; the deep read still lives in the Valuation
                            # section (P/E ranges) and Fundamental Quality (score).
                            _health_color = "var(--dim)"
                            _mname = str(_r.metric or "").lower()
                            if _r.unit == "%":
                                if "roe" in _mname or "roce" in _mname:
                                    _health_color = ("var(--bull)" if _r.value >= 15
                                                     else "var(--amber)" if _r.value >= 8
                                                     else "var(--bear)")
                                elif "cagr" in _mname:
                                    _health_color = ("var(--bull)" if _r.value >= 12
                                                     else "var(--amber)" if _r.value >= 5
                                                     else "var(--bear)")
                            elif "debt" in _mname:
                                _health_color = ("var(--bull)" if _r.value <= 0.5
                                                 else "var(--amber)" if _r.value <= 1.0
                                                 else "var(--bear)")
                            _col.markdown(
                                f'<div style="font-size:11px;color:var(--dim);'
                                f'text-transform:uppercase;letter-spacing:0.5px">'
                                f'{_r.metric}</div>'
                                f'<div style="font-size:24px;font-weight:700;'
                                f'color:{_health_color}">{_txt}</div>',
                                unsafe_allow_html=True,
                            )
                            _col.caption(
                                f"data: {_r.confidence} coverage"
                                + (f" · {_r.reason}" if _r.reason else "")
                            )
                        else:
                            _col.metric(_r.metric, "N/A")
                            _col.caption(f"⚠️ {_r.reason}")

                    from analysis.sector_classification import classify_sector as _classify
                    _sp = _classify(
                        getattr(cs, "sector", None),
                        name=getattr(cs, "company_name", None),
                    )
                    # Rule 3: a lender's debt/equity is its funding model, not
                    # leverage risk, so the row only shows where it applies.
                    _f_keys = ["revenue_cagr", "eps_cagr", "roe"]
                    if _sp.leverage_warning_applies:
                        _f_keys.append("debt_to_equity")
                    for _fcol, _fkey in zip(st.columns(4), _f_keys):
                        _f_show(_fcol, _f_res[_fkey])

                    # (Removed: the same "🔬 Revenue growth …" marketing blurb —
                    # see the identical removal in the score-hero section. The
                    # metric card above already carries the same context.)

                    _cagr_results = [
                        r for r in [_f_res.get("revenue_cagr"), _f_res.get("eps_cagr")]
                        if r is not None and getattr(r, "available", False)
                    ]
                    if _cagr_results and any(r.confidence in ("medium", "low") for r in _cagr_results):
                        st.caption(
                            "📊 **Data coverage note:** the *data:* label above measures how many "
                            "years of history Yahoo Finance returned (~4–5 for most NSE names) — "
                            "**not** whether the number itself is good. Colour on the value = "
                            "the metric's own health (green good · amber ok · red weak)."
                        )

                    if _sp.is_financial:
                        st.info(f"🏦 **{_sp.group}** — {_sp.note}")
                    else:
                        _rc1, _rc2 = st.columns(2)
                        _f_show(_rc1, _f_res["roce"])
                        _rr = _f_res["fcf"]
                        if _rr.available and _rr.value is not None:
                            _rc2.metric("Free Cash Flow", f"₹{_rr.value:,.0f} cr")
                            _cap = (
                                " · capex-heavy: negative FCF can be a normal investment cycle"
                                if _sp.fcf_capex_caveat else ""
                            )
                            _rc2.caption(f"data: {_rr.confidence} coverage{_cap}")
                        else:
                            _rc2.metric("Free Cash Flow", "N/A")
                            _rc2.caption(f"⚠️ {_rr.reason}")
                    st.caption(
                        "Phase 0/D1: Yahoo Finance data only (~4-yr depth), no paid provider. "
                        "ROCE/FCF shown only where economically meaningful. Not investment advice."
                    )
                except Exception as _f_e:
                    st.caption(f"⚠️ Fundamentals unavailable: {_f_e}")
            with tab_fund:
                _frag_fund()

            @st.fragment
            def _frag_val():
                # ── Valuation Context ──────────────────────────────────────────
                st.markdown(section_header("Valuation context", "descriptive · no fair value"),
                            unsafe_allow_html=True)
                st.caption(
                    "Valuation multiples already available from the fundamentals provider. "
                    "Factual context only — no cheap/expensive judgment, no peer comparison yet."
                )
                try:
                    from analysis.fundamentals.valuation import build_valuation_context
                    from analysis.sector_classification import classify_sector as _classify_v
                    _spv     = _classify_v(
                        getattr(cs, "sector", None),
                        name=getattr(cs, "company_name", None),
                    )
                    _val_cf  = _fund_service().get_fundamentals(ticker)
                    _val     = build_valuation_context(_val_cf, sector_profile=_spv)
                    _vc1, _vc2, _vc3 = st.columns(3)
                    _vc1.metric("P/E",  f"{_val.pe:,.1f}x"  if _val.pe  is not None else "N/A")
                    _vc2.metric("P/B",  f"{_val.pb:,.1f}x"  if _val.pb  is not None else "N/A")
                    if _val.ev_ebitda_applicable:
                        _vc3.metric(
                            "EV/EBITDA",
                            f"{_val.ev_ebitda:,.1f}x" if _val.ev_ebitda is not None else "N/A",
                        )
                    else:
                        _vc3.metric("EV/EBITDA", "n/a")
                        _vc3.caption("not meaningful for financials")
                    if _val.preferred_valuation:
                        st.caption(f"📐 Right lens for this sector: **{_val.preferred_valuation}**")
                    for _vn in _val.notes:
                        st.caption("ℹ️ " + _vn)
                    st.caption(
                        f"Coverage: **{_val.confidence}**"
                        + (f" · missing: {', '.join(_val.missing_fields)}" if _val.missing_fields else "")
                        + (f" · source: {_val.source}" if _val.source else "")
                        + ". Values are None when unavailable — never fabricated."
                    )

                    st.markdown("**🧮 Valuation Assessment** *(growth- & quality-adjusted, descriptive)*")
                    try:
                        from analysis.fundamentals.valuation_decision import assess_valuation
                        _va_res = _fund_analytics.compute_all(_val_cf)
                        _va     = assess_valuation(_val, _va_res, _spv, cf=_val_cf)

                        # UX-FIX: the old rendering was a small blockquote + a
                        # trailing "confidence: X" line that read as an admission
                        # of insufficient data every time. Promote the POSTURE
                        # itself to a bold colored badge (that IS the assessment),
                        # then a clean two-panel layout — left: basis + reasons;
                        # right: caveats + coverage — instead of five stacked
                        # captions the eye slides past.
                        _POSTURE_COLORS = {
                            "SUPPORTED":              ("var(--bull)", "🟢"),
                            "REASONABLE":             ("var(--bull)", "🟢"),
                            "STRETCHED":              ("var(--amber)", "🟡"),
                            "PRICING_IN_PERFECTION":  ("var(--bear)", "🔴"),
                            "PEG_RICH":               ("var(--bear)", "🔴"),
                            "CYCLICAL_PEAK":          ("var(--amber)", "🟡"),
                            "CYCLICAL_TROUGH":        ("var(--accent)", "🔵"),
                            "INSUFFICIENT_EVIDENCE":  ("var(--dim)", "⚪"),
                        }
                        _post = str(_va.posture or "INSUFFICIENT_EVIDENCE")
                        _pc, _picon = _POSTURE_COLORS.get(
                            _post, ("var(--dim)", "⚪"))
                        st.markdown(
                            f'<div style="background:var(--surface);border-left:5px solid {_pc};'
                            f'border-radius:8px;padding:14px 18px;margin:8px 0">'
                            f'<div style="font-size:11px;color:var(--dim);letter-spacing:1.5px;'
                            f'text-transform:uppercase">Valuation posture</div>'
                            f'<div style="font-size:22px;font-weight:700;color:{_pc};'
                            f'margin:2px 0 6px 0">{_picon} {_post.replace("_", " ").title()}</div>'
                            f'<div style="font-size:13px;color:var(--ink-mid);line-height:1.5">'
                            f'{_va.phrase}</div>'
                            f'</div>',
                            unsafe_allow_html=True,
                        )
                        _vac1, _vac2 = st.columns(2)
                        with _vac1:
                            if _va.justification and _post != "INSUFFICIENT_EVIDENCE":
                                st.markdown(f"**Basis:** {_va.justification}")
                            if _va.reasons:
                                st.markdown("**Reasons:**")
                                for _rz in _va.reasons:
                                    st.markdown(f"- {_rz}")
                            if _va.triggered_guard:
                                st.caption(f"🛡 Guard: {_va.triggered_guard}")
                        with _vac2:
                            if _va.caveats:
                                st.markdown("**Caveats:**")
                                for _cv in _va.caveats:
                                    st.markdown(f"- ⚠️ {_cv}")
                            if _va.confidence_factors:
                                st.caption("Coverage factors: " + " · ".join(_va.confidence_factors))
                            st.caption(
                                f"Data coverage for this assessment: **{_va.confidence}**. "
                                "Descriptive only — no buy/sell, no fair/intrinsic value, "
                                "no cheap/expensive label."
                            )
                    except Exception as _va_e:
                        st.caption(f"⚠️ Valuation assessment unavailable: {_va_e}")
                except Exception as _val_e:
                    st.caption(f"⚠️ Valuation context unavailable: {_val_e}")
            with tab_fund:
                _frag_val()

            @st.fragment
            def _frag_liq():
                # ── Liquidity Context ──────────────────────────────────────────
                # NOTE: _liq_ctx is computed EARLIER (right after _dc) so the
                # Investment Thesis section can consume it; this render block just
                # displays what was already computed. Do not re-compute here.
                st.markdown(section_header("Liquidity context", "30d vs 90d · from OHLCV"),
                            unsafe_allow_html=True)
                try:
                    from analysis.liquidity import format_turnover
                    if _liq_ctx is None:
                        raise RuntimeError("liquidity context not available (see log)")
                    _lt_color = {
                        "High": "var(--bull)", "Medium": "var(--bull)",
                        "Low":  "var(--amber)", "Illiquid": "var(--bear)",
                    }.get(_liq_ctx.liquidity_tier, "var(--dim)")
                    st.markdown(
                        f"Liquidity tier: <b style='color:{_lt_color}'>{_liq_ctx.liquidity_tier}</b>",
                        unsafe_allow_html=True,
                    )
                    _lc1, _lc2, _lc3 = st.columns(3)
                    _lc1.metric(
                        "Avg daily turnover (30d)",
                        format_turnover(_liq_ctx.avg_daily_turnover_30d),
                    )
                    _lc2.metric(
                        "Avg daily volume (30d)",
                        f"{_liq_ctx.avg_daily_volume_30d:,.0f}"
                        if _liq_ctx.avg_daily_volume_30d is not None else "N/A",
                    )
                    _lc3.metric(
                        "Volume trend (30d vs 90d)",
                        (_liq_ctx.volume_trend or "—").title(),
                        f"{_liq_ctx.volume_trend_ratio:.2f}x"
                        if _liq_ctx.volume_trend_ratio is not None else None,
                    )
                    st.caption(
                        _liq_ctx.reason
                        + " · computed from existing OHLCV (no new data source)."
                    )
                except Exception as _liq_e:
                    st.caption(f"⚠️ Liquidity context unavailable: {_liq_e}")

                # (Investment Thesis section moved UP to just before Fundamentals
                # — see the "🧭 Investment Thesis" block earlier on the page. Kept
                # the local variable `_th` in scope so the Portfolio Fit block
                # below can still consume the candidate thesis.)
            with tab_fund:
                _frag_liq()

            @st.fragment
            def _frag_corp_actions():
                # Corporate actions and shareholding changes, read from the
                # same 6h-cached qualitative-flag feed the News tab uses
                # (NSE top-corp-info, BSE fallback). No extra fetch.
                import html as _html
                _ca_flags = []
                try:
                    from dashboard.shared.flags_ui import get_cached_flags as _ca_gcf
                    _ca_flags = [
                        _f for _f in (_ca_gcf(ticker, company_name=getattr(cs, "company_name", None)) or [])
                        if str(_f.get("category", "")) in ("corporate_action", "governance")
                    ]
                except Exception as _ca_e:
                    _log.debug("corp actions fetch failed for %s: %s", ticker, _ca_e)
                _ca_src = sorted({str(_f.get("source") or "").split(":")[0] for _f in _ca_flags} - {""})
                st.markdown(section_header("Corporate actions", " · ".join(_ca_src) or "NSE / BSE"),
                            unsafe_allow_html=True)
                if not _ca_flags:
                    st.markdown(empty_state(
                        "No corporate actions on record",
                        hint="Nothing recent from NSE or BSE for this ticker (or both "
                             "sources were unreachable). Dividends, splits, buybacks and "
                             "promoter-holding changes appear here when filed.",
                    ), unsafe_allow_html=True)
                    return
                _tone = {"negative": "bad", "positive": "good"}
                _rows = []
                for _f in sorted(_ca_flags, key=lambda f: str(f.get("date") or ""), reverse=True):
                    _kind = "Shareholding" if str(_f.get("category")) == "governance" else "Action"
                    _rows.append([
                        str(_f.get("date") or "—")[:10],
                        chip_tag(_kind),
                        _html.escape(str(_f.get("headline") or "")[:160]),
                        chip_pill(str(_f.get("sentiment") or "neutral").title(),
                                  _tone.get(str(_f.get("sentiment", "")).lower(), "neutral")),
                    ])
                st.markdown(signal_table(
                    [("Date", "l"), ("Type", "l"), ("Detail", "l"), ("Read", "l")], _rows),
                    unsafe_allow_html=True)
                st.caption("Keyword-classified read of each filing's text; descriptive only.")
            with tab_ca:
                _frag_corp_actions()

            @st.fragment
            def _frag_pf():
                # ── Portfolio Fit — FIX A5 + A9: cached, reads manual holdings ──
                st.markdown(section_header("Portfolio fit", "marginal impact on your book"),
                            unsafe_allow_html=True)
                st.caption(
                    "Is this a good *addition* to your current book? Marginal impact on "
                    "diversification, sector mix, beta and concentration. Not investment advice."
                )
                try:
                    # FIX A9: manual holdings (kv-backed) replace the old CSV path read
                    _pf_holds_raw = load_manual_holdings()
                    _pf_holds = []
                    for _r in _pf_holds_raw:
                        _t = str(_r.get("ticker", "")).strip()
                        if _t and not _t.upper().endswith(".NS"):
                            _t += ".NS"
                        _q = float(_r.get("quantity", 0) or 0)
                        if _t and _q > 0:
                            _pf_holds.append({"ticker": _t, "quantity": _q})

                    if not _pf_holds:
                        # F5 empty-state kit -- see dashboard/shared/ui_components.py.
                        from dashboard.shared.ui_components import empty_state as _empty
                        st.markdown(
                            _empty(
                                title="No holdings found",
                                hint="Add holdings on the **🏠 My Portfolio** page "
                                     "to see how this stock would fit your book.",
                                icon="📂",
                            ),
                            unsafe_allow_html=True,
                        )
                    else:
                        from analysis.thesis import build_fit_inputs, assess_fit
                        # F6 · skeleton — portfolio-fit needs per-holding data.
                        _pf_skel = st.empty()
                        try:
                            from dashboard.shared.ui_components import (
                                loading_skeleton as _f6_skel_pf,
                            )
                            _pf_skel.markdown(_f6_skel_pf(kind="card", count=2),
                                              unsafe_allow_html=True)
                        except Exception:
                            pass
                        _pf_th = None
                        try:
                            st.session_state["_as_cs_snap"]  = cs
                            st.session_state["_as_dc_snap"]  = _dc
                            st.session_state["_as_liq_snap"] = _liq_ctx
                            _pf_th = _cached_thesis_full(
                                ticker, cs.score, cs.action,
                                _dc.get("total") if isinstance(_dc, dict) else None,
                                getattr(_liq_ctx, "liquidity_tier", None))
                        except Exception as _pf_th_e:
                            _log.debug("portfolio fit: thesis unavailable for %s: %s", ticker, _pf_th_e)
                        with st.spinner("Assessing fit against your portfolio…"):
                            _fit = assess_fit(
                                build_fit_inputs(ticker, _pf_holds, candidate_thesis=_pf_th)
                            )
                        _pf_skel.empty()

                        # Portfolio fit rating routed through chip_pill --
                        # mirrors the thesis verdict pill above so the two
                        # postures read as one vocabulary. "Poor Fit" stays WARN
                        # amber (a friction, not a veto); "Strong Conflict" is
                        # the BAD red step.
                        _fr_tone = {
                            "Strong Fit":     "good",    "Fit":      "good",
                            "Neutral":        "neutral", "Poor Fit": "warn",
                            "Strong Conflict":"bad",
                        }.get(_fit.fit_rating, "neutral")
                        st.markdown(
                            f"<div style='font-size:1.05rem;display:flex;"
                            f"align-items:center;gap:8px;flex-wrap:wrap'>"
                            f"<span>Fit rating:</span>"
                            f"{chip_pill(_fit.fit_rating, tone=_fr_tone)}"
                            f"<span style='color:var(--dim);font-family:var(--font-mono);"
                            f"font-size:12px'>score {_fit.fit_score:+d}</span></div>",
                            unsafe_allow_html=True,
                        )
                        _im1, _im2 = st.columns(2)
                        _im1.caption("📊 " + _fit.diversification_impact)
                        _im1.caption("🏭 " + _fit.sector_impact)
                        _im2.caption("📈 " + _fit.beta_impact)
                        _im2.caption("⚖️ " + _fit.concentration_impact)

                        # UX-FIX: mirror the Investment-Thesis chip-card layout
                        # here so Positive/Negative effects have the same visual
                        # weight and color coding — user asked for parity.
                        _FIT_STYLES = {
                            "pos": ("var(--sunken)", "var(--bull)", "✅"),
                            "neg": ("var(--sunken)", "var(--bear)", "❌"),
                        }
                        def _fit_chips(_factors, _kind, _empty):
                            if not _factors:
                                st.caption(_empty); return
                            _bg, _border, _icon = _FIT_STYLES[_kind]
                            for _f in _factors:
                                # Mirror _factor_chips above -- shared chip_tag.
                                _pill = (
                                    chip_tag(_f.source)
                                    if getattr(_f, "source", "") else ""
                                )
                                st.markdown(
                                    f'<div style="background:{_bg};border-left:3px solid {_border};'
                                    f'border-radius:6px;padding:8px 12px;margin:4px 0">'
                                    f'<div style="color:var(--ink-mid);font-size:13px;line-height:1.4">'
                                    f'{_icon} {_f.text}</div>'
                                    f'<div style="margin-top:4px;font-size:11px;color:var(--dim)">'
                                    f'{_pill} <span style="margin-left:6px">{getattr(_f, "evidence", "")}</span></div>'
                                    f'</div>',
                                    unsafe_allow_html=True,
                                )

                        _fp, _fn = st.columns(2)
                        with _fp:
                            st.markdown(
                                '<div style="color:var(--bull);font-weight:700;'
                                'letter-spacing:1px;text-transform:uppercase;font-size:12px;'
                                'margin-bottom:4px">✅ Positive effects</div>',
                                unsafe_allow_html=True,
                            )
                            _fit_chips(_fit.positive_effects, "pos", "No positive effects flagged.")
                        with _fn:
                            st.markdown(
                                '<div style="color:var(--bear);font-weight:700;'
                                'letter-spacing:1px;text-transform:uppercase;font-size:12px;'
                                'margin-bottom:4px">❌ Negative effects</div>',
                                unsafe_allow_html=True,
                            )
                            _fit_chips(_fit.negative_effects, "neg", "No negative effects flagged.")

                        _ps_color = {
                            "Large": "var(--bull)", "Moderate": "var(--amber)", "Small": "var(--amber)",
                        }.get(_fit.position_size_guidance, "var(--dim)")
                        st.markdown(
                            f"**Position size guidance:** "
                            f"<b style='color:{_ps_color}'>{_fit.position_size_guidance}</b>",
                            unsafe_allow_html=True,
                        )
                        st.caption(_fit.position_size_reason)
                        st.caption(
                            "Contributing subsystems: "
                            + (", ".join(_fit.inputs_present) or "none")
                            + ". Phase B — rules only, no buy/sell recommendation, no target price."
                        )
                except Exception as _pf_e:
                    st.caption(f"⚠️ Portfolio fit unavailable: {_pf_e}")
            with tab_pf:
                _frag_pf()

        except Exception as e:
            # F6 · make sure the loading skeleton clears even on the score /
            # render failure path — otherwise the shimmer sits above the
            # error message.
            try:
                _skel_ph.empty()
            except Exception:
                pass
            # BUGFIX: previously every failure here — including a simple
            # misspelled/unknown ticker reaching this point via some other
            # path — dumped "Analysis failed: <raw exception>" plus a full
            # Python traceback. fetch_single() raises a ValueError starting
            # with "No data for" specifically when no source recognises the
            # symbol, so that one known case now gets a plain message instead;
            # anything else still shows the traceback since that's a genuine
            # bug worth seeing, not a typo.
            if isinstance(e, ValueError) and str(e).startswith("No data for"):
                st.error(
                    f"❌ **Couldn't find '{ticker.replace('.NS','')}' on NSE.** "
                    "Double-check the spelling, or search by company name above "
                    "(e.g. RELIANCE, INFY, TCS)."
                )
            else:
                # FIX TRACE-LEAK (audit 2026-10-03): the full traceback went to
                # every viewer (file paths, driver errors). Log it; show a
                # short message.
                logging.getLogger("dashboard.analyze_stock").exception(
                    "Analyze Stock failed for %s", ticker)
                st.error(f"❌ Analysis failed for {ticker.replace('.NS', '')} "
                         f"({type(e).__name__}). Try again in a minute; details are in the server log.")


# ── AI Co-Pilot panel — moved higher, see the block right after the Verdict
# Card near the top of the page (2026-09-04). This bottom placement stays
# for the case where the top block failed to render (guard-only, expander
# collapsed) so the diagnostic is still reachable at the foot of the page.
# Guarded (2026-09-06) by the session-state flag the top block sets on
# success, so users don't see two panels for the same symbol.
if not st.session_state.get("_ai_panel_top_rendered", False):
    try:
        from dashboard.shared.ai import (
            collect_for_analyze_stock,
            render_chat_panel,
        )

        _copilot_sym = ticker.replace(".NS", "") if ticker else ""
        if _copilot_sym:
            _copilot_inputs = collect_for_analyze_stock(_copilot_sym)
            render_chat_panel(_copilot_sym, _copilot_inputs,
                              heading="AI Co-Pilot (bottom fallback)",
                              expanded=False)
    except Exception as _copilot_err:
        st.caption(f"AI co-pilot not rendered: {_copilot_err}")
# Reset the flag so the next page render evaluates the guard fresh.
st.session_state["_ai_panel_top_rendered"] = False
