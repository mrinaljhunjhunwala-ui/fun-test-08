"""
data/fetcher.py
Fetches OHLCV data for NSE/BSE stocks — no yfinance library anywhere.

Source priority (fastest / most reliable first):
  Tier 0 : Angel One SmartAPI   — real-time, no rate limits, official exchange data.
            Only active when [angel_one] credentials are set in Streamlit secrets.
  Tier 1 : Stooq CSV            — free, no API key, cloud-safe (daily bars only).
  Tier 2 : Yahoo Finance v8 API — direct urllib + cookie+crumb auth (since mid-2024).

In-memory cache: each (ticker, period, interval) fetched once per TTL window (5 min).
  - Caches (dataframe, timestamp) tuples to enable TTL expiry across Streamlit sessions.
  - Batch operations use 16 workers with per-ticker timeout (6s) + 1 retry on timeout/connection errors.

Stooq circuit breaker (FIX SPEED2): after 5 consecutive Stooq failures in this
process, Stooq is skipped entirely for 300s and every ticker goes straight to
Yahoo — see _stooq_breaker_* below. Without this, a fully-degraded Stooq (geo-
block/rate-limit/maintenance) made every single ticker in a full-universe scan
pay a ~4s Stooq timeout tax before falling through, which is what blew the
Warm Top Picks GitHub Actions job's time budget in production.
"""

import http.cookiejar
import io
import logging
import os
import threading
import time
import datetime
import urllib.parse
import urllib.request
import pandas as pd
from typing import List, Optional, Tuple

# Structured logging for the data-source fallback chain (Angel → Stooq → Yahoo).
# Failures at each tier are logged at WARNING with provider + exception type + symbol;
# the tier that ultimately serves the data is logged so a degraded path is diagnosable.
_log = logging.getLogger("data.fetcher")

# ── In-process cache: {(ticker, period, interval): (dataframe, timestamp)} ──────
# Entries expire after _FETCH_CACHE_TTL seconds (5 min, matching utils/vix.py pattern)
#
# FIX CACHE1 — two problems with the previous bare dict:
#
#   (a) Not thread-safe. fetch_data() drives this from a 16-worker
#       ThreadPoolExecutor, and the expiry path was a check-then-act:
#           if cache_key in _FETCH_CACHE:      # thread A and B both True
#               ... if expired: del _FETCH_CACHE[cache_key]
#       Two workers asking for the same (ticker, period, interval) can both
#       see the stale entry and both reach the `del`; the loser raises
#       KeyError, which escapes fetch_single() and is caught upstream in
#       _fetch_with_retry()'s generic `except Exception` as a "data error (no
#       retry)" — so the ticker is silently dropped from the batch result. The
#       Stooq breaker (below) already takes a lock for exactly this reason.
#
#   (b) Unbounded. Nothing ever evicted an entry except the re-fetch path, so
#       a full-universe scan (~1,400 tickers) held ~1,400 DataFrames of ~500
#       daily bars each, and each additional period/interval the dashboard
#       asks for adds another full set on top. Expired entries for tickers
#       never requested again were never reclaimed at all. On Streamlit Cloud's
#       ~1 GB container that grows until the app is evicted.
#
# Both are handled by _FETCH_CACHE_LOCK plus a capacity bound: on insert,
# already-expired entries are dropped first, and if that isn't enough the
# oldest entries are evicted until the cache is under _FETCH_CACHE_MAX.
_FETCH_CACHE: dict = {}
_FETCH_CACHE_TTL = 300  # 5 minutes — consistent with VIX cache TTL
_FETCH_CACHE_MAX = 2000  # entries; comfortably covers one full-universe scan
_FETCH_CACHE_LOCK = threading.Lock()

# FIX DIAG-FETCHER (Task 2.3 follow-up C): expose last-diagnostic per provider
# tier so dashboard/shared/data_health.py's Command Centre panel can surface
# actual Yahoo / Stooq / Angel liveness without launching probes. Written on
# every fetch_single call - success or failure per tier.
_LAST_DIAG_BY_PROVIDER: dict = {"AngelOne": {}, "Stooq": {}, "Yahoo": {}, "NSE-bhavcopy": {}}


def get_last_diagnostic(provider: str) -> dict:
    """Return the diagnostic recorded on the most recent fetch attempt for
    the given provider tier ('AngelOne' / 'Stooq' / 'Yahoo').

    Shape: {"ok": bool, "at": iso, "reason": str, "symbol": str, "warnings": int}.
    Empty dict if that provider has not been exercised this process.
    """
    return dict(_LAST_DIAG_BY_PROVIDER.get(provider, {}))


def _record_provider_diagnostic(provider: str, *, ok: bool, symbol: str,
                                reason: str = "") -> None:
    from datetime import datetime as _dt
    prior = _LAST_DIAG_BY_PROVIDER.get(provider) or {}
    _LAST_DIAG_BY_PROVIDER[provider] = {
        "ok": ok,
        "at": _dt.now().isoformat(),
        "reason": reason,
        "symbol": symbol,
        "warnings": int(prior.get("warnings", 0)) + (0 if ok else 1),
    }


