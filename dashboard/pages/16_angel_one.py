"""Angel One - NSE Smart Investor (multipage page; body verbatim from app.py).

LAYOUT (2026-09-29): artboard 11 of the 2026-09-12 "proposed layout" mockup.
A quiet status panel (connection state, when the session renews, breaker /
budget / cache tiles) beside masked credentials and a recent-activity log,
all read from angel_fetcher.status() / credential_summary() with no extra
network calls. Account, holdings, positions, orders, quick order and the
holding-posture read follow as tabs once connected.
"""
import os, sys
import datetime as _dt
import logging

_log = logging.getLogger("dashboard.angel_one")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
import pandas as pd
import streamlit as st
from dashboard.shared.design import apply_design
from dashboard.shared.nav import render_sidebar
from dashboard.shared.chart_helpers import render_top_bar
from dashboard.shared.tokens import COLORS as _TK  # noqa: E402
from dashboard.shared.ui_components import (
    activity_log,
    chip_pill,
    gate_strip,
    kit_card,
    kv_list,
    section_header,
    status_panel,
)

apply_design()
render_sidebar(current="Angel One")
render_top_bar()

# ───────────────────────── page body (de-indented from app.py) ─────────────────────────
from data.angel_fetcher import (  # noqa: E402
    is_configured as _ao_is_configured,
    _get_session as _ao_get_session,
    credential_summary as _ao_credentials,
    status as _ao_status,
    get_profile as _ao_get_profile,
    get_funds as _ao_get_funds,
    get_holdings as _ao_get_holdings,
    get_positions as _ao_get_positions,
    get_order_book as _ao_get_orders,
    get_trade_book as _ao_get_trades,
    place_order as _ao_place_order,
    cancel_order as _ao_cancel_order,
    get_gtt_list as _ao_get_gtts,
    cancel_gtt as _ao_cancel_gtt,
    clear_session as _ao_clear_session,
)

st.markdown(
    '<h1 class="page-title-serif">Angel <em>One</em></h1>'
    '<p class="page-subtitle">Broker integration · the first live-price source when connected.</p>',
    unsafe_allow_html=True,
)

_ao_ok = _ao_is_configured()
if _ao_ok:
    # FIX AO-PIN (audit 2026-10-04): the app has no login, so every viewer
    # could see holdings/funds and place real orders. Lock the page behind
    # the owner's PIN before any broker call. Not configured → setup text
    # below needs no PIN (nothing sensitive is shown).
    from dashboard.shared.pin_gate import require_pin as _require_pin
    _require_pin("angel_one", "Angel One")

if _ao_ok:
    # Log in (or reuse the cached session) so the status below is current.
    try:
        _ao_get_session()
    except Exception as _sess_e:
        _log.debug("Angel One session check failed: %s", _sess_e)
_st = _ao_status()
_cr = _ao_credentials()


def _hhmm(ts) -> str:
    return _dt.datetime.fromtimestamp(float(ts)).strftime("%H:%M") if ts else "—"


_cool_min = _st["breaker_cooldown_s"] // 60
if not _ao_ok:
    _label, _tone = "Not connected", "neutral"
    _headline = "Credentials <em>not configured</em>"
    _hint = ("Live prices come from Yahoo and NSE until Angel One is set up. "
             "Four steps below; the page lights up after a restart.")
elif _st["login_breaker_open"]:
    _label, _tone = "Login paused", "bad"
    _headline = f"Login <em>paused for {_cool_min} min</em>"
    _hint = (f"{_st['login_fails']} logins in a row were refused, so attempts are paused. "
             "Check that the password and TOTP seed are still current.")
elif not _st["logged_in"]:
    _label, _tone = "Configured · not logged in", "warn"
    _headline = "Login <em>didn't go through</em>"
    _hint = "The last attempt was refused or errored; Recent activity has the reason."
