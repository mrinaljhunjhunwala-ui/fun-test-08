"""dashboard/shared/trade_utils.py - paper-trade DB helpers + position sizing.

FIXES applied in this revision
───────────────────────────────
TU1  NSE holiday calendar — _is_market_open() and _is_squareoff_time() now
     check a cached NSE holiday list (fetched once daily from NSE's own
     bhavcopy calendar endpoint, with a hard-coded fallback list for the
     current year) before evaluating time/weekday logic. On holidays both
     functions return False so auto-close never runs against stale EOD prices.

TU2  MIS square-off fallback price — when live price is unavailable at
     square-off time the trade is NOT closed at entry price. Instead it is
     flagged with exit_reason="Auto square-off: price unavailable at 15:15"
     and left OPEN so the user can close it manually. The closed list entry
     marks exit as None so the P&L display shows "—" rather than a phantom ₹0.

TU3  paper_delete_account() — now raises a ValueError (caught by callers) if
     the account still has open positions, preventing silent history wipe.
     Callers that want to force-delete must first close all positions.

TU4  _portfolio_live_prices batch cache — individual ticker failures no longer
     poison the whole batch. Each ticker is fetched independently inside a
     try/except; failures are logged and excluded from the result dict without
     invalidating the cached prices for healthy tickers.

TU5  Signal monitor state (_pf_prev_actions) persisted to the kv store via
     trade_store.kv_set / kv_get so the "N changes since your last check"
     diff survives browser refreshes and new tabs.

TU6  _live_quote_price() — tenacity retry with exponential back-off (3
     attempts, 1 s / 2 s delays) so a transient Yahoo 429 does not silently
     return None and cascade into the entry-price square-off fallback (TU2).

FIX3 clear_price_caches() — added as a named public export so
     03_my_portfolio.py can bust only the live-price TTL caches (60 s) on
     the user's "Refresh Prices" click without clearing risk or fundamental
     caches.  Calls _portfolio_live_prices.clear() and
     _fetch_single_live_price.clear() — both are @st.cache_data functions
     so .clear() is always available.

FIX MH1  Manual portfolio holdings persistence (load_manual_holdings /
     save_manual_holdings) — replaces the old portfolio.csv / file-upload /
     Angel-One-import flow. Holdings are entered by hand on the My Portfolio
     page and persisted to the kv store, same pattern as TU5.
"""

from __future__ import annotations

import datetime
import logging
import os
import pathlib
import sys
import warnings
import io
import json
import math
import tempfile
import time
from typing import Optional

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
import plotly.io as pio
from plotly.subplots import make_subplots
import streamlit as st

_log = logging.getLogger("dashboard.trade_utils")
# FIX WARN1 — narrowed from a blanket `filterwarnings("ignore")` so numpy's
# RuntimeWarnings (invalid value / divide by zero / all-NaN slice) stay visible.
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", category=PendingDeprecationWarning)

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import trade_store as _store


# ─────────────────────────────────────────────────────────────────────────────
# FIX TU1 — NSE holiday calendar
# ─────────────────────────────────────────────────────────────────────────────

# FIX TU-HOL — this was a SECOND, hand-maintained NSE holiday list, separate
# from the canonical one in dashboard/shared/market_hours.NSE_HOLIDAYS, and it
# had drifted badly. It was missing EIGHT of 2026's sixteen closures:
#   26 Mar (Ram Navami), 31 Mar (Mahavir Jayanti), 28 May (Bakri Id),
#   26 Jun (Muharram), 14 Sep (Ganesh Chaturthi), 20 Oct (Dussehra),
#   10 Nov (Diwali Balipratipada), 24 Nov (Guru Nanak Jayanti)
# — four of which are still ahead of us this year. It also carried 2025 dates
# that disagree with the canonical list (e.g. Dussehra on 22 Oct vs 2 Oct).
#
# That is not cosmetic: this set is what _is_market_open() and
# _is_squareoff_time() consult, and FIX TU1 added it specifically so auto
# square-off would never run against stale EOD prices on a closed exchange.
# On any holiday missing from this copy, that protection simply wasn't there —
# the live NSE fetch below is the only other guard, and it routinely fails from
# datacenter IPs (nseindia.com 403s unauthenticated requests), which is exactly
# why a fallback exists.
#
# Deriving from the canonical list means one place to update each year, and no
# way for the two to silently disagree again. utils/market_hours.py already
# imports the same constant for the same reason.
def _canonical_holiday_dates() -> set:
    from dashboard.shared.market_hours import NSE_HOLIDAYS
    out = set()
    for d in NSE_HOLIDAYS:
        try:
            out.add(datetime.date.fromisoformat(d) if isinstance(d, str) else d)
        except (TypeError, ValueError) as _e:
            _log.debug("trade_utils: unparseable canonical holiday %r: %s", d, _e)
    return out


_NSE_HOLIDAYS_FALLBACK = _canonical_holiday_dates()

# Cache the fetched holiday set for the current calendar day
_holiday_cache: dict = {}   # {"date": datetime.date, "holidays": set}


