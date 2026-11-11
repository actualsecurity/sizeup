"""[cloud] role = "station": the Mac with the SDRs runs everything as all-in-one and also feeds the hub.

    App._emit(kind, data, clip)  -> outbox.sqlite (outbox.py), in order, durable
        page        a page off the air (paging channel), with its clip      (App._segment)
        page_text   its transcript (the hub parses the dispatch itself)     (App._transcribed)
        traffic     a finished, transcribed transmission, with its clip     (App._transcribed)
        retext      a re-run's new text for a page or transmission          (App._retext)
        rerun       a re-run is over: changed | same | failed: why          (App._rerun_finish)
        alt_text    "both" re-run: whisper's text as the alternative        (App._set_alt)
        heard       a radio ID (and P25 alias) heard on the air             (App._heard)
    forwarder thread   POST /api/station/events: batches in order, retried with backoff; the hub
                       answers with the last seq it has, which is then deleted here
    state thread       POST /api/station/state: who is talking now and a page going out now (paging:
                       each tone pair as it decodes), pushed the moment it changes
                       (sub-second; latest wins, never queued) and every 5 s the radio / SDR / STT /
                       voice status, re-run progress, the outbox size and this machine's health
                       (sizeup/cloud/health.py: CPU, memory, disk, uptime, each radio program; also the heartbeat)
                       The hub answers with its build: Watch (autoupdate.py) passes it to the
                       station's auto-updater once it holds still; this station's version and the
                       updater's last result go up in the status ("version")
    aircraft thread    POST /api/station/aircraft: our ADS-B receiver's aircraft (#235, aircraftlink.py), read every
                       second, what changed, every 1-5 s as the hub asks; dropped (never queued) while it's offline
    jobs thread        GET /api/station/jobs?wait=25 (long poll: the station never takes inbound
                       connections): "tts" renders a dispatch with the local voice; "call" runs
                       set_mode / set_talkgroup / set_talkgroup_group / retranscribe / transcribe_now /
                       set_channel_prefs
                       (Settings > Radios > Channels, as the hub has them) / emergency_all_clear (an
                       alarm's All clear: the emergency window here, App._call's keep decision) here

Clips go as 8 kHz WAV: they are already band-limited to 300-3300 Hz (stt.radio_voice), so
halving the rate halves the upload and changes nothing you can hear.
"""

from __future__ import annotations

import base64
import io
import json
import tempfile
import threading
import time
import wave
from pathlib import Path
from typing import Callable

import numpy as np

from sizeup import tz
from sizeup.cad import dispatcher
from sizeup.cad.dispatcher import AudioUnavailable
from sizeup.cloud.aircraftlink import AircraftForwarder
from sizeup.cloud.adapters import request_reset
from sizeup.cloud.autoupdate import Watch
from sizeup.cloud.client import Backoff, HubClient, HubError
from sizeup.cloud.health import Metrics, station_metrics
from sizeup.cloud.outbox import Outbox
from sizeup.tts import VoiceCfg

LIVE_REFRESH_S = 2.0     # while someone is talking the snapshot is re-sent this often (the hub drops one older than 8 s)
STATUS_S = 5.0           # radio / SDR / STT / voice status (and the heartbeat)
TALKGROUPS_S = 30.0      # the talkgroup list (big, slow to change): this often, or when a switch changes
JOB_WAIT_S = 25          # long poll; Cloudflare allows 100 s
CONNECTED_S = 15.0       # no answer from the hub this long = not connected (the station's own header)
CALLS = ("set_mode", "set_talkgroup", "set_talkgroup_group", "retranscribe", "set_channel_prefs",
         "emergency_all_clear",       # an alarm's All clear on the hub: the emergency window here closes (#128)
         "transcribe_now",            # the hub's fallback when Amazon Transcribe can't: whisper at live priority (#195)
         "set_adsb_receiver",         # Settings > Radios on the hub: the dongle reserved for ADS-B (#235)
         "sources_poll_now")          # a pre-alert on the hub: the CAD feed adapters here poll now (#297)


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


