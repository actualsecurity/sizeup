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


PAGING_HANG_S = 1.5        # see ChannelCfg.hang_s / continue_s
PAGING_CONTINUE_S = 3.0


@dataclass(frozen=True)
class ChannelCfg:
    label: str                 # "South 2", "FG1"
    mhz: float
    kind: str                  # "paging" (tones + pages) | "traffic" (voice traffic, e.g. fireground)
    device: str = ""           # dongle serial ("" = first found)
    gain_db: float | None = 40.0
    squelch_db: float = 10.0   # FM quieting needed to open
    drop_db: float | None = None   # end a transmission this far under its own level (fm.NoiseSquelch)
    # Fades on paging channels (GitHub #61): the squelch hang once a transmission has had tones or speech
    # in it, and how long after it closes the carrier may come back (at the close threshold) and still
    # be the same page. None: by kind (paging 1.5 s / 3 s; traffic 0.4 s, as always / none).
    hang_s: float | None = None
    continue_s: float | None = None

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
    shared_device: str = ""      # one dongle for everything (bench): switch channel / P25 in the web app
    start_mode: str = ""         # with shared_device: what the dongle does at startup ("South 2", "FG1", "Fire/EMS 15")
    sdrs: tuple[str, ...] = ()   # dongles (exact USB serials) sizeup may use, each switched in the web app
    sdr_names: dict[str, str] = field(default_factory=dict)   # serial -> name shown in the web app ("adsb" -> "V3")
    sdr_ignore: tuple[str, ...] = ()   # serials never shown or used (a dongle dedicated to another app)
    sdr_auto: bool = False       # switch whatever RTL-SDRs are plugged in (rows come and go with the dongles)
    # A dongle that appears with no job a person chose gets the first job in auto_assign_order that no
    # other dongle has (sizeup/app.py _auto_assign). load() defaults it on; built directly (tests), off.
    sdr_auto_assign: bool = False
    sdr_auto_order: tuple[str, ...] = ("FG1", "Fire/EMS 15", "South 2", "FG2")   # channel labels / "Fire/EMS 15" (P25)
    p25_recorder: str = ""       # trunk-recorder command line, started/stopped by sizeup
    p25_recorder_dir: str = ""   # its working directory (config.json + talkgroups.csv)
    clips_dir: str = ""
    keep_hours: float = 24.0          # pages, incidents, transcripts
    audio_keep_hours: float = 12.0    # every audio file under captures/ (clips, P25 calls, skipped clicks)
    home: tuple[float, float, float] = (-73.6640, 41.0130, 13.4)   # lon, lat, zoom
    apparatus_path: str = ""          # the roster CSV actually found ("" = repo data/)
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
    # [alerts] (sizeup/notify.py; docs/cloud.md "Station health and alerts"): our OWN ntfy topic for pushes that
    # page a person: the emergency alarms (sizeup/cad/alarms.py, #128) and station health (sizeup/cloud/health.py,
    # #79: banner on /cad and /monitor, the log, the push), and station health's thresholds. Topic: env
    # SIZEUP_ALERT_TOPIC (the hub: SSM /sizeup-hub/alert-topic) else ntfy_topic; "" = no pushes. Never Tapout's
    # topics (pcfd-phone-prealert, pcfd-status, whatever [prealert] names): refused here and in sizeup/notify.py.
    alerts_ntfy_topic: str = ""
    alerts_ntfy_server: str = "https://ntfy.sh"
    alerts_silent_min: float = 3.0       # no state post from the station this long
    alerts_behind_min: float = 15.0      # the station on another build than the hub this long
    alerts_down_min: float = 2.0         # whisper or trunk-recorder down this long
    alerts_outbox_min: float = 5.0       # the oldest event still waiting on the station this old
    alerts_disk_pct: float = 90.0        # the station's disk this full
    alerts_repeat_min: float = 15.0      # at most one push per condition this often (plus its recovery)
    # [station] auto_update: a Linux station installed by deploy/station/install-linux.sh follows the
    # hub's build (sizeup/cloud/autoupdate.py); false keeps it where it is (also: sizeup-station autoupdate off)
    station_auto_update: bool = True
    # [roads] (sizeup/roads.py, #73, #86): the highway layers on the CAD maps: the Thruway's public feeds
    # (on by default) and 511NY's (a key: env SIZEUP_511NY_KEY, the hub: SSM /sizeup-hub/511ny-key, else
    # [roads] key, and the feed addresses below; none: 511NY off).
    roads_enabled: bool = True
    roads_key: str = ""
    roads_bbox: tuple[float, float, float, float] = (-73.80, 40.92, -73.56, 41.10)   # west, south, east, north
    roads_cameras_s: float = 3600.0
    roads_events_s: float = 90.0
    roads_signs_s: float = 300.0
    # Each feed's address from 511NY's access approval, "{key}" where the key goes ("" = that feed off).
    roads_events_url: str = ""
    roads_cameras_url: str = ""
    roads_signs_url: str = ""
    roads_thruway: bool = True        # the NYS Thruway Authority's public feeds (no key): I-95, I-287 (#86)
    roads_county: bool = True         # Westchester County DPW's cameras (a public ArcGIS layer, no key) (#99)
    roads_stream_max: int = 8         # live camera streams open at once, hub-wide (sizeup/roads.py STREAM_MAX, #125)
    roads_wall_streams: int = 0       # the camera wall's live tiles at once per page; 0: every camera in our area that streams
    # [roadwork] (sizeup/roadprojects.py, #185): road projects & closures for the maps and the incident view: the
    # Thruway's and 511NY's roadwork (through [roads]), the capital programs (Thruway, NYSDOT, CTDOT), the villages'
    # road notices; a WZDx feed when one is set ("{key}": the 511NY key, SIZEUP_511NY_KEY).
    roadwork_enabled: bool = True
    roadwork_projects: bool = True
    roadwork_notices: bool = True
    roadwork_wzdx_url: str = ""
    # [gis] (sizeup/gis.py, #91): Westchester County GIS datasets as map layers, fetched by the hub.
    # load() ships the starter set (gis.DEFAULT_LAYERS); built directly (tests), none.
    gis_enabled: bool = True
    gis_bbox: tuple[float, float, float, float] = (-73.80, 40.92, -73.56, 41.10)
    gis_layers: tuple = ()            # sizeup.gis.Layer, in panel order
    # [trains] (sizeup/trains.py, #131): Metro-North's New Haven Line from the MTA's public feeds (no key
    # today; one, should the MTA bring keys back: env SIZEUP_MTA_KEY, the hub: SSM /sizeup-hub/mta-key).
    # The feed addresses: "" = the MTA's (sizeup/trains.py RT_URL, GTFS_URL).
    trains_enabled: bool = True
    trains_key: str = ""
    trains_rt_url: str = ""
    trains_gtfs_url: str = ""
    trains_bbox: tuple[float, float, float, float] = (-73.845, 40.900, -73.530, 41.060)
    trains_rt_every_s: float = 5.0          # the realtime feed this often while someone looks (5 to 300 s)
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
    # [aircraft.receiver]: our own receiver, read only (readsb / dump1090 1090 MHz, dump978-fa 978 MHz UAT
    # aircraft.json, a path or an http(s) URL); sdr: the dongle meant for ADS-B (exact USB serial), kept out of
    # sizeup's own radios. Off, and no dongle named, until there is one.
    aircraft_receiver: bool = False
    aircraft_receiver_1090: str = ""
    aircraft_receiver_978: str = ""
    aircraft_sdr: str = ""


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
#   SIZEUP_ALERT_TOPIC / _SERVER [alerts] ntfy_topic / ntfy_server (our push topic: a name is its only secret;
#                                the hub: SSM /sizeup-hub/alert-topic)
#   SIZEUP_511NY_KEY                  [roads] key: the 511NY developer key (never in a file on the hub)
#   SIZEUP_STATUS_TOKEN               [cloud] status_token: the deploy check's read-only bearer
#   (no [gis] secrets: the county's open data needs no key)
#   SIZEUP_MTA_KEY                    [trains] key: only should the MTA require one again (none needed, 2026-10-14)
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
    """[alerts] -> Config fields: our ntfy topic for pushes (sizeup/notify.py: the emergency alarms, #128, and
    station health, #79) and station health's thresholds (sizeup/cloud/health.py). No section: the defaults, no
    push unless SIZEUP_ALERT_TOPIC is set. Tapout's topics (and whatever [prealert] names) are refused."""
    from sizeup.notify import server_problem, topic_problem
    al = _take(data.get("alerts", {}), "alerts", {"ntfy_topic", "ntfy_server", "silent_min", "behind_min", "down_min",
                                                  "outbox_min", "disk_pct", "repeat_min"})
    topic = _env("SIZEUP_ALERT_TOPIC", str(al.get("ntfy_topic", ""))).strip()
    if topic:
        why = topic_problem(topic, (prealert.get("prealert_ring_topic", ""), prealert.get("prealert_status_topic", "")))
        if why:
            raise ConfigError(f"alerts.ntfy_topic / SIZEUP_ALERT_TOPIC: {why}")
    server = _env("SIZEUP_ALERT_SERVER", str(al.get("ntfy_server", "https://ntfy.sh"))).strip().rstrip("/")
    if server_problem(server):
        raise ConfigError(f"alerts.ntfy_server: {server_problem(server)}")
    limits = {"silent_min": (3, 1, 120), "behind_min": (15, 5, 1440), "down_min": (2, 0.5, 120),
              "outbox_min": (5, 1, 1440), "disk_pct": (90, 50, 99.9), "repeat_min": (15, 1, 1440)}
    out = {}
    for k, (d, lo, hi) in limits.items():
        v = al.get(k, d)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= float(v) <= hi:
            raise ConfigError(f"alerts.{k}: a number from {lo:g} to {hi:g}")
        out[f"alerts_{k}"] = float(v)
    return dict(alerts_ntfy_topic=topic, alerts_ntfy_server=server, **out)


