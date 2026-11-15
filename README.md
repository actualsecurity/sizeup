# sizeup

A self-dispatch CAD and radio monitor for Port Chester FD (PCFD). It hears the radio, transcribes it and keeps the
CAD up to date with the call. It supplements county dispatch (60 Control); it does not replace it.

It runs in two places from one codebase (`python -m sizeup --config <file>`, `[cloud] role`):

- **Hub:** the CAD of record and the web UI, on AWS ECS Fargate behind Cloudflare Access. No radios.
- **Station:** a PC with RTL-SDR dongles at a firehouse. It receives, decodes and transcribes, and feeds the hub over
  outbound HTTPS. It has no CAD.

With no `[cloud]` section it runs all-in-one, for local development.

## What it does

| area | what |
|---|---|
| Paging | Decodes two-tone pages on South 2, names the departments paged and transcribes the voice page |
| Fire radio | County P25 (Fire/EMS 15, 60 Control) through trunk-recorder; analog fireground with MDC-1200 and FleetSync unit IDs |
| Other radio | A second P25 system (MTA MRRS: local police dispatch, Metro-North) as context, by talkgroup tier; HPN ATC on AM airband |
| Speech-to-text | whisper.cpp on the station and Amazon Transcribe from the hub, chosen per channel, with monthly budgets, fallback and catch-up |
| CAD | Events, units and status boards, run cards and recommendations, dispatch text and voice, dispatch modes, CSV export |
| Incident sources | External CAD feeds first, paging as the backup; reports matched to one incident; pre-alerts never create an incident |
| Radio log | Every transmission with its rig, radio ID, channel and events; views per service; re-run a transcript |
| Alarms | The radio emergency button (P25, MDC, FleetSync) and mayday keywords, acknowledged at the console and pushed |
| Pre-alerts | The headquarters phone ringing (read only; its topics are set in Settings), airport alerts from ATC and ADS-B, radio-intelligence rule packs with an optional LLM stage behind them (off by default, capped) and a Listen link on the push |
| Maps | Rigs and their probable routes, aircraft near HPN, Metro-North trains, vessels (AIS), highway cameras and DVR, road closures, county GIS, weather |
| Buildings | Address history, premise notes, pre-plan points and site documents (floor plans on the map), incident attachments |
| Search | Full-text search over incidents, the chronology and the radio log |
| Notifications | ntfy pushes in three kinds: alerts, system status, radio intel. Topics that sizeup reads are protected: never published to |
| Operations | Litestream replication to S3, hot/cold deploys behind a single-writer lock, replica checks, station auto-update and health |

Dispatch audio plays at the console. Nothing transmits on the air.

## Architecture

```
 station (firehouse PC)                              hub (ECS Fargate)
 RTL-SDRs -> rtl_fm / trunk-recorder / readsb        CAD of record (SQLite) + web UI
   -> tones, MDC, squelch -> clips -> whisper   ==>  Amazon Transcribe, Polly, map data, ntfy
   -> outbox.sqlite (durable, in order)       HTTPS  Litestream -> S3; leader lock; replica watch
 private incident-source adapter              <==    jobs (long poll); the build to follow
                                                     people: Cloudflare Access + Tunnel
```

The station only connects out. The hub has no inbound port. Full analysis, data flow, data model, security boundaries
and the module map: [`docs/architecture.md`](docs/architecture.md).

## Run it locally

```sh
python3.14 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -n auto -q                   # the suite, ~3 min
sed 's/^auto_assign = true/auto_assign = false/' config.example.toml > config.toml   # all-in-one, no dongle opened
.venv/bin/python -m sizeup --config config.toml         # http://localhost:8090/cad
.venv/bin/python tools/dev_seed.py                      # a few invented calls on it
```

