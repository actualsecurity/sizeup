# sizeup in the cloud: hub and station

To try the CAD with firefighters who aren't at the firehouse, sizeup runs in two places from the
same code and the same entry point (`python -m sizeup --config ...`), chosen by `[cloud] role`:

| | **station** (the Mac with the SDRs) | **hub** (the cloud) |
|---|---|---|
| Radios, trunk-recorder, MDC decode | yes | none: nothing opens an SDR or starts a program |
| whisper (3.8 GB), Kokoro voice (1.4 GB) | yes | none |
| Web UI: monitor `/`, CAD `/cad`, `/comms`, settings | yes (its own, local) | yes: **the CAD of record** |
| CAD database | its own `state/cad.sqlite`, not synced | `cad.sqlite` (+ `hub.sqlite`, `audio.sqlite`) |
| Network | outbound HTTPS to the hub only | reached only through Cloudflare Tunnel |

No `[cloud]` section = today's all-in-one, unchanged.

```
 station (Mac)                                                       hub (Fargate task)
 ┌──────────────────────────────────────────┐   HTTPS, outbound   ┌──────────────────────────────────┐
 │ receivers → STT → App (as all-in-one)    │   Bearer token      │ cloudflared ──► sizeup (HubApp)  │
 │   _emit ─► outbox.sqlite ─► forwarder ───┼──── POST events ───►│   same App code applies events   │
 │   talking now / status ─────────────────┼──── POST state ────►│   monitor, CAD comms, 60 Control │
 │   job runner ◄── long poll ─────────────┼──── GET jobs ◄──────│   jobs: voice, switches, re-runs │
 │     Kokoro renders the dispatch voice ───┼──── POST result ───►│   SQLite ─► Litestream ─► S3     │
 └──────────────────────────────────────────┘                     └──────────────────────────────────┘
```

## What flows

**Station → hub, in order, durable** (`POST /api/station/events`, batched): the station emits at the
same places it updates its own state (`sizeup/app.py`), and the hub applies each event through the
same App code, so the hub's monitor and CAD come out as they would locally.

| event | emitted in | carries | the hub does |
|---|---|---|---|
| `page` | `_segment` (paging channel) | the page (tones, departments, times) + its clip | `_page_heard`: listed; a tones-only page waits for its voice |
| `page_text` | `_transcribed` | the page's transcript | `_transcribed`: parses the dispatch itself → monitor incident → **pending CAD event** (`from_monitor`) |
| `traffic` | `_transcribed` | a finished transmission: times, channel / talkgroup, talker, radio IDs (P25: ID + alias per talker), transcript, model, whether it attaches to our incident + its clip | rig from the **hub's** radio registry, then `_transcribed`: monitor + CAD communications log (links to events) |
| `retext` | `_retext` | new text for a page / transmission (a re-run) | `_retext`: monitor, page dispatch, CAD log; old text kept in history |
| `heard` | `_heard` | a radio ID (and P25 alias) heard on the air | the hub's radio registry learns it |

Clips go as 8 kHz mono WAV (already band-limited to 300-3300 Hz, so nothing audible changes; half
the upload). Each event has a `seq` from the station's outbox; the hub remembers the last one applied
per outbox `epoch` (in `hub.sqlite`), so a retried batch is applied once, and pages / transmissions are
also keyed by their ids.

**Station → hub, latest wins** (`POST /api/station/state`): who is talking now, pushed the moment it
changes (sub-second; re-sent every 2 s while someone talks; the hub drops a snapshot older than 8 s),
and every 5 s the station's radio / SDR / STT / voice rows, SDR switches, re-run progress and outbox
size (also the heartbeat). The talkgroup list goes every 30 s or when a switch changes.

**Hub → station, jobs** (`GET /api/station/jobs?wait=25`, the station's long poll; the answer comes
back as `POST /api/station/jobs/<id>`), handed out once, dropped if not picked up in time:

| job | from | the station | if the station is offline / slow |
|---|---|---|---|
| `tts` | a dispatch / preview / upgrade on the hub (`Cad._render_remote`) | renders it with `render_dispatch` and its Kokoro (`say` as its fallback), returns the WAV | at once (offline) or after 20 s: **text only**, the note says "Voice unavailable — station offline" (or "didn't answer in 20 s"); never a hung request |
| `call` `set_mode`, `set_talkgroup`, `set_talkgroup_group` | the hub's SDR / talkgroup switches | runs it, returns its own refusal if any | the switch is refused with the reason ("station offline since 12:03") |
| `call` `retranscribe` | "Re-run" on the hub | queues it; new text comes back as `retext` | refused: speech-to-text runs on the station |

Settings, run cards, units, radio IDs and every CAD record stay on the hub. The station's own CAD
keeps working for local use, with a banner on its `/cad`: the CAD of record is the hub.

