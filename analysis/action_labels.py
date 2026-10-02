"""Descriptive display labels for internal CompositeScore action strings.

Phase 2 UI honesty — the engine's action strings ("STRONG BUY", "EXIT", ...)
are internal keys (DB, CSV export, sort keys, signal logic) and are NEVER
changed. Anything a person reads — dashboard pages, Telegram/Gmail alerts —
goes through action_display_label() instead (CLAUDE.md rule 1).

Pure module (no Streamlit) so alerts/ can use it without importing the UI.
"""

ACTION_DISPLAY_LABELS = {
    "STRONG BUY": "Strong Trend ▲▲",
    "BUY":        "Uptrend ▲",
    "WATCHLIST":  "Watch",
    "HOLD":       "Neutral",
    "CAUTION":    "Weakening ▼",
    "EXIT":       "Broken Trend ▼▼",
    "SELL":       "Broken Trend ▼▼",   # legacy alias from signals.py
}


def action_display_label(action: str) -> str:
    """Descriptive label for an internal action string; unknown values pass through."""
    return ACTION_DISPLAY_LABELS.get(action, action)


# FinalVerdict (analysis/final_verdict.VERDICTS) → descriptive display label.
VERDICT_DISPLAY_LABELS = {
    "STRONG BUY": "Strong trend ▲▲",
    "BUY":        "Uptrend ▲",
    "WATCH":      "Mixed signals",
    "HOLD":       "Neutral",
    "AVOID":      "Weak / broken trend ▼",
}


def verdict_display_label(verdict: str) -> str:
    """Descriptive label for a FinalVerdict value (never an instruction)."""
    return VERDICT_DISPLAY_LABELS.get(verdict, verdict)


__all__ = ["ACTION_DISPLAY_LABELS", "action_display_label",
           "VERDICT_DISPLAY_LABELS", "verdict_display_label"]
