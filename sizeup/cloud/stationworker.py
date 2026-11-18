"""A station's whisper as a worker in the hub's speech-to-text pool (#639 phase 2: whisper capacity across the cluster).

Every station runs whisper for its own radios. With an admin's go-ahead for that station (Settings > Station > Stations,
per station, off by default), it takes clips from the hub's pool (another station's, handed over because that station
was too busy or restarting) while it has nothing of its own to do, so a clip never waits or is shed on a busy station
while another one is idle. The hub's pool decides the order: a GPU worker first, then an idle station's whisper, then
AWS; a station's whisper never gets a pre-alert clip, an AWS channel's, or one of its own station's.

Its own radios always come first. whisper-server takes one request at a time, so a clip of others' must never hold up
a page or a fireground:

    when      only while the hub says this station's whisper is in the pool, whisper is up, none of its own clips wait,
              none of its own has been heard for QUIET_S (a quiet spell, as the catch-up's), and the CPU (measured from
              /proc/stat; on WSL the Linux side's) is under CPU_MAX_PCT. Otherwise "paused" (with why and its load)
              every HEARTBEAT_S, never claiming.
    how much  only clips it decodes in MAX_DECODE_S at its own measured pace (its whisper's last transcripts: run time
              over audio length): it tells the hub max_s, and the hub never hands it a longer one. Always one greedy
              pass (no temperature fallback: whisper-server's temperature_inc 0).
    claimed   looked at again at once: its own work came meanwhile -> handed back (release) at once.
    pre-empt  while it decodes, one of its own live clips (urgent, high, live) waiting -> the decode's connection closed
              (whisper-server stops a request whose client went away: "client disconnected, aborted processing",
              checked on whisper.cpp 1.9.4) and the job handed back, so its own clip is next on whisper at once.
    a job     its audio from the hub (the other station's own STT input), the lease extended meanwhile, the whole answer
              (segments, words) sent back; whisper failing: handed back.
    numbers   claimed, done, handed back, pre-empted, lost (409), the last error: the station's status ("pool_worker").
"""

from __future__ import annotations

import http.client
import io
import json
import os
import threading
import time
import urllib.parse
import uuid
import wave
from typing import Callable

import numpy as np

from sizeup.cloud.client import HubError
from sizeup.stt import LIVE_CLASSES

API = "/api/station/stt/"
CLAIM_WAIT_S = 10.0       # a claim's long poll
HEARTBEAT_S = 5.0         # a word to the hub at least this often, while it doesn't claim (the hub's LOST_S is 12 s)
CPU_MAX_PCT = 85.0        # the machine busier than this: no clips of others
QUIET_S = 90.0            # none of its own clips heard for this long first (sizeup/stt.py CATCHUP_QUIET_S)
MAX_DECODE_S = 8.0        # only clips it decodes in this long at its own pace (its own page waits that long at most,
                          # and the pre-emption cuts even that short)
DEFAULT_RTF = 1.0         # its pace before it has transcribed anything: decode seconds per audio second
PREEMPT_POLL_S = 0.1      # how often a remote decode looks whether its own live work came
IDLE_POLL_S = 2.0         # how often it looks whether it may claim
OFF_POLL_S = 30.0         # the hub says this station's whisper isn't in the pool: looked at again this often


def wav_audio(wav: bytes) -> np.ndarray | None:
    """A 16 kHz mono 16-bit WAV -> float32 samples (as whisper-server would read it), None if it isn't one."""
    try:
        with wave.open(io.BytesIO(wav)) as w:
            if w.getsampwidth() != 2 or w.getnchannels() != 1 or w.getframerate() != 16000:
                return None
            return np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32) / 32768.0
    except (wave.Error, EOFError):
        return None


