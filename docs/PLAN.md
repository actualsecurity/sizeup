# sizeup — plan

Fire dispatch situational display for Port Chester FD (PCFD) firehouses. Sibling of
shortfinal (ADS-B + KHPN ATC); same idea — receive the radio ourselves, transcribe it, put
it on a map — applied to our own calls.

## What it does (user's scope, 2026-10-01)

1. **Paging, South 2:** detect the PCFD and Rye Brook alert tones, capture and transcribe the
   voice page. Later: enrich with IamResponding (IAR) or text dispatch data.
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

**Tone codes:** only hobbyist-sourced so far — Port Chester A 349.1 Hz / B 311.1 Hz
(RadioReference wiki, edited 2025-07); Rye Brook not listed. Treat as unverified: sizeup
logs *every* decoded tone pair with its voice page, so the real codes are learned from the
air (a member confirms which page was which) and then named in the config.

**Incident data:** no public CAD/RSS/PulsePoint. 60 Control's "CAD Times" needs an agency
login and its guide forbids redistribution, so it is out of scope unless the department gets
access and agrees. IAR integration: ask the department (user will provide).

## Hardware

All the fire channels are UHF 450–480 MHz: one UHF antenna (or the roof scanner antenna)
through a splitter/LNA feeds every fire dongle. RTL-SDR V4, one per band:

| Dongle | Tuned | Receives |
|---|---|---|
| fire-page | 470.2 MHz | South 2 paging (+ 470.2125 if South 1 appears) |
| fire-p25 | ~477.2 MHz, 2.4–2.56 MS/s | P25 South site: control channel + both voice channels (span 2.24 MHz, at the edge of one dongle; a second dongle is the fallback) |
| fire-fg | ~453.46 MHz | Fireground 1 453.0375 + FG3 453.8875 (FG2/FG4 at 458 need another dongle) |

Plus shortfinal's three if both run on one firehouse box (7 dongles: USB bandwidth ~5 MB/s
each — a Pi 5's two USB3 controllers or a mini PC, not one USB2 hub).

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

- **STT:** whisper.cpp server as in shortfinal. The ATC fine-tune is wrong for fire radio;
  start with a general English model + an area prompt (streets, units, incident types) and
  measure. Fine-tune later on corrected local clips.
- **P25:** trunk-recorder (C++, Pi-friendly, writes a WAV + JSON per call with talkgroup and
  radio IDs). Simulcast (LSM) can be hard for RTL-SDR decoders; SDRTrunk or OP25 are the
  fallbacks if trunk-recorder decodes poorly. Record TG 307 (+ 301).
- **Incident model:** a page opens an incident (type, address, units, time out); Fire/EMS 15
  and Fireground 1 traffic attaches to the open incident while it is active; it closes on
  "back in service"/timeout.
- **Map:** MapLibre web app like shortfinal, home view = Port Chester / Rye Brook; an incident
  flies the map to the geocoded address with a type badge.
- **Rules carried over from shortfinal:** nothing fake on screen; self-healing services;
  alerts never to the Tapout crew ntfy topic.

## Milestones

The first monitor's milestones (2026-10-01), kept for history. F0–F4 are built; the app is now the
CAD + monitor. Current plan: [ROADMAP.md](ROADMAP.md) and the GitHub issues.

- **F0** ✔ scaffold + two-tone decoder (tests).
- **F1** paging chain on the bench: 470.2 NFM receiver, squelch, tones → page capture → STT;
  log every tone pair (learn PCFD / Rye Brook codes from the air).
- **F2** page parser + offline geocoder + web map with incidents.
- **F3** P25: trunk-recorder for TG 307/301, ingest calls → STT → incident timeline.
- **F4** Fireground 1 receiver, attached to the active incident.
- **F5** IAR / text dispatch enrichment, alerts, deploy (systemd, OTA like Tapout).
