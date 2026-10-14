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
           set_channel_prefs: Settings > Radios > Channels (record / transcribe / engine), the tone pairs
           named from the console (#80), and whether
           Amazon Transcribe can take audio now, sent whenever the station's reported copy (its
           state's channels.sig) differs, so a station that was offline catches up (channels_check)
Amazon Transcribe ([stt] aws, sizeup/transcribe.py) runs here: a "traffic" / "page" event with
transcript_pending "aws" (the station skipped whisper) is shown as "Transcribing" and gets AWS's text
(_aws_start -> _retext); "both" gets AWS's text with the station's kept as alt_text. If AWS can't, the
station's whisper is asked for it (retranscribe), else it says "transcription failed".

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

from sizeup import channels
from sizeup.app import CLIP_ID, App, AwsJob, channels_model
from sizeup.cad.dispatcher import AudioUnavailable
from sizeup.cad.radios import fm_system
from sizeup.cloud.autoupdate import describe
from sizeup.cloud.health import StationHealth, status_token
from sizeup.cloud.hubstore import AudioDb, MonitorDb
from sizeup.incidents import Traffic
from sizeup.radio.page import page_from_dict

JSON = "application/json"
MAX_EVENTS_BODY = 32 * 1024 * 1024
MAX_STATE_BODY = 4 * 1024 * 1024
MAX_RESULT_BODY = 16 * 1024 * 1024
LIVE_STALE_S = 8.0       # a "talking now" snapshot older than this is dropped (the station resends every 2 s)
TTS_WAIT_S = 20.0        # the dispatch voice: the station renders it; longer than this = text only
CALL_WAIT_S = 12.0       # an SDR / talkgroup switch (the monitor's switch gives up at 15 s)
JOB_PATH = re.compile(r"/api/station/jobs/([0-9a-f]{16})")
EXTRA = re.compile(r"[a-z][a-z0-9_]{0,30}")      # a transmission's field names, as the station sends them


class StationOffline(RuntimeError):
    pass


def _json(code: int, obj) -> tuple[int, bytes, str]:
    return code, json.dumps(obj, separators=(",", ":")).encode(), JSON


def _hhmm(t: float | None) -> str:
    return time.strftime("%H:%M", time.localtime(t)) if t else "?"


class Jobs:
    """Hub -> station work, handed out on the station's long poll, once each. A job nobody picked
    up before it expires is dropped (a station back after an hour doesn't replay old clicks)."""

    def __init__(self):
        self._cond = threading.Condition()
        self._jobs: OrderedDict[str, dict] = OrderedDict()

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

    def take(self, wait: float) -> list[dict]:
        with self._cond:
            self._cond.wait_for(lambda: bool(self._open()), timeout=wait)
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
                "detail": f"{name}: offline since {time.strftime('%H:%M:%S', time.localtime(since))} "
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
        h = self.health()
        n = len(self.health_.alerts()) if self.health_ is not None else 0
        return {"label": "Station", "mhz": None, "state": h["state"], "word": h["word"],
                "detail": h["detail"] + (f" · {n} health alert(s)" if n else "") + " · Station health: Settings > System",
                "open": False, "quieting_db": None, "restarts": 0, "last_heard": self.last_seen,
                "link": "#settings/system/health"}

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
        h = self.health()
        return {"role": "hub", "state": h["state"], "word": h["word"], "detail": h["detail"],
                "alerts": self.health_.alerts() if self.health_ is not None else [],
                "health_url": "/cad#settings/system/health"}

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
        if isinstance(d.get("talking"), list):
            self.live.update(d["talking"], now, d.get("paging") if isinstance(d.get("paging"), list) else None)
        return _json(200, {"ok": True, "build": self.build})

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
                try:
                    audio = base64.b64decode(ev["audio"]) if ev.get("audio") else None
                    self.app.apply(str(ev.get("type")), ev.get("data") or {}, audio)
                    n += 1
                except Exception as e:  # one bad event must not block the ones behind it
                    self.errors += 1
                    self.last_error = f"{ev.get('type')} #{seq}: {type(e).__name__}: {e}"[:200]
                    self.log(f"hub: could not apply {self.last_error}")
                self.seq = seq
            self.app.mdb.put("station_epoch", self.epoch)
            self.app.mdb.put("station_seq", str(self.seq))
            if n:
                self.app.sync()
                self.live.on_change()       # push the display now, not on its next second
            return _json(200, {"ok": True, "acked": self.seq, "applied": n})


class HubApp(App):
    """App with no radios: the station's events go through the same code (apply), the station's
    switches through jobs, and the state lives in SQLite (hubstore.py)."""

    def __init__(self, cfg, log: Callable[[str], None] = print):
        state = Path(cfg.cad_db).parent if cfg.cad_db else None
        self.mdb = MonitorDb(state / "hub.sqlite" if state else ":memory:")
        self.audio = AudioDb(state / "audio.sqlite" if state else ":memory:", int(cfg.cloud_audio_max_mb * 1024 * 1024))
        # Nothing that could open an SDR or start a program: no dongles, no trunk-recorder or its
        # capture dir, no voice server, and no captures/ (its JSON files and WAVs live in SQLite here).
        super().__init__(replace(cfg, clips_dir="", p25_dir="", p25_recorder="", sdrs=(), sdr_auto=False,
                                 shared_device="", dispatch_tts_python=""))
        self.receivers, self.modes = [], []
        self.hub = Hub(self, cfg, log)
        # station health (GitHub #79): the alerts, the banner, the push to [alerts] ntfy_topic (never Tapout's),
        # through the same Notifier as the emergency alarms (App: self.cad.alarms.notifier), one queue in order
        notifier = getattr(getattr(self.cad, "alarms", None), "notifier", None)
        self.health = self.hub.health_ = StationHealth(self.hub, cfg, self.mdb, notifier, log)
        if notifier is None:
            log("health: no push for station alerts ([alerts] ntfy_topic / SIZEUP_ALERT_TOPIC unset): banner and log only")
        self.live = self.hub.live                      # main() sets on_change to push the display
        self.cad.live = self.live.state
        self.paging_state = self.live.paging_state     # the station's page going out now
        self.cad.remote_voice = self.hub.render_voice
        self.cad.audio_blobs = self.audio
        self.cad.cloud = self.hub.cad_cloud
        self._chan_push = {"busy": False, "at": 0.0, "error": "", "again": False}
        for d in self.mdb.load(self.store):
            self.pages.append(page_from_dict(d))
        self._rematch_pages()                          # pairs named since they were heard (#80)
        for p in list(self.pages):                     # pages from before #64: rows in Radio too
            self._page_to_cad(p, replace=False)
        self._clean_hub()

    # -- the station's events ----------------------------------------------------------------------

    def apply(self, kind: str, d: dict, audio: bytes | None) -> None:
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
            if page is not None and page.status == "pending" and getattr(page, "stt", "") == "aws":
                # the station's whisper standing in for Amazon Transcribe: the page is read now
                page.raw, page.confidence = str(d.get("text") or ""), d.get("confidence")
                page.status, page.model = str(d.get("status") or "done"), str(d.get("model") or "")  # type: ignore[attr-defined]
                page.stt = ""  # type: ignore[attr-defined]
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
        elif kind == "heard":
            if d.get("system") in ("mdc", "p25", "fleetsync"):
                self.radios.heard(d["system"], str(d.get("radio_id") or ""), str(d.get("alias") or ""), d.get("at"),
                                  str(d.get("channel") or ""))
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
            self.audio.put(pid, audio, "clip", at=page.end)
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
        elif page.status != "pending" or getattr(page, "stt", "") == "aws":
            return                                    # applied before (or Amazon Transcribe reads it here)
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
        if t.talkgroup is None:
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
        if audio and not self.audio.get(tid):
            self.audio.put(tid, audio, "clip", at=float(d.get("end") or time.time()))
        t = self._traffic_of(d)
        t.text = ""
        t.emergency = True  # type: ignore[attr-defined]
        self._alarm(t, "")

    def _apply_traffic(self, d: dict, audio: bytes | None) -> None:
        tid = self._id(d)
        if any(t.id == tid for t in list(self.store.loose) + [t for i in self.store.incidents for t in i.traffic]):
            return
        if audio:
            self.audio.put(tid, audio, "clip", at=float(d.get("end") or time.time()))
        # with the station's extras (raw, confidence, model, attach, pairs, ...): plain names only
        t = self._traffic_of(d)
        key = channels.tg_key(t.talkgroup) if t.talkgroup is not None else t.source
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

    def _aws_done(self, job, text: str, conf) -> None:
        super()._aws_done(job, text, conf)
        self.sync()

    def _aws_failed(self, job, why: str) -> None:
        """Amazon Transcribe couldn't: the station's whisper does it (retranscribe, the text comes back as
        a retext event); a station that can't: "transcription failed", said on the transmission."""
        self.hub.log(f"aws transcribe: {job.kind} {job.id}: {why}" + ("" if job.alt is not None else
                                                                       ": asking the station's whisper"))
        if job.rerun:                              # re-transcribe: the station's whisper instead, said so
            self._rerun_note[job.id] = f"AWS failed ({why[:80]})"
            threading.Thread(target=self._rerun_station, args=([job.id],), name="aws-fallback", daemon=True).start()
            return
        if job.alt is not None:                    # "both": the station's text stays
            return

        def ask() -> None:
            err = ""
            try:
                n = int(self.hub.remote("retranscribe", [job.id], timeout=CALL_WAIT_S) or 0)
            except (StationOffline, RuntimeError, TypeError, ValueError) as e:
                n, err = 0, str(e)
            if n:
                return
            note = f"{channels_model()} failed ({why[:80]}); station: {err or 'no recording to re-run'}"
            if job.kind == "page" and job.page is not None:
                job.page.status, job.page.model = "error", note
                self.sync()
            else:
                self._retext(job.id, "", "error", None, note, why="transcription failed:")
                self.sync()
        threading.Thread(target=ask, name="aws-fallback", daemon=True).start()

    def _aws_recover(self) -> None:
        """After a redeploy: what was waiting for Amazon Transcribe goes to it again (or the station)."""
        if self.aws is None:
            return
        cutoff = time.time() - 3600
        with self._lock:
            pages = [p for p in self.pages if p.status == "pending" and getattr(p, "stt", "") == "aws" and p.end > cutoff]
        for p in pages:
            self._aws_from_clip(p, self.audio.get(p.id), p.label)
        for t in list(self.store.loose) + [t for i in self.store.incidents for t in i.traffic]:
            if t.status == "pending" and getattr(t, "stt", "") == "aws" and t.end > cutoff:
                from sizeup.transcribe import wav_pcm
                pcm = wav_pcm(self.audio.get(t.id) or b"")
                key = channels.tg_key(t.talkgroup) if t.talkgroup is not None else t.source
                if pcm is not None and self.aws.available()[0]:
                    self.aws.submit(key, AwsJob("traffic", t.id, key), pcm)
                else:
                    self._aws_failed(AwsJob("traffic", t.id, key), "the hub restarted")

    # -- Settings > Radios > Channels on the station ----------------------------------------------------

    def channels_changed(self) -> None:
        super().channels_changed()
        self.channels_check(force=True)

    def channel_desired(self) -> dict:
        ok, why = self.aws_ok()
        return {"prefs": dict(self._chan_ov), "aws": {"ok": ok, "why": why}, "tones": dict(self._tone_ov),
                "sig": channels.sig(self._chan_ov, ok, self._tone_ov)}

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
                res = self.hub.remote("set_channel_prefs", want["prefs"], want["aws"], want["tones"])
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
        st["rerun"] = self.hub.rerun()
        st["cloud"] = self.hub.cad_cloud()
        return st

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

    def set_mode(self, mode: str, sdr: str | None = None) -> str | None:
        return self._switch("set_mode", mode, sdr)

    def set_talkgroup(self, tg: int, record: bool | None = None, main: bool | None = None) -> str | None:
        return self._switch("set_talkgroup", int(tg), record, main)

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

    def _clean_hub(self) -> None:
        now = time.time()
        self.store.prune(now - self.keep_s)
        with self._lock:
            while self.pages and self.pages[-1].end < now - self.keep_s:
                self.pages.pop()
        self.audio.prune_clips(now - self.cfg.audio_keep_hours * 3600)
        self.sync()

    def _hub_janitor(self) -> None:
        while not self._stopping.wait(600):
            try:
                self._clean_hub()
            except Exception as e:  # cleanup must never take the display down
                self.hub.log(f"hub: cleanup error: {type(e).__name__}: {e}")

    def start(self) -> HubApp:
        threading.Thread(target=self._hub_janitor, name="hub-janitor", daemon=True).start()
        threading.Thread(target=self.health.run, args=(self._stopping,), name="station-health", daemon=True).start()
        self._started = True                      # the phone pre-alert: the hub's own subscription
        self.prealert_sync()
        self.roads.start()                        # 511NY's layers for the maps (sizeup/roads.py)
        self.gis.start()                          # the county GIS layers (sizeup/gis.py)
        self.trains.start()                       # Metro-North's trains (sizeup/trains.py)
        self.aircraft.start()                     # aircraft near HPN, while someone looks (sizeup/aircraft.py)
        self.weather.start()                      # NWS alerts, conditions, radar; NOAA tides (sizeup/weather.py)
        if self.vocab is not None and self.vocab.state()["state"] == "PENDING":
            self.vocab.poll()
        try:
            self._aws_recover()
        except Exception as e:  # never in the way of starting
            self.hub.log(f"hub: could not resume Amazon Transcribe work: {type(e).__name__}: {e}")
        return self

    def stop(self) -> None:
        self._stopping.set()
        if self.prealert is not None:
            self.prealert.stop()
        self.roads.stop()
        self.gis.stop()
        self.trains.stop()
        self.aircraft.stop()
        self.weather.stop()
        if self.aws is not None:
            self.aws.stop()
        try:
            self.sync()
        except Exception:
            pass
