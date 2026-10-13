#!/usr/bin/env bash
# CI (.github/workflows/station-install.yml), as root, after install-linux.sh --ci and a running
# station: the auto-update end to end against a local git repo standing in for GitHub.
#   A  this tree on main: the station switches from its folder copy to git (install-linux.sh --repo)
#   B  a Python-only change: the hub "runs" it -> built beside A, switched, /healthz says B; A kept
#   C  starts but never answers: switched to, switched back to B, C recorded as failed, not retried
#   D  fails its preflight (an import): never switched to, B keeps running
#   then sizeup-station rollback by hand: back to A (never to the failed C)
set -euo pipefail

WORK=/srv/sizeup-ci
SRC=$WORK/src
BARE=$WORK/sizeup.git
WANT=/run/sizeup/update-wanted
STATE=/var/lib/sizeup-update/state.json
here=$(pwd)

say() { printf '\n== %s\n' "$*"; }
gitc() { git -C "$SRC" -c user.name=ci -c user.email=ci@example.invalid "$@"; }
build() { curl -fsS --max-time 3 http://127.0.0.1:8091/healthz | python3 -c 'import json, sys; print(json.load(sys.stdin)["build"])'; }
current() { cat /opt/sizeup/current/BUILD; }
field() { python3 -c 'import json, sys; d = json.load(open(sys.argv[1])); print(eval(sys.argv[2], {"d": d}))' "$STATE" "$1"; }
wait_for() {   # wait_for SECONDS CMD...: until CMD succeeds
  local limit=$1
  shift
  for _ in $(seq 1 "$limit"); do
    if "$@" >/dev/null 2>&1; then return 0; fi
    sleep 1
  done
  echo "timed out after $limit s: $*" >&2
  return 1
}
is_build() { [ "$(build)" = "$1" ]; }
state_is() { [ "$(field 'd.get("state")')" = "$1" ] && [ "$(field 'd.get("hub")')" = "$2" ]; }
push() { gitc commit -q -am "$1"; gitc push -q --force "$BARE" main; gitc rev-parse HEAD; }
hub_runs() { echo "$1" > "$WANT"; }      # what the station writes when the hub reports that build

say "A: a local repo, and the station on git"
rm -rf "$WORK"
mkdir -p "$SRC"
rsync -a --exclude .git "$here/" "$SRC/"
git -C "$SRC" init -q -b main
gitc add -A
gitc commit -q -m A
git clone -q --bare "$SRC" "$BARE"
A=$(gitc rev-parse HEAD)
"$here/deploy/station/install-linux.sh" --ci --repo "$BARE" --ref main | tail -n 15
test "$(current)" = "$A"
systemctl daemon-reload
systemctl restart sizeup-station.service
wait_for 120 is_build "$A"
systemctl enable --now sizeup-update.path sizeup-update.timer
sizeup-station status || true

say "B: a Python-only change, side by side (and the sizeup user plants links where root writes)"
runuser -u sizeup -- ln -sfn /usr/local/lib/sizeup/autoupdate.py /var/lib/sizeup/trunk-recorder/config.json
runuser -u sizeup -- ln -sfn /usr/local/lib/sizeup/start-station /var/lib/sizeup/trunk-recorder/unit-event.sh
echo "# ci: release B" >> "$SRC/sizeup/__init__.py"
B=$(push B)
t0=$(date +%s)
hub_runs "$B"
wait_for 600 state_is current "$B"
echo "B took $(( $(date +%s) - t0 )) s from the hub's build to running it"
test "$(current)" = "$B"
is_build "$B"
test -d "/opt/sizeup/releases/$A" && test -d "/opt/sizeup/releases/$B"
np=$(find "/opt/sizeup/releases/$B/venv" -name '__init__.py' -path '*numpy/__init__.py' | head -n 1)
test "$(stat -c %i "$np")" = "$(stat -c %i "/opt/sizeup/releases/$A/${np#/opt/sizeup/releases/"$B"/}")"   # venv: hard links
tail -n 1 /var/log/sizeup-update.log | grep -q "ok in"
tail -n 1 /var/log/sizeup-update.log
# root wrote neither through the planted links: they were replaced, the targets are untouched and root's
test ! -L /var/lib/sizeup/trunk-recorder/config.json && test ! -L /var/lib/sizeup/trunk-recorder/unit-event.sh
cmp "/opt/sizeup/releases/$B/sizeup/cloud/autoupdate.py" /usr/local/lib/sizeup/autoupdate.py
cmp "/opt/sizeup/releases/$B/deploy/station/start-station" /usr/local/lib/sizeup/start-station
test "$(stat -c %U /usr/local/lib/sizeup/autoupdate.py /usr/local/lib/sizeup/start-station | sort -u)" = root

say "a docs-only change: no restart"
pid=$(systemctl show -p MainPID --value sizeup-station.service)
echo "more words" >> "$SRC/docs/cloud.md"
DOCS=$(push docs)
hub_runs "$DOCS"
wait_for 300 state_is current "$DOCS"
test "$(current)" = "$B"
test "$(systemctl show -p MainPID --value sizeup-station.service)" = "$pid"
field 'd["equivalent"]["'"$DOCS"'"]'
tail -n 1 /var/log/sizeup-update.log | grep -q "no restart"

say "C: starts, never answers: switched back"
python3 - "$SRC/sizeup/app.py" <<'PY'
import sys
p = sys.argv[1]
s = open(p).read()
old = "    server = make_server(app)\n"
assert s.count(old) == 1
open(p, "w").write(s.replace(old, "    raise SystemExit('ci: release C never answers')\n" + old))
PY
C=$(push C)
hub_runs "$C"
wait_for 600 state_is failed "$C"
test "$(current)" = "$B"
wait_for 60 is_build "$B"
test "$(field 'd["last"]["result"]')" = "rolled back"
field 'd["failed"]["'"$C"'"]["why"]'
tail -n 1 /var/log/sizeup-update.log | grep -q FAILED
systemctl start sizeup-update.service                   # again: the same sha is left alone
test "$(current)" = "$B"

say "D: fails its preflight: never switched to"
gitc reset -q --hard "$B"
echo "import does_not_exist_ci  # noqa" >> "$SRC/sizeup/cloud/station.py"
D=$(push D)                                             # (main moved on from C: it isn't the hub's any more)
hub_runs "$D"
wait_for 600 state_is failed "$D"
test "$(current)" = "$B"
is_build "$B"
test "$(field 'd["last"]["result"]')" = "preflight failed"
field 'd["last"]["why"]'

say "rollback by hand: back to A, never to the failed C"
sizeup-station rollback
test "$(current)" = "$A"
wait_for 60 is_build "$A"
sizeup-station autoupdate status
say "ok"
