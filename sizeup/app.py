"""sizeup service: receivers -> transcription -> incidents -> web display.

    python -m sizeup --config config.toml        # open http://localhost:8090/

    paging channel (South 2) --squelch--> transmission --tones?--> Page (held a few seconds in case the
        carrier comes back after a fade: radio/page.py PageHold, GitHub #61)
        page whose tones are one of ours (#80) --STT--> dispatch parse --> incident (geocoded)
        voice-only transmission --STT--> dispatcher traffic (attached to the active incident),
            or the voice half of a tones-only page sent as a separate transmission
    traffic channels (FG1)    --squelch--> transmission --STT--> traffic on the active incident
    P25 (trunk-recorder dir)  --call--> STT --> traffic on the active incident (unit IDs kept)
    incident for Port Chester --> an event in the CAD (/cad, sizeup/cad/) waiting for a person to accept it
    every transmission: its radio ID (MDC / P25) -> the rig it's on (sizeup/cad/radios.py) -> the CAD's
        communications log, linked to the events that rig is assigned to (/cad#radio, sizeup/cad/comms.py)
    who is keying up now (squelch + live MDC decode on FG1; trunk-recorder's grants + log for P25):
        sizeup/radio/live.py -> "talking" in both pages' state
    a page going out now on South 2 (tone pairs as they decode, then "voice"): sizeup/radio/livepage.py
        -> "paging"; it and FG1's live MDC both come from the live tap on each FM receiver's frames
        (sizeup/radio/tap.py). Each page is also a row in the communications log, linked to the event
        it opened (comms.py why "page"): it plays from the Radio view and the event.
    [cloud] role = "station": every finished page / transmission / transcript / radio ID heard also
        goes to the hub through a durable outbox (_emit, sizeup/cloud/station.py); role = "hub": no
        radios at all, the same state fed by the station (sizeup/cloud/hub.py). docs/cloud.md
        Forwarding is switched on and off while running in Settings > System > Station
        (sizeup/cloud/stationmode.py; state/cloud.json then wins over the role here).
    [roads]: 511NY's cameras, incidents, roadwork and message signs for the maps' Layers, camera
        snapshots proxied by this process (sizeup/roads.py)
    [roadwork]: road projects & closures for the maps and the incident view: the Thruway's and 511NY's
        roadwork, the villages' road notices, WZDx feeds (sizeup/roadprojects.py)
    [gis]: Westchester County GIS datasets (parcels, boundaries, utilities ...) for the maps' Layers,
        fetched and served by this process (sizeup/gis.py)
    [weather]: NWS alerts, conditions and radar, NOAA tides for the banner, the header, the maps and
        marine calls, fetched and served by this process (sizeup/weather.py)
    [prealert]: Tapout's ntfy feed (the HQ phone ringing), read only, on the CAD of record (all-in-one
        or the hub) -> a pre-alert at the top of the CAD's 60 Control column (sizeup/prealert.py)
    per channel (Settings > Radios, sizeup/channels.py): record / transcribe / engine (local whisper,
        Amazon Transcribe on the hub, or both): _stt() routes each transmission; AWS results come back
        through _aws_done -> _retext (sizeup/transcribe.py)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import wave
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from sizeup.config import P25_JOB, Config, ConfigError, airband_owner, load
from sizeup import janitor, tz
from sizeup.cad import Cad
from sizeup.cad.alarms import STUCK_WITHIN_S
from sizeup.cad.api import CadApi
from sizeup.dispatch import parse
from sizeup.incidents import IncidentStore, Traffic
from sizeup.radio.fm import RATE, Channel, NfmReceiver, Segment, id_burst_frames, speech_seconds, voice_floor_db
from sizeup.radio.am import AmReceiver
from sizeup.radio.am import Extra as AmExtra
from sizeup.radio.am import Schedule as AmSchedule
from sizeup.radio.mdc1200 import decode as mdc_decode
from sizeup.radio import fleetsync
from sizeup.cad.radios import fm_system
from sizeup.radio.p25 import Call, CallIngest, Piece, split_call
from sizeup.radio import claims
from sizeup.radio.live import LiveTalk
from sizeup.radio.livepage import HOLD_S, LivePaging
from sizeup.radio import sdr as sdr_lookup
from sizeup.radio import adsbd
from sizeup.radio import recorder as trunk_recorder
from sizeup.radio import systems as p25_systems
from sizeup.radio.page import Page, PageHold, assemble, decode_tones, page_from_dict, recording_note
from sizeup.radio.tap import LiveTap
from sizeup.cad import tonelist
from sizeup.radio.tones import ToneEvent, TonePlan, base_name
from sizeup.talkers import talker_prompt
from sizeup import channels
from sizeup.stt import CONTEXT, HIGH, LIVE, LOW, RERUN, SHED, URGENT, Transcriber, WhisperClient, radio_voice, wav_bytes
from sizeup.talkgroups import TalkgroupPrefs
from sizeup import sourcekeys as sources, tiers
from sizeup.tts import TtsService
from sizeup.web.server import WebServer
from sizeup.procenv import child_env

ROOT = Path(__file__).resolve().parent.parent
# MTA TrainTime's fast rate (#362): emergency incidents, active or pending, created or changed (anything logged) since ?
HOT_INCIDENTS_SQL = ("SELECT i.lat, i.lon FROM incidents i WHERE i.status IN ('active', 'pending') AND i.emergency = 1 "
                     "AND i.lat IS NOT NULL AND i.lon IS NOT NULL AND MAX(i.created_at, COALESCE((SELECT MAX(e.at) FROM events e "
                     "WHERE e.incident_id = i.id), 0)) >= ?")
HUB_SWITCH_S = 0.001     # the hub's GIL switch interval (sys.setswitchinterval; Python's default 0.005), start_app() (#284)
SPLIT_PAGE_S = 30.0      # voice this soon after a tones-only page for us = that page's voice
# The gazetteer names a town only from this confidence up (#80: "34 Longdale Avenue" read as Port Chester at
# 0.34 was a street near Fairview). The point and label stay for the map; the town is never a trigger anyway.
TOWN_CONFIDENCE = 0.6


def geo_of(m) -> dict | None:
    """A gazetteer match as the page / incident carries it: {lat, lon, label, kind, town, confidence}."""
    if m is None:
        return None
    conf = round(float(m.confidence), 2)
    return {"lat": m.lat, "lon": m.lon, "label": m.label, "kind": m.kind,
            "town": m.municipality if conf >= TOWN_CONFIDENCE else "", "confidence": conf}
# Channels renamed by an update (#275, sizeup/cloud/configupdate.py "airband-names"): a dongle's job saved under the old
# name (state/sdr_modes.json) is the renamed channel's.
RENAMED = {"NY Approach": "NY App 126.4/120.8", "NY Approach 124.65": "NY App 124.65"}
def traffic_key(t) -> str:
    """Where a transmission's speech-to-text choices are (sizeup/channels.py): its channel, "tg:<n>" for the county
    system's talkgroup, "p25:<system>:<n>" for another system's (#409)."""
    if t.talkgroup is None:
        return t.source
    return sources.channel_key(getattr(t, "system", ""), t.talkgroup)


P25_MODE = P25_JOB         # the shared-dongle mode that runs trunk-recorder ("Fire/EMS 15")
OFF_MODE = "Off"           # the shared dongle released for other programs (sizeup keeps running)
SWITCH_PAUSE_S = 1.0       # between stopping one program on the shared dongle and starting the next
SDR_WATCH_S = 3.0          # [radio] auto_assign: how often the plugged-in dongles are looked at
SDR_FAILING_RESTARTS = 3   # an auto job that has restarted this often and is down: logged once, left as is
SDR_SETTLE_S = 60.0        # after startup, Off dongles wait this long for the ones that had jobs (USB attach order)
EVIDENCE_LIST = 200        # an emergency call's record (App._p25_evidence): at most this many srcList / freqList entries
# Speech-to-text first on the station's whisper (#195): pages, Fire/EMS 15 and our firegrounds, ahead of the rest.
STT_FIRST = ("tg:307", "FG1", "FG2")
AIRBAND_FIRST: set[str] = set()   # ... and the airband channels (HPN ATC, #233: the pre-alerts), filled by App
ATC_CEILING_FT = 10000   # the station's own ADS-B: aircraft above this aren't talking to HPN Tower (the ATC prompt, #257)
MIN_SPEECH_S = 0.15      # less sound than this above the dead-air floor (and no tones) = a mic click
LIVE_WATCH_S = 0.2       # FG1 talking watcher: how often the squelch is looked at (the MDC ID: radio/tap.py)
ID_EDGE_S = 1.0          # an FG1 key-up with no ID: a burst is looked for this close to either end of the carrier (#163)
NO_ID_MIN_QUIETING_DB = 20.0   # ... and "no ID sent" is said only of a carrier this strong (PCFD radios: ~40 dB at
                               # the station); weaker, a burst may be lost in the hiss: "weak", nothing said either way
CATEGORIES = {"fire": "#ff4d3d", "ems": "#4da3ff", "rescue": "#ffb020", "hazard": "#c86bff",
              "service": "#3ddc97", "unknown": "#9aa0b4"}


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


# -- warm before the takeover (GitHub #264) ---------------------------------------------------------------------
# The hub's supervisor (sizeup/handoff.py) starts the app while its task stands by, gated: everything that needs
# no database (the imports, the gazetteer, the map providers and their caches, the ATC rules) is built then
# (prewarm), and App.__init__ takes it from here instead of building it again after the takeover.
_WARM: dict[str, object] = {}
_STARTUP = None                         # a sizeup.timing.Phases while main() builds the app: App marks its phases


def _mark(name: str) -> None:
    if _STARTUP is not None:
        _STARTUP.mark(name)


def _warm_for(cfg: Config) -> tuple:
    return (cfg.cad_db, cfg.cloud_role)


def _take_warm(key: str, cfg: Config):
    """What prewarm() built for this config (the same state dir), once."""
    if _WARM.get("_for") != _warm_for(cfg):
        return None
    return _WARM.pop(key, None)


def make_providers(cfg: Config, gaz) -> dict:
    """The map's data providers (constructed, not started: App.start_layers starts them), each having read its cache
    from the state dir. The county GIS layers' own cache (~50 MB) is read later, in the background (_load_layers)."""
    from sizeup.aircraft import AircraftLayer
    from sizeup.gis import Gis
    from sizeup.roadgraph import RoadGraph
    from sizeup.roadprojects import RoadProjects
    from sizeup.roads import Roads
    from sizeup.trains import Trains
    from sizeup.weather import Weather
    roads = Roads.from_config(cfg, log=log)
    return {"roads": roads, "gis": Gis.from_config(cfg, log=log, load=False),
            "roadwork": RoadProjects.from_config(cfg, roads=roads, gaz=gaz, log=log),
            "trains": Trains.from_config(cfg, log=log),
            "aircraft": AircraftLayer.from_config(cfg, log=log, devices=lambda: sdr_lookup.devices(cfg.rtl_fm)),
            "weather": Weather.from_config(cfg, log=log), "_gaz": gaz,
            "roadgraph": RoadGraph.from_config(cfg, log=log)}      # its graph read later, in the background


def _load_atc_rules(cfg: Config):
    from sizeup.atc import Rules
    return Rules.load(cfg.atc_rules or None)


def prewarm(cfg: Config, phases=None) -> None:
    """What the app can build before it may touch a database: never writes anything (the standby's task holds no
    lock). Whatever fails here is simply built again by App.__init__ (and fails there, as it always did)."""
    mark = phases.mark if phases is not None else (lambda name: None)
    _WARM.clear()
    _WARM["_for"] = _warm_for(cfg)
    try:
        import sizeup.atc, sizeup.cachesync, sizeup.cloud.access, sizeup.datasources, sizeup.fence  # noqa: E401,F401
        import sizeup.notify, sizeup.prealert, sizeup.transcribe  # noqa: E401,F401
        if cfg.cloud_role == "hub":
            import sizeup.cloud.hub, sizeup.cloud.hubstore  # noqa: E401,F401
        from sizeup.geo import intersections, pagegeo, places  # noqa: F401
        mark("imports")
        gaz = App._load_gazetteer()
        _WARM["gaz"] = gaz
        mark("gazetteer")
        _WARM["providers"] = make_providers(cfg, gaz)
        mark("providers")
        _WARM["providers"]["roadgraph"].load_cache()        # the road graph (#360): a second of CPU, here while we wait
        mark("road graph")
        if cfg.atc_enabled and any(c.kind == "airband" for c in cfg.channels):
            _WARM["atc_rules"] = (cfg.atc_rules, _load_atc_rules(cfg))
            mark("atc rules")
    except Exception as e:  # never fatal: App.__init__ builds what isn't here
        log(f"sizeup: warm-up incomplete ({type(e).__name__}: {e}): the rest is built at the takeover")


def renamed_overrides(ov: dict, kinds: dict) -> dict:
    """Settings > Radios > Channels choices saved under a channel's old name (#275: RENAMED) count for its new one, unless
    the new one has its own, or the old one is still a channel here. The old name's is then dropped (#307: a channel
    given that name later doesn't inherit it); App._channel_overrides saves that."""
    out = dict(ov)
    for old, new in RENAMED.items():
        if old in out and old not in kinds and new in kinds:
            moved = out.pop(old)
            out.setdefault(new, moved)
    return out


def plan_text(c) -> str:
    """A channel's frequencies by the hours (#260 schedule, #275 also): '118.575 + 120.8 23:00-07:00'."""
    head = AmSchedule(c.schedule).describe() if getattr(c, "schedule", ()) else f"{c.mhz:g}"
    return " + ".join([head, *(AmExtra(f, label, a, b).describe() for f, a, b, label in getattr(c, "also", ()))])


def make_receiver(cfg: Config, c, device, on_segment, covered=None) -> NfmReceiver:
    """The receiver for one [[radio.channels]] entry: AM with the carrier squelch on an airband channel (HPN ATC, #233:
    sizeup/radio/am.py), else NFM with the noise squelch."""
    if c.kind == "airband":     # a schedule (#260, NY Approach): retuned by the time of day, between transmissions
        # also (#275): other channels' frequencies on the same dongle, each heard as its own channel's (channelize.py)
        return AmReceiver(cfg.rtl_fm, device, c.gain_db, Channel(c.mhz, c.label), on_segment, log, open_db=c.squelch_db,
                          schedule=getattr(c, "schedule", ()) or None,
                          also=[AmExtra(f, label, a, b) for f, a, b, label in getattr(c, "also", ())],
                          owner=lambda mhz: airband_owner(cfg.channels, mhz), covered=covered,
                          multichannel=getattr(cfg, "airband_multichannel", False))
    return NfmReceiver(cfg.rtl_fm, device, c.gain_db, Channel(c.mhz, c.label), on_segment, log, open_db=c.squelch_db,
                       drop_db=c.drop_db, hang_s=c.hang_s, continue_s=c.continue_s)


def whisper_threads() -> int:
    """whisper-server's threads at most: one fewer than the CPUs (#300). whisper.cpp's threads wait on each other at
    every step, so one preempted by the radios stalls them all: more threads than free cores is slower, not faster."""
    return max(1, cpus() - 1)


def cpus() -> int:
    """The CPUs this process may run on (its affinity: a container's or a cgroup's share), else the machine's."""
    if hasattr(os, "sched_getaffinity"):
        try:
            return len(os.sched_getaffinity(0)) or 1
        except OSError:
            pass
    return os.cpu_count() or 4


class WhisperService:
    """Keep whisper-server running (model stays loaded). A server sizeup started earlier (recorded
    in state_path) is reused when its model/flags still match the config and replaced when they
    don't; a server on the port that sizeup didn't start is used as-is, never killed.
    SIZEUP_STT_EXTERNAL=1 (the Linux station: sizeup-whisper.service runs it, so restarting sizeup
    for an update doesn't reload the model): never started here, only waited for."""

    def __init__(self, binary: str, model: str, port: int, threads: int, beam_size: int = 0,
                 state_path: str | Path | None = None):
        threads = min(int(threads or 0) or whisper_threads(), whisper_threads())   # (start-whisper does the same, #300)
        self.argv = [binary, "-m", model, "--host", "127.0.0.1", "--port", str(port), "-t", str(threads)]
        if beam_size > 1:
            self.argv += ["-bs", str(beam_size)]
        self.port, self.state, self.detail = port, "starting", ""
        self.state_path = Path(state_path) if state_path else None
        self._proc: subprocess.Popen | None = None
        self._stop = threading.Event()
        self.external = os.environ.get("SIZEUP_STT_EXTERNAL", "") == "1"

    def _ours(self) -> tuple[int, list[str]] | None:
        """(pid, argv) of the whisper-server sizeup last started, if that process is still it."""
        try:
            d = json.loads(self.state_path.read_text()) if self.state_path else None
            pid, argv = int(d["pid"]), list(d["argv"])
            args = subprocess.run(["ps", "-ww", "-o", "args=", "-p", str(pid)], capture_output=True, text=True,
                                  timeout=5).stdout.strip()
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            return None
        return (pid, argv) if "whisper-server" in args and f"--port {self.port}" in args else None

    def _replace_stale(self) -> None:
        ours = self._ours()
        if not ours or ours[1] == self.argv:
            return
        log(f"whisper-server: config changed ({' '.join(ours[1][2:3])} -> {' '.join(self.argv[2:3])}), restarting it")
        try:
            os.kill(ours[0], signal.SIGTERM)
        except OSError:
            return
        for _ in range(40):
            if not self.up():
                return
            time.sleep(0.5)

    def up(self) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=2):
                return True
        except OSError:
            return False

    def start(self) -> WhisperService:
        threading.Thread(target=self._run, name="whisper", daemon=True).start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()

    def _run(self) -> None:
        backoff = 2.0
        if not self.external:
            self._replace_stale()
        while not self._stop.is_set():
            if self.up():
                self.state, self.detail, backoff = "ok", "speech-to-text ready", 2.0
            elif self.external:
                self.state, self.detail = "starting", "waiting for whisper-server (sizeup-whisper.service)"
            elif self._proc is None or self._proc.poll() is not None:
                try:
                    self._proc = subprocess.Popen(self.argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                                  env=child_env())
                    self.state, self.detail = "starting", "loading model"
                    if self.state_path:
                        try:
                            self.state_path.write_text(json.dumps({"pid": self._proc.pid, "argv": self.argv}))
                        except OSError:
                            pass
                except OSError as e:
                    self.state, self.detail = "down", f"cannot start whisper-server: {e}"
                    backoff = min(backoff * 2, 30)
            self._stop.wait(backoff)



CLIP_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,120}")


def split_turns(call: Call) -> list[Piece]:
    """One Piece (id, start, end, talker, audio, pairs, stt) per key-up: radio/p25.py split_call."""
    return split_call(call, RATE)


@dataclass
class AwsJob:
    """A transmission or page sent to Amazon Transcribe (sizeup/transcribe.py hands it back)."""
    kind: str                 # "traffic" | "page"
    id: str
    channel: str
    alt: dict | None = None   # "both": the local model's {text, model}, kept next to AWS's text
    page: object = None
    rerun: bool = False       # a person asked for it again (re-transcribe): the text is replaced, the result said
    recovered: bool = False   # left waiting by a restart (the hub): its fallback waits for the station to reconnect


@dataclass
class Rerun:
    """A speech-to-text re-run of a recording already on screen (a transmission or a page)."""
    id: str
    kind: str                 # "traffic" | "page"
    fm: bool                  # FM audio still needs de-emphasis (False for P25 and for saved listening clips)
    low_energy: bool = False
    unit: str = ""            # talkers, for the speech-to-text hint (same as the live pass)
    start: float | None = None
    status: str = "pending"
    raw: str = ""
    confidence: float | None = None
    as_alt: bool = False      # "both" re-run: this text is the alternative to compare, not the text

