"""The capture-split contract's reference aids (contracts/README.md "Capture split (designed)"; the design is
docs/native/capture-split.md, #773). Nothing of the split is implemented yet, so what the schemas cannot say is written
here as small pure functions and models that the tests run:

    check_spool, Spool,       the spool directory's rules (contracts/capture/spool.md), a writer that keeps them, the
    recover_seq, is_emergency next sequence number at a start, and what an emergency item is
    frame, unframe            the live socket's framing (contracts/capture/socket.md)
    age, is_late, WallSteps   which clock an age is taken from, and what a wall step is
    Hub, Lane, P25Bits,       the urgent lane's idempotency, as a model of the hub's dedupe and of capture's sender,
    match_call, p25_key,      capture's match of a P25 call to its live alarm and the alarm's key, what counts as the
    hub_shaped, trimmed       hub's answer, and an item cut to fit the request's bytes
    Requests                  the socket's request rules (an id applied once, a deadline on the boot clock)
    combination, read_mode    the two mode files

THESE ARE SPECIFICATION AIDS, NOT PRODUCTION CODE. They say what the contract means in a form a test can run; they
are not the hub's route, not capture's sender and not the spool's writer, and nothing under sizeup/ imports them."""
from __future__ import annotations

import copy
import io
import json
import os
import re
import struct
import wave
from pathlib import Path

from jsonschema import Draft202012Validator

from tests import contracts as c

CAPTURE = c.ROOT / "capture"                 # the contract's files that are not schemas: prose, DDL, data tables
NAMES = ("capture-spool-sidecar", "capture-spool-cursor", "capture-urgent-item", "capture-urgent-request",
         "capture-urgent-response", "capture-socket-event", "capture-socket-request", "capture-socket-reply",
         "capture-wish", "capture-status", "capture-hello-answer")
EXTENSIONS = ("capture-hello-answer",)       # an as-built schema plus optional keys (as the node schemas of #774 are)


def data(name: str) -> dict:
    """One of the contract's data tables, contracts/capture/<name>.json."""
    return c.loads_strict((CAPTURE / f"{name}.json").read_text(encoding="utf-8"))


# -- schemas that choose by a word (`kind`, `t`, `op`) -----------------------------------------------------------------
# These say `if` <word> `then` <that word's fields>, so that a word from a newer build needs nothing. The harness's
# own `undeclared` does not look inside `then`; this one does.

def opened(name: str, inst) -> dict:
    """The schema as it applies to inst: itself and the `then` of every `if` inst meets, as one `allOf`."""
    doc = c.load(name)
    reg = c.registry()
    then = [b["then"] if "if" in b else b for b in doc.get("allOf", [])
            if "if" not in b or Draft202012Validator(b["if"], registry=reg).is_valid(inst)]
    # (the more specific last-listed `then` first: where two parts name one key, tests/contracts.py reads the first's)
    return {"allOf": [*reversed(then), {k: v for k, v in doc.items() if k != "allOf"}]}


def undeclared(name: str, inst) -> list[str]:
    """The keys of inst no part of the schema names, wherever the part that applies lists its keys (x-complete)."""
    doc = c.load(name)
    return c._undeclared(opened(name, inst), c.registry().resolver(doc["$id"]), inst, "$")


def with_unknown_keys(name: str, inst, key: str = "from_the_future") -> tuple[object, int]:
    """(a copy of inst with a key no schema names in every object the schema describes field by field, how many were
    added): tests/contracts.py with_unknown_keys, for a schema that chooses by a word."""
    doc = c.load(name)
    out = copy.deepcopy(inst)
    return out, c._inject(opened(name, inst), c.registry().resolver(doc["$id"]), out, key)


# -- clocks and limits (docs/native/capture-split.md 4 "One rule", 4.1 "Late", 4.3 "Clocks") ---------------------------

LIVE_MAX_AGE_S = 60.0        # a spool item read at this age or older is `late`
PAGE_ITEM_MAX_AGE_S = 120.0  # a page revision unsent this long after capture made it is not sent ...
PAGE_ENDED_MAX_AGE_S = 600.0  # ... but an `ended` one stays sendable this long
STATUS_STALE_S = 15.0        # a status file this old, on the reader's boot, is a capture that is down
CLOCK_STEP_S = 5.0           # a change of wall - boot clock by more than this, between samples ...
STEP_SAMPLE_S = 5.0          # ... at most this far apart, is a wall step
FROZEN_S = 60.0              # the wall clock outran the boot clock by more than this: the boot clock froze (or a step)
RATE_BOUND = 0.10            # every window on the station is safe with a boot clock off by +-10 %


def outside(value: float, limit: float) -> bool:
    """Every limit of the contract is exclusive on the inside: under the limit is inside, EXACTLY the limit is
    outside (60 s is late, 120 s is expired, an alarm exactly `max_age_s` old is stale, a bit last set exactly 10 min
    ago is a new press). A slack written as "+-" is inclusive: `within`."""
    return value >= limit


def within(value: float, low: float, high: float) -> bool:
    return low <= value <= high


def age(event_at: float, event_boot: float, boot: str, *, wall: float, boottime: float, boot_now: str) -> tuple[float, str]:
    """(age_s, age_src) of an urgent item at the moment of sending. Within one boot: the boot clock, and only it (a
    wall clock that stepped must not make a fresh alarm look old). From before a reboot the boot clock says nothing:
    the wall-clock age, clamped at 0. Never null, and the item is never held back for want of a good clock."""
    if boot == boot_now:
        return max(0.0, boottime - event_boot), "boot"
    return max(0.0, wall - event_at), "wall"


def larger_age(at: float, at_boot: float, boot: str, *, wall: float, boottime: float, boot_now: str) -> float:
    """The age for `late` and for a page revision's expiry: the LARGER of the boot-clock age and the wall-clock age
    (either clock can under-count, and for these the safe side is "old"). Across boots only the wall age exists, and
    a negative one counts as old."""
    wall_age = wall - at
    if boot != boot_now:
        return float("inf") if wall_age < 0 else wall_age
    return max(boottime - at_boot, wall_age)


def is_late(sidecar: dict, *, wall: float, boottime: float, boot_now: str) -> bool:
    """Is a spool item `late` at the app's read: by age and nothing else. (An emergency in it is never gated by this:
    its alarm went by the urgent lane, and the late copy only attaches to it.)"""
    return outside(larger_age(sidecar["end"], sidecar["closed_boot"], sidecar["boot"], wall=wall, boottime=boottime,
                              boot_now=boot_now), LIVE_MAX_AGE_S)


def capture_down(status: dict, *, boottime: float, boot_now: str) -> bool:
    """Does a reader take capture as down from its status file: `at_boot` 15 s or more behind the reader's
    CLOCK_BOOTTIME, or a file of another boot (left from before a reboot)."""
    return status["boot"] != boot_now or outside(boottime - status["at_boot"], STATUS_STALE_S)


URGENT_STATES = ("off", "no_credentials", "no_route", "refused", "retrying", "ok")


def urgent_state(*, mode: str, forward: bool, caps: list[str], credentials: bool, last: str) -> str:
    """The status file's `urgent.state`: one word, the FIRST of URGENT_STATES that holds. last: what the hub's last
    answer was (`ok`, `retrying`, `refused`, `no_route`). A reset of the file is not a state: `resets`, `reset_at`."""
    if mode != "send" or not forward or "urgent" not in caps:
        return "off"
    if not credentials:
        return "no_credentials"
    return last if last in ("no_route", "refused", "retrying") else "ok"


# -- the socket's framing (contracts/capture/socket.md; #709's: sizeup/cloud/sttencode.py) -------------------------------

MAX_HEADER = 64 * 1024       # bytes of JSON header; a longer one is refused before it is read
MAX_PAYLOAD = 1024 * 1024    # bytes of payload a version 1 reader skips; version 1 writers send none


class FrameError(ValueError):
    pass


