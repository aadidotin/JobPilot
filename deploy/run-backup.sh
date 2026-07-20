#!/usr/bin/env bash
# Nightly encrypted backup (eng T5, CEO T4). Separate from the pipeline cron on
# purpose: the backup must still run on a night the pipeline is failing.
#
# Install (02:00 daily):
#   crontab -e
#   0 2 * * * $HOME/Desktop/Self-Product/JobPilot/deploy/run-backup.sh
#
# Restore:
#   uv run python -c "from pathlib import Path; from jobpilot.backup import restore; \
#     restore(Path('<archive>.db.gz.gpg'), Path('restored.db'))"
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1

export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
LOG_DIR="${JOBPILOT_LOG_DIR:-data/logs}"
mkdir -p "$LOG_DIR"

exec >>"$LOG_DIR/backup.log" 2>&1
echo "=== $(date -Is) backup start ==="
uv run jobpilot backup
echo "=== $(date -Is) backup exit=$? ==="
