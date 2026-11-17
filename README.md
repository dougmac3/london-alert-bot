# London alert bot

Checks GDACS, Met Office (London & SE), BBC UK/World and GOV.UK every 10 minutes and pushes to your phone through ntfy:

- **Urgent (score 8+):** pushed straight away at max priority, with sound.
- **Notable (5–7):** saved for a daily digest at 08:00 UTC.
- **Everything else:** dropped.

The daily digest is also the heartbeat. **If it stops arriving, the bot is down.** If no feed can be reached for 3 runs in a row, the bot sends you a warning.

It runs on GitHub's servers, so it keeps going if your laptop is off, your flat loses power, or Claude is down.

## Setup (about 10 minutes)

1. **Phone:** install **ntfy** (iOS or Android). Subscribe to a long random topic name, e.g. `doug-alerts-7f3k9q2x`. Anyone who knows the name can read it, so don't make it guessable. On iOS, allow notifications. On Android, set the max-priority channel to override Do Not Disturb.
2. **GitHub:** create a new **public** repo and upload these files. Keep the `.github/workflows/` folder. Public repos get unlimited free Actions minutes; a private repo running every 10 minutes would go over the free 2,000 minutes a month. The repo holds only code and headlines. Your secrets stay hidden.
3. **Repo → Settings → Secrets and variables → Actions**, then add:
   - `NTFY_TOPIC`: your topic name
   - `ANTHROPIC_API_KEY` (optional): turns on Claude scoring, which is much better than keyword matching. It costs pennies a day on Haiku.
4. **Actions tab → alert-bot → Run workflow.** You should get a "bot is live" push.

## Tuning
Set these as `env:` lines in `.github/workflows/alerts.yml`:
`URGENT_THRESHOLD` (default 8), `DIGEST_THRESHOLD` (5), `DIGEST_HOUR_UTC` (8).
To add a source, edit the `FEEDS` dict in `alert_bot.py`.

## Limits
- Your phone needs a data connection to receive a push. If mobile data is down, your fallbacks are the UK Emergency Alerts system (cell broadcast, works without data) and FM radio.
- GitHub's cron can run a few minutes late under load, so treat this as a backup to official alerts, not a replacement.
