"""
trade_store.py — Storage backend for paper trades and user settings.

Two backends, chosen automatically:
  • SQLite (default)  — local file `trades.db`. Works out of the box, but
    Streamlit Cloud's disk is EPHEMERAL — trades reset on every redeploy.
  • Postgres (opt-in) — set DATABASE_URL and trades survive redeploys.
    Use a free Neon (neon.tech) or Supabase (supabase.com) instance.
    See dashboard/DB_SETUP.md for the 5-minute setup.

Fixes applied vs previous version:
  - _database_url() deliberately NOT cached — tests swap DATABASE_URL between runs
  - _schema_ready / _kv_ready flags — ensure_schema() no longer opens a second
    connection on every read (was opening 2 connections per operation)
  - Postgres connection pool (ThreadedConnectionPool, max 5) — replaces one new
    connection per call which exhausts Neon/Supabase free tier under load
  - user_id column added to user_kv — all users were sharing the same KV namespace,
    overwriting each other's watchlists and settings
  - rename_account / delete_account now call ensure_schema() (were skipping it)
  - fetch_open no-account path now uses _q() consistently
  - edit_trade cursor assigned to variable (was anonymous — risk of GC before commit)
  - open_trade now accepts strategy and action params (were hardcoded "Manual"/"BUY")
  - open_trade validates price > 0, qty > 0, sl < price
  - _get_conn() context manager centralises connection acquire/release
  - _get_conn() waits for a free pool slot instead of raising PoolError when
    scan threads outnumber pool connections (2026-09-27)
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import threading
from contextlib import contextmanager
from typing import Any, List, Optional

import pandas as pd

_log = logging.getLogger("trade_store")
_SQLITE_PATH = "trades.db"

# ── Schema-ready flags — track which DB path the schema was created for ───────
# Storing the path (not just a bool) means tests that swap _SQLITE_PATH to a
# temp file correctly trigger a re-run of the DDL for that new path.
_schema_ready_for: Optional[str] = None
_kv_ready_for:     Optional[str] = None

# ── Postgres connection pool (created once, reused across calls) ──────────────
# psycopg2's ThreadedConnectionPool raises PoolError the moment all maxconn
# connections are out — it never waits. Universe scans run 10+ worker threads
# (ThreadPoolExecutor in dashboard/shared/cache.py) that each read delivery /
# F&O history, so a bare pool failed most tickers and silently dropped the
# delivery sub-score on scan pages. _pg_slots (a semaphore sized to maxconn)
# makes callers queue for a free connection instead.
_PG_MAXCONN = 5
_PG_ACQUIRE_TIMEOUT_S = 30.0
_pg_pool = None
_pg_slots: Optional[threading.BoundedSemaphore] = None
_pg_pool_lock = threading.Lock()
_pg_held = threading.local()   # per-thread count of connections checked out


# ─────────────────────────────────────────────────────────────────────────────
# Backend selection
# ─────────────────────────────────────────────────────────────────────────────

def _database_url() -> Optional[str]:
    """
    Return Postgres URL from Streamlit secrets or env, else None.
    Not cached with lru_cache — tests monkeypatch DATABASE_URL between runs
    and need live re-reads. Fast enough: called only at connection time.
    """
    url = None
    try:
        import streamlit as st
        secrets = getattr(st, "secrets", None)
        if secrets is not None:
            try:
                url = secrets["database"]["url"]
            except Exception as e:
                url = None
                _log.debug("trade_store._database_url: secrets['database']['url'] unavailable: %s", e)
            if not url:
                try:
                    url = secrets["DATABASE_URL"]
                except Exception as e:
                    url = None
                    _log.debug("trade_store._database_url: secrets['DATABASE_URL'] unavailable: %s", e)
    except Exception as e:
        url = None
        _log.debug("trade_store._database_url: st.secrets access failed: %s", e)
    return (url or os.environ.get("DATABASE_URL")) or None


def backend_name() -> str:
    return "postgres" if _database_url() else "sqlite"


def _is_pg() -> bool:
    return bool(_database_url())


def _get_pg_pool():
    """Return (and lazily create) a threaded Postgres connection pool."""
    global _pg_pool, _pg_slots
    if _pg_pool is None:
        with _pg_pool_lock:   # scan threads race here on first use
            if _pg_pool is None:
                from psycopg2 import pool as pg_pool_mod
                _pg_slots = threading.BoundedSemaphore(_PG_MAXCONN)
                _pg_pool = pg_pool_mod.ThreadedConnectionPool(1, _PG_MAXCONN, _database_url())
    return _pg_pool


@contextmanager
def _get_conn():
    """
    Context manager that yields a DB connection and releases it on exit.
    For Postgres: waits (up to _PG_ACQUIRE_TIMEOUT_S) for a free pool slot,
    borrows a connection and returns it. A nested _get_conn() on a thread that
    already holds one skips the wait — blocking there could deadlock the
    thread against itself — and hits the pool directly, as before.
    For SQLite: opens a file connection and closes it.
    """
    if _is_pg():
        pool = _get_pg_pool()
        slots = _pg_slots
        depth = getattr(_pg_held, "n", 0)
        gated = depth == 0 and slots is not None
        if gated and not slots.acquire(timeout=_PG_ACQUIRE_TIMEOUT_S):
            from psycopg2.pool import PoolError
            raise PoolError(
                f"no Postgres connection free after {_PG_ACQUIRE_TIMEOUT_S:.0f}s "
                f"(pool max {_PG_MAXCONN})"
            )
        try:
            conn = pool.getconn()
        except BaseException:
            if gated:
                slots.release()
            raise
        _pg_held.n = depth + 1
        try:
            yield conn
        finally:
            _pg_held.n = depth
            try:
                pool.putconn(conn)
            finally:
                if gated:
                    slots.release()
    else:
        import sqlite3
        conn = sqlite3.connect(_SQLITE_PATH)
        try:
            yield conn
        finally:
            conn.close()


def _q(sql: str) -> str:
    """Translate '?' placeholders to '%s' on Postgres; leave as-is on SQLite."""
    return sql.replace("?", "%s") if _is_pg() else sql


# read_sql_df lives in the pure utils.sql module; re-exported here for callers
# that already use ``_store.read_sql_df``.
from utils.sql import read_sql_df  # noqa: E402,F401


# ─────────────────────────────────────────────────────────────────────────────
# Startup validation
# ─────────────────────────────────────────────────────────────────────────────

def validate_persistence() -> dict:
    """
    Startup check: is the DB reachable and schema valid?
    Returns a structured status dict. Never raises.
    """
    backend   = backend_name()
    ephemeral = (backend == "sqlite")
    status    = {
        "backend":       backend,
        "db_url_present": bool(_database_url()),
        "reachable":     False,
        "schema_ok":     False,
        "ephemeral":     ephemeral,
        "warnings":      [],
        "error":         None,
    }
    if ephemeral:
        status["warnings"].append(
            "SQLite backend: storage is EPHEMERAL on Streamlit Cloud — paper trades, "
            "watchlist and saved settings RESET on every redeploy. Set DATABASE_URL "
            "(Postgres) to persist them. See docs/DEPLOYMENT_CHECKLIST.md."
        )
    try:
        ensure_schema()
        _kv_ensure()
        with _get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM trades LIMIT 1")
            cur.execute("SELECT 1 FROM user_kv LIMIT 1")
        status["reachable"] = True
        status["schema_ok"] = True
    except Exception as e:
        status["error"] = f"{type(e).__name__}: {e}"
        status["warnings"].append(
            f"Database not reachable / schema invalid ({backend}): {e}. "
            "Reads will return empty and writes will fail."
        )
        _log.error("validate_persistence failed (%s): %s", backend, e)
    return status


# ─────────────────────────────────────────────────────────────────────────────
# Schema
# ─────────────────────────────────────────────────────────────────────────────

def ensure_schema() -> None:
    """Create the trades table if needed. Skips if already run for this DB path."""
    global _schema_ready_for
    db_key = _database_url() or _SQLITE_PATH
    if _schema_ready_for == db_key:
        return

    with _get_conn() as conn:
        cur = conn.cursor()
        if _is_pg():
            cur.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id          SERIAL PRIMARY KEY,
                    account     TEXT    NOT NULL DEFAULT 'My Account',
                    ticker      TEXT    NOT NULL,
                    strategy    TEXT    NOT NULL DEFAULT 'Manual',
                    action      TEXT    NOT NULL DEFAULT 'BUY',
                    price       DOUBLE PRECISION NOT NULL,
                    quantity    INTEGER NOT NULL,
                    sl          DOUBLE PRECISION,
                    tp          DOUBLE PRECISION,
                    trail_stop  DOUBLE PRECISION,
                    capital     DOUBLE PRECISION,
                    reason      TEXT,
                    timestamp   TEXT    NOT NULL,
                    status      TEXT    DEFAULT 'OPEN',
                    exit_price  DOUBLE PRECISION,
                    exit_reason TEXT,
                    exit_time   TEXT,
                    pnl         DOUBLE PRECISION,
                    pnl_pct     DOUBLE PRECISION
                )
            """)
        else:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    account     TEXT    NOT NULL DEFAULT 'My Account',
                    ticker      TEXT    NOT NULL,
                    strategy    TEXT    NOT NULL DEFAULT 'Manual',
                    action      TEXT    NOT NULL DEFAULT 'BUY',
                    price       REAL    NOT NULL,
                    quantity    INTEGER NOT NULL,
                    sl          REAL,
                    tp          REAL,
                    trail_stop  REAL,
                    capital     REAL,
                    reason      TEXT,
                    timestamp   TEXT    NOT NULL,
                    status      TEXT    DEFAULT 'OPEN',
                    exit_price  REAL,
                    exit_reason TEXT,
                    exit_time   TEXT,
                    pnl         REAL,
                    pnl_pct     REAL
                )
            """)
            # Migration: older DBs may lack account column
            cols = [r[1] for r in cur.execute("PRAGMA table_info(trades)").fetchall()]
            if "account" not in cols:
                cur.execute(
                    "ALTER TABLE trades ADD COLUMN account TEXT NOT NULL DEFAULT 'My Account'"
                )
        conn.commit()

    _schema_ready_for = db_key


# ─────────────────────────────────────────────────────────────────────────────
# Accounts
# ─────────────────────────────────────────────────────────────────────────────

def list_accounts() -> List[str]:
    ensure_schema()
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT account FROM trades ORDER BY account")
        names = [r[0] for r in cur.fetchall() if r[0]]
    return names if names else ["My Account"]


def rename_account(old_name: str, new_name: str) -> None:
    ensure_schema()   # was missing — would crash on a fresh DB
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute(_q("UPDATE trades SET account=? WHERE account=?"), (new_name, old_name))
        conn.commit()


def delete_account(name: str) -> None:
    ensure_schema()   # was missing — would crash on a fresh DB
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute(_q("DELETE FROM trades WHERE account=?"), (name,))
        conn.commit()


# ─────────────────────────────────────────────────────────────────────────────
# Trades
# ─────────────────────────────────────────────────────────────────────────────

def open_trade(
    ticker:   str,
    price:    float,
    qty:      int,
    sl:       float,
    tp:       float,
    reason:   str = "",
    account:  str = "My Account",
    strategy: str = "Manual",       # was hardcoded — now a param
    action:   str = "BUY",          # was hardcoded — now accepts SELL/SHORT too
) -> int:
    """
    Record a new paper trade. Returns the new trade ID.
    Raises ValueError on invalid inputs (price=0, qty=0, sl above entry).
    """
    if price <= 0:
        raise ValueError(f"Invalid entry price: {price}")
    if qty <= 0:
        raise ValueError(f"Invalid quantity: {qty}")
    if sl > 0 and action.upper() == "BUY" and sl >= price:
        raise ValueError(f"Stop loss ({sl}) must be below entry price ({price}) for a BUY trade")

    ensure_schema()
    now  = datetime.datetime.now().isoformat()
    cols = "account,ticker,strategy,action,price,quantity,sl,tp,capital,reason,timestamp"
    vals = (account, ticker, strategy, action.upper(), price, qty, sl, tp, price * qty, reason, now)

    with _get_conn() as conn:
        cur = conn.cursor()
        if _is_pg():
            cur.execute(_q(f"INSERT INTO trades ({cols}) VALUES (?,?,?,?,?,?,?,?,?,?,?) RETURNING id"), vals)
            new_id = cur.fetchone()[0]
        else:
            cur.execute(f"INSERT INTO trades ({cols}) VALUES (?,?,?,?,?,?,?,?,?,?,?)", vals)
            new_id = cur.lastrowid
        conn.commit()

    return int(new_id)


def close_trade(trade_id: int, exit_price: float, reason: str = "Manual close") -> None:
    now = datetime.datetime.now().isoformat()
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute(_q("SELECT price, quantity, action FROM trades WHERE id=?"), (trade_id,))
        row = cur.fetchone()
        if not row:
            return
        entry_price, qty, action = float(row[0]), int(row[1]), str(row[2]).upper()
        # P&L direction depends on trade side
        direction = -1 if action == "SELL" else 1
        pnl       = direction * (exit_price - entry_price) * qty
        pnl_pct   = direction * (exit_price / entry_price - 1) * 100 if entry_price > 0 else 0
        cur.execute(
            _q("UPDATE trades SET status='CLOSED', exit_price=?, exit_time=?, "
               "exit_reason=?, pnl=?, pnl_pct=? WHERE id=?"),
            (exit_price, now, reason, round(pnl, 2), round(pnl_pct, 2), trade_id),
        )
        conn.commit()


def edit_trade(
    trade_id: int,
    sl:       float = None,
    tp:       float = None,
    reason:   str   = None,
) -> None:
    fields, vals = [], []
    if sl is not None:
        fields.append("sl=?")
        vals.append(sl)
    if tp is not None:
        fields.append("tp=?")
        vals.append(tp)
    if reason is not None:
        fields.append("reason=?")
        vals.append(reason)
    if not fields:
        return
    vals.append(trade_id)
    with _get_conn() as conn:
        cur = conn.cursor()   # assigned to variable — prevents GC before commit
        cur.execute(_q(f"UPDATE trades SET {', '.join(fields)} WHERE id=?"), vals)
        conn.commit()


def load_by_account(account: str) -> pd.DataFrame:
    ensure_schema()
    try:
        with _get_conn() as conn:
            return read_sql_df(
                _q("SELECT * FROM trades WHERE account=? ORDER BY id DESC"),
                conn, params=(account,),
            )
    except Exception as e:
        _log.warning("load_by_account(%r) failed: %s", account, e)
        return pd.DataFrame()


def fetch_open(account: str = None) -> pd.DataFrame:
    ensure_schema()
    try:
        with _get_conn() as conn:
            if account:
                return read_sql_df(
                    _q("SELECT * FROM trades WHERE status='OPEN' AND account=? ORDER BY timestamp DESC"),
                    conn, params=(account,),
                )
            # No-account path: now uses _q() consistently
            return read_sql_df(
                _q("SELECT * FROM trades WHERE status='OPEN' ORDER BY timestamp DESC"),
                conn,
            )
    except Exception as e:
        _log.warning("fetch_open(account=%r) failed: %s", account, e)
        return pd.DataFrame()


# ─────────────────────────────────────────────────────────────────────────────
# Key-value store — user settings & watchlist
# ─────────────────────────────────────────────────────────────────────────────

def _kv_ensure() -> None:
    """Create user_kv table if needed. Skips if already run for this DB path."""
    global _kv_ready_for
    db_key = _database_url() or _SQLITE_PATH
    if _kv_ready_for == db_key:
        return
    with _get_conn() as conn:
        cur = conn.cursor()
        if _is_pg():
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_kv (
                    user_id TEXT NOT NULL DEFAULT 'default',
                    k       TEXT NOT NULL,
                    v       TEXT,
                    PRIMARY KEY (user_id, k)
                )
            """)
        else:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_kv (
                    user_id TEXT NOT NULL DEFAULT 'default',
                    k       TEXT NOT NULL,
                    v       TEXT,
                    PRIMARY KEY (user_id, k)
                )
            """)
            # Migration: older single-user DBs had (k TEXT PRIMARY KEY) only
            cols = [r[1] for r in cur.execute("PRAGMA table_info(user_kv)").fetchall()]
            if "user_id" not in cols:
                # SQLite can't ALTER PRIMARY KEY — recreate the table
                cur.execute("ALTER TABLE user_kv RENAME TO user_kv_old")
                cur.execute("""
                    CREATE TABLE user_kv (
                        user_id TEXT NOT NULL DEFAULT 'default',
                        k       TEXT NOT NULL,
                        v       TEXT,
                        PRIMARY KEY (user_id, k)
                    )
                """)
                cur.execute(
                    "INSERT INTO user_kv (user_id, k, v) SELECT 'default', k, v FROM user_kv_old"
                )
                cur.execute("DROP TABLE user_kv_old")
        conn.commit()
    _kv_ready_for = db_key


def kv_get(key: str, default: Any = None, user_id: str = "default") -> Any:
    """
    Read a JSON-serialised setting for a specific user.
    Returns `default` if missing or on failure.
    `user_id` defaults to 'default' — pass st.experimental_user.email for
    per-user isolation on Streamlit Cloud with login enabled.
    """
    try:
        _kv_ensure()
        with _get_conn() as conn:
            cur = conn.cursor()
            cur.execute(_q("SELECT v FROM user_kv WHERE user_id=? AND k=?"), (user_id, key))
            row = cur.fetchone()
            return json.loads(row[0]) if row and row[0] is not None else default
    except Exception as e:
        _log.warning("kv_get(%r, user=%r) failed: %s", key, user_id, e)
        return default


def kv_set(key: str, value: Any, user_id: str = "default") -> bool:
    """
    Upsert a JSON-serialisable setting for a specific user.
    Returns True on success, False on failure (never silent).
    `user_id` defaults to 'default' — single-user setups work unchanged.
    """
    try:
        _kv_ensure()
        payload = json.dumps(value)
        with _get_conn() as conn:
            cur = conn.cursor()
            if _is_pg():
                cur.execute(
                    "INSERT INTO user_kv (user_id, k, v) VALUES (%s, %s, %s) "
                    "ON CONFLICT (user_id, k) DO UPDATE SET v = EXCLUDED.v",
                    (user_id, key, payload),
                )
            else:
                cur.execute(
                    "INSERT OR REPLACE INTO user_kv (user_id, k, v) VALUES (?, ?, ?)",
                    (user_id, key, payload),
                )
            conn.commit()
        return True
    except Exception as e:
        _log.error("kv_set(%r, user=%r) FAILED — setting not persisted: %s", key, user_id, e)
        return False


def kv_delete(key: str, user_id: str = "default") -> bool:
    """Delete a single KV entry. Returns True on success."""
    try:
        _kv_ensure()
        with _get_conn() as conn:
            cur = conn.cursor()
            cur.execute(_q("DELETE FROM user_kv WHERE user_id=? AND k=?"), (user_id, key))
            conn.commit()
        return True
    except Exception as e:
        _log.warning("kv_delete(%r, user=%r) failed: %s", key, user_id, e)
        return False



# Snapshot generated_at helpers — live in utils/snapshot_time.py (pure, so
# callers that stub trade_store in tests can still use them); re-exported here
# because every snapshot reader/writer already imports trade_store.
from utils.snapshot_time import (  # noqa: E402,F401
    parse_snapshot_timestamp,
    snapshot_age_seconds,
    snapshot_timestamp,
)
