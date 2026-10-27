#!/usr/bin/env bash
# One-time setup for a fresh Ubuntu 24.04 droplet. Run as root:
#   curl -fsSL https://raw.githubusercontent.com/gkitad/agent-ops-lab/main/deploy/bootstrap.sh | bash
set -euo pipefail
REPO=https://github.com/gkitad/agent-ops-lab
ROOT=/opt/agent-ops-lab

export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q docker.io docker-compose-v2 git python3 ufw openssl
systemctl enable --now docker

# A little swap so image builds don't exhaust 2 GB of RAM.
if [ ! -f /swapfile ]; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

[ -d "$ROOT/.git" ] || git clone "$REPO" "$ROOT"
[ -f "$ROOT/lab/.env" ] || echo "DB_PASSWORD=$(openssl rand -hex 16)" > "$ROOT/lab/.env"
mkdir -p /var/log/agent-ops-lab /var/lib/agent-ops-lab

cp "$ROOT"/deploy/systemd/* /etc/systemd/system/
systemctl daemon-reload
bash "$ROOT/deploy/deploy.sh"
systemctl enable --now labctl.service lab-update.timer
echo "Bootstrap complete. Status: http://$(curl -fsS https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')/"
