"""dashboard/shared/nav.py - sidebar + grouped navigation (st.switch_page routing)."""
from __future__ import annotations
import os, sys, sqlite3, warnings, io, json, math, datetime
import numpy as np
import pandas as pd
import streamlit as st
# FIX WARN1 — narrowed from a blanket `filterwarnings("ignore")` so numpy's
# RuntimeWarnings (invalid value / divide by zero / all-NaN slice) stay visible.
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", category=PendingDeprecationWarning)
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
import trade_store as _store


# ── SLICE 1 · UI/UX 2026-09 · sidebar IA restructure ───────────────────────
# Design doc: docs/UI_UX_DESIGN_2026-09.md §2 · reference mockup:
# https://claude.ai/code/artifact/d630c73b-5365-4061-83f4-d4bb1cf136b5
#
# Changes vs pre-Slice-1:
#   - Trading group merged into "Tools & Ops"; Intraday moves there
#   - New "Scanners & Signals" group (Screener + Tomorrow's WL + TQS)
#   - Tomorrow's Watchlist moves Portfolio -> Scanners & Signals
#   - TQS moves Analysis -> Scanners & Signals
#   - Verdict Calibration moves Analysis -> Tools & Ops
#   - Deep Dive removed from sidebar AND folded into Analyze Stock as
#     the "🔬 Deep Dive" tab (Slice 2 landed). The old pages/20_deep_dive.py
#     is deleted; xrefs (picks_ui full-analysis button, staleness prompt
#     on Analyze Stock's PortfolioFit tab) now route to Analyze Stock.
#   - "Market Live" -> "Live Ticker"; "Overview" -> "Market Breadth"
#   - TQS emoji retagged 📊 -> 📈 to resolve the emoji collision with
#     the old "Overview" (now "Market Breadth")
# Palette shift (saffron accent, Instrument Serif type) is a later slice;
# this slice ships IA + sidebar chrome only.
_NAV_GROUPS: dict = {
    "Home":                ["Command Centre"],
    "Markets":             ["Live Ticker", "Market Breadth", "Quality Watch", "FII / DII Flows"],
    "Portfolio":           ["My Portfolio", "Paper Trades", "My Watchlist", "Stock Journal"],
    "Scanners & Signals":  ["Smart Screener", "Tomorrow's Watchlist", "Trend Quality Score"],
    "Analysis":            ["Analyze Stock", "Backtest"],
    "Tools & Ops":         ["Intraday Trader", "Position Sizer", "Angel One",
                            "Verdict Calibration", "Investor Guide"],
}

_PAGE_EMOJI: dict = {
    "Command Centre":  "🎯",
    "Live Ticker":     "📡",
    "Market Breadth":  "📊",
    "Quality Watch":   "🏆",
    "Intraday Trader": "⚡",
    "Smart Screener":  "🔎",
    "My Portfolio":    "🏠",
    "Paper Trades":    "📂",
    "My Watchlist":    "⭐",
    "Stock Journal":   "📓",
    "Tomorrow's Watchlist": "📅",
    "Analyze Stock":   "🔍",
    "Backtest":        "🧪",
    "Position Sizer":  "📐",
    "Angel One":       "🔗",
    "Investor Guide":  "📖",
    "Trend Quality Score": "📈",  # retagged from 📊 (collision with Market Breadth)
    "Verdict Calibration": "📏",
    "FII / DII Flows":     "🏦",
}

_PAGE_FULL_NAME: dict = {
    "Command Centre":  "🎯 Command Centre",
    "Live Ticker":     "📡 Live Ticker",
    "Market Breadth":  "📊 Market Breadth",
    "Quality Watch":   "🏆 Quality Watch",
    "Intraday Trader": "⚡ Intraday Trader",
    "Smart Screener":  "🔎 Smart Screener",
    "My Portfolio":    "🏠 My Portfolio",
    "Paper Trades":    "📂 Paper Trades",
    "My Watchlist":    "⭐ My Watchlist",
    "Stock Journal":   "📓 Stock Journal",
    "Tomorrow's Watchlist": "📅 Tomorrow's Watchlist",
    "Analyze Stock":   "🔍 Analyze Stock",
    "Backtest":        "🧪 Backtest",
    "Position Sizer":  "📐 Position Sizer",
    "Angel One":       "🔗 Angel One",
    "Investor Guide":  "📖 Investor Guide",
    "Trend Quality Score": "📈 Trend Quality Score",
    "Verdict Calibration": "📏 Verdict Calibration",
    "FII / DII Flows":     "🏦 FII / DII Flows",
}

_group_icons: dict = {
    "Home": "🎯", "Markets": "📊", "Portfolio": "💼",
    "Scanners & Signals": "🔎", "Analysis": "🔍", "Tools & Ops": "🛠",
}



_PAGE_FILE = {
    "Live Ticker":     "pages/01_market_live.py",   # renamed from "Market Live" — file path kept
    "Command Centre":  "pages/02_command_centre.py",
    "My Portfolio":    "pages/03_my_portfolio.py",
    "Analyze Stock":   "pages/04_analyze_stock.py",
    "Market Breadth":  "pages/05_market_overview.py",  # renamed from "Overview" — file path kept
    "Smart Screener":  "pages/06_smart_screener.py",
    "Paper Trades":    "pages/07_paper_trades.py",
    "Backtest":        "pages/08_backtest.py",
    # "Market Internals" removed — merged into "Overview" above.
    # dashboard/pages/09_market_internals.py should be DELETED from the repo;
    # its content now lives in Overview's "🌍 Macro" / "📈 Breadth" tabs.
    # "OI & Options" removed — merged into "Intraday Trader" above (FIX MERGE1).
    # dashboard/pages/10_oi_options.py should be DELETED from the repo;
    # its content now lives in Intraday Trader's "Options Strategy" /
    # "Max Pain Calculator" / "PCR Zone Reference" tabs.
    "Intraday Trader": "pages/11_intraday_trader.py",
    "Position Sizer":  "pages/12_position_sizer.py",
    "Stock Journal":   "pages/13_stock_journal.py",
    "My Watchlist":    "pages/14_my_watchlist.py",
    "Investor Guide":  "pages/15_investor_guide.py",
    "Angel One":       "pages/16_angel_one.py",
    "Tomorrow's Watchlist": "pages/17_tomorrow_watchlist.py",
    "Trend Quality Score": "pages/18_tqs_scanner.py", # <-- Added File Route Mapping
    "Quality Watch":   "pages/19_quality_watch.py",  # NEW: Long-Term Holds + Quality Watch
    # "Deep Dive Analysis" removed -- Slice 2 folded it into Analyze Stock as
    # the "🔬 Deep Dive" tab. dashboard/pages/20_deep_dive.py DELETED.
    # 2026-08-30 sprint: durable-persistence-backed learning pages
    "Verdict Calibration": "pages/21_verdict_calibration.py",
    "FII / DII Flows":     "pages/22_fii_dii_flows.py",
}


