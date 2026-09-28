"""
analysis/verdict_ledger.py — persist every FinalVerdict the model emits,
compute forward returns later, and answer "is this system actually right?"

Why this exists
───────────────
The whole point of building a scoring system is to know, later, whether it
worked. Without a ledger, every day's verdict is thrown away and the app
becomes a beautiful opinion machine with no accountability. With a ledger:

  * VERDICT CALIBRATION — for every BUY/STRONG BUY/WATCH/HOLD/AVOID we
    emitted, what did the stock actually do over the next 1d / 5d / 20d /
    60d / 250d? Also relative to NIFTY (alpha) so we don't take credit for
    market-wide moves. And by conviction bucket, so we can plot a real
    calibration curve (higher conviction → higher realised return, or is it
    flat noise?).

  * SHADOW TRADES — every STRONG BUY / BUY the model emits gets recorded
    with source='shadow_auto', whether or not the user paper-traded it.
    Three months later we can look back and see which winners we skipped —
    the single most useful piece of information for improving discipline.

Design
──────
  * Storage: two new tables (verdict_log, verdict_forward_returns) inside
    the SAME database trade_store.py already manages (SQLite locally,
    Postgres on Streamlit Cloud when DATABASE_URL is set). We reuse
    trade_store's connection pool and _q() placeholder translation so both
    backends work without duplicating code. This is deliberate — the ledger
    is EXACTLY as durable as the paper-trade store, no more, no less.

  * De-duplication: UNIQUE(logged_date, ticker, horizon, source) means the
    same ticker viewed 20 times on the same day yields ONE log row per
    horizon per source. The dashboard visit count is not information.

  * Forward returns are computed lazily. On Calibration-page load we scan
    for log entries whose forward-return columns are still None and enough
    calendar days have elapsed, then batch-fetch OHLCV via the existing
    data.fetcher.fetch_single (Stooq → Yahoo fallback — zero new data
    sources). Prices are stored so we never re-fetch the same one.

  * NIFTY benchmark: we log ^NSEI's price at the same time and compute
    forward returns for it too, so every ticker return can be paired with
    its alpha (ret_ticker - ret_nifty). A verdict that made +5% while the
    index made +6% is a losing call — the ledger records that honestly.

  * No new dependencies. Uses trade_store, pandas, and the existing fetcher.
"""
from __future__ import annotations

import datetime as _dt
import logging
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

import trade_store as _store
from utils.nse_holidays import NSE_HOLIDAYS, is_trading_day
from utils.sql import read_sql_df

_log = logging.getLogger("analysis.verdict_ledger")

# ── Horizons we track, in TRADING days (sessions). Short-term validation
# (1, 5 sessions), ~1 month (20), ~1 quarter (60), ~1 year (250).
#
# FIX LEDGER-TD (2026-09-24) — these used to be added as CALENDAR days
# (logged + timedelta(days=h)) while the UI labelled them like trading
# periods, so "20d (1 mo)" was really ~14 sessions and "250d (~1 yr)" was
# ~8 months. Targets are now logged_date + h NSE sessions (weekends and
# utils.nse_holidays skipped; years not yet in that calendar fall back to
# weekdays, and _find_bar's "first bar on-or-after" absorbs any miss).
# Rows back-filled before this fix keep their calendar-day values.
HORIZONS_DAYS = (1, 5, 20, 60, 250)
NIFTY_BENCH = "^NSEI"


_NSE_BDAY = pd.offsets.CustomBusinessDay(holidays=sorted(NSE_HOLIDAYS))


def horizon_target_date(logged: _dt.date, h: int) -> _dt.date:
    """logged_date + h NSE sessions (weekends and NSE holidays skipped —
    plain BDay counted a holiday as a session, ending the window a day early)."""
    return (pd.Timestamp(logged) + _NSE_BDAY * int(h)).date()


