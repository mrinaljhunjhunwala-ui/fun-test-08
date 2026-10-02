# Handoff — 2026-09-28 · Bear-regime fix, warmers working, Stooq off, EOD pipeline

## Repo state
- **`main` at `686364c`**: CI passing. Local `main` is synced, and the only untracked item is `archive/`.
- **Canonical checkout:** `D:\Kashti Claude\nse-smart-investor`.
  - `E:\code\nse-smart-investor` is an **empty folder**. Nothing should point at it.
- **Open PRs:** none from this work.
- **Remote branches:** `main`, plus two waiting on a user decision:
  - `alerts/consolidate-fresh-workflow`: PR #93 is still open.
  - `design/mockup-06-smart-screener` is safe to delete (owner to remove): PR #176 was stacked under #178 and its commit landed in that
    squash-merge; the branch was identical to `main`.
  - Deleted on 2026-09-28: `ux2-ticker-hover-preview` (its feature shipped in #150 and #153) and 8 fully merged branches.
- **Worktrees:** `sweet-cannon-bc10b5` belongs to another live session. `quizzical-faraday-f467bc` is no longer
  registered with git, but its folder was locked by a running process; delete it by hand once that process exits.
- **Other work merged in parallel** (not from this session):
  - #174: wide layout is re-asserted on every page.
  - #175 and #178–#182: pages moved onto the mockup layout.
  - The trade_store pool now waits instead of raising `PoolError`.
  - The test suite is blocked from writing to production Postgres, and there's a purge tool for leaked rows.
  - Page-smoke background threads are joined.
  - The verdict_log dedup behaviour is pinned by tests.
  - Also in parallel, 09-27 → 09-28: #188 (Claude may run `gh pr merge`), #190 (pyotp for Angel One),
    #192 (morning digest from the pre-open scan), #195 (bulk scans skip Angel One), #196 (every
    scheduled job has an external trigger), and verdict-ledger fixes (IST session dates, NSE holidays,
    backfill no longer starved).

## Alerts pipeline — state as of 2026-09-29 (#190 #192 #195 #196 #200 #206 #207)

- **GitHub cron is effectively dead for this repo.** On Mon 2026-09-28 it ran 1 warm-up and **zero**
  Market Alerts, digest or pre-open runs during market hours. Every scheduled workflow now takes an
  external `workflow_dispatch` trigger. The 8 targets (URL, body, IST schedule) are in a table in
  `alerts/README.md`.
  - **cron-job.org is live for #1 (pre-open, 08:40) and #3 (Market Alerts, every 15 min)** since
    2026-09-29. Both were checked that night: HTTP 204, and the `workflow_dispatch` runs passed. They use a
    fine-grained token (this repo only, Actions read/write) that lives only in cron-job.org. Jobs #2 and
    #4–#8 are not created yet. The README lists the gotchas: the Content-Type header is required, clones
    start inactive, and ad blockers break the test-run button.
  - **Windows Task Scheduler stopgap: retired 2026-09-30.** Both tasks are deleted. First full market
    morning on cron-job.org alone (30 Sep): the pre-open ran at 08:40 and sent the Morning Digest, and Market
    Alerts ran every slot from 09:00. The 09:15 run sent the Nifty-downtrend alert, and later runs were
    deduped. Both messages reached Telegram. `C:\Users\ASUS\nse-triggers\dispatch.ps1` is kept for manual
    dispatches.
  - **Every alert also goes to Gmail** (`ALERT_GMAIL_*` secrets). The user is thinking about dropping email;
    to do that, delete the three secrets and revoke the app password. No code change is needed.
  - `market-digest.yml` dispatches must send `"force":"false"`. Otherwise they act as manual tests and skip
    the market-hours guard and dedup.
