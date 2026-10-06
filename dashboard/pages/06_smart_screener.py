"""Smart Screener - NSE Smart Investor.

LAYOUT (mockup artboard 08, 2026-09-12 "proposed layout" artifact)
────────────────────────────────────────────────────────────────────
Screen presets across the top, a filter panel on the left, the result set
on the right: count + export header, the ranked table, and one detail panel
for the selected setup (replaces 30 stacked expanders).

SCR-PERSIST  Results now live in st.session_state. Before, they only existed
             in the run where "Run Screen" was clicked, so any later widget
             interaction threw a finished multi-minute scan away. The revenue
             growth filter now narrows the stored result set live instead of
             being frozen at scan time.
"""
import os
import sys
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import datetime as _dt
import html
import logging

import pandas as pd
import streamlit as st

from dashboard.shared.design import apply_design
from dashboard.shared.nav import render_sidebar
from dashboard.shared.cache import get_vix_info, get_display_name
from dashboard.shared.chart_helpers import render_top_bar
from dashboard.shared.trade_utils import _display_label, _paper_trade_popover
from dashboard.shared.ui_components import (
    chip_pill, chip_tag, empty_state, fmt_inr, gate_strip, rank_chip, section_header,
)

_log = logging.getLogger("dashboard.smart_screener")

apply_design()
render_sidebar(current="Smart Screener")
render_top_bar()

st.markdown('<h1 class="page-title-serif">Smart <em>Stock Screener</em></h1>', unsafe_allow_html=True)
st.markdown(
    '<p class="page-subtitle">Scan an NSE universe with a screen preset. Each match can carry the '
    '0–90 trend-quality composite: trend health, not a return forecast.</p>',
    unsafe_allow_html=True,
)

# ── Presets across the top ───────────────────────────────────────────────────
_PRESETS = {
    "All 4 screens":    "all",
    "Oversold bounce":  "oversold",
    # FIX SR1: was "momentum" — collided with the legacy CLI strategy string in
    # trading/signals.py and silently ran check_momentum_signal() instead of
    # check_momentum_leader().
    "Momentum leaders": "momentum_leader",
    "Breakouts":        "breakout",
    "Pullback to SMA20": "pullback_SMA20",
    "VCP (Minervini)":  "vcp",
}
screen_choice = st.pills(
    "Screen preset", list(_PRESETS), default="All 4 screens", required=True,
    key="scr_preset", label_visibility="collapsed",
    help=("VCP = Volatility Contraction Pattern (Mark Minervini). Finds the OPPOSITE "
          "of oversold: Stage-2 uptrend, tight base, volume dry-up, right at the pivot. "
          "Very selective; expect fewer matches than the other screens."),
)
screen_key = _PRESETS.get(screen_choice or "All 4 screens", "all")

_UNIVERSES = {
    "NIFTY 50 (50 stocks)":    "nifty50",
    "NIFTY 100 (100 stocks)":  "nifty100",
    "NIFTY 200 (200 stocks)":  "nifty200",
    "NIFTY 500 (~400 stocks)": "nifty500",
}
_RG_THRESH = {"Any": None, "> 0%": 0.0, "> 5%": 5.0, "> 10%": 10.0, "> 15%": 15.0}

_left, _right = st.columns([1.15, 3], gap="large")

# ── Filter panel (left) ──────────────────────────────────────────────────────
with _left:
    with st.container(border=True):
        st.markdown('<div class="t-label">Filters</div>', unsafe_allow_html=True)
        universe_choice = st.selectbox("Universe", list(_UNIVERSES))
        enrich_scores = st.toggle(
            "Trend-quality score", value=True,
            help="Adds the 0–90 composite score to each result (slower).")
        # Revenue-growth filter (R1 — docs/REVENUE_GROWTH_DISCOVERY_AUDIT.md).
        # Capped at 15%: >20% concentrated results into one sector and silently
        # removed 19/21 top trend-quality names. Default "Any" so the column
        # informs without filtering; missing data is included by default.
        rg_filter = st.selectbox(
            "Revenue growth", list(_RG_THRESH), index=0, key="scr_rg_filter",
            help="Filters results by annualised revenue growth (audited statements). "
                 "Capped at 15%: higher thresholds were shown to distort discovery.")
        rg_excl_missing = st.toggle(
            "Hide missing growth data", value=False, key="scr_rg_excl",
            help="Off (default): stocks with no growth data stay visible and show '–'. "
                 "Only ~4% of the universe lacks data.")
        scan_btn = st.button("🔍 Run screen", type="primary", width="stretch")

    # Phase 1 (UI honesty): regime reliability next to live score output
    from dashboard.shared.disclosures import (
        render_regime_reliability_note as _scr_regime_note,
        render_score_methodology as _scr_score_methodology,
    )
    _scr_regime_note()
    _scr_score_methodology()

    # FIX SCR-XREF — the three screener-family pages (this one, Tomorrow's
    # Watchlist, TQS Scanner) answer "which names should I look at" with
    # different universes / engines / cadences. Cross-links teach the map.
    with st.expander("↔️ Also see", expanded=False):
        st.markdown(
            "- **Tomorrow's Watchlist**: the same style of scan, pre-computed on "
            "yesterday's close so you don't wait.\n"
            "- **TQS Scanner**: a *different* scoring engine (four-pillar Trend "
            "Quality Score, 0-90) on the same universes. Cross-check a name here "
            "against its TQS reading before acting."
        )


