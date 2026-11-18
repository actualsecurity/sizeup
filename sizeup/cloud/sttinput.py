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
    StationLink._stt_forward  POST /api/station/stt, gzipped, only while no event waits in the outbox and only to a
                              hub that says it takes them ("stt_input" in its caps)

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
import threading
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
QUEUE_S = 600.0                 # ... and this much audio (~38 MB of float32): over either, dropped and counted
NICE = 10                       # the encoder thread's niceness (Linux: per thread), under the radio threads
WHISPER_PAD = 8000              # WhisperClient's silence each side (0.5 s at 16 kHz)
DROP_REASONS = ("queue", "too_big", "failed", "outbox", "refused")


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


def _lower_priority() -> None:
    try:
        os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), NICE)    # Linux: this thread only
    except (AttributeError, OSError):
        pass                                                                # (macOS: per process only; left as is)


class Encoder:
    """The station's STT inputs, made off the radio path: submit() only queues a reference to the audio; one
    low-priority thread makes each FLAC and hands it to sink(data, flac, meta) (the outbox's lane)."""

    def __init__(self, sink: Callable[[dict, bytes, dict], None], log: Callable[[str], None] = print,
                 max_items: int = QUEUE_ITEMS, max_s: float = QUEUE_S):
        self.sink, self.log, self.max_items, self.max_s = sink, log, max_items, max_s
        self._cond = threading.Condition()
        self._q: deque = deque()
        self._queued_s = 0.0
        self._thread: threading.Thread | None = None
        self._stop = False
        self.made = 0
        self.dropped = {"queue": 0, "too_big": 0, "failed": 0}
        self._said = ""

    def submit(self, item, audio) -> bool:
        """Queue one transmission / page (never blocks, never encodes). False: dropped (counted)."""
        if audio is None or not len(audio):
            return False
        secs = len(audio) / RATE
        if len(audio) > MAX_SECONDS * RATE:
            with self._cond:
                self.dropped["too_big"] += 1
            return False
        with self._cond:
            if self._stop:
                return False
            if len(self._q) >= self.max_items or self._queued_s + secs > self.max_s:
                self.dropped["queue"] += 1
                return False
            # what it is, taken now (the item goes on changing on other threads); getattr(item, "fm", True) is
            # Transcriber._run's very expression for de-emphasis
            self._q.append(((describe(item), getattr(item, "fm", True), getattr(item, "voice_offset_s", 0.0)), audio, secs))
            self._queued_s += secs
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="hub-stt-input", daemon=True)
                self._thread.start()
            self._cond.notify()
        return True

    def _run(self) -> None:
        _lower_priority()
        while True:
            with self._cond:
                self._cond.wait_for(lambda: self._stop or bool(self._q))
                if self._stop:
                    return
                (desc, fm, offset), audio, secs = self._q.popleft()
                self._queued_s -= secs
            try:
                got = _blob(desc, fm, offset, audio)
                if got is None:
                    with self._cond:
                        self.dropped["too_big"] += 1
                    continue
                self.sink(desc, *got)
                self.made += 1
            except Exception as e:  # noqa: BLE001 - one bad clip never stops the next (the hub uses its clip)
                with self._cond:
                    self.dropped["failed"] += 1
                if self._said != type(e).__name__:
                    self._said = type(e).__name__
                    self.log(f"hub link: speech-to-text input not made: {type(e).__name__}: {e}")

    def idle(self) -> bool:
        with self._cond:
            return not self._q

    def stop(self) -> None:
        with self._cond:
            self._stop = True
            self._q.clear()
            self._queued_s = 0.0
            self._cond.notify_all()

    def stats(self) -> dict:
        with self._cond:
            return {"queued": len(self._q), "made": self.made, "dropped": dict(self.dropped)}


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
