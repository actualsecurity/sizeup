"""[cloud] role = "station": the station (the machine with the SDRs) runs everything as all-in-one and also feeds the hub.

    App._emit(kind, data, clip)  -> outbox.sqlite (outbox.py), in order, durable
        page        a page off the air (paging channel), with its clip      (App._segment)
        page_text   its transcript (the hub parses the dispatch itself)     (App._transcribed)
        traffic     a finished transmission, with its clip: transcribed, or (a hub with traffic_pending, #927) at its
                    close, "Transcribing", its text following as a retext with fill   (App._transcribed, _land_pending)
        retext      a re-run's new text for a page or transmission          (App._retext); a fill (App._fill_pending)
        rerun       a re-run is over: changed | same | failed: why          (App._rerun_finish)
        alt_text    "both" re-run: whisper's text as the alternative        (App._set_alt)
        heard       a radio ID (and P25 alias) heard on the air             (App._heard)
    forwarder thread   POST /api/station/events: batches in order, retried with backoff; the hub
                       answers with the last seq it has, which is then deleted here. A batch the hub refuses
                       for good (400 / 413 / 422) is split until the event it refuses is alone, which is
                       quarantined (outbox.py; a critical one sent again slowly, and a critical alert on the
                       hub), so the events behind it still go (_forward_once, _rejected)
    state thread       POST /api/station/state: who is talking now and a page going out now (paging:
                       each tone pair as it decodes), pushed the moment it changes
                       (sub-second; latest wins, never queued) and every 5 s the radio / SDR / STT /
                       voice status, re-run progress, the outbox size and this machine's health
                       (sizeup/cloud/health.py: CPU, memory, disk, uptime, each radio program; also the heartbeat)
                       The hub answers with its build: Watch (autoupdate.py) passes it to the
                       station's auto-updater once it holds still; this station's version and the
                       updater's last result go up in the status ("version"). And with the id it took
                       the post as ("station"): the hub is the source of truth for this station's id,
                       which it adopts (adopt(): its events, status, logs and metrics say it from then
                       on), so an admin's rename on the hub (Settings > Station > Stations) never locks
                       it out. Its config's [station] id goes up too ("config_id")
                       Beside `at` on every post: this boot's id and the wall clock's steps over 5 s
                       ("boot_id", "wall_steps": sizeup/cloud/wallclock.py; the count kept across a restart in
                       wall_clock.json beside the outbox), for the hub to judge its clock skew
                       ... and the admins' service per source ("services": sizeup/sourcekeys.py), kept beside the
                       outbox (hub_services.json) so a station starting while the hub is away tags as it did
    aircraft thread    POST /api/station/aircraft: our ADS-B receiver's aircraft (#235, aircraftlink.py), read every
                       second, what changed, every 1-5 s as the hub asks; dropped (never queued) while it's offline
    logs thread        POST /api/station/logs: this station's journal (sizeup-logread hands it over: logread.py),
                       scrubbed and gzipped, every ~45 s, spooled on disk while the hub is away (logship.py); the hub
                       writes it to CloudWatch (cwlogs.py)
    metrics threads    POST /api/station/metrics: the signal metrics (sizeup/cloud/metrics.py): each channel's noise floor
                       and squelch a minute, each transmission's levels, each P25 control channel's decode rate, what
                       changed; batched every minute, in memory while the hub is away (bounded: the oldest dropped,
                       counted), never through the outbox
    vessels thread     POST /api/station/vessels: our AIS receiver's vessels (#402, vessellink.py), only with
                       [vessels.receiver] enabled; the whole list every 5-30 s as the hub asks; never queued
    jobs thread        GET /api/station/jobs?wait=25 (long poll: the station never takes inbound
                       connections): "tts" renders a dispatch with the local voice; "call" runs
                       set_mode / set_talkgroup / set_talkgroup_group / retranscribe / transcribe_now /
                       catch_up (shed clips caught up, sizeup/sttcatchup.py) /
                       reception_post / reception_sample_b64 (a reception test, #410) / set_channel_prefs
                       (Settings > Radios > Channels, as the hub has them) / emergency_all_clear (an
                       alarm's All clear: the emergency window here, App._call's keep decision) here

Clips go as 8 kHz WAV: they are already band-limited to 300-3300 Hz (stt.radio_voice), so
halving the rate halves the upload and changes nothing you can hear.

    stt-input threads  each transmission's / page's speech-to-text input (the exact audio the station's whisper
                       transcribes, FLAC at 16 kHz, sizeup/cloud/sttinput.py): made by one low-priority thread
                       ("hub-stt-input") off the radio path, kept in the outbox's own lane, and sent by "hub-stt"
                       to POST /api/station/stt, gzipped, only while no event waits and only to a hub that takes them;
                       the pre-alerts first (pages, alert-tier talkgroups), then fire traffic (#677)
"""

from __future__ import annotations

import base64
import inspect
import io
import json
import tempfile
import threading
import time
import wave
from pathlib import Path
from typing import Callable

import numpy as np

from sizeup import sourcekeys, tz
from sizeup.cad import dispatcher
from sizeup.cad.alarms import MAX_AGE_S as ALARM_MAX_AGE_S
from sizeup.cad.dispatcher import AudioUnavailable
from sizeup.cloud.aircraftlink import AircraftForwarder
from sizeup.cloud.adapters import request_reset
from sizeup.cloud.autoupdate import Watch
from sizeup.cloud.client import REJECTED, Backoff, HubClient, HubError
from sizeup.cloud.health import Metrics, station_metrics
from sizeup.cloud.logship import LogShipper
from sizeup.cloud.metrics import MetricsLink
from sizeup.cloud import sttinput
from sizeup.cloud.outbox import NOTES_KEPT, Outbox, open_outbox
from sizeup.cloud.vessellink import VesselForwarder
from sizeup.cloud.wallclock import FILE as WALL_FILE, WallSteps
from sizeup.tts import VoiceCfg

LIVE_REFRESH_S = 2.0     # while someone is talking the snapshot is re-sent this often (the hub drops one older than 8 s)
STATUS_S = 5.0           # radio / SDR / STT / voice status (and the heartbeat)
TALKGROUPS_S = 30.0      # the talkgroup list (big, slow to change): this often, or when a switch changes
JOB_WAIT_S = 25          # long poll; Cloudflare allows 100 s
CONNECTED_S = 15.0       # no answer from the hub this long = not connected (the station's own header)
CALLS = ("set_mode", "set_talkgroup", "set_talkgroup_group", "retranscribe", "set_channel_prefs",
         "emergency_all_clear",       # an alarm's All clear on the hub: the emergency window here closes (#128)
         "transcribe_now",            # the hub's fallback when Amazon Transcribe can't: whisper at live priority (#195)
         "catch_up",                  # shed clips caught up (sizeup/sttcatchup.py): whisper, under every other clip
         "set_adsb_receiver",         # Settings > Radios on the hub: the dongle reserved for ADS-B (#235)
         "set_antenna_groups",        # ... the antenna groups: dongles locked to jobs (sizeup/radio/groups.py)
         "sources_poll_now",          # a pre-alert on the hub: the CAD feed adapters here poll now (#297)
         "reception_post",            # Settings > Radios on the hub: a reception test here (#410)
         "reception_sample_b64",      # ... and a call it captured, to listen to (never stored on the hub)
         "set_stt_alert_keys",        # the sources the hub's pre-alerts push from: HIGH at least in whisper (#639 ph. 2)
         "set_stt_terms",             # the private term lists' top for whisper's prompt (sizeup/stt_terms.py)
         "quarantine_dismiss",        # Health's Dismiss: the stuck / late events the hub refused (outbox.py dismiss)
         "stt_withdraw")              # the hub's pool took these clips: this whisper skips them (#927; status caps)