class App:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.plan = TonePlan(list(cfg.tones))
        self.whisper = WhisperService(cfg.stt_server, cfg.stt_model, cfg.stt_port, cfg.stt_threads, cfg.stt_beam_size,
                                      (Path(cfg.clips_dir).parent / "whisper-server.json") if cfg.clips_dir else None)
        # The dispatch voice (Kokoro, sizeup/tts.py), kept warm the same way; dispatches use `say` while it's down.
        self.tts = (TtsService(cfg.dispatch_tts_python, cfg.dispatch_tts_port, cfg.dispatch_voice,
                               (Path(cfg.clips_dir).parent / "tts-server.json") if cfg.clips_dir else None, log=log)
                    if cfg.dispatch_tts_engine == "kokoro" and cfg.dispatch_tts_python else None)
        self.stt = Transcriber(WhisperClient(f"http://127.0.0.1:{cfg.stt_port}/inference"), self._transcribed,
                               prompt=self._prompt, on_drop=self._stt_dropped)
        _mark("init")
        gaz = _take_warm("gaz", cfg)
        self.gaz = gaz if gaz is not None else self._load_gazetteer()
        _mark("gazetteer")
        self.keep_s = cfg.keep_hours * 3600
        self._lock = threading.Lock()
        self.storage: dict | None = None
        self._rerun_ids: set[str] = set()
        self.rerun = {"total": 0, "done": 0, "changed": 0, "failed": 0}
        self._rerun_note: dict[str, str] = {}     # id -> why a re-run didn't go the channel's way ("AWS unavailable ...")
        self.clips: OrderedDict[str, tuple[np.ndarray, float]] = OrderedDict()   # id -> (pcm, saved at)
        self.clips_dir = Path(cfg.clips_dir) if cfg.clips_dir else None
        if self.clips_dir:
            self.clips_dir.mkdir(parents=True, exist_ok=True)
        # Pages, incidents and traffic survive restarts (storage.keep_hours); old clips are pruned.
        self.store = IncidentStore(set(cfg.ours), geocode=self._geocode, keep_s=self.keep_s,
                                   path=(self.clips_dir.parent / "incidents.json") if self.clips_dir else None)
        self.pages: deque[Page] = deque(maxlen=1000)
        self._load_pages()
        self.tg = TalkgroupPrefs(cfg.p25_tg_info or {k: {"name": v} for k, v in cfg.p25_talkgroups.items()},
                                 set(cfg.p25_incident_talkgroups),
                                 (self.clips_dir.parent / "talkgroups.json") if self.clips_dir else None,
                                 others={x.name: x.talkgroups for x in cfg.p25_systems})   # (#409: MTA MRRS)
        for t in list(self.store.loose) + [t for i in self.store.incidents for t in i.traffic]:
            if t.talkgroup is not None:
                self.tg.seed(t.talkgroup, t.end, getattr(t, "system", ""))
        self._last_tones_only: Page | None = None
        self.pagehold = PageHold(self.plan)   # paging: a page waits out its continuation window
        self.receivers = [make_receiver(cfg, c, c.device, self._segment, self._heard_elsewhere) for c in cfg.channels]
        # trunk-recorder: sizeup's child, or on the Linux station its own service (sizeup-trunk-recorder, #71), which an
        # update of sizeup leaves running on its control channel (sizeup/radio/recorder.py make)
        self.recorder = (trunk_recorder.make(cfg.p25_recorder, cfg.p25_recorder_dir or ".", log)
                         if cfg.p25_recorder else None)
        # Switchable dongles ([radio] sdrs, by exact serial): each runs one mode at a time, picked in
        # the web app; a channel / Fire/EMS 15 runs on at most one of them.
        # sdrs = "auto": every RTL-SDR plugged in gets a row. A listed serial is always shown. sdr_ignore
        # hides a dongle for good. [radio] auto_assign (default on): a dongle that appears with no job
        # a person chose gets the first job in auto_assign_order no plugged-in dongle has (_arrive);
        # off, new ones start Off and sizeup never grabs a dongle by itself.
        # The ADS-B dongle (#235: behind a 1090 MHz filter and an LNA, "cant auto assign"): [aircraft.receiver] sdr, or
        # the one Settings > Radios named (an admin, with a confirm; state/adsb_receiver.json, which the station's
        # decoder reads too: sizeup/radio/adsbd.py). Never shown as a radio, never auto-assigned, never given a job.
        self._adsb_file = (Path(cfg.cad_db).parent / Path(adsbd.OVERRIDE).name) if cfg.cad_db else None
        self.adsb_sdr = self._load_adsb()
        self._ignore_base = frozenset(x for x in cfg.sdr_ignore if x != cfg.aircraft_sdr)
        self._listed: list[str] = [sd for sd in (list(cfg.sdrs) or ([cfg.shared_device] if cfg.shared_device else []))
                                   if not self._sdr_ignored(sd)]
        self.shared = bool(self._listed) or cfg.sdr_auto
        # More P25 systems (#409, sizeup/radio/systems.py: MTA MRRS): each its own trunk-recorder, on the sources a
        # dongle was given for in Settings > Radios (one job per source; never auto-assigned). None on the hub.
        self.systems: dict[str, p25_systems.Runtime] = {}
        if cfg.p25_recorder and cfg.p25_dir:
            for sysd in cfg.p25_systems:
                self.systems[sysd.name] = p25_systems.Runtime(
                    sysd, trunk_recorder.make_system(sysd.name, sysd.label, cfg.p25_recorder, sysd.dir, log), self.tg,
                    self._call_system, log, self.clips_dir.parent if self.clips_dir else None,
                    on_activity=self._p25_activity)
        sources.set_context_talkgroups(tg for x in cfg.p25_systems for tg in x.talkgroups)
        for why in cfg.p25_system_problems:       # a system directory that can't be used: said, the rest runs
            log(f"p25 systems: left out: {why}")
        self._sys_jobs: dict[str, tuple[p25_systems.Runtime, int]] = {
            x.job: (rt, x.index) for rt in self.systems.values() for x in rt.system.sources}
        self._booting = False                 # start(): each system's dongles are handed over together
        self.modes = [c.label for c in cfg.channels] + ([P25_MODE] if self.recorder else []) + list(self._sys_jobs)
        if self.shared:
            self.modes.append(OFF_MODE)
        first = cfg.start_mode if cfg.start_mode in self.modes else (self.modes[0] if self.modes else OFF_MODE)
        self.assign: dict[str, str] = {sd: (first if k == 0 else OFF_MODE) for k, sd in enumerate(self._listed)}
        # Who set each dongle's mode: "person" (the web app, here or the hub's) or "auto" (_auto_assign).
        # Missing = a default, or saved before this was recorded: auto-assign may fill it when it's Off.
        self.set_by: dict[str, str] = {}
        # What was picked in the web app wins over start_mode, so a restart doesn't move a dongle.
        self._mode_file = (Path(cfg.clips_dir).parent / "sdr_modes.json") if cfg.clips_dir else None
        self._load_modes()
        # Each serial's product while it was the only dongle with it (#242): {serial: product}. When a second dongle
        # turns up with the same serial (a factory 00000001), the one whose product differs is the newcomer: the
        # alert suggests renaming that one, and sizeup-station sdr-serial knows which physical dongle a job meant.
        self._products_file = self._mode_file.with_name("sdr_products.json") if self._mode_file else None
        self._products: dict[str, str] = self._load_products()
        self._ambiguous: dict[str, str] = {}  # serials two or more plugged-in dongles share now -> why (logged once)
        if self.recorder is not None:         # a config.json source naming a shared serial is left out (recorder.py)
            self.recorder.ambiguous_fn = lambda serial: self._ambiguity(serial, sdr_lookup.devices(cfg.rtl_fm, fresh=True))
        if not self.shared:                   # one dongle per channel: its serial is checked before each start too
            for k, c in enumerate(cfg.channels):
                if isinstance(c.device, str) and c.device and not c.device.isdigit():
                    self.receivers[k].device = self._guarded(c.device)
        self._seen: set[str] = set()          # dongles plugged in at the last look (new ones = arrivals)
        self._auto_skip: set[str] = set()     # an auto job failed to start on it: not tried again this run
        self._failing: set[str] = set()       # auto job down after restarts: logged once
        self._p25_skip = ""                   # why P25 isn't auto-assigned (logged when it changes)
        self._deferred: set[str] = set()      # Off arrivals waiting for the startup settle (_sdr_arrivals)
        self._settle_until = 0.0
        self._mode_lock = threading.RLock()
        self.switching = False
        self._kind = {c.label: c.kind for c in cfg.channels}
        self.p25 = (CallIngest(cfg.p25_dir, cfg.p25_talkgroups, self._call, {**cfg.unit_callsigns, **cfg.p25_aliases}, on_event=log,
                               name=P25_MODE)
                    if cfg.p25_dir else None)
        # PCFD's own dispatch console: incidents, units, run cards (state/cad.sqlite).
        self.cad = Cad.from_config(cfg, geocoder=self._geocode_text if self.gaz is not None else None, log=log)
        _mark("cad")
        if self.gaz is not None:           # the place search over the same gazetteer (a note's "@", #67)
            from sizeup.geo.intersections import IntersectionSearch
            from sizeup.geo.places import PlaceIndex
            # two streets typed, "N Main St / Westchester Ave" (#213): where they meet, in [geo] search_towns
            self.cad.cross_source = lambda: IntersectionSearch(self.gaz, towns=cfg.geo_search_towns, hq=cfg.geo_hq)
            self.cad.place_source = lambda: PlaceIndex(self.gaz, crossings=self.cad.crossings())
            # a new call's location: its place's address, its block's cross streets, what is at a map click (#204)
            from sizeup.geo.pagegeo import location_detail, what_is_here
            self.cad.detail_source = lambda m: location_detail(self.gaz, m)
            self.cad.here_source = lambda lat, lon, places: what_is_here(self.gaz, lat, lon, places)
        # Radio ID -> rig, and who is transmitting right now (both pages' "talking").
        self.radios = self.cad.radios
        self.live = LiveTalk(resolve=self.radios.resolve, tg_name=self.tg.name, main=self.tg.main,
                             learn=lambda src, alias, at: self._heard("p25", src, alias, at), debug=log)
        if self.p25 is not None:        # a finished call's key-ups checked against the live grants (issue #62)
            self.p25.grants = self.live.grants
            self.p25.alias_of = lambda src: self.live.aliases.get(src, "")
        # [cloud] role = "station": the hub link (sizeup/cloud/station.py) sets these; None = all-in-one.
        self.events: Callable[[str, dict, str | None], None] | None = None   # (type, data, clip id)
        self.link = None
        # Settings > System > Station turns that link on and off while running (sizeup/cloud/stationmode.py).
        self.station = None
        self._heard_sent: dict[tuple[str, str, str], float] = {}
        self.cad.live = self.live.state
        # A page going out now (South 2: who is being paged, pair by pair) and the MDC pre-ID at key-up
        # (FG1): one live tap on every FM receiver's frames (radio/tap.py, radio/livepage.py).
        # It outlasts a paging channel's continuation window (#61: a page is held continue_s after its
        # carrier drops, then finalised through _page_done -> _page_heard, which ends the live state).
        cont = [c.continue_s or 0.0 for c in cfg.channels if c.kind == "paging"]
        self.paging = LivePaging(self.plan, set(cfg.ours) | set(cfg.cad_monitor_departments),
                                 hold_s=max(HOLD_S, max(cont, default=0.0) + 1.0))
        self.paging_state: Callable[[], list[dict]] = self.paging.state    # the hub: the station's (cloud/hub.py)
        self.tap = LiveTap(lambda label: self._kind.get(label, ""), self.paging, self.live, mdc=lambda a: self._mdc(a), log=log)
        self.tap.on_emergency = self._fm_emergency      # the emergency button at the key-up (#128)
        for r in self.receivers:
            self.tap.attach(r)
        self.cad.paging = lambda: self.paging_state()
        self.cad.has_clip = self.has_clip
        # A page that isn't ours naming our units is flagged (#115's safety net): the feed's unit matcher, for the
        # Radio view's rows too.
        self.cad.comms.names_ours = self.cad.feed.names_ours
        self.cad.main_tg = self.tg.main
        # Per-channel record / transcribe / engine (Settings > Radios, sizeup/channels.py) and Amazon
        # Transcribe where this process may call it ([stt] aws: the hub; sizeup/transcribe.py).
        self.cad.radio_ctx = self
        self._chan_ov: dict = self._channel_overrides()
        # Tone pairs named from the console (Settings "tones", #80): merged into the plan, with the names
        # that count as ours; the hub sends them to the station with the channel choices.
        self._tone_ov: dict = {}
        self.cad.settings.use_tone_list(cfg.tones, set(cfg.ours) | set(cfg.cad_monitor_departments),
                                        (self.clips_dir.parent / "unknown_tones.jsonl") if self.clips_dir else None)
        self._apply_named_tones()
        self.cad.tone_names = self.tone_names
        self._hub_aws = self._load_hub_aws()
        self._aws_wait: dict[str, tuple[np.ndarray, str]] = {}
        self._vocab_suggest: list[str] | None = None
        self.aws = self.vocab = self.atc_vocab = None
        _mark("radio")
        if cfg.stt_aws:
            self._aws_setup()
            _mark("speech-to-text")
        # The phone pre-alert from Tapout's ntfy feed (sizeup/prealert.py), read only, on the CAD of
        # record: all-in-one or the hub. While this station forwards to a hub it is off here (the hub
        # subscribes: one alert): prealert_sync() follows Settings > System > Station.
        self.prealert = None
        self._started = False
        # External incident sources (#297, sizeup/sources/): the station's read-only CAD feed adapters, found and
        # started in start() (never on the hub: it has none). Their events go to the hub's outbox, or the CAD here.
        self.sources_runner = None
        # The map caches kept across hub restarts (sizeup/cachesync.py, #229): on a new Fargate task, the county GIS
        # layers, the camera lists, Metro-North's stations and the FAA registry come back from the S3 backups bucket.
        # Since #251 that happens in the background after the CAD serves (start_layers -> _load_layers), and the
        # providers below read (or read again) their caches once it is over. The hub only (LITESTREAM_BUCKET set);
        # local only everywhere else.
        self.cache_mirror = None
        self.fence = None                         # the hub's single-writer lease (sizeup/fence.py), under its supervisor
        if cfg.cloud_role != "station" and cfg.cad_db:
            from sizeup.cachesync import CacheMirror
            from sizeup.fence import Fence
            fence = Fence.from_env(log=log)      # the hub's single-writer lock (sizeup/handoff.py): uploads only while ours
            self.fence = fence
            self.cache_mirror = CacheMirror.from_env(Path(cfg.cad_db).parent, log=log, fence=fence.ok if fence else None)
        self._layers_lock = threading.Lock()
        self._layers_on = False                   # start_layers() asked for, stop_layers() not since
        self._layers_thread: threading.Thread | None = None
        self.layers_ready = threading.Event()     # the map caches read: the providers run (Settings, /healthz "loading")
        # The map's highway layers (sizeup/roads.py, #73): 511NY's cameras, incidents and signs where
        # it is configured, the gazetteer's mile markers and exits everywhere. Its place search is a
        # provider for the "@" place search (#67's Cad.search_places asks cad.place_providers).
        prov = _take_warm("providers", cfg)
        if not isinstance(prov, dict) or prov.get("_gaz") is not self.gaz:
            prov = make_providers(cfg, self.gaz)
        self.roads = prov["roads"]
        self.cad.place_providers = [self.roads.search_places]
        # The county GIS layers (sizeup/gis.py, #91): parcels, boundaries, utilities, facilities ... fetched
        # from Westchester County GIS by the CAD of record, served to the maps from here.
        self.gis = prov["gis"]                     # its cache (28 layers, ~50 MB) read in _load_layers
        # The camera wall's area (#125): our fire districts from the county layer, the Settings edits
        # ("camera_wall": margin, include, exclude), our departments for the municipal fallback; Settings >
        # System shows every camera with where it falls (cad.camera_area).
        self.roads.gis = self.gis
        self.roads.ours = tuple(cfg.cad_monitor_departments)
        self.roads.wall_settings = lambda: self.cad.settings.overrides().get("camera_wall") or {}
        self.cad.camera_area = self.roads.settings_rows
        # The camera DVR (#277, sizeup/dvr.py): live cameras' segments on this task's disk, on demand (the always list,
        # pins, an open event's cameras, the ones watched); the pins in the CAD store so a deploy keeps them.
        # On shared storage (#322: EFS mounted at the DVR's folder, or [roads] dvr_shared = true) the recording outlives a
        # deploy: the response area's cameras 24/7, a 12 h window, and only the lease holder writes (self.fence). This
        # is the leader's app: the standby's warm app (prewarm) never gets here.
        if cfg.roads_dvr and cfg.roads_enabled:
            from sizeup.dvr import WORKERS, MAX_WORKERS, Dvr, shared_mount
            state = Path(cfg.cad_db).parent if cfg.cad_db else Path(".")
            root = Path(cfg.roads_dvr_dir) if cfg.roads_dvr_dir else state / "dvr"
            fs = shared_mount(root) if cfg.roads_dvr_shared == "auto" else ""
            shared = cfg.roads_dvr_shared == "on" or bool(fs)
            if shared:
                log(f"dvr: {root} is shared storage ({fs or 'dvr_shared = true'}): the response area's cameras, "
                    f"{cfg.roads_dvr_shared_window_s / 3600:.0f} h, only while this task holds the lease")
            cams = cfg.roads_dvr_shared_max_cameras if shared else cfg.roads_dvr_max_cameras
            self.roads.dvr = Dvr(self.roads, root,
                                 window_s=cfg.roads_dvr_shared_window_s if shared else cfg.roads_dvr_window_s,
                                 max_bytes=cfg.roads_dvr_shared_max_bytes if shared else cfg.roads_dvr_max_bytes,
                                 near_m=cfg.roads_dvr_near_m, always=cfg.roads_dvr_always, linger_s=cfg.roads_dvr_linger_s,
                                 max_cameras=cams, log=log, area=(cfg.roads_dvr_area or ()) if shared else (), shared=shared,
                                 area_wall=shared and cfg.roads_dvr_area is None, area_extra_m=cfg.roads_dvr_extra_m,
                                 workers=min(MAX_WORKERS, max(WORKERS, cams // 2)) if shared else WORKERS,
                                 may_write=self.fence.ok if self.fence is not None else None)
            store = self.cad.store
            self.roads.dvr.incidents = lambda: store.q(
                "SELECT lat, lon, address FROM incidents WHERE status IN ('queued', 'pending', 'active') "
                "AND lat IS NOT NULL AND lon IS NOT NULL")
            self.roads.dvr.load_pins = lambda: json.loads(store.meta_get("dvr_pins") or "[]")
            self.roads.dvr.save_pins = lambda pins: store.meta_put("dvr_pins", json.dumps(pins))
            self.cad.dvr_pin = self.roads.dvr.pin
        # Incident attachments (#323, sizeup/attachments.py): camera clips saved from the DVR and every transmission
        # linked to an event, copied to the private S3 bucket (SIZEUP_ATTACHMENTS_BUCKET) and kept a year; only the
        # lease holder writes. Without the bucket: "not configured", nothing copied.
        from sizeup.attachments import Attachments
        self.attachments = Attachments.from_env(self.cad.store, log=log,
                                                may_write=self.fence.ok if self.fence is not None else None)
        self.attachments.dvr = lambda: self.roads.dvr
        self.attachments.camera = self.roads.camera
        self.attachments.audio = self.clip
        self.cad.attachments = self.attachments
        # Road projects & closures (sizeup/roadprojects.py, #185): the Thruway's and 511NY's roadwork through roads,
        # the villages' road notices (placed by the gazetteer, or listed), any WZDx feed configured; read while someone looks.
        self.roadwork = prov["roadwork"]
        # The road graph and the rigs' probable paths (#360, sizeup/roadgraph.py, sizeup/cad/routes.py): the graph is
        # read in the background (the standby's prewarm, or _load_layers), fetched on Settings > Data sources' schedule;
        # until it is there the maps draw straight lines and the estimates are straight-line ones.
        self.roadgraph = prov.get("roadgraph")
        if self.roadgraph is None:
            from sizeup.roadgraph import RoadGraph
            self.roadgraph = RoadGraph.from_config(cfg, log=log)
        if self.roadgraph.configured:
            from sizeup.cad.routes import Routes
            self.cad.routes = Routes(self.roadgraph.ready, lambda: self.cad.eta, self._route_closures,
                                     lambda: self.cad.firehouses, log=log)
        # Metro-North's trains (sizeup/trains.py, #131): the MTA's feeds, read by the CAD of record while
        # someone looks at the Trains layer or an event near the tracks.
        self.trains = prov["trains"]
        # one station, one mark (#162): the county's Train stations leave out the ones the Trains layer draws
        self.gis.train_stations = self.trains.stations
        # TrainTime's fast rate for an active incident near the line (#362); each new read pushed (trains_rev)
        from sizeup.traintime import HOT_AGE_S
        self.trains.hot_points = lambda: [(r["lat"], r["lon"]) for r in self.cad.store.q(HOT_INCIDENTS_SQL,
                                                                                            (time.time() - HOT_AGE_S,))]
        self.cad.trains_rev = lambda: self.trains.rev
        # Aircraft near HPN (sizeup/aircraft.py, #155): shortfinal's PCFD-focused picture, read by the CAD of record
        # while someone looks at the Aircraft layer. It never opens a dongle: a receiver's aircraft.json is only read.
        self.aircraft = prov["aircraft"]
        # Weather and water (sizeup/weather.py, #132): NWS alerts, conditions and radar, NOAA tides; a warning
        # said by the dispatch voice when Settings > Dispatch allows it (played where the console is open).
        self.weather = prov["weather"]
        _mark("providers")
        self.weather.speak_on = lambda: bool(getattr(self.cad, "weather_speak", False))
        self.weather.speaker = self._weather_voice
        if cfg.prealert_enabled:
            from sizeup.prealert import PreAlerts
            self.prealert = PreAlerts(self.cad, cfg.prealert_server, cfg.prealert_ring_topic, cfg.prealert_status_topic,
                                      cfg.prealert_expire_min, cfg.prealert_heartbeat_stale_min, log=log)
            self.prealert_sync()
        # Pushes (sizeup/notify.py Pushes, [alerts]): our own ntfy topics by kind: the emergency alarms
        # (sizeup/cad/alarms.py, #128, aircraft squawks too) to the alerts topic; station health and Amazon
        # Transcribe (sizeup/cloud/health.py, the hub) to the status topic. Only one set: both go there.
        # Settings > System > Push notifications (the CAD settings' "notify") win over env / SSM over the file,
        # looked up at every push: a change there needs no restart.
        from sizeup import notify
        env_is = "ssm" if cfg.cloud_role == "hub" else "env"      # the hub's env comes from SSM (deploy/aws/taskdef.json)
        def src(s: str) -> str:
            return env_is if s == "env" else (s or "config")
        self.pushes = notify.Pushes(
            cfg.alerts_ntfy_server, cfg.alerts_ntfy_topic, cfg.alerts_status_topic,
            sources={notify.ALERTS: src(cfg.alerts_ntfy_topic_source), notify.STATUS: src(cfg.alerts_status_topic_source)},
            server_source=src(cfg.alerts_ntfy_server_source) if cfg.alerts_ntfy_server_source != "default" else "default",
            settings=lambda: self.cad.settings.section(notify.SECTION),
            also_forbidden=(cfg.prealert_ring_topic, cfg.prealert_status_topic), log=log)
        self.cad.pushes = self.pushes
        self.cad.alarms.notifier = self.pushes.notifier(notify.ALERTS)
        log(self.pushes.line())
        _mark("prealert + pushes")
        self.cad.alarms.public_url = cfg.cloud_public_url
        # Airport pre-alerts (sizeup/cad/airport_alerts.py, #233): pushed to the alerts topic; HPN ATC's watch
        # (sizeup/atc.py) reads every airband transcript on the CAD of record (_atc).
        aa = self.cad.airport_alerts
        aa.dedupe_s, aa.expire_s, aa.max_per_hour = cfg.atc_dedupe_min * 60, cfg.atc_expire_min * 60, cfg.atc_max_per_hour
        aa.notifier, aa.public_url = self.pushes.notifier(notify.ALERTS), cfg.cloud_public_url
        self.atc = None
        self._atc_vocab: dict | None = None
        airband = [c.label for c in cfg.channels if c.kind == "airband"]
        AIRBAND_FIRST.update(airband)
        if airband and cfg.atc_enabled:
            from sizeup.atc import AtcWatch
            try:
                warm = _take_warm("atc_rules", cfg)
                rules = warm[1] if isinstance(warm, tuple) and warm[0] == cfg.atc_rules else _load_atc_rules(cfg)
                self.atc = AtcWatch(aa, airband, rules, aircraft=self._atc_aircraft,
                                    context=cfg.atc_context, context_s=cfg.atc_context_s, log=log)
                log(f"atc: airport pre-alerts from {', '.join(airband)} ({len(self.atc.rules.rules)} rules)")
            except (OSError, ValueError, KeyError) as e:
                log(f"atc: WARNING the pre-alert rules didn't load ({type(e).__name__}: {e}): no HPN ATC pre-alerts")
        # ... and the ones that build up over several transmissions (#369, sizeup/airport_watch.py): an abnormal
        # go-around followed through its frequency changes (_atc, _atc_view), "alert level N" on 60 Control (the CAD's)
        self.cad.airport_watch.configure(cfg.channels, self._atc_aircraft, cfg.atc_alert_level_incident,
                                         enabled=cfg.atc_enabled, airport_names=cfg.atc_airport_names)
        # The emergency window (alarms.Windows): the button's talkgroup and radio kept this long, Record off or on
        self.cad.alarms.windows.keep_s = cfg.p25_emergency_window_min * 60
        self.cad.alarms.on_clear = self._all_clear_sent
        # An aircraft emergency near HPN (a 7500 / 7600 / 7700 squawk, #155) raises the same alarm and push, on the
        # CAD of record only (a station forwarding to the hub leaves it to the hub).
        self.aircraft.on_emergency = lambda ev: None if self.forwarding() else self.cad.alarms.on_aircraft(ev)
        self.aircraft.on_clear = lambda ev, aid, why: self.cad.alarms.aircraft_cleared(ev, aid, why)
        # ... and an airport pre-alert in the queue with everything known about the aircraft (#235): HPN ATC's kind
        # (#233, sizeup/cad/airport_alerts.py, source "adsb"), one per emergency, beside its alarm. The API feeds are
        # read for the watch only on the CAD of record: a station forwarding to the hub leaves that to the hub.
        from sizeup.cad.airport_alerts import AircraftPreAlerts
        self.aircraft.prealerts = AircraftPreAlerts(self.cad.airport_alerts, self.cad)
        self.aircraft.active = lambda: not self.forwarding()
        # Who is talking on HPN Tower (#252, sizeup/atc_talkers.py): every picture kept a few minutes (a callsign heard is
        # matched against what was near HPN around its transmission), and the map's "talking" ring; the CAD of record only.
        talkers = self.cad.atc_talkers
        talkers.aircraft = self._atc_aircraft
        self.aircraft.on_view = lambda view, now: None if self.forwarding() else self._atc_view(view, now)
        # ... and the sizeup-atc vocabulary kept up with the operators seen (#257, sizeup/atc_vocab.py): the leader only
        self.atc_refresh = None
        if getattr(self, "atc_vocab", None) is not None:
            from sizeup import atc as atc_mod
            from sizeup.atc_vocab import Refresher
            self.atc_refresh = Refresher(self.atc_vocab, talkers.operators, lambda: atc_mod.vocabulary()["phrases"],
                                         enabled=lambda: bool(getattr(self.cad, "atc_vocab_refresh", True)),
                                         leader=self._atc_leader, on_pending=self._atc_vocab_pending, log=log)
        self.aircraft.extra = lambda: None if self.forwarding() else talkers.map_payload()
        if self.recorder is not None:
            self.recorder.on_line = self.live.feed_line
        # Settings > Data sources (sizeup/datasources.py, #186): every pull above with its status, Refresh now and,
        # for the slow ones, the schedule the registry keeps (the admins' choices in the CAD settings).
        from sizeup import datasources
        self.datasources = datasources.build(self, log=log, choices=lambda: self.cad.settings.overrides().get(datasources.SECTION) or {})
        self.cad.datasources = self.datasources
        self._fm_live: dict[str, dict] = {}
        self._stopping = threading.Event()
        _mark("atc + data sources")
        self._backfill()
        _mark("backfill")

    # -- geocoding ------------------------------------------------------------------------------

    @staticmethod
    def _load_gazetteer():
        try:
            from sizeup.geo.gazetteer import Gazetteer
            return Gazetteer()
        except Exception as e:  # missing data / module: incidents still open, just without a map point
            log(f"geocoder unavailable ({type(e).__name__}: {e}); incidents won't be placed on the map")
            return None

    def _geocode(self, d, tone_departments=()) -> dict | None:
        """A page's location (#176, sizeup/geo/pagegeo.py): every address, read-back and named place in the page,
        checked against its cross streets and held to the towns of its tones; "confirmed" False = the card offers
        the readings to pick from and doesn't set the map."""
        if self.gaz is None or not d.location:
            return None
        try:
            from sizeup.geo.pagegeo import locate_page
            return locate_page(self.gaz, d.text, tone_departments).as_geo()
        except Exception as e:
            log(f"geocode error for {d.location!r}: {type(e).__name__}: {e}")
            return None

    def _geocode_text(self, q: str) -> list[dict]:
        """The CAD location box: every reading of what was typed, best first (the CAD orders them by town)."""
        try:
            return [{k: getattr(m, k) for k in ("label", "lat", "lon", "kind", "municipality", "precision")}
                    | {"confidence": round(float(m.confidence), 2)} | self._geocode_parts(m) for m in self.gaz.candidates(q)[:20]]
        except Exception as e:
            log(f"geocode error for {q!r}: {type(e).__name__}: {e}")
            return []

    @staticmethod
    def _geocode_parts(m) -> dict:
        """An address's street and number; a landmark's name and its street address as the data has it (#204)."""
        if m.kind == "landmark":
            town = f", {m.municipality}" if m.municipality else ""
            name = m.label[:-len(town)] if town and m.label.endswith(town) else m.label
            return {"place": name, "place_address": m.street or ""}
        if m.kind == "intersection":         # both streets: the cross streets when it is picked (#213)
            return {"street": m.street or "", "house_number": "", "streets": [m.street or "", m.cross_street or ""]}
        return {"street": m.street or "", "house_number": m.house_number or ""}

    # -- persistence ----------------------------------------------------------------------------

    def _load_pages(self) -> None:
        if not self.clips_dir:
            return
        cutoff = time.time() - self.keep_s
        self._clean()
        loaded = []
        for f in self.clips_dir.glob("*.page.json"):
            try:
                d = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            if d.get("end", 0) >= cutoff:
                loaded.append(page_from_dict(d))
        for p in sorted(loaded, key=lambda p: p.start, reverse=True):
            self.pages.append(p)
        if loaded:
            log(f"reloaded {len(loaded)} page(s) from the last {self.cfg.keep_hours:g} h")

    def _clean(self) -> None:
        """Enforce the storage limits (sizeup/janitor.py) and drop expired history from memory."""
        if not self.clips_dir:
            return
        audio_s, history_s = self.cfg.audio_keep_hours * 3600, self.keep_s
        self.storage = janitor.clean(self.clips_dir.parent, self.clips_dir,
                                     Path(self.cfg.p25_dir) if self.cfg.p25_dir else None, audio_s, history_s)
        # (#409) each other system's own capture and temp directories (never on the hub: it has no recorder)
        for sysd in (self.cfg.p25_systems if self.cfg.p25_recorder and self.cfg.p25_dir else ()):
            got = janitor.clean_system(Path(sysd.capture_dir), audio_s)
            self.storage["deleted"] = self.storage.get("deleted", 0) + got["deleted"]
            if got["over"]:
                log(f"storage: {sysd.label}'s captures were over {janitor.SYSTEM_MAX_BYTES / 1e6:.0f} MB: "
                    f"{got['over']} oldest call(s) removed")
        now = time.time()
        with self._lock:
            for cid in [c for c, (_, t) in self.clips.items() if now - t > audio_s]:
                del self.clips[cid]
            while self.pages and self.pages[-1].end < now - history_s:
                self.pages.pop()
        if hasattr(self, "store"):
            self.store.prune(now - history_s)
        if self.storage.get("deleted"):
            log(f"storage: removed {self.storage['deleted']} expired file(s); "
                f"{self.storage['bytes'] / 1e6:.1f} MB in captures/")

    def _janitor(self) -> None:
        while True:
            time.sleep(600)
            try:
                self._clean()
            except Exception as e:  # cleanup must never take the display down
                log(f"storage cleanup error: {type(e).__name__}: {e}")

    @staticmethod
    def _page_doc(page: Page) -> dict:
        """A page as saved (and as sent to the hub): Page.as_dict + what the pipeline added."""
        d = dict(page.as_dict(), dispatch=getattr(page, "dispatch", None),
                 history=getattr(page, "history", None) or [], rerun_at=getattr(page, "rerun_at", None),
                 rerun_result=getattr(page, "rerun_result", ""),
                 model=getattr(page, "model", ""), stt=getattr(page, "stt", ""),
                 read_twice=page.read_twice)
        if getattr(page, "alt_model", ""):
            d.update(alt_text=getattr(page, "alt_text", ""), alt_model=page.alt_model)  # type: ignore[attr-defined]
        return d

    def _save_page(self, page: Page) -> None:
        if not self.clips_dir:
            return
        try:
            (self.clips_dir / f"{page.id}.page.json").write_text(json.dumps(self._page_doc(page)))
        except OSError:
            pass

    # -- the hub link ([cloud] role = "station") ------------------------------------------------------

    def _emit(self, kind: str, data: dict, clip: str | None = None) -> None:
        """Hand one finished thing to the hub link (a no-op all-in-one). Never raises into the radio path."""
        if self.events is None:
            return
        try:
            self.events(kind, data, clip)
        except Exception as e:  # the outbox must never stop the monitor
            log(f"hub link: could not queue {kind}: {type(e).__name__}: {e}")

    def _heard(self, system: str, radio_id: str, alias: str = "", at: float | None = None, channel: str = "") -> None:
        """A radio heard on the air (P25, and an MDC / FleetSync ID on FG1 alike, #163): into the registry, and
        (station) to the hub's registry, once per radio/alias every few minutes (P25 lists the same talkers on
        every call)."""
        self.radios.heard(system, radio_id, alias, at, channel)
        if self.events is None or not radio_id:
            return
        key, now = (system, str(radio_id), alias or ""), time.time()
        if now - self._heard_sent.get(key, 0) < 300:
            return
        self._heard_sent[key] = now
        if len(self._heard_sent) > 5000:
            self._heard_sent = {k: v for k, v in self._heard_sent.items() if now - v < 300}
        self._emit("heard", {"system": system, "radio_id": str(radio_id), "alias": alias or "", "at": at, "channel": channel})

    # -- clips ----------------------------------------------------------------------------------

    def _keep_clip(self, cid: str, audio: np.ndarray, processed: bool = False) -> None:
        # Stored as heard through a radio speaker (de-emphasis, voice band, steady loudness),
        # not the raw discriminator output, which is quiet and dominated by CTCSS + hiss.
        pcm = (np.clip(audio if processed else radio_voice(audio), -1, 1) * 32767).astype(np.int16)
        with self._lock:
            self.clips[cid] = (pcm, time.time())
            while len(self.clips) > 400:
                self.clips.popitem(last=False)
        if self.clips_dir:
            try:
                (self.clips_dir / f"{cid}.wav").write_bytes(wav_bytes(pcm, RATE))
            except OSError:
                pass

    def _prompt(self, item) -> str:
        """whisper's prompt for one recording: the configured text, plus (stt.talker_prompt) the
        talkers on this call as they're said on the air."""
        if self._kind.get(getattr(item, "source", "")) == "airband":
            return self._atc_prompt(getattr(item, "source", ""))
        parts = [self.cfg.stt_prompt]
        if self.cfg.stt_talker_prompt != "off":
            parts.append(talker_prompt(getattr(item, "unit", "") or "", getattr(item, "start", None),
                                       with_time=self.cfg.stt_talker_prompt == "ids+time"))
        return " ".join(p for p in parts if p)

    def _atc_prompt(self, channel: str = "") -> str:
        """whisper's prompt on an airband channel (#233): HPN's words, then the callsigns of the aircraft nearest the field
        as said on the air (shortfinal stt_prompt.py), from the Aircraft layer when it has a fresh picture here; only the
        ones that can be on that frequency (#260, sizeup/atc_scope.py: Ground the field, Approach out to 40 NM)."""
        from sizeup import atc
        from sizeup.atc_scope import planes_for
        if self._atc_vocab is None:
            self._atc_vocab = atc.vocabulary()
        return atc.whisper_prompt(self._atc_vocab["prompt"], planes_for(channel, self._atc_aircraft()), atc.telephony_words())

    def _atc_aircraft(self) -> list[dict]:
        """The aircraft the Aircraft layer has near HPN now (sizeup/aircraft.py: its last picture, read every 15 s while the
        emergency watch runs), for matching a callsign heard on ATC; none when that picture is over a minute old.
        A station forwarding to the hub doesn't read the layer (the hub does): its whisper prompt (the fallback when
        Amazon Transcribe can't) takes the callsigns from the station's own ADS-B receiver instead (#257)."""
        layer = getattr(self, "aircraft", None)
        fetched = layer.st.get("fetched") if layer is not None else None
        if not fetched or time.time() - fetched > 60:
            return self._receiver_aircraft()
        at = layer.st.get("at")      # the picture's feed time: a position's time is that less its age (atc.adsb_check)
        with layer._lock:      # ... and the wider set while NY Approach is heard (#260; each channel takes its own)
            return [dict(a, pos_at=at - a["age"]) if isinstance(at, (int, float)) and isinstance(a.get("age"), (int, float))
                    else dict(a) for a in layer.view + getattr(layer, "wide", [])]

    def _receiver_aircraft(self, max_age_s: float = 30.0) -> list[dict]:
        """The station's own ADS-B picture (sizeup/cloud/aircraftlink.py, read every second to forward to the hub): the
        aircraft with a callsign within RADIUS_NM of HPN and under ATC_CEILING_FT (or on the ground), nearest first."""
        fwd = getattr(getattr(self, "station", None), "aircraft", None)
        last = getattr(fwd, "last", None)
        if not last or time.time() - last[1] > max_age_s:
            return []
        from sizeup.aircraft import ARP, RADIUS_NM, nm
        out = []
        for p in last[0].planes:
            if p.lat is None or p.lon is None or not (p.flight or "").strip():
                continue
            alt = p.alt_baro
            if alt != "ground" and isinstance(alt, (int, float)) and alt > ATC_CEILING_FT:
                continue
            d = nm(ARP[0], ARP[1], p.lat, p.lon)
            if d <= RADIUS_NM:
                out.append({"hex": p.hex, "flight": p.flight.strip(), "reg": p.reg, "dist": round(d, 1),
                            "phase": "ground" if alt == "ground" else ""})
        return sorted(out, key=lambda a: a["dist"])

    def _atc(self, t: Traffic, text: str | None = None) -> None:
        """An airband transcript (new, or new text) to the HPN ATC pre-alert watch (sizeup/atc.py, #233): on the CAD of
        record only (a station forwarding to a hub leaves it to the hub, which has the same transcript)."""
        if self.forwarding():
            return
        from sizeup import atc_scope
        if self._kind.get(t.source) == "airband" and atc_scope.facility(t.source) == "approach" \
                and getattr(self, "aircraft", None) is not None:
            self.aircraft.widen(atc_scope.APPROACH_NM)    # NY Approach's traffic is 10-40 NM out (#260)
        txt = t.text if text is None else text
        if not txt:
            return
        from sizeup.atc_normalize import normalize
        txt = normalize(txt)     # the written form (#257: "RNAV Yankee 34", "TWORF", "cleared to land"); the raw text is kept
        w = self.atc
        if w is not None and w.handles(t.source):
            try:
                w.heard({"id": t.id, "source": t.source, "start": t.start, "end": t.end, "text": txt})
            except Exception as e:  # the pre-alerts must never stop the monitor
                log(f"atc: {t.id}: {type(e).__name__}: {e}")
        # ... and who is talking (#252, sizeup/atc_talkers.py: the callsign heard, matched to ADS-B; speech-derived)
        talkers = getattr(self.cad, "atc_talkers", None)
        rec = None
        if talkers is not None and talkers.handles(t.source):
            try:
                rec = talkers.heard({"id": t.id, "source": t.source, "start": t.start, "end": t.end, "text": txt,
                                     "carrier_db": getattr(t, "carrier_db", None)})
            except Exception as e:  # never stops the monitor
                log(f"atc talkers: {t.id}: {type(e).__name__}: {e}")
        # ... and an abnormal go-around built up over several transmissions (#369, sizeup/airport_watch.py)
        aw = getattr(self.cad, "airport_watch", None)
        if aw is not None and aw.enabled and self._kind.get(t.source) == "airband":
            try:
                aw.heard_atc({"id": t.id, "source": t.source, "start": t.start, "end": t.end, "text": txt}, rec)
            except Exception as e:  # never stops the monitor
                log(f"airport watch: {t.id}: {type(e).__name__}: {e}")

    def _keep_source(self, cid: str, audio: np.ndarray | None) -> None:
        """The exact audio speech-to-text was given (before the listening filter), kept with the
        clip for the same time so a re-run sees what the live pass saw."""
        if not self.clips_dir or audio is None or not len(audio):
            return
        try:
            pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
            (self.clips_dir / f"{cid}.src.wav").write_bytes(wav_bytes(pcm, RATE))
        except OSError:
            pass

    def clip(self, cid: str) -> bytes | None:
        # P25 ids carry trunk-recorder's timestamp ("307-1790954731.970_476112500.0-call_11-t2"):
        # dots are allowed, but nothing that could leave the clips folder.
        if not CLIP_ID.fullmatch(cid) or ".." in cid:
            return None
        with self._lock:
            hit = self.clips.get(cid)
        if hit is not None:
            return wav_bytes(hit[0], RATE)
        p = self.clips_dir / f"{cid}.wav" if self.clips_dir else None
        return p.read_bytes() if p and p.is_file() else None

    # -- radio events -----------------------------------------------------------------------------

    def _mdc(self, *audios) -> list[tuple[str, bool]]:
        """(radio ID, emergency) for each radio ID found in these pieces of audio, in order: MDC-1200 (Motorola,
        "6401") and FleetSync (Kenwood, "167-1002": radio/fleetsync.py). An emergency packet is the radio's
        emergency button (#128); MDC's emergency acknowledge (the console's answer) is not."""
        out: list[tuple[str, bool]] = []
        for b in audios:
            found: list[tuple[str, bool]] = []
            try:
                found += [(p.unit_hex, p.kind == "emergency") for p in mdc_decode(b, RATE)
                          if p.kind in ("pre-id", "post-id", "emergency")]
            except Exception:
                pass
            try:            # a long transmission: its first and last 3 s (where a radio sends its ID)
                edge = 3 * RATE
                for part in ([b] if len(b) <= 2 * edge else [b[:edge], b[-edge:]]):
                    found += [(p.id, p.emergency) for p in fleetsync.decode(part, RATE)
                              if p.kind in ("pre-id", "post-id", "emergency")]
            except Exception:
                pass
            for x in found:
                if x not in out:
                    out.append(x)
        return out

    def _units(self, seg: Segment, found: list[tuple[str, bool]] | None = None) -> list[str]:
        """Who keyed up: MDC-1200 PTT-IDs in the bursts cut from the transmission's ends, and anywhere
        in the transmission itself (a burst the edge cut missed still names the radio; packets are
        CRC-checked, so this can't invent an ID)."""
        out: list[str] = []
        for hexid, emergency in (self._mdc(*seg.bursts, seg.audio) if found is None else found):
            name = self.cfg.mdc_aliases.get(hexid, hexid)
            name = f"{name} EMERGENCY" if emergency else name
            if name not in out:
                out.append(name)
        return out

    def _id_note(self, label: str, seg: Segment, units: list[str]) -> str:
        """Why an analog key-up has no radio ID, when it hasn't (#163; stored on the transmission, radio_traffic.id_note):

            undecoded   an MDC / FleetSync burst was there (both tones of a pair, well over the dead air: radio/fm.py
                        id_burst_frames) at the key-up (the live tap's window, pre-roll included) or at either end of
                        the carrier (the bursts the squelch cut off), or an MDC sync there failed its CRC: "ID not decoded"
            none        no burst anywhere in a strong transmission: the radio sent none ("No ID sent": PTT ID off in
                        its codeplug, a radio from elsewhere)
            weak        no burst, but the carrier quieted the hiss under NO_ID_MIN_QUIETING_DB: a burst may be lost in
                        the noise, so nothing is said either way
            ''          the squelch cut something data-like off an edge (most of it at 1000-2100 Hz) that isn't both
                        tones of a pair and has no MDC sync: a roger beep, or a burst too broken to tell. No claim.

        A burst anywhere else (mid-transmission) is not counted (a siren, a tone): it says nothing about the key-up."""
        if units:
            return ""
        floor = voice_floor_db(seg.audio)
        edge = int(ID_EDGE_S * RATE)        # a FleetSync 2400 burst (1200/2400 Hz): the squelch's edge cut leaves it
        seen = (self.tap.burst(label, seg.start, seg.end) or any(any(id_burst_frames(b, floor)) for b in seg.bursts)
                or any(any(id_burst_frames(x, floor)) for x in (seg.audio[:edge], seg.audio[-edge:])))
        bad = 0
        for b in seg.bursts:
            try:
                bad += sum(not p.crc_ok for p in mdc_decode(b, RATE, only_valid=False))
            except Exception:
                pass
        if seen or bad:
            log(f"{label}: radio ID burst heard but not decoded ({len(seg.bursts)} cut burst(s)"
                + (f", {bad} MDC sync hit(s) failed the CRC" if bad else "") + ")")
            return "undecoded"
        if seg.bursts:
            return ""
        return "none" if seg.quieting_db >= NO_ID_MIN_QUIETING_DB else "weak"

    def _keep_bursts(self, cid: str, seg: Segment) -> None:
        """The cut-off MDC bursts, next to the clip (same 12 h limit), so the ID can be decoded again."""
        if not self.clips_dir or not seg.bursts:
            return
        gap = np.zeros(RATE // 20, dtype=np.float32)
        audio = np.concatenate([x for b in seg.bursts for x in (np.asarray(b, np.float32), gap)])
        try:
            (self.clips_dir / f"{cid}.mdc.wav").write_bytes(wav_bytes((np.clip(audio, -1, 1) * 32767).astype(np.int16), RATE))
        except OSError:
            pass

    def _segment(self, ch: Channel, seg: Segment) -> None:
        if self._kind.get(ch.label) == "paging":
            return self._page_segment(ch, seg)
        if self._kind.get(ch.label) == "airband":
            return self._airband_segment(ch, seg)
        found = self._mdc(*seg.bursts, seg.audio)
        # An ID decoded live at key-up (radio/tap.py: a pre-ID the squelch cut off the segment) first, then
        # the segment's own (its edges, a post-ID at release): different ones are all kept, never one picked.
        pre = self.tap.ids(ch.label, seg.start, seg.end)
        found = pre + [x for x in found if x not in pre]
        units = self._units(seg, found)
        record = self.channel_pref(ch.label)["record"]
        id_note = "" if not record else self._id_note(ch.label, seg, units)
        # Weak signals: speech can sit only 2-6 dB over the dead air (FG1 bench, indoor antenna),
        # so nothing is skipped on energy. Everything is transcribed; a result that is one of
        # whisper's dead-air phrases ("Thank you.", "Yes.") on audio with no voice energy is a
        # mic click (stt.Transcriber), dropped in _transcribed.
        low_energy = speech_seconds(seg.audio) < MIN_SPEECH_S
        ids = list(dict.fromkeys(h for h, _e in found))
        for h in ids:
            self._heard(fm_system(h), h, at=seg.end, channel=ch.label)
        self.live.fm_segment(ch.label, ids, seg.start, seg.end)
        emergency = any(e for _h, e in found)        # the radio's emergency button (MDC / FleetSync, #128)
        live_key = self.tap.alarm_key(ch.label, seg.start, seg.end) if emergency else ""
        if not record:              # squelch, MDC IDs and "talking now" stay live; nothing is kept
            if emergency and not live_key:      # ... but the button still alarms (the tap missed it live)
                self._fm_emergency(ch.label, seg.start, found)
            return
        t = Traffic(f"{ch.label.replace(' ', '')}-{int(seg.start * 1000)}", ch.label, seg.start, seg.end,
                    "", status="pending", unit=", ".join(units), rig=self.radios.rig_mdc(ids), radio_id=", ".join(ids))
        t.low_energy = low_energy  # type: ignore[attr-defined]
        if id_note:
            t.id_note = id_note    # type: ignore[attr-defined]
        self._keep_bursts(t.id, seg)
        t.raw_audio = seg.audio    # type: ignore[attr-defined]  (saved if it turns out to be a click)
        self._keep_clip(t.id, seg.audio)
        self._keep_source(t.id, seg.audio)
        if emergency:
            t.emergency = True                                   # type: ignore[attr-defined]
            t.emergency_via = self._via(found)                   # type: ignore[attr-defined]
            if live_key:
                t.alarm_key = live_key                           # type: ignore[attr-defined]  (the alarm raised at key-up)
            self._alarm_now(t)          # its clip now; one alarm per transmission (the key-up's, if there was one)
        self._stt(t, seg.audio, self.stt_engine(ch.label), ch.label)

    def _airband_segment(self, ch: Channel, seg: Segment) -> None:
        """A transmission on an airband channel (HPN ATC, #233): AM (no de-emphasis), no radio IDs, kept and transcribed
        like traffic (Settings > Radios: AWS by default). A weak carrier (sizeup/radio/am.py MIN_CARRIER_DB, 3 dB) is kept
        and playable but not transcribed: speech-to-text invents words from noise (shortfinal's rule); Re-transcribe can.
        A marginal one (under MARGINAL_DB, 6 dB) is transcribed and flagged (#257)."""
        if not self.channel_pref(ch.label)["record"]:
            return
        t = Traffic(f"{ch.label.replace(' ', '')}-{int(seg.start * 1000)}", ch.label, seg.start, seg.end, "", status="pending")
        t.fm = False                                      # type: ignore[attr-defined]
        t.mhz = round(float(ch.mhz), 4)                   # type: ignore[attr-defined]  (the frequency it was heard on: #260)
        t.carrier_db = round(float(seg.quieting_db), 1)   # type: ignore[attr-defined]  (the tower's is steady: #252)
        # #270: the level, the floor and the voice's SNR of each transmission, sent to the hub with it (its traffic doc),
        # so a weak transmitter can be told from a missing one there; and one line here
        for k in ("carrier_dbfs", "snr_db", "opened"):
            if getattr(seg, k, None) is not None:
                setattr(t, k, getattr(seg, k))
        if getattr(seg, "tones", None):
            # #263: the tones notched out (not "tones": on a row that is a page's tone pair)
            t.notched_hz = list(seg.tones)                # type: ignore[attr-defined]
        if seg.hiss_db is not None:
            t.floor_dbfs = round(float(seg.hiss_db), 1)   # type: ignore[attr-defined]
        log(f"{ch.label}: carrier {t.carrier_db:+.1f} dB over the floor"
            f" ({getattr(t, 'carrier_dbfs', '?')} / {getattr(t, 'floor_dbfs', '?')} dBFS), voice SNR "
            f"{'?' if getattr(t, 'snr_db', None) is None else f'{t.snr_db:.1f} dB'}, {seg.end - seg.start:.1f} s"
            + (", opened on a sustained weak carrier" if getattr(seg, "opened", "") == "sustained" else "")
            + (f", tones notched {', '.join(f'{f:g}' for f in seg.tones)} Hz" if getattr(seg, "tones", None) else ""))
        t.low_energy = speech_seconds(seg.audio) < MIN_SPEECH_S   # type: ignore[attr-defined]
        self._keep_clip(t.id, radio_voice(seg.audio, deemph=False), processed=True)
        self._keep_source(t.id, seg.audio)
        if getattr(seg, "weak", False):
            t.stt = ""                                    # type: ignore[attr-defined]
            t.raw, t.confidence, t.status = "", None, "off"   # type: ignore[attr-defined]
            t.model = f"weak signal (carrier {seg.quieting_db:.1f} dB): not transcribed"   # type: ignore[attr-defined]
            return self._transcribed(t)
        # A marginal carrier (seg.marginal, #257) is transcribed like the rest: Amazon Transcribe answers nothing for
        # noise rather than invent words, and the row says the words may be wrong (sizeup/atc_talkers.py decorate)
        self._stt(t, seg.audio, self.stt_engine(ch.label), ch.label)

    def _page_segment(self, ch: Channel, seg: Segment) -> None:
        """A transmission on a paging channel: a new page, or (after a fade) more of the one before.
        A page shows as pending at once; it is finalised (_page_done) when the squelch's continuation
        window has passed (PageHold, GitHub #61), or at once when there is none."""
        if not self.channel_pref(ch.label)["record"]:   # Settings > Radios: not recorded (receiver keeps running)
            log(f"{ch.label}: {assemble(ch.label, ch.mhz, seg, self.plan).kind} not recorded "
                "(Record is off in Settings > Radios)")
            return
        done, touched = self.pagehold.add(ch.label, ch.mhz, seg)
        finished = {id(p) for p in done}
        for page, joined in touched:
            self._keep_clip(page.id, page.audio)        # joined: the clip grows with the page
            if joined:
                continue
            log(f"{ch.label}: {page.kind} " + (", ".join(t.describe() for t in page.tones) or "voice"))
            if id(page) not in finished:
                self._show_page(page)                   # pending until its window passes
        for page in done:
            self._page_done(page)

    def _show_page(self, page: Page) -> None:
        with self._lock:
            if not getattr(page, "shown", False):
                page.shown = True  # type: ignore[attr-defined]
                self.pages.appendleft(page)

    def _page_done(self, page: Page) -> None:
        """A paging recording is complete: save it, send it to the hub, transcribe it."""
        log(recording_note(page))
        heard = self._page_heard(page)
        eng = self.stt_engine(page.label) if heard else ""
        page.stt = eng if eng in ("aws", "both") else ""  # type: ignore[attr-defined]
        if eng == "aws" and self.forwarding():
            page.status = "remote"          # the hub transcribes it (Amazon Transcribe)
        self._emit("page", self._page_doc(page), page.id)
        if not heard:
            return
        self._keep_source(page.id, page.voice)
        self._stt(page, page.voice, eng, page.label)

    def _release_pages(self, now: float | None = None) -> None:
        """Pages whose continuation window has passed (or is still being added to, while the squelch
        is open on the continuation): finalise them."""
        if not self.pagehold.held:
            return

        def busy(label: str) -> bool:
            r = self._receiver(label)
            return r is not None and r.squelch.open and r.squelch.continuing
        for page in self.pagehold.due(time.time() if now is None else now, busy):
            self._page_done(page)

    def _page_heard(self, page: Page) -> bool:
        """A page just off the air (here, or on the hub from the station). False = tones only:
        nothing to transcribe (kept as the tones half of a page whose voice comes separately)."""
        self._show_page(page)
        self.paging.finished(page.label, page.id, page.end)   # the page going out live hands over to this one
        if any(not t.names for t in page.tones):
            self._unknown_tones(page)                   # once per page (the live display never logs)
        self._page_to_cad(page)                         # a row in Radio, with play, from now on (#64)
        if page.tones:
            # The tones a voice-only transmission after this may belong to: the last tones heard, whoever's (#183: a
            # voice after West Harrison's tones-only page never takes Port Chester's from the one before it)
            self._last_tones_only = page if page.status == "no_voice" else None
        if page.status != "no_voice":
            return True
        self._save_page(page)
        return False

    def _unknown_tones(self, page: Page) -> None:
        """Pairs nobody's code matches, kept to be named later (data/westchester_tones.csv): the log, and
        a line per page in unknown_tones.jsonl next to the clips {at, channel, page, pairs, named}."""
        unknown = [t for t in page.tones if not t.names]
        log(f"  {page.label}: unknown tone pair(s) kept for learning: "
            + ", ".join(f"{t.a_hz:.1f}/{(t.b_hz or 0):.1f} Hz" for t in unknown))
        if not self.clips_dir:
            return
        row = {"at": round(page.start, 2), "time": tz.clock(page.start, "%Y-%m-%d %H:%M:%S"),
               "channel": page.label, "page": page.id,
               "pairs": [[round(t.a_hz, 1), round(t.b_hz, 1) if t.b_hz else None] for t in unknown],
               "named": [n for t in page.tones for n in t.names]}       # stacked with these (who else was paged)
        try:
            with open(self.clips_dir.parent / "unknown_tones.jsonl", "a") as f:
                f.write(json.dumps(row) + "\n")
        except OSError:
            pass

    def _page_to_cad(self, page: Page, replace: bool = True) -> None:
        """A page into the CAD's communications log: a row in Radio like any transmission (talker 60
        Control, its tone pairs, where the voice starts), so it plays there and from the event it opens. A pair from
        a page before this one's message (one recording of two pages, made before #183) is marked other_page: shown,
        never what makes the row ours (comms.page_kind)."""
        own = page.message_tones
        try:
            self.cad.record_traffic({"id": page.id, "start": page.start, "end": page.end, "source": page.label,
                                     "talkgroup": None, "unit": "60 Control" if page.tones else "", "rig": "", "radio_id": "",
                                     "text": page.raw, "status": page.status, "model": getattr(page, "model", ""),
                                     "tones": [{"a_hz": round(t.a_hz, 1), "b_hz": round(t.b_hz, 1) if t.b_hz else None,
                                                "names": list(t.names), "ours": self.paging.is_ours(t.names)}
                                               | ({} if any(t is m for m in own) else {"other_page": True})
                                               for t in page.tones],
                                     "tone_seq": list(getattr(page, "tone_seq", None) or []),
                                     "voice_at": round(page.voice_offset_s, 2) if page.tones else None},
                                    replace=replace)
        except Exception as e:  # the CAD must never stop the monitor
            log(f"CAD: could not log a page: {type(e).__name__}: {e}")

    def _call(self, call: Call) -> None:
        flagged = call.emergency or any(t.emergency for t in call.turns)    # trunk-recorder's flag on this call itself
        windows = self.cad.alarms.windows
        emergency, stuck, keep_record = flagged, "", flagged
        if flagged:
            # 5.2.0's flag is the call's, from the grant that opened it: its first key-up's radio (srcList's own
            # flags, when a later trunk-recorder fills them, name theirs)
            first = next((t for t in call.turns if t.src), None) if call.emergency else None
            turns = [t for t in call.turns if t.emergency and t.src] + ([first] if first is not None else [])
            pressed = list(dict.fromkeys(t.src for t in turns))
            ours = self._p25_ours(call, turns)
            v = windows.flag(call.talkgroup, pressed, call.end, self._flag_length(call), ours)
            if v["stuck"]:
                # Someone else's flag on call after call (#190): a radio left in emergency mode, or a talkgroup the
                # system holds in emergency. Raised once (its first flags); now it is ordinary traffic, kept only as
                # Record (or a window still running) says, and it renews nothing.
                emergency = False
                names = {t.src: t.talker for t in call.turns if t.src}
                stuck = (f"{call.label} (TG {call.talkgroup})" if v["stuck"] == "talkgroup"
                         else ", ".join(f"{names[r]} ({r})" if names.get(r) and names[r] != r else r for r in v["radios"]))
                keep_record = v["first"]                # its record: the one that showed it stuck, for audit
                if v["first"]:
                    log(f"p25: {call.label} {call.id}: emergency flag STUCK on {stuck}: not an emergency from now on "
                        f"(no new alarm, the window not renewed) until it has been quiet {STUCK_WITHIN_S // 60:.0f} min")
                    # its alarm marked now, whether or not this call is kept (here, or the hub: an "alarm" event that
                    # says it isn't one, carrying the mark)
                    mark = Traffic(call.id, call.label, call.start, call.end, "", status="pending",
                                   unit=", ".join(call.units), talkgroup=call.talkgroup, radio_id=", ".join(v["radios"]))
                    mark.emergency, mark.flag_stuck = False, stuck  # type: ignore[attr-defined]
                    self._alarm_now(mark)
            else:
                log(f"p25: {call.label} {call.id}: EMERGENCY flag (radio {', '.join(call.units) or 'unknown'})")
                # The emergency window (alarms.Windows): this talkgroup, and the radio that pressed it anywhere, kept
                # for [p25] emergency_window_min after this call (someone else's: at most WINDOW_MAX_KEPT calls).
                windows.open(call.talkgroup, call.label, pressed, call.end, ours=ours)
        kept = ""
        if not self.tg.record(call.talkgroup):  # switched off in the web app: dropped on arrival ...
            # ... but an emergency call is kept whatever the switch says (issue #128), and so is everything on its
            # talkgroup or from its radio until the window ends (All clear, or emergency_window_min later)
            why = "button" if emergency else windows.keeps(call.talkgroup, [t.src for t in call.turns if t.src], call.start)
            if not why:
                self.tg.seen(call.talkgroup, dropped=True)
                self._drop_recorder_files(call, keep_record)
                return
            kept = "emergency"
            if not emergency:
                log(f"p25: {call.label} {call.id}: Record is off, kept: emergency window "
                    f"({'its talkgroup' if why == 'talkgroup' else 'the radio that pressed it'})")
        evidence = self._p25_evidence(call) if emergency else None
        self.tg.seen(call.talkgroup)
        for _name, (src, tag) in call.ids.items():      # radio ID <-> alias, kept for lookups without the alias
            self._heard("p25", src, tag, call.start, call.label)
        key = channels.tg_key(call.talkgroup)
        eng = self.stt_engine(key)
        if call.audio is not None:
            # One row per key-up (issue #62): 60 Control and the unit it's talking to, a unit keying
            # twice, a radio with no ID; each piece is transcribed with only its own talker as the hint.
            for pc in split_turns(call):
                t = Traffic(pc.id, call.label, pc.start, pc.end, "", status="pending", unit=pc.talker,
                            talkgroup=call.talkgroup, rig=self.radios.rig_p25(pc.pairs),
                            radio_id=", ".join(src for src, _t in pc.pairs if src))
                t.fm = False  # decoded P25 voice: no FM de-emphasis  # type: ignore[attr-defined]
                t.pairs = pc.pairs  # (radio ID, alias) per talker, for the hub's own rig lookup  # type: ignore[attr-defined]
                self._keep_clip(t.id, radio_voice(pc.audio, deemph=False), processed=True)
                self._keep_source(t.id, pc.audio)
                if kept:
                    t.kept = kept  # on a talkgroup switched off: kept for the emergency  # type: ignore[attr-defined]
                if pc.emergency and stuck:
                    t.flag_stuck = stuck  # flagged, but stuck (#190): its alarm is marked, once  # type: ignore[attr-defined]
                elif pc.emergency:
                    t.emergency = True  # the P25 emergency button (#128)  # type: ignore[attr-defined]
                    if evidence:
                        t.evidence = evidence  # trunk-recorder's call record, for audit  # type: ignore[attr-defined]
                    self._alarm_now(t)      # before speech-to-text: the alarm doesn't wait for the words
                if pc.stt:
                    self._stt(t, pc.audio, eng, key)
                else:                   # a sub-0.3 s key-up with no voice: kept, not transcribed
                    t.raw, t.confidence, t.status, t.model = "", None, "no_speech", ""  # type: ignore[attr-defined]
                    t.stt = ""  # type: ignore[attr-defined]
                    self._transcribed(t)
        self._drop_recorder_files(call, keep_record)  # sizeup's clip is the one copy kept

    def _call_system(self, rt, call: Call) -> None:
        """A finished call on another P25 system (#409, sizeup/radio/systems.py), by its talkgroup's tier
        (sizeup/tiers.py). Context only: never Main, never on our incident; its radio IDs are another system's, so
        never looked up in our registry (no rig) nor learned into it; its emergency flags raise none of our alarms."""
        name, tg = rt.system.name, call.talkgroup
        tier = self.tg.tier(tg, name)
        if call.encrypted and tier in tiers.RECORDED:
            # the call says it was encrypted, whatever the file says of its talkgroup: nothing of it is kept
            log(f"p25 {name}: {call.label} {call.id}: encrypted call on a talkgroup listed as clear: not kept")
            tier = tiers.ACTIVITY
        if tier not in tiers.RECORDED:               # off, or encrypted (never recorded: nothing to keep)
            self.tg.seen(tg, dropped=True, system=name)
            self._drop_recorder_files(call, False)
            return
        self.tg.seen(tg, system=name)
        info = rt.system.talkgroups.get(tg, {})
        key = sources.channel_key(name, tg)
        eng = "off" if tier == tiers.RECORD else channels.effective(
            dict(self.channel_pref(key), transcribe=True), self.aws_ok(key)[0])
        if call.audio is not None:
            for pc in split_turns(call):
                t = Traffic(pc.id, call.label, pc.start, pc.end, "", status="pending", unit=pc.talker,
                            talkgroup=call.talkgroup, rig="", radio_id=", ".join(src for src, _t in pc.pairs if src))
                t.fm = False  # type: ignore[attr-defined]
                # the row's source (#415): its system, never a talkgroup number alone; context, so never "ours"
                t.system, t.tier, t.ours = name, tier, False  # type: ignore[attr-defined]
                t.source_key = sources.p25(name, tg)  # type: ignore[attr-defined]
                t.group, t.service = info.get("group") or "", info.get("service") or "other"  # type: ignore[attr-defined]
                t.dept = info.get("department") or ""  # type: ignore[attr-defined]
                self._keep_clip(t.id, radio_voice(pc.audio, deemph=False), processed=True)
                self._keep_source(t.id, pc.audio)
                if pc.stt:
                    self._stt(t, pc.audio, eng, key)
                else:
                    t.raw, t.confidence, t.status, t.model = "", None, "no_speech", ""  # type: ignore[attr-defined]
                    t.stt = ""  # type: ignore[attr-defined]
                    self._transcribed(t)
        self._drop_recorder_files(call, False)

    def _p25_activity(self, source: str, minute: int, calls: int, radios: list[str]) -> None:
        """A minute's key-ups on an encrypted talkgroup (#409): to the radio log's activity table (the hub's, from a
        station). Never a transmission row: nothing of it is recorded."""
        if self.forwarding():
            return self._emit("activity", {"source": source, "minute": minute, "calls": calls, "radios": radios})
        try:
            self.cad.comms.activity(source, minute, calls, radios)
        except Exception as e:  # noqa: BLE001 - the CAD must never stop the monitor
            log(f"CAD: could not log activity on {source}: {type(e).__name__}: {e}")

    def talkgroups_state(self) -> list[dict]:
        """TalkgroupPrefs.state(), each row with a tier: its own (another system's), else what its switches amount to
        (sizeup/tiers.py derived; "tier_set": False: changed by Record / Transcribe, not by a tier)."""
        rows = self.tg.state()
        for r in rows:
            r["tier_set"] = r["tier"] is not None
            if r["tier"] is None:
                r["tier"] = tiers.derived(r["record"], self.channel_pref(r["key"])["transcribe"],
                                          r["encrypted"])
        return rows

    def p25_systems_state(self) -> list[dict]:
        """state()["p25_systems"] (#409): each other system with its jobs and dongles, what runs, its control channel,
        the unknown talkgroups seen on it and its encrypted activity. [] with none (the hub shows its station's)."""
        return [rt.state(self.assign) for rt in self.systems.values()]

    def set_talkgroup_tier(self, tg: int, tier: str, system: str = "") -> str | None:
        err = self.tg.set_tier(int(tg), str(tier), system)
        if not err:
            log(f"talkgroup {system}:{tg} ({self.tg.name(int(tg), system)}): tier={self.tg.tier(int(tg), system)}")
        return err

    def _p25_ours(self, call: Call, turns) -> bool:
        """The flag is ours (alarms.counts_as_ours): its talkgroup is one of our fire channels, or the radio that
        flagged is on one of our rigs. Never second-guessed (Windows.flag)."""
        rig = self.radios.rig_p25([(t.src, t.tag or t.talker) for t in turns if t.src])
        try:
            return self.cad.alarms.counts_as_ours({"talkgroup": call.talkgroup, "source": call.label, "rig": rig})
        except Exception as e:  # noqa: BLE001 - when in doubt, ours: an emergency is never dropped for a lookup
            log(f"p25: {call.id}: ours? {type(e).__name__}: {e}")
            return True

    @staticmethod
    def _flag_length(call: Call) -> float:
        """How long the flagged key-up ran (5.2.0: the call's first; srcList's own flags: the longest of them), the
        whole call when the key-ups have no lengths: a routine-length one counts toward a stuck flag."""
        first = call.turns[0] if call.emergency and call.turns else None
        lens = [t.length for t in call.turns if (t.emergency or t is first) and t.length is not None]
        return max(lens) if lens else max(0.0, call.end - call.start)

    # -- the emergency alarms (sizeup/cad/alarms.py, #128) ------------------------------------------------

    def _alarm(self, t: Traffic, text: str | None = None) -> None:
        """A transmission (new, or new text) to the alarms: the CAD of record raises them. A station forwarding
        to a hub doesn't (the hub does, from the same transmission). Another P25 system's (#409) raises none,
        whichever way it comes (new, a re-run's text, the hub's copy)."""
        if self.forwarding() or getattr(t, "system", ""):
            return
        d = {"id": t.id, "start": t.start, "end": t.end, "source": t.source, "talkgroup": t.talkgroup, "unit": t.unit,
             "rig": t.rig, "radio_id": t.radio_id, "text": t.text if text is None else text,
             "emergency": bool(getattr(t, "emergency", False)), "via": getattr(t, "emergency_via", ""),
             "alarm_key": getattr(t, "alarm_key", ""), "stuck": getattr(t, "flag_stuck", "")}
        d["unit"] = ", ".join(x.removesuffix(" EMERGENCY") for x in (t.unit or "").split(", ") if x)
        try:
            self.cad.alarms.on_traffic(d)
        except Exception as e:  # the alarms must never stop the monitor (it logs; the next text tries again)
            log(f"alarms: {t.id}: {type(e).__name__}: {e}")

    def _alarm_now(self, t: Traffic) -> None:
        """The emergency button (P25 at the end of its call; MDC / FleetSync at the key-up and again at the end
        of the transmission): raised here, or sent to the hub at once (an "alarm" event with its clip when
        there is one yet, ahead of the transcript)."""
        if self.forwarding():
            self._emit("alarm", {k: v for k, v in t.as_dict().items() if k not in ("pairs", "raw_audio")}
                       | {"pairs": [list(p) for p in getattr(t, "pairs", None) or []]},
                       t.id if self.has_clip(t.id) else None)
        else:
            self._alarm(t, "")

    @staticmethod
    def _via(found: list[tuple[str, bool]]) -> str:
        em = [h for h, e in found if e]
        return "FleetSync" if em and fm_system(em[0]) == "fleetsync" else "MDC"

    def _fm_emergency(self, label: str, since: float, found: list[tuple[str, bool]]) -> str:
        """An emergency packet on an analog channel (the live tap, at the key-up; or the end of a transmission
        that isn't kept): the alarm now. Its key is the carrier's; the finished transmission takes it over."""
        ids = list(dict.fromkeys(h for h, _e in found))
        em = [h for h, e in found if e]
        names = list(dict.fromkeys(self.cfg.mdc_aliases.get(h, h) for h in em + ids))
        key = f"{label.replace(' ', '')}-live-{int(since * 1000)}"
        t = Traffic(key, label, since, max(since, time.time()), "", status="pending", unit=", ".join(names[:1]),
                    rig=self.radios.rig_mdc(em or ids), radio_id=", ".join(em or ids))
        t.emergency, t.emergency_via = True, self._via(found)   # type: ignore[attr-defined]
        log(f"{label}: EMERGENCY button ({t.emergency_via}) from {', '.join(em)} at key-up")
        self._alarm_now(t)
        return key

    def _drop_recorder_files(self, call: Call, keep_record: bool | None = None) -> None:
        """trunk-recorder's WAV and JSON, once sizeup has its own clip. An emergency call's JSON is kept instead, in
        captures/emergency/ (janitor.EMERGENCY_KEEP_S, 90 days, like its alarm): the raw record to audit it by.
        keep_record: whether to (None: any flagged call; a stuck flag's: only the call that showed it stuck)."""
        if not self.clips_dir or not call.wav_path:  # without our own clip store, keep the originals
            return
        wav = Path(call.wav_path)
        meta = wav.with_suffix(".json")
        if keep_record is None:
            keep_record = call.emergency or any(t.emergency for t in call.turns)
        if keep_record:
            keep = self.clips_dir.parent / janitor.EMERGENCY_DIR
            try:
                keep.mkdir(parents=True, exist_ok=True)
                shutil.move(str(meta), str(keep / meta.name))
                log(f"p25: {call.id}: emergency call record kept: {keep / meta.name}")
            except OSError as e:
                log(f"p25: {call.id}: emergency call record not kept: {type(e).__name__}: {e}")
        for f in (wav, meta):
            try:
                f.unlink()
            except OSError:
                pass

    @staticmethod
    def _p25_evidence(call: Call) -> dict | None:
        """An emergency call's own record from trunk-recorder (its JSON: srcList with every radio ID and its emergency
        flag, freqList, the call's flags), carried with the flagged transmission to the CAD (radio_traffic.evidence)
        so it can be audited on the hub too; the lists at most EVIDENCE_LIST entries."""
        if not call.wav_path:
            return None
        meta = Path(call.wav_path).with_suffix(".json")
        try:
            d = json.loads(meta.read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(d, dict):
            return None
        for k in ("srcList", "freqList"):
            if isinstance(d.get(k), list):
                d[k] = d[k][:EVIDENCE_LIST]
        d["file"] = meta.name
        return d

    def emergency_all_clear(self, tg=None, radio_ids=None) -> bool:
        """The alarm's All clear (from the hub, or this CAD): the emergency window on talkgroup tg and its radio(s)
        closed now, so a talkgroup switched off is dropped again. True when one was open here."""
        ids = [str(r) for r in radio_ids or [] if str(r).strip()]
        closed = self.cad.alarms.windows.clear(tg, ids)
        log(f"emergency window: all clear for TG {tg}" + (f", radio {', '.join(ids)}" if ids else "")
            + ("" if closed else " (none open here)"))
        return closed

    def _all_clear_sent(self, a: dict) -> None:
        """Alarms.on_clear: all-in-one, the window here is the alarms' own (closed already); a hub sends it on."""
        return None

    def _transcribed(self, item) -> None:
        self._transcribed_one(item)
        wait = (self._aws_wait.pop(getattr(item, "id", ""), None)
                if self._aws_wait and not isinstance(item, Rerun) else None)
        if wait is not None and getattr(item, "status", "") in ("done", "no_speech"):
            self._aws_alt(item, *wait)          # "both": the local text is in; Amazon Transcribe's next

    def _transcribed_one(self, item) -> None:
        if isinstance(item, Traffic):
            item.text = getattr(item, "raw", "")
            # attach: the station's talkgroup choice when this came from one (hub); pairs: P25 talkers
            attach = item.__dict__.pop("attach", None)
            other = getattr(item, "system", "")          # another P25 system's (#409): never attached
            attach = (not other and (item.talkgroup is None or self.tg.main(item.talkgroup))) if attach is None \
                else bool(attach) and not other
            if item.talkgroup is not None:
                item.ours = attach  # type: ignore[attr-defined]   (in "Our calls", #415: the station's Main choice)
            pairs = item.__dict__.pop("pairs", None)
            if item.status == "no_speech":
                # A key-up with nothing said (mic click): still shown, as who keyed and when, with
                # its clip; just no transcript.
                item.text = ""
                log(f"{item.source}: key-up ({item.end - item.start:.1f} s, no speech)"
                    + (f" · unit {item.unit}" if item.unit else ""))
            if hasattr(item, "raw_audio"):
                del item.raw_audio
            # which model wrote this text (the station's, when the hub replays it)
            item.model = getattr(item, "model", "") or Path(self.cfg.stt_model).name  # type: ignore[attr-defined]
            self._emit("traffic", dict(item.as_dict(), attach=attach, pairs=pairs), item.id)
            self.store.on_traffic(item, attach=attach)
            self._to_cad(item)
            if not getattr(item, "system", ""):      # another system's traffic (#409) raises none of our alarms
                self._alarm(item)
            self._atc(item)
            if item.status != "no_speech":
                log(f"{item.source}: {'[' + item.unit + '] ' if item.unit else ''}{item.text or '(' + item.status + ')'}")
            return
        if isinstance(item, Rerun):
            return self._rerun_done(item)
        page: Page = item
        page.model = getattr(page, "model", "") or Path(self.cfg.stt_model).name  # type: ignore[attr-defined]
        d = self._dispatch(page.raw)
        page.dispatch = d.as_dict() if d else None  # type: ignore[attr-defined]
        self._save_page(page)
        self._page_to_cad(page)                     # its text in the log (before an event links it)
        self._emit("page_text", self._page_doc(page))
        log(f"{page.label} {page.kind}: {page.raw or '(' + page.status + ')'}"
            + {True: " [read twice]", False: " [not read twice]"}.get(page.read_twice, ""))
        if d is None:
            return
        # Ours by this page's own tones (#183): the group its message answers, never a page's before it
        depts = page.message_departments
        if len(page.message_tones) < len(page.tones):
            log(f"  {page.label}: tones of an earlier page in this recording left out: "
                + ", ".join(t.describe() for t in page.tones if not any(t is m for m in page.message_tones)))
        tones_half = None
        if page.kind == "voice":
            prev = self._last_tones_only
            if prev and prev.label == page.label and page.start - prev.end < SPLIT_PAGE_S:
                depts = prev.message_departments  # the voice half of a tones-only page
                self._last_tones_only = None
                tones_half = prev
        # Ours only by our tones (#80: never by the address or the units named); anything else stays a page
        # row in Radio and the feed (and a voice-only page is traffic too).
        inc = self._page_incident(page, depts, d, tones_half)
        if inc is None and page.kind == "voice":
            t = Traffic(page.id, page.label, page.start, page.end, page.raw)
            self.store.on_traffic(t)
            self._to_cad(t)
            return
        if inc is None and depts and not self.store.is_ours(depts):
            log(f"{page.label}: {', '.join(depts)} paged: not ours")

    def _page_incident(self, page: Page, depts: list[str], d, tones_half: Page | None = None):
        """The page into the monitor's incidents and, through it, the CAD's queue: only when its tones
        name one of ours (IncidentStore.on_page). Returns the incident, or None."""
        inc = self.store.on_page(page.id, depts, d, at=page.start)
        if inc:
            where = inc.geo["label"] if inc.geo else (inc.location or "location not found")
            if inc.geo and inc.geo.get("confirmed") is False:
                where = f"{inc.location or where} (location unconfirmed: {inc.geo.get('why') or 'not sure'}; best {where})"
            log(f"INCIDENT {inc.incident_type} · {where} · {', '.join(inc.departments)}")
            try:
                ev = self.cad.from_monitor(inc)
                if ev and tones_half is not None:       # its tones came as a transmission of their own
                    self.cad.comms.link_pages(ev["id"], [tones_half.id])
            except Exception as e:  # the CAD must never stop the monitor
                log(f"CAD: could not open a pending incident: {type(e).__name__}: {e}")
        return inc

    # -- tone pairs named from the console (Settings "tones", #80) ------------------------------------------

    def _apply_named_tones(self) -> None:
        """The plan = the tone book (sizeup/cad/tonelist.py, #122): the config's codes read as entities (places +
        service), with the edits from Settings > Radios > Tones on them and the pairs named there, each pair named
        "Places · Service[ · Role]"; ours = the ours entities' names (+ the config's), the CAD's queue = PCFD's and
        Rye Brook's (monitor_names)."""
        self._tone_ov = self.cad.settings.tone_overrides()
        book = self.cad.settings.tone_book(self._tone_ov)
        self.plan.replace(book.codes())
        ours = book.ours_names()
        self.store.ours = ours
        self.paging.ours = {base_name(n) for n in ours}
        self.cad.monitor_names = {base_name(n) for n in book.monitor_names()}
        self._rematch_pages()

    def _rematch_pages(self) -> None:
        """The pages on screen matched against the plan as it is now (names learned since they were heard)."""
        with self._lock:
            for p in self.pages:
                self.plan.match_all(p.tones)

    def _match_pair(self, a_hz: float, b_hz: float | None) -> list[str]:
        return list(self.plan.match(ToneEvent("two_tone" if b_hz else "long_tone", a_hz, b_hz, 0.0, 0.0)).names)

    def tones_changed(self, page_id: str = "") -> None:
        """Settings: a pair was named (or its name removed). The plan changes at once, every page row with the
        pair says the name, and the page it was named from is read again (its tones now say who it was for,
        so a page of ours becomes a pending event)."""
        self._apply_named_tones()
        log("tone pairs named or edited from the console: " + (", ".join(
            f"{k} " + ("taken out" if v.get("deleted") else str(v.get("name") or tonelist.display(v.get("places") or [],
                                                                                                  v.get("service", ""), v.get("role", ""))))
            + (" (ours)" if v.get("ours") else "")
            for k, v in sorted(self._tone_ov.items())) or "none"))
        try:
            n = self.cad.comms.retag_pages(self._match_pair, self.paging.is_ours)
            if n:
                log(f"  {n} page row(s) in Radio say the name now")
        except Exception as e:  # the CAD must never stop the monitor
            log(f"CAD: could not retag the page rows: {type(e).__name__}: {e}")
        if page_id:
            self.reevaluate_page(page_id)
        self.channels_check_tones()

    def tone_list_source(self) -> tuple:
        """Settings > Data sources' Tone list row (sizeup/datasources.py tones_source): (the file, the codes now, its
        Reload: the file read again under the config's [[tones]], then applied as a Settings edit is)."""
        path = Path(self.cfg.tones_file) if self.cfg.tones_file else None

        def reload() -> bool:
            from sizeup.config import read_tones_file
            st = self.cad.settings
            codes = read_tones_file(path) + [c for c in st.base_tones if c.source == "config"]
            st.use_tone_list(codes, st.base_ours, st.unknown_log)
            self.tones_changed()
            return True
        return path, lambda: list(self.cad.settings.base_tones), reload if path is not None else None

    def channels_check_tones(self) -> None:
        """The hub sends the named pairs to the station with the channel choices (HubApp)."""

    def reevaluate_page(self, page_id: str) -> bool:
        """One page matched against the plan as it is now: its rows (Radio, the feed, the station page) show
        the names, and the page -> incident path runs again. False when there is no such page."""
        with self._lock:
            page = next((p for p in self.pages if p.id == page_id), None)
        if page is None:
            return False
        self.plan.match_all(page.tones)
        self._save_page(page)
        self._page_to_cad(page)
        if page.kind == "page" and page.status in ("done", "no_speech") and page.raw:
            d = self._dispatch(page.raw)
            if d is not None:
                page.dispatch = d.as_dict()  # type: ignore[attr-defined]
                self._page_incident(page, page.message_departments, d)
        return True

    def reread_tones(self, page_id: str) -> dict:
        """Settings "tones" reread (#115): the page's recording through the tone decoder again (a page decoded before
        a decoder fix), its pairs matched against the plan as it is now and the page read again (reevaluate_page:
        a page of ours queues its event). {pairs, sequence}. ValueError: no such page, or no recording of it."""
        with self._lock:
            page = next((p for p in self.pages if p.id == page_id), None)
        if page is None:
            raise ValueError(f"no page {page_id!r} here")
        wav = self.clip(page_id)
        audio = None
        if wav:
            import io
            try:
                with wave.open(io.BytesIO(wav)) as w:
                    if w.getframerate() == RATE and w.getsampwidth() == 2 and w.getnchannels() == 1:
                        audio = np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32) / 32768.0
            except (wave.Error, EOFError):
                audio = None
        if audio is None:
            raise ValueError("no recording of that page left (audio is kept 12 h)")
        tones, seq = decode_tones(audio, self.plan)
        before = [(round(t.a_hz, 1), round(t.b_hz, 1) if t.b_hz else None) for t in page.tones]
        page.tones, page.tone_seq = tones, seq
        if tones:
            page.kind = "page"
        log(f"{page.label}: page {page_id} tones re-read: " + (", ".join(t.describe() for t in tones) or "none")
            + f" (were {before})")
        self.reevaluate_page(page_id)
        return {"pairs": [{"a_hz": round(t.a_hz, 1), "b_hz": round(t.b_hz, 1) if t.b_hz else None, "names": list(t.names)}
                          for t in tones], "sequence": seq}

    def tone_names(self) -> dict:
        """GET /api/cad/tones: the pairs named here and what to pick from (#122): choices (every entity, the county's
        agencies: [{key, name, places, service, aliases, ours}]), places (one canonical list), services; names: the
        choices' names; ours: the ours entities'."""
        got = self.cad.settings.tone_choices()
        return {"named": self.cad.settings.tone_rows(), "names": [c["name"] for c in got["choices"]], **got,
                "ours": sorted(c["name"] for c in got["choices"] if c["ours"])}

    def _to_cad(self, t: Traffic, replace: bool = True) -> None:
        """A transmission into the CAD's communications log (linked to its rig's events)."""
        try:
            self.cad.record_traffic({"id": t.id, "start": t.start, "end": t.end, "source": t.source,
                                     "talkgroup": t.talkgroup, "unit": t.unit, "rig": t.rig, "radio_id": t.radio_id,
                                     "text": t.text, "status": t.status, "model": getattr(t, "model", ""),
                                     "id_note": getattr(t, "id_note", ""),
                                     # the emergency button (#128), its raw call record, kept by its window
                                     "emergency": bool(getattr(t, "emergency", False)),
                                     "evidence": getattr(t, "evidence", None), "kept": getattr(t, "kept", ""),
                                     # #409: the P25 system it was heard on ("": the county's, or FM) and its tier
                                     **self._row_source(t)},
                                    replace=replace)
        except Exception as e:  # the CAD must never stop the monitor
            log(f"CAD: could not log a transmission: {type(e).__name__}: {e}")

    def _row_source(self, t: Traffic) -> dict:
        """A transmission's source for the radio log (#409 / #415, sizeup/sourcekeys.py): {source_key, system, group,
        service, ours, dept, tier}. Another P25 system's row carries its own (App._call_system); the county's
        talkgroups and the analog channels are worked out here."""
        if getattr(t, "system", ""):
            return {"source_key": getattr(t, "source_key", ""), "system": t.system, "group": getattr(t, "group", ""),
                    "service": getattr(t, "service", ""), "ours": False, "dept": getattr(t, "dept", ""),
                    "tier": getattr(t, "tier", "")}
        if t.talkgroup is None:
            kind = next((c.kind for c in self.cfg.channels if c.label == t.source), "")
            air = kind == "airband"
            return {"source_key": sources.fm(t.source, kind), "system": "airband" if air else "analog",
                    "group": "Airband" if air else "Analog", "service": sources.service(kind=kind or "traffic"),
                    "ours": not air, "dept": "", "tier": ""}
        info = self.tg.info.get(t.talkgroup, {})
        return {"source_key": sources.p25(self.tg.county, t.talkgroup), "system": self.tg.county,
                "group": info.get("group") or "", "service": sources.service(info.get("group") or ""),
                "ours": bool(getattr(t, "ours", self.tg.main(t.talkgroup))), "dept": "", "tier": ""}

    def _backfill(self) -> None:
        """Traffic saved before radio IDs were tied to rigs: the rig from its talker text, and every
        transmission still in the history into the communications log (ones already there are left)."""
        try:        # airband rows from before a row said so (#409's source columns): never analog, never "ours"
            fixed = self.cad.comms.fix_air([c.label for c in self.cfg.channels if c.kind == "airband"])
            if fixed:
                log(f"CAD: {fixed} earlier airband transmission(s) given their source")
        except Exception as e:  # noqa: BLE001 - the CAD must never stop the monitor
            log(f"CAD: airband sources not fixed: {type(e).__name__}: {e}")
        rows = list(self.store.loose) + [t for i in self.store.incidents for t in i.traffic]
        n = 0
        for t in rows:
            if not t.rig and t.unit:
                rig, ids = self.radios.rig_for_names(t.unit, t.talkgroup is None, self.cfg.mdc_aliases)
                t.rig = rig
                t.radio_id = t.radio_id or ids
            if t.status != "pending":
                before = self.cad.comms.rev
                self._to_cad(t, replace=False)
                n += self.cad.comms.rev != before
        with self._lock:
            pages = list(self.pages)
        for p in pages:                                   # pages from before #64: rows in Radio too
            before = self.cad.comms.rev
            self._page_to_cad(p, replace=False)
            n += self.cad.comms.rev != before
        if n:
            log(f"CAD: {n} earlier transmission(s) added to the communications log")

    # -- who is talking now (FG1) ------------------------------------------------------------------

    def _watch_fm(self) -> None:
        """While a traffic channel's squelch is open it is "talking" (opened at once by the live tap,
        radio/tap.py, which also decodes the MDC pre-ID as the frames come in); this watcher ends it
        when the squelch closes, or when its dongle is switched to something else."""
        while not self._stopping.wait(LIVE_WATCH_S):
            try:
                self._watch_fm_once()
            except Exception as e:  # never let the indicator take anything down
                log(f"talking watcher error: {type(e).__name__}: {e}")
            try:
                self._release_pages()
            except Exception as e:
                log(f"page hold error: {type(e).__name__}: {e}")

    def _watch_fm_once(self, now: float | None = None) -> None:
        for r in list(self.receivers):
            label = r.channel.label
            if self._kind.get(label) != "traffic":
                continue
            sq = r.squelch
            on = sq.open and (not self.shared or label in self.assign.values())
            st = self._fm_live.get(label)
            if not on:
                if st is not None:
                    del self._fm_live[label]
                    self.live.fm_close(label)
                continue
            since = sq._start
            if st is None or st["since"] != since:
                st = self._fm_live[label] = {"since": since}
                self.live.fm_open(label, since)

    def _dispatch(self, raw: str):
        d = parse(raw) if raw else None
        if d is not None:  # units dispatched name their department ("Engine 61" -> Port Chester FD)
            named = list(d.units)   # said with the word ("Engine 61"): these name a department
            for n in d.numbers:  # "174, 175, Ladder 1": engines read without the word, if the roster has them
                u = f"Engine {n}"
                if u in self.cfg.unit_departments and u not in d.units:
                    d.units.append(u)
            for cs, name in self.cfg.unit_callsigns.items():  # chiefs by callsign: "2398 respond to ..."
                if re.search(rf"\b{cs}\b", raw) and name not in d.units:
                    d.units.append(name)
                    named.append(name)
            # A bare number is only a guess at an engine: shown with the page, but it doesn't make the page
            # some department's (2026-10-08: "16-10-191-22" read as Engine 16 and 191 queued a Rye call for PCFD).
            for u in named:
                dep = self.cfg.unit_departments.get(u)
                if dep and dep not in d.departments:
                    d.departments.append(dep)
        return d

    # -- speech-to-text re-runs ----------------------------------------------------------------------

    def _read_clip(self, name: str) -> np.ndarray | None:
        p = self.clips_dir / name if self.clips_dir else None
        if not p or not p.is_file():
            return None
        try:
            with wave.open(str(p)) as w:
                if w.getframerate() != RATE or w.getsampwidth() != 2:
                    return None
                return np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32) / 32768.0
        except (OSError, EOFError, wave.Error):
            return None

    def _rerun_audio(self, cid: str, fm: bool, voice_offset_s: float = 0.0) -> tuple[np.ndarray, bool] | None:
        """(audio, needs FM de-emphasis). The saved STT input when there is one; otherwise the saved
        listening clip (already de-emphasised and filtered; pages: tones cut off by voice_offset_s)."""
        src = self._read_clip(f"{cid}.src.wav")
        if src is not None:
            return src, fm
        with self._lock:
            hit = self.clips.get(cid)
        audio = hit[0].astype(np.float32) / 32768.0 if hit is not None else self._read_clip(f"{cid}.wav")
        if audio is None:
            return None
        return audio[int(voice_offset_s * RATE):], False

    def retranscribe(self, ids: list[str] | None = None, as_alt: bool = False) -> int:
        """Run speech-to-text again (ids=None: every recording still on disk, with the local model).
        Each one the way its channel says now (Settings > Radios > Engine): "aws" with Amazon Transcribe
        when it is set up here (else the local model, the reason kept for the result), "both" with AWS
        for the text and the local model for the alternative, otherwise the local model. as_alt: the
        hub's "both" re-run asking this station's whisper for the alternative. Every one finishes with
        a result on the transmission (rerun_at, rerun_result: changed | same | failed: why): the
        Re-transcribe menu item waits for it."""
        if ids is None or as_alt or self.aws is None or self.forwarding():
            return self._rerun_local(ids, as_alt)
        n, local, alt = self._rerun_split(ids)
        n += self._rerun_local(local) if local else 0
        if alt:
            self._rerun_local(alt, as_alt=True)
        return n

    def _find(self, cid: str):
        """(page or transmission, its channel key: "FG1", "South 2", "tg:307") or (None, "")."""
        with self._lock:
            page = next((p for p in self.pages if p.id == cid), None)
        if page is not None:
            return page, page.label
        t = next((t for t in list(self.store.loose) + [t for i in self.store.incidents for t in i.traffic] if t.id == cid), None)
        if t is None:
            return None, ""
        return t, traffic_key(t)

    def _rerun_pcm(self, item) -> np.ndarray | None:
        """A recording as Amazon Transcribe takes it (int16, 8 kHz), or None."""
        from sizeup.transcribe import pcm8k
        if isinstance(item, Traffic):
            got = self._rerun_audio(item.id, getattr(item, "fm", True))
        else:
            got = self._rerun_audio(item.id, True, item.voice_offset_s)
        return pcm8k(radio_voice(got[0], deemph=got[1], nr=False), RATE) if got else None

    def _rerun_split(self, ids: list[str]) -> tuple[int, list[str], list[str]]:
        """The ids whose channel is on AWS go to it now: (how many, the ones for the local model, the ones
        whose local text is the alternative ("both"))."""
        ok, why = self.aws.available() if self.aws is not None else (False, "Amazon Transcribe isn't set up here")
        n, local, alt = 0, [], []
        for cid in dict.fromkeys(ids):
            item, key = self._find(cid)
            eng = self.channel_pref(key)["engine"] if item is not None else ""
            if eng not in ("aws", "both"):
                local.append(cid)
                continue
            pcm = self._rerun_pcm(item) if ok else None
            if pcm is None:
                self._rerun_note[cid] = f"AWS unavailable ({why if not ok else 'no recording for it here'})"
                local.append(cid)
                continue
            page = None if isinstance(item, Traffic) else item
            self.aws.submit(key, AwsJob("traffic" if page is None else "page", cid, key, page=page, rerun=True), pcm)
            n += 1
            if eng == "both":
                alt.append(cid)
        if n:
            log(f"speech-to-text re-run: {n} recording(s) to Amazon Transcribe")
        return n, local, alt

    def transcribe_now(self, ids: list[str]) -> int:
        """The hub's fallback for live clips Amazon Transcribe couldn't do (#195): the local model on them at
        live priority (pages, Fire/EMS 15 and the firegrounds first), not behind live traffic like a re-run."""
        return self._rerun_local(list(ids or []), live=True)

    def _rerun_local(self, ids: list[str] | None = None, as_alt: bool = False, live: bool = False) -> int:
        """The local model again on recordings still on disk (ids=None: all of them). Queued behind live
        traffic (live: as live traffic, for the hub's fallback); results replace the text and keep the old
        one in history."""
        want = set(ids) if ids is not None else None
        jobs: list[tuple[float, Rerun, np.ndarray]] = []
        seen: set[str] = set()
        with self._lock:
            pages = [p for p in self.pages if p.status != "no_voice"]
            busy = set(self._rerun_ids)
        for p in pages:
            if (want is None or p.id in want) and p.id not in busy and p.id not in seen:
                got = self._rerun_audio(p.id, True, p.voice_offset_s)
                if got:
                    jobs.append((p.start, Rerun(p.id, "page", got[1], start=p.start, as_alt=as_alt), got[0]))
                    seen.add(p.id)
        traffic = list(self.store.loose) + [t for i in self.store.incidents for t in i.traffic]
        keys: dict[str, str] = {}           # live: each one's channel, for its class (_stt_priority)
        for t in traffic:
            if (want is None or t.id in want) and t.id not in busy and t.id not in seen:
                keys[t.id] = traffic_key(t)
                got = self._rerun_audio(t.id, getattr(t, "fm", self._kind.get(t.source) != "airband"))
                if got:
                    low = speech_seconds(got[0]) < MIN_SPEECH_S  # same mic-click rule as the live pass
                    jobs.append((t.start, Rerun(t.id, "traffic", got[1], low, t.unit, t.start, as_alt=as_alt), got[0]))
                    seen.add(t.id)
        jobs.sort(key=lambda j: -j[0])  # newest first: what's on screen updates first
        with self._lock:
            if not self._rerun_ids:  # a new batch: restart the progress count
                self.rerun = {"total": 0, "done": 0, "changed": 0, "failed": 0}
            n = 0
            for _start, job, audio in jobs:
                key = keys.get(job.id, "")
                prio = RERUN if not live else self._stt_priority(job, key)
                if self.stt.submit(job, audio, priority=prio, channel=(key or None) if live else None, fallback=live):
                    self._rerun_ids.add(job.id)
                    n += 1
            self.rerun["total"] += n
        if n:
            log(f"speech-to-text re-run: {n} recording(s) queued")
        return n

    def _rerun_done(self, job: Rerun) -> None:
        text = job.raw if job.status == "done" else ""
        model = Path(self.cfg.stt_model).name
        changed = False
        if job.as_alt:
            if job.status != "error":
                self._set_alt(job.id, text, model)
        elif job.status != "error":
            changed = self._retext(job.id, text, job.status, job.confidence, model)
        with self._lock:
            self._rerun_ids.discard(job.id)
            self.rerun["done"] += 1
            self.rerun["changed"] += changed
            self.rerun["failed"] += job.status == "error"
        if not job.as_alt:
            self._rerun_finish(job.id, "failed: speech-to-text error" if job.status == "error"
                               else "changed" if changed else "same", model)

    def _rerun_finish(self, cid: str, result: str, model: str = "") -> None:
        """A re-run is over, whatever came of it: changed | same | failed: why (and, after "; ", why it
        didn't run the channel's way). On the transmission (rerun_at, rerun_result) and the page, and to
        the hub (a "rerun" event): the Re-transcribe menu item shows it."""
        note = self._rerun_note.pop(cid, "")
        if note and not result.startswith("failed"):
            note += ": the local model ran"
        result = (result + (f"; {note}" if note else ""))[:300]
        with self._lock:
            page = next((p for p in self.pages if p.id == cid), None)
        if page is not None:
            page.rerun_at, page.rerun_result = time.time(), result  # type: ignore[attr-defined]
        try:
            self.cad.comms.rerun_done(cid, result)
        except Exception as e:  # the CAD must never stop the monitor
            log(f"CAD: could not note a re-run: {type(e).__name__}: {e}")
        log(f"re-run {cid}: {result}" + (f" ({model})" if model else ""))
        self._emit("rerun", {"id": cid, "result": result, "model": model})

    def _set_alt(self, cid: str, text: str, model: str) -> None:
        """The alternative transcript ("both": the local model's next to AWS's), the text left as it is."""
        alt = {"text": text, "model": model}
        with self._lock:
            page = next((p for p in self.pages if p.id == cid), None)
        if page is not None:
            page.alt_text, page.alt_model = text, model  # type: ignore[attr-defined]
            self._save_page(page)
        self.store.set_alt(cid, alt)
        try:
            self.cad.comms.set_alt(cid, alt)
        except Exception as e:  # the CAD must never stop the monitor
            log(f"CAD: could not update the communications log: {type(e).__name__}: {e}")
        self._emit("alt_text", {"id": cid, **alt})

    def _retext(self, cid: str, text: str, status: str, confidence: float | None, model: str,
                alt: dict | None = None, why: str = "re-run") -> bool:
        """New text for a page or transmission already on screen (a re-run here, or one the station
        sent the hub, or Amazon Transcribe's), the old text kept in its history. True = the text changed.
        alt {text, model}: the other engine's transcript, kept to compare ("both")."""
        changed = False
        with self._lock:
            page = next((p for p in self.pages if p.id == cid), None)
        if page is not None:
            if page.raw != text and (page.raw or page.status not in ("pending", "remote")):
                hist = list(getattr(page, "history", None) or [])
                hist.append({"text": page.raw, "at": time.time(), "model": getattr(page, "model", "") or ""})
                page.history = hist[-5:]  # type: ignore[attr-defined]
                changed = True
            page.raw, page.confidence = text, confidence
            page.rerun_at, page.model = time.time(), model  # type: ignore[attr-defined]
            if alt is not None:
                page.alt_text, page.alt_model = str(alt.get("text") or ""), str(alt.get("model") or "")  # type: ignore[attr-defined]
            page.status = "done" if text else "no_speech"
            d = self._dispatch(text)
            page.dispatch = d.as_dict() if d else None  # type: ignore[attr-defined]
            self._save_page(page)
        old = next((t.text for t in list(self.store.loose) + [t for i in self.store.incidents for t in i.traffic]
                    if t.id == cid), None)
        changed = changed or (old is not None and old != text)
        self.store.retext(cid, text, status, confidence, model, alt=alt)
        try:
            self.cad.comms.retext(cid, text, status, model, alt)
        except Exception as e:  # the CAD must never stop the monitor
            log(f"CAD: could not update the communications log: {type(e).__name__}: {e}")
        t = next((x for x in list(self.store.loose) + [x for i in self.store.incidents for x in i.traffic]
                  if x.id == cid), None)
        if t is not None and text:
            self._alarm(t, text)                       # the words came later (Amazon Transcribe, a re-run)
            self._atc(t, text)
        if changed:
            log(f"{why} {cid}: {text or '(no speech)'}")
        self._emit("retext", {"id": cid, "text": text, "status": status, "confidence": confidence, "model": model,
                              **({"alt": alt} if alt is not None else {})})
        return changed


    # -- per-channel speech-to-text (Settings > Radios, sizeup/channels.py) --------------------------------

    def channel_pref(self, key: str) -> dict:
        """{record, transcribe, engine} for "FG1" / "South 2" / "tg:307" (a talkgroup's record is its own switch)."""
        p = channels.pref(key, self._chan_ov, self.cfg.stt_aws_channels, self.cfg.stt_record_only)
        if channels.is_tg(key):
            tsys, tgn = channels.tg_of(key)
            p["record"] = self.tg.record(tgn, tsys)
            tier = self.tg.tier(tgn, tsys)
            if tier is not None:                 # another system's talkgroup (#409): Transcribe is its tier's
                p["transcribe"] = tier in tiers.TRANSCRIBED
        return p

    def aws_ok(self, key: str | None = None) -> tuple[bool, str]:
        """Can Amazon Transcribe take audio now (from that channel: past the monthly limit the pages and
        Fire/EMS 15 keep it within the reserve)? Forwarding to a hub: what the hub last said (it runs it);
        else this process's own (the hub; all-in-one with [stt] aws and credentials)."""
        if self.forwarding():
            ok = bool(self._hub_aws.get("ok")) or (key is not None and key in (self._hub_aws.get("reserve") or ()))
            return ok, "" if ok else str(self._hub_aws.get("why") or "")
        if self.aws is None:
            return False, "Amazon Transcribe isn't set up here ([stt] aws): the local model is used"
        return self.aws.available(key)

    def stt_engine(self, key: str) -> str:
        """off | local | aws | both: what runs for this channel now ("aws" falls back to local without AWS)."""
        return channels.effective(self.channel_pref(key), self.aws_ok(key)[0])

    def _stt(self, item, audio: np.ndarray, eng: str, key: str) -> None:
        """One transmission or page to speech-to-text the way its channel says (audio: whisper's 16 kHz input)."""
        item.stt = eng if eng in ("aws", "both") else ""
        if eng == "off":                       # kept, not transcribed
            item.raw, item.confidence, item.status = "", None, "off"
            item.model = ""
            return self._transcribed(item)
        if eng == "aws" and self.forwarding():
            # The hub runs Amazon Transcribe on the clip it gets: no whisper here.
            item.raw, item.confidence, item.status, item.model = "", None, "remote", channels_model()
            if isinstance(item, Traffic):
                item.transcript_pending = "aws"
                return self._transcribed(item)
            return self._save_page(item)
        if eng == "aws" or (eng == "both" and not self.forwarding()):   # (both, forwarding: the hub adds AWS)
            from sizeup.transcribe import pcm8k
            pcm = pcm8k(radio_voice(audio, deemph=getattr(item, "fm", True), nr=False), RATE)
            if eng == "aws":
                return self._aws_start(item, pcm, key)
            self._aws_wait[item.id] = (pcm, key)
        self.stt.submit(item, audio, self._stt_priority(item, key), key)    # (dropped: _stt_dropped says so on it)

    def _stt_priority(self, item, key: str) -> int:
        """The whisper queue's class (strict, sizeup/stt.py, #300): pages, Fire/EMS 15 and the firegrounds first
        (URGENT), the airband pre-alerts (HIGH), other live traffic (LIVE), and last a talkgroup heard only as
        context (LOW: not "main" in Settings > Radios), which is shed when whisper is behind."""
        if not isinstance(item, (Traffic, Rerun)) or getattr(item, "kind", "") == "page" or key in STT_FIRST:
            return URGENT
        if key in AIRBAND_FIRST:
            return HIGH
        if channels.is_tg(key) and (tier := self.tg.tier(*channels.tg_of(key)[::-1])) is not None:
            # another system's talkgroup (#409): by its tier, in classes under every one of ours and shed when
            # they wait (sizeup/stt.py CONTEXT): fire traffic never queues behind police
            return CONTEXT if tier == tiers.ALERT else LOW
        if channels.is_tg(key) and not self.tg.main(*channels.tg_of(key)[::-1]):
            return LOW
        return LIVE

    def _stt_dropped(self, item, why: str) -> None:
        """The whisper queue dropped or skipped it (sizeup/stt.py SttQueue, #195): it says so ("Transcription
        failed", the reason in its model line), never left "Transcribing"; Re-transcribe can still run it."""
        log(f"speech-to-text: {getattr(item, 'id', '?')} not transcribed: {why}")
        if isinstance(item, Rerun):
            with self._lock:
                had = item.id in self._rerun_ids
                self._rerun_ids.discard(item.id)
                if had:
                    self.rerun["done"] += 1
                    self.rerun["failed"] += 1
            if not item.as_alt:
                self._rerun_finish(item.id, f"failed: {why}"[:200])
            return
        shed = why.startswith(SHED)             # whisper was busy (#300): a quiet "skipped", still re-runnable
        item.raw, item.confidence, item.status = "", None, "skipped" if shed else "error"
        item.model = f"{Path(self.cfg.stt_model).name} ({why if shed else 'not transcribed: ' + why})"[:200]
        self._transcribed(item)

    # -- Amazon Transcribe (the hub; all-in-one with [stt] aws) ---------------------------------------------

    def _aws_setup(self) -> None:
        from sizeup import aws as aws_mod
        from sizeup.transcribe import AwsTranscriber, Settings as AwsSettings, Usage, Vocabulary, respell
        cfg, store = self.cfg, self.cad.store
        reg = aws_mod.region(cfg.stt_aws_region)
        self.aws_usage = Usage(store.meta_get, store.meta_put)
        self.vocab = Vocabulary(reg, store.meta_get, store.meta_put, cfg.stt_aws_vocabulary, cfg.stt_aws_api_endpoint,
                                log=log, on_ready=self._vocab_ready)
        # HPN ATC (#233): the airband channels keep AWS past the monthly limit (with the pages and Fire/EMS 15), and
        # stream with their own vocabulary (sizeup-atc: data/atc/vocabulary.json), pushed with the PCFD one
        from sizeup.transcribe import RESERVE_CHANNELS
        airband = tuple(c.label for c in cfg.channels if c.kind == "airband")
        self.atc_vocab = None
        if airband:
            from sizeup.atc import VOCABULARY as ATC_VOCABULARY
            self.atc_vocab = Vocabulary(reg, store.meta_get, store.meta_put, ATC_VOCABULARY, cfg.stt_aws_api_endpoint,
                                        log=log, on_ready=self._atc_vocab_ready, key="aws_vocabulary_atc")
        self.aws = AwsTranscriber(AwsSettings(region=reg, endpoint=cfg.stt_aws_endpoint, api_endpoint=cfg.stt_aws_api_endpoint,
                                              idle_s=cfg.stt_aws_idle_s, pace=cfg.stt_aws_pace, vocabulary=cfg.stt_aws_vocabulary,
                                              reserve_channels=tuple(dict.fromkeys((*RESERVE_CHANNELS, *airband)))),
                                  self.aws_usage, self._aws_done, self._aws_failed, log=log,
                                  limit_usd=self.cad.settings.aws_limit,
                                  respell=lambda t: respell(t, self.vocab.state()["respell"]),
                                  on_late=self._aws_late, on_change=self._aws_changed)
        if self.vocab.ready():
            self.aws.vocabulary = cfg.stt_aws_vocabulary
        if self.atc_vocab is not None and self.atc_vocab.ready():
            self._atc_vocab_ready(self.atc_vocab.name)
        log(f"speech-to-text: Amazon Transcribe streaming ({reg}) for the channels set to AWS in Settings > Radios")

    def _atc_view(self, view, now) -> None:
        """A new Aircraft layer picture on the CAD of record: to the ATC talkers, and a look at the vocabulary (#257)."""
        self.cad.atc_talkers.observe(view, now)
        aw = getattr(self.cad, "airport_watch", None)
        if aw is not None and aw.enabled:
            try:                     # an open pre-alert's aircraft followed (#369): kept open, ended when it lands
                aw.tick(self._atc_aircraft(), now)
            except Exception as e:  # noqa: BLE001 - the aircraft layer goes on
                log(f"airport watch: {type(e).__name__}: {e}")
        r = getattr(self, "atc_refresh", None)
        if r is not None:
            try:
                r.tick()
            except Exception as e:  # noqa: BLE001 - the aircraft layer goes on
                log(f"atc vocabulary: {type(e).__name__}: {e}")

    def _atc_leader(self) -> bool:
        """The CAD of record holding the hub's lease (#254: sizeup/fence.py), or all-in-one (no fence)."""
        fence = getattr(self, "fence", None)
        return not self.forwarding() and (fence is None or fence.ok())

    def _atc_vocab_pending(self) -> None:
        """The HPN ATC vocabulary is being updated (PENDING): new airband streams open without it until it is READY
        again (_atc_vocab_ready), so none asks AWS for a vocabulary it is still building; open streams keep theirs."""
        if self.aws is None:
            return
        for c in self.cfg.channels:
            if c.kind == "airband" and self.aws.channel_vocabulary.pop(c.label, None):
                log(f"aws transcribe: {c.label}: new streams without the custom vocabulary until the update is READY")

    def _atc_vocab_ready(self, name: str) -> None:
        """The HPN ATC vocabulary is READY: the airband channels' next streams use it (the rest keep sizeup-pcfd)."""
        if self.aws is None:
            return
        for c in self.cfg.channels:
            if c.kind == "airband" and self.aws.channel_vocabulary.get(c.label) != name:
                self.aws.channel_vocabulary[c.label] = name
                log(f"aws transcribe: custom vocabulary {name} in use on {c.label} from its next stream")

    def _vocab_ready(self, name: str) -> None:
        if self.aws is not None and self.aws.vocabulary != name:
            self.aws.vocabulary = name
            log(f"aws transcribe: custom vocabulary {name} in use from the next stream")

    def _aws_start(self, item, pcm: np.ndarray, key: str) -> None:
        """Amazon Transcribe for a transmission (shown at once as "Transcribing", the text follows) or a
        page (its dispatch is read when the text comes)."""
        item.status, item.raw, item.confidence, item.model = "pending", "", None, channels_model()
        item.stt = "aws"
        if isinstance(item, Traffic):
            item.__dict__.pop("transcript_pending", None)
            self._transcribed(item)
            job = AwsJob("traffic", item.id, key)
        else:
            self._save_page(item)
            job = AwsJob("page", item.id, key, page=item)
        ok, why = self.aws.available(key) if self.aws is not None else (False, "Amazon Transcribe isn't set up here")
        if not ok:
            return self._aws_failed(job, why)
        self.aws.submit(key, job, pcm)

    def _aws_alt(self, item, pcm: np.ndarray, key: str) -> None:
        """"both": the local text is in; Amazon Transcribe's becomes the text, the local one the alternative."""
        if self.aws is None or not self.aws.available()[0]:
            return
        alt = {"text": getattr(item, "text", "") if isinstance(item, Traffic) else getattr(item, "raw", ""),
               "model": getattr(item, "model", "") or ""}
        self.aws.submit(key, AwsJob("traffic" if isinstance(item, Traffic) else "page", item.id, key, alt,
                                    None if isinstance(item, Traffic) else item), pcm)

    def _aws_done(self, job: AwsJob, text: str, conf: float | None) -> None:
        status = "done" if text else "no_speech"
        if job.rerun:
            changed = self._retext(job.id, text, status, conf, channels_model(), why="AWS Transcribe re-run")
            return self._rerun_finish(job.id, "changed" if changed else "same", channels_model())
        if job.kind == "page" and job.alt is None:
            page = job.page
            page.raw, page.confidence, page.status, page.model = text, conf, status, channels_model()
            self._transcribed(page)
        else:
            self._retext(job.id, text, status, conf, channels_model(), alt=job.alt, why="AWS Transcribe")

    def _aws_failed(self, job: AwsJob, why: str) -> None:
        """Never dropped: the local model takes it ("both": the local text simply stays)."""
        log(f"aws transcribe: {job.kind} {job.id}: {why}" + ("" if job.alt is not None else ": using the local model"))
        if job.rerun:
            self._rerun_note[job.id] = f"AWS failed ({why[:80]})"
            if not self._rerun_local([job.id]):
                self._rerun_note.pop(job.id, None)
                self._rerun_finish(job.id, f"failed: AWS: {why[:80]}; no recording here for the local model")
            return
        if job.alt is not None:
            return
        if job.kind == "page":
            page = job.page
            if page.audio is not None:
                page.status, page.model, page.stt = "pending", "", ""
                self.stt.submit(page, page.voice, URGENT, job.channel)
                return
            page.status = "error"
            self._save_page(page)
            return
        if not self._rerun_local([job.id]):
            self._retext(job.id, "", "error", None, f"{channels_model()} (failed: {why[:80]})", why="AWS Transcribe")

    def _aws_late(self, job: AwsJob, text: str, conf: float | None) -> None:
        """AWS answered after the job's deadline (#195): its fallback ran already, so the late text never takes
        the place of whisper's. It is kept as the alternative transcript (alt_text, model "aws-transcribe
        (late)"), next to whisper's, like a "both" channel; only when the fallback found no text at all (the
        item says "transcription failed") does it become the text. Nothing said: dropped."""
        if not text:
            log(f"aws transcribe: {job.kind} {job.id}: a late answer with no words: dropped")
            return
        item, _key = self._find(job.id)
        if item is None:
            return
        if getattr(item, "status", "") in ("error", "skipped") and job.alt is None and not job.rerun:
            log(f"aws transcribe: {job.kind} {job.id}: a late answer, and the fallback had none: it is the text")
            return self._aws_done(job, text, conf)
        log(f"aws transcribe: {job.kind} {job.id}: a late answer, kept as the alternative: {text}")
        self._set_alt(job.id, text, f"{channels_model()} (late)")

    def _aws_changed(self) -> None:
        """Amazon Transcribe's breaker opened or closed (sizeup/transcribe.py): aws_ok() says so from now on."""

    def aws_health(self) -> dict:
        """Amazon Transcribe's health (AwsTranscriber.health()): station health and Settings > Data sources."""
        if self.aws is not None:
            return self.aws.health()
        return {"on": False, "state": "off",
                "why": "runs on the hub" if self.forwarding() else "Amazon Transcribe isn't set up here ([stt] aws)"}

    # -- the radios' side of Settings > Radios (Cad.radio_ctx) ---------------------------------------------

    def channels_locked(self) -> str:
        if self.forwarding():
            return ("Channels are set on the hub (the CAD of record) while this station forwards to it: "
                    "change them there")
        return ""

    def aws_channels(self) -> tuple[str, ...]:
        return self.cfg.stt_aws_channels

    def record_only(self) -> tuple[str, ...]:
        return self.cfg.stt_record_only

    def aws_limit_default(self) -> float:
        return float(self.cfg.stt_aws_limit_usd)

    def _channel_overrides(self) -> dict:
        """The channel choices (renamed_overrides), the old names' moved and dropped in the store too (#307)."""
        stored = self.cad.settings.channel_overrides()
        out = renamed_overrides(stored, self._kind)
        if out != stored:
            try:
                self.cad.settings.replace_channels(out, "rename")
                log("channel choices: moved from the old names: " + ", ".join(sorted(set(stored) - set(out))))
            except Exception as e:  # noqa: BLE001 - never in the way: still used as moved, saved next time
                log(f"channel choices: couldn't save the move from the old names ({type(e).__name__}: {e})")
        return out

    def channels_changed(self) -> None:
        self._chan_ov = self._channel_overrides()
        log("speech-to-text channels: " + (", ".join(f"{k} {v}" for k, v in sorted(self._chan_ov.items())) or "the defaults"))

    def vocabulary_suggestions(self) -> list[str]:
        if self._vocab_suggest is None:
            streets: list[str] = []
            if self.gaz is not None:
                ours = ("Port Chester", "Rye Brook")
                ranked = sorted(self.gaz.streets.values(),
                                key=lambda st: -sum(len(v) for m, v in st.addrs.items() if m in ours))
                streets = [st.name for st in ranked if any(m in ours for m in st.addrs)]
            self._vocab_suggest = channels.vocabulary_suggestions(self.cad.lists.units, streets)
        return list(self._vocab_suggest)

    def vocabulary_push(self, phrases: list[str]) -> dict:
        if self.vocab is None:
            return {"state": "error", "reason": "Amazon Transcribe isn't set up here: push the vocabulary on the hub"}
        st = self.vocab.push(phrases)
        if getattr(self, "atc_vocab", None) is not None:       # HPN ATC's own (#233), from data/atc/vocabulary.json
            from sizeup import atc
            r = getattr(self, "atc_refresh", None)             # ... with the operators seen near HPN lately (#257)
            self._atc_vocab_pending()
            st = dict(st, atc=self.atc_vocab.push(r.phrases() if r is not None else atc.vocabulary()["phrases"]))
        return st

    def vocabulary_state(self) -> dict:
        if self.vocab is None:
            return {"name": self.cfg.stt_aws_vocabulary, "state": "none", "reason": "", "phrases": 0, "sig": ""}
        st = self.vocab.state()
        st.pop("respell", None)
        if getattr(self, "atc_vocab", None) is not None:
            a = self.atc_vocab.state()
            a.pop("respell", None)
            st["atc"] = a
        return st

    def aws_status(self) -> dict:
        """Where Amazon Transcribe runs and what it cost this month (the cost line)."""
        ok, why = self.aws_ok()
        if self.aws is None:
            return {"enabled": False, "where": "hub" if self.forwarding() else "none", "ok": ok, "why": why}
        return {"enabled": True, "where": "here", "ok": ok, "why": why, **self.aws.spent(),
                "price_per_min": self.aws.s.price_per_min, **self.aws.status()}

    # -- the hub's choices on the station (the set_channel_prefs job) ------------------------------------------

    def _load_hub_aws(self) -> dict:
        try:
            d = json.loads(self.cad.store.meta_get("hub_aws") or "{}")
        except ValueError:
            d = {}
        return {"ok": bool(d.get("ok")), "why": str(d.get("why") or "the hub hasn't said whether it can use AWS yet"),
                "reserve": [str(k) for k in (d.get("reserve") or [])]}

    def set_channel_prefs(self, prefs: dict, aws: dict | None = None, tones: dict | None = None) -> dict:
        """From the hub (CAD of record): its whole channel map, whether it can take AWS audio now, and the
        tone pairs named there (#80; a hub from before sends none: this station's stay). Applied here and
        kept (this station's own Settings overrides, audited as "hub")."""
        clean = channels.check_map(prefs)
        changed = self.cad.settings.replace_channels(clean, "hub")
        self._chan_ov = self._channel_overrides()
        if isinstance(tones, dict) and self.cad.settings.replace_tones(tones, "hub"):
            self._apply_named_tones()
            changed = True
        if isinstance(aws, dict):
            new = {"ok": bool(aws.get("ok")), "why": str(aws.get("why") or "")[:300],
                   "reserve": [str(k)[:40] for k in (aws.get("reserve") or [])][:8]}
            if new != self._hub_aws:
                self._hub_aws = new
                self.cad.store.meta_put("hub_aws", json.dumps(new))
                changed = True
        if changed:
            log(f"speech-to-text channels from the hub: {len(clean)} set; AWS {'on' if self._hub_aws['ok'] else 'off'}"
                + (f" ({self._hub_aws['why']})" if not self._hub_aws["ok"] and self._hub_aws["why"] else ""))
        return self.channels_applied()

    def channels_applied(self) -> dict:
        """What this station runs (its state push: the hub compares sig)."""
        return {"prefs": self._chan_ov, "aws": dict(self._hub_aws), "tones": len(self._tone_ov),
                "sig": channels.sig(self._chan_ov, self._hub_aws["ok"], self._tone_ov, self._hub_aws.get("reserve"))}

    def channels_sync(self) -> dict:
        if self.forwarding():
            return {"state": "hub", "detail": "Set on the hub (the CAD of record) and applied here"}
        return {"state": "local", "detail": ""}

    def channels_state(self) -> dict:
        """Settings > Radios > Channels: the choices, what AWS can do, where they stand, the cost."""
        ok, why = self.aws_ok()
        reserve = ([k for k in channels_reserve(self) if self.aws_ok(k)[0]] if not ok else [])
        return {"prefs": self._chan_ov, "aws_channels": list(self.cfg.stt_aws_channels),
                "record_only": list(self.cfg.stt_record_only), "aws_ok": ok, "aws_why": why,
                "aws_reserve": reserve,
                "fm": [{"key": c.label, "kind": c.kind, "mhz": c.mhz,
                        **({"schedule": plan_text(c)} if getattr(c, "schedule", ()) or getattr(c, "also", ()) else {})}
                       for c in self.cfg.channels],
                "sync": self.channels_sync(), "aws": self.aws_status(), "tiers": self.tiers_state()}

    def tiers_state(self) -> dict:
        """The talkgroup tiers (#409, sizeup/tiers.py): how long each tier's audio is kept ([storage] tier_keep_days)
        and the storage that comes to, from the audio logged per tier in the last 7 days (worked out every 10 minutes)."""
        days = self.cfg.tier_keep_days
        at, per_day = getattr(self, "_tier_audio", (0.0, {}))
        if time.monotonic() - at > 600 or not at:
            try:
                per_day = self.cad.comms.audio_per_day() if self.cfg.p25_systems else {}
            except Exception:  # noqa: BLE001 - an estimate, never in the way of the state
                per_day = {}
            self._tier_audio = (time.monotonic(), per_day)
        return {"keep_days": days, "labels": tiers.LABELS,
                "storage": tiers.storage(per_day, days, self.cfg.audio_keep_hours)}

    # -- state --------------------------------------------------------------------------------------

    # -- switchable dongles ------------------------------------------------------------------------

    @property
    def sdrs(self) -> list[str]:
        """The dongles shown now: listed ones, every one plugged in (auto), and any unplugged one
        still assigned a mode (so you can see what stopped). Plug order; listed first."""
        out = list(self._listed)
        here = [s for _i, _p, s in sdr_lookup.devices(self.cfg.rtl_fm)] if self.cfg.sdr_auto else []
        for sd in here + [sd for sd, m in self.assign.items() if m != OFF_MODE]:
            if sd and sd not in out and not self._sdr_ignored(sd):
                out.append(sd)
        return out

    @property
    def mode(self) -> str:
        """The first dongle's mode (one-dongle setups)."""
        rows = self.sdrs
        return self.assign.get(rows[0], OFF_MODE) if rows else OFF_MODE

    def _load_modes(self) -> None:
        """sdr_modes.json: {serial: mode, ..., "_set_by": {serial: "person" | "auto"}}. Files from before
        "_set_by" (just {serial: mode}) still load: every mode in them counts as not set by a person."""
        saved: dict = {}
        try:
            if self._mode_file and self._mode_file.is_file():
                saved = json.loads(self._mode_file.read_text())
            elif self._mode_file and self._listed and self._mode_file.with_name("sdr_mode.txt").is_file():
                saved = {self._listed[0]: self._mode_file.with_name("sdr_mode.txt").read_text().strip()}  # one-dongle era
        except (OSError, ValueError):
            saved = {}
        saved = saved if isinstance(saved, dict) else {}
        by = saved.pop("_set_by", None)
        by = by if isinstance(by, dict) else {}
        taken: set[str] = set()
        # Two dongles saved on one job (a person's kept while a replacement ran it): the person's wins,
        # the other starts Off (and auto-assign gives it the next job).
        for sd in sorted(dict.fromkeys([*self._listed, *saved]), key=lambda sd: by.get(sd) != "person"):
            if self._sdr_ignored(sd):
                continue
            m = saved.get(sd, self.assign.get(sd, OFF_MODE))
            m = RENAMED.get(m, m) if m not in self.modes else m     # #275: a job saved under a channel's old name
            m = m if m in self.modes and (m == OFF_MODE or m not in taken) else OFF_MODE
            self.assign[sd] = m
            if by.get(sd) in ("person", "auto") and RENAMED.get(saved.get(sd), saved.get(sd)) == m:
                self.set_by[sd] = by[sd]
            taken.add(m)

    def sdr_name(self, serial: str) -> str:
        return self.cfg.sdr_names.get(serial) or serial

    # -- two dongles, one serial (#242) ---------------------------------------------------------------------

    def _load_products(self) -> dict[str, str]:
        try:
            d = json.loads(self._products_file.read_text()) if self._products_file else {}
        except (OSError, ValueError):
            d = {}
        return {str(k): str(v) for k, v in d.items() if isinstance(v, str)} if isinstance(d, dict) else {}

    def _note_products(self, devs: list[tuple[int, str, str]]) -> None:
        """Remember each serial's product while it is the only dongle with it (sdr_products.json). A serial seen alone
        with two different products (a dongle swapped, or the old and the new one unplugged in turn) gets "": which
        one it meant is then never guessed (sizeup/radio/sdrserial.py prints what to set instead)."""
        dup = sdr_lookup.duplicates(devs)
        new = {}
        for _i, p, s in devs:
            if s and p and s not in dup:
                was = self._products.get(s)
                want = p if was is None else was if was == p else ""
                if want != was:
                    new[s] = want
        if not new or self._products_file is None:
            return
        self._products.update(new)
        try:
            tmp = self._products_file.with_name(f".{self._products_file.name}.{os.getpid()}")
            tmp.write_text(json.dumps(self._products, indent=1, sort_keys=True))
            os.replace(tmp, self._products_file)
        except OSError:
            pass

    def usb(self, devs: list[tuple[int, str, str]] | None = None) -> dict:
        """The dongles on USB now and the serials two or more of them share: {"devices": [{index, product, serial}],
        "duplicates": [{serial, indexes, products, name, job, text}]} (the station's health, Settings > Radios)."""
        devs = sdr_lookup.devices(self.cfg.rtl_fm) if devs is None else devs
        self._note_products(devs)
        dups = []
        for serial, entries in sdr_lookup.duplicates(devs).items():
            dups.append({"serial": serial, "indexes": [i for i, _p in entries], "products": [p for _i, p in entries],
                         "name": self.sdr_name(serial), "job": self.assign.get(serial, OFF_MODE),
                         "rename": sdr_lookup.rename_index(serial, entries, self._products),
                         "was": self._products.get(serial, ""),
                         "text": sdr_lookup.duplicate_text(serial, entries, self._products)})
        return {"devices": [{"index": i, "product": p, "serial": s} for i, p, s in devs], "duplicates": dups}

    def _ambiguity(self, serial: str, devs: list[tuple[int, str, str]] | None = None) -> str:
        """Why nothing of ours may open `serial` now (two or more dongles have it), or ""."""
        if not serial:
            return ""
        devs = sdr_lookup.devices(self.cfg.rtl_fm) if devs is None else devs
        return sdr_lookup.ambiguity(serial, devs, self._products)

    def _watch_ambiguous(self, devs: list[tuple[int, str, str]]) -> list[str]:
        """Log each serial that turns ambiguous (once) and each that is unique again; the ones unique again."""
        now = {s: sdr_lookup.duplicate_text(s, e, self._products) for s, e in sdr_lookup.duplicates(devs).items()}
        for s, text in now.items():
            if s not in self._ambiguous:
                job = self.assign.get(s, OFF_MODE)
                log(f"sdr: CRITICAL {text}"
                    + (f". {job} doesn't (re)start on it until then: a program already running keeps the dongle it "
                       "opened" if job != OFF_MODE else ""))
        back = [s for s in self._ambiguous if s not in now]
        for s in back:
            log(f"sdr: serial {s} is on one dongle again ({self._sdr_tag(s)}): usable")
        self._ambiguous = now
        return back

    # -- the ADS-B dongle (#235) ------------------------------------------------------------------------------------

    def _sdr_ignored(self, sd: str) -> bool:
        """[radio] sdr_ignore, and the dongle named for ADS-B: never one of sizeup's radios."""
        return sd in self._ignore_base or (bool(self.adsb_sdr) and sd == self.adsb_sdr)

    def _load_adsb(self) -> str:
        """The dongle named for ADS-B: Settings' choice (state/adsb_receiver.json), else [aircraft.receiver] sdr."""
        try:
            d = json.loads(self._adsb_file.read_text()) if self._adsb_file else None
        except (OSError, ValueError):
            d = None
        if isinstance(d, dict) and isinstance(d.get("sdr"), str) and not adsbd.serial_problem(d["sdr"].strip()):
            return d["sdr"].strip()
        return self.cfg.aircraft_sdr

    def set_adsb_receiver(self, serial: str | None) -> str | None:
        """Settings > Radios (POST /api/adsb-receiver {sdr, confirm: true}; admins; the hub sends it here as a job):
        name the dongle for ADS-B ("" = none). A job it was running stops first; from then on it is the decoder's only
        (sizeup-adsb.service) and never one of the radios. A dongle unnamed is Off, set by a person: auto-assign never
        gives it a job by itself (it still has the filter and LNA on it). None, or why not (like set_mode)."""
        serial = (serial or "").strip()
        if serial:
            if why := adsbd.serial_problem(serial):
                return why
            if serial in (list(self.cfg.sdrs) + ([self.cfg.shared_device] if self.cfg.shared_device else [])):
                return f"{serial} is one of the radios listed in [radio] sdrs: take it out there first"
            held = claims.other(serial)
            if held and held.get("mode") != adsbd.MODE:
                return f"SDR {self.sdr_name(serial)} is in use by {held.get('app', 'another app')} ({held.get('mode', '?')})"
            if why := self._ambiguity(serial):
                return f"SDR {self.sdr_name(serial)}: {why}"
        old = self.adsb_sdr
        if serial == old:
            return None
        if self._adsb_file is None:
            return "no state folder here to keep the choice in ([cad] db)"
        with self._mode_lock:
            if serial and self.shared and self.assign.get(serial, OFF_MODE) != OFF_MODE:
                err = self.set_mode(OFF_MODE, serial, by="person")
                if err:
                    return f"its job couldn't be stopped first: {err}"
            try:
                tmp = self._adsb_file.with_name(f".{self._adsb_file.name}.{os.getpid()}")
                tmp.write_text(json.dumps({"sdr": serial, "at": time.time(), "by": "settings"}))
                os.replace(tmp, self._adsb_file)
            except OSError as e:
                return f"couldn't keep the choice: {e}"
            self.adsb_sdr = serial
            if serial:
                self._guard_adsb()                    # whatever ran there, assigned or not (#262)
                self.assign.pop(serial, None)
                self.set_by.pop(serial, None)
                self._listed = [sd for sd in self._listed if sd != serial]
            if old:
                self._auto_skip.add(old)
                if self.shared:
                    self.assign[old], self.set_by[old] = OFF_MODE, "person"
                self._seen.add(old)
            self._save_modes()
        log(f"sdr: ADS-B receiver: {self._sdr_tag(serial) if serial else 'none'}"
            + (f" (was {self._sdr_tag(old)}, now Off: never given a job by itself)" if old else "")
            + (" -> the decoder (sizeup-adsb) runs on it; sizeup's radios never use it" if serial else ""))
        return None

    def adsb_row(self, plugged: dict | None = None) -> dict:
        """Settings > Radios' "ADS-B receiver (reserved)" row: the dongle (or none), its decoder's state
        (sizeup/radio/adsbd.py's status), who holds it, and the forwarding to the hub."""
        sd = self.adsb_sdr
        if plugged is None:
            plugged = {s: p for _i, p, s in sdr_lookup.devices(self.cfg.rtl_fm)}
        st = adsbd.read_status() or {}
        if st and st.get("sdr") != sd:
            st = {}                                   # the decoder hasn't caught up with the change yet
        h = claims.holder(sd) if sd else None
        fwd = getattr(getattr(self, "link", None), "aircraft", None)
        src = "settings" if self._adsb_file and self._adsb_file.is_file() and sd != self.cfg.aircraft_sdr else (
            "config" if sd else "")
        return {"serial": sd, "name": self.sdr_name(sd) if sd else "", "present": sd in plugged if sd else None,
                "product": plugged.get(sd, "") if sd else "", "source": src,
                "held_by": {"app": h.get("app"), "mode": h.get("mode")} if h else None,
                "installed": Path(adsbd.READSB).exists() or bool(st),
                "gain": self.cfg.aircraft_gain, "bias_tee": self.cfg.aircraft_bias_tee,
                "decoder": {k: st.get(k) for k in ("state", "detail", "since", "messages_per_s", "aircraft", "with_pos",
                                                    "last_message_at", "restarts", "gain", "bias_tee",
                                                    # why it failed (#262): an older decoder has none of these
                                                    "cause", "holder", "tail", "recovery",
                                                    # the kernel's usbfs limit and readsb's USB transfers (#269)
                                                    "usbfs_mb", "buffer_kib")} if st else None,
                "forward": fwd.stats() if fwd is not None else None}

    def _save_modes(self) -> None:
        if self._mode_file:
            try:
                by = {sd: b for sd, b in self.set_by.items() if sd in self.assign}
                self._mode_file.write_text(json.dumps({**self.assign, "_set_by": by}))
            except OSError:
                pass

    def _sdr_tag(self, serial: str) -> str:
        name = self.sdr_name(serial)
        return f"{name} ({serial})" if name != serial else serial

    def _plugged(self) -> list[str]:
        """Serials plugged in now that sizeup may use (listed, or any with sdrs = "auto"; not ignored)."""
        devs = sdr_lookup.devices(self.cfg.rtl_fm)
        self._note_products(devs)
        here = dict.fromkeys(s for _i, _p, s in devs)
        return [s for s in here if s and not self._sdr_ignored(s) and (self.cfg.sdr_auto or s in self._listed)]

    def _covered(self, but: str = "") -> set[str]:
        """The jobs running now: on a dongle that is plugged in (a job whose dongle is gone is free)."""
        return {self.assign.get(sd, OFF_MODE) for sd in self._seen if sd != but} - {OFF_MODE}

    def _next_job(self, sd: str) -> str | None:
        """The first job in [radio] auto_assign_order no plugged-in dongle has and that can run on one dongle."""
        have = self._covered(but=sd)
        for job in self.cfg.sdr_auto_order:
            if job == OFF_MODE or job not in self.modes or job in have:
                continue
            if job == P25_MODE:
                ok, why = self.recorder.one_dongle()
                if why != self._p25_skip:
                    self._p25_skip = why
                    if why:
                        log(f"sdr: auto-assign skips {P25_MODE}: {why}")
                if not ok:
                    continue
            return job
        return None

    def _program_on(self, mode: str) -> str | None:
        """The dongle the program for `mode` (its rtl_fm, or trunk-recorder) is running on, if any."""
        if mode == P25_MODE:
            return self.recorder.on() if self.recorder is not None else None
        if mode in self._sys_jobs:
            rt, k = self._sys_jobs[mode]
            return rt.recorder.source_on(k)
        r = self._receiver(mode)
        return getattr(r.device, "serial", None) if r is not None and not r._stop.is_set() else None

    def _auto_assign(self, sd: str, head: str = "") -> str | None:
        """A dongle just appeared (or was there at startup): if it has no job and a person didn't
        choose Off for it, give it the next job (_next_job). A job whose dongle is unplugged is free:
        its program stops waiting there and runs here. Never a dongle another app holds, never one
        whose auto job already failed to start this run. The job it got, or None."""
        if not (self.shared and self.cfg.sdr_auto_assign):
            return None
        head = head or f"sdr: {self._sdr_tag(sd)}"
        with self._mode_lock:
            if self.assign.get(sd, OFF_MODE) != OFF_MODE or sd in self._auto_skip:
                return None                       # has a job (picked, auto or saved): _arrive
            if self.set_by.get(sd) == "person":
                log(f"{head} stays Off (set by a person)")
                return None
            held = claims.other(sd)
            if held:
                log(f"{head} in use by {held.get('app', 'another app')} ({held.get('mode', '?')}), not auto-assigned")
                return None
            if self._ambiguity(sd):
                log(f"{head} not auto-assigned: {self._ambiguity(sd)}")
                return None
            job = self._next_job(sd)
            if job is None:
                jobs = [j for j in self.cfg.sdr_auto_order if j in self.modes]
                log(f"{head} → Off (auto: {', '.join(jobs) or 'no jobs'} already covered)")
                return None
            self.switching = True
            try:
                for o in [o for o, m in self.assign.items() if m == job and o != sd]:   # unplugged ones
                    log(f"sdr: {job}'s dongle {self._sdr_tag(o)} is unplugged: {job} goes to {self.sdr_name(sd)}")
                    self._stop_mode(job, f"moved to SDR {self.sdr_name(sd)}", o)
                ok = self._start_mode(job, sd)
            finally:
                self.switching = False
            if not ok:
                self._auto_skip.add(sd)
                log(f"{head} → {job} (auto) didn't start: left Off")
                return None
            self.assign[sd], self.set_by[sd] = job, "auto"
            self._save_modes()
            log(f"{head} → {job} (auto)")
            return job

    def _arrive(self, sd: str) -> str | None:
        """One dongle plugged in (or there at startup). Off: _auto_assign. With a job that no other
        plugged-in dongle runs: it resumes here. With a job a replacement took while it was gone: set
        by auto, it is looked at like a new dongle; set by a person, it keeps it (warned)."""
        job = self.assign.get(sd, OFF_MODE)
        if job == OFF_MODE:
            return self._auto_assign(sd)
        tag = self._sdr_tag(sd)
        with self._mode_lock:
            if self.assign.get(sd, OFF_MODE) != job:
                return None
            other = next((o for o in sorted(self._seen) if o != sd and self.assign.get(o) == job), None)
            if other is None:
                on = self._program_on(job)
                if on != sd:                            # it ran on a replacement that is gone now: back here
                    if on is not None:
                        self._stop_mode(job, f"back on SDR {self.sdr_name(sd)}", on)
                    if not self._start_mode(job, sd):
                        self.assign[sd] = OFF_MODE
                        self.set_by.pop(sd, None)
                        self._save_modes()
                    log(f"sdr: {tag} back: {job} runs here again")
                return None
            if self.set_by.get(sd) == "person":
                log(f"sdr: WARNING {tag} back with {job} (set by a person), which {self.sdr_name(other)} also has now: "
                    f"kept; {job} runs on {self.sdr_name(self._program_on(job) or other)} until a job is picked in the web app")
                return None
            self.assign[sd] = OFF_MODE                 # its program already went to the replacement
            self.set_by.pop(sd, None)
            claims.release(sd)
            self._save_modes()
            return self._auto_assign(sd, head=f"sdr: {tag} back; {job} is on {self.sdr_name(other)}")

    def _sdr_arrivals(self) -> list[str]:
        """Look at what's plugged in: every dongle not there last time is an arrival (_arrive).
        Unplugging moves nothing. Right after startup, Off dongles wait (SDR_SETTLE_S) for the ones
        that had jobs, so the order USB attaches them in doesn't move jobs around. The jobs given."""
        with self._mode_lock:
            self._guard_adsb()
        here = self._plugged()
        again = [s for s in self._watch_ambiguous(sdr_lookup.devices(self.cfg.rtl_fm)) if s in here and s in self._seen]
        new = [s for s in here if s not in self._seen] + again
        gone = self._seen - set(here)
        self._seen = set(here)
        self._departures(gone)
        waiting = time.monotonic() < self._settle_until and any(
            m != OFF_MODE and sd not in self._seen for sd, m in self.assign.items())
        if waiting:
            for sd in [s for s in new if self.assign.get(s, OFF_MODE) == OFF_MODE]:
                log(f"sdr: {self._sdr_tag(sd)} waits up to {SDR_SETTLE_S:.0f} s for the dongles that had jobs")
                self._deferred.add(sd)
                new.remove(sd)
        else:
            new = [s for s in sorted(self._deferred) if s in self._seen] + new
            self._deferred.clear()
        given = [j for sd in new if (j := self._arrive(sd))]
        self._watch_failing()
        return given

    def _departures(self, gone: set[str]) -> None:
        """A dongle unplugged: nothing is reassigned. Only when its job is also kept on a plugged-in
        dongle (a person's, beside a replacement) does the job's program go to that one."""
        with self._mode_lock:
            for g in sorted(gone):
                job = self.assign.get(g, OFF_MODE)
                if job == OFF_MODE or self._program_on(job) != g:
                    continue
                o = next((o for o in sorted(self._seen) if self.assign.get(o) == job), None)
                if o is not None:
                    self._stop_mode(job, f"SDR {self.sdr_name(g)} unplugged", g)
                    if self._start_mode(job, o):
                        log(f"sdr: {self._sdr_tag(g)} unplugged: {job} runs on {self.sdr_name(o)}, which also has it")

    def _watch_failing(self) -> None:
        """An auto job whose program keeps failing on its dongle is logged once and left where it is:
        jobs aren't moved around between running dongles."""
        for sd, m in list(self.assign.items()):
            if m == OFF_MODE or self.set_by.get(sd) != "auto" or self._program_on(m) != sd:
                self._failing.discard(sd)
                continue
            r = self.recorder if m == P25_MODE else self._sys_jobs[m][0].recorder if m in self._sys_jobs \
                else self._receiver(m)
            st = r.status() if r is not None else {}
            if st.get("state") == "down" and st.get("restarts", 0) >= SDR_FAILING_RESTARTS:
                if sd not in self._failing:
                    self._failing.add(sd)
                    log(f"sdr: {self._sdr_tag(sd)} → {m} (auto) keeps failing ({st.get('detail', '')}); "
                        f"left as is: pick its job in the web app")
            elif st.get("state") == "ok":
                self._failing.discard(sd)

    def _watch_sdrs(self) -> None:
        while not self._stopping.wait(SDR_WATCH_S):
            try:
                self._sdr_arrivals()
            except Exception as e:  # the watcher must never die
                log(f"sdr: auto-assign watch: {type(e).__name__}: {e}")

    def _dongle(self, serial: str):
        def find() -> str | None:
            i, why = sdr_lookup.lookup(serial, self.cfg.rtl_fm)
            find.why = self._ambiguity(serial) if why else ""  # type: ignore[attr-defined]
            return None if i is None else str(i)
        find.serial = serial  # type: ignore[attr-defined]
        find.why = ""  # type: ignore[attr-defined]
        find.label = self.sdr_name(serial)  # type: ignore[attr-defined]
        return find

    def _guarded(self, serial: str):
        """One dongle per channel ([[radio.channels]] device): passed to rtl_fm as configured, as before, unless two
        plugged-in dongles share that serial (#242): then nothing starts (None, and why)."""
        def find() -> str | None:
            find.why = self._ambiguity(serial, sdr_lookup.devices(self.cfg.rtl_fm, fresh=True))  # type: ignore[attr-defined]
            return None if find.why else serial  # type: ignore[attr-defined]
        find.serial, find.label, find.why = serial, self.sdr_name(serial), ""  # type: ignore[attr-defined]
        return find

    def _receiver(self, label: str) -> NfmReceiver | None:
        return next((r for r in self.receivers if r.channel.label == label), None)

    def _fresh_receiver(self, old: NfmReceiver, serial: str) -> NfmReceiver:
        c = next(c for c in self.cfg.channels if c.label == old.channel.label)
        r = make_receiver(self.cfg, c, self._dongle(serial), self._segment, self._heard_elsewhere)
        self.tap.attach(r)
        self.receivers[self.receivers.index(old)] = r
        return r

    def _running(self, r) -> bool:
        """A receiver with its program up on a dongle (its job assigned, when the dongles are switchable)."""
        return (not r._stop.is_set() and r.state == "ok"
                and (not self.shared or r.channel.label in self.assign.values()))

    def airband_ab(self, label: str, minutes: float = 60.0, phase_s: float = 300.0) -> str | None:
        """POST /api/airband-ab (#275, tools/airband_ab.py): an on-air A/B of that airband channel's dongle, rtl_fm
        against the channelizer (radio/am.py AbTest); its numbers are in the channel's Radios row ("ab") and logged at
        the end. None, or why not."""
        r = self._receiver(label)
        if not isinstance(r, AmReceiver):
            return f"no airband channel {label!r}"
        if not self._running(r):
            return f"{label} isn't running on a dongle: pick it for one first"
        if not 2 <= float(minutes) <= 24 * 60:
            return "minutes: 2 to 1440"
        return r.ab_start(float(minutes), float(phase_s))

    def _heard_elsewhere(self, mhz: float, me) -> bool:
        """#275: a frequency another running dongle has (Ground's on 124.65 overnight, and NY App 124.65's own) is kept
        by one: the dongle of the channel it belongs to, else the first one in the config. True: `me` leaves it (an
        `also` frequency isn't taken at all, radio/am.py plan_at; a transmission both had is dropped)."""
        key = round(float(mhz), 6)
        owner = airband_owner(self.cfg.channels, mhz)
        others = [r for r in list(self.receivers) if r is not me and self._running(r)
                  and key in {round(f, 6) for f in getattr(r, "active", (r.channel.mhz,))}]
        if not others or me.channel.label == owner:
            return False
        if any(r.channel.label == owner for r in others):
            return True
        order = list(self.receivers)
        mine = order.index(me) if me in order else len(order)
        return any(order.index(r) < mine for r in others)

    def _start_mode(self, mode: str, serial: str) -> bool:
        if mode == OFF_MODE:
            log(f"SDR {self.sdr_name(serial)} released: nothing of ours on it (switch it back on in the web app)")
            return True
        if self.adsb_sdr and serial == self.adsb_sdr:     # reserved for the ADS-B decoder (#235, #262): never a job
            log(f"SDR {self.sdr_name(serial)} is reserved for the ADS-B receiver: {mode} not started on it")
            return False
        if not claims.claim(serial, mode):   # another app (shortfinal) holds it: leave it alone
            c = claims.other(serial) or {}
            log(f"SDR {self.sdr_name(serial)}: in use by {c.get('app', 'another app')} ({c.get('mode', '?')}), not starting {mode}")
            return False
        if mode == P25_MODE:
            self.recorder.device_fn, self.recorder.device_name = self._dongle(serial), self.sdr_name(serial)
            self.recorder.start()
        elif mode in self._sys_jobs:                # one source of another system (#409): its trunk-recorder (re)starts
            rt, k = self._sys_jobs[mode]
            rt.recorder.give(k, self._dongle(serial), mode, apply=not self._booting)
        else:
            r = self._receiver(mode)
            if r is not None:
                if not r._stop.is_set():
                    # Still running (on another dongle): stop it before it is replaced, or its rtl_fm runs on there
                    # with nothing left that can reach it (#262: FG1's old rtl_fm kept the dongle reserved for ADS-B).
                    # (One never started, from before any dongle had the job: stopping it does nothing.)
                    if (was := getattr(r.device, "serial", None)) and was != serial:
                        log(f"sdr: {mode}'s program on SDR {self.sdr_name(was)} stopped: {mode} moves to SDR "
                            f"{self.sdr_name(serial)}")
                    r.stop()
                self._fresh_receiver(r, serial).start()
        return True

    def _guard_adsb(self) -> None:
        """A reserved dongle never runs a job (#262): any of our programs found on the ADS-B dongle is stopped."""
        sd = self.adsb_sdr
        if not sd:
            return
        for m in [m for m in self.modes if m != OFF_MODE]:
            try:
                on = self._program_on(m)
            except Exception:  # noqa: BLE001 - a guard, never in the way
                continue
            if on == sd:
                log(f"sdr: {m} was running on SDR {self.sdr_name(sd)}, the ADS-B receiver's dongle: stopped")
                self._stop_mode(m, f"SDR {self.sdr_name(sd)} is reserved for ADS-B", sd)

    def _stop_mode(self, mode: str, why: str, serial: str) -> None:
        """Stop `mode`'s program if it runs on this dongle (not when it runs on another one: two dongles
        can hold one job, a person's kept beside a replacement's) and release the dongle."""
        on = self._program_on(mode) if mode != OFF_MODE else None
        if on is None or on == serial:
            if mode == P25_MODE:
                self.recorder.stop(why)
            elif mode in self._sys_jobs:
                rt, k = self._sys_jobs[mode]
                rt.recorder.take(k, why)                 # the system runs on with its other source(s), or stops
            elif mode != OFF_MODE:
                r = self._receiver(mode)
                if r is not None:
                    r.stop()
        claims.release(serial)

    CONFIRM = "confirm: "       # what a refusal that only needs the person's confirm starts with (the UI asks, then resends)

    def fire_job(self, mode: str) -> bool:
        """A job of ours a dongle shouldn't lose by one click: the county's trunk-recorder, paging, a fireground."""
        return mode == P25_MODE or any(c.label == mode and c.kind in ("paging", "traffic") for c in self.cfg.channels)

    def set_mode(self, mode: str, sdr: str | None = None, by: str = "person", confirm: bool = False) -> str | None:
        """Give dongle `sdr` (default: the first) a channel, Fire/EMS 15 (trunk-recorder) or Off.
        A mode already running on another dongle moves here (that dongle goes Off, which auto-assign
        may fill on its next start: a person didn't choose that Off). `by` is recorded per dongle;
        a person's choice, Off included, is never changed by auto-assign."""
        if not self.shared:
            return "each channel has its own dongle: nothing to switch"
        rows = self.sdrs
        sdr = sdr or (rows[0] if rows else "")
        if sdr not in rows:
            return f"no SDR {sdr!r} plugged in" + (f"; one of {', '.join(rows)}" if rows else "")
        if mode not in self.modes:
            return f"unknown mode {mode!r}; one of {', '.join(self.modes)}"
        was = self.assign.get(sdr, OFF_MODE)
        if mode in self._sys_jobs and self.fire_job(was) and not confirm:
            # (#409) another system's job never takes a dongle off the fire side without the person saying so
            return f"{self.CONFIRM}SDR {self.sdr_name(sdr)} runs {was}. Giving it to {mode} stops {was} on it."
        held = claims.other(sdr) if mode != OFF_MODE else None
        if held:
            return f"SDR {self.sdr_name(sdr)} is in use by {held.get('app', 'another app')} ({held.get('mode', '?')})"
        why = self._ambiguity(sdr) if mode != OFF_MODE else ""
        if why:
            return f"SDR {self.sdr_name(sdr)}: {why}"
        with self._mode_lock:
            dup = mode != OFF_MODE and any(o != sdr and m == mode for o, m in self.assign.items())
            if self.assign.get(sdr, OFF_MODE) == mode and not dup:
                if self.set_by.get(sdr) != by:           # a person confirmed what auto picked: theirs now
                    self.set_by[sdr] = by
                    self._save_modes()
                return None
            self.switching = True
            try:
                for other in [o for o, m in self.assign.items() if o != sdr and m == mode != OFF_MODE]:
                    log(f"SDR {self.sdr_name(other)}: {mode} -> {OFF_MODE} (moving to {self.sdr_name(sdr)})")
                    self._stop_mode(mode, f"moved to SDR {self.sdr_name(sdr)}", other)
                    self.assign[other] = OFF_MODE
                    self.set_by.pop(other, None)
                log(f"SDR {self.sdr_name(sdr)}: {self.assign.get(sdr, OFF_MODE)} -> {mode}")
                self._stop_mode(self.assign.get(sdr, OFF_MODE), f"SDR {self.sdr_name(sdr)} on {mode}", sdr)
                time.sleep(SWITCH_PAUSE_S)  # let the dongle go before the next program opens it
                ok = self._start_mode(mode, sdr)
                self.assign[sdr] = mode if ok else OFF_MODE
                if ok:
                    self.set_by[sdr] = by
                else:
                    self.set_by.pop(sdr, None)
                self._save_modes()
                if not ok:
                    return f"SDR {self.sdr_name(sdr)} was just taken by another app"
            finally:
                self.switching = False
        return None

    def set_talkgroup(self, tg: int, record: bool | None = None, main: bool | None = None,
                      tier: str | None = None, system: str = "") -> str | None:
        """POST /api/talkgroup: record = keep its calls; main = main list + attach to our incident. Another system's
        talkgroup (#409: `system`, "" = the county's): tier = alert | text | record | off | activity."""
        if not self.p25:
            return "no P25 configured"
        system = "" if system == self.tg.county else str(system or "")
        if system and not self.tg.known(int(tg), system):
            return f"no talkgroup {tg} on {system}"
        if tier is not None:
            return self.set_talkgroup_tier(tg, tier, system)
        self.tg.set(int(tg), record, main, system)
        if system:
            log(f"talkgroup {system}:{tg} ({self.tg.name(int(tg), system)}): tier={self.tg.tier(int(tg), system)}")
            return None
        log(f"talkgroup {tg} ({self.tg.name(int(tg))}): record={self.tg.record(int(tg))} main={self.tg.main(int(tg))}")
        return None

    def _off_detail(self) -> str:
        many = len(self.sdrs) > 1
        on = [f"SDR {self.sdr_name(sd)} on {m}" if many else f"SDR on {m}" for sd, m in self.assign.items() if m != OFF_MODE]
        return "; ".join(on) if on else ("SDRs released (Off)" if many else "SDR released (Off)")

    def set_talkgroup_group(self, group: str, record: bool | None = None, main: bool | None = None) -> str | None:
        """POST /api/talkgroup-group: a whole section on/off in one go."""
        if not self.p25:
            return "no P25 configured"
        n = self.tg.set_group(str(group), record, main)
        if not n:
            return f"no talkgroups in group {group!r}"
        log(f"talkgroup group {group} ({n}): record={record} main={main}")
        return None

    def sdr(self) -> dict | None:
        if not self.shared:
            return None
        devs = sdr_lookup.devices(self.cfg.rtl_fm)
        plugged = {s: p for _i, p, s in devs}
        rows = self.sdrs
        usb = self.usb(devs)
        # #275: what each airband job is on now ("126.4", "118.575 CTAF + 120.8"), for its button
        now = {r.channel.label: r.status().get("now", "") for r in self.receivers if isinstance(r, AmReceiver)}
        return {"shared": True, "mode": self.assign.get(rows[0], OFF_MODE) if rows else OFF_MODE, "modes": self.modes,
                "now": {k: v for k, v in now.items() if v},
                "switching": self.switching, "adsb": self.adsb_row(plugged),
                "duplicates": usb["duplicates"],          # two dongles, one serial (#242): Settings > Radios shows the fix
                "sdrs": [{"serial": sd, "name": self.sdr_name(sd), "product": plugged.get(sd, ""),
                          "mode": self.assign.get(sd, OFF_MODE), "present": sd in plugged,
                          "ambiguous": self._ambiguity(sd, devs),   # "" or why nothing of ours opens it now
                          "set_by": self.set_by.get(sd),       # "person", "auto" or None (a default / unknown)
                          "held_by": ({"app": c.get("app"), "mode": c.get("mode")} if (c := claims.other(sd)) else None)}
                         for sd in rows]}

    def radio_status(self) -> list[dict]:
        out = []
        for r in self.receivers:
            st = r.status()
            if self.shared and r.channel.label not in self.assign.values():
                st.update(state="off", detail=self._off_detail(), open=False, quieting_db=None, open_s=None, stuck=False)
            out.append(st)
        if self.p25:
            st = self.p25.status()
            if self.recorder is not None:
                rs = self.recorder.status()
                if self.shared and P25_MODE not in self.assign.values():
                    st.update(state="off", detail=self._off_detail())
                elif rs["state"] != "ok":
                    st.update(state=rs["state"], detail=rs["detail"])
                else:
                    st["detail"] = rs["detail"] + (" · " + st["detail"] if st.get("detail") else "")
            out.append(st)
        for rt in self.systems.values():                # (#409) one row per other system
            out.append(rt.status(self.assign.values()))
        out.append({"label": "STT", "mhz": None, "state": self.whisper.state, "detail": self.whisper.detail,
                    "open": False, "quieting_db": None, "restarts": 0, "last_heard": None})
        engine = self.cfg.dispatch_tts_engine
        out.append(self.tts.status() if self.tts else TtsService.polly_status(self.cfg.dispatch_voice)
                   if engine == "polly" else TtsService.none_status() if engine == "none"
                   else TtsService.say_status(self.cfg.dispatch_voice))
        hub = self.station.hub_row() if self.station is not None else (self.link.status_row() if self.link else None)
        if hub is not None:
            out.append(hub)                          # [cloud] station: the hub connection
        return out

    def close_incident(self, iid: str) -> bool:
        """POST /api/incident/<id>/close (the monitor's incident, not the CAD's)."""
        return self.store.close(iid)

    def has_clip(self, cid: str) -> bool:
        with self._lock:
            if cid in self.clips:
                return True
        return bool(self.clips_dir and (self.clips_dir / f"{cid}.wav").is_file())

    def state(self) -> dict:
        with self._lock:
            pages = [dict(p.as_dict(), dispatch=getattr(p, "dispatch", None), history=getattr(p, "history", None) or [],
                          rerun_at=getattr(p, "rerun_at", None))
                     for p in list(self.pages)[:50]]
        st = {"time": time.time(), "radios": self.radio_status(), **self.store.state(), "pages": pages, "sdr": self.sdr(),
              "storage": getattr(self, "storage", None),
              "rerun": dict(self.rerun, pending=len(self._rerun_ids)),
              "talkgroups": self.talkgroups_state() if self.p25 else [],
              "p25_systems": self.p25_systems_state(),
              "talking": self.live.state(),
              "paging": self.paging_state(),
              "prealert": self.cad.prealert_state(),
              "channels": self.channels_state()}
        # Tell the display which entries have a recording (so ▶ isn't a dead button).
        with self._lock:
            busy = set(self._rerun_ids)
        for p in st["pages"]:
            p["has_audio"] = self.has_clip(p["id"])
            p["rerun_pending"] = p["id"] in busy
            for t in p.get("tones") or []:
                t["ours"] = self.paging.is_ours(t.get("names") or [])
        for t in st["loose_traffic"]:
            t["has_audio"] = self.has_clip(t["id"])
            t["rerun_pending"] = t["id"] in busy
        for inc in st["incidents"]:
            for t in inc["traffic"]:
                t["has_audio"] = self.has_clip(t["id"])
                t["rerun_pending"] = t["id"] in busy
            for e in inc["page_entries"]:
                e["has_audio"] = self.has_clip(e["id"])
        return st

    def scene(self) -> dict:
        from sizeup.roads import AREA_BBOX
        fh = json.loads((ROOT / "data" / "firehouses.json").read_text())["firehouses"]
        lon, lat, zoom = self.cfg.home
        # home.bounds [w, s, e, n]: our area, Port Chester and Rye Brook; the Overview frames it and the open calls in
        # it (cadmap.js areaBounds, #218)
        return {"station": {"id": self.cfg.station_id, "name": self.cfg.station_name},
                "timezone": tz.TZ_NAME,
                "home": {"center": [lon, lat], "zoom": zoom, "bounds": list(AREA_BBOX)}, "incident_zoom": 17.2,
                "tiles": "https://tiles.openfreemap.org/planet",
                "glyphs": "https://tiles.openfreemap.org/fonts/{fontstack}/{range}.pbf",
                "firehouses": fh, "categories": CATEGORIES}

    def start(self) -> App:
        self.whisper.start()
        if self.tts:
            self.tts.start()
        self.stt.start()
        threading.Thread(target=self._janitor, name="janitor", daemon=True).start()
        self.attachments.start()                  # linked transmissions copied to the attachments bucket (#323)
        threading.Thread(target=self.cad.place_index, name="places", daemon=True).start()   # built before the first "@"
        threading.Thread(target=self._watch_fm, name="talking", daemon=True).start()
        if self.shared:
            self._booting = True
            try:
                for sd, m in list(self.assign.items()):  # each dongle runs what was picked for it
                    if not self._start_mode(m, sd):         # held by shortfinal now: don't fight for it
                        self.assign[sd] = OFF_MODE
                        self.set_by.pop(sd, None)
            finally:
                self._booting = False
            self._save_modes()
            for rt in self.systems.values():            # (#409) each system once, on all the dongles it has
                if rt.recorder.sources:
                    rt.recorder.start()
                elif getattr(rt.recorder, "unit", False):
                    rt.recorder.stop(f"no dongle on {rt.system.label}")
            if getattr(self.recorder, "unit", False) and P25_MODE not in self.assign.values():
                # trunk-recorder's own service may still run what the sizeup before this one asked: no dongle has it now
                self.recorder.stop(f"no dongle on {P25_MODE}")
            if self.cfg.sdr_auto_assign:                # unassigned ones now, new ones as they're plugged in
                self._settle_until = time.monotonic() + SDR_SETTLE_S
                self._sdr_arrivals()
                threading.Thread(target=self._watch_sdrs, name="sdr-watch", daemon=True).start()
        else:
            for r in self.receivers:
                r.start()
            if self.recorder:
                self.recorder.start()
        if self.p25:
            self.p25.start()
        for rt in self.systems.values():
            rt.ingest.start()
        if self.vocab is not None and self.vocab.state()["state"] == "PENDING":
            self.vocab.poll()
        if getattr(self, "atc_vocab", None) is not None and self.atc_vocab.state()["state"] == "PENDING":
            self.atc_vocab.poll()
        if self.station is not None:
            self.station.start()
        elif self.link is not None:
            self.link.start()
        self._started = True
        self.prealert_sync()
        self.start_sources()
        if self.cfg.cloud_role != "station":   # the station page has no maps: don't spend 511NY calls there
            self.start_layers()
        return self

    def start_layers(self) -> None:
        """The maps' data: one list for the all-in-one app and the hub (sizeup/cloud/hub.py HubApp), so
        neither can miss one (#225: the hub never started the road projects reader).
        The first time, in the background (#251): the caches restored from S3, the county GIS layers read from
        disk, then the providers started (_load_layers); the CAD serves meanwhile, each layer saying "loading"."""
        with self._layers_lock:
            self._layers_on = True
            if not self.layers_ready.is_set():
                if self._layers_thread is None:
                    self._layers_thread = threading.Thread(target=self._load_layers, name="layers-load", daemon=True)
                    self._layers_thread.start()
                return
        self._start_providers()

    def _load_layers(self) -> None:
        """start_layers()'s first time, off the request path. Never raises; whatever fails, the providers start."""
        t0 = time.monotonic()
        restored = 0
        try:
            if self.cache_mirror is not None:
                restored = self.cache_mirror.restore()
            if restored:
                self._reread_caches()
            if self.roadgraph.graph is None:
                self.roadgraph.cache_loaded = False        # (cachesync may have just put it back)
            self.roadgraph.load_cache()
            self.gis.load_cache()
        except Exception as e:  # never in the way of the layers starting: they fetch from their sources
            log(f"layers: map caches not all read ({type(e).__name__}: {e}); fetched from their sources")
        finally:
            with self._layers_lock:
                self.layers_ready.set()
                on = self._layers_on
            log(f"layers: map caches read in {time.monotonic() - t0:.1f} s"
                + (f" ({restored} restored from S3)" if restored else "") + ("" if on else "; layers stopped meanwhile"))
            if on:
                self._start_providers()

    def _route_closures(self) -> list[dict]:
        """The road closures and roadwork the routes avoid (#360): asked for only while a rig is going to a call, so
        the sources are read then too (RoadProjects reads them while someone looks)."""
        self.roadwork.ask()
        return self.roadwork.items()

    def _reread_caches(self) -> None:
        """The small providers read their caches when they were made, before cachesync put a new task's back: again."""
        for name, fn in (("roads", lambda: self.roads._load()), ("roadwork", lambda: self.roadwork._load()),
                         ("trains", lambda: (self.trains._load(), self.trains._load_points(None), self.trains.tt._load_routes(),
                                             self.trains.assets.reload())),
                         ("weather", lambda: self.weather._load()),
                         ("aircraft", lambda: self.aircraft.registry.con is None and self.aircraft.registry._open())):
            try:
                fn()
            except Exception as e:  # one provider's cache: never the others'
                log(f"layers: {name} cache not read again ({type(e).__name__}: {e})")

    def health_extra(self, fence=None) -> dict:
        """/healthz beyond the static part (#251): "hub_role" under the hub's supervisor (the app runs only as the
        leader: "active", "fenced" once the lease isn't ours and fresh; a standby task has no app: its supervisor's
        control port says "standby"), that the CAD serves, and what is still loading in the background."""
        out: dict = {"serving": True, "loading": self.loading()}
        if fence is not None:
            out["hub_role"] = "active" if fence.ok() else "fenced"
        return out

    def loading(self) -> list[str]:
        """What is still loading in the background (/healthz, the log): never the CAD itself."""
        return [] if self.layers_ready.is_set() or not self._layers_on else ["map layers"]

    def _start_providers(self) -> None:
        self.roads.start()                        # 511NY's and the Thruway's layers (sizeup/roads.py)
        self.roadwork.start()                     # road projects & closures (sizeup/roadprojects.py)
        self.gis.start()                          # the county GIS layers (sizeup/gis.py)
        self.trains.start()                       # Metro-North's trains (sizeup/trains.py)
        self.aircraft.start()                     # aircraft near HPN, while someone looks (sizeup/aircraft.py)
        self.weather.start()                      # NWS alerts, conditions, radar; NOAA tides (sizeup/weather.py)
        self.datasources.start()                  # the slow pulls' schedule, Settings > Data sources
        if self.cache_mirror is not None:
            self.cache_mirror.start()             # their caches kept in S3 as they change (sizeup/cachesync.py)

    def stop_layers(self) -> None:
        with self._layers_lock:
            self._layers_on = False
        self.roads.stop()
        self.roadwork.stop()
        self.gis.stop()
        self.trains.stop()
        self.aircraft.stop()
        self.weather.stop()
        self.datasources.stop()
        if self.cache_mirror is not None:
            self.cache_mirror.stop()

    def start_sources(self) -> None:
        """The incident sources (#297): the CAD's ticker (held reports, promotion) and this machine's adapters."""
        self.cad.sources.start()
        self.cad.radiolog.start()                 # open calls' units matched again (#370: what a restart left)
        if self.cfg.cloud_role == "hub":         # station only: the hub gets their events through the outbox
            return
        from sizeup.sources.runner import SourceRunner, context_for, load_adapters, require_station
        role = self.cfg.cloud_role or ""
        require_station(role)                      # HubRefused: a hard check, before anything is looked at
        root = Path(self.cfg.cad_db).parent if self.cfg.cad_db else Path(".")
        env = dict(os.environ)                     # as it is now: the thread may start late

        def load() -> None:                        # off the start path: a slow or hanging factory never holds it up
            try:
                adapters = load_adapters(context_for(root, log), log=log, env=env, role=role)
                if adapters and not self._stopping.is_set():
                    self.sources_runner = SourceRunner(adapters, self._source_out, log=log, role=role,
                                                       target=self.forwarding).start()
                    self.paging.on_ours = lambda: self.sources_poll_now("paging tones")
                    self.cad.sources.trigger = self.sources_poll_now      # a pre-alert on this CAD (all-in-one)
            except KeyboardInterrupt:
                raise
            except BaseException as e:  # noqa: BLE001 - never in the way of the station
                log(f"incident sources: not loaded: {type(e).__name__}")
        threading.Thread(target=load, name="sources-load", daemon=True).start()

    def sources_poll_now(self, reason: str = "") -> int:
        """Paging tones of ours, a radio or phone pre-alert (here, or the hub's job): the adapters poll now."""
        r = self.sources_runner
        return r.trigger(str(reason or "")[:40]) if r is not None else 0

    def _source_out(self, kind: str, data: dict) -> None:
        """An adapter's event or health: to the hub (a station forwarding), else the CAD here."""
        if self.forwarding():
            self._emit(kind, data)
        else:
            self.apply_source(kind, data)

    def apply_source(self, kind: str, data: dict) -> None:
        """A "source_event" / "source_health" / "source_seen" on the CAD of record (the hub applies the station's the
        same way)."""
        if kind == "source_event":
            self.cad.source_event(data)              # the incident sources (#297), then the dispatch mode (#311)
        else:
            self.cad.sources.apply(kind, data)

    def forwarding(self) -> bool:
        """This app sends what it hears to a hub, which is then the CAD of record."""
        if self.station is not None:
            return bool(self.station.forward)
        return self.cfg.cloud_role == "station"

    def prealert_sync(self) -> None:
        """The phone pre-alert on while this is the CAD of record, off while forwarding to a hub
        (at start, and on every Settings > System > Station change)."""
        pa = self.prealert
        if pa is None:
            return
        if self.forwarding():
            if pa.running or self.cad.prealert is not None:
                log("prealert: off here: this station forwards to a hub, which subscribes to Tapout's feed")
            pa.stop()
            self.cad.prealert = None
            return
        self.cad.prealert = pa
        if self._started and not pa.running:
            pa.start()

    def stop(self) -> None:
        self._stopping.set()
        self.attachments.stop()
        for r in self.receivers:
            r.stop()
        for page in self.pagehold.flush():   # pages still in their continuation window: as they are
            try:
                self._page_done(page)
            except Exception as e:
                log(f"page hold: {page.label} page not finalised: {type(e).__name__}: {e}")
        if self.recorder:
            self.recorder.shutdown()           # its own service (the station) keeps running: the next sizeup adopts it
        if self.p25:
            self.p25.stop()
        for rt in self.systems.values():
            rt.recorder.shutdown()
            rt.ingest.stop()
            rt.activity.flush(everything=True)
            rt.activity.save()
        self.tg.save_unknown()
        for sd in list(self.assign):
            claims.release(sd)
        self.whisper.stop()
        if self.aws is not None:
            self.aws.stop()
        if self.tts:
            self.tts.stop()
        if self.station is not None:
            self.station.stop()
        elif self.link is not None:
            self.link.stop()
        if self.prealert is not None:
            self.prealert.stop()
        if self.sources_runner is not None:
            self.sources_runner.stop()
        self.cad.sources.stop()
        self.cad.radiolog.stop()
        self.stop_layers()

    def _weather_voice(self, text: str) -> tuple[str | None, str]:
        """A weather warning said by the dispatch voice, no tones (sizeup/weather.py Weather.speech): (its
        audio URL, which voice) or (None, why not). Played in the browser; never keyed."""
        path, note = self.cad._render(text, tones=[])
        return (self.cad._audio_url(path), note) if path else (None, note)


def channels_reserve(app) -> tuple:
    """The channels that keep AWS past the monthly limit (sizeup/transcribe.py RESERVE_CHANNELS)."""
    aws = getattr(app, "aws", None)
    if aws is not None:
        return tuple(aws.s.reserve_channels)
    from sizeup.transcribe import RESERVE_CHANNELS
    return RESERVE_CHANNELS


def channels_model() -> str:
    from sizeup.transcribe import MODEL
    return MODEL


def make_app(cfg: Config) -> App:
    """The app for this config's role: the hub (no radios: fed by the station, sizeup/cloud/hub.py),
    or all-in-one, forwarding to the hub as the station when that is on (config.toml's [cloud] role
    = "station" until Settings > System > Station changes it: sizeup/cloud/stationmode.py)."""
    if cfg.cloud_role == "hub":
        from sizeup.cloud.adapters import hub_guard
        from sizeup.cloud.hub import HubApp
        hub_guard(cfg.cloud_role, log)     # incident-source adapters: the station's only (#301), never loaded here
        return HubApp(cfg, log=log)
    # the incident-source adapter's credentials (#301): files under $CREDENTIALS_DIRECTORY only, out of os.environ before
    # anything is started (sizeup/procenv.py)
    from sizeup.procenv import take_credentials
    take_credentials(cfg.adapter_env)
    app = App(cfg)
    from sizeup.cloud.stationmode import StationMode
    StationMode(app, cfg, log=log)     # on: sets app.events / app.link now, so nothing is missed
    app.prealert_sync()                # state/cloud.json may say otherwise than config.toml
    return app


def start_app(cfg: Config) -> App:
    """make_app() and start it; the hub's log is read for its own error spikes first (#285, sizeup/errwatch.py)."""
    if cfg.cloud_role == "hub":
        # #284: Python's sqlite3 lets the GIL go at every row it steps, and waits up to the switch interval to get it
        # back while another thread computes (a GIS layer's refresh, the state push): 5 ms a row by default, so a
        # 500-row Radio view took 1-2 s whenever anything else was busy. 1 ms: about 5x less, for a little switching.
        sys.setswitchinterval(HUB_SWITCH_S)
    app = make_app(cfg)
    if getattr(app, "error_watch", None) is not None:      # the hub: its own error spikes to the status topic
        from sizeup import errwatch
        errwatch.install(app.error_watch)
    return app.start()


def make_server(app: App, fence=None, listener=None) -> WebServer:
    cfg = app.cfg
    server = WebServer(cfg.host, cfg.port, app.scene(), app.state, app.clip, app.close_incident, app.set_mode,
                       app.set_talkgroup, app.set_talkgroup_group, app.retranscribe, cad=CadApi(app.cad),
                       station=getattr(app, "hub", None), health=health_info(cfg), access=access_verifier(cfg),
                       access_log=log if cfg.cloud_role == "hub" else None, station_mode=app.station,
                       role=cfg.cloud_role, roads=app.roads, gis=app.gis,
                       trains=app.trains, weather=app.weather, station_health=getattr(app, "health", None),
                       aircraft=app.aircraft, roadwork=app.roadwork, set_adsb=app.set_adsb_receiver,
                       airband_ab=app.airband_ab if cfg.cloud_role != "hub" else None,
                       fence=fence, health_fn=lambda: app.health_extra(fence), listener=listener)

    def changed() -> None:             # a new talker is pushed at once, not on the next second
        server.poke()
        link = app.link                # (the hub link comes and goes: Settings > System > Station)
        if link is not None:
            link.live_changed()
    app.live.on_change = changed
    app.paging.on_change = changed     # each tone pair of a page going out, as it decodes
    if app.prealert is not None:
        app.prealert.on_change = server.poke             # so is the phone ringing
    app.cad.alarms.on_change = server.poke               # and an emergency alarm (#128)
    app.cad.airport_alerts.on_change = server.poke       # and an airport pre-alert (#233)
    app.trains.on_change = server.poke                   # and Metro-North's trains, each TrainTime read (#362)
    return server


def access_verifier(cfg: Config):
    """The hub behind Cloudflare Access with its team and AUD tag set: who signed in
    (sizeup/cloud/access.py), required on every page and the CAD API. Anything else: None (as before)."""
    if cfg.cloud_role != "hub":
        return None
    if not (cfg.cloud_access_team and cfg.cloud_access_aud):
        if cfg.cloud_access_team or cfg.cloud_access_aud:
            log("cloudflare access: identity OFF: set both [cloud] access_team and access_aud "
                "(SIZEUP_ACCESS_TEAM / SIZEUP_ACCESS_AUD); the Operator field is free text")
        return None
    from sizeup.cloud.access import AccessVerifier
    log(f"cloudflare access: identity on (team {cfg.cloud_access_team}): the operator is who signed in")
    return AccessVerifier(cfg.cloud_access_team, cfg.cloud_access_aud, log=log)


def health_info(cfg: Config) -> dict:
    """/healthz: cheap, always 200 while the process serves (the container health check). The app serves the CAD
    before the map layers and the hub's audio are loaded (#251): "loading" lists what is still on its way
    (["map layers", "audio"], [] once all is in), and is never a reason for an unhealthy answer."""
    try:
        from importlib.metadata import version
        ver = version("sizeup")
    except Exception:
        from sizeup import __version__ as ver
    return {"ok": True, "version": ver, "build": os.environ.get("SIZEUP_BUILD", ""),
            "role": cfg.cloud_role or "all-in-one", "station": cfg.station_id,
            **({"public_url": cfg.cloud_public_url} if cfg.cloud_public_url else {})}


def init_state(cfg: Config) -> list[str]:
    """--init-state: create / migrate the databases, nothing else (#251: it used to build the whole app, the GIS
    layers and the S3 cache restore included, ~15 s on Fargate; the hub's supervisor runs it at every takeover,
    sizeup/handoff.py). The names of the databases."""
    made: list[str] = []
    if not cfg.cad_db:
        return made
    Cad.from_config(cfg, log=log).store.db.close()     # the schema, its migrations, the seeded lists
    made.append(cfg.cad_db)
    if cfg.cloud_role == "hub":
        from sizeup.cloud.hubstore import AudioDb, MonitorDb
        state = Path(cfg.cad_db).parent
        MonitorDb(state / "hub.sqlite").db.close()
        AudioDb(state / "audio.sqlite").db.close()
        made += [str(state / "hub.sqlite"), str(state / "audio.sqlite")]
    return made


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="sizeup", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config.toml")
    ap.add_argument("--init-state", action="store_true",
                    help="create / migrate the databases and exit (the hub container runs this before Litestream)")
    args = ap.parse_args(argv)
    try:
        cfg = load(args.config)
    except (ConfigError, FileNotFoundError) as e:
        print(f"config: {e}", file=sys.stderr)
        return 2
    if args.init_state:
        init_state(cfg)
        log(f"sizeup: databases ready ({cfg.cad_db or 'in memory'})")
        return 0
    # The hub under its supervisor (sizeup/handoff.py), started while its task stands by: warm up, then wait for the
    # takeover (#264).
    gate = os.environ.get("SIZEUP_APP_GATE", "").strip()
    if gate:
        return run_gated(cfg, Path(gate))
    # The hub under its supervisor: the lease must stay ours and fresh, or this process stops writing at once
    # (sizeup/fence.py); SIGTERM drains: new requests get 503 + Retry-After, those in flight finish, then it exits
    # (the supervisor then puts the last changes in S3 and hands over).
    from sizeup.fence import Fence
    fence = Fence.from_env(log=log)
    if fence is not None:
        if not fence.ok():
            log("fence: started without a fresh lease: not starting")
            return 75
        fence.watch()
    return serve(cfg, fence)


GATE_POLL_S = 0.05


def _gate_says(gate: Path) -> str:
    try:
        return gate.read_text().strip()
    except OSError:
        return ""


def _wait_gate(gate: Path, want: tuple[str, ...]) -> str:
    while True:
        said = _gate_says(gate)
        if said in want:
            return said
        time.sleep(GATE_POLL_S)


def _answer_gate(gate: Path, text: str) -> None:
    out = Path(f"{gate}.answer")
    tmp = out.with_name(f".{out.name}.{os.getpid()}.tmp")
    tmp.write_text(text + "\n")
    os.replace(tmp, out)


def run_gated(cfg: Config, gate: Path) -> int:
    """The hub's app started by its supervisor while the task stands by (sizeup/handoff.py, #264). Nothing here may
    write until the supervisor says so through the gate file (and the lease file says the lock is ours):

        (standby)  prewarm(): imports, the gazetteer, the map providers and their caches, the ATC rules; then wait
        "init"     the takeover: the databases promoted. --init-state's work, here (no new Python to start); the
                   answer ("ok" / "error: ...") in <gate>.answer; then wait while the supervisor starts Litestream
        "run"      the port listens at once ("updating": the supervisor starts the connector beside it), the app is
                   built and takes the socket over: the CAD serves

    "run" without "init" (a retake): straight to serving. Each step's time is logged (startup timing)."""
    from sizeup.fence import Fence
    from sizeup.timing import Phases
    warm = Phases("warm-up")
    prewarm(cfg, warm)
    _answer_gate(gate, "warm")
    log(warm.line("waiting for the takeover"))
    said = _wait_gate(gate, ("init", "run"))
    t = Phases("startup")
    fence = Fence.from_env(log=log)
    if fence is None or not fence.ok():
        _answer_gate(gate, "error: no fresh lease")
        log("fence: told to start without a fresh lease: not starting")
        return 75
    fence.watch()
    if said == "init":
        try:
            init_state(cfg)
        except Exception as e:
            _answer_gate(gate, f"error: {type(e).__name__}: {e}"[:300])
            log(f"sizeup: the databases didn't open ({type(e).__name__}: {e})")
            return 1
        _answer_gate(gate, "ok")
        log(f"sizeup: databases ready ({cfg.cad_db or 'in memory'})")
        t.mark("init-state")
        _wait_gate(gate, ("run",))
        t.mark("litestream")                      # (the supervisor starts it meanwhile)
    early = None
    try:
        from sizeup.web.updating import serve_updating
        early = serve_updating(cfg.host, cfg.port)
    except OSError as e:  # the real server binds it below, as it always did (and says why if it can't)
        log(f"sizeup: port {cfg.port} not taken early ({e})")
    t.mark("listen")
    return serve(cfg, fence, t, early)


def serve(cfg: Config, fence=None, phases=None, early=None) -> int:
    """Build the app, start it, serve until SIGTERM drains it. early: the "updating" server already on the port (its
    socket is taken over, nothing refused in between)."""
    global _STARTUP
    from sizeup.timing import Phases
    t = phases if phases is not None else Phases("startup")
    _STARTUP = t
    try:
        app = start_app(cfg)                      # make_app, the hub's errwatch on its log (#285), start
    finally:
        _STARTUP = None
    t.mark("start")
    listener = None
    if early is not None:
        early.shutdown()                          # stops answering; the socket stays open, connections queue on it
        listener = early.socket
    server = make_server(app, fence, listener)
    t.mark("server")
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, lambda *_: server.drain())
    sm = app.station
    role = (" · hub (fed by the station, no radios)" if cfg.cloud_role == "hub"
            else f" · station, forwarding to {sm.hub_url}" if sm is not None and sm.forward else "")
    log(f"sizeup: {cfg.station_name} · {len(cfg.channels)} channel(s)"
        + (f" · P25 {cfg.p25_dir}" if cfg.p25_dir and cfg.cloud_role != "hub" else "") + role
        + (f" · station page {server.url} (the CAD is on the hub)" if cfg.cloud_role == "station"
           else f" · {server.url} · CAD {server.url}cad" + f" ({cfg.cad_db or 'in memory'}; transmit off)"))
    log(t.line())
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        t0 = time.monotonic()
        app.stop()
        log(f"sizeup: stopped in {time.monotonic() - t0:.1f} s")
    return 0