# ── Scan (writes the result set to session state) ────────────────────────────
def _run_scan() -> None:
    from data.universe import get_universe
    from trading.signals import scan_tickers
    universe = get_universe(_UNIVERSES[universe_choice])

    with st.spinner(f"Scanning {len(universe)} stocks… this may take a few minutes…"):
        signals = scan_tickers(universe, strategy=screen_key, period="1y")

    # New listings (under 60 sessions) don't get ranked: their indicators are
    # still warming up. Reads the frames the scan just cached; never refetches.
    from dashboard.shared.cache import _list_exclusion
    _excluded = {}
    for _sig in signals or []:
        _why = _list_exclusion(_sig.get("ticker", ""), period="1y")
        if _why and _why.startswith("new listing"):
            _excluded[_sig["ticker"]] = _why
    signals = [s for s in (signals or []) if s.get("ticker") not in _excluded]

    if signals and enrich_scores:
        from analysis.score import score_stock
        vix_info = get_vix_info()
        prog = st.progress(0, text="Scoring matches…")
        for i, sig in enumerate(signals):
            try:
                cs = score_stock(sig["ticker"], period="1y", vix_info=vix_info)
                sig["composite_score"] = round(cs.score, 1)
                sig["grade"]           = cs.grade
                sig["action"]          = cs.action
                sig["narrative"]       = cs.headline
                sig["stop_loss"]       = round(cs.stop_loss, 2)
                sig["target"]          = round(cs.target, 2)
            except Exception as _score_e:
                _log.debug("score_stock failed for %s: %s — marking unscored",
                           sig.get("ticker"), _score_e)
                # Unscored — never fake a neutral 50/"C" and rank it alongside
                # real results.
                sig["composite_score"] = None
                sig["grade"]           = "–"
                sig["action"]          = sig.get("action", "WATCHLIST")
                sig["narrative"]       = ""
            prog.progress((i + 1) / len(signals), text="Scoring matches…")
        prog.empty()
        signals = sorted(
            signals,
            key=lambda x: (x.get("composite_score") is not None, x.get("composite_score") or 0),
            reverse=True)  # unscored rows sort last

    from concurrent.futures import ThreadPoolExecutor, wait as _fwait
    if signals:
        # Revenue-growth enrichment (R1) — bounded fetch, graceful "–". Per
        # the discovery audit: never block indefinitely; display/filter only,
        # the composite ordering above is never touched.
        def _rg_for(sig):
            try:
                from analysis.fundamentals.service import default_service
                from analysis.fundamentals.analytics import revenue_cagr
                cf = default_service().get_fundamentals(sig["ticker"])
                if cf is not None:
                    r = revenue_cagr(cf, years=5)
                    if getattr(r, "available", False) and r.value is not None:
                        return float(r.value)
            except Exception as _rg_thr_e:
                _log.debug("rev growth fetch failed for %s: %s", sig.get("ticker"), _rg_thr_e)
            return None

        with st.spinner("Fetching revenue growth for results…"):
            _rg_pool = ThreadPoolExecutor(max_workers=8)
            try:
                _rg_futs = {_rg_pool.submit(_rg_for, s): s for s in signals}
                _done, _ = _fwait(list(_rg_futs), timeout=30)
                for _f in _done:
                    try:
                        _rg_futs[_f]["rev_growth"] = _f.result(timeout=0)
                    except Exception:
                        _rg_futs[_f]["rev_growth"] = None
            finally:
                # cancel_futures drops anything not yet started instead of
                # leaking threads after the page run ends.
                _rg_pool.shutdown(wait=False, cancel_futures=True)
        for s in signals:
            s.setdefault("rev_growth", None)

        # Sparklines — 22 daily closes via the 5-min-cached _sparkline_closes;
        # anything not back in 15 s renders as an empty cell.
        from dashboard.shared.cache import _sparkline_closes
        _sp_pool = ThreadPoolExecutor(max_workers=8)
        try:
            _sp_futs = {_sp_pool.submit(_sparkline_closes, s["ticker"]): s for s in signals[:30]}
            _sp_done, _ = _fwait(list(_sp_futs), timeout=15)
            for _f in _sp_done:
                try:
                    _sp_futs[_f]["_spark"] = _f.result(timeout=0) or []
                except Exception:
                    _sp_futs[_f]["_spark"] = []
        finally:
            _sp_pool.shutdown(wait=False, cancel_futures=True)

        # R:R computed once per signal. None ("–") whenever the risk leg isn't
        # a sane long — never clamp a zero/negative risk up to 0.01.
        for _sg in signals:
            _rr = _sg.get("rr_ratio")
            if _rr is None:
                _px = _sg.get("price", 0) or 0
                _sl = _sg.get("sl", _sg.get("stop_loss", 0)) or 0
                _tp = _sg.get("tp", _sg.get("target", None))
                _risk = _px - _sl
                _rr = round((_tp - _px) / _risk, 1) if (_tp and _risk > 0.01) else None
            _sg["_rr"] = _rr

    st.session_state["scr_run"] = {
        "signals":  signals or [],
        "universe": universe_choice,
        "n_universe": len(universe),
        "screen":   screen_choice,
        "enriched": bool(enrich_scores),
        "excluded": _excluded,
        "ran_at":   _dt.datetime.now().strftime("%d %b %H:%M"),
    }
    st.session_state.pop("scr_detail", None)


