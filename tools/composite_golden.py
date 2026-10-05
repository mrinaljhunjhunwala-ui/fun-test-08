"""tools/composite_golden.py — golden snapshot for the composite score (score_dataframe).

OFFLINE and deterministic: every case is a seeded synthetic OHLCV series (plus a
seeded synthetic benchmark for RS), run through the same indicator groups and
RS enrichment that `score_stock()` uses, then scored by `score_dataframe()` under
a fixed context (VIX / sector rank / flows / delivery / regime / positioning).

Used by tests/test_composite_golden_snapshot.py. After an INTENTIONAL scoring
change, regenerate and review the diff:

    python tools/composite_golden.py --update
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from contextlib import contextmanager
from typing import Dict, Iterator, List, Optional

import numpy as np
import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

SNAPSHOT_PATH = os.path.join(_ROOT, "data", "composite_golden_snapshot.json")
SCHEMA_VERSION = 1
_N_BARS = 320
_START = "2025-01-01"

# ── Price-path shapes ─────────────────────────────────────────────────────────
# drift / vol are per-bar log-return mean / stdev; seed fixes the noise.
_SERIES: Dict[str, Dict] = {
    "steady_uptrend":   {"seed": 11, "drift":  0.0030, "vol": 0.006},
    "steady_downtrend": {"seed": 12, "drift": -0.0030, "vol": 0.006},
    "sideways":         {"seed": 13, "drift":  0.0000, "vol": 0.008},
    "volatile_chop":    {"seed": 14, "drift":  0.0002, "vol": 0.028},
    "late_breakout":    {"seed": 15, "drift":  0.0000, "vol": 0.009, "tail_drift": 0.012, "tail": 15},
    "late_crash":       {"seed": 16, "drift":  0.0010, "vol": 0.009, "tail_drift": -0.015, "tail": 15},
    "short_history":    {"seed": 17, "drift":  0.0008, "vol": 0.012, "bars": 45},
    # Last-bar shapes for the Volume pillar (audit 2026-10-03 #16/#17): a 2.6x
    # day vs the prior 20 sessions sits in the top ratio bucket only when the
    # bar is kept out of its own baseline (it used to read ~2.4x), and the
    # gap-up fade / gap-down recovery separate close-vs-prev from close-vs-open.
    "volume_spike_up":  {"seed": 13, "drift":  0.0000, "vol": 0.008,
                         "last": {"gap": 0.000, "day": 0.020, "vol_mult": 2.6}},
    "gap_up_fade":      {"seed": 13, "drift":  0.0000, "vol": 0.008,
                         "last": {"gap": 0.040, "day": 0.020, "vol_mult": 2.6}},
    "gap_down_recover": {"seed": 13, "drift":  0.0000, "vol": 0.008,
                         "last": {"gap": -0.040, "day": -0.020, "vol_mult": 2.6}},
}
_BENCH = {"seed": 99, "drift": 0.0004, "vol": 0.008}

_VIX_NORMAL = {"regime": "normal", "vix": 14.0, "allow_buy": True}
_VIX_HIGH   = {"regime": "high",   "vix": 24.0, "allow_buy": True}
_FLOWS_POS  = {"fii_5d": 4500.0, "dii_5d": 1200.0}
_FLOWS_NEG  = {"fii_5d": -6000.0, "dii_5d": 2500.0}

# ── Cases: (name, series, ticker, with_rs, context, env) ─────────────────────
# env keys map to the scoring feature flags; None means "unset" (the default).
_BASE_ENV = {"NSE_USE_REGIME_WEIGHTS": None, "NSE_USE_POSITIONING_PILLAR": None}


def _case(name: str, series: str, ticker: str = "TESTCO.NS", with_rs: bool = True,
          env: Optional[Dict] = None, **ctx) -> Dict:
    return {"name": name, "series": series, "ticker": ticker, "with_rs": with_rs,
            "env": {**_BASE_ENV, **(env or {})}, "ctx": ctx}


CASES: List[Dict] = [
    # Every shape under a neutral context.
    *[_case(f"{s}__neutral", s) for s in _SERIES],
    # No benchmark → legacy 25-pt absolute momentum.
    _case("steady_uptrend__no_rs", "steady_uptrend", with_rs=False),
    _case("steady_downtrend__no_rs", "steady_downtrend", with_rs=False),
    # Bear-regime mean-reversion momentum (#168: ON by default) vs flag off.
    _case("steady_downtrend__bear", "steady_downtrend", regime_label="trend_down"),
    _case("late_crash__bear", "late_crash", regime_label="risk_off"),
    _case("steady_uptrend__bear", "steady_uptrend", regime_label="trend_down"),
    _case("steady_downtrend__bear_flag_off", "steady_downtrend", regime_label="trend_down",
          env={"NSE_USE_REGIME_WEIGHTS": "0"}),
    _case("sideways__uptrend_regime", "sideways", regime_label="trend_up"),
    # VIX regime must not move points (#168) but does move entry levels / horizon.
    _case("steady_uptrend__vix_high", "steady_uptrend", vix_info=_VIX_HIGH),
    # Sector rank + flows.
    _case("steady_uptrend__top_sector_pos_flows", "steady_uptrend",
          sector_rank=1, flows_info=_FLOWS_POS),
    _case("steady_downtrend__bottom_sector_neg_flows", "steady_downtrend",
          sector_rank=15, flows_info=_FLOWS_NEG),
    # Delivery sub-score in the Volume pillar.
    _case("late_breakout__high_delivery", "late_breakout",
          delivery_info={"today": 68.0, "mean": 42.0, "zscore": 2.1}),
    # Positioning pillar: F&O ticker, flag on, real inputs → cap 100.
    _case("steady_uptrend__positioning_on", "steady_uptrend", ticker="RELIANCE.NS",
          env={"NSE_USE_POSITIONING_PILLAR": "1"},
          positioning_info={"oi_regime": "long_buildup", "pcr": 1.25,
                            "max_pain_distance_pct": -1.5, "fii_deriv_net": 8000.0,
                            "fii_deriv_pct": 0.85}),
    # Same F&O ticker with the flag off → legacy 90 shape.
    _case("steady_uptrend__positioning_flag_off", "steady_uptrend", ticker="RELIANCE.NS",
          positioning_info={"oi_regime": "long_buildup", "pcr": 1.25}),
]

# Fields captured per case. Dates / free text (valid_until, narrative) are left out.
_FIELDS = ("score", "grade", "action", "technical_score", "momentum_score",
           "volume_score", "sentiment_score", "positioning_score", "is_fno",
           "entry", "stop_loss", "target", "risk_reward", "rsi", "return_1d",
           "rs_score", "momentum_fallback", "patterns_detected", "horizon")


def _ohlcv(seed: int, drift: float, vol: float, bars: int = _N_BARS,
           tail: int = 0, tail_drift: float = 0.0,
           last: Optional[Dict] = None) -> pd.DataFrame:
    rng = np.random.RandomState(seed)
    mu = np.full(bars, drift)
    if tail:
        mu[-tail:] = tail_drift
    rets = mu + rng.normal(0.0, vol, bars)
    close = 1000.0 * np.exp(np.cumsum(rets))
    open_ = close * np.exp(rng.normal(0.0, vol / 3, bars))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0.0, vol / 2, bars)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0.0, vol / 2, bars)))
    volume = rng.lognormal(13.0, 0.35, bars)
    if tail:
        volume[-tail:] *= 1.8
    if last:
        # Rewrite the final bar: open `gap` and close `day` from the prior close,
        # volume `vol_mult` x the mean of the 20 bars before it.
        prev = close[-2]
        open_[-1] = prev * (1 + last["gap"])
        close[-1] = prev * (1 + last["day"])
        high[-1] = max(open_[-1], close[-1]) * 1.002
        low[-1] = min(open_[-1], close[-1]) * 0.998
        volume[-1] = last["vol_mult"] * volume[-21:-1].mean()
    idx = pd.bdate_range(_START, periods=bars)
    return pd.DataFrame({"Open": open_, "High": high, "Low": low,
                         "Close": close, "Volume": volume.round()}, index=idx)


def build_frame(series: str, with_rs: bool) -> pd.DataFrame:
    """Synthetic df enriched exactly like score_stock() does it."""
    from analysis.score import _SCORE_INDICATOR_GROUPS
    from utils.indicators import add_all_indicators, add_relative_strength

    spec = dict(_SERIES[series])
    spec.pop("seed")
    df = _ohlcv(_SERIES[series]["seed"], **spec)
    df = add_all_indicators(df, groups=_SCORE_INDICATOR_GROUPS)
    if with_rs:
        df = add_relative_strength(df, _ohlcv(_BENCH["seed"], _BENCH["drift"], _BENCH["vol"]))
    df = df.dropna(subset=["RSI", "ATR"])
    return df


@contextmanager
def _env(overrides: Dict) -> Iterator[None]:
    saved = {k: os.environ.get(k) for k in overrides}
    try:
        for k, v in overrides.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _clean(v):
    if isinstance(v, (np.floating, float)):
        f = float(v)
        return None if np.isnan(f) else round(f, 6)
    if isinstance(v, np.integer):
        return int(v)
    if isinstance(v, np.bool_):
        return bool(v)
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    return v


def run_case(case: Dict) -> Dict:
    from analysis.score import score_dataframe

    df = build_frame(case["series"], case["with_rs"])
    ctx = dict(case["ctx"])
    ctx.setdefault("vix_info", dict(_VIX_NORMAL))
    ctx.setdefault("sector", "Energy")
    with _env(case["env"]):
        cs = score_dataframe(df=df, ticker=case["ticker"], **ctx)
    return {f: _clean(getattr(cs, f)) for f in _FIELDS}


def build_snapshot() -> Dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "generator": "tools/composite_golden.py",
        "cases": {c["name"]: run_case(c) for c in CASES},
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--update", action="store_true", help="rewrite the snapshot file")
    args = ap.parse_args(argv)
    snap = build_snapshot()
    if args.update:
        with open(SNAPSHOT_PATH, "w", encoding="utf-8") as f:
            json.dump(snap, f, indent=2, sort_keys=True)
            f.write("\n")
        print(f"wrote {len(snap['cases'])} cases -> {os.path.relpath(SNAPSHOT_PATH, _ROOT)}")
    else:
        for name, row in snap["cases"].items():
            print(f"{name:45s} {row['score']:6.1f} {row['grade']:3s} {row['action']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
