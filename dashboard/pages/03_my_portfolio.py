"""My Portfolio - NSE Smart Investor (multipage page; body verbatim from app.py).

FIXES applied in this revision
───────────────────────────────
MH1  Removed the entire CSV-upload / sample-CSV-download / Angel-One-import
     flow. The page is now driven by manually-entered holdings (ticker, qty,
     avg buy price, date bought) persisted via load_manual_holdings() /
     save_manual_holdings() in trade_utils.py. No price/qty suggestions are
     given — the user types exactly what they hold. This also removes the
     dependency on _safe_tmpfile that was causing the missing-import error.

MH2  Portfolio Heatmap is now inside a collapsed st.expander, matching the
     Sector Breakdown treatment, instead of always rendering full-width.

MH3  The "📊 Analyze" button on each holding card sets
     st.session_state["analyze_ticker"] before navigating — paired with the
     corresponding read on the Analyze Stock page so the ticker is honored
     automatically instead of requiring manual re-entry.

MH4  FIX (this revision) — PortfolioManager(csv_path) does `Path(csv_path)`
     internally, so it has ALWAYS required an actual file path, never a
     DataFrame. MH1 dropped the old CSV-upload flow (and the _safe_tmpfile
     helper that used to bridge this) but nothing replaced that bridge, so
     every portfolio analysis run failed with
     "TypeError: ... not 'DataFrame'". A small temp-file helper
     (_holdings_csv_tmpfile) now writes the in-memory holdings to a real
     temp CSV and hands PortfolioManager that path, exactly as it expects.
     The temp file is cleaned up in a finally block.

MH5  FIX (this revision) — "Manage holdings" rendered raw input widgets with
     no column labels, so an opened expander was a wall of unlabeled boxes.
     Added a header row (Ticker / Qty / Avg ₹ / Date bought) above the list.

MH6  FIX (this revision) — "Add a holding" required typing the exact NSE
     ticker with no help. Added a searchable "Find by company name" dropdown
     (powered by the existing STOCK_SEARCH_MAP) that auto-fills the ticker
     field, similar to the lookup already used elsewhere in the app (e.g.
     Paper Trades). Manual ticker entry still works for anything not in the
     map.

Phase2  UI honesty — all action labels shown to the user go through
     _display_label() so "STRONG BUY" → "Strong Trend ▲▲" etc. Internal
     strings (DB, CSV export, sort keys) are unchanged.

QualityFix  compute_quality_score returns 0 for no-data; UI now treats
     0 as None so the table shows "—" instead of a misleading Quality=0.

DC1  DECISION-CLARITY REORDER (this revision) — the page previously buried
     the actual buy/hold/exit decision (holdings cards with score, action,
     SL/target, headline) below six other sections: live-price table, PM
     scoring spinner, health metrics, signal monitor, sector breakdown, and
     a full Risk & Performance block with NAV curve / correlation matrix.
     Someone opening the page had to scroll past all of that to reach the
     one thing that actually helps them decide anything. Reordered to:
     (1) fast top-line today/overall/value boxes, (2) PM scoring,
     (3) signal-flip alerts + health narrative, (4) the actual decision —
     holdings cards, right up top, (5) everything else under a clearly
     labelled "📊 Deeper Analysis (optional)" divider, unchanged internally.

DC2  MERGED REDUNDANT VIEWS — the old page rendered the SAME per-stock P&L
     twice: once as a plain live-price table, once as the scored holdings
     cards below. That's wasted render work and, worse, gives two numbers
     for "today's change" that can drift apart (table used live-price-cache
     `chg`, cards used PortfolioManager's `today_chg_pct` — same underlying
     data, two code paths). The table is gone; the fast top-line boxes
     (today/overall/value) are the only thing that runs off the quick
     live-price cache now, and the cards are the only per-stock P&L view.

DC3  CARDS NOW SURFACE RISK-REWARD + THE FULL "WHY" — score_stock() already
     computes a risk_reward ratio and a full multi-sentence narrative
     (pattern detection, sector rank, VIX regime context, entry/SL/TP
     reasoning) but the old card only ever showed the one-line headline and
     threw the rest away. No new fetches: added an RR badge on the card and
     a one-click "Why?" expander with the full narrative, straight from the
     HoldingResult that PortfolioManager already computed.
"""
import os
import sys
import tempfile
import pandas as pd
import plotly.express as px
import streamlit as st
import datetime as _dt

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from dashboard.shared.design import apply_design
from dashboard.shared.nav import render_sidebar
from dashboard.shared.chart_helpers import render_top_bar
from dashboard.shared.tokens import COLORS as _TK  # noqa: E402
import html as _html
from dashboard.shared.ui_components import (
    chip_pill, ticker_hover_wrap, section_header, share_bars, fmt_inr as _fmt_inr,
)
from dashboard.shared.trade_utils import (
    _action_emoji,
    _display_label,                # Phase 2 UI honesty
    _paper_trade_popover,
    _portfolio_live_prices,
    clear_price_caches,            # FIX3
    load_signal_monitor_state,     # FIX TU5
    save_signal_monitor_state,     # FIX TU5
    load_manual_holdings,          # FIX MH1
    save_manual_holdings,          # FIX MH1
)
from dashboard.shared.chart_helpers import _ROOT, render_top_bar, rdylgn_bg, tick_pulse_tracker
from dashboard.shared.squareoff_monitor import render_squareoff_monitor
from dashboard.shared.cache import STOCK_SEARCH_MAP  # FIX MH6
from analysis.portfolio_concentration import analyze_concentration, concentration_grade
from analysis.portfolio_fundamentals import (
    batch_fetch_fundamentals, compute_quality_score, quality_score_coverage,
)

apply_design()
render_sidebar(current="My Portfolio")
render_top_bar()

st.markdown('<h1 class="page-title-serif">My <em>Portfolio</em></h1>', unsafe_allow_html=True)

st.markdown(
    "Your holdings health check — live prices, trend-quality scores, and plain English guidance for each stock."
)

# FIX UI-REGIME — same regime badge as Command Centre / Analyze Stock so
# the "current regime affects the read" context is consistent across the app.
try:
    from dashboard.shared.ui_components import regime_badge as _ui_regime_badge
    @st.cache_data(ttl=1800, show_spinner=False)
    def _mp_regime_snap():
        from analysis.regime import snapshot_live
        try:
            return snapshot_live().as_dict()
        except Exception:
            return None
    _mp_reg = _mp_regime_snap()
    if _mp_reg:
        st.markdown(
            _ui_regime_badge(_mp_reg.get("label", "unknown"),
                             _mp_reg.get("confidence", "low"),
                             compact=False),
            unsafe_allow_html=True,
        )
except Exception as _mp_reg_err:
    import logging
    logging.getLogger("dashboard.my_portfolio").debug(
        "regime badge render failed: %s", _mp_reg_err)

render_squareoff_monitor(poll_every=60, show_badge=True)


# FIX MH4 — PortfolioManager(csv_path) does Path(csv_path) internally and has
# always required a real file path, never a DataFrame. This writes the
# in-memory holdings list to a temp CSV and returns the path; caller is
# responsible for deleting it (done in a finally block below).
def _holdings_csv_tmpfile(df: pd.DataFrame) -> str:
    _tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".csv", prefix="nse_portfolio_", delete=False, newline=""
    )
    try:
        df.to_csv(_tmp.name, index=False)
    finally:
        _tmp.close()
    return _tmp.name


# ════════════════════════════════════════════════════════════════════════
# FIX MH1 — MANUAL HOLDINGS: add / edit / remove
# ════════════════════════════════════════════════════════════════════════
_holdings = load_manual_holdings()

# FIX MH7 — the company-name lookup dropdown lives OUTSIDE the form (so
# picking a name can update the ticker live), which meant clear_on_submit
# never touched it: after a successful add it kept showing the previously
# picked company, and that stale selection then silently overrode whatever
# new ticker you typed manually on the NEXT add (this is the "still shows
# the previous stock name when adding" bug). Streamlit also raises
# "cannot be modified after the widget is instantiated" if you try to reset
# a widget's session_state value after that widget has already rendered in
# the current script run — so the reset can't happen at submit time, it has
# to be deferred to the TOP of the next run, before the selectbox exists
# yet. Same clear-pending pattern Analyze Stock already uses for its own
# search box.
_MH_NONE = "— search by company name (optional) —"
if st.session_state.pop("_mh_clear_pending", False):
    st.session_state["mh_company_lookup"] = _MH_NONE

