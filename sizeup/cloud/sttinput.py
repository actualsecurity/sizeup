"""The speech-to-text input, station -> hub: every engine transcribes the same audio.

The station sends each transmission and page with its listening clip (8 kHz WAV, the voice band, de-emphasised and
de-whined for people: sizeup/cloud/station.py clip_8k). Its own whisper transcribes something else: the audio before
the listening filter, through radio_voice(deemph=<FM>, nr=False) at 16 kHz (sizeup/stt.py Transcriber._run). So a hub
engine (Amazon Transcribe, a GPU worker) that took the listening clip heard other audio than the station's whisper,
and their transcripts couldn't be compared. Now the station also sends that exact input, losslessly.

Station (StationLink):
    stt_pcm(audio, fm)        int16, 16 kHz, mono: exactly the samples the station's whisper transcribes (the same
                              radio_voice call as Transcriber._run, quantised as stt.wav_bytes does), without the 0.5 s
                              of silence WhisperClient puts on each side. FM de-emphasis is already applied (FM
                              channels and pages); P25 and the airband never get it, as in whisper.
    Stash                     App._stt (traffic) / App._page_done (pages) put it here as FLAC (sizeup/cloud/flac.py)
                              the moment the item goes to speech-to-text; StationLink.emit takes it off with the
                              "traffic" / "page" event that carries the clip. Bounded (STASH_BYTES, the oldest out,
                              counted): a whisper backlog never grows it without limit.

Wire (POST /api/station/events), beside "audio" (the listening clip, unchanged):
    "stt": {"v": 1, "format": "flac", "input": "stt-src", "rate": 16000, "samples": <n>,
            "voice_offset_s": <s>,            pages only: where this audio starts in the listening clip (after the tones)
            "audio": "<base64 FLAC>"}
    "stt_dropped": true                       the outbox dropped it to stay under its limit (sizeup/cloud/outbox.py)
An older hub ignores both keys; an older station sends neither.

Hub (HubApp):
    store(audio_db, cid, ...)  the FLAC as sent, in audio.sqlite / the store as clip "<id>.src" (key
                               audio/clip/<yyyy>/<mm>/<dd>/<id>.src.flac), with the clip's channel, talkgroup and time,
                               so it is uploaded, cached and pruned with the clip (#414, #428). Never served by
                               /api/audio (HubApp.clip refuses the id).
    read(audio_db, cid, voice_offset_s) -> SttAudio | None
                               the STT input when the station sent it (input "stt-src"), else the listening clip from
                               voice_offset_s on (input "listening-8k": an older station, or the outbox dropped it),
                               None when there is neither. HubApp.read_stt_audio(cid) finds a page's voice_offset_s.
"""

from __future__ import annotations

import base64
import binascii
import io
import json
import threading
import wave
from collections import OrderedDict
from dataclasses import dataclass

import numpy as np

from sizeup.audiostore import STT_SRC
from sizeup.cloud import flac
from sizeup.stt import radio_voice, wav_bytes

RATE = 16000                    # what the station's whisper gets
INPUT_SRC = "stt-src"           # the station's speech-to-text input itself
INPUT_LISTENING = "listening-8k"  # the 8 kHz listening clip, in its place (a transcript from it: not comparable)
STT_KINDS = ("traffic", "page")   # the events whose clip goes with its STT input
STASH_BYTES = 48 * 1024 * 1024    # FLAC waiting on the station for its event (~13 h of P25 traffic at the county's rate)
MAX_BLOB = 8 * 1024 * 1024        # one STT input at most (a 5-minute transmission is ~6 MB)
WHISPER_PAD = 8000                # WhisperClient's silence each side (0.5 s at 16 kHz)


def stt_pcm(audio: np.ndarray, fm: bool) -> np.ndarray:
    """The samples the station's whisper transcribes for this audio (sizeup/stt.py Transcriber._run, then the int16
    of its WAV), at 16 kHz."""
    x = radio_voice(audio, deemph=fm, nr=False)
    return (np.clip(x, -1, 1) * 32767).astype(np.int16)


