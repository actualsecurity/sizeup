# Vessels (AIS)

Live ships and boats on the CAD maps (GitHub #402): a **Vessels** layer in every map's Layers box, built like
Aircraft and Trains. Code: `sizeup/vessels.py` (the hub), `sizeup/web/static/vessels.js` (the page),
`sizeup/radio/aisd.py` + `sizeup/cloud/vessellink.py` (our own receiver on the station).

## What it shows

- **Area:** one box, `[vessels] bbox` (west, south, east, north). The default is `[-73.83, 40.78, -73.50, 41.07]`:
  western Long Island Sound from Throgs Neck (where the East River and NY Harbor meet the Sound) east to Stamford,
  and from the Westchester / Connecticut shore across to Long Island's north shore (Manhasset Bay, Hempstead Harbor,
  Oyster Bay). aisstream.io is asked for this box, and any vessel reported outside it is dropped, whatever the source.
- **Each vessel:** a hull turned to its heading (its course if it reports no heading), shaped and coloured by type:
  ship (cargo, tanker, passenger), tug, small craft (pleasure, fishing, other) and sail. Law enforcement and SAR are
  drawn in red. A vessel with no heading and no course is a dot. Its name, or its MMSI until a name arrives, shows
  with type and speed from zoom 12.
- **Age:** a position over **3 min** old is hollow and fades; after **10 min** it is gone. A vessel's name, type and
  size are kept by MMSI for 24 h, so one seen again is named at once.
- **Motion:** between reports a moving vessel (≥ 0.5 kn with a course) is carried on along its course at its speed.
  Past 30 s it moves at half rate, and it stops 90 s after the report. A new report is eased into over 4 s with no
  jump forward.
- **Click:** follows the vessel (a drag, Recenter or ✕ lets go; one follower at a time across aircraft, trains and
  units). It also opens a live box with name, type, length, speed, course, status, destination, MMSI / callsign
  and the report's age.
- **Marine calls:** a call whose type and subtype say so (any `*/BOAT`, such as `MVF/BOAT` or `SERVICE/BOAT`;
  `RESCUE/WATER`; `AIRPORT/ACFT-WATER`), or one with a marine unit on it, turns the layer on for that call's map.
  `NOTIF/WATER` (a water-department notification) is not a marine call. Unticking the layer there holds for that call.
  A DPT placeholder unit never counts and is never located. The hub (`vessels.is_marine`, `app.MARINE_OPEN_SQL`) and
  the page (`vessels.js isMarine`) use the same rule. Under the map, **Vessels nearby** lists any vessel the call
  names first, ringed on the maps, then the 3 nearest within 10 nm with type, distance, speed and course. A vessel is
  named by its name or 9-digit MMSI in the remarks, notes or log text, never in the location words: a vessel called
  CITY ISLAND is not named by "City Island Ave". Click a row to follow that vessel.

## The limits of AIS

- **Small craft often carry none.** Kayaks, paddleboards, jet skis, dinghies and most small recreational boats have
  no AIS transponder. Many that do (class B) transmit less often, and some only while under way. An empty patch of
  water on the map does not mean an empty patch of water.
- Class B reports have no navigational status. Long-range (type 27) positions are coarse (about 200 m) and never
  replace a precise one younger than 5 min.
- Names and types are what the vessel broadcasts, unchecked.

## Sources

Pluggable, in this order of preference. For each vessel, the newest position wins whichever source it came from.

### 1. Our own receiver (AIS-catcher at the station): off until the hardware is in

