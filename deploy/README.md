# Deploying JobPilot on the laptop

Two long-lived pieces: a **cron job** that runs the pipeline every 30 minutes,
and a **bot daemon** that receives your 👍/👎 taps and `/applied` commands.
The pipeline sends; the daemon receives. Both share one SQLite file in WAL mode.

## 1. Configure

```bash
cp .env.example .env      # then fill in TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID
```

Point the database at a real, gitignored location by adding to `.env`:

```
JOBPILOT_DB=data/jobpilot.db
```

Then create it:

```bash
mkdir -p data
uv run jobpilot initdb
```

## 2. Bot daemon (receives taps)

```bash
mkdir -p ~/.config/systemd/user
cp deploy/jobpilot-bot.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now jobpilot-bot
loginctl enable-linger "$USER"     # keeps it running after logout
systemctl --user status jobpilot-bot
```

Send the bot `/start` to confirm it answers.

## 3. Pipeline (cron, every 30 min)

```bash
crontab -e
```

Add:

```
*/30 * * * * $HOME/Desktop/Self-Product/JobPilot/deploy/run-pipeline.sh
```

Logs land in `data/logs/pipeline.log`. Overlapping ticks are safe — the run
takes an flock and skips rather than queues if the previous one is still going.

Which legs actually fire is decided inside the run, from `config/schedule.yaml`:
ATS boards every run, JobSpy sweeps 3x/day, digest once a day at 20:00. A laptop
that was asleep through a window does one catch-up when it wakes, not three.

## 4. Dead-man's ping (optional but recommended)

Create a check at <https://healthchecks.io> (free tier), period 1h, grace 24h
during laptop-only operation, and put its ping URL in `.env`:

```
HEALTHCHECKS_PING_URL=https://hc-ping.com/your-uuid-here
```

This catches "didn't run". The per-tier silence alert inside the pipeline
catches the other failure — "ran, but ingested nothing".

## Verifying without waiting for a window

```bash
uv run jobpilot run                    # ATS only, unless a window is due
uv run jobpilot run --force-jobspy     # also sweep the aggregators now
uv run jobpilot run --force-digest     # also send the digest now
```

`--force-digest` on a fresh database sends up to `digest_max` (25) messages.
That cap is the only thing standing between you and ~290 notifications on day
one, so lower it before raising it.

## Turning it off

```bash
systemctl --user disable --now jobpilot-bot
crontab -e     # delete the run-pipeline.sh line
```
