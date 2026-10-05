"""Regression tests for the 2026-09-24 engine audit (branch fix-engine-audit).

Each test pins one audited defect:
  1. RS zeroing when the benchmark lacks the stock's last bar + bench/flows cache TTL
  2. Missing SMAs must not award SMA-stack points
  3. TQS up/down volume ratio with zero down-volume
  4. Backtest commission is per-side, table formatting helpers
  5. Verdict ledger: trading-day horizons, alpha unavailable without Nifty, win-vs-Nifty
  6. Quality Watch score: complete posture mapping, sector-aware ratios
"""
from __future__ import annotations

import datetime as dt
import inspect
import math

import numpy as np
import pandas as pd
import pytest


def _ohlcv(n: int = 320, seed: int = 1, end: str = "2026-09-23") -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(end=end, periods=n)
    close = 100 * np.exp(np.cumsum(rng.normal(0.001, 0.01, n)))
    return pd.DataFrame({"Open": close, "High": close * 1.01, "Low": close * 0.99,
                         "Close": close, "Volume": rng.integers(1e5, 2e5, n).astype(float)},
                        index=idx)


# ── 1. RS zeroing ────────────────────────────────────────────────────────────

def test_rs_score_uses_ffilled_benchmark_when_last_bar_missing():
    from utils.indicators import add_relative_strength
    stock = _ohlcv(seed=1)
    bench = _ohlcv(seed=2).iloc[:-1]           # Nifty lags one session
    out = add_relative_strength(stock.copy(), bench)
    assert not math.isnan(out["RS_Score"].iloc[-1])
    # Same result as if the bench's last bar were the carried-forward value
    bench_full = pd.concat([bench, bench.iloc[[-1]].set_axis([stock.index[-1]])])
    ref = add_relative_strength(stock.copy(), bench_full)
    assert out["RS_Score"].iloc[-1] == pytest.approx(ref["RS_Score"].iloc[-1])


def test_rs_score_is_nan_not_zero_when_benchmark_stale_beyond_tolerance():
    from utils.indicators import add_relative_strength
    stock = _ohlcv(seed=1)
    bench = _ohlcv(seed=2).iloc[:-5]           # 5 bars stale > 2-bar ffill limit
    out = add_relative_strength(stock.copy(), bench)
    assert math.isnan(out["RS_Score"].iloc[-1])


def test_bench_cache_negative_ttl_and_staleness(monkeypatch):
    import analysis.score as sc
    calls = {"n": 0}

    def _fail(*a, **k):
        calls["n"] += 1
        return None

    monkeypatch.setattr(sc, "_fetch_single", _fail, raising=False)
    sc._BENCH_CACHE.clear()
    t0 = 1_000_000.0
    monkeypatch.setattr(sc, "_now", lambda: t0)
    assert sc._get_bench_cached("2y") is None
    assert sc._get_bench_cached("2y") is None
    assert calls["n"] == 1                      # negative-cached briefly
    monkeypatch.setattr(sc, "_now", lambda: t0 + sc._BENCH_NEG_TTL_S + 1)
    assert sc._get_bench_cached("2y") is None
    assert calls["n"] == 2                      # failure NOT cached permanently

    good = _ohlcv()
    monkeypatch.setattr(sc, "_fetch_single", lambda *a, **k: good, raising=False)
    sc._BENCH_CACHE.clear()
    monkeypatch.setattr(sc, "_now", lambda: t0)
    assert sc._get_bench_cached("2y") is good
    monkeypatch.setattr(sc, "_now", lambda: t0 + sc._BENCH_TTL_S + 1)
    fresh = _ohlcv(seed=9)
    monkeypatch.setattr(sc, "_fetch_single", lambda *a, **k: fresh, raising=False)
    assert sc._get_bench_cached("2y") is fresh  # TTL expiry refetches
    sc._BENCH_CACHE.clear()


def test_flows_cache_has_ttl(monkeypatch):
    import analysis.score as sc
    sc._FLOWS_CACHE.clear()
    monkeypatch.setattr(sc, "_now", lambda: 0.0)
    sc._FLOWS_CACHE["flows"] = (0.0, {"fii_5d": 1.0})
    assert sc._get_flows_cached() == {"fii_5d": 1.0}
    monkeypatch.setattr(sc, "_now", lambda: sc._FLOWS_TTL_S + 1)
    import analysis.fii_dii as fd
    monkeypatch.setattr(fd, "load_history", lambda days=5: pd.DataFrame())
    assert sc._get_flows_cached() is None
    sc._FLOWS_CACHE.clear()


# ── 2. Missing SMA ───────────────────────────────────────────────────────────

def test_missing_smas_award_no_sma_points():
    from analysis.score import _score_technical
    df = pd.DataFrame({"Close": [100.0, 101.0], "RSI": [55, 55], "MACD": [0, 0],
                       "MACD_Signal": [0, 0], "MACD_Hist": [0, 0], "ADX": [20, 20]})
    _, pts = _score_technical(df)
    assert pts["sma"] == 0.0
    df["SMA_200"] = [90.0, 90.0]                # only 200 known, price above
    _, pts = _score_technical(df)
    assert pts["sma"] == 4.0


# ── 3. TQS volume ratio ──────────────────────────────────────────────────────

