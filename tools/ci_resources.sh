#!/usr/bin/env bash
# The CI runner's resources, for the test shards (.github/workflows/test.yml, #984).
#
#   tools/ci_resources.sh snapshot LABEL     CPUs, load, memory, disks, the biggest and busiest processes, OOM kills
#   tools/ci_resources.sh watch PID LOG      until PID exits: a sample every 10 s to LOG, one a minute to stdout;
#                                            near the limits a snapshot, at them the tests' stacks and then the tests
#                                            killed, so the step ends and its log is kept
#
# Why: a runner that runs out of memory or disk does not fail the job, it dies. GitHub then shows "The runner has
# received a shutdown signal" or only "exceeded the maximum execution time", and keeps no log at all (not even the
# steps that finished). Ending the tests first leaves the log, the samples and the snapshot.
set -u

MB() { awk -v k="$1" '$1 == k":" {print int($2 / 1024)}' /proc/meminfo; }
DISK_MB() { df -Pm "$1" 2>/dev/null | awk 'NR == 2 {print $4}'; }

snapshot() {
  echo "== resources: $1 ($(date -u +%H:%M:%S) UTC)"
  echo "nproc $(nproc) · load $(cut -d' ' -f1-3 /proc/loadavg) · processes $(ps -e --no-headers | wc -l) · threads $(ps -eL --no-headers | wc -l)"
  free -m
  df -h / /tmp "${RUNNER_TEMP:-/tmp}" 2>/dev/null | awk '!seen[$0]++'
  echo "-- most memory (RSS MB)"
  ps -eo pid,ppid,rss,pcpu,etime,args --sort=-rss --no-headers | head -12 \
    | awk '{printf "%7s %7s %6d %5s %9s ", $1, $2, $3 / 1024, $4, $5; $1=$2=$3=$4=$5=""; print substr($0, 6, 160)}'
  echo "-- most CPU"
  ps -eo pid,ppid,rss,pcpu,etime,args --sort=-pcpu --no-headers | head -8 \
    | awk '{printf "%7s %7s %6d %5s %9s ", $1, $2, $3 / 1024, $4, $5; $1=$2=$3=$4=$5=""; print substr($0, 6, 160)}'
  if [ -r /sys/fs/cgroup/ci-tests/memory.events ]; then
    echo "-- the tests' cgroup: $(( $(cat /sys/fs/cgroup/ci-tests/memory.peak 2>/dev/null || echo 0) / 1048576 )) MB peak;" \
      "$(tr '\n' ' ' < /sys/fs/cgroup/ci-tests/memory.events)"
  fi
  sudo -n dmesg -T 2>/dev/null | grep -iE "out of memory|oom-kill|killed process|no space left" | tail -10 || true
}

tree() {   # PID and every process under it
  local all="$1" new="$1" next
  while [ -n "$new" ]; do
    next=$(ps -eo pid=,ppid= | awk -v p=" $new " 'index(p, " " $2 " ") {printf "%s ", $1}')
    all="$all $next"; new=$(echo $next)
  done
  echo $all
}

watch() {
  local pid="$1" log="$2" start now n=0 avail swap load disk tmpd procs threads rss warned=0
  start=$(date +%s)
  echo "elapsed avail_MB swap_used_MB load1 disk_free_MB tmp_free_MB procs tests_RSS_MB threads" > "$log"
  while kill -0 "$pid" 2>/dev/null; do
    now=$(( $(date +%s) - start ))
    avail=$(MB MemAvailable); swap=$(( $(MB SwapTotal) - $(MB SwapFree) ))
    load=$(cut -d' ' -f1 /proc/loadavg); disk=$(DISK_MB /); tmpd=$(DISK_MB /tmp)
    procs=$(ps -e --no-headers | wc -l); threads=$(ps -eL --no-headers | wc -l)
    rss=$(ps -o rss= -p "$(tree "$pid" | tr ' ' ',')" 2>/dev/null | awk '{s += $1} END {print int(s / 1024)}')
    line="$(printf '%4d:%02d' $((now / 60)) $((now % 60))) $avail $swap $load $disk $tmpd $procs $rss $threads"
    echo "$line" >> "$log"
    [ $((n % 6)) -eq 0 ] && echo "resources $line   (elapsed avail_MB swap_MB load1 disk_MB tmp_MB procs tests_MB threads)"
    n=$((n + 1))
    if [ "${avail:-99999}" -lt 700 ] || [ "${disk:-99999}" -lt 400 ] || [ "$swap" -gt 2200 ]; then
      echo "::error::the runner is at its limits (available ${avail} MB, swap ${swap} MB, disk ${disk} MB): the tests' stacks, then the tests are stopped before the runner dies (#984)"
      snapshot "at the limit"
      kill -USR1 $(tree "$pid") 2>/dev/null; sleep 5
      kill -KILL $(tree "$pid") 2>/dev/null
      return 0
    fi
    if [ "$warned" -eq 0 ] && { [ "${avail:-99999}" -lt 2500 ] || [ "${disk:-99999}" -lt 2000 ] || [ "$swap" -gt 1000 ]; }; then
      echo "::warning::the runner is near its limits (available ${avail} MB, swap ${swap} MB, disk ${disk} MB) (#984)"
      snapshot "near the limit"; warned=1
    fi
    sleep 10
  done
}

case "${1:-}" in
  snapshot) snapshot "${2:-now}" ;;
  watch) watch "$2" "$3" ;;
  *) sed -n '2,10p' "$0" >&2; exit 2 ;;
esac
