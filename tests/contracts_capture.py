"""The capture-split contract's reference aids (contracts/README.md "Capture split (designed)"; the design is
docs/native/capture-split.md, #773). Nothing of the split is implemented yet, so what the schemas cannot say is written
here as small pure functions and models that the tests run:

    check_spool, Spool        the spool directory's rules (contracts/capture/spool.md) and a writer that keeps them
    frame, unframe            the live socket's framing (contracts/capture/socket.md)
    age, is_late, ...         which clock an age is taken from
    Hub, Lane, P25Bits        the urgent lane's idempotency, as a model of the hub's dedupe and of capture's sender
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
         "capture-wish", "capture-status")


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
    then = [b["then"] for b in doc.get("allOf", [])
            if "if" in b and Draft202012Validator(b["if"], registry=reg).is_valid(inst)]
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


# -- clocks (docs/native/capture-split.md 4.1 "Late", 4.3 "Clocks") ----------------------------------------------------

LIVE_MAX_AGE_S = 60.0        # a spool item read older than this is `late`
PAGE_ITEM_MAX_AGE_S = 120.0  # a page item unsent this long is not sent
STATUS_STALE_S = 15.0        # a status file older than this, on the reader's boot, is a capture that is down


def age(event_at: float, event_boot: float, boot: str, *, wall: float, boottime: float, boot_now: str) -> tuple[float, str]:
    """(age_s, age_src) of an urgent item at the moment of sending. Within one boot: the boot clock, and only it (a
    wall clock that stepped must not make a fresh alarm look old). From before a reboot the boot clock says nothing:
    the wall-clock age, clamped at 0. Never null, and the item is never held back for want of a good clock."""
    if boot == boot_now:
        return max(0.0, boottime - event_boot), "boot"
    return max(0.0, wall - event_at), "wall"


def larger_age(at: float, at_boot: float, boot: str, *, wall: float, boottime: float, boot_now: str) -> float:
    """The age for `late` and for a page item's 120 s: the LARGER of the boot-clock age and the wall-clock age (either
    clock can under-count, and for these the safe side is "old"). Across boots only the wall age exists, and a
    negative one counts as old."""
    wall_age = wall - at
    if boot != boot_now:
        return float("inf") if wall_age < 0 else wall_age
    return max(boottime - at_boot, wall_age)


def is_late(sidecar: dict, *, wall: float, boottime: float, boot_now: str) -> bool:
    """Is a spool item `late` at the app's read: by age and nothing else. (An emergency in it is never gated by this:
    its alarm went by the urgent lane, and the late copy only attaches to it.)"""
    return larger_age(sidecar["end"], sidecar["closed_boot"], sidecar["boot"], wall=wall, boottime=boottime,
                      boot_now=boot_now) >= LIVE_MAX_AGE_S


def capture_down(status: dict, *, boottime: float, boot_now: str) -> bool:
    """Does a reader take capture as down from its status file. On the same boot: `at_boot` more than 15 s behind the
    reader's CLOCK_BOOTTIME. A status of another boot was written by a capture that boot ended."""
    return status["boot"] != boot_now or boottime - status["at_boot"] > STATUS_STALE_S


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


def segment_id(label: str, start: float) -> str:
    """A transmission's id, today's clip id: the label without spaces, a dash, the start in whole milliseconds."""
    return f"{label.replace(' ', '')}-{int(start * 1000)}"


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