def _cache_get(cache_key, now: float):
    """Return a cached DataFrame for `cache_key`, or None if absent/expired."""
    with _FETCH_CACHE_LOCK:
        entry = _FETCH_CACHE.get(cache_key)
        if entry is None:
            return None
        cached_df, cached_ts = entry
        if now - cached_ts < _FETCH_CACHE_TTL:
            _log.debug("cache hit: symbol=%s (age=%.0fs)", cache_key[0], now - cached_ts)
            return cached_df
        # Expired — pop() rather than `del` so a concurrent expiry of the same
        # key can't raise KeyError on the loser.
        _FETCH_CACHE.pop(cache_key, None)
        _log.debug("cache expired: symbol=%s (age=%.0fs, ttl=%ds)",
                   cache_key[0], now - cached_ts, _FETCH_CACHE_TTL)
        return None


def peek_cached(ticker: str, period: str = "2y", interval: str = "1d") -> Optional[pd.DataFrame]:
    """The frame fetch_single() cached for these arguments, or None. Never
    fetches: for callers that only want data a scan already paid for."""
    cached = _cache_get((ticker, period, interval), time.time())
    return None if cached is None else cached.copy()


def _cache_put(cache_key, df, now: float) -> None:
    """Store `df` under `cache_key`, evicting expired then oldest entries."""
    with _FETCH_CACHE_LOCK:
        _FETCH_CACHE[cache_key] = (df, now)
        if len(_FETCH_CACHE) <= _FETCH_CACHE_MAX:
            return
        for k in [k for k, (_, ts) in _FETCH_CACHE.items()
                  if now - ts >= _FETCH_CACHE_TTL]:
            _FETCH_CACHE.pop(k, None)
        if len(_FETCH_CACHE) > _FETCH_CACHE_MAX:
            # Still over budget with everything live — drop the oldest first.
            for k, _ in sorted(_FETCH_CACHE.items(), key=lambda kv: kv[1][1])[
                    : len(_FETCH_CACHE) - _FETCH_CACHE_MAX]:
                _FETCH_CACHE.pop(k, None)


def clear_fetch_cache() -> None:
    """Drop every cached price frame — test helper / manual refresh hook."""
    with _FETCH_CACHE_LOCK:
        _FETCH_CACHE.clear()


# ── Yahoo Finance session cache (cookie jar + crumb token) ───────────────────
_YF_SESSION: dict = {"opener": None, "crumb": "", "ts": 0.0}


# ── Stooq circuit breaker ─────────────────────────────────────────────────────
# FIX SPEED2 — fetch_single() previously tried Stooq for EVERY ticker no matter
# how many times it had already failed in this same process. When Stooq
# degrades for a whole run (geo-block, rate-limit, maintenance — all observed
# in production: fast "returned HTML" rejects escalating to full urlopen
# timeouts partway through a run), every remaining ticker still pays its ~4s
# Stooq timeout before falling through to Yahoo. On a ~1,400+ ticker universe
# scan that's what blew the Warm Top Picks GitHub Actions job's 8-minute
# budget (see .github/workflows/warm-top-picks.yml). Same discipline as the
# existing Angel One login circuit breaker (_LOGIN_BREAKER_* in
# angel_fetcher.py) — after enough consecutive failures, stop paying the tax
# and go straight to Yahoo for the rest of this cooldown window.
_STOOQ_BREAKER_THRESHOLD = 5      # consecutive failures before tripping
_STOOQ_BREAKER_COOLDOWN  = 300    # seconds — matches _FETCH_CACHE_TTL
_STOOQ_BREAKER: dict = {"consecutive_failures": 0, "tripped_until": 0.0}
_STOOQ_BREAKER_LOCK = threading.Lock()  # batch fetches hit this from 16 workers


def _stooq_enabled() -> bool:
    """FIX STOOQ-OFF (2026-09-27): Stooq is OFF unless NSE_ENABLE_STOOQ is set.

    Since 2026-09-06 stooq.com answers every CSV request with a JavaScript
    browser-verification page, so the tier can never succeed from a script
    and only cost a 0.6-4 s round-trip (plus 5 breaker-tripping failures
    every 5 min per process) before falling through to Yahoo. Read per call
    so tests and operators can flip it without a restart.
    """
    return os.environ.get("NSE_ENABLE_STOOQ", "").strip().lower() in {"1", "true", "yes", "on"}


def _stooq_breaker_is_tripped() -> bool:
    return time.time() < _STOOQ_BREAKER["tripped_until"]


