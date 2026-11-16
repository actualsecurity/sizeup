# The station

The station is the computer with the RTL-SDR dongles, at the South End firehouse (51-53 Grace Church St). It runs
sizeup with `[cloud] role = "station"`:

| It runs | With |
|---|---|
| South 2 (paging), FG1, FG2 | `rtl_fm`, one dongle each: two-tone paging, MDC-1200 / FleetSync IDs |
| The county P25 system (Fire/EMS 15) | trunk-recorder, a service of its own |
| Another P25 system (MTA MRRS), optional | a second trunk-recorder, on the dongles a person gives it |
| HPN Tower, NY Approach, HPN Ground (airband AM), optional | `rtl_fm`, or `rtl_sdr` with several frequencies on one dongle |
| Our ADS-B and AIS receivers, optional | readsb and AIS-catcher, each on the one dongle named for it |
| Speech-to-text for what the hub doesn't transcribe | whisper.cpp (`whisper-server`), a service of its own |
| The link to the hub | outbound HTTPS, through an outbox on disk |

The link **calls home**: everything the station hears goes to the hub (https://sizeup.actualsecurity.org) over
outbound HTTPS, through an outbox that keeps it all while the hub or the internet is down. No port
forwarding, no inbound firewall rule. The hub is the CAD of record and says the dispatches (Amazon
Polly); the main channels (South 2, FG1, FG2, Fire/EMS 15) can be transcribed there with Amazon
Transcribe (Settings > Radios, synced to the station). The station's own page
(http://localhost:8091, [below](#the-station-page)) is about the station only: its radios, what it
heard and the hub link. It has no CAD. Architecture and failure behaviour: [docs/cloud.md](../../docs/cloud.md).

Three ways to run it:

* **Windows 10/11** (the station PC today): WSL2 + Ubuntu 24.04, set up by `install-windows.ps1`.
* **Raspberry Pi / Debian / Ubuntu**: `install-linux.sh` directly.
* **A Mac**: sizeup from a checkout, secrets in the Keychain ([below](#on-a-mac)).

## Contents

| | |
|---|---|
| Install | [Quick start](#quick-start) · [Where the station is](#where-the-station-is) · [Windows](#windows-quick-start) · [Raspberry Pi / Linux](#raspberry-pi--linux-quick-start) · [Where things are](#where-things-are) · [On a Mac](#on-a-mac) |
| Operate | [The `sizeup-station` command](#the-sizeup-station-command) · [Updating](#updating) · [The station page](#the-station-page) · [Station logs](#station-logs) · [Switching over from the Mac](#switching-over-from-the-mac) |
| Radios | [Dongles get a job by themselves](#dongles-get-a-job-by-themselves) · [trunk-recorder](#trunk-recorder-its-own-service) · [More P25 systems](#more-p25-systems-mta-mrrs) · [Squelch](#squelch) · [ADS-B receiver](#ads-b-receiver) · [AIS receiver](#ais-receiver) · [HPN ATC](#hpn-atc-the-airband-dongle) · [NY Approach and HPN Ground](#ny-approach-and-hpn-ground) · [Two dongles, one serial](#two-dongles-one-serial) |
| Other parts | [Incident-source adapter](#incident-source-adapter-station-only) · [Speech-to-text and CPU](#speech-to-text-and-cpu) · [The voice](#the-voice) |
| When it breaks | [It comes back by itself](#when-things-go-wrong-it-comes-back-by-itself) · [Troubleshooting](#troubleshooting) |

## Quick start

| Step | Windows 10/11 | Raspberry Pi / Debian / Ubuntu |
|---|---|---|
| 1. Copy this `deploy/station` folder to the machine | e.g. `C:\sizeup-station` | anywhere |
| 2. Install | elevated PowerShell: `powershell -ExecutionPolicy Bypass -File .\install-windows.ps1` | `sudo ./install-linux.sh` |
| 3. Add the deploy key it prints | GitHub > the repo > Settings > Deploy keys, write access off | same |
| 4. Enter the secrets when asked (hidden) | station token, Access Client ID and Secret | same |
| 5. Plug in the dongles | attached to WSL within ~10 s; each gets a job by itself | each gets a job by itself |
| 6. Check (with no dongle plugged in, `doctor` says FAIL "no RTL-SDR on USB") | `wsl -d Ubuntu-24.04 -u root sizeup-station doctor` | `sudo sizeup-station doctor` |
| 7. Look | http://localhost:8091 | http://localhost:8091 |

After that it runs by itself: it starts at boot, restarts itself, and follows the hub's build
([Updating](#updating)). The three commands to remember:

```sh
sizeup-station status       # is it up, the dongles, the hub link
sudo sizeup-station doctor  # every check, with what to do about each failure
sizeup-station logs -f      # the journal
```

Something wrong: [Troubleshooting](#troubleshooting).

## Where the station is

The station PC is at the **South End firehouse** (51-53 Grace Church St), not Headquarters (#271). `[station]` in
`/etc/sizeup/config.toml`:

* `id = "pcfd-hq"`: the hub keys the station's token, history and outbox on it. It stays `pcfd-hq` (an old name, kept
  as a key); the pages show the name instead.
* `name = "Port Chester FD South End"`: Settings, the station page's header, the hub's Health and the alerts
  ("Station update failed · Port Chester FD South End").
* `lat` / `lon` (default 40.99600, -73.66453, the South End): where the antennas are, our ADS-B receiver's position
  (readsb `--lat/--lon`, [below](#ads-b-receiver)). Leave them out for the default.

What stays Headquarters (209 Westchester Ave) is what is about Headquarters: Tapout on its phone line, `[geo] hq_lat /
hq_lon` (where the intersection search's "nearest" is measured from) and the "headquarters" radio phrases.

**An installed station** is renamed once by the update to this release (`sizeup/cloud/configupdate.py` "south-end",
the old file kept as `config.toml.before-south-end`): `name = "Port Chester FD Headquarters"` becomes `name = "Port
Chester FD South End"`, only when it is still exactly that (a name set by hand is left alone). It is not deferred:
every release reads `[station] name`, so a switch back is safe. The install log says `config: south-end: [station]
name is now "Port Chester FD South End" ...`; the station sends the new name to the hub with its next status, once
it has restarted on the new release. `lat` / `lon` are not added: the code's default is the South End.

## Windows quick start

On the station PC (Windows 11, or Windows 10 version 2004 / build 19041 or later: the installer refuses older),
signed in with the administrator account that will own the station:

1. Copy this `deploy\station` folder to the PC (USB stick, or the repo's zip), e.g. to `C:\sizeup-station`.
2. Start **PowerShell as administrator** (right-click > Run as administrator) and run:

   ```powershell
   cd C:\sizeup-station
   powershell -ExecutionPolicy Bypass -File .\install-windows.ps1
   ```

3. If it says a restart is needed, press Enter. Sign in again as the same user: the install carries on
   by itself in a PowerShell window.
4. When asked, type that Windows account's password (Task Scheduler keeps it so the station starts
   even when nobody is signed in; Cancel = only while signed in).
5. It builds everything inside WSL (20-40 minutes the first time on the i5-6400T). Then it shows a
   **deploy key**: on GitHub open actualsecurity/sizeup > Settings > Deploy keys > Add deploy key,
   paste it, title "sizeup station", leave "Allow write access" **off**, Add. Back in the window,
   press Enter.
6. Enter the **station token** (the hub's `SIZEUP_STATION_TOKEN`), then the Cloudflare Access service
   token's Client ID and Client Secret. Typing is hidden; nothing is shown or logged.
7. It finishes with `sizeup-station doctor`. Plug in the dongles (any time): they are attached to WSL
   within about 10 s, and each one gets a job by itself (see "Dongles get a job by themselves"
   below). Open http://localhost:8091 (the station page, Radios) to see or change them, or do it
   from the hub's monitor.

Then once, by hand: in the BIOS/UEFI set **Restore on AC power loss** (or "After power failure") to
**Power On**, so the PC boots by itself after a power cut.

What `install-windows.ps1` does (all of it again, safely, on every run):

* turns on WSL and Virtual Machine Platform (DISM), `wsl --update`, WSL 2 by default;
* installs Ubuntu-24.04 (`wsl --install --no-launch`, else Ubuntu's own WSL image, checksum checked)
  and sets `/etc/wsl.conf` (`[boot] systemd=true`, default user root);
* writes `%UserProfile%\.wslconfig`: `memory=8GB`, `processors=` all, `vmIdleTimeout=-1` (the VM is
  never stopped for being idle);
* installs usbipd-win (winget, else the pinned MSI 5.3.0, checksum checked) and shares (`usbipd bind`) every
  RTL-SDR plugged in. **Only USB ids `0bda:2838` and `0bda:2832` are shared into WSL**: no other USB device is
  touched, and a dongle with another id is not picked up. No Zadig, no driver change;
* power: no sleep or hibernate on AC, hibernation off (that also turns off Fast Startup, so a power-on
  is a real boot and the startup tasks run), USB selective suspend off;
* scheduled tasks, as you, at startup + at sign-in + every 5 minutes, restart on failure every minute,
  no time limit, never stopped on battery:
  * **sizeup-wsl** (`C:\ProgramData\sizeup\wsl-keepalive.ps1`) keeps the distro running. WSL stops a
    distro when no `wsl.exe` session is attached to it, whatever systemd runs inside; one
    `sleep infinity` session keeps it (and systemd, and the station) up. If that session ends (a WSL
    update, `wsl --shutdown`, a VM crash) the loop starts it again within 10 s; if the loop dies, the
    task brings it back. Log: `C:\ProgramData\sizeup\wsl.log`.
  * **sizeup-usb** (`C:\ProgramData\sizeup\usb-attach.ps1`): every 10 s it reads `usbipd list` and, for each
    RTL-SDR (the two USB ids above) that isn't attached to WSL, shares it if needed (`usbipd bind`) and attaches it
    (`usbipd attach --wsl`). One copy runs at a time. Log: `C:\ProgramData\sizeup\usb.log` (only changes: `bind`,
    `attached ... to WSL`, `attach ... failed`, `unplugged`; 1 MB, one old file kept).
* runs `install-linux.sh` inside the distro (below).

Options: `-Model large-v3-turbo-q5_0|small.en-q5_1|base.en` (default `small.en-q5_1`), `-Ref <branch or tag>`
(default `main`), `-Distro` (default `Ubuntu-24.04`), `-MemoryGB 8`, `-Processors 4` (default: all),
`-NoPassword` (tasks without a stored password). Log: `C:\ProgramData\sizeup\install.log`.
`uninstall-windows.ps1` removes the tasks and their scripts (`-Unshare` also gives the dongles back
to Windows); WSL and the station inside it stay (`wsl --unregister Ubuntu-24.04` deletes them for good).

**Starting before anyone signs in.** With the password stored, the tasks start at boot. If on this
PC the station only comes up once someone signs in (after a restart, `C:\ProgramData\sizeup\wsl.log`
gets no "keepalive: starting" line until then), turn on automatic sign-in for that account
(Sysinternals Autologon, or `netplwiz` > untick "Users must enter a user name and password"), and
lock the screen with a password screensaver if the PC is in a public room.

Commands from Windows (any PowerShell): `wsl -d Ubuntu-24.04 -u root sizeup-station status` (or
`doctor`, `logs`, `update`, `secrets`, `restart`: [all of them](#the-sizeup-station-command)). `usbipd list` shows
the dongles and their state:

| `usbipd list` STATE | Meaning |
|---|---|
| `Not shared` | Windows has it. sizeup-usb shares it at its next pass. |
| `Shared` | Offered to WSL, not attached yet. sizeup-usb attaches it at its next pass. |
| `Attached` | In WSL: `lsusb` there lists it, and sizeup can open it. |

## Raspberry Pi / Linux quick start

Raspberry Pi OS (64-bit, Bookworm), Debian 12 or Ubuntu 24.04, with systemd. Copy this folder over,
then:

```sh
sudo ./install-linux.sh             # deploy key + clone, builds, asks for the secrets
sudo sizeup-station doctor
```

On a Pi 4/5 (4 GB or more) use `--model base.en`, keep the main channels on AWS and turn off
transcription for the rest. Building trunk-recorder takes a while there; the installer limits
parallel jobs to the memory it has.

`install-linux.sh` options: `--ref REF`, `--repo URL`, `--from-dir DIR` (a copy of the repo instead
of the deploy key and a clone), `--model NAME`, `--update`, `--secrets`, `--rebuild`, `--ci` (CI:
nothing enabled or started, no prompts); `--stage SHA` and `--activate NAME` are the auto-updater's
([Updating](#updating)). Running it again at any time is the repair. Log: `/var/log/sizeup-install.log`.

## Where things are

What `install-linux.sh` installs (Linux, and WSL on the Windows PC):

| What | Where |
|---|---|
| the repo, a bare clone (root's), fetched with the station's read-only deploy key (`/etc/sizeup/deploy_key`, root only) | `/opt/sizeup/repo.git` |
| one release per commit: the code (`git archive`, root-owned: the service user only reads it), its own venv, `BUILD` (the sha), `BUILDS` (the builds it runs with) | `/opt/sizeup/releases/<sha>` |
| the release that runs (a link, swapped by rename) | `/opt/sizeup/current` |
| rtl-sdr-blog v1.3.6 (RTL-SDR Blog V4 support): rtl_fm, rtl_test, rtl_eeprom, librtlsdr | `/opt/sizeup/rtl-sdr` -> `builds/rtl-sdr-blog-<tag>-<commit>` |
| trunk-recorder v5.2.0 with its plugins (unit_script) | `/opt/sizeup/trunk-recorder` -> `builds/trunk-recorder-<tag>-<commit>` |
| whisper.cpp v1.9.4 whisper-server, built for this CPU (AVX2 on the i5-6400T) | `/opt/sizeup/whisper` -> `builds/whisper.cpp-<tag>-<commit>-native` |
| readsb v3.16.21 (wiedehopf), RTL-SDR with the bias-tee switch, against rtl-sdr-blog's librtlsdr: the ADS-B decoder (#235) | `/opt/sizeup/readsb` -> `builds/readsb-<tag>-<commit>` |
| whisper model, SHA-256 checked; `whisper.bin` points at the one in use | `/opt/sizeup/models` |
| config, from `config.station.toml` once and never overwritten (`config.toml.dist` = the current template) | `/etc/sizeup/config.toml` |
| secrets `SIZEUP_STATION_TOKEN`, `SIZEUP_ACCESS_CLIENT_ID`, `SIZEUP_ACCESS_CLIENT_SECRET`, and an incident-source adapter's (root 0600) | `/etc/sizeup/station.env` |
| what `update` fetches (repo, ref), the model, auto-update on / off | `/etc/sizeup/install.conf` |
| state: CAD, outbox, `cloud.json`, captures, trunk-recorder's working dir | `/var/lib/sizeup` |
| the services ([the table below](#the-services)); journald: kept across reboots, 200 MB at most, 30 days at most | `/etc/systemd/system/sizeup-*`, `/etc/systemd/journald.conf.d/sizeup.conf` |
| the journal reader's user `sizeup-logs` and its hand-off directory ([Station logs](#station-logs)) | `/var/lib/sizeup-logs` |
| the dongles: udev rule (group `plugdev`, which the `sizeup` user is in), the DVB-T drivers blacklisted, rtl-sdr-blog's librtlsdr first, no usbfs memory limit | `/etc/udev/rules.d/60-sizeup-rtlsdr.rules`, `/etc/modprobe.d/sizeup-rtlsdr-blacklist.conf`, `/etc/ld.so.conf.d/00-sizeup-rtlsdr.conf`, `/etc/modprobe.d/sizeup-usbfs.conf` |
| the auto-updater's record (state, last result, failed builds) and its one-line-per-update log | `/var/lib/sizeup-update/state.json`, `/var/log/sizeup-update.log` |
| the `sizeup-station` command, and links to `rtl_fm`, `rtl_test`, `rtl_eeprom` and `whisper-server` | `/usr/local/bin` |
| the launchers (`start-station`, `start-whisper`, `health-check`) and the scripts the system python3 runs (`autoupdate.py`, `trunkrecd.py`, `adsbd.py`, `aisd.py`, `logread.py`) | `/usr/local/lib/sizeup` |

Not installed by it: AIS-catcher ([AIS receiver](#ais-receiver)).

### The services

| Unit | Runs as | What |
|---|---|---|
| `sizeup-station.service` | `sizeup` | sizeup itself: the FM and airband receivers (`rtl_fm` is its child), the link to the hub, the station page. `Restart=always`. |
| `sizeup-whisper.service` | `sizeup` | `whisper-server` with the `[stt]` settings. Restarted by an update only when its program, model or flags change. |
| `sizeup-trunk-recorder.service` | `sizeup` | trunk-recorder on the dongle sizeup gives Fire/EMS 15, and one more per other P25 system ([below](#trunk-recorder-its-own-service)). |
| `sizeup-adsb.service` | `sizeup` | readsb, only on the dongle named for ADS-B. Idle until one is named ([below](#ads-b-receiver)). |
| `sizeup-ais.service` | `sizeup` | AIS-catcher, only on the dongle `[vessels.receiver]` names. Idle until then ([below](#ais-receiver)). |
| `sizeup-logread.service` | `sizeup-logs` | Reads the journal's `sizeup-*` entries for the log shipping ([below](#station-logs)). |
| `sizeup-usbfs.service` | root | At boot: no usbfs memory limit (`usbfs_memory_mb` = 0, #269). |
| `sizeup-health.timer` / `.service` | root | Every minute: `/healthz`; 3 failures in a row restart sizeup-station ([below](#health-checks)). |
| `sizeup-update.path` / `.timer` / `.service` | root | The auto-updater ([Updating](#updating)). |

The source builds are pinned to a tag **and** its commit (a moved tag is refused), built once per
pin into their own directory under `/opt/sizeup/builds`, and rebuilt only with `--rebuild`. trunk-recorder's `config.json` in
`/var/lib/sizeup/trunk-recorder` is made from the repo's `deploy/trunk-recorder/config.json` on every
run: change it there. The whisper models come from huggingface.co/ggerganov/whisper.cpp at a fixed
revision and are checked against the SHA-256 that revision lists for them. On Ubuntu 24.04,
trunk-recorder uses the distribution's librtlsdr 2.0.1 (it knows the V4 too); rtl_fm and sizeup's
dongle lookup use the rtl-sdr-blog build.

## The `sizeup-station` command

Installed as `/usr/local/bin/sizeup-station`. A command that needs root runs itself again with `sudo`. With no
argument it is `status`. Everything it has:

| Command | Root | What |
|---|---|---|
| `status` | no | The services, the build and the last update, the web page, the hub link, each radio, the dongles on USB, CPU load, memory, free disk. |
| `logs [journalctl options]` | no | The journal of `sizeup-station`, `sizeup-health`, `sizeup-whisper`, `sizeup-trunk-recorder`, `sizeup-adsb` and `sizeup-update`. No options: the last 200 lines. `-f` follows. |
| `restart`, `start`, `stop` | yes | sizeup-station. `restart` leaves trunk-recorder running and restarts whisper-server only if `[stt]` changed; `stop` stops trunk-recorder too; `start` starts both. |
| `update [--ref REF] [--model NAME]` | yes | The tip of the configured ref now, side by side ([Updating](#updating)). Options pass to `install-linux.sh --update`. Refused while an automatic update runs. |
| `autoupdate on` \| `off` \| `status` \| `now` | on, off, now | Follow the hub's build (on by default), stay put, show this build, the hub's and the last 5 update lines, or look now. |
| `rollback [--force]` | yes | Back to the release that ran before: a rename and a restart. |
| `secrets` | yes | Enter the station token and the Access service token again, then restart. |
| `adapter setup` \| `status` \| `remove` \| `reset [source]` | yes | The private incident-source adapter ([below](#incident-source-adapter-station-only)). |
| `doctor` | yes | Every check below, `ok` / `FAIL` / `note` per line, and what to do about each failure. Exit code 0 only with no FAIL. |
| `sdr-serial <index\|serial> <new-serial> [--dry-run] [--yes]` | yes | Give one dongle a new, unique serial ([Two dongles, one serial](#two-dongles-one-serial)). |
| `stt [--hours H] [--bench clip.wav]` | no | Why speech-to-text is behind ([Speech-to-text and CPU](#speech-to-text-and-cpu)). Read-only. |
| `version` | no | The release that runs, its build and commit, and `/etc/sizeup/install.conf`. |
| `help` | no | The list. |

It never prints a secret: the hub check goes through the station itself (`POST /api/station-mode/test`).

**What `status` shows**, one line each: `service` (state, since when, enabled at boot), `health` (the timer and
its last check), `whisper`, `p25` (the trunk-recorder service: its state, the dongle, trunk-recorder's pid), `adsb`
(the decoder's state, `usbfs_memory_mb`), the version lines and `updater`, `web`, `hub` (the link's state, the hub
URL, forwarding on or off), `radios` (one row per channel), `dongles on USB` (`lsusb`, ids `0bda:2838` / `0bda:2832`)
and what librtlsdr sees (index, serial, product; a shared serial is marked), `CPU`, `disk`. Run it with `sudo` for
the librtlsdr list.

**What `doctor` checks:**

| Area | Checks |
|---|---|
| systemd | It runs. `sizeup-station` runs and is enabled at boot. `sizeup-health.timer` is on. `sizeup-whisper` and `sizeup-trunk-recorder` run. |
| ADS-B | `sizeup-adsb` runs, readsb is installed, `usbfs_memory_mb` is 0. |
| Updates | `sizeup-update.path` and `.timer` are active, plus the updater's own checks: the last update didn't fail, the adapter, the Windows scripts are current. |
| Web | `/healthz` answers on the `[web]` port. |
| Dongles | At least one RTL-SDR on USB (in WSL, a failure points at `usbipd list` and `usb.log`). What librtlsdr sees. Two dongles on one serial: FAIL. |
| Programs | `rtl_fm`, `rtl_sdr`, trunk-recorder (`--version`) and its `unit_script` plugin, `whisper-server`, the whisper model, and whisper-server answering on the `[stt]` port. |
| Secrets | `/etc/sizeup/station.env` is root 0600; which of the three names are set (never a value). |
| Hub | Its `/healthz` is reachable; the hub takes the token (the station's own test); the link's state. |
| Machine | The clock is NTP-synchronized (a note if not). At least 5 GB free under `/var/lib/sizeup`. |

Not in `status`, `logs` or `doctor`: the AIS decoder and the journal reader. Use `systemctl status sizeup-ais
sizeup-logread`, `journalctl -u sizeup-ais`, `journalctl -u sizeup-logread`, and
`python3 /usr/local/lib/sizeup/aisd.py status`.

### Health checks

| Check | How often | What it does |
|---|---|---|
| systemd `Restart=always` | on exit | sizeup-station is started again after 5 s, with no start limit. The same for whisper, trunk-recorder and the ADS-B and AIS decoders (5 s) and the journal reader (10 s). |
| `sizeup-health.timer` (`/usr/local/lib/sizeup/health-check`) | every minute, from 2 min after boot | `GET /healthz` on the `[web]` port, 10 s timeout. 3 failures in a row restart sizeup-station: this catches a process that runs but no longer answers. Skipped for 120 s after a (re)start. A service stopped or disabled on purpose is left alone. |
| The updater's check | after each switch | `/healthz` must answer with the new build within 90 s, or it switches back ([Updating](#updating)). |
| The hub's view | continuous | The hub raises the alerts (station offline, a channel stuck, the P25 system silent, two dongles on one serial ...): Settings > Station > Health there ([docs/cloud.md](../../docs/cloud.md#station-health-and-alerts)). |

### The config file

Updates come by themselves ([below](#updating)). `/etc/sizeup/config.toml` is never overwritten: compare it
with `/etc/sizeup/config.toml.dist` after an update. The exceptions are the additions a release makes once
(`sizeup/cloud/configupdate.py`). Each keeps the old file beside it and is noted in `/etc/sizeup/config-updates`,
so something taken out by hand stays out. The install log says what it added.

| Addition | Old file kept as | What |
|---|---|---|
| `fg2` (#119) | `config.toml.before-fg2` | The FG2 channel is appended. |
| `tones-47` (#47) | `config.toml.before-tones-47` | A `[[tones]]` entry still on the unverified Port Chester pair 349.1/311.1 becomes 903.2/810.2, and Rye Brook FD 767.4/928.1 (ours) is added after it. Tones set by hand are left alone. |
| `hpn-atc` (#233) | `config.toml.before-hpn-atc` | The HPN Tower channel ([below](#hpn-atc-the-airband-dongle)). |
| `approach-ground` (#260), `airband-sets` (#275) | `config.toml.before-approach-ground`, `config.toml.before-airband-sets` | NY Approach and HPN Ground ([below](#ny-approach-and-hpn-ground)). |
| `south-end` (#271) | `config.toml.before-south-end` | The station's name ([above](#where-the-station-is)). |

Another model: `sudo sizeup-station update --model base.en`.

## Updating

**The station runs exactly the commit the hub runs, and follows it by itself.** Nothing to do after
a merge to main: CI deploys the hub, and about a minute later the station runs the same build.

How it goes, end to end:

1. The hub knows its build: CI builds its image with `SIZEUP_BUILD=<the full git sha>`
   (`.github/workflows/deploy.yml`), and every answer to the station's state post (every 5 s or
   sooner) carries it. No new connection, nothing inbound, no polling GitHub.
2. Once the hub has said the same build for 30 s (a rolling deploy answers with both for a while),
   the station writes it to `/run/sizeup/update-wanted`: the only thing the sizeup user does for an
   update. An unknown or empty build: nothing.
3. `sizeup-update.path` sees the file change and starts `sizeup-update.service`: root, outside the
   sizeup process (restarting sizeup can't kill it half-way), at low CPU priority.
4. It fetches into `/opt/sizeup/repo.git`. The commit must be on `origin/main` (`git merge-base
   --is-ancestor`): never anything ahead of or beside what the hub can run.
5. **Side by side, nothing in place.** That commit's own `install-linux.sh --stage <sha>` builds
   `/opt/sizeup/releases/<sha>`: the code from git, the running release's venv as hard links (a
   second) unless `pyproject.toml` changed (then a new venv), bytecode compiled. trunk-recorder,
   whisper.cpp or rtl-sdr are built (into a new `builds/` directory) only if the release changes
   their pin; apt runs only if a package is missing. The running station is not touched.
6. **Preflight**, before anything switches: the new code imports, `/etc/sizeup/config.toml` loads
   with it, `sizeup --init-state` brings the databases up in a scratch directory, its systemd units
   parse, rtl_fm / trunk-recorder `--version` / whisper-server / readsb run, and the scripts run with
   the system python3 (adsbd.py, aisd.py, trunkrecd.py, autoupdate.py, configupdate.py, logread.py and
   tools/stt_diag.py: stdlib only, Python 3.11+) load with it. A missing `rtl_sdr` is only a warning. A failure: no switch, the sha is recorded as failed and reported to the hub, with the
   error (for a script, the system Python's version and its exception).
7. **A quiet moment**: no carrier open, nobody talking, no transcription queued or running, no re-run
   pending (the station's `GET /api/station-mode`, `update.busy`). It looks every 5 s and waits up to 5 minutes
   for one, then goes anyway. Shed clips being caught up ([below](#speech-to-text-and-cpu)) never count as busy:
   the ones still waiting are taken back while an update waits.
8. **The switch**: `install-linux.sh --activate <sha>` swaps the build links and `/opt/sizeup/current`
   (each one rename), installs the release's units and commands, and restarts sizeup-station once.
   whisper-server is its own service now (`sizeup-whisper.service`) and keeps its model loaded; it is
   restarted only when its program, model or `[stt]` flags change. trunk-recorder is too
   (`sizeup-trunk-recorder.service`, #71): restarted only when its build, its `config.json` or
   `talkgroups.csv`, its supervisor or its unit change, so a Python-only update leaves Fire/EMS 15 on
   its control channel (the same trunk-recorder process before and after). The ADS-B decoder, the AIS decoder
   and the journal reader are left running the same way, each unless its own script or unit changed.
9. `/healthz` must answer **with the new build** within 90 s. Otherwise the same swap back to the
   release that ran before (a rename and a restart: nothing rebuilt), and that sha is not tried again
   until the hub moves on to another one.
10. The last 3 releases are kept (the current one and the two before it), older ones and builds no
    kept release uses are deleted.

**No restart when nothing the station runs changed.** If the new commit only touches `docs/`,
`tests/`, `.github/`, the hub's own files (`deploy/hub/`, `deploy/aws/`, `Dockerfile`, `.dockerignore`),
`.gitignore`, the Windows installers or `*.md` outside `sizeup/` and `data/`, the station stays as it is and counts as up to
date ("up to date: abc1234 (no runtime change since def5678)", here and on the hub). Anything else
goes the usual way.

**A build that breaks for another reason** than its preflight (apt or dpkg busy with unattended
upgrades, the network, a full disk, a slow pin rebuild cut off) is tried again after 5, 15, 60 and
60 minutes, 5 times in all, then recorded as failed; `autoupdate status` and the hub say "not built
yet (try N of 5 ...)". A pin rebuild that was cut off keeps its build directory and carries on.
A preflight failure is not retried (the same commit would fail the same way).

**One install at a time.** The updater holds `/run/sizeup-update/lock` for a whole update; a manual
`sizeup-station update` (or `install-linux.sh`) takes the same lock, says so if an update is running,
and stops `sizeup-update.path` / `.timer` until it is done (then looks once for a hub build it missed).

Timings: a Python-only update is about 10-20 s of building beside the running station (archive, venv
links, bytecode, preflight), then the quiet-moment wait, then one restart. The radios are off the air
only during that restart: the FM channels (rtl_fm) for a few seconds. Fire/EMS 15 isn't off at all:
trunk-recorder is its own service and keeps running (#71); only an update that changes trunk-recorder's
build or config restarts it, and then P25 is back once it has locked on to the control channel again.
Each update logs those numbers (`sizeup-station autoupdate status`, `/var/log/sizeup-update.log`:
"web back N s after the switch, radios M s"). Nothing heard is lost across a restart: the outbox
(`/var/lib/sizeup/state/outbox.sqlite`) is on disk and goes up first when the station is back.

**Update logs:**

| Where | What |
|---|---|
| `/var/log/sizeup-update.log` | One line per attempt and result. `sizeup-station autoupdate status` shows the last 5. |
| `journalctl -u sizeup-update` | The whole run: the fetch, the build, the preflight, the switch. |
| `/var/lib/sizeup-update/state.json` | The updater's record (root's, world-readable): state, last result, failed builds, the adapter. |
| `/var/log/sizeup-install.log` | Everything `install-linux.sh` says: `--stage`, `--activate` and runs by hand. |

Watching it:

* `sizeup-station status` / `autoupdate status`: this station's build, the hub's, auto-update on or
  off, the last update and its result; `doctor` fails on an update that failed.
* The station page: the Hub row says "version: up to date (abc1234)", "updating ...", or "update to
  abc1234 failed (...): staying on def5678 until the hub moves on".
* The hub: its Station row says the same about the station.
* `journalctl -u sizeup-update`, and one line per attempt in `/var/log/sizeup-update.log`.

By hand:

```sh
sudo sizeup-station autoupdate off   # stay put (on: follow the hub again, now)
sudo sizeup-station autoupdate now   # look now instead of waiting for the next change / 15 min check
sudo sizeup-station rollback         # back to the release that ran before (never to one that failed);
                                     # the one left isn't tried again until the hub moves on
sudo sizeup-station update           # the tip of the ref now, side by side (also: --ref, --model)
```

`[station] auto_update = false` in `/etc/sizeup/config.toml` also keeps it put. **A station on another
`--ref` than main doesn't auto-update** (testing a branch: the hub runs main, and the station never
runs anything the hub can't); `sudo sizeup-station update --ref main` follows the hub again. Installed
with `--from-dir`: no auto-update either. If the hub can't be reached the station stays where it is;
a 15-minute timer looks again, but only ever at the build the hub said (a want older than an hour is
ignored).

Windows side: when `deploy/station/windows/*.ps1` differ from `C:\ProgramData\sizeup\*.ps1`, the
updater copies them over through `/mnt/c` (a temporary file, then a rename). If it can't, the station
page and `doctor` say "Windows scripts out of date: run install-windows.ps1". This means root in WSL
can rewrite the scripts the Windows tasks run as the administrator: acceptable, since root in WSL is
already "whoever controls main" (it runs main's installer).

What root trusts from the sizeup user, and how: the want file is read without following a link,
never waited on (not a FIFO), 64 bytes at most, and only a full sha that main has is taken; files in
`/var/lib/sizeup` (trunk-recorder's directory included) are written by the sizeup user itself, never by
root through a path it controls; the old checkout is read as a `git bundle` made by that user.

**A station installed before this** (a git checkout in `/opt/sizeup/app`, run in place) moves to
releases once, by hand: `sudo sizeup-station update`. The old command fetches main and runs the new
installer, which: makes `/opt/sizeup/repo.git` from the old checkout (no network), copies the deploy
key to `/etc/sizeup/deploy_key` (GitHub needs nothing new), moves each build under `/opt/sizeup/builds`
with a link in its place (a rename: nothing rebuilt), makes a release of the commit that was running
(with the old venv, as hard links) and one of main, preflights main, changes `[p25] talkgroups_file`
to trunk-recorder's copy, switches and restarts once. If main doesn't answer within 90 s it switches
back to the release of the old commit by itself. Safe to run again at any point. `/opt/sizeup/app`
and `/opt/sizeup/venv` stay until that old release is pruned (after two more updates), then go.

## Incident-source adapter (station only)

A private, read-only incident-source adapter (#301; the interface it implements: #297, `sizeup/sources/`) lives in a
repo of its own and is installed and run **on the station only, never on the hub**. Without one set up, nothing below
happens and updates are exactly as above.

On the station:

```sh
sudo sizeup-station adapter setup    # asks for the repo (git@github.com:OWNER/REPO.git), the branch or tag
                                     # (main), its credential variable names; prints a deploy key and the command
                                     # to add it; then each credential, hidden
sudo sizeup-station adapter status   # repo, installed commit, installed or failing, which credentials are set
sudo sizeup-station adapter remove   # key, credentials and [adapter] out; the next release runs without it
sudo sizeup-station adapter reset [source]   # a source that stopped itself (e.g. a login it won't retry): its reset()
```

**How often it is read.** The station log's start-up line says it: `incident sources: LABEL (SOURCE, rank 1, every
30 s ...)`. sizeup calls an adapter's `poll()` no more than every 30 s, except for one whose `poll()` asks its source
nothing and only reads a snapshot the adapter keeps in memory: that one is read every `poll_s` (down to 1 s), so a new
call reaches the hub that much sooner and the source sees nothing more. An adapter says so itself (`local_poll =
True`). For one that is known to be so but doesn't say it, add its source id to `/etc/sizeup/station.env` and restart
(off unless set; the line then reads `memory read every 5 s (SIZEUP_INCIDENT_SOURCES_SNAPSHOT)`):

```sh
echo 'SIZEUP_INCIDENT_SOURCES_SNAPSHOT=SOURCE' | sudo tee -a /etc/sizeup/station.env >/dev/null
sudo sizeup-station restart
```

**What setup asks**, in this order (Enter keeps what is already set):

1. The adapter's repo (`git@github.com:OWNER/REPO.git`).
2. The branch or tag (default `main`).
3. Its credential names, comma-separated. The names are the adapter's own.
4. "Read them from AWS SSM?" (default no, or yes once an SSM key is stored). On yes: the AWS region
   (default `us-east-1`), the station's access key id and its secret access key (both hidden), then whether to
   remove the copy kept here of each password that SSM now has (default yes). [More below](#a-credential-kept-in-aws-ssm).
5. Each remaining credential's value, hidden. Empty leaves it as it is.

All of that is asked before anything changes. A hub's config (`[cloud] role = "hub"`) makes setup refuse.

Setup makes `/etc/sizeup/adapter-deploy-key` (ed25519, root 0600) and prints **only its public half**, with the
command to run on your Mac, where `gh` is signed in (read-only: no `--allow-write`):

```sh
echo 'ssh-ed25519 AAAA... sizeup-adapter@station' | gh repo deploy-key add - -R OWNER/REPO --title 'sizeup station HOST'
```

The repo must be on GitHub (`git@github.com:OWNER/REPO.git`); GitHub's host key is pinned in `/etc/sizeup/known_hosts`
(`StrictHostKeyChecking=yes`).

**Credentials** go into systemd's credstore, `/etc/credstore/sizeup-adapter.<NAME>` (root 0700 / 0600, never echoed,
logged or sent to the hub), and reach the station service only as files under `$CREDENTIALS_DIRECTORY`, never in its
environment. A drop-in (`/etc/systemd/system/sizeup-station.service.d/adapter-credentials.conf`) says
`ImportCredential=sizeup-adapter.*` on systemd 254+ (Ubuntu 24.04 / WSL2: 255), so a file deleted by hand is only a
missing credential (the adapter shows failing), never a station that won't start; older systemd (Debian 12: 252) gets
one `LoadCredential=` per file there, rewritten whenever setup or remove change them, and `doctor` fails if they don't
match. sizeup takes the directory's path out of its environment at start, and every program it starts (rtl_fm,
whisper-server, trunk-recorder, Kokoro) gets an explicit environment without it (`sizeup/procenv.py`). An adapter
reads one with `sizeup.cloud.adapters.credential("NAME")`. The config gets `[adapter] repo`, `ref` and `env` (the names
only). An older release refuses that table, so while a rollback would go to one, it waits in
`/etc/sizeup/adapter-pending.json` and is written at the next switch (`sizeup/cloud/configupdate.py`, the same rule as
the other config additions). (A local path as the repo is for CI and tests only: `SIZEUP_ADAPTER_ALLOW_LOCAL=1`.)

How it updates (`sizeup/cloud/autoupdate.py`): every update, and every check while the station stays on its build,
fetches the ref over SSH into `/opt/sizeup/adapter.git`. Each adapter commit's wheels (and its dependencies') are
built **once**, as the sizeup user, checked and kept root's in `/opt/sizeup/adapter-wheels/<commit>-<cpXY>`
(pruned to the kept releases' commits and the one at the ref). Root refuses a wheel with a `.pth`,
`sitecustomize.py` / `usercustomize.py`, any `.data/` directory, a path outside site-packages, or a top-level module or
package that the venv already has or another distribution's RECORD claims, then **unpacks** the rest itself into a
hard-linked copy of the staged release, `releases/<sha>-a<adapter commit>`: no pip, no interpreter (the old version
goes by its RECORD). Its **preflight** runs as the sizeup user with no credentials: it imports, has a
`sizeup.incident_sources` entry point, and its module's `self_check()` passes. The contract: an optional
module-level `self_check()`, no arguments, run with no credentials; it passes by returning `None`, `""`, `True` or an
empty list, and fails by returning a reason (a string) or a list of reasons, or by raising (only the exception's type
is kept). Root's
other interpreters never read a venv's site-packages either (`-I -S` for compileall, `-S` for `sdr-serial`, the
donor-venv check as the sizeup user). No adapter or dependency code ever runs as root.

**An adapter problem never holds up a sizeup update.** The release gets the adapter commit at the ref; if that one
can't be fetched, built, installed or doesn't pass, the one that runs now (its wheels are cached: no rebuild, no
PyPI); if even that fails, none (the paging decoder is the primary meanwhile, #297). The failure is recorded for the
adapter commit only (retried after 5, 15, 60, 60 minutes, 5 tries, then failed until its ref moves), and shows as
`failing` in status, doctor and the hub's Health. If the release with the adapter doesn't come up (`/healthz`, or
a start cut short), it goes back, then tries the same sizeup release without the adapter before sizeup's sha is blamed:
if that comes up, the adapter commit is the failed one. A new adapter commit alone is a side-by-side rebuild of the
same sha, with the same quiet-moment wait, `/healthz` check and switch back. `sizeup-station rollback` goes back to the
previous release, its adapter with it; to a release with the adapter only while the adapter is set up (`--force`: the
next check takes it out again).

Watching it: `sizeup-station status` / `doctor` ("adapter: NAME abc1234 installed"), the station page's Hub row and
the hub's Settings > Station > Health (Version > Adapter): the repo's short name, the installed commit, installed
or running (the runner's word, each source's own health) or failing. Never a credential or the repo's URL.

The adapter stays out of the **public** deploy check: the update's result line (`last.line`), `last.why` and sizeup's
own failures say only "the adapter" (no reason, repo name or commit; a release name's adapter commit becomes
`+adapter`), because `GET /api/station/status` passes them to `tools/deploy_check.py`, which prints them into the
GitHub Actions log of this public repo. The adapter's own words are in state.json `adapter`, `last.adapter_why`, the
station's journal and `/var/log/sizeup-update.log`, and the hub's Settings > Station > Health (behind Cloudflare Access).

### A credential kept in AWS SSM

Changed without touching the station (#417). A credential that changes now and then (a
shared login's password) can live in the department's AWS account instead, as a SecureString parameter
`/sizeup-station/adapter/<NAME>` (`<NAME>` = the credential's name) encrypted with a KMS key of its own. The station
reads it with an IAM user of its own, `sizeup-station-adapter`, allowed `ssm:GetParameter` on `/sizeup-station/adapter/*`
and `kms:Decrypt` on that key through SSM, nothing else; the key's policy denies Decrypt to every other principal (the
account's root included), and the hub's roles get an explicit Deny on the prefix and the key. **The hub never holds,
relays or can read it.** Setup answers y to "Read them from AWS SSM" and gives the region and that user's access key
(hidden); they are credentials like the others (`SIZEUP_SSM_REGION`, `SIZEUP_SSM_KEY_ID`, `SIZEUP_SSM_SECRET` in the
credstore, never station.env, the config or the environment). A new key pair must read the password's parameter (one
signed GetParameter by the release's `adapterssm.py` with `-I -S`, the pair on its stdin, the version only) or nothing
is saved, and setup says AWS's error code (UnrecognizedClientException: a wrong key id; InvalidSignatureException: a
wrong secret or the clock off). Setup then offers to remove the copy kept here of each password (`*_PASS`, default
yes); a user name stays here unless SSM has a parameter for it. An adapter writes `ssm.json` in its state directory
(`/var/lib/sizeup/sources/<source>/`) after each SSM fetch, `{"names": [...], "at": <epoch>}`, the names SSM gave,
never a value: `adapter status` / `doctor` say "from AWS SSM" only for those, and `status` shows the last 4
characters of the station key in use (to compare with `aws iam list-access-keys`). Any change to the SSM settings
restarts the station service, so the adapter uses them at once.
The fetch is `sizeup/cloud/adapterssm.py` (stdlib, SigV4 from `sizeup/aws.py`, no proxy): a value only in memory,
never logged; an error is AWS's error type only.

How often to read it and when a changed version may be tried are the adapter's own rules (its repo, not this one).
The station's adapter:

* reads SSM at start and every 5 minutes;
* holds its log-ons ("waiting for SSM") while SSM has never answered, rather than try a local value that may be stale;
* after a refused log-on, reads SSM again and tries once more only if the version changed;
* **the lockout rule:** a new version lifts a stop on bad log-ons but keeps its log-on caps (each new version is one
  more attempt on the shared account), and **3 refused versions in 24 h stop it until a person resets it**
  ([Reset](#reset-from-the-hub-or-the-station)).

Its health says `credential vN from SSM (key ...WXYZ), updated <time>`.

Changing the password, from a Mac with the department's AWS sign-in:
`printf '%s' "$pw" | aws ssm put-parameter --name /sizeup-station/adapter/<NAME> --type SecureString --key-id
alias/sizeup-station-adapter --overwrite --value file:///dev/stdin` with `$pw` read hidden (never an argument or an
environment variable); the department's setup scripts do this, make the key, the user and the denies, and rotate the
access key. IAM Roles Anywhere (an X.509 certificate on the station instead of a long-lived key) is the later option.

### Reset, from the hub or the station

An adapter may stop with "failing: login (manual reset needed)" to protect a shared account. Fix the cause first
(the credential), then reset it, either way:

* on the station: `sudo sizeup-station adapter reset [source]` (no source: every source);
* from the hub, an admin: Settings > Station > Health, a Reset button beside each source that is failing (with a
  confirm). That sends a `sources_reset` job over the station's authenticated jobs channel; the station calls the
adapter's optional `reset()` on that adapter's own thread, before its next poll. `POST /api/sources/reset` (hub and
station) takes JSON with `X-Sizeup-Action: reset` only, one per source per 5 minutes (counted once the station took
it; "all" counts for every source). The hub logs who pressed it
(their Access identity), nothing else. An adapter without `reset()`: nothing happens, and the answer says so.

### Limits

**Known limitation (#314):** the adapter runs inside sizeup-station, as the `sizeup` user, and its credentials are files
under `/run/credentials/sizeup-station.service/` owned by that user. Other programs that run as `sizeup` and parse
untrusted radio or network input (trunk-recorder, whisper-server, readsb, rtl_fm) could read them if one were
compromised. Running the adapter as its own service under its own user is #314.

The hub refuses: a hub's config (`[cloud] role = "hub"`) makes setup, the updater and the self-check refuse, and the
hub process names any adapter installed in its environment in its log and never loads it.

## Switching over from the Mac

Only one station should forward at a time (two would send everything twice).

1. On the Mac: its station page's Settings (Settings > Station > Hub link if it runs the CAD), turn
   **Forward to hub** off (or stop sizeup there). What it had queued goes up first.
2. Move the dongles to the new station (all at once, or one by one: a dongle the Mac still has is
   simply not there for the PC). The PC's `sizeup-station status` lists each one as it arrives.
3. On the new station: `sizeup-station status` shows the hub connected; each dongle takes the next
   job as it arrives (FG1, then Fire/EMS 15, then South 2, then FG2). Check or change them at
   http://localhost:8091, Radios (or from the hub's monitor). The hub's Station row says "connected".

Both use station id `pcfd-hq`: to the hub it is the same station, moved. Going back is the reverse.

## Dongles get a job by themselves

`[radio] auto_assign = true` (the default): when a dongle shows up, at startup or plugged in while
running, and no person has picked a job for it, it gets the first job in `auto_assign_order` that no
**plugged-in** dongle has:

1. `fg1`: FG1, 453.0375 NFM, MDC-1200 IDs
2. `p25`: Fire/EMS 15, the county P25 system through trunk-recorder. One dongle runs it (2.4 MS/s at
   477.19375 MHz covers the control channels 476.075-476.2375 and the voice channels); it is skipped
   when trunk-recorder isn't installed or its config.json has more than one source.
3. `south2`: South 2, 470.2 NFM, paging tones
4. `fg2`: FG2, the county's Fireground 2, 458.0375 NFM, MDC-1200 / FleetSync IDs like FG1 (#119). Last,
   so it takes a 4th free dongle and never one of the first three's; with three dongles, pick FG2 by
   hand on one of them (station page, Radios, or the hub's Settings > Radios)
5. `hpn tower`: HPN Tower, Westchester County Airport ATC, 118.575 AM (#233; [below](#hpn-atc-the-airband-dongle)).
   Last of all: only a 5th free dongle takes it by itself; normally a person picks it for the airband dongle

With all five covered, another dongle stays Off. NY App 126.4/120.8, NY App 124.65 and HPN Ground (#260) are never in
the order: a dongle only runs them when a person picks one ([below](#ny-approach-and-hpn-ground)). The log says `sdr: V3 #2 (00000001) → FG1 (auto)`,
and the station page and the hub's monitor show the row as "· FG1 · auto".

* **FG1 first, even after a dongle dies.** A job whose dongle is unplugged or missing isn't covered:
  the next dongle that arrives takes it (its program stops waiting on the missing one), so a
  replacement for a dead FG1 dongle gets FG1.
* **When the old dongle comes back:** if nobody took its job, it resumes it. If a replacement has it
  and the job was set automatically, the old dongle is looked at like a new one: next free job, else
  Off (`sdr: V4 (v4) back; FG1 is on V3 #2 → Fire/EMS 15 (auto)`). If a person had set it, it keeps
  its job beside the replacement, with a warning in the log; the job keeps running where it is now
  (on the replacement) until a person picks, or the replacement goes, and at the next restart the
  person's dongle has it and the other one gets the next free job.
* **Running dongles are never moved.** Only dongles arriving (and, at startup, those without a job)
  are looked at: a dongle on P25 doesn't jump to FG1 when FG1's dongle disappears.
* **Startup:** for up to 60 s after sizeup starts, a dongle without a job waits until the dongles
  that had jobs are back, so the order USB attaches them in (usbipd, every 10 s) doesn't move jobs.
* A job picked by a person (here or on the hub), **Off included**, is kept across restarts and
  replugs and never changed automatically. Picking the job a dongle already has makes it the person's.
* A dongle another app holds (rtl-claims, e.g. shortfinal on the Mac) is never taken. An auto job
  that fails to start isn't retried; one that keeps failing is logged once and left where it is.
* `sdr_modes.json` (next to `captures/clips`) records who set each job: `{serial: mode, ...,
  "_set_by": {serial: "person" | "auto"}}`. Entries saved before this (no `"_set_by"`) count as not
  set by a person, so an Off left over from the old "new dongles start Off" is filled.
* `auto_assign = false`: new dongles start Off until someone picks a job (the old behaviour).

## trunk-recorder, its own service

trunk-recorder (Fire/EMS 15, the county P25 system) runs under `sizeup-trunk-recorder.service`, not inside
sizeup-station (GitHub #71), so a restart or a Python-only update of sizeup doesn't stop it: P25 keeps its control
channel, and the Fire/EMS 15 row stays "ok" through the update.

* **Who decides.** sizeup, as before: Settings > Radios, auto-assign and the hub's switch pick the dongle. sizeup writes
  its choice to `/var/lib/sizeup/state/p25_recorder.json`; the service (`/usr/local/lib/sizeup/trunkrecd.py`, the
  system python3, stdlib only) reads it every half second and runs trunk-recorder on that dongle's current index,
  or nothing. Switching a dongle away from Fire/EMS 15 waits until trunk-recorder has stopped and let the dongle go.
* **No privilege.** The service runs as the sizeup user, like sizeup-station and the ADS-B decoder: no sudo, no polkit
  rule, no root helper. sizeup never starts or stops a unit; it writes a file the service reads.
* **Across a restart.** sizeup-station leaves trunk-recorder running when it exits. The next sizeup hands it the same
  dongle (it keeps its jobs in `sdr_modes.json`), which changes nothing: the same trunk-recorder runs on. A sizeup that
  starts with no dongle on Fire/EMS 15 stops it.
* **The dongle claim** (`~/.local/state/rtl-claims`, shared with shortfinal) is the service's while it runs Fire/EMS 15
  (app `sizeup`, mode `Fire/EMS 15`, the service's pid), so it stays right while sizeup restarts. sizeup counts it as
  its own. A dongle another app holds is never taken: the row says `held`.
* **Who is talking** still comes from trunk-recorder's output and the unit script (`unit-event.sh`): the service
  writes both to `/var/lib/sizeup/trunk-recorder/trunk-recorder.log` (5 MB, one old file kept), and sizeup reads it as
  it grows. The calls go to the capture directory sizeup watches, as before.
* **When it restarts.** Only when what it runs changed: an update with another trunk-recorder build (or librtlsdr), a
  new `config.json` or `talkgroups.csv`, or a new `trunkrecd.py` or unit (`install-linux.sh` `tr_sig`). The install log
  says `trunk-recorder: unchanged, left running` or `trunk-recorder: (re)started`. A change of `[p25] recorder` or
  `recorder_dir` in `/etc/sizeup/config.toml` restarts trunk-recorder by itself within a second. After editing
  `/var/lib/sizeup/trunk-recorder/config.json` by hand (it is remade from the repo at the next update):
  `sudo systemctl restart sizeup-trunk-recorder`.
* **Checking it.** `sizeup-station status` has a `p25:` line (the service, its state, the dongle, trunk-recorder's
  pid); `doctor` checks it runs; `sizeup-station logs` includes it (`journalctl -u sizeup-trunk-recorder`: one line per
  start, stop and exit). `python3 -I /usr/local/lib/sizeup/trunkrecd.py print` shows the command line it would run.
* **The watchdog** (#547). A trunk-recorder that runs but stops working is restarted, at most 3 times an hour, with a
  `p25: watchdog: <why>: restarting trunk-recorder` line in the journal each time: no new call file for 10 times the
  system's own mean gap between calls over the 3 h before the last call (15 min to 3 h; 3 h with fewer than 10 calls;
  doubled after each restart with no call since), no control channel decode rate logged for 2 min (`config.json` has
  `"controlWarnRate": -1`: one every 3 s), or the control channel under 2 msg/s for 10 min (doubled after each restart
  that didn't bring it back, until it has decoded well for 10 min). Durations are on the monotonic clock: a step of
  the wall clock (NTP, WSL2 after the host slept) is no fault. The hub pushes "no decode rate" only after 5 min, so a
  decoder the watchdog heals never pages. `[p25] watchdog_min_min`,
  `watchdog_max_min`, `watchdog_factor` and `watchdog_per_hour` (0: off) in `/etc/sizeup/config.toml` change it (a
  release from before #547 refuses those keys: add them only once you won't switch back that far).
* **Its own output in the journal** (#547): the control channel's decode rate (the first, lost / back, and every
  30 min), and trunk-recorder's error, warning and control channel lines (radio IDs masked, at most 10 different ones
  per 5 min, then a count). Everything else stays in `trunk-recorder.log`.
* `sudo sizeup-station restart` restarts sizeup only; `stop` stops trunk-recorder's service too, `start` starts both.
* **Going back to a release from before this** (the updater's switch back, or `sizeup-station rollback`): the updater
  stops, disables and removes the service first, so that release's sizeup runs trunk-recorder itself, as it did. Going
  back by hand with an old `install-linux.sh`: `sudo systemctl disable --now sizeup-trunk-recorder` first.
* On a Mac or a bench (no `SIZEUP_TRUNK_RECORDER_UNIT=1`, which only sizeup-station.service sets) sizeup still runs
  trunk-recorder as its own child.

### More P25 systems (MTA MRRS)

A second P25 system (#409, #419) is one directory, `deploy/trunk-recorder/systems/<name>/`. `mrrs` is MTA MRRS.
Nothing of it runs until a person gives it a dongle. The files, the tiers and the rules:
[deploy/trunk-recorder/README.md](../trunk-recorder/README.md#more-systems-409) and docs/cloud.md, "More P25 systems".

| | |
|---|---|
| Dongle jobs | One per source of its `config.json`: `MRRS 770` and `MRRS 773`. One 2.4 MS/s dongle takes in one side. |
| Picked | By a person, in Settings > Radios. Never by auto-assign. Giving it a dongle that runs a fire job asks for a confirm. |
| Runs as | A second trunk-recorder under `sizeup-trunk-recorder.service`, at a lower CPU priority (nice 5) than the county's. Only the sources that have a dongle run. The county's trunk-recorder never restarts for it. |
| Files | `/var/lib/sizeup/trunk-recorder/systems/mrrs/` (`config.json` and `talkgroups.csv` from the release, `trunk-recorder.log`). Calls: `/var/lib/sizeup/captures/p25-mrrs`. sizeup's wish: `/var/lib/sizeup/state/p25_recorder.mrrs.json`. |
| Claims | Each of its dongles has a claim with its job as the mode ([docs/handoff-shortfinal-dongles.md](../../docs/handoff-shortfinal-dongles.md)). |
| Unplugged | A dongle of it gone from USB stops its trunk-recorder (`absent`, not green); it starts again once every one is back (journal: `p25 mrrs: dongles back after m:ss: restarting trunk-recorder`), never counted against the watchdog's 3 an hour (#592). |
| Talkgroup tiers | `alert` (record, transcribe, may pre-alert), `text` (record, transcribe), `record` (audio only), `off`, and `activity` for an encrypted talkgroup: never recorded, only that it was active. The file's `Tier` column is the default; Settings > Radios changes it. |
| Source keys | Its talkgroups are named with the system: `p25:mrrs:<talkgroup>`. The county's keep `tg:<talkgroup>`. |
| Its traffic | Context only: never Main, never on our incident, never an incident source. |

**Before giving it dongles for good**, measure it: Settings > Radios > Receivers > **Reception test** on the hub lends
one dongle to one side for a while and gives it back ([docs/reception-test.md](../../docs/reception-test.md)). The
county's own system can be tested the same way from another dongle, through a probe that never touches the live
recorder.

## Squelch

Each FM channel has a noise squelch (`sizeup/radio/fm.py`): it opens when the idle hiss drops by **Open** dB (a
carrier quiets the receiver) and stays open above **Close** dB. Tuned per channel on the hub: Settings > Radios >
**Squelch** (#476, admins; `sizeup/channels.py`). The hub sends the values to the station with the channel choices,
and the station applies them to the running receiver at once: no restart.

| Setting | Default | Range | What |
|---|---|---|---|
| Open | the channel's `squelch_db` in the config, else 10 dB | 4 to 30, steps of 0.5 | Quieting that opens the squelch. Higher: fewer noise opens, weak carriers missed. |
| Close | 6 dB | 2 to 28, steps of 0.5 | It stays open above this. Must be under Open. |
| Min | 0.3 s | 0.1 to 2.0, steps of 0.05 | A carrier shorter than this: nothing kept, never shown as talking. |

* **Traffic channels only** (FG1, FG2). **A paging channel (South 2) is read-only** there and stays at the
  receiver's default: a high Open would mute pages. The airband channels have their own AM carrier squelch
  (`squelch_db` in the config), not this one.
* **The columns** beside each channel: the noise floor (the idle hiss, relative dB), the quieting now, **Short/min**
  and **PL est.**
* **The short-open counter** (Short/min): opens that closed with nothing kept, per minute over the last 5 minutes.
  2 a minute or more means the squelch is chattering on noise.
* **The health hint**: at 2 short opens a minute or more, the hub's Settings > Station > Health shows on that channel's
  row `squelch chattering: N short opens/min · raise Open in Settings > Radios > Squelch` (a paging channel:
  `noise on the channel: check antenna and dongle`). A hint only, never an alert.
* **The PL estimate**: for each key-up, an estimate of the CTCSS (PL) tone on it: a tone in Hz, `none` (no tone line
  heard) or `unsure` (too short, speech or hum). FG1 runs PL 100.0, so a key-up without it may be another user or
  interference. It is a diagnostic: nothing is gated on it.
* A value set for a channel that is gone, renamed or now a paging channel is shown as "not applied" and changes nothing.

## ADS-B receiver

The station can run our own ADS-B receiver (GitHub #235): one RTL-SDR with a 1090 MHz filter and an LNA in front of
it. Because of that filter it is **never** a radio: it is chosen by its exact serial, never auto-assigned, never given
a channel. `sizeup-adsb.service` runs readsb on it (and on nothing else), the station sends what it hears to the hub
every second or two, and the hub's map uses our positions first, adsb.lol / adsb.fi when we lose a plane. An
emergency squawk raises the alarm and an airport pre-alert in the queue (docs/cloud.md "Our own receiver on the
station").

**When you plug it in:**

1. **Give it a serial that is unique and not all digits** (readsb and the rtl tools read `00000001` as "device #1").
   Find its index with `sudo sizeup-station doctor` ("librtlsdr sees"), then
   `sudo sizeup-station sdr-serial <index> adsb1090` ([Two dongles, one serial](#two-dongles-one-serial): it stops
   the radios, trunk-recorder's service and the decoder, backs up the EEPROM, writes the serial, waits for the replug, starts them again).
   Skip this if it already has a unique name.
2. **Name it**, either way:
   * **Settings > Radios** (an admin, on the hub or the station page): its row has **Use for ADS-B**; confirm. Whatever
     it was running stops; it shows as **ADS-B receiver (reserved)**. **Release** gives it back (it stays Off, and
     auto-assign never gives it a job by itself).
   * or in `/etc/sizeup/config.toml`: `[aircraft.receiver] sdr = "adsb1090"`, then `sudo sizeup-station restart`.
     (Settings' choice wins over the file once made.)
3. **Bias-tee for the LNA:** `bias_tee = false` by default. Set `bias_tee = true` in `[aircraft.receiver]` **only if
   the LNA is powered up the coax** (it has no power supply or USB lead of its own, and its maker says it takes 4.5 V
   bias power); leave it false for an LNA with its own supply, a filter alone, or if you aren't sure. A change takes
   effect within a few seconds (the decoder rereads the config), no restart needed. `gain = "auto"` stays unless you
   have a reason (an LNA makes a fixed high gain overload).
4. **Check it:**
   * `sizeup-station status`: the `adsb:` line says `ok: N aircraft · M with position · X msg/s (adsb1090)`.
     `held` means shortfinal has the dongle (switch it Off there); `unplugged` means the serial isn't on USB;
     `no signal` means nothing decoded for 2 minutes (antenna, filter, LNA, bias-tee).
   * `sizeup-station logs` (`journalctl -u sizeup-adsb`): `adsb: readsb on adsb1090 (device #N), gain auto, bias-tee off`.
   * Settings > Radios: *ADS-B receiver (reserved) · decoding: N aircraft, X messages/s · to the hub every 1 s*.
   * The hub's Settings > Station > Health: *ADS-B receiver (station)*, messages a second, last message, *To the hub: ok*.
   * The CAD map's Aircraft layer: click an aircraft near HPN: the box's foot says *Our receiver · hh:mm:ss*, and its
     "i" *Position: ADS-B ..., 1090 MHz*; the layer box lists *Receiver (station) · adsb1090 · 1090 MHz: N aircraft*.

What runs: `python3 -I /usr/local/lib/sizeup/adsbd.py run` as the sizeup user (stdlib only, like the updater), which
starts `readsb --device-type rtlsdr --device <its index> --gain auto --lat/--lon <[station] lat / lon: the South
End by default> [--enable-biastee] --write-json
/run/sizeup-adsb/json --write-json-every 1` (no network ports) and restarts it when it exits, stalls or the settings
change; `python3 -I /usr/local/lib/sizeup/adsbd.py print` shows the command line it would run now. It holds the
dongle's claim in `~/.local/state/rtl-claims` while readsb runs, and never starts while another app holds it. An
update restarts it only when the decoder or its unit changed.

USB transfer memory (#269): every dongle on the host (readsb, each rtl_fm, trunk-recorder) shares the kernel's
`usbfs_memory_mb` (16 MB by default). With about six dongles it runs out and readsb dies a few seconds in with
`Failed to submit transfer`, `echo 0 > /sys/module/usbcore/parameters/usbfs_memory_mb` and `rtlsdr_read_async returned
unexpectedly` (the cause shows as `usbfs`). The station sets it to 0 (no limit) as root: install-linux.sh (now, and
`/etc/modprobe.d/sizeup-usbfs.conf` where usbcore is a module), `sizeup-usbfs.service` at every boot (WSL2's usbcore is
built in), and sizeup-adsb's `ExecStartPre=+`. A missing or read-only file is noted and never stops anything.
`sizeup-station status` and `doctor`, Settings > Radios and the hub's Health show the value. If readsb still dies
that way twice in a row, it is restarted with smaller USB transfers (`--sdr-buffer-size 32`, KiB, instead of 128).

A wedged dongle (#315): readsb exits with `SDR wedged, exiting! (check power supply / ...)` when the dongle stops
delivering samples, usually power: an LNA on the bias-tee draws from the dongle's port, and USB extensions and unpowered
hubs sag. The cause shows as `wedged` with what to check (the bias-tee only when it is on). After 3 wedges in an hour
it waits 5 min before the next try, then 15 min, and says so. 10 min of decoding resets the count, and so does
changing the dongle, gain or bias-tee (which also ends the wait at once).

## AIS receiver

Our own AIS receiver (GitHub #402; docs/vessels.md): one RTL-SDR on a marine VHF antenna, decoding AIS channels A and
B (161.975 / 162.025 MHz) with AIS-catcher. **Off until the hardware is in.** The Vessels map layer works without it
(aisstream.io, docs/vessels.md); with it, the hub gets what we hear ourselves too, first-hand and with no third party.
Like the ADS-B dongle it is chosen by its exact serial and is **never** one of sizeup's radios: never auto-assigned,
never given a channel.

**Hardware:**

* **An RTL-SDR**, ideally a TCXO one (RTL-SDR Blog V3 or V4): it stays on frequency, so `ppm = 0` holds. A cheap
  dongle without a TCXO drifts a few ppm and needs `ppm` set (AIS-catcher's `-p`).
* **A marine VHF antenna tuned near 162 MHz**, mounted as high and clear as you can: a 1/2-wave or 5/8-wave marine
  whip (a stock boat VHF antenna is fine), or a broadband discone if one antenna must serve other bands too. AIS is
  line of sight: height buys range.
* **Low-loss coax**, kept short: LMR-400 for a long run, LMR-240 for a short one (RG-58 loses a lot at 162 MHz).
* **Optional:** a 162 MHz SAW bandpass filter (it keeps paging, FM broadcast and the county P25 out of the dongle) and/or
  an LNA. Use the dongle's bias-tee only for an LNA that is powered up the coax (AIS-catcher `-gr biastee on`; not
  configured by sizeup today: add it by hand only if you need it).

**When you plug it in:**

1. **Give it a serial that is unique and not all digits:** find its index with `sudo sizeup-station doctor`, then
   `sudo sizeup-station sdr-serial <index> ais162` ([Two dongles, one serial](#two-dongles-one-serial)), or
   `rtl_eeprom -d <index> -s ais162` with nothing else running on it. `sdr-serial` does not stop `sizeup-ais`:
   to rename a dongle the AIS decoder already runs on, `sudo systemctl stop sizeup-ais` first and start it after.
2. **Install AIS-catcher** (not built by install-linux.sh, so updates stay fast): build its latest release from
   https://github.com/jvde-github/AIS-catcher (its README: `cmake` against librtlsdr, then `make`) and put the binary
   at `/opt/sizeup/ais-catcher/bin/AIS-catcher` (or anywhere on the sizeup user's PATH). `AIS-catcher -l` should list
   the dongle.
3. **Turn it on**, only once the station runs a release with this section (an older release refuses config keys it
   doesn't know, so a switch back to it would fail): in `/etc/sizeup/config.toml`

   ```toml
   [vessels.receiver]
   enabled = true
   sdr = "ais162"
   # gain = "auto"        # or dB (0 to 50)
   # ppm = 0              # a non-TCXO dongle's correction
   ```

   then `sudo systemctl restart sizeup-ais sizeup-station`.
4. **Check it:**
   * `python3 /usr/local/lib/sizeup/aisd.py status`: `"state": "ok"`, with messages a second and vessels heard.
     `held` means another app (shortfinal) has the dongle; `missing` means AIS-catcher isn't installed or the serial
     isn't on USB; `no_signal` means nothing heard for 10 minutes (the antenna, or a quiet harbour at night).
   * `python3 /usr/local/lib/sizeup/aisd.py print`: the command line it runs.
   * `journalctl -u sizeup-ais`: `ais: starting: AIS-catcher on ais162 (index N)`.
   * The CAD map's Vessels layer: a vessel's box says *Our receiver* in its foot.

What runs: `python3 -I /usr/local/lib/sizeup/aisd.py run` as the sizeup user (stdlib only), which starts
`AIS-catcher -d:<its index> -gr rtlagc on tuner auto -p 0 -o 5` (JSON lines on stdout; no web viewer, no network
ports), keeps each vessel's latest messages in `/run/sizeup-ais/messages.json`, and restarts it with backoff when it
exits. It holds the dongle's claim in `~/.local/state/rtl-claims` (mode `ais`) while AIS-catcher runs and never starts
while another app holds it. sizeup-station posts the vessels to the hub every 5 s while someone looks at the layer (or
a marine call is open), else every 30 s; nothing queues while the hub is away.

**Share it once it runs.** Feeding [AISHub](https://www.aishub.net/) is what earns its free data API (another source the
Vessels layer has a hook for), and AIS-catcher can also share with aiscatcher.org (`-X <sharing key>`). Both need
AIS-catcher's network output, which sizeup leaves off: add it to the command line by hand and say so in the issue.

## HPN ATC: the airband dongle

The airport pre-alerts (#233, `sizeup/atc.py`) listen to Westchester Tower on **118.575 MHz AM** (FAA Chart
Supplement via [airnav.com/airport/KHPN](https://www.airnav.com/airport/KHPN), effective 2026-10-29; the tower is
open 0600-2300, CTAF on the same frequency when it's closed). Its other frequencies, for another dongle and
another `[[radio.channels]]` entry with `kind = "airband"` (picked by hand):

| | MHz | |
|---|---|---|
| Westchester Tower | 118.575 | the default channel ("HPN Tower") |
| Westchester Ground | 121.825 | 3.25 MHz from Tower: one RTL-SDR covers ~2 MHz, so its own dongle |
| Clearance Delivery | 126.4 (127.25 when the tower is closed) | |
| D-ATIS | 133.8 | continuous: a good receiver check, nothing for the pre-alerts |
| New York Approach | 126.4 (0700-2300), 120.8 (2300-0700), 124.65 | N90, HPN arrivals |
| New York Departure | 120.55 | N90, HPN departures (1.975 MHz from Tower: one dongle only with several channels per dongle, #119) |

One dongle runs one channel today (several per dongle is #119). The receiver is shortfinal's (tested on the bench):
`rtl_fm -M am -s 12000` at 36.4 dB gain, a carrier squelch 3.5 dB over the noise floor (1.5 dB hysteresis, 0.4 s
hang, 0.6 s minimum, 30 s maximum; a carrier held 0.4 s within 1 dB under the open level opens too, #270), and a carrier
under 3 dB is kept but not transcribed. Each transmission's carrier and floor (dBFS) and its voice SNR are logged on the
station (`HPN Tower: carrier +12.3 dB over the floor (...)`) and sent to the hub with it. Its transmissions go to
Amazon Transcribe on the hub by default (a priority channel, kept within the reserve past the monthly limit, with its
own vocabulary `sizeup-atc`: Settings > Radios > Vocabulary > Push sends it with the PCFD one).

**Pick it:** plug the airband dongle in, then on the station page (Radios) or the hub's Settings > Radios set that
dongle's row to **HPN Tower**. A dongle another app holds (shortfinal's rtl-claims) is never taken, by hand or by
itself.

**An installed station** gets the HPN Tower channel from the update once (`sizeup/cloud/configupdate.py`
"hpn-atc", the old file kept as `config.toml.before-hpn-atc`), but only when the release it is switching from can
read an airband channel: a release from before #233 can't, and a failed switch goes back to it. So the first update
to this release says `config: hpn-atc: deferred ...`, and the next update after it adds the channel. To have it at
once, add the block from `deploy/station/config.station.toml` to `/etc/sizeup/config.toml` by hand
(`sudo nano /etc/sizeup/config.toml`, and "hpn tower" at the end of `auto_assign_order`), then
`sudo systemctl restart sizeup-station`.

**Serials.** Give every dongle a unique serial that isn't all digits, once: a Nooelec NESDR SMArt, like most RTL2832U
dongles, ships with a default serial (often `00000001`, the same as the station's "V3 #2"). For the airband dongle,
`sudo sizeup-station sdr-serial <its index> hpn-twr` ([Two dongles, one serial](#two-dongles-one-serial)), then
name it in `[radio] sdr_names` (`hpn-twr = "NESDR SMArt"`). shortfinal keeps its dongles' factory EEPROMs in
`deploy/eeprom-backups/`; copy the NESDR SMArt's backup (`~/sizeup-eeprom-backups/`) next to them.

## NY Approach and HPN Ground

Two more airband choices for a dongle (#260), beside HPN Tower, with the same treatment: AM, the carrier squelch,
Amazon Transcribe with the `sizeup-atc` vocabulary, the airport pre-alert rules and talker identification. Emergencies
are usually declared to Approach before the tower, so a spare dongle on NY Approach gives the earliest warning.
Frequencies and hours from the FAA Chart Supplement via [airnav.com/airport/KHPN](https://www.airnav.com/airport/KHPN)
(FAA information effective 29 October 2026): WESTCHESTER TOWER 118.575 [0600-2300] (CTAF 118.575); WESTCHESTER GROUND
121.825 [0600-2300]; NEW YORK APPROACH 120.8 2300-0700, 126.4 0700-2300, 124.65 (no hours).

| Channel (#275 names) | On its own dongle | |
|---|---|---|
| HPN Tower | 118.575 all day | the tower is closed 23:00-06:00: 118.575 is the CTAF then, still heard |
| NY App 126.4/120.8 | 126.4 07:00-23:00 (+ 124.65 with multichannel on), 120.8 23:00-07:00 | the `schedule` |
| NY App 124.65 | 124.65 | Approach's third HPN frequency, fixed |
| HPN Ground | 121.825 06:00-23:00, 124.65 23:00-06:00 | Ground is closed overnight: its dongle hears 124.65 then |

**Several frequencies on one dongle (#275), off by default.** An RTL-SDR captures about 2 MHz. A channel's
`also = [{ mhz = 124.65, from = "07:00", until = "23:00" }]` puts another channel's frequency on the same dongle, but
only with `[radio] airband_multichannel = true` (off by default) and only while no other running dongle has that
frequency (then the dongle stays on `rtl_fm`, narrow). While more than one is on, the dongle runs `rtl_sdr` and
`sizeup/radio/channelize.py` (one FFT channelizer, its own process) instead of `rtl_fm`, and each frequency has its own
squelch, tone notch, clip, transcript and talker ID, and is heard as the channel it belongs to. The capture keeps every
channel within 35% of the rate from the centre (the R820T's IF filter follows the rate, and past it the RTL2832U's
decimator rolls off and lets aliases in), 25 kHz off the DC spike and 30 kHz off the other's mirror image, and a pair
more than 1.2 MHz apart gets at least 2.4 MS/s: 126.4 + 124.65 at 2.56 MS/s centred on 125.54, 120.8 + 121.825 at 1.6
MS/s. At most about 1.78 MHz between the lowest and highest at any one time (the config says so when a set doesn't
fit): 118.575 and 120.8 (2.2 MHz) need two dongles. The carrier over the floor reads about 1 dB higher through the
channelizer than through `rtl_fm` (its noise bandwidth is narrower). If `rtl_sdr` gives no audio 3 starts in a row, the
dongle goes back to `rtl_fm` on its own frequency for 30 minutes; if the channelizer runs under 97% of real time (USB
dropping samples), the log says so.

**Before turning it on: the on-air A/B.** With the channel on its dongle, on the station:
`python3 /opt/sizeup/current/tools/airband_ab.py "NY App 126.4/120.8" --minutes 60`. The dongle alternates between
`rtl_fm` and the channelizer every 5 minutes (between transmissions) and the tool prints, per path and frequency, the
transmissions' median carrier over the floor, floor (dBFS) and voice SNR (also logged: `... A/B over on 126.4: ...`).
The channelizer's carrier should read about +1 dB and its SNR the same; lower means the wideband capture costs the
channel something here: leave `airband_multichannel` off. Turn it on by adding `airband_multichannel = true` under
`[radio]` in `/etc/sizeup/config.toml` and restarting `sizeup-station`.

**The schedule and the set.** `schedule = [{ at = "07:00", mhz = 126.4 }, { at = "23:00", mhz = 120.8 }]` on the
channel: each frequency from its time until the next, in America/New_York wall-clock time (DST-aware); `also`'s `from` /
`until` the same. The program can't retune while it runs, so the receiver restarts it on the new set, and only between
transmissions: never while one is open or in its hang on any of its frequencies, and after 2 s with no carrier (a
read-back stays with its call); a channel that never falls quiet that long retunes 5 minutes late at the next gap. The
log says `NY App 126.4/120.8: retuned 126.4 -> 126.4 + 124.65 MHz (schedule: ...)`; each
transmission in the Radio view shows the frequency it was heard on (`118.575 CTAF` while the tower is closed).

**On screen.** The Radios rows show what each dongle is on now (`118.575 CTAF + 120.8`) and `Tower open` / `Tower
closed`; the dongle's button `NY App 126.4/120.8 · 126.4`; the tower's mark on the map says `CTAF` while it is closed.
The details (each frequency's carrier, the capture, the hours) are in the row's tooltip.

**Who it matches.** Approach traffic is 10-40 NM out, so while NY Approach is heard the hub's Aircraft feeds are asked
for 40 NM (for 30 minutes after its last transmission; the map stays 15 NM) and its callsigns are matched to HPN's
arrivals (on an approach, or pointed at HPN and descending) and departures (climbing away) out to 40 NM. HPN Ground
matches only aircraft on the field; HPN Tower is as before (`sizeup/atc_scope.py`).

**Pick it:** rename the spare dongle's serial once (`sudo sizeup-station sdr-serial <its index> nyapp`, [Two dongles,
one serial](#two-dongles-one-serial)), then on the station page (Radios) or the hub's Settings > Radios set its row to
**NY App 126.4/120.8**, and press Settings > Radios > Vocabulary > **Push** so the `sizeup-atc` vocabulary is current.

**An installed station** gets the three channels from the update once (`sizeup/cloud/configupdate.py`
"approach-ground", the old file kept as `config.toml.before-approach-ground`), only when the release it is switching
from reads a `schedule`: a release from before #260 refuses it, and a failed switch goes back to it. So the first update
to this release says `config: approach-ground: deferred ...` and the next one adds them; never to `auto_assign_order`.
Then #275's names and sets come the same way ("airband-sets", `config.toml.before-airband-sets`): "NY Approach" becomes
"NY App 126.4/120.8", "NY Approach 124.65" "NY App 124.65", NY App 126.4/120.8 gets the `also` line and HPN Ground the
`schedule` above, each only on a channel exactly as it was added, and only after approach-ground is done. A release from
before #275 refuses `also`, so the first update to this release says `config: airband-sets: deferred ...` and the next
one changes them. A dongle's saved job and the Settings > Radios > Channels choices follow the rename. It never turns
`airband_multichannel` on.

## Two dongles, one serial

sizeup opens every dongle by its exact USB serial. Two dongles with the same one can't be told apart, and a program
asked for "serial 00000001" may open the wrong one: on 2026-11-04 a NESDR SMArt plugged in on its factory `00000001`,
the same serial as "V3 #2", and Fire/EMS 15 (tg:307) stopped reaching the hub with nothing logged (#242). Now:

* **The station says so, loudly.** A serial on two or more dongles is a **critical** alert on the hub (Settings >
  Health, pushed to the status topic, a recovery notice once it's fixed): *Two dongles share serial 00000001
  (indexes 1 and 4): sizeup can't tell them apart. Fix: sizeup-station sdr-serial 4 &lt;new-serial&gt; (renames #4,
  NESDR SMArt); nothing opens 00000001 until then*. The index it suggests is the newcomer: the one whose product
  isn't the one that serial had while it was the only one (`sdr_products.json`, next to `sdr_modes.json`); without a
  product on record (none yet, or two different ones each seen alone under that serial), the last one. The station page (Radios) and the hub's Settings > Radios show a red box with the
  commands above the dongles, and the dongle's row says *serial shared: not opened*. `sudo sizeup-station doctor`
  FAILs on it, and the journal has `sdr: CRITICAL Two dongles share serial ...` once.
* **Nothing guesses.** No job (rtl_fm), no trunk-recorder and no readsb is started on an ambiguous serial: the
  program waits, its row says why (state *ambiguous*), and it starts by itself once the serial is on one dongle again.
  A trunk-recorder source in `config.json` that names such a serial (`rtl=00000001`) is left out of
  `config.runtime.json`; with none left it doesn't start. Auto-assign gives such a dongle no job, picking a job for it
  (or Use for ADS-B) is refused with the same text, and a job saved for it is kept, not dropped, for after the fix.
  A program already running keeps the dongle it opened while its serial was still its own (that handle is the right
  dongle); it isn't restarted on it. Dongles with serials of their own keep working.
* **Silence:** only the whole P25 system going quiet is an alert ("P25 trunk silent", critical: no call on any
  talkgroup for 20 min, 60 min into the night, or trunk-recorder losing the control channel). One talkgroup or channel
  silent (tg:307, FG1, South 2, HPN Tower ...) is a hint in Settings > Station > Health only
  ([docs/cloud.md](../../docs/cloud.md#station-health-and-alerts)).

**The fix: `sizeup-station sdr-serial <index|current-serial> <new-serial>`** gives one dongle a new serial, safely:

```sh
sudo sizeup-station doctor                            # "librtlsdr sees": index, serial, product of each dongle
sudo sizeup-station sdr-serial 4 hpn-twr --dry-run    # the checks and the plan; nothing stopped or written
sudo sizeup-station sdr-serial 4 hpn-twr              # do it: asks once, then tells you when to replug
```

1. **Checks the new serial**: 3-16 characters of `A-Z a-z 0-9 -` with at least one letter (the rtl tools read a
   number as a device index), not the serial of any dongle plugged in now. A serial on two dongles must be named by
   index (`sdr-serial 00000001 ...` is refused and says which indexes).
2. **Stops** `sizeup-station` (rtl_fm is its child), `sizeup-trunk-recorder` (trunk-recorder) and `sizeup-adsb` (readsb), and holds the
   updater and the health timer, so nothing opens a dongle while the EEPROM is written.
3. **shortfinal**: if its claim (`~/.local/state/rtl-claims/<serial>.json`) holds that serial, it asks you to switch
   that dongle Off in shortfinal and waits (Enter to look again, `q` to give up). shortfinal is never stopped or
   killed from here.
4. **Backs up the EEPROM**: `rtl_eeprom -d N -r ~/sizeup-eeprom-backups/<old>-<date>.bin` (your home under sudo).
   No backup, or one that isn't a 256-byte RTL2832 EEPROM, and nothing is written. To undo later:
   `rtl_eeprom -d <index> -w <that file>`.
5. **Writes** `rtl_eeprom -d N -s <new>`, answering its *Write new configuration to device [y/n]?*.
6. **Replug**: it asks you to unplug that dongle and plug it back in (WSL: sizeup-usb attaches it again within ~10
   s), waits up to 5 minutes, and checks that the new serial is there, on one dongle.
7. **References** to the old serial: the job saved for it (`sdr_modes.json`), the ADS-B choice
   (`state/adsb_receiver.json`), `[radio] sdr_names` / `sdrs` / `sdr_ignore` / `shared_device`, a channel's
   `device`, `[aircraft.receiver] sdr`, a trunk-recorder source. They are moved to the new serial only when it is
   certain the renamed dongle is the one they meant: its serial was its own, or it has the product that serial had
   before the second dongle turned up. Renaming the newcomer (the usual case) leaves them with the dongle that keeps
   the old serial: nothing to change. When it can't be told (no product on record, two of the same model) nothing is
   changed and it prints what to set either way. `/etc/sizeup/config.toml` is never edited: it prints the exact line
   (`[radio] sdr_names: "hpn-twr" = "V3 #2" (in place of "00000001" = "V3 #2")`); then `sudo sizeup-station restart`.
8. **Starts** what it stopped and lists the dongles: `#4  hpn-twr  NESDR SMArt: Off`, `#1  00000001 (V3 #2)
   RTL2838UHIDIR: Fire/EMS 15 (trunk-recorder)`. Pick the new one's job on the station page or the hub's Settings >
   Radios (HPN Tower for the airband dongle).

If anything fails after the services were stopped, they are started again and it says what stopped it.

**Examples.**

* The new **NESDR SMArt** for HPN Tower, on `00000001` like V3 #2 (the alert names its index, e.g. 4):
  `sudo sizeup-station sdr-serial 4 hpn-twr`, replug it, then `[radio] sdr_names`: `hpn-twr = "NESDR SMArt"` and
  its row to **HPN Tower**. V3 #2 keeps `00000001` and its job.
* The **ADS-B dongle** (behind the 1090 MHz filter), on a factory serial: `sudo sizeup-station sdr-serial <index>
  adsb1090`, replug, then **Use for ADS-B** on its row (or `[aircraft.receiver] sdr = "adsb1090"`). sizeup-adsb is
  stopped while it writes and started after.

By hand, if you must (the same steps): `sudo systemctl stop sizeup-station sizeup-trunk-recorder sizeup-adsb`, stop
shortfinal's use of it, `rtl_eeprom -d N -r ~/sizeup-eeprom-backups/<old>.bin`, `rtl_eeprom -d N -s <new>` (answer
`y`), replug, `sudo systemctl start sizeup-station sizeup-trunk-recorder sizeup-adsb`.

## Station logs

The station's journal goes to the hub, which writes it to CloudWatch Logs in the department's AWS account. Admins
read it on the hub: Settings > Station > Logs (#494). The whole story: [docs/station-logs.md](../../docs/station-logs.md).

| | |
|---|---|
| The reader | `sizeup-logread.service`, as its own user `sizeup-logs` (in `systemd-journal`), with no network. It hands over only the `sizeup-*` units' entries, in `/var/lib/sizeup-logs`. The `sizeup` user never reads the journal. |
| The scrub | In sizeup-station, before anything leaves: a line holding a known credential value is dropped; passwords, tokens, keys and auth headers are redacted; a scrub that fails drops its line (`sizeup/logscrub.py`). The hub runs the patterns again. |
| The adapter's lines | **Withheld by default.** Lines from the incident-source adapter stay on the station unless an admin turns on "Ship the adapter's own lines". |
| The spool | `/var/lib/sizeup/state/logspool/`: batches waiting for the hub, 50 MB at most, oldest dropped first. |
| Rejected | `logspool/rejected/`: a batch the hub will never take. Kept (the last 5), never retried. |
| Settings | On the hub, Settings > Station > Logs: on / off, the least level (default info), only / never these units, the adapter's lines, days kept. On the station: `SIZEUP_LOG_SHIP=off` in `/etc/sizeup/station.env` turns it off. |
| Retention | CloudWatch: 30 days by default (7 to 365). On the station the journal itself: 200 MB, 30 days. |
| No AWS key for it on the station | The hub writes to CloudWatch with its own role. |

## When things go wrong, it comes back by itself

| What happens | What brings it back |
|---|---|
| sizeup crashes | systemd restarts it after 5 s, forever (`Restart=always`, no start limit). rtl_fm is its child and restarts with it; whisper-server and trunk-recorder are services of their own (`sizeup-whisper`, `sizeup-trunk-recorder`, also `Restart=always`) and keep running. |
| An update doesn't come up | The updater switches back to the release that ran before within 90 s, and doesn't try that build again until the hub moves on ([Updating](#updating)). |
| sizeup hangs (running, not answering) | `sizeup-health.timer` asks `/healthz` every minute; 3 failures in a row restart it. |
| PC restart, Windows Update restart | The sizeup-wsl task starts WSL at boot, systemd starts the station, sizeup-usb attaches the dongles: a couple of minutes. |
| Power cut | The same, once the PC powers on: BIOS "Restore on AC power loss = Power On". |
| WSL stopped or updated | The keep-alive loop starts it again within 10 s (the task every 5 minutes, should the loop itself die). |
| Dongle unplugged and plugged back in | sizeup-usb attaches it again within ~10 s; sizeup finds it by serial and resumes the mode it had (one that never had one gets the next job by itself). |
| Hub or internet down | Everything queues in `/var/lib/sizeup/state/outbox.sqlite` and goes up in order when the hub is back, also across restarts (docs/cloud.md). The hub shows "Station offline since ...". |
| Missing or wrong token | The station runs and queues; its page (Settings) and `doctor` say what is missing: `sizeup-station secrets`. |
| Disk filling up | Audio older than 12 h is deleted (every 10 min); the outbox drops the oldest queued clips over 300 MB (never transcripts); the journal is capped at 200 MB. |

## Troubleshooting

Start with `sudo sizeup-station doctor`: each `FAIL` line says what to do. Then `sizeup-station logs`.

| Symptom | Look at | Fix |
|---|---|---|
| The station page doesn't open | `sizeup-station status` (`service`, `web`), `sizeup-station logs` | `sudo sizeup-station restart`. A config edited by hand that no longer loads is said in the log (unknown keys are refused). |
| The hub says "Station offline" | `doctor`: "hub reachable", "hub took the token", "clock synchronized" | The internet; `sudo sizeup-station secrets` for a wrong or missing token; the clock within 5 s of the hub. What it heard meanwhile is in the outbox and goes up later. |
| After a Windows restart nothing runs until someone signs in | `C:\ProgramData\sizeup\wsl.log`: no "keepalive: starting" line | [Starting before anyone signs in](#windows-quick-start). |
| `doctor`: "systemd isn't running" (WSL) | `/etc/wsl.conf` has `[boot] systemd=true` | In Windows: `wsl --shutdown`, or run `install-windows.ps1` again (it is the repair). |
| `doctor`: "no RTL-SDR on USB" (WSL) | `usbipd list` in Windows, `C:\ProgramData\sizeup\usb.log` | [Dongles missing after moving them to a USB hub](#dongles-missing-after-moving-them-to-a-usb-hub): the same steps. |
| A dongle's row says "held" | `~/.local/state/rtl-claims/<serial>.json` (`/var/lib/sizeup/.local/state/rtl-claims/` on the station) | Another app (shortfinal) or sizeup's own ADS-B / AIS decoder has it: switch it Off there. [docs/handoff-shortfinal-dongles.md](../../docs/handoff-shortfinal-dongles.md). |
| A red box: two dongles share a serial | `doctor`, "librtlsdr sees" | [Two dongles, one serial](#two-dongles-one-serial). |
| A dongle got the wrong job | The station page, Radios | Pick its job by hand: a person's choice is kept. [Dongles get a job by themselves](#dongles-get-a-job-by-themselves). |
| Fire/EMS 15 quiet, "trunk-recorder down" or "P25 trunk silent" | `status` (`p25:`), `journalctl -u sizeup-trunk-recorder`, `/var/lib/sizeup/trunk-recorder/trunk-recorder.log` | `sudo systemctl restart sizeup-trunk-recorder`; the dongle and its antenna. [trunk-recorder](#trunk-recorder-its-own-service). |
| An FM channel opens on noise, or misses weak carriers | The hub's Settings > Radios > Squelch: Short/min | [Squelch](#squelch). |
| ADS-B: readsb dies with `Failed to submit transfer` | `status` (`adsb:` shows `usbfs_memory_mb`) | `sudo systemctl restart sizeup-usbfs sizeup-adsb`. [ADS-B receiver](#ads-b-receiver). |
| ADS-B: `SDR wedged` | `journalctl -u sizeup-adsb` | Power: the hub's supply, the bias-tee, a USB extension. [ADS-B receiver](#ads-b-receiver). |
| Transcripts are late, or "Skipped: busy" | `sizeup-station stt` | [Speech-to-text and CPU](#speech-to-text-and-cpu). |
| An update failed | `sizeup-station autoupdate status`, `/var/log/sizeup-update.log`, `journalctl -u sizeup-update` | The station stays on the build that works and tries the next one the hub runs. By hand: `sudo sizeup-station update`, or `rollback`. [Updating](#updating). |
| "Windows scripts out of date" | The station page, `doctor` | Run `install-windows.ps1` again. |
| The adapter shows `failing` | `sudo sizeup-station adapter status` | [Incident-source adapter](#incident-source-adapter-station-only); a source that stopped itself: `adapter reset`. |
| No station logs on the hub | `systemctl status sizeup-logread`, `/var/lib/sizeup-logs/status.json` | [docs/station-logs.md](../../docs/station-logs.md). |
| The disk fills up | `status` (`disk`), `du -sh /var/lib/sizeup/*` | Audio, the outbox and the journal are capped ([above](#when-things-go-wrong-it-comes-back-by-itself)); old releases are pruned to 3. |

### Dongles missing after moving them to a USB hub

Work from the hub towards sizeup, and stop at the first step that fails.

What comes from this repo: the USB ids, the `usbipd` states, the `sizeup-usb` task and `usb.log`, `doctor`'s output and the
97% channelizer line. Steps 1 and 2 and the bandwidth figures below are general USB knowledge, not something sizeup checks.

| # | Check | Good | If not |
|---|---|---|---|
| 1 | **The hub's per-port power switches.** | Every port with a dongle is switched on. | Switch it on. A port that is off looks exactly like an unplugged dongle. |
| 2 | **The hub's power adapter.** | Plugged in, and the hub is powered from it. | Use the adapter. Several dongles, and an LNA on a bias-tee, draw more than a bus-powered hub passes on. A sagging supply shows as dongles that come and go, or readsb's `SDR wedged`. |
| 3 | **Windows: `usbipd list`** (any PowerShell). | Each dongle is listed with VID:PID `0bda:2838` or `0bda:2832` and STATE `Attached`. | See the states below. |
| 4 | **WSL: `lsusb`** (`wsl -d Ubuntu-24.04 -u root lsusb`). | One `ID 0bda:2838` (or `2832`) line per dongle. | `Attached` in Windows but missing here: `usb.log` for an `attach ... failed` line; unplug the dongle and plug it back in. |
| 5 | **`sudo sizeup-station doctor`.** | "RTL-SDR(s) on USB: N" and, under "librtlsdr sees", each dongle's index, serial and product. | A serial listed twice: [Two dongles, one serial](#two-dongles-one-serial). |
| 6 | **The jobs** (station page, Radios). | Each dongle has the job it had: jobs follow the serial, not the port. | Pick it by hand. |

The `usbipd list` states:

| STATE | Meaning | What to do |
|---|---|---|
| not listed | Windows doesn't see the dongle at all. | Steps 1 and 2, the cable, another port on the hub, the hub's own cable to the PC. |
| `Not shared` | Windows has it; it isn't offered to WSL. | Wait ~10 s: the sizeup-usb task shares and attaches it. A dongle on a new port has a new BUSID (the `usbipd list` column), and the task shares it by BUSID, so it may start here until the next pass. |
| `Shared` | Offered to WSL, not attached. | Wait ~10 s. Still `Shared`: `C:\ProgramData\sizeup\usb.log` says why the attach fails. |
| `Attached` | In WSL. | Nothing: go on to step 4. |
| listed with another VID:PID | Not an RTL-SDR the station picks up: only `0bda:2838` and `0bda:2832` are shared into WSL. | Not a dongle sizeup can use as it is. |

If dongles stay `Not shared` or `Shared`, the sizeup-usb task isn't doing its 10 s pass: `usb.log` has no new lines.
Check Task Scheduler for **sizeup-usb** (and **sizeup-wsl**: there is nothing to attach to while WSL is down,
`C:\ProgramData\sizeup\wsl.log`), or run `install-windows.ps1` again.

**USB 2.0 bandwidth.** Every dongle on one hub shares that hub's single USB 2.0 link to the PC: 480 Mbit/s in
all, less in practice (general USB knowledge: an RTL-SDR is a USB 2.0 device, also on a USB 3 hub). A P25 dongle streams 2.4 MS/s all the
time (`rate` in `deploy/trunk-recorder/config.json` and in MRRS's): 8-bit I and Q, so about 4.8 MB/s, 38 Mbit/s,
each. When the dongles on one hub ask for more than it delivers, samples are dropped: P25 decodes badly, and an
airband dongle with several frequencies logs that its channelizer runs under 97% of real time. So:

* **Put the P25 dongles (Fire/EMS 15, MRRS 770, MRRS 773) on the PC's own USB ports**, not on the hub. They stream all the time and matter most.
* If a hub must carry several dongles, use more than one hub, each on its own PC port.
* This is not the usbfs memory limit (#269, `usbfs_memory_mb`): that one is per host, and the station sets it to 0.

## Speech-to-text and CPU

### What is transcribed where

Each channel and talkgroup has its own choice on the hub (Settings > Radios > Channels: record, transcribe,
engine), synced to the station and locked on the station page while it forwards. `sizeup/channels.py`;
[docs/cloud.md](../../docs/cloud.md), "Speech-to-text per channel".

| Engine | Where it runs | By default |
|---|---|---|
| `aws`: Amazon Transcribe | **On the hub.** The station skips whisper and forwards the clip; the hub transcribes it. | South 2, FG1, FG2, Fire/EMS 15 (`tg:307`), the airband channels, and MRRS PD dispatch (`p25:mrrs:9211`): `[stt] aws_channels`. |
| `local`: whisper.cpp | **On the station**: `whisper-server` (`sizeup-whisper.service`, 127.0.0.1, `[stt] port` 8179). | Everything else that is transcribed: the other talkgroups. |
| `both` | whisper on the station and Transcribe on the hub. Transcribe's text is the transcript; whisper's is kept to compare. | Nothing. |

* An `aws` or `both` channel falls back to the station's whisper whenever AWS can't take audio (no AWS credentials,
  the monthly limit reached), and the hub asks the station's whisper again for a clip AWS failed on.
* Transcribe always runs on the hub: the station has no AWS credentials for it.
* The whisper model is the installer's `--model`: `small.en-q5_1` (default), `large-v3-turbo-q5_0` or `base.en`.

### The whisper queue

One worker, strict priority (`sizeup/stt.py`, #300): a class is served only when every class above it is empty.
At most 2000 clips wait, 400 per channel; past that the worst one goes (lowest class, then oldest).

| Class | What | If it waits |
|---|---|---|
| `urgent` | Pages, Fire/EMS 15, FG1, FG2. | After 10 min it goes behind the fresh clips of its class; after 3 h it is skipped. |
| `high` | The airband channels (the airport pre-alerts). | Skipped after 5 min: a late pre-alert is no use. |
| `live` | Other live traffic: the talkgroups marked Main. | As `urgent`. |
| `rerun` | A person's Re-transcribe. | Never skipped. |
| `context-fallback` | Another P25 system's `alert` talkgroup that is set to AWS while AWS can't take it: whisper is its only text. | Skipped after 30 min. |
| `context` | Another P25 system's `alert` talkgroups (MRRS police dispatch). | Skipped after 10 min. |
| `low` | Talkgroups heard only as context: not Main, and another system's `text` tier. | One greedy pass while whisper is behind (the oldest live clip waiting 2 min); skipped after 15 min. |
| `catchup` | Skipped clips run again later ([below](#catch-up)). | Under everything else. |

A skipped clip says "Skipped: busy" on the Radio page and can still be re-transcribed by hand. Fire traffic never
queues behind police or context traffic.

### Catch-up

Clips skipped for being busy are run again once there is room (#515, `sizeup/sttcatchup.py`). The CAD of record
drives it (the hub); the station's whisper does the work in the `catchup` class.

| | |
|---|---|
| Which clips | Skipped ones at most 6 h old (Settings > Radios > Speech-to-text on the hub: on / off and the age, 0.5 to 48 h). The alert tier first, then the newest. |
| Fire first | Nothing while an event of ours, an alarm or an emergency window is open, a carrier is open or someone is talking. Then only when the station's queue has no `urgent`, `high`, `live` or `context` clip and at most 5 clips in all. |
| On the station | Started only after 90 s with no clip of ours coming in. One greedy pass, one try, a 20 s timeout, at most 30 s of audio. 3 clips a batch, no more until they are done. |
| Tries | A failed clip is tried again after 15 min, twice in all. |
| Quiet | The text arrives like a re-run, but nothing that alerts acts on words that may be hours old. |
| Updates | Catch-up work never holds up a station update; clips still waiting are taken back while one waits. |
| Counts | The hub's Settings > Station > Health, "Speech-to-text queue (station)": waiting, caught up in the last 24 h, given up. |

Full rules: [docs/cloud.md](../../docs/cloud.md), "Speech-to-text per channel".

### CPU: the i5-6400T

Four cores at 2.2 GHz, no GPU for whisper. trunk-recorder following the P25 control channel takes
about one core all the time; each rtl_fm a little. So:

* Keep the **main channels on AWS Transcribe** (South 2, FG1, FG2, Fire/EMS 15: the default; Settings >
  Radios on the hub): fast and accurate, and the station's CPU stays free.
* whisper on the station does the rest with **small.en (q5_1)**, the default, on 3 threads.
  `large-v3-turbo-q5_0` is more accurate but several times slower: only if the station transcribes
  little. `base.en` if it falls behind.
* In Settings > Radios on the hub, **turn off transcription for talkgroups you don't need** (they can stay
  recorded): every transmission whisper skips is CPU for the ones that matter.
* `sizeup-station status` shows the load: above 4 for long, transcribe less or use a smaller model.
* whisper's cost is per clip, not per second of audio (#300: ~16 s each on this box whether the clip lasts 1 s or
  10 s, so ~220 clips an hour at most). Mark a talkgroup "main" to keep it out of the `low` class.
* whisper-server never runs more threads than the CPUs less one (3 here: more is slower, its threads wait on each
  other); readsb runs below it (nice 5), the radios above it (nice -5), the AIS decoder (nice 10) and the journal
  reader (nice 15) below all of them.
* `sizeup-station stt` says why it is behind: the model and threads, whisper's time per clip by the hour, the CPU
  use and niceness of the radios, readsb and whisper, and what was shed or skipped (`--bench clip.wav` also times
  one clip a few ways; it adds a few requests, so run it when the station is quiet).

## On a Mac

sizeup from a checkout (`python -m sizeup --config config.toml`), with `[cloud] role = "station"` and
`hub_url` in `config.toml`. The secrets go in the login Keychain of the user running it (`-w` with no
value asks for it, so it stays out of the shell history):

```sh
security add-generic-password -a sizeup -s sizeup-station-token -w
security add-generic-password -a sizeup -s sizeup-access-id -w
security add-generic-password -a sizeup -s sizeup-access-secret -w
```

Each secret is looked up in this order: the environment (`SIZEUP_STATION_TOKEN`,
`SIZEUP_ACCESS_CLIENT_ID`, `SIZEUP_ACCESS_CLIENT_SECRET`: what Linux uses), then `[cloud]` in
`config.toml` (`token`, `access_client_id`, `access_client_secret`), then the Keychain
(`[cloud] keychain_prefix` or `keychain_*` rename the services). The values are never logged or
shown: the page says only "set (env)" / "set (Keychain)" / "missing". With an Access *bypass* policy
on `/api/station/*` instead of a service token, leave the Access id and secret out: the bearer token
is then the only gate. The service token is the station, never a person: on the hub it is never
taken for an operator.

## The station page

http://localhost:8091 on the station (`/monitor` opens its Monitor). The header says **Station**, the
station's name (Port Chester FD South End; the id `pcfd-hq` on hover), the hub link's state (Hub connected / Hub unreachable / Hub not sending / Not
forwarding) and links to the CAD on the hub. Three views:

* **Radios**: the receivers, speech-to-text and the Hub row; each dongle's job (and whether shortfinal
  holds it; two dongles on one serial: a red box with the fix, [above](#two-dongles-one-serial)); the paged incident; Channels (record, transcribe, engine: set on the hub while this
  forwards, locked here); the AWS vocabulary; re-run speech-to-text; the disk.
* **Monitor**: the pages and transmissions this station heard, newest first, with the audio, the talker
  (rig and radio ID), the channel and the transcript. Channels on AWS say "Transcribed on the hub
  (AWS)": their text is on the hub. Search and a channel filter.
* **Settings**, the **Hub link** card:
  * **Forward to hub** on / off. Off: nothing is sent; radios and speech-to-text keep running, and what
    is already queued waits here.
  * **Hub URL** and the status (Connected / Offline since 12:03 / N waiting / not sending, and why), the
    last contact, what waits in the outbox, the last error.
  * Where each secret came from, or that it is missing (never a value).
  * **Test connection**: the hub's `/healthz`, then its station path with the token and the Access
    headers ("OK: hub reached, token accepted", or the HTTP code and what it means).
    `sizeup-station doctor` runs the same test.

  and **This station**: id, name, role, version, build. The rest of the checks and the logs are on the
  PC: `sizeup-station status`, `doctor`, `logs -f`.

No CAD here: no dispatch console, events, run cards, CAD lists or users, dispatch voice settings or
wall board. `/cad` comes back to this page with "This is the station. The CAD is on the hub: ...", and
the CAD's API answers `409` ("this is the station: the CAD is on the hub"). The station keeps its own
`state/cad.sqlite` for what its radios use (radio IDs for talker names, the channel choices the hub
sends, the vocabulary).

Each change of the hub link (with the Operator name from the header) is kept in the settings audit.
The choice is kept in `state/cloud.json` and survives restarts; from the first change there it wins
over `config.toml`. Keep the clock on network time: the hub warns when the two differ by more than
5 s. Deleting `state/outbox.sqlite` throws away whatever hadn't gone up yet.

## The voice

The Linux station has none (`[dispatch] tts_engine = "none"`): the hub says dispatches with Amazon
Polly. If Polly fails, the hub asks the station, which answers "station has no voice", and the
dispatch is text only, said so on the console. A Mac station can keep Kokoro (`tts_engine = "kokoro"`,
tools/setup_tts.sh) as that fallback.
