"""A station's whisper as a worker in the hub's speech-to-text pool (#639 phase 2: whisper capacity across the cluster).

Every station runs whisper for its own radios. When it has nothing of its own to do, it takes clips from the hub's pool
(another station's, handed over because that station was too busy or restarting), so a clip never waits or is shed on
a busy station while another one is idle. The hub's pool decides the order: a GPU worker first, then an idle station's
whisper, then AWS; a station's whisper never gets a pre-alert clip nor an AWS channel's (it is slower than AWS).

    who       the station's own identity (its sz1 token, sizeup/cloud/stations.py, #555): no token more to issue, rotate
              or revoke; revoking the station ends its worker too. The hub calls it "station:<id>".
    when      only while the hub says the pool takes stations (its state answer's "pool": worker), whisper is up, its
              own queue holds nothing of its own (sizeup/stt.py Transcriber.busy(catchup=False) == 0) and the machine's
              CPU is under CPU_MAX_PCT: its own capture and its own clips always come first. Otherwise it tells the hub
              "paused" (with why and its load) every HEARTBEAT_S, never claiming.
    a job     claimed with a long poll of CLAIM_WAIT_S; its audio from the hub (the other station's own whisper input,
              16 kHz, padded, as whisper-server takes it); transcribed on whisper-server directly (one clip: a clip of
              its own heard meanwhile waits that long at most), the lease extended every half lease meanwhile; the
              whole answer (segments, words) sent back. whisper failing: handed back (release) at once.
    numbers   claimed, done, handed back, lost (409), the last error: the station's status ("pool_worker").
"""

from __future__ import annotations

import io
import threading
import time
import wave
from typing import Callable

import numpy as np

from sizeup.cloud.client import HubError

API = "/api/station/stt/"
CLAIM_WAIT_S = 10.0       # a claim's long poll
HEARTBEAT_S = 5.0         # a word to the hub at least this often, while it doesn't claim (the hub's LOST_S is 12 s)
CPU_MAX_PCT = 85.0        # the machine busier than this: no clips of others
IDLE_POLL_S = 2.0         # how often it looks whether it may claim
OFF_POLL_S = 30.0         # the hub says the pool doesn't take stations: looked at again this often


def wav_audio(wav: bytes) -> np.ndarray | None:
    """A 16 kHz mono 16-bit WAV -> float32 samples (as whisper-server would read it), None if it isn't one."""
    try:
        with wave.open(io.BytesIO(wav)) as w:
            if w.getsampwidth() != 2 or w.getnchannels() != 1 or w.getframerate() != 16000:
                return None
            return np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32) / 32768.0
    except (wave.Error, EOFError):
        return None