def recover_seq(root: Path) -> int:
    """The next sequence number after a start: max(SEQ, the highest in items/) + 1. Every file in items/ counts,
    debris too, so a number that was ever on disk is never given out again."""
    try:
        last = int((root / "SEQ").read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        last = 0
    names = [ANY_NAME.fullmatch(p.name) for p in (root / "items").iterdir()] if (root / "items").is_dir() else []
    return max([last, *(int(m[1]) for m in names if m)]) + 1


def check_spool(root: Path, settled: bool = True) -> list[str]:
    """Every rule of contracts/capture/spool.md the directory breaks, as `rule: detail` lines ([] for none).

    settled: nothing is being published (capture has just started and cleaned up, or has stopped). Then tmp/ is
    empty and items/ holds nothing but complete items. While capture runs (settled=False) tmp/ may hold writes in
    progress and items/ the files of ONE item whose sidecar is not renamed yet: the newest."""
    out: list[str] = []
    root = Path(root)
    for d in ("tmp", "items"):
        if not (root / d).is_dir():
            out.append(f"layout: no {d}/")
    try:
        epoch = (root / "EPOCH").read_text(encoding="ascii").strip()
    except OSError:
        epoch = ""
    if not re.fullmatch(r"[0-9a-f]{16}", epoch):
        out.append("epoch: EPOCH is not 16 hex characters")
    if out:
        return out
    if settled and any((root / "tmp").iterdir()):
        out.append("tmp: writes in progress left in tmp/")
    sides: dict[int, dict] = {}
    owned: dict[str, int] = {}                      # file name in items/ -> the seq of the item it belongs to
    ids: dict[str, int] = {}
    files = {p.name: p for p in (root / "items").iterdir()}
    for name in sorted(n for n in files if n.endswith(".json")):
        m = ITEM_NAME.fullmatch(name)
        if m is None:
            out.append(f"name: {name} is not <seq, 12 digits>-<id>.json")
            continue
        seq = int(m[1])
        try:
            side = c.loads_strict(files[name].read_text(encoding="utf-8"))
        except ValueError:
            out.append(f"sidecar: {name} is not JSON (a sidecar is written whole under tmp/ and renamed)")
            continue
        owned[name] = seq
        errs = c.errors("capture-spool-sidecar", side)
        if errs:
            out.append(f"sidecar: {name}: {errs[0]}")
            continue
        if side["seq"] != seq or side["id"] != m[2]:
            out.append(f"name: {name} holds seq {side['seq']} id {side['id']}")
        if side["spool_epoch"] != epoch:
            out.append(f"epoch: {name} is of spool {side['spool_epoch']}, not {epoch}")
        if side["id"] in ids:
            out.append(f"id: {side['id']} is on the spool twice (seq {ids[side['id']]} and {seq})")
        ids[side["id"]] = seq
        sides[seq] = side
        audio = side.get("audio")
        if audio is not None:
            want = item_name(seq, side["id"], ".wav")
            owned[audio["file"]] = seq
            if audio["file"] != want:
                out.append(f"name: {name} names its audio {audio['file']}, not {want}")
            elif want not in files:
                out.append(f"audio: {name} is complete and its audio {want} is not there (audio is renamed first)")
            elif files[want].stat().st_size != audio["bytes"]:
                out.append(f"audio-size: {want} is {files[want].stat().st_size} bytes, the sidecar says {audio['bytes']}")
            elif side["kind"] == "segment":
                out += _check_wav(files[want], audio)
        for key, suffix in (("bursts_audio", ".mdc.wav"), ("tr", ".tr")):
            if side.get(key):
                owned[side[key]] = seq
                if side[key] != item_name(seq, side["id"], suffix):
                    out.append(f"name: {name} names its {key} {side[key]}")
                elif side[key] not in files:
                    out.append(f"audio: {name} is complete and its {key} file is not there")
    last: dict[str, tuple[float, int]] = {}
    for seq in sorted(sides):                       # per channel, seq rises in the order the transmissions ended
        key = _order_key(sides[seq])
        if key is None:
            continue
        end = sides[seq]["end"]
        if key in last and end < last[key][0]:
            out.append(f"order: seq {seq} on {key[3:]} ended before seq {last[key][1]}")
        last[key] = (end, seq)
    stray: dict[int, list[str]] = {}
    for name in sorted(set(files) - set(owned)):
        m = ANY_NAME.fullmatch(name)
        if m is None:
            out.append(f"name: {name} does not belong in items/")
        else:
            stray.setdefault(int(m[1]), []).append(name)
    newest = max(sides, default=0)
    for seq, names in sorted(stray.items()):
        if seq in sides:
            out.append(f"debris: {', '.join(names)}: no sidecar names it")
        elif settled or seq < newest or len([s for s in stray if s not in sides]) > 1:
            out.append(f"debris: {', '.join(names)} has no sidecar (a crash's leftovers: capture removes them at start)")
    cur = root / "cursor.app.json"
    if cur.exists():
        try:
            doc = c.loads_strict(cur.read_text(encoding="utf-8"))
        except ValueError:
            doc = None
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
    """The writer was stopped at a named point (Spool.publish(stop=...))."""


class Spool:
    """A writer that keeps the spool's rules, for the tests' fixtures: a reference for the ORDER of the steps, not a
    spool implementation (no bounds, no sync, no pruning by age). stop= ends a publish at a point a crash could:
    `tmp` (everything written under tmp/, nothing renamed), `audio` (the audio renamed into items/, the sidecar not),
    `sidecar` (the sidecar renamed, SEQ not yet rewritten)."""

    def __init__(self, root: Path, epoch: str = "3f9a1c0b7d2e4a61"):
        self.root = Path(root)
        for d in ("tmp", "items", "quarantine"):
            (self.root / d).mkdir(parents=True, exist_ok=True)
        if not (self.root / "EPOCH").exists():
            self._replace("EPOCH", epoch + "\n")          # made with the directory, never changed after
        self.epoch = (self.root / "EPOCH").read_text(encoding="ascii").strip()
        self.start()

    def _replace(self, name: str, text: str) -> None:
        tmp = self.root / "tmp" / f"{name}.new"
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, self.root / name)

    def start(self) -> None:
        """What capture does at start: wipe tmp/, recover SEQ, remove what has no sidecar."""
        for p in (self.root / "tmp").iterdir():
            p.unlink()
        self.next = recover_seq(self.root)
        items = self.root / "items"
        done = {m[1] for p in items.iterdir() if (m := ITEM_NAME.fullmatch(p.name))}
        for p in items.iterdir():
            m = ANY_NAME.fullmatch(p.name)
            if m is None or m[1] not in done:
                p.unlink()
        self._replace("SEQ", f"{self.next - 1}\n")

    def publish(self, side: dict, audio: bytes | None = None, bursts: bytes | None = None, stop: str = "") -> dict:
        """Publish one item: its files under tmp/, then (under the publish lock, here the one caller) the next seq,
        the audio renamed into items/, the sidecar written and renamed LAST, SEQ rewritten."""
        tmp, items = self.root / "tmp", self.root / "items"
        if audio is not None:
            (tmp / "audio").write_bytes(audio)
        if bursts is not None:
            (tmp / "bursts").write_bytes(bursts)
        if stop == "tmp":
            raise Crash(stop)
        seq, self.next = self.next, self.next + 1
        side = dict(copy.deepcopy(side), seq=seq, spool_epoch=self.epoch)
        if audio is not None:
            side["audio"] = dict(side["audio"], file=item_name(seq, side["id"], ".wav"), bytes=len(audio))
            os.replace(tmp / "audio", items / side["audio"]["file"])
        if bursts is not None:
            side["bursts_audio"] = item_name(seq, side["id"], ".mdc.wav")
            os.replace(tmp / "bursts", items / side["bursts_audio"])
        if stop == "audio":
            raise Crash(stop)
        (tmp / "sidecar").write_text(json.dumps(side, allow_nan=False), encoding="utf-8")
        os.replace(tmp / "sidecar", items / item_name(seq, side["id"], ".json"))      # the completion marker
        if stop == "sidecar":
            raise Crash(stop)
        self._replace("SEQ", f"{seq}\n")
        return side

    def drop_audio(self, seq: int) -> None:
        """A bound was reached: the sidecar is rewritten first (`audio` null, `audio_dropped`), then the audio goes."""
        items = self.root / "items"
        path = next(p for p in items.iterdir() if (m := ITEM_NAME.fullmatch(p.name)) and int(m[1]) == seq)
        side = json.loads(path.read_text(encoding="utf-8"))
        gone = [side["audio"]["file"], side.get("bursts_audio")]
        side.update(audio=None, bursts_audio=None, audio_dropped=True)
        (self.root / "tmp" / "sidecar").write_text(json.dumps(side), encoding="utf-8")
        os.replace(self.root / "tmp" / "sidecar", path)
        for name in filter(None, gone):
            (items / name).unlink()

    def prune(self, cursor: dict) -> list[int]:
        """Capture deletes what the reader's cursor says is done (the reader never deletes): the sidecar first, so
        that no sidecar ever names audio that is gone."""
        if cursor.get("spool_epoch") != self.epoch:
            return []
        done = []
        items = self.root / "items"
        for p in sorted(items.glob("*.json")):
            m = ITEM_NAME.fullmatch(p.name)
            if m and (int(m[1]) <= cursor["seq"] or int(m[1]) in cursor.get("done", [])):
                p.unlink()
                for other in items.glob(f"{m[1]}-*"):
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

