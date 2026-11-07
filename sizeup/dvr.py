"""Camera DVR (#277): the live cameras' HLS segments kept on the hub's disk for a rolling window, so the viewer can
scrub back. Copies, no re-encoding: each segment as the hub serves it live (checked whole and repaired, #276,
sizeup/hlsseg.py), one file per segment.

What is recorded, on demand only (Dvr.wanted, at most max_cameras at once, in this order):
  always    [roads] dvr_always: camera ids recorded all the time
  incident  an open CAD event's cameras: the ones just before / at / after it along the road (Roads.cameras_for)
            and any within dvr_near_m as the crow flies
  pinned    a dispatcher's "Keep" in the viewer (POST /api/cad/cameras/<id>/dvr/pin): PIN_S (24 h) or until unpinned
            (kept in the CAD store's meta, so a deploy keeps the pins)
  open      a camera someone is watching live, and for linger_s after the last viewer closed
Only cameras with live video; a snapshot-only camera (the county's) has nothing to copy.
A camera listed under another id now (#320, Roads.alias: its records merged and another one kept): its Keep and its
recording go with it (follow), and its old id's paths find it.

The window: window_s (default 2 h) per camera; older segments go. And a size cap over every camera (max_bytes,
default 6 GB) and a floor of free disk (FREE_FLOOR_BYTES), both made room for before each write: the oldest go
first, whichever camera; when nothing more can go and the floor still isn't met, recording stops and says so
(status "stopped") until there is room again. Measured
2026-11-07 on the 511NY cameras: about 200 kbit/s, so 175 MB per camera per 2 h; 12 cameras at once, about 2.1 GB.

Where a segment came from: the camera's own HLS session (fetched, checked and repaired here, never in the live
viewers' cache), or the live stream's when one is open (Roads._streams: the same segments, fetched once). A few
workers record the cameras side by side. Segments are keyed by the stream's media sequence (Wowza's counter runs on
across sessions), so neither a new session nor the two sources ever double one; a counter that starts over
(the origin restarted) is carried on from the last key. A chunklist that fails is asked again at once with a new
session; only after repeated failures does the camera wait (15 s doubling to SESSION_RETRY_S). Its time: when it first appears in the
chunklist (Wowza lists a segment once it is whole), less the durations of those after it, carried on from the one
before when they follow each other.

HTTP (behind Cloudflare Access with the rest of /api/cad; nothing here is public, no share links):
  GET  /api/cad/cameras/<id>/dvr                   {recording, why: [always | pinned | incident | open], pinned,
                                                    from, to, segments, bytes, window_s, gaps: [[from, to], ...]}
  GET  /api/cad/cameras/<id>/dvr/index.m3u8         the recording as a VOD playlist: EXT-X-PROGRAM-DATE-TIME on every
                                                    segment (the viewer's timestamp), EXT-X-DISCONTINUITY at a gap and
                                                    at a new stream (another stream, the origin restarted)
  GET  /api/cad/cameras/<id>/dvr/<seq>.ts?s=<ms>    a recorded segment (s: its start, as the playlist lists it; another
                                                    segment under the same key now: 404)
  POST /api/cad/cameras/<id>/dvr/pin {on}           (sizeup/cad/api.py, dispatchers and admins) keep recording / stop

A deploy (sizeup/handoff.py): the recordings are on the task's ephemeral disk, so a new task starts with none. The old
task records until it drains; the new one starts recording when its app serves (TAKEOVER): the window restarts
empty and there's a gap of the handoff's length. The pins are in the CAD store (Litestream): they carry over.
"""

from __future__ import annotations

import json
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs

from sizeup import hlsseg

