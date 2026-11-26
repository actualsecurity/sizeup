"""P25 talkgroup calls from trunk-recorder (Fire/EMS 15 = TG 307, 60 Control = TG 301).

trunk-recorder follows the Westchester County P25 Phase II system and writes one audio file
plus a JSON sidecar per call:

    <capture_dir>/<shortName>/<YYYY>/<M>/<D>/<tg>-<start>_<freq>-call_<n>.wav
    ... .json  {"talkgroup": 307, "start_time": ..., "stop_time": ..., "freq": ...,
                "srcList": [{"src": 2391001, "time": ..., "pos": 0.0, "tag": "..."}], ...}

CallIngest polls the directory for new JSON sidecars (stdlib only, no inotify needed: POLL_S), keeps
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
"sizeup": {"split": "on"} (MRRS) it ends the transmission where the new ID came in, so each talker is its own srcList
entry at its own "pos" ("log", the default and the county's for now, only logs where it would cut). Without it, or
with a bench's own trunk-recorder, apply_grants() below still cuts such a transmission where a live grant names the
next radio.

"src" is -1 when no radio ID was decoded for that transmission ("time" is whole seconds). Each
entry is its own Turn: the same radio keying twice is two turns, and an entry with no ID is a turn
of an unknown talker, never folded into the previous one. Where the live grants (radio/live.py,
the unit script) name the radio that keyed at that moment, apply_grants() names the unknown turn,
and splits a transmission that the voice channel ran into the next one (no terminator decoded:
trunk-recorder then credits the whole transmission to the later radio).

The grant split (#819). A turn the voice channel credits to one radio can still hold several talkers: the next radio's
ID was never decoded on the voice channel, so the patch (#638) had nothing to cut at. The control channel did grant it:
trunk-recorder logs "Unit ID externally set from GRANT: <new> ... doesn't match current" (a grant or grant update for
the talkgroup naming another radio while it records the call's transmission), and apply_grants() leaves a turn with an
ID alone. Per system, config.json "sizeup": {"grant_split": "off" | "log" | "on"} (GrantSplit, default off):
plan_grant_cuts() cuts the turn where such a grant came, at the quietest gap near it (a new key-up starts with ~0.2 s
of no speech) or, with none, the grant's time less its latency; the new part is the granted radio's (Turn.via
GRANT_CUT, the row's cut "grant"). "log" only logs each cut it would make and counts it for trunk-recorder's hourly
split line (GrantCount, trunkrecd.Shipper); "on" makes the parts rows like the patch's (#816). Audio gaps alone never
cut: a talker's own pauses are as long.

The P25 emergency button (issue #128): the call JSON's top-level "emergency" (1 when the control channel
granted the call as an emergency: trunk-recorder 5.2.0 takes it from the grant that set the call up, so it
is the call's first key-up) and each srcList entry's "emergency" (always 0 in 5.2.0, which writes false
there; read anyway, for a later trunk-recorder that fills it). Call.emergency / Turn.emergency carry them;
split_call() marks the piece the button belongs to (Piece.emergency). The unit script gets no emergency
flag in 5.2.0 (plugins/unit_script passes only the system, radio ID, talkgroup and patches), so the
button is known when the call's JSON is written, not at the key-up.

Each transmission as it ends (#986). trunk-recorder writes the call's WAV and JSON only at the call's end, so a first
turn waited for every later one. Its sink closes each transmission's own WAV when it ends (the header's lengths written;
the call WAV is a new file made from them later), and the station's patch, with SIZEUP_TR_PERTX=1 (per system: its
config.json "sizeup": {"per_tx": "off" | "log" | "on"}, off by default), links it into <capture dir>/sizeup-tx/ as
"<call>-t<k>.wav" and writes "<call>-t<k>.json" beside it: <call> the call file's own stem, k its srcList index. With
"on", CallIngest hands each to on_part as a Call of one turn (Call.part = k): its row "<call>-t<k>" (turn k) at once.
The call's JSON still comes at its end; the transmissions taken as parts go with it (Call.parts) and split_call makes
only the others, under the same ids (part_rest). "log" only notes them, for the hourly line. The capture dir is polled
every POLL_S (0.25 s, was 1 s), listed with os.scandir (half of Path.rglob's CPU). Measured on a dev Mac: 0.15-0.3 ms of
CPU a look on a tree like the station's (the janitor removes empty day dirs: today's and a few more), about 0.1 % of a
core per system; 60 day dirs 2 ms, 0.8 %. A call WAV whose header says it is whole (wav_complete) is taken at once,
instead of after two looks a second apart.

Calls written while sizeup was down (#747). trunk-recorder keeps recording through a restart of sizeup (the station
service restarts for every auto-update, about a dozen times a day), so a call can conclude while nothing reads the
capture dir. CallIngest keeps a high-water mark per capture dir (mark_path, beside the station's state): the newest
sidecar it handed on, (mtime, path), and the few at or before it still waiting for their WAV. At start a sidecar newer
than the mark (or one of those waiting) and no older than REPLAY_S is ingested late (Call.late: the radio log, speech-
to-text and the hub take it, no pre-alert does, and an emergency still alarms, labelled late: sizeup/app.py);
anything older is primed as seen, as before.
With no mark (the first start, a new capture dir) or one that can't be read, every sidecar on disk is primed, as
before. The mark moves only once on_call has taken a call, and is written (tmp + rename, no fsync) at most every
MARK_SAVE_S and at stop. Taken calls' files are removed by the app (App._drop_recorder_files), so a crash between two
writes can at most hand on a call the app has already taken: the app drops such a late call by its id.
"""

from __future__ import annotations

