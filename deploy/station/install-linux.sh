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
#     --update             fetch and check out the ref again, then repair everything and restart
#     --secrets            only (re)enter the hub token and the Cloudflare Access service token
#     --rebuild            rebuild rtl-sdr, trunk-recorder and whisper.cpp even if up to date
#     --ci                 CI: no systemd enable/start, no hardware, no prompts, no deploy key
#     --wsl-prepare        (install-windows.ps1) only write /etc/wsl.conf (systemd on), then exit
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
APP=$OPT/app
VENV=$OPT/venv
SRC=$OPT/src
RTLSDR_PREFIX=$OPT/rtl-sdr
TR_PREFIX=$OPT/trunk-recorder
WHISPER_PREFIX=$OPT/whisper
MODELS=$OPT/models
STATE=/var/lib/sizeup
TR_DIR=$STATE/trunk-recorder
ETC=/etc/sizeup
CONF=$ETC/config.toml
ENVFILE=$ETC/station.env
INSTALL_CONF=$ETC/install.conf
LOG=/var/log/sizeup-install.log
SVC_USER=sizeup

# -- options ---------------------------------------------------------------------------------------
CI_MODE=0 UPDATE=0 SECRETS_ONLY=0 REBUILD=0 NO_FETCH=0 WSL_PREPARE=0
OPT_REF="" OPT_REPO="" OPT_MODEL="" OPT_FROM_DIR=""
usage() { awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; }
while [ $# -gt 0 ]; do
  case "$1" in
    --ci) CI_MODE=1 ;;
    --update) UPDATE=1 ;;
    --secrets) SECRETS_ONLY=1 ;;
    --rebuild) REBUILD=1 ;;
    --no-fetch) NO_FETCH=1 ;;
    --wsl-prepare) WSL_PREPARE=1 ;;
    --ref) OPT_REF="${2:?--ref needs a value}"; shift ;;
    --repo) OPT_REPO="${2:?--repo needs a value}"; shift ;;
    --model) OPT_MODEL="${2:?--model needs a value}"; shift ;;
    --from-dir) OPT_FROM_DIR="${2:?--from-dir needs a value}"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "install-linux.sh: unknown option $1 (--help)" >&2; exit 2 ;;
  esac
  shift
done

if [ "$(id -u)" -ne 0 ]; then
  echo "install-linux.sh: run it as root (sudo $0 ...)" >&2
  exit 1
fi

mkdir -p "$(dirname "$LOG")"
touch "$LOG" && chmod 0640 "$LOG"
if [ -z "${SIZEUP_INSTALL_TEE:-}" ]; then     # (once: --update re-runs the new installer under this one)
  export SIZEUP_INSTALL_TEE=1
  exec > >(tee -a "$LOG") 2>&1
fi

STEP=""
say() { printf '%s  %s\n' "$(date '+%F %T')" "$*"; }
step() { STEP="$*"; say "== $*"; }
warn() { say "WARNING: $*"; }
die() { say "FAILED: $*" >&2; exit 1; }
on_exit() {
  local rc=$?
  if [ "$rc" -ne 0 ]; then
    say "install-linux.sh stopped (exit $rc) during: ${STEP:-start}. Fix it and run it again: it picks up where it left off. Log: $LOG"
  fi
}
trap on_exit EXIT

is_wsl() { grep -qiE 'microsoft|wsl' /proc/sys/kernel/osrelease 2>/dev/null; }
interactive() { [ "$CI_MODE" -eq 0 ] && [ -t 0 ]; }
as_user() { runuser -u "$SVC_USER" -- env HOME="$STATE" "$@"; }

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
if [ -f "$INSTALL_CONF" ]; then
  # KEY=value lines this installer wrote; read them without sourcing
  while IFS='=' read -r k v; do
    case "$k" in
      SIZEUP_REPO|SIZEUP_REF|SIZEUP_SOURCE|SIZEUP_MODEL) printf -v "$k" '%s' "$v" ;;
    esac
  done < "$INSTALL_CONF"
