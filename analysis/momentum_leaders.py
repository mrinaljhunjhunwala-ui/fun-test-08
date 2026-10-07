"""
analysis/momentum_leaders.py — plain trend/momentum ranking, regime-independent.

Why this exists: in a bear regime analysis/score.py's regime weights (#198)
hold Technical at 20/40 and RS at 5/10 and score 5-day momentum as mean
reversion, so the composite tops out near the BUY line and strong-momentum
names read HOLD. This module ranks the same daily frames on raw momentum
only, so a separate "Momentum leaders" list can show them. It is a
DESCRIPTIVE ranking, not a recommendation, and it does not touch the
composite score.

Momentum-strength score, 0-100:
    20d return            20  (-5% -> 0, +25% -> 20)
    60d return            20  (-10% -> 0, +50% -> 20)
    near 52-week high     20  (25% below -> 0, at the high -> 20)
    moving-average stack  15  (close > 50 DMA, close > 200 DMA, 50 > 200; 5 each)
    ADX trend strength    15  (ADX 15 -> 0, 40 -> 15; 0 when -DI leads)
    volume ratio          10  (5d avg / 50d avg volume: 0.8 -> 0, 2.0 -> 10)

Pure: no Streamlit, no network. Callers hand in frames they already fetched.
"""
from __future__ import annotations

from typing import Callable, Dict, List, Mapping, Optional

import numpy as np
import pandas as pd

MIN_BARS = 130                    # ~6 months: enough for 60d return + ADX warm-up
MIN_TRADED_VALUE = 2e7            # Rs 2 crore, 20-session median close*volume
MIN_PRICE = 20.0


def _lin(x: float, lo: float, hi: float, pts: float) -> float:
    if x is None or not np.isfinite(x):
        return 0.0
    return float(pts * min(1.0, max(0.0, (x - lo) / (hi - lo))))


def _adx_di(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14):
    """Wilder ADX with +DI / -DI (last values)."""
    tr = pd.concat([high - low, (high - close.shift(1)).abs(),
                    (low - close.shift(1)).abs()], axis=1).max(axis=1)
    up, down = high.diff(), low.shift(1) - low
    pdm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=high.index)
    mdm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=high.index)
    a = 1 / period
    atr = tr.ewm(alpha=a, adjust=False).mean().replace(0, np.nan)
    pdi = 100 * pdm.ewm(alpha=a, adjust=False).mean() / atr
    mdi = 100 * mdm.ewm(alpha=a, adjust=False).mean() / atr
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    adx = dx.ewm(alpha=a, adjust=False).mean()
    return float(adx.iloc[-1]), float(pdi.iloc[-1]), float(mdi.iloc[-1])


def momentum_metrics(df: Optional[pd.DataFrame]) -> Optional[Dict]:
    """Momentum metrics + 0-100 strength score for one DAILY OHLCV frame.
    None when the frame is missing columns or too short. Never raises."""
    try:
        if df is None or len(df) < MIN_BARS:
            return None
        d = df[["High", "Low", "Close", "Volume"]].astype(float).dropna(subset=["Close"])
        if len(d) < MIN_BARS:
            return None
        c, v = d["Close"], d["Volume"].fillna(0)
        last = float(c.iloc[-1])
        if not last > 0:
            return None
        r20 = (last / float(c.iloc[-21]) - 1) * 100
        r60 = (last / float(c.iloc[-61]) - 1) * 100
        hi52 = float(d["High"].tail(252).max())
        from_high = (last / hi52 - 1) * 100 if hi52 > 0 else np.nan     # <= 0
        dma50 = float(c.tail(50).mean())
        dma200 = float(c.tail(200).mean())
        adx, pdi, mdi = _adx_di(d["High"], d["Low"], c)
        v50 = float(v.tail(50).mean())
        vol_ratio = float(v.tail(5).mean()) / v50 if v50 > 0 else np.nan
        traded_value = float((c * v).tail(20).median())

        comps = {
            "ret20": _lin(r20, -5, 25, 20),
            "ret60": _lin(r60, -10, 50, 20),
            "near_high": _lin(from_high, -25, 0, 20),
            "ma_stack": 5.0 * ((last > dma50) + (last > dma200) + (dma50 > dma200)),
            "adx": _lin(adx, 15, 40, 15) if pdi > mdi else 0.0,
            "volume": _lin(vol_ratio, 0.8, 2.0, 10),
        }
        return {
            "price": round(last, 2),
            "ret_20d": round(r20, 2),
            "ret_60d": round(r60, 2),
            "from_52w_high": round(from_high, 2),
            "above_50dma": bool(last > dma50),
            "above_200dma": bool(last > dma200),
            "adx": round(adx, 1) if np.isfinite(adx) else None,
            "vol_ratio": round(vol_ratio, 2) if np.isfinite(vol_ratio) else None,
            "traded_value_cr": round(traded_value / 1e7, 2),
            "components": {k: round(x, 1) for k, x in comps.items()},
            "momentum_score": round(sum(comps.values()), 1),
        }
    except Exception:
        return None


def _default_exclusion(ticker: str, df: pd.DataFrame) -> Optional[str]:
    from analysis.edge_flags import list_exclusion
    return list_exclusion(df)


def rank_momentum_leaders(
    frames: Mapping[str, Optional[pd.DataFrame]],
    n: int = 15,
    exclusion: Optional[Callable[[str, pd.DataFrame], Optional[str]]] = None,
    min_traded_value: float = MIN_TRADED_VALUE,
    min_price: float = MIN_PRICE,
    extra: Optional[Mapping[str, Mapping]] = None,
) -> Dict:
    """Rank tickers by momentum_score (desc), after the list exclusions
    (circuit lock, BZ, new listing, degraded data), a liquidity floor and a
    price floor. `extra` (ticker -> dict) is merged into each row, e.g. the
    composite posture and sector. Returns {"leaders": [...], "n_ranked",
    "n_filtered": {reason: count}}. Never raises per ticker."""
    exclusion = exclusion or _default_exclusion
    rows: List[Dict] = []
    filtered: Dict[str, int] = {}

    def _skip(why: str) -> None:
        filtered[why] = filtered.get(why, 0) + 1

    for tk, df in frames.items():
        if df is None or len(df) == 0:
            _skip("no data")
            continue
        try:
            why = exclusion(tk, df)
        except Exception:
            why = None
        if why:
            _skip(str(why))
            continue
        m = momentum_metrics(df)
        if m is None:
            _skip("short history")
            continue
        if m["price"] <= min_price:
            _skip("price floor")
            continue
        if m["traded_value_cr"] * 1e7 < min_traded_value:
            _skip("liquidity floor")
            continue
        row = {"ticker": tk, **m}
        if extra and tk in extra:
            row.update({k: val for k, val in extra[tk].items() if k not in row})
        rows.append(row)

    rows.sort(key=lambda r: (-r["momentum_score"], -r["ret_60d"]))
    for i, r in enumerate(rows[:n], 1):
        r["rank"] = i
    return {"leaders": rows[:n], "n_ranked": len(rows), "n_filtered": filtered}
