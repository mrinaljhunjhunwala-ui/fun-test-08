"""FIX FLOWS-STALE — FII/DII 5-day sum must ignore rows older than the window."""
import pandas as pd

import analysis.score as sc


def _run(monkeypatch, dates):
    df = pd.DataFrame({"date": dates, "fii_net": [-1000.0] * len(dates),
                       "dii_net": [500.0] * len(dates)})
    import analysis.fii_dii as fd
    monkeypatch.setattr(fd, "load_history", lambda days=5: df.head(days))
    monkeypatch.setattr(sc, "_expected_last_session", lambda: pd.Timestamp("2026-10-01"))
    sc._FLOWS_CACHE.clear()
    try:
        return sc._get_flows_cached()
    finally:
        sc._FLOWS_CACHE.clear()


def test_fresh_rows_are_used(monkeypatch):
    v = _run(monkeypatch, ["2026-10-01", "2026-09-30", "2026-09-29", "2026-09-28", "2026-09-25"])
    assert v["n_days"] == 5 and v["fii_5d"] == -5000.0


def test_old_rows_are_dropped(monkeypatch):
    v = _run(monkeypatch, ["2026-10-01", "2026-09-30", "2026-09-29", "2026-09-16", "2026-09-15"])
    assert v["n_days"] == 3 and v["fii_5d"] == -3000.0


def test_all_stale_gives_no_flows(monkeypatch):
    assert _run(monkeypatch, ["2026-09-16", "2026-09-15", "2026-08-31", "2026-08-28"]) is None