# ── Session dating (FIX LEDGER-IST, 2026-09-27) ──────────────────────────────
# logged_date used to be the SERVER's wall-clock date. Streamlit Cloud runs on
# UTC, so a scan between 00:00 and 05:30 IST was dated the previous day — and
# even an IST wall-clock date is wrong before the open: a scan at 08:00 IST
# prices off the PREVIOUS session's close. logged_date is now the NSE session
# the entry price belongs to — weekends and NSE holidays (utils.nse_holidays)
# roll back to the previous trading day.
_IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))
_NSE_OPEN = _dt.time(9, 15)
_NSE_CLOSE = _dt.time(15, 30)


def _now_ist() -> _dt.datetime:
    return _dt.datetime.now(_IST)


def _prev_session(d: _dt.date) -> _dt.date:
    d -= _dt.timedelta(days=1)
    while not is_trading_day(d):
        d -= _dt.timedelta(days=1)
    return d


def session_date(now: Optional[_dt.datetime] = None) -> _dt.date:
    """The NSE session whose prices a scan run at `now` sees: today (IST) once
    the market has opened on a trading day, else the previous trading day."""
    n = (now or _now_ist()).astimezone(_IST)
    d = n.date()
    if is_trading_day(d) and n.time() >= _NSE_OPEN:
        return d
    return _prev_session(d)


def last_closed_session(now: Optional[_dt.datetime] = None) -> _dt.date:
    """Most recent session whose closing bar is final. Forward-return targets
    after this date aren't reached yet (a same-day target during market hours
    would otherwise read an intraday, still-moving bar)."""
    n = (now or _now_ist()).astimezone(_IST)
    d = n.date()
    if is_trading_day(d) and n.time() >= _NSE_CLOSE:
        return d
    return _prev_session(d)

_schema_ready_for: Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
# Schema
# ─────────────────────────────────────────────────────────────────────────────

def _db_key() -> str:
    return _store._database_url() or _store._SQLITE_PATH


def ensure_schema() -> None:
    """Create verdict_log + verdict_forward_returns if absent. Idempotent."""
    global _schema_ready_for
    key = _db_key()
    if _schema_ready_for == key:
        return

    is_pg = _store._is_pg()
    pk_line = "id SERIAL PRIMARY KEY" if is_pg else "id INTEGER PRIMARY KEY AUTOINCREMENT"
    real = "DOUBLE PRECISION" if is_pg else "REAL"
    integer = "INTEGER" if is_pg else "INTEGER"

    with _store._get_conn() as conn:
        cur = conn.cursor()
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS verdict_log (
                {pk_line},
                logged_at         TEXT     NOT NULL,
                logged_date       TEXT     NOT NULL,
                ticker            TEXT     NOT NULL,
                entry_price       {real},
                verdict           TEXT,
                conviction        {integer},
                confidence        TEXT,
                horizon           TEXT,
                composite_score   {real},
                composite_action  TEXT,
                tqs               {real},
                valuation_posture TEXT,
                thesis_verdict    TEXT,
                thesis_score      {integer},
                quality_score     {real},
                quality_flags     TEXT,
                limiting_gate     TEXT,
                primary_reason    TEXT,
                source            TEXT     NOT NULL
            )
        """)
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS ux_verdict_log_daily
            ON verdict_log(logged_date, ticker, horizon, source)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS ix_verdict_log_ticker
            ON verdict_log(ticker)
        """)
        # Signal-tag ledger — one row per (verdict_log_id, tag). Lets us grade
        # each individual sub-signal (RSI oversold, volume surge, BullEngulfing
        # pattern, tech_high bucket…) independently of the verdict label, with
        # its own Wilson-lower win rate and mean forward return.
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS verdict_signal_tags (
                verdict_log_id {integer} NOT NULL,
                tag            TEXT      NOT NULL,
                PRIMARY KEY (verdict_log_id, tag)
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS ix_verdict_signal_tags_tag
            ON verdict_signal_tags(tag)
        """)
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS verdict_forward_returns (
                verdict_log_id    {integer} PRIMARY KEY,
                checked_at        TEXT,
                price_1d    {real}, ret_1d    {real}, nifty_ret_1d    {real},
                price_5d    {real}, ret_5d    {real}, nifty_ret_5d    {real},
                price_20d   {real}, ret_20d   {real}, nifty_ret_20d   {real},
                price_60d   {real}, ret_60d   {real}, nifty_ret_60d   {real},
                price_250d  {real}, ret_250d  {real}, nifty_ret_250d  {real}
            )
        """)
        # Additive migration: backfill_misses counts backfill passes that
        # found this row's price history dead (see _MAX_BACKFILL_MISSES).
        if is_pg:
            cur.execute("ALTER TABLE verdict_log "
                        "ADD COLUMN IF NOT EXISTS backfill_misses INTEGER DEFAULT 0")
        else:
            cur.execute("PRAGMA table_info(verdict_log)")
            if "backfill_misses" not in {row[1] for row in cur.fetchall()}:
                try:
                    cur.execute("ALTER TABLE verdict_log "
                                "ADD COLUMN backfill_misses INTEGER DEFAULT 0")
                except Exception as e:   # another connection added it first
                    if "duplicate column" not in str(e).lower():
                        raise
        conn.commit()
    _schema_ready_for = key


