"""FIX FII-PCTL: FII index-futures sub-score is percentile-based."""
import sqlite3

import pytest

from analysis.score import _score_positioning


@pytest.mark.parametrize("pct,pts", [
    (None, 1.5), (0.95, 3.0), (0.80, 3.0), (0.60, 2.0), (0.50, 2.0),
    (0.30, 1.0), (0.20, 1.0), (0.05, 0.0),
])
def test_fii_subscore_bands(pct, pts):
    _, d = _score_positioning(fii_deriv_pct=pct)
    assert d["fii_deriv"] == pts


def test_raw_net_alone_is_neutral_not_pinned():
    """-300k used to fall below the fixed -30k band and score 0 forever."""
    _, d = _score_positioning(fii_deriv_net=-300_000.0)
    assert d["fii_deriv"] == 1.5


@pytest.fixture
def fii_db(monkeypatch, tmp_path):
    import trade_store
    from data import nse_fii_deriv as fd
    db = tmp_path / "fii.db"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(trade_store, "_database_url", lambda: None)
    monkeypatch.setattr(trade_store, "_SQLITE_PATH", str(db))
    monkeypatch.setattr(fd, "_schema_ready_for", None)
    fd.ensure_schema()

    def fill(values):
        with sqlite3.connect(db) as c:
            c.executemany("INSERT INTO nse_fii_deriv_daily (date, fut_idx_net) VALUES (?, ?)",
                          [(f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}", v) for i, v in enumerate(values)])
    return fd, fill


def test_pct_needs_min_history(fii_db):
    fd, fill = fii_db
    fill([-100.0] * 30)
    assert fd.get_latest_fut_idx_pct(min_history=60) is None


def test_pct_ranks_latest_within_window(fii_db):
    fd, fill = fii_db
    fill([float(-i) for i in range(100, 0, -1)] + [-50.5])   # latest = -50.5 among -100..-1
    pct = fd.get_latest_fut_idx_pct(lookback=250, min_history=60)
    assert pct == pytest.approx(51 / 101)