import json
import os
import re
import statistics
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
    via: str = "srclist"          # srclist | grant (talker named by a live grant) | split (cut at a grant) | grant_cut (#819)
    emergency: bool = False       # srcList "emergency": this key-up was an emergency (P25 emergency button)
    tx: int = -1                  # its transmission's index in the call (srcList order; a cut's: the one it was cut from)


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
    late: bool = False         # written while sizeup was down, ingested at the next start (#747): no pre-alert
    # Per-transmission hand-off (#986, the module's doc): part = this Call is one transmission of a call still going on
    # (its index k; id the call's own, its row "<id>-t<k>"); parts = at the call's end, the transmissions already taken
    # as parts (their rows are in: only the others are made)
    part: int | None = None
    parts: frozenset = frozenset()

    def __post_init__(self) -> None:
        self.turns = [t if isinstance(t, Turn) else Turn(float(t[0]), str(t[1])) for t in self.turns]


WAV_HEADER = 44            # bytes; a WAV this size or smaller has no audio yet
SETTLE_GIVE_UP_S = 60.0
# A call WAV whose header doesn't say it is whole (wav_complete) is taken once its size has held this long (the poll
# was 1 s and the size had to hold across two polls before #986)
SETTLE_S = 1.0
POLL_S = 0.25              # the capture dir's poll (#986: 1 s before; measured cost in the module's doc)
# Per-transmission hand-off (#986, the module's doc)
PARTS_DIR = "sizeup-tx"    # under the capture dir: trunk-recorder's <call>-t<k>.wav + .json, SIZEUP_TR_PERTX=1
PER_TX_MODES = ("off", "log", "on")
PART_KEEP_S = 1800.0       # a call's parts are kept in mind this long for its JSON (a call never concluded: dropped)
PART_STALE_S = 600.0       # a part file this old is an orphan (its call long over): removed
PART_REPORT_S = 3600.0     # the hourly per-tx line
CALL_NUM = re.compile(r"-call_(\d+)$")
PART_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,110}")
# Calls written while sizeup was down (#747): ingested late at start back this far (older ones are primed as seen)
REPLAY_S = 600.0
MARK_SAVE_S = 5.0          # the high-water mark is written at most this often (and at stop)
MARK_WAITING_MAX = 200     # sidecars at or before the mark still waiting for their WAV, kept in the mark
STOP_JOIN_S = 3.0          # stop(): the poll thread finishes the call in hand, then the mark is written
LATE_REPORT_S = SETTLE_GIVE_UP_S + 5.0     # the start's "ingested late" line, at the latest this long after start
CLOCK_SLACK_S = 1.0        # a mark (or its write) further ahead of the clock than this: the clock stepped back since


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


