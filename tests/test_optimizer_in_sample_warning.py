"""Audit 2026-10-03: --optimize results must be labelled in-sample."""
from backtest.optimizer import IN_SAMPLE_LABEL, IN_SAMPLE_WARNING, in_sample_banner


def test_banner_empty_when_not_optimised():
    assert in_sample_banner(False) == ""


def test_banner_labels_in_sample_when_optimised():
    text = in_sample_banner(True)
    assert IN_SAMPLE_WARNING in text and IN_SAMPLE_LABEL in text
    assert "in-sample (optimised on this same period)" in text
    assert "—" not in text  # no em-dash in user-facing copy
    for word in ("buy", "sell", "hold"):
        assert word not in text.lower().split()
