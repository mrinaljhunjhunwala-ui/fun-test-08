"""Backtest - NSE Smart Investor (multipage page; body verbatim from app.py).

FIXES applied in this revision
───────────────────────────────
B1  In-app backtest no longer blocks the Streamlit request thread. The run
    is dispatched to a background ThreadPoolExecutor worker. Progress and
    partial results are written into st.session_state["bt_partial"] every
    ticker so the UI stays live. A "Cancel" button sets a threading.Event
    that the worker checks between tickers. A timeout of 20 min is enforced
    after which the partial results are surfaced automatically.

B2  Unified display path — the top of the page now also loads into
    session_state["bt_result"] when portfolio_results.csv is present, so
    the detailed sortable table always renders regardless of whether the
    result came from a file or an in-app run.

B3  Normalised comparison now aligns all tickers to their common date range
    before dividing by the first value, so every line starts from the same
    baseline date and the comparison is apples-to-apples.

B4  In-app run now writes to portfolio_results.csv (same filename the loader
    reads) instead of backtest_results.csv, so the top summary table
    reflects the most recent run immediately after completion.

FIX BT1  Styler.background_gradient() requires matplotlib as an optional
    dependency — pandas imports it lazily only when that method is called,
    so this was invisible in local testing (matplotlib happened to be
    present in the sandbox) and crashed with an ImportError on Streamlit
    Cloud, which doesn't have it (not in requirements.txt, and nothing else
    in this app needs it). Replaced with a small hand-rolled Red-Yellow-
    Green interpolation (chart_helpers.rdylgn_bg) that needs only stdlib — same
    visual result, no new dependency to track.
"""

import os, sys
import logging

_log = logging.getLogger("dashboard.backtest")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import streamlit as st
from dashboard.shared.design import apply_design
from dashboard.shared.nav import render_sidebar
from dashboard.shared.chart_helpers import render_top_bar
from dashboard.shared.tokens import COLORS as _TK  # noqa: E402
from dashboard.shared.disclosures import (
    render_survivorship_notice,
    render_backtest_assumptions,
)

import os
import threading
import time
import concurrent.futures
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
import sys

from dashboard.shared.design import apply_design
from dashboard.shared.cache import load_ticker_df
from dashboard.shared.chart_helpers import _ROOT, render_top_bar
from dashboard.shared.table_styles import arrow_fmt, pnl_styler
from backtest.display import integer_columns, signed_columns  # noqa: E402

apply_design()
render_sidebar(current="Backtest")
render_top_bar()

# ─────────────────────────────────────────────────────────────────────────────
st.markdown('<h1 class="page-title-serif">Backtest <em>Results</em></h1>', unsafe_allow_html=True)

st.caption(
    "Historical performance of the rule-based **RSI-MACD** and **Momentum** "
    "strategies in `strategies/` — how would those rules have done in the past? "
    "This is **not** a test of the 0–90 composite score or of the verdicts shown "
    "elsewhere in the app; for how the app's own logged verdicts played out, see "
    "**Verdict Calibration**. Costs: every fill priced with Indian delivery charges "
    "(STT both legs, stamp duty, exchange/SEBI, GST, ₹20 DP, Angel One brokerage; ~0.26% "
    "round trip on a ₹2L order, more on small orders)."
)

render_survivorship_notice()
render_backtest_assumptions()


def load_backtest_csv(path: str = "portfolio_results.csv") -> pd.DataFrame:
    if os.path.exists(path):
        try:
            return pd.read_csv(path, index_col=0)
        except Exception as e:
            _log.warning("load_backtest_csv: %s exists but failed to parse: %s", path, e)
            return pd.DataFrame()
    return pd.DataFrame()