def _stooq_breaker_record(success: bool) -> None:
    with _STOOQ_BREAKER_LOCK:
        if success:
            _STOOQ_BREAKER["consecutive_failures"] = 0
            _STOOQ_BREAKER["tripped_until"] = 0.0
            return
        _STOOQ_BREAKER["consecutive_failures"] += 1
        if _STOOQ_BREAKER["consecutive_failures"] >= _STOOQ_BREAKER_THRESHOLD:
            _STOOQ_BREAKER["tripped_until"] = time.time() + _STOOQ_BREAKER_COOLDOWN
            _log.warning(
                "Stooq circuit breaker TRIPPED after %d consecutive failures — "
                "skipping Stooq for %ds, going straight to Yahoo",
                _STOOQ_BREAKER["consecutive_failures"], _STOOQ_BREAKER_COOLDOWN)


def _reset_stooq_breaker() -> None:
    """Test-only helper — restores the breaker to a fresh, untripped state."""
    with _STOOQ_BREAKER_LOCK:
        _STOOQ_BREAKER["consecutive_failures"] = 0
        _STOOQ_BREAKER["tripped_until"] = 0.0


def _get_yf_crumb():
    """
    Obtain a Yahoo Finance cookie-aware opener and a crumb token.

    Yahoo's v8 chart endpoint started requiring authentication in mid-2024:
      1. A session cookie is issued by  https://fc.yahoo.com/
      2. A crumb token is retrieved from /v1/test/getcrumb
      3. The crumb is appended as ?crumb=<token> to every chart API call.

    The session is cached for 30 minutes; a fresh crumb is fetched on expiry.
    Returns: (opener: urllib.request.OpenerDirector, crumb: str)
    """
    global _YF_SESSION
    now = time.time()
    if _YF_SESSION["opener"] is not None and now - _YF_SESSION["ts"] < 1800:
        return _YF_SESSION["opener"], _YF_SESSION["crumb"]

    cj     = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    _hdrs  = [
        ("User-Agent",      "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"),
        ("Accept",          "application/json, */*"),
        ("Accept-Language", "en-US,en;q=0.9"),
    ]
    opener.addheaders = _hdrs

    # Step 1 — consent gate (sets B / GUC cookies required for crumb)
    for _gate in ("https://fc.yahoo.com/", "https://finance.yahoo.com/"):
        try:
            opener.open(urllib.request.Request(
                _gate, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            ), timeout=10)
            break
        except Exception as e:
            _log.debug("YF consent gate %s failed: %s", _gate, e)  # try next gate
            continue

    # Step 2 — crumb endpoint (short alpha-numeric token, ≤ 20 chars)
    crumb = ""
    for _cu in (
        "https://query2.finance.yahoo.com/v1/test/getcrumb",
        "https://query1.finance.yahoo.com/v1/test/getcrumb",
    ):
        try:
            with opener.open(urllib.request.Request(
                _cu, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            ), timeout=10) as _r:
                _raw = _r.read().decode("utf-8").strip()
                if _raw and len(_raw) <= 25 and not _raw.startswith("<"):
                    crumb = _raw
                    break
        except Exception as e:
            _log.debug("YF crumb %s failed: %s", _cu, e)  # try next crumb endpoint
            continue

    _YF_SESSION.update({"opener": opener, "crumb": crumb, "ts": now})
    return opener, crumb

# ── Period → calendar days mapping ───────────────────────────────────────────
_PERIOD_DAYS = {
    # UI-style labels (1D / 5D / 1M / 6M / YTD / Max)
    "1d":  10,  "5d":  18,  "1m":  35,  "6m": 185,
    # yfinance-style labels (legacy internal use)
    "1mo": 35,  "2mo": 65,  "3mo": 95,  "6mo": 185,
    "1y":  370, "2y":  740, "3y": 1100, "5y": 1830, "10y": 3660,
    "max": 1830,
}

# ── UI label → internal period key used for fetching ─────────────────────────
UI_PERIOD_MAP = {
    "1D":  "1d",
    "5D":  "5d",
    "1M":  "1m",
    "6M":  "6m",
    "YTD": "ytd",
    "Max": "max",
}


def _period_to_dates(period: str):
    """Convert period string to (start_str, end_str) for Stooq.
    Handles UI labels (1d/5d/1m/6m/ytd/max) and legacy yfinance strings.
    """
    end = datetime.date.today()
    if period.lower() == "ytd":
        start = datetime.date(end.year, 1, 1)
    elif period.lower() == "max":
        start = end - datetime.timedelta(days=1830)
    else:
        days  = _PERIOD_DAYS.get(period.lower(), _PERIOD_DAYS.get(period, 370))
        start = end - datetime.timedelta(days=days)
    return start.strftime("%Y%m%d"), end.strftime("%Y%m%d")