WINDOW_S = 7200.0
MAX_BYTES = 6 * 1024 ** 3
FREE_FLOOR_BYTES = 3 * 1024 ** 3     # never leave less free on the disk (the SQLite files, their standby copies)
NEAR_M = 1000.0
LINGER_S = 1800.0                    # an opened camera records on this long after its last viewer closed
PIN_S = 24 * 3600.0                  # a Keep lasts this long unless renewed
MAX_CAMERAS = 12
WORKERS = 8                          # cameras recorded side by side (measured: a segment takes 1-2 s from the origin)
TICK_S = 4.0                         # each recorded camera's chunklist: this often (a viewer's player: about the same)
GAP_S = 1.5                          # segments further apart than this: a discontinuity in the playlist
SESSION_RETRY_S = 300.0              # a camera whose stream keeps failing: asked at most this seldom
FAILS_BEFORE_WAIT = 3
SEQ_RE = re.compile(r"_(\d{1,12})\.ts$")
FILE_RE = re.compile(r"(\d{1,15})_(\d{10,16})_(\d{1,9})(_n)?\.ts")   # key, start (ms), duration (ms), _n: a new stream
CAM_ID = r"[A-Za-z0-9_][A-Za-z0-9_.:-]{0,63}"                      # never "." or "..": a folder name
CAM_RE = re.compile(CAM_ID)
DVR_PATH = re.compile(rf"/api/cad/cameras/({CAM_ID})/dvr(?:/(index\.m3u8|\d{{1,15}}\.ts))?")
JSON = "application/json"
REASONS = ("always", "incident", "pinned", "open")


def _chunklist(text: str) -> tuple[int, list[tuple[str, float]]]:
    """(media sequence of the first, [(name, duration), ...]) of a media playlist."""
    seq, out, dur = 0, [], 0.0
    for t in text.splitlines():
        t = t.strip()
        if t.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                seq = int(t.split(":", 1)[1])
            except ValueError:
                seq = 0
        elif t.startswith("#EXTINF:"):
            try:
                dur = float(t[8:].split(",")[0])
            except ValueError:
                dur = 0.0
        elif t and not t.startswith("#") and t.endswith(".ts"):
            out.append((t, dur))
            dur = 0.0
    return seq, out