# A call's arguments that must be given, never left to the function's default: a default that means "everything"
# (retranscribe's ids=None: every recording on the disk). Given as null, it is asked for; left out, it is refused. The
# parameters without a default are required anyway.
CALL_MUST_NAME = {"retranscribe": ("ids",)}


def call_args(fn: str, f, args) -> dict | str | None:
    """A call's arguments, by name, for f: an object by name (a name f does not take is ignored), or a list by position
    (a hub's form so far; one past f's last parameter is ignored: a newer hub's). Why not, as a string: not a list or an
    object, or an argument f must have (CALL_MUST_NAME, or one without a default) left out. None: f's parameters can't
    be told apart (no signature, *args): the args as given."""
    if not isinstance(args, (list, dict)):
        return "args: a list or an object"
    try:
        params = list(inspect.signature(f).parameters.values())
    except (TypeError, ValueError):
        return None                         # (no signature to read: as given, _call)
    if any(p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD, p.POSITIONAL_ONLY) for p in params):
        return None                         # (a stand-in that takes anything: as given, _call)
    names = [p.name for p in params]
    if isinstance(args, list):
        given = dict(zip(names, args))
    else:
        given = {k: v for k, v in args.items() if k in names}
    must = [p.name for p in params if p.default is p.empty] + [n for n in CALL_MUST_NAME.get(fn, ()) if n in names]
    missing = [n for n in must if n not in given]
    if missing:
        return f"TypeError: {fn}() missing argument {', '.join(map(repr, missing))} (a call never runs on its defaults)"
    return given


def clip_8k(wav: bytes | None) -> bytes | None:
    """A 16 kHz mono clip as an 8 kHz one (every other sample: the clip has nothing above 3.3 kHz)."""
    if not wav:
        return None
    try:
        with wave.open(io.BytesIO(wav)) as w:
            rate, width, ch = w.getframerate(), w.getsampwidth(), w.getnchannels()
            pcm = np.frombuffer(w.readframes(w.getnframes()), "<i2")
    except (wave.Error, EOFError):
        return wav
    if rate != 16000 or width != 2 or ch != 1:
        return wav
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(8000)
        out.writeframes(np.ascontiguousarray(pcm[::2]).tobytes())
    return buf.getvalue()


def source_rows(runner) -> list[dict]:
    """Each incident source's own health from #297's SourceRunner (its runs: the adapter, and (state, reason) as its
    runner last sent it; none yet: "starting"). [] without a runner."""
    if runner is None:
        return []
    out = []
    for r in runner.runs:
        a, h = r.a, r.health
        out.append({"source": a.source, "label": a.label, "state": h[0] if h else "starting",
                    "reason": (h[1] if h else "") or ""})
    return out


POOL_CAP = "stt_pool"      # the hub takes clips handed to its pool (sizeup/cloud/hub.py POOL_CAP, #639 phase 2)
# what this station takes from the hub (its status' "caps", station-state status.caps): the hub's pool withdrawing clips
# from its whisper (the stt_withdraw call, App.stt_withdraw, #927)
STATION_CAPS = ("stt_withdraw",)
RESTART_S = (1.0, 30.0)    # a link thread that raised starts again after this long (doubling, at most the second)
STT_LANE_LOOK_S = 2.0      # the STT lane looks again this often while it waits (an event's ack, or a new input, wakes it)


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


# The hub's state answer, field by field (D6 of #774): (check, what it must be). A field of another type is ignored and
# the value it last gave stays (said once per field); absent (or null) is what it always was: a hub without it.
ANSWER = {
    "build": (lambda v: isinstance(v, str), "a string"),
    "caps": (lambda v: isinstance(v, list) and all(isinstance(c, str) for c in v), "a list of strings"),
    "services": (lambda v: isinstance(v, dict), "an object"),
    "pool": (lambda v: isinstance(v, dict), "an object"),
    "station": (lambda v: isinstance(v, str), "a string"),
}
# the rest of what the hub answers, as said when it isn't so
OTHER = {"answer": (None, "an object"), "jobs": (None, "a list of objects"), "jobs[]": (None, "an object")}
# ... and the pool's word (#639 phase 2), each of its fields so
POOL = {"on": (lambda v: isinstance(v, bool), "true or false"), "worker": (lambda v: isinstance(v, bool), "true or false"),
        "ready": (_is_int, "an integer"), "max_s": (_is_num, "a number"),
        "gpu_idle": (lambda v: isinstance(v, bool), "true or false"), "gpu_idle_n": (_is_int, "an integer")}


def _cpu_pct() -> float | None:
    """The machine's load as a CPU %: the 1-minute load average over the CPUs (None where there is none)."""
    import os
    try:
        return 100.0 * os.getloadavg()[0] / max(1, os.cpu_count() or 1)
    except (OSError, AttributeError):
        return None


