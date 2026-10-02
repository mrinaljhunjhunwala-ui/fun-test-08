"""Settled-pick statistics for the Tomorrow's Watchlist scorecard.

Pure: takes (direction, return %, Nifty return %) triples and returns plain
dicts, so the page stays a thin view and the maths is unit-testable.

Why a "vs Nifty" view: a raw hit rate rewards being on the market's side. In
a falling tape most stocks fall, so down-calls hit without any stock-picking
skill. Subtracting Nifty's move over the same window separates the two.
"""
from __future__ import annotations

import math
from typing import Iterable, Optional

Triple = tuple  # (direction: +1/-1, ret_pct, nifty_ret_pct or None)


def _f(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def settle_stats(rows: Iterable[Triple], band: float = 2.0) -> dict:
    """Hit/Miss/Flat on the ±band rule plus Nifty-relative figures.

    `with_setup` is the return signed by direction (positive = moved the way
    the setup pointed). `vs_nifty` is the same after subtracting Nifty's move;
    rows without a Nifty return are left out of the vs-Nifty figures only.
    `worst` is the most negative with-setup move (close to close, not intraday).
    """
    hit = miss = flat = 0
    with_setup: list = []
    vs_nifty: list = []
    for d, ret, nifty in rows:
        r = _f(ret)
        if r is None or d not in (1, -1):
            continue
        s = d * r
        with_setup.append(s)
        if s >= band:
            hit += 1
        elif s <= -band:
            miss += 1
        else:
            flat += 1
        n_r = _f(nifty)
        if n_r is not None:
            vs_nifty.append(d * (r - n_r))
    n = len(with_setup)
    nv = len(vs_nifty)
    return {
        "n": n, "hit": hit, "miss": miss, "flat": flat,
        "hit_rate": hit / n * 100.0 if n else None,
        "mean_with": sum(with_setup) / n if n else None,
        "worst": min(with_setup) if n else None,
        "n_vs": nv,
        "beat_rate": sum(1 for x in vs_nifty if x > 0) / nv * 100.0 if nv else None,
        "mean_vs_nifty": sum(vs_nifty) / nv if nv else None,
    }


def split_by_market(rows: Iterable[Triple], band: float = 2.0) -> list:
    """settle_stats per (Nifty window up/down, setup direction).

    Returns [(market, side, stats)] in a fixed order, skipping empty cells.
    Rows without a Nifty return can't be placed and are dropped here.
    """
    cells: dict = {}
    for d, ret, nifty in rows:
        n_r = _f(nifty)
        if n_r is None or d not in (1, -1):
            continue
        key = ("Nifty up" if n_r >= 0 else "Nifty down", "bullish" if d > 0 else "bearish")
        cells.setdefault(key, []).append((d, ret, nifty))
    out = []
    for mk in ("Nifty down", "Nifty up"):
        for side in ("bearish", "bullish"):
            if (mk, side) in cells:
                out.append((mk, side, settle_stats(cells[(mk, side)], band)))
    return out
