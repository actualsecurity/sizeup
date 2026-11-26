"""The speech-to-text input, station -> hub: every engine transcribes the same audio.

The station sends each transmission and page with its listening clip (8 kHz WAV, the voice band, de-emphasised and
de-whined for people: sizeup/cloud/station.py clip_8k). Its own whisper transcribes something else: the audio before
the listening filter, through radio_voice(deemph=<FM>, nr=False) at 16 kHz (sizeup/stt.py Transcriber._run). So a hub
engine (Amazon Transcribe, a GPU worker) that took the listening clip heard other audio than the station's whisper,
and their transcripts couldn't be compared. Now the station also sends that exact input, losslessly, on its own lane.

Station (StationLink):
    stt_pcm(audio, fm)        int16, 16 kHz, mono: exactly the samples the station's whisper transcribes (the same
                              radio_voice call as Transcriber._run, quantised as stt.wav_bytes does), without the 0.5 s
                              of silence WhisperClient puts on each side. FM de-emphasis is already applied (FM
                              channels and pages); P25 and the airband never get it, as in whisper.
    Encoder                   App._stt hands it each transmission / page (a reference to its audio, nothing more: the
                              radio threads never wait); ONE low-priority thread makes the FLAC (sizeup/cloud/flac.py)
                              and puts it in the outbox's own lane (outbox.py add_stt). Bounded (QUEUE_ITEMS, QUEUE_S
                              of audio): when full it drops and counts. Never part of the page / traffic event.
    priority (#677)           each has its channel's (App._stt_lane, from whisper's own classes): PRE the pre-alerts
                              (pages, a talkgroup of the alert tier: PD dispatch), FIRE our fire traffic (whisper's
                              URGENT: Fire/EMS 15, the firegrounds), NORMAL the airband and other traffic, LOW a context
                              talkgroup of another tier. The encoder makes the highest first; the outbox sends the
                              highest first (PRE / FIRE: fresh before stale); under pressure both drop the lowest first
                              (the oldest within it): the airband never costs a fireground clip
    StationLink._stt_forward  POST /api/station/stt, gzipped, only while no event waits in the outbox and only to a
                              hub that says it takes them ("stt_input" in its caps). FIFO (#678): an input enters the
                              lane only after its own traffic / page event is queued (StationLink._stt_made holds one
                              made first), and the lane is woken when the events are acked: it follows its event at once

Wire (POST /api/station/stt, Content-Encoding: gzip):
    {"v": 1, "station": <id>, "items": [{"n": <outbox row>, "id": <clip id>, "kind": "traffic" | "page",
        traffic: "source", "talkgroup", "system"; page: "label"; "end",
        "stt": {"v": 1, "format": "flac", "input": "stt-src", "kind": ..., "rate": 16000, "samples": <n>,
                "voice_offset_s": <s>,        pages only: where this audio starts in the listening clip (after the tones)
                "audio": "<base64 FLAC>"}}]}
    -> {"ok": true, "stored": [n, ...], "refused": [n, ...]}  (both are done with: the station deletes them)
An older hub has no such route and doesn't say "stt_input": the station keeps them (bounded) and never sends them.

Hub (HubApp):
    from_wire(stt)             checked before it is kept: FLAC, its STREAMINFO's rate and sample count as the metadata
                               says, at most MAX_SECONDS, with its MD5 (flac.info: nothing decoded)
    store(audio_db, cid, ...)  the FLAC as sent, in audio.sqlite / the store as clip "<id>.src" (key
                               audio/clip/<yyyy>/<mm>/<dd>/<id>.src.flac), with the clip's channel, talkgroup and time,
                               so it is uploaded, cached and pruned with the clip (#414, #428). Never served by
                               /api/audio (HubApp.clip refuses the id). It may come before or after its clip's event.
    read(audio_db, cid, voice_offset_s) -> SttAudio | None
                               the STT input when the station sent it (input "stt-src"), else the listening clip from
                               voice_offset_s on (input "listening-8k": an older station, dropped, or not here yet),
                               None when there is neither. HubApp.read_stt_audio(cid, wait_s=...) finds a page's
                               voice_offset_s and can wait a little for an STT input still on its way.
"""

from __future__ import annotations

import base64
import binascii
import io
import json
import os
import sys
import threading
import time
import wave
from collections import deque
from dataclasses import dataclass
from typing import Callable

import numpy as np

