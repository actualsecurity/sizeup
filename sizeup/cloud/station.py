"""[cloud] role = "station": the Mac with the SDRs runs everything as all-in-one and also feeds the hub.

    App._emit(kind, data, clip)  -> outbox.sqlite (outbox.py), in order, durable
        page        a page off the air (paging channel), with its clip      (App._segment)
        page_text   its transcript (the hub parses the dispatch itself)     (App._transcribed)
        traffic     a finished, transcribed transmission, with its clip     (App._transcribed)
        retext      a re-run's new text for a page or transmission          (App._retext)
        heard       a radio ID (and P25 alias) heard on the air             (App._heard)
    forwarder thread   POST /api/station/events: batches in order, retried with backoff; the hub
                       answers with the last seq it has, which is then deleted here
    state thread       POST /api/station/state: who is talking now (pushed the moment it changes:
                       sub-second; latest wins, never queued) and every 5 s the radio / SDR / STT /
                       voice status, re-run progress and the outbox size (also the heartbeat)
    jobs thread        GET /api/station/jobs?wait=25 (long poll: the station never takes inbound
                       connections): "tts" renders a dispatch with the local voice; "call" runs
                       set_mode / set_talkgroup / set_talkgroup_group / retranscribe here

Clips go as 8 kHz WAV: they are already band-limited to 300-3300 Hz (stt.radio_voice), so
halving the rate halves the upload and changes nothing you can hear.
"""

from __future__ import annotations

import base64
import io
import random
import tempfile
import threading
import time
import wave
from pathlib import Path
from typing import Callable

import numpy as np

from sizeup.cad import dispatcher
from sizeup.cad.dispatcher import AudioUnavailable
from sizeup.cloud.client import HubClient, HubError
from sizeup.cloud.outbox import Outbox
from sizeup.tts import VoiceCfg

LIVE_REFRESH_S = 2.0     # while someone is talking the snapshot is re-sent this often (the hub drops one older than 8 s)
STATUS_S = 5.0           # radio / SDR / STT / voice status (and the heartbeat)
TALKGROUPS_S = 30.0      # the talkgroup list (big, slow to change): this often, or when a switch changes
JOB_WAIT_S = 25          # long poll; Cloudflare allows 100 s
CONNECTED_S = 15.0       # no answer from the hub this long = not connected (the station's own header)
CALLS = ("set_mode", "set_talkgroup", "set_talkgroup_group", "retranscribe")


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


