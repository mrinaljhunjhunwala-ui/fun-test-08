"""tests/test_standalone_scripts_smoke.py — import-smoke coverage for standalone scripts.

FIX GAP1 — alerts/check_alerts.py, and every script under research/, had ZERO test
coverage: not a single pytest imported them, so `python -m py_compile` was the only
thing standing between a broken file and production. That gap is exactly how a
copy-paste mistake (an entire unrelated module's content pasted into
alerts/check_alerts.py, overwriting the real Telegram alert script) shipped to `main`
and passed all 301 existing tests undetected — the corrupted file was simply never
touched by anything in tests/.

This file closes that gap cheaply: it discovers every standalone script (one with a
`if __name__ == "__main__":` guard, so a plain import has no side effects — no network
calls, no Telegram sends, no argparse execution) and asserts it at least
    1. compiles (py_compile) — catches syntax errors
    2. imports cleanly — catches broken/misplaced-relative imports (this is exactly
       what would have caught the check_alerts.py incident: the corrupted file failed
       importlib.import_module with "attempted relative import beyond top-level
       package")
    3. still exposes the specific public entry points each script is known to need
       (e.g. check_alerts.py must still have `main` and `send_telegram`) — catches a
       "imports fine but is quietly the wrong file" mistake that a bare import check
       alone would miss.

This deliberately does NOT call main() for any of these — most need live credentials,
network access, or write to real state files, which is out of place in a fast unit
suite. Import-level verification is the right amount of coverage for "did this file
get clobbered," which is the failure mode this test exists to catch.
"""
from __future__ import annotations

import glob
import importlib
import os
import py_compile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# (module import path, path relative to repo root, required public attributes)
_STANDALONE_SCRIPTS = [
    ("alerts.check_alerts",                 "alerts/check_alerts.py",
     ["main", "send_telegram", "_load_state", "_save_state"]),
    ("alerts.check_alerts_v2",              "alerts/check_alerts_v2.py", []),
    ("research.score_efficacy",             "research/score_efficacy.py", ["main"]),
    ("research.portfolio_fit_efficacy",     "research/portfolio_fit_efficacy.py", ["main"]),
    ("research.regime_study",               "research/regime_study.py", ["main"]),
    ("research.score_variants_momentum_vol", "research/score_variants_momentum_vol.py", ["main"]),
    ("research.score_regime_alt_factors",  "research/score_regime_alt_factors.py", ["main"]),
    ("research.score_variants_reversal",    "research/score_variants_reversal.py", ["main"]),
    ("research.score_reversal_sector_neutral", "research/score_reversal_sector_neutral.py", ["main"]),
    ("research.revenue_growth_discovery",   "research/revenue_growth_discovery.py", ["main"]),
    ("research.fundamental_quality",        "research/fundamental_quality.py", ["main"]),
    ("research.score_variants",             "research/score_variants.py", ["main"]),
    ("research.score_variants_volume",       "research/score_variants_volume.py", ["main"]),
    ("research.score_variants_regime",       "research/score_variants_regime.py", ["main"]),
    ("research.score_variants_untapped",     "research/score_variants_untapped.py", ["main"]),
    ("research.score_variants_rs",           "research/score_variants_rs.py", ["main"]),
    ("research.sector_vs_stock",             "research/sector_vs_stock.py", ["main"]),
    ("research.decay_attribution",           "research/decay_attribution.py", ["main"]),
    ("research.fundamentals_historical_variant", "research/fundamentals_historical_variant.py", ["main"]),
    ("research.fundamentals_prospective_collect", "research/fundamentals_prospective_collect.py", ["main"]),
    # New research scripts added in the audit-driven regime work:
    ("research.accuracy_deepdive",          "research/accuracy_deepdive.py", ["main"]),
    ("research.regime_validation",          "research/regime_validation.py", ["main"]),
    ("research.regime_axes_search",         "research/regime_axes_search.py", ["main"]),
    ("research.mean_reversion_factors",     "research/mean_reversion_factors.py", ["main"]),
    ("research.revenue_growth_trend_interaction", "research/revenue_growth_trend_interaction.py", ["main"]),
    ("research.fundamental_growth_regime",  "research/fundamental_growth_regime.py", ["main"]),
    ("research.revenue_growth_reconcile",   "research/revenue_growth_reconcile.py", ["main"]),
    ("research.falling_market_ranking",     "research/falling_market_ranking.py", ["main"]),
    ("research.regime_breadth_study",       "research/regime_breadth_study.py",
     ["main", "parse_full", "fill_cache", "regime_frame", "lag_table"]),
    ("tools.validate_valuation",            "tools/validate_valuation.py", ["main"]),
    ("tools.refresh_flags_batch",           "tools/refresh_flags_batch.py", ["main"]),
    ("tools.composite_golden",              "tools/composite_golden.py", ["main", "build_snapshot", "run_case"]),
    ("tools.purge_test_rows",               "tools/purge_test_rows.py",
     ["main", "find_test_rows", "apply_plan", "is_test_ticker"]),
    ("tools.backfill_ledger",               "tools/backfill_ledger.py", ["main", "run_backfill"]),
    ("tools.fix_offsession_rows",           "tools/fix_offsession_rows.py",
     ["main", "find_plan", "apply_plan"]),
    ("tools.repair_ledger_entry_basis",     "tools/repair_ledger_entry_basis.py",
     ["main", "find_plan", "apply_plan"]),
    ("tools.refresh_universe",              "tools/refresh_universe.py",
     ["main", "parse_index_csv", "build", "diff"]),
]
_IDS = [mod for mod, _, _ in _STANDALONE_SCRIPTS]


def test_standalone_script_inventory_matches():
    # Guard: catches a script silently dropped from this list (or the reverse —
    # a new standalone script added to the repo without ever getting coverage here).
    on_disk = set()
    for pattern in ("alerts/*.py", "research/*.py", "tools/*.py"):
        for p in glob.glob(os.path.join(_ROOT, pattern)):
            name = os.path.basename(p)
            if name in ("__init__.py",):
                continue
            with open(p, encoding="utf-8") as f:
                if '__name__ == "__main__"' in f.read():
                    on_disk.add(os.path.relpath(p, _ROOT).replace(os.sep, "/"))

    covered = {path for _, path, _ in _STANDALONE_SCRIPTS}
    missing = on_disk - covered
    assert not missing, (
        f"Standalone script(s) with a __main__ guard exist but aren't covered by "
        f"this smoke test: {missing}. Add them to _STANDALONE_SCRIPTS above."
    )


@pytest.mark.smoke
@pytest.mark.parametrize("mod_path,rel_path,required_attrs", _STANDALONE_SCRIPTS, ids=_IDS)
def test_script_compiles_and_imports(mod_path, rel_path, required_attrs):
    abs_path = os.path.join(_ROOT, rel_path)
    assert os.path.isfile(abs_path), f"{rel_path} not found on disk"

    # 1. Syntax check
    py_compile.compile(abs_path, doraise=True)

    # 2. Clean import (catches broken/misplaced relative imports, missing deps)
    mod = importlib.import_module(mod_path)

    # 3. The module is actually what it claims to be, not some other file's content
    for attr in required_attrs:
        assert hasattr(mod, attr), (
            f"{rel_path} imported successfully but is missing expected attribute "
            f"'{attr}' — it may have been overwritten with the wrong file's content "
            f"(this is exactly how the check_alerts.py incident happened)."
        )
