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

from sizeup.config import P25_JOB, Config, ConfigError, load
from sizeup import janitor
from sizeup.cad import Cad
from sizeup.cad.api import CadApi
from sizeup.dispatch import parse
from sizeup.incidents import IncidentStore, Traffic
from sizeup.radio.fm import RATE, Channel, NfmReceiver, Segment, id_burst_frames, speech_seconds, voice_floor_db
from sizeup.radio.mdc1200 import decode as mdc_decode
from sizeup.radio import fleetsync
from sizeup.cad.radios import fm_system
from sizeup.radio.p25 import Call, CallIngest, Piece, split_call
from sizeup.radio import claims
from sizeup.radio.live import LiveTalk
from sizeup.radio.livepage import HOLD_S, LivePaging
from sizeup.radio import sdr as sdr_lookup
from sizeup.radio.recorder import TrunkRecorder
from sizeup.radio.page import Page, PageHold, assemble, decode_tones, page_from_dict, read_twice, recording_note
from sizeup.radio.tap import LiveTap
from sizeup.cad import tonelist
from sizeup.radio.tones import ToneEvent, TonePlan, base_name
from sizeup.talkers import talker_prompt
from sizeup import channels
from sizeup.stt import LIVE, RERUN, Transcriber, WhisperClient, radio_voice, wav_bytes
from sizeup.talkgroups import TalkgroupPrefs
from sizeup.tts import TtsService
from sizeup.web.server import WebServer

ROOT = Path(__file__).resolve().parent.parent
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
P25_MODE = P25_JOB         # the shared-dongle mode that runs trunk-recorder ("Fire/EMS 15")
OFF_MODE = "Off"           # the shared dongle released for other programs (sizeup keeps running)
SWITCH_PAUSE_S = 1.0       # between stopping one program on the shared dongle and starting the next
SDR_WATCH_S = 3.0          # [radio] auto_assign: how often the plugged-in dongles are looked at
SDR_FAILING_RESTARTS = 3   # an auto job that has restarted this often and is down: logged once, left as is
SDR_SETTLE_S = 60.0        # after startup, Off dongles wait this long for the ones that had jobs (USB attach order)
EVIDENCE_LIST = 200        # an emergency call's record (App._p25_evidence): at most this many srcList / freqList entries
MIN_SPEECH_S = 0.15      # less sound than this above the dead-air floor (and no tones) = a mic click
LIVE_WATCH_S = 0.2       # FG1 talking watcher: how often the squelch is looked at (the MDC ID: radio/tap.py)
ID_EDGE_S = 1.0          # an FG1 key-up with no ID: a burst is looked for this close to either end of the carrier (#163)
NO_ID_MIN_QUIETING_DB = 20.0   # ... and "no ID sent" is said only of a carrier this strong (PCFD radios: ~40 dB at
                               # the station); weaker, a burst may be lost in the hiss: "weak", nothing said either way
CATEGORIES = {"fire": "#ff4d3d", "ems": "#4da3ff", "rescue": "#ffb020", "hazard": "#c86bff",
              "service": "#3ddc97", "unknown": "#9aa0b4"}


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