def _nse_holidays() -> set:
    """Return the set of NSE trading holidays for the current year.

    Tries to load from the kv store (refreshed once per calendar day).
    Falls back to the hard-coded set if the network call fails.
    """
    today = datetime.date.today()
    # In-process cache — avoids repeat fetches within the same Python session
    if _holiday_cache.get("date") == today:
        return _holiday_cache["holidays"]

    # Try kv store first (populated below once per day)
    try:
        _kv = _store.kv_get("nse_holidays", None)
        if _kv and isinstance(_kv, dict) and _kv.get("date") == str(today):
            _h = {datetime.date.fromisoformat(d) for d in _kv.get("holidays", [])}
            _holiday_cache.update({"date": today, "holidays": _h})
            return _h
    except Exception as _e:
        _log.debug("trade_utils._nse_holidays kv read: %s", _e)

    # Try to fetch from NSE (public bhavcopy / holiday API, no auth needed)
    _fetched: set = set()
    try:
        import urllib.request, json as _json
        _year = today.year
        _url  = "https://www.nseindia.com/api/holiday-master?type=trading"
        _req  = urllib.request.Request(
            _url,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept":     "application/json",
                "Referer":    "https://www.nseindia.com/",
            },
        )
        with urllib.request.urlopen(_req, timeout=5) as _resp:
            _data = _json.loads(_resp.read())
        # NSE returns {"CM": [...], "FO": [...], ...}; CM = capital markets
        for _entry in _data.get("CM", []):
            try:
                _d = datetime.datetime.strptime(
                    _entry.get("tradingDate", ""), "%d-%b-%Y"
                ).date()
                _fetched.add(_d)
            except Exception as _parse_e:
                _log.debug("trade_utils._nse_holidays: could not parse date entry %s: %s", _entry, _parse_e)
        _log.info("trade_utils._nse_holidays: fetched %d holidays for %d", len(_fetched), _year)
    except Exception as _e:
        _log.debug("trade_utils._nse_holidays live fetch failed: %s — using fallback", _e)

    # Merge fallback so we never end up with an empty set
    holidays = (_fetched | _NSE_HOLIDAYS_FALLBACK) if _fetched else _NSE_HOLIDAYS_FALLBACK

    # Persist to kv store so other processes / page reruns skip the fetch today
    try:
        _store.kv_set(
            "nse_holidays",
            {"date": str(today), "holidays": [str(d) for d in holidays]},
        )
    except Exception as _e:
        _log.debug("trade_utils._nse_holidays kv write: %s", _e)

    _holiday_cache.update({"date": today, "holidays": holidays})
    return holidays


def _ist_now() -> datetime.datetime:
    """Current time in IST (UTC+5:30)."""
    return datetime.datetime.now(
        datetime.timezone(datetime.timedelta(hours=5, minutes=30))
    )


# ─────────────────────────────────────────────────────────────────────────────
# DB / account helpers (unchanged public API)
# ─────────────────────────────────────────────────────────────────────────────

def load_trades_by_account(account: str, path: str = "trades.db") -> pd.DataFrame:
    return _store.load_by_account(account)


def _ensure_paper_db(path: str = "trades.db"):
    _store.ensure_schema()


def paper_list_accounts(path: str = "trades.db") -> list:
    """Return sorted distinct account names, including empty registered accounts."""
    names = set()
    try:
        names.update(_store.list_accounts())
    except Exception as _e:
        _log.warning("trade_utils.paper_list_accounts (trades) degraded: %s", _e)
    try:
        names.update(_store.kv_get("paper_accounts", []) or [])
    except Exception as _e:
        _log.debug("trade_utils.paper_list_accounts (registry) degraded: %s", _e)
    names.add("My Account")
    return sorted(names)


def _register_paper_account(name: str) -> None:
    try:
        reg = set(_store.kv_get("paper_accounts", []) or [])
        if name not in reg:
            reg.add(name)
            _store.kv_set("paper_accounts", sorted(reg))
    except Exception as _e:
        _log.debug("trade_utils._register_paper_account degraded: %s", _e)


def paper_rename_account(old_name: str, new_name: str, path: str = "trades.db"):
    _store.rename_account(old_name, new_name)
    try:
        reg = set(_store.kv_get("paper_accounts", []) or [])
        reg.discard(old_name)
        reg.add(new_name)
        _store.kv_set("paper_accounts", sorted(reg))
        _store.kv_set(f"acct_type:{new_name}", paper_account_type(old_name))
    except Exception as _e:
        _log.debug("trade_utils.paper_rename_account registry degraded: %s", _e)


def paper_delete_account(name: str, path: str = "trades.db"):
    """Delete all trades in an account and drop it from the kv registry.

    FIX TU3: raises ValueError if the account still has OPEN positions so
    callers are forced to surface a warning rather than silently wiping P&L.
    """
    try:
        _open = _store.fetch_open(name)
        if not _open.empty:
            raise ValueError(
                f"Account '{name}' has {len(_open)} open position(s). "
                "Close all positions before deleting the account, or use "
                "paper_delete_account_force() to override."
            )
    except ValueError:
        raise
    except Exception as _fe:
        _log.warning("trade_utils.paper_delete_account: could not check open positions for '%s': %s — proceeding with deletion", name, _fe)
    _store.delete_account(name)
    try:
        reg = set(_store.kv_get("paper_accounts", []) or [])
        reg.discard(name)
        _store.kv_set("paper_accounts", sorted(reg))
    except Exception as _e:
        _log.debug("trade_utils.paper_delete_account registry degraded: %s", _e)


def paper_delete_account_force(name: str, path: str = "trades.db"):
    """Force-delete an account even if it has open positions (no safeguard)."""
    _store.delete_account(name)
    try:
        reg = set(_store.kv_get("paper_accounts", []) or [])
        reg.discard(name)
        _store.kv_set("paper_accounts", sorted(reg))
    except Exception as _e:
        _log.debug("trade_utils.paper_delete_account_force registry degraded: %s", _e)


