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

Checked against 5.2.0's source (call_concluder.cc create_call_data / create_call_json, gr_blocks/transmission_sink.cc):
"pos" is the sum of the earlier transmissions' lengths, each from its own sample count, so it is where that transmission
starts in the call WAV (ffmpeg concatenates the transmissions' WAVs; the loudnorm 5.2.0 applies by default changes the
level, never the length, though a short call comes out at 192 kHz: read_wav_16k resamples). sizeup cuts the WAV at
"pos" as it is: nothing is trimmed or re-encoded first.

A transmission ends on a terminator only: Phase 1's TDU, or on Phase 2 (TDMA) MAC_HANGTIME (5.2.0 leaves MAC_END_PTT
alone). When the terminator isn't decoded and the next radio keys up on the same voice channel, stock 5.2.0 keeps
writing the same transmission and the new radio ID only replaces the old one: one srcList entry, credited to the later
radio. The station's build is patched (#638, deploy/station/install-linux.sh tr_patch): with a system's config.json
"sizeup": {"split": "on"} it ends the transmission where the new ID came in, so each talker is its own srcList entry at
its own "pos" ("log", the default for now, only logs where it would cut). Until then, or with a bench's own
trunk-recorder, apply_grants() below still cuts such a transmission where a live grant names the next radio.

"src" is -1 when no radio ID was decoded for that transmission ("time" is whole seconds). Each
entry is its own Turn: the same radio keying twice is two turns, and an entry with no ID is a turn
of an unknown talker, never folded into the previous one. Where the live grants (radio/live.py,
the unit script) name the radio that keyed at that moment, apply_grants() names the unknown turn,
and splits a transmission that the voice channel ran into the next one (no terminator decoded:
trunk-recorder then credits the whole transmission to the later radio).

