# sizeup: the first plan (2026-10-01)

Fire dispatch situational display for Port Chester FD (PCFD) firehouses. Sibling of shortfinal (ADS-B + KHPN ATC): the
same idea (receive the radio ourselves, transcribe it, put it on a map), applied to our own calls.

This is the original plan for the radio monitor, kept for history. The app has since become a CAD with the monitor
feeding it. Current direction: [ROADMAP.md](ROADMAP.md). How it is built now: [architecture.md](architecture.md) and
[cloud.md](cloud.md). The CAD's own plan: [cad-plan.md](cad-plan.md).

## Status as of 2026-11-14

Everything in the original scope is built. What differs from the plan below:

| Planned item | Status | Now |
|---|---|---|
| South 2 paging: tones, voice page, transcript | Done | `sizeup/radio/tones.py`, `sizeup/radio/page.py`, `sizeup/dispatch.py` (the page parser). Tone pairs are learned from the air and named in Settings > Radios > Tones. |
| Fire/EMS 15: dispatcher and units transcribed | Done | trunk-recorder as its own service on the station (`sizeup/radio/trunkrecd.py`, `sizeup/radio/p25.py`). |
| Map: zoom to the incident, show the type | Done | `sizeup/geo/` (offline gazetteer, page geocoding), MapLibre in `sizeup/web/static/cadmap.js`. |
| Fireground 1 transcribed while the call is on | Done | `sizeup/radio/fm.py`, MDC-1200 and FleetSync IDs (`mdc1200.py`, `fleetsync.py`). Only identified radios attach to an incident. FG2 is a channel too. |
| Speech-to-text: whisper.cpp only | Changed | Amazon Transcribe on the hub for the main channels, HPN ATC and PD dispatch; whisper.cpp on the station elsewhere and as the fallback ([cloud.md](cloud.md), [stt-vocabulary.md](stt-vocabulary.md)). The engine decision is open (#123). |
| One dongle for FG1 + FG3 | Changed | One dongle per FM channel (one `rtl_fm` each). Several frequencies on one dongle exist only for airband (`sizeup/radio/channelize.py`). FG3 and FG4 (EMS) are not received. More channels: #119. |
| Incident = a page; closes on "back in service" or a timeout | Changed | Incidents are CAD events. Ranked sources feed them: external CAD feeds first, paging as the backup (`sizeup/cad/sources.py`). A person closes an event with a disposition; in "60 Control dispatching" mode the feed's reports track it (`sizeup/cad/automode.py`). |
| Incident data: no external feed | Changed | External incident feeds are read through private adapters that run on the station only and are not in this repo (#297, #301). |
| IamResponding / text dispatch enrichment (F5) | Still planned | Waiting on the vendor and the department (#43). |
| Alerts (F5) | Done | Station health and alarm pushes to our own ntfy topics (`sizeup/notify.py`, `sizeup/cloud/health.py`). |
| Deploy: systemd, over-the-air updates (F5) | Done | The station follows the hub's build (`sizeup/cloud/autoupdate.py`, `deploy/station/`); the hub deploys on a merge to main. |
| A single box at the firehouse | Changed | Split into a station (the radios, at the South End firehouse) and a hub in the cloud ([cloud.md](cloud.md)). |
| SDRTrunk / OP25 as P25 fallbacks | Dropped | trunk-recorder decodes the county system; no fallback was needed. |
| Fine-tune whisper on corrected local clips | Still planned | The corrections side is built (admin corrections and an export, #487); the audio archive it needs is #428. |

Added since, outside this plan: the CAD ([cad-plan.md](cad-plan.md)), HPN ATC and airport pre-alerts, a second P25
system (MTA MRRS, context only), radio pre-alerts (`sizeup/intel/`), and the map layers (cameras, trains, aircraft,
vessels, weather, county GIS).

## What it does (the scope, 2026-10-01)

1. **Paging, South 2:** detect the PCFD and Rye Brook alert tones, capture and transcribe the voice page. Later: enrich
   with IamResponding (IAR) or text dispatch data.
2. **60 Control, Fire/EMS 15:** transcribe the dispatcher and the responding units.
3. **Map:** zoom to the incident location and show the incident type.
4. **Fireground 1:** transcribe PCFD / Rye Brook fireground traffic while the call is on.

## Radio facts (research 2026-10-01; re-check yearly, the county is migrating)

| What | Where | Mode | Source |
|---|---|---|---|
| **South 2 paging** (60 Control fire paging, Battalions 14/18 + 15/19 incl. Port Chester, Rye Brook, Rye) | **470.2000 MHz**, simulcast from the South Zone sites (one transmitter in Port Chester) | analog NFM, CTCSS 156.7, two-tone sequential alert then voice | RadioReference forum (60 Control radio staff, 2022), FAB minutes May 2023, FCC DA 23-96 (Feb 2023) |
| Future "South 1" | possibly 470.2125 | — | FCC DA 23-96 (inference) |
| Legacy low band "60-Control F1" | 46.26 MHz | NFM, being phased out | FAB minutes 2023 |
| **Fire/EMS 15** | Westchester County **P25 Phase II** trunked, system A66 / WACN BEE00, South simulcast site 002, NAC A62; **TG 307** (60 Control dispatch TG 301) | P25 Phase II TDMA, **clear** (only hospital/"Private" TGs encrypted) | RadioReference db sid 11717 (Apr 2026); Fire/EMS 15 = Port Chester FD, Rye Brook FD, Rye FD, PC/Rye/RB EMS, Larchmont, Mamaronecks |
| South site control channels | 476.075, 476.1125, 476.2125, 476.2375 MHz | | |
| South site voice | 478.1875, 478.3125 MHz | | |
| **Fireground 1** (PCFD & Rye Brook, dept #239 primary fire) | **453.0375 MHz** | analog NFM simplex, CTCSS 100.0, clear | County fireground channel list 2024-11-06 |
| Fireground 2 (secondary fire) | 458.0375 MHz | same | same |
| Fireground 3 / 4 (EMS) | 453.8875 / 458.8875 MHz | same | same |

**Tone codes:** learned from the air (#47; 60 Control tone tests 10-20 and 10-23-2026, 18 days of
stored South 2 pages): Port Chester Fire 903.2/810.2, Rye Brook Fire 767.4/928.1, often stacked on
one page; both are ours. Port Chester–Rye–Rye Brook EMS is 928.1/510.5 (not ours). The
RadioReference wiki's Port Chester pair 349.1/311.1 was never heard. sizeup logs *every* decoded
tone pair with its voice page, so other codes are learned the same way and named in Settings >
Radios > Tones.

**Incident data (as planned):** no public CAD, RSS or PulsePoint feed exists. A feed that needs an agency login was out
of scope unless the department got access and agreed. **Now:** such feeds are read only through private adapters on
the station, and nothing about them is in this repo (#297, #301). IAR integration is
still to ask the department about (#43).

## Hardware

All the fire channels are UHF 450–480 MHz: one UHF antenna (or the roof scanner antenna) through a splitter/LNA feeds
every fire dongle. RTL-SDR V4, one per band. This is the plan's table; the notes column says what changed.

| Dongle | Tuned | Receives | Now |
|---|---|---|---|
| fire-page | 470.2 MHz | South 2 paging (+ 470.2125 if South 1 appears) | As planned |
| fire-p25 | ~477.2 MHz, 2.4–2.56 MS/s | P25 South site: control channel + both voice channels (span 2.24 MHz, at the edge of one dongle; a second dongle is the fallback) | One dongle, run by trunk-recorder |
| fire-fg | ~453.46 MHz | Fireground 1 453.0375 + FG3 453.8875 (FG2/FG4 at 458 need another dongle) | Tuned to FG1 only (453.0375); FG3 is not received |
| fire-fg2 | 458.0375 MHz | Fireground 2 (#119): received like FG1; a 4th dongle, or picked by hand on one of the three | As planned (PR #178) |
| hpn-twr | 118.575 MHz AM | HPN Tower (#233): ATC for the airport pre-alerts; a 5th dongle, picked by hand. Ground 121.825 is 3.25 MHz away: another dongle | As planned; NY Approach and HPN Ground are channels too (#260, #275) |

Not in the plan: a dongle for our own ADS-B receiver (#235), one for AIS ([vessels.md](vessels.md), not installed yet)
and up to two for the MRRS system (#408). Dongles are named by USB serial and given a job in Settings > Radios, or by
`[radio] auto_assign_order`. shortfinal shares dongles with sizeup through claim files
([handoff-shortfinal-dongles.md](handoff-shortfinal-dongles.md)).

The plan's note on running both apps on one box: shortfinal's three dongles as well makes seven, at about 5 MB/s of USB
each, so a Pi 5's two USB3 controllers or a mini PC, not one USB2 hub.

## Software

```
470.2 NFM ──► FM demod + noise squelch ──► tone decoder (sizeup/radio/tones.py)
                                         └► voice page capture ──► whisper ──► page parser
                                                                     (type, address, cross st, units)
P25 South ──► trunk-recorder (per-call WAV + JSON: TG, unit IDs) ──► whisper ──► call log
453.0375  ──► FM demod + squelch ──► transmissions ──► whisper ──► fireground log (active incident)
                       │
        incident = page (+ IAR later) ─► geocode (offline address points) ─► map zoom + type
```

As planned (the status table at the top says what changed):

- **STT:** whisper.cpp server as in shortfinal. The ATC fine-tune is wrong for fire radio; start with a general English
  model + an area prompt (streets, units, incident types) and measure. Fine-tune later on corrected local clips.
- **P25:** trunk-recorder (C++, Pi-friendly, writes a WAV + JSON per call with talkgroup and radio IDs). Simulcast (LSM)
  can be hard for RTL-SDR decoders; SDRTrunk or OP25 are the fallbacks if trunk-recorder decodes poorly. Record TG 307
  (+ 301). Now: every unencrypted talkgroup is recorded unless its Record switch is off (Settings > Radios); TG 307 and
  333 (Airport Incident) attach to our incidents (`[p25] incident_talkgroups`).
- **Incident model:** a page opens an incident (type, address, units, time out); Fire/EMS 15 and Fireground 1 traffic
  attaches to the open incident while it is active; it closes on "back in service"/timeout.
- **Map:** MapLibre web app like shortfinal, home view = Port Chester / Rye Brook; an incident flies the map to the
  geocoded address with a type badge.
- **Rules carried over from shortfinal:** nothing fake on screen; self-healing services; alerts never to the Tapout
  crew's ntfy topics.

## Milestones

The first monitor's milestones (2026-10-01), kept for history. The app is now the CAD + monitor. Current plan:
[ROADMAP.md](ROADMAP.md) and the GitHub issues.

| | Milestone | Status |
|---|---|---|
| F0 | Scaffold + two-tone decoder (tests) | Done |
| F1 | Paging chain on the bench: 470.2 NFM receiver, squelch, tones → page capture → STT; log every tone pair (learn the PCFD / Rye Brook codes from the air) | Done (codes learned, #47) |
| F2 | Page parser + offline geocoder + web map with incidents | Done |
| F3 | P25: trunk-recorder for TG 307/301, ingest calls → STT → incident timeline | Done |
| F4 | Fireground 1 receiver, attached to the active incident | Done |
| F5 | IAR / text dispatch enrichment, alerts, deploy (systemd, OTA like Tapout) | Alerts and deploy done; external incident feeds done another way (#297); IAR still planned (#43) |
