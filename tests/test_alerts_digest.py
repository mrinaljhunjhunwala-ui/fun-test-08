"""tests/test_alerts_digest.py — Unit tests for the two new alert modes.

Covers:
  * check_delivery_digest reads the persisted top-picks snapshot and dedups
    per day, formats a well-shaped message, and fails safely with no snapshot.
  * check_momentum_opportunities dedups by am/pm bucket and skips when the
    scanner returns nothing (still emits a "quiet day" message so the user
    knows the pipeline ran).

Every network dependency is monkeypatched — this file MUST run offline in CI.
"""
from __future__ import annotations

import datetime
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

# Import the module under test — check_alerts.py has module-level state
# like _ALERTS_CSV. The dispatch fan-out is monkeypatched per-test.
import alerts.check_alerts as ca


@pytest.fixture(autouse=True)
def _silence_dispatch(monkeypatch):
    """Replace dispatch with a MagicMock that records calls but sends nothing."""
    mock = MagicMock(return_value=True)
    monkeypatch.setattr(ca, "dispatch", mock)
    return mock


# ─────────────────────────────────────────────────────────────────────────────
# check_delivery_digest
# ─────────────────────────────────────────────────────────────────────────────

def _mk_snapshot(n_buys: int = 3):
    """Build a top-picks snapshot that mirrors what warm_top_picks writes."""
    buys = [{
        "ticker": f"TICK{i}", "action": "BUY", "score": 80 - i,
        "price": 1000.0 + i * 100, "entry_zone": f"~{1000 + i*100}",
        "stop_loss": 950 + i*100, "target": 1150 + i*100,
        "sector": "IT",
    } for i in range(n_buys)]
    return {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "data": {"buys": buys, "sells": [], "meta": {}},
    }


def test_delivery_digest_sends_when_snapshot_present(_silence_dispatch, monkeypatch):
    fake_store = SimpleNamespace(kv_get=lambda k, user_id: _mk_snapshot(5))
    monkeypatch.setitem(sys.modules, "trade_store", fake_store)

    state, today = {}, "2026-09-08"
    fired = ca.check_delivery_digest(state, today)

    assert fired == 1
    assert state.get("delivery_digest") == today
    _silence_dispatch.assert_called_once()
    msg = _silence_dispatch.call_args[0][0]
    assert "Morning Delivery Digest" in msg
    assert "TICK0" in msg


def test_delivery_digest_dedups_same_day(_silence_dispatch, monkeypatch):
    fake_store = SimpleNamespace(kv_get=lambda k, user_id: _mk_snapshot(3))
    monkeypatch.setitem(sys.modules, "trade_store", fake_store)

    state, today = {"delivery_digest": "2026-09-08"}, "2026-09-08"
    fired = ca.check_delivery_digest(state, today)

    assert fired == 0
    _silence_dispatch.assert_not_called()


def test_delivery_digest_no_snapshot_returns_zero(_silence_dispatch, monkeypatch):
    fake_store = SimpleNamespace(kv_get=lambda k, user_id: None)
    monkeypatch.setitem(sys.modules, "trade_store", fake_store)

    fired = ca.check_delivery_digest({}, "2026-09-08")
    assert fired == 0
    _silence_dispatch.assert_not_called()


def test_delivery_digest_empty_buys_returns_zero(_silence_dispatch, monkeypatch):
    """Snapshot exists but has no buys — degrade cleanly, don't spam."""
    snap = _mk_snapshot(0)
    snap["data"]["buys"] = []
    fake_store = SimpleNamespace(kv_get=lambda k, user_id: snap)
    monkeypatch.setitem(sys.modules, "trade_store", fake_store)

    fired = ca.check_delivery_digest({}, "2026-09-08")
    assert fired == 0


