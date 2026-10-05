"""
data/angel_fetcher.py
Angel One SmartAPI — Tier 0 data source for NSE/BSE historical + live data.

This file includes:
 - tolerant JSON parsing helpers (handles double-encoded or text payloads)
 - instrument-master pre-load and O(1) token lookup
 - rate limiting + circuit breakers for token lookups and login
 - correct failure recording: missing token counts as a failure (so breakers trip)
 - full set of public helper functions used by the app/tests
"""
from __future__ import annotations

import logging
import os
import time
import threading
import datetime
import json
from collections import deque
from typing import Dict, List, Optional, Tuple, Any

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

_log = logging.getLogger("angel_fetcher")

_http = requests.Session()
_http.mount("https://", HTTPAdapter(
    max_retries=Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
    )
))

_SESSION: Dict = {"jwt": None, "feed_token": None, "api_key": "", "ts": 0.0}
_SESSION_LOCK = threading.Lock()
_TOKEN_CACHE: Dict[str, Tuple[Optional[str], float]] = {}
_TOKEN_TTL = 3600 * 6
_TOKEN_FAIL_TTL = 300

_INST_MAP: Dict[str, str] = {}
_INST_MAP_LOCK = threading.Lock()
_INST_MAP_TS: List[float] = [0.0]
_INST_MAP_TTL = 3600 * 4
_INST_MASTER_URL = (
    "https://margincalculator.angelbroking.com"
    "/OpenAPI_File/files/OpenAPIScripMaster.json"
)

# In-process activity log for the Angel One page: logins, breaker trips,
# instrument-master loads, manual session clears. Newest last; lost on
# restart (it describes this server process, nothing more).
_EVENTS: deque = deque(maxlen=40)
_EVENTS_LOCK = threading.Lock()


def _event(kind: str, detail: str, tone: str = "neutral") -> None:
    with _EVENTS_LOCK:
        _EVENTS.append({"ts": time.time(), "kind": kind, "detail": detail, "tone": tone})


def _safe_parse_json_response(resp: requests.Response) -> Optional[Any]:
    """
    Tolerantly parse a requests.Response-like object:
      - prefer resp.json()
      - if that returns a str containing JSON, parse it
      - if resp.json() raises, fall back to json.loads(resp.text)
    Returns parsed JSON (dict/list/...) or None.
    """
    if resp is None:
        return None
    # If the object already is a dict/list (tests sometimes pass that in), return it
    try:
        # Some fake responses implement .json() as returning a dict/list directly.
        parsed = resp.json()
    except Exception as e:
        # Fall back to text parsing
        text = getattr(resp, "text", None)
        if not text:
            _log.debug("angel_fetcher: response has no .json() and no .text: %s", e)
            return None
        try:
            parsed = json.loads(text)
        except Exception as ex:
            _log.debug("angel_fetcher: json.loads(resp.text) failed: %s", ex)
            return None

    # If parsed is a string that itself contains JSON, try to decode it
    if isinstance(parsed, str):
        try:
            parsed2 = json.loads(parsed)
            parsed = parsed2
        except Exception:
            # keep original string if not JSON
            return None

    return parsed


def _load_instrument_master() -> bool:
    """
    Load Angel's public instrument master into _INST_MAP:
    mapping: bare symbol (no -EQ suffix) -> token (string)
    """
    global _INST_MAP, _INST_MAP_TS

    now = time.time()
    if _INST_MAP and (now - _INST_MAP_TS[0]) < _INST_MAP_TTL:
        return True

    with _INST_MAP_LOCK:
        if _INST_MAP and (now - _INST_MAP_TS[0]) < _INST_MAP_TTL:
            return True
        try:
            req = _http.get(_INST_MASTER_URL, timeout=20)
            req.raise_for_status()
            instruments = _safe_parse_json_response(req)
            if not instruments or not isinstance(instruments, list):
                _log.warning("angel_fetcher: instrument master load returned unexpected shape: %s", type(instruments))
                return False
            new_map: Dict[str, str] = {}
            for inst in instruments:
                if not isinstance(inst, dict):
                    continue
                exch = inst.get("exch_seg", "")
                itype = inst.get("instrumenttype", "")
                raw_sym = str(inst.get("symbol", "")).strip().upper()
                tok = inst.get("token", "") or inst.get("symboltoken", "")
                if (
                    exch == "NSE"
                    and itype in ("", "EQ")
                    and raw_sym.endswith("-EQ")
                    and tok
                ):
                    sym = raw_sym[:-3]
                    new_map[sym] = str(tok)
            if len(new_map) < 500:
                _log.warning(
                    "angel_fetcher: instrument master loaded only %d NSE EQ entries — skipping",
                    len(new_map),
                )
                return False
            _INST_MAP = new_map
            _INST_MAP_TS[0] = time.time()
            _event("Instruments", f"{len(new_map):,} NSE equity tokens loaded", "good")
            _log.info(
                "angel_fetcher: instrument master loaded — %d NSE EQ tokens cached",
                len(new_map),
            )
            return True
        except Exception as e:
            _log.warning("angel_fetcher: instrument master load failed: %s", e)
            return False


_HIST_LOCK = threading.Lock()
_HIST_LAST = [0.0]
_HIST_MIN_INTERVAL = 1.0


def _angel_rate_limit() -> None:
    with _HIST_LOCK:
        elapsed = time.time() - _HIST_LAST[0]
        if elapsed < _HIST_MIN_INTERVAL:
            time.sleep(_HIST_MIN_INTERVAL - elapsed)
        _HIST_LAST[0] = time.time()


_hist_rate_limit = _angel_rate_limit


# FIX AO-BULK (2026-09-28): Angel's candle API answers 403 "Access denied
# because of exceeding access rate" after ~90 calls/minute, and a longer-window
# quota cut a 1 req/s scan off after ~700 calls. Queueing every thread behind
# the 1 req/s limiter made a ~745-ticker scan take ~25 min. Instead, at most
# ONE thread uses Angel for candles at a time, within a per-minute budget;
# everyone else gets AngelBusy immediately and the caller falls through to
# the next provider. Single lookups still get Angel; bulk scans run at Yahoo
# speed.
_HIST_PER_MIN = 60
_HIST_SENT: deque = deque()
_HIST_INFLIGHT = threading.Semaphore(1)


class AngelBusy(Exception):
    """Angel's candle budget is in use or spent — use another provider now."""


def _hist_budget_take(k: int) -> bool:
    with _HIST_LOCK:
        now = time.time()
        while _HIST_SENT and now - _HIST_SENT[0] >= 60:
            _HIST_SENT.popleft()
        if len(_HIST_SENT) + k > _HIST_PER_MIN:
            return False
        _HIST_SENT.extend([now] * k)
        return True

