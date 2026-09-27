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

### 2. Create the cron job (cron-job.org, free)
- URL: `https://api.github.com/repos/mrinaljhunjhunwala-ui/nse-smart-investor/actions/workflows/market-alerts.yml/dispatches`
- Method: **POST**
- Headers:
  - `Authorization: Bearer <your token>`
  - `Accept: application/vnd.github+json`
  - `X-GitHub-Api-Version: 2022-11-28`
- Body: `{"ref":"main"}`
- Schedule: every 15 minutes, 09:00–15:45, Mon–Fri, timezone **Asia/Kolkata**
  (the script itself guards the exact 09:15–15:30 window and NSE holidays).

### 3. Second cron job: pre-open scan + Morning Delivery Digest
Same token, headers and body; only the URL and schedule differ:
- URL: `https://api.github.com/repos/mrinaljhunjhunwala-ui/nse-smart-investor/actions/workflows/warm-preopen-scan.yml/dispatches`
- Schedule: **08:40 IST**, Mon–Fri

This job refreshes the Top Picks snapshot and sends the Morning Delivery
Digest as soon as it's written (~2–3 min). GitHub's own 08:50 IST cron for it
was starting as late as 14:00 IST.

A successful call returns **HTTP 204**. Check the Actions tab for a
`workflow_dispatch` run of **Market Alerts** within a minute.

Cost: ~26 runs × ~1–2 min ≈ 1,100 Actions minutes/month.

To run it yourself locally:
```bash
python alerts/check_alerts.py --force   # --force ignores market-hours guard
```
Without Telegram secrets set, it prints the alerts to the console instead of sending.