`sizeup-ais.service` runs `aisd.py`, which runs [AIS-catcher](https://github.com/jvde-github/AIS-catcher) on one
RTL-SDR named for AIS, with JSON on stdout (`-o 5`). It keeps each vessel's latest messages in
`/run/sizeup-ais/messages.json`, and sizeup-station posts the vessels to the hub (`POST /api/station/vessels`). The
dongle is claimed through the shared rtl-claims (app `sizeup`, mode `ais`). It is never opened while another app
(shortfinal) holds it, and it is never one of sizeup's radios. Hardware and steps:
`deploy/station/README.md` → **AIS receiver**. In short:

1. Hardware: an RTL-SDR (ideally a TCXO RTL-SDR Blog V3/V4), a marine VHF antenna tuned ~162 MHz (or a broadband
   discone) mounted high, short low-loss coax, and optionally a 162 MHz bandpass filter and/or LNA.
2. Name the dongle (`sizeup-station sdr-serial <index> ais162`) and install AIS-catcher.
3. On a station already running this release, add to `/etc/sizeup/config.toml`:
   ```toml
   [vessels.receiver]
   enabled = true
   sdr = "ais162"
   ```
   then `sudo systemctl restart sizeup-ais sizeup-station`.
4. Once it runs, feed [AISHub](https://www.aishub.net/): feeding it is what earns AISHub's free API, the third source
   (below).

### 2. aisstream.io (free websocket)

One websocket to `wss://stream.aisstream.io/v0/stream`, subscribed to the box. It is open only while someone has
the layer on (and 15 min after, since names come in slowly) or while a marine call is open. When it fails it backs
off from 5 s, doubling to 5 min, and waits 30 min after a refused key. A server close right after the subscription,
with nothing sent, counts as a refused key; any other close with nothing received counts as a failure; and every
connection waits at least 5 s after the last one. When the box is quiet it reconnects every 10 min. Only the hub's
lease holder connects: a standby task never does (hot/cold deploys). It identifies as *Port Chester Fire Department - sizeup emergency situational awareness application
(github.com/actualsecurity/sizeup)*.

**The key** is read from env `SIZEUP_AISSTREAM_KEY`, or `[vessels] aisstream_key` in a local, uncommitted config.
**With no key, this source is off.** The key is never logged or shown; the API only says `"key": "set"` or `"missing"`.

- Sign up (free, with a GitHub login) at <https://aisstream.io/> → API keys → create a key.
- The hub: SSM SecureString `/sizeup-hub/aisstream-key` (`aws ssm put-parameter --name /sizeup-hub/aisstream-key
  --type SecureString --value '<the key>'`). `deploy/aws/taskdef.json` passes it as `SIZEUP_AISSTREAM_KEY`. To turn
  the source off, drop that line and redeploy.
- Locally: `SIZEUP_AISSTREAM_KEY=$(security find-generic-password -a sizeup -s sizeup-aisstream-key -w) python -m
  sizeup --config …` (never paste it into a file in the repo).

### 3. AISHub: a hook only

AISHub shares its data API with stations that feed it. Once our receiver feeds it, a feed class beside
`AisStreamFeed` can poll it. It is not implemented yet.

**Never MarineTraffic**: its API is paid, and its terms forbid scraping.

## Config

```toml
[vessels]                          # leave out: on, but with no source it shows nothing
enabled = true
bbox = [-73.83, 40.78, -73.50, 41.07]   # west, south, east, north (at most 2° a side)
# aisstream_key: env SIZEUP_AISSTREAM_KEY instead (the hub: SSM /sizeup-hub/aisstream-key)

[vessels.receiver]                 # the station's own receiver (deploy/station/README.md "AIS receiver")
enabled = false
sdr = ""                           # its exact USB serial, e.g. "ais162" (not all digits)
gain = "auto"                      # or dB
ppm = 0                            # a non-TCXO dongle's frequency correction
# json = ""                        # all-in-one: the decoder's messages file ("" = /run/sizeup-ais/messages.json)
```

## API

| | |
|---|---|
| `GET /api/cad/vessels` | `{configured, note, at, stale_s, drop_s, bbox, feeds: [{key, label, state, error}], credit, key: set\|missing, vessels: [{id (MMSI), name, cs, imo, type, cls, label, len, beam, lat, lon, sog, cog, hdg, navc, nav, dest, age, sage, c: A\|B\|LR, src}]}`. `at` is when what the hub knows last changed, and ages are as at `at`. The page adds the time since `at` by the hub's clock, so an unchanged answer keeps its ETag and comes back as a 304. Polled every 5 s only while a map has the layer on. |
| `GET /api/cad/vessels/near?event=<id>` | `{id, marine, configured, note, named: [vessel], nearest: [vessel + dist (nm)]}` for that call. Cached 10 s. |
| `POST /api/station/vessels` | the station's receiver (station token): `{v, at, vessels: [vessel], rx}`; the answer's `every` sets how often to post (5 s while viewed, else 30 s). |

## Cost

The hub keeps one dict per vessel (a few hundred at most in the box) and parses each websocket message once (a few
a second in the box). Nothing runs while nobody looks and no marine call is open: the socket closes. The page polls
every 5 s and redraws only while a vessel moves, at most 10 times a second and usually about once a second.

## Tests

`tests/test_vessels.py` (decoding every message type from both formats, merging, stale and drop, the box, no key →
off, the session and backoff, the handshake, incident matching and ranking), `tests/test_vessels_page.py` (node:
the layer's default, following, the live box, dead-reckoning, the nearby panel and the named ring) and
`tests/test_vessel_receiver.py` (the station's decoder, forwarder and the hub's ingest). They use synthetic MMSIs
(9990000xx) and invented names only. No captured AIS is committed.
