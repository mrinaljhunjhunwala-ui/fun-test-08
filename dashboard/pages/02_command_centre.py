"""Command Centre - NSE Smart Investor (multipage page; body verbatim from app.py)."""
import os
import sys
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import threading
import pandas as pd
import streamlit as st
import trade_store as _store

from dashboard.shared.design import apply_design
from dashboard.shared.nav import render_sidebar
from dashboard.shared.picks_ui import render_pick_analysis
from dashboard.shared.chart_helpers import render_top_bar, tick_pulse_tracker
from dashboard.shared.ui_components import chip_pill, ticker_hover_wrap
from dashboard.shared.cache import (
    get_top_picks,
    _persisted_top_picks_snapshot,   # FIX TP-FAST1 / FIX TP-NOOP1
    _score_watchlist,
    _sector_ranks_tuple,
    _sparkline_closes,
    _sparkline_svg,
    _trade_type,
    _picks_live_prices,     # FIX CC-LIVE1
    _horizon_countdown,     # FIX CC-LIVE1
    get_vix_info,
)
from dashboard.shared.trade_utils import (
    _auto_close_breached,
    _display_label,            # Phase 2 UI honesty
    verdict_display_label,
    _is_squareoff_time,
    _paper_trade_popover,
    _portfolio_live_prices,
    _render_autoclose_banner,
    paper_close_trade,
)

apply_design()
render_sidebar(current="Command Centre")
render_top_bar()

# ───────────────────────── page body ─────────────────────────
# LAYOUT (mockup artboard 01, 2026-09-12 "proposed layout" artifact)
# Title + meta line → five-cell market pulse → descriptive posture hero →
# sector heatmap → top gainers + recent posture changes → data-health strip.
# Top Picks, paper trades and open positions below keep their behaviour.
#
# CC-COPY: the blocks this replaced (morning card, VIX/Nifty cards, "market
# mood" gauge, regime badge) carried instruction copy — "avoid new buys,
# protect capital", "trade your setups at plan size", "position sizing
# halved", "tighten stops". Everything here describes the market from data;
# nothing tells the user what to do (CLAUDE.md rule 1). The second, Top Picks
# ticker tape is dropped too: the top bar already runs a tape, and the audit
# (UI_AUDIT_2026-09 cluster A) flagged a marquee on a decision surface.
import datetime as _mb_dt
import html as _html

from analysis.score import _regime_weights_enabled as _cc_rw_enabled, _BEAR_REGIMES as _cc_bear
from dashboard.shared.cache import (
    get_regime_snapshot, get_fii_dii_recent, get_recent_posture_changes,
    _nifty50_gainers_ticker, get_display_name,
)
from dashboard.shared.chart_helpers import _index_strip_data
from dashboard.shared.ui_components import (
    gate_strip, section_header, signal_table, chip_delta, chip_tag, fmt_inr, rank_chip,
)

_v2_flag = _cc_rw_enabled()   # default ON since 2026-09-24; NSE_USE_REGIME_WEIGHTS=0 disables

st.markdown('<h1 class="page-title-serif">Command <em>Centre</em></h1>', unsafe_allow_html=True)

_mb_now = _mb_dt.datetime.now(_mb_dt.timezone(_mb_dt.timedelta(hours=5, minutes=30)))
try:
    _mbo = _store.fetch_open()
    _mb_open = 0 if (_mbo is None or _mbo.empty) else len(_mbo)
    _mb_open_txt = f"{_mb_open} open paper position{'s' if _mb_open != 1 else ''}"
except Exception as _e:
    _mb_open_txt = "paper positions unavailable"
st.markdown(
    f'<p class="page-subtitle">{_mb_now.strftime("%a %d %b %Y · %H:%M IST")} · {_mb_open_txt} · '
    'market conditions, open positions and watchlist in one place.</p>',
    unsafe_allow_html=True,
)

# ── Inputs shared by the pulse strip and the hero ───────────────────────────
try:
    _cc_vix_info = get_vix_info()
except Exception:
    _cc_vix_info = {}
_cc_vix_r = str(_cc_vix_info.get("regime", "unknown")).lower()
_cc_vix_v = _cc_vix_info.get("vix")
try:
    _cc_reg = get_regime_snapshot() or {}
except Exception:
    _cc_reg = {}
_cc_label = str(_cc_reg.get("label") or "unknown").lower()
_cc_conf = str(_cc_reg.get("confidence") or "").lower()
_cc_breadth = str((_cc_reg.get("components") or {}).get("breadth", "unknown"))
_cc_breadth_pct = (_cc_reg.get("metrics") or {}).get("pct_above_sma50")
try:
    _cc_idx = {lbl: (px, chg) for lbl, px, chg in _index_strip_data()}
except Exception:
    _cc_idx = {}
try:
    _cc_flows = get_fii_dii_recent()
except Exception:
    _cc_flows = None

_TONE = {"complacency": "amber", "normal": "bull", "elevated": "amber", "fear": "bear", "panic": "bear",
         "trend_up": "bull", "trend_down": "bear", "risk_off": "bear", "range": "amber",
         "broad": "bull", "mixed": "amber", "narrow": "bear"}


def _tinted(text: str, key: str) -> str:
    return f'<span style="color:var(--{_TONE.get(key, "dim")})">{_html.escape(text)}</span>'


def _index_cell(label: str, name: str) -> tuple:
    px_chg = _cc_idx.get(label)
    if not px_chg:
        return (name, "–", "feed unavailable")
    return (name, fmt_inr(px_chg[0], 0), chip_delta(px_chg[1]) + "today")


def _pulse_cells() -> list:
    cells = [_index_cell("NIFTY 50", "Nifty 50"), _index_cell("BANK NIFTY", "Bank Nifty")]
    cells.append(("India VIX",
                  f"{_cc_vix_v:.2f}" if isinstance(_cc_vix_v, (int, float)) else "–",
                  _tinted(_cc_vix_r.title() + " zone", _cc_vix_r)))
    cells.append(("Breadth",
                  f"{_cc_breadth_pct:.0f}%" if isinstance(_cc_breadth_pct, (int, float)) else "–",
                  _tinted(_cc_breadth.title(), _cc_breadth) + " · Nifty 500 above SMA50"))
    if _cc_flows and _cc_flows.get("fii") is not None:
        _dii = _cc_flows.get("dii")
        cells.append(("FII net · ₹ Cr",
                      f'{"+" if _cc_flows["fii"] >= 0 else "−"}{fmt_inr(abs(_cc_flows["fii"]), 0)}',
                      (f"DII {_dii:+,.0f} · " if _dii is not None else "")
                      + f"5D {_cc_flows['fii_5d']:+,.0f} · {_html.escape(_cc_flows['date'])}"))
    else:
        cells.append(("FII net · ₹ Cr", "–", "no stored flows yet"))
    return cells


st.markdown(gate_strip(_pulse_cells()), unsafe_allow_html=True)


# ── Descriptive posture hero ─────────────────────────────────────────────────
def _posture() -> tuple:
    """(noun, qualifier, tone) describing the tape. Never an instruction."""
    if _cc_label == "risk_off" or _cc_vix_r in ("fear", "panic"):
        return "Defensive", "volatility elevated", "bad"
    if _cc_label == "trend_down":
        return "Weak", "trend pointing lower", "bad"
    if _cc_vix_r == "complacency":
        return "Complacent", "volatility unusually low", "warn"
    if _cc_label == "trend_up" and _cc_vix_r == "normal":
        return "Constructive", "trend and volatility aligned", "accent"
    if _cc_vix_r == "elevated":
        return "Cautious", "volatility above normal", "warn"
    return "Mixed", "no clear trend", "warn"