def _bt_table_styler(frame: pd.DataFrame, ret_col):
    """P2 · P&L table pattern for backtest results — rows tinted by return,
    ▲/▼ + sign colour on return columns (bold beyond ±10%), 2dp elsewhere.
    Replaces the RdYlGn gradient, which relied on red/green hue alone."""
    # Audit 2026-09-24: signed columns now include portfolio.py's "B&H (%)"
    # and "Alpha (%)"; count columns (# Trades) render as integers.
    _signed = signed_columns(frame, ret_col)
    _ints = set(integer_columns(frame))
    _fmts = {}
    for c in frame.columns:
        if not pd.api.types.is_numeric_dtype(frame[c]):
            continue
        if c in _signed:
            _fmts[c] = arrow_fmt(2, "%")
        elif c in _ints:
            _fmts[c] = "{:,.0f}"
        else:
            _fmts[c] = "{:.2f}"
    return pnl_styler(frame, tint_col=ret_col or "", signed_cols=_signed,
                      formats=_fmts, bold_at={c: 10.0 for c in _signed},
                      full_at=30.0)


# ─────────────────────────────────────────────────────────────────────────────
# FIX B2: unified display — always load file result into session_state so
# the detailed table renders whether data came from file or in-app run.
# ─────────────────────────────────────────────────────────────────────────────
_file_df = load_backtest_csv()
if not _file_df.empty and "bt_result" not in st.session_state:
    st.session_state["bt_result"]       = _file_df
    st.session_state["bt_result_label"] = "Loaded from portfolio_results.csv"

df = st.session_state.get("bt_result", pd.DataFrame())

if not df.empty:
    r_col = next((c for c in ["Return (%)", "Return(%)"]   if c in df.columns), None)
    s_col = next((c for c in ["Sharpe", "Sharpe Ratio"]    if c in df.columns), None)
    t_col = next((c for c in ["# Trades", "Trades"]        if c in df.columns), None)

    bt1, bt2, bt3, bt4 = st.columns(4)
    bt1.metric("Tickers Tested", len(df))
    bt2.metric("Avg Return",  f"{df[r_col].mean():.2f}%" if r_col else "—")
    bt3.metric("Avg Sharpe",  f"{df[s_col].mean():.2f}"  if s_col else "—")
    bt4.metric("Total Trades",f"{df[t_col].sum():,.0f}"  if t_col else "—")

    _styler = _bt_table_styler(df, r_col)
    st.dataframe(
        _styler,
        use_container_width=True,
    )

    if r_col:
        fig = px.bar(
            df.reset_index(), x=df.index, y=r_col,
            color=r_col, color_continuous_scale="RdYlGn",
            title="Return (%) per Ticker",
            labels={r_col: "Return (%)"},
        )
        fig.update_layout(
            template="nse_pro", height=400,
            margin=dict(l=0, r=0, t=40, b=0),
        )
        st.plotly_chart(fig, use_container_width=True)
else:
    # F5 empty-state kit -- see dashboard/shared/ui_components.py.
    from dashboard.shared.ui_components import empty_state as _empty
    st.markdown(
        _empty(
            title="No backtest results yet",
            hint="Use the <b>Run a Backtest</b> section below, or run "
                 "<code>python main.py --mode backtest --portfolio "
                 "--index nifty50</code> from the terminal. Results will "
                 "appear here automatically.",
            icon="🧪",
        ),
        unsafe_allow_html=True,
    )

# ─────────────────────────────────────────────────────────────────────────────
# FIX B1 — Non-blocking in-app backtest runner
# ─────────────────────────────────────────────────────────────────────────────
st.markdown("---")
st.markdown(
    '<div class="t-h2" style="margin:14px 0 6px 0">'
    '⚡ Run a Backtest — in the app</div>',
    unsafe_allow_html=True,
)

_bt_c1, _bt_c2, _bt_c3, _bt_c4 = st.columns([2, 2, 1, 1])
with _bt_c1:
    _bt_uni = st.selectbox(
        "Universe",
        ["Nifty 50", "Nifty 100", "Nifty 200", "Nifty 500"],
        key="bt_uni",
    )
with _bt_c2:
    _bt_strat = st.selectbox(
        "Strategy", ["RSI + MACD", "Momentum"], key="bt_strat"
    )
