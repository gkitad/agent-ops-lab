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

# MCP server and agent: policy tests, Python environment, tokens, services.
(cd "$ROOT/opsmcp" && python3 test_policy.py)
if [ ! -x "$ROOT/.venv/bin/python" ]; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get install -y -q python3-venv >/dev/null 2>&1 || { apt-get update -q >/dev/null; apt-get install -y -q python3-venv >/dev/null; }
  python3 -m venv "$ROOT/.venv"
fi
REQ=$(sha256sum "$ROOT/requirements.txt" | cut -c1-16)
if [ "$(cat "$ROOT/.venv/.requirements" 2>/dev/null)" != "$REQ" ]; then
  "$ROOT/.venv/bin/pip" install -q --disable-pip-version-check -r "$ROOT/requirements.txt"
  echo "$REQ" > "$ROOT/.venv/.requirements"
fi
install -d -m 700 /etc/agent-ops-lab
for t in agent operator; do
  [ -s /etc/agent-ops-lab/$t.token ] || (umask 077; openssl rand -hex 32 > /etc/agent-ops-lab/$t.token)
done
systemctl enable opsmcp.service opsagent.service >/dev/null 2>&1
systemctl restart opsmcp.service
systemctl restart opsagent.service

git -C "$ROOT" rev-parse HEAD > /var/lib/agent-ops-lab/deployed
rm -f /etc/systemd/system/labctl.service.d/fallback.conf && systemctl daemon-reload
systemctl restart labctl.service 2>/dev/null || true

# Public HTTPS proxy (Caddy) in front of labctl and the incident service.
IP=$(curl -fsS --max-time 5 http://169.254.169.254/metadata/v1/interfaces/public/0/ipv4/address || hostname -I | awk '{print $1}')
sed -e "s/PUBLIC_HOST/${IP//./-}.sslip.io/" -e "s/PUBLIC_IP/$IP/" "$ROOT/deploy/public/Caddyfile.template" > /var/lib/agent-ops-lab/Caddyfile
docker rm -f public-proxy >/dev/null 2>&1 || true
docker run -d -q --name public-proxy --restart unless-stopped --network host \
  -v /var/lib/agent-ops-lab/Caddyfile:/etc/caddy/Caddyfile:ro -v caddy-data:/data caddy:2.8.4 >/dev/null
sleep 5
if curl -fsS -o /dev/null --max-time 40 "http://$IP/api/lab"; then
  echo "public: https://${IP//./-}.sslip.io"
else
  # Never lose the status page: serve labctl directly on port 80 until the next deploy.
  echo "public proxy failed; labctl falls back to port 80"
  docker logs --tail 20 public-proxy 2>&1 || true
  docker rm -f public-proxy >/dev/null 2>&1 || true
  mkdir -p /etc/systemd/system/labctl.service.d
  printf '[Service]\nEnvironment=HOST=0.0.0.0 PORT=80\n' > /etc/systemd/system/labctl.service.d/fallback.conf
  systemctl daemon-reload && systemctl restart labctl.service
fi
echo "deployed $(git -C "$ROOT" log -1 --format='%h %s')"