def paper_open_trade(
    ticker: str, price: float, qty: int,
    sl: float, tp: float, reason: str = "",
    account: str = "My Account",
    path: str = "trades.db",
) -> int:
    return _store.open_trade(ticker, price, qty, sl, tp, reason=reason, account=account)


def paper_close_trade(
    trade_id: int, exit_price: float,
    reason: str = "Manual close",
    path: str = "trades.db",
):
    _store.close_trade(trade_id, exit_price, reason=reason)


def paper_edit_trade(
    trade_id: int, sl: float = None, tp: float = None,
    reason: str = None, path: str = "trades.db",
):
    _store.edit_trade(trade_id, sl=sl, tp=tp, reason=reason)


# ── Account product type ───────────────────────────────────────────────────

def paper_account_type(name: str) -> str:
    try:
        return _store.kv_get(f"acct_type:{name}", "CNC") or "CNC"
    except Exception as _e:
        _log.debug("trade_utils.paper_account_type degraded: %s", _e)
        return "CNC"


def set_paper_account_type(name: str, atype: str) -> None:
    try:
        _store.kv_set(
            f"acct_type:{name}",
            "MIS"
            if str(atype).upper().startswith("MIS") or "INTRA" in str(atype).upper()
            else "CNC",
        )
        _register_paper_account(name)
    except Exception as _e:
        _log.debug("trade_utils.set_paper_account_type degraded: %s", _e)


# ─────────────────────────────────────────────────────────────────────────────
# FIX TU4 — per-ticker live price fetch with isolated failure handling
# ─────────────────────────────────────────────────────────────────────────────

@st.cache_data(ttl=60, show_spinner=False)
def _fetch_single_live_price(ticker: str, max_wait_seconds: float = 10) -> dict:
    """Fetch live price for one ticker. Failures return {} without poisoning others.

    FIX TU-WAIT1: previously called get_live_prices_batch([ticker]) with no
    max_wait_seconds. With Angel One not configured (or its circuit breaker
    tripped, or the ticker not in its instrument master), the per-symbol
    Yahoo→NSE→Stooq fallback chain's own scaled wait (see live_price.py's
    FIX LP1) applies in full: for a single remaining symbol that's
    min(120, 1*26+4) = 30 SECONDS, on every cache-miss call. Every other
    caller of get_live_prices_batch() in this app already bounds this —
    nav.py's sidebar passes max_wait_seconds=10 (FIX NAV2) — except this
    one, which is called per-ticker on Deep Dive's Live Snapshot (a single
    interactive lookup) AND sequentially per holding in
    _portfolio_live_prices() below — so a 5-holding portfolio with Angel One
    unavailable could take up to 5×30s = 150s to load. This is almost
    certainly the actual mechanism behind "Deep Dive / My Portfolio feels
    very stale" reports, not (only) NSE's WAF. Default of 10s matches
    nav.py's existing precedent.

    FIX VOL1: also carries through "volume" (qty traded) when the resolving
    tier provided one — real-time from Angel One, best-effort daily volume
    from Yahoo/NSE/Stooq otherwise. Absent (not 0) when no tier had it, so
    callers should always use .get("volume") rather than assume it's set.
    """
    try:
        from utils.live_price import get_live_prices_batch
        raw = get_live_prices_batch([ticker], max_wait_seconds=max_wait_seconds)
        q   = raw.get(ticker)
        if isinstance(q, dict) and q.get("price"):
            out = {
                "price": q["price"],
                "prev":  q["prev_close"],
                "chg":   q["chg_pct"],
            }
            if q.get("volume"):
                out["volume"] = q["volume"]
            return out
    except Exception as _e:
        _log.debug("trade_utils._fetch_single_live_price(%s) degraded: %s", ticker, _e)
    return {}


@st.cache_data(ttl=60, show_spinner=False)
def _portfolio_live_prices(tickers: tuple) -> dict:
    """Live prices for portfolio holdings.

    FIX TU4: each ticker is fetched independently so a single bad/delisted
    ticker does not poison the cached result for the whole portfolio.
    Failures are logged and excluded; healthy tickers are always returned.

    FIX TU-WAIT1: max_wait_seconds=6 per ticker (tighter than
    _fetch_single_live_price's own 10s default) because this loop is
    sequential across ALL holdings — at the 10s default, a 10-holding
    portfolio with Angel One unavailable could still take up to 100s to
    load; at 6s that's a 60s worst case, which is still not fast but no
    longer minutes-long. See _fetch_single_live_price's docstring for the
    full picture of what this is bounding.
    """
    results = {}
    for t in tickers:
        try:
            _r = _fetch_single_live_price(t, max_wait_seconds=6)
            if _r:
                results[t] = _r
        except Exception as _e:
            _log.debug("trade_utils._portfolio_live_prices(%s) failed: %s", t, _e)
    return results


# ─────────────────────────────────────────────────────────────────────────────
# FIX4 — Secure temp file helper
# ─────────────────────────────────────────────────────────────────────────────

def _safe_tmpfile(suffix: str = ".csv") -> pathlib.Path:
    """Return a Path to a freshly-created secure temporary file.

    FIX4: replaces the insecure tempfile.mktemp() pattern (which creates a
    race condition between name generation and file creation) with
    NamedTemporaryFile(delete=False), which atomically creates the file.
    The caller owns the file and is responsible for deletion when done.

    Usage:
        tmp = _safe_tmpfile(suffix=".csv")
        tmp.write_text(content, encoding="utf-8")

    NOTE: kept for backwards compatibility / other potential callers. The
    My Portfolio page no longer uses this for holdings (see FIX MH1 below) —
    holdings are now entered manually and persisted via the kv store, not
    through CSV uploads or Angel-One tmp-file imports.
    """
    with tempfile.NamedTemporaryFile(
        suffix=suffix, delete=False, mode="w", encoding="utf-8"
    ) as _f:
        _path = _f.name   # file is created and immediately closed (empty)
    return pathlib.Path(_path)


