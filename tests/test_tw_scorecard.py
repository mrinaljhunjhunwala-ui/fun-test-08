"""analysis.tw_scorecard — Nifty-relative and market-split stats for settled picks."""
from analysis.tw_scorecard import settle_stats, split_by_market


def test_down_call_in_falling_market_hits_but_barely_beats_nifty():
    # Stock -3%, Nifty -2.5%: a Hit on the ±2% rule, only +0.5% vs the index.
    s = settle_stats([(-1, -3.0, -2.5)])
    assert s["hit"] == 1 and s["hit_rate"] == 100.0
    assert s["beat_rate"] == 100.0
    assert abs(s["mean_vs_nifty"] - 0.5) < 1e-9


def test_up_call_that_rose_less_than_nifty_does_not_beat_it():
    s = settle_stats([(1, 2.5, 4.0)])
    assert s["hit"] == 1
    assert s["beat_rate"] == 0.0
    assert abs(s["mean_vs_nifty"] + 1.5) < 1e-9


def test_band_and_worst():
    s = settle_stats([(1, 2.0, 0.0), (1, -2.0, 0.0), (1, 1.0, 0.0), (-1, 7.0, 0.0)])
    assert (s["hit"], s["miss"], s["flat"], s["n"]) == (1, 2, 1, 4)
    assert s["worst"] == -7.0          # a down-call that rose 7%


def test_missing_nifty_kept_for_hit_rate_but_not_vs_nifty():
    s = settle_stats([(1, 3.0, None), (1, 3.0, 1.0)])
    assert s["n"] == 2 and s["n_vs"] == 1
    assert s["mean_vs_nifty"] == 2.0


def test_bad_rows_skipped_and_empty_is_safe():
    s = settle_stats([(1, None, 0.0), (0, 5.0, 0.0), (1, float("nan"), 0.0)])
    assert s["n"] == 0 and s["hit_rate"] is None and s["beat_rate"] is None and s["worst"] is None


def test_split_by_market_groups_and_orders():
    rows = [(-1, -4.0, -2.0), (1, -3.0, -2.0), (-1, 1.0, 1.5), (1, 2.0, 1.5), (1, 2.0, None)]
    out = split_by_market(rows)
    assert [(m, side) for m, side, _ in out] == [
        ("Nifty down", "bearish"), ("Nifty down", "bullish"),
        ("Nifty up", "bearish"), ("Nifty up", "bullish"),
    ]
    assert sum(s["n"] for _, _, s in out) == 4   # the row without Nifty is dropped