elif _st["data_breaker_open"]:
    _label, _tone = "Connected · data paused", "warn"
    _headline = f"Session active, quotes <em>paused {_cool_min} min</em>"
    _hint = (f"{_st['data_fails']} Angel One calls failed in a row, so the fetcher is using "
             "Yahoo until the pause ends. No action needed.")
else:
    _label, _tone = "Connected · authenticated", "good"
    _renews_at = _dt.datetime.now().timestamp() + (_st["renews_in_s"] or 0)
    _headline = f"Session active · renews at <em>{_hhmm(_renews_at)}</em>"
    _hint = (f"Angel One is the first price source. After {_st['breaker_threshold']} failed calls in "
             f"a row the fetcher pauses it for {_cool_min} min and falls back to Yahoo on its own; "
             "bulk scans always use Yahoo.")

_stats = [
    ("Logged in", _hhmm(_st["login_at"]) if _st["logged_in"] else "—",
     "this process" if _st["logged_in"] else ""),
    ("Renews in", f"{int((_st['renews_in_s'] or 0) // 60)} min" if _st["logged_in"] else "—",
     "auto re-login" if _st["logged_in"] else ""),
    ("Candle budget", f"{_st['candle_calls_last_min']} / {_st['candle_budget_per_min']}", "last minute"),
    ("Failed calls", f"{_st['data_fails']}", f"pause at {_st['breaker_threshold']}"),
    ("Instruments", f"{_st['instruments']:,}" if _st["instruments"] else "—",
     "NSE EQ tokens" if _st["instruments"] else "not loaded yet"),
    ("Symbols resolved", f"{_st['tokens_cached']:,}", "token cache"),
]

_ao_l, _ao_r = st.columns([1.55, 1], gap="medium")
with _ao_l:
    # Tiles only once credentials exist; before that they'd all read "—" / 0.
    st.markdown(status_panel(_label, _tone, _headline, _hint, _stats if _ao_ok else None),
                unsafe_allow_html=True)
with _ao_r:
    _cr_labels = (("client_id", "Client code"), ("api_key", "API key"),
                  ("password", "Password"), ("totp_secret", "TOTP seed"))
    _cr_rows = [
        (_lbl, _cr[_k]["masked"] if _cr[_k]["masked"] else chip_pill("missing", "bad"))
        for _k, _lbl in _cr_labels
    ]
    _srcs = {_cr[_k]["source"] for _k, _ in _cr_labels} - {""}
    _cr_sub = {"secrets": "Streamlit secrets", "env": "environment variables"}.get(
        next(iter(_srcs)), "") if len(_srcs) == 1 else ("mixed sources" if _srcs else "none set")
    st.markdown(
        kit_card("Credentials", kv_list(
            _cr_rows,
            note="Kept in <b>.streamlit/secrets.toml</b> (gitignored) or the platform's secret "
                 "store. Only the last four characters are ever shown here."),
            sub=_cr_sub),
        unsafe_allow_html=True,
    )
    st.markdown(kit_card("Recent activity", activity_log(_st["events"][:8]),
                         sub="this server process · since start"),
                unsafe_allow_html=True)

# ── Credentials setup ────────────────────────────────────────────────────
if not _ao_ok:
    with _ao_l:
        st.markdown(kit_card("Connect in four steps", (
            '<ol class="ao-steps">'
            "<li><b>Get your SmartAPI key.</b> Angel One → My Profile → API Key (or "
            '<a href="https://smartapi.angelone.in" target="_blank">smartapi.angelone.in</a>) '
            "→ Generate API Key → copy it.</li>"
            "<li><b>Get your TOTP seed.</b> Profile → Security Settings → Two-Factor "
            "Authentication → Re-Setup → “Can't scan QR?” → copy the base32 text.</li>"
            "<li><b>Add the credentials</b> to <code>.streamlit/secrets.toml</code> (gitignored) "
            "or your platform's secret store, never in code (template below).</li>"
            "<li><b>Restart Streamlit</b> so the session picks them up.</li></ol>"
        )), unsafe_allow_html=True)
        st.code(
            "[angel_one]\n"
            'api_key     = "<your SmartAPI key>"\n'
            'client_id   = "<your client ID>"\n'
            'password    = "<your login password>"\n'
            'totp_secret = "<base32 TOTP seed>"\n',
            language="toml",
        )
        st.caption("Or set environment variables:")
        st.code("ANGEL_API_KEY=...  ANGEL_CLIENT_ID=...  ANGEL_PASSWORD=...  "
                "ANGEL_TOTP_SECRET=...", language="bash")
    st.stop()