# ─────────────────────────────────────────────────────────────────────────────
# FIX9 — Portfolio CSV upload validation
# ─────────────────────────────────────────────────────────────────────────────

_REQUIRED_CSV_COLS = {"ticker", "quantity", "avg_buy_price", "date_bought"}


def upload_validate_portfolio_csv(
    uploaded_file,
) -> "tuple[pathlib.Path | None, str | None]":
    """Validate an st.file_uploader result as a portfolio CSV.

    FIX9: validates required columns at upload time so the rest of the page
    never receives a malformed DataFrame.

    NOTE: no longer called from My Portfolio (that page now uses manual
    holdings entry — see load_manual_holdings/save_manual_holdings below).
    Left in place in case any other page or export/import workflow still
    needs CSV validation.

    Returns
    -------
    (path, None)   — file is valid; path is a secure tmp Path ready to read.
    (None, errmsg) — file is invalid; errmsg is a human-readable explanation.
    """
    import io as _io

    if uploaded_file is None:
        return None, "No file provided."

    try:
        _raw = uploaded_file.read()
        uploaded_file.seek(0)          # reset so callers can re-read if needed
        _df  = pd.read_csv(_io.BytesIO(_raw))
    except Exception as _e:
        return None, f"Could not parse CSV: {_e}"

    _cols_lower = {c.strip().lower() for c in _df.columns}
    _missing    = _REQUIRED_CSV_COLS - _cols_lower
    if _missing:
        return None, (
            f"Missing required column(s): {', '.join(sorted(_missing))}. "
            f"Expected: {', '.join(sorted(_REQUIRED_CSV_COLS))}."
        )

    if _df.empty:
        return None, "The uploaded CSV has no data rows."

    # Write to a secure temp file so the rest of the page can use a Path
    try:
        _tmp = _safe_tmpfile(suffix=".csv")
        _tmp.write_bytes(_raw)
        return _tmp, None
    except Exception as _e:
        return None, f"Could not write temp file: {_e}"


# ─────────────────────────────────────────────────────────────────────────────
# FIX3 — Granular live-price cache buster (public export)
# ─────────────────────────────────────────────────────────────────────────────

def clear_price_caches() -> None:
    """Invalidate only the live-price TTL caches (60 s).

    Called from 03_my_portfolio.py when the user clicks 'Refresh Prices'.
    Deliberately leaves risk and fundamental caches untouched so an on-demand
    price refresh does not trigger expensive re-fetches of unrelated data.

    Both _portfolio_live_prices and _fetch_single_live_price are decorated
    with @st.cache_data, so .clear() is always available.
    """
    try:
        _portfolio_live_prices.clear()
    except Exception as _e:
        _log.debug("clear_price_caches: _portfolio_live_prices.clear() failed: %s", _e)
    try:
        _fetch_single_live_price.clear()
    except Exception as _e:
        _log.debug("clear_price_caches: _fetch_single_live_price.clear() failed: %s", _e)


# ─────────────────────────────────────────────────────────────────────────────
# FIX TU6 — live quote with retry / back-off
# ─────────────────────────────────────────────────────────────────────────────

def _live_quote_price(ticker: str) -> Optional[float]:
    """Best-effort live LTP for a ticker (Angel One → Yahoo).

    FIX TU6: retries up to 3 times with 1 s / 2 s exponential back-off so a
    transient Yahoo 429 does not immediately return None and cascade into the
    entry-price square-off fallback.
    """
    _delays = [0, 1, 2]
    for _attempt, _delay in enumerate(_delays):
        if _delay:
            time.sleep(_delay)
        try:
            from utils.live_price import get_live_quote
            q = get_live_quote(ticker)
            if isinstance(q, dict) and q.get("price"):
                return float(q["price"])
        except Exception as _e:
            _log.debug(
                "trade_utils._live_quote_price(%s) attempt %d failed: %s",
                ticker, _attempt + 1, _e,
            )
    return None


# ─────────────────────────────────────────────────────────────────────────────
# UI helpers
# ─────────────────────────────────────────────────────────────────────────────

def _action_color(action: str) -> str:
    if action == "STRONG BUY":
        return "card-green pulse-green"   # extra glow to distinguish from plain BUY
    elif action == "BUY":
        return "card-green"
    elif action in ("WATCHLIST", "HOLD"):
        return "card-yellow"
    else:
        return "card-red"


def _action_emoji(action: str) -> str:
    return {
        "STRONG BUY": "🚀", "BUY": "🟢", "WATCHLIST": "👀",
        "HOLD": "🟡",       "CAUTION": "⚠️", "EXIT": "🔴",
    }.get(action, "")


# Phase 2 UI honesty — map internal action strings to honest display labels.
# Internal strings (engine, DB, CSV, sort keys) are NEVER changed.
# Only this function touches what the user actually reads.
# Single source of truth lives in the pure analysis/action_labels.py so the
# Telegram/Gmail alerts share it without importing Streamlit.
from analysis.action_labels import ACTION_DISPLAY_LABELS as _ACTION_DISPLAY_LABELS  # noqa: E402


def _display_label(action: str) -> str:
    """Return the honest UI display label for an internal action string.

    Phase 2 UI honesty audit: replaces imperative labels like 'STRONG BUY'
    with descriptive trend-quality language consistent with the efficacy study
    finding that these are trend-quality scores, not return forecasts.

    The internal action string is preserved everywhere else (DB, CSV export,
    sort keys, signal logic). Only the display layer uses this function.
    """
    return _ACTION_DISPLAY_LABELS.get(action, action)


