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

# MCP server: policy tests, Python environment, tokens, service.
(cd "$ROOT/opsmcp" && python3 test_policy.py)
if [ ! -x "$ROOT/.venv/bin/python" ]; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get install -y -q python3-venv >/dev/null 2>&1 || { apt-get update -q >/dev/null; apt-get install -y -q python3-venv >/dev/null; }
  python3 -m venv "$ROOT/.venv"
fi
REQ=$(sha256sum "$ROOT/opsmcp/requirements.txt" | cut -c1-16)
if [ "$(cat "$ROOT/.venv/.requirements" 2>/dev/null)" != "$REQ" ]; then
  "$ROOT/.venv/bin/pip" install -q --disable-pip-version-check -r "$ROOT/opsmcp/requirements.txt"
  echo "$REQ" > "$ROOT/.venv/.requirements"
fi
install -d -m 700 /etc/agent-ops-lab
for t in agent operator; do
  [ -s /etc/agent-ops-lab/$t.token ] || (umask 077; openssl rand -hex 32 > /etc/agent-ops-lab/$t.token)
done
systemctl enable opsmcp.service >/dev/null 2>&1
systemctl restart opsmcp.service

git -C "$ROOT" rev-parse HEAD > /var/lib/agent-ops-lab/deployed
systemctl restart labctl.service 2>/dev/null || true
echo "deployed $(git -C "$ROOT" log -1 --format='%h %s')"
