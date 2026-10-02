"""
Streamlit Cloud's in-place redeploy can leave an older analysis.action_labels
in sys.modules. trade_utils must reload it rather than raise ImportError
(which took down every page on 2026-10-03).
"""
from __future__ import annotations

import importlib
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def test_trade_utils_survives_a_stale_action_labels_module(monkeypatch):
    import analysis.action_labels as real
    stale = types.ModuleType("analysis.action_labels")      # pre-#248 shape
    stale.__file__ = real.__file__
    stale.__spec__ = real.__spec__
    stale.ACTION_DISPLAY_LABELS = dict(real.ACTION_DISPLAY_LABELS)
    stale.action_display_label = real.action_display_label
    monkeypatch.setitem(sys.modules, "analysis.action_labels", stale)
    monkeypatch.delitem(sys.modules, "dashboard.shared.trade_utils", raising=False)

    tu = importlib.import_module("dashboard.shared.trade_utils")
    assert tu.verdict_display_label("BUY") == real.verdict_display_label("BUY")
    assert tu._VERDICT_DISPLAY_LABELS == real.VERDICT_DISPLAY_LABELS