with _bt_c3:
    _bt_period = st.selectbox(
        "Period", ["6mo", "1y", "2y", "3y"], index=2, key="bt_period",
        help=(
            "How much price history to backtest over. Shorter periods run "
            "faster but give indicators like the 200-day SMA less warmup "
            "room, so the earliest trades in the window may be less "
            "reliable. 2y+ is the safest choice if you're unsure."
        ),
    )
with _bt_c4:
    st.write("")
    _bt_run = st.button(
        "🚀 Run", type="primary", key="bt_run", use_container_width=True
    )

_uni_map = {
    "Nifty 50":  "nifty50",  "Nifty 100": "nifty100",
    "Nifty 200": "nifty200", "Nifty 500": "nifty500",
}
_est = {
    "Nifty 50":  "~1-2 min", "Nifty 100": "~3-4 min",
    "Nifty 200": "~6-8 min", "Nifty 500": "~10-15 min",
}[_bt_uni]
st.caption(
    f"⏱️ Estimated run time: **{_est}**. "
    "Runs in the background — you can watch live progress below. "
    "A **Cancel** button appears once the run starts."
)

# ── Session-state keys used by the background worker ─────────────────────
# bt_running    : bool  — True while the worker thread is active
# bt_cancel     : threading.Event — set by the Cancel button
# bt_partial    : list of result dicts written by the worker
# bt_progress   : (done, total, current_ticker) tuple for the progress bar
# bt_result     : final pd.DataFrame (set when the run completes)
# bt_result_label: human-readable label for the results header

if "bt_cancel" not in st.session_state:
    st.session_state["bt_cancel"]   = threading.Event()
if "bt_running" not in st.session_state:
    st.session_state["bt_running"]  = False
if "bt_partial" not in st.session_state:
    st.session_state["bt_partial"]  = []
if "bt_progress" not in st.session_state:
    st.session_state["bt_progress"] = (0, 1, "")


def _run_backtest_worker(
    tickers: list,
    strat_cls,
    period: str,
    cost,                     # trading.costs per-fill callable or per-side rate
    cancel_event: threading.Event,
    partial_results: list,      # shared list — worker appends, UI reads
    progress_holder: list,      # [done, total, current_ticker]
):
    """Background worker — runs one ticker at a time, writes partial results.

    FIX B6 — throttling + failure visibility. This loop used to hammer
    fetch_single() for every ticker back-to-back with zero delay between
    requests. Yahoo's undocumented chart API and Stooq's CSV export both
    rate-limit/block abusive request patterns, and a cloud-hosted app
    sharing an IP range with many other users (Streamlit Community Cloud)
    is already more exposed to that than a residential IP — see
    data/fetcher.py's own module docs on Stooq's cloud-IP blocking. Scanning
    a 500-ticker universe with no delay at all is exactly the kind of
    pattern that trips it partway through, which matches the previously-
    observed failure logs (HTML-not-CSV from Stooq, connection-refused and
    handshake-timeout errors from both Stooq and Yahoo) for the majority of
    a run. A small per-ticker delay costs a modest amount of wall-clock
    time but meaningfully reduces how often that happens.

    Previously, ANY failed ticker (fetch failure OR insufficient history)
    was silently dropped from the final results with zero indication to
    the person of how many tickers failed or why — a 500-ticker Nifty 500
    run that only fetched 5 tickers successfully looked identical, from the
    results table alone, to a run that had genuinely only found 5 valid
    setups. Each failure now records a short, categorized reason so the UI
    can show "487/500 failed: data unavailable" instead of just a small,
    unexplained table.
    """
    from data.fetcher import fetch_single as _bt_fs
    from utils.indicators import add_all_indicators as _bt_ind
    from backtesting import Backtest as _BT

    total = len(tickers)
    for i, t in enumerate(tickers):
        if cancel_event.is_set():
            break
        progress_holder[0] = i
        progress_holder[2] = t.replace(".NS", "")
        try:
            _bd = _bt_fs(t, period=period)
            _bd = _bt_ind(_bd)
            _bd = _bd.dropna(axis=1, how="all")
            _bd = _bd.dropna(subset=["Open", "High", "Low", "Close", "Volume"])
            if len(_bd) >= 60:
                _stats = _BT(
                    _bd, strat_cls,
                    cash=1_000_000, commission=cost, exclusive_orders=True,
                ).run()
                partial_results.append({
                    "Ticker":           t.replace(".NS", ""),
                    "Return (%)":       round(float(_stats["Return [%]"]),            2),
                    "Buy & Hold (%)":   round(float(_stats["Buy & Hold Return [%]"]), 2),
                    "Sharpe":           round(float(_stats["Sharpe Ratio"]),           2),
                    "Max Drawdown (%)": round(float(_stats["Max. Drawdown [%]"]),      2),
                    "Win Rate (%)":     round(float(_stats["Win Rate [%]"]),           2),
                    "# Trades":         int(_stats["# Trades"]),
                    "_fail_reason":     None,
                })
            else:
                # FIX B6: distinct from a fetch failure — data was fetched
                # but too short a history to backtest reliably.
                partial_results.append({
                    "Ticker":  t.replace(".NS", "") + " ⚠️",
                    "Return (%)": None, "Buy & Hold (%)": None,
                    "Sharpe": None,     "Max Drawdown (%)": None,
                    "Win Rate (%)": None, "# Trades": 0,
                    "_fail_reason": "insufficient history (<60 bars)",
                })
        except Exception as _e:
            # Record skip without crashing the worker
            _log.debug("backtest sweep: %s skipped: %s", t, _e)
            partial_results.append({
                "Ticker":  t.replace(".NS", "") + " ⚠️",
                "Return (%)": None, "Buy & Hold (%)": None,
                "Sharpe": None,     "Max Drawdown (%)": None,
                "Win Rate (%)": None, "# Trades": 0,
                "_fail_reason": f"fetch/data error: {type(_e).__name__}",
            })
        # FIX B6: brief pause between tickers — see docstring above.
        if not cancel_event.is_set() and i < total - 1:
            time.sleep(0.35)
    progress_holder[0] = total  # signal completion