# FinalVerdict (analysis/final_verdict.VERDICTS) → descriptive display label.
# Internal verdict strings are unchanged; this is display-only (CLAUDE.md rule 1).
_VERDICT_DISPLAY_LABELS = {
    "STRONG BUY": "Strong trend ▲▲",
    "BUY":        "Uptrend ▲",
    "WATCH":      "Mixed signals",
    "HOLD":       "Neutral",
    "AVOID":      "Weak / broken trend ▼",
}


def verdict_display_label(verdict: str) -> str:
    """Descriptive UI label for a FinalVerdict value (never an instruction)."""
    return _VERDICT_DISPLAY_LABELS.get(verdict, verdict)


def _grade_color(grade: str) -> str:
    return {
        "A+": "#6faa84", "A": "#4CAF50", "B": "#8BC34A",
        "C":  "#FFC107", "D": "#FF5722", "F": "#f44336",
    }.get(grade, "#9E9E9E")


# ─────────────────────────────────────────────────────────────────────────────
# Position sizing
# ─────────────────────────────────────────────────────────────────────────────

def _reanchor_levels(entry: float, sl: float, tp: float, live_price: float) -> tuple:
    """Re-anchor entry/SL/TP to a live price while preserving the ATR-based
    stop distance and conviction-scaled target distance already computed at
    scan time — same technique analysis/score.py's score_stock() already
    uses for single-ticker live lookups.

    Used by Deep Dive Analysis's Live Snapshot section: a pick's entry/SL/TP
    are frozen at whatever the last daily close was when the Top Picks scan
    ran (Command Centre no longer live-fetches per-pick at all — see that
    page's FIX CC-LOAD1 comment) — this re-anchors those three numbers to a
    live price fetched on demand for the one ticker being looked at, cheap
    since it's a single fetch rather than a batch across ~40 tickers.
    Returns (entry, sl, tp) unchanged if live_price is falsy or entry is 0.
    """
    entry = float(entry or 0)
    sl    = float(sl or 0)
    tp    = float(tp or 0)
    if not live_price or entry <= 0:
        return entry, sl, tp
    risk_dist   = entry - sl
    reward_dist = tp - entry
    return (
        round(float(live_price), 2),
        round(float(live_price) - risk_dist, 2),
        round(float(live_price) + reward_dist, 2),
    )