from sizeup.audiostore import STT_SRC
from sizeup.cloud import flac
from sizeup.stt import radio_voice, wav_bytes

RATE = 16000                    # what the station's whisper gets
INPUT_SRC = "stt-src"           # the station's speech-to-text input itself
INPUT_LISTENING = "listening-8k"  # the 8 kHz listening clip, in its place (a transcript from it: not comparable)
CAP = "stt_input"               # what a hub that takes them says in its caps
PATH = "/api/station/stt"
MAX_SECONDS = 300               # one STT input at most this long (a longer transmission goes without one, counted)
MAX_BLOB = 8 * 1024 * 1024      # ... and this big (300 s of noisy P25 is ~7 MB)
QUEUE_ITEMS = 200               # the encoder's queue: at most this many waiting ...
QUEUE_S = 600.0                 # ... and this much audio (~77 MB: the frames are float64, kept as they are because
                                # whisper's input is made from them bit for bit): over either, dropped and counted
NICE = 10                       # the encoder thread's niceness (Linux: per thread), under the radio threads
WHISPER_PAD = 8000              # WhisperClient's silence each side (0.5 s at 16 kHz)
DROP_REASONS = ("queue", "too_big", "failed", "outbox", "refused")
PRE, FIRE, NORMAL, LOW = 3, 2, 1, 0   # an STT input's priority in the lane (#677, App._stt_lane; outbox.py STT_*)


def stt_pcm(audio: np.ndarray, fm: bool) -> np.ndarray:
    """The samples the station's whisper transcribes for this audio (sizeup/stt.py Transcriber._run, then the int16
    of its WAV), at 16 kHz."""
    x = radio_voice(audio, deemph=fm, nr=False)
    return (np.clip(x, -1, 1) * 32767).astype(np.int16)


def describe(item) -> dict:
    """The clip id and where it was heard (the hub keeps the STT input with the clip's channel and retention)."""
    from sizeup.radio.page import Page
    if isinstance(item, Page):
        return {"id": item.id, "kind": "page", "label": item.label, "end": float(item.end)}
    return {"id": item.id, "kind": "traffic", "source": getattr(item, "source", ""),
            "talkgroup": getattr(item, "talkgroup", None), "system": getattr(item, "system", "") or "",
            "end": float(getattr(item, "end", 0.0) or 0.0)}


def blob(item, audio: np.ndarray) -> tuple[bytes, dict] | None:
    """(FLAC, metadata) for one transmission or page (audio: what App._stt gets). None when there is no audio, or
    it is too long / too big to send."""
    return _blob(describe(item), getattr(item, "fm", True), getattr(item, "voice_offset_s", 0.0), audio)


def _blob(desc: dict, fm: bool, voice_offset_s: float, audio: np.ndarray) -> tuple[bytes, dict] | None:
    if audio is None or not len(audio) or len(audio) > MAX_SECONDS * RATE:
        return None
    pcm = stt_pcm(audio, fm)
    data = flac.encode(pcm, RATE)
    if len(data) > MAX_BLOB:
        return None
    meta = {"v": 1, "format": "flac", "input": INPUT_SRC, "kind": desc["kind"], "rate": RATE, "samples": int(len(pcm))}
    if desc["kind"] == "page":                              # its voice, after the tones
        meta["voice_offset_s"] = round(float(voice_offset_s), 4)
    return data, meta


ENCODER_ENV = "SIZEUP_STT_ENCODER"   # auto (default) | thread | process: where the FLAC is made (Encoder below)
WORKER_FAILS = 3                # the worker dead this often in a row: the thread, until WORKER_RETRY_S later
WORKER_RETRY_S = 300.0          # ... (and auto without the service's socket, or another release's: looked again then)
QUICK_LOOK_S = 10.0             # ... but every this often for QUICK_FOR_S after the start or after the worker died
QUICK_FOR_S = 300.0             #     (a service restarting: back on it within seconds, not 5 min)
WAIT_CAP_S = 120.0              # a wait on the worker's answer: at most this long (then waited for again) ...
WORKER_SLOW = 3                 # ... this many waits in a row without one, or ...
UNHEALTHY_S = 600.0             # ... this long: the worker is unhealthy (NORMAL / LOW wait queued)
HELD_LOOK_S = 1.0               # held (whisper behind): looked at again this often
HOLD_MAX_S = 300.0              # one hold at most this long (then NORMAL / LOW go on until whisper has caught up)