if _bt_run and not st.session_state.get("bt_running", False):
    try:
        from data.universe import get_universe
        from strategies.rsi_macd import RSIMACDStrategy
        from strategies.momentum import MomentumStrategy
        try:
            # Same per-fill cost model as backtest/runner.py (trading/costs.py).
            from backtest.runner import COMMISSION as _BT_COST
        except Exception as e:
            _log.debug("backtest COMMISSION import failed, using the flat cost fallback: %s", e)
            from trading.costs import representative_round_trip_pct as _rt_pct
            _BT_COST = _rt_pct() / 2

        _strat_cls   = RSIMACDStrategy if _bt_strat.startswith("RSI") else MomentumStrategy
        _bt_tickers  = get_universe(_uni_map[_bt_uni])

        # Reset shared state
        _cancel_ev   = threading.Event()
        _partial     = []
        _progress    = [0, len(_bt_tickers), ""]

        st.session_state["bt_cancel"]       = _cancel_ev
        st.session_state["bt_partial"]      = _partial
        st.session_state["bt_progress"]     = _progress
        st.session_state["bt_running"]      = True
        st.session_state["bt_result_label"] = (
            f"{_bt_strat} · {_bt_uni} · {_bt_period}"
        )

        # Launch background thread
        _executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        # FIX B7: capture the real Future — see the fix at the finish-check
        # below for why this matters.
        _future = _executor.submit(
            _run_backtest_worker,
            _bt_tickers, _strat_cls, _bt_period, _BT_COST,
            _cancel_ev, _partial, _progress,
        )
        st.session_state["bt_executor"] = _executor
        st.session_state["bt_future"]   = _future
        st.rerun()

    except Exception as _bt_err:
        st.error(f"Backtest failed to start: {_bt_err}")