def wav_complete(path: Path, size: int) -> bool:
    """Whether a WAV's header says it is whole: its RIFF length is the file's and its data chunk fits in it. Both
    writers of a call's audio put a placeholder there when they open the file (trunk-recorder's sink 0, ffmpeg
    0xFFFFFFFF) and the real lengths when they close it, the RIFF length last; so a file still being written never
    passes, and one that passes is taken at once instead of after its size held still (SETTLE_S)."""
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
    except OSError:
        return False
    if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
        return False
    riff = int.from_bytes(head[4:8], "little")
    if riff in (0, 0xFFFFFFFF) or riff + 8 != size:
        return False
    at = 12
    while at + 8 <= len(head):
        n = int.from_bytes(head[at + 4:at + 8], "little")
        if head[at:at + 4] == b"data":
            return n != 0xFFFFFFFF and at + 8 + n <= size
        at += 8 + n + (n & 1)
    return False


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
    for i, s in enumerate(d.get("srcList") or []):
        if not isinstance(s, dict):
            continue
        pos = round(max(0.0, _num(s.get("pos"), 0.0)), 2)
        src = str(s.get("src", "")).strip()
        src = "" if src in UNKNOWN_SRC else src
        tag = str(s.get("tag") or "").strip() if src else ""
        f = freqs.get(pos) or {}
        turns.append(Turn(pos, talker_name(src, tag, aliases) if src else "", src, tag, _num(f.get("len")),
                          _num(s.get("time")), int(_num(f.get("error_count"), 0) or 0),
                          emergency=_flag(s.get("emergency")), tx=i))
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
                            t.errors if k == 0 else 0, via, t.emergency and k == 0, t.tx))
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
    tx: tuple = ()             # the call's transmissions it holds (Turn.tx; a merged fragment's too)
    turn: int | None = None    # its turn number when it isn't its place in the list (#986: a transmission's index)


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
    one merge. One piece keeps the call's own id; more are "<id>-t0", "-t1", ... in talk order.
    Per-transmission hand-off (#986): a part (Call.part = k) is one piece, "<id>-t<k>" (turn k); a call whose parts were
    taken (Call.parts) makes only the pieces of its other transmissions (part_rest)."""
    audio = call.audio if call.audio is not None else np.zeros(0, np.float32)
    n = len(audio)

    def pairs_of(t: Turn | None, names: list[str]) -> list[tuple[str, str]]:
        if t is not None and t.src:
            return [(t.src, t.tag or t.talker)]
        return [(call.ids.get(x, ("", ""))[0], call.ids.get(x, ("", ""))[1] or x) for x in names if x]

    turns = sorted(call.turns, key=lambda t: t.pos)
    if call.part is not None:
        t = turns[0] if turns else None
        stt = n >= MIN_STT_S * rate and not (n < SHORT_TURN_S * rate and not has_voice(audio, rate))
        return [Piece(f"{call.id}-t{call.part}", call.start, call.start + n / rate, t.talker if t else "", audio,
                      pairs_of(t, [t.talker] if t else []), stt, t.via if t else "srclist", call.emergency,
                      (call.part,), call.part)]
    if not turns or (len(turns) > 1 and all(t.pos <= 0 for t in turns)):
        # No positions (an old JSON shape): the talkers can't be placed in the audio.
        names = list(call.units)
        return part_rest(call, [Piece(call.id, call.start, call.end, ", ".join(names), audio, pairs_of(None, names),
                                      emergency=call.emergency or any(t.emergency for t in turns),
                                      tx=tuple(t.tx for t in turns))])
    cuts = [(min(n, max(0, int(round(t.pos * rate)))), t) for t in turns]
    raw: list[list] = []                                     # [a, b, turn]
    for i, (a, t) in enumerate(cuts):
        b = cuts[i + 1][0] if i + 1 < len(cuts) else n
        if b > a:                                            # an empty one has no audio to keep
            raw.append([a, b, t])
    if not raw:
        t = turns[-1]
        return part_rest(call, [Piece(call.id, call.start, call.end, t.talker, audio, pairs_of(t, [t.talker]),
                                      via=t.via, emergency=call.emergency or any(x.emergency for x in turns),
                                      tx=tuple(x.tx for x in turns))])
    raw[0][0] = 0
    short = SHORT_TURN_S * rate
    kept: list[list] = []                                    # [a, b, turn, emergency, transmissions]
    carry = None                                             # a leading fragment, for the next piece
    carry_em = False
    carry_tx: list[int] = []
    for a, b, t in raw:
        if len(raw) > 1 and b - a < short and not t.src and not t.talker and not has_voice(audio[a:b], rate):
            if kept:
                kept[-1][1] = b
                kept[-1][3] = kept[-1][3] or t.emergency
                kept[-1][4].append(t.tx)
            else:
                carry = a if carry is None else carry
                carry_em = carry_em or t.emergency
                carry_tx.append(t.tx)
            continue
        em, txs = t.emergency, [t.tx]
        if carry is not None:
            a, carry, em, txs = carry, None, em or carry_em, carry_tx + txs
        kept.append([a, b, t, em, txs])
    if kept and call.emergency:      # 5.2.0: the call's flag, from the grant that opened it: its first key-up
        kept[0][3] = True
    out: list[Piece] = []
    for k, (a, b, t, em, txs) in enumerate(kept):
        cid = call.id if len(kept) == 1 else f"{call.id}-t{k}"
        start = t.at if t.at is not None else call.start + a / rate
        if out and start < out[-1].start:
            start = out[-1].start
        clip = audio[a:b]
        stt = (b - a) >= MIN_STT_S * rate and not ((b - a) < short and not has_voice(clip, rate))
        out.append(Piece(cid, start, start + (b - a) / rate, t.talker, clip, pairs_of(t, [t.talker]), stt, t.via, em,
                         tuple(dict.fromkeys(txs))))
    return part_rest(call, out)


def part_rest(call: Call, pieces: list[Piece]) -> list[Piece]:
    """(#986) The pieces of a call some of whose transmissions were taken as parts (Call.parts): a piece holding any of
    them is left out (its part is in; a fragment merged into one goes with it), the others are named by their own
    transmission as the parts are, "<id>-t<k>" (turn k), so no id is ever made twice; a second piece of one
    transmission (a cut) "<id>-t<k>c<j>". No parts: the pieces as they are."""
    if not call.parts:
        return pieces
    out: list[Piece] = []
    used: dict[int, int] = {}
    for pc in pieces:
        if any(x in call.parts for x in pc.tx):
            continue
        k = min((x for x in pc.tx if x >= 0), default=-1)
        if k < 0:                                            # (no transmission index: after every part)
            k = max(call.parts) + 1
        j = used.get(k, 0)
        used[k] = j + 1
        pc.id, pc.turn = (f"{call.id}-t{k}", k) if not j else (f"{call.id}-t{k}c{j}", k)
        out.append(pc)
    return out


# The grant split (#819, the module's doc). Times in s.
GRANT_CUT = "grant_cut"          # Turn.via / Piece.via of a part cut at a mid-call grant: its talker is the granted radio
GRANT_SPLIT_MODES = ("off", "log", "on")
GAP_FRAME_S = 0.01               # the gap search's frames
GAP_BELOW_DB = 20.0              # a gap's frames: this far under the turn's speech level (its loud frames), or silent
SILENCE_DBFS = -70.0             # a gap this quiet is digital silence (no one keyed); louder: a talker's own pause


@dataclass
class GrantSplit:
    """A system's grant split: its mode and where it cuts. A grant to another radio during the call, settle_s or more
    into the current talker's turn, with no voice-ID cut (a turn's start, the patch's cut or would-cut) within voice_s,
    is cut at the quietest run of min_gap_s or more from before_s before it to after_s after it, else latency_s before
    it; grants for one radio collapse_s apart are one; no part is ever shorter than min_piece_s, and nothing is cut
    within voice_s of the call's end (the next call's grant). A unit-script grant counts only for a radio coming back:
    one the call credited before (A, B, A: the GRANT log line never names the call's own first radio)."""
    mode: str = "off"
    before_s: float = 1.5
    after_s: float = 0.5
    latency_s: float = 0.3
    min_gap_s: float = 0.15
    settle_s: float = 0.5
    voice_s: float = 1.0
    collapse_s: float = 1.0
    min_piece_s: float = 0.4

    TUNE = {"before_s": 3.0, "after_s": 5.0, "latency_s": 5.0, "min_gap_s": 5.0}     # each tunable, and its most

    @classmethod
    def of(cls, conf: dict | None) -> GrantSplit:
        """From a config.json's "sizeup" block: "grant_split" (a mode: anything else is off) and optional
        "grant_split_tune": {before_s (0-3), after_s, latency_s, min_gap_s (0-5)} in seconds; anything else keeps the
        default."""
        conf = conf if isinstance(conf, dict) else {}
        mode = conf.get("grant_split")
        out = cls(mode if mode in GRANT_SPLIT_MODES else "off")
        tune = conf.get("grant_split_tune")
        for k, v in (tune.items() if isinstance(tune, dict) else ()):
            if k in cls.TUNE and isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= cls.TUNE[k]:
                setattr(out, k, float(v))
        return out


@dataclass
class GrantCut:
    """One cut plan_grant_cuts() makes (or would): where in the call audio, for which radio, why there."""
    pos: float                 # s into the call audio
    src: str                   # the granted radio: the next part's talker
    at: float                  # the grant's time (wall clock)
    expected: float            # where the grant's time falls in the call audio
    gap: float                 # the length of the gap it was put in, 0 when none (expected - latency_s)
    level: float | None = None  # the gap's mean level, dBFS (None: no gap)
    via: str = "grant"         # what granted it: "grant" (trunk-recorder's GRANT log line) | "unit" (the unit script)

    def why(self) -> str:
        """For its log line (tuning: #819): the gap, its level and kind, or the fallback; and the grant's source."""
        if self.gap and self.level is not None:
            kind = "silence" if self.level <= SILENCE_DBFS else "pause"
            where = f"gap {self.gap:.2f} s at {max(self.level, -99.0):.0f} dBFS ({kind})"
        else:
            where = "no gap: at the grant less its latency"
        src = "unit script" if self.via == "unit" else "log line"
        return f"{where}, {self.pos - self.expected:+.1f} s from the grant ({src})"


def _frames_db(audio: np.ndarray, a: int, b: int, rate: int) -> np.ndarray:
    """Each GAP_FRAME_S frame's level (dBFS) of audio[a:b] (whole frames only)."""
    f = max(1, int(round(GAP_FRAME_S * rate)))
    n = (b - a) // f
    if n <= 0:
        return np.zeros(0)
    x = np.asarray(audio[a:a + n * f], dtype=np.float64).reshape(n, f)
    return 20 * np.log10(np.sqrt((x ** 2).mean(axis=1)) + 1e-9)


def best_gap(audio: np.ndarray, lo: float, hi: float, aim: float, level_db: float, min_gap_s: float,
             rate: int = RATE) -> tuple[float, float, float] | None:
    """(middle, length in s, mean level in dBFS) of the quietest run of quiet frames (GAP_BELOW_DB under level_db, or under NO_VOICE_DBFS)
    at least min_gap_s long between lo and hi; the nearer to aim of two as quiet (to the dB). None: no such run."""
    a, b = max(0, int(lo * rate)), min(len(audio), int(hi * rate))
    db = _frames_db(audio, a, b, rate)
    quiet = db <= max(NO_VOICE_DBFS, level_db - GAP_BELOW_DB)
    need = int(np.ceil(min_gap_s / GAP_FRAME_S - 1e-9))
    best, k, n = None, 0, len(db)
    while k < n:
        if not quiet[k]:
            k += 1
            continue
        j = k
        while j < n and quiet[j]:
            j += 1
        if j - k >= need:
            mid = a / rate + (k + j) / 2 * GAP_FRAME_S
            key = (round(float(db[k:j].mean())), abs(mid - aim))
            if best is None or key < best[0]:
                best = (key, mid, (j - k) * GAP_FRAME_S, float(db[k:j].mean()))
        k = j
    return None if best is None else (best[1], best[2], best[3])


def plan_grant_cuts(call: Call, events: list[tuple[float, str, str]], conf: GrantSplit,
                    rate: int = RATE) -> list[GrantCut]:
    """Where the grant split cuts this call (the module's doc, GrantSplit): events are (time, radio ID, "grant" | "unit"
    | "voice") from the control channel and the voice channel (radio/live.py cut_events). Only a turn with a place in
    time (Turn.at) and a length, and only in the call's audio."""
    audio = call.audio
    if audio is None or not len(audio):
        return []
    turns = sorted((t for t in call.turns if t.at is not None and t.length), key=lambda t: t.pos)
    if not turns:
        return []
    every = sorted(call.turns, key=lambda t: t.pos)
    bounds = [t.pos for t in every[1:]] + [len(audio) / rate]   # every key-up the call has already, and its end
    voices = [at for at, _s, kind in events if kind == "voice"]
    grants = sorted((float(at), str(src), kind) for at, src, kind in events
                    if kind in ("grant", "unit") and str(src) not in UNKNOWN_SRC)
    seen: dict[str, float] = {}
    cuts: list[GrantCut] = []
    cur: dict[int, tuple[float, str]] = {}                   # turn -> (its current talker's start, radio)
    levels: dict[int, float] = {}
    for at, src, kind in grants:
        k = next((i for i, t in enumerate(turns) if t.at <= at < t.at + t.length), None)
        if kind == "unit":
            # the unit script's: only a radio coming back (credited before here: a turn's, or an earlier cut's)
            here = turns[k].pos + (at - turns[k].at) if k is not None else None
            if here is None or src not in {t.src for t in every if t.src and t.pos < here} | {c.src for c in cuts}:
                continue
        last, seen[src] = seen.get(src), at
        if last is not None and at - last <= conf.collapse_s:
            continue                                         # the same grant again (an update): one
        if k is None:
            continue                                         # between transmissions, or not in this call
        t = turns[k]
        start, talker = cur.get(k, (t.pos, t.src))
        expected = t.pos + (at - t.at)
        end = t.pos + t.length
        if src == talker or expected - start < conf.settle_s:
            continue                                         # the current talker again, or its own key-up
        if any(abs(v - at) <= conf.voice_s for v in voices) or any(abs(b - expected) <= conf.voice_s for b in bounds):
            continue                                         # the voice channel cut there (or would have), or the end
        lo, hi = max(start + conf.min_piece_s, expected - conf.before_s), min(end - conf.min_piece_s,
                                                                              expected + conf.after_s)
        if hi <= lo:
            continue                                         # no room for a part on either side
        if k not in levels:
            db = _frames_db(audio, int(t.pos * rate), int(end * rate), rate)
            levels[k] = float(np.percentile(db, 90)) if len(db) else NO_VOICE_DBFS
        aim = expected - conf.latency_s
        gap = best_gap(audio, lo, hi, aim, levels[k], conf.min_gap_s, rate)
        pos = gap[0] if gap else min(max(aim, lo), hi)
        pos = round(pos, 2)
        if pos - start < conf.min_piece_s or end - pos < conf.min_piece_s:
            continue
        cuts.append(GrantCut(pos, src, at, round(expected, 2), round(gap[1], 2) if gap else 0.0,
                             round(gap[2], 1) if gap else None, kind))
        cur[k] = (pos, src)
    return cuts


def apply_grant_cuts(call: Call, cuts: list[GrantCut], aliases: dict[str, str] | None = None,
                     alias_of: Callable[[str], str] = lambda src: "") -> bool:
    """Make plan_grant_cuts()'s cuts: each a turn of its own from its pos (via GRANT_CUT, the granted radio's), the turn
    it was cut from shortened. True if anything changed."""
    if not cuts:
        return False
    aliases = aliases or {}
    out: list[Turn] = []
    for t in sorted(call.turns, key=lambda t: t.pos):
        mine = sorted((c for c in cuts if t.length and t.pos < c.pos < t.pos + t.length), key=lambda c: c.pos)
        if not mine:
            out.append(t)
            continue
        end = t.pos + t.length
        t.length = round(mine[0].pos - t.pos, 2)
        out.append(t)
        for i, c in enumerate(mine):
            nxt = mine[i + 1].pos if i + 1 < len(mine) else end
            tag = alias_of(c.src) or ""
            out.append(Turn(c.pos, talker_name(c.src, tag, aliases), c.src, tag, round(nxt - c.pos, 2),
                            (t.at + (c.pos - t.pos)) if t.at is not None else None, 0, GRANT_CUT, tx=t.tx))
    call.turns = out
    call.units = []
    for t in call.turns:
        if t.talker and (not call.units or call.units[-1] != t.talker):
            call.units.append(t.talker)
    return True


class GrantCount:
    """The grant split's cuts (made, or would be in "log") of one system, kept for trunk-recorder's supervisor
    (trunkrecd.Shipper says them in its hourly split line): {"v": 1, "system", "mode", "cuts", "calls", "at"} in
    path, cumulative (kept over sizeup's restarts), written (tmp + rename) at each cut call."""

    def __init__(self, path: str | Path, system: str = ""):
        self.path, self.system = Path(path), system
        self.cuts = self.calls = 0
        self._lock = threading.Lock()
        try:
            d = json.loads(self.path.read_text())
            if isinstance(d, dict) and d.get("v") == 1:
                self.cuts, self.calls = (int(d.get(k) or 0) for k in ("cuts", "calls"))
        except (OSError, ValueError, TypeError):
            pass

    def add(self, n: int, mode: str) -> None:
        with self._lock:
            self.cuts += n
            self.calls += 1
            self._write(mode)

    def mark(self, mode: str) -> None:
        """The mode alone written (the counts as they are): "off" once the system's grant split is off, so the hourly
        line says nothing of it."""
        with self._lock:
            self._write(mode)

    def _write(self, mode: str) -> None:
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")   # (its own tmp)
        try:
            tmp.write_text(json.dumps({"v": 1, "system": self.system, "mode": mode, "cuts": self.cuts,
                                       "calls": self.calls, "at": round(time.time(), 1)}))
            os.replace(tmp, self.path)
        except OSError:
            pass                                             # (a count, never in the way of the call)


class CallIngest:
    """Poll a trunk-recorder capture dir; on_call(Call) for each new call on our talkgroups. mark_path: where the
    high-water mark is kept (#747, the module's doc: calls written while sizeup was down); None: none kept, every
    start primes what is on disk."""

    def __init__(self, capture_dir: str | Path, labels: dict[int, str], on_call: Callable[[Call], None],
                 aliases: dict[str, str] | None = None, poll_s: float = POLL_S,
                 on_event: Callable[[str], None] = lambda m: None, name: str = "P25",
                 mark_path: str | Path | None = None):
        self.name = name  # status label (the header pill), not every talkgroup's name
        self.record_all = True  # every talkgroup trunk-recorder writes (sizeup decides what to keep)
        self.dir, self.labels, self.on_call = Path(capture_dir), labels, on_call
        self.aliases, self.poll_s, self.on_event = aliases or {}, poll_s, on_event
        self._seen: set[str] = set()
        self._pending: dict[str, tuple[float, int, float]] = {}   # json path -> (first seen, last WAV size, since when)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.state, self.detail, self.last_call = "starting", "", 0.0
        # Live grants (radio/live.py LiveTalk.grants): (system, talkgroup, t0, t1) -> [(time, radio ID)],
        # and the alias trunk-recorder logged for a radio ID. Set by the app; without them the call
        # JSON alone says who talked.
        self.grants: Callable[[str, int, float, float], list[tuple[float, str]]] | None = None
        self.alias_of: Callable[[str], str] = lambda src: ""
        # The grant split (#819): its mode, the events of a live call (LiveTalk.cut_events) and of one from while sizeup
        # was down (LogEvents.events: trunk-recorder's log), and where its cuts are counted. Set by the app.
        self.grant_split = GrantSplit()
        self.cut_events: Callable[[str, int, float, float], list[tuple[float, str, str]]] | None = None
        self.log_events: Callable[[str, int, float, float], list[tuple[float, str, str]]] | None = None
        self.grant_count: GrantCount | None = None
        # The high-water mark (#747): (mtime, path under the capture dir) of the newest sidecar on_call took, and the
        # sidecars at or before it not taken yet (their WAV still coming) as the last scan found them
        self.mark_path = Path(mark_path) if mark_path else None
        self._mark: tuple[float, str] | None = None
        self._behind: list[tuple[float, str]] | None = None    # None: not scanned yet (the loaded mark's are kept)
        self._waiting: list[str] = []                           # the loaded mark's
        self._mark_lock = threading.Lock()
        self._saved: tuple | None = None                        # what was written last, and when (monotonic)
        self._saved_at = -MARK_SAVE_S
        self._mark_err = False
        self._keys: dict[str, tuple[float, str]] = {}           # wav path of a call scan() returned -> its sidecar key
        # Calls written while sizeup was down, ingested late at this start, and the start's line about them
        self._late: set[str] = set()
        self._late_n = self._late_skipped = 0
        self._late_say_by: float | None = None
        # Per-transmission hand-off (#986): the mode (the system's config.json "sizeup": {"per_tx"}), what takes a part
        # (Call.part set; True: taken, its row made), and the calls whose parts came in, until their JSON does
        self.per_tx = "off"
        self.on_part: Callable[[Call], bool] | None = None
        self._calls: dict[str, dict] = {}                       # call id -> {start, at, key, seen {k: (release, at)}, taken}
        self._by_num: dict[tuple[str, int, int], str] = {}      # (system, talkgroup, call number) -> call id
        self._done: dict[str, float] = {}                       # call ids whose JSON came in -> when (a late part: dropped)
        self._part_stats: list[tuple[float, float]] = []        # (after its release, before its call's file) per part
        self._part_calls = self._part_taken = 0
        self._part_said = time.monotonic()
        self._part_swept = 0.0
        self.started_at = time.time()

    def prime(self) -> CallIngest:
        """Mark calls already on disk as seen: don't replay old calls as if they were live."""
        self._seen = {str(p) for _k, p in self._listing()}
        return self

    def resume(self) -> CallIngest:
        """At start, with a mark kept (#747): sidecars newer than the mark (or waiting for their WAV at it), written in
        the last REPLAY_S, are ingested late; every other one on disk is primed as seen. No mark, or one that can't be
        read: everything is primed (as prime()), and the mark starts at the newest sidecar on disk. A mark ahead of the
        clock (its newest sidecar's mtime, or when it was written: the clock stepped back since, as WSL2 does): it can't
        say what was taken, so every sidecar of the last REPLAY_S is ingested late (one taken before is dropped by its id,
        App._late_dup, and an alarm by its transmission id) and the mark starts again from the older ones."""
        loaded, why = self._load_mark()
        listing = self._listing()
        if loaded is None:
            self._seen = {str(p) for _k, p in listing}
            with self._mark_lock:
                self._mark, self._behind, self._waiting = (listing[-1][0] if listing else (0.0, "")), None, []
            self._save_mark(force=True)
            self.on_event(f"p25 {self.name}: {why}: the {len(listing)} call(s) on disk not replayed")
            return self
        mark, waiting, saved_at = loaded
        now = time.time()
        ahead = mark[0] > now + CLOCK_SLACK_S or saved_at > now + CLOCK_SLACK_S
        if ahead:
            self.on_event(f"p25 {self.name}: ingest mark is ahead of the clock (it stepped back): every call of the last "
                          f"{REPLAY_S / 60:.0f} min ingested late, any taken before dropped by its id")
            mark, waiting = (-1.0, ""), set()
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
        if ahead:                                    # (from the newest primed one: the late ones move it as they're taken)
            mark = max((k for k, p in listing if str(p) in seen), default=(0.0, ""))
        with self._mark_lock:
            self._mark, self._behind, self._waiting = mark, None, sorted(waiting)
        self._late_say_by = now + LATE_REPORT_S
        if ahead:
            self._save_mark(force=True)
        self._say_late()
        return self

    def start(self, skip_existing: bool = True) -> CallIngest:
        self.started_at = time.time()
        self._sweep_parts(0.0)     # (#986) parts from before this start: their calls' JSON brings them in
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
        out = []
        parts, mark = str(self.dir / PARTS_DIR), str(self.mark_path) if self.mark_path else ""
        stack = [str(self.dir)]
        while stack:               # (os.scandir, not Path.rglob: half the CPU, which a 0.25 s poll pays four times a second)
            d = stack.pop()
            try:
                it = os.scandir(d)
            except OSError:
                continue
            with it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if e.path != parts:      # (#986's parts)
                                stack.append(e.path)
                        elif e.name.endswith(".json") and e.path != mark:   # (a mark kept in it)
                            p = Path(e.path)
                            out.append(((e.stat().st_mtime, self._rel(p)), p))
                    except OSError:                  # removed between listing and looking
                        continue
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
            first, last_size, since = self._pending.get(k, (now, -1, now))
            since = since if size == last_size else now
            settled = size > WAV_HEADER and (wav_complete(wav, size) or (size == last_size and now - since >= SETTLE_S))
            if not settled and now - first < SETTLE_GIVE_UP_S:
                self._pending[k] = (first, size, since)
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
            self._join_parts(call)                           # (#986) its transmissions taken as parts already
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
            if self.grant_split.mode in ("log", "on") and call.turns and not call.encrypted:
                self._grant_split(call)
            if len(self._keys) > 1000:                       # (scan() called without poll(): nothing to keep)
                self._keys.clear()
            self._keys[call.wav_path] = key
            calls.append(call)
        self._behind = behind
        return calls

    def _grant_split(self, call: Call) -> None:
        """The grant split (#819) on a call: its cuts planned from the live events (a late call's from trunk-recorder's
        log), a line each (no radio ID), counted, and made when it is "on"."""
        conf = self.grant_split
        events = self.log_events if call.late else self.cut_events
        if events is None:
            return
        try:
            cuts = plan_grant_cuts(call, events(call.system, call.talkgroup, call.start - conf.before_s - GRANT_SLACK_S,
                                                call.end + GRANT_SLACK_S), conf)
            if not cuts:
                return
            dur = len(call.audio) / RATE if call.audio is not None else 0.0
            for c in cuts:
                self.on_event(f"p25 {self.name}: grant split ({conf.mode}): {call.label}: "
                              f"{'would cut' if conf.mode == 'log' else 'cut'} at {c.pos:.1f} of {dur:.1f} s, "
                              f"{c.why()}" + (" (late)" if call.late else ""))
            if self.grant_count is not None:
                self.grant_count.add(len(cuts), conf.mode)
            if conf.mode == "on":
                apply_grant_cuts(call, cuts, self.aliases, self.alias_of)
        except Exception as e:                               # the call as trunk-recorder wrote it is still the record
            self.on_event(f"p25: grant split not applied to {call.label}: {type(e).__name__} {e}")

    def poll(self) -> list[Call]:
        """One look: each new part (#986) to on_part, then each new call to on_call, the mark moved past each one it
        took (never one it raised on), the start's "ingested late" line once those are in, the mark written when due.
        The calls on_call took."""
        if not self._look():
            return []
        try:
            self._parts()
        except Exception as e:                               # never let one bad part stop ingest
            self.on_event(f"p25 {self.name}: per-tx error: {type(e).__name__}: {e}")
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

    # -- per-transmission hand-off (#986, the module's doc) ----------------------------------------------------------

    def _parts(self) -> None:
        """Each new part in the parts dir: noted for its call (log, on) and with "on" handed to on_part; its files then
        removed (taken or not: a part not taken is its call's at the call's end). Orphans swept once a minute."""
        d = self.dir / PARTS_DIR
        names = []
        if self.per_tx in ("log", "on"):                     # (off: only swept, should a trunk-recorder still write them)
            try:
                names = sorted(n for n in os.listdir(d) if n.endswith(".json"))
            except OSError:
                pass
        for name in names:
            self._part(d / name)
        self._sweep_parts(PART_STALE_S)
        self._say_parts()

    def _part(self, meta: Path) -> None:
        wav = meta.with_suffix(".wav")
        try:
            self._take_part(meta, wav)
        finally:
            for f in (meta, wav):
                try:
                    f.unlink()
                except OSError:
                    pass

    def _take_part(self, meta: Path, wav: Path) -> None:
        try:
            d = json.loads(meta.read_text())
            if not isinstance(d, dict) or d.get("v") != 1:
                raise ValueError("not a part")
            cid, k, tg, num = d["call"], d["k"], d["talkgroup"], d["call_num"]
            start, stop = float(d["start_time_ms"]) / 1000.0, float(d["stop_time_ms"]) / 1000.0
            if not (isinstance(cid, str) and PART_NAME.fullmatch(cid) and all(
                    isinstance(x, int) and not isinstance(x, bool) and x >= 0 for x in (k, tg, num))):
                raise ValueError("fields")
        except (OSError, ValueError, KeyError, TypeError) as e:
            self.on_event(f"p25 {self.name}: per-tx: {meta.name} not read: {type(e).__name__} {e}")
            return
        if cid in self._done or (tg not in self.labels and not self.record_all):
            return                                           # its call is in already; a talkgroup not taken at all
        now = time.time()
        system = str(d.get("short_name") or "")
        c = self._calls.get(cid)
        if c is None:
            c = self._calls[cid] = {"start": start, "at": now, "key": (system, tg, num), "seen": {}, "taken": set()}
            self._by_num[(system, tg, num)] = cid
        c["seen"][k] = (stop, now)
        if self.per_tx != "on" or self.on_part is None or _flag(d.get("emergency")) or _flag(d.get("encrypted")):
            return                     # log: noted only; an emergency or encrypted call is all taken at its end, as before
        call = self._part_call(d, cid, k, tg, system, start, stop, wav)
        if call is None:
            return
        try:
            took = bool(self.on_part(call))
        except Exception as e:                               # (left to its call's end)
            self.on_event(f"p25 {self.name}: per-tx handler error: {type(e).__name__}: {e}")
            took = False
        if took:
            c["taken"].add(k)
            self.last_call = stop or now

    def _part_call(self, d: dict, cid: str, k: int, tg: int, system: str, start: float, stop: float,
                   wav: Path) -> Call | None:
        """One part as a Call of one turn (Call.part = k), named from the live grants when the voice channel gave no
        radio ID (never cut: a part is one transmission)."""
        try:
            audio = read_wav_16k(wav)
        except (OSError, ValueError, EOFError, wave.Error) as e:
            self.on_event(f"p25 {self.name}: per-tx: {wav.name} not read: {type(e).__name__} {e}")
            return None
        src = str(d.get("src", "")).strip()
        src = "" if src in UNKNOWN_SRC else src
        tag = self.alias_of(src) if src else ""
        length = len(audio) / RATE
        turn = Turn(0.0, talker_name(src, tag, self.aliases) if src else "", src, tag, round(length, 2), start,
                    int(_num(d.get("error_count"), 0) or 0), tx=k)
        call = Call(cid, tg, self.labels.get(tg) or f"TG {tg}", start, max(stop, start + length),
                    [turn.talker] if turn.talker else [], audio, str(wav), [turn],
                    {turn.talker: (src, tag)} if turn.talker else {}, system, False, False, None, None,
                    _num(d.get("freq")), part=k)
        if not src and self.grants is not None:
            try:
                one = Call(cid, tg, call.label, start, call.end, [], None, "", [Turn(0.0, "", length=turn.length,
                                                                                     at=start, tx=k)], {}, system)
                got = self.grants(system, tg, start - GRANT_LEAD_S - GRANT_SLACK_S, call.end + GRANT_SLACK_S)
                if apply_grants(one, got, self.aliases, self.alias_of) and len(one.turns) == 1 and one.turns[0].src:
                    t = one.turns[0]
                    turn.talker, turn.src, turn.tag, turn.via = t.talker, t.src, t.tag, t.via
                    call.units, call.ids = [t.talker], {t.talker: (t.src, t.tag)}
            except Exception as e:                           # (unnamed, as the voice channel left it)
                self.on_event(f"p25 {self.name}: per-tx: grants not applied: {type(e).__name__} {e}")
        return call

    def _join_parts(self, call: Call) -> None:
        """A call's JSON is in: the transmissions taken as parts go with it (Call.parts), counted for the hourly line;
        any part of it still on disk is taken first (trunk-recorder writes each before the call's JSON), and one
        coming later is dropped (its call is in)."""
        if self.per_tx in ("log", "on"):
            self._parts()
        now = time.time()
        c = self._calls.pop(call.id, None)
        if c is None:                  # (its JSON named otherwise: its first start stepped; the same call by its number)
            m = CALL_NUM.search(call.id)
            cid = self._by_num.get((call.system, call.talkgroup, int(m.group(1)))) if m else None
            if cid is not None and abs(self._calls.get(cid, {}).get("start", -1e18) - call.start) < 60:
                c = self._calls.pop(cid)
                self._done[cid] = now
        self._done[call.id] = now
        if len(self._done) > 2000:
            for x in sorted(self._done, key=self._done.get)[:1000]:
                self._done.pop(x, None)
        if self.per_tx == "on" and call.start < self.started_at and not call.late:
            call.straddles = True      # type: ignore[attr-defined]  # (going on at this start: parts taken before?)
        if c is None:
            return
        self._by_num.pop(c["key"], None)
        call.parts = frozenset(c["taken"])
        self._part_calls += 1
        self._part_taken += len(c["taken"])
        for release, at in c["seen"].values():
            self._part_stats.append((max(0.0, at - release), max(0.0, now - at)))
        del self._part_stats[:-5000]

    def _sweep_parts(self, older_s: float) -> None:
        """Part files older than older_s removed (0: all, at start); calls whose JSON never came forgotten."""
        now = time.time()
        if older_s and now - self._part_swept < 60:
            return
        self._part_swept = now
        for cid in [x for x, c in self._calls.items() if now - c["at"] > PART_KEEP_S]:
            self._by_num.pop(self._calls.pop(cid)["key"], None)
        d = self.dir / PARTS_DIR
        try:
            names = os.listdir(d)
        except OSError:
            return
        for n in names:
            f = d / n
            try:
                if not older_s or now - f.stat().st_mtime > older_s:
                    f.unlink()
            except OSError:
                pass

    def _say_parts(self) -> None:
        """Hourly (#986): what the parts gained, counts and seconds only (never a radio ID)."""
        mono = time.monotonic()
        if mono - self._part_said < PART_REPORT_S:
            return
        self._part_said = mono
        stats, self._part_stats = self._part_stats, []
        calls, taken, self._part_calls, self._part_taken = self._part_calls, self._part_taken, 0, 0
        if not stats:
            return

        def q(xs: list[float], p: float) -> float:
            xs = sorted(xs)
            return xs[min(len(xs) - 1, int(p * len(xs)))]
        after, before = [a for a, _b in stats], [b for _a, b in stats]
        self.on_event(f"p25 {self.name}: per-tx ({self.per_tx}) last hour: {len(stats)} transmissions in {calls} calls"
                      + (f", {taken} taken as they ended" if self.per_tx == "on" else "")
                      + f"; ready {statistics.median(after):.2f} s after the release (p90 {q(after, 0.9):.2f}), "
                      f"{statistics.median(before):.1f} s before the call's file (p90 {q(before, 0.9):.1f})")

    def _say_late(self) -> None:
        """The start's one line (#747), once every call from while it was down is in, or LATE_REPORT_S after start."""
        by = self._late_say_by
        if by is None or (self._late and time.time() < by):
            return
        self._late_say_by = None
        left = len(self._late)
        if not (self._late_n or self._late_skipped or left):
            return                                   # (nothing from while it was down: nothing said)
        self.on_event(f"p25 {self.name}: {self._late_n} calls from while it was down ingested late "
                      f"({self._late_skipped} older skipped)" + (f"; {left} not ready yet" if left else ""))

    def _load_mark(self) -> tuple[tuple[tuple[float, str], set[str], float] | None, str]:
        """((mtime, name), waiting names, when it was written (wall clock; 0 unknown)) from mark_path, or (None, why
        not)."""
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
            at = d.get("saved_at", 0.0)
            at = float(at) if isinstance(at, (int, float)) and not isinstance(at, bool) and 0 <= at < float("inf") else 0.0
        except (KeyError, ValueError):
            return None, "ingest mark unreadable"
        return ((float(mt), name), set(waiting), at), ""

    def _save_mark(self, force: bool = False) -> None:
        """The mark to mark_path (tmp + rename, no fsync) when it changed: at most every MARK_SAVE_S, or now (force)."""
        if self.mark_path is None:
            return
        with self._mark_lock:
            mark = self._mark
            if mark is None:
                return
            now, mono = time.time(), time.monotonic()        # (the throttle on the monotonic clock: a step can't stall it)
            if self._behind is None:
                waiting = list(self._waiting)
            else:                                            # (older than REPLAY_S: never replayed anyway)
                waiting = [n for mt, n in self._behind if (mt, n) <= mark and now - mt <= REPLAY_S]
            waiting = waiting[-MARK_WAITING_MAX:]
            state = (mark, tuple(waiting))
            if state == self._saved or (not force and mono - self._saved_at < MARK_SAVE_S):
                return
            self._saved_at = mono
            tmp = self.mark_path.with_name(self.mark_path.name + ".tmp")
            try:
                self.mark_path.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_text(json.dumps({"v": 1, "dir": str(self.dir), "mtime": mark[0], "name": mark[1],
                                           "waiting": waiting, "saved_at": now}))
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
