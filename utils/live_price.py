"""
utils/live_price.py — Real-time NSE equity prices.

Tier hierarchy (fastest → most reliable fallback):
  Tier 0: Angel One SmartAPI   — true real-time LTP, no delay, official exchange feed.
           get_live_prices_batch() uses get_batch_quotes() (up to 50 symbols per HTTP
           call) so the entire portfolio/scanner resolves in 1–2 network round-trips
           instead of N parallel single-symbol calls that trip the rate limiter.
  Tier 1: Yahoo Finance JSON API — direct HTTP, no library, works from cloud IPs.
           Returns regularMarketPrice which is live during market hours but can be
           15-min delayed outside them or when Yahoo throttles cloud IPs.
  Tier 2: NSE India official API — real-time but needs session cookies; may return
           403 from datacenter IPs (Streamlit Cloud included).
  Tier 3: Stooq EOD             — yesterday's close, never fails, last resort.

Root cause of previous price variance
──────────────────────────────────────
The old get_live_prices_batch() called get_live_quote() once per symbol inside a
ThreadPoolExecutor. Each call hit _get_token() (rate-limited at 1 req/s) + a quote
endpoint call with NO rate limit. With 8 workers and N stocks, Angel One received a
burst of N requests in a few seconds, responded with empty/error bodies, and every
symbol silently fell through to Yahoo's 15-min delayed regularMarketPrice — even
though Angel One credentials were perfectly valid.

Fix: batch path calls angel_fetcher.get_batch_quotes() which sends all symbols in
a single "mode=FULL, exchangeTokens={NSE: [tok1,tok2,...]}" POST (max 50 per call).
Token lookup is the only sequential step (still rate-limited); the quote fetch itself
is one round-trip per 50 symbols. Symbols not resolved by Angel One fall back to the
per-symbol Yahoo → NSE → Stooq chain.

FIX LP1 — get_live_prices_batch()'s fallback ThreadPoolExecutor used a flat
20-second `wait(timeout=20)` regardless of how many symbols needed the
fallback chain. Each fallback call can take up to ~24s in the worst case
(Yahoo's 8s timeout + NSE's 6s + Stooq's 10s, tried sequentially when all
three fail). With max_workers=8, any remaining list bigger than 8 needs
multiple sequential "rounds" through the pool — e.g. 20 remaining symbols
needs ceil(20/8)=3 rounds, up to ~72s worst case — but the old code only
ever waited 20s total, then silently abandoned everything still running:
those symbols' results were never collected and just stayed None. This is
exactly the scenario this fallback path exists for (Angel One Tier-0 not
covering a chunk of symbols — circuit breaker tripped, new listings, BSE-
only names), so it tended to fail precisely when it mattered most. Fixed by
scaling the wait to the actual workload (bounded so one bad batch can't hang
the page indefinitely) and logging — not silently dropping — anything still
outstanding when the wait ends.
"""

from __future__ import annotations

import json
import logging
import math
import time
import urllib.parse
import urllib.request
from typing import Dict, List, Optional

_log = logging.getLogger("live_price")


# ─── Yahoo Finance direct quote API ─────────────────────────────────────────

def _vwap_from_bars(result: dict) -> dict:
    """{"vwap": float} from a Yahoo chart result's intraday bars, or {}.

    Typical price (H+L+C)/3 weighted by bar volume; bars with a missing
    field or zero volume are skipped. Empty when no volume traded.
    """
    try:
        q = result["indicators"]["quote"][0]
        num = den = 0.0
        for h, lo, c, v in zip(q.get("high") or [], q.get("low") or [],
                               q.get("close") or [], q.get("volume") or []):
            if None in (h, lo, c, v) or not v:
                continue
            num += (h + lo + c) / 3.0 * v
            den += v
        return {"vwap": num / den} if den > 0 else {}
    except (KeyError, IndexError, TypeError):
        return {}


