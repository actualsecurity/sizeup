#!/usr/bin/env bash
# sizeup STATION installer for Linux: Ubuntu 24.04 (WSL2 on the Windows box) or Debian / Raspberry Pi
# OS (arm64). apt-based. Run as root; safe to run again at any time (= repair). deploy/station/README.md.
#
#   install-linux.sh [options]
#     --ref REF            branch or tag of the sizeup repo to run (default: main; kept for updates)
#     --repo URL           the repo (default git@github.com:actualsecurity/sizeup.git, read with a
#                          deploy key the installer makes and asks you to add on GitHub)
#     --from-dir DIR       use this sizeup checkout / copy instead of cloning (no deploy key; CI)
#     --model NAME         whisper model: small.en-q5_1 (default) | large-v3-turbo-q5_0 | base.en
#     --update             fetch the ref, build its release beside the running one, switch, restart
#     --secrets            only (re)enter the hub token and the Cloudflare Access service token
#     --rebuild            rebuild rtl-sdr, trunk-recorder and whisper.cpp even if up to date
#     --ci                 CI: no systemd enable/start, no hardware, no prompts, no deploy key
#     --wsl-prepare        (install-windows.ps1) only write /etc/wsl.conf (systemd on), then exit
#     --stage SHA          (the auto-updater) build release SHA beside the running one and preflight
#                          it; nothing the running station uses is touched
#     --activate NAME      (the auto-updater) switch to the staged release NAME: system files, the
#                          build links, /opt/sizeup/current, one restart
#
# Side-by-side releases (README "Updating"): each commit gets its own /opt/sizeup/releases/<sha> (the
# code from git and its own venv, root-owned); /opt/sizeup/current points at the one that runs and is
# swapped with one rename. rtl-sdr-blog, trunk-recorder and whisper.cpp are built into
# /opt/sizeup/builds/<name>-<tag>-<commit>, once per pinned version, behind the links
# /opt/sizeup/{rtl-sdr,trunk-recorder,whisper} the config uses.
#
# Everything it says goes to /var/log/sizeup-install.log too. Secrets are read with hidden input,
# written only to /etc/sizeup/station.env (root:sizeup 0640) and never printed or logged.
set -euo pipefail
set +x
umask 022

# -- pinned versions -------------------------------------------------------------------------------
# rtl-sdr-blog: the RTL-SDR Blog fork of librtlsdr / rtl_fm (V4 support on any distro)
RTLSDR_REPO="https://github.com/rtlsdrblog/rtl-sdr-blog.git"
RTLSDR_TAG="v1.3.6"
RTLSDR_COMMIT="240bd0e1e6d9f64361b6949047468958cd08aa31"
# trunk-recorder (P25: Fire/EMS 15), with its built-in plugins (unit_script: who keyed up, live)
TR_REPO="https://github.com/TrunkRecorder/trunk-recorder.git"
TR_TAG="v5.2.0"
TR_COMMIT="7f9e21ecfa445e568371cef3153a07887c36b380"
# whisper.cpp: whisper-server, the same release the Mac station runs
WHISPER_REPO="https://github.com/ggml-org/whisper.cpp.git"
WHISPER_TAG="v1.9.4"
WHISPER_COMMIT="927cfce34f31707e17f2bff35c349632fb9e2c3a"
# whisper models: huggingface.co/ggerganov/whisper.cpp at a fixed revision; SHA-256 = the LFS oid
# in that revision's file listing (api/models/ggerganov/whisper.cpp/tree/<rev>).
MODEL_REV="5359861c739e955e79d9a303bcbc70fb988958b1"
model_file() {
  case "$1" in
    small.en-q5_1)       echo "ggml-small.en-q5_1.bin bfdff4894dcb76bbf647d56263ea2a96645423f1669176f4844a1bf8e478ad30" ;;
    large-v3-turbo-q5_0) echo "ggml-large-v3-turbo-q5_0.bin 394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2" ;;
    base.en)             echo "ggml-base.en.bin a03779c86df3323075f5e796cb2ce5029f00ec8869eee3fdfb897afe36c6d002" ;;
    *) return 1 ;;
  esac
}
# GitHub's published SSH host key (docs.github.com "GitHub's SSH key fingerprints"): no trust on first use.
GITHUB_HOSTKEY="github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"

# -- layout ----------------------------------------------------------------------------------------
OPT=/opt/sizeup
REPO=$OPT/repo.git                 # bare clone (root's): every release is cut from it
RELEASES=$OPT/releases             # <sha>/: the code + its venv, root-owned (the sizeup user only reads)
CURRENT=$OPT/current               # -> releases/<sha>: what runs
BUILDS=$OPT/builds                 # <name>-<tag>-<commit>/: one per pinned version
SRC=$OPT/src
MODELS=$OPT/models
LEGACY_APP=$OPT/app                # before #58: a git checkout run in place (and its venv)
LEGACY_VENV=$OPT/venv
RTLSDR_B="$BUILDS/rtl-sdr-blog-$RTLSDR_TAG-${RTLSDR_COMMIT:0:12}"
TR_B="$BUILDS/trunk-recorder-$TR_TAG-${TR_COMMIT:0:12}"
WHISPER_B="$BUILDS/whisper.cpp-$WHISPER_TAG-${WHISPER_COMMIT:0:12}-native"
STATE=/var/lib/sizeup
TR_DIR=$STATE/trunk-recorder
UPDATE_STATE=/var/lib/sizeup-update
ETC=/etc/sizeup
CONF=$ETC/config.toml
ENVFILE=$ETC/station.env
INSTALL_CONF=$ETC/install.conf
DEPLOY_KEY=$ETC/deploy_key
KNOWN_HOSTS=$ETC/known_hosts
LIB=/usr/local/lib/sizeup
LOG=/var/log/sizeup-install.log
SVC_USER=sizeup
SELF_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

# -- options ---------------------------------------------------------------------------------------
CI_MODE=0 UPDATE=0 SECRETS_ONLY=0 REBUILD=0 NO_FETCH=0 WSL_PREPARE=0
OPT_REF="" OPT_REPO="" OPT_MODEL="" OPT_FROM_DIR="" STAGE_SHA="" ACTIVATE_NAME=""
usage() { awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; }
while [ $# -gt 0 ]; do
  case "$1" in
    --ci) CI_MODE=1 ;;
    --update) UPDATE=1 ;;
    --secrets) SECRETS_ONLY=1 ;;
    --rebuild) REBUILD=1 ;;
    --no-fetch) NO_FETCH=1 ;;           # (an installer from before #58 runs the new one with this)
    --wsl-prepare) WSL_PREPARE=1 ;;
    --ref) OPT_REF="${2:?--ref needs a value}"; shift ;;
    --repo) OPT_REPO="${2:?--repo needs a value}"; shift ;;
    --model) OPT_MODEL="${2:?--model needs a value}"; shift ;;
    --from-dir) OPT_FROM_DIR="${2:?--from-dir needs a value}"; shift ;;
    --stage) STAGE_SHA="${2:?--stage needs a commit}"; shift ;;
    --activate) ACTIVATE_NAME="${2:?--activate needs a release}"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "install-linux.sh: unknown option $1 (--help)" >&2; exit 2 ;;
  esac
  shift
done
# --rebuild: into new directories (the ones in use stay until the switch; pruned later)
if [ "$REBUILD" -eq 1 ]; then
  rebuilt="rebuilt-$(date +%Y%m%d%H%M%S)"
  RTLSDR_B="$RTLSDR_B-$rebuilt" TR_B="$TR_B-$rebuilt" WHISPER_B="$WHISPER_B-$rebuilt"
