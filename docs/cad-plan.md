# sizeup CAD: self-dispatch for Port Chester FD

Started 2026-10-07 on branch `cad`. sizeup grows from "listen to 60 Control" into a small
computer-aided dispatch system that PCFD runs for itself. The radio recording, transcription, and
map stay as features that feed it.

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
     until closed, with no dispatch.
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

- **M1 (started):**
  - CAD store; the sheet's lists and unit boards from `data/cad_lists.json`; default run cards
    keyed TYPE or TYPE/SUBTYPE (editable stand-ins, not policy).
  - `/cad` console laid out like the sheet's LIVE CAD tab: Event info (with received via),
    From 60 Control, Pending / Active / Non-emergency events, Working event (map, run-card
    recommendation, preview, dispatch, add-on and upgrade wording, full department response,
    disposition, close, chronology), Unit status and Other / mutual aid boards.
  - Incident database tab; CSV exports with the sheet's columns (Incident Database, Call History,
    Unit History, Event Log); admin counts and next event number.
  - Events waiting to be accepted from 60 Control pages for Port Chester.
- **M2:** transmit through a real radio interface (after hardware and authorization); key-time limits;
  transmit log.
- **M3:** radio-derived status suggestions (FG1 MDC IDs, Fire/EMS 15 talkers and phrases); incident
  close-out; reports.
- **M4+:**
  - pre-plans and hydrants
  - mutual aid
  - station alerting
  - reports
  - **Live unit and member locations (AVL) and statuses:**
    - **IamResponding:** IAR already tracks responders ("Track Me if I Respond", Incident Map). Ask IAR/RapidSOS for agency API access in both directions: read responder status and locations into sizeup, and push sizeup's dispatched events into IAR through its CAD Incidents API so members get them in the IAR app.
    - **Red Alert (Alpine Software, RedNMX):** ask Alpine for API access to member/apparatus location, and a CAD link so closed events pre-fill Red Alert incident reports.
    - **An apparatus iPad web app** (ours, no vendor approval): shows the assigned event; GPS every few seconds while open; one-tap status (en route, on scene, available). Needs data access on the iPads and secure remote access to sizeup (VPN such as Tailscale, not exposed to the internet). Background tracking would need a native app.
  - the user's request (2026-10-07): "could we also pull from RedAlert... to pull location information live of where they are? or maybe we could run an app on the ipads for that... would be nice to use IAR or RedAlert though that already has that info"

## Open questions for the department

1. **Run cards:** what does PCFD send per call type today? A starting set is seeded in M1 to edit.
2. **Paging on the PC repeater:** frequency and access tone; which alert tones members' pagers or
   radios respond to (department-wide vs per station/company). Do members carry pagers (e.g. Minitor)
   or radios with tone alert on that channel?
3. **Transmit hardware:** which radio (model, accessory port) is available for a base or desk setup,
   and where will it sit?
4. **Who dispatches:** operators and sign-in (members at HQ? the duty officer?).
5. **Unit and station names** as members say them on the air: are these Engine 60 / Ladder 31 /
   Rescue 40 and the 239x chiefs, or are there others?
