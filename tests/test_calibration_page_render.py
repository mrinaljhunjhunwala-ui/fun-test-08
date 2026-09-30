"""
Verdict Calibration page renders its new sections (pre-registered test, day
columns, regime split) against a SEEDED ledger — the generic page smoke test
hits an empty ledger and stops before any of them.
"""
from __future__ import annotations

import os
import socket
import sqlite3
import sys

import pytest
from streamlit.testing.v1 import AppTest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import trade_store  # noqa: E402
from analysis import verdict_ledger as vl  # noqa: E402

_PAGE = os.path.join(_ROOT, "dashboard", "pages", "21_verdict_calibration.py")


@pytest.fixture
def offline(monkeypatch):
    def _blocked(*a, **k):
        raise OSError("network blocked")
    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(vl, "_fetch_history", lambda t: None)


def _seed():
    vl.ensure_schema()
    with sqlite3.connect(trade_store._SQLITE_PATH) as con:
        for d in range(1, 11):
            for i in range(3):
                day = f"2026-08-{d + 10:02d}"
                cur = con.execute(
                    "INSERT INTO verdict_log (logged_at, logged_date, ticker, entry_price, verdict, "
                    "conviction, horizon, composite_action, source) VALUES (?,?,?,?,?,?,?,?,?)",
                    (f"{day}T20:00:0{i}", day, f"T{d}{i}.NS", 100.0,
                     "STRONG BUY" if i == 0 else "BUY", 90, "medium", "BUY", "shadow_auto"))
                con.execute(
                    "INSERT INTO verdict_forward_returns (verdict_log_id, price_1d, ret_1d, "
                    "nifty_ret_1d, price_5d, ret_5d, nifty_ret_5d, price_20d, ret_20d, nifty_ret_20d) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (cur.lastrowid, 101, 1.0, 0.5, 102, 2.0, 1.0, 97, -3.0, -5.0))


def test_calibration_page_renders_new_sections(offline):
    _seed()
    at = AppTest.from_file(_PAGE, default_timeout=120).run()
    assert not at.exception, at.exception
    text = " ".join(str(m.value) for m in (*at.markdown, *at.caption, *at.info))
    assert "Pre-registered evidence test" in text
    assert "Not enough data yet" in text                   # 30 verdicts < 150
    assert "By market regime" in text
    assert "counting each of the" in text                  # day-based interpretation line
    frames = [df.value for df in at.dataframe]
    assert any("Days" in f.columns and "Wilson lower (days) vs NIFTY %" in f.columns
               for f in frames)
    assert any("NIFTY regime" in f.columns for f in frames)
    assert any("α vs sector %" in f.columns and "Wilson lower (days) vs sector %" in f.columns
               for f in frames)
    assert "vs sector" in text


def test_changes_panel_lists_before_and_after(offline):
    _seed()
    at = AppTest.from_file(_PAGE, default_timeout=120).run()
    assert not at.exception, at.exception
    assert any("was meant to do" in e.label for e in at.expander)
    changes = [df.value for df in at.dataframe if list(df.value.columns) == ["Date", "Before", "Now"]]
    assert changes and len(changes[0]) >= 9
    assert changes[0]["Date"].is_monotonic_increasing          # dated, append-only history


def test_stale_ledger_module_is_reloaded_not_crashed(offline, monkeypatch):
    """Simulate Streamlit Cloud keeping the pre-deploy module in memory."""
    _seed()
    monkeypatch.delattr(vl, "backfill_sector_returns")
    at = AppTest.from_file(_PAGE, default_timeout=120).run()
    assert not at.exception, at.exception
    text = " ".join(str(c.value) for c in at.caption)
    assert "refreshed after an app update" in text
    assert hasattr(vl, "backfill_sector_returns")                # reload restored it


def test_unrecoverable_stale_module_hides_new_sections(offline, monkeypatch):
    _seed()
    import importlib

    def _reload_without(mod):
        if hasattr(mod, "regime_calibration"):
            delattr(mod, "regime_calibration")
        return mod
    monkeypatch.delattr(vl, "regime_calibration")
    monkeypatch.setattr(importlib, "reload", _reload_without)
    at = AppTest.from_file(_PAGE, default_timeout=120).run()
    assert not at.exception, at.exception
    assert any("still running older calibration code" in str(w.value) for w in at.warning)
    text = " ".join(str(m.value) for m in (*at.markdown, *at.caption))
    assert "Pre-registered evidence test" not in text and "By market regime" not in text
