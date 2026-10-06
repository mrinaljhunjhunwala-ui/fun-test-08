"""tests/test_bse_corp_info.py — data/bse_corp_info.py.

The two sample payloads below are the `bse` package's own committed sample
responses (github.com/BennyThadikaran/BseIndiaApi/blob/main/src/samples/
announcements.json and actions.json), not invented data — this catches a
real field-name mismatch, not just a mismatch against a guess.

No test here hits the network — `_get_client()` and the underlying `bse`
client are always monkeypatched/stubbed, matching the rest of this repo's
"no live network in the default test suite" convention (see
tests/test_pages_smoke.py's _no_network fixture for the same principle).
"""
from __future__ import annotations

import builtins

import pytest

import data.bse_corp_info as bci

# Real sample payloads from the `bse` package's repo (see module docstring).
_ANNOUNCEMENTS_SAMPLE = {
    "Table": [
        {
            "NEWSSUB": "PAN ELECTRONICS INDIA LTD. - 517397 - Compliances-Reg. 39 (3) "
                      "- Details of Loss of Certificate / Duplicate Certificate",
            "News_submission_dt": "2023-10-20T23:44:22",
            "DissemDT": "2023-10-20T23:44:22.95",
            "HEADLINE": "Issuance of duplicate share certificate in lieu of loss of "
                       "original share certificate.",
        }
    ],
    "Table1": [{"ROWCNT": 1292}],
}
_ACTIONS_SAMPLE = [
    {"scrip_code": 500209, "short_name": "INFY", "Ex_date": "25 Oct 2023",
     "Purpose": "Interim Dividend - Rs. - 18.0000", "exdate": "20231025"},
    {"scrip_code": 520066, "short_name": "JAYBARMARU", "Ex_date": "26 Oct 2023",
     "Purpose": "Stock  Split From Rs.5/- to Rs.2/-", "exdate": "20231026"},
]


class _FakeBSEClient:
    """Stands in for `bse.BSE` — no network, no filesystem download dir."""
    def __init__(self, scripcode_map=None, raise_on_scripcode=False):
        self._scripcode_map = scripcode_map or {"INFY": "500209"}
        self._raise_on_scripcode = raise_on_scripcode

    def getScripCode(self, symbol):
        if symbol in self._scripcode_map:
            return self._scripcode_map[symbol]
        raise ValueError(f"Could not find scrip code for {symbol}")

    def announcements(self, scripcode=None, from_date=None, to_date=None):
        assert scripcode == "500209"
        return _ANNOUNCEMENTS_SAMPLE

    def actions(self, scripcode=None):
        assert scripcode == "500209"
        return _ACTIONS_SAMPLE


@pytest.fixture(autouse=True)
def _fresh_caches(monkeypatch):
    """Each test gets its own cache instances and diagnostic dict, so a
    24h/30d TTLCache from one test can't leak a cached scripcode/result
    into another."""
    monkeypatch.setattr(bci, "_raw_cache", bci.TTLCache(ttl_seconds=86400, name="test"))
    monkeypatch.setattr(bci, "_scripcode_cache",
                        bci.TTLCache(ttl_seconds=86400 * 30, name="test2"))
    monkeypatch.setattr(bci, "_last_diagnostic", {})
    yield


def test_get_corp_info_reshapes_to_nse_schema(monkeypatch):
    monkeypatch.setattr(bci, "_get_client", lambda: _FakeBSEClient())
    result = bci.get_corp_info("INFY")

    ann = result["latest_announcements"]["data"]
    assert ann[0]["subject"].startswith("PAN ELECTRONICS INDIA LTD.")
    assert ann[0]["broadcastdate"] == "2023-10-20"

    actions = result["corporate_actions"]["data"]
    assert actions[0]["purpose"] == "Interim Dividend - Rs. - 18.0000"
    # Machine-readable 'exdate' (YYYYMMDD) converted to ISO, not the
    # human-readable 'Ex_date' string.
    assert actions[0]["exdate"] == "2023-10-25"
    assert actions[1]["exdate"] == "2023-10-26"