RESEND_WINDOW_S = 120.0      # a changed hub generation: every alarm marked sent within this is sent again
REQUEST_MAX_ITEMS, REQUEST_MAX_BYTES = 32, 64 * 1024
SENT = ("applied", "duplicate", "stale")


class Lane:
    """REFERENCE MODEL of capture's half of the urgent lane: the items in memory, the rows of the urgent file, and what
    the sender does with the hub's answers. Time is passed in (wall, boottime): there is no clock here. `disk`:
    `ok` | `full` (inserts fail) | `stalled` (inserts neither fail nor return); a model's `tick` writes what it can."""

    def __init__(self, station: str = "st-north", boot: str = "boot-1", epoch: str = "9c2d7e10a4b35f68"):
        self.station, self.boot, self.epoch = station, boot, epoch
        self.rows: dict[tuple[str, int], dict] = {}        # the file: survives a crash of capture
        self.memory: dict[tuple[str, int], dict] = {}      # the senders' items: lost with the process
        self.answers: dict[tuple[str, int], dict] = {}     # the hub's word per item, kept by the sender
        self.disk, self.n, self.gen = "ok", 0, None

    def press(self, key: str, kind: str, data_: dict, *, wall: float, boottime: float, rev: int = 0,
              event_at: float | None = None, event_boot: float | None = None) -> dict:
        """An item is made: handed to the sender in memory at once, and to the writer."""
        item = {"key": key, "rev": rev, "kind": kind, "at": wall, "event_at": wall if event_at is None else event_at,
                "event_boot": boottime if event_boot is None else event_boot, "boot": self.boot, "data": data_}
        self.memory[(key, rev)] = item
        self.tick()
        return item

    def tick(self) -> None:
        """The writer: INSERT OR IGNORE on (key, rev) for every item in memory that has no row, while the disk takes
        it. A row whose answer came first is written as sent, never left `new`."""
        if self.disk != "ok":
            return
        for ident, item in self.memory.items():
            if ident not in self.rows:
                self.n += 1
                got = self.answers.get(ident)
                self.rows[ident] = dict(item, n=self.n, state=got["state"] if got else "new",
                                        hub=got["hub"] if got else None, hub_gen=got["gen"] if got else None,
                                        sent_boot=got["boot"] if got else None)

    def frames(self) -> list[dict]:
        """The items as the app sees them (`urgent` frames, `urgent_since`): `durable` false and `n` null without a row."""
        out = []
        for ident, item in self.memory.items():
            row = self.rows.get(ident)
            out.append(dict(item, n=row["n"] if row else None, durable=row is not None))
        return out

    def _state(self, ident) -> str:
        if ident in self.answers:
            return self.answers[ident]["state"]
        return self.rows[ident]["state"] if ident in self.rows else "new"

    def unsent(self) -> list[str]:
        """The alarms the hub has not answered for: the senders' own view (the status file's `urgent.unsent`)."""
        return [k for (k, r), it in self.memory.items() if it["kind"] == "alarm" and self._state((k, r)) == "new"]

    def request(self, kind: str, *, wall: float, boottime: float) -> dict | None:
        """The next request of one connection (`alarm` or `page`): None when it has nothing to send. Alarms in the
        order they were made, never expired by any clock. For a page only the highest unsent rev of each key, and
        none older than 120 s by the larger of its two ages: those become `expired`."""
        todo = [(i, it) for i, it in self.memory.items() if it["kind"] == kind and self._state(i) == "new"]
        if kind == "page":
            top = {}
            for ident, it in todo:
                old = larger_age(it["event_at"], it["event_boot"], it["boot"], wall=wall, boottime=boottime,
                                 boot_now=self.boot) > PAGE_ITEM_MAX_AGE_S
                if old or ident[1] < max(r for (k, r), _ in todo if k == ident[0]):
                    self._mark(ident, "expired", None, boottime)
                else:
                    top[ident] = it
            todo = list(top.items())
        items = []
        for _ident, it in todo[:REQUEST_MAX_ITEMS]:
            age_s, src = age(it["event_at"], it["event_boot"], it["boot"], wall=wall, boottime=boottime, boot_now=self.boot)
            items.append({"key": it["key"], "rev": it["rev"], "kind": it["kind"], "at": it["at"],
                          "event_at": it["event_at"], "age_s": round(age_s, 3), "age_src": src, "data": it["data"]})
        if not items:
            return None
        return {"v": 1, "station": self.station, "urgent_epoch": self.epoch, "sent_at": wall, "items": items}

    def _mark(self, ident, state: str, hub: str | None, boottime: float) -> None:
        self.answers[ident] = {"state": state, "hub": hub, "gen": self.gen, "boot": boottime}
        if ident in self.rows:
            self.rows[ident].update(state=state, hub=hub, hub_gen=self.gen, sent_boot=boottime)

    def answer(self, code: int, body: dict | None, *, boottime: float) -> None:
        """The hub's answer to the last request, or none at all (code 0: a timeout, the connection gone). Only a 200
        that names an item `applied`, `duplicate` or `stale` makes it sent; `rejected` ends it; anything else, an
        item the 200 does not name and a word this build does not know leave it to be sent again."""
        if code != 200 or not isinstance(body, dict):
            return
        if self.gen is not None and body.get("gen") != self.gen:       # the hub lost what it had answered for, or a
            for ident, got in self.answers.items():                    # handoff: the last 120 s of alarms go again
                if got["state"] == "sent" and self.memory[ident]["kind"] == "alarm" \
                        and boottime - got["boot"] <= RESEND_WINDOW_S:
                    got["state"] = "new"
                    if ident in self.rows:
                        self.rows[ident]["state"] = "new"
        self.gen = body.get("gen")
        for res in body.get("results", []):
            ident = (res.get("key"), res.get("rev"))
            if ident in self.memory and self._state(ident) == "new":
                if res.get("status") in SENT:
                    self._mark(ident, "sent", res["status"], boottime)
                elif res.get("status") == "rejected":
                    self._mark(ident, "rejected", "rejected", boottime)

    def crash(self) -> None:
        """Capture is killed: what was only in memory is gone."""
        self.memory, self.answers = {}, {}

    def start(self, *, wall: float, boottime: float, boot: str | None = None) -> None:
        """Capture starts (after a reboot: another boot id): every `new` row goes back to the senders, the oldest
        alarm first; a page item only if it is under 120 s old. A row marked sent is not sent again."""
        self.boot = boot or self.boot
        self.memory, self.answers = {}, {}
        for ident, row in sorted(self.rows.items(), key=lambda x: (x[1]["kind"] != "alarm", x[1]["n"])):
            if row["state"] != "new":
                continue
            if row["kind"] == "page" and larger_age(row["event_at"], row["event_boot"], row["boot"], wall=wall,
                                                    boottime=boottime, boot_now=self.boot) > PAGE_ITEM_MAX_AGE_S:
                row["state"] = "expired"
                continue
            self.memory[ident] = {k: row[k] for k in ("key", "rev", "kind", "at", "event_at", "event_boot", "boot", "data")}