def frame(header: dict, payload: bytes = b"") -> bytes:
    """One frame: `>II` (header length, payload length), the header as compact UTF-8 JSON, the payload."""
    h = json.dumps(header, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    if len(h) > MAX_HEADER or len(payload) > MAX_PAYLOAD:
        raise FrameError("too big")
    return struct.pack(">II", len(h), len(payload)) + h + payload


def unframe(buf: bytes) -> tuple[dict, bytes, bytes] | None:
    """(header, payload, the rest of buf) for the first frame in buf; None while it is not all there yet. FrameError
    for a frame a reader must close the connection on: a length over the limit (refused before anything is read), a
    header that is not a JSON object."""
    if len(buf) < 8:
        return None
    hl, pl = struct.unpack(">II", buf[:8])
    if hl > MAX_HEADER or pl > MAX_PAYLOAD:
        raise FrameError("too big")
    if len(buf) < 8 + hl + pl:
        return None
    try:
        head = c.loads_strict(buf[8:8 + hl])
    except ValueError:
        raise FrameError("header: not JSON") from None
    if not isinstance(head, dict):
        raise FrameError("header: not an object")
    return head, buf[8 + hl:8 + hl + pl], buf[8 + hl + pl:]


def unframe_all(buf: bytes) -> tuple[list[tuple[dict, bytes]], bytes]:
    """Every whole frame at the front of buf, and what is left (the start of a frame still arriving)."""
    out = []
    while (got := unframe(buf)) is not None:
        out.append(got[:2])
        buf = got[2]
    return out, buf


# -- the spool directory (contracts/capture/spool.md) --------------------------------------------------------------------

SEQ_DIGITS = 12
ITEM_NAME = re.compile(r"([0-9]{12})-([A-Za-z0-9][A-Za-z0-9._-]{0,120})\.json")
ANY_NAME = re.compile(r"([0-9]{12})-.+")
WAV_HEADER = 44


def item_name(seq: int, item_id: str, suffix: str) -> str:
    return f"{seq:0{SEQ_DIGITS}d}-{item_id}{suffix}"


def slug(label: str) -> str:
    """A channel label as it stands in an id: spaces removed (as today), any other character outside
    [A-Za-z0-9._-] replaced by `_`."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", label.replace(" ", ""))


def segment_id(label: str, start: float) -> str:
    """A transmission's id: the label's slug, a dash, the start in whole milliseconds. For every label made of
    [A-Za-z0-9._-] and spaces it is exactly today's clip id."""
    return f"{slug(label)}-{int(start * 1000)}"


def slug_clashes(labels: list[str]) -> list[list[str]]:
    """The labels of a channel set that give one slug: capture refuses such a set (`refusals`, `what: "channel"`)."""
    by: dict[str, list[str]] = {}
    for label in labels:
        by.setdefault(slug(label), []).append(label)
    return [group for group in by.values() if len(group) > 1]


def wav_s16(samples: int, rate_hz: int = 16000) -> bytes:
    """A mono s16le WAV of that many (silent) samples: a 44-byte header and 2 bytes a sample."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate_hz)
        w.writeframes(b"\x00\x00" * samples)
    return buf.getvalue()


def _order_key(side: dict) -> str | None:
    """What "per channel" means for the order rule: a segment's channel label, a call's system and talkgroup."""
    if side.get("kind") == "segment":
        return f"ch:{side['channel']['label']}"
    if side.get("kind") == "call":
        return f"tg:{side['system']}:{side['talkgroup']}"
    return None


def is_emergency(side: dict) -> bool:
    """An emergency item, for the spool's bounds (4.1): any of four signs. An `ids` entry with `emergency: true`; a
    call's turn with `emergency: true`; the call-level `emergency: true`; a non-empty `alarm_key`. One test, the same
    in capture and in check_spool."""
    return side.get("emergency") is True or bool(side.get("alarm_key")) or any(
        isinstance(x, dict) and x.get("emergency") is True for x in [*(side.get("ids") or []), *(side.get("turns") or [])])


def _read(path: Path):
    """A JSON file's content, None when it is not there or not JSON."""
    try:
        return c.loads_strict(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _epoch(root: Path) -> str:
    try:
        return (root / "EPOCH").read_text(encoding="ascii").strip()
    except OSError:
        return ""


def read_seq(root: Path) -> int | None:
    """The number in SEQ; None when the file is missing or is not a number."""
    try:
        text = (root / "SEQ").read_text(encoding="ascii").strip()
    except OSError:
        return None
    return int(text) if re.fullmatch(r"[0-9]{1,12}", text) else None


def _cursor_top(root: Path) -> int:
    """The reader's cursor's highest number, `seq` or any of `done`, when it is of this spool; 0 otherwise."""
    cursor = _read(root / "cursor.app.json")
    if isinstance(cursor, dict) and cursor.get("spool_epoch") == _epoch(root) and not c.errors("capture-spool-cursor", cursor):
        return max([cursor["seq"], *cursor.get("done", [])])
    return 0


def _names_top(root: Path) -> int:
    """The highest number in any file name in items/ (debris included) and in quarantine/."""
    names = [ANY_NAME.fullmatch(p.name) for d in ("items", "quarantine") if (root / d).is_dir()
             for p in (root / d).iterdir()]
    return max([int(m[1]) for m in names if m], default=0)


def recover_seq(root: Path) -> int:
    """The next sequence number after a start, ALWAYS from all of them: the largest of SEQ (0 when missing or not a
    number), the highest number in a file name in items/ (debris included) and in quarantine/, and the reader's
    cursor (read-only: its `seq` and its highest `done`, when it is of this spool); plus 1. SEQ is not fsynced at
    each publish, so a power cut can roll it back behind items already handled and pruned: a number at or under
    the cursor would count as done and never be handled."""
    return max(read_seq(root) or 0, _names_top(root), _cursor_top(root)) + 1


def check_spool(root: Path, settled: bool = True) -> list[str]:
    """Every rule of contracts/capture/spool.md the directory breaks, as `rule: detail` lines ([] for none).

    settled: nothing is being written or removed (capture has just started and cleaned up, or has stopped). Then
    tmp/ is empty and items/ holds nothing but complete items. While capture runs (settled=False) tmp/ may hold
    writes in progress, and items/ the files of ONE item being published (above every complete one: its sidecar is
    not renamed yet) and of ONE item being removed (its sidecar gone or rewritten first, its audio not yet)."""
    out: list[str] = []
    root = Path(root)
    for d in ("tmp", "items"):
        if not (root / d).is_dir():
            out.append(f"layout: no {d}/")
    epoch = _epoch(root)
    if not re.fullmatch(r"[0-9a-f]{16}", epoch):
        out.append("epoch: EPOCH is not 16 hex characters")
    if out:
        return out
    seq_file = read_seq(root)
    if seq_file is None:
        out.append("seq: SEQ is missing or not a number (it counts as 0: the next number is taken above every file "
                   "name in items/ and quarantine/ and above the reader's cursor)")
    elif seq_file < _cursor_top(root) or seq_file < _names_top(root) - (0 if settled else 1):
        out.append(f"seq-behind: SEQ is {seq_file}, below the reader's cursor or a file name (a power cut rolled it "
                   f"back: capture logs and counts it, `spool.seq_behind`, and starts above all of them)")
    if settled and any((root / "tmp").iterdir()):
        out.append("tmp: writes in progress left in tmp/")
    items = root / "items"
    files = {p.name: p for p in items.iterdir()}
    links = {name for name, p in files.items() if p.is_symlink()}
    for name in sorted(links):                      # (never followed: a dangling one must not stop the check)
        out.append(f"link: {name} is a symbolic link (in items/ a file is a file or a hard link)")

    def size(name: str) -> int:
        return os.lstat(items / name).st_size

    sides: list[tuple[int, str, dict]] = []         # (seq, the sidecar's stem, the sidecar) of each valid sidecar
    stems: dict[str, dict | None] = {}              # every sidecar's `<seq>-<id>`, with what it says (None: unreadable)
    owned: set[str] = set()
    first: dict[int, str] = {}
    ids: dict[str, int] = {}
    for name in sorted(n for n in files if n.endswith(".json")):
        m = ITEM_NAME.fullmatch(name)
        if m is None:
            out.append(f"name: {name} is not <seq, 12 digits>-<id>.json")
            continue
        owned.add(name)
        seq, stem = int(m[1]), name[:-len(".json")]
        if seq in first:                            # two complete items under one number
            out.append(f"seq: {m[1]} is used by two items ({first[seq]}.json and {name})")
        first.setdefault(seq, stem)
        side = None if name in links else _read(files[name])
        stems[stem] = side if isinstance(side, dict) else None
        if name in links:
            continue
        if side is None:
            out.append(f"sidecar: {name} is not JSON (a sidecar is written whole under tmp/ and renamed)")
            continue
        errs = c.errors("capture-spool-sidecar", side)
        if errs:
            stems[stem] = None
            out.append(f"sidecar: {name}: {errs[0]}")
            continue
        if side["seq"] != seq or side["id"] != m[2]:
            out.append(f"name: {name} holds seq {side['seq']} id {side['id']}")
        if side["spool_epoch"] != epoch:
            out.append(f"epoch: {name} is of spool {side['spool_epoch']}, not {epoch}")
        if side["id"] in ids:
            out.append(f"id: {side['id']} is on the spool twice (seq {ids[side['id']]} and {seq})")
        ids[side["id"]] = seq
        sides.append((seq, stem, side))
        audio = side.get("audio")
        if audio is not None:
            want = item_name(seq, side["id"], ".wav")
            owned.add(audio["file"])
            if audio["file"] != want:
                out.append(f"name: {name} names its audio {audio['file']}, not {want}")
            elif want not in files:
                out.append(f"audio: {name} is complete and its audio {want} is not there (audio is renamed first)")
            elif want in links:
                pass                                # (said above)
            elif size(want) != audio["bytes"]:
                out.append(f"audio-size: {want} is {size(want)} bytes, the sidecar says {audio['bytes']}")
            elif side["kind"] == "segment":
                out += _check_wav(files[want], audio)
        for key, suffix in (("bursts_audio", ".mdc.wav"), ("tr", ".tr")):
            if side.get(key):
                owned.add(side[key])
                if side[key] != item_name(seq, side["id"], suffix):
                    out.append(f"name: {name} names its {key} {side[key]}")
                elif side[key] not in files:
                    out.append(f"audio: {name} is complete and its {key} file is not there")
    last: dict[str, tuple[float, int]] = {}
    for seq, _stem, side in sorted(sides, key=lambda x: x[0]):         # per channel, seq rises in the order of `end`
        key = _order_key(side)
        if key is None:
            continue
        if key in last and side["end"] < last[key][0]:
            out.append(f"order: seq {seq} on {key[3:]} ended before seq {last[key][1]}")
        last[key] = (side["end"], seq)
    # what no sidecar names: a publish, a deletion or a drop caught half way, or something that does not belong
    newest = max(first, default=0)
    coming: dict[str, list[str]] = {}               # no sidecar yet, above every complete item: being published
    going: dict[str, list[str]] = {}                # the sidecar gone, or rewritten without the audio: being removed
    for name in sorted(set(files) - owned):
        m = ANY_NAME.fullmatch(name)
        stem = next((s for s in stems if name.startswith(s + ".")), None)
        if m is None:
            out.append(f"name: {name} does not belong in items/")
        elif stem is None:
            (coming if int(m[1]) > newest else going).setdefault(m[1], []).append(name)
        else:
            side = stems[stem]
            dropped = side is not None and ((name == stem + ".wav" and side.get("audio") is None)
                                            or (name == stem + ".mdc.wav" and not side.get("bursts_audio")))
            if dropped:
                going.setdefault(stem, []).append(name)
            elif side is not None:
                out.append(f"debris: {name}: its sidecar does not name it")
    for groups, limit, what in ((coming, 1, "a publish that did not finish"), (going, 1, "a removal that did not finish")):
        if settled or len(groups) > limit:
            out += [f"debris: {', '.join(names)}: {what} (capture removes it at start)" for names in groups.values()]
    doc = _read(root / "cursor.app.json")
    if (root / "cursor.app.json").exists():
        errs = ["not JSON"] if doc is None else c.errors("capture-spool-cursor", doc)
        if errs:
            out.append(f"cursor: {errs[0]}")
        elif doc["spool_epoch"] == epoch:
            if any(n <= doc["seq"] for n in doc.get("done", [])):
                out.append("cursor: `done` holds a seq at or below `seq`")
            if any(int(k) <= doc["seq"] or int(k) in doc.get("done", []) for k in doc.get("tries", {})):
                out.append("cursor: `tries` counts an item that is done")
    return out


def _check_wav(path: Path, audio: dict) -> list[str]:
    try:
        with wave.open(str(path), "rb") as w:
            got = (w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes())
    except (wave.Error, EOFError, OSError) as e:
        return [f"audio-format: {path.name} is not a WAV ({e})"]
    want = (audio["channels"], 2, audio["rate_hz"], audio["samples"])
    out = [] if got == want else [f"audio-format: {path.name} is {got} (channels, bytes a sample, Hz, samples), the "
                                  f"sidecar says {want}"]
    if audio["bytes"] != WAV_HEADER + 2 * audio["samples"] * audio["channels"]:
        out.append(f"audio-format: {path.name}: {audio['bytes']} bytes is not a 44-byte header and the samples")
    return out


class Crash(Exception):
    """The writer was stopped at a named point (Spool.publish(stop=...), drop_audio, prune)."""


class Spool:
    """A writer that keeps the spool's rules, for the tests' fixtures: a reference for the ORDER of the steps, not a
    spool implementation (no bounds, no sync, no pruning by age). stop= ends a step at a point a crash could.
    publish: `tmp` (everything written under tmp/, nothing renamed), `audio` (the audio renamed into items/, the
    sidecar not), `sidecar` (the sidecar renamed, SEQ not yet rewritten). drop_audio and prune: `sidecar` (the sidecar
    rewritten or deleted, the audio still there)."""

    def __init__(self, root: Path, epoch: str = "3f9a1c0b7d2e4a61"):
        self.root = Path(root)
        for d in ("tmp", "items", "quarantine"):
            (self.root / d).mkdir(parents=True, exist_ok=True)
        if not (self.root / "EPOCH").exists():
            self._replace("EPOCH", epoch + "\n")          # a new spool: made with the directory, never changed after
        self.epoch = _epoch(self.root)
        self.start()

    def _replace(self, name: str, text: str) -> None:
        tmp = self.root / "tmp" / f"{name}.new"
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, self.root / name)

    def _sidecars(self) -> dict[str, dict | None]:
        return {p.name[:-len(".json")]: (s if isinstance(s := _read(p), dict) else None)
                for p in (self.root / "items").iterdir() if ITEM_NAME.fullmatch(p.name) and not p.is_symlink()}

    def start(self) -> None:
        """What capture does at start, matching the sidecar-first order of every removal: wipe tmp/; note the highest
        number that is on disk (debris counts: it is never given out again); remove every file whose `<seq>-<id>` has
        no sidecar (a publish that did not finish, or a deletion that removed the sidecar and stopped); remove the
        audio of an item whose sidecar says it has none (a drop that rewrote the sidecar and stopped); rewrite SEQ."""
        for p in (self.root / "tmp").iterdir():
            p.unlink()
        self.next = recover_seq(self.root)
        items, sides = self.root / "items", self._sidecars()
        for p in list(items.iterdir()):
            stem = next((s for s in sides if p.name == s + ".json" or p.name.startswith(s + ".")), None)
            side = sides.get(stem) if stem else None
            if stem is None \
                    or (side is not None and p.name == stem + ".wav" and side.get("audio") is None) \
                    or (side is not None and p.name == stem + ".mdc.wav" and not side.get("bursts_audio")):
                p.unlink()
        self._replace("SEQ", f"{self.next - 1}\n")

    def publish(self, side: dict, audio: bytes | None = None, bursts: bytes | None = None, tr: bytes | None = None,
                stop: str = "") -> dict:
        """Publish one item: its files under tmp/, then (under the publish lock, here the one caller) the next seq,
        the audio renamed into items/, the sidecar written and renamed LAST, SEQ rewritten."""
        tmp, items = self.root / "tmp", self.root / "items"
        for name, blob in (("audio", audio), ("bursts", bursts), ("tr", tr)):
            if blob is not None:
                (tmp / name).write_bytes(blob)
        if stop == "tmp":
            raise Crash(stop)
        seq, self.next = self.next, self.next + 1          # taken before the audio is renamed: its name carries it
        side = dict(copy.deepcopy(side), seq=seq, spool_epoch=self.epoch)
        if audio is not None:
            side["audio"] = dict(side["audio"], file=item_name(seq, side["id"], ".wav"), bytes=len(audio))
            os.replace(tmp / "audio", items / side["audio"]["file"])
        if bursts is not None:
            side["bursts_audio"] = item_name(seq, side["id"], ".mdc.wav")
            os.replace(tmp / "bursts", items / side["bursts_audio"])
        if tr is not None:
            side["tr"] = item_name(seq, side["id"], ".tr")
            os.replace(tmp / "tr", items / side["tr"])
        if stop == "audio":
            raise Crash(stop)
        (tmp / "sidecar").write_text(json.dumps(side, allow_nan=False), encoding="utf-8")
        os.replace(tmp / "sidecar", items / item_name(seq, side["id"], ".json"))      # the completion marker
        if stop == "sidecar":
            raise Crash(stop)
        self._replace("SEQ", f"{seq}\n")
        return side

    def drop_audio(self, seq: int, stop: str = "") -> None:
        """A bound was reached: the sidecar is rewritten first (`audio` null, `audio_dropped`), then the audio goes.
        Never for an item with an emergency id: it keeps its audio until the reader has it, whatever the bounds."""
        items = self.root / "items"
        path = next(p for p in items.iterdir() if (m := ITEM_NAME.fullmatch(p.name)) and int(m[1]) == seq)
        side = json.loads(path.read_text(encoding="utf-8"))
        if is_emergency(side):
            raise ValueError(f"{side['id']} has an emergency id: its audio is never dropped at a bound")
        gone = [side["audio"]["file"], side.get("bursts_audio")]
        side.update(audio=None, bursts_audio=None, audio_dropped=True)
        (self.root / "tmp" / "sidecar").write_text(json.dumps(side), encoding="utf-8")
        os.replace(self.root / "tmp" / "sidecar", path)
        if stop == "sidecar":
            raise Crash(stop)
        for name in filter(None, gone):
            (items / name).unlink()

    def bound(self, max_items: int) -> list[int]:
        """Past `spool.max_items` the oldest sidecars go too (sidecar first), never an emergency item's."""
        items = self.root / "items"
        sides = sorted((int(m[1]), p) for p in items.glob("*.json") if (m := ITEM_NAME.fullmatch(p.name)))
        gone = []
        for seq, p in sides:
            if len(sides) - len(gone) <= max_items:
                break
            if is_emergency(json.loads(p.read_text(encoding="utf-8"))):
                continue
            stem = p.name[:-len(".json")]
            p.unlink()
            for other in items.glob(f"{stem}.*"):
                other.unlink()
            gone.append(seq)
        return gone

    def prune(self, cursor: dict, stop: str = "") -> list[int]:
        """Capture deletes what the reader's cursor says is done (the reader never deletes): the sidecar first, so
        that no sidecar ever names audio that is gone."""
        if cursor.get("spool_epoch") != self.epoch:
            return []
        done = []
        items = self.root / "items"
        for p in sorted(items.glob("*.json")):
            m = ITEM_NAME.fullmatch(p.name)
            if m and (int(m[1]) <= cursor["seq"] or int(m[1]) in cursor.get("done", [])):
                stem = p.name[:-len(".json")]
                p.unlink()
                if stop == "sidecar":
                    raise Crash(stop)
                for other in items.glob(f"{stem}.*"):
                    other.unlink()
                done.append(int(m[1]))
        return done


# -- the two mode files (docs/native/capture-split.md 4.3 "The two mode files together") -------------------------------

CAPTURE_MODES, URGENT_MODES = ("inline", "shadow", "service"), ("off", "write", "send")


def read_mode(text: str | None, known: tuple[str, ...]) -> str:
    """A one-word mode file's value: the word, surrounding whitespace dropped. A missing file (None), an empty one and a
    word this build does not know are the default, the first of `known` (`inline`, `off`): the safe side, today's
    behaviour."""
    word = (text or "").strip()
    return word if word in known else known[0]


def combination(capture_mode: str, urgent_mode: str) -> dict:
    """What one combination of the two mode files means, by the design's rules (the table in contracts/capture/
    modes.json is the same thing written out): who decodes a key-up, who writes the urgent item and where, who sends
    a live alarm to the hub, and what happens with the app down."""
    service = capture_mode == "service"
    effective = "write" if service and urgent_mode == "off" else urgent_mode      # service needs write or send
    host = "capture" if service else "app"
    decodes = [host] + (["shadow"] if capture_mode == "shadow" else [])
    writes = [] if effective == "off" else [{"by": host, "file": "urgent.sqlite"}]
    if capture_mode == "shadow":
        writes.append({"by": "shadow", "file": "urgent-shadow.sqlite"})
    sends = [{"lane": "urgent", "by": host}] if effective == "send" else []
    sends.append({"lane": "outbox", "by": "app"})              # the app's own lane always carries the alarm too
    if service:
        down = {"decoded": True, "written": True, "sent": "by capture" if effective == "send" else "when the app is back"}
    else:
        down = {"decoded": False, "written": False, "sent": "nothing to send"}
    return {"capture_mode": capture_mode, "urgent_mode": urgent_mode, "urgent_mode_in_effect": effective,
            "decodes": decodes, "writes": writes, "sends": sends, "app_down": down}


# -- the urgent lane: capture's side -------------------------------------------------------------------------------------

RESEND_BACK_S = 120.0        # a changed hub generation: what was answered this long before the old one's last sighting
RESEND_MAX = 32              # ... goes again, at most this many, the newest
REQUEST_MAX_ITEMS, REQUEST_MAX_BYTES = 32, 64 * 1024
SENT = ("applied", "duplicate", "stale")
UNKNOWN_TRIES = 5            # answers capture could not read for one item, before it gives the item up on this lane
MEMORY_ITEMS = 256           # the senders' memory: page items, and the copies of items that have a row
ALARMS_WITHOUT_A_ROW = 10_000   # the one bound on alarms that could not be written: past it the oldest is logged and let go


def gen_value(gen) -> int | None:
    """The hub's generation as a number: generations are compared BY VALUE (5 and "5" are one). None for none."""
    try:
        return c.counter(gen)
    except ValueError:
        return None


def encoded(body: dict) -> int:
    """The bytes of a request body as sent."""
    return len(json.dumps(body, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode())


TRIM_STRING = 200
TRIM_ALARM = ("source", "start", "end", "radio_id", "unit", "emergency", "emergency_via", "system", "talkgroup",
              "key_time")
TRIM_PAGE = ("channel", "started", "phase", "ours", "ended", "until")


def trimmed(item: dict, body: dict) -> dict:
    """An item over the byte limit on its own, as it is sent: its `data` cut to the essentials, `trimmed: true`
    beside them. An alarm: its fields with each string cut to 200 characters, no `ids`. A page: its fields and as
    many `pairs` as fit. The row keeps the full `data`; the app's lane carries it whole."""
    d = item["data"]
    keep = TRIM_ALARM if item["kind"] == "alarm" else TRIM_PAGE
    cut = {k: (v[:TRIM_STRING] if isinstance(v, str) else v) for k, v in d.items() if k in keep}
    out = dict(item, data=dict(cut, trimmed=True))
    if item["kind"] == "page":
        out["data"]["pairs"] = []
        for pair in d.get("pairs", []):
            more = dict(out, data=dict(out["data"], pairs=[*out["data"]["pairs"], pair]))
            if encoded(dict(body, items=[more])) > REQUEST_MAX_BYTES:
                break
            out = more
    return out


def hub_shaped(code: int, body) -> bool:
    """Is this an answer at all: a 200 whose body passes capture-urgent-response with `ok: true`; a 400, 413 or 422
    with an object holding `error` and a `reason` of the hub's refusals. Anything else of those four statuses (an
    edge's page, `200 {}`, a 422 with no body) is no answer. Every other status counts by the status alone."""
    if code == 200:
        return isinstance(body, dict) and body.get("ok") is True and not c.errors("capture-urgent-response", body)
    if code in (400, 413, 422):
        return isinstance(body, dict) and isinstance(body.get("error"), str) and body.get("reason") in REFUSALS
    return True


class Lane:
    """REFERENCE MODEL of capture's half of the urgent lane: the urgent file (`rows`, `meta`: what survives the
    process), the senders' memory, and what the senders do with the hub's answers. Time is passed in (wall,
    boottime): there is no clock here. `disk`: `ok` | `full` (inserts fail) | `stalled` (inserts do not return);
    `tick` writes what it can."""

    def __init__(self, station: str = "st-north", boot: str = "boot-1", epoch: str = "9c2d7e10a4b35f68"):
        self.station, self.boot, self.epoch = station, boot, epoch
        self.rows: dict[tuple[str, int], dict] = {}        # the `urgent` table
        self.meta: dict = {}                               # the `meta` table: gen, gen_seen_at, gen_seen_boot, gen_seen_boot_id
        self.log: list[str] = []                           # CRITICAL lines (the journal: they outlive the process too)
        self.disk, self.n = "ok", 0
        self._fresh()

    def _fresh(self) -> None:
        """A new process: nothing but the file."""
        self.memory: dict[tuple[str, int], dict] = {}      # the senders' items
        self.answers: dict[tuple[str, int], dict] = {}     # the hub's word for an item that has no row (yet)
        self.flight: dict[str, list] = {}                  # per connection, the items of the request in flight
        self.alone: set = set()                            # items of a refused batch: each goes in a request of its own
        self.tries: dict[tuple[str, int], int] = {}        # answers that could not be read, per item
        self.delivered: set[str] = set()                   # alarm keys the app's lane says the hub has
        self.beyond: list[str] = []                        # alarms the resend cap left out: named on Health
        self.order = 0

    # .. the writer .........................................................................................................
    def press(self, key: str, kind: str, data_: dict, *, wall: float, boottime: float, rev: int = 0,
              event_at: float | None = None, event_boot: float | None = None) -> dict:
        """An item is made: handed to the sender in memory at once, and to the writer. `at` is set here, once."""
        self.order += 1
        item = {"key": key, "rev": rev, "kind": kind, "at": wall, "at_boot": boottime,
                "event_at": wall if event_at is None else event_at,
                "event_boot": boottime if event_boot is None else event_boot, "boot": self.boot, "data": data_,
                "order": self.order}
        self.memory[(key, rev)] = item
        self.tick()
        self._bound()
        return item

    def tick(self) -> None:
        """The writer: INSERT OR IGNORE on (key, rev) for every item in memory that has no row, while the disk takes
        it. A row whose answer came first is written as answered, never left `new`."""
        if self.disk != "ok":
            return
        for ident, item in self.memory.items():
            if ident not in self.rows:
                self.n += 1
                got = self.answers.pop(ident, None) or {"state": "new", "hub": None, "hub_gen": None, "sent_at": None,
                                                        "sent_boot": None, "sent_boot_id": None}
                self.rows[ident] = {**{k: v for k, v in item.items() if k != "order"}, "n": self.n,
                                    "tries": self.tries.get(ident, 0), **got}

    def _bound(self) -> None:
        """The memory's bound is on page items and on the copies of items that have a row (those are read back from
        the file when their turn comes). An alarm without a row is NEVER dropped to make room; such alarms are
        bounded only at 10,000, past which the oldest is written out as a CRITICAL line and let go."""
        def oldest(pick):
            hits = [i for i, it in self.memory.items() if pick(i, it)]
            return min(hits, key=lambda i: self.memory[i]["order"]) if hits else None
        while len(self.memory) > MEMORY_ITEMS:
            victim = oldest(lambda i, it: it["kind"] == "page") or oldest(lambda i, it: i in self.rows)
            if victim is None:
                break
            del self.memory[victim]
        bare = [i for i, it in self.memory.items() if it["kind"] == "alarm" and i not in self.rows]
        while len(bare) > ALARMS_WITHOUT_A_ROW:
            victim = min(bare, key=lambda i: self.memory[i]["order"])
            it = self.memory.pop(victim)
            bare.remove(victim)
            self.log.append(f"CRITICAL urgent: alarm {it['key']} on {it['data'].get('source')} (radio "
                            f"{it['data'].get('radio_id')}, {it['event_at']}) could be neither written nor sent: let go")

    # .. what the senders see ...............................................................................................
    def _rec(self, ident) -> dict:
        return self.rows.get(ident) or self.answers.get(ident) or {"state": "new"}

    def _item(self, ident) -> dict:
        return self.memory.get(ident) or self.rows[ident]

    def _idents(self, kind: str, state: str) -> list:
        """The items of a kind in a state, in memory or in the file, in the order they were made."""
        hits = [i for i in {*self.memory, *self.rows} if self._item(i)["kind"] == kind and self._rec(i)["state"] == state]
        return sorted(hits, key=lambda i: (0, self.rows[i]["n"]) if i in self.rows else (1, self.memory[i]["order"]))

    def frames(self) -> list[dict]:
        """The items as the app sees them (`urgent` frames, `urgent_since`): `durable` false and `n` null without a row."""
        out = []
        for ident, item in self.memory.items():
            row = self.rows.get(ident)
            out.append({**{k: item[k] for k in ("key", "rev", "kind", "at", "event_at", "event_boot", "boot", "data")},
                        "n": row["n"] if row else None, "durable": row is not None})
        return out

    def unsent(self) -> list[str]:
        """The alarms the hub has not answered for: the senders' own view."""
        return [i[0] for i in self._idents("alarm", "new")]

    def not_delivered(self) -> list[str]:
        """What the station's page shows as "EMERGENCY not delivered to the hub": the unsent alarms, and the alarms
        this lane gave up on until the hub has them by the app's lane."""
        return self.unsent() + [i[0] for i in self._idents("alarm", "rejected") if i[0] not in self.delivered]

    def health(self) -> list[str]:
        """The critical Health items: an alarm that ended `rejected` names its key; so do the alarms the resend cap
        left out after a restore of the hub."""
        out = [f"critical: the hub will not take alarm {i[0]} on the urgent lane ({self._rec(i).get('hub')}): it rides "
               f"the app's lane" for i in self._idents("alarm", "rejected") if i[0] not in self.delivered]
        if self.beyond:
            out.append(f"critical: {len(self.beyond)} alarms the hub may have lost were not sent again (over the "
                       f"{RESEND_MAX} newest): {', '.join(self.beyond)}")
        return out

    def app_delivered(self, key: str) -> None:
        """The app's lane has handed the hub this alarm (its take-over does not depend on the sender)."""
        self.delivered.add(key)

    # .. the requests .......................................................................................................
    def request(self, kind: str, *, wall: float, boottime: float) -> dict | None:
        """The next request of one connection (`alarm` or `page`: never both in one); None when it has nothing to
        send. Alarms in the order they were made, never expired by any clock. A page's clock runs per revision: one
        unsent 120 s after capture made it (the larger of its two ages) is `expired`, an `ended` one after 10 min;
        and only the highest unsent revision of a key goes. An item whose answer could not be read, or that the hub
        refused in a batch, goes after the healthy ones and alone. The body never encodes over 65,536 bytes: items are
        added while it fits, and an item over the limit on its own is sent trimmed (`data.trimmed`), never dropped."""
        todo = self._idents(kind, "new")
        if kind == "page":
            keep = []
            for ident in todo:
                it = self._item(ident)
                limit = PAGE_ENDED_MAX_AGE_S if it["data"].get("ended") else PAGE_ITEM_MAX_AGE_S
                old = outside(larger_age(it["at"], it["at_boot"], it["boot"], wall=wall, boottime=boottime,
                                         boot_now=self.boot), limit)
                if old or ident[1] < max(r for k, r in todo if k == ident[0]):
                    self._mark(ident, "expired", None, wall, boottime)
                else:
                    keep.append(ident)
            todo = keep
        healthy = [i for i in todo if not self.tries.get(i) and i not in self.alone]
        batch = healthy[:REQUEST_MAX_ITEMS] or [i for i in todo if not self.tries.get(i)][:1] or todo[:1]
        if not batch:
            return None
        body = {"v": 1, "station": self.station, "urgent_epoch": self.epoch, "sent_at": wall, "items": []}
        sent = []
        for ident in batch:
            it = self._item(ident)
            age_s, src = age(it["event_at"], it["event_boot"], it["boot"], wall=wall, boottime=boottime, boot_now=self.boot)
            item = {"key": it["key"], "rev": it["rev"], "kind": it["kind"], "at": it["at"],
                    "event_at": it["event_at"], "age_s": round(age_s, 3), "age_src": src, "data": it["data"]}
            if encoded(dict(body, items=[*body["items"], item])) > REQUEST_MAX_BYTES:
                if body["items"]:
                    break                                      # the next request takes it
                item = trimmed(item, body)
            body["items"].append(item)
            sent.append(ident)
        self.flight[kind] = sent
        return body

    def _mark(self, ident, state: str, hub: str | None, wall: float, boottime: float) -> None:
        got = {"state": state, "hub": hub, "hub_gen": self.meta.get("gen"), "sent_at": wall, "sent_boot": boottime,
               "sent_boot_id": self.boot}
        if ident in self.rows:
            self.rows[ident].update(got)
        else:
            self.answers[ident] = got
        self.alone.discard(ident)
        self.tries.pop(ident, None)

    def answer(self, code: int, body: dict | None, *, wall: float, boottime: float, kind: str = "alarm") -> None:
        """The hub's answer to the request in flight on one connection, or none at all (code 0: a timeout, the
        connection gone). A 200, 400, 413 or 422 counts only with a body in the hub's shape (`hub_shaped`); without
        it, as with no answer, everything stays `new` and no try is counted. Only a 200 that names an item `applied`,
        `duplicate` or `stale` makes it sent; `rejected` ends it; an item the 200 does not name is sent again, no try
        counted; a word this build does not know is a try, and the 5th is given up as rejected. The hub's 400, 413 or
        422 refused the request whole: the first refusal marks each of its items to go alone, and only the refusal of
        a request of one item that was already alone makes that item rejected. Any other status leaves everything to
        be sent again."""
        flight = self.flight.pop(kind, [])
        if not hub_shaped(code, body):
            return
        if code == 200:
            self._gen(body.get("gen"), wall=wall, boottime=boottime)
            words = {(r.get("key"), r.get("rev")): r.get("status") for r in body["results"]}
            for ident in flight:
                if self._rec(ident)["state"] != "new" or ident not in words:
                    continue
                word = words[ident]
                if word in SENT:
                    self._mark(ident, "sent", word, wall, boottime)
                elif word == "rejected":
                    self._mark(ident, "rejected", "rejected", wall, boottime)
                else:
                    self.tries[ident] = self.tries.get(ident, 0) + 1
                    if ident in self.rows:
                        self.rows[ident]["tries"] = self.tries[ident]
                    if self.tries[ident] >= UNKNOWN_TRIES:
                        self._mark(ident, "rejected", f"{UNKNOWN_TRIES} answers it could not read", wall, boottime)
        elif code in (400, 413, 422):
            if len(flight) == 1 and flight[0] in self.alone:
                self._mark(flight[0], "rejected", f"{code} {body['reason']} for the item alone, twice", wall, boottime)
            else:
                self.alone.update(flight)

    def hello(self, code: int, body: dict | None, *, wall: float, boottime: float) -> None:
        """The answer to the 50 s `GET /api/station/hello` on the alarm connection (always asked in `send` mode): it
        carries the hub's generation, which is how an idle sender learns that the hub lost what it had answered for."""
        if code == 200 and isinstance(body, dict):
            self._gen(body.get("gen"), wall=wall, boottime=boottime)

    # .. the resend rule ....................................................................................................
    def _since_sighting(self, rec: dict, wall: float) -> bool:
        """Was this answered at or after the last sighting of the hub's generation minus 120 s. On the boot clock when
        both are of one boot; else on wall time, and when that leaves doubt (a time that lies in the future: the
        clock stepped back since) the answer is yes: a duplicate is harmless."""
        if rec.get("sent_boot_id") is not None and rec["sent_boot_id"] == self.meta.get("gen_seen_boot_id"):
            return rec["sent_boot"] >= self.meta["gen_seen_boot"] - RESEND_BACK_S
        seen_at = self.meta.get("gen_seen_at")
        if rec.get("sent_at") is None or seen_at is None or rec["sent_at"] > wall or seen_at > wall:
            return True
        return rec["sent_at"] >= seen_at - RESEND_BACK_S

    def _gen(self, gen, *, wall: float, boottime: float) -> None:
        """Every sighting of the hub's generation, in an urgent answer or a hello's. The same one (by value): the
        sighting moves on. Another one: every alarm that was answered under a different generation at or after the
        last sighting of the OLD one minus 120 s is sent again (the 32 newest at most; pages never). The window is
        anchored at that sighting, not at now, so it holds however long the hub was away; and it is read from the
        file, so it holds across capture's own restart."""
        value = gen_value(gen)
        if value is None:
            return
        old = gen_value(self.meta.get("gen"))
        if old is not None and value != old:
            hits = [i for i in self._idents("alarm", "sent")
                    if gen_value(self._rec(i).get("hub_gen")) != value and self._since_sighting(self._rec(i), wall)]
            one_boot = len({self._rec(i).get("sent_boot_id") for i in hits}) == 1
            hits.sort(key=lambda i: (self._rec(i)["sent_boot" if one_boot else "sent_at"] or 0.0,
                                     self.rows[i]["n"] if i in self.rows else -1), reverse=True)
            self.beyond = [i[0] for i in hits[RESEND_MAX:]]          # newest by answer time, then by n
            for ident in hits[:RESEND_MAX]:
                self._rec(ident)["state"] = "new"          # in the file: a restart before the resend does not lose it
                self.memory.setdefault(ident, dict(self._item(ident), order=self.order))
        self.meta.update(gen=str(value), gen_seen_at=wall, gen_seen_boot=boottime, gen_seen_boot_id=self.boot)

    # .. the process ........................................................................................................
    def crash(self) -> None:
        """Capture is killed: what was only in memory is gone."""
        self._fresh()

    def start(self, *, wall: float, boottime: float, boot: str | None = None) -> None:
        """Capture starts (after a reboot: another boot id). Into the senders go every `new` row, the oldest alarm
        first (a page revision only if it is still sendable), and, for the resend rule, every alarm row answered at
        or after the last sighting of the hub's generation minus 120 s, as "answered under `hub_gen`". A row marked
        sent is sent again only by that rule. (In the model every row is in `rows`, and the rule reads them there.)"""
        self.boot = boot or self.boot
        self._fresh()
        for ident, row in sorted(self.rows.items(), key=lambda x: (x[1]["kind"] != "alarm", x[1]["n"])):
            if row["state"] != "new":
                continue
            limit = PAGE_ENDED_MAX_AGE_S if row["data"].get("ended") else PAGE_ITEM_MAX_AGE_S
            if row["kind"] == "page" and outside(larger_age(row["at"], row["at_boot"], row["boot"], wall=wall,
                                                            boottime=boottime, boot_now=self.boot), limit):
                row["state"] = "expired"
                continue
            self.order += 1
            self.memory[ident] = {**{k: row[k] for k in ("key", "rev", "kind", "at", "at_boot", "event_at", "event_boot",
                                                         "boot", "data")}, "order": self.order}
            if row["tries"]:
                self.tries[ident] = row["tries"]
        self._bound()


class P25Bits:
    """REFERENCE MODEL of #775's transition rule, capture's side (the `p25_bit` table): a live P25 emergency bit raises
    an alarm only on a transition: the bit set for a (system, radio) that has not had it set within the last 10 min.
    The 10 min are on the BOOT clock, so a wall clock stepped back cannot hide a real new press and one stepped
    forward cannot re-alarm a stuck bit. Every sighting moves the last-set time on, so a stuck bit never re-alarms.
    The row is in the urgent file, so capture's own restart does not re-alarm; across a reboot the boot clock says
    nothing, the row counts as "not set lately" and the next set bit alarms, once. And a boot clock that froze (a
    host asleep) would hide a real press, so on the same boot the row also counts as not set lately when the wall
    clock advanced by more than 60 s more than the boot clock since the last set bit (a wall step forward of over
    60 s looks the same: one repeated alarm, the safe side)."""

    def __init__(self, quiet_s: float = 600.0):
        self.quiet_s = quiet_s
        self.rows: dict[tuple[str, str], dict] = {}                # (system, radio) -> last_set, last_set_boot, boot
        self.transitions: list[tuple[str, float]] = []             # (boot id, boot clock) of each alarm

    def seen(self, system: str, radio: str, *, wall: float, boottime: float, boot: str) -> bool:
        """The bit is set on a grant now: is this a new press (an alarm), or the same one still set."""
        row = self.rows.get((system, radio))
        press = row is None or row["boot"] != boot or outside(boottime - row["last_set_boot"], self.quiet_s) \
            or (wall - row["last_set"]) - (boottime - row["last_set_boot"]) > FROZEN_S
        self.rows[(system, radio)] = {"last_set": wall, "last_set_boot": boottime, "boot": boot}
        if press:
            self.transitions.append((boot, boottime))
        return press

    def per_hour(self, *, boottime: float, boot: str) -> int:
        """Health's counter: transitions in the last hour, a misbehaving radio or decoder shows here."""
        return sum(1 for b, t in self.transitions if b == boot and not outside(boottime - t, 3600.0))


P25_SLACK_S = 10.0           # a live grant belongs to a call when it is inside the call's span, this much either side
P25_SETTLE_S = 60.0          # the ingest's wait for a finished call's files


def p25_key(system: str, talkgroup: int, radio: str, *, event_at: float | None, at: float) -> tuple[str, str]:
    """(key, key_time) of a live P25 alarm: from the grant's own time as trunk-recorder stamps it, so the same grant
    on one station always gives one key (a restart or a replay is a `duplicate`); only when the grant carries no
    time, capture's `at`. `data.key_time` says which: `grant` | `capture`."""
    when, src = (event_at, "grant") if event_at is not None else (at, "capture")
    return f"p25-{system}-{talkgroup}-{radio}-{int(when * 1000)}", src


class WallSteps:
    """What a wall step is (docs/native/capture-split.md 4.3 "Clocks"): capture samples the offset wall - boot clock
    on its tick; a step is a change of that offset by MORE than 5 s between two samples at most 5 s apart, and only
    that. Each is logged once, with its size. The routine backward corrections of about 2 s are not steps, and drift
    (the offset moving minutes over hours) never is one."""

    def __init__(self):
        self.last: tuple[float, float] | None = None       # (boottime, offset) of the last sample
        self.at: list[float] = []                          # the boot-clock times steps were seen at
        self.log: list[str] = []

    def sample(self, *, wall: float, boottime: float) -> float | None:
        offset = wall - boottime
        step = None
        if self.last is not None and boottime - self.last[0] <= STEP_SAMPLE_S \
                and abs(offset - self.last[1]) > CLOCK_STEP_S:
            step = offset - self.last[1]
            self.at.append(boottime)
            self.log.append(f"the wall clock stepped {step:+.1f} s")
        self.last = (boottime, offset)
        return step

    def between(self, a: float, b: float) -> bool:
        """Was a step seen after boot-clock time a and up to b."""
        return any(a < t <= b for t in self.at)


def match_call(rows, call: dict, *, wall: float, boottime: float, boot: str, steps: WallSteps | None = None) -> str:
    """REFERENCE of the match capture's ingest makes when it takes a finished P25 call (at wall / boottime): the `key`
    of the live P25 alarm in the urgent file the call belongs to, or "" (the call's sidecar carries it as
    `alarm_key`, and the app's end-of-call alarm rides the hub's take-over by it: the hub compares no clocks).

    The same system + talkgroup + unit, and the grant's time (`event_at`) inside the call's span (`start` to `end`),
    10 s either side, the edges included: both are the station's wall clock. If that test fails and capture saw a
    wall step (`WallSteps`) between the grant and now, in this boot, the boot clock decides: the grant under the
    call's length plus 70 s (the slack and the ingest's settle wait) before this moment. Without a step the wall test
    alone decides, so a grant of an earlier call is never taken for this one's. Drift is not a step."""
    for row in rows:
        d = row["data"]
        if row["kind"] != "alarm" or d.get("emergency_via") != "P25" \
                or (d.get("system"), d.get("talkgroup"), str(d.get("radio_id"))) != \
                (call["system"], call["talkgroup"], str(call["unit"])):
            continue
        if within(row["event_at"], call["start"] - P25_SLACK_S, call["end"] + P25_SLACK_S):
            return row["key"]
        stepped = row["boot"] == boot and steps is not None and steps.between(row["event_boot"], boottime)
        back = boottime - row["event_boot"]
        if stepped and 0 <= back and not outside(back, (call["end"] - call["start"]) + P25_SLACK_S + P25_SETTLE_S):
            return row["key"]
    return ""


# -- the urgent lane: the hub's side --------------------------------------------------------------------------------------

RAISED, STALE, SEEN = "raised", "stale", "seen"       # an alarm key's state in the dedupe store; raised > stale > seen
_RANK = {SEEN: 0, STALE: 1, RAISED: 2}
PAGE_LIVE_MAX_S = 300.0      # the hub ends a live page entry itself this long after its last revision
AGE_MARGIN = 1.1             # every age cutoff on an emergency: MAX_AGE_S x 1.1 (a boot clock over-counts by up to 5 %)
REFUSALS = ("bad_json", "too_large", "bad_batch")     # the `reason`s of the hub's 400, 413, 422: a request refused whole
AFTER_CLEAR = "emergency still set after All clear"


class Hub:
    """REFERENCE MODEL of the hub's dedupe for `POST /api/station/urgent` (docs/native/capture-split.md 4.3
    "Idempotency, on the hub"; 11 H1b to H1k). NOT the hub: no tokens, no storage, no rig, no alarm levels. It
    holds only what decides the answer for an item and whether an alarm is raised and pushed:

    - `store`, keyed by (station, key), written by BOTH lanes after each lane's own decision. An alarm key is in one
      of three states. `raised`: an alarm exists for it, raised by either lane. `stale`: the urgent lane judged the
      item too old and made the late notice; `stale_src` says which clock that age was on. `seen`: the events lane
      met the key and raised nothing (its own age gate, an event that did not carry the emergency, notices off). An
      urgent item is `duplicate` only for raised and stale; for `seen` it is judged on its own `age_s`. The events
      lane raises nothing for a key that is stale with `stale_src` `boot`, and judges an event on its own when it is
      `wall`. For a page the store holds the highest rev taken.
    - the alarms, each with the `traffic_id` it hangs on, which the finished transmission takes over by `alarm_key`.
      "Open" is not cleared, acknowledged or not.
    - per (system, radio), the P25 press window {alarm, last}: `last` is the HUB's receive time of the radio's last
      flag. Armed only by a flag that raised or joined an alarm; a later flag joins as "still set" only while that
      alarm is open and 0 <= t - last < 600 s; an All clear ends it, and the next flag's alarm says "emergency still
      set after All clear". Two station wall times are never compared for it.
    - the live pages, by (station, key), each with the hub's own expiry. Ended is final.

    max_age_s is A7: the admin has NOT decided what a late emergency becomes. 1800 (30 min, `alarms.MAX_AGE_S`) is
    the recommended rule and only a default here. The cutoff on either lane is max_age_s x 1.1: under it an alarm and
    a push that says its age, at it or past it `stale` (a late notice, no push). The clock is `now`, the hub's own,
    set by the caller; an item is placed at `now - age_s`, never at a station's time. `restore` puts back the whole
    state as of `now - lost_s`; pushes already made stay made."""

    def __init__(self, max_age_s: float = 1800.0, repeat_s: float = 120.0, stuck_within_s: float = 600.0,
                 p25_slack_s: float = P25_SLACK_S, station: str = "st-north", multi_station: bool = False,
                 off_channels: tuple = ()):
        self.max_age_s, self.repeat_s, self.stuck_within_s, self.p25_slack_s = max_age_s, repeat_s, stuck_within_s, p25_slack_s
        self.station, self.multi_station, self.off_channels = station, multi_station, set(off_channels)
        self.now, self.leader, self.gen = 0.0, True, 1
        self.store: dict[tuple[str, str], dict] = {}
        self.alarms: list[dict] = []
        self.pages: dict[tuple[str, str], dict] = {}
        self.windows: dict[tuple[str, str], dict] = {}       # (system, radio) -> {alarm, last, cleared}
        self.pushes: list[dict] = []                          # what left the hub: never undone by a restore
        self.notices: list[dict] = []
        self.log: list[str] = []
        self._ids = 0
        self.history: list[tuple[float, dict]] = [(float("-inf"), self._state())]

    @property
    def cutoff(self) -> float:
        return round(self.max_age_s * AGE_MARGIN, 6)          # (1980.0, not 1980.0000000000002)

    def _state(self) -> dict:
        return copy.deepcopy({"store": self.store, "alarms": self.alarms, "pages": self.pages, "windows": self.windows})

    def _keep(self) -> None:
        """The state after a write, for `restore`."""
        self.history.append((self.now, self._state()))

    def _set(self, station: str, key: str, state: str, alarm: int | None = None, src: str | None = None) -> None:
        """Write a key's state: raised over stale over seen, never the other way."""
        have = self.store.get((station, key))
        if have is None or _RANK[state] >= _RANK.get(have.get("state"), -1):
            self.store[(station, key)] = {"kind": "alarm", "state": state, "rev": 0, "alarm": alarm,
                                          "stale_src": src if state == STALE else None,
                                          "first": have["first"] if have else self.now}

    def _alarm(self, **where) -> dict | None:
        return next((a for a in self.alarms if all(a.get(k) == v for k, v in where.items())), None)

    # .. the routes .........................................................................................................
    def hello(self) -> tuple[int, dict]:
        """`GET /api/station/hello`: today's answer, and the generation (H1a). Answered by a standby too."""
        return 200, {"ok": True, "station": self.station, "token": "own", "gen": self.gen}

    def urgent(self, body: dict) -> tuple[int, dict]:
        """(status, answer) for one request. A hub that is not the leader applies nothing. A request that is not
        well formed (not version 1, not 1 to 32 items each with its key, rev, kind, data and age) is refused whole,
        and nothing of it is applied."""
        if not self.leader:
            return 503, {"error": "the hub is updating: back in a few seconds", "updating": True}
        raw = json.dumps(body, separators=(",", ":")).encode()
        if len(raw) > REQUEST_MAX_BYTES:
            return 413, {"error": "body over 65536 bytes", "reason": "too_large"}
        items = body.get("items") if isinstance(body, dict) else None
        ok = isinstance(body, dict) and body.get("v") == 1 and not isinstance(body.get("v"), bool) \
            and isinstance(body.get("station"), str) and isinstance(items, list) \
            and 1 <= len(items) <= REQUEST_MAX_ITEMS and all(
                isinstance(i, dict) and isinstance(i.get("key"), str) and 0 < len(i["key"]) <= 200
                and isinstance(i.get("rev"), int) and not isinstance(i["rev"], bool) and i["rev"] >= 0
                and isinstance(i.get("kind"), str) and isinstance(i.get("data"), dict)
                and isinstance(i.get("age_s"), (int, float)) and not isinstance(i["age_s"], bool) for i in items)
        if not ok:
            return 422, {"error": "not a version 1 request of 1 to 32 whole items", "reason": "bad_batch"}
        station = body["station"]                             # (the real route takes it from the token, never from here)
        if station != self.station and not self.multi_station:
            return 409, {"error": "this hub takes one station", "reason": "multi_station_off"}
        results = []
        for item in items:
            status, reason = self._item(station, item, body.get("sent_at"))
            results.append({"key": item["key"], "rev": item["rev"], "status": status, **({"reason": reason} if reason else {})})
        self._keep()
        return 200, {"ok": True, "v": 1, "station": station, "gen": self.gen, "results": results}

    def _item(self, station: str, item: dict, sent_at) -> tuple[str, str]:
        if item["kind"] == "page":
            return self._page(station, item, sent_at), ""
        if item["kind"] != "alarm":
            return "rejected", "kind"
        key, d = item["key"], item["data"]
        radio = self._radio(d.get("system") if d.get("emergency_via") == "P25" else None, d.get("radio_id"))
        have = self.store.get((station, key))
        if have is not None and have["state"] in (RAISED, STALE):     # this lane again, or the app's lane raised it
            if have["state"] == RAISED and radio:
                self._arm(radio, self._alarm(id=have["alarm"]))
            return "duplicate", ""
        cur = self._alarm(station=station, traffic_id=key)            # today's rule: one traffic_id, one alarm
        if cur is None and radio:                                     # an end-of-call event with no alarm_key came first
            cur = self._p25_call(station, d)                          # (an older capture): station time against station time
        if cur is not None:
            self._set(station, key, RAISED, cur["id"])
            self._arm(radio, cur)
            return "duplicate", ""
        age_s = item.get("age_s")                             # judged on its own age: the key is new, or only `seen`
        age_s = min(max(float(age_s), 0.0), 86400.0) if age_s == age_s else 0.0     # clamped into [0, 24 h]
        if outside(age_s, self.cutoff):                       # A7, recommended: a late notice, no alarm, no push
            self._set(station, key, STALE, src="boot" if item.get("age_src") == "boot" else "wall")
            self.notices.append({"station": station, "key": key, "age_s": age_s})
            return "stale", ""                                # (and the press window is not armed)
        at = self.now - age_s
        cur = self._open(d, at) or self._raise(station, key, d, at=at, age_s=age_s,
                                               approx=item.get("age_src") != "boot", live_p25=bool(radio),
                                               label=self._label(radio))
        if station not in cur["sources"]:
            cur["sources"].append(station)                    # the second station is a second source, not a second alarm
        self._set(station, key, RAISED, cur["id"])
        self._arm(radio, cur)                                 # a live P25 item: the station judged it a new press
        return "applied", ""

    @staticmethod
    def _radio(system, radio_id) -> tuple[str, str] | None:
        return (str(system), str(radio_id or "")) if system else None

    def _arm(self, radio, alarm: dict | None) -> None:
        """The press window of a radio, after a flag that raised or joined `alarm`: {that alarm, the hub's now}."""
        if radio and alarm is not None:
            self.windows[radio] = {"alarm": alarm["id"], "last": self.now, "cleared": bool(alarm.get("cleared"))}

    def _label(self, radio) -> str:
        """The label of a new alarm raised by a radio's flag: after an All clear ended its window, the bit is still
        set (the radio maintainers' wording), not presented as a new press."""
        w = self.windows.get(radio) if radio else None
        return AFTER_CLEAR if w is not None and w["cleared"] else ""

    def _still_set(self, radio) -> dict | None:
        """Step 2 of the press window: the open alarm a flag received now joins as "still set", if any."""
        w = self.windows.get(radio) if radio else None
        a = self._alarm(id=w["alarm"]) if w else None
        if a is None or w["cleared"] or a.get("cleared") or not (0 <= self.now - w["last"] < self.stuck_within_s):
            return None
        return a

    def _channel(self, d: dict) -> tuple:
        """What "the same channel" is: the channel's label; for P25 the system and the talkgroup."""
        return ("p25", d.get("system"), d.get("talkgroup")) if d.get("system") else ("ch", str(d.get("source") or ""))

    def _open(self, d: dict, at: float) -> dict | None:
        """The alarm code's same-radio rule, which has no station in it: the same radio's button on the same channel
        while its alarm is open (not cleared; acknowledged or not) and was raised under REPEAT_S before, from ANY
        station, joins that alarm. Both are placed on the hub's clock, so no station's clock is compared."""
        rid = str(d.get("radio_id") or "")
        for a in reversed(self.alarms):
            if rid and a["radio_id"] == rid and a["channel"] == self._channel(d) and not a.get("cleared") \
                    and not outside(abs(at - a["at"]), self.repeat_s):
                return a
        return None

    def _raise(self, station: str, tid: str, d: dict, *, at: float, age_s: float, approx: bool = False,
               live_p25: bool = False, span: tuple[float, float] | None = None, label: str = "") -> dict:
        self._ids += 1
        a = {"id": self._ids, "station": station, "sources": [station], "traffic_id": tid, "at": at,
             "created": self.now, "radio_id": str(d.get("radio_id") or ""), "channel": self._channel(d), "acked": False,
             "cleared": False, "system": d.get("system"), "talkgroup": d.get("talkgroup"), "live_p25": live_p25,
             "start": d.get("start"), "span": span, "still_set": 0, "label": label}
        self.alarms.append(a)
        self.pushes.append({"alarm": a["id"], "at": at, "age_s": age_s, "approximate": approx, "label": label})
        return a

    def _p25_call(self, station: str, d: dict) -> dict | None:
        """The finished call's alarm a live item belongs to, when the call's event carried no `alarm_key`: system +
        talkgroup + unit, the live item's own `data.start` inside the call's span, 10 s either side, edges included.
        Both are the station's wall clock; the hub's own placing of the item is never compared with them."""
        for a in self.alarms:
            if a["station"] == station and a["span"] and (a["system"], a["talkgroup"], a["radio_id"]) == \
                    (d.get("system"), d.get("talkgroup"), str(d.get("radio_id") or "")) and "start" in d \
                    and within(float(d["start"]), a["span"][0] - self.p25_slack_s, a["span"][1] + self.p25_slack_s):
                return a
        return None

    def _page(self, station: str, item: dict, sent_at) -> str:
        k = (station, item["key"])
        have = self.store.get(k)
        if have is not None and item["rev"] <= have["rev"]:
            return "duplicate" if item["rev"] == have["rev"] else "stale"
        self.store[k] = {"kind": "page", "rev": item["rev"], "first": have["first"] if have else self.now, "alarm": None}
        until = item["data"].get("until")
        # the hub's own expiry: `until` is the station's wall clock, so only its distance from the same request's
        # `sent_at` is used, kept within 0 to 300 s; with none, 300 s after this revision
        left = PAGE_LIVE_MAX_S if until is None or not isinstance(sent_at, (int, float)) \
            else min(max(float(until) - float(sent_at), 0.0), PAGE_LIVE_MAX_S)
        old = self.pages.get(k)
        self.pages[k] = {"rev": item["rev"], "data": copy.deepcopy(item["data"]), "from": "urgent",
                         "ended": bool(item["data"].get("ended")) or bool(old and old["ended"]),     # ended is final
                         "expires": self.now + left}
        return "applied"

    def page_live(self, station: str, key: str) -> bool:
        """Is the page still going out, as the hub shows it: not ended, and not past the hub's own expiry."""
        page = self.pages.get((station, key))
        return page is not None and not page["ended"] and not outside(self.now, page["expires"])

    # .. the app's lane: /api/station/events and /api/station/state ........................................................
    def event(self, station: str, ev: dict) -> int:
        out = self._event(station, ev)
        self._keep()
        return out

    def _event(self, station: str, ev: dict) -> int:
        """One event of the app's lane: `alarm` (the take-over, `id` = the urgent key; or a finished P25 call's alarm,
        with `alarm_key` when capture matched the call to its live alarm), `traffic` (a finished transmission) or `page`
        (a finished page). Its times (`start`, `end`) are the station's wall clock. The lane asks the store before it
        raises, and writes it AFTER its own decision: `raised` for the `id` of an alarm event and for an `alarm_key`
        when an alarm exists for it, `seen` when it met the key and raised nothing. A P25 flag goes through the press
        window: tied to an alarm, it updates it; else it joins the radio's open alarm under 600 s of the hub's clock
        after its last flag; else it is judged on its own."""
        if not self.leader:
            return 503
        tid, key = str(ev.get("id") or ""), str(ev.get("alarm_key") or "")
        if ev.get("type") == "page":
            page = self.pages.get((station, str(ev.get("live_key") or "")))
            if page is not None:
                page["ended"] = True                          # the finished page ends the live entry at once
            return 200
        mine = list(dict.fromkeys(([tid] if ev.get("type") == "alarm" else []) + ([key] if key else [])))
        emergency = ev.get("type") == "alarm" or bool(ev.get("emergency"))
        p25 = bool(ev.get("system")) and "start" in ev and "end" in ev
        span = (float(ev["start"]), float(ev["end"])) if p25 else None
        radio = self._radio(ev.get("system"), ev.get("radio_id")) if p25 and emergency else None

        def done(state: str, alarm: dict | None = None, taken: bool = True) -> int:
            for k in mine:
                self._set(station, k, state, alarm["id"] if alarm else None)
            if alarm is not None and span is not None and taken:          # the call's row is the alarm's now
                alarm.update(span=span, live_p25=False)
            self._arm(radio, alarm)                                       # (nothing raised or joined: left as it was)
            return 200
        # 1. the existing rules: the alarm this event belongs to
        cur = self._alarm(station=station, traffic_id=tid)
        if cur is None and key and key != tid:                # raised at the key-up (or live, for P25): the finished
            cur = self._alarm(station=station, traffic_id=key)   # transmission takes the alarm over by its alarm_key
            if cur is not None:
                cur["traffic_id"] = tid
        if cur is None:                                       # the alarm has moved on (taken over, joined): the store knows
            known = [self.store[(station, k)] for k in mine if (station, k) in self.store]
            hit = next((h for h in known if h["state"] == RAISED and h["alarm"]), None)
            cur = self._alarm(id=hit["alarm"]) if hit else None
            if cur is None and any(h["state"] == STALE and h.get("stale_src") == "boot" for h in known):
                return 200                                    # too old on a boot-clock age: the row joins the late notice
        if cur is not None:
            return done(RAISED, cur)
        if not emergency:
            return done(SEEN)                                 # (a traffic event that carried the key and no emergency)
        if p25 and not key:                                   # an older capture: no alarm_key. Its live alarm, if any:
            live = next((a for a in self.alarms if a["station"] == station and a["live_p25"] and a["start"] is not None
                         and (a["system"], a["talkgroup"], a["radio_id"]) ==
                         (ev.get("system"), ev.get("talkgroup"), str(ev.get("radio_id") or ""))
                         and within(float(a["start"]), span[0] - self.p25_slack_s, span[1] + self.p25_slack_s)), None)
            if live is not None:
                live["traffic_id"] = tid
                return done(RAISED, live)
        # 2. the press window: the radio's open alarm, under 600 s of the hub's clock since its last flag
        joined = self._still_set(radio)
        if joined is not None:
            joined["still_set"] += 1                          # the row says "emergency still set"; no alarm, no push
            return done(RAISED, joined, taken=False)
        # 3. judged on its own
        end = float(ev.get("end", ev.get("start", self.now)))
        if outside(self.now - end, self.cutoff):              # the alarm code's own age gate, with the same margin:
            self.log.append(f"{tid}: too old to raise now (logged only)")       # the station's wall time, the hub's clock
            return done(SEEN)
        if str(ev.get("source") or "") in self.off_channels:  # notices are off for it
            return done(SEEN)
        cur = self._open(ev, self.now) or self._raise(station, tid, ev, at=self.now, age_s=max(0.0, self.now - end),
                                                      span=span, label=self._label(radio))
        return done(RAISED, cur)

    def state(self, station: str, paging: list[dict]) -> None:
        """The app's state post, its `paging` list: merged with the urgent lane's entries by `key`; the newer of the
        two, which is the later to arrive, wins. Ended is final: no state post makes an ended page live again."""
        for entry in paging:
            k = (station, entry["key"])
            old = self.pages.get(k)
            self.pages[k] = {"rev": self.store.get(k, {}).get("rev"), "data": copy.deepcopy(entry), "from": "state",
                             "ended": bool(entry.get("ended")) or bool(old and old["ended"]),
                             "expires": self.now + PAGE_LIVE_MAX_S}
        self._keep()

    def ack(self, traffic_id: str) -> None:
        """A person acknowledged the alarm. It stays open: a second station's item, or the same radio, still joins it."""
        self._alarm(traffic_id=traffic_id)["acked"] = True
        self._keep()

    def all_clear(self, traffic_id: str) -> None:
        """An All clear closes the alarm and ends its press window: a bit still set after it raises an alarm again on
        its next flag, labelled "emergency still set after All clear"."""
        a = self._alarm(traffic_id=traffic_id)
        a.update(acked=True, cleared=True)
        for w in self.windows.values():
            if w["alarm"] == a["id"]:
                w["cleared"] = True
        self._keep()

    # .. the hub itself .....................................................................................................
    def handoff(self) -> None:
        """A deploy: this task stops leading. Every write is answered 503 until lead()."""
        self.leader = False

    def lead(self) -> None:
        self.leader, self.gen = True, self.gen + 1

    def restore(self, lost_s: float) -> None:
        """A crash, and back from a replica `lost_s` old: the WHOLE state as it was at now - lost_s (each dedupe entry
        whole, so a `seen` later upgraded to `raised` comes back as `seen`; the alarms, the live pages, the press
        windows). Pushes already made stay made, and the generation is new."""
        cut = self.now - lost_s
        snap = [s for t, s in self.history if t <= cut][-1]
        snap = copy.deepcopy(snap)
        self.store, self.alarms, self.pages, self.windows = snap["store"], snap["alarms"], snap["pages"], snap["windows"]
        self.history.append((self.now, self._state()))
        self.gen += 1


# -- the socket's requests (docs/native/capture-split.md 4.2 "Rule") -----------------------------------------------------

REQUEST_IDS, REQUEST_IDS_S = 256, 600.0
REASONS = ("stale", "expired", "unsupported", "forbidden", "parked", "taken", "held", "busy", "invalid", "confirm")


class Requests:
    """REFERENCE MODEL of capture's request rules: who may ask, an id applied once (the last 256 ids for 10 min on the
    boot clock: a repeat gets the first reply again and changes nothing, even after its deadline), a deadline on the
    boot clock, an op it does not know. A refusal has a `reason` word and an `error` text. `apply` is the op itself:
    (op, args) -> (ok, reason, error, confirm, result), or None for an op it does not know."""

    def __init__(self, apply):
        self.apply = apply
        self.seen: dict[str, tuple[float, dict]] = {}
        self.applied: list[tuple[str, str]] = []

    def handle(self, req: dict, *, role: str, uid: int, boottime: float, wall: float) -> dict:
        def reply(ok, reason="", error="", confirm=False, result=None):
            out = {"v": 1, "t": "reply", "id": req["id"], "ok": ok, "at": wall}
            out.update({} if ok else {"reason": reason, "error": error, "confirm": confirm})
            return out if result is None else dict(out, result=result)

        def run():
            if outside(boottime, req["expires_boot"]):        # at or after the deadline: answered, never applied
                return reply(False, "expired", "answered too late: not applied")
            got = self.apply(req["op"], req.get("args") or {})
            if got is None:
                return reply(False, "unsupported", "this capture does not know that request")
            ok, reason, error, confirm, result = got
            if ok:
                self.applied.append((req["id"], req["op"]))
            return reply(ok, reason, error, confirm, result)
        self.seen = {k: v for k, v in list(self.seen.items())[-REQUEST_IDS:] if not outside(boottime - v[0], REQUEST_IDS_S)}
        if req["op"] == "credentials":                        # uid 0 only, whatever the role; never kept in the memory
            return run() if uid == 0 else reply(False, "forbidden", "not allowed")
        if role != "reader":
            return reply(False, "forbidden", "not allowed")
        if req["id"] in self.seen:
            return dict(self.seen[req["id"]][1], at=wall)
        out = run()
        self.seen[req["id"]] = (boottime, out)
        return out
