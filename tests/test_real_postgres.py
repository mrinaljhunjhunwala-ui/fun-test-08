"""
Real-Postgres checks (opt-in: @pytest.mark.real_postgres + TEST_DATABASE_URL).

Every bug found in the 2026-09-26..30 calibration work reproduced only against
the real database or deployment, never against the fakes/SQLite the rest of
the suite uses: pool exhaustion under scan concurrency, ON CONFLICT dedup,
additive ALTER migrations, TEXT date comparisons, object-dtype NULL columns.
These tests run the same code paths against a throwaway Neon branch
(`ci-tests`, schema only). conftest.py refuses to run them against production.

Every row written carries a unique per-run prefix and is deleted afterwards.
"""
from __future__ import annotations

import datetime as dt
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import pytest

import trade_store
from analysis import verdict_ledger as vl
from analysis.final_verdict import combine
from data import nse_delivery

pytestmark = pytest.mark.real_postgres

RUN = uuid.uuid4().hex[:6].upper()
PFX = f"ZZT{RUN}"                        # symbol prefix for this run's rows


def _sym(i) -> str:
    return f"{PFX}{i}"


def _exec(sql, params=()):
    with trade_store._get_conn() as conn:
        cur = conn.cursor()
        cur.execute(trade_store._q(sql), params)
        conn.commit()


@pytest.fixture(autouse=True)
def _fresh_schema_flags_and_cleanup(monkeypatch):
    # module-level "schema ready" flags would otherwise carry over from SQLite tests
    monkeypatch.setattr(vl, "_schema_ready_for", None)
    monkeypatch.setattr(nse_delivery, "_schema_ready_for", None)
    yield
    # each session gets a fresh schema (conftest), so a test that never touched
    # a table leaves it missing — create before cleaning up
    vl.ensure_schema()
    nse_delivery.ensure_schema()
    like = f"{PFX}%"
    _exec("DELETE FROM verdict_signal_tags WHERE verdict_log_id IN "
          "(SELECT id FROM verdict_log WHERE ticker LIKE ?)", (like,))
    _exec("DELETE FROM verdict_forward_returns WHERE verdict_log_id IN "
          "(SELECT id FROM verdict_log WHERE ticker LIKE ?)", (like,))
    _exec("DELETE FROM verdict_log WHERE ticker LIKE ?", (like,))
    _exec("DELETE FROM nse_delivery_daily WHERE symbol LIKE ?", (like,))


def test_backend_is_postgres():
    assert trade_store._is_pg()
    assert trade_store.backend_name() == "postgres"


# ── schema / migrations ──────────────────────────────────────────────────────

def test_migrations_are_idempotent_and_add_columns(monkeypatch):
    vl.ensure_schema()
    monkeypatch.setattr(vl, "_schema_ready_for", None)
    vl.ensure_schema()                                   # second run: ADD COLUMN IF NOT EXISTS
    with trade_store._get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'verdict_log' AND table_schema = current_schema()")
        log_cols = {r[0] for r in cur.fetchall()}
        cur.execute("SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'verdict_forward_returns' "
                    "AND table_schema = current_schema()")
        fwd_cols = {r[0] for r in cur.fetchall()}
    assert "backfill_misses" in log_cols
    assert {f"sector_ret_{h}d" for h in vl.HORIZONS_DAYS} <= fwd_cols


# ── connection pool under scan concurrency ───────────────────────────────────

def test_pool_serves_more_threads_than_connections():
    nse_delivery.ensure_schema()
    syms = [_sym(i) for i in range(4 * trade_store._PG_MAXCONN)]
    with ThreadPoolExecutor(max_workers=10) as ex:
        frames = list(ex.map(nse_delivery.load_symbol_history, syms))
    assert len(frames) == len(syms)
    assert all(isinstance(f, pd.DataFrame) for f in frames)   # empty, but no PoolError


