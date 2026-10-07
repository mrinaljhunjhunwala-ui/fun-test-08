"""--optimize --holdout: fit on the early window, report the late window (offline)."""
import numpy as np
import pandas as pd
import pytest
from backtesting import Strategy

from backtest import optimizer as opt

CALLS = []


class _Toy(Strategy):
    """Buys after `k` up-closes; records the window and param of every run."""
    k = 1

    def init(self):
        CALLS.append((self.data.index[0], self.data.index[-1], self.k, len(self.data)))

    def next(self):
        c = self.data.Close
        if len(c) > self.k + 1 and all(c[-i] > c[-i - 1] for i in range(1, self.k + 1)):
            if not self.position:
                self.buy()
        elif self.position:
            self.position.close()


def _frame(n=400, seed=0):
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0.0005, 0.015, n)))
    idx = pd.bdate_range("2022-01-03", periods=n)
    return pd.DataFrame({"Open": close, "High": close * 1.01, "Low": close * 0.99,
                         "Close": close, "Volume": 1e6}, index=idx)


def test_split_boundaries_and_no_overlap():
    df = _frame(400)
    fit, test = opt.split_holdout(df, 0.3)
    assert len(test) == 120 and len(fit) == 280
    assert fit.index[-1] < test.index[0]
    assert fit.index.intersection(test.index).empty
    assert len(fit) + len(test) == len(df)
    with pytest.raises(ValueError):
        opt.split_holdout(df, 0)


def test_best_fit_params_applied_to_holdout_only():
    df = _frame(400)
    CALLS.clear()
    res = opt.fit_and_test(df, _Toy, {"k": [1, 2, 3]}, 0.3)
    fit, test = opt.split_holdout(df, 0.3)
    opt_calls, oos_call = CALLS[:-1], CALLS[-1]
    # every optimisation run saw only the fit window
    assert all(c[1] == fit.index[-1] and c[3] == len(fit) for c in opt_calls)
    # the final run is on the holdout window with the winning param
    assert oos_call[0] == test.index[0] and oos_call[1] == test.index[-1]
    assert oos_call[2] == res["best"]["k"]
    assert res["out_of_sample"] is not None and res["skipped"] is None
    assert set(res["in_sample"]) == {"Return (%)", "Sharpe", "Max DD (%)",
                                     "Win Rate (%)", "# Trades"}
    text = opt.format_holdout_report("TOY.NS", res)
    assert "IN-SAMPLE" in text and "OUT-OF-SAMPLE" in text and opt.IN_SAMPLE_WARNING in text
    assert res["fit_range"] in text and res["test_range"] in text


def test_short_holdout_skips_oos():
    df = _frame(150)  # 30% = 45 bars < 60
    res = opt.fit_and_test(df, _Toy, {"k": [1, 2]}, 0.3)
    assert res["out_of_sample"] is None and "< 60" in res["skipped"]
    assert "out-of-sample skipped" in opt.format_holdout_report("TOY.NS", res)


def test_holdout_default_and_zero_path():
    from main import resolve_holdout
    assert resolve_holdout(True, None) == 0.3
    assert resolve_holdout(False, None) == 0.0
    assert resolve_holdout(True, 0) == 0.0
    with pytest.raises(SystemExit):
        resolve_holdout(True, 1.0)


def test_runner_holdout_zero_unchanged(monkeypatch, tmp_path):
    """holdout=0 keeps the old single-ticker in-sample optimise on the full frame."""
    from backtest import runner
    monkeypatch.chdir(tmp_path)  # run_backtest writes backtest_results.csv
    df = _frame(300)
    monkeypatch.setattr(runner, "fetch_single", lambda t, period: df)
    monkeypatch.setattr(runner, "add_all_indicators", lambda d: d)
    monkeypatch.setattr(runner, "_RUNNER_GRID", {"k": [1, 2]})
    monkeypatch.setattr(runner, "_RUNNER_CONSTRAINT", None)
    CALLS.clear()
    rep = runner.run_backtest(["TOY.NS"], _Toy, optimize=True, plot=False)
    assert not rep.empty and all(c[3] == len(df) for c in CALLS)
    CALLS.clear()
    rep = runner.run_backtest(["TOY.NS"], _Toy, optimize=True, plot=False, holdout=0.3)
    assert CALLS[-1][3] == 90 and "Window" in rep.columns


def _string_frame(n=300):
    df = _frame(n)
    df["Sector"] = pd.Series(["Energy"] * n, index=df.index, dtype="string")
    return df


def test_optimize_strategy_survives_string_column(monkeypatch, tmp_path):
    """Regression: a pandas StringDtype column crashed bt.optimize's shm copy."""
    monkeypatch.chdir(tmp_path)  # writes best_params.json
    monkeypatch.setattr(opt, "fetch_single", lambda t, period: _string_frame())
    monkeypatch.setattr(opt, "add_all_indicators", lambda d: d)
    monkeypatch.setitem(opt.PARAM_GRIDS, "_Toy", {"k": [1, 2]})
    assert opt.optimize_strategy(["TOY.NS"], _Toy, sample_size=1) != {}


def test_runner_optimize_survives_string_column(monkeypatch, tmp_path):
    from backtest import runner
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runner, "fetch_single", lambda t, period: _string_frame())
    monkeypatch.setattr(runner, "add_all_indicators", lambda d: d)
    monkeypatch.setattr(runner, "_RUNNER_GRID", {"k": [1, 2]})
    monkeypatch.setattr(runner, "_RUNNER_CONSTRAINT", None)
    assert not runner.run_backtest(["TOY.NS"], _Toy, optimize=True, plot=False).empty


def test_numeric_frame_drops_strings_only():
    from backtest.runner import numeric_frame
    out = numeric_frame(_string_frame(10))
    assert "Sector" not in out and list(out.columns) == ["Open", "High", "Low", "Close", "Volume"]


def test_oos_nan_prints_na_zero_trades():
    res = {"best": {"k": 1}, "fit_range": "a", "test_range": "b", "n_fit": 1, "n_test": 1,
           "in_sample": {"Sharpe": 1.0, "Win Rate (%)": 50.0, "# Trades": 2},
           "out_of_sample": {"Sharpe": float("nan"), "Win Rate (%)": float("nan"), "# Trades": 0},
           "skipped": None}
    text = opt.format_holdout_report("TOY.NS", res)
    assert text.count("n/a (0 trades)") == 2 and "nan" not in text
