"""The CAD service: what the web API and the monitor bridge call. Validates input (CadError =
a 400 with the reason), keeps the lists, store, run cards, dispatch audio and the transmitter together.

    monitor incident for Port Chester (60 Control page with PCFD's or Rye Brook's tones) -> from_monitor()
              -> queued CAD event (any other page stays a page row in the feed and Radio, #80)
    operator: Event info -> Accept event (numbered; emergency -> Pending events, else Non-emergency)
              -> recommend -> preview -> dispatch (assign, log, audio, NullTransmitter) -> Active events
              -> Close event with a disposition
    radio traffic (monitor) -> record_traffic(): the rig from the radio registry (radios.py), linked
              to the events its unit (or its tactical channel) was on (comms.py) -> /comms, event "Radio traffic"
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from sizeup import polly, sourcekeys, tiers
from sizeup.cad import airport, dispatcher, eta, export, mapview, runcards
from sizeup.cad.activity import Activity
from sizeup.cad.alarms import Alarms
from sizeup.cad.automode import AutoDispatch
from sizeup.cad.comms import (COUNT_MINUTES, MAIN_LIMIT, MAX_ROWS, MUTE_MAX, OTHER_LIMIT, PAGE, PAGE_MAX, SOURCES_MAX,
                              VIEWS, Comms)
from sizeup.cad.feed import Feed
from sizeup.cad.newids import NewIds
from sizeup.cad.statushints import StatusHints
from sizeup.cad.sources import PAGING_RANK, Sources
from sizeup.cad.radiolog import RadioLog
from sizeup.cad import scope as scope_mod
from sizeup.cad.scope import Scope
from sizeup.cad.radios import Registry, RadioError, is_pcfd
from sizeup.cad import settings as settings_mod
from sizeup.cad import lists as lists_mod
from sizeup.cad.dispatcher import AudioUnavailable
from sizeup.cad.store import OPERATOR_MAX, TZ, CadError, Store
from sizeup.cad.transmit import Transmitter, make_transmitter
from sizeup.radio.tones import base_name
from sizeup.tts import VoiceCfg
from sizeup.tts import voices as tts_voices

AUDIO_NAME = re.compile(r"[0-9a-f]{20}")
COMMS_CACHE_S, COMMS_CACHE_N = 10.0, 32     # GET /api/cad/comms: an answer reused this long, at most this many kept (#284)
# GET /api/cad/comms: any of these asks for a page of a view (#415, Cad._comms_page), none: Main and Other as before
COMMS_PAGE_PARAMS = ("view", "system", "group", "source", "service", "tier", "dept", "safety", "mute", "before", "changed", "at")
COMMS_UNSENT = ("call_id", "turn", "updated_at")   # radio_traffic columns the Radio view never reads (#284: smaller answer)
FIRE_KINDS = ("chief", "engine", "ladder", "tower", "rescue", "utility", "marine")
PREVIEW_KEEP_S = 24 * 3600
MAX_UNITS = 30
VOICE_SAMPLE = "Engine 60, respond to 1 Main Street, structure fire. Time out 14:32."   # Settings > Voice > Preview
GEO_WINDOW = 0.25        # suggestions this close to the best one are ordered by town first
PLACE_KINDS = ("address", "intersection", "street", "place", "milemarker", "exit")     # a note's "@" places
PLACE_FIELDS = ("kind", "label", "sub", "lat", "lon", "source", "category", "detail", "route", "dir", "value",
                "precision", "score")


@dataclass
class Settings:
    db: str = ""                                   # "" = in memory (tests)
    audio_dir: str = ""                            # "" = next to the db (in memory: a temp dir)
    lists: str = ""                                # "" = data/cad_lists.json
    department: str = "Port Chester Fire"          # first words of every dispatch
    monitor_departments: tuple[str, ...] = ("Port Chester FD", "Rye Brook FD")   # their tones -> queued (#80)
    paging: tuple[str, ...] = ("South 2",)        # the paging channels: their rows are pages (the feed, Radio)
    tones: tuple[tuple[float, float], ...] = ((903.2, 1.0), (810.2, 3.0))
    voice: str = "Samantha"                        # tts_engine "say": a `say -v` voice; "kokoro": a Kokoro voice; "polly": "Matthew"
    rate_wpm: int = 0
    tts_engine: str = "say"                        # "kokoro" | "polly" | "say" | "none"; the config's default is kokoro, "say" here
    speed: float = 1.0                             # keeps tests off any running TTS server (sizeup/tts.py)
    tts_port: int = 8093
    fallback_voice: str = "Samantha"
    polly_region: str = ""                         # "" = AWS_REGION (sizeup/polly.py)
    repeat: int = 2
    transmit: bool = False
    channels: tuple[str, ...] = ("FG1", "PC repeater")
    tactical: tuple[str, ...] = ("FG1",)          # analog fireground channels: their traffic goes on the active events
    incident_talkgroups: tuple[int, ...] = (307, 333)   # P25: where 60 Control answers our units
    airport_talkgroups: tuple[int, ...] = (333,)        # P25 Airport Incident: an open Airport event's (#87)
    airband: tuple[str, ...] = ()                  # HPN ATC channels (#233): never Main in Radio (busy, not ours)
    radio_seed: str = ""                           # "" = data/radio_ids.json


def _short(why: str, n: int = 160) -> str:
    return why if len(why) <= n else why[:n - 1].rstrip() + "…"


def _inner(note: str) -> str:
    """'Voice unavailable — station offline since 14:02: text only' -> 'station offline since 14:02'."""
    return note.removeprefix("Voice unavailable — ").removesuffix(": text only")


def _text(body: dict, key: str, max_len: int, required: bool = False) -> str:
    v = body.get(key, "")
    if v is None:
        v = ""
    if not isinstance(v, str):
        raise CadError(f"{key}: text")
    v = v.strip()
    if required and not v:
        raise CadError(f"{key}: required")
    if len(v) > max_len:
        raise CadError(f"{key}: at most {max_len} characters")
    return v


def _num(body: dict, key: str, lo: float, hi: float) -> float | None:
    v = body.get(key)
    if v is None or v == "":
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
        raise CadError(f"{key}: a number from {lo} to {hi}")
    return float(v)


def _count(body: dict, key: str, hi: int) -> int | None:
    """A whole number 0..hi (or a string of digits), None for none."""
    v = body.get(key)
    if v in (None, ""):
        return None
    if isinstance(v, str) and v.strip().isdigit():
        v = int(v.strip())
    if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= hi:
        raise CadError(f"{key}: a whole number up to {hi}")
    return v


MAP_LAYER_KEY = re.compile(r"[a-z][a-z0-9_:-]{0,47}")      # "cameras", "gis:hydrants"


def _map_layers(v) -> str:
    """An event's own map layer set (#111): {layer: true | false}, stored as sorted JSON; null / {} clears it
    (the viewer's default set again)."""
    if v is None or v == {}:
        return ""
    if not isinstance(v, dict) or len(v) > 64 or not all(isinstance(k, str) and MAP_LAYER_KEY.fullmatch(k) and isinstance(b, bool)
                                                         for k, b in v.items()):
        raise CadError("map_layers: {layer: true | false} (up to 64 layers), or null for the default")
    return json.dumps(dict(sorted(v.items())), separators=(",", ":"))

def _priority(body: dict) -> int | None:
    v = body.get("priority")
    if v in (None, ""):
        return None
    if isinstance(v, bool) or v not in (1, 2, 3, "1", "2", "3"):
        raise CadError("priority: 1, 2 or 3")
    return int(v)


def _bool(body: dict, key: str) -> bool | None:
    v = body.get(key)
    if v is None:
        return None
    if not isinstance(v, bool):
        raise CadError(f"{key}: true or false")
    return v


class Cad:
    def __init__(self, s: Settings, geocoder: Callable[[str], list[dict] | None] | None = None,
                 log: Callable[[str], None] = print, clock: Callable[[], float] = time.time):
        self.s, self.geocoder, self.log, self.clock = s, geocoder, log, clock
        self.lists = lists_mod.load(s.lists or None)          # ListsError: the file says what's wrong
        self.store = Store(s.db or ":memory:", clock=clock, lists=self.lists)
        for m in self.store.migrated:
            log(f"CAD: {m}")
        if s.audio_dir:
            self.audio_dir: Path | None = Path(s.audio_dir)
        elif s.db:
            self.audio_dir = Path(s.db).parent / "dispatch-audio"
        else:
            self.audio_dir = None                      # made on first render
        self.transmitter: Transmitter = make_transmitter(s.transmit, log)
        self._render_lock = threading.Lock()           # one `say` at a time
        self._state: tuple[int, dict] | None = None
        self.firehouses = mapview.load_firehouses()          # the maps' stations (a unit's home station is one)
        self.eta = eta.Params()                              # the ETA estimates' settings (Settings > Dispatch)
        self.map_display = mapview.display_zooms()           # the rigs' zooms on the maps (Settings > System, #376)
        self._clears: tuple[int, dict] | None = None         # (rev, Store.recent_clears): returning rigs
        self.settings = settings_mod.Editor(self)           # the Settings page's edits over the data file
        # Per-person activity on the hub (sizeup/cad/activity.py): who signed in, did and was refused what.
        self.activity = Activity(self.store, lambda m: self.log(m))
        self.settings.startup()
        n = self.store.sync_units()
        if n:
            log(f"CAD: added {n} units from {Path(self.lists.path).name}")
        released = self.store.release_stale()        # units left on a closed call's status (#232): back to free
        if released:
            log(f"CAD: released {len(released)} unit(s) left on a closed call's status: "
                + ", ".join(f"{r['unit']} {r['from']} -> {r['to']} ({r['number'] or 'no number'})" for r in released))
        if runcards.seed(self.store, fill=bool(self.store.migrated)):
            log("CAD: seeded default run cards (a stand-in: edit them to PCFD policy)")
        self.radios = Registry(self.store, s.radio_seed or None)
        n = self.radios.seed()
        if n:
            log(f"CAD: seeded {n} radio IDs ({len(self.radios.plan['conflicts'])} conflicts left unmapped)")
        self.comms = Comms(self.store, s.tactical, s.incident_talkgroups, clock=clock, paging=s.paging,
                           airport_tgs=s.airport_talkgroups)
        self.comms.prune()
        self._comms_cache: dict = {}       # GET /api/cad/comms answers by (filters, revs), COMMS_CACHE_S (#284)
        self._sources_cache: dict = {}     # GET /api/cad/comms/sources: {may see context: (rev, the tree)}
        # Who is talking on HPN Tower (#252, sizeup/atc_talkers.py): the callsign heard, matched to ADS-B, on each airband
        # row ("atc"), speech-derived (never a radio ID). The app hands it the transcripts and the Aircraft layer's pictures.
        from sizeup.atc_talkers import AtcTalkers
        self.atc_talkers = AtcTalkers(self, s.airband, log=log, clock=clock)
        self.comms.atc = self.atc_talkers.decorate
        # The 60 Control feed: the incident talkgroup(s) and every page on the paging channel(s) (#80).
        self.feed = Feed(self.store, lambda: self.lists, has_clip=lambda cid: self.has_clip(cid), paging=s.paging)
        # Unit statuses suggested from what is heard on our fire channels (#37): a dispatcher accepts or dismisses each.
        self.hints = StatusHints(self.store, lambda: self.lists, channels=s.tactical,
                                 talkgroups=tuple(dict.fromkeys((*s.incident_talkgroups, *s.airport_talkgroups))), log=log)
        self.comms.on_text = self._on_text
        self.comms.fix = self.hints.decorate             # the Radio rows: misheard status words put back, heard on hover
        # Incident sources, ranked (#297, sizeup/cad/sources.py): external CAD feeds first, the paging decoder the backup;
        # which may make an incident, whose fields win, the possible-incoming notices. The app starts its ticker.
        self.sources = Sources(self, clock=clock, log=log)
        # Dispatch modes (#311, sizeup/cad/automode.py): 60 Control dispatching moves a feed's incident through the call
        # by itself (accept, units, statuses, close), PCFD dispatching leaves it queued. The per-incident radio log in
        # both (sizeup/cad/radiolog.py): transmissions naming its units, address or place, or on its fireground channel.
        self.auto = AutoDispatch(self, clock=clock, log=log)
        self.sources.on_possibly = self.auto.possibly
        self.radiolog = RadioLog(self)
        # A unit put on an incident (by a person, the feed or the radio): the radio log again for it, back to the
        # incident's dispatch (#370), off this thread
        self.store.on_assigned = lambda iid, names: self.radiolog.units_soon(iid)
        self.store.on_unassigned = self.radiolog.units_soon     # ... taken off before it went: its mentions there go
        # Full department or specific units (#370, sizeup/cad/scope.py): from the page, then the feed
        self.scope = Scope(self)
        self.sources.on_page_moved = self.scope.move
        # FG1 radio IDs nobody has named yet (#163): "Radio 1234 (MDC) on FG1: who is this?" on the console.
        self.newids = NewIds(self)
        # The emergency button and a mayday heard on our fire channels (sizeup/cad/alarms.py, #128): the app feeds it
        # transmissions on the CAD of record; every console and wall board shows them until acknowledged.
        self.alarms = Alarms(self, lambda m: self.log(m))
        self.pushes = None             # sizeup/notify.py Pushes (the app sets it): Settings > System > Push notifications
        # Set by the app: who is transmitting now (sizeup/radio/live.py) and whether a clip's audio is still kept.
        self.live: Callable[[], list[dict]] = lambda: []
        self.paging: Callable[[], list[dict]] = lambda: []      # a page going out now (sizeup/radio/livepage.py)
        self.has_clip: Callable[[str], bool] = lambda cid: False
        # Is this talkgroup "Main" (the station's switch, Settings > Radios)? The app sets it; alone: the incident talkgroups.
        self.main_tg: Callable[[int], bool] = lambda tg: tg in s.incident_talkgroups
        # The Radio page's source tree (#415, comms_sources): every known talkgroup as the monitor's state lists it
        # ([{source, system, name, group, service, tier, ours, encrypted, department, last}], App.talkgroups_state; the
        # hub: the station's), and each P25 system's label ({name: label}). Without them: what the log itself says.
        self.source_rows: Callable[[], list[dict]] = lambda: []
        self.source_systems: Callable[[], dict[str, str]] = lambda: {}
        # The maps (sizeup/cad/mapview.py): live unit positions {code: {lat, lon, at, heading?, speed?, source?}}
        # from a future AVL feed (IamResponding, Red Alert, an iPad app; #358). The single input: no provider sets
        # it yet (the source isn't chosen); _feed_position() feeds fixes in the ingest shape for tests and dev.
        self.positions: Callable[[], dict[str, dict]] = lambda: {}
        self._fed: dict[str, dict] = {}
        # The probable path of each responding rig (#360, sizeup/cad/routes.py): the app sets it when the road graph
        # is on (sizeup/roadgraph.py); None: no routes, the maps draw the straight line and estimates stay straight-line.
        self.routes = None
        # [cloud] role = "hub" (sizeup/cloud/hub.py): the voice is Amazon Polly's, rendered here
        # (tts_engine "polly"), else the station's: remote_voice(job) -> (WAV bytes, "Voice: ...") or
        # AudioUnavailable, also Polly's fallback; audio_blobs keeps the dispatch audio in SQLite (the
        # hub's disk is thrown away on a redeploy). cloud(): the
        # station / hub connection for the console's header ({role, ...}), None all-in-one.
        self.remote_voice: Callable[[dict], tuple[bytes, str]] | None = None
        self.audio_blobs = None
        self.cloud: Callable[[], dict | None] = lambda: None
        # The phone pre-alert from Tapout (sizeup/prealert.py PreAlerts), set by the app on the CAD of
        # record (hub, all-in-one) when [prealert] is on; None: no pre-alerts here.
        self.prealert = None
        # Airport pre-alerts (sizeup/cad/airport_alerts.py, #233): HPN ATC's rule hits (sizeup/atc.py), and any other
        # source that hands one in (#235: our ADS-B receiver). Shown with the phone pre-alerts; accept makes a pending event.
        from sizeup.cad.airport_alerts import AirportAlerts
        self.airport_alerts = AirportAlerts(self, log=log)
        # ... and the ones that build up over several transmissions (#369, sizeup/airport_watch.py): an abnormal go-around
        # on HPN ATC followed through its frequency changes, "alert level N" on 60 Control's fire radio (_on_text)
        from sizeup.airport_watch import AirportWatch
        self.airport_watch = AirportWatch(self.airport_alerts, s.airband,
                                          fire_talkgroups=(301, *s.incident_talkgroups, *s.airport_talkgroups),
                                          log=log, clock=clock)
        # The place search (sizeup/geo/places.py, #67): the app sets place_source, a function that builds the
        # PlaceIndex over its gazetteer (once, on first use); None: no gazetteer, no place suggestions.
        self.place_source: Callable[[], object] | None = None
        self._place_ix = None
        # A new call's location (#204), set by the app over the same gazetteer (sizeup/geo/pagegeo.py):
        # detail_source(suggestion) -> {place, street_address, address_checked, cross, block} (location_detail) and
        # here_source(lat, lon, places) -> what is at a point on the map (what_is_here). None: not here.
        self.detail_source: Callable[[dict], dict] | None = None
        self.here_source: Callable[..., dict] | None = None
        # The intersection search (#213, sizeup/geo/intersections.py): cross_source builds it over the same gazetteer
        # (once, on first use); "N Main St / Westchester Ave" in the location box. None: not here.
        self.cross_source: Callable[[], object] | None = None
        self._cross_ix = None
        self._cross_lock = threading.Lock()      # its own: the PlaceIndex is built with it (under _place_lock)
        self._details: dict[tuple, dict] = {}
        self._place_lock = threading.Lock()
        # The radios' side of Settings > Radios > Channels / Vocabulary (sizeup/channels.py): the app
        # (App) answers channels_locked(), set_talkgroup(tg, record), channels_changed(), aws_channels(), record_only(),
        # vocabulary_suggestions() / _push(phrases) / _state(), aws_status(), aws_limit_default(). None: alone.
        self.radio_ctx = None
        # The tone book's names for PCFD and Rye Brook ("Port Chester · Fire", #122): their pages are queued. The app sets it.
        self.monitor_names: set[str] = set()
        # Settings > System > Camera wall (#125): every camera with where it falls in our area (sizeup/roads.py
        # Roads.settings_rows); the app sets it. None: no highway layers here.
        self.camera_area: Callable[[], dict] | None = None
        # The camera DVR's pins (#277, sizeup/dvr.py Dvr.pin(cid, on) -> its status); the app sets it.
        self.dvr_pin: Callable[[str, bool], dict] | None = None
        # Incident attachments (#323, sizeup/attachments.py Attachments); the app sets it. None: none here.
        self.attachments = None
        # Settings > Data sources (#186): the hub's data pulls (sizeup/datasources.py DataSources); the app sets it.
        # None: no data sources here (GET /api/cad/datasources says so).
        self.datasources = None
        # Metro-North's trains (#362): a counter that moves with each new read; the page reloads the layer when it does
        self.trains_rev: Callable[[], int | None] = lambda: None
        # GET /api/cad/tones (#80): the pairs named from the console and the names to pick from; the app sets it.
        self.tone_names: Callable[[], dict] = lambda: {"named": self.settings.tone_rows(), "names": list(s.monitor_departments),
                                                        "ours": list(s.monitor_departments)}

    @classmethod
    def from_config(cls, cfg, geocoder=None, log=print) -> Cad:
        return cls(Settings(db=cfg.cad_db, lists=cfg.cad_lists, department=cfg.cad_department,
                            monitor_departments=tuple(cfg.cad_monitor_departments), tones=tuple(cfg.dispatch_tones),
                            voice=cfg.dispatch_voice, rate_wpm=cfg.dispatch_rate, repeat=cfg.dispatch_repeat,
                            tts_engine=cfg.dispatch_tts_engine, speed=cfg.dispatch_speed, tts_port=cfg.dispatch_tts_port,
                            fallback_voice=cfg.dispatch_fallback_voice, polly_region=cfg.dispatch_polly_region,
                            transmit=cfg.dispatch_transmit, channels=tuple(cfg.dispatch_channels),
                            tactical=tuple(c.label for c in cfg.channels if c.kind == "traffic") or ("FG1",),
                            paging=tuple(c.label for c in cfg.channels if c.kind == "paging") or ("South 2",),
                            airband=tuple(c.label for c in cfg.channels if c.kind == "airband"),
                            incident_talkgroups=tuple(sorted(cfg.p25_incident_talkgroups))),
                   geocoder=geocoder, log=log)

    # -- state ----------------------------------------------------------------------------------

    def state(self) -> dict:
        """Pushed to /cad once a second; rebuilt only when something changed."""
        rev = self.store.rev
        if self._state is None or self._state[0] != rev:
            cards = runcards.cards(self.store)
            incs = self._with_radio(self.store.incidents())
            try:
                self.sources.decorate(incs)               # a feed's jurisdiction and run numbers on the incident (#297)
            except Exception as e:  # noqa: BLE001
                self.log(f"CAD: incident sources: {type(e).__name__}")
            self._decorate_auto(incs)
            self._state = (rev, {
                "department": self.s.department,
                "dispatch_mode": self.auto.state(),       # #311: the header's switch
                "lists": self.lists.public(),
                "units": self.store.units(),
                "incidents": incs,
                "unit_log": self.store.unit_log(),
                "run_cards": cards,
                "run_cards_default": any(c["is_default"] for c in cards),
                "transmit": {"enabled": self.transmitter.enabled, "transmitter": self.transmitter.name,
                             "channels": list(self.s.channels),
                             "note": "Transmit off: logged, played here only"},
                "audio": {"tts": self.s.tts_engine in ("kokoro", "polly") or (self.s.tts_engine != "none" and bool(
                              shutil.which("say") and shutil.which("afconvert"))),
                          "engine": self.s.tts_engine, "voice": self.s.voice, "speed": self.s.speed,
                          "fallback_voice": self.s.fallback_voice, "label": self._voice().label(),
                          "tones": [list(t) for t in self.s.tones]},
                "geocoder": self.geocoder is not None,
                "operators": self.settings.operator_names(),
                "users_rev": self._users_rev(),
                "feed": self.feed.state(),
                "overview": mapview.overview(incs, self.s.tactical),
                # Settings > System > ATC talkers (#252): the switches, for the rows' chips and the map
                "atc": {"identify": self.atc_talkers.identify, "map": self.atc_talkers.on_map,
                        "channels": sorted(self.atc_talkers.channels)},
            })
        st = self._state[1]
        now = self.clock()
        pos = self._positions(now)
        units = [dict(u, position=pos[u["name"]]) if u["name"] in pos else u for u in st["units"]] if pos else st["units"]
        # comms_rev: what GET /api/cad/comms calls its rev; the Radio view and the board ask again only when it moves (#137)
        return dict(st, units=units, map=self.map_slice(units, st["incidents"], pos, now), time=now, rev=rev,
                    comms_rev=f"{self.comms.rev}.{rev}",
                    talking=self.live(), paging=self.paging(), cloud=self.cloud(), prealert=self.prealert_state(now),
                    airport_alerts=self.airport_alerts_state(now),
                    status_hints=self.status_hints(), alarms=self.alarms_state(now), radio_ids=self.radio_ids(now),
                    sources=self.sources_state(now), trains_rev=self._trains_rev())

    def _trains_rev(self) -> int | None:
        try:
            return self.trains_rev()
        except Exception:  # noqa: BLE001 - never breaks the console
            return None

    def sources_state(self, now: float | None = None) -> dict | None:
        """The incident sources (#297): the tiers, the acting primary, the possible-incoming notices (never breaks the
        console)."""
        try:
            return self.sources.state(now)
        except Exception as e:  # noqa: BLE001
            self.log(f"CAD: incident sources: {type(e).__name__}")
            return None

    def _on_text(self, tid: str) -> None:
        """A transcript arrived or changed: the per-incident radio log (#311), status suggestions (#37), taken at once
        for a tracked incident in 60 Control mode (#311), the radio pre-alerts (#297) and an airport alert level (#369)."""
        try:
            self.radiolog.consider(tid)
        except Exception as e:  # noqa: BLE001 - the log never stops the transcript
            self.log(f"CAD: radio log: {type(e).__name__}")
        try:
            self.auto.on_hints(self.hints.consider(tid))
        finally:
            try:
                self.sources.consider_radio(tid)
            except Exception as e:  # noqa: BLE001 - a notice never stops the transcript
                self.log(f"CAD: possible incoming: {type(e).__name__}: {e}")
            try:                                          # "alert level N" for the airport (#369)
                t = self.store.one("SELECT id, channel, talkgroup, start, text, talker, dispatcher, system FROM radio_traffic WHERE id = ?", (tid,))
                if t and not sourcekeys.is_context(t):
                    self.airport_watch.heard_radio(t)
            except Exception as e:  # noqa: BLE001 - a pre-alert never stops the transcript
                self.log(f"CAD: airport alert level: {type(e).__name__}: {e}")

    def _decorate_auto(self, incs: list[dict]) -> list[dict]:
        try:
            self.auto.decorate(incs)                      # #311: each automatic step, its source and its undo
        except Exception as e:  # noqa: BLE001
            self.log(f"CAD: dispatch mode: {type(e).__name__}")
        try:
            self.scope.decorate(incs)                     # #370: full department or specific units
        except Exception as e:  # noqa: BLE001
            self.log(f"CAD: dispatch scope: {type(e).__name__}")
        return incs

    def source_event(self, d: dict) -> int | None:
        """A "source_event" on the CAD of record (#297), then the dispatch mode's part (#311). The incident id."""
        iid = self.sources.on_event(d)
        try:
            self.scope.on_feed(iid)                       # full department or specific units (#370)
        except Exception as e:  # noqa: BLE001 - the report stands whatever this does
            self.log(f"CAD: dispatch scope: {type(e).__name__}")
        self.auto.on_feed(iid)
        return iid

    def dispatch_mode(self, body: dict) -> dict:
        """POST /api/cad/dispatch-mode {mode: 60control | pcfd, operator}: the header's switch (#311)."""
        return self.auto.set_mode(_text(body, "mode", 20, required=True), _text(body, "operator", OPERATOR_MAX))

    def auto_undo(self, iid: int, body: dict) -> dict:
        """POST /api/cad/incident/<id>/auto-undo {step, operator}: an automatic step taken back (#311)."""
        sid = body.get("step")
        if isinstance(sid, bool) or not isinstance(sid, int):
            raise CadError("step: a number")
        return self.auto.undo(iid, sid, _text(body, "operator", OPERATOR_MAX))

    def _recheck_from(self, iid: int, units: list[dict]) -> None:
        """Units moved here from other calls: a call of theirs waiting to close goes ahead once its last unit is off (#311)."""
        self.auto.recheck_close([u.get("incident_id") for u in units if u.get("incident_id") and u.get("incident_id") != iid])

    def _by_hand(self, iid: int | None, uids, operator: str) -> None:
        """A person put these units on (or moved them on) the incident: automation leaves them there alone (#311)."""
        if not iid:
            return
        by = {u["id"]: u["name"] for u in self.store.units()}
        self.auto.mark_units(iid, [by.get(i) for i in uids], operator)

    def radio_ids(self, now: float | None = None) -> list[dict]:
        """The FG1 radio IDs to ask about (#163, sizeup/cad/newids.py); a fault never breaks the console."""
        try:
            return self.newids.state(self.clock() if now is None else now)
        except Exception as e:  # noqa: BLE001
            self.log(f"CAD: new radio IDs: {type(e).__name__}: {e}")
            return []

    def name_radio(self, body: dict) -> dict:
        """POST /api/cad/radio-id {system, radio_id, unit? | label? | ignore?, operator}: who a new FG1 radio is."""
        try:
            r = self.newids.answer(body, _text(body, "operator", OPERATOR_MAX))
        except RadioError as e:
            raise CadError(str(e)) from None
        if r["linked"]:
            self.log(f"CAD: radio {r['radio']['system']} {r['radio']['radio_id']} named {r['radio']['unit']}: "
                     f"its transmissions -> {', '.join(r['linked'])}")
        return r

    def status_hints(self) -> list[dict]:
        """The radio's status suggestions to show now (#37); a broken one never breaks the console."""
        try:
            return self.hints.active()
        except Exception as e:  # noqa: BLE001
            self.log(f"CAD: status suggestions: {type(e).__name__}: {e}")
            return []

    def hint(self, hid: int, action: str, body: dict) -> dict:
        """POST /api/cad/hint/<id>/accept | dismiss {operator}: a radio status suggestion taken or turned down."""
        op = _text(body, "operator", OPERATOR_MAX)
        if action != "accept":
            return self.hints.dismiss(hid, op)
        h = self.store.one("SELECT unit_id, incident_id FROM status_hints WHERE id = ?", (hid,))
        if h is not None:                                # a person's, before the write (#311 L4): where it was and goes
            was = self.store.unit(h["unit_id"])["incident_id"]
            for i in {was, h["incident_id"]}:
                self._by_hand(i, [h["unit_id"]], op)
        out = self.hints.accept(hid, op)
        self._by_hand(out.get("incident_id"), [out["id"]], op)
        self.auto.recheck_close([was] if h is not None else [])   # a unit off a call that waits to close (#311 N1)
        return out

    def alarms_state(self, now: float | None = None) -> dict | None:
        """Open alarms, notices and the just acknowledged (sizeup/cad/alarms.py; a fault never breaks the console)."""
        try:
            return self.alarms.state(now)
        except Exception as e:  # noqa: BLE001
            self.log(f"CAD: alarms: {type(e).__name__}: {e}")
            return None

    def alarm_ack(self, aid: int, body: dict) -> dict:
        return self.alarms.acknowledge(aid, body)

    def alarm_clear(self, aid: int, body: dict) -> dict:
        return self.alarms.all_clear(aid, body)

    def alarms_page(self) -> dict:
        """GET /api/cad/alarms: the state, the settings, the last 200 (newest first)."""
        return {**(self.alarms_state() or {}), "settings": self.alarms.settings_page(), "history": self.alarms.history()}

    def _users_rev(self) -> str:
        """Settings > Users as a short hash: the page asks whoami again only when it moves (a role changed, #137)."""
        rows = self.store.q("SELECT id, name, role, active, email, callsign FROM operators ORDER BY id")
        return hashlib.sha1(json.dumps(rows, separators=(",", ":"), default=str).encode()).hexdigest()[:12]

    def prealert_state(self, now: float | None = None) -> dict | None:
        """The phone pre-alerts and Tapout's health (a broken feed never breaks the console)."""
        if self.prealert is None:
            return None
        try:
            return self.prealert.state(now)
        except Exception as e:  # noqa: BLE001
            self.log(f"CAD: pre-alert: {type(e).__name__}: {e}")
            return None

    def _prealert_match(self, inc: dict) -> None:
        try:
            self.sources.created(inc)               # the possible-incoming notices it answers (#297)
        except Exception as e:  # noqa: BLE001
            self.log(f"CAD: possible incoming: {type(e).__name__}: {e}")
        if self.prealert is not None:
            try:
                self.prealert.match(inc)
            except Exception as e:  # noqa: BLE001 - the event stands whatever the pre-alert does
                self.log(f"CAD: pre-alert match: {type(e).__name__}: {e}")
        try:
            self.airport_alerts.match(inc)          # an aircraft emergency's pre-alert joins an Airport event (#235)
        except Exception as e:  # noqa: BLE001
            self.log(f"CAD: airport pre-alert match: {type(e).__name__}: {e}")

    def airport_alerts_state(self, now: float | None = None) -> dict | None:
        """The airport pre-alerts (#233; a fault never breaks the console)."""
        try:
            return self.airport_alerts.state(now)
        except Exception as e:  # noqa: BLE001
            self.log(f"CAD: airport pre-alerts: {type(e).__name__}: {e}")
            return None

    def airport_alert(self, pid: int, action: str, body: dict) -> dict:
        """POST /api/cad/airport-alert/<id>/accept | dismiss {operator, reason?}: accept makes a PENDING event (never
        a dispatch); dismiss says it is nothing."""
        return self.airport_alerts.accept(pid, body) if action == "accept" else self.airport_alerts.dismiss(pid, body)

    def prealert_dismiss(self, pid: int, body: dict) -> dict:
        if self.prealert is None:
            raise CadError("no phone pre-alerts here ([prealert] is off, or this is a station: the hub has them)")
        return self.prealert.dismiss(pid, body)

    def _positions(self, now: float) -> dict[str, dict]:
        """The AVL hook's fresh fixes (a broken feed never breaks the console)."""
        try:
            raw = self.positions() or {}
        except Exception as e:  # noqa: BLE001 - whatever the feed raised
            self.log(f"CAD: unit positions: {type(e).__name__}: {e}")
            return {}
        out = {}
        for code, p in raw.items() if isinstance(raw, dict) else ():
            f = mapview.fresh_position(p, now)
            if f:
                out[str(code)] = f
        return out

    def _feed_position(self, fix: dict) -> None:
        """Tests and local dev only (#358): one fix in mapview's ingest shape {unit, lat, lon, at, heading?,
        speed?, source}, kept as that unit's latest and handed to the maps through Cad.positions, the single
        input. Not a provider: nothing in the app calls it."""
        code, pos = mapview.ingest(fix)
        self._fed[code] = pos
        self.positions = lambda: dict(self._fed)

    def map_slice(self, units: list[dict], incs: list[dict], pos: dict, now: float) -> dict:
        tags = {c: j["tag"] for j in self.lists.joint_response.values() for c in j["units"]}
        out = mapview.map_slice(units, incs, self.lists.role, self.firehouses, pos, now, joint=lambda c: tags.get(c, ""),
                                clears=self.clears(now), params=self.eta, display=self.map_display)
        if self.routes is not None:
            try:
                out.update(self.routes.for_map(out["markers"], now, incs))     # routes, trails (#360)
            except Exception as e:  # noqa: BLE001 - routing's trouble never breaks the console
                self.log(f"CAD: routes: {type(e).__name__}: {e}")
        return out

    def clears(self, now: float) -> dict[int, dict]:
        """Each unit's latest cleared call (a returning rig's start), re-read only when something changed."""
        rev = self.store.rev
        if self._clears is None or self._clears[0] != rev:
            self._clears = (rev, self.store.recent_clears(now - mapview.RECENT_S))
        return self._clears[1]

    def locations(self, units: list[dict] | None = None, now: float | None = None) -> dict[str, dict | None]:
        """unit code -> where it is now, estimated (mapview.locate): AVL, on a call, returning, its station."""
        now = self.clock() if now is None else now
        units = self.store.units() if units is None else units
        incs = self.store.q("SELECT id, number, status, lat, lon FROM incidents WHERE status IN ('queued', 'pending', 'active')")
        return mapview.locations(units, incs, self.lists.role, self.firehouses, self._positions(now), now,
                                 self.clears(now), self.eta)

    def _with_radio(self, incs: list[dict], limit: int = 200) -> list[dict]:
        """Each event's radio traffic (newest first), with whether its audio is still kept."""
        by = self.comms.for_incidents([i["id"] for i in incs], limit)
        units = self.store.units()
        for i in incs:
            i["tag_units"] = self.tag_order(i["id"], units)
            rows = by.get(i["id"], [])
            for r in rows:
                r["has_audio"] = self.has_clip(r["id"])
            i["radio"] = rows
        return incs

    # -- events -----------------------------------------------------------------------------------

    def _fields(self, body: dict, partial: bool) -> dict:
        out: dict = {}
        for k, n in (("type", 24), ("subtype", 24), ("received_via", 40)):
            if not partial or k in body:
                out[k] = _text(body, k, n)
        for k, n in (("address", 200), ("cross_street", 200), ("caller_name", 120), ("caller_address", 200),
                     ("phone", 40), ("remarks", 4000), ("geo_label", 200)):
            if not partial or k in body:
                out[k] = _text(body, k, n)
        for k, n in (("place", 120), ("place_address", 200)):
            if k in body:
                out[k] = _text(body, k, n)
        if "emergency" in body:
            e = _bool(body, "emergency")
            if e is not None:
                out["emergency"] = e
        if not partial or "until_at" in body:
            out["until_at"] = _num(body, "until_at", 1e9, 1e10)
        if not partial or "priority" in body:
            out["priority"] = _priority(body)
        if not partial or "lat" in body or "lon" in body:
            lat, lon = _num(body, "lat", 40.0, 42.0), _num(body, "lon", -75.0, -72.0)
            if (lat is None) != (lon is None):
                raise CadError("lat and lon: both or neither")
            out["lat"], out["lon"] = lat, lon
            if lat is None:
                out["geo_label"] = ""
        self._airport_fields(body, partial, out)
        if "map_layers" in body:
            out["map_layers"] = _map_layers(body["map_layers"])
        return out

    @staticmethod
    def _airport_fields(body: dict, partial: bool, out: dict) -> None:
        """An Airport event's facts (#105): the tower's aircraft type, nature, minutes out, runway (16 / 34 / 11 / 29),
        souls on board, Level 1-5 (from the souls unless said), fuel, dangerous goods. Any event may carry them
        (the console shows them for type Airport); none given: left as they are (an edit) or empty (new)."""
        for k, n in (("acft_type", 40), ("acft_nature", 120), ("acft_fuel", 40), ("acft_cargo", 120)):
            if not partial or k in body:
                out[k] = _text(body, k, n)
        if not partial or "acft_runway" in body:
            rw = _text(body, "acft_runway", 8).upper().removeprefix("RWY").strip()
            if rw and rw not in airport.RUNWAYS:
                raise CadError("acft_runway: 16, 34, 11 or 29")
            out["acft_runway"] = rw
        for k, hi in (("acft_eta", 600), ("acft_souls", 999), ("acft_level", 5)):
            if not partial or k in body:
                out[k] = _count(body, k, hi)
        if out.get("acft_souls") is not None and ("acft_level" not in body or out.get("acft_level") is None):
            out["acft_level"] = airport.level(out["acft_souls"])      # the Level follows the souls unless said

    def _auto_geo(self, f: dict) -> None:
        """Location typed but no suggestion picked: place it if the gazetteer is sure; else no pin."""
        if f.get("lat") is not None or not f.get("address") or self.geocoder is None:
            return
        q = f["address"] + (f" cross of {f['cross_street']}" if f.get("cross_street") else "")
        m = next(iter(self._ranked(q)), None)
        if m and m["confidence"] >= 0.6:
            f["lat"], f["lon"], f["geo_label"] = m["lat"], m["lon"], m["label"]
            return
        # "I287 EB MM 10.0", "Hutch SB exit 30": a highway marker the place search knows exactly (#67)
        h = next((h for h in self._place_hits(f["address"][:120], limit=2)
                  if h["kind"] in ("milemarker", "exit") and h["quality"] >= 0.95), None)
        if h:
            f["lat"], f["lon"], f["geo_label"] = h["lat"], h["lon"], h["label"]

    @staticmethod
    def _place(f: dict, old: dict | None = None) -> None:
        """A named place's location (#204): the address is shown "<street address> @ <place>" ("575 Boston Post Rd @
        Whole Foods Market"; "@ Whole Foods Market" when the data has no street address), and the place and its street
        address are kept on their own (place, place_address). place given: the address made from it (place_address,
        else what the address says before the "@", else the address typed). An edit of the address alone keeps the place
        while the address still ends with "@ <place>" (the street address typed in front of it); else the place goes."""
        if "place" not in f and ("address" not in f or not (old or {}).get("place")):
            if old is None:                                 # a new event without one
                f["place"] = f["place_address"] = ""
            return
        place = f["place"] if "place" in f else old["place"]
        addr = f.get("address", old["address"] if old else "") or ""
        tail = f"@ {place}"
        before = addr[:-len(tail)].strip() if place and addr.lower().endswith(tail.lower()) else None
        if not place or ("place" not in f and before is None):     # none, or the address no longer names it
            f["place"] = f["place_address"] = ""
            return
        street = (before if before is not None else (f.get("place_address") or addr)).strip()[:200]
        f["place"], f["place_address"] = place, street
        f["address"] = (f"{street} {tail}" if street else tail)[:200]

    def create(self, body: dict) -> dict:
        """Accept event (Event info): numbered at once; emergency -> Pending events, else Non-emergency."""
        f = self._fields(body, partial=False)
        self._place(f)
        airport.default_location(f)            # an Airport event with no location: the terminal (#87)
        self._auto_geo(f)
        iid = self.store.create_incident(**f, source="manual", status="pending", operator=_text(body, "operator", OPERATOR_MAX))
        inc = self.store.incident(iid)
        self._prealert_match(inc)
        return inc

    def update(self, iid: int, body: dict) -> dict:
        f = self._fields(body, partial=True)
        if "place" in f or "address" in f:
            self._place(f, self.store.incident(iid))
        if "address" in f and "lat" not in f:      # moved without a picked point: re-place or unpin
            f["lat"] = f["lon"] = None
            f["geo_label"] = ""
            self._auto_geo(f)
        if not f.get("address") and f.get("lat") is None and ("type" in f or "address" in f or "lat" in f):
            old = self.store.incident(iid)              # typed Airport, or an Airport event's location cleared: the terminal (#87)
            if f.get("type", old["type"]) == airport.TYPE and ("address" in f or not old["address"]) and ("lat" in f or old["lat"] is None):
                g = dict(f, type=airport.TYPE)
                if airport.default_location(g):
                    f.update(address=g["address"], lat=g["lat"], lon=g["lon"], geo_label=g["geo_label"])
        op = _text(body, "operator", OPERATOR_MAX)
        before = self.store.incident(iid)
        after = self.store.update_incident(iid, f, op)
        # a person set these (#297): a CAD feed only notes its own type / location / pin from now on
        hand = [g for g, ks in (("type", ("type", "subtype")), ("location", ("address", "cross_street", "place")),
                                ("pin", ("lat", "lon"))) if any(k in f and before[k] != after[k] for k in ks)]
        if "pin" in hand and "lat" not in body:
            hand.remove("pin")                       # re-placed from a typed address, not a pin a person put down
        self.sources.mark_manual(iid, hand, op)
        return after

    def feed_use(self, iid: int, body: dict) -> dict:
        """POST /api/cad/incident/<id>/feed-use {field: type | location, operator}: the CAD feed's value over the
        one a person set (#297)."""
        return self.sources.feed_use(iid, _text(body, "field", 20, required=True), _text(body, "operator", OPERATOR_MAX))

    def accept(self, iid: int, body: dict) -> dict:
        """A queued 60 Control page accepted (the type can be picked in the same call)."""
        op = _text(body, "operator", OPERATOR_MAX)
        f = {k: _text(body, k, 24) for k in ("type", "subtype") if k in body}
        if f:
            before = self.store.incident(iid)
            after = self.store.update_incident(iid, f, op)
            # a person changed it (#297: a feed only notes its own from then on). The console sends the type it shows
            # with every accept: the same type and subtype back is not an edit, and the feed keeps its authority
            if any(before[k] != after[k] for k in ("type", "subtype")):
                self.sources.mark_manual(iid, ["type"], op)
        out = self.store.accept(iid, op)
        self.auto.accepted(iid)                               # #311: listed units dispatched, nothing replayed
        return out

    def dismiss(self, iid: int, body: dict) -> dict:
        return self.store.dismiss(iid, _text(body, "operator", OPERATOR_MAX), _text(body, "reason", 200))

    def close(self, iid: int, body: dict) -> dict:
        return self.store.close(iid, _text(body, "disposition", 40), _text(body, "operator", OPERATOR_MAX))

    def reopen(self, iid: int, body: dict) -> dict:
        """A closed or dismissed event back on the boards (#89): see Store.reopen. A feed's in service no longer closes
        it by itself (#311)."""
        op = _text(body, "operator", OPERATOR_MAX)
        out = self.store.reopen(iid, op)
        self.auto.mark(iid, ["close"], op)
        return out

    def note(self, iid: int, body: dict) -> dict:
        """A timeline comment. units: the unit tags picked with the autocomplete ("E63 stated that ..."):
        who said it, so the event can show that unit's notes with its radio traffic. places: the places
        picked after an "@" (#67), [{label, lat, lon, kind?, sub?, source?}], each kept only while its
        "@label" is in the text; shown as a chip that points at the spot. A place never moves the event."""
        text = _text(body, "text", 2000, required=True)
        places = self._note_places(body.get("places"), text)
        units = body.get("units") or []
        if not isinstance(units, list) or len(units) > 10 or not all(isinstance(u, str) for u in units):
            raise CadError("units: a list of up to 10 unit codes")
        codes = {u["name"].upper(): u["name"] for u in self.store.units() if u["board"] or u["incident_id"]}
        bad = [u for u in units if u.strip().upper() not in codes]
        if bad:
            raise CadError(f"units: {bad[0]!r} is not on the boards")
        tags = list(dict.fromkeys(codes[u.strip().upper()] for u in units))
        self.store.note(iid, text, _text(body, "operator", OPERATOR_MAX), units=tags, places=places)
        return {"ok": True, "units": tags, "places": places}

    @staticmethod
    def _note_places(raw, text: str) -> list[dict]:
        if raw in (None, ""):
            return []
        if not isinstance(raw, list) or len(raw) > 10:
            raise CadError("places: a list of up to 10 places")
        out, seen, low = [], set(), text.lower()
        for p in raw:
            if not isinstance(p, dict):
                raise CadError("places: each one {label, lat, lon}")
            label = _text(p, "label", 200, required=True)
            lat, lon = _num(p, "lat", 40.0, 42.0), _num(p, "lon", -75.0, -72.0)
            if lat is None or lon is None:
                raise CadError(f"places: {label!r} needs lat and lon")
            kind = _text(p, "kind", 20)
            if kind and kind not in PLACE_KINDS:
                raise CadError(f"places: kind one of {', '.join(PLACE_KINDS)}")
            if f"@{label.lower()}" not in low or label.lower() in seen:
                continue                                 # its "@label" was edited away: not a mention any more
            seen.add(label.lower())
            out.append({"label": label, "lat": round(lat, 6), "lon": round(lon, 6), "kind": kind or "place",
                        "sub": _text(p, "sub", 80), "source": _text(p, "source", 40)})
        return out

    def tag_order(self, iid: int | None, units: list[dict] | None = None) -> list[str]:
        """The unit autocomplete's order: units on this event (as assigned) first, then PCFD's and
        Rye Brook's fire units (board order), then everyone else (police, EMS, agencies)."""
        units = units if units is not None else self.store.units()
        first = [r["name"] for r in self.store.q("SELECT u.name FROM assignments a JOIN units u ON u.id = a.unit_id "
                                                 "WHERE a.incident_id = ? ORDER BY a.cleared_at IS NOT NULL, a.id",
                                                 (iid,))] if iid else []
        fire = [u["name"] for u in units if u["board"] and u["kind"] in FIRE_KINDS and not u["mutual_aid"]]
        rest = [u["name"] for u in units if u["board"]]
        return list(dict.fromkeys(first + fire + rest))

    def suggest_units(self, iid: int | None, prefix: str, limit: int = 8) -> list[str]:
        """Codes starting with what was typed (any case), in tag_order: 'e6' -> E63 (on the event), E59, E60..."""
        p = (prefix or "").strip().lower()
        if not p:
            return []
        return [c for c in self.tag_order(iid) if c.lower().startswith(p)][:limit]

    def recommend(self, iid: int, body: dict) -> dict:
        inc = self.store.incident(iid)
        if not inc["emergency"]:
            on = [u["name"] for u in self.store.units() if u["incident_id"] == iid]
            return {"card": None, "card_label": "", "units": [], "unfilled": [], "already": on,
                    "note": "Non-emergency: no run card, no dispatch. Attach units if needed."}
        typ = _text(body, "type", 24) or inc["type"]
        sub = _text(body, "subtype", 24) if "type" in body else inc["subtype"]
        r = runcards.recommend(self.store, typ, sub, iid, where=self.locations(), params=self.eta,
                               road=self.routes.to_one if self.routes is not None else None)
        return dict(r, units=[{k: u[k] for k in ("id", "name", "kind", "spare", "status", "slot", "eta_s", "eta", "reason")} | {"how": u.get("how", "")}
                              for u in r["units"]])

    def full(self, iid: int) -> dict:
        return self._decorate_auto(self._with_radio([self.store.full(iid)], limit=1000))[0]

    def history(self) -> dict:
        return {"incidents": self.store.history()}

    def admin(self) -> dict:
        return export.admin(self.store)

    def export(self, name: str) -> bytes | None:
        if name == "prealerts.csv":
            return self.prealert.csv() if self.prealert is not None else None
        if name == "airport-alerts.csv":
            return self.airport_alerts.csv()
        if name == "status-hints.csv":
            return self.hints.csv()
        return export.export(self.store, name)

    # -- dispatch -------------------------------------------------------------------------------

    def _units_for(self, body: dict, dispatching: bool = True) -> list[dict]:
        ids = body.get("unit_ids")
        if not isinstance(ids, list) or not ids or len(ids) > MAX_UNITS:
            raise CadError(f"unit_ids: a list of 1 to {MAX_UNITS} unit ids")
        if not all(isinstance(i, int) and not isinstance(i, bool) for i in ids) or len(set(ids)) != len(ids):
            raise CadError("unit_ids: distinct unit ids (numbers)")
        by_id = {u["id"]: u for u in self.store.units()}
        missing = [i for i in ids if i not in by_id]
        if missing:
            raise CadError(f"unit_ids: no unit {missing[0]}")
        agency = [by_id[i]["name"] for i in ids if by_id[i]["response"] == "agency"]
        if dispatching and agency:
            raise CadError(f"Agency units: attach, don't dispatch ({', '.join(agency)})")
        cant = [by_id[i]["name"] for i in ids if self.lists.role(by_id[i]["status"]) == "oos"
                and not (by_id[i]["response"] == "agency" and not dispatching)]
        if cant:
            raise CadError("Not available: " + ", ".join(by_id[i]["name"] + " " + by_id[i]["status"] for i in ids if by_id[i]["name"] in cant))
        return [by_id[i] for i in ids]

    def _channel(self, body: dict) -> str:
        ch = _text(body, "channel", 40) or (self.s.channels[0] if self.s.channels else "")
        if self.s.channels and ch not in self.s.channels:
            raise CadError(f"channel: one of {', '.join(self.s.channels)}")
        return ch

    def _voice(self) -> VoiceCfg:
        return VoiceCfg(self.s.tts_engine, self.s.voice, self.s.speed, self.s.tts_port, self.s.fallback_voice,
                        self.s.rate_wpm, region=self.s.polly_region)

    def tts_voices(self) -> dict:
        """GET /api/cad/tts/voices: the voices to pick from (Kokoro's, or Polly's with their engines,
        polly_engine: the one the voice last spoke with) and what's set now."""
        out = {"engine": self.s.tts_engine, "voice": self.s.voice, "speed": self.s.speed,
               "fallback_voice": self.s.fallback_voice}
        if self.s.tts_engine == "polly":
            return dict(out, voices=polly.voices(), polly_engine=polly.engine_in_use(self.s.voice, self.s.polly_region))
        return dict(out, voices=tts_voices(self.s.tts_port))

    def voice_preview(self) -> dict:
        """Settings > Dispatch > Voice > Preview: a short sample said by the voice set now, through the
        same render (and fallbacks) as a dispatch."""
        text = VOICE_SAMPLE
        path, note = self._render(text, tones=[])
        self._prune_audio()
        return {"text": text, "audio_url": self._audio_url(path), "audio_note": note}

    def _render(self, text: str, location: str = "", tones=None) -> tuple[Path | None, str]:
        """(the WAV, which voice said it: "Voice: Kokoro af_heart") or (None, why there's no audio).
        `location` is no longer needed: every number in the text is said the dispatch way."""
        if self.audio_dir is None:
            import tempfile
            self.audio_dir = Path(tempfile.mkdtemp(prefix="sizeup-cad-audio-"))
        if self.remote_voice is not None:
            return self._render_hub(text, tones)
        try:
            with self._render_lock:
                return dispatcher.render_dispatch(text, self.s.tones if tones is None else tones, self.audio_dir, self._voice(), self.s.repeat,
                                                  {c: u.get("spoken", "") for c, u in self.lists.units.items()},
                                                  log=self.log)
        except AudioUnavailable as e:
            return None, str(e)
        except OSError as e:
            return None, f"could not write the dispatch audio: {e}"

    def _render_hub(self, text: str, tones=None) -> tuple[Path | None, str]:
        """The hub's dispatch audio. Engine polly: Amazon Polly, here (no station needed); if Polly
        fails (no permission, network), the station's voice (_render_remote, its Kokoro with a man's
        voice); neither: text only, saying why ("Voice unavailable — Polly: AccessDeniedException:
        ...; station offline ...: text only"). Any other engine: the station, as before. The WAV
        is cached here and in audio_blobs by content either way.

        Seam: transmission stays station-local. When dispatches are transmitted (M2), the hub sends
        this same WAV to the station to key up and play; the voice is never made there for it."""
        v = self._voice()
        if v.engine != "polly":
            return self._render_remote(text, tones, v)
        groups = dispatcher.tone_groups(self.s.tones if tones is None else tones)
        spoken = {c: u.get("spoken", "") for c, u in self.lists.units.items()}
        key = dispatcher.dispatch_key(text, groups, v, self.s.repeat, spoken)
        path, label = self._cached(key)
        if path is not None:
            return path, label or v.label()
        try:
            with self._render_lock:
                path, label = dispatcher.render_dispatch(text, groups, self.audio_dir, v, self.s.repeat, spoken,
                                                         log=self.log, local_fallback=False)
        except AudioUnavailable as e:
            why = str(e)
            self.log(f"CAD: dispatch voice: {why}; asking the station")
            station = VoiceCfg("kokoro", polly.STATION_VOICE, v.speed, v.port, v.fallback_voice, v.rate_wpm)
            path, note = self._render_remote(text, tones, station)
            if path is None:
                return None, f"Voice unavailable — {_short(why)}; {_inner(note)}: text only"
            return path, f"{note} (station: {_short(why, 80)})"
        except OSError as e:
            return None, f"could not write the dispatch audio: {e}"
        if self.audio_blobs is not None:
            self.audio_blobs.put(key, path.read_bytes(), "dispatch", label)
        return path, label

    def _cached(self, key: str) -> tuple[Path | None, str]:
        """(the WAV, its note) already rendered on the hub: on disk, or in audio_blobs after a redeploy."""
        path = self.audio_dir / f"{key}.wav"
        blobs = self.audio_blobs
        if not path.is_file() and blobs is not None and (hit := blobs.get(key)) is not None:
            self.audio_dir.mkdir(parents=True, exist_ok=True)
            path.write_bytes(hit)
        if path.is_file():
            return path, blobs.note(key) if blobs is not None else ""
        return None, ""

    def _render_remote(self, text: str, tones=None, v: VoiceCfg | None = None) -> tuple[Path | None, str]:
        """The hub's dispatch audio from the station (same render_dispatch, its Kokoro): cached here
        and in audio_blobs by content, like a local render. No station, or no answer in time:
        (None, why) and the dispatch is text only (never a hang)."""
        v = v or self._voice()
        groups = dispatcher.tone_groups(self.s.tones if tones is None else tones)
        spoken = {c: u.get("spoken", "") for c, u in self.lists.units.items()}
        said = dispatcher.speech(text, spoken=spoken)
        key = dispatcher.audio_key(said + "\x00station", groups, v.engine, v.voice,
                                   v.speed if v.engine == "kokoro" else v.rate_wpm, self.s.repeat)
        path, label = self._cached(key)
        if path is not None:
            return path, label or v.label()
        try:
            wav, label = self.remote_voice({
                "text": text, "tones": [[list(t) for t in g] for g in groups], "repeat": self.s.repeat,
                "spoken": spoken, "voice": {"engine": v.engine, "voice": v.voice, "speed": v.speed,
                                            "fallback_voice": v.fallback_voice, "rate_wpm": v.rate_wpm}})
        except AudioUnavailable as e:
            return None, str(e)
        path = self.audio_dir / f"{key}.wav"
        try:
            self.audio_dir.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(wav)
            tmp.replace(path)
        except OSError as e:
            return None, f"could not write the dispatch audio: {e}"
        if self.audio_blobs is not None:
            self.audio_blobs.put(key, wav, "dispatch", label)
        return path, label

    def _phrase(self, typ: str, sub: str) -> str:
        """The run card's phrase for TYPE/SUBTYPE; else the data file's, a TYPE card's phrase standing
        for the type's (the subtype is always said: '{type} with multiple injuries')."""
        card = runcards.resolve(self.store, typ, sub) or {}
        if card.get("spoken") and card["type"] == lists_mod.key(typ, sub):
            return card["spoken"]
        return self.lists.phrase(typ, sub, base=card.get("spoken") or "")

    def _departments(self, units: list[dict]) -> tuple[list[str], bool]:
        """The joint-response towns among these units (in the data file's order) and whether one of
        PCFD's own (apparatus kinds, not mutual aid) is among them."""
        L = self.lists
        towns = {L.joint_town(u["name"]) for u in units} - {""}
        own = any(not L.joint_town(u["name"]) and not u.get("mutual_aid")
                  and (u["kind"] in L.full_response_kinds or not L.full_response_kinds) for u in units)
        return [t for t in L.joint_response if t in towns], own

    def _plan(self, inc: dict, units: list[dict], kind: str, full: bool) -> tuple[str, dict]:
        """Lead-in and alert tones for this transmission. Lead-in: by the units sent and those already on
        the event ('Port Chester and Rye Brook Fire', or 'Rye Brook Fire' if only theirs). Tones: the
        departments of the units sent (an upgrade or full response: also everyone on the event)."""
        L = self.lists
        sending = {u["id"] for u in units}
        on = [u for u in self.store.units() if u["incident_id"] == inc["id"] and u["id"] not in sending]
        joint, own = self._departments(units + on)
        lead = dispatcher.lead_in(self.s.department, joint, own, full, L.wording, L.lead_in_joint_only)
        t_joint, t_own = self._departments(units + on if kind == "upgrade" or full else units)
        home = dispatcher.HOME_MUNI
        groups = ([(home, list(self.s.tones))] if t_own or full or not t_joint else []) + \
                 [(t, L.dispatch_tones.get(t)) for t in t_joint]
        return lead, dispatcher.tone_plan(groups, home, list(self.s.tones))

    def _text_for(self, inc: dict, units: list[dict], kind: str, full: bool = False, info: str = "",
                  new_phrase: str = "", remarks: str = "") -> tuple[str, dict]:
        """The dispatch wording (data/cad_lists.json dispatch_wording) and its tone plan. An add-on says who
        is already on the event, by status (already_statuses), leaving out the units being sent."""
        L = self.lists
        lead, tones = self._plan(inc, units, kind, full)
        already = []
        if kind == "add_on":
            sending = {u["id"] for u in units}
            order = [r["unit_id"] for r in self.store.q("SELECT unit_id FROM assignments WHERE incident_id = ? AND "
                                                        "cleared_at IS NULL ORDER BY id", (inc["id"],))]
            on = sorted((u for u in self.store.units() if u["incident_id"] == inc["id"] and u["id"] not in sending
                         and u["response"] != "agency"),                  # EMS / PD are never said on the air
                        key=lambda u: order.index(u["id"]) if u["id"] in order else len(order))   # in the order sent
            for st in L.already_statuses:
                names = [u["spoken"] or u["name"] for u in on if u["status"] == st]
                if names:
                    already.append((L.status_phrase(st), names))
        return dispatcher.compose(kind, department=lead, incident=inc,
                                  units=[u["spoken"] or u["name"] for u in units], phrase=self._phrase(inc["type"], inc["subtype"]),
                                  at=self.clock(), full=full, already=already, info=info, new_phrase=new_phrase,
                                  wording=L.wording, remarks=remarks), tones

    def _dispatchable(self, inc: dict) -> None:
        if inc["status"] == "queued":
            raise CadError("Accept first")
        if inc["status"] not in ("pending", "active"):
            raise CadError(f"event is {inc['status']}")
        if not inc["emergency"]:
            raise CadError("Non-emergency: attach units, no dispatch")

    def _audio_url(self, path: Path | None) -> str | None:
        return f"/api/cad/audio/{path.stem}.wav" if path else None

    @staticmethod
    def _remarks(body: dict) -> str:
        return dispatcher.clean_remarks(_text(body, "remarks", dispatcher.REMARKS_MAX))

    @staticmethod
    def _tones_text(plan: dict) -> str:
        """'Port Chester + Rye Brook (not set → PC). Rye Brook tones not set — played Port Chester's.'"""
        return ". ".join([plan["label"]] + plan["warnings"])

    def _shown(self, text: str, kind: str, path: Path | None, note: str, remarks: str, plan: dict) -> dict:
        """What a preview (and a dispatch) answers: the text and audio, the remarks and tones used."""
        return {"text": text, "kind": kind, "audio_url": self._audio_url(path), "audio_note": note, "remarks": remarks,
                "tones": plan["label"], "tone_warnings": plan["warnings"]}

    def _send(self, iid: int, text: str, units: list[dict], operator: str, channel: str, kind: str, moved=(),
              remarks: str = "", plan: dict | None = None) -> dict:
        plan = plan or dispatcher.tone_plan([], dispatcher.HOME_MUNI, list(self.s.tones))
        path, note = self._render(text, dispatcher.where(self.store.incident(iid)), plan["groups"])
        tx = self.transmitter.transmit(path, channel, text)
        did = self.store.log_dispatch(iid, operator=operator, text=text, unit_ids=[u["id"] for u in units],
                                      audio_path=str(path) if path else "", audio_note=note, transmitted=tx.transmitted,
                                      channel=channel, tx_note=tx.detail, kind=kind, remarks=remarks,
                                      tones=self._tones_text(plan))
        return dict(self._shown(text, kind, path, note, remarks, plan), dispatch_id=did,
                    audio_url=f"/api/cad/dispatch/{did}.wav" if path else None,
                    transmitted=tx.transmitted, detail=tx.detail, channel=channel, moved=list(moved))

    def preview(self, iid: int, body: dict) -> dict:
        """The dispatch as it would go out now, to hear first. Nothing is assigned or logged.
        An event that already had a dispatch gets the add-on wording."""
        inc = self.store.incident(iid)
        if inc["status"] not in ("queued", "pending", "active"):
            raise CadError(f"event is {inc['status']}")
        if not inc["emergency"]:
            raise CadError("Non-emergency: attach units, no dispatch")
        units = self._units_for(body)
        remarks = self._remarks(body)
        kind = "add_on" if self.store.has_dispatch(iid) else "new"
        text, plan = self._text_for(inc, units, kind, full=bool(_bool(body, "full")), remarks=remarks)
        path, note = self._render(text, dispatcher.where(inc), plan["groups"])
        self._prune_audio()
        return self._shown(text, kind, path, note, remarks, plan)

    def dispatch(self, iid: int, body: dict) -> dict:
        """Dispatch the ticked units (full: 'full department response' instead of their names).
        An event that already had a dispatch: 'Engine 61, added to the assignment...'."""
        inc = self.store.incident(iid)
        self._dispatchable(inc)
        operator = _text(body, "operator", OPERATOR_MAX, required=True)
        units = self._units_for(body)
        remarks = self._remarks(body)
        channel = self._channel(body)
        kind = "add_on" if self.store.has_dispatch(iid) else "new"
        text, plan = self._text_for(inc, units, kind, full=bool(_bool(body, "full")), remarks=remarks)
        self._by_hand(iid, [u["id"] for u in units], operator)
        moved = self.store.assign(iid, [u["id"] for u in units], operator,
                                  verb="added to the assignment" if kind == "add_on" else "")
        self._recheck_from(iid, units)
        return self._send(iid, text, units, operator, channel, "add-on" if kind == "add_on" else "dispatch", moved,
                          remarks, plan)

    def upgrade(self, iid: int, body: dict) -> dict:
        """Re-dispatch with new information: optional new type/subtype, a line of new information,
        optional extra units, full department response. {"preview": true} only renders it.
        'Port Chester Fire, upgrade on your last alarm, 169 North Main Street. Smoke in the
        structure. Now a structure fire, full department response. Time out 15:20.'"""
        inc = self.store.incident(iid)
        L = self.lists
        if inc["status"] not in ("pending", "active"):
            raise CadError("Accept first" if inc["status"] == "queued" else f"event is {inc['status']}")
        if not self.store.has_dispatch(iid):
            raise CadError("Dispatch first")
        typ, sub = (_text(body, "type", 24), _text(body, "subtype", 24)) if body.get("type") else (inc["type"], inc["subtype"])
        changed = (typ, sub) != (inc["type"], inc["subtype"])
        if changed:                                    # an old event's pair the map no longer allows: kept as it is
            try:
                L.check_type(typ, sub)
            except ValueError as e:
                raise CadError(str(e)) from None
        if not (inc["emergency"] or (changed and L.is_emergency(typ, sub))):
            raise CadError("Non-emergency: mark it emergency first")
        info = _text(body, "info", 300)
        remarks = self._remarks(body)
        full = bool(_bool(body, "full"))
        units = self._units_for(body) if body.get("unit_ids") else []
        if not (changed or info or full or units or remarks):
            raise CadError("Nothing to upgrade: add a type, info, remarks or units")
        text, plan = self._text_for(inc, units, "upgrade", full=full, info=info,
                                    new_phrase=self._phrase(typ, sub) if changed else "", remarks=remarks)
        if body.get("preview"):
            path, note = self._render(text, dispatcher.where(inc), plan["groups"])
            return self._shown(text, "upgrade", path, note, remarks, plan)
        operator = _text(body, "operator", OPERATOR_MAX, required=True)
        channel = self._channel(body)
        what = []
        if changed:
            what.append(f"{L.type_label(inc['type'], inc['subtype'])} → {L.type_label(typ, sub)}")
            self.store.update_incident(iid, {"type": typ, "subtype": sub}, operator)
        if info:
            what.append(info)
        if full:
            what.append(L.wording["full_response"])
        self.store.note(iid, "Upgrade: " + "; ".join(what or ["more units"]), operator, kind="upgrade")
        self._by_hand(iid, [u["id"] for u in units], operator)
        moved = self.store.assign(iid, [u["id"] for u in units], operator, verb="dispatched (upgrade)") if units else []
        self._recheck_from(iid, units)
        return self._send(iid, text, units, operator, channel, "upgrade", moved, remarks, plan)

    def attach(self, iid: int, body: dict) -> dict:
        """Units on an event with no dispatch (a non-emergency event: a hydrant, a closure).
        status (RS, OL...: Assigned > Add unit): a unit heard on the air responding, or showing up,
        that nobody toned out (user, 2026-10-08: "we need to 'add someone responding' even if they
        were not on the original dispatch"). Any open event, emergency too; nothing is said on the air."""
        inc = self.store.incident(iid)
        if inc["status"] not in ("pending", "active"):
            raise CadError("Accept first" if inc["status"] == "queued" else f"event is {inc['status']}")
        units = self._units_for(body, dispatching=False)
        if body.get("status") is not None:
            return self._add_responding(iid, units, body)
        op = _text(body, "operator", OPERATOR_MAX)
        self._by_hand(iid, [u["id"] for u in units], op)
        ours = [u["id"] for u in units if u["response"] != "agency"]
        agency = [u["id"] for u in units if u["response"] == "agency"]
        moved = self.store.assign(iid, ours, op, attach=True) if ours else []
        if agency:          # another agency's unit: on the event assigned, not yet responding (#219: never
            moved += self.store.assign(iid, agency, op, attach=True)    # straight to On location), then its statuses
            by = {u["id"]: u for u in units}
            for i in agency:                                            # "RBPD dispatched", "KEYHOLDER notified"
                self.store.set_unit_status(i, self.lists.agency_attach_status(by[i]["status_set"]), op)
        self._recheck_from(iid, units)
        return {"ok": True, "moved": moved}

    def _add_responding(self, iid: int, units: list[dict], body: dict) -> dict:
        """attach with a status: on the event in that status, no dispatch logged, no Disp time. A unit
        already on this event is refused; one on another call is moved, as a dispatch moves it."""
        L = self.lists
        status = _text(body, "status", 40)
        ok = self.store.return_statuses()
        if status not in ok:
            raise CadError("status: " + " or ".join(f"{c} ({L.name('unit_statuses', c)})" for c in ok))
        op = _text(body, "operator", OPERATOR_MAX, required=True)
        here = [u["name"] for u in units if u["incident_id"] == iid]
        if here:
            raise CadError(f"Already on this event: {', '.join(here)}")
        agency = [u["name"] for u in units if u["response"] == "agency" and status not in L.unit_statuses_for(u["name"], u["board"])]
        if agency:
            raise CadError(f"Another agency's unit, not {L.name('unit_statuses', status)}: {', '.join(agency)}")
        self._by_hand(iid, [u["id"] for u in units], op)
        moved = self.store.assign(iid, [u["id"] for u in units], op, status=status)
        self._recheck_from(iid, units)
        return {"ok": True, "moved": moved, "status": status}

    def _prune_audio(self) -> None:
        """Previews nobody dispatched are dropped after a day; dispatch audio is kept with the log."""
        if self.audio_dir is None or not self.audio_dir.is_dir():
            return
        keep = {Path(r["audio_path"]).name for r in self.store.q("SELECT audio_path FROM dispatches WHERE audio_path != ''")}
        cutoff = time.time() - PREVIEW_KEEP_S
        if self.audio_blobs is not None:
            self.audio_blobs.prune_dispatch({Path(k).stem for k in keep}, cutoff)
        for f in self.audio_dir.glob("*.wav"):
            try:
                if f.name not in keep and f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                pass

    def dispatch_audio(self, did: int) -> bytes | None:
        row = self.store.dispatch_row(did)
        p = Path(row["audio_path"]) if row and row["audio_path"] else None
        if p and not p.is_file() and self.audio_blobs is not None:
            return self.audio_blobs.get(p.stem)          # the hub after a redeploy: kept in SQLite
        return p.read_bytes() if p and p.is_file() else None

    def preview_audio(self, name: str) -> bytes | None:
        if not AUDIO_NAME.fullmatch(name) or self.audio_dir is None:
            return None
        p = self.audio_dir / f"{name}.wav"
        if not p.is_file() and self.audio_blobs is not None:
            return self.audio_blobs.get(name)
        return p.read_bytes() if p.is_file() else None

    # -- units and run cards ----------------------------------------------------------------------

    def return_unit(self, iid: int, body: dict) -> dict:
        """POST incident/<id>/return {unit_id, status, operator}: a cleared unit goes back to it."""
        try:
            uid = int(body["unit_id"])
        except (KeyError, TypeError, ValueError):
            raise CadError("unit_id: required") from None
        op = _text(body, "operator", OPERATOR_MAX)
        self._by_hand(iid, [uid], op)
        return self.store.return_to_incident(iid, uid, _text(body, "status", 40, required=True), op)

    def unit_status(self, uid: int, body: dict) -> dict:
        status = _text(body, "status", 40, required=True)
        op = _text(body, "operator", OPERATOR_MAX)
        was = self.store.unit(uid)["incident_id"]
        self._by_hand(was, [uid], op)                    # a person's, before the write (#311 L4)
        if "incident_id" in body and body["incident_id"] is not None:   # the board's "which call?" answer
            try:
                iid = int(body["incident_id"])
            except (TypeError, ValueError):
                raise CadError("incident_id: a number (0 = no call)") from None
            self._by_hand(iid, [uid], op)
            out = self.store.set_unit_status_on(uid, status, iid, op, _text(body, "note", 200))
        else:
            out = self.store.set_unit_status(uid, status, op, _text(body, "note", 200))
        for i in {was, out.get("incident_id")}:          # a person's: automation leaves the unit alone there (#311)
            self._by_hand(i, [uid], op)
        if was and out.get("incident_id") != was:
            self.auto.recheck_close([was])               # the last unit off a call that waits to close (#311 N1)
        return out

    def unit_station(self, uid: int, body: dict) -> dict:
        """A unit's home firehouse (a name from data/firehouses.json; '' = not set). Stored as a Settings
        units override (sizeup/cad/settings.py, so Settings > Units shows and edits it too) over the data
        file's units[code].station, and audited in settings_audit (section "units", key CODE/station)."""
        u = self.store.unit(uid)
        self.settings.post("units", {"action": "station", "code": u["name"], "station": body.get("station"),
                                     "operator": body.get("operator")})
        return self.store.unit(uid)

    def run_cards(self) -> list[dict]:
        return runcards.cards(self.store)

    def save_run_card(self, body: dict) -> dict:
        k, rename = _text(body, "type", 50, required=True), _text(body, "rename_from", 50)
        old = runcards.card(self.store, rename or k)
        c = runcards.save(self.store, k, body.get("slots"), _text(body, "spoken", 120), rename,
                          remarks=_text(body, "remarks", runcards.MAX_REMARKS))
        self.settings.audit_card(_text(body, "operator", OPERATOR_MAX), old, c)
        return c

    def delete_run_card(self, body: dict) -> dict:
        k = _text(body, "type", 50, required=True)
        old = runcards.card(self.store, k)
        runcards.delete(self.store, k)
        self.settings.audit_card(_text(body, "operator", OPERATOR_MAX), old, None)
        return {"ok": True}

    # -- radios and communications ---------------------------------------------------------------

    def radio_registry(self) -> dict:
        """GET /api/cad/radios: the registry, and how many FG1 transmissions had an ID in the last 24 h (#163)."""
        return dict(self.radios.public(), id_stats=self.comms.id_stats(self.clock() - 24 * 3600))

    def save_radio(self, body: dict) -> dict:
        """Settings > Radios: add / edit one; a radio given a unit takes its unnamed transmissions with it (#163)."""
        try:
            row = self.radios.save(body, _text(body, "operator", OPERATOR_MAX))
        except RadioError as e:
            raise CadError(str(e)) from None
        if row and row["unit"] and "unit" in body:
            self.comms.relink(row["system"], row["radio_id"], row["unit"])
        return row

    def delete_radio(self, body: dict) -> dict:
        try:
            self.radios.delete(_text(body, "system", 8, required=True), _text(body, "radio_id", 12, required=True),
                               _text(body, "operator", OPERATOR_MAX))
        except RadioError as e:
            raise CadError(str(e)) from None
        return {"ok": True}

    def record_traffic(self, t: dict, replace: bool = True) -> list[int]:
        """A transcribed transmission from the monitor: logged, and linked to the events its rig
        (or its tactical channel) was on. Returns the event ids."""
        iids = self.comms.record(t, replace=replace)
        if iids and replace:
            nums = [r["number"] or str(r["id"]) for r in self.store.q(
                f"SELECT id, number FROM incidents WHERE id IN ({','.join('?' * len(iids))})", iids)]
            self.log(f"CAD: {t.get('source')} {t.get('rig') or t.get('unit') or 'transmission'} -> {', '.join(nums)}")
        return iids

    def comms_query(self, q: dict) -> dict:
        """GET /api/cad/comms: {rows, rev, channels, rigs, pcfd, talking, counts, limits}. ?rev=N: {unchanged: true}
        when nothing was logged since N (the CAD's Radio view polls). ?q=: words in the transcript or
        note. Each row says whether it is "main" (FG1 and the like, a talkgroup marked Main, a note):
        the Radio view puts those up front and folds the rest by talkgroup. Each is its own newest N (#241: the
        other talkgroups filled a shared newest 300 and Main showed None): ?main_limit= (MAIN_LIMIT) for Main,
        ?limit= (OTHER_LIMIT) for the rest, the same filters on both; rows: both, newest first; counts and
        limits: {main, other}. ?emergency=1: only transmissions with the emergency button or an alarm
        (comms.with_alarms puts the alarm on every row).

        The Radio page's views (#415), when any of these is asked (the answer is then one page: {rows, rev, older,
        newer, next, cursor, reload, view}; rows still say "main"):
          ?view=ours|police|ems|transit|air|all   ours: our fire channels (Main); a service's sources; all
          ?system= ?group= ?service= ?tier= ?dept=  the row's own
          ?source=<key>   only these sources (repeat it, or commas between keys: p25:<system>:<tg>, fm:<label>, air:<label>)
          ?safety=1       only scene-safety rows (suggested or confirmed)
          ?mute=<key>,..  sources left out (the viewer's muted ones)
          ?before=<start>,<id>  the page older than that row (a page's `next`)
          ?changed=<cursor>     only what changed since a page's `cursor` (new rows, a transcript, links, an alarm seen
                                or cleared); gone: ids deleted or no longer matching; reload: ask afresh
          ?at=<unix>      a page around that time
          ?limit=         rows a page (PAGE; at most PAGE_MAX)
        Another P25 system's traffic (context) is in an answer only when the reader may see it (#419's one gate,
        sourcekeys.may_see_context: a signed-in person). Otherwise those rows are not here at all (not their id, time,
        talker or radio ID), nor their channels and units in the choices; they are left out before the words are searched.
        Without any of the views' parameters the answer is as it always was, but for each row's `service`, now filled
        on the airband ("air") and the analog channels ("fire")."""
        def one(k: str) -> str:
            v = (q.get(k) or [""])[0].strip()
            if len(v) > 60:
                raise CadError(f"{k}: at most 60 characters")
            return v

        def when(k: str) -> float | None:
            v = one(k)
            if not v:
                return None
            try:
                return float(v)
            except ValueError:
                raise CadError(f"{k}: unix seconds") from None
        rev = one("rev")
        talking = self.live()
        now_rev = f"{self.comms.rev}.{self.store.rev}"
        if rev and rev == now_rev:
            return {"unchanged": True, "rev": rev, "talking": talking}
        # The same filters at the same revs (every screen asks when comms_rev moves; a filter tried again): the answer
        # made a moment ago, for COMMS_CACHE_S (#284). Only "talking" is live.
        pcfd = [u["name"] for u in self.radios.units if is_pcfd(u)]
        main_tgs = [tg for tg in self.comms.talkgroups() if self.main_tg(tg)]
        # (+ whether this reader may be shown another P25 system's rows, #419: an answer is never another reader's)
        context = self.comms._sees_context()
        key = (now_rev, context, tuple(pcfd), tuple(main_tgs),
               tuple(sorted((k, tuple(v)) for k, v in q.items() if k != "rev")))
        hit = self._comms_cache.get(key)
        if hit is not None and time.monotonic() - hit[0] < COMMS_CACHE_S:
            return {**hit[1], "talking": talking}
        if any((q.get(k) or [""])[0].strip() for k in COMMS_PAGE_PARAMS):
            out = self._comms_page(q, one, when, pcfd, main_tgs, now_rev, context)
            return {**self._comms_keep(key, now_rev, out), "talking": talking}
        try:
            limit = max(1, min(int(one("limit") or OTHER_LIMIT), MAX_ROWS))
            main_limit = max(1, min(int(one("main_limit") or MAIN_LIMIT), MAX_ROWS))
        except ValueError:
            raise CadError("limit, main_limit: numbers") from None
        main, other = self.comms.split(main_tgs, self.s.airband,
                                       unit=one("unit"), channel=one("channel"), event=one("event"),
                                       pcfd=pcfd if one("pcfd") in ("1", "true", "on") else None,
                                       since=when("since"), until=when("until"), main_limit=main_limit, limit=limit,
                                       kind={"radio": "radio", "notes": "notes"}.get(one("type"), ""), text=one("q"),
                                       emergency=one("emergency") in ("1", "true", "on"))
        for r in main:
            r["main"] = True
        for r in other:
            r["main"] = False
        rows = sorted(main + other, key=lambda r: -r["start"])
        self._comms_rows(rows)
        out = {"rows": rows, "rev": now_rev, "pcfd": pcfd, **self._comms_facets(context),
               "counts": {"main": len(main), "other": len(other)}, "limits": {"main": main_limit, "other": limit},
               "events": [{"id": r["id"], "number": r["number"], "type": r["type"], "address": r["address"]}
                          for r in self.store.q("SELECT DISTINCT i.id, i.number, i.type, i.address FROM radio_links l "
                                                "JOIN incidents i ON i.id = l.incident_id ORDER BY i.id DESC LIMIT 100")]}
        return {**self._comms_keep(key, now_rev, out), "talking": talking}

    def _comms_keep(self, key, now_rev: str, out: dict) -> dict:
        kept = {k: v for k, v in self._comms_cache.items() if k[0] == now_rev}     # an older rev's: never asked again
        if len(kept) >= COMMS_CACHE_N:
            kept = {}
        kept[key] = (time.monotonic(), out)
        self._comms_cache = kept
        return out

    def _comms_rows(self, rows: list[dict]) -> None:
        """Each row as it is sent: whether its audio is here, without the columns nobody reads."""
        for r in rows:
            r["has_audio"] = r["kind"] == "radio" and self.has_clip(r["id"])
            for k in COMMS_UNSENT:
                r.pop(k, None)

    def _comms_facets(self, context: bool) -> dict:
        """The Radio view's choices: {channels, rigs}. A viewer who may not see another system's traffic gets none of its
        channels, and only units of our own registry."""
        f = self.comms.facets()
        if context:
            return f
        ours = {u["name"] for u in self.radios.units}
        return {"channels": self.comms.source_names(False) + [f["channels"][-1]], "rigs": [r for r in f["rigs"] if r in ours]}

    def _comms_page(self, q: dict, one, when, pcfd, main_tgs, now_rev: str, context: bool) -> dict:
        """comms_query's answer for the Radio page's views (#415): one page (Comms.page)."""
        def keys(k: str, most: int) -> list[str]:
            out = [x.strip() for v in q.get(k) or [] for x in v.split(",") if x.strip()]
            if len(out) > most or any(len(x) > 80 for x in out):
                raise CadError(f"{k}: at most {most} source keys")
            return out
        view = one("view").lower()
        if view and view not in VIEWS:
            raise CadError(f"view: one of {', '.join(VIEWS)}")
        service = one("service").lower()
        if service and service not in sourcekeys.SERVICES:
            raise CadError(f"service: one of {', '.join(sourcekeys.SERVICES)}")
        before = None
        token = (q.get("before") or [""])[0].strip()       # a page's `next`: "<start>,<id>", a P25 clip id is long
        if len(token) > 240:
            raise CadError("before: a page's next")
        if token:
            start, _, tid = token.partition(",")
            try:
                before = (float(start), tid)
            except ValueError:
                raise CadError("before: <start>,<id> (a page's next)") from None
        try:
            limit = max(1, min(int(one("limit") or PAGE), PAGE_MAX))
        except ValueError:
            raise CadError("limit: a number") from None
        changed, at = when("changed"), when("at")
        got = self.comms.page(
            main_tgs, self.s.airband, view=view, system=one("system"), group=one("group"), source=keys("source", SOURCES_MAX),
            service=service, tier=one("tier").lower(), dept=one("dept"), safety=one("safety") in ("1", "true", "on"),
            mute=keys("mute", MUTE_MAX), before=before, changed=None if changed is None else int(changed), at=at, limit=limit,
            unit=one("unit"), channel=one("channel"), event=one("event"),
            pcfd=pcfd if one("pcfd") in ("1", "true", "on") else None, since=when("since"), until=when("until"),
            kind={"radio": "radio", "notes": "notes"}.get(one("type"), ""), text=one("q"),
            emergency=one("emergency") in ("1", "true", "on"), context=context)
        self._comms_rows(got["rows"])
        out = {**got, "rev": now_rev, "view": view or "all", "pcfd": pcfd}
        if changed is None and before is None:           # a first page: the choices for the filters too
            out.update(self._comms_facets(context))
            out["events"] = [{"id": r["id"], "number": r["number"], "type": r["type"], "address": r["address"]}
                             for r in self.store.q("SELECT DISTINCT i.id, i.number, i.type, i.address FROM radio_links l "
                                                   "JOIN incidents i ON i.id = l.incident_id ORDER BY i.id DESC LIMIT 100")]
        return out

    def comms_sources(self, q: dict | None = None) -> dict:
        """GET /api/cad/comms/sources?rev=: the Radio page's tree (#415), {rev, now, minutes, live: [keys], sources:
        [{key, system, system_label, group, name, service, tier, ours, encrypted, department, talkgroup, last, counts,
        emergency, stuck}]}. ?rev= a tree's rev: {unchanged: true, rev, live} while nothing was logged and the minute is
        the same (the page asks every few seconds).

        A source: every one in the log or the activity table, and every known talkgroup that is ours, was heard, or is
        another system's with a tier that isn't off. last: when it was last heard (unix). live: the sources transmitting
        now. counts: [[minutes ago, calls], ...] over the last hour, only the minutes with calls (the viewer's unread
        counts and the time bar come from these: no per-viewer state here). emergency: "ours" | "other": an emergency
        on it nobody has acknowledged (Comms.emergencies). In the tree's order: system, group, name. Nothing of what
        was said. A reader who may not see another system's traffic (sourcekeys.may_see_context) gets none of its
        sources."""
        context = self.comms._sees_context()
        now = self.clock()
        minute = int(now // 60)
        rev = f"{self.comms.rev}.{self.store.rev}.{minute}.{int(bool(context))}"
        live = set()
        for e in self.live():
            if e.get("ended"):
                continue
            if e.get("source"):
                live.add(str(e["source"]))
            elif e.get("tg") is not None:
                live.add(sourcekeys.p25(str(e.get("p25_system") or sourcekeys.COUNTY), int(e["tg"])))
            elif e.get("channel"):
                live.update((f"fm:{e['channel']}", f"air:{e['channel']}"))
        hit = self._sources_cache.get(bool(context))
        if hit is None or hit[0] != rev:
            county = sourcekeys.COUNTY
            labels = {"analog": "Analog", "airband": "Airband", **{str(k): str(v) for k, v in self.source_systems().items()}}
            info = {str(r.get("source")): r for r in self.source_rows() if r.get("source")}
            seen = {r["source"]: r for r in self.comms.sources_seen()}
            counts = self.comms.minute_counts(now)
            ems = self.comms.emergencies(now)
            out = []
            for key in dict.fromkeys([*seen, *info]):
                i, s = info.get(key, {}), seen.get(key, {})
                got = sourcekeys.parse(key)
                kind = "airband" if key.startswith("air:") else "" if got else "traffic"
                system = str(i.get("system") or s.get("system") or (got[0] if got else "airband" if kind == "airband" else "analog"))
                if not context and sourcekeys.is_context({"system": system}):
                    continue
                name = str(i.get("name") or s.get("channel") or key.split(":", 1)[-1])
                if got:
                    ours = system == county and bool(self.main_tg(got[1]))
                else:
                    ours = kind != "airband" and name not in self.s.airband
                tier = str(i.get("tier") or s.get("tier") or "")
                last = max(float(s.get("last") or 0.0), float(i.get("last") or 0.0)) or None
                if key not in seen and not ours and not last and (system == county or tier in ("", tiers.IGNORE)):
                    continue                             # a talkgroup only listed: never heard, not ours, not chosen
                group = str(i.get("group") or s.get("group") or ("Airband" if kind == "airband" else "Analog" if not got else ""))
                em = ems.get(key) or {}
                out.append({"key": key, "system": system, "system_label": labels.get(system, system), "group": group,
                            "name": name,
                            "service": str(i.get("service") or s.get("service") or sourcekeys.service(group, kind=kind)),
                            "tier": tier, "ours": ours, "encrypted": bool(i.get("encrypted")),
                            "department": str(i.get("department") or ""), "talkgroup": got[1] if got else None,
                            "last": last, "counts": counts.get(key) or [], "emergency": em.get("emergency", ""),
                            "stuck": em.get("stuck", "")})
            order = {"analog": 1, "airband": 3}
            out.sort(key=lambda r: (0 if r["system"] == county else order.get(r["system"], 2), r["system"],
                                    not r["ours"], r["group"], r["name"]))
            hit = self._sources_cache[bool(context)] = (rev, out)
        shown = {r["key"] for r in hit[1]}
        live_keys = sorted(live & shown)
        if q and (q.get("rev") or [""])[0] == rev:
            return {"unchanged": True, "rev": rev, "live": live_keys}
        return {"rev": rev, "now": now, "minutes": COUNT_MINUTES, "live": live_keys, "sources": hit[1]}

    def feed_query(self, q: dict) -> dict:
        """GET /api/cad/feed?limit=&tg=307,333: the 60 Control feed (default: the data file's talkgroups)."""
        try:
            limit = int((q.get("limit") or ["0"])[0] or 0) or None
            tgs = tuple(int(x) for x in (q.get("tg") or [""])[0].split(",") if x.strip()) or None
        except ValueError:
            raise CadError("limit, tg: numbers (tg: comma-separated talkgroups)") from None
        return dict(self.feed.state(), rows=self.feed.rows(limit, tgs))

    def link_traffic(self, iid: int, body: dict) -> dict:
        """POST /api/cad/incident/<id>/link {traffic_id, operator}: "Add to event" in a transmission's menu."""
        inc = self.store.incident(iid)
        if inc["status"] == "dismissed":
            raise CadError("event is dismissed")
        r = self.comms.link(_text(body, "traffic_id", 200, required=True), iid, _text(body, "operator", OPERATOR_MAX))
        if r.get("error"):
            raise CadError(r["error"])
        return dict(r, incident_id=iid, number=inc["number"])

    def keep_traffic(self, iid: int, body: dict) -> dict:
        """POST /api/cad/incident/<id>/keep {traffic_id, undo?, operator}: an automatic mention / fireground link confirmed
        by a person (#311): its audio and row are kept like any on the event; undo: not kept any more."""
        inc = self.store.incident(iid)
        undo = _bool(body, "undo") or False
        r = self.radiolog.keep(_text(body, "traffic_id", 200, required=True), iid, _text(body, "operator", OPERATOR_MAX), undo)
        if r.get("error"):
            raise CadError(r["error"])
        return dict(r, incident_id=iid, number=inc["number"])

    def unlink_traffic(self, iid: int, body: dict) -> dict:
        """POST /api/cad/incident/<id>/unlink {traffic_id, operator}: "Remove from 26-000007" in a
        transmission's menu. An auto link removed stays removed (comms.py radio_link_exclusions)."""
        inc = self.store.incident(iid)
        r = self.comms.unlink(_text(body, "traffic_id", 200, required=True), iid, _text(body, "operator", OPERATOR_MAX))
        if r.get("error"):
            raise CadError(r["error"])
        return dict(r, incident_id=iid, number=inc["number"])

    def rigs(self) -> dict:
        """The board units for the alert settings (PCFD's own flagged)."""
        return {"units": [{"name": u["name"], "kind": u["kind"], "pcfd": is_pcfd(u), "department": u["department"]}
                          for u in self.store.units() if u["board"]]}
    # -- settings (sizeup/cad/settings.py) ---------------------------------------------------------

    def settings_get(self) -> dict:
        return self.settings.page()

    def settings_post(self, section: str, body: dict, guard: bool = False) -> dict:
        return self.settings.post(section, body, guard)

    def settings_export(self) -> dict:
        return self.settings.export()

    def settings_audit_log(self, q: dict) -> dict:
        return {"audit": self.settings.audit_log((q.get("section") or [""])[0][:40])}

    # -- location suggestions -----------------------------------------------------------------------

    def _ranked(self, q: str) -> list[dict]:
        """The gazetteer's readings, ours first: a town typed in the box wins; then, among the
        readings about as good as the best one, the towns in geo_town_order (Port Chester, Rye
        Brook, the towns around); then the rest. Each says its town."""
        L = self.lists
        ms = [dict(m) for m in (self.geocoder(q) or [])] if self.geocoder else []
        if not ms:
            return ms
        typed = L.towns_typed(q)
        best = max(m["confidence"] for m in ms)
        for m in ms:
            m["town"] = L.town_of(m.get("municipality") or "")
            if m["town"] and not m["label"].lower().endswith(m["town"].lower()):
                m["label"] = f"{m['label']}, {m['town']}"
        return sorted(ms, key=lambda m: (m["town"] not in typed, m["confidence"] < best - GEO_WINDOW,
                                         L.town_rank(m["town"]), -m["confidence"]))

    def geocode(self, q: str) -> dict:
        """The location box. The gazetteer's readings, plus the place search's highway mile markers and
        exits (first: the gazetteer has none) and named places it doesn't know (#67)."""
        q = (q or "").strip()
        if not q or len(q) > 200:
            raise CadError("q: 1 to 200 characters")
        if self.geocoder is None:
            return {"matches": [], "error": "geocoder unavailable"}
        cross = self._crossings(q)
        if cross is not None:
            return cross
        ms = [m for m in self._ranked(q) if m["confidence"] >= 0.2]
        hits = self._place_hits(q, limit=8) if len(q) <= 120 else []
        highway = [self._as_match(h) for h in hits if h["kind"] in ("milemarker", "exit")]
        known = {(m["label"].split(",")[0].strip().lower()) for m in ms}
        named = [self._as_match(h) for h in hits if h["kind"] == "place" and h["label"].lower() not in known]
        rest = sorted(ms + named, key=lambda m: -m["confidence"]) if named else ms
        return {"matches": [self._detailed(m) for m in (highway + rest)[:6]]}

    def _crossings(self, q: str) -> dict | None:
        """Two streets typed ("N Main St / Westchester Ave", "n main & west": #213): where they meet in our area,
        ours first, at most LIMIT with how many more ({matches, total, more}); a side under two letters: the error
        (what to type). None: not two streets, or nowhere they meet (the box's usual suggestions then)."""
        ix = self.crossings()
        if ix is None:
            return None
        try:
            found = ix.search(q, is_place=self._names_place)
        except Exception as e:                   # the usual suggestions, as before
            self.log(f"CAD: intersection search failed ({type(e).__name__}: {e})")
            return None
        if found is None:
            return None
        if found.get("error"):
            return {"matches": [], "error": found["error"]}
        if not found["results"]:
            return None
        L = self.lists
        ms = [self._detailed(dict(r, town=L.town_of(r["municipality"]))) for r in found["results"]]
        return {"matches": ms, "total": found["total"], "more": found["more"]}

    def crossings(self):
        """The IntersectionSearch (built on first use: cross_source), or None without a gazetteer."""
        if self._cross_ix is None and self.cross_source is not None:
            with self._cross_lock:
                if self._cross_ix is None:
                    try:
                        self._cross_ix = self.cross_source()
                    except Exception as e:           # no intersection search, everything else as before
                        self.log(f"CAD: intersection search unavailable ({type(e).__name__}: {e})")
                        self.cross_source = None
        return self._cross_ix

    def _names_place(self, text: str) -> bool:
        """What follows an "@" names a place (a named place's whole name, or one of the department's own)."""
        ix = self.place_index()
        return bool(ix is not None and getattr(ix, "names_place", None) and ix.names_place(text, self.local_places()))

    def _detailed(self, m: dict) -> dict:
        """A suggestion with what the new call's form fills from it (#204): place (a named place's name),
        street_address (an address's, or the place's from the places data; '' when there is none), address_checked
        (found in the address points), cross (the streets at each end of its block) and block (the map's)."""
        m = dict(m)
        kind = m.get("kind") or ""
        base = {"place": m.get("place") or "", "street_address": "", "address_checked": False, "cross": "", "block": None}
        if kind == "address":
            base["street_address"] = f"{m.get('house_number') or ''} {m.get('street') or ''}".strip()
        elif kind in ("landmark", "place"):
            base["street_address"] = (m.get("place_address") or "").strip()
        elif kind == "intersection" and m.get("streets"):    # its two streets are the cross streets (#213)
            base["cross"] = " and ".join(s for s in m["streets"] if s)
        if self.detail_source is not None and kind in ("address", "landmark", "place"):
            key = (kind, m.get("label"), m.get("lat"), m.get("lon"), m.get("place_address"), m.get("street"))
            got = self._details.get(key)
            if got is None:
                try:
                    got = self.detail_source(m) or {}
                except Exception as e:              # the suggestion as it was, without them
                    self.log(f"CAD: location detail failed for {m.get('label')!r} ({type(e).__name__}: {e})")
                    got = {}
                if len(self._details) > 500:
                    self._details.clear()
                self._details[key] = got
            base.update({k: v for k, v in got.items() if k in base and (v or k in ("cross", "block"))})
        m.update(base)
        return m

    def here(self, query: dict) -> dict:
        """GET /api/cad/geocode/here?lat=&lon=: what is at a point of the new call's map (#204): the nearest address
        point, intersection and named place, each a location suggestion (with its cross streets), and the street under
        it (offered as a cross street). Read only."""
        def num(k: str, lo: float, hi: float) -> float:
            try:
                v = float((query.get(k) or [""])[0])
            except ValueError:
                raise CadError(f"{k}: a number") from None
            if not lo <= v <= hi:
                raise CadError(f"{k}: in the area")
            return v
        lat, lon = num("lat", 40.0, 42.0), num("lon", -75.0, -72.0)
        if self.here_source is None:
            return {"address": None, "intersection": None, "place": None, "street": None, "error": "geocoder unavailable"}
        ix = self.place_index()
        places = [{"name": p["name"], "address": p.get("address") or "", "muni": p.get("muni") or "", "lat": p["lat"],
                   "lon": p["lon"]} for p in (getattr(ix, "places", None) or [])]
        places += [{"name": r.get("label") or r["name"], "address": "", "muni": r.get("muni") or "", "lat": r["lat"],
                    "lon": r["lon"]} for r in self.local_places() if r.get("lat") is not None and r.get("lon") is not None]
        got = self.here_source(lat, lon, places)
        L = self.lists
        for k in ("address", "intersection", "place"):
            m = got.get(k)
            if m:
                m["town"] = L.town_of(m.get("municipality") or "")
                got[k] = self._detailed(m)
        return got

    # -- the place search: a note's "@", the location box (#67) -------------------------------------

    def place_index(self):
        """The PlaceIndex (built on first use: place_source), or None without a gazetteer."""
        if self._place_ix is None and self.place_source is not None:
            with self._place_lock:
                if self._place_ix is None:
                    try:
                        self._place_ix = self.place_source()
                    except Exception as e:           # no place suggestions, everything else as before
                        self.log(f"CAD: place search unavailable ({type(e).__name__}: {e})")
                        self.place_source = None
        return self._place_ix

    def local_places(self) -> list[dict]:
        """The department's own names for places (Settings > Places), for the place search."""
        return self.settings.local_places()

    def _place_hits(self, q: str, near=None, limit: int = 8) -> list[dict]:
        ix = self.place_index()
        if ix is None:
            return []
        L = self.lists
        hits = ix.search(q, near=near, limit=limit, extra=self.local_places(), priority=tuple(L.geo_town_order))
        for h in hits:
            h["sub"] = L.town_of(h["sub"])
        return hits

    def _as_match(self, h: dict) -> dict:
        """A place-search hit as a location box suggestion (the gazetteer's shape)."""
        conf = round(min(0.99, max(0.2, h.get("quality", 0.6))), 2)
        kind = {"milemarker": "mile marker"}.get(h["kind"], h["kind"])       # as the box shows it
        out = {"label": h["label"], "lat": h["lat"], "lon": h["lon"], "kind": kind, "municipality": h["sub"],
               "town": h["sub"], "precision": h.get("category") or h["kind"], "confidence": conf}
        if h["kind"] == "place":         # a named place: its name, and its street address from the data (#204)
            out["place"] = h["label"]
            out["place_address"] = "" if h.get("source") == "local" else (h.get("detail") or "")
        return out

    def search_places(self, query: dict) -> dict:
        """GET /api/cad/places?q=&near=lat,lon&iid=: ranked places of every kind (addresses, intersections,
        streets, named places, mile markers, exits); near (else the event's own point) breaks ties."""
        def one(k: str) -> str:
            return (query.get(k) or [""])[0].strip()
        q = one("q")
        if not q or len(q) > 120:
            raise CadError("q: 1 to 120 characters")
        near = None
        if one("near"):
            try:
                lat, lon = (float(x) for x in one("near").split(","))
            except ValueError:
                raise CadError("near: lat,lon") from None
            if not (40.0 <= lat <= 42.0 and -75.0 <= lon <= -72.0):
                raise CadError("near: lat,lon in the area")
            near = (lat, lon)
        elif one("iid").isdigit():
            try:
                inc = self.store.incident(int(one("iid")))
                if inc["lat"] is not None and inc["lon"] is not None:
                    near = (inc["lat"], inc["lon"])
            except CadError:
                pass
        try:
            limit = max(1, min(int(one("limit") or 8), 20))
        except ValueError:
            raise CadError("limit: a number") from None
        if self.place_index() is None:
            return {"places": [], "error": "place search unavailable"}
        return {"places": [{k: h[k] for k in PLACE_FIELDS if k in h} for h in self._place_hits(q, near, limit)]}

    # -- the bridge from the monitor ----------------------------------------------------------------

    def _ours(self, inc) -> bool:
        """A page for us: its tones name one of our departments (inc.tone_departments: PCFD, Rye Brook). Units
        or departments only heard in the text don't count (user, 2026-10-08), and the address never does
        (#80: a Fairview page geocoded weakly to Port Chester was queued; the monitor departments' tones only)."""
        mine = set(self.s.monitor_departments) | self.monitor_names
        return any(base_name(d) in mine for d in getattr(inc, "tone_departments", []))

    def from_monitor(self, inc) -> dict | None:
        """A monitor incident (60 Control page) for one of our departments -> a queued CAD event,
        once per monitor incident. A repeat of the page fills in what the first reading missed
        while it is still queued; nothing changes once a person has accepted it."""
        if not self._ours(inc):
            return None
        ref = f"monitor:{inc.id}"
        geo = inc.geo or {}
        heard = (inc.location or "").title()
        # A location the page's own words don't confirm (#176: no cross street fits, another town, two readings, a
        # sound-alike) is never placed: the card says "location unconfirmed" and offers the readings to pick.
        sure = bool(geo.get("label")) and geo.get("confirmed", True) is not False
        if sure:
            placed = ("address", "intersection", "landmark") + (("street",) if geo.get("confirmed") else ())   # a block
            address = geo.get("label") if geo.get("kind") in placed else heard
        else:
            address = heard or geo.get("label") or ""
        alts = [] if sure else [{k: a.get(k) for k in ("label", "lat", "lon", "town", "confidence", "why")}
                                for a in (geo.get("alts") or []) if a.get("label") and a.get("lat") is not None][:4]
        cross = (inc.cross_street or "").title()
        if sure and geo.get("checked") == "all" and len(geo.get("crosses") or []) >= 2:
            cross = " and ".join(geo["crosses"][:2])          # as the map names them: "Clinton St and W William St"
        lines = []
        for pid_text, start in zip(inc.page_text, inc.page_starts or [inc.opened] * len(inc.page_text)):
            lines.append(f"60 Control page {datetime.fromtimestamp(start, TZ):%H:%M}: {pid_text}")
        facts = [f"type heard: {inc.incident_type}"]
        if inc.code:
            facts.append(f"code {inc.code}")
        if inc.units:
            facts.append(f"units named on the page: {', '.join(inc.units)}")
        if heard:
            if sure:
                checked = {"all": "cross streets " + " and ".join(geo.get("crosses") or []),
                           "partial": f"only {', '.join(geo.get('crosses') or [])} of the cross streets"}.get(geo.get("checked"), "")
                where = f" (map: {geo['label']}, {geo.get('confidence', 0):.2f}" + (f", {checked}" if checked else "") + ")"
            elif geo.get("label"):
                where = (f" (location unconfirmed: {geo.get('why') or 'not sure'}; best reading {geo['label']}, "
                         f"{geo.get('confidence', 0):.2f}: pick on the card)")
            else:
                where = " (not found on the map)"
            facts.append(f"location as heard: {heard}{where}")
        if inc.time_out:
            facts.append(f"county time out {inc.time_out}")
        remarks = "\n".join(lines + ["; ".join(facts)])[:4000]
        typ, sub, note = runcards.from_page(inc.incident_type, inc.category, " ".join(inc.page_text))
        try:
            self.lists.check_type(typ, sub, required=False)
        except ValueError:
            try:                                       # the type fits, the subtype not (the map changed): keep the type
                self.lists.check_type(typ, required=False)
                sub, note = "", note or f"subtype heard as {sub}, not one for {self.lists.type_label(typ)}: pick the subtype"
            except ValueError:                         # a data file without that code: leave it to the operator
                typ, sub, note = "", "", note or f"type heard: {inc.incident_type}"
        f = {"type": typ, "subtype": sub, "type_note": note,
             "address": (address or "")[:200], "cross_street": cross[:200], "remarks": remarks,
             "lat": geo.get("lat") if sure else None, "lon": geo.get("lon") if sure else None,
             "geo_label": (geo.get("label") or "")[:200] if sure else "", "geo_alts": json.dumps(alts) if alts else ""}
        if typ == airport.TYPE:                        # the tower's facts as the page says them (#105)
            f.update(airport.fields(" ".join(inc.page_text)))
        old = self.store.by_source_ref(ref)
        got = self.sources.paging_incident(str(inc.id))
        if got and (old is None or (old["status"] == "dismissed" and got != old["id"])):
            old = self.store.incident(got)             # attached to an incident a higher tier made (#297), or moved there
                                                       # from its own card (#365: rematch_page)
        if old is None:
            # The paging decoder is the backup (#297, sizeup/cad/sources.py): it attaches to the incident a feed has
            # for it, else makes its own at once (never held), noted "possibly the same as" a near one it may not join.
            data = {"f": f, "ref": ref, "pages": list(getattr(inc, "pages", []) or []), "type_heard": inc.incident_type}
            at = (inc.page_starts or [inc.opened])[0]
            how, got = self.sources.page(str(inc.id), at, f["address"] or heard, list(inc.units or []), data)
            if how == "attached":
                self._page_enrich(got, inc, f, lines)         # its scope too (#370)
                self.auto.on_page(got, list(inc.units or []), at)   # 60 Control mode: "paged", its units (#311, #365)
                return self.store.incident(got)
            if (typ, sub) == (airport.TYPE, "ALERT-1"):  # an HPN Alert 1 is a standby pre-alert, never an incident (#369)
                try:
                    self.airport_watch.standby("60 Control page", str(inc.id), " ".join(inc.page_text)[:400], at)
                except Exception as e:  # noqa: BLE001 - the monitor goes on
                    self.log(f"CAD: Alert 1 page: {type(e).__name__}: {e}")
                return None
            iid = self._create_from_page(data)
            self.sources.created_by_page(str(inc.id), iid, got)
            self._page_scope(iid, inc)
            return self.store.incident(iid)
        self.comms.link_pages(old["id"], getattr(inc, "pages", []))  # a repeat of the page: on it too, accepted or not
        at = (inc.page_starts or [inc.opened])[0]
        if old["source_ref"] == ref and old["status"] == "queued" and not self.sources._has_feed(old["id"]):
            # its own card, made from less than it says now (the tones half, a first reading): matched again (#365)
            data = {"f": f, "ref": ref, "pages": list(getattr(inc, "pages", []) or []), "type_heard": inc.incident_type}
            how, got = self.sources.rematch_page(str(inc.id), old["id"], at, f["address"] or heard,
                                                 list(inc.units or []), data)
            if how == "attached":
                self._page_enrich(got, inc, f, lines)     # its scope too (#370)
                self.auto.on_page(got, list(inc.units or []), at)
                return self.store.incident(got)
        if self.sources._has_feed(old["id"]):          # a feed's incident: the repeat enriches it (#365), its scope (#370)
            self._page_enrich(old["id"], inc, f, lines)
            self.auto.on_page(old["id"], list(inc.units or []), at)
        else:
            self._page_scope(old["id"], inc)
        if old["status"] != "queued":
            return old
        auth = self.sources.authority(old["id"])
        if auth is not None and auth < PAGING_RANK:    # a feed's fields rule it: the page's repeat changes nothing
            return old
        fill = {k: v for k, v in f.items() if k == "remarks" or
                (k not in ("type", "subtype", "type_note", "geo_alts") and v not in (None, "") and old[k] in (None, ""))}
        if old["geo_alts"] and sure:                   # the repeat confirmed what the first reading couldn't (#176)
            fill.update(address=f["address"], cross_street=f["cross_street"], lat=f["lat"], lon=f["lon"],
                        geo_label=f["geo_label"], geo_alts="")
        elif old["geo_alts"] and alts:                 # still unconfirmed: the readings as they stand now
            fill["geo_alts"] = f["geo_alts"]
        if not old["type"] and typ:                    # the repeat said what it is
            fill.update(type=typ, subtype=sub, type_note=note)
        return self.store.update_incident(old["id"], fill, log=False)

    def _page_scope(self, iid: int, inc, note: bool = True) -> tuple[str, bool]:
        """Full department or specific units, as the page says it (#370: the page's stands over a feed's): (the
        feed's label when it says otherwise ("units"), else ""; whether the page's reading is new there). note: that
        disagreement is a timeline line of its own."""
        try:
            return self.scope.on_page(iid, inc, note=note)
        except Exception as e:  # noqa: BLE001 - the page stands whatever this does
            self.log(f"CAD: dispatch scope: {type(e).__name__}")
            return "", True

    def _page_enrich(self, iid: int, inc, f: dict, lines: list[str]) -> None:
        """A page on an incident a feed has (#365): never a card of its own, it enriches that one. Its clip is on it
        (Sources._page_attached, comms.link_pages); on the timeline (kind "tones"), once each: its transcript lines
        ("60 Control page 14:31: ...") and one line for who it paged (#370, scope.page_line: "Page: all PCFD", "Page:
        PCFD · units Engine 60, Ladder 31", with " · feed: units" when the feed's label says otherwise). In 60
        Control mode AutoDispatch.on_page dispatches the units it named that are on our boards; in PCFD mode they stay
        that line for a person. Its cross streets fill the incident's when it has none and no person set its
        location. The feed's type and location stand: the page's are only noted (Sources._page_attached)."""
        cur = self.store.incident(iid)
        have = {e["text"] for e in self.store.q("SELECT text FROM events WHERE incident_id = ? AND kind = 'tones'", (iid,))}
        depts = list(dict.fromkeys([*(getattr(inc, "tone_departments", None) or []), *(getattr(inc, "departments", None) or [])]))
        feed, _ = self._page_scope(iid, inc, note=False)
        texts = [x[:2000] for x in lines]
        base = self.scope.line(iid) or scope_mod.page_line(depts, scope_mod.latest_units(inc),
                                                           getattr(inc, "tone_departments", ()), inc.incident_type)
        if base and not any(x == base or x.startswith(base + " · feed: ") for x in have):   # once per reading
            texts.append(base + (f" · feed: {feed}" if feed else ""))   # (feed: "" when that disagreement is said)
        new = [x for x in dict.fromkeys(texts) if x not in have]
        if new:
            with self.store.tx() as db:
                for x in new:
                    self.store._event(db, iid, "tones", x, "Paging", at=self.clock())
        cross = (f.get("cross_street") or "").strip()
        if (cross and not (cur["cross_street"] or "").strip() and cur["status"] in ("queued", "pending", "active")
                and "location" not in self.sources.manual(iid)):          # a person's location: theirs, cross too
            try:
                self.store.update_incident(iid, {"cross_street": cross}, operator="Paging", log=False)
            except CadError:
                pass

    def _create_from_page(self, data: dict) -> int:
        """A page's queued event (from_monitor's fields)."""
        f, ref = data["f"], data["ref"]
        old = self.store.by_source_ref(ref)
        if old is not None:
            return old["id"]
        via = "60 CONTROL" if "60 CONTROL" in self.lists.received_codes else ""
        iid = self.store.create_incident(**f, received_via=via, source="60 Control page", source_ref=ref, status="queued")
        self.log(f"CAD: from 60 Control: {self.lists.type_label(f['type'], f['subtype']) or 'type not set'} at "
                 f"{f['address'] or '(no location)'}")
        self.comms.link_pages(iid, data.get("pages") or [])   # its page plays from the event (#64)
        self._prealert_match(self.store.incident(iid))
        return iid

    def map_type(self, raw: str, label: str = "") -> tuple[str, str, str]:
        """An external feed's event type -> (TYPE, SUBTYPE, note): one of our codes as it is ("STRU", "ALARM/COMM"),
        else read like a page's type (runcards.from_page); one nobody maps leaves the type to the operator."""
        raw = (raw or "").strip()
        if not raw:
            return "", "", ""
        typ, _, sub = re.sub(r"\s*,\s*", "/", raw.upper(), count=1).partition("/")   # "TYPE,SUBTYPE" too (#365)
        try:
            self.lists.check_type(typ.strip(), sub.strip(), required=True)
            return typ.strip(), sub.strip(), ""
        except ValueError:
            pass
        from sizeup.dispatch import parse
        d = parse(f"respond to {raw}")
        typ, sub, note = runcards.from_page(d.incident_type, d.category, re.sub(r"[/,]", " ", raw))
        for t, s in ((typ, sub), (typ, "")):
            try:
                self.lists.check_type(t, s, required=False)
                if t:
                    return t, s, note if s == sub else f"subtype not given by {label or 'the feed'}: {raw}"
            except ValueError:
                continue
        return "", "", f"type from {label or 'the feed'}: {raw}"[:200]
