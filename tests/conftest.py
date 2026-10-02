"""tests/conftest.py — keep the test suite out of the production database.

Why this exists (incident 2026-09-26): on the owner's machine DATABASE_URL is a
Windows *system* environment variable pointing at the production Neon Postgres,
and ``trade_store._database_url()`` falls back to ``os.environ["DATABASE_URL"]``
when ``st.secrets`` has none. With no conftest, any locally-run test that
reached ``trade_store`` wrote to production — e.g.
``test_tomorrow_watchlist_threads_horizon_through`` logged a fake ``FAKE1.NS``
@ ₹100 pick into ``verdict_log``, which Tomorrow's Watchlist then showed to
users as a "previous pick".

Every DB access in the repo funnels through ``trade_store._get_conn()`` →
``_is_pg()`` → ``_database_url()``, and every real Postgres connection is
ultimately opened by ``psycopg2.connect``. The guard sits at both ends:

1. **Session-wide hard block** — installed when this file is imported, i.e.
   before any test module is collected, and never undone during the session
   (so background threads that outlive their test — test_pages_smoke.py's
   live-price pool — and module-level code are covered, not just test bodies):
   * ``DATABASE_URL`` is removed from ``os.environ``;
   * ``trade_store._database_url`` returns None (ignoring st.secrets too), so
     code takes the SQLite path by default;
   * ``psycopg2.connect`` raises. Tests that exercise the Postgres code path
     against a FAKE pool (tests/test_trade_store_pg_pool.py) still work — a
     fake pool never calls psycopg2.connect — but nothing can open a real
     connection. Note test_pages_smoke.py's socket block does NOT cover this:
     libpq opens its own C-level sockets.
2. **Per-test SQLite isolation** — an autouse fixture points
   ``trade_store._SQLITE_PATH`` at a fresh ``tmp_path`` file and resets the
   pool / schema-ready flags, so tests can't see each other's rows or touch
   the repo's local ``trades.db``.

Opting in to a real Postgres: mark the test ``@pytest.mark.real_postgres``.
It then runs against ``TEST_DATABASE_URL`` (never ``DATABASE_URL``), is
skipped when that isn't set, and fails if it equals the production URL that
was in the environment at startup.
"""
from __future__ import annotations

import hashlib
import os
import uuid

import pytest

import trade_store

# ── Layer 1: session-wide hard block (runs at conftest import) ───────────────

# Keep only a fingerprint of the production URL — enough to refuse a
# TEST_DATABASE_URL that is actually production, without keeping the secret.
def _fingerprint(url: str | None) -> str | None:
    return hashlib.sha256(url.strip().encode()).hexdigest() if url else None


_PROD_URL_FINGERPRINT = _fingerprint(os.environ.pop("DATABASE_URL", None))

_real_database_url = trade_store._database_url
# Set only by the real_postgres opt-in fixture. Deliberately NOT routed through
# the real _database_url(): that reads st.secrets first, which could be prod.
_opt_in_url: str | None = None


def _guarded_database_url():
    return _opt_in_url


trade_store._database_url = _guarded_database_url
trade_store._pg_pool = None

try:
    import psycopg2
except ImportError:  # optional dependency — nothing to block
    psycopg2 = None
else:
    _real_pg_connect = psycopg2.connect

    def _guarded_pg_connect(*args, **kwargs):
        if not _opt_in_url:
            raise RuntimeError(
                "tests/conftest.py: Postgres access is blocked in the test suite. "
                "Mark the test @pytest.mark.real_postgres and set TEST_DATABASE_URL "
                "to a throwaway database to opt in."
            )
        if args and isinstance(args[0], str):
            args = (_direct_endpoint(args[0]),) + args[1:]
        conn = _real_pg_connect(*args, **kwargs)
        _enter_run_schema(conn)
        return conn

    psycopg2.connect = _guarded_pg_connect

# Per-run schema: every CI run shares the Neon `ci-tests` branch, and two runs
# doing DDL / cleanup on the same tables deadlocked (CI run 37014283387). Each
# pytest session now creates its own schema, points every connection's
# search_path at it, and drops it at session end.
_RUN_SCHEMA = f"ci_{os.getpid()}_{uuid.uuid4().hex[:10]}"
_run_schema_created = False


def _direct_endpoint(url: str) -> str:
    # Neon's -pooler endpoint is PgBouncer in transaction mode, where a session
    # SET search_path doesn't stick to the connection — use the direct host.
    return url.replace("-pooler.", ".")


def _enter_run_schema(conn) -> None:
    global _run_schema_created
    cur = conn.cursor()
    if not _run_schema_created:
        cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{_RUN_SCHEMA}"')
        _run_schema_created = True
    cur.execute(f'SET search_path TO "{_RUN_SCHEMA}"')
    conn.commit()


def pytest_sessionfinish(session, exitstatus):
    url = os.environ.get("TEST_DATABASE_URL")
    if not (_run_schema_created and url and psycopg2 is not None):
        return
    try:
        conn = _real_pg_connect(_direct_endpoint(url))
        try:
            conn.cursor().execute(f'DROP SCHEMA IF EXISTS "{_RUN_SCHEMA}" CASCADE')
            conn.commit()
        finally:
            conn.close()
    except Exception as e:  # best effort — a leftover schema is harmless
        print(f"conftest: could not drop {_RUN_SCHEMA}: {e}")

# Angel One: a real .streamlit/secrets.toml on the owner's machine made
# "not connected" tests see credentials and page tests attempt live logins.
# Blank the secrets read point session-wide; tests set ANGEL_* env vars or
# stub angel_fetcher functions when they want a configured state.
import data.angel_fetcher as _angel  # noqa: E402

_angel._secrets_section = lambda: {}


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "real_postgres: opt in to a real Postgres via TEST_DATABASE_URL "
        "(never DATABASE_URL); skipped when unset",
    )


@pytest.fixture(scope="session", autouse=True)
def _session_sqlite(tmp_path_factory):
    """Default SQLite file for anything running outside a test's own fixture
    scope (leaked background threads, session-scoped fixtures)."""
    trade_store._SQLITE_PATH = str(tmp_path_factory.mktemp("trade_store") / "session.db")
    yield


# ── Layer 2: per-test isolation ──────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolate_trade_store(request, monkeypatch, tmp_path):
    global _opt_in_url

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(trade_store, "_SQLITE_PATH", str(tmp_path / "trades.db"))
    monkeypatch.setattr(trade_store, "_pg_pool", None)
    monkeypatch.setattr(trade_store, "_pg_slots", None, raising=False)
    monkeypatch.setattr(trade_store, "_schema_ready_for", None)
    monkeypatch.setattr(trade_store, "_kv_ready_for", None)

    if request.node.get_closest_marker("real_postgres") is None:
        yield
        return

    test_url = os.environ.get("TEST_DATABASE_URL")
    if not test_url:
        pytest.skip("real_postgres test: TEST_DATABASE_URL not set")
    if _PROD_URL_FINGERPRINT and _fingerprint(test_url) == _PROD_URL_FINGERPRINT:
        pytest.fail("TEST_DATABASE_URL is the production DATABASE_URL — refusing to run")

    _opt_in_url = test_url
    try:
        yield
    finally:
        _opt_in_url = None
        pool = trade_store._pg_pool
        if pool is not None:
            pool.closeall()
