"""
data/bse_corp_info.py — BSE fallback for corporate disclosures.

FIX FLAGS-BSE1. Second, independent remedy for the NSE corp-info WAF block
documented in data/nse_corp_info.py's module docstring: NSE and BSE run
separate WAFs, so a cloud/datacenter IP being blocked on NSE says nothing
about whether the same IP is also blocked on BSE. Most NSE-listed large/
mid-caps are dual-listed on BSE, so this covers a large fraction of the
tickers this app actually scores even when NSE's endpoint is unreachable.

Uses the community-maintained `bse` package (PyPI: bse,
github.com/BennyThadikaran/BseIndiaApi) — NOTE this package is GPLv3
licensed (unlike the MIT reference implementation nse_corp_info.py is
ported from). For a personal, non-distributed tool this is not a practical
concern, but flagging it explicitly rather than deciding silently, since
license terms are a decision for the project owner, not something to
resolve quietly in a dependency add.

WHAT THIS RETURNS: reshaped to the EXACT same dict shape
data/nse_corp_info.py.get_corp_info() returns —
    {"latest_announcements": {"data": [{"subject", "broadcastdate"}, ...]},
     "corporate_actions":    {"data": [{"purpose", "exdate"}, ...]}}
— specifically so analysis/qualitative_flags.py's existing
parse_announcement_flags() / parse_corporate_action_flags() consume this
unmodified, with no new parsing logic needed. Shareholding-pattern data
(NSE's shareholdings_patterns key) has no equivalent surfaced here — the
`bse` package doesn't expose it — so that key is always absent; the
existing parse_shareholding_flags() already treats a missing key as "no
data" and degrades to an empty list, same as any other missing source.

CONTRACT (matches nse_corp_info.py's get_corp_info exactly):
    * never raises to the caller — returns {} on any failure
    * caches raw responses for 24h (TTLCache, same policy as the NSE module)
    * caches the ticker -> BSE scripcode lookup separately, for 30 days
      (scripcode is effectively permanent — no reason to re-resolve it
      every day just because the 24h announcements cache expired)
    * records every attempt via get_last_diagnostic(ticker), same shape as
      nse_corp_info.get_last_diagnostic, so the UI/warning banner can report
      on this source exactly the same way it already does for NSE

REQUIRES: `bse` package — in requirements.txt since 2026-10-07 (verified
live against RELIANCE on bse 3.3.3: announcements "Table" envelope and
actions "Purpose"/"exdate" fields unchanged). If it's missing anyway, this
module still degrades to {} with a WARNING and a diagnostic the Data Health
page surfaces, rather than raising ImportError into the app.
"""
from __future__ import annotations

import datetime
import logging
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from analysis.fundamentals.cache import TTLCache

_log = logging.getLogger("data.bse_corp_info")

_raw_cache = TTLCache(ttl_seconds=24 * 60 * 60, name="bse_corp_info")
_scripcode_cache = TTLCache(ttl_seconds=30 * 24 * 60 * 60, name="bse_scripcode")

_DOWNLOAD_DIR = Path(tempfile.gettempdir()) / "nse_smart_investor_bse_cache"

_last_diagnostic: Dict[str, Dict[str, Any]] = {}

_bse_client = None
_bse_import_error: Optional[str] = None


def _get_client():
    """Lazy singleton — importing `bse` is optional (see module docstring);
    a missing dependency degrades every call to {} instead of crashing the
    app at import time."""
    global _bse_client, _bse_import_error
    if _bse_client is not None:
        return _bse_client
    if _bse_import_error is not None:
        return None
    try:
        from bse import BSE as _BSEClient
        _DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        _bse_client = _BSEClient(str(_DOWNLOAD_DIR))
        return _bse_client
    except Exception as e:
        _bse_import_error = str(e)
        _log.warning("data.bse_corp_info: `bse` package unavailable (%s) — BSE fallback "
                    "disabled until it's installed (pip install bse)", e)
        return None


def package_status() -> tuple[bool, str]:
    """(available, reason) for the optional `bse` dependency, without making
    a network call — lets the Data Health page say "BSE fallback disabled"
    instead of the fallback silently returning {} forever."""
    if _bse_import_error is not None:
        return False, f"`bse` package unavailable ({_bse_import_error})"
    import importlib.util
    if importlib.util.find_spec("bse") is None:
        return False, "`bse` package not installed (pip install -r requirements.txt)"
    return True, ""


def _normalize_symbol(ticker: str) -> str:
    return ticker.replace(".NS", "").replace(".BO", "").upper()


