"""
verdict_log is written as a side effect of rendering scan pages (Tomorrow's
Watchlist live scan, Top Picks via pick_freshness) — potentially from several
Streamlit sessions and background threads at once. Its only guard against
double-logging is the UNIQUE index on (logged_date, ticker, horizon, source)
plus INSERT OR IGNORE / ON CONFLICT DO NOTHING. These tests pin that contract.
"""
from __future__ import annotations

import os
import sys
import threading

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from analysis.final_verdict import combine  # noqa: E402
from analysis.verdict_ledger import load_ledger, log_verdict  # noqa: E402


def _fv():
    return combine(composite_score=62.0, composite_action="Bullish")


def _log(ticker="INFY.NS", source="tomorrow_watchlist", price=1500.0, tags=None):
    return log_verdict(ticker=ticker, final_verdict=_fv(), entry_price=price,
                       composite_score=62.0, source=source, signal_tags=tags)


def test_same_day_same_source_logs_once_and_keeps_first_entry():
    first = _log(price=1500.0)
    second = _log(price=1525.0)          # a later rerun of the scan the same day
    assert first is not None
    assert second is None                # dedup → no new row
    led = load_ledger()
    assert len(led) == 1
    assert float(led["entry_price"].iloc[0]) == 1500.0


def test_different_source_is_a_separate_row():
    assert _log(source="tomorrow_watchlist") is not None
    assert _log(source="top_picks") is not None
    assert len(load_ledger()) == 2


def test_concurrent_sessions_racing_log_exactly_one_row():
    barrier = threading.Barrier(10)
    ids = []

    def _worker():
        barrier.wait()
        ids.append(_log(tags=["tech_high"]))

    threads = [threading.Thread(target=_worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    assert len(load_ledger()) == 1
    winners = [i for i in ids if i is not None]
    assert len(winners) == 1, f"expected one insert to win, got ids={ids}"