def _lower_priority() -> None:
    try:
        os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), NICE)    # Linux: this thread only
    except (AttributeError, OSError):
        pass                                                                # (macOS: per process only; left as is)


class Encoder:
    """The station's STT inputs, made off the radio path: submit() only queues a reference to the audio and the FLAC is
    handed to sink(data, flac, meta, prio) (the outbox's lane). The highest priority is made first; when the queue is
    full a new one takes the place of lower-priority ones (the oldest of the lowest first), else it is dropped.

    Two threads, so that a pre-alert never waits on anything:
      "hub-stt-input"     PRE and FIRE (pages, alert-tier talkgroups, fire traffic): always made here, on the station,
                          at once (small, cheap). It also hands NORMAL / LOW to the helper, one at a time.
      "hub-stt-input-bg"  NORMAL / LOW, one in flight: in the worker (sizeup/cloud/sttencode.py) when there is one,
                          else here (mode "thread", no service, another release's, or a dead one).

    The worker (about 40 ms of CPU an audio-second on the i5, so never at the station's own cgroup weight): mode
    (ENCODER_ENV) "auto" the sizeup-stt-encode service (its own cgroup, #709) when its socket is there; "process" a
    child (the tests: the same bytes either way); "thread" never. One connection, one clip in flight, never sent twice:
    no answer within its time (WAIT_CAP_S at most) and the helper goes on waiting on the same connection; WORKER_SLOW
    such waits in a row, or UNHEALTHY_S without an answer, and the worker is unhealthy: NORMAL / LOW wait queued
    (subject to the eviction above), never made on the station for it, until it answers. A dead worker (refused, reset,
    gone): the helper makes that clip; WORKER_FAILS in a row, or another release's, and the helper makes them for
    WORKER_RETRY_S. Each change of state is logged once.

    held(): whisper is behind on live traffic (sizeup/cloud/station.py _whisper_behind). PRE and FIRE are never held;
    NORMAL and LOW wait (bounded as above, the priorities kept) and drain, highest first, once whisper caught up, or
    after HOLD_MAX_S of one hold.
    """

    def __init__(self, sink: Callable[..., None], log: Callable[[str], None] = print,
                 max_items: int = QUEUE_ITEMS, max_s: float = QUEUE_S, held: Callable[[], bool] | None = None,
                 mode: str | None = None):
        self.sink, self.log, self.max_items, self.max_s = sink, log, max_items, max_s
        self.held = held
        self.mode = (mode or os.environ.get(ENCODER_ENV) or "auto").strip().lower()
        self._cond = threading.Condition()
        self._q: dict[int, deque] = {PRE: deque(), FIRE: deque(), NORMAL: deque(), LOW: deque()}
        self._queued_s = 0.0
        self._thread: threading.Thread | None = None
        self._bg: threading.Thread | None = None
        self._stop = False
        self.made = 0
        self.dropped = {"queue": 0, "too_big": 0, "failed": 0}
        self.dropped_pre = 0            # ... of them, the pre-alert channels' for want of room ("queue" only)
        self.dropped_fire = 0           # ... and fire traffic's
        self.holding = False            # NORMAL / LOW held now (whisper behind)
        self.held_s = 0.0               # ... for this long in all
        self._hold_since: float | None = None
        self.where = ""                 # "service" | "process" | "thread (why)": where NORMAL / LOW are made
        self._worker = None             # sttencode.Client (the helper's)
        self._target: tuple | None = None   # ("sock", path) | ("argv", [...]) | None: the helper itself
        self._look_at = 0.0             # (monotonic) the target looked at again then
        self._fails = 0                 # dead, in a row
        self._slow = 0                  # waits without an answer, in a row
        self.unhealthy = False          # the worker answers nothing: NORMAL / LOW wait
        self._inflight: tuple | None = None     # (prio, job): the helper's one clip
        self._worker_used = 0.0         # (monotonic) its last answer
        self._state = ""                # the worker's, for the log: said once each time it changes
        self._state_msg = ""            # ... and what was said
        self._quick_until = time.monotonic() + QUICK_FOR_S
        self._said = ""

    def _queued(self) -> int:
        return sum(len(q) for q in self._q.values())

    def _drop(self, why: str, prio: int) -> None:
        self.dropped[why] += 1
        self.dropped_pre += int(prio >= PRE and why == "queue")      # (pressure: never a too long / failed one)
        self.dropped_fire += int(prio == FIRE and why == "queue")

    def _room(self, prio: int, secs: float) -> bool:
        """(under the lock) Room for one more of this priority: lower-priority ones queued give it their place, the
        oldest of the lowest first, but only when that makes room (nothing is dropped for nothing)."""
        n, s = self._queued(), self._queued_s
        if n < self.max_items and s + secs <= self.max_s:
            return True
        lower = [(p, x) for p in sorted(self._q) if p < prio for x in self._q[p]]
        k = 0
        while k < len(lower) and not (n - k < self.max_items and s + secs <= self.max_s):
            s -= lower[k][1][2]
            k += 1
        if not (n - k < self.max_items and s + secs <= self.max_s):
            return False
        for p, _x in lower[:k]:
            _desc, _audio, gone = self._q[p].popleft()
            self._queued_s -= gone
            self._drop("queue", p)
        return True

    def submit(self, item, audio, prio: int = NORMAL) -> bool:
        """Queue one transmission / page (never blocks, never encodes). False: dropped (counted)."""
        if audio is None or not len(audio):
            return False
        prio = prio if prio in self._q else NORMAL
        secs = len(audio) / RATE
        if len(audio) > MAX_SECONDS * RATE:
            with self._cond:
                self._drop("too_big", prio)
            return False
        with self._cond:
            if self._stop:
                return False
            if not self._room(prio, secs):
                self._drop("queue", prio)
                return False
            # what it is, taken now (the item goes on changing on other threads); getattr(item, "fm", True) is
            # Transcriber._run's very expression for de-emphasis
            what = (describe(item), getattr(item, "fm", True), getattr(item, "voice_offset_s", 0.0))
            self._q[prio].append((what, audio, secs))
            self._queued_s += secs
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="hub-stt-input", daemon=True)
                self._bg = threading.Thread(target=self._run_bg, name="hub-stt-input-bg", daemon=True)
                self._thread.start()
                self._bg.start()
            self._cond.notify_all()                             # (a wait of either thread: wakes now)
        return True

    def _held(self) -> bool:
        """NORMAL / LOW wait now: whisper behind on live traffic, this hold under HOLD_MAX_S (any error: not held)."""
        try:
            behind = bool(self.held is not None and self.held())
        except Exception:  # noqa: BLE001 - never stops the lane
            behind = False
        if not behind:
            self._hold_since = None
            return False
        now = time.monotonic()
        if self._hold_since is None:
            self._hold_since = now
        return now - self._hold_since < HOLD_MAX_S         # (capped: on until whisper has caught up, then anew)

    def _deliver(self, prio: int, desc: dict, got) -> None:
        if got is None:
            with self._cond:
                self._drop("too_big", prio)
            return
        try:
            self.sink(desc, *got, prio=prio)
            self.made += 1
        except Exception as e:  # noqa: BLE001 - one bad clip never stops the next
            with self._cond:
                self._drop("failed", prio)
            self._failed_once(e, "kept")

    def _failed_once(self, e: Exception, what: str = "made") -> None:
        if self._said != type(e).__name__:
            self._said = type(e).__name__
            self.log(f"hub link: speech-to-text input not {what}: {type(e).__name__}: {e}")

    def _run(self) -> None:
        """PRE / FIRE: made here at once. NORMAL / LOW: handed to the helper, one at a time, when not held."""
        _lower_priority()
        while True:
            with self._cond:
                self._cond.wait_for(lambda: self._stop or bool(self._queued()))
                if self._stop:
                    return
                low = bool(self._q[NORMAL] or self._q[LOW]) and self._inflight is None and not self.unhealthy
            held = low and self._held()
            t0 = time.monotonic()
            with self._cond:
                if self._stop:
                    return
                top = next((p for p in (PRE, FIRE) if self._q[p]), None)
                if top is not None:
                    job = self._q[top].popleft()
                    self._queued_s -= job[2]
                elif low and not held and self._inflight is None and not self.unhealthy:
                    p = NORMAL if self._q[NORMAL] else LOW
                    job = self._q[p].popleft()
                    self._queued_s -= job[2]
                    self._inflight = (p, job)                   # (the helper's)
                    self.holding = False
                    self._cond.notify_all()
                    continue
                else:                                           # nothing it may take now: a submit, the helper
                    self.holding = held                         # done, or the hold's next look wakes it
                    self._cond.wait(HELD_LOOK_S)
                    if held:
                        self.held_s += time.monotonic() - t0
                    continue
            (desc, fm, offset), audio, _secs = job
            try:
                got = _blob(desc, fm, offset, audio)            # (PRE / FIRE: always here, never the worker)
            except Exception as e:  # noqa: BLE001 - one bad clip never stops the next (the hub uses its clip)
                with self._cond:
                    self._drop("failed", top)
                self._failed_once(e)
                continue
            self._deliver(top, desc, got)

    # -- the helper: NORMAL / LOW, one in flight ----------------------------------------------------------------------

    def _say(self, state: str, msg: str) -> None:
        if state != self._state:
            self._state, self._state_msg = state, msg
            self.log(f"hub link: speech-to-text input encoder: {msg}")

    def worker(self) -> dict:
        """The station's status (Health): where NORMAL / LOW are made, and the worker's state: ok | slow | unhealthy
        | dead | thread ("" before the first), with what was said of it."""
        if self.where.startswith("thread"):
            state = "thread"
        else:
            state = self._state if self._state in ("ok", "slow", "unhealthy", "dead") else ""
        return {"where": self.where, "state": state, "detail": self._state_msg[:300], "unhealthy": self.unhealthy,
                "in_flight": self._inflight is not None}

    def _retry_in(self) -> float:
        return QUICK_LOOK_S if time.monotonic() < self._quick_until else WORKER_RETRY_S

    def _look(self) -> None:
        """Where NORMAL / LOW are made, for this mode (auto: the service when its socket is there)."""
        from sizeup.cloud import sttencode
        self._look_at = time.monotonic() + self._retry_in()
        if self.mode == "thread":
            self._target, self.where = None, "thread"
        elif self.mode == "process":
            self._target, self.where = ("argv", [sys.executable, *sttencode.WORKER_ARGS]), "process"
        else:
            path = os.environ.get(sttencode.SOCKET_ENV) or sttencode.SOCKET
            if os.path.exists(path):
                self._target, self.where = ("sock", path), "service"
            else:
                self._target, self.where = None, "thread (no sizeup-stt-encode service)"

    def _thread_for_now(self, why: str, quick: bool = False) -> None:
        self._close_worker()
        if quick:                                               # (it died: likely restarting, looked for again soon)
            self._quick_until = time.monotonic() + QUICK_FOR_S
        retry = self._retry_in()
        self._target, self._look_at, self._fails = None, time.monotonic() + retry, 0
        self.where = f"thread ({why})"
        self._say("thread", f"{why}: made on the station's helper thread, the worker looked for again in "
                            f"{retry:.0f} s")

    def _set_unhealthy(self, bad: bool, why: str = "") -> None:
        with self._cond:
            if bad == self.unhealthy:
                return
            self.unhealthy = bad
            self._cond.notify_all()
        self._say("unhealthy" if bad else "ok", f"the worker is unhealthy ({why}): NORMAL / LOW wait queued" if bad
                  else f"the worker ({self.where}) answers again")

    def _run_bg(self) -> None:
        _lower_priority()
        while True:
            with self._cond:
                self._cond.wait_for(lambda: self._stop or self._inflight is not None)
                if self._stop:
                    return
                prio, job = self._inflight
            (desc, fm, offset), audio, _secs = job
            try:
                got = self._make(desc, fm, offset, audio)
            except _Stopped:
                return
            except Exception as e:  # noqa: BLE001 - one bad clip never stops the next (the hub uses its clip)
                with self._cond:
                    self._drop("failed", prio)
                self._failed_once(e)
                got = _NOTHING
            if got is not _NOTHING:
                self._deliver(prio, desc, got)
            with self._cond:
                self._inflight = None
                self._cond.notify_all()

    def _make(self, desc: dict, fm: bool, offset: float, audio):
        """(the helper) _blob, in the worker when there is one (the same bytes: the same code on the same samples):
        sent once, then waited for on the same connection, however long (never sent again, never made here for
        being slow: the worker is marked unhealthy meanwhile)."""
        from sizeup.cloud import sttencode
        if not self.where or (self._target is None and self.mode != "thread" and time.monotonic() >= self._look_at):
            self._look()
        if self._target is not None:
            try:
                if self._worker is not None and time.monotonic() - self._worker_used > sttencode.SERVER_IDLE_S * 0.8:
                    self._close_worker()                        # (the service closes a silent one: a new one first)
                if self._worker is None:
                    self._worker = sttencode.Client(*self._target)
                self._worker.send(desc, fm, offset, audio)
                sent = time.monotonic()
                window = min(WAIT_CAP_S, sttencode.BASE_TIMEOUT_S + sttencode.PER_AUDIO_S * len(audio) / RATE)
                while True:
                    try:
                        got = self._worker.wait(window)
                        break
                    except sttencode.Slow:
                        if self._stop:
                            raise _Stopped() from None
                        self._slow += 1
                        if self._slow >= WORKER_SLOW or time.monotonic() - sent >= UNHEALTHY_S:
                            self._set_unhealthy(True, f"no answer for {time.monotonic() - sent:.0f} s")
                        else:
                            self._say("slow", f"the worker is slow (no answer in {window:.0f} s): still waiting "
                                              "for it, never sent again")
                self._worker_used = time.monotonic()
                self._fails = self._slow = 0
                self._set_unhealthy(False)
                self._say("ok", f"the worker ({self.where}) makes them")
                return got
            except sttencode.Mismatch as e:
                self._thread_for_now(str(e))
            except sttencode.Dead as e:
                self._close_worker()
                self._set_unhealthy(False)
                if self._stop:                                  # (stop() cut it short: nothing more is made)
                    raise _Stopped() from None
                self._fails += 1
                if self._fails >= WORKER_FAILS:
                    self._thread_for_now(f"the worker failed {WORKER_FAILS} times: {e}", quick=True)
                else:
                    self._say("dead", f"the worker is gone ({e}): this one on the station's helper thread")
        if self._stop:
            raise _Stopped()
        return _blob(desc, fm, offset, audio)                   # (no worker, or a dead one: here)

    def _close_worker(self) -> None:
        w, self._worker = self._worker, None
        if w is not None:
            w.close()

    def idle(self) -> bool:
        with self._cond:
            return not self._queued() and self._inflight is None

    def stop(self) -> None:
        with self._cond:
            self._stop = True
            for q in self._q.values():
                q.clear()
            self._queued_s = 0.0
            self._cond.notify_all()
        w = self._worker
        if w is not None:
            w.shutdown()                                        # (a wait on the worker returns now)
        for t in (self._thread, self._bg):
            if t is not None and t is not threading.current_thread():
                t.join(2.0)
        self._close_worker()

    def stats(self) -> dict:
        with self._cond:
            return {"queued": self._queued(), "made": self.made, "dropped": dict(self.dropped)}


