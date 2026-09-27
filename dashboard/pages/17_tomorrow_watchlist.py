"""Tomorrow's Watchlist - NSE Smart Investor (next-session EOD setups).

FIXES applied in this revision
───────────────────────────────
W1  Cold-start page freeze — the EOD scan no longer runs synchronously inside
    a blocking st.spinner() in the render path. On a cache miss, the page now:
      1. Shows the last cached/stale result immediately (if one exists in the
         kv store from a previous session), tagged with a "stale" badge.
      2. Kicks off the scan in a background thread via the same pattern used
         in backtest.py (ThreadPoolExecutor + session_state progress flags).
      3. Polls every 3 s and swaps in the fresh result once the scan
         completes, without ever blocking the UI thread for 2 minutes.
    If there is no stale result available at all (very first run, e.g. fresh
    deploy), a short blocking scan is unavoidable — but this is now flagged
    to the user with an honest "first run" message rather than a generic
    spinner.

W2  Paper-trade reason string no longer silently truncates the headline.
    reason now uses the full headline; the dashboard.shared.trade_utils
    storage layer is responsible for any column-width truncation, and if it
    does truncate, the truncation will be on the DB layer (consistent),
    not duplicated/hidden here. We additionally add an ellipsis-safe local
    helper for the CARD display copy only (not the stored reason).

LAYOUT (mockup artboard 03, 2026-09-12 "proposed layout" artifact)
────────────────────────────────────────────────────────────────────
Gate strip (scan coverage · shortlist · bucket split · 5-day follow-through ·
regime) → previous session's picks as follow-through cards → one ranked
table per bucket with rank chips, plus a single detail card with actions for
the selected setup instead of a long card list.

W4  The follow-through strip had three bugs, fixed with the move to the kit:
      * it read an `action` column the ledger does not have (it stores
        `composite_action`), so every card said "logged";
      * ledger returns are stored in PERCENT, but were treated as fractions
        (a 3% move rendered as +300%, and the ±2% band was really ±0.02%);
      * it picked the newest ledger rows, which are today's own picks with no
        forward data yet, and judged every pick as long-only, so a breakdown
        name that fell read as "Stopped".
"""

import os, sys
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import html
import math
import time
import threading

import streamlit as st
from dashboard.shared.design import apply_design
from dashboard.shared.nav import render_sidebar
from dashboard.shared.chart_helpers import render_top_bar
from dashboard.shared.cache import (
    _tomorrow_watchlist, get_tomorrow_watchlist, get_display_name,
    get_tw_ledger, get_regime_snapshot, _picks_live_prices,
)
from dashboard.shared.trade_utils import _display_label, _paper_trade_popover
from dashboard.shared.flags_ui import render_flag_badge_html  # QF2: shortlist-only flag badge
from dashboard.shared.ui_components import (
    chip_pill, chip_delta, chip_tag, empty_state, fmt_inr,
    section_header, gate_strip, rank_chip, signal_table, follow_card,
)

apply_design()
render_sidebar(current="Tomorrow's Watchlist")
render_top_bar()

# ─────────────────────────────────────────────────────────────────────────────
st.markdown('<h1 class="page-title-serif">Tomorrow\'s <em>Watchlist</em></h1>', unsafe_allow_html=True)
st.markdown(
    '<p class="page-subtitle">Next-session setups from today\'s close: breakouts setting up, '
    'breakdown risks and divergence candidates. Separate from intraday Top Picks.</p>',
    unsafe_allow_html=True,
)

# ─────────────────────────────────────────────────────────────────────────────
# FIX W1 — non-blocking scan with stale-while-revalidate pattern
# ─────────────────────────────────────────────────────────────────────────────
_EMPTY_WL = {
    "breakout_candidates": [], "breakdown_watch": [], "reversal_watch": [],
    "scan_time": "—",
}

if "tw_running" not in st.session_state:
    st.session_state["tw_running"] = False
if "tw_result" not in st.session_state:
    st.session_state["tw_result"] = None
if "tw_stale" not in st.session_state:
    st.session_state["tw_stale"] = False