# ─────────────────────────────────────────────────────────────────────────────
# Write path — log_verdict()
# ─────────────────────────────────────────────────────────────────────────────

def log_verdict(*,
                ticker:          str,
                final_verdict:   Any,          # analysis.final_verdict.FinalVerdict
                entry_price:     Optional[float] = None,
                composite_score: Optional[float] = None,   # FV doesn't expose it
                thesis_score:    Optional[int]   = None,   # ditto
                signal_tags:     Optional[List[str]] = None,
                source:          str = "analyze_page",
                horizon:         Optional[str]  = None,
                ) -> Optional[int]:
    """
    Persist ONE verdict row. Idempotent per (logged_date, ticker, horizon, source):
    calling twice on the same day for the same ticker is a no-op.

    Every failure is caught and logged — the ledger is a background concern
    and must NEVER surface an exception in the user-facing page render.
    Returns the row id on insert, None on dedup or any failure.
    """
    try:
        ensure_schema()
        fv = final_verdict
        if fv is None:
            return None
        now = _now_ist()
        labels = getattr(fv, "subsystem_labels", {}) or {}

        # Extract the raw subsystem numbers from labels where the FinalVerdict
        # exposes them (as strings like "73/90"); we store the raw number so
        # calibration groupers can bucket cleanly.
        def _num_before(sep: str, s: Any) -> Optional[float]:
            try:
                return float(str(s).split(sep, 1)[0])
            except Exception:
                return None

        tqs_num = _num_before("/", labels.get("tqs"))
        qual_num = _num_before("/", labels.get("quality"))

        # thesis_score lives on the FinalVerdict indirectly via the message —
        # we don't have it as a field, so we leave it None here. The banner
        # caller CAN pass it via subsystem_labels if desired; the ledger
        # doesn't require it for calibration to work.
        row = (
            now.replace(tzinfo=None).isoformat(timespec="seconds"),   # IST, naive like old rows
            session_date(now).isoformat(),
            ticker,
            float(entry_price) if entry_price is not None else None,
            str(getattr(fv, "verdict", "") or "") or None,
            int(getattr(fv, "conviction", 0) or 0),
            str(getattr(fv, "confidence", "") or "") or None,
            (horizon or getattr(fv, "horizon", "medium") or "medium"),
            float(composite_score) if composite_score is not None else None,
            labels.get("composite"),
            tqs_num,
            labels.get("valuation"),
            labels.get("thesis"),
            int(thesis_score) if thesis_score is not None else None,
            qual_num,
            labels.get("flags"),
            getattr(fv, "limiting_gate", None),
            (getattr(fv, "primary_reason", "") or "")[:500] or None,
            source,
        )
        cols = ("logged_at,logged_date,ticker,entry_price,verdict,conviction,"
                "confidence,horizon,composite_score,composite_action,tqs,"
                "valuation_posture,thesis_verdict,thesis_score,quality_score,"
                "quality_flags,limiting_gate,primary_reason,source")
        placeholders = ",".join(["?"] * 19)

        with _store._get_conn() as conn:
            cur = conn.cursor()
            try:
                if _store._is_pg():
                    cur.execute(_store._q(
                        f"INSERT INTO verdict_log ({cols}) VALUES ({placeholders}) "
                        f"ON CONFLICT (logged_date, ticker, horizon, source) DO NOTHING "
                        f"RETURNING id"), row)
                    got = cur.fetchone()
                    new_id = int(got[0]) if got else None
                else:
                    cur.execute(
                        f"INSERT OR IGNORE INTO verdict_log ({cols}) VALUES ({placeholders})",
                        row)
                    new_id = int(cur.lastrowid) if cur.lastrowid else None
                # Persist the signal tags associated with this verdict (if any).
                # PRIMARY KEY (verdict_log_id, tag) → duplicate tag inserts are
                # no-ops. Only fired when we actually got a new_id (dedup case
                # would insert against a stale id).
                if new_id and signal_tags:
                    for _tag in {str(t)[:64] for t in signal_tags if t}:
                        try:
                            cur.execute(_store._q(
                                "INSERT INTO verdict_signal_tags (verdict_log_id, tag) "
                                "VALUES (?, ?)"), (new_id, _tag))
                        except Exception:
                            pass    # duplicate — safe to ignore
                conn.commit()
                return new_id
            except Exception as e:
                # De-duplication collisions & schema drift both land here.
                # Never a page-level error — just log and move on.
                _log.debug("log_verdict(%s, source=%s) insert failed: %s",
                           ticker, source, e)
                return None
    except Exception as e:
        _log.debug("log_verdict(%s) outer failed: %s: %s",
                   ticker, type(e).__name__, e)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Read path
