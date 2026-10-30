# Setting up the bot on GitHub (no git skills needed — pick A or B)

## 0. Before anything: enable Telegram
Open [t.me/nofomoherebot](https://t.me/nofomoherebot) and press **START** (or send "hi").
The bot discovers your chat id from this message and its first run sends you a test message.

## 1. Create the repository
1. Go to https://github.com/new
2. Repository name: `fx-shadow-bot`
3. Visibility: **Public** (free unlimited Actions minutes)
4. Click **Create repository**

## 2. Get the files in

### Option A — web upload
In the repo click **"uploading an existing file"** and drag in these files from
`/Users/mott/Desktop/fx-shadow-bot/`:
`detector.py`, `mfb_sentiment.js`, `package.json`, `tz_map.json`, `providers.json`,
`state.json`, `LEDGER.md`, `README.md`, `mfb.json`.

Then **Add file → Create new file**, name it exactly `.github/workflows/bot.yml`,
paste the contents of `/Users/mott/Desktop/fx-shadow-bot/.github/workflows/bot.yml`,
commit.

### Option B — terminal one-shot
Paste into Terminal:
```
cd /Users/mott/Desktop/fx-shadow-bot
git init -b main
git add -A
git commit -m "fade bot v2"
git remote add origin https://github.com/nofomohere/fx-shadow-bot.git
git push -u origin main
```
(A browser window will ask you to log in to GitHub — approve it.)

## 3. Add the two secrets
Repo → **Settings → Secrets and variables → Actions → New repository secret**:

| Name | Value |
|---|---|
| `MQL5_COOKIE` | full contents of `/Users/mott/Desktop/fx-copy-backtester/raw/session/cookies.txt` |
| `TELEGRAM_BOT_TOKEN` | `9031268438:AAF_NdcTOhQxuS41Pf2aAuWM4Ab2uajPx3M` |

## 4. Enable and run
1. **Actions** tab → "I understand my workflows, go ahead and enable them"
2. **fade-bot** (left) → **Run workflow** → **Run workflow**
3. Within ~3 minutes your Telegram gets: **"✅ Fade Bot is ONLINE…"** — that's the test message.
   After that: 🔥/🧠 for every fade signal, ✅ for every close, 📊 daily at midnight UTC,
   ⚠️ if the MQL5 cookie dies.

## What the bot does
- Every ~15 min (GitHub's servers — not your computer, not your IP):
  1. Fetches myfxbook retail sentiment → fades pairs where retail is ≥80% one-sided.
  2. Fetches MQL5 provider histories → fades 5+ provider one-sided clusters.
  3. Paper-trades at real Yahoo prices, exits 8h later, logs to `LEDGER.md`, notifies Telegram.
- Zero money, paper only. Watch `LEDGER.md` + Telegram for a few weeks.

## Notes
- MQL5's outage/ban affects YOUR browser and the dev machine, but not GitHub's servers.
  If Telegram shows `⚠️ provider errors 39/39`, the cookie expired — paste a fresh one
  into the secret (value from a fresh DevTools cURL, or ask the assistant).
- After the paper phase proves out (~20-30 trades), the next step is demo execution
  (OANDA practice or Hyperliquid xyz:EUR at high leverage) — decided later, only on evidence.
