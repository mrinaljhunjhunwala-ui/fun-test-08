"""Daily NSE market breadth from bhavcopy sessions — PURE (no Streamlit, no I/O).

Input is the parsed NSE ``sec_bhavdata_full`` data for many sessions, either a
``{date: frame}`` dict or one long frame. Each frame needs the columns
``SYMBOL, Close, Volume`` and ideally ``SERIES, PREV_CLOSE, DATE``.

Why PREV_CLOSE matters: NSE adjusts ``PREV_CLOSE`` for splits and bonuses on
the ex-date, so ``Close / PREV_CLOSE`` is the true one-day return even across
corporate actions. Chaining those returns gives a split-consistent price index,
which is what the moving-average and 52-week tests run on. Without PREV_CLOSE
the raw close is used (a 1:1 bonus then reads as a -50% day).

Output (``compute_breadth``): one row per session with
  advances, declines, unchanged, n, ad_ratio, ad_line,
  pct_above_20dma / 50dma / 200dma (among stocks with that much history),
  new_52w_highs, new_52w_lows (close-based, stocks with >= 250 sessions),
  up_volume, down_volume, up_volume_ratio.

``breadth_regime`` turns that into a smoothed bull/bear/neutral read; the
regime study (research/regime_breadth_study.py) is where its thresholds come from.
"""
from __future__ import annotations

from typing import Dict, Iterable, Mapping, Optional, Union

import numpy as np
import pandas as pd

SERIES_DEFAULT = ("EQ",)
_RET_CLIP = 0.5          # |1-day return| > 50% on NSE EQ is almost always a data artefact


def to_long(sessions: Union[Mapping, pd.DataFrame]) -> pd.DataFrame:
    """Normalise a ``{date: frame}`` dict or a long frame to one long frame with DATE."""
    if isinstance(sessions, pd.DataFrame):
        out = sessions.copy()
    else:
        parts = []
        for d, f in sessions.items():
            if f is None or len(f) == 0:
                continue
            f = f.copy()
            if "DATE" not in f.columns:
                f["DATE"] = d
            parts.append(f)
        if not parts:
            return pd.DataFrame(columns=["DATE", "SYMBOL", "Close", "Volume"])
        out = pd.concat(parts, ignore_index=True)
    out["DATE"] = pd.to_datetime(out["DATE"]).dt.normalize()
    return out


def build_panels(sessions: Union[Mapping, pd.DataFrame],
                 series: Iterable[str] = SERIES_DEFAULT,
                 universe: Optional[Iterable[str]] = None) -> Dict[str, pd.DataFrame]:
    """Wide (date x symbol) panels: close, ret (1-day), adj (split-consistent index), volume."""
    df = to_long(sessions)
    if "SERIES" in df.columns and series:
        df = df[df["SERIES"].astype(str).str.upper().isin({s.upper() for s in series})]
    df = df.assign(SYMBOL=df["SYMBOL"].astype(str).str.upper())
    if universe is not None:
        uni = {str(u).upper().removesuffix(".NS") for u in universe}
        df = df[df["SYMBOL"].isin(uni)]
    df = df.drop_duplicates(["DATE", "SYMBOL"], keep="first")
    close = df.pivot(index="DATE", columns="SYMBOL", values="Close").sort_index().astype(float)
    vol = (df.pivot(index="DATE", columns="SYMBOL", values="Volume").sort_index().astype(float)
           .reindex_like(close))
    if "PREV_CLOSE" in df.columns:
        prev = (df.pivot(index="DATE", columns="SYMBOL", values="PREV_CLOSE").sort_index()
                .astype(float).reindex_like(close))
        ret = close / prev.where(prev > 0) - 1.0
    else:
        ret = close.pct_change(fill_method=None)
    ret = ret.where(ret.abs() <= _RET_CLIP)
    # Chain returns into an index; a missing day leaves the index flat (NaN return -> 0).
    adj = (1.0 + ret.fillna(0.0)).cumprod().where(close.notna())
    return {"close": close, "ret": ret, "adj": adj, "volume": vol}


