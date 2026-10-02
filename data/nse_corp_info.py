"""
data/nse_corp_info.py

NSE corporate-info fetcher used by the app and tests.

Exports:
 - get_corp_info(symbol, use_cache=True) -> dict
 - get_last_diagnostic(symbol) -> dict | None
 - _NseFetchError exception class
 - _session_singleton (module-level) so tests can reset it

Behavior notes:
 - Primes the NSE homepage (simple GET to "/") before calling the API path.
 - On HTTP 403, other non-200, malformed JSON (non-dict), or network errors
   it records diagnostics and returns {} (recoverable) — callers should check
   get_last_diagnostic(symbol) to see why, if the result is empty.
 - Maintains a small in-memory cache when use_cache=True.
"""

from __future__ import annotations
import logging
import re
from datetime import datetime
from typing import Optional, Dict, Any
import requests

_log = logging.getLogger("data.nse_corp_info")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# Module-level session singleton; tests may reset this to force a fresh session.
_session_singleton: Optional[requests.Session] = None

# Simple in-memory cache: symbol -> (data_dict, generated_at_iso)
_cache: Dict[str, Dict[str, Any]] = {}

# Last diagnostics recorded per symbol for observability (used by UI/tests)
_last_diagnostics: Dict[str, Dict[str, Any]] = {}


class _NseFetchError(Exception):
    """Raised when the NSE API explicitly blocks the request (HTTP 403)."""


def _extract_status_code_from_exception(exc: Exception) -> Optional[int]:
    """
    Best-effort extract of an HTTP status code from an exception object or its text.
    Looks for:
      - exc.response.status_code (requests.HTTPError shape)
      - exc.status_code attribute
      - first 3-digit number in the exception text
    """
    # requests exceptions may carry .response
    resp = getattr(exc, "response", None)
    if resp is not None:
        sc = getattr(resp, "status_code", None)
        try:
            if sc is not None:
                return int(sc)
        except Exception:
            pass

    sc_attr = getattr(exc, "status_code", None)
    if sc_attr is not None:
        try:
            return int(sc_attr)
        except Exception:
            pass

    # A timeout / connection failure has no HTTP status. Its text does carry
    # "port=443", which the 3-digit fallback below used to report as
    # status 443 (audit 2026-10-03).
    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return None

    # Fallback: search for a 3-digit HTTP-status-like code in text (not a port)
    m = re.search(r"(?<!port=)(?<!:)\b([1-5]\d{2})\b", str(exc))
    if m:
        try:
            return int(m.group(1))
        except Exception:
            pass

    return None


def _ensure_session() -> requests.Session:
    """Return a module-level Session, creating it if needed."""
    global _session_singleton
    if _session_singleton is None:
        s = requests.Session()
        # Default headers mimic a real browser to reduce 403 risk in practice.
        # FIX CORPINFO-UA (audit 2026-10-03): NSE's WAF tarpits the old custom
        # "nse-smart-investor/1.0" agent (read timeout after ~21 s on every
        # call) while a Chrome agent gets HTTP 200 in ~1 s — the same agent
        # nse_delivery / fii_dii already use.
        s.headers.update({
            "User-Agent": _UA,
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://www.nseindia.com/",
        })
        _session_singleton = s
    return _session_singleton


def get_last_diagnostic(symbol: str) -> Optional[Dict[str, Any]]:
    """Return the last recorded diagnostic dict for a symbol, or None."""
    if not symbol:
        return None
    sym = symbol.strip().upper()
    return _last_diagnostics.get(sym)