# ─────────────────────────────────────────────────────────────────────────────

def load_ledger(*, ticker: Optional[str] = None,
                source: Optional[str] = None,
                limit: int = 500) -> pd.DataFrame:
    """
    Load the ledger, joined with any forward returns already computed.
    Returns a fresh empty DataFrame on any error — never raises.
    """
    try:
        ensure_schema()
        where, params = [], []
        if ticker:
            where.append("v.ticker = ?"); params.append(ticker)
        if source:
            where.append("v.source = ?"); params.append(source)
        wsql = ("WHERE " + " AND ".join(where)) if where else ""
        sql = _store._q(f"""
            SELECT v.*,
                   r.checked_at,
                   r.price_1d,   r.ret_1d,   r.nifty_ret_1d,
                   r.price_5d,   r.ret_5d,   r.nifty_ret_5d,
                   r.price_20d,  r.ret_20d,  r.nifty_ret_20d,
                   r.price_60d,  r.ret_60d,  r.nifty_ret_60d,
                   r.price_250d, r.ret_250d, r.nifty_ret_250d
            FROM verdict_log v
            LEFT JOIN verdict_forward_returns r ON r.verdict_log_id = v.id
            {wsql}
            ORDER BY v.logged_at DESC
            LIMIT ?
        """)
        params.append(int(limit))
        with _store._get_conn() as conn:
            return read_sql_df(sql, conn, params=tuple(params))
    except Exception as e:
        _log.warning("load_ledger failed: %s: %s", type(e).__name__, e)
        return pd.DataFrame()


# ─────────────────────────────────────────────────────────────────────────────
# Backfill forward returns — lazy, called from the Calibration page
# ─────────────────────────────────────────────────────────────────────────────

def _find_bar(df: pd.DataFrame, target: _dt.date) -> Optional[float]:
    """Return the first Close on-or-after `target` (skips weekends/holidays)."""
    if df is None or df.empty:
        return None
    # fetcher returns DatetimeIndex; normalize to date for the search
    try:
        idx_dates = pd.Series(df.index.date, index=df.index)
    except Exception:
        return None
    mask = idx_dates >= target
    if not mask.any():
        return None
    row = df.loc[mask].iloc[0]
    try:
        return float(row["Close"])
    except (KeyError, ValueError, TypeError):
        return None


def _fetch_history(ticker: str) -> Optional[pd.DataFrame]:
    """One-year history through today. Errors → None (silent skip)."""
    try:
        from data.fetcher import fetch_single
        # 2y: a 250-trading-day horizon needs more than a calendar year back.
        df = fetch_single(ticker, period="2y", interval="1d")
        if df is None or df.empty:
            return None
        # Normalize index to DatetimeIndex ordered ascending
        df = df.sort_index()
        return df
    except Exception as e:
        _log.debug("_fetch_history(%s) failed: %s", ticker, e)
        return None


