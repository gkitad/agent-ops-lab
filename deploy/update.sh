#!/usr/bin/env bash
# Run every minute by lab-update.timer: deploys new commits from main.
set -euo pipefail
ROOT=/opt/agent-ops-lab
LOG=/var/log/agent-ops-lab/deploy.log
cd "$ROOT"
git fetch -q origin main
NEW=$(git rev-parse origin/main)
OLD=$(cat /var/lib/agent-ops-lab/deployed 2>/dev/null || echo none)
[ "$NEW" = "$OLD" ] && exit 0
{
  echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) deploying ${NEW:0:7} (was ${OLD:0:7})"
  git reset -q --hard origin/main
  cp deploy/systemd/* /etc/systemd/system/ && systemctl daemon-reload
  if bash deploy/deploy.sh; then echo "=== ok"; else echo "=== FAILED"; echo "$NEW" > /var/lib/agent-ops-lab/deployed; fi
} >> "$LOG" 2>&1
tail -n 400 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