class P25Bits:
    """REFERENCE MODEL of #775's transition rule, capture's side: a live P25 emergency bit raises an alarm only on a
    transition: the bit set for a (system, radio) that has not had it set within the last 10 min. Every sighting
    moves the last-set time on, so a stuck bit never re-alarms; the times are kept in the urgent file, so capture's
    own restart does not re-alarm either."""

    def __init__(self, quiet_s: float = 600.0):
        self.quiet_s = quiet_s
        self.last_set: dict[tuple[str, str], float] = {}          # the p25_bit table
        self.transitions: list[float] = []

    def seen(self, system: str, radio: str, at: float) -> bool:
        """The bit is set on a grant at `at`: is this a new press (an alarm), or the same one still set."""
        last = self.last_set.get((system, radio))
        self.last_set[(system, radio)] = at
        if last is None or at - last > self.quiet_s:
            self.transitions.append(at)
            return True
        return False

    def per_hour(self, now: float) -> int:
        return sum(1 for t in self.transitions if now - t <= 3600.0)


# -- the urgent lane: the hub's side --------------------------------------------------------------------------------------

class Hub:
    """REFERENCE MODEL of the hub's dedupe for `POST /api/station/urgent` (docs/native/capture-split.md 4.3
    "Idempotency, on the hub"; 11 H1b to H1e, H1i). NOT the hub: no tokens, no storage, no rig, no alarm levels. It
    holds only what decides the answer for an item and whether an alarm is raised and pushed:

    - `store`, keyed by (station, key), written by BOTH lanes and asked before anything is raised;
    - the alarms, each with the `traffic_id` it hangs on, which the finished transmission takes over;
    - the live pages, by (station, key), with the highest rev taken.

    max_age_s is A7: the admin has NOT decided what a late emergency becomes. 1800 (30 min, `alarms.MAX_AGE_S`) is
    the recommended rule and only a default here: up to it an item is `applied` (an alarm and a push that says its
    age), past it `stale` (a late notice, no push). The clock is `now`, set by the caller."""

    def __init__(self, max_age_s: float = 1800.0, p25_slack_s: float = 10.0, repeat_s: float = 120.0,
                 station: str = "st-north", multi_station: bool = False):
        self.max_age_s, self.p25_slack_s, self.repeat_s = max_age_s, p25_slack_s, repeat_s
        self.station, self.multi_station = station, multi_station
        self.now, self.leader, self.gen = 0.0, True, 1
        self.store: dict[tuple[str, str], dict] = {}
        self.alarms: list[dict] = []
        self.pushes: list[dict] = []
        self.notices: list[dict] = []
        self.pages: dict[tuple[str, str], dict] = {}
        self.log: list[str] = []

    # .. the route ...................................................................................................
    def urgent(self, body: dict) -> tuple[int, dict]:
        """(status, answer) for one request. A hub that is not the leader applies nothing. A request that is not
        well formed is refused whole, and nothing of it is applied (the sender cuts it to single items)."""
        if not self.leader:
            return 503, {"error": "updating", "updating": True}
        raw = json.dumps(body, separators=(",", ":")).encode()
        if len(raw) > REQUEST_MAX_BYTES:
            return 413, {"error": "body over 65536 bytes", "reason": "too_large"}
        items = body.get("items") if isinstance(body, dict) else None
        ok = isinstance(items, list) and 1 <= len(items) <= REQUEST_MAX_ITEMS and all(
            isinstance(i, dict) and isinstance(i.get("key"), str) and 0 < len(i["key"]) <= 200
            and isinstance(i.get("rev"), int) and not isinstance(i["rev"], bool) and i["rev"] >= 0
            and isinstance(i.get("kind"), str) and isinstance(i.get("data"), dict) for i in items)
        if not ok or not isinstance(body.get("station"), str):
            return 422, {"error": "not a list of 1 to 32 items", "reason": "bad_batch"}
        station = body["station"]
        if station != self.station and not self.multi_station:
            return 409, {"error": "this hub takes one station", "reason": "multi_station_off"}
        results = []
        for item in items:
            status, reason = self._item(station, item)
            results.append({"key": item["key"], "rev": item["rev"], "status": status,
                            **({"reason": reason} if reason else {})})
        return 200, {"ok": True, "v": 1, "station": station, "gen": self.gen, "results": results}

    def _item(self, station: str, item: dict) -> tuple[str, str]:
        if item["kind"] == "page":
            return self._page(station, item), ""
        if item["kind"] != "alarm":
            return "rejected", "kind"
        k = (station, item["key"])
        if k in self.store:                                   # this lane again, or the app's lane was first
            return "duplicate", ""
        cur = next((a for a in self.alarms if a["station"] == station and a["traffic_id"] == item["key"]), None)
        if cur is not None:                                   # today's rule: the same traffic_id is never raised again
            self.store[k] = {"kind": "alarm", "rev": 0, "first": self.now, "alarm": cur["id"]}
            return "duplicate", ""
        age_s = item.get("age_s")
        age_s = min(max(float(age_s), 0.0), 86400.0) if isinstance(age_s, (int, float)) and not isinstance(age_s, bool) \
            and age_s == age_s else 0.0                       # not a number, negative or over 24 h: clamped
        d = item["data"]
        if d.get("emergency_via") == "P25":                   # the end-of-call alarm may already be there (Q-I)
            call = self._p25_match(station, d, self.now - age_s)
            if call is not None:
                self.store[k] = {"kind": "alarm", "rev": 0, "first": self.now, "alarm": call["id"]}
                return "duplicate", ""
        if age_s > self.max_age_s:                            # A7, recommended: a late notice, no alarm, no push
            self.store[k] = {"kind": "alarm", "rev": 0, "first": self.now, "alarm": None}
            self.notices.append({"station": station, "key": item["key"], "age_s": age_s})
            return "stale", ""
        a = self._raise(station, item["key"], d, at=self.now - age_s, age_s=age_s, live_p25=d.get("emergency_via") == "P25")
        self.store[k] = {"kind": "alarm", "rev": 0, "first": self.now, "alarm": a["id"]}     # the route records the key
        return "applied", ""

    def _raise(self, station: str, tid: str, d: dict, *, at: float, age_s: float, live_p25: bool = False,
               span: tuple[float, float] | None = None) -> dict:
        """The alarm code, cut down to what the lane touches: the same radio's button again on the same channel while
        its alarm is unacknowledged and under repeat_s old joins that alarm (no second alarm, no second push)."""
        rid, src = str(d.get("radio_id") or ""), str(d.get("source") or "")
        for a in reversed(self.alarms):
            if rid and a["station"] == station and a["radio_id"] == rid and a["channel"] == src and not a["acked"] \
                    and self.now - a["created"] <= self.repeat_s:
                return a
        a = {"id": len(self.alarms) + 1, "station": station, "traffic_id": tid, "at": at, "created": self.now,
             "radio_id": rid, "channel": src, "acked": False, "system": d.get("system"), "talkgroup": d.get("talkgroup"),
             "live_p25": live_p25, "span": span}
        self.alarms.append(a)
        self.pushes.append({"alarm": a["id"], "at": at, "age_s": age_s})
        return a

    def _p25_match(self, station: str, d: dict, at: float) -> dict | None:
        """The finished call's alarm a live grant belongs to: system + talkgroup + unit, the grant inside the call's
        span with the slack at each end."""
        for a in self.alarms:
            if a["station"] == station and a["span"] and (a["system"], a["talkgroup"], a["radio_id"]) == \
                    (d.get("system"), d.get("talkgroup"), str(d.get("radio_id") or "")) \
                    and a["span"][0] - self.p25_slack_s <= at <= a["span"][1] + self.p25_slack_s:
                return a
        return None

    def _page(self, station: str, item: dict) -> str:
        k = (station, item["key"])
        have = self.store.get(k)
        if have is not None and item["rev"] <= have["rev"]:
            return "duplicate" if item["rev"] == have["rev"] else "stale"
        self.store[k] = {"kind": "page", "rev": item["rev"], "first": have["first"] if have else self.now, "alarm": None}
        self.pages[k] = {"rev": item["rev"], "data": copy.deepcopy(item["data"]), "from": "urgent", "ended": bool(
            item["data"].get("ended"))}
        return "applied"

    # .. the app's lane: /api/station/events and /api/station/state ..................................................
    def event(self, station: str, ev: dict) -> int:
        """One event of the app's lane: `alarm` (the take-over, `id` = the urgent key; or a finished P25 call's
        alarm), `traffic` (a finished transmission) or `page` (a finished page). The lane records the `id` of every
        alarm event and every `alarm_key` it sees in the dedupe store, and asks the store before it raises."""
        if not self.leader:
            return 503
        tid, key = str(ev.get("id") or ""), str(ev.get("alarm_key") or "")
        if ev.get("type") == "page":
            page = self.pages.get((station, str(ev.get("live_key") or "")))
            if page is not None:
                page["ended"] = True                          # the finished page ends the live entry at once
            return 200
        emergency = ev.get("type") == "alarm" or bool(ev.get("emergency"))
        cur = next((a for a in self.alarms if a["station"] == station and a["traffic_id"] == tid), None)
        if cur is None and key and key != tid:           # raised at the key-up: the finished transmission takes it over
            cur = next((a for a in self.alarms if a["station"] == station and a["traffic_id"] == key), None)
            if cur is not None:
                cur["traffic_id"] = tid
        known = [self.store[k] for k in ((station, tid), (station, key)) if k[1] and k in self.store]
        # what this lane records: the id of an `alarm` event, and an `alarm_key` on an alarm or a traffic event
        mine = [(station, x) for x in ([tid] if ev.get("type") == "alarm" else []) + ([key] if key else [])]
        for k in mine:
            self.store.setdefault(k, {"kind": "alarm", "rev": 0, "first": self.now, "alarm": cur["id"] if cur else None})
        if cur is not None or not emergency:
            return 200
        if known:                                             # the urgent lane had it (even as stale): nothing new
            return 200
        span = (float(ev["start"]), float(ev["end"])) if ev.get("system") and "start" in ev and "end" in ev else None
        if span is not None:                                  # a finished P25 call: its live alarm, if one was raised
            rid = str(ev.get("radio_id") or "")
            for a in self.alarms:
                if a["station"] == station and a["live_p25"] and (a["system"], a["talkgroup"], a["radio_id"]) == \
                        (ev.get("system"), ev.get("talkgroup"), rid) \
                        and span[0] - self.p25_slack_s <= a["at"] <= span[1] + self.p25_slack_s:
                    a.update(traffic_id=tid, span=span, live_p25=False)       # the end-of-call row takes it over
                    for k in mine:
                        self.store[k]["alarm"] = a["id"]
                    return 200
        end = float(ev.get("end", ev.get("start", self.now)))
        if self.now - end > self.max_age_s:
            self.log.append(f"{tid}: too old to raise now (logged only)")
            return 200
        a = self._raise(station, tid, ev, at=float(ev.get("start", self.now)), age_s=max(0.0, self.now - end), span=span)
        for k in mine:
            self.store[k]["alarm"] = a["id"]
        return 200

    def state(self, station: str, paging: list[dict]) -> None:
        """The app's state post, its `paging` list: merged with the urgent lane's entries by `key`, the newer wins."""
        for entry in paging:
            self.pages[(station, entry["key"])] = {"rev": self.store.get((station, entry["key"]), {}).get("rev"),
                                                   "data": copy.deepcopy(entry), "from": "state",
                                                   "ended": bool(entry.get("ended"))}

    def ack(self, traffic_id: str) -> None:
        next(a for a in self.alarms if a["traffic_id"] == traffic_id)["acked"] = True

    # .. the hub itself ..............................................................................................
    def handoff(self) -> None:
        """A deploy: this task stops leading. Every write is answered 503 until lead()."""
        self.leader = False

    def lead(self) -> None:
        self.leader, self.gen = True, self.gen + 1

    def restore(self, lost_s: float) -> None:
        """A crash, and back from a replica `lost_s` old: the newest writes are gone, and the generation is new."""
        cut = self.now - lost_s
        self.store = {k: v for k, v in self.store.items() if v["first"] <= cut}
        self.alarms = [a for a in self.alarms if a["created"] <= cut]
        self.pages = {k: v for k, v in self.pages.items() if k in self.store or v["from"] == "state"}
        self.gen += 1