with st.expander("➕ Add a holding", expanded=(len(_holdings) == 0)):
    st.caption(
        "Enter your holding details manually. No price or quantity suggestions — "
        "type exactly what you hold."
    )

    # FIX MH6 — searchable company lookup, outside the form so picking a
    # name can update the ticker field live (form widgets don't rerun on
    # their own change, so this lives just above the form instead).
    _mh_company_options = [_MH_NONE] + sorted(STOCK_SEARCH_MAP.keys())
    _mh_picked_company = st.selectbox(
        "🔎 Find by company name",
        _mh_company_options,
        key="mh_company_lookup",
        help="Start typing a company name to filter — selecting one fills in the "
             "ticker below. Leave on the default option to type a ticker manually.",
    )
    _mh_looked_up_ticker = (
        STOCK_SEARCH_MAP.get(_mh_picked_company, "") if _mh_picked_company != _MH_NONE else ""
    )
    if _mh_looked_up_ticker:
        st.caption(f"→ Ticker: **{_mh_looked_up_ticker}**")

    with st.form("mh_add_form", clear_on_submit=True):
        _f1, _f2, _f3, _f4 = st.columns([2, 1, 1, 1.3])
        with _f1:
            _mh_ticker_manual = st.text_input(
                "Ticker (or use the lookup above)",
                placeholder="e.g. RELIANCE or RELIANCE.NS",
            ).strip().upper()
        with _f2:
            _mh_qty = st.number_input("Quantity", min_value=0.0, step=1.0, value=0.0)
        with _f3:
            _mh_price = st.number_input(
                "Avg buy price (₹)", min_value=0.0, step=0.05, value=0.0, format="%.2f"
            )
        with _f4:
            _mh_date = st.date_input("Date bought", value=_dt.date.today())
        _mh_submit = st.form_submit_button(
            "Add holding", type="primary", width="stretch"
        )

    if _mh_submit:
        # FIX MH6 — the company-name lookup wins if a real company was
        # selected; otherwise fall back to whatever was typed manually.
        _mh_ticker = _mh_looked_up_ticker.replace(".NS", "") if _mh_looked_up_ticker else _mh_ticker_manual
        if not _mh_ticker:
            st.error("Ticker is required — type one, or pick a company from the lookup above.")
        elif _mh_qty <= 0:
            st.error("Quantity must be greater than 0.")
        elif _mh_price <= 0:
            st.error("Avg buy price must be greater than 0.")
        else:
            _norm_ticker = _mh_ticker if _mh_ticker.endswith(".NS") else _mh_ticker + ".NS"

            # FIX MH8 — unlike Analyze Stock (which validates the ticker and
            # tells you plainly if NSE doesn't recognise it), this form used
            # to save whatever was typed with zero feedback either way. Now:
            # 1) try resolving via data.universe.resolve_ticker (handles a
            #    correctable typo / partial name), 2) probe a real live
            #    price for the resolved ticker so "saved but is it actually
            #    a tradeable NSE symbol?" gets an honest answer immediately,
            #    not a silent save followed by a blank "Live Price: —" later.
            try:
                from data.universe import resolve_ticker as _mh_resolve
                _resolved = _mh_resolve(_mh_ticker)
                if _resolved:
                    _norm_ticker = _resolved
            except Exception as _res_e:
                import logging; logging.getLogger("dashboard.my_portfolio").debug("resolve_ticker(%s) failed: %s — using raw input", _mh_ticker, _res_e)

            _mh_recognized = False
            try:
                _mh_probe = _portfolio_live_prices((_norm_ticker,))
                _mh_recognized = bool(_mh_probe.get(_norm_ticker, {}).get("price"))
            except Exception as _probe_e:
                import logging; logging.getLogger("dashboard.my_portfolio").debug("live-price probe failed for %s: %s — treating as unrecognized, not blocking save", _norm_ticker, _probe_e)
                _mh_recognized = False  # network hiccup — don't block the save on this

            _holdings = [h for h in _holdings if h["ticker"] != _norm_ticker]
            _holdings.append({
                "ticker":        _norm_ticker,
                "quantity":      float(_mh_qty),
                "avg_buy_price": float(_mh_price),
                "date_bought":   _mh_date.isoformat(),
            })
            save_manual_holdings(_holdings)
            clear_price_caches()
            # FIX MH7 — reset the lookup dropdown on the NEXT run, not now.
            st.session_state["_mh_clear_pending"] = True

            if _mh_recognized:
                st.success(
                    f"✅ Recognized **{_norm_ticker.replace('.NS','')}** on NSE — "
                    "added to your portfolio."
                )
            else:
                st.warning(
                    f"⚠️ Added **{_norm_ticker.replace('.NS','')}**, but couldn't confirm "
                    "it against live NSE prices just now. Double-check the spelling "
                    "(e.g. Vedanta's ticker is **VEDL**, not VEDANTA) — if it's wrong, "
                    "delete it below and re-add with the correct ticker, or use the "
                    "company-name lookup above instead of typing it manually."
                )
            st.success(f"Added {_norm_ticker.replace('.NS','')} to your portfolio.")
            st.rerun()

if _holdings:
    with st.expander(f"✏️ Manage holdings ({len(_holdings)})", expanded=False):
        # FIX MH5 — header row so the unlabeled input boxes below are clear
        # at a glance (previously this expander had no labels at all).
        _mhh1, _mhh2, _mhh3, _mhh4, _mhh5 = st.columns([2, 1, 1, 1.3, 0.8])
        _mhh1.markdown("**Ticker**")
        _mhh2.markdown("**Qty**")
        _mhh3.markdown("**Avg ₹**")
        _mhh4.markdown("**Date bought**")
        _mhh5.markdown("**Del**")
        st.markdown(
            '<hr style="margin:2px 0 8px 0;border-color:rgba(255,255,255,.08)">',
            unsafe_allow_html=True,
        )

        for _i, _h in enumerate(_holdings):
            _mc1, _mc2, _mc3, _mc4, _mc5 = st.columns([2, 1, 1, 1.3, 0.8])
            _mc1.markdown(f"**{_h['ticker'].replace('.NS','')}**")
            _new_qty = _mc2.number_input(
                "Qty", min_value=0.0, step=1.0,
                value=float(_h["quantity"]), key=f"mh_qty_{_i}",
                label_visibility="collapsed",
            )
            _new_price = _mc3.number_input(
                "Avg ₹", min_value=0.0, step=0.05,
                value=float(_h["avg_buy_price"]), key=f"mh_price_{_i}",
                format="%.2f", label_visibility="collapsed",
            )
            try:
                _cur_date = _dt.date.fromisoformat(str(_h.get("date_bought"))[:10])
            except Exception as _dtparse_e:
                import logging; logging.getLogger("dashboard.my_portfolio").debug("date_bought parse failed for %s: %s — defaulting to today", _h.get("ticker"), _dtparse_e)
                _cur_date = _dt.date.today()
            _new_date = _mc4.date_input(
                "Date", value=_cur_date, key=f"mh_date_{_i}",
                label_visibility="collapsed",
            )
            if _mc5.button("🗑️", key=f"mh_del_{_i}", width="stretch"):
                _holdings.pop(_i)
                save_manual_holdings(_holdings)
                clear_price_caches()
                st.rerun()
            if (_new_qty != _h["quantity"] or _new_price != _h["avg_buy_price"]
                    or _new_date.isoformat() != str(_h.get("date_bought"))[:10]):
                _h["quantity"]      = float(_new_qty)
                _h["avg_buy_price"] = float(_new_price)
                _h["date_bought"]   = _new_date.isoformat()
                save_manual_holdings(_holdings)

_csv_source = pd.DataFrame(_holdings) if _holdings else None