import logging as _logging
_log = _logging.getLogger("dashboard.nav")

@st.cache_data(ttl=60, show_spinner=False)

def _qv_prices(tickers: tuple) -> dict:
    """Live prices for the sidebar quick-view.

    FIX NAV2 — max_wait_seconds=10: this is called from render_sidebar(),
    which runs on EVERY page. Without this cap, get_live_prices_batch's own
    wait scales with ticker-list size and can reach 30-82+ seconds when
    Angel One isn't configured (a fast, instant "not configured" check —
    see live_price.py's FIX LP2 docstring) and Yahoo/NSE/Stooq are degraded
    (which happens routinely on Streamlit Cloud's shared IPs). That meant
    the ENTIRE APP could freeze for the better part of a minute on any page,
    any time this 60s cache went cold.

    FIX SPEED-DEV1 (2026-09-15):
      • `NSE_SIDEBAR_QV_OFF=1` opt-out returns {} instantly, skipping the
        batch entirely — for local dev where you don't need the quick-view.
      • `NSE_QV_MAX_WAIT_SECONDS` overrides the wait cap (default drops
        from 10 -> 4s; fast networks stay under, slow paths degrade to
        "unavailable" instead of freezing every navigation).
    """
    if os.environ.get("NSE_SIDEBAR_QV_OFF", "").lower() in ("1", "true", "yes"):
        return {}
    _wait = 4
    try:
        _wait = int(os.environ.get("NSE_QV_MAX_WAIT_SECONDS", "4"))
    except (TypeError, ValueError):
        _wait = 4
    try:
        from utils.live_price import get_live_prices_batch
        raw = get_live_prices_batch(list(tickers), max_wait_seconds=_wait)
    except Exception as _e:
        _log.debug("nav.%s degraded: %s", "_qv_prices", _e)
        raw = {}
    res = {}
    for t in tickers:
        q = raw.get(t)
        if isinstance(q, dict) and q.get("price"):
            res[t] = {"price": q["price"], "prev": q["prev_close"], "chg": q["chg_pct"]}
    return res

@st.cache_data(ttl=600, show_spinner=False)

def _sidebar_all():
    """Fetch VIX + 4 macro instruments concurrently. Returns in <10s or gives up.

    Cloud-safe: uses Yahoo Finance JSON API directly (no yfinance library),
    which avoids the rate-limiting that hits yf.download() from datacenter IPs.
    """
    from utils.live_price import _yahoo_json_quote
    from concurrent.futures import ThreadPoolExecutor, wait as _wait

    symbols = {
        "^INDIAVIX": "vix",
        "^NSEI":     "Nifty",
        "^NSEBANK":  "BNifty",
        "GC=F":      "Gold",
        "BZ=F":      "Crude",
    }
    results = {}
    pool = ThreadPoolExecutor(max_workers=5)
    try:
        futs = {pool.submit(_yahoo_json_quote, sym): (sym, name)
                for sym, name in symbols.items()}
        done, _ = _wait(list(futs.keys()), timeout=12)
        for fut in done:
            sym, name = futs[fut]
            try:
                q = fut.result(timeout=0)
                if q:
                    results[name] = q   # {"price": float, "prev_close": float}
            except Exception as _e:
                _log.debug("nav.%s degraded: %s", "_sidebar_all", _e)
                pass
    finally:
        pool.shutdown(wait=False)

    # Parse VIX
    vix_data = (None, None, "Unknown", "⚪")
    if "vix" in results:
        try:
            import math as _m
            val = results["vix"]["price"]
            prev= results["vix"]["prev_close"]
            chg = (val / prev - 1) * 100 if prev > 0 else 0.0
            if _m.isnan(val) or val <= 0:
                raise ValueError("VIX NaN")
            if val < 16:   reg, col = "Normal",   "🟢"
            elif val < 22: reg, col = "Elevated",  "🟡"
            elif val < 28: reg, col = "Fear",      "🔴"
            else:          reg, col = "PANIC",     "🔴"
            vix_data = (val, chg, reg, col)
        except Exception as _e:
            _log.debug("nav.%s degraded: %s", "_sidebar_all", _e)
            pass

    # Parse macro pulse
    pulse = {}
    dp_map = {"Nifty": 0, "BNifty": 0, "Gold": 1, "Crude": 2}
    for name in ("Nifty", "BNifty", "Gold", "Crude"):
        if name in results:
            try:
                c  = results[name]["price"]
                pc = results[name]["prev_close"]
                pulse[name] = (c, (c / pc - 1) * 100 if pc > 0 else 0.0, dp_map[name])
            except Exception as _e:
                _log.debug("nav.%s degraded: %s", "_sidebar_all", _e)
                pass

    return vix_data, pulse