Python 3.11 or newer; CI runs 3.14. The only runtime dependency is numpy (the `docs` extra adds PDF and image handling
for site documents). The radios need `rtl_fm`, trunk-recorder and a whisper.cpp server.
[`config.example.toml`](config.example.toml) lists every option. The developer guide,
[`docs/development.md`](docs/development.md), covers the hub and station locally, the optional test tools (Playwright,
Litestream, node), known flakes and the PR checklist.

## Documentation

| doc | what |
|---|---|
| [`docs/development.md`](docs/development.md) | Running it locally without radios or project access, the tests, known flakes, the PR checklist |
| [`docs/architecture.md`](docs/architecture.md) | The system: components, hub and station, pipelines, data model, security, costs, module map |
| [`docs/cloud.md`](docs/cloud.md) | Hub and station in operation: flows, health, auth, STT, incident sources, every map layer's source and terms, storage, deploys and the handoff, replica checks |
| [`deploy/station/README.md`](deploy/station/README.md) | Installing and running a station (Windows + WSL2), its services and commands |
| [`deploy/trunk-recorder/README.md`](deploy/trunk-recorder/README.md) | trunk-recorder configuration for P25 |
| [`docs/reception-test.md`](docs/reception-test.md) | The reception test: can this station hear a P25 system with a dongle it has |
| [`docs/station-logs.md`](docs/station-logs.md) | The station's journal, scrubbed, to CloudWatch through the hub |
| [`docs/stt-vocabulary.md`](docs/stt-vocabulary.md) | The Amazon Transcribe custom vocabulary and its measurements |
| [`docs/vessels.md`](docs/vessels.md) | The Vessels layer: AIS sources, setup, limits |
| [`docs/handoff-shortfinal-dongles.md`](docs/handoff-shortfinal-dongles.md) | The dongle claim files shared with shortfinal (a sibling ADS-B and ATC display) |
| [`docs/ROADMAP.md`](docs/ROADMAP.md) | Direction and order. Day-to-day work is in GitHub issues (`now` / `next` / `later` / `waiting`). |
| [`docs/cad-plan.md`](docs/cad-plan.md) | The CAD's design plan, including transmit hardware and rules |
| [`docs/PLAN.md`](docs/PLAN.md) | The original plan (background) |
| [`CLAUDE.md`](CLAUDE.md) | Rules for agents and contributors: tests, definition of done, public-repo rules, git |
| [`config.example.toml`](config.example.toml) | Every config option, commented |

## Data sources and licences

Each layer's terms are also in `docs/cloud.md` under its heading and in its provider's module docstring.

### In the repo

`data/gazetteer.sqlite` (offline geocoder, rebuilt with `tools/fetch_addresses.py`; sources,
licences and fetch dates are in its `meta` table):