# ── Connected — session control + tabs ───────────────────────────────────
if st.button("🔄 Refresh Session", key="ao_refresh"):
    # FIX MKT6: was also calling a blanket st.cache_data.clear() here,
    # which wiped every other page's cached data (Top Picks, watchlist
    # scans, etc.) for zero benefit — this page doesn't actually use
    # st.cache_data for any of its own data (funds/holdings/positions
    # are fetched live from angel_fetcher on every rerun already).
    # _ao_clear_session() is what actually matters: it forces a fresh
    # login on the next request.
    _ao_clear_session()
    st.success("Session cleared — reconnecting on next request")
    st.rerun()
if st.button("🔒 Lock page", key="ao_lock"):
    from dashboard.shared.pin_gate import relock as _relock
    _relock("angel_one")
    st.rerun()

tab_ao1, tab_ao2, tab_ao3, tab_ao4, tab_ao5, tab_ao6 = st.tabs([
    "Account", "Holdings", "Today's positions", "Orders & trades",
    "Quick order", "Holding posture",
])

# ── TAB 1: ACCOUNT ────────────────────────────────────────────────────────
with tab_ao1:
    st.markdown(section_header("Account", "profile · funds"), unsafe_allow_html=True)
    try:
        _funds = _ao_get_funds()
        if _funds:
            _m2m = _funds["m2m"]
            _m2m_clr = "var(--bull)" if _m2m >= 0 else "var(--bear)"
            st.markdown(gate_strip([
                ("Available cash", f"₹{_funds['available_cash']:,.0f}", ""),
                ("Used margin", f"₹{_funds['used_margin']:,.0f}", ""),
                ("Unrealised P&L", f'<span style="color:{_m2m_clr}">₹{_m2m:+,.0f}</span>', "mark to market"),
            ]), unsafe_allow_html=True)
        else:
            st.caption("Funds data unavailable.")
    except Exception as e:
        _log.debug("Angel One funds display failed: %s", e)
        st.caption("Funds data unavailable.")
    try:
        _prof = _ao_get_profile()
        if _prof:
            st.markdown(kit_card("Profile", kv_list([
                ("Name", _prof["name"]),
                ("Client ID", _prof["client_id"]),
                ("Email", _prof["email"]),
                ("Exchanges", ", ".join(_prof["exchanges"])),
            ])), unsafe_allow_html=True)
    except Exception as e:
        _log.debug("Angel One profile display failed: %s", e)
        st.caption("Profile unavailable.")