def _yahoo_json_quote(ticker_ns: str) -> Optional[dict]:
    """
    Fetch live quote from Yahoo Finance JSON endpoint.
    Uses cookie+crumb session (required since mid-2024).
    Returns dict with 'price', 'prev_close', or None on failure.

    Note: regularMarketPrice is the last traded price during market hours.
    Outside hours it reflects the closing price of the last session — this
    is correct behaviour, not a delay. The 15-min delay only appears when
    Yahoo throttles requests from datacenter IPs; Angel One Tier 0 bypasses
    this entirely during market hours.
    """
    try:
        from data.fetcher import _get_yf_crumb
        _opener, _crumb = _get_yf_crumb()
        _crumb_qs = f"&crumb={urllib.parse.quote(_crumb)}" if _crumb else ""

        url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker_ns}"
               f"?interval=1m&range=1d&includePrePost=false{_crumb_qs}")
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Accept": "application/json",
        })
        with _opener.open(req, timeout=8) as r:
            data = json.loads(r.read())

        meta = data["chart"]["result"][0]["meta"]
        price      = meta.get("regularMarketPrice")
        prev_close = meta.get("previousClose") or meta.get("chartPreviousClose")
        volume     = meta.get("regularMarketVolume")

        if price and float(price) > 0 and not math.isnan(float(price)):
            out = {
                "price":      float(price),
                "prev_close": float(prev_close) if prev_close else float(price),
            }
            if volume:
                out["volume"] = int(volume)
            # Session range + VWAP (Live Ticker table). The range comes from
            # the chart meta; VWAP is volume-weighted typical price over the
            # 1-minute bars this request already returned. Both optional.
            _hi, _lo = meta.get("regularMarketDayHigh"), meta.get("regularMarketDayLow")
            if _hi and _lo:
                out["high"], out["low"] = float(_hi), float(_lo)
            out.update(_vwap_from_bars(data["chart"]["result"][0]))
            return out
    except Exception as e:
        _log.debug("yahoo JSON quote failed for %s: %s", ticker_ns, e)
    return None


# ─── NSE India session (cookie-based, real-time) ────────────────────────────

_nse_session = None
_nse_session_ts: float = 0.0
_NSE_SESSION_TTL = 300


def _get_nse_session():
    global _nse_session, _nse_session_ts
    if _nse_session is None or (time.time() - _nse_session_ts) > _NSE_SESSION_TTL:
        import requests
        s = requests.Session()
        s.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.nseindia.com/",
        })
        try:
            s.get("https://www.nseindia.com/", timeout=6)
        except Exception as e:
            _log.debug("NSE session warm-up failed: %s", e)
        _nse_session = s
        _nse_session_ts = time.time()
    return _nse_session


def _nse_live_price(symbol: str) -> Optional[dict]:
    """NSE India official API — real-time, needs fresh session cookies."""
    try:
        session = _get_nse_session()
        url = f"https://www.nseindia.com/api/quote-equity?symbol={symbol.upper()}"
        resp = session.get(url, timeout=6)
        if resp.status_code != 200:
            return None
        data = resp.json()
        pi = data.get("priceInfo", {})
        price = pi.get("lastPrice") or pi.get("close")
        prev  = pi.get("previousClose") or pi.get("close")
        if price:
            out = {"price": float(price), "prev_close": float(prev or price)}
            try:
                vol = data.get("marketDeptOrderBook", {}).get("tradeInfo", {}).get("totalTradedVolume")
                if vol:
                    out["volume"] = int(vol)
            except Exception:
                pass
            return out
    except Exception as e:
        _log.debug("NSE live price failed for %s: %s", symbol, e)
    return None


# ─── Stooq EOD fallback ──────────────────────────────────────────────────────