def test_delivery_digest_respects_max_picks(_silence_dispatch, monkeypatch):
    """Snapshot has 10 buys but we should surface only max_picks."""
    fake_store = SimpleNamespace(kv_get=lambda k, user_id: _mk_snapshot(10))
    monkeypatch.setitem(sys.modules, "trade_store", fake_store)

    ca.check_delivery_digest({}, "2026-09-08", max_picks=3)
    msg = _silence_dispatch.call_args[0][0]
    # Verify only top 3 are named
    assert "TICK0" in msg and "TICK1" in msg and "TICK2" in msg
    assert "TICK5" not in msg


@pytest.mark.parametrize("gen_at", [
    (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=3)).isoformat(),  # Friday's scan on Monday
    "unknown",
    None,
])
def test_delivery_digest_skips_stale_snapshot(_silence_dispatch, monkeypatch, gen_at):
    """An old or undated snapshot must not go out as "today's scan", and must
    not stamp state — a later run with a fresh snapshot still sends."""
    snap = _mk_snapshot(3)
    snap["generated_at"] = gen_at
    fake_store = SimpleNamespace(kv_get=lambda k, user_id: snap)
    monkeypatch.setitem(sys.modules, "trade_store", fake_store)

    state = {}
    assert ca.check_delivery_digest(state, "2026-09-08") == 0
    assert "delivery_digest" not in state
    _silence_dispatch.assert_not_called()


def test_delivery_digest_accepts_last_evening_snapshot(_silence_dispatch, monkeypatch):
    """Post-close scan (~13.5h before a 09:00 digest) is still today's data."""
    snap = _mk_snapshot(3)
    snap["generated_at"] = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=13.5)).isoformat()
    fake_store = SimpleNamespace(kv_get=lambda k, user_id: snap)
    monkeypatch.setitem(sys.modules, "trade_store", fake_store)

    assert ca.check_delivery_digest({}, "2026-09-08") == 1


# ─────────────────────────────────────────────────────────────────────────────
# check_momentum_opportunities
# ─────────────────────────────────────────────────────────────────────────────

def test_momentum_am_pm_bucket_dedup(_silence_dispatch, monkeypatch):
    """Two calls in the same AM bucket must fire only once."""
    # Force scan_momentum to return one candidate so dispatch is called
    monkeypatch.setattr(
        "analysis.momentum_scanner.scan_momentum",
        lambda *a, **kw: [{
            "ticker": "GOOD", "price": 1000.0, "prior_high_55d": 980.0,
            "breakout_pct": 2.0, "volume_ratio": 2.0, "rsi": 65.0,
            "vwap": 990.0, "atr": 10.0, "sma50": 950.0, "sma200": 900.0,
            "rs_63d": 5.0, "suggested_entry": 1000.0,
            "suggested_stop": 980.0, "suggested_target": 1040.0,
        }],
    )
    monkeypatch.setattr("data.fetcher.fetch_single", lambda *a, **kw: pd.DataFrame())
    monkeypatch.setattr("data.universe.get_universe", lambda level: ["GOOD"])

    # Freeze time to 10:00 IST — AM bucket
    fake_now = datetime.datetime(2026, 9, 8, 10, 0, tzinfo=ca._IST)
    class _FakeDt(datetime.datetime):
        @classmethod
        def now(cls, tz=None): return fake_now
    monkeypatch.setattr(ca.datetime, "datetime", _FakeDt)

    state = {}
    assert ca.check_momentum_opportunities(state, "2026-09-08") == 1
    assert ca.check_momentum_opportunities(state, "2026-09-08") == 0  # dedup
    assert _silence_dispatch.call_count == 1


def test_momentum_quiet_day_still_sends(_silence_dispatch, monkeypatch):
    """No candidates — we still send a 'quiet day' note so the user
    knows the pipeline ran, and dedup marks the bucket so we don't retry."""
    monkeypatch.setattr("analysis.momentum_scanner.scan_momentum", lambda *a, **kw: [])
    monkeypatch.setattr("data.fetcher.fetch_single", lambda *a, **kw: pd.DataFrame())
    monkeypatch.setattr("data.universe.get_universe", lambda level: ["ANY"])

    fired = ca.check_momentum_opportunities({}, "2026-09-08")
    assert fired == 1
    msg = _silence_dispatch.call_args[0][0]
    assert "No stocks passed" in msg or "quiet" in msg.lower()


