"""Tests for dashboard/shared/table_styles.py (P2 table upgrades)."""
import math

import pandas as pd

from dashboard.shared.table_styles import (
    arrow_fmt, pnl_styler, posture_label, row_tint_css, signed_text_css,
)


def test_arrow_fmt_sign_and_missing():
    f = arrow_fmt(2, "%")
    assert f(1.234) == "▲ 1.23%"
    assert f(-0.5) == "▼ 0.50%"
    assert f(None) == "—"
    assert f(float("nan")) == "—"


def test_arrow_fmt_indian_grouping():
    assert arrow_fmt(0, "", prefix="₹", indian=True)(-1234567) == "▼ ₹12,34,567"


def test_signed_text_css_bold_threshold():
    assert "font-weight:700" in signed_text_css(12, bold_at=10)
    assert "font-weight" not in signed_text_css(5, bold_at=10)
    assert signed_text_css(0) == ""
    assert signed_text_css("x") == ""


def test_row_tint_scales_and_caps():
    lo = row_tint_css(1, full_at=20)
    hi = row_tint_css(100, full_at=20)
    assert "111,170,132" in lo and "0.160" in hi
    assert "199,112,112" in row_tint_css(-5)
    assert row_tint_css(None) == ""


def test_pnl_styler_renders():
    df = pd.DataFrame({"Ticker": ["A", "B"], "P&L %": [5.0, -12.0]})
    html = pnl_styler(df, "P&L %", ["P&L %"],
                      formats={"P&L %": arrow_fmt(1, "%")},
                      bold_at={"P&L %": 10}).to_html()
    assert "▲ 5.0%" in html and "▼ 12.0%" in html
    assert "font-weight: 700" in html.replace("font-weight:700", "font-weight: 700")


def test_posture_label_is_honest_and_shaped():
    for raw in ("STRONG BUY", "BUY", "WATCHLIST", "HOLD", "CAUTION", "EXIT"):
        lbl = posture_label(raw)
        assert "BUY" not in lbl.upper() and "SELL" not in lbl.upper()
        assert any(g in lbl for g in "▲▼◆●")
    assert posture_label("BUY").count("▲") == 1