# ── Live progress display (shown while bt_running is True) ────────────────
# FIX B5 (perf) — this used to poll with a blocking time.sleep(3) followed by
# st.rerun(), which re-executes the ENTIRE page (imports, sidebar, design,
# everything above) every 3 seconds for the whole duration of a backtest run.
# Same anti-pattern already fixed in Command Centre's Top Picks and
# Tomorrow's Watchlist — applying the same @st.fragment(run_every=3) fix
# here: Streamlit reruns just this fragment on its own timer, no blocking
# sleep on the main thread. st.rerun() (full-app scope by default, even
# inside a fragment) is still used once, to escape the fragment when the
# backtest actually finishes and render the final results below.
if st.session_state.get("bt_running", False):

    @st.fragment(run_every=3)
    def _bt_poll_fragment():
        _prog   = st.session_state["bt_progress"]
        _done   = _prog[0]
        _total  = max(_prog[1], 1)
        _ticker = _prog[2]
        _partial = st.session_state.get("bt_partial", [])

        # Cancel button
        if st.button("🛑 Cancel backtest", key="bt_cancel_btn"):
            st.session_state["bt_cancel"].set()
            st.session_state["bt_running"] = False
            st.warning("Backtest cancelled — partial results shown below.")
            st.rerun()

        _pct = _done / _total
        st.progress(_pct, text=f"Running {_ticker} ({_done}/{_total})")

        # FIX B7 — CRITICAL: this used to check
        # `all(f.done() for f in getattr(_executor, "_futures", []))`.
        # concurrent.futures.ThreadPoolExecutor has NO "_futures" attribute
        # at all (verified directly: hasattr(ThreadPoolExecutor(), "_futures")
        # is False) — so getattr(...)  ALWAYS silently fell through to its
        # [] default, and all(... for f in []) is vacuously True on an empty
        # iterable in Python. That made "_executor is not None and all(...)"
        # unconditionally True the instant a run started, so _finished was
        # True on literally the FIRST poll of this fragment — about 3
        # seconds in — regardless of how many tickers had actually been
        # processed. The run then got marked finished, bt_running was set
        # False, and this fragment (guarded by "if bt_running") never polled
        # again — silently freezing the displayed results at whatever tiny
        # handful of tickers (2-4, given per-ticker latency) had completed
        # in that first ~3 seconds, while the real background thread kept
        # running to completion with nothing left to ever read or display
        # its results. This is what was actually causing "only 3-5 stocks"
        # regardless of universe size — not a data-availability problem.
        # Fixed by checking the real Future object returned by submit()
        # (captured in bt_future, see where the worker is launched above)
        # instead of introspecting nonexistent executor internals.
        _future = st.session_state.get("bt_future")
        _finished = _done >= _total or (_future is not None and _future.done())

        if _finished:
            st.session_state["bt_running"] = False
            _bt_rows = [r for r in _partial if r.get("Return (%)") is not None]
            _bt_fails = [r for r in _partial if r.get("Return (%)") is None]
            # FIX B6: store the failure breakdown so it survives this
            # fragment's own rerun below and can be shown in the main page
            # body (fragments render in an isolated container, so anything
            # meant to persist on the page after the fragment finishes
            # needs to go through session_state, same as bt_result itself).
            st.session_state["bt_fail_count"] = len(_bt_fails)
            st.session_state["bt_fail_total"] = len(_partial)
            _reason_counts: dict = {}
            for r in _bt_fails:
                _reason = r.get("_fail_reason") or "unknown"
                _reason_counts[_reason] = _reason_counts.get(_reason, 0) + 1
            st.session_state["bt_fail_reasons"] = _reason_counts

            if _bt_rows:
                _bt_res = pd.DataFrame(_bt_rows).drop(
                    columns=["_fail_reason"], errors="ignore"
                ).set_index("Ticker")
                st.session_state["bt_result"] = _bt_res
                # FIX B4: write to portfolio_results.csv so the top table refreshes
                try:
                    _bt_res.to_csv("portfolio_results.csv")
                except Exception as _csv_e:
                    import logging; logging.getLogger("dashboard.backtest").warning("Could not write portfolio_results.csv: %s", _csv_e)
                if _bt_fails:
                    st.warning(
                        f"✅ Backtested {len(_bt_res)}/{len(_partial)} stocks "
                        f"({st.session_state.get('bt_result_label','')}) — "
                        f"**{len(_bt_fails)} couldn't be backtested**, see the "
                        "breakdown below the results."
                    )
                else:
                    st.success(
                        f"✅ Backtested {len(_bt_res)} stocks "
                        f"({st.session_state.get('bt_result_label','')})."
                    )
            else:
                st.warning(
                    f"No results — all {len(_partial)} tickers failed. "
                    "See the breakdown below for why."
                )
            st.rerun()
        # else: still running — no sleep needed, run_every=3 handles the
        # next check on its own timer.

    _bt_poll_fragment()