def get_corp_info(symbol: str, use_cache: bool = True) -> dict:
    """
    Fetch corporate info for `symbol` from NSE.

    Parameters:
      - symbol: ticker symbol, e.g. "RELIANCE.NS" (function normalizes case)
      - use_cache: if True, return cached data if present (default True).
                   tests call with use_cache=False to force a fresh fetch.

    Returns:
      - dict with parsed API result on success
      - {} on recoverable failure (network error, parse error, non-200 incl. 403)
        — check get_last_diagnostic(symbol) for the reason
    """
    if not symbol:
        raise ValueError("symbol required")
    sym = symbol.strip().upper()

    # Cache fast-path
    if use_cache:
        cached = _cache.get(sym)
        if cached and isinstance(cached, dict) and "data" in cached:
            return cached["data"]

    base = "https://www.nseindia.com"
    homepage_url = base + "/"
    # FIX PROV-2026-09-06 — NSE retired /api/corporate-info/{sym} (returns
    # HTTP 404 "Resource not found"). The current live endpoint is
    # /api/top-corp-info?symbol=X&market=cash and returns the same top-level
    # shape the rest of this module and analysis/qualitative_flags.py already
    # expect (latest_announcements.data, corporate_actions.data,
    # shareholdings_patterns, financial_results, borad_meeting). Caught by
    # the 2026-09-06 data-provenance audit; see docs/DATA_PROVENANCE_2026-09.md
    # finding #4. Note qualitative_flags.py:370 was already documented against
    # top-corp-info - the URL had diverged from the internal doc at some point.
    api_url = f"{base}/api/top-corp-info?symbol={sym}&market=cash"

    session = _ensure_session()

    try:
        # Prime the session by GETting the homepage; some NSE endpoints expect this.
        try:
            session.get(homepage_url, timeout=10)
        except Exception:
            # Ignore homepage priming errors; we'll still attempt the API call below.
            _log.debug("nse_corp_info: homepage prime failed (ignoring)")

        resp = session.get(api_url, timeout=10)

        # If blocked (403), record diagnostics and return {} like other
        # non-200 failures (matches this function's documented contract).
        if getattr(resp, "status_code", None) == 403:
            diag = {
                "ok": False,
                "status_code": 403,
                "reason": "HTTP 403 Forbidden from NSE API",
                "at": datetime.now().isoformat(),
            }
            _last_diagnostics[sym] = diag
            _log.warning("nse_corp_info: blocked fetching %s — status=403", sym)
            return {}

        if getattr(resp, "status_code", None) != 200:
            # Non-200 (but not 403): record and return empty dict
            sc = getattr(resp, "status_code", None)
            diag = {
                "ok": False,
                "status_code": sc,
                "reason": f"HTTP {sc} from NSE API",
                "at": datetime.now().isoformat(),
            }
            _last_diagnostics[sym] = diag
            _log.debug("nse_corp_info: non-200 fetching %s — status=%s", sym, sc)
            return {}

        # Parse JSON defensively
        try:
            parsed = resp.json()
        except Exception:
            # fallback: try to parse text as JSON if possible
            try:
                import json as _json
                parsed = _json.loads(resp.text or "")
            except Exception as _e:
                diag = {
                    "ok": False,
                    "status_code": getattr(resp, "status_code", None),
                    "reason": f"failed to parse JSON: {str(_e)}",
                    "at": datetime.now().isoformat(),
                }
                _last_diagnostics[sym] = diag
                _log.debug("nse_corp_info: JSON parse failed for %s: %s", sym, _e)
                return {}

        # Ensure parsed is a dict — callers expect dict shape
        if not isinstance(parsed, dict):
            diag = {
                "ok": False,
                "status_code": getattr(resp, "status_code", None),
                "reason": "parsed JSON not an object/dict",
                "at": datetime.now().isoformat(),
            }
            _last_diagnostics[sym] = diag
            _log.debug("nse_corp_info: parsed JSON is not dict for %s (type=%s)", sym, type(parsed))
            return {}

        # Success: cache & diagnostics
        _cache[sym] = {"data": parsed, "generated_at": datetime.now().isoformat()}
        _last_diagnostics[sym] = {
            "ok": True,
            "status_code": getattr(resp, "status_code", None),
            "reason": "ok",
            "at": datetime.now().isoformat(),
        }
        return parsed

    except Exception as e:
        # Network/parsing/other unexpected errors: record and return {}
        sc = _extract_status_code_from_exception(e)
        diag = {
            "ok": False,
            "status_code": sc,
            "reason": str(e),
            "at": datetime.now().isoformat(),
        }
        _last_diagnostics[sym] = diag
        _log.debug("nse_corp_info: fetch failed for %s: %s (extracted_status=%s)", sym, e, sc)
        return {}
