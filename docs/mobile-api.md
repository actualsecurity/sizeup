# Mobile app and `/api/v1`: design

> **Status:** design, not built. Decisions open: D1-D11 ([§11](#11-decisions)), tracked in #556.
>
> **For:** a developer building the native iOS and Android app, and the maintainers who will build the API it uses.
> **Owners:** the hub, `/api/v1`, its OpenAPI specs, sign-in, pushes and the change log are **Platform** work, done by
> the existing maintainers. The native client is **App** work. The API contract belongs to the platform side and the app
> consumes it. Until the first specs land, the app developer can build the app shell, the sign-in UX against a mock of
> the auth endpoints ([§3.3](#33-auth-for-the-native-app)), and then generated clients as each spec merges.

Written from a read of `main` at `a3de36f` (2026-11-15). File:line references are to that commit and will drift.
**Unverified** marks anything not checked in code or measured.

Contents: [how to work on this](#how-to-work-on-this) · [1 current state](#1-current-state) ·
[2 a separate service?](#2-should-the-api-be-its-own-service-no-not-now) · [3 API strategy](#3-api-strategy) ·
[4 pushes](#4-pushes-to-the-app) · [5 media and maps](#5-audio-video-and-maps) · [6 location](#6-location-from-phones) ·
[7 OpenAPI](#7-openapi) · [8 everything else](#8-everything-else-a-second-consumer-needs) ·
[9 alignment](#9-alignment-with-other-work) · [10 plan](#10-phased-plan) · [11 decisions](#11-decisions) ·
[12 risks](#12-risks) · [could not verify](#could-not-verify) · [sources](#sources)

**In one paragraph.** Do **not** break the API out into its own service. The hub is a single writer on SQLite with a
leader fence and 30 s Litestream replicas; a separate API task would either proxy every request to the leader (one more
hop, one more thing to hand over) or read data up to 30 s stale (unacceptable for dispatch). A volunteer department's
mobile load (tens of devices, a burst of a few requests a second at a tone-out) is small next to what the hub already
serves. Instead: a **versioned `/api/v1` surface inside the hub**, its own handler module with its own auth (hub-issued
per-device tokens after one Cloudflare Access sign-in), **resource-shaped** JSON described by **hand-written OpenAPI
3.1** with contract tests, a **cursor change log** (shared with a planned machine feed, `/api/feed/v1`) for resume,
**SSE in the foreground** and **APNs/FCM through Amazon SNS in the background**, and **short-lived signed media URLs**.
Headroom comes from moving the camera DVR out of the hub task or going to 1 vCPU (about +$15 a month), not from
splitting the API.

## How to work on this

For the app developer.

- **Run the hub locally:** [development.md](development.md) (setup, all-in-one, `tools/dev_seed.py` for invented
  calls). All-in-one has no Cloudflare Access, so every route is open: that is the place to prototype.
- **The contract:** `/api/v1` and its OpenAPI specs ([§7](#7-openapi)) live in this repo and are owned by the platform
  side. Generate the Swift and Kotlin clients from the specs at a pinned commit of this repo.
- **Changing the API:** open an issue here first that says what the app needs and why, then a spec-only PR (the YAML
  and its examples) for review. The handler follows in a separate PR, and the contract tests hold it to the spec. Don't
  build against an endpoint that is not in a merged spec, except the internal routes listed in
  [§10](#10-phased-plan) for throwaway prototypes.
- **Where the app lives:** a separate **private** repo (proposed, D10). It holds the hub hostname, bundle IDs, push
  setup and store metadata. App work is tracked there; this repo tracks the platform side.
- **Public repo:** this repo is public ([CLAUDE.md](../CLAUDE.md), "Public repo rules"). Specs, fixtures, issues and
  PRs here use synthetic data only: `example.org` hosts, made-up units and addresses. No hostnames, account IDs, keys
  or real incident data.
- **Never point a prototype at production.** Use a local hub, then staging once it exists (PR 3).

---

## 1. Current state

### 1.1 The server

- Stdlib `http.server.ThreadingHTTPServer` with one `BaseHTTPRequestHandler` (`sizeup/web/server.py:380-1211`,
  `WebServer` 1214). No framework and no router table: `_get` (706-828) and `_post` (842-1047) are `if path ...`
  chains, and the CAD's routes are regexes in `sizeup/cad/api.py:315-345`. One thread per connection; each SSE stream
  holds a thread for its life.
- Dependencies: `numpy`, plus the optional `docs` and `intel-llm` extras (`pyproject.toml`). AWS calls are signed in
  the stdlib (`sizeup/aws.py:178 sign`, `202 presign`). There is no HTTP/2 client and no RSA *signing* (only
  verification in `access.py`). This matters for push ([§4](#4-pushes-to-the-app)).
- The route catalogue lives in module docstrings, not in a spec: `server.py:1-110`, `cad/api.py:1-300`,
  `cloud/hub.py:1-36`, `roads.py:95-113`, `dvr.py:34-45`, plus the layer modules.

### 1.2 Route inventory, by domain

About 117 documented routes in `cad/api.py`, 26 in `server.py`, 9 in `cloud/hub.py` and about 25 in the layer modules:
roughly 175. Auth is Cloudflare Access for people. GET routes are open to anyone signed in unless listed in
`ADMIN_READS` (`cad/api.py:351-365`). POST routes are checked against `WRITES` (`cad/api.py:370-401`; `needs()` 680,
`permit()` 695, `MAY` 403). Roles are viewer, dispatcher and admin (`settings.py:107`, `standing()` 2702).

| Domain | Main routes | Auth | Shape |
|---|---|---|---|
| Live console | `GET /api/cad/state`, `GET /api/cad/stream` (SSE) | signed in | **UI blob** (`Cad.state`, `cad/service.py:415-468`): about 30 top-level sections (lists, units, incidents, unit_log, run_cards, map, overview, talking, paging, prealert, airport_alerts, intel, status_hints, alarms, radio_ids, sources, revs) |
| Monitor (legacy) | `GET /api/state`, `/api/stream`, `/api/scene`, `POST /api/incident/<id>/close` | signed in / dispatch | UI blob |
| Incidents | `GET /api/cad/incident/<id>`, `/history`, `/search` (paged), `/incident/<id>/here`; `POST /api/cad/incident` plus actions `/<id>/{accept,dismiss,close,reopen,note,recommend,preview,dispatch,upgrade,attach,link,unlink,keep,feed-use,auto-undo}` | read / dispatch | resource reads; **RPC-style action POSTs** (fine for mobile) |
| Units | `POST /api/cad/unit/<id>/status`, `/station`; `GET /api/cad/incident/<id>/units?q=`, `/api/cad/rigs` | dispatch / admin | units have no standalone GET; they come only in the blob |
| Dispatch | `POST /api/cad/dispatch-mode`; `GET /api/cad/dispatch/<id>.wav`, `/api/cad/audio/<key>.wav`, `/tts/voices` | dispatch | actions + WAV |
| Alerts | `POST /api/cad/{prealert,airport-alert,hint,alarm}/<id>/...`, `GET /api/cad/alarms`, `POST /api/cad/intel/<id>/{dismiss,feedback}`, `GET /api/cad/intel/events?since=` | dispatch / read | resources (intel events: `since` cursor) |
| Radio / audio | `GET /api/cad/comms` (cursor pages), `/comms/sources`, `/comms/activity` (admin), `/feed`; `GET /api/audio/<id>.wav`; `POST /api/cad/radio-id`, `/radios` | read / admin | resources; audio is **WAV only** |
| Map layers | `/api/cad/{roads,gis,gis/<key>?bbox=,roadprojects,trains,weather,radar tiles,vessels,aircraft,airport,geocode,places}` | read | reusable resources with ETag, gzip and max-age (`server.py:633-645`) |
| Cameras / DVR | `/api/cad/cameras[?for=,area=]`, `/<id>/image`, `/<id>/stream/*.m3u8,.ts`, `/<id>/dvr`, `/dvr/index.m3u8`; `POST .../dvr/{pin,clip}` | read / dispatch | HLS proxied through the hub, cookie auth; at most 8 live streams hub-wide (`roads.py:237 STREAM_MAX`) |
| Pre-plans / docs / premise | `/api/cad/preplan[?bbox=]`, `/preplan/docs[/<id>/file]`, `/premise`; POST variants; upload with `X-Sizeup-Doc` (25 MB) | read / dispatch; restricted kinds filtered per role | resources |
| Attachments | `/api/cad/incident/<id>/attachments`, `/attachments/<id>/{index.m3u8,<n>.ts,audio.wav,download}`, `export.zip` | read, audited / admin delete | resources |
| Settings / admin | `/api/cad/settings`, `/settings/<section>` (about 30 sections), `/whoami`, `/activity`, `/notify`, `/health`, `/perf`, `/datasources`, `/logs`, `/stt/*`, `/intel`, `/adapter-password`, CSV exports | admin mostly | **UI-panel shaped** |
| Station radio control | `POST /api/{talkgroup,talkgroup-group,mode,adsb-receiver,reception,retranscribe,airband-ab,sources/reset}` | admin | actions |
| Station link | `/api/station/{events,state,jobs,jobs/<id>,aircraft,vessels,logs,hello,status}` | **Bearer** shared token (`hub.py:36, 427`), Access exempt (`server.py:483`) | machine protocol: epoch + seq, long poll |
| Health | `GET /healthz` | none | tiny JSON |

No route returns HTML fragments. Everything is JSON, WAV, HLS or files.

**Blob versus resources.** By route count the API is mostly resources and actions. By bytes on the wire and by what
drives the screen, it is **one blob**: the full console state is about 160 KB uncompressed (`server.py:226`, #137), and
the console takes everything live from it. A mobile app cannot use the blob as a contract. Its sections mirror panels
(`map.markers` with draw sizes, `overview`, `run_cards_default`, `audio.label`), change with the UI, and are the same for
every viewer (one shared `Broadcaster`), so they cannot be filtered per role or per device.

### 1.3 Live updates today

- `Broadcaster` (`server.py:222-333`) rebuilds the state every 1 s, or at once on `poke()` with at least 0.25 s between
  pushes. It keeps each top-level section's JSON with the seq it last changed at. A client gets the full state once,
  then `{"_delta":1, <changed sections>, "_gone":[...]}`. Idle, only `time` changes.
- Ids are `<boot>.<seq>`. SSE `id:` is resumed by `Last-Event-ID` or `?since=`; the polling fallback uses `?since=`
  with `X-State-Id` (`_state` 647, `_stream` 1157-1195). The stream is gzipped per stream (`Z_SYNC_FLUSH`), opens with
  `retry: 2000` and 2 KB of padding for proxies, and `live.js` falls back to polling.
- **Resume does not survive a task handoff.** `boot` is random per process (`server.py:235`), so after each deploy every
  client downloads the full state again. Resume holds no history: a client that was away gets the current state, not
  what happened meanwhile. Fine for a console, wrong for "what did I miss" on a phone.
- The CAD rev bumps on no-op commits (#446 item 1), so rebuilds happen more often than needed.

### 1.4 Identity today

- `AccessVerifier` (`sizeup/cloud/access.py:110-215`): RS256 against the team's certs, `aud`/`iss`/`exp`. The token
  comes from the `Cf-Access-Jwt-Assertion` header, else the `CF_Authorization` cookie (`identify` 202). Identity is the
  `email` claim. Service tokens give `Identity(service=...)`, `person=False`.
- `_admit` (`server.py:478-495`): with Access configured, everything except `/healthz` and `/api/station/` needs a person
  (401 otherwise). Each verified request goes to `cad.visit` (activity, `activity.py:74`).
- **Fail-open trap:** `CadApi.permit` checks nothing when `who` is None or has no email (`cad/api.py:697-699`), and
  without a verified person `operator` is whatever the client sent. That is right for all-in-one, but **any new auth
  path that reaches `CadApi` without a person `Identity` is an unrestricted admin.**
- `sourcekeys.request_context(who)` gates other P25 systems' traffic per request (`sourcekeys.py:56-68`). The shared
  stream carries it only because every listener is verified.
- CSRF: `cross_site` (`server.py:364`) applies to the station and all-in-one only (`same_origin_only = role != "hub"`,
  424). On the hub, Access's SameSite cookie is the protection (#447).

### 1.5 Pushes today

- ntfy only (`sizeup/notify.py`), three kinds: ALERTS (emergency buttons, mayday, aircraft emergencies, airport
  pre-alerts), STATUS (system health) and INTEL (radio pre-alerts) (`notify.py:21-37`, `KINDS` 107). Topic names are
  secrets kept in Settings, never in the repo or logs. The protected topics of the third-party pre-alert app (which
  sizeup only reads) are derived, with a hashed backstop (`notify.py:42-56, 78-81`).
- **sizeup does not push dispatches to the crew today.** The department's paging and the third-party app do that. An app
  that pushes dispatches would be a new responsibility (D3).
- Intel pushes are composed from an allow-list (`intel/push.py`): no transcript words, talkers or radio IDs among the
  fields. Since 2026-11-17 the scrub runs only with `push_pii` off (Settings > System > Intel; on by default). Caps
  (`intel/policy.py` `DEFAULTS`): 30 an hour, 200 a day, 10 per category an hour, with quiet hours and a digest.
- Listen links (`intel/listen.py`) are random S3 keys behind a presigned GET for 20 min. Audio is AAC/M4A at 24 kbit/s
  where ffmpeg exists, else 8 kHz WAV. The hub image has no ffmpeg, so production serves WAV at about 1 MB a minute.
- The outbound guard (`sizeup/outbound.py`, PR #541; follow-ups #545): `notify.publish`, `aws.sign/presign`, Polly and
  Bedrock refuse when outbound is off. Any new push channel must sit behind it.

### 1.6 Maps, media, location

- Maps: MapLibre GL JS (`static/vendor/maplibre-gl.js`) with OpenFreeMap vector tiles and glyphs
  (`app.py:3977-3978`), attribution OpenFreeMap / OpenMapTiles / OSM (`map-style.js:38`). **The style is built
  client-side in JS** (`map-style.js:48-195`), so a native client would have to reimplement it.
- Media under Access by cookie: clip WAVs, dispatch WAVs, camera HLS (live, DVR, attachments), site documents
  (streamed, no links, `server.py:497`).
- Location: `Cad.positions` is the single hook (`cad/service.py:303-306`). Ingest shape
  `{unit, lat, lon, at, heading?, speed?, source}` (`cad/mapview.py:32-50`), with fresh, stale and ignored ages.
  **No provider exists.** "Unit" means an apparatus. There is **no concept of an individual responder** (a person's
  status or location) anywhere in the code (only #43 and [cad-plan.md](cad-plan.md)).

### 1.7 Capacity

- The task is 512 CPU units with 2048 MB (`deploy/aws/taskdef.json`), plus the cloudflared sidecar. CPU is estimated at
  about 50% of 0.5 vCPU, mostly the DVR (**not re-measured here**; `perf.py`, #425, has the real numbers). Open CPU
  items: #446.
- Writes go to the leader only: the `Gate`/fence answers a write on a non-leader with 503 + Retry-After
  (`server.py:335-361`, `fence.py:11-34`). Replicas lag up to 30 s (the Litestream interval).

---

## 2. Should the API be its own service? No, not now

| Option | What it takes | Verdict |
|---|---|---|
| **A. Separate API task reading replicas** | A Litestream restore loop on the API task, 0-30 s stale; writes proxied to the leader | Stale incidents and unit statuses in a dispatch app are a safety problem. **No.** |
| **B. Separate API task proxying to the leader** | Two hops per request, handoff awareness in both, a second auth boundary, a second deploy | Adds latency and failure modes; the hub still does the work. **No.** |
| **C. `/api/v1` module inside the hub** (recommended) | A new package `sizeup/api/` with its own handler, routed from `server.py` before `/api/cad/`; reuses `Cad` and the store | Cheapest, correct consistency, one deploy. **Yes.** |
| D. Read-only follower for heavy reads | Search, history and exports from a replica on a small task | Only if search or exports ever hurt. **Later, if measured.** |
| E. Move the DVR out | Its own Fargate task (0.25 vCPU / 1 GB, about $11 a month), the hub proxying playlists | **The best CPU lever** if the DVR is the main consumer. |
| F. Raise the task to 1 vCPU / 2 GB | One line in the task definition | About **+$15 a month** (≈ $21 → $36 at us-east-1 list prices, $0.04048 per vCPU-hour and $0.004445 per GB-hour; **from memory, check**). Simplest headroom. |

**Load a mobile app adds (estimate).** About 40 enrolled devices, about 25 in the foreground at a tone-out.

- Foreground streams: 25 threads. Each event is built once; the per-stream cost is a small gzip (idle about 50 B/s).
- At a tone-out: 25 × (one incident detail + one changes catch-up) in about 10 s, about 5 req/s for 10 s, then a few
  status or "responding" POSTs.
- Background: no requests (push only).
- Location while responding (if built): 25 × one fix per 5 s is 5 req/s for about 10 minutes. Kept **in memory**, never
  one SQLite write per fix ([§6](#6-location-from-phones)).

That fits one 0.5 vCPU process *if* #446 item 1 (no-op rev bumps) is fixed. Mobile clients must never be wired to
`comms_rev`. Re-measure with `perf.py` after the first mobile beta and decide E or F on the numbers.

**When to revisit:** more than about 200 concurrent streams, a second department, or an external integration with its
own SLA. Then the split is a stateless edge (auth, rate limiting, fan-out of the change log) in front of the single
writer, not a second writer.

---

## 3. API strategy

One strategy for every consumer.

### 3.1 Surfaces

| Surface | Consumer | Auth | Status |
|---|---|---|---|
| `/api/cad/*`, `/api/stream`, `/api/state` | the web console (first-party, same deploy) | Access cookie | **unversioned, internal**: changes with the page; never documented as public |
| `/api/v1/*` | the mobile app, future first-party clients | hub-issued device token ([§3.3](#33-auth-for-the-native-app)) | **versioned, OpenAPI, compatibility promised** |
| `/api/feed/v1/*` (planned) | machine consumers | Access service token + bearer + scopes | versioned |
| `/api/station/*` | stations | Access service token + bearer, one per station once there are several | versioned protocol (`v` fields exist) |

The web console is not migrated to `/api/v1`. It may adopt v1 endpoints where useful, but it keeps its blob stream.

### 3.2 Conventions for `/api/v1` (and `/api/feed/v1`)

- **Versioning:** the major version is in the path. Changes within v1 are additive only (new endpoints, new optional
  fields, new enum values). Clients ignore unknown fields and treat unknown enum values as "other". A breaking change
  means `/api/v2`, served beside v1 for at least **12 months** or until the minimum supported app version passes it.
  Deprecation is announced with `Deprecation` and `Sunset` headers and in `/api/v1/meta`.
- `GET /api/v1/meta` returns `{api: "1.x", min_app: {ios, android}, features: {...}, server_time, epoch}`. It drives
  forced upgrades and feature flags.
- Errors are always `{"error": {"code": "machine_code", "message": "human text", "retry_after_s"?}}`. The 503 during a
  handoff carries `Retry-After` as today.
- Ids are stable strings. Times are **unix seconds (float)**, with `server_time` in `meta` and in every SSE event for
  skew.
- Every mutable resource has a `rev` (per object, not the global `store.rev`), an `ETag` on GET, and `If-Match` on edits.
- Lists use cursor pagination (`?after=<opaque>&limit=`, response `{items, next}`). No page numbers in v1.
- Writes take an `Idempotency-Key` (UUID) header, stored 24 h per principal, the first response replayed
  ([§8](#8-everything-else-a-second-consumer-needs)).
- Every request carries `X-Client: <app>/<version> (<platform>/<os>; build)` for metrics and min-version enforcement.

### 3.3 Auth for the native app

**Can people sign in through Cloudflare Access and come back into the app? Yes.** The app sends the person to
Cloudflare Access in a system browser sheet, and they sign in exactly as on the web. The hub sees the Access JWT it
already verifies and hands a one-time code back to the app. The app trades the code for **hub-issued per-device
tokens**. This is option **B** below, recommended (D1).

**What Access is configured with today:** email one-time PIN, no identity provider ([architecture.md](architecture.md),
[ROADMAP.md](ROADMAP.md), `sizeup/cloud/access.py`; [cloud.md](cloud.md) names an identity provider only as an
option). The design does not depend on the identity source: if one is added to Access later, the app gets it for free,
because that step happens inside Access's page.

**Hub endpoints:** OAuth 2.0 authorization code + PKCE (RFC 6749 / 7636, native-app rules of RFC 8252), standard so the
app can use **AppAuth-iOS / AppAuth-Android** instead of hand-rolled code.

**The flow.**

1. The app generates `code_verifier` and `state` and opens `ASWebAuthenticationSession` (iOS,
   `prefersEphemeralWebBrowserSession = true`, so no Access cookie stays in Safari) or a Custom Tab (Android) at
   `https://<hub>/auth/v1/authorize?response_type=code&client_id=sizeup-ios&redirect_uri=<app link>&code_challenge=<S256>&code_challenge_method=S256&state=…&device_name=…`
2. **That path is under the existing Access application** (the whole hostname), so it needs no Access change. Access
   shows its login page; the person enters their email and PIN (or later the identity provider). Access sends the
   browser on to the `authorize` URL, and the request reaches the hub with `Cf-Access-Jwt-Assertion`.
3. The hub, in a dedicated handler:
   - verifies the JWT with the existing `AccessVerifier`;
   - resolves `settings.standing(email)` and refuses a deactivated or unknown user with a page, not a redirect;
   - checks `client_id` and `redirect_uri` against a fixed allow-list (exact match);
   - stores `code → (email, Access session id, code_challenge, client_id, device_name, expires +60 s, used=false)`;
   - answers `302 <redirect_uri>?code=…&state=…`.
4. The app calls `POST /auth/v1/token` (`grant_type=authorization_code`, `code`, `code_verifier`, `client_id`,
   `redirect_uri`, plus `platform`, `app_version`). The hub checks `S256(verifier) == challenge`, that the code is
   unused and unexpired, and the client and redirect. It creates the `devices` row and returns
   `{access_token, expires_in: 900, refresh_token, device_id}`.
5. API calls send `Authorization: Bearer <access_token>`. Refresh (`grant_type=refresh_token`) is **silent, no
   browser**.

**Details.**

- Prefer an **https redirect** (iOS Universal Link, Android App Link) to a custom scheme, which any app can claim.
  `ASWebAuthenticationSession` with an https callback needs **iOS 17.4+** (`Callback.https(host:path:)`). Below that, a
  custom scheme is acceptable *because PKCE makes an intercepted code useless*.
- **The link-association files must be reachable without Access.** Apple's CDN fetches
  `/.well-known/apple-app-site-association` and Google fetches `/.well-known/assetlinks.json`, with no cookie, so they
  need a **Bypass** on `/.well-known/`. The files are public by nature (team ID, bundle ID, certificate fingerprint).
  Alternatively, host them on a separate small domain used only for the redirect.
- The code is bound to the PKCE challenge **and** to the Access session (`Identity.session`): never reusable, and the
  enrollment appears as one "signed in" in the activity log.
- Access's own JWT is **never** given to the app. The hub reads it once at enrollment and discards it.

**Token lifetimes, refresh, revocation, logout.**

| Item | Value | Notes |
|---|---|---|
| Authorization code | 60 s, single use | stored hashed |
| Access token | 15 min | Compact token signed **HMAC-SHA256** with a key from SSM (`kid` for rotation), carrying `sub` (email), `did`, `iat`, `exp`. Stdlib only. The alg is fixed and never read from the token |
| Refresh token | Rotated on every use; **30 days idle, 180 days absolute** | Stored hashed. At the absolute limit the person signs in through Access again (one PIN email) |
| Reuse detection | An already-rotated refresh token presented again **revokes the device** | the standard theft signal |
| Per-request check | The device is active and the user is active; role from `standing()` | Cached by settings rev, so a revoke or deactivation takes effect on the **next request**, not at token expiry |
| Revoke (admin) | Settings > Users > the person's devices > Revoke | Deletes refresh tokens and the push endpoint, writes the audit log. The app's next call gets `401 device_revoked` and the app wipes its cache |
| Revoke (self) | The app's "Sign out", or "My devices" on the web | the same |
| Deactivate a user | Revokes all their devices | |
| Logout | `POST /auth/v1/revoke` (RFC 7009) for the device's tokens and push endpoint | No Access logout needed: the ephemeral sheet kept no Access cookie |
| Audit | `activity.py` gets `device_enrolled`, `refresh` (once a day, not each), `revoked`, `refused`, with device id, app version, IP, UA | |

**Options considered.**

**(A) The app sends the Access token itself on every call. Rejected.**

- Cloudflare's CLI tutorial has `cloudflared access login <url>` fetch a token and send it in a `cf-access-token`
  header, so Access does accept a user's token that way.
- The app could only get it through cloudflared's token "transfer" flow, which comes from cloudflared's source
  (`token/transfer.go`) and is **not documented as a public API**; the tutorial says the token is intended only for a
  command-line tool. Scraping `CF_Authorization` out of a web view is worse.
- Lifetime is the Access session (24 h by default) with **no refresh**: on email PIN, another email every day or week,
  sooner or later in the middle of a call.
- Revocation is per user, not per device, and the token opens the **whole hostname** (web UI, every `/api/cad` route).

**(B) The app's API paths bypass Access at the edge; the hub enforces its own tokens. Recommended.**

- **Access configuration:**
  - One more Access application on the same hostname with paths `/api/v1/`, `/auth/v1/token`, `/auth/v1/revoke` and
    `/.well-known/`, with a **Bypass** policy. Path-scoped applications on a hostname are how `/api/station/` is already
    done ([cloud.md](cloud.md)).
  - **`/auth/v1/authorize` stays under the main application.** That is the one place a person meets Access.
  - **Unverified:** that Access always gives the more specific path application precedence. Test on staging with
    `curl`: no cookie on `/api/cad/state` must get Access's login; no cookie on `/api/v1/meta` must reach the hub's 401.
- **Tunnel, web app, station:** unchanged.
- **The risk: the hub is the only gate on `/api/v1/`.** Mitigations:
  - **A strict verifier, before any body is read:** `Authorization: Bearer` only; a fixed format and alg;
    constant-time MAC compare; `exp` with at most 60 s leeway; known `kid`; device and user active. Anything else is
    `401` and the connection is closed (as `/api/station/` does).
  - **No fall-through:** on `/api/v1/` the resolver never consults `access is None` and never yields `who=None`, which
    closes the `CadApi.permit` trap ([§1.4](#14-identity-today)). Cookies are ignored on `/api/v1/` (so CSRF does not
    apply). Device tokens are refused on `/api/cad/*`, and Access cookies on `/api/v1/*`.
  - **Rate limits:** a Cloudflare WAF rate-limiting rule on `/api/v1/` and `/auth/v1/token` (**unverified** on the
    account's plan); in the hub, a token bucket per IP for unauthenticated requests (for example 10 a minute on
    `/auth/v1/token`) and per device for the rest; 429 with `Retry-After`.
  - **Small surface:** only `/auth/v1/token`, `/auth/v1/revoke`, `/api/v1/meta` and `/.well-known/` answer without a
    token, and `meta` reveals nothing beyond versions.
  - **Tests, required in the PR:** every `/api/v1` route returns 401 without a token, and with an expired, tampered,
    wrong-`kid`, `alg`-swapped, revoked-device or deactivated-user token, and with a station bearer, a feed bearer or an
    Access cookie; every `/api/cad` route refuses a device bearer; a code replay, a wrong verifier and a wrong
    `redirect_uri` are refused; refresh-token reuse revokes the device; a route-table test that every `/api/v1` route
    declares its role.
  - **Monitoring:** 401 and 429 rates on Health; a status push on a spike, through the existing error watch.
  - **Key handling:** the signing key in SSM, rotated by `kid`, two keys valid during the overlap.

**(C) Cloudflare Access for SaaS (OIDC). Second choice.** Access can act as a standard OIDC provider with optional
PKCE, a configurable access-token lifetime and refresh tokens; a refresh re-checks the user against the policies with no
browser. The app would run AppAuth against Cloudflare, and the hub would verify Cloudflare-issued tokens against that
client's JWKS. `/api/v1/` would still need B's Bypass. Second choice because revocation is per user session, not per
device (the hub still needs its `devices` table and per-request check); there are two token issuers to reason about; and
**unverified:** whether a public client without a secret is allowed, whether app-link redirect URIs are accepted, and
whether the account's Zero Trust plan includes it. Pick C over B only to avoid running a token issuer in the hub; the
hub-side work is almost the same.

**Rejected outright:** Access service tokens for people (shared, extractable from the app), and WARP device enrollment
on volunteers' own phones (too heavy; revisit for department-managed devices).

**What the app developer gets from the platform side.**

1. A staging hub hostname and a test account, plus Apple and Google review accounts, on staging (PR 3).
2. Client ids (`sizeup-ios`, `sizeup-android`) and the exact redirect URIs. The app side supplies the bundle ID, Apple
   Team ID and Android package with its signing-certificate SHA-256 for the hub's `apple-app-site-association` /
   `assetlinks.json` (config, not secret).
3. The endpoints, in the OpenAPI spec: `GET /auth/v1/authorize`, `POST /auth/v1/token` (code and refresh grants),
   `POST /auth/v1/revoke`, `GET /api/v1/meta`, `GET /api/v1/me` (email, name, role, device id). Error codes:
   `invalid_grant`, `device_revoked`, `user_inactive`, `rate_limited`, `upgrade_required`.
4. Token rules: the lifetimes above; refresh on 401 `token_expired` or at 80% of `expires_in`; single-flight refresh;
   on `invalid_grant` / `device_revoked` wipe local data and go back to sign-in.
5. Storage (App): the refresh token in the Keychain (`kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly`, so a
   push-triggered fetch works while locked after first unlock) or Android Keystore-backed storage; never in backups.
6. AppAuth (iOS and Android) works with these endpoints unchanged.
7. Until PR 2 lands: a local all-in-one hub (no Access, everything open) and a stub or mock of the auth endpoints.

### 3.4 Live updates for mobile

**What phones allow (platform facts, not measured here).** iOS suspends a backgrounded app within seconds to about
30 s, and its sockets die. There is no long-lived background connection except VoIP/PushKit (forbidden for non-calls) or
background location. Android Doze defers network access; a foreground service needs a visible notification and a
declared type. So: **foreground stream + background push + cursor resume.**

| State | Mechanism |
|---|---|
| App in foreground | `GET /api/v1/stream?after=<cursor>` (SSE). Typed events `{kind: incident\|unit\|alarm\|prealert\|…, id, rev, op: upsert\|delete, data}`, each with `id: <cursor>`; resume with `Last-Event-ID`. A heartbeat comment every 15 s (not 1 s state pushes). Filtered per principal (role, sourcekeys) |
| Backgrounded or killed | APNs/FCM ([§4](#4-pushes-to-the-app)). The push carries a reference (kind, id), not the object |
| Back to foreground | `GET /api/v1/changes?after=<cursor>&limit=500`, then reopen the stream. `410 {oldest}` means the cursor is too old: resnapshot with `GET /api/v1/snapshot` (active incidents, units, open alarms) |
| Bad network | The same `changes` endpoint as a long poll (`&wait=25`, under Cloudflare's 100 s) |

**Why not reuse the console's `Broadcaster`:** its ids reset on every handoff; it holds no history; its payload is the
UI blob, the same for every viewer; and it pushes every second even when idle, which costs a phone radio wake-ups.

**Why not WebSockets:** nothing needs client-to-server streaming, SSE already passes the Cloudflare Tunnel in
production, and native SSE clients are small (URLSession bytes, OkHttp-sse).

**One change log** (`sizeup/changes.py`), built once and shared with the planned `/api/feed/v1`: references
`(seq, at, kind, id)` hydrated at read time, an epoch, 7-day retention, 410 when a cursor is too old, 409 on an epoch
change, at-least-once delivery deduplicated by `(kind, id, rev)`. `/api/feed/v1/events`, `/api/v1/changes` and
`/api/v1/stream` are three views of it with different scopes and hydrators.

- The epoch lives in the database, not the process, so it survives handoffs.
- Writes to it are batched off the request path, and none happen at startup (the startup-writes rule, PR #507).
- **Unverified:** which sources besides `radio_changes.seq` already have a monotonic sequence.

---

## 4. Pushes to the app

**Provider: Amazon SNS mobile push** (platform applications for APNs and FCM) through the existing stdlib SigV4
(`aws.sign`), D2.

- **Why SNS:** a direct APNs client needs HTTP/2, and FCM HTTP v1 needs RS256 signing of a service-account JWT. The
  stdlib has neither, so direct pushes would mean new dependencies (`h2`/`httpx`, `cryptography`) in the hub. SNS also
  falls under the outbound guard automatically, because it goes through `aws.sign`.
- **Cost:** SNS mobile push is free for the first 1M publishes a month, then about $0.50 per million (**from memory,
  check**). APNs and FCM are free. Apple Developer Program $99 a year; Google Play $25 once.
- **Unverified:** that SNS passes the APNs `interruption-level` / critical `sound` dictionary and
  `apns-push-type`/`apns-priority` through unchanged. Prove it in the first push PR.

**Critical alerts.**

- **iOS:** the Critical Alerts entitlement needs Apple's approval, granted case by case to public-safety and medical
  apps. **Apply as soon as the bundle ID exists; it can take weeks and can be refused.** Fallback:
  `interruption-level: time-sensitive` (no entitlement; breaks through Focus if the user allows it, not the mute switch).
- **Android:** a high-priority FCM *data* message, a notification channel with `IMPORTANCE_HIGH` and an alarm sound, and
  optionally a full-screen intent. Android 14+ limits `USE_FULL_SCREEN_INTENT` to calling and alarm apps through Play
  policy, so expect review questions. "Override Do Not Disturb" is a per-channel user setting.

**Targeting:** per device, not per topic. In `notify.Pushes`, a kind's message goes to its ntfy topic (unchanged)
**and** to every active device subscribed to that kind whose role allows it:

| Kind | Who may receive it |
|---|---|
| alerts, intel | everyone signed in |
| status | admins |
| dispatch (new, D3) | everyone signed in, or chosen units and companies |

Per-kind device preferences and quiet hours are stored server-side. Fan-out is one SNS `Publish` per endpoint (40
devices, about 40 calls) on the Notifier thread.

**Content rules.**

- **One compose path for ntfy and the app.** Title and body come from the same allow-list and `scrub()` as today: no
  names, talkers, radio IDs or plates. Exact addresses are allowed.
- Payloads cross Apple's and Google's infrastructure, so they carry `{kind, id, title, body}` and nothing more. The app
  fetches details after unlock with its token. On iOS a Notification Service Extension can enrich the notification on
  the device.
- Caps: the intel caps stay for intel. Per-kind caps for dispatch and alerts, generous but present, so a loop cannot page
  40 phones a second.
- **Listen in the app:** the push deep-links to `/api/v1/listen/<event>`, which returns a fresh short-lived media URL
  ([§5](#5-audio-video-and-maps)). The presigned 20-min Listen link stays for ntfy.
- Device pushes never touch ntfy topics, so the protected-topic machinery is unaffected.

---

## 5. Audio, video and maps

| Media | Today | For the app |
|---|---|---|
| Clip / dispatch WAV | `GET /api/audio/<id>.wav`, `/api/cad/dispatch/<id>.wav` with the cookie | `GET /api/v1/media/clip/<id>` with a bearer returns `{url, expires_at}`: an **S3 presigned URL** (clips already in S3) or a **hub-signed media URL** (`/m/<token>`, HMAC, 5 min, bound to the device and the object). AVPlayer and ExoPlayer can't reliably send `Authorization` on streamed media, hence URLs |
| Listen | presigned S3, 20 min, random key | reused as is |
| Format | WAV (no ffmpeg in the hub image) | WAV is about 1 MB a minute. **ffmpeg in the hub image** (D11) gives AAC at 24 kbit/s, 0.18 MB a minute, before the app plays much audio on cellular. The image grows by about 40-80 MB (**unverified**) |
| Camera live HLS | proxied, cookie, 8 streams hub-wide | a hub-signed token in the path; the hub **rewrites playlists** so every chunklist and segment URL carries it. Mobile viewers count against `STREAM_MAX` (8), so the app shows "busy" gracefully. About 26 KB/s per stream |
| DVR / attachments HLS | `/dvr/index.m3u8`, `/attachments/<id>/index.m3u8`, audited | the same token rewriting; views still audited per person and device |
| Site documents | streamed, no links | the same signed path, `Content-Disposition: inline`. Not cached past the session unless "keep for offline" is allowed (D7) |
| Map | MapLibre GL JS + OpenFreeMap, style built in JS | **MapLibre Native** (iOS and Android, BSD) with the same tiles. The style moves to the server: `GET /api/v1/map/style.json`, generated from one source shared with `map-style.js`, so both clients match. OSM data is ODbL: attribution is required in the app |
| Layers | GeoJSON with ETag | v1 wrappers (`/api/v1/layers/<key>?bbox=`), the same caching |

**Unverified:** OpenFreeMap's terms for app traffic and offline packs (free, keyless, no SLA; confirm before shipping
offline maps, or self-host PMTiles of the response area); the camera sources' terms for redistribution to mobile viewers.

---

## 6. Location from phones

What exists: `Cad.positions` with the generic fix shape and staleness; follow, routes and motion built on it (#358); a
plan for rig AVL from a vendor (no API access yet, #43). There is **no responder model**: no "I'm responding", no
person's location.

**Two different features.**

1. **Rig AVL from a device in the rig.** A device enrolled *as a unit* (an admin binds a device to a unit code) posts
   `POST /api/v1/units/{code}/position {lat, lon, at, heading?, speed?, accuracy}` every 5 s while moving. It goes
   straight into `Cad.positions` through `mapview.ingest`, **in memory** (as aircraft and vessels do). At most a
   downsampled track is persisted (one fix every 30 s, kept with the incident). Useful even if a vendor API never comes.
2. **Responder status ("I'm responding").** A new `responders` domain:
   - `POST /api/v1/incidents/{id}/responding {to: station|scene, eta_s?}` (with `Idempotency-Key`), plus cancel and
     arrived.
   - The list is shown on the incident to signed-in roles.
   - Optional location **only while responding**: fixes for at most 20 minutes or until arrival or clear, then the app
     stops by itself.
   - Status events are persisted (audit). Positions stay in memory, plus at most the last fix, purged at clear.

**Privacy (D5):** opt-in per person; never outside an active response; never in pushes; never in machine feeds unless
the department decides otherwise (default no); shown to signed-in roles only; a visible indicator in the app; retention
set by the department.

**Battery (App):** iOS "When In Use" plus background location during the response window (the blue indicator),
`distanceFilter` about 50 m, `activityType = .automotiveNavigation`; Android a foreground service of type `location`.
20 minutes of GPS costs a few percent of battery. No location at any other time.

**Overlap (D4):** IamResponding or another vendor may already do (2) for the department (#43, [cad-plan.md](cad-plan.md)).
Building it duplicates a tool members may already use.

---

## 7. OpenAPI

**Where the specs live (proposed).** Hand-written OpenAPI 3.1 YAML, one file per domain, at the repo root so the app
repo can vendor one directory:

```
api/openapi/v1/openapi.yaml          # root: info, servers (placeholder hosts), security schemes
api/openapi/v1/paths/<domain>.yaml   # auth, meta, incidents, units, alarms, changes, media, push, map, ...
api/openapi/v1/schemas/*.yaml        # Incident, Unit, Alarm, Transmission, Error, ...
api/openapi/feed-v1/                 # the machine feed, sharing schemas/ with v1
```

The server is a hand-routed stdlib handler, so nothing can generate a spec from code.

**Checked in tests.**

- **Route table as code:** `sizeup/api/v1/routes.py` lists `(method, path template, handler, need)`. A test asserts
  that set equals the spec's `paths`, both ways, so neither can drift.
- **Contract tests (pytest)** with dev-only dependencies `jsonschema` and `openapi-spec-validator` (the hub image is
  unchanged). For every operation an in-process server with synthetic fixtures is called, and the status, headers and
  body are validated against the response schema. The spec's request examples are posted and must succeed. Negative
  tests: unknown fields ignored, missing required fields 400.
- **Synthetic examples only:** `example.org` hosts, 555 numbers, fictitious streets, made-up incident numbers and units
  (`E99`). A CI test greps the spec and fixtures for a denylist kept **outside the repo** (in CI secrets) plus generic
  patterns (emails not at `example.org`, real phone formats).
- **Breaking-change check:** a PR that changes the spec gets a `spec-diff` check (`oasdiff breaking`) that fails on a
  breaking change within v1.

**Versioning and deprecation:** as [§3.2](#32-conventions-for-apiv1-and-apifeedv1). Additive within v1; `/api/v2` beside
v1 for at least 12 months; `Deprecation` / `Sunset` headers and `meta`; `min_app` forces upgrades; the last two app
releases keep working.

**Clients (App):** Apple's `swift-openapi-generator` (SwiftPM plugin) and `openapi-generator` `kotlin` (OkHttp +
kotlinx.serialization). The app repo **vendors the spec at a pinned commit** of this repo and regenerates in CI.

**Domain order:** meta + auth + devices → incidents (read) + units (read) + alarms → changes / stream → writes
(status, responding, ack, note) → push registration → media → map style and layers → radio log → pre-plans and
documents → cameras. Settings and admin are **never** in v1: admins use the web.

---

## 8. Everything else a second consumer needs

| Concern | Proposal |
|---|---|
| Rate limiting | Per principal (device, station, feed token), an in-memory token bucket: reads 10/s burst 30, writes 2/s burst 10, 2 concurrent streams per device. 429 + `Retry-After`. Plus the Cloudflare WAF rule on the bypassed paths. The existing `ResetLimit` is the per-action pattern |
| CORS | None for v1 (native apps don't need it). A future third-party web consumer gets an explicit allow-list, never `*`. The console's same-origin model is unchanged (#447) |
| Pagination | Cursor (`after`, `limit`, `next`) everywhere in v1. `/api/cad/search` keeps page numbers for the web |
| Idempotency | `Idempotency-Key` on every v1 POST; table `(principal, key) → (status, body, at)` kept 24 h. "I'm responding", status and ack retried on a flaky network never apply twice. The same key with a different body gets 422 |
| Offline / queued actions | The app queues writes with the key and its own `at`. On replay the server orders by receive time, records `client_at` in the chronology, and refuses actions that no longer make sense (incident closed, unit moved on) with 409 + the current object. The app shows "not applied: …". A dispatch is never auto-merged |
| Clock skew | `server_time` in `meta` and every SSE event; the app computes its offset. A `client_at` more than 5 min in the future is clamped and flagged |
| Concurrency | `If-Match: <rev>` on incident edits (notes are append-only and need none); 412 with the current object. Needs a per-incident `rev` (today only the global `store.rev`) |
| Machine API keys | Not end-user tokens. Service token + bearer + scopes, one key per consumer in SSM, listed on Settings > System with last use, rotated by script. One token per station |
| Audit | `activity.py` gains `device_id`, `app_version` and `via` (web, ios, android). Every v1 write goes through `CadApi.did` as today |
| Observability | `perf.py` path families gain a client dimension `(via, app_version)`. Health tile: active devices, versions in the wild, push success and failure (SNS delivery logs), stream count |
| Backward compatibility | v1 additive only; `min_app` forces upgrades; the last two app releases keep working. App store review and rollout take 1-7 days and users update late |
| Feature flags | Server-side in `meta.features` (from Settings), so a feature can be turned off without an app release. No third-party flag service |
| Staging | **Needed before the first external build** (D8). A second small hub task (0.25-0.5 vCPU, about $11-21 a month) on its own hostname and Access app, synthetic data only, outbound guard on except SNS to a sandbox app. App review needs a working demo login: give Apple and Google a **review account on staging**, never production |
| Distribution | TestFlight (internal: up to 100 team members without review; external: beta review) and Play internal testing (up to 100 testers). Then a public listing, or Apple Business Manager custom apps for a private listing (**unverified** fit for volunteers' own phones) (D10) |
| Repos | The hub, the spec and the contract tests stay in this **public** repo. The app goes in a **private** repo (proposed, D10). The app binary holds no secrets: only the hub URL; everything else comes from enrollment |

---

## 9. Alignment with other work

- **The machine feed (`/api/feed/v1`, planned):** shares the change log ([§3.4](#34-live-updates-for-mobile)), the
  envelope conventions (cursor, epoch, 410/409, `(kind, id, rev)`, additive versioning) and the schema files. The change
  log is built once (PR 5) with a scope set per consumer. Its handler, like v1's, never routes a non-person principal
  into `CadApi`.
- **More stations:** station identity becomes per station (a token each, the station id in the token table, jobs
  targeted at a station). One principal table for devices, stations and feed keys:
  `principals(id, kind: person-device|station|service, …)`, one revocation UI, one rate limiter, one audit dimension.
  Media served to the app comes from the hub's best copy, never from a station directly.
- **Outbound guard (PR #541, #545):** SNS goes through `aws.sign`, so it is guarded for free. Add a test that device
  pushes refuse when outbound is off, and that a hand-started hub with real data cannot push to real phones. Staging's
  SNS platform app uses the APNs development (sandbox) environment.
- **One principal resolver** in `server.py` replaces the scattered checks: the Access cookie or header gives a person
  (web); a device bearer gives a person plus a device (v1); a station bearer gives a station (`/api/station/`); a feed
  bearer gives a service (`/api/feed/`). Each surface accepts exactly one kind and refuses the rest, and that is tested.

---

## 10. Phased plan

**Owner:** **Platform** = the hub, API, auth, push, change log and specs (the existing maintainers). **App** = the
native client (private repo). **Both** = needs work or inputs from each side.

**What the app developer can start now (App).**

- The app shell, navigation, MapLibre Native with OpenFreeMap tiles, and a push spike against a throwaway Firebase /
  APNs sandbox.
- The sign-in UX with AppAuth against a mock of the auth endpoints in [§3.3](#33-auth-for-the-native-app).
- Prototypes against a **local all-in-one hub with invented data** ([development.md](development.md)). Without Access
  everything is open: `/api/cad/state` + `/api/cad/stream` (SSE, `_delta` merge), `/api/cad/incident/<id>`,
  `POST /api/cad/unit/<id>/status`. **These are internal and will change:** use them for UI exploration only, behind a
  thin repository layer that is swapped for the generated v1 client later.
- Long-lead paperwork (Both: the department's accounts): an Apple Developer organisation account (needs a D-U-N-S number;
  **unverified** whether the department has one), the Critical Alerts entitlement request, the Play Console, bundle IDs
  and Associated Domains for the https redirect.

**The PRs, in order.**

| # | PR | Owner | Unblocks | Issue |
|---|---|---|---|---|
| 1 | `sizeup/api/v1` skeleton: route table, error envelope, `GET /api/v1/meta`, OpenAPI root + contract-test harness + spec/route parity test + synthetic-example check | Platform | spec-first work in the app | #557 |
| 2 | Principal resolver in `_admit`; `devices` table; `/auth/v1/authorize` (behind Access) + `/auth/v1/{token,revoke}`; `/.well-known/` association files; Access path application with Bypass (staging first); HMAC tokens (key in SSM); Settings > Users devices and Revoke; fail-closed tests ([§3.3](#33-auth-for-the-native-app)) | Platform (App supplies bundle/team IDs, tests on a device) | sign-in on a real device | #558 |
| 3 | Staging hub: task, hostname, Access app, synthetic seed, review account | Platform | TestFlight / Play internal | #559 |
| 4 | Read resources: `/api/v1/snapshot`, `/incidents` (active + recent, cursor), `/incidents/{id}`, `/units`, `/alarms`, `/me`; per-object `rev` + ETag; role and sourcekeys filtering | Platform | the main screens | #560 |
| 5 | Change log (shared with the machine feed) + `/api/v1/changes` + `/api/v1/stream` (SSE, `Last-Event-ID`) | Platform | a live app, resume after background | #561 |
| 6 | Writes: unit status, alarm ack, incident note; Idempotency-Key table; `If-Match`; activity with device and version | Platform | crew actions | #562 |
| 7 | Push: `/api/v1/devices/{id}/push` registration, SNS platform apps, `Pushes` fan-out by kind and role, preferences, caps, the shared scrub, outbound-guard tests | Both (App: APNs key / FCM project, token registration, notification handling) | background alerts | #563 |
| 8 | Media: signed media URLs, clip and Listen endpoints, HLS token rewriting for live, DVR and attachments; ffmpeg in the image for AAC | Platform | audio and cameras | #564 |
| 9 | Map: server-side `style.json` shared with the web, `/api/v1/layers/*` | Platform | matching maps | #565 |
| 10 | Per-principal rate limits, `perf.py` client dimension, Health tile, `min_app` enforcement, deprecation headers | Platform | production rollout | #566 |
| 11 | (after D4/D5) Responders: "I'm responding", optional location while responding; unit-bound device AVL into `Cad.positions` | Both | who is coming, AVL | #567 |
| — | In parallel: #446 item 1 (no-op rev bumps) before a mobile beta; then measure, then DVR out (E) or 1 vCPU (F) (D9) | Platform | headroom | #446 |

The app consumes each platform PR as it lands: regenerate the client from the merged spec, then build the screen.

---

## 11. Decisions

Tracked as a checklist in #556.

- **D1** Auth model: (B) Access sign-in hands off to hub-issued per-device tokens, with an Access Bypass on `/api/v1/`,
  `/auth/v1/token|revoke` and `/.well-known/` (recommended); or (C) Access for SaaS OIDC tokens verified by the hub. (A)
  is rejected. Also: add an identity provider to Access, or stay on email PIN?
- **D2** Push provider: SNS (recommended, no new hub dependencies) or direct APNs/FCM (new dependencies
  `h2`/`cryptography`).
- **D3** Does sizeup push **dispatches** to members' phones (a new responsibility next to the department's official
  paging and the third-party app), or only alerts, intel and status as today? If dispatches: the department's sign-off,
  and the critical-alert entitlement becomes essential.
- **D4** Responder status and location: build it in the app, or integrate a vendor (#43)?
- **D5** Location privacy: opt-in, response-only, retention (suggested: positions purged at clear, status kept with the
  incident).
- **D6** Who may enroll a device: everyone with a sign-in, or per role? Devices per person (suggested limit: 3)?
- **D7** Offline caching of pre-plans and site documents on phones (useful at a scene; a lost-device exposure).
  Suggested: off by default, admins decide per document kind.
- **D8** Staging environment: approve about $11-21 a month.
- **D9** CPU headroom: the DVR in its own task, or 1 vCPU (+$15 a month), after re-measuring.
- **D10** The app repo private (recommended), and the distribution path (TestFlight / Play testing, then a public or a
  private listing).
- **D11** ffmpeg in the hub image (AAC audio for phones).

## 12. Risks

| Risk | Mitigation |
|---|---|
| A new auth path reaches `CadApi` without a person and is treated as unrestricted (`cad/api.py:697`) | The principal resolver; v1 handlers build a person `Identity` or answer 401; tests for every negative case |
| The Bypass on `/api/v1/` exposes the origin path to the internet | A fail-closed bearer check before any body is read, the WAF rate limit, short tokens, reuse detection, no unauthenticated endpoints beyond `/auth/v1/token`, `/auth/v1/revoke`, `/api/v1/meta` and `/.well-known/` |
| A lost or stolen phone with a cached session | Revoke in Settings, role re-read per request, Keychain / Keystore, the app's cache wiped on `device_revoked`, no offline documents by default |
| Push content leaks through Apple / Google | References plus a scrubbed title and body only; the no-names rule enforced in the one compose path |
| Critical Alerts entitlement refused | Time-sensitive notifications and guidance to members. Don't promise "breaks through silent mode" until approved |
| Old app versions in the wild | v1 additive only, `min_app`, two releases supported, contract tests + `oasdiff` in CI |
| A loop pushes to every phone | Per-kind caps, a kill switch for every kind (intel's `policy` pattern), the outbound guard on non-production hubs |
| Hub CPU (DVR + streams) | Fix #446, measure with `perf.py`, then DVR out or 1 vCPU. Heartbeats every 15 s, not 1 s pushes |
| Change-log writes add Litestream frames and S3 cost | Batched off the request path, references only, created lazily, positions never logged |
| Real data in the public spec or fixtures | Synthetic examples, a CI denylist from secrets, PRs grepped for exact strings |
| No staging today, so app review hits production | Staging before the first external TestFlight build; the review account only on staging |
| Handoffs mid-stream | 503 + Retry-After already exists. The cursor is database-backed, so a reconnect after a handoff resumes instead of resnapshotting |

## Could not verify

- The hub's CPU split today (DVR versus the rest); `perf.py` in production has the answer.
- Exact Fargate, SNS and image-size numbers (list prices from memory).
- SNS passthrough of iOS critical and time-sensitive fields.
- OpenFreeMap's terms for native and offline use; the camera sources' redistribution terms.
- Cloudflare plan features (WAF rate limiting, Access for SaaS); Access path precedence with a Bypass application beside
  the main one (test on staging); whether Access for SaaS OIDC allows public clients and app-link redirect URIs.
- Whether the department will add an identity provider to Access (today: email PIN).
- Which hub tables already have monotonic change sequences for the shared change log.
- Whether the department can get an Apple organisation account (D-U-N-S) and the Critical Alerts entitlement.

## Sources

Cloudflare, checked 2026-11-15:

- CLI token and the `cf-access-token` header: https://developers.cloudflare.com/cloudflare-one/tutorials/cli/
- Authorization cookie and session durations (24 h default):
  https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/
- Service tokens: https://developers.cloudflare.com/cloudflare-one/identity/service-tokens/
- Access for SaaS, generic OIDC (PKCE, refresh tokens, endpoints):
  https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/saas-apps/generic-oidc-saas/
- The cloudflared token transfer flow: `token/transfer.go` in the cloudflared source (not official documentation).
