"""tests/test_data_health.py - unit tests for the data_health module
(Task 2.3). Pure - no Streamlit runtime, no network."""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest import mock

import pytest

from dashboard.shared import data_health as dh


# ── ProviderCheck construction ───────────────────────────────────────────────

def test_provider_check_defaults():
    c = dh.ProviderCheck(name="X", group="market", status=dh.STATUS_HEALTHY)
    assert c.name == "X"
    assert c.warnings == 0
    assert c.last_success_at is None
    assert c.note == ""


# ── _status_from_diagnostic buckets ──────────────────────────────────────────

def test_status_from_diagnostic_no_diag_is_idle():
    status, at, warnings, note = dh._status_from_diagnostic(None)
    assert status == dh.STATUS_IDLE
    assert at is None
    assert warnings == 0


def test_status_from_diagnostic_unavailable_when_reason_supplied():
    status, at, warnings, note = dh._status_from_diagnostic(
        None, unavailable_reason="package missing")
    assert status == dh.STATUS_UNAVAILABLE
    assert note == "package missing"


def test_status_from_diagnostic_not_ok_is_degraded():
    diag = {"ok": False, "at": "2026-09-06T12:00:00", "reason": "HTTP 404"}
    status, _at, _w, note = dh._status_from_diagnostic(diag)
    assert status == dh.STATUS_DEGRADED
    assert "404" in note


def test_status_from_diagnostic_fresh_ok_is_healthy():
    now_iso = datetime.now().isoformat()
    diag = {"ok": True, "at": now_iso}
    status, at, _w, _n = dh._status_from_diagnostic(diag)
    assert status == dh.STATUS_HEALTHY
    assert at == now_iso


def test_status_from_diagnostic_old_ok_is_stale():
    old_iso = (datetime.now() - timedelta(hours=2)).isoformat()
    diag = {"ok": True, "at": old_iso}
    status, _at, _w, _n = dh._status_from_diagnostic(diag)
    assert status == dh.STATUS_STALE


# ── probes: individual providers behave as designed ─────────────────────────

def test_probe_angel_unavailable_when_missing_config(monkeypatch):
    fake_module = mock.MagicMock()
    fake_module.is_configured.return_value = False
    monkeypatch.setitem(__import__("sys").modules, "data.angel_fetcher", fake_module)
    c = dh.probe_angel()
    assert c.name == "Angel One SmartAPI"
    assert c.status == dh.STATUS_UNAVAILABLE


def test_probe_angel_healthy_when_configured(monkeypatch):
    """Configured + no fetcher diag yet -> HEALTHY with 'no fetches yet' note.

    Test explicitly mocks BOTH is_configured (angel_fetcher) AND
    get_last_diagnostic (data.fetcher). Without the second mock, a prior
    test in the same session may have exercised fetch_single and left a
    real Angel diagnostic behind, taking the probe down a different branch.
    """
    import sys
    fake_angel = mock.MagicMock()
    fake_angel.is_configured.return_value = True
    monkeypatch.setitem(sys.modules, "data.angel_fetcher", fake_angel)
    fake_fetcher = mock.MagicMock()
    fake_fetcher.get_last_diagnostic.return_value = {}
    monkeypatch.setitem(sys.modules, "data.fetcher", fake_fetcher)
    c = dh.probe_angel()
    assert c.status == dh.STATUS_HEALTHY


def test_probe_stooq_reads_circuit_breaker(monkeypatch):
    import sys
    fake = mock.MagicMock()
    fake._STOOQ_BREAKER = {"consecutive_failures": 3, "tripped_until": 0.0}
    fake._STOOQ_BREAKER_COOLDOWN = 300
    monkeypatch.setitem(sys.modules, "data.fetcher", fake)
    c = dh.probe_stooq()
    assert c.name == "Stooq CSV"
    # 3 failures but breaker not tripped -> healthy but flagged
    assert c.warnings == 3
    assert c.status in (dh.STATUS_HEALTHY, dh.STATUS_IDLE)


# ── collect_all_health always returns a list ────────────────────────────────

def test_collect_all_health_returns_list():
    checks = dh.collect_all_health()
    assert isinstance(checks, list)
    assert len(checks) > 0
    for c in checks:
        assert isinstance(c, dh.ProviderCheck)
        assert c.status in {
            dh.STATUS_HEALTHY, dh.STATUS_STALE, dh.STATUS_DEGRADED,
            dh.STATUS_UNAVAILABLE, dh.STATUS_IDLE,
        }


# ── HTML render sanity ──────────────────────────────────────────────────────

def test_render_data_health_html_produces_string_with_status_pills():
    checks = [
        dh.ProviderCheck("HealthySrc",   "market",    dh.STATUS_HEALTHY,     "2026-09-06T12:00:00"),
        dh.ProviderCheck("StaleSrc",     "corp_info", dh.STATUS_STALE,       "2026-09-06T09:00:00"),
        dh.ProviderCheck("DegradedSrc",  "news",      dh.STATUS_DEGRADED,    "2026-09-06T11:00:00", note="HTTP 404"),
        dh.ProviderCheck("OfflineSrc",   "market",    dh.STATUS_UNAVAILABLE, note="not configured"),
        dh.ProviderCheck("IdleSrc",      "news",      dh.STATUS_IDLE),
    ]
    html = dh.render_data_health_html(checks)
    assert isinstance(html, str)
    for label in ("HealthySrc", "StaleSrc", "DegradedSrc", "OfflineSrc", "IdleSrc"):
        assert label in html
    # Bucketing labels — unavailable + idle now share the calm "inactive" bucket
    # so the panel stops shouting about providers that are simply not configured.
    assert "providers live" in html
    assert "needs attention" in html
    assert "inactive" in html