def _fetch_stooq(ticker: str, period: str = "1y") -> pd.DataFrame:
    """
    Fetch daily OHLCV from Stooq — zero rate limits, works from cloud IPs.
    Ticker format: 'RELIANCE.NS' → stooq uses 'reliance.ns' (lowercase).
    Returns flat DataFrame with Open, High, Low, Close, Volume index=Date.
    """
    sym = ticker.lower()
    # Stooq uses ^nsei for Nifty 50 index — map common index names
    _INDEX_MAP = {"^nsei": "^nsei", "^bsesn": "^bsesn", "^nsebank": "^nsebank"}
    if sym in _INDEX_MAP:
        sym = _INDEX_MAP[sym]

    d1, d2 = _period_to_dates(period)
    url = f"https://stooq.com/q/d/l/?s={sym}&d1={d1}&d2={d2}&i=d"

    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    # Short timeout: when Stooq is congested, fail fast to the Yahoo fallback rather
    # than blocking ~12 s per ticker (which makes a full-universe scan take minutes).
    with urllib.request.urlopen(req, timeout=4) as r:
        raw = r.read().decode("utf-8", errors="replace")

    if not raw.strip() or "No data" in raw or len(raw) < 60:
        raise ValueError(f"Stooq returned no data for {ticker}")

    # Stooq sometimes returns an HTML page instead of CSV (maintenance / geo-block)
    if raw.lstrip().startswith("<") or "<!DOCTYPE" in raw[:200] or "<html" in raw[:200].lower():
        # FIX STOOQ-DIAG — previously this only logged "returned HTML", which
        # looks identical whether Stooq sent a rate-limit page, a Cloudflare
        # challenge, or a geo-block notice. Log a snippet of the actual body
        # so the next run's logs show which one it actually is, instead of
        # leaving the cause a guess every time this fires.
        _snippet = " ".join(raw[:300].split())
        _log.warning("Stooq returned HTML (not CSV) for %s — body starts: %r",
                     ticker, _snippet)
        raise ValueError(f"Stooq returned HTML (not CSV) for {ticker}")

    try:
        df = pd.read_csv(io.StringIO(raw))
    except Exception as e:
        # Fallback: python engine is more lenient with malformed CSV rows
        _log.debug("stooq CSV parse failed for %s, retrying with lenient parser: %s", ticker, e)
        df = pd.read_csv(io.StringIO(raw), engine="python", on_bad_lines="skip")
    df.columns = [c.strip().title() for c in df.columns]
    if "Date" not in df.columns:
        raise ValueError(f"Stooq unexpected format for {ticker}: {df.columns.tolist()}")

    df["Date"] = pd.to_datetime(df["Date"])
    df.set_index("Date", inplace=True)
    df.sort_index(inplace=True)
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    df = df[df["Close"] > 0]
    return df


def _parse_yahoo_chart(data: dict, ticker: str, yf_interval: str) -> pd.DataFrame:
    """Yahoo v8 chart JSON -> OHLCV DataFrame (index Date, or IST Datetime intraday)."""
    import datetime

    result = data.get("chart", {}).get("result")
    if not result:
        err = data.get("chart", {}).get("error", {})
        raise ValueError(f"Yahoo chart API error for {ticker}: {err}")

    # Defensive parse — flagged by data-provenance-auditor 2026-09-02. Yahoo can
    # rename / reshape `indicators.quote` without notice; previously any drift
    # here raised a bare KeyError from `r0["indicators"]["quote"][0]` and got
    # swallowed by callers. Now we surface a clear ValueError naming the drift.
    r0         = result[0] if result else None
    if r0 is None:
        raise ValueError(f"Yahoo chart API: empty result list for {ticker}")
    timestamps = r0.get("timestamp", [])
    _indicators = r0.get("indicators") or {}
    _quote_list = _indicators.get("quote") or []
    if not _quote_list:
        raise ValueError(
            f"Yahoo schema drift for {ticker}: missing indicators.quote "
            "(expected a non-empty list). Provider may have renamed the field."
        )
    quote      = _quote_list[0] or {}

    if not timestamps:
        raise ValueError(f"Yahoo chart API returned no timestamps for {ticker}")

    # FIX OHLC-BASIS (audit 2026-10-03): Close used to be `adjclose`
    # (dividend-adjusted) while Open/High/Low stayed raw, so the close sat
    # outside the day's range on most bars (HDFCBANK: 403 of 500), ATR read
    # ~69% high and gaps were invented. Yahoo's raw quote series is already
    # split/bonus-adjusted, so use it for every column: bars are internally
    # consistent and on the same exchange-price basis as Angel One, Stooq,
    # live quotes and intraday bars. (Dividends now show as their real
    # ex-date drop instead of being back-adjusted.)
    close_list = quote.get("close", [])

    # For intraday intervals keep full datetime; for daily use date-only
    _intraday = yf_interval not in ("1d", "1wk", "1mo")
    # datetime.utcfromtimestamp() is deprecated (Python 3.12+) and slated for
    # removal; it also returns a naive datetime that merely *claims* to be UTC.
    # fromtimestamp(..., tz=utc) is the supported spelling. The IST conversion
    # is now a real timezone shift rather than manual arithmetic, then stripped
    # back to naive so the index type the callers already handle is unchanged.
    _UTC = datetime.timezone.utc
    if _intraday:
        _IST  = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
        dates = [
            datetime.datetime.fromtimestamp(ts, tz=_UTC).astimezone(_IST).replace(tzinfo=None)
            for ts in timestamps
        ]
        idx = pd.DatetimeIndex(dates, name="Datetime")
    else:
        dates = [datetime.datetime.fromtimestamp(ts, tz=_UTC).date() for ts in timestamps]
        idx   = pd.DatetimeIndex(dates, name="Date")

    df = pd.DataFrame({
        "Open":   quote.get("open",   [None] * len(dates)),
        "High":   quote.get("high",   [None] * len(dates)),
        "Low":    quote.get("low",    [None] * len(dates)),
        "Close":  close_list,
        "Volume": quote.get("volume", [None] * len(dates)),
    }, index=idx)

    df = df.dropna(subset=["Close"])
    df = df[df["Close"] > 0]
    if not _intraday:
        # Yahoo pads daily history with flat zero-volume bars on exchange
        # holidays (and occasionally a lost session). They are not sessions:
        # keeping them drags Volume_SMA_20 and fakes "no movement" days.
        # Indices always report Volume 0, so the bar must also be flat.
        _flat = (df["Open"] == df["High"]) & (df["High"] == df["Low"]) & (df["Low"] == df["Close"])
        _novol = df["Volume"].fillna(0) == 0
        df = df[~(_flat & _novol)]
    df.sort_index(inplace=True)
    return df


