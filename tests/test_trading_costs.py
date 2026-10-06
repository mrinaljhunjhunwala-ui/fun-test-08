"""Indian equity trading charges (audit 2026-10-03 #23).

Expected values worked by hand from the published rates (see trading/costs.py):
₹2,00,000 delivery buy  = STT 200 + NSE 5.94 + SEBI 0.20 + stamp 30 + brokerage 20
                          + GST 18% x (20 + 5.94 + 0.20)                     = 260.8452
₹2,00,000 delivery sell = STT 200 + NSE 5.94 + SEBI 0.20 + brokerage 20 + DP 20
                          + GST 18% x (20 + 5.94 + 0.20 + 20)                = 254.4452
"""
import pandas as pd
import pytest

from trading import costs as C


def test_delivery_legs_match_hand_calculation():
    buy = C.leg_charges(200_000, "BUY", C.DELIVERY)
    sell = C.leg_charges(200_000, "SELL", C.DELIVERY)
    assert buy["stt"] == pytest.approx(200.0) and sell["stt"] == pytest.approx(200.0)   # both legs
    assert buy["stamp"] == pytest.approx(30.0) and sell["stamp"] == 0.0
    assert buy["dp"] == 0.0 and sell["dp"] == pytest.approx(20.0)
    assert buy["total"] == pytest.approx(260.8452, abs=1e-3)
    assert sell["total"] == pytest.approx(254.4452, abs=1e-3)
    assert C.round_trip(2000.0, 2000.0, 100)["total"] == pytest.approx(515.2904, abs=1e-3)


def test_intraday_round_trip():
    # buy: stamp 6 + NSE 5.94 + SEBI 0.2 + brk 20 + GST 4.7052; sell: STT 50 + same fees
    rt = C.round_trip(2000.0, 2000.0, 100, product=C.INTRADAY)
    assert rt["stt"] == pytest.approx(50.0) and rt["dp"] == 0.0
    assert rt["total"] == pytest.approx(117.6904, abs=1e-3)


@pytest.mark.parametrize("value,brk", [(2_000, 5.0), (10_000, 10.0), (50_000, 20.0)])
def test_brokerage_floor_and_cap(value, brk):
    assert C.leg_charges(value, "BUY")["brokerage"] == pytest.approx(brk)


def test_small_orders_cost_more_in_percent():
    assert C.representative_round_trip_pct(10_000) > 2 * C.representative_round_trip_pct(200_000)


def test_net_pnl_long_and_short():
    long_ = C.net_pnl(100.0, 110.0, 100)
    assert long_["gross"] == pytest.approx(1000.0) and long_["net"] < long_["gross"]
    short = C.net_pnl(110.0, 100.0, 100, side="SELL")
    assert short["gross"] == pytest.approx(1000.0) and short["charges"] > 0


def test_charges_for_trades_handles_open_and_bad_rows():
    df = pd.DataFrame({
        "price": [2000.0, 500.0, 100.0], "exit_price": [2000.0, None, 0.0],
        "quantity": [100, 10, 5], "action": ["BUY", "BUY", "SELL"],
    })
    out = C.charges_for_trades(df)
    assert out.iloc[0] == pytest.approx(515.29, abs=0.01)
    assert out.iloc[1] == 0.0 and out.iloc[2] == 0.0


def test_backtest_commission_callable_is_per_fill():
    assert C.backtest_commission(100, 2000.0) == pytest.approx(260.8452, abs=1e-3)    # buy fill
    assert C.backtest_commission(-100, 2000.0) == pytest.approx(254.4452, abs=1e-3)   # sell fill


def test_backtests_actually_charge_both_legs():
    from backtesting import Backtest, Strategy
    from backtest.runner import COMMISSION
    idx = pd.bdate_range("2025-01-01", periods=30)
    df = pd.DataFrame({"Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.0,
                       "Volume": 1e5}, index=idx)

    class _RoundTrip(Strategy):
        def init(self):
            pass

        def next(self):
            if len(self.data) == 5:
                self.buy(size=1000)
            elif len(self.data) == 10:
                self.position.close()

    stats = Backtest(df, _RoundTrip, cash=1_000_000, commission=COMMISSION).run()
    lost = 1_000_000 - stats["Equity Final [$]"]
    # Flat price, so every rupee lost is charges on BOTH legs.
    if callable(COMMISSION):      # backtesting.py >= 0.6: exact per-fill charges
        expected = C.round_trip(100.0, 100.0, 1000)["total"]
    else:                         # backtesting.py 0.5.x: a float charged ONCE per round trip
        assert COMMISSION == pytest.approx(C.representative_round_trip_pct())
        expected = COMMISSION * 100_000
    assert lost == pytest.approx(expected, rel=0.05)
    assert lost > 0.002 * 100_000          # more than the old sell-side-only STT model's ~0.23%


def test_callable_support_follows_backtesting_version(monkeypatch):
    import backtesting
    monkeypatch.setattr(backtesting, "__version__", "0.5.0")
    assert C.supports_callable_commission() is False
    # 0.5.x charges the float once per round trip, so it gets the FULL round-trip rate.
    assert C.backtest_commission_arg() == pytest.approx(C.representative_round_trip_pct())
    monkeypatch.setattr(backtesting, "__version__", "0.6.5")
    assert C.supports_callable_commission() is True
    assert C.backtest_commission_arg() is C.backtest_commission
