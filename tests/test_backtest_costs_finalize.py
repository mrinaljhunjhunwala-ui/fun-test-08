"""backtesting 0.6 leaves a position open at the end out of # Trades unless
finalize_trades=True; 0.5 closed and counted it. Keep stats comparable."""
import trading.costs as C


def test_extra_kwargs_follow_version(monkeypatch):
    monkeypatch.setattr(C, "supports_callable_commission", lambda: True)
    assert C.backtest_extra_kwargs() == {"finalize_trades": True}
    monkeypatch.setattr(C, "supports_callable_commission", lambda: False)
    assert C.backtest_extra_kwargs() == {}


def test_open_trade_at_end_is_counted():
    import numpy as np
    import pandas as pd
    from backtesting import Backtest, Strategy
    from backtest.runner import BT_KWARGS

    class BuyAndHold(Strategy):
        def init(self):
            pass

        def next(self):
            if not self.position:
                self.buy()

    idx = pd.bdate_range("2026-01-01", periods=30)
    c = np.linspace(100, 110, 30)
    df = pd.DataFrame({"Open": c, "High": c + 1, "Low": c - 1, "Close": c, "Volume": 1e5}, index=idx)
    stats = Backtest(df, BuyAndHold, cash=100_000, commission=0.0, **BT_KWARGS).run()
    assert int(stats["# Trades"]) == 1