- NYS Address Points, Westchester: NYS ITS GIS, CC0
- Connecticut 9-1-1 address points (Greenwich), through the USDOT National Address Database: public domain
- TIGER/Line 2025 municipal boundaries, ADDRFEAT + roads: US Census Bureau, public domain
- Westchester County community facilities: Westchester County GIS (no licence stated; attributed)
- Landmarks © OpenStreetMap contributors, ODbL
- Firehouses (`data/firehouses.json`): addresses from NERIS and ryebrookny.gov, coordinates from the NYS Geocoder
- The `places` table (`tools/fetch_places.py`: what a note's "@" and the location box suggest):
  NYSDOT reference markers (the posted panels on I-287 and I-684; tenth-mile milepoints on the Hutch, US-1,
  NY-120 and NY-120A, the Hutch's offset to 60 Control's own mileage, #83): NYSDOT GIS;
  New England Thruway (I-95) mileposts: NYS Thruway Authority GIS; exits and named places
  © OpenStreetMap contributors, ODbL (each row keeps its source)
- Westchester County Airport (HPN, `tools/fetch_airport.py`, #87): `data/khpn_airport.geojson` (the Airport map
  layer) and the airport's `places` rows (terminal, FBOs, hangars, gates, runway ends, taxiways, the Command
  Post / EOC staging area, the entrances), from `data/airport/`: OSM aeroway features © OpenStreetMap
  contributors, ODbL (`khpn_surface.geojson`); FAA CIFP cycle 261001 approaches and Chart Supplement runway
  ends, public domain (`khpn_procedures.geojson`); NYS address points (CC0) and TIGER intersections (public
  domain) already in the gazetteer

Other data files:

- `data/westchester_departments.csv`, `data/westchester_apparatus.csv`: transcribed from the Westchester County Fire
  Mutual Aid Plan (WCDES, 2021)
- `data/westchester_tones.csv`: two-tone paging codes from the RadioReference wiki's Westchester tone-out list,
  plus tones learned on the air
- `data/mnr_tracks.json` (`tools/fetch_train_tracks.py`): New Haven Line tracks © OpenStreetMap contributors, ODbL
- `data/mnr_reporting_points.json`: learned from the MTA's Metro-North GTFS-realtime positions
- `data/airport/airlines.csv`: airlines from FAA JO 7340.2, public domain
- `sizeup/web/static/vendor/`: MapLibre GL JS, its licence beside it

### Fetched at run time

Read by the hub (or all-in-one), cached and served to the pages.

| layer | source | terms |
|---|---|---|
| Map tiles | OpenFreeMap / OpenMapTiles | © OpenStreetMap contributors |
| Road graph (rig routes) | OpenStreetMap through the Overpass API | ODbL |
| Aircraft near HPN (#155) | adsb.lol and adsb.fi; the station's own ADS-B receiver when one is named | adsb.lol: ODbL. adsb.fi: personal, non-commercial use, credited |
| Aircraft types | FAA Releasable Aircraft Database | Public domain |
| Trains | MTA Metro-North GTFS and GTFS-realtime | The MTA's developer terms: free, credited, our estimates labelled as ours |
| Trains: GPS, consist, track | The MTA TrainTime backend | Undocumented; GTFS-realtime stays the fallback |
| Tracks, catenary poles, mileposts | Metro-North GIS, a public ArcGIS service of the MTA | No licence stated; credited |
| Vessels | aisstream.io (a free key); the station's own AIS receiver when installed | `docs/vessels.md` |
| Highways: cameras, events, signs | NYS Thruway Authority public data; 511NY (NYSDOT; an account) | Thruway: credited, no resale. 511NY: polled at most every 5 minutes |
| Road projects and closures | Thruway and NYSDOT capital programs, CTDOT work areas, village RSS notices, WZDx | Public feeds, credited, each item linked to its source |
| County GIS layers | Westchester County GIS (parcels with NYS ITS) | Attribution; each dataset's terms on its entry |
| Weather, radar, tides | NOAA / National Weather Service, NOAA CO-OPS | Public domain |

Services and programs it runs on: Amazon Transcribe and Polly, Cloudflare Tunnel and Access, ntfy,
[Litestream](https://litestream.io), [trunk-recorder](https://github.com/TrunkRecorder/trunk-recorder),
[whisper.cpp](https://github.com/ggml-org/whisper.cpp), [readsb](https://github.com/wiedehopf/readsb) and
[AIS-catcher](https://github.com/jvde-github/AIS-catcher).

## Replay fixtures

Replay tests (#283, `tests/test_replay_*.py`) replay real captured data: ADS-B around HPN, the CARS-Hub's camera
streams, airband clips. That data is private and never in git. It lives in a dedicated private S3 bucket (the default
in `tools/replay_sync.py`, or `$SIZEUP_REPLAY_S3`) and in a local cache outside the repo (`~/.cache/sizeup-replay`, or
`$SIZEUP_REPLAY_DIR`). Without it the replay tests skip and say why.

```sh
.venv/bin/python tools/replay_sync.py pull                        # read-only download into the cache
.venv/bin/python tools/capture_fixture.py adsb|camera|audio       # capture a new set (tests/replay.py)
.venv/bin/python tools/replay_sync.py push --write-to-s3          # share it (refuses without the flag)
```