# ─────────────────────────────────────────────────────────────────────────────
# check_portfolio_posture
# ─────────────────────────────────────────────────────────────────────────────

def test_portfolio_posture_no_holdings_returns_zero(_silence_dispatch, monkeypatch):
    monkeypatch.setattr("data.angel_fetcher.get_holdings", lambda: [])
    monkeypatch.setattr("data.angel_fetcher.get_positions", lambda: {"day": [], "net": []})
    monkeypatch.setattr("data.fetcher.fetch_single", lambda *a, **kw: pd.DataFrame())

    fired = ca.check_portfolio_posture({}, "2026-09-08")
    assert fired == 0
    _silence_dispatch.assert_not_called()


def test_portfolio_posture_all_hold_marks_dedup(_silence_dispatch, monkeypatch):
    """When every holding is HOLD, we don't send a message but DO mark the
    day fired so the workflow won't keep retrying every re-run."""
    from analysis.portfolio_posture import HoldingPosture
    fake_hold = HoldingPosture(
        symbol="X", qty=10, avg_price=100, ltp=110, pnl_pct=10,
        posture="HOLD", reason="fine", filters_passing=4,
    )
    monkeypatch.setattr("data.angel_fetcher.get_holdings", lambda: [{"symbol": "X"}])
    monkeypatch.setattr("data.angel_fetcher.get_positions", lambda: {"day": [], "net": []})
    monkeypatch.setattr("data.fetcher.fetch_single", lambda *a, **kw: pd.DataFrame())
    monkeypatch.setattr("analysis.portfolio_posture.analyse_holdings", lambda *a, **kw: [fake_hold])
    monkeypatch.setattr("analysis.portfolio_posture.analyse_positions", lambda *a, **kw: [])

    state = {}
    fired = ca.check_portfolio_posture(state, "2026-09-08")
    assert fired == 0
    assert state.get("portfolio_posture") == "2026-09-08"    # deduped
    _silence_dispatch.assert_not_called()


def test_portfolio_posture_fires_when_exit_watch(_silence_dispatch, monkeypatch):
    from analysis.portfolio_posture import HoldingPosture
    danger = HoldingPosture(
        symbol="DANGER", qty=10, avg_price=100, ltp=80, pnl_pct=-20,
        posture="EXIT_WATCH", reason="SMA50 breakdown", filters_passing=1,
    )
    monkeypatch.setattr("data.angel_fetcher.get_holdings", lambda: [{"symbol": "DANGER"}])
    monkeypatch.setattr("data.angel_fetcher.get_positions", lambda: {"day": [], "net": []})
    monkeypatch.setattr("data.fetcher.fetch_single", lambda *a, **kw: pd.DataFrame())
    monkeypatch.setattr("analysis.portfolio_posture.analyse_holdings", lambda *a, **kw: [danger])
    monkeypatch.setattr("analysis.portfolio_posture.analyse_positions", lambda *a, **kw: [])

    fired = ca.check_portfolio_posture({}, "2026-09-08")
    assert fired == 1
    msg = _silence_dispatch.call_args[0][0]
    assert "DANGER" in msg and "EXIT" in msg


# ─────────────────────────────────────────────────────────────────────────────
# check_intraday_watchlist
# ─────────────────────────────────────────────────────────────────────────────

def _mk_ohlcv(n=30, close=1000):
    """Synthetic OHLCV frame for ATR-computable rows."""
    import numpy as np
    idx = pd.date_range("2026-08-01", periods=n, freq="B")
    return pd.DataFrame({
        "Open":   np.full(n, float(close)),
        "High":   np.full(n, close * 1.01),
        "Low":    np.full(n, close * 0.99),
        "Close":  np.full(n, float(close)),
        "Volume": np.full(n, 100_000.0),
    }, index=idx)