_BREAKER_LOCK = threading.Lock()
_BREAKER_FAILS = [0]
_BREAKER_TRIPPED_AT = [0.0]
_BREAKER_THRESHOLD = 5
_BREAKER_COOLDOWN = 300


def _breaker_tripped() -> bool:
    with _BREAKER_LOCK:
        if _BREAKER_TRIPPED_AT[0]:
            if time.time() - _BREAKER_TRIPPED_AT[0] < _BREAKER_COOLDOWN:
                return True
            # cooldown passed -> reset
            _BREAKER_TRIPPED_AT[0] = 0.0
            _BREAKER_FAILS[0] = 0
        return False


def _breaker_record_failure() -> None:
    with _BREAKER_LOCK:
        _BREAKER_FAILS[0] += 1
        if _BREAKER_FAILS[0] >= _BREAKER_THRESHOLD and not _BREAKER_TRIPPED_AT[0]:
            _BREAKER_TRIPPED_AT[0] = time.time()
            _event("Data breaker", f"tripped after {_BREAKER_FAILS[0]} failures · "
                   f"paused {_BREAKER_COOLDOWN // 60} min", "bad")
            _log.warning(
                "angel_fetcher: circuit breaker TRIPPED after %d consecutive failures — "
                "pausing Angel One calls for %ds",
                _BREAKER_FAILS[0], _BREAKER_COOLDOWN,
            )


def _breaker_record_success() -> None:
    with _BREAKER_LOCK:
        _BREAKER_FAILS[0] = 0
        _BREAKER_TRIPPED_AT[0] = 0.0


# -- Circuit breaker (login) — FIX AO5 ----------------------------------------
_LOGIN_BREAKER_LOCK = threading.Lock()
_LOGIN_BREAKER_FAILS = [0]
_LOGIN_BREAKER_TRIPPED_AT = [0.0]
_LOGIN_BREAKER_THRESHOLD = 5
_LOGIN_BREAKER_COOLDOWN = 300


def _login_breaker_tripped() -> bool:
    with _LOGIN_BREAKER_LOCK:
        if _LOGIN_BREAKER_TRIPPED_AT[0]:
            if time.time() - _LOGIN_BREAKER_TRIPPED_AT[0] < _LOGIN_BREAKER_COOLDOWN:
                return True
            _LOGIN_BREAKER_TRIPPED_AT[0] = 0.0
            _LOGIN_BREAKER_FAILS[0] = 0
        return False


def _login_breaker_record_failure() -> None:
    with _LOGIN_BREAKER_LOCK:
        _LOGIN_BREAKER_FAILS[0] += 1
        if _LOGIN_BREAKER_FAILS[0] >= _LOGIN_BREAKER_THRESHOLD and not _LOGIN_BREAKER_TRIPPED_AT[0]:
            _LOGIN_BREAKER_TRIPPED_AT[0] = time.time()
            _event("Login breaker", f"tripped after {_LOGIN_BREAKER_FAILS[0]} failures · "
                   f"paused {_LOGIN_BREAKER_COOLDOWN // 60} min", "bad")
            _log.warning(
                "angel_fetcher: LOGIN circuit breaker TRIPPED after %d consecutive "
                "login failures — pausing all Angel One login attempts for %ds. "
                "Check ANGEL_TOTP_SECRET / ANGEL_PASSWORD are still current.",
                _LOGIN_BREAKER_FAILS[0], _LOGIN_BREAKER_COOLDOWN,
            )


def _login_breaker_record_success() -> None:
    with _LOGIN_BREAKER_LOCK:
        _LOGIN_BREAKER_FAILS[0] = 0
        _LOGIN_BREAKER_TRIPPED_AT[0] = 0.0


_BASE = "https://apiconnect.angelbroking.com"

_INTERVAL_MAP: Dict[str, str] = {
    "1d": "ONE_DAY",
    "1wk": "ONE_WEEK",
    "1mo": "ONE_MONTH",
    "5m": "FIVE_MINUTE",
    "15m": "FIFTEEN_MINUTE",
    "30m": "THIRTY_MINUTE",
    "60m": "ONE_HOUR",
    "1h": "ONE_HOUR",
}

_PERIOD_DAYS: Dict[str, int] = {
    "1d": 10, "5d": 18, "1m": 35, "6m": 185,
    "1y": 370, "2y": 740, "5y": 1830, "10y": 3650, "max": 1830,
}

# Angel SmartAPI historical/getCandleData caps a single request at ~2000 daily
# bars (~5.5 years). Any period longer than this window is fetched in chunks
# and stitched. 700 days per chunk keeps us safely inside that ceiling while
# minimising round-trips for the 10y case (~5-6 chunks per ticker).
_CANDLE_CHUNK_DAYS = 700


def _secrets_section() -> Any:
    """The [angel_one] table from st.secrets — the single read point, so
    tests/conftest.py can blank it and never pick up real credentials."""
    import streamlit as st
    return st.secrets.get("angel_one", {})


def _get_credentials() -> Dict[str, str]:
    creds: Dict[str, str] = {}
    try:
        ao = _secrets_section()
        creds = {
            "api_key": str(ao.get("api_key", "")),
            "client_id": str(ao.get("client_id", "")),
            "password": str(ao.get("password", "")),
            "totp_secret": str(ao.get("totp_secret", "")),
        }
    except Exception as e:
        _log.debug("_get_credentials: st.secrets['angel_one'] unavailable, falling back to env vars: %s", e)
    for key, env in (
        ("api_key", "ANGEL_API_KEY"),
        ("client_id", "ANGEL_CLIENT_ID"),
        ("password", "ANGEL_PASSWORD"),
        ("totp_secret", "ANGEL_TOTP_SECRET"),
    ):
        if not creds.get(key):
            creds[key] = os.environ.get(env, "")
    return creds


def is_configured() -> bool:
    c = _get_credentials()
    return bool(c["api_key"] and c["client_id"] and c["password"] and c["totp_secret"])


def _auth_headers(jwt: str, api_key: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {jwt}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-UserType": "USER",
        "X-SourceID": "WEB",
        "X-ClientLocalIP": "127.0.0.1",
        "X-ClientPublicIP": "127.0.0.1",
        "X-MACAddress": "00:00:00:00:00:00",
        "X-PrivateKey": api_key,
    }


