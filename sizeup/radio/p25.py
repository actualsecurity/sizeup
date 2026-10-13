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

One call, many key-ups (issue #62). trunk-recorder keeps a talkgroup's back-to-back transmissions
in one call (until callTimeout, 3 s, of quiet). Its transmission sink ends a transmission on each
P25 terminator (the TDU at PTT release), so 5.2.0's call JSON lists every key-up, in order, as one
srcList entry and one freqList entry with the same "pos" (seconds into the call WAV: transmissions
are concatenated, gaps left out):

    "srcList":  [{"src": 600008, "time": 1790000000, "pos": 0.0, "emergency": 0,
                  "signal_system": "", "tag": "60 Ctrl OP 8", "tag_ota": ""}, ...]
    "freqList": [{"freq": 478187500, "time": 1790000000, "pos": 0.0, "len": 5.84,
                  "error_count": 3, "spike_count": 0}, ...]

"src" is -1 when no radio ID was decoded for that transmission ("time" is whole seconds). Each
entry is its own Turn: the same radio keying twice is two turns, and an entry with no ID is a turn
of an unknown talker, never folded into the previous one. Where the live grants (radio/live.py,
the unit script) name the radio that keyed at that moment, apply_grants() names the unknown turn,
and splits a transmission that the voice channel ran into the next one (no terminator decoded:
trunk-recorder then credits the whole transmission to the later radio).
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


UNKNOWN_SRC = ("", "-1", "0")     # srcList "src" when no radio ID was decoded


@dataclass
class Turn:
    """One key-up inside a call."""
    pos: float                    # s into the call audio where it starts
    talker: str                   # alias / tag / radio ID as shown; "" = unknown radio
    src: str = ""                 # P25 radio ID ("" = none decoded)
    tag: str = ""                 # trunk-recorder's alias for src
    length: float | None = None   # s of audio; None = until the next turn
    at: float | None = None       # wall-clock start (estimated: srcList "time" is whole seconds)
    errors: int = 0               # freqList error_count
    via: str = "srclist"          # srclist | grant (talker named by a live grant) | split (cut at a grant)


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
    turns: list[Turn] = field(default_factory=list)  # one per key-up, in order ((pos, talker) tuples are taken too)
    ids: dict[str, tuple[str, str]] = field(default_factory=dict)  # talker -> (radio ID, alias tag) as sent
    system: str = ""           # trunk-recorder's shortName ("westchester")

    def __post_init__(self) -> None:
        self.turns = [t if isinstance(t, Turn) else Turn(float(t[0]), str(t[1])) for t in self.turns]


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


def _num(v, default: float | None = None) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return x if x == x else default                          # NaN


def talker_name(src: str, tag: str, aliases: dict[str, str]) -> str:
    return aliases.get(src) or aliases.get(tag) or tag or src


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
    start = float(d.get("start_time", 0))
    stop = float(d.get("stop_time", d.get("start_time", 0)))
    if _num(d.get("start_time_ms")):
        start = float(d["start_time_ms"]) / 1000.0
    if _num(d.get("stop_time_ms")):
        stop = float(d["stop_time_ms"]) / 1000.0
    length = _num(d.get("call_length_ms"))
    length = length / 1000.0 if length is not None else _num(d.get("call_length"))
    turns = _turns(d, aliases)
    _place(turns, start if _num(d.get("start_time_ms")) else None,
           stop if _num(d.get("stop_time_ms")) else None, length)
    units: list[str] = []
    ids: dict[str, tuple[str, str]] = {}
    for t in turns:
        if not t.talker:
            continue
        ids.setdefault(t.talker, (t.src, t.tag))
        if not units or units[-1] != t.talker:
            units.append(t.talker)
    wav = meta_path.with_suffix(".wav")
    return Call(meta_path.stem, int(tg), labels.get(tg) or f"TG {tg}", start, stop, units, None, str(wav), turns,
                ids, str(d.get("short_name") or ""))


def _turns(d: dict, aliases: dict[str, str]) -> list[Turn]:
    """One Turn per srcList entry (= per transmission), in audio order. A freqList entry (5.x: one
    per transmission, with "len") at a position no srcList entry has is a key-up too, talker unknown."""
    freqs: dict[float, dict] = {}
    for f in d.get("freqList") or []:
        p = _num(f.get("pos")) if isinstance(f, dict) else None
        if p is not None and "len" in f:
            freqs.setdefault(round(max(0.0, p), 2), f)
    turns: list[Turn] = []
    for s in d.get("srcList") or []:
        if not isinstance(s, dict):
            continue
        pos = round(max(0.0, _num(s.get("pos"), 0.0)), 2)
        src = str(s.get("src", "")).strip()
        src = "" if src in UNKNOWN_SRC else src
        tag = str(s.get("tag") or "").strip() if src else ""
        f = freqs.get(pos) or {}
        turns.append(Turn(pos, talker_name(src, tag, aliases) if src else "", src, tag, _num(f.get("len")),
                          _num(s.get("time")), int(_num(f.get("error_count"), 0) or 0)))
    have = {t.pos for t in turns}
    for pos, f in freqs.items():
        if pos not in have:
            turns.append(Turn(pos, "", length=_num(f.get("len")), at=_num(f.get("time")),
                              errors=int(_num(f.get("error_count"), 0) or 0)))
    turns.sort(key=lambda t: t.pos)                          # stable: srcList order at equal positions
    return turns


def _place(turns: list[Turn], start: float | None, stop: float | None, length: float | None) -> None:
    """Wall-clock start of each turn, for matching live grants. srcList "time" is whole seconds; the
    audio holds the transmissions back to back, so a turn starts no sooner than the one before it
    plus its length, and no later than the one after it minus its own length. The call's
    start_time_ms / stop_time_ms pin the first start and the last end."""
    if not turns:
        return
    n = len(turns)
    lens = []
    for i, t in enumerate(turns):
        if i + 1 < n:
            lens.append(max(0.0, turns[i + 1].pos - t.pos))
        else:
            lens.append(t.length if t.length is not None else max(0.0, (length or 0.0) - t.pos))
    lo: list[float | None] = [None] * n
    hi: list[float | None] = [None] * n
    for i, t in enumerate(turns):
        cands = [x for x in (t.at, (lo[i - 1] + lens[i - 1]) if i and lo[i - 1] is not None else None,
                             start if i == 0 else None) if x is not None]
        lo[i] = max(cands) if cands else None
    for i in range(n - 1, -1, -1):
        t = turns[i]
        cands = [x for x in ((t.at + 1.0) if t.at is not None else None,
                             (hi[i + 1] - lens[i]) if i + 1 < n and hi[i + 1] is not None else None,
                             (stop - lens[i]) if i == n - 1 and stop is not None else None,
                             start if i == 0 and start is not None else None) if x is not None]
        hi[i] = min(cands) if cands else None
    for i, t in enumerate(turns):
        a, b = lo[i], hi[i]
        if a is None and b is None:
            t.at = (start + t.pos) if start is not None else None
        elif a is None or b is None:
            t.at = a if a is not None else b
        else:
            t.at = (a + b) / 2 if b >= a else a
        if t.length is None:
            t.length = lens[i] if (i + 1 < n or length is not None) else None


# Live grants against a call's turns (times in s). A grant comes before the voice it grants:
# GRANT_LEAD_S at most (plus the ~1 s a turn's start is known to); voice follows it by VOICE_LAG_S.
GRANT_LEAD_S = 3.0
GRANT_SLACK_S = 1.0
VOICE_LAG_S = 0.5
MIN_SPLIT_S = 0.3        # a grant cut leaves at least this much audio on each side


def apply_grants(call: Call, grants: list[tuple[float, str]], aliases: dict[str, str] | None = None,
                 alias_of: Callable[[str], str] = lambda src: "") -> bool:
    """Name and cut key-ups from the control channel's grants on this talkgroup ((time, radio ID),
    from radio/live.py). A grant opens the turn whose voice starts nearest VOICE_LAG_S after it
    (within GRANT_LEAD_S, and not a turn the voice channel credits to another radio); one inside a
    turn instead is a new talker the voice channel ran on into (no terminator decoded)
    when trunk-recorder credits that turn to the granted radio (it keeps the last ID it heard) or
    has none: the turn is cut there, the first part named by its own start grant (else unknown).
    A turn with an ID keeps it: the grant only names a turn with none. True if anything changed."""
    aliases = aliases or {}
    turns = [t for t in call.turns if t.at is not None]
    if not grants or not turns:
        return False
    grants = sorted((float(a), str(s)) for a, s in grants if str(s) not in UNKNOWN_SRC)
    starts: dict[int, list[tuple[float, str]]] = {}
    inside: dict[int, list[tuple[float, str]]] = {}
    for g in grants:
        at, src = g
        near = [i for i, t in enumerate(turns) if at - GRANT_SLACK_S <= t.at <= at + GRANT_LEAD_S]
        # the turn starting closest to when its voice would: not one the voice channel named someone else
        near = [i for i in near if turns[i].src in ("", src)] or near
        k = min(near, key=lambda i: abs(turns[i].at - (at + VOICE_LAG_S)), default=None)
        if k is not None:
            starts.setdefault(k, []).append(g)
            continue
        k = next((i for i, t in enumerate(turns)
                  if t.at + GRANT_SLACK_S < at and t.length is not None
                  and at + VOICE_LAG_S - t.at < t.length - MIN_SPLIT_S), None)
        if k is not None:
            inside.setdefault(k, []).append(g)

    def name(src: str) -> tuple[str, str]:
        tag = alias_of(src) or ""
        return talker_name(src, tag, aliases), tag

    changed = False
    out: list[Turn] = []
    for i, t in enumerate(turns):
        first = starts.get(i, [])
        opener = first[-1][1] if first else ""
        cuts = inside.get(i, [])
        if t.src:
            # trunk-recorder credits a run-on transmission to the last radio it heard: a grant inside
            # it names that radio only if it is this turn's; one for anyone else is left alone.
            last = max((k for k, (_a, src) in enumerate(cuts) if src == t.src), default=-1)
            cuts = cuts[:last + 1]
        if not cuts:
            if not t.src and opener:
                t.talker, t.tag = name(opener)
                t.src, t.via, changed = opener, "grant", True
            out.append(t)
            continue
        segs: list[tuple[float, str, str]] = [(t.pos, opener, "grant" if opener else "split")]   # (pos, src, via)
        for at, src in cuts:
            cut = round(t.pos + at + VOICE_LAG_S - t.at, 2)
            if src == segs[-1][1] or cut - segs[-1][0] < MIN_SPLIT_S:
                continue
            segs.append((cut, src, "split"))
        if len(segs) == 1:                                   # every cut was too close or no change
            if not t.src and opener:
                t.talker, t.tag = name(opener)
                t.src, t.via, changed = opener, "grant", True
            out.append(t)
            continue
        end = t.pos + (t.length or 0.0)
        for k, (pos, src, via) in enumerate(segs):
            nxt = segs[k + 1][0] if k + 1 < len(segs) else end
            talker, tag = name(src) if src else ("", "")
            if src and src == t.src and k == len(segs) - 1:
                talker, tag, via = t.talker, t.tag, "srclist"     # as the call JSON named it
            out.append(Turn(pos, talker, src, tag, round(nxt - pos, 2), t.at + (pos - t.pos),
                            t.errors if k == 0 else 0, via))
        changed = True
    if changed:
        call.turns = out + [t for t in call.turns if t.at is None]
        call.turns.sort(key=lambda t: t.pos)
        call.units = []
        for t in call.turns:
            if t.talker and (not call.units or call.units[-1] != t.talker):
                call.units.append(t.talker)
    return changed


# One clip per key-up (issue #62). A key-up is never folded into its neighbour for being short or for
# being the same radio again. The one exception: a piece shorter than SHORT_TURN_S with no radio ID
# and no audio energy (its loudest 20 ms under NO_VOICE_DBFS: a decode fragment, not a person) is
# merged into the piece before it (the first piece: the one after). A short piece with a radio ID,
# or with sound, keeps its own clip and row; when it has no voice it skips speech-to-text (shown as
# a key-up with nothing said) instead of asking whisper about 0.2 s of silence.
SHORT_TURN_S = 0.3
NO_VOICE_DBFS = -45.0
RATE = 16000


@dataclass
class Piece:
    """One key-up's clip: what becomes one radio traffic row."""
    id: str                    # the call's id, or "<call id>-t<k>" when the call has more than one
    start: float
    end: float
    talker: str                # "" = unknown radio
    audio: np.ndarray = field(repr=False)
    pairs: list[tuple[str, str]] = field(default_factory=list)   # (radio ID, alias) per talker
    stt: bool = True           # False: too short and silent to transcribe (a key-up, nothing said)
    via: str = "srclist"


def has_voice(audio: np.ndarray, rate: int = RATE) -> bool:
    """Any 20 ms of the clip above NO_VOICE_DBFS (decoded P25 silence is near digital zero)."""
    if audio is None or not len(audio):
        return False
    blk = max(1, rate // 50)
    n = max(1, len(audio) // blk)
    x = np.asarray(audio[: n * blk] if len(audio) >= blk else audio, dtype=np.float64).reshape(n, -1)
    rms = float(np.sqrt((x ** 2).mean(axis=1)).max())
    return 20 * np.log10(rms + 1e-12) > NO_VOICE_DBFS


def split_call(call: Call, rate: int = RATE) -> list[Piece]:
    """The call's audio cut at every key-up (Call.turns): one Piece each, see SHORT_TURN_S for the
    one merge. One piece keeps the call's own id; more are "<id>-t0", "-t1", ... in talk order."""
    audio = call.audio if call.audio is not None else np.zeros(0, np.float32)
    n = len(audio)

    def pairs_of(t: Turn | None, names: list[str]) -> list[tuple[str, str]]:
        if t is not None and t.src:
            return [(t.src, t.tag or t.talker)]
        return [(call.ids.get(x, ("", ""))[0], call.ids.get(x, ("", ""))[1] or x) for x in names if x]

    turns = sorted(call.turns, key=lambda t: t.pos)
    if not turns or (len(turns) > 1 and all(t.pos <= 0 for t in turns)):
        # No positions (an old JSON shape): the talkers can't be placed in the audio.
        names = list(call.units)
        return [Piece(call.id, call.start, call.end, ", ".join(names), audio, pairs_of(None, names))]
    cuts = [(min(n, max(0, int(round(t.pos * rate)))), t) for t in turns]
    raw: list[list] = []                                     # [a, b, turn]
    for i, (a, t) in enumerate(cuts):
        b = cuts[i + 1][0] if i + 1 < len(cuts) else n
        if b > a:                                            # an empty one has no audio to keep
            raw.append([a, b, t])
    if not raw:
        t = turns[-1]
        return [Piece(call.id, call.start, call.end, t.talker, audio, pairs_of(t, [t.talker]), via=t.via)]
    raw[0][0] = 0
    short = SHORT_TURN_S * rate
    kept: list[list] = []
    carry = None                                             # a leading fragment, for the next piece
    for a, b, t in raw:
        if len(raw) > 1 and b - a < short and not t.src and not t.talker and not has_voice(audio[a:b], rate):
            if kept:
                kept[-1][1] = b
            else:
                carry = a if carry is None else carry
            continue
        if carry is not None:
            a, carry = carry, None
        kept.append([a, b, t])
    out: list[Piece] = []
    for k, (a, b, t) in enumerate(kept):
        cid = call.id if len(kept) == 1 else f"{call.id}-t{k}"
        start = t.at if t.at is not None else call.start + a / rate
        if out and start < out[-1].start:
            start = out[-1].start
        clip = audio[a:b]
        stt = not ((b - a) < short and not has_voice(clip, rate))
        out.append(Piece(cid, start, start + (b - a) / rate, t.talker, clip, pairs_of(t, [t.talker]), stt, t.via))
    return out


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
        # Live grants (radio/live.py LiveTalk.grants): (system, talkgroup, t0, t1) -> [(time, radio ID)],
        # and the alias trunk-recorder logged for a radio ID. Set by the app; without them the call
        # JSON alone says who talked.
        self.grants: Callable[[str, int, float, float], list[tuple[float, str]]] | None = None
        self.alias_of: Callable[[str], str] = lambda src: ""

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
            if self.grants is not None and call.turns:
                try:
                    got = self.grants(call.system, call.talkgroup, call.start - GRANT_LEAD_S - GRANT_SLACK_S,
                                      call.end + GRANT_SLACK_S)
                    if apply_grants(call, got, self.aliases, self.alias_of):
                        self.on_event(f"p25: {call.label} {call.id}: key-ups from live grants: " + ", ".join(
                            f"{t.pos:.1f}s {t.talker or 'unknown'}" for t in call.turns if t.via != "srclist"))
                except Exception as e:                       # the call JSON alone is still the record
                    self.on_event(f"p25: grants not applied to {meta.name}: {type(e).__name__} {e}")
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
