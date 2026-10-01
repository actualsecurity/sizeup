"""P25 talkgroup calls from trunk-recorder (Fire/EMS 15 = TG 307, 60 Control = TG 301).

trunk-recorder follows the Westchester County P25 Phase II system and writes one audio file
plus a JSON sidecar per call:

    <capture_dir>/<shortName>/<YYYY>/<M>/<D>/<tg>-<start>_<freq>-call_<n>.wav
    ... .json  {"talkgroup": 307, "start_time": ..., "stop_time": ..., "freq": ...,
                "srcList": [{"src": 2391001, "time": ..., "pos": 0.0, "tag": "..."}], ...}

CallIngest polls the directory for new JSON sidecars (stdlib only, no inotify needed), keeps
the talkgroups we care about, and hands each call (audio at 16 kHz + who talked) on.
Decoding P25 stays outside sizeup, so trunk-recorder can be swapped for SDRTrunk/OP25 if the
simulcast decodes poorly with RTL-SDRs.
"""

from __future__ import annotations

import json
import threading
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np


@dataclass
class Call:
    id: str
    talkgroup: int
    label: str                 # "Fire/EMS 15"
    start: float
    end: float
    units: list[str]           # radio IDs (or tags/aliases) in talk order
    audio: np.ndarray | None = field(default=None, repr=False)  # float32 16 kHz
    wav_path: str = ""


WAV_HEADER = 44            # bytes; a WAV this size or smaller has no audio yet
SETTLE_GIVE_UP_S = 60.0


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:                                          # removed between listing and sorting
        return 0.0


def read_wav_16k(path: Path) -> np.ndarray:
    with wave.open(str(path)) as w:
        rate, n, ch, width = w.getframerate(), w.getnframes(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(n)
    if width != 2:
        raise ValueError(f"{path.name}: {width * 8}-bit audio not supported")
    x = np.frombuffer(raw, "<i2").astype(np.float32) / 32768.0
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    if rate != 16000:
        x = np.interp(np.linspace(0, len(x) - 1, int(len(x) * 16000 / rate)), np.arange(len(x)), x)
    return x.astype(np.float32)


def parse_call(meta_path: Path, labels: dict[int, str], aliases: dict[str, str] | None = None,
               record_all: bool = False) -> Call | None:
    try:
        d = json.loads(meta_path.read_text())
    except (OSError, ValueError):
        return None
    tg = d.get("talkgroup")
    if not isinstance(tg, int) or (tg not in labels and not record_all):
        return None
    aliases = aliases or {}
    units = []
    for s in d.get("srcList") or []:
        src = str(s.get("src", ""))
        if src in ("", "-1", "0"):
            continue
        name = aliases.get(src) or s.get("tag") or src
        if not units or units[-1] != name:
            units.append(name)
    wav = meta_path.with_suffix(".wav")
    return Call(meta_path.stem, int(tg), labels.get(tg) or f"TG {tg}", float(d.get("start_time", 0)),
                float(d.get("stop_time", d.get("start_time", 0))), units, None, str(wav))


class CallIngest:
    """Poll a trunk-recorder capture dir; on_call(Call) for each new call on our talkgroups."""

    def __init__(self, capture_dir: str | Path, labels: dict[int, str], on_call: Callable[[Call], None],
                 aliases: dict[str, str] | None = None, poll_s: float = 1.0,
                 on_event: Callable[[str], None] = lambda m: None, name: str = "P25"):
        self.name = name  # status label (the header pill), not every talkgroup's name
        self.record_all = True  # every talkgroup trunk-recorder writes (sizeup decides what to keep)
        self.dir, self.labels, self.on_call = Path(capture_dir), labels, on_call
        self.aliases, self.poll_s, self.on_event = aliases or {}, poll_s, on_event
        self._seen: set[str] = set()
        self._pending: dict[str, tuple[float, int]] = {}   # json path -> (first seen, last WAV size)
        self._stop = threading.Event()
        self.state, self.detail, self.last_call = "starting", "", 0.0

    def prime(self) -> CallIngest:
        """Mark calls already on disk as seen: don't replay old calls as if they were live."""
        self._seen = {str(p) for p in self.dir.rglob("*.json")} if self.dir.exists() else set()
        return self

    def start(self, skip_existing: bool = True) -> CallIngest:
        if skip_existing:
            self.prime()
        threading.Thread(target=self._run, name="p25-ingest", daemon=True).start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict:
        return {"label": self.name, "mhz": None, "state": self.state,
                "detail": self.detail, "open": False, "quieting_db": None, "restarts": 0,
                "last_heard": self.last_call or None}

    def scan(self) -> list[Call]:
        """New finished calls. trunk-recorder can publish the JSON before the WAV is fully written
        (we've read 0-byte WAVs that were 190 KB a moment later), so a call is taken only once its
        WAV is non-empty and the same size on two polls in a row; give up after SETTLE_GIVE_UP_S."""
        calls = []
        now = time.time()
        for meta in sorted(self.dir.rglob("*.json"), key=_mtime):
            key = str(meta)
            if key in self._seen:
                continue
            wav = meta.with_suffix(".wav")
            try:
                size = wav.stat().st_size
            except OSError:
                continue  # audio not there yet: pick it up next poll
            first, last_size = self._pending.get(key, (now, -1))
            settled = size > WAV_HEADER and size == last_size
            if not settled and now - first < SETTLE_GIVE_UP_S:
                self._pending[key] = (first, size)
                continue
            self._pending.pop(key, None)
            self._seen.add(key)
            call = parse_call(meta, self.labels, self.aliases, record_all=self.record_all)
            if call is None:
                continue
            try:
                call.audio = read_wav_16k(wav)
            except (OSError, ValueError, EOFError, wave.Error) as e:   # EOFError: empty/truncated WAV
                self.on_event(f"p25: skipped {wav.name}: {type(e).__name__} {e}")
                continue
            calls.append(call)
        return calls

    def _run(self) -> None:
        while not self._stop.is_set():
            if not self.dir.exists():
                self.state, self.detail = "absent", f"no trunk-recorder output at {self.dir}"
            else:
                self.state = "ok"
                self.detail = f"watching {self.dir.name}" + (
                    f" · last call {time.strftime('%H:%M', time.localtime(self.last_call))}" if self.last_call else "")
                try:
                    calls = self.scan()
                except Exception as e:                       # never let one bad file stop ingest
                    self.on_event(f"p25 scan error: {type(e).__name__}: {e}")
                    calls = []
                for call in calls:
                    self.last_call = call.end or time.time()
                    try:
                        self.on_call(call)
                    except Exception as e:
                        self.on_event(f"p25 call handler error: {type(e).__name__}: {e}")
            self._stop.wait(self.poll_s)