def _get_session() -> Optional[Dict]:
    """
    Login and return session dict (jwt, feed_token, api_key).
    Cached for 50 minutes. Thread-safe via double-check locking.
    """
    global _SESSION
    if _SESSION["jwt"] and time.time() - _SESSION["ts"] < 3000:
        return _SESSION

    with _SESSION_LOCK:
        if _SESSION["jwt"] and time.time() - _SESSION["ts"] < 3000:
            return _SESSION

        # Bail out if login breaker tripped
        if _login_breaker_tripped():
            return None

        creds = _get_credentials()
        if not all(creds.values()):
            return None

        try:
            import pyotp
            totp_code = pyotp.TOTP(creds["totp_secret"]).now()
            resp = _http.post(
                f"{_BASE}/rest/auth/angelbroking/user/v1/loginByPassword",
                json={
                    "clientcode": creds["client_id"],
                    "password": creds["password"],
                    "totp": totp_code,
                },
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "X-UserType": "USER",
                    "X-SourceID": "WEB",
                    "X-ClientLocalIP": "127.0.0.1",
                    "X-ClientPublicIP": "127.0.0.1",
                    "X-MACAddress": "00:00:00:00:00:00",
                    "X-PrivateKey": creds["api_key"],
                },
                timeout=15,
            )
            parsed = _safe_parse_json_response(resp)
            if not parsed or not parsed.get("status"):
                _msg = (parsed or {}).get("message", "unknown")
                _log.warning("angel_fetcher: login failed — %s", _msg)
                _event("Session", f"login refused · {str(_msg)[:80]}", "bad")
                _login_breaker_record_failure()
                return None

            _SESSION.update({
                "jwt": parsed["data"]["jwtToken"],
                "feed_token": parsed["data"]["feedToken"],
                "api_key": creds["api_key"],
                "ts": time.time(),
            })
            _login_breaker_record_success()
            _event("Session", "logged in · TOTP verified", "good")
            return _SESSION

        except Exception as e:
            _log.warning("angel_fetcher._get_session failed: %s", e)
            _event("Session", f"login error · {type(e).__name__}", "bad")
            _login_breaker_record_failure()
            return None


def _get_token(symbol_base: str, session: Dict) -> Optional[str]:
    """
    Resolve and cache Angel token for a bare symbol (no .NS suffix).
    Uses _INST_MAP first, falls back to searchScrip.
    Correctly treats a successful HTTP response that contains no matching token
    as a failure (so circuit breaker increments).
    """
    key = symbol_base.upper()
    _search_key = key.replace("&", "%26") if "&" in key else key

    if key in _TOKEN_CACHE:
        token, ts = _TOKEN_CACHE[key]
        ttl = _TOKEN_TTL if token is not None else _TOKEN_FAIL_TTL
        if time.time() - ts < ttl:
            return token

    if not _INST_MAP:
        _load_instrument_master()

    if key in _INST_MAP:
        token = _INST_MAP[key]
        _TOKEN_CACHE[key] = (token, time.time())
        return token

    if _breaker_tripped():
        return None

    try:
        _angel_rate_limit()
        resp = _http.post(
            f"{_BASE}/rest/secure/angelbroking/order/v1/searchScrip",
            json={"exchange": "NSE", "searchscrip": _search_key},
            headers=_auth_headers(session["jwt"], session["api_key"]),
            timeout=10,
        )
        # Treat HTTP >=400 as failure
        status = getattr(resp, "status_code", None)
        if status and status >= 400:
            _log.warning("angel_fetcher._get_token searchScrip returned HTTP %s for %s", status, key)
            _TOKEN_CACHE[key] = (None, time.time())
            _breaker_record_failure()
            return None

        parsed = _safe_parse_json_response(resp)
        scrips = (parsed.get("data") if isinstance(parsed, dict) else None) or []
        token: Optional[str] = None

        # scrips might be dict/list; normalize to list of dicts
        if isinstance(scrips, dict):
            # if data is a dict with 'results' or similar
            scrips = scrips.get("result") or scrips.get("scrips") or []
        if not isinstance(scrips, list):
            scrips = []

        for s in scrips:
            if not isinstance(s, dict):
                continue
            sym = s.get("tradingsymbol", "").upper()
            itype = s.get("instrumenttype", "").upper()
            # symboltoken vs token key naming
            stoken = s.get("symboltoken") or s.get("token") or s.get("symbolToken")
            if sym in (key, _search_key) and itype in ("", "EQ", "-EQ"):
                token = str(stoken)
                break

        # If still no token, pick first available symboltoken if present
        if token is None and scrips:
            for s in scrips:
                stoken = s.get("symboltoken") or s.get("token") or s.get("symbolToken")
                if stoken:
                    token = str(stoken)
                    break

        if token is None:
            # No token found -> record failure
            _TOKEN_CACHE[key] = (None, time.time())
            _breaker_record_failure()
            _log.debug("angel_fetcher._get_token(%s) no token found in searchScrip response", key)
            return None

        _TOKEN_CACHE[key] = (token, time.time())
        _breaker_record_success()
        return token

    except Exception as e:
        _log.warning("angel_fetcher._get_token(%s) failed: %s", key, e)
        _TOKEN_CACHE[key] = (None, time.time())
        _breaker_record_failure()
        return None


def _period_to_dates(period: str) -> Tuple[str, str]:
    today = datetime.date.today()
    if period == "ytd":
        start = datetime.date(today.year, 1, 1)
    else:
        days = _PERIOD_DAYS.get(period, 370)
        start = today - datetime.timedelta(days=days)
    _fmt = "%Y-%m-%d %H:%M"
    fromdate = datetime.datetime.combine(start, datetime.time(9, 15)).strftime(_fmt)
    todate = datetime.datetime.combine(today, datetime.time(15, 30)).strftime(_fmt)
    return fromdate, todate


