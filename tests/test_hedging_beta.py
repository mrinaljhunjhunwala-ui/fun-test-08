"""Portfolio beta must not read a missing beta as zero.

Audit 2026-10-03: when the Nifty fetch failed every stock beta came back NaN,
each NaN was added to the weighted sum as 0.0, and the book was reported as
beta 0.0 — "defensive", no hedge needed. A single holding without history was
likewise counted as beta 0, dragging the book's beta down. Unknown is now
excluded and the remaining weights renormalised; no betas at all → None.
"""
import numpy as np
import pandas as pd
import pytest

import analysis.hedging as H

_IDX = pd.bdate_range("2025-01-01", periods=250)
_RNG = np.random.default_rng(0)
_NIFTY_RET = _RNG.normal(0, 0.01, 250)
_NIFTY_DF = pd.DataFrame({"Close": 20_000 * np.cumprod(1 + _NIFTY_RET)}, index=_IDX)


def _stock(beta: float, seed: int) -> pd.DataFrame:
    noise = np.random.default_rng(seed).normal(0, 0.004, 250)
    return pd.DataFrame({"Close": 100 * np.cumprod(1 + beta * _NIFTY_RET + noise)}, index=_IDX)


_BOOK = {"HIGH.NS": _stock(1.5, 1), "LOW.NS": _stock(0.5, 2)}


def _loader(fail_nifty=False, missing=()):
    def _fetch(ticker, period="1y", **_kw):
        if ticker == H._NIFTY:
            if fail_nifty:
                raise ConnectionError("network blocked")
            return _NIFTY_DF
        if ticker in missing:
            return pd.DataFrame()
        return _BOOK[ticker]
    return _fetch


def _holdings():
    return [{"ticker": "HIGH.NS", "value_rs": 500_000}, {"ticker": "LOW.NS", "value_rs": 500_000}]


def test_beta_recovers_weighted_average(monkeypatch):
    monkeypatch.setattr(H, "fetch_single", _loader())
    out = H.calculate_portfolio_beta(_holdings())
    assert out["portfolio_beta"] == pytest.approx(1.0, abs=0.08)
    assert out["beta_coverage_pct"] == pytest.approx(100.0)


def test_nifty_fetch_failure_gives_none_not_zero(monkeypatch):
    monkeypatch.setattr(H, "fetch_single", _loader(fail_nifty=True))
    out = H.calculate_portfolio_beta(_holdings())
    assert out["portfolio_beta"] is None
    assert out["needs_hedge"] is False
    assert "unavailable" in out["interpretation"].lower()
    assert "defensive" not in out["interpretation"].lower()


def test_missing_holding_beta_is_excluded_not_zero(monkeypatch):
    monkeypatch.setattr(H, "fetch_single", _loader(missing=("LOW.NS",)))
    out = H.calculate_portfolio_beta(_holdings())
    # Only HIGH (~1.5) is measurable: the book beta is ~1.5 over 50% coverage,
    # not 0.5 × 1.5 + 0.5 × 0 = 0.75.
    assert out["portfolio_beta"] == pytest.approx(1.5, abs=0.1)
    assert out["beta_coverage_pct"] == pytest.approx(50.0)
    assert "50%" in out["interpretation"]


@pytest.mark.parametrize("beta", [0.3, 0.7, 1.0, 1.3, 1.8])
def test_interpretation_is_descriptive_not_an_instruction(monkeypatch, beta):
    monkeypatch.setattr(H, "fetch_single",
                        lambda t, period="1y", **k: _NIFTY_DF if t == H._NIFTY else _stock(beta, 3))
    text = H.calculate_portfolio_beta([{"ticker": "X.NS", "value_rs": 1}])["interpretation"].lower()
    assert "recommend" not in text and "should" not in text


def test_no_orphan_hedge_instruction_helpers():
    # hedge_sizing / suggest_hedge had no callers, used a stale 25-unit Nifty lot
    # and emitted "Short N Nifty Futures" instructions (rule 1). Removed.
    assert not hasattr(H, "suggest_hedge")
    assert not hasattr(H, "hedge_sizing")