fi
[ -n "$OPT_REPO" ] && SIZEUP_REPO="$OPT_REPO"
[ -n "$OPT_REF" ] && SIZEUP_REF="$OPT_REF"
[ -n "$OPT_MODEL" ] && SIZEUP_MODEL="$OPT_MODEL"
[ -n "$OPT_FROM_DIR" ] && SIZEUP_SOURCE="dir"
[ -z "$OPT_FROM_DIR" ] && [ -n "$OPT_REPO$OPT_REF" ] && SIZEUP_SOURCE="git"
model_file "$SIZEUP_MODEL" >/dev/null || die "--model: small.en-q5_1, large-v3-turbo-q5_0 or base.en (not $SIZEUP_MODEL)"
[[ "$SIZEUP_REF" =~ ^[A-Za-z0-9._/-]+$ ]] || die "--ref: a branch or tag name"
if [ "$SIZEUP_SOURCE" = dir ] && [ -z "$OPT_FROM_DIR" ] && [ "$WSL_PREPARE" -eq 0 ] && [ "$SECRETS_ONLY" -eq 0 ]; then
  die "this station was installed from a folder: run install-linux.sh --from-dir <the new copy> (or --repo URL --ref REF to switch to git)"
fi
if [ "$CI_MODE" -eq 1 ] && [ "$SIZEUP_SOURCE" = git ] && [ ! -d "$APP/.git" ]; then
  die "--ci needs --from-dir (no deploy key in CI)"
fi