def _fetch_yahoo_direct(ticker: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame:
    """
    Fallback: direct Yahoo Finance v8 chart API — no yfinance library, no rate limiting.
    Uses the same underlying endpoint as the yfinance library but via raw urllib,
    which avoids the HTTP 429 rate-limiting that hits the library from cloud IPs.
    Returns flat DataFrame with Open, High, Low, Close, Volume index=Date.
    """
    import json

    # Map internal period strings → Yahoo Finance API range parameter
    _RANGE_MAP = {
        "1d":  "5d",   "5d":  "5d",   "1m":  "1mo",  "6m":  "6mo",
        "ytd": "ytd",  "max": "max",
        "1mo": "1mo",  "2mo": "3mo",  "3mo": "3mo",  "6mo": "6mo",
        "1y":  "1y",   "2y":  "2y",   "3y":  "5y",   "5y":  "5y",   "10y": "10y",
        # Intraday period keys (used by fetch_intraday).
        # FIX INTRA1 — two of these were wrong. "7d" mapped to Yahoo's "5d",
        # so fetch_intraday(days=6..15) asked for up to 15 days of 15m bars and
        # got 5; and "60d" mapped to "60d", which is not a value Yahoo's chart
        # API accepts at all (its ranges are 1d/5d/1mo/3mo/6mo/1y/2y/5y/10y/
        # ytd/max), so the longest intraday request fell through to the "1y"
        # default and came back as daily-scale coverage. Both now map to the
        # smallest real Yahoo range that actually contains the requested span;
        # callers already trim to their own window.
        "7d":  "1mo",  "15d": "1mo",  "30d": "1mo",   "60d": "3mo",
    }
    yf_range = _RANGE_MAP.get(period.lower(), "1y")
    # Allow intraday intervals — Yahoo supports 5m, 15m, 30m, 60m, 1h
    _VALID_INTERVALS = {"1d", "1wk", "1mo", "5m", "15m", "30m", "60m", "1h"}
    yf_interval = interval if interval in _VALID_INTERVALS else "1d"

    # Use cookie-aware opener + crumb (required by Yahoo Finance since mid-2024)
    _opener, _crumb = _get_yf_crumb()
    _crumb_qs = f"&crumb={urllib.parse.quote(_crumb)}" if _crumb else ""

    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
           f"?interval={yf_interval}&range={yf_range}&includePrePost=false{_crumb_qs}")
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept":     "application/json",
    })
    try:
        with _opener.open(req, timeout=15) as r:
            data = json.loads(r.read())
    except Exception as e:
        # Session may have expired — reset cache and retry once with query2
        _log.debug("yfinance query1 failed for %s, resetting session and retrying query2: %s", ticker, e)
        _YF_SESSION["opener"] = None
        url2 = (f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}"
                f"?interval={yf_interval}&range={yf_range}&includePrePost=false")
        req2 = urllib.request.Request(url2, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Accept":     "application/json",
        })
        with urllib.request.urlopen(req2, timeout=15) as r:
            data = json.loads(r.read())

    return _parse_yahoo_chart(data, ticker, yf_interval)


def _rate_limit():
    """No-op — kept for backward compatibility with any callers."""
    pass

