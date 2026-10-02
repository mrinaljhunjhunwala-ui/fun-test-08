"""
Tomorrow's Watchlist picks carry their bucket as a 'tw:<bucket>' signal tag;
tw_buckets() reads it back, and the page judges direction from it (a
breakdown or bearish-divergence pick is a down-call), falling back to the
logged posture for picks that predate the tag.
"""
from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from analysis.final_verdict import combine  # noqa: E402
from analysis.verdict_ledger import log_verdict, tw_buckets  # noqa: E402


def _log(ticker, tags):
    return log_verdict(ticker=ticker, final_verdict=combine(composite_score=55.0, composite_action="WATCH"),
                       entry_price=100.0, composite_score=55.0, source="tomorrow_watchlist",
                       signal_tags=tags)


def test_tw_buckets_reads_only_bucket_tags():
    a = _log("A.NS", ["tw:reversal_bear"])
    b = _log("B.NS", ["rsi_oversold", "tw:breakout"])
    c = _log("C.NS", None)                                  # pre-tag pick
    assert tw_buckets([a, b, c]) == {a: "reversal_bear", b: "breakout"}


def test_tw_buckets_empty_input():
    assert tw_buckets([]) == {}
    assert tw_buckets([None]) == {}