# ── TAB 2: HOLDINGS ────────────────────────────────────────────────────────
with tab_ao2:
    st.markdown(section_header("Demat holdings", "live from Angel One"), unsafe_allow_html=True)
    with st.spinner("Fetching holdings from Angel One…"):
        _holdings = _ao_get_holdings()

    if _holdings is None:
        st.error("Could not fetch holdings. Check credentials and try again.")
    elif len(_holdings) == 0:
        st.info("No holdings found in your demat account.")
    else:
        _total_invested = sum(h["avg_price"] * h["qty"] for h in _holdings)
        _total_value    = sum(h["value_rs"] for h in _holdings)
        _total_pnl      = _total_value - _total_invested
        _total_pnl_pct  = (_total_pnl / _total_invested * 100) if _total_invested > 0 else 0

        _pnl_clr = "var(--bull)" if _total_pnl >= 0 else "var(--bear)"
        st.markdown(gate_strip([
            ("Stocks", f"{len(_holdings)}", ""),
            ("Portfolio value", f"₹{_total_value:,.0f}", ""),
            ("Total P&L", f'<span style="color:{_pnl_clr}">₹{_total_pnl:+,.0f}</span>',
             f"{_total_pnl_pct:+.2f}%"),
            ("Invested", f"₹{_total_invested:,.0f}", ""),
        ]), unsafe_allow_html=True)

        _hdf = pd.DataFrame(_holdings)
        _hdf = _hdf[["symbol", "qty", "avg_price", "ltp", "pnl", "pnl_pct", "value_rs"]]
        _hdf.columns = ["Symbol", "Qty", "Avg Price", "LTP", "P&L (Rs)", "P&L %", "Value (Rs)"]

        def _color_pnl(val):
            # F7b · font-weight bump acts as a non-colour cue reinforcing the
            # ▲/▼ prefix added by the formatters below — so P&L direction is
            # legible without relying on the red/green channel alone.
            if isinstance(val, (int, float)):
                color = _TK["bull"] if val >= 0 else _TK["bear"]
                return f"color: {color}; font-weight:700"
            return ""

        def _fmt_pnl_rs(val):
            try:
                v = float(val)
            except (ValueError, TypeError):
                return val
            return f"{'▲' if v >= 0 else '▼'} Rs {abs(v):,.0f}"

        def _fmt_pnl_pct(val):
            try:
                v = float(val)
            except (ValueError, TypeError):
                return val
            return f"{'▲' if v >= 0 else '▼'} {abs(v):.2f}%"

        _hdf_styled = (
            _hdf.style
            .format({
                "Avg Price": "Rs {:.2f}",
                "LTP":       "Rs {:.2f}",
                "P&L (Rs)":  _fmt_pnl_rs,
                "P&L %":     _fmt_pnl_pct,
                "Value (Rs)":"Rs {:.0f}",
            })
            .map(_color_pnl, subset=["P&L (Rs)", "P&L %"])
        )
        st.dataframe(_hdf_styled, hide_index=True, width="stretch")

        # Export
        _holdings_csv = _hdf.to_csv(index=False).encode("utf-8")
        st.download_button(
            "⬇ Export Holdings CSV",
            _holdings_csv,
            file_name="angel_one_holdings.csv",
            mime="text/csv",
        )