def compute_breadth(sessions: Union[Mapping, pd.DataFrame, Dict[str, pd.DataFrame]],
                    series: Iterable[str] = SERIES_DEFAULT,
                    universe: Optional[Iterable[str]] = None,
                    flat_tol: float = 0.0) -> pd.DataFrame:
    """Daily breadth table (see module docstring). ``sessions`` may also be ``build_panels`` output."""
    if isinstance(sessions, dict) and {"close", "ret", "adj"} <= set(sessions):
        p = sessions
    else:
        p = build_panels(sessions, series=series, universe=universe)
    ret, adj, vol = p["ret"], p["adj"], p["volume"]
    if ret.empty:
        return pd.DataFrame()

    adv = (ret > flat_tol).sum(axis=1)
    dec = (ret < -flat_tol).sum(axis=1)
    unch = (ret.abs() <= flat_tol).sum(axis=1)
    out = pd.DataFrame({"advances": adv, "declines": dec, "unchanged": unch,
                        "n": adv + dec + unch})
    out["ad_ratio"] = adv / dec.where(dec > 0)
    out["ad_line"] = (adv - dec).cumsum()

    present = adj.notna()
    hist = present.cumsum()                      # sessions of history per stock
    for w in (20, 50, 200):
        ma = adj.rolling(w, min_periods=w).mean()
        ok = ma.notna() & present
        above = (adj > ma) & ok
        out[f"pct_above_{w}dma"] = 100.0 * above.sum(axis=1) / ok.sum(axis=1).where(ok.sum(axis=1) > 0)

    ok252 = (hist >= 250) & present
    hi = adj.rolling(252, min_periods=250).max()
    lo = adj.rolling(252, min_periods=250).min()
    out["new_52w_highs"] = ((adj >= hi) & ok252).sum(axis=1)
    out["new_52w_lows"] = ((adj <= lo) & ok252).sum(axis=1)
    out.loc[ok252.sum(axis=1) == 0, ["new_52w_highs", "new_52w_lows"]] = np.nan

    if vol is not None:
        v = vol.where(ret.notna())
        out["up_volume"] = v.where(ret > flat_tol).sum(axis=1)
        out["down_volume"] = v.where(ret < -flat_tol).sum(axis=1)
        tot = out["up_volume"] + out["down_volume"]
        out["up_volume_ratio"] = out["up_volume"] / tot.where(tot > 0)
    out.index.name = "date"
    return out


