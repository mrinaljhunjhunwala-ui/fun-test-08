"""Shared renderer for the "Momentum leaders" list (Command Centre + Tomorrow's
Watchlist). Data comes from cache.get_momentum_leaders / the scan payload."""
from __future__ import annotations

import pandas as pd
import streamlit as st

CAPTION = (
    "Descriptive ranking, not a recommendation. Plain trend/momentum (20d/60d return, "
    "distance from the 52-week high, 50/200 DMA, ADX, volume ratio), independent of the "
    "composite's regime weights. In falling-regime weeks momentum has historically been "
    "less reliable (docs/SCORE_EFFICACY_BEAR_2026-09-28.md). Excludes circuit-locked, BZ, "
    "new listings and degraded data; needs ₹2 cr median traded value and price above ₹20."
)


def _table(leaders: list) -> pd.DataFrame:
    rows = []
    for r in leaders:
        rows.append({
            "#": r.get("rank"),
            "Stock": str(r.get("ticker", "")).replace(".NS", ""),
            "Sector": r.get("sector") or "",
            "Momentum /100": r.get("momentum_score"),
            "20d %": r.get("ret_20d"),
            "60d %": r.get("ret_60d"),
            "From 52w high %": r.get("from_52w_high"),
            "ADX": r.get("adx"),
            "Vol ratio": r.get("vol_ratio"),
            ">50/200 DMA": ("✓" if r.get("above_50dma") else "✗") + "/"
                           + ("✓" if r.get("above_200dma") else "✗"),
            "Composite": (f"{r['composite_score']:.0f} · {r.get('composite_action') or ''}"
                          if isinstance(r.get("composite_score"), (int, float)) else "—"),
        })
    return pd.DataFrame(rows)


def render_momentum_leaders(data: "dict | None", title: str = "🏃 Momentum leaders") -> None:
    """Render the list as its own clearly separate section. Never raises."""
    try:
        st.markdown(f"#### {title}")
        st.caption(CAPTION)
        leaders = (data or {}).get("leaders") or []
        if not leaders:
            st.info("Momentum leaders appear after the next scheduled scan.")
            return
        st.dataframe(_table(leaders), hide_index=True, width="stretch")
        gen = (data or {}).get("generated_at") or (data or {}).get("scan_time")
        st.caption(f"{data.get('n_ranked', len(leaders))} names passed the filters"
                   + (f" · scan {gen}" if gen else ""))
    except Exception as _e:
        st.caption(f"Momentum leaders unavailable ({type(_e).__name__}).")