The P25 emergency button (issue #128): the call JSON's top-level "emergency" (1 when the control channel
granted the call as an emergency: trunk-recorder 5.2.0 takes it from the grant that set the call up, so it
is the call's first key-up) and each srcList entry's "emergency" (always 0 in 5.2.0, which writes false
there; read anyway, for a later trunk-recorder that fills it). Call.emergency / Turn.emergency carry them;
split_call() marks the piece the button belongs to (Piece.emergency). The unit script gets no emergency
flag in 5.2.0 (plugins/unit_script passes only the system, radio ID, talkgroup and patches), so the
button is known when the call's JSON is written, not at the key-up.

Calls written while sizeup was down (#747). trunk-recorder keeps recording through a restart of sizeup (the station
service restarts for every auto-update, about a dozen times a day), so a call can conclude while nothing reads the
capture dir. CallIngest keeps a high-water mark per capture dir (mark_path, beside the station's state): the newest
sidecar it handed on, (mtime, path), and the few at or before it still waiting for their WAV. At start a sidecar newer
than the mark (or one of those waiting) and no older than REPLAY_S is ingested late (Call.late: the radio log, speech-
to-text and the hub take it; nothing that alerts does, sizeup/app.py); anything older is primed as seen, as before.
With no mark (the first start, a new capture dir) or one that can't be read, every sidecar on disk is primed, as
before. The mark moves only once on_call has taken a call, and is written (tmp + rename, no fsync) at most every
MARK_SAVE_S and at stop. Taken calls' files are removed by the app (App._drop_recorder_files), so a crash between two
writes can at most hand on a call the app has already taken: the app drops such a late call by its id.
"""

from __future__ import annotations

import json
import os
import threading
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from sizeup import tz


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
    emergency: bool = False       # srcList "emergency": this key-up was an emergency (P25 emergency button)


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
    emergency: bool = False    # the call JSON's "emergency": granted as an emergency call (the button, #128)
    encrypted: bool = False    # the call JSON's "encrypted" (#409: another system's such call is never kept)
    signal: float | None = None   # the call JSON's "signal" / "noise" (dB; 5.x), None when it has none (0 / 0)
    noise: float | None = None
    freq: float | None = None     # its voice channel, Hz
    late: bool = False         # written while sizeup was down, ingested at the next start (#747): never a live alert

    def __post_init__(self) -> None:
        self.turns = [t if isinstance(t, Turn) else Turn(float(t[0]), str(t[1])) for t in self.turns]


WAV_HEADER = 44            # bytes; a WAV this size or smaller has no audio yet
SETTLE_GIVE_UP_S = 60.0
# Calls written while sizeup was down (#747): ingested late at start back this far (older ones are primed as seen)
REPLAY_S = 600.0
MARK_SAVE_S = 5.0          # the high-water mark is written at most this often (and at stop)
MARK_WAITING_MAX = 200     # sidecars at or before the mark still waiting for their WAV, kept in the mark
STOP_JOIN_S = 3.0          # stop(): the poll thread finishes the call in hand, then the mark is written
LATE_REPORT_S = SETTLE_GIVE_UP_S + 5.0     # the start's "ingested late" line, at the latest this long after start


def _levels(d: dict) -> tuple[float | None, float | None]:
    """(signal, noise) of a call JSON in dB, (None, None) when trunk-recorder measured none (both 0, or absent)."""
    sig, noise = _num(d.get("signal")), _num(d.get("noise"))
    if sig is None or noise is None or (sig == 0 and noise == 0):
        return None, None
    return float(sig), float(noise)


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


def _flag(v) -> bool:
    """trunk-recorder's 0 / 1 (or true / false) flags."""
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes")
    return bool(v) if isinstance(v, (int, float, bool)) else False


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
                ids, str(d.get("short_name") or ""), _flag(d.get("emergency")), _flag(d.get("encrypted")), *_levels(d),
                _num(d.get("freq")))


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
                          _num(s.get("time")), int(_num(f.get("error_count"), 0) or 0),
                          emergency=_flag(s.get("emergency"))))
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
                            t.errors if k == 0 else 0, via, t.emergency and k == 0))
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
# A piece shorter than MIN_STT_S skips speech-to-text even with sound: no word fits in it. Measured on a copy of
# the hub's data (2026-11): 6.6 % of the P25 pieces sent to speech-to-text were under 0.25 s, and what came back
# was whisper's dead-air phrases ("You're welcome.", "It", the talker prompt echoed) or AWS's "." - never a word
# that belonged there. Each one still cost the station's whisper a full decode (#196, #300).
SHORT_TURN_S = 0.3
MIN_STT_S = 0.25
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
    emergency: bool = False    # the P25 emergency button: its srcList entry, or the call's flag on its first key-up


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
        return [Piece(call.id, call.start, call.end, ", ".join(names), audio, pairs_of(None, names),
                      emergency=call.emergency or any(t.emergency for t in turns))]
    cuts = [(min(n, max(0, int(round(t.pos * rate)))), t) for t in turns]
    raw: list[list] = []                                     # [a, b, turn]
    for i, (a, t) in enumerate(cuts):
        b = cuts[i + 1][0] if i + 1 < len(cuts) else n
        if b > a:                                            # an empty one has no audio to keep
            raw.append([a, b, t])
    if not raw:
        t = turns[-1]
        return [Piece(call.id, call.start, call.end, t.talker, audio, pairs_of(t, [t.talker]), via=t.via,
                      emergency=call.emergency or any(x.emergency for x in turns))]
    raw[0][0] = 0
    short = SHORT_TURN_S * rate
    kept: list[list] = []                                    # [a, b, turn, emergency]
    carry = None                                             # a leading fragment, for the next piece
    carry_em = False
    for a, b, t in raw:
        if len(raw) > 1 and b - a < short and not t.src and not t.talker and not has_voice(audio[a:b], rate):
            if kept:
                kept[-1][1] = b
                kept[-1][3] = kept[-1][3] or t.emergency
            else:
                carry = a if carry is None else carry
                carry_em = carry_em or t.emergency
            continue
        em = t.emergency
        if carry is not None:
            a, carry, em = carry, None, em or carry_em
        kept.append([a, b, t, em])
    if kept and call.emergency:      # 5.2.0: the call's flag, from the grant that opened it: its first key-up
        kept[0][3] = True
    out: list[Piece] = []
    for k, (a, b, t, em) in enumerate(kept):
        cid = call.id if len(kept) == 1 else f"{call.id}-t{k}"
        start = t.at if t.at is not None else call.start + a / rate
        if out and start < out[-1].start:
            start = out[-1].start
        clip = audio[a:b]
        stt = (b - a) >= MIN_STT_S * rate and not ((b - a) < short and not has_voice(clip, rate))
        out.append(Piece(cid, start, start + (b - a) / rate, t.talker, clip, pairs_of(t, [t.talker]), stt, t.via, em))
    return out