class _Stopped(Exception):
    """stop() during a wait on the worker: nothing more is made."""


_NOTHING = object()


# -- the wire ---------------------------------------------------------------------------------------------------------

def to_wire(data: bytes, meta: dict) -> dict:
    return dict(meta, audio=base64.b64encode(data).decode())


def from_wire(stt) -> tuple[bytes, dict]:
    """An item's "stt" -> (FLAC, metadata), checked (see the module doc). ValueError when it isn't one to keep."""
    if not isinstance(stt, dict) or stt.get("format") != "flac" or stt.get("input") != INPUT_SRC:
        raise ValueError("stt: not a FLAC STT input")
    rate, samples = stt.get("rate"), stt.get("samples")
    if not isinstance(rate, int) or isinstance(rate, bool) or not 4000 <= rate <= 48000:
        raise ValueError("stt: rate")
    if not isinstance(samples, int) or isinstance(samples, bool) or not 0 < samples <= MAX_SECONDS * rate:
        raise ValueError("stt: samples")
    try:
        data = base64.b64decode(str(stt.get("audio") or ""), validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("stt: audio is not base64") from None
    if len(data) > MAX_BLOB:
        raise ValueError("stt: too big")
    try:
        frate, total, md5, _at = flac.info(data)
    except flac.FlacError as e:
        raise ValueError(f"stt: {e}") from None
    if frate != rate or total != samples or md5 == bytes(16):
        raise ValueError("stt: the FLAC's own header doesn't match (rate, samples, MD5)")
    meta = {"format": "flac", "input": INPUT_SRC, "rate": rate, "samples": samples}
    if stt.get("kind") in ("traffic", "page"):
        meta["kind"] = stt["kind"]
    off = stt.get("voice_offset_s")
    if isinstance(off, (int, float)) and not isinstance(off, bool) and 0 <= off < 3600:
        meta["voice_offset_s"] = float(off)
    return data, meta


# -- the hub ----------------------------------------------------------------------------------------------------------

def src_id(cid: str) -> str:
    return cid + STT_SRC


def store(audio_db, cid: str, data: bytes, meta: dict, at: float, channel: str = "", talkgroup=None) -> None:
    """The station's STT input for clip `cid`, beside the clip (the metadata in the row's note)."""
    audio_db.put(src_id(cid), data, "clip", note=json.dumps(meta, separators=(",", ":")), at=at, channel=channel,
                 talkgroup=talkgroup)


@dataclass(frozen=True)
class SttAudio:
    """A clip's audio for speech-to-text, on the hub.

    pcm             int16 mono samples
    rate            16000 (stt-src) | the listening clip's rate (8000: listening-8k)
    input           "stt-src": the station's whisper input itself | "listening-8k": the listening clip in its place
                    (stamp transcripts with it: one from listening-8k isn't comparable with the station's)
    voice_offset_s  a page: where pcm starts in the listening clip (the tones are before it, never in pcm); else 0
    flac            stt-src: the FLAC as the station sent it (Amazon Transcribe streaming takes media-encoding=flac)
    """

    pcm: np.ndarray
    rate: int
    input: str
    voice_offset_s: float = 0.0
    flac: bytes | None = None

    @property
    def seconds(self) -> float:
        return len(self.pcm) / self.rate if self.rate else 0.0

    def pcm8k(self) -> np.ndarray:
        """int16 at 8 kHz, what the AWS path takes (sizeup/transcribe.py pcm8k: every other sample of 16 kHz audio,
        which radio_voice has band-limited to 3.7 kHz)."""
        if self.rate == 16000:
            return np.ascontiguousarray(self.pcm[::2])
        if self.rate == 8000:
            return self.pcm
        raise ValueError(f"audio at {self.rate} Hz")

    def whisper_wav(self) -> bytes:
        """A 16 kHz WAV with 0.5 s of silence each side, as WhisperClient posts it to whisper-server: for stt-src the
        very bytes the station's whisper got. listening-8k is brought to 16 kHz by linear interpolation."""
        x = self.pcm
        if self.rate == 8000:
            f = x.astype(np.float32) / 32768.0
            if len(f) > 1:
                f = np.interp(np.arange(2 * len(f)) / 2.0, np.arange(len(f)), f).astype(np.float32)
            x = (np.clip(f, -1, 1) * 32767).astype(np.int16)
        elif self.rate != 16000:
            raise ValueError(f"audio at {self.rate} Hz")
        pad = np.zeros(WHISPER_PAD, np.int16)
        return wav_bytes(np.concatenate([pad, x, pad]), 16000)


def _wav(data: bytes) -> tuple[np.ndarray, int] | None:
    try:
        with wave.open(io.BytesIO(data)) as w:
            if w.getsampwidth() != 2 or w.getnchannels() != 1:
                return None
            return np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.int16), w.getframerate()
    except (wave.Error, EOFError):
        return None