class StationLink:
    def __init__(self, app, cfg, log: Callable[[str], None] = print,
                 client: Callable[[float], HubClient] | None = None, outbox: Outbox | None = None,
                 updates: Watch | None = None):
        """outbox: the one already open (Settings > Station > Hub link turns the link off and on
        again over the same outbox, sizeup/cloud/stationmode.py); updates: the hub's build -> the
        auto-updater (kept across that too)."""
        self.app, self.cfg, self.log = app, cfg, log
        self.station_id = cfg.station_id        # the id the hub assigns this station, once it says (adopt)
        self.updates = updates or Watch(log=log)
        self.outbox = outbox or open_outbox(cfg.cloud_outbox or ":memory:", int(cfg.cloud_outbox_max_mb * 1024 * 1024),
                                            log=log)
        # what the hub last said it knows (#409), kept beside the outbox: a station starting while the hub is away
        # still queues another system's rows for it
        self._caps_file = (Path(cfg.cloud_outbox).with_name("hub_caps.json")
                           if cfg.cloud_outbox and cfg.cloud_outbox != ":memory:" else None)
        try:
            if self._caps_file is not None and self._caps_file.is_file():
                kept = json.loads(self._caps_file.read_text())
                if ANSWER["caps"][0](kept):                 # (a file of another shape: as if none)
                    self.hub_caps = frozenset(kept)
        except (OSError, ValueError, TypeError):
            pass
        # the admins' service per source, as the hub last sent it (sourcekeys.set_service_overrides): kept beside it too
        self._services_file = self._caps_file.with_name("hub_services.json") if self._caps_file is not None else None
        try:
            if self._services_file is not None and self._services_file.is_file():
                self._set_services(json.loads(self._services_file.read_text()), keep=False)
        except (OSError, ValueError, TypeError):
            pass
        self._client = client or (lambda timeout: HubClient(cfg.cloud_hub_url, cfg.cloud_token, cfg.cloud_access_id,
                                                            cfg.cloud_access_secret, timeout))
        self._stop = threading.Event()
        self._wake = threading.Event()          # a new event in the outbox
        self._live_wake = threading.Event()     # talking changed, or status wanted now
        self._status_now = False
        self._lock = threading.Lock()
        self.last_ok: float | None = None       # the hub's last answer (any thread)
        self.last_error, self.error_since = "", None
        self.sent = 0
        # this machine (CPU, memory, disk, temperature, uptime) and its radio programs: status["health"]
        # (sizeup/cloud/health.py; the hub's station health and alerts)
        disk = cfg.cloud_outbox if cfg.cloud_outbox and cfg.cloud_outbox != ":memory:" else (cfg.clips_dir or ".")
        self.metrics = Metrics(str(Path(disk).parent) if disk != "." else ".")
        self._threads: list[threading.Thread] = []
        self._answer_bad: set[str] = set()      # the answer's fields said to be of another type (said once each)
        # the link's threads that raised and started again, by name: {n, why, at} (the Hub row, Health's hints)
        self.restarts: dict[str, dict] = {}
        # what the hub left out of this station's posts for its shape (its answer's "refused" / "reason"): "" none
        self.refused, self._refused = "", {"status": "", "live": ""}
        self._watch_lock = threading.Lock()
        # our ADS-B receiver's aircraft, straight to the hub (#235, sizeup/cloud/aircraftlink.py): live data, never queued
        self.aircraft = AircraftForwarder(app, cfg, self._client, log)
        # our AIS receiver's vessels (#402, sizeup/cloud/vessellink.py): only with [vessels.receiver] enabled
        self.vessels = VesselForwarder(cfg, self._client, log)
        # the station's journal (handed over by sizeup-logread), scrubbed, to the hub and CloudWatch
        # (sizeup/cloud/logship.py): spooled on disk; the incident-source adapter's lines stay here unless Settings say
        self.logs = LogShipper(cfg, self._client, log, app=app)
        # the wall clock's steps and this boot (sizeup/cloud/wallclock.py; every state post, #773 / #788): sampled on the
        # metrics sampler's 1 s tick; kept on the app (the link going off and on) and beside the outbox (a restart)
        if not isinstance(getattr(app, "wall_clock", None), WallSteps):
            app.wall_clock = WallSteps(log, path=self._caps_file.with_name(WALL_FILE) if self._caps_file else None,
                                       note=lambda text: self._outbox_note(text))
        self.wall: WallSteps = app.wall_clock
        # the signal metrics (sizeup/cloud/metrics.py): sampled here, to the hub on their own thread, never the outbox
        self.metrics_link = MetricsLink(app, cfg, self._client, log, tick=self.wall.sample)
        # each transmission's / page's speech-to-text input: made off the radio path, into the outbox's own lane
        # (held while whisper is behind: the FLAC is made once it caught up, #677)
        self.stt_encoder = sttinput.Encoder(self._stt_made, log, held=self._whisper_behind)
        self._stt_wake = threading.Event()
        # the hub's speech-to-text pool (#639 phase 2): its word (on, may this station's whisper claim, how many others
        # could take a clip now: its state answer's "pool"), and this station's whisper as one of its workers
        self.pool: dict = {}
        from sizeup.cloud.stationworker import StationWorker
        self.pool_worker = StationWorker(app, self, self._client, log, cpu=_cpu_pct)
        self.stt_refused = 0                    # the hub refused them for good (a bad one: never sent again)
        self.stt_sent = 0
        app.events, app.link = self.emit, self
        app.cad.cloud = self.cad_cloud
        n, _ = self.outbox.pending()
        if n:
            log(f"hub link: {n} event(s) still queued from before, sending them first")

    def _wake_stt(self) -> None:
        """The STT lane looks now (a link built without __init__, as some tests do, has no lane: nothing)."""
        wake = getattr(self, "_stt_wake", None)
        if wake is not None:
            wake.set()

    # -- the app's side ---------------------------------------------------------------------------

    def _outbox_note(self, text: str) -> None:
        """A short note in the outbox's own notes: on Health (\"Station outbox\") for a day, from the status."""
        ob = self.outbox
        ob.notes = (list(getattr(ob, "notes", None) or []) + [{"at": time.time(), "text": text[:120]}])[-NOTES_KEPT:]

    hub_caps: frozenset = frozenset()       # what the hub says it knows ("caps" in its answers): "p25_systems" (#409)

    def _set_caps(self, caps) -> None:
        """What the hub says it knows: a list of strings (none: a hub without any). Anything else is never taken."""
        if caps is not None and not (isinstance(caps, (list, tuple, set, frozenset))
                                     and all(isinstance(c, str) for c in caps)):
            return self._wrong("caps")
        caps = frozenset(caps or [])
        if caps != self.hub_caps:
            self.hub_caps = caps
            try:
                if getattr(self, "_caps_file", None) is not None:
                    self._caps_file.write_text(json.dumps(sorted(caps)))
            except OSError:
                pass

    def _set_services(self, m, keep: bool = True) -> None:
        """The hub's service per source (its answer's "services"): every row tagged here from now on uses it, the
        station's own CAD's never; kept beside the outbox when it changed."""
        if not isinstance(m, dict):
            if m is not None and keep:
                self._wrong("services")
            return
        m = sourcekeys.clean_services(m)
        if m == sourcekeys.service_overrides() and sourcekeys._service_from_hub:
            return
        sourcekeys.set_service_overrides(m, hub=True)
        f = getattr(self, "_services_file", None)
        if keep and f is not None:
            try:
                f.write_text(json.dumps(m))
            except OSError:
                pass
        self.log(f"hub link: service per source from the hub: {len(m)} chosen")

    def _wrong(self, field: str) -> None:
        """A field of the hub's answer of another type than it must be: ignored (the last good value stays), said once
        until it is good again."""
        bad = self.__dict__.setdefault("_answer_bad", set())
        if field not in bad:
            bad.add(field)
            kept = ANSWER.get(field) or (POOL.get(field[5:]) if field.startswith("pool.") else None)
            what = (kept or OTHER.get(field) or (None, "of its type"))[1]
            self.log(f"hub link: the hub's {field} is not {what}: ignored"
                     + (", its last good value kept" if kept else ""))

    def _good(self, field: str) -> None:
        self.__dict__.setdefault("_answer_bad", set()).discard(field)

    def _set_pool(self, word) -> None:
        """The pool's word (its state answer's "pool"; a hub without it: {}): each field of its type, else the value it
        last had."""
        if word is None:
            self.pool = {}
            return
        if not isinstance(word, dict):
            return self._wrong("pool")
        out = dict(word)
        for k, (ok, _what) in POOL.items():
            if k in out and not ok(out[k]):
                self._wrong(f"pool.{k}")
                if k in self.pool:
                    out[k] = self.pool[k]
                else:
                    del out[k]
            elif k in out:
                self._good(f"pool.{k}")
        self.pool = out

    def apply_answer(self, r) -> None:
        """The hub's answer to a state post, field by field: a field of another type is ignored (the last good value
        stays, ANSWER); one that fails to apply never stops the others, nor the thread (said once per field)."""
        if not isinstance(r, dict):
            return self._wrong("answer")
        self._good("answer")
        for field, apply in (("build", self.updates.hub), ("caps", self._set_caps),
                             ("services", self._set_services),     # (a hub before this sends none: kept as they were)
                             ("pool", self._set_pool),             # (#639 phase 2; older: none)
                             ("station", self.adopt)):
            v = r.get(field)
            if v is not None and not ANSWER[field][0](v):
                self._wrong(field)
                continue
            self._good(field)
            try:
                apply(v)
                self._good(f"{field}!")
            except Exception as e:  # noqa: BLE001 - one field never in the way of the rest, nor of the link
                if f"{field}!" not in self.__dict__.setdefault("_answer_bad", set()):
                    self._answer_bad.add(f"{field}!")
                    self.log(f"hub link: the hub's {field} not applied: {type(e).__name__}: {str(e)[:120]}")

    def _left_out(self, r, had_status: bool) -> None:
        """The hub's word on what it left out of this post for its shape (its answer's "refused": ["status", "live"],
        "reason"; the rest was taken): said once per reason, on the Hub row while it lasts. A post that carried a status
        and has no word on it clears the status' word; any post taken without a word on the talkers clears theirs."""
        ref = r.get("refused") if isinstance(r, dict) and isinstance(r.get("refused"), list) else []
        why = str(r.get("reason") or "?")[:300] if isinstance(r, dict) else "?"
        for part, sent in (("status", had_status), ("live", True)):
            if part in ref:
                text = f"the hub left out this station's {'status' if part == 'status' else 'talkers / pages'}: {why}"
                if text != self._refused[part]:
                    self.log(f"hub link: {text}")
                self._refused[part] = text
            elif sent:
                self._refused[part] = ""
        self.refused = " · ".join(t for t in self._refused.values() if t)

    def adopt(self, sid) -> None:
        """The id the hub took this station's post as ("station" in its state answer): this station's id from now on,
        in its events, status, logs and metrics. Memory only, never written to the config: the hub keeps taking a
        renamed station's former id from the shared token (sizeup/cloud/stations.py aliases), so a restart, or an
        older release after a switch back, that starts with the config's id still works, and is told again."""
        if not isinstance(sid, str) or not sid or len(sid) > 64 or not sid.isprintable() or sid == self.station_id:
            return
        self.log(f"hub link: the hub names this station {sid} (was {self.station_id}; [station] id: {self.cfg.station_id})")
        self.station_id = sid
        self.logs.station = sid
        self.metrics_link.station = sid

    def emit(self, kind: str, data: dict, clip: str | None = None) -> None:
        # Another P25 system's (#409): a hub from before it would file the row as the county's talkgroup of that
        # number, so it is not sent there (the station's own page still has it); its audio waits in its own budget.
        context = kind == "activity" or (kind in ("traffic", "alarm") and bool(data.get("system")))
        if context and "p25_systems" not in self.hub_caps:
            return
        audio = clip_8k(self.app.clip(clip)) if clip else None
        self.outbox.add(kind, data, audio, context=context)
        self._wake.set()

    def pool_takes(self) -> bool:
        """The hub takes clips this station hands to its pool (#639 phase 2): it knows them, and its pool is on."""
        return POOL_CAP in self.hub_caps and bool(self.pool.get("on"))

    def pool_has_room(self, seconds: float | None = None) -> bool:
        """The hub's last word, heard lately: a worker other than this station's whisper could take a context clip (this
        long) now. Never a round trip: a clip no worker elsewhere could take stays here; nor to a hub not heard lately
        (its last word may be stale: a hub deploying), which keeps it here (saved, or queued) instead."""
        ready, max_s = self.pool.get("ready"), self.pool.get("max_s")
        if not self.pool_takes() or not _is_int(ready) or ready <= 0 or not self.connected():
            return False
        return seconds is None or (_is_num(max_s) and seconds <= max_s)

    def pool_gpu_idle(self) -> bool:
        """The hub's last word, heard lately: a GPU worker idle and quick on pre-alert clips now (a stopping station
        hands those over only then; else they are saved here and transcribed first at the next start)."""
        return self.pool_gpu_idle_n() > 0

    def pool_gpu_idle_n(self) -> int:
        """How many: a stopping station hands over one pre-alert clip per idle quick GPU worker at most (a second one
        would wait behind the first, or fall to this station's own whisper, which is stopping)."""
        if not self.pool_takes() or not self.connected():
            return 0
        n = self.pool.get("gpu_idle_n")
        return max(0, n) if _is_int(n) else int(self.pool.get("gpu_idle") is True)

    def stt_input(self, item, audio, prio: int = sttinput.NORMAL) -> None:
        """App._stt: this transmission / page goes to speech-to-text now with `audio`. Queued (a reference, nothing
        made here: the radio threads never wait); its FLAC is made on the encoder's thread and goes up on its own.
        Not even queued for a hub that doesn't take them (an older one): no encoding for nothing. prio: its channel's
        (#677, App._stt_lane): the pre-alert channels' go first and are dropped last."""
        if sttinput.CAP in self.hub_caps:
            self.stt_encoder.submit(item, audio, prio)

    def _whisper_behind(self) -> bool:
        """The station's whisper is behind on live traffic: the oldest clip of a live class (URGENT, HIGH, LIVE: never
        a context talkgroup's, kept up to 15 or 30 min by design, nor a re-run's or the catch-up's) has waited more
        than stt.BEHIND_S: Transcriber.behind_live() (thread-safe). The STT inputs of NORMAL / LOW priority wait
        meanwhile (sttinput.Encoder: never PRE / FIRE, at most HOLD_MAX_S). A whisper without it (an older Transcriber,
        a test's stand-in): its queue's own list, read by the same rule. Anything unexpected: not behind."""
        try:
            stt = getattr(self.app, "stt", None)
            live = getattr(stt, "behind_live", None)
            if callable(live):
                return bool(live())
            from sizeup.stt import BEHIND_S, LIVE_CLASSES
            q = getattr(stt, "_q", None)
            if q is None:
                return False
            now = q.clock()
            with q._cv:
                ats = [e[5] for e in q._items if e[0] in LIVE_CLASSES]
            return any(now - float(at) > BEHIND_S for at in ats)
        except Exception:  # noqa: BLE001 - never in the lane's way
            return False

    def _stt_made(self, data: dict, flac: bytes, meta: dict, prio: int = sttinput.NORMAL) -> None:
        self.outbox.add_stt(data, flac, meta, prio=prio)
        self._wake_stt()

    def stt_stats(self) -> dict:
        """The STT inputs: waiting (to be made, to go up), sent, and dropped by reason."""
        enc = self.stt_encoder.stats()
        n, b = self.outbox.stt_pending()
        dropped = dict(enc["dropped"], outbox=self.outbox.dropped_stt, refused=self.stt_refused)
        return {"queued": enc["queued"], "waiting": n, "bytes": b, "made": enc["made"], "sent": self.stt_sent,
                "dropped": dropped, "hub_takes": sttinput.CAP in self.hub_caps,
                # (#677) of the dropped, the pre-alert channels' (only once nothing of a lower priority was left)
                "dropped_pre": self.stt_encoder.dropped_pre + self.outbox.dropped_stt_pre,
                "dropped_fire": self.stt_encoder.dropped_fire + self.outbox.dropped_stt_fire,
                # (#677) where the FLAC is made (scope | process | thread (why)), and held now for whisper
                "wake_on_ack": True,         # (#678) its lane goes at once after the events' ack: the hub's short waits
                "encoder": self.stt_encoder.where, "held": self.stt_encoder.holding,
                "worker": self.stt_encoder.worker()}       # (its state: a wedged-but-alive service is seen)

    def live_changed(self) -> None:
        self._live_wake.set()

    def start(self) -> StationLink:
        if self._threads:
            return self
        self._threads = [go() for _name, go in self._loops()]
        self.aircraft.start()
        self.vessels.start()                  # (idle unless [vessels.receiver] enabled)
        self.logs.start()
        self.metrics_link.start()
        self.pool_worker.start()              # (idle until the hub says its pool takes stations)
        return self

    def _loops(self) -> tuple:
        """The link's own threads, in order: (name, start it supervised)."""
        return (("hub-forward", lambda: self._spawn("hub-forward", self._forward)),
                ("hub-state", lambda: self._spawn("hub-state", self._state)),
                ("hub-jobs", lambda: self._spawn("hub-jobs", self._jobs)),
                ("hub-stt", lambda: self._spawn("hub-stt", self._stt_forward)))

    def _spawn(self, name: str, fn) -> threading.Thread:
        t = threading.Thread(target=self._supervised, args=(name, fn), name=name, daemon=True)
        t.start()
        return t

    def _supervised(self, name: str, fn) -> None:
        """A link thread's loop that never dies: whatever it raises (a hub's answer nothing expected, a bug) is said
        (once per thread and error type), counted (the Hub row, Health) and the loop starts again, after RESTART_S
        (doubling while it keeps raising; a fresh client, its own state as at the start)."""
        wait = RESTART_S[0]
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                fn()
                return                                  # (stopped)
            except Exception as e:  # noqa: BLE001 - the link's threads never die
                self._restarted(name, e)
            if time.monotonic() - t0 > 10 * RESTART_S[1]:
                wait = RESTART_S[0]                     # (it ran a good while: not the same streak)
            self._stop.wait(wait)
            wait = min(RESTART_S[1], wait * 2)

    def _restarted(self, name: str, e: BaseException) -> None:
        why = type(e).__name__
        with self._lock:
            r = self.restarts.setdefault(name, {"n": 0, "why": "", "at": None})
            said = r["why"] == why
            r.update(n=r["n"] + 1, why=why, at=time.time())
        if not said:
            self.log(f"hub link: {name} raised {why}: {str(e)[:160]}; started again")

    def watchdog(self) -> list[str]:
        """The link's long-lived threads, each alive while the link runs: one that died (a forwarder's own loop has
        a catch-all; this is the check of the check) is started again and counted like a restart. The names."""
        if not self._threads or self._stop.is_set() or not self._watch_lock.acquire(blocking=False):
            return []
        try:
            return self._watch()
        finally:
            self._watch_lock.release()

    def _watch(self) -> list[str]:
        def dead(t) -> bool:                            # (one made but not started yet, in start(): not dead)
            return t is not None and t.ident is not None and not t.is_alive()
        out = []
        for i, (name, go) in enumerate(self._loops()):
            if i < len(self._threads) and dead(self._threads[i]):
                self._restarted(name, RuntimeError("thread ended"))
                self._threads[i] = go()
                out.append(name)
        for name, obj in (("hub-aircraft", self.aircraft), ("hub-vessels", self.vessels), ("hub-logs", self.logs),
                          ("pool-worker", self.pool_worker)):
            if dead(getattr(obj, "_thread", None)) and not obj._stop.is_set():
                self._restarted(name, RuntimeError("thread ended"))
                obj._thread = None
                obj.start()
                out.append(name)
        ml = self.metrics_link
        for i, (name, make) in enumerate((
                ("station-metrics", lambda: threading.Thread(target=ml._sample, name="station-metrics", daemon=True)),
                ("hub-metrics", lambda: threading.Thread(target=ml._send, name="hub-metrics", daemon=True)))):
            if i < len(ml._threads) and dead(ml._threads[i]) and not ml._stop.is_set():
                self._restarted(name, RuntimeError("thread ended"))
                ml._threads[i] = make()
                ml._threads[i].start()
                out.append(name)
        return out

    def restarts_text(self, within_s: float = 3600.0) -> str:
        """The link's threads that started again in the last hour: "hub-state 2× (TypeError, 14:05)"; "" for none."""
        now = time.time()
        with self._lock:
            rs = sorted((k, dict(v)) for k, v in self.restarts.items() if now - (v["at"] or 0) < within_s)
        return ", ".join(f"{n} {r['n']}× ({r['why']}, {tz.hhmm(r['at'])})" for n, r in rs)

    @property
    def running(self) -> bool:
        return bool(self._threads) and not self._stop.is_set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def stop(self) -> None:
        """The threads end at their next turn (a long poll in flight: when it returns)."""
        self._stop.set()
        self._wake.set()
        self._live_wake.set()
        self._stt_wake.set()
        self.stt_encoder.stop()
        self.aircraft.stop()
        self.vessels.stop()
        self.logs.stop()
        self.metrics_link.stop()
        self.pool_worker.stop()
        self.wall.stop()                      # (the step count saved: a restart carries it on)

    # -- health -------------------------------------------------------------------------------------

    def _ok(self) -> None:
        with self._lock:
            was = self.connected()
            self.last_ok, self.last_error, self.error_since = time.time(), "", None
        if not was:
            self.log(f"hub link: connected to {self.cfg.cloud_hub_url}")

    def _fail(self, e: Exception) -> None:
        refused = isinstance(e, HubError) and e.status in (401, 409) and str(e).startswith("hub refused this station")
        with self._lock:
            first = self.error_since is None
            self.last_error = str(e)[:400 if refused else 200]
            self.error_since = self.error_since or time.time()
            # the hub refusing this station's token (revoked, unknown, another id): said when it starts, even in the
            # middle of an outage, and again when the reason changes (sizeup/cloud/client.py REFUSED_HINT)
            again = refused and str(e) != getattr(self, "_refused_said", "")
            if refused:
                self._refused_said = str(e)
        if first or again:
            self.log(f"hub link: {e}")

    def connected(self) -> bool:
        return self.last_ok is not None and time.time() - self.last_ok < CONNECTED_S and self.error_since is None

    def status_row(self) -> dict:
        """The station's own header: a "Hub" row next to the radios."""
        n, oldest = self.outbox.pending()
        queued = f"{n} event(s) waiting to go up" + (f", oldest {tz.hhmm(oldest)}"
                                                     if oldest else "") if n else "nothing waiting"
        if self.connected():
            state, word = "ok", (f"{n} queued" if n > 5 else "")
            detail = f"connected to the hub {self.cfg.cloud_hub_url} · {queued}"
        elif self.last_ok is None and self.error_since is None:
            state, word, detail = "starting", "", f"connecting to the hub {self.cfg.cloud_hub_url}"
        else:
            since = self.error_since or self.last_ok
            state, word = "down", f"offline {tz.hhmm(since)}"
            detail = (f"can't reach the hub {self.cfg.cloud_hub_url} since {tz.hhmmss(since)}"
                      + (f" ({self.last_error})" if self.last_error else "") + f" · {queued}: nothing is lost, "
                      "it goes up when the hub is back")
        if self.outbox.dropped_audio:
            detail += f" · {self.outbox.dropped_audio} clip(s) dropped to keep the outbox under its limit"
        q = self.outbox.summary(limit=0)
        if q["n"]:
            detail += f" · {q['n']} event(s) refused by the hub, kept here" + (f" ({q['critical']} critical, retried)"
                                                                            if q["critical"] else "")
        lost = sum(self.stt_stats()["dropped"].values())
        if lost:
            detail += f" · {lost} STT input(s) dropped (the hub used the clip)"
        if getattr(self, "refused", ""):
            detail += f" · {self.refused}"
        self.watchdog()
        again = self.restarts_text()
        if again:
            detail += f" · restarted: {again}"
        v = self.updates.status()
        detail += f" · version: {v['text']}"
        ad = self.adapter(v)
        if ad.get("text"):
            detail += f" · adapter: {ad['text']}"
        if not word and v["state"] in ("updating", "failed"):
            word = "updating" if v["state"] == "updating" else "update failed"
        return {"label": "Hub", "mhz": None, "state": state, "word": word, "detail": detail, "open": False,
                "quieting_db": None, "restarts": 0, "last_heard": None}

    def adapter(self, v: dict | None = None) -> dict:
        """The incident-source adapter (#301): what the release has installed, the updater's last word, and each
        source's own health from the runner (#297, when there is one). Names and states only, never a credential."""
        ad = dict((v or self.updates.status()).get("adapter") or {})
        runner = getattr(self.app, "sources_runner", None)
        try:
            rows = source_rows(runner)
        except Exception as e:  # noqa: BLE001 - a report: never in the way of the status (but said, by type)
            rows = []
            if getattr(self, "_rows_said", "") != type(e).__name__:
                self._rows_said = type(e).__name__
                self.log(f"hub link: incident sources' health not read: {type(e).__name__}")
        ad["health"] = [{k: str(r.get(k) or "")[:120] for k in ("source", "label", "state", "reason")}
                        for r in rows if isinstance(r, dict)][:10]
        if ad["health"] and ad.get("text"):
            ad["text"] += " · " + ", ".join(f"{h['label'] or h['source']} {h['state'] or '?'}"
                                            + (f" ({h['reason']})" if h["reason"] and h["state"] != "ok" else "")
                                            for h in ad["health"])
        said = [h for h in ad["health"] if h["state"] != "starting"]        # (not polled yet: no word either way)
        if ad.get("state") == "installed" and said:              # installed, and the runner says how it runs
            ad["state"] = "running" if all(h["state"] == "ok" for h in said) else "failing"
        return ad

    def version(self) -> dict:
        """The updater's status for the hub, the adapter's with its runtime health."""
        v = self.updates.status()
        v["adapter"] = self.adapter(v)
        return v

    def cad_cloud(self) -> dict:
        """The station's console header: the CAD of record is the hub's."""
        row = self.status_row()
        return {"role": "station", "hub_url": self.cfg.cloud_hub_url, "state": row["state"], "detail": row["detail"]}

    # -- events (in order, durable) ------------------------------------------------------------------

    def _forward(self) -> None:
        client = self._client(60.0)
        backoff = Backoff(first=1.0, fast_cap=5.0, cap=60.0)      # a deploy is seconds now (#247)
        while not self._stop.is_set():
            wait = self._forward_once(client, backoff)
            if wait is None:
                self._wake_stt()        # (the outbox emptied with no ack: a quarantined or unreadable last event, #678)
                self._wake.wait(5.0)
                self._wake.clear()
            elif wait > 0:
                self._stop.wait(wait)

    def _post_events(self, client: HubClient, rows: list[dict]) -> dict:
        # (one cursor (epoch, seq) per outbox, and the hub keeps one: a cursor per node is step 3 of the nodes design,
        # docs/nodes.md)
        body = {"epoch": self.outbox.epoch, "station": self.station_id,
                "events": [{"seq": r["seq"], "at": r["at"], "type": r["type"], "data": r["data"],
                            "audio": base64.b64encode(r["audio"]).decode() if r["audio"] else None,
                            "audio_dropped": r["audio_dropped"],
                            # a quarantined event sent again: its quarantine row (the hub applies such a feed snapshot
                            # only over an older one; an older hub ignores the key)
                            **({"qid": r["qid"]} if isinstance(r.get("qid"), int) else {})} for r in rows]}
        return client.request("POST", "/api/station/events", body, timeout=60.0)

    def _forward_once(self, client: HubClient, backoff: Backoff, now: float | None = None) -> float | None:
        """One batch to the hub. Returns how long to wait before the next (0: go on now), None when nothing waits.

        In order, the oldest first. A batch the hub refuses for good (client.REJECTED: 400 / 413 / 422) is split
        (_rejected) until the event it refuses is alone, which goes to the quarantine (outbox.py) so everything behind
        it still goes; the critical ones are sent again on a slow schedule and raise a critical alert on the hub
        (health.py "rejected"). Anything else (5xx, network, a refused token) is retried as it is, with backoff."""
        for q in self.outbox.requeue_due(now):
            if q["superseded"]:
                self.log(f"hub link: a {q['kind']} the hub refused is dropped: the feed has a newer report of it")
            else:
                self.log(f"hub link: sending a {q['kind']} the hub refused again (refused {q['tries']} time(s) so far)")
        iso = self._isolate
        rows = (self.outbox.batch(max_bytes=iso["bytes"], max_events=iso["n"], meta=True) if iso
                else self.outbox.batch(meta=True))
        if not rows:
            self._isolate = None
            return None
        try:
            r = self._post_events(client, rows)
            acked = r["acked"]
            if not _is_int(acked):                  # (true would ack seq 1; "7" or 7.5 is no hub's answer)
                raise ValueError(f"the hub's acked is not an integer ({type(acked).__name__})")
        except (HubError, KeyError, TypeError, ValueError) as e:
            if isinstance(e, HubError) and e.status in REJECTED:
                wait = self._rejected(client, rows, e, now)
                if wait is not None:
                    return wait
            self._fail(e)
            return backoff.fail(e)
        # the events the hub took but couldn't apply ("rejected", a hub from before this sends none): its cursor is past
        # them, a copy is kept here (and a critical one sent again later)
        bad = {}
        listed = r.get("rejected") if isinstance(r, dict) else None
        for x in listed if isinstance(listed, list) else []:
            if isinstance(x, dict) and isinstance(x.get("seq"), int) and x["seq"] <= acked:
                bad[x["seq"]] = x
        for row in rows:
            if row["seq"] in bad:
                self._quarantined(row, str(bad[row["seq"]].get("reason") or "apply_failed"),
                                  str(bad[row["seq"]].get("error") or ""), now)
        t = time.time() if now is None else now
        for x in rows:
            if not (x["qid"] and x["seq"] <= acked and x["seq"] not in bad):
                continue
            end = x["data"].get("end") if x["type"] == "alarm" else None
            if isinstance(end, (int, float)) and t - end > ALARM_MAX_AGE_S:
                # the hub raises no alarm this old (sizeup/cad/alarms.py MAX_AGE_S): taken, never raised. Kept, its
                # critical alert on: a person hears it, and drops it
                why = f"alarm delivered late, not raised ({(t - end) / 60:.0f} min after it ended)"
                self.outbox.late(x["qid"], why, now)
                self.log(f"hub link: CRITICAL: {why}")
            else:
                self.outbox.released([x["qid"]])
                self.log(f"hub link: the hub took the {x['type']} it had refused")
        self.outbox.ack(acked)
        # the STT lane waits while an event is in the outbox: wake it now the events went, so a clip's STT input
        # follows its event at once instead of at the lane's next 2 s look (that look was the hub's ~2 s wait, #678)
        self._wake_stt()
        self.sent += sum(1 for x in rows if x["seq"] <= acked and x["seq"] not in bad)
        if iso and acked >= iso["hi"]:
            self._isolate = None                    # past every event of the batch it refused: whole batches again
        if not self._stop.is_set():
            self._ok()
        backoff.ok()
        return 0.0

    _isolate: dict | None = None        # splitting a refused batch: {n, bytes, hi (its last seq), probed}

    def _rejected(self, client: HubClient, rows: list[dict], e: HubError, now: float | None = None) -> float | None:
        """The hub refused this batch for good. Split it in halves (by size, too, for a 413) until the event it refuses
        is alone, then quarantine that one; the rest goes in order. Returns 0 (go on now), or None: not the events' fault
        after all (a hub that refuses even an empty batch), retried as it is with backoff."""
        doc = e.body if isinstance(e.body, dict) else {}
        reason = str(doc.get("reason") or ("too_large" if e.status == 413 else f"http_{e.status}"))[:40]
        error = str(doc.get("error") or e)[:300]
        if len(rows) > 1:
            iso = self._isolate or {"hi": rows[-1]["seq"], "probed": False}
            size = sum(len(json.dumps(x["data"], default=str)) + len(x["audio"] or b"") for x in rows)
            if self._isolate is None:
                self.log(f"hub link: the hub refused {len(rows)} event(s) ({e.status} {reason}): splitting them "
                         "to find the one it refuses; the rest go on")
            self._isolate = dict(iso, n=max(1, len(rows) // 2), hi=max(iso["hi"], rows[-1]["seq"]),
                                 bytes=max(1, size // 2) if e.status == 413 else (iso.get("bytes") or 4 * 1024 * 1024))
            return 0.0
        row = rows[0]
        if not self._probe(client):
            return None                             # the hub refuses everything: hold every event, as for an outage
        if e.status == 413 and row["audio"]:
            # too large with its clip: it goes without it (the station keeps the clip in its own store)
            self.outbox.strip_audio(row["seq"])
            self.log(f"hub link: a {row['type']} too large for the hub ({len(row['audio']) // 1024} KB of audio): "
                     "sent without its clip")
            return 0.0
        self._quarantined(row, reason, error, now)
        return 0.0

    def _probe(self, client: HubClient) -> bool:
        """Does the hub take an empty batch? Then a batch it refuses is refused for its events, never the hub's own
        trouble (once per split)."""
        if self._isolate is not None and self._isolate.get("probed"):
            return True
        try:
            acked = client.request("POST", "/api/station/events",
                                   {"epoch": self.outbox.epoch, "station": self.station_id, "events": []},
                                   timeout=30.0)["acked"]
            if not _is_int(acked):
                raise ValueError(f"the hub's acked is not an integer ({type(acked).__name__})")
        except (HubError, KeyError, TypeError, ValueError) as e:
            if getattr(self, "_probe_said", "") != str(e)[:80]:
                self._probe_said = str(e)[:80]
                self.log(f"hub link: the hub refuses even an empty batch ({e}): holding every event until it takes them")
            return False
        if self._isolate is not None:
            self._isolate["probed"] = True
        return True

    def _quarantined(self, row: dict, reason: str, error: str, now: float | None = None) -> None:
        """One event the hub refuses: to the quarantine (outbox.py), said in the log (its type and the hub's reason; an
        incident source's error never, it may quote the county's data)."""
        q = self.outbox.quarantine(row, reason, error, now)
        self._isolate = None                        # (found: whole batches again; another bad one is split out the same way)
        said = "" if row["type"] in ("source_event", "source_health", "source_seen") else f": {error[:160]}"
        if q["state"] == "dropped":
            self.log(f"hub link: the hub refused a {row['type']} a person had dropped meanwhile: gone")
        elif q["critical"] and q["state"] == "stuck":
            self.log(f"hub link: CRITICAL: the hub refused a {row['type']} again ({reason}{said}); refused {q['tries']} "
                     "time(s): kept on the station, not sent again (sizeup-station quarantine)")
        elif q["critical"]:
            self.log(f"hub link: CRITICAL: the hub refused a {row['type']} ({reason}{said}); kept on the station, "
                     f"sent again in {int((q['next_at'] - (now or time.time())) // 60)} min (refused {q['tries']} time(s))")
        else:
            self.log(f"hub link: the hub refused a {row['type']} ({reason}{said}); kept on the station, the rest go on")

    # -- the STT inputs (their own lane, under the events) -----------------------------------------------------

    def _stt_forward(self) -> None:
        """POST /api/station/stt: the STT inputs, gzipped, a few hundred KB at a time, only to a hub that takes them and
        only while no event waits in the outbox (a page or a transmission never queues behind one), in the lane's order
        (outbox.py _stt_order, #677 / #701: the pre-alert channels' first, then by n). An input never waits for its own
        event: a clip's traffic event goes only with its text, and the hub keeps an input that comes first (its rows'
        order is the hub's). The events' ack wakes it (#678)."""
        client = self._client(60.0)
        backoff = Backoff(first=2.0, fast_cap=10.0, cap=120.0)
        while not self._stop.is_set():
            if sttinput.CAP not in self.hub_caps or self.outbox.pending()[0] or not self.outbox.stt_pending()[0]:
                self._stt_wake.wait(STT_LANE_LOOK_S)
                self._stt_wake.clear()
                continue
            rows = self.outbox.stt_batch()
            if self.outbox.pending()[0]:            # an event queued meanwhile: it goes first (then the lane is woken)
                continue
            body = {"v": 1, "station": self.station_id,
                    "items": [dict(r["data"], n=r["n"], stt=sttinput.to_wire(r["flac"], r["meta"])) for r in rows]}
            try:
                r = client.request("POST", sttinput.PATH, body, timeout=60.0, gzip_body=True)
                stored, refused = r.get("stored") or [], r.get("refused") or []
                if not all(isinstance(x, list) and all(_is_int(n) for n in x) for x in (stored, refused)):
                    raise ValueError("the hub's stored / refused: lists of integers")
            except (HubError, TypeError, ValueError, AttributeError) as e:
                if isinstance(e, HubError) and e.status == 404:          # a hub without the route after all
                    self._set_caps(self.hub_caps - {sttinput.CAP})
                self._stop.wait(backoff.fail(e))
                continue
            sent = {r["n"] for r in rows}
            self.outbox.stt_ack([n for n in stored + refused if n in sent])
            self.stt_sent += sum(1 for n in stored if n in sent)
            self.stt_refused += sum(1 for n in refused if n in sent)
            backoff.ok()

    # -- talking now + status (latest wins) ------------------------------------------------------------

    def status(self, talkgroups: bool = True) -> dict:
        app = self.app
        n, oldest = self.outbox.pending()
        out = {"id": self.station_id, "config_id": self.cfg.station_id, "name": self.cfg.station_name,
               "radios": [r for r in app.radio_status() if r.get("label") != "Hub"],
               "sdr": app.sdr(), "rerun": dict(app.rerun, pending=len(app._rerun_ids - app._catchup_ids)),
               "channels": app.channels_applied(),
               "p25_systems": app.p25_systems_state(),       # (#409) the other P25 systems: jobs, state, activity
               "reception": app.reception.state(),           # (#410) the reception test: its numbers, the history
               "stt_terms": app.stt_terms_applied(),         # the term lists the hub sent ({v, sets}: versions only)
               "outbox": {"pending": n, "oldest": oldest, "dropped_audio": self.outbox.dropped_audio,
                          "quarantine": self.outbox.summary(),      # (the events the hub refused: health.py "rejected")
                          **({"notes": list(notes)} if (notes := getattr(self.outbox, "notes", None)) else {})},
               "stt_input": self.stt_stats(),                # (each clip's STT input: waiting, sent, dropped by reason)
               "pool_worker": dict(self.pool_worker.stats),  # (its whisper as a pool worker, #639 phase 2)
               "stt_alert_rev": app.stt_alert_rev,           # (the alert sources' revision it has: the hub resends on a change)
               "caps": list(STATION_CAPS),                   # (what it takes from the hub: stt_withdraw, #927)
               "version": self.version()}
        try:
            out["logship"] = self.logs.stats()
        except Exception as e:  # noqa: BLE001 - a report
            out["logship"] = {"state": "error", "reason": type(e).__name__}
        out["metrics"] = self.metrics_link.stats()
        try:
            out["health"] = station_metrics(app, self.metrics)
        except Exception as e:  # health is a report: never in the way of the status itself
            out["health"] = {"error": f"{type(e).__name__}: {e}"[:200]}
        self.watchdog()
        if self.restarts:                               # the link's threads that started again (Health's hints)
            with self._lock:
                out["health"]["link_restarts"] = {k: dict(v) for k, v in self.restarts.items()}
        if talkgroups:
            out["talkgroups"] = app.talkgroups_state() if app.p25 else []
        return out

    def _state(self) -> None:
        client = self._client(10.0)
        backoff = Backoff(first=1.0, fast_cap=3.0, cap=5.0)
        last_status = last_tg = 0.0
        tg_sig = None
        while not self._stop.is_set():
            # `at` and the wall clock's step count from one look (wallclock.py): a post right after a step carries
            # the new count, and a stalled 1 s sampler never hides a step between posts (its save: left to the tick)
            at, steps = self.wall.sample(save=False)
            now = time.time() if at is None else at
            talking = self.app.live.state()
            paging = self.app.paging_state()            # a page going out now (a hub before #63 ignores it)
            body: dict = {"at": now, "boot_id": self.wall.post_boot_id, "wall_steps": steps,
                          "talking": talking, "paging": paging}
            if self._status_now or now - last_status >= STATUS_S:
                tgs = self.app.tg.state() if self.app.p25 else []
                sig = [(r["tg"], r["record"], r["main"], r.get("system"), r.get("tier")) for r in tgs]
                send_tg = sig != tg_sig or now - last_tg >= TALKGROUPS_S
                body["status"] = self.status(talkgroups=False)
                if send_tg:
                    body["status"]["talkgroups"] = tgs
            try:
                r = client.request("POST", "/api/station/state", body, timeout=10.0)
                self._ok()
                self.apply_answer(r)
                self._left_out(r, "status" in body)
                left_out = r.get("refused") if isinstance(r, dict) and isinstance(r.get("refused"), list) else []
                if "status" in body:
                    last_status, self._status_now = now, False
                    if "talkgroups" in body["status"] and "status" not in left_out:   # (left out: sent again next)
                        last_tg, tg_sig = now, [(r["tg"], r["record"], r["main"], r.get("system"), r.get("tier"))
                                                for r in body["status"]["talkgroups"]]
                backoff.ok()
            except HubError as e:
                self._fail(e)
                self._stop.wait(backoff.fail(e))
            self._live_wake.wait(LIVE_REFRESH_S if talking or paging else STATUS_S)
            self._live_wake.clear()
            self._stop.wait(0.05)           # a burst of changes goes up as one update

    # -- jobs from the hub ------------------------------------------------------------------------------

    def _jobs(self) -> None:
        client = self._client(JOB_WAIT_S + 15.0)
        backoff = Backoff(first=1.0, fast_cap=5.0, cap=30.0)
        while not self._stop.is_set():
            try:
                r = client.request("GET", f"/api/station/jobs?wait={JOB_WAIT_S}", timeout=JOB_WAIT_S + 15.0)
            except HubError as e:
                self._fail(e)
                self._stop.wait(backoff.fail(e))
                continue
            backoff.ok()
            if not self._stop.is_set():
                self._ok()
            jobs = r.get("jobs") if isinstance(r, dict) else None
            if jobs is not None and not isinstance(jobs, list):
                self._wrong("jobs")                 # (a list of objects: none taken from this answer)
                jobs = None
            if isinstance(jobs, list):
                self._good("jobs")
                if all(isinstance(j, dict) for j in jobs):
                    self._good("jobs[]")
            for job in jobs or []:                  # handed to this station: run, even if just turned off
                if not isinstance(job, dict):
                    self._wrong("jobs[]")
                    continue
                threading.Thread(target=self._run_job, args=(job,), name="hub-job", daemon=True).start()

    def _run_job(self, job: dict) -> None:
        try:
            kind = job.get("type")
            if kind == "tts":
                res = self._tts(job.get("args") or {})
            elif kind == "call":
                res = self._call(job.get("args") or {})
            elif kind == "sources_reset":               # an admin on the hub: an incident source's reset() (#301)
                a = job.get("args")
                if not isinstance(a, dict) or not isinstance(a.get("source"), str):
                    # ("" is every source, asked for by name: a reset is never taken from args that say nothing)
                    res = {"ok": False, "error": "sources_reset: args: {source, by} (source \"\" for every source)"}
                else:
                    res = request_reset(getattr(self.app, "sources_runner", None), a["source"],
                                        f"{a.get('by') or 'an admin'} on the hub", self.log, origin="hub")
            else:
                res = {"ok": False, "error": f"this station doesn't know job {kind!r}"}
        except Exception as e:  # a bad job must never take the station down
            res = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
        client = self._client(30.0)
        for attempt in range(3):
            try:
                client.request("POST", f"/api/station/jobs/{job.get('id')}", res, timeout=30.0)
                return
            except HubError as e:
                if 400 <= e.status < 500:
                    return                    # the hub gave up on it (timed out) or refused it
                self._stop.wait(1.0 + attempt)
            finally:
                client.close()

    def _tts(self, a: dict) -> dict:
        """A dispatch said by this station's voice (Kokoro here when the station has it, `say` on a Mac as its fallback; the deployed station has no local TTS), for the hub."""
        text, v = a.get("text"), a.get("voice") or {}
        if self.cfg.dispatch_tts_engine == "none":
            return {"ok": False, "error": "station has no voice"}
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            return {"ok": False, "error": "no dispatch text"}
        voice = VoiceCfg(str(v.get("engine") or "kokoro"), str(v.get("voice") or ""), float(v.get("speed") or 1.0),
                         self.cfg.dispatch_tts_port, str(v.get("fallback_voice") or "Samantha"),
                         int(v.get("rate_wpm") or 0))
        spoken = a.get("spoken") if isinstance(a.get("spoken"), dict) else {}
        repeat = min(3, max(1, int(a.get("repeat") or 2)))
        try:
            with tempfile.TemporaryDirectory(prefix="sizeup-hub-tts-") as d:
                path, label = dispatcher.render_dispatch(text, a.get("tones") or [], d, voice, repeat,
                                                         {str(k): str(x) for k, x in spoken.items()}, log=self.log)
                wav = Path(path).read_bytes()
        except AudioUnavailable as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "label": label, "audio": base64.b64encode(wav).decode()}

    def _call(self, a: dict) -> dict:
        """One of CALLS. Its arguments by name or by position (call_args); a call without them (absent or null) is
        refused, never run on the function's defaults."""
        fn = a.get("fn")
        if fn not in CALLS:
            return {"ok": False, "error": f"not allowed from the hub: {fn!r}"}
        args = a.get("args")
        if args is None:
            return {"ok": False, "error": f"{fn}: no args (a call never runs on its defaults)"}
        f = getattr(self.app, fn)
        given = call_args(fn, f, args)
        if isinstance(given, str):
            return {"ok": False, "error": given}
        if given is None:
            result = f(*args) if isinstance(args, list) else f(**args)
        else:
            result = f(**given)
        self._status_now = True             # the switch shows on the hub right away
        self._live_wake.set()
        return {"ok": True, "result": result}
