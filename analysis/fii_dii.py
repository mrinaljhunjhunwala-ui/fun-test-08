"""
analysis/fii_dii.py — FII / DII cash-market flow tracker.

WHY THIS MATTERS
────────────────
Foreign Institutional Investors (FIIs) and Domestic Institutional Investors
(DIIs) are the biggest single drivers of Nifty direction on any given day.
The daily cash-market net-buy/sell numbers correlate strongly with next-day
Nifty moves:

  * FII net-sell + DII net-buy = the classic "domestic-supported dip" — a
    tradeable pullback, not a trend break.
  * FII net-sell + DII net-sell = distribution; usually precedes weakness.
  * FII net-buy + DII net-buy = broad participation; a persistent rally.

WHERE THE DATA COMES FROM (all FREE)
────────────────────────────────────
Tried in order (FIX FIIDII-FALLBACK, audit #28):

  1. NSE `/api/fiidiiTradeReact` — official, all-exchange (NSE+BSE+MSEI)
     cash figures for the latest session. Called directly first (as of
     2026-10-08 the API answers 200 while the homepage cookie-prime gets
     403), then once more after a best-effort cookie prime.
  2. NSE `/api/fiidiiTradeNse` — same JSON shape, NSE-exchange-only figures.
     Same sign almost always, smaller magnitude; stored with source
     "nse_exchange_only" and never overwrites a date already stored.

The old moneycontrol scrape was removed: the page is now client-rendered
(no FII/DII table in the HTML; earlier 403s), so it could never return rows
and only added a 12 s timeout to every failed refresh.

Everything successfully fetched is persisted to the same DB trade_store
manages (SQLite/Postgres), one row per date. On a fetch failure we surface
"last-known" data rather than an empty page — the trend chart continues
to render, and a status banner explains the freshness.
"""
from __future__ import annotations

import datetime as _dt
import logging
import re
from typing import Dict, List, Optional

import pandas as pd
import requests

import trade_store as _store

_log = logging.getLogger("analysis.fii_dii")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")
_TIMEOUT = 12

_schema_ready_for: Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
# Storage
# ─────────────────────────────────────────────────────────────────────────────