class CallIngest:
    """Poll a trunk-recorder capture dir; on_call(Call) for each new call on our talkgroups. mark_path: where the
    high-water mark is kept (#747, the module's doc: calls written while sizeup was down); None: none kept, every
    start primes what is on disk."""

    def __init__(self, capture_dir: str | Path, labels: dict[int, str], on_call: Callable[[Call], None],
                 aliases: dict[str, str] | None = None, poll_s: float = 1.0,
                 on_event: Callable[[str], None] = lambda m: None, name: str = "P25",
                 mark_path: str | Path | None = None):
        self.name = name  # status label (the header pill), not every talkgroup's name
        self.record_all = True  # every talkgroup trunk-recorder writes (sizeup decides what to keep)
        self.dir, self.labels, self.on_call = Path(capture_dir), labels, on_call
        self.aliases, self.poll_s, self.on_event = aliases or {}, poll_s, on_event
        self._seen: set[str] = set()
        self._pending: dict[str, tuple[float, int]] = {}   # json path -> (first seen, last WAV size)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.state, self.detail, self.last_call = "starting", "", 0.0
        # Live grants (radio/live.py LiveTalk.grants): (system, talkgroup, t0, t1) -> [(time, radio ID)],
        # and the alias trunk-recorder logged for a radio ID. Set by the app; without them the call
        # JSON alone says who talked.
        self.grants: Callable[[str, int, float, float], list[tuple[float, str]]] | None = None
        self.alias_of: Callable[[str], str] = lambda src: ""
        # The high-water mark (#747): (mtime, path under the capture dir) of the newest sidecar on_call took, and the
        # sidecars at or before it not taken yet (their WAV still coming) as the last scan found them
        self.mark_path = Path(mark_path) if mark_path else None
        self._mark: tuple[float, str] | None = None
        self._behind: list[tuple[float, str]] | None = None    # None: not scanned yet (the loaded mark's are kept)
        self._waiting: list[str] = []                           # the loaded mark's
        self._mark_lock = threading.Lock()
        self._saved: tuple | None = None                        # what was written last, and when
        self._saved_at = 0.0
        self._mark_err = False
        self._keys: dict[str, tuple[float, str]] = {}           # wav path of a call scan() returned -> its sidecar key
        # Calls written while sizeup was down, ingested late at this start, and the start's line about them
        self._late: set[str] = set()
        self._late_n = self._late_skipped = 0
        self._late_say_by: float | None = None

    def prime(self) -> CallIngest:
        """Mark calls already on disk as seen: don't replay old calls as if they were live."""
        self._seen = {str(p) for p in self.dir.rglob("*.json")} if self.dir.exists() else set()
        return self

    def resume(self) -> CallIngest:
        """At start, with a mark kept (#747): sidecars newer than the mark (or waiting for their WAV at it), written in
        the last REPLAY_S, are ingested late; every other one on disk is primed as seen. No mark, or one that can't be
        read: everything is primed (as prime()), and the mark starts at the newest sidecar on disk."""
        loaded, why = self._load_mark()
        listing = self._listing()
        if loaded is None:
            self._seen = {str(p) for _k, p in listing}
            with self._mark_lock:
                self._mark, self._behind, self._waiting = (listing[-1][0] if listing else (0.0, "")), None, []
            self._save_mark(force=True)
            self.on_event(f"p25 {self.name}: {why}: the {len(listing)} call(s) on disk not replayed")
            return self
        mark, waiting = loaded
        now = time.time()
        seen, late, skipped = set(), set(), 0
        for key, p in listing:
            if key <= mark and key[1] not in waiting:
                seen.add(str(p))                     # taken before the restart (or primed then)
            elif now - key[0] > REPLAY_S:
                seen.add(str(p))                     # from while it was down, but too old to be worth it now
                skipped += 1
            else:
                late.add(str(p))
        self._seen, self._late, self._late_n, self._late_skipped = seen, late, 0, skipped
        with self._mark_lock:
            self._mark, self._behind, self._waiting = mark, None, sorted(waiting)
        self._late_say_by = now + LATE_REPORT_S
        self._say_late()
        return self

    def start(self, skip_existing: bool = True) -> CallIngest:
        if skip_existing:
            if self.mark_path is not None:
                self.resume()
            else:
                self.prime()
        self._look()          # its state from the first look, not "starting" until the thread's first poll (#282)
        self._thread = threading.Thread(target=self._run, name="p25-ingest", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        """The poll thread finishes the call in hand (STOP_JOIN_S at most), then the mark is written."""
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(STOP_JOIN_S)
        self._save_mark(force=True)

    def status(self) -> dict:
        return {"label": self.name, "mhz": None, "state": self.state,
                "detail": self.detail, "open": False, "quieting_db": None, "restarts": 0,
                "last_heard": self.last_call or None}

    def _rel(self, p: Path) -> str:
        try:
            return p.relative_to(self.dir).as_posix()
        except ValueError:
            return str(p)

    def _listing(self) -> list[tuple[tuple[float, str], Path]]:
        """Every sidecar in the capture dir, oldest first: ((mtime, path under the dir), path)."""
        if not self.dir.exists():
            return []
        out = [((_mtime(p), self._rel(p)), p) for p in self.dir.rglob("*.json") if p != self.mark_path]   # (a mark kept in it)
        out.sort(key=lambda x: x[0])
        return out

    def scan(self) -> list[Call]:
        """New finished calls. trunk-recorder can publish the JSON before the WAV is fully written
        (we've read 0-byte WAVs that were 190 KB a moment later), so a call is taken only once its
        WAV is non-empty and the same size on two polls in a row; give up after SETTLE_GIVE_UP_S.
        A call written while sizeup was down (resume()) comes back with late set."""
        calls = []
        now = time.time()
        behind: list[tuple[float, str]] = []                 # not taken yet: for the mark (_save_mark)
        for key, meta in self._listing():
            k = str(meta)
            if k in self._seen:
                continue
            wav = meta.with_suffix(".wav")
            try:
                size = wav.stat().st_size
            except OSError:
                behind.append(key)
                continue  # audio not there yet: pick it up next poll
            first, last_size = self._pending.get(k, (now, -1))
            settled = size > WAV_HEADER and size == last_size
            if not settled and now - first < SETTLE_GIVE_UP_S:
                self._pending[k] = (first, size)
                behind.append(key)
                continue
            self._pending.pop(k, None)
            self._seen.add(k)
            late = k in self._late
            self._late.discard(k)
            call = parse_call(meta, self.labels, self.aliases, record_all=self.record_all)
            if call is None:
                continue
            call.late = late
            # (a late call's grants were never heard: the live ones held now came after the restart, another call's)
            if self.grants is not None and call.turns and not late:
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
            if len(self._keys) > 1000:                       # (scan() called without poll(): nothing to keep)
                self._keys.clear()
            self._keys[call.wav_path] = key
            calls.append(call)
        self._behind = behind
        return calls

    def poll(self) -> list[Call]:
        """One look: each new call to on_call, the mark moved past each one it took (never one it raised on), the
        start's "ingested late" line once those are in, the mark written when due. The calls on_call took."""
        if not self._look():
            return []
        try:
            calls = self.scan()
        except Exception as e:                               # never let one bad file stop ingest
            self.on_event(f"p25 scan error: {type(e).__name__}: {e}")
            calls = []
        took = []
        for call in calls:
            self.last_call = call.end or time.time()
            key = self._keys.pop(call.wav_path, None)
            try:
                self.on_call(call)
            except Exception as e:
                self.on_event(f"p25 call handler error: {type(e).__name__}: {e}")
                continue
            took.append(call)
            self._late_n += call.late
            if key is not None:
                with self._mark_lock:
                    if self._mark is None or key > self._mark:
                        self._mark = key
        self._say_late()
        self._save_mark()
        return took

    def _say_late(self) -> None:
        """The start's one line (#747), once every call from while it was down is in, or LATE_REPORT_S after start."""
        by = self._late_say_by
        if by is None or (self._late and time.time() < by):
            return
        self._late_say_by = None
        left = len(self._late)
        self.on_event(f"p25 {self.name}: {self._late_n} calls from while it was down ingested late "
                      f"({self._late_skipped} older skipped)" + (f"; {left} not ready yet" if left else ""))

    def _load_mark(self) -> tuple[tuple[tuple[float, str], set[str]] | None, str]:
        """((mtime, name), waiting names) from mark_path, or (None, why not)."""
        try:
            d = json.loads(self.mark_path.read_text())
        except FileNotFoundError:
            return None, "no ingest mark yet (first start here)"
        except (OSError, ValueError):
            return None, "ingest mark unreadable"
        try:
            if not isinstance(d, dict) or d.get("v") != 1:
                raise ValueError("not a mark")
            if d.get("dir") != str(self.dir):
                return None, "ingest mark is another capture dir's"
            mt, name, waiting = d["mtime"], d["name"], d.get("waiting") or []
            if isinstance(mt, bool) or not isinstance(mt, (int, float)) or not 0 <= mt < float("inf"):
                raise ValueError("mtime")
            if not isinstance(name, str) or not isinstance(waiting, list) or \
                    not all(isinstance(w, str) for w in waiting):
                raise ValueError("name")
        except (KeyError, ValueError):
            return None, "ingest mark unreadable"
        return ((float(mt), name), set(waiting)), ""

    def _save_mark(self, force: bool = False) -> None:
        """The mark to mark_path (tmp + rename, no fsync) when it changed: at most every MARK_SAVE_S, or now (force)."""
        if self.mark_path is None:
            return
        with self._mark_lock:
            mark = self._mark
            if mark is None:
                return
            now = time.time()
            if self._behind is None:
                waiting = list(self._waiting)
            else:                                            # (older than REPLAY_S: never replayed anyway)
                waiting = [n for mt, n in self._behind if (mt, n) <= mark and now - mt <= REPLAY_S]
            waiting = waiting[-MARK_WAITING_MAX:]
            state = (mark, tuple(waiting))
            if state == self._saved or (not force and now - self._saved_at < MARK_SAVE_S):
                return
            self._saved_at = now
            tmp = self.mark_path.with_name(self.mark_path.name + ".tmp")
            try:
                self.mark_path.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_text(json.dumps({"v": 1, "dir": str(self.dir), "mtime": mark[0], "name": mark[1],
                                           "waiting": waiting}))
                os.replace(tmp, self.mark_path)
            except OSError as e:
                if not self._mark_err:                       # (said once: it is tried again every MARK_SAVE_S)
                    self.on_event(f"p25 {self.name}: ingest mark not written: {type(e).__name__}: {e}")
                self._mark_err = True
                return
            self._saved, self._mark_err = state, False

    def _look(self) -> bool:
        """state / detail from whether trunk-recorder's capture dir is there; True when it is."""
        if not self.dir.exists():
            self.state, self.detail = "absent", f"no trunk-recorder output at {self.dir}"
            return False
        self.state = "ok"
        self.detail = f"watching {self.dir.name}" + (
            f" · last call {tz.hhmm(self.last_call)}" if self.last_call else "")
        return True

    def _run(self) -> None:
        while not self._stop.is_set():
            self.poll()
            self._stop.wait(self.poll_s)
        self._save_mark(force=True)