# ── TAB 3: TODAY'S POSITIONS ───────────────────────────────────────────────
with tab_ao3:
    st.markdown(section_header("Today's positions", "net · intraday + delivery"),
                unsafe_allow_html=True)
    with st.spinner("Fetching positions…"):
        _positions = _ao_get_positions()

    if _positions is None:
        st.error("Could not fetch positions.")
    else:
        _net_pos = _positions.get("net", [])
        _closed_pos = _positions.get("closed", [])
        if _closed_pos:
            _realised = sum(p["pnl"] for p in _closed_pos)
            st.caption(f"{len(_closed_pos)} position(s) squared off today · "
                       f"realised P&L Rs {_realised:+,.0f}")
            _cdf = pd.DataFrame(_closed_pos)[["symbol", "segment", "expiry", "product", "pnl"]]
            _cdf.columns = ["Symbol", "Segment", "Expiry", "Product", "Realised P&L"]
            st.dataframe(_cdf, hide_index=True, width="stretch")
        if not _net_pos:
            st.info("No open positions right now.")
        else:
            _pos_df = pd.DataFrame(_net_pos)
            _pos_df = _pos_df[["symbol", "segment", "expiry", "qty", "avg_price", "ltp",
                               "pnl", "product", "side"]]
            _pos_df.columns = ["Symbol", "Segment", "Expiry", "Qty", "Avg Price", "LTP",
                               "P&L", "Product", "Side"]

            _total_pos_pnl = sum(p["pnl"] for p in _net_pos)
            _pos_clr = "var(--bull)" if _total_pos_pnl >= 0 else "var(--bear)"
            st.markdown(gate_strip([
                ("Open positions", f"{len(_net_pos)}", ""),
                ("P&L today", f'<span style="color:{_pos_clr}">₹{_total_pos_pnl:+,.0f}</span>', ""),
                ("Long / short", f"{sum(1 for p in _net_pos if p['qty'] > 0)} / "
                                 f"{sum(1 for p in _net_pos if p['qty'] < 0)}", ""),
            ]), unsafe_allow_html=True)

            # F7b · same colour-blind pattern as the Holdings tab above —
            # ▲/▼ prefix in the P&L display + font-weight:700 so direction
            # survives red-green colour-vision deficiency.
            def _fmt_pos_pnl(val):
                try:
                    v = float(val)
                except (ValueError, TypeError):
                    return val
                return f"{'▲' if v >= 0 else '▼'} Rs {abs(v):,.0f}"

            st.dataframe(
                _pos_df.style
                .format({
                    "Avg Price": "Rs {:.2f}",
                    "LTP":       "Rs {:.2f}",
                    "P&L":       _fmt_pos_pnl,
                })
                .map(lambda v: f"color:{_TK['bull']};font-weight:700" if isinstance(v, (int,float)) and v >= 0
                     else (f"color:{_TK['bear']};font-weight:700" if isinstance(v, (int,float)) else ""),
                     subset=["P&L"]),
                hide_index=True,
                width="stretch",
            )

# ── TAB 4: ORDERS & TRADES ─────────────────────────────────────────────────
with tab_ao4:
    st.markdown(section_header("Today's orders &amp; trades", "order book · trade book · GTT"),
                unsafe_allow_html=True)
    ord_t1, ord_t2, ord_t3 = st.tabs(["Order book", "Trade book", "GTT orders"])

    with ord_t1:
        with st.spinner("Fetching order book…"):
            _orders = _ao_get_orders()
        if _orders is None:
            st.error("Could not fetch orders.")
        elif not _orders:
            st.info("No orders today.")
        else:
            _odf = pd.DataFrame(_orders)[
                ["order_id", "symbol", "side", "qty", "filled_qty",
                 "order_type", "price", "avg_price", "status", "time"]
            ]
            _odf.columns = ["Order ID", "Symbol", "Side", "Qty", "Filled",
                             "Type", "Price", "Fill Price", "Status", "Time"]

            def _status_color(val):
                colors = {
                    "complete": _TK["bull"], "rejected": _TK["bear"],
                    "cancelled": _TK["dim"], "open": _TK["amber"],
                    "pending": _TK["amber"],
                }
                c = colors.get(str(val).lower(), _TK["ink-mid"])
                return f"color:{c}; font-weight:600"

            st.dataframe(
                _odf.style.map(_status_color, subset=["Status"]),
                hide_index=True,
                width="stretch",
            )

            # Cancel pending order
            _pending = [o for o in _orders if o["status"].lower() in ("open", "pending", "trigger pending")]
            if _pending:
                st.markdown("**Cancel Pending Order:**")
                _cancel_opts = {f"{o['symbol']} — {o['side']} {o['qty']} @ {o['price']}": o["order_id"]
                                for o in _pending}
                _to_cancel = st.selectbox("Select order", list(_cancel_opts.keys()), key="ao_cancel_sel")
                if st.button("Cancel Order", key="ao_cancel_btn", type="primary"):
                    if _ao_cancel_order(_cancel_opts[_to_cancel]):
                        st.success("Order cancelled")
                        st.rerun()
                    else:
                        st.error("Cancel failed — order may already be processed")

    with ord_t2:
        with st.spinner("Fetching trade book…"):
            _trades = _ao_get_trades()
        if _trades is None:
            st.error("Could not fetch trades.")
        elif not _trades:
            st.info("No executed trades today.")
        else:
            _tdf = pd.DataFrame(_trades)[
                ["symbol", "side", "qty", "price", "product", "time"]
            ]
            _tdf.columns = ["Symbol", "Side", "Qty", "Price", "Product", "Time"]
            _total_traded = sum(
                t["qty"] * t["price"]
                for t in _trades
                if isinstance(t.get("qty"), (int, float)) and isinstance(t.get("price"), (int, float))
            )
            st.metric("Total Turnover Today", f"Rs {_total_traded:,.0f}")
            st.dataframe(_tdf, hide_index=True, width="stretch")

    with ord_t3:
        with st.spinner("Fetching GTT rules…"):
            _gtts = _ao_get_gtts()
        if _gtts is None:
            st.error("Could not fetch GTT rules.")
        elif not _gtts:
            st.info("No active GTT orders.")
        else:
            _gdf = pd.DataFrame(_gtts)[
                ["rule_id", "symbol", "side", "qty", "trigger", "limit_price", "status"]
            ]
            _gdf.columns = ["Rule ID", "Symbol", "Side", "Qty", "Trigger", "Limit", "Status"]
            st.dataframe(_gdf, hide_index=True, width="stretch")

            _active_gtts = [g for g in _gtts if g["status"].lower() in ("new", "active")]
            if _active_gtts:
                st.markdown("**Cancel GTT:**")
                _gtt_opts = {f"{g['symbol']} {g['side']} {g['qty']} @ trigger {g['trigger']}": g
                             for g in _active_gtts}
                _gtt_sel = st.selectbox("Select GTT", list(_gtt_opts.keys()), key="ao_gtt_sel")
                if st.button("Cancel GTT", key="ao_gtt_cancel_btn"):
                    _g = _gtt_opts[_gtt_sel]
                    if _ao_cancel_gtt(_g["rule_id"], _g["symbol"]):
                        st.success("GTT cancelled")
                        st.rerun()
                    else:
                        st.error("Could not cancel GTT")