def ensure_schema() -> None:
    global _schema_ready_for
    key = _store._database_url() or _store._SQLITE_PATH
    if _schema_ready_for == key:
        return
    real = "DOUBLE PRECISION" if _store._is_pg() else "REAL"
    with _store._get_conn() as conn:
        cur = conn.cursor()
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS fii_dii_daily (
                date         TEXT PRIMARY KEY,
                fii_buy      {real},
                fii_sell     {real},
                fii_net      {real},
                dii_buy      {real},
                dii_sell     {real},
                dii_net      {real},
                source       TEXT,
                fetched_at   TEXT
            )
        """)
        conn.commit()
    _schema_ready_for = key


def _persist(rows: List[Dict], source: str) -> int:
    """Upsert rows into fii_dii_daily. Returns number of rows written."""
    if not rows:
        return 0
    ensure_schema()
    now = _dt.datetime.now().isoformat(timespec="seconds")
    n = 0
    with _store._get_conn() as conn:
        cur = conn.cursor()
        for r in rows:
            try:
                if _store._is_pg():
                    cur.execute(_store._q("""
                        INSERT INTO fii_dii_daily
                          (date,fii_buy,fii_sell,fii_net,dii_buy,dii_sell,dii_net,source,fetched_at)
                        VALUES (?,?,?,?,?,?,?,?,?)
                        ON CONFLICT (date) DO UPDATE SET
                          fii_buy=EXCLUDED.fii_buy, fii_sell=EXCLUDED.fii_sell, fii_net=EXCLUDED.fii_net,
                          dii_buy=EXCLUDED.dii_buy, dii_sell=EXCLUDED.dii_sell, dii_net=EXCLUDED.dii_net,
                          source=EXCLUDED.source,   fetched_at=EXCLUDED.fetched_at
                    """), (r["date"], r.get("fii_buy"), r.get("fii_sell"), r.get("fii_net"),
                           r.get("dii_buy"), r.get("dii_sell"), r.get("dii_net"), source, now))
                else:
                    cur.execute("""
                        INSERT INTO fii_dii_daily
                          (date,fii_buy,fii_sell,fii_net,dii_buy,dii_sell,dii_net,source,fetched_at)
                        VALUES (?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(date) DO UPDATE SET
                          fii_buy=excluded.fii_buy, fii_sell=excluded.fii_sell, fii_net=excluded.fii_net,
                          dii_buy=excluded.dii_buy, dii_sell=excluded.dii_sell, dii_net=excluded.dii_net,
                          source=excluded.source,   fetched_at=excluded.fetched_at
                    """, (r["date"], r.get("fii_buy"), r.get("fii_sell"), r.get("fii_net"),
                          r.get("dii_buy"), r.get("dii_sell"), r.get("dii_net"), source, now))
                n += 1
            except Exception as e:
                _log.debug("_persist row failed for %s: %s", r.get("date"), e)
        conn.commit()
    return n


def load_history(days: int = 90) -> pd.DataFrame:
    ensure_schema()
    try:
        with _store._get_conn() as conn:
            return _store.read_sql_df(_store._q("""
                SELECT * FROM fii_dii_daily
                ORDER BY date DESC LIMIT ?
            """), conn, params=(int(days),))
    except Exception as e:
        _log.warning("load_history failed: %s", e)
        return pd.DataFrame()


# ─────────────────────────────────────────────────────────────────────────────
# Fetchers
# ─────────────────────────────────────────────────────────────────────────────

def _parse_num(txt: str) -> Optional[float]:
    if txt is None:
        return None
    s = str(txt).strip().replace(",", "").replace("–", "-").replace("−", "-")
    if not s or s in {"-", "--"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


_NSE_HOME = "https://www.nseindia.com/"
_NSE_REACT = "https://www.nseindia.com/api/fiidiiTradeReact"
_NSE_EXCH_ONLY = "https://www.nseindia.com/api/fiidiiTradeNse"


def _parse_nse_payload(payload) -> List[Dict]:
    """NSE fiidiiTrade* JSON -> [{date, fii_buy/sell/net, dii_buy/sell/net}]."""
    # payload is a list of {"category":"FII/FPI *","date":"04-Sep-2026",
    # "buyValue":"12345.67","sellValue":"11111.11","netValue":"1234.56"}
    latest: Dict[str, Dict] = {}
    for entry in payload if isinstance(payload, list) else []:
        if not isinstance(entry, dict):
            continue
        cat = str(entry.get("category", "")).upper()
        try:
            d = _dt.datetime.strptime(str(entry.get("date", "")), "%d-%b-%Y").date().isoformat()
        except ValueError:
            continue
        side = "fii" if "FII" in cat or "FPI" in cat else ("dii" if "DII" in cat else None)
        if not side:
            continue
        row = latest.setdefault(d, {"date": d})
        row[f"{side}_buy"] = _parse_num(entry.get("buyValue"))
        row[f"{side}_sell"] = _parse_num(entry.get("sellValue"))
        row[f"{side}_net"] = _parse_num(entry.get("netValue"))
    # A row is only usable when BOTH nets parsed — a half row would be
    # summed as 0 by the score's 5-day flow sum.
    return [r for r in latest.values()
            if r.get("fii_net") is not None and r.get("dii_net") is not None]


def _nse_get_json(url: str):
    """GET an NSE API: direct first, then once more after a cookie prime."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": _UA, "Accept": "application/json, text/plain, */*",
        "Referer": _NSE_HOME,
    })
    for attempt in (0, 1):
        if attempt:
            try:
                session.get(_NSE_HOME, timeout=_TIMEOUT)   # best effort
            except requests.RequestException as e:
                _log.debug("NSE cookie prime failed: %s", e)
        try:
            r = session.get(url, timeout=_TIMEOUT)
            if r.status_code == 200:
                return r.json()
            _log.info("NSE %s returned %s (attempt %d)", url, r.status_code, attempt + 1)
        except (requests.RequestException, ValueError) as e:
            _log.info("NSE %s failed: %s: %s", url, type(e).__name__, e)
    return None


def _fetch_nse() -> List[Dict]:
    """Primary source: NSE all-exchange FII/DII cash figures."""
    return _parse_nse_payload(_nse_get_json(_NSE_REACT))


def _fetch_nse_exchange_only() -> List[Dict]:
    """Fallback: NSE-exchange-only FII/DII cash figures (same JSON shape)."""
    return _parse_nse_payload(_nse_get_json(_NSE_EXCH_ONLY))


def refresh(*, force: bool = False) -> Dict:
    """
    Fetch latest FII/DII, persist, and report status. Idempotent — if today's
    data is already stored (and force=False) we skip the network entirely.

    Returns {"rows_written": n, "source": "nse|nse_exchange_only|cache|unavailable",
    "latest_date": …}.
    """
    ensure_schema()
    today = _dt.date.today().isoformat()

    if not force:
        existing = load_history(days=3)
        if not existing.empty and existing["date"].iloc[0] >= today:
            return {"rows_written": 0, "source": "cache",
                    "latest_date": existing["date"].iloc[0]}

    rows = _fetch_nse()
    src = "nse"
    if not rows:
        rows = _fetch_nse_exchange_only()
        src = "nse_exchange_only"
        if rows:
            # Never replace an all-exchange row with the narrower basis.
            have = set(load_history(days=10).get("date", pd.Series(dtype=str)).astype(str))
            rows = [r for r in rows if r["date"] not in have]
            if not rows:
                return {"rows_written": 0, "source": "cache",
                        "latest_date": max(have) if have else None}
    if not rows:
        latest = load_history(days=1)
        return {"rows_written": 0, "source": "unavailable",
                "latest_date": (latest["date"].iloc[0] if not latest.empty else None)}
    n = _persist(rows, source=src)
    latest = max((r["date"] for r in rows), default=None)
    return {"rows_written": n, "source": src, "latest_date": latest}