# ── NIFTY 50 constituents (updated 2026) ────────────────────────────────────
# Nifty 50 as of 2026 using Yahoo Finance working symbols:
#   TATAMOTORS → TMPV.NS  (Tata Motors PV, post-demerger Yahoo symbol)
#   ZOMATO     → ETERNAL.NS  (Zomato renamed to Eternal Ltd 2025)
#   UPL / LTIM removed; SHRIRAMFIN added
NIFTY50_TICKERS = [
    "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "ICICIBANK.NS", "BHARTIARTL.NS",
    "INFY.NS", "SBIN.NS", "HINDUNILVR.NS", "ITC.NS", "LT.NS",
    "BAJFINANCE.NS", "HCLTECH.NS", "MARUTI.NS", "SUNPHARMA.NS", "ADANIENT.NS",
    "KOTAKBANK.NS", "TITAN.NS", "ONGC.NS", "NTPC.NS", "AXISBANK.NS",
    "WIPRO.NS", "ULTRACEMCO.NS", "ASIANPAINT.NS", "BAJAJFINSV.NS", "POWERGRID.NS",
    "M&M.NS", "NESTLEIND.NS", "JSWSTEEL.NS", "TMPV.NS", "TATASTEEL.NS",
    "TECHM.NS", "GRASIM.NS", "BPCL.NS", "ADANIPORTS.NS", "CIPLA.NS",
    "BRITANNIA.NS", "EICHERMOT.NS", "DRREDDY.NS", "HINDALCO.NS", "COALINDIA.NS",
    "DIVISLAB.NS", "TATACONSUM.NS", "SBILIFE.NS", "APOLLOHOSP.NS", "HDFCLIFE.NS",
    "INDUSINDBK.NS", "HEROMOTOCO.NS", "BAJAJ-AUTO.NS", "ETERNAL.NS", "SHRIRAMFIN.NS",
]

# Display name overrides for renamed / split tickers
TICKER_DISPLAY_NAMES = {
    "TMPV.NS":    "Tata Motors (PV)",
    "ETERNAL.NS": "Eternal Ltd (Zomato)",
}

# ── BANKNIFTY constituents ───────────────────────────────────────────────────
BANKNIFTY_TICKERS = [
    "HDFCBANK.NS", "ICICIBANK.NS", "SBIN.NS", "KOTAKBANK.NS", "AXISBANK.NS",
    "INDUSINDBK.NS", "BANDHANBNK.NS", "IDFCFIRSTB.NS", "AUBANK.NS", "FEDERALBNK.NS",
    "PNB.NS", "BANKBARODA.NS",
]


