"""Analyze Stock - NSE Smart Investor (multipage page; body verbatim from app.py).

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
from dashboard.shared.ui_components import chip_pill, chip_tag
from dashboard.shared.trade_utils import (
    _action_color,
    _action_emoji,
    _display_label,            # Phase 2 UI honesty
    _grade_color,
    verdict_display_label,
    _paper_trade_popover,      # FIX A1: use popover instead of direct call
    load_manual_holdings,      # FIX A9: manual holdings replace CSV/Angel One path
)
from dashboard.shared.chart_helpers import (
    build_price_chart,
    render_top_bar,
)
from dashboard.shared.flags_ui import render_flag_strip  # QF2: qualitative flags panel

apply_design()
render_sidebar(current="Analyze Stock")
render_top_bar()

# ─────────────────────────────────────────────────────────────────────────────
st.markdown('<h1 class="page-title-serif">Analyze <em>Any NSE Stock</em></h1>', unsafe_allow_html=True)

st.markdown(
    "Search by company name or ticker — get a full **trend-quality score**, "
    "chart, stop-loss, and plain-English read of the setup."
)

# FIX UI-REGIME — surface the current market regime next to the title so
# users see the SAME regime context as on Command Centre. The tooltip
# carries the historical hit-rate note per regime; users interpret every
# BUY signal below through it.
try:
    import streamlit as _as_st_reg
    from dashboard.shared.ui_components import regime_badge as _ui_regime_badge
    @_as_st_reg.cache_data(ttl=1800, show_spinner=False)
    def _as_regime_snap():
        from analysis.regime import snapshot_live
        try:
            return snapshot_live().as_dict()
        except Exception:
            return None
    _as_reg = _as_regime_snap()
    if _as_reg:
        _as_st_reg.markdown(
            _ui_regime_badge(_as_reg.get("label", "unknown"),
                             _as_reg.get("confidence", "low"),
                             compact=False),
            unsafe_allow_html=True,
        )
except Exception as _as_reg_err:
    import logging
    logging.getLogger("dashboard.analyze_stock").debug(
        "regime badge render failed: %s", _as_reg_err)

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

try:
    _mc = _market_context_row()
    _sev_color = {"green": "var(--bull)", "amber": "var(--amber)",
                  "red":   "var(--bear)", "neutral": "var(--dim)"}[_mc["regime_severity"]]
    st.markdown(
        '<div style="background:var(--surface);border:1px solid var(--hairline);border-radius:8px;'
        'padding:8px 14px;margin:6px 0 10px 0">'
        '<div style="font-size:10px;color:var(--accent);letter-spacing:1.5px;'
        'text-transform:uppercase;font-weight:600;margin-bottom:4px">'
        '🌐 Market context · same for every stock, not ticker-specific</div>'
        + (
            f'<span style="font-size:12px;color:var(--ink-mid)">'
            f'<b style="color:{_sev_color}">FII 5d</b> ₹{_mc["fii_5d"]:+,.0f} Cr &nbsp;·&nbsp; '
            f'<b style="color:{_sev_color}">DII 5d</b> ₹{_mc["dii_5d"]:+,.0f} Cr &nbsp;·&nbsp; '
            f'{_mc["regime_msg"]}</span>'
            if _mc["fii_5d"] is not None
            else '<span style="font-size:12px;color:var(--dim)">'
                 'FII/DII flows unavailable — visit the 🏦 FII / DII Flows page '
                 'once to populate the local table.</span>'
        )
        + '</div>',
        unsafe_allow_html=True,
    )
except Exception as _mc_err:
    import logging
    logging.getLogger("dashboard.analyze_stock").debug(
        "market context strip render failed: %s", _mc_err)

# FIX ANL-XREF — the per-ticker analytics surface is spread across this
# page (with its several tabs) and Quality Watch. Explicit map here so
# someone starting on any of them learns the shape. (Swing Checklist was
# folded into this page as the "🎯 Pre-trade go/no-go" expander —
# Analysis-page-consolidation #5. Deep Dive was folded in as the
# "🔬 Deep Dive" tab below — Slice 2, docs/UI_UX_DESIGN_2026-09.md.)
with st.expander("↔️ Related per-ticker views: Deep Dive tab · Quality Watch", expanded=False):
    st.markdown(
        "- **This page (Analyze Stock)** — headline read: composite score, "
        "chart, entry/SL/target, narrative, plus the 8-factor swing-trade "
        "go/no-go checklist as an inline expander.\n"
        "- **🔬 Deep Dive tab (below)** — prepares a full equity-research "
        "prompt (all this page's outputs + fundamentals + governance flags "
        "+ thesis verdict) for you to paste into a Claude conversation with "
        "the annual report / concall PDFs attached. Save the write-up back "
        "with a date.\n"
        "- **Quality Watch** — long-term-hold suitability lens (fundamental "
        "quality flags, governance, event risk). Use for a name you plan to sit in."
    )

from dashboard.shared.disclosures import (
    render_score_methodology as _render_score_methodology,
    render_regime_reliability_note as _render_regime_note,
)
_render_regime_note()
_render_score_methodology()


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

_ui_period = st.radio(
    "Chart period",
    list(_AS_PERIOD_MAP.keys()),
    index=3,
    horizontal=True,
    key="analyze_period",
)
period = _AS_PERIOD_MAP[_ui_period]
# UX-CLARITY: users were reading the chart period as if it re-scored the stock.
# It does not — get_composite_score(), Final Verdict, Thesis, Valuation and the
# Horizon Fit strip all use a FIXED analytics lookback. This picker only changes
# what window of the chart you see. Say it plainly, right next to the picker.
st.caption(
    "*Chart window only.* The score, verdict, horizon fit and every metric "
    "below use a fixed analytics lookback — changing this radio only re-slices "
    "the chart."
)

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

            # ── UI/UX 2026-09 · editorial hero verdict (mockup Variant A) ───
            # Renders ABOVE the existing verdict_card so the descriptive
            # posture noun (never buy/sell/hold, guardrail §1) reads first.
            # Score -> posture mapping stays inside the sector-aware
            # composite thresholds (grade thresholds match analysis/score.py).
            try:
                from dashboard.shared.ui_components import hero_verdict as _hero_verdict
                from dashboard.shared.ui_components import ticker_hover_wrap as _thw
                _score = float(getattr(cs, "score", 0) or 0)
                if _score >= 70:
                    _posture, _qual, _tone = (
                        "Constructive", "trend intact, momentum aligned", "good")
                elif _score >= 55:
                    _posture, _qual, _tone = (
                        "Constructive", "with reservations", "accent")
                elif _score >= 40:
                    _posture, _qual, _tone = (
                        "Neutral", "no clear direction on the tape", "neutral")
                elif _score >= 25:
                    _posture, _qual, _tone = (
                        "Watchful", "trend structure weakening", "warn")
                else:
                    _posture, _qual, _tone = (
                        "Weak", "trend structure broken", "bad")
                _hv_why = ""
                _narr = getattr(cs, "narrative", "") or ""
                if _narr:
                    _hv_why = _narr.strip().split("\n")[0][:280]
                st.markdown(
                    _hero_verdict(
                        posture=_posture,
                        posture_qualifier=_qual,
                        composite=_score,
                        max_score=90,
                        kicker="Posture · " + ticker.replace('.NS', ''),
                        why=_hv_why,
                        tone=_tone,
                    ),
                    unsafe_allow_html=True,
                )
                # UX2 · hover preview on the ticker — rendered OUTSIDE the
                # hero card because hero_verdict clips overflow (the popover
                # was cut off inside the kicker).
                st.markdown(
                    '<div style="font-size:12px;color:var(--dim);margin:-4px 0 8px">Hover: ' + _thw(
                        ticker.replace('.NS', ''),
                        price=(float(cs.price) if getattr(cs, "price", None) else None),
                        chg_pct=getattr(cs, "return_1d", None),
                        score=_score,
                        sector=str(getattr(cs, "sector", "") or ""),
                    ) + '</div>',
                    unsafe_allow_html=True,
                )
            except Exception as _hv_err:
                import logging
                logging.getLogger("dashboard.analyze_stock").debug(
                    "hero_verdict render failed: %s", _hv_err)

            # ── Task 1.4: Verdict Card hero (existing detail card, kept) ────
            # Existing card retained below the new editorial hero so the
            # user still gets sector-aware fundamentals, portfolio context,
            # and per-pillar breakdown. Two hero moments layered:
            #    (1) descriptive posture in serif italic  (new)
            #    (2) detailed action/reasoning card       (existing)
            try:
                from dashboard.shared.ui_components import verdict_card as _verdict_card
                _pctx = None
                try:
                    _holdings = load_manual_holdings()
                    if _holdings is not None and not _holdings.empty:
                        _short = ticker.replace(".NS", "")
                        _row = _holdings[_holdings["ticker"].str.replace(".NS", "", regex=False) == _short]
                        if not _row.empty:
                            _pctx = {
                                "shares_held": int(_row.iloc[0].get("quantity", 0) or 0),
                                "avg_price":   float(_row.iloc[0].get("avg_price", 0) or 0.0),
                            }
                except Exception as _hp_e:
                    import logging
                    logging.getLogger("dashboard.analyze_stock").debug(
                        "portfolio ctx for verdict card unavailable: %s", _hp_e)
                st.markdown(_verdict_card(cs, portfolio_ctx=_pctx),
                            unsafe_allow_html=True)
            except Exception as _vc_err:
                import logging
                logging.getLogger("dashboard.analyze_stock").debug(
                    "verdict card render failed: %s", _vc_err)

            # ── IR1: Bull / Bear / Risk hero card (docs/UI_UX_BACKLOG.md) ────
            # The thesis engine already emits bull_factors + bear_factors +
            # key_risks; historically these lived only inside the Thesis tab.
            # Promoted here so the shape of the thesis is above-the-fold
            # alongside the Verdict Card -- a user answering "so what's the
            # story on this ticker" doesn't have to click into a tab.
            # Full list + rules-provenance caption stay in the Thesis tab.
            try:
                from dashboard.shared.ui_components import bull_bear_risk_card as _bbr_card
                _bbr_th = _cached_thesis_banner(ticker, cs.score, cs.action)
                st.markdown(
                    _bbr_card(
                        bull_factors=getattr(_bbr_th, "bull_factors", []) or [],
                        bear_factors=getattr(_bbr_th, "bear_factors", []) or [],
                        key_risks=getattr(_bbr_th, "key_risks",     []) or [],
                    ),
                    unsafe_allow_html=True,
                )
            except Exception as _bbr_err:
                import logging
                logging.getLogger("dashboard.analyze_stock").debug(
                    "bull/bear/risk hero card render failed: %s", _bbr_err)

            # ── AI Co-Pilot panel (moved 2026-09-04, portfolio wiring 2026-09-06) ─
            # Was at the bottom of the file behind ~2260 lines of page
            # content, inside a collapsed expander. Users reasonably said
            # "AI chatbot not showing" - they never scrolled that far or
            # clicked the expander. Rendered here right after the Verdict
            # Card so it's visible above the fold; the panel itself opens
            # by default when GROQ_API_KEY is set (see panel.py's expanded=
            # kwarg default). Try/except-wrapped so a co-pilot failure
            # never crashes the page.
            #
            # Portfolio: reuse _pctx from the Verdict Card block above (same
            # load_manual_holdings lookup) so the panel sees the user's real
            # position when it exists. Panel accepts a Portfolio dataclass;
            # translate _pctx dict -> Portfolio here.
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

            df_chart = _trim_to_period(df, period)

            # Revenue growth chip
            _rg_val, _rg_conf = None, ""
            try:
                _rg_cf  = _fund_service().get_fundamentals(ticker)
                if _rg_cf is not None:
                    _rg_res = _fund_analytics.revenue_cagr(_rg_cf, years=5)
                    if getattr(_rg_res, "available", False) and _rg_res.value is not None:
                        _rg_val  = float(_rg_res.value)
                        _rg_conf = str(_rg_res.confidence)
            except Exception as _rg_e:
                import logging; logging.getLogger("dashboard.analyze_stock").debug("Revenue growth fetch failed for %s: %s", ticker, _rg_e)

            # ── FinalVerdict banner (single answer combining every subsystem) ─
            # FIX FV-BANNER — the seven-scores problem lived here: users saw
            # composite / TQS / valuation / thesis / quality / flags stacked
            # top-to-bottom and had to synthesise a decision themselves. This
            # banner renders analysis.final_verdict.combine() output at the
            # top so the ONE verdict is the first thing they read; the
            # existing subsystem sections stay below as drilldown.
            #
            # We compute cheap inputs (composite + TQS) here and pass what
            # richer subsystems produce further down the page as None — the
            # aggregator degrades gracefully (see FinalVerdict confidence
            # tracking). A future pass can compute valuation/thesis/quality
            # up top too, at the cost of moving their fetches earlier.
            try:
                from analysis.final_verdict import combine as _fv_combine
                _fv_tqs = None
                try:
                    from analysis.trend_quality_score import score_ticker as _fv_tqs_score
                    _fv_tqs_res = _fv_tqs_score(ticker, period="1y")
                    if hasattr(_fv_tqs_res, "tqs"):
                        _fv_tqs = float(_fv_tqs_res.tqs)
                except Exception as _fv_tqs_e:
                    import logging
                    logging.getLogger("dashboard.analyze_stock").debug(
                        "FinalVerdict TQS fetch failed for %s: %s", ticker, _fv_tqs_e)

                # FIX FV-HORIZON — one aggregator, three lenses. Same subsystem
                # readings; the horizon selector re-weights how they combine.
                # See analysis/final_verdict._HORIZON_WEIGHTS for exact rules.
                _fv_horizon_labels = {
                    "short":  "🎯 Short-term  (days–weeks)",
                    "medium": "📈 Medium  (weeks–months)",
                    "long":   "🏛 Long-term  (6 months +)",
                }
                _fv_horizon_choice = st.session_state.get("_fv_horizon", "medium")
                _fv_h_col1, _fv_h_col2 = st.columns([3, 2])
                with _fv_h_col1:
                    _fv_horizon = st.radio(
                        "Verdict horizon",
                        options=list(_fv_horizon_labels.keys()),
                        format_func=lambda k: _fv_horizon_labels[k],
                        index=list(_fv_horizon_labels.keys()).index(_fv_horizon_choice),
                        horizontal=True,
                        key="_fv_horizon",
                        help=(
                            "Same subsystem readings, three lenses.\n"
                            "• Short: technical setup + trend dominant. "
                            "Bad valuation barely matters — you're not holding long enough.\n"
                            "• Medium: technical + trend + regime, valuation informational.\n"
                            "• Long: quality + valuation + thesis dominate. A technical "
                            "EXIT signal today doesn't kill a 5-year thesis."
                        ),
                    )
                with _fv_h_col2:
                    st.caption("")   # vertical spacer to align

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
                _fv_colors = {
                    "STRONG BUY": "var(--bull)",
                    "BUY":        "var(--bull)",
                    "WATCH":      "var(--accent)",
                    "HOLD":       "var(--dim)",
                    "AVOID":      "var(--bear)",
                }
                _fv_bg = _fv_colors.get(_fv.verdict, "var(--dim)")
                st.markdown("---")
                st.markdown(
                    f'<div style="background:linear-gradient(135deg,var(--surface),var(--sunken));'
                    f'border-left:6px solid {_fv_bg};border-radius:10px;'
                    f'padding:16px 20px;margin-bottom:8px">'
                    f'<div style="display:flex;justify-content:space-between;align-items:baseline;flex-wrap:wrap">'
                    f'<div>'
                    f'<div style="font-size:12px;color:var(--dim);letter-spacing:1.5px;text-transform:uppercase">'
                    f'Combined read · {_fv.horizon.title()}-term · {_fv.confidence.title()} confidence</div>'
                    f'<div style="font-size:32px;font-weight:700;color:{_fv_bg};margin:4px 0">'
                    f'{verdict_display_label(_fv.verdict)}</div>'
                    f'<div style="font-size:14px;color:var(--ink-mid)">{_fv.primary_reason}</div>'
                    f'</div>'
                    f'<div style="text-align:right">'
                    f'<div style="font-size:12px;color:var(--dim)">Conviction</div>'
                    f'<div style="font-size:28px;font-weight:700;color:var(--ink)">{_fv.conviction}</div>'
                    f'<div style="font-size:11px;color:var(--dim)">/ 100</div>'
                    f'</div>'
                    f'</div></div>',
                    unsafe_allow_html=True,
                )

                # ── HORIZON FIT STRIP ───────────────────────────────────────
                # User ask: "the stock is good right now in which terms?" —
                # answer all three horizons at once instead of making them
                # toggle a picker. Reuses the same subsystem inputs we already
                # gathered for the banner above; combine() is pure Python so
                # calling it three times is essentially free. Each cell shows
                # the horizon's verdict as one of three postures — Positive /
                # Neutral / Negative — with the driving reason underneath. The
                # picker above still exists as the "focus" horizon, and the
                # matching cell gets a thicker border so the two views agree.
                try:
                    _hz_verdicts = {}
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

                    # 5-way posture map (was 3-way). User asked for the full
                    # gradient — Strong Positive / Positive / Neutral / Negative
                    # / Strong Negative — so the STRONG BUY vs BUY (or AVOID vs
                    # HOLD) distinction the underlying engine already produces
                    # isn't collapsed away at the display step. Conviction
                    # thresholds split BUY into two bands: conviction ≥ 75 is
                    # STRONG POSITIVE, otherwise POSITIVE. AVOID splits by
                    # symmetric conviction ≤ 25 for STRONG NEGATIVE.
                    def _posture_for(_hzv):
                        _v = _hzv.verdict
                        _c = int(getattr(_hzv, "conviction", 50) or 50)
                        if _v == "STRONG BUY" or (_v == "BUY" and _c >= 75):
                            return ("Strong Positive", "var(--bull)")
                        if _v == "BUY":
                            return ("Positive", "var(--bull)")
                        if _v == "WATCH":
                            return ("Neutral", "var(--accent)")
                        if _v == "HOLD":
                            return ("Neutral", "var(--dim)")
                        if _v == "AVOID" and _c <= 25:
                            return ("Strong Negative", "var(--bear)")
                        if _v == "AVOID":
                            return ("Negative", "var(--bear)")
                        return ("Neutral", "var(--dim)")
                    _hz_labels = {
                        "short":  ("Short-term",  "days–weeks"),
                        "medium": ("Medium-term", "1–6 months"),
                        "long":   ("Long-term",   "1 year+"),
                    }
                    st.markdown(
                        '<div style="font-size:12px;color:var(--dim);letter-spacing:1.5px;'
                        'text-transform:uppercase;margin:6px 0 4px 0">'
                        'Horizon fit — how the same inputs read across three holding periods'
                        '</div>',
                        unsafe_allow_html=True,
                    )
                    _hz_cols = st.columns(3)
                    for _i, _hz in enumerate(("short", "medium", "long")):
                        _hzv = _hz_verdicts[_hz]
                        _posture, _color = _posture_for(_hzv)
                        _hz_title, _hz_range = _hz_labels[_hz]
                        _is_focus = (_hz == _fv_horizon)
                        _border = ("2px solid " + _color) if _is_focus else "1px solid var(--hairline)"
                        _hz_cols[_i].markdown(
                            f'<div style="background:var(--surface);border:{_border};'
                            f'border-radius:10px;padding:12px 14px;height:100%">'
                            f'<div style="font-size:11px;color:var(--dim);letter-spacing:1px;'
                            f'text-transform:uppercase">{_hz_title} · {_hz_range}</div>'
                            f'<div style="font-size:20px;font-weight:700;color:{_color};'
                            f'margin:2px 0">{_posture}</div>'
                            f'<div style="font-size:11px;color:var(--dim)">'
                            f'Read: <b style="color:var(--ink-mid)">{verdict_display_label(_hzv.verdict)}</b> · '
                            f'conviction {_hzv.conviction}/100</div>'
                            f'<div style="font-size:12px;color:var(--ink-mid);margin-top:6px;'
                            f'line-height:1.35">{_hzv.primary_reason}</div>'
                            f'</div>',
                            unsafe_allow_html=True,
                        )
                    st.caption(
                        f"↑ Same subsystem inputs as the verdict banner above, re-weighted "
                        f"per horizon. The picker sets the *focus* horizon "
                        f"(highlighted). *Independent of chart period.*"
                    )
                except Exception as _hz_err:
                    import logging
                    logging.getLogger("dashboard.analyze_stock").debug(
                        "Horizon-fit strip failed for %s: %s", ticker, _hz_err)

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

                with st.expander("Why? — gate-by-gate breakdown", expanded=False):
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
            except Exception as _fv_err:
                import logging
                logging.getLogger("dashboard.analyze_stock").debug(
                    "FinalVerdict banner failed for %s: %s", ticker, _fv_err)

            # (FII/DII regime context moved to the top-of-page Market Context
            # strip — it's a market-wide signal identical for every ticker, so
            # showing it inside the per-ticker analyze block was redundant and
            # made the page feel "same regime message everywhere".)

            # ── 🧠 Thesis freshness alarm (Analysis-page-consolidation #4) ────
            # The Deep Dive page stores past deep-dive writeups per ticker under
            # kv key deep_dive_history:{ticker}. Surface the AGE of the newest
            # entry here so the user knows whether their long-term thesis has
            # been reviewed recently — critical for a 10-yr holder.
            try:
                import trade_store as _tstore
                import datetime as _dt_al
                _dd_hist_key = f"deep_dive_history:{ticker}"
                _dd_hist = _tstore.kv_get(_dd_hist_key, default=None, user_id="default") or []
                if _dd_hist:
                    _newest = _dd_hist[-1]
                    _gen_at = _newest.get("generated_at", "")
                    try:
                        _gen_dt = _dt_al.datetime.fromisoformat(_gen_at)
                        _age_days = (_dt_al.datetime.now() - _gen_dt).days
                    except Exception:
                        _age_days = None
                    if _age_days is not None:
                        _label = _newest.get("doc_period_label") or "Untitled batch"
                        if _age_days <= 90:
                            _fresh_msg = (f"✅ **Thesis fresh** — last Deep Dive **{_age_days} days ago** "
                                          f"({_label}).")
                            _fresh_color = "success"
                        elif _age_days <= 180:
                            _fresh_msg = (f"🟡 **Thesis ageing** — last Deep Dive **{_age_days} days ago** "
                                          f"({_label}). Consider refreshing after next results.")
                            _fresh_color = "warning"
                        else:
                            _fresh_msg = (f"🔴 **Thesis stale** — last Deep Dive **{_age_days} days ago** "
                                          f"({_label}). Refresh before adding to this position.")
                            _fresh_color = "error"
                        _fresh_msg += " Refresh via the **🔬 Deep Dive** tab below."
                        if _fresh_color == "success":
                            st.success(_fresh_msg)
                        elif _fresh_color == "warning":
                            st.warning(_fresh_msg)
                        else:
                            st.error(_fresh_msg)
                else:
                    st.caption(
                        "💡 No structured Deep Dive saved for this ticker yet. "
                        "The **🔬 Deep Dive** tab below generates a research "
                        "prompt you can run in Claude with your own AR / concall PDFs, "
                        "then paste the result back to save it with a date."
                    )
            except Exception as _fresh_e:
                import logging
                logging.getLogger("dashboard.analyze_stock").debug(
                    "Thesis freshness check failed for %s: %s", ticker, _fresh_e)

            # ── 📈 Verdict trend for THIS ticker (Analysis-page-consolidation #1) ─
            # Uses verdict_ledger data captured on every prior analysis of this
            # ticker. Shows conviction + composite_score over time so the user
            # can see whether today's verdict is a fresh flip or a persistent
            # signal. Silently skipped when the ledger is empty for this ticker.
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

            # ── 🌊 TQS 4-pillar breakdown (Analysis-page-consolidation #2) ────
            # The TQS scanner page has a Deep Dive tab that decomposes TQS into
            # its four pillars (Strength / Persistence / Momentum / Confirmation).
            # We already fetched the TQSResult for the banner at the top of this
            # block (_fv_tqs_res). Surface the pillar breakdown here so users
            # don't need to visit a second page to see WHY TQS is what it is.
            try:
                if _fv_tqs_res is not None:
                    _p1 = float(getattr(_fv_tqs_res, "p1", 0.0))
                    _p2 = float(getattr(_fv_tqs_res, "p2", 0.0))
                    _p3 = float(getattr(_fv_tqs_res, "p3", 0.0))
                    _p4 = float(getattr(_fv_tqs_res, "p4", 0.0))
                    with st.expander(f"🌊 TQS breakdown — {_fv_tqs:.0f}/90 "
                                     f"({getattr(_fv_tqs_res, 'signal', lambda: '')()})"
                                     if _fv_tqs is not None else "🌊 TQS breakdown",
                                     expanded=False):
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
                            "The composite TQS score in the banner above is the sum of all four."
                        )
            except Exception as _tqs_pill_e:
                import logging
                logging.getLogger("dashboard.analyze_stock").debug(
                    "TQS pillar breakdown failed for %s: %s", ticker, _tqs_pill_e)

            # ── 🎯 8-factor pre-trade checklist (Analysis consolidation #5) ──
            # Absorbs the standalone Swing Checklist page (13_swing_checklist.py).
            # Uses the same 8 rules (VIX / SMA200 / MA stack / RSI zone / ADX /
            # MTF alignment / Sector top-3 / Volume) but rendered inline so the
            # user doesn't have to visit a separate page to get the go/no-go.
            # The standalone page will be dropped once this is verified live.
            try:
                from dashboard.shared.checklist_ui import render_checklist_expander
                render_checklist_expander(ticker, df, df_weekly=None, expanded=False)
            except Exception as _chk_e:
                import logging
                logging.getLogger("dashboard.analyze_stock").debug(
                    "checklist expander failed for %s: %s", ticker, _chk_e)

            # ── 📅 Next catalyst window (Analysis consolidation #7) ──────────
            # Earnings date + "is a new entry within N days a gamble?" from
            # data.events. Long-term holders care less about this than swing
            # traders but it's a useful "wait for post-result" nudge.
            try:
                from data.events import get_earnings_date as _get_ed, earnings_within_days as _ewd
                import datetime as _dt_ev
                _ed = _get_ed(ticker)
                if _ed is not None:
                    _days_to = (_ed.date() - _dt_ev.date.today()).days
                    if _days_to < 0:
                        _cat_msg = f"📅 Last results: {_ed.date().isoformat()} ({-_days_to} days ago)."
                        st.caption(_cat_msg)
                    elif _days_to <= 7:
                        st.warning(
                            f"⚠️ **Earnings in {_days_to} days ({_ed.date().isoformat()})** — "
                            "IV usually rises into the print, and gap ±5-20% on results day is "
                            "common. Prefer waiting for post-result confirmation before adding.",
                        )
                    elif _days_to <= 30:
                        st.info(
                            f"📅 **Earnings in {_days_to} days ({_ed.date().isoformat()})** — "
                            "watch for pre-result run-up / drift; size accordingly.",
                        )
                    else:
                        st.caption(f"📅 Next earnings: {_ed.date().isoformat()} "
                                   f"({_days_to} days away). No immediate event risk.")
            except Exception as _cat_e:
                import logging
                logging.getLogger("dashboard.analyze_stock").debug(
                    "next-catalyst render failed for %s: %s", ticker, _cat_e)

            # ── 👥 Peer snapshot (Analysis consolidation #6) ────────────────
            # Show 4-5 sector peers with their composite score so the user
            # sees whether they're picking the strongest or weakest name in
            # the sector. Cheap: reuses the app's own score_stock, capped at
            # 5 peers and a 6s timeout so the page never hangs.
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

            # ── Score hero section ─────────────────────────────────────────
            st.markdown("---")
            hero_col, detail_col = st.columns([1, 2])

            with hero_col:
                grade_c = _grade_color(cs.grade)
                card_c  = _action_color(cs.action)
                emoji   = _action_emoji(cs.action)
                st.markdown(
                    f'<div class="{card_c}" style="text-align:center;padding:24px">'
                    f'<div class="ticker-label">{ticker.replace(".NS","")}</div>'
                    f'<div style="font-size:14px;color:var(--dim)">₹{cs.price:,.2f}</div>'
                    f'<div class="score-big" style="color:{grade_c}">{cs.score:.0f}</div>'
                    f'<div style="font-size:13px;color:var(--dim)">out of 90</div>'
                    f'<div style="font-size:28px;font-weight:700;color:{grade_c};margin:8px 0">'
                    f'Grade: {cs.grade}</div>'
                    f'<div class="signal-big">{emoji} {_display_label(cs.action)}</div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )
                st.markdown("")
                score_breakdown = {
                    "Technical (40)": cs.technical_score,
                    "Momentum (25)":  cs.momentum_score,
                    "Volume (15)":    cs.volume_score,
                    "Sentiment (10)": cs.sentiment_score,
                }
                for label, val in score_breakdown.items():
                    _max = {"Technical (40)": 40, "Momentum (25)": 25,
                            "Volume (15)": 15,    "Sentiment (10)": 10}[label]
                    pct       = val / _max * 100
                    bar_color = "var(--bull)" if pct >= 60 else "var(--amber)" if pct >= 35 else "var(--bear)"
                    st.markdown(
                        f'<div style="display:flex;align-items:center;margin:3px 0;">'
                        f'<span style="width:160px;font-size:12px;color:var(--ink-mid)">{label}</span>'
                        f'<div style="flex:1;background:var(--sunken);border-radius:4px;height:10px">'
                        f'<div style="width:{pct:.0f}%;background:{bar_color};'
                        f'border-radius:4px;height:10px"></div></div>'
                        f'<span style="width:42px;text-align:right;font-size:12px;color:var(--ink-mid)">'
                        f'{val:.0f}</span></div>',
                        unsafe_allow_html=True,
                    )

            with detail_col:
                latest  = df.iloc[-1]
                prev    = df.iloc[-2]   # safe — len(df) >= 2 guarded above
                day_chg = (latest["Close"] / prev["Close"] - 1) * 100

                _disp_price = _an_live if _an_live else cs.price
                mc1, mc2, mc3, mc4, mc5 = st.columns(5)
                mc1.metric(
                    "Price (live)" if _an_live else "Close",
                    f"₹{_disp_price:,.2f}", f"{day_chg:+.2f}%",
                )
                mc2.metric("Sector",      cs.sector)
                mc3.metric("VIX Regime",  cs.vix_regime)
                # FIX A6: guard against cs.sector_rank being None
                mc4.metric(
                    "Sector Rank",
                    f"#{cs.sector_rank}" if cs.sector_rank else "—",
                )
                mc5.metric(
                    "Rev Growth /yr",
                    f"{_rg_val:+.1f}%" if _rg_val is not None else "—",
                    help=(
                        "Annualised revenue growth from audited statements"
                        + (f" · confidence: {_rg_conf}" if _rg_conf else "")
                        + ". The strongest return-linked metric in platform "
                          "research (2022–2025) — a measured observation, "
                          "not a buy signal."
                    ),
                )
                # (Removed: "🔬 Revenue growth has been the strongest
                # return-predictive signal … 2022–2025 validation …" blurb —
                # a static marketing string that read as stale once its own
                # date drifted out of the current year. The metric already
                # has the same context in its help= tooltip; no need to
                # repeat it as a footnote on every page load.)

                # FIX A2: live drift caption only fires when market is actually open
                try:
                    from utils.market_hours import market_status as _an_ms
                    _ms_an = _an_ms()
                    try:
                        _dlabel = df.index[-1].strftime("%d-%b")
                    except Exception:
                        _dlabel = ""
                    if _ms_an.get("is_open") and _an_live and _an_drift >= 0.5:
                        # FIX A2: only warn about drift during live market hours
                        # P2 · promoted from st.caption to a dismissible amber
                        # banner so it isn't lost in the top-bar noise. The
                        # dismissal is per ticker + drift bucket, so a bigger
                        # move later in the session re-surfaces it.
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
                                    fallback="Treat entry / stop / target below as a guide until the close settles.",
                                ), unsafe_allow_html=True)
                            with _db_c2:
                                if st.button("✕", key=f"btn{_drift_key}",
                                             help="Dismiss for this ticker"):
                                    st.session_state[_drift_key] = True
                                    st.rerun()
                    elif _ms_an.get("is_open"):
                        st.caption(
                            "🔴 LIVE · market open — official close settles after 3:30 PM."
                        )
                    else:
                        st.caption(
                            f"🟢 Settled EOD close{f' · {_dlabel}' if _dlabel else ''} "
                            "(market closed — official end-of-day price)."
                        )
                except Exception as _ms_e:
                    import logging; logging.getLogger("dashboard.analyze_stock").debug("Market status check failed: %s", _ms_e)

                tc1, tc2, tc3, tc4 = st.columns(4)
                tc1.metric("Entry (now)", f"₹{cs.entry:,.2f}")
                tc2.metric(
                    "Stop-Loss", f"₹{cs.stop_loss:,.2f}",
                    f"-{(cs.price - cs.stop_loss)/cs.price*100:.1f}%",
                    delta_color="inverse",
                )
                tc3.metric(
                    "Target", f"₹{cs.target:,.2f}",
                    f"+{(cs.target - cs.price)/cs.price*100:.1f}%",
                )
                tc4.metric("Risk : Reward", f"{cs.risk_reward:.1f} : 1")

                # RR AUDIT: flag setups where R:R is under the common 1.5:1
                # sizing threshold. The Action strip below (since removed) used
                # to hint at this by colouring the RR chip; making it explicit
                # here — right next to the number — matches the user ask to
                # "check the trading plan in terms of Rewards:Risk".
                if cs.risk_reward < 1.5:
                    st.caption(
                        f"⚠️ **R:R = {cs.risk_reward:.1f}:1** — below the 1.5:1 "
                        "swing-trade minimum. Either wait for a tighter entry "
                        "(reduces stop distance) or a higher target, or **halve size** "
                        "if you're taking this setup on conviction from other gates."
                    )
                elif cs.risk_reward < 2.0:
                    st.caption(
                        f"R:R = {cs.risk_reward:.1f}:1 — acceptable but on the tight "
                        "side; a run to target still net-positive after typical slippage."
                    )

                # UX-FIX (stale-date): the previous "reassess after {cs.valid_until}"
                # caption baked a scoring-time date into cs and NEVER refreshed —
                # loading the same ticker a week later still showed the OLD
                # reassess date. Compute the horizon window LIVE from today so
                # it always reads correctly no matter when the page is opened,
                # and phrase it as "reassess by <weekday, date>" so the user
                # doesn't have to translate an ISO date in their head.
                if getattr(cs, "horizon", ""):
                    _hz_window_days = {
                        "Intraday":   1,
                        "Swing":      10,
                        "Positional": 35,
                        "Long-term":  120,
                    }
                    _hz_lbl = str(cs.horizon)
                    _hz_days = None
                    for _k, _d in _hz_window_days.items():
                        if _k.lower() in _hz_lbl.lower():
                            _hz_days = _d
                            break
                    if _hz_days:
                        import datetime as _hz_dt
                        _reassess = _hz_dt.date.today() + _hz_dt.timedelta(days=_hz_days)
                        st.caption(
                            f"⏱ **Horizon:** {cs.horizon} — reassess by "
                            f"**{_reassess.strftime('%a, %d %b %Y')}** "
                            f"(≈ {_hz_days}d from today)."
                        )
                    else:
                        st.caption(f"⏱ **Horizon:** {cs.horizon}")

                st.markdown(
                    f'<div class="{_action_color(cs.action)}">'
                    f'<b style="font-size:16px">{cs.headline}</b><br><br>'
                    f'<span class="narrative">{cs.narrative}</span>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

                # PLAIN ENGLISH — folded into the score-hero detail column
                # (previously duplicated further down in a "💬 In plain English"
                # panel that repeated the same entry/SL/target numbers already
                # shown as metrics above). One canonical place, near the top,
                # where the user actually reads first.
                st.markdown(
                    f'<div class="glass-panel" style="margin-top:10px;padding:12px 16px">'
                    f'<div style="font-size:11px;color:var(--amber);font-weight:700;'
                    f'text-transform:uppercase;letter-spacing:1px;margin-bottom:6px">'
                    f'💬 In plain English</div>'
                    f'<div style="font-size:14px;line-height:1.6;color:var(--ink-mid)">'
                    f'{_plain_english(cs.action, cs.entry, cs.stop_loss, cs.target, cs.risk_reward)}'
                    f'</div></div>',
                    unsafe_allow_html=True,
                )

            # (Standalone Qualitative-Flags strip removed — it was a parallel
            # feed from NSE + Google News + RSS that duplicated the News
            # section below. Flags are now merged INTO the News section:
            # summary badge + expander with active-flag detail, one section
            # instead of two.)

            # (Removed: standalone "Signal:" action strip + separate "💬 In
            # plain English" panel. Both duplicated content already shown in
            # the score-hero card above — Signal/Score/Entry/SL/Target/RR live
            # as metrics inside detail_col, and the plain-English blurb has
            # been folded into the same column. One canonical block, no
            # scrolling required to read the same data twice.)

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

            # MULTI-SIGNAL COMPLETION: the panel used to show 4 confirmations
            # (weekly trend · relative strength · earnings · signal agreement)
            # and felt incomplete because it missed two of the FIRST things a
            # trader eyes on a chart — where price sits relative to its 50-day
            # and 200-day moving averages. Both come free from `df` already in
            # scope (SMA_50/SMA_200 are computed in the price loader), so no
            # extra network calls.
            _ma50_txt, _ma50_c = "—", "var(--dim)"
            _ma200_txt, _ma200_c = "—", "var(--dim)"
            try:
                _px = float(latest["Close"])
                _sma50 = float(latest.get("SMA_50", float("nan")))
                _sma200 = float(latest.get("SMA_200", float("nan")))
                if _sma50 == _sma50:   # NaN check
                    _pct50 = (_px / _sma50 - 1) * 100
                    if _pct50 >= 2:
                        _ma50_txt = f"🟢 Above ({_pct50:+.1f}%)"; _ma50_c = "var(--bull)"
                    elif _pct50 <= -2:
                        _ma50_txt = f"🔴 Below ({_pct50:+.1f}%)"; _ma50_c = "var(--bear)"
                    else:
                        _ma50_txt = f"🟡 At ({_pct50:+.1f}%)";    _ma50_c = "var(--amber)"
                if _sma200 == _sma200:
                    _pct200 = (_px / _sma200 - 1) * 100
                    if _pct200 >= 2:
                        _ma200_txt = f"🟢 Above ({_pct200:+.1f}%)"; _ma200_c = "var(--bull)"
                    elif _pct200 <= -2:
                        _ma200_txt = f"🔴 Below ({_pct200:+.1f}%)"; _ma200_c = "var(--bear)"
                    else:
                        _ma200_txt = f"🟡 At ({_pct200:+.1f}%)";    _ma200_c = "var(--amber)"
            except Exception as _ma_err:
                import logging
                logging.getLogger("dashboard.analyze_stock").debug(
                    "multi-signal MA cell derivation failed for %s: %s", ticker, _ma_err)

            st.markdown(
                f'<div style="background:var(--surface);border:1px solid rgba(255,255,255,.06);'
                f'border-radius:12px;padding:14px 18px;margin-bottom:12px">'
                f'<div style="font-size:11px;color:var(--accent);font-weight:700;'
                f'text-transform:uppercase;letter-spacing:1px;margin-bottom:10px">'
                f'🔬 Multi-Signal Confirmation</div>'
                f'<div style="display:flex;gap:22px;flex-wrap:wrap;align-items:flex-start">'
                # Conviction score
                f'<div style="border-right:1px solid rgba(255,255,255,.08);padding-right:18px">'
                f'<div style="font-size:10px;color:var(--faint)">CONVICTION</div>'
                f'<div style="font-size:24px;font-weight:800;color:{_cv_c}">{_conviction:.0f}'
                f'<span style="font-size:12px;color:var(--dim)"> /100</span></div>'
                + (
                    f'<div style="font-size:10px;color:{_delta_c}">base {cs.score:.0f} · {_delta_s} confirmation</div>'
                    if _confirmation_available
                    else '<div style="font-size:10px;color:var(--dim)">⏸ confirmation unavailable</div>'
                ) +
                f'</div>'
                f'<div><div style="font-size:10px;color:var(--faint)">WEEKLY TREND</div>'
                f'<div style="font-size:14px;font-weight:700;color:{_wk_c}">{_wk_txt}</div></div>'
                f'<div><div style="font-size:10px;color:var(--faint)">RELATIVE STRENGTH</div>'
                f'<div style="font-size:14px;font-weight:700;color:{_rs_c}">{_rs_txt}</div></div>'
                f'<div><div style="font-size:10px;color:var(--faint)">EARNINGS</div>'
                f'<div style="margin-top:2px">{_ed_badge}</div></div>'
                f'<div><div style="font-size:10px;color:var(--faint)">SIGNAL AGREEMENT</div>'
                f'<div style="font-size:14px;font-weight:700;color:{_agr_c}">'
                + (f'{_bull} of {_tot} bullish' if _confirmation_available else '—') +
                f'</div></div>'
                f'<div><div style="font-size:10px;color:var(--faint)">PRICE VS 50DMA</div>'
                f'<div style="font-size:14px;font-weight:700;color:{_ma50_c}">{_ma50_txt}</div></div>'
                f'<div><div style="font-size:10px;color:var(--faint)">PRICE VS 200DMA</div>'
                f'<div style="font-size:14px;font-weight:700;color:{_ma200_c}">{_ma200_txt}</div></div>'
                f'</div>'
                + (
                    f'<div style="font-size:11px;color:var(--dim);margin-top:8px">'
                    f'Conviction adjustments: {" · ".join(_conf_reasons)}</div>'
                    if _conf_reasons
                    else (
                        '<div style="font-size:11px;color:var(--dim);margin-top:8px">'
                        'No adjustment — confirmation signals are neutral.</div>'
                        if _confirmation_available
                        else
                        '<div style="font-size:11px;color:var(--dim);margin-top:8px">'
                        '⏸ Deep confirmation unavailable — conviction equals base score.</div>'
                    )
                )
                + "</div>",
                unsafe_allow_html=True,
            )

            # Signal checklist
            if _confirmation_available:
                with st.expander(f"🔎 See all {_tot} signals", expanded=False):
                    for _sname, _sok in _dc.get("signals", []):
                        st.markdown(
                            f'<div style="font-size:13px;color:var(--ink-mid);padding:2px 0">'
                            f'{"🟢" if _sok else "⚪"} {_sname}</div>',
                            unsafe_allow_html=True,
                        )

            # LAYOUT-REORDER: Paper Trade popover promoted to here — right
            # under the Multi-Signal Confirmation block — because that's the
            # moment the user has all the info to decide: score, verdict,
            # horizon fit, entry/SL/target, R:R, weekly trend, MA position,
            # earnings window. Previously it lived after the Chart + News,
            # forcing a long scroll past sections the user had already read.
            _pt_col, _pt_info = st.columns([1, 3])
            with _pt_col:
                _paper_trade_popover(
                    ticker,
                    entry   = cs.entry,
                    sl      = cs.stop_loss,
                    tp      = cs.target,
                    reason  = f"{_display_label(cs.action)} score={cs.score:.0f}/90: {cs.headline}",
                    key     = f"as_ptpop_{ticker}",
                    label   = "📌 Paper Trade This Signal",
                )
            with _pt_info:
                st.info(
                    "📌 **Paper Trading** lets you test this signal without real money. "
                    "Track it in the **📂 Paper Trades** page to see if the model's calls are accurate."
                )

            # Utility row (Watchlist / Re-Analyze — Paper Trade removed, its
            # popover above replaces the navigate-to-page shortcut that used
            # to live here).
            _as_c1, _as_c2, _as_c3 = st.columns([1, 1, 4])
            if _as_c1.button("➕ Watchlist", key=f"as_wl_{ticker}", width="stretch"):
                _wl = st.session_state.setdefault("watchlist", [])
                if ticker not in _wl:
                    _wl.append(ticker)
                st.toast(f"{ticker.replace('.NS','')} added to watchlist ✓")
            if _as_c2.button("🔄 Re-Analyze", key=f"as_re_{ticker}", width="stretch"):
                # FIX MKT3: was a blanket st.cache_data.clear() — wiped every
                # other page's cached data too (Top Picks, watchlist scans,
                # etc.), not just this ticker's analysis. load_ticker_df is
                # already imported at the top of this module, so it's safe
                # to clear directly here.
                load_ticker_df.clear()
                st.rerun()

            # ── Technical indicators (behind an expander now) ──────────────
            # These 6 numbers (RSI/ADX/ATR/Vol/Stoch/VWAP%) were previously
            # rendered as a full-width metric row that DUPLICATED the readings
            # the pre-trade checklist above already interprets in plain English
            # (RSI zone, ADX trend-strength, volume surge, MTF alignment). The
            # raw values are useful for a chart-reading user but not for the
            # everyday read — so they now live behind an expander, keeping the
            # main scroll clean while remaining one click away.
            st.markdown("---")
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
                indicators_display = [
                    ("RSI (14)",  f"{_rsi_val:.1f}",
                     f"Oversold (<30){_ovr_suf}"   if _rsi_val < 30
                     else "Overbought (>70)" if _rsi_val > 70
                     else "Normal"),
                    ("ADX",       f"{latest.get('ADX', 0):.1f}",
                     "Trending (>25)" if latest.get("ADX", 0) > 25 else "Ranging"),
                    ("ATR",       f"₹{latest.get('ATR', 0):.1f}", "Daily move range"),
                    ("Vol Ratio", f"{latest.get('Volume_Ratio', 0):.2f}x",
                     "High volume" if latest.get("Volume_Ratio", 1) > 1.5 else "Normal"),
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
                    _pat_out = _sh2_pat(active_pats,
                                        latest.get("Volume_Ratio"))
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


            # ────────────────────────────────────────────────────────────────
            # Task 2.4 F3 PR C.1 (audit docs/RENDER_SPEED_AUDIT_2026-09.md):
            # tail sections grouped into tabs so this page no longer renders
            # as a 600+ LOC vertical scroll. Streamlit still runs every tab's
            # body on every rerun (compute-gating is PR C.2's job); this ships
            # the visual/DOM win first as a low-risk incremental improvement.
            # ────────────────────────────────────────────────────────────────
            # Slice 2 (docs/UI_UX_DESIGN_2026-09.md) -- the old standalone
            # "Deep Dive Analysis" page now folds into Analyze Stock as an
            # 8th tab. Everything Deep Dive used to render on its own page
            # (live price / chart / signal / short-term-vs-long-term compare)
            # was already duplicated across the other tabs on this page;
            # what stays unique is the LLM prompt builder + saved-research
            # history, now inside "🔬 Deep Dive" below.
            tab_chart, tab_news, tab_thesis, tab_fund, tab_val, tab_liq, tab_pf, tab_dd = st.tabs([
                "📊 Chart", "📰 News & Flags", "🧭 Thesis",
                "🏢 Fundamentals", "💰 Valuation", "💧 Liquidity",
                "🧩 Portfolio Fit", "🔬 Deep Dive",
            ])

            # ────────────────────────────────────────────────────────────────
            # Task 2.4 F3 PR C.2: each tab body wrapped in @st.fragment so
            # widget-driven reruns inside a tab only re-execute that tab's
            # body, not the other 6 tabs and not the outer script. Fragments
            # capture parent-scope vars (df_chart, cs, latest, _fv, ...) via
            # closure so no context needs to be threaded through as args.
            # ────────────────────────────────────────────────────────────────

            @st.fragment
            def _frag_chart():
                # ── Chart ──────────────────────────────────────────────────────
                st.markdown("---")
                st.markdown(
                    '<div class="t-h2" style="margin:14px 0 6px 0">'
                    '📊 Price Chart</div>',
                    unsafe_allow_html=True,
                )
                st.plotly_chart(build_price_chart(df_chart, ticker, period=period),
                                width="stretch")
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
                st.markdown("---")
                st.markdown(
                    f'<div class="t-h2" style="margin:14px 0 6px 0">'
                    f'📰 News &amp; Flags <span style="color:var(--dim);'
                    f'font-weight:400;font-size:12px">'
                    f'{get_display_name(ticker)}</span></div>',
                    unsafe_allow_html=True,
                )

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

                # (Paper Trade popover moved UP to right below Multi-Signal
                # Confirmation — see the Paper Trade block earlier on the page.)
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
                st.markdown("---")
                st.markdown(
                    '<div class="t-h2" style="margin:14px 0 6px 0">'
                    '🧭 Investment Thesis (structured)</div>',
                    unsafe_allow_html=True,
                )
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
                st.markdown("---")
                st.markdown(
                    '<div class="t-h2" style="margin:14px 0 6px 0">'
                    '📊 Fundamentals</div>',
                    unsafe_allow_html=True,
                )
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

                    _fc1, _fc2, _fc3, _fc4 = st.columns(4)
                    _f_show(_fc1, _f_res["revenue_cagr"])
                    _f_show(_fc2, _f_res["eps_cagr"])
                    _f_show(_fc3, _f_res["roe"])
                    _f_show(_fc4, _f_res["debt_to_equity"])

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

                    from analysis.sector_classification import classify_sector as _classify
                    _sp = _classify(
                        getattr(cs, "sector", None),
                        name=getattr(cs, "company_name", None),
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
                st.markdown("---")
                st.markdown(
                    '<div class="t-h2" style="margin:14px 0 6px 0">'
                    '💰 Valuation Context</div>',
                    unsafe_allow_html=True,
                )
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
            with tab_val:
                _frag_val()

            @st.fragment
            def _frag_liq():
                # ── Liquidity Context ──────────────────────────────────────────
                # NOTE: _liq_ctx is computed EARLIER (right after _dc) so the
                # Investment Thesis section can consume it; this render block just
                # displays what was already computed. Do not re-compute here.
                st.markdown("---")
                st.markdown(
                    '<div class="t-h2" style="margin:14px 0 6px 0">'
                    '💧 Liquidity Context</div>',
                    unsafe_allow_html=True,
                )
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
            with tab_liq:
                _frag_liq()

            @st.fragment
            def _frag_pf():
                # ── Portfolio Fit — FIX A5 + A9: cached, reads manual holdings ──
                st.markdown("---")
                st.markdown(
                    '<div class="t-h2" style="margin:14px 0 6px 0">'
                    '🧩 Portfolio Fit Assessment</div>',
                    unsafe_allow_html=True,
                )
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
                        with st.spinner("Assessing fit against your portfolio…"):
                            _fit = assess_fit(
                                build_fit_inputs(ticker, _pf_holds, candidate_thesis=_th)
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

            @st.fragment
            def _frag_deep_dive():
                # Slice 2 -- Deep Dive folded in from the old page 20.
                # See dashboard/shared/deep_dive_tab.py for the unique
                # bits (LLM prompt builder + saved research history).
                from dashboard.shared.deep_dive_tab import render_deep_dive_tab
                render_deep_dive_tab(ticker)
            with tab_dd:
                _frag_deep_dive()

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
                st.error(f"Analysis failed: {e}")
                import traceback
                st.code(traceback.format_exc())


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