# ── verdict dedup (ON CONFLICT DO NOTHING RETURNING id) ──────────────────────

def test_concurrent_log_verdict_writes_exactly_one_row():
    fv = combine(composite_score=62.0, composite_action="Bullish")
    barrier = threading.Barrier(8)
    ids = []

    def _worker():
        barrier.wait()
        ids.append(vl.log_verdict(ticker=f"{_sym('D')}.NS", final_verdict=fv,
                                  entry_price=100.0, source="tomorrow_watchlist",
                                  signal_tags=["tech_high"]))

    threads = [threading.Thread(target=_worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    led = vl.load_ledger(ticker=f"{_sym('D')}.NS")
    assert len(led) == 1
    assert len([i for i in ids if i is not None]) == 1


# ── calibration on Postgres dtypes (NULL NIFTY → object dtype on pandas 2) ───

def test_calibration_with_all_null_nifty_returns():
    vl.ensure_schema()
    with trade_store._get_conn() as conn:
        cur = conn.cursor()
        for i in range(3):
            cur.execute(trade_store._q(
                "INSERT INTO verdict_log (logged_at, logged_date, ticker, entry_price, verdict, "
                "horizon, source) VALUES (?,?,?,?,?,?,?) RETURNING id"),
                ("2026-09-01T20:00:00", "2026-09-01", f"{_sym(i)}.NS", 100.0,
                 f"{PFX}-V", "medium", "x"))
            log_id = cur.fetchone()[0]
            cur.execute(trade_store._q(
                "INSERT INTO verdict_forward_returns (verdict_log_id, ret_1d) VALUES (?, ?)"),
                (log_id, 1.0))
        conn.commit()
    out = vl.calibration_by(group_col="verdict", horizon_days=1)
    row = out[out["verdict"] == f"{PFX}-V"].iloc[0]
    assert row["n"] == 3 and row["n_alpha"] == 0
    assert pd.isna(row["mean_alpha"])


# ── sector backfill: TEXT date comparisons + IN lists on Postgres ────────────

def test_sector_backfill_on_postgres(monkeypatch):
    import data.universe as uni
    names = [_sym(f"S{i}") for i in range(5)]
    monkeypatch.setattr(uni, "SECTOR_MAP", {f"{n}.NS": "TestSector" for n in names})
    days = [dt.date(2026, 9, d) for d in (21, 22, 23)]
    nse_delivery.ensure_schema()
    with trade_store._get_conn() as conn:
        cur = conn.cursor()
        for i, n in enumerate(names):
            for k, d in enumerate(days):
                cur.execute(trade_store._q(
                    "INSERT INTO nse_delivery_daily (symbol, date, close) VALUES (?,?,?)"),
                    (n, d.isoformat(), 100.0 * (1 + 0.01 * i) ** k))
        conn.commit()
    vl.ensure_schema()
    with trade_store._get_conn() as conn:
        cur = conn.cursor()
        cur.execute(trade_store._q(
            "INSERT INTO verdict_log (logged_at, logged_date, ticker, entry_price, verdict, "
            "horizon, source) VALUES (?,?,?,?,?,?,?) RETURNING id"),
            ("2026-09-21T20:00:00", "2026-09-21", f"{names[0]}.NS", 100.0, "BUY", "medium", "x"))
        log_id = cur.fetchone()[0]
        cur.execute(trade_store._q(
            "INSERT INTO verdict_forward_returns (verdict_log_id, ret_1d, nifty_ret_1d) "
            "VALUES (?,?,?)"), (log_id, 3.0, 0.0))
        conn.commit()
    # the ci-tests branch may hold other closes; only our window matters
    stats = vl.backfill_sector_returns()
    assert stats["updated"] >= 1
    led = vl.load_ledger(ticker=f"{names[0]}.NS")
    # daily moves 0%,1%,2%,3%,4% → median 2%
    assert led["sector_ret_1d"].iloc[0] == pytest.approx(2.0)
