"""
utils/vix.py — standalone India VIX fetcher.

Deliberately has NO imports from this project so it can never be caught
in a stale sys.modules chain.  Both analysis.score and
analysis.portfolio_manager import from here instead of trading.signals.

Uses direct urllib + Yahoo Finance cookie+crumb auth (required since mid-2024).
No yfinance dependency — safe on Streamlit Cloud.
"""

from __future__ import annotations
import logging
import time
from datetime import datetime
from typing import Any, Dict, Optional

_log = logging.getLogger("vix")
_VIX_CACHE: Optional[Dict] = None
_VIX_CACHE_TTL = 600   # 10 minutes — VIX can spike intraday

# FIX DIAG-VIX (Task 2.3 follow-up C): expose a last-diagnostic getter so
# dashboard/shared/data_health.py can surface VIX freshness in the
# Command Centre panel. Same shape other providers use (nse_corp_info,
# news_feed, etc.). Written on every fetch attempt, success or failure.
_last_diagnostic: Dict[str, Any] = {}


def get_last_diagnostic() -> Dict[str, Any]:
    """Return the diagnostic recorded on the most recent VIX fetch attempt.

    Shape: {"ok": bool, "at": iso, "reason": str, "warnings": int}.
    Empty dict if no fetch has been attempted yet this process.
    """
    return dict(_last_diagnostic)


def get_india_vix_regime() -> Dict:
    """
    Fetch India VIX and classify regime.
    Cached for 10 minutes — refreshes intraday so panic spikes are caught.

    Returns:
        vix        : float | None
        regime     : "complacency" | "normal" | "elevated" | "fear" | "panic" | "unknown"
        allow_buy  : bool   (False when VIX > 28)
        vix_pct_chg: float  (1-day % change)
    """
    global _VIX_CACHE
    if _VIX_CACHE is not None and time.time() - _VIX_CACHE.get("_ts", 0) < _VIX_CACHE_TTL:
        return {k: v for k, v in _VIX_CACHE.items() if k != "_ts"}

    try:
        import json
        import urllib.parse
        import urllib.request

        # FIX YF-SESSION — this used to open its OWN cookie jar + do its OWN
        # two-step consent-gate + crumb dance, duplicating what data.fetcher's
        # _get_yf_crumb() already does (and caches for 30 minutes). Two separate
        # sessions meant a fresh 4-request handshake here on every 10-minute
        # cache miss even when the fetcher already had a live one, and the
        # module docstring's "deliberately has NO imports from this project"
        # rule (written to avoid a stale sys.modules chain) is preserved by
        # importing lazily inside the function rather than at module top-level.
        # If the shared crumb call fails for any reason we fall back to an
        # unauthenticated request — same graceful path as the old inline code.
        try:
            from data.fetcher import _get_yf_crumb
            _opener, _crumb = _get_yf_crumb()
        except Exception as e:
            _log.debug("VIX: shared YF session unavailable, going unauthenticated: %s", e)
            _opener, _crumb = urllib.request.build_opener(), ""

        _ua  = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        _cqs = f"&crumb={urllib.parse.quote(_crumb)}" if _crumb else ""
        url  = (
            "https://query1.finance.yahoo.com/v8/finance/chart/%5EINDIAVIX"
            f"?interval=1d&range=5d&includePrePost=false{_cqs}"
        )
        req = urllib.request.Request(url, headers={
            "User-Agent": _ua,
            "Accept":     "application/json",
        })
        with _opener.open(req, timeout=10) as r:
            data = json.loads(r.read())

        # Defensive parse — flagged by data-provenance-auditor 2026-09-02.
        # Bare indexing here would swallow a Yahoo schema drift into the outer
        # `except`, defaulting the regime to "unknown" with `allow_buy=True` —
        # a live panic-VIX reading would then be misclassified as safe. Naming
        # the drift explicitly makes it visible in the warning log.
        _chart   = (data or {}).get("chart") or {}
        _results = _chart.get("result") or []
        if not _results:
            raise ValueError(
                f"India VIX schema drift: chart.result missing/empty "
                f"(chart.error={_chart.get('error')!r})"
            )
        _indicators = (_results[0] or {}).get("indicators") or {}
        _quote_list = _indicators.get("quote") or []
        if not _quote_list:
            raise ValueError(
                "India VIX schema drift: indicators.quote missing "
                "(provider may have renamed the field)"
            )
        closes = list((_quote_list[0] or {}).get("close") or [])
        # Yahoo leaves the latest session's close null for hours after the
        # close; the price is only in meta. Without this the reading is a
        # session old (same bug as data/fetcher._fill_last_daily_close).
        # Only when meta's trade time is on the last bar's own IST date, so a
        # stale meta or a holiday pad can't put a wrong reading in place.
        _meta = (_results[0] or {}).get("meta") or {}
        _tss = (_results[0] or {}).get("timestamp") or []
        if closes and closes[-1] is None and _meta.get("regularMarketPrice") and _tss:
            try:
                import datetime as _dt
                _ist = _dt.timedelta(hours=5, minutes=30)

                def _day(ts):
                    return (_dt.datetime.fromtimestamp(int(ts), tz=_dt.timezone.utc) + _ist).date()
                if _day(_meta.get("regularMarketTime")) == _day(_tss[-1]):
                    closes[-1] = float(_meta["regularMarketPrice"])
            except (TypeError, ValueError, OverflowError):
                pass
        valid  = [v for v in closes if v is not None]
        if len(valid) < 2:
            raise ValueError("insufficient VIX data")

        curr    = float(valid[-1])
        prev    = float(valid[-2])
        pct_chg = (curr / prev - 1) * 100

        if   curr < 12: regime = "complacency"
        elif curr < 16: regime = "normal"
        elif curr < 22: regime = "elevated"
        elif curr < 28: regime = "fear"
        else:           regime = "panic"

        _VIX_CACHE = {
            "vix":         round(curr, 2),
            "regime":      regime,
            "allow_buy":   curr <= 28,
            "vix_pct_chg": round(pct_chg, 2),
            "_ts":         time.time(),
        }
        _last_diagnostic.update({
            "ok": True, "at": datetime.now().isoformat(),
            "reason": "", "warnings": 0,
        })

    except Exception as e:
        _log.warning("India VIX fetch failed, defaulting to 'unknown' regime: %s", e)
        _VIX_CACHE = {
            "vix": None, "regime": "unknown",
            "allow_buy": True, "vix_pct_chg": 0.0,
            "_ts": time.time(),
        }
        _last_diagnostic.update({
            "ok": False, "at": datetime.now().isoformat(),
            "reason": f"{type(e).__name__}: {e}",
            "warnings": int(_last_diagnostic.get("warnings", 0)) + 1,
        })

    return {k: v for k, v in _VIX_CACHE.items() if k != "_ts"}


def clear_vix_cache() -> None:
    """Force next call to re-fetch from network."""
    global _VIX_CACHE
    _VIX_CACHE = None