def test_get_corp_info_records_ok_diagnostic(monkeypatch):
    monkeypatch.setattr(bci, "_get_client", lambda: _FakeBSEClient())
    bci.get_corp_info("INFY")
    diag = bci.get_last_diagnostic("INFY")
    assert diag["ok"] is True


def test_get_corp_info_not_listed_on_bse_returns_empty(monkeypatch):
    monkeypatch.setattr(bci, "_get_client", lambda: _FakeBSEClient(scripcode_map={}))
    result = bci.get_corp_info("SOMEFAKETICKER")
    assert result == {}
    diag = bci.get_last_diagnostic("SOMEFAKETICKER")
    assert diag["ok"] is False
    assert "not found on BSE" in diag["reason"]


def test_get_corp_info_missing_bse_package_degrades_to_empty(monkeypatch):
    """If `bse` isn't installed, this must return {} and log why — never
    raise ImportError into the caller (refresh_all_flags depends on this)."""
    real_import = builtins.__import__

    def _fake_import(name, *a, **k):
        if name == "bse":
            raise ImportError("No module named 'bse'")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    monkeypatch.setattr(bci, "_bse_client", None)
    monkeypatch.setattr(bci, "_bse_import_error", None)

    result = bci.get_corp_info("INFY")
    assert result == {}
    diag = bci.get_last_diagnostic("INFY")
    assert diag["ok"] is False
    assert "not installed" in diag["reason"]


def test_get_corp_info_transport_failure_degrades_to_empty(monkeypatch):
    class _BoomClient:
        def getScripCode(self, symbol):
            return "500209"
        def announcements(self, **kwargs):
            raise ConnectionError("BSE unreachable")
        def actions(self, **kwargs):
            return []

    monkeypatch.setattr(bci, "_get_client", lambda: _BoomClient())
    result = bci.get_corp_info("INFY")
    assert result == {}
    diag = bci.get_last_diagnostic("INFY")
    assert diag["ok"] is False


def test_scripcode_lookup_is_cached(monkeypatch):
    """A second get_corp_info() call for the same ticker must not re-resolve
    the scripcode — it's cached for 30 days since it's effectively static."""
    calls = {"getScripCode": 0}

    class _CountingClient(_FakeBSEClient):
        def getScripCode(self, symbol):
            calls["getScripCode"] += 1
            return super().getScripCode(symbol)

    monkeypatch.setattr(bci, "_get_client", lambda: _CountingClient())
    bci.get_corp_info("INFY", use_cache=False)
    bci.get_corp_info("INFY", use_cache=False)
    assert calls["getScripCode"] == 1


# --- Audit 2026-10-07: the `bse` package's absence must be visible ---------

def test_package_status_reports_missing(monkeypatch):
    import importlib.util
    monkeypatch.setattr(bci, "_bse_import_error", None)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    ok, why = bci.package_status()
    assert ok is False and "not installed" in why


def test_package_status_reports_import_error(monkeypatch):
    monkeypatch.setattr(bci, "_bse_import_error", "No module named 'bse'")
    ok, why = bci.package_status()
    assert ok is False and "unavailable" in why


def test_data_health_probe_surfaces_missing_bse(monkeypatch):
    from dashboard.shared import data_health as dh
    monkeypatch.setattr(bci, "package_status", lambda: (False, "`bse` package not installed"))
    check = dh.probe_bse_corp_info()
    assert check.status == dh.STATUS_UNAVAILABLE
    assert "BSE fallback disabled" in check.note


def test_requirements_pin_bse():
    from pathlib import Path
    req = (Path(__file__).resolve().parents[1] / "requirements.txt").read_text(encoding="utf-8")
    assert any(line.strip().startswith("bse") for line in req.splitlines())