def _persist_user_state():
    """Save watchlist + sizing settings to the store if they changed since last save."""
    try:
        import trade_store as _ts_p
        _snap = (
            tuple(st.session_state.get("watchlist", [])),
            st.session_state.get("trade_capital"),
            st.session_state.get("risk_pct"),
        )
        if st.session_state.get("_user_state_snapshot") != _snap:
            _ok = all([
                _ts_p.kv_set("watchlist", list(st.session_state.get("watchlist", []))),
                _ts_p.kv_set("trade_capital", st.session_state.get("trade_capital", 500_000)),
                _ts_p.kv_set("risk_pct", st.session_state.get("risk_pct", 1.0)),
            ])
            # Only advance the snapshot if the writes actually persisted, so a transient
            # failure retries next run instead of being silently dropped.
            if _ok:
                st.session_state["_user_state_snapshot"] = _snap
                st.session_state.pop("_persist_failed", None)
            else:
                st.session_state["_persist_failed"] = True
    except Exception as e:
        import logging as _lg
        _lg.getLogger("nav").warning("user-state persist failed: %s", e)
        st.session_state["_persist_failed"] = True

@st.cache_data(ttl=60, show_spinner=False)

def _watchlist_prices(tickers_tuple: tuple) -> dict:
    # FIX NAV2 — max_wait_seconds=10: same reasoning as _qv_prices above.
    # This is the third of three separate sidebar price fetches that all
    # used to be able to block up to 30-82+ seconds each with no cap.
    from utils.live_price import get_live_prices_batch
    raw = get_live_prices_batch(list(tickers_tuple), max_wait_seconds=10)
    out = {}
    for t in tickers_tuple:
        q = raw.get(t)
        if isinstance(q, dict) and q.get("price"):
            out[t] = q
    return out


def _qv_holding_field(row, *names, default=0):
    """Pull the first present/non-None attribute from `row` across a list of
    possible column-name spellings. Manual holdings storage may use slightly
    different field names than the old portfolio.csv did (e.g. 'qty' vs
    'quantity'); this keeps the sidebar resilient to either."""
    for n in names:
        v = getattr(row, n, None)
        if v is not None:
            return v
    return default


def _cmdbar_match(query: str) -> list[tuple[str, str, str]]:
    """Fuzzy-search across pages and (best-effort) NSE tickers.

    Returns up to 8 tuples of (display, kind, target) where:
      kind = "page"   → target is a nav page name (feeds _nav_to)
      kind = "ticker" → target is a symbol (e.g. "RELIANCE"), the caller
                        routes to Analyze Stock with the ticker preloaded.

    Case-insensitive substring match. Pages ranked first, tickers after.
    Ticker lookup falls back silently if the search map cannot be
    imported (Analyze Stock has its own fallback anyway).
    """
    q = (query or "").strip().lower()
    if not q or len(q) < 2:
        return []

    matches: list[tuple[str, str, str]] = []
    for _grp, _ps in _NAV_GROUPS.items():
        for _p in _ps:
            hay = f"{_p} {_PAGE_FULL_NAME.get(_p, _p)}".lower()
            if q in hay:
                matches.append(
                    (f"{_PAGE_EMOJI.get(_p, '·')} {_p}",
                     "page", _p)
                )
                if len(matches) >= 4:
                    break
        if len(matches) >= 4:
            break

    try:
        from dashboard.shared.cache import STOCK_SEARCH_MAP as _SSM
    except Exception:
        _SSM = {}
    _ticker_hits = 0
    for _name, _sym in _SSM.items():
        if _ticker_hits >= 4:
            break
        if q in _name.lower() or q in str(_sym).lower():
            _clean = str(_sym).replace(".NS", "")
            matches.append(
                (f"🔍 Analyze {_clean} · {_name}",
                 "ticker", _clean)
            )
            _ticker_hits += 1

    return matches[:8]


def _cmdbar_navigate(kind: str, target: str, q_state_key: str) -> None:
    """Shared post-click nav — used by both the sidebar bar and the ⌘K modal.

    Clears the caller's query-input state key so the box comes back empty on the
    landing page, then routes: "page" → st.switch_page, "ticker" → set
    analyze_ticker + switch to Analyze Stock.
    """
    st.session_state[q_state_key] = ""
    if kind == "page":
        _t = _PAGE_FILE.get(target)
        if _t:
            st.switch_page(_t)
    elif kind == "ticker":
        st.session_state["analyze_ticker"] = f"{target}.NS"
        st.session_state["_goto_page"] = _PAGE_FULL_NAME.get(
            "Analyze Stock", "🔍 Analyze Stock")
        _t = _PAGE_FILE.get("Analyze Stock")
        if _t:
            st.switch_page(_t)


def _render_command_bar() -> None:
    """Sidebar command bar (§10 UX ideas: command-palette lite).

    Text input at the top of the sidebar. Two chars triggers matching;
    matched pages/tickers appear as buttons that navigate on click.
    No JS/HTML — native Streamlit only, so it works inside AppTest and
    doesn't need injected JS. Paired with _render_ctrlk_palette()
    below which upgrades this to a Ctrl+K modal on real browsers.
    """
    q = st.sidebar.text_input(
        "🔎 Jump to…",
        key="__cmdbar_q",
        placeholder="page or ticker (min 2 chars)",
        label_visibility="collapsed",
    )
    hits = _cmdbar_match(q)

    # When the box is empty and the user has previously analyzed some
    # tickers this session, surface them as quick-jump buttons. Writing
    # side lives on Analyze Stock (04_analyze_stock.py) — see
    # st.session_state['__recent_tickers'].
    if not hits and not (q or "").strip():
        _recent = st.session_state.get("__recent_tickers", [])
        if _recent:
            st.sidebar.caption("Recent")
            for _sym in _recent[:5]:
                if st.sidebar.button(
                    f"🔍 {_sym}", key=f"__cmdbar_recent_{_sym}",
                    use_container_width=True,
                ):
                    _cmdbar_navigate("ticker", _sym, "__cmdbar_q")
        return
    if not hits:
        return

    for _display, _kind, _target in hits:
        if st.sidebar.button(
            _display, key=f"__cmdbar_{_kind}_{_target}",
            use_container_width=True,
        ):
            _cmdbar_navigate(_kind, _target, "__cmdbar_q")


