# sizeup

Fire dispatch situational display for Port Chester FD firehouses: the sibling of
[shortfinal](../shortfinal) (ADS-B + KHPN ATC), applied to our own calls.

- **Paging (South 2):** decode the two-tone alert (PCFD, Rye Brook first), transcribe the voice page.
- **60 Control Fire/EMS 15:** transcribe dispatcher and responding-unit traffic.
- **Fireground 1:** transcribe PCFD / Rye Brook fireground traffic while the call is ongoing.
- **Map:** zoom to the incident location and show the incident type.

Where it's going: `docs/ROADMAP.md` (work is tracked in GitHub issues). Background: `docs/PLAN.md`. To run the CAD in the cloud (a hub fed by this machine as the station): `docs/cloud.md`.
- **Phone pre-alert:** Tapout's ntfy feed (the HQ phone ringing), read only, at the top of the CAD's
  60 Control column as a possible incoming call (`[prealert]`, `docs/cloud.md` "Phone pre-alert").

## Data

`data/gazetteer.sqlite` (offline geocoder, rebuilt with `tools/fetch_addresses.py`; sources,
licences and fetch dates are in its `meta` table):

- NYS Address Points, Westchester — NYS ITS GIS, CC0
- TIGER/Line 2025 ADDRFEAT + roads — US Census Bureau, public domain
- Westchester County community facilities — Westchester County GIS
- Landmarks © OpenStreetMap contributors, ODbL
- The `places` table (`tools/fetch_places.py`: what a note's "@" and the location box suggest):
  NYSDOT reference markers (the posted panels on I-287 and I-684; tenth-mile milepoints on the Hutch, US-1,
  NY-120 and NY-120A, the Hutch's offset to 60 Control's own mileage, #83) — NYSDOT GIS;
  New England Thruway (I-95) mileposts — NYS Thruway Authority GIS; exits and named places
  © OpenStreetMap contributors, ODbL (each row keeps its source)

Map tiles: OpenFreeMap / OpenMapTiles, © OpenStreetMap contributors.

```sh
/opt/homebrew/bin/python3.14 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest
```
