"""Station config (TOML). Unknown keys are rejected so typos don't silently do nothing.
See config.example.toml for every option with comments."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from sizeup.cloud.access import team_host
from sizeup.polly import DEFAULT_VOICE as POLLY_DEFAULT
from sizeup.polly import VOICES as POLLY_VOICES
from sizeup.radio.tones import ToneCode
from sizeup.tts import DEFAULT_PYTHON, KOKORO_VOICES


class ConfigError(ValueError):
    pass


# Department names used across sizeup for the two home departments (tone codes, incidents).
HOME_DEPARTMENTS = {"Port Chester": "Port Chester FD", "Rye Brook": "Rye Brook FD"}
STATION_LL = (40.99600, -73.66453)   # lat, lon: the station PC is at the South End firehouse, 51-53 Grace Church St (#271)


PAGING_HANG_S = 1.5        # see ChannelCfg.hang_s / continue_s
PAGING_CONTINUE_S = 3.0
KINDS = ("paging", "traffic", "airband")
# Airband (#233: HPN ATC, AM): shortfinal's tested receiver settings (sizeup/radio/am.py): the carrier squelch opens
# 3.5 dB over the noise floor, 36.4 dB of gain. hang_s / drop_db / continue_s are the FM squelch's: not used here.
AIRBAND_SQUELCH_DB = 3.5
AIRBAND_GAIN_DB = 36.4
AIRBAND_MHZ = (118.0, 137.0)   # VHF airband voice (AM)


@dataclass(frozen=True)
class ChannelCfg:
    label: str                 # "South 2", "FG1"
    mhz: float
    kind: str                  # "paging" (tones + pages) | "traffic" (voice traffic, e.g. fireground) |
                               # "airband" (AM aviation voice: HPN ATC, #233; sizeup/radio/am.py, sizeup/atc.py)
    device: str = ""           # dongle serial ("" = first found)
    gain_db: float | None = 40.0
    squelch_db: float = 10.0   # FM quieting needed to open
    drop_db: float | None = None   # end a transmission this far under its own level (fm.NoiseSquelch)
    # Fades on paging channels (GitHub #61): the squelch hang once a transmission has had tones or speech
    # in it, and how long after it closes the carrier may come back (at the close threshold) and still
    # be the same page. None: by kind (paging 1.5 s / 3 s; traffic 0.4 s, as always / none).
    hang_s: float | None = None
    continue_s: float | None = None
    # An airband channel whose frequency changes by the time of day (#260: NY Approach, 126.4 by day, 120.8 overnight):
    # ((minute of the local day, MHz), ...) in time order, each from its time until the next one (round midnight), in
    # America/New_York wall-clock time (sizeup/tz.py, DST-aware). () = mhz all day. The receiver retunes between
    # transmissions (sizeup/radio/am.py Schedule).
    schedule: tuple[tuple[int, float], ...] = ()
    # Other frequencies on the same dongle (#275, "also"): ((MHz, from, until, label), ...), from/until minutes of the
    # local day (None: all day); label: the airband channel that frequency belongs to (its transmissions are that
    # channel's). More than one frequency on: rtl_sdr + the channelizer instead of rtl_fm (sizeup/radio/channelize.py).
    also: tuple[tuple[float, int | None, int | None, str], ...] = ()

    def __post_init__(self):
        paging = self.kind == "paging"
        if self.hang_s is None:
            object.__setattr__(self, "hang_s", PAGING_HANG_S if paging else None)
        if self.continue_s is None:
            object.__setattr__(self, "continue_s", PAGING_CONTINUE_S if paging else 0.0)


@dataclass(frozen=True)
class Config:
    station_id: str
    station_name: str
    host: str
    port: int
    rtl_fm: str
    channels: tuple[ChannelCfg, ...]
    tones: tuple[ToneCode, ...]
    ours: frozenset[str]                       # tone-code names that are "our" departments
    stt_server: str
    stt_model: str
    stt_port: int
    stt_threads: int
    stt_prompt: str
    p25_dir: str
    p25_talkgroups: dict[int, str] = field(default_factory=dict)
    tones_file: str = ""                       # the tone list file read into tones ([radio] tones_file), "" for none
    p25_incident_talkgroups: frozenset[int] = frozenset({307, 333})
    p25_tg_info: dict[int, dict] = field(default_factory=dict)   # tg -> {name, group, encrypted}  # attach to our incident (others: context)
    p25_aliases: dict[str, str] = field(default_factory=dict)
    p25_emergency_window_min: float = 10.0   # the emergency button keeps its talkgroup and radio this long (#128)
    # More P25 systems (#409: <recorder_dir or the talkgroups file's folder>/systems/<name>/, sizeup/radio/systems.py)
    p25_systems: tuple = ()
    tier_keep_days: dict = field(default_factory=lambda: {"alert": 30.0, "text": 0.0, "record": 7.0})
    context_audio_max_mb: float = 100.0      # hub: another system's clips kept at most (their own budget, inside the cap)
    p25_system_problems: tuple = ()          # a system directory that couldn't be used: said, that system left out
    mdc_aliases: dict[str, str] = field(default_factory=dict)   # MDC-1200 unit ID ("0611") -> name
    unit_departments: dict[str, str] = field(default_factory=dict)  # "Engine 60" -> "Port Chester FD"
    unit_callsigns: dict[str, str] = field(default_factory=dict)    # "2398" -> "2398 Duty Officer"
    stt_beam_size: int = 0            # whisper-server -bs (0/1 = greedy). Beam search: fewer garbled digit strings
    stt_talker_prompt: str = "off"    # "ids": hint whisper with who keyed up ("77 Alpha 4, 60 Control"); "ids+time": + HHMM
    # Amazon Transcribe streaming ([stt] aws*, sizeup/transcribe.py, sizeup/channels.py): where it runs (the hub;
    # all-in-one only with credentials here), and which channels default to it (Settings > Radios changes that).
    stt_aws: bool = False             # this process may call Transcribe (the hub: deploy/hub/config.hub.toml)
    stt_aws_region: str = ""          # "" = AWS_REGION / AWS_DEFAULT_REGION (the Fargate task has it), else us-east-1
    stt_aws_endpoint: str = ""        # the streaming endpoint (tests / dev: a local fake ws://127.0.0.1:port)
    stt_aws_api_endpoint: str = ""    # the JSON API for the custom vocabulary (tests / dev: a local fake)
    stt_aws_idle_s: float = 120.0     # a channel's stream ends this long after its last transmission
    stt_aws_pace: float = 2.0         # audio sent this many times faster than real time
    stt_aws_limit_usd: float = 25.0   # the monthly limit until Settings > Radios sets one
    stt_aws_channels: tuple[str, ...] = ("South 2", "FG1", "FG2", "tg:307")   # engine "aws" by default
    stt_aws_vocabulary: str = "sizeup-pcfd"
    stt_record_only: tuple[str, ...] = ()     # recorded, not transcribed by default ([stt] record_only, #201)
    shared_device: str = ""      # one dongle for everything (bench): switch channel / P25 in the web app
    start_mode: str = ""         # with shared_device: what the dongle does at startup ("South 2", "FG1", "Fire/EMS 15")
    sdrs: tuple[str, ...] = ()   # dongles (exact USB serials) sizeup may use, each switched in the web app
    sdr_names: dict[str, str] = field(default_factory=dict)   # serial -> name shown in the web app ("adsb" -> "V3")
    sdr_ignore: tuple[str, ...] = ()   # serials never shown or used (a dongle dedicated to another app)
    sdr_auto: bool = False       # switch whatever RTL-SDRs are plugged in (rows come and go with the dongles)
    # A dongle that appears with no job a person chose gets the first job in auto_assign_order that no
    # other dongle has (sizeup/app.py _auto_assign). load() defaults it on; built directly (tests), off.
    sdr_auto_assign: bool = False
    # #275: the airband channels' `also` frequencies taken (several per dongle, sizeup/radio/channelize.py). Off by default
    # (PR #289's review): each dongle runs rtl_fm on its own channel until an on-air A/B (tools/airband_ab.py) says the
    # wideband capture costs nothing at the site.
    airband_multichannel: bool = False
    sdr_auto_order: tuple[str, ...] = ("FG1", "Fire/EMS 15", "South 2", "FG2", "HPN Tower")   # labels / "Fire/EMS 15" (P25)
    p25_recorder: str = ""       # trunk-recorder command line, started/stopped by sizeup
    p25_recorder_dir: str = ""   # its working directory (config.json + talkgroups.csv)
    clips_dir: str = ""
    keep_hours: float = 24.0          # pages, incidents, transcripts
    audio_keep_hours: float = 12.0    # every audio file under captures/ (clips, P25 calls, skipped clicks)
    # The hub's audio.sqlite retention (#251, docs/cloud.md "Audio retention"): clips on a CAD event, with an alarm or
    # an emergency are a record, kept this many days (0 = forever) instead of audio_keep_hours; pruned audio_prune_batch
    # rows per transaction; free pages handed back (incremental vacuum) only in the off-peak hours [start, end) local.
    audio_keep_event_days: float = 365.0
    audio_prune_batch: int = 200
    audio_offpeak: tuple[int, int] = (2, 5)
    home: tuple[float, float, float] = (-73.6640, 41.0130, 13.4)   # lon, lat, zoom
    apparatus_path: str = ""          # the roster CSV actually found ("" = repo data/)
    # [geo]: the intersection search of the CAD's location box (#213, sizeup/geo/intersections.py): the towns it
    # searches (the gazetteer's town labels) and where "nearest" is measured from (PCFD Headquarters)
    geo_search_towns: tuple[str, ...] = ("Port Chester", "Rye Brook", "Rye", "Harrison", "Greenwich", "Mamaroneck",
                                         "Larchmont", "Town of Mamaroneck")
    geo_hq: tuple[float, float] = (41.00253, -73.66692)            # lat, lon: 209 Westchester Ave
    # [station] lat / lon: where the station PC (its antennas) is, the South End firehouse (#271), not HQ: the ADS-B
    # receiver's position (readsb --lat/--lon, sizeup/radio/adsbd.py reads it from the file itself)
    station_ll: tuple[float, float] = STATION_LL
    # CAD ([cad], [dispatch]): see sizeup/cad/
    cad_db: str = ""                  # "" = in memory (tests); load() defaults to state/cad.sqlite
    cad_department: str = "Port Chester Fire"           # first words of every dispatch, as said on the air
    cad_lists: str = ""               # the CAD's lists / boards / wording ("" = data/cad_lists.json)
    cad_monitor_departments: tuple[str, ...] = ("Port Chester FD", "Rye Brook FD")   # their tones or towns: wait to be accepted
    dispatch_tones: tuple[tuple[float, float], ...] = ((903.2, 1.0), (810.2, 3.0))   # (Hz, s) in order
    # The dispatch voice. load() defaults to Kokoro af_heart at 0.92; built directly (tests), `say` Samantha.
    dispatch_tts_engine: str = "say"  # "kokoro" (sizeup/tts_server.py; `say` as fallback) | "say" | "polly" | "none"
    dispatch_voice: str = "Samantha"  # kokoro: a Kokoro voice ("af_heart"); say: macOS `say -v`; polly: "Matthew"
    dispatch_speed: float = 0.92      # Kokoro / Polly speaking speed (0.5-2.0)
    dispatch_polly_region: str = ""   # "" = AWS_REGION / AWS_DEFAULT_REGION, else us-east-1 (sizeup/polly.py)
    dispatch_tts_port: int = 8093     # the TTS server, on 127.0.0.1
    dispatch_fallback_voice: str = "Samantha"   # `say -v` when the TTS server is down, errors or is slow
    dispatch_tts_python: str = ""     # the TTS environment's Python (tools/setup_tts.sh); "" = no TTS server
    dispatch_rate: int = 0            # words per minute (0 = the voice's own)
    dispatch_repeat: int = 2          # the voice read this many times
    dispatch_transmit: bool = False   # M1: must stay false (no radio interface yet)
    dispatch_channels: tuple[str, ...] = ("FG1", "PC repeater")   # labels on the log only, in M1
    # [cloud] (sizeup/cloud/, docs/cloud.md): "" = all-in-one; "hub" (the cloud copy, no radios) or
    # "station" (the Mac with the SDRs, forwarding to the hub).
    cloud_role: str = ""
    adapter_env: tuple = ()           # [adapter] env (#301): the adapter's credential names, never in a child's environment
    cloud_hub_url: str = ""           # station: the hub's https base URL
    cloud_token: str = ""             # both: the station's bearer token (env SIZEUP_STATION_TOKEN wins)
    cloud_access_id: str = ""         # station: Cloudflare Access service token (env SIZEUP_ACCESS_CLIENT_ID / _SECRET)
    cloud_access_secret: str = ""
    cloud_outbox: str = ""            # station: the durable outbox (SQLite); "" = outbox.sqlite next to cloud_state
    cloud_state: str = ""             # not the hub: state/cloud.json, Settings > System > Station (sizeup/cloud/stationmode.py)
    # not the hub: macOS Keychain services (generic passwords) for the three secrets when the env has none
    cloud_keychain_token: str = "sizeup-station-token"
    cloud_keychain_access_id: str = "sizeup-access-id"
    cloud_keychain_access_secret: str = "sizeup-access-secret"
    cloud_outbox_max_mb: float = 300.0   # station: queued audio over this drops the oldest audio (never metadata)
    cloud_public_url: str = ""        # hub: its public URL (env SIZEUP_PUBLIC_URL), shown in /healthz and the log
    cloud_offline_s: float = 15.0     # hub: no word from the station this long = offline
    cloud_audio_max_mb: float = 500.0  # hub: received audio kept (state/audio.sqlite), oldest dropped first
    # hub: Cloudflare Access in front of it (sizeup/cloud/access.py). Both set = every page and API
    # but /healthz and /api/station/ needs a verified Access token, and its email is the operator.
    cloud_access_team: str = ""       # "yoda.cloudflareaccess.com" (env SIZEUP_ACCESS_TEAM)
    cloud_access_aud: str = ""        # the Access application's AUD tag (env SIZEUP_ACCESS_AUD)
    # hub: the read-only bearer for GET /api/station/status (the deploy check, sizeup/cloud/health.py);
    # env SIZEUP_STATUS_TOKEN; "" = derived from the station token (health.status_token)
    cloud_status_token: str = ""
    # [prealert] (sizeup/prealert.py): Tapout's ntfy feed, read only, on the CAD of record (hub or
    # all-in-one; never a station forwarding to a hub). Off unless the section is in the config.
    prealert_enabled: bool = False
    prealert_server: str = "https://ntfy.sh"
    prealert_ring_topic: str = "pcfd-phone-prealert"
    prealert_status_topic: str = "pcfd-status"
    prealert_expire_min: float = 5.0
    prealert_heartbeat_stale_min: float = 90.0
    # [alerts] (sizeup/notify.py; docs/cloud.md "Station health and alerts"): our OWN ntfy topics for pushes that
    # page a person, two kinds (notify.route()): ntfy_topic for the incident alarms (sizeup/cad/alarms.py, #128:
    # emergency buttons, mayday, aircraft squawks; env SIZEUP_ALERT_TOPIC, the hub: SSM /sizeup-hub/alert-topic),
    # status_topic for the system's health (sizeup/cloud/health.py, #79: station health, Amazon Transcribe and its
    # budget; env SIZEUP_STATUS_TOPIC, the hub: SSM /sizeup-hub/status-topic). Only one set: everything goes there;
    # neither: no pushes. And station health's thresholds. Never Tapout's topics (pcfd-phone-prealert, pcfd-status,
    # whatever [prealert] names): refused here and in sizeup/notify.py.
    alerts_ntfy_topic: str = ""
    alerts_status_topic: str = ""
    alerts_ntfy_topic_source: str = ""     # where each came from: "env" (the hub: SSM) | "config" | "" (unset)
    alerts_status_topic_source: str = ""   # (Settings > System > Push notifications shows it; Settings win over both)
    alerts_ntfy_server_source: str = "default"
    alerts_ntfy_server: str = "https://ntfy.sh"
    alerts_silent_min: float = 3.0       # no state post from the station this long
    alerts_behind_min: float = 15.0      # the station on another build than the hub this long
    alerts_down_min: float = 2.0         # whisper or trunk-recorder down this long
    alerts_outbox_min: float = 5.0       # the oldest event still waiting on the station this old
    alerts_disk_pct: float = 90.0        # the station's disk this full
    alerts_repeat_min: float = 15.0      # at most one push per condition this often (plus its recovery)
    # "P25 trunk silent" (sizeup/cloud/health.py, #258): no call on ANY talkgroup this long while trunk-recorder runs
    # (longer when the system's learned gaps say so); one channel or talkgroup silent is only a hint
    alerts_p25_silent_min: float = 20.0
    alerts_p25_silent_night_min: float = 60.0   # ... for a silence that reaches the night (00-06, station time)
    # [station] auto_update: a Linux station installed by deploy/station/install-linux.sh follows the
    # hub's build (sizeup/cloud/autoupdate.py); false keeps it where it is (also: sizeup-station autoupdate off)
    station_auto_update: bool = True
    # [atc] (sizeup/atc.py, #233): the airport pre-alert from HPN ATC (the airband channels' transcripts), on the CAD of
    # record: a rule hit -> an airport pre-alert (sizeup/cad/airport_alerts.py) and a push to the alerts topic
    atc_enabled: bool = True
    atc_rules: str = ""                # the rule set ("" = data/atc/rules.json)
    atc_context: int = 4               # earlier transmissions on the channel read with each one (the exchange)
    atc_context_s: float = 120.0       # ... no older than this
    atc_dedupe_min: float = 5.0        # the same subject again within this: added to the open pre-alert
    atc_expire_min: float = 30.0       # an open pre-alert nobody answered: expired after this
    atc_max_per_hour: int = 8          # new pre-alerts an hour at most (more: added to the latest open one)
    atc_alert_level_incident: tuple = (2, 3)   # #369: 60 Control's "alert level N" at these levels also makes a queued incident
    atc_airport_names: tuple = ("westchester", "west chester", "hpn", "white plains")   # #369: our airport, as said
    # [roads] (sizeup/roads.py, #73, #86, #188): the highway layers on the CAD maps: the Thruway's public feeds
    # and the county's cameras (on by default), and 511NY's on CARS: its public signs (on by default), the API's
    # events and the CARS-Hub's CCTV feed with 511NY's username and password (env SIZEUP_511NY_USER / SIZEUP_511NY_PASS, the
    # hub: SSM /sizeup-hub/511ny-user and /sizeup-hub/511ny-pass; never in a file): off until they are there.
    roads_enabled: bool = True
    roads_ny511: bool = True
    roads_511ny_user: str = ""
    roads_511ny_pass: str = ""
    roads_bbox: tuple[float, float, float, float] = (-73.80, 40.92, -73.56, 41.10)   # west, south, east, north
    roads_cameras_s: float = 3600.0
    roads_events_s: float = 300.0       # 511NY: "once every 5 minutes"
    roads_signs_s: float = 300.0
    # 511NY's addresses: "" = its own (sizeup/roads.py NY511_URLS), "off" = that feed off, else an https:// address
    roads_events_url: str = ""
    roads_signs_url: str = ""
    roads_ny511_hub: str = ""           # the CARS-Hub's base address ("" = https://nysdot.carsprogram.org/hub/, "off" = none)
    roads_carshub_imagery: bool = True   # CARS-Hub cameras on the map / wall / overlay (false: only counted against ours)
    roads_thruway: bool = True        # the NYS Thruway Authority's public feeds (no key): I-95, I-287 (#86)
    roads_county: bool = True         # Westchester County DPW's cameras (a public ArcGIS layer, no key) (#99)
    roads_stream_max: int = 8         # live camera streams open at once, hub-wide (sizeup/roads.py STREAM_MAX, #125)
    roads_wall_streams: int = 0       # the camera wall's live tiles at once per page; 0: every camera in our area that streams
    roads_dvr: bool = True            # camera DVR (#277, sizeup/dvr.py): segments kept on the hub's disk, on demand
    roads_dvr_window_s: float = 7200.0
    roads_dvr_max_bytes: int = 6 * 1024 ** 3
    roads_dvr_near_m: float = 1000.0
    roads_dvr_linger_s: float = 1800.0
    roads_dvr_max_cameras: int = 12
    roads_dvr_always: tuple[str, ...] = ()
    roads_dvr_dir: str = ""
    # #322: on shared storage (EFS at dvr_dir) the response area's cameras around the clock and a 12 h window; "auto":
    # the mount says. Which cameras: the camera wall's area (Settings > System: our fire districts plus its margin_m,
    # its include / exclude) and dvr_extra_m beyond that margin; dvr_area: a polygon instead (an override, rarely)
    roads_dvr_shared: str = "auto"                     # auto | on | off
    roads_dvr_area: tuple | None = None                # None: the camera wall's area; () none; a ring of (lon, lat)
    roads_dvr_extra_m: float = 1000.0
    roads_dvr_shared_window_s: float = 43200.0
    roads_dvr_shared_max_bytes: int = 25 * 1024 ** 3
    roads_dvr_shared_max_cameras: int = 20
    # [roadwork] (sizeup/roadprojects.py, #185): road projects & closures for the maps and the incident view: the
    # Thruway's and 511NY's roadwork (through [roads]), the capital programs (Thruway, NYSDOT, CTDOT), the villages'
    # road notices; 511NY's WZDx (the CARS API, with [roads]' username and password) and any other public WZDx feed.
    roadwork_enabled: bool = True
    roadwork_projects: bool = True
    roadwork_notices: bool = True
    roadwork_wzdx: bool = True          # 511NY's WZDx (CARS API /wzdx): on once the credentials are there
    roadwork_wzdx_url: str = ""
    # [gis] (sizeup/gis.py, #91): Westchester County GIS datasets as map layers, fetched by the hub.
    # load() ships the starter set (gis.DEFAULT_LAYERS); built directly (tests), none.
    gis_enabled: bool = True
    gis_bbox: tuple[float, float, float, float] = (-73.80, 40.92, -73.56, 41.10)
    gis_layers: tuple = ()            # sizeup.gis.Layer, in panel order
    # [routing] (sizeup/roadgraph.py, #360): the road graph from OpenStreetMap for the rigs' probable paths and the
    # estimates by road; the hub (or all-in-one) only. load() turns it on; built directly (tests): off (no fetching).
    routing_enabled: bool = False
    routing_ring_km: float = 12.0     # around the firehouses (data/firehouses.json)
    routing_overpass_url: str = ""    # "" = overpass-api.de (sizeup/roadgraph.py OVERPASS_URL)
    # [trains] (sizeup/trains.py, #131): Metro-North's New Haven Line from the MTA's public feeds (no key
    # today; one, should the MTA bring keys back: env SIZEUP_MTA_KEY, the hub: SSM /sizeup-hub/mta-key).
    # The feed addresses: "" = the MTA's (sizeup/trains.py RT_URL, GTFS_URL).
    trains_enabled: bool = True
    trains_key: str = ""
    trains_rt_url: str = ""
    trains_gtfs_url: str = ""
    trains_bbox: tuple[float, float, float, float] = (-73.845, 40.900, -73.530, 41.060)
    trains_rt_every_s: float = 5.0          # the realtime feed this often while someone looks (5 to 300 s)
    trains_traintime: bool = True           # MTA TrainTime merged in (sizeup/traintime.py, #362); GTFS-realtime the fallback
    trains_traintime_url: str = ""          # "" = the MTA's (sizeup/traintime.py BASE_URL)
    trains_traintime_every_s: float = 5.0   # TrainTime this often while the layer is on / an incident is near (5 to 60 s)
    # [weather] (sizeup/weather.py, #132): NWS alerts, conditions and radar, NOAA tides, fetched by the hub.
    weather_enabled: bool = True
    weather_radar: bool = True
    weather_zones: tuple[str, ...] = ("NYZ071", "NYC119", "NYZ211")   # forecast, county, fire weather zone
    weather_marine_zones: tuple[str, ...] = ("ANZ335",)                # Long Island Sound west
    weather_area: str = "Southern Westchester"                         # how a spoken warning names it
    weather_station: str = "KHPN"
    weather_tide_station: tuple[str, str] = ("8518091", "Rye Beach")
    weather_gauge_station: tuple[str, str] = ("8516945", "Kings Point")
    # [aircraft] (sizeup/aircraft.py, #155): aircraft near HPN, shortfinal's PCFD-focused design, read by the hub
    # (or all-in-one). feeds: "receiver" (our own ADS-B receiver: its positions first), "adsblol" (ODbL) and
    # "adsbfi" (personal, non-commercial use: the user's decision, 2026-10-14), combined; the busier API is primary.
    aircraft_enabled: bool = True
    aircraft_feeds: tuple[str, ...] = ("adsblol", "adsbfi")
    aircraft_alarms: bool = True      # a 7500 / 7600 / 7700 squawk near HPN raises the alarm (and the push)
    aircraft_radius_nm: float = 15.0
    aircraft_ceiling_ft: float = 6000.0
    aircraft_adsblol_url: str = ""    # "" = adsb.lol's public API (sizeup/aircraft.py ADSBLOL_URL)
    aircraft_adsbfi_url: str = ""
    aircraft_registry: bool = True    # types from the FAA Releasable Aircraft Database (public domain)
    # [aircraft.receiver]: our own receiver (readsb / dump1090 1090 MHz, dump978-fa 978 MHz UAT aircraft.json, a path
    # or an http(s) URL); sdr: the dongle meant for ADS-B (exact USB serial), kept out of sizeup's own radios and the
    # only one the station's decoder (sizeup/radio/adsbd.py, sizeup-adsb.service) ever opens; gain / bias_tee: its
    # readsb settings (auto gain; bias-tee power for an LNA that needs it, off by default). A named dongle's decoder
    # output is json_1090 unless one is given. The station forwards what json_1090 / json_978 say to the hub (#235).
    aircraft_receiver: bool = False
    aircraft_receiver_1090: str = ""
    aircraft_receiver_978: str = ""
    aircraft_sdr: str = ""
    aircraft_gain: str = "auto"
    aircraft_bias_tee: bool = False
    # [vessels] (sizeup/vessels.py, docs/vessels.md): AIS ships and boats on western Long Island Sound, read by the hub
    # (or all-in-one). aisstream.io only with a key: [vessels] aisstream_key or env SIZEUP_AISSTREAM_KEY (the hub: SSM
    # /sizeup-hub/aisstream-key); none: that source is off. bbox: the area (west, south, east, north), by default Throgs
    # Neck to Stamford across to Long Island's north shore; anything outside it is dropped.
    vessels_enabled: bool = True
    vessels_aisstream_key: str = ""
    vessels_bbox: tuple[float, float, float, float] = (-73.83, 40.78, -73.50, 41.07)
    # [vessels.receiver]: our own AIS receiver (AIS-catcher on one RTL-SDR at the station, sizeup-ais.service,
    # sizeup/radio/aisd.py); off until the dongle and a marine VHF antenna are in. sdr: that dongle's exact USB serial,
    # kept out of sizeup's own radios; json: the decoder's messages file ("" = sizeup/radio/aisd.py MESSAGES).
    vessels_receiver: bool = False
    vessels_sdr: str = ""
    vessels_gain: str = "auto"
    vessels_ppm: int = 0
    vessels_receiver_json: str = ""


def read_tones_file(path: str | Path) -> list[ToneCode]:
    """The tone list file (data/westchester_tones.csv: name, a_hz, b_hz, estimated, source, fetched; # comments):
    read at start, and again by Settings > Data sources' Reload (sizeup/datasources.py, #186)."""
    import csv
    rows = (ln for ln in Path(path).read_text().splitlines() if ln.strip() and not ln.startswith("#"))
    out = []
    for r in csv.DictReader(rows):
        tag = {"1": " (est.)", "2": " (heard)"}.get(r.get("estimated", "0"), "")
        out.append(ToneCode(r["name"] + tag, float(r["a_hz"]), float(r["b_hz"]) if r.get("b_hz") else None,
                            source=(r.get("source") or "list").strip(), fetched=(r.get("fetched") or "").strip()))
    return out


def _take(d: dict, path: str, allowed: set[str]) -> dict:
    extra = set(d) - allowed
    if extra:
        raise ConfigError(f"{path}: unknown key(s) {', '.join(sorted(extra))}")
    return d


def _dispatch_voice(dsp: dict) -> tuple[str, str, str]:
    """[dispatch] tts_engine, voice, fallback_voice. An older config's `voice = "Samantha"` (a `say`
    voice, no tts_engine) becomes the fallback, and Kokoro's af_heart the voice."""
    engine = str(dsp.get("tts_engine", "kokoro"))
    if engine not in ("kokoro", "say", "polly", "none"):
        raise ConfigError('dispatch.tts_engine: "kokoro", "polly", "say" or "none"')
    voice, fallback = str(dsp.get("voice", "")).strip(), str(dsp.get("fallback_voice", "")).strip()
    if engine == "polly" and voice and voice not in POLLY_VOICES:
        raise ConfigError(f"dispatch.voice: {voice!r} isn't one of the Polly voices offered ({', '.join(POLLY_VOICES)})")
    if engine == "kokoro" and voice and voice not in KOKORO_VOICES:
        if "tts_engine" in dsp or re.fullmatch(r"[a-z]{2}_\w+", voice):
            raise ConfigError(f"dispatch.voice: {voice!r} isn't a Kokoro voice (af_heart, am_michael, bf_emma, ...); "
                              'for a macOS `say` voice set tts_engine = "say", or use it as fallback_voice')
        voice, fallback = "", fallback or voice
    voice = voice or {"kokoro": "af_heart", "polly": POLLY_DEFAULT}.get(engine, "Samantha")
    fallback = fallback or "Samantha"
    if not all(re.fullmatch(r"[\w .()'-]{1,60}", v) for v in (voice, fallback)):
        raise ConfigError("dispatch.voice / dispatch.fallback_voice: a voice name")
    return engine, voice, fallback


def hub_url_problem(url: str) -> str:
    """"" for a usable hub URL, else why not (config and Settings > System > Station)."""
    m = re.fullmatch(r"(https?)://([^/:\s]+)(:\d{1,5})?(/\S*)?", url or "")
    if not m:
        return "the hub's URL, https://..."
    if m.group(1) == "http" and m.group(2) not in ("127.0.0.1", "localhost", "::1"):
        return "https:// (plain http only to this machine)"
    return ""


# Environment overrides, for a container whose config file is baked into the image (deploy/hub/):
#   SIZEUP_HOST, SIZEUP_PORT          [web] host / port
#   SIZEUP_STATE_DIR                  where relative storage paths go (cad.db, storage.clips_dir,
#                                     cloud.outbox) instead of the config file's folder
#   SIZEUP_STATION_TOKEN              [cloud] token (preferred: never in a file)
#   SIZEUP_ACCESS_CLIENT_ID / _SECRET [cloud] access_client_id / access_client_secret
#   SIZEUP_PUBLIC_URL, SIZEUP_HUB_URL [cloud] public_url / hub_url
#   SIZEUP_ACCESS_TEAM / _AUD         [cloud] access_team / access_aud (hub: Cloudflare Access identity)
#   SIZEUP_PREALERT_SERVER            [prealert] server (a local test server instead of ntfy.sh)
#   SIZEUP_ALERT_TOPIC / _SERVER [alerts] ntfy_topic / ntfy_server (our alarms' push topic: a name is its only
#                                secret; the hub: SSM /sizeup-hub/alert-topic)
#   SIZEUP_STATUS_TOPIC          [alerts] status_topic (our status / health push topic; the hub: SSM
#                                /sizeup-hub/status-topic)
#   SIZEUP_511NY_USER / _PASS         [roads]: 511NY's username and password for the CARS API and CARS-Hub (only
#                                     ever here; the hub: SSM /sizeup-hub/511ny-user, /sizeup-hub/511ny-pass)
#   SIZEUP_STATUS_TOKEN               [cloud] status_token: the deploy check's read-only bearer
#   (no [gis] secrets: the county's open data needs no key)
#   SIZEUP_MTA_KEY                    [trains] key: only should the MTA require one again (none needed, 2026-10-14)
#   SIZEUP_AISSTREAM_KEY              [vessels] aisstream_key: aisstream.io's API key (the hub: SSM
#                                     /sizeup-hub/aisstream-key); none: that source is off
def _env(name: str, default):
    v = os.environ.get(name, "").strip()
    return v if v else default


def _keychain(cl: dict) -> dict:
    """[cloud] keychain_prefix ("sizeup": sizeup-station-token, sizeup-access-id, sizeup-access-secret)
    or keychain_token / keychain_access_id / keychain_access_secret one by one."""
    prefix = str(cl.get("keychain_prefix", "sizeup"))
    out = {}
    for key, field_, suffix in (("keychain_token", "cloud_keychain_token", "station-token"),
                                ("keychain_access_id", "cloud_keychain_access_id", "access-id"),
                                ("keychain_access_secret", "cloud_keychain_access_secret", "access-secret")):
        name = str(cl.get(key, f"{prefix}-{suffix}"))
        if not re.fullmatch(r"[\w.@-]{1,100}", name):
            raise ConfigError(f"cloud.{key} / keychain_prefix: a Keychain service name (letters, digits, . _ - @)")
        out[field_] = name
    return out


def _cloud(data: dict) -> dict:
    """[cloud] -> Config fields. No section = all-in-one (role ""). A section without a role is
    all-in-one too, with its hub_url / Keychain names ready for Settings > System > Station."""
    cl = _take(data.get("cloud", {}), "cloud", {"role", "hub_url", "token", "access_client_id", "access_client_secret",
                                                "outbox", "outbox_max_mb", "public_url", "offline_after_s", "audio_max_mb",
                                                "access_team", "access_aud", "keychain_prefix", "keychain_token",
                                                "keychain_access_id", "keychain_access_secret", "status_token"})
    if not cl:
        return {}                      # all-in-one: no environment variable changes anything here
    role = str(cl.get("role", ""))
    if role not in ("", "hub", "station"):
        raise ConfigError('cloud.role: "hub" or "station" (or leave it out: all-in-one)')
    token = _env("SIZEUP_STATION_TOKEN", str(cl.get("token", "")))
    hub_url = _env("SIZEUP_HUB_URL", str(cl.get("hub_url", ""))).rstrip("/")
    if role == "station" or (role == "" and hub_url):
        why = hub_url_problem(hub_url)
        if why:
            raise ConfigError(f"cloud.hub_url: {why}")
    # The station's token may also come from the Keychain (sizeup/cloud/stationmode.py): without one
    # anywhere the link waits, and Settings > System > Station says what is missing.
    if token and len(token) < 16:
        raise ConfigError("cloud.token: at least 16 characters (e.g. `openssl rand -hex 32`)")
    status_token = _env("SIZEUP_STATUS_TOKEN", str(cl.get("status_token", "")))
    if status_token and (len(status_token) < 16 or status_token == token):
        raise ConfigError("cloud.status_token / SIZEUP_STATUS_TOKEN: at least 16 characters, not the station token")
    team_raw = _env("SIZEUP_ACCESS_TEAM", str(cl.get("access_team", "")))
    team = team_host(team_raw)
    if team_raw and not team:
        raise ConfigError('cloud.access_team: the Access team domain, e.g. "yoda.cloudflareaccess.com"')
    aud = _env("SIZEUP_ACCESS_AUD", str(cl.get("access_aud", ""))).strip().lower()
    if aud and not re.fullmatch(r"[0-9a-f]{32,128}", aud):
        raise ConfigError("cloud.access_aud: the Access application's AUD tag (64 hex digits, from Zero Trust → "
                          "Access → Applications → the app → Overview)")
    try:
        nums = {k: float(cl.get(k, d)) for k, d in (("outbox_max_mb", 300), ("offline_after_s", 15), ("audio_max_mb", 500))}
    except (TypeError, ValueError):
        raise ConfigError("cloud.outbox_max_mb / offline_after_s / audio_max_mb: numbers") from None
    if nums["outbox_max_mb"] < 1 or nums["audio_max_mb"] < 1 or not 5 <= nums["offline_after_s"] <= 300:
        raise ConfigError("cloud: outbox_max_mb and audio_max_mb at least 1; offline_after_s 5 to 300")
    return dict(cloud_role=role, cloud_hub_url=hub_url, cloud_token=token,
                cloud_access_id=_env("SIZEUP_ACCESS_CLIENT_ID", str(cl.get("access_client_id", ""))),
                cloud_access_secret=_env("SIZEUP_ACCESS_CLIENT_SECRET", str(cl.get("access_client_secret", ""))),
                cloud_outbox=str(cl.get("outbox", "state/outbox.sqlite")) if role != "hub" else "",
                cloud_outbox_max_mb=nums["outbox_max_mb"], cloud_offline_s=nums["offline_after_s"],
                cloud_audio_max_mb=nums["audio_max_mb"],
                cloud_public_url=_env("SIZEUP_PUBLIC_URL", str(cl.get("public_url", ""))).rstrip("/"),
                cloud_access_team=team, cloud_access_aud=aud, cloud_status_token=status_token, **_keychain(cl))


def _prealert(data: dict) -> dict:
    """[prealert] -> Config fields. No section = off. Read only: sizeup never publishes to ntfy."""
    if "prealert" not in data:
        return {}
    pa = _take(data["prealert"], "prealert", {"enabled", "server", "ring_topic", "status_topic", "expire_min",
                                              "heartbeat_stale_min"})
    server = _env("SIZEUP_PREALERT_SERVER", str(pa.get("server", "https://ntfy.sh"))).rstrip("/")
    m = re.fullmatch(r"(https?)://([^/:?#]+)(:\d+)?", server)
    if not m or (m.group(1) == "http" and m.group(2) not in ("127.0.0.1", "localhost", "::1")):
        raise ConfigError('prealert.server: the ntfy server, "https://ntfy.sh" (plain http only to this machine)')
    topics = {k: str(pa.get(k, d)) for k, d in (("ring_topic", "pcfd-phone-prealert"), ("status_topic", "pcfd-status"))}
    if not all(re.fullmatch(r"[-_A-Za-z0-9]{1,64}", t) for t in topics.values()) or len(set(topics.values())) != 2:
        raise ConfigError("prealert.ring_topic / status_topic: two different ntfy topics (letters, digits, - and _)")
    try:
        expire, stale = float(pa.get("expire_min", 5)), float(pa.get("heartbeat_stale_min", 90))
    except (TypeError, ValueError):
        raise ConfigError("prealert.expire_min / heartbeat_stale_min: minutes") from None
    if not 0.5 <= expire <= 60 or not 5 <= stale <= 24 * 60:
        raise ConfigError("prealert.expire_min: 0.5 to 60; prealert.heartbeat_stale_min: 5 to 1440")
    enabled = pa.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError("prealert.enabled: true or false")
    return dict(prealert_enabled=enabled, prealert_server=server, prealert_ring_topic=topics["ring_topic"],
                prealert_status_topic=topics["status_topic"], prealert_expire_min=expire,
                prealert_heartbeat_stale_min=stale)


def _alerts(data: dict, prealert: dict) -> dict:
    """[alerts] -> Config fields: our ntfy topics for pushes (sizeup/notify.py route(): ntfy_topic for the
    emergency alarms, #128; status_topic for station health, #79, and Amazon Transcribe) and station health's
    thresholds (sizeup/cloud/health.py). No section: the defaults, no push unless SIZEUP_ALERT_TOPIC or
    SIZEUP_STATUS_TOPIC is set. Tapout's topics (and whatever [prealert] names) are refused, by exact name."""
    from sizeup.notify import server_problem, topic_problem
    al = _take(data.get("alerts", {}), "alerts", {"ntfy_topic", "status_topic", "ntfy_server", "silent_min",
                                                  "behind_min", "down_min", "outbox_min", "disk_pct", "repeat_min",
                                                  "p25_silent_min", "p25_silent_night_min",
                                                  "main_channels"})   # (#242's, inert since #258: read, ignored)
    tapout = (prealert.get("prealert_ring_topic", ""), prealert.get("prealert_status_topic", ""))
    topics, sources = {}, {}
    for key, env in (("ntfy_topic", "SIZEUP_ALERT_TOPIC"), ("status_topic", "SIZEUP_STATUS_TOPIC")):
        t = _env(env, str(al.get(key, ""))).strip()
        why = topic_problem(t, tapout) if t else ""
        if why:
            raise ConfigError(f"alerts.{key} / {env}: {why}")
        topics[key] = t
        sources[key] = ("env" if os.environ.get(env, "").strip() else "config") if t else ""
    topic = topics["ntfy_topic"]
    server = _env("SIZEUP_ALERT_SERVER", str(al.get("ntfy_server", "https://ntfy.sh"))).strip().rstrip("/")
    if server_problem(server):
        raise ConfigError(f"alerts.ntfy_server: {server_problem(server)}")
    limits = {"silent_min": (3, 1, 120), "behind_min": (15, 5, 1440), "down_min": (2, 0.5, 120),
              "outbox_min": (5, 1, 1440), "disk_pct": (90, 50, 99.9), "repeat_min": (15, 1, 1440),
              "p25_silent_min": (20, 5, 720), "p25_silent_night_min": (60, 5, 720)}
    out = {}
    for k, (d, lo, hi) in limits.items():
        v = al.get(k, d)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= float(v) <= hi:
            raise ConfigError(f"alerts.{k}: a number from {lo:g} to {hi:g}")
        out[f"alerts_{k}"] = float(v)
    # main_channels (#242: per-channel silence alerts) is no more (#258: only the whole P25 system silent alerts): an
    # older config that has it still loads, and it changes nothing
    server_source = ("env" if os.environ.get("SIZEUP_ALERT_SERVER", "").strip() else
                     "config" if str(al.get("ntfy_server", "")).strip() else "default")
    return dict(alerts_ntfy_topic=topic, alerts_status_topic=topics["status_topic"], alerts_ntfy_server=server,
                alerts_ntfy_topic_source=sources["ntfy_topic"], alerts_status_topic_source=sources["status_topic"],
                alerts_ntfy_server_source=server_source, **out)


P25_JOB = "Fire/EMS 15"   # the dongle job that runs trunk-recorder (sizeup/app.py P25_MODE)
# FG2 (#119), then HPN Tower (#233, ATC): only a 4th / 5th free dongle takes them by themselves; a job with no channel
# in the config is skipped.
AUTO_ORDER = ("FG1", P25_JOB, "South 2", "FG2", "HPN Tower")


def _airband(c: dict, i: int) -> ChannelCfg:
    """One [[radio.channels]] entry with kind = "airband": AM, the carrier squelch (squelch_db over the floor), no
    FM-only keys. The frequency must be VHF airband voice (118-137 MHz)."""
    where = f"radio.channels[{i}]"
    for k in ("drop_db", "hang_s", "continue_s"):
        if k in c:
            raise ConfigError(f"{where}.{k}: FM channels only (an airband channel's squelch is the AM carrier's)")
    try:
        mhz, sq = float(c["mhz"]), float(c.get("squelch_db", AIRBAND_SQUELCH_DB))
    except (KeyError, TypeError, ValueError):
        raise ConfigError(f"{where}: label, mhz (and squelch_db) as numbers") from None
    if not AIRBAND_MHZ[0] <= mhz <= AIRBAND_MHZ[1]:
        raise ConfigError(f"{where}.mhz: airband voice is {AIRBAND_MHZ[0]:g} to {AIRBAND_MHZ[1]:g} MHz (AM)")
    if not 1.0 <= sq <= 20.0:
        raise ConfigError(f"{where}.squelch_db: 1 to 20 dB over the noise floor (3.5: shortfinal's, tested)")
    sched = _schedule(c["schedule"], mhz, where) if "schedule" in c else ()
    also = _also(c["also"], where) if "also" in c else ()
    return ChannelCfg(str(c["label"]), mhz, "airband", c.get("device", ""), c.get("gain_db", AIRBAND_GAIN_DB), sq,
                      schedule=sched, also=also)


_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def _hhmm(v, where: str) -> int:
    m = _HHMM.match(v.strip()) if isinstance(v, str) else None
    if not m:
        raise ConfigError(f'{where}: {v!r}: a time of day "HH:MM" (00:00 to 23:59)')
    return int(m.group(1)) * 60 + int(m.group(2))


def _also(raw, where: str) -> tuple[tuple[float, int | None, int | None, str], ...]:
    """An airband channel's "also" (#275): [{mhz = 120.8, from = "23:00", until = "07:00"}, ...]: other frequencies
    on the same dongle, all day or between two local times. The labels are filled in by _also_owners."""
    why = (f'{where}.also: a list of {{mhz = ..., from = "HH:MM", until = "HH:MM"}} (from / until: both or neither), '
           'e.g. [{mhz = 120.8, from = "23:00", until = "07:00"}]')
    if not isinstance(raw, list) or not raw:
        raise ConfigError(why)
    out = []
    for e in raw:
        if not isinstance(e, dict) or "mhz" not in e or set(e) - {"mhz", "from", "until"} \
                or ("from" in e) != ("until" in e) or isinstance(e["mhz"], bool) or not isinstance(e["mhz"], (int, float)):
            raise ConfigError(why)
        f = float(e["mhz"])
        if not AIRBAND_MHZ[0] <= f <= AIRBAND_MHZ[1]:
            raise ConfigError(f"{where}.also: {f:g} MHz: airband voice is {AIRBAND_MHZ[0]:g} to {AIRBAND_MHZ[1]:g} MHz (AM)")
        a = _hhmm(e["from"], f"{where}.also.from") if "from" in e else None
        b = _hhmm(e["until"], f"{where}.also.until") if "until" in e else None
        out.append((f, a, b, ""))
    return tuple(out)


def airband_owner(channels, mhz: float) -> str | None:
    """The airband channel a frequency belongs to (#275): the one on it (its mhz), else the one whose schedule has it."""
    key = round(float(mhz), 6)
    air = [c for c in channels if c.kind == "airband"]
    for c in air:
        if round(c.mhz, 6) == key:
            return c.label
    for c in air:
        if any(round(f, 6) == key for _at, f in c.schedule):
            return c.label
    return None


def _also_owners(channels: list) -> list:
    """Each "also" frequency with the channel it belongs to (that channel's transmissions, AWS, talkers ...), and every
    set of frequencies one dongle would have on at once checked to fit one capture (sizeup/radio/channelize.py plan)."""
    from dataclasses import replace

    from sizeup.radio import channelize
    out = []
    for i, c in enumerate(channels):
        if not c.also:
            out.append(c)
            continue
        where = f"radio.channels[{i}].also"
        also = []
        for f, a, b, _l in c.also:
            owner = airband_owner(channels, f)
            if owner is None:
                raise ConfigError(f"{where}: {f:g} MHz isn't on any airband channel here: add a channel for it")
            also.append((f, a, b, owner))
        cuts = sorted({0, *(at for at, _f in c.schedule),
                       *(x for f, a, b, _o in also for x in (a, b) if x is not None)})
        for m in cuts:
            first = c.mhz
            for at, f in c.schedule:
                first = f if m >= at else first
            if c.schedule and m < c.schedule[0][0]:
                first = c.schedule[-1][1]
            on = {round(first, 6)}
            for f, a, b, _o in also:
                if a is None or b is None or a == b or (a <= m < b if a < b else (m >= a or m < b)):
                    on.add(round(f, 6))
            try:
                channelize.plan(sorted(on))
            except ValueError as e:
                raise ConfigError(f"{where}: at {m // 60:02d}:{m % 60:02d} {e}") from None
        out.append(replace(c, also=tuple(also)))
    return out


def _schedule(raw, mhz: float, where: str) -> tuple[tuple[int, float], ...]:
    """An airband channel's "schedule" (#260): [{at = "07:00", mhz = 126.4}, {at = "23:00", mhz = 120.8}], local
    (America/New_York) wall-clock times, each frequency from its time until the next. mhz is one of them."""
    why = (f'{where}.schedule: a list of {{at = "HH:MM", mhz = ...}} (local time), two or more, e.g. '
           '[{at = "07:00", mhz = 126.4}, {at = "23:00", mhz = 120.8}]')
    if not isinstance(raw, list) or len(raw) < 2:
        raise ConfigError(why)
    out: dict[int, float] = {}
    for e in raw:
        if not isinstance(e, dict) or set(e) != {"at", "mhz"} or not isinstance(e["at"], str) \
                or isinstance(e["mhz"], bool) or not isinstance(e["mhz"], (int, float)):
            raise ConfigError(why)
        m = _HHMM.match(e["at"].strip())
        if not m:
            raise ConfigError(f'{where}.schedule: at = {e["at"]!r}: a time of day "HH:MM" (00:00 to 23:59)')
        at = int(m.group(1)) * 60 + int(m.group(2))
        if at in out:
            raise ConfigError(f'{where}.schedule: {e["at"]} is there twice')
        f = float(e["mhz"])
        if not AIRBAND_MHZ[0] <= f <= AIRBAND_MHZ[1]:
            raise ConfigError(f"{where}.schedule: {f:g} MHz: airband voice is {AIRBAND_MHZ[0]:g} to {AIRBAND_MHZ[1]:g} MHz (AM)")
        out[at] = f
    if round(mhz, 6) not in {round(f, 6) for f in out.values()}:
        raise ConfigError(f"{where}.mhz: {mhz:g} is not one of the schedule's frequencies")
    return tuple(sorted(out.items()))


def _atc(data: dict, base: Path) -> dict:
    """[atc] -> Config fields (#233). No section: on, data/atc/rules.json, the defaults."""
    a = _take(data.get("atc", {}), "atc", {"enabled", "rules", "context", "context_s", "dedupe_min", "expire_min",
                                         "max_per_hour", "alert_level_incident", "airport_names"})
    on = a.get("enabled", True)
    if not isinstance(on, bool):
        raise ConfigError("atc.enabled: true or false")
    rules = str(a.get("rules", "") or "")
    if rules:
        rp = Path(rules) if Path(rules).is_absolute() else base / rules
        if not rp.is_file():
            rp = Path(__file__).resolve().parent.parent / rules
        if not rp.is_file():
            raise ConfigError(f"atc.rules: {rules} not found")
        rules = str(rp.resolve())
    nums = {}
    for k, d, lo, hi in (("context", 4, 0, 12), ("context_s", 120, 10, 900), ("dedupe_min", 5, 0.5, 60),
                         ("expire_min", 30, 1, 240), ("max_per_hour", 8, 1, 60)):
        v = a.get(k, d)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= float(v) <= hi:
            raise ConfigError(f"atc.{k}: a number from {lo:g} to {hi:g}")
        nums[k] = v
    lv = a.get("alert_level_incident", [2, 3])
    if not isinstance(lv, list) or any(isinstance(x, bool) or x not in (2, 3) for x in lv):
        raise ConfigError("atc.alert_level_incident: a list of alert levels 2 and 3, e.g. [2, 3] (the default); [] = pre-alerts "
                          "only. Never 1: an Alert 1 is a standby pre-alert, never an incident")
    names = a.get("airport_names", ["westchester", "west chester", "hpn", "white plains"])
    if not isinstance(names, list) or not names or any(not isinstance(x, str) or not x.strip() for x in names):
        raise ConfigError('atc.airport_names: a list of names, e.g. ["westchester", "west chester", "hpn", "white plains"]')
    return dict(atc_enabled=on, atc_airport_names=tuple(x.lower().strip() for x in names), atc_rules=rules, atc_context=int(nums["context"]), atc_context_s=float(nums["context_s"]),
                atc_dedupe_min=float(nums["dedupe_min"]), atc_expire_min=float(nums["expire_min"]),
                atc_max_per_hour=int(nums["max_per_hour"]), atc_alert_level_incident=tuple(sorted(set(lv))))


def _job_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _auto_assign(radio: dict, channels: list) -> dict:
    """[radio] auto_assign (default on) and auto_assign_order: the jobs a newly seen dongle gets, first
    one no other dongle has. Entries name a channel by its label (any case and spacing: "fg1", "south2")
    or the P25 system ("p25" or "Fire/EMS 15"; skipped while there's no [p25] recorder to run)."""
    on = radio.get("auto_assign", True)
    if not isinstance(on, bool):
        raise ConfigError("radio.auto_assign: true or false")
    if "auto_assign_order" not in radio:
        return dict(sdr_auto_assign=on, sdr_auto_order=AUTO_ORDER)
    raw = radio["auto_assign_order"]
    if not isinstance(raw, list) or not all(isinstance(x, str) and x.strip() for x in raw):
        raise ConfigError('radio.auto_assign_order: a list of jobs, e.g. ["fg1", "p25", "south2"]')
    known = {_job_key(c.label): c.label for c in channels} | {"p25": P25_JOB, _job_key(P25_JOB): P25_JOB}
    order: list[str] = []
    for x in raw:
        job = known.get(_job_key(x))
        if job is None:
            raise ConfigError(f"radio.auto_assign_order: {x!r} is not a channel label here or \"p25\" "
                              f"(one of {', '.join(sorted({*(c.label for c in channels), 'p25'}))})")
        if job not in order:
            order.append(job)
    return dict(sdr_auto_assign=on, sdr_auto_order=tuple(order))


def _roads(data: dict) -> dict:
    """[roads] -> Config fields. No section: the Thruway's and the county's feeds on, 511NY's signs on and its
    events and CARS-Hub waiting for the username and password (the environment only, never a file), our area, the
    default schedule (511NY: every 5 minutes at most)."""
    raw = data.get("roads", {})
    if isinstance(raw, dict):
        for gone, why in (("key", "511NY moved to CARS on 2026-09-30 and gives a username and password instead: env "
                                  "SIZEUP_511NY_USER / SIZEUP_511NY_PASS (the hub: SSM /sizeup-hub/511ny-user, -pass)"),
                          ("cameras_url", "511NY's cameras now come from the CARS-Hub's CCTV feed: [roads] ny511_hub"),
                          ("carshub_url", "the CARS-Hub's base address: [roads] ny511_hub")):
            if gone in raw:
                raise ConfigError(f"roads.{gone}: no longer used ({why})")
    rd = _take(raw, "roads", {"enabled", "ny511", "bbox", "cameras_every_min", "events_every_s", "signs_every_min",
                              "events_url", "signs_url", "ny511_hub", "carshub_imagery", "thruway", "county",
                              "stream_max", "wall_streams", "dvr", "dvr_window_min", "dvr_max_gb", "dvr_near_m",
                              "dvr_linger_min", "dvr_max_cameras", "dvr_always", "dvr_dir", "dvr_shared", "dvr_area",
                              "dvr_shared_window_min", "dvr_shared_max_gb", "dvr_shared_max_cameras",
                              "dvr_extra_m"})
    flags = {k: rd.get(k, d) for k, d in (("enabled", True), ("ny511", True), ("thruway", True), ("county", True),
                                          ("carshub_imagery", True))}
    if not all(isinstance(x, bool) for x in flags.values()):
        raise ConfigError("roads.enabled / ny511 / thruway / county / carshub_imagery: true or false")
    user, password = _env("SIZEUP_511NY_USER", ""), os.environ.get("SIZEUP_511NY_PASS", "")
    if bool(user) != bool(password):
        raise ConfigError("SIZEUP_511NY_USER and SIZEUP_511NY_PASS: both (511NY's username and password), or neither")
    bbox = rd.get("bbox", [-73.80, 40.92, -73.56, 41.10])
    try:
        w, s, e, n = (float(x) for x in bbox)
    except (TypeError, ValueError):
        raise ConfigError("roads.bbox: [west, south, east, north] in degrees") from None
    if not (-180 <= w < e <= 180 and -90 <= s < n <= 90) or e - w > 3 or n - s > 3:
        raise ConfigError("roads.bbox: [west, south, east, north], west < east and south < north, at most 3 degrees a side")
    try:
        cams, events, signs = (float(rd.get("cameras_every_min", 60)) * 60, float(rd.get("events_every_s", 300)),
                               float(rd.get("signs_every_min", 5)) * 60)
    except (TypeError, ValueError):
        raise ConfigError("roads.cameras_every_min / events_every_s / signs_every_min: numbers") from None
    # 511NY recommends polling once every 5 minutes: never more often.
    if not (cams >= 600 and events >= 300 and signs >= 300):
        raise ConfigError("roads: cameras_every_min at least 10, events_every_s at least 300, signs_every_min at least 5 "
                          "(511NY: once every 5 minutes)")
    smax, wall = rd.get("stream_max", 8), rd.get("wall_streams", 0)
    if not (isinstance(smax, int) and not isinstance(smax, bool) and 1 <= smax <= 64):
        raise ConfigError("roads.stream_max: live camera streams at once through the hub, 1 to 64")
    if not (isinstance(wall, int) and not isinstance(wall, bool) and 0 <= wall <= 64):
        raise ConfigError("roads.wall_streams: the camera wall's live tiles per page, 0 (every camera in our area that streams) to 64")
    dvr = rd.get("dvr", True)
    if not isinstance(dvr, bool):
        raise ConfigError("roads.dvr: true or false")
    def num(key, default, lo, hi, what):
        v = rd.get(key, default)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
            raise ConfigError(f"roads.{key}: {what}, {lo} to {hi}")
        return v
    dvr_window = num("dvr_window_min", 120, 10, 720, "minutes kept per camera") * 60.0
    dvr_gb = num("dvr_max_gb", 6, 0.5, 200, "gigabytes for every camera's recording together")
    dvr_near = num("dvr_near_m", 1000, 0, 5000, "metres from an open event")
    dvr_linger = num("dvr_linger_min", 30, 0, 240, "minutes an opened camera records on") * 60.0
    dvr_max = num("dvr_max_cameras", 12, 1, 40, "cameras recorded at once")
    always = rd.get("dvr_always", [])
    if not (isinstance(always, list) and all(isinstance(x, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", x) for x in always)):
        raise ConfigError("roads.dvr_always: a list of camera ids")
    dvr_dir = rd.get("dvr_dir", "")
    if not isinstance(dvr_dir, str):
        raise ConfigError("roads.dvr_dir: a folder (\"\" = the state folder's dvr/)")
    shared = rd.get("dvr_shared", "auto")
    shared = {True: "on", False: "off"}.get(shared, shared) if isinstance(shared, bool) else shared
    if shared not in ("auto", "on", "off"):
        raise ConfigError('roads.dvr_shared: "auto" (the mount says: EFS / NFS), true or false')
    sh_window = num("dvr_shared_window_min", 720, 10, 2880, "minutes kept per camera on shared storage") * 60.0
    sh_gb = num("dvr_shared_max_gb", 25, 0.5, 2000, "gigabytes for every camera's recording on shared storage")
    sh_max = num("dvr_shared_max_cameras", 20, 1, 64, "cameras recorded at once on shared storage")
    extra = num("dvr_extra_m", 1000, 0, 5000, "metres beyond the camera wall's margin still recorded on shared storage")
    area = _dvr_area(rd.get("dvr_area", None))
    urls = {}
    for k in ("events_url", "signs_url", "ny511_hub"):
        u = str(rd.get(k, "")).strip()
        if u == "off":
            urls[f"roads_{k}"] = u
            continue
        m = re.fullmatch(r"(https?)://([^/:?#\s@]+)(:\d+)?([^\s]*)", u) if u else None
        if u and (not m or "@" in u.split("/")[2] or (m.group(1) == "http" and m.group(2) not in ("127.0.0.1", "localhost", "::1"))):
            raise ConfigError(f'roads.{k}: an https:// address (plain http only to this machine; no user:password@), or "off"')
        urls[f"roads_{k}"] = u
    return dict(roads_enabled=flags["enabled"], roads_ny511=flags["ny511"], roads_511ny_user=user, roads_511ny_pass=password,
                roads_bbox=(w, s, e, n), roads_cameras_s=cams, roads_events_s=events, roads_signs_s=signs,
                roads_thruway=flags["thruway"], roads_county=flags["county"], roads_carshub_imagery=flags["carshub_imagery"],
                roads_stream_max=smax, roads_wall_streams=wall, roads_dvr=dvr, roads_dvr_window_s=float(dvr_window),
                roads_dvr_max_bytes=int(dvr_gb * 1024 ** 3), roads_dvr_near_m=float(dvr_near),
                roads_dvr_linger_s=float(dvr_linger), roads_dvr_max_cameras=int(dvr_max), roads_dvr_always=tuple(always),
                roads_dvr_dir=dvr_dir.strip(), roads_dvr_shared=shared, roads_dvr_area=area, roads_dvr_extra_m=float(extra),
                roads_dvr_shared_window_s=float(sh_window), roads_dvr_shared_max_bytes=int(sh_gb * 1024 ** 3),
                roads_dvr_shared_max_cameras=int(sh_max), **urls)


def _dvr_area(raw) -> tuple:
    """[roads] dvr_area: [west, south, east, north], or a polygon [[lon, lat], ...] (3 to 200 points), or [] for none;
    not given: None (the camera wall's area, Settings > System, governs it). A ring of (lon, lat)."""
    if raw is None:
        return None
    bad = ConfigError("roads.dvr_area: [west, south, east, north] or [[lon, lat], ...] (3 to 200 points) in our "
                      "part of the world, or [] for none")
    if not isinstance(raw, list):
        raise bad
    if not raw:
        return ()
    num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)    # noqa: E731
    if len(raw) == 4 and all(num(v) for v in raw):
        w, s, e, n = (float(v) for v in raw)
        if not (w < e and s < n):
            raise bad
        pts = ((w, s), (e, s), (e, n), (w, n))
    elif 3 <= len(raw) <= 200 and all(isinstance(p, list) and len(p) == 2 and all(num(v) for v in p) for p in raw):
        pts = tuple((float(p[0]), float(p[1])) for p in raw)
    else:
        raise bad
    if not all(-180 <= x <= 180 and -90 <= y <= 90 for x, y in pts) or max(x for x, _ in pts) - min(x for x, _ in pts) > 3 \
            or max(y for _, y in pts) - min(y for _, y in pts) > 3:
        raise bad
    return pts


def _roadwork(data: dict) -> dict:
    """[roadwork] -> Config fields. No section: on, every public source; WZDx off (no address)."""
    rw = _take(data.get("roadwork", {}), "roadwork", {"enabled", "projects", "notices", "wzdx", "wzdx_url"})
    flags = {k: rw.get(k, True) for k in ("enabled", "projects", "notices", "wzdx")}
    if not all(isinstance(v, bool) for v in flags.values()):
        raise ConfigError("roadwork.enabled / roadwork.projects / roadwork.notices / roadwork.wzdx: true or false")
    u = str(rw.get("wzdx_url", "")).strip()
    m = re.fullmatch(r"(https?)://([^/:?#\s]+)(:\d+)?([^\s]*)", u) if u else None
    if u and (not m or (m.group(1) == "http" and m.group(2) not in ("127.0.0.1", "localhost", "::1"))):
        raise ConfigError("roadwork.wzdx_url: another WZDx feed's https:// address (plain http only to this machine)")
    if u and (re.search(r"(key|token)=", u, re.I) or "{key}" in u):
        raise ConfigError("roadwork.wzdx_url: another public WZDx feed's address, never a key in it "
                          "(511NY's own WZDx: [roadwork] wzdx = true, with [roads]' username and password)")
    return dict(roadwork_enabled=flags["enabled"], roadwork_projects=flags["projects"], roadwork_notices=flags["notices"],
                roadwork_wzdx=flags["wzdx"], roadwork_wzdx_url=u)


def _geo(data: dict) -> dict:
    """[geo] search_towns (the intersection search's towns, #213), hq_lat / hq_lon (results nearest this first)."""
    g = _take(data.get("geo", {}), "geo", {"search_towns", "hq_lat", "hq_lon"})
    out: dict = {}
    if "search_towns" in g:
        towns = g["search_towns"]
        if not isinstance(towns, list) or not towns or not all(isinstance(t, str) and t.strip() for t in towns):
            raise ConfigError('geo.search_towns: a list of town names ("Port Chester", "Rye Brook", ...)')
        out["geo_search_towns"] = tuple(dict.fromkeys(t.strip() for t in towns))
    if "hq_lat" in g or "hq_lon" in g:
        try:
            lat, lon = float(g["hq_lat"]), float(g["hq_lon"])
        except (KeyError, TypeError, ValueError):
            raise ConfigError("geo.hq_lat and geo.hq_lon: both, in degrees") from None
        if not (40.0 <= lat <= 42.0 and -75.0 <= lon <= -72.0):
            raise ConfigError("geo.hq_lat / hq_lon: in the area")
        out["geo_hq"] = (lat, lon)
    return out


def _trains(data: dict) -> dict:
    """[trains] -> Config fields. No section: on, the MTA's feeds, our stretch of the line (Mount Vernon East to Stamford)."""
    tr = _take(data.get("trains", {}), "trains", {"enabled", "key", "rt_url", "gtfs_url", "bbox", "rt_every_s",
                                                        "traintime", "traintime_url", "traintime_every_s"})
    enabled = tr.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError("trains.enabled: true or false")
    key = _env("SIZEUP_MTA_KEY", str(tr.get("key", ""))).strip()
    if key and not re.fullmatch(r"[!-~]{8,256}", key):
        raise ConfigError("trains.key / SIZEUP_MTA_KEY: the MTA developer key (8 or more characters, no spaces)")
    urls = {}
    for k in ("rt_url", "gtfs_url", "traintime_url"):
        u = str(tr.get(k, "")).strip()
        m = re.fullmatch(r"(https?)://([^/:?#\s]+)(:\d+)?([^\s]*)", u) if u else None
        if u and (not m or (m.group(1) == "http" and m.group(2) not in ("127.0.0.1", "localhost", "::1"))):
            raise ConfigError(f"trains.{k}: an https:// address (plain http only to this machine); leave it out for the MTA's")
        urls[f"trains_{k}"] = u
    bbox = _bbox(tr.get("bbox", [-73.845, 40.900, -73.530, 41.060]), "trains.bbox", 1.0)
    every = tr.get("rt_every_s", 5.0)
    if isinstance(every, bool) or not isinstance(every, (int, float)) or not 5 <= every <= 300:
        raise ConfigError("trains.rt_every_s: seconds between reads of the MTA's realtime feed while someone looks, 5 to 300")
    tt = tr.get("traintime", True)
    if not isinstance(tt, bool):
        raise ConfigError("trains.traintime: true or false")
    tt_every = tr.get("traintime_every_s", 5.0)
    if isinstance(tt_every, bool) or not isinstance(tt_every, (int, float)) or not 5 <= tt_every <= 60:
        raise ConfigError("trains.traintime_every_s: seconds between reads of MTA TrainTime while the layer is on or an "
                          "incident is near the line, 5 to 60")
    return dict(trains_enabled=enabled, trains_key=key, trains_bbox=bbox, trains_rt_every_s=float(every),
                trains_traintime=tt, trains_traintime_every_s=float(tt_every), **urls)


def _vessels(data: dict, base: Path, sdrs: tuple[str, ...], adsb_sdr: str = "") -> dict:
    """[vessels] and [vessels.receiver] -> Config fields. No section: on, but with no source (no aisstream.io key, no
    receiver) it shows nothing. The key comes from env SIZEUP_AISSTREAM_KEY first (the hub: SSM); never logged."""
    vs = _take(data.get("vessels", {}), "vessels", {"enabled", "aisstream_key", "bbox", "receiver"})
    if not isinstance(vs.get("receiver", {}), dict):
        raise ConfigError("vessels.receiver: a table ([vessels.receiver])")
    rc = _take(vs.get("receiver", {}), "vessels.receiver", {"enabled", "sdr", "gain", "ppm", "json"})
    enabled, receiver = vs.get("enabled", True), rc.get("enabled", False)
    if not isinstance(enabled, bool) or not isinstance(receiver, bool):
        raise ConfigError("vessels.enabled / vessels.receiver.enabled: true or false")
    key = _env("SIZEUP_AISSTREAM_KEY", str(vs.get("aisstream_key", ""))).strip()
    if key and not re.fullmatch(r"[!-~]{16,256}", key):
        raise ConfigError("vessels.aisstream_key / SIZEUP_AISSTREAM_KEY: aisstream.io's API key (16 or more characters, "
                          "no spaces)")
    bbox = _bbox(vs.get("bbox", [-73.83, 40.78, -73.50, 41.07]), "vessels.bbox", 2.0)
    from sizeup.radio.adsbd import gain_problem
    gain = rc.get("gain", "auto")
    if isinstance(gain, bool) or (why := gain_problem(gain)):
        raise ConfigError(f"vessels.receiver.gain: {why if not isinstance(gain, bool) else 'auto or a number of dB'}")
    gain = "auto" if str(gain).strip().lower() == "auto" else f"{float(gain):g}"
    ppm = rc.get("ppm", 0)
    if isinstance(ppm, bool) or not isinstance(ppm, int) or not -150 <= ppm <= 150:
        raise ConfigError("vessels.receiver.ppm: the dongle's frequency correction, a whole number -150 to 150 (0 for a TCXO)")
    sdr = str(rc.get("sdr", "")).strip()
    if sdr:
        if not re.fullmatch(r"[\w.-]{1,32}", sdr) or sdr.isdigit():
            raise ConfigError("vessels.receiver.sdr: the AIS dongle's exact USB serial (letters, digits, . _ -; not all "
                              "digits: rtl_eeprom -d <index> -s ais162)")
        if sdr in sdrs:
            raise ConfigError(f"vessels.receiver.sdr: {sdr} is one of sizeup's radios ([radio] sdrs); give AIS its own dongle")
        if sdr == adsb_sdr:
            raise ConfigError(f"vessels.receiver.sdr: {sdr} is the ADS-B dongle ([aircraft.receiver] sdr); give AIS its own")
    js = str(rc.get("json", "")).strip()
    if js and not Path(js).is_absolute():
        js = str((base / js).resolve())
    return dict(vessels_enabled=enabled, vessels_aisstream_key=key, vessels_bbox=bbox, vessels_receiver=receiver,
                vessels_sdr=sdr, vessels_gain=gain, vessels_ppm=ppm, vessels_receiver_json=js)


def _bbox(v, what: str, max_deg: float = 3.0) -> tuple[float, float, float, float]:
    try:
        w, s, e, n = (float(x) for x in v)
    except (TypeError, ValueError):
        raise ConfigError(f"{what}: [west, south, east, north] in degrees") from None
    if not (-180 <= w < e <= 180 and -90 <= s < n <= 90) or e - w > max_deg or n - s > max_deg:
        raise ConfigError(f"{what}: [west, south, east, north], west < east and south < north, at most {max_deg:g} degrees a side")
    return (w, s, e, n)


AIRCRAFT_FEEDS = ("receiver", "adsblol", "adsbfi")


def _aircraft(data: dict, base: Path, sdrs: tuple[str, ...]) -> dict:
    """[aircraft] and [aircraft.receiver] -> Config fields. No section: on, adsb.lol + adsb.fi, 15 nm around HPN, the
    emergency alarm on, no receiver. An enabled receiver is always a feed, and its positions come first.
    The receiver's sdr (a dongle for ADS-B) is never one of sizeup's own radios ([radio] sdrs / shared_device)."""
    ac = _take(data.get("aircraft", {}), "aircraft", {"enabled", "feeds", "radius_nm", "ceiling_ft", "adsblol_url",
                                                      "adsbfi_url", "registry", "receiver", "alarms"})
    if not isinstance(ac.get("receiver", {}), dict):
        raise ConfigError("aircraft.receiver: a table ([aircraft.receiver])")
    rc = _take(ac.get("receiver", {}), "aircraft.receiver", {"enabled", "sdr", "json_1090", "json_978", "gain", "bias_tee"})
    enabled, registry, receiver = ac.get("enabled", True), ac.get("registry", True), rc.get("enabled", False)
    alarms, bias_tee = ac.get("alarms", True), rc.get("bias_tee", False)
    if not all(isinstance(x, bool) for x in (enabled, registry, receiver, alarms, bias_tee)):
        raise ConfigError("aircraft.enabled / registry / alarms / aircraft.receiver.enabled / bias_tee: true or false")
    from sizeup.radio.adsbd import gain_problem
    gain = rc.get("gain", "auto")
    if isinstance(gain, bool) or (why := gain_problem(gain)):
        raise ConfigError(f"aircraft.receiver.gain: {why if not isinstance(gain, bool) else 'auto or a number of dB'}")
    gain = "auto" if str(gain).strip().lower() == "auto" else f"{float(gain):g}"
    feeds = ac.get("feeds", ["adsblol", "adsbfi"])
    if not (isinstance(feeds, list) and feeds and all(f in AIRCRAFT_FEEDS for f in feeds) and len(set(feeds)) == len(feeds)):
        raise ConfigError('aircraft.feeds: from "receiver", "adsblol", "adsbfi" (each once; the APIs in the order to '
                          'prefer on a tie)')
    try:
        radius, ceiling = float(ac.get("radius_nm", 15)), float(ac.get("ceiling_ft", 6000))
    except (TypeError, ValueError):
        raise ConfigError("aircraft.radius_nm / aircraft.ceiling_ft: numbers") from None
    if not (3 <= radius <= 40 and 1000 <= ceiling <= 18000):
        raise ConfigError("aircraft.radius_nm: 3 to 40 (around HPN); aircraft.ceiling_ft: 1000 to 18000 (MSL)")
    urls = {}
    for k in ("adsblol_url", "adsbfi_url"):
        u = str(ac.get(k, "")).strip()
        m = re.fullmatch(r"(https?)://([^/:?#\s]+)(:\d+)?([^\s]*)", u) if u else None
        if u and (not m or (m.group(1) == "http" and m.group(2) not in ("127.0.0.1", "localhost", "::1"))
                  or not all(f"{{{x}}}" in u for x in ("lat", "lon", "radius"))):
            raise ConfigError(f"aircraft.{k}: an https:// address with {{lat}}, {{lon}} and {{radius}} (plain http only to "
                              "this machine); leave it out for the public API")
        urls[f"aircraft_{k}"] = u
    srcs = {}
    for k in ("json_1090", "json_978"):
        u = str(rc.get(k, "")).strip()
        if u and re.match(r"^[a-z][a-z0-9+.-]*://", u, re.I) and not re.fullmatch(r"https?://[^\s]+", u, re.I):
            raise ConfigError(f"aircraft.receiver.{k}: the decoder's aircraft.json: a file path or an http(s):// address")
        srcs[f"aircraft_receiver_{k.split('_')[1]}"] = (u if not u or "://" in u or Path(u).is_absolute()
                                                         else str((base / u).resolve()))
    sdr = str(rc.get("sdr", "")).strip()
    if sdr and receiver and not any(srcs.values()):
        from sizeup.radio.adsbd import AIRCRAFT_JSON
        srcs["aircraft_receiver_1090"] = AIRCRAFT_JSON      # the named dongle's decoder (sizeup-adsb.service) writes it
    if ("receiver" in feeds or receiver) and not (receiver and any(srcs.values())):
        raise ConfigError('aircraft.feeds: "receiver" needs [aircraft.receiver] enabled = true and json_1090 or json_978')
    if receiver and "receiver" not in feeds:
        feeds = ["receiver", *feeds]      # user, 2026-10-14: "primary local SDR on ADSB, secondary the above sources"
    if sdr:
        if not re.fullmatch(r"[\w.-]{1,32}", sdr):
            raise ConfigError("aircraft.receiver.sdr: the ADS-B dongle's exact USB serial (letters, digits, . _ -)")
        if sdr.isdigit():
            raise ConfigError("aircraft.receiver.sdr: an all-digit serial is read as a device index by readsb and rtl "
                              "tools; give the dongle a name first (rtl_eeprom -d <index> -s adsb1090)")
        if sdr in sdrs:
            raise ConfigError(f"aircraft.receiver.sdr: {sdr} is one of sizeup's own radios ([radio] sdrs / shared_device)")
    return dict(aircraft_enabled=enabled, aircraft_feeds=tuple(feeds), aircraft_alarms=alarms, aircraft_radius_nm=radius,
                aircraft_ceiling_ft=ceiling,
                aircraft_registry=registry, aircraft_receiver=receiver, aircraft_sdr=sdr, aircraft_gain=gain,
                aircraft_bias_tee=bias_tee, **urls, **srcs)


def _weather(data: dict) -> dict:
    """[weather] -> Config fields (sizeup/weather.py). No section: our zones, KHPN, Rye Beach and Kings Point."""
    w = _take(data.get("weather", {}), "weather", {"enabled", "radar", "zones", "marine_zones", "area", "station",
                                                   "tide_station", "tide_name", "gauge_station", "gauge_name"})
    enabled, radar = w.get("enabled", True), w.get("radar", True)
    if not isinstance(enabled, bool) or not isinstance(radar, bool):
        raise ConfigError("weather.enabled / weather.radar: true or false")
    out = {}
    for k, dflt in (("zones", ["NYZ071", "NYC119", "NYZ211"]), ("marine_zones", ["ANZ335"])):
        v = w.get(k, dflt)
        if not isinstance(v, list) or not all(isinstance(z, str) and re.fullmatch(r"[A-Z]{2}[CZ]\d{3}", z) for z in v):
            raise ConfigError(f'weather.{k}: NWS zone codes, e.g. ["NYZ071", "NYC119"]')
        out[k] = tuple(v)
    if not out["zones"]:
        raise ConfigError("weather.zones: at least one NWS zone")
    station = str(w.get("station", "KHPN")).strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{3,5}", station):
        raise ConfigError('weather.station: an NWS observation station id, e.g. "KHPN"')
    tides = []
    for k, dflt, name in (("tide_station", "8518091", "Rye Beach"), ("gauge_station", "8516945", "Kings Point")):
        sid = str(w.get(k, dflt)).strip()
        if not re.fullmatch(r"\d{7}", sid):
            raise ConfigError(f"weather.{k}: a NOAA CO-OPS station id (7 digits)")
        tides.append((sid, str(w.get(k.replace("station", "name"), name if sid == dflt else sid))[:60]))
    return dict(weather_enabled=enabled, weather_radar=radar, weather_zones=out["zones"], weather_marine_zones=out["marine_zones"],
                weather_area=str(w.get("area", "Southern Westchester"))[:60], weather_station=station,
                weather_tide_station=tides[0], weather_gauge_station=tides[1])


GIS_LAYER_KEYS = {"key", "label", "url", "group", "kind", "style", "minzoom", "fields", "refresh", "bbox", "where", "on",
                  "tiled", "simplify_m", "licence", "page", "enabled", "asof", "ours_field", "drop_ours", "note", "icon", "sub",
                  "trains_field"}


ICON_RE = re.compile(r"[a-z][a-z0-9-]{0,31}")


def _gis_layer(raw: dict, i: int, base=None):
    """One [[gis.layers]] entry -> a sizeup.gis.Layer. base: the starter entry with the same key, whose
    values it overrides (so one line, `on = true`, changes one thing)."""
    from sizeup.gis import KEY_RE, KINDS, REFRESH_S, URL_RE, Layer
    where = f"gis.layers[{i}]"
    _take(raw, where, GIS_LAYER_KEYS)
    d = {k: getattr(base, k) for k in GIS_LAYER_KEYS} if base is not None else {}
    d.update({k: v for k, v in raw.items()})
    key = str(d.get("key", ""))
    if not KEY_RE.fullmatch(key):
        raise ConfigError(f"{where}.key: a short name, lower-case letters, digits and _ (\"parcels\")")
    where = f"gis.layers[{key}]"
    url = str(d.get("url", "")).strip().rstrip("/")
    if not URL_RE.fullmatch(url):
        raise ConfigError(f"{where}.url: the ArcGIS REST layer, https://.../MapServer/<n> or .../FeatureServer/<n>")
    label = str(d.get("label", "")).strip()
    if not 1 <= len(label) <= 60:
        raise ConfigError(f"{where}.label: the name on the Layers panel (1 to 60 characters)")
    kind = str(d.get("kind", ""))
    if kind not in ("", *KINDS):
        raise ConfigError(f"{where}.kind: point, line or polygon (or leave it out: the service says)")
    refresh = str(d.get("refresh", "daily"))
    if refresh not in REFRESH_S:
        raise ConfigError(f"{where}.refresh: {', '.join(REFRESH_S)}")
    style = d.get("style", {})
    if not isinstance(style, dict) or not all(isinstance(v, (str, int, float)) and not isinstance(v, bool) for v in style.values()):
        raise ConfigError(f"{where}.style: a table of colors (design tokens \"--info\" or \"#hex\") and numbers")
    fields = []
    for f in d.get("fields", ()):
        if isinstance(f, str) and f.strip():
            fields.append((f.strip(), f.strip().replace("_", " ").title()))
        elif isinstance(f, (list, tuple)) and len(f) == 2 and all(isinstance(x, str) and x.strip() for x in f):
            fields.append((f[0].strip(), f[1].strip()))
        else:
            raise ConfigError(f"{where}.fields: a list of the service's field names, each \"FIELD\" or [\"FIELD\", \"Label shown\"]")
    try:
        minzoom, simplify_m = int(d.get("minzoom", 0)), float(d.get("simplify_m", 0))
    except (TypeError, ValueError):
        raise ConfigError(f"{where}.minzoom: a zoom level; simplify_m: metres") from None
    if not 0 <= minzoom <= 22 or not 0 <= simplify_m <= 50:
        raise ConfigError(f"{where}.minzoom: 0 to 22; simplify_m: 0 to 50 metres")
    for k in ("on", "tiled", "enabled"):
        if not isinstance(d.get(k, False), bool):
            raise ConfigError(f"{where}.{k}: true or false")
    bbox = _bbox(d["bbox"], f"{where}.bbox") if d.get("bbox") else None
    text = {k: str(d.get(k, "")).strip() for k in ("group", "where", "licence", "page", "asof", "ours_field", "note", "icon", "sub",
                                          "trains_field")}
    if not text["group"]:
        text["group"] = "County GIS"
    if text["icon"] and not ICON_RE.fullmatch(text["icon"]):
        raise ConfigError(f"{where}.icon: a mark from static/icons.svg (\"hydrant\", \"fire-station\", ... : i-gis-<icon>)")
    return Layer(key=key, label=label, url=url, group=text["group"], kind=kind, style=dict(style), minzoom=minzoom,
                 fields=tuple(fields), refresh=refresh, bbox=bbox, where=text["where"], on=bool(d.get("on", False)),
                 tiled=bool(d.get("tiled", False)), simplify_m=simplify_m, licence=text["licence"], page=text["page"],
                 enabled=bool(d.get("enabled", True)), asof=text["asof"], ours_field=text["ours_field"],
                 drop_ours=bool(d.get("drop_ours", False)), note=text["note"], icon=text["icon"], sub=text["sub"],
                 trains_field=text["trains_field"])


def _gis(data: dict) -> dict:
    """[gis] -> Config fields (sizeup/gis.py). No section: the starter set. A [[gis.layers]] entry with a
    starter key overrides that entry's values (enabled = false drops it); a new key adds a dataset."""
    from sizeup.gis import DEFAULT_LAYERS
    g = _take(data.get("gis", {}), "gis", {"enabled", "bbox", "layers"})
    enabled = g.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError("gis.enabled: true or false")
    bbox = _bbox(g.get("bbox", [-73.80, 40.92, -73.56, 41.10]), "gis.bbox")
    layers = {l.key: l for l in DEFAULT_LAYERS}
    raw = g.get("layers", [])
    if not isinstance(raw, list) or not all(isinstance(x, dict) for x in raw):
        raise ConfigError("gis.layers: [[gis.layers]] tables, one per dataset")
    for i, entry in enumerate(raw):
        key = str(entry.get("key", ""))
        layers[key] = _gis_layer(entry, i, layers.get(key))
    return dict(gis_enabled=enabled, gis_bbox=bbox, gis_layers=tuple(l for l in layers.values() if l.enabled))


def _routing(data: dict) -> dict:
    """[routing] -> Config fields (sizeup/roadgraph.py, #360). No section: on, a 12 km ring, overpass-api.de."""
    r = _take(data.get("routing", {}), "routing", {"enabled", "ring_km", "overpass_url"})
    enabled, ring, url = r.get("enabled", True), r.get("ring_km", 12.0), r.get("overpass_url", "")
    if not isinstance(enabled, bool):
        raise ConfigError("routing.enabled: true or false")
    if isinstance(ring, bool) or not isinstance(ring, (int, float)) or not 2 <= ring <= 40:
        raise ConfigError("routing.ring_km: 2 to 40 (km around the firehouses)")
    if not isinstance(url, str) or (url and not url.startswith("https://")):
        raise ConfigError("routing.overpass_url: an https:// address (or leave it out)")
    return dict(routing_enabled=enabled, routing_ring_km=float(ring), routing_overpass_url=url)


def _stt_aws(stt: dict) -> dict:
    """[stt] aws*: Amazon Transcribe (sizeup/transcribe.py). aws_channels: labels or talkgroup numbers. Also
    record_only (labels or talkgroups): recorded, not transcribed unless Settings > Radios says so (#201)."""
    try:
        idle, pace = float(stt.get("aws_idle_s", 120)), float(stt.get("aws_pace", 2.0))
        limit = float(stt.get("aws_monthly_limit_usd", 25))
    except (TypeError, ValueError):
        raise ConfigError("stt.aws_idle_s, aws_pace, aws_monthly_limit_usd: numbers") from None
    if not 1 <= idle <= 3600 or not 0.5 <= pace <= 10 or not 0 <= limit <= 10000:
        raise ConfigError("stt.aws_idle_s: 1 to 3600; aws_pace: 0.5 to 10; aws_monthly_limit_usd: 0 to 10000")
    chans = stt.get("aws_channels", ["South 2", "FG1", "FG2", 307])
    if not isinstance(chans, list):
        raise ConfigError('stt.aws_channels: a list of channel labels and talkgroups, e.g. ["South 2", "FG1", 307]')
    names = tuple(f"tg:{c}" if isinstance(c, int) else str(c) for c in chans)
    ro = stt.get("record_only", [])
    if not isinstance(ro, list) or not all(isinstance(c, (int, str)) and not isinstance(c, bool) for c in ro):
        raise ConfigError('stt.record_only: a list of channel labels and talkgroups, e.g. [383, 3127]')
    record_only = tuple(f"tg:{c}" if isinstance(c, int) else str(c) for c in ro)
    vocab = str(stt.get("aws_vocabulary", "sizeup-pcfd"))
    if not re.fullmatch(r"[0-9a-zA-Z._-]{1,200}", vocab):
        raise ConfigError("stt.aws_vocabulary: letters, digits, . _ -")
    return dict(stt_aws=bool(stt.get("aws", False)), stt_aws_region=str(stt.get("aws_region", "")),
                stt_aws_endpoint=str(stt.get("aws_endpoint", "")).rstrip("/"),
                stt_aws_api_endpoint=str(stt.get("aws_api_endpoint", "")).rstrip("/"),
                stt_aws_idle_s=idle, stt_aws_pace=pace, stt_aws_limit_usd=limit, stt_aws_channels=names,
                stt_aws_vocabulary=vocab, stt_record_only=record_only)


# [adapter] (#301): the station's incident-source adapter, read only by the station's updater
# (sizeup/cloud/autoupdate.py, written by sudo sizeup-station adapter setup): never its credentials (station.env).
# configupdate.py looks for ADAPTER_KEYS here to know a release reads the table.
ADAPTER_KEYS = {"repo", "ref", "env"}


def _adapter(data: dict) -> tuple:
    a = _take(data.get("adapter", {}), "adapter", ADAPTER_KEYS)
    if not isinstance(a.get("repo", ""), str) or not isinstance(a.get("ref", ""), str):
        raise ConfigError("adapter.repo / adapter.ref: text (sudo sizeup-station adapter setup)")
    env = a.get("env", [])
    if not isinstance(env, list) or not all(isinstance(x, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", x) for x in env):
        raise ConfigError('adapter.env: a list of variable names, e.g. ["FEED_USER", "FEED_PASS"]')
    return tuple(env)


def rel_dir(base: Path, p: str) -> str:
    return str((base / p).resolve()) if p and not Path(p).is_absolute() else p


def load(path: str | Path) -> Config:
    path = Path(path)
    base = path.parent
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from e
    _take(data, "config", {"station", "web", "radio", "tones", "stt", "p25", "mdc", "units", "storage", "map",
                           "cad", "dispatch", "cloud", "prealert", "roads", "roadwork", "gis", "trains", "weather", "alerts",
                           "aircraft", "geo", "atc", "adapter", "routing", "vessels"})
    adapter_env = _adapter(data)
    st = _take(data.get("station", {}), "station", {"id", "name", "auto_update", "lat", "lon"})
    if not isinstance(st.get("auto_update", True), bool):
        raise ConfigError("station.auto_update: true or false")
    station_ll = STATION_LL
    if "lat" in st or "lon" in st:
        try:
            station_ll = (float(st["lat"]), float(st["lon"]))
        except (KeyError, TypeError, ValueError):
            raise ConfigError("station.lat and station.lon: both, in degrees") from None
        if not (40.0 <= station_ll[0] <= 42.0 and -75.0 <= station_ll[1] <= -72.0):
            raise ConfigError("station.lat / lon: in the area")
    web = dict(_take(data.get("web", {}), "web", {"host", "port"}))
    web["host"] = _env("SIZEUP_HOST", web.get("host", "127.0.0.1"))
    try:
        web["port"] = int(_env("SIZEUP_PORT", web.get("port", 8090)))
    except ValueError:
        raise ConfigError("SIZEUP_PORT: a port number") from None
    cloud = _cloud(data)
    pa = _prealert(data)
    state_dir = Path(os.environ["SIZEUP_STATE_DIR"]).expanduser() if os.environ.get("SIZEUP_STATE_DIR", "").strip() else base
    radio = _take(data.get("radio", {}), "radio", {"rtl_fm", "channels", "tones_file", "shared_device", "start_mode", "sdrs", "sdr_names", "sdr_ignore",
                                                 "auto_assign", "auto_assign_order", "airband_multichannel"})
    if not isinstance(radio.get("airband_multichannel", False), bool):
        raise ConfigError("radio.airband_multichannel: true or false (#275: the channels' `also` frequencies on; off by default)")
    raw_sdrs = radio.get("sdrs", ())
    sdr_auto = raw_sdrs == "auto"
    sdrs = (() if sdr_auto else tuple(str(x) for x in raw_sdrs)) or ((str(radio["shared_device"]),) if radio.get("shared_device") else ())
    if len(set(sdrs)) != len(sdrs):
        raise ConfigError("radio.sdrs: a serial is listed twice")
    aircraft = _aircraft(data, base, sdrs + ((str(radio["shared_device"]),) if radio.get("shared_device") else ()))
    vessels = _vessels(data, base, sdrs + ((str(radio["shared_device"]),) if radio.get("shared_device") else ()),
                       aircraft.get("aircraft_sdr", ""))
    channels = []
    for i, c in enumerate(radio.get("channels", [])):
        _take(c, f"radio.channels[{i}]", {"label", "mhz", "kind", "device", "gain_db", "squelch_db",
                                                 "drop_db", "hang_s", "continue_s", "schedule", "also"})
        if c.get("kind", "traffic") not in KINDS:
            raise ConfigError(f"radio.channels[{i}].kind: 'paging', 'traffic' or 'airband'")
        kind = c.get("kind", "traffic")
        if "schedule" in c and kind != "airband":
            raise ConfigError(f"radio.channels[{i}].schedule: airband channels only (#260)")
        if "also" in c and kind != "airband":
            raise ConfigError(f"radio.channels[{i}].also: airband channels only (#275)")
        if kind == "airband":
            channels.append(_airband(c, i))
            continue
        # Simplex fireground: a weaker carrier can sit under ours (FG1 2026-10-07), so traffic channels
        # end a transmission on a 10 dB drop. Paging stays off: a distant repeater fades (South 2).
        drop = c.get("drop_db", 10.0 if kind == "traffic" else 0)
        if kind != "paging" and c.get("continue_s"):
            raise ConfigError(f"radio.channels[{i}].continue_s: paging channels only (pages are joined, traffic isn't)")
        hang = c.get("hang_s")
        if hang is not None and not 0.05 <= float(hang) <= 10:
            raise ConfigError(f"radio.channels[{i}].hang_s: 0.05 to 10 seconds")
        cont = c.get("continue_s")
        if cont is not None and not 0 <= float(cont) <= 30:
            raise ConfigError(f"radio.channels[{i}].continue_s: 0 to 30 seconds")
        channels.append(ChannelCfg(c["label"], float(c["mhz"]), kind, c.get("device", ""),
                                   c.get("gain_db", 40.0), float(c.get("squelch_db", 10.0)),
                                   float(drop) if drop else None,
                                   float(hang) if hang is not None else None,
                                   float(cont) if cont is not None else None))
    labels = [c.label for c in channels]
    if len(set(labels)) != len(labels):
        raise ConfigError("radio.channels: two channels have the same label")
    channels = _also_owners(channels)
    tones, ours, tones_file = [], set(), ""
    tf = radio.get("tones_file", "data/westchester_tones.csv")
    if tf:
        tf_path = Path(tf) if Path(tf).is_absolute() else (base / tf)
        if not tf_path.is_file():
            tf_path = Path(__file__).resolve().parent.parent / tf   # repo data/ when run from elsewhere
        if tf_path.is_file():
            tones.extend(read_tones_file(tf_path))
            tones_file = str(tf_path)
        elif "tones_file" in radio:
            raise ConfigError(f"radio.tones_file: {tf} not found")
    for i, t in enumerate(data.get("tones", [])):
        _take(t, f"tones[{i}]", {"name", "a_hz", "b_hz", "agency", "ours"})
        tones.append(ToneCode(t["name"], float(t["a_hz"]), float(t["b_hz"]) if t.get("b_hz") else None,
                              t.get("agency", ""), source="config"))
        if t.get("ours", False):
            ours.add(t["name"])
    stt = _take(data.get("stt", {}), "stt", {"server_binary", "model", "port", "threads", "prompt", "talker_prompt", "beam_size",
                                             "aws", "aws_region", "aws_endpoint", "aws_api_endpoint", "aws_idle_s", "aws_pace",
                                             "aws_monthly_limit_usd", "aws_channels", "aws_vocabulary",
                                             "record_only"})
    talker_mode = str(stt.get("talker_prompt", "off"))
    if talker_mode not in ("off", "ids", "ids+time"):
        raise ConfigError('stt.talker_prompt: "off", "ids" or "ids+time"')
    aws_stt = _stt_aws(stt)
    if "aws_channels" not in stt:     # an airband channel (HPN ATC, #233) is a priority channel: AWS by default
        aws_stt["stt_aws_channels"] = tuple(dict.fromkeys([*aws_stt["stt_aws_channels"],
                                                           *(c.label for c in channels if c.kind == "airband")]))
    p25 = _take(data.get("p25", {}), "p25", {"capture_dir", "talkgroups", "talkgroups_file", "incident_talkgroups",
                                             "aliases", "recorder", "recorder_dir", "emergency_window_min"})
    try:
        em_window = float(p25.get("emergency_window_min", 10))
    except (TypeError, ValueError):
        em_window = -1.0
    if not 0 <= em_window <= 120:
        raise ConfigError("p25.emergency_window_min: minutes from 0 to 120 (10: the default)")
    p25_tgs = {int(k): v for k, v in p25.get("talkgroups", {"307": "Fire/EMS 15"}).items()}
    p25_info: dict[int, dict] = {k: {"name": v, "group": "", "encrypted": False} for k, v in p25_tgs.items()}
    if p25.get("talkgroups_file"):  # trunk-recorder's talkgroups.csv: one list for both programs
        import csv
        tf = Path(p25["talkgroups_file"])
        tf = tf if tf.is_absolute() else base / tf
        if not tf.is_file():
            raise ConfigError(f"p25.talkgroups_file: {tf} not found")
        for r in csv.DictReader(tf.read_text().splitlines()):
            p25_tgs[int(r["Decimal"])] = r["Alpha Tag"]
            p25_info[int(r["Decimal"])] = {"name": r["Alpha Tag"], "group": r.get("Tag") or "",
                                           "encrypted": "E" in (r.get("Mode") or "").upper()}
    # More P25 systems (#409), from their directories beside trunk-recorder's config (or the talkgroups file: the hub)
    from sizeup import tiers as tiers_mod
    from sizeup.radio import systems as p25_systems_mod
    sys_base = ""
    if p25.get("recorder_dir"):
        sys_base = str(base / p25["recorder_dir"])
    elif p25.get("talkgroups_file"):
        sys_base = str((base / p25["talkgroups_file"]).parent)
    # One that can't be used (a bad file, a talkgroup number or a name of ours used again) is left out and said
    # (Config.p25_system_problems): another system never keeps the fire side from starting.
    sys_problems: list[str] = []
    ours_tgs = {*p25_tgs, 307, 333, 301, *(int(x) for x in p25.get("incident_talkgroups", [307, 333]))}
    more_systems = p25_systems_mod.discover(sys_base, ours_tgs, [c.label for c in channels] + [P25_JOB, "Off"],
                                            problems=sys_problems, county_capture=rel_dir(base, p25.get("capture_dir", "")))
    storage = _take(data.get("storage", {}), "storage", {"clips_dir", "keep_hours", "audio_keep_hours", "audio_keep_event_days",
                                                         "audio_prune_batch", "audio_offpeak", "tier_keep_days",
                                                         "context_audio_max_mb"})
    try:
        tier_days = tiers_mod.keep_days(storage.get("tier_keep_days"))
    except ValueError as e:
        raise ConfigError(f"storage.tier_keep_days: {e}") from None
    try:
        context_mb = float(storage.get("context_audio_max_mb", 100))
    except (TypeError, ValueError):
        context_mb = -1.0
    if not 0 <= context_mb <= 100000:
        raise ConfigError("storage.context_audio_max_mb: megabytes, 0 or more")
    try:
        keep_event_days = float(storage.get("audio_keep_event_days", 365))
        prune_batch = int(storage.get("audio_prune_batch", 200))
        offpeak = tuple(int(h) for h in storage.get("audio_offpeak", (2, 5)))
    except (TypeError, ValueError):
        raise ConfigError("storage: audio_keep_event_days a number, audio_prune_batch a whole number, "
                          "audio_offpeak two hours [start, end]") from None
    if keep_event_days < 0 or not 1 <= prune_batch <= 10000 or len(offpeak) != 2 or not all(0 <= h <= 24 for h in offpeak):
        raise ConfigError("storage: audio_keep_event_days >= 0 (0 = forever), audio_prune_batch 1..10000, "
                          "audio_offpeak [start, end] hours 0..24")
    mdc = _take(data.get("mdc", {}), "mdc", {"aliases"})
    units = _take(data.get("units", {}), "units", {"departments", "apparatus_file", "callsigns"})
    unit_departments: dict[str, str] = {}
    af_found = ""
    af = units.get("apparatus_file", "data/westchester_apparatus.csv")
    if af:
        af_path = Path(af) if Path(af).is_absolute() else (base / af)
        if not af_path.is_file():
            af_path = Path(__file__).resolve().parent.parent / af
        if af_path.is_file():
            import csv
            af_found = str(af_path.resolve())
            rows = (ln for ln in af_path.read_text().splitlines() if ln.strip() and not ln.startswith("#"))
            for r in csv.DictReader(rows):
                dep = HOME_DEPARTMENTS.get(r["department"], r["department"])
                unit_departments[r["unit"].strip().title()] = dep
        elif "apparatus_file" in units:
            raise ConfigError(f"units.apparatus_file: {af} not found")
    unit_departments.update({str(k).title(): v for k, v in units.get("departments", {}).items()})
    # Chief officers by callsign (dept number + rank digit: 2391 = Port Chester's chief). Department from the prefix.
    dept_numbers: dict[str, str] = {}
    df = (af_path.parent / "westchester_departments.csv") if af else None
    if df and df.is_file():
        import csv
        rows = (ln for ln in df.read_text().splitlines() if ln.strip() and not ln.startswith("#"))
        for r in csv.DictReader(rows):
            if r.get("dept_number"):
                dept_numbers[r["dept_number"].strip()] = HOME_DEPARTMENTS.get(r["department"], r["department"])
    unit_callsigns: dict[str, str] = {}
    for cs, title in units.get("callsigns", {}).items():
        cs = str(cs).strip()
        unit_callsigns[cs] = f"{cs} {title}".strip()
        if cs[:3] in dept_numbers:
            unit_departments.setdefault(unit_callsigns[cs], dept_numbers[cs[:3]])
    mp = _take(data.get("map", {}), "map", {"home_lon", "home_lat", "home_zoom"})
    rel = lambda p: str((base / p).resolve()) if p and not Path(p).is_absolute() else p
    rel_state = lambda p: str((state_dir / p).resolve()) if p and not Path(p).is_absolute() else p   # storage
    # roster_department: accepted from older configs, unused (the boards come from cad.lists)
    cad = _take(data.get("cad", {}), "cad", {"db", "department", "lists", "roster_department", "monitor_departments"})
    cad_lists = ""
    if cad.get("lists"):
        cl = Path(cad["lists"]) if Path(cad["lists"]).is_absolute() else base / cad["lists"]
        if not cl.is_file():
            raise ConfigError(f"cad.lists: {cl} not found")
        cad_lists = str(cl.resolve())
    dsp = _take(data.get("dispatch", {}), "dispatch", {"tones", "voice", "rate", "repeat", "transmit", "channels",
                                                       "tts_engine", "speed", "tts_port", "fallback_voice", "tts_python",
                                                       "polly_region"})
    if dsp.get("transmit", False) is not False:
        raise ConfigError("dispatch.transmit: must be false: this version has no radio interface (M2), "
                          "so dispatches are logged and played locally only")
    try:
        d_tones = tuple((float(hz), float(sec)) for hz, sec in dsp.get("tones", [[903.2, 1.0], [810.2, 3.0]]))
    except (TypeError, ValueError):
        raise ConfigError("dispatch.tones: a list of [hz, seconds] pairs") from None
    if any(not (50 <= hz <= 4000 and 0 < sec <= 10) for hz, sec in d_tones) or sum(s for _, s in d_tones) > 20:
        raise ConfigError("dispatch.tones: each 50-4000 Hz for up to 10 s, 20 s in all")
    d_repeat, d_rate = int(dsp.get("repeat", 2)), int(dsp.get("rate", 0))
    if not 1 <= d_repeat <= 3 or not (d_rate == 0 or 80 <= d_rate <= 400):
        raise ConfigError("dispatch.repeat: 1 to 3; dispatch.rate: 0 or 80 to 400 words per minute")
    d_channels = tuple(str(c) for c in dsp.get("channels", ["FG1", "PC repeater"]))
    d_engine, d_voice, d_fallback = _dispatch_voice(dsp)
    try:
        d_speed, d_port = float(dsp.get("speed", 0.92)), int(dsp.get("tts_port", 8093))
    except (TypeError, ValueError):
        raise ConfigError("dispatch.speed: a number; dispatch.tts_port: a port number") from None
    if not 0.5 <= d_speed <= 2.0:
        raise ConfigError("dispatch.speed: 0.5 to 2.0 (1.0 = the voice's own pace)")
    if not 1024 <= d_port <= 65535 or d_port in (int(web.get("port", 8090)), int(stt.get("port", 8179))):
        raise ConfigError("dispatch.tts_port: a free port from 1024 to 65535 (not the web or whisper port)")
    d_python = str(Path(str(dsp.get("tts_python") or DEFAULT_PYTHON)).expanduser())
    d_region = str(dsp.get("polly_region", "")).strip()
    if d_region and not re.fullmatch(r"[a-z]{2}(-[a-z]+)+-\d", d_region):
        raise ConfigError('dispatch.polly_region: an AWS region ("us-east-1"), or "" for AWS_REGION')
    return Config(
        adapter_env=adapter_env,
        station_id=st.get("id", "pcfd"), station_name=st.get("name", "Port Chester FD"),
        station_auto_update=st.get("auto_update", True), station_ll=station_ll,
        host=web.get("host", "127.0.0.1"), port=int(web.get("port", 8090)),
        rtl_fm=radio.get("rtl_fm", "rtl_fm"), airband_multichannel=bool(radio.get("airband_multichannel", False)),
        channels=tuple(channels), tones=tuple(tones), tones_file=tones_file,
        ours=frozenset(ours | {"Port Chester FD", "Rye Brook FD"}),
        stt_server=stt.get("server_binary", "whisper-server"), stt_model=rel(stt.get("model", "models/ggml-large-v3-turbo.bin")),
        stt_port=int(stt.get("port", 8179)), stt_threads=int(stt.get("threads", 8)), stt_prompt=stt.get("prompt", ""),
        stt_talker_prompt=talker_mode, stt_beam_size=int(stt.get("beam_size", 0)), **aws_stt,
        p25_dir=rel(p25.get("capture_dir", "")),
        p25_talkgroups=p25_tgs, p25_tg_info=p25_info,
        p25_incident_talkgroups=frozenset(int(x) for x in p25.get("incident_talkgroups", [307, 333])),
        p25_aliases={str(k): v for k, v in p25.get("aliases", {}).items()}, p25_emergency_window_min=em_window,
        p25_systems=more_systems, tier_keep_days=tier_days, context_audio_max_mb=context_mb,
        p25_system_problems=tuple(sys_problems),
        mdc_aliases={str(k).upper(): v for k, v in mdc.get("aliases", {}).items()},
        unit_departments=unit_departments, unit_callsigns=unit_callsigns,
        shared_device=radio.get("shared_device", "") or (sdrs[0] if sdrs else ""), start_mode=radio.get("start_mode", ""),
        sdrs=sdrs, sdr_names={str(k): str(v) for k, v in radio.get("sdr_names", {}).items()},
        # the dongle named for ADS-B ([aircraft.receiver] sdr) is never one of sizeup's radios
        sdr_ignore=tuple(dict.fromkeys([*(str(x) for x in radio.get("sdr_ignore", ())), *([aircraft["aircraft_sdr"]] if aircraft["aircraft_sdr"] else [])])),
        sdr_auto=sdr_auto,
        **_auto_assign(radio, channels),
        p25_recorder=p25.get("recorder", ""), p25_recorder_dir=rel(p25.get("recorder_dir", "")),
        clips_dir=rel_state(storage.get("clips_dir", "captures/clips")), keep_hours=float(storage.get("keep_hours", 24)),
        audio_keep_hours=float(storage.get("audio_keep_hours", 12)),
        audio_keep_event_days=keep_event_days, audio_prune_batch=prune_batch, audio_offpeak=offpeak,  # type: ignore[arg-type]
        home=(float(mp.get("home_lon", -73.6640)), float(mp.get("home_lat", 41.0130)), float(mp.get("home_zoom", 13.4))),
        apparatus_path=af_found,
        cad_db=rel_state(cad.get("db", "state/cad.sqlite")), cad_department=str(cad.get("department", "Port Chester Fire")),
        cad_lists=cad_lists,
        cad_monitor_departments=tuple(str(x) for x in cad.get("monitor_departments", ["Port Chester FD"])),
        dispatch_tones=d_tones, dispatch_tts_engine=d_engine, dispatch_voice=d_voice, dispatch_speed=d_speed,
        dispatch_tts_port=d_port, dispatch_fallback_voice=d_fallback, dispatch_tts_python=d_python, dispatch_rate=d_rate,
        dispatch_repeat=d_repeat, dispatch_transmit=False, dispatch_channels=d_channels, dispatch_polly_region=d_region,
        **(dict(cloud, cloud_outbox=rel_state(cloud["cloud_outbox"])) if cloud else {}),
        cloud_state="" if cloud.get("cloud_role") == "hub" else rel_state("state/cloud.json"),
        **_geo(data),
        **_atc(data, base),
        **pa, **_alerts(data, pa), **_roads(data), **_roadwork(data), **_gis(data), **_routing(data), **_trains(data), **_weather(data), **aircraft, **vessels,
    )