def test_render_data_health_html_empty_state():
    html = dh.render_data_health_html([])
    assert "No provider health signal" in html


def test_relative_time_formats():
    now = datetime.now()
    assert dh._relative_time(None) == "-"
    assert dh._relative_time((now - timedelta(seconds=15)).isoformat()).endswith("s ago")
    assert dh._relative_time((now - timedelta(minutes=5)).isoformat()).endswith("m ago")
    assert dh._relative_time((now - timedelta(hours=3)).isoformat()).endswith("h ago")
    assert dh._relative_time((now - timedelta(days=2)).isoformat()).endswith("d ago")


# ── stored EOD datasets (FIX HEALTH-EOD) ─────────────────────────────────────
from datetime import date  # noqa: E402

from dashboard.shared.market_hours import _IST  # noqa: E402

_FRI_NIGHT = datetime(2026, 9, 25, 21, 0, tzinfo=_IST)    # after the 20:00 IST cut-off
_FRI_NOON  = datetime(2026, 9, 25, 12, 0, tzinfo=_IST)
_SATURDAY  = datetime(2026, 9, 26, 10, 0, tzinfo=_IST)


@pytest.fixture(autouse=False)
def fresh_cache(monkeypatch):
    monkeypatch.setattr(dh, "_STORED_CACHE", {})


def test_reference_session_respects_eod_cutoff_and_weekends():
    assert dh._reference_session(_FRI_NIGHT) == date(2026, 9, 25)
    assert dh._reference_session(_FRI_NOON) == date(2026, 9, 24)
    assert dh._reference_session(_SATURDAY) == date(2026, 9, 25)


def test_sessions_behind_counts_trading_days_only():
    assert dh._sessions_behind(date(2026, 9, 25), date(2026, 9, 25)) == 0
    assert dh._sessions_behind(date(2026, 9, 18), date(2026, 9, 25)) == 5   # weekend skipped
    # 2026-10-02 (Gandhi Jayanti) is an NSE holiday: Thu 1st -> Mon 5th is 1 session.
    assert dh._sessions_behind(date(2026, 10, 1), date(2026, 10, 5)) == 1


def _stub_dates(monkeypatch, mapping):
    monkeypatch.setattr(dh, "_latest_stored_dates", lambda: mapping)


def test_probe_stored_datasets_statuses(monkeypatch):
    _stub_dates(monkeypatch, {
        "fii_dii_daily":          "2026-09-25",   # current
        "nse_fii_deriv_daily":    "2026-09-24",   # 1 behind: within grace
        "nse_fno_oi_daily":       "2026-09-21",   # 4 behind: stale
        "nse_option_chain_daily": None,           # empty table
        "nse_delivery_daily":     dh._TABLE_MISSING,
    })
    by = {c.name: c for c in dh.probe_stored_datasets(_SATURDAY)}
    assert len(by) == len(dh._STORED_DATASETS)
    assert by["FII/DII cash flows"].status == dh.STATUS_HEALTHY
    assert by["FII index futures"].status == dh.STATUS_HEALTHY
    oi = by["F&O open interest"]
    assert oi.status == dh.STATUS_STALE
    assert oi.warnings == 4
    assert "4 sessions behind" in oi.note and "fetch_nse_fno_bhavcopy" in oi.note
    assert oi.last_success_at == "2026-09-21"
    assert by["Option chain PCR / pain"].status == dh.STATUS_IDLE
    assert by["Delivery % (bhavcopy)"].status == dh.STATUS_IDLE


def test_probe_stored_datasets_db_unreachable(monkeypatch):
    _stub_dates(monkeypatch, None)
    checks = dh.probe_stored_datasets(_SATURDAY)
    assert {c.status for c in checks} == {dh.STATUS_UNAVAILABLE}


def test_stale_stored_dataset_lands_in_needs_attention(monkeypatch):
    _stub_dates(monkeypatch, {t: "2026-09-10" for _n, t, _h in dh._STORED_DATASETS})
    html = dh.render_data_health_html(dh.probe_stored_datasets(_SATURDAY))
    assert "needs attention" in html
    assert "sessions behind" in html


def test_latest_stored_dates_reads_sqlite_and_caches(monkeypatch, tmp_path, fresh_cache):
    import sqlite3
    import trade_store
    db = tmp_path / "health.db"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(trade_store, "_database_url", lambda: None)
    monkeypatch.setattr(trade_store, "_SQLITE_PATH", str(db))
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE fii_dii_daily (date TEXT PRIMARY KEY)")
        c.executemany("INSERT INTO fii_dii_daily VALUES (?)", [("2026-09-24",), ("2026-09-25",)])
        c.execute("CREATE TABLE nse_fno_oi_daily (symbol TEXT, date TEXT)")

    got = dh._latest_stored_dates()
    assert got["fii_dii_daily"] == "2026-09-25"
    assert got["nse_fno_oi_daily"] is None                    # table exists, no rows
    assert got["nse_option_chain_daily"] == dh._TABLE_MISSING  # never created

    with sqlite3.connect(db) as c:
        c.execute("INSERT INTO fii_dii_daily VALUES ('2026-09-26')")
    assert dh._latest_stored_dates()["fii_dii_daily"] == "2026-09-25"   # served from cache



def test_probe_stooq_reports_disabled_by_default(monkeypatch):
    """FIX STOOQ-OFF: the panel says 'disabled', not 'degraded'."""
    monkeypatch.delenv("NSE_ENABLE_STOOQ", raising=False)
    c = dh.probe_stooq()
    assert c.status == dh.STATUS_UNAVAILABLE
    assert "disabled" in c.note
