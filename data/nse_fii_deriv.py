"""
data/nse_fii_deriv.py – FII derivatives (index-futures) net positioning.

Feeds the FII-deriv sub-score (3 pts of 10) inside Recommendation 6's
Positioning pillar. See docs/POSITIONING_INTEGRATION_2026-09.md.

WHY
───
FII index-futures net position is one of the strongest same-day directional
signals on Indian markets. FIIs net-long index futures = they are hedged
long overall = risk-on for the market. FIIs net-short = risk-off.

The value is UNIVERSE-LEVEL: a single number per trading day that applies
to every F&O-eligible ticker's positioning score. So one fetch per day,
one row per day in the DB, one snapshot function shared by every score
call. Cheapest of the four Positioning-pillar data pipelines by an order
of magnitude.

WHERE THE DATA COMES FROM
─────────────────────────
NSE publishes `fao_participant_oi_DDMMYYYY.csv` at
`https://nsearchives.nseindia.com/content/nsccl/`. Rows are Client / DII /
FII / Pro / TOTAL; columns include Future Index Long, Future Index Short,
Future Stock Long/Short, and index/stock option long/short breakdowns.

UNITS: contracts (NOT rupees). NSE would need lot-size + price to convert;
we keep the raw signed contract count because (a) the SIGN is the primary
signal, and (b) same-day contract-count comparability across days matches
how retail dashboards render it.

Scoring (FIX FII-PCTL, 2026-09-27): the 3-pt Positioning sub-score uses the
PERCENTILE of today's net within the trailing ~250 stored sessions
(get_latest_fut_idx_pct): >=80th = 3, >=50th = 2, >=20th = 1, else 0;
neutral 1.5 with < 60 sessions of history. The earlier fixed +/-30k
contract bands assumed a -100k..+100k range; 2026-09 values sit near
-300k, which pinned the sub-score at 0.

MUST RUN FROM RESIDENTIAL IP — NSE WAF blocks cloud ranges. Same
constraint as nse_delivery / nse_fno_bhavcopy / qualitative_flags.
"""
from __future__ import annotations

import csv
import datetime as _dt
import io
import logging
from typing import Dict, List, Optional

import pandas as pd
import requests

import trade_store as _store
from utils.sql import read_sql_df

_log = logging.getLogger("data.nse_fii_deriv")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")
_TIMEOUT = 20