def _posture_why() -> str:
    bits = []
    nifty = _cc_idx.get("NIFTY 50")
    if nifty:
        bits.append(f"Nifty 50 at <b>{fmt_inr(nifty[0], 0)}</b> ({nifty[1]:+.2f}% today).")
    if _cc_label != "unknown":
        bits.append(f"The regime classifier reads <b>{_cc_label.replace('_', ' ')}</b>"
                    + (f" with {_html.escape(_cc_conf)} confidence." if _cc_conf else "."))
    if isinstance(_cc_vix_v, (int, float)):
        bits.append(f"India VIX {_cc_vix_v:.1f} sits in the {_cc_vix_r} zone.")
    if isinstance(_cc_breadth_pct, (int, float)):
        bits.append(f"{_cc_breadth_pct:.0f}% of Nifty 500 names trade above their 50-day average.")
    if _cc_flows and _cc_flows.get("n_5d"):
        _f5 = _cc_flows["fii_5d"]
        bits.append(f"FIIs were net {'buyers' if _f5 >= 0 else 'sellers'} of "
                    f"₹{fmt_inr(abs(_f5), 0)} Cr over the last {_cc_flows['n_5d']} sessions.")
    if _v2_flag and _cc_label in _cc_bear:
        bits.append("In this regime the momentum pillar scores 5-day reversals instead of trend momentum.")
    return " ".join(bits) or "Market feeds are unavailable right now."


try:
    from dashboard.shared.ui_components import hero_verdict as _hero_verdict
    _pn, _pq, _pt = _posture()
    _mode = ("bear-mode scoring" if (_v2_flag and _cc_label in _cc_bear)
             else ("standard scoring" if _v2_flag else "legacy scoring"))
    st.markdown(
        _hero_verdict(posture=_pn, posture_qualifier=_pq,
                      kicker=f"Market posture · {_cc_label.replace('_', ' ')} regime · {_mode}",
                      why=_posture_why(), tone=_pt),
        unsafe_allow_html=True,
    )
except Exception as _hv_err:
    import logging
    logging.getLogger("dashboard.command_centre").debug("hero_verdict render failed: %s", _hv_err)


# ── Sector heatmap (Nifty sectoral indices, 1-day change) ────────────────────
_SECTOR_IDX = [("BANK NIFTY", "Bank"), ("NIFTY IT", "IT"), ("NIFTY AUTO", "Auto"),
               ("NIFTY FMCG", "FMCG"), ("NIFTY PHARMA", "Pharma"), ("NIFTY METAL", "Metal"),
               ("NIFTY ENERGY", "Energy")]


def _render_sector_heatmap() -> None:
    rows = [(name, *_cc_idx[lbl]) for lbl, name in _SECTOR_IDX if lbl in _cc_idx]
    st.markdown(section_header("Sector heatmap", "1-day change · nifty sectoral indices · sorted"),
                unsafe_allow_html=True)
    if not rows:
        st.caption("Sector index quotes are unavailable right now.")
        return
    rows.sort(key=lambda r: -r[2])
    peak = max(max(abs(r[2]) for r in rows), 1.0)
    cells = []
    for name, px, chg in rows:
        hue = "bull" if chg >= 0 else "bear"
        alpha = 4 + round(min(abs(chg) / peak, 1.0) * 22)      # hue = sign, strength = size
        cells.append(
            f'<div class="heat-cell" style="background:color-mix(in srgb, var(--{hue}) {alpha}%, var(--surface))">'
            f'<span class="heat-name">{name}</span>'
            f'<div><div class="heat-val" style="color:var(--{hue})">{chg:+.2f}%</div>'
            f'<div class="heat-sub">{fmt_inr(px, 0)}</div></div></div>'
        )
    st.markdown(f'<div class="heat-grid">{"".join(cells)}</div>', unsafe_allow_html=True)


_render_sector_heatmap()


# ── Two columns: today's gainers + recent posture changes ────────────────────
def _panel_html(title: str, sub: str, body: str) -> str:
    return (f'<div class="kit-panel"><div class="kit-panel-hd"><span class="kit-panel-t">{title}</span>'
            f'<span class="kit-panel-s">{sub}</span></div>{body}</div>')


def _gainers_html() -> str:
    try:
        rows = _nifty50_gainers_ticker(n=5)
    except Exception:
        rows = []
    if not rows:
        return _panel_html("Top gainers · today", "Nifty 50 · live",
                           '<div class="ledger-note" style="padding:14px">'
                           "No Nifty 50 name is up today, or quotes are unavailable.</div>")
    body = signal_table(
        [("#", "l"), ("Ticker", "l"), ("LTP", "r"), ("Δ %", "r")],
        [[rank_chip(i),
          f'<span class="sym">{_html.escape(r["ticker"].replace(".NS", ""))}</span>'
          f'<span class="co">{_html.escape(get_display_name(r["ticker"]))}</span>',
          "₹" + fmt_inr(r["price"], 2 if r["price"] < 1000 else 0),
          chip_delta(r["chg_pct"])]
         for i, r in enumerate(rows, start=1)],
    )
    return _panel_html("Top gainers · today", "Nifty 50 · live, 60 s cache", body)


def _changes_html() -> str:
    try:
        changes = get_recent_posture_changes(limit=5)
    except Exception:
        changes = []
    if not changes:
        return _panel_html("Posture changes · recent", "verdict ledger",
                           '<div class="ledger-note" style="padding:14px">'
                           "No verdict has changed between logged sessions yet.</div>")
    items = []
    for c in changes:
        _sc = ""
        if c.get("prev_score") is not None and c.get("new_score") is not None:
            try:
                _sc = f"composite {float(c['prev_score']):.0f} → {float(c['new_score']):.0f} · "
            except (TypeError, ValueError):
                _sc = ""
        items.append(
            f'<li><span class="ledger-when">{_html.escape(c["date"][5:])}</span><div>'
            f'<span class="ledger-sym">{_html.escape(str(c["ticker"]).replace(".NS", ""))}</span>'
            f'{chip_tag(_html.escape(verdict_display_label(c["prev"])))} → '
            f'{chip_tag(_html.escape(verdict_display_label(c["new"])))}'
            f'<div class="ledger-note">{_sc}logged by {_html.escape(str(c["source"]).replace("_", " "))}</div>'
            f'</div></li>'
        )
    return _panel_html("Posture changes · recent", "verdict ledger · latest change per ticker",
                       f'<ul class="ledger-list">{"".join(items)}</ul>')


_cc_c1, _cc_c2 = st.columns(2, gap="medium")
with _cc_c1:
    st.markdown(_gainers_html(), unsafe_allow_html=True)
with _cc_c2:
    st.markdown(_changes_html(), unsafe_allow_html=True)


# ── Data health: one-line strip, full table on demand (Task 2.3) ─────────────
# Read-only aggregation of each provider's own diagnostics; no network here.
try:
    from dashboard.shared.data_health import (
        collect_all_health as _dh_collect, render_data_health_html as _dh_render,
    )
    _dh_checks = _dh_collect()
    _dh_dot = {"healthy": "bull", "stale": "amber", "degraded": "bear"}
    st.markdown(
        '<div class="health-strip"><b>Data health</b>' + "".join(
            f'<span class="health-item"><span class="health-dot" '
            f'style="background:var(--{_dh_dot.get(c.status, "faint")})"></span>{_html.escape(c.name)}</span>'
            for c in _dh_checks) + "</div>",
        unsafe_allow_html=True,
    )
    with st.expander("Data health details", expanded=False):
        st.markdown(_dh_render(_dh_checks), unsafe_allow_html=True)
except Exception as _dh_err:
    import logging
    logging.getLogger("dashboard.command_centre").debug("data_health strip render failed: %s", _dh_err)

st.markdown("---")