def read(audio_db, cid: str, voice_offset_s: float = 0.0, log=None) -> SttAudio | None:
    """The audio a speech-to-text engine should take for clip `cid` (see SttAudio). voice_offset_s: a page's, to cut
    the tones off the listening clip when that is all there is (the STT input never has them)."""
    data = audio_db.get(src_id(cid))
    if data:
        try:
            pcm, rate = flac.decode(data, max_samples=MAX_SECONDS * 48000)
            meta = json.loads(audio_db.note(src_id(cid)) or "{}")
            if "voice_offset_s" in meta:
                off = float(meta["voice_offset_s"])
            elif not meta.get("kind"):                  # a row made again without its note (the orphan sweep)
                off = float(voice_offset_s or 0.0)
            else:
                off = 0.0                               # a transmission: it starts where its clip does
            return SttAudio(pcm, rate, INPUT_SRC, off, data)
        except (flac.FlacError, ValueError, TypeError, MemoryError) as e:
            if log is not None:
                log(f"stt input: {cid}: not readable ({type(e).__name__}: {str(e)[:80]}): the listening clip instead")
    wav = audio_db.get(cid)
    got = _wav(wav) if wav else None
    if got is None:
        return None
    pcm, rate = got
    off = max(0.0, float(voice_offset_s or 0.0))
    return SttAudio(pcm[int(off * rate):], rate, INPUT_LISTENING, off)