class StationWorker:
    """The station's pool worker. app: the station's App (its stt Transcriber, its whisper service); link: its
    StationLink (the hub's word on the pool, its client); cpu(): the machine's CPU % now (None: unknown)."""

    def __init__(self, app, link, client: Callable[[float], object], log: Callable[[str], None] = print,
                 cpu: Callable[[], float | None] = lambda: None, clock: Callable[[], float] = time.time):
        self.app, self.link, self._client, self.log, self.cpu, self.clock = app, link, client, log, cpu, clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.stats = {"claimed": 0, "done": 0, "released": 0, "lost": 0, "error": "", "state": "off", "why": ""}
        self._said = 0.0

    def start(self) -> StationWorker:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="pool-worker", daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    # -- may it take a clip of another station's now ------------------------------------------------------------------

    def _model(self) -> str:
        return str(getattr(self.app.cfg, "stt_model", "") or "").rsplit("/", 1)[-1]

    def load(self) -> dict:
        """Its own: the clips of its own waiting (its queue), the CPU, whisper's pace lately."""
        stt = self.app.stt
        st = stt.stats() if hasattr(stt, "stats") else {}
        cpu = self.cpu()
        return {"own": int(stt.busy(catchup=False)) if hasattr(stt, "busy") else 0, "queue": int(st.get("queued") or 0),
                **({"cpu_pct": round(float(cpu), 1)} if isinstance(cpu, (int, float)) else {}),
                **({"service_s": st["service_s"]} if isinstance(st.get("service_s"), (int, float)) else {})}

    def why_not(self, load: dict | None = None) -> str:
        """"" when it may claim now; else why not (the hub hears it with "paused")."""
        word = getattr(self.link, "pool", None) or {}
        if not word.get("worker"):
            return "the hub's pool doesn't take stations"
        if getattr(getattr(self.app, "whisper", None), "state", "ok") not in ("ok", None):
            return "whisper not ready"
        load = self.load() if load is None else load
        if load.get("own"):
            return f"its own clips first ({load['own']} waiting)"
        if isinstance(load.get("cpu_pct"), (int, float)) and load["cpu_pct"] > CPU_MAX_PCT:
            return f"the CPU busy ({load['cpu_pct']:.0f} %)"
        return ""

    # -- the loop ------------------------------------------------------------------------------------------------------

    def _run(self) -> None:
        last_word = 0.0
        while not self._stop.is_set():
            try:
                word = getattr(self.link, "pool", None) or {}
                if not word.get("worker"):
                    self.stats.update(state="off", why="the hub's pool doesn't take stations")
                    self._stop.wait(OFF_POLL_S)
                    continue
                load = self.load()
                why = self.why_not(load)
                if why:
                    self.stats.update(state="paused", why=why)
                    if self.clock() - last_word >= HEARTBEAT_S:
                        self._post("state", {"state": "paused", "model": self._model(), "load": dict(load, why=why)})
                        last_word = self.clock()
                    self._stop.wait(IDLE_POLL_S)
                    continue
                self.stats.update(state="online", why="")
                out = self._post("claim", {"wait": CLAIM_WAIT_S, "state": "online", "model": self._model(), "load": load},
                                 timeout=CLAIM_WAIT_S + 10.0)
                last_word = self.clock()
                job = out.get("job") if isinstance(out, dict) else None
                if isinstance(job, dict) and job.get("lease"):
                    self.stats["claimed"] += 1
                    self.run_job(job)
            except HubError as e:
                self.stats["error"] = str(e)[:200]
                self._say(f"pool worker: {e}")
                self._stop.wait(10.0 if e.status not in (401, 403) else 300.0)
            except Exception as e:  # noqa: BLE001 - the worker never takes the station down
                self.stats["error"] = f"{type(e).__name__}: {e}"[:200]
                self._say(f"pool worker: {self.stats['error']}")
                self._stop.wait(10.0)

    def _say(self, msg: str) -> None:
        if self.clock() - self._said >= 300.0:
            self._said = self.clock()
            self.log(msg)

    def _post(self, what: str, body: dict, timeout: float = 15.0) -> dict:
        return self._client(timeout).request("POST", API + what, body, timeout=timeout)

    def run_job(self, job: dict) -> str:
        """One claimed clip: done | released | lost."""
        lease = str(job["lease"])
        try:
            wav = self._client(30.0).get_bytes(str(job.get("audio") or f"{API}jobs/{lease}/audio"), timeout=30.0)
        except HubError as e:
            if e.status in (409, 410):
                self.stats["lost"] += 1
                return "lost"
            return self._release(lease, f"audio: {e}")
        if wav_audio(wav) is None:
            return self._release(lease, "the audio isn't a 16 kHz WAV")
        stop = threading.Event()
        ext = threading.Thread(target=self._extend, args=(lease, float(job.get("lease_s") or 10.0), stop),
                               name="pool-extend", daemon=True)
        ext.start()
        t0 = time.monotonic()
        try:
            out = self.app.stt.client.transcribe_wav(wav, str(job.get("prompt") or ""), fast=bool(job.get("fast")))
        except Exception as e:  # noqa: BLE001 - whisper failing: the next engine takes it now
            stop.set()
            self.stats["error"] = f"whisper: {type(e).__name__}"
            return self._release(lease, f"whisper: {type(e).__name__}")
        finally:
            stop.set()
        infer_ms = round((time.monotonic() - t0) * 1000)
        body = {"text": str(out.get("text") or ""), "model": self._model(), "segments": [
            {"start": s.get("start"), "end": s.get("end"), "text": s.get("text"), "words": s.get("words") or []}
            for s in (out.get("segments") or []) if isinstance(s, dict)], "timing": {"infer_ms": infer_ms}}
        try:
            self._post(f"jobs/{lease}/complete", body, timeout=20.0)
        except HubError as e:
            if e.status == 409:
                self.stats["lost"] += 1
                return "lost"
            raise
        self.stats["done"] += 1
        return "done"

    def _extend(self, lease: str, lease_s: float, stop: threading.Event) -> None:
        while not stop.wait(max(1.0, min(lease_s / 2.0, HEARTBEAT_S))):
            try:
                self._post(f"jobs/{lease}/extend", {}, timeout=5.0)
            except HubError:
                return

    def _release(self, lease: str, why: str) -> str:
        try:
            self._post(f"jobs/{lease}/release", {"why": why[:160]}, timeout=10.0)
        except HubError:
            pass
        self.stats["released"] += 1
        return "released"