class StationLink:
    def __init__(self, app, cfg, log: Callable[[str], None] = print,
                 client: Callable[[float], HubClient] | None = None, outbox: Outbox | None = None):
        """outbox: the one already open (Settings > System > Station turns the link off and on
        again over the same outbox, sizeup/cloud/stationmode.py)."""
        self.app, self.cfg, self.log = app, cfg, log
        self.outbox = outbox or Outbox(cfg.cloud_outbox or ":memory:", int(cfg.cloud_outbox_max_mb * 1024 * 1024))
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
        self._threads: list[threading.Thread] = []
        app.events, app.link = self.emit, self
        app.cad.cloud = self.cad_cloud
        n, _ = self.outbox.pending()
        if n:
            log(f"hub link: {n} event(s) still queued from before, sending them first")

    # -- the app's side ---------------------------------------------------------------------------

    def emit(self, kind: str, data: dict, clip: str | None = None) -> None:
        audio = clip_8k(self.app.clip(clip)) if clip else None
        self.outbox.add(kind, data, audio)
        self._wake.set()

    def live_changed(self) -> None:
        self._live_wake.set()

    def start(self) -> StationLink:
        if self._threads:
            return self
        for name, fn in (("hub-forward", self._forward), ("hub-state", self._state), ("hub-jobs", self._jobs)):
            t = threading.Thread(target=fn, name=name, daemon=True)
            t.start()
            self._threads.append(t)
        return self

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

    # -- health -------------------------------------------------------------------------------------

    def _ok(self) -> None:
        with self._lock:
            was = self.connected()
            self.last_ok, self.last_error, self.error_since = time.time(), "", None
        if not was:
            self.log(f"hub link: connected to {self.cfg.cloud_hub_url}")

    def _fail(self, e: Exception) -> None:
        with self._lock:
            first = self.error_since is None
            self.last_error = str(e)[:200]
            self.error_since = self.error_since or time.time()
        if first:
            self.log(f"hub link: {e}")

    def connected(self) -> bool:
        return self.last_ok is not None and time.time() - self.last_ok < CONNECTED_S and self.error_since is None

    def status_row(self) -> dict:
        """The station's own header: a "Hub" row next to the radios."""
        n, oldest = self.outbox.pending()
        queued = f"{n} event(s) waiting to go up" + (f", oldest {time.strftime('%H:%M', time.localtime(oldest))}"
                                                     if oldest else "") if n else "nothing waiting"
        if self.connected():
            state, word = "ok", (f"{n} queued" if n > 5 else "")
            detail = f"connected to the hub {self.cfg.cloud_hub_url} · {queued}"
        elif self.last_ok is None and self.error_since is None:
            state, word, detail = "starting", "", f"connecting to the hub {self.cfg.cloud_hub_url}"
        else:
            since = self.error_since or self.last_ok
            state, word = "down", f"offline {time.strftime('%H:%M', time.localtime(since))}"
            detail = (f"can't reach the hub {self.cfg.cloud_hub_url} since {time.strftime('%H:%M:%S', time.localtime(since))}"
                      + (f" ({self.last_error})" if self.last_error else "") + f" · {queued}: nothing is lost, "
                      "it goes up when the hub is back")
        if self.outbox.dropped_audio:
            detail += f" · {self.outbox.dropped_audio} clip(s) dropped to keep the outbox under its limit"
        return {"label": "Hub", "mhz": None, "state": state, "word": word, "detail": detail, "open": False,
                "quieting_db": None, "restarts": 0, "last_heard": None}

    def cad_cloud(self) -> dict:
        """The station's console header: the CAD of record is the hub's."""
        row = self.status_row()
        return {"role": "station", "hub_url": self.cfg.cloud_hub_url, "state": row["state"], "detail": row["detail"]}

    # -- events (in order, durable) ------------------------------------------------------------------

    def _forward(self) -> None:
        client = self._client(60.0)
        backoff = 1.0
        while not self._stop.is_set():
            rows = self.outbox.batch()
            if not rows:
                self._wake.wait(5.0)
                self._wake.clear()
                continue
            body = {"epoch": self.outbox.epoch, "station": self.cfg.station_id,
                    "events": [dict(r, audio=base64.b64encode(r["audio"]).decode() if r["audio"] else None)
                               for r in rows]}
            try:
                r = client.request("POST", "/api/station/events", body, timeout=60.0)
                acked = int(r["acked"])
            except (HubError, KeyError, TypeError, ValueError) as e:
                self._fail(e)
                self._stop.wait(backoff * random.uniform(0.8, 1.2))
                backoff = min(backoff * 2, 60.0)
                continue
            self.outbox.ack(acked)
            self.sent += sum(1 for x in rows if x["seq"] <= acked)
            if not self._stop.is_set():
                self._ok()
            backoff = 1.0

    # -- talking now + status (latest wins) ------------------------------------------------------------

    def status(self, talkgroups: bool = True) -> dict:
        app = self.app
        n, oldest = self.outbox.pending()
        out = {"id": self.cfg.station_id, "name": self.cfg.station_name,
               "radios": [r for r in app.radio_status() if r.get("label") != "Hub"],
               "sdr": app.sdr(), "rerun": dict(app.rerun, pending=len(app._rerun_ids)),
               "outbox": {"pending": n, "oldest": oldest, "dropped_audio": self.outbox.dropped_audio}}
        if talkgroups:
            out["talkgroups"] = app.tg.state() if app.p25 else []
        return out

    def _state(self) -> None:
        client = self._client(10.0)
        last_status = last_tg = 0.0
        tg_sig = None
        while not self._stop.is_set():
            now = time.time()
            talking = self.app.live.state()
            body: dict = {"at": now, "talking": talking}
            if self._status_now or now - last_status >= STATUS_S:
                tgs = self.app.tg.state() if self.app.p25 else []
                sig = [(r["tg"], r["record"], r["main"]) for r in tgs]
                send_tg = sig != tg_sig or now - last_tg >= TALKGROUPS_S
                body["status"] = self.status(talkgroups=False)
                if send_tg:
                    body["status"]["talkgroups"] = tgs
            try:
                client.request("POST", "/api/station/state", body, timeout=10.0)
                self._ok()
                if "status" in body:
                    last_status, self._status_now = now, False
                    if "talkgroups" in body["status"]:
                        last_tg, tg_sig = now, [(r["tg"], r["record"], r["main"]) for r in body["status"]["talkgroups"]]
            except HubError as e:
                self._fail(e)
                self._stop.wait(3.0)
            self._live_wake.wait(LIVE_REFRESH_S if talking else STATUS_S)
            self._live_wake.clear()
            self._stop.wait(0.05)           # a burst of changes goes up as one update

    # -- jobs from the hub ------------------------------------------------------------------------------

    def _jobs(self) -> None:
        client = self._client(JOB_WAIT_S + 15.0)
        backoff = 2.0
        while not self._stop.is_set():
            try:
                r = client.request("GET", f"/api/station/jobs?wait={JOB_WAIT_S}", timeout=JOB_WAIT_S + 15.0)
            except HubError as e:
                self._fail(e)
                self._stop.wait(backoff * random.uniform(0.8, 1.2))
                backoff = min(backoff * 2, 30.0)
                continue
            backoff = 2.0
            if not self._stop.is_set():
                self._ok()
            for job in r.get("jobs") or []:        # handed to this station: run, even if just turned off
                threading.Thread(target=self._run_job, args=(job,), name="hub-job", daemon=True).start()

    def _run_job(self, job: dict) -> None:
        try:
            kind = job.get("type")
            if kind == "tts":
                res = self._tts(job.get("args") or {})
            elif kind == "call":
                res = self._call(job.get("args") or {})
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
        """A dispatch said by this station's voice (Kokoro here; `say` as its fallback), for the hub."""
        text, v = a.get("text"), a.get("voice") or {}
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
        fn = a.get("fn")
        if fn not in CALLS:
            return {"ok": False, "error": f"not allowed from the hub: {fn!r}"}
        args = a.get("args") or []
        if not isinstance(args, list):
            return {"ok": False, "error": "args: a list"}
        result = getattr(self.app, fn)(*args)
        self._status_now = True             # the switch shows on the hub right away
        self._live_wake.set()
        return {"ok": True, "result": result}
