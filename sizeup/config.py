"""Station config (TOML). Unknown keys are rejected so typos don't silently do nothing.
See config.example.toml for every option with comments."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from sizeup.radio.tones import ToneCode


class ConfigError(ValueError):
    pass


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
    p25_aliases: dict[str, str] = field(default_factory=dict)
    mdc_aliases: dict[str, str] = field(default_factory=dict)   # MDC-1200 unit ID ("0611") -> name
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
    _take(data, "config", {"station", "web", "radio", "tones", "stt", "p25", "mdc", "storage", "map"})
    st = _take(data.get("station", {}), "station", {"id", "name"})
    web = _take(data.get("web", {}), "web", {"host", "port"})
    radio = _take(data.get("radio", {}), "radio", {"rtl_fm", "channels"})
    channels = []
    for i, c in enumerate(radio.get("channels", [])):
        _take(c, f"radio.channels[{i}]", {"label", "mhz", "kind", "device", "gain_db", "squelch_db"})
        if c.get("kind", "traffic") not in ("paging", "traffic"):
            raise ConfigError(f"radio.channels[{i}].kind: 'paging' or 'traffic'")
        channels.append(ChannelCfg(c["label"], float(c["mhz"]), c.get("kind", "traffic"), c.get("device", ""),
                                   c.get("gain_db", 40.0), float(c.get("squelch_db", 10.0))))
    tones, ours = [], set()
    for i, t in enumerate(data.get("tones", [])):
        _take(t, f"tones[{i}]", {"name", "a_hz", "b_hz", "agency", "ours"})
        tones.append(ToneCode(t["name"], float(t["a_hz"]), float(t["b_hz"]) if t.get("b_hz") else None,
                              t.get("agency", "")))
        if t.get("ours", False):
            ours.add(t["name"])
    stt = _take(data.get("stt", {}), "stt", {"server_binary", "model", "port", "threads", "prompt"})
    p25 = _take(data.get("p25", {}), "p25", {"capture_dir", "talkgroups", "aliases"})
    storage = _take(data.get("storage", {}), "storage", {"clips_dir", "keep_hours"})
    mdc = _take(data.get("mdc", {}), "mdc", {"aliases"})
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
        p25_talkgroups={int(k): v for k, v in p25.get("talkgroups", {"307": "Fire/EMS 15"}).items()},
        p25_aliases={str(k): v for k, v in p25.get("aliases", {}).items()},
        mdc_aliases={str(k).upper(): v for k, v in mdc.get("aliases", {}).items()},
        clips_dir=rel(storage.get("clips_dir", "captures/clips")), keep_hours=float(storage.get("keep_hours", 24)),
        home=(float(mp.get("home_lon", -73.6640)), float(mp.get("home_lat", 41.0130)), float(mp.get("home_zoom", 13.4))),
    )