class Dvr:
    def __init__(self, roads, root: str | Path, window_s: float = WINDOW_S, max_bytes: int = MAX_BYTES,
                 near_m: float = NEAR_M, always: tuple[str, ...] = (), linger_s: float = LINGER_S,
                 max_cameras: int = MAX_CAMERAS, clock: Callable[[], float] = time.time, log: Callable[[str], None] = print,
                 free_floor: int = FREE_FLOOR_BYTES, workers: int = WORKERS):
        self.roads, self.root = roads, Path(root)
        self.window_s, self.max_bytes, self.near_m = float(window_s), int(max_bytes), float(near_m)
        self.always = tuple(a for a in always if CAM_RE.fullmatch(a))
        self.linger_s, self.max_cameras = float(linger_s), int(max_cameras)
        self.clock, self.log, self.free_floor, self.workers = clock, log, int(free_floor), max(1, int(workers))
        # the app hands in: open CAD events [{lat, lon, address, label}], and where the pins are kept
        self.incidents: Callable[[], list[dict]] = list
        self.load_pins: Callable[[], object] = list
        self.save_pins: Callable[[dict], None] = lambda pins: None
        self.pins: dict[str, float] = {}          # cid -> when it was pinned
        # a recording's folder renamed to the id its camera is kept under now (follow, #320): only when this says so
        self.may_move: Callable[[str, str], bool] = lambda old, new: True
        self._lock = threading.RLock()            # _cams, _opened, _wanted, pins
        self._disk = threading.RLock()            # making room and writing: one at a time
        self._cams: dict[str, dict] = {}          # cid -> {segs: {key: (t0, dur, bytes)}, last, end, offset, chunk, ...}
        self._opened: dict[str, float] = {}       # cid -> last time someone watched it live
        self._wanted: dict[str, list[str]] = {}
        self.stopped = ""                         # why recording stopped (the disk), "" when it records
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pool: ThreadPoolExecutor | None = None
        self._busy: set[str] = set()              # cameras with a round under way (pool mode)
        self._next: dict[str, float] = {}         # cid -> when its next round is due
        self._scan()

    # -- the disk ----------------------------------------------------------------------------------------------------

    def _dir(self, cid: str) -> Path:
        if not CAM_RE.fullmatch(cid):
            raise ValueError(f"not a camera id: {cid!r}")
        return self.root / cid

    def _cam(self, cid: str) -> dict:
        with self._lock:
            return self._cams.setdefault(cid, {"segs": {}, "last": None, "end": None, "offset": 0, "breaks": set()})

    def _scan(self) -> None:
        """What is already on disk (the app restarted within the same task): the index from the file names; a write
        cut short (a .tmp) goes."""
        if not self.root.is_dir():
            return
        for d in self.root.iterdir():
            if not d.is_dir() or not CAM_RE.fullmatch(d.name):
                continue
            segs, breaks = {}, set()
            for f in d.iterdir():
                m = FILE_RE.fullmatch(f.name)
                if m:
                    segs[int(m.group(1))] = (int(m.group(2)) / 1000, int(m.group(3)) / 1000, f.stat().st_size)
                    if m.group(4):
                        breaks.add(int(m.group(1)))
                elif f.suffix == ".tmp":
                    f.unlink(missing_ok=True)
            if segs:
                last = max(segs)
                self._cams[d.name] = {"segs": segs, "last": last, "end": segs[last][0] + segs[last][1], "offset": 0,
                                      "breaks": breaks}

    def _file(self, cid: str, key: int) -> Path | None:
        with self._lock:
            c = self._cams.get(cid)
            s = c["segs"].get(key) if c else None
            brk = bool(c) and key in c.get("breaks", ())
        if not s:
            return None
        return self._dir(cid) / f"{key}_{int(s[0] * 1000)}_{int(s[1] * 1000)}{'_n' if brk else ''}.ts"

    def _free(self) -> int:
        """Free bytes on the recordings' disk; 0 when it can't be told (fail closed: no write)."""
        try:
            p = self.root
            while not p.exists() and p != p.parent:
                p = p.parent
            return shutil.disk_usage(p).free
        except OSError:
            return 0

    def _room(self, need: int) -> bool:
        """Room for need more bytes: under max_bytes, and the disk's free floor kept after it, the oldest going
        first (whichever camera) until there is. False when nothing more can go and there still isn't."""
        with self._disk:
            while True:
                if self.total_bytes() + need <= self.max_bytes and self._free() - need >= self.free_floor:
                    return True
                with self._lock:
                    every = [(s[0], cid, key) for cid, c in self._cams.items() for key, s in c["segs"].items()]
                if not every:
                    return False
                _, cid, key = min(every)
                self._remove(cid, key)

    def _write(self, cid: str, key: int, t0: float, dur: float, body: bytes, brk: bool = False) -> bool:
        """One segment onto the disk, whole or not at all (a temp file renamed; gone on any error), after making room
        for it. False when there is no room: recording stops (self.stopped) until there is. brk: the first of a new
        stream (another stream, a new session, the origin's counter started over): a discontinuity in the playlist,
        kept in its file name ("_n") so a restart knows it too."""
        with self._disk:
            if not self._room(len(body)):
                self._stopped(f"Disk full: under {self.free_floor / 1024 ** 3:.1f} GB free"
                              if self._free() - len(body) < self.free_floor else "Over the recording cap")
                return False
            d = self._dir(cid)
            f = d / f"{key}_{int(t0 * 1000)}_{int(dur * 1000)}{'_n' if brk else ''}.ts"
            tmp = f.with_suffix(".tmp")
            try:
                d.mkdir(parents=True, exist_ok=True)
                tmp.write_bytes(body)
                tmp.replace(f)
            except OSError as e:
                tmp.unlink(missing_ok=True)
                self.log(f"dvr: {cid}: not written ({type(e).__name__}: {e})")
                return False
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
            self._stopped("")
            c = self._cam(cid)
            with self._lock:
                c["segs"][key] = (t0, dur, len(body))
                if brk:
                    c.setdefault("breaks", set()).add(key)
                if c["last"] is None or key > c["last"]:
                    c["last"], c["end"] = key, t0 + dur
        return True

    def _stopped(self, why: str) -> None:
        if why != self.stopped:
            self.log(f"dvr: recording stopped: {why}" if why else "dvr: room again: recording")
            self.stopped = why
            self.roads._changed()

    def _remove(self, cid: str, key: int) -> int:
        f = self._file(cid, key)
        with self._lock:
            c = self._cams.get(cid)
            s = c["segs"].pop(key, None) if c else None
            if c:
                c.get("breaks", set()).discard(key)
        if f is not None:
            f.unlink(missing_ok=True)
        return s[2] if s else 0

    def total_bytes(self) -> int:
        with self._lock:
            return sum(s[2] for c in self._cams.values() for s in c["segs"].values())

    def evict(self) -> int:
        """Older than the window: gone; then over max_bytes or under the free floor: the oldest first (_room)."""
        now = self.clock()
        freed = 0
        with self._disk:
            with self._lock:
                old = [(cid, key) for cid, c in self._cams.items() for key, s in c["segs"].items() if s[0] < now - self.window_s]
            for cid, key in old:
                freed += self._remove(cid, key)
            self._room(0)
            with self._lock:
                empty = [cid for cid, c in self._cams.items() if not c["segs"] and cid not in self._wanted]
                for cid in empty:
                    self._cams.pop(cid)
            for cid in empty:
                try:
                    self._dir(cid).rmdir()
                except OSError:
                    pass
        return freed

    # -- what to record ----------------------------------------------------------------------------------------------

    def _alias(self, cid: str) -> str:
        """The id the camera is listed under now (Roads.alias, #320: its records merged, another one kept)."""
        fn = getattr(self.roads, "alias", None)
        return fn(cid) if fn is not None else cid

    def _pins_now(self) -> dict[str, float]:
        now = self.clock()
        with self._lock:
            if not isinstance(self.pins, dict):           # ids only (a set or list): pinned now
                self.pins = {cid: now for cid in self.pins}
            gone = [cid for cid, at in self.pins.items() if now - at > PIN_S]
            for cid in gone:
                self.pins.pop(cid)
            moved = {cid: self._alias(cid) for cid in self.pins}
            moved = {a: b for a, b in moved.items() if a != b}
            for a, b in moved.items():                    # a Keep follows its camera to the id kept for it (#320)
                at = self.pins.pop(a)
                self.pins[b] = max(at, self.pins.get(b, at))
            pins = dict(self.pins)
        if gone:
            self.log(f"dvr: Keep ran out for {', '.join(sorted(gone))}")
        if moved:
            self.log(f"dvr: Keep moved with its camera: {', '.join(f'{a} -> {b}' for a, b in sorted(moved.items()))}")
        if gone or moved:
            self.save_pins(pins)
        return pins

    def follow(self) -> list[str]:
        """A camera's records merged and the id kept for it changed (#320): its recording goes with it, so the viewer
        of the id kept now finds the window recorded so far. Moved only to an id with nothing recorded yet (else the
        older stays under its own id, still served there), never while a segment is written (the disk lock: _write
        holds it), never while either camera has a round under way (_busy), and only when may_move(old, new) says so
        (whatever else reads the segments' paths: a clip being cut or uploaded, a standby that mustn't write). The ids
        moved."""
        fn = getattr(self.roads, "aliases", None)
        moved = []
        for old, new in (fn() if fn is not None else {}).items():
            if not (CAM_RE.fullmatch(old) and CAM_RE.fullmatch(new)) or old == new:
                continue
            with self._disk, self._lock:
                c = self._cams.get(old)
                if not c or not c["segs"] or (self._cams.get(new) or {}).get("segs") or old in self._busy or new in self._busy:
                    continue
                if not self.may_move(old, new):
                    continue
                src, dst = self._dir(old), self._dir(new)
                try:
                    if dst.exists():
                        dst.rmdir()                       # (an empty folder only: anything in it stays and the move waits)
                    src.rename(dst)
                except OSError as e:
                    self.log(f"dvr: {old}'s recording not moved to {new} ({type(e).__name__})")
                    continue
                for k in ("chunk", "hstate", "retry_at", "fails"):
                    c.pop(k, None)                        # another stream, maybe: a session of its own, keys carried on
                c["restarted"] = True                     # (and a discontinuity before its first)
                self._cams[new] = c
                self._cams.pop(old)
                for d in (self._opened, self._next):
                    if old in d:
                        d[new] = max(d.pop(old), d.get(new, 0))
            moved.append(old)
            self.log(f"dvr: {old}'s recording moved to {new} (the same camera, now listed as {new})")
        return moved

    def pin(self, cid: str, on: bool) -> dict:
        cid = self._alias(cid)
        cam = self.roads.camera(cid)
        if cam is None or not CAM_RE.fullmatch(cid):
            raise KeyError(cid)
        if on and not cam.get("video"):
            raise ValueError("this camera has no live video to record")
        with self._lock:
            if on:
                self.pins[cid] = self.clock()             # (again: renewed for PIN_S)
            else:
                self.pins.pop(cid, None)
            pins = dict(self.pins)
        self.save_pins(pins)
        self.log(f"dvr: {cid} {'kept' if on else 'no longer kept'}")
        with self._lock:
            self._wanted = self.wanted()
        self.roads._changed()
        return self.status(cid)

    def _incident_cams(self) -> dict[str, float]:
        """Camera id -> metres from the nearest open CAD event, for the cameras an event wants."""
        out: dict[str, float] = {}
        from sizeup.roads import km
        try:
            events = self.incidents() or []
        except Exception as e:  # the CAD store busy or gone: no event cameras this round
            self.log(f"dvr: open events not read ({type(e).__name__})")
            events = []
        cams = [c for c in self.roads.merged("cameras") if c.get("video") and not c.get("off")]
        for ev in events:
            lat, lon = ev.get("lat"), ev.get("lon")
            if lat is None or lon is None:
                continue
            for c in cams:
                d = km(lat, lon, c["lat"], c["lon"]) * 1000
                if d <= self.near_m:
                    out[c["id"]] = min(out.get(c["id"], d), d)
            try:
                along = self.roads.cameras_for(lat, lon, ev.get("label", ""), ev.get("address", ""))["cameras"]
            except Exception:  # pragma: no cover - the picker's trouble never stops the recording
                along = []
            for c in along:
                if c.get("rel") in ("at", "before", "after") and c.get("has_video"):
                    d = (c.get("km") or 0) * 1000
                    out[c["id"]] = min(out.get(c["id"], d), d)
        return out

    def wanted(self) -> dict[str, list[str]]:
        """Camera id -> why it is recorded, at most max_cameras: always, an open event's (nearest first), the ones
        kept (a Keep runs out after PIN_S), then the ones watched."""
        now = self.clock()
        with self._lock:
            for cid, st in list(self.roads._streams.items()):
                if now - st.get("last", 0) <= 30:
                    self._opened[cid] = now
            for cid in [c for c, t in self._opened.items() if now - t > self.linger_s]:
                self._opened.pop(cid)
            opened = sorted(self._opened.items(), key=lambda x: -x[1])
        pins = self._pins_now()
        self.follow()
        video = {c["id"] for c in self.roads.merged("cameras") if c.get("video") and not c.get("off") and CAM_RE.fullmatch(c["id"])}
        why: dict[str, list[str]] = {}
        order: list[str] = []

        def add(cid: str, reason: str) -> None:
            if cid not in video:
                return
            if cid not in why:
                why[cid] = []
                order.append(cid)
            if reason not in why[cid]:
                why[cid].append(reason)
        for cid in self.always:
            add(self._alias(cid), "always")
        for cid, _ in sorted(self._incident_cams().items(), key=lambda x: x[1]):
            add(cid, "incident")
        for cid in sorted(pins):
            add(cid, "pinned")
        for cid, _ in opened:
            add(cid, "open")
        return {cid: why[cid] for cid in order[:self.max_cameras]}

    # -- recording ---------------------------------------------------------------------------------------------------

    def _own_list(self, cid: str, cam: dict, c: dict, now: float) -> str | None:
        """This recorder's own session's chunklist: its master playlist asked once (again when a chunklist fails,
        at once); a camera that keeps failing waits, 15 s doubling to SESSION_RETRY_S."""
        if c.get("retry_at") and now < c["retry_at"]:
            return None
        base = cam["video"].rsplit("/", 1)[0] + "/"
        why = ""
        for attempt in (1, 2):                            # the second: a new session at once
            try:
                if not c.get("chunk") or attempt == 2:
                    code, _, body = self.roads.fetch(cam["video"], 6.0, 1 << 20)
                    lines = [t.strip() for t in body.decode(errors="replace").splitlines()] if code == 200 else []
                    c["chunk"] = next((t for t in lines if t and not t.startswith("#")), "")
                    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}\.m3u8", c["chunk"] or ""):
                        raise ValueError("no chunklist named")
                code, _, body = self.roads.fetch(base + c["chunk"], 10.0, 1 << 20)
                if code != 200:
                    raise ValueError(f"HTTP {code}")
                c["fails"], c["retry_at"] = 0, 0.0
                return body.decode(errors="replace")
            except (HTTPError, URLError, OSError, ValueError) as e:
                c["chunk"] = ""
                why = f"{type(e).__name__}: {e}"
        c["fails"] = c.get("fails", 0) + 1
        if c["fails"] >= FAILS_BEFORE_WAIT:
            wait_s = min(SESSION_RETRY_S, 15.0 * 2 ** (c["fails"] - FAILS_BEFORE_WAIT))
            c["retry_at"] = now + wait_s
            self.log(f"dvr: {cid}: no stream ({why}); {c['fails']} failures, trying again in {wait_s:.0f} s")
        return None

    def _list(self, cid: str, cam: dict, c: dict) -> tuple[str, str, bool] | None:
        """(the stream's folder, its chunklist text, live): the live stream's when one is open (the pump's copy),
        else this recorder's own session."""
        st = self.roads._streams.get(cid)
        now = self.clock()
        if st is not None and st.get("list") and now - st.get("list_at", 0) < 10:
            return st["base"], st["list"].decode(errors="replace"), True
        text = self._own_list(cid, cam, c, now)
        return (cam["video"].rsplit("/", 1)[0] + "/", text, False) if text is not None else None

    def _fetch_own(self, cid: str, c: dict, base: str, name: str, seq: int | None) -> bytes | None:
        """A segment for the recording only (never into the live viewers' cache): fetched, checked (carrying on from
        the one before), repaired; None when it isn't MPEG-TS twice or has no picture left (a hole, never a bad copy)."""
        for attempt in (1, 2):
            try:
                code, _, body = self.roads.fetch(base + name, 10.0, 4 * 1024 * 1024)
                if code != 200 or not body:
                    raise ValueError(f"HTTP {code}")
                prev = c.get("hstate")
                state = prev[1] if prev and seq is not None and prev[0] == seq - 1 else None
                info = hlsseg.check(body, state)
                if seq is not None:
                    c["hstate"] = (seq, info["state"])
                return hlsseg.repair(body, info)
            except hlsseg.Bad as e:
                if "no picture" in str(e):
                    self.log(f"dvr: {cid}: {name}: nothing to show, a hole")
                    return None
                if attempt == 2:
                    self.log(f"dvr: {cid}: {name} not kept ({e})")
                    return None
            except (HTTPError, URLError, OSError, ValueError) as e:
                if attempt == 2:
                    self.log(f"dvr: {cid}: {name} not kept ({type(e).__name__}: {e})")
                    return None
        return None

    def record_once(self, cid: str) -> int:
        """One round for one camera: its chunklist, then every segment in it not yet kept. The number written."""
        cam = self.roads.camera(cid)
        if cam is None or not cam.get("video") or not CAM_RE.fullmatch(cid):
            return 0
        c = self._cam(cid)
        got = self._list(cid, cam, c)
        if got is None:
            return 0
        base, text, live = got
        first, items = _chunklist(text)
        if not items:
            return 0
        now = self.clock()
        seqs = []
        for k, (name, _) in enumerate(items):
            m = SEQ_RE.search(name)
            seqs.append(int(m.group(1)) if m else first + k)
        with self._lock:
            last, offset = c["last"], c.get("offset", 0)
        if last is not None and seqs[-1] + offset + 3 < last:
            # the origin's counter started over (a restart): carry the keys on from the last one
            offset = last + 1 - seqs[0]
            with self._lock:
                c["offset"] = offset
            c["restarted"] = True                              # a discontinuity before the next one kept
            c.pop("hstate", None)
            self.log(f"dvr: {cid}: the stream's sequence started over (now {seqs[-1]}); carried on from {last}")
        # each one's end: now, less the durations of those after it (a segment is listed once it is whole)
        ends, t = [], now
        for _, dur in reversed(items):
            ends.append(t)
            t -= dur
        ends.reverse()
        n = 0
        for k, (name, dur) in enumerate(items):
            key = seqs[k] + offset
            with self._lock:
                have = key in c["segs"] or (c["last"] is not None and key <= c["last"])
                last, end = c["last"], c["end"]
            if have:
                continue
            if self._stop.is_set() or self.stopped and not self._room(1 << 20):
                break
            t0 = ends[k] - dur
            if last is not None and key == last + 1 and end is not None and abs(end - t0) < 3 * dur + 2:
                t0 = end                                       # the one before ends where this begins
            if live:
                try:
                    hit = self.roads._segment(cid, base, name)    # the viewers' copy: fetched once for both
                except (HTTPError, URLError, OSError, ValueError) as e:
                    self.log(f"dvr: {cid}: {name} not kept ({type(e).__name__}: {e})")
                    continue
                body = None if hit.get("noplay") else hit["body"]
            else:
                body = self._fetch_own(cid, c, base, name, seqs[k])
            if body is None:
                continue
            # the first of a new stream: another stream (the camera kept under another record, #320), the origin's counter
            # started over, a jump in the sequence, or nothing known of the one before (a restart, a recording moved)
            brk = last is not None and (c.get("restarted") or c.get("src") != base or key != last + 1)
            if self._write(cid, key, t0, dur, body, brk):
                c["src"] = base
                c.pop("restarted", None)
                n += 1
        return n

    def tick(self, wait_all: bool = False) -> int:
        """One round over every camera wanted, then the eviction. Started (a pool of WORKERS): each camera on its own
        cadence, a new round when its last one is done and TICK_S has passed, never waiting on the slowest camera;
        the segments written by the rounds that ended this tick (all of them with wait_all). Not started: each in
        turn. The segments written."""
        wanted = self.wanted()
        with self._lock:
            before = self._wanted
            self._wanted = wanted
        if set(wanted) != set(before):
            added, gone = set(wanted) - set(before), set(before) - set(wanted)
            if added:
                self.log(f"dvr: recording {', '.join(f'{c} ({'/'.join(wanted[c])})' for c in sorted(added))}")
            if gone:
                self.log(f"dvr: stopped {', '.join(sorted(gone))} (the recording stays for its window)")
            self.roads._changed()                              # the map's "rec" marks
        if self.stopped and not self._room(1 << 20):
            return 0                                           # no room: nothing fetched (status says why)
        n = 0
        if self._pool is not None:
            now = self.clock()
            futs = []
            for cid in wanted:
                with self._lock:
                    if cid in self._busy or now < self._next.get(cid, 0):
                        continue
                    self._busy.add(cid)
                futs.append(self._pool.submit(self._round, cid))
            if wait_all and futs:
                done, _ = wait(futs, timeout=60.0)
                n = sum(f.result() for f in done)
        else:
            for cid in wanted:
                if self._stop.is_set():
                    break
                n += self._record_safe(cid)
        self.evict()
        return n

    def _round(self, cid: str) -> int:
        try:
            return self._record_safe(cid)
        finally:
            with self._lock:
                self._busy.discard(cid)
                self._next[cid] = self.clock() + TICK_S

    def _record_safe(self, cid: str) -> int:
        try:
            return self.record_once(cid)
        except Exception as e:  # pragma: no cover - one camera's surprise never stops the others
            self.log(f"dvr: {cid}: {type(e).__name__}: {e}")
            return 0

    def start(self) -> Dvr:
        raw = self.load_pins() or {}
        now = self.clock()
        if isinstance(raw, list):                              # (the first shape: ids only)
            raw = {p: now for p in raw}
        with self._lock:
            self.pins = {p: float(at) for p, at in raw.items() if isinstance(p, str) and CAM_RE.fullmatch(p)
                         and isinstance(at, (int, float))}
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._pool = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="dvr")
            self._thread = threading.Thread(target=self._run, name="dvr", daemon=True)
            self._thread.start()
            self.log(f"dvr: on, {self.window_s / 60:.0f} min per camera, at most {self.max_cameras} cameras and "
                     f"{self.max_bytes / 1024 ** 3:.1f} GB, in {self.root}")
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = None

    def _run(self) -> None:
        while not self._stop.is_set():
            t = self.clock()
            try:
                self.tick()
            except Exception as e:  # pragma: no cover
                self.log(f"dvr: {type(e).__name__}: {e}")
            self._stop.wait(1.0 if self._pool is not None else max(1.0, TICK_S - (self.clock() - t)))

    # -- what the pages get ------------------------------------------------------------------------------------------

    def recording(self, cid: str) -> list[str]:
        with self._lock:
            return [] if self.stopped else list(self._wanted.get(cid, []))

    def status(self, cid: str) -> dict:
        with self._lock:
            c = self._cams.get(cid)
            segs = sorted(c["segs"].items()) if c else []
            wanted = list(self._wanted.get(cid, []))
            pinned_at = self.pins.get(cid)
        gaps, prev = [], None
        for _, (t0, dur, _) in segs:
            if prev is not None and t0 - prev > GAP_S:
                gaps.append([round(prev, 3), round(t0, 3)])
            prev = t0 + dur
        return {"recording": bool(wanted) and not self.stopped, "why": wanted, "stopped": self.stopped if wanted else "",
                "pinned": pinned_at is not None, "pinned_until": round(pinned_at + PIN_S) if pinned_at else None,
                "from": round(segs[0][1][0], 3) if segs else None, "to": round(prev, 3) if prev else None,
                "segments": len(segs), "bytes": sum(s[2] for _, s in segs), "window_s": self.window_s,
                "gaps": gaps[-50:]}

    def playlist(self, cid: str) -> bytes | None:
        with self._lock:
            c = self._cams.get(cid)
            segs = sorted(c["segs"].items()) if c else []
            breaks = set(c.get("breaks", ())) if c else set()
        if not segs:
            return None
        target = max(1, int(max(s[1] for _, s in segs) + 0.999))
        out = ["#EXTM3U", "#EXT-X-VERSION:3", f"#EXT-X-TARGETDURATION:{target}", "#EXT-X-PLAYLIST-TYPE:VOD",
               f"#EXT-X-MEDIA-SEQUENCE:{segs[0][0]}"]
        prev = None
        for key, (t0, dur, _) in segs:
            if prev is not None and (abs(t0 - prev) > GAP_S or key in breaks):
                out.append("#EXT-X-DISCONTINUITY")
            pdt = datetime.fromtimestamp(t0, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            # ?s=<start ms>: a key the counter reuses after a restart is another URL, never a cached old segment (#321)
            out += [f"#EXT-X-PROGRAM-DATE-TIME:{pdt}", f"#EXTINF:{dur:.3f},", f"{key}.ts?s={int(t0 * 1000)}"]
            prev = t0 + dur
        out.append("#EXT-X-ENDLIST")
        return ("\n".join(out) + "\n").encode()

    def handles(self, path: str) -> bool:
        return bool(DVR_PATH.fullmatch(path))

    def get(self, path: str, query: str = "") -> tuple[int, bytes, str, dict]:
        m = DVR_PATH.fullmatch(path)
        if not m:
            return 404, b"not found\n", "text/plain", {}
        cid, what = m.group(1), m.group(2)
        hdr = {"Cache-Control": "no-store"}
        with self._lock:
            known = cid in self._cams
        if self.roads.camera(cid) is None and not known:
            cid = self._alias(cid)                        # a merged-away id: the camera kept for it (#320)
            with self._lock:
                known = cid in self._cams
        if self.roads.camera(cid) is None and not known:
            return 404, b'{"error":"no such camera"}', JSON, hdr
        if not what:
            return 200, json.dumps(self.status(cid)).encode(), JSON, hdr
        if what == "index.m3u8":
            body = self.playlist(cid)
            if body is None:
                return 404, b'{"error":"nothing recorded"}', JSON, hdr
            return 200, body, "application/vnd.apple.mpegurl", hdr
        f = self._file(cid, int(what[:-3]))
        want = parse_qs(query).get("s", [""])[0]
        if f is not None and want and f.name.split("_")[1] != want:      # that key, but another segment now
            f = None
        try:
            body = f.read_bytes() if f is not None else None
        except OSError:
            body = None
        if body is None:
            return 404, b'{"error":"not recorded (or past the window)"}', JSON, hdr
        return 200, body, "video/MP2T", {"Cache-Control": "private, max-age=3600"}