# ── UX1 · Ctrl+K command palette ─────────────────────────────────────────────
# Modal upgrade of the sidebar command bar. On real browsers, Ctrl/Cmd+K opens
# an st.dialog anywhere on the page; the sidebar bar stays as the AppTest-safe
# fallback (native widgets only, no JS). Both share _cmdbar_match + the
# _cmdbar_navigate handler, so search behaviour is identical.
#
# JS→server plumbing: a small "⌘ K" trigger button in the sidebar is what the
# keydown listener .click()s to open the dialog. Kept visible (not hidden) so
# it doubles as a discoverable affordance for touch/mouse users and works
# without a keyboard binding at all.
def _command_palette_dialog_body() -> None:
    """Body of the palette modal — invoked by the @st.dialog wrapper below.

    Kept as a standalone function (not the decorated one directly) so its logic
    can be unit-tested without opening a real Streamlit dialog runtime.
    """
    q = st.text_input(
        "Search pages and tickers",
        key="__palette_q",
        placeholder="Start typing a page name or ticker (min 2 chars)…",
        label_visibility="collapsed",
    )
    hits = _cmdbar_match(q)

    if not hits and not (q or "").strip():
        _recent = st.session_state.get("__recent_tickers", [])
        if _recent:
            st.caption("Recent")
            for _sym in _recent[:5]:
                if st.button(
                    f"🔍 {_sym}", key=f"__palette_recent_{_sym}",
                    use_container_width=True,
                ):
                    _cmdbar_navigate("ticker", _sym, "__palette_q")
        else:
            st.caption(
                "Type a page name (e.g. **portfolio**, **screener**) or an "
                "NSE ticker (e.g. **RELIANCE**). Two characters minimum."
            )
        return

    if not hits:
        st.caption("No matches. Try a different page name or ticker.")
        return

    for _display, _kind, _target in hits:
        if st.button(
            _display, key=f"__palette_{_kind}_{_target}",
            use_container_width=True,
        ):
            _cmdbar_navigate(_kind, _target, "__palette_q")


@st.dialog("Jump to…", width="large")
def _command_palette_dialog() -> None:
    _command_palette_dialog_body()


def _render_ctrlk_palette() -> None:
    """Add the ⌘K trigger to the sidebar and bind the keyboard shortcut.

    The trigger is a real, visible sidebar button — clicking it (with a mouse,
    tap, or a JS-driven .click() from the Ctrl+K listener) invokes the dialog.
    Streamlit treats a programmatic .click() the same as a user click, which
    is exactly the plumbing we need to bridge JS → Streamlit script rerun.
    """
    # Trigger button — mid-sidebar, right under the always-visible search bar.
    # Fires on real click AND on Ctrl+K (JS clicks it programmatically).
    if st.sidebar.button(
        "⌘ K  ·  Command palette",
        key="__palette_trigger",
        use_container_width=True,
        help="Open command palette (Ctrl+K / ⌘+K)",
    ):
        _command_palette_dialog()

    # Keybind — one-time listener on the parent document. Guarded by a flag on
    # window so it isn't rebound across Streamlit's iframe reloads (which fire
    # on every rerun); rebinding would cascade to N clicks on one keystroke.
    #
    # st.html(unsafe_allow_javascript=True) (Streamlit >=1.52) runs the script
    # directly in the app document (no iframe), replacing the deprecated
    # st.components.v1.html. window.parent === window there, so `doc` resolves
    # to the app document either way. The button above still works in
    # AppTest as a plain sidebar button.
    try:
        st.html(
            """
            <script>
            (function() {
                const doc = (window.parent && window.parent.document) || document;
                if (doc.__nse_ctrlk_bound) return;
                doc.__nse_ctrlk_bound = true;
                doc.addEventListener('keydown', function(e) {
                    if (!((e.ctrlKey || e.metaKey) && (e.key === 'k' || e.key === 'K'))) return;
                    // Don't hijack the shortcut while the user is inside an
                    // input, textarea, or contenteditable — leaves the browser
                    // default alone (address-bar autofill toggle, etc.) and
                    // gives an escape hatch on shortcut collisions.
                    const t = e.target;
                    const inField = t && (t.tagName === 'INPUT'
                        || t.tagName === 'TEXTAREA'
                        || t.isContentEditable);
                    if (inField) return;
                    e.preventDefault();
                    // Find the trigger button by its text — Streamlit renders
                    // the label as the button's inner text, and the ⌘ K string
                    // is unique on the page.
                    const btns = doc.querySelectorAll('button');
                    for (const b of btns) {
                        if ((b.textContent || '').trim().startsWith('⌘ K')) {
                            b.click();
                            break;
                        }
                    }
                });
            })();
            </script>
            """,
            unsafe_allow_javascript=True,
        )
    except Exception:
        # AppTest / anything that can't render JS — the
        # trigger button above still works as a plain click affordance.
        pass


def _alert_js_literal(msg: str) -> str:
    """Encode an alert message as a safe JS string literal for inline <script>.

    json.dumps handles quotes / backslashes / newlines; escaping ``</`` stops a
    message containing ``</script>`` from closing the script element early."""
    return json.dumps(str(msg)[:90]).replace("</", "<\\/")