# ═══════════════════════════════════════════════════════════════════════════
# ── 2. TODAY'S TOP PICKS — full NSE-wide scan, stale-while-revalidate ──────
# ═══════════════════════════════════════════════════════════════════════════
# FIX (blanking on rescan): the old version called _home_top_picks() inside
# `with st.spinner(...)`, directly on the page's normal script run. On a
# cache-miss (cold start, or the 5-min TTL expiring), that call blocks for
# ~2 minutes — and because the Top Picks section literally hasn't executed
# yet on this run, there is nothing to show: the previous cards aren't "still
# there", they just haven't been re-drawn, so the whole section reads as
# blank/spinner until the scan finishes.
#
# Fix: the entire section is now an @st.fragment(run_every=...) — Streamlit
# reruns ONLY this fragment on its own timer, not the whole page. The last
# good scan result is kept in st.session_state and rendered immediately on
# every fragment run, BEFORE checking whether a refresh is needed. If the
# cached result is stale, a background thread kicks off _home_top_picks()
# without blocking the render — so the existing cards stay exactly as they
# are (with a small "refreshing…" note) until the new scan lands, at which
# point the next fragment tick swaps them in. Nothing ever goes blank.
#
# Universe: this scans get_universe("niftytotalmarket") inside
# _home_top_picks — the full ~745-ticker liquid NSE list (Nifty 500 +
# Microcap 250), not a Nifty-50-only set. FIX TP2: previously scanned only
# the narrower "nifty500" (~504 tickers) set, which under-used the wider
# universe already built in data/universe.py.

_PICKS_KEY = "_cc_top_picks"
_IST = _mb_dt.timezone(_mb_dt.timedelta(hours=5, minutes=30))  # chip shows IST even on a UTC host
_PICKS_TTL_SECONDS = 300  # matches _home_top_picks' own cache TTL


def _gen_at_local(gen_at: str) -> _mb_dt.datetime:
    """A snapshot's generated_at as naive local time, comparable with
    datetime.now(). The warmer stamps UTC (older stamps are naive UTC), so a
    bare fromisoformat() was 5.5 h off on an IST machine."""
    from utils.snapshot_time import parse_snapshot_timestamp  # not via trade_store — see cache.py
    _ts = parse_snapshot_timestamp(gen_at)
    return _ts.astimezone().replace(tzinfo=None) if _ts else _mb_dt.datetime.now()


def _picks_background_fetch(vix_regime: str, sector_ranks: tuple, force_live: bool = False) -> None:
    """Runs in a worker thread. Only touches st.session_state — never calls
    st.* UI functions, which are not safe off the main script thread.

    FIX (stuck-scanning bug): a bare threading.Thread has no Streamlit
    ScriptRunContext attached. Touching st.session_state with no context
    raises NoSessionContext on modern Streamlit — and since BOTH the
    try-block writes AND the except-block's own write below would raise,
    the exception from the except handler itself goes unhandled and the
    thread dies silently (it's a daemon thread) before the `finally` ever
    runs. That left `_fetching` stuck True forever, so the fragment kept
    showing "Running the first scan…" with no result ever landing. The
    caller now attaches the calling thread's ScriptRunContext to this
    thread via add_script_run_ctx() before starting it, so these
    session_state writes work normally.
    """
    try:
        # FIX SPEED1: get_top_picks() reads a scheduled scan snapshot from
        # trade_store first (written every 15 min by scripts/warm_top_picks.py
        # via GitHub Actions) and only falls back to a live ~2-min scan
        # (_home_top_picks, unchanged) if that snapshot is missing or stale.
        result = get_top_picks(vix_regime=vix_regime, sector_ranks=sector_ranks,
                               force_live=force_live)
        st.session_state[_PICKS_KEY] = result
        # FIX TP-NOOP1: record the snapshot's generated_at (when available)
        # so the next fragment tick can detect "snapshot unchanged" and skip
        # the whole refresh instead of spawning another bg thread every 5 min.
        # Live-scan results carry no generated_at — set to None so any future
        # persisted snapshot will look "different" and load in.
        _gen_at = result.get("generated_at") if isinstance(result, dict) else None
        st.session_state[f"{_PICKS_KEY}_gen_at"] = _gen_at
        # Anchor _ts to the snapshot's true generation time when available,
        # so the "Top Picks last updated" chip reflects when the scan
        # actually ran (not the round-trip time on this thread).
        if _gen_at:
            st.session_state[f"{_PICKS_KEY}_ts"] = _gen_at_local(_gen_at)
        else:
            st.session_state[f"{_PICKS_KEY}_ts"] = datetime.datetime.now()
        st.session_state[f"{_PICKS_KEY}_error"] = None
    except Exception as _e:
        st.session_state[f"{_PICKS_KEY}_error"] = str(_e)
    finally:
        st.session_state[f"{_PICKS_KEY}_fetching"] = False


import datetime


# FIX CC-FRESH → dashboard/shared/pick_freshness — pick-card freshness helper
# was extracted here so both Command Centre and My Watchlist use one impl.
# Local aliases kept so the test that scrapes _reanchor_levels + _COST_ROUNDTRIP_PCT
# out of this file still passes.
from dashboard.shared.pick_freshness import (
    COST_ROUNDTRIP_PCT as _COST_ROUNDTRIP_PCT,
    LIVE_DRIFT_THRESHOLD_PCT as _LIVE_DRIFT_THRESHOLD_PCT,
    reanchor_levels as _reanchor_levels,
    compose_finalverdict_for_card as _compose_fv_for_card,
)