save_install_conf() {
  install -d -m 0750 -o root -g "$SVC_USER" "$ETC"
  local tmp
  tmp=$(mktemp "$ETC/.install.conf.XXXXXX")
  {
    echo "# written by install-linux.sh: what --update fetches and which model runs"
    echo "SIZEUP_REPO=$SIZEUP_REPO"
    echo "SIZEUP_REF=$SIZEUP_REF"
    echo "SIZEUP_SOURCE=$SIZEUP_SOURCE"
    echo "SIZEUP_MODEL=$SIZEUP_MODEL"
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
say "sizeup station install: ${PRETTY_NAME:-$ID} $ARCH, $(nproc) CPU(s), $(( MEM_KB / 1024 )) MB$(is_wsl && echo ', WSL2')$([ "$CI_MODE" -eq 1 ] && echo ', CI mode'); building with -j$JOBS"
command -v apt-get >/dev/null || die "apt-get not found: this installer is for Debian / Ubuntu / Raspberry Pi OS"
HAVE_SYSTEMD=0
[ -d /run/systemd/system ] && HAVE_SYSTEMD=1
if [ "$HAVE_SYSTEMD" -eq 0 ] && [ "$CI_MODE" -eq 0 ]; then
  if is_wsl; then
    wsl_conf
    die "systemd isn't running in this WSL distro yet. In Windows: wsl --terminate <distro> (or install-windows.ps1 does it), then run this again"
  fi
  die "systemd isn't running: the station needs it to start at boot and restart itself"
fi

# -- update: new code first, then the rest with the new installer ---------------------------------
git_ssh() {
  printf 'ssh -i %s -o IdentitiesOnly=yes -o UserKnownHostsFile=%s -o StrictHostKeyChecking=yes -o BatchMode=yes' \
    "$STATE/.ssh/id_ed25519" "$STATE/.ssh/known_hosts"
}
app_git() { as_user env GIT_SSH_COMMAND="$(git_ssh)" git -C "$APP" "$@"; }

checkout_ref() {
  app_git fetch --prune --tags --force origin
  if app_git rev-parse -q --verify "refs/remotes/origin/$SIZEUP_REF" >/dev/null; then
    app_git checkout -q --force -B "$SIZEUP_REF" "origin/$SIZEUP_REF"
  else
    app_git checkout -q --force --detach "$SIZEUP_REF"
  fi
  app_git clean -q -fd
  say "code: $(app_git log -1 --format='%h %s (%cd)' --date=short)"
}

if [ "$UPDATE" -eq 1 ] && [ "$NO_FETCH" -eq 0 ] && [ "$SIZEUP_SOURCE" = git ]; then
  step "update: fetch $SIZEUP_REF"
  [ -d "$APP/.git" ] || die "$APP isn't a git checkout: run install-linux.sh without --update first"
  app_git remote set-url origin "$SIZEUP_REPO"
  checkout_ref
  save_install_conf
  args=(--update --no-fetch)
  [ "$CI_MODE" -eq 1 ] && args+=(--ci)
  [ "$REBUILD" -eq 1 ] && args+=(--rebuild)
  exec bash "$APP/deploy/station/install-linux.sh" "${args[@]}"
fi

# -- packages --------------------------------------------------------------------------------------
step "packages (apt)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
PKGS=(
  ca-certificates curl git openssh-client rsync usbutils util-linux procps
  python3 python3-venv
  build-essential cmake pkg-config libusb-1.0-0-dev
  ffmpeg
  # trunk-recorder (its Ubuntu 24.04 Dockerfile, minus the SDRs we don't use)
  gnuradio-dev gr-osmosdr libboost-all-dev libcurl4-openssl-dev libgmp-dev libhackrf-dev
  liborc-0.4-dev libpthread-stubs0-dev librtlsdr-dev libsndfile1-dev libssl-dev libuhd-dev libusb-dev
)
apt-get install -y -q --no-install-recommends "${PKGS[@]}"
# volk_profile (tunes GNU Radio's SIMD kernels for this CPU): optional, named differently per release
for p in libvolk-bin libvolk2-bin; do
  if apt-get install -y -q --no-install-recommends "$p" >/dev/null 2>&1; then break; fi
done

# -- user and directories --------------------------------------------------------------------------
step "user $SVC_USER and directories"
getent group plugdev >/dev/null || groupadd --system plugdev
if ! id "$SVC_USER" >/dev/null 2>&1; then
  useradd --system --user-group --home-dir "$STATE" --shell /usr/sbin/nologin --comment "sizeup station" "$SVC_USER"
fi
usermod -a -G plugdev "$SVC_USER"
install -d -m 0755 -o root -g root "$OPT" "$SRC" "$MODELS"
install -d -m 0755 -o "$SVC_USER" -g "$SVC_USER" "$APP"
install -d -m 0750 -o "$SVC_USER" -g "$SVC_USER" "$STATE" "$STATE/state" "$STATE/captures" "$TR_DIR"
install -d -m 0700 -o "$SVC_USER" -g "$SVC_USER" "$STATE/.ssh"
install -d -m 0750 -o root -g "$SVC_USER" "$ETC"
save_install_conf

# -- sources of the pinned builds ------------------------------------------------------------------
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
  git -C "$dir" clean -q -fdx >&2
  echo "$dir"
}
up_to_date() { [ "$REBUILD" -eq 0 ] && [ -x "$1" ] && [ "$(cat "$2" 2>/dev/null)" = "$3" ]; }

# -- rtl-sdr-blog ----------------------------------------------------------------------------------
step "rtl-sdr-blog $RTLSDR_TAG"
want="rtl-sdr-blog $RTLSDR_TAG $RTLSDR_COMMIT"
if up_to_date "$RTLSDR_PREFIX/bin/rtl_fm" "$RTLSDR_PREFIX/.sizeup-build" "$want"; then
  say "rtl-sdr-blog: up to date"
else
  src=$(fetch_source rtl-sdr-blog "$RTLSDR_REPO" "$RTLSDR_TAG" "$RTLSDR_COMMIT")
  cmake -S "$src" -B "$src/build" -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$RTLSDR_PREFIX" \
    -DCMAKE_INSTALL_LIBDIR=lib -DCMAKE_INSTALL_RPATH="$RTLSDR_PREFIX/lib" \
    -DDETACH_KERNEL_DRIVER=ON -DINSTALL_UDEV_RULES=OFF
  cmake --build "$src/build" -j "$JOBS"
  rm -rf "$RTLSDR_PREFIX"
  cmake --install "$src/build"
  if [ ! -e "$RTLSDR_PREFIX/lib/librtlsdr.so.0" ]; then
    lib=$(find "$RTLSDR_PREFIX" -name 'librtlsdr.so.0' | head -n 1)
    [ -n "$lib" ] || die "rtl-sdr-blog installed no librtlsdr.so.0"
    install -d "$RTLSDR_PREFIX/lib"
    ln -sf "$lib" "$RTLSDR_PREFIX/lib/librtlsdr.so.0"
  fi
  echo "$want" > "$RTLSDR_PREFIX/.sizeup-build"
fi
for t in rtl_fm rtl_test rtl_eeprom; do
  if [ -x "$RTLSDR_PREFIX/bin/$t" ]; then ln -sf "$RTLSDR_PREFIX/bin/$t" "/usr/local/bin/$t"; fi
done
# The blog librtlsdr first for everything that loads librtlsdr.so.0 (sizeup's serial lookup, and
# gr-osmosdr on Debian / Pi OS, whose own librtlsdr0 predates the V4). Ubuntu 24.04's gr-osmosdr uses
# librtlsdr.so.2 (osmocom 2.0.1, which knows the V4 too).
echo "$RTLSDR_PREFIX/lib" > /etc/ld.so.conf.d/00-sizeup-rtlsdr.conf
ldconfig
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
if [ "$CI_MODE" -eq 0 ]; then
  udevadm control --reload-rules 2>/dev/null || true
  udevadm trigger --subsystem-match=usb 2>/dev/null || true
  modprobe -r dvb_usb_rtl28xxu 2>/dev/null || true
fi

# -- trunk-recorder --------------------------------------------------------------------------------
step "trunk-recorder $TR_TAG"
want="trunk-recorder $TR_TAG $TR_COMMIT"
if up_to_date "$TR_PREFIX/bin/trunk-recorder" "$TR_PREFIX/.sizeup-build" "$want"; then
  say "trunk-recorder: up to date"
else
  src=$(fetch_source trunk-recorder "$TR_REPO" "$TR_TAG" "$TR_COMMIT")
  cmake -S "$src" -B "$src/build" -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$TR_PREFIX"
  cmake --build "$src/build" -j "$JOBS"
  rm -rf "$TR_PREFIX"
  cmake --install "$src/build"
  echo "$want" > "$TR_PREFIX/.sizeup-build"
fi
[ -x "$TR_PREFIX/bin/trunk-recorder" ] || die "trunk-recorder didn't install"
find "$TR_PREFIX" -name 'libunit_script.so' | grep -q . || die "trunk-recorder's unit_script plugin didn't install"

# -- whisper.cpp -----------------------------------------------------------------------------------
step "whisper.cpp $WHISPER_TAG"
want="whisper.cpp $WHISPER_TAG $WHISPER_COMMIT native"
if up_to_date "$WHISPER_PREFIX/bin/whisper-server" "$WHISPER_PREFIX/.sizeup-build" "$want"; then
  say "whisper.cpp: up to date"
else
  src=$(fetch_source whisper.cpp "$WHISPER_REPO" "$WHISPER_TAG" "$WHISPER_COMMIT")
  # GGML_NATIVE: tuned for this CPU (AVX2/FMA on the i5-6400T, NEON on a Pi); built here, run here.
  cmake -S "$src" -B "$src/build" -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF -DGGML_NATIVE=ON \
    -DWHISPER_BUILD_TESTS=OFF -DWHISPER_BUILD_EXAMPLES=ON -DWHISPER_BUILD_SERVER=ON -DWHISPER_SDL2=OFF -DWHISPER_CURL=OFF
  cmake --build "$src/build" -j "$JOBS" --target whisper-server
  rm -rf "$WHISPER_PREFIX"
  install -D -m 0755 "$src/build/bin/whisper-server" "$WHISPER_PREFIX/bin/whisper-server"
  echo "$want" > "$WHISPER_PREFIX/.sizeup-build"
fi
ln -sf "$WHISPER_PREFIX/bin/whisper-server" /usr/local/bin/whisper-server

# -- whisper model ---------------------------------------------------------------------------------
step "whisper model $SIZEUP_MODEL"
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

# -- the sizeup code -------------------------------------------------------------------------------
step "sizeup code ($SIZEUP_SOURCE)"
if [ "$SIZEUP_SOURCE" = dir ]; then
  [ -n "$OPT_FROM_DIR" ] || die "installed from a folder: run again with --from-dir <the new copy> (or --repo/--ref for git)"
  from=$(cd "$OPT_FROM_DIR" && pwd)
  if [ ! -f "$from/pyproject.toml" ] || [ ! -d "$from/sizeup" ]; then die "--from-dir $from: not a sizeup checkout"; fi
  # .git stays behind (a CI checkout's credentials live in it); so do local state and models.
  rsync -a --delete --exclude /.git --exclude /.venv --exclude /.claude --exclude /state --exclude /captures \
    --exclude /models --exclude /config.toml --exclude /build --exclude '__pycache__' --exclude '*.egg-info' \
    "$from/" "$APP/"
  chown -R "$SVC_USER:$SVC_USER" "$APP"
  say "code: copied from $from"
elif [ -d "$APP/.git" ]; then
  if [ -n "$OPT_REF$OPT_REPO" ] && [ "$UPDATE" -eq 0 ]; then
    app_git remote set-url origin "$SIZEUP_REPO"
    checkout_ref                              # a new --ref / --repo: switch to it now
  elif [ "$UPDATE" -eq 0 ]; then
    say "code: keeping $(app_git log -1 --format='%h %s' 2>/dev/null || echo '?') (sizeup-station update fetches $SIZEUP_REF)"
  fi
else
  if [ -n "$(ls -A "$APP" 2>/dev/null)" ]; then     # a folder copy before (--from-dir): make room for the clone
    rm -rf "$APP"
    install -d -m 0755 -o "$SVC_USER" -g "$SVC_USER" "$APP"
  fi
  if [[ "$SIZEUP_REPO" == git@* || "$SIZEUP_REPO" == ssh://* ]]; then
    key="$STATE/.ssh/id_ed25519"
    if [ ! -f "$key" ]; then
      as_user ssh-keygen -q -t ed25519 -N "" -C "sizeup-station@$(hostname)" -f "$key"
    fi
    grep -qF "$GITHUB_HOSTKEY" "$STATE/.ssh/known_hosts" 2>/dev/null || echo "$GITHUB_HOSTKEY" >> "$STATE/.ssh/known_hosts"
    chown "$SVC_USER:$SVC_USER" "$STATE/.ssh/known_hosts"
    until err=$(as_user env GIT_SSH_COMMAND="$(git_ssh)" git ls-remote "$SIZEUP_REPO" HEAD 2>&1 >/dev/null); do
      echo
      echo "GitHub said: $(printf '%s\n' "$err" | grep -v '^$' | tail -n 1)"
      if printf '%s' "$err" | grep -qiE 'resolve|timed out|unreachable|refused|no route'; then
        echo "That's the network, not the key: check this machine's internet connection."
      fi
      echo "This station reads the code with its own read-only deploy key. Add this PUBLIC key on GitHub:"
      echo "  GitHub > actualsecurity/sizeup > Settings > Deploy keys > Add deploy key"
      echo "  Title: sizeup station $(hostname)    Key:"
      echo
      cat "$key.pub"
      echo
      echo "  Leave \"Allow write access\" OFF."
      interactive || die "no access to $SIZEUP_REPO yet: add the key above, then run this again"
      read -r -p "Press Enter once it's added (Ctrl-C to stop; run this again later to continue)... " _
    done
    say "code: deploy key accepted"
  fi
  as_user env GIT_SSH_COMMAND="$(git_ssh)" git clone -q "$SIZEUP_REPO" "$APP"
  checkout_ref
fi
BUILD=$(app_git rev-parse --short HEAD 2>/dev/null || echo "")
[ -f "$APP/deploy/station/config.station.toml" ] || die "$APP has no deploy/station/config.station.toml (ref too old?)"
STATION_SRC="$APP/deploy/station"

# -- python ----------------------------------------------------------------------------------------
step "python venv"
if ! "$VENV/bin/python" -c 'import sys; assert sys.version_info >= (3, 11)' 2>/dev/null; then
  rm -rf "$VENV"
  python3 -m venv "$VENV"
fi
# Installed from a copy (no build/ or egg-info left in the checkout); the service runs the checkout
# itself (PYTHONPATH=$APP) so data/ is found next to the package, like the hub image.
tmp=$(mktemp -d)
cp -a "$APP/pyproject.toml" "$APP/sizeup" "$tmp/"
"$VENV/bin/pip" install -q --disable-pip-version-check --no-cache-dir --upgrade "$tmp"
rm -rf "$tmp"

# -- trunk-recorder's working directory ------------------------------------------------------------
step "trunk-recorder config ($TR_DIR)"
install -m 0644 -o "$SVC_USER" -g "$SVC_USER" "$APP/deploy/trunk-recorder/talkgroups.csv" "$TR_DIR/talkgroups.csv"
install -m 0755 -o "$SVC_USER" -g "$SVC_USER" "$APP/deploy/trunk-recorder/unit-event.sh" "$TR_DIR/unit-event.sh"
# Made from the repo's config.json each run (change it there): capture paths absolute, under $STATE.
python3 - "$APP/deploy/trunk-recorder/config.json" "$TR_DIR/config.json" "$STATE/captures" <<'PY'
import json, sys
src, dst, captures = sys.argv[1:4]
c = json.load(open(src))
c["captureDir"] = f"{captures}/p25"
c["tempDir"] = f"{captures}/p25-tmp"
for s in c.get("systems", []):
    s["talkgroupsFile"] = "talkgroups.csv"
    s["unitScript"] = "./unit-event.sh"
json.dump(c, open(dst, "w"), indent=2)
PY
chown "$SVC_USER:$SVC_USER" "$TR_DIR/config.json"
install -d -m 0750 -o "$SVC_USER" -g "$SVC_USER" "$STATE/captures/p25" "$STATE/captures/p25-tmp" "$STATE/captures/clips"

# -- config ----------------------------------------------------------------------------------------
step "config ($CONF)"
install -m 0640 -o root -g "$SVC_USER" "$STATION_SRC/config.station.toml" "$CONF.dist"
if [ ! -f "$CONF" ]; then
  install -m 0640 -o root -g "$SVC_USER" "$STATION_SRC/config.station.toml" "$CONF"
  say "config: new $CONF from the template"
else
  cmp -s "$CONF" "$CONF.dist" || say "config: keeping your $CONF (the current template is $CONF.dist)"
fi
cat > "$ETC/build.env" <<EOF
SIZEUP_BUILD=$BUILD
EOF
chmod 0644 "$ETC/build.env"
run_py() {
  as_user env -C "$APP" PYTHONPATH="$APP" SIZEUP_STATE_DIR="$STATE" HOME="$STATE" \
    LD_LIBRARY_PATH="$RTLSDR_PREFIX/lib" "$VENV/bin/python" "$@"
}
run_py -c "from sizeup.config import load; c = load('$CONF'); print('config: OK, role', c.cloud_role or 'all-in-one', '->', c.cloud_hub_url)" \
  || die "$CONF doesn't load (see above)"
PORT=$(run_py -c "from sizeup.config import load; print(load('$CONF').port)")

# -- systemd, CLI, health check, journald ----------------------------------------------------------
step "services"
install -m 0755 "$STATION_SRC/sizeup-station" /usr/local/bin/sizeup-station
install -D -m 0755 "$STATION_SRC/health-check.sh" /usr/local/lib/sizeup/health-check
install -m 0644 "$STATION_SRC/systemd/sizeup-station.service" /etc/systemd/system/sizeup-station.service
install -m 0644 "$STATION_SRC/systemd/sizeup-health.service" /etc/systemd/system/sizeup-health.service
install -m 0644 "$STATION_SRC/systemd/sizeup-health.timer" /etc/systemd/system/sizeup-health.timer
install -D -m 0644 "$STATION_SRC/systemd/journald-sizeup.conf" /etc/systemd/journald.conf.d/sizeup.conf
install -d -m 2755 -o root -g systemd-journal /var/log/journal 2>/dev/null || install -d -m 2755 /var/log/journal
if command -v systemd-analyze >/dev/null; then
  systemd-analyze verify /etc/systemd/system/sizeup-station.service /etc/systemd/system/sizeup-health.service \
    /etc/systemd/system/sizeup-health.timer || warn "systemd-analyze verify complained (above)"
fi

ask_secrets 0

# A volk profile makes GNU Radio (trunk-recorder) use this CPU's fastest kernels: once, a few minutes.
if [ "$CI_MODE" -eq 0 ] && command -v volk_profile >/dev/null && [ ! -f "$STATE/.volk/volk_config" ]; then
  step "volk_profile (once, a few minutes)"
  as_user env HOME="$STATE" volk_profile >/dev/null 2>&1 || warn "volk_profile failed (trunk-recorder still works, a bit slower)"
fi

if [ "$CI_MODE" -eq 1 ]; then
  say "CI: not enabling or starting services"
else
  step "start"
  systemctl daemon-reload
  systemctl restart systemd-journald || true
  if ! systemctl is-active --quiet sizeup-station; then
    run_py -m sizeup --config "$CONF" --init-state || die "sizeup --init-state failed"
  fi
  systemctl enable sizeup-station.service sizeup-health.timer
  systemctl restart sizeup-station.service
  systemctl restart sizeup-health.timer
  for _ in $(seq 1 30); do
    curl -fsS --max-time 3 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && break
    sleep 2
  done
  if curl -fsS --max-time 3 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then
    say "sizeup-station is up: http://localhost:$PORT (diagnostics) · status: sizeup-station status · checks: sizeup-station doctor"
  else
    warn "sizeup-station hasn't answered yet: sizeup-station logs"
  fi
fi
STEP=""
say "done ($SIZEUP_REF $BUILD, model $SIZEUP_MODEL)"