def _px(v) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "–"
    if not f or f <= 0:
        return "–"
    return "₹" + fmt_inr(f, 2 if f < 1000 else 0)


def _rg_passes(sig) -> bool:
    g = sig.get("rev_growth")
    if g is None:
        return not rg_excl_missing
    th = _RG_THRESH.get(rg_filter)
    return True if th is None else g > th


def _render_detail(sig: dict, rank: int, enriched: bool) -> None:
    t = sig["ticker"].replace(".NS", "")
    price = sig.get("price", 0) or 0
    sl = sig.get("sl", sig.get("stop_loss", 0)) or 0
    tp = sig.get("tp", sig.get("target", None))
    rr = sig.get("_rr")
    rg = sig.get("rev_growth")
    sector = sig.get("sector", "") or ""
    cs = sig.get("composite_score")
    with st.container(border=True):
        _chips = chip_tag(html.escape(str(sig.get("screen", "") or "screen").replace("_", " ")))
        if sector:
            _chips += chip_tag(html.escape(sector))
        _score = (f'<span class="t-value-sm">{cs:.0f}/90</span>' if (enriched and cs is not None) else "")
        st.markdown(
            f'<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">'
            f'{rank_chip(rank)}<span style="font-weight:700;font-size:16px;color:var(--ink)">{html.escape(t)}</span>'
            f'<span style="color:var(--dim);font-size:12px">{html.escape(get_display_name(sig["ticker"]))}</span>'
            f'{chip_pill(html.escape(_display_label(sig.get("action", "WATCHLIST"))), "neutral")}'
            f'{_chips}<span style="margin-left:auto">{_score}</span></div>',
            unsafe_allow_html=True,
        )
        _rr_tone = ("bull" if rr >= 2 else "amber") if rr else "dim"
        st.markdown(gate_strip([
            ("Entry", _px(price), "last close"),
            ("Stop", _px(sl), str(sig.get("stop_type", "atr")).upper() + " stop"),
            ("Target", _px(tp) if tp else "Trail SMA20", "scenario, not a call"),
            ("R:R", f'<span style="color:var(--{_rr_tone})">{rr:.1f}x</span>' if rr else "–",
             "reward per unit of risk"),
            ("Rev growth /yr", f"{rg:+.1f}%" if rg is not None else "–", "audited statements"),
        ]), unsafe_allow_html=True)
        if sig.get("reason"):
            st.caption(f"📌 {sig['reason']}")
        if enriched and sig.get("narrative"):
            st.markdown(f'<div class="t-body" style="color:var(--ink-mid)">{html.escape(sig["narrative"])}</div>',
                        unsafe_allow_html=True)
        b1, b2 = st.columns(2)
        with b1:
            if price and sl and tp and price > sl:
                _paper_trade_popover(sig["ticker"], price, sl, tp,
                                     reason=f"Screener ({sig.get('screen', '')}): {sig.get('reason', '')}",
                                     key=f"scr_{sig['ticker']}", label=f"📌 Paper Trade {t}")
        with b2:
            if st.button(f"📊 Analyze {t}", key=f"scr_{sig['ticker']}_analyze", width="stretch"):
                st.session_state["analyze_ticker"] = sig["ticker"]
                st.session_state["_goto_page"] = "🔍 Analyze Stock"
                st.rerun()


