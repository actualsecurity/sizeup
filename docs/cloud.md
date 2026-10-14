# sizeup in the cloud: hub and station

To try the CAD with firefighters who aren't at the firehouse, sizeup runs in two places from the
same code and the same entry point (`python -m sizeup --config ...`), chosen by `[cloud] role`:

| | **station** (the Mac with the SDRs) | **hub** (the cloud) |
|---|---|---|
| Radios, trunk-recorder, MDC decode | yes | none: nothing opens an SDR or starts a program |
| whisper (3.8 GB), Kokoro voice (1.4 GB) | yes | none |
| Dispatch voice | its Kokoro (all-in-one; the hub's fallback) | **Amazon Polly**, made on the hub ([below](#the-dispatch-voice-amazon-polly)) |
| Web UI | the **station page** `/` ([below](#the-station-page)): Radios, Monitor, Settings (the hub link). No CAD | CAD `/cad` (Dispatch, Overview, Radio, Settings), wall board `/monitor`: **the CAD of record** |
| CAD database | its own `state/cad.sqlite`, not synced: the radio-ID registry (talker names), the channel and speech-to-text settings the hub sends, the vocabulary | `cad.sqlite` (+ `hub.sqlite`, `audio.sqlite`) |
| Network | outbound HTTPS to the hub only | reached only through Cloudflare Tunnel |

No `[cloud]` section = today's all-in-one, unchanged.

### The station page

With `role = "station"` the station's own web page (http://localhost:8091 on the station PC) is about
the station only (user, 2026-10-13: "the local system doesnt show the cad bits just things relevant to
the sdr station data pulling/pushing, and maybe the monitor page too"): `static/station.html`,
`station.js`. The header says it is the station: "Station", its id, the hub link's state ("Hub
connected" / "Hub unreachable" / "Hub not sending" / "Not forwarding") and a link to the CAD on the hub.

| view | what's on it |
|---|---|
| **Radios** (`/`, `#radios`) | the CAD's Settings > Radios (`radio-controls.js`): receivers, speech-to-text and the Hub row; each dongle's job (and who else holds it: shortfinal's claims); the paged incident; Channels (record, transcribe, engine; set on the hub while forwarding, then locked here); vocabulary; re-run speech-to-text; the disk |
| **Monitor** (`#monitor`, also `/monitor`) | the pages and transmissions this station heard, newest first: play, time, talker (rig and radio ID), channel, the transcript and the model that wrote it ("Transcribed on the hub (AWS)" for channels on AWS); search and a channel filter |
| **Settings** (`#settings`) | the **Hub link** card (`hublink.js`, the same card as the CAD's Settings → System → Station): Forward to hub on / off, the hub URL, Test connection, the status, the last contact, what waits in the outbox, the last error, where each secret came from (never a value); **This station**: id, name, role, version, build, and the `sizeup-station` commands for the rest (`status`, `doctor`, `logs -f`) |

What it doesn't have: the dispatch console, events, run cards, the CAD's lists and users, the dispatch
voice settings, the wall board. `/cad` redirects to the station page (`/?from=cad`, which then says
"This is the station. The CAD is on the hub: <hub URL>/cad"; the console's old links land on the
matching view: `#settings/radios` on Radios, `#settings/...` on Settings, `#radio` on Monitor).
Every `/api/cad/...` route answers `409 {"error": "this is the station: the CAD is on the hub
(<hub URL>)", "hub_url"}`, the mirror of the hub's `409` on `/api/station-mode`, but for the three
Settings > Radios uses (`GET /api/cad/stt`, `POST /api/cad/settings/channels`, `POST
/api/cad/settings/stt`: `sizeup/web/server.py` `STATION_CAD`).

The CAD itself still runs inside the station process: its store holds what the station's radios use
(the radio-ID registry for talker names, the channel and speech-to-text choices the hub sends with
`set_channel_prefs`, the vocabulary, the communications log), and the hub's `tts` jobs render through
it. What doesn't start on a station is the CAD's state push (the whole console rebuilt and sent to
`/api/cad/stream` once a second), which nothing there would read. Hub and all-in-one are unchanged.

**Station on / off from the UI** (user, 2026-10-08: "how do we turn on and off station mode
though? id like that to be easy to do from the ui"): on any machine that isn't the hub,
**Settings → System → Station** (on a station: the station page's **Settings**) has a *Forward to hub* switch, the hub URL, the link's status, where
each secret came from and *Test connection* (`sizeup/cloud/stationmode.py`). Switching starts or
stops only the link (forwarder, state, job long poll) and the station-only parts (the "CAD of
record is the hub" banner, the Hub row and pill): radios, speech-to-text and the voice keep
running. Off keeps the outbox: what was queued goes up first when it's back on; what is heard while
off is not queued (all-in-one). The choice is kept in `state/cloud.json` and from the first change
wins over `[cloud] role` / `hub_url`, which are only the default. Each change is in the Settings
audit (section system) with the Operator's name. The hub can't be switched (a cloud deployment):
`GET|POST /api/station-mode` answers 409 there. API: `GET /api/station-mode` → `{forward, hub_url,
from, running, status: {state, text, detail, waiting, last_error, ...}, secrets: {token, access_id,
access_secret: {set, source}}, keychain}`; `POST /api/station-mode {forward?, hub_url?, operator}`;
`POST /api/station-mode/test {hub_url?}` → `{ok, status, text}` (the hub's `/healthz`, then
`GET /api/station/hello` with the token: nothing sent, the hub doesn't count it as "seen").

Secrets on the station: the environment, else `[cloud]`, else the **macOS Keychain**
(`security find-generic-password -s <service> -w`; services `sizeup-station-token`,
`sizeup-access-id`, `sizeup-access-secret`, renamed with `[cloud] keychain_prefix` or
`keychain_token` / `keychain_access_id` / `keychain_access_secret`). Never logged or shown: only
"set (Keychain)" / "set (env)" / "missing". See `deploy/station/README.md`. A Linux station (Windows
via WSL2, or a Raspberry Pi: `deploy/station/install-windows.ps1` / `install-linux.sh`) has them in
`/etc/sizeup/station.env`, which its systemd unit loads into the environment; it has no voice
(`[dispatch] tts_engine = "none"`), so a dispatch Polly can't say is text only.

```
 station (Mac)                                                       hub (Fargate task)
 ┌──────────────────────────────────────────┐   HTTPS, outbound   ┌──────────────────────────────────┐
 │ receivers → STT → App (as all-in-one)    │   Bearer token      │ cloudflared ──► sizeup (HubApp)  │
 │   _emit ─► outbox.sqlite ─► forwarder ───┼──── POST events ───►│   same App code applies events   │
 │   talking now / status ─────────────────┼──── POST state ────►│   monitor, CAD comms, 60 Control │
 │   job runner ◄── long poll ─────────────┼──── GET jobs ◄──────│   jobs: switches, re-runs, voice │
 │     Kokoro: the voice if Polly fails ────┼──── POST result ───►│   SQLite ─► Litestream ─► S3     │
 └──────────────────────────────────────────┘                     │ dispatch voice ─► Amazon Polly   │
                                                                  └──────────────────────────────────┘
```

## What flows

**Station → hub, in order, durable** (`POST /api/station/events`, batched): the station emits at the
same places it updates its own state (`sizeup/app.py`), and the hub applies each event through the
same App code, so the hub's monitor and CAD come out as they would locally.

| event | emitted in | carries | the hub does |
|---|---|---|---|
| `page` | `_segment` (paging channel) | the page (tones, departments, times) + its clip; `stt: "aws"` (no `page_text` follows) / `"both"` | `_page_heard`: listed; a tones-only page waits for its voice; "aws": Amazon Transcribe reads the voice, then the dispatch is parsed |
| `page_text` | `_transcribed` | the page's transcript | `_transcribed`: parses the dispatch itself → monitor incident → **pending CAD event** (`from_monitor`) |
| `traffic` | `_transcribed` | a finished transmission: times, channel / talkgroup, talker, radio IDs (P25: ID + alias per talker), transcript, model, whether it attaches to our incident + its clip; `transcript_pending: "aws"` (no text: the station skipped whisper) or `stt: "both"` | rig from the **hub's** radio registry, then `_transcribed`: monitor + CAD communications log (links to events); "aws" / "both": Amazon Transcribe on the clip (below) |
| `retext` | `_retext` | new text for a page / transmission (a re-run) | `_retext`: monitor, page dispatch, CAD log; old text kept in history |
| `rerun` | `_rerun_finish` | a re-run is over, whatever came of it: `result` changed / same / failed: why | `_rerun_finish`: the transmission's `rerun_at` / `rerun_result` (the ⋯ menu's Re-transcribe waits for it) |
| `alt_text` | `_set_alt` | "both" re-run: the station's whisper text as the alternative | `_set_alt`: kept to compare, the text (AWS's) stays |
| `heard` | `_heard` | a radio ID (and P25 alias) heard on the air | the hub's radio registry learns it |

Clips go as 8 kHz mono WAV (already band-limited to 300-3300 Hz, so nothing audible changes; half
the upload). Each event has a `seq` from the station's outbox; the hub remembers the last one applied
per outbox `epoch` (in `hub.sqlite`), so a retried batch is applied once, and pages / transmissions are
also keyed by their ids.

**Station → hub, latest wins** (`POST /api/station/state`): who is talking now and a page going out now
on South 2 (`paging`: each tone pair as it decodes, then "voice", then ended for 4 s once the page is
built; sizeup/radio/livepage.py), pushed the moment it changes (sub-second; re-sent every 2 s while
someone talks or a page goes out; the hub drops a snapshot older than 8 s; a hub from before ignores
`paging`, a station from before sends none),
and every 5 s the station's radio / SDR / STT / voice rows, SDR switches, re-run progress, outbox
size and its version (also the heartbeat). The talkgroup list goes every 30 s or when a switch
changes. The hub answers `{ok, build}`: its own build, which the station follows
([Station updates](#station-updates-it-follows-the-hubs-build)).

**Hub → station, jobs** (`GET /api/station/jobs?wait=25`, the station's long poll; the answer comes
back as `POST /api/station/jobs/<id>`), handed out once, dropped if not picked up in time:

| job | from | the station | if the station is offline / slow |
|---|---|---|---|
| `tts` | a dispatch / preview / upgrade on the hub when Polly failed (`Cad._render_hub` → `_render_remote`), or always with an engine other than `polly` | renders it with `render_dispatch` and its Kokoro (`say` as its fallback), returns the WAV | at once (offline) or after 20 s: **text only**, the note says why: "Voice unavailable — Polly: AccessDeniedException: …; station offline since 14:02: text only" (or "didn't answer in 20 s"); never a hung request |
| `call` `set_mode`, `set_talkgroup`, `set_talkgroup_group` | the hub's SDR / talkgroup switches | runs it, returns its own refusal if any | the switch is refused with the reason ("station offline since 12:03") |
| `call` `retranscribe` | Re-transcribe on the hub for a channel on the local model (one on "aws" runs Amazon Transcribe on the hub's clip; "both": AWS here, this for the alternative, args `[ids, true]`; AWS unavailable: here, said in the result), or Amazon Transcribe failing for a clip the station didn't run whisper on | queues it; new text comes back as `retext`, then always a `rerun` (changed / same / failed) | refused: speech-to-text runs on the station (a failed AWS clip then says "transcription failed"; a re-run ends "failed: ...") |
| `call` `set_channel_prefs` | Settings > Radios > Channels on the hub, or the station reporting other choices than the hub's (`channels.sig` in its state) | applies the whole map + whether AWS can take audio now, keeps it (its own Settings overrides, audited as "hub"), reports `{prefs, aws, sig}` | sent when it's back: the Channels card says "Station offline ...: these apply when it's back" |

Settings, run cards, units, radio IDs and every CAD record stay on the hub. The station has no CAD
pages of its own ([the station page](#the-station-page)).

## On screen

* Hub, Settings > Radios, Receivers: a **Station** row ("3 s" / "offline 12:03"; the tooltip says "connected, last
  seen 3 s ago" or "offline since 12:03:41", and the station's version: "up to date (abc1234)",
  "updating ...", "update to abc1234 failed (...)"; the word says "updating" / "update failed"), then the station's rows as it reported them. Offline,
  those rows turn *unknown* with what they last said: never a stale "ok". The SDR switches only show
  while the station is connected. A dongle whose job the station gave it by itself (`[radio]
  auto_assign`, deploy/station/README.md) reads "· FG1 · auto" (the row's `set_by`: "person", "auto",
  or missing from an older station, which then shows no "auto").
* Hub, CAD header: "Station connected" / "Station offline 12:03".
* Station page, Radios, Receivers: a **Hub** row (connected, or offline with how many events are waiting;
  "not sending" when forwarding is on without a token; its version against the hub's, as above). Its header: the "Hub connected" / "Hub
  unreachable" / "Not forwarding" pill and a link to the CAD on the hub. Settings: the last contact
  and what waits in the outbox.
* An all-in-one machine forwarding from Settings → System → Station: the same Hub row, and on its
  `/cad` the pill and the banner with the hub's URL. They go when forwarding is switched off and come
  back when it's on.

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
| A station update doesn't come up | Switched back to the release that ran before within 90 s; that build isn't tried again until the hub moves on. The Station row says "update to ... failed". |
| A station update fails its preflight | Never switched to: the station keeps running, untouched. Same report. |

## Station updates: it follows the hub's build

The Linux station (deploy/station/README.md "Updating") runs exactly the commit the hub runs:

* The hub's image is built with `SIZEUP_BUILD` = the full git sha (`.github/workflows/deploy.yml`);
  `/healthz` and every `POST /api/station/state` answer carry it. No build (a local hub): stations
  do nothing.
* The station (`sizeup/cloud/autoupdate.py`, `Watch`) writes that build to
  `/run/sizeup/update-wanted` once the hub has said it for 30 s. That file is all the sizeup user
  does; `sizeup-update.path` starts the root updater outside the sizeup process.
* The updater only takes a commit that is on `origin/main`; builds it **beside** the running one
  (`/opt/sizeup/releases/<sha>`: the code, its venv, as hard links when the requirements didn't
  change; trunk-recorder / whisper.cpp / rtl-sdr only when their pins changed); preflights it
  (imports, the config, the databases in a scratch dir, the units, the programs); waits up to 5 min
  for a quiet moment (no carrier open, nobody talking, no transcription running); swaps
  `/opt/sizeup/current` by rename and restarts once; and swaps back if `/healthz` doesn't answer with
  the new build within 90 s. A failed build is not tried again until the hub moves on. 3 releases kept.
* A commit that changes nothing the station runs (docs, tests, CI, the hub's files) doesn't restart
  it: the station counts as up to date with it. A build that breaks for a passing reason (apt lock,
  network, disk) is retried after 5, 15, 60, 60 minutes, then recorded as failed.
* Paused: `sizeup-station autoupdate off`, `[station] auto_update = false`, a station on another
  `--ref` than main, or one installed from a folder. The hub out of reach: the station stays put.
* What it did goes up in the station's status (`version`: its build, the updater's state, last
  result, recent failures) and shows in the hub's Station row (above).
* Nothing heard is lost across the restart: the outbox is on disk (`state/outbox.sqlite`, committed
  per event) and goes up first. whisper-server runs as its own service on the station, so an update
  doesn't reload the model; trunk-recorder still restarts with sizeup (#71).

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

## The dispatch voice (Amazon Polly)

> "voice unavailable... that should be server side not station local, only the transmission when
> we get there should be station local" (user, 2026-10-08) — Amazon Polly, "with a male voice".

The hub makes the dispatch voice itself with **Amazon Polly** (`[dispatch] tts_engine = "polly"`,
`voice = "Matthew"` in `deploy/hub/config.hub.toml`): a dispatch, preview or upgrade no longer needs
the station. `sizeup/polly.py` calls `SynthesizeSpeech` (`POST https://polly.<region>.amazonaws.com/v1/speech`,
16 kHz 16-bit PCM) signed with SigV4 in the standard library (no boto3); `render_dispatch` puts the
tones in front and reads it twice with the time out once, exactly as with Kokoro.

* **Credentials:** the task role, from the ECS container credentials endpoint
  (`AWS_CONTAINER_CREDENTIALS_RELATIVE_URI`), cached until 5 min before they expire;
  `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_SESSION_TOKEN` win when set (running a hub
  elsewhere). **Region:** `[dispatch] polly_region`, else `AWS_REGION` (the task definition sets it), else
  `us-east-1`. Timeouts: 3 s to connect, 10 s to answer.
* **Permission:** the task role (`sizeup-hub-task`) needs `polly:SynthesizeSpeech`, nothing else:
  run `sizeup-aws/11-polly.sh` once (an inline policy `polly`); it applies from the next task start.
* **Voices** (US English, Settings → Dispatch → Voice lists them, men first): Matthew (generative,
  neural, standard), Stephen (generative, neural), Gregory (neural), Joey (neural, standard); also
  Joanna, Ruth, Danielle. A voice is tried on its best engine first; if Polly answers that the
  engine (or the SSML) isn't supported for it — `EngineNotSupportedException` / `ValidationException`
  — the next one is used, logged once and remembered (`GET /api/cad/tts/voices` → `polly_engine`).
  Which voices have the generative engine in us-east-1 is Amazon's list and changes; the fallback
  covers an entry here that is off.
* **Speed:** `speed` (0.92) is said as SSML `<prosody rate="92%">`; an engine that rejects it says
  the text at its own pace. Over 2900 characters (Polly bills and caps 3000 a request) the text is
  split at sentence ends and the pieces joined.
* **Cache:** the WAV is keyed by the words, tones, `polly`, the voice and its first engine
  (`Matthew:generative`), speed and repeat, on disk and in `audio.sqlite` like the station's renders:
  a preview and the dispatch right after share it, and nothing is rendered twice.
* **When Polly fails** (no permission yet, network, Polly's own error), the hub asks the station
  (`tts` job, its Kokoro with a man's voice, `am_michael`): the note says so, "Voice: Kokoro
  am_michael (station: Polly: AccessDeniedException: …)". No station either: **text only**, "Voice
  unavailable — Polly: AccessDeniedException: …; no station connected: text only". The next dispatch
  tries Polly again.
* **Preview:** Settings → Dispatch → Voice → *Preview* says "Engine 60, respond to 1 Main Street,
  structure fire. Time out 14:32." through the same path (`POST /api/cad/settings/dispatch
  {action: "voice_preview"}`).
* Stations and all-in-one keep Kokoro (`tts_engine = "kokoro"`); `polly` works there too with AWS
  credentials in the environment, falling back to macOS `say` like Kokoro does.

**Seam for transmission (M2, not built):** keying a radio stays on the station. The hub already
stores and serves the rendered WAV for every dispatch (`dispatch-audio/` + `audio.sqlite`, `GET
/api/cad/dispatch/<id>.wav`); when transmit is turned on, the hub's transmitter will hand that same
WAV to the station (a job, like `tts` in reverse) for it to key up and play. The voice is never
re-made on the station for that. Until then transmit stays off (`NullTransmitter`).

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
| dispatcher | run calls: create / edit / accept / dismiss / close events, notes, recommend, preview, dispatch, upgrade, attach, return, put a transmission on an event, unit status on the board, close the paged incident (Settings > Radios). **No** Settings of any kind (lists, run cards, units and their home station, dispatch wording / tones / channels / voice / arrival estimates, users, import, reset), the radio-ID registry, the talkgroup / mode switches or re-running speech-to-text |
| viewer | look only (no writes) |
| deactivated (any role) | look only, "Your account is deactivated" |

A refused write is a `403 {"error": "Only an admin can change settings (you are a dispatcher)"}` (and
the like), logged on the hub and in the person's activity (below: `CAD activity: <email>: Refused:
settings/users (needs admin; dispatcher) (POST /api/cad/settings/users) ip=...`). A write
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

### Activity log

> "lets add some per user logging too" (user, 2026-10-08)

With Access on, the hub keeps one history per signed-in person (`sizeup/cad/activity.py`, the
`user_activity` table in `state/cad.sqlite`, so Litestream backs it up), each row with the time, the
route, the incident number when there is one, the visitor's address (`Cf-Connecting-Ip`) and browser:

| Kind | When | Summary, e.g. |
|---|---|---|
| `signin` | once per Access login (the token's `jti` / `nonce` / `iat`), not per request | Signed in |
| `action` | every write that went through (`CadApi.post`, and the monitor's own POSTs) | Created 26-000015 STRU/RESD at 209 Westchester Ave · Dispatched 26-000014: E60, L31 · Set E60 to OS (pump) · Changed settings: users/Julian role · Talkgroup 307: record on |
| `refused` | every 403 from the roles above, and the last-admin 409 | Refused: settings/users (needs admin; dispatcher) |

Summaries name what changed, never whole bodies or setting values (those stay in the settings
audit); previews and recommendations change nothing and aren't logged. Each row is also one line in
the server log (`CAD activity: <email>: <summary> [<number>] (<route>) ip=<ip>`), so CloudWatch has it.
Each person's *last seen* (time, address, browser) is kept on their Users row, written at most once a
minute. Rows older than **90 days** are dropped (on start and daily). All-in-one, the station or a hub
without Access record nothing here: the chronology and the settings audit are unchanged.

`GET /api/cad/activity?email=&kind=&since=&limit=` (newest first) and
`GET /api/cad/export/activity.csv`: an admin reads anyone's (no `email`: everyone's) and exports
it; anyone else reads only their own (`403` for someone else's, and for the CSV). **Settings → Users**
shows "last seen 3 min ago · 203.0.113.9" on each row and an **Activity** list (a kind filter, *All
users* and CSV for admins; *My activity* for everyone else).

**The AUD tag:** Cloudflare dashboard → Zero Trust → Access controls → Applications → **sizeup** →
Overview → *Application Audience (AUD) Tag* (64 hex digits). Use the `sizeup` application's (the
hostname's), not the `api/station` service-token application's. Not a secret: it goes in
`deploy/hub/config.hub.toml` (`access_aud`) or `SIZEUP_ACCESS_AUD`.

## Config

Station (optional: the station page's Settings does the same; see `deploy/station/README.md`):

```toml
[cloud]
role = "station"           # the default until the switch is first used (state/cloud.json then wins)
hub_url = "https://sizeup.example.org"
# token: env SIZEUP_STATION_TOKEN or Keychain sizeup-station-token; Access service token: env
# SIZEUP_ACCESS_CLIENT_ID / _SECRET or Keychain sizeup-access-id / sizeup-access-secret
# keychain_prefix = "sizeup"
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

## Speech-to-text per channel: whisper on the station, Amazon Transcribe on the hub

> "lets try it [AWS Transcribe], but lets make it configurable, which channel in the settings gets
> recorded ... and of which, which gets transcribed, and which transcription to use, that will let us
> test aws transcribe on the important channels like South 2 paging, Fire/EMS 15 and our fireground for
> now, and use the existing model infra for the other channels" (user, 2026-10-08)

**Settings → Radios → Channels** (admins; `sizeup/channels.py`): one row per FM channel (FG1, South 2,
at the top) and per talkgroup, each with

| switch | on | off |
|---|---|---|
| Record | the transmission is kept (clip, monitor, CAD log) and forwarded | FM: the receiver keeps running (squelch, MDC IDs, Talking now stay live), nothing is kept or forwarded. Talkgroup: its existing Record switch (calls dropped on arrival) |
| Main | (talkgroups) up front in Radio, attached to our incident | folded under Other talkgroups |
| Transcribe | speech-to-text with the Engine | kept without text ("Not transcribed") |
| Engine | **Local**: whisper.cpp large-v3 on the station (whisper-server, `127.0.0.1:8179`). **AWS**: Amazon Transcribe streaming, called from the hub with its task role; the station skips whisper and sends the clip with `transcript_pending: "aws"`. **Both**: whisper on the station and AWS on the hub; AWS's text is the transcript, whisper's is kept (`alt_text`, `alt_model`) and shown with ⇄ in the Radio view | |

Defaults (`[stt] aws_channels`): **South 2, FG1 and Fire/EMS 15 (TG 307): record, transcribe, AWS**;
every other talkgroup as before (its Record switch, transcribe, local). Stored as CAD settings
overrides (section `channels`: only what differs from the default), audited under Radios, in the
settings export / import. Group switches (Record: all / none per talkgroup section) work as before.

**Where they apply.** The hub is the CAD of record: its choices go to the station as a `call`
`set_channel_prefs` job (the whole map, and whether the hub can take AWS audio now), whenever the
`sig` the station reports in its state push differs from the hub's — after a change, after the
station was offline or restarted, when the monthly limit is reached or a new month starts. The card
says *Applied on the station*, *Waiting for the station* or *Station offline 12:03: these apply when
it's back*. On a station that forwards, the channels are read-only ("set on the hub"). All-in-one:
applied at once, AWS only if that machine has `[stt] aws` and credentials.

**An AWS channel uses whisper whenever AWS can't take audio**: the hub without `[stt] aws`, no
credentials, or **the monthly limit** (`aws_monthly_limit_usd`, default $25, editable under Vocabulary)
reached: the station is told (`aws.ok = false`) and runs whisper; the row says "AWS off: local (why)".
The cost line: *AWS this month: 214 min ≈ $5.14 of $25 limit* (billed seconds per month in the CAD
database, `meta` `aws_usage:YYYY-MM`; a new month starts at zero).

**How the hub streams** (`sizeup/transcribe.py`, stdlib: RFC 6455 WebSocket, AWS event-stream framing
with CRC32, SigV4 query signing in `sizeup/aws.py`): Transcribe bills $0.024 a minute with **15 s
minimum per stream**, so there is **one long-lived stream per channel** (`wss://transcribestreaming.
<region>.amazonaws.com:8443/stream-transcription-websocket`, en-US, PCM, 8 kHz = the clips the hub
gets, `vocabulary-name` once the vocabulary is ready). Transmissions are fed back to back (100 ms
chunks at 2× real time, `aws_pace`) with 0.3 s of silence between them; each one's place in the
stream is kept, and the words of the final results (their start time) go to the transmission they
fall in, so a result that runs over two transmissions is split correctly. A transmission is done
when no open result starts inside it and a final result ended past it, or 1.5 s after its audio went;
a result still open gets up to 1.5 s more silence, then the stream is ended (which finalizes it).
The stream is ended `aws_idle_s` (120 s) after the channel's last transmission and opened again on
the next; while it waits it gets 0.1 s of silence every 10 s (Transcribe closes a stream with no
audio for 15 s; billed as audio: ~1 % of the idle time). Near the 4-hour stream limit a new stream is
opened between transmissions. Errors (BadRequest, LimitExceeded, ServiceUnavailable, a dropped
connection): what wasn't done is tried once more in a new stream after a backoff, then the
fallback; a 403 (no permission) falls back at once.

**Never dropped.** A transmission waiting for AWS shows at once as "Transcribing"; AWS's text comes
in like a re-run (`_retext`, model `aws-transcribe`). A page waits
for AWS's text before its dispatch is parsed. AWS failing: the hub asks the station's whisper
(`retranscribe`: the station keeps every clip's whisper input); the station unreachable too: the row
says "Transcription failed" with the reason in its model. "Both": AWS failing leaves whisper's text.
After a hub redeploy what was still waiting for AWS goes to it again (or to the station).

**Vocabulary** (Settings → Radios → Vocabulary; `sizeup-pcfd`): the phrases AWS should expect —
seeded from the boards' spoken unit names (Engine 60, Ladder 31, Tower Ladder 2, Rescue 40, 23 98 …),
PCFD words (60 Control, Port Chester, Rye Brook, Fire EMS 15 …) and the 60 busiest Port Chester /
Rye Brook streets as said ("North Main Street"). AWS's rules for a phrase list: letters, apostrophes
and periods only, the words of a phrase joined by hyphens, ≤ 256 characters, **no digits**: numbers
are spelled out as said on the air ("Engine 60" → `Engine-sixty`, "23 98" →
`twenty-three-ninety-eight`), and Transcribe's "Engine sixty" is written back as "Engine 60".
*Push* sends it (`CreateVocabulary`, or `UpdateVocabulary` when it exists, SigV4 JSON API), polls
`GetVocabulary` until READY / FAILED, and new streams use it once READY.

AWS permissions: the hub's task role, from `sizeup-aws/12-transcribe.sh`
(`transcribe:StartStreamTranscription*`, the `sizeup-*` vocabularies). Credentials: `sizeup/aws.py`
(the environment, else the ECS container credentials endpoint, refreshed before they expire) —
written to be shared with the Polly voice. Config: `[stt] aws = true` on the hub
(`deploy/hub/config.hub.toml`); `aws_region`, `aws_channels`, `aws_monthly_limit_usd`, `aws_idle_s`,
`aws_pace`, `aws_vocabulary`; `aws_endpoint` / `aws_api_endpoint` point at a local fake
(`python -m tests.fake_transcribe`) for tests and dev.

## Phone pre-alert (Tapout)

> "can we bake in the ntfy.sh feed from tapout into this? lets receive that feed as a possible
> pre-alert of a possible incoming call" (user, 2026-10-08)

Tapout (repo `firehouse-ring-alerter`) is an ESP32 on the PCFD headquarters phone line. When the
line rings it posts to ntfy.sh; sizeup reads that feed (`sizeup/prealert.py`, `[prealert]` in the
config) and shows each ring as a **pre-alert**: a possible incoming call, often before 60 Control
pages it.

**Read only.** `pcfd-phone-prealert` is the fire crew's real alert channel: sizeup only subscribes
(every request is a `GET`, checked by `tests/test_prealert.py`) and never publishes to it or to any
ntfy topic. Don't post to it by hand "to test" either: it pages the crew. Tests use a local fake
ntfy server only.

| topic | Tapout sends | sizeup does |
|---|---|---|
| `ring_topic` (`pcfd-phone-prealert`) | title `STATION PHONE RINGING`, max priority, message `Incoming call on the station line at 2026-10-08 14:03:21 EDT.` (late: `Phone rang ~45s ago at ... (alert delayed by network).`); a commissioning test is `[TEST] STATION PHONE RINGING` | a pre-alert {at, source "Tapout HQ phone", message, ntfy id}; a test is shown as "Test ring" and never matched |
| `status_topic` (`pcfd-status`) | `Tapout heartbeat` hourly (`... hourly check-in, seq 12 ...`), `Tapout restarted` at boot, warnings (`Detector line STUCK active`, `ALERT DELIVERY FAILED`, `Low memory`), OTA lifecycle | Tapout's health: "Tapout: ok, heartbeat 12 min ago"; over `heartbeat_stale_min` (90) "no heartbeat for 2 h" in warning; never heard: "no heartbeat yet" |

**Where it runs:** the CAD of record only: the **hub**, or all-in-one. While a station forwards to
a hub it doesn't subscribe (logged): the hub does on its own, so it works whether or not the station
is connected, and nobody gets two alerts. Switching forwarding (Settings > System > Station, or the station page's Settings) starts
or stops it at once (`App.prealert_sync`; a restart resumes after the last message).

**The stream:** one thread per topic on `GET <server>/<topic>/json` (ntfy's line-per-event JSON:
`open`, `keepalive` about every 45 s, `message`). A dropped stream reconnects with backoff (1 s
doubling to 60 s; a `429` waits at least 2 min, or `Retry-After`, up to 10) and resumes with
`since=<last message id>`. The first connection asks `since=10m` for rings (a ring just before a
restart isn't lost, old ones aren't news) and `since=90m` for the status topic (the last heartbeat).
Rings are deduplicated by ntfy id in the database; one received more than `expire_min` after it rang
is recorded as expired ("received late"), never shown as live.

**On screen:** the open pre-alerts sit at the top of the CAD's **60 Control** column, a red blinking
row "Phone ringing · Tapout HQ phone · 14:03:21", its age ticking, with **Dismiss**; Tapout's health
is in that column's header (its tooltip: the last heartbeat and restart, a recent warning, a feed
that isn't connected). The header shows a **Phone ringing 0:42** badge.
Sound: the header's **Alerts** panel has *Phone pre-alert* (on by default, this browser): a toast, and
the chime when *Chime* is on. The wall board (`/monitor`) has the
same header badge and the open pre-alerts, read-only, above its 60 Control feed.

**How each ends** (each in the CAD event log, kind `PRE-ALERT` in `event-log.csv`):

| outcome | when |
|---|---|
| matched | a 60 Control page for Port Chester (`Cad.from_monitor`) or a new emergency event (`Cad.create`) is made while it is open (within `expire_min`), or up to 2 min before the ring. Every open pre-alert is linked to that event, whose chronology says "Phone pre-alert 14:03:21 matched: Tapout HQ phone rang 95 s before this event (60 Control page)". Non-emergency events don't match. |
| dismissed | `POST /api/cad/prealert/<id>/dismiss {reason?}`: dispatchers and admins (hub with Access: enforced, the person's activity says "Dismissed the phone pre-alert of 14:03:21") |
| expired | nothing within `expire_min` (5) |

Pre-alerts are in `cad.sqlite` (`prealerts`, so Litestream keeps them): the last 24 h in the state,
90 days kept. **Records → CSV → Phone pre-alerts** (`/api/cad/export/prealerts.csv`): each ring with
its outcome, when, the seconds from ring to outcome and the event number, for reviewing ring → call.

```toml
[prealert]                       # leave the section out: off
enabled = true
server = "https://ntfy.sh"       # SIZEUP_PREALERT_SERVER overrides (a local test server)
ring_topic = "pcfd-phone-prealert"
status_topic = "pcfd-status"
expire_min = 5
heartbeat_stale_min = 90
```

## Highway layers: cameras, incidents, signs, mileposts (the Thruway, 511NY)

> "it looks like this has location data of all the highway things, including cameras, that would be
> nice to add in for the ability to @ and then also, click on camera feeds available from the maps
> (maybe there is a layers option to turn on these overlays too on the map)" (user, 2026-10-13, on the
> Thruway's traveler map; GitHub #73). Then, #86: "they exist on that other website so we should be
> able to enumerate them without ny511".

Every CAD map (the event's, the large map, the Overview, `/monitor`) has a **Layers** button:
*Cameras*, *Incidents & closures*, *Roadwork*, *Message signs* (from the sources below), *Mileposts*
and *Exits* (from the gazetteer, #67, #83), and *Airport* with *Approaches*
([the airport](#the-airport-hpn-the-airport-layer-places-alerts), #87). Each is on or off per viewer (this browser's
`localStorage`, key `sizeup.mapLayers`); cameras and incidents start on. Signs show from zoom 11,
exits and whole mileposts from 12, the tenths from 14. A camera opens the **camera viewer**: live video where the source streams
(the Thruway's cameras; muted, see below), else the snapshot, with its name, road, direction and
milepost, which source it is from and when the image was taken, a new snapshot every 10 s while it
is open and the tab is visible (paused when hidden); it fits a 390 px phone. An incident, closure, roadwork or sign opens a small box with what the source says and
when it was updated. An event with a map point has a **Cameras** panel under its map (#95: "add
that as an automatic option on the incident viewer to show the camera live feed, even if it's just
past or just before where it came in, as immediate reference points"): on a highway we have mile
markers for (I-95, I-287, the Hutch, US-1, I-684, the 120s: the location is a marker or exit, or
the point is within 150 m of the road's markers), the camera **just before** and **just after** the
event along the road, by our markers, in its direction of travel when the location says one ("0.3 mi
before (SB) · I-95 at Interchange 21"; a camera over the event's carriageway wins over one up to
0.3 mi nearer and is marked as facing), else each side by compass ("0.4 mi south"); within 2 mi,
nothing shown beyond that. Then **Nearby**, lower: cameras within 300 m as the crow flies (off a
highway, that alone). The panel is collapsible, open while the event is open, with live snapshots
(a new one every 10 s while the page is visible); a click opens the viewer. The wall board shows
the two along the road for the active highway event, small, above its cards.
`GET /api/cad/cameras?for=lat,lon&label=&address=` → `{highway: {road, dir, mile} | null, cameras:
[{..., rel: before | after | at | side | nearby, miles, side, facing, km}]}` (`Roads.cameras_for`). The Layers box names each source that is on and when it
last read: *Data: New York State Thruway Authority, updated 16:48*, *powered by 511NY, updated 16:49*.

### Sources

Two providers (`sizeup/roads.py` `Provider`), read side by side and merged (`Roads.merged`): when
both are on, **511NY wins per item**: a Thruway camera, sign or event within 50 m of a 511NY one (the
same layer, for events) is left out as the same thing. Every item carries its `source` (`"511NY"` or
`"Thruway"`), shown on the map, in the camera viewer and in "Cameras nearby".

**The New York State Thruway Authority** (`[roads] thruway = true`, the default; no key): the public
data host its traveler map reads, `webdataapi.thruway.ny.gov/api/data/<feed>/xml`
(<https://www.thruway.ny.gov/developers>): `trafficcameras` (the camera list, with each camera's
snapshot and video URLs, milepost and which way it looks), `events` (incidents and roadwork; a
closure is read from the wording: "all lanes closed", "ramp closed", "closure"), `dmsstatus` (the
message signs' text). I-95 (New England Thruway) and I-287 (Cross Westchester Expressway) are
Thruway roads, so this covers our two highways with no sign-up at all: 15 cameras, 8 signs and the
day's events in our area on 2026-10-13. Checked that day: all three feeds (and the interchange and
milepost ones) answered a plain client, `curl` and Python's `urllib`, with the User-Agent below, HTTP
200 with no key, cookie or browser headers; so did the snapshot host (`nyssnapshot.com`, one
redirect, `image/png`, with a `Last-Modified`). Nothing is done to look like a browser: if the host
ever refuses plain clients, that feed stays off and the log says so.
*Terms:* the site's [disclaimer](https://www.thruway.ny.gov/legal/disclaimer) says the data "is not
to be sold, or used in any process for resale as a value added product, or otherwise distributed for
profit"; this is a fire department's non-commercial dispatch tool, shown to its members, with the
source named on the map, in the Layers box and in the viewer ("Data: New York State Thruway
Authority", linked). Polled gently: the camera list hourly, events every 2 min, signs every 5, with a
real User-Agent, `sizeup/<version> (Port Chester FD dispatch; contact
https://github.com/actualsecurity/sizeup)`. The Thruway's **milepost and interchange feeds are not
read** for the map: the gazetteer's own mile markers are (#67, #83; the Thruway's were used once to
cross-check them, see #83).

**Westchester County DPW** (`[roads] county = true`, the default; no key): the county's traffic
cameras, from the public ArcGIS feature layer its map reads
(`giswww.westchestergov.com/arcgis/rest/services/MappingWestchesterCounty/MapServer/124`, no token;
public viewer <https://www.westchestergov.com/trafficcamera>), 51 cameras countywide, 26 in our
area: the King St / Willett Ave / N Pearl St approaches on the Port Chester–Rye Brook line, Playland
Pkwy at Forest Ave, Mamaroneck Ave at the Hutch ramps, I-287 at White Plains Ave, the Bronx River
Pkwy set. The list is read daily; the images (`www.westchestergov.com/cctv/camera<id>.jpg`,
1280×960, a new one every 8 s with its `Last-Modified`) go through the proxy like the others. A dead
county camera serves a placeholder last modified in 2021, or a 404: the hub never shows those (below).
Each camera's direction comes from the layer's description ("Traffic Approaching the Camera is
going South on King St"). *Terms:* the ArcGIS item's disclaimer (a public service, no liability, not
recorded), no reuse restriction; the county's own release says the cameras are for rapid response
to accidents and weather emergencies. Attribution "Westchester County DPW" in the Layers box and the
map. When 511NY arrives it wins the 50 m dedupe (the county's cameras are in 511NY too).

**511NY** (NYSDOT, <https://511ny.org/help/24>; off until approved): the Developer Access Agreement
(updated 2026-09-18) lists traffic events (JSON), the camera inventory (XML), message signs (JSON)
and winter road conditions. Access is by request (the *XML Data Feed Request Form* linked there,
reviewed weekly); the approval brings the feed addresses, their documentation and a key. So the
addresses are config (`[roads] events_url`, `cameras_url`, `signs_url`, each with `{key}` where the
key goes) and the key a secret (`SIZEUP_511NY_KEY`; on the hub SSM `/sizeup-hub/511ny-key`). Each
feed may be JSON or XML; records are read by field name (`ID`, `RoadwayName`, `DirectionOfTravel`,
`Latitude`, the inventory's `Views [{Url, VideoUrl}]` ...), whatever the case. *Terms:* no fee; we
may "redistribute, enhance, repackage, and add value to the data" but not alter what a report says;
we "must display the time at which the data was published or last updated wherever [we] present it"
(every event, sign and snapshot shows its time); the source may be acknowledged as "powered by 511NY"
with a link to 511NY.org (the viewer, the Layers box, the map's attribution); the name and logo are
not ours to brand with. NYSDOT may stop the feed or a use at any time. The old developer API
(`511ny.org/api/getcameras?key=`) is gone since the site was rebuilt in 2026: those paths answer the
site's HTML page. Schedule: events every 90 s, signs every 5 min, the inventory hourly; the config
refuses anything faster than 5 calls a minute for all three (the old API's limit was 10 a minute).

Considered and not used:

| source | why not |
|---|---|
| 511ny.org's own map endpoints (`api-511x-nysdot.carsprogram.org`) | undocumented: the site's, not for us |
| data.ny.gov | Thruway interchanges (`7jkf-259w`), travel plazas, gantries; archived 511 events: no live cameras or events, and the static data is the gazetteer's |
| NYSDOT Reference Markers (ArcGIS) | mile markers: static, in the gazetteer (#67, #83) |
| CTroads (Connecticut, I-95 in Greenwich) | the same kind of keyed developer API (events, signs, advisories, 10 calls a minute); its API documents no cameras and its terms page could not be read: later, if CTDOT confirms |

Camera video (HLS) is not proxied from either source: snapshots only (neither source's terms speak
to re-serving video, and every segment would come through the hub).

### Cameras that are down

A camera is **down** when its latest image is older than 5 min (by `Last-Modified`: the county's 2021
placeholders) or its snapshot fetch fails 3 times running (a 404, a timeout); a fresh image brings
it back on its own. The hub looks at every camera every 15 min (`Roads.sweep`), so the map knows
before anyone opens one. Down cameras are obvious and out of the way: on the map the icon is dim
with a small red "!" badge (and *Hide down cameras (N)* in the Layers box hides them, off by
default, per viewer); in the incident viewer's Cameras panel and the before/after picker they are
never shown as a stale thumbnail: the live ones come first and the down ones fold into a one-line
"N cameras down" row (expandable, each with how far and since when); the viewer shows *Down since
hh:mm (last image hh:mm)* rather than the old picture (`GET .../image` answers 404 `{error: "no
current image", down, down_since, last_image}`). Each camera in `/api/cad/roads` carries `down`,
`down_since`, `last_image` and `cadence` (how often its source's snapshots change: the county 8 s,
the Thruway and 511NY about 60 s; the viewer says *Snapshot, updated every N s*).

### Live video

> "can we do live feeds, instead of just screenshots?" (user, 2026-10-13; #101)

Per provider: the **Thruway**'s camera list gives each camera an HLS stream (`videourl`, on
`nysdot.skyvdn.com`: 512×288 H.264, about 205 kbit/s, 6 s segments; it answers a plain client with
no key, CORS open); checked 2026-10-13. **511NY**'s inventory has a `VideoUrl` for some cameras
(kept as `video`; untested until access is approved). The **county**'s cameras are JPEG only, every
8 s. The terms are those of the pictures (the Thruway's disclaimer: no resale or redistribution for
profit); the stream is re-served only to signed-in members, only while one of them is watching.

The viewer plays a stream **muted** (hls.js 1.6 from cdnjs, or Safari's own HLS), through the hub:
`GET /api/cad/cameras/<id>/stream/playlist.m3u8` (the camera's master playlist), `.../<chunklist>.m3u8`,
`.../<segment>.ts` (`Roads.stream`). The hub fetches only what a playlist named, as a plain file name
in the stream's own folder; a playlist that names anything else is refused. A playlist request opens
or keeps the camera's stream; segments are served only while one was asked for within 30 s, so a
closed viewer stops the flow by itself (the player stops polling); a hidden tab pauses the player
and its polling. **At most 4 streams at once, hub-wide** (the 5th gets 429 and that viewer shows
snapshots); the viewer itself shows one camera at a time. Any failure (no HLS support, a stream that
won't play, the cap) falls back to the snapshots and says so for a few seconds. The viewer always
says which it shows: **Live** (only once the video is actually playing), or **Snapshot, updated
every N s** with the source's real cadence (the county 8 s, the Thruway and 511NY about 60 s).
Thumbnails (the Cameras panel, the board) stay snapshots.

Measured 2026-10-13 on a Mac, a hub with the real Thruway feeds and two streams open for 60 s
through the proxy (a client polling each chunklist every 6 s and fetching each new segment, like
hls.js): 13 segments, 1.15 MB served, 18.7 KB/s; the hub process used 2.7 s of CPU over the 95 s run
including its feed refreshes and health sweep, about 3% of one core; memory steady around 300 MB
RSS. Each stream is at most ~26 KB/s (205 kbit/s) each way, so the 4-stream cap is ~105 KB/s out
and in, well within a 0.25 vCPU Fargate task; the pass-through does no decoding. One stream origin
answered 502 at open during the run: that is the fallback path (snapshots) working.

### How it works

Each provider fetches its feeds on its own schedule and keeps only our area (`[roads] bbox`: Port
Chester, Rye Brook and the mutual-aid neighbours with a margin, Greenwich included); one disk cache
(`roads511.json` in the state dir) holds what each kept, items only, so a restart spends no calls.
A refused key (401 / 403) waits 30 min, a 429 5 min, anything else backs off 1 min doubling to 15,
never under the feed's floor; one provider's error never touches the other's data.

**The 511NY key never leaves the hub.** It is not in any response, the cache, an error or the log
(`_redact`; `tests/test_roads.py` checks each). **The browser never talks to a provider or a
camera**: `GET /api/cad/cameras/<id>/image` is fetched by the hub from the camera's snapshot URL as
its provider lists it, kept 15 s (one fetch however many are watching; four cameras at a time at
most), and served with `X-Image-Time` (the camera's `Last-Modified`, else when the hub fetched it).
A camera that stops answering gets its last snapshot for up to 10 min, marked stale. So the key
stays private, and there is no mixed content or cross-origin request behind Cloudflare Access.
Anyone signed in may look (viewers too): these are all GETs.

| | |
|---|---|
| `GET /api/cad/roads[?rev=]` | `{configured, note, sources: [{name, short, label, link, configured, at, status: {cameras, events, signs: {on, at, n, error}}}], rev, cameras, events, signs, mileposts, exits, highway_places}`; the same `rev`: `{rev, same: true}` (the pages poll each minute). `configured`: any source is on; `note`: why 511NY isn't, when it isn't. Each camera, event and sign has `source` and, from the Thruway, `mile`. |
| `GET /api/cad/cameras?q=&near=lat,lon&n=` | `{cameras}`: cameras for the place search, or the nearest to a point |
| `GET /api/cad/cameras/<id>/image` | the snapshot (`image/*`), `X-Image-Time`, `X-Image-Stale: 1` when old; 404 no source on / no such camera, 502 the camera didn't answer |

**@ cameras.** `Roads.search_places(q)` answers "@I-95 exit 21 camera", "@287 mile 2.5 camera" in
the place search's shape (`{kind: "camera", label, sub, lat, lon, source, camera}`), and the app
hands it to the CAD as `cad.place_providers`, for #67's `/api/cad/places` to ask. In the page,
`MapLayers.searchPlaces(q)` and `MapLayers.openPlace(place)` do the same, and any element with
`data-camera="<id>"` (a camera chip in a note) opens the viewer.

**Without any source** (`thruway = false` and no 511NY key) nothing is fetched, the log says so
once, the four layers are greyed in the Layers box with the reason, and the mileposts and exits
still work. With the Thruway alone (the default until 511NY approves), the layers are on and the
box notes that 511NY isn't.

**Setting up 511NY:**

1. Request access: the *XML Data Feed Request Form* linked from <https://511ny.org/help/24> (reviewed
   weekly). Describe the use: Port Chester FD's dispatch console, shown to its members behind sign-in;
   cameras, events and signs for the area around Port Chester; no third parties. The approval brings
   the feed addresses, their documentation and a key.
2. The key into SSM (from the `sizeup-aws` folder's environment):
   `aws ssm put-parameter --name /sizeup-hub/511ny-key --type SecureString --value '<the key>'`
3. Then, and only then (a task whose secret is missing doesn't start), add it to the hub container's
   `secrets` in `deploy/aws/taskdef.json` (and `sizeup-aws/7-deploy-pr.sh`'s copy):
   `{"name":"SIZEUP_511NY_KEY","valueFrom":"arn:aws:ssm:us-east-1:504804195561:parameter/sizeup-hub/511ny-key"}`.
   The execution role may already read `/sizeup-hub/*` (`2-iam.sh`).
4. The addresses into `deploy/hub/config.hub.toml` `[roads]`, `{key}` where the key goes (never the
   key itself: the config refuses it). Re-record `tests/fixtures/511ny/` from the real feeds.

```toml
[roads]                          # sizeup/roads.py; leave out: the Thruway and the county on, 511NY off
thruway = true                   # the NYS Thruway Authority's public feeds: I-95, I-287 (no key)
county = true                    # Westchester County DPW's cameras (no key)
events_url = "https://.../events?key={key}&format=json"     # 511NY, from its approval
cameras_url = "https://.../cameras?key={key}"
signs_url = "https://.../messagesigns?key={key}&format=json"
bbox = [-73.80, 40.92, -73.56, 41.10]   # west, south, east, north
events_every_s = 90              # 511NY; at least 60 (the Thruway: every 2 min, fixed)
signs_every_min = 5              # at least 1
cameras_every_min = 60           # at least 10
# key: env SIZEUP_511NY_KEY (the hub: SSM /sizeup-hub/511ny-key); enabled = false turns everything off
```

## The airport (HPN): the Airport layer, places, alerts

> "the airport is part of Port Chester / Rye Brook area, we respond (Port Chester) to their L2+ alerts"
> (user, 2026-10-13; GitHub #87)

**On the map.** The Layers box has **Airport** (the field's outline at any zoom; from zoom 12 the runways
with their numbers at the ends, the taxiways, the aprons; from 13 the hangars and other buildings and the
points of interest: the terminal, the FBOs, the control tower, the Command Post / EOC staging area, the
two entrances) and, under it, **Approaches** (off by default: the final approach paths to runways 16 and 34
from the FAA CIFP and the fixes on them, labelled, CZIMR and ISLET among them, so "inside CZIMR" can be
read against the map). Both are remembered per viewer like the other layers; the Airport layer also comes
on by itself, on that map only, for an event at the airport (within 1.5 km of the field, or typed Airport),
marked "event here" in the box until unticked, and the event's map takes in the whole field. The data is
`data/khpn_airport.geojson` (`GET /api/cad/airport`; `tools/fetch_airport.py` from the OSM and FAA files in
`data/airport/`, sources in the README), drawn by `sizeup/web/static/airport.js`, which `maplayers.js` lists
and switches.

**Places.** The "@" search and the location box know the airport's places (category *airport*): `@hpn`
lists them all; `@staging way`, `@airport command post` or `@airport EOC` find the **Airport Command Post /
EOC (staging)** at 1 Airport Emergency Staging Way (the NYS address point: the spot is to be confirmed on
the ground); `@runway 16`, `@taxiway B`, `@gate C`, `@million air`, `@terminal`, `@airport entrance`. The
ARFF station and Signature's office are not in the data: add them in Settings > Places when known.

**The event.** Type **Airport** with the FAA's three alerts (AC 150/5200-31C, *Airport Emergency Plan*,
para 7-1-4a) and the other calls PCFD has had at the field: *Alert 1 (local standby)* (an operational defect
that should not normally cause serious difficulty: notification only, ARFF stands by; PCFD normally not due,
to confirm; no run card), *Alert 2 (full emergency)* (a defect with danger of an accident: fire on board,
unsafe gear, hydraulics, an engine out; all units to their positions, the airport closed after the landing),
*Alert 3 (aircraft accident)* (an accident on or in the vicinity of the airport), *Aircraft accident off
airport* (Rye Lake 2023; dispatched as an Alert 3 or as a normal call: to confirm), *Aircraft in the water /
water rescue* (Rye Lake, Kensico, Blind Brook), *Fuel spill / hazmat* (the tanker rollover of December 2025),
*Aircraft fire / ground emergency* (APU, brakes, a cabin fire at the gate), *Structural fire on the airport*
(terminal, hangar, fuel farm), *Disabled aircraft / runway excursion* (2021), *Bomb threat / security*,
*Hijack* (whether FD is dispatched: to confirm), *Other*. Each subtype carries its definition (`help` in
`data/cad_lists.json`): on the subtype pick-list as a hover, behind "?" on the event and in Settings. The
list and the wording are a proposal marked "to confirm" like the other guesses (#105); the spoken phrases are
terse ("an Alert 2, full emergency, at Westchester County Airport", "an aircraft accident near the airport",
"an aircraft in the water", "a fuel spill at the airport").

**Alert vs Level.** The alert is the FAA's (1 / 2 / 3). Westchester responders overlay a **Level 1–5** on
the souls on board (1–2 / 3–10 / 11–25 / 26–50 / 51+, reported by responders, unofficial), so "Alert 2 Level
3" can be heard. The CAD keeps both and never derives one from the other: "Level N" alone is the souls
Level, never the alert; the dispatcher picks the alert.

**Run cards** (the department, 2026-10-13: "put Engine 64, 62 and Rescue 40 on the Alert Level 2, and FDR on
the 3 and up"): **Alert 2** = Engine 64, Engine 62, Rescue 40, by name, in that order; **Alert 3** = the full
department response. Both are the department's own (`confirmed` in the data file: seeded as not a default,
shown with a *department* chip instead of *default* / *edited*, no "not PCFD policy" banner for them; Reset
puts them back as given; a database seeded with #96's stand-ins gets them on the next open unless someone
edited those). "And up": *Aircraft accident off airport* and *Aircraft in the water* take Alert 3's card
(plus Marine 40 for the water) as a proposal, marked to confirm. Alert 1 has no card. Every card's remarks
line carries "Staging: Airport Command Post / EOC, 1 Airport Emergency Staging Way" (the NYS address point,
to be confirmed on the ground). The dispatch for an Alert 2 reads "Port Chester Fire, Engine 64, Engine 62,
Rescue 40, respond to 240 Airport Road, for an Alert 2, full emergency, at Westchester County Airport. Time
out 14:32."

**What the tower passes.** An Airport event carries the ARFF notification (AC 150/5200-31C): **aircraft
type**, **nature of the emergency**, **minutes out**, **runway** (16 / 34 / 11 / 29), **souls on board** with
the **Level** it implies (set from the souls unless said), **fuel on board**, **dangerous goods**. The event
view shows them in one compact row under the location ("Aircraft Boeing 737 · Nature unsafe gear · ETA 12 min
· RWY 16 · 140 souls (Level 5) · 12,000 lb fuel · DG: none"), with the subtype's definition behind "?"; Edit
(dispatchers and admins; the new-event form too) has a field for each, the Level following the souls as they
are typed. The queue, the Overview's cards and the wall board show the same line ("Alert 2 · Boeing 737 ·
unsafe gear · 12 min · RWY 16 · 140 souls (Level 5)"); `incidents.csv` has it as a last column, *Airport*.
Stored on the event (`acft_*` columns, added to an older database on open; older events have none) and in
`POST /api/cad/incident` as `acft_type`, `acft_nature`, `acft_eta`, `acft_runway`, `acft_souls`, `acft_level`,
`acft_fuel`, `acft_cargo`.

**Pages and the talkgroup.** A 60 Control page for us (by tones, #84) or a Fire/EMS dispatch that names the
airport ("Westchester County Airport", "the airport", "HPN"), an alert ("Alert 2", "alert two", "Alert II",
"alert level 2", "full emergency", "local standby") or an aircraft emergency ("inbound with an emergency",
"declared an emergency", "emergency landing", "unsafe gear", "engine out", "smoke in the cockpit", "hydraulic
failure", "bird strike") opens the queued event as Airport (`sizeup/cad/airport.py` `heard()`): the alert
when one was said; an accident ("plane down", "aircraft accident", "crash") as an Alert 3 on or near the
field, *off airport* when the page puts it off the field (Rye Lake, Kensico, "near the airport", a street),
*in the water* when it does; a fuel spill, a hazmat condition, an aircraft fire on the ground, a hangar or
terminal fire, an aircraft off the runway, a bomb threat, a suspicious package, a hijack: that subtype. An
aircraft emergency with no alert said is left for the dispatcher with the nature as the note ("unsafe gear:
alert level not stated"); "Level 3" alone adds "the souls-on-board level, not an alert". The page's facts fill
the fields (`fields()`: "a Boeing 737", "12 minutes out", "runway 16", "140 souls on board" / "POB" /
"persons on board" / "48 pax 4 crew", "12,000 pounds of fuel", "no dangerous goods"); a repeat of the page
fills what was still empty. "240 Airport Road" alone is not the airport: a fire alarm at the terminal stays
an alarm. While exactly one Airport event is open, every transmission on the **Airport Incident** talkgroup
(333) is linked to it (reason "Airport TG"), whoever is talking; two open and it can't tell whose, so none; a
link a person removes is not put back.

**The airport's own units.** Airport 7, Airport 11, Airport 17 (ARFF, Airport Operations, county department
259) and Car 2591 are on a folded *Westchester County Airport (ARFF)* board as mutual-aid units (never on a
PCFD run card, never picked by kind), so a P25 talker alias "AP7" or "Airport 7" on the Airport Incident
talkgroup resolves to the unit and the boards say "Airport 7" is talking. Their P25 radio IDs are not known:
nothing is guessed; a radio heard with such an alias gets a "heard" row in the registry, mapped when a person
confirms it.
## County GIS layers (Westchester County GIS)

> "add Westchester County GIS open-data datasets as map layers ... there are a lot of data points,
> let's focus on the important ones for fire service" (user, 2026-10-13; GitHub #91)

Every CAD map's **Layers** box has a **County GIS** group: Westchester County GIS open data as map
layers, one toggle per dataset under a folding sub-heading (*Parcels*, *Boundaries*, *Utilities*,
*Facilities*, *Hazards*, *Site details*, *Transportation*), each on or off per viewer (this browser's
`localStorage`, key `sizeup.gisLayers`; the folds are remembered too). Only the fire districts start
on. A layer with a minimum zoom says "zoom in" below it. A click on a feature opens the same small box
the highway layers use, with the configured fields as the county names them (a parcel: address, unit,
municipality, owner, class, acres, living area, year built, style, heat, fuel, tax map number), the
dataset's date and the source line. Our own fire district (`[cad] monitor_departments`: Port Chester
FD, Rye Brook FD) is drawn in the department's red; a fire station of ours likewise. *Buildings* can
be extruded by their height: the "3D" row in the group tilts the map. The attribution "Westchester
County GIS" is on the group and in the map's corner.

**Source: Westchester County GIS** (<https://gis.westchestercountyny.gov>, an ArcGIS Hub; the data
from the county's ArcGIS Server at `giswww.westchestergov.com/arcgis/rest/services/`). Surveyed
2026-10-13; the portal's categories are Basemap Planimetrics, Boundary, Tax Parcels, Infrastructure,
Environment & Planning, Transportation, Demographics (skipped) and Community Facilities.

**Terms.** The portal has no terms page and no licence statement; every dataset is public under the
county's disclaimer, on the portal's home page: *"the geographic information and all downloadable data
is provided "as is" to the public for general information, and are not accurate to surveying or
engineering standards, and not suitable for site-specific purposes. The County of Westchester hereby
disclaims any liability for any and all damage, loss, or liability which may result, directly or
indirectly, from the use of this GIS mapping system and downloadable data."* Some items carry that text
as their licence, some none; none restricts reuse, and `accessInformation` asks for "Westchester
County, NY GIS" as the source (given). The tax parcels carry the NYS ITS parcel terms as well: *"as
is", no warranty, "the User ... assumes all risks associated with its use"*, and the county's note that
the data is for general reference and the tax maps of record are each assessor's. Each entry in
`sizeup/gis.py DEFAULT_LAYERS` records its terms (`licence`) and its portal page (`page`).

**The starter set** (the dataset, its service layer, its date as the county states it, the fields shown):

| group | layer | the county's layer | date | on a click |
|---|---|---|---|---|
| Parcels | Tax parcels (zoom 15+, Port Chester, Rye Brook, Rye and Harrison only, weekly) | `DataHub_TaxParcels/0` (NYS schema, 258 k parcels countywide) | 2025 roll, May 2026 | address, unit, municipality, owner(s), class (NYS code), acres, living sq ft, year built, style, heat, fuel, tax map |
| Boundaries | Fire districts (on at first; ours in red) | `Datahub_Boundaries/153` | Feb 2011 | district, number |
| | EMS districts | `Datahub_Boundaries/152` | Feb 2011 | district |
| | Municipal boundaries | `Datahub_Boundaries/163` | | municipality, square miles |
| Utilities | Hydrants (zoom 14+) | `DataHub_Infrastructure/147` (22 k, from 50 departments, some partial) | Oct 2015 | fire district, hydrant number, municipality |
| | Petroleum bulk storage | `DataHub_EnvironmentandPlanning/165` (NYSDEC PBS) | June 2017 | facility, address, PBS number |
| | Tanks, towers, antennas | `DataHub_BasemapPlanimetrics/14` (structures with codes TANK / TOWER / ANTENNA) | per feature (photo date) | structure, height, top elevation |
| | Bridges & tunnels | `DataHub_BasemapPlanimetrics/16` (footprints: BRIDGE / TUNNEL / RAIL_BRIDGE / PEDESTRIAN_BRIDGE) | per feature | structure, photo date; the county publishes no load or clearance limits |
| Facilities | Fire stations (ours in red) | `DataHub_CommunityFacilities/183` | Dec 2015 | department, address, phone |
| | Police stations | `DataHub_CommunityFacilities/167` | | station, address, phone |
| | Hospitals | `DataHub_CommunityFacilities/178` (NYS DOH) | Apr 2020 | hospital, address, phone |
| | Nursing homes | `DOHdata/1` (hosted, NYS DOH) | Sept 2020 | facility, address, phone, occupied beds, operator |
| | Assisted living & adult care | `DOHdata/0` (hosted, NYS DOH) | Sept 2020 | facility, address, phone, beds, type, operator |
| | Public schools | `DataHub_CommunityFacilities/162` | | school, address, district, phone |
| | Houses of worship | `DataHub_CommunityFacilities/176` | | congregation, address |
| | Volunteer ambulance corps | `DataHub_CommunityFacilities/150` | 2016 | corps, address, phone |
| Hazards | Flood plains (FEMA) | `DataHub_EnvironmentandPlanning/157` (FEMA DFIRM: 100- and 500-year) | | flood plain (year) |
| | Wetlands | `DataHub_EnvironmentandPlanning/154` (USFWS National Wetlands Inventory) | May 2021 | type, NWI code, acres |
| Site details | Buildings (zoom 15+, our four towns; 3D by height) | `DataHub_BasemapPlanimetrics/5` (278 k footprints countywide) | per feature (photos 2000-2023) | height, roof and base elevation, photo date |
| | Driveways (zoom 16+) | `DataHub_BasemapPlanimetrics/10` | per feature | surface, photo date |
| | Parking lots (zoom 16+) | `DataHub_BasemapPlanimetrics/9` | per feature | surface, photo date |
| | Walls & guard rails (zoom 16+) | `DataHub_BasemapPlanimetrics/12` (guard rails, road barriers, stone / concrete / retaining / noise walls) | per feature | barrier, photo date |
| | Streams | `DataHub_BasemapPlanimetrics/0` | | stream, kind |
| | Lakes & ponds | `DataHub_BasemapPlanimetrics/11` | | water, kind |
| Transportation | Railroads (zoom 13+) | `DataHub_BasemapPlanimetrics/1` (the county's rail lines; no owner or electrification field: the New Haven Line's catenary is a fixed note) | | line, kind |
| | Train stations | `DataHub_Transportation/156` | | station |
| | Bee-Line bus routes | `DataHub_Transportation/154` | Apr 2022 | route, name |
| | Bee-Line bus stops (zoom 15+) | `DataHub_Transportation/153` | | stop, on, at, routes |

**Looked for and not on the portal** (Hub search and every service enumerated): water mains, gas
transmission, electric substations and lines, dams, railroad grade crossings, cell towers (only the
TOWER / ANTENNA footprints above), sewer pump stations (only treatment plants, mains and manholes),
emergency shelters, airports / heliports (that folder needs a token), a fire response-area layer other
than the fire districts, swimming pools, fences, stairs, overhead wires, curbs (the edge-of-pavement
polygons are the *Roadways* layer), building storeys or use (the parcel has style and year built),
private schools, a Greenwich edge (the county's data stops at the state line). Not included on
purpose: school districts, demographics, utility poles (188 k points from 2004), the storm-drain and
sewer layers, zip codes, zoning, land use. Worth adding later with one entry each: the EPA hazardous
waste / TRI facilities (hosted `EPAdata`), the hurricane storm-surge and planning zones, water districts
(the supplier per fire district, with a phone), police districts, traffic cameras (the county's copy of
511NY's), 2-ft contours, the 2025 aerial photo service.

**How it works** (`sizeup/gis.py`): the hub (or all-in-one; a station has no maps) fetches each layer
through the ArcGIS REST query API (`<layer>/query?f=geojson&outSR=4326&geometry=<bbox>&outFields=...`),
paged with `resultOffset` and never more than the service's `maxRecordCount` a page, with only the
configured fields and only our area (`[gis] bbox`, or the layer's own: the parcels and the planimetrics
keep to Port Chester, Rye Brook, Rye and Harrison with a margin, and the parcels add
`MUNI_NAME IN (...)`), the geometry rounded to 6 decimals and simplified (Douglas-Peucker, the entry's
`simplify_m`: 0.5 m for parcels, 0.3 m for the planimetrics, 1 m for water). What is kept is cached on
disk (`gis/<key>.geojson` under the state dir, with the config it was fetched for: a changed entry is
fetched afresh), so a restart fetches nothing, and fetched again on the entry's schedule (parcels
weekly, the rest monthly; `hourly` and `daily` exist); a failed fetch backs off 5 min doubling to 6 h.
The request names us: `User-Agent: sizeup/<version> (Port Chester FD dispatch; contact ...)`. The log
says each layer's size when fetched. Measured 2026-10-13: parcels 20,903 features, 11.9 MB (1.4 MB gzip);
buildings 42,352, 16.8 MB (2.6 MB); driveways 30,304, 12.9 MB (2.1 MB); parking 1,898, 2.4 MB; hydrants 7,042,
1.2 MB (0.08 MB); flood plains 747, 1.7 MB (0.4 MB); bus routes 41, 1.5 MB; the rest under 0.5 MB each; about
40 MB on disk in all.

**The browser never talks to the county.** Everything comes from the hub, to anyone signed in (viewers
too; all GET), with an `ETag` (a 304 for `If-None-Match`) and gzip when the browser accepts it:

| | |
|---|---|
| `GET /api/cad/gis` | the catalog: `{configured, source, attribution, link, terms, rev, layers: [{key, label, group, kind, style, icon, sub, minzoom, fields, on, refresh, tiled, licence, page, asof, note, at, n, bytes, error, updated, rev}]}` (the pages poll it every 5 min) |
| `GET /api/cad/gis/<key>` | the whole layer as GeoJSON (`Cache-Control: private, max-age=300`), one fetch for every map on the page |
| `GET /api/cad/gis/<key>?bbox=w,s,e,n` | only the features touching the box (`max-age=60`): how a **tiled** layer is served. The parcels and the planimetrics are too big to send whole (21 k parcels, 42 k buildings in our area), so the page asks for its viewport, padded, from the layer's minimum zoom, and again only when the map leaves that box; a box over 0.25° a side is refused |

**Adding a dataset is one config entry** (`[[gis.layers]]`, no code): its key, label and group, the
layer's REST URL (`.../MapServer/<n>` or `.../FeatureServer/<n>`), the fields to show (the first is the
title), a style (colors as design tokens: `--info`, `--warn`, `--err`, `--ok`, `--dim`, `--text-2`, or
hex; `fill`, `width`, `radius`, `dash`, a `label` field and `labelzoom`, a `height` field with
`height_scale` for 3D), an `icon` for a point layer (a mark from `static/icons.svg`: `hydrant`, `fire-station`,
`police`, `hospital`, `nursing-home`, `school`, `worship`, `ambulance`, `train`, `bus`, `petroleum`, `tank`; none: a
dot; `size` in the style scales it), a `sub` line for the click box, `minzoom`, `refresh`, `tiled` for a big one, `simplify_m`, a `bbox` or `where`,
`on`, and its `licence`, `page`, `asof` and `note`. An entry with a starter key changes only what it
sets; `enabled = false` drops one. `config.example.toml` has a worked example. The config refuses a
plain-http URL, an unknown key, a bad zoom or refresh.

**Fire districts as "ours".** The hub keeps the fire-district polygons, so `gis.district_at(lat, lon)`
answers which district a point is in (`{FIRENAME, FIREID, _ours}`); a follow-up issue wires it into the
page / event "ours" checks and the geocoder's town threshold. Note the county's 2011 boundaries put Rye
Brook inside the Port Chester FD district.

```toml
[gis]                            # sizeup/gis.py; leave out: the starter set
enabled = true
bbox = [-73.80, 40.92, -73.56, 41.10]
[[gis.layers]]
key = "hydrants"
on = true                        # one line changes one thing
```

## The hub's storage

Everything the hub keeps is SQLite under `SIZEUP_STATE_DIR`, so Litestream can replicate it:

| file | holds |
|---|---|
| `cad.sqlite` | the CAD: events, units, run cards, dispatch log, settings, radio registry, communications log, phone pre-alerts |
| `hub.sqlite` | the monitor: incidents, transmissions, pages (one row each, written only when changed), and the station events applied so far |
| `audio.sqlite` | received clips (`storage.audio_keep_hours`, 12 h, and `audio_max_mb`, oldest first) and the dispatch audio (Polly's or the station's; kept with the dispatch log; unused previews a day) |

The hub has no `captures/` and writes no JSON files. Dispatch WAVs are also cached in
`dispatch-audio/` on the task's disk; after a redeploy they're served from `audio.sqlite`.

## Packaging and the Fargate task

`Dockerfile`: `python:3.14-slim`, linux/arm64 (amd64 builds too), non-root user `sizeup` (uid
10001), `pip install .`, `data/` included, Litestream 0.5.17 pinned and sha256-checked, health check
on `/healthz` (a Python one-liner: slim has no curl). `/healthz` answers 200 with
`{ok, version, build, role, station, public_url}` while the process serves (it doesn't depend on the
station). Build with `--build-arg SIZEUP_BUILD=$(git rev-parse HEAD)`: the full sha (the deploy
workflow passes it); stations follow it.

One task, desired count 1, ephemeral disk:

* **hub** container (this image); its task role may call `polly:SynthesizeSpeech` (`11-polly.sh`). Entrypoint `deploy/hub/entrypoint.sh`:
  * `LITESTREAM_BUCKET` set: `litestream restore -if-db-not-exists -if-replica-exists` for each of
    the three files, `python -m sizeup --init-state` (creates them on a first start), then
    `exec litestream replicate -exec "python -m sizeup --config ..."` with `deploy/hub/litestream.yml`
    (bucket from `LITESTREAM_BUCKET`, prefix `LITESTREAM_PATH` default `sizeup-hub`, region
    `LITESTREAM_REGION` default `AWS_REGION`; credentials from the **task role**, no keys).
  * unset: just `python -m sizeup` (local dev).
  * Environment: `SIZEUP_STATION_TOKEN` (secret from SSM), `SIZEUP_PUBLIC_URL`, optionally
    `SIZEUP_PORT` and `SIZEUP_511NY_KEY` (secret from SSM, [the highway layers](#highway-layers-cameras-incidents-signs-mileposts-the-thruway-511ny));
    the image sets `SIZEUP_STATE_DIR=/data`, `SIZEUP_CONFIG`.
* **cloudflared** sidecar: `cloudflare/cloudflared:latest tunnel --no-autoupdate run`, `TUNNEL_TOKEN`
  secret from SSM. awsvpc: it reaches the hub at `http://localhost:8090` (the tunnel's public
  hostname points there). No published ports, no load balancer.
* Deploy so the old task stops before the new one starts (minimum healthy 0 %, maximum 100 %): two
  hubs replicating to the same S3 prefix at once would corrupt the backup, and two would also split
  the station's events.

`deploy/hub/compose.yaml` is a local convenience only (hub + cloudflared sharing a network
namespace, a named volume for `/data`, no Litestream).

## Code

`sizeup/cloud/`: `station.py` (StationLink: outbox, forwarder, state, jobs), `autoupdate.py` (the
station follows the hub's build: `Watch` in the station, the root updater), `stationmode.py`
(Settings → System → Station and the station page's hub link: on / off at runtime, state/cloud.json, the Keychain), `outbox.py`,
`client.py` (kept-alive HTTPS), `hub.py` (Hub: ingest, health, jobs; HubApp), `hubstore.py`
(hub.sqlite, audio.sqlite), `access.py` (Cloudflare Access identity; the gate is in
`sizeup/web/server.py`, the operator in `sizeup/cad/api.py` and `Settings.person`; the activity log in
`sizeup/cad/activity.py`). Seams in `sizeup/app.py`: `_emit`, `_heard`, `_page_heard`, `_retext`, `_stt`,
`_aws_start` / `_aws_done` / `_aws_failed`, `set_channel_prefs`, `make_app`; in `sizeup/cad/service.py`:
`remote_voice`, `_render_hub`, `audio_blobs`, `cloud`, `radio_ctx`. The voice: `sizeup/polly.py`. Speech-to-text per channel: `sizeup/channels.py`,
`sizeup/transcribe.py`, `sizeup/aws.py`. The station page: `sizeup/web/static/station.html`, `station.js`,
`hublink.js`, `station.css`; its routes in `sizeup/web/server.py`. Tests: `tests/test_station_page.py`, `tests/test_polly.py`, `tests/test_cloud.py`, `tests/test_stationmode.py`,
`tests/test_access.py`, `tests/test_roles.py`, `tests/test_activity.py`, `tests/test_channels.py`,
`tests/test_transcribe.py` (fakes: `tests/fake_transcribe.py`), `tests/test_autoupdate.py`,
`tests/test_station_install.py`. The county GIS layers: `sizeup/gis.py` (`tests/test_gis.py`, fixtures
`tests/fixtures/wcgis/`), `sizeup/web/static/gislayers.js` (a group in maplayers.js's Layers box).
