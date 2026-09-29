"""Investor Guide (mockup artboard 09): seven chapter tabs, each a document
with eyebrow, serif title, a table of contents whose links resolve to
numbered section heads, a drop-cap lede and quiet tables; no coloured
st.info / st.warning boxes and no raw markdown asterisks left in the HTML.
"""
import os
import re
import socket

import pytest
from streamlit.testing.v1 import AppTest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PAGE = os.path.join(_ROOT, "dashboard", "pages", "15_investor_guide.py")


@pytest.fixture(autouse=True)
def _net(monkeypatch):
    def _blocked(*a, **k):
        raise OSError("network blocked for investor guide layout test")
    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)


def test_guide_renders_as_chapters():
    at = AppTest.from_file(_PAGE, default_timeout=120).run()
    assert not at.exception, at.exception
    html = "\n".join(str(m.value) for m in at.markdown)

    labels = [t.label for t in at.tabs]
    assert labels[:7] == ["Scores & signals", "Indicators", "Stop-loss & risk", "News signals",
                          "Paper trading", "Universe & coverage", "TQS scanner"]
    assert html.count('class="gd-title"') == 7 and html.count('class="gd-lede"') == 7
    assert "chapter 1 of 7" in html and "chapter 7 of 7" in html

    # every TOC link points at a section head that exists
    targets = set(re.findall(r'href="#(g\d-\d)"', html))
    heads = set(re.findall(r'class="gd-h3" id="(g\d-\d)"', html))
    assert targets and targets == heads

    assert "sig-table" in html and not at.dataframe
    assert not at.info and not at.warning
    # raw HTML blocks must have converted their markdown (st.markdown prose renders it)
    blocks = [str(m.value) for m in at.markdown if 'class="gd-' in str(m.value) or "sig-table" in str(m.value)]
    assert blocks and not any("**" in b for b in blocks)
    assert "Paper Trade This Signal" not in html   # button renamed in #210