- **Angel One candle API:** it works from GitHub runners and isn't IP-blocked. It returns 403 "exceeding
  access rate" at about 90 calls/min, and a longer-window quota cuts off at about 700 calls.
  - Since #195, one thread at a time fetches candles, within 60/min.
  - Any other thread gets `AngelBusy` and falls through to Yahoo silently.
  - Bulk scans never need Angel.
- **Morning Delivery Digest:** `warm-preopen-scan.yml` sends it right after writing the fresh snapshot
  (#192). Snapshots older than 16h are skipped. First delivered 2026-09-29, late because the PC was off at
  08:40.
- **Portfolio alerts (#206):** built from the My Portfolio holdings (KV `manual_portfolio_holdings`) on every
  Market Alerts run.
  - A ±10% step alert from a stored reference price, which re-anchors on each alert.
  - An alert when the price crosses the average cost.
  - References live in KV `portfolio_alert_anchors` (user `_system`), seeded from the 28-Sep closes.
  - `data/alerts.csv` is back to disabled examples, for custom one-off levels only.
- **Run logs name what fired (#207):** `[sent] <headline>` for each alert, and
  `[main] per-check: price= portfolio= vix= nifty=` per run. The recurring daily alert is the
  **Nifty 50 downtrend** check.
- **CI hygiene (#207):** actions are on their Node-24 majors (checkout v5, setup-python v6, cache v5,
  upload-artifact v6). Runners are pinned to `ubuntu-24.04`, because `ubuntu-latest` moves to Ubuntu 26 on
  2026-10-19.
- **Open decision: Tomorrow's Watchlist in bear regimes.** #168 (25 Sep) and #198 (28 Sep) compress
  bear-regime scores (Technical held at 20/40, RS at 5/10).
  - No stock reaches BUY ≥ 65; the top score on 29 Sep was 62.6.
  - The breakout funnel's first gate is `BUY/STRONG BUY`, so the list is empty by construction.
  - Breakdown fell from 15 to 1–4.
  - Options: rank-based gates in bear regimes, or show the lists as regime-suppressed. Needs a user call.

## Mockup layout rollout — complete 2026-09-30 (#210 #212 #214 #216 #218)

All 11 artboards of the 2026-09-12 "proposed layout" mockup
(https://claude.ai/artifact/TT3yhxsvDHvg4CorKC9Hv4) are on `main`, plus its palette. Earlier pages:
#175, #178–#182.
- **Analyze Stock (#210):** ticker head with actions (Size Position opens the Position Sizer prefilled),
  Overview tab first, posture hero with pillar bars, one combined-read strip (3 horizons + TQS), chart +
  thesis grid, sector-aware fundamentals card, overlay-signals and reference-levels cards. The old
  `verdict_card` (it printed the raw action, e.g. "BUY") is gone.
  - Bugs fixed on the way: Portfolio Fit always failed with holdings (`_th` NameError across fragments);
    the Fundamentals tab showed Debt/Equity for banks; the "your position" lookup never matched.
- **Live Ticker (#212):** dense price table (day high/low + VWAP) and index rail in one
  `@st.fragment(run_every=30s)` while NSE is open; rows flash on a tick. This replaced the whole-page
  `<meta refresh>`. Quote tiers now pass optional `high` / `low` / `vwap`.
  **Not yet seen live during market hours.**
- **Angel One (#214):** status panel, masked credentials, activity log, from new
  `angel_fetcher.status()` / `credential_summary()` and an in-process event log (no network). The posture
  tab shows descriptive labels ("Trend broken", not `EXIT_WATCH`). **The connected state has only been
  checked by the stubbed test** (no credentials on the dev PC).
- **Investor Guide (#216):** each tab is a chapter in a 760px reading column (contents, drop cap, callouts,
  document tables).
- **Palette (#218), dark only (the owner doesn't want light mode):** rust accent `#c88a3f`, muted
  bull / bear / amber, blue-black ground `#0e1116`, and Inter + JetBrains Mono instead of IBM Plex. It's
  driven by `dashboard/shared/tokens.py`; hard-coded copies were mapped in 13 files. A few legacy one-offs
  remain (e.g. `trade_utils._grade_color`).
- **Kit to reuse** (`ui_components.py`, CSS in `design.py`): `stock_head`, `kv_list`, `kit_card`,
  `thesis_grid`, `hero_verdict(pillars=)`, `ticker_table`, `mini_index`, `sparkline`, `fmt_volume`,
  `status_panel`, `activity_log`, plus the earlier `section_header`, `gate_strip`, `level_strip`,
  `signal_table`, etc. Mockup numbers the app doesn't compute are left out, never invented.

## What shipped 2026-09-28, later (#200 → #204)

- **Bear follow-up (#202, docs only):** on the same 10y data, #198 improves every risk_off cell and 3 of 4
  trend_down cells. The exception rests on 5 weeks, so both regimes keep one rule.
  - "Volume neutral in trend_down only" is recorded as a **pre-registered candidate**: judge it on bear weeks
    after 2026-07 before adopting it.
  - Flipping Technical stays rejected.
- **Regime notes (#204):** the badge notes now quote the current 10y per-week 20d IC: trend_up +0.044,
  trend_down +0.069, risk_off +0.035, range ≈ 0. They used to say bear rankings were "weak" or "backwards".
- **Hand checks done:**
  - **Page 04 drift banner:** checked live at 15:00 IST with RELIANCE (down 2.2%). It works as designed. It
    didn't fire because Yahoo's daily data includes today's partial bar during market hours, so entry already
    equals the live price. It only fires when the daily data is a stale close.
  - **Page 18 grade shades:** measured, not eyeballed. Adjacent grades are ΔE ≥ 20 apart, and contrast is at
    least 5.7:1 on the dark background. The letter grade is always shown, so colour isn't the only cue.
- **Parallel work:** #200 added price alerts for the 12 portfolio holdings (±10% and an avg-cost cross).

## What shipped 2026-09-28 (#189 → #198)

**Bear-regime ranking fixed (#198):** full write-up in `docs/SCORE_EFFICACY_BEAR_2026-09-28.md`.
- **Finding:** in trend_down and risk_off, Technical and RS rank backwards (recent strength predicts
  worse returns). Evidence: 10-year study, 484 tickers, 178k weekly observations, 2017-07 → 2026-07.
- **Change:** in bear regimes, Technical is held at 20/40 and RS at 5/10. It uses the same opt-out as
  #168 (`NSE_USE_REGIME_WEIGHTS=0`).
- **Result:** bear-week IC improves in both halves, at both 20 and 60 days (20d 2017–20 −0.024 →
  +0.026, 2021–26 −0.005 → +0.061). Overall 20d IC goes from +0.0209 to +0.0329.
- **Selection:** the variant was chosen on 2022–26 data before the 10y run existed, so 2017–20 is
  out of sample. Variants found after seeing the 10y data (also neutralising Volume, flipping
  Technical) were not adopted.
- **`CompositeScore.technical_raw`:** keeps the raw points. The thesis engine and page 04's ledger tags
  read it (flagged by the verdict-regression review).
- **Fetcher:** new `10y` period. `10y` used to silently return 1y, and Yahoo's `max` is **monthly**
  bars.
- Bear rankings are now mildly right, not strong.

**Warmers finally reach Postgres (#191, #193):**
- **Top Picks warmer:** it had not completed a scan since 2026-09-08. Once the ANGEL_* secrets were
  added, Angel One returned 403 for every candle call from GitHub's runners, and each ticker paid two
  failing calls. The result was 52 cancelled runs; the "successes" were weekend skips.
- **Fix:** Angel credentials were removed from the Top Picks and pre-open warmers, and the Top Picks
  timeout went from 8 to 20 min, with a per-minute heartbeat.
- **Verified:** the scan took 213 s and 744/745 stocks scored. `top_picks_snapshot` and
  `tomorrow_watchlist_snapshot` are both in Postgres.

**Angel One breaker (#194):** candle-API HTTP errors now count as breaker failures, and
`fetch_historical` checks the breaker before making calls. Before, it tripped once in 333 rejections.

**Stooq off by default (#189):** stooq.com now serves a JavaScript browser check, so a script can
never get data. The tier is skipped unless `NSE_ENABLE_STOOQ=1`, and the Data health panel shows it
as disabled.

**Earlier on 09-27 (after #186):** HANDOFF refresh (#187).

## What shipped 2026-09-27 (#170 → #186)

**Composite golden snapshot (#170):**
- `tools/composite_golden.py` generates `data/composite_golden_snapshot.json`: 20 seeded synthetic cases replayed through `score_dataframe()`.
- They cover:
  - trend shapes and short history;
  - running without a benchmark (no RS);
  - bear-regime momentum on and off;
  - high VIX;
  - sector rank and flows;
  - delivery;
  - the positioning add-on on and off.
- `tests/test_composite_golden_snapshot.py` fails on any drift in score, grade, action, pillar points or entry levels. It also asserts that VIX doesn't move points, the bear option is on by default, and the 90/100 caps hold.
- **After an intentional scoring change:** run `py tools/composite_golden.py --update` and review the JSON diff in the PR.

**Technical pillar +1 bug (#171):**
- `_score_technical` summed `pts.values()`, which included the bool `sma_available` (from #163). That gave every stock with a 200-day SMA a free +1.
- It now sums `rsi + macd + sma + adx` explicitly.
- Also fixed: `test_signal_parity.py` failed on weekends because `bdate_range(end=Saturday)` returned n−1 dates.

**Direction-aware ADX: tested, not adopted (#172, docs only):**
- The rule tested: a downtrend (−DI ≥ +DI) is capped at the 1-point ADX tier.
- The result was worse, on the same data scored twice: 20-day IC went from +0.0205 to +0.0188, and the 60-day paired difference was −0.0034 (t −3.5).
- The losses sit in trend_down and range; trend_up was unchanged. Strong downtrends in this sample tended to rebound.
- Write-up: `docs/SCORE_EFFICACY_ADX_DIRECTION_2026-09-26.md`. **Don't retry this without a new angle.**

**Data-health freshness (#173):**
- `probe_stored_datasets()` in `dashboard/shared/data_health.py` reads `MAX(date)` from each of five tables: `fii_dii_daily`, `nse_fii_deriv_daily`, `nse_fno_oi_daily`, `nse_option_chain_daily` and `nse_delivery_daily`.
- It counts NSE sessions behind the latest completed session. That session is today after 20:00 IST, otherwise the previous one, and holidays come from `market_hours`.
- More than one session behind shows as **stale** under "needs attention", together with the refresh command.
- Results are cached for 10 minutes, over one pooled connection with a rollback after any failed query.

**EOD data pipeline moved to GitHub Actions (#177):**
- `.github/workflows/refresh-eod-data.yml` runs at 19:45 and 22:00 IST, Monday to Friday. It covers FII/DII, FII index futures, F&O OI, delivery and the option chain.
  - `--days 3` lets a missed evening heal on the next run.
  - Each step runs even if an earlier one failed.
  - The job fails fast if `DATABASE_URL` is missing.
- **Background:** before this, four Windows scheduled tasks pointed at the empty `E:\` folder and were refused (`0x800710E0`). The data had been stale since about Sep 18.
  - Those tasks are now **disabled**, not deleted, in case one fetch ever has to run locally again.
- **Repo secret `DATABASE_URL`:** it didn't exist before this session. That means the existing warm-top-picks and tomorrow-watchlist workflows had been writing to throwaway SQLite on the runner while reporting success. The user has since added it.
  - The value must be **only** the `postgres://…` URL, not the `url = "…"` line from `secrets.toml`.
- **First run:** every step passed, including the option chain, so NSE did not block the runner.
- **Option-chain cookie:** it can now also come from an `NSE_COOKIE` env var or secret. It isn't set, and wasn't needed on the first run.
- **Local catch-up:** Sep 18–25 filled for delivery, F&O OI and FII index futures. All five datasets are current.

**F&O universe drift (#184):**
- Removed 15 names that are absent from the 2026-09-25 F&O bhavcopy: ABFRL, ACC, BALKRISIND, DALBHARAT, DEEPAKNTR, IGL, IPCALAB, IRCTC, LTTS, NAVINFLUOR, PEL, SUNTV, SYNGENE, TATACHEM and TATACOMM. The list goes from 137 to 122.
- **LTIMindtree now trades as `LTM`** in both cash and F&O. `LTIM.NS` fetches had been failing, so it's renamed everywhere: `fno_universe`, the IT sector list in `data/fetcher.py`, `data/universe.py` and the name map in `dashboard/shared/cache.py`.
- #186 did the same for the valuation golden snapshot and `tools/validate_valuation.py`. It re-captured only that one ticker live: REASONABLE / low confidence, with every input present.

**FII index-futures sub-score (#185):**
- The fixed ±30,000-contract bands pinned the sub-score at 0, because live values sit near −300,000.
- It now uses the latest value's **percentile within the trailing ~250 stored sessions** (`data.nse_fii_deriv.get_latest_fut_idx_pct`): ≥80th → 3, ≥50th → 2, ≥20th → 1, else 0. With fewer than 60 sessions it scores the neutral 1.5.
- History was backfilled to 2024-09-06 (398 sessions). Today's value is at the 0.4th percentile (the most net-short in a year), so it scores 0.
- **The positioning add-on is opt-in** (`NSE_USE_POSITIONING_PILLAR`), so default live scores are unaffected.

**Page 02 speed:** no change needed.
- Its smoke-test call now takes 6.6 s under CI conditions, down from 103 s earlier in the session and about 200 s in the old handoff.
- The gain came from the parallel pool and thread fixes, not from this session.
- The old ~200 s figure was measured with the user-level `DATABASE_URL` set, so tests hit remote Postgres.

## Guard tests (they fail CI, so don't work around them)
- **Advice language:** `test_no_advice_copy.py` bans buy/sell/hold instruction copy in the UI and in `analysis/`.
- **Colours:** `test_tokens.py` allows no raw hex in pages.
- **Pages:** `test_pages_smoke.py` compiles and renders each page with the network blocked.
- **Scoring snapshot:** `test_composite_golden_snapshot.py` is new. When it fails, regenerate the snapshot on purpose rather than loosening the test.
- **Scoring behaviour:**
  - `test_bear_regime_vix_neutral.py`
  - `test_engine_audit_fixes.py`
  - `test_audit_fixes.py`, which includes the TECH-BOOL regression
  - `test_fii_deriv_percentile.py`
- **Valuation:** `test_valuation_golden_snapshot.py` (E1-v2).

## Open / next
1. **Watch scheduled runs** of `refresh-eod-data` (19:45 IST) and the warmers in the Data health
   panel. If NSE blocks the runner, set the `NSE_COOKIE` secret, or re-enable the relevant (disabled)
   Windows task after pointing it at the `D:\` checkout.
2. **Bear regimes:**
   - Keep an eye on the verdict ledger's bear-regime rows.
   - Evaluate the pre-registered "Volume neutral in trend_down" candidate once there are enough bear weeks
     after 2026-07.
3. **Validation gaps:** sector rank, flows, delivery % and positioning can't be replayed
   historically, and survivorship bias is not corrected. The positioning add-on stays opt-in until
   it's validated.
4. **Branches:** decide on `alerts/consolidate-fresh-workflow` (open PR #93).
5. **Mockup follow-ups: closed 2026-10-02.** The owner checked mobile, Live Ticker's live refresh and Angel One
   connected. `FinalVerdict.primary_reason` now uses descriptive action labels.
6. **Data sources:** NSE corp-info times out intermittently. Re-check the F&O list against NSE's
   monthly circular; the drift warning in `fetch_nse_fno_bhavcopy` names the candidates.

## Gotchas
- **Running the app for a live check:** the Browser pane's `preview_start` by name looks in the session's
  original scratch folder, not the repo. Start Streamlit yourself on a spare port (e.g. 8511, with the env vars
  from `.claude/launch.json`) and open it with `preview_start url=http://localhost:8511/<page>`.
  - Page slugs drop the number: `/analyze_stock`, `/tqs_scanner`.
  - A `launch.json` entry's `cwd` must be a relative path **inside** the repo, so to preview a worktree,
    keep the worktree under `.claude/worktrees/`.
  - Dataframes render to a canvas, so read table colours from the code and tokens, not screenshots.
- **Several Claude sessions work in this repo at once.** One ran `git reset --hard` in the main
  checkout and wiped another session's uncommitted edits. **Work in `.claude/worktrees/<name>` on
  your own branch and commit early.**
- **The laptop idle-sleeps overnight.** A long study (the 10y efficacy run takes about 45 min of
  compute) was paused for 11 hours. Hold the machine awake (keep-awake, `until: turn_end`) and wait
  in the turn.
- **Research runs on long periods:** use `--period 10y`, not `max` (monthly bars). Copy
  `research/output/observations.csv` elsewhere before starting the next run. Per-pillar variants
  can be evaluated exactly from the observation columns (pillars add up; RS pts = `RS_Score / 10`).
- **GitHub Actions "success" can mean "skipped".** The warmers exit 0 on weekends and holidays.
  Check the log for `scan done` / `persisted`.
- **Local tests reach production Postgres unless you unset `DATABASE_URL`.** The user-level `DATABASE_URL` points at the production database; the suite now blocks writes, but reads and timings still go remote. To match CI, run `env -u DATABASE_URL py -m pytest ...`, then delete any `trades.db` it creates.
- **Stacked PRs and `--delete-branch`:** squash-merging the base PR deletes its branch, and that **closes** the stacked PR. Merge without deleting, or retarget the stacked PR to `main` first. Afterwards, rebase the stacked branch onto `main` to drop the pre-squash commits.
- **pytest summary grep:** the summary line reads `N passed, M deselected in …`, so grepping for `passed in` misses it. Use `tail`.
- **Profiling page 02:** `cProfile` under AppTest effectively hangs. A `sys._current_frames()` sampler thread works.
- **Background agents:** `isolation: remote` on the Agent tool ran **locally**, in `.claude/worktrees/`, not in the cloud. Verify where it ran, and clean up the worktree (`git worktree remove -f -f` if it's locked).
- **Don't switch branches in the main checkout while tests or agents are running.**
- **Research runs** take about 11 minutes and overwrite `research/output/`, so copy each run to `run_*/` first.
- **`gh pr checks N --watch`** before merging. `gh` sometimes drops the connection ("forcibly closed"); just retry.

## Conventions
- **No buy/sell/hold instruction copy, ever** (CLAUDE.md rule 1).
- **Composite is 0–90, as four pillars (40+25+15+10).** It goes up to 100 only with the opt-in positioning add-on. No candlestick component.
- **`analysis/` stays pure:** no Streamlit imports.
- **Scoring changes:** regenerate the composite snapshot on purpose, run the `verdict-regression-reviewer` agent, and for anything that re-ranks broadly, run a same-data efficacy A/B (`research.score_efficacy`).
- **Windows shell:** use `py`, with `PYTHONUTF8=1`.
- **Merging:** squash-merge after CI passes, when the user has asked for the merge.