def smoothed(breadth: pd.DataFrame, window: int = 10) -> pd.DataFrame:
    """10-session smoothing: mean of %-above columns, A/D ratio as sum(adv)/sum(dec)."""
    b = breadth
    out = pd.DataFrame(index=b.index)
    for c in ("pct_above_20dma", "pct_above_50dma", "pct_above_200dma", "up_volume_ratio"):
        if c in b:
            out[c] = b[c].rolling(window, min_periods=max(1, window // 2)).mean()
    a = b["advances"].rolling(window, min_periods=max(1, window // 2)).sum()
    d = b["declines"].rolling(window, min_periods=max(1, window // 2)).sum()
    out["ad_ratio"] = a / d.where(d > 0)
    return out


def breadth_regime(breadth: pd.DataFrame, window: int = 10,
                   bear_pct50: float = 40.0, bull_pct50: float = 60.0,
                   bear_ad: float = 1.0) -> pd.Series:
    """bear / bull / neutral per session from smoothed breadth.

    bear : smoothed %-above-50DMA < bear_pct50 AND smoothed A/D ratio < bear_ad
    bull : smoothed %-above-50DMA > bull_pct50
    """
    s = smoothed(breadth, window)
    lab = pd.Series("neutral", index=s.index, dtype=object)
    lab[s["pct_above_50dma"] > bull_pct50] = "bull"
    lab[(s["pct_above_50dma"] < bear_pct50) & (s["ad_ratio"] < bear_ad)] = "bear"
    lab[s["pct_above_50dma"].isna()] = "unknown"
    return lab


# App regime labels (analysis.regime.snapshot_live) grouped the way analysis.score does.
_APP_FAMILY = {"trend_down": "bear", "risk_off": "bear", "bear": "bear",
               "trend_up": "bull", "bull": "bull",
               "range": "neutral", "sideways": "neutral"}


def breadth_agreement(app_label: Optional[str], breadth_label: Optional[str],
                      pct_above_50dma: Optional[float] = None) -> Dict[str, str]:
    """Plain-language check of the app's regime label against the breadth read.

    Returns ``{"status": "MATCH" | "MISMATCH" | "UNKNOWN", "text": ...}``, e.g.
    ``label trend_down; 27% above 50DMA: breadth agrees``. Descriptive only.
    """
    app = str(app_label or "unknown").lower()
    br = str(breadth_label or "unknown").lower()
    pct = None
    try:
        if pct_above_50dma is not None and pct_above_50dma == pct_above_50dma:
            pct = float(pct_above_50dma)
    except (TypeError, ValueError):
        pct = None
    head = f"label {app}" + (f"; {pct:.0f}% above 50DMA" if pct is not None else "")
    fam = _APP_FAMILY.get(app)
    if fam is None or br not in ("bull", "bear", "neutral"):
        return {"status": "UNKNOWN", "text": f"{head}: not enough data to compare"}
    if fam == br:
        return {"status": "MATCH", "text": f"{head}: breadth agrees"}
    return {"status": "MISMATCH", "text": f"{head}: breadth disagrees (reads {br})"}


# -- Snapshot helpers (shared by the bhavcopy loader and the scan warmer) --
def _num(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def snapshot_from_breadth(b: pd.DataFrame) -> Optional[dict]:
    """Latest row of a ``compute_breadth`` table as a plain dict; None when empty."""
    if b is None or b.empty:
        return None
    last = b.iloc[-1]
    if not (last.get("advances", 0) + last.get("declines", 0)):
        return None
    return {
        "date": pd.Timestamp(b.index[-1]).date().isoformat(),
        "sessions": int(len(b)),
        "advances": int(last["advances"]),
        "declines": int(last["declines"]),
        "unchanged": int(last["unchanged"]),
        "ad_ratio": _num(last.get("ad_ratio")),
        "pct_above_20dma": _num(last.get("pct_above_20dma")),
        "pct_above_50dma": _num(last.get("pct_above_50dma")),
        "pct_above_200dma": _num(last.get("pct_above_200dma")),
        "new_52w_highs": _num(last.get("new_52w_highs")),
        "new_52w_lows": _num(last.get("new_52w_lows")),
        "breadth_label": str(breadth_regime(b).iloc[-1]),
    }


def frames_to_long(frames: Mapping[str, pd.DataFrame]) -> pd.DataFrame:
    """``{ticker: OHLC frame (DatetimeIndex)}`` -> long (DATE, SYMBOL, Close, Volume).

    Frames whose last bar is older than the most common last date are cut to
    that date, so a single stale or early-printing ticker cannot make the
    latest "session" a near-empty row.
    """
    parts, last_dates = [], []
    for tk, df in (frames or {}).items():
        if df is None or len(df) == 0:
            continue
        cols = {str(c).lower(): c for c in df.columns}
        if "close" not in cols:
            continue
        idx = pd.to_datetime(df.index, errors="coerce")
        if getattr(idx, "tz", None) is not None:
            idx = idx.tz_localize(None)
        part = pd.DataFrame({
            "DATE": idx.normalize(),
            "SYMBOL": str(tk).upper().removesuffix(".NS").removesuffix(".BO"),
            "Close": pd.to_numeric(df[cols["close"]], errors="coerce").to_numpy(),
            "Volume": (pd.to_numeric(df[cols["volume"]], errors="coerce").to_numpy()
                       if "volume" in cols else np.nan),
        }).dropna(subset=["DATE", "Close"])
        if part.empty:
            continue
        parts.append(part)
        last_dates.append(part["DATE"].max())
    if not parts:
        return pd.DataFrame(columns=["DATE", "SYMBOL", "Close", "Volume"])
    cutoff = pd.Series(last_dates).mode().max()
    out = pd.concat(parts, ignore_index=True)
    return out[out["DATE"] <= cutoff]


def breadth_from_price_frames(frames: Mapping[str, pd.DataFrame],
                              min_stocks: int = 20) -> Optional[dict]:
    """Breadth snapshot from per-ticker daily price frames (e.g. a scan's ~1-2y history).

    Same fields as the bhavcopy snapshot plus ``universe_size`` (tickers with
    data). Closes come from the frames as-is (Yahoo's are split-adjusted), so
    there is no PREV_CLOSE. None when fewer than ``min_stocks`` usable frames.
    """
    long = frames_to_long(frames)
    n = int(long["SYMBOL"].nunique()) if not long.empty else 0
    if n < min_stocks or long["DATE"].nunique() < 2:
        return None
    snap = snapshot_from_breadth(compute_breadth(long, series=()))
    if snap:
        snap["universe_size"] = n
    return snap