P25_JOB = "Fire/EMS 15"   # the dongle job that runs trunk-recorder (sizeup/app.py P25_MODE)
# FG2 (#119) last: only a 4th free dongle takes it by itself; a job with no channel in the config is skipped.
AUTO_ORDER = ("FG1", P25_JOB, "South 2", "FG2")


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
    """[roads] -> Config fields. No section: the Thruway's feeds on, 511NY off (no feed addresses), our
    area, the default schedule. The key never needs to be in a file (the environment wins); the
    addresses hold "{key}" instead."""
    rd = _take(data.get("roads", {}), "roads", {"enabled", "key", "bbox", "cameras_every_min", "events_every_s",
                                                "signs_every_min", "events_url", "cameras_url", "signs_url", "thruway", "county",
                                                "stream_max", "wall_streams"})
    enabled, thruway, county = rd.get("enabled", True), rd.get("thruway", True), rd.get("county", True)
    if not all(isinstance(x, bool) for x in (enabled, thruway, county)):
        raise ConfigError("roads.enabled / roads.thruway / roads.county: true or false")
    key = _env("SIZEUP_511NY_KEY", str(rd.get("key", ""))).strip()
    if key and not re.fullmatch(r"[!-~]{8,256}", key):
        raise ConfigError("roads.key / SIZEUP_511NY_KEY: the key from 511NY's access approval (8 or more characters, no spaces)")
    bbox = rd.get("bbox", [-73.80, 40.92, -73.56, 41.10])
    try:
        w, s, e, n = (float(x) for x in bbox)
    except (TypeError, ValueError):
        raise ConfigError("roads.bbox: [west, south, east, north] in degrees") from None
    if not (-180 <= w < e <= 180 and -90 <= s < n <= 90) or e - w > 3 or n - s > 3:
        raise ConfigError("roads.bbox: [west, south, east, north], west < east and south < north, at most 3 degrees a side")
    try:
        cams, events, signs = (float(rd.get("cameras_every_min", 60)) * 60, float(rd.get("events_every_s", 90)),
                               float(rd.get("signs_every_min", 5)) * 60)
    except (TypeError, ValueError):
        raise ConfigError("roads.cameras_every_min / events_every_s / signs_every_min: numbers") from None
    # 511NY allows 10 calls a minute per key: these floors keep the three feeds at 5 at most.
    if not (cams >= 600 and events >= 60 and signs >= 60):
        raise ConfigError("roads: cameras_every_min at least 10, events_every_s at least 60, signs_every_min at least 1 "
                          "(511NY allows 10 calls a minute)")
    smax, wall = rd.get("stream_max", 8), rd.get("wall_streams", 0)
    if not (isinstance(smax, int) and not isinstance(smax, bool) and 1 <= smax <= 64):
        raise ConfigError("roads.stream_max: live camera streams at once through the hub, 1 to 64")
    if not (isinstance(wall, int) and not isinstance(wall, bool) and 0 <= wall <= 64):
        raise ConfigError("roads.wall_streams: the camera wall's live tiles per page, 0 (every camera in our area that streams) to 64")
    urls = {}
    for k in ("events_url", "cameras_url", "signs_url"):
        u = str(rd.get(k, "")).strip()
        m = re.fullmatch(r"(https?)://([^/:?#\s]+)(:\d+)?([^\s]*)", u) if u else None
        if u and (not m or (m.group(1) == "http" and m.group(2) not in ("127.0.0.1", "localhost", "::1"))):
            raise ConfigError(f"roads.{k}: the feed's https:// address from 511NY, with {{key}} where the key goes "
                              "(plain http only to this machine)")
        if u and "key=" in u.lower() and "{key}" not in u:
            raise ConfigError(f"roads.{k}: put {{key}} where the key goes, never the key itself (SIZEUP_511NY_KEY)")
        urls[f"roads_{k}"] = u
    return dict(roads_enabled=enabled, roads_key=key, roads_bbox=(w, s, e, n), roads_cameras_s=cams,
                roads_events_s=events, roads_signs_s=signs, roads_thruway=thruway, roads_county=county,
                roads_stream_max=smax, roads_wall_streams=wall, **urls)


