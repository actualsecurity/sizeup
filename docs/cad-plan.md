# sizeup CAD: self-dispatch for Port Chester FD

Started 2026-10-07 on branch `cad`. sizeup grows from "listen to 60 Control" into a small computer-aided dispatch system
that PCFD runs for itself. The radio recording, transcription, and map stay as features that feed it.

This is the CAD's original plan, kept for history with its status. Current direction: [ROADMAP.md](ROADMAP.md). How it
is built now: [architecture.md](architecture.md) and [cloud.md](cloud.md).

## Status as of 2026-11-14

| Planned item | Status | Now |
|---|---|---|
| Incidents with a number, type, subtype, location, remarks, chronology (goal 1) | Done | `sizeup/cad/store.py`, `sizeup/cad/service.py`, lists from `data/cad_lists.json`. |
| Events created from 60 Control pages, accepted by a person | Changed | Ranked incident sources: external CAD feeds first (private, station-only adapters), paging as the backup (`sizeup/cad/sources.py`, #297, #301). In "60 Control dispatching" mode a feed's incident that lists our units is accepted and tracked automatically; in "PCFD dispatching" mode a person does it (`sizeup/cad/automode.py`, #311). |
| Run cards, editable, skipping unavailable units (goal 2) | Done | `sizeup/cad/runcards.py`, Settings > Run cards. Also full department, "any apparatus", and estimates by road (`sizeup/cad/eta.py`, `routes.py`). Not signed off as policy (#50). |
| Unit status board, set by a click (goal 3) | Done | The two boards from `data/cad_lists.json`. |
| Statuses suggested from the radio, a person confirms (goal 3, M3) | Done | `sizeup/cad/statushints.py` (#37, PR #143). |
| Dispatch: tones, generated voice, preview (goal 4) | Done, voice changed | `sizeup/cad/dispatcher.py`. The voice is Kokoro locally or Amazon Polly on the hub, with macOS `say` only as the fallback (`sizeup/tts.py`, `sizeup/polly.py`), not `say` alone. |
| Transmit through a real radio (goal 4, M2) | Still planned | `AudioPttTransmitter` in `sizeup/cad/transmit.py` is a stub that refuses to construct. `[dispatch] transmit = true` is refused at startup. Waiting on hardware and authorization (#41). |
| Incident board and map, radio traffic on the incident (goal 5) | Done | Plus a per-incident radio log (`sizeup/cad/radiolog.py`) and a wall board at `/monitor`. |
| `/cad` next to the monitor page, on one machine | Changed | `/cad` is the app. It runs all-in-one, or split into a station and a hub in the cloud behind Cloudflare Access ([cloud.md](cloud.md)). |
| CSV exports with the sheet's columns (M1) | Done | `sizeup/cad/export.py`. |
| Incident close-out (M3) | Done | Disposition and close; units go back to Available (PR #236). |
| Reports (M3, M4) | Partly | CSV exports and CAD search with address history (PR #421). A monthly response report is #133. No NFIRS export, and no issue for one. |
| Pre-plans and hydrants (M4) | Done in part | Hydrants as a county GIS layer (PR #227); pre-plan points and site documents on the call's map (`sizeup/cad/preplan.py`, `preplan_docs.py`, PR #458, #464); premise notes (`premise.py`). Issue #44 is still open; a first-due card is #129. |
| Mutual aid (M4) | Partly | The "Other / mutual aid" board and joint-response departments exist. Mutual-aid requests as their own workflow are not built; no issue. |
| Station alerting: lights, printer (M4) | Still planned | #45, waiting. |
| Member notification bridge, acknowledging from a phone (goal 6) | Still planned | Tied to the vendor question (#43). |
| Live unit locations (AVL), the map side | Done | `Cad.positions`, follow, routes, estimated motion (#358, #360, #453). |
| Live unit locations (AVL), a provider | Still planned | No source chosen: IamResponding, Red Alert or our own iPad app (#43). |
| The iPad app reached over a VPN | Changed | The hub is reached through Cloudflare Access, so no VPN is needed. The app itself is not built. |

The original plan follows, unchanged except where a line says "now" or gives a status.

## What it does (goal)

1. **Incidents, ours.** Every call for PCFD becomes a CAD event with a number (26-000001: year,
   dash, sequence, as on the department's sheet), a type and subtype, a location (geocoded on our
   offline Westchester gazetteer, Port Chester and Rye Brook first), caller details, remarks and a
   chronology.
   - The vocabulary (types, subtypes, dispositions, unit statuses, the two unit boards, colors,
     dispatch wording) comes from the department's Google Sheet "[TEMPLATE] PCFD ECC OPS", which the
     console replaces: `data/cad_lists.json`, codes as in the sheet, readable names on screen.
   - Emergency events go Pending (accepted) -> Active (units dispatched) -> closed with a
     disposition. Non-emergency ones (notifications, road closures, hydrants) are logged and tracked
     until closed, with no dispatch; one with an Until closes by itself (Expired) when it passes.
   - Created automatically from a 60 Control page or Fire/EMS 15 dispatch that names Port Chester
     (sizeup already detects these). Waits in "From 60 Control" until a person accepts it.
   - Or entered by hand: a walk-in, a phone call, a still alarm, a mutual-aid request.
2. **Run cards.** Per call type (structure fire, fire alarm, CO alarm, MVA, EMS assist, wires down,
   water rescue…), the units to send and in what order ("Structure fire: 3 engines, ladder, rescue,
   2398 Duty Officer"). The units come from PCFD's roster (Engines 59–64, Ladder 31, Tower Ladder 2,
   Rescue 40/41, Marine 40, chiefs 2391–2398). Edited in the web app. The recommendation skips units
   that are out of service or already committed and offers the next one.
3. **Unit status board.** Available / dispatched / en route / on scene / transporting /
   back in service / out of service, with times.
   - Set by a click.
   - Later, also from radio: MDC IDs on FG1 and talker IDs plus phrases on Fire/EMS 15
     ("Engine 60 on location") become suggested status changes a person confirms.
4. **Dispatch.** One click builds the dispatch:
   - **Tone-out:** the station/department alert tones (two-tone sequential, configurable).
   - **Voice:** generated speech. Example: "Port Chester Fire, Engine 60, Ladder 31, Rescue 40,
     respond to 209 Westchester Avenue, cross of Pearl Street, for a structure fire. Time out 14:32."
   - **Preview:** the operator hears it in the browser first.
   - **Transmit:** sizeup keys a real radio and plays the dispatch, either on FG1 or on the Port
     Chester repeater as PCFD's own paging channel.
   - Every transmission is logged on the incident timeline with its audio.
5. **Incident board and map.** Active incidents, assigned units and elapsed times, plus the existing
   live map, radio traffic and transcripts attached to the incident.
6. Later, to work through with the department: pre-plans and hydrants per address, mutual-aid
   requests, a member notification bridge (IamResponding or text), reports and NFIRS export,
   station alerting (lights, printer), and acknowledging units from a phone.

## Transmitting: hardware and rules

- **RTL-SDRs only receive.** To page or dispatch, sizeup drives a real PCFD radio:
  - Typically a Motorola mobile or desk set on FG1 or the PC repeater.
  - Connected through an audio/PTT interface: a USB sound-card interface with a PTT line (e.g. a
    CM108-based board or a commercial dispatch interface) into the radio's accessory port.
  - sizeup plays the tones and voice into the radio's mic input and asserts PTT for exactly the
    length of the audio, with a hard maximum key time.
- **Licensed channels.** PCFD's FCC authorization must cover this use on FG1 and on the repeater, the
   radio must be programmed and keyed by the department, and it must not interfere with 60 Control or
   county operations. sizeup supplements county dispatch; it doesn't replace it.
- **A person dispatches.** There's no autonomous transmit. Every transmission is a button press by a
   signed-in operator, with a preview first. Transmit is off in config until the hardware is installed
   and authorized (`[dispatch] transmit = false`). Until then the "Dispatch" button logs what *would*
   have gone out and plays it locally.

## Architecture (in this repo)

As planned. `sizeup/cad/` now has many more modules ([architecture.md](architecture.md) has the module map), the
store has more tables (radios, radio traffic, settings, operators, activity), and the dispatch voice is Kokoro or
Polly.

- `sizeup/cad/`: the new domain model.
  - `store.py`: SQLite (`state/cad.sqlite`, not under `captures/`, so the audio cleaner never touches
    it). Tables: units, incidents, assignments (unit × incident with status times), run_cards and
    run_card_slots, events (timeline), dispatches (text, audio file, transmitted?).
  - `runcards.py`: call type → recommended units, skipping unavailable ones.
  - `dispatcher.py`: builds the dispatch text, renders audio (tone generator + macOS `say` TTS, 16 kHz
    WAV), and hands it to a `Transmitter`.
  - `transmit.py`: `Transmitter` interface. `NullTransmitter` (logs; default),
    `AudioPttTransmitter` (sound card + PTT via serial RTS/DTR or CM108 GPIO; enabled later).
- Web: `/cad` page (dispatch console) next to the existing monitor page. JSON API under `/api/cad/...`
  (incidents, units, run cards, dispatch, preview audio). Same stdlib server, SSE push.
- Bridge from the monitor: a 60 Control page or Fire/EMS 15 dispatch for Port Chester creates a
  *pending* CAD incident pre-filled from the parsed page (type, address, units named).

## Milestones

Order and status now live in [ROADMAP.md](ROADMAP.md) and the GitHub issues. M1 and the status suggestions of M3 are
built; M2 is not (see the status table at the top).

- **M1 (built):**
  - CAD store; the sheet's lists and unit boards from `data/cad_lists.json`; default run cards
    keyed TYPE or TYPE/SUBTYPE (editable stand-ins, not policy).
  - `/cad` console laid out like the sheet's LIVE CAD tab: Event info (with received via),
    From 60 Control, Pending / Active / Non-emergency events, Working event (map, run-card
    recommendation, preview, dispatch, add-on and upgrade wording, full department response,
    disposition, close, chronology), Unit status and Other / mutual aid boards.
  - Incident database tab; CSV exports with the sheet's columns (Incident Database, Call History,
    Unit History, Event Log); admin counts and next event number.
  - Events waiting to be accepted from 60 Control pages for Port Chester.
- **M2 (not built, #41):** transmit through a real radio interface (after hardware and authorization); key-time
  limits; transmit log.
- **M3 (built, except reports):** radio-derived status suggestions (FG1 MDC IDs, Fire/EMS 15 talkers and phrases);
  incident close-out; reports.
- **M4+:**
  - pre-plans and hydrants (built in part: PR #227, #458, #464; #44 open)
  - mutual aid (the board exists; requests are not built)
  - station alerting (#45, waiting)
  - reports (CSV exports exist; #133)
  - **Live unit and member locations (AVL) and statuses:**
    - **IamResponding:** IAR already tracks responders ("Track Me if I Respond", Incident Map). Ask IAR/RapidSOS for agency API access in both directions: read responder status and locations into sizeup, and push sizeup's dispatched events into IAR through its CAD Incidents API so members get them in the IAR app.
    - **Red Alert (Alpine Software, RedNMX):** ask Alpine for API access to member/apparatus location, and a CAD link so closed events pre-fill Red Alert incident reports.
    - **An apparatus iPad web app** (ours, no vendor approval): shows the assigned event; GPS every few seconds while open; one-tap status (en route, on scene, available). Needs data access on the iPads and secure remote access to sizeup (as planned: a VPN; now: the hub behind Cloudflare Access). Background tracking would need a native app.
  - The request (2026-10-07) was to pull live locations from Red Alert or IAR, which already have them, or else to run
    an app on the iPads.
  - **The map side is ready (#358); no provider is built until the source is chosen.** `Cad.positions` is the single
    input (`sizeup/cad/mapview.py`). Whichever source we pick hands in one fix per unit in this shape
    (`mapview.ingest()` checks it):

    | field | | |
    |---|---|---|
    | `unit` | required | the unit's code (`E60`, `L31`, `2392`) |
    | `lat`, `lon` | required | WGS84 degrees |
    | `at` | required | epoch seconds when the fix was true (not when it arrived) |
    | `heading` | optional | degrees true, 0-360 |
    | `speed` | optional | metres per second over the ground |
    | `source` | required | a short name for where it came from (shown in the rig's tooltip) |

    A fix up to 60 s old (`POSITION_STALE_S`) is live: the rig is drawn there and moved on between fixes along its
    heading and speed (glide.js, as aircraft are). Up to 300 s (`POSITION_MAX_AGE_S`) it is stale: hollow, faded, its
    age on the chip. Older, the rig is placed by its status again. Follow and the rings work the same with or without
    a fix. `Cad._feed_position(fix)` feeds one in for tests and local dev only.
  - **The probable path (#360).** Each rig going to a call (dispatched or responding) gets a route by road from its fix,
    or from its status position (its station, or where it was returning), to the call's point, drawn dashed in its
    status colour from its mark; the follow chip adds "~3 min · 1.2 mi". The road graph is OpenStreetMap's roads for
    the firehouses plus 12 km (`sizeup/roadgraph.py`, Settings > Data sources "Road graph (routes)", monthly; cached
    and kept in S3 like the GIS layers); routing is A* in-process (`sizeup/cad/routes.py`). One-ways are honoured
    except a short wrong-way final stretch; closures in effect now are avoided. Rigs keep to local streets unless the
    call is on a highway: motorways (I-95, I-287, I-684) are a last resort, parkways (no commercial vehicles) are
    shut. A call whose address names a highway and whose point is on it (or, naming none, whose point is on one) is
    routed onto that highway's carriageway in its signed direction (NB / SB / EB / WB, else the nearest); a cross
    street naming the highway never does it. Re-routed when a
    fix is more than 50 m off the path, or after 3 fixes heading away. The recommendation's estimates use the route's
    time where there is one, else the straight-line model. The numbers are in Settings > Dispatch. No traffic, signals or turn costs.
  - **Estimated motion (#453).** A rig with no live fix is moved along its route by time, to the call and home after it
    is released, and is marked as estimated on the map. A live fix replaces the estimate (`sizeup/cad/routes.py`).

## Open questions for the department

All five are still open. They are tracked in the issues named.

1. **Run cards:** what does PCFD send per call type today? A starting set is seeded in M1 to edit. (#50)
2. **Paging on the PC repeater:** frequency and access tone; which alert tones members' pagers or radios respond to
   (department-wide vs per station/company). Do members carry pagers (e.g. Minitor) or radios with tone alert on that
   channel? (#41, #119)
3. **Transmit hardware:** which radio (model, accessory port) is available for a base or desk setup, and where will it
   sit? (#41)
4. **Who dispatches:** operators and sign-in (members at HQ? the duty officer?). Sign-in and the admin / dispatcher /
   viewer roles exist ([cloud.md](cloud.md)); who holds them is the department's decision (#52).
5. **Unit and station names** as members say them on the air: are these Engine 60 / Ladder 31 / Rescue 40 and the 239x
   chiefs, or are there others? (#50, #49)
