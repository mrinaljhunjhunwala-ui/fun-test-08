# CLAUDE.md — NSE Smart Investor

Read this before doing anything in the repo. It's the one-page brief that keeps agents from wasting turns re-deriving what the codebase already documents.

## What this is

An 18-page Streamlit dashboard for **NSE/BSE (Indian) equity analysis**. It scores stocks on a composite framework, surfaces qualitative flags, runs a valuation decision layer (E1-v2), does portfolio risk, paper-trading, intraday tools, and optional Angel One live-broker integration. **Educational only — never a SEBI-registered advice product.**

## Architecture at a glance

```
dashboard/                       Streamlit UI — the "view" layer
├── app.py                       thin entry: page config + theme + redirect
├── pages/*.py                   19 pages (01–22), one per screen, explicit imports
└── shared/                      cross-page utilities (design/theme, nav, cache, chart helpers)
analysis/                        PURE engine — NO Streamlit imports here
├── score.py                     composite score (technical 40 + momentum 25 + volume 15 + sentiment 10)
├── trend_quality_score.py       TQS scanner (90-point, 4-pillar)
├── sector_classification.py     single source of truth for sector-aware metric picking
├── qualitative_flags.py         governance / regulatory red-amber-green flags
├── fundamentals/                Yahoo-backed models, valuation engine (E1-v2)
├── thesis/                      Bull/Bear/Risk/Verdict rules
├── portfolio_risk.py            beta, NAV, Sharpe/Sortino/Calmar, HHI, hedge sizing
└── ... (regime, hedging, macro, mtf, price_bands, verdict_ledger, etc.)
strategies/                      pluggable strategy modules (momentum, rsi_macd, sector_rotation)
utils/                           small helpers (indicators, vix, news, telegram, live_price)
data/  →  fetcher.py             tiered price fetch: Angel One → Stooq → Yahoo (cached)
tests/                           pytest, 20+ files, offline-mocked
trade_store.py                   persistence: SQLite by default, Postgres when DATABASE_URL is set
```

**Golden rule of module boundaries**: everything in `analysis/` and `strategies/` must be Streamlit-free so it stays unit-testable. Do not `import streamlit` there.

## Non-negotiable rules

1. **No buy/sell/hold recommendation, ever.** The Valuation Decision Layer emits a *descriptive posture* (Bullish/Neutral/Bearish/etc.), not an instruction. UI copy that reads as advice is a bug. See `PATTERN_REMOVAL_MIGRATION.md` for tone conventions.
2. **Composite score is 0–90, four components (40+25+15+10).** No candlestick component — the 40k-observation study killed it. Do not add it back.
3. **Sector-aware fundamentals.** Banks/NBFCs/insurers are assessed on P/B + ROE, not "leverage". Route metric selection through `analysis/sector_classification.py` — never hard-code sector logic in a page.
4. **`analysis/` is pure.** No `import streamlit`, no `st.cache_*`, no session state. Cache at the `dashboard/shared/cache.py` layer.
5. **Every page must survive network being blocked.** The `test_pages_smoke.py` suite runs each page with the network stubbed; graceful degraded rendering is not optional.
6. **Windows-first shell.** Use `py` launcher, not `python`. Set `PYTHONUTF8=1` for ₹-symbol output.
7. **Page config has exactly two call sites.** `dashboard/app.py` sets title / icon / layout for the entry run, and `apply_design()` re-asserts `layout="wide"` on every page, because `pages/` routing drops app.py's config after `st.switch_page` (every page silently fell back to the narrow 736px column). Streamlit ≥1.44 makes repeated calls additive, so this is safe. Pages themselves still must not call `st.set_page_config`.
8. **Secrets live in `.streamlit/secrets.toml` (gitignored) or the platform's secret store** — never in code, never in commits.

## Where things live

| Looking for... | Look in... |
|---|---|
| A specific dashboard page | `dashboard/pages/NN_*.py` (numbered by nav order) |
| Composite scoring math | `analysis/score.py` |
| TQS scanner math | `analysis/trend_quality_score.py` |
| Valuation posture (E1-v2) | `analysis/fundamentals/` + `analysis/thesis/` |
| Sector classification | `analysis/sector_classification.py` |
| Corp actions / news / RSS pulls | `data/nse_corp_info.py`, `data/news_feed.py`, `data/nse_rss_feeds.py` |
| Price fetcher (tiered) | `data/fetcher.py` (Angel One → Stooq → Yahoo) |
| Persistent trades | `trade_store.py` |
| Global theme + CSS | `dashboard/shared/design.py` |
| Sidebar nav | `dashboard/shared/nav.py` |
| Cross-page cache | `dashboard/shared/cache.py` |

## Common commands