def _suggest_position(
    entry: float, sl: float,
    capital: float = None,
    risk_pct: float = None,
    max_alloc_pct: float = 20.0,
) -> dict:
    """Fixed-fractional risk sizing capped at max_alloc_pct% of capital."""
    if capital is None:
        capital = float(st.session_state.get("trade_capital", 500_000.0))
    if risk_pct is None:
        risk_pct = float(st.session_state.get("risk_pct", 1.0))
    entry = float(entry or 0)
    sl    = float(sl    or 0)
    if entry <= 0:
        return {
            "qty": 1, "price": entry, "risk_per_share": 0,
            "capital_at_risk": 0, "position_value": entry, "basis": "fallback",
        }

    risk_amount = capital * (risk_pct / 100.0)
    rps = abs(entry - sl)
    if rps > 0.01:
        qty_risk = int(risk_amount / rps)
        basis    = f"{risk_pct:.0f}% risk (₹{risk_amount:,.0f}) ÷ ₹{rps:.2f}/share"
    else:
        qty_risk = int(risk_amount / entry)
        basis    = "notional (no valid stop)"

    qty_cap = int((capital * max_alloc_pct / 100.0) / entry)
    qty     = max(1, min(qty_risk, qty_cap))
    if qty == qty_cap < qty_risk:
        basis += f" · capped at {max_alloc_pct:.0f}% allocation"

    return {
        "qty":             qty,
        "price":           round(entry, 2),
        "risk_per_share":  round(rps, 2),
        "capital_at_risk": round(rps * qty, 0),
        "position_value":  round(entry * qty, 0),
        "basis":           basis,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Paper trade popover (account selector inside)
# ─────────────────────────────────────────────────────────────────────────────

@st.fragment
def _paper_trade_popover(
    ticker: str, entry: float, sl: float, tp: float,
    reason: str, key: str, label: str = "📌 Paper Trade",
) -> None:
    """Open-a-paper-trade popover that enters at the live market price by default.

    FIX (lag + missing confirmation): this used to be a plain function, so
    EVERY interaction inside it — typing a qty, switching account, even just
    opening the popover — triggered a full-page rerun. On this app that means
    re-running the whole sidebar (live prices, notification bell, portfolio
    quick view, VIX/macro pulse) on every keystroke, which is the main source
    of the page feeling slow/laggy. Wrapping this in @st.fragment means its
    own reruns (including the one st.rerun() fires on confirm) are scoped to
    just this small widget — the rest of the page, including the sidebar,
    is left untouched.

    One side effect: because confirming a trade no longer triggers a
    full-page rerun, things elsewhere on the page that depend on the new
    trade (e.g. the sidebar's open-position count, notification bell) won't
    reflect it until the next full-page interaction. That's an intentional
    trade-off for responsiveness; the trade itself is saved immediately
    either way.

    Also: st.toast() alone was easy to miss — it's a few seconds and the
    page used to be busy re-fetching the whole sidebar while it showed, so
    it was often gone by the time the page settled. Added a persistent
    inline success banner (st.success, shown right above the button — does
    not auto-expire) as a more reliable confirmation than the toast alone.
    """
    _tlbl = ticker.replace(".NS", "")
    _cap  = float(st.session_state.get("trade_capital", 500_000.0))
    _rkp  = float(st.session_state.get("risk_pct", 1.0))

    _analysis_entry = float(entry or 0)
    _sl_dist = (_analysis_entry - float(sl)) if (sl and _analysis_entry) else None
    _tp_dist = (float(tp) - _analysis_entry) if (tp and _analysis_entry) else None

    # Persistent confirmation banner — survives until the next trade from
    # this same button (unlike st.toast, doesn't auto-expire after a few sec).
    _msg_key = f"{key}_success_msg"
    _last_msg = st.session_state.get(_msg_key)
    if _last_msg:
        st.success(_last_msg, icon="✅")
        if st.button("Dismiss", key=f"{key}_dismiss", width="stretch"):
            st.session_state.pop(_msg_key, None)
            st.rerun()

    with st.popover(label, width="stretch"):
        st.markdown(f"**{_tlbl}** — open paper trade")

        # Account selector inside the popover
        _acct_list    = paper_list_accounts()
        _default_acct = st.session_state.get("pt_account", "My Account")
        _default_idx  = (
            _acct_list.index(_default_acct) if _default_acct in _acct_list else 0
        )
        _selected_acct = st.selectbox(
            "Account", options=_acct_list, index=_default_idx,
            key=f"{key}_acct",
            help="Choose which paper trading account to book this trade into.",
        )
        _acct_type = paper_account_type(_selected_acct)
        _acct_type_label = (
            "⏱ Intraday (MIS) — auto squared off at 15:15"
            if _acct_type == "MIS"
            else "📦 Delivery (CNC) — held until you close manually"
        )
        st.caption(f"Account type: **{_acct_type_label}** · Change in Paper Trades → Accounts.")

        # FIX TU6: use retried live quote
        _live          = _live_quote_price(ticker)
        _default_entry = _live if (_live and _live > 0) else _analysis_entry

        if _live and _analysis_entry and abs(_live - _analysis_entry) / _analysis_entry > 0.002:
            _drift = (_live / _analysis_entry - 1) * 100
            st.caption(
                f"🔴 **Live ₹{_live:,.2f}** vs analysis ₹{_analysis_entry:,.2f} "
                f"({_drift:+.2f}%). Entry defaults to live; SL/TP re-anchored."
            )
        elif _live:
            st.caption(f"🟢 Entering at **live ₹{_live:,.2f}** (matches analysis).")
        else:
            st.caption(
                "⚠️ Live price unavailable after retries — using the analysis entry. "
                "Verify before trusting the fill."
            )

        entry_use = st.number_input(
            "Entry price (₹) — defaults to LIVE, editable for a limit",
            min_value=0.01, value=round(float(_default_entry or 0.01), 2),
            step=0.05, format="%.2f", key=f"{key}_entry",
        )
        sl_use = round(entry_use - _sl_dist, 2) if _sl_dist is not None else (float(sl) if sl else 0.0)
        tp_use = round(entry_use + _tp_dist, 2) if _tp_dist is not None else (float(tp) if tp else 0.0)

        sugg = _suggest_position(entry_use, sl_use)
        st.caption(
            f"💡 Suggested **{sugg['qty']} shares** — sizes your loss-to-stop to "
            f"≈{_rkp:.2g}% of ₹{_cap:,.0f} (₹{sugg['capital_at_risk']:,.0f} at risk). "
            f"Change capital & risk in the sidebar; adjust qty below."
        )
        qty   = st.number_input(
            "Quantity (shares)", min_value=1, max_value=1_000_000,
            value=int(sugg["qty"]), step=1, key=f"{key}_qty",
        )
        _val  = qty * entry_use
        _risk = abs(entry_use - (sl_use or entry_use)) * qty
        _c1, _c2, _c3 = st.columns(3)
        _c1.metric("Entry",    f"₹{entry_use:,.2f}")
        _c2.metric("Position", f"₹{_val:,.0f}")
        _c3.metric("At Risk",  f"₹{_risk:,.0f}")
        if sl_use or tp_use:
            _rr = (
                (tp_use - entry_use) / (entry_use - sl_use)
                if (entry_use - sl_use) > 0.01 and tp_use
                else 0
            )
            st.caption(
                f"🛑 SL ₹{(sl_use or 0):,.2f}  ·  🎯 Target ₹{(tp_use or 0):,.2f}"
                + (f"  ·  R:R {_rr:.1f}x" if _rr else "")
            )
        if st.button(
            "✅ Confirm & Open", key=f"{key}_confirm",
            type="primary", width="stretch",
        ):
            _id = paper_open_trade(
                ticker, float(entry_use), int(qty),
                sl=sl_use, tp=tp_use, reason=reason,
                account=_selected_acct,
            )
            st.toast(
                f"📌 Opened #{_id}: {int(qty)} × {_tlbl} @ ₹{entry_use:,.2f} "
                f"in '{_selected_acct}'",
                icon="✅",
            )
            st.session_state[_msg_key] = (
                f"Opened #{_id}: {int(qty)} × {_tlbl} @ ₹{entry_use:,.2f} "
                f"in '{_selected_acct}'"
            )
            # FIX TU-confirm: paper_open_trade() writes straight to trade_store
            # (uncached) — nothing here needs st.cache_data invalidated. The old
            # blanket st.cache_data.clear() also wiped Top Picks (5-min scan),
            # Tomorrow's Watchlist (1-hr EOD scan), and sector ranks, which on
            # pages with their own cold-cache state machine (e.g. Tomorrow's
            # Watchlist) forced an immediate "first run, scanning…" banner on
            # the very next rerun — burying this toast before it was ever seen.
            # Only the live-price cache for this ticker is now stale (the new
            # position needs a fresh quote), so clear just that.
            _fetch_single_live_price.clear()
            st.rerun()


# ─────────────────────────────────────────────────────────────────────────────
# FIX TU1 — Market hours helpers with holiday awareness
# ─────────────────────────────────────────────────────────────────────────────

def _is_market_open() -> bool:
    """True if NSE is currently in a live session (9:15–15:30 IST, Mon–Fri, non-holiday).

    FIX TU1: checks the NSE holiday calendar before evaluating time/weekday so
    auto-close logic never runs against stale EOD prices on exchange holidays.
    """
    try:
        from utils.market_hours import market_status as _msx
        return bool(_msx().get("is_open", False))
    except Exception as _mh_e:
        _log.debug("trade_utils._is_market_open: market_hours import failed (%s) — falling back to manual IST check", _mh_e)
    # Fallback: manual IST check
    now = _ist_now()
    if now.weekday() >= 5:                         # Saturday / Sunday
        return False
    if now.date() in _nse_holidays():              # FIX TU1: holiday check
        return False
    mins = now.hour * 60 + now.minute
    return 555 <= mins <= 930                       # 9:15 – 15:30


def _is_squareoff_time() -> bool:
    """True from 15:15 IST onward on trading weekdays — MIS intraday square-off window.

    FIX TU1: also returns False on NSE holidays.
    """
    now = _ist_now()
    if now.weekday() >= 5:
        return False
    if now.date() in _nse_holidays():              # FIX TU1: holiday check
        return False
    mins = now.hour * 60 + now.minute
    return mins >= 915                             # 15:15 PM


# ─────────────────────────────────────────────────────────────────────────────
# FIX TU2 — Auto-close with safe square-off price fallback
# ─────────────────────────────────────────────────────────────────────────────

def _auto_close_breached(account: str = None, path: str = "trades.db") -> list:
    """Auto-close OPEN paper trades whose live price has crossed SL or TP.

    Rules
    ─────
    • CNC: SL/TP breach only, during live market hours.
    • MIS: SL/TP breach during market hours, PLUS force-close all remaining
      open MIS positions at 15:15 (square-off).

    FIX TU1: returns [] on NSE holidays (both _is_market_open and
    _is_squareoff_time already return False, but explicit guard here too).

    FIX TU2: when live price is unavailable at square-off time the trade is
    NOT closed at entry price. Instead it is flagged with exit_reason
    "Auto square-off: price unavailable at 15:15" and left OPEN so the user
    can close it manually. The returned list entry has exit=None to signal
    that no fill was recorded.
    """
    closed  = []
    _open   = _is_market_open()
    _sqoff  = _is_squareoff_time()

    if not _open and not _sqoff:
        return closed

    # Extra holiday guard (belt-and-suspenders)
    if _ist_now().date() in _nse_holidays():
        return closed

    try:
        rows = _store.fetch_open(account)
        if rows.empty:
            return closed

        syms = tuple(rows["ticker"].tolist())
        lp   = _portfolio_live_prices(syms)

        for _, r in rows.iterrows():
            tk        = str(r["ticker"])
            ep        = float(r.get("price",    0) or 0)
            qty       = int(  r.get("quantity", 0) or 0)
            sl        = float(r.get("sl",       0) or 0) or None
            tp        = float(r.get("tp",       0) or 0) or None
            trade_id  = int(r["id"])
            acct      = str(r.get("account", account or "My Account"))
            acct_type = paper_account_type(acct)
            cur       = lp.get(tk, {}).get("price")

            # ── MIS square-off ──────────────────────────────────────────────
            if acct_type == "MIS" and _sqoff:
                if cur and cur > 0:
                    # Normal case — live price available
                    paper_close_trade(
                        trade_id, cur,
                        "Auto square-off: MIS position closed at 15:15",
                    )
                    closed.append({
                        "ticker":  tk.replace(".NS", ""),
                        "type":    "squareoff",
                        "exit":    cur,
                        "pnl":     (cur - ep) * qty,
                        "account": acct,
                    })
                else:
                    # FIX TU2: price unavailable — flag trade, do NOT use entry price
                    _log.warning(
                        "trade_utils._auto_close_breached: live price unavailable for %s "
                        "at square-off — trade #%d left open for manual close.",
                        tk, trade_id,
                    )
                    paper_edit_trade(
                        trade_id,
                        reason=(
                            "⚠️ Auto square-off attempted at 15:15 but live price was "
                            "unavailable. Please close this position manually."
                        ),
                    )
                    closed.append({
                        "ticker":  tk.replace(".NS", ""),
                        "type":    "squareoff_failed",
                        "exit":    None,        # no fill — signals display "—"
                        "pnl":     None,
                        "account": acct,
                    })
                continue   # skip SL/TP check — already handled

            # ── SL / TP breach ──────────────────────────────────────────────
            if not _open or cur is None or ep <= 0:
                continue

            hit     = None
            exit_px = None
            why     = ""
            if tp and cur >= tp:
                hit, exit_px, why = "target", tp,  "Auto-closed: target reached"
            elif sl and cur <= sl:
                hit, exit_px, why = "stop",   sl,  "Auto-closed: stop-loss hit"

            if hit:
                paper_close_trade(trade_id, exit_px, why)
                closed.append({
                    "ticker":  tk.replace(".NS", ""),
                    "type":    hit,
                    "exit":    exit_px,
                    "pnl":     (exit_px - ep) * qty,
                    "account": acct,
                })

    except Exception as _e:
        _log.warning("trade_utils._auto_close_breached error: %s", _e)

    return closed


def _render_autoclose_banner(closed: list) -> None:
    """Show a prominent banner listing trades that were just auto-closed.

    FIX TU2: handles exit=None (squareoff_failed) gracefully — shows a
    warning row instead of trying to format a None price.
    """
    if not closed:
        return

    _TYPE_LABEL = {
        "target":           "target reached",
        "stop":             "stop-loss hit",
        "squareoff":        "MIS square-off @ 15:15",
        "squareoff_failed": "⚠️ MIS square-off FAILED — close manually",
    }
    _TYPE_ICON = {
        "target":           "🎯",
        "stop":             "🛑",
        "squareoff":        "⏰",
        "squareoff_failed": "⚠️",
    }

    _rows = ""
    for c in closed:
        _ic  = _TYPE_ICON.get(c["type"], "🔔")
        _lbl = _TYPE_LABEL.get(c["type"], c["type"])

        if c["exit"] is None:
            # FIX TU2: squareoff_failed — no P&L to show
            _rows += (
                f'<div style="display:flex;justify-content:space-between;'
                f'padding:5px 0;border-bottom:1px solid rgba(255,255,255,.06);font-size:13px">'
                f'<span style="color:#ffa726">{_ic} <b>{c["ticker"]}</b> '
                f'<span style="color:#888">({c["account"]})</span> — {_lbl}</span>'
                f'<span style="color:#ffa726;font-weight:700">close manually</span></div>'
            )
        else:
            _col = "#6faa84" if (c["pnl"] or 0) >= 0 else "#c77070"
            _rows += (
                f'<div style="display:flex;justify-content:space-between;'
                f'padding:5px 0;border-bottom:1px solid rgba(255,255,255,.06);font-size:13px">'
                f'<span style="color:#eee">{_ic} <b>{c["ticker"]}</b> '
                f'<span style="color:#888">({c["account"]})</span> — '
                f'{_lbl} @ ₹{c["exit"]:,.2f}</span>'
                f'<span style="color:{_col};font-weight:700">₹{(c["pnl"] or 0):+,.0f}</span></div>'
            )

    st.markdown(
        f'<div style="background:linear-gradient(135deg,#1a1200,#2d1f00);'
        f'border:1px solid #FFC107;border-radius:12px;padding:14px 18px;margin-bottom:14px">'
        f'<div style="font-size:14px;font-weight:700;color:#FFC107;margin-bottom:6px">'
        f'🔔 {len(closed)} position{"s" if len(closed)!=1 else ""} auto-closed</div>'
        f'{_rows}</div>',
        unsafe_allow_html=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# FIX TU5 — Signal monitor state persistence
# ─────────────────────────────────────────────────────────────────────────────

def load_signal_monitor_state() -> dict:
    """Load the previous signal monitor action map from the kv store.

    FIX TU5: persists across browser refreshes / new tabs so the diff
    "N changes since your last check" is meaningful even after a page reload.
    Falls back to session_state for backwards compatibility.
    """
    try:
        _kv = _store.kv_get("pf_prev_actions", None)
        if _kv and isinstance(_kv, dict):
            return _kv
    except Exception as _e:
        _log.debug("trade_utils.load_signal_monitor_state kv read: %s", _e)
    # Fallback to whatever is already in session_state
    return st.session_state.get("_pf_prev_actions", {})


def save_signal_monitor_state(actions: dict) -> None:
    """Persist the current signal monitor action map to the kv store.

    FIX TU5: called after each portfolio re-score so the state survives
    page refreshes.
    """
    st.session_state["_pf_prev_actions"] = actions
    try:
        _store.kv_set("pf_prev_actions", actions)
    except Exception as _e:
        _log.debug("trade_utils.save_signal_monitor_state kv write: %s", _e)


# ─────────────────────────────────────────────────────────────────────────────
# FIX MH1 — Manual portfolio holdings persistence (replaces CSV upload)
# ─────────────────────────────────────────────────────────────────────────────

_MANUAL_HOLDINGS_KV_KEY = "manual_portfolio_holdings"


def load_manual_holdings() -> list:
    """Load the user's manually-entered portfolio holdings from the kv store.

    FIX MH1: replaces the old portfolio.csv / file-upload / Angel-One-import
    flow. Holdings are entered by hand on the My Portfolio page (ticker, qty,
    avg buy price, date bought — no price/qty auto-suggestion) and persisted
    here so they survive refreshes and new tabs, same pattern as TU5's
    signal-monitor state.

    Returns a list of dicts: [{"ticker": "...", "quantity": float,
    "avg_buy_price": float, "date_bought": "YYYY-MM-DD"}, ...]
    """
    try:
        _kv = _store.kv_get(_MANUAL_HOLDINGS_KV_KEY, None)
        if isinstance(_kv, list):
            return _kv
    except Exception as _e:
        _log.debug("trade_utils.load_manual_holdings kv read: %s", _e)
    return st.session_state.get("_manual_holdings", [])


def save_manual_holdings(holdings: list) -> None:
    """Persist the user's manually-entered portfolio holdings to the kv store.

    FIX MH1: called after every add / edit / delete on the My Portfolio page.
    """
    st.session_state["_manual_holdings"] = holdings
    try:
        _store.kv_set(_MANUAL_HOLDINGS_KV_KEY, holdings)
    except Exception as _e:
        _log.debug("trade_utils.save_manual_holdings kv write: %s", _e)