# Upper bound on candidate rows pulled per backfill call (ids + dates only).
_BACKFILL_CANDIDATE_CAP = 20000

# A row stops being retried after this many backfill passes found its price
# history dead (delisted / renamed ticker): no history at all, no bar on or
# after the logged date, or a last bar _STALE_BEHIND_NIFTY_SESSIONS+ sessions
# older than NIFTY's. Misses are only counted while the NIFTY fetch works, so
# a network outage never burns retries; the session gap absorbs the EOD lag
# and a slower provider tier serving the ticker a session behind NIFTY (a
# delisted name falls weeks behind). Any successful fill resets the count.
_MAX_BACKFILL_MISSES = 5
_STALE_BEHIND_NIFTY_SESSIONS = 3


def backfill_returns(*, max_rows: int = 200) -> Dict[str, int]:
    """
    Fill missing forward-return columns for eligible log rows.

    A row is eligible for horizon H if:
      (today - logged_date) >= H days AND that horizon's ret column is NULL.

    We process at most max_rows log rows per call to keep the page render
    snappy. Rows not touched this call are picked up on the next Calibration
    page visit.

    Returns {"scanned": n, "updated": n, "tickers_fetched": n}.
    """
    stats = {"scanned": 0, "updated": 0, "tickers_fetched": 0}
    try:
        ensure_schema()
        today = last_closed_session()

        # Load rows needing work: any horizon column still None, logged before
        # the last closed session (a row from that session can't have reached
        # even the 1-session horizon).
        #
        # FIX LEDGER-STARVE (2026-09-28): this used to take the max_rows
        # NEWEST rows needing work. Scan pages log 30-60 rows a day, so the
        # newest 200 were always too recent for any horizon — every page load
        # updated 0 rows while ~430 eligible older rows never got 1d/5d
        # returns. max_rows now applies only AFTER keeping rows with at least
        # one missing horizon that has actually been reached.
        sql = _store._q("""
            SELECT v.id, v.ticker, v.logged_date, v.entry_price, v.backfill_misses,
                   r.price_1d, r.price_5d, r.price_20d, r.price_60d, r.price_250d
            FROM verdict_log v
            LEFT JOIN verdict_forward_returns r ON r.verdict_log_id = v.id
            WHERE v.logged_date < ?
              AND COALESCE(v.backfill_misses, 0) < ?
              AND (r.verdict_log_id IS NULL
               OR r.price_1d   IS NULL
               OR r.price_5d   IS NULL
               OR r.price_20d  IS NULL
               OR r.price_60d  IS NULL
               OR r.price_250d IS NULL)
            ORDER BY v.logged_at DESC
            LIMIT ?
        """)
        with _store._get_conn() as conn:
            rows = read_sql_df(sql, conn, params=(today.isoformat(), _MAX_BACKFILL_MISSES,
                                                  _BACKFILL_CANDIDATE_CAP))
        if rows.empty:
            return stats

        def _has_reached_gap(r) -> bool:
            try:
                logged = _dt.date.fromisoformat(str(r["logged_date"]))
            except ValueError:
                return False
            return any(
                pd.isna(r.get(f"price_{h}d")) and horizon_target_date(logged, h) <= today
                for h in HORIZONS_DAYS
            )

        rows = rows[rows.apply(_has_reached_gap, axis=1)].head(max_rows)
        if rows.empty:
            return stats
        stats["scanned"] = len(rows)

        # Fetch history once per unique ticker (Nifty benchmark always fetched).
        tickers = sorted(set(rows["ticker"].tolist()) | {NIFTY_BENCH})
        hist: Dict[str, Optional[pd.DataFrame]] = {}
        for t in tickers:
            hist[t] = _fetch_history(t)
            if hist[t] is not None:
                stats["tickers_fetched"] += 1
        nifty_df = hist.get(NIFTY_BENCH)
        # Only judge a ticker dead while the data source is demonstrably up.
        nifty_last = _last_bar_date(nifty_df)
        misses: List[int] = []
        resets: List[int] = []

        # Compute + upsert one row at a time.
        for _, r in rows.iterrows():
            try:
                log_id = int(r["id"])
                ticker = str(r["ticker"])
                logged = _dt.date.fromisoformat(str(r["logged_date"]))
                entry = r["entry_price"]

                tdf = hist.get(ticker)
                if tdf is None:
                    if nifty_last is not None:
                        misses.append(log_id)
                    continue

                # Anchor entry to the logged_date bar if we don't have it.
                if entry is None or pd.isna(entry):
                    entry = _find_bar(tdf, logged)
                if entry is None or entry <= 0:
                    if nifty_last is not None:
                        misses.append(log_id)
                    continue

                nifty_entry = _find_bar(nifty_df, logged) if nifty_df is not None else None

                updates: Dict[str, Optional[float]] = {}
                for h in HORIZONS_DAYS:
                    target = horizon_target_date(logged, h)
                    if target > today:
                        continue     # horizon not yet reached
                    # Skip horizons already filled (row-existence per horizon)
                    col = f"price_{h}d"
                    if col in r and pd.notna(r[col]):
                        continue
                    price = _find_bar(tdf, target)
                    if price is None:
                        continue
                    ret = (price / entry - 1.0) * 100.0
                    updates[f"price_{h}d"] = round(float(price), 4)
                    updates[f"ret_{h}d"]   = round(float(ret),   4)
                    if nifty_entry and nifty_df is not None:
                        n_price = _find_bar(nifty_df, target)
                        if n_price is not None and nifty_entry > 0:
                            n_ret = (n_price / nifty_entry - 1.0) * 100.0
                            updates[f"nifty_ret_{h}d"] = round(float(n_ret), 4)

                if not updates:
                    # A reached horizon with no bar: dead only if this ticker's
                    # history stops before NIFTY's (not just the EOD lag).
                    t_last = _last_bar_date(tdf)
                    if (nifty_last is not None and t_last is not None
                            and horizon_target_date(t_last, _STALE_BEHIND_NIFTY_SESSIONS) <= nifty_last):
                        misses.append(log_id)
                    continue
                _upsert_returns(log_id, updates)
                stats["updated"] += 1
                if pd.notna(r.get("backfill_misses")) and int(r["backfill_misses"]) > 0:
                    resets.append(log_id)
            except Exception as e:
                _log.debug("backfill row failed id=%s: %s", r.get("id"), e)
                continue
        _record_misses(misses)
        _record_misses(resets, reset=True)
    except Exception as e:
        _log.warning("backfill_returns failed: %s: %s", type(e).__name__, e)
    return stats


