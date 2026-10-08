# sizeup in the cloud: hub and station

To try the CAD with firefighters who aren't at the firehouse, sizeup runs in two places from the
same code and the same entry point (`python -m sizeup --config ...`), chosen by `[cloud] role`:

| | **station** (the Mac with the SDRs) | **hub** (the cloud) |
|---|---|---|
| Radios, trunk-recorder, MDC decode | yes | none: nothing opens an SDR or starts a program |
| whisper (3.8 GB), Kokoro voice (1.4 GB) | yes | none |
| Dispatch voice | its Kokoro (all-in-one; the hub's fallback) | **Amazon Polly**, made on the hub ([below](#the-dispatch-voice-amazon-polly)) |
| Web UI: CAD `/cad` (Dispatch, Overview, Radio, Settings), wall board `/monitor` | yes (its own, local) | yes: **the CAD of record** |
| CAD database | its own `state/cad.sqlite`, not synced | `cad.sqlite` (+ `hub.sqlite`, `audio.sqlite`) |
| Network | outbound HTTPS to the hub only | reached only through Cloudflare Tunnel |

No `[cloud]` section = today's all-in-one, unchanged.

**Station on / off from the UI** (user, 2026-10-08: "how do we turn on and off station mode
though? id like that to be easy to do from the ui"): on any machine that isn't the hub,
**Settings → System → Station** has a *Forward to hub* switch, the hub URL, the link's status, where
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
| `tts` | a dispatch / preview / upgrade on the hub when Polly failed (`Cad._render_hub` → `_render_remote`), or always with an engine other than `polly` | renders it with `render_dispatch` and its Kokoro (`say` as its fallback), returns the WAV | at once (offline) or after 20 s: **text only**, the note says why: "Voice unavailable — Polly: AccessDeniedException: …; station offline since 14:02: text only" (or "didn't answer in 20 s"); never a hung request |
| `call` `set_mode`, `set_talkgroup`, `set_talkgroup_group` | the hub's SDR / talkgroup switches | runs it, returns its own refusal if any | the switch is refused with the reason ("station offline since 12:03") |
| `call` `retranscribe` | "Re-run" on the hub, or Amazon Transcribe failing for a clip the station didn't run whisper on | queues it; new text comes back as `retext` | refused: speech-to-text runs on the station (a failed AWS clip then says "transcription failed") |
| `call` `set_channel_prefs` | Settings > Radios > Channels on the hub, or the station reporting other choices than the hub's (`channels.sig` in its state) | applies the whole map + whether AWS can take audio now, keeps it (its own Settings overrides, audited as "hub"), reports `{prefs, aws, sig}` | sent when it's back: the Channels card says "Station offline ...: these apply when it's back" |

Settings, run cards, units, radio IDs and every CAD record stay on the hub. The station's own CAD
keeps working for local use, with a banner on its `/cad`: the CAD of record is the hub.

## On screen

* Hub, Settings > Radios, Receivers: a **Station** row ("3 s" / "offline 12:03"; the tooltip says "connected, last
  seen 3 s ago" or "offline since 12:03:41"), then the station's rows as it reported them. Offline,
  those rows turn *unknown* with what they last said: never a stale "ok". The SDR switches only show
  while the station is connected.
* Hub, CAD header: "Station connected" / "Station offline 12:03".
* Station, Settings > Radios, Receivers: a **Hub** row (connected, or offline with how many events are waiting;
  "not sending" when forwarding is on without a token). Station `/cad`: the "Hub connected" /
  "Hub unreachable" pill and the banner with the hub's URL. All of it goes when forwarding is
  switched off (Settings → System → Station) and comes back when it's on.

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

Station (optional: Settings → System → Station does the same; see `deploy/station/README.md`):

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
is connected, and nobody gets two alerts. Switching forwarding in Settings > System > Station starts
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
station). Build with `--build-arg SIZEUP_BUILD=$(git rev-parse --short HEAD)`.

One task, desired count 1, ephemeral disk:

* **hub** container (this image); its task role may call `polly:SynthesizeSpeech` (`11-polly.sh`). Entrypoint `deploy/hub/entrypoint.sh`:
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

`sizeup/cloud/`: `station.py` (StationLink: outbox, forwarder, state, jobs), `stationmode.py`
(Settings → System → Station: the link on / off at runtime, state/cloud.json, the Keychain), `outbox.py`,
`client.py` (kept-alive HTTPS), `hub.py` (Hub: ingest, health, jobs; HubApp), `hubstore.py`
(hub.sqlite, audio.sqlite), `access.py` (Cloudflare Access identity; the gate is in
`sizeup/web/server.py`, the operator in `sizeup/cad/api.py` and `Settings.person`; the activity log in
`sizeup/cad/activity.py`). Seams in `sizeup/app.py`: `_emit`, `_heard`, `_page_heard`, `_retext`, `_stt`,
`_aws_start` / `_aws_done` / `_aws_failed`, `set_channel_prefs`, `make_app`; in `sizeup/cad/service.py`:
`remote_voice`, `_render_hub`, `audio_blobs`, `cloud`, `radio_ctx`. The voice: `sizeup/polly.py`. Speech-to-text per channel: `sizeup/channels.py`,
`sizeup/transcribe.py`, `sizeup/aws.py`. Tests: `tests/test_polly.py`, `tests/test_cloud.py`, `tests/test_stationmode.py`,
`tests/test_access.py`, `tests/test_roles.py`, `tests/test_activity.py`, `tests/test_channels.py`,
`tests/test_transcribe.py` (fakes: `tests/fake_transcribe.py`).