def _roadwork(data: dict) -> dict:
    """[roadwork] -> Config fields. No section: on, every public source; WZDx off (no address)."""
    rw = _take(data.get("roadwork", {}), "roadwork", {"enabled", "projects", "notices", "wzdx_url"})
    flags = {k: rw.get(k, True) for k in ("enabled", "projects", "notices")}
    if not all(isinstance(v, bool) for v in flags.values()):
        raise ConfigError("roadwork.enabled / roadwork.projects / roadwork.notices: true or false")
    u = str(rw.get("wzdx_url", "")).strip()
    m = re.fullmatch(r"(https?)://([^/:?#\s]+)(:\d+)?([^\s]*)", u) if u else None
    if u and (not m or (m.group(1) == "http" and m.group(2) not in ("127.0.0.1", "localhost", "::1"))):
        raise ConfigError("roadwork.wzdx_url: the WZDx feed's https:// address, {key} where a key goes (plain http only to this machine)")
    if u and re.search(r"(key|token)=", u, re.I) and "{key}" not in u:
        raise ConfigError("roadwork.wzdx_url: put {key} where the key goes, never the key itself (SIZEUP_511NY_KEY)")
    return dict(roadwork_enabled=flags["enabled"], roadwork_projects=flags["projects"], roadwork_notices=flags["notices"],
                roadwork_wzdx_url=u)