# ── Show last in-app / file backtest result ───────────────────────────────
if "bt_result" in st.session_state and not st.session_state.get("bt_running", False):
    _bt_res = st.session_state["bt_result"]
    if not _bt_res.empty:
        st.markdown(
            f"#### 📊 Results — {st.session_state.get('bt_result_label','')}"
        )
        _rb1, _rb2, _rb3, _rb4 = st.columns(4)
        _rb1.metric("Stocks", len(_bt_res))

        _r_col2 = next(
            (c for c in ["Return (%)", "Return(%)"] if c in _bt_res.columns), None
        )
        _s_col2 = next(
            (c for c in ["Sharpe", "Sharpe Ratio"] if c in _bt_res.columns), None
        )
        _bh_col = next(
            (c for c in ["Buy & Hold (%)", "Buy & Hold Return (%)"]
             if c in _bt_res.columns), None,
        )

        if _r_col2:
            _avg_ret = _bt_res[_r_col2].dropna().mean()
            _rb2.metric(
                "Avg Return", f"{_avg_ret:.1f}%",
                delta_color="normal" if _avg_ret >= 0 else "inverse",
            )
        if _s_col2:
            _rb3.metric("Avg Sharpe", f"{_bt_res[_s_col2].dropna().mean():.2f}")
        if _r_col2 and _bh_col:
            _beat = (
                _bt_res[_r_col2].dropna() > _bt_res[_bh_col].dropna()
            ).sum()
            _rb4.metric("Beat Buy&Hold", f"{_beat}/{len(_bt_res)}")

        _bt_sorted  = _bt_res.sort_values(_r_col2, ascending=False) if _r_col2 else _bt_res
        _styler2 = _bt_table_styler(_bt_sorted, _r_col2)
        st.dataframe(
            _styler2,
            use_container_width=True, height=380,
        )
        st.caption(
            "Sorted by return. ▲ = gain, ▼ = loss; row tint deepens with size. "
            "'Beat Buy&Hold' = how often the strategy outperformed simply holding."
        )
        # DT1 + DT2 · attribution on the strategy backtest table. Historical
        # OHLCV comes from data/fetcher.py's tiered pipeline (Yahoo primary
        # for daily history). Backtests run per invocation — no cache hint.
        try:
            from dashboard.shared.ui_components import data_as_of as _bt_asof
            import datetime as _bt_dt
            _bt_when = _bt_dt.datetime.now().strftime("%H:%M IST")
            st.markdown(
                _bt_asof(_bt_when, source="yfinance",
                         ttl_hint="daily-bars history · no cache"),
                unsafe_allow_html=True,
            )
        except Exception:
            pass

    # FIX B6 — failure breakdown. Previously any ticker that failed (data
    # fetch failure, or fetched but too little history) was silently
    # dropped from the table above with zero indication of how many or
    # why — a run that only found 5 valid results out of 500 requested
    # looked identical, from the table alone, to a run that had genuinely
    # only found 5 valid setups. This surfaces the count and the reason
    # breakdown recorded by the worker (see FIX B6 in _run_backtest_worker
    # above) so it's clear when the gap is a data-availability problem
    # rather than a real result.
    _fail_n     = st.session_state.get("bt_fail_count", 0)
    _fail_total = st.session_state.get("bt_fail_total", 0)
    if _fail_n:
        with st.expander(
            f"⚠️ {_fail_n}/{_fail_total} tickers couldn't be backtested — why?",
            expanded=_bt_res.empty,
        ):
            _reasons = st.session_state.get("bt_fail_reasons", {})
            for _reason, _count in sorted(_reasons.items(), key=lambda kv: -kv[1]):
                st.markdown(f"- **{_count}** — {_reason}")
            st.caption(
                "\"fetch/data error\" means every configured data source "
                "(Angel One, Stooq, Yahoo) failed for that ticker in this "
                "run — often a temporary provider block rather than the "
                "stock itself being unavailable. Re-running later, or with "
                "a smaller universe, sometimes recovers more of these."
            )

