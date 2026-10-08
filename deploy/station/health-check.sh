#!/usr/bin/env bash
# sizeup station health check (sizeup-health.timer, every minute; installed as
# /usr/local/lib/sizeup/health-check). GET http://127.0.0.1:<port>/healthz; after 3 failures in a row
# restart sizeup-station. A crash is systemd's job (Restart=always): this catches a process that is
# running but no longer answering. A service someone stopped on purpose is left alone.
set -euo pipefail

UNIT=sizeup-station.service
CONF=/etc/sizeup/config.toml
COUNT=/run/sizeup-health/failures
LIMIT=3
GRACE_S=120          # just (re)started: give it time to come up

systemctl is-enabled --quiet "$UNIT" 2>/dev/null || exit 0
systemctl is-active --quiet "$UNIT" 2>/dev/null || exit 0

since=$(systemctl show -p ActiveEnterTimestampMonotonic --value "$UNIT" 2>/dev/null || echo 0)
now=$(awk '{printf "%d", $1 * 1000000}' /proc/uptime)
if [ "${since:-0}" -gt 0 ] && [ $(( (now - since) / 1000000 )) -lt "$GRACE_S" ]; then
  exit 0
fi

port=$(/opt/sizeup/venv/bin/python -c 'import sys, tomllib; print(int(tomllib.load(open(sys.argv[1], "rb")).get("web", {}).get("port", 8090)))' "$CONF" 2>/dev/null || echo 8091)

mkdir -p "$(dirname "$COUNT")"
if curl -fsS --max-time 10 -o /dev/null "http://127.0.0.1:$port/healthz"; then
  rm -f "$COUNT"
  exit 0
fi
n=$(( $(cat "$COUNT" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$COUNT"
echo "sizeup-station: /healthz on port $port failed ($n of $LIMIT)"
if [ "$n" -ge "$LIMIT" ]; then
  rm -f "$COUNT"
  echo "sizeup-station: not answering for $LIMIT checks: restarting it"
  systemctl restart "$UNIT"
fi