def _stooq_eod_price(ticker_ns: str) -> Optional[dict]:
    """Last EOD close from Stooq — never rate-limited, works everywhere."""
    try:
        import io, datetime, pandas as pd
        sym = ticker_ns.lower()
        end   = datetime.date.today()
        start = end - datetime.timedelta(days=7)
        url = (f"https://stooq.com/q/d/l/?s={sym}"
               f"&d1={start.strftime('%Y%m%d')}&d2={end.strftime('%Y%m%d')}&i=d")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read().decode()
        if len(raw) < 60 or "No data" in raw:
            return None
        df = pd.read_csv(io.StringIO(raw))
        df.columns = [c.strip().title() for c in df.columns]
        df = df.dropna(subset=["Close"])
        if df.empty:
            return None
        df = df.sort_values("Date")
        price = float(df["Close"].iloc[-1])
        prev  = float(df["Close"].iloc[-2]) if len(df) > 1 else price
        out = {"price": price, "prev_close": prev}
        if "Volume" in df.columns:
            try:
                vol = df["Volume"].iloc[-1]
                if vol == vol:  # not NaN
                    out["volume"] = int(vol)
            except Exception:
                pass
        return out
    except Exception as e:
        _log.debug("Stooq EOD price failed for %s: %s", ticker_ns, e)
        return None


# ─── Internal: build a normalised quote dict ─────────────────────────────────

def _normalise(q: dict) -> dict:
    """Add chg_pct to a raw {price, prev_close[, volume]} dict.

    FIX VOL1: passes through an optional "volume" key from whichever tier
    supplied it (Yahoo's regularMarketVolume, NSE's totalTradedVolume, or
    Stooq's daily Volume column) so callers get best-effort qty-traded data
    even when Angel One (the only tier with guaranteed real-time volume)
    isn't the source. Omitted entirely if no tier provided one — callers
    must .get("volume") defensively, never index it directly.
    """
    p  = float(q["price"])
    pc = float(q.get("prev_close") or p)
    out = {
        "price":      p,
        "prev_close": pc,
        "chg_pct":    (p / pc - 1) * 100 if pc > 0 else 0.0,
    }
    if q.get("volume"):
        out["volume"] = int(q["volume"])
    # Optional session fields (Yahoo tier only); omitted when absent.
    for _k in ("high", "low", "vwap"):
        if q.get(_k):
            out[_k] = float(q[_k])
    return out


# ─── Public interface ────────────────────────────────────────────────────────

def get_live_price(symbol: str) -> Optional[float]:
    """
    Get current price for a single NSE symbol.
    Returns float or None.
    """
    q = get_live_quote(symbol)
    return q["price"] if q else None


def get_live_quote(symbol: str) -> Optional[dict]:
    """
    Get price + prev_close dict for one NSE symbol.
    Returns {"price": float, "prev_close": float, "chg_pct": float} or None.

    For fetching multiple symbols at once, prefer get_live_prices_batch() —
    it resolves the whole list in 1-2 Angel One round-trips instead of N.

    Priority:
      Tier 0: Angel One SmartAPI (real-time, if credentials configured)
      Tier 1: Yahoo Finance direct JSON (live during market hours)
      Tier 2: NSE India official API (real-time, may fail on cloud)
      Tier 3: Stooq EOD (yesterday's close — always works)
    """
    clean_ns = (symbol if symbol.endswith(".NS") else f"{symbol}.NS")
    clean    = symbol.replace(".NS", "").upper()

    # Tier 0: Angel One (real-time, true LTP)
    try:
        from data.angel_fetcher import get_full_quote as _ao_full_quote, is_configured as _ao_ok
        if _ao_ok():
            q = _ao_full_quote(clean_ns)
            if q:
                # FIX VOL1: get_full_quote (not the stripped get_live_quote
                # wrapper) so this single-symbol path carries "volume"
                # through just like get_live_prices_batch's Angel One tier
                # already does — previously this path silently dropped it.
                out = {
                    "price":      q["price"],
                    "prev_close": q["prev_close"],
                    "chg_pct":    q["chg_pct"],
                }
                if q.get("volume"):
                    out["volume"] = q["volume"]
                return out
    except Exception as e:
        _log.debug("Angel One live quote failed for %s: %s", clean_ns, e)

    # Tiers 1–3: fallbacks
    for fetch_fn, arg in [
        (_yahoo_json_quote, clean_ns),
        (_nse_live_price,   clean),
        (_stooq_eod_price,  clean_ns),
    ]:
        q = fetch_fn(arg)
        if q:
            return _normalise(q)

    _log.warning("all live-price tiers failed for %s — no quote available", symbol)
    return None