# FIX CC-FRAG — this decorator used to live above the old inline
# _reanchor_levels() helper (before it was extracted to pick_freshness).
# The extraction left the decorator dangling above the import block, which
# is a SyntaxError on real Python (streamlit-testing's AppTest was swallowing
# it as a startup error, so the local smoke test passed while the actual
# Streamlit Cloud runtime crashed with "invalid syntax" on ast.parse).
# The decorator belongs on _render_top_picks_section — that's what should
# rerun every 20s to keep the pick cards' live prices ticking.
@st.fragment(run_every=20)
def _render_top_picks_section(vix_regime: str, sector_tuple: tuple) -> None:
    _tp_h1, _tp_h2 = st.columns([5, 2])
    with _tp_h1:
        # §9.4 typography scale · was st.markdown("### …"). Streamlit's own
        # h3 rendering doesn't match the scale (bolder, tighter, different
        # letter-spacing) so anchor headers stamp through the .t-h1 class
        # for consistency with other pages that will adopt this next.
        st.markdown(
            '<div class="t-h1" style="margin:6px 0 4px 0">'
            '🔥 Today\'s Top Picks — NSE Scan</div>',
            unsafe_allow_html=True,
        )
        st.caption("Strongest and weakest **trend-quality** setups today. "
                   "Scores rank trend health — they are **not a forecast of returns**. "
                   "The pick list is regenerated every ~15 min by a scheduled scan and this page "
                   "picks up each new snapshot within seconds; prices on each card tick live "
                   "(~60s) in between. Old picks stay on screen while a refresh runs in the "
                   "background.")
    with _tp_h2:
        st.write("")
        _run_picks = st.button("🔎 Scan Now", key="cc_run_picks", width="stretch")

    from dashboard.shared.disclosures import (
        render_regime_reliability_note as _cc_regime_note,
        render_score_methodology as _cc_score_methodology,
    )
    _cc_regime_note()
    _cc_score_methodology()

    # ── Decide whether a (re)scan is needed, then kick it off in the background ──
    _now = datetime.datetime.now()
    _last_ts = st.session_state.get(f"{_PICKS_KEY}_ts")
    _is_stale = (_last_ts is None) or ((_now - _last_ts).total_seconds() > _PICKS_TTL_SECONDS)
    _fetching = st.session_state.get(f"{_PICKS_KEY}_fetching", False)

    # FIX (stuck-scanning watchdog): if a scan has been "fetching" for longer
    # than any real scan should ever take, the background thread has died
    # (crashed, killed, or — before the ScriptRunContext fix above — a
    # NoSessionContext error) without ever resetting the flag. Self-heal
    # instead of showing "scanning…" forever.
    #
    # FIX TP-WATCHDOG1: raised 180 → 300 s. The user-facing copy in the
    # first-scan banner says the live scan "can take ~2 minutes", and the
    # underlying _home_top_picks worst case observed at 745 tickers × 16
    # workers on a bad Angel-throttle day is closer to 3 min, not 2. At the
    # old 180 s threshold a slow-but-healthy live scan could be falsely
    # marked "stalled and was reset automatically" while the bg thread was
    # still working — then it would land seconds later against session_state
    # already flipped to a fresh scan attempt, causing a needless second
    # 2-min scan on top of the one that would have succeeded. 300 s comfortably
    # brackets the real worst case while still self-healing a genuinely dead
    # thread within a fragment tick or two.
    # 300 → 480 s (2026-09-28): the full scan now takes ~215-225 s even on
    # GitHub's runners (warm_top_picks logs), and Streamlit Cloud is slower,
    # so a healthy live scan was being reset as "stalled" and restarted.
    _fetch_started = st.session_state.get(f"{_PICKS_KEY}_fetch_started")
    if _fetching and _fetch_started and (_now - _fetch_started).total_seconds() > 480:
        st.session_state[f"{_PICKS_KEY}_fetching"] = False
        st.session_state[f"{_PICKS_KEY}_error"] = (
            "Previous scan attempt stalled and was reset automatically."
        )
        _fetching = False
        _is_stale = True

    # ── FIX TP-FAST1 + FIX TP-NOOP1 — cheap snapshot peek before any bg work ──
    # Previously EVERY refresh (first render, and every 5 min thereafter) went
    # straight to the bg-thread path, which:
    #   (a) on first render, showed a scary "~2 minutes" banner even though the
    #       persisted snapshot returns in ~1 s from Postgres, and
    #   (b) on every 5-min tick, flashed the "🔄 Refreshing…" banner and paid
    #       a DB round-trip even though the warmer only advances the snapshot
    #       every 15 min — so 2 in 3 refreshes had nothing new to fetch.
    #
    # Fix: peek at the persisted snapshot's generated_at directly. If it hasn't
    # advanced since we last loaded it, no-op the whole refresh (no bg thread,
    # no banner). If it HAS advanced, load it synchronously — a Postgres KV
    # read is fast enough to do on the main thread. Only the true slow path
    # (Scan Now, or persisted snapshot genuinely missing / older than
    # _TOP_PICKS_MAX_AGE_SECONDS in trade_store) still spawns the bg thread
    # and shows the "~2 minutes" banner. This makes the common case both
    # faster (no bg thread ceremony) and quieter (no misleading banners).
    _prev_gen_at = st.session_state.get(f"{_PICKS_KEY}_gen_at")
    _needs_bg_scan = False
    if _run_picks:
        # User forced: skip the peek and go straight to the slow live-scan
        # path (Scan Now is the escape hatch for "the snapshot looks stale
        # even though the warmer says otherwise").
        _needs_bg_scan = True
    elif _is_stale:
        _snap_peek = _persisted_top_picks_snapshot()
        _snap_gen = _snap_peek.get("generated_at") if _snap_peek else None
        if _snap_peek is None:
            # No persisted snapshot at all (or it's older than the tolerance
            # window) — this is the real slow path. Bg thread + banner.
            _needs_bg_scan = True
        elif _snap_gen and _snap_gen == _prev_gen_at:
            # Snapshot unchanged since our last load — no work to do. Push our
            # local _ts forward so we don't re-peek every 20s fragment tick.
            st.session_state[f"{_PICKS_KEY}_ts"] = _now
            _last_ts = _now
            _is_stale = False
        else:
            # Snapshot advanced — swap it in synchronously. No bg thread, no
            # "refreshing…" banner flash, cards update in one clean render.
            st.session_state[_PICKS_KEY] = _snap_peek
            st.session_state[f"{_PICKS_KEY}_gen_at"] = _snap_gen
            # Anchor _ts to the snapshot's real generation time (not local
            # render time) so the "Top Picks last updated" chip below shows
            # when the scan actually ran, not when we happened to read it.
            st.session_state[f"{_PICKS_KEY}_ts"] = _gen_at_local(_snap_gen)
            st.session_state[f"{_PICKS_KEY}_error"] = None
            _last_ts = st.session_state[f"{_PICKS_KEY}_ts"]
            _is_stale = False

    if _needs_bg_scan and not _fetching:
        st.session_state[f"{_PICKS_KEY}_fetching"] = True
        st.session_state[f"{_PICKS_KEY}_fetch_started"] = _now
        _bg_thread = threading.Thread(
            target=_picks_background_fetch,
            args=(vix_regime, sector_tuple, bool(_run_picks)),
            daemon=True,
        )
        # FIX (stuck-scanning bug): without this, st.session_state writes
        # inside _picks_background_fetch raise NoSessionContext and the
        # thread dies before `_fetching` is ever reset to False — see the
        # docstring on _picks_background_fetch for the full failure chain.
        try:
            from streamlit.runtime.scriptrunner import add_script_run_ctx
            add_script_run_ctx(_bg_thread)
        except Exception as _ctx_e:
            import logging as _ctx_log
            _ctx_log.getLogger("dashboard.command_centre").warning(
                "Could not attach ScriptRunContext to Top Picks scan thread: %s", _ctx_e
            )
        _bg_thread.start()
        _fetching = True

    _picks = st.session_state.get(_PICKS_KEY)
    _err = st.session_state.get(f"{_PICKS_KEY}_error")

    # Status strip — always non-blocking; never replaces the cards below.
    if _picks is None and _fetching:
        st.info("⏳ Running the first scan of the full NSE universe — this can take ~2 minutes. "
                "This page will update on its own the moment it's ready; feel free to keep "
                "using the rest of Command Centre meanwhile.")
        return
    if _picks is None and _err:
        st.error(f"⚠️ Last scan attempt failed: {_err}")
        return
    if _picks is None:
        st.caption("Waiting for the first scan to start…")
        return

    # Legacy persisted snapshots can be missing the "sells" list (see
    # cache._persisted_top_picks_snapshot — historically only required
    # "buys"). Normalize once so downstream accesses can't KeyError.
    _picks.setdefault("buys", [])
    _picks.setdefault("sells", [])

    if _fetching:
        st.markdown(
            '<div style="background:var(--sunken);border:1px solid var(--sunken);border-radius:8px;'
            'padding:6px 14px;margin-bottom:10px">'
            '<span style="font-size:12px;color:var(--bull)">🔄 Refreshing in the background — '
            'current picks below stay as-is until the new scan lands.</span></div>',
            unsafe_allow_html=True,
        )
    elif _last_ts:
        # FIX TP-NOOP1: the freshness chip now shows the snapshot's actual
        # generation time (from the warmer's generated_at, when the picks came
        # from the persisted snapshot) rather than the moment this session
        # happened to load them. Copy updated to match reality: the warmer
        # advances the snapshot every ~15 min in market hours, and the page
        # picks up the new one on its next 20s fragment tick — the old copy
        # ("auto-refreshes every 5 min") was true of the fragment cadence,
        # not of when the cards actually change.
        # FIX TP-VIX1: also surface the vix_regime the scan was scored under,
        # so it's obvious the picks reflect the market regime at scan time
        # (which may lag the live regime by up to ~15 min).
        _src = (_picks or {}).get("source") if isinstance(_picks, dict) else None
        _src_label = "live scan" if _src == "live_scan" else "scheduled scan"
        _snap_regime = ((_picks or {}).get("meta", {}) or {}).get("vix_regime")
        _regime_html = (
            f' <span style="color:var(--dim)">· regime <b style="color:var(--ink-mid)">{_snap_regime}</b></span>'
            if _snap_regime else ""
        )
        st.markdown(
            f'<div style="background:var(--sunken);border:1px solid var(--sunken);border-radius:8px;'
            f'padding:7px 14px;margin-bottom:10px;display:flex;justify-content:space-between;'
            f'align-items:center">'
            f'<span style="font-size:12px;color:var(--bull)">📊 Top Picks last scored: '
            f'<b>{_last_ts.astimezone(_IST).strftime("%H:%M:%S")} IST</b> '
            f'<span style="color:var(--dim)">({_src_label})</span>{_regime_html}</span>'
            f'<span style="font-size:11px;color:var(--faint)">Scan refreshes every ~15 min · '
            f'tap Scan Now to force a live rescan</span>'
            f'</div>',
            unsafe_allow_html=True,
        )

    # ── FIX TP-VIX1: warn on regime mismatch ──
    # If VIX has flipped between when the snapshot was scored and now, the
    # picks (esp. their allow_buy gating and any regime-sensitive scoring)
    # may no longer be appropriate. This is deliberately a soft warning, not
    # an auto-rescan — the warmer will catch up within one 15-min tick, and
    # the escape hatch is "Scan Now" which is one click away below.
    _snap_meta_for_regime = (_picks or {}).get("meta", {}) or {}
    _snap_regime = _snap_meta_for_regime.get("vix_regime")
    _current_regime = vix_regime
    if (_snap_regime and _current_regime and _snap_regime != _current_regime):
        # Only actually flag it as a mismatch worth warning about if either
        # side is one of the risk-off regimes — a normal↔calm drift is not
        # meaningful for the scoring model (allow_buy is identical for both).
        _risk_off = {"fear", "panic"}
        _material = (_snap_regime in _risk_off) != (_current_regime in _risk_off)
        if _material:
            st.markdown(
                f'<div style="background:var(--sunken);border:1px solid var(--sunken);border-radius:8px;'
                f'padding:8px 14px;margin-bottom:10px">'
                f'<span style="font-size:12px;color:var(--amber)">⚠ VIX regime has shifted since '
                f'the last scan (was <b>{_snap_regime}</b>, now <b>{_current_regime}</b>). '
                f'The picks below were scored under the earlier regime. The scheduled scan '
                f'will catch up within ~15 min — or tap <b>Scan Now</b> above to force a '
                f'live rescan under the current regime.</span></div>',
                unsafe_allow_html=True,
            )

    # ── FIX TP1 (page side) — honest "no strong picks" banner + tier-aware cards ──
    _picks_meta = _picks.get("meta", {})

    # ── FIX TP-HEALTH1: scan-health chip when data-fetch degraded ──
    # _home_top_picks now records n_scanned / n_scored_ok / n_unavailable in
    # meta. When the unavailable fraction crosses UNAVAIL_WARN_FRACTION, show
    # a small chip so users understand a short Strongest-trends column reflects
    # a data-source problem (Stooq breaker open, Yahoo throttle, Angel token
    # expired), not a genuinely quiet market. Threshold is deliberate: below
    # ~10 % is normal noise (illiquid tail names, new listings without SMA200
    # history, etc.) and would just add banner fatigue.
    _UNAVAIL_WARN_FRACTION = 0.10
    _n_scanned    = int(_picks_meta.get("n_scanned", 0) or 0)
    _n_unavail    = int(_picks_meta.get("n_unavailable", 0) or 0)
    if _n_scanned > 0 and (_n_unavail / _n_scanned) >= _UNAVAIL_WARN_FRACTION:
        _pct = 100.0 * _n_unavail / _n_scanned
        # DT3 degraded-mode banner -- see dashboard/shared/ui_components.py.
        from dashboard.shared.ui_components import degraded_banner as _degraded
        st.markdown(
            _degraded(
                title="Data quality alert",
                detail=(f"<b>{_n_unavail}/{_n_scanned}</b> tickers ({_pct:.1f}%) "
                        f"were unavailable this scan — the pick list below is "
                        f"drawn from the remaining <b>{_n_scanned - _n_unavail}</b>. "
                        f"A source (Stooq / Yahoo / Angel) may be throttled "
                        f"or degraded."),
                fallback="Picks are still valid but the universe is narrower than usual.",
            ),
            unsafe_allow_html=True,
        )

    if _picks_meta.get("no_strong_picks"):
        st.markdown(
            '<div style="background:var(--sunken);border:1px solid var(--sunken);border-radius:8px;'
            'padding:10px 14px;margin-bottom:10px">'
            '<span style="font-size:13px;color:var(--amber)">⚠️ <b>No strong-trend setups '
            'in today\'s scan.</b> The names below are the closest watchlist-grade '
            'candidates — none currently clear the strong-trend score bar.</span></div>',
            unsafe_allow_html=True,
        )
    elif _picks_meta.get("n_strong_buys", 0) < len(_picks["buys"]):
        st.caption(
            f"📊 {_picks_meta.get('n_strong_buys', 0)} strong-trend setup(s) in today's scan — "
            "remaining cards below are watchlist-grade backfill, marked accordingly."
        )

    # FIX CC-LOAD1 (original): this section used to batch-fetch live price,
    # re-anchor entry/SL/TP AND compute suggested qty for every buy+sell
    # ticker (up to ~40) on EVERY Command Centre rerun — real, unbounded
    # network cost paid whether or not anyone was about to act on any of it.
    # All three were removed; SL/TP re-anchoring + qty sizing still live only
    # in Deep Dive's on-demand Live Snapshot, for the one ticker actually
    # being looked at — sizing off a stale price is a worse failure mode
    # than a stale price label, so that boundary stays.
    #
    # FIX CC-LIVE1: price alone is reinstated, on the same bounded pattern
    # the old Top Picks ticker strip used (removed in #178) — one tiered
    # batch call (_picks_live_prices, cached 60s) for every ticker actually
    # on screen, not a fetch-per-card. This fragment reruns every 20s, but
    # the 60s cache means the live-price network cost only actually fires
    # once every 3rd rerun, same cost profile that strip had.
    _pk_tickers = tuple(sorted({b["ticker"] for b in _picks["buys"]} |
                               {s["ticker"] for s in _picks["sells"]}))
    _pk_live = _picks_live_prices(_pk_tickers)

    # Sparklines for the hover previews: prefetch ALL pick tickers once,
    # bounded pool + hard timeout, instead of one serial fetch per card
    # inside this 20 s fragment. Anything not back in time gets no sparkline.
    _pk_spark: dict = {}
    if _pk_tickers:
        import concurrent.futures as _cc_fut
        _sp_pool = _cc_fut.ThreadPoolExecutor(max_workers=min(8, len(_pk_tickers)))
        try:
            _sp_futs = {_sp_pool.submit(_sparkline_closes, _t): _t for _t in _pk_tickers}
            _sp_done, _ = _cc_fut.wait(list(_sp_futs), timeout=6)
            for _f in _sp_done:
                try:
                    _pk_spark[_sp_futs[_f]] = _f.result(timeout=0)
                except Exception:
                    pass
        finally:
            _sp_pool.shutdown(wait=False, cancel_futures=True)

    # UX3 · live-tick pulse — one shared tracker for both Buy and Sell card
    # loops so a symbol that moves between the two lists compares against its
    # own last-seen price rather than starting over. commit() at the end of
    # the fragment tick prunes symbols no longer in the picks output.
    _pk_pulse, _pk_pulse_commit = tick_pulse_tracker("_cc_picks_prev")

    _pk_buy, _pk_sell = st.columns(2)
    with _pk_buy:
        st.markdown(
            '<div class="t-h2" style="margin:8px 0 6px 0">'
            '🟢 Strongest trends</div>',
            unsafe_allow_html=True,
        )
        if not _picks["buys"]:
            st.caption("No names in the strong-trend band in today's scan.")
        for _b in _picks["buys"]:
            _bl = _b["ticker"].replace(".NS", "")
            _tt_lbl, _tt_emo, _tt_col = _trade_type(_b.get("headline", ""))
            _grade_tag = ("A+" if _b["score"] >= 88 else "A" if _b["score"] >= 75
                          else "B" if _b["score"] >= 62 else "")
            _grade_html = (f'<span style="background:{_tt_col}22;color:{_tt_col};border:1px solid {_tt_col};'
                           f'border-radius:5px;padding:1px 7px;font-size:10px;font-weight:700;margin-left:6px">'
                           f'GRADE {_grade_tag}</span>') if _grade_tag else ""

            _is_watch_tier = _b.get("tier") == "watch"
            _card_border = "var(--amber)" if _is_watch_tier else "var(--bull)"
            _card_grad   = ("linear-gradient(135deg,var(--sunken),var(--sunken))" if _is_watch_tier
                            else "linear-gradient(135deg,var(--sunken),var(--sunken))")
            _score_color = "var(--amber)" if _is_watch_tier else "var(--bull)"
            _tier_badge = chip_pill("Watchlist-grade", tone="warn") if _is_watch_tier else ""

            # FIX FV-PILL — surface the ONE-verdict answer on the pick card.
            # Horizon is inferred from the pick's own "horizon" hint so a
            # Swing-labelled pick is scored on the short lens and a
            # Positional-labelled one on medium. See
            # dashboard/shared/pick_freshness._horizon_for_pick.
            _fv_pill = ""
            try:
                _fv = _compose_fv_for_card(_b, tqs=None)
                _fv_pill_tones = {
                    "STRONG BUY": "good", "BUY": "good", "WATCH": "accent",
                    "HOLD": "neutral", "AVOID": "bad",
                }
                _fv_tone = _fv_pill_tones.get(_fv.verdict, "neutral")
                _fv_title = (
                    f"Combined read on the {_fv.horizon} horizon -- "
                    f"{_fv.confidence} confidence, conviction {_fv.conviction}/100. "
                    f"{_fv.primary_reason}"
                )
                _fv_pill = chip_pill(
                    f"Read: {verdict_display_label(_fv.verdict)}", tone=_fv_tone, title=_fv_title,
                )
            except Exception as _fv_pill_e:
                import logging
                logging.getLogger("dashboard.command_centre").debug(
                    "FinalVerdict pill failed for %s: %s", _b.get("ticker"), _fv_pill_e)

            # FIX CC-LIVE1: live price if the batch fetch resolved this
            # ticker, otherwise the same honest "(last close)" fallback as
            # before — never silently pretend a stale price is live.
            _b_lp = _pk_live.get(_b["ticker"])
            _b_live_price = float(_b_lp["price"]) if _b_lp else None

            # UX2 · hover preview on the ticker label — sparkline + live
            # price + score chip pre-baked into the anchor span. Cached
            # 5 min per ticker via _sparkline_closes, so the cost is one
            # 3-month fetch per pick per fragment refresh cycle at most.
            _b_hover_svg = _sparkline_svg(_pk_spark.get(_b["ticker"]) or [])
            _bl_hover = ticker_hover_wrap(
                _bl,
                sparkline_svg=_b_hover_svg,
                price=_b_live_price,
                chg_pct=(_b_lp.get("chg_pct") if _b_lp else None),
                score=_b.get("score"),
                sector=_b.get("sector", ""),
            )

            # FIX CC-FRESH — re-anchor entry/SL/TP to live price if it's
            # drifted > 0.5 % from the scored entry, and compute honest
            # cost-adjusted R:R. See _reanchor_levels() docstring above.
            _b_lvl = _reanchor_levels(
                float(_b.get("entry") or 0), float(_b.get("sl") or 0),
                float(_b.get("tp") or 0), _b_live_price,
            )
            if _b_lp:
                _b_up = (_b_lp.get("chg_pct") or 0) >= 0
                _b_pc = "var(--bull)" if _b_up else "var(--bear)"
                _b_live_span = (
                    f'<span style="color:{_b_pc};font-weight:700">₹{_b_lp["price"]:,.2f} '
                    f'{"▲" if _b_up else "▼"}{abs(_b_lp.get("chg_pct") or 0):.2f}%</span>'
                    f' <span style="color:var(--faint)">live</span>'
                )
                if _b_lvl["reanchored"]:
                    _b_live_span += (
                        f' <span style="color:var(--amber)"> · re-anchored '
                        f'({_b_lvl["drift_pct"]:+.1f}% drift from scored entry)</span>'
                    )
            else:
                _b_live_span = '<span style="color:var(--faint)">(last close — live price unavailable)</span>'

            _b_rr_html = ""
            if _b_lvl["entry"] and _b_lvl["sl"] and _b_lvl["tp"]:
                _rr_gross = _b_lvl["rr"]
                _rr_net   = _b_lvl["rr_net"]
                _b_rr_html = (
                    f'<div style="font-size:11px;color:var(--dim);margin-top:2px">'
                    f'R:R <span style="color:var(--ink)">{_rr_gross:.1f}:1</span> gross, '
                    f'<span style="color:var(--amber)">{_rr_net:.1f}:1 net of ~{_COST_ROUNDTRIP_PCT:.2f}% costs</span></div>'
                )

            # Freshness stamps — score time and live-price time
            _b_scored_at = st.session_state.get(f"{_PICKS_KEY}_ts")
            _stamp_html = (
                f'<div style="font-size:10px;color:var(--faint);margin-top:3px">'
                f'📊 Scored at {_b_scored_at.strftime("%H:%M") if _b_scored_at else "unknown"}'
                f' · 💹 Live price {"as of now" if _b_lp else "unavailable"}'
                f'</div>'
            )

            # UX3 · pulse the card when the live price ticks. Falls back to
            # "" (no pulse) when live-price is unavailable — matches the honest
            # "(last close)" fallback rather than pretending a tick happened.
            _b_tick_cls = _pk_pulse(_b["ticker"], _b_live_price) if _b_live_price else ""
            st.markdown(
                f'<div class="pick-card{_b_tick_cls}" style="background:{_card_grad};'
                f'border-left:4px solid {_card_border};border-radius:10px;padding:11px 14px;margin-bottom:6px">'
                f'<div style="display:flex;justify-content:space-between;align-items:center">'
                f'<span><span style="font-size:16px;font-weight:700;color:var(--ink)">{_bl_hover}</span>{_grade_html}{_tier_badge}{_fv_pill}</span>'
                f'<span style="font-size:13px;font-weight:700;color:{_score_color}">{_b["score"]:.0f}/90 · '
                f'{_display_label(_b["action"])}</span>'
                f'</div>'
                f'<div style="font-size:11px;color:{_tt_col};font-weight:600;margin-top:3px">{_tt_emo} {_tt_lbl} setup</div>'
                f'<div style="font-size:12px;color:var(--ink-mid);margin-top:2px">{_b["headline"]}</div>'
                + (f'<div style="font-size:11px;color:var(--dim);margin-top:4px">'
                   f'Entry ₹{_b_lvl["entry"]:,.2f} · SL ₹{_b_lvl["sl"]:,.2f} · TP ₹{_b_lvl["tp"]:,.2f} '
                   f'{_b_live_span}</div>'
                   if _b_lvl["entry"] else "")
                + (f'<div style="font-size:11px;color:var(--azure);margin-top:2px">'
                   f'⏱ {_b.get("horizon")}'
                   + (f' · {_horizon_countdown(_b.get("valid_until"))}' if _b.get("valid_until") else '')
                   + '</div>'
                   if _b.get("horizon") else "")
                # P2 · "5 above the fold" — R:R and freshness stamps are
                # secondary; demoted into a native <details> disclosure.
                + (f'<details class="pick-more"><summary>R:R &amp; freshness</summary>'
                   f'{_b_rr_html}{_stamp_html}</details>')
                + '</div>',
                unsafe_allow_html=True,
            )
            if _b_lvl["entry"]:
                # Paper trade uses the RE-ANCHORED levels — a live-price
                # entry with SL/TP at the stale-scored values would set stops
                # in the wrong place from the moment the trade opens.
                _paper_trade_popover(
                    _b["ticker"], _b_lvl["entry"], _b_lvl["sl"], _b_lvl["tp"],
                    reason=f"Top Pick: {_b['headline'][:55]}",
                    key=f"cc_pick_{_b['ticker']}",
                    label=f"📌 Paper Trade {_bl}",
                )
            render_pick_analysis(_b, key_prefix=f"cc_buy_{_b['ticker']}")
    with _pk_sell:
        st.markdown(
            '<div class="t-h2" style="margin:8px 0 6px 0">'
            '🔴 Weakest trends</div>',
            unsafe_allow_html=True,
        )
        if not _picks["sells"]:
            st.caption("No names in the weak/broken-trend band in today's scan.")
        for _sv in _picks["sells"]:
            _svl = _sv["ticker"].replace(".NS", "")
            # FIX CC-LIVE1: same bounded pattern as the Buy loop above —
            # price only, from the one shared _pk_live batch fetch already
            # done for every ticker on screen this rerun.
            _sv_lp = _pk_live.get(_sv["ticker"])
            if _sv_lp:
                _sv_up = (_sv_lp.get("chg_pct") or 0) >= 0
                _sv_pc = "var(--bull)" if _sv_up else "var(--bear)"
                _sv_live_html = (
                    f'<div style="font-size:11px;margin-top:4px">'
                    f'<span style="color:{_sv_pc};font-weight:700">₹{_sv_lp["price"]:,.2f} '
                    f'{"▲" if _sv_up else "▼"}{abs(_sv_lp.get("chg_pct") or 0):.2f}%</span> '
                    f'<span style="color:var(--faint)">live</span></div>'
                )
            else:
                _sv_live_html = ""
            _sv_live_price = float(_sv_lp["price"]) if _sv_lp else None
            _sv_tick_cls = _pk_pulse(_sv["ticker"], _sv_live_price) if _sv_live_price else ""
            # UX2 · hover preview — same pattern as the Buy loop above.
            _sv_hover_svg = _sparkline_svg(_pk_spark.get(_sv["ticker"]) or [])
            _svl_hover = ticker_hover_wrap(
                _svl,
                sparkline_svg=_sv_hover_svg,
                price=_sv_live_price,
                chg_pct=(_sv_lp.get("chg_pct") if _sv_lp else None),
                score=_sv.get("score"),
                sector=_sv.get("sector", ""),
            )
            st.markdown(
                f'<div class="pick-card{_sv_tick_cls}" style="background:linear-gradient(135deg,var(--sunken),var(--sunken));'
                f'border-left:4px solid var(--bear);border-radius:10px;padding:11px 14px;margin-bottom:6px">'
                f'<div style="display:flex;justify-content:space-between;align-items:center">'
                f'<span style="font-size:16px;font-weight:700;color:var(--ink)">{_svl_hover}</span>'
                f'<span style="font-size:13px;font-weight:700;color:var(--bear)">{_sv["score"]:.0f}/90 · '
                f'{_display_label(_sv["action"])}</span>'
                f'</div>'
                f'<div style="font-size:12px;color:var(--ink-mid);margin-top:3px">{_sv["headline"]}</div>'
                f'{_sv_live_html}'
                + '</div>',
                unsafe_allow_html=True,
            )
            render_pick_analysis(_sv, key_prefix=f"cc_sell_{_sv['ticker']}")

    # UX3 · freeze this pass' seen set. Symbols that dropped out of _picks
    # (e.g. a scan reshuffled the buy list) don't survive to next tick.
    _pk_pulse_commit()


