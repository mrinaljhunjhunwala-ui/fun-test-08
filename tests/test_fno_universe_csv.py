"""F&O universe from NSE's official list (audit #30/#22). Offline."""
import importlib
import socket

import pytest

import data.fno_universe as fu
from tools import refresh_fno_universe as rf

_MKTLOTS = (
    "UNDERLYING                          ,SYMBOL    ,OCT-26     ,NOV-26     \n"
    "NIFTY 50                            ,NIFTY     ,65         ,65         \n"
    "NIFTY BANK                          ,BANKNIFTY ,30         ,30         \n"
    "Derivatives on Individual Securities,Symbol    ,OCT-26     ,NOV-26     \n"
    "ADANI PORT & SEZ LTD                ,ADANIPORTS,475        ,475        \n"
    "360 ONE WAM LIMITED                 ,360ONE    ,500        ,500        \n"
)


def test_parse_mktlots_skips_indices():
    rows = rf.parse_mktlots(_MKTLOTS)
    assert [r["Symbol"] for r in rows] == ["360ONE", "ADANIPORTS"]
    assert rows[1]["LotSize"] == "475"


def test_parse_underlying_api():
    text = ('{"data":{"IndexList":[{"symbol":"NIFTY"}],'
            '"UnderlyingList":[{"symbol":"ABB","underlying":"ABB India"}]}}')
    assert rf.parse_underlying_api(text) == [{"Symbol": "ABB", "Company": "ABB India", "LotSize": ""}]


def test_dry_run_writes_nothing(monkeypatch, tmp_path):
    out = tmp_path / "x.csv"
    monkeypatch.setattr(rf, "OUT_PATH", str(out))
    monkeypatch.setattr(rf, "fetch", lambda: [{"Symbol": f"S{i}", "Company": "", "LotSize": "1"}
                                              for i in range(150)])
    assert rf.main([]) == 0 and not out.exists()
    assert rf.main(["--apply"]) == 0 and out.exists()


def test_refuses_tiny_list(monkeypatch):
    monkeypatch.setattr(rf, "fetch", lambda: [])
    assert rf.main([]) == 1


def test_csv_loaded_at_import_without_network(monkeypatch):
    def _no_net(*a, **k):
        raise AssertionError("network used at import")
    monkeypatch.setattr(socket.socket, "connect", _no_net)
    mod = importlib.reload(fu)
    assert mod.FNO_SOURCE == "csv"
    syms = mod.list_fno_tickers()
    assert len(syms) >= 150
    assert "NIFTY" not in syms and "BANKNIFTY" not in syms
    assert mod._FALLBACK_TICKERS <= syms          # old curated list is a subset
    assert mod.is_fno_eligible("RELIANCE.NS") and mod.fno_lot_size("RELIANCE") > 0


def test_missing_csv_falls_back(monkeypatch, tmp_path):
    monkeypatch.setattr(fu, "_CSV_PATH", str(tmp_path / "missing.csv"))
    with pytest.raises(OSError):
        fu._load_csv(fu._CSV_PATH)


def test_larger_list_changes_no_score_with_pillar_off(monkeypatch):
    """With NSE_USE_POSITIONING_PILLAR unset (the default) a newly eligible
    name scores identically whether or not it's on the F&O list."""
    import numpy as np
    import pandas as pd

    import analysis.score as sc
    monkeypatch.delenv("NSE_USE_POSITIONING_PILLAR", raising=False)
    assert sc._positioning_pillar_enabled() is False
    rng = np.random.default_rng(0)
    close = 100 + np.cumsum(rng.normal(0, 1, 300))
    idx = pd.bdate_range("2025-01-01", periods=300)
    df = pd.DataFrame({"Open": close, "High": close + 1, "Low": close - 1, "Close": close,
                       "Volume": rng.integers(1e5, 2e5, 300)}, index=idx)
    pos = {"oi_regime": "long_buildup", "pcr": 1.1}
    import data.fno_universe as live
    monkeypatch.setattr(live, "is_fno_eligible", lambda t: True)
    a = sc.score_dataframe(df, "SWIGGY.NS", positioning_info=pos)
    monkeypatch.setattr(live, "is_fno_eligible", lambda t: False)
    b = sc.score_dataframe(df, "SWIGGY.NS", positioning_info=pos)
    assert a.is_fno and not b.is_fno
    assert a.score == b.score and a.positioning_score is None