if _csv_source is not None:

    # ── FAST TOP-LINE (DC2) — today's/overall P&L + value only, off the ─────
    # quick 60s live-price cache. No per-stock table here anymore — that's
    # what the Decision Summary cards below are for, using PM's own
    # today_chg_pct so there's exactly one number per stock, not two.
    _live_prices: dict = {}
    _total_today_pnl = None
    try:
        _port_csv = _csv_source.copy()
        _port_tickers = tuple(
            (t if t.endswith(".NS") else t + ".NS")
            for t in _port_csv["ticker"].tolist()
        )
        # Mockup artboard 04: one KPI row. The separate live-price strip is
        # gone; today's move is computed here and shown on the NAV tile.
        _live_col, _refresh_col = st.columns([5, 1], vertical_alignment="bottom")
        with _refresh_col:
            if st.button("🔄 Refresh prices", key="port_refresh_live", width="stretch"):
                clear_price_caches()
        with _live_col:
            st.caption("Live prices refresh every 60 s; scores every 5 min.")
        _live_prices = _portfolio_live_prices(_port_tickers)
        if _live_prices:
            _total_today_pnl   = 0.0
            _total_overall_pnl = 0.0
            _total_port_value  = 0.0
            _total_invested    = 0.0
            for _row in _port_csv.itertuples():
                _sym = _row.ticker if str(_row.ticker).endswith(".NS") else f"{_row.ticker}.NS"
                _lp  = _live_prices.get(_sym, {})
                _cur = _lp.get("price")
                _qty = getattr(_row, "quantity", 1)
                _buy = getattr(_row, "avg_buy_price", 0)
                if _cur:
                    _total_today_pnl   += (_cur - _lp.get("prev", _cur)) * _qty
                    _total_overall_pnl += (_cur - _buy) * _qty
                    _total_port_value  += _cur * _qty
                    _total_invested    += _buy * _qty

        else:
            st.caption("⚠️ Live prices unavailable — trying again. Showing scored data below once ready.")
    except Exception as _e:
        st.caption(f"Live price strip skipped: {_e}")

    st.markdown("---")
    # FIX MH4 — PortfolioManager always expected a real CSV file path
    # (it does Path(csv_path) internally), never a DataFrame. Write the
    # in-memory holdings to a temp file and point PortfolioManager at that,
    # cleaning the temp file up afterwards regardless of success/failure.
    _pm_tmp_csv_path = None
    with st.spinner("Scoring your portfolio (parallel)… ~10–20 s for 5–10 stocks"):
        try:
            from analysis.portfolio_manager import PortfolioManager
            _pm_tmp_csv_path = _holdings_csv_tmpfile(_csv_source)
            pm = PortfolioManager(_pm_tmp_csv_path)
            summary = pm.mark_to_market(parallel=True)

            pnl_sign  = "+" if summary.total_pnl >= 0 else ""
            pnl_color = "var(--bull)" if summary.total_pnl >= 0 else "var(--bear)"

            # ── UI/UX 2026-09 · editorial KPI-tile row (mockup Portfolio row) ──
            # Replaces the flat st.metric row with the mockup's 5-KPI ceiling
            # design: first tile carries the saffron accent gradient (NAV is
            # the number that matters most), rest are neutral. Semantic delta
            # colours preserved for P&L per the trading-dashboard-design skill.
            _pn_sign = pnl_sign  # "+" or ""
            _pn_col  = "var(--bull)" if summary.total_pnl >= 0 else "var(--bear)"
            _grade_letter = summary.portfolio_grade
            _grade_col = {
                "A": "var(--bull)", "B": "var(--bull)", "C": "var(--amber)",
                "D": "var(--bear)", "F": "var(--bear)",
            }.get(str(_grade_letter)[:1] if _grade_letter else "", "var(--dim)")
            _vix_reg = str(summary.vix_regime or "normal")
            _vix_col = {
                "panic":       "var(--bear)",
                "fear":        "var(--bear)",
                "elevated":    "var(--amber)",
                "normal":      "var(--bull)",
                "complacency": "var(--amber)",
            }.get(_vix_reg, "var(--dim)")
            _tile_common = (
                "background:var(--surface);border:1px solid var(--hairline);"
                "border-radius:var(--r-sharp);padding:14px 16px"
            )
            _tile_hero = (
                "background:linear-gradient(180deg,"
                "color-mix(in srgb,var(--accent) 8%,var(--surface)),"
                "var(--surface) 80%);"
                "border:1px solid color-mix(in srgb,var(--accent) 30%,var(--hairline));"
                "border-radius:var(--r-sharp);padding:14px 16px"
            )
            _k_lbl = ("font-size:10px;text-transform:uppercase;"
                      "letter-spacing:.1em;color:var(--dim);font-weight:600")
            _k_v   = ("font-family:var(--font-mono);font-size:22px;font-weight:500;"
                      "margin-top:4px;letter-spacing:-.01em;color:var(--ink)")
            _k_ccy = ("font-size:14px;color:var(--dim);font-weight:400;margin-right:3px")
            _k_d   = "margin-top:3px;font-family:var(--font-mono);font-size:12px"
            _k_txt = ("font-family:'Inter',sans-serif;font-size:14px;"
                      "font-weight:500;margin-top:4px;letter-spacing:-.005em")
            if _total_today_pnl is not None:
                _nav_up = _total_today_pnl >= 0
                _nav_delta = (
                    f'<span style="color:{"var(--bull)" if _nav_up else "var(--bear)"}">'
                    f'{"▲" if _nav_up else "▼"} ₹{_fmt_inr(abs(_total_today_pnl), 0)}</span>'
                    '<span style="color:var(--dim)"> today</span>')
            else:
                _nav_delta = f'<span style="color:{_pn_col}">{_pn_sign}₹{_fmt_inr(summary.total_pnl, 0)}</span>'
            st.markdown(
                f'<div class="mobile-stack" style="display:grid;grid-template-columns:repeat(5,1fr);'
                f'gap:10px;margin:8px 0 14px 0">'
                # NAV -- hero tile
                f'<div style="{_tile_hero}">'
                f'<div style="{_k_lbl}">NAV</div>'
                f'<div style="{_k_v}"><span style="{_k_ccy}">₹</span>'
                f'{_fmt_inr(summary.total_current_value, 0)}</div>'
                f'<div style="{_k_d}">{_nav_delta}</div>'
                f'</div>'
                # Total return
                f'<div style="{_tile_common}">'
                f'<div style="{_k_lbl}">Total return</div>'
                f'<div style="{_k_v};color:{_pn_col}">'
                f'{_pn_sign}{summary.total_pnl_pct:.1f}%</div>'
                f'<div style="{_k_d};color:var(--dim)">{_pn_sign}₹{_fmt_inr(summary.total_pnl, 0)} vs cost</div>'
                f'</div>'
                # Health score
                f'<div style="{_tile_common}">'
                f'<div style="{_k_lbl}">Health score</div>'
                f'<div style="{_k_v}">{summary.portfolio_score:.0f}<span style="{_k_ccy};font-size:14px"> /90</span></div>'
                f'<div style="{_k_d};color:{_grade_col}">Grade {_grade_letter}</div>'
                f'</div>'
                # Diversification
                f'<div style="{_tile_common}">'
                f'<div style="{_k_lbl}">Diversification</div>'
                f'<div style="{_k_txt}">{summary.diversification.concentration_risk}</div>'
                f'<div style="{_k_d};color:var(--dim)">concentration</div>'
                f'</div>'
                # VIX regime
                f'<div style="{_tile_common}">'
                f'<div style="{_k_lbl}">VIX regime</div>'
                f'<div style="{_k_txt};color:{_vix_col}">'
                f'{_vix_reg.title()}</div>'
                f'<div style="{_k_d};color:var(--dim)">market conditions</div>'
                f'</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

            # ── Portfolio posture strip (mockup artboard 04) ─────────────
            # Spread of holdings across trend postures, by market value,
            # plus the existing summary narrative. Holdings carry no pillar
            # sub-scores, so none are shown (no invented numbers).
            _PV_ORDER = ["STRONG BUY", "BUY", "WATCHLIST", "HOLD", "CAUTION", "EXIT"]
            _PV_FILL = {"STRONG BUY": "var(--bull)", "BUY": "var(--bull)",
                        "WATCHLIST": "var(--accent)", "HOLD": "var(--dim)",
                        "CAUTION": "var(--amber)", "EXIT": "var(--bear)"}
            _pv_hold = list(summary.holdings)
            _pv_tot = sum(max(h.current_value, 0) for h in _pv_hold) or 1.0
            _pv_rows = []
            for _pa in _PV_ORDER + sorted({h.action for h in _pv_hold} - set(_PV_ORDER)):
                _phs = [h for h in _pv_hold if h.action == _pa]
                if not _phs:
                    continue
                _pvv = sum(max(h.current_value, 0) for h in _phs) / _pv_tot * 100
                _pv_rows.append((_html.escape(_display_label(_pa)), _pvv,
                                 f"{len(_phs)} · {_pvv:.0f}%", _PV_FILL.get(_pa, "var(--dim)")))
            _pv_tone = {"A": "good", "B": "good", "C": "warn", "D": "bad", "F": "bad"}.get(
                str(_grade_letter or "")[:1], "neutral")
            st.markdown(
                f'<div class="kit-panel"><div class="kit-panel-hd">'
                f'<span class="kit-panel-t">Portfolio posture &nbsp;'
                f'{chip_pill("Grade " + _html.escape(str(_grade_letter)), tone=_pv_tone)}</span>'
                f'<span class="kit-panel-s">{summary.portfolio_score:.0f}/90 value-weighted · '
                f'{len(_pv_hold)} holdings · share of value by posture</span></div>'
                + share_bars(_pv_rows)
                + f'<div class="kit-panel-note">{_html.escape(summary.summary_narrative or "")}</div></div>',
                unsafe_allow_html=True,
            )

            # ── Auto-Signal Monitor — signal changes right above the decision cards ─
            _PF_BUY  = {"STRONG BUY", "BUY"}
            _PF_SELL = {"CAUTION", "EXIT", "SELL", "REDUCE"}
            _pf_cur  = {h.ticker: h.action for h in summary.holdings}
            _pf_prev = load_signal_monitor_state()
            _pf_flips = []
            for _tk, _ac in _pf_cur.items():
                _pv = _pf_prev.get(_tk)
                if _pv and _pv != _ac and (_ac in _PF_BUY or _ac in _PF_SELL):
                    _pf_flips.append((_tk.replace(".NS", ""), _pv, _ac,
                                      "buy" if _ac in _PF_BUY else "sell"))
            save_signal_monitor_state(_pf_cur)

            _sg1, _sg2 = st.columns([5, 2])
            _sg1.markdown(section_header("Holdings", "same scoring pass as the tiles above"),
                          unsafe_allow_html=True)
            _pf_auto = _sg2.toggle("Auto-refresh (5 min)", key="pf_auto_signal")

            _pf_buys  = [h for h in summary.holdings if h.action in _PF_BUY]
            _pf_sells = [h for h in summary.holdings if h.action in _PF_SELL]

            if _pf_flips:
                _fl_rows = "".join(
                    f'<div style="font-size:12.5px;color:var(--ink);margin:2px 0">'
                    f'{"🟢" if _d == "buy" else "🔴"} <b>{_t}</b> '
                    f'<span style="color:var(--dim)">{_display_label(_p)}</span> → '
                    f'<b style="color:{"var(--bull)" if _d == "buy" else "var(--bear)"}">{_display_label(_a)}</b></div>'
                    for _t, _p, _a, _d in _pf_flips)
                st.markdown(
                    f'<div style="background:var(--tint-amber);'
                    f'border-left:4px solid var(--amber);border-radius:10px;padding:10px 14px;'
                    f'margin:4px 0 8px">'
                    f'<div style="font-size:12px;font-weight:700;color:var(--amber);margin-bottom:3px">'
                    f'⚡ {len(_pf_flips)} signal change(s) since your last check</div>'
                    f'{_fl_rows}</div>', unsafe_allow_html=True)
                for _t, _p, _a, _d in _pf_flips:
                    st.toast(
                        f'{"🟢" if _d == "buy" else "🔴"} {_t}: '
                        f'{_display_label(_p)} → {_display_label(_a)}',
                        icon="⚡",
                    )

            st.caption(
                f"📡 **{len(_pf_buys)}** holding(s) in an **Uptrend / Strong Trend**, "
                f"**{len(_pf_sells)}** showing **Weakening / Broken Trend** right now — see the cards below. "
                "Trend-quality scores only; the app never auto-executes real trades. "
                "Toggle **Auto-refresh** to keep this live while the page is open.")

            if _pf_auto:
                @st.fragment(run_every="300s")
                def _pf_signal_tick():
                    st.rerun()
                _pf_signal_tick()

            # ── DC1/DC2/DC3 — THE DECISION: holdings cards, moved up front, ──
            # merged with per-stock P&L (no separate table anymore), each
            # card now also shows the risk-reward ratio and a "Why?"
            # expander with the full narrative already computed by
            # score_stock() — sector rank, VIX context, pattern detection,
            # entry/SL/TP reasoning — instead of throwing it away.
            _hh1, _hh2 = st.columns([3, 2])
            _hh1.caption("Sort order applies to the table and the holding picker.")
            with _hh2:
                _h_sort = st.selectbox(
                    "Sort by",
                    ["Action (buy→exit)", "Total P&L (high→low)", "Total P&L (low→high)",
                     "Today's change", "Score (best first)", "Value (high→low)"],
                    key="pf_holdings_sort", label_visibility="collapsed",
                )
            _ACT_ORDER = {"STRONG BUY": 0, "BUY": 1, "WATCHLIST": 2, "HOLD": 3,
                          "CAUTION": 4, "EXIT": 5}
            _hold_sorted = list(summary.holdings)
            try:
                if _h_sort == "Action (buy→exit)":
                    _hold_sorted.sort(key=lambda h: _ACT_ORDER.get(h.action, 9))
                elif _h_sort == "Total P&L (high→low)":
                    _hold_sorted.sort(key=lambda h: -h.pnl)
                elif _h_sort == "Total P&L (low→high)":
                    _hold_sorted.sort(key=lambda h: h.pnl)
                elif _h_sort == "Today's change":
                    _hold_sorted.sort(
                        key=lambda h: -getattr(h, "today_chg_pct", getattr(h, "pnl_pct", 0))
                    )
                elif _h_sort == "Score (best first)":
                    _hold_sorted.sort(key=lambda h: -getattr(h, "score", 0))
                elif _h_sort == "Value (high→low)":
                    _hold_sorted.sort(key=lambda h: -(h.current_price * h.quantity))
            except Exception as _sort_e:
                import logging; logging.getLogger("dashboard.my_portfolio").debug("Holdings sort failed (%s): %s — using default order", _h_sort, _sort_e)
                _hold_sorted = list(summary.holdings)

            # FIX MH7 — bring back the missing per-stock table. DC2 (see
            # this file's header comment) removed the old live-price table
            # entirely on the reasoning that it duplicated the cards below
            # and the two could silently drift apart (different code paths
            # for "today's change"). That's still true, so this table is
            # built from the EXACT SAME _hold_sorted / HoldingResult objects
            # already computed for the cards — not a second live-price
            # fetch — so it's mechanically the same numbers, just also
            # available at a glance across every holding at once, which the
            # cards' one-stock-at-a-time layout doesn't give you.
            _tbl_col, _alloc_col = st.columns([1.7, 1], gap="medium")
            with _alloc_col:
                # Sector allocation bars replace the pie that sat in
                # "Deeper Analysis" (mockup artboard 04, right column).
                _dv = summary.diversification
                if _dv.sector_weights:
                    _al_rows = [(_html.escape(str(_sn)), _sw, f"{_sw:.1f}%")
                                for _sn, _sw in sorted(_dv.sector_weights.items(), key=lambda kv: -kv[1])]
                    st.markdown(
                        f'<div class="kit-panel"><div class="kit-panel-hd">'
                        f'<span class="kit-panel-t">Sector allocation</span>'
                        f'<span class="kit-panel-s">by market value · {_dv.n_sectors} sectors</span></div>'
                        + share_bars(_al_rows)
                        + f'<div class="kit-panel-note">Concentration '
                          f'<b>{_html.escape(str(_dv.concentration_risk).lower())}</b>. '
                          f'{_html.escape(_dv.advice or "")}</div></div>',
                        unsafe_allow_html=True,
                    )
                else:
                    st.caption("Sector allocation needs priced holdings.")
            with _tbl_col:
                _tbl_rows = []
                for _th in _hold_sorted:
                    _th_today = getattr(_th, "today_chg_pct", None)
                    _th_today_pnl = (
                        _th.current_price * _th.quantity * (_th_today / 100.0)
                        if _th_today is not None else None
                    )
                    _tbl_rows.append({
                        "Ticker": _th.ticker.replace(".NS", ""),
                        "Price (₹)": _th.current_price,
                        "Today %": _th_today,
                        "Today ₹": _th_today_pnl,
                        "Qty": _th.quantity,
                        "Invested (₹)": _th.avg_buy_price * _th.quantity,
                        "Value (₹)": _th.current_price * _th.quantity,
                        "P&L (₹)": _th.pnl,
                        "P&L %": _th.pnl_pct,
                        "Posture": _display_label(_th.action),
                        "Score": _th.score,
                    })
                # P2 · P&L table pattern — rows tinted by P&L %, ▲/▼ + sign
                # colour on the signed columns (bold on big moves), Indian digit
                # grouping on ₹ columns, Ticker pinned while scrolling sideways.
                from dashboard.shared.table_styles import (
                    arrow_fmt as _ts_arrow, pnl_styler as _ts_pnl,
                    pinned_text_col as _ts_pin,
                )
                from dashboard.shared.ui_components import fmt_inr as _ts_inr
                _inr0 = lambda v: "—" if pd.isna(v) else f"₹{_ts_inr(v, 0)}"
                _tbl_df = pd.DataFrame(_tbl_rows)
                st.dataframe(
                    _ts_pnl(
                        _tbl_df,
                        tint_col="P&L %",
                        signed_cols=["Today %", "Today ₹", "P&L (₹)", "P&L %"],
                        bold_at={"Today %": 2.0, "P&L %": 10.0},
                        formats={
                            "Price (₹)":    lambda v: "—" if pd.isna(v) else f"₹{_ts_inr(v, 2)}",
                            "Today %":      _ts_arrow(2, "%"),
                            "Today ₹":      _ts_arrow(0, "", prefix="₹", indian=True),
                            "Qty":          "{:.0f}",
                            "Invested (₹)": _inr0,
                            "Value (₹)":    _inr0,
                            "P&L (₹)":      _ts_arrow(0, "", prefix="₹", indian=True),
                            "P&L %":        _ts_arrow(2, "%"),
                            "Score":        "{:.0f}/90",
                        },
                    ),
                    width="stretch",
                    hide_index=True,
                    column_config={"Ticker": _ts_pin("Ticker")},
                )
                st.caption(
                    "Same numbers as the cards below, sourced from the same "
                    "scoring pass — just laid out for a quick scan across every "
                    "holding at once."
                )
                # DT1 + DT2 · attribution on the holdings table. Prices flow
                # through data/fetcher.py's tiered pipeline (Angel One → Stooq
                # → Yahoo); scores come out of PortfolioManager's per-run pass.
                try:
                    from dashboard.shared.ui_components import data_as_of as _mp_asof
                    _mp_when = _dt.datetime.now().strftime("%H:%M IST")
                    st.markdown(
                        _mp_asof(_mp_when, source="yfinance",
                                 ttl_hint="live-price cache 60 s · score cache 5 min"),
                        unsafe_allow_html=True,
                    )
                except Exception:
                    pass

            # Aligned to design.py's "Dealing Room v2" tokens (bull #6faa84 /
            # bear #c77070 / caution #d6a64c / accent #c88a3f) instead of the
            # pre-redesign teal/material-green/blue set, so this page matches
            # the rest of the app now.
            # F1 audit -- routed through design.py tokens. The (rail, tint)
            # pairs source from --bull/--bear/--amber/--accent + their
            # matching --tint-* backgrounds so a palette swap flows through
            # in one place.
            _ACT_CARD_STYLE = {
                "STRONG BUY": ("var(--bull)",   "var(--tint-bull)"),
                "BUY":        ("var(--bull)",   "var(--tint-bull)"),
                "WATCHLIST":  ("var(--accent)", "var(--tint-accent)"),
                "HOLD":       ("var(--dim)",    "rgba(255,255,255,.04)"),
                "CAUTION":    ("var(--amber)",  "var(--tint-amber)"),
                "EXIT":       ("var(--bear)",   "var(--tint-bear)"),
            }
            # Chip-vocabulary tone map -- keeps the action pill on holding
            # cards in the same shape/colours the rest of the app uses.
            _ACT_CHIP_TONE = {
                "STRONG BUY": "good", "BUY": "good",
                "WATCHLIST":  "accent",
                "HOLD":       "neutral",
                "CAUTION":    "warn",
                "EXIT":       "bad",
            }
            # UX3 · live-tick pulse on holdings cards. Fires a one-shot halo
            # when a holding's current_price ticks between reruns. Scoring is
            # cached ~5 min so most reruns will see identical prices and no
            # pulse — that's correct (nothing ticked). commit() at loop end
            # prunes any tickers removed from the manual holdings list.
            _pf_pulse, _pf_pulse_commit = tick_pulse_tracker("_pf_holdings_prev")
            # Mockup pattern shared with the scanner pages: the table is the
            # overview; one selected holding gets the full card + actions
            # (was a two-column wall of every card).
            _pf_sel = (st.selectbox(
                "Holding detail & actions", options=list(range(len(_hold_sorted))),
                format_func=lambda i: (f"{_hold_sorted[i].ticker.replace('.NS', '')} · "
                                       f"{_display_label(_hold_sorted[i].action)} · "
                                       f"{_hold_sorted[i].pnl_pct:+.1f}%"),
                key="pf_detail") if _hold_sorted else None)
            _hc_grid = st.columns([1.3, 1])
            for _hi, h in ([(0, _hold_sorted[_pf_sel])] if _pf_sel is not None else []):
                _h_ac, _h_bg = _ACT_CARD_STYLE.get(
                    h.action, ("var(--dim)", "var(--sunken)"),
                )
                _h_emoji  = _action_emoji(h.action)
                _h_pnl_c  = "var(--bull)" if h.pnl >= 0 else "var(--bear)"
                _h_pnl_a  = "▲" if h.pnl >= 0 else "▼"
                _h_lbl    = h.ticker.replace(".NS", "")
                _h_inv    = h.avg_buy_price * h.quantity
                _h_val    = h.current_price * h.quantity
                _h_sl     = h.stop_loss or (h.avg_buy_price * 0.95)
                _h_tp     = h.target    or (h.avg_buy_price * 1.10)
                _h_rng    = max(_h_tp - _h_sl, 0.01)
                _h_cur_pct = min(100, max(0, (h.current_price - _h_sl) / _h_rng * 100))
                _h_bar_c  = "var(--bull)" if h.current_price >= h.avg_buy_price else "var(--bear)"
                _h_score_w = min(int(round(h.score * 100 / 90)), 100)  # composite is 0–90 → bar %
                _h_today  = getattr(h, "today_chg_pct", None)
                _h_today_c = "var(--bull)" if (_h_today or 0) >= 0 else "var(--bear)"
                # PGF (Portfolio Gap Fix) — the removed live-price table used to be
                # the only place showing today's ₹ P&L per stock; the cards never
                # picked that up when the table was dropped. Derived here from
                # fields HoldingResult already has (current_price, quantity,
                # today_chg_pct) — no extra fetch, same number the top-line
                # "Today's Change" box would show if summed across holdings.
                _h_today_pnl = (
                    h.current_price * h.quantity * (_h_today / 100.0)
                    if _h_today is not None else None
                )
                _h_today_txt = (
                    f"{_h_today:+.2f}% · ₹{_h_today_pnl:+,.0f} today"
                    if _h_today is not None else ""
                )
                # DC3 — risk-reward badge, already computed, just unused before
                _h_rr = getattr(h, "risk_reward", None)
                _h_rr_txt = f"RR {_h_rr:.1f}:1" if _h_rr else ""

                # Phase 2 -- honest display label, not raw action string.
                # Action badge routed through the shared chip_pill so every
                # posture across the app uses one vocabulary.
                _h_chip_tone = _ACT_CHIP_TONE.get(h.action, "neutral")
                _h_action_chip = chip_pill(
                    f"{_h_emoji} {_display_label(h.action)}", tone=_h_chip_tone,
                )
                _h_tick_cls = _pf_pulse(h.ticker, h.current_price)
                # UX2 · hover preview on the holding label — reuses fields the
                # HoldingResult already carries (no extra fetch per card).
                _h_lbl_hover = ticker_hover_wrap(
                    _h_lbl,
                    price=h.current_price,
                    chg_pct=_h_today,
                    score=h.score,
                    sector=str(getattr(h, "sector", "") or ""),
                )
                _h_html = (
                    f'<div class="holding-card{_h_tick_cls}" '
                    f'style="background:{_h_bg};border-left:5px solid {_h_ac};'
                    f'border-radius:10px;padding:14px 16px;margin-bottom:8px">'
                    f'<div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:6px">'
                    f'<div>'
                    f'<span style="font-size:20px;font-weight:700;color:var(--ink)">{_h_lbl_hover}</span>'
                    f'&nbsp;&nbsp;{_h_action_chip}'
                    f'</div>'
                    f'<div style="text-align:right">'
                    f'<span style="font-size:13px;font-weight:700;color:{_h_ac}">{h.score:.0f}/90</span>'
                    f'<div style="width:60px;height:5px;background:var(--hairline);border-radius:3px;margin-top:3px">'
                    f'<div style="width:{_h_score_w}%;height:100%;background:{_h_ac};border-radius:3px"></div>'
                    f'</div></div></div>'
                    f'<div style="font-size:15px;color:var(--ink);margin-bottom:4px">'
                    f'<b>₹{h.current_price:,.2f}</b>'
                    f'<span style="font-size:12px;color:{_h_today_c};margin-left:8px;font-weight:600">{_h_today_txt}</span>'
                    f'<span style="font-size:12px;color:var(--ink-mid);margin-left:8px">'
                    f'{h.quantity:.0f} shares · held {h.days_held}d</span>'
                    f'</div>'
                    f'<div style="font-size:12px;color:var(--ink-mid);margin-bottom:6px">'
                    f'Invested ₹{_h_inv:,.0f} → Now ₹{_h_val:,.0f}'
                    f'{"  ·  " + _h_rr_txt if _h_rr_txt else ""}'
                    f'</div>'
                    f'<div style="font-size:18px;font-weight:700;color:{_h_pnl_c};margin-bottom:8px">'
                    f'{_h_pnl_a} ₹{abs(h.pnl):,.0f} ({h.pnl_pct:+.1f}%)'
                    f'</div>'
                    f'<div style="margin-bottom:6px">'
                    f'<div style="display:flex;justify-content:space-between;font-size:10px;color:var(--faint);margin-bottom:2px">'
                    f'<span>SL ₹{_h_sl:,.0f}</span><span>Target ₹{_h_tp:,.0f}</span></div>'
                    f'<div style="width:100%;height:6px;background:var(--hairline);border-radius:3px;position:relative">'
                    f'<div style="position:absolute;left:0;width:{_h_cur_pct:.0f}%;height:100%;'
                    f'background:{_h_bar_c};border-radius:3px;opacity:0.7"></div>'
                    f'<div style="position:absolute;left:{_h_cur_pct:.0f}%;transform:translateX(-50%);'
                    f'top:-4px;width:14px;height:14px;background:{_h_bar_c};border-radius:50%;'
                    f'border:2px solid var(--ink)"></div>'
                    f'</div></div>'
                    f'<div style="font-size:12px;color:var(--ink-mid);margin-top:6px">{h.headline}</div>'
                    f'</div>'
                )
                with _hc_grid[_hi % 2]:
                    st.markdown(_h_html, unsafe_allow_html=True)
                    _hb1, _hb2, _hb3 = st.columns(3)
                    with _hb1:
                        if st.button(f"📊 Analyze", key=f"ph_an_{h.ticker}",
                                     width="stretch"):
                            # FIX MH3
                            st.session_state["analyze_ticker"] = h.ticker
                            st.session_state["_goto_page"]     = "🔍 Analyze Stock"
                            st.rerun()
                    with _hb2:
                        _ph_price = h.current_price or h.avg_buy_price
                        _paper_trade_popover(
                            h.ticker, _ph_price, h.stop_loss or _ph_price * 0.95, h.target,
                            reason=f"{_display_label(h.action)}: {h.headline}",
                            key=f"ph_pt_{h.ticker}",
                        )
                    with _hb3:
                        # DC3 — the full narrative was always computed, just
                        # never shown. One click, no extra fetch.
                        with st.popover("❓ Why?", width="stretch"):
                            st.caption(h.narrative or h.headline)
                    if h.error:
                        st.caption(f"⚠️ {h.error}")
            _pf_pulse_commit()

            # ══════════════════════════════════════════════════════════════
            # DC1 — DEEPER ANALYSIS (optional), everything below unchanged
            # internally, just moved beneath the decision cards.
            # ══════════════════════════════════════════════════════════════
            st.markdown("---")
            st.markdown("## 📊 Deeper Analysis (optional)")

            # Sector breakdown now lives beside the holdings table (mockup artboard 04).

            # ── Portfolio Heatmap (moved here from the old top-of-page ──
            # live-price strip — this is deeper analysis, not a decision).
            _hm_rows = []
            for _row in _port_csv.itertuples():
                _sym = _row.ticker if str(_row.ticker).endswith(".NS") else f"{_row.ticker}.NS"
                _lp  = _live_prices.get(_sym, {}) if _live_prices else {}
                _cur = _lp.get("price")
                _buy = getattr(_row, "avg_buy_price", 0)
                _qty = getattr(_row, "quantity", 1)
                if _cur and _buy and _buy > 0:
                    _pct = (_cur / _buy - 1) * 100
                    _val = _cur * _qty
                    _hm_rows.append({
                        "label": _row.ticker, "value": _val,
                        "pct": round(_pct, 2),
                        "text": f"{_row.ticker}<br>{_pct:+.1f}%<br>₹{_val/1000:.0f}K",
                    })
            if _hm_rows:
                with st.expander(
                    "📊 Portfolio Heatmap — sized by value, coloured by P&L", expanded=False
                ):
                    _hm_df = pd.DataFrame(_hm_rows)
                    import plotly.express as _px2
                    # F1 audit exception: Plotly's color_continuous_scale
                    # requires real colour values (it doesn't parse CSS
                    # custom properties), so read them from tokens.
                    _fig_hm = _px2.treemap(
                        _hm_df, path=["label"], values="value", color="pct",
                        color_continuous_scale=[_TK["bear"], _TK["faint"], _TK["bull"]],
                        color_continuous_midpoint=0, custom_data=["pct", "text"],
                    )
                    _fig_hm.update_traces(
                        texttemplate="%{customdata[1]}", textfont_size=13,
                        hovertemplate="<b>%{label}</b><br>P&L: %{customdata[0]:+.1f}%<extra></extra>",
                    )
                    _fig_hm.update_layout(
                        template="nse_pro", height=300,
                        margin=dict(l=0, r=0, t=10, b=0),
                        coloraxis_showscale=False,
                    )
                    st.plotly_chart(_fig_hm, width="stretch")

            # ── Portfolio Risk & Performance ───────────────────────────
            st.markdown("---")
            _rh1, _rh2 = st.columns([5, 2])
            # §9.4 typography scale · Streamlit's own st.subheader renders
            # its own h3 that doesn't match the anchor scale used on
            # Command Centre.
            _rh1.markdown(
                '<div class="t-h1" style="margin:6px 0 4px 0">'
                '📉 Portfolio Risk &amp; Performance</div>',
                unsafe_allow_html=True,
            )
            with _rh2:
                _risk_period = st.selectbox(
                    "Lookback", ["6mo", "1y", "2y", "3y"], index=1,
                    key="pf_risk_period", label_visibility="collapsed")

            _db_map = {}
            for _hr in getattr(pm, "holdings_raw", []) or []:
                _rt = str(_hr.get("ticker", "")).strip().upper()
                if _rt and not _rt.endswith(".NS"):
                    _rt += ".NS"
                if _rt:
                    _db_map[_rt] = _hr.get("date_bought")
            _risk_holds = tuple(
                (h.ticker, float(getattr(h, "quantity", 0) or 0),
                 _db_map.get(str(h.ticker).upper()))
                for h in summary.holdings if getattr(h, "quantity", 0))

            @st.cache_data(ttl=900, show_spinner=False)
            def _pf_risk(_holds, _period):
                import pandas as _pd
                from analysis.portfolio_risk import compute_portfolio_risk
                from data.fetcher import fetch_single as _fs

                def _tz_safe_loader(_tkr, period="1y"):
                    _df = _fs(_tkr, period=period)
                    try:
                        if _df is not None and not getattr(_df, "empty", True):
                            _ix = _df.index
                            if isinstance(_ix, _pd.DatetimeIndex) and _ix.tz is not None:
                                _df = _df.copy()
                                _df.index = _ix.tz_localize(None)
                    except Exception as _tz_e:
                        import logging; logging.getLogger("dashboard.my_portfolio").debug("tz_localize skipped for %s: %s", _tkr, _tz_e)
                    return _df

                return compute_portfolio_risk(
                    [{"ticker": t, "quantity": q, "date_bought": db}
                     for t, q, db in _holds], period=_period, price_loader=_tz_safe_loader)

            # IR3 (docs/UI_UX_BACKLOG.md) -- perf & risk tiles used to render
            # as raw st.metric cells; replaced with the shared stat() helper
            # so they read as anchor artifacts and pick up semantic tone
            # from meaningful thresholds (Sharpe > 1 = bull, Max DD < -30%
            # = bear, etc.) rather than a uniform grey wall.
            def _perf_tone(name: str, val):
                """Semantic tone per metric based on published thresholds."""
                if val is None:
                    return "neutral"
                n = name.lower()
                if "sharpe" in n:   # >1 great · 0.5-1 mid · <0.5 poor
                    return "bull" if val >= 1.0 else "amber" if val >= 0.5 else "bear"
                if "sortino" in n:  # tighter: downside-only variance
                    return "bull" if val >= 1.5 else "amber" if val >= 0.7 else "bear"
                if "calmar" in n:   # CAGR / max-DD -- >0.5 is very good
                    return "bull" if val >= 0.5 else "amber" if val >= 0.2 else "bear"
                if "max drawdown" in n:  # value is a NEGATIVE %; less-negative is better
                    return "bear" if val <= -30 else "amber" if val <= -15 else "bull"
                if "cagr" in n or "total return" in n:  # roughly index-relative
                    return "bull" if val >= 12 else "amber" if val >= 4 else "bear"
                if "beta" in n:  # <1 below-market · 1-1.5 mid · >1.5 high
                    return "bull" if val <= 1.0 else "amber" if val <= 1.5 else "bear"
                if "vol" in n:   # <15% low · 15-25% mid · >25% high
                    return "bull" if val <= 15 else "amber" if val <= 25 else "bear"
                return "neutral"

            def _fmt_metric(val, unit: str) -> str:
                if val is None:
                    return "—"
                if unit == "%":
                    return f"{val:+.1f}%" if abs(val) < 1000 else f"{val:.0f}%"
                return f"{val:.2f}"

            if not _risk_holds:
                st.caption("No holdings with quantity to analyze.")
            else:
                with st.spinner("Reconstructing NAV & computing risk metrics…"):
                    _rr = _pf_risk(_risk_holds, _risk_period)
                if _rr.error:
                    # DT3 degraded-mode banner -- consistent look across the app.
                    from dashboard.shared.ui_components import degraded_banner as _degraded
                    st.markdown(
                        _degraded(
                            title="Risk analytics unavailable",
                            detail=str(_rr.error),
                            fallback="The rest of the portfolio view still works below.",
                            tone="bad",
                        ),
                        unsafe_allow_html=True,
                    )
                else:
                    if (_rr.affected_weight_pct or 0) >= 25 or not _rr.purchase_dates_known:
                        st.warning(f"⚠️ {_rr.disclosure}")
                    else:
                        st.info(f"ℹ️ {_rr.disclosure}")
                    st.caption(f"Confidence: **{_rr.confidence}** — {_rr.confidence_reason}")

                    from dashboard.shared.ui_components import (
                        panel as _panel, stat as _stat,
                    )

                    # ── Performance tiles (IR3) ────────────────────────────
                    st.markdown(
                        '<div class="t-h2" style="margin:14px 0 6px 0">'
                        '📈 Hypothetical Performance '
                        '<span style="color:var(--dim);font-weight:400;font-size:12px">'
                        'if you\'d held today\'s exact book</span></div>',
                        unsafe_allow_html=True,
                    )
                    _perf = _rr.performance_metrics()
                    _perf_body = (
                        '<div style="display:grid;'
                        'grid-template-columns:repeat(auto-fit,minmax(140px,1fr));'
                        'gap:14px 22px">'
                        + "".join(
                            _stat(_l, _fmt_metric(_v, _u),
                                  tone=_perf_tone(_l, _v), align="center")
                            for (_l, _v, _u) in _perf
                        )
                        + '</div>'
                    )
                    st.markdown(
                        _panel(_perf_body, kind="glass", tone="neutral",
                               margin="0 0 12px 0"),
                        unsafe_allow_html=True,
                    )

                    # ── NAV curve (IR3) with peak/trough drawdown markers ──
                    if _rr.nav_curve is not None:
                        _nav_df = _rr.nav_curve.rename("NAV").reset_index()
                        _nav_df.columns = ["Date", "NAV"]
                        import plotly.graph_objects as _go
                        _fig_nav = _go.Figure()
                        # Bull-tinted fill under the NAV line -- muted, so
                        # attention lands on the line and drawdown region.
                        _fig_nav.add_trace(_go.Scatter(
                            x=_nav_df["Date"], y=_nav_df["NAV"],
                            mode="lines", name="NAV",
                            line=dict(color=_TK["accent"], width=2),
                            fill="tozeroy",
                            fillcolor="rgba(200,138,63,0.06)",
                            hovertemplate="%{x|%d %b %Y}<br>NAV Rs.%{y:,.0f}<extra></extra>",
                        ))
                        # Peak → trough drawdown band -- ties the Max Drawdown
                        # tile above to the visible worst-case period.
                        if (_rr.max_dd_peak and _rr.max_dd_trough
                                and _rr.max_dd_peak != _rr.max_dd_trough):
                            _fig_nav.add_vrect(
                                x0=_rr.max_dd_peak, x1=_rr.max_dd_trough,
                                fillcolor="rgba(199,112,112,0.10)",
                                line_width=0,
                                annotation_text=(
                                    f"Max DD "
                                    f"{(_rr.max_drawdown_pct or 0):.1f}%"
                                ),
                                annotation_position="top left",
                                annotation_font=dict(
                                    color=_TK["bear"], size=11,
                                    family="JetBrains Mono",
                                ),
                            )
                            _fig_nav.add_trace(_go.Scatter(
                                x=[_rr.max_dd_peak, _rr.max_dd_trough],
                                y=[_nav_df.loc[_nav_df["Date"].astype("datetime64[ns]")
                                               == pd.Timestamp(_rr.max_dd_peak),
                                               "NAV"].iloc[0]
                                   if not _nav_df[_nav_df["Date"].astype("datetime64[ns]")
                                                  == pd.Timestamp(_rr.max_dd_peak)].empty
                                   else _nav_df["NAV"].max(),
                                   _nav_df.loc[_nav_df["Date"].astype("datetime64[ns]")
                                               == pd.Timestamp(_rr.max_dd_trough),
                                               "NAV"].iloc[0]
                                   if not _nav_df[_nav_df["Date"].astype("datetime64[ns]")
                                                  == pd.Timestamp(_rr.max_dd_trough)].empty
                                   else _nav_df["NAV"].min()],
                                mode="markers",
                                marker=dict(size=[8, 10],
                                            color=[_TK["bull"], _TK["bear"]],
                                            line=dict(color=_TK["ground"], width=1.5)),
                                hovertemplate="%{x|%d %b %Y}<br>Rs.%{y:,.0f}<extra></extra>",
                                showlegend=False,
                            ))
                        _fig_nav.update_layout(
                            template="nse_pro",
                            height=320,
                            margin=dict(l=6, r=6, t=8, b=0),
                            showlegend=False,
                            hovermode="x unified",
                        )
                        _fig_nav.update_yaxes(
                            tickprefix="₹", tickformat=",.0f",
                            gridcolor="rgba(255,255,255,0.04)",
                        )
                        _fig_nav.update_xaxes(
                            gridcolor="rgba(255,255,255,0.04)",
                        )
                        st.plotly_chart(_fig_nav, width="stretch",
                                        config={"displayModeBar": False})

                    # ── Risk-profile tiles (IR3) ───────────────────────────
                    st.markdown(
                        '<div class="t-h2" style="margin:14px 0 6px 0">'
                        '🛡️ Risk Profile '
                        '<span style="color:var(--dim);font-weight:400;font-size:12px">'
                        'current book · robust to the holdings assumption</span></div>',
                        unsafe_allow_html=True,
                    )
                    _rk = _rr.risk_metrics()
                    _risk_body = (
                        '<div style="display:grid;'
                        'grid-template-columns:repeat(auto-fit,minmax(140px,1fr));'
                        'gap:14px 22px">'
                        + _stat(_rk[0][0], _fmt_metric(_rk[0][1], _rk[0][2]),
                                tone=_perf_tone(_rk[0][0], _rk[0][1]),
                                align="center")
                        + _stat(_rk[1][0], _fmt_metric(_rk[1][1], _rk[1][2]),
                                tone=_perf_tone(_rk[1][0], _rk[1][1]),
                                align="center")
                        + _stat("Holdings Analysed", str(len(_rr.holdings_used)),
                                tone="neutral", align="center")
                        + _stat("Lookback (Days)", str(_rr.n_days),
                                tone="neutral", align="center")
                        + '</div>'
                    )
                    st.markdown(
                        _panel(_risk_body, kind="glass", tone="neutral",
                               margin="0 0 12px 0"),
                        unsafe_allow_html=True,
                    )

                    # ── IR2 · Beta Exposure hero ────────────────────────────
                    # Re-frames portfolio β from the Risk Profile row above
                    # with tone (defensive / market / aggressive) and pairs
                    # it with a per-holding β bar chart against a reference
                    # line at 1.0 so users can see WHICH positions push the
                    # book above or below market beta. Backlog IR2.
                    try:
                        from dashboard.shared.ui_components import (
                            panel as _ir2_panel, stat as _ir2_stat,
                        )
                        _pb = _rr.portfolio_beta
                        if _pb is not None:
                            if _pb < 0.85:
                                _pb_tone, _pb_word = "bull", "defensive vs Nifty"
                            elif _pb <= 1.15:
                                _pb_tone, _pb_word = "neutral", "tracks Nifty closely"
                            else:
                                _pb_tone, _pb_word = "bear", "amplifies market moves"
                            _beta_hero_html = _ir2_panel(
                                _ir2_stat(
                                    "Portfolio β vs Nifty",
                                    f"{_pb:.2f}",
                                    sub=_pb_word,
                                    tone=_pb_tone,
                                    align="left",
                                ),
                                kind="glass",
                                tone=_pb_tone,
                                title="🎯 Beta Exposure",
                            )
                            st.markdown(_beta_hero_html, unsafe_allow_html=True)
                            _bh_rows = [
                                {"Stock": p.ticker.replace(".NS", ""),
                                 "β": round(float(p.beta or 0.0), 3),
                                 "β contrib": round(float(p.weight_pct or 0.0)
                                                    * float(p.beta or 0.0) / 100.0, 3)}
                                for p in _rr.risk_contributions
                                if p.beta is not None
                            ]
                            if _bh_rows:
                                _bh_df = pd.DataFrame(_bh_rows).sort_values(
                                    "β", ascending=True)
                                _bh_df["_tone"] = _bh_df["β"].apply(
                                    lambda b: "defensive" if b < 0.85
                                    else "market" if b <= 1.15
                                    else "aggressive"
                                )
                                _fig_beta = px.bar(
                                    _bh_df, x="β", y="Stock", color="_tone",
                                    orientation="h",
                                    color_discrete_map={
                                        "defensive": _TK["bull"],
                                        "market":    _TK["azure"],
                                        "aggressive": _TK["bear"],
                                    },
                                    hover_data={"β contrib": True, "_tone": False},
                                    title=None,
                                )
                                _fig_beta.add_vline(
                                    x=1.0, line_dash="dot",
                                    line_color=_TK["ink"],
                                    annotation_text="Nifty β = 1.0",
                                    annotation_position="top",
                                    annotation_font_size=10,
                                    annotation_font_color=_TK["ink"],
                                )
                                _fig_beta.update_layout(
                                    template="nse_pro",
                                    height=max(180, 30 * len(_bh_df) + 90),
                                    margin=dict(l=0, r=0, t=10, b=0),
                                    showlegend=False,
                                )
                                st.plotly_chart(_fig_beta, width="stretch")
                                st.caption(
                                    "**β** = sensitivity to Nifty moves "
                                    "(1.0 = market). **β contrib** = weight × β "
                                    "— each position's push on portfolio β."
                                )
                        else:
                            st.caption(
                                "Beta unavailable — Nifty history could not be "
                                "loaded for this lookback."
                            )
                    except Exception as _ir2_e:
                        import logging as _ir2_log
                        _ir2_log.getLogger("dashboard.my_portfolio").debug(
                            "IR2 beta hero render failed: %s", _ir2_e)
                    _rcL, _rcR = st.columns([1, 1])
                    with _rcL:
                        # ── IR4 · Holdings correlation heatmap hero ─────────
                        # Adds: cleaner heatmap dressing (labels without the
                        # .NS suffix, tighter margins, RdBu_r kept for the
                        # bull/bear polarity users already read on this app),
                        # a plain-English diversification score derived from
                        # the average off-diagonal correlation, and named
                        # callouts for the most- and least-correlated pair.
                        # Skill: dataviz.
                        _cm = _rr.correlation_matrix
                        if _cm is not None and hasattr(_cm, "shape") and _cm.shape[0] >= 2:
                            # Clean labels
                            _cm_clean = _cm.copy()
                            _cm_clean.index = [str(i).replace(".NS", "")
                                               for i in _cm_clean.index]
                            _cm_clean.columns = [str(c).replace(".NS", "")
                                                 for c in _cm_clean.columns]
                            _figc = px.imshow(_cm_clean, text_auto=".2f",
                                              color_continuous_scale="RdBu_r",
                                              zmin=-1, zmax=1, aspect="auto")
                            _figc.update_layout(
                                template="nse_pro",
                                height=360,
                                margin=dict(l=0, r=0, t=8, b=0),
                                coloraxis_colorbar=dict(
                                    title="ρ", thickness=10, len=0.9,
                                    tickvals=[-1, -0.5, 0, 0.5, 1],
                                ),
                            )
                            _figc.update_xaxes(side="bottom", tickangle=-30)

                            # Diversification score from off-diagonal average.
                            try:
                                import numpy as _np
                                _vals = _cm.values.astype(float)
                                _mask = ~_np.eye(_vals.shape[0], dtype=bool)
                                _off = _vals[_mask]
                                _off = _off[~_np.isnan(_off)]
                                _avg_corr = float(_off.mean()) if _off.size else None
                            except Exception:
                                _avg_corr = None

                            if _avg_corr is not None:
                                if _avg_corr < 0.30:
                                    _div_tone, _div_word = "bull", "well diversified"
                                elif _avg_corr < 0.60:
                                    _div_tone, _div_word = "amber", "moderately diversified"
                                else:
                                    _div_tone, _div_word = "bear", "highly correlated"
                                try:
                                    from dashboard.shared.ui_components import (
                                        panel as _ir4_panel, stat as _ir4_stat,
                                    )
                                    st.markdown(
                                        _ir4_panel(
                                            _ir4_stat(
                                                "Avg pairwise ρ",
                                                f"{_avg_corr:.2f}",
                                                sub=_div_word,
                                                tone=_div_tone,
                                                align="left",
                                            ),
                                            kind="flat",
                                            tone=_div_tone,
                                            title="🔗 Holdings Correlation",
                                            margin="0 0 8px 0",
                                        ),
                                        unsafe_allow_html=True,
                                    )
                                except Exception:
                                    st.markdown(
                                        f"**🔗 Holdings Correlation** — avg pairwise ρ "
                                        f"= **{_avg_corr:.2f}** ({_div_word})."
                                    )
                            st.plotly_chart(_figc, width="stretch")

                            # Named callouts — most- and least-correlated pair.
                            try:
                                import numpy as _np
                                _vals = _cm.values.astype(float)
                                _np.fill_diagonal(_vals, _np.nan)
                                _flat = _np.where(_np.isnan(_vals), -_np.inf, _vals)
                                _i, _j = _np.unravel_index(_np.argmax(_flat), _vals.shape)
                                _hi_pair = (
                                    str(_cm.index[_i]).replace(".NS", ""),
                                    str(_cm.columns[_j]).replace(".NS", ""),
                                    float(_vals[_i, _j]),
                                )
                                _flat_lo = _np.where(_np.isnan(_vals), _np.inf, _vals)
                                _i2, _j2 = _np.unravel_index(_np.argmin(_flat_lo), _vals.shape)
                                _lo_pair = (
                                    str(_cm.index[_i2]).replace(".NS", ""),
                                    str(_cm.columns[_j2]).replace(".NS", ""),
                                    float(_vals[_i2, _j2]),
                                )
                                st.caption(
                                    f"**Most correlated:** {_hi_pair[0]} ↔ "
                                    f"{_hi_pair[1]} (ρ = {_hi_pair[2]:+.2f}) · "
                                    f"**Least:** {_lo_pair[0]} ↔ {_lo_pair[1]} "
                                    f"(ρ = {_lo_pair[2]:+.2f}). Diversification "
                                    "cuts drawdowns; high ρ means positions move together."
                                )
                            except Exception:
                                pass
                        elif _cm is not None:
                            st.caption(
                                "Correlation heatmap needs ≥2 holdings with shared history."
                            )
                        else:
                            st.caption("Correlation needs ≥2 holdings with shared history.")
                    with _rcR:
                        st.markdown("**Risk contribution by position**")
                        _rc_df = pd.DataFrame(
                            [{"Stock": p.ticker, "Weight %": p.weight_pct,
                              "Beta": p.beta,
                              # IR2: contribution to portfolio beta =
                              # weight × beta. Different from Risk %, which is
                              # variance-based.
                              "β Contrib": (round(float(p.weight_pct or 0.0)
                                                  * float(p.beta or 0.0) / 100.0, 3)
                                            if p.beta is not None else None),
                              "Risk %": p.risk_contribution_pct}
                             for p in _rr.risk_contributions])
                        st.dataframe(_rc_df, width="stretch", hide_index=True)
                        st.caption("**Risk %** = share of portfolio *variance* from each "
                                   "position — concentration of risk, which can differ "
                                   "from capital weight. **β Contrib** = weight × β — "
                                   "additive to portfolio β.")

                    with st.expander("ℹ️ Methodology & assumptions", expanded=False):
                        st.markdown("**Two metric groups, two interpretations:**")
                        st.markdown("- **Hypothetical Performance** assumes today's holdings were held "
                                    "over the whole lookback — read as *current-book hypothetical*, "
                                    "not realised returns.")
                        st.markdown("- **Risk Profile** (Beta, Volatility, Correlation, Risk "
                                    "Contribution) are current-book snapshots — safe to trust.")
                        for _n in _rr.notes:
                            st.markdown(f"- {_n}")
                        st.caption("Informational analytics — not investment advice.")

            # ── Concentration & Diversification (HHI) ─────────────────
            # IR5 (docs/UI_UX_BACKLOG.md) -- was a plain st.markdown header
            # + a mix of hand-rolled .metric-box + raw st.metric widgets +
            # a .card-green/yellow/red narrative div. Migrated to the same
            # §9 hero-tier treatment used by IR3 above: .t-h1 head + one
            # glass panel with 4 stat() tiles + a semantic panel() for the
            # recommendation narrative. Now reads as part of the same
            # anchor-artifact family as Portfolio Risk & Performance above.
            st.markdown("---")
            st.markdown(
                '<div class="t-h1" style="margin:6px 0 4px 0">'
                '🎯 Concentration &amp; Diversification</div>',
                unsafe_allow_html=True,
            )
            try:
                _conc_holdings = []
                _tot_val = sum(max(h.current_price * h.quantity, 0) for h in summary.holdings)
                if _tot_val > 0:
                    for h in summary.holdings:
                        _w = (h.current_price * h.quantity) / _tot_val * 100
                        _row = {"ticker": h.ticker.replace(".NS", ""), "weight_pct": _w}
                        _sec = getattr(h, "sector", None)
                        if _sec:
                            _row["sector"] = _sec
                        _conc_holdings.append(_row)
                _conc  = analyze_concentration(_conc_holdings)
                _grade = concentration_grade(_conc.hhi)

                # Semantic tone for each tile:
                #  - HHI: bull if diversified (<1500), amber if moderate,
                #    bear if concentrated (>2500) -- matches HHI bands.
                #  - Largest position: bull <10%, amber <20%, bear >=20%.
                #  - Top 5 weight: bull <50%, amber <75%, bear >=75%.
                #  - Holdings count: bull >=15, amber >=8, bear <8 (thin book).
                _hhi_tone = {"LOW": "bull", "MEDIUM": "amber", "HIGH": "bear"}.get(
                    _conc.risk_level, "neutral")
                def _lp_tone(w): return "bull" if w < 10 else "amber" if w < 20 else "bear"
                def _t5_tone(w): return "bull" if w < 50 else "amber" if w < 75 else "bear"
                def _n_tone(n):  return "bull" if n >= 15 else "amber" if n >= 8 else "bear"

                from dashboard.shared.ui_components import (
                    panel as _panel, stat as _stat,
                )
                _conc_body = (
                    '<div style="display:grid;'
                    'grid-template-columns:repeat(auto-fit,minmax(140px,1fr));'
                    'gap:14px 22px">'
                    + _stat("HHI Index", f"{_conc.hhi:,.0f}",
                            sub=f"{_conc.hhi_category} · Grade {_grade}",
                            tone=_hhi_tone, align="center")
                    + _stat("Largest Position", f"{_conc.top_1_weight:.1f}%",
                            tone=_lp_tone(_conc.top_1_weight), align="center")
                    + _stat("Top 5 Weight", f"{_conc.top_5_weight:.1f}%",
                            tone=_t5_tone(_conc.top_5_weight), align="center")
                    + _stat("Holdings", str(_conc.total_holdings),
                            tone=_n_tone(_conc.total_holdings), align="center")
                    + '</div>'
                )
                st.markdown(
                    _panel(_conc_body, kind="glass", tone="neutral",
                           margin="0 0 12px 0"),
                    unsafe_allow_html=True,
                )

                # Recommendation narrative -- semantic-toned panel that ties
                # to the HHI tile's tone above.
                _rec_tone = {"LOW": "bull", "MEDIUM": "amber", "HIGH": "bear"}.get(
                    _conc.risk_level, "neutral")
                _rec_body = (
                    f'<div style="font-size:13px;font-weight:700;color:var(--ink);'
                    f'margin-bottom:4px">Concentration risk: {_conc.risk_level}</div>'
                    f'<div style="font-size:13px;color:var(--ink-mid);line-height:1.55">'
                    f'{_conc.recommendation}</div>'
                )
                st.markdown(
                    _panel(_rec_body, kind="flat", tone=_rec_tone,
                           margin="0 0 8px 0"),
                    unsafe_allow_html=True,
                )
                if _conc.sector_concentration is not None:
                    st.caption(f"Sector HHI: {_conc.sector_concentration:,.0f} "
                               "(higher = more concentrated by sector).")
                st.caption("**HHI** = Σ(weight%)². <1500 diversified · 1500–2500 moderate · "
                           ">2500 concentrated. Informational, not advice.")
            except Exception as _conc_err:
                st.caption(f"Concentration analysis unavailable: {_conc_err}")

            # ── Fundamental Quality ────────────────────────────────────
            st.markdown(
                '<div class="t-h2" style="margin:16px 0 4px 0">'
                '🔬 Fundamental Quality Scores</div>',
                unsafe_allow_html=True,
            )
            st.caption("Quality score (0–100) from ROE/ROCE, revenue & EPS CAGR, "
                       "leverage, and FCF health. Fetches live fundamentals — opt-in.")
            if st.button("📊 Score my holdings on fundamentals", key="pf_fund_btn"):
                _fund_tickers = [h.ticker for h in summary.holdings]

                @st.cache_data(ttl=86400, show_spinner=False)
                def _cached_fundamentals(tickers_tuple: tuple):
                    rows = []
                    for _f in batch_fetch_fundamentals(list(tickers_tuple)):
                        try:
                            _q = compute_quality_score(_f)
                            _cov_n, _cov_total = quality_score_coverage(_f)
                        except Exception as _qs_e:
                            import logging; logging.getLogger("dashboard.my_portfolio").debug("compute_quality_score failed for %s: %s", _f.get("ticker"), _qs_e)
                            _q = None
                            _cov_n, _cov_total = 0, 4
                        rows.append({
                            "Stock":      str(_f.get("ticker", "")).replace(".NS", ""),
                            # QualityFix: 0 means no data — render as "—" not "0"
                            "Quality":    round(_q, 0) if (_q is not None and _q > 0) else None,
                            # Coverage — how many of the 4 weighted metrics
                            # actually fed the Quality score above, so a
                            # score based on 1 metric doesn't look as
                            # confident as one based on all 4.
                            "Coverage":   f"{_cov_n}/{_cov_total}",
                            "ROE %":      _f.get("roe"),
                            "ROCE %":     _f.get("roce"),
                            "Rev CAGR %": _f.get("revenue_cagr_5y") or _f.get("revenue_cagr_3y"),
                            "EPS CAGR %": _f.get("eps_cagr_5y") or _f.get("eps_cagr_3y"),
                            "D/E":        _f.get("debt_to_equity"),
                        })
                    return rows

                with st.spinner("Fetching fundamentals for your holdings…"):
                    _fund_rows = _cached_fundamentals(tuple(_fund_tickers))

                if _fund_rows:
                    _fdf = pd.DataFrame(_fund_rows).sort_values(
                        "Quality", ascending=False, na_position="last")
                    st.dataframe(
                        _fdf.style.format({
                            "Quality": "{:.0f}", "ROE %": "{:.1f}", "ROCE %": "{:.1f}",
                            "Rev CAGR %": "{:.1f}", "EPS CAGR %": "{:.1f}", "D/E": "{:.2f}",
                        }, na_rep="—").map(
                            # FIX BT1: background_gradient() requires matplotlib,
                            # not a project dependency — crashed on Streamlit
                            # Cloud. See dashboard/shared/chart_helpers.rdylgn_bg.
                            lambda v: rdylgn_bg(v, 0, 100), subset=["Quality"]),
                        hide_index=True, width="stretch")
                    _avg_q = _fdf["Quality"].dropna().mean()
                    if pd.notna(_avg_q):
                        st.caption(f"Average portfolio quality: **{_avg_q:.0f}/100** "
                                   f"across {_fdf['Quality'].notna().sum()} scored holdings.")
                    st.caption("**Coverage** = how many of the 4 metrics behind the Quality "
                               "score (ROE, ROCE, Revenue CAGR, EPS CAGR) were actually "
                               "available — Yahoo's fundamentals coverage is patchy for "
                               "small/mid-caps, so a score based on fewer metrics is a less "
                               "reliable read than one based on all 4.")
                else:
                    st.info("No fundamental data could be retrieved (source may be rate-limited).")

            # ── IR6 · Contribution to return + risk-contribution bar ────
            # Backlog IR6 (UI_UX_BACKLOG.md · 🟨 P2 · M). Per-holding P&L is
            # already computed on summary.holdings (avg_buy_price, quantity,
            # current_price → pnl in ₹), and per-holding risk contribution
            # sits on _rr.risk_contributions from analysis/portfolio_risk.py.
            # The two together answer "which positions built or ate the
            # book's return, and which ones drove its variance?"
            st.markdown("---")
            st.markdown("##### 📊 Contribution to return & risk")
            try:
                _ir6_rows = []
                _tot_pnl = 0.0
                for _h in summary.holdings:
                    _pnl = float(getattr(_h, "pnl", 0.0) or 0.0)
                    _tot_pnl += _pnl
                    _ir6_rows.append({
                        "Stock": _h.ticker.replace(".NS", ""),
                        "P&L (₹)": _pnl,
                    })
                if _ir6_rows and abs(_tot_pnl) > 1e-6:
                    for _row in _ir6_rows:
                        _row["Contrib %"] = round(
                            _row["P&L (₹)"] / _tot_pnl * 100.0, 2)
                elif _ir6_rows:
                    for _row in _ir6_rows:
                        _row["Contrib %"] = 0.0

                # Merge in risk contribution from _rr where available.
                _rc_map = {}
                try:
                    for _p in _rr.risk_contributions:
                        _rc_map[_p.ticker.replace(".NS", "")] = (
                            float(_p.risk_contribution_pct or 0.0))
                except Exception:
                    _rc_map = {}
                for _row in _ir6_rows:
                    _row["Risk %"] = _rc_map.get(_row["Stock"])

                # Sort by absolute P&L descending — biggest movers first.
                _ir6_rows.sort(key=lambda r: -abs(r["P&L (₹)"]))
                _ir6_df = pd.DataFrame(_ir6_rows)

                _c1, _c2 = st.columns([1, 1])
                with _c1:
                    st.markdown("**Return contribution** — who built or ate the book's P&L")
                    if _tot_pnl != 0:
                        _cr_df = _ir6_df.copy()
                        _cr_df["_tone"] = _cr_df["P&L (₹)"].apply(
                            lambda v: "gain" if v > 0
                            else "loss" if v < 0 else "flat"
                        )
                        _cr_sorted = _cr_df.sort_values("P&L (₹)", ascending=True)
                        _fig_cr = px.bar(
                            _cr_sorted, x="P&L (₹)", y="Stock",
                            color="_tone", orientation="h",
                            color_discrete_map={
                                "gain": _TK["bull"], "loss": _TK["bear"],
                                "flat": _TK["dim"],
                            },
                            hover_data={"Contrib %": ":.1f", "_tone": False},
                            title=None,
                        )
                        _fig_cr.add_vline(
                            x=0, line_dash="dot", line_color=_TK["ink"],
                            opacity=0.5,
                        )
                        _fig_cr.update_layout(
                            template="nse_pro",
                            height=max(180, 30 * len(_cr_sorted) + 60),
                            margin=dict(l=0, r=0, t=8, b=0),
                            showlegend=False,
                        )
                        st.plotly_chart(_fig_cr, width="stretch")
                        st.caption(
                            f"Total book P&L: **₹{_tot_pnl:,.0f}**. Bars show "
                            "each position's contribution — capital gainers vs "
                            "the drag on the book."
                        )
                    else:
                        st.caption(
                            "Total P&L is zero — contribution attribution needs "
                            "non-zero positions."
                        )
                with _c2:
                    st.markdown("**Risk contribution** — who drives portfolio variance")
                    if any(r.get("Risk %") is not None for r in _ir6_rows):
                        _rk_df = pd.DataFrame([
                            {"Stock": r["Stock"], "Risk %": r["Risk %"]}
                            for r in _ir6_rows if r.get("Risk %") is not None
                        ]).sort_values("Risk %", ascending=True)
                        _fig_rk = px.bar(
                            _rk_df, x="Risk %", y="Stock", orientation="h",
                            color="Risk %", color_continuous_scale="Oranges",
                            title=None,
                        )
                        _fig_rk.update_layout(
                            template="nse_pro",
                            height=max(180, 30 * len(_rk_df) + 60),
                            margin=dict(l=0, r=0, t=8, b=0),
                            coloraxis_showscale=False,
                        )
                        st.plotly_chart(_fig_rk, width="stretch")
                        st.caption(
                            "Risk % = share of portfolio **variance** each name "
                            "contributes. When it's much bigger than capital "
                            "weight, that name is doing outsized damage on down "
                            "days — a hedge or trim target."
                        )
                    else:
                        st.caption(
                            "Risk contribution needs the Portfolio Risk block "
                            "above to compute — enable holdings with ≥2 tickers."
                        )
            except Exception as _ir6_e:  # noqa: BLE001
                import logging as _ir6_log
                _ir6_log.getLogger("dashboard.my_portfolio").debug(
                    "IR6 contribution section render failed: %s", _ir6_e)

            # ── Best / Worst ───────────────────────────────────────────
            # Migrated to shared panel() so both cards sit on §9 card-lift
            # with matching hairline + semantic rail (bull green / bear red)
            # instead of the older .card-green / .card-red utility classes.
            st.markdown("---")
            bw_cols = st.columns(2)
            from dashboard.shared.ui_components import panel as _bw_panel
            if summary.best_holding:
                bh = summary.best_holding
                with bw_cols[0]:
                    _bh_body = (
                        f'<div style="font-size:11px;color:var(--dim);'
                        f'letter-spacing:.12em;text-transform:uppercase;'
                        f'font-weight:600">🏆 Best Performer</div>'
                        f'<div style="font-size:16px;font-weight:700;color:var(--ink);'
                        f'margin-top:4px">{bh.ticker.replace(".NS", "")}</div>'
                        f'<div style="font-family:var(--font-mono);font-size:14px;'
                        f'color:var(--bull);font-weight:600;margin-top:2px">'
                        f'+{bh.pnl_pct:.1f}% · ₹+{bh.pnl:,.0f}</div>'
                    )
                    st.markdown(_bw_panel(_bh_body, kind="flat", tone="bull",
                                          margin="6px 0"),
                                unsafe_allow_html=True)
            if summary.worst_holding:
                wh = summary.worst_holding
                with bw_cols[1]:
                    sign = "+" if wh.pnl_pct >= 0 else ""
                    _wh_tone = "bear" if wh.pnl_pct < 0 else "amber"
                    _wh_color = "var(--bear)" if wh.pnl_pct < 0 else "var(--amber)"
                    _wh_body = (
                        f'<div style="font-size:11px;color:var(--dim);'
                        f'letter-spacing:.12em;text-transform:uppercase;'
                        f'font-weight:600">📉 Needs Attention</div>'
                        f'<div style="font-size:16px;font-weight:700;color:var(--ink);'
                        f'margin-top:4px">{wh.ticker.replace(".NS", "")}</div>'
                        f'<div style="font-family:var(--font-mono);font-size:14px;'
                        f'color:{_wh_color};font-weight:600;margin-top:2px">'
                        f'{sign}{wh.pnl_pct:.1f}% · ₹{sign}{wh.pnl:,.0f}</div>'
                    )
                    st.markdown(_bw_panel(_wh_body, kind="flat", tone=_wh_tone,
                                          margin="6px 0"),
                                unsafe_allow_html=True)

            # ── Export ─────────────────────────────────────────────────
            st.markdown("---")
            export_df = pd.DataFrame([{
                "Ticker":   h.ticker.replace(".NS", ""),
                "Qty":      h.quantity,
                "Buy Price": h.avg_buy_price,
                "Current":  h.current_price,
                "P&L (₹)":  round(h.pnl, 2),
                "P&L (%)":  round(h.pnl_pct, 2),
                "Score":    h.score,
                "Grade":    h.grade,
                "Action":   h.action,   # internal string kept in CSV
                "Signal":   h.signal.replace("🟢","G").replace("🟡","Y").replace("🔴","R"),
                "Sector":   h.sector,
            } for h in summary.holdings])
            st.download_button(
                "📥 Download Full Report CSV",
                data=export_df.to_csv(index=False).encode(),
                file_name="portfolio_health_report.csv",
                mime="text/csv",
            )

        except Exception as e:
            import logging as _pf_logging, traceback as _pf_tb
            _pf_logging.getLogger("dashboard.my_portfolio").error(
                "Portfolio analysis failed: %s", e, exc_info=True)
            st.error(f"Portfolio analysis failed: {type(e).__name__}: {e}")
            with st.expander("Show technical details (for debugging)"):
                st.code(_pf_tb.format_exc())
        finally:
            # FIX MH4 — always clean up the temp CSV, success or failure.
            if _pm_tmp_csv_path:
                try:
                    os.remove(_pm_tmp_csv_path)
                except Exception as _rm_e:
                    import logging; logging.getLogger("dashboard.my_portfolio").debug("Could not remove tmp CSV %s: %s", _pm_tmp_csv_path, _rm_e)
else:
    st.markdown("---")
    st.info(
        "No holdings yet. Use **➕ Add a holding** above to get started.  \n\n"
        "**What you'll see once added:**  \n"
        "- 🚀 Strong Trend / Uptrend = strong, persistent trend momentum  \n"
        "- 🟡 Neutral = mixed signals, no clear edge  \n"
        "- ⚠️ Weakening / Broken Trend = trend deteriorating  \n"
        "- Composite score (0–90) — higher = stronger trend (not a return forecast)  \n"
        "- Plain English explanation and the model's reference stop / target levels per holding"
    )
    col_ex1, col_ex2, col_ex3 = st.columns(3)
    with col_ex1:
        st.markdown("""
        <div class="card-green">
        <b>🚀 Strong Trend ▲▲ / Uptrend ▲ (Score ≥ 65)</b><br>
        Technicals, momentum, and volume are aligned.
        This is a trend-quality score — not a return forecast or advice to buy.
        </div>
        """, unsafe_allow_html=True)
    with col_ex2:
        st.markdown("""
        <div class="card-yellow">
        <b>🟡 Neutral (Score 40–64)</b><br>
        Mixed signals — some positives, some caution.
        No clear directional edge right now.
        </div>
        """, unsafe_allow_html=True)
    with col_ex3:
        st.markdown("""
        <div class="card-red">
        <b>⚠️ Weakening ▼ / Broken Trend ▼▼ (Score &lt; 40)</b><br>
        Trend quality is deteriorating.
        Price has broken below key averages or momentum has turned negative.
        </div>
        """, unsafe_allow_html=True)