fi
for v in "$STAGE_SHA" "$ACTIVATE_NAME"; do
  if [ -n "$v" ] && ! [[ "$v" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
    echo "install-linux.sh: bad release name $v" >&2
    exit 2
  fi
done

if [ "$(id -u)" -ne 0 ]; then
  echo "install-linux.sh: run it as root (sudo $0 ...)" >&2
  exit 1
fi

mkdir -p "$(dirname "$LOG")"
touch "$LOG" && chmod 0640 "$LOG"
if [ -z "${SIZEUP_INSTALL_TEE:-}" ]; then     # (once: an installer started by another one shares it)
  export SIZEUP_INSTALL_TEE=1
  exec > >(tee -a "$LOG") 2>&1
fi

STEP=""
say() { printf '%s  %s\n' "$(date '+%F %T')" "$*"; }
step() { STEP="$*"; say "== $*"; }
warn() { say "WARNING: $*"; }
die() { say "FAILED: $*" >&2; exit 1; }
PAUSED_UPDATER=0
on_exit() {
  local rc=$?
  if [ "$rc" -ne 0 ]; then
    say "install-linux.sh stopped (exit $rc) during: ${STEP:-start}. Fix it and run it again: it picks up where it left off. Log: $LOG"
  fi
  if [ "$PAUSED_UPDATER" -eq 1 ]; then
    exec 9>&-                                  # (the lock: the updater may run now)
    systemctl start sizeup-update.path sizeup-update.timer 2>/dev/null || true
    systemctl start --no-block sizeup-update.service 2>/dev/null || true    # a hub build heard meanwhile
  fi
}
trap on_exit EXIT

# One install at a time: the auto-updater (sizeup/cloud/autoupdate.py) holds this lock for a whole
# update and runs --stage / --activate under it (SIZEUP_UPDATE_LOCKED=1); a run by hand takes it too
# and keeps the updater's path unit and timer stopped until it's done.
if [ -z "${SIZEUP_UPDATE_LOCKED:-}" ] && [ "$WSL_PREPARE" -eq 0 ]; then
  mkdir -p /run/sizeup-update
  exec 9>/run/sizeup-update/lock
  if ! flock -n 9; then
    echo "install-linux.sh: an automatic update is running (journalctl -u sizeup-update -f): run this again when it's done" >&2
    exit 1
  fi
  export SIZEUP_UPDATE_LOCKED=1
  if [ -d /run/systemd/system ] && systemctl is-active --quiet sizeup-update.path 2>/dev/null; then
    systemctl stop sizeup-update.path sizeup-update.timer
    PAUSED_UPDATER=1
  fi
fi

is_wsl() { grep -qiE 'microsoft|wsl' /proc/sys/kernel/osrelease 2>/dev/null; }
interactive() { [ "$CI_MODE" -eq 0 ] && [ -t 0 ]; }
as_user() { runuser -u "$SVC_USER" -- env HOME="$STATE" "$@"; }
short() { printf '%s' "${1:0:7}"; }

# -- WSL: systemd on (install-windows.ps1 runs this first, then restarts the distro) ---------------
wsl_conf() {
  is_wsl || return 0
  local f=/etc/wsl.conf changed=0
  touch "$f"
  if ! grep -qE '^[[:space:]]*systemd[[:space:]]*=[[:space:]]*true' "$f"; then
    python3 - "$f" <<'PY'
import configparser, sys
p = sys.argv[1]
c = configparser.ConfigParser()
c.optionxform = str
c.read(p)
if not c.has_section("boot"):
    c.add_section("boot")
c.set("boot", "systemd", "true")
if not c.has_section("user"):
    c.add_section("user")
if not c.has_option("user", "default"):
    c.set("user", "default", "root")
with open(p, "w") as f:
    c.write(f)
PY
    changed=1
  fi
  say "/etc/wsl.conf: systemd on$([ $changed -eq 1 ] && echo ' (changed: the distro must restart: wsl --terminate <distro>)')"
}

if [ "$WSL_PREPARE" -eq 1 ]; then
  step "WSL: /etc/wsl.conf"
  wsl_conf
  exit 0
fi

# -- saved settings (install.conf) -----------------------------------------------------------------
SIZEUP_REPO="git@github.com:actualsecurity/sizeup.git"
SIZEUP_REF="main"
SIZEUP_SOURCE="git"
SIZEUP_MODEL="small.en-q5_1"
SIZEUP_AUTO_UPDATE="on"
if [ -f "$INSTALL_CONF" ]; then
  # KEY=value lines this installer (or sizeup-station autoupdate) wrote; read them without sourcing
  while IFS='=' read -r k v; do
    case "$k" in
      SIZEUP_REPO|SIZEUP_REF|SIZEUP_SOURCE|SIZEUP_MODEL|SIZEUP_AUTO_UPDATE) printf -v "$k" '%s' "$v" ;;
    esac
  done < "$INSTALL_CONF"
fi
[ -n "$OPT_REPO" ] && SIZEUP_REPO="$OPT_REPO"
[ -n "$OPT_REF" ] && SIZEUP_REF="$OPT_REF"
[ -n "$OPT_MODEL" ] && SIZEUP_MODEL="$OPT_MODEL"
[ -n "$OPT_FROM_DIR" ] && SIZEUP_SOURCE="dir"
[ -z "$OPT_FROM_DIR" ] && [ -n "$OPT_REPO$OPT_REF" ] && SIZEUP_SOURCE="git"
case "$SIZEUP_AUTO_UPDATE" in on|off) ;; *) SIZEUP_AUTO_UPDATE=on ;; esac
model_file "$SIZEUP_MODEL" >/dev/null || die "--model: small.en-q5_1, large-v3-turbo-q5_0 or base.en (not $SIZEUP_MODEL)"
[[ "$SIZEUP_REF" =~ ^[A-Za-z0-9._/-]+$ ]] || die "--ref: a branch or tag name"
if [ "$SIZEUP_SOURCE" = dir ] && [ -z "$OPT_FROM_DIR" ] && [ "$SECRETS_ONLY" -eq 0 ] && [ -z "$STAGE_SHA$ACTIVATE_NAME" ]; then
  die "this station was installed from a folder: run install-linux.sh --from-dir <the new copy> (or --repo URL --ref REF to switch to git)"
