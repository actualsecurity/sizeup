# sizeup roadmap

Where this is going, in what order, and what has to be decided first. Updated when the direction changes, not every PR.
Day-to-day work lives in [GitHub issues](https://github.com/actualsecurity/sizeup/issues).

Last rebuilt from the issues and PRs on 2026-11-14 (main at `56dc3c7`, PR #522). A bare `#n` is an issue; PRs are marked
"PR".

## What sizeup is for

A self-dispatch CAD that Port Chester FD *wants* to use. It's clear, fast and adjustable to how the department works, and
it hears the radio so the screen keeps up with the call by itself. It supplements county dispatch (60 Control); it
doesn't replace it.

## How work is tracked

- **Issues** are the backlog, one per piece of work or open question. PRs close them (`Fixes #n`). Anything raised in
  conversation gets an issue.
- **Labels:** `now` (this week), `next` (ready to pick up), `later` (wanted, not soon), `waiting` (blocked on the
  department, a vendor, hardware or a decision).
- **No process beyond that** (no sprints, estimates or boards) until more people are working on it.
- **This file** holds the narrative and the decisions. The detail lives in the issues, [cad-plan.md](cad-plan.md),
  [cloud.md](cloud.md) and [architecture.md](architecture.md).

## Where it stands (November 2026)

| Part | State |
|---|---|
| Hub | AWS ECS Fargate, 0.5 vCPU (PR #298), behind a Cloudflare Tunnel and Access. Sign-in by email one-time PIN; roles admin, dispatcher, viewer. A merge to main deploys it, hot/cold with a standby task and an S3 single-writer lock (PR #254, #292). Litestream replicas in S3, replicating before any migration (PR #507) and checked continuously (PR #506). |
| Station | The PC with the RTL-SDRs, at the South End firehouse (PR #273). Outbound HTTPS only; buffers while the hub is unreachable; follows the hub's build. Installer for Windows (WSL2) and Linux / Raspberry Pi (PR #28). The move to the Windows PC is still an open issue (#32). |
| Radio | South 2 paging, Fire/EMS 15 (P25), FG1, FG2, HPN Tower / NY Approach / HPN Ground (AM), and a second P25 system (MTA MRRS: police and Metro-North, context only, PR #419). Audio in S3 with retention per channel and talkgroup (PR #422, #454). |
| Speech-to-text | Amazon Transcribe on the main channels, HPN ATC and PD dispatch, each with its own vocabulary ([stt-vocabulary.md](stt-vocabulary.md)); whisper on the station elsewhere and as the fallback. Clips skipped while busy are caught up later (PR #515). |
| Incidents | Ranked sources: external CAD feeds first (private, station-only adapters, PR #304, #305), South 2 paging as the backup, radio and phone as pre-alerts only. Two dispatch modes: 60 Control dispatching (tracked automatically) and PCFD dispatching (PR #318). |
| CAD | Events, run cards, dispatch wording said by Amazon Polly, unit boards, radio traffic per incident, search and address history (PR #421), pre-plan points and site documents (PR #458, #464), a read-only wall board at `/monitor`. |
| Maps | County GIS layers, cameras with a DVR, road projects, trains, aircraft, vessels, weather, rigs with routes and follow. |
| Transmit | Off. The Dispatch button logs and plays locally (#41). |

## Shipped

By area. PRs before #103 (the first monitor, the CAD's first version, the cloud split, the station installer) are not
itemised. An issue marked "open" is still open after the PR listed for it merged.

### Radio and speech-to-text

| What | PRs | Issues |
|---|---|---|
| Speech-to-text catch-up for clips skipped while busy | #515 | |
| Other agencies' emergencies quieted; Settings > Radios > Emergencies | #514 | |
| FM squelch: no transmission on noise openings, per-channel squelch, key-up tones | #476 | |
| Radio page: views, paging and the source tree on the server; the rail layout | #441, #442, #484 | #415 (open) |
| Second P25 system (MTA MRRS), talkgroup tiers, system-qualified sources | #419 | #409 |
| Reception test from Settings > Radios, every P25 system | #436, #479 | #410 (open) |
| Radio audio in S3, retention per channel / talkgroup; incident audio kept until an admin deletes it | #422, #454, #443 | #414, #440 (open) |
| County-wide Transcribe vocabulary; police STT profile and budget; the AWS price corrected | #435, #495 | #434, #490, #423 |
| Whisper backlog: strict priority, context talkgroups shed; AWS deadline and breaker; short key-ups skipped | #306, #197, #424 | #300, #195 |
| HPN ATC: tower channel, airport pre-alerts, who is talking, written-form transcripts, several frequencies per dongle | #238, #256, #259, #266, #267, #274, #289, #326, #332, #353, #356, #372, #381 | #233, #252, #257, #260, #263, #270, #275, #307, #350, #369, #380 |
| Emergency alarm (P25 / MDC-1200 / FleetSync button, mayday); stuck flags bounded | #142, #173, #191 | #128, #190 |
| Tones: the tone book, pairs learned from the air, one page per tone group | #121, #127, #184, #192 | #115, #122, #183, #47 |
| FG1 / FG2: identified radios only, new radio IDs, FG2 as a channel | #167, #172, #178, #180 | #163, #177, #119 (open: more channels) |
| Unit status suggestions from the radio; status words put back | #143, #391 | #37, #386 |

### Radio intelligence (pre-alerts from radio traffic)

| What | PRs | Issues |
|---|---|---|
| The context-transcript hook (plan PR 0) | #483 | |
| Replay / backtest over stored radio rows (`tools/intel_replay.py`), STT evaluation (`tools/stt_eval.py`), admin transcript corrections and their export (plan PR 7) | #491 | #487, #428 (open: the archive) |
| The INTEL push kind | #486 | |
| The core and the police pack: rules, window, events, policy, push, Settings > Intel, the console card, the events API (plan PR 1) | #492 | part of #411 |
| Police STT profile, police AWS budget, PD audio budget (plan PR 3) | #495 | #490 |
| Listen links on the push, radiogeo depth (spoken intersections, blocks, relative cues, exits and markers, cross streets) and the inference stage (Bedrock, off by default, capped; backs the rules, never pushes alone): the content of PR #499, #502, #504 | #517 | part of #411 |

### Incidents and dispatch

| What | PRs | Issues |
|---|---|---|
| Incident addresses stored normalised, the source's text kept for matching; source health hysteresis so a station restart never flips the primary; a shown failing stays bad until ok holds | #475, #471, #524 | |
| Ranked incident sources with automatic fallback; private adapters on the station only; adapter credentials from SSM | #304, #305, #417 | #297, #301 |
| Dispatch modes and the per-incident radio log | #318 | #311 |
| Pages joined to feed incidents; full department vs specific units | #367, #373, #387, #398, #399, #519 (full department from the tones and the feed) | #365, #370, #518 |
| Department placeholders: assigned first, released when their unit signs on | #400, #431 | |
| Dispatch answers before its voice is rendered | #429 | #426 |
| CAD search, History here, premise notes | #421 | #403 |
| Pre-plan points and site documents on the call's map | #458, #464 | #456, #459 |
| Units back to Available when their call closes | #236 | #232 |
| AWS vocabulary phrases shown without their hyphens (on arrival and on read) | #519 | #518 |
| Pre-alert topics in Settings > System > Push notifications; the protected set derived, fails closed; a static hashed backstop under it; no private names in the repo | #521, #526 | |
| Page geocoding: cross streets, read-backs, named places; intersection search; proper-case addresses | #181, #214, #388 | #176, #213 |
| Airport events: FAA Alert 1/2/3, the souls scale | #109 | #105 |
| Incident view and new call entry: layout, comments, rig tiles, resizable map | #207, #208, #211, #212, #222, #223, #224, #234 | #204, #205, #209, #210, #216 to #221, #231 |

### Maps and layers

| What | PRs | Issues |
|---|---|---|
| Rigs: follow, routes by road, estimated motion, overhead icons, firehouse bay doors (Rye Brook: back in through the PD lot), local streets first | #359, #361, #363, #377, #395, #396, #432, #455, #477, #478, #485 | #358, #360 (open), #376, #384, #393, #427, #453, #473 |
| Vessels (AIS): aisstream.io and our own receiver ([vessels.md](vessels.md)) | #416 | #402 |
| Trains: Metro-North on the map, to scale, TrainTime data, numbered tracks, poles and mileposts | #138, #152, #165, #166, #174, #175, #366, #374, #378, #389, #392, #394, #452 | #131, #151, #162, #164, #362, #371, #375, #379, #385, #390, #451 |
| Aircraft near HPN: two public feeds, our own ADS-B receiver, emergency squawks, follow | #161, #169, #171, #182, #240, #265, #272, #287, #309, #319, #333, #355 | #155, #179, #235, #262, #269, #279, #313, #315, #352 |
| Cameras: Thruway, county and 511NY sources, live video, the wall, the DVR (on EFS) | #106, #108, #114, #134, #148, #170, #249, #288, #294, #310, #324, #325, #328, #335, #343, #344, #348 | #99, #101, #113, #125, #147, #168, #276, #277, #320, #321, #322 |
| Road projects and closures | #189, #228, #239 | #185 (open), #225, #237 |
| County GIS layers, real icons, per-map layer sets, hydrants | #103, #117, #140, #227, #230 | #91, #111, #112, #226, #229 |
| Weather and water: NWS alerts, conditions, radar, tides | #145 | #132 |

### Hub, station and operations

| What | PRs | Issues |
|---|---|---|
| Litestream: one file to S3 every 30 s, not every second (S3 request costs); a TRUNCATE checkpoint before Litestream is started again under the app | #522 | |
| Replicate before migrate; replica watch; a fenced task writes nothing; store transaction safety | #497, #506, #507, #509 | #498 (open), #501 |
| Hot/cold deploys with a standby task; faster start; deploy stops on a missing secret | #254, #255, #292, #469 | #247 (open), #251, #264 |
| Settings > Station > Adapter: the station adapter's password set from the hub, write-only | #465 | #460 |
| Station logs to CloudWatch through the hub; Settings > Station | #493, #494 | #488, #489 |
| Built-in profiler; hub measured and sped up; 0.5 vCPU | #299, #298, #437 | #284, #425 |
| Station health, alerts, push topics (status vs alerts), per-alert mute, error-rate notices | #144, #199, #206, #250, #261, #290, #342 | #79, #203, #242, #258, #285, #340 |
| trunk-recorder as its own service on the station | #253 | #71 |
| Settings > Data sources: refresh now and on a schedule | #187, #194 | #186 |
| Idle traffic cut: push only what changed | #146, #154 | #137, #153 |

### Development and tests

| What | PRs | Issues |
|---|---|---|
| Flaky timing tests made reliable; pytest in parallel | #295, #347, #357, #433, #516 | #282, #346, #430 |
| Real Litestream in CI | #511, #512 | |
| Private real-data replay fixtures; fictional aircraft in public fixtures | #302, #334 | #283, #293 |
| `CLAUDE.md` with the definition of done; concise UI copy | #286, #291, #330 | #280, #278, #303 |

## In flight (open PRs)

| PR | What | Issue |
|---|---|---|
| #462 | Street view: 360° street imagery in the CAD maps | #461 |
| #520 | Documentation refresh (this PR) | |
| #528 | Settings: no redraw under you; a refused save says why next to its field | #527 |

## Now

| Area | Issues |
|---|---|
| Speech-to-text | #196 the station's whisper can't keep up; #124 free fixes (the price is done, #423; the hyphens, #518, are done) |
| MRRS (umbrella #408) | #410 reception probe (PR #436, #479 merged); #411 PD pre-alerts and scene-safety markers; #412 learned PD roster; #413 Metro-North / MTA PD traffic on trains and track locations |
| Hub | #498 the 2026-11-13 outage write-up (PR #497, #506, #507, #509 merged; the process items remain); #523 a replica can diverge when Litestream restarts under the app (the TRUNCATE checkpoint merged in PR #522; issue open); #446 hub CPU; #247 deploys without downtime (PR #254 merged); #33 settings after the October deploys |
| Station | #32 move to the Windows PC |
| Settings | #527 the page redraws under you, typed text is lost, a refused save shows no reason (PR #528) |
| Incidents | #51 the two call-notification websites as a feed; #323 incident attachments stored one way (backend in PR #328, #344) |
| Maps | #360 probable routes (PR #361, #363 merged); #185 road projects layer (PR #189 merged) |
| Development | #281 split the largest files; #364 a per-test timeout; #317 two races in test code |

Several `now` issues are still open after their main PR merged (#410, #247, #360, #185). Each needs a check for what is
left, then closing.

## Next

| Area | Issues |
|---|---|
| Radio intelligence | #510 event tie and move tuning after PR #492; #448 privacy-review follow-ups for context systems |
| Radio | #119 more channels (the Port Chester repeater, PC local fireground); #418 Metro-North train dispatch on analog VHF; #415 Radio page navigation; #397 read-backs merged into a unit's clip; #444 dispatch voice playback; #440 incident audio kept until deleted (PR #443 merged); #75 place chips in the Radio list; #38 transmission menu targets |
| Audio | #428 training / evaluation archive: daily bundles in cold storage |
| HPN ATC | #351 re-check an accident pre-alert against ADS-B; #383 controller wording credited to the named aircraft; #158 airport status and TFRs |
| Incidents | #349 a feed event with no times; #66 EMS calls on 60 Control pages; #34 the full-response pick on the server; #104 fire district polygon as the "ours" check |
| Maps | #461 street view (PR #462); #470 CAD header wider than an iPad; #141 hide a wrong county GIS feature; #156 stream gauges; #157 lightning density; #74 Connecticut mile markers; #78 @ cameras in the place search; #308 camera follow-ups |
| Fireground | #130 benchmarks, elapsed-time notices, PAR prompts; #129 first-due card per address |
| Hub and station | #447 same-origin check on POSTs; #405 nightly restore drill; #36 rehearse a restore; #337 a draining app answers page loads with JSON; #314 incident-source adapters as their own service and user |
| Mobile app and `/api/v1` ([mobile-api.md](mobile-api.md)) | #557 the `/api/v1` skeleton, OpenAPI and contract tests; #558 device sign-in (Access hand-off to per-device tokens) |
| Tests and CI | #513, #500, #449 flaky tests; #457 two tests fail on macOS (`select()`); #445 the 15-minute CI limit; #39 seed-dependent noise test; #338 real aircraft in public fixtures; #40 UI consistency check |

## Later

| Area | Issues |
|---|---|
| Storage and reports | #406 long-term archive and analytics copy; #46 retention policy; #133 monthly response report; #345 radio backfill retention |
| Maps | #467 street view in a sandboxed iframe; #508 tap area of a parked rig; #407 train speed step; #382 track poles hardening; #368 TrainTime filter state; #92 newer terrain and building layers; #88 live aircraft near HPN (the layer and our receiver shipped in PR #161, #240); #44 pre-plans and hydrants (hydrants, points and documents shipped in PR #227, #458, #464) |
| Site documents | #505 render uploads under their own user; #463 documents offline on the rigs' iPads |
| Incidents | #474 a bare number in a feed's unit list |
| Radio | #450 radio views follow-ups |
| Station | #42 second station for redundancy; #339 refuse a non-localhost Host |
| Repo | #296 third-party ntfy topic names out of the public repo |
| Mobile app and `/api/v1` | #559 staging; #560 read resources; #561 change log and stream; #562 writes; #563 device push (SNS); #564 media; #565 map style and layers; #566 rate limits and rollout; #567 responders and device AVL (also `waiting`, D4/D5) |
| Tests | #481, #480, #468 flaky tests; #529 a speech-to-text re-run test failed once under parallel pytest |
| Also `waiting` | #45 station alerting; #43 responder status and location from a vendor; #41 transmit over a PCFD radio |

## Waiting

| Blocked on | Issues |
|---|---|
| The department | #52 what sizeup is to the department; #50 sign-off on run cards, subtypes and unit facts; #49 radio IDs per rig; #48 PTT-ID at keypress; #45 station alerting; #41 transmit |
| A decision | #123 Whisper turbo (hosted or our own box) or stay on AWS; #556 mobile app decisions D1-D11 |
| A vendor or agreement | #43 responder status and location; #188 road closure sources (keys, agreements); #159 pipelines and Tier II hazmat data; #100 CTDOT cameras (written consent); #98 camera sharing agreements |
| Not stated in the title | #420 CAD query phase 3: summaries with citations, natural language to search filters |

Unlabelled: #503 (reception test race; PR #516 names it, still open).

## Larger arcs

Only arcs an issue or PR backs. Each row says what exists and what is planned.

| Arc | Exists on main | Planned | Backing |
|---|---|---|---|
| Radio intelligence | The core, the police rule pack, Settings > Intel with 7-day counts per rule, the console card with Dismiss and feedback buttons, the events API, Listen links, radiogeo depth, the inference stage (off until Settings turn it on and AWS is set up) | a police activity map layer on the events API (plan PR 5, named in PR #492); a dry run on save in Settings > Intel (plan PR 6); scene-safety markers (#411); the learned PD roster (#412); tie and move tuning (#510); train and track locations (#413) | #408, #411, #412, #413, #510, #487; PR #492, #517, #491 |
| Whisper hosting | whisper.cpp on the station; AWS on the main channels | Decide between hosted Whisper turbo, our own box, or AWS (#123); the station's backlog (#196) | #123, #196 |
| Rig AVL | The map side: one fix per unit into `Cad.positions`, follow, routes, estimated motion | A provider. None is built until a source is chosen ([cad-plan.md](cad-plan.md)) | #358 (closed: the map side), #43 |
| Audio archive | Audio in S3 with per-channel retention | Daily bundles in cold storage for training and evaluation sets | #428 |
| Transcript corrections and replay | Admin corrections, an export, `tools/stt_eval.py`, the intel replay (PR #491) | Fine-tuning on corrected clips, which needs the audio archive | #487, #428 |
| Street view | | 360° street imagery in the CAD maps; later a sandboxed iframe | #461, #467; PR #462 |
| iPad header | | The CAD header fits 820 px | #470 |
| Mobile app and `/api/v1` | The web console's internal `/api/cad` routes; Cloudflare Access sign-in; ntfy pushes; `Cad.positions` | A versioned `/api/v1` inside the hub with OpenAPI specs and contract tests, per-device sign-in after Access, a shared change log with SSE, device pushes through SNS, signed media URLs ([mobile-api.md](mobile-api.md)). The platform side builds the API; the native app is built against it in a separate repo | #556 (decisions), #557 to #567 |
| Flaky tests | PR #516 | #513, #500, #449, #481, #480 remain; #503, #468, #457 are named by PR #516 and still open | as listed |
| Outage follow-up | Replicate before migrate, replica watch, fencing, store safety | The process items in the write-up | #498 |

Not listed, because no issue or PR of their own backs them yet:

- raising any of the log-only intel packs (`county_police`, `ems`, `fire`) above `log`: tune them on replayed traffic
  first, then an always-alert list of single rules;
- more Settings > Intel feedback and statistics than the above;
- marine VHF channel 16.

File an issue before adding them here.

## Phases

The long view, with the status as of 2026-11-14.

### 1. Trial (now)

A few people use it alongside the real system and say what's wrong.

- Station moved to the Windows PC and left running unattended through restarts and a power cut (#32, open)
- Settings caught up after the October deploys (#33, open)
- Feedback from the early users, filed as issues

**Done when:** the station has run unattended for a few weeks, early users rely on it to follow calls, and the department
has seen it.

### 2. Dependable

Things that have to be true before anyone relies on it during a call.

| Item | Status |
|---|---|
| Someone is alerted when the station goes quiet, and when it recovers (#35) | Done (PR #144) |
| Unit status suggestions from the radio: suggest, a person confirms (#37) | Done (PR #143) |
| Deploys without downtime (#247) | PR #254, #292 merged; issue open |
| Replicas verified; failed takeovers pushed | Done (PR #506, #507) |
| The backup restored once on purpose, the steps written down (#36, #405) | Open |
| Dispatch logic only on the server (#34) | Open |
| A retention policy for audio, transcripts and activity (#46) | Open; audio retention is configurable (PR #422) |
| Optional: a second station for redundancy (#42) | Open |

### 3. A department tool

Needs decisions that aren't technical (#52).

- Run cards, subtypes and unit facts signed off as PCFD policy (#50)
- Radio facts confirmed: radio IDs per rig (#49), PTT-ID at keypress (#48). The Rye Brook tones are done (#47).
- Ownership of the accounts and the cost once past the test, and who administers it
- **Transmit** over a real PCFD radio (#41), with FCC coverage, department approval and the hardware. A person presses
  every transmission, with a hard key-time limit.
- Station alerting (#45)

### 4. Connected

| Item | Status |
|---|---|
| Call detail from external incident feeds | Done as ranked sources (#297); the two notification websites remain (#51) |
| Pre-plans and hydrants on the event map (#44) | Hydrants (PR #227), pre-plan points and site documents (PR #458, #464) merged; issue open |
| Responder status and location from a vendor, or our own iPad app (#43) | Waiting; the map side is ready (#358) |
| Reports and exports the department needs | CSV exports exist (`sizeup/cad/export.py`); the monthly report is #133 |

## Decisions still open

These shape phases 3 and 4. Tracked in #52.

1. **What sizeup is to the department:** a trial tool, a supplement to county dispatch, or PCFD's self-dispatch of record.
   This sets how much policy, sign-off and liability work follows.
2. **Who owns it and pays:** the AWS, Cloudflare and GitHub accounts, and the cost after the test.
3. **Who uses it:** who administers, who dispatches, who only views, and how new people get access.
4. **How feedback comes in:** chat today. Later, perhaps a feedback button that files an issue.
5. **Which speech-to-text engine** carries the main channels (#123).

## Rules that don't change

- Nothing fake on screen. Unknown is shown as unknown, never guessed. Estimates are marked as estimates.
- No autonomous transmit. A signed-in person presses every transmission, after a preview.
- The backend doesn't depend on the UI: business logic sits on the server behind the API.
- The station only calls out; nothing reaches into the firehouse network.
- Never publish to Tapout's ntfy topics (reading is fine).
- Police traffic is pre-alert and safety context only, never an incident source. Pushes carry no names (#408).
- Private incident-source adapters and their details stay on the station and out of this repo (#301).
- Secrets stay in SSM, the Keychain or root-only files, never in the repo or logs.
- The repo is public: no real incident data, transcripts or observed radio IDs in code, tests, issues or PRs.
- Internal department documents are never published.
- No database write before Litestream is replicating it (PR #507).