## On screen

* Hub, monitor header: a **Station** row ("3 s" / "offline 12:03"; the tooltip says "connected, last
  seen 3 s ago" or "offline since 12:03:41"), then the station's rows as it reported them. Offline,
  those rows turn *unknown* with what they last said: never a stale "ok". The SDR switches only show
  while the station is connected.
* Hub, CAD header: "Station connected" / "Station offline 12:03".
* Station, monitor header: a **Hub** row (connected, or offline with how many events are waiting).
  Station `/cad`: the banner with the hub's URL.

## Failure behaviour

| what fails | what happens |
|---|---|
| Internet / hub down | The station runs on as all-in-one. Events wait in `state/outbox.sqlite` (committed as they happen) and go up in order when the hub is back, also after either side restarts. The hub shows "Station offline since ...". |
| Outbox grows huge | Over `outbox_max_mb` of queued audio, the oldest clips are dropped (the event stays, marked); transcripts, pages and radio IDs are never dropped. |
| A batch is delivered twice | Applied once (seq per epoch, ids). |
| One event can't be applied | Logged and counted in the Station row; the ones behind it still apply. |
| Station offline during a dispatch | Text-only dispatch with the reason, immediately. |
| Station slow to render | After 20 s, text only; a late answer is discarded. |
| Hub redeployed | Litestream restores the three SQLite files before the app starts. Lost: up to ~1 s of the newest writes, a tones-only page waiting for its separate voice transmission, the in-memory "talking now" and station status (back within 5 s). Received clips survive (in `audio.sqlite`, 12 h). |
| Clocks differ | Transmission times are the station's clock; the hub warns in the Station row above 5 s. Keep both on NTP. |
| Late delivery | Events that arrive after an outage are applied as they arrive: the monitor attaches traffic to whatever incident is active then; the CAD links by the transmission's own time. |

## Auth and security

* Every `/api/station/*` request carries `Authorization: Bearer <token>`. The hub compares it in
  constant time (`hmac.compare_digest`) and refuses everything when it has no token configured
  (403). The token comes from `SIZEUP_STATION_TOKEN` (preferred; `[cloud] token` also works), at
  least 16 characters: `openssl rand -hex 32`. A refused request never has its body read.
* The station needs `https://` for the hub (plain http only to this machine, for tests) and never
  listens for the hub: no inbound ports, no port forwarding.
* **Cloudflare Access** in front of the hub's hostname for people (email one-time PIN or the
  department's identity provider). For `/api/station/*`, choose one:
  1. **Service token (recommended):** a second Access application on `<host>/api/station/` with a
     *Service Auth* policy for a service token. The station sends `CF-Access-Client-Id` /
     `CF-Access-Client-Secret` (`SIZEUP_ACCESS_CLIENT_ID` / `SIZEUP_ACCESS_CLIENT_SECRET`) **and** the
     bearer token. Two independent gates; nothing reaches the hub without the service token.
  2. **Bypass:** an Access *Bypass* policy on `<host>/api/station/`. Simpler; the bearer token is
     then the only gate (rate-limit that path with a Cloudflare WAF rule if you can).
  Either way the rest of the hostname (UI, CAD API) stays behind Access. Access answering the
  station with its login page shows up on the station as "hub sent something that isn't JSON: is
  Cloudflare Access letting the station through?".
* The hub binds `127.0.0.1` inside the task: only the cloudflared sidecar reaches it.
* With `access_team` + `access_aud` set, the hub itself also checks every person's Access token
  (below): reaching the origin around Access gets a 401, not the CAD.
* Transmit stays off on both (`NullTransmitter`): the hub has no radio at all.

## Who is signed in (the operator from Cloudflare Access)

> "can we get the auth info from the cloudflare access auth to identify email that was authed with
> one time code to set the operators name dynamically?" (user, 2026-10-07)

Access sends every request it lets through to the origin with a signed JWT
(`Cf-Access-Jwt-Assertion`; the browser also holds it as the `CF_Authorization` cookie). With
`[cloud] access_team` and `access_aud` set, the hub (`sizeup/cloud/access.py`, stdlib only):

* **verifies it**: RS256 signature against the team's keys from
  `https://<access_team>/cdn-cgi/access/certs` (cached an hour; fetched again for an unknown key id,
  at most every 30 s; the cached keys are kept if a fetch fails), `aud` contains the AUD tag, `iss`
  is `https://<access_team>`, `exp` / `nbf` with 60 s leeway. Any other algorithm (`none`, `HS256`)
  is refused. The `Cf-Access-Authenticated-User-Email` header is never trusted on its own.
* **requires it** on every page and API except `/healthz` (the health check) and `/api/station/*`
  (the station's bearer token): 401 otherwise. A service token's JWT (no email, `common_name` =
  client id: the station) is a machine and never an operator.
* **uses the email as the operator**: every CAD write (events, notes, dispatches, unit status, run
  cards, radio IDs, Settings edits and their audit) records the signed-in person, whatever the page
  sent (`CadApi.post`). The name comes from **Settings → Users**, where an operator can have a
  sign-in email and a callsign; someone signing in for the first time is added under their email,
  "not named yet", for an admin to name. Recorded as `Corey <corey@example.org>` (just the email
  until named): the chronology shows "Corey" with the email as its tooltip, the audit log and CSV
  exports the whole thing.
* **shows it**: `GET /api/cad/whoami` → `{email, name, callsign, source: "access" | "none", role,
  enforced}` (with Access also `active`, `may`, `bootstrap`, `admins`); the CAD's Operator field then
  shows the name, read-only ("Signed in with Cloudflare Access as ...").

Without both keys (all-in-one, the station, local dev, a hub not set up yet) nothing changes: the
Operator field is typed or picked, as before. The hub logs which it is at startup.

### Roles: who may change what

> "admins should be able to set users, dispatchers should not, but first login should be able to set
> whatever i guess... now that i added julian and myself, myself as an admin i should only be able to
> be the one to make changes?" (user, 2026-10-08)

Each user in **Settings → Users** has a role. On the hub with Access on, it is **enforced** for the
signed-in person (their row, by email), server-side, on every POST (`sizeup/cad/api.py` `WRITES`,
checked in `CadApi.post` and, for the monitor's own POSTs, in `server.py`):

| Role | May |
|---|---|
| admin | everything |
| dispatcher | run calls: create / edit / accept / dismiss / close events, notes, recommend, preview, dispatch, upgrade, attach, return, put a transmission on an event, unit status on the board, close an incident on the monitor. **No** Settings of any kind (lists, run cards, units and their home station, dispatch wording / tones / channels / voice / arrival estimates, users, import, reset), the radio-ID registry, the talkgroup / mode switches or re-running speech-to-text |
| viewer | look only (no writes) |
| deactivated (any role) | look only, "Your account is deactivated" |

A refused write is a `403 {"error": "Only an admin can change settings (you are a dispatcher)"}` (and
the like), logged on the hub (`CAD: refused POST <path> for <email> (<role>): needs admin`). A write
route not in `WRITES` is admin only until someone decides otherwise. Reads are not limited: everyone
signed in sees the console, Settings (read-only), the records, the CSV exports and the settings
export (the same users and emails Settings already shows them).

**No admin yet:** until there is an *active admin with a sign-in email*, anyone signed in may do what
an admin does, so the first person can set things up (Settings says "No admin yet: anyone signed in
can change settings. Make someone an admin."). Someone signing in for the first time is added as a
dispatcher. **No lockout:** once there is such an admin, the last one can't be demoted, deactivated or
have their email removed, and a settings import can't leave none (`409`, dry run included): make
someone else an admin first.

The console shows it (`cad.js` `applyRoles`): for a dispatcher Settings is read-only (every control
off but the section tabs, filters, records and export) under "Settings are read-only for dispatchers
— ask an admin (Corey)"; for a viewer or a deactivated user every action on the dispatch screen is
off too, under a banner. Without Access (all-in-one, the station, a hub without the keys) the role is
recorded, not enforced: a typed or picked name is no proof of who is at the console.

**The AUD tag:** Cloudflare dashboard → Zero Trust → Access controls → Applications → **sizeup** →
Overview → *Application Audience (AUD) Tag* (64 hex digits). Use the `sizeup` application's (the
hostname's), not the `api/station` service-token application's. Not a secret: it goes in
`deploy/hub/config.hub.toml` (`access_aud`) or `SIZEUP_ACCESS_AUD`.

## Config

Station (`config.toml`, see `deploy/station/README.md`):

```toml
[cloud]
role = "station"
hub_url = "https://sizeup.example.org"
# token: env SIZEUP_STATION_TOKEN; Access service token: env SIZEUP_ACCESS_CLIENT_ID / _SECRET
# outbox = "state/outbox.sqlite"
# outbox_max_mb = 300
```

Hub (`deploy/hub/config.hub.toml`, baked into the image; no secrets in it):

```toml
[cloud]
role = "hub"
public_url = ""          # or SIZEUP_PUBLIC_URL
offline_after_s = 15
audio_max_mb = 500
access_team = "yoda.cloudflareaccess.com"   # or SIZEUP_ACCESS_TEAM
access_aud = ""          # the sizeup Access app's AUD tag (or SIZEUP_ACCESS_AUD); empty = identity off

[cad]
db = "cad.sqlite"        # under SIZEUP_STATE_DIR (/data in the image)
```

Environment overrides (any role): `SIZEUP_HOST`, `SIZEUP_PORT`, `SIZEUP_STATE_DIR` (where relative
storage paths go: `cad.db`, `storage.clips_dir`, `cloud.outbox`), `SIZEUP_STATION_TOKEN`,
`SIZEUP_PUBLIC_URL`, `SIZEUP_HUB_URL`, `SIZEUP_ACCESS_CLIENT_ID` / `_SECRET`, `SIZEUP_ACCESS_TEAM` /
`SIZEUP_ACCESS_AUD`. `[cloud]` values are
only read with a `[cloud]` section: all-in-one ignores them.

Run a hub on any machine without radios:

```sh
SIZEUP_STATE_DIR=$(mktemp -d) SIZEUP_STATION_TOKEN=$(openssl rand -hex 32) \
  python -m sizeup --config deploy/hub/config.hub.toml        # http://localhost:8090/cad, /healthz
```

## The hub's storage

Everything the hub keeps is SQLite under `SIZEUP_STATE_DIR`, so Litestream can replicate it:

| file | holds |
|---|---|
| `cad.sqlite` | the CAD: events, units, run cards, dispatch log, settings, radio registry, communications log |
| `hub.sqlite` | the monitor: incidents, transmissions, pages (one row each, written only when changed), and the station events applied so far |
| `audio.sqlite` | received clips (`storage.audio_keep_hours`, 12 h, and `audio_max_mb`, oldest first) and the dispatch audio the station rendered (kept with the dispatch log; unused previews a day) |

The hub has no `captures/` and writes no JSON files. Dispatch WAVs are also cached in
`dispatch-audio/` on the task's disk; after a redeploy they're served from `audio.sqlite`.

## Packaging and the Fargate task

`Dockerfile`: `python:3.14-slim`, linux/arm64 (amd64 builds too), non-root user `sizeup` (uid
10001), `pip install .`, `data/` included, Litestream 0.5.17 pinned and sha256-checked, health check
on `/healthz` (a Python one-liner: slim has no curl). `/healthz` answers 200 with
`{ok, version, build, role, station, public_url}` while the process serves (it doesn't depend on the
station). Build with `--build-arg SIZEUP_BUILD=$(git rev-parse --short HEAD)`.

One task, desired count 1, ephemeral disk:

* **hub** container (this image). Entrypoint `deploy/hub/entrypoint.sh`:
  * `LITESTREAM_BUCKET` set: `litestream restore -if-db-not-exists -if-replica-exists` for each of
    the three files, `python -m sizeup --init-state` (creates them on a first start), then
    `exec litestream replicate -exec "python -m sizeup --config ..."` with `deploy/hub/litestream.yml`
    (bucket from `LITESTREAM_BUCKET`, prefix `LITESTREAM_PATH` default `sizeup-hub`, region
    `LITESTREAM_REGION` default `AWS_REGION`; credentials from the **task role**, no keys).
  * unset: just `python -m sizeup` (local dev).
  * Environment: `SIZEUP_STATION_TOKEN` (secret from SSM), `SIZEUP_PUBLIC_URL`, optionally
    `SIZEUP_PORT`; the image sets `SIZEUP_STATE_DIR=/data`, `SIZEUP_CONFIG`.
* **cloudflared** sidecar: `cloudflare/cloudflared:latest tunnel --no-autoupdate run`, `TUNNEL_TOKEN`
  secret from SSM. awsvpc: it reaches the hub at `http://localhost:8090` (the tunnel's public
  hostname points there). No published ports, no load balancer.
* Deploy so the old task stops before the new one starts (minimum healthy 0 %, maximum 100 %): two
  hubs replicating to the same S3 prefix at once would corrupt the backup, and two would also split
  the station's events.

`deploy/hub/compose.yaml` is a local convenience only (hub + cloudflared sharing a network
namespace, a named volume for `/data`, no Litestream).

## Code

`sizeup/cloud/`: `station.py` (StationLink: outbox, forwarder, state, jobs), `outbox.py`,
`client.py` (kept-alive HTTPS), `hub.py` (Hub: ingest, health, jobs; HubApp), `hubstore.py`
(hub.sqlite, audio.sqlite), `access.py` (Cloudflare Access identity; the gate is in
`sizeup/web/server.py`, the operator in `sizeup/cad/api.py` and `Settings.person`). Seams in `sizeup/app.py`: `_emit`, `_heard`, `_page_heard`, `_retext`,
`make_app`; in `sizeup/cad/service.py`: `remote_voice`, `audio_blobs`, `cloud`. Tests:
`tests/test_cloud.py`, `tests/test_access.py`.