class WhisperService:
    """Keep whisper-server running (model stays loaded). A server sizeup started earlier (recorded
    in state_path) is reused when its model/flags still match the config and replaced when they
    don't; a server on the port that sizeup didn't start is used as-is, never killed.
    SIZEUP_STT_EXTERNAL=1 (the Linux station: sizeup-whisper.service runs it, so restarting sizeup
    for an update doesn't reload the model): never started here, only waited for."""

    def __init__(self, binary: str, model: str, port: int, threads: int, beam_size: int = 0,
                 state_path: str | Path | None = None):
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
                    self._proc = subprocess.Popen(self.argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
                               prompt=self._prompt)
        self.gaz = self._load_gazetteer()
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
                                 (self.clips_dir.parent / "talkgroups.json") if self.clips_dir else None)
        for t in list(self.store.loose) + [t for i in self.store.incidents for t in i.traffic]:
            if t.talkgroup is not None:
                self.tg.seed(t.talkgroup, t.end)
        self._last_tones_only: Page | None = None
        self.pagehold = PageHold(self.plan)   # paging: a page waits out its continuation window
        self.receivers = [NfmReceiver(cfg.rtl_fm, c.device, c.gain_db, Channel(c.mhz, c.label),
                                      self._segment, log, open_db=c.squelch_db, drop_db=c.drop_db,
                                      hang_s=c.hang_s, continue_s=c.continue_s)
                          for c in cfg.channels]
        self.recorder = (TrunkRecorder(cfg.p25_recorder, cfg.p25_recorder_dir or ".", log)
                         if cfg.p25_recorder else None)
        # Switchable dongles ([radio] sdrs, by exact serial): each runs one mode at a time, picked in
        # the web app; a channel / Fire/EMS 15 runs on at most one of them.
        # sdrs = "auto": every RTL-SDR plugged in gets a row. A listed serial is always shown. sdr_ignore
        # hides a dongle for good. [radio] auto_assign (default on): a dongle that appears with no job
        # a person chose gets the first job in auto_assign_order no plugged-in dongle has (_arrive);
        # off, new ones start Off and sizeup never grabs a dongle by itself.
        self._listed: list[str] = [sd for sd in (list(cfg.sdrs) or ([cfg.shared_device] if cfg.shared_device else []))
                                   if sd not in cfg.sdr_ignore]
        self.shared = bool(self._listed) or cfg.sdr_auto
        self.modes = [c.label for c in cfg.channels] + ([P25_MODE] if self.recorder else [])
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
        if self.gaz is not None:           # the place search over the same gazetteer (a note's "@", #67)
            from sizeup.geo.places import PlaceIndex
            self.cad.place_source = lambda: PlaceIndex(self.gaz)
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
        self._chan_ov: dict = self.cad.settings.channel_overrides()
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
        self.aws = self.vocab = None
        if cfg.stt_aws:
            self._aws_setup()
        # The phone pre-alert from Tapout's ntfy feed (sizeup/prealert.py), read only, on the CAD of
        # record: all-in-one or the hub. While this station forwards to a hub it is off here (the hub
        # subscribes: one alert): prealert_sync() follows Settings > System > Station.
        self.prealert = None
        self._started = False
        # The map's highway layers (sizeup/roads.py, #73): 511NY's cameras, incidents and signs where
        # it is configured, the gazetteer's mile markers and exits everywhere. Its place search is a
        # provider for the "@" place search (#67's Cad.search_places asks cad.place_providers).
        from sizeup.roads import Roads
        self.roads = Roads.from_config(cfg, log=log)
        self.cad.place_providers = [self.roads.search_places]
        # The county GIS layers (sizeup/gis.py, #91): parcels, boundaries, utilities, facilities ... fetched
        # from Westchester County GIS by the CAD of record, served to the maps from here.
        from sizeup.gis import Gis
        self.gis = Gis.from_config(cfg, log=log)
        # The camera wall's area (#125): our fire districts from the county layer, the Settings edits
        # ("camera_wall": margin, include, exclude), our departments for the municipal fallback; Settings >
        # System shows every camera with where it falls (cad.camera_area).
        self.roads.gis = self.gis
        self.roads.ours = tuple(cfg.cad_monitor_departments)
        self.roads.wall_settings = lambda: self.cad.settings.overrides().get("camera_wall") or {}
        self.cad.camera_area = self.roads.settings_rows
        # Metro-North's trains (sizeup/trains.py, #131): the MTA's feeds, read by the CAD of record while
        # someone looks at the Trains layer or an event near the tracks.
        from sizeup.trains import Trains
        self.trains = Trains.from_config(cfg, log=log)
        # one station, one mark (#162): the county's Train stations leave out the ones the Trains layer draws
        self.gis.train_stations = self.trains.stations
        # Aircraft near HPN (sizeup/aircraft.py, #155): shortfinal's PCFD-focused picture, read by the CAD of record
        # while someone looks at the Aircraft layer. It never opens a dongle: a receiver's aircraft.json is only read.
        from sizeup.aircraft import AircraftLayer
        self.aircraft = AircraftLayer.from_config(cfg, log=log, devices=lambda: sdr_lookup.devices(cfg.rtl_fm))
        # Weather and water (sizeup/weather.py, #132): NWS alerts, conditions and radar, NOAA tides; a warning
        # said by the dispatch voice when Settings > Dispatch allows it (played where the console is open).
        from sizeup.weather import Weather
        self.weather = Weather.from_config(cfg, log=log)
        self.weather.speak_on = lambda: bool(getattr(self.cad, "weather_speak", False))
        self.weather.speaker = self._weather_voice
        if cfg.prealert_enabled:
            from sizeup.prealert import PreAlerts
            self.prealert = PreAlerts(self.cad, cfg.prealert_server, cfg.prealert_ring_topic, cfg.prealert_status_topic,
                                      cfg.prealert_expire_min, cfg.prealert_heartbeat_stale_min, log=log)
            self.prealert_sync()
        # The emergency alarms' pushes (sizeup/cad/alarms.py, #128): our own ntfy topic ([alerts], sizeup/notify.py).
        if cfg.alerts_ntfy_topic:
            from sizeup.notify import Notifier
            self.cad.alarms.notifier = Notifier(cfg.alerts_ntfy_server, cfg.alerts_ntfy_topic, log)
        self.cad.alarms.public_url = cfg.cloud_public_url
        # The emergency window (alarms.Windows): the button's talkgroup and radio kept this long, Record off or on
        self.cad.alarms.windows.keep_s = cfg.p25_emergency_window_min * 60
        self.cad.alarms.on_clear = self._all_clear_sent
        # An aircraft emergency near HPN (a 7500 / 7600 / 7700 squawk, #155) raises the same alarm and push, on the
        # CAD of record only (a station forwarding to the hub leaves it to the hub).
        self.aircraft.on_emergency = lambda ev: None if self.forwarding() else self.cad.alarms.on_aircraft(ev)
        self.aircraft.on_clear = lambda ev, aid, why: self.cad.alarms.aircraft_cleared(ev, aid, why)
        if self.recorder is not None:
            self.recorder.on_line = self.live.feed_line
        self._fm_live: dict[str, dict] = {}
        self._stopping = threading.Event()
        self._backfill()

    # -- geocoding ------------------------------------------------------------------------------

    def _load_gazetteer(self):
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
                    | {"confidence": round(float(m.confidence), 2)} for m in self.gaz.candidates(q)[:20]]
        except Exception as e:
            log(f"geocode error for {q!r}: {type(e).__name__}: {e}")
            return []

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
                 read_twice=getattr(page, "read_twice", None))
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
        parts = [self.cfg.stt_prompt]
        if self.cfg.stt_talker_prompt != "off":
            parts.append(talker_prompt(getattr(item, "unit", "") or "", getattr(item, "start", None),
                                       with_time=self.cfg.stt_talker_prompt == "ids+time"))
        return " ".join(p for p in parts if p)

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
        row = {"at": round(page.start, 2), "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(page.start)),
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
        emergency = call.emergency or any(t.emergency for t in call.turns)
        windows = self.cad.alarms.windows
        if emergency:
            log(f"p25: {call.label} {call.id}: EMERGENCY flag (radio {', '.join(call.units) or 'unknown'})")
            # The emergency window (alarms.Windows): this talkgroup, and the radio that pressed it anywhere, kept for
            # [p25] emergency_window_min after this call. 5.2.0's flag is the call's (its first key-up's radio).
            first = next((t.src for t in call.turns if t.src), "") if call.emergency else ""
            pressed = list(dict.fromkeys([t.src for t in call.turns if t.emergency and t.src] + ([first] if first else [])))
            windows.open(call.talkgroup, call.label, pressed, call.end)
        kept = ""
        if not self.tg.record(call.talkgroup):  # switched off in the web app: dropped on arrival ...
            # ... but an emergency call is kept whatever the switch says (issue #128), and so is everything on its
            # talkgroup or from its radio until the window ends (All clear, or emergency_window_min later)
            why = "button" if emergency else windows.keeps(call.talkgroup, [t.src for t in call.turns if t.src], call.start)
            if not why:
                self.tg.seen(call.talkgroup, dropped=True)
                self._drop_recorder_files(call)
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
                if pc.emergency:
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
        self._drop_recorder_files(call)  # sizeup's clip is the one copy kept

    # -- the emergency alarms (sizeup/cad/alarms.py, #128) ------------------------------------------------

    def _alarm(self, t: Traffic, text: str | None = None) -> None:
        """A transmission (new, or new text) to the alarms: the CAD of record raises them. A station forwarding
        to a hub doesn't (the hub does, from the same transmission)."""
        if self.forwarding():
            return
        d = {"id": t.id, "start": t.start, "end": t.end, "source": t.source, "talkgroup": t.talkgroup, "unit": t.unit,
             "rig": t.rig, "radio_id": t.radio_id, "text": t.text if text is None else text,
             "emergency": bool(getattr(t, "emergency", False)), "via": getattr(t, "emergency_via", ""),
             "alarm_key": getattr(t, "alarm_key", "")}
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

    def _drop_recorder_files(self, call: Call) -> None:
        """trunk-recorder's WAV and JSON, once sizeup has its own clip. An emergency call's JSON is kept instead, in
        captures/emergency/ (janitor.EMERGENCY_KEEP_S, 90 days, like its alarm): the raw record to audit it by."""
        if not self.clips_dir or not call.wav_path:  # without our own clip store, keep the originals
            return
        wav = Path(call.wav_path)
        meta = wav.with_suffix(".json")
        if call.emergency or any(t.emergency for t in call.turns):
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
            attach = (item.talkgroup is None or self.tg.main(item.talkgroup)) if attach is None else bool(attach)
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
            self._alarm(item)
            if item.status != "no_speech":
                log(f"{item.source}: {'[' + item.unit + '] ' if item.unit else ''}{item.text or '(' + item.status + ')'}")
            return
        if isinstance(item, Rerun):
            return self._rerun_done(item)
        page: Page = item
        page.model = getattr(page, "model", "") or Path(self.cfg.stt_model).name  # type: ignore[attr-defined]
        d = self._dispatch(page.raw)
        page.dispatch = d.as_dict() if d else None  # type: ignore[attr-defined]
        if page.kind == "page":
            page.read_twice = read_twice(page.raw)  # type: ignore[attr-defined]   (a confirming signal, #183)
        self._save_page(page)
        self._page_to_cad(page)                     # its text in the log (before an event links it)
        self._emit("page_text", self._page_doc(page))
        log(f"{page.label} {page.kind}: {page.raw or '(' + page.status + ')'}"
            + {True: " [read twice]", False: " [not read twice]"}.get(getattr(page, "read_twice", None), ""))
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
                                     "evidence": getattr(t, "evidence", None), "kept": getattr(t, "kept", "")},
                                    replace=replace)
        except Exception as e:  # the CAD must never stop the monitor
            log(f"CAD: could not log a transmission: {type(e).__name__}: {e}")

    def _backfill(self) -> None:
        """Traffic saved before radio IDs were tied to rigs: the rig from its talker text, and every
        transmission still in the history into the communications log (ones already there are left)."""
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
        return t, channels.tg_key(t.talkgroup) if t.talkgroup is not None else t.source

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

    def _rerun_local(self, ids: list[str] | None = None, as_alt: bool = False) -> int:
        """The local model again on recordings still on disk (ids=None: all of them). Queued behind live
        traffic; results replace the text and keep the old one in history."""
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
        for t in traffic:
            if (want is None or t.id in want) and t.id not in busy and t.id not in seen:
                got = self._rerun_audio(t.id, getattr(t, "fm", True))
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
                if self.stt.submit(job, audio, priority=RERUN):
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
        if changed:
            log(f"{why} {cid}: {text or '(no speech)'}")
        self._emit("retext", {"id": cid, "text": text, "status": status, "confidence": confidence, "model": model,
                              **({"alt": alt} if alt is not None else {})})
        return changed


    # -- per-channel speech-to-text (Settings > Radios, sizeup/channels.py) --------------------------------

    def channel_pref(self, key: str) -> dict:
        """{record, transcribe, engine} for "FG1" / "South 2" / "tg:307" (a talkgroup's record is its own switch)."""
        p = channels.pref(key, self._chan_ov, self.cfg.stt_aws_channels)
        if channels.is_tg(key):
            p["record"] = self.tg.record(int(key[3:]))
        return p

    def aws_ok(self) -> tuple[bool, str]:
        """Can Amazon Transcribe take audio now? Forwarding to a hub: what the hub last said (it runs it);
        else this process's own (the hub; all-in-one with [stt] aws and credentials)."""
        if self.forwarding():
            return bool(self._hub_aws.get("ok")), str(self._hub_aws.get("why") or "")
        if self.aws is None:
            return False, "Amazon Transcribe isn't set up here ([stt] aws): the local model is used"
        return self.aws.available()

    def stt_engine(self, key: str) -> str:
        """off | local | aws | both: what runs for this channel now ("aws" falls back to local without AWS)."""
        return channels.effective(self.channel_pref(key), self.aws_ok()[0])

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
        self.stt.submit(item, audio)

    # -- Amazon Transcribe (the hub; all-in-one with [stt] aws) ---------------------------------------------

    def _aws_setup(self) -> None:
        from sizeup import aws as aws_mod
        from sizeup.transcribe import AwsTranscriber, Settings as AwsSettings, Usage, Vocabulary, respell
        cfg, store = self.cfg, self.cad.store
        reg = aws_mod.region(cfg.stt_aws_region)
        self.aws_usage = Usage(store.meta_get, store.meta_put)
        self.vocab = Vocabulary(reg, store.meta_get, store.meta_put, cfg.stt_aws_vocabulary, cfg.stt_aws_api_endpoint,
                                log=log, on_ready=self._vocab_ready)
        self.aws = AwsTranscriber(AwsSettings(region=reg, endpoint=cfg.stt_aws_endpoint, api_endpoint=cfg.stt_aws_api_endpoint,
                                              idle_s=cfg.stt_aws_idle_s, pace=cfg.stt_aws_pace, vocabulary=cfg.stt_aws_vocabulary),
                                  self.aws_usage, self._aws_done, self._aws_failed, log=log,
                                  limit_usd=self.cad.settings.aws_limit,
                                  respell=lambda t: respell(t, self.vocab.state()["respell"]))
        if self.vocab.ready():
            self.aws.vocabulary = cfg.stt_aws_vocabulary
        log(f"speech-to-text: Amazon Transcribe streaming ({reg}) for the channels set to AWS in Settings > Radios")

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
        ok, why = self.aws.available() if self.aws is not None else (False, "Amazon Transcribe isn't set up here")
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
                self.stt.submit(page, page.voice)
                return
            page.status = "error"
            self._save_page(page)
            return
        if not self._rerun_local([job.id]):
            self._retext(job.id, "", "error", None, f"{channels_model()} (failed: {why[:80]})", why="AWS Transcribe")

    # -- the radios' side of Settings > Radios (Cad.radio_ctx) ---------------------------------------------

    def channels_locked(self) -> str:
        if self.forwarding():
            return ("Channels are set on the hub (the CAD of record) while this station forwards to it: "
                    "change them there")
        return ""

    def aws_channels(self) -> tuple[str, ...]:
        return self.cfg.stt_aws_channels

    def aws_limit_default(self) -> float:
        return float(self.cfg.stt_aws_limit_usd)

    def channels_changed(self) -> None:
        self._chan_ov = self.cad.settings.channel_overrides()
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
        return self.vocab.push(phrases)

    def vocabulary_state(self) -> dict:
        if self.vocab is None:
            return {"name": self.cfg.stt_aws_vocabulary, "state": "none", "reason": "", "phrases": 0, "sig": ""}
        st = self.vocab.state()
        st.pop("respell", None)
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
        return {"ok": bool(d.get("ok")), "why": str(d.get("why") or "the hub hasn't said whether it can use AWS yet")}

    def set_channel_prefs(self, prefs: dict, aws: dict | None = None, tones: dict | None = None) -> dict:
        """From the hub (CAD of record): its whole channel map, whether it can take AWS audio now, and the
        tone pairs named there (#80; a hub from before sends none: this station's stay). Applied here and
        kept (this station's own Settings overrides, audited as "hub")."""
        clean = channels.check_map(prefs)
        changed = self.cad.settings.replace_channels(clean, "hub")
        self._chan_ov = self.cad.settings.channel_overrides()
        if isinstance(tones, dict) and self.cad.settings.replace_tones(tones, "hub"):
            self._apply_named_tones()
            changed = True
        if isinstance(aws, dict):
            new = {"ok": bool(aws.get("ok")), "why": str(aws.get("why") or "")[:300]}
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
                "sig": channels.sig(self._chan_ov, self._hub_aws["ok"], self._tone_ov)}

    def channels_sync(self) -> dict:
        if self.forwarding():
            return {"state": "hub", "detail": "Set on the hub (the CAD of record) and applied here"}
        return {"state": "local", "detail": ""}

    def channels_state(self) -> dict:
        """Settings > Radios > Channels: the choices, what AWS can do, where they stand, the cost."""
        ok, why = self.aws_ok()
        return {"prefs": self._chan_ov, "aws_channels": list(self.cfg.stt_aws_channels), "aws_ok": ok, "aws_why": why,
                "fm": [{"key": c.label, "kind": c.kind, "mhz": c.mhz} for c in self.cfg.channels],
                "sync": self.channels_sync(), "aws": self.aws_status()}

    # -- state --------------------------------------------------------------------------------------

    # -- switchable dongles ------------------------------------------------------------------------

    @property
    def sdrs(self) -> list[str]:
        """The dongles shown now: listed ones, every one plugged in (auto), and any unplugged one
        still assigned a mode (so you can see what stopped). Plug order; listed first."""
        out = list(self._listed)
        here = [s for _i, _p, s in sdr_lookup.devices(self.cfg.rtl_fm)] if self.cfg.sdr_auto else []
        for sd in here + [sd for sd, m in self.assign.items() if m != OFF_MODE]:
            if sd and sd not in out and sd not in self.cfg.sdr_ignore:
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
            if sd in self.cfg.sdr_ignore:
                continue
            m = saved.get(sd, self.assign.get(sd, OFF_MODE))
            m = m if m in self.modes and (m == OFF_MODE or m not in taken) else OFF_MODE
            self.assign[sd] = m
            if by.get(sd) in ("person", "auto") and saved.get(sd) == m:
                self.set_by[sd] = by[sd]
            taken.add(m)

    def sdr_name(self, serial: str) -> str:
        return self.cfg.sdr_names.get(serial) or serial

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
        here = dict.fromkeys(s for _i, _p, s in sdr_lookup.devices(self.cfg.rtl_fm))
        return [s for s in here if s and s not in self.cfg.sdr_ignore and (self.cfg.sdr_auto or s in self._listed)]

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
            rec = self.recorder
            alive = rec is not None and rec._thread is not None and rec._thread.is_alive()
            return getattr(rec.device_fn, "serial", None) if alive else None
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
        here = self._plugged()
        new = [s for s in here if s not in self._seen]
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
            r = self.recorder if m == P25_MODE else self._receiver(m)
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
            i = sdr_lookup.resolve(serial, self.cfg.rtl_fm)
            return None if i is None else str(i)
        find.serial = serial  # type: ignore[attr-defined]
        find.label = self.sdr_name(serial)  # type: ignore[attr-defined]
        return find

    def _receiver(self, label: str) -> NfmReceiver | None:
        return next((r for r in self.receivers if r.channel.label == label), None)

    def _fresh_receiver(self, old: NfmReceiver, serial: str) -> NfmReceiver:
        c = next(c for c in self.cfg.channels if c.label == old.channel.label)
        r = NfmReceiver(self.cfg.rtl_fm, self._dongle(serial), c.gain_db, Channel(c.mhz, c.label),
                        self._segment, log, open_db=c.squelch_db, drop_db=c.drop_db,
                        hang_s=c.hang_s, continue_s=c.continue_s)
        self.tap.attach(r)
        self.receivers[self.receivers.index(old)] = r
        return r

    def _start_mode(self, mode: str, serial: str) -> bool:
        if mode == OFF_MODE:
            log(f"SDR {self.sdr_name(serial)} released: nothing of ours on it (switch it back on in the web app)")
            return True
        if not claims.claim(serial, mode):   # another app (shortfinal) holds it: leave it alone
            c = claims.other(serial) or {}
            log(f"SDR {self.sdr_name(serial)}: in use by {c.get('app', 'another app')} ({c.get('mode', '?')}), not starting {mode}")
            return False
        if mode == P25_MODE:
            self.recorder.device_fn, self.recorder.device_name = self._dongle(serial), self.sdr_name(serial)
            self.recorder.start()
        else:
            r = self._receiver(mode)
            if r is not None:
                self._fresh_receiver(r, serial).start()
        return True

    def _stop_mode(self, mode: str, why: str, serial: str) -> None:
        """Stop `mode`'s program if it runs on this dongle (not when it runs on another one: two dongles
        can hold one job, a person's kept beside a replacement's) and release the dongle."""
        on = self._program_on(mode) if mode != OFF_MODE else None
        if on is None or on == serial:
            if mode == P25_MODE:
                self.recorder.stop(why)
            elif mode != OFF_MODE:
                r = self._receiver(mode)
                if r is not None:
                    r.stop()
        claims.release(serial)

    def set_mode(self, mode: str, sdr: str | None = None, by: str = "person") -> str | None:
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
        held = claims.other(sdr) if mode != OFF_MODE else None
        if held:
            return f"SDR {self.sdr_name(sdr)} is in use by {held.get('app', 'another app')} ({held.get('mode', '?')})"
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

    def set_talkgroup(self, tg: int, record: bool | None = None, main: bool | None = None) -> str | None:
        """POST /api/talkgroup: record = keep its calls; main = main list + attach to our incident."""
        if not self.p25:
            return "no P25 configured"
        self.tg.set(int(tg), record, main)
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
        plugged = {s: p for _i, p, s in sdr_lookup.devices(self.cfg.rtl_fm)}
        rows = self.sdrs
        return {"shared": True, "mode": self.assign.get(rows[0], OFF_MODE) if rows else OFF_MODE, "modes": self.modes,
                "switching": self.switching,
                "sdrs": [{"serial": sd, "name": self.sdr_name(sd), "product": plugged.get(sd, ""),
                          "mode": self.assign.get(sd, OFF_MODE), "present": sd in plugged,
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
              "talkgroups": self.tg.state() if self.p25 else [],
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
        fh = json.loads((ROOT / "data" / "firehouses.json").read_text())["firehouses"]
        lon, lat, zoom = self.cfg.home
        return {"station": {"id": self.cfg.station_id, "name": self.cfg.station_name},
                "timezone": "America/New_York",
                "home": {"center": [lon, lat], "zoom": zoom}, "incident_zoom": 17.2,
                "tiles": "https://tiles.openfreemap.org/planet",
                "glyphs": "https://tiles.openfreemap.org/fonts/{fontstack}/{range}.pbf",
                "firehouses": fh, "categories": CATEGORIES}

    def start(self) -> App:
        self.whisper.start()
        if self.tts:
            self.tts.start()
        self.stt.start()
        threading.Thread(target=self._janitor, name="janitor", daemon=True).start()
        threading.Thread(target=self.cad.place_index, name="places", daemon=True).start()   # built before the first "@"
        threading.Thread(target=self._watch_fm, name="talking", daemon=True).start()
        if self.shared:
            for sd, m in list(self.assign.items()):  # each dongle runs what was picked for it
                if not self._start_mode(m, sd):         # held by shortfinal now: don't fight for it
                    self.assign[sd] = OFF_MODE
                    self.set_by.pop(sd, None)
            self._save_modes()
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
        if self.vocab is not None and self.vocab.state()["state"] == "PENDING":
            self.vocab.poll()
        if self.station is not None:
            self.station.start()
        elif self.link is not None:
            self.link.start()
        self._started = True
        self.prealert_sync()
        if self.cfg.cloud_role != "station":   # the station page has no maps: don't spend 511NY calls there
            self.roads.start()
            self.gis.start()
            self.trains.start()
            self.aircraft.start()
            self.weather.start()
        return self

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
        for r in self.receivers:
            r.stop()
        for page in self.pagehold.flush():   # pages still in their continuation window: as they are
            try:
                self._page_done(page)
            except Exception as e:
                log(f"page hold: {page.label} page not finalised: {type(e).__name__}: {e}")
        if self.recorder:
            self.recorder.stop()
        if self.p25:
            self.p25.stop()
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
        self.roads.stop()
        self.gis.stop()
        self.trains.stop()
        self.aircraft.stop()
        self.weather.stop()

    def _weather_voice(self, text: str) -> tuple[str | None, str]:
        """A weather warning said by the dispatch voice, no tones (sizeup/weather.py Weather.speech): (its
        audio URL, which voice) or (None, why not). Played in the browser; never keyed."""
        path, note = self.cad._render(text, tones=[])
        return (self.cad._audio_url(path), note) if path else (None, note)


def channels_model() -> str:
    from sizeup.transcribe import MODEL
    return MODEL


def make_app(cfg: Config) -> App:
    """The app for this config's role: the hub (no radios: fed by the station, sizeup/cloud/hub.py),
    or all-in-one, forwarding to the hub as the station when that is on (config.toml's [cloud] role
    = "station" until Settings > System > Station changes it: sizeup/cloud/stationmode.py)."""
    if cfg.cloud_role == "hub":
        from sizeup.cloud.hub import HubApp
        return HubApp(cfg, log=log)
    app = App(cfg)
    from sizeup.cloud.stationmode import StationMode
    StationMode(app, cfg, log=log)     # on: sets app.events / app.link now, so nothing is missed
    app.prealert_sync()                # state/cloud.json may say otherwise than config.toml
    return app


def make_server(app: App) -> WebServer:
    cfg = app.cfg
    server = WebServer(cfg.host, cfg.port, app.scene(), app.state, app.clip, app.close_incident, app.set_mode,
                       app.set_talkgroup, app.set_talkgroup_group, app.retranscribe, cad=CadApi(app.cad),
                       station=getattr(app, "hub", None), health=health_info(cfg), access=access_verifier(cfg),
                       access_log=log if cfg.cloud_role == "hub" else None, station_mode=app.station,
                       role=cfg.cloud_role, roads=app.roads, gis=app.gis,
                       trains=app.trains, weather=app.weather, station_health=getattr(app, "health", None),
                       aircraft=app.aircraft)

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
    """/healthz: cheap, always 200 while the process serves (the container health check)."""
    try:
        from importlib.metadata import version
        ver = version("sizeup")
    except Exception:
        from sizeup import __version__ as ver
    return {"ok": True, "version": ver, "build": os.environ.get("SIZEUP_BUILD", ""),
            "role": cfg.cloud_role or "all-in-one", "station": cfg.station_id,
            **({"public_url": cfg.cloud_public_url} if cfg.cloud_public_url else {})}


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
    app = make_app(cfg)
    if args.init_state:
        log(f"sizeup: databases ready ({cfg.cad_db or 'in memory'})")
        return 0
    app.start()
    server = make_server(app)
    signal.signal(signal.SIGTERM, lambda *_: server.shutdown())
    sm = app.station
    role = (" · hub (fed by the station, no radios)" if cfg.cloud_role == "hub"
            else f" · station, forwarding to {sm.hub_url}" if sm is not None and sm.forward else "")
    log(f"sizeup: {cfg.station_name} · {len(cfg.channels)} channel(s)"
        + (f" · P25 {cfg.p25_dir}" if cfg.p25_dir and cfg.cloud_role != "hub" else "") + role
        + (f" · station page {server.url} (the CAD is on the hub)" if cfg.cloud_role == "station"
           else f" · {server.url} · CAD {server.url}cad" + f" ({cfg.cad_db or 'in memory'}; transmit off)"))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.stop()
    return 0