def _trains(data: dict) -> dict:
    """[trains] -> Config fields. No section: on, the MTA's feeds, our stretch of the line (Mount Vernon East to Stamford)."""
    tr = _take(data.get("trains", {}), "trains", {"enabled", "key", "rt_url", "gtfs_url", "bbox", "rt_every_s"})
    enabled = tr.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError("trains.enabled: true or false")
    key = _env("SIZEUP_MTA_KEY", str(tr.get("key", ""))).strip()
    if key and not re.fullmatch(r"[!-~]{8,256}", key):
        raise ConfigError("trains.key / SIZEUP_MTA_KEY: the MTA developer key (8 or more characters, no spaces)")
    urls = {}
    for k in ("rt_url", "gtfs_url"):
        u = str(tr.get(k, "")).strip()
        m = re.fullmatch(r"(https?)://([^/:?#\s]+)(:\d+)?([^\s]*)", u) if u else None
        if u and (not m or (m.group(1) == "http" and m.group(2) not in ("127.0.0.1", "localhost", "::1"))):
            raise ConfigError(f"trains.{k}: an https:// address (plain http only to this machine); leave it out for the MTA's")
        urls[f"trains_{k}"] = u
    bbox = _bbox(tr.get("bbox", [-73.845, 40.900, -73.530, 41.060]), "trains.bbox", 1.0)
    every = tr.get("rt_every_s", 5.0)
    if isinstance(every, bool) or not isinstance(every, (int, float)) or not 5 <= every <= 300:
        raise ConfigError("trains.rt_every_s: seconds between reads of the MTA's realtime feed while someone looks, 5 to 300")
    return dict(trains_enabled=enabled, trains_key=key, trains_bbox=bbox, trains_rt_every_s=float(every), **urls)


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
    rc = _take(ac.get("receiver", {}), "aircraft.receiver", {"enabled", "sdr", "json_1090", "json_978"})
    enabled, registry, receiver = ac.get("enabled", True), ac.get("registry", True), rc.get("enabled", False)
    alarms = ac.get("alarms", True)
    if not all(isinstance(x, bool) for x in (enabled, registry, receiver, alarms)):
        raise ConfigError("aircraft.enabled / registry / alarms / aircraft.receiver.enabled: true or false")
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
    if ("receiver" in feeds or receiver) and not (receiver and any(srcs.values())):
        raise ConfigError('aircraft.feeds: "receiver" needs [aircraft.receiver] enabled = true and json_1090 or json_978')
    if receiver and "receiver" not in feeds:
        feeds = ["receiver", *feeds]      # user, 2026-10-14: "primary local SDR on ADSB, secondary the above sources"
    sdr = str(rc.get("sdr", "")).strip()
    if sdr:
        if not re.fullmatch(r"[\w.-]{1,32}", sdr):
            raise ConfigError("aircraft.receiver.sdr: the ADS-B dongle's exact USB serial (letters, digits, . _ -)")
        if sdr.isdigit():
            raise ConfigError("aircraft.receiver.sdr: an all-digit serial is read as a device index by readsb and rtl "
                              "tools; give the dongle a name first (rtl_eeprom -s adsb)")
        if sdr in sdrs:
            raise ConfigError(f"aircraft.receiver.sdr: {sdr} is one of sizeup's own radios ([radio] sdrs / shared_device)")
    return dict(aircraft_enabled=enabled, aircraft_feeds=tuple(feeds), aircraft_alarms=alarms, aircraft_radius_nm=radius,
                aircraft_ceiling_ft=ceiling,
                aircraft_registry=registry, aircraft_receiver=receiver, aircraft_sdr=sdr, **urls, **srcs)


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


