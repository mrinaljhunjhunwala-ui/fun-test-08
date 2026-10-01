# Why the composite score's edge faded — 2026-10-01

Source: `research/decay_attribution.py` run on the 5y replay saved by
`research/sector_vs_stock.py --period 5y --tag 5y` (483 tickers, 85,148 weekly
observations, production regime handling, live-ledger period held out). Metric:
mean weekly cross-sectional rank IC vs 20-session return over the universe
median; t-stat over weeks. No live-ledger data was used.

## 1. The fade is mostly a change in regime mix, not a broken signal

| | weeks | trend_up share | score IC |
|---|---|---|---|
| first half | 99 | 78% | +0.042 (t +4.5) |
| second half | 112 | 38% | +0.009 (t +0.8) |

Within each regime, the IC barely changes from one half to the other:

| regime | first half | second half |
|---|---|---|
| trend_up | +0.046 (t +4.2, 77w) | +0.042 (t +3.1, 43w) |
| range | +0.026 (t +1.3, 18w) | −0.012 (t −0.5, 38w) |
| trend_down | (2 weeks) | −0.034 (t −1.7, 25w) |
| risk_off | (2 weeks) | +0.082 (t +2.5, 6w) |

In trend_up, the score and its technical, momentum and RS parts still rank
stocks about as well as before. The second half had twice as many range weeks
and twelve times as many trend_down weeks, and the score has no edge in either.
By calendar year, 2025 was the weak year (−0.012) and 2026 recovered (+0.027).
The volume pillar shows no ranking edge in any slice (|IC| ≤ 0.02 apart from
the 2022 year).

## 2. Variant screen for trend_down / range — nothing adopted

The folds were alternating calendar quarters, fixed before looking at the
results. The adoption rule was set in advance: a variant must beat the current
score with t > 2 in **both** folds. Candidates were RS alone, distance from the
200-day average, 20-day momentum, short-term reversal, low RSI, the volume
pillar, and RS + 200-day distance.

Every candidate that looked strong in one fold reversed in the other. In
trend_down, for example, 200-day distance scored −0.051 (A) and +0.092 (B), and
RS alone −0.036 and +0.076. **No variant passed**, so production scoring is
unchanged and the live evidence count does not restart.

## 3. What changed

- The Analyze Stock page now shows a descriptive caption when NIFTY's trend
  regime is `range` or `trend_down`. It says the score has historically carried
  no ranking information in those regimes, and it contains no instruction
  (`dashboard/shared/disclosures.py::trend_regime_note`).
- The live ledger was logged almost entirely in trend_down. On history, that is
  a regime where the score's expected edge is about zero, so a weak October
  read-out should be read with that in mind. The pre-registered rule stays as
  it is.

## Limits

These numbers use today's universe, so they carry survivorship bias. The
historical scores have no delivery-% input. There are only 27 trend_down weeks
in 5 years. A 10-year replay would add bear weeks, but it would not make a
sign-flipping candidate trustworthy.
