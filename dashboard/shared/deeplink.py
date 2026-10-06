"""``?ticker=`` deep-link parsing (no Streamlit imports)."""
from __future__ import annotations

import re

_TICKER_RE = re.compile(r"^[A-Z0-9&\-]{1,20}(\.NS|\.BO)?$")


def normalise_ticker_param(raw) -> str | None:
    """Upper-case and add .NS when no exchange suffix; None if empty/invalid."""
    if isinstance(raw, (list, tuple)):
        raw = raw[0] if raw else None
    if not isinstance(raw, str):
        return None
    sym = raw.strip().upper()
    if not sym or not _TICKER_RE.match(sym):
        return None
    return sym if sym.endswith((".NS", ".BO")) else sym + ".NS"