# -- the socket's requests (docs/native/capture-split.md 4.2 "Rule") -----------------------------------------------------

REQUEST_IDS, REQUEST_IDS_S = 256, 600.0


class Requests:
    """REFERENCE MODEL of capture's request rules: who may ask, an id applied once (the last 256 ids for 10 min: a repeat
    gets the first reply again and changes nothing), a deadline on the boot clock, an op it does not know. `apply` is
    the op itself: (op, args) -> (ok, error, confirm, result)."""

    def __init__(self, apply):
        self.apply = apply
        self.seen: dict[str, tuple[float, dict]] = {}
        self.applied: list[tuple[str, str]] = []

    def handle(self, req: dict, *, role: str, uid: int, boottime: float, wall: float) -> dict:
        def reply(ok, error="", confirm=False, result=None):
            out = {"v": 1, "t": "reply", "id": req["id"], "ok": ok, "at": wall}
            out.update({} if ok else {"error": error, "confirm": confirm})
            return out if result is None else dict(out, result=result)
        self.seen = {k: v for k, v in list(self.seen.items())[-REQUEST_IDS:] if boottime - v[0] <= REQUEST_IDS_S}
        if req["op"] == "credentials":                        # uid 0 only; never kept, never replayed
            return reply(True) if uid == 0 else reply(False, "forbidden")
        if role != "reader":
            return reply(False, "forbidden")
        if req["id"] in self.seen:
            return dict(self.seen[req["id"]][1], at=wall)
        if boottime > req["expires_boot"]:
            out = reply(False, "expired")
        else:
            got = self.apply(req["op"], req.get("args") or {})
            if got is None:
                out = reply(False, "unsupported")
            else:
                ok, error, confirm, result = got
                out = reply(ok, error, confirm, result)
                if ok:
                    self.applied.append((req["id"], req["op"]))
        self.seen[req["id"]] = (boottime, out)
        return out
