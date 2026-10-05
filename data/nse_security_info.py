"""data/nse_security_info.py — per-security trading status from NSE's daily files.

* sec_list.csv  — Symbol, Series, Security Name, Band, Remarks. Series EQ is
  normal; BE = trade-to-trade (delivery only); BZ = trade-to-trade for
  non-compliant companies; SM / ST = SME platform. Band is the circuit
  filter in % (2/5/10/20/40) or "No Band" (F&O names: dynamic bands).
* EQUITY_L.csv  — SYMBOL, SERIES, DATE OF LISTING, ...

Both are public archive files (no cookie). Fetched once per process per
12 hours; any failure returns {} so pages degrade quietly (CLAUDE.md rule 5).
ASM / GSM surveillance lists are NOT covered: NSE serves them only through
its cookie-gated API.
"""
from __future__ import annotations

import csv
import datetime as _dt
import io
import logging
import threading
import time
import urllib.request
from typing import Dict, Optional

_log = logging.getLogger("data.nse_security_info")

_SEC_LIST_URL = "https://nsearchives.nseindia.com/content/equities/sec_list.csv"
_EQUITY_L_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_TIMEOUT = 20
_TTL_S = 12 * 3600
_FAIL_TTL_S = 15 * 60

_lock = threading.Lock()
_cache: Dict[str, object] = {"t": 0.0, "ok": False, "data": {}}


def _get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "text/csv,*/*"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
        return r.read().decode("utf-8", "replace")


def _norm(row: Dict[str, str]) -> Dict[str, str]:
    return {(k or "").strip().upper(): (v or "").strip() for k, v in row.items()}


def parse_sec_list(text: str) -> Dict[str, Dict]:
    """{SYMBOL: {"series": str, "band": float | None}} — band None = "No Band"."""
    out: Dict[str, Dict] = {}
    for row in csv.DictReader(io.StringIO(text)):
        r = _norm(row)
        sym = r.get("SYMBOL", "").upper()
        if not sym:
            continue
        band_raw = r.get("BAND", "")
        try:
            band: Optional[float] = float(band_raw)
        except ValueError:
            band = None
        # A symbol can appear in more than one series; prefer EQ, then the first seen.
        if sym in out and out[sym]["series"] == "EQ":
            continue
        out[sym] = {"series": r.get("SERIES", "").upper(), "band": band}
    if not out:
        raise ValueError("NSE sec_list.csv schema drift: no SYMBOL rows parsed")
    return out


def parse_equity_list(text: str) -> Dict[str, _dt.date]:
    """{SYMBOL: listing date}."""
    out: Dict[str, _dt.date] = {}
    for row in csv.DictReader(io.StringIO(text)):
        r = _norm(row)
        sym = r.get("SYMBOL", "").upper()
        raw = r.get("DATE OF LISTING", "")
        if not sym or not raw:
            continue
        try:
            out[sym] = _dt.datetime.strptime(raw, "%d-%b-%Y").date()
        except ValueError:
            continue
    return out


def _load() -> Dict[str, Dict]:
    sec = parse_sec_list(_get(_SEC_LIST_URL))
    try:
        listed = parse_equity_list(_get(_EQUITY_L_URL))
    except Exception as e:                 # listing dates are optional
        _log.warning("nse_security_info: EQUITY_L.csv unavailable: %s", e)
        listed = {}
    for sym, d in listed.items():
        sec.setdefault(sym, {"series": "", "band": None})["listed_on"] = d
    return sec


def get_security_info(symbol: str) -> Dict:
    """{"series", "band", "listed_on"?} for one NSE symbol, or {} when unknown
    or the files can't be fetched."""
    sym = (symbol or "").upper().replace(".NS", "").replace(".BO", "")
    now = time.time()
    with _lock:
        ttl = _TTL_S if _cache["ok"] else _FAIL_TTL_S
        if not _cache["t"] or now - float(_cache["t"]) >= ttl:
            try:
                _cache.update(t=now, ok=True, data=_load())
            except Exception as e:
                _log.warning("nse_security_info: fetch failed, flags degrade: %s", e)
                _cache.update(t=now, ok=False, data=_cache.get("data") or {})
        data = _cache["data"]
    return dict(data.get(sym, {})) if isinstance(data, dict) else {}
