"""tests/test_db_isolation.py — the suite must never reach the production DB.

Regression for 2026-09-26: tests wrote FAKE1.NS rows into production
verdict_log because DATABASE_URL is a system env var on the owner's machine.
tests/conftest.py blocks that; these tests prove the block holds even when a
production-looking URL is present in the environment AND in st.secrets.
"""
from __future__ import annotations

import os
import sqlite3

import pytest

import trade_store
from tests import conftest

_PROD_LIKE = "postgresql://owner:secret@ep-prod-123.ap-southeast-1.aws.neon.tech/neondb"


def test_env_database_url_is_ignored(monkeypatch):
    import streamlit as st
    # Blank st.secrets: a real .streamlit/secrets.toml would win over the env
    # var, fail the assert below and print the real URL into the test output.
    monkeypatch.setattr(st, "secrets", {}, raising=False)
    monkeypatch.setenv("DATABASE_URL", _PROD_LIKE)
    # The unguarded resolver WOULD have picked it up — so the checks below mean something.
    assert conftest._real_database_url() == _PROD_LIKE
    assert trade_store._database_url() is None
    assert trade_store.backend_name() == "sqlite"
    assert trade_store._is_pg() is False


def test_streamlit_secrets_database_url_is_ignored(monkeypatch):
    import streamlit as st
    monkeypatch.setattr(st, "secrets", {"DATABASE_URL": _PROD_LIKE}, raising=False)
    assert conftest._real_database_url() == _PROD_LIKE
    assert trade_store._database_url() is None
    assert trade_store.backend_name() == "sqlite"


def test_real_postgres_connection_cannot_be_opened(monkeypatch):
    """Even if code resolves a URL (here: _database_url forced to prod-like),
    the real psycopg2 pool can't connect — psycopg2.connect is blocked."""
    psycopg2 = pytest.importorskip("psycopg2")
    monkeypatch.setattr(trade_store, "_database_url", lambda: _PROD_LIKE)
    with pytest.raises(RuntimeError, match="Postgres access is blocked"):
        psycopg2.connect(_PROD_LIKE)
    with pytest.raises(RuntimeError, match="Postgres access is blocked"):
        trade_store._get_pg_pool()   # ThreadedConnectionPool connects eagerly
    with pytest.raises(RuntimeError, match="Postgres access is blocked"):
        with trade_store._get_conn():
            pass


def test_production_url_removed_from_environment():
    assert "DATABASE_URL" not in os.environ


def test_sqlite_path_is_per_test_tmp(tmp_path):
    assert trade_store._SQLITE_PATH == str(tmp_path / "trades.db")


def test_verdict_ledger_writes_land_in_tmp_sqlite(monkeypatch, tmp_path):
    """The exact path that leaked FAKE1.NS: log_verdict(source="tomorrow_watchlist")."""
    monkeypatch.setenv("DATABASE_URL", _PROD_LIKE)
    from analysis.final_verdict import combine
    from analysis.verdict_ledger import load_ledger, log_verdict

    log_verdict(
        ticker="FAKE1.NS",
        final_verdict=combine(composite_score=70.0, composite_action="BUY"),
        entry_price=100.0,
        composite_score=70.0,
        source="tomorrow_watchlist",
    )

    ledger = load_ledger(source="tomorrow_watchlist")
    assert list(ledger["ticker"]) == ["FAKE1.NS"]
    with sqlite3.connect(tmp_path / "trades.db") as conn:
        assert conn.execute("SELECT count(*) FROM verdict_log").fetchone()[0] == 1


def test_kv_is_isolated_between_tests_part1():
    assert trade_store.kv_set("isolation_probe", "part1")


def test_kv_is_isolated_between_tests_part2():
    assert trade_store.kv_get("isolation_probe") is None


@pytest.mark.real_postgres
def test_real_postgres_opt_in_uses_test_database_url():
    """Only runs with TEST_DATABASE_URL set; proves opt-in hands out that URL."""
    assert trade_store._database_url() == os.environ["TEST_DATABASE_URL"]
