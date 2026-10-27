#!/usr/bin/env bash
# Injects and restores real faults in the lab.
#   fault.sh list
#   fault.sh <fault> inject|restore
#   fault.sh status
#   fault.sh restore-all
set -euo pipefail

LAB="$(cd "$(dirname "$0")/.." && pwd)"
RUNTIME="$LAB/runtime"
STATE="$RUNTIME/faults"
mkdir -p "$STATE"

FAULTS=(disk-full app-crash dns-stale cert-expired db-connections)

dc() { docker compose -f "$LAB/compose.yaml" --env-file "$LAB/.env" "$@"; }
cid() { dc ps -a -q "$1"; }
ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }

change() { # append to the change log the agent can read
  echo "$(ts) $1" >> "$RUNTIME/changes.log"
}

write_zone() { # $1 = IP for crm.lab.internal
  local serial
  serial=$(date +%s)
  sed -e "s/SERIAL/$serial/" -e "s/CRM_IP/$1/" "$LAB/dns/zone.template" > "$RUNTIME/zone.tmp"
  mv "$RUNTIME/zone.tmp" "$LAB/dns/runtime/lab.internal.db"
}

container_log() { # write a line into a container's stdout so it shows in its logs
  docker exec -u 0 "$(cid "$1")" sh -c "echo \"\$(date -u +%Y-%m-%dT%H:%M:%SZ) $2\" > /proc/1/fd/1" || true
}

inject() {
  case "$1" in
    disk-full)
      change "crm-app-02: set log level to DEBUG to troubleshoot slow exports (CHG-2242)"
      change "db-01: moved finance monthly report to business hours (CHG-2238)"
      # A runaway debug log fills the shared storage both app servers write to.
      docker exec "$(cid app2)" sh -c '
        f=/var/lib/crm/debug-export-trace.log
        while dd if=/dev/urandom bs=1M count=4 2>/dev/null | base64 >> $f; do :; done
        true'
      ;;
    app-crash)
      change "db-01: moved finance monthly report to business hours (CHG-2238)"
      container_log app1 "WARN crm-api[crm-app-01]: heap usage 92% (job: export full customer report)"
      docker kill --signal=KILL "$(cid app1)" >/dev/null
      ;;
    dns-stale)
      change "dns-01: migration cleanup for retired load balancer lb-old 172.30.0.15 (CHG-2233)"
      change "crm-app-02: OS patches and reboot (CHG-2232)"
      write_zone 172.30.0.15
      ;;
    cert-expired)
      change "dns-01: rotated DNS provider API tokens (CHG-2228)"
      change "vpn-01: firmware update 9.4.2 (CHG-2231)"
      container_log edge "certbot: renewal failed for crm.lab.internal: DNS-01 challenge: provider API returned 401 Unauthorized"
      docker exec "$(cid edge)" sh -c 'cp /certs/expired.crt /certs/live/crm.crt && nginx -s reload' >/dev/null 2>&1
      ;;
    db-connections)
      change "reporting: new hourly sales export job enabled (CHG-2241)"
      docker exec -d "$(cid db)" sh -c 'for i in $(seq 1 45); do psql -U reporting -d crm -q -c "/* sales-export */ SELECT pg_sleep(86400)" >/dev/null 2>&1 & done; wait'
      sleep 3
      ;;
    *) echo "unknown fault: $1" >&2; exit 2 ;;
  esac
  touch "$STATE/$1"
  echo "injected $1"
}

restore() {
  case "$1" in
    disk-full)
      docker exec "$(cid app2)" sh -c 'rm -f /var/lib/crm/debug-export-trace.log' || true
      ;;
    app-crash)
      docker start "$(cid app1)" >/dev/null
      ;;
    dns-stale)
      write_zone 172.30.0.40
      ;;
    cert-expired)
      docker exec "$(cid edge)" sh -c 'cp /certs/valid.crt /certs/live/crm.crt && nginx -s reload' >/dev/null 2>&1 || true
      ;;
    db-connections)
      docker exec "$(cid db)" sh -c "pkill -f 'pg_sleep' ; psql -U crm -d crm -q -c \"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE query LIKE '%sales-export%' AND pid <> pg_backend_pid()\" >/dev/null" || true
      ;;
    *) echo "unknown fault: $1" >&2; exit 2 ;;
  esac
  rm -f "$STATE/$1"
  echo "restored $1"
}

case "${1:-}" in
  list) printf '%s\n' "${FAULTS[@]}" ;;
  status) ls "$STATE" 2>/dev/null || true ;;
  restore-all)
    for f in "${FAULTS[@]}"; do restore "$f" >/dev/null 2>&1 || true; done
    : > "$RUNTIME/changes.log"
    echo "all faults restored"
    ;;
  *)
    [[ $# -eq 2 ]] || { echo "usage: $0 <fault> inject|restore | list | status | restore-all" >&2; exit 2; }
    case "$2" in inject) inject "$1" ;; restore) restore "$1" ;; *) exit 2 ;; esac
    ;;
esac