def _classify(error: Optional[Exception]) -> str:
    if error is None:
        return "unknown"
    msg = str(error)
    if isinstance(error, ConnectionError) or "ConnectionError" in type(error).__name__:
        return f"BSE request error: {msg}"
    if isinstance(error, TimeoutError):
        return "timeout — BSE did not respond in time"
    if isinstance(error, ValueError):
        return f"symbol not found on BSE: {msg}"
    return f"unexpected error: {type(error).__name__}: {msg}"


def get_last_diagnostic(ticker: str) -> Optional[Dict[str, Any]]:
    """Same shape as nse_corp_info.get_last_diagnostic — {"ok", "reason",
    "at"} — for the most recent fetch attempt for this ticker this process
    lifetime, or None if never attempted."""
    return _last_diagnostic.get(_normalize_symbol(ticker))


def _get_scripcode(client, symbol: str) -> Optional[str]:
    cache_key = f"scripcode:{symbol}"
    cached = _scripcode_cache.get(cache_key)
    if cached is not None:
        return cached or None  # cached "" means "looked up, not found"
    try:
        code = client.getScripCode(symbol)
        _scripcode_cache.set(cache_key, code)
        return code
    except Exception as e:
        _log.info("bse_corp_info: no BSE scripcode found for %s: %s", symbol, e)
        _scripcode_cache.set(cache_key, "")  # cache the miss too — don't re-hit every call
        return None


def get_corp_info(ticker: str, use_cache: bool = True) -> Dict[str, Any]:
    """Fetch recent BSE announcements + forthcoming corporate actions for a
    ticker, reshaped to match nse_corp_info.get_corp_info()'s return shape.
    Returns {} (never raises) if the symbol isn't BSE-listed, the `bse`
    package isn't installed, or the request fails after BSE's own retry
    handling is exhausted.
    """
    symbol = _normalize_symbol(ticker)
    now_iso = datetime.datetime.now().isoformat()
    if not symbol:
        return {}

    cache_key = f"corp_info:{symbol}"
    if use_cache:
        cached = _raw_cache.get(cache_key)
        if cached is not None:
            return cached

    client = _get_client()
    if client is None:
        _last_diagnostic[symbol] = {
            "ok": False, "reason": "`bse` package not installed", "at": now_iso,
        }
        return {}

    try:
        scripcode = _get_scripcode(client, symbol)
        if not scripcode:
            _last_diagnostic[symbol] = {
                "ok": False, "reason": f"{symbol} not found on BSE (not listed there, "
                                       "or lookup failed)", "at": now_iso,
            }
            return {}

        from_date = datetime.datetime.now() - datetime.timedelta(days=30)
        to_date = datetime.datetime.now()

        announcements_raw = client.announcements(
            scripcode=scripcode, from_date=from_date, to_date=to_date,
        )
        actions_raw = client.actions(scripcode=scripcode)

        # Defensive schema check — flagged by data-provenance-auditor 2026-09-02.
        # Previously the `.get("Table") or []` chain silently returned an empty
        # announcements list whenever the upstream `bse` client reshaped its
        # response (e.g. dropped the "Table" envelope), making a real API drift
        # look like "no announcements this month" to the UI. Log a WARNING so
        # drift is visible in operator logs instead of being invisible.
        if announcements_raw and isinstance(announcements_raw, dict) \
                and "Table" not in announcements_raw:
            _log.warning(
                "bse_corp_info: %s announcements response missing 'Table' key "
                "(top-level keys: %s) — possible upstream schema drift.",
                ticker, list(announcements_raw.keys())[:8],
            )
        ann_items = (announcements_raw or {}).get("Table") or []
        def _bse_exdate_to_iso(raw: Any) -> str:
            s = str(raw or "").strip()
            if len(s) == 8 and s.isdigit():
                return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
            return now_iso[:10]

        result = {
            "latest_announcements": {
                "data": [
                    {"subject": a.get("NEWSSUB") or a.get("HEADLINE") or "",
                     "broadcastdate": str(a.get("News_submission_dt")
                                          or a.get("DissemDT") or now_iso[:10])[:10]}
                    for a in ann_items
                ],
            },
            "corporate_actions": {
                "data": [
                    {"purpose": a.get("Purpose") or "",
                     "exdate": _bse_exdate_to_iso(a.get("exdate"))}
                    for a in (actions_raw or [])
                ],
            },
        }
        if use_cache:
            _raw_cache.set(cache_key, result)
        _last_diagnostic[symbol] = {"ok": True, "reason": "ok", "at": now_iso}
        return result
    except Exception as e:
        _last_diagnostic[symbol] = {"ok": False, "reason": _classify(e), "at": now_iso}
        _log.warning("bse_corp_info: get_corp_info(%s) failed: %s", ticker, e)
        return {}
