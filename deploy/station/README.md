# The station

The station is the computer with the RTL-SDR dongles. It runs sizeup with `[cloud] role = "station"`:
the radios (rtl_fm for South 2 and FG1, trunk-recorder for the county P25 system with Fire/EMS 15),
MDC-1200 decoding, local speech-to-text (whisper.cpp) for whatever the hub doesn't transcribe, and a
link that **calls home**: everything it hears goes to the hub (https://sizeup.actualsecurity.org) over
outbound HTTPS, through an outbox that keeps it all while the hub or the internet is down. No port
forwarding, no inbound firewall rule. The hub is the CAD of record and says the dispatches (Amazon
Polly); the main channels (South 2, FG1, Fire/EMS 15) can be transcribed there with Amazon
Transcribe (Settings > Radios, synced to the station). The station's own page
(http://localhost:8091) is for diagnostics only. Architecture and failure behaviour: [docs/cloud.md](../../docs/cloud.md).

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
   below). Open http://localhost:8091 to see or change them, or do it from the hub's monitor.

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
nothing enabled or started, no prompts). Running it again at any time is the repair. Log:
`/var/log/sizeup-install.log`.

## What it installs (Linux)

| What | Where |
|---|---|
| sizeup: git checkout at the ref, read with the station's read-only deploy key | `/opt/sizeup/app` |
| Python venv (sizeup's dependencies; the service runs the checkout, `PYTHONPATH=/opt/sizeup/app`) | `/opt/sizeup/venv` |
| rtl-sdr-blog v1.3.6 (RTL-SDR Blog V4 support): rtl_fm, rtl_test, rtl_eeprom, librtlsdr | `/opt/sizeup/rtl-sdr` |
| trunk-recorder v5.2.0 with its plugins (unit_script) | `/opt/sizeup/trunk-recorder` |
| whisper.cpp v1.9.4 whisper-server, built for this CPU (AVX2 on the i5-6400T) | `/opt/sizeup/whisper` |
| whisper model, SHA-256 checked; `whisper.bin` points at the one in use | `/opt/sizeup/models` |
| config, from `config.station.toml` once and never overwritten (`config.toml.dist` = the current template) | `/etc/sizeup/config.toml` |
| secrets `SIZEUP_STATION_TOKEN`, `SIZEUP_ACCESS_CLIENT_ID`, `SIZEUP_ACCESS_CLIENT_SECRET` (root:sizeup 0640) | `/etc/sizeup/station.env` |
| what `update` fetches (repo, ref) and the model | `/etc/sizeup/install.conf` |
| state: CAD, outbox, `cloud.json`, captures, trunk-recorder's working dir, the deploy key | `/var/lib/sizeup` |
| `sizeup-station.service`, `sizeup-health.timer`, journald cap (200 MB, kept across reboots) | `/etc/systemd/...` |
| the `sizeup-station` command | `/usr/local/bin` |

The source builds are pinned to a tag **and** its commit (a moved tag is refused), and rebuilt only
when the pin changes (or with `--rebuild`). trunk-recorder's `config.json` in
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
sudo sizeup-station update  # fetch the ref, rebuild what changed, restart; config, state, secrets kept
sudo sizeup-station secrets # enter the token / Access service token again
```

`update` runs the *new* version's installer after fetching, so changes to the units or this command
come along. Another branch or tag: `sudo sizeup-station update --ref <name>` (remembered). Another
model: `sudo sizeup-station update --model base.en`. The config file is never overwritten: compare it
with `/etc/sizeup/config.toml.dist` after an update.

## Switching over from the Mac

Only one station should forward at a time (two would send everything twice).

1. On the Mac's console: Settings > System > Station, turn **Forward to hub** off (or stop sizeup
   there). What it had queued goes up first.
2. Move the dongles to the new station (all at once, or one by one: a dongle the Mac still has is
   simply not there for the PC). The PC's `sizeup-station status` lists each one as it arrives.
3. On the new station: `sizeup-station status` shows the hub connected; each dongle takes the next
   job as it arrives (FG1, then Fire/EMS 15, then South 2). Check or change them at
   http://localhost:8091 (or from the hub's monitor). The hub's Station row says "connected".

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

With all three covered, another dongle stays Off. The log says `sdr: V3 #2 (00000001) → FG1 (auto)`,
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
| sizeup crashes | systemd restarts it after 5 s, forever (`Restart=always`, no start limit). rtl_fm, trunk-recorder and whisper-server are its children and restart with it. |
| sizeup hangs (running, not answering) | `sizeup-health.timer` asks `/healthz` every minute; 3 failures in a row restart it. |
| PC restart, Windows Update restart | The sizeup-wsl task starts WSL at boot, systemd starts the station, sizeup-usb attaches the dongles: a couple of minutes. |
| Power cut | The same, once the PC powers on: BIOS "Restore on AC power loss = Power On". |
| WSL stopped or updated | The keep-alive loop starts it again within 10 s (the task every 5 minutes, should the loop itself die). |
| Dongle unplugged and plugged back in | sizeup-usb attaches it again within ~10 s; sizeup finds it by serial and resumes the mode it had (one that never had one gets the next job by itself). |
| Hub or internet down | Everything queues in `/var/lib/sizeup/state/outbox.sqlite` and goes up in order when the hub is back, also across restarts (docs/cloud.md). The hub shows "Station offline since ...". |
| Missing or wrong token | The station runs and queues; its page and `doctor` say what is missing: `sizeup-station secrets`. |
| Disk filling up | Audio older than 12 h is deleted (every 10 min); the outbox drops the oldest queued clips over 300 MB (never transcripts); the journal is capped at 200 MB. |

## CPU: the i5-6400T

Four cores at 2.2 GHz, no GPU for whisper. trunk-recorder following the P25 control channel takes
about one core all the time; each rtl_fm a little. So:

* Keep the **main channels on AWS Transcribe** (South 2, FG1, Fire/EMS 15: the default; Settings >
  Radios on the hub): fast and accurate, and the station's CPU stays free.
* whisper on the station does the rest with **small.en (q5_1)**, the default, on 3 threads.
  `large-v3-turbo-q5_0` is more accurate but several times slower: only if the station transcribes
  little. `base.en` if it falls behind.
* In Settings > Radios, **turn off transcription for talkgroups you don't need** (they can stay
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

## Settings > System > Station

On the station's own console (`/cad` > Settings > System), the **Station** card:

* **Forward to hub** on / off. Off: all-in-one again; radios and speech-to-text keep running.
* **Hub URL** and the status (Connected / Offline since 12:03 / N waiting / not sending, and why).
* Where each secret came from, or that it is missing.
* **Test connection**: the hub's `/healthz`, then its station path with the token and the Access
  headers ("OK: hub reached, token accepted", or the HTTP code and what it means).
  `sizeup-station doctor` runs the same test.

Each change is in the Settings audit log. The choice is kept in `state/cloud.json` and survives
restarts; from the first change there it wins over `config.toml`. Keep the clock on network time:
the hub warns when the two differ by more than 5 s. Deleting `state/outbox.sqlite` throws away
whatever hadn't gone up yet.

## The voice

The Linux station has none (`[dispatch] tts_engine = "none"`): the hub says dispatches with Amazon
Polly. If Polly fails, the hub asks the station, which answers "station has no voice", and the
dispatch is text only, said so on the console. A Mac station can keep Kokoro (`tts_engine = "kokoro"`,
tools/setup_tts.sh) as that fallback.
