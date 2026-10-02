"""An unranked sector must score neutral, never as a tailwind.

Audit 2026-10-03: score_stock() defaulted sector_rank to 7 but measured the
thirds against len(list_sectors()) == 22, so 7 <= 22 // 3 put every unranked
stock in the TOP third — full sector points and a "ranked #7 — a tailwind"
narrative. That hit all "Other"-sector names and every caller that doesn't
pass sector_scores_df (Analyze Stock, Portfolio, Screener, Thesis...).
"""
import pandas as pd
import pytest

from analysis import score as S


@pytest.mark.parametrize("flows,mid_pts", [(None, 2.0), ({"fii_5d": 100.0, "dii_5d": 50.0}, 1.5)])
def test_unknown_rank_scores_the_neutral_middle(flows, mid_pts):
    _, detail = S._score_sentiment({"regime": "normal"}, None, 22, flows_info=flows)
    assert detail["sector"] == mid_pts
    assert detail["sector_ranked"] is False


def test_known_ranks_keep_their_thirds():
    top = S._score_sentiment({"regime": "normal"}, 1, 21)[1]
    bottom = S._score_sentiment({"regime": "normal"}, 21, 21)[1]
    assert top["sector"] == 4.0 and top["sector_ranked"] is True
    assert bottom["sector"] == 0.0


def test_resolve_rank_uses_the_ranked_table_size():
    df = pd.DataFrame({"Rank": [1, 2, 3, 4, 5, 6]}, index=["IT", "Banking", "Auto", "FMCG", "Metal", "Energy"])
    assert S._resolve_sector_rank("Metal", df) == (5, 6)
    # sector missing from the ranking (e.g. "Other") or no ranking at all
    assert S._resolve_sector_rank("Other", df) == (None, 6)
    assert S._resolve_sector_rank("IT", None) == (None, 0)
    assert S._resolve_sector_rank("IT", pd.DataFrame()) == (None, 0)


def test_narrative_does_not_call_an_unranked_sector_a_tailwind():
    n = 120
    idx = pd.bdate_range(end="2026-10-01", periods=n)
    close = pd.Series(range(100, 100 + n), index=idx, dtype=float)
    df = pd.DataFrame({"Open": close, "High": close * 1.01, "Low": close * 0.99,
                       "Close": close, "Volume": 1e5}, index=idx)
    from utils.indicators import add_all_indicators
    cs = S.score_dataframe(add_all_indicators(df), "X.NS", sector_rank=None,
                           sector="Hospitality", n_sectors=22)
    assert cs.sector_rank is None
    assert "tailwind" not in cs.narrative
    assert "#None" not in cs.narrative
