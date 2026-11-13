"""[cloud] role = "hub": the web UI and the CAD in the cloud, fed by the station. No radios, no
trunk-recorder, no whisper, no voice server: nothing here opens an SDR or starts a program, so it
runs on a bare Linux box with Python + numpy.

    POST /api/station/events      {epoch, station, events: [{seq, at, type, data, audio?}]} -> {acked}
                                  applied in order through the same App code as on the station
                                  (_page_heard, _transcribed, _retext, the radio registry); each
                                  event once: the hub remembers the last seq per outbox epoch
                                  (hub.sqlite), and pages / transmissions are keyed by their ids
    POST /api/station/state       {at, talking?, paging?, status?}: who is talking now and a page going
                                  out now (latest wins; paging: sizeup/radio/livepage.py) and the
                                  station's radio / SDR / STT / voice rows, talkgroups, re-run
                                  progress, outbox size, its version -> {ok, build}: the hub's own
                                  build (SIZEUP_BUILD), which the station follows (autoupdate.py)
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
    GET  /api/station/hello      {ok}: the token works (the station's Test connection; not counted as "seen")
    GET  /api/station/status     read only, for the deploy check (tools/deploy_check.py): the hub's build, the
                                 station's version and last update, the health alerts (sizeup/cloud/health.py).
                                 The station token or the status token, which opens nothing else; not "seen"

Every request needs `Authorization: Bearer <SIZEUP_STATION_TOKEN>` (constant-time compare); with
no token configured the hub refuses them all. Jobs (the station only connects out):
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
import hmac
import json
import os
import re
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import replace
from pathlib import Path
from typing import Callable

from sizeup import channels, sourcekeys
from sizeup import tz
from sizeup import app as app_mod
from sizeup.app import (AIRBAND_FIRST, CLIP_ID, RENAMED, STT_FIRST, WHISPER_FALLBACK_STATION, App, AwsJob, channels_model,
                        traffic_key)
from sizeup.cad.dispatcher import AudioUnavailable
from sizeup.cad.radios import fm_system
from sizeup.cloud.autoupdate import describe
from sizeup.cloud.health import StationHealth, status_token
from sizeup.cloud.hubstore import AudioDb, MonitorDb
from sizeup.incidents import Traffic
from sizeup.radio.page import page_from_dict
from sizeup.sources import SourceError
from sizeup.transcribe import RESERVE_CHANNELS, deadline_for

JSON = "application/json"
MAX_EVENTS_BODY = 32 * 1024 * 1024
MAX_STATE_BODY = 4 * 1024 * 1024
MAX_RESULT_BODY = 16 * 1024 * 1024
MAX_AIRCRAFT_BODY = 512 * 1024        # 300 aircraft as JSON are ~30 KB; gzipped, a few
MAX_VESSELS_BODY = 512 * 1024         # 500 vessel records as JSON are ~150 KB; gzipped, far less
MAX_LOGS_BODY = 4 * 1024 * 1024       # a station log batch, gzipped (sizeup/cloud/logship.py: ~512 KB before gzip)
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
JOB_PATH = re.compile(r"/api/station/jobs/([0-9a-f]{16})")
EXTRA = re.compile(r"[a-z][a-z0-9_]{0,30}")      # a transmission's field names, as the station sends them


class StationOffline(RuntimeError):
    pass


def _json(code: int, obj) -> tuple[int, bytes, str]:
    return code, json.dumps(obj, separators=(",", ":")).encode(), JSON


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
            return [dict(p) for p in self._paging if not (p.get("ended") and (p.get("until") or 0) <= now)]

    def state(self) -> list[dict]:
        now = self.clock()
        with self._lock:
            if now - self._at > LIVE_STALE_S:
                return []
            entries = [dict(e) for e in self._entries]
        out = []
        for e in entries:
            if e.get("ended") and (e.get("until") or 0) <= now:
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
        self.status_token = status_token(self.token, getattr(cfg, "cloud_status_token", ""))
        self.health_: StationHealth | None = None  # station health and alerts (HubApp sets it)
        self.polling = 0
        self.status: dict | None = None          # the station's last report (state "status")
        self.talkgroups: list[dict] = []
        self.skew = 0.0                          # hub clock - station clock (s)
        self.errors, self.last_error = 0, ""
        self._lock = threading.Lock()
        self._ingest = threading.Lock()
        self.epoch = app.mdb.get("station_epoch")
        self.seq = int(app.mdb.get("station_seq", "0") or 0)
        self._failed: tuple[str, int, int] = ("", 0, 0)   # (epoch, seq, tries) of a source_event that failed to apply

    # -- health --------------------------------------------------------------------------------------

    def connected(self) -> bool:
        return self.polling > 0 or (self.last_seen is not None and time.time() - self.last_seen < self.offline_s)

    def _seen(self) -> None:
        self.last_seen = time.time()

    def health(self) -> dict:
        """{state, word, detail}: the "Station" row and the console's header pill."""
        now = time.time()
        name = (self.status or {}).get("name") or "the station"
        if not self.token:
            return {"state": "down", "word": "no token",
                    "detail": "no station token configured on the hub (SIZEUP_STATION_TOKEN): the station can't connect"}
        if self.connected():
            ago = 0 if self.polling else int(now - (self.last_seen or now))
            q = ((self.status or {}).get("outbox") or {}).get("pending") or 0
            # the seconds only once they matter (as the word does): a count ticking every second between two
            # polls would change the pushed state every second for nothing (#146)
            detail = (f"{name}: connected" + (f", last seen {ago} s ago" if ago >= 5 else "")
                      + (f" · {q} event(s) still on their way up" if q else "")
                      + (f" · clocks differ by {self.skew:+.0f} s (set both to network time)" if abs(self.skew) > 5 else "")
                      + (f" · {self.errors} event(s) failed to apply, last: {self.last_error}" if self.errors else ""))
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
        v = (self.status or {}).get("version")
        if not isinstance(v, dict):
            return None
        return describe(str(v.get("build") or ""), self.build, v.get("update") if isinstance(v.get("update"), dict) else {})

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
        for r in (self.status or {}).get("radios") or []:
            r = dict(r)
            if not on:
                r.update(state="unknown", open=False, quieting_db=None, word="",
                         detail=f"station offline since {_hhmm(self.last_seen)}; last report: "
                                f"{r.get('state', '?')}: {r.get('detail') or ''}".rstrip(": "))
            rows.append(r)
        return rows

    def sdr(self) -> dict | None:
        """The station's SDR switches, only while it is there to switch them."""
        return (self.status or {}).get("sdr") if self.connected() else None

    def rerun(self) -> dict:
        r = (self.status or {}).get("rerun") if self.connected() else None
        return dict(r) if isinstance(r, dict) else {"total": 0, "done": 0, "changed": 0, "failed": 0, "pending": 0}

    def cad_cloud(self) -> dict:
        """The CAD state's cloud (everyone's state push): the station's connection, for the header pill. No health
        alerts here (#203): those are on the admins' Settings > Health, from GET /api/cad/health."""
        h = self.health()
        return {"role": "hub", "state": h["state"], "word": h["word"], "detail": h["detail"],
                "health_url": "/cad#settings/station/health"}

    # -- jobs ------------------------------------------------------------------------------------------

    def call(self, kind: str, args: dict, timeout: float) -> dict:
        """Run a job on the station and wait for its answer. StationOffline when it isn't connected
        (at once: no waiting on a station that isn't there) or didn't answer in time."""
        if not self.token:
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
        """POST /api/sources/reset (an admin, Settings > Health): the station's incident source's reset() (#301), as a
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
        try:
            wav = base64.b64decode(res.get("audio") or "", validate=True)
        except (binascii.Error, ValueError):
            wav = b""
        if wav[:4] != b"RIFF":
            raise AudioUnavailable("Voice unavailable — the station sent no audio: text only")
        return wav, str(res.get("label") or "Voice: rendered by the station")

    def remote(self, fn: str, *args, timeout: float = CALL_WAIT_S):
        """One of the station's switches (StationOffline / RuntimeError with the reason)."""
        return self.call("call", {"fn": fn, "args": list(args)}, timeout).get("result")

    # -- HTTP --------------------------------------------------------------------------------------------

    def _auth(self, headers, read_only: bool = False) -> tuple[int, bytes, str] | None:
        """None when the bearer is the station token (or, read_only, the status token); else the refusal."""
        if not self.token:
            return _json(403, {"error": "station ingest is off: no station token configured on the hub "
                                        "(SIZEUP_STATION_TOKEN)"})
        h = (headers.get("Authorization") or "").strip()
        given = h[7:].strip() if h[:7].lower() == "bearer " else ""
        ok = bool(given) and hmac.compare_digest(given.encode(), self.token.encode())
        if read_only and given and self.status_token:
            ok = hmac.compare_digest(given.encode(), self.status_token.encode()) or ok
        if not ok:
            return _json(401, {"error": "bad or missing station token"})
        return None

    def handle(self, method: str, path: str, query: str, headers, body: Callable[[int], bytes | None] | None):
        if path == "/api/station/status":            # read only (the deploy check): not "seen", nothing changes
            refused = self._auth(headers, read_only=True)
            if refused:
                return refused
            if method != "GET":
                return _json(405, {"error": "GET only"})
            return _json(200, self.health_.status() if self.health_ is not None else
                         {"ok": True, "hub_build": self.build, "station": None, "alerts": []})
        refused = self._auth(headers)
        if refused:
            return refused
        if method == "GET" and path == "/api/station/hello":
            return _json(200, {"ok": True})    # the station's Test connection: the token works (not "seen")
        self._seen()
        try:
            if method == "GET" and path == "/api/station/jobs":
                return self._jobs(query)
            if method == "POST" and path == "/api/station/events":
                return self._events(body(MAX_EVENTS_BODY))
            if method == "POST" and path == "/api/station/state":
                return self._state(body(MAX_STATE_BODY))
            if method == "POST" and path == "/api/station/aircraft":
                return self._aircraft(body(MAX_AIRCRAFT_BODY), headers)
            if method == "POST" and path == "/api/station/vessels":
                return self._vessels(body(MAX_VESSELS_BODY), headers)
            if method == "POST" and path == "/api/station/logs":
                return self._logs(body(MAX_LOGS_BODY), headers)
            if method == "POST" and (m := JOB_PATH.fullmatch(path)):
                raw = body(MAX_RESULT_BODY)
                if raw is None:
                    return _json(413, {"error": "too large"})
                res = json.loads(raw or b"{}")
                if not isinstance(res, dict):
                    return _json(400, {"error": "a JSON object"})
                return _json(200, {"ok": True}) if self.jobs.finish(m.group(1), res) else \
                    _json(404, {"error": "no such job (it timed out)"})
        except ValueError as e:
            return _json(400, {"error": f"bad request: {e}"})
        return _json(404, {"error": "not found"})

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

    def _state(self, raw: bytes | None) -> tuple[int, bytes, str]:
        if raw is None:
            return _json(413, {"error": "too large"})
        d = json.loads(raw or b"{}")
        if not isinstance(d, dict):
            raise ValueError("a JSON object")
        now = time.time()
        self.last_state = now
        if isinstance(d.get("at"), (int, float)):
            self.skew = now - float(d["at"])
        if isinstance(d.get("status"), dict):
            st = dict(d["status"])
            tgs = st.pop("talkgroups", None)
            if isinstance(tgs, list):
                self.talkgroups = tgs
            self.status = st
            if self.health_ is not None:
                self.health_.observe(st)           # each channel's gaps, for the silence hints
            self.app.channels_check()          # the station runs other channel choices than these: send them
            try:
                self.app.reception_seen(st.get("reception"))     # (#410) a reception test that ended: logged, once
            except Exception as e:  # noqa: BLE001 - a log row, never in the way of the station's report
                self.log(f"hub: reception test not logged: {type(e).__name__}: {e}")
        if isinstance(d.get("talking"), list):
            self.live.update(d["talking"], now, d.get("paging") if isinstance(d.get("paging"), list) else None)
        return _json(200, {"ok": True, "build": self.build, "caps": ["p25_systems"]})   # (#409: what this hub knows)

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
        return _json(200, layer.station_ingest(json.loads(raw or b"{}"), size))

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
        return _json(200, layer.station_ingest(json.loads(raw or b"{}")))

    def _logs(self, raw: bytes | None, headers) -> tuple[int, bytes, str]:
        """The station's journal, scrubbed (sizeup/cloud/logship.py): into CloudWatch (sizeup/cloud/cwlogs.py)."""
        sink = getattr(self.app, "station_logs", None)
        if sink is None:
            return _json(404, {"error": "no station logs here"})
        try:
            code, out = sink.ingest(raw, headers)
        except Exception as e:  # noqa: BLE001 - never in the way of the station's link
            self.log(f"station logs: {type(e).__name__}")
            return _json(503, {"error": f"station logs: {type(e).__name__}"})
        return _json(code, out)

    def _retry(self, seq: int) -> bool:
        """A source_event failed to apply: True while it still has tries left (left unacked, the station sends it
        again), False once it has had SOURCE_RETRIES (then skipped, like any other bad event)."""
        ep, s, n = self._failed
        n = n + 1 if (ep, s) == (self.epoch, seq) else 1
        self._failed = (self.epoch, seq, n)
        return n < SOURCE_RETRIES

    def _events(self, raw: bytes | None) -> tuple[int, bytes, str]:
        if raw is None:
            return _json(413, {"error": f"too large ({MAX_EVENTS_BODY // 1024 // 1024} MB max): send smaller batches"})
        d = json.loads(raw or b"{}")
        if not isinstance(d, dict) or not isinstance(d.get("events"), list):
            raise ValueError("{epoch, events: [...]}")
        epoch = str(d.get("epoch") or "")[:40]
        with self._ingest:
            if epoch != self.epoch:
                if self.epoch:
                    self.log(f"hub: the station's outbox is new (epoch {epoch}): counting its events from the start")
                self.epoch, self.seq = epoch, 0
            n = 0
            for ev in sorted((e for e in d["events"] if isinstance(e, dict)), key=lambda e: int(e.get("seq") or 0)):
                seq = int(ev.get("seq") or 0)
                if seq <= self.seq:
                    continue                   # delivered before (the station retried): already applied
                kind = str(ev.get("type"))
                try:
                    audio = base64.b64decode(ev["audio"]) if ev.get("audio") else None
                    self.app.apply(kind, ev.get("data") or {}, audio)
                    n += 1
                except Exception as e:  # one bad event must not block the ones behind it
                    self.errors += 1
                    # an incident source's event: its text may quote the county's data, so the type only (#297)
                    said = "" if kind in SOURCE_KINDS else f": {e}"
                    self.last_error = f"{kind} #{seq}: {type(e).__name__}{said}"[:200]
                    self.log(f"hub: could not apply {kind} #{seq}: {type(e).__name__}")
                    if kind == "source_event" and not isinstance(e, SourceError) and self._retry(seq):
                        break                  # not acked: the station sends it again (a malformed one is skipped)
                self.seq = seq
            self.app.mdb.put("station_epoch", self.epoch)
            self.app.mdb.put("station_seq", str(self.seq))
            if n:
                self.app.sync()
                self.live.on_change()       # push the display now, not on its next second
            return _json(200, {"ok": True, "acked": self.seq, "applied": n})