def fetch_historical(
    ticker: str,
    period: str = "1y",
    interval: str = "1d",
) -> Optional[pd.DataFrame]:
    session = _get_session()
    if session is None:
        return None

    symbol = ticker.replace(".NS", "").replace(".BO", "").upper()
    # FIX AO-403 (2026-09-28): tokens usually come from the cached instrument
    # master, which returns before _get_token ever consults the breaker, so a
    # tripped breaker never stopped candle calls. Check it here.
    if _breaker_tripped():
        # FIX AO-BULK: a known-open breaker is a skip, not a fresh failure —
        # callers fall through quietly instead of logging one WARNING per ticker.
        raise AngelBusy("circuit breaker open")
    token = _get_token(symbol, session)
    if token is None:
        return None

    ao_interval = _INTERVAL_MAP.get(interval, "ONE_DAY")

    # Compute the requested calendar-day span so we can decide whether to
    # single-shot or chunk. YTD is bounded (< 1 year) and single-shots.
    today = datetime.date.today()
    if period == "ytd":
        start_date = datetime.date(today.year, 1, 1)
    else:
        start_date = today - datetime.timedelta(days=_PERIOD_DAYS.get(period, 370))
    span_days = (today - start_date).days

    n_windows = 1 if span_days <= _CANDLE_CHUNK_DAYS else -(-(span_days + 1) // (_CANDLE_CHUNK_DAYS + 1))
    if not _HIST_INFLIGHT.acquire(blocking=False):
        raise AngelBusy("another candle fetch in flight")
    try:
        if not _hist_budget_take(n_windows):
            raise AngelBusy("per-minute candle budget spent")
        return _fetch_historical_windows(session, token, symbol, ao_interval, period,
                                         today, start_date, span_days)
    finally:
        _HIST_INFLIGHT.release()


def _fetch_historical_windows(
    session: Dict, token: str, symbol: str, ao_interval: str, period: str,
    today: datetime.date, start_date: datetime.date, span_days: int,
) -> Optional[pd.DataFrame]:
    try:
        if span_days <= _CANDLE_CHUNK_DAYS:
            fromdate, todate = _period_to_dates(period)
            df = _fetch_candles_window(session, token, ao_interval, fromdate, todate)
        else:
            # Chunked path: slice into ~700-day windows, fetch each, concat.
            frames: List[pd.DataFrame] = []
            fmt = "%Y-%m-%d %H:%M"
            cur_start = start_date
            while cur_start <= today:
                cur_end = min(today, cur_start + datetime.timedelta(days=_CANDLE_CHUNK_DAYS))
                fd = datetime.datetime.combine(cur_start, datetime.time(9, 15)).strftime(fmt)
                td = datetime.datetime.combine(cur_end,   datetime.time(15, 30)).strftime(fmt)
                chunk = _fetch_candles_window(session, token, ao_interval, fd, td)
                if chunk is not None and not chunk.empty:
                    frames.append(chunk)
                elif _breaker_tripped():
                    break          # FIX AO-403: stop paying for windows that will 403 too
                # Advance past this window's end (no overlap needed — endpoint
                # returns inclusive rows on both bounds; dedupe below handles
                # any single-bar overlap defensively).
                cur_start = cur_end + datetime.timedelta(days=1)
            if not frames:
                return None
            df = pd.concat(frames).sort_index()
            df = df[~df.index.duplicated(keep="first")]

        return df if df is not None and not df.empty else None

    except Exception as e:
        _log.warning("angel_fetcher.fetch_historical(%s) failed: %s", symbol, e)
        _SESSION["jwt"] = None
        return None


def _fetch_candles_window(
    session: Dict, token: str, ao_interval: str, fromdate: str, todate: str,
) -> Optional[pd.DataFrame]:
    """Single Angel getCandleData round-trip. Callers own chunking + retries."""
    _hist_rate_limit()
    resp = _http.post(
        f"{_BASE}/rest/secure/angelbroking/historical/v1/getCandleData",
        json={
            "exchange": "NSE",
            "symboltoken": token,
            "interval": ao_interval,
            "fromdate": fromdate,
            "todate": todate,
        },
        headers=_auth_headers(session["jwt"], session["api_key"]),
        timeout=20,
    )
    status = getattr(resp, "status_code", None)
    if status and status >= 400:
        _log.warning("angel_fetcher._fetch_candles_window: API returned %s (%s -> %s)",
                     status, fromdate, todate)
        # FIX AO-403 (2026-09-28): an HTTP error is a failure. It used to record
        # nothing, so from GitHub's runners (403 on every call) the breaker
        # tripped once in 333 rejections and every ticker kept paying for
        # two failing calls before the Yahoo fallback.
        _breaker_record_failure()
        return None

    parsed = _safe_parse_json_response(resp)
    candles = (parsed.get("data") if isinstance(parsed, dict) else None) or []
    if not candles:
        return None        # a 200 with no candles is legitimate (illiquid / new listing)
    _breaker_record_success()

    df = pd.DataFrame(candles, columns=["Date", "Open", "High", "Low", "Close", "Volume"])
    df["Date"] = pd.to_datetime(df["Date"])
    df = df.set_index("Date").sort_index()
    df = df.apply(pd.to_numeric, errors="coerce")
    df.dropna(subset=["Close"], inplace=True)
    return df if not df.empty else None


def get_full_quote(ticker: str) -> Optional[Dict]:
    session = _get_session()
    if session is None:
        return None

    symbol = ticker.replace(".NS", "").replace(".BO", "").upper()
    token = _get_token(symbol, session)
    if token is None:
        return None

    try:
        resp = _http.post(
            f"{_BASE}/rest/secure/angelbroking/market/v1/quote/",
            json={"mode": "FULL", "exchangeTokens": {"NSE": [token]}},
            headers=_auth_headers(session["jwt"], session["api_key"]),
            timeout=10,
        )
        status = getattr(resp, "status_code", None)
        if status and status >= 400:
            _log.warning("angel_fetcher.get_full_quote: quote API returned %s", status)
            return None

        parsed = _safe_parse_json_response(resp)
        fetched = (parsed.get("data", {}).get("fetched") if isinstance(parsed, dict) else None) or []
        if not fetched:
            return None

        q = fetched[0]
        price = float(q.get("ltp", 0))
        prev_close = float(q.get("close", price))
        if price <= 0:
            return None

        return {
            "ticker": symbol,
            "price": price,
            "prev_close": prev_close,
            "chg_pct": (price / prev_close - 1) * 100 if prev_close > 0 else 0.0,
            "open": float(q.get("open", 0)),
            "high": float(q.get("high", 0)),
            "low": float(q.get("low", 0)),
            "volume": int(q.get("tradeVolume", 0) or 0),
            "avg_price": float(q.get("avgPrice", 0) or 0),
            "upper_circuit": float(q.get("upperCircuit", 0) or 0),
            "lower_circuit": float(q.get("lowerCircuit", 0) or 0),
            "week_52_high": float(q.get("52WeekHigh", 0) or 0),
            "week_52_low": float(q.get("52WeekLow", 0) or 0),
            "oi": int(q.get("opnInterest", 0) or 0),
            "bid": float((q.get("depth", {}).get("buy", [{}])[0] or {}).get("price", 0)),
            "ask": float((q.get("depth", {}).get("sell", [{}])[0] or {}).get("price", 0)),
            "bid_qty": int((q.get("depth", {}).get("buy", [{}])[0] or {}).get("quantity", 0)),
            "ask_qty": int((q.get("depth", {}).get("sell", [{}])[0] or {}).get("quantity", 0)),
            "net_chg": price - prev_close,
        }

    except Exception as e:
        _log.warning("angel_fetcher.get_full_quote(%s) failed: %s", symbol, e)
        return None


def get_live_quote(ticker: str) -> Optional[Dict]:
    full = get_full_quote(ticker)
    if full is None:
        return None
    return {
        "price": full["price"],
        "prev_close": full["prev_close"],
        "chg_pct": full["chg_pct"],
    }


def clear_session() -> None:
    global _SESSION
    _SESSION["jwt"] = None
    _event("Session", "cleared · next request logs in again", "neutral")


_SESSION_REUSE_S = 3000   # _get_session re-logs in after this many seconds


def status() -> Dict[str, Any]:
    """Read-only health snapshot for the Angel One page. No network calls.

    Everything here is this server process's own state: the cached login,
    both circuit breakers, the candle budget, the instrument / token caches
    and the recent event log (newest first).
    """
    now = time.time()
    ts = float(_SESSION.get("ts") or 0.0)
    logged_in = bool(_SESSION.get("jwt")) and (now - ts) < _SESSION_REUSE_S
    with _HIST_LOCK:
        candle_calls = sum(1 for t in _HIST_SENT if now - t < 60)
    with _EVENTS_LOCK:
        events = list(_EVENTS)[::-1]
    return {
        "configured": is_configured(),
        "logged_in": logged_in,
        "login_at": ts or None,
        "renews_in_s": max(0.0, _SESSION_REUSE_S - (now - ts)) if logged_in else None,
        "login_breaker_open": _login_breaker_tripped(),
        "login_fails": _LOGIN_BREAKER_FAILS[0],
        "data_breaker_open": _breaker_tripped(),
        "data_fails": _BREAKER_FAILS[0],
        "breaker_threshold": _BREAKER_THRESHOLD,
        "breaker_cooldown_s": _BREAKER_COOLDOWN,
        "candle_calls_last_min": candle_calls,
        "candle_budget_per_min": _HIST_PER_MIN,
        "instruments": len(_INST_MAP),
        "instruments_loaded_at": _INST_MAP_TS[0] or None,
        "tokens_cached": sum(1 for tok, _ in _TOKEN_CACHE.values() if tok),
        "events": events,
    }


def _mask(key: str, value: str) -> str:
    if not value:
        return ""
    if key in ("password", "totp_secret"):
        return "stored"          # never any part of a secret
    tail = value[-4:] if len(value) > 6 else ""
    head = value[0] if key == "client_id" and len(value) > 6 else ""
    return f"{head}••••{tail}"


def credential_summary() -> Dict[str, Dict[str, str]]:
    """Per credential: where it came from ("secrets" / "env" / "") and a
    masked form (last four characters of the key and client code only;
    password and TOTP seed are just "stored"). Never returns a raw value."""
    from_secrets: Dict[str, str] = {}
    try:
        ao = _secrets_section()
        from_secrets = {k: str(ao.get(k, "")) for k in ("api_key", "client_id", "password", "totp_secret")}
    except Exception as e:
        _log.debug("credential_summary: st.secrets unavailable: %s", e)
    out: Dict[str, Dict[str, str]] = {}
    for key, env in (("client_id", "ANGEL_CLIENT_ID"), ("api_key", "ANGEL_API_KEY"),
                     ("password", "ANGEL_PASSWORD"), ("totp_secret", "ANGEL_TOTP_SECRET")):
        val, src = from_secrets.get(key, ""), "secrets"
        if not val:
            val, src = os.environ.get(env, ""), "env"
        out[key] = {"source": src if val else "", "masked": _mask(key, val)}
    return out


def get_batch_quotes(tickers: List[str]) -> Dict[str, Optional[Dict]]:
    if not tickers:
        return {}

    session = _get_session()
    if session is None:
        return {t: None for t in tickers}

    symbols = [t.replace(".NS", "").replace(".BO", "").upper() for t in tickers]
    token_map = {}
    valid_tokens: List[str] = []

    for orig, sym in zip(tickers, symbols):
        token = _get_token(sym, session)
        if token:
            token_map[token] = orig
            valid_tokens.append(token)

    if not valid_tokens:
        return {t: None for t in tickers}

    results: Dict[str, Optional[Dict]] = {t: None for t in tickers}
    BATCH = 50

    for i in range(0, len(valid_tokens), BATCH):
        batch = valid_tokens[i: i + BATCH]
        try:
            _hist_rate_limit()
            resp = _http.post(
                f"{_BASE}/rest/secure/angelbroking/market/v1/quote/",
                json={"mode": "FULL", "exchangeTokens": {"NSE": batch}},
                headers=_auth_headers(session["jwt"], session["api_key"]),
                timeout=15,
            )
            status = getattr(resp, "status_code", None)
            if status and status >= 400:
                _log.warning("angel_fetcher.get_batch_quotes batch %d returned HTTP %s", i // BATCH, status)
                continue
            parsed = _safe_parse_json_response(resp)
            fetched = (parsed.get("data", {}).get("fetched") if isinstance(parsed, dict) else None) or []
            for q in fetched:
                tok = str(q.get("symbolToken") or q.get("symboltoken") or "")
                orig = token_map.get(tok)
                if orig is None:
                    continue
                price = float(q.get("ltp", 0))
                prev_close = float(q.get("close", price))
                if price <= 0:
                    continue
                results[orig] = {
                    "ticker": orig,
                    "price": price,
                    "prev_close": prev_close,
                    "chg_pct": (price / prev_close - 1) * 100 if prev_close > 0 else 0.0,
                    "open": float(q.get("open", 0)),
                    "high": float(q.get("high", 0)),
                    "low": float(q.get("low", 0)),
                    "volume": int(q.get("tradeVolume", 0) or 0),
                    # avgPrice is Angel's average traded price, i.e. session VWAP.
                    "vwap": float(q.get("avgPrice", 0) or 0),
                    "upper_circuit": float(q.get("upperCircuit", 0) or 0),
                    "lower_circuit": float(q.get("lowerCircuit", 0) or 0),
                    "week_52_high": float(q.get("52WeekHigh", 0) or 0),
                    "week_52_low": float(q.get("52WeekLow", 0) or 0),
                    "net_chg": price - prev_close,
                }
        except Exception as e:
            _log.warning("angel_fetcher.get_batch_quotes batch %d failed: %s", i // BATCH, e)
            continue

    return results


def get_market_depth(ticker: str) -> Optional[Dict]:
    session = _get_session()
    if session is None:
        return None

    symbol = ticker.replace(".NS", "").replace(".BO", "").upper()
    token = _get_token(symbol, session)
    if token is None:
        return None

    try:
        resp = _http.post(
            f"{_BASE}/rest/secure/angelbroking/market/v1/quote/",
            json={"mode": "FULL", "exchangeTokens": {"NSE": [token]}},
            headers=_auth_headers(session["jwt"], session["api_key"]),
            timeout=10,
        )
        parsed = _safe_parse_json_response(resp)
        fetched = (parsed.get("data", {}).get("fetched") if isinstance(parsed, dict) else None) or []
        if not fetched:
            return None

        q = fetched[0]
        depth = q.get("depth", {})
        buys = depth.get("buy", []) or []
        sells = depth.get("sell", []) or []

        def _parse_levels(levels: list) -> List[Dict]:
            return [
                {
                    "price": float(lv.get("price", 0)),
                    "qty": int(lv.get("quantity", 0)),
                    "orders": int(lv.get("orders", 0)),
                }
                for lv in levels[:5]
            ]

        parsed_buys = _parse_levels(buys)
        parsed_sells = _parse_levels(sells)
        total_buy = sum(lv["qty"] for lv in parsed_buys)
        total_sell = sum(lv["qty"] for lv in parsed_sells)

        return {
            "buys": parsed_buys,
            "sells": parsed_sells,
            "ltp": float(q.get("ltp", 0)),
            "total_buy_qty": total_buy,
            "total_sell_qty": total_sell,
            "buy_sell_ratio": (total_buy / total_sell if total_sell > 0 else None),
        }

    except Exception as e:
        _log.warning("angel_fetcher.get_market_depth(%s) failed: %s", symbol, e)
        return None


def get_holdings() -> Optional[List[Dict]]:
    session = _get_session()
    if session is None:
        return None

    try:
        resp = _http.get(
            f"{_BASE}/rest/secure/angelbroking/portfolio/v1/getHolding",
            headers=_auth_headers(session["jwt"], session["api_key"]),
            timeout=15,
        )
        parsed = _safe_parse_json_response(resp)
        raw = (parsed.get("data") if isinstance(parsed, dict) else None) or []
        if not raw:
            return []

        holdings = []
        for h in raw:
            qty = int(h.get("quantity", 0) or 0)
            if qty <= 0:
                continue
            avg = float(h.get("averageprice", 0) or 0)
            ltp = float(h.get("ltp", avg) or avg)
            pnl = (ltp - avg) * qty
            pnl_p = ((ltp / avg) - 1) * 100 if avg > 0 else 0.0
            holdings.append({
                "symbol": h.get("tradingsymbol", "").replace("-EQ", ""),
                "exchange": h.get("exchange", "NSE"),
                "isin": h.get("isin", ""),
                "qty": qty,
                "t1_qty": int(h.get("t1qty", 0) or 0),
                "avg_price": round(avg, 2),
                "ltp": round(ltp, 2),
                "pnl": round(pnl, 2),
                "pnl_pct": round(pnl_p, 2),
                "value_rs": round(ltp * qty, 2),
            })

        return sorted(holdings, key=lambda x: x["value_rs"], reverse=True)

    except Exception as e:
        _log.warning("angel_fetcher.get_holdings failed: %s", e)
        return None


def get_positions() -> Optional[Dict]:
    session = _get_session()
    if session is None:
        return None

    try:
        resp = _http.get(
            f"{_BASE}/rest/secure/angelbroking/order/v1/getPosition",
            headers=_auth_headers(session["jwt"], session["api_key"]),
            timeout=12,
        )
        parsed = _safe_parse_json_response(resp)
        raw = (parsed.get("data") if isinstance(parsed, dict) else None) or []
        # SmartAPI getPosition returns a flat list of rows (equity and F&O
        # alike). Older code assumed a {"day": [...], "net": [...]} dict and
        # crashed on .get() of a list, so the page showed no positions at all.
        if isinstance(raw, dict):
            raw = (raw.get("net") or []) + (raw.get("day") or [])
        if not raw:
            return {"day": [], "net": [], "closed": []}

        def _f(p: Dict, *keys: str) -> float:
            for k in keys:
                try:
                    v = p.get(k)
                    if v not in (None, ""):
                        return float(v)
                except (TypeError, ValueError):
                    continue
            return 0.0

        open_rows: List[Dict] = []
        closed_rows: List[Dict] = []
        seen = set()
        for p in raw:
            key = (p.get("tradingsymbol"), p.get("exchange"), p.get("producttype"))
            if key in seen:
                continue
            seen.add(key)
            qty = int(_f(p, "netqty"))
            avg = _f(p, "netprice", "avgnetprice", "netavgprice")
            ltp = _f(p, "ltp") or avg
            exch = str(p.get("exchange") or "")
            pnl = _f(p, "pnl") if p.get("pnl") not in (None, "") else (ltp - avg) * qty
            row = {
                "symbol": str(p.get("tradingsymbol") or "").replace("-EQ", ""),
                "exchange": exch,
                "segment": "F&O" if exch in ("NFO", "BFO", "MCX", "CDS") else "Equity",
                "instrument": str(p.get("instrumenttype") or ""),
                "expiry": str(p.get("expirydate") or ""),
                "strike": _f(p, "strikeprice") or None,
                "option_type": str(p.get("optiontype") or ""),
                "lot_size": int(_f(p, "lotsize")) or None,
                "qty": qty,
                "avg_price": round(avg, 2),
                "ltp": round(ltp, 2),
                "pnl": round(pnl, 2),
                "realised": round(_f(p, "realised"), 2),
                "unrealised": round(_f(p, "unrealised"), 2),
                "product": str(p.get("producttype") or ""),
                "side": "LONG" if qty > 0 else ("SHORT" if qty < 0 else "CLOSED"),
            }
            (open_rows if qty != 0 else closed_rows).append(row)

        # "day" kept for callers (alerts, posture) — Angel's position book is
        # already today's book, so open rows serve both.
        return {"day": open_rows, "net": open_rows, "closed": closed_rows}

    except Exception as e:
        _log.warning("angel_fetcher.get_positions failed: %s", e)
        return None


def get_funds() -> Optional[Dict]:
    session = _get_session()
    if session is None:
        return None

    try:
        resp = _http.get(
            f"{_BASE}/rest/secure/angelbroking/user/v1/getRMS",
            headers=_auth_headers(session["jwt"], session["api_key"]),
            timeout=10,
        )
        parsed = _safe_parse_json_response(resp)
        d = (parsed.get("data") if isinstance(parsed, dict) else None) or {}
        return {
            "available_cash": float(d.get("availablecash", 0) or 0),
            "used_margin": float(d.get("utiliseddebits", 0) or 0),
            "total_margin": float(d.get("net", 0) or 0),
            "collateral": float(d.get("collateral", 0) or 0),
            "m2m": float(d.get("m2munrealisedprofit", 0) or 0),
        }

    except Exception as e:
        _log.warning("angel_fetcher.get_funds failed: %s", e)
        return None


def get_order_book() -> Optional[List[Dict]]:
    session = _get_session()
    if session is None:
        return None

    try:
        resp = _http.get(
            f"{_BASE}/rest/secure/angelbroking/order/v1/list",
            headers=_auth_headers(session["jwt"], session["api_key"]),
            timeout=12,
        )
        parsed = _safe_parse_json_response(resp)
        data = (parsed.get("data") if isinstance(parsed, dict) else None) or []
        return [
            {
                "order_id": o.get("orderid", ""),
                "symbol": o.get("tradingsymbol", "").replace("-EQ", ""),
                "side": o.get("transactiontype", ""),
                "qty": int(o.get("quantity", 0) or 0),
                "filled_qty": int(o.get("filledshares", 0) or 0),
                "price": float(o.get("price", 0) or 0),
                "avg_price": float(o.get("averageprice", 0) or 0),
                "order_type": o.get("ordertype", ""),
                "status": o.get("status", ""),
                "product": o.get("producttype", ""),
                "time": o.get("updatetime", ""),
                "variety": o.get("variety", "NORMAL"),
            }
            for o in (data or [])
        ]

    except Exception as e:
        _log.warning("angel_fetcher.get_order_book failed: %s", e)
        return None


def get_trade_book() -> Optional[List[Dict]]:
    session = _get_session()
    if session is None:
        return None

    try:
        resp = _http.get(
            f"{_BASE}/rest/secure/angelbroking/order/v1/getTradeBook",
            headers=_auth_headers(session["jwt"], session["api_key"]),
            timeout=12,
        )
        parsed = _safe_parse_json_response(resp)
        data = (parsed.get("data") if isinstance(parsed, dict) else None) or []
        return [
            {
                "order_id": t.get("orderid", ""),
                "trade_id": t.get("tradeid", ""),
                "symbol": t.get("tradingsymbol", "").replace("-EQ", ""),
                "side": t.get("transactiontype", ""),
                "qty": int(t.get("fillsize", 0) or 0),
                "price": float(t.get("fillprice", 0) or 0),
                "product": t.get("producttype", ""),
                "exchange": t.get("exchange", ""),
                "time": t.get("filltime", ""),
            }
            for t in (data or [])
        ]

    except Exception as e:
        _log.warning("angel_fetcher.get_trade_book failed: %s", e)
        return None


# FIX AO-SL (replica-test S2, 2026-10-04): the Quick-order form's "SL" / "SL-M"
# went out verbatim as `ordertype` with variety NORMAL and the trigger in
# `stoploss` — a ROBO/bracket field — so every stop-loss order was malformed.
# SmartAPI stop-loss orders use variety STOPLOSS, ordertype STOPLOSS_LIMIT /
# STOPLOSS_MARKET and the trigger in `triggerprice`.
_ORDER_TYPE_ALIASES = {
    "SL": "STOPLOSS_LIMIT", "SL-L": "STOPLOSS_LIMIT", "STOPLOSS_LIMIT": "STOPLOSS_LIMIT",
    "SL-M": "STOPLOSS_MARKET", "SLM": "STOPLOSS_MARKET", "STOPLOSS_MARKET": "STOPLOSS_MARKET",
    "MARKET": "MARKET", "LIMIT": "LIMIT",
}


def _order_fields(order_type: str, side: str, price: float, trigger_price: float,
                  variety: str = "NORMAL") -> Dict[str, str]:
    """SmartAPI variety / ordertype / price / triggerprice / stoploss for an order.

    Raises ValueError (message shown to the user) for combinations the
    exchange would reject, so nothing malformed is ever sent.
    """
    otype = _ORDER_TYPE_ALIASES.get(str(order_type).strip().upper())
    if otype is None:
        raise ValueError(f"Unknown order type {order_type!r}")
    price = float(price or 0.0)
    trigger = float(trigger_price or 0.0)
    side = str(side or "").upper()

    if otype in ("STOPLOSS_LIMIT", "STOPLOSS_MARKET"):
        if trigger <= 0:
            raise ValueError("Stop-loss orders need a trigger price above 0.")
        if otype == "STOPLOSS_LIMIT":
            if price <= 0:
                raise ValueError("Stop-loss limit orders need a limit price above 0.")
            if side == "SELL" and trigger < price:
                raise ValueError("SELL stop-loss: the trigger must be at or above the limit price.")
            if side == "BUY" and trigger > price:
                raise ValueError("BUY stop-loss: the trigger must be at or below the limit price.")
        else:
            price = 0.0
        variety = "STOPLOSS"
    else:
        if otype == "LIMIT" and price <= 0:
            raise ValueError("Limit orders need a limit price above 0.")
        if otype == "MARKET":
            price = 0.0
        trigger = 0.0
        if variety == "STOPLOSS":
            variety = "NORMAL"

    return {
        "variety": variety,
        "ordertype": otype,
        "price": str(price) if price else "0",
        "triggerprice": str(trigger) if trigger else "0",
        "stoploss": "0",      # ROBO (bracket) orders only
    }


def place_order(
    symbol: str,
    qty: int,
    side: str,
    order_type: str = "MARKET",
    price: float = 0.0,
    trigger_price: float = 0.0,
    product: str = "INTRADAY",
    exchange: str = "NSE",
    validity: str = "DAY",
    variety: str = "NORMAL",
) -> Optional[Dict]:
    try:
        fields = _order_fields(order_type, side, price, trigger_price, variety)
    except ValueError as ve:
        return {"status": "invalid", "message": str(ve)}

    session = _get_session()
    if session is None:
        return None

    sym = symbol.replace(".NS", "").replace(".BO", "").upper()
    token = _get_token(sym, session)
    if token is None:
        return None

    try:
        resp = _http.post(
            f"{_BASE}/rest/secure/angelbroking/order/v1/placeOrder",
            json={
                "tradingsymbol": sym + "-EQ",
                "symboltoken": token,
                "transactiontype": side.upper(),
                "exchange": exchange,
                "producttype": product,
                "duration": validity,
                "squareoff": "0",
                "quantity": str(qty),
                **fields,
            },
            headers=_auth_headers(session["jwt"], session["api_key"]),
            timeout=15,
        )
        parsed = _safe_parse_json_response(resp)
        if parsed and parsed.get("status"):
            return {"order_id": parsed.get("data", {}).get("orderid", ""), "status": "placed"}
        return {"status": "failed", "message": (parsed or {}).get("message", "Unknown error")}
    except Exception as e:
        _log.warning("angel_fetcher.place_order(%s) failed: %s", sym, e)
        return {"status": "error", "message": str(e)}


def modify_order(
    order_id: str,
    symbol: str,
    qty: int,
    order_type: str,
    price: float = 0.0,
    trigger_price: float = 0.0,
    product: str = "DELIVERY",
    exchange: str = "NSE",
    validity: str = "DAY",
    variety: str = "NORMAL",
    side: str = "",
) -> Optional[Dict]:
    try:
        fields = _order_fields(order_type, side, price, trigger_price, variety)
    except ValueError as ve:
        return {"status": "invalid", "message": str(ve)}

    session = _get_session()
    if session is None:
        return None

    sym = symbol.replace(".NS", "").replace(".BO", "").upper()
    token = _get_token(sym, session)
    if token is None:
        return None

    try:
        resp = _http.post(
            f"{_BASE}/rest/secure/angelbroking/order/v1/modifyOrder",
            json={
                "orderid": order_id,
                "producttype": product,
                "duration": validity,
                "quantity": str(qty),
                "tradingsymbol": sym + "-EQ",
                "symboltoken": token,
                "exchange": exchange,
                "squareoff": "0",
                **fields,
            },
            headers=_auth_headers(session["jwt"], session["api_key"]),
            timeout=12,
        )
        parsed = _safe_parse_json_response(resp)
        if parsed and parsed.get("status"):
            return {"order_id": parsed.get("data", {}).get("orderid", order_id), "status": "modified"}
        return {"status": "failed", "message": (parsed or {}).get("message", "")}
    except Exception as e:
        _log.warning("angel_fetcher.modify_order(%s) failed: %s", order_id, e)
        return {"status": "error", "message": str(e)}


def cancel_order(order_id: str, variety: str = "NORMAL") -> bool:
    session = _get_session()
    if session is None:
        return False

    try:
        resp = _http.post(
            f"{_BASE}/rest/secure/angelbroking/order/v1/cancelOrder",
            json={"variety": variety, "orderid": order_id},
            headers=_auth_headers(session["jwt"], session["api_key"]),
            timeout=10,
        )
        parsed = _safe_parse_json_response(resp)
        return bool(parsed and parsed.get("status"))
    except Exception as e:
        _log.warning("angel_fetcher.cancel_order(%s) failed: %s", order_id, e)
        return False


def place_gtt(
    symbol: str,
    qty: int,
    trigger_price: float,
    limit_price: float,
    current_price: float,
    side: str = "BUY",
    exchange: str = "NSE",
    product_type: str = "DELIVERY",
) -> Optional[Dict]:
    session = _get_session()
    if session is None:
        return None

    sym = symbol.replace(".NS", "").replace(".BO", "").upper()
    token = _get_token(sym, session)
    if token is None:
        return None

    try:
        resp = _http.post(
            f"{_BASE}/rest/secure/angelbroking/gtt/v1/createRule",
            json={
                "tradingsymbol": sym + "-EQ",
                "symboltoken": token,
                "exchange": exchange,
                "producttype": product_type,
                "transactiontype": side.upper(),
                "price": str(limit_price),
                "qty": str(qty),
                "triggerprice": str(trigger_price),
                "disclosedqty": "0",
                "timeperiod": "365",
            },
            headers=_auth_headers(session["jwt"], session["api_key"]),
            timeout=12,
        )
        parsed = _safe_parse_json_response(resp)
        if parsed and parsed.get("status"):
            return {"rule_id": str(parsed.get("data", {}).get("id", "")), "status": "placed"}
        return {"status": "failed", "message": (parsed or {}).get("message", "")}
    except Exception as e:
        _log.warning("angel_fetcher.place_gtt(%s) failed: %s", sym, e)
        return {"status": "error", "message": str(e)}


def get_gtt_list() -> Optional[List[Dict]]:
    session = _get_session()
    if session is None:
        return None

    try:
        resp = _http.get(
            f"{_BASE}/rest/secure/angelbroking/gtt/v1/ruleList",
            headers=_auth_headers(session["jwt"], session["api_key"]),
            timeout=10,
        )
        parsed = _safe_parse_json_response(resp)
        data = (parsed.get("data") if isinstance(parsed, dict) else None) or []
        return [
            {
                "rule_id": str(g.get("id", "")),
                "symbol": g.get("tradingsymbol", "").replace("-EQ", ""),
                "side": g.get("transactiontype", ""),
                "qty": int(g.get("qty", 0) or 0),
                "trigger": float(g.get("triggerprice", 0) or 0),
                "limit_price": float(g.get("price", 0) or 0),
                "status": g.get("status", ""),
            }
            for g in (data or [])
        ]
    except Exception as e:
        _log.warning("angel_fetcher.get_gtt_list failed: %s", e)
        return None


def cancel_gtt(rule_id: str, symbol: str, exchange: str = "NSE") -> bool:
    session = _get_session()
    if session is None:
        return False

    sym = symbol.replace(".NS", "").replace(".BO", "").upper()
    token = _get_token(sym, session) or ""

    try:
        resp = _http.post(
            f"{_BASE}/rest/secure/angelbroking/gtt/v1/cancelRule",
            json={
                "id": str(rule_id),
                "tradingsymbol": sym + "-EQ",
                "symboltoken": token,
                "exchange": exchange,
            },
            headers=_auth_headers(session["jwt"], session["api_key"]),
            timeout=10,
        )
        parsed = _safe_parse_json_response(resp)
        return bool(parsed and parsed.get("status"))
    except Exception as e:
        _log.warning("angel_fetcher.cancel_gtt(%s) failed: %s", rule_id, e)
        return False


def get_profile() -> Optional[Dict]:
    session = _get_session()
    if session is None:
        return None

    try:
        resp = _http.get(
            f"{_BASE}/rest/secure/angelbroking/user/v1/getProfile",
            headers=_auth_headers(session["jwt"], session["api_key"]),
            timeout=10,
        )
        parsed = _safe_parse_json_response(resp)
        d = (parsed.get("data") if isinstance(parsed, dict) else None) or {}
        return {
            "name": d.get("name", ""),
            "client_id": d.get("clientcode", ""),
            "email": d.get("email", ""),
            "mobile": d.get("mobileno", ""),
            "exchanges": d.get("exchanges", []),
            "products": d.get("products", []),
            "broker": d.get("broker", "Angel One"),
        }
    except Exception as e:
        _log.warning("angel_fetcher.get_profile failed: %s", e)
        return None