# ── Result set (right) ───────────────────────────────────────────────────────
with _right:
    if scan_btn:
        _run_scan()

    run = st.session_state.get("scr_run")
    if run:
        from dashboard.shared.edge_flags_ui import excluded_note as _scr_excluded_note
        _scr_excl = _scr_excluded_note(run.get("excluded"))
        if _scr_excl:
            st.caption(_scr_excl)
    if not run:
        st.markdown(empty_state(
            "Pick a preset and run the screen",
            "Results stay on the page while you filter and inspect them.", icon="🔍"),
            unsafe_allow_html=True)
    elif not run["signals"]:
        st.markdown(empty_state(
            "No setups for this screen",
            f"{run['screen']} on {run['universe']} found nothing. "
            "Try a broader universe or another preset.", icon="∅"),
            unsafe_allow_html=True)
    else:
        _all = run["signals"]
        signals = [s for s in _all if _rg_passes(s)]
        _enriched = run["enriched"]
        _hc1, _hc2 = st.columns([4, 1], vertical_alignment="bottom")
        with _hc1:
            _filtered_note = (f" · revenue filter kept {len(signals)} of {len(_all)}"
                              if len(signals) != len(_all) else "")
            st.markdown(
                section_header(
                    f'<span style="color:var(--accent)">{len(signals)}</span> setups match',
                    f"{html.escape(run['screen'])} · {html.escape(run['universe'])} · "
                    f"run {run['ran_at']}{_filtered_note}"),
                unsafe_allow_html=True)
        with _hc2:
            st.download_button(
                "↗ Export CSV",
                data=pd.DataFrame(signals).drop(columns=["_spark"], errors="ignore")
                    .to_csv(index=False).encode(),
                file_name="nse_screener.csv", mime="text/csv", width="stretch",
            )

        if not signals:
            st.markdown(empty_state("Revenue filter removed every setup",
                                    "Loosen the revenue-growth filter to see them again."),
                        unsafe_allow_html=True)
        else:
            from dashboard.shared.table_styles import (
                posture_label as _ts_posture, pinned_text_col as _ts_pin,
            )
            _top = signals[:30]
            _sig_df = pd.DataFrame([{
                "#": f"#{i}",
                "Ticker": s["ticker"].replace(".NS", ""),
                "Posture": _ts_posture(s.get("action", "WATCHLIST")),
                "Screen": (s.get("screen", "") or "–").replace("_", " "),
                "Sector": s.get("sector", "") or "–",
                "Score": s.get("composite_score") if _enriched else None,
                "Price": s.get("price", 0) or 0,
                "22d": s.get("_spark", []),
                "R:R": s.get("_rr"),
                "Rev Growth /yr": s.get("rev_growth"),
            } for i, s in enumerate(_top, start=1)])
            # Coerce numeric columns so a missing value renders as an empty
            # cell — an all-None leading run left R:R as object dtype and the
            # grid printed the literal string "None".
            for _nc in ("Score", "Price", "R:R", "Rev Growth /yr"):
                _sig_df[_nc] = pd.to_numeric(_sig_df[_nc], errors="coerce")
            if not _enriched:
                _sig_df = _sig_df.drop(columns=["Score"])
            st.dataframe(
                _sig_df, hide_index=True, width="stretch",
                column_config={
                    # Pin rank too — pinned columns render first.
                    "#": st.column_config.TextColumn("#", width="small", pinned=True),
                    "Ticker": _ts_pin("Ticker"),
                    "Score": st.column_config.ProgressColumn(
                        "Score", min_value=0, max_value=90, format="%d"),
                    "Price": st.column_config.NumberColumn(format="₹%.2f"),
                    "R:R": st.column_config.NumberColumn(format="%.1fx"),
                    "Rev Growth /yr": st.column_config.NumberColumn(format="%+.1f%%"),
                    "22d": st.column_config.LineChartColumn(
                        "22d", width="small",
                        help="Last 22 daily closes (shape only — each row is self-scaled)."),
                },
            )
            if len(signals) > 30:
                st.caption(f"Showing the top 30 of {len(signals)} by composite; "
                           "the CSV export has all of them.")

            _sel = st.selectbox(
                "Setup detail & actions", options=list(range(len(_top))),
                format_func=lambda i: (f"#{i + 1}  {_top[i]['ticker'].replace('.NS', '')}"
                                       + (f" · {_top[i]['composite_score']:.0f}/90"
                                          if _enriched and _top[i].get("composite_score") is not None
                                          else "")),
                key="scr_detail",
            )
            _render_detail(_top[_sel], _sel + 1, _enriched)

        from dashboard.shared.disclosures import (
            render_revenue_growth_evidence as _scr_rg_evidence,
        )
        _scr_rg_evidence()

        # DT1 + DT2 · attribution below the results. Prices come from
        # data/fetcher.py's tiered pipeline; screens run per scan.
        try:
            from dashboard.shared.ui_components import data_as_of as _scr_asof
            st.markdown(_scr_asof(run["ran_at"], source="yfinance", ttl_hint="scan cache 15 min"),
                        unsafe_allow_html=True)
        except Exception:
            pass
