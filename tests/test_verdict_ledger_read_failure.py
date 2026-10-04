"""A failed ledger read must never look like an empty ledger (2026-10-05).

Right after a reboot on Streamlit Cloud the full-ledger read lost a DB-pool race
and load_ledger() returned an empty frame, so page 21's evidence test said
"0 verdicts over 0 days" while the page's own metrics showed 1,044 verdicts.
"""
from __future__ import annotations

import contextlib
import os
import socket
import sqlite3
import sys

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import trade_store  # noqa: E402
from analysis import verdict_ledger as vl  # noqa: E402

_PAGE = os.path.join(_ROOT, "dashboard", "pages", "21_verdict_calibration.py")


class PoolError(Exception):
    """Same class name as psycopg2.pool.PoolError — matched by name."""


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(vl, "_LEDGER_RETRY_DELAYS_S", (0.0,))


def _seed(n_days=3, per_day=2):
    vl.ensure_schema()
    with sqlite3.connect(trade_store._SQLITE_PATH) as con:
        for d in range(n_days):
            for i in range(per_day):
                day = f"2026-08-{d + 10:02d}"
                cur = con.execute(
                    "INSERT INTO verdict_log (logged_at, logged_date, ticker, entry_price, verdict, "
                    "conviction, horizon, composite_action, source) VALUES (?,?,?,?,?,?,?,?,?)",
                    (f"{day}T20:00:0{i}", day, f"T{d}{i}.NS", 100.0, "BUY", 80, "medium", "BUY",
                     "shadow_auto"))
                con.execute(
                    "INSERT INTO verdict_forward_returns (verdict_log_id, price_20d, ret_20d, "
                    "nifty_ret_20d) VALUES (?,?,?,?)", (cur.lastrowid, 103, 3.0, 1.0))


def _failing_conn(exc_factory, calls):
    @contextlib.contextmanager
    def _get_conn():
        calls.append(1)
        raise exc_factory()
        yield  # pragma: no cover
    return _get_conn


# ── load_ledger ──────────────────────────────────────────────────────────────

def test_default_returns_tagged_empty_frame(monkeypatch):
    _seed()
    calls = []
    monkeypatch.setattr(vl, "ensure_schema", lambda: None)
    monkeypatch.setattr(trade_store, "_get_conn", _failing_conn(lambda: PoolError("busy"), calls))
    df = vl.load_ledger()
    assert df.empty
    assert vl.ledger_error(df) == "PoolError"
    assert len(calls) == 2                                   # one retry for a transient error


def test_raise_mode_raises_ledger_unavailable(monkeypatch):
    calls = []
    monkeypatch.setattr(vl, "ensure_schema", lambda: None)
    monkeypatch.setattr(trade_store, "_get_conn", _failing_conn(lambda: PoolError("busy"), calls))
    with pytest.raises(vl.LedgerUnavailable) as ei:
        vl.load_ledger(on_error="raise")
    assert ei.value.error_type == "PoolError"


def test_non_transient_error_is_not_retried(monkeypatch):
    calls = []
    monkeypatch.setattr(vl, "ensure_schema", lambda: None)
    monkeypatch.setattr(trade_store, "_get_conn", _failing_conn(lambda: KeyError("col"), calls))
    assert vl.ledger_error(vl.load_ledger()) == "KeyError"
    assert len(calls) == 1


def test_transient_error_recovers_on_retry(monkeypatch):
    _seed()
    real = vl._read_ledger
    state = {"n": 0}

    def _flaky(**kw):
        state["n"] += 1
        if state["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(**kw)
    monkeypatch.setattr(vl, "_read_ledger", _flaky)
    df = vl.load_ledger(on_error="raise")
    assert len(df) == 6 and vl.ledger_error(df) is None


def test_real_empty_ledger_is_not_an_error():
    vl.ensure_schema()
    df = vl.load_ledger()
    assert df.empty and vl.ledger_error(df) is None


def test_bad_on_error_value_rejected():
    with pytest.raises(ValueError):
        vl.load_ledger(on_error="ignore")


def test_ledger_error_ignores_none():
    assert vl.ledger_error(None) is None
    assert vl.ledger_error(pd.DataFrame()) is None


# ── consumers report "unavailable", not zero ────────────────────────────────

@pytest.fixture
def broken_db(monkeypatch):
    _seed()
    monkeypatch.setattr(vl, "ensure_schema", lambda: None)
    monkeypatch.setattr(trade_store, "_get_conn", _failing_conn(lambda: PoolError("busy"), []))


def test_decision_rule_reports_unavailable(broken_db):
    res = vl.evaluate_decision_rule()
    assert res["status"] == "unavailable"
    assert res["error"] == "PoolError"


def test_decision_rule_ok_read_has_no_error():
    _seed()
    res = vl.evaluate_decision_rule()
    assert res["status"] == "insufficient" and res["error"] is None
    assert res["n_alpha"] == 6


@pytest.mark.parametrize("call", [
    lambda: vl.calibration_by(group_col="verdict", horizon_days=20),
    lambda: vl.regime_calibration(horizon_days=20, nifty_df=None),
    lambda: vl.tag_calibration(horizon_days=20),
    lambda: vl.shadow_pnl(horizon_days=20),
])
def test_calibration_frames_carry_the_error(broken_db, call):
    df = call()
    assert df.empty and vl.ledger_error(df) == "PoolError"


# ── page 21 ──────────────────────────────────────────────────────────────────

@pytest.fixture
def offline(monkeypatch):
    def _blocked(*a, **k):
        raise OSError("network blocked")
    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(vl, "_fetch_history", lambda t: None)


def test_page_says_couldnt_read_instead_of_zero(offline, monkeypatch):
    """The 2026-10-05 shape: the 5,000-row read works, the full read fails."""
    _seed()
    real = vl._read_ledger

    def _full_read_fails(**kw):
        if kw["limit"] >= vl._FULL_LEDGER_LIMIT:
            raise PoolError("pool exhausted")
        return real(**kw)
    monkeypatch.setattr(vl, "_read_ledger", _full_read_fails)
    at = AppTest.from_file(_PAGE, default_timeout=120).run()
    assert not at.exception, at.exception
    warnings = " ".join(str(w.value) for w in at.warning)
    assert "Couldn't read the verdict ledger right now" in warnings and "PoolError" in warnings
    infos = " ".join(str(i.value) for i in at.info)
    assert "Not enough data yet" not in infos
    assert "0** verdicts over **0" not in infos + warnings


def test_page_stops_with_notice_when_nothing_reads(offline, monkeypatch):
    _seed()
    monkeypatch.setattr(vl, "_read_ledger", lambda **kw: (_ for _ in ()).throw(PoolError("x")))
    at = AppTest.from_file(_PAGE, default_timeout=120).run()
    assert not at.exception, at.exception
    assert any("Couldn't read the verdict ledger" in str(w.value) for w in at.warning)
    assert not any("No verdicts logged yet" in str(i.value) for i in at.info)
