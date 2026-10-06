"""Pure-function tests for research/decay_attribution.py (offline)."""
import numpy as np
import pandas as pd

from research import decay_attribution as da


def _obs(signal_regime="trend_up", n_weeks=6, n=40, seed=0):
    rng = np.random.default_rng(seed)
    rows = []
    dates = pd.date_range("2024-01-05", periods=n_weeks * 2, freq="W-FRI")
    for k, d in enumerate(dates):
        reg = signal_regime if k % 2 == 0 else "range"
        x = rng.normal(size=n)
        y = (x if reg == signal_regime else rng.normal(size=n)) + 0.1 * rng.normal(size=n)
        for i in range(n):
            rows.append({"date": str(d.date()), "live_regime": reg, "half": "first half",
                         "score90": x[i], "technical": x[i], "momentum": x[i], "volume": 0.0,
                         "rs_score": x[i], "bl_mom20": x[i], "bl_rsi": x[i], "bl_sma200d": x[i],
                         "fwd_20d": y[i], "market_20d": 0.0, "fwd_60d": y[i], "market_60d": 0.0})
    return da.add_targets(pd.DataFrame(rows))


def test_weekly_ic_detects_signal_and_ignores_constant():
    o = _obs()
    up = o[o.live_regime == "trend_up"]
    assert da.weekly_ic(up, "score90", "ex20")["ic"] > 0.9
    assert da.weekly_ic(up, "volume", "ex20")["weeks"] == 0   # constant → skipped, not NaN-crash


def test_attribution_splits_by_regime():
    att = da.attribution(_obs())
    r = att[(att.slice == "live_regime") & (att.feature == "score90")].set_index("value")["ic"]
    assert r["trend_up"] > 0.9 and abs(r["range"]) < 0.3


def test_adoption_rule_needs_both_folds():
    scr = pd.DataFrame([
        {"regime": "range", "fold": "A", "variant": "current", "ic": 0.0, "t": 0.0, "weeks": 30},
        {"regime": "range", "fold": "B", "variant": "current", "ic": 0.0, "t": 0.0, "weeks": 30},
        {"regime": "range", "fold": "A", "variant": "x", "ic": 0.05, "t": 2.5, "weeks": 30},
        {"regime": "range", "fold": "B", "variant": "x", "ic": -0.02, "t": -1.0, "weeks": 30},
        {"regime": "range", "fold": "A", "variant": "y", "ic": 0.05, "t": 2.5, "weeks": 30},
        {"regime": "range", "fold": "B", "variant": "y", "ic": 0.06, "t": 2.2, "weeks": 30},
    ])
    scr["beats_current"] = scr["ic"] > 0
    assert da.adopted(scr) == ["range:y"]


def test_trend_regime_note_only_for_low_info_regimes():
    from dashboard.shared.disclosures import trend_regime_note
    assert "low-information" in trend_regime_note("trend_down")
    assert "range-bound" in trend_regime_note("RANGE")
    assert "slightly negative" in trend_regime_note("trend_down")
    assert "+0.004" in trend_regime_note("range")
    for lbl in ("trend_up", "risk_off", None, "unknown"):
        assert trend_regime_note(lbl) is None
    txt = trend_regime_note("range").lower()
    assert not any(w in txt for w in ("buy", "sell", "avoid"))