def _stt_aws(stt: dict) -> dict:
    """[stt] aws*: Amazon Transcribe (sizeup/transcribe.py). aws_channels: labels or talkgroup numbers."""
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
    vocab = str(stt.get("aws_vocabulary", "sizeup-pcfd"))
    if not re.fullmatch(r"[0-9a-zA-Z._-]{1,200}", vocab):
        raise ConfigError("stt.aws_vocabulary: letters, digits, . _ -")
    return dict(stt_aws=bool(stt.get("aws", False)), stt_aws_region=str(stt.get("aws_region", "")),
                stt_aws_endpoint=str(stt.get("aws_endpoint", "")).rstrip("/"),
                stt_aws_api_endpoint=str(stt.get("aws_api_endpoint", "")).rstrip("/"),
                stt_aws_idle_s=idle, stt_aws_pace=pace, stt_aws_limit_usd=limit, stt_aws_channels=names,
                stt_aws_vocabulary=vocab)


def load(path: str | Path) -> Config:
    path = Path(path)
    base = path.parent
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from e
    _take(data, "config", {"station", "web", "radio", "tones", "stt", "p25", "mdc", "units", "storage", "map",
                           "cad", "dispatch", "cloud", "prealert", "roads", "roadwork", "gis", "trains", "weather", "alerts",
                           "aircraft"})
    st = _take(data.get("station", {}), "station", {"id", "name", "auto_update"})
    if not isinstance(st.get("auto_update", True), bool):
        raise ConfigError("station.auto_update: true or false")
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
                                                 "auto_assign", "auto_assign_order"})
    raw_sdrs = radio.get("sdrs", ())
    sdr_auto = raw_sdrs == "auto"
    sdrs = (() if sdr_auto else tuple(str(x) for x in raw_sdrs)) or ((str(radio["shared_device"]),) if radio.get("shared_device") else ())
    if len(set(sdrs)) != len(sdrs):
        raise ConfigError("radio.sdrs: a serial is listed twice")
    aircraft = _aircraft(data, base, sdrs + ((str(radio["shared_device"]),) if radio.get("shared_device") else ()))
    channels = []
    for i, c in enumerate(radio.get("channels", [])):
        _take(c, f"radio.channels[{i}]", {"label", "mhz", "kind", "device", "gain_db", "squelch_db",
                                                 "drop_db", "hang_s", "continue_s"})
        if c.get("kind", "traffic") not in ("paging", "traffic"):
            raise ConfigError(f"radio.channels[{i}].kind: 'paging' or 'traffic'")
        kind = c.get("kind", "traffic")
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
                                             "aws_monthly_limit_usd", "aws_channels", "aws_vocabulary"})
    talker_mode = str(stt.get("talker_prompt", "off"))
    if talker_mode not in ("off", "ids", "ids+time"):
        raise ConfigError('stt.talker_prompt: "off", "ids" or "ids+time"')
    aws_stt = _stt_aws(stt)
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
    storage = _take(data.get("storage", {}), "storage", {"clips_dir", "keep_hours", "audio_keep_hours"})
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
        station_id=st.get("id", "pcfd"), station_name=st.get("name", "Port Chester FD"),
        station_auto_update=st.get("auto_update", True),
        host=web.get("host", "127.0.0.1"), port=int(web.get("port", 8090)),
        rtl_fm=radio.get("rtl_fm", "rtl_fm"), channels=tuple(channels), tones=tuple(tones), tones_file=tones_file,
        ours=frozenset(ours | {"Port Chester FD", "Rye Brook FD"}),
        stt_server=stt.get("server_binary", "whisper-server"), stt_model=rel(stt.get("model", "models/ggml-large-v3-turbo.bin")),
        stt_port=int(stt.get("port", 8179)), stt_threads=int(stt.get("threads", 8)), stt_prompt=stt.get("prompt", ""),
        stt_talker_prompt=talker_mode, stt_beam_size=int(stt.get("beam_size", 0)), **aws_stt,
        p25_dir=rel(p25.get("capture_dir", "")),
        p25_talkgroups=p25_tgs, p25_tg_info=p25_info,
        p25_incident_talkgroups=frozenset(int(x) for x in p25.get("incident_talkgroups", [307, 333])),
        p25_aliases={str(k): v for k, v in p25.get("aliases", {}).items()}, p25_emergency_window_min=em_window,
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
        **pa, **_alerts(data, pa), **_roads(data), **_roadwork(data), **_gis(data), **_trains(data), **_weather(data), **aircraft,
    )