# Required columns on the fao_participant_oi CSV. Guardrail §14: any
# missing raises a named ValueError. NSE has kept these header names
# stable since at least 2023 but this guards the drift path.
_REQUIRED_COLS = {"Client Type", "Future Index Long", "Future Index Short",
                  "Future Stock Long", "Future Stock Short"}

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
            CREATE TABLE IF NOT EXISTS nse_fii_deriv_daily (
                date            TEXT PRIMARY KEY,
                fut_idx_long    {real},
                fut_idx_short   {real},
                fut_idx_net     {real},
                fut_stk_long    {real},
                fut_stk_short   {real},
                fut_stk_net     {real},
                fetched_at      TEXT
            )
        """)
        conn.commit()
    _schema_ready_for = key


def _persist(row: Dict) -> int:
    if not row or "date" not in row:
        return 0
    ensure_schema()
    now = _dt.datetime.now().isoformat(timespec="seconds")
    with _store._get_conn() as conn:
        cur = conn.cursor()
        try:
            if _store._is_pg():
                cur.execute(_store._q("""
                    INSERT INTO nse_fii_deriv_daily
                      (date,fut_idx_long,fut_idx_short,fut_idx_net,
                       fut_stk_long,fut_stk_short,fut_stk_net,fetched_at)
                    VALUES (?,?,?,?,?,?,?,?)
                    ON CONFLICT (date) DO UPDATE SET
                      fut_idx_long=EXCLUDED.fut_idx_long,
                      fut_idx_short=EXCLUDED.fut_idx_short,
                      fut_idx_net=EXCLUDED.fut_idx_net,
                      fut_stk_long=EXCLUDED.fut_stk_long,
                      fut_stk_short=EXCLUDED.fut_stk_short,
                      fut_stk_net=EXCLUDED.fut_stk_net,
                      fetched_at=EXCLUDED.fetched_at
                """), (row["date"], row.get("fut_idx_long"),
                       row.get("fut_idx_short"), row.get("fut_idx_net"),
                       row.get("fut_stk_long"), row.get("fut_stk_short"),
                       row.get("fut_stk_net"), now))
            else:
                cur.execute("""
                    INSERT INTO nse_fii_deriv_daily
                      (date,fut_idx_long,fut_idx_short,fut_idx_net,
                       fut_stk_long,fut_stk_short,fut_stk_net,fetched_at)
                    VALUES (?,?,?,?,?,?,?,?)
                    ON CONFLICT(date) DO UPDATE SET
                      fut_idx_long=excluded.fut_idx_long,
                      fut_idx_short=excluded.fut_idx_short,
                      fut_idx_net=excluded.fut_idx_net,
                      fut_stk_long=excluded.fut_stk_long,
                      fut_stk_short=excluded.fut_stk_short,
                      fut_stk_net=excluded.fut_stk_net,
                      fetched_at=excluded.fetched_at
                """, (row["date"], row.get("fut_idx_long"),
                      row.get("fut_idx_short"), row.get("fut_idx_net"),
                      row.get("fut_stk_long"), row.get("fut_stk_short"),
                      row.get("fut_stk_net"), now))
            conn.commit()
            return 1
        except Exception as e:
            _log.warning("nse_fii_deriv._persist failed for %s: %s",
                         row.get("date"), e)
            return 0


# ─────────────────────────────────────────────────────────────────────────────
# Fetcher / parser
# ─────────────────────────────────────────────────────────────────────────────

def _url(date: _dt.date) -> str:
    return (f"https://nsearchives.nseindia.com/content/nsccl/"
            f"fao_participant_oi_{date.strftime('%d%m%Y')}.csv")


def _parse(csv_text: str, target_date: _dt.date) -> Optional[Dict]:
    """
    Parse the fao_participant_oi CSV; extract the FII row. Named
    ValueError on empty body or schema drift.

    NSE prepends a "As on <date>" line above the header on some days;
    the DictReader skips leading blank lines but not that comment line,
    so we normalise it out first.
    """
    if not csv_text or not csv_text.strip():
        raise ValueError("fao_participant_oi: empty response body")

    # Drop a leading "As on" comment line if present so the header row
    # aligns with DictReader's expected structure. NSE has toggled this
    # over the years; the check is cheap and idempotent.
    lines = [ln for ln in csv_text.splitlines() if ln.strip()]
    if lines and not lines[0].lower().startswith("client type"):
        # First real header sits below any preamble comments.
        for i, ln in enumerate(lines):
            if ln.lower().startswith("client type"):
                lines = lines[i:]
                break
    normalised = "\n".join(lines)

    reader = csv.DictReader(io.StringIO(normalised))
    fieldnames = {(fn or "").strip() for fn in (reader.fieldnames or [])}
    missing = _REQUIRED_COLS - fieldnames
    if missing:
        raise ValueError(
            f"NSE fao_participant_oi schema drift: required columns missing "
            f"{sorted(missing)}. Got: {sorted(fieldnames)[:12]}"
        )

    fii_row: Optional[Dict[str, str]] = None
    for raw in reader:
        row = {(k or "").strip(): (v or "").strip() for k, v in raw.items()}
        if row.get("Client Type", "").upper() == "FII":
            fii_row = row
            break
    if fii_row is None:
        # Guardrail §15: silent-empty is worse than a crash.
        _log.warning(
            "fao_participant_oi: no FII row found for %s. Client Type "
            "values seen may have been renamed by NSE.",
            target_date.isoformat(),
        )
        return None

    def _to_int(s: str) -> Optional[float]:
        v = (s or "").replace(",", "").strip()
        if v in {"", "-"}:
            return None
        try:
            return float(v)
        except ValueError:
            return None

    fut_idx_long  = _to_int(fii_row.get("Future Index Long", ""))
    fut_idx_short = _to_int(fii_row.get("Future Index Short", ""))
    fut_stk_long  = _to_int(fii_row.get("Future Stock Long", ""))
    fut_stk_short = _to_int(fii_row.get("Future Stock Short", ""))

    def _net(l, s):
        return (l - s) if (l is not None and s is not None) else None

    return {
        "date":          target_date.isoformat(),
        "fut_idx_long":  fut_idx_long,
        "fut_idx_short": fut_idx_short,
        "fut_idx_net":   _net(fut_idx_long, fut_idx_short),
        "fut_stk_long":  fut_stk_long,
        "fut_stk_short": fut_stk_short,
        "fut_stk_net":   _net(fut_stk_long, fut_stk_short),
    }


def _fetch(date: _dt.date) -> str:
    url = _url(date)
    headers = {
        "User-Agent": _UA,
        "Accept": "text/csv,application/csv,*/*",
        "Referer": "https://www.nseindia.com/",
    }
    r = requests.get(url, headers=headers, timeout=_TIMEOUT)
    if r.status_code == 404:
        raise ValueError(
            f"fao_participant_oi not available for {date.isoformat()} (404). "
            f"Likely a market holiday or not yet published."
        )
    r.raise_for_status()
    text = r.text or ""
    if text.lstrip().startswith("<"):
        raise ValueError(
            f"NSE returned HTML (not CSV) for {url} — probable rate-limit "
            f"or WAF challenge. Body starts: {text[:120]!r}"
        )
    return text


def fetch_and_persist(date: Optional[_dt.date] = None) -> int:
    date = date or _dt.date.today()
    text = _fetch(date)
    row = _parse(text, date)
    if row is None:
        return 0
    written = _persist(row)
    _log.info("nse_fii_deriv: persisted %d row for %s (fut_idx_net=%s)",
              written, date.isoformat(), row.get("fut_idx_net"))
    return written


# ─────────────────────────────────────────────────────────────────────────────
# Read API used by analysis.score
# ─────────────────────────────────────────────────────────────────────────────

def load_history(days: int = 30) -> pd.DataFrame:
    ensure_schema()
    try:
        with _store._get_conn() as conn:
            return read_sql_df(_store._q("""
                SELECT * FROM nse_fii_deriv_daily
                ORDER BY date DESC LIMIT ?
            """), conn, params=(int(days),))
    except Exception as e:
        _log.warning("nse_fii_deriv.load_history failed: %s", e)
        return pd.DataFrame()


def get_latest_fut_idx_net() -> Optional[float]:
    """Return the most-recent FII net index-futures position, in CONTRACTS.

    Sign is the primary signal; magnitude feeds the 4-band Positioning
    sub-score. Returns None when the DB is empty or the most-recent row
    has no fut_idx_net value.
    """
    hist = load_history(days=1)
    if hist is None or hist.empty:
        return None
    v = hist["fut_idx_net"].iloc[0]
    if v is None or pd.isna(v):
        return None
    return float(v)


def get_latest_fut_idx_pct(lookback: int = 250, min_history: int = 60) -> Optional[float]:
    """Percentile (0-1) of the latest FII net index-futures position within
    the trailing `lookback` stored sessions (latest included). None when
    fewer than `min_history` sessions are stored - too little history to
    say what "heavy" or "light" means. Used by the Positioning sub-score
    (FIX FII-PCTL): raw contract counts drift with lot-size changes, so
    fixed thresholds go stale; a trailing percentile does not.
    """
    hist = load_history(days=lookback)
    if hist is None or hist.empty or "fut_idx_net" not in hist:
        return None
    vals = pd.to_numeric(hist["fut_idx_net"], errors="coerce").dropna()
    if len(vals) < min_history:
        return None
    latest = float(vals.iloc[0])            # load_history is newest-first
    return float((vals <= latest).mean())
