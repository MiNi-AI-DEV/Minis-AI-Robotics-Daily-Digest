# 🤖 Mini's AI Robotics Daily Digest

An automated agent that:
1. Pulls the latest AI & Robotics news from ~20 curated RSS feeds (arXiv, OpenAI, MIT, IEEE Spectrum, TechCrunch, etc. — see `feeds.json`), up to 5 stories per site.
2. Uses **Gemini** to write a title + a 5-6 line summary for every single story (one card per news item, not a grouped blob).
3. Guarantees **at least 25 stories** per digest — if there isn't enough fresh news yet, it automatically widens the time window and re-checks until it has enough.
4. **Never repeats a story** — every article is permanently tracked in `state.json`, so once it's sent, it's never sent again (not tomorrow, not next year).
5. Renders a polished **HTML + CSS + JS page** with a live search box to filter stories.
6. Sends that page to **your Telegram chat** every morning at **4:30 AM IST** via a bot, using **GitHub Actions** as the free scheduler — no server needed.

---

## 1. What you need before starting

- A GitHub account
- The Telegram bot you already created with BotFather (you'll have a **bot token** that looks like `123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`)
- A **Gemini API key** from https://aistudio.google.com/apikey

---

## 2. Create the repo

1. On GitHub, click **New repository**.
2. Name it `Minis-AI-Robotics-Daily-Digest` (or any name you like).
3. Keep it **Public** or **Private** — either works.
4. Don't initialize with a README (you'll upload these files instead).
5. Upload **all the files/folders** from this project (keep the folder structure, especially `.github/workflows/daily-digest.yml`).

---

## 3. Get your Telegram Chat ID

Your bot needs to know *where* to send messages.

1. Open Telegram and search for your bot (the one you made with BotFather), then send it any message, e.g. "hi".
2. In your browser, go to:
   `https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates`
   (replace `<YOUR_BOT_TOKEN>` with your real token)
3. You'll see JSON like:
   ```json
   { "result": [ { "message": { "chat": { "id": 123456789, ... } } } ] }
   ```
4. That number (`123456789`) is your **Chat ID**. Copy it.

> Tip: If you want the digest sent to a Telegram **channel** instead of a personal chat, add the bot as an admin of the channel and use the channel's `@username` or its numeric ID as the Chat ID.

---

## 4. Add your secrets to GitHub

In your new repo:

1. Go to **Settings → Secrets and variables → Actions → New repository secret**.
2. Add these three secrets one at a time:

| Secret name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | The token from BotFather |
| `TELEGRAM_CHAT_ID` | The chat ID you found in step 3 |
| `GEMINI_API_KEY` | Your Gemini API key |

That's it — no keys are ever written into the code itself.

---

## 5. Enable the workflow

1. Go to the **Actions** tab of your repo.
2. GitHub may ask you to confirm you want to enable workflows — click **I understand my workflows, go ahead and enable them**.
3. You'll see **"Daily AI & Robotics Digest"** listed. It's scheduled to run automatically at **23:00 UTC (4:30 AM IST)** every day.
4. To test it right now instead of waiting: open the workflow → click **Run workflow** → **Run workflow** again to confirm.
5. Check your Telegram — within a minute or two you should receive a `digest.html` file from your bot.

---

## 6. How it works under the hood

```
GitHub Actions (cron, daily)
        │
        ▼
digest_agent.py
        │
        ├── Reads feeds.json → fetches each RSS feed (feedparser)
        ├── Filters to items from the last ~26 hours
        ├── De-dupes against state.json (so you never get repeats)
        ├── Sends the fresh items to Gemini → gets back a grouped Markdown digest
        ├── Renders a styled HTML page → output/digest.html
        └── Sends output/digest.html to your Telegram chat via the Bot API
```

After each run, the workflow commits the updated `state.json` (list of already-seen article IDs) and the latest `output/digest.html` back into the repo, so the next run knows what's new.

---

## 7. Customizing

- **Change the wake-up time**: edit the `cron` line in `.github/workflows/daily-digest.yml`. Cron times are in UTC. For example, `30 1 * * *` = 07:00 IST. Use a tool like [crontab.guru](https://crontab.guru) to build other times.
- **Add/remove feeds**: edit `feeds.json`. Each entry needs a `name`, `url`, and `tier` (`must-have` or `recommended`).
- **Change how far back it looks**: set `LOOKBACK_HOURS` as a repo variable or edit the default in `digest_agent.py`.
- **Change the Gemini model**: edit the `model = genai.GenerativeModel("gemini-2.0-flash")` line in `digest_agent.py`.
- **No Gemini key?** The script still works — it'll fall back to a simple grouped list of headlines/links without AI summarization.

---

## 8. Files in this repo

| File | Purpose |
|---|---|
| `digest_agent.py` | Main script: fetch → dedupe → summarize → render → send |
| `feeds.json` | Curated list of RSS feeds to poll |
| `state.json` | Tracks which article IDs have already been sent (auto-updated) |
| `requirements.txt` | Python dependencies |
| `.github/workflows/daily-digest.yml` | The GitHub Actions schedule + job |
| `output/digest.html` | The most recent digest (auto-updated each run) |

---

## 9. Troubleshooting

- **Nothing arrives in Telegram**: double-check the three secrets are spelled exactly as above, and that you've messaged your bot at least once before fetching the chat ID.
- **Workflow shows a red ❌**: click into the run → check the log. Most common causes are a missing/incorrect secret or a temporarily unreachable RSS feed (the script logs a warning and skips it rather than failing).
- **You get "No new stories"**: this just means everything currently in the feeds was already sent in a previous run — check back tomorrow, or lower `LOOKBACK_HOURS`.
