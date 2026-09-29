# Background Market Alerts (Telegram)

Get a Telegram message when a stock crosses your price level, when India VIX
turns fearful, or when Nifty breaks into a downtrend — **even when the
dashboard is closed.**

A GitHub Actions job runs `alerts/check_alerts.py` every 15 minutes during
NSE market hours (Mon–Fri, 09:15–15:30 IST) and sends alerts via a Telegram bot.

---

## One-time setup (≈5 minutes)

### 1. Create a Telegram bot
1. Open Telegram, search for **@BotFather**, start a chat.
2. Send `/newbot`, follow the prompts (name + username).
3. BotFather replies with a **bot token** like `7123456789:AAH...xyz`. Copy it.

### 2. Get your chat ID
1. Search for **@userinfobot** in Telegram, start it.
2. It replies with your numeric **Id** (e.g. `123456789`). Copy it.
3. **Important:** send your new bot any message (e.g. "hi") once, so it's
   allowed to message you back.

### 3. Add the secrets to GitHub
1. Go to your repo → **Settings → Secrets and variables → Actions**.
2. Click **New repository secret** and add these two:
   | Name | Value |
   |------|-------|
   | `TELEGRAM_BOT_TOKEN` | the bot token from step 1 |
   | `TELEGRAM_CHAT_ID`   | your chat id from step 2 |
3. *(Optional)* For real-time prices instead of 15-min-delayed Yahoo data,
   also add `ANGEL_API_KEY`, `ANGEL_CLIENT_ID`, `ANGEL_PASSWORD`,
   `ANGEL_TOTP_SECRET` (same values you put in Streamlit secrets).

### 4. Turn it on
The workflow is already committed. To verify:
- Go to the **Actions** tab → **Market Alerts** → **Run workflow**
  (it respects the market-hours guard, so outside 09:15–15:30 IST it logs
  "outside NSE market hours" and sends nothing — test during market hours).
- You should receive any currently-triggered alerts in Telegram within ~30 s.

> **Note:** GitHub's free scheduled runs can be delayed a few minutes under load
> and don't fire on the exact second. Fine for position alerts, not for
> millisecond scalping.

---

## Managing your price alerts

Edit **`data/alerts.csv`** — directly on GitHub (pencil icon) or locally, then commit.

| column | meaning | example |
|--------|---------|---------|
| `ticker` | NSE symbol (no `.NS`) | `RELIANCE` |
| `condition` | `above` or `below` | `above` |
| `level` | price threshold (₹) | `1400` |
| `note` | free text shown in the alert | `Breakout watch` |
| `enabled` | `1` = active, `0` = off | `1` |

Example:
```csv
ticker,condition,level,note,enabled
RELIANCE,above,1400,Breakout above resistance,1
TCS,below,3700,Stop-loss zone,1
```

Each alert fires **once per day** (de-duplicated), so you won't get spammed
every 15 minutes while the condition holds.

---

## What gets checked each run

| Check | Trigger |
|-------|---------|
| **Price alerts** | any enabled row in `alerts.csv` whose level is crossed |
| **VIX regime** | India VIX enters `fear` (>22) or `panic` (>28) |
| **Nifty trend** | Nifty closes below both SMA20 and SMA50 (downtrend) |

---

## Reliable 15-minute cadence (external trigger)

GitHub's own `schedule:` cron is best-effort and in practice was firing only
~2 of the ~26 daily slots (often after market close). The workflow now relies
on an external scheduler calling GitHub's `workflow_dispatch` API instead.
The GitHub cron is kept only as a fallback.

### 1. Create a fine-grained GitHub token
GitHub → Settings → Developer settings → **Fine-grained tokens** → Generate:
- Repository access: **only** `nse-smart-investor`
- Permissions: **Actions → Read and write** (nothing else)
- Pick an expiry and set a reminder to rotate it.

### 2. Create the cron jobs (cron-job.org, free)

On 2026-09-28 (a Monday) GitHub's scheduler ran **one** warm-up and **zero**
Market Alerts / digest / pre-open runs during market hours, so every job below
needs an external trigger. All share the same settings:

- Method: **POST**, timezone **Asia/Kolkata** (set it on the account *and* in
  each job's Advanced tab), days **Mon–Fri** (except the weekly digest)
- Headers (all four — without `Content-Type` cron-job.org doesn't send the
  body as JSON and GitHub answers **422**):
  - `Authorization: Bearer <your token>`
  - `Accept: application/vnd.github+json`
  - `X-GitHub-Api-Version: 2022-11-28`
  - `Content-Type: application/json` (the "Add Content-Type header now" button under the body adds it)
- URL: `https://api.github.com/repos/mrinaljhunjhunwala-ui/nse-smart-investor/actions/workflows/<workflow file>/dispatches`
- Schedule: pick **Custom** and paste the crontab below. "Every day at…" also
  fires on weekends.
- Request body: the box only appears once the method is POST. Type the JSON
  with straight quotes — curly quotes get a 400, an empty body a 422.

| # | Workflow file | Body | Crontab (IST) | Schedule | What it does |
|---|---|---|---|---|---|
| 1 | `warm-preopen-scan.yml` | `{"ref":"main"}` | `40 8 * * 1-5` | 08:40 | Fresh Top Picks + Tomorrow's Watchlist, then sends the **Morning Delivery Digest** |
| 2 | `market-digest.yml` | `{"ref":"main","inputs":{"mode":"intraday-watchlist","force":"false"}}` | `20 9 * * 1-5` | 09:20 | Intraday framing of the top picks |
| 3 | `market-alerts.yml` | `{"ref":"main"}` | `*/15 9-15 * * 1-5` | every 15 min, 09:00–15:45 | Price alerts, India VIX regime, Nifty trend |
| 4 | `warm-top-picks.yml` | `{"ref":"main"}` | `15,45 9-15 * * 1-5` | every 30 min, 09:15–15:45 | Keeps the dashboard's Top Picks snapshot fresh |
| 5 | `market-digest.yml` | `{"ref":"main","inputs":{"mode":"momentum","force":"false"}}` | `0,30 10-14 * * 1-5` | every 30 min, 10:00–14:30 | Momentum-scan digest (max one AM + one PM) |
| 6 | `market-digest.yml` | `{"ref":"main","inputs":{"mode":"portfolio-posture","force":"false"}}` | `0 14 * * 1-5` | 14:00 | Angel One holdings posture |
| 7 | `warm_tomorrow_watchlist.yml` | `{"ref":"main"}` | `50 15 * * 1-5` | 15:50 | Tomorrow's Watchlist after close |
| 8 | `market-digest.yml` | `{"ref":"main","inputs":{"mode":"weekly-digest","force":"false"}}` | `0 9 * * 0` | Sunday 09:00 | Week-ahead summary |

Fastest way to add jobs 2–8: open a working job's ⋯ menu → **Clone**, then
change the title, URL, body and crontab. Clones start **Inactive** — flip
"Enable job" before saving.

`"force":"false"` matters for `market-digest.yml`: without it a dispatched run
is treated as a manual test (skips the market-hours guard **and** the
once-per-day dedup), and you'd get a digest on every trigger.

Start with #1 and #3 — they carry the alerts. The scripts guard the exact
market window and NSE holidays themselves, so triggering on a holiday is a
harmless no-op.

A successful call returns **HTTP 204**; a `workflow_dispatch` run should appear
in the Actions tab within a minute. Reading a failure: **401** = bad/expired
token, **403/404** = token lacks Actions write on this repo, **422** = missing
body or `Content-Type`. cron-job.org's "Test run" button sits behind a bot
check that ad blockers can break ("couldn't verify your test run request");
if so, set the crontab two minutes ahead, check the job's History, then put
the real crontab back. Turn on "Save responses in job history" to see
GitHub's error text there. The GitHub `schedule:` crons stay in the
workflow files as a fallback — duplicates are serialised by concurrency groups
and de-duplicated by `alert_state.json`.

Cost (private repo): roughly 130 runner-minutes per trading day ≈ 2,900
Actions minutes/month with all eight; #1–#3 alone ≈ 1,200. The free plan
includes 2,000/month (Pro: 3,000).

To run it yourself locally:
```bash
python alerts/check_alerts.py --force   # --force ignores market-hours guard
```
Without Telegram secrets set, it prints the alerts to the console instead of sending.