def _tw_worker(result_holder: list):
    """Background worker — runs the (potentially slow) EOD scan once.
    FIX W-SPEED: calls get_tomorrow_watchlist() (snapshot-aware) rather than
    _tomorrow_watchlist() directly, so a fresh pre-warmed snapshot (written
    by scripts/warm_tomorrow_watchlist.py) is read instantly instead of this
    page unconditionally re-running its own live scan."""
    try:
        _r = get_tomorrow_watchlist()
        result_holder.append(("ok", _r))
    except Exception as _e:
        result_holder.append(("error", str(_e)))


# Try a fast, already-cached call first. Streamlit's own @st.cache_data inside
# _tomorrow_watchlist means this is instant if the cache is warm, and will
# raise/slow only on a true cold start — which we now detect and offload.
_wl = None
_cache_was_cold = False

if not st.session_state["tw_running"]:
    # Probe: try the cached function with a very short patience budget by
    # running it in a thread with a timeout. If it returns fast (cache hit),
    # great. If it's still running after ~0.3s, treat as cold and switch to
    # the background-scan UI instead of blocking.
    _probe_holder = []
    _probe_thread = threading.Thread(target=_tw_worker, args=(_probe_holder,), daemon=True)
    _probe_thread.start()
    _probe_thread.join(timeout=0.3)

    if _probe_thread.is_alive():
        # Cold cache — scan is genuinely running. Don't block; hand off to
        # the background-run UI below and keep this thread alive in session.
        _cache_was_cold = True
        st.session_state["tw_running"]      = True
        st.session_state["tw_bg_holder"]    = _probe_holder
        st.session_state["tw_bg_thread"]    = _probe_thread
        st.session_state["tw_scan_started"] = time.time()
    else:
        # Cache was warm — we already have the result
        _status, _payload = _probe_holder[0] if _probe_holder else ("error", "no result")
        if _status == "ok":
            _wl = _payload
            st.session_state["tw_result"] = _wl
            st.session_state["tw_stale"]  = False
        else:
            st.error(f"Watchlist scan unavailable: {_payload}")
            _wl = _EMPTY_WL

# ── If a background scan is in progress, show stale data + progress ───────
# FIX W3 (perf) — this used to poll with a blocking time.sleep(3) followed by
# st.rerun(), which restarts the ENTIRE page (sidebar, design, every import)
# every 3 seconds for the full ~2 min of a cold scan — the same anti-pattern
# Command Centre's Top Picks section used to have. Replaced with the same fix:
# an @st.fragment(run_every=3s). Streamlit reruns just this fragment on its
# own timer — no blocking sleep on the main thread, no re-executing the rest
# of the page each tick. When the background scan actually finishes, the
# fragment calls a plain st.rerun() (full-app scope by default, even from
# inside a fragment) exactly once, to escape the fragment and render the
# final cards below with the fresh data.
if st.session_state["tw_running"]:

    @st.fragment(run_every=3)
    def _tw_poll_fragment():
        _holder  = st.session_state.get("tw_bg_holder", [])
        _started = st.session_state.get("tw_scan_started", time.time())
        _elapsed = time.time() - _started

        if _holder:
            # Background scan finished
            _status, _payload = _holder[0]
            st.session_state["tw_running"] = False
            if _status == "ok":
                st.session_state["tw_result"] = _payload
                st.session_state["tw_stale"]  = False
            else:
                st.error(f"Watchlist scan unavailable: {_payload}")
                st.session_state["tw_stale"] = st.session_state["tw_result"] is not None
            st.rerun()
        else:
            # Still running — show what we have (stale or empty) plus a live
            # banner. No sleep needed: run_every=3 handles the next check.
            _prior = st.session_state.get("tw_result")
            if _prior is not None:
                st.info(
                    f"🔄 Refreshing today's scan in the background "
                    f"({_elapsed:.0f}s elapsed) — showing the **previous session's** "
                    "results below until the new scan completes."
                )
                st.session_state["tw_stale"] = True
            else:
                st.warning(
                    f"🔄 **First run** — scanning the full NSE universe for the first "
                    f"time ({_elapsed:.0f}s elapsed, typically ~2 min). This only "
                    "happens once; subsequent visits use the cache. You can leave this "
                    "tab open or come back shortly."
                )

    _tw_poll_fragment()
    _wl = st.session_state.get("tw_result") or _EMPTY_WL

