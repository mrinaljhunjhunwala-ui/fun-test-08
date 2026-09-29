"""
Sector peer-basket baseline (2026-09-30): pure basket math, the no-network
sector backfill, and the α-vs-sector calibration columns.
"""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
import sys

import pandas as pd
import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import trade_store  # noqa: E402
from analysis import sector_baskets as sb  # noqa: E402
from analysis import verdict_ledger as vl  # noqa: E402

D = [dt.date(2026, 9, d) for d in (21, 22, 23, 24, 25)]
SECTOR_OF = {"A": "IT", "B": "IT", "C": "IT", "E": "IT", "F": "IT",
             "X": "Metal", "Y": "Metal"}


def _closes(rows):
    return pd.DataFrame(rows, columns=["symbol", "date", "close"])


def _it_closes(extra=()):
    rows = []
    # day-2 moves: A +10%, B 0, C -10%, E +2%, F +4%  → median +2%
    # day-4 moves: A +10%, E +1%, others 0            → median 0
    for sym, path in {"A": [100, 110, 110, 121, 121],
                      "B": [100, 100, 100, 100, 100],
                      "C": [100, 90, 90, 90, 90],
                      "E": [100, 102, 102, 103.02, 103.02],
                      "F": [100, 104, 104, 104, 104]}.items():
        rows += [(sym, d.isoformat(), c) for d, c in zip(D, path)]
    return _closes(rows + list(extra))


# ── pure math ────────────────────────────────────────────────────────────────

def test_daily_basket_is_the_median_move():
    daily = sb.basket_daily_returns(_it_closes(), SECTOR_OF)
    assert pd.isna(daily.loc[D[0], "IT"])                         # anchor day, no return
    assert daily.loc[D[1], "IT"] == pytest.approx(0.02)
    assert daily.loc[D[3], "IT"] == pytest.approx(0.0)


def test_min_constituents_blanks_thin_sectors():
    closes = _closes([(s, d.isoformat(), 100 + i) for s in ("X", "Y")
                      for i, d in enumerate(D)])
    daily = sb.basket_daily_returns(closes, SECTOR_OF)
    assert daily["Metal"].isna().all()                            # 2 names < 5


def test_corporate_action_jump_is_dropped():
    closes = _it_closes().copy()
    closes.loc[(closes.symbol == "B") & (closes.date == D[2].isoformat()), "close"] = 50  # 1:2 split
    closes.loc[(closes.symbol == "B") & (closes.date > D[2].isoformat()), "close"] = 50
    daily = sb.basket_daily_returns(closes, SECTOR_OF, min_constituents=5)
    # B's -50% day is dropped → only 4 usable names that day → no basket value
    assert pd.isna(daily.loc[D[2], "IT"])
    daily4 = sb.basket_daily_returns(closes, SECTOR_OF, min_constituents=4)
    assert daily4.loc[D[2], "IT"] == pytest.approx(0.0)           # median of A,C,E,F (all flat)


def test_basket_return_compounds_and_requires_full_coverage():
    daily = sb.basket_daily_returns(_it_closes(), SECTOR_OF)
    assert sb.basket_return(daily, "IT", D[0], D[3]) == pytest.approx(2.0)   # +2%, 0, 0
    assert sb.basket_return(daily, "IT", D[0], dt.date(2026, 9, 29)) is None   # end off-grid
    assert sb.basket_return(daily, "IT", dt.date(2026, 9, 18), D[2]) is None   # start off-grid
    assert sb.basket_return(daily, "Metal", D[0], D[2]) is None                # no such sector
    assert sb.basket_return(daily, "IT", D[2], D[2]) is None


# ── backfill (no network) ────────────────────────────────────────────────────

def _seed_delivery(closes):
    from data import nse_delivery
    nse_delivery._schema_ready_for = None
    nse_delivery.ensure_schema()
    with sqlite3.connect(trade_store._SQLITE_PATH) as con:
        con.executemany("INSERT INTO nse_delivery_daily (symbol, date, close) VALUES (?,?,?)",
                        closes.itertuples(index=False, name=None))


def _seed_verdict(ticker, logged, ret_1d):
    vl.ensure_schema()
    with sqlite3.connect(trade_store._SQLITE_PATH) as con:
        cur = con.execute(
            "INSERT INTO verdict_log (logged_at, logged_date, ticker, entry_price, verdict, horizon, "
            "source) VALUES (?,?,?,?,?,?,?)",
            (f"{logged}T20:00:00", logged, ticker, 100.0, "BUY", "medium", "x"))
        con.execute("INSERT INTO verdict_forward_returns (verdict_log_id, ret_1d, nifty_ret_1d) "
                    "VALUES (?,?,?)", (cur.lastrowid, ret_1d, 0.0))


@pytest.fixture
def it_universe(monkeypatch):
    import data.universe as uni
    monkeypatch.setattr(uni, "SECTOR_MAP", {f"{s}.NS": sec for s, sec in SECTOR_OF.items()})


def test_backfill_fills_sector_return_from_stored_closes(it_universe):
    _seed_delivery(_it_closes())
    _seed_verdict("A.NS", D[0].isoformat(), 10.0)
    _seed_verdict("ZZZ.NS", D[0].isoformat(), 1.0)             # not in universe → skipped
    stats = vl.backfill_sector_returns()
    assert stats == {"candidates": 1, "updated": 1}
    led = vl.load_ledger().set_index("ticker")
    assert led.loc["A.NS", "sector_ret_1d"] == pytest.approx(2.0)
    assert pd.isna(led.loc["ZZZ.NS", "sector_ret_1d"])
    # idempotent: nothing left to fill for A
    assert vl.backfill_sector_returns()["updated"] == 0


def test_uncovered_window_stays_null(it_universe):
    _seed_delivery(_it_closes())
    _seed_verdict("A.NS", D[-1].isoformat(), 1.0)              # 1d target beyond stored closes
    assert vl.backfill_sector_returns()["updated"] == 0


# ── calibration columns ─────────────────────────────────────────────────────

def test_calibration_reports_alpha_vs_sector(it_universe):
    _seed_delivery(_it_closes())
    _seed_verdict("A.NS", D[0].isoformat(), 10.0)              # sector 1d ret = +2 → α +8
    vl.backfill_sector_returns()
    r = vl.calibration_by(group_col="verdict", horizon_days=1).iloc[0]
    assert r["n_alpha_sector"] == 1
    assert r["mean_alpha_sector"] == pytest.approx(8.0)
    assert r["win_vs_sector"] == 100.0
    assert r["sector_days"] == 1


def test_backfill_skips_the_close_read_when_nothing_is_coverable(it_universe, monkeypatch):
    _seed_delivery(_it_closes())
    _seed_verdict("A.NS", D[-1].isoformat(), 1.0)              # 1d target after last close
    called = []
    monkeypatch.setattr(sb, "basket_daily_returns", lambda *a, **k: called.append(1))
    assert vl.backfill_sector_returns() == {"candidates": 0, "updated": 0}
    assert not called
