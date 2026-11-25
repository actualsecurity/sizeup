"""[cloud] role = "hub": the web UI and the CAD in the cloud, fed by the station. No radios, no
trunk-recorder, no whisper, no voice server: nothing here opens an SDR or starts a program, so it
runs on a bare Linux box with Python + numpy.

    POST /api/station/events      {epoch, station, events: [{seq, at, type, data, audio?}]} -> {acked, rejected?}
                                  applied in order through the same App code as on the station
                                  (_page_heard, _transcribed, _retext, the radio registry); each
                                  event once: the hub remembers the last seq per outbox epoch
                                  (hub.sqlite), and pages / transmissions are keyed by their ids.
                                  An event it can't parse or apply is skipped and listed in "rejected"
                                  ({i, seq, type, reason, error}); a batch it can't read at all is 413 / 400
                                  {error, reason}: the station splits it and quarantines the event (_events)
    POST /api/station/stt         each clip's speech-to-text input, gzipped (sizeup/cloud/sttinput.py): {v, station,
                                  items: [{n, id, kind, ..., stt}]} -> {ok, stored: [n], refused: [n]}: kept as clip
                                  "<id>.src" beside the clip (before or after its event); read_stt_audio reads it.
                                  Its own lane: never through the events' cursor
    POST /api/station/state       {at, boot_id?, wall_steps?, talking?, paging?, status?}: who is talking
                                  now and a page going out now (latest wins; paging: sizeup/radio/livepage.py) and the
                                  station's radio / SDR / STT / voice rows, talkgroups, re-run
                                  progress, outbox size, its version -> {ok, build, caps, station, services}:
                                  the hub's own build (SIZEUP_BUILD), which the station follows (autoupdate.py),
                                  and the id it took the post as, which the station adopts (stations.py);
                                  services: the admins' service per source (sourcekeys.set_service_overrides)
    GET  /api/station/jobs?wait=25   long poll: jobs for the station -> {jobs: [{id, type, args}]}
    POST /api/station/jobs/<id>   the station's answer {ok, error?, result?, label?, audio?}
                                  (jobs: tts, call, sources_reset: an incident source's reset(), #301)
    POST /api/station/aircraft    our ADS-B receiver on the station (#235, sizeup/cloud/aircraftlink.py): {v, epoch, seq,
                                  full, ac: [[hex, lat, lon, alt, gs, trk, vr, squawk, emergency, flight, seen_pos, seen,
                                  rssi, ...]], gone: [hex], rx?}, gzipped, every 1 s while someone looks at the map (or an
                                  emergency is on), else every 5 s; only what changed -> {ok, full, every}. Into the
                                  aircraft layer's "receiver" feed (sizeup/aircraft.py StationFeed), never the state push
    POST /api/station/vessels     our AIS receiver on the station (#402, sizeup/cloud/vessellink.py): {v, at, vessels:
                                  [vessel records], rx?}, gzipped, the whole list every 5 s while someone looks (or a
                                  marine call is open), else every 30 s -> {ok, n, every}. Into the Vessels layer's
                                  "receiver" feed (sizeup/vessels.py StationFeed), never the state push
    POST /api/station/logs        the station's journal, scrubbed, gzipped (sizeup/cloud/logship.py): {v, station, batch,
                                  lines: [[ms, unit, level, msg]]} -> {ok, accepted, config, retry_s?}: into CloudWatch
                                  Logs /sizeup/station (sizeup/cloud/cwlogs.py), or "not configured" until sizeup-aws
                                  step 25 has run (the station keeps its spool)
    POST /api/station/metrics     the station's signal metrics (sizeup/cloud/metrics.py), gzipped: {v, station,
                                  records: [{k, s, at, ...}]} -> {ok, accepted, skipped, resend_from?}: into
                                  metrics.sqlite (sizeup/cloud/metricstore.py), keyed by the authenticated station id.
                                  Taken from any station (like its logs), never through the events' cursor, never
                                  "seen" (a second station's metrics don't make the station look connected)
    GET  /api/station/hello      {ok}: the token works (the station's Test connection; not counted as "seen")
    /api/station/stt/...         the GPU speech-to-text workers (#639, sizeup/cloud/sttworkers.py): their own tokens
                                 (sw1...), never a station's; claim / audio / extend / complete / release
    GET  /api/station/status     read only, for the deploy check (tools/deploy_check.py): the hub's build, the
                                 station's version and last update, the health alerts (sizeup/cloud/health.py).
                                 The station token or the status token, which opens nothing else; not "seen"

Every request needs `Authorization: Bearer <token>`: a station's own token (sz1.<id>.<secret>) or the shared
SIZEUP_STATION_TOKEN, which is one station (sizeup/cloud/stations.py; docs/cloud.md "How station authentication
works"). _principal turns it into the station id it proves before any body is read, and the id a payload names must be
that one (STATION_ROUTES: every route here). With no token configured and no station added the hub refuses them all.
Jobs (the station only connects out):
    tts    the dispatch voice when the station makes it: [dispatch] tts_engine = "polly" (the
           default here) is rendered on the hub itself (sizeup/polly.py) and comes here only when
           Polly fails; any other engine always. Cad._render_remote -> render_voice() waits up to
           20 s for the station to render it with its Kokoro; offline or too slow -> text only,
           said so ("Voice unavailable — station offline"), never a hung request
    call   set_mode / set_talkgroup / set_talkgroup_group / retranscribe from this UI, run there;
           emergency_all_clear: an alarm's All clear, closing the station's emergency window (#128);
           set_channel_prefs: Settings > Radios > Channels (record / transcribe / engine), the tone pairs
           named from the console (#80), and whether
           Amazon Transcribe can take audio now, sent whenever the station's reported copy (its
           state's channels.sig) differs, so a station that was offline catches up (channels_check)
Amazon Transcribe ([stt] aws, sizeup/transcribe.py) runs here: a "traffic" / "page" event with
transcript_pending "aws" (the station skipped whisper) is shown as "Transcribing" and gets AWS's text
(_aws_start -> _retext); "both" gets AWS's text with the station's kept as alt_text. If AWS can't, the
station's whisper is asked for it (retranscribe), else it says "transcription failed".
Never left "Transcribing" (#195): an AWS job past its deadline fails as "AWS timed out" (sizeup/transcribe.py's
watchdog) and goes the same way, but only on pages, Fire/EMS 15, the firegrounds and the airband: a clip on any other channel
that timed out waits for AWS's late answer instead, which becomes its text (#300: a slow AWS never floods the
station's whisper); a late AWS answer after the whisper fallback is kept as alt_text (App._aws_late). Every SWEEP_S the
hub also looks for anything still pending "aws" (_aws_sweep): past its deadline + SWEEP_GRACE_S with no job
answering for it (a job lost, a thread gone) it is failed over too (the station's whisper, at live priority:
transcribe_now), and one the station's whisper was asked for that has no text FALLBACK_WAIT_S later says
"transcription failed" (flagged: its text still comes up if it arrives); a page waiting for the station's own
whisper is flagged the same way after PENDING_FLAG_S. After a restart (_aws_recover) what was waiting goes to
AWS again (less than RECOVER_S old) or to the station's whisper (waiting up to RECOVER_WAIT_S for the station
to reconnect). While AWS keeps failing its breaker is open, and past the monthly limit only the reserve
channels (pages, Fire/EMS 15) keep it: aws_ok() says so, the station is told (channels_check), and it runs
whisper on new clips itself. When AWS can take audio again (a new month, a higher limit, the breaker
closed) what is still waiting or failed from the last RESEND_S on an AWS channel goes to it (_aws_resend).

The monitor's state lives in hub.sqlite and the clips in audio.sqlite (hubstore.py), next to the
CAD's cad.sqlite: all of it under one state dir that Litestream replicates (deploy/hub/).
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import os
import re
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import replace
from pathlib import Path
from typing import Callable

from sizeup import channels, sourcekeys, stt_quality, stt_terms
from sizeup import tz
from sizeup import app as app_mod
from sizeup import fence as fence_mod
from sizeup.app import (AIRBAND_FIRST, CLIP_ID, RENAMED, STT_FIRST, WHISPER_FALLBACK_STATION, App, AwsJob, channels_model,
                        traffic_key)
from sizeup.cad.dispatcher import AudioUnavailable
from sizeup.cad.radios import OTHER_P25, fm_system
from sizeup.audiostore import STT_SRC
from sizeup.cloud import client as wire, statusshape, sttinput
from sizeup.cloud.autoupdate import describe
from sizeup.cloud.health import StationHealth, status_token
from sizeup.cloud.hubstore import AudioDb, MonitorDb
from sizeup.cloud.stations import Principal, Refused, StationRegistry
from sizeup.cloud.sttworkers import API as STT_API, STATION_PREFIX, WorkerApi, WorkerRegistry
from sizeup.incidents import Traffic
from sizeup.radio.page import page_from_dict
from sizeup.sources import SourceError
from sizeup import stt_time, sttpool, sttruns
from sizeup.cloud import infer as infer_mod
from sizeup.stt import CATCHUP, CATCHUP_NOTE, judge, wav_bytes
from sizeup.transcribe import RESERVE_CHANNELS, deadline_for

JSON = "application/json"
MAX_EVENTS_BODY = 32 * 1024 * 1024
MAX_STATE_BODY = 4 * 1024 * 1024
MAX_RESULT_BODY = 16 * 1024 * 1024
MAX_AIRCRAFT_BODY = 512 * 1024        # 300 aircraft as JSON are ~30 KB; gzipped, a few
MAX_VESSELS_BODY = 512 * 1024         # 500 vessel records as JSON are ~150 KB; gzipped, far less
MAX_LOGS_BODY = 4 * 1024 * 1024       # a station log batch, gzipped (sizeup/cloud/logship.py: ~512 KB before gzip)
MAX_METRICS_BODY = 2 * 1024 * 1024    # a metrics batch, gzipped (sizeup/cloud/metrics.py: 2000 records, ~250 KB before gzip)
MAX_METRICS_JSON = 4 * 1024 * 1024    # ... and unpacked
MAX_STT_BODY = 12 * 1024 * 1024       # STT inputs, gzipped (the station sends ~256 KB of FLAC a post; one may be 8 MB)
MAX_STT_JSON = 16 * 1024 * 1024       # ... and unpacked (base64)
STT_WAIT_MAX_S = 30.0                 # read_stt_audio's wait_s at most this
LIVE_STALE_S = 8.0       # a "talking now" snapshot older than this is dropped (the station resends every 2 s)
TTS_WAIT_S = 20.0        # the dispatch voice: the station renders it; longer than this = text only
CALL_WAIT_S = 12.0       # an SDR / talkgroup switch (the monitor's switch gives up at 15 s)
SWEEP_S = 15.0           # Amazon Transcribe (#195): how often the hub looks for what is still "Transcribing" ...
SWEEP_GRACE_S = 15.0     # ... failed over this long past its job's own deadline (the job's watchdog goes first)
FALLBACK_WAIT_S = 120.0  # the station's whisper asked and no text this long after: flagged "transcription failed"
PENDING_FLAG_S = 120.0   # a page waiting this long for the station's own whisper: flagged the same way
RESEND_S = 6 * 3600.0    # AWS back (a new month, a higher limit): what failed or waits from this far back goes to it
RESEND_MAX = 200         # ... at most this many, the newest first
RECOVER_S = 3600.0       # after a restart: what waited for AWS less than this goes to AWS again, older to whisper
RECOVER_WAIT_S = 120.0   # ... to whisper: waiting this long for the station to reconnect first
CATCHUP_STATUS_S = 30.0  # shed clips caught up: the station's whisper judged idle only on a report this fresh
POOL_CAP = "stt_pool"    # the hub takes clips a station hands to the pool (#639 phase 2: transcript_pending "pool")
POOL_AUDIO_WAIT_S = 10.0  # a pool job's audio: the station's STT input waited for this long before the listening clip
CATCHUP_REFUSED_S = 600.0  # ... and a station that refuses the call (an older build) isn't asked again for this long
JOB_PATH = re.compile(r"/api/station/jobs/([0-9a-f]{16})")
# Every route Hub.handle serves, each behind _principal (tests/test_station_identity.py fails on one not listed here)
STATION_ROUTES = (("GET", "/api/station/status"), ("GET", "/api/station/hello"), ("GET", "/api/station/jobs"),
                  ("POST", "/api/station/events"), ("POST", "/api/station/state"), ("POST", "/api/station/aircraft"),
                  ("POST", "/api/station/vessels"), ("POST", "/api/station/logs"), ("POST", "/api/station/metrics"),
                  ("POST", "/api/station/stt"),
                  ("POST", "/api/station/jobs/0123456789abcdef"))
REFUSAL_LOG_S = 3600.0   # a refused station request: logged once per (reason, station) this often
MISMATCH_LOG_S = 60.0    # ... an id_mismatch this often (a live station locked out: its log says who it says it is)
TOKEN_ID = re.compile(r"sz1\.([a-z0-9][a-z0-9_-]{0,39})\.")    # the station id a refused own token names (for the log)
EXTRA = re.compile(r"[a-z][a-z0-9_]{0,30}")      # a transmission's field names, as the station sends them


LANDED = ("done", "no_speech", stt_quality.UNCLEAR)    # a transcript is in (unclear: its words kept apart, its text "")
UNCLEAR_TRIED_MAX = 5000      # clips an unclear transcript was asked again of another engine for, remembered (once each)


class StationOffline(RuntimeError):
    pass


def _part(status, key: str, kind: type = dict):
    """The station's status' `key` when the status is a dict and the value a `kind`, else None: a reader never gets a
    value of another type (each status is checked as it comes, sizeup/cloud/statusshape.py; this keeps the readers
    safe from a value stored otherwise)."""
    v = status.get(key) if isinstance(status, dict) else None
    return v if isinstance(v, kind) else None


def _json(code: int, obj) -> tuple[int, bytes, str]:
    return code, wire.dumps(obj, default=None), JSON      # strict JSON: a number that isn't finite goes as null


def _hhmm(t: float | None) -> str:
    return tz.hhmm(t)          # the console's time zone, not the container's UTC (#200)


class Jobs:
    """Hub -> station work, handed out on the station's long poll, once each. A job nobody picked
    up before it expires is dropped (a station back after an hour doesn't replay old clicks)."""

    def __init__(self):
        self._cond = threading.Condition()
        self._jobs: OrderedDict[str, dict] = OrderedDict()
        self.closed = False             # the hub is draining (a deploy): the long polls return now, empty

    def put(self, kind: str, args: dict, ttl: float) -> dict:
        job = {"id": secrets.token_hex(8), "type": kind, "args": args, "expires": time.time() + ttl,
               "taken": False, "done": threading.Event(), "result": None}
        with self._cond:
            self._jobs[job["id"]] = job
            self._cond.notify_all()
        return job

    def _open(self) -> list[dict]:
        now = time.time()
        for jid in [j for j, x in self._jobs.items() if x["expires"] < now]:
            del self._jobs[jid]
        return [x for x in self._jobs.values() if not x["taken"]]

    def close(self) -> None:
        """Draining: every long poll returns at once with nothing; nothing more is handed out (a job waiting
        here is the next task's to give: the clicker's request fails and is tried again)."""
        with self._cond:
            self.closed = True
            self._cond.notify_all()

    def take(self, wait: float) -> list[dict]:
        with self._cond:
            self._cond.wait_for(lambda: self.closed or bool(self._open()), timeout=wait)
            if self.closed:
                return []
            out = self._open()
            for x in out:
                x["taken"] = True
            return [{"id": x["id"], "type": x["type"], "args": x["args"]} for x in out]

    def finish(self, jid: str, result: dict) -> bool:
        with self._cond:
            job = self._jobs.pop(jid, None)
        if job is None:
            return False
        job["result"] = result
        job["done"].set()
        return True

    def cancel(self, job: dict) -> None:
        with self._cond:
            self._jobs.pop(job["id"], None)

    def pending(self) -> int:
        with self._cond:
            return len(self._open())


def _until(e: dict) -> float:
    """An ended talker's / page's `until` (0: gone now): never a value of another type (statusshape.live_problem)."""
    u = e.get("until")
    return float(u) if isinstance(u, (int, float)) and not isinstance(u, bool) and math.isfinite(u) else 0.0


class RemoteLive:
    """The station's "talking now", standing in for LiveTalk on the hub. Rigs are looked up again
    in the hub's own radio registry (the one people edit here)."""

    def __init__(self, resolve: Callable[[str, str, str], dict | None], clock: Callable[[], float] = time.time):
        self.resolve, self.clock = resolve, clock
        self.on_change: Callable[[], None] = lambda: None
        self._lock = threading.Lock()
        self._entries: list[dict] = []
        self._paging: list[dict] = []
        self._at = 0.0

    def update(self, entries: list, at: float | None = None, paging: list | None = None) -> None:
        """A snapshot: who is talking, and (paging) the pages going out now; a station before #63 sends
        none, which is the same as none going out."""
        clean = [dict(e) for e in entries if isinstance(e, dict)][:50]
        pages = [dict(p) for p in (paging or []) if isinstance(p, dict) and isinstance(p.get("pairs"), list)][:8]
        with self._lock:
            changed = clean != self._entries or pages != self._paging
            self._entries, self._paging, self._at = clean, pages, self.clock() if at is None else at
        if changed:
            self.on_change()

    def paging_state(self) -> list[dict]:
        """The station's pages going out now (a just-finished one until its `until`); [] once the
        snapshot is older than LIVE_STALE_S, like the talkers."""
        now = self.clock()
        with self._lock:
            if now - self._at > LIVE_STALE_S:
                return []
            return [dict(p) for p in self._paging if not (p.get("ended") and _until(p) <= now)]

    def state(self) -> list[dict]:
        now = self.clock()
        with self._lock:
            if now - self._at > LIVE_STALE_S:
                return []
            entries = [dict(e) for e in self._entries]
        out = []
        for e in entries:
            if e.get("ended") and _until(e) <= now:
                continue
            rid = str(e.get("radio_id") or "")
            if rid:
                try:
                    r = self.resolve(fm_system(rid) if e.get("system") == "fm" else "p25", rid, str(e.get("alias") or ""))
                except Exception:  # the registry must never break the indicator
                    r = None
                e["rig"], e["label"] = (r or {}).get("unit", e.get("rig", "")), (r or {}).get("label", e.get("label", ""))
            out.append(e)
        return out


class Hub:
    def __init__(self, app: HubApp, cfg, log: Callable[[str], None] = print):
        self.app, self.log = app, log
        self.token = cfg.cloud_token
        self.offline_s = cfg.cloud_offline_s
        self.own_voice = cfg.dispatch_tts_engine == "polly"   # the dispatch voice is the hub's (Polly), not the station's
        self.started = time.time()
        self.build = os.environ.get("SIZEUP_BUILD", "").strip()   # the git sha CI built this image from
        self.jobs = Jobs()
        self.live = RemoteLive(app.radios.resolve)
        self.last_seen: float | None = None
        self.last_state: float | None = None     # the station's last state post (station health: "silent")
        self.status_at: float | None = None      # ... the last one with its status (the catch-up's word on whisper)
        self.status_token = status_token(self.token, getattr(cfg, "cloud_status_token", ""))
        # who each request is (sizeup/cloud/stations.py): stations' own tokens (hub.sqlite, made on first use) and the
        # shared token above as one station
        self.stations = StationRegistry(app.mdb, self.token, getattr(cfg, "cloud_status_token", ""),
                                        getattr(cfg, "cloud_legacy_station_id", ""), log=log)
        self.stations.on_rename = self._station_renamed
        self._refused_said: dict[tuple[str, str], float] = {}
        # the GPU speech-to-text workers' own tokens (#639, sizeup/cloud/sttworkers.py; hub.sqlite, made on first use)
        self.workers = WorkerRegistry(app.mdb, log=log)
        self.health_: StationHealth | None = None  # station health and alerts (HubApp sets it)
        self.polling = 0
        self.status: dict | None = None          # the station's last report (state "status")
        self.status_refused: dict | None = None  # the latest status left out for its shape ({at, why, n, since})
        self.live_refused: dict | None = None    # ... and talker / page going out
        self.talkgroups: list[dict] = []
        self.skew = 0.0                          # hub clock - station clock (s)
        self.errors, self.last_error = 0, ""
        self._lock = threading.Lock()
        self._ingest = threading.Lock()
        self.epoch = app.mdb.get("station_epoch")
        self.seq = int(app.mdb.get("station_seq", "0") or 0)
        self._failed: tuple[str, int, int] = ("", 0, 0)   # (epoch, seq, tries) of a source_event that failed to apply
        # the events it couldn't apply lately, {(epoch, seq): {seq, type, reason, error}}: listed again on a retry
        self._rejections: OrderedDict[tuple[str, int], dict] = OrderedDict()
        self._wrong_results: set[tuple[str, str]] = set()     # (job, key) said once (_wrong_result)

    # -- health --------------------------------------------------------------------------------------

    def connected(self) -> bool:
        return self.polling > 0 or (self.last_seen is not None and time.time() - self.last_seen < self.offline_s)

    def _seen(self) -> None:
        self.last_seen = time.time()

    def health(self) -> dict:
        """{state, word, detail}: the "Station" row and the console's header pill."""
        now = time.time()
        name = _part(self.status, "name", str) or "the station"
        reg = getattr(self, "stations", None)
        if not (reg.ingest_on() if reg is not None else self.token):
            return {"state": "down", "word": "no token",
                    "detail": "no station token configured on the hub (SIZEUP_STATION_TOKEN): the station can't connect"}
        if self.connected():
            ago = 0 if self.polling else int(now - (self.last_seen or now))
            q = (_part(self.status, "outbox") or {}).get("pending")
            q = q if isinstance(q, int) and not isinstance(q, bool) and q > 0 else 0
            # the seconds only once they matter (as the word does): a count ticking every second between two
            # polls would change the pushed state every second for nothing (#146)
            detail = (f"{name}: connected" + (f", last seen {ago} s ago" if ago >= 5 else "")
                      + (f" · {q} event(s) still on their way up" if q else "")
                      + (f" · clocks differ by {self.skew:+.0f} s (set both to network time)" if abs(self.skew) > 5 else "")
                      + (f" · {self.errors} event(s) failed to apply, last: {self.last_error}" if self.errors else "")
                      + (f" · refused (multi-station isn't on yet): {', '.join(sorted(others))}"
                         if reg is not None and (others := reg.others()) else ""))
            v = self.version()
            word = f"{ago} s" if ago >= 5 else ""
            if v is not None:
                detail += f" · version: {v['text']}"
                if not word and v["state"] in ("updating", "failed"):
                    word = "updating" if v["state"] == "updating" else "update failed"
            return {"state": "ok", "word": word, "detail": detail}
        if self.last_seen is None:
            return {"state": "down", "word": "waiting",
                    "detail": f"no station has connected since the hub started ({_hhmm(self.started)})"}
        since = self.last_seen
        return {"state": "down", "word": f"offline {_hhmm(since)}",
                "detail": f"{name}: offline since {tz.hhmmss(since)} "
                          f"(no contact for {int((now - since) // 60)} min): radios"
                          + (" and speech-to-text are" if self.own_voice else ", speech-to-text and the dispatch voice are")
                          + " unavailable until it's back; what it heard meanwhile comes up then"}

    def version(self) -> dict | None:
        """{state, text}: the station's version against this hub's build ("up to date", "updating",
        "update to abc1234 failed ..."), from what the station last reported; None from a station
        that doesn't report one."""
        v = _part(self.status, "version")
        if v is None:
            return None
        try:
            return describe(str(v.get("build") or ""), self.build, v.get("update") if isinstance(v.get("update"), dict) else {})
        except Exception as e:  # noqa: BLE001 - a report: never in the way of the routes that show it
            return {"state": "unknown", "text": f"the station's version not read ({type(e).__name__})"}

    def station_row(self) -> dict:
        """The Station row of the radios (everyone's state push): its connection only. Its health alerts are the
        admins' (Settings > Station > Health, GET /api/cad/health, #203); the page links the row there for an admin."""
        h = self.health()
        return {"label": "Station", "mhz": None, "state": h["state"], "word": h["word"],
                "detail": h["detail"] + " · Station health: Settings > Station > Health (admins)",
                "open": False, "quieting_db": None, "restarts": 0, "last_heard": self.last_seen,
                "link": "#settings/station/health"}

    def radio_rows(self) -> list[dict]:
        """The Station row, then the station's own rows as it reported them, or, offline, marked
        unknown with what they last said (never a stale "ok")."""
        rows = [self.station_row()]
        on = self.connected()
        for r in _part(self.status, "radios", list) or []:
            if not isinstance(r, dict):
                continue
            r = dict(r)
            if not on:
                r.update(state="unknown", open=False, quieting_db=None, word="",
                         detail=f"station offline since {_hhmm(self.last_seen)}; last report: "
                                f"{r.get('state', '?')}: {r.get('detail') or ''}".rstrip(": "))
            rows.append(r)
        return rows

    def sdr(self) -> dict | None:
        """The station's SDR switches, only while it is there to switch them."""
        return _part(self.status, "sdr") if self.connected() else None

    def rerun(self) -> dict:
        r = _part(self.status, "rerun") if self.connected() else None
        return dict(r) if isinstance(r, dict) else {"total": 0, "done": 0, "changed": 0, "failed": 0, "pending": 0}

    def cad_cloud(self) -> dict:
        """The CAD state's cloud (everyone's state push): the station's connection, for the header pill. No health
        alerts here (#203): those are on the admins' Settings > Station > Health, from GET /api/cad/health."""
        h = self.health()
        return {"role": "hub", "state": h["state"], "word": h["word"], "detail": h["detail"],
                "health_url": "/cad#settings/station/health"}

    # -- jobs ------------------------------------------------------------------------------------------

    def call(self, kind: str, args: dict, timeout: float) -> dict:
        """Run a job on the station and wait for its answer. StationOffline when it isn't connected
        (at once: no waiting on a station that isn't there) or didn't answer in time."""
        if not self.stations.ingest_on():
            raise StationOffline("no station token configured on the hub")
        if not self.connected():
            raise StationOffline(f"station offline since {_hhmm(self.last_seen)}" if self.last_seen
                                 else "no station connected")
        job = self.jobs.put(kind, args, ttl=timeout)
        if not job["done"].wait(timeout):
            self.jobs.cancel(job)
            raise StationOffline(f"the station didn't answer in {timeout:g} s")
        res = job["result"] or {}
        if not res.get("ok"):
            raise RuntimeError(str(res.get("error") or "the station couldn't do it"))
        return res

    def reset_source(self, source: str, by: str = "") -> dict:
        """POST /api/sources/reset (an admin, Settings > Station > Health): the station's incident source's reset() (#301), as a
        sources_reset job over the station's authenticated jobs channel. Logged with who asked (their Access
        identity; never a credential). ValueError for a bad source, StationOffline / RuntimeError from the station."""
        from sizeup.cloud.adapters import SOURCE_ID
        source = str(source or "").strip()
        if source and not SOURCE_ID.fullmatch(source):
            raise ValueError("source: a source id")
        self.log(f"incident sources: reset of {source or 'all'} asked by {by or 'someone (no sign-in)'}")
        res = self.call("sources_reset", {"source": source, "by": by}, 30.0)
        self.log(f"incident sources: reset of {source or 'all'}: {str(res.get('result') or 'done')[:200]}")
        return {"ok": True, "result": str(res.get("result") or "")[:300]}

    def render_voice(self, args: dict) -> tuple[bytes, str]:
        """Cad.remote_voice: the dispatch said by the station's voice, or AudioUnavailable (text only)."""
        try:
            res = self.call("tts", args, TTS_WAIT_S)
        except StationOffline as e:
            raise AudioUnavailable(f"Voice unavailable — {e}: text only") from None
        except RuntimeError as e:
            raise AudioUnavailable(f"Voice unavailable — station: {e}: text only") from None
        audio, label = res.get("audio"), res.get("label")
        if not isinstance(audio, (str, type(None))) or not isinstance(label, (str, type(None))):
            self._wrong_result("tts", "audio" if not isinstance(audio, (str, type(None))) else "label")
        try:
            wav = base64.b64decode(audio if isinstance(audio, str) else "", validate=True)
        except (binascii.Error, ValueError):
            wav = b""
        if wav[:4] != b"RIFF":
            raise AudioUnavailable("Voice unavailable — the station sent no audio: text only")
        return wav, (label if isinstance(label, str) and label else "Voice: rendered by the station")

    def _wrong_result(self, job: str, key: str) -> None:
        """A job's result with a wrong type where its caller reads it: taken as a station that sent nothing there. Said
        once per job and key."""
        if (job, key) not in self._wrong_results:
            self._wrong_results.add((job, key))
            self.log(f"hub: the station's {job} result has a wrong type in {key!r}: taken as nothing sent")

    def dismiss_rejected(self, kind: str, by: str = "") -> dict:
        """POST /api/cad/health/dismiss (an admin, Settings > Station > Health): the station drops its stuck and late
        quarantined events of this type (sizeup/cloud/outbox.py dismiss, by a quarantine_dismiss call), so their alert
        clears. Logged and audited (settings_audit, System) with who asked. ValueError for a bad type, StationOffline /
        RuntimeError from the station."""
        kind = str(kind or "").strip()
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", kind):
            raise ValueError("kind: an event type")
        n = int(self.remote("quarantine_dismiss", kind) or 0)
        who = by or "someone (no sign-in)"
        self.log(f"hub: the station's refused {kind} event(s) dismissed by {who}: {n}")
        from sizeup.cad.radios import audit
        store = self.app.cad.store
        with store.tx() as db:
            audit(db, time.time(), by, "system", f"health_dismiss:{kind}", "stuck / late on the station", f"dismissed ({n})")
        return {"ok": True, "dismissed": n}

    def remote(self, fn: str, *args, timeout: float = CALL_WAIT_S):
        """One of the station's switches (StationOffline / RuntimeError with the reason)."""
        return self.call("call", {"fn": fn, "args": list(args)}, timeout).get("result")

    # -- HTTP --------------------------------------------------------------------------------------------

    def _principal(self, headers, read_only: bool = False) -> Principal | tuple[int, bytes, str]:
        """Who this request is (sizeup/cloud/stations.py), from its bearer alone, before any body is read; else the
        refusal: 401 {error, reason} (logged once per reason and station, never the token), or 403 with no token
        configured and no station added. read_only: GET /api/station/status, where a status token counts too."""
        if not self.stations.ingest_on():
            return _json(403, {"error": "station ingest is off: no station token configured on the hub "
                                        "(SIZEUP_STATION_TOKEN) and no station added (Settings > Station > Stations)",
                               "reason": "ingest_off"})
        h = (headers.get("Authorization") or "").strip()
        given = h[7:].strip() if h[:7].lower() == "bearer " else ""
        try:
            return self.stations.resolve(given, read_only)
        except Refused as e:
            m = TOKEN_ID.match(given)
            return self._refuse(e, m.group(1) if m and self.stations.row(m.group(1)) is not None else "")

    def _refuse(self, e: Refused, sid: str = "") -> tuple[int, bytes, str]:
        """The 401: {error, reason} (fixed text, never the token); in the log once per (reason, station) an hour, an
        id_mismatch once a minute with the two ids (Health's "Station rejected" too: StationRegistry.mismatch)."""
        now = time.time()
        every = MISMATCH_LOG_S if e.reason == "id_mismatch" else REFUSAL_LOG_S
        if now - self._refused_said.get((e.reason, sid), 0.0) >= every:
            self._refused_said[(e.reason, sid)] = now
            m = self.stations.mismatch() if e.reason == "id_mismatch" else None
            self.log(f"hub: station request refused: {e.reason}" + (f" (station {sid})" if sid else "")
                     + (f": the station says {m['said'] or '?'}, the token is {m['token']}" if m else ""))
        return _json(401, {"error": str(e), "reason": e.reason})

    def _other_station(self, p: Principal) -> tuple[int, bytes, str]:
        """409 {error, reason: multi_station_off}: a station other than the one this hub takes (sizeup/cloud/stations.py
        primary), logged once an hour per station and shown on the Station row (Health)."""
        self.stations.refused_other(p.id)
        e = Refused("multi_station_off", f"it takes {self.stations.legacy_id()}, not {p.id}")
        now = time.time()
        if now - self._refused_said.get((e.reason, p.id), 0.0) >= REFUSAL_LOG_S:
            self._refused_said[(e.reason, p.id)] = now
            self.log(f"hub: station request refused: {e.reason} (station {p.id})")
        return _json(409, {"error": str(e), "reason": e.reason})

    def _claim(self, p: Principal, claimed) -> str:
        """The id this request acts as: the token's. Refused (id_mismatch) when the payload names another."""
        try:
            return self.stations.claim(p, claimed)
        except Refused as e:
            said = re.sub(r"[^A-Za-z0-9_.-]", "?", claimed[:40]) if isinstance(claimed, str) else ""
            if e.reason == "id_mismatch":
                self.stations.mismatched(said, p.id or "the shared one")
            raise Refused(e.reason, f"the token is {p.id or 'the shared one'}, the request says {said}") from None

    def _station_renamed(self, old: str, new: str) -> None:
        """The shared token's station renamed (StationRegistry.set_shared_id): what the hub keys by the id moves with
        it. Its status now (the next post says so too); the station's signal metrics in the background, never on the
        admin's request (HubApp.apply_station_renames: chunked, from the renames kept in hub.sqlite). Its CloudWatch
        streams are named per id and day: new ones get the new id, the old ones stay in the group the Logs view reads."""
        if isinstance(self.status, dict) and self.status.get("id") == old:
            self.status = dict(self.status, id=new)
        move = getattr(self.app, "apply_station_renames", None)
        if move is not None:
            threading.Thread(target=move, name="station-rename", daemon=True).start()

    def handle(self, method: str, path: str, query: str, headers, body: Callable[[int], bytes | None] | None):
        if path.startswith(STT_API):                 # a GPU worker (#639): its own token, never a station's; not "seen"
            return self.app.stt_api.handle(method, path, headers, body)
        if path == "/api/station/status":            # read only (the deploy check): not "seen", nothing changes
            p = self._principal(headers, read_only=True)
            if not isinstance(p, Principal):
                return p
            if method != "GET":
                return _json(405, {"error": "GET only"})
            return _json(200, self.health_.status() if self.health_ is not None else
                         {"ok": True, "hub_build": self.build, "station": None, "alerts": []})
        p = self._principal(headers)
        if not isinstance(p, Principal):
            return p
        if method == "GET" and path == "/api/station/hello":
            # the station's Test connection: the token works (not "seen"), and as which station
            return _json(200, {"ok": True, "station": p.id, "token": p.via})
        if not self.app._may_write_audio():
            # not the leader (#501): nothing the station sends is taken here, and no job is handed out whose result
            # couldn't be kept: 503 + Retry-After (sizeup/web/server.py); its outbox sends it again, to the leader
            fence_mod.refuse(f"{method} {path}")
        if path not in ("/api/station/logs", "/api/station/metrics") and p.id and p.id != self.stations.primary(p):
            # one station until multi-station (per-station state, cursors, jobs: design A2-A4): a second one would
            # reset the outbox cursor, overwrite the status and take the first one's jobs. Not "seen"; on Health
            return self._other_station(p)
        if path not in ("/api/station/logs", "/api/station/metrics"):
            # (its logs and metrics come from a thread of their own, and from any station: neither says the station's
            # link works, so a second station or a wedged primary never keeps "connected" up)
            self._seen()
        self.stations.touch(p)
        try:
            if method == "GET" and path == "/api/station/jobs":
                return self._jobs(query)
            if method == "POST" and path == "/api/station/events":
                return self._events(body(MAX_EVENTS_BODY), p)
            if method == "POST" and path == "/api/station/state":
                return self._state(body(MAX_STATE_BODY), p)
            if method == "POST" and path == "/api/station/aircraft":
                return self._aircraft(body(MAX_AIRCRAFT_BODY), headers)
            if method == "POST" and path == "/api/station/vessels":
                return self._vessels(body(MAX_VESSELS_BODY), headers)
            if method == "POST" and path == "/api/station/logs":
                return self._logs(body(MAX_LOGS_BODY), headers, p)
            if method == "POST" and path == "/api/station/metrics":
                return self._metrics(body(MAX_METRICS_BODY), headers, p)
            if method == "POST" and path == "/api/station/stt":           # (sttinput.PATH)
                return self._stt_inputs(body(MAX_STT_BODY), headers, p)
            if method == "POST" and (m := JOB_PATH.fullmatch(path)):
                raw = body(MAX_RESULT_BODY)
                if raw is None:
                    return _json(413, {"error": "too large"})
                res = wire.loads(raw or b"{}")
                if not isinstance(res, dict):
                    return _json(400, {"error": "a JSON object"})
                return _json(200, {"ok": True}) if self.jobs.finish(m.group(1), res) else \
                    _json(404, {"error": "no such job (it timed out)"})
        except Refused as e:
            return self._refuse(e, p.id)
        except ValueError as e:
            return _json(400, {"error": f"bad request: {e}"})
        return _json(404, {"error": "not found"})

    def station_of(self, bearer: str) -> str | None:
        """The station id a station's own token (or the shared one) proves, None when it proves none: a station's
        whisper claims pool jobs as "station:<id>" (#639 phase 2). Never a status token."""
        try:
            p = self.stations.resolve(bearer)
        except Refused:
            return None
        return p.id if p.id and not p.read_only else None

    def _jobs(self, query: str) -> tuple[int, bytes, str]:
        from urllib.parse import parse_qs
        try:
            wait = min(30.0, max(0.0, float((parse_qs(query).get("wait") or ["0"])[0])))
        except ValueError:
            wait = 0.0
        with self._lock:
            self.polling += 1
        try:
            jobs = self.jobs.take(wait)
        finally:
            with self._lock:
                self.polling -= 1          # (not "seen" now: a poll that ran out on a dead connection proves nothing)
        return _json(200, {"jobs": jobs})

    def _state(self, raw: bytes | None, p: Principal | None = None) -> tuple[int, bytes, str]:
        """p: who sent it (handle passes it always; None only from a caller in this process, e.g. a test)."""
        if raw is None:
            return _json(413, {"error": "too large"})
        d = wire.loads(raw or b"{}")
        if not isinstance(d, dict):
            raise ValueError("a JSON object")
        claimed = d["status"].get("id") if isinstance(d.get("status"), dict) else None
        sid = self._claim(p, claimed) if p is not None else ""
        # What the hub can't read (sizeup/cloud/statusshape.py: a wrong-typed field stored broke every reader of it until
        # the next) is left out, never the post: a status of another shape is not taken (the last good one stays), a
        # talker / page going out of another shape is dropped (the others are taken). The heartbeat, the rest of the
        # post and the answer (its build: a station of any release still auto-updates) go on; the answer says what was
        # left out ("refused", "reason").
        who = sid or (p.id if p is not None else "")
        refused, why = [], []
        if d.get("status") is not None and (bad := statusshape.problem(d["status"])):
            self._refused("status", bad, who)
            refused.append("status")
            why.append(bad)
            tgs = d["status"].get("talkgroups") if isinstance(d["status"], dict) else None
            if isinstance(tgs, list) and statusshape.problem({"talkgroups": tgs}) is None:
                self._take_talkgroups(tgs)          # (they ride in the status: a good list is still taken)
            d["status"] = None
        talking, paging, bad = statusshape.live_clean(d.get("talking"), d.get("paging"))
        if bad:
            self._refused("live", bad, who)
            refused.append("live")
            why.append(bad)
        now = time.time()
        self.last_state = now
        if isinstance(d.get("at"), (int, float)) and not isinstance(d["at"], bool) and math.isfinite(d["at"]):
            self.skew = now - float(d["at"])     # (never NaN: wire.loads reads a bare NaN as None)
        if isinstance(d.get("status"), dict):
            st = dict(d["status"])
            if sid:
                st["id"] = sid                     # the authenticated id (what Health shows), never only the claimed one
            if p is not None and p.via == "shared":
                self.stations.reconfigured(claimed, st.get("config_id"))   # posting as the current id from its config
            tgs = st.pop("talkgroups", None)
            if isinstance(tgs, list):
                self._take_talkgroups(tgs)
            self.status, self.status_at, self.status_refused = st, now, None
            if self.health_ is not None:
                self.health_.observe(st)           # each channel's gaps, for the silence hints
            self.app.channels_check()          # the station runs other channel choices than these: send them
            self.app.alert_keys_check()        # ... and another revision of the pre-alerts' sources (#639 phase 2)
            self.app.stt_terms_check()         # ... or other term lists for whisper's prompt (sizeup/stt_terms.py)
            try:
                self.app.reception_seen(st.get("reception"))     # (#410) a reception test that ended: logged, once
            except Exception as e:  # noqa: BLE001 - a log row, never in the way of the station's report
                self.log(f"hub: reception test not logged: {type(e).__name__}: {e}")
        if isinstance(talking, list):
            self.live.update(talking, now, paging if isinstance(paging, list) else None)
        # caps: what this hub knows (#409's other P25 systems; each clip's STT input, sizeup/cloud/sttinput.py)
        # station: the id the hub took it as (the station adopts it: StationLink.adopt; a rename never locks it out)
        # services: the admins' service per source (settings radio_services, sizeup/sourcekeys.py): the station tags
        # its rows (and picks its speech-to-text profile) as the hub does; a station before this ignores it
        return _json(200, {"ok": True, "build": self.build, "caps": ["p25_systems", sttinput.CAP, POOL_CAP],
                           "pool": self.app.pool_word(sid or (p.id if p is not None else "")),
                           "services": dict(getattr(getattr(self.app, "cad", None), "radio_services", None) or {}),
                           **({"station": sid or p.id} if p is not None and (sid or p.id) else {}),
                           **({"refused": refused, "reason": "; ".join(why)} if refused else {})})

    def _take_talkgroups(self, tgs: list) -> None:
        # the station's rows are TalkgroupPrefs.state(): "tier_set" (another system's talkgroup: a tier, never
        # Record / Main / Transcribe) as its own page has it (App.talkgroups_state), from the tier each carries
        self.talkgroups = [dict(r, tier_set=r.get("tier") is not None) if isinstance(r, dict) and "tier_set" not in r
                           else r for r in tgs]
        if self.talkgroups:     # a source's re-tag that waited for what the station lists (its automatic service)
            getattr(getattr(self.app, "cad", None), "retag_resume", lambda **k: None)(rows_came=True)

    def _refused(self, part: str, why: str, sid: str = "") -> None:
        """A part of a state post left out for its shape ("status": the last good one stays; "live": that talker /
        page): logged once per reason an hour. self.status_refused / live_refused: {at, why, n, since} of the latest;
        status_refused is None again once a status is taken (Health's hint while it lasts)."""
        now = time.time()
        attr = "status_refused" if part == "status" else "live_refused"
        r = getattr(self, attr, None) or {}
        setattr(self, attr, {"at": now, "why": why, "n": int(r.get("n") or 0) + 1, "since": r.get("since") or now})
        if now - self._refused_said.get((f"{part}:{why}", sid), 0.0) >= REFUSAL_LOG_S:
            self._refused_said[(f"{part}:{why}", sid)] = now
            self.log(f"hub: station {'status' if part == 'status' else 'talker / page going out'} left out: {why}"
                     + (f" (station {sid})" if sid else "")
                     + (": the last good status kept" if part == "status" else "") + "; the rest of the post taken")

    def _aircraft(self, raw: bytes | None, headers) -> tuple[int, bytes, str]:
        """Our receiver's aircraft, forwarded by the station (#235): into the aircraft layer, nothing else."""
        if raw is None:
            return _json(413, {"error": "too large"})
        size = len(raw)
        if (headers.get("Content-Encoding") or "").strip().lower() == "gzip":
            import zlib
            z = zlib.decompressobj(wbits=31)
            try:
                raw = z.decompress(raw, MAX_AIRCRAFT_BODY * 4)
            except zlib.error as e:
                return _json(400, {"error": f"bad gzip: {e}"})
            if z.unconsumed_tail:
                return _json(413, {"error": "too large"})
        layer = getattr(self.app, "aircraft", None)
        if layer is None:
            return _json(404, {"error": "no aircraft layer here"})
        return _json(200, layer.station_ingest(wire.loads(raw or b"{}"), size))

    def _vessels(self, raw: bytes | None, headers) -> tuple[int, bytes, str]:
        """Our AIS receiver's vessels, forwarded by the station (#402): into the Vessels layer, nothing else."""
        if raw is None:
            return _json(413, {"error": "too large"})
        if (headers.get("Content-Encoding") or "").strip().lower() == "gzip":
            import zlib
            z = zlib.decompressobj(wbits=31)
            try:
                raw = z.decompress(raw, MAX_VESSELS_BODY * 4)
            except zlib.error as e:
                return _json(400, {"error": f"bad gzip: {e}"})
            if z.unconsumed_tail:
                return _json(413, {"error": "too large"})
        layer = getattr(self.app, "vessels", None)
        if layer is None:
            return _json(404, {"error": "no vessels layer here"})
        return _json(200, layer.station_ingest(wire.loads(raw or b"{}")))

    def _logs(self, raw: bytes | None, headers, p: Principal) -> tuple[int, bytes, str]:
        """The station's journal, scrubbed (sizeup/cloud/logship.py): into CloudWatch (sizeup/cloud/cwlogs.py), in the
        authenticated station's stream."""
        sink = getattr(self.app, "station_logs", None)
        if sink is None:
            return _json(404, {"error": "no station logs here"})
        try:
            code, out = sink.ingest(raw, headers, station=lambda claimed: self._claim(p, claimed))
        except Refused:
            raise
        except Exception as e:  # noqa: BLE001 - never in the way of the station's link
            self.log(f"station logs: {type(e).__name__}")
            return _json(503, {"error": f"station logs: {type(e).__name__}"})
        return _json(code, out)

    def _stt_inputs(self, raw: bytes | None, headers, p: Principal | None = None) -> tuple[int, bytes, str]:
        """The station's STT inputs (sizeup/cloud/sttinput.py): each kept beside its clip. stored / refused (a bad one,
        never sent again: whatever went wrong with one item, the rest still go); a write the fence refused fails the
        whole post (503: the station sends it to the leader)."""
        if raw is None:
            return _json(413, {"error": "too large"})
        if (headers.get("Content-Encoding") or "").strip().lower() == "gzip":
            import zlib
            z = zlib.decompressobj(wbits=31)
            try:
                raw = z.decompress(raw, MAX_STT_JSON)
            except zlib.error as e:
                return _json(400, {"error": f"bad gzip: {e}"})
            if z.unconsumed_tail:
                return _json(413, {"error": "too large"})
        if len(raw or b"") > MAX_STT_JSON:
            return _json(413, {"error": "too large"})
        d = wire.loads(raw or b"{}")
        if not isinstance(d, dict) or not isinstance(d.get("items"), list):
            raise ValueError("{items: [...]}")
        sid = ""
        if p is not None:
            # (the station a payload names must be the one its token proves; its id keys the arrival delays, #677)
            sid = self._claim(p, d.get("station")) or p.id
        stored, refused = [], []
        try:
            for it in d["items"][:200]:
                n = it.get("n") if isinstance(it, dict) else None
                if not isinstance(n, int) or isinstance(n, bool):
                    continue
                fenced = fence_mod.refused()
                try:
                    ok = self.app.put_stt_input(it, station=sid)
                except Exception as e:  # noqa: BLE001 - one malformed item never fails the post (nor stalls the lane)
                    if isinstance(e, fence_mod.FencedWrite) or fence_mod.refused() != fenced:
                        raise
                    self.app._stt_say(f"hub: speech-to-text input refused: {type(e).__name__}")
                    ok = False
                (stored if ok else refused).append(n)
        finally:
            if stored:
                self.app.stt_stored()               # the waiters (read_stt_audio's wait_s): once per post
        return _json(200, {"ok": True, "stored": stored, "refused": refused})

    def _metrics(self, raw: bytes | None, headers, p: Principal) -> tuple[int, bytes, str]:
        """The station's signal metrics (sizeup/cloud/metrics.py): into metrics.sqlite under the authenticated id."""
        from sizeup.cloud.metricstore import MetricsError
        if raw is None:
            return _json(413, {"error": "too large"})
        if (headers.get("Content-Encoding") or "").strip().lower() == "gzip":
            import zlib
            z = zlib.decompressobj(wbits=31)
            try:
                raw = z.decompress(raw, MAX_METRICS_JSON)
            except zlib.error as e:
                return _json(400, {"error": f"bad gzip: {e}"})
            if z.unconsumed_tail:
                return _json(413, {"error": "too large"})
        if len(raw or b"") > MAX_METRICS_JSON:
            return _json(413, {"error": "too large"})
        store = getattr(self.app, "metric_store", None)
        if store is None:
            return _json(404, {"error": "no station metrics here"})
        try:
            d = wire.loads(raw or b"{}")
        except (ValueError, RecursionError, OverflowError) as e:
            # a batch that isn't one: 400, which the station drops (never sent again and again)
            return _json(400, {"error": f"bad JSON: {type(e).__name__}"})
        if not isinstance(d, dict):
            return _json(400, {"error": "a JSON object"})
        sid = self._claim(p, d.get("station")) or p.id
        try:
            return _json(200, store.ingest(sid, d.get("records") if "records" in d else []))
        except MetricsError as e:
            return _json(400, {"error": str(e)})
        except fence_mod.FencedWrite:
            raise                                  # not the leader: 503 + Retry-After, the station sends it again
        except Exception as e:  # noqa: BLE001 - never in the way of the station's link
            self.log(f"station metrics: not kept: {type(e).__name__}: {e}")
            return _json(503, {"error": f"station metrics: {type(e).__name__}"})

    def _retry(self, seq: int) -> bool:
        """A source_event failed to apply: True while it still has tries left (left unacked, the station sends it
        again), False once it has had SOURCE_RETRIES (then skipped, like any other bad event)."""
        ep, s, n = self._failed
        n = n + 1 if (ep, s) == (self.epoch, seq) else 1
        self._failed = (self.epoch, seq, n)
        return n < SOURCE_RETRIES

    def _refused_batch(self, code: int, reason: str, error: str, **extra) -> tuple[int, bytes, str]:
        """A batch of events refused as a whole: {error, reason, ...} (the station splits it to find the event:
        sizeup/cloud/station.py _rejected), in the log once per reason every REFUSAL_LOG_S."""
        now = time.time()
        if now - self._refused_said.get(("events", reason), 0.0) >= REFUSAL_LOG_S:
            self._refused_said[("events", reason)] = now
            self.log(f"hub: refused a batch of the station's events: {code} {reason}")
        return _json(code, {"error": error, "reason": reason, **extra})

    def _events(self, raw: bytes | None, p: Principal | None = None) -> tuple[int, bytes, str]:
        """p: who sent it (handle passes it always; None only from a caller in this process, e.g. a test).

        -> 200 {ok, acked, applied, rejected?: [{i, seq, type, reason, error}]}: each event it couldn't parse
        ("malformed") or apply ("apply_failed") is skipped, never blocking the ones behind it, and listed (by its index
        in the batch and its seq), so the station keeps it (sizeup/cloud/outbox.py quarantine). A batch it can't read
        at all: 413 {reason: too_large, max_bytes} / 400 {reason: bad_json | bad_batch}. A station from before this
        ignores "rejected"; a hub from before this sends none (and a 400 without a reason: the station splits it)."""
        if raw is None:
            return self._refused_batch(413, "too_large", f"too large ({MAX_EVENTS_BODY // 1024 // 1024} MB max): "
                                                         "send smaller batches", max_bytes=MAX_EVENTS_BODY)
        try:
            d = wire.loads(raw or b"{}")
        except (ValueError, RecursionError) as e:
            return self._refused_batch(400, "bad_json", f"bad request: not JSON ({type(e).__name__})")
        if not isinstance(d, dict) or not isinstance(d.get("events"), list):
            return self._refused_batch(400, "bad_batch", "bad request: {epoch, events: [...]}")
        if p is not None:
            # (one outbox cursor still: a cursor per node is step 3 of the nodes design, docs/nodes.md)
            self._claim(p, d.get("station"))
        epoch = str(d.get("epoch") or "")[:40]
        rejected: list[dict] = []
        events: list[tuple[int, int, dict]] = []
        for i, ev in enumerate(d["events"]):
            seq = ev.get("seq") if isinstance(ev, dict) else None
            if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
                # can't be acked or skipped by its seq: listed by its index, never moving the cursor
                rejected.append({"i": i, "seq": seq if isinstance(seq, int) and not isinstance(seq, bool) else None,
                                 "type": str(ev.get("type"))[:40] if isinstance(ev, dict) else "",
                                 "reason": "malformed", "error": "an event needs a seq (an integer from 1)"})
                continue
            events.append((seq, i, ev))
        with self._ingest:
            if epoch != self.epoch:
                if self.epoch:
                    self.log(f"hub: the station's outbox is new (epoch {epoch}): counting its events from the start")
                self.epoch, self.seq = epoch, 0
            n = 0
            for seq, i, ev in sorted(events, key=lambda x: (x[0], x[1])):
                if seq <= self.seq:
                    # delivered before (the station retried): already applied. One it couldn't apply is listed again
                    # (the post that said so may have ended in a 503 later on, never reaching the station)
                    if (self.epoch, seq) in self._rejections:
                        rejected.append(dict(self._rejections[(self.epoch, seq)], i=i))
                    continue
                kind = str(ev.get("type"))
                refused = fence_mod.refused()
                try:
                    if not isinstance(ev.get("data") or {}, dict):
                        raise BadEvent("data: a JSON object")
                    audio = base64.b64decode(ev["audio"]) if ev.get("audio") else None
                    data = ev.get("data") or {}
                    if p is not None and p.id and isinstance(data, dict) and kind in ("traffic", "page_text"):
                        # whose clip it is, on the clip (a pool job never goes back to the station it came from)
                        data = {**data, "origin_station": p.id}
                    if kind == "source_event":
                        # a copy the station's quarantine sends again (its qid): applied only over an older snapshot
                        # (on_event); said by the hub alone, never taken from the station's data
                        data = {**data, "resent": isinstance(ev.get("qid"), int) and not isinstance(ev["qid"], bool)}
                    self.app.apply(kind, data, audio)
                    n += 1
                except Exception as e:  # one bad event must not block the ones behind it
                    if isinstance(e, fence_mod.FencedWrite) or fence_mod.refused() != refused:
                        raise fence_mod.FencedWrite(f"not the leader: {kind} #{seq} not acked") from e
                    self.errors += 1
                    # an incident source's event: its text may quote the county's data, so the type only (#297)
                    said = "" if kind in SOURCE_KINDS else f": {e}"
                    self.last_error = f"{kind} #{seq}: {type(e).__name__}{said}"[:200]
                    self.log(f"hub: could not apply {kind} #{seq}: {type(e).__name__}")
                    if kind == "source_event" and not isinstance(e, SourceError) and self._retry(seq):
                        break                  # not acked: the station sends it again (a malformed one is skipped)
                    # malformed: never applies, whenever it is sent (the station doesn't keep sending it); else it may
                    # once the hub is fixed
                    bad = {"seq": seq, "type": kind[:40],
                           "reason": "malformed" if isinstance(e, (BadEvent, SourceError)) else "apply_failed",
                           "error": f"{type(e).__name__}{said}"[:200]}
                    rejected.append(dict(bad, i=i))
                    self._rejections[(self.epoch, seq)] = bad
                    while len(self._rejections) > REJECTIONS_KEPT:
                        self._rejections.popitem(last=False)
                if fence_mod.refused() != refused or not self.app._may_write_audio():
                    # a write of this event was refused (the lease went, #501), even if something on the way caught
                    # it, or the lease is gone now: neither it nor anything after it is acked (seq stays), the
                    # station sends them again, to the leader
                    raise fence_mod.FencedWrite(f"not the leader: {kind} #{seq} not acked")
                self.seq = seq
            self.app.mdb.put("station_epoch", self.epoch)
            self.app.mdb.put("station_seq", str(self.seq))
            if n:
                self.app.sync()
                self.live.on_change()       # push the display now, not on its next second
            return _json(200, {"ok": True, "acked": self.seq, "applied": n, **({"rejected": rejected} if rejected else {})})


SOURCE_KINDS = ("source_event", "source_health", "source_seen")   # an external incident source's (#297)
SOURCE_RETRIES = 3          # a source_event that fails to apply (not a malformed one) is retried this often, then skipped
REJECTIONS_KEPT = 200       # the events it couldn't apply, remembered to say so again on a retried post (Hub._events)


class BadEvent(ValueError):
    """A station event that can never apply (a bad id, data that isn't an object): "malformed" in the answer."""
AUDIO_UPLOAD_S = 5.0        # the audio uploader's round (#414): the clips the ingest couldn't store, then the migration
AUDIO_UPLOAD_ROWS = (20, 200)   # rows a round: in the day, off-peak
SWEEP_LEADER_S = 24 * 3600   # the audio orphan sweep: only once this task has held the lease this long (#414)
STOP_LAYERS_S = 5.0          # HubApp.stop(): the threads' stops get this long, then speech-to-text and the final sync
                             # go ahead anyway (#546: the 30 s drain once ran out before the sync)


def _bounded(steps: list, wait_s: float, log: Callable[[str], None]) -> str:
    """Run each (name, stop) in turn on a thread of their own, waiting for them at most wait_s. "" when all are done,
    else the name of the one still running (it carries on, a daemon). One that raises is logged and the rest still run."""
    at = [""]

    def run() -> None:
        for name, fn in steps:
            at[0] = name
            try:
                fn()
            except Exception as e:  # noqa: BLE001 - never keeps the next one from stopping
                log(f"hub: stop: {name}: {type(e).__name__}: {e}")
        at[0] = ""

    th = threading.Thread(target=run, name="hub-stop", daemon=True)
    th.start()
    th.join(wait_s)
    return at[0] if th.is_alive() else ""


def _our_tg(t) -> int | None:
    """A transmission's talkgroup for its clip's row, None for another P25 system's (#409): see _where."""
    return None if sourcekeys.is_context(t.__dict__) else t.talkgroup


def _where(d: dict) -> dict:
    """A transmission's channel and talkgroup, for its clip's retention (#414). Another P25 system's (#409): its
    channel only, never a "tg:<n>" of ours (the same number there is another talkgroup)."""
    tg = None if sourcekeys.is_context(d) else d.get("talkgroup")
    return {"channel": str(d.get("source") or ""), "talkgroup": tg if isinstance(tg, int) and not isinstance(tg, bool) else None}


class HubApp(App):
    """App with no radios: the station's events go through the same code (apply), the station's
    switches through jobs, and the state lives in SQLite (hubstore.py)."""

    @property
    def clip_origin(self) -> str:
        """The station the catch-up's clips come from: the one station this hub takes until multi-station (a clip handed
        over carries its own, origin_station; the catch-up per station's rows is #725, with multi-station)."""
        hub = getattr(self, "hub", None)
        reg = getattr(hub, "stations", None)
        try:
            return reg.legacy_id() if reg is not None else ""
        except Exception:  # noqa: BLE001 - none known
            return ""

    def __init__(self, cfg, log: Callable[[str], None] = print, fence=None):
        self._catchup_refused_until = 0.0              # the station refused catch_up (an older build): not asked before
        self._aws_since: dict[str, float] = {}         # id -> when Amazon Transcribe got it (the sweep's clock)
        self._aws_asked: dict[str, float] = {}         # id -> when the station's whisper was asked instead
        self._aws_late_wait: set[str] = set()          # timed out, not a priority channel: AWS's late answer is the text
        self._unclear_tried: dict[str, float] = {}     # id -> when its unclear transcript went to another engine (once)
        self._pending_seen: dict[str, float] = {}      # page id -> when the sweep first saw it waiting for whisper
        self._aws_was_ok: bool | None = None           # AWS could take audio at the last sweep (_aws_resend)
        self._stt_cond = threading.Condition()          # an STT input stored (read_stt_audio's wait_s)
        state = Path(cfg.cad_db).parent if cfg.cad_db else None
        self.mdb = MonitorDb(state / "hub.sqlite" if state else ":memory:")
        # audio.sqlite: under the hub's supervisor (sizeup/handoff.py) the standby follows it with cad and hub, so
        # on a takeover it is current and opened at once; retention keeps it small (_audio_retention, #251). With
        # SIZEUP_AUDIO_BUCKET (#414) it keeps the metadata only, the bytes in S3 (sizeup/audiostore.py), recent clips
        # cached on the task's disk; only the leader writes (may_write: the fence, set below once App has it)
        from sizeup import audiostore
        store = audiostore.from_env(log)
        cache = audiostore.DiskCache(state / "audio-cache") if store is not None and state else None
        self.audio = AudioDb(state / "audio.sqlite" if state else ":memory:", int(cfg.cloud_audio_max_mb * 1024 * 1024),
                             store=store, cache=cache, log=log)
        if store is not None:
            log(f"audio: bytes in {store.name}, metadata in audio.sqlite")
        app_mod._mark("hub databases")
        # Nothing that could open an SDR or start a program: no dongles, no trunk-recorder or its
        # capture dir, no voice server, and no captures/ (its JSON files and WAVs live in SQLite here).
        super().__init__(replace(cfg, clips_dir="", p25_dir="", p25_recorder="", sdrs=(), sdr_auto=False,
                                 shared_device="", dispatch_tts_python=""), fence=fence)
        self.receivers, self.modes = [], []
        self.audio.may_write = self._may_write_audio
        self.mdb.may_write = self._may_write_audio       # hub.sqlite too: a fenced task writes neither (#501)
        self.mdb.db.log = self.audio.db.log = log
        if self.fence is not None:
            self.mdb.db.on_refuse = self.audio.db.on_refuse = self.fence.check    # refused: the process stops (#501)
        self.audio.may_delete = self._may_delete_audio
        self.hub = Hub(self, cfg, log)
        # the GPU worker pool (#639, sizeup/sttpool.py): AWS-channel clips and the shed clips' catch-up go to a worker
        # first while one is ready, else the way they went before; off with no worker added (Settings > Station > STT
        # workers). Every transcript is kept as written (sizeup/sttruns.py), its latency per channel on Health.
        reg = self.hub.workers
        self.sttpool = sttpool.Pool(enabled=reg.enabled, log=log, allowed=lambda wid: (
            reg.stations_on(wid.removeprefix(STATION_PREFIX)) if wid.startswith(STATION_PREFIX) else reg.active(wid)))
        # a station's whisper is a pool worker under its own station identity (#639 phase 2): no token more to issue,
        # rotate or revoke; revoking the station ends its worker too
        self.stt_api = WorkerApi(reg, self.sttpool, self._may_write_audio, log, station=self.hub.station_of)
        from sizeup.cloud.metricsbackup import S3Copies
        self.stt_runs = sttruns.Runs(state / "stt.sqlite" if state else ":memory:", may_write=self._may_write_audio,
                                     backup=S3Copies.from_env("stt") if state else None, exists=self._clips_logged,
                                     log=log)
        self.stt_latency = sttruns.LatencyBook()
        # the LLM lane (sizeup/cloud/infer.py): a worker's local model for the intel engine's JSON classifications; its own
        # queue, after the pre-alert clips, off unless a worker advertises an LLM. Prompts and answers never kept
        self.infer_lane = infer_mod.Lane(stt_busy=self.sttpool.holds_prealert, may_write=self._may_write_audio, log=log,
                                         enabled=lambda: bool(self.hub.workers.pool_settings()["on"]),
                                         llm_on=self.hub.workers.llm_on)
        self.stt_api.infer = self.infer_lane
        self.hub.workers.listeners.append(self.infer_lane.switched)     # (a switch holds at once, not a second later)
        self.cad.stt_workers = self.hub.workers        # Settings > Station > STT workers (sizeup/cad/api.py; admins)
        self.cad.stt_workers_live = self._worker_live
        self.cad.stt_pool_forget = lambda wid: (self.sttpool.forget(wid, "revoked"), self.infer_lane.forget(wid))[0]
        # station health (GitHub #79): the alerts, the admins' Settings > Station > Health (#203), the push to the status topic in effect (Settings, else
        # [alerts] status_topic, else ntfy_topic: notify.Pushes; never Tapout's): App.pushes' STATUS kind (the same
        # Notifier as the emergency alarms when both kinds share a topic: one queue in order)
        from sizeup.notify import STATUS
        notifier = self.pushes.notifier(STATUS)
        self.health = self.hub.health_ = StationHealth(self.hub, cfg, self.mdb, notifier, log)
        # The incident sources (#297): paging is a tier only while the station is connected; Settings > Station > Health shows them.
        self.cad.sources.paging_ok = self.hub.connected
        self.cad.sources.trigger = self._sources_poll_now      # a pre-alert here: the station's adapters poll now
        self.health.sources = self.cad.sources.panel
        # the intel engine's counters, a line per pack: in scope / considered (sizeup/intel/counters.py); first, a warning
        # row when intel pushes go nowhere (no intel topic of its own, or one refused: notify.Pushes.intel_health); then
        # the Listen links' self-test and cleanup (sizeup/intel/listen.py health()); and last, the local model behind the
        # police rules (sizeup/intel/backing.py): PC worker ok / rules only
        lk = lambda: getattr(self.cad.intel, "listen", None)      # noqa: E731
        bk = lambda: getattr(self.cad.intel, "backing", None)     # noqa: E731
        self.health.intel = lambda: [r for r in (self.pushes.intel_health(), lk() and lk().health()) if r] \
            + self.cad.intel.counts()["packs"] + ([bk().health_row()] if bk() is not None else [])
        # MTA TrainTime or its fallback (#362); Tapout's phone pre-alert (not configured until an admin sets the topic)
        # and each aircraft source with the FAA registry (#542: last good read, failures and back-off on hover)
        self.health.feeds = lambda: [self.trains.feed_health()] + ([self.prealert.feed_health()] if self.prealert is not None else []) \
            + (self.aircraft.feed_health() if getattr(self, "aircraft", None) is not None else [])
        # the hub's own error spikes (#285, sizeup/errwatch.py): the same status notifier; main() tees the log into it
        from sizeup import errwatch
        pub = (cfg.cloud_public_url or "").rstrip("/")
        self.error_watch = errwatch.ErrorWatch(notifier, settings=lambda: self.cad.settings.section(errwatch.SECTION), log=log,
                                               click=f"{pub}/cad#settings/station/health" if pub else "", db=self.mdb,
                                               muted=lambda: self.health.muted("hub_errors"))
        # an alert kind muted in Settings > System > Push notifications (#340): shown on Health, not pushed
        from sizeup.cloud.health import MUTE_SECTION
        self.health.mutes = lambda: self.cad.settings.section(MUTE_SECTION)
        self.health.errors = self.error_watch
        # the camera DVR (#550): Health's Camera DVR lines (how many record, the cameras parked as dead upstream)
        self.health.dvr = lambda: self.roads.dvr.panel() if getattr(self.roads, "dvr", None) is not None else None
        # the replicas as the supervisor last verified them (sizeup/replicawatch.py): Health's Replicas row and alert
        if os.environ.get("LITESTREAM_BUCKET", "").strip():
            from sizeup import replicawatch
            rw_state = Path(os.environ.get("SIZEUP_RUN_DIR", "/tmp/sizeup-run")) / replicawatch.STATE_FILE
            self.health.replicas = lambda: replicawatch.read_state(rw_state)
        if not notifier.enabled:
            log("health: no push for station alerts (no status or alerts topic in effect): Settings > Station > Health and log only")
        self.live = self.hub.live                      # main() sets on_change to push the display
        self.cad.live = self.live.state
        # the Radio page's source tree (#415): the station's talkgroups and systems, as its state says them
        self.cad.source_rows = lambda: self.hub.talkgroups or []
        self.cad.source_systems = lambda: {self.tg.county: "County", **{
            str(x.get("name")): str(x.get("label") or x.get("name")) for x in (_part(self.hub.status, "p25_systems", list) or [])
            if isinstance(x, dict) and x.get("name")}}
        self.paging_state = self.live.paging_state     # the station's page going out now
        self.cad.remote_voice = self.hub.render_voice
        self.cad.audio_blobs = self.audio
        self.cad.audio_keep_days = cfg.audio_keep_hours / 24          # Settings > System > Audio retention (#414)
        self.cad.audio_event_days = cfg.audio_keep_event_days
        self.cad.preview_blob_keep_s = cfg.audio_keep_hours * 3600     # unused dispatch previews: as long as the clips
        self.cad.comms.audio_default_days = cfg.audio_keep_hours / 24     # transcripts never before their audio (#414)
        if getattr(self, "attachments", None) is not None:
            # the incident's copy: a clip whose bytes can't be read from the store now raises (tried again later),
            # never taken for "no audio kept" (sizeup/attachments.py _copy_radio, #414)
            self.attachments.audio = lambda tid: self.audio.get(tid, strict=True) if CLIP_ID.fullmatch(tid) else None
        if store is not None and cfg.audio_keep_event_days == 0:
            log("audio: [storage] audio_keep_event_days = 0 (forever): in S3 the bucket's lifecycle still deletes clips "
                f"after {audiostore.HARD_MAX_DAYS} days (their incident attachments are kept a year)")
        self.cad.cloud = self.hub.cad_cloud
        self._chan_push = {"busy": False, "at": 0.0, "error": "", "again": False}
        for d in self.mdb.load(self.store):
            self.pages.append(page_from_dict(d))
        self._rematch_pages()                          # pairs named since they were heard (#80)
        for p in list(self.pages):                     # pages from before #64: rows in Radio too
            self._page_to_cad(p, replace=False)
        self._clean_hub()
        # where the hub's CPU goes (#425, sizeup/perf.py): by thread, always on; a profile on demand; a log line an hour
        from sizeup.perf import Perf
        self.perf = Perf(log=log)
        self._perf_sizes()
        # the station's logs into CloudWatch, and Settings > Station > Logs (sizeup/cloud/cwlogs.py): no AWS call until
        # the station sends a batch or an admin opens Logs; "not configured" until sizeup-aws step 25 has run
        from sizeup.cloud.cwlogs import SECTION as LOGS_SECTION, StationLogs
        from sizeup import logscrub, procenv
        procenv.register_secrets()                      # the hub's own secrets in its environment: never in a log row
        logscrub.secret(self.hub.token, self.hub.status_token)
        self.station_logs = StationLogs(settings=lambda: self.cad.settings.section(LOGS_SECTION),
                                        station_status=lambda: self.hub.status, log=log)
        self.cad.station_logs = self.station_logs
        self.cad.stations = self.hub.stations          # Settings > Station > Stations (sizeup/cad/api.py; admins)
        # the station's signal metrics (sizeup/cloud/metricstore.py): metrics.sqlite, its own file (not in Litestream:
        # copies in the backups bucket, sizeup/cloud/metricsbackup.py), opened at the first post; GET /api/cad/metrics
        from sizeup.cloud.metricsbackup import S3Copies
        from sizeup.cloud.metricstore import MetricStore
        self.metric_store = MetricStore(state / "metrics.sqlite" if state else ":memory:", may_write=self._may_write_audio,
                                        log=log, audit=self._metrics_audit, backup=S3Copies.from_env() if state else None)
        self.cad.station_metrics = self.metric_store
        self.health.metrics = self.metric_store.health     # Settings > Station > Health: a warning if copies stall
        # #606: the suggested squelch, Health's sparks (GET /api/cad/metrics?view=) and the shift warnings (Health hints)
        from sizeup.cloud.metricsinsight import SECTION as SIGNAL_SECTION, Insight
        self.metric_insight = Insight(self.metric_store, section=lambda: self.cad.settings.section(SIGNAL_SECTION), log=log)
        self.cad.station_metrics_insight = self.metric_insight
        self.health.shifts = self.metric_insight.hints
        # #677: each STT input's arrival after its event (sizeup/cloud/sttarrival.py): into the metrics store, on Health
        from sizeup.cloud.sttarrival import Arrival
        self.stt_arrival = Arrival()
        self._stt_arrival_cache = None
        self.health.stt_arrival = self.stt_arrival_stats
        self._renaming = threading.Lock()                  # apply_station_renames: one at a time
        app_mod._mark("hub")

    def _perf_sizes(self) -> None:
        """How many things the main in-memory lists and caches hold (#425: a len() each, read once a minute), so
        what grows with uptime shows next to the CPU. A name that is gone is simply not listed."""
        def size(obj, attr: str):
            return lambda: len(v) if (v := getattr(obj, attr, None)) is not None else None

        dvr = getattr(self.roads, "dvr", None)
        for name, obj, attr in (
                ("pages", self, "pages"), ("monitor_incidents", self.store, "incidents"), ("monitor_traffic", self.store, "loose"),
                ("hubdb_written", self.mdb, "_written"), ("station_jobs", getattr(self.hub, "jobs", None), "_jobs"),
                ("aws_waiting", self, "_aws_since"), ("camera_images", self.roads, "_images"),
                ("camera_segments", self.roads, "_segments"), ("camera_streams", self.roads, "_streams"),
                ("camera_health", self.roads, "_health"), ("dvr_cameras", dvr, "_cams"), ("gis_layers", self.gis, "_bytes"),
                ("radar_tiles", self.weather, "_tiles"), ("trains", self.trains, "_gtfs_pos"),
                ("aircraft", self.aircraft, "phases")):
            if obj is not None:
                self.perf.gauge(name, size(obj, attr))
        # context clips the station's whisper shed (#300, radio-intel PR 3): lost recall that would otherwise look quiet
        self.perf.gauge("stt_shed_context", lambda: q.get("shed_context") if isinstance(
            q := (_part(self.hub.status, "health") or {}).get("stt_queue"), dict) else None)
        # ... and caught up later (sizeup/sttcatchup.py): the backlog waiting, and caught up in the last 24 h
        self.perf.gauge("stt_catchup_waiting", lambda: self.catchup.stats().get("waiting"))
        self.perf.gauge("stt_caught_up_24h", lambda: self.catchup.stats().get("caught_24h"))
        self.perf.gauge("dvr_segments", lambda: sum(len(c.get("segs") or ()) for c in list(getattr(dvr, "_cams", {}).values()))
                        if dvr is not None else None)

    # -- the station's events ----------------------------------------------------------------------

    def apply(self, kind: str, d: dict, audio: bytes | None) -> None:
        if kind in ("traffic", "alarm") and d.get("source") in RENAMED and d["source"] not in self._kind \
                and RENAMED[d["source"]] in self._kind:
            # #275: a station whose config isn't renamed yet (its update is deferred once) sends the old name: its
            # transmissions are the renamed channel's here (airband, the pre-alerts, AWS); the id stays the station's
            d = {**d, "source": RENAMED[d["source"]]}
        if kind in ("page", "traffic"):
            (self._apply_page if kind == "page" else self._apply_traffic)(d, audio)
            arrival = getattr(self, "stt_arrival", None)
            if arrival is not None:                  # (#677: its STT input's arrival is measured from now)
                arrival.event(str(d.get("id") or ""))
        elif kind == "page_text":
            self._apply_page_text(d)
        elif kind == "retext":
            pid = self._id(d)
            with self._lock:
                page = next((p for p in self.pages if p.id == pid), None)
            if page is not None and getattr(page, "stt", "") == "aws" and (
                    page.status == "pending" or getattr(page, "flagged", False)):
                # the station's whisper standing in for Amazon Transcribe: the page is read now
                page.raw, page.confidence = str(d.get("text") or ""), d.get("confidence")
                page.status, page.model = str(d.get("status") or "done"), str(d.get("model") or "")  # type: ignore[attr-defined]
                page.stt, page.flagged = "", False  # type: ignore[attr-defined]
                page.vocab_v = str(d.get("vocab_v") or "")[:80]  # type: ignore[attr-defined]
                self._transcribed(page)
                self._stt_ran(pid, "station-whisper", page.model, page.raw, page.status, page.confidence, item=page)
                return
            alt = d.get("alt") if isinstance(d.get("alt"), dict) else None
            # (standing in for AWS: the text of record; else a person's re-run, or the catch-up's)
            role = "catchup" if d.get("catchup") else "text" if pid in self._aws_asked else "rerun"
            self._prompt_time(pid, d)               # (#727: whisper's prompt had the clip's own time)
            self._retext(pid, str(d.get("text") or ""), str(d.get("status") or "done"),
                         d.get("confidence"), str(d.get("model") or ""), alt=alt,
                         quiet=bool(d.get("catchup")),      # (a shed clip caught up: old words, nothing alerts)
                         vocab_v=str(d.get("vocab_v") or "")[:80],
                         late=bool(d.get("late")))          # (#747: a call from while the station was down: the same)
            if str(d.get("status") or "done") in ("done", "no_speech"):     # the station's whisper's text (#639)
                self._stt_ran(pid, "station-whisper", str(d.get("model") or ""), str(d.get("text") or ""),
                              str(d.get("status") or "done"), d.get("confidence"), role=role,
                              vocab_v=str(d.get("vocab_v") or "")[:80])
        elif kind == "rerun":                         # a re-run on the station is over (changed, same or failed)
            self._rerun_finish(self._id(d), str(d.get("result") or "")[:200], str(d.get("model") or "")[:80])
        elif kind == "alt_text":                      # "both" re-run: the station's whisper text, to compare
            self._set_alt(self._id(d), str(d.get("text") or ""), str(d.get("model") or "")[:80])
        elif kind == "alarm":                         # the P25 emergency button, ahead of its transcript (#128)
            self._apply_alarm(d, audio)
        elif kind in SOURCE_KINDS:                     # an external incident source on the station (#297)
            self.apply_source(kind, d)
        elif kind == "heard":
            if d.get("system") in ("mdc", "p25", "fleetsync", "mrrs"):     # (MRRS: heard rows, never a rig)
                self.radios.heard(d["system"], str(d.get("radio_id") or ""), str(d.get("alias") or ""), d.get("at"),
                                  str(d.get("channel") or ""))
        elif kind == "activity":                      # key-ups on an encrypted talkgroup (#409): counted, never rows
            self.cad.comms.activity(str(d.get("source") or ""), int(d.get("minute") or 0), int(d.get("calls") or 0),
                                    [str(x)[:12] for x in (d.get("radios") or [])][:40])
        else:
            self.hub.log(f"hub: ignoring event type {kind!r} (a newer station?)")

    @staticmethod
    def _id(d: dict) -> str:
        cid = str(d.get("id") or "")
        if not CLIP_ID.fullmatch(cid) or ".." in cid:
            raise BadEvent(f"bad id {cid[:40]!r}")
        return cid

    def _apply_page(self, d: dict, audio: bytes | None) -> None:
        pid = self._id(d)
        with self._lock:
            have = next((p for p in self.pages if p.id == pid), None)
        if have is not None:
            # here before (its page_text came first: the page event was late, or sent again from the station's
            # quarantine): its clip, if it has none yet, and its row in Radio again (with play)
            if audio and not self.audio.has(pid):
                self.audio.put(pid, audio, "clip", at=have.end, channel=have.label)
                self._page_to_cad(have)
            return
        page = page_from_dict(d)
        if audio:
            self.audio.put(pid, audio, "clip", at=page.end, channel=page.label)
        heard = self._page_heard(page)
        if heard and getattr(page, "stt", "") == "aws":      # the station skipped whisper: Amazon Transcribe here
            self._aws_from_clip(page, audio, page.label)

    def _apply_page_text(self, d: dict) -> None:
        pid = self._id(d)
        with self._lock:
            page = next((p for p in self.pages if p.id == pid), None)
        if page is None:                              # its "page" never came: take this one as it
            page = page_from_dict(d)
            with self._lock:
                self.pages.appendleft(page)
        elif (page.status != "pending" and not getattr(page, "flagged", False)) or getattr(page, "stt", "") in ("aws", "pool"):
            return                                    # applied before (or Amazon Transcribe or the pool reads it here)
        if str(d.get("stt") or "") == "pool" and not d.get("raw"):     # the station handed it to the pool (its restart)
            return self._pool_clip(page, page.label, self.audio.get(pid), "urgent")
        page.flagged = False  # type: ignore[attr-defined]   (flagged by the sweep: its text came after all)
        page.raw, page.confidence = str(d.get("raw") or ""), d.get("confidence")
        page.status, page.model = str(d.get("status") or "done"), str(d.get("model") or "")  # type: ignore[attr-defined]
        page.vocab_v = str(d.get("vocab_v") or "")[:80]  # type: ignore[attr-defined]
        self._transcribed(page)
        if page.status in LANDED:                        # the station's whisper's text (#639)
            self._stt_ran(pid, "station-whisper", page.model, page.raw, page.status, page.confidence, item=page)
        if str(d.get("stt") or "") == "both" and page.status in LANDED:
            self._aws_from_clip(page, self.audio.get(pid), page.label, both=True)

    def _traffic_of(self, d: dict) -> Traffic:
        """The station's transmission as sent, its rig from the hub's radio registry (edited here)."""
        t = Traffic.from_dict({k: v for k, v in d.items() if EXTRA.fullmatch(str(k))})
        ids = [x.strip() for x in (t.radio_id or "").split(",") if x.strip()]
        pairs = d.get("pairs")
        system = str(getattr(t, "system", "") or "")
        if system:                                     # another P25 system's radio IDs (#409): never one of our rigs,
            t.rig = ""                                 # its talkers named from the registry here (an admin's MRRS names)
            if system in OTHER_P25:
                if isinstance(pairs, list):
                    got = [(str(p[0]), str(p[1])) for p in pairs if isinstance(p, list) and len(p) == 2]
                    t.unit = self.radios.talkers(system, got, t.unit)
                elif len(ids) == 1 and self.radios.name(system, ids[0]):     # a station from before it sent pairs
                    t.unit = self.radios.name(system, ids[0])
        elif t.talkgroup is None:
            t.rig = self.radios.rig_mdc(ids)
        elif isinstance(pairs, list):
            t.rig = self.radios.rig_p25([(str(p[0]), str(p[1])) for p in pairs if isinstance(p, list) and len(p) == 2])
        else:
            t.rig = self.radios.rig_for_names(t.unit, False)[0]
        return t

    def _apply_alarm(self, d: dict, audio: bytes | None) -> None:
        """The station flagged an emergency at the end of the call: the alarm now (and its clip, for Replay);
        the "traffic" event with the transcript follows and updates it."""
        tid = self._id(d)
        if audio and not self.audio.has(tid):
            self.audio.put(tid, audio, "clip", at=float(d.get("end") or time.time()), **_where(d))
        t = self._traffic_of(d)
        t.text = ""
        # the event's own flag (every station sends "emergency": true with an alarm; one without it is taken as one)
        t.emergency = bool(d.get("emergency", True))  # type: ignore[attr-defined]
        self._alarm(t, "")

    def _apply_traffic(self, d: dict, audio: bytes | None) -> None:
        tid = self._id(d)
        if any(t.id == tid for t in list(self.store.loose) + [t for i in self.store.incidents for t in i.traffic]):
            return
        if audio:
            self.audio.put(tid, audio, "clip", at=float(d.get("end") or time.time()), **_where(d))
        # with the station's extras (raw, confidence, model, attach, pairs, ...): plain names only
        t = self._traffic_of(d)
        key = traffic_key(t)
        if d.get("transcript_pending") == "aws":            # the station skipped whisper: Amazon Transcribe here
            return self._aws_from_clip(t, audio, key)
        if d.get("transcript_pending") == "pool":           # a station handed it to the pool (#639 phase 2)
            return self._pool_clip(t, key, audio, d.get("stt_class"))
        self._transcribed(t)
        if t.status in LANDED:                                 # the station's whisper's text, as it came (#639)
            self._stt_ran(t.id, "station-whisper", str(getattr(t, "model", "") or ""), t.text, t.status,
                          getattr(t, "confidence", None), key=key, item=t, wait_s=d.get("stt_wait_s"),
                          run_s=d.get("stt_run_s"))
        if d.get("stt") == "both" and t.status in LANDED:
            self._aws_from_clip(t, audio, key, both=True)

    # -- Amazon Transcribe here, for the station's clips --------------------------------------------------

    def _aws_from_clip(self, item, wav: bytes | None, key: str, both: bool = False) -> None:
        from sizeup.transcribe import wav_pcm
        pcm = wav_pcm(wav, getattr(item, "voice_offset_s", 0.0) if not isinstance(item, Traffic) else 0.0) if wav else None
        if both:
            if pcm is not None:
                self._aws_alt(item, pcm, key)
            return
        if pcm is None:                                    # no clip came (the outbox dropped it): the station's whisper
            item.status, item.raw, item.model, item.stt = "pending", "", channels_model(), "aws"
            if isinstance(item, Traffic):
                item.__dict__.pop("transcript_pending", None)
                self._transcribed(item)
            return self._aws_failed(AwsJob("traffic" if isinstance(item, Traffic) else "page", item.id, key,
                                           page=None if isinstance(item, Traffic) else item), "no audio came with it")
        self._aws_start(item, pcm, key)

    def _aws_start(self, item, pcm, key: str) -> None:
        if self._pool_first(item, pcm, key):       # a GPU worker ready (#639): it first, AWS if it can't
            return
        self._aws_since[item.id] = time.time()
        self._aws_asked.pop(item.id, None)
        super()._aws_start(item, pcm, key)

    def _aws_done(self, job, text: str, conf) -> None:
        since, ran = self._aws_since.get(job.id), time.time()
        super()._aws_done(job, text, conf)              # (the text first: the record never delays it)
        self._stt_ran(job.id, "aws", channels_model(), text, "done" if text else "no_speech", conf, key=job.channel,
                      item=job.page if job.kind == "page" else None, queued_at=since,
                      run_s=ran - since if since else None,     # (AWS's stream: its wait and run as one)
                      role="catchup" if job.catchup else "rerun" if job.rerun else "text", vocab_v=job.vocab_v)
        if job.alt is None and not job.rerun:
            self._aws_since.pop(job.id, None)
            self._aws_asked.pop(job.id, None)
        self.sync()

    def _unclear(self, item, key: str, quiet: bool = False) -> None:
        """An unclear transcript (sizeup/stt_quality.py): said (App._unclear), then the clip asked ONCE of another engine
        that can take it now: a whisper's to Amazon Transcribe (its budget allowing), else (or AWS's) to the station's
        whisper as a re-run. Its answer is checked the same way; still unclear, it stays so. Never for a catch-up or a
        late call (old words), a "both" channel (AWS is asked already) or a standby."""
        super()._unclear(item, key, quiet)
        cid = str(getattr(item, "id", "") or "")
        if quiet or not cid or cid in self._unclear_tried or not self._may_write_audio():
            return
        if self.channel_pref(key)["engine"] == "both":
            return
        self._unclear_tried[cid] = time.time()
        while len(self._unclear_tried) > UNCLEAR_TRIED_MAX:
            self._unclear_tried.pop(next(iter(self._unclear_tried)))
        engine = stt_quality.engine_of(str(getattr(item, "model", "") or ""))
        page = None if isinstance(item, Traffic) else item
        if engine != "aws" and self.aws is not None:
            from sizeup.transcribe import is_context
            ok, why = self.aws.available(key if is_context(key) else None)
            pcm = self._rerun_pcm(item) if ok else None
            if pcm is not None:
                self.aws.submit(key, AwsJob("traffic" if page is None else "page", cid, key, page=page, rerun=True), pcm)
                self.hub.log(f"speech-to-text {cid}: unclear from {engine}: asked of Amazon Transcribe")
                return
            self.hub.log(f"speech-to-text {cid}: unclear from {engine}: AWS can't take it "
                         f"({why if not ok else 'no clip here'})")
        if engine == "station-whisper":
            return                             # (no other whisper to ask: it stays unclear)
        threading.Thread(target=self._rerun_station, args=([cid],), name="hub-unclear-rerun", daemon=True).start()
        self.hub.log(f"speech-to-text {cid}: unclear from {engine}: asked of the station's whisper")

    def _aws_late(self, job, text: str, conf) -> None:
        self._stt_ran(job.id, "aws", channels_model(), text, "done" if text else "no_speech", conf, key=job.channel,
                      role="late", vocab_v=job.vocab_v)
        if job.id in self._aws_late_wait and job.alt is None and not job.rerun:
            self._aws_late_wait.discard(job.id)     # nothing else was asked for it: AWS's late answer is the text
            item, _key = self._find(job.id)
            if item is not None and getattr(item, "status", "") == "pending":
                return self._aws_done(job, text, conf)
        super()._aws_late(job, text, conf)
        self.sync()

    def _aws_changed(self) -> None:
        """The breaker opened or closed: the station hears it now (AWS channels run its whisper, or AWS again)."""
        if getattr(self, "hub", None) is not None:
            self.channels_check(force=True)

    @staticmethod
    def _whisper_fallback(job, promoted=frozenset()) -> bool:
        """Does AWS timing out on it send it to the station's whisper? Pages, Fire/EMS 15, the firegrounds, a county
        talkgroup an incident of ours is worked on now (sizeup/cad/opchannel.py) and the airband (the ATC pre-alerts,
        #233; whisper sheds an airband clip that waits 5 min) only (#300): anything else waits for AWS's late answer
        (usually seconds after the deadline), so a slow AWS never floods the station's whisper with clips nobody is
        waiting on."""
        return (job.kind == "page" or job.channel in STT_FIRST or job.channel in RESERVE_CHANNELS
                or job.channel in AIRBAND_FIRST or job.channel in promoted)

    def _aws_failed(self, job, why: str) -> None:
        """Amazon Transcribe couldn't: the station's whisper does it (retranscribe, the text comes back as
        a retext event); a station that can't: "transcription failed", said on the transmission. Timed out on a
        channel that isn't a priority one: AWS's late answer is awaited instead (_whisper_fallback)."""
        if job.catchup:                            # a shed clip caught up: still skipped, a failed try on its row
            self.hub.log(f"aws transcribe: catch-up {job.id}: {why}: left skipped")
            self._rerun_finish(job.id, f"failed: AWS: {why[:80]}")
            return self.sync()
        late = (job.alt is None and not job.rerun and not job.recovered and why.startswith("AWS timed out")
                and not self._whisper_fallback(job, self.promoted_keys()))
        self.hub.log(f"aws transcribe: {job.kind} {job.id}: {why}" + (
            "" if job.alt is not None else ": waiting for AWS's late answer (whisper is for pages, Fire/EMS 15, the "
                                           "firegrounds and the airband)" if late else ": asking the station's whisper"))
        if late:
            self._aws_asked[job.id] = time.time()  # (the sweep: no late answer in FALLBACK_WAIT_S -> failed)
            self._aws_late_wait.add(job.id)
            return
        if job.rerun:                              # re-transcribe: the station's whisper instead, said so
            self._rerun_note[job.id] = f"AWS failed ({why[:80]})"
            threading.Thread(target=self._rerun_station, args=([job.id],), name="aws-fallback", daemon=True).start()
            return
        if job.alt is not None:                    # "both": the station's text stays
            return
        self._aws_asked[job.id] = time.time()      # (the sweep: no text from the station either -> failed)
        self._stt_state(job.page if job.kind == "page" and job.page is not None else self._find(job.id)[0], job.id,
                        "pending", f"{WHISPER_FALLBACK_STATION} (AWS failed: {why[:80]})")

        def ask() -> None:
            err = ""
            if job.recovered:                      # just restarted: the station is usually back within seconds
                end = time.time() + RECOVER_WAIT_S
                while not self.hub.connected() and time.time() < end and not self._stopping.wait(1.0):
                    pass
            try:
                try:                               # whisper at live priority, not behind live traffic (#195)
                    n = int(self.hub.remote("transcribe_now", [job.id], timeout=CALL_WAIT_S) or 0)
                except RuntimeError as e:
                    if "not allowed" not in str(e):
                        raise
                    n = int(self.hub.remote("retranscribe", [job.id], timeout=CALL_WAIT_S) or 0)   # an older station
            except (StationOffline, RuntimeError, TypeError, ValueError) as e:
                n, err = 0, str(e)
            if n:
                return
            item = job.page if job.kind == "page" and job.page is not None else self._find(job.id)[0]
            if item is not None and (getattr(item, "raw", "") or getattr(item, "text", "")
                                     or getattr(item, "status", "") not in ("pending", "remote")):
                return                             # its text came meanwhile (a late AWS answer, the station's whisper)
            note = f"{channels_model()} failed ({why[:80]}); station: {err or 'no recording to re-run'}"
            if job.kind == "page" and job.page is not None:
                job.page.status, job.page.model = "error", note
                self._stt_row(job.id, "error", note)
                self.sync()
            else:
                self._retext(job.id, "", "error", None, note, why="transcription failed:")
                self.sync()
        threading.Thread(target=ask, name="aws-fallback", daemon=True).start()

    def _stt_state(self, item, cid: str, status: str, model: str) -> None:
        """The page or transmission (in memory) and its log row (App._stt_row) say the same: which engine has it now,
        so a later write of the item to the log (_page_to_cad, _to_cad) never puts back "(AWS)"."""
        if item is not None and not (getattr(item, "raw", "") or getattr(item, "text", "")):
            item.status, item.model = status, model
        self._stt_row(cid, status, model)

    def _aws_pending(self) -> list[tuple[object, str, str]]:
        """Every page and transmission still waiting for Amazon Transcribe: (item, its channel key, kind)."""
        with self._lock:
            out = [(p, p.label, "page") for p in self.pages if p.status == "pending" and getattr(p, "stt", "") == "aws"]
        for t in list(self.store.loose) + [t for i in self.store.incidents for t in i.traffic]:
            if t.status == "pending" and getattr(t, "stt", "") == "aws":
                out.append((t, traffic_key(t), "traffic"))
        return out

    def _aws_recover(self) -> int:
        """After a restart (a redeploy, a crash) nothing that waited for Amazon Transcribe is left
        "Transcribing": less than RECOVER_S old with its clip here, to AWS again when it can take it; the
        rest (and all of it when AWS can't: the breaker, the limit, not set up) to the station's whisper."""
        from sizeup.transcribe import wav_pcm
        now = time.time()
        again = whisper = 0
        for item, key, kind in self._aws_pending():
            # each item's own pool (a police source's budget is its own, radio-intel PR 3)
            ok, why = self.aws.available(key) if self.aws is not None else (False, "Amazon Transcribe isn't set up on the hub")
            page = item if kind == "page" else None
            wav = self.audio.get(item.id)
            pcm = wav_pcm(wav, getattr(item, "voice_offset_s", 0.0) if page is not None else 0.0) if wav else None
            if ok and pcm is not None and item.end > now - RECOVER_S:
                self._aws_since[item.id] = now
                self._stt_state(item, item.id, "pending", channels_model())    # the log says AWS again
                self.aws.submit(key, AwsJob(kind, item.id, key, page=page), pcm)
                again += 1
                continue
            reason = why if not ok else "no clip on the hub" if pcm is None else f"waiting over {RECOVER_S / 60:.0f} min"
            self._aws_failed(AwsJob(kind, item.id, key, page=page, recovered=True), f"the hub restarted ({reason})")
            whisper += 1
        if again or whisper:
            self.hub.log(f"hub: after the restart, {again} waiting for Amazon Transcribe sent to it again, "
                         f"{whisper} to the station's whisper")
        return again + whisper

    def _aws_sweep(self, now: float | None = None) -> int:
        """Anything still pending "aws" past its deadline (+ SWEEP_GRACE_S) is failed over, whatever became of
        its job (a thread gone, a job lost, a hub restart); one the station's whisper was asked for and that has
        no text FALLBACK_WAIT_S later says "transcription failed". How many it moved on."""
        now = time.time() if now is None else now
        n = self._flag_waiting(now)
        pending = self._aws_pending()
        ids = {item.id for item, _k, _kind in pending}
        for d in (self._aws_since, self._aws_asked):
            for cid in [c for c in d if c not in ids]:
                d.pop(cid, None)
        self._aws_late_wait.intersection_update(ids)
        for item, key, kind in pending:
            if self.sttpool.has(item.id):          # a GPU worker has it (#639): the pool's own clock falls it back
                continue
            asked = self._aws_asked.get(item.id)
            if asked is not None:
                if now - asked >= FALLBACK_WAIT_S:
                    self._aws_give_up(item, kind, (f"AWS timed out and sent no late answer in {FALLBACK_WAIT_S / 60:.0f} "
                                                   "min; it comes up if it arrives") if item.id in self._aws_late_wait else
                                      f"AWS failed and the station's whisper sent no text in "
                                      f"{FALLBACK_WAIT_S / 60:.0f} min; it comes up if it arrives")
                    n += 1
                continue
            since = self._aws_since.setdefault(item.id, now)    # (one nobody tracked: counted from now)
            clip_s = max(0.0, float(item.end) - float(item.start))
            limit = (self.aws.deadline_for(clip_s) if self.aws is not None else deadline_for(clip_s)) + SWEEP_GRACE_S
            if now - since < limit:
                continue
            why = f"AWS timed out: still no text after {now - since:.0f} s"
            self.hub.log(f"aws transcribe: {kind} {item.id}: {why} (the hub's sweep)")
            mine = (lambda x, cid=item.id: getattr(x, "id", None) == cid and getattr(x, "alt", None) is None
                    and not getattr(x, "rerun", False))
            if self.aws is None or not self.aws.give_up(mine, why):
                self._aws_failed(AwsJob(kind, item.id, key, page=item if kind == "page" else None), why)
            n += 1
        return n

    def _aws_give_up(self, item, kind: str, why: str, model: str = "") -> None:
        """Flag it: "Transcription failed" with why, never "Transcribing" for good. flagged: a text that still
        comes (the station's whisper, late) is taken as it would have been."""
        self._aws_asked.pop(item.id, None)
        self._aws_since.pop(item.id, None)
        note = f"{model or channels_model()} failed ({why[:160]})"
        self.hub.log(f"{'aws transcribe' if not model else 'speech-to-text'}: {kind} {item.id}: {why}: flagged as failed")
        item.flagged = True
        if kind == "page":
            item.status, item.model = "error", note
            self._stt_row(item.id, "error", note)
        else:
            self._retext(item.id, "", "error", None, note, why="transcription failed:")
        self.sync()

    def _flag_waiting(self, now: float) -> int:
        """A page waiting for the station's own whisper (not AWS) PENDING_FLAG_S after the hub got it: flagged."""
        with self._lock:
            waiting = [p for p in self.pages if p.status == "pending" and getattr(p, "stt", "") not in ("aws", "pool")]
        ids = {p.id for p in waiting}
        for cid in [c for c in self._pending_seen if c not in ids]:
            self._pending_seen.pop(cid, None)
        n = 0
        for p in waiting:
            seen = self._pending_seen.setdefault(p.id, now)
            if now - seen >= PENDING_FLAG_S:
                self._pending_seen.pop(p.id, None)
                self._aws_give_up(p, "page", f"no text from the station's speech-to-text in {PENDING_FLAG_S / 60:.0f} min "
                                             "(it may be behind: Settings > Station > Health); it comes up if it arrives",
                                  model="speech-to-text")
                n += 1
        return n

    def _aws_resend(self, now: float | None = None) -> int:
        """AWS can take audio again (a new month, a higher limit, the breaker closed): what is still waiting or
        failed (no text) from the last RESEND_S on a channel set to AWS goes to it, the newest first, at most
        RESEND_MAX. Its text, when it comes, takes the place of the failure."""
        from sizeup.transcribe import wav_pcm
        if self.aws is None:
            return 0
        now = time.time() if now is None else now
        with self._lock:
            pages = [(p, p.label, "page") for p in self.pages]
        items = pages + [(t, traffic_key(t), "traffic")
                         for t in list(self.store.loose) + [t for i in self.store.incidents for t in i.traffic]]
        todo = [(it, key, kind) for it, key, kind in items
                if it.end > now - RESEND_S and (it.status == "pending" or (it.status in ("error", "skipped") and not getattr(it, "text", "")
                                                                           and not getattr(it, "raw", "")))
                and self.channel_pref(key)["engine"] == "aws"
                and self.aws.available(key)[0]
                and (it.id not in self._aws_since or it.id in self._aws_asked)     # (not one AWS is doing now)
                and not self.sttpool.has(it.id)                                     # (nor one a GPU worker has)
                and self._rerun_note.get(it.id) != CATCHUP_NOTE]                    # (nor one the catch-up sent it)
        todo.sort(key=lambda x: -x[0].end)
        n = 0
        for it, key, kind in todo[:RESEND_MAX]:
            if not self.aws.available(key)[0]:
                continue
            wav = self.audio.get(it.id)
            pcm = wav_pcm(wav, getattr(it, "voice_offset_s", 0.0) if kind == "page" else 0.0) if wav else None
            if pcm is None:
                continue
            self._aws_since[it.id] = now
            self._aws_asked.pop(it.id, None)
            self.aws.submit(key, AwsJob(kind, it.id, key, page=it if kind == "page" else None), pcm)
            n += 1
        if n:
            self.hub.log(f"aws transcribe: AWS can take audio again: {n} waiting or failed from the last "
                         f"{RESEND_S / 3600:.0f} h sent to it")
        return n

    def _aws_check_resend(self) -> bool:
        """Either pool (ours, or the context pool's own budget: radio-intel PR 3) able to take audio again since the
        last sweep: what waited goes to AWS (_aws_resend sends each item only within its own pool). True if it did."""
        from sizeup.transcribe import CONTEXT
        if self.aws is None:
            return False
        now = {"": self.aws.available()[0], CONTEXT: self.aws.available("p25:")[0]}
        was = getattr(self, "_aws_pools_ok", {})
        back = any(ok and was.get(pool) is False for pool, ok in now.items())
        self._aws_pools_ok, self._aws_was_ok = now, now[""]
        if back:
            self._aws_resend()
        return back

    def _aws_watch(self) -> None:
        while not self._stopping.wait(SWEEP_S):
            try:
                self._aws_sweep()
                self._aws_check_resend()
            except Exception as e:  # the sweep must never die
                self.hub.log(f"hub: Amazon Transcribe sweep: {type(e).__name__}: {e}")

    # -- the GPU worker pool (#639, sizeup/sttpool.py) ---------------------------------------------------------------

    @staticmethod
    def _pool_wav(pcm) -> bytes:
        """A clip (int16, 8 kHz, as AWS takes it) as whisper-server takes it: 16 kHz with 0.5 s of silence each side,
        what the station's WhisperClient posts."""
        import numpy as np
        x = np.asarray(pcm, dtype=np.float32) / 32768.0
        if len(x) > 1:
            x = np.interp(np.arange(2 * len(x)) / 2.0, np.arange(len(x)), x).astype(np.float32)
        pad = np.zeros(8000, dtype=np.float32)
        return wav_bytes(np.concatenate([pad, x, pad]), 16000)

    def _pool_class(self, item, key: str) -> float:
        try:
            return self._stt_priority(item, key)
        except Exception:  # noqa: BLE001 - a talkgroup the hub can't place: live
            from sizeup.stt import LIVE
            return LIVE

    def _pool_first(self, item, pcm, key: str) -> bool:
        """An AWS-channel clip to a GPU worker first (it is shown as transcribing; AWS, then the station's whisper, if
        no worker gives its text in time). False: the pool can't take it now (no worker ready, off for its class, too
        busy to beat its wait): the caller goes the AWS way, nothing changed."""
        if pcm is None or getattr(self, "sttpool", None) is None:
            return False
        cls = self._pool_class(item, key)
        if not self.sttpool.ready(cls, gpu_only=True)[0]:     # (an AWS channel's: never a station's slower whisper)
            return False
        page = None if isinstance(item, Traffic) else item
        item.status, item.raw, item.confidence, item.model = "pending", "", None, sttpool.PENDING_MODEL
        item.stt = "aws"                          # (still waiting for the AWS path: the sweep and a restart know it)
        if page is None:
            item.__dict__.pop("transcript_pending", None)
            self._transcribed(item)
        else:
            self._save_page(item)
            self._stt_row(item.id, "pending", sttpool.PENDING_MODEL)
        try:                                      # (its vocab_v: the term list in this very prompt, or none)
            prompt, vocab_v = self._prompt_v(item)
        except Exception:  # noqa: BLE001 - no prompt is still a transcript
            prompt, vocab_v = "", ""
        job = sttpool.Job(item.id, cls, key, "page" if page is not None else "traffic", len(pcm) / 8000.0,
                          audio_wait=True, audio=self._pool_audio_of(item.id, page, pcm), on_done=self._pool_done,
                          on_fallback=self._pool_fallback, prompt=prompt, clip_end=self._hub_time(getattr(item, "end", None)),
                          gpu_only=True, extra={"pcm": pcm, "page": page, "vocab_v": vocab_v})
        if not self.sttpool.submit(job):          # not ready any more in between: the AWS way, the row shown already
            self._pool_to_aws(job, "the GPU pool couldn't take it")
        self.sync()
        return True

    def _pool_audio_of(self, cid: str, page, pcm):
        """A pool job's audio, read when its worker asks (on that request's thread, never the events'): the station's
        own whisper input (#671's stt-src, waited for up to POOL_AUDIO_WAIT_S: it comes on its own lane) at 16 kHz with
        0.5 s of silence each side, exactly what whisper-server would get on the station; else the listening clip."""
        def audio(wait_s: float = 0.0) -> bytes | None:
            import numpy as np
            sa = None
            try:
                sa = self.read_stt_audio(cid, float(getattr(page, "voice_offset_s", 0.0) or 0.0) if page is not None
                                         else 0.0, wait_s=min(POOL_AUDIO_WAIT_S, wait_s))
            except Exception as e:  # noqa: BLE001 - the listening clip then
                self.hub.log(f"stt pool: {cid}: no STT input ({type(e).__name__})")
            if sa is not None and sa.input == "stt-src" and sa.rate == 16000:
                pad = np.zeros(8000, dtype=np.float32)
                return wav_bytes(np.concatenate([pad, np.asarray(sa.pcm, dtype=np.float32) / 32768.0, pad]), 16000)
            if pcm is not None:
                return self._pool_wav(pcm)
            return self._pool_wav(sa.pcm) if sa is not None and sa.rate == 8000 else None
        return audio

    def pool_word(self, sid: str = "") -> dict:
        """The pool as a station hears it in its state answer (#639 phase 2): on (it may hand clips over and claim
        some), worker (its whisper may claim), and ready: how many workers other than its own whisper could take a
        context clip now (a station hands one over at once only when one could)."""
        reg = self.hub.workers
        try:
            word = self.sttpool.word(sid)
        except Exception:  # noqa: BLE001 - none
            word = {"ready": 0, "max_s": 0.0, "gpu_idle": False, "gpu_idle_n": 0}
        return {"on": reg.enabled(sttpool.LOW), "worker": reg.stations_on(sid), **word}

    def _pool_clip(self, item, key: str, wav: bytes | None, cls_name=None, fresh: bool = True) -> None:
        """A clip a station handed to the pool (#639 phase 2: its whisper too busy to keep it, or the station stopping):
        shown as transcribing, a pool job (any worker: a GPU worker first, then an idle station's whisper), and when
        none takes it, the next way (_pool_elsewhere). Never dropped. fresh=False: one the pool held before a restart."""
        from sizeup.transcribe import wav_pcm
        page = None if isinstance(item, Traffic) else item
        pcm = wav_pcm(wav, getattr(item, "voice_offset_s", 0.0) if page is not None else 0.0) if wav else None
        cls = self._pool_class(item, key)
        if fresh:
            item.status, item.raw, item.confidence, item.model = "pending", "", None, sttpool.PENDING_MODEL
            item.stt = "pool"
            if page is None:
                item.__dict__.pop("transcript_pending", None)
                self._transcribed(item)
            else:
                self._save_page(item)
                self._stt_row(item.id, "pending", sttpool.PENDING_MODEL)
        try:                                      # (its vocab_v: the term list in this very prompt, or none)
            prompt, vocab_v = self._prompt_v(item)
        except Exception:  # noqa: BLE001 - no prompt is still a transcript
            prompt, vocab_v = "", ""
        seconds = len(pcm) / 8000.0 if pcm is not None else max(0.0, float(item.end) - float(item.start))
        job = sttpool.Job(item.id, cls, key, "page" if page is not None else "traffic", seconds,
                          audio_wait=True, audio=self._pool_audio_of(item.id, page, pcm), on_done=self._pool_done,
                          on_fallback=self._pool_fallback, prompt=prompt, clip_end=self._hub_time(getattr(item, "end", None)),
                          extra={"pcm": pcm, "page": page, "handed": True,
                                 "origin": str(getattr(item, "origin_station", "") or self.clip_origin),
                                 "vocab_v": vocab_v})
        if not self.sttpool.submit(job):
            return self._pool_elsewhere(job, "no pool worker could take it now")
        self.sync()

    def _pool_elsewhere(self, job: sttpool.Job, why: str) -> None:
        """A handed-over clip no pool worker transcribed: a live one to AWS (its budget allowing), else to the station's
        whisper at live priority (it waits for a station restarting to come back); a context or catch-up one marked
        shed, so the catch-up runs it once there is room. Nothing is dropped."""
        from sizeup.stt import HIGH, LIVE, SHED, URGENT
        page = job.extra.get("page")
        item = page if job.kind == "page" else self._find(job.id)[0]
        if item is None or getattr(item, "raw", "") or getattr(item, "text", "") or \
                getattr(item, "status", "") not in ("pending", "remote"):
            return
        if job.cls in (URGENT, HIGH, LIVE):         # (a context class never at live priority, never past the
                                                   # catch-up's own AWS cap: the catch-up takes it, CATCHUP_AWS_PCT)
            item.stt = "aws"                       # (the AWS path's sweep and a hub restart know it from here)
            if job.extra.get("pcm") is not None and self.aws is not None and self.aws.available(job.channel)[0]:
                self.hub.log(f"stt pool: {job.kind} {job.id}: {why}: Amazon Transcribe")
                return self._pool_to_aws(job, why)
            self.hub.log(f"stt pool: {job.kind} {job.id}: {why}: the station's whisper")
            self._aws_failed(AwsJob(job.kind, job.id, job.channel, page=page, recovered=True), f"pool: {why[:80]}")
            return self.sync()
        self.hub.log(f"stt pool: {job.kind} {job.id}: {why}: left for the catch-up")
        self._stt_state(item, job.id, "skipped", f"{sttpool.PENDING_MODEL} ({SHED}: {why[:80]}; the catch-up runs it)")
        item.stt = ""
        self.sync()

    def _pool_recover(self) -> int:
        """After a restart: what the pool held for a station (stt "pool", still pending) goes the pool's way again (the
        pool empty at start: the next way, _pool_elsewhere). How many."""
        with self._lock:
            pages = [(p, p.label) for p in self.pages if p.status == "pending" and getattr(p, "stt", "") == "pool"]
        items = pages + [(t, traffic_key(t)) for t in list(self.store.loose) + [t for i in self.store.incidents
                                                                              for t in i.traffic]
                         if t.status == "pending" and getattr(t, "stt", "") == "pool"]
        for item, key in items:
            self._pool_clip(item, key, self.audio.get(item.id), fresh=False)
        if items:
            self.hub.log(f"hub: after the restart, {len(items)} clip(s) the pool held sent on again")
        return len(items)

    def _hub_time(self, t) -> float | None:
        """A station time on the hub's clock."""
        return float(t) + float(getattr(self.hub, "skew", 0.0) or 0.0) if isinstance(t, (int, float)) else None

    def _pool_done(self, job: sttpool.Job, res: dict) -> None:
        """A GPU worker's text: judged like the station's whisper (no speech, hallucinations), the raw words kept
        (stt_runs), then the same way AWS's text goes: the page read, the transmission's text, the catch-up's row."""
        raw = str(res.get("text") or "")
        probs = [float(w[1]) for w in res.get("words") or []
                 if isinstance(w[1], (int, float)) and math.isfinite(w[1]) and 0.0 <= w[1] <= 1.0]
        conf = round(sum(probs) / len(probs), 2) if probs else None
        vocab_v = str(job.extra.get("vocab_v") or "")       # a term list in its prompt: the read-back guard too
        text, status = judge(raw, job.seconds, prompt=job.prompt, vocab_v=vocab_v)
        kind = str(job.extra.get("kind") or "gpu")
        model = sttpool.model_label(str(res.get("model") or ""), job.worker, kind)
        catchup = bool(job.extra.get("catchup"))
        done_at = time.time()
        if catchup:
            changed = self._retext(job.id, text, status, conf, model, why="pool worker catch-up", quiet=True,
                                   vocab_v=vocab_v)
            self._rerun_finish(job.id, "changed" if changed or text else "same", model)
        elif job.kind == "page" and job.extra.get("page") is not None:
            page = job.extra["page"]
            page.raw, page.confidence, page.status, page.model = text, conf, status, model
            page.stt = ""
            page.vocab_v = vocab_v  # type: ignore[attr-defined]
            self._transcribed(page)
        else:
            self._retext(job.id, text, status, conf, model, why="pool worker", vocab_v=vocab_v)
        self._aws_since.pop(job.id, None)
        self._aws_asked.pop(job.id, None)
        self.sync()
        self._stt_ran(job.id, "station-whisper" if kind == "station" else "gpu-worker", str(res.get("model") or ""),
                      raw, status, conf, key=job.channel,                                                      # (after
                      item=job.extra.get("page"), role="catchup" if catchup else "text", words=res.get("words"),  # the text)
                      queued_at=job.queued_at, engine_start=job.claimed_at, worker=job.worker,
                      prompted=stt_time.prompted(job.prompt, job.clip_end),     # (#727)
                      wait_s=job.claimed_at - job.queued_at if job.claimed_at else None,
                      run_s=done_at - job.claimed_at if job.claimed_at else None,
                      prompt_v=sttruns.prompt_version(job.prompt), cls=job.cls, clip_end=job.clip_end, vocab_v=vocab_v)

    def _pool_fallback(self, job: sttpool.Job, why: str) -> None:
        """No text from the pool in time: an AWS-channel clip the AWS way (and the station's whisper if AWS can't). A
        catch-up clip: back to the catch-up, no try burnt, when nobody took it (no worker, unclaimed, handed back); a
        lease lost or a result that couldn't be applied is a failed try on its row (a clip that kills workers ends)."""
        if job.extra.get("catchup"):
            if job.extra.get("fell") in ("lease", "error"):
                self.hub.log(f"stt pool: catch-up {job.id}: {why}: a failed try")
                self._rerun_finish(job.id, f"failed: GPU worker: {why[:80]}")     # ("; catch-up try N": its note)
                return self.sync()
            self.hub.log(f"stt pool: catch-up {job.id}: {why}: back to the catch-up")
            self._rerun_note.pop(job.id, None)
            self.catchup.forget(job.id)
            return
        if job.extra.get("handed"):               # a station handed it over (busy, or restarting): the next way on
            return self._pool_elsewhere(job, why)
        self.hub.log(f"stt pool: {job.kind} {job.id}: {why}: Amazon Transcribe")
        self._pool_to_aws(job, why)

    def _pool_to_aws(self, job: sttpool.Job, why: str) -> None:
        """The AWS path for a clip that was the pool's (its row already says it is transcribing): AWS if it can take
        it, else _aws_failed (the station's whisper, or "transcription failed")."""
        page = job.extra.get("page")
        item = page if job.kind == "page" else self._find(job.id)[0]
        if item is None:
            return
        if (getattr(item, "raw", "") or getattr(item, "text", "")
                or getattr(item, "status", "") not in ("pending", "remote")):
            return                                 # its text came meanwhile
        aws_job = AwsJob(job.kind, job.id, job.channel, page=page)
        ok, awhy = self.aws.available(job.channel) if self.aws is not None else \
            (False, "Amazon Transcribe isn't set up on the hub")
        if not ok:
            self._aws_failed(aws_job, f"GPU worker: {why[:80]}; {awhy}")
            return self.sync()
        self._aws_since[job.id] = time.time()
        self._aws_asked.pop(job.id, None)
        self._stt_state(item, job.id, "pending", channels_model())
        self.aws.submit(job.channel, aws_job, job.extra["pcm"])
        self.sync()

    def _catchup_route(self, key: str) -> str:
        """The GPU pool first for a shed clip while a worker is ready (#639), else as before (AWS on a context channel
        with budget, else the station's whisper)."""
        how = super()._catchup_route(key)
        # (a worker other than the clip's own station: its own whisper takes its own clips the catch-up's way, with its
        # quiet spell and its idleness rules)
        if how and getattr(self, "sttpool", None) is not None and self.sttpool.ready(CATCHUP, origin=self.clip_origin)[0]:
            return "pool"
        return how

    def _catchup_pool(self, row: dict) -> tuple[bool, str]:
        """One shed clip to the GPU pool (quiet: old words alert nothing). (sent, why it never can be)."""
        cid, key = str(row.get("id") or ""), str(row.get("key") or "")
        if not CLIP_ID.fullmatch(cid) or self.sttpool.has(cid):
            return False, ""
        pcm = self._catchup_pcm(cid, key, str(row.get("source") or ""))
        if pcm is None:
            return False, "no recording"
        item = self._find(cid)[0]
        try:                                      # (its vocab_v: the term list in this very prompt, or none)
            prompt, vocab_v = self._prompt_v(item) if item is not None else ("", "")
        except Exception:  # noqa: BLE001 - no prompt is still a transcript
            prompt, vocab_v = "", ""
        end = row.get("start")
        job = sttpool.Job(cid, CATCHUP, key, "traffic", len(pcm) / 8000.0, audio_wait=True, audio=self._pool_audio_of(cid, None, pcm),
                          on_done=self._pool_done, on_fallback=self._pool_fallback, prompt=prompt,
                          clip_end=self._hub_time(float(end) + float(row.get("seconds") or 0.0))
                          if isinstance(end, (int, float)) else None,
                          extra={"pcm": pcm, "catchup": True, "origin": self.clip_origin, "vocab_v": vocab_v})
        self._rerun_note[cid] = CATCHUP_NOTE
        if not self.sttpool.submit(job):
            self._rerun_note.pop(cid, None)
            return False, ""
        return True, ""

    def _stt_ran(self, cid: str, engine: str, model: str, text: str, status: str, conf, *, key: str = "", item=None,
                 prompted: bool | None = None,
                 role: str = "text", words=None, queued_at=None, engine_start=None, worker: str = "", prompt_v: str = "",
                 cls=None, clip_end=None, wait_s=None, run_s=None, vocab_v: str | None = None) -> None:
        """A transcript as an engine wrote it, kept (sizeup/sttruns.py), and its latency (transmission end -> text
        here) counted for Health when it is the text of record. Never in the way of the transcript."""
        try:
            if item is None:
                item = self._find(cid)[0]
            if status == stt_quality.UNCLEAR and not text:      # (the item's text is "": the engine's words, kept apart)
                text = str(getattr(item, "unclear_text", "") or "")
            elif status == "done" and text and stt_quality.assess(text, App._clip_seconds(item) if item is not None
                                                                   else None):
                status = stt_quality.UNCLEAR                    # (a loop, noise: the run says so, with its engine)
            if not key and item is not None:
                key = traffic_key(item) if isinstance(item, Traffic) else str(getattr(item, "label", "") or "")
            if clip_end is None and item is not None:
                clip_end = self._hub_time(getattr(item, "end", None))
            if cls is None and item is not None:
                cls = self._pool_class(item, key)
            check = self._time_check(key, item, text, clip_end, engine, prompted)
            now = time.time()
            if not self._may_write_audio():      # a standby (or a lease running out) writes nothing (#501)
                return
            if vocab_v is None:                  # (the item's own: whisper's prompt or AWS's stream, sizeup/stt_terms.py)
                vocab_v = str(getattr(item, "vocab_v", "") or "")
            self.stt_runs.add(cid, engine, text=text, status=status, model=model, worker=worker, prompt_v=prompt_v,
                              vocab_v=vocab_v,
                              role=role, confidence=conf if isinstance(conf, (int, float)) else None, words=words,
                              channel=key, cls=sttpool.class_key(cls) if cls is not None else "", clip_end=clip_end,
                              queued_at=queued_at, engine_start=engine_start, done_at=now, wait_s=wait_s,
                              run_s=run_s if run_s is not None else (now - engine_start if engine_start else None),
                              keyup=getattr(item, "keyup", None),     # (a cut transmission's piece, the radio's split)
                              call_id=str(getattr(item, "call_id", "") or ""), turn=getattr(item, "turn", None),
                              check=check)
            if role == "text" and clip_end is not None:
                self.stt_latency.add(key, engine, now - clip_end, target=cls in sttruns.TARGET_CLASSES)
        except Exception as e:  # noqa: BLE001 - bookkeeping
            self.hub.log(f"stt runs: {cid}: {type(e).__name__}: {e}")

    def _time_check(self, key: str, item, text: str, clip_end, engine: str = "",
                    prompted: bool | None = None) -> stt_time.Check | None:
        """The dispatcher's trailing time (#727, sizeup/stt_time.py) on a configured channel, the dispatcher's (or a
        talker-less) transmission only: kept with the run, the raw text never changed. None: not checked (no clip
        found here, no end). prompted: the engine's prompt carried the clip's time (a GPU worker's: from its job's
        prompt); None: the station's whisper says so on the clip (stt_prompt_time), any other engine had none. Never in
        the way of the run: a failure is no check."""
        try:
            if item is None or not text or not stt_time.applies(key, str(getattr(item, "unit", "") or ""),
                                                                 self.cfg.stt_time_channels):
                return None
            if prompted is None:
                prompted = engine == "station-whisper" and bool(getattr(item, "stt_prompt_time", False))
            return stt_time.check(text, clip_end, prompted=bool(prompted))
        except Exception as e:  # noqa: BLE001 - bookkeeping
            self.hub.log(f"stt runs: trailing time: {type(e).__name__}: {e}")
            return None

    def _prompt_time(self, cid: str, d: dict) -> None:
        """The station's retext says whether whisper's prompt carried the clip's own time: kept on the transmission
        for its run's trailing-time check (_time_check)."""
        item = self._find(cid)[0]
        if item is not None:
            try:
                item.stt_prompt_time = bool(d.get("stt_prompt_time"))
            except AttributeError:
                pass

    def stt_time_health(self) -> dict | None:
        """Settings > Station > Health: the trailing-time match rate per channel, engine, model (and vocab_v), 24 h and
        TIME_DAYS days, a day at a time (sizeup/sttruns.py Runs.time_rates). None with nothing checked."""
        rows = self.stt_runs.time_rates()
        if not rows:
            return None
        info, names = self.cfg.p25_tg_info or {}, self.cfg.p25_talkgroups or {}
        for r in rows:                         # (a county talkgroup's name: "Fire/EMS 15")
            ch = str(r.get("channel") or "")
            n = int(ch[3:]) if ch.startswith("tg:") and ch[3:].isdigit() else None
            r["name"] = ((info.get(n) or {}).get("name") or names.get(n) or ch) if n is not None else ch
        return {"rows": rows, "rule": stt_time.RULE, "window_min": list(stt_time.WINDOW_MIN), "days": sttruns.TIME_DAYS}

    def _clips_logged(self, ids: list[str]) -> set:
        """Of these clip ids, the ones still in the radio log (a run goes with its transcript)."""
        out: set = set()
        for i in range(0, len(ids), 400):
            part = ids[i:i + 400]
            rows = self.cad.store.q(f"SELECT id FROM radio_traffic WHERE id IN ({', '.join('?' * len(part))})", tuple(part))
            out.update(r["id"] for r in rows)
        return out

    def _worker_live(self, wid: str) -> dict:
        """A worker's live state for Settings > Station > STT workers."""
        w = self.sttpool.workers.get(wid)
        if w is None:
            return {"state": "offline"}
        now = self.sttpool.clock()
        lw = self.infer_lane.workers.get(wid)
        return {"state": self.sttpool.state_of(w, now), "model": w.model, "seen": w.seen, "gpu": dict(w.gpu),
                "version": w.version, "llm": lw.llm if lw is not None else "", **self._update_view(wid, w.update)}

    def _update_view(self, wid: str, update: dict) -> dict:
        """A GPU worker's self-update for Settings and Health: what it runs (update), what the hub wants it on
        (update_wanted: a sha256, "" for none) and whether that is what it runs (update_current; None: it can't say)."""
        if not update:
            return {}
        wanted = self.stt_api.wanted(wid).get("sha256", "")
        return {"update": dict(update), "update_wanted": wanted,
                "update_current": (update.get("sha256") == wanted) if wanted and update.get("sha256") else None}

    def pool_health(self) -> dict | None:
        """Settings > Station > Health: the pool (its workers, queue, fallbacks, p50/p90) and every channel's latency.
        None while no worker was ever added and no transcript timed (nothing to show)."""
        lat = self.stt_latency.table()
        active = self.hub.workers.any() or bool(self.sttpool.workers)
        if not active and not self.sttpool.workers and not lat and self.stt_runs.health() is None:
            return None
        stats = self.sttpool.stats()
        for w in stats["workers"]:
            w |= self._update_view(w["id"], w.get("update") or {})
        return {"active": active, "settings": self.hub.workers.pool_settings(), **stats,
                "infer": self.infer_lane.stats() if self.infer_lane.any_llm() else None,
                "latency": lat, "target": {"p50_s": sttruns.TARGET_P50_S, "p95_s": sttruns.TARGET_P95_S},
                "copies": self.stt_runs.health()}      # (stt.sqlite's copies in the bucket: unsettled since, last copy)

    # -- Settings > Radios > Channels on the station ----------------------------------------------------

    def channels_changed(self) -> None:
        super().channels_changed()
        self.channels_check(force=True)

    def channel_desired(self) -> dict:
        ok, why = self.aws_ok()
        reserve = [] if ok or self.aws is None else [k for k in self.aws.s.reserve_channels if self.aws.available(k)[0]]
        # another P25 system's police (radio-intel PR 3): its own budget, apart from ours either way
        more, deny, deny_why = self.aws_split(ok)
        reserve += more
        if not self._deny_ok():
            # a station from before deny would never echo it in its signature (a push every 10 s): it isn't sent; that
            # station runs its police channel on our pool's word until it updates
            deny = []
        prefs = channels.for_station(self._chan_ov, self.cfg.stt_record_only)   # + the hub's record_only (#201)
        if "p25_systems" not in (self.hub.status or {}):
            # a station from before #409 refuses a whole map with a "p25:<system>:<tg>" key in it: left out for it
            prefs = {k: v for k, v in prefs.items() if not k.startswith("p25:")}
        # each FM channel's squelch: only to a station that takes it (one before it refuses a 4th argument)
        sq = None
        if self._squelch_ok():          # what of it fits the station's channels, as it reported them (the station agrees)
            kinds = {r.get("label"): r.get("channel_kind") for r in self.hub.radio_rows() if r.get("channel_kind")}
            sq = channels.fit_squelch(self._sq_ov, kinds, self.squelch_defaults)[0]
        # the county talkgroups an incident of ours is worked on now (sizeup/cad/opchannel.py): only to a station that
        # takes them (one from before would never echo them in its signature: a push every 10 s)
        st_ch = _part(self.hub.status, "channels") or {}
        promote = sorted((k for k in self.promoted_keys()
                          if k.startswith("tg:") or (k.startswith("p25:") and st_ch.get("promote_p25_ok"))),
                         key=lambda k: (not k.startswith("tg:"), k))[:channels.AWS_KEYS_MAX] if st_ch.get("promote_ok") else []
        # (our incidents' talkgroups first: past AWS_KEYS_MAX a crash pre-alert's channel goes, never one of ours)
        # (another system's talkgroup, a crash pre-alert's: only to a station that takes "p25:" keys in promote)
        since = {**self.intel_promoted(), **self.cad.opchan.promoted_since()} if promote else {}
        aws = {"ok": ok, "why": why, "reserve": reserve, **({"deny": deny, "deny_why": deny_why} if deny else {}),
               **({"promote": promote, "promote_since": {k: since[k] for k in promote if k in since}} if promote else {})}
        return {"prefs": prefs, "aws": aws, "tones": dict(self._tone_ov),
                "squelch": sq, "sig": channels.sig(prefs, ok, self._tone_ov, reserve, sq, deny, promote)}

    def promoted_changed(self) -> None:
        """An incident's operating channel came or went: the station hears it now (its whisper class, its engine)."""
        super().promoted_changed()
        if getattr(self, "hub", None) is not None:
            self.channels_check(force=True)

    def _aws_word_log(self, ok: bool, why: str, reserve: list, deny: list, deny_why: str) -> None:
        """A channel denied AWS (the monthly budget, the breaker; police past its own budget) is said in the log when
        it starts and when it ends (#639: the baseline's gap), never per clip."""
        word = (bool(ok), tuple(sorted(str(k) for k in reserve)) if not ok else (), tuple(sorted(str(k) for k in deny)))
        was = getattr(self, "_aws_word", None)
        self._aws_word = word
        if was is None or was == word:
            if was is None and (not ok or deny):
                self.hub.log(f"aws transcribe: denied now: {'our channels' if not ok else ''}"
                             f"{' and ' if not ok and deny else ''}{', '.join(word[2])}"
                             f" ({why if not ok else deny_why}); reserve: {', '.join(word[1]) or 'none'}")
            return
        if was[0] != word[0]:
            self.hub.log("aws transcribe: our channels " + ("may use AWS again" if ok else
                         f"denied AWS ({why}): the station's whisper; reserve kept: {', '.join(word[1]) or 'none'}"))
        if was[2] != word[2]:
            self.hub.log("aws transcribe: " + (f"denied AWS on its own budget: {', '.join(word[2])} ({deny_why})"
                                               if word[2] else "no channel denied on its own budget any more"))

    def squelch_defaults(self, key: str) -> dict:
        """The station's receiver defaults, as it reported them (its config's, not the hub's)."""
        hub = getattr(self, "hub", None)          # (None while App's __init__ runs)
        rows = hub.radio_rows() if hub is not None else []
        row = next((r for r in rows if r.get("label") == key), None) or {}
        d = row.get("squelch_default")
        return {f: d.get(f) for f in channels.SQUELCH} if isinstance(d, dict) else super().squelch_defaults(key)

    def _deny_ok(self) -> bool:
        return bool((_part(self.hub.status, "channels") or {}).get("deny_ok"))

    def _squelch_ok(self) -> bool:
        return bool((_part(self.hub.status, "channels") or {}).get("squelch_ok"))

    def channels_check_tones(self) -> None:
        """A pair named here: the station gets it with the next channel push (the signature changed)."""
        self.channels_check(force=True)

    def alert_keys_check(self) -> None:
        """The station has another revision of the pre-alerts' sources than the intel engine's (its status says which):
        sent (set_stt_alert_keys, a call job; one at a time). A station from before this reports none and is never sent
        them (it refuses the call)."""
        if not self._may_write_audio():                # a standby sends nothing (only the leader's word counts)
            return
        keys, rev = self._stt_alert_source()
        st = self.hub.status or {}
        if "stt_alert_rev" not in st or st.get("stt_alert_rev") == rev or not self.hub.connected():
            return
        if getattr(self, "_alert_push", False):
            return
        self._alert_push = True

        def push() -> None:
            try:
                self.hub.remote("set_stt_alert_keys", sorted(keys), rev, timeout=CALL_WAIT_S)
            except Exception as e:  # noqa: BLE001 - again at the next state post
                self.hub.log(f"hub: the pre-alerts' sources not sent to the station: {str(e)[:120]}")
            finally:
                self._alert_push = False
        threading.Thread(target=push, name="alert-keys", daemon=True).start()

    def channels_check(self, force: bool = False) -> None:
        """Send the station these choices when what it reports differs (a change here, a station that was
        offline, AWS's limit reached or a new month): one push at a time, at most every 10 s unless forced."""
        want = self.channel_desired()
        a = want["aws"]
        self._aws_word_log(a["ok"], a["why"], a["reserve"], a.get("deny") or [], a.get("deny_why") or "")
        got = _part(self.hub.status, "channels") or {}
        if got.get("sig") == want["sig"] or not self.hub.connected():
            return
        now = time.time()
        with self._lock:
            if self._chan_push["busy"]:
                self._chan_push["again"] = self._chan_push["again"] or force    # a change while one is on its way
                return
            if not force and now - self._chan_push["at"] < 10:
                return
            self._chan_push.update(busy=True, at=now, again=False)

        def push() -> None:
            try:
                res = self.hub.remote("set_channel_prefs", want["prefs"], want["aws"], want["tones"],
                                      *([want["squelch"]] if want["squelch"] is not None else []))
                if isinstance(res, dict) and self.hub.status is not None \
                        and statusshape.problem({"channels": res}) is None:      # (of the shape a status' is)
                    self.hub.status["channels"] = res
                self._chan_push["error"] = ""
            except (StationOffline, RuntimeError) as e:
                self._chan_push["error"] = str(e)
                self.hub.log(f"hub: channel choices not applied on the station: {e}")
            finally:
                with self._lock:
                    self._chan_push["busy"] = False
                    again, self._chan_push["again"] = self._chan_push["again"], False
                if again:
                    self.channels_check(force=True)
        threading.Thread(target=push, name="hub-channels", daemon=True).start()

    def stt_terms_check(self) -> None:
        """The station is sent the private term lists' top (whisper's prompt, sizeup/stt_terms.py) when the copy it reports
        differs: after a change here, or a station that restarted with none. A station from before this refuses the
        call: tried again a minute later, said once."""
        push = self.__dict__.get("_terms_push")
        if push is None:
            push = self._terms_push = stt_terms.StationPush(
                self.stt_terms_payload, lambda p: self.hub.remote("set_stt_terms", p), log=self.hub.log)
        push.check(_part(self.hub.status, "stt_terms"), self.hub.connected())

    def channels_sync(self) -> dict:
        want = self.channel_desired()
        got = _part(self.hub.status, "channels") or {}
        if not self.hub.status:
            return {"state": "offline", "detail": "No station has reported yet: these apply when it connects"}
        if got.get("sig") == want["sig"] and self._sq_ov and not self._squelch_ok():
            return {"state": "pending", "detail": "The station runs an older sizeup: update it to apply the squelch"}
        if got.get("sig") == want["sig"]:
            return {"state": "applied", "detail": "Applied on the station"}
        if not self.hub.connected():
            return {"state": "offline", "detail": f"Station offline since {_hhmm(self.hub.last_seen)}: "
                                                  "these apply when it's back"}
        if "sig" not in got:
            return {"state": "pending", "detail": "The station runs an older sizeup: update it to apply these"}
        err = self._chan_push.get("error")
        return {"state": "pending", "detail": "Waiting for the station" + (f" ({err})" if err else "")}

    def sync(self) -> None:
        with self._lock:
            docs = [self._page_doc(p) for p in self.pages]
        self.mdb.sync(self.store, docs)

    # -- what App does with local radios -----------------------------------------------------------------

    def clip(self, cid: str) -> bytes | None:
        if not CLIP_ID.fullmatch(cid) or ".." in cid or cid.endswith(STT_SRC):   # (an STT input is no clip to play)
            return None
        return self.audio.get(cid)

    def has_clip(self, cid: str) -> bool:
        return not cid.endswith(STT_SRC) and self.audio.has(cid)

    # -- the speech-to-text input (sizeup/cloud/sttinput.py) -------------------------------------------------------

    def put_stt_input(self, it: dict, station: str = "") -> bool:
        """One clip's STT input from POST /api/station/stt, beside its clip (the same channel, talkgroup and time: the
        same retention), whether its event came already or not. False: not one to keep (refused for good, logged; its
        STT consumers get the listening clip). A write the fence refused is raised (nothing acked). Its arrival after
        its event is measured (#677, sizeup/cloud/sttarrival.py), under `station` (the post's)."""
        try:
            cid = self._id(it)
            kind = it.get("kind")
            if kind not in ("traffic", "page"):
                raise ValueError(f"kind {str(kind)[:20]!r}")
            data, meta = sttinput.from_wire(it.get("stt"))
        except ValueError as e:
            self._stt_say(f"hub: speech-to-text input refused: {str(e)[:120]}")
            return False
        d = dict(it)
        if kind == "traffic" and d.get("source") in RENAMED and d["source"] not in self._kind \
                and RENAMED[d["source"]] in self._kind:
            d["source"] = RENAMED[d["source"]]                  # (#275, as apply() does for its event)
        end = d.get("end")
        at = float(end) if isinstance(end, (int, float)) and not isinstance(end, bool) else time.time()
        where = _where(d) if kind == "traffic" else {"channel": str(d.get("label") or "")}
        refused = fence_mod.refused()
        try:
            sttinput.store(self.audio, cid, data, meta, at, **where)
        except Exception as e:  # noqa: BLE001 - a store that fails never costs anything else (its clip is read)
            if isinstance(e, fence_mod.FencedWrite) or fence_mod.refused() != refused:
                raise
            self._stt_say(f"hub: speech-to-text input for {cid} not stored: {type(e).__name__}: {str(e)[:120]}")
            return False
        status = self.hub.status if isinstance(getattr(self.hub, "status", None), dict) else {}
        self.stt_arrival.blob(cid, station or str(status.get("id") or ""), kind, where.get("channel") or "")
        return True

    def stt_arrival_flush(self) -> int:
        """The STT input arrival delays measured since (#677) into the station metrics (kind "stt_in", the subject the
        channel): graphed on the Signal tab, Health's p50 / p95. On the metrics-backup thread, never a request's (a
        standby's write refused there must not fail a post): on a standby they are dropped (the leader measures its
        own). The records added."""
        got = self.stt_arrival.take()
        if not got:
            return 0
        n = 0
        for station, recs in got.items():
            try:
                n += int(self.metric_store.ingest(station, recs, tell=False).get("accepted") or 0)
            except fence_mod.FencedWrite:
                pass
            except Exception as e:  # noqa: BLE001 - a measurement, never in anything's way
                self._stt_say(f"hub: STT input arrival not kept: {type(e).__name__}: {str(e)[:120]}")
        if n:
            self._stt_arrival_cache = None
        return n

    def stt_arrival_stats(self, now: float | None = None) -> dict | None:
        """Health: the STT inputs' arrival after their events, p50 / p95 per kind and channel over 1 h and 24 h (#677).
        Read from the metrics store at most once a minute."""
        from sizeup.cloud import sttarrival
        now = time.time() if now is None else now
        hit = getattr(self, "_stt_arrival_cache", None)
        if hit is not None and 0 <= now - hit[0] < 60.0:
            return hit[1]
        out = sttarrival.stats(self.metric_store, now)
        self._stt_arrival_cache = (now, out)
        return out

    def stt_stored(self) -> None:
        """STT inputs were stored: wake read_stt_audio's waiters (once per post)."""
        with self._stt_cond:
            self._stt_cond.notify_all()

    def _stt_say(self, msg: str) -> None:
        now = time.monotonic()
        if now - getattr(self, "_stt_said", -1e9) >= 60.0:
            self._stt_said = now
            self.hub.log(msg)

    def read_stt_audio(self, cid: str, voice_offset_s: float | None = None,
                       wait_s: float = 0.0) -> sttinput.SttAudio | None:
        """The audio a speech-to-text engine on the hub should take for this transmission or page (Amazon Transcribe,
        a GPU worker): the station's own whisper input ("stt-src") when it came, else the listening clip ("listening-8k",
        a page's from its voice on), else None. voice_offset_s: a page's (None: looked up among the pages here).
        wait_s: the STT input comes on its own lane, usually a moment after its event: wait up to this long for it
        before taking the listening clip (0: never wait)."""
        if not CLIP_ID.fullmatch(cid) or ".." in cid:
            return None
        if voice_offset_s is None:
            with self._lock:
                page = next((p for p in self.pages if p.id == cid), None)
            voice_offset_s = float(page.voice_offset_s) if page is not None else 0.0
        wait_s = min(max(0.0, float(wait_s or 0.0)), STT_WAIT_MAX_S)
        if wait_s > 0:
            with self._stt_cond:
                self._stt_cond.wait_for(lambda: self.audio.has(sttinput.src_id(cid)), timeout=wait_s)
        return sttinput.read(self.audio, cid, voice_offset_s, log=self.hub.log)

    def radio_status(self) -> list[dict]:
        return self.hub.radio_rows()

    def sdr(self) -> dict | None:
        return self.hub.sdr()

    def state(self) -> dict:
        st = super().state()
        st["talkgroups"] = self.hub.talkgroups
        st["p25_systems"] = _part(self.hub.status, "p25_systems", list) or []      # (#409) the station's other P25 systems
        st["rerun"] = self.hub.rerun()
        st["cloud"] = self.hub.cad_cloud()
        return self._hide_context(st)       # (#419: the station's lists too, where nobody is signed in)

    def _sources_poll_now(self, reason: str) -> None:
        """A radio pre-alert or an airport alert on the hub: the station's adapters poll now (a job; never waits, never
        raises). Never the phone ring."""
        def send() -> None:
            try:
                self.hub.remote("sources_poll_now", reason, timeout=10.0)
            except Exception:  # noqa: BLE001 - offline or an older station: it polls on its own schedule
                pass
        if self.hub.connected():
            threading.Thread(target=send, name="sources-poll-now", daemon=True).start()

    def close_incident(self, iid: str) -> bool:
        ok = super().close_incident(iid)
        self.sync()
        return ok

    def _switch(self, fn: str, *args) -> str | None:
        try:
            return self.hub.remote(fn, *args)
        except StationOffline as e:
            return f"{e}: switches work when it's back"
        except RuntimeError as e:
            return str(e)

    def set_mode(self, mode: str, sdr: str | None = None, confirm: bool = False) -> str | None:
        if confirm:                                    # (#409: a fire job's dongle to another system's job)
            return self._switch("set_mode", mode, sdr, "person", True)
        return self._switch("set_mode", mode, sdr)

    def _reception_send(self, body: dict, tid: str) -> str | None:
        """Settings > Radios here: a reception test starts, stops or is annotated on the station (#410). The station
        is sent the test's id only: who asked stays here (App.reception_post), for the row when it ends."""
        return self._switch("reception_post", body, "", tid)

    def reception_state(self) -> dict | None:
        """The station's reception tests as it last reported them (admins only: server.py); None while it is away."""
        rec = _part(self.hub.status, "reception")
        return rec if rec is not None and self.hub.connected() else None

    def reception_seen(self, rec) -> None:
        """The station's report: each test that ended since the last one, and each paused job that came back late,
        is a row in the activity of who started it, once (the hub's Stop doesn't wait for the restore; this is where
        its outcome is recorded)."""
        from sizeup.radio import reception
        rows = rec.get("history") if isinstance(rec, dict) else None
        if not isinstance(rows, list):
            return
        try:
            done = set(json.loads(self.cad.store.meta_get("reception_audited") or "[]"))
        except (ValueError, TypeError):
            done = set()
        new = []
        for h in rows:
            key = f"{h.get('id')}:{h.get('restored')}" if isinstance(h, dict) else ""
            if not key or key in done or not h.get("ended"):
                continue
            new.append(key)
            by = self._reception_who(str(h.get("id") or ""))
            if by:
                self.cad.activity.record(by, "action", "reception test", reception.ended_text(h), "",
                                         reception.restored_ok(h))
        if new:
            keep = [f"{h.get('id')}:{h.get('restored')}" for h in rows if isinstance(h, dict)]
            self.cad.store.meta_put("reception_audited", json.dumps(keep[:80]))

    def reception_sample(self, sid: str) -> bytes | None:
        """A call the station's reception test captured, fetched from it as it is asked for: never stored here."""
        try:
            wav = base64.b64decode(self.hub.remote("reception_sample_b64", str(sid)) or "", validate=True)
        except (StationOffline, RuntimeError, binascii.Error, ValueError, TypeError):
            return None
        return wav if wav[:4] == b"RIFF" else None

    def set_adsb_receiver(self, serial: str | None) -> str | None:
        """Settings > Radios here: the station's ADS-B dongle is named (or unnamed) on the station (#235)."""
        return self._switch("set_adsb_receiver", serial or "")

    def set_antenna_groups(self, groups, confirm: str = "") -> str | None:
        """Settings > Radios > Receivers here: the station's antenna groups, kept and applied on the station like a
        dongle's job (sizeup/radio/groups.py); what it runs comes back in its sdr state. confirm: the station's ask,
        word for word, as the person confirmed it (a save that stops a running fire job, or ranks fire jobs against
        the pinned order); the station refuses it if its ask has changed since."""
        return self._switch("set_antenna_groups", groups, str(confirm)) if confirm else \
            self._switch("set_antenna_groups", groups)

    def set_talkgroup(self, tg: int, record: bool | None = None, main: bool | None = None,
                      tier: str | None = None, system: str = "") -> str | None:
        if system == sourcekeys.COUNTY and tier is None and "p25_systems" not in (self.hub.status or {}):
            system = ""                                # the county's, to a station from before #409: as it always went
        if tier is not None or system:                 # (#409: a station from before this refuses the extra arguments)
            return self._switch("set_talkgroup", int(tg), record, main, tier, str(system or ""))
        return self._switch("set_talkgroup", int(tg), record, main)

    def _all_clear_sent(self, a: dict) -> None:
        """An alarm's All clear here: the station decides what is kept (App._call), so its emergency window closes
        too (in the background: the person isn't kept waiting on the station). Offline or an older station: its
        window ends on its own ([p25] emergency_window_min)."""
        if a.get("talkgroup") is None or not a.get("emergency"):
            return
        ids = [x.strip() for x in str(a.get("radio_id") or "").split(",") if x.strip()]

        def send() -> None:
            err = self._switch("emergency_all_clear", int(a["talkgroup"]), ids)
            if isinstance(err, str):
                self.hub.log(f"hub: all clear {a.get('id')}: not sent to the station ({err}); its window ends on its own")
        threading.Thread(target=send, name="all-clear", daemon=True).start()

    def set_talkgroup_group(self, group: str, record: bool | None = None, main: bool | None = None) -> str | None:
        return self._switch("set_talkgroup_group", str(group), record, main)

    def retranscribe(self, ids: list[str] | None = None, as_alt: bool = False) -> int:
        """Each one the way its channel says now: "aws" with Amazon Transcribe here (on the clip in
        audio.sqlite), "both" AWS here and the station's whisper for the alternative, "local" (and every
        recording: ids None) the station's whisper. AWS unavailable (not set up, the monthly limit, no
        clip): the station's whisper, said in the result."""
        if ids is None:
            return self._station_rerun(None)
        n, local, alt = self._rerun_split(ids) if self.aws is not None else (0, list(ids), [])
        if self.aws is None:
            for cid in local:
                item, key = self._find(cid)
                if item is not None and self.channel_pref(key)["engine"] in ("aws", "both"):
                    self._rerun_note[cid] = "AWS unavailable (not set up on the hub)"
        if alt:
            threading.Thread(target=self._rerun_station, args=(alt, True), name="hub-rerun-alt", daemon=True).start()
        if not local:
            return n
        if n:                    # AWS took some: the rest go to the station without holding up the answer
            threading.Thread(target=self._rerun_station, args=(local,), name="hub-rerun", daemon=True).start()
            return n + len(local)
        err: ValueError | None = None
        try:
            got = self._station_rerun(local)
        except ValueError as e:
            got, err = 0, e
        if not got:
            for cid in local:
                self._rerun_note.pop(cid, None)
        if err is not None:
            raise err
        return got

    def _station_rerun(self, ids: list[str] | None, as_alt: bool = False) -> int:
        try:
            return int((self.hub.remote("retranscribe", ids, True) if as_alt else self.hub.remote("retranscribe", ids)) or 0)
        except (StationOffline, RuntimeError, TypeError, ValueError) as e:
            raise ValueError(f"speech-to-text runs on the station: {e}") from None

    def _rerun_station(self, ids: list[str], as_alt: bool = False) -> None:
        """In the background: the station's whisper for these; any it can't take finish failed (not for
        the alternative of a "both": AWS's result is the one said)."""
        try:
            n, err = self._station_rerun(ids, as_alt), ""
        except ValueError as e:
            n, err = 0, str(e)
        if n or as_alt:
            return
        for cid in ids:
            self._rerun_finish(cid, f"failed: {err or 'the station has no recording to re-run'}")
        self.sync()

    # -- shed clips caught up (sizeup/sttcatchup.py): driven here, the station's whisper runs them (catch_up) ------

    def _catchup_may_run(self) -> bool:
        """The leader with a fresh lease only (sizeup/fence.py): a standby changes no rows."""
        return self._may_write_audio() and not self._stopping.is_set()

    def _catchup_whisper_ready(self, after: float) -> tuple[bool, str]:
        """The station's whisper idle, by its last report, and that report sent after the last batch was handed over
        (so a batch still waiting there is seen before another goes)."""
        from sizeup.sttcatchup import whisper_idle
        if not self.hub.connected():
            return False, "station offline"
        last = getattr(self.hub, "status_at", None) or 0.0
        if last <= after or time.time() - last > CATCHUP_STATUS_S:
            return False, "waiting for the station's report"
        hl = _part(self.hub.status, "health") or {}
        wh = hl.get("whisper") if isinstance(hl.get("whisper"), dict) else {}
        if wh.get("state") not in (None, "ok"):
            return False, "whisper not ready"
        return whisper_idle(hl.get("stt_queue"))

    def _catchup_whisper(self, specs: list[dict]) -> dict | None:
        """The station's whisper (its catch_up call): {queued, refused, hold}. None: the call itself failed (offline,
        a network error, an older station that refuses it): no clip was tried, so none is counted as tried. A station
        that doesn't know the call isn't asked again for CATCHUP_REFUSED_S."""
        now = time.time()
        if now < self._catchup_refused_until:
            return None
        try:
            got = self.hub.remote("catch_up", specs, timeout=CALL_WAIT_S)
        except (StationOffline, RuntimeError, TypeError, ValueError) as e:
            if "not allowed" in str(e):
                self._catchup_refused_until = now + CATCHUP_REFUSED_S
            self.hub.log(f"speech-to-text catch-up: not sent to the station: {str(e)[:160]}"
                         + (f" (asked again in {CATCHUP_REFUSED_S / 60:.0f} min)" if "not allowed" in str(e) else ""))
            return None
        if not isinstance(got, dict):
            return None
        if not isinstance(got.get("queued"), (list, type(None))):
            self.hub._wrong_result("catch_up", "queued")
            return None                     # (as a call that failed: nothing counted as tried)
        refused = got.get("refused") if isinstance(got.get("refused"), dict) else {}
        return {"queued": [str(x) for x in got.get("queued") or [] if isinstance(x, str)],
                "refused": {str(k): str(v)[:160] for k, v in refused.items()}, "hold": str(got.get("hold") or "")[:160]}

    def _catchup_pcm(self, cid: str, key: str, source: str):
        from sizeup.transcribe import wav_pcm
        wav = self.audio.get(cid)
        return wav_pcm(wav, 0.0) if wav else None

    def _rerun_pcm(self, item):
        from sizeup.transcribe import wav_pcm
        wav = self.audio.get(item.id)
        return wav_pcm(wav, 0.0 if isinstance(item, Traffic) else getattr(item, "voice_offset_s", 0.0)) if wav else None

    def _clean_hub(self, background: bool = False, now: float | None = None) -> None:
        now = time.time() if now is None else now
        self.store.prune(now - self.keep_s)
        with self._lock:
            while self.pages and self.pages[-1].end < now - self.keep_s:
                self.pages.pop()
        self._audio_retention(now, background)
        self.sync()

    def offpeak(self, now: float) -> bool:
        """[storage] audio_offpeak: [start, end) in the console's time zone (2-5 am by default)."""
        from datetime import datetime
        a, b = self.cfg.audio_offpeak
        h = datetime.fromtimestamp(now, tz.TZ).hour
        return a <= h < b if a <= b else (h >= a or h < b)

    def _may_write_audio(self) -> bool:
        fence = getattr(self, "fence", None)
        return fence is None or fence.ok()

    def _may_delete_audio(self) -> bool:
        """Deletes from the audio store: only with a real lease, ours and fresh (sizeup/fence.py). No fence (a local
        hub, compose, a hub without its supervisor): never, whatever audio.sqlite says (#414)."""
        fence = getattr(self, "fence", None)
        ok = fence is not None and fence.ok()
        if ok and getattr(self, "_lead_since", None) is None:
            self._lead_since = time.monotonic()
        elif not ok:
            self._lead_since = None
        return ok

    def audio_keep(self):
        """The retention per channel / talkgroup (sizeup/audiostore.py Retention): Settings > System > Audio retention
        (#414), read now, over [storage] audio_keep_hours."""
        from sizeup import audiostore
        return audiostore.Retention(self.cad.settings.section(audiostore.RETENTION_SECTION), self.cfg.audio_keep_hours / 24)

    def _audio_retention(self, now: float, background: bool = False) -> int:
        """The audio retention (docs/cloud.md "Audio retention", #251, #46, #414): a clip goes once older than its
        channel's or talkgroup's retention (Settings > System > Audio retention, else [storage] audio_keep_hours; read at
        each pass, so a change applies to the clips already kept), but the ones on a CAD event, with an alarm or an
        emergency (Comms.kept_for_events), not before audio_keep_event_days either (0 = forever; their copy in the
        incident attachments is kept a year whatever this says); the size cap drops only clips that aren't a record.
        In small transactions; outside the off-peak hours at most 10 of them a pass (the backlog waits for the night),
        and the free pages are handed back (incremental vacuum) only off-peak. Nothing while the lease isn't ours and
        fresh."""
        if not self._may_write_audio():
            return 0                              # not the hub's leader with a fresh lease: no writes (sizeup/fence.py)
        cfg = self.cfg
        quiet = self.offpeak(now)
        days = cfg.audio_keep_event_days
        policy = self.audio_keep()
        comms = self.cad.comms            # a record, or still inside its tier's own retention (#409, sizeup/tiers.py)
        # Another P25 system's clips (#409) are context: a tier only keeps them past their usual retention, inside their
        # own budget ([storage] context_audio_max_mb); over the cap they go before any clip of ours (AudioDb.prune_clips)
        n = self.audio.prune_clips(now - policy.shortest_days() * 86400, kept=comms.kept_for_events,
                                   context=lambda ids: comms.context_clips(ids, cfg.tier_keep_days),
                                   context_max_bytes=int(cfg.context_audio_max_mb * 1024 * 1024),
                                   kept_older_than=(now - days * 86400) if days else None, batch=cfg.audio_prune_batch,
                                   pause_s=0.05 if background else 0.0, max_rows=None if quiet else 10 * cfg.audio_prune_batch,
                                   policy=policy, now=now)
        try:                                      # the transcripts and metadata: their own retention, never before the audio
            self.cad.comms.prune(pause_s=0.02 if background else 0.0)
        except Exception as e:  # noqa: BLE001 (the radio log's prune never stops the audio's)
            self.hub.log(f"hub: radio log prune: {type(e).__name__}: {e}")
        over = getattr(self.audio, "over_cap", 0)
        if over and over != getattr(self, "_over_said", 0):
            self._over_said = over
            self.hub.log(f"audio: the clips kept as records alone are {over / 1e6:.0f} MB, over [cloud] audio_max_mb: "
                         "kept (lower [storage] audio_keep_event_days to keep less)")
        if quiet and background:
            freed = self.audio.incremental_vacuum(max_pages=8192, step=128, pause_s=0.05)
            if freed:
                self.hub.log(f"audio: {freed} free page(s) of audio.sqlite handed back (incremental vacuum)")
            elif self.audio.compact():
                self.hub.log("audio: audio.sqlite rewritten small once its audio was in the store (VACUUM, #414)")
            if self._sweep_due(now):
                self._swept_at = now                  # once a night: objects no row names, a day old (#414)
                try:
                    gone = self.audio.sweep_orphans(pause_s=0.2, now=now, known=self._clip_known)
                except Exception as e:  # noqa: BLE001 (S3 down: the next night)
                    gone = 0
                    self.hub.log(f"audio: orphan sweep: {type(e).__name__}: {str(e)[:120]}")
                if gone:
                    self.hub.log(f"audio: {gone} object(s) in the store that no clip named, deleted")
                if self.audio.sweep_restored:
                    self.hub.log(f"audio: {self.audio.sweep_restored} clip row(s) audio.sqlite had lost made again from "
                                 "their objects (the radio log still knew them)")
                if self.audio.sweep_status:
                    self.hub.log(f"audio: orphan sweep stopped: {self.audio.sweep_status}")
                    try:
                        from sizeup.notify import STATUS
                        self.pushes.notifier(STATUS).send("sizeup: audio orphan sweep stopped", self.audio.sweep_status)
                    except Exception as e:  # noqa: BLE001 (the push is a courtesy; the log has it)
                        self.hub.log(f"audio: could not push the sweep's notice: {type(e).__name__}")
        return n

    def _sweep_due(self, now: float) -> bool:
        """The orphan sweep: once a night, only with a store, a real lease held for SWEEP_LEADER_S already (a takeover
        that restored an old audio.sqlite has a day to be noticed first) and the migration done."""
        if self.audio.store is None or now - getattr(self, "_swept_at", 0.0) < 20 * 3600 or not self._may_delete_audio():
            return False
        since = getattr(self, "_lead_since", None)
        return since is not None and time.monotonic() - since >= SWEEP_LEADER_S and not self.audio.pending()[0]

    def _clip_known(self, ids: list[str]) -> dict[str, tuple[str, int | None, float]]:
        """The clip ids the rest of the hub knows (the monitor's transmissions and pages, the CAD's radio log): {id:
        (channel, talkgroup, when it ended)}. The orphan sweep never deletes these (#414)."""
        want, out = set(ids), {}
        with self.store._lock:
            for t in list(self.store.loose) + [t for i in self.store.incidents for t in i.traffic]:
                if t.id in want:
                    out[t.id] = (t.source, _our_tg(t), t.end)
        with self._lock:
            for p in self.pages:
                if p.id in want:
                    out[p.id] = (p.label, None, p.end)
        rest = [i for i in ids if i not in out]
        for k in range(0, len(rest), 500):
            chunk = rest[k:k + 500]
            rows = self.cad.store.q(f"SELECT id, channel, CASE WHEN system IN {sourcekeys.OURS_SQL} THEN talkgroup END "
                                    f"AS talkgroup, end FROM radio_traffic WHERE id IN ({','.join('?' * len(chunk))})",
                                    chunk)
            out.update((r["id"], (r["channel"], r["talkgroup"], r["end"])) for r in rows)
        return out

    def _clip_channels(self, ids: list[str]) -> dict[str, tuple[str, int | None]]:
        """The channel and talkgroup of these clips (the migration's backfill, #414): the hub's transmissions and
        pages, then the CAD's radio log."""
        want, out = set(ids), {}
        with self.store._lock:
            for t in list(self.store.loose) + [t for i in self.store.incidents for t in i.traffic]:
                if t.id in want:
                    out[t.id] = (t.source, _our_tg(t))
        with self._lock:
            for p in self.pages:
                if p.id in want:
                    out[p.id] = (p.label, None)
        rest = [i for i in ids if i not in out]
        for k in range(0, len(rest), 500):
            chunk = rest[k:k + 500]
            try:
                rows = self.cad.store.q(f"SELECT id, channel, CASE WHEN system IN {sourcekeys.OURS_SQL} THEN talkgroup "
                                        f"END AS talkgroup FROM radio_traffic WHERE id IN ({','.join('?' * len(chunk))})",
                                        chunk)
            except Exception:  # noqa: BLE001 (a CAD without the radio log)
                break
            out.update((r["id"], (r["channel"], r["talkgroup"])) for r in rows if r["channel"])
        return out

    def _audio_uploader(self) -> None:
        """#414: woken by every new clip (AudioDb.wake), else every AUDIO_UPLOAD_S: the rows still holding their bytes
        into the store, newest first (the clips just in, then the rows from before: the migration), a few a round in
        the day, more off-peak; once, the old clips' channels. Leader only (AudioDb.may_write); nothing without a
        store. The ingest never waits for this: a page or a transmission is in audio.sqlite before any S3 call."""
        if self.audio.store is None:
            return
        named = False
        said = 0
        while not self._stopping.is_set():
            self.audio.wake.wait(AUDIO_UPLOAD_S)
            self.audio.wake.clear()
            if self._stopping.is_set():
                break
            try:
                if not self._may_write_audio():
                    continue
                if not named:
                    n = self.audio.backfill_channels(self._clip_channels)
                    named = True
                    if n:
                        self.hub.log(f"audio: {n} older clip(s) given their channel (the retention per channel)")
                rows = AUDIO_UPLOAD_ROWS[1] if self.offpeak(time.time()) else AUDIO_UPLOAD_ROWS[0]
                moved = self.audio.upload_pending(limit=rows, pause_s=0.02)
                if moved:
                    left = self.audio.pending()[0]
                    if not left or said == 0 or time.monotonic() - said > 600:
                        said = time.monotonic()
                        self.hub.log(f"audio: {moved} clip(s) moved into the store, {left} still in audio.sqlite")
            except Exception as e:  # never takes the hub down
                self.hub.log(f"audio: uploader error: {type(e).__name__}: {e}")

    def _metrics_audit(self, subject: str, t0: float, t1: float) -> list[dict]:
        """The Settings audit's changes to a channel (its squelch, its record / transcribe choices) in [t0, t1): the
        graphs' annotations beside the station's own events (sizeup/cloud/metricstore.py)."""
        return self.cad.store.q("SELECT at, operator, key, old, new FROM settings_audit WHERE section = 'radios' AND "
                                "key IN (?, ?) AND at >= ? AND at < ? ORDER BY at LIMIT 500",
                                (f"squelch/{subject}", f"channel/{subject}", t0, t1))

    def apply_station_renames(self) -> int:
        """The shared token's renames (hub.sqlite meta "station_renames", StationRegistry.moves) applied to the station's
        signal metrics: each former id's rows from before its rename moved to the id it is now (a later row under it is
        another station's), in chunks, never on an admin's request. Run after a rename, at the janitor's turn, and once
        the store settles (a takeover restores a copy that may be from before a rename: metrics.sqlite isn't
        replicated). The janitor's rollups wait while it runs (_renaming). Moved anything: a copy goes up soon. The
        rows moved."""
        store = getattr(self, "metric_store", None)
        if store is None or not store.settled or not self._renaming.acquire(blocking=False):
            return 0
        try:
            n = 0
            for old, (new, at) in self.hub.stations.moves().items():
                m = store.rename_station(old, new, before=at)     # (only its rows from before the rename)
                if m:
                    self.hub.log(f"stations: station metrics: {m} row(s) moved from {old} to {new}")
                    ins = getattr(self, "metric_insight", None)     # #606: its suggestions / sparks / shifts follow
                    if ins is not None:
                        ins.renamed(old, new)
                n += m
            if n:
                store.copy_soon()
            return n
        except fence_mod.FencedWrite:
            return 0                               # not the leader now: the next one does it
        finally:
            self._renaming.release()

    def _metrics_backup(self) -> None:
        """The station metrics' copies (sizeup/cloud/metricstore.py backup_turn): settle from the bucket, then a copy
        every 3 h. The leader's only (the store checks); never takes anything down."""
        from sizeup.cloud.metricstore import BACKUP_TURN_S
        while not self._stopping.wait(BACKUP_TURN_S):
            try:                                   # (#677) the STT input arrival delays measured since: into the store
                self.stt_arrival_flush()
            except Exception as e:  # noqa: BLE001
                self.hub.log(f"metrics: STT input arrival: {type(e).__name__}: {e}")
            try:
                if self.metric_store.backup_turn() in ("restored", "none"):
                    self.apply_station_renames()   # settled from a copy: a rename since it was made, again
            except Exception as e:  # noqa: BLE001
                self.hub.log(f"metrics: backup error: {type(e).__name__}: {e}")
            try:                                  # every transcript as written (#639): stt.sqlite's copies, the same way
                self.stt_runs.backup_turn()
            except Exception as e:  # noqa: BLE001
                self.hub.log(f"stt runs: backup error: {type(e).__name__}: {e}")

    def _hub_janitor(self) -> None:
        while not self._stopping.wait(600):
            try:
                self._clean_hub(background=True)
            except Exception as e:  # cleanup must never take the display down
                self.hub.log(f"hub: cleanup error: {type(e).__name__}: {e}")
            try:                                  # every transcript as written (#639): KEEP_DAYS, a batch a pass
                self.stt_runs.prune()             # (the leader's only: Runs checks)
            except Exception as e:  # noqa: BLE001 - never takes anything down
                self.hub.log(f"stt runs: prune error: {type(e).__name__}: {e}")
            try:                                  # the station metrics: renames, rollups, retention
                self.metrics_turn()
            except Exception as e:  # noqa: BLE001 - never takes anything down
                self.hub.log(f"metrics: maintenance error: {type(e).__name__}: {e}")
            try:                                  # ... then the shift checks (#606): Health's warnings, never a push
                self.metric_insight.turn()
            except Exception as e:  # noqa: BLE001 - never takes anything down
                self.hub.log(f"metrics: shift check error: {type(e).__name__}: {e}")

    def metrics_turn(self) -> dict | None:
        """The janitor's station-metrics turn: the renames, then the rollups and retention (maintain), which wait for
        the next turn while a move runs (a rollup of a move half done would be wrong). None: skipped for a move."""
        self.apply_station_renames()
        if not self._renaming.acquire(blocking=False):
            return None
        try:
            return self.metric_store.maintain()
        finally:
            self._renaming.release()

    def start(self) -> HubApp:
        threading.Thread(target=self._hub_janitor, name="hub-janitor", daemon=True).start()
        threading.Thread(target=self._audio_uploader, name="audio-uploader", daemon=True).start()
        threading.Thread(target=self._metrics_backup, name="metrics-backup", daemon=True).start()
        threading.Thread(target=self.health.run, args=(self._stopping,), name="station-health", daemon=True).start()
        threading.Thread(target=self.error_watch.run, args=(self._stopping,), name="hub-errors", daemon=True).start()
        threading.Thread(target=self._aws_watch, name="aws-sweep", daemon=True).start()
        self.sttpool.start()                      # the GPU workers' queue (#639): empty, nothing written
        infer_mod.install(self.infer_lane.start())  # the LLM lane: sizeup.cloud.infer.submit() asks it from now on
        self.catchup.start()                      # its first look minutes from now: no writes at start
        # radio pre-alerts (sizeup/intel): the "intel" thread, as App.start() starts it. Without it every transcript
        # handed to the engine waited in its queue unread: no rule was ever tried on the hub
        self.cad.intel.start()
        self.perf.start()
        self._started = True                      # the phone pre-alert: the hub's own subscription
        self.prealert_sync()
        self.start_sources()                      # held reports and promotion (#297); the adapters are the station's
        self.start_layers()                       # the maps' data, as App starts it (road projects too: #225)
        if isinstance(self.stt_terms, stt_terms.TermStore):
            self.stt_terms.start()                # the private term lists (sizeup/stt_terms.py): now, then every 5 min
        self._terms_cleanup()                     # ([stt] terms_aws off: the term vocabularies made before deleted)
        if self.vocab is not None and self.vocab.state()["state"] == "PENDING":
            self.vocab.poll()
        try:
            self._aws_recover()
        except Exception as e:  # never in the way of starting
            self.hub.log(f"hub: could not resume Amazon Transcribe work: {type(e).__name__}: {e}")
        try:
            self._pool_recover()
        except Exception as e:  # never in the way of starting
            self.hub.log(f"hub: could not resume the pool's clips: {type(e).__name__}: {e}")
        return self

    def stop(self) -> None:
        from sizeup.timing import Phases
        t = Phases("stop")                             # the drain's share of a deploy (#264): where it goes
        self._stopping.set()
        self.cad.intel.stop()                          # (a flag: the thread ends at its next wait)
        infer_mod.uninstall(self.infer_lane)
        self.infer_lane.stop()                         # (what it had: None at once; the intel engine goes on without)
        left = self.sttpool.stop(fall_back=False)   # (what a GPU worker had stays "pending": the next start routes it)
        if left:
            self.hub.log(f"stt pool: stopping with {left} clip(s) still out: the next start sends them the AWS way")
        steps = [("catch-up", self.catchup.stop), ("perf", self.perf.stop)]
        if self.prealert is not None:
            steps.append(("pre-alert", self.prealert.stop))
        steps += [("sources", self.cad.sources.stop), ("layers", self.stop_layers)]
        late = _bounded(steps, STOP_LAYERS_S, self.hub.log)
        t.mark("layers")
        if late:                                       # (#546: a slow one never keeps the final sync from happening)
            self.hub.log(f"hub: stop: {late} still stopping after {STOP_LAYERS_S:g} s: going on without it")
        if self.aws is not None:
            self.aws.stop()
            t.mark("speech-to-text")
        try:
            self.sync()
        except Exception:
            pass
        t.mark("sync")
        self.hub.log(t.line())
