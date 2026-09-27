"""
Transparency / auditability regression tests (audit follow-up):
  • data-source fallback chain logs each failure + the provider that served (P1)
  • survivorship-bias disclosure renders (P2)
  • backtest assumptions section renders (P3)

Run:  py -m pytest tests/test_audit_transparency.py -q
"""
import logging
import os
import sys

import pandas as pd
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ───────────────────────── P1: fallback logging ────────────────────────────────
def _good_df():
    idx = pd.date_range("2024-01-01", periods=3, freq="B")
    return pd.DataFrame({"Open": [1, 2, 3], "High": [1, 2, 3], "Low": [1, 2, 3],
                         "Close": [1, 2, 3], "Volume": [10, 20, 30]}, index=idx)


def _boom(*a, **k):
    raise ConnectionError("simulated outage")


def test_fallback_logs_failed_provider_and_served(monkeypatch, caplog):
    """Stooq fails → Yahoo serves: must log the Stooq failure (provider + symbol +
    exception type) and the provider that ultimately served the data."""
    from data import fetcher, angel_fetcher
    monkeypatch.setattr(angel_fetcher, "is_configured", lambda: False)   # skip Angel tier
    monkeypatch.setenv("NSE_ENABLE_STOOQ", "1")                          # Stooq is opt-in
    monkeypatch.setattr(fetcher, "_fetch_stooq", _boom)                  # Tier 1 fails
    monkeypatch.setattr(fetcher, "_fetch_yahoo_direct",
                        lambda t, period, interval: _good_df())          # Tier 2 serves
    fetcher._FETCH_CACHE.clear()
    fetcher._reset_stooq_breaker()  # isolate from other tests' Stooq failures

    with caplog.at_level(logging.DEBUG, logger="data.fetcher"):
        df = fetcher.fetch_single("FAKE1.NS", period="1y")

    assert not df.empty
    msgs = [r.getMessage() for r in caplog.records]
    # failed provider + symbol + exception type
    assert any("provider=Stooq" in m and "FAKE1.NS" in m and "ConnectionError" in m
               for m in msgs), msgs
    # provider that ultimately succeeded
    assert any("data served" in m and "provider=Yahoo" in m and "FAKE1.NS" in m
               for m in msgs), msgs


def test_fallback_all_providers_fail_raises_and_logs(monkeypatch, caplog):
    """All tiers fail → ValueError raised AND an error logged naming the symbol."""
    from data import fetcher, angel_fetcher
    monkeypatch.setattr(angel_fetcher, "is_configured", lambda: False)
    monkeypatch.setattr(fetcher, "_fetch_stooq", _boom)
    monkeypatch.setattr(fetcher, "_fetch_yahoo_direct", _boom)
    fetcher._FETCH_CACHE.clear()
    fetcher._reset_stooq_breaker()  # isolate from other tests' Stooq failures

    with caplog.at_level(logging.ERROR, logger="data.fetcher"):
        with pytest.raises(ValueError):
            fetcher.fetch_single("FAKE2.NS", period="1y")
    assert any("all providers failed" in r.getMessage() and "FAKE2.NS" in r.getMessage()
               for r in caplog.records)


# ───────────────────────── P2 / P3: disclosures render ──────────────────────────
def _all_text(at) -> str:
    out = []
    for name in ("info", "warning", "error", "success", "markdown", "caption"):
        try:
            out += [getattr(e, "value", "") for e in getattr(at, name)]
        except AttributeError:
            # FIX TEST1 — this used to be a bare `except Exception: pass`, which
            # would also swallow a genuine bug surfaced while iterating a real
            # element collection (e.g. a TypeError from a malformed test
            # fixture) and misattribute it to "this element type doesn't
            # exist." AttributeError is the only expected failure here: some
            # AppTest versions may not expose every element-type accessor.
            pass
    for e in at.expander:
        out.append(getattr(e, "label", "") or "")
    return " ".join(t for t in out if t)


def _run_disclosures(tmp_path):
    from streamlit.testing.v1 import AppTest
    script = tmp_path / "disc.py"
    script.write_text(
        "import sys; sys.path.insert(0, %r)\n"
        "import streamlit as st\n"
        "from dashboard.shared.disclosures import "
        "render_survivorship_notice, render_backtest_assumptions\n"
        "render_survivorship_notice()\n"
        "render_backtest_assumptions()\n" % ROOT,
        encoding="utf-8",
    )
    return AppTest.from_file(str(script), default_timeout=30).run()


def test_survivorship_disclosure_renders(tmp_path):
    at = _run_disclosures(tmp_path)
    assert not at.exception, at.exception
    text = _all_text(at).lower()
    assert "survivorship" in text
    assert "present-day" in text or "delisted" in text or "current" in text


def test_backtest_assumptions_section_renders(tmp_path):
    at = _run_disclosures(tmp_path)
    assert not at.exception, at.exception
    # the assumptions section is an expander with an 'assumptions' label
    assert any("assumption" in (getattr(e, "label", "") or "").lower() for e in at.expander)
    text = _all_text(at).lower()
    # the four required disclosure dimensions are present
    for kw in ("commission", "execution", "slippage", "survivorship"):
        assert kw in text, f"assumptions missing '{kw}'"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