def get_live_prices_batch(
    symbols: List[str],
    max_workers: int = 8,
    max_wait_seconds: Optional[float] = None,
) -> Dict[str, Optional[dict]]:
    """
    Fetch live quotes for multiple symbols.
    Returns {symbol: {"price", "prev_close", "chg_pct"} or None}

    Batch strategy
    ──────────────
    Tier 0 (Angel One): calls get_batch_quotes() which groups all symbols into
    50-symbol bulk POST requests.  The entire list resolves in ceil(N/50) round-
    trips — typically 1 for a portfolio, 2–3 for a 100-stock screener scan.
    This replaces the old approach of N parallel get_live_quote() calls which
    saturated Angel One's rate limiter and caused silent fallthrough to Yahoo.

    Any symbol that Angel One returns None for (not configured, circuit breaker
    tripped, delisted, BSE-only stock) falls back to the per-symbol Yahoo →
    NSE → Stooq chain via a thread pool.

    FIX LP1: the fallback wait is now sized to the actual remaining workload
    (each fetch_fn already self-bounds to 6-10s per call, ~24s worst case per
    symbol across all 3 tiers) instead of a flat 20s that silently truncated
    anything past the first round of max_workers symbols. See module docstring.

    FIX LP2 — max_wait_seconds: LP1's scaled wait (rounds * 26 + 4, capped at
    120s) is appropriate for a background scan page (Smart Screener, Tomorrow's
    Watchlist) where the person expects a scan to take a while and the page
    has nothing else competing for that time. It is NOT appropriate for
    dashboard/shared/nav.py's sidebar, which calls this on every single page
    load — with Angel One not configured (is_configured() fails instantly,
    with no network call, so EVERY symbol falls into this fallback path) and
    Yahoo/NSE/Stooq degraded (as commonly happens on Streamlit Cloud's shared
    IPs — see live_price.py's own module docstring on cloud-IP blocking), a
    12-ticker watchlist alone could block the sidebar — and therefore every
    page in the app — for up to 56 seconds, and a 20-ticker list up to 82s.
    max_wait_seconds lets latency-sensitive callers opt into a hard, low
    ceiling instead. It only ever shortens the wait (via min()), never
    lengthens it, so passing nothing at all preserves the exact previous
    behavior for existing callers (e.g. scanner pages) — nothing changes for
    them. Symbols still outstanding when the (shorter) wait ends are logged
    and returned as None, same graceful-degradation path as before; the
    person just sees "price unavailable, refresh to retry" instead of a
    frozen page for the better part of a minute.
    """
    if not symbols:
        return {}

    results: Dict[str, Optional[dict]] = {s: None for s in symbols}
    remaining: List[str] = list(symbols)

    # ── Tier 0: Angel One bulk batch ────────────────────────────────────────
    try:
        from data.angel_fetcher import get_batch_quotes as _ao_batch, is_configured as _ao_ok
        if _ao_ok():
            ao_results = _ao_batch(remaining)
            resolved: List[str] = []
            for sym, q in ao_results.items():
                if q and float(q.get("price", 0)) > 0:
                    # angel_fetcher returns a full dict including chg_pct
                    results[sym] = q
                    resolved.append(sym)
            remaining = [s for s in remaining if s not in resolved]
            if resolved:
                _log.debug(
                    "live_prices_batch: Angel One resolved %d/%d symbols",
                    len(resolved), len(symbols),
                )
    except Exception as e:
        _log.debug("live_prices_batch: Angel One batch failed: %s — falling back per-symbol", e)

    # ── Tiers 1–3: per-symbol fallback for anything Angel One missed ─────────
    if not remaining:
        return results

    from concurrent.futures import ThreadPoolExecutor, wait as _wait

    def _fallback(sym: str) -> tuple:
        clean_ns = sym if sym.endswith(".NS") else f"{sym}.NS"
        clean    = sym.replace(".NS", "").upper()
        for fetch_fn, arg in [
            (_yahoo_json_quote, clean_ns),
            (_nse_live_price,   clean),
            (_stooq_eod_price,  clean_ns),
        ]:
            q = fetch_fn(arg)
            if q:
                return sym, _normalise(q)
        _log.warning("all live-price tiers failed for %s — no quote available", sym)
        return sym, None

    # FIX LP1: scale the wait to the real workload instead of a flat 20s.
    # Worst case per symbol is ~24s (Yahoo 8s + NSE 6s + Stooq 10s, all
    # failing in sequence). With max_workers concurrent slots, N remaining
    # symbols need ceil(N / max_workers) sequential "rounds" through the
    # pool. Capped at 120s so one pathological batch can't hang the page
    # indefinitely — anything still outstanding past that is logged
    # (not silently dropped) and returns None, same as a genuine failure.
    #
    # FIX LP2: max_wait_seconds (when passed) tightens this further — see
    # the docstring above. min() means it can only shorten the wait.
    rounds       = math.ceil(len(remaining) / max_workers)
    wait_timeout = min(120, rounds * 26 + 4)
    if max_wait_seconds is not None:
        wait_timeout = min(wait_timeout, max_wait_seconds)

    pool = ThreadPoolExecutor(max_workers=max_workers)
    try:
        futs = {pool.submit(_fallback, sym): sym for sym in remaining}
        done, not_done = _wait(list(futs.keys()), timeout=wait_timeout)
        for fut in done:
            try:
                sym, val = fut.result(timeout=0)
                results[sym] = val
            except Exception as e:
                _log.debug("fallback quote worker failed: %s", e)
        if not_done:
            pending_syms = [futs[f] for f in not_done]
            _log.warning(
                "live_prices_batch: %d/%d fallback lookups still running after "
                "%.0fs wait — returning None for them (first 10: %s)",
                len(not_done), len(futs), wait_timeout, ", ".join(pending_syms[:10]),
            )
    finally:
        # FIX LP3 — this was `pool.shutdown(wait=False)`, which returns
        # immediately but does NOT stop the work. Futures that were submitted
        # and are still QUEUED keep getting picked up and executed by the
        # worker threads long after this function has returned its results.
        #
        # That is the common case, not the rare one: max_workers is 8, so a
        # 50-symbol scan queues 42 tasks behind the first 8. When the wait
        # above expires, those 42 are abandoned as far as the caller is
        # concerned — their results are thrown away — but each one still runs
        # the full Yahoo(8s) → NSE(6s) → Stooq(10s) fallback chain. The pool
        # keeps burning threads and sockets on answers nobody will read, on
        # every page load that hits the timeout, competing with the next
        # page's fetches. The worker threads are also non-daemon, so the
        # interpreter joins them at exit.
        #
        # cancel_futures=True (Python 3.9+) drops everything not yet started —
        # the large majority. Already-running tasks can't be interrupted and
        # still finish, bounded by their own per-tier timeouts.
        #
        # This was also what made tests/test_pages_smoke.py pathological:
        # 04_analyze_stock.py resolves ~50 Nifty quotes, its leftover queued
        # tasks outlived the test, and once the per-test socket block was torn
        # down they escaped into REAL network calls with real multi-second
        # timeouts. That one page test took 7,754s (2h9m) in a full-suite run
        # versus ~60s in isolation, which is what pushed the CI suite (20-minute
        # budget) to 3h11m.
        pool.shutdown(wait=False, cancel_futures=True)

    return results