_sec_tuple = _sector_ranks_tuple()
st.session_state["_sec_ranks_cache"] = _sec_tuple   # share with watchlist
_render_top_picks_section(_cc_vix_r, _sec_tuple)

st.markdown("---")

# ── PAPER TRADES OVERVIEW (quick view; moved below Top Picks per the mockup) ─────────────────────────────────
try:
    import trade_store as _pto_ts
    _pto_open  = _pto_ts.fetch_open()
    _pto_accts = _pto_ts.list_accounts()
    _pto_all   = (pd.concat([_pto_ts.load_by_account(_a) for _a in _pto_accts],
                            ignore_index=True)
                  if _pto_accts else pd.DataFrame())

    _pto_n = 0 if (_pto_open is None or _pto_open.empty) else len(_pto_open)

    _pto_unreal = 0.0
    if _pto_n:
        _pto_syms = tuple(_pto_open["ticker"].tolist())
        _pto_lp   = _portfolio_live_prices(_pto_syms)
        for _, _por in _pto_open.iterrows():
            _pep = float(_por.get("price", 0) or 0)
            _pqt = int(_por.get("quantity", 0) or 0)
            _pcur = _pto_lp.get(str(_por["ticker"]), {}).get("price", _pep)
            _pto_unreal += (_pcur - _pep) * _pqt

    _pto_real, _pto_wins, _pto_tot = 0.0, 0, 0
    if not _pto_all.empty and "status" in _pto_all.columns:
        _pto_closed = _pto_all[_pto_all["status"].isin(["CLOSED", "STOPPED"])]
        if not _pto_closed.empty and "pnl" in _pto_closed.columns:
            _pnl_series = _pto_closed["pnl"].fillna(0)
            _pto_real = float(_pnl_series.sum())
            _pto_tot  = int(len(_pto_closed))
            _pto_wins = int((_pnl_series > 0).sum())
    _pto_wr = (_pto_wins / _pto_tot * 100) if _pto_tot else 0.0

    # Task 1.3: this block used a bespoke _pto_cell helper + a raw
    # .glass-panel wrapper with drifting hex ("var(--ink)", "var(--faint)",
    # "var(--faint)", "var(--dim)"). Migrated to the shared panel() + stat()
    # components so it renders in the same visual language as every
    # other card in the app and pulls color from CSS custom properties.
    from dashboard.shared.ui_components import panel as _panel, stat as _stat
    _u_tone = "bull" if _pto_unreal >= 0 else "bear"
    _r_tone = "bull" if _pto_real   >= 0 else "bear"
    _wr_tone = ("bull" if _pto_wr >= 50 else
                "amber" if _pto_wr >= 35 else "bear")
    _pto_body = (
        '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));'
        'gap:14px 22px">'
        + _stat("Open Positions", f"{_pto_n}", tone="neutral", align="center")
        + _stat("Unrealised P&amp;L", f"Rs.{_pto_unreal:+,.0f}",
                sub="live prices", tone=_u_tone, align="center")
        + _stat("Realised P&amp;L", f"Rs.{_pto_real:+,.0f}",
                sub=f"{_pto_tot} closed", tone=_r_tone, align="center")
        + _stat("Win Rate", f"{_pto_wr:.0f}%",
                sub=(f"{_pto_wins}/{_pto_tot} wins" if _pto_tot else "no closed trades"),
                tone=_wr_tone, align="center")
        + '</div>'
    )
    st.markdown(
        _panel(_pto_body, kind="glass", tone="neutral",
               title="Paper Trades Overview", margin="0 0 14px 0"),
        unsafe_allow_html=True,
    )
