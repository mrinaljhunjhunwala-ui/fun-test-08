"""tests/test_pages_smoke.py — CI page smoke coverage (Part 1).

Loads every Streamlit page headlessly via AppTest and asserts it renders with **no uncaught
exception** (which also catches unresolved imports / NameErrors — the P3 risk). Made
deterministic and CI-safe by **fail-fast network blocking**: outbound sockets raise
immediately, so each page takes its graceful degraded path (the pages already wrap their
fetches — P2) and renders quickly, with no dependence on live Yahoo/NSE.

Marked `smoke`; runs in the default suite and CI. The one slow backtest e2e is separate
(`-m slow`).
"""
from __future__ import annotations

import glob
import os
import socket
import threading
import time

import pytest
from streamlit.testing.v1 import AppTest

_PAGES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "dashboard", "pages")
_PAGES = sorted(glob.glob(os.path.join(_PAGES_DIR, "*.py")))
_IDS = [os.path.basename(p) for p in _PAGES]


@pytest.fixture(scope="module", autouse=True)
def _no_network():
    """Block outbound network so pages take the fast, deterministic degraded path.

    Module-scoped on purpose. As a per-test fixture this block was torn down at
    the end of each test, but the pages under test start their own background
    threads (utils/live_price.get_live_prices_batch runs a ThreadPoolExecutor,
    and its caller returns as soon as its wait expires — see FIX LP3 there).
    Those threads outlive the test that spawned them, so tearing the block down
    per-test let them escape into REAL network calls, with real 6–10s per-tier
    timeouts, while the NEXT test was running. That is what made this file
    non-deterministic and monstrously slow in a full-suite run — the analyze-
    stock page measured 7,754s (2h 9m) in-suite versus ~60s in isolation, and
    which page ended up carrying the blame moved around between runs.

    Holding the block for the whole module means a leaked thread still fails
    fast no matter which test is executing when it wakes up.
    """
    def _blocked(*args, **kwargs):
        raise OSError("network blocked for page smoke test")

    orig_connect = socket.socket.connect
    orig_create  = socket.create_connection
    socket.socket.connect = _blocked
    socket.create_connection = _blocked
    try:
        yield
    finally:
        socket.socket.connect = orig_connect
        socket.create_connection = orig_create


@pytest.fixture(autouse=True)
def _join_page_threads():
    """Wait for the background threads a page starts before the test ends.

    Pages start daemon threads that outlive AppTest.run() — Tomorrow's
    Watchlist's ``_tw_worker`` scan (which logs picks via log_verdict), the
    Command Centre picks scan, live-price pools. conftest.py points
    trade_store at a per-test tmp SQLite by patching a module global, so a
    thread still running when the NEXT test starts writes into THAT test's
    database (seen as an intermittent failure in test_purge_test_rows.py:
    real tickers appearing in its ledger). This fixture tears down before
    conftest's isolation does, so late writes land in this test's own DB.
    With the network blocked the threads finish within seconds; the deadline
    only guards against a genuine hang.
    """
    before = set(threading.enumerate())
    yield
    deadline = time.monotonic() + 120
    for t in set(threading.enumerate()) - before:
        # threading.enumerate() also lists threads still in "limbo" (start()
        # called, bootstrap not yet run); join() on one raises "cannot join
        # thread before it is started" — seen as a CI flake on a pool worker.
        # Wait for it to finish starting, then join as usual.
        while True:
            try:
                t.join(timeout=max(0.0, deadline - time.monotonic()))
                break
            except RuntimeError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)