# ── TAB 5: QUICK ORDER ─────────────────────────────────────────────────────
with tab_ao5:
    st.markdown(section_header("Quick order", "places a real order"), unsafe_allow_html=True)
    st.warning(
        "This places a **real order** in your Angel One account using live funds. "
        "Double-check all details before confirming.",
        icon="⚠️",
    )

    qo_c1, qo_c2 = st.columns(2)
    with qo_c1:
        _qo_sym   = st.text_input("Stock Symbol (NSE)", value="", placeholder="e.g. RELIANCE", key="qo_sym").strip().upper()
        _qo_qty   = st.number_input("Quantity", min_value=1, value=1, step=1, key="qo_qty")
        _qo_side  = st.radio("Transaction", ["BUY", "SELL"], horizontal=True, key="qo_side")
        _qo_prod  = st.radio("Product", ["DELIVERY", "INTRADAY"], horizontal=True, key="qo_prod")

    with qo_c2:
        _qo_type  = st.selectbox("Order Type", ["MARKET", "LIMIT", "SL", "SL-M"], key="qo_type")
        _qo_price = st.number_input("Limit price (LIMIT and SL; ignored for MARKET / SL-M)",
                                     min_value=0.0, value=0.0, step=0.05, format="%.2f", key="qo_price")
        _qo_trig  = st.number_input("Trigger price (SL and SL-M only)",
                                     min_value=0.0, value=0.0, step=0.05, format="%.2f", key="qo_trig")
        _qo_valid = st.radio("Validity", ["DAY", "IOC"], horizontal=True, key="qo_valid")

    if _qo_sym:
        _card_cls = "order-buy" if _qo_side == "BUY" else "order-sell"
        st.markdown(
            f'<div class="{_card_cls}">'
            f'<b>{_qo_side} {_qo_qty} × {_qo_sym}</b>  |  '
            f'{_qo_prod} · {_qo_type}'
            + (f'  |  Price: Rs {_qo_price:.2f}' if _qo_price > 0 else "  |  Market Order")
            + f'</div>',
            unsafe_allow_html=True,
        )

    _ao_confirm = st.checkbox(
        "I confirm this is a real order with real money", key="qo_confirm"
    )
    _place_col, _ = st.columns([1, 3])
    with _place_col:
        if st.button("Place Order", type="primary", key="qo_place",
                     disabled=not (_qo_sym and _ao_confirm)):
            with st.spinner("Placing order…"):
                _result = _ao_place_order(
                    symbol=_qo_sym,
                    qty=int(_qo_qty),
                    side=_qo_side,
                    order_type=_qo_type,
                    price=float(_qo_price),
                    trigger_price=float(_qo_trig),
                    product=_qo_prod,
                    validity=_qo_valid,
                )
            if _result and _result.get("status") == "placed":
                st.success(f"Order placed! Order ID: {_result.get('order_id')}")
            elif _result and _result.get("status") == "invalid":
                st.error(f"Not sent: {_result.get('message')}")
            elif _result:
                st.error(f"Order failed: {_result.get('message', 'Unknown error')}")
            else:
                st.error("Could not connect to Angel One — check session")

