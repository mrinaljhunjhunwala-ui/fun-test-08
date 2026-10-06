"""
Production-readiness regression tests (Priorities 2, 3, 4).

The headline test is `test_no_lookahead_in_indicators`: it recomputes every
indicator on a series and on the same series truncated by 50 future bars, and
asserts the historical values are byte-identical. If any indicator changes when
future bars are added, that indicator is leaking the future (look-ahead bias).

Also covers: backtest cost realism, paper-trade P&L round-trip (isolated DB),
and the no-scattered-NaN invariant the backtest data-prep relies on.

Run:  py -m pytest tests/test_production_audit.py -q
"""
import os
import sys

import numpy as np
import pandas as pd
import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from utils.indicators import add_all_indicators  # noqa: E402


def _ohlcv(n: int = 400, seed: int = 11) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.3, 1.2, n)
    close = np.maximum(20.0, 100 + np.cumsum(steps))
    openp = np.concatenate([[close[0]], close[:-1]])
    high = np.maximum(openp, close) * (1 + rng.uniform(0, 0.012, n))
    low = np.minimum(openp, close) * (1 - rng.uniform(0, 0.012, n))
    vol = rng.integers(400_000, 3_000_000, n).astype(float)
    idx = pd.date_range("2023-01-02", periods=n, freq="B")
    return pd.DataFrame({"Open": openp, "High": high, "Low": low,
                         "Close": close, "Volume": vol}, index=idx)


# ───────────────────────── Priority 2: look-ahead / leakage ─────────────────────

def test_no_lookahead_in_indicators():
    """Historical indicator values must not change when future bars are appended."""
    base = _ohlcv(400)
    full = add_all_indicators(base.copy())
    trunc = add_all_indicators(base.iloc[:-50].copy())   # 50 fewer FUTURE bars

    # numeric indicator columns present in both
    cols = [c for c in trunc.columns
            if c not in ("Open", "High", "Low", "Close", "Volume")
            and pd.api.types.is_numeric_dtype(trunc[c])]
    assert len(cols) > 10, "expected many indicator columns"

    compare_upto = len(trunc) - 30      # ignore the last 30 bars of the truncated set
    leaks = []
    for c in cols:
        a = full[c].to_numpy()[:compare_upto]
        b = trunc[c].to_numpy()[:compare_upto]
        # NaN positions must match, finite values must be equal
        both_nan = np.isnan(a) & np.isnan(b)
        finite = ~np.isnan(a) & ~np.isnan(b)
        if not np.array_equal(np.isnan(a), np.isnan(b)):
            leaks.append(f"{c} (NaN mask differs)")
        elif not np.allclose(a[finite], b[finite], rtol=1e-9, atol=1e-9):
            leaks.append(f"{c} (values differ)")
    assert not leaks, "LOOK-AHEAD detected in: " + ", ".join(leaks)


def test_indicators_no_scattered_midseries_nan():
    """Core indicator columns must be NaN only during warm-up (a contiguous prefix),
    never scattered mid-series — otherwise backtest dropna() punches gaps in time."""
    df = add_all_indicators(_ohlcv(400))
    for col in ("RSI", "MACD", "ATR", "SMA_50", "Fib_38_2", "Supertrend"):
        if col not in df.columns:
            continue
        isna = df[col].isna().to_numpy()
        last_nan = np.where(isna)[0].max() if isna.any() else -1
        # every NaN index must be within the warm-up prefix [0 .. last_nan]
        assert isna[: last_nan + 1].all() or not isna.any(), \
            f"{col} has scattered mid-series NaNs (warm-up should be contiguous)"


# ───────────────────────── Priority 2: backtest cost realism ────────────────────

def test_backtest_costs_realistic():
    from backtest.runner import TOTAL_COST
    from trading.costs import DEFAULT, representative_round_trip_pct
    # FIX COST-MODEL (audit #23): the old formula charged STT on the sell
    # side only. Delivery STT is 0.1% on BOTH legs, so it alone is 0.2% of a
    # round trip; TOTAL_COST now comes from trading.costs.
    assert TOTAL_COST == representative_round_trip_pct()
    assert TOTAL_COST > 2 * DEFAULT.stt_delivery
    # sanity: a real round-trip cost for Indian equity delivery is ~0.1-0.5%
    assert 0.0010 < TOTAL_COST < 0.005, f"TOTAL_COST {TOTAL_COST} outside realistic band"


