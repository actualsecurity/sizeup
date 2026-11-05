#!/bin/sh
# The Cloudflare connector of a hub task (GitHub #247, docs/cloud.md "Deploys: the hot/cold handoff"): the
# "cloudflared" container of deploy/aws/taskdef.json runs this, from the hub image.
#
# Only the leader's task may be reached. The hub container's supervisor (sizeup/handoff.py) writes
# $SIZEUP_SHARED_DIR/leader (a task volume both containers mount) once this task holds the lock and its app
# answers; it removes it when the overlap after a handoff is over, or at once when the lease is lost.
#   * no `leader` file: cloudflared is not started (a standby, or a task still starting)
#   * `leader` appears: cloudflared starts
#   * `leader` goes, or the hub's heartbeat ($SIZEUP_SHARED_DIR/hub-alive, unix seconds, rewritten every second)
#     is older than CONNECTOR_STALE_S: cloudflared is stopped and this exits
#   * SIGTERM (ECS stopping the task) is NOT obeyed while the leader file is there: the old task keeps its
#     connector up through the handoff, until its supervisor says. ECS's stopTimeout (120 s) bounds it.
#   * cloudflared exits by itself: this exits with its code (the container is essential: the task stops,
#     ECS replaces it, the hub hands over)
# Environment: TUNNEL_TOKEN (cloudflared reads it), SIZEUP_SHARED_DIR (default /run/sizeup-shared),
# CLOUDFLARED (default cloudflared), CONNECTOR_STALE_S (10), CONNECTOR_TICK (0.5), SIZEUP_PORT (8090).
set -u
SHARED="${SIZEUP_SHARED_DIR:-/run/sizeup-shared}"
CLOUDFLARED="${CLOUDFLARED:-cloudflared}"
STALE_S="${CONNECTOR_STALE_S:-10}"
TICK="${CONNECTOR_TICK:-0.5}"
PORT="${SIZEUP_PORT:-8090}"
term=0
trap 'term=1' TERM INT
say() { echo "connector: $*"; }

hub_alive() {
  beat=$(cat "$SHARED/hub-alive" 2>/dev/null) || return 1
  case "$beat" in ''|*[!0-9]*) return 1 ;; esac
  [ $(( $(date +%s) - beat )) -le "$STALE_S" ]
}

say "waiting to be the leader's task ($SHARED/leader)"
while [ ! -f "$SHARED/leader" ]; do
  if [ "$term" = 1 ]; then
    say "stopped before this task led: cloudflared never started"
    exit 0
  fi
  sleep "$TICK"
done

say "this task is the leader (generation $(cat "$SHARED/leader" 2>/dev/null)): starting cloudflared"
"$CLOUDFLARED" tunnel --no-autoupdate --metrics 127.0.0.1:20241 --grace-period 3s run --url "http://localhost:$PORT" &
pid=$!
why=""
while :; do
  if ! kill -0 "$pid" 2>/dev/null; then
    wait "$pid"
    code=$?
    say "cloudflared exited ($code)"
    exit "$code"
  fi
  if [ ! -f "$SHARED/leader" ]; then why="the hub says the handoff is over"; break; fi
  if ! hub_alive; then why="no heartbeat from the hub for over ${STALE_S} s"; break; fi
  sleep "$TICK"
done
say "$why: stopping cloudflared"
kill -TERM "$pid" 2>/dev/null
wait "$pid"
say "cloudflared stopped"
exit 0
