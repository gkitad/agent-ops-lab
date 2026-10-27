#!/usr/bin/env bash
# Builds and (re)starts the lab from the current checkout, with every fault restored.
set -euo pipefail
ROOT=/opt/agent-ops-lab
LAB=$ROOT/lab
cd "$LAB"
mkdir -p runtime dns/runtime /var/lib/agent-ops-lab
# Healthy DNS zone before CoreDNS starts.
sed -e "s/SERIAL/$(date +%s)/" -e "s/CRM_IP/172.30.0.40/" dns/zone.template > dns/runtime/lab.internal.db
docker compose --env-file .env build -q
docker compose --env-file .env up -d --remove-orphans --force-recreate
bash faults/fault.sh restore-all
docker image prune -f >/dev/null 2>&1 || true
git -C "$ROOT" rev-parse HEAD > /var/lib/agent-ops-lab/deployed
systemctl restart labctl.service 2>/dev/null || true
echo "deployed $(git -C "$ROOT" log -1 --format='%h %s')"
