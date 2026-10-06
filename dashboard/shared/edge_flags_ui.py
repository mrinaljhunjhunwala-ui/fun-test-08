"""dashboard/shared/edge_flags_ui.py — render trading-status caveats for a stock.

Thin view over analysis.edge_flags (logic) and data.nse_security_info (NSE
series / price band / listing date). Display only — never changes a score.
"""
from __future__ import annotations

import html as _html
import logging
from typing import List

import streamlit as st

from dashboard.shared.ui_components import chip_pill

_log = logging.getLogger("dashboard.edge_flags_ui")
_TONE = {"red": "bad", "amber": "warn", "info": "neutral"}


def flags_for(ticker: str, df) -> List:
    """EdgeFlags for one stock; [] on any failure (network blocked, odd frame)."""
    try:
        from analysis.edge_flags import edge_flags
        from data.nse_security_info import get_security_info
        return edge_flags(df, get_security_info(ticker))
    except Exception as e:
        _log.debug("edge flags unavailable for %s: %s", ticker, e)
        return []


def render_edge_flags(ticker: str, df) -> None:
    """A row of status chips with one plain-English line each. Renders nothing
    when the stock trades normally."""
    flags = flags_for(ticker, df)
    if not flags:
        return
    chips = " ".join(chip_pill(f.label, _TONE.get(f.tone, "neutral"), title=f.detail) for f in flags)
    lines = "".join(
        f'<div style="font-size:12px;color:var(--ink-mid);margin-top:3px">'
        f'<b>{_html.escape(f.label)}:</b> {_html.escape(f.detail)}</div>'
        for f in flags
    )
    st.markdown(f'<div style="margin:6px 0 10px 0">{chips}{lines}</div>', unsafe_allow_html=True)


def excluded_note(excluded, limit: int = 8) -> "str | None":
    """One caption naming the stocks a scan kept off a ranked list and why
    ({ticker: reason}, from cache._list_exclusion). None when nothing was."""
    if not isinstance(excluded, dict) or not excluded:
        return None
    items = [f"{str(t).replace('.NS', '')} ({r})" for t, r in sorted(excluded.items())]
    more = f" and {len(items) - limit} more" if len(items) > limit else ""
    return ("🚫 Left off this list: " + ", ".join(items[:limit]) + more
            + ". Their scores still show on Analyze Stock with the flag.")
