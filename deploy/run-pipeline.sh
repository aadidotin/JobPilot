#!/usr/bin/env bash
# Cron entry point. Cron gets a near-empty environment, so everything the run
# needs (PATH to uv, working directory) is established here rather than assumed.
#
# Install (every 30 min):
#   crontab -e
#   */30 * * * * $HOME/Desktop/Self-Product/JobPilot/deploy/run-pipeline.sh
#
# Concurrency is handled inside the pipeline by an flock (E10: skip, don't
# queue), so overlapping cron ticks are safe.
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1

export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
LOG_DIR="${JOBPILOT_LOG_DIR:-data/logs}"
mkdir -p "$LOG_DIR"

exec >>"$LOG_DIR/pipeline.log" 2>&1
echo "=== $(date -Is) run start ==="
uv run jobpilot run
echo "=== $(date -Is) run exit=$? ==="