fi
ssh_repo() { [[ "$SIZEUP_REPO" == git@* || "$SIZEUP_REPO" == ssh://* ]]; }
if [ "$CI_MODE" -eq 1 ] && [ "$SIZEUP_SOURCE" = git ] && ssh_repo; then
  die "--ci needs --from-dir or a local --repo (no deploy key in CI)"
fi

save_install_conf() {
  install -d -m 0750 -o root -g "$SVC_USER" "$ETC"
  local tmp
  tmp=$(mktemp "$ETC/.install.conf.XXXXXX")
  {
    echo "# written by install-linux.sh: what --update fetches, which model runs, auto-update on/off"
    echo "SIZEUP_REPO=$SIZEUP_REPO"
    echo "SIZEUP_REF=$SIZEUP_REF"
    echo "SIZEUP_SOURCE=$SIZEUP_SOURCE"
    echo "SIZEUP_MODEL=$SIZEUP_MODEL"
    echo "SIZEUP_AUTO_UPDATE=$SIZEUP_AUTO_UPDATE"
  } > "$tmp"
  chmod 0644 "$tmp"
  mv -f "$tmp" "$INSTALL_CONF"
}

# -- secrets ---------------------------------------------------------------------------------------
# The values live only in the secret_* variables and the env file. No echo / printf of them anywhere
# but the redirected writes into "$envtmp" (tests/test_station_install.py checks this).
env_has() { [ -f "$ENVFILE" ] && grep -qE "^$1=.+" "$ENVFILE"; }
env_get() { sed -n "s/^$1=//p" "$ENVFILE" 2>/dev/null | tail -n 1; }
valid_secret() { [[ "$1" =~ ^[A-Za-z0-9._~+/=-]+$ ]]; }

ask_secrets() {
  local force=$1 secret_token="" secret_id="" secret_key="" envtmp
  if [ "$force" -eq 0 ] && env_has SIZEUP_STATION_TOKEN; then
    say "secrets: $ENVFILE has the station token (sizeup-station secrets to change them)"
    return 0
  fi
  if ! interactive; then
    warn "no station token yet: the station records and queues everything but can't reach the hub."
    warn "add it with: sudo sizeup-station secrets"
    return 0
  fi
  echo
  echo "The hub's station token (SIZEUP_STATION_TOKEN on the hub: the same value). Typing is hidden."
  while :; do
    read -r -s -p "Station token: " secret_token; echo
    if [ -z "$secret_token" ] && env_has SIZEUP_STATION_TOKEN; then
      secret_token=$(env_get SIZEUP_STATION_TOKEN); echo "(kept the current one)"; break
    fi
    if [ "${#secret_token}" -ge 16 ] && valid_secret "$secret_token"; then break; fi
    echo "That isn't it: at least 16 characters, letters / digits / . _ - (openssl rand -hex 32). Again."
  done
  echo
  echo "Cloudflare Access service token for /api/station/* (Zero Trust > Access > Service auth)."
  echo "Enter = keep the current one, or none if the hub's station path uses a bypass policy."
  while :; do
    read -r -s -p "Access Client ID: " secret_id; echo
    [ -z "$secret_id" ] && secret_id=$(env_get SIZEUP_ACCESS_CLIENT_ID)
    if [ -z "$secret_id" ] || valid_secret "$secret_id"; then break; fi
    echo "Letters, digits and . _ - only. Again."
  done
  if [ -n "$secret_id" ]; then
    while :; do
      read -r -s -p "Access Client Secret: " secret_key; echo
      [ -z "$secret_key" ] && secret_key=$(env_get SIZEUP_ACCESS_CLIENT_SECRET)
      if [ -n "$secret_key" ] && valid_secret "$secret_key"; then break; fi
      echo "Needed with a Client ID (letters, digits and . _ - only). Again."
    done
  fi
  install -d -m 0750 -o root -g "$SVC_USER" "$ETC"
  envtmp=$(mktemp "$ETC/.station.env.XXXXXX")
  chown root:"$SVC_USER" "$envtmp"
  chmod 0640 "$envtmp"
  echo "# sizeup station secrets (install-linux.sh / sizeup-station secrets). root:sizeup 0640." >> "$envtmp"
  printf 'SIZEUP_STATION_TOKEN=%s\n' "$secret_token" >> "$envtmp"
  if [ -n "$secret_id" ]; then
    printf 'SIZEUP_ACCESS_CLIENT_ID=%s\n' "$secret_id" >> "$envtmp"
    printf 'SIZEUP_ACCESS_CLIENT_SECRET=%s\n' "$secret_key" >> "$envtmp"
  fi
  mv -f "$envtmp" "$ENVFILE"
  secret_token="" secret_id="" secret_key=""
  say "secrets: saved to $ENVFILE (token$(env_has SIZEUP_ACCESS_CLIENT_ID && echo ', Access service token'))"
}

if [ "$SECRETS_ONLY" -eq 1 ]; then
  STEP="secrets"
  id "$SVC_USER" >/dev/null 2>&1 || die "run the full install first"
  ask_secrets 1
  if systemctl is-active --quiet sizeup-station 2>/dev/null; then
    systemctl restart sizeup-station && say "sizeup-station restarted with the new secrets"
  fi
  exit 0
fi

# -- facts -----------------------------------------------------------------------------------------
# shellcheck disable=SC1091
. /etc/os-release
ARCH=$(dpkg --print-architecture)
MEM_KB=$(awk '/MemTotal/ {print $2}' /proc/meminfo)
JOBS=$(( MEM_KB / 1500000 )); [ "$JOBS" -lt 1 ] && JOBS=1
[ "$JOBS" -gt "$(nproc)" ] && JOBS=$(nproc)
command -v apt-get >/dev/null || die "apt-get not found: this installer is for Debian / Ubuntu / Raspberry Pi OS"
HAVE_SYSTEMD=0
[ -d /run/systemd/system ] && HAVE_SYSTEMD=1
if [ -z "$ACTIVATE_NAME" ]; then
  say "sizeup station install: ${PRETTY_NAME:-$ID} $ARCH, $(nproc) CPU(s), $(( MEM_KB / 1024 )) MB$(is_wsl && echo ', WSL2')$([ "$CI_MODE" -eq 1 ] && echo ', CI mode'); building with -j$JOBS"
fi
if [ "$HAVE_SYSTEMD" -eq 0 ] && [ "$CI_MODE" -eq 0 ]; then
  if is_wsl; then
    wsl_conf
    die "systemd isn't running in this WSL distro yet. In Windows: wsl --terminate <distro> (or install-windows.ps1 does it), then run this again"
  fi
  die "systemd isn't running: the station needs it to start at boot and restart itself"
fi

# -- git: the bare clone the releases are cut from (root's: the sizeup user can't change it) ---------
git_ssh() {
  printf 'ssh -i %s -o IdentitiesOnly=yes -o UserKnownHostsFile=%s -o StrictHostKeyChecking=yes -o BatchMode=yes' \
    "$DEPLOY_KEY" "$KNOWN_HOSTS"
}
repo_git() { env GIT_SSH_COMMAND="$(git_ssh)" git --git-dir="$REPO" "$@"; }

# The read-only deploy key, root's (/etc/sizeup/deploy_key). A station from before #58 kept it in
# the sizeup user's ~/.ssh: the same key is copied over, so GitHub needs nothing new.
install_key() {
  ssh_repo || return 0
  install -d -m 0750 -o root -g "$SVC_USER" "$ETC"
  if [ ! -f "$DEPLOY_KEY" ]; then
    if [ -f "$STATE/.ssh/id_ed25519" ]; then
      # (read as the sizeup user: whatever that path is, root copies only what sizeup could read)
      ( umask 077; as_user cat "$STATE/.ssh/id_ed25519" > "$DEPLOY_KEY.tmp" )
      mv -f "$DEPLOY_KEY.tmp" "$DEPLOY_KEY"
      say "code: the deploy key is now $DEPLOY_KEY (root only)"
    else
      ssh-keygen -q -t ed25519 -N "" -C "sizeup-station@$(hostname)" -f "$DEPLOY_KEY"
    fi
  fi
  chmod 0600 "$DEPLOY_KEY"
  grep -qF "$GITHUB_HOSTKEY" "$KNOWN_HOSTS" 2>/dev/null || echo "$GITHUB_HOSTKEY" >> "$KNOWN_HOSTS"
  chmod 0644 "$KNOWN_HOSTS"
}

check_access() {
  ssh_repo || return 0
  local err
  until err=$(env GIT_SSH_COMMAND="$(git_ssh)" git ls-remote "$SIZEUP_REPO" HEAD 2>&1 >/dev/null); do
    echo
    echo "GitHub said: $(printf '%s\n' "$err" | grep -v '^$' | tail -n 1)"
    if printf '%s' "$err" | grep -qiE 'resolve|timed out|unreachable|refused|no route'; then
      echo "That's the network, not the key: check this machine's internet connection."
    fi
    echo "This station reads the code with its own read-only deploy key. Add this PUBLIC key on GitHub:"
    echo "  GitHub > actualsecurity/sizeup > Settings > Deploy keys > Add deploy key"
    echo "  Title: sizeup station $(hostname)    Key:"
    echo
    ssh-keygen -y -f "$DEPLOY_KEY"
    echo
    echo "  Leave \"Allow write access\" OFF."
    interactive || die "no access to $SIZEUP_REPO yet: add the key above, then run this again"
    read -r -p "Press Enter once it's added (Ctrl-C to stop; run this again later to continue)... " _
  done
}

# The old checkout (sizeup's) as a git bundle, written by the sizeup user: root never runs git in it.
LEGACY_BUNDLE=""
legacy_bundle() {
  [ -z "$LEGACY_BUNDLE" ] || return 0
  LEGACY_BUNDLE=$(mktemp /tmp/sizeup-legacy.XXXXXX.bundle)
  as_user git -C "$LEGACY_APP" bundle create - --all > "$LEGACY_BUNDLE"
}
legacy_git() { as_user git -C "$LEGACY_APP" "$@"; }

# The bare clone: made once (from the old checkout when there is one: no network needed), then fetched.
ensure_repo() {
  if [ ! -d "$REPO" ] || ! git --git-dir="$REPO" rev-parse --git-dir >/dev/null 2>&1; then
    rm -rf "$REPO" "$REPO.tmp"
    if [ -d "$LEGACY_APP/.git" ]; then
      legacy_bundle
      git clone -q --bare "$LEGACY_BUNDLE" "$REPO.tmp"
      say "code: $REPO made from the old checkout $LEGACY_APP"
    else
      install_key
      check_access
      env GIT_SSH_COMMAND="$(git_ssh)" git clone -q --bare "$SIZEUP_REPO" "$REPO.tmp"
    fi
    mv "$REPO.tmp" "$REPO"
  fi
  repo_git config remote.origin.url "$SIZEUP_REPO"
  repo_git config remote.origin.fetch '+refs/heads/main:refs/remotes/origin/main'
}

# main (what the hub runs), and the configured ref when it's another branch or a tag. No other heads.
fetch_repo() {
  install_key
  check_access
  repo_git fetch -q --force origin '+refs/heads/main:refs/remotes/origin/main'
  if [ "$SIZEUP_REF" != main ]; then
    repo_git fetch -q --force origin "+refs/heads/$SIZEUP_REF:refs/remotes/origin/$SIZEUP_REF" 2>/dev/null \
      || repo_git fetch -q --force origin "+refs/tags/$SIZEUP_REF:refs/tags/$SIZEUP_REF" 2>/dev/null \
      || warn "no branch or tag $SIZEUP_REF on $SIZEUP_REPO (a commit sha is looked up as it is)"
  fi
}

# The commit a ref names: a branch (origin/REF), else a tag or a sha.
resolve_ref() {
  repo_git rev-parse -q --verify "refs/remotes/origin/$1^{commit}" 2>/dev/null \
    || repo_git rev-parse -q --verify "$1^{commit}" 2>/dev/null \
    || die "no commit $1 in $SIZEUP_REPO"
}

# -- packages --------------------------------------------------------------------------------------
PKGS=(
  ca-certificates curl git openssh-client rsync usbutils util-linux procps
  python3 python3-venv
  build-essential cmake pkg-config libusb-1.0-0-dev
  ffmpeg
  # trunk-recorder (its Ubuntu 24.04 Dockerfile, minus the SDRs we don't use)
  gnuradio-dev gr-osmosdr libboost-all-dev libcurl4-openssl-dev libgmp-dev libhackrf-dev
  liborc-0.4-dev libpthread-stubs0-dev librtlsdr-dev libsndfile1-dev libssl-dev libuhd-dev libusb-dev
)
all_installed() {
  local states
  states=$(dpkg-query -W -f '${db:Status-Status}\n' "${PKGS[@]}" 2>/dev/null || true)
  [ "$(printf '%s\n' "$states" | grep -c '^installed$')" -eq "${#PKGS[@]}" ]
}
packages() {
  step "packages (apt)"
  export DEBIAN_FRONTEND=noninteractive
  # an update: apt only when a package is missing (an update is seconds when only Python changed)
  if { [ "$UPDATE" -eq 1 ] || [ -n "$STAGE_SHA" ]; } && all_installed; then
    say "packages: all installed"
    return 0
  fi
  apt-get update -q
  apt-get install -y -q --no-install-recommends "${PKGS[@]}"
  # volk_profile (tunes GNU Radio's SIMD kernels for this CPU): optional, named differently per release
  for p in libvolk-bin libvolk2-bin; do
    if apt-get install -y -q --no-install-recommends "$p" >/dev/null 2>&1; then break; fi
  done
}

# -- user and directories --------------------------------------------------------------------------
user_and_dirs() {
  step "user $SVC_USER and directories"
  getent group plugdev >/dev/null || groupadd --system plugdev
  if ! id "$SVC_USER" >/dev/null 2>&1; then
    useradd --system --user-group --home-dir "$STATE" --shell /usr/sbin/nologin --comment "sizeup station" "$SVC_USER"
  fi
  usermod -a -G plugdev "$SVC_USER"
  install -d -m 0755 -o root -g root "$OPT" "$SRC" "$MODELS" "$RELEASES" "$BUILDS" "$UPDATE_STATE"
  # $STATE's parent is root's; anything inside is the sizeup user's to make (root never follows a
  # link the sizeup user could have put there)
  install -d -m 0750 -o "$SVC_USER" -g "$SVC_USER" "$STATE"
  as_user mkdir -p -m 0750 "$STATE/state" "$STATE/captures" "$TR_DIR"
  install -d -m 0750 -o root -g "$SVC_USER" "$ETC"
}

# -- the pinned builds, one directory per version --------------------------------------------------
# fetch_source NAME REPO TAG COMMIT -> prints the checkout's path. Refuses a tag that moved.
fetch_source() {
  local name=$1 repo=$2 tag=$3 commit=$4 dir="$SRC/$1" got
  if [ ! -d "$dir/.git" ]; then
    rm -rf "$dir"
    git init -q "$dir" >&2
    git -C "$dir" remote add origin "$repo" >&2
  fi
  git -C "$dir" fetch -q --depth 1 --force origin "refs/tags/$tag:refs/tags/$tag" >&2
  got=$(git -C "$dir" rev-parse "refs/tags/$tag^{commit}")
  [ "$got" = "$commit" ] || die "$name $tag is $got upstream, pinned $commit: refusing (update the pin on purpose)"
  git -C "$dir" -c advice.detachedHead=false checkout -q --force "$commit" >&2
  # The build dir stays for the same commit (a slow build that was stopped picks up where it was);
  # another commit or --rebuild starts it fresh.
  if [ "$REBUILD" -eq 1 ] || [ "$(cat "$dir/build/.sizeup-commit" 2>/dev/null)" != "$commit" ]; then rm -rf "$dir/build"; fi
  git -C "$dir" clean -q -fdx -e /build/ >&2
  mkdir -p "$dir/build"
  echo "$commit" > "$dir/build/.sizeup-commit"
  echo "$dir"
}
up_to_date() { [ "$REBUILD" -eq 0 ] && [ -x "$1" ] && [ "$(cat "$2" 2>/dev/null)" = "$3" ]; }

# Before #58 a build lived right at /opt/sizeup/<name>: move it under builds/ (the same disk: a
# rename; the programs running from it don't notice) and leave a link in its place.
adopt_legacy() {
  local link=$1 want=$2 versioned=$3 to
  if [ ! -d "$link" ] || [ -L "$link" ]; then return 0; fi
  if [ "$(cat "$link/.sizeup-build" 2>/dev/null)" = "$want" ] && [ ! -e "$versioned" ]; then
    to=$versioned
  else
    to="$BUILDS/$(basename "$link")-before-releases"
    rm -rf "$to"
  fi
  mv "$link" "$to"
  ln -sfn "$to" "$link"
  say "$(basename "$link"): moved to $to (the link $link stays)"
}

builds() {
  local want src lib pair
  adopt_legacy "$OPT/rtl-sdr" "rtl-sdr-blog $RTLSDR_TAG $RTLSDR_COMMIT" "$RTLSDR_B"
  adopt_legacy "$OPT/trunk-recorder" "trunk-recorder $TR_TAG $TR_COMMIT" "$TR_B"
  adopt_legacy "$OPT/whisper" "whisper.cpp $WHISPER_TAG $WHISPER_COMMIT native" "$WHISPER_B"

  step "rtl-sdr-blog $RTLSDR_TAG"
  want="rtl-sdr-blog $RTLSDR_TAG $RTLSDR_COMMIT"
  if up_to_date "$RTLSDR_B/bin/rtl_fm" "$RTLSDR_B/.sizeup-build" "$want"; then
    say "rtl-sdr-blog: up to date"
  else
    src=$(fetch_source rtl-sdr-blog "$RTLSDR_REPO" "$RTLSDR_TAG" "$RTLSDR_COMMIT")
    cmake -S "$src" -B "$src/build" -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$RTLSDR_B" \
      -DCMAKE_INSTALL_LIBDIR=lib -DCMAKE_INSTALL_RPATH="$RTLSDR_B/lib" \
      -DDETACH_KERNEL_DRIVER=ON -DINSTALL_UDEV_RULES=OFF
    cmake --build "$src/build" -j "$JOBS"
    rm -rf "$RTLSDR_B"
    cmake --install "$src/build"
    if [ ! -e "$RTLSDR_B/lib/librtlsdr.so.0" ]; then
      lib=$(find "$RTLSDR_B" -name 'librtlsdr.so.0' | head -n 1)
      [ -n "$lib" ] || die "rtl-sdr-blog installed no librtlsdr.so.0"
      install -d "$RTLSDR_B/lib"
      ln -sf "$lib" "$RTLSDR_B/lib/librtlsdr.so.0"
    fi
    echo "$want" > "$RTLSDR_B/.sizeup-build"
  fi

  step "trunk-recorder $TR_TAG"
  want="trunk-recorder $TR_TAG $TR_COMMIT"
  if up_to_date "$TR_B/bin/trunk-recorder" "$TR_B/.sizeup-build" "$want"; then
    say "trunk-recorder: up to date"
  else
    src=$(fetch_source trunk-recorder "$TR_REPO" "$TR_TAG" "$TR_COMMIT")
    cmake -S "$src" -B "$src/build" -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$TR_B"
    cmake --build "$src/build" -j "$JOBS"
    rm -rf "$TR_B"
    cmake --install "$src/build"
    echo "$want" > "$TR_B/.sizeup-build"
  fi
  [ -x "$TR_B/bin/trunk-recorder" ] || die "trunk-recorder didn't install"
  find "$TR_B" -name 'libunit_script.so' | grep -q . || die "trunk-recorder's unit_script plugin didn't install"

  step "whisper.cpp $WHISPER_TAG"
  want="whisper.cpp $WHISPER_TAG $WHISPER_COMMIT native"
  if up_to_date "$WHISPER_B/bin/whisper-server" "$WHISPER_B/.sizeup-build" "$want"; then
    say "whisper.cpp: up to date"
  else
    src=$(fetch_source whisper.cpp "$WHISPER_REPO" "$WHISPER_TAG" "$WHISPER_COMMIT")
    # GGML_NATIVE: tuned for this CPU (AVX2/FMA on the i5-6400T, NEON on a Pi); built here, run here.
    cmake -S "$src" -B "$src/build" -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF -DGGML_NATIVE=ON \
      -DWHISPER_BUILD_TESTS=OFF -DWHISPER_BUILD_EXAMPLES=ON -DWHISPER_BUILD_SERVER=ON -DWHISPER_SDL2=OFF -DWHISPER_CURL=OFF
    cmake --build "$src/build" -j "$JOBS" --target whisper-server
    rm -rf "$WHISPER_B"
    install -D -m 0755 "$src/build/bin/whisper-server" "$WHISPER_B/bin/whisper-server"
    echo "$want" > "$WHISPER_B/.sizeup-build"
  fi
  # A new box gets its links now; an existing link moves only with a release (activate).
  for pair in "rtl-sdr=$RTLSDR_B" "trunk-recorder=$TR_B" "whisper=$WHISPER_B"; do
    [ -e "$OPT/${pair%%=*}" ] || ln -sfn "${pair#*=}" "$OPT/${pair%%=*}"
  done
}

# -- whisper model ---------------------------------------------------------------------------------
model() {
  step "whisper model $SIZEUP_MODEL"
  local mfile msum url
  read -r mfile msum < <(model_file "$SIZEUP_MODEL")
  if [ -f "$MODELS/$mfile" ] && echo "$msum  $MODELS/$mfile" | sha256sum -c --quiet - 2>/dev/null; then
    say "model: $mfile verified"
  else
    url="https://huggingface.co/ggerganov/whisper.cpp/resolve/$MODEL_REV/$mfile"
    say "model: downloading $mfile"
    curl -fL --retry 5 --retry-delay 5 --connect-timeout 30 -o "$MODELS/$mfile.part" "$url"
    echo "$msum  $MODELS/$mfile.part" | sha256sum -c --quiet - || { rm -f "$MODELS/$mfile.part"; die "$mfile: SHA-256 mismatch"; }
    mv -f "$MODELS/$mfile.part" "$MODELS/$mfile"
    chmod 0644 "$MODELS/$mfile"
    say "model: $mfile verified"
  fi
  ln -sfn "$mfile" "$MODELS/whisper.bin"
}

# -- a release: the code at one commit and its venv, beside the running one ------------------------
reqs_key() { { cat "$1/pyproject.toml"; python3 --version; } | sha256sum | cut -c1-16; }
venv_ok() { [ -x "$1/venv/bin/python" ] && "$1/venv/bin/python" -c 'import numpy' >/dev/null 2>&1; }

# make_release NAME SHA [VENV_DONOR]: /opt/sizeup/releases/NAME, made once (a finished one is reused).
make_release() {
  local name=$1 sha=$2 donor=${3:-} rel="$RELEASES/$1" tmp="$RELEASES/.staging-$1" reqs d tmpsrc
  if [ -f "$rel/.complete" ]; then
    say "release $name: already staged"
    return 0
  fi
  step "release $name"
  rm -rf "$tmp" "$rel"
  install -d -m 0755 "$tmp"
  if [ "$SIZEUP_SOURCE" = dir ]; then
    rsync -a --exclude /.git --exclude /.venv --exclude /.claude --exclude /state --exclude /captures \
      --exclude /models --exclude /config.toml --exclude /build --exclude '__pycache__' --exclude '*.egg-info' \
      "$OPT_FROM_DIR/" "$tmp/"
  else
    repo_git archive --format=tar "$sha" | tar -x -C "$tmp"
  fi
  if [ ! -f "$tmp/pyproject.toml" ] || [ ! -d "$tmp/sizeup" ]; then die "$name: not a sizeup tree"; fi
  printf '%s\n' "$sha" > "$tmp/BUILD"
  reqs=$(reqs_key "$tmp")
  # The venv: the current release's (or any with the same requirements) as hard links, in a second;
  # a new one only when pyproject.toml (or Python) changed.
  if [ -z "$donor" ]; then
    for d in "$(readlink -f "$CURRENT" 2>/dev/null || true)" "$RELEASES"/*; do
      if [ -n "$d" ] && [ -f "$d/.complete" ] && [ "$(cat "$d/REQS" 2>/dev/null)" = "$reqs" ] && venv_ok "$d"; then
        donor=$d/venv
        break
      fi
    done
  fi
  if [ -n "$donor" ] && [ -x "$donor/bin/python" ]; then
    cp -al "$donor" "$tmp/venv"
    say "release $name: venv shared with $donor (requirements unchanged)"
  else
    say "release $name: new venv (requirements changed)"
    python3 -m venv "$tmp/venv"
    tmpsrc=$(mktemp -d)
    cp -a "$tmp/pyproject.toml" "$tmp/sizeup" "$tmpsrc/"
    "$tmp/venv/bin/python" -m pip install -q --disable-pip-version-check --no-cache-dir --upgrade "$tmpsrc"
    rm -rf "$tmpsrc"
  fi
  echo "$reqs" > "$tmp/REQS"
  # bytecode now, not at every start (the service runs with PYTHONDONTWRITEBYTECODE)
  "$tmp/venv/bin/python" -m compileall -q -j 0 "$tmp/sizeup" >/dev/null || warn "compileall complained"
  # the builds it runs with: this installer's pins (an older release keeps the ones it had)
  printf 'rtl-sdr=%s\ntrunk-recorder=%s\nwhisper=%s\n' "$RTLSDR_B" "$TR_B" "$WHISPER_B" > "$tmp/BUILDS"
  chown -R root:root "$tmp"
  chmod -R u+rwX,go+rX,go-w "$tmp"
  mv "$tmp" "$rel"
  touch "$rel/.complete"
  say "release $name: staged"
}

# The code that ran before #58 (the old checkout's commit, with the old venv and the builds it used),
# as a release too: the first switch can go back to it in a second.
LEGACY_NAME=""
legacy_release() {
  local sha=$1 name
  if [ -z "$sha" ] || [ ! -x "$LEGACY_VENV/bin/python" ]; then return 0; fi
  name=$(repo_git rev-parse -q --verify "$sha^{commit}" 2>/dev/null) || return 0
  if [ ! -f "$RELEASES/$name/.complete" ]; then
    make_release "$name" "$name" "$LEGACY_VENV"
    {
      echo "rtl-sdr=$(readlink -f "$OPT/rtl-sdr")"
      echo "trunk-recorder=$(readlink -f "$OPT/trunk-recorder")"
      echo "whisper=$(readlink -f "$OPT/whisper")"
    } > "$RELEASES/$name/BUILDS"
    say "release $(short "$name"): what ran before the switch to releases (to go back to)"
  fi
  echo "$name" > "$RELEASES/.legacy"
  grep -qxF "$name" "$RELEASES/.history" 2>/dev/null || echo "$name" >> "$RELEASES/.history"
  LEGACY_NAME=$name
}

# -- preflight: the new release must work before anything switches to it ---------------------------
builds_of() { sed -n "s/^$2=//p" "$1/BUILDS" 2>/dev/null | head -n 1; }

preflight() {
  local rel=$1 pf rtl tr wh out bad=0 conf=$CONF
  step "preflight $(basename "$rel")"
  rtl=$(builds_of "$rel" rtl-sdr)
  tr=$(builds_of "$rel" trunk-recorder)
  wh=$(builds_of "$rel" whisper)
  [ -f "$conf" ] || conf="$rel/deploy/station/config.station.toml"
  pf=$(mktemp -d /tmp/sizeup-preflight.XXXXXX)
  chown "$SVC_USER:$SVC_USER" "$pf"
  pfpy() {
    runuser -u "$SVC_USER" -- env -C "$rel" HOME="$STATE" PYTHONPATH="$rel" SIZEUP_STATE_DIR="$pf" \
      PYTHONDONTWRITEBYTECODE=1 LD_LIBRARY_PATH="$rtl/lib" "$rel/venv/bin/python" "$@"
  }
  if out=$(pfpy -c 'import sizeup.app, sizeup.cloud.station, sizeup.cloud.stationmode, sizeup.web.server' 2>&1); then
    say "preflight: Python imports"
  else
    warn "preflight: imports fail: $(printf '%s' "$out" | tail -n 1)"; bad=1
  fi
  if out=$(pfpy -c 'import sys; from sizeup.config import load; load(sys.argv[1])' "$conf" 2>&1); then
    say "preflight: $conf loads"
  else
    warn "preflight: $conf doesn't load with this release: $(printf '%s' "$out" | tail -n 1)"; bad=1
  fi
  if out=$(pfpy -m sizeup --config "$conf" --init-state 2>&1); then
    say "preflight: the databases come up (sizeup --init-state in a scratch dir)"
  else
    warn "preflight: sizeup --init-state fails: $(printf '%s' "$out" | tail -n 1)"; bad=1
  fi
  rm -rf "$pf"
  if command -v systemd-analyze >/dev/null && [ -d "$rel/deploy/station/systemd" ]; then
    # (a program that isn't installed yet is fine here; a unit that doesn't parse is not)
    out=$(systemd-analyze verify "$rel"/deploy/station/systemd/*.service "$rel"/deploy/station/systemd/*.timer \
            "$rel"/deploy/station/systemd/*.path 2>&1 \
          | grep -F "$rel/deploy/station/systemd/" \
          | grep -iE 'unknown|failed to parse|invalid|bad-setting|missing' \
          | grep -viE 'not executable|no such file|not found' || true)
    if [ -z "$out" ]; then say "preflight: systemd units parse"; else warn "preflight: systemd units: $out"; bad=1; fi
  fi
  if [ ! -x "$rtl/bin/rtl_fm" ] || ldd "$rtl/bin/rtl_fm" 2>/dev/null | grep -q 'not found'; then
    warn "preflight: $rtl/bin/rtl_fm can't run"; bad=1
  fi
  if ! out=$(LD_LIBRARY_PATH="$rtl/lib:$tr/lib/trunk-recorder" "$tr/bin/trunk-recorder" --version 2>&1); then
    warn "preflight: trunk-recorder --version: $(printf '%s' "$out" | tail -n 1)"; bad=1
  fi
  if ! "$wh/bin/whisper-server" --help >/dev/null 2>&1; then
    warn "preflight: whisper-server --help fails"; bad=1
  fi
  if [ "$bad" -ne 0 ]; then
    say "FAILED: preflight failed for $(basename "$rel"): not switching to it" >&2
    exit 3                                     # (the updater: a build that doesn't work, not one to retry)
  fi
  say "preflight: ok"
}

# -- activate: point the station at a release (system files, build links, current), one restart ----
swap_link() {     # swap_link TARGET LINK: a rename over the old link, never a moment without one
  local tmp
  tmp="$(dirname "$2")/.$(basename "$2").$$"
  ln -sfn "$1" "$tmp"
  mv -Tf "$tmp" "$2"
}

# what the running whisper-server was started with (written by start-whisper as the sizeup user):
# read as that user, bounded, never waiting long
whisper_argv() { timeout 5 runuser -u "$SVC_USER" -- head -c 8192 /run/sizeup-whisper/argv 2>/dev/null || true; }

activate() {
  local name=$1 rel="$RELEASES/$1" sys pair t want have t0 t1
  [ -f "$rel/.complete" ] || die "no staged release $name in $RELEASES"
  step "switch to release $name"
  # The system files: the release's own, or (a release from before #58) this installer's.
  sys="$rel/deploy/station"
  [ -f "$sys/start-station" ] || sys=$SELF_DIR
  [ -f "$sys/start-station" ] || die "no start-station next to $0"
  install -m 0755 "$sys/sizeup-station" /usr/local/bin/sizeup-station
  install -D -m 0755 "$sys/health-check.sh" "$LIB/health-check"
  install -D -m 0755 "$sys/start-station" "$LIB/start-station"
  install -D -m 0755 "$sys/start-whisper" "$LIB/start-whisper"
  install -D -m 0644 "$sys/../../sizeup/cloud/autoupdate.py" "$LIB/autoupdate.py"
  for t in sizeup-station.service sizeup-whisper.service sizeup-health.service sizeup-health.timer \
           sizeup-update.service sizeup-update.path sizeup-update.timer; do
    install -m 0644 "$sys/systemd/$t" "/etc/systemd/system/$t"
  done
  install -D -m 0644 "$sys/systemd/journald-sizeup.conf" /etc/systemd/journald.conf.d/sizeup.conf
  install -d -m 2755 -o root -g systemd-journal /var/log/journal 2>/dev/null || install -d -m 2755 /var/log/journal
  rm -f "$ETC/build.env"                       # (before #58; the release's BUILD file says it now)
  # A config from before #58 read the talkgroups from the old checkout, which goes away: trunk-recorder's copy.
  if grep -qF "$LEGACY_APP/deploy/trunk-recorder/talkgroups.csv" "$CONF" 2>/dev/null; then
    sed -i "s#$LEGACY_APP/deploy/trunk-recorder/talkgroups.csv#$TR_DIR/talkgroups.csv#" "$CONF"
    say "config: [p25] talkgroups_file is now $TR_DIR/talkgroups.csv (trunk-recorder's copy, from the release)"
  fi
  if [ -f "$rel/deploy/station/config.station.toml" ]; then
    install -m 0640 -o root -g "$SVC_USER" "$rel/deploy/station/config.station.toml" "$CONF.dist"
  fi
  # trunk-recorder's working directory, from this release's deploy/trunk-recorder: written by the
  # sizeup user (it owns that directory; root never writes through a path it controls)
  as_user rm -f "$TR_DIR/talkgroups.csv" "$TR_DIR/unit-event.sh"       # (a link there goes, not what it points at)
  as_user install -m 0644 "$rel/deploy/trunk-recorder/talkgroups.csv" "$TR_DIR/talkgroups.csv"
  as_user install -m 0755 "$rel/deploy/trunk-recorder/unit-event.sh" "$TR_DIR/unit-event.sh"
  # Made from the repo's config.json each time (change it there): capture paths absolute, under $STATE.
  as_user python3 - "$rel/deploy/trunk-recorder/config.json" "$TR_DIR/config.json" "$STATE/captures" <<'PY'
import json, os, sys
src, dst, captures = sys.argv[1:4]
c = json.load(open(src))
c["captureDir"] = f"{captures}/p25"
c["tempDir"] = f"{captures}/p25-tmp"
for s in c.get("systems", []):
    s["talkgroupsFile"] = "talkgroups.csv"
    s["unitScript"] = "./unit-event.sh"
with open(dst + ".tmp", "w") as f:
    json.dump(c, f, indent=2)
os.replace(dst + ".tmp", dst)       # (replaces whatever is there, a link included: never written through)
PY
  as_user mkdir -p -m 0750 "$STATE/captures/p25" "$STATE/captures/p25-tmp" "$STATE/captures/clips"
  # The builds this release was staged with, then the release itself: renames, nothing rebuilt.
  while IFS= read -r pair; do
    case "${pair%%=*}" in rtl-sdr|trunk-recorder|whisper) ;; *) continue ;; esac
    [ -d "${pair#*=}" ] || die "release $name wants ${pair#*=}, which isn't there: stage it again"
    swap_link "${pair#*=}" "$OPT/${pair%%=*}"
  done < "$rel/BUILDS"
  for t in rtl_fm rtl_test rtl_eeprom; do ln -sfn "$OPT/rtl-sdr/bin/$t" "/usr/local/bin/$t"; done
  ln -sfn "$OPT/whisper/bin/whisper-server" /usr/local/bin/whisper-server
  swap_link "releases/$name" "$CURRENT"
  echo "$name" >> "$RELEASES/.history"
  ldconfig
  if [ "$CI_MODE" -eq 1 ] || [ "$HAVE_SYSTEMD" -eq 0 ]; then
    say "release $name is current (CI: nothing restarted)"
    return 0
  fi
  systemctl daemon-reload
  systemctl enable --quiet sizeup-station.service sizeup-whisper.service sizeup-health.timer \
    sizeup-update.path sizeup-update.timer
  t0=$(date +%s%N)
  systemctl restart sizeup-station.service
  t1=$(date +%s%N)
  # whisper-server is a service of its own: restarted only when its program, model or flags change
  want=$("$LIB/start-whisper" --print 2>/dev/null || true)
  have=$(whisper_argv)
  if systemctl is-active --quiet sizeup-whisper.service && [ "$want" = "$have" ]; then
    say "whisper-server: unchanged, left running (the model stays loaded)"
  else
    systemctl restart sizeup-whisper.service
    say "whisper-server: (re)started"
  fi
  systemctl restart sizeup-health.timer
  # (a run by hand stopped them: they start again when it ends)
  [ "$PAUSED_UPDATER" -eq 1 ] || systemctl start sizeup-update.path sizeup-update.timer
  say "release $name is current; sizeup-station restarted in $(( (t1 - t0) / 1000000 )) ms"
}

# healthy BUILD SECONDS: /healthz answers with that build ("" = any) within SECONDS
healthy() {
  local build=$1 limit=$2 port i got
  port=$(python3 -c 'import sys, tomllib; print(int(tomllib.load(open(sys.argv[1], "rb")).get("web", {}).get("port", 8090)))' "$CONF" 2>/dev/null || echo 8090)
  for i in $(seq 1 "$limit"); do
    got=$(curl -fsS --max-time 2 "http://127.0.0.1:$port/healthz" 2>/dev/null \
      | python3 -c 'import json, sys; print(json.load(sys.stdin).get("build", "") or "-")' 2>/dev/null || true)
    if [ -n "$got" ] && { [ -z "$build" ] || [ "${build#"$got"}" != "$build" ]; }; then
      say "sizeup-station answers ($(short "$got")) after ${i} s: http://localhost:$port (station page)"
      return 0
    fi
    sleep 1
  done
  return 1
}

if [ -n "$STAGE_SHA" ]; then
  # -- the auto-updater: the release beside the running one, preflighted. Nothing switches. ---------
  STEP="stage $STAGE_SHA"
  [ "$SIZEUP_SOURCE" = git ] || die "--stage: this station runs from a folder, not git"
  user_and_dirs
  packages
  builds
  model
  # Exactly this commit, and only if main has it: a full sha, looked up as an object (never as a
  # branch or tag of that name), checked against origin/main here as well as by the updater.
  [[ "$STAGE_SHA" =~ ^[0-9a-f]{40}$ ]] || die "--stage: a full 40-character commit sha"
  ensure_repo
  sha=$(repo_git rev-parse -q --verify "$STAGE_SHA^{commit}" 2>/dev/null || true)
  [ "$sha" = "$STAGE_SHA" ] || die "--stage: no commit $STAGE_SHA in $REPO"
  repo_git merge-base --is-ancestor "$sha" "refs/remotes/origin/main" || die "--stage: $sha isn't on origin/main"
  make_release "$sha" "$sha"
  preflight "$RELEASES/$sha"
  STEP=""
  say "staged $sha"
  exit 0
fi
if [ -n "$ACTIVATE_NAME" ]; then
  STEP="activate $ACTIVATE_NAME"
  activate "$ACTIVATE_NAME"
  STEP=""
  exit 0
fi

# -- install / repair / update ----------------------------------------------------------------------
packages
user_and_dirs
save_install_conf
builds
model

step "sizeup code ($SIZEUP_SOURCE)"
BACK=""                                   # the release to go back to if the new one doesn't come up
[ -L "$CURRENT" ] && BACK=$(basename "$(readlink -f "$CURRENT")")
LEGACY_SHA=""
if [ ! -L "$CURRENT" ] && [ -d "$LEGACY_APP/.git" ]; then
  # The first run since #58: what ran so far. build.env (the old installer wrote it), else the
  # checkout's HEAD before the old installer's --update moved it (HEAD@{1}), else HEAD.
  LEGACY_SHA=$(sed -n 's/^SIZEUP_BUILD=//p' "$ETC/build.env" 2>/dev/null | head -n 1)
  [ -n "$LEGACY_SHA" ] || LEGACY_SHA=$(legacy_git rev-parse -q --verify 'HEAD@{1}' 2>/dev/null || true)
  [ -n "$LEGACY_SHA" ] || LEGACY_SHA=$(legacy_git rev-parse HEAD 2>/dev/null || true)
  say "code: moving this station to side-by-side releases (it ran $(short "${LEGACY_SHA:-?}") from $LEGACY_APP)"
fi
SHA=""
if [ "$SIZEUP_SOURCE" = dir ]; then
  [ -n "$OPT_FROM_DIR" ] || die "installed from a folder: run again with --from-dir <the new copy> (or --repo/--ref for git)"
  OPT_FROM_DIR=$(cd "$OPT_FROM_DIR" && pwd)
  if [ ! -f "$OPT_FROM_DIR/pyproject.toml" ] || [ ! -d "$OPT_FROM_DIR/sizeup" ]; then die "--from-dir $OPT_FROM_DIR: not a sizeup checkout"; fi
  SHA=$(git -c safe.directory='*' -C "$OPT_FROM_DIR" rev-parse HEAD 2>/dev/null || true)
  NAME="dir-$(date +%Y%m%d-%H%M%S)"
  make_release "$NAME" "$SHA"
else
  ensure_repo
  if [ "$NO_FETCH" -eq 1 ] && [ -n "$LEGACY_SHA" ]; then
    # An installer from before #58 already fetched, checked the new code out in $LEGACY_APP and ran
    # this one: that commit is the one to run.
    legacy_bundle
    repo_git fetch -q "$LEGACY_BUNDLE" "+HEAD:refs/sizeup/checkout-head"
    SHA=$(repo_git rev-parse refs/sizeup/checkout-head)
    install_key
  elif [ "$UPDATE" -eq 1 ] || [ -n "$OPT_REF$OPT_REPO" ] || [ -z "$BACK" ]; then
    step "fetch $SIZEUP_REF"
    fetch_repo
    SHA=$(resolve_ref "$SIZEUP_REF")
  else
    SHA=$(cat "$CURRENT/BUILD")
    say "code: keeping release $(short "$SHA") (sizeup-station update fetches $SIZEUP_REF)"
  fi
  if [ -n "$LEGACY_SHA" ]; then
    legacy_release "$LEGACY_SHA"
    BACK=$LEGACY_NAME
  fi
  NAME=$SHA
  make_release "$NAME" "$SHA"
fi
REL="$RELEASES/$NAME"
[ -z "$LEGACY_BUNDLE" ] || rm -f "$LEGACY_BUNDLE"
if [ "$REBUILD" -eq 1 ]; then                 # (a release staged before: it runs with the rebuilt ones now)
  printf 'rtl-sdr=%s\ntrunk-recorder=%s\nwhisper=%s\n' "$RTLSDR_B" "$TR_B" "$WHISPER_B" > "$REL/BUILDS"
fi

# -- config ----------------------------------------------------------------------------------------
step "config ($CONF)"
if [ ! -f "$CONF" ]; then
  install -m 0640 -o root -g "$SVC_USER" "$REL/deploy/station/config.station.toml" "$CONF"
  say "config: new $CONF from the template"
elif ! cmp -s "$CONF" "$REL/deploy/station/config.station.toml"; then
  say "config: keeping your $CONF (the current template is $CONF.dist)"
fi
# (a new box: the talkgroups the config names, before the preflight loads it)
[ -f "$TR_DIR/talkgroups.csv" ] || as_user install -m 0644 "$REL/deploy/trunk-recorder/talkgroups.csv" "$TR_DIR/talkgroups.csv"
preflight "$REL"

ask_secrets 0

# A volk profile makes GNU Radio (trunk-recorder) use this CPU's fastest kernels: once, a few minutes.
if [ "$CI_MODE" -eq 0 ] && command -v volk_profile >/dev/null && [ ! -f "$STATE/.volk/volk_config" ]; then
  step "volk_profile (once, a few minutes)"
  as_user env HOME="$STATE" volk_profile >/dev/null 2>&1 || warn "volk_profile failed (trunk-recorder still works, a bit slower)"
fi

step "dongles (udev, modprobe, ld.so)"
cat > /etc/udev/rules.d/60-sizeup-rtlsdr.rules <<'EOF'
# sizeup: RTL-SDR dongles (RTL2838 / RTL2832U) usable by the plugdev group (the sizeup user)
SUBSYSTEM=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2838", ENV{ID_SOFTWARE_RADIO}="1", MODE="0660", GROUP="plugdev"
SUBSYSTEM=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2832", ENV{ID_SOFTWARE_RADIO}="1", MODE="0660", GROUP="plugdev"
EOF
cat > /etc/modprobe.d/sizeup-rtlsdr-blacklist.conf <<'EOF'
# sizeup: keep the DVB-T TV drivers off the RTL-SDRs (librtlsdr also detaches them)
blacklist dvb_usb_rtl28xxu
blacklist rtl2832
blacklist rtl2830
blacklist rtl2832_sdr
EOF
# The blog librtlsdr first for everything that loads librtlsdr.so.0 (sizeup's serial lookup, and
# gr-osmosdr on Debian / Pi OS, whose own librtlsdr0 predates the V4). Ubuntu 24.04's gr-osmosdr uses
# librtlsdr.so.2 (osmocom 2.0.1, which knows the V4 too). Through the link: it follows the release.
echo "$OPT/rtl-sdr/lib" > /etc/ld.so.conf.d/00-sizeup-rtlsdr.conf
if [ "$CI_MODE" -eq 0 ]; then
  udevadm control --reload-rules 2>/dev/null || true
  udevadm trigger --subsystem-match=usb 2>/dev/null || true
  modprobe -r dvb_usb_rtl28xxu 2>/dev/null || true
fi

if [ "$CI_MODE" -eq 0 ] && ! systemctl is-active --quiet sizeup-station 2>/dev/null; then
  as_user env -C "$REL" PYTHONPATH="$REL" SIZEUP_STATE_DIR="$STATE" "$REL/venv/bin/python" -m sizeup \
    --config "$CONF" --init-state || die "sizeup --init-state failed"
fi
activate "$NAME"
if command -v systemd-analyze >/dev/null; then
  systemd-analyze verify /etc/systemd/system/sizeup-station.service /etc/systemd/system/sizeup-whisper.service \
    /etc/systemd/system/sizeup-health.timer /etc/systemd/system/sizeup-update.path \
    /etc/systemd/system/sizeup-update.timer || warn "systemd-analyze verify complained (above)"
fi

if [ "$CI_MODE" -eq 0 ]; then
  if healthy "$SHA" 90; then
    say "status: sizeup-station status · checks: sizeup-station doctor"
    python3 -I "$LIB/autoupdate.py" prune || warn "pruning old releases failed"
  elif [ -n "$BACK" ] && [ "$BACK" != "$NAME" ] && [ -f "$RELEASES/$BACK/.complete" ]; then
    warn "release $NAME didn't answer within 90 s: going back to $BACK (sizeup-station logs)"
    activate "$BACK"
    die "release $NAME didn't come up; the station runs $BACK again"
  else
    warn "sizeup-station hasn't answered yet: sizeup-station logs"
  fi
fi
STEP=""
say "done ($SIZEUP_REF $(short "${SHA:-$NAME}"), release $NAME, model $SIZEUP_MODEL)"