def blob(item, audio: np.ndarray) -> tuple[bytes, dict] | None:
    """(FLAC, metadata) for one transmission or page about to go to speech-to-text (audio: what App._stt gets)."""
    if audio is None or not len(audio):
        return None
    pcm = stt_pcm(audio, getattr(item, "fm", True))         # (Transcriber._run's very expression for de-emphasis)
    data = flac.encode(pcm, RATE)
    if len(data) > MAX_BLOB:
        return None
    meta = {"v": 1, "format": "flac", "input": INPUT_SRC, "rate": RATE, "samples": int(len(pcm))}
    from sizeup.radio.page import Page
    if isinstance(item, Page):                              # a page: its voice, after the tones
        meta["voice_offset_s"] = round(float(item.voice_offset_s), 4)
    return data, meta


class Stash:
    """The STT inputs made, waiting for the event that carries their clip (by clip id). Bounded by bytes."""

    def __init__(self, max_bytes: int = STASH_BYTES):
        self.max_bytes = max_bytes
        self._mu = threading.Lock()
        self._items: OrderedDict[str, tuple[bytes, dict]] = OrderedDict()
        self._bytes = 0
        self.evicted = 0                # made but pushed out before their event came (that event goes without it)
        self.failed = 0                 # couldn't be made (an exception: counted, never in the radio path's way)

    def put(self, cid: str, got: tuple[bytes, dict]) -> None:
        with self._mu:
            old = self._items.pop(cid, None)
            self._bytes -= len(old[0]) if old else 0
            self._items[cid] = got
            self._bytes += len(got[0])
            while self._bytes > self.max_bytes and len(self._items) > 1:
                _cid, (b, _m) = self._items.popitem(last=False)
                self._bytes -= len(b)
                self.evicted += 1

    def has(self, cid: str) -> bool:
        with self._mu:
            return cid in self._items

    def take(self, cid: str) -> tuple[bytes, dict] | None:
        with self._mu:
            got = self._items.pop(cid, None)
            self._bytes -= len(got[0]) if got else 0
            return got

    def stats(self) -> dict:
        with self._mu:
            return {"waiting": len(self._items), "bytes": self._bytes, "evicted": self.evicted, "failed": self.failed}


# -- the wire ---------------------------------------------------------------------------------------------------------

def to_wire(data: bytes, meta: dict) -> dict:
    return dict(meta, audio=base64.b64encode(data).decode())


def from_wire(stt) -> tuple[bytes, dict]:
    """An event's "stt" -> (FLAC, metadata). ValueError when it isn't one this hub knows."""
    if not isinstance(stt, dict) or stt.get("format") != "flac" or stt.get("input") != INPUT_SRC:
        raise ValueError("stt: not a FLAC STT input")
    rate = stt.get("rate")
    if not isinstance(rate, int) or isinstance(rate, bool) or not 4000 <= rate <= 48000:
        raise ValueError("stt: rate")
    try:
        data = base64.b64decode(str(stt.get("audio") or ""), validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("stt: audio is not base64") from None
    if not data.startswith(b"fLaC") or len(data) > MAX_BLOB:
        raise ValueError("stt: not a FLAC stream")
    meta = {"format": "flac", "input": INPUT_SRC, "rate": rate}
    if isinstance(stt.get("samples"), int):
        meta["samples"] = int(stt["samples"])
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
            pcm, rate = flac.decode(data)
            meta = json.loads(audio_db.note(src_id(cid)) or "{}")
            return SttAudio(pcm, rate, INPUT_SRC, float(meta.get("voice_offset_s") or voice_offset_s or 0.0), data)
        except (flac.FlacError, ValueError, TypeError) as e:
            if log is not None:
                log(f"stt input: {cid}: not readable ({type(e).__name__}: {str(e)[:80]}): the listening clip instead")
    wav = audio_db.get(cid)
    got = _wav(wav) if wav else None
    if got is None:
        return None
    pcm, rate = got
    off = max(0.0, float(voice_offset_s or 0.0))
    return SttAudio(pcm[int(off * rate):], rate, INPUT_LISTENING, off)