def _last_bar_date(df: Optional[pd.DataFrame]) -> Optional[_dt.date]:
    """Date of the last bar in a fetched history, or None."""
    if df is None or df.empty:
        return None
    try:
        return pd.Timestamp(df.index[-1]).date()
    except Exception:
        return None


def _record_misses(log_ids: List[int], reset: bool = False) -> None:
    """Bump backfill_misses for rows whose history looked dead this pass, or
    (reset=True) zero it for rows that just filled a horizon."""
    if not log_ids:
        return
    placeholders = ",".join(["?"] * len(log_ids))
    new_val = "0" if reset else "COALESCE(backfill_misses, 0) + 1"
    try:
        with _store._get_conn() as conn:
            cur = conn.cursor()
            cur.execute(_store._q(
                f"UPDATE verdict_log SET backfill_misses = {new_val} "
                f"WHERE id IN ({placeholders})"), tuple(log_ids))
            conn.commit()
    except Exception as e:
        _log.debug("_record_misses failed for %d rows: %s", len(log_ids), e)


def _upsert_returns(log_id: int, updates: Dict[str, Optional[float]]) -> None:
    """Insert-or-update verdict_forward_returns for one log row."""
    with _store._get_conn() as conn:
        cur = conn.cursor()
        cur.execute(_store._q(
            "SELECT verdict_log_id FROM verdict_forward_returns WHERE verdict_log_id=?"),
            (log_id,))
        exists = cur.fetchone() is not None
        now = _dt.datetime.now().isoformat(timespec="seconds")
        if not exists:
            cols = ["verdict_log_id", "checked_at"] + list(updates.keys())
            vals = [log_id, now] + list(updates.values())
            placeholders = ",".join(["?"] * len(cols))
            cur.execute(_store._q(
                f"INSERT INTO verdict_forward_returns ({','.join(cols)}) "
                f"VALUES ({placeholders})"), tuple(vals))
        else:
            set_clause = ", ".join([f"{k}=?" for k in updates.keys()] + ["checked_at=?"])
            vals = list(updates.values()) + [now, log_id]
            cur.execute(_store._q(
                f"UPDATE verdict_forward_returns SET {set_clause} "
                f"WHERE verdict_log_id=?"), tuple(vals))
        conn.commit()