# ─────────────────────────────────────────────────────────────────────────────
# FIX B3 — Normalised comparison with common-date alignment
# ─────────────────────────────────────────────────────────────────────────────
st.markdown("---")
st.markdown(
    '<div class="t-h2" style="margin:14px 0 6px 0">'
    '🔍 Quick Chart Comparison</div>',
    unsafe_allow_html=True,
)
raw2 = st.text_input(
    "Compare tickers (space-separated)",
    value="RELIANCE.NS TCS.NS HDFCBANK.NS",
    key="backtest_tickers",
)
_comp_ui = st.radio(
    "Period", ["1M", "6M", "YTD", "1Y", "Max"],
    index=1, horizontal=True, key="comp_period",
)
comp_period = {"1M": "1m", "6M": "6m", "YTD": "ytd", "1Y": "1y", "Max": "max"}[_comp_ui]

if st.button("📊 Show Normalised Performance", key="compare_btn"):
    tickers_list = [t.strip().upper() for t in raw2.split() if t.strip()]
    tickers_list = [
        t if t.endswith(".NS") else t + ".NS"
        for t in tickers_list
    ]

    # FIX B3: collect all close series, then align to common date range
    _raw_series: dict[str, pd.Series] = {}
    with st.spinner("Loading price data…"):
        for t in tickers_list:
            try:
                d = load_ticker_df(t, period=comp_period)
                if not d.empty and "Close" in d.columns:
                    _raw_series[t] = d["Close"]
            except Exception as _e:
                st.caption(
                    f"⚠️ Couldn't load {t.replace('.NS','')} for comparison: {_e}"
                )

    if _raw_series:
        # Find the common start date (latest first date across all tickers)
        _common_start = max(s.index[0] for s in _raw_series.values())
        _common_end   = min(s.index[-1] for s in _raw_series.values())

        if _common_start >= _common_end:
            st.warning(
                "Tickers don't share a common date range for the selected period. "
                "Try a shorter period or different tickers."
            )
        else:
            fig_comp = go.Figure()
            for t, series in _raw_series.items():
                # FIX B3: slice to common range before normalising
                _aligned = series.loc[_common_start:_common_end].dropna()
                if _aligned.empty:
                    continue
                norm = _aligned / _aligned.iloc[0] * 100
                fig_comp.add_trace(go.Scatter(
                    x=norm.index, y=norm,
                    name=t.replace(".NS", ""),
                    line=dict(width=2),
                ))

            if fig_comp.data:
                fig_comp.add_hline(y=100, line_dash="dot", line_color="gray")
                fig_comp.add_annotation(
                    text=f"Common start: {_common_start.strftime('%d %b %Y')}",
                    xref="paper", yref="paper",
                    x=0, y=1.08, showarrow=False,
                    font=dict(size=10, color=_TK["dim"]),
                )
                fig_comp.update_layout(
                    title="Normalised Price Performance (Base = 100 at common start date)",
                    template="nse_pro", height=400,
                    yaxis_title="% of Start Price",
                    margin=dict(l=0, r=0, t=50, b=0),
                )
                st.plotly_chart(fig_comp, use_container_width=True)
                st.caption(
                    f"All lines indexed to 100 at **{_common_start.strftime('%d %b %Y')}** "
                    f"(the latest common start date across all tickers) so the comparison "
                    "is apples-to-apples."
                )
    else:
        st.warning("No price data could be loaded for the selected tickers.")
