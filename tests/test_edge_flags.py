"""Edge-case trading-status flags (audit 2026-10-03 #20). Offline."""
import datetime as dt

import numpy as np
import pandas as pd

from analysis.edge_flags import edge_flags
from data import nse_security_info as NSI

_SEC = '''Symbol,Series,Security Name,Band,Remarks
RELIANCE,EQ,RELIANCE INDUSTRIES LTD,No Band,"-"
ACSTECH,BE,A C S TECHNOLOGIES LIMITED,2,"-"
BADCO,BZ,BAD CO LTD,5,"-"
SMEX,SM,SME EXAMPLE LTD,5,"-"
DUAL,BE,DUAL LISTED,5,"-"
DUAL,EQ,DUAL LISTED,20,"-"
'''
_EQL = '''SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE
RELIANCE,Reliance Industries Limited,EQ,29-NOV-1995,10
NEWCO,New Co Limited,EQ,15-SEP-2026,10
'''


def _frame(n=300, end="2026-10-05", last=None, vol=1e5):
    idx = pd.bdate_range(end=end, periods=n)
    c = np.linspace(100, 120, n)
    df = pd.DataFrame({"Open": c, "High": c * 1.01, "Low": c * 0.99, "Close": c,
                       "Volume": vol}, index=idx)
    if last:
        for k, v in last.items():
            df.iloc[-1, df.columns.get_loc(k)] = v
    return df


def _keys(flags):
    return {f.key for f in flags}


def test_parse_sec_list_prefers_eq_and_reads_bands():
    out = NSI.parse_sec_list(_SEC)
    assert out["RELIANCE"] == {"series": "EQ", "band": None}
    assert out["ACSTECH"] == {"series": "BE", "band": 2.0}
    assert out["DUAL"]["series"] == "EQ" and out["DUAL"]["band"] == 20.0


def test_parse_equity_list_dates():
    out = NSI.parse_equity_list(_EQL)
    assert out["NEWCO"] == dt.date(2026, 9, 15)


def test_get_security_info_degrades_to_empty_offline(monkeypatch):
    monkeypatch.setattr(NSI, "_cache", {"t": 0.0, "ok": False, "data": {}})
    monkeypatch.setattr(NSI, "_get", lambda url: (_ for _ in ()).throw(OSError("blocked")))
    assert NSI.get_security_info("RELIANCE.NS") == {}


def test_normal_large_cap_has_no_flags():
    info = {"series": "EQ", "band": None, "listed_on": dt.date(1995, 11, 29)}
    assert edge_flags(_frame(), info, today=dt.date(2026, 10, 5)) == []


def test_series_flags():
    today = dt.date(2026, 10, 5)
    assert "series_be" in _keys(edge_flags(_frame(), {"series": "BE", "band": 20}, today))
    bz = edge_flags(_frame(), {"series": "BZ", "band": 20}, today)
    assert bz[0].key == "series_bz" and bz[0].tone == "red"


def test_new_listing_uses_listing_date():
    info = {"series": "EQ", "band": 20, "listed_on": dt.date(2026, 9, 15)}
    f = edge_flags(_frame(n=15), info, today=dt.date(2026, 10, 5))
    nl = [x for x in f if x.key == "new_listing"][0]
    assert nl.tone == "red" and "listed 15 Sep 2026" in nl.detail


def test_short_history_without_listing_date_counts_bars():
    assert "short_history" in _keys(edge_flags(_frame(n=120), {}, today=dt.date(2026, 10, 5)))


def test_stale_and_zero_volume():
    df = _frame(end="2026-09-22")
    df.iloc[-4:, df.columns.get_loc("Volume")] = 0
    keys = _keys(edge_flags(df, {"band": 20}, today=dt.date(2026, 10, 5)))
    assert {"no_recent_bars", "zero_volume"} <= keys


def test_upper_circuit_locked():
    df = _frame()
    pc = float(df["Close"].iloc[-2])
    lim = round(pc * 1.05, 2)
    df = _frame(last={"Open": lim, "High": lim, "Low": lim, "Close": lim})
    f = edge_flags(df, {"series": "EQ", "band": 5}, today=dt.date(2026, 10, 5))
    uc = [x for x in f if x.key == "upper_circuit"][0]
    assert uc.tone == "red" and "locked" in uc.label


def test_lower_circuit_touched_not_locked():
    df = _frame()
    pc = float(df["Close"].iloc[-2])
    lim = round(pc * 0.80, 2)
    df = _frame(last={"Open": pc, "High": pc * 1.01, "Low": lim, "Close": lim})
    f = edge_flags(df, {"band": 20}, today=dt.date(2026, 10, 5))
    lc = [x for x in f if x.key == "lower_circuit"][0]
    assert lc.tone == "amber" and "locked" not in lc.label


def test_no_band_fno_stock_never_flags_circuit():
    df = _frame(last={"Close": 200.0, "High": 200.0})
    assert not {"upper_circuit", "lower_circuit"} & _keys(edge_flags(df, {"band": None},
                                                                      today=dt.date(2026, 10, 5)))


def test_never_raises_on_junk():
    assert edge_flags(None, None) == []          # no data is not a "new listing"
    assert isinstance(edge_flags(pd.DataFrame({"x": [1]}), {"band": "x"}), list)


def test_old_listing_is_fast_and_quiet(caplog):
    import logging
    import time
    info = {"series": "EQ", "band": None, "listed_on": dt.date(1995, 11, 29)}
    t0 = time.perf_counter()
    with caplog.at_level(logging.WARNING):
        assert edge_flags(_frame(), info, today=dt.date(2026, 10, 5)) == []
    assert time.perf_counter() - t0 < 0.5
    assert not [r for r in caplog.records if "holiday calendar" in r.getMessage()]


def test_renderer_shows_flags_and_stays_silent_for_normal_stocks(monkeypatch):
    from dashboard.shared import edge_flags_ui as UI
    shown = []
    monkeypatch.setattr(UI.st, "markdown", lambda body, **k: shown.append(body))
    monkeypatch.setattr(NSI, "get_security_info", lambda t: {"series": "BZ", "band": 20})
    UI.render_edge_flags("BADCO.NS", _frame())
    assert shown and "Non-compliant company" in shown[0]
    shown.clear()
    monkeypatch.setattr(NSI, "get_security_info", lambda t: {"series": "EQ", "band": None})
    UI.render_edge_flags("RELIANCE.NS", _frame())
    assert shown == []
    monkeypatch.setattr(NSI, "get_security_info", lambda t: (_ for _ in ()).throw(OSError("x")))
    assert UI.flags_for("X.NS", _frame()) == []
