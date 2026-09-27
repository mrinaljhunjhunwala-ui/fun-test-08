"""
data/fno_universe.py – NSE F&O-eligible universe.

Recommendation 6 (design 6a) of docs/COMPOSITE_SCORE_SHAPE_REVIEW.md
adds a Positioning pillar to the composite score. The pillar only
applies to F&O-eligible names — non-F&O names keep the legacy 4-pillar
40+25+15+10=90 shape because their positioning inputs (options OI,
PCR, max pain, FII index-futures) do not exist by construction.

This module is the source of truth for that eligibility check.

DATA
────
NSE publishes the F&O eligibility list in monthly circulars. This starter
list is the Nifty-50 + Bank-Nifty + top mid-caps that have been F&O-eligible
continuously through 2025-26. It intentionally errs SMALL (~60 names) so
Recommendation 6 activates on the tickers where the data quality is best.

FOLLOW-UP: refresh from the most recent NSE circular (typically monthly).
See docs/POSITIONING_INTEGRATION_2026-09.md for the process.
"""
from __future__ import annotations

from typing import Iterable, Set

# ── F&O-eligible starter list (Nifty-50 + high-volume mid-caps) ─────────────
# Tickers stored WITHOUT the .NS suffix; the checker normalises both forms.
_FNO_TICKERS: Set[str] = frozenset({
    # Nifty 50 (core)
    "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK",
    "BAJAJ-AUTO", "BAJAJFINSV", "BAJFINANCE", "BEL", "BHARTIARTL",
    "BPCL", "BRITANNIA", "CIPLA", "COALINDIA", "DIVISLAB",
    "DRREDDY", "EICHERMOT", "GRASIM", "HCLTECH", "HDFCBANK",
    "HDFCLIFE", "HEROMOTOCO", "HINDALCO", "HINDUNILVR", "ICICIBANK",
    "INDUSINDBK", "INFY", "ITC", "JIOFIN", "JSWSTEEL",
    "KOTAKBANK", "LT", "LTM", "M&M", "MARUTI",
    "NESTLEIND", "NTPC", "ONGC", "POWERGRID", "RELIANCE",
    "SBILIFE", "SBIN", "SHRIRAMFIN", "SUNPHARMA", "TATACONSUM",
    "TATASTEEL", "TCS", "TECHM", "TITAN",
    "TRENT", "ULTRACEMCO", "UPL", "WIPRO",  # NB: TATAMOTORS removed 2026-09-18 (demerger — no F&O contracts)
    # Bank Nifty additions
    "BANDHANBNK", "FEDERALBNK", "IDFCFIRSTB", "PNB", "RBLBANK",
    # High-conviction mid-caps continuously F&O since 2023
    "ABB", "AMBUJACEM", "ASHOKLEY",
    "AUROPHARMA", "BANKBARODA", "BHEL", "BIOCON",
    "BOSCHLTD", "CANBK", "CHOLAFIN", "COLPAL", "CONCOR",
    "CUMMINSIND", "DABUR", "DIXON",
    "GAIL", "GODREJCP", "GODREJPROP", "HAVELLS", "HDFCAMC",
    "ICICIGI", "ICICIPRULI", "IDEA", "IEX",     "INDHOTEL", "INDIGO", "IOC",     "JINDALSTEL", "JUBLFOOD", "LAURUSLABS", "LICHSGFIN", "LICI",
    "LUPIN", "MANAPPURAM", "MARICO", "MFSL",
    "MOTHERSON", "MPHASIS", "NAM-INDIA", "NAUKRI",     "NMDC", "OBEROIRLTY", "OFSS", "PAGEIND",     "PERSISTENT", "PETRONET", "PFC", "PIDILITIND", "PIIND",
    "PNBHOUSING", "POLYCAB", "RECLTD", "SAIL", "SBICARD",
    "SIEMENS", "SRF",     "TATAPOWER", "TIINDIA", "TORNTPHARM",
    "TVSMOTOR", "VBL", "VEDL", "VOLTAS", "ZYDUSLIFE",
    # TORNTPOWER and UBL removed 2026-09-18 — dropped from NSE F&O universe
    # Removed 2026-09-27 (absent from the 2026-09-25 F&O bhavcopy): ABFRL, ACC,
    # BALKRISIND, DALBHARAT, DEEPAKNTR, IGL, IPCALAB, IRCTC, LTTS, NAVINFLUOR,
    # PEL, SUNTV, SYNGENE, TATACHEM, TATACOMM. LTIM renamed LTM (LTIMindtree).
})


def _normalize(ticker: str) -> str:
    """Strip .NS suffix and uppercase. Handles either shape safely."""
    if not ticker:
        return ""
    t = str(ticker).upper().strip()
    if t.endswith(".NS"):
        t = t[:-3]
    return t


def is_fno_eligible(ticker: str) -> bool:
    """True when the ticker is F&O-eligible per the starter universe.

    Non-F&O names skip Recommendation 6's Positioning pillar entirely
    and keep the legacy 4-pillar 40+25+15+10=90 shape.
    """
    return _normalize(ticker) in _FNO_TICKERS


def list_fno_tickers() -> Set[str]:
    """Return the full F&O-eligible set (without .NS suffix)."""
    return set(_FNO_TICKERS)


def add_fno_tickers(extra: Iterable[str]) -> None:
    """Extend the in-process F&O set — for tests and one-off overrides.

    Persistent additions should edit _FNO_TICKERS above and land as a
    commit with the source NSE circular referenced in the message.
    """
    global _FNO_TICKERS
    _FNO_TICKERS = frozenset(_FNO_TICKERS | {_normalize(t) for t in extra if t})


def check_universe_drift(date=None):
    """Compare _FNO_TICKERS against the most recent F&O bhavcopy in the DB.

    Returns a dict with:
      - db_date:   the bhavcopy date used for comparison (or None if empty)
      - db_count:  symbols in that day's bhavcopy
      - stale:     symbols in _FNO_TICKERS but NOT in the bhavcopy (candidates to remove)
      - missing:   symbols in the bhavcopy but NOT in _FNO_TICKERS (candidates to add)

    Reads through trade_store._get_conn(), so it sees the same DB the
    bhavcopy fetcher writes to — Postgres when DATABASE_URL is set, else the
    local SQLite file. (It used to open trades.db directly, which on a
    Postgres deployment always looked empty and made the check a silent no-op.)
    Imports are lazy so is_fno_eligible() callers stay import-light.
    Returns an empty result if the bhavcopy table has no rows yet.
    """
    import trade_store as _store
    from data.nse_fno_bhavcopy import ensure_schema

    empty = {"db_date": None, "db_count": 0, "stale": set(), "missing": set()}
    ensure_schema()
    with _store._get_conn() as conn:
        cur = conn.cursor()
        if date is None:
            cur.execute("SELECT MAX(date) FROM nse_fno_oi_daily")
            r = cur.fetchone()
            date = r[0] if r else None
        if not date:
            return empty
        cur.execute(_store._q("SELECT DISTINCT symbol FROM nse_fno_oi_daily WHERE date=?"),
                    (date,))
        rows = cur.fetchall()
    db_set = {_normalize(r[0]) for r in rows if r[0]}
    static = set(_FNO_TICKERS)
    return {
        "db_date": date,
        "db_count": len(db_set),
        "stale": static - db_set,
        "missing": db_set - static,
    }
