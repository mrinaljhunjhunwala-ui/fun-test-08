"""dashboard/shared/volume_display.py — one way to show a stock's volume reading.

Every page reads volume through analysis.score.volume_reading() so the tile,
the checklist and the composite narrative quote the same number. While
today's session is in progress that number is either the previous session's
ratio or today's volume projected to a full day, and the label says which.
"""
from __future__ import annotations

import datetime as _dt
from typing import Dict, Optional, Tuple

import pandas as pd


def reading(df: pd.DataFrame, now_ist=None) -> Dict:
    """volume_reading(df) with a safe fallback for frames it can't read."""
    try:
        from analysis.score import volume_reading
        return volume_reading(df, now_ist=now_ist)
    except Exception:
        raw = None
        try:
            raw = float(df.iloc[-1].get("Volume_Ratio"))
        except Exception:
            pass
        return {"ratio": raw if raw is not None else 1.0, "ratio_raw": raw, "volume": None,
                "baseline": None, "basis": "latest", "session_minutes": None, "up_day": True,
                "flat_day": False}


def _compact(n: Optional[float]) -> str:
    if n is None or pd.isna(n):
        return ""
    for div, suf in ((1e7, "Cr"), (1e5, "L"), (1e3, "K")):
        if abs(n) >= div:
            return f"{n / div:.1f}{suf}"
    return f"{n:.0f}"


def basis_label(r: Dict) -> str:
    """'' for a completed session; 'last session' / 'on pace (HH:MM)' while live."""
    basis = r.get("basis", "latest")
    if basis == "previous_session":
        return "last session"
    if basis == "projected":
        mins = r.get("session_minutes")
        if isinstance(mins, (int, float)):
            t = _dt.datetime(2000, 1, 1, 9, 15) + _dt.timedelta(minutes=int(mins))
            return f"on pace ({t:%H:%M})"
        return "on pace"
    return ""


def vol_ratio_tile(r: Dict, high: float = 1.5) -> Tuple[str, str]:
    """(value, note) for an indicator tile, e.g. ('1.62x', 'High volume · on pace (11:30) · 3.2M vs 2.0M')."""
    if r.get("ratio_raw") is None:
        # volume_reading reports a neutral 1.0 when volume is unknown; don't
        # present that as a measurement.
        return "–", "Volume unavailable"
    ratio = r.get("ratio")
    value = f"{float(ratio):.2f}x" if ratio is not None and not pd.isna(ratio) else "–"
    note = "High volume" if (ratio is not None and not pd.isna(ratio) and ratio > high) else "Normal"
    lbl = basis_label(r)
    if lbl:
        note += f" · {lbl}"
    v, b = _compact(r.get("volume")), _compact(r.get("baseline"))
    if v and b:
        note += f" · {v} vs {b} avg"
    return value, note
