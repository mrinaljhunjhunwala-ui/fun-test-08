"""Audit 2026-10-03 regressions: advice-shaped UI copy and unescaped feed text.

Rule 1 (CLAUDE.md): UI copy that reads as an instruction is a bug. These
strings were found on the live app; the source scan keeps them from coming
back. The flag-card tests cover headline/URL text from RSS and exchange
announcements, which is rendered through unsafe_allow_html.
"""
from pathlib import Path

import pytest

from analysis.qualitative_flags import FlagCategory, FlagSentiment, QualitativeFlag
from dashboard.shared import flags_ui

_PAGES = Path(__file__).resolve().parents[1] / "dashboard" / "pages"

_FORBIDDEN = [
    "BUY options",
    "SELL premium",
    "SELL wide spreads",
    "BUY-grade",
    "on any BUY signal",
    "Consider waiting for a cleaner setup",
    "Recommended: {strat}",
    "never buy naked options",
    "Hedge recommended",
]


@pytest.mark.parametrize("phrase", _FORBIDDEN)
def test_no_instruction_copy_on_pages(phrase):
    hits = [p.name for p in _PAGES.glob("*.py") if phrase in p.read_text(encoding="utf-8")]
    assert not hits, f"{phrase!r} reappeared in {hits}"


def test_analyze_stock_does_not_render_tracebacks():
    src = (_PAGES / "04_analyze_stock.py").read_text(encoding="utf-8")
    assert "st.code(traceback.format_exc())" not in src


@pytest.mark.parametrize("url,ok", [
    ("https://news.google.com/rss/articles/abc", True),
    ("http://example.com/x", True),
    ("javascript:alert(1)", False),
    ("data:text/html;base64,PHNjcmlwdD4=", False),
    ("  JavaScript:alert(1)", False),
    ("not a url", False),
    ("", False),
])
def test_only_web_urls_become_links(url, ok):
    assert flags_ui._is_web_url(url) is ok


def _flag(headline, detail, source):
    return QualitativeFlag(
        ticker="TEST", category=next(iter(FlagCategory)), sentiment=FlagSentiment.AMBER,
        headline=headline, date="2026-10-01", detected_at="2026-10-01T18:00:00",
        source=source, detail=detail,
    )


def test_news_card_with_hostile_text_and_url_is_inert():
    html = flags_ui._flag_card_html(
        _flag('Q2 profit <img src=x onerror=alert(1)> beats', "javascript:alert(1)", "News: Google"))
    assert "<img src=x" not in html and "&lt;img src=x" in html
    assert "href=" not in html and "javascript:" not in html


def test_announcement_card_escapes_detail_and_source():
    html = flags_ui._flag_card_html(
        _flag("Board meeting", "<script>alert(2)</script>", "NSE <b>RSS</b>"))
    assert "<script>" not in html and "<b>RSS</b>" not in html
    assert "&lt;script&gt;" in html


def test_news_card_still_links_a_real_article():
    url = "https://news.google.com/rss/articles/abc?x=1&y=2"
    html = flags_ui._flag_card_html(_flag("HDFC Bank Q2", url, "News: Google"))
    assert 'href="https://news.google.com/rss/articles/abc?x=1&amp;y=2"' in html
    assert ">HDFC Bank Q2</a>" in html
