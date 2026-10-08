"""Station config (TOML). Unknown keys are rejected so typos don't silently do nothing.
See config.example.toml for every option with comments."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from sizeup.cloud.access import team_host
from sizeup.radio.tones import ToneCode
from sizeup.tts import DEFAULT_PYTHON, KOKORO_VOICES


class ConfigError(ValueError):
    pass


# Department names used across sizeup for the two home departments (tone codes, incidents).
HOME_DEPARTMENTS = {"Port Chester": "Port Chester FD", "Rye Brook": "Rye Brook FD"}


@dataclass(frozen=True)
class ChannelCfg:
    label: str                 # "South 2", "FG1"
    mhz: float
    kind: str                  # "paging" (tones + pages) | "traffic" (voice traffic, e.g. fireground)
    device: str = ""           # dongle serial ("" = first found)
    gain_db: float | None = 40.0
    squelch_db: float = 10.0   # FM quieting needed to open
    drop_db: float | None = None   # end a transmission this far under its own level (fm.NoiseSquelch)


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
    p25_incident_talkgroups: frozenset[int] = frozenset({307, 333})
    p25_tg_info: dict[int, dict] = field(default_factory=dict)   # tg -> {name, group, encrypted}  # attach to our incident (others: context)
    p25_aliases: dict[str, str] = field(default_factory=dict)
    mdc_aliases: dict[str, str] = field(default_factory=dict)   # MDC-1200 unit ID ("0611") -> name
    unit_departments: dict[str, str] = field(default_factory=dict)  # "Engine 60" -> "Port Chester FD"
    unit_callsigns: dict[str, str] = field(default_factory=dict)    # "2398" -> "2398 Duty Officer"
    stt_beam_size: int = 0            # whisper-server -bs (0/1 = greedy). Beam search: fewer garbled digit strings
    stt_talker_prompt: str = "off"    # "ids": hint whisper with who keyed up ("77 Alpha 4, 60 Control"); "ids+time": + HHMM
    shared_device: str = ""      # one dongle for everything (bench): switch channel / P25 in the web app
    start_mode: str = ""         # with shared_device: what the dongle does at startup ("South 2", "FG1", "Fire/EMS 15")
    sdrs: tuple[str, ...] = ()   # dongles (exact USB serials) sizeup may use, each switched in the web app
    sdr_names: dict[str, str] = field(default_factory=dict)   # serial -> name shown in the web app ("adsb" -> "V3")
    sdr_ignore: tuple[str, ...] = ()   # serials never shown or used (a dongle dedicated to another app)
    sdr_auto: bool = False       # switch whatever RTL-SDRs are plugged in (rows come and go with the dongles)
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
    cad_monitor_departments: tuple[str, ...] = ("Port Chester FD",)   # monitor incidents that wait to be accepted
    dispatch_tones: tuple[tuple[float, float], ...] = ((349.1, 1.0), (311.1, 3.0))   # (Hz, s) in order
    # The dispatch voice. load() defaults to Kokoro af_heart at 0.92; built directly (tests), `say` Samantha.
    dispatch_tts_engine: str = "say"  # "kokoro" (sizeup/tts_server.py; `say` as fallback) | "say"
    dispatch_voice: str = "Samantha"  # engine kokoro: a Kokoro voice ("af_heart"); engine say: macOS `say -v`
    dispatch_speed: float = 0.92      # Kokoro speaking speed (0.5-2.0)
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
    # [prealert] (sizeup/prealert.py): Tapout's ntfy feed, read only, on the CAD of record (hub or
    # all-in-one; never a station forwarding to a hub). Off unless the section is in the config.
    prealert_enabled: bool = False
    prealert_server: str = "https://ntfy.sh"
    prealert_ring_topic: str = "pcfd-phone-prealert"
    prealert_status_topic: str = "pcfd-status"
    prealert_expire_min: float = 5.0
    prealert_heartbeat_stale_min: float = 90.0


def _take(d: dict, path: str, allowed: set[str]) -> dict:
    extra = set(d) - allowed
    if extra:
        raise ConfigError(f"{path}: unknown key(s) {', '.join(sorted(extra))}")
    return d


def _dispatch_voice(dsp: dict) -> tuple[str, str, str]:
    """[dispatch] tts_engine, voice, fallback_voice. An older config's `voice = "Samantha"` (a `say`
    voice, no tts_engine) becomes the fallback, and Kokoro's af_heart the voice."""
    engine = str(dsp.get("tts_engine", "kokoro"))
    if engine not in ("kokoro", "say"):
        raise ConfigError('dispatch.tts_engine: "kokoro" or "say"')
    voice, fallback = str(dsp.get("voice", "")).strip(), str(dsp.get("fallback_voice", "")).strip()
    if engine == "kokoro" and voice and voice not in KOKORO_VOICES:
        if "tts_engine" in dsp or re.fullmatch(r"[a-z]{2}_\w+", voice):
            raise ConfigError(f"dispatch.voice: {voice!r} isn't a Kokoro voice (af_heart, am_michael, bf_emma, ...); "
                              'for a macOS `say` voice set tts_engine = "say", or use it as fallback_voice')
        voice, fallback = "", fallback or voice
    voice = voice or ("af_heart" if engine == "kokoro" else "Samantha")
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
                                                "keychain_access_id", "keychain_access_secret"})
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
                cloud_access_team=team, cloud_access_aud=aud, **_keychain(cl))


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


