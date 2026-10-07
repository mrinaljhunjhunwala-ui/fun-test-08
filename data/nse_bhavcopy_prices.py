"""
NSE bhavcopy daily-close fallback — the last tier of ``data.fetcher.fetch_single``.

Why: the daily tiers are Angel One (credentials only) → Stooq (gated off; JS
challenge) → Yahoo. Without this module Yahoo is a single point of failure.

Source: ``sec_bhavdata_full_DDMMYYYY.csv`` from nsearchives.nseindia.com — the
same cookie-less archive ``data/nse_delivery.py`` already pulls (Chrome UA).

Design
- One parsed file per session is cached on disk under ``data/cache/bhavcopy/``
  (gitignored) as a small CSV of EQ/BE OHLCV rows. Only missing sessions are
  downloaded, so after the first call each new day costs one request.
- Sessions are enumerated with ``utils.nse_holidays.is_trading_day``.
- History is CAPPED at ``MAX_SESSIONS`` (60) regardless of ``period``; a longer
  request is served truncated with a WARNING.

Caveat — prices are NOT split/bonus adjusted. A raw bhavcopy series shows a
fake gap on the ex-date. We do not serve that: if any one-day close move
exceeds ``JUMP_THRESHOLD`` (30%; a 1:2 bonus is about -33%) while NIFTY moved
under 5% that day (checked against the fetcher's cached ^NSEI when present;
bhavcopy has no index rows), the frame is truncated to start at the jump,
with a WARNING.

Load: the walk starts at the last session whose file can exist (today only
after 18:30 IST), and missing dates are negatively cached in-process for
30 min, so a universe scan on this tier doesn't hammer nsearchives.

The returned frame carries ``attrs`` source/truncated/unadjusted/sessions,
which analysis.edge_flags turns into a red "degraded_source" flag.
"""
from __future__ import annotations

import csv
import datetime as _dt
import io
import logging
import os
import threading
import time
from typing import Callable, Dict, List, Optional

import pandas as pd

_log = logging.getLogger("data.nse_bhavcopy_prices")

MAX_SESSIONS = 60
JUMP_THRESHOLD = 0.30            # a 1:2 bonus is about -33%
NIFTY_CALM = 0.05               # only a corp action if NIFTY moved less than this
NEG_TTL_S = 30 * 60             # don't re-request a missing session for 30 min
_PUBLISH_IST = _dt.time(18, 30)  # bhavcopy is published in the evening
_MAX_CONSEC_MISSES = 5          # stop walking back after this many failed downloads
_SERIES = {"EQ", "BE"}
_REQUIRED = {"SYMBOL", "SERIES", "DATE1", "OPEN_PRICE", "HIGH_PRICE",
             "LOW_PRICE", "CLOSE_PRICE", "TTL_TRD_QNTY"}
_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", "bhavcopy")
_COLS = ["Open", "High", "Low", "Close", "Volume"]

_PERIOD_SESSIONS = {"1d": 1, "5d": 5, "1mo": 21, "3mo": 63, "6mo": 126,
                    "1y": 250, "2y": 500, "5y": 1250, "10y": 2500, "max": 2500, "ytd": 250}


def period_to_sessions(period: str) -> int:
    """Requested sessions for a yfinance-style period (uncapped)."""
    return _PERIOD_SESSIONS.get(str(period).lower(), 250)


def parse_ohlcv(csv_text: str) -> pd.DataFrame:
    """Parse a bhavcopy CSV into rows SYMBOL, DATE, Open..Volume (EQ/BE only).

    Named ValueError on schema drift (empty body / missing column / bad date).
    """
    if not csv_text or not csv_text.strip():
        raise ValueError("bhavcopy: empty response body")
    reader = csv.DictReader(io.StringIO(csv_text))
    fields = {(f or "").strip() for f in (reader.fieldnames or [])}
    missing = _REQUIRED - fields
    if missing:
        raise ValueError(f"NSE bhavcopy schema drift: required columns missing {sorted(missing)}")

    def _f(v: str) -> Optional[float]:
        try:
            return float(v) if v not in {"", "-"} else None
        except ValueError:
            return None

    out: List[Dict] = []
    for raw in reader:
        row = {(k or "").strip(): (v or "").strip() for k, v in raw.items()}
        if row.get("SERIES", "").upper() not in _SERIES:
            continue
        try:
            d = _dt.datetime.strptime(row["DATE1"], "%d-%b-%Y").date()
        except ValueError as e:
            raise ValueError(f"NSE bhavcopy schema drift: bad DATE1 {row.get('DATE1')!r}: {e}")
        out.append({"SYMBOL": row["SYMBOL"].upper(), "SERIES": row["SERIES"].upper(),
                    "DATE": d.isoformat(),
                    "Open": _f(row["OPEN_PRICE"]), "High": _f(row["HIGH_PRICE"]),
                    "Low": _f(row["LOW_PRICE"]), "Close": _f(row["CLOSE_PRICE"]),
                    "Volume": _f(row["TTL_TRD_QNTY"])})
    return pd.DataFrame(out, columns=["SYMBOL", "SERIES", "DATE", *_COLS])


def _default_download(d: _dt.date) -> str:
    from data.nse_delivery import _fetch_bhavcopy
    return _fetch_bhavcopy(d)


_NEG_CACHE: Dict[_dt.date, float] = {}
_NEG_LOCK = threading.Lock()


def _neg_cached(d: _dt.date) -> bool:
    with _NEG_LOCK:
        exp = _NEG_CACHE.get(d)
        if exp is None:
            return False
        if exp < time.monotonic():
            del _NEG_CACHE[d]
            return False
        return True