except Exception as _pto_e:
    st.caption(f"⚠️ Paper trades overview unavailable ({_pto_e}).")


@st.fragment
def _render_open_positions_section():
    """Own fragment so Close Now / autoclose-toggle clicks only rerun this section, not the whole Command Centre page."""
    # ── 3. OPEN POSITION ALERTS + AUTO-CLOSE ───────────────────────────────────
    _cc_h1, _cc_h2 = st.columns([5, 2])
    _cc_h1.markdown(
        '<div class="t-h1" style="margin:6px 0 4px 0">'
        '📌 Open Positions</div>',
        unsafe_allow_html=True,
    )
    with _cc_h2:
        _cc_autoclose = st.toggle(
            "🤖 Auto-close CNC on SL/TP",
            value=st.session_state.get("auto_close_on", False),
            key="cc_autoclose_toggle",
            help="When ON, CNC (delivery) paper trades that hit their target or stop-loss are "
                 "closed automatically on page load (during market hours only, on live prices). "
                 "MIS (intraday) positions are ALWAYS squared off at 15:15 regardless of this toggle. "
                 "Real broker holdings are never auto-traded — only alerted.",
        )
        st.session_state["auto_close_on"] = _cc_autoclose
    
    _cc_sq_banner_shown = False
    if _is_squareoff_time():
        _sq_all_closed = _auto_close_breached()
        _sq_mis_closed = [c for c in _sq_all_closed if c["type"] == "squareoff"]
        if _sq_mis_closed:
            _render_autoclose_banner(_sq_mis_closed)
            # BUGFIX: only live prices need to be re-fetched after an auto-close —
            # a blanket st.cache_data.clear() here also nuked Top Picks, watchlist
            # scores, and VIX info on every squareoff event.
            _portfolio_live_prices.clear()
            _cc_sq_banner_shown = True
    
    if _cc_autoclose:
        _cc_all_closed = _auto_close_breached()
        _cc_sltp_closed = [c for c in _cc_all_closed if c["type"] in ("target", "stop")]
        if _cc_sltp_closed:
            _render_autoclose_banner(_cc_sltp_closed)
            _portfolio_live_prices.clear()
    
    _cc_open_df = pd.DataFrame()
    try:
        _cc_open_df = _store.fetch_open()
    except Exception as _e:
        st.caption(f"⚠️ Couldn't load open paper positions ({_e}).")
    
    if _cc_open_df.empty:
        # F5 empty-state kit -- see dashboard/shared/ui_components.py.
        from dashboard.shared.ui_components import empty_state as _empty_cc_pt
        st.markdown(
            _empty_cc_pt(
                title="No open paper positions",
                hint="Use the **Paper Trades** page, or click "
                     "**Paper Trade** on any setup card below.",
                icon="📌",
            ),
            unsafe_allow_html=True,
        )
    else:
        _cc_syms = tuple(_cc_open_df["ticker"].tolist())
        _cc_lp   = _portfolio_live_prices(_cc_syms)
    
        _cc_alerts, _cc_normal_pos = [], []
        for _, _ccr in _cc_open_df.iterrows():
            _ck  = _ccr["ticker"]
            _cep = float(_ccr.get("price", 0) or 0)
            _cqt = int(  _ccr.get("quantity", 0) or 0)
            _csl = float(_ccr.get("sl", 0) or 0) or None
            _ctp = float(_ccr.get("tp", 0) or 0) or None
            _clp_d = _cc_lp.get(_ck, {})
            _ccur  = _clp_d.get("price", _cep)
            _cunr  = (_ccur - _cep) * _cqt
            _cunr_pct = (_ccur / _cep - 1) * 100 if _cep > 0 else 0
    
            _cst = "normal"
            if _ctp and _ccur >= _ctp:       _cst = "target_hit"
            elif _csl and _ccur <= _csl:     _cst = "sl_hit"
            elif abs(_cunr_pct) >= 5:        _cst = "big_move"
    
            _entry_d = dict(id=int(_ccr["id"]), ticker=_ck,
                            account=str(_ccr.get("account","My Account")),
                            ep=_cep, cur=_ccur, qty=_cqt, sl=_csl, tp=_ctp,
                            unr=_cunr, unr_pct=_cunr_pct, status=_cst)
            (_cc_alerts if _cst != "normal" else _cc_normal_pos).append(_entry_d)
    
        if _cc_alerts:
            st.markdown("**⚠️ These positions need your attention:**")
    
        for _pos in _cc_alerts + _cc_normal_pos:
            _pbdr = {"target_hit": "var(--bull)", "sl_hit": "var(--bear)", "big_move": "var(--amber)",
                     "normal": "var(--accent)"}.get(_pos["status"], "var(--accent)")
            _pbg  = {"target_hit": "var(--sunken)", "sl_hit": "var(--sunken)",  "big_move": "var(--sunken)",
                     "normal": "var(--surface)"}.get(_pos["status"], "var(--surface)")
            _purc = "var(--bull)" if _pos["unr"] >= 0 else "var(--bear)"
            _palert = {
                "target_hit": f"🎯 Target reached — price is at or above your target",
                "sl_hit":     f"🚨 Stop-loss breached — price is below your stop",
                "big_move":   f"{'📈' if _pos['unr_pct']>0 else '📉'} Large move since entry",
            }.get(_pos["status"], "")
    
            _pc1, _pc2 = st.columns([5, 1])
            with _pc1:
                st.markdown(
                    f'<div style="background:{_pbg};border-left:5px solid {_pbdr};'
                    f'border-radius:10px;padding:11px 15px;margin-bottom:6px">'
                    f'<div style="display:flex;justify-content:space-between;align-items:center">'
                    f'<div><span style="font-size:16px;font-weight:700;color:var(--ink)">'
                    f'{_pos["ticker"].replace(".NS","")}</span>'
                    f'<span style="font-size:11px;color:var(--dim);margin-left:8px">📂 {_pos["account"]}</span>'
                    f'<span style="font-size:12px;color:var(--dim);margin-left:8px">'
                    f'Entry ₹{_pos["ep"]:,.2f} → Now ₹{_pos["cur"]:,.2f}</span></div>'
                    f'<div style="font-size:16px;font-weight:700;color:{_purc}">'
                    f'₹{_pos["unr"]:+,.0f} ({_pos["unr_pct"]:+.1f}%)</div></div>'
                    + (f'<div style="font-size:13px;color:var(--ink-mid);margin-top:4px">{_palert}</div>' if _palert else '')
                    + '</div>',
                    unsafe_allow_html=True,
                )
            with _pc2:
                if _pos["status"] in ("target_hit", "sl_hit"):
                    if st.button("Close Now", key=f"cc_cl_{_pos['id']}",
                                 width="stretch", type="primary"):
                        paper_close_trade(_pos["id"], _pos["cur"],
                                          "Closed via Command Centre")
                        # BUGFIX: closing one position only needs fresh live
                        # prices for the remaining open positions — it doesn't
                        # need to invalidate Top Picks or watchlist scores too.
                        _portfolio_live_prices.clear()
                        st.rerun()
    

