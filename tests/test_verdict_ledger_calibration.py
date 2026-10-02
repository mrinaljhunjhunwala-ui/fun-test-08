"""
Calibration honesty (2026-09-29): day-clustered stats, NIFTY-regime split,
and the pre-registered decision rule.
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
from analysis import verdict_ledger as vl  # noqa: E402


def _seed(rows):
    """rows: (logged_date, ticker, verdict, ret_20d, nifty_ret_20d)"""
    vl.ensure_schema()
    with sqlite3.connect(trade_store._SQLITE_PATH) as con:
        for d, tk, verdict, ret, nret in rows:
            cur = con.execute(
                "INSERT INTO verdict_log (logged_at, logged_date, ticker, entry_price, verdict, "
                "horizon, source) VALUES (?,?,?,?,?,?,?)",
                (f"{d}T20:00:00", d, tk, 100.0, verdict, "medium", "tomorrow_watchlist"))
            con.execute(
                "INSERT INTO verdict_forward_returns (verdict_log_id, ret_20d, nifty_ret_20d) "
                "VALUES (?,?,?)", (cur.lastrowid, ret, nret))


# ── day-clustered columns ────────────────────────────────────────────────────

def test_same_day_picks_count_as_one_day():
    # 22 picks on ONE day, 14 beat NIFTY — row-level looks like 22 trials
    # 14 picks alpha +3, 8 picks alpha -1 → the day's mean alpha is positive
    rows = [("2026-08-31", f"T{i}.NS", "STRONG BUY", -2.0, (-5.0 if i < 14 else -1.0))
            for i in range(22)]
    _seed(rows)
    out = vl.calibration_by(group_col="verdict", horizon_days=20)
    r = out.iloc[0]
    assert r["n"] == 22
    assert r["n_days"] == 1
    assert r["win_vs_nifty"] == pytest.approx(63.6, abs=0.1)
    # one day, and its mean alpha is positive → 1/1 days, Wilson lower over n=1
    assert r["day_win_vs_nifty"] == 100.0
    assert r["wilson_lower_days_vs_nifty"] < 25.0      # n=1 day → very wide
    assert r["wilson_lower_days_vs_nifty"] < r["wilson_lower_vs_nifty"]


def test_day_win_uses_each_days_mean_alpha():
    _seed([
        ("2026-09-01", "A.NS", "BUY", 5.0, 0.0),     # day 1 mean alpha +1 → win
        ("2026-09-01", "B.NS", "BUY", -3.0, 0.0),
        ("2026-09-02", "C.NS", "BUY", -4.0, 0.0),    # day 2 mean alpha -4 → loss
    ])
    r = vl.calibration_by(group_col="verdict", horizon_days=20).iloc[0]
    assert r["n_days"] == 2
    assert r["day_win_vs_nifty"] == 50.0


def test_days_without_nifty_return_are_not_counted_as_wins_or_losses():
    _seed([("2026-09-01", "A.NS", "BUY", 5.0, None)])
    r = vl.calibration_by(group_col="verdict", horizon_days=20).iloc[0]
    assert r["n_days"] == 1
    assert pd.isna(r["day_win_vs_nifty"])


# ── regime ───────────────────────────────────────────────────────────────────

def _nifty(start, closes):
    idx = pd.bdate_range(start, periods=len(closes))
    return pd.DataFrame({"Close": closes}, index=idx)


def test_regime_uses_only_past_bars():
    # falls for 30 sessions, then rises steeply — a verdict logged at the
    # bottom must be 'Falling', even though the future is up.
    closes = [100 - i for i in range(30)] + [70 + 3 * i for i in range(30)]
    n = _nifty("2026-07-01", closes)
    bottom = n.index[29].date()
    assert vl.nifty_regime_at(n, bottom) == "Falling"
    assert vl.nifty_regime_at(n, n.index[-1].date()) == "Rising"


def test_regime_unknown_without_enough_history():
    n = _nifty("2026-09-01", [100 + i for i in range(10)])
    assert vl.nifty_regime_at(n, n.index[-1].date()) is None
    assert vl.nifty_regime_at(None, dt.date(2026, 9, 1)) is None


def test_regime_calibration_groups_bullish_verdicts_only():
    closes = [100 - i for i in range(30)] + [70 + i for i in range(30)]
    n = _nifty("2026-07-01", closes)
    falling_day = n.index[29].date().isoformat()
    rising_day = n.index[-1].date().isoformat()
    _seed([
        (falling_day, "A.NS", "BUY", 1.0, -2.0),
        (rising_day, "B.NS", "STRONG BUY", 2.0, 1.0),
        (rising_day, "C.NS", "AVOID", -5.0, 1.0),     # excluded: not bullish
        ("2026-01-05", "D.NS", "BUY", 1.0, 0.0),      # before NIFTY history → Unknown
    ])
    out = vl.regime_calibration(horizon_days=20, nifty_df=n).set_index("regime")
    assert set(out.index) == {"Falling", "Rising", "Unknown"}
    assert out.loc["Falling", "n"] == 1 and out.loc["Rising", "n"] == 1


# ── pre-registered decision rule ─────────────────────────────────────────────

_SMALL_RULE = dict(vl.DECISION_RULE, min_verdicts=6, min_days=3)


def test_rule_is_insufficient_below_sample_floor():
    _seed([("2026-08-31", f"T{i}.NS", "STRONG BUY", 1.0, -5.0) for i in range(22)])
    res = vl.evaluate_decision_rule()
    assert res["status"] == "insufficient"
    assert res["n"] == 22 and res["n_days"] == 1


def test_rule_met_and_not_met():
    days = [f"2026-09-{d:02d}" for d in (1, 2, 3, 4, 7, 8, 9, 10)]
    _seed([(d, f"{d}-{i}.NS", "BUY", 3.0, 0.0) for d in days for i in range(2)])
    met = vl.evaluate_decision_rule(dict(_SMALL_RULE, threshold_pct=60.0))
    assert met["status"] == "met"                       # 8/8 days, Wilson lower ≈ 67.6%
    not_met = vl.evaluate_decision_rule(dict(_SMALL_RULE, threshold_pct=70.0))
    assert not_met["status"] == "not_met"


def test_registered_rule_is_frozen():
    r = vl.DECISION_RULE
    assert (r["registered_on"], r["horizon_days"], r["min_verdicts"], r["min_days"],
            r["threshold_pct"]) == ("2026-09-29", 20, 150, 30, 50.0)
    assert tuple(r["verdicts"]) == ("BUY", "STRONG BUY")


def test_rule_floors_count_only_rows_with_a_nifty_return():
    days = [f"2026-09-{d:02d}" for d in (1, 2, 3, 4, 7, 8, 9, 10)]
    # every day has returns, but NIFTY is missing on 6 of the 8 days
    _seed([(d, f"{d}-{i}.NS", "BUY", 3.0, (0.0 if d in days[:2] else None))
           for d in days for i in range(2)])
    res = vl.evaluate_decision_rule(dict(_SMALL_RULE, threshold_pct=10.0))
    assert res["n_days"] == 8 and res["alpha_days"] == 2
    assert res["status"] == "insufficient"               # 2 alpha days < min_days 3


def test_regime_ignores_the_logged_days_own_close():
    # 21 flat bars, then a huge jump ON the logged day — must not flip to Rising
    closes = [100.0] * 20 + [99.0] + [200.0]
    n = _nifty("2026-08-03", closes)
    assert vl.nifty_regime_at(n, n.index[-1].date()) == "Falling"


def test_registered_rising_rule_is_frozen():
    r = vl.DECISION_RULE_RISING
    assert (r["registered_on"], r["regime"], r["horizon_days"], r["min_verdicts"], r["min_days"],
            r["threshold_pct"]) == ("2026-10-02", "Rising", 20, 150, 30, 50.0)
    assert tuple(r["verdicts"]) == ("BUY", "STRONG BUY")
    assert "regime" not in vl.DECISION_RULE           # the original rule stays pooled


def test_rising_rule_counts_only_rising_days():
    closes = [100 - i for i in range(30)] + [70 + 3 * i for i in range(30)]
    n = _nifty("2026-07-01", closes)
    falling = [str(n.index[k].date()) for k in (25, 26, 27, 28)]
    rising = [str(n.index[k].date()) for k in (55, 56, 57, 58, 59)]
    _seed([(d, f"{d}-{i}.NS", "BUY", 3.0, 0.0) for d in falling + rising for i in range(2)])
    rule = dict(vl.DECISION_RULE_RISING, min_verdicts=6, min_days=3, threshold_pct=10.0)
    res = vl.evaluate_decision_rule(rule, nifty_df=n)
    assert res["n_days"] == 5 and res["n"] == 10 and res["status"] == "met"
    assert vl.evaluate_decision_rule(rule, nifty_df=None)["status"] == "insufficient"