def _neg_put(d: _dt.date) -> None:
    with _NEG_LOCK:
        _NEG_CACHE[d] = time.monotonic() + NEG_TTL_S


def _now_ist() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=5, minutes=30)))


def last_published_session(now: Optional[_dt.datetime] = None) -> _dt.date:
    """Latest session whose bhavcopy can exist: today only after 18:30 IST."""
    from utils.nse_holidays import is_trading_day
    n = now or _now_ist()
    d = n.date()
    if is_trading_day(d) and n.time() >= _PUBLISH_IST:
        return d
    d -= _dt.timedelta(days=1)
    while not is_trading_day(d):
        d -= _dt.timedelta(days=1)
    return d


def _recent_sessions(n: int, today: _dt.date) -> List[_dt.date]:
    from utils.nse_holidays import is_trading_day
    out: List[_dt.date] = []
    d = today
    while len(out) < n and (today - d).days < n * 3 + 30:
        if is_trading_day(d):
            out.append(d)
        d -= _dt.timedelta(days=1)
    return out                  # newest first


def _load_session(d: _dt.date, download: Callable[[_dt.date], str],
                  cache_dir: str) -> Optional[pd.DataFrame]:
    path = os.path.join(cache_dir, f"{d.isoformat()}.csv")
    if os.path.exists(path):
        try:
            return pd.read_csv(path)
        except Exception as e:
            _log.debug("bhavcopy cache unreadable %s: %s — refetching", path, e)
    if _neg_cached(d):
        return None
    try:
        frame = parse_ohlcv(download(d))
    except Exception as e:
        _neg_put(d)
        _log.debug("bhavcopy session %s unavailable: %s: %s", d, type(e).__name__, e)
        return None
    try:
        os.makedirs(cache_dir, exist_ok=True)
        frame.to_csv(path, index=False)
    except OSError as e:
        _log.debug("bhavcopy cache write failed %s: %s", path, e)
    return frame


def _nifty_moves() -> Optional[pd.Series]:
    """Abs daily NIFTY moves from the fetcher's cache, or None (check skipped)."""
    try:
        from data.fetcher import peek_cached
        for p in ("2y", "1y", "5y"):
            nd = peek_cached("^NSEI", period=p)
            if nd is not None and not nd.empty:
                s = nd["Close"].pct_change().abs()
                s.index = pd.DatetimeIndex(s.index).normalize()
                return s
    except Exception as e:
        _log.debug("NIFTY cross-check unavailable: %s", e)
    return None


def _truncate_at_jump(df: pd.DataFrame, symbol: str,
                      nifty: Optional[pd.Series] = None) -> pd.DataFrame:
    """Drop everything up to an unadjusted split/bonus-like close jump."""
    chg = df["Close"].pct_change().abs()
    jumps = chg[chg > JUMP_THRESHOLD]
    if not jumps.empty:
        nifty = nifty if nifty is not None else _nifty_moves()
        if nifty is not None:
            calm = [ts for ts in jumps.index
                    if ts not in nifty.index or not (nifty.get(ts, 0) >= NIFTY_CALM)]
            jumps = jumps.loc[calm]
    if jumps.empty:
        return df
    cut = jumps.index[-1]
    _log.warning("bhavcopy: %s close moved %.0f%% on %s — likely unadjusted corporate "
                 "action; truncating history to start there", symbol,
                 jumps.iloc[-1] * 100, cut.date())
    return df.loc[cut:]


def fetch_bhavcopy_history(ticker: str, period: str = "2y", *,
                           download: Optional[Callable[[_dt.date], str]] = None,
                           today: Optional[_dt.date] = None,
                           cache_dir: Optional[str] = None,
                           nifty: Optional[pd.Series] = None) -> pd.DataFrame:
    """OHLCV frame (DatetimeIndex 'Date', Open/High/Low/Close/Volume) for one symbol.

    Raises ValueError when no session yields a row for the symbol.
    """
    symbol = ticker.upper().removesuffix(".NS").removesuffix(".BO")
    if ticker.startswith("^"):
        raise ValueError(f"bhavcopy has no index rows ({ticker})")
    want = period_to_sessions(period)
    n = min(want, MAX_SESSIONS)
    download = download or _default_download
    cache_dir = cache_dir or _CACHE_DIR

    rows, misses = [], 0
    for d in _recent_sessions(n, today or last_published_session()):
        frame = _load_session(d, download, cache_dir)
        if frame is None:
            misses += 1
            if misses >= _MAX_CONSEC_MISSES:
                _log.warning("bhavcopy: %d consecutive sessions unavailable — stopping", misses)
                break
            continue
        misses = 0
        hit = frame[frame["SYMBOL"] == symbol]
        if not hit.empty:
            # EQ preferred over BE if a symbol appears in both on one day.
            hit = hit.sort_values("SERIES", key=lambda s: s.ne("EQ")).iloc[:1]
            rows.append(hit)
    if not rows:
        raise ValueError(f"bhavcopy: no rows for {symbol} in last {n} sessions")

    df = pd.concat(rows)
    df.index = pd.DatetimeIndex(pd.to_datetime(df["DATE"]), name="Date")
    df = df[_COLS].astype(float).sort_index()
    df = df[~df.index.duplicated(keep="last")].dropna(subset=["Close"])
    df = _truncate_at_jump(df, symbol, nifty)
    df.attrs.update({"source": "NSE-bhavcopy", "unadjusted": True,
                     "truncated": want > len(df), "sessions": len(df)})
    if want > MAX_SESSIONS:
        _log.warning("bhavcopy: history truncated for %s — served %d sessions of %d "
                     "requested (period=%s); prices are NOT split/bonus adjusted",
                     symbol, len(df), want, period)
    return df