_render_open_positions_section()

# FIX CC-TRIM — the bottom "Watchlist — What to Do Today" section was removed
# from this page on request. It duplicated 14_my_watchlist.py, which is where
# per-name scoring lives exclusively. Home page below is Top Picks + Open
# Positions + Background Alerts.

# ── 5. BACKGROUND TELEGRAM ALERTS (viewer) ─────────────────────────────────
st.markdown("---")
with st.expander("🔔 Background Alerts (Telegram) — fire even when this app is closed", expanded=False):
    st.caption(
        "A GitHub Actions job checks these every 15 min during market hours and "
        "messages you on Telegram. Edit **data/alerts.csv** in your GitHub repo to "
        "change them. Full setup: **alerts/README.md**."
    )
    try:
        import pathlib as _alp
        _alerts_path = _alp.Path(_ROOT) / "data" / "alerts.csv"
        if _alerts_path.exists():
            _al_df = pd.read_csv(_alerts_path)
            _act_df = _al_df[_al_df["enabled"].astype(str).isin(["1", "True", "true"])]
            st.markdown(f"**{len(_act_df)} active** of {len(_al_df)} configured price alerts:")
            if not _act_df.empty:
                _al_show = _act_df[["ticker", "condition", "level", "note"]].copy()
                _al_show.columns = ["Stock", "When price goes", "Level (₹)", "Note"]
                st.dataframe(_al_show, hide_index=True, width="stretch")
            else:
                from dashboard.shared.ui_components import empty_state as _empty_al
                st.markdown(
                    _empty_al(
                        title="No active price alerts",
                        hint="All rows are examples (enabled=0). Set "
                             "`enabled=1` on a row in `data/alerts.csv` to "
                             "activate it.",
                        icon="🔔",
                    ),
                    unsafe_allow_html=True,
                )
        else:
            from dashboard.shared.ui_components import empty_state as _empty_al2
            st.markdown(
                _empty_al2(
                    title="No alerts.csv found yet",
                    hint="Create `data/alerts.csv` with your desired price "
                         "levels to start getting Telegram notifications.",
                    icon="📄",
                ),
                unsafe_allow_html=True,
            )
    except Exception as _ale:
        st.caption(f"Could not read alerts.csv: {_ale}")

    st.markdown(
        "**Also alerted automatically:** 🔴 VIX entering fear/panic · 📉 Nifty breaking into a downtrend.  \n"
        "**One-time setup:** create a Telegram bot via @BotFather, then add "
        "`TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` as GitHub Actions secrets "
        "(Settings → Secrets and variables → Actions)."
    )

# ═══════════════════════════════════════════════════════════════════════════════
# PAGE 1 — MY PORTFOLIO
# ═══════════════════════════════════════════════════════════════════════════════
