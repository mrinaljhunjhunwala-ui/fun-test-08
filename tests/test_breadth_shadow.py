"""Breadth agreement mapping, the bhavcopy breadth loader, and the SHADOW
breadth columns on verdict_log (SQLite round-trip)."""
from __future__ import annotations

import datetime as _dt
import os
import sys

import pandas as pd
import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import analysis.verdict_ledger as vl  # noqa: E402
from analysis.final_verdict import combine  # noqa: E402
from analysis.market_breadth import breadth_agreement  # noqa: E402


@pytest.mark.parametrize("app,br,status", [
    ("trend_down", "bear", "MATCH"),
    ("risk_off", "bear", "MATCH"),
    ("trend_up", "bull", "MATCH"),
    ("range", "neutral", "MATCH"),
    ("trend_down", "bull", "MISMATCH"),
    ("trend_up", "neutral", "MISMATCH"),
    ("unknown", "bear", "UNKNOWN"),
    ("trend_up", "unknown", "UNKNOWN"),
    (None, None, "UNKNOWN"),
])
def test_breadth_agreement_status(app, br, status):
    assert breadth_agreement(app, br, 50.0)["status"] == status


def test_breadth_agreement_text():
    assert breadth_agreement("trend_down", "bear", 27.4)["text"] == \
        "label trend_down; 27% above 50DMA: breadth agrees"
    t = breadth_agreement("trend_up", "bear", float("nan"))["text"]
    assert t == "label trend_up: breadth disagrees (reads bear)"


def _write_cache(d: str, n_sessions: int, up: bool) -> _dt.date:
    """n weekday sessions of 40 stocks, all rising (up) or falling; no PREV_CLOSE column."""
    os.makedirs(d, exist_ok=True)
    days = pd.bdate_range("2026-01-05", periods=n_sessions)
    for i, day in enumerate(days):
        px = 100 * (1.01 ** i if up else 0.99 ** i)
        pd.DataFrame({"SYMBOL": [f"S{k}" for k in range(40)], "SERIES": "EQ",
                      "DATE": day.date().isoformat(), "Close": px, "Volume": 1000}
                     ).to_csv(os.path.join(d, f"{day.date().isoformat()}.csv"), index=False)
    return days[-1].date()


def test_breadth_snapshot_from_disk(tmp_path, monkeypatch):
    import utils.nse_holidays as hol
    monkeypatch.setattr(hol, "is_trading_day", lambda d: d.weekday() < 5)
    from data.market_breadth_loader import breadth_snapshot
    last = _write_cache(str(tmp_path), 60, up=False)
    snap = breadth_snapshot(cache_dir=str(tmp_path), today=last)
    assert snap["advances"] == 0 and snap["declines"] == 40
    assert snap["pct_above_50dma"] == 0.0
    assert snap["breadth_label"] == "bear"
    assert snap["new_52w_highs"] is None          # < 250 sessions of history


def test_breadth_snapshot_empty_is_none(tmp_path):
    from data.market_breadth_loader import breadth_snapshot
    assert breadth_snapshot(cache_dir=str(tmp_path / "none"), today=_dt.date(2026, 10, 7)) is None


def _reset_shadow(monkeypatch, val):
    monkeypatch.setattr(vl, "_breadth_shadow_cache", {"t": 0.0, "val": (None, None)})
    if val is not None:
        monkeypatch.setattr(vl, "_breadth_shadow", lambda: val)


def test_ledger_round_trips_shadow_breadth(monkeypatch):
    _reset_shadow(monkeypatch, ("bear", 27.43))
    fv = combine(composite_score=62.0, composite_action="Bullish")
    rid = vl.log_verdict(ticker="INFY.NS", final_verdict=fv, entry_price=1500.0, source="shadow_auto")
    assert rid
    df = vl.load_ledger(ticker="INFY.NS")
    row = df.iloc[0]
    assert row["breadth_label"] == "bear"
    assert row["breadth_pct_above_50dma"] == pytest.approx(27.43)
    # Shadow only: the verdict itself is what combine() produced.
    assert row["verdict"] == fv.verdict


def test_ledger_logs_null_when_breadth_unavailable(monkeypatch):
    _reset_shadow(monkeypatch, None)   # real path: empty tmp bhavcopy cache
    fv = combine(composite_score=62.0, composite_action="Bullish")
    assert vl.log_verdict(ticker="TCS.NS", final_verdict=fv, entry_price=3000.0, source="shadow_auto")
    row = vl.load_ledger(ticker="TCS.NS").iloc[0]
    assert pd.isna(row["breadth_label"]) and pd.isna(row["breadth_pct_above_50dma"])
