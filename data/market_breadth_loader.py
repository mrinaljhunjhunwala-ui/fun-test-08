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
    """Files cached by the production reader lack PREV_CLOSE; use the prior session's close."""
    df = df.sort_values(["SYMBOL", "DATE"])
    prior = df.groupby("SYMBOL")["Close"].shift(1)
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


def _num(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def breadth_snapshot(n: int = DEFAULT_SESSIONS, **kw) -> Optional[dict]:
    """Latest-session breadth as a plain dict, or None when there is too little data."""
    try:
        from analysis.market_breadth import breadth_regime, compute_breadth
        sessions = load_sessions(n, **kw)
        if sessions.empty or sessions["DATE"].nunique() < MIN_SESSIONS:
            return None
        b = compute_breadth(sessions)
        if b.empty:
            return None
        last = b.iloc[-1]
        if not (last.get("advances", 0) + last.get("declines", 0)):
            return None
        label = str(breadth_regime(b).iloc[-1])
        return {
            "date": pd.Timestamp(b.index[-1]).date().isoformat(),
            "sessions": int(len(b)),
            "advances": int(last["advances"]),
            "declines": int(last["declines"]),
            "unchanged": int(last["unchanged"]),
            "ad_ratio": _num(last.get("ad_ratio")),
            "pct_above_20dma": _num(last.get("pct_above_20dma")),
            "pct_above_50dma": _num(last.get("pct_above_50dma")),
            "pct_above_200dma": _num(last.get("pct_above_200dma")),
            "new_52w_highs": _num(last.get("new_52w_highs")),
            "new_52w_lows": _num(last.get("new_52w_lows")),
            "breadth_label": label,
        }
    except Exception as e:
        _log.debug("breadth_snapshot failed: %s: %s", type(e).__name__, e)
        return None
