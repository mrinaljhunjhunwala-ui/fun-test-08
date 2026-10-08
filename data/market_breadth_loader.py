"""Latest NSE market-breadth snapshot from the shared bhavcopy cache.

Reads the per-session CSVs in ``data/cache/bhavcopy`` (written by both
``data.nse_bhavcopy_prices`` and ``research/regime_breadth_study.py``) and runs
the pure ``analysis.market_breadth`` helpers over them. ``max_downloads`` lets a
caller fill a few missing recent sessions per call; 0 means disk only (the
verdict-ledger shadow label uses that, so logging never waits on the network).

Every failure returns ``None`` — breadth is context, never a hard dependency.
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
from typing import Callable, Optional

import pandas as pd

_log = logging.getLogger("data.market_breadth_loader")

DEFAULT_SESSIONS = 260          # 200DMA + 52-week window need ~250 sessions
MIN_SESSIONS = 2                # A/D needs one prior close


def _fill_prev_close(df: pd.DataFrame) -> pd.DataFrame:
    """Files cached by the production reader lack PREV_CLOSE; use the prior cached
    session's close, but only when that session is the immediately previous NSE
    trading day. Across a gap in the cache the 'return' would span several days,
    so PREV_CLOSE stays null there (the stock has no 1-day return that session)."""
    from utils.nse_holidays import is_trading_day

    df = df.sort_values(["SYMBOL", "DATE"])
    prior = df.groupby("SYMBOL")["Close"].shift(1)
    prior_date = df.groupby("SYMBOL")["DATE"].shift(1).astype(str).str[:10]
    expected = {}
    for ds in df["DATE"].dropna().unique():
        d = _dt.date.fromisoformat(str(ds)[:10]) - _dt.timedelta(days=1)
        for _ in range(30):
            if is_trading_day(d):
                break
            d -= _dt.timedelta(days=1)
        expected[ds] = d.isoformat()
    prior = prior.where(prior_date == df["DATE"].map(expected))
    if "PREV_CLOSE" in df.columns:
        df["PREV_CLOSE"] = pd.to_numeric(df["PREV_CLOSE"], errors="coerce").fillna(prior)
    else:
        df["PREV_CLOSE"] = prior
    return df


def load_sessions(n: int = DEFAULT_SESSIONS, *, cache_dir: Optional[str] = None,
                  max_downloads: int = 0,
                  download: Optional[Callable[[_dt.date], str]] = None,
                  today: Optional[_dt.date] = None) -> pd.DataFrame:
    """Long frame (DATE, SYMBOL, SERIES, Close, Volume, PREV_CLOSE) for up to ``n`` recent sessions."""
    from data import nse_bhavcopy_prices as bp

    cache_dir = cache_dir or bp._CACHE_DIR
    end = today or bp.last_published_session()
    parts, budget = [], max(0, int(max_downloads))
    for d in bp._recent_sessions(n, end):                # newest first
        path = os.path.join(cache_dir, f"{d.isoformat()}.csv")
        frame = None
        if os.path.exists(path):
            try:
                frame = pd.read_csv(path)
            except Exception as e:
                _log.debug("breadth: unreadable %s: %s", path, e)
        elif budget > 0:
            budget -= 1
            frame = bp._load_session(d, download or bp._default_download, cache_dir)
        if frame is None or frame.empty or not {"SYMBOL", "Close"} <= set(frame.columns):
            continue
        frame = frame.copy()
        frame["DATE"] = d.isoformat()                    # the file's session, whatever it says
        if "Volume" not in frame.columns:
            frame["Volume"] = float("nan")
        parts.append(frame)
    if not parts:
        return pd.DataFrame()
    return _fill_prev_close(pd.concat(parts, ignore_index=True))


def breadth_snapshot(n: int = DEFAULT_SESSIONS, **kw) -> Optional[dict]:
    """Latest-session breadth as a plain dict, or None when there is too little data."""
    try:
        from analysis.market_breadth import compute_breadth, snapshot_from_breadth
        sessions = load_sessions(n, **kw)
        if sessions.empty or sessions["DATE"].nunique() < MIN_SESSIONS:
            return None
        snap = snapshot_from_breadth(compute_breadth(sessions))
        if snap:
            snap["source"] = "bhavcopy"
            snap["universe"] = "all EQ"
            snap["universe_size"] = int(sessions["SYMBOL"].nunique())
        return snap
    except Exception as e:
        _log.debug("breadth_snapshot failed: %s: %s", type(e).__name__, e)
        return None


# -- Persisted snapshot (written by scripts/warm_tomorrow_watchlist.py) -------
KV_KEY = "market_breadth"
KV_USER = "_system"
MAX_AGE_DAYS = 4                # covers a weekend plus a holiday


def _today_ist() -> _dt.date:
    try:
        from zoneinfo import ZoneInfo
        return _dt.datetime.now(ZoneInfo("Asia/Kolkata")).date()
    except Exception:
        return _dt.date.today()


_MIN_SCAN_COVERAGE = 0.6


def snapshot_from_scan(tickers, universe: str, period: str = "2y",
                       peek: Optional[Callable] = None) -> Optional[dict]:
    """Breadth from the price frames a scan ALREADY fetched (data.fetcher.peek_cached).
    Never fetches; None when too few frames are cached, or when fewer than
    _MIN_SCAN_COVERAGE of the universe is still cached (the fetch cache expires
    after 5 min, so a slow scan would otherwise persist a partial "breadth")."""
    try:
        from analysis.market_breadth import breadth_from_price_frames
        if peek is None:
            from data.fetcher import peek_cached as peek
        tickers = list(tickers)
        frames = {}
        for tk in tickers:
            df = peek(tk, period=period)
            if df is None:   # score_stock caches under the resolved symbol
                try:
                    from data.universe import resolve_ticker
                    alias = resolve_ticker(tk.replace(".NS", ""))
                    df = peek(alias, period=period) if alias and alias != tk else None
                except Exception:
                    df = None
            if df is not None:
                frames[tk] = df
        if tickers and len(frames) < _MIN_SCAN_COVERAGE * len(tickers):
            _log.warning("breadth snapshot skipped: only %d/%d frames cached",
                         len(frames), len(tickers))
            return None
        snap = breadth_from_price_frames(frames)
        if snap:
            snap["source"] = "scan"
            snap["universe"] = universe
        return snap
    except Exception as e:
        _log.debug("snapshot_from_scan failed: %s: %s", type(e).__name__, e)
        return None


def persist_snapshot(snap: Optional[dict], kv_set: Optional[Callable] = None) -> bool:
    """Write ``snap`` to the shared trade_store KV. False on any failure."""
    if not snap:
        return False
    try:
        if kv_set is None:
            import trade_store
            kv_set = trade_store.kv_set
        payload = {"data": dict(snap),
                   "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")}
        return bool(kv_set(KV_KEY, payload, user_id=KV_USER))
    except Exception as e:
        _log.debug("persist_snapshot failed: %s: %s", type(e).__name__, e)
        return False


def persisted_snapshot(max_age_days: int = MAX_AGE_DAYS, *, today: Optional[_dt.date] = None,
                       kv_get: Optional[Callable] = None) -> Optional[dict]:
    """The KV snapshot when its as-of date is at most ``max_age_days`` calendar days old."""
    try:
        if kv_get is None:
            import trade_store
            kv_get = trade_store.kv_get
        raw = kv_get(KV_KEY, user_id=KV_USER)
        data = raw.get("data") if isinstance(raw, dict) else None
        if not (isinstance(data, dict) and data.get("date")):
            return None
        age = ((today or _today_ist()) - _dt.date.fromisoformat(str(data["date"])[:10])).days
        if age < 0 or age > max_age_days:
            return None
        out = dict(data)
        out.setdefault("source", "scan")
        out["generated_at"] = raw.get("generated_at")
        return out
    except Exception as e:
        _log.debug("persisted_snapshot failed: %s: %s", type(e).__name__, e)
        return None


def latest_snapshot(max_downloads: int = 0, *, today: Optional[_dt.date] = None,
                    kv_get: Optional[Callable] = None, **kw) -> Optional[dict]:
    """Fresh persisted snapshot, else the local bhavcopy computation, else None."""
    snap = persisted_snapshot(today=today, kv_get=kv_get)
    if snap:
        return snap
    return breadth_snapshot(max_downloads=max_downloads, **kw)