```bash
# Run the app locally
py -m streamlit run dashboard/app.py --server.port 8501 --server.headless true

# Full test suite (what CI runs)
py -m pytest -m "not slow" -q

# One page's smoke test only (fast during dashboard/pages/ edits)
py -m pytest tests/test_pages_smoke.py -k "04_analyze_stock" -q

# Lint a single file (config: pyproject.toml → [tool.ruff], max-line 120)
py -m ruff check dashboard/pages/04_analyze_stock.py

# Slow end-to-end backtest (needs network — gated out of CI)
py -m pytest -m slow -q
```

## Editing discipline

- **Dashboard edits** — after touching `dashboard/pages/*.py` or `dashboard/shared/*.py`, run the matching page-smoke test. The `page-smoke-check` skill enforces this automatically.
- **Sensitive files are hook-blocked.** `portfolio.csv`, `trades.db` / `*.db` / `*.sqlite`, `.streamlit/secrets.toml`, `.env*`, `.credentials*` cannot be Edit/Written by an agent. See `.claude/hooks/block_sensitive.py`. If a legitimate edit needs to pass, temporarily disable the PreToolUse hook in `.claude/settings.json` — never loosen the deny list.
- **Auto-lint after Python edits.** `.claude/hooks/lint_python.py` runs ruff in the background and surfaces violations to stderr. Fix same-turn. Ruff replaced flake8 on 2026-09-02 (~100× faster, bundles pyflakes + pycodestyle + isort).
- **Scoring/valuation edits** — the E1-v2 layer has a golden-snapshot regression (`tests/test_final_verdict.py`, `tests/test_audit_transparency.py`). After touching scoring/valuation/strategies, spawn the `verdict-regression-reviewer` subagent to classify every posture/confidence delta.
- **Fetcher edits or "empty data" bugs** — spawn the `data-provenance-auditor` subagent to canary every external provider (yfinance / NSE / BSE / RSS / Screener / VIX) for schema drift.
- **PRs: branch → PR → green CI → squash-merge; never push to `main`.** Open every PR with `gh pr create ... --label automerge`: `.github/workflows/automerge.yml` squash-merges it once `test` + `real-postgres` pass on its head, so don't wait to merge by hand. Leave the label off PRs that change the automerge workflow/script (the label run reads the script from `main`) or that the user asked to hold. If a labelled PR stays open after green CI, read the Automerge run log (it skips drafts, forks, and heads that moved after CI) and merge manually — only after reading the check results. `main` is not branch-protected (needs GitHub Pro), so a pending check does not block a manual merge. README "Tests & CI" covers the optional `AUTOMERGE_TOKEN`.

## Delegation

- Do small, single-file edits directly. Delegate only when the work is broad (multi-page sweeps, audits) or a dedicated agent exists (`verdict-regression-reviewer`, `data-provenance-auditor`).
- When delegating, set `model` explicitly on the Agent call: `haiku` for lookups/summaries, `opus` for edits/tests/refactors/docs, `fable` for hard bugs, architecture and review.
- Run independent sub-agents in parallel. Always verify their results (tests, diff) before reporting done.

## Skill routing hints for this repo

| Task | Skill |
|---|---|
| Deep-dive on ONE Indian ticker (portable — works in any chat) | `nse-ticker-deepdive` |
| Any trading analysis on a symbol | `nse-trading-toolkit` (routes to the specific framework skill) |
| Adding/tuning the AI co-pilot | `ai-copilot-context` |
| Editing a dashboard page | `page-smoke-check` runs automatically |
| Reviewing UI look-and-feel | `trading-dashboard-design` + `taste-skill` |
| Diagnosing a broken build/test | `debugging-and-error-recovery` |
| Before merging a diff | `code-review` (built-in) |
| Reducing complexity | `simplify` (built-in) |
| Security concerns | `security-review` (built-in) |
| Spec for a new feature | `spec-driven-development` → `planning-and-task-breakdown` |

## Deploy targets

- **Streamlit Community Cloud** — auto-deploys from GitHub `main`. Current live target.
- **Hugging Face Spaces** — deploy kit at `deploy/huggingface/` (README + packages.txt + DEPLOY.md). Uses a separate `hf-deploy` branch pushed to an `hf` remote. See `deploy/huggingface/DEPLOY.md` for the 6-step guide.

## Where the domain skills live

All installed at `~/.claude/skills/` — user-global across projects. NSE-specific ones this repo uses heavily: `technical-analysis`, `candlestick-patterns` (surfaced in narrative only, not scored), `rsi-divergence`, `fibonacci-trading`, `vwap-volume-profile`, `multi-timeframe-analysis`, `position-sizing`, `stop-loss-strategies`, `trailing-stops`, `risk-reward-ratio`, `sector-rotation`, `market-breadth`, `india-vix-sentiment`, `options-fno-analysis`, `commodity-currency-correlations`, `oi-pcr-analysis`, `earnings-corporate-events`, `stock-screener`, `trade-journal`, `portfolio-hedging`, `trading-dashboard-design`.