class CpuMeter:
    """The machine's CPU %, from /proc/stat between two looks (Linux; on WSL its Linux side's: Windows' own load isn't
    seen from there); the 1-minute load average over the CPUs where there is no /proc/stat. None when neither reads."""

    def __init__(self, path: str = "/proc/stat"):
        self.path, self._last = path, None

    def __call__(self) -> float | None:
        try:
            with open(self.path) as f:
                vals = [int(x) for x in f.readline().split()[1:]]
            idle, total = vals[3] + (vals[4] if len(vals) > 4 else 0), sum(vals)
            last, self._last = self._last, (idle, total)
            if last is None or total <= last[1]:
                return None
            return round(100.0 * (1.0 - (idle - last[0]) / (total - last[1])), 1)
        except (OSError, ValueError, IndexError):
            try:
                return 100.0 * os.getloadavg()[0] / max(1, os.cpu_count() or 1)
            except (OSError, AttributeError):
                return None


class StationWorker:
    """The station's pool worker. app: the station's App (its stt Transcriber, its whisper service); link: its
    StationLink (the hub's word on the pool, its client); cpu(): the machine's CPU % now (None: unknown)."""

    def __init__(self, app, link, client: Callable[[float], object], log: Callable[[str], None] = print,
                 cpu: Callable[[], float | None] | None = None, clock: Callable[[], float] = time.time):
        self.app, self.link, self._client, self.log, self.clock = app, link, client, log, clock
        self.cpu = cpu if cpu is not None else CpuMeter()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.stats = {"claimed": 0, "done": 0, "released": 0, "preempted": 0, "lost": 0, "error": "", "state": "off",
                      "why": ""}
        self._said = 0.0
        self._conn: http.client.HTTPConnection | None = None
        self._cpu_last: float | None = None

    def start(self) -> StationWorker:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="pool-worker", daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._abort()

    # -- may it take a clip of another station's now ------------------------------------------------------------------

    def _model(self) -> str:
        return str(getattr(self.app.cfg, "stt_model", "") or "").rsplit("/", 1)[-1]

    def _stats(self) -> dict:
        stt = self.app.stt
        return stt.stats() if hasattr(stt, "stats") else {}

    def rtf(self) -> float:
        """Its own whisper's pace lately: decode seconds per audio second (median of its last transcripts)."""
        timed = list(getattr(self.app.stt, "timed", ()))[-30:]
        r = sorted(x[3] / x[4] for x in timed if len(x) > 4 and x[4] and x[4] > 0.5 and x[3] is not None)
        return r[len(r) // 2] if r else DEFAULT_RTF

    def load(self) -> dict:
        """Its own: the clips of its own waiting, the CPU, its pace, and the longest clip it would take (max_s)."""
        stt, st = self.app.stt, self._stats()
        cpu = self.cpu()
        if isinstance(cpu, (int, float)):
            self._cpu_last = float(cpu)
        rtf = self.rtf()
        return {"own": int(stt.busy(catchup=False)) if hasattr(stt, "busy") else 0, "queue": int(st.get("queued") or 0),
                "rtf": round(rtf, 2), "max_s": round(MAX_DECODE_S / max(0.05, rtf), 1),
                **({"cpu_pct": round(self._cpu_last, 1)} if self._cpu_last is not None else {}),
                **({"service_s": st["service_s"]} if isinstance(st.get("service_s"), (int, float)) else {})}

    def why_not(self, load: dict | None = None) -> str:
        """"" when it may claim now; else why not (the hub hears it with "paused")."""
        word = getattr(self.link, "pool", None) or {}
        if not word.get("worker"):
            return "this station's whisper isn't in the pool"
        if getattr(getattr(self.app, "whisper", None), "state", "ok") not in ("ok", None):
            return "whisper not ready"
        load = self.load() if load is None else load
        if load.get("own"):
            return f"its own clips first ({load['own']} waiting)"
        ago = self._stats().get("ours_ago_s")
        if isinstance(ago, (int, float)) and ago < QUIET_S:
            return f"its own traffic {ago:.0f} s ago"
        if isinstance(load.get("cpu_pct"), (int, float)) and load["cpu_pct"] > CPU_MAX_PCT:
            return f"the CPU busy ({load['cpu_pct']:.0f} %)"
        return ""

    def _own_live_waiting(self) -> bool:
        """One of its own clips of a live class (urgent, high, live) waits for whisper: queued, or already sent to
        whisper-server (where it waits behind the remote decode)."""
        stt = self.app.stt
        cur = stt.current() if hasattr(stt, "current") else None
        if cur is not None and cur[0] in LIVE_CLASSES:
            return True
        return int(self._stats().get("live") or 0) > 0

    # -- the loop ------------------------------------------------------------------------------------------------------

    def _run(self) -> None:
        last_word = 0.0
        while not self._stop.is_set():
            try:
                word = getattr(self.link, "pool", None) or {}
                if not word.get("worker"):
                    self.stats.update(state="off", why="this station's whisper isn't in the pool")
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
        """One claimed clip: done | released | preempted | lost."""
        lease = str(job["lease"])
        why = self.why_not()                       # (its own work may have come during the claim's long poll)
        if why:
            return self._release(lease, f"the station's own work first ({why})")
        try:
            wav = self._client(30.0).get_bytes(str(job.get("audio") or f"{API}jobs/{lease}/audio"), timeout=30.0)
        except HubError as e:
            if e.status in (409, 410):
                self.stats["lost"] += 1
                return "lost"
            return self._release(lease, f"audio: {e}")
        if wav_audio(wav) is None:
            return self._release(lease, "the audio isn't a 16 kHz WAV")
        stop, preempted = threading.Event(), threading.Event()
        threading.Thread(target=self._extend, args=(lease, float(job.get("lease_s") or 10.0), stop),
                         name="pool-extend", daemon=True).start()
        threading.Thread(target=self._watch, args=(stop, preempted), name="pool-preempt", daemon=True).start()
        t0 = time.monotonic()
        try:
            out = self.decode(wav, str(job.get("prompt") or ""))
        except Exception as e:  # noqa: BLE001 - whisper failing, or its own clip came: the next engine takes it now
            stop.set()
            if preempted.is_set():
                self.stats["preempted"] += 1
                self._release(lease, "the station's own live clip came")
                return "preempted"
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

    def decode(self, wav: bytes, prompt: str = "") -> dict:
        """The WAV through this station's whisper-server, one greedy pass, on a connection the pre-emption can close."""
        url = urllib.parse.urlsplit(str(getattr(self.app.stt.client, "url", "") or "http://127.0.0.1:8179/inference"))
        boundary = uuid.uuid4().hex
        fields = [("response_format", "verbose_json"), ("temperature", "0.0"), ("temperature_inc", "0.0")]
        if prompt:
            fields.append(("prompt", prompt))
        parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{n}"\r\n\r\n{v}\r\n'.encode() for n, v in fields]
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="tx.wav"\r\n'
                      "Content-Type: audio/wav\r\n\r\n").encode() + wav + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        conn = http.client.HTTPConnection(url.hostname or "127.0.0.1", url.port or 80, timeout=MAX_DECODE_S * 4 + 30.0)
        self._conn = conn
        try:
            conn.request("POST", url.path or "/inference", body=b"".join(parts),
                         headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
            r = conn.getresponse()
            raw = r.read()
            if r.status != 200:
                raise OSError(f"whisper-server said {r.status}")
            out = json.loads(raw)
            return out if isinstance(out, dict) else {}
        finally:
            self._conn = None
            conn.close()

    def _abort(self) -> None:
        conn = self._conn
        if conn is not None:
            try:
                if conn.sock is not None:
                    conn.sock.shutdown(2)          # (whisper-server sees the client gone and stops the decode)
                conn.close()
            except OSError:
                pass

    def _watch(self, stop: threading.Event, preempted: threading.Event) -> None:
        """While it decodes another station's clip: one of its own live clips waiting -> the decode cut at once."""
        while not stop.wait(PREEMPT_POLL_S):
            if self._own_live_waiting() or self._stop.is_set():
                preempted.set()
                self._abort()
                return

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