# ─────────────────────────────────────────────────────────────────────────────
# Calibration aggregates
# ─────────────────────────────────────────────────────────────────────────────

def _wilson_lower(wins: int, n: int, z: float = 1.96) -> float:
    """Wilson lower-bound on a binomial proportion. Returns 0.0 for n=0."""
    if n <= 0:
        return 0.0
    p = wins / n
    denom = 1 + z*z / n
    centre = p + z*z / (2*n)
    margin = z * ((p*(1 - p)/n + z*z/(4*n*n)) ** 0.5)
    return max(0.0, (centre - margin) / denom)


def calibration_by(*, group_col: str, horizon_days: int,
                   source: Optional[str] = None) -> pd.DataFrame:
    """
    Return one row per bucket with n / mean-return / win-rate / alpha /
    Wilson lower bound. Missing (unfilled) rows are excluded so the stats
    reflect only calls that have had time to play out.
    """
    df = load_ledger(source=source, limit=5000)
    if df.empty:
        return pd.DataFrame()
    ret_col   = f"ret_{horizon_days}d"
    nifty_col = f"nifty_ret_{horizon_days}d"
    if ret_col not in df.columns:
        return pd.DataFrame()
    df = df[df[ret_col].notna()].copy()
    if df.empty:
        return pd.DataFrame()
    n_ret = df[nifty_col] if nifty_col in df.columns else pd.Series(np.nan, index=df.index)
    out = _aggregate(df.rename(columns={ret_col: "ret_val"}).assign(n_ret_val=n_ret),
                     group_col=group_col, id_col="id")
    return out.sort_values("n", ascending=False)


def _aggregate(df: pd.DataFrame, *, group_col: str, id_col: str) -> pd.DataFrame:
    """Shared calibration aggregation over columns ret_val / n_ret_val.

    FIX LEDGER-ALPHA (2026-09-24):
      * alpha = ret - nifty_ret ONLY when the Nifty return exists; otherwise
        alpha is NaN ("unavailable") instead of silently falling back to the
        raw return (the old `.fillna(0)`), which credited market moves as
        skill. `n_alpha` counts rows with a usable alpha.
      * two win rates, labelled distinctly:
          win_rate      — raw: forward return > 0
          win_vs_nifty  — alpha > 0 (beat the index), over n_alpha rows
        each with its own Wilson lower bound.
    """
    df = df.copy()
    df["alpha"] = df["ret_val"] - df["n_ret_val"]          # NaN when Nifty missing
    df["is_win"] = (df["ret_val"] > 0).astype(int)
    df["has_alpha"] = df["alpha"].notna().astype(int)
    df["is_alpha_win"] = (df["alpha"] > 0).astype(int)     # NaN > 0 is False
    out = df.groupby(group_col, dropna=False).agg(
        n=(id_col, "count"),
        mean_ret=("ret_val", "mean"),
        median_ret=("ret_val", "median"),
        mean_alpha=("alpha", "mean"),                       # skips NaN
        n_alpha=("has_alpha", "sum"),
        win_rate=("is_win", "mean"),
        wins=("is_win", "sum"),
        alpha_wins=("is_alpha_win", "sum"),
    ).reset_index()
    out["wilson_lower_win"] = out.apply(
        lambda r: _wilson_lower(int(r["wins"]), int(r["n"])), axis=1)
    out["win_vs_nifty"] = out.apply(
        lambda r: (r["alpha_wins"] / r["n_alpha"]) if r["n_alpha"] > 0 else np.nan, axis=1)
    out["wilson_lower_vs_nifty"] = out.apply(
        lambda r: _wilson_lower(int(r["alpha_wins"]), int(r["n_alpha"]))
        if r["n_alpha"] > 0 else np.nan, axis=1)
    out["mean_ret"] = out["mean_ret"].round(2)
    out["median_ret"] = out["median_ret"].round(2)
    out["mean_alpha"] = out["mean_alpha"].round(2)
    out["win_rate"] = (out["win_rate"] * 100).round(1)
    out["wilson_lower_win"] = (out["wilson_lower_win"] * 100).round(1)
    out["win_vs_nifty"] = (out["win_vs_nifty"].astype(float) * 100).round(1)
    out["wilson_lower_vs_nifty"] = (out["wilson_lower_vs_nifty"].astype(float) * 100).round(1)
    out["n_alpha"] = out["n_alpha"].astype(int)
    return out.drop(columns=["alpha_wins"])


