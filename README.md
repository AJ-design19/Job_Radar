# Job Radar

Scans company career boards (Greenhouse, Lever, Ashby), an optional job-search API and any pages you
watch. Emails you instantly for strong matches, tells you when a posting closes, and sends a daily
digest at 08:00 IST. Runs free on GitHub Actions.

## Setup (10 minutes)

1. **Create a GitHub repo** and push this folder.
   - Public repo: unlimited Actions minutes, so the 10-minute schedule is fine.
   - Private repo: 2,000 free minutes/month, so change `*/10` to `*/30` in `scan.yml`.
2. **Gmail App Password:** turn on 2-Step Verification, then create one at
   <https://myaccount.google.com/apppasswords>.
3. **Repo → Settings → Secrets and variables → Actions**, add:
   | Secret | Value |
   |---|---|
   | `GMAIL_ADDRESS` | the Gmail that sends the mail |
   | `GMAIL_APP_PASSWORD` | the 16-character app password |
   | `NOTIFY_TO` | (optional) where to receive; defaults to `GMAIL_ADDRESS` |
   | `GEMINI_API_KEY` | (optional) smarter screening, deadline + eligibility extraction |
   | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | (optional) instant phone push via @BotFather |
   | `ADZUNA_APP_ID`, `ADZUNA_APP_KEY` | (optional) wider job search |
4. **Actions tab → scan → Run workflow** with `command = test`. You should get a test email.
5. Run `scan` once manually. The first run only builds a baseline (no alert flood). From then on it
   alerts on new matches, and the digest arrives daily.

Edit `config.yaml` to add companies, change keywords or locations, or add `watch_urls`.

## Local run

```bash
pip install -r requirements.txt
python agent.py scan      # without secrets, emails are printed instead of sent
python agent.py digest
```

## How "is it open?" works
- ATS boards: if a job vanishes from a board that loaded fine, it is marked closed.
- Search-API jobs: the daily digest re-opens each posting page and looks for 404s, removal redirects
  and phrases like "no longer accepting applications".
- Watch URLs: you get an alert whenever the status flips open ↔ closed.

## Limits
- Latency is about 10-15 minutes, not seconds (GitHub cron granularity and delays).
- Google, Microsoft and Amazon use custom portals with no public API. Use `watch_urls` and the
  career site's own email alerts for those.
- Wrong company slugs are harmless and are listed under "Source problems" in the digest.
