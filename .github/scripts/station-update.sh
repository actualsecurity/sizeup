#!/usr/bin/env bash
# CI (.github/workflows/station-install.yml), as root, after install-linux.sh --ci and a running
# station: the auto-update end to end against a local git repo standing in for GitHub.
#   A  this tree on main: the station switches from its folder copy to git (install-linux.sh --repo)
#   B  a Python-only change: the hub "runs" it -> built beside A, switched, /healthz says B; A kept
#   C  starts but never answers: switched to, switched back to B, C recorded as failed, not retried;
#      trunk-recorder's own service (#71) is left running through both switches (Python only: same PID)
#   D  fails its preflight (an import): never switched to, B keeps running
#   then sizeup-station rollback by hand: back to A (never to the failed C)
#   E  an incident-source adapter (#301) from a local repo standing in for the private one: set up, installed into a
#      copy of A's release, a new adapter commit alone rebuilds, a broken one is never switched to, a rollback brings
#      the previous adapter back, removed: A without it
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
tr_pid() { systemctl show -p MainPID --value sizeup-trunk-recorder.service; }
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
systemctl is-active --quiet sizeup-trunk-recorder.service                # (started by B's switch: CI's install starts nothing)
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

say "C: starts, never answers: switched back (trunk-recorder's service left running throughout)"
trpid=$(tr_pid)
test "$trpid" -gt 0
python3 - "$SRC/sizeup/app.py" <<'PY'
import sys
p = sys.argv[1]
s = open(p).read()
import re
m = re.findall(r"^    server = make_server\(app[^\n]*\)\n", s, re.M)
assert len(m) == 1, m
old = m[0]
open(p, "w").write(s.replace(old, "    raise SystemExit('ci: release C never answers')\n" + old))
PY
C=$(push C)
hub_runs "$C"
wait_for 600 state_is failed "$C"
test "$(current)" = "$B"
wait_for 60 is_build "$B"
test "$(field 'd["last"]["result"]')" = "rolled back"
test "$(tr_pid)" = "$trpid"                              # a Python-only switch and switch back: never restarted
grep -q "trunk-recorder: unchanged, left running" /var/log/sizeup-install.log
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

say "E: an incident-source adapter (#301): installed side by side, a new commit, a broken one, rollback, removed"
AD=$WORK/adapter
ADB=$WORK/adapter.git
release() { basename "$(readlink -f /opt/sizeup/current)"; }
release_is() { [ "$(release)" = "$1" ]; }
idle() { case "$(systemctl show -p ActiveState --value sizeup-update.service)" in inactive|failed) ;; *) return 1 ;; esac; }   # (a oneshot running is "activating")
in_venv() { find "$1/venv" -path '*site-packages/ci_feed' -print -quit; }
adapter_commit() { python3 -c 'import json; print(json.load(open("/opt/sizeup/current/ADAPTER.json"))["commit"])'; }
gita() { git -C "$AD" -c user.name=ci -c user.email=ci@example.invalid "$@"; }
pusha() { gita commit -q -am "$1"; gita push -q --force "$ADB" main; gita rev-parse HEAD; }
mkdir -p "$AD/ci_feed"
cat > "$AD/pyproject.toml" <<'TOML'
[build-system]
requires = ["setuptools>=61"]
build-backend = "setuptools.build_meta"

[project]
name = "ci-feed"
version = "0.1"

[project.entry-points."sizeup.incident_sources"]
feed = "ci_feed:make"

[tool.setuptools]
packages = ["ci_feed"]
TOML
printf 'def make(ctx):\n    return []\n\n\ndef self_check():\n    return ""\n' > "$AD/ci_feed/__init__.py"
gita init -q -b main
gita add -A
gita commit -q -m one
git clone -q --bare "$AD" "$ADB"
A1=$(gita rev-parse HEAD)
wait_for 600 idle
printf 'ci-user\n' | sizeup-station adapter setup --repo "$ADB" --ref main --env CI_FEED_USER --no-check
test "$(stat -c '%U %a' /etc/sizeup/adapter-deploy-key)" = "root 600"
test "$(stat -c '%U %a' /etc/sizeup/credentials/CI_FEED_USER)" = "root 600"     # LoadCredential=, never the environment
test "$(cat /etc/sizeup/credentials/CI_FEED_USER)" = ci-user
grep -q '^LoadCredential=CI_FEED_USER:/etc/sizeup/credentials/CI_FEED_USER$' \
  /etc/systemd/system/sizeup-station.service.d/adapter-credentials.conf