SOURCE_KINDS = ("source_event", "source_health", "source_seen")   # an external incident source's (#297)
SOURCE_RETRIES = 3          # a source_event that fails to apply (not a malformed one) is retried this often, then skipped
AUDIO_UPLOAD_S = 5.0        # the audio uploader's round (#414): the clips the ingest couldn't store, then the migration
AUDIO_UPLOAD_ROWS = (20, 200)   # rows a round: in the day, off-peak
SWEEP_LEADER_S = 24 * 3600   # the audio orphan sweep: only once this task has held the lease this long (#414)


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

    def __init__(self, cfg, log: Callable[[str], None] = print):
        self._aws_since: dict[str, float] = {}         # id -> when Amazon Transcribe got it (the sweep's clock)
        self._aws_asked: dict[str, float] = {}         # id -> when the station's whisper was asked instead
        self._aws_late_wait: set[str] = set()          # timed out, not a priority channel: AWS's late answer is the text
        self._pending_seen: dict[str, float] = {}      # page id -> when the sweep first saw it waiting for whisper
        self._aws_was_ok: bool | None = None           # AWS could take audio at the last sweep (_aws_resend)
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
                                 shared_device="", dispatch_tts_python=""))
        self.receivers, self.modes = [], []
        self.audio.may_write = self._may_write_audio
        self.audio.may_delete = self._may_delete_audio
        self.hub = Hub(self, cfg, log)
        # station health (GitHub #79): the alerts, the admins' Settings > Health (#203), the push to the status topic in effect (Settings, else
        # [alerts] status_topic, else ntfy_topic: notify.Pushes; never Tapout's): App.pushes' STATUS kind (the same
        # Notifier as the emergency alarms when both kinds share a topic: one queue in order)
        from sizeup.notify import STATUS
        notifier = self.pushes.notifier(STATUS)
        self.health = self.hub.health_ = StationHealth(self.hub, cfg, self.mdb, notifier, log)
        # The incident sources (#297): paging is a tier only while the station is connected; Settings > Health shows them.
        self.cad.sources.paging_ok = self.hub.connected
        self.cad.sources.trigger = self._sources_poll_now      # a pre-alert here: the station's adapters poll now
        self.health.sources = self.cad.sources.panel
        self.health.feeds = lambda: [self.trains.feed_health()]      # MTA TrainTime or its fallback (#362)
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
        if not notifier.enabled:
            log("health: no push for station alerts (no status or alerts topic in effect): Settings > Health and log only")
        self.live = self.hub.live                      # main() sets on_change to push the display
        self.cad.live = self.live.state
        # the Radio page's source tree (#415): the station's talkgroups and systems, as its state says them
        self.cad.source_rows = lambda: self.hub.talkgroups or []
        self.cad.source_systems = lambda: {self.tg.county: "County", **{
            str(x.get("name")): str(x.get("label") or x.get("name")) for x in ((self.hub.status or {}).get("p25_systems") or [])
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
        self.station_logs = StationLogs(settings=lambda: self.cad.settings.section(LOGS_SECTION),
                                        station_status=lambda: self.hub.status, log=log)
        self.cad.station_logs = self.station_logs
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
        self.perf.gauge("dvr_segments", lambda: sum(len(c.get("segs") or ()) for c in list(getattr(dvr, "_cams", {}).values()))
                        if dvr is not None else None)

    # -- the station's events ----------------------------------------------------------------------

    def apply(self, kind: str, d: dict, audio: bytes | None) -> None:
        if kind in ("traffic", "alarm") and d.get("source") in RENAMED and d["source"] not in self._kind \
                and RENAMED[d["source"]] in self._kind:
            # #275: a station whose config isn't renamed yet (its update is deferred once) sends the old name: its
            # transmissions are the renamed channel's here (airband, the pre-alerts, AWS); the id stays the station's
            d = {**d, "source": RENAMED[d["source"]]}
        if kind == "page":
            self._apply_page(d, audio)
        elif kind == "page_text":
            self._apply_page_text(d)
        elif kind == "traffic":
            self._apply_traffic(d, audio)
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
                self._transcribed(page)
                return
            alt = d.get("alt") if isinstance(d.get("alt"), dict) else None
            self._retext(pid, str(d.get("text") or ""), str(d.get("status") or "done"),
                         d.get("confidence"), str(d.get("model") or ""), alt=alt)
        elif kind == "rerun":                         # a re-run on the station is over (changed, same or failed)
            self._rerun_finish(self._id(d), str(d.get("result") or "")[:200], str(d.get("model") or "")[:80])
        elif kind == "alt_text":                      # "both" re-run: the station's whisper text, to compare
            self._set_alt(self._id(d), str(d.get("text") or ""), str(d.get("model") or "")[:80])
        elif kind == "alarm":                         # the P25 emergency button, ahead of its transcript (#128)
            self._apply_alarm(d, audio)
        elif kind in SOURCE_KINDS:                     # an external incident source on the station (#297)
            self.apply_source(kind, d)
        elif kind == "heard":
            if d.get("system") in ("mdc", "p25", "fleetsync"):
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
            raise ValueError(f"bad id {cid[:40]!r}")
        return cid

    def _apply_page(self, d: dict, audio: bytes | None) -> None:
        pid = self._id(d)
        with self._lock:
            if any(p.id == pid for p in self.pages):
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
        elif (page.status != "pending" and not getattr(page, "flagged", False)) or getattr(page, "stt", "") == "aws":
            return                                    # applied before (or Amazon Transcribe reads it here)
        page.flagged = False  # type: ignore[attr-defined]   (flagged by the sweep: its text came after all)
        page.raw, page.confidence = str(d.get("raw") or ""), d.get("confidence")
        page.status, page.model = str(d.get("status") or "done"), str(d.get("model") or "")  # type: ignore[attr-defined]
        self._transcribed(page)
        if str(d.get("stt") or "") == "both" and page.status in ("done", "no_speech"):
            self._aws_from_clip(page, self.audio.get(pid), page.label, both=True)

    def _traffic_of(self, d: dict) -> Traffic:
        """The station's transmission as sent, its rig from the hub's radio registry (edited here)."""
        t = Traffic.from_dict({k: v for k, v in d.items() if EXTRA.fullmatch(str(k))})
        ids = [x.strip() for x in (t.radio_id or "").split(",") if x.strip()]
        pairs = d.get("pairs")
        if getattr(t, "system", ""):                   # another P25 system's radio IDs (#409): never our registry's
            t.rig = ""
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
        self._transcribed(t)
        if d.get("stt") == "both" and t.status in ("done", "no_speech"):
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
        self._aws_since[item.id] = time.time()
        self._aws_asked.pop(item.id, None)
        super()._aws_start(item, pcm, key)

    def _aws_done(self, job, text: str, conf) -> None:
        super()._aws_done(job, text, conf)
        if job.alt is None and not job.rerun:
            self._aws_since.pop(job.id, None)
            self._aws_asked.pop(job.id, None)
        self.sync()

    def _aws_late(self, job, text: str, conf) -> None:
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
    def _whisper_fallback(job) -> bool:
        """Does AWS timing out on it send it to the station's whisper? Pages, Fire/EMS 15, the firegrounds and the
        airband (the ATC pre-alerts, #233; whisper sheds an airband clip that waits 5 min) only (#300): anything else
        waits for AWS's late answer (usually seconds after the deadline), so a slow AWS never floods the station's
        whisper with clips nobody is waiting on."""
        return (job.kind == "page" or job.channel in STT_FIRST or job.channel in RESERVE_CHANNELS
                or job.channel in AIRBAND_FIRST)

    def _aws_failed(self, job, why: str) -> None:
        """Amazon Transcribe couldn't: the station's whisper does it (retranscribe, the text comes back as
        a retext event); a station that can't: "transcription failed", said on the transmission. Timed out on a
        channel that isn't a priority one: AWS's late answer is awaited instead (_whisper_fallback)."""
        late = (job.alt is None and not job.rerun and not job.recovered and why.startswith("AWS timed out")
                and not self._whisper_fallback(job))
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
        ok, why = self.aws.available() if self.aws is not None else (False, "Amazon Transcribe isn't set up on the hub")
        again = whisper = 0
        for item, key, kind in self._aws_pending():
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
            waiting = [p for p in self.pages if p.status == "pending" and getattr(p, "stt", "") != "aws"]
        ids = {p.id for p in waiting}
        for cid in [c for c in self._pending_seen if c not in ids]:
            self._pending_seen.pop(cid, None)
        n = 0
        for p in waiting:
            seen = self._pending_seen.setdefault(p.id, now)
            if now - seen >= PENDING_FLAG_S:
                self._pending_seen.pop(p.id, None)
                self._aws_give_up(p, "page", f"no text from the station's speech-to-text in {PENDING_FLAG_S / 60:.0f} min "
                                             "(it may be behind: Settings > Health); it comes up if it arrives",
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
                and (it.id not in self._aws_since or it.id in self._aws_asked)]     # (not one AWS is doing now)
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

    def _aws_watch(self) -> None:
        while not self._stopping.wait(SWEEP_S):
            try:
                self._aws_sweep()
                ok = self.aws is not None and self.aws.available()[0]
                if ok and self._aws_was_ok is False:
                    self._aws_resend()
                self._aws_was_ok = ok
            except Exception as e:  # the sweep must never die
                self.hub.log(f"hub: Amazon Transcribe sweep: {type(e).__name__}: {e}")

    # -- Settings > Radios > Channels on the station ----------------------------------------------------

    def channels_changed(self) -> None:
        super().channels_changed()
        self.channels_check(force=True)

    def channel_desired(self) -> dict:
        ok, why = self.aws_ok()
        reserve = [] if ok or self.aws is None else [k for k in self.aws.s.reserve_channels if self.aws.available(k)[0]]
        prefs = channels.for_station(self._chan_ov, self.cfg.stt_record_only)   # + the hub's record_only (#201)
        if "p25_systems" not in (self.hub.status or {}):
            # a station from before #409 refuses a whole map with a "p25:<system>:<tg>" key in it: left out for it
            prefs = {k: v for k, v in prefs.items() if not k.startswith("p25:")}
        # each FM channel's squelch: only to a station that takes it (one before it refuses a 4th argument)
        sq = None
        if self._squelch_ok():          # what of it fits the station's channels, as it reported them (the station agrees)
            kinds = {r.get("label"): r.get("channel_kind") for r in self.hub.radio_rows() if r.get("channel_kind")}
            sq = channels.fit_squelch(self._sq_ov, kinds, self.squelch_defaults)[0]
        return {"prefs": prefs, "aws": {"ok": ok, "why": why, "reserve": reserve}, "tones": dict(self._tone_ov),
                "squelch": sq, "sig": channels.sig(prefs, ok, self._tone_ov, reserve, sq)}

    def squelch_defaults(self, key: str) -> dict:
        """The station's receiver defaults, as it reported them (its config's, not the hub's)."""
        hub = getattr(self, "hub", None)          # (None while App's __init__ runs)
        rows = hub.radio_rows() if hub is not None else []
        row = next((r for r in rows if r.get("label") == key), None) or {}
        d = row.get("squelch_default")
        return {f: d.get(f) for f in channels.SQUELCH} if isinstance(d, dict) else super().squelch_defaults(key)

    def _squelch_ok(self) -> bool:
        return bool(((self.hub.status or {}).get("channels") or {}).get("squelch_ok"))

    def channels_check_tones(self) -> None:
        """A pair named here: the station gets it with the next channel push (the signature changed)."""
        self.channels_check(force=True)

    def channels_check(self, force: bool = False) -> None:
        """Send the station these choices when what it reports differs (a change here, a station that was
        offline, AWS's limit reached or a new month): one push at a time, at most every 10 s unless forced."""
        want = self.channel_desired()
        got = (self.hub.status or {}).get("channels") or {}
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
                if isinstance(res, dict) and self.hub.status is not None:
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

    def channels_sync(self) -> dict:
        want = self.channel_desired()
        got = (self.hub.status or {}).get("channels") or {}
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
        if not CLIP_ID.fullmatch(cid) or ".." in cid:
            return None
        return self.audio.get(cid)

    def has_clip(self, cid: str) -> bool:
        return self.audio.has(cid)

    def radio_status(self) -> list[dict]:
        return self.hub.radio_rows()

    def sdr(self) -> dict | None:
        return self.hub.sdr()

    def state(self) -> dict:
        st = super().state()
        st["talkgroups"] = self.hub.talkgroups
        systems = (self.hub.status or {}).get("p25_systems")
        st["p25_systems"] = systems if isinstance(systems, list) else []      # (#409) the station's other P25 systems
        st["rerun"] = self.hub.rerun()
        st["cloud"] = self.hub.cad_cloud()
        return self._hide_context(st)       # (#419: the station's lists too, where nobody is signed in)

    def _sources_poll_now(self, reason: str) -> None:
        """A radio or phone pre-alert on the hub: the station's adapters poll now (a job; never waits, never raises)."""
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
        rec = (self.hub.status or {}).get("reception")
        return rec if isinstance(rec, dict) and self.hub.connected() else None

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

    def set_talkgroup(self, tg: int, record: bool | None = None, main: bool | None = None,
                      tier: str | None = None, system: str = "") -> str | None:
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

    def _hub_janitor(self) -> None:
        while not self._stopping.wait(600):
            try:
                self._clean_hub(background=True)
            except Exception as e:  # cleanup must never take the display down
                self.hub.log(f"hub: cleanup error: {type(e).__name__}: {e}")

    def start(self) -> HubApp:
        threading.Thread(target=self._hub_janitor, name="hub-janitor", daemon=True).start()
        threading.Thread(target=self._audio_uploader, name="audio-uploader", daemon=True).start()
        threading.Thread(target=self.health.run, args=(self._stopping,), name="station-health", daemon=True).start()
        threading.Thread(target=self.error_watch.run, args=(self._stopping,), name="hub-errors", daemon=True).start()
        threading.Thread(target=self._aws_watch, name="aws-sweep", daemon=True).start()
        self.perf.start()
        self._started = True                      # the phone pre-alert: the hub's own subscription
        self.prealert_sync()
        self.start_sources()                      # held reports and promotion (#297); the adapters are the station's
        self.start_layers()                       # the maps' data, as App starts it (road projects too: #225)
        if self.vocab is not None and self.vocab.state()["state"] == "PENDING":
            self.vocab.poll()
        try:
            self._aws_recover()
        except Exception as e:  # never in the way of starting
            self.hub.log(f"hub: could not resume Amazon Transcribe work: {type(e).__name__}: {e}")
        return self

    def stop(self) -> None:
        from sizeup.timing import Phases
        t = Phases("stop")                             # the drain's share of a deploy (#264): where it goes
        self._stopping.set()
        self.perf.stop()
        if self.prealert is not None:
            self.prealert.stop()
        self.cad.sources.stop()
        self.stop_layers()
        t.mark("layers")
        if self.aws is not None:
            self.aws.stop()
            t.mark("speech-to-text")
        try:
            self.sync()
        except Exception:
            pass
        t.mark("sync")
        self.hub.log(t.line())
