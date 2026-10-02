"""
Live canary for data/nse_corp_info.py - one of the two providers the
2026-09-06 audit caught silently 404-ing (finding #4). Would have caught
the /api/corporate-info retirement the day it happened.

Marked `slow` per Task 2.5 - stays out of the default `pytest` lane; run
with `pytest -m slow` on a periodic sweep.

The canary asserts three things against the live endpoint:
  1. The URL the fetcher constructs actually returns 200 (URL not retired)
  2. The response is a dict (JSON body parsed cleanly)
  3. At least one of the keys the qualitative-flags pipeline reads is
     present. Absence of ALL known keys is drift; individual keys can go
     empty on a low-news day without failing this check.
"""
from __future__ import annotations

import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from data.nse_corp_info import get_corp_info, get_last_diagnostic  # noqa: E402


# Keys the qualitative-flags pipeline reads (per analysis/qualitative_flags.py).
# Presence of AT LEAST ONE means the endpoint is still shape-compatible.
_EXPECTED_KEYS = {
    "latest_announcements",
    "corporate_actions",
    "shareholdings_patterns",
    "financial_results",
    "borad_meeting",
}


@pytest.mark.slow
def test_nse_corp_info_live_canary_returns_expected_shape() -> None:
    """Live canary against /api/top-corp-info for a large-cap ticker.

    Fails loudly (rather than silently degrading to `{}`) if NSE retires
    the endpoint, changes the response schema, or blocks the request. If
    THIS test starts failing in prod, the qualitative-flags pipeline is
    going dark and someone needs to look at data/nse_corp_info.py.

    NSE is WAF-fronted and well-known for intermittent slowness, so it
    retries once. A timeout on BOTH attempts now FAILS: the 2026-10-03 audit
    found the WAF tarpitting this module's User-Agent on every call, and the
    old SKIP hid a total outage of the flags pipeline.
    """
    result: dict | None = None
    last_diag: dict | None = None
    for attempt in (1, 2):
        result = get_corp_info("RELIANCE", use_cache=False)
        last_diag = get_last_diagnostic("RELIANCE")
        if last_diag and last_diag.get("ok"):
            break

    assert last_diag is not None, "diagnostic must be recorded on any call"
    if not last_diag.get("ok"):
        pytest.fail(
            f"NSE corp-info canary failed: {last_diag}. "
            f"Endpoint may have moved again - see docs/DATA_PROVENANCE_2026-09.md finding #4."
        )

    assert isinstance(result, dict), (
        f"expected dict response, got {type(result).__name__}"
    )

    present = _EXPECTED_KEYS & set(result.keys())
    assert present, (
        f"none of the expected qualitative-flags keys ({sorted(_EXPECTED_KEYS)}) "
        f"present in response. Got keys: {sorted(result.keys())}. "
        f"Response schema likely drifted."
    )
