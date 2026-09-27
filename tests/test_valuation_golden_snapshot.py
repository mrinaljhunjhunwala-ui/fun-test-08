"""tests/test_valuation_golden_snapshot.py — OFFLINE valuation regression (Part 2).

NO network. Two layers of protection:

1. **Replay regression** — for every ticker the snapshot stores the captured
   `ValuationInputs`. This test reconstructs each one and runs the PURE `assess()` engine,
   asserting the posture / confidence / branch / guards still match. Any change to E1-v2
   logic that alters an outcome fails CI deterministically (posture / confidence / guard
   drift). The intentional-update workflow: regenerate with
   `python tools/validate_valuation.py --update` and review the diff.

2. **Structure + known V1 postures** — schema, the 62 tickers, required fields, spot-checks.

The LIVE drift check (re-fetching Yahoo) is the diagnostic `tools/validate_valuation.py`,
which is NOT run in CI.
"""
from __future__ import annotations

import json
import os

import pytest

from analysis.fundamentals.valuation_decision import ValuationInputs, assess

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SNAPSHOT = os.path.join(_ROOT, "data", "valuation_golden_snapshot.json")

_EXPECTED_TICKERS = {
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "HINDUNILVR", "ITC", "LT",
    "BHARTIARTL", "MARUTI", "SBIN", "KOTAKBANK", "AXISBANK", "BANKBARODA", "PNB",
    "INDUSINDBK", "FEDERALBNK", "BAJFINANCE", "BAJAJFINSV", "CHOLAFIN", "MUTHOOTFIN",
    "SHRIRAMFIN", "SBICARD", "LICHSGFIN", "ICICIGI", "ICICIPRULI", "HDFCLIFE", "SBILIFE",
    "ONGC", "NTPC", "POWERGRID", "BHEL", "SAIL", "GAIL", "IOC", "COALINDIA", "NMDC",
    "SIEMENS", "ABB", "CUMMINSIND", "THERMAX", "BEL", "HAVELLS", "NESTLEIND", "BRITANNIA",
    "DABUR", "MARICO", "TATACONSUM", "COLPAL", "WIPRO", "HCLTECH", "TECHM", "LTM",
    "PERSISTENT", "COFORGE", "SRF", "DEEPAKNITR", "POLYCAB", "DIXON", "TATAELXSI",
    "TATASTEEL", "HINDALCO",
}
_REQUIRED_FIELDS = {"posture", "confidence", "branch", "guards_triggered", "inputs"}


@pytest.fixture(scope="module")
def snap():
    assert os.path.exists(_SNAPSHOT), "golden snapshot file missing"
    with open(_SNAPSHOT, "r", encoding="utf-8") as f:
        return json.load(f)


# ── Layer 1: deterministic replay regression (the core CI guard) ─────────────────
def _replay(entry):
    inp = ValuationInputs(**entry["inputs"])
    va = assess(inp)
    return {
        "posture": va.posture,
        "confidence": va.confidence,
        "branch": va.sector_branch,
        "guards_triggered": [va.triggered_guard] if va.triggered_guard else [],
    }


def test_replay_no_drift(snap):
    """Re-run the pure engine on every captured input; fail on any posture/confidence/
    branch/guard drift. Offline + deterministic."""
    drift = []
    for tk, entry in snap["tickers"].items():
        got = _replay(entry)
        for field in ("posture", "confidence", "branch", "guards_triggered"):
            if got[field] != entry.get(field):
                drift.append(f"{tk}.{field}: snapshot={entry.get(field)} now={got[field]}")
    assert not drift, "Valuation regression drift:\n  " + "\n  ".join(drift)


# ── Layer 2: structure + known V1 postures ───────────────────────────────────────
def test_snapshot_valid_json_and_schema(snap):
    assert snap.get("schema_version") == 1
    assert snap.get("source") == "docs/V1_NSE_VALIDATION_REPORT.md"
    assert isinstance(snap.get("tickers"), dict)


def test_snapshot_contains_all_62_v1_tickers(snap):
    assert set(snap["tickers"].keys()) == _EXPECTED_TICKERS
    assert len(snap["tickers"]) == 62


def test_every_entry_has_required_fields(snap):
    for tk, entry in snap["tickers"].items():
        assert _REQUIRED_FIELDS <= set(entry.keys()), f"{tk} missing fields"
        assert isinstance(entry["guards_triggered"], list)
        assert entry["confidence"] in ("high", "medium", "low", "none")
        assert isinstance(entry["inputs"], dict)


def test_spotcheck_known_postures(snap):
    t = snap["tickers"]
    assert t["ICICIBANK"]["posture"] == "SUPPORTED_BY_ROE"
    assert t["HDFCBANK"]["posture"] == "DEMANDING_VS_ROE"
    assert t["TCS"]["posture"] == "SUPPORTED_BY_QUALITY"
    assert t["SBILIFE"]["posture"] == "INSUFFICIENT_EVIDENCE"
    assert "H4-insurance" in t["SBILIFE"]["guards_triggered"]
    assert t["SAIL"]["posture"] == "INSUFFICIENT_EVIDENCE"
    assert "TR-cyclical-trough" in t["SAIL"]["guards_triggered"]


def test_powergrid_not_insufficient_after_part1_fix(snap):
    pg = snap["tickers"]["POWERGRID"]
    assert pg["posture"] != "INSUFFICIENT_EVIDENCE"
    assert pg["guards_triggered"] == []
    assert pg["inputs"].get("is_regulated_utility") is True