def test_tqs_vol_ratio_zero_down_volume_is_max():
    from analysis import trend_quality_score as tqs
    src = inspect.getsource(tqs)
    assert "FIX TQS-VOLRATIO" in src
    up = pd.Series([1.0] * 25)
    down = pd.Series([0.0] * 25)
    r = tqs._up_down_volume_ratio(up, down)
    assert r.iloc[-1] == pytest.approx(3.0)
    r2 = tqs._up_down_volume_ratio(pd.Series([0.0] * 25), down)
    assert r2.iloc[-1] == pytest.approx(1.0)


# ── 4. Backtest ──────────────────────────────────────────────────────────────

def test_backtest_commission_is_per_side():
    # FIX COST-MODEL: every backtest entry point prices fills with the same
    # trading.costs model; backtesting.py charges `commission` per fill.
    from backtest import runner, portfolio, optimizer
    assert runner.PER_SIDE_COST == pytest.approx(runner.TOTAL_COST / 2)
    assert inspect.signature(runner.run_backtest).parameters["commission"].default is runner.COMMISSION
    assert inspect.signature(portfolio.run_portfolio_backtest).parameters["commission"].default \
        is runner.COMMISSION
    assert optimizer.COMMISSION is runner.COMMISSION


def test_backtest_table_column_helpers():
    from backtest.display import signed_columns, integer_columns
    df = pd.DataFrame({"Return (%)": [1.0], "B&H (%)": [2.0], "Alpha (%)": [-1.0],
                       "Buy & Hold (%)": [1.0], "Sharpe": [1.1], "# Trades": [4],
                       "Max DD (%)": [-5.0], "Win Rate (%)": [50.0]})
    s = signed_columns(df, "Return (%)")
    assert {"Return (%)", "B&H (%)", "Alpha (%)", "Buy & Hold (%)"} <= set(s)
    assert "Win Rate (%)" not in s and "Sharpe" not in s
    assert integer_columns(df) == ["# Trades"]


# ── 5. Verdict ledger ────────────────────────────────────────────────────────

def test_ledger_horizon_uses_trading_days():
    from analysis.verdict_ledger import horizon_target_date
    fri = dt.date(2026, 9, 18)
    assert horizon_target_date(fri, 1) == dt.date(2026, 9, 21)   # Monday
    assert horizon_target_date(fri, 5) == dt.date(2026, 9, 25)


def test_ledger_alpha_unavailable_without_nifty(monkeypatch):
    import analysis.verdict_ledger as vl
    df = pd.DataFrame({"id": [1, 2, 3], "verdict": ["BUY"] * 3,
                       "ret_5d": [2.0, -1.0, 3.0], "nifty_ret_5d": [1.0, -2.0, np.nan]})
    monkeypatch.setattr(vl, "load_ledger", lambda **k: df.copy())
    out = vl.calibration_by(group_col="verdict", horizon_days=5)
    row = out.iloc[0]
    assert row["n"] == 3
    assert row["n_alpha"] == 2
    assert row["mean_alpha"] == pytest.approx(1.0)      # (1 + 1) / 2, row 3 excluded
    assert row["win_rate"] == pytest.approx(66.7)       # raw: ret > 0
    assert row["win_vs_nifty"] == pytest.approx(100.0)  # alpha > 0 among 2


# ── 6. Quality Watch ─────────────────────────────────────────────────────────

def test_quality_watch_supported_postures_rank_above_demanding():
    from analysis.quality_watch import POSTURE_POINTS
    from analysis.fundamentals import valuation_decision as vd
    for p in (vd.SUPPORTED_BY_GROWTH_AND_QUALITY, vd.SUPPORTED_BY_GROWTH,
              vd.SUPPORTED_BY_QUALITY, vd.SUPPORTED_BY_ROE, vd.REASONABLE,
              vd.DEMANDING_VS_GROWTH, vd.DEMANDING_VS_RETURNS, vd.DEMANDING_VS_ROE,
              vd.INSUFFICIENT_EVIDENCE):
        assert p in POSTURE_POINTS
    dem = max(POSTURE_POINTS[p] for p in (vd.DEMANDING_VS_GROWTH, vd.DEMANDING_VS_RETURNS,
                                          vd.DEMANDING_VS_ROE))
    for p in (vd.SUPPORTED_BY_GROWTH_AND_QUALITY, vd.SUPPORTED_BY_GROWTH,
              vd.SUPPORTED_BY_QUALITY, vd.SUPPORTED_BY_ROE):
        assert POSTURE_POINTS[p] > dem


def test_quality_watch_bank_uses_pb_roe_not_roce_de():
    from analysis.quality_watch import compute_quality_score
    from analysis.sector_classification import classify_sector
    bank = classify_sector("Banks")
    s1, b1 = compute_quality_score("REASONABLE", "high", 0, 0, roe=0.18, roce=0.01,
                                   debt_to_equity=9.0, pb=1.5, sector_profile=bank)
    s2, b2 = compute_quality_score("REASONABLE", "high", 0, 0, roe=0.18, roce=None,
                                   debt_to_equity=None, pb=1.5, sector_profile=bank)
    assert b1["quality_ratios"] == b2["quality_ratios"] == 20
    assert set(b1["ratios_used"]) == {"roe", "pb"}
    it = classify_sector("IT Services")
    _, b3 = compute_quality_score("REASONABLE", "high", 0, 0, roe=0.18, roce=0.2,
                                  debt_to_equity=0.1, pb=9.0, sector_profile=it)
    assert set(b3["ratios_used"]) == {"roe", "roce", "debt_to_equity"}