def test_all_pages_present():
    # Guard: the universe of pages the smoke covers (catches an accidentally-dropped page).
    # 17 after merging Macro Dashboard + Market Breadth → Market Internals.
    # 18 after adding 18_tqs_scanner.py.
    # Still 18 after: merging Market Overview + Market Internals → Overview
    # (net -1: 09_market_internals.py deleted) and adding 19_quality_watch.py
    # (net +1) — the two changes cancel out in the page COUNT, but _IDS below
    # will differ (09_market_internals.py gone, 19_quality_watch.py present).
    # Still 18 after: merging OI & Options → Intraday Trader (net -1:
    # 10_oi_options.py deleted, its 3 tabs now live in Intraday Trader) and
    # adding 20_deep_dive.py (net +1) — same cancel-out pattern as above.
    # This assertion will read 19 (not 18) until 10_oi_options.py is manually
    # deleted from the repo — Claude cannot delete files from GitHub directly.
    #
    # 2026-08-30 sprint (calibration/FII-DII): +2 pages
    #   * 21_verdict_calibration.py (Tier 1 #1/#2/#3 — verdict ledger + calibration)
    #   * 22_fii_dii_flows.py       (Tier 2 #6 — FII/DII flow tracker)
    # Renumbered from 18/19 to avoid collision with existing 18_tqs_scanner.py
    # and 19_quality_watch.py. Both are wired into dashboard/shared/nav.py.
    #
    # 2026-08-30 sprint (Analysis-page consolidation): −1 page
    #   * 13_swing_checklist.py removed — its 8-factor go/no-go was folded
    #     into 04_analyze_stock.py via dashboard/shared/checklist_ui.py.
    #
    # 2026-09-09 (PR 3 alerts follow-through): +1 page
    #   * 13_stock_journal.py — reclaims the number vacated by the swing
    #     checklist removal. Backing store is data/stock_journal.csv +
    #     alerts/journal_store.py (already shipped in PR #74). Weekly
    #     digest reads entries added in the last 7 days and reviews due
    #     in the next 7 days.
    #
    # 2026-09-18 (Slice 2 · Deep Dive fold-in): −1 page
    #   * 20_deep_dive.py removed — its indicators / candlestick / mini-
    #     report views were folded into 04_analyze_stock.py as an 8th tab
    #     via dashboard/shared/deep_dive_tab.py. See PR #115.
    assert len(_PAGES) == 19, f"expected 19 pages, found {len(_PAGES)}: {_IDS}"


@pytest.mark.smoke
@pytest.mark.parametrize("page", _PAGES, ids=_IDS)
def test_page_loads_without_exception(page):
    # Timeout bumped 120 -> 240s (2026-09-04): Streamlit's AppTest is
    # materially slower on Python 3.11 (CI runner) than on 3.12+. Command
    # Centre's ticker sweep + panel rendering routinely lands around
    # 120-130s on 3.11 even after the per-process bench / flows caches in
    # analysis/score.py cut the per-ticker DB work significantly. 240s
    # keeps a comfortable ceiling for the slowest page on the slowest
    # runner without hiding any real regression (a genuine hang is minutes,
    # not just tens of seconds over the old ceiling).
    name = os.path.basename(page)

    # Compile first. Streamlit's ScriptRunner catches compile errors (SyntaxError,
    # IndentationError, ...) itself: it logs "Script compilation error" and fires
    # SCRIPT_STOPPED_WITH_COMPILE_ERROR instead of emitting an exception element.
    # AppTest ignores that event, so ``at.exception`` stays EMPTY and the test
    # used to pass on a page that could not even parse (16_angel_one.py regression).
    with open(page, encoding="utf-8") as fh:
        source = fh.read()
    try:
        compile(source, page, "exec")
    except SyntaxError as e:
        pytest.fail(f"{name} failed to compile: {type(e).__name__}: {e}")

    at = AppTest.from_file(page, default_timeout=240).run()
    # AppTest.exception is an ElementList (falsy when empty), not None.
    assert not at.exception, (
        f"{name} raised an uncaught exception: "
        + "; ".join(str(e.value) for e in at.exception)
    )
    # Belt-and-braces for any other silent bail-out (compile error on a
    # Streamlit version whose handling differs, script never started, ...):
    # render_sidebar() always populates the sidebar, so counting it would make
    # this vacuous. Every page renders at least its top bar / title in the MAIN
    # area, so an empty main tree means the page body never ran.
    rendered = len(at.main.children)
    assert rendered > 0, f"{name} rendered no main-area elements — page body did not run"