def fetch_data(
    tickers: List[str],
    period: str = "2y",
    interval: str = "1d",
    auto_adjust: bool = True,
    dropna: bool = True,
) -> pd.DataFrame:
    """
    Download OHLCV for multiple tickers (backtesting / batch use).
    Uses parallel fetch_single() calls with per-ticker timeout + retry logic.
    No yfinance library; avoids rate-limiting from cloud IPs.
    Returns a MultiIndex DataFrame compatible with yfinance batch output.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    print(f"  Fetching {len(tickers)} ticker(s) | period={period} | interval={interval}")

    results: dict = {}
    
    # Per-ticker timeout (6s) + 1 retry on timeout/connection errors before giving up.
    # If even one retry fails, log and continue (cap drops from results, not a fatal error).
    _TICKER_TIMEOUT = 6
    _BATCH_TIMEOUT = min(120, len(tickers) * 8 + 10)  # Hard cap on total batch time

    def _fetch_with_retry(t: str) -> Tuple[str, Optional[pd.DataFrame]]:
        """Fetch a single ticker with one retry on timeout/connection errors."""
        for attempt in range(2):
            try:
                df = fetch_single(t, period=period, interval=interval)
                if df is not None and not df.empty:
                    return t, df
                else:
                    _log.warning("batch fetch: symbol=%s returned empty dataframe", t)
                    return t, None
            except (TimeoutError, urllib.error.URLError, ConnectionError) as e:
                if attempt == 0:
                    _log.debug("batch fetch: symbol=%s attempt 1 timeout/connection, retrying: %s",
                              t, type(e).__name__)
                    time.sleep(1)  # brief backoff before retry
                    continue
                else:
                    _log.warning("batch fetch: symbol=%s attempt 2 also failed: %s: %s",
                                t, type(e).__name__, e)
                    return t, None
            except Exception as e:
                # Real data errors (no data, bad format, etc.) don't retry — fail immediately
                _log.warning("batch fetch: symbol=%s data error (no retry): %s: %s",
                            t, type(e).__name__, e)
                return t, None

    with ThreadPoolExecutor(max_workers=16) as pool:
        futs = {pool.submit(_fetch_with_retry, t): t for t in tickers}
        try:
            for fut in as_completed(futs, timeout=_BATCH_TIMEOUT):
                try:
                    t, df = fut.result(timeout=_TICKER_TIMEOUT)
                    if df is not None:
                        results[t] = df
                except Exception as e:
                    # Timeout or other executor issue — log and skip this ticker
                    ticker_name = futs.get(fut, "unknown")
                    _log.warning("batch fetch: future result failed for %s: %s: %s",
                                ticker_name, type(e).__name__, e)
        except TimeoutError:
            _log.warning("batch fetch: batch timeout reached (%ds), %d tickers completed",
                        _BATCH_TIMEOUT, len(results))

    if not results:
        raise ValueError(f"No data returned for any of {len(tickers)} tickers")

    # Build MultiIndex DataFrame (Price × Ticker) to match yfinance batch format
    frames = []
    for t, df in results.items():
        for col in ("Open", "High", "Low", "Close", "Volume"):
            frames.append(df[[col]].rename(columns={col: (col, t)}))

    combined = pd.concat(frames, axis=1)
    combined.columns = pd.MultiIndex.from_tuples(combined.columns)
    if dropna:
        combined.dropna(how="all", inplace=True)

    print(f"  OK Fetched {len(combined)} rows × {len(results)}/{len(tickers)} tickers")
    return combined


class _SkipAngel(Exception):
    """Internal: Angel tier skipped (busy), not failed."""


def fetch_single(ticker: str, period: str = "2y", interval: str = "1d") -> pd.DataFrame:
    """
    Fetch OHLCV for one ticker.
    Priority: Angel One (Tier 0) → Stooq (Tier 1) → Yahoo Finance (Tier 2)
    → NSE bhavcopy (Tier 3, daily only, last ~60 sessions, NOT split-adjusted).
    Results are cached in-process with TTL expiry (5 minutes) — repeated calls
    within the TTL window return the cached copy; expired entries trigger a re-fetch.
    """
    cache_key = (ticker, period, interval)
    now = time.time()

    # FIX CACHE1: lock-guarded lookup + expiry (see _cache_get above).
    cached = _cache_get(cache_key, now)
    if cached is not None:
        return cached.copy()

    df = None
    served = None
    failures = []        # [(provider, exception_type)] — for the final error + summary

    def _fail(provider, exc):
        """Record + log a tier failure (provider, exception type, symbol)."""
        failures.append((provider, type(exc).__name__))
        _log.warning("data fallback: provider=%s symbol=%s failed: %s: %s",
                     provider, ticker, type(exc).__name__, exc)

    # ── Tier 0: Angel One SmartAPI (only if credentials configured) ───────────
    try:
        import importlib
        _af = importlib.import_module(".angel_fetcher", __package__)
        _ao_fetch, _ao_ok = _af.fetch_historical, _af.is_configured
        AngelBusy = getattr(_af, "AngelBusy", _SkipAngel)
        if _ao_ok():
            try:
                df = _ao_fetch(ticker, period=period, interval=interval)
            except AngelBusy:
                # Bulk scan: another thread holds Angel's slot (see FIX AO-BULK
                # in angel_fetcher). Not a failure — go straight to the next tier.
                df = None
                _log.debug("AngelOne busy — skipping to next tier for symbol=%s", ticker)
                raise _SkipAngel from None
            if df is not None and not df.empty:
                served = "AngelOne"
                _record_provider_diagnostic("AngelOne", ok=True, symbol=ticker)
            else:
                _log.warning("data fallback: provider=AngelOne symbol=%s returned no data",
                             ticker)
                _record_provider_diagnostic("AngelOne", ok=False, symbol=ticker,
                                            reason="returned no data")
    except _SkipAngel:
        pass
    except Exception as e:
        df = None
        _fail("AngelOne", e)
        _record_provider_diagnostic("AngelOne", ok=False, symbol=ticker,
                                    reason=f"{type(e).__name__}: {e}")

    # ── Tier 1: Stooq CSV (daily bars only — no intraday support) ────────────
    if (df is None or df.empty) and interval == "1d" and _stooq_enabled():
        if _stooq_breaker_is_tripped():
            _log.debug("Stooq circuit breaker open — skipping Stooq for symbol=%s", ticker)
        else:
            try:
                df = _fetch_stooq(ticker, period=period)
                served = "Stooq"
                _stooq_breaker_record(success=True)
                _record_provider_diagnostic("Stooq", ok=True, symbol=ticker)
            except Exception as e:
                df = None
                _stooq_breaker_record(success=False)
                _fail("Stooq", e)
                _record_provider_diagnostic("Stooq", ok=False, symbol=ticker,
                                            reason=f"{type(e).__name__}: {e}")
    elif (df is None or df.empty) and served is None and interval != "1d":
        _log.debug("intraday %s for %s — skipping Stooq (daily-only)", interval, ticker)

    # ── Tier 2: Yahoo Finance v8 chart API (cookie+crumb auth) ───────────────
    if df is None or df.empty:
        try:
            df = _fetch_yahoo_direct(ticker, period=period, interval=interval)
            served = "Yahoo"
            _record_provider_diagnostic("Yahoo", ok=True, symbol=ticker)
        except Exception as e:
            df = None
            _fail("Yahoo", e)
            _record_provider_diagnostic("Yahoo", ok=False, symbol=ticker,
                                        reason=f"{type(e).__name__}: {e}")

    # ── Tier 3: NSE bhavcopy (daily only; last resort; <=60 sessions, unadjusted) ─
    if (df is None or df.empty) and interval == "1d":
        try:
            from data.nse_bhavcopy_prices import fetch_bhavcopy_history
            df = fetch_bhavcopy_history(ticker, period=period)
            served = "NSE-bhavcopy"
            _record_provider_diagnostic("NSE-bhavcopy", ok=True, symbol=ticker)
        except Exception as e:
            df = None
            _fail("NSE-bhavcopy", e)
            _record_provider_diagnostic("NSE-bhavcopy", ok=False, symbol=ticker,
                                        reason=f"{type(e).__name__}: {e}")

    if df is None or df.empty:
        _log.error("data fetch FAILED: symbol=%s — all providers failed: %s",
                   ticker, failures)
        raise ValueError(f"No data for {ticker}. All sources failed: {failures}")

    # Log the provider that ultimately succeeded — INFO when a fallback was needed
    # (a degraded path worth noticing), DEBUG on a clean first-tier hit.
    if failures:
        _log.info("data served: symbol=%s provider=%s rows=%d (after failures: %s)",
                  ticker, served, len(df), [p for p, _ in failures])
    else:
        _log.debug("data served: symbol=%s provider=%s rows=%d", ticker, served, len(df))

    # Store in cache with current timestamp (bounded + lock-guarded — FIX CACHE1)
    _cache_put(cache_key, df, now)
    return df.copy()


def fetch_intraday(
    ticker:   str,
    interval: str = "5m",
    days:     int = 5,
) -> pd.DataFrame:
    """
    Fetch intraday OHLCV bars for a single NSE ticker.

    Yahoo Finance intraday limits (free API):
        5m  bars → last 5  trading days max
        15m bars → last 60 trading days max
        30m bars → last 60 trading days max
        60m bars → last 730 trading days max

    Args:
        ticker   : yfinance symbol (e.g. 'RELIANCE.NS')
        interval : '5m' | '15m' | '30m' | '60m'  (default '5m')
        days     : how many trading days of data to request

    Returns:
        DataFrame with IST datetime index, columns: Open, High, Low, Close, Volume
        Only market hours rows (09:15–15:30 IST) are returned.

    Raises:
        ValueError if no data returned.
    """
    _MAX_DAYS = {"5m": 5, "15m": 60, "30m": 60, "60m": 730, "1h": 730}
    if interval not in _MAX_DAYS:
        raise ValueError(f"interval must be one of {list(_MAX_DAYS)} — got '{interval}'")

    # Cap days to Yahoo's limit for the chosen interval
    cap = _MAX_DAYS[interval]
    days = min(days, cap)

    # Map days → period string understood by _fetch_yahoo_direct / _RANGE_MAP
    if days <= 5:
        period = "5d"
    elif days <= 15:
        period = "7d"
    elif days <= 30:
        period = "15d"
    else:
        period = "60d"

    df = fetch_single(ticker, period=period, interval=interval)

    # Filter to NSE market hours 09:15 – 15:30 IST
    try:
        times = df.index.time
        import datetime as _dt
        mkt_open  = _dt.time(9, 15)
        mkt_close = _dt.time(15, 30)
        df = df[(times >= mkt_open) & (times <= mkt_close)]
    except Exception as e:
        _log.debug("intraday market-hours filter skipped: %s", e)  # index may be date-only

    return df


def fetch_index(index: str = "^NSEI", period: str = "2y") -> pd.DataFrame:
    """
    Fetch index data.
    Common Indian indices:
        ^NSEI   = NIFTY 50
        ^BSESN  = SENSEX
        ^NSEBANK = BANKNIFTY
    """
    return fetch_single(index, period=period)


def get_sector_tickers(sector: str) -> List[str]:
    """Return a predefined list of NSE tickers for a given sector."""
    sectors = {
        "it":      ["TCS.NS", "INFY.NS", "WIPRO.NS", "HCLTECH.NS", "TECHM.NS", "LTM.NS"],
        "banking": BANKNIFTY_TICKERS,
        "pharma":  ["SUNPHARMA.NS", "DRREDDY.NS", "CIPLA.NS", "DIVISLAB.NS", "APOLLOHOSP.NS"],
        "auto":    ["MARUTI.NS", "TMCV.NS", "M&M.NS", "BAJAJ-AUTO.NS", "EICHERMOT.NS", "HEROMOTOCO.NS"],
        "fmcg":    ["HINDUNILVR.NS", "ITC.NS", "NESTLEIND.NS", "BRITANNIA.NS", "TATACONSUM.NS"],
        "energy":  ["RELIANCE.NS", "ONGC.NS", "BPCL.NS", "NTPC.NS", "POWERGRID.NS"],
        "metal":   ["TATASTEEL.NS", "JSWSTEEL.NS", "HINDALCO.NS"],
    }
    key = sector.lower()
    if key not in sectors:
        raise ValueError(f"Unknown sector '{sector}'. Available: {list(sectors.keys())}")
    return sectors[key]