def load(path: str | Path) -> Config:
    path = Path(path)
    base = path.parent
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from e
    _take(data, "config", {"station", "web", "radio", "tones", "stt", "p25", "mdc", "units", "storage", "map",
                           "cad", "dispatch", "cloud", "prealert"})
    st = _take(data.get("station", {}), "station", {"id", "name"})
    web = dict(_take(data.get("web", {}), "web", {"host", "port"}))
    web["host"] = _env("SIZEUP_HOST", web.get("host", "127.0.0.1"))
    try:
        web["port"] = int(_env("SIZEUP_PORT", web.get("port", 8090)))
    except ValueError:
        raise ConfigError("SIZEUP_PORT: a port number") from None
    cloud = _cloud(data)
    state_dir = Path(os.environ["SIZEUP_STATE_DIR"]).expanduser() if os.environ.get("SIZEUP_STATE_DIR", "").strip() else base
    radio = _take(data.get("radio", {}), "radio", {"rtl_fm", "channels", "tones_file", "shared_device", "start_mode", "sdrs", "sdr_names", "sdr_ignore"})
    raw_sdrs = radio.get("sdrs", ())
    sdr_auto = raw_sdrs == "auto"
    sdrs = (() if sdr_auto else tuple(str(x) for x in raw_sdrs)) or ((str(radio["shared_device"]),) if radio.get("shared_device") else ())
    if len(set(sdrs)) != len(sdrs):
        raise ConfigError("radio.sdrs: a serial is listed twice")
    channels = []
    for i, c in enumerate(radio.get("channels", [])):
        _take(c, f"radio.channels[{i}]", {"label", "mhz", "kind", "device", "gain_db", "squelch_db",
                                                 "drop_db"})
        if c.get("kind", "traffic") not in ("paging", "traffic"):
            raise ConfigError(f"radio.channels[{i}].kind: 'paging' or 'traffic'")
        kind = c.get("kind", "traffic")
        # Simplex fireground: a weaker carrier can sit under ours (FG1 2026-10-07), so traffic channels
        # end a transmission on a 10 dB drop. Paging stays off: a distant repeater fades (South 2).
        drop = c.get("drop_db", 10.0 if kind == "traffic" else 0)
        channels.append(ChannelCfg(c["label"], float(c["mhz"]), kind, c.get("device", ""),
                                   c.get("gain_db", 40.0), float(c.get("squelch_db", 10.0)),
                                   float(drop) if drop else None))
    tones, ours = [], set()
    tf = radio.get("tones_file", "data/westchester_tones.csv")
    if tf:
        tf_path = Path(tf) if Path(tf).is_absolute() else (base / tf)
        if not tf_path.is_file():
            tf_path = Path(__file__).resolve().parent.parent / tf   # repo data/ when run from elsewhere
        if tf_path.is_file():
            import csv
            rows = (ln for ln in tf_path.read_text().splitlines() if ln.strip() and not ln.startswith("#"))
            for r in csv.DictReader(rows):
                tag = {"1": " (est.)", "2": " (heard)"}.get(r.get("estimated", "0"), "")
                tones.append(ToneCode(r["name"] + tag,
                                      float(r["a_hz"]), float(r["b_hz"]) if r.get("b_hz") else None))
        elif "tones_file" in radio:
            raise ConfigError(f"radio.tones_file: {tf} not found")
    for i, t in enumerate(data.get("tones", [])):
        _take(t, f"tones[{i}]", {"name", "a_hz", "b_hz", "agency", "ours"})
        tones.append(ToneCode(t["name"], float(t["a_hz"]), float(t["b_hz"]) if t.get("b_hz") else None,
                              t.get("agency", "")))
        if t.get("ours", False):
            ours.add(t["name"])
    stt = _take(data.get("stt", {}), "stt", {"server_binary", "model", "port", "threads", "prompt", "talker_prompt", "beam_size"})
    talker_mode = str(stt.get("talker_prompt", "off"))
    if talker_mode not in ("off", "ids", "ids+time"):
        raise ConfigError('stt.talker_prompt: "off", "ids" or "ids+time"')
    p25 = _take(data.get("p25", {}), "p25", {"capture_dir", "talkgroups", "talkgroups_file", "incident_talkgroups",
                                             "aliases", "recorder", "recorder_dir"})
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
                                                       "tts_engine", "speed", "tts_port", "fallback_voice", "tts_python"})
    if dsp.get("transmit", False) is not False:
        raise ConfigError("dispatch.transmit: must be false: this version has no radio interface (M2), "
                          "so dispatches are logged and played locally only")
    try:
        d_tones = tuple((float(hz), float(sec)) for hz, sec in dsp.get("tones", [[349.1, 1.0], [311.1, 3.0]]))
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
    return Config(
        station_id=st.get("id", "pcfd"), station_name=st.get("name", "Port Chester FD"),
        host=web.get("host", "127.0.0.1"), port=int(web.get("port", 8090)),
        rtl_fm=radio.get("rtl_fm", "rtl_fm"), channels=tuple(channels), tones=tuple(tones),
        ours=frozenset(ours | {"Port Chester FD", "Rye Brook FD"}),
        stt_server=stt.get("server_binary", "whisper-server"), stt_model=rel(stt.get("model", "models/ggml-large-v3-turbo.bin")),
        stt_port=int(stt.get("port", 8179)), stt_threads=int(stt.get("threads", 8)), stt_prompt=stt.get("prompt", ""),
        stt_talker_prompt=talker_mode, stt_beam_size=int(stt.get("beam_size", 0)),
        p25_dir=rel(p25.get("capture_dir", "")),
        p25_talkgroups=p25_tgs, p25_tg_info=p25_info,
        p25_incident_talkgroups=frozenset(int(x) for x in p25.get("incident_talkgroups", [307, 333])),
        p25_aliases={str(k): v for k, v in p25.get("aliases", {}).items()},
        mdc_aliases={str(k).upper(): v for k, v in mdc.get("aliases", {}).items()},
        unit_departments=unit_departments, unit_callsigns=unit_callsigns,
        shared_device=radio.get("shared_device", "") or (sdrs[0] if sdrs else ""), start_mode=radio.get("start_mode", ""),
        sdrs=sdrs, sdr_names={str(k): str(v) for k, v in radio.get("sdr_names", {}).items()},
        sdr_ignore=tuple(str(x) for x in radio.get("sdr_ignore", ())), sdr_auto=sdr_auto,
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
        dispatch_repeat=d_repeat, dispatch_transmit=False, dispatch_channels=d_channels,
        **(dict(cloud, cloud_outbox=rel_state(cloud["cloud_outbox"])) if cloud else {}),
        cloud_state="" if cloud.get("cloud_role") == "hub" else rel_state("state/cloud.json"),
        **_prealert(data),
    )
