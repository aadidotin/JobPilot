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

Then create it — on a fresh deploy, build the schema through Alembic so the
database is recorded at the current migration version from the start:

```bash
mkdir -p data
uv run alembic upgrade head
```

(`uv run jobpilot initdb` also builds the tables, but it does not record a
migration version, so a later `alembic upgrade` would collide with the tables
it already made. Prefer `alembic upgrade head` for a brand-new database.)

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

## 4. Nightly encrypted backup

```
0 2 * * * $HOME/Desktop/Self-Product/JobPilot/deploy/run-backup.sh
```

Archives land in `~/.local/share/jobpilot/backups` (newest 14 kept), encrypted
with a passphrase generated once at `~/.config/jobpilot/backup.key`.

**Copy that key somewhere off this machine.** Without it the backups are
unreadable, and it is not itself included in them. The key lives in your home
directory, so this protects backups that leave the laptop — not against someone
who already has your home directory.

Restore:

```bash
uv run python -c "from pathlib import Path; from jobpilot.backup import restore; \
  restore(Path('$HOME/.local/share/jobpilot/backups/jobpilot-YYYYMMDDThhmmss.db.gz.gpg'), \
          Path('restored.db'))"
```

Backups matter more than the job rows suggest: your 👍/👎 annotations are
judgment calls that no re-poll can regenerate.

## 5. Watching the gate

```bash
uv run jobpilot gate          # or /gate in Telegram
```

Reports 👍 per week and the funnel per market x source tier. Weekend 2 is
blocked until it reads PASSES: >=8 👍/week for two consecutive weeks. It
distinguishes a cell that showed you roles you did not like (taste — retune
filters) from one that delivered nothing at all (sourcing — grow
companies.yaml).

## 6. Dead-man's ping (optional but recommended)

Create a check at <https://healthchecks.io> (free tier), period 1h, grace 24h
during laptop-only operation, and put its ping URL in `.env`:

```
HEALTHCHECKS_PING_URL=https://hc-ping.com/your-uuid-here
```

This catches "didn't run". The per-tier silence alert inside the pipeline
catches the other failure — "ran, but ingested nothing".

Create a **second, separate** check for the bot daemon (period 10m, grace 20m —
it pings every 5 minutes) and add:

```
HEALTHCHECKS_BOT_PING_URL=https://hc-ping.com/a-different-uuid
```

Separate on purpose: sharing one URL would let the bot's healthy pings mask a
dead pipeline, which is the exact failure the first check exists to catch.

The bot check is not redundant with `Restart=always`. systemd only sees the
process exit; the expensive failure is quieter — process up, long-polling
stopped (token revoked, updater dead, network gone). Digests keep arriving,
because those are sent by the pipeline, not the daemon, so the only visible
symptom is buttons that do nothing. Since 👍 taps are what `jobpilot gate`
counts, a silently dead bot reads as "liked nothing this week" and burns gate
weeks. The heartbeat checks both that the updater is polling and that the API
still accepts the token, and hits `/fail` on a bad result so the alert is
immediate rather than waiting out the grace period.

## Verifying without waiting for a window

```bash
uv run jobpilot run                    # ATS only, unless a window is due
uv run jobpilot run --force-jobspy     # also sweep the aggregators now
uv run jobpilot run --force-digest     # also send the digest now
```

`--force-digest` on a fresh database sends up to `digest_max` (25) messages.
That cap is the only thing standing between you and ~290 notifications on day
one, so lower it before raising it.

## Database migrations

The schema is versioned with Alembic. `create_all` (what `jobpilot initdb`
uses) can build a fresh schema but never *change* one — SQLite can't ALTER most
columns in place — so every schema change after the first goes through a
migration. The live database carries the only copy of your 👍/👎 annotations,
so the rule is **migrate, never rebuild.**

To change the schema:

```bash
# 1. Edit the models in jobpilot/models.py, then autogenerate the migration:
uv run alembic revision --autogenerate -m "add whatever column"

# 2. READ the generated file in alembic/versions/ — autogenerate is a draft,
#    not gospel (it misses some constraint and data-move cases).

# 3. Apply it. On SQLite this runs in batch mode (rebuild-and-swap per table),
#    and your rows are preserved:
uv run alembic upgrade head
```

Useful checks:

```bash
uv run alembic current   # which revision the live DB is at
uv run alembic history   # the migration chain
uv run alembic check     # models and DB in sync? (fails if a migration is owed)
uv run alembic downgrade -1   # undo the last migration
```

Before applying a migration to the live DB, rehearse it on a copy — take a
snapshot with `jobpilot.backup.snapshot(DB_PATH, dest)` and point `JOBPILOT_DB`
at the copy. A nightly backup already runs (section 4), but a fresh snapshot
right before a schema change is cheap insurance.

## Turning it off

```bash
systemctl --user disable --now jobpilot-bot
crontab -e     # delete the run-pipeline.sh line
```
