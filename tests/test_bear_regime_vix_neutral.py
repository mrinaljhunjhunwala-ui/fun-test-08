"""2026-09-24 scoring changes (docs/SCORE_EFFICACY_2026-09-24.md):
bear-regime momentum variant ON by default (cached live regime) and the VIX
sub-component of Sentiment held neutral."""
import analysis.score as sc


def test_regime_weights_default_on(monkeypatch):
    monkeypatch.delenv("NSE_USE_REGIME_WEIGHTS", raising=False)
    assert sc._regime_weights_enabled() is True
    for off in ("0", "false", "off", "no"):
        monkeypatch.setenv("NSE_USE_REGIME_WEIGHTS", off)
        assert sc._regime_weights_enabled() is False


def test_live_regime_label_cached(monkeypatch):
    calls = {"n": 0}

    class _Snap:
        label = "trend_down"

    def fake_snap():
        calls["n"] += 1
        return _Snap()

    import analysis.regime as rg
    monkeypatch.setattr(rg, "snapshot_live", fake_snap)
    monkeypatch.setattr(sc, "_REGIME_LABEL_CACHE", {"t": 0.0, "label": None, "ok": False})
    t = {"now": 1000.0}
    monkeypatch.setattr(sc._regime_time, "time", lambda: t["now"])

    assert sc._live_regime_label() == "trend_down"
    assert sc._live_regime_label() == "trend_down"
    assert calls["n"] == 1                      # served from cache
    t["now"] += sc._REGIME_TTL_OK + 1
    sc._live_regime_label()
    assert calls["n"] == 2                      # refreshed after TTL


def test_live_regime_failure_retries_after_short_ttl(monkeypatch):
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        raise RuntimeError("network")

    import analysis.regime as rg
    monkeypatch.setattr(rg, "snapshot_live", boom)
    monkeypatch.setattr(sc, "_REGIME_LABEL_CACHE", {"t": 0.0, "label": None, "ok": False})
    t = {"now": 1000.0}
    monkeypatch.setattr(sc._regime_time, "time", lambda: t["now"])

    assert sc._live_regime_label() is None
    sc._live_regime_label()
    assert calls["n"] == 1                      # negative-cached
    t["now"] += sc._REGIME_TTL_FAIL + 1
    sc._live_regime_label()
    assert calls["n"] == 2


def test_vix_points_identical_across_regimes():
    regimes = ["complacency", "normal", "elevated", "fear", "panic", "unknown"]
    legacy = {sc._score_sentiment({"regime": r}, 7)[1]["vix"] for r in regimes}
    flows = {sc._score_sentiment({"regime": r}, 7, flows_info={"fii_5d": 1.0, "dii_5d": 1.0})[1]["vix"]
             for r in regimes}
    assert legacy == {6.0}
    assert flows == {5.0}
    # Still reported for context.
    assert sc._score_sentiment({"regime": "fear"}, 7)[1]["vix_regime"] == "fear"


# ── FIX BEAR-NEUTRAL (2026-09-28): technical + RS neutral in bear regimes ────
def _frame(drift=0.003, n=320):
    import numpy as np
    import pandas as pd
    from utils.indicators import add_all_indicators, add_relative_strength
    rng = np.random.RandomState(7)
    idx = pd.bdate_range("2025-01-01", periods=n)
    close = 1000 * np.exp(np.cumsum(drift + rng.normal(0, 0.006, n)))
    df = pd.DataFrame({"Open": close, "High": close * 1.005, "Low": close * 0.995,
                       "Close": close, "Volume": rng.lognormal(13, 0.3, n)}, index=idx)
    df = add_all_indicators(df, groups=sc._SCORE_INDICATOR_GROUPS)
    bench = pd.DataFrame({"Close": 1000 * np.exp(np.cumsum(rng.normal(0.0003, 0.006, n)))}, index=idx)
    return add_relative_strength(df, bench).dropna(subset=["RSI", "ATR"])


def test_bear_regime_neutralises_technical_and_rs(monkeypatch):
    monkeypatch.delenv("NSE_USE_REGIME_WEIGHTS", raising=False)
    df = _frame()
    bear = sc.score_dataframe(df=df, ticker="TESTCO.NS", regime_label="trend_down")
    up   = sc.score_dataframe(df=df, ticker="TESTCO.NS", regime_label="trend_up")
    assert bear.technical_score == 20.0
    assert up.technical_score != 20.0
    _, mdet = sc._score_momentum(df, regime_label="risk_off")
    assert mdet["rs_pts"] == 5.0 and mdet["rs_neutralised"] is True
    assert mdet["rs_pts_raw"] != 5.0


def test_bear_neutral_respects_opt_out(monkeypatch):
    monkeypatch.setenv("NSE_USE_REGIME_WEIGHTS", "0")
    df = _frame()
    bear = sc.score_dataframe(df=df, ticker="TESTCO.NS", regime_label="trend_down")
    plain = sc.score_dataframe(df=df, ticker="TESTCO.NS", regime_label=None)
    assert bear.technical_score == plain.technical_score
    assert bear.score == plain.score


def test_technical_raw_kept_for_descriptive_uses(monkeypatch):
    """Thesis + ledger tags read technical_raw, not the neutralised score."""
    monkeypatch.delenv("NSE_USE_REGIME_WEIGHTS", raising=False)
    df = _frame()
    bear = sc.score_dataframe(df=df, ticker="TESTCO.NS", regime_label="trend_down")
    up = sc.score_dataframe(df=df, ticker="TESTCO.NS", regime_label="trend_up")
    assert bear.technical_score == 20.0
    assert bear.technical_raw == up.technical_score == up.technical_raw
    assert bear.as_dict()["technical_raw"] == bear.technical_raw

    from analysis.thesis.thesis_engine import _from_composite
    from analysis.thesis.thesis_models import ThesisInputs
    for cs in (bear, bear.as_dict()):
        inp = ThesisInputs(ticker="TESTCO")
        _from_composite(inp, cs)
        assert inp.technical_score == bear.technical_raw