def render_sidebar(current: str = None) -> None:
    """Render the full sidebar. `current` = this page's name (for routing)."""
    st.sidebar.title("NSE Smart Investor")
    st.sidebar.markdown("*AI-powered equity companion*")
    _render_command_bar()
    _render_ctrlk_palette()
    st.sidebar.markdown("---")

    # Two-level grouped navigation — Home + 5 sections

    # Emoji map for display

    # Restore old page key names for backward-compat with all elif checks below



    # ── Grouped navigation — single-click collapsible sections ─────────────────
    # FIX (nav): the old selectbox→radio combo needed two separate interactions
    # to reach any page outside the current section (pick a Section, THEN pick
    # a Page). Originally replaced with st.page_link inside per-section
    # expanders, but st.page_link resolves its target page's registry entry
    # (url_pathname) EAGERLY at render time — not just on click. When a page
    # is run standalone (as the CI smoke test does via AppTest.from_file on
    # one pages/*.py file at a time, with no sibling pages registered) that
    # lookup raises KeyError('url_pathname') immediately on render, before
    # any interaction. Using st.button + st.switch_page instead defers page
    # resolution until an actual click — which never happens in the smoke
    # test — while still keeping every page a single click away in the
    # sidebar. The _goto_page programmatic hook (used by buttons all over the
    # app to jump pages) and _nav_target deep-link resolution both still work
    # exactly as before.
    _cur_grp = next((_g for _g, _ps in _NAV_GROUPS.items() if current in _ps), None)

    def _nav_to(_name):
        _t = _PAGE_FILE.get(_name)
        if _t:
            st.switch_page(_t)

    # Programmatic nav: a button elsewhere set _goto_page to a full page name.
    if st.session_state.get('_goto_page'):
        _goto = st.session_state.pop('_goto_page')
        _match = next((p for _g, _ps in _NAV_GROUPS.items() for p in _ps
                       if _goto in (f'{_PAGE_EMOJI[p]} {p}', _PAGE_FULL_NAME.get(p, p))), None)
        if _match and _match != current:
            _nav_to(_match)

    for _grp, _pages in _NAV_GROUPS.items():
        _is_cur_grp = (_grp == _cur_grp)
        with st.sidebar.expander(
            f"{_group_icons[_grp]} {_grp}", expanded=_is_cur_grp,
        ):
            for _p in _pages:
                _is_cur_page = _is_cur_grp and (_p == current)
                _label = (f"**{_PAGE_EMOJI[_p]} {_p}**" if _is_cur_page else f"{_PAGE_EMOJI[_p]} {_p}")
                if st.button(
                    _label, key=f"navbtn_{_p}", use_container_width=True,
                    disabled=_is_cur_page,
                ):
                    _nav_to(_p)


    # ── Portfolio quick-view (right under the nav — value + today's P&L) ───────────
    # FIX: My Portfolio no longer uses portfolio.csv / Angel One import — holdings
    # are added/edited/deleted manually via trade_utils.load_manual_holdings() /
    # save_manual_holdings(), persisted through the kv store. This quick view was
    # still reading the old portfolio.csv path directly, so it kept showing stale
    # (or now permanently empty) data that no longer has anything to do with what's
    # actually on the My Portfolio page. Routed through load_manual_holdings()
    # instead, matching what My Portfolio / Analyze Stock already do.
    st.sidebar.markdown("---")
    with st.sidebar.expander("💼 Portfolio Quick View", expanded=True):
        try:
            from dashboard.shared.trade_utils import load_manual_holdings
            # FIX: load_manual_holdings() returns a list[dict] (same as
            # my_portfolio.py consumes it — .append()/.pop() on it directly),
            # not a DataFrame. Calling .empty/.itertuples() on the raw list
            # is what threw "'list' object has no attribute 'empty'" in the
            # sidebar. Wrap it in a DataFrame here, same as my_portfolio.py
            # does locally before handing it to PortfolioManager.
            _holdings_list = load_manual_holdings()
            _qdf = pd.DataFrame(_holdings_list) if _holdings_list else pd.DataFrame()
            if not _qdf.empty:
                _qsyms = tuple((t if str(t).endswith(".NS") else f"{t}.NS")
                               for t in _qdf["ticker"].tolist())
                _qlp = _qv_prices(_qsyms)
                _q_val = _q_today = _q_total = _q_inv = 0.0
                _q_rows = []
                for _qr in _qdf.itertuples():
                    _qsym = _qr.ticker if str(_qr.ticker).endswith(".NS") else f"{_qr.ticker}.NS"
                    _ql = _qlp.get(_qsym, {})
                    _qcur = _ql.get("price")
                    _qty  = _qv_holding_field(_qr, "quantity", "qty")
                    _qbuy = _qv_holding_field(_qr, "avg_buy_price", "avg_price", "buy_price")
                    if _qcur:
                        _q_val   += _qcur * _qty
                        _q_inv   += _qbuy * _qty
                        _q_today += (_qcur - _ql.get("prev", _qcur)) * _qty
                        _q_total += (_qcur - _qbuy) * _qty
                        _q_rows.append((str(_qr.ticker).replace(".NS",""),
                                        (_qcur/_qbuy-1)*100 if _qbuy else 0))
                _tc = "#00d4aa" if _q_today >= 0 else "#ff4757"
                _oc = "#00d4aa" if _q_total >= 0 else "#ff4757"
                _op = (_q_total/_q_inv*100) if _q_inv else 0
                st.markdown(
                    f'<div style="font-size:11px;color:#4a5568;text-transform:uppercase;letter-spacing:1px">Value</div>'
                    f'<div style="font-size:22px;font-weight:800;color:#f0f4ff">₹{_q_val:,.0f}</div>'
                    f'<div style="display:flex;gap:14px;margin-top:6px">'
                    f'<div><div style="font-size:10px;color:#4a5568">TODAY</div>'
                    f'<div style="font-size:14px;font-weight:700;color:{_tc}">{"▲" if _q_today>=0 else "▼"} ₹{abs(_q_today):,.0f}</div></div>'
                    f'<div><div style="font-size:10px;color:#4a5568">OVERALL</div>'
                    f'<div style="font-size:14px;font-weight:700;color:{_oc}">{"▲" if _q_total>=0 else "▼"} ₹{abs(_q_total):,.0f} ({_op:+.1f}%)</div></div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )
                if _q_rows:
                    _q_rows.sort(key=lambda x: -x[1])
                    _best, _worst = _q_rows[0], _q_rows[-1]
                    st.caption(f"🏆 {_best[0]} {_best[1]:+.1f}%  ·  🔻 {_worst[0]} {_worst[1]:+.1f}%")
                if st.button("📂 Open Full Portfolio", key="sb_open_portfolio", use_container_width=True):
                    st.session_state["_goto_page"] = "🏠 My Portfolio"
                    st.rerun()
            else:
                st.caption("No holdings yet. Add them on the My Portfolio page.")
        except Exception as _qe:
            st.caption(f"Quick view unavailable: {str(_qe)[:50]}")

    # ── Paper Trades quick-view ──────────────────────────────────────────────
    # NEW: mirrors the on-page "Paper Trades Overview" panel (open count +
    # unrealised P&L on live prices), with a one-click jump to the full page.
    with st.sidebar.expander("📂 Paper Trades Quick View", expanded=True):
        try:
            _pt_open = _store.fetch_open()
            _pt_n = 0 if (_pt_open is None or _pt_open.empty) else len(_pt_open)
            _pt_unreal = 0.0
            if _pt_n:
                _pt_syms = tuple(_pt_open["ticker"].tolist())
                _pt_lp = _qv_prices(_pt_syms)
                for _, _ptr in _pt_open.iterrows():
                    _pt_ep  = float(_ptr.get("price", 0) or 0)
                    _pt_qty = int(_ptr.get("quantity", 0) or 0)
                    _pt_cur = _pt_lp.get(str(_ptr["ticker"]), {}).get("price", _pt_ep)
                    _pt_unreal += (_pt_cur - _pt_ep) * _pt_qty
            _puc = "#00d4aa" if _pt_unreal >= 0 else "#ff4757"
            st.markdown(
                f'<div style="font-size:11px;color:#4a5568;text-transform:uppercase;letter-spacing:1px">Open Positions</div>'
                f'<div style="font-size:22px;font-weight:800;color:#f0f4ff">{_pt_n}</div>'
                f'<div style="margin-top:6px">'
                f'<div style="font-size:10px;color:#4a5568">UNREALISED P&amp;L</div>'
                f'<div style="font-size:14px;font-weight:700;color:{_puc}">'
                f'{"▲" if _pt_unreal>=0 else "▼"} ₹{abs(_pt_unreal):,.0f}</div></div>',
                unsafe_allow_html=True,
            )
            if not _pt_n:
                st.caption("No open paper positions.")
            if st.button("📂 Open Paper Trades", key="sb_open_papertrades", use_container_width=True):
                st.session_state["_goto_page"] = "📂 Paper Trades"
                st.rerun()
        except Exception as _pte:
            st.caption(f"Quick view unavailable: {str(_pte)[:50]}")

    st.sidebar.markdown("---")

    # ── Sidebar live data — fetched in parallel with a hard 12-second timeout ────
    try:
        _vix_data, _pulse = _sidebar_all()
        vix_val, vix_chg, vix_reg, vix_col = _vix_data
    except Exception as _e:
        _log.debug("nav.%s degraded: %s", "render_sidebar", _e)
        vix_val, vix_chg, vix_reg, vix_col = None, None, "Unknown", "⚪"
        _pulse = {}

    if vix_val:
        chg_str = f"{vix_chg:+.1f}%"
        st.sidebar.markdown(
            f"**Market Fear Gauge (VIX)**  \n"
            f"{vix_col} **{vix_val:.2f}** ({chg_str})  \n"
            f"Regime: **{vix_reg}**"
        )
    else:
        st.sidebar.markdown("VIX: *—*")

    for name, (price, chg, dp) in _pulse.items():
        clr   = "#6faa84" if chg >= 0 else "#c77070"
        arrow = "▲" if chg >= 0 else "▼"
        st.sidebar.markdown(
            f'<span style="font-size:11px"><b>{name}</b> '
            f'{price:,.{dp}f} '
            f'<span style="color:{clr}">{arrow}{abs(chg):.1f}%</span></span>',
            unsafe_allow_html=True,
        )

    st.sidebar.markdown("---")

    # ── Market status indicator ────────────────────────────────────────────────────
    try:
        from utils.market_hours import market_status as _mstatus
        _ms = _mstatus()
        st.sidebar.markdown(
            f"**NSE Market**  \n"
            f"{_ms['color']} **{_ms['status']}**  \n"
            f"<span style='font-size:11px'>{_ms['time_ist']} · {_ms['detail']}</span>",
            unsafe_allow_html=True,
        )
        if _ms["is_open"]:
            if st.sidebar.button("🔄 Refresh Prices", key="sidebar_refresh"):
                # FIX NAV1: was a blanket st.cache_data.clear() — this runs on
                # EVERY page (nav.py is loaded sidebar-wide), so clicking it
                # from anywhere silently wiped Command Centre's Top Picks
                # cache, the watchlist scan, and every other page's cached
                # data too, forcing expensive unrelated re-scans. "Refresh
                # Prices" only means this sidebar's own price helpers.
                _qv_prices.clear()
                _sidebar_all.clear()
                _watchlist_prices.clear()
                st.rerun()
    except Exception as _e:
        _log.debug("nav.%s degraded: %s", "render_sidebar", _e)
        pass

    # ── Angel One connection status ───────────────────────────────────────────────
    try:
        from data.angel_fetcher import is_configured as _ao_configured
        _ao_on = _ao_configured()
        if _ao_on:
            st.sidebar.markdown(
                '<span class="ao-badge-on">🔗 Angel One <b>Connected</b></span>',
                unsafe_allow_html=True,
            )
        else:
            st.sidebar.markdown(
                '<span class="ao-badge-off">🔗 Angel One  <b>Not connected</b> '
                '— go to Tools › Angel One to set up</span>',
                unsafe_allow_html=True,
            )
    except Exception as _e:
        _log.debug("nav.%s degraded: %s", "render_sidebar", _e)
        _ao_on = False

    st.sidebar.markdown("---")

    # ── Personal Watchlist ────────────────────────────────────────────────────────
    st.sidebar.markdown("#### 👀 My Watchlist")

    # Initialise watchlist in session state with 5 popular defaults
    # Initialise persisted user state (watchlist + sizing settings) from the store
    # once per session, so they survive page refreshes (and redeploys on Postgres).
    if "_user_state_loaded" not in st.session_state:
        try:
            import trade_store as _ts_init
            _saved_wl = _ts_init.kv_get("watchlist", None)
            st.session_state["watchlist"] = _saved_wl if _saved_wl else [
                "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "INFY.NS", "SBIN.NS"
            ]
            st.session_state["trade_capital"] = float(_ts_init.kv_get("trade_capital", 500_000))
            st.session_state["risk_pct"]      = float(_ts_init.kv_get("risk_pct", 1.0))
        except Exception as _e:
            _log.warning("nav.%s storage failure: %s", "_on_group_change", _e)
            st.session_state.setdefault("watchlist",
                ["RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "INFY.NS", "SBIN.NS"])
        st.session_state["_user_state_loaded"] = True

    if "watchlist" not in st.session_state:
        st.session_state["watchlist"] = [
            "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "INFY.NS", "SBIN.NS"
        ]

    # FIX NAV3 — the "Add ticker" box never cleared after adding a symbol:
    # st.text_input's value="" argument only seeds the initial value the
    # first time a key is created. Once "wl_add_input" exists in
    # session_state (which it does after the very first render), Streamlit
    # displays whatever's in session_state[key] on every subsequent rerun,
    # ignoring the value="" argument entirely — so the typed ticker just
    # sat there after clicking "＋". Directly assigning
    # st.session_state["wl_add_input"] = "" right after the button click
    # isn't allowed either — Streamlit raises "cannot be modified after the
    # widget ... is instantiated" because the text_input has already been
    # created earlier in this same script run. The fix (same deferred-flag
    # + st.rerun() pattern already used for the search boxes in
    # dashboard/pages/04_analyze_stock.py's "_as_clear_pending" flag): set
    # a plain flag when "＋" is clicked, then consume it up here, before the
    # widget is instantiated on the *next* run.
    if st.session_state.pop("_wl_add_clear_pending", False):
        st.session_state["wl_add_input"] = ""

    _wl_add_col, _wl_btn_col = st.sidebar.columns([3, 1])
    with _wl_add_col:
        _wl_input = st.text_input(
            "Add ticker", value="", placeholder="e.g. WIPRO",
            label_visibility="collapsed", key="wl_add_input"
        ).strip().upper()
    with _wl_btn_col:
        st.write("")
        _wl_add_clicked = st.button("＋", key="wl_add_btn", use_container_width=True)

    if _wl_add_clicked and _wl_input:
        _sym = _wl_input if _wl_input.endswith(".NS") else f"{_wl_input}.NS"
        if _sym not in st.session_state["watchlist"] and len(st.session_state["watchlist"]) < 12:
            st.session_state["watchlist"].append(_sym)
        # Clear the box regardless of whether the add actually happened
        # (duplicate ticker / list already at its 12-symbol cap) — the
        # person is done with this input either way once they've clicked
        # the button, same as the analyze-stock search boxes.
        st.session_state["_wl_add_clear_pending"] = True
        st.rerun()

    # Fetch live prices for watchlist tickers
    _wl_tickers = tuple(st.session_state["watchlist"])
    try:
        _wl_prices = _watchlist_prices(_wl_tickers)
    except Exception as _e:
        _log.debug("nav.%s degraded: %s", "render_sidebar", _e)
        _wl_prices = {}

    _wl_to_remove = None
    for _wl_sym in list(st.session_state["watchlist"]):
        _wl_q   = _wl_prices.get(_wl_sym)
        _wl_label = _wl_sym.replace(".NS", "")
        _wl_c1, _wl_c2, _wl_c3 = st.sidebar.columns([2, 2, 1])
        _wl_c1.markdown(f"<span style='font-size:12px;font-weight:700'>{_wl_label}</span>",
                        unsafe_allow_html=True)
        if _wl_q:
            _wl_p   = _wl_q["price"]
            _wl_chg = _wl_q.get("chg_pct", 0.0)
            _wl_clr = "#6faa84" if _wl_chg >= 0 else "#c77070"
            _wl_arr = "▲" if _wl_chg >= 0 else "▼"
            _wl_c2.markdown(
                f'<span style="font-size:11px">₹{_wl_p:,.1f} '
                f'<span style="color:{_wl_clr}">{_wl_arr}{abs(_wl_chg):.1f}%</span></span>',
                unsafe_allow_html=True,
            )
        else:
            _wl_c2.markdown('<span style="font-size:11px;color:#777">—</span>',
                            unsafe_allow_html=True)
        if _wl_c3.button("✕", key=f"wl_rm_{_wl_sym}", use_container_width=True):
            _wl_to_remove = _wl_sym

    if _wl_to_remove and _wl_to_remove in st.session_state["watchlist"]:
        st.session_state["watchlist"].remove(_wl_to_remove)
        st.rerun()

    # ── Notification bell (sidebar — visible on every page) ───────────────────────
    _notifs = []
    try:
        import trade_store as _nb
        _nb_open = _nb.fetch_open()
        if _nb_open is not None and not _nb_open.empty:
            _nb_syms = tuple(_nb_open["ticker"].tolist())
            _nb_lp = _qv_prices(_nb_syms)
            for _, _nr in _nb_open.iterrows():
                _ncur = _nb_lp.get(str(_nr["ticker"]), {}).get("price")
                if _ncur is None:
                    continue
                _nsl = float(_nr.get("sl", 0) or 0)
                _ntp = float(_nr.get("tp", 0) or 0)
                _nt = str(_nr["ticker"]).replace(".NS", "")
                if _ntp and _ncur >= _ntp:
                    _notifs.append(("🎯", f"{_nt} hit target ₹{_ntp:,.2f}", "#00d4aa"))
                elif _nsl and _ncur <= _nsl:
                    _notifs.append(("🚨", f"{_nt} hit stop ₹{_nsl:,.2f}", "#ff4757"))
    except Exception as _e:
        _log.debug("nav.%s degraded: %s", "render_sidebar", _e)
        pass
    try:
        from utils.vix import get_india_vix_regime as _nb_vix
        _nvr = _nb_vix().get("regime", "normal")
        if _nvr in ("fear", "panic"):
            # CC-COPY: descriptive only, no instruction (CLAUDE.md rule 1).
            _notifs.append(("🔴", f"India VIX in the {_nvr} zone", "#ff4757"))
        elif _nvr == "complacency":
            _notifs.append(("😴", "India VIX unusually low (complacency zone)", "#c88a3f"))
    except Exception as _e:
        _log.debug("nav.%s degraded: %s", "render_sidebar", _e)
        pass

    st.sidebar.markdown("---")
    _nb_count = len(_notifs)
    _nb_color = "#ff4757" if any(c == "#ff4757" for _, _, c in _notifs) else ("#00d4aa" if _nb_count else "#4a5568")
    with st.sidebar.expander(f"🔔 Notifications ({_nb_count})", expanded=_nb_count > 0):
        if _notifs:
            for _ic, _msg, _col in _notifs:
                st.markdown(
                    f'<div style="border-left:3px solid {_col};background:rgba(255,255,255,.02);'
                    f'border-radius:6px;padding:7px 11px;margin:4px 0;font-size:12px;color:#d0d0d0">'
                    f'{_ic} {_msg}</div>', unsafe_allow_html=True)
            if st.button("🎯 Go to Command Centre", key="nb_goto_cc", use_container_width=True):
                st.session_state["_goto_page"] = "🎯 Command Centre"
                st.rerun()
        else:
            st.caption("✅ All clear — no positions at SL/TP, market calm.")

    # ── Sound + desktop alert on a NEW SL/TP hit (fires once per new alert) ────────
    try:
        _sltp = [m for _i, m, _c in _notifs if "hit target" in m or "hit stop" in m]
        _alert_key = "|".join(sorted(_sltp))
        if _sltp and st.session_state.get("_last_alert_key") != _alert_key:
            st.session_state["_last_alert_key"] = _alert_key
            _amsg = _alert_js_literal(_sltp[0])
            st.html(
                f"""<script>
                try {{
                    var ctx = new (window.AudioContext || window.webkitAudioContext)();
                    [880, 1175].forEach(function(f, i) {{
                        var o = ctx.createOscillator(), g = ctx.createGain();
                        o.frequency.value = f; o.connect(g); g.connect(ctx.destination);
                        g.gain.setValueAtTime(0.0001, ctx.currentTime + i*0.18);
                        g.gain.exponentialRampToValueAtTime(0.25, ctx.currentTime + i*0.18 + 0.02);
                        g.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + i*0.18 + 0.16);
                        o.start(ctx.currentTime + i*0.18); o.stop(ctx.currentTime + i*0.18 + 0.17);
                    }});
                }} catch(e) {{}}
                try {{
                    var show = function() {{ new Notification("📈 NSE Smart Investor", {{ body: {_amsg} }}); }};
                    if (window.Notification) {{
                        if (Notification.permission === "granted") show();
                        else if (Notification.permission !== "denied")
                            Notification.requestPermission().then(function(p) {{ if (p === "granted") show(); }});
                    }}
                }} catch(e) {{}}
                </script>""",
                unsafe_allow_javascript=True,
            )
    except Exception as _e:
        _log.debug("nav.%s degraded: %s", "render_sidebar", _e)
        pass

    # ── Position-sizing settings (drive all suggested quantities) ─────────────────
    st.sidebar.markdown("---")
    with st.sidebar.expander("⚙️ Position Sizing", expanded=False):
        st.caption("Used to suggest how many shares to buy on every Paper Trade button.")
        st.session_state["trade_capital"] = st.number_input(
            "Trading capital (₹)", min_value=10_000, max_value=100_000_000,
            value=int(st.session_state.get("trade_capital", 500_000)), step=10_000,
            key="sb_trade_capital",
            help="Your total trading capital. Suggested quantity is sized against this.",
        )
        st.session_state["risk_pct"] = st.slider(
            "Risk per trade (%)", min_value=0.25, max_value=5.0,
            value=float(st.session_state.get("risk_pct", 1.0)), step=0.25,
            key="sb_risk_pct",
            help="Max % of capital you're willing to lose if the stop-loss is hit. "
                 "1% is the common rule.",
        )
        _eg = st.session_state["trade_capital"] * st.session_state["risk_pct"] / 100
        st.caption(f"→ Risking up to **₹{_eg:,.0f}** per trade.")

    # Persist watchlist + sizing settings whenever they change (survives refresh)
    _persist_user_state()

    # ── Storage backend badge + startup persistence validation (P1) ──────────────
    try:
        import trade_store as _ts_badge
        # Validate once per session (cheap; cached in session_state).
        _pv = st.session_state.get("_persistence_status")
        if _pv is None:
            _pv = _ts_badge.validate_persistence()
            st.session_state["_persistence_status"] = _pv
        if not _pv.get("reachable"):
            st.sidebar.error("🔴 Storage unreachable — data will NOT be saved. "
                             + (_pv.get("error") or ""))
        elif _pv.get("ephemeral"):
            st.sidebar.caption("🟡 Paper trades & watchlist: local SQLite — **resets on "
                               "redeploy**. Set DATABASE_URL to persist (docs/DEPLOYMENT_CHECKLIST.md).")
        else:
            st.sidebar.caption("🟢 Paper trades & watchlist: cloud DB (persistent)")
        if st.session_state.get("_persist_failed"):
            st.sidebar.warning("⚠️ Last settings save did not persist — check storage.")
    except Exception as e:
        import logging as _lg
        _lg.getLogger("nav").warning("persistence badge failed: %s", e)
        st.sidebar.caption("⚠️ Persistence status unknown — see logs.")

    st.sidebar.markdown("---")
    st.sidebar.markdown(
        "⚠️ *For educational use only.*  \n"
        "Not SEBI registered advice.  \n"
        "Past performance ≠ future results."
    )
