"""Tests for chart_helpers.diverging_colors / PLOT_COLORS (P2 diverging rule)."""
from dashboard.shared.chart_helpers import PLOT_COLORS, diverging_colors


def test_hue_by_sign_opacity_by_magnitude():
    c = diverging_colors([10, -10, 5, 0])
    assert c[0] == "rgba(111,170,132,1.00)"
    assert c[1] == "rgba(199,112,112,1.00)"
    assert c[2].endswith("0.68)")          # 0.35 + 0.65 * 0.5 (rounded)
    assert c[3].startswith("rgba(111,170,132")


def test_nan_none_and_all_zero_safe():
    assert len(diverging_colors([None, float("nan")])) == 2
    assert diverging_colors([0, 0]) == ["rgba(111,170,132,0.35)"] * 2


def test_full_at_caps():
    assert diverging_colors([1000], full_at=10)[0].endswith("1.00)")


def test_non_finite_full_at_computes_from_data():
    for bad in (float("nan"), float("inf"), None, 0, -5):
        c = diverging_colors([10, -5], full_at=bad)
        assert "nan" not in "".join(c) and "inf" not in "".join(c)
        assert c[0].endswith("1.00)")


def test_all_nan_or_none_with_nan_full_at():
    import plotly.graph_objects as go
    c = diverging_colors([None, float("nan")], full_at=float("nan"))
    assert c == ["rgba(111,170,132,0.35)"] * 2
    go.Figure(go.Bar(y=[0, 0], marker_color=c))  # must not raise


def test_finite_abs_peak():
    import pandas as pd
    from dashboard.shared.chart_helpers import finite_abs_peak
    assert finite_abs_peak(pd.Series([None, None], dtype=object)) is None
    assert finite_abs_peak(pd.Series([1, -7]), pd.Series([None], dtype=object)) == 7.0
    assert finite_abs_peak() is None


def test_tokens_present():
    assert {"bull", "bear", "accent", "azure", "faint"} <= set(PLOT_COLORS)