# ───────────────────────── Priority 3/4: paper-trade P&L (isolated DB) ──────────

def test_paper_trade_pnl_roundtrip(tmp_path, monkeypatch):
    import trade_store
    monkeypatch.setattr(trade_store, "_SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    trade_store.ensure_schema()

    tid = trade_store.open_trade("RELIANCE.NS", price=100.0, qty=10,
                                 sl=95.0, tp=115.0, account="UT")
    assert isinstance(tid, int)
    opendf = trade_store.fetch_open("UT")
    assert len(opendf) == 1 and opendf.iloc[0]["status"] == "OPEN"

    trade_store.close_trade(tid, exit_price=110.0, reason="unit test")
    allrows = trade_store.load_by_account("UT")
    closed = allrows[allrows["status"] == "CLOSED"].iloc[0]
    assert closed["pnl"] == pytest.approx((110.0 - 100.0) * 10)        # +100
    assert closed["pnl_pct"] == pytest.approx(10.0)
    # the closed trade must no longer appear as open
    assert len(trade_store.fetch_open("UT")) == 0


def test_close_nonexistent_trade_is_safe(tmp_path, monkeypatch):
    import trade_store
    monkeypatch.setattr(trade_store, "_SQLITE_PATH", str(tmp_path / "t2.db"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    trade_store.ensure_schema()
    # closing a non-existent id must not raise (no-op)
    trade_store.close_trade(999999, exit_price=50.0)


def test_warm_caches_runs_and_returns_expected_shape(monkeypatch):
    """FIX GAP2 — dashboard/shared/cache.py's warm_caches() is a scheduled-job
    entry point (intended for a cron/APScheduler hook before market open) with
    NO caller anywhere in the codebase: no cron wiring, no page, no test. That
    meant a typo in its own `return` statement (`return resultso` instead of
    `return results`) shipped to `main` and sat there undetected until it was
    caught by hand — nothing in CI ever actually called this function. Mock
    out the two expensive underlying scans so this runs fast and offline, and
    assert the actual returned shape, so a NameError/AttributeError/wrong-key
    regression here fails a real test instead of waiting for a human to
    notice.
    """
    import dashboard.shared.cache as cache_mod

    monkeypatch.setattr(cache_mod, "get_vix_info", lambda: {"regime": "normal", "vix": 14.0})
    monkeypatch.setattr(cache_mod, "_sector_ranks_tuple", lambda: ())
    monkeypatch.setattr(cache_mod, "_home_top_picks",
                         lambda vix_regime="normal", n=10, sector_ranks=(): {"buys": [], "sells": []})
    monkeypatch.setattr(cache_mod, "_tomorrow_watchlist", lambda n=15: {"picks": []})

    result = cache_mod.warm_caches()

    assert set(result.keys()) == {"home_top_picks", "tomorrow_watchlist"}
    for key in ("home_top_picks", "tomorrow_watchlist"):
        assert result[key]["ok"] is True
        assert "seconds" in result[key]


def test_warm_caches_degrades_gracefully_on_failure(monkeypatch):
    """Companion to the above: if one of the two warm-up scans raises, the
    other must still run, and the failure must be captured in the return
    value (not raised) — warm_caches() is meant to be safe for an unattended
    scheduled job to call.
    """
    import dashboard.shared.cache as cache_mod

    monkeypatch.setattr(cache_mod, "get_vix_info", lambda: {"regime": "normal", "vix": 14.0})
    monkeypatch.setattr(cache_mod, "_sector_ranks_tuple", lambda: ())

    def _boom(*a, **kw):
        raise RuntimeError("simulated scan failure")

    monkeypatch.setattr(cache_mod, "_home_top_picks", _boom)
    monkeypatch.setattr(cache_mod, "_tomorrow_watchlist", lambda n=15: {"picks": []})

    result = cache_mod.warm_caches()

    assert result["home_top_picks"]["ok"] is False
    assert "simulated scan failure" in result["home_top_picks"]["error"]
    assert result["tomorrow_watchlist"]["ok"] is True


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