test -z "$(grep '^CI_FEED_USER=' /etc/sizeup/station.env 2>/dev/null || true)"
python3 -c 'import tomllib; a = tomllib.load(open("/etc/sizeup/config.toml", "rb"))["adapter"]; assert a["ref"] == "main", a'
wait_for 600 release_is "$A-a${A1:0:12}"
wait_for 300 idle                                          # (its /healthz check and prune, then done)
wait_for 120 is_build "$A"
test "$(adapter_commit)" = "$A1"
# the credential reaches the service as a file under $CREDENTIALS_DIRECTORY (systemd 255), not in its environment
spid=$(systemctl show -p MainPID --value sizeup-station.service)
test "$(cat /run/credentials/sizeup-station.service/CI_FEED_USER)" = ci-user
test -z "$(tr '\0' '\n' < "/proc/$spid/environ" | grep '^CI_FEED_USER=' || true)"
test -n "$(ls /opt/sizeup/adapter-wheels/)"                                   # its wheels, built once, root's
test "$(stat -c %U "$(ls -d /opt/sizeup/adapter-wheels/"$A1"-*)")" = root
/opt/sizeup/current/venv/bin/python -c 'import importlib.metadata as m; assert [e.name for e in m.entry_points(group="sizeup.incident_sources")] == ["feed"]'
test -n "$(in_venv /opt/sizeup/current)" && test -z "$(in_venv "/opt/sizeup/releases/$A")"   # only in the copy
sizeup-station status > "$WORK/status.txt"
grep "^adapter:" "$WORK/status.txt"
grep -q "^adapter:   adapter ${A1:0:7} installed" "$WORK/status.txt"
sizeup-station adapter status
wait_for 600 idle

echo "# two" >> "$AD/ci_feed/__init__.py"
A2=$(pusha two)
systemctl start sizeup-update.service
release_is "$A-a${A2:0:12}"
wait_for 120 is_build "$A"
test "$(adapter_commit)" = "$A2"

printf 'def make(ctx):\n    return []\n\n\ndef self_check():\n    return "ci: broken"\n' > "$AD/ci_feed/__init__.py"
A3=$(pusha three)
systemctl start sizeup-update.service || true            # (the unit fails: that adapter commit failed its preflight)
wait_for 300 idle
release_is "$A-a${A2:0:12}"                               # never switched to
test ! -e "/opt/sizeup/releases/$A-a${A3:0:12}"
[[ "$(field 'd["adapter"]["failed"]["'"$A3"'"]["why"]')" == *"ci: broken"* ]]
test "$(field 'd["last"]["result"]')" = "adapter not built"
doc=$(sizeup-station doctor 2>&1 || true)                 # (no dongles, no token here: it fails anyway)
grep -q "FAIL  adapter adapter ${A2:0:7} installed · ${A3:0:7} failed" <<<"$doc"

sizeup-station rollback                                   # the previous adapter comes back with its release
release_is "$A-a${A1:0:12}"
wait_for 60 is_build "$A"
test "$(adapter_commit)" = "$A1"

sizeup-station adapter remove --yes
test ! -e /etc/sizeup/adapter-deploy-key && test ! -e /etc/sizeup/credentials
test ! -e /etc/systemd/system/sizeup-station.service.d/adapter-credentials.conf
wait_for 600 release_is "$A"
wait_for 300 idle
wait_for 60 is_build "$A"
test ! -e /opt/sizeup/current/ADAPTER.json && test -z "$(in_venv /opt/sizeup/current)"
say "ok"