# ── TAB 6: HOLDING POSTURE ─────────────────────────────────────────────────
# Runs the same six-filter momentum recipe used by the alerts' momentum scan
# on every delivery holding and every open intraday position, so the user
# sees at a glance which names still have their trend intact vs which have
# decayed. Pure analysis (no order side-effects). See
# analysis/portfolio_posture.py for the classifier. The engine's codes
# (EXIT_WATCH, ADD_WATCH, ...) read as instructions, so the page shows
# descriptive labels for them (CLAUDE.md rule 1).
_POSTURE_LABELS = {
    "EXIT_WATCH":    "Trend broken",
    "TRIM_WATCH":    "Weakening",
    "HOLD":          "Trend intact",
    "ADD_WATCH":     "Strengthening",
    "STOPPED_OUT":   "Below ATR stop",
    "STOP_HIT":      "Stop level hit",
    "TARGET_HIT":    "Target level hit",
    "TRAIL_TIGHTER": "Extended",
    "RUNNING":       "Running",
    "UNCLEAR":       "Unclear",
}
with tab_ao6:
    st.markdown(section_header("Holding posture", "six-filter momentum read"),
                unsafe_allow_html=True)
    st.caption(
        "Descriptive posture per holding using the same 55-day breakout + "
        "1.5× volume + RSI 55–75 + trend + VWAP + ATR filters the momentum "
        "scanner uses. **Not advice** — position sizing and stops are yours."
    )

    _ap_c1, _ap_c2 = st.columns([1, 1])
    with _ap_c1:
        _do_del = st.checkbox("Analyse delivery holdings", value=True, key="ap_do_del")
    with _ap_c2:
        _do_int = st.checkbox("Analyse intraday positions", value=True, key="ap_do_int")

    if st.button("Run posture analysis", type="primary", key="ap_run"):
        from analysis.portfolio_posture import (
            analyse_holdings, analyse_positions, format_portfolio_message,
        )
        from data.fetcher import fetch_single

        def _fetch(sym: str):
            try:
                return fetch_single(sym, period="2y")
            except Exception:
                return None

        _nifty = None
        try:
            _nifty = fetch_single("^NSEI", period="2y")
        except Exception:
            pass

        _hpost, _ipost, _hskip = [], [], []
        _holdings_now = []
        with st.spinner("Analysing holdings…"):
            if _do_del:
                _holdings_now = _ao_get_holdings() or []
                _hpost = analyse_holdings(_holdings_now, _fetch, nifty_history=_nifty,
                                          skipped=_hskip)
            if _do_int:
                _pos_now = _ao_get_positions() or {"day": [], "net": []}
                _open = _pos_now.get("day") or []
                # F&O contracts (NIFTY…CE etc.) have no Yahoo history; the
                # posture falls back to entry-vs-LTP, so skip the slow lookup.
                _fo = {p["symbol"] for p in _open if p.get("segment") == "F&O"}
                _ipost = analyse_positions(
                    _open, lambda s: None if s in _fo else _fetch(s))

        # Rendering: colour chip per posture, trend-broken names first
        _order = {"EXIT_WATCH": 0, "TRIM_WATCH": 1, "HOLD": 2, "ADD_WATCH": 3, "STOPPED_OUT": 4}
        _hpost.sort(key=lambda h: _order.get(h.posture, 9))

        _colour = {
            "EXIT_WATCH":  "var(--bear)",
            "TRIM_WATCH":  "var(--amber)",
            "HOLD":        "var(--azure)",
            "ADD_WATCH":   "var(--bull)",
            "STOPPED_OUT": "var(--dim)",
            "STOP_HIT":    "var(--bear)",
            "TARGET_HIT":  "var(--bull)",
            "TRAIL_TIGHTER": "var(--amber)",
            "RUNNING":     "var(--azure)",
            "UNCLEAR":     "var(--dim)",
        }

        if _hpost:
            st.markdown("#### 💼 Delivery holdings")
            for h in _hpost:
                _c = _colour.get(h.posture, "var(--azure)")
                st.markdown(
                    f'<div style="border-left:4px solid {_c}; padding:8px 12px;'
                    f' margin:6px 0; background:rgba(66,165,245,0.05)">'
                    f'<b>{h.symbol}</b> · '
                    f'<span style="color:{_c};font-weight:600">{_POSTURE_LABELS.get(h.posture, h.posture)}</span> · '
                    f'{h.filters_passing}/6 filters · P&L {h.pnl_pct:+.2f}%<br>'
                    f'<span style="color:var(--dim);font-size:0.9em">{h.reason}</span><br>'
                    f'<span style="font-size:0.85em">RSI {h.rsi} · SMA50 ₹{h.sma50:,.2f} · '
                    f'ATR ₹{h.atr:,.2f} · ATR stop ref ₹{h.suggested_stop:,.2f} · '
                    f'RS-63d {h.rs_63d:+.1f}%</span>'
                    f'</div>',
                    unsafe_allow_html=True,
                )
        elif _do_del and not _holdings_now:
            st.info("No delivery holdings in your demat account.")
        if _do_del and _hskip:
            st.info("No posture for: " + "; ".join(
                f"**{s['symbol']}** — {s['reason']}" for s in _hskip))

        if _ipost:
            st.markdown("#### ⚡ Intraday positions")
            for p in _ipost:
                _c = _colour.get(p.posture, "var(--azure)")
                st.markdown(
                    f'<div style="border-left:4px solid {_c}; padding:8px 12px;'
                    f' margin:6px 0; background:rgba(66,165,245,0.05)">'
                    f'<b>{p.symbol}</b> · '
                    f'<span style="color:{_c};font-weight:600">{_POSTURE_LABELS.get(p.posture, p.posture)}</span> · '
                    f'P&L {p.pnl_pct:+.2f}%<br>'
                    f'<span style="color:var(--dim);font-size:0.9em">{p.reason}</span>'
                    f'</div>',
                    unsafe_allow_html=True,
                )
        elif _do_int:
            st.info("No open intraday or F&O positions right now — posture is read "
                    "for open positions only; squared-off trades are on Today's positions.")

        if _hpost or _ipost:
            with st.expander("📨 Preview alert message"):
                st.code(format_portfolio_message(_hpost, _ipost), language="text")
    else:
        st.info(
            "Click **Run posture analysis** to fetch your Angel One holdings and "
            "score each name on the 6-filter momentum recipe. Fresh network call — "
            "takes ~10–30 seconds depending on holdings count."
        )
