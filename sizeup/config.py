"""Station config (TOML). Unknown keys are rejected so typos don't silently do nothing.
See config.example.toml for every option with comments."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from sizeup.radio.tones import ToneCode


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
    shared_device: str = ""      # one dongle for everything (bench): switch channel / P25 in the web app
    start_mode: str = ""         # with shared_device: what the dongle does at startup ("South 2", "FG1", "Fire/EMS 15")
    p25_recorder: str = ""       # trunk-recorder command line, started/stopped by sizeup
    p25_recorder_dir: str = ""   # its working directory (config.json + talkgroups.csv)
    clips_dir: str = ""
    keep_hours: float = 24.0
    home: tuple[float, float, float] = (-73.6640, 41.0130, 13.4)   # lon, lat, zoom


def _take(d: dict, path: str, allowed: set[str]) -> dict:
    extra = set(d) - allowed
    if extra:
        raise ConfigError(f"{path}: unknown key(s) {', '.join(sorted(extra))}")
    return d


def load(path: str | Path) -> Config:
    path = Path(path)
    base = path.parent
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from e
    _take(data, "config", {"station", "web", "radio", "tones", "stt", "p25", "mdc", "units", "storage", "map"})
    st = _take(data.get("station", {}), "station", {"id", "name"})
    web = _take(data.get("web", {}), "web", {"host", "port"})
    radio = _take(data.get("radio", {}), "radio", {"rtl_fm", "channels", "tones_file", "shared_device", "start_mode"})
    channels = []
    for i, c in enumerate(radio.get("channels", [])):
        _take(c, f"radio.channels[{i}]", {"label", "mhz", "kind", "device", "gain_db", "squelch_db"})
        if c.get("kind", "traffic") not in ("paging", "traffic"):
            raise ConfigError(f"radio.channels[{i}].kind: 'paging' or 'traffic'")
        channels.append(ChannelCfg(c["label"], float(c["mhz"]), c.get("kind", "traffic"), c.get("device", ""),
                                   c.get("gain_db", 40.0), float(c.get("squelch_db", 10.0))))
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
    stt = _take(data.get("stt", {}), "stt", {"server_binary", "model", "port", "threads", "prompt"})
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
    storage = _take(data.get("storage", {}), "storage", {"clips_dir", "keep_hours"})
    mdc = _take(data.get("mdc", {}), "mdc", {"aliases"})
    units = _take(data.get("units", {}), "units", {"departments", "apparatus_file"})
    unit_departments: dict[str, str] = {}
    af = units.get("apparatus_file", "data/westchester_apparatus.csv")
    if af:
        af_path = Path(af) if Path(af).is_absolute() else (base / af)
        if not af_path.is_file():
            af_path = Path(__file__).resolve().parent.parent / af
        if af_path.is_file():
            import csv
            rows = (ln for ln in af_path.read_text().splitlines() if ln.strip() and not ln.startswith("#"))
            for r in csv.DictReader(rows):
                dep = HOME_DEPARTMENTS.get(r["department"], r["department"])
                unit_departments[r["unit"].strip().title()] = dep
        elif "apparatus_file" in units:
            raise ConfigError(f"units.apparatus_file: {af} not found")
    unit_departments.update({str(k).title(): v for k, v in units.get("departments", {}).items()})
    mp = _take(data.get("map", {}), "map", {"home_lon", "home_lat", "home_zoom"})
    rel = lambda p: str((base / p).resolve()) if p and not Path(p).is_absolute() else p
    return Config(
        station_id=st.get("id", "pcfd"), station_name=st.get("name", "Port Chester FD"),
        host=web.get("host", "127.0.0.1"), port=int(web.get("port", 8090)),
        rtl_fm=radio.get("rtl_fm", "rtl_fm"), channels=tuple(channels), tones=tuple(tones),
        ours=frozenset(ours | {"Port Chester FD", "Rye Brook FD"}),
        stt_server=stt.get("server_binary", "whisper-server"), stt_model=rel(stt.get("model", "models/ggml-large-v3-turbo.bin")),
        stt_port=int(stt.get("port", 8179)), stt_threads=int(stt.get("threads", 8)), stt_prompt=stt.get("prompt", ""),
        p25_dir=rel(p25.get("capture_dir", "")),
        p25_talkgroups=p25_tgs, p25_tg_info=p25_info,
        p25_incident_talkgroups=frozenset(int(x) for x in p25.get("incident_talkgroups", [307, 333])),
        p25_aliases={str(k): v for k, v in p25.get("aliases", {}).items()},
        mdc_aliases={str(k).upper(): v for k, v in mdc.get("aliases", {}).items()},
        unit_departments=unit_departments,
        shared_device=radio.get("shared_device", ""), start_mode=radio.get("start_mode", ""),
        p25_recorder=p25.get("recorder", ""), p25_recorder_dir=rel(p25.get("recorder_dir", "")),
        clips_dir=rel(storage.get("clips_dir", "captures/clips")), keep_hours=float(storage.get("keep_hours", 24)),
        home=(float(mp.get("home_lon", -73.6640)), float(mp.get("home_lat", 41.0130)), float(mp.get("home_zoom", 13.4))),
    )