def tag_calibration(*, horizon_days: int, min_n: int = 5) -> pd.DataFrame:
    """
    Per-signal-tag win rate + mean forward return at `horizon_days`.

    Joins verdict_signal_tags → verdict_log → verdict_forward_returns so
    the same forward-return numbers already computed for verdict-level
    calibration are re-used per tag. Filters to tags with n >= min_n
    to avoid noisy rankings on singleton signals.
    """
    ensure_schema()
    ret_col   = f"ret_{horizon_days}d"
    nifty_col = f"nifty_ret_{horizon_days}d"
    try:
        sql = _store._q(f"""
            SELECT t.tag,
                   v.id AS log_id,
                   r.{ret_col}   AS ret_val,
                   r.{nifty_col} AS n_ret_val
            FROM verdict_signal_tags t
            JOIN verdict_log v ON v.id = t.verdict_log_id
            JOIN verdict_forward_returns r ON r.verdict_log_id = v.id
            WHERE r.{ret_col} IS NOT NULL
        """)
        with _store._get_conn() as conn:
            df = read_sql_df(sql, conn)
    except Exception as e:
        _log.warning("tag_calibration failed: %s: %s", type(e).__name__, e)
        return pd.DataFrame()

    if df.empty:
        return df
    agg = _aggregate(df, group_col="tag", id_col="log_id")
    agg = agg[agg["n"] >= min_n]
    return agg.sort_values("wilson_lower_win", ascending=False)


def shadow_pnl(*, horizon_days: int = 20) -> pd.DataFrame:
    """
    'What would you have made if you'd taken every BUY/STRONG BUY the model
    emitted?' Filters shadow-source rows with a positive-side verdict and a
    computed forward return for the horizon.
    """
    df = load_ledger(source="shadow_auto", limit=5000)
    if df.empty:
        return df
    df = df[df["verdict"].isin(["BUY", "STRONG BUY"])].copy()
    ret_col   = f"ret_{horizon_days}d"
    nifty_col = f"nifty_ret_{horizon_days}d"
    if ret_col not in df.columns:
        return pd.DataFrame()
    df = df[df[ret_col].notna()].copy()
    if df.empty:
        return df
    # FIX LEDGER-ALPHA: alpha is NaN (unavailable) when Nifty return is missing.
    df["alpha"] = df[ret_col] - (df[nifty_col] if nifty_col in df.columns else np.nan)
    keep =["logged_date", "ticker", "verdict", "conviction", "horizon",
            "entry_price", f"price_{horizon_days}d", ret_col, nifty_col, "alpha",
            "primary_reason"]
    keep = [c for c in keep if c in df.columns]
    return df[keep].rename(columns={
        f"price_{horizon_days}d": f"exit_{horizon_days}d",
        ret_col:                  f"return_{horizon_days}d_pct",
        nifty_col:                f"nifty_{horizon_days}d_pct",
    }).sort_values("logged_date", ascending=False)