class StationLink:
    def __init__(self, app, cfg, log: Callable[[str], None] = print,
                 client: Callable[[float], HubClient] | None = None, outbox: Outbox | None = None,
                 updates: Watch | None = None):
        """outbox: the one already open (Settings > System > Station turns the link off and on
        again over the same outbox, sizeup/cloud/stationmode.py); updates: the hub's build -> the
        auto-updater (kept across that too)."""
        self.app, self.cfg, self.log = app, cfg, log
        self.updates = updates or Watch(log=log)
        self.outbox = outbox or Outbox(cfg.cloud_outbox or ":memory:", int(cfg.cloud_outbox_max_mb * 1024 * 1024))
        # what the hub last said it knows (#409), kept beside the outbox: a station starting while the hub is away
        # still queues another system's rows for it
        self._caps_file = (Path(cfg.cloud_outbox).with_name("hub_caps.json")
                           if cfg.cloud_outbox and cfg.cloud_outbox != ":memory:" else None)
        try:
            if self._caps_file is not None and self._caps_file.is_file():
                self.hub_caps = frozenset(str(c) for c in json.loads(self._caps_file.read_text()))
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
        # our ADS-B receiver's aircraft, straight to the hub (#235, sizeup/cloud/aircraftlink.py): live data, never queued
        self.aircraft = AircraftForwarder(app, cfg, self._client, log)
        app.events, app.link = self.emit, self
        app.cad.cloud = self.cad_cloud
        n, _ = self.outbox.pending()
        if n:
            log(f"hub link: {n} event(s) still queued from before, sending them first")

    # -- the app's side ---------------------------------------------------------------------------

    hub_caps: frozenset = frozenset()       # what the hub says it knows ("caps" in its answers): "p25_systems" (#409)

    def _set_caps(self, caps) -> None:
        caps = frozenset(str(c) for c in (caps or []))
        if caps != self.hub_caps:
            self.hub_caps = caps
            try:
                if getattr(self, "_caps_file", None) is not None:
                    self._caps_file.write_text(json.dumps(sorted(caps)))
            except OSError:
                pass

    def emit(self, kind: str, data: dict, clip: str | None = None) -> None:
        # Another P25 system's (#409): a hub from before it would file the row as the county's talkgroup of that
        # number, so it is not sent there (the station's own page still has it); its audio waits in its own budget.
        context = kind == "activity" or (kind in ("traffic", "alarm") and bool(data.get("system")))
        if context and "p25_systems" not in self.hub_caps:
            return
        audio = clip_8k(self.app.clip(clip)) if clip else None
        self.outbox.add(kind, data, audio, context=context)
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
        self.aircraft.start()
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
        self.aircraft.stop()

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
                self._stop.wait(backoff.fail(e))
                continue
            self.outbox.ack(acked)
            self.sent += sum(1 for x in rows if x["seq"] <= acked)
            if not self._stop.is_set():
                self._ok()
            backoff.ok()

    # -- talking now + status (latest wins) ------------------------------------------------------------

    def status(self, talkgroups: bool = True) -> dict:
        app = self.app
        n, oldest = self.outbox.pending()
        out = {"id": self.cfg.station_id, "name": self.cfg.station_name,
               "radios": [r for r in app.radio_status() if r.get("label") != "Hub"],
               "sdr": app.sdr(), "rerun": dict(app.rerun, pending=len(app._rerun_ids)),
               "channels": app.channels_applied(),
               "p25_systems": app.p25_systems_state(),       # (#409) the other P25 systems: jobs, state, activity
               "outbox": {"pending": n, "oldest": oldest, "dropped_audio": self.outbox.dropped_audio},
               "version": self.version()}
        try:
            out["health"] = station_metrics(app, self.metrics)
        except Exception as e:  # health is a report: never in the way of the status itself
            out["health"] = {"error": f"{type(e).__name__}: {e}"[:200]}
        if talkgroups:
            out["talkgroups"] = app.talkgroups_state() if app.p25 else []
        return out

    def _state(self) -> None:
        client = self._client(10.0)
        backoff = Backoff(first=1.0, fast_cap=3.0, cap=5.0)
        last_status = last_tg = 0.0
        tg_sig = None
        while not self._stop.is_set():
            now = time.time()
            talking = self.app.live.state()
            paging = self.app.paging_state()            # a page going out now (a hub before #63 ignores it)
            body: dict = {"at": now, "talking": talking, "paging": paging}
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
                self.updates.hub(r.get("build") if isinstance(r, dict) else None)
                if isinstance(r, dict):
                    self._set_caps(r.get("caps"))
                if "status" in body:
                    last_status, self._status_now = now, False
                    if "talkgroups" in body["status"]:
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
            for job in r.get("jobs") or []:        # handed to this station: run, even if just turned off
                threading.Thread(target=self._run_job, args=(job,), name="hub-job", daemon=True).start()

    def _run_job(self, job: dict) -> None:
        try:
            kind = job.get("type")
            if kind == "tts":
                res = self._tts(job.get("args") or {})
            elif kind == "call":
                res = self._call(job.get("args") or {})
            elif kind == "sources_reset":               # an admin on the hub: an incident source's reset() (#301)
                a = job.get("args") if isinstance(job.get("args"), dict) else {}
                res = request_reset(getattr(self.app, "sources_runner", None), str(a.get("source") or ""),
                                    f"{a.get('by') or 'an admin'} on the hub", self.log)
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
