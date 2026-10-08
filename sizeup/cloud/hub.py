"""[cloud] role = "hub": the web UI and the CAD in the cloud, fed by the station. No radios, no
trunk-recorder, no whisper, no voice server: nothing here opens an SDR or starts a program, so it
runs on a bare Linux box with Python + numpy.

    POST /api/station/events      {epoch, station, events: [{seq, at, type, data, audio?}]} -> {acked}
                                  applied in order through the same App code as on the station
                                  (_page_heard, _transcribed, _retext, the radio registry); each
                                  event once: the hub remembers the last seq per outbox epoch
                                  (hub.sqlite), and pages / transmissions are keyed by their ids
    POST /api/station/state       {at, talking?, status?}: who is talking now (latest wins) and the
                                  station's radio / SDR / STT / voice rows, talkgroups, re-run
                                  progress, outbox size
    GET  /api/station/jobs?wait=25   long poll: jobs for the station -> {jobs: [{id, type, args}]}
    POST /api/station/jobs/<id>   the station's answer {ok, error?, result?, label?, audio?}
    GET  /api/station/hello      {ok}: the token works (the station's Test connection; not counted as "seen")

Every request needs `Authorization: Bearer <SIZEUP_STATION_TOKEN>` (constant-time compare); with
no token configured the hub refuses them all. Jobs (the station only connects out):
    tts    the dispatch voice when the station makes it: [dispatch] tts_engine = "polly" (the
           default here) is rendered on the hub itself (sizeup/polly.py) and comes here only when
           Polly fails; any other engine always. Cad._render_remote -> render_voice() waits up to
           20 s for the station to render it with its Kokoro; offline or too slow -> text only,
           said so ("Voice unavailable — station offline"), never a hung request
    call   set_mode / set_talkgroup / set_talkgroup_group / retranscribe from this UI, run there

The monitor's state lives in hub.sqlite and the clips in audio.sqlite (hubstore.py), next to the
CAD's cad.sqlite: all of it under one state dir that Litestream replicates (deploy/hub/).
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import re
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import replace
from pathlib import Path
from typing import Callable

from sizeup.app import CLIP_ID, App
from sizeup.cad.dispatcher import AudioUnavailable
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
        self._at = 0.0

    def update(self, entries: list, at: float | None = None) -> None:
        clean = [dict(e) for e in entries if isinstance(e, dict)][:50]
        with self._lock:
            changed = clean != self._entries
            self._entries, self._at = clean, self.clock() if at is None else at
        if changed:
            self.on_change()

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
                    r = self.resolve("mdc" if e.get("system") == "fm" else "p25", rid, str(e.get("alias") or ""))
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
        self.jobs = Jobs()
        self.live = RemoteLive(app.radios.resolve)
        self.last_seen: float | None = None
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
            detail = (f"{name}: connected, last seen {ago} s ago" + (f" · {q} event(s) still on their way up" if q else "")
                      + (f" · clocks differ by {self.skew:+.0f} s (set both to network time)" if abs(self.skew) > 5 else "")
                      + (f" · {self.errors} event(s) failed to apply, last: {self.last_error}" if self.errors else ""))
            return {"state": "ok", "word": f"{ago} s" if ago >= 5 else "", "detail": detail}
        if self.last_seen is None:
            return {"state": "down", "word": "waiting",
                    "detail": f"no station has connected since the hub started ({_hhmm(self.started)})"}
        since = self.last_seen
        return {"state": "down", "word": f"offline {_hhmm(since)}",
                "detail": f"{name}: offline since {time.strftime('%H:%M:%S', time.localtime(since))} "
                          f"(no contact for {int((now - since) // 60)} min): radios"
                          + (" and speech-to-text are" if self.own_voice else ", speech-to-text and the dispatch voice are")
                          + " unavailable until it's back; what it heard meanwhile comes up then"}

    def station_row(self) -> dict:
        h = self.health()
        return {"label": "Station", "mhz": None, "state": h["state"], "word": h["word"], "detail": h["detail"],
                "open": False, "quieting_db": None, "restarts": 0, "last_heard": self.last_seen}

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
        return {"role": "hub", "state": h["state"], "word": h["word"], "detail": h["detail"]}

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

    def _auth(self, headers) -> tuple[int, bytes, str] | None:
        if not self.token:
            return _json(403, {"error": "station ingest is off: no station token configured on the hub "
                                        "(SIZEUP_STATION_TOKEN)"})
        h = (headers.get("Authorization") or "").strip()
        given = h[7:].strip() if h[:7].lower() == "bearer " else ""
        if not given or not hmac.compare_digest(given.encode(), self.token.encode()):
            return _json(401, {"error": "bad or missing station token"})
        return None

    def handle(self, method: str, path: str, query: str, headers, body: Callable[[int], bytes | None] | None):
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
        if isinstance(d.get("at"), (int, float)):
            self.skew = now - float(d["at"])
        if isinstance(d.get("status"), dict):
            st = dict(d["status"])
            tgs = st.pop("talkgroups", None)
            if isinstance(tgs, list):
                self.talkgroups = tgs
            self.status = st
        if isinstance(d.get("talking"), list):
            self.live.update(d["talking"], now)
        return _json(200, {"ok": True})

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
        self.live = self.hub.live                      # main() sets on_change to push the display
        self.cad.live = self.live.state
        self.cad.remote_voice = self.hub.render_voice
        self.cad.audio_blobs = self.audio
        self.cad.cloud = self.hub.cad_cloud
        for d in self.mdb.load(self.store):
            self.pages.append(page_from_dict(d))
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
            self._retext(self._id(d), str(d.get("text") or ""), str(d.get("status") or "done"),
                         d.get("confidence"), str(d.get("model") or ""))
        elif kind == "heard":
            if d.get("system") in ("mdc", "p25"):
                self.radios.heard(d["system"], str(d.get("radio_id") or ""), str(d.get("alias") or ""), d.get("at"))
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
        self._page_heard(page)

    def _apply_page_text(self, d: dict) -> None:
        pid = self._id(d)
        with self._lock:
            page = next((p for p in self.pages if p.id == pid), None)
        if page is None:                              # its "page" never came: take this one as it
            page = page_from_dict(d)
            with self._lock:
                self.pages.appendleft(page)
        elif page.status != "pending":
            return                                    # applied before
        page.raw, page.confidence = str(d.get("raw") or ""), d.get("confidence")
        page.status, page.model = str(d.get("status") or "done"), str(d.get("model") or "")  # type: ignore[attr-defined]
        self._transcribed(page)

    def _apply_traffic(self, d: dict, audio: bytes | None) -> None:
        tid = self._id(d)
        if any(t.id == tid for t in list(self.store.loose) + [t for i in self.store.incidents for t in i.traffic]):
            return
        if audio:
            self.audio.put(tid, audio, "clip", at=float(d.get("end") or time.time()))
        # with the station's extras (raw, confidence, model, attach, pairs, ...): plain names only
        t = Traffic.from_dict({k: v for k, v in d.items() if EXTRA.fullmatch(str(k))})
        ids = [x.strip() for x in (t.radio_id or "").split(",") if x.strip()]
        pairs = d.get("pairs")
        # The rig from the hub's radio registry (edited here, in the CAD of record).
        if t.talkgroup is None:
            t.rig = self.radios.rig_mdc(ids)
        elif isinstance(pairs, list):
            t.rig = self.radios.rig_p25([(str(p[0]), str(p[1])) for p in pairs if isinstance(p, list) and len(p) == 2])
        else:
            t.rig = self.radios.rig_for_names(t.unit, False)[0]
        self._transcribed(t)

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

    def retranscribe(self, ids: list[str] | None = None) -> int:
        try:
            return int(self.hub.remote("retranscribe", ids) or 0)
        except (StationOffline, RuntimeError) as e:
            raise ValueError(f"speech-to-text runs on the station: {e}") from None

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
        self._started = True                      # the phone pre-alert: the hub's own subscription
        self.prealert_sync()
        return self

    def stop(self) -> None:
        self._stopping.set()
        if self.prealert is not None:
            self.prealert.stop()
        try:
            self.sync()
        except Exception:
            pass