# Fallback safety net
if _wl is None:
    _wl = st.session_state.get("tw_result") or _EMPTY_WL

_is_stale = st.session_state.get("tw_stale", False)

_brk_items = _wl.get("breakout_candidates", []) or []
_bdn_items = _wl.get("breakdown_watch", []) or []
_rev_items = _wl.get("reversal_watch", []) or []


# ─────────────────────────────────────────────────────────────────────────────
# Small helpers
# ─────────────────────────────────────────────────────────────────────────────
def _num(v):
    """float(v) when finite, else None."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _px(v) -> str:
    f = _num(v)
    if not f or f <= 0:
        return "–"
    return "₹" + fmt_inr(f, 2 if f < 1000 else 0)


_BEARISH_ACTIONS = frozenset({"EXIT", "CAUTION", "AVOID", "SELL", "STRONG SELL"})
_MOVE_BAND = 2.0   # % move that counts as following through / against the setup


def _setup_direction(action, score) -> int:
    """+1 for a bullish setup, -1 for a bearish one.

    Mirrors the breakdown gate in cache._tomorrow_watchlist (EXIT/CAUTION or
    score < 40). The ledger does not record which bucket a pick came from, so
    bearish-divergence names from the reversal bucket read as bullish here;
    the strip says "direction from logged posture" so the basis is visible.
    """
    sc = _num(score)
    if str(action or "").strip().upper() in _BEARISH_ACTIONS or (sc is not None and sc < 40):
        return -1
    return 1


def _conviction(score) -> tuple:
    """Composite-band conviction label (same bands as the grade thresholds)."""
    sc = _num(score) or 0.0
    if sc >= 70:
        return "High", "good"
    if sc >= 55:
        return "Developing", "warn"
    return "Watch only", "neutral"


def _rr(item):
    rr = _num(item.get("rr"))
    if rr and rr > 0:
        return rr
    e, s, t = _num(item.get("entry")), _num(item.get("sl")), _num(item.get("tp"))
    if e and s and t and abs(e - s) > 1e-9:
        return abs(t - e) / abs(e - s)
    return None


def _setup_name(signal_type: str) -> str:
    """'🚀 Breakout setup' → 'Breakout setup' (emoji stays out of the table)."""
    s = str(signal_type or "").strip()
    if s and not s[0].isalnum():
        s = s.split(" ", 1)[-1]
    return s


def _sym(ticker) -> str:
    return html.escape(str(ticker or "?").replace(".NS", ""))


# ─────────────────────────────────────────────────────────────────────────────
# Gate strip — five cells (UI_UX_DESIGN §4.1 ceiling)
# ─────────────────────────────────────────────────────────────────────────────
try:
    _ledger = get_tw_ledger()
except Exception:
    _ledger = None


def _follow_through_rate(ledger):
    """(hit %, n) over settled 5-day rows, direction-aware; (None, n) under 5."""
    if ledger is None or getattr(ledger, "empty", True) or "ret_5d" not in ledger.columns:
        return None, 0
    df = ledger[ledger["ret_5d"].notna()]
    n = len(df)
    if n < 5:
        return None, n
    hits = 0
    for act, sc, ret in zip(df.get("composite_action", [None] * n),
                            df.get("composite_score", [None] * n), df["ret_5d"]):
        r = _num(ret)
        if r is not None and _setup_direction(act, sc) * r > 0:
            hits += 1
    return hits / n * 100.0, n


def _gate_cells() -> list:
    n_uni, n_sc = _wl.get("n_universe"), _wl.get("n_scored")
    n_short = len(_brk_items) + len(_bdn_items) + len(_rev_items)
    cells = [
        ("Scanned",
         f"{n_sc:,}" if n_sc else "–",
         (f"of {n_uni:,} · Nifty 500" if n_uni else "Nifty 500") if n_sc
         else "count arrives with the next scan"),
        ("Shortlisted", f"{n_short}",
         f"{n_short / n_sc * 100:.1f}% cleared a bucket" if n_sc else "setups on today's scan"),
        ("Split", f"{len(_brk_items)} · {len(_bdn_items)} · {len(_rev_items)}",
         "breakout · breakdown · reversal"),
    ]
    _ft, _ft_n = _follow_through_rate(_ledger)
    cells.append(("5D follow-through",
                  f"{_ft:.0f}%" if _ft is not None else "–",
                  f"n={_ft_n} · moved with the setup" if _ft is not None
                  else f"{_ft_n} settled so far, needs 5"))
    try:
        _reg = get_regime_snapshot() or {}
    except Exception:
        _reg = {}
    _lab = str(_reg.get("label") or "unknown")
    _tone = {"trend_up": "bull", "trend_down": "bear", "risk_off": "bear", "range": "amber"}.get(_lab, "dim")
    _conf = str(_reg.get("confidence") or "").strip().lower()   # low | medium | high
    cells.append(("Regime",
                  f'<span style="color:var(--{_tone})">{html.escape(_lab.replace("_", " ").title())}</span>',
                  f"{html.escape(_conf)} confidence" if _conf
                  else ("live snapshot" if _reg else "feed unavailable")))
    return cells


st.markdown(gate_strip(_gate_cells()), unsafe_allow_html=True)

# DT1 + DT2 · scan-time attribution with the source pill.
try:
    from dashboard.shared.ui_components import data_as_of as _tw_asof
    _tw_when = str(_wl.get("scan_time") or "unknown")
    st.markdown(
        _tw_asof(_tw_when, source="yfinance",
                 ttl_hint="EOD scan cached until next session"),
        unsafe_allow_html=True,
    )
except Exception:
    pass
st.caption(
    "Runs end-of-day on today's daily close and stays cached until the next session; "
    "not intraday."
    + (" · ⚠️ showing the previous session while a refresh runs." if _is_stale else "")
)

from dashboard.shared.disclosures import (
    render_regime_reliability_note as _tw_regime_note,
)
_tw_regime_note()


# ─────────────────────────────────────────────────────────────────────────────
# Previous session's picks — follow-through strip (fix W4)
# ─────────────────────────────────────────────────────────────────────────────
def _render_follow_through() -> None:
    _hdr = "Previous picks · follow-through"
    _prior = None
    if _ledger is not None and not getattr(_ledger, "empty", True) and "logged_date" in _ledger.columns:
        # logged_date is the NSE session (IST) the pick priced off — compare
        # against the current session, not the server's UTC calendar date.
        from analysis.verdict_ledger import session_date as _session_date
        _dates = _ledger["logged_date"].astype(str)
        _prior = _ledger[_dates < _session_date().isoformat()]
    if _prior is None or _prior.empty:
        st.markdown(section_header(_hdr, "source: verdict_ledger"), unsafe_allow_html=True)
        st.caption("Appears once the ledger holds a previous session's picks. "
                   "Every end-of-day scan logs its shortlist.")
        return

    _last = _prior["logged_date"].astype(str).max()
    _day = _prior[_prior["logged_date"].astype(str) == _last]
    if "composite_score" in _day.columns:
        _day = _day.sort_values("composite_score", ascending=False)
    _day = _day.drop_duplicates("ticker").head(5)
    try:
        _live = _picks_live_prices(tuple(str(t) for t in _day["ticker"]))
    except Exception:
        _live = {}

    _cards = []
    for _rank, _r in enumerate(_day.to_dict("records"), start=1):
        _act = _r.get("composite_action")
        _sc = _num(_r.get("composite_score"))
        _dir = _setup_direction(_act, _sc)
        _entry = _num(_r.get("entry_price"))
        _now = _num((_live.get(str(_r.get("ticker"))) or {}).get("price"))

        _rows = [("Entry", _px(_entry))]
        _move = None
        if _entry and _now:
            _move = (_now / _entry - 1.0) * 100.0
            _rows.append(("Now", f"{_px(_now)} {chip_delta(_move)}"))
        else:
            for _h in ("1d", "5d", "20d"):          # ledger returns are in percent
                _v = _num(_r.get(f"ret_{_h}"))
                if _v is not None:
                    _move = _v
                    _rows.append((f"{_h.upper()} ret", chip_delta(_v)))
                    break
            else:
                _rows.append(("Now", '<span style="color:var(--faint)">pending</span>'))

        if _move is None:
            _pill, _tone = chip_pill("Pending", "neutral"), ""
        elif _dir * _move >= _MOVE_BAND:
            _pill, _tone = chip_pill("Following through", "good"), "good"
        elif _dir * _move <= -_MOVE_BAND:
            _pill, _tone = chip_pill("Against setup", "bad"), "bad"
        else:
            _pill, _tone = chip_pill("Flat", "neutral"), ""

        _sub = (f"{_sc:.0f}/90 · " if _sc is not None else "") + html.escape(
            _display_label(str(_act or "")) or "logged")
        _sub += " · bearish setup" if _dir < 0 else " · bullish setup"
        _cards.append(follow_card(_rank, _sym(_r.get("ticker")), _sub, _rows, _pill, _tone))

    st.markdown(
        section_header(_hdr, f"logged {html.escape(_last)} · top {len(_cards)} by score · "
                             "direction from logged posture"),
        unsafe_allow_html=True,
    )
    st.markdown(f'<div class="fu-grid">{"".join(_cards)}</div>', unsafe_allow_html=True)


try:
    _render_follow_through()
except Exception as _yp_err:
    import logging
    logging.getLogger("dashboard.tomorrow_watchlist").debug(
        "follow-through strip render failed: %s", _yp_err)


# ─────────────────────────────────────────────────────────────────────────────
# Fresh picks — ranked table per bucket + one detail card with actions
# ─────────────────────────────────────────────────────────────────────────────
_ACCENT = {"breakout": "var(--bull)", "breakdown": "var(--bear)", "reversal": "var(--violet)"}


def _card_bg(tok: str) -> str:
    """Dark tinted card gradient: token hue blended into the ground surface."""
    return (f"linear-gradient(135deg,color-mix(in srgb, var(--{tok}) 12%, var(--ground)),"
            f"color-mix(in srgb, var(--{tok}) 17%, var(--ground)))")


_BG = {"breakout": _card_bg("bull"), "breakdown": _card_bg("bear"), "reversal": _card_bg("violet")}

_TABLE_COLS = [("#", "l"), ("Ticker", "l"), ("Entry", "r"), ("Stop", "r"), ("Target", "r"),
               ("R:R", "r"), ("Score", "r"), ("Conviction", "l"), ("Setup", "l")]


def _table_row(rank: int, it: dict) -> list:
    _s = _sym(it.get("ticker"))
    _name = html.escape(get_display_name(str(it.get("ticker", ""))))
    _co = f'<span class="co">{_name}</span>' if _name and _name != _s else ""
    _rr_v = _rr(it)
    _conv, _conv_tone = _conviction(it.get("score"))
    _hz = str(it.get("horizon") or "").split(" (")[0]
    if _hz.lower().startswith("watch only"):   # the conviction pill already says so
        _hz = ""
    return [
        rank_chip(rank),
        f'<span class="sym">{_s}</span>{_co}',
        _px(it.get("entry")), _px(it.get("sl")), _px(it.get("tp")),
        f"1 : {_rr_v:.1f}" if _rr_v else "–",
        f"{_num(it.get('score')) or 0:.0f}",
        chip_pill(_conv, _conv_tone),
        chip_tag(html.escape(_setup_name(it.get("signal_type", "")))) + (chip_tag(html.escape(_hz)) if _hz else ""),
    ]


def _render_detail(it: dict, rank: int, kind: str, key_prefix: str) -> None:
    """Detail card + actions for the one setup selected under the table."""
    accent = _ACCENT[kind]
    _lbl = str(it["ticker"]).replace(".NS", "")
    _entry = it.get("entry") or 0
    _sl    = it.get("sl")    or 0
    _tp    = it.get("tp")    or 0
    _show_levels = _entry > 0 and _sl > 0 and _tp > 0

    _headline_full = it.get("headline", "")
    # FIX W2: card display copy gets an honest ellipsis if truncated;
    # the FULL headline (not truncated) is used for the stored reason below.
    _headline_card = (
        _headline_full[:90] + "…" if len(_headline_full) > 90 else _headline_full
    )
    # FIX HZ1-WL: holding-period label from score_stock()'s _pick_horizon.
    _horizon = it.get("horizon", "")
    _valid_until = it.get("valid_until", "")
    # QF2: qualitative flag badge — safe here because this is the already-
    # shortlisted list, NOT the wide universe scan (see flags_ui.py docstring).
    try:
        _flag_badge = render_flag_badge_html(it["ticker"])
    except Exception:
        _flag_badge = ""
    _conv, _conv_tone = _conviction(it.get("score"))

    st.markdown(
        f'<div style="background:{_BG[kind]};border-left:4px solid {accent};'
        f'border-radius:10px;padding:11px 14px;margin:4px 0 6px 0">'
        f'<div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap">'
        f'<span style="display:inline-flex;align-items:center;gap:8px;font-size:16px;'
        f'font-weight:700;color:var(--ink)">{rank_chip(rank)}{html.escape(_lbl)}'
        f'{chip_pill(_conv, _conv_tone)}</span>'
        f'<span style="font-size:13px;font-weight:700;color:{accent}">'
        f'{it["score"]:.0f}/90 · {_display_label(it["action"])}</span>'
        f'</div>'
        f'{_flag_badge}'
        f'<div style="font-size:11px;color:{accent};font-weight:600;margin-top:3px">'
        f'{it["signal_type"]} · key level {it["key_level"]}</div>'
        f'<div style="font-size:12px;color:var(--ink-mid);margin-top:2px">{html.escape(_headline_card)}</div>'
        + (
            f'<div style="font-size:11px;color:var(--dim);margin-top:2px">'
            f'⏳ {_horizon}' + (f' · fresh until {_valid_until}' if _valid_until else '')
            + '</div>'
            if _horizon else ""
        )
        + (
            f'<div style="font-size:11px;color:var(--dim);margin-top:4px">'
            f'Entry ₹{_entry:,.2f} · SL ₹{_sl:,.2f} · TP ₹{_tp:,.2f}</div>'
            if _show_levels else ""
        )
        + "</div>",
        unsafe_allow_html=True,
    )
    if _show_levels:
        # FIX W2: full headline (not truncated) for the stored trade reason.
        _btn_col1, _btn_col2 = st.columns([1, 1])
        with _btn_col1:
            _paper_trade_popover(
                it["ticker"], _entry, _sl, _tp,
                reason=f"Tomorrow Watch ({it['signal_type']}): {_headline_full}",
                key=f"{key_prefix}_{it['ticker']}",
                label=f"📌 Paper Trade {_lbl}",
            )
        with _btn_col2:
            # FIX NAV-TW: canonical handoff into Analyze Stock (FIX NAV1).
            if st.button(f"📊 Analyze {_lbl}", key=f"{key_prefix}_{it['ticker']}_analyze",
                         width="stretch"):
                st.session_state["analyze_ticker"] = it["ticker"]
                st.session_state["_goto_page"] = "🔍 Analyze Stock"
                st.rerun()
    else:
        # No valid entry/SL/TP to paper-trade on, but Analyze is still useful.
        if st.button(f"📊 Analyze {_lbl}", key=f"{key_prefix}_{it['ticker']}_analyze_only",
                     width="stretch"):
            st.session_state["analyze_ticker"] = it["ticker"]
            st.session_state["_goto_page"] = "🔍 Analyze Stock"
            st.rerun()


def _render_bucket(items: list, kind: str, key_prefix: str) -> None:
    if not items:
        st.markdown(empty_state("No candidates in this bucket",
                                "Today's scan found no setups that pass this bucket's gate."),
                    unsafe_allow_html=True)
        # Breakout funnel: which gate emptied the bucket. Older persisted
        # snapshots don't carry it — show nothing rather than zeros.
        _f = _wl.get("breakout_funnel") if kind == "breakout" else None
        if isinstance(_f, dict) and _f.get("eligible"):
            st.caption(
                "Where names dropped out: "
                f"{_f.get('eligible', 0)} scored → {_f.get('buy_action', 0)} with a bullish "
                f"composite label → {_f.get('score_ge_52', 0)} with score ≥ 52 → "
                f"{_f.get('momentum_ge_8', 0)} with momentum ≥ 8 → "
                f"{_f.get('volume_ge_5', 0)} with volume ≥ 5."
            )
        return
    # Items arrive ranked by _tomorrow_watchlist (desc score for breakout /
    # reversal, asc for breakdown), so enumerate() from 1 is the rank.
    st.markdown(signal_table(_TABLE_COLS, [_table_row(i, it) for i, it in enumerate(items, 1)]),
                unsafe_allow_html=True)
    _sel = st.selectbox(
        "Setup detail & actions",
        options=list(range(len(items))),
        format_func=lambda i: (f"#{i + 1}  {str(items[i]['ticker']).replace('.NS', '')}"
                               f" · {_num(items[i].get('score')) or 0:.0f}/90"),
        key=f"{key_prefix}_sel",
    )
    _render_detail(items[_sel], _sel + 1, kind, key_prefix)


st.markdown(
    section_header("Fresh picks · next session",
                   "ranked by composite within each bucket · rank chip = top 3"),
    unsafe_allow_html=True,
)
_t1, _t2, _t3 = st.tabs([
    f"🚀 Breakout Watch ({len(_brk_items)})",
    f"🔻 Breakdown Watch ({len(_bdn_items)})",
    f"🔄 Reversal Watch ({len(_rev_items)})",
])
with _t1:
    st.caption(
        "Constructive setups approaching resistance with momentum and volume building; "
        "watch for a breakout at next open."
    )
    _render_bucket(_brk_items, "breakout", "tw_brk")
with _t2:
    st.caption(
        "Weak names below key moving averages with distribution volume; watch for a "
        "potential breakdown."
    )
    _render_bucket(_bdn_items, "breakdown", "tw_bdn")
with _t3:
    st.caption(
        "Divergences: price and momentum disagreeing (a potential turn). Confirm before "
        "acting; these are watch-only, not signals."
    )
    _render_bucket(_rev_items, "reversal", "tw_rev")

# FIX SCR-XREF — see the matching note in 06_smart_screener.py.
with st.expander("↔️ Also see: Smart Screener · TQS Scanner", expanded=False):
    st.markdown(
        "- **Smart Screener** — run the same 4-screen scan interactively with your "
        "own universe / parameters. Use when this pre-computed list doesn't have "
        "what you're after.\n"
        "- **TQS Scanner** — a *different* scoring engine (four-pillar Trend "
        "Quality Score) applied to the same universes. Cross-check a shortlisted "
        "name against its TQS reading before acting."
    )

# Manual refresh control — lets the user trigger a re-scan without waiting
# for the cache TTL, using the same non-blocking pattern as the cold start.
st.markdown("---")
_rf1, _rf2 = st.columns([5, 1])
with _rf2:
    if st.button("🔄 Re-scan now", key="tw_manual_rescan"):
        # FIX MKT4: was a blanket st.cache_data.clear() — wiped every other
        # page's cached data too (Command Centre's Top Picks, etc.), not
        # just this page's own scan. _tomorrow_watchlist is already
        # imported at the top of this module, so it's safe to clear here.
        _tomorrow_watchlist.clear()
        st.session_state["tw_running"]   = False
        st.session_state.pop("tw_bg_holder", None)
        st.session_state.pop("tw_bg_thread", None)
        st.rerun()
with _rf1:
    st.caption(
        "⚠️ Educational watchlist on end-of-day signals — not investment advice. "
        "Always confirm at next open before acting."
    )
