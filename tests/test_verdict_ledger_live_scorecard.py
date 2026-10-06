"""Live scorecard + non-overlapping period stats (audit 2026-10-03 #26)."""
import numpy as np
import pandas as pd
import pytest

from analysis import verdict_ledger as VL


def _row(date, verdict, ret, nifty, ticker="X.NS", h=5):
    return {"logged_date": date, "logged_at": f"{date}T16:00:00", "ticker": ticker,
            "verdict": verdict, f"ret_{h}d": ret, f"nifty_ret_{h}d": nifty}


def test_session_index_skips_weekends_and_holidays():
    idx = VL._session_index(["2026-10-01", "2026-10-02", "2026-10-05"])   # 2 Oct = NSE holiday
    assert idx[2] - idx[0] == 1


def test_block_stats_counts_overlapping_days_as_few_periods():
    d = pd.DataFrame({"logged_date": ["2026-09-21", "2026-09-22", "2026-09-23",
                                      "2026-09-24", "2026-09-25"], "v": [1, 2, 3, 4, 5]})
    assert VL._block_stats(d, "v", 5)["n_blocks"] in (1, 2)     # never 5
    assert VL._block_stats(d, "v", 1)["n_blocks"] == 5


def test_live_scorecard_signs_avoid_calls_and_labels_thin_evidence(monkeypatch):
    rows = [_row("2026-09-21", "WATCH", 3.0, 1.0, "A.NS"),     # edge +2
            _row("2026-09-21", "AVOID", -2.0, 1.0, "B.NS"),    # edge +3 (stock lagged)
            _row("2026-09-21", "HOLD", 5.0, 1.0, "C.NS"),      # no direction: ignored
            _row("2026-09-22", "WATCH", None, None, "D.NS")]   # not settled yet
    monkeypatch.setattr(VL, "load_ledger", lambda **k: pd.DataFrame(rows))
    r = VL.live_scorecard(horizon_days=5, source=None)
    s = r["summary"].set_index("side")
    assert s.loc["all", "picks"] == 2 and r["pending"] == 1
    assert s.loc["bearish", "mean_edge_pct"] == pytest.approx(3.0)
    assert s.loc["all", "evidence"].startswith("too early")


def test_live_scorecard_reports_unreadable_ledger(monkeypatch):
    def boom(**k):
        raise VL.LedgerUnavailable(RuntimeError("db down"))
    monkeypatch.setattr(VL, "load_ledger", boom)
    r = VL.live_scorecard(horizon_days=5)
    assert r["error"] and r["summary"].empty


def test_evidence_label_needs_eight_periods_and_t_two():
    base = {"block_mean": 1.0, "block_win": 0.9, "block_wilson_lower": 0.6}
    assert VL._evidence_label({**base, "n_blocks": 7, "block_t": 5.0}).startswith("too early")
    assert VL._evidence_label({**base, "n_blocks": 8, "block_t": 2.5}) == "ahead of the Nifty so far"
    assert VL._evidence_label({**base, "n_blocks": 8, "block_t": -2.5}) == "behind the Nifty so far"
    assert VL._evidence_label({**base, "n_blocks": 8, "block_t": np.nan}).startswith("no clear")
