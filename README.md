Fun test

An AI-powered **Streamlit dashboard for N (Indian) equity analysis** — composite scoring,
portfolio risk, paper trading, sector-aware fundamentals, a rules-based valuation/thesis engine,
intraday tools, and optional live broker integration via Angel One SmartAPI.

> **Educational use only.** Not SEBI-registered investment advice. Past performance ≠ future results.

🔗 **Live app:** auto-deployed on Streamlit Community Cloud from `main`
📦 **Repo:** [github.com/mrinaljhunjhunwala-ui/nse-smart-investor](https://github.com/mrinaljhunjhunwala-ui/nse-smart-investor)

---

## ✨ Features

An 18-page platform, grouped into six workspaces:

| Workspace | Pages |
|---|---|
| **Markets** | Market Live · Market Overview (incl. breadth + macro) |
| **Trading** | Intraday Trader (CPR / ORB / Supertrend / VWAP / OI & options) · Smart Screener |
| **Portfolio** | My Portfolio · Paper Trades · My Watchlist · Tomorrow's Watchlist |
| **Analysis** | Analyze Stock · Deep Dive · Backtest · Swing Checklist |
| **Scanners** | TQS Scanner (Trend Quality Score) · Quality Watch (long-term holds) |
| **Tools** | Position Sizer · Angel One · Investor Guide · Command Centre (home) |

**Highlights**
- **Composite Score (0–90):** technical (40) · momentum (25) · volume (15) · sentiment (10),
  where sentiment is the India VIX regime plus the stock's sector rank. Candlestick patterns are
  detected and shown in the narrative but are **not** scored — the 5-year, 40,663-observation
  variant study found the old 10-point pattern component had zero-to-negative ranking power in
  every regime (see `PATTERN_REMOVAL_MIGRATION.md`). This line previously read "0–100 · …
  candlestick pattern · news sentiment", describing a scoring model that no longer exists.
- **Trend Quality Score (TQS):** a 90-point, four-pillar scanner across Nifty 50/100/500/Total Market.
- **Qualitative flags:** governance/regulatory red-amber-green flags from NSE corporate actions,
  Google News, and NSE RSS feeds — surfaced on Quality Watch and Analyze Stock.
- **Sector-aware fundamentals:** banks/NBFCs/insurers are assessed on the right metrics (P/B + ROE),
  not penalised for "leverage" that is really deposits.
- **Valuation Decision Layer (E1-v2):** regime-neutral, guard-first engine that outputs a *descriptive
  posture* — never a buy/sell call.
- **Portfolio risk:** beta vs Nifty, NAV reconstruction, Sharpe/Sortino/Calmar, correlation, HHI
  concentration, and hedge sizing.
- **Intraday toolkit:** Opening-Range Breakout, Central Pivot Range, Supertrend, anchored VWAP, gap scanner.
- **Angel One SmartAPI (optional):** live quotes, holdings, positions, funds, order placement & GTT.
- **Paper trading:** journal with persistence (SQLite locally, Postgres in production).

---

## 🚀 Quick start

```bash
pip install -r requirements.txt
streamlit run dashboard/app.py
```

**Windows:** use the `py` launcher (not `python`). For ₹-symbol console output, set `PYTHONUTF8=1`.

---

## 🔑 Configuration (optional)

Live broker data and durable persistence are optional. Add secrets to `.streamlit/secrets.toml`
(gitignored) or as environment variables — never commit them:

```toml
# Angel One SmartAPI (Tier-0 live data + order placement)
[angel_one]
api_key     = "..."
client_id   = "..."
password    = "..."
totp_secret = "..."   # base32 seed from authenticator setup

# Durable persistence (paper trades + watchlist survive redeploys)
# DATABASE_URL = "postgresql://..."   # e.g. a free Neon/Supabase instance
```

Without these the app still runs fully — it falls back to Stooq/Yahoo for data and ephemeral
SQLite for storage.

---

## 🧱 Architecture

```
dashboard/
  app.py                 thin entry point (page config + theme + redirect to Command Centre)
  pages/01..19_*.py      one file per page (explicit imports — no global injection), incl.
                         18_tqs_scanner.py and 19_quality_watch.py
  shared/                design.py (NSE Pro theme) · nav.py · cache.py · chart_helpers.py · trade_utils.py
analysis/                PURE engine, no Streamlit — heavily unit-tested
  score.py · hedging.py · portfolio_risk.py · portfolio_manager.py · liquidity.py
  trend_quality_score.py Trend Quality Score (TQS) — 90-pt, four-pillar scoring
  qualitative_flags.py   governance/regulatory red-amber-green flags (corp actions + news + RSS)
  sector_classification.py   single source of truth for metric applicability
  fundamentals/          Yahoo-backed models, analytics (ROE/ROCE/CAGR/FCF), valuation engine
  thesis/                rules-based Bull/Bear/Risk/Verdict + portfolio fit
data/
  fetcher.py             tiered price fetch: Angel One → Stooq → Yahoo (cached)
  angel_fetcher.py       Angel One SmartAPI client
  universe.py            ticker → sector map (~745 Nifty Total Market tickers)
  nse_corp_info.py · news_feed.py · nse_rss_feeds.py   corporate actions, Google News, NSE RSS
                         feeds — inputs to qualitative_flags.py
trade_store.py           persistence: SQLite by default, Postgres when DATABASE_URL is set
```

The data engine in `analysis/` is intentionally Streamlit-free so it can be unit-tested and reused.

---

## 🧪 Tests & CI

```bash
py -m pytest -m "not slow" -q      # default suite (what CI runs)
py -m pytest -m slow -q            # gated end-to-end backtest smoke (needs network)
```

- **Page smoke** (`tests/test_pages_smoke.py`) loads all 18 pages headlessly with network blocked,
  so they take the graceful degraded path → deterministic.
- **Valuation regression** (`tests/test_valuation_golden_snapshot.py`) replays captured inputs through
  the pure engine and fails on posture/confidence drift.
- **CI** (`.github/workflows/ci.yml`) runs `pytest -m "not slow"` on every push/PR to `main`.
- **Automerge** (`.github/workflows/automerge.yml`): label a PR `automerge` and it is squash-merged
  once CI's `test` and `real-postgres` jobs pass on its latest commit (same-repo, non-draft PRs only).
  It is a convenience, not branch protection — manual merges and direct pushes are still possible
  (required checks need GitHub Pro on a private repo).

### Optional: `AUTOMERGE_TOKEN` secret

Without it, Automerge merges with the built-in `GITHUB_TOKEN`, and GitHub doesn't start other
workflows from that merge, so CI's push-to-`main` run is skipped for automerged PRs (the PR's own
CI run already tested that exact commit; Streamlit Cloud still deploys). To get the `main` run back:

1. GitHub → **Settings → Developer settings → Personal access tokens → Fine-grained tokens →
   Generate new token**.
2. **Resource owner:** your account. **Repository access:** *Only select repositories* →
   `nse-smart-investor`. Pick an expiry and set a reminder to renew it.
3. **Repository permissions:** **Contents: Read and write** and **Pull requests: Read and write**
   (Metadata: Read-only is added automatically). Nothing else.
4. Generate and copy the token.
5. In the repo: **Settings → Secrets and variables → Actions → New repository secret**.
   Name `AUTOMERGE_TOKEN`, paste the token, save.

The workflow uses `AUTOMERGE_TOKEN` when it exists and falls back to `GITHUB_TOKEN` otherwise, so
nothing else changes. Merges then appear under your account. If the token expires, Automerge runs fail
(red in the Actions tab) until you replace the secret, or delete it to fall back to `GITHUB_TOKEN`.

---

## 🌐 Deployment

Pushes to `main` auto-deploy on **Streamlit Community Cloud**. For durable paper-trade/watchlist
persistence across redeploys, set a `DATABASE_URL` (see Configuration) — SQLite is ephemeral on Cloud.

---

## 📚 Further reading

New to the codebase? Start with **[`HANDOFF.md`](HANDOFF.md)** — a cold-start guide covering the
architecture, test/CI commands, deploy/persistence steps, and a "how to resume" checklist. Deeper
decision docs (valuation spec, sector-awareness audit, data-provider decisions) are listed there.

---

*Built as a personal, no-budget project. No paid data APIs.*