def test_intraday_watchlist_reformats_picks(_silence_dispatch, monkeypatch):
    """A snapshot with 3 buys + a working fetcher must send an intraday msg."""
    fake_store = SimpleNamespace(kv_get=lambda k, user_id: _mk_snapshot(3))
    monkeypatch.setitem(sys.modules, "trade_store", fake_store)
    monkeypatch.setattr("data.fetcher.fetch_single", lambda t, period="3mo": _mk_ohlcv())

    fired = ca.check_intraday_watchlist({}, "2026-09-09")
    assert fired == 1
    msg = _silence_dispatch.call_args[0][0]
    assert "Intraday Morning Watchlist" in msg
    assert "ORB high" in msg
    assert "TICK0" in msg


def test_intraday_watchlist_dedups(_silence_dispatch, monkeypatch):
    fake_store = SimpleNamespace(kv_get=lambda k, user_id: _mk_snapshot(3))
    monkeypatch.setitem(sys.modules, "trade_store", fake_store)
    monkeypatch.setattr("data.fetcher.fetch_single", lambda t, period="3mo": _mk_ohlcv())

    state = {"intraday_watchlist": "2026-09-09"}
    assert ca.check_intraday_watchlist(state, "2026-09-09") == 0
    _silence_dispatch.assert_not_called()


def test_intraday_watchlist_no_snapshot_skips(_silence_dispatch, monkeypatch):
    fake_store = SimpleNamespace(kv_get=lambda k, user_id: None)
    monkeypatch.setitem(sys.modules, "trade_store", fake_store)

    assert ca.check_intraday_watchlist({}, "2026-09-09") == 0
    _silence_dispatch.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# check_weekly_digest
# ─────────────────────────────────────────────────────────────────────────────

def test_weekly_digest_refuses_mid_week(_silence_dispatch, monkeypatch):
    """weekly-digest must NOT fire on a weekday even if manually triggered."""
    # Fake Tuesday
    fake_now = datetime.datetime(2026, 9, 8, 9, 0, tzinfo=ca._IST)  # weekday=1
    class _FakeDt(datetime.datetime):
        @classmethod
        def now(cls, tz=None): return fake_now
    monkeypatch.setattr(ca.datetime, "datetime", _FakeDt)

    fired = ca.check_weekly_digest({}, "2026-09-08")
    assert fired == 0
    _silence_dispatch.assert_not_called()


def test_weekly_digest_fires_on_sunday(_silence_dispatch, monkeypatch, tmp_path):
    """On a Sunday with a snapshot and journal activity, digest goes out."""
    # Fake Sunday. Capture the real strptime BEFORE patching so our fake
    # can delegate to it without recursing into itself.
    _real_strptime = datetime.datetime.strptime
    fake_now = datetime.datetime(2026, 9, 13, 9, 0, tzinfo=ca._IST)  # weekday=6
    class _FakeDt(datetime.datetime):
        @classmethod
        def now(cls, tz=None): return fake_now
        @classmethod
        def strptime(cls, s, f): return _real_strptime(s, f)
    monkeypatch.setattr(ca.datetime, "datetime", _FakeDt)

    fake_store = SimpleNamespace(kv_get=lambda k, user_id: _mk_snapshot(4))
    monkeypatch.setitem(sys.modules, "trade_store", fake_store)

    # Empty journal is fine — the digest still sends with the picks section
    monkeypatch.setattr("alerts.journal_store.read_entries", lambda: [])

    fired = ca.check_weekly_digest({}, "2026-09-13")
    assert fired == 1
    msg = _silence_dispatch.call_args[0][0]
    assert "Weekly Digest" in msg
    assert "TICK0" in msg
