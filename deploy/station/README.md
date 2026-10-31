# The station

The station is the computer with the RTL-SDR dongles. It runs sizeup with `[cloud] role = "station"`:
the radios (rtl_fm for South 2, FG1 and FG2, trunk-recorder for the county P25 system with Fire/EMS 15),
MDC-1200 decoding, local speech-to-text (whisper.cpp) for whatever the hub doesn't transcribe, and a
link that **calls home**: everything it hears goes to the hub (https://sizeup.actualsecurity.org) over
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

## Windows quick start

On the station PC (Windows 10 22H2 or 11), signed in with the administrator account that will own
the station:

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
* installs usbipd-win (winget, else the pinned MSI, checksum checked) and shares every RTL-SDR
  (USB id `0bda:2838` / `0bda:2832`) with WSL. No Zadig, no driver change;
* power: no sleep or hibernate on AC, hibernation off (that also turns off Fast Startup, so a power-on
  is a real boot and the startup tasks run), USB selective suspend off;
* scheduled tasks, as you, at startup + at sign-in + every 5 minutes, restart on failure every minute,
  no time limit, never stopped on battery:
  * **sizeup-wsl** (`C:\ProgramData\sizeup\wsl-keepalive.ps1`) keeps the distro running. WSL stops a
    distro when no `wsl.exe` session is attached to it, whatever systemd runs inside; one
    `sleep infinity` session keeps it (and systemd, and the station) up. If that session ends (a WSL
    update, `wsl --shutdown`, a VM crash) the loop starts it again within 10 s; if the loop dies, the
    task brings it back. Log: `C:\ProgramData\sizeup\wsl.log`.
  * **sizeup-usb** (`C:\ProgramData\sizeup\usb-attach.ps1`): every 10 s, each RTL-SDR that isn't
    attached to WSL is shared and attached (`usbipd attach --wsl`). Log: `C:\ProgramData\sizeup\usb.log`
    (only changes; 1 MB, one old file kept).
* runs `install-linux.sh` inside the distro (below).

Options: `-Model large-v3-turbo-q5_0|small.en-q5_1|base.en`, `-Ref <branch or tag>`, `-MemoryGB 8`,
`-Processors 4`, `-NoPassword` (tasks without a stored password). Log: `C:\ProgramData\sizeup\install.log`.
`uninstall-windows.ps1` removes the tasks and their scripts (`-Unshare` also gives the dongles back
to Windows); WSL and the station inside it stay (`wsl --unregister Ubuntu-24.04` deletes them for good).

**Starting before anyone signs in.** With the password stored, the tasks start at boot. If on this
PC the station only comes up once someone signs in (after a restart, `C:\ProgramData\sizeup\wsl.log`
gets no "keepalive: starting" line until then), turn on automatic sign-in for that account
(Sysinternals Autologon, or `netplwiz` > untick "Users must enter a user name and password"), and
lock the screen with a password screensaver if the PC is in a public room.

Commands from Windows (any PowerShell): `wsl -d Ubuntu-24.04 -u root sizeup-station status` (or
`doctor`, `logs`, `update`, `secrets`, `restart`). `usbipd list` shows the dongles (`Attached` = in WSL).

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

## What it installs (Linux)

| What | Where |
|---|---|
| the repo, a bare clone (root's), fetched with the station's read-only deploy key (`/etc/sizeup/deploy_key`, root only) | `/opt/sizeup/repo.git` |
| one release per commit: the code (`git archive`, root-owned: the service user only reads it), its own venv, `BUILD` (the sha), `BUILDS` (the builds it runs with) | `/opt/sizeup/releases/<sha>` |
| the release that runs (a link, swapped by rename) | `/opt/sizeup/current` |
| rtl-sdr-blog v1.3.6 (RTL-SDR Blog V4 support): rtl_fm, rtl_test, rtl_eeprom, librtlsdr | `/opt/sizeup/rtl-sdr` -> `builds/rtl-sdr-blog-<tag>-<commit>` |
| trunk-recorder v5.2.0 with its plugins (unit_script) | `/opt/sizeup/trunk-recorder` -> `builds/trunk-recorder-<tag>-<commit>` |
| whisper.cpp v1.9.4 whisper-server, built for this CPU (AVX2 on the i5-6400T) | `/opt/sizeup/whisper` -> `builds/whisper.cpp-<tag>-<commit>-native` |
| whisper model, SHA-256 checked; `whisper.bin` points at the one in use | `/opt/sizeup/models` |
| config, from `config.station.toml` once and never overwritten (`config.toml.dist` = the current template) | `/etc/sizeup/config.toml` |
| secrets `SIZEUP_STATION_TOKEN`, `SIZEUP_ACCESS_CLIENT_ID`, `SIZEUP_ACCESS_CLIENT_SECRET` (root:sizeup 0640) | `/etc/sizeup/station.env` |
| what `update` fetches (repo, ref), the model, auto-update on / off | `/etc/sizeup/install.conf` |
| state: CAD, outbox, `cloud.json`, captures, trunk-recorder's working dir | `/var/lib/sizeup` |
| `sizeup-station.service`, `sizeup-whisper.service`, `sizeup-health.timer`, `sizeup-update.path` / `.timer` / `.service`, journald cap (200 MB, kept across reboots) | `/etc/systemd/...` |
| the auto-updater's record (state, last result, failed builds) and its one-line-per-update log | `/var/lib/sizeup-update/state.json`, `/var/log/sizeup-update.log` |
| the `sizeup-station` command; the launchers and the updater | `/usr/local/bin`; `/usr/local/lib/sizeup` |

The source builds are pinned to a tag **and** its commit (a moved tag is refused), built once per
pin into their own directory under `/opt/sizeup/builds`, and rebuilt only with `--rebuild`. trunk-recorder's `config.json` in
`/var/lib/sizeup/trunk-recorder` is made from the repo's `deploy/trunk-recorder/config.json` on every
run: change it there. The whisper models come from huggingface.co/ggerganov/whisper.cpp at a fixed
revision and are checked against the SHA-256 that revision lists for them. On Ubuntu 24.04,
trunk-recorder uses the distribution's librtlsdr 2.0.1 (it knows the V4 too); rtl_fm and sizeup's
dongle lookup use the rtl-sdr-blog build.

## Day to day

```sh
sizeup-station status       # service, dongles, hub link, what's waiting to go up, CPU, disk
sizeup-station logs -f      # the station's log (journalctl options pass through)
sizeup-station doctor       # every check, and what to do about each failure
sudo sizeup-station restart
sudo sizeup-station secrets # enter the token / Access service token again
sizeup-station autoupdate status     # this station's build, the hub's, the last update
```

Updates come by themselves ([below](#updating)). The config file is never overwritten: compare it
with `/etc/sizeup/config.toml.dist` after an update. The one exception is a channel a release adds
(FG2, #119): the switch to that release appends it to the config once (`sizeup/cloud/configupdate.py`),
keeps the old file beside it (`config.toml.before-fg2`) and notes it in `/etc/sizeup/config-updates`,
so a channel taken out by hand stays out. Likewise our tones as heard on the air (#47): a `[[tones]]`
entry still on the unverified Port Chester pair 349.1/311.1 becomes 903.2/810.2 and Rye Brook FD
767.4/928.1 (ours) is added after it (`config.toml.before-tones-47`); tones set by hand are left alone.
The install log says what it added. Another model:
`sudo sizeup-station update --model base.en`.

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
   parse, rtl_fm / trunk-recorder `--version` / whisper-server run. A failure: no switch, the sha is
   recorded as failed and reported to the hub.
7. **A quiet moment**: no carrier open, nobody talking, no transcription queued or running (the
   station's `GET /api/station-mode`, `update.busy`). It waits up to 5 minutes for one, then goes
   anyway.
8. **The switch**: `install-linux.sh --activate <sha>` swaps the build links and `/opt/sizeup/current`
   (each one rename), installs the release's units and commands, and restarts sizeup-station once.
   whisper-server is its own service now (`sizeup-whisper.service`) and keeps its model loaded; it is
   restarted only when its program, model or `[stt]` flags change.
9. `/healthz` must answer **with the new build** within 90 s. Otherwise the same swap back to the
   release that ran before (a rename and a restart: nothing rebuilt), and that sha is not tried again
   until the hub moves on to another one.
10. The last 3 releases are kept (the current one and the two before it), older ones and builds no
    kept release uses are deleted.

**No restart when nothing the station runs changed.** If the new commit only touches `docs/`,
`tests/`, `.github/`, the hub's own files (`deploy/hub/`, `deploy/aws/`, `Dockerfile`), the Windows
installers or `*.md` outside `sizeup/` and `data/`, the station stays as it is and counts as up to
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
only during that restart: the FM channels (rtl_fm) for a few seconds, Fire/EMS 15 until trunk-recorder
has restarted and locked on to the control channel again (#71: trunk-recorder as its own service).
Each update logs those numbers (`sizeup-station autoupdate status`, `/var/log/sizeup-update.log`:
"web back N s after the switch, radios M s"). Nothing heard is lost across a restart: the outbox
(`/var/lib/sizeup/state/outbox.sqlite`) is on disk and goes up first when the station is back.

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

## Switching over from the Mac

Only one station should forward at a time (two would send everything twice).

1. On the Mac: its station page's Settings (Settings > System > Station if it runs the CAD), turn
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

With all four covered, another dongle stays Off. The log says `sdr: V3 #2 (00000001) → FG1 (auto)`,
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

## When things go wrong, it comes back by itself

| What happens | What brings it back |
|---|---|
| sizeup crashes | systemd restarts it after 5 s, forever (`Restart=always`, no start limit). rtl_fm and trunk-recorder are its children and restart with it; whisper-server is its own service (`sizeup-whisper`, also `Restart=always`). |
| An update doesn't come up | The updater switches back to the release that ran before within 90 s, and doesn't try that build again until the hub moves on ([Updating](#updating)). |
| sizeup hangs (running, not answering) | `sizeup-health.timer` asks `/healthz` every minute; 3 failures in a row restart it. |
| PC restart, Windows Update restart | The sizeup-wsl task starts WSL at boot, systemd starts the station, sizeup-usb attaches the dongles: a couple of minutes. |
| Power cut | The same, once the PC powers on: BIOS "Restore on AC power loss = Power On". |
| WSL stopped or updated | The keep-alive loop starts it again within 10 s (the task every 5 minutes, should the loop itself die). |
| Dongle unplugged and plugged back in | sizeup-usb attaches it again within ~10 s; sizeup finds it by serial and resumes the mode it had (one that never had one gets the next job by itself). |
| Hub or internet down | Everything queues in `/var/lib/sizeup/state/outbox.sqlite` and goes up in order when the hub is back, also across restarts (docs/cloud.md). The hub shows "Station offline since ...". |
| Missing or wrong token | The station runs and queues; its page (Settings) and `doctor` say what is missing: `sizeup-station secrets`. |
| Disk filling up | Audio older than 12 h is deleted (every 10 min); the outbox drops the oldest queued clips over 300 MB (never transcripts); the journal is capped at 200 MB. |

## CPU: the i5-6400T

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
station id (`pcfd-hq`), the hub link's state (Hub connected / Hub unreachable / Hub not sending / Not
forwarding) and links to the CAD on the hub. Three views:

* **Radios**: the receivers, speech-to-text and the Hub row; each dongle's job (and whether shortfinal
  holds it); the paged incident; Channels (record, transcribe, engine: set on the hub while this
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
