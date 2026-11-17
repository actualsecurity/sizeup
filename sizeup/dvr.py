"""Camera DVR (#277): the live cameras' HLS segments kept on the hub's disk for a rolling window, so the viewer can
scrub back. Copies, no re-encoding: each segment as the hub serves it live (checked whole and repaired, #276,
sizeup/hlsseg.py), one file per segment.

What is recorded, on demand only (Dvr.wanted, at most max_cameras at once, in this order):
  always    [roads] dvr_always: camera ids recorded all the time
  incident  an open CAD event's cameras: the ones just before / at / after it along the road (Roads.cameras_for)
            and any within dvr_near_m as the crow flies
  always    (#322) on shared storage only: every camera with video in our response area, around the clock: the
            camera wall's area (Settings > System, Roads.area_rows: our fire districts, its margin_m, its include /
            exclude) and up to area_extra_m ([roads] dvr_extra_m) beyond that margin; [roads] dvr_area, a polygon,
            only as an override
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
Each camera's index keeps its byte total and its segments in time order as it changes (_Segs, #446): the window and
the cap take the oldest from its head, never a pass over every kept segment (12 h of 19 cameras: ~128,000, several
times a second), and the free disk is read at most every FREE_EVERY_S. A camera's own session fetches over kept-alive
connections (sizeup/httpkeep.py).

Where a segment came from: the camera's own HLS session (fetched, checked and repaired here, never in the live
viewers' cache), or the live stream's when one is open (Roads._streams: the same segments, fetched once). A few
workers record the cameras side by side. Segments are keyed by the stream's media sequence (Wowza's counter runs on
across sessions), so neither a new session nor the two sources ever double one; a counter that starts over
(the origin restarted) is carried on from the last key. A chunklist that fails is asked again at once with a new
session; only after repeated failures does the camera wait (15 s doubling to SESSION_RETRY_S). Its time: when it first appears in the
chunklist (Wowza lists a segment once it is whole), less the durations of those after it, carried on from the one
before when they follow each other.

Parked (#550): a camera dead upstream (no answer, or its master playlist 404) is not retried forever. Failing without
a success for PARK_AFTER_S (1 h; PARK_404_S, 15 min, when every try is a master-playlist 404) and at least
PARK_MIN_FAILS rounds, it is parked: one log line, no recording rounds and no per-try lines, its slot freed for the
next camera wanted. A parked camera is looked at every PARK_CHECK_S (30 min: its master playlist and chunklist,
PROBE_TIMEOUT_S each; a fresher probe by roads' sweep, or an open live stream, counts without asking): when it
answers it is back (one line) and is wanted again in its place. Cameras that restart at their origin every few minutes
recover well inside those times and never park. The parked list is kept in PARKED_FILE next to the recordings
(written whole, only by the leader: may_write), so a restart or a takeover keeps it. Health shows it (panel()).

HTTP (behind Cloudflare Access with the rest of /api/cad; nothing here is public, no share links):
  GET  /api/cad/cameras/<id>/dvr                   {recording, why: [always | pinned | incident | open], pinned,
                                                    from, to, segments, bytes, window_s, gaps: [[from, to], ...],
                                                    parked: {why, since, next} | null (#550)}
  GET  /api/cad/cameras/<id>/dvr/index.m3u8         the recording as a VOD playlist: EXT-X-PROGRAM-DATE-TIME on every
                                                    segment (the viewer's timestamp), EXT-X-DISCONTINUITY at a gap and
                                                    at a new stream (another stream, the origin restarted)
  GET  /api/cad/cameras/<id>/dvr/<seq>.ts?s=<ms>    a recorded segment (s: its start, as the playlist lists it; another
                                                    segment under the same key now: 404)
  POST /api/cad/cameras/<id>/dvr/pin {on}           (sizeup/cad/api.py, dispatchers and admins) keep recording / stop

A deploy (sizeup/handoff.py): the recordings are on the task's ephemeral disk, so a new task starts with none. The old
task records until it drains; the new one starts recording when its app serves (TAKEOVER): the window restarts
empty and there's a gap of the handoff's length. The pins are in the CAD store (Litestream): they carry over.

Shared storage (#322): dvr_dir on a mount shared between tasks (EFS, NFS: shared_mount(), or [roads] dvr_shared) keeps
the recording across deploys, so the window is 12 h (dvr_shared_window_min) and the area's cameras are recorded 24/7.
Only the lease-holding leader writes or deletes (may_write: the app's fence, sizeup/fence.py); the standby's warm app
(sizeup/app.py prewarm) never makes a Dvr at all. A new leader reads the index from the file names in the background
(_scan, the recording waits for it), then appends after each camera's last key. The old leader may still be draining
meanwhile: a file that vanishes under the scan is skipped, another writer's temp file is left alone unless it is stale
(STALE_TMP_S), and for RESCAN_FOR_S after the start the folder is read again every RESCAN_S, taking in what the old
leader wrote after the first look and dropping what it evicted. A segment's size is in its file name
(key_start_duration_bytes.ts), so reading the index never stats a file over NFS. Without shared storage: as before.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs

from sizeup import hlsseg, httpkeep

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
FILE_RE = re.compile(r"(\d{1,15})_(\d{10,16})_(\d{1,9})(?:_(\d{1,12}))?(_n)?\.ts")   # key, start (ms), duration (ms),
#   bytes (#322: the index read without a stat), _n: a new stream (#320); the older forms without either still read
CAM_ID = r"[A-Za-z0-9_][A-Za-z0-9_.:-]{0,63}"                      # never "." or "..": a folder name
CAM_RE = re.compile(CAM_ID)
DVR_PATH = re.compile(rf"/api/cad/cameras/({CAM_ID})/dvr(?:/(index\.m3u8|\d{{1,15}}\.ts))?")
JSON = "application/json"
REASONS = ("always", "incident", "pinned", "open")
SHARED_FS = ("nfs", "nfs4", "efs")    # a mount of these types: shared between tasks (EFS)
STALE_TMP_S = 600.0                   # shared storage: another writer's temp file this old is a write cut short
RESCAN_S = 30.0                       # shared storage: the folder read again this often ...
RESCAN_FOR_S = 300.0                  # ... for this long after the start (the old leader draining)
MAX_WORKERS = 24
AREA_EVERY_S = 60.0                   # the response area's cameras: worked out again this often
PARK_AFTER_S = 3600.0                 # (#550) failing without a success this long: parked ...
PARK_404_S = 900.0                    # ... this long when every try is a master-playlist 404 (gone, not restarting)
PARK_MIN_FAILS = 6                    # ... and at least this many failed rounds
PARK_CHECK_S = 1800.0                 # a parked camera looked at this often
PROBE_TIMEOUT_S = 5.0                 # ... each of its two requests
PARKED_FILE = ".parked.json"          # next to the recordings (never a camera's folder: CAM_ID starts with no dot)
FREE_EVERY_S = 30.0                   # the disk's free space (statvfs: on EFS a call over the network) read this often ...
FREE_SLACK = 512 * 1024 ** 2          # ... and at once when what was written since may bring it this near the floor


def shared_mount(path: str | Path, mounts: str = "/proc/self/mounts") -> str:
    """The file system type of the mount path is on when it is one shared between tasks (EFS: "nfs4"), else ""."""
    try:
        p = os.path.realpath(path)
        best, kind = "", ""
        with open(mounts) as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mp = parts[1].replace("\\040", " ")
                if (p == mp or p.startswith(mp.rstrip("/") + "/")) and len(mp) >= len(best):
                    best, kind = mp, parts[2]
        return kind if kind.split(".")[-1] in SHARED_FS else ""
    except OSError:
        return ""


def area_polygon(area) -> tuple[tuple[float, float], ...]:
    """[roads] dvr_area as a ring of (lon, lat): a bbox [west, south, east, north] or a polygon [[lon, lat], ...]."""
    if not area:
        return ()
    if len(area) == 4 and all(isinstance(x, (int, float)) for x in area):
        w, s, e, n = (float(x) for x in area)
        return ((w, s), (e, s), (e, n), (w, n))
    return tuple((float(p[0]), float(p[1])) for p in area)


def in_area(poly, lat: float, lon: float) -> bool:
    """Inside the ring (ray casting)."""
    inside, n = False, len(poly)
    for i in range(n):
        (x1, y1), (x2, y2) = poly[i], poly[(i + 1) % n]
        if (y1 > lat) != (y2 > lat) and lon < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


class _Segs(dict):
    """A camera's index, key -> (start, duration, bytes), that keeps its own byte total and its keys in time order
    (oldest first) as it changes (#446): the total without a pass over every segment, the oldest at its head (the
    window and the cap take from there). An entry of the order whose segment went, or now starts at another time, is
    dropped when it reaches the head; the order is sorted again only after one came in out of time (a scan)."""
    __slots__ = ("bytes", "_order", "_sorted")

    def __init__(self, items=()):
        super().__init__()
        self.bytes, self._order, self._sorted = 0, deque(), True
        self.update(items)

    def __setitem__(self, key, seg) -> None:
        old = dict.get(self, key)
        dict.__setitem__(self, key, seg)
        self.bytes += seg[2] - (old[2] if old is not None else 0)
        if old is None or old[0] != seg[0]:
            e = (seg[0], key)
            if self._order and e < self._order[-1]:
                self._sorted = False
            self._order.append(e)
            if len(self._order) > 2 * len(self) + 64:
                self._resort()

    def __delitem__(self, key) -> None:
        self.bytes -= dict.pop(self, key)[2]

    def pop(self, key, *default):
        if key in self:
            seg = dict.pop(self, key)
            self.bytes -= seg[2]
            return seg
        if default:
            return default[0]
        raise KeyError(key)

    def popitem(self):
        key, seg = dict.popitem(self)
        self.bytes -= seg[2]
        return key, seg

    def setdefault(self, key, default=None):
        if key not in self:
            self[key] = default
        return dict.__getitem__(self, key)

    def update(self, *a, **kw) -> None:
        for key, seg in dict(*a, **kw).items():
            self[key] = seg

    def __ior__(self, other):
        self.update(other)
        return self

    def clear(self) -> None:
        dict.clear(self)
        self.bytes, self._order, self._sorted = 0, deque(), True

    def _resort(self) -> None:
        self._order, self._sorted = deque(sorted((s[0], k) for k, s in dict.items(self))), True

    def _live(self, e: tuple) -> bool:
        s = dict.get(self, e[1])
        return s is not None and s[0] == e[0]

    def head(self) -> tuple | None:
        """(start, key) of the oldest segment, None when there is none (what went before it: dropped from the order)."""
        if not self._sorted:
            self._resort()
        o = self._order
        while o and not self._live(o[0]):
            o.popleft()
        return o[0] if o else None

    def older(self, before: float) -> list:
        """The keys of the segments that start before this, oldest first."""
        out = []
        if self.head() is None:
            return out
        for e in self._order:
            if e[0] >= before:
                break
            if self._live(e):
                out.append(e[1])
        return out


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
                 free_floor: int = FREE_FLOOR_BYTES, workers: int = WORKERS, area=(), shared: bool = False,
                 may_write: Callable[[], bool] | None = None, area_wall: bool = False, area_extra_m: float = 0.0):
        self.roads, self.root = roads, Path(root)
        self.window_s, self.max_bytes, self.near_m = float(window_s), int(max_bytes), float(near_m)
        self.always = tuple(a for a in always if CAM_RE.fullmatch(a))
        self.linger_s, self.max_cameras = float(linger_s), int(max_cameras)
        self.clock, self.log, self.free_floor, self.workers = clock, log, int(free_floor), max(1, int(workers))
        # shared storage (#322): the area's cameras always; only the leader writes; the index read in the background
        self.area, self.shared = area_polygon(area), bool(shared)
        self.area_wall, self.area_extra_m = bool(area_wall) and not self.area, float(area_extra_m)
        self._area_at, self._area_ids = -1e18, []
        self._inc_cache: tuple = (None, {})       # (open events, cameras with video, near_m) -> _incident_cams
        self.may_write = may_write or (lambda: True)
        self._scanned = threading.Event()
        self._started_at: float | None = None
        self._rescanned_at = 0.0
        # the app hands in: open CAD events [{lat, lon, address, label}], and where the pins are kept
        self.incidents: Callable[[], list[dict]] = list
        self.load_pins: Callable[[], object] = list
        self.save_pins: Callable[[dict], None] = lambda pins: None
        self.pins: dict[str, float] = {}          # cid -> when it was pinned
        # a recording's folder renamed to the id its camera is kept under now (follow, #320): only when this says so
        # (#322: only the leader, never while a clip of either camera is being saved: _may_move)
        self.may_move: Callable[[str, str], bool] = lambda old, new: self._may_move(old, new)
        self._lock = threading.RLock()            # _cams, _opened, _wanted, pins
        self._disk = threading.RLock()            # making room and writing: one at a time
        self._cams: dict[str, dict] = {}          # cid -> {segs: _Segs {key: (t0, dur, bytes)}, last, end, offset, ...}
        self._opened: dict[str, float] = {}       # cid -> last time someone watched it live
        self._wanted: dict[str, list[str]] = {}
        self.stopped = ""                         # why recording stopped (the disk), "" when it records
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pool: ThreadPoolExecutor | None = None
        self._busy: set[str] = set()              # cameras with a round under way (pool mode)
        self._next: dict[str, float] = {}         # cid -> when its next round is due
        self.holding: dict[str, int] = {}         # cid -> clips of it being saved (never renamed meanwhile)
        self.parked: dict[str, dict] = {}         # (#550) cid -> {why, since, at, checked, next}: dead upstream
        self._back: set[str] = set()              # unparked since the last tick's "recording" line (said already)
        self._parked_lock = threading.Lock()      # PARKED_FILE: one writer at a time
        self._load_parked()
        self.mono: Callable[[], float] = time.monotonic
        self._free_read: tuple[float, int, int] | None = None   # (when, free bytes, total_bytes() then): _free_after
        if not self.shared:
            self._scan()                          # (shared: in the background, before the first round: start())
            self._scanned.set()

    # -- the disk ----------------------------------------------------------------------------------------------------

    def _dir(self, cid: str) -> Path:
        if not CAM_RE.fullmatch(cid):
            raise ValueError(f"not a camera id: {cid!r}")
        return self.root / cid

    def _cam(self, cid: str) -> dict:
        with self._lock:
            return self._cams.setdefault(cid, {"segs": _Segs(), "last": None, "end": None, "offset": 0, "breaks": set()})

    def _read_dir(self, d: Path) -> dict[int, list[tuple[float, float, int, str, bool]]] | None:
        """A camera's folder: key -> its files [(start, duration, bytes, file name, a new stream)], usually one; two when
        both leaders wrote the same segment (reported, never deleted here: _scan decides which one stays). None when
        the folder can't be listed (its index is left as it is). A file that goes meanwhile (another writer evicting
        it) is skipped; a write cut short (a temp file) goes: at once on our own disk, on shared storage only once it
        is STALE_TMP_S old (it may be the old leader's, still being written)."""
        segs: dict[int, list[tuple[float, float, int, str, bool]]] = {}
        try:
            entries = list(os.scandir(d))
        except OSError:
            return None
        now = time.time()
        for f in entries:
            m = FILE_RE.fullmatch(f.name)
            try:
                if m:
                    size = int(m.group(4)) if m.group(4) else f.stat().st_size
                    segs.setdefault(int(m.group(1)), []).append(
                        (int(m.group(2)) / 1000, int(m.group(3)) / 1000, size, f.name, bool(m.group(5))))
                elif f.name.endswith(".tmp") and (not self.shared or now - f.stat().st_mtime > STALE_TMP_S):
                    if self.may_write():
                        Path(f.path).unlink(missing_ok=True)
            except FileNotFoundError:
                continue
        return segs

    @staticmethod
    def _newest(d: Path, files: list[tuple]) -> tuple:
        """Of a segment's files (both leaders wrote it), the one written last (its mtime; a file gone meanwhile: never)."""
        def mtime(x) -> float:
            try:
                return (d / x[3]).stat().st_mtime
            except OSError:
                return -1.0
        return max(files, key=lambda x: (mtime(x), x[3]))

    def _scan(self) -> None:
        """What is already on disk (the app restarted within the same task, or on shared storage the leader before
        this one): the index from the file names. Taken in alongside what is already indexed (rescans): a segment
        not indexed is added, an indexed one whose file is gone is dropped. A segment both leaders wrote: the indexed
        file stays (else the newest), only the other is deleted, whatever order the folder lists them in."""
        if not self.root.is_dir():
            return
        try:
            dirs = [d for d in os.scandir(self.root) if CAM_RE.fullmatch(d.name) and d.is_dir()]
        except OSError:
            return
        for d in dirs:
            with self._lock:                          # what was indexed before the listing (not what we write meanwhile)
                known = set(self._cams[d.name]["segs"]) if d.name in self._cams else set()
            found = self._read_dir(Path(d.path))
            if found is None:
                continue                              # not listable now: its index stays as it is
            twins = []
            with self._lock:
                c = self._cams.get(d.name)
                if c is None:
                    if not found:
                        continue
                    c = self._cams.setdefault(d.name, {"segs": _Segs(), "last": None, "end": None, "offset": 0, "names": {},
                                                        "breaks": set()})
                names = c.setdefault("names", {})
                breaks = c.setdefault("breaks", set())
                for key in [k for k in known if k not in found]:
                    c["segs"].pop(key, None)          # gone from the disk (evicted by the other writer)
                    names.pop(key, None)
                    breaks.discard(key)
                for key, files in found.items():
                    have = (names.get(key) or _name(key, *c["segs"][key], key in breaks)) if key in c["segs"] else None
                    keep = next((x for x in files if x[3] == have), None)
                    if keep is None:                  # not indexed (or its indexed file is gone): the newest one
                        keep = files[0] if len(files) == 1 else self._newest(Path(d.path), files)
                        t0, dur, size, name, brk = keep
                        c["segs"][key] = (t0, dur, size)
                        names[key] = name
                        if brk:
                            breaks.add(key)
                        else:
                            breaks.discard(key)
                    twins += [x[3] for x in files if x[3] != keep[3]]   # the same segment twice: one is enough
                if c["segs"]:
                    last = max(c["segs"])
                    if c["last"] is None or last > c["last"]:
                        c["last"], c["end"] = last, c["segs"][last][0] + c["segs"][last][1]
            if twins and self.may_write():
                for name in twins:
                    (Path(d.path) / name).unlink(missing_ok=True)

    def scanned(self) -> bool:
        return self._scanned.is_set()

    def _file(self, cid: str, key: int) -> Path | None:
        with self._lock:
            c = self._cams.get(cid)
            s = c["segs"].get(key) if c else None
            name = c.get("names", {}).get(key) if c else None
            brk = bool(c) and key in c.get("breaks", ())
        if not s:
            return None
        return self._dir(cid) / (name or _name(key, s[0], s[1], s[2], brk))

    def _free(self) -> int:
        """Free bytes on the recordings' disk; 0 when it can't be told (fail closed: no write)."""
        try:
            p = self.root
            while not p.exists() and p != p.parent:
                p = p.parent
            return shutil.disk_usage(p).free
        except OSError:
            return 0

    def _free_after(self, need: int) -> int:
        """Free bytes on the recordings' disk once need more are written. The disk is read at most every FREE_EVERY_S
        (#446); in between: that reading less what the recordings grew by since (what went is never counted back). Read
        again at once whenever that comes within FREE_SLACK of the floor, so a write is never refused, nor the floor
        passed, on an old reading."""
        now, total, seen = self.mono(), self.total_bytes(), self._free_read
        if seen is not None and now - seen[0] < FREE_EVERY_S:
            free = seen[1] - max(0, total - seen[2]) - need
            if free >= self.free_floor + FREE_SLACK:
                return free
        free = self._free()
        self._free_read = (now, free, total)
        return free - need

    def _segs(self, c: dict) -> _Segs:
        """A camera's index (under _lock), made a _Segs if it was handed in as a plain dict."""
        s = c["segs"]
        if not isinstance(s, _Segs):
            s = c["segs"] = _Segs(s)
        return s

    def _room(self, need: int) -> bool:
        """Room for need more bytes: under max_bytes, and the disk's free floor kept after it, the oldest going
        first (whichever camera) until there is. False when nothing more can go and there still isn't."""
        with self._disk:
            while True:
                if self.total_bytes() + need <= self.max_bytes and self._free_after(need) >= self.free_floor:
                    return True
                with self._lock:
                    heads = [(h[0], cid, h[1]) for cid, c in self._cams.items() if (h := self._segs(c).head())]
                if not heads:
                    return False
                _, cid, key = min(heads)
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
            f = d / _name(key, t0, dur, len(body), brk)
            tmp = d / f".{f.stem}.{os.getpid()}.tmp" if self.shared else f.with_suffix(".tmp")
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
                c.setdefault("names", {}).pop(key, None)
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
                c.get("names", {}).pop(key, None)
                c.get("breaks", set()).discard(key)
        if f is not None:
            f.unlink(missing_ok=True)
        return s[2] if s else 0

    def total_bytes(self) -> int:
        """Every camera's recording together: each camera's running total (#446), never a pass over the segments."""
        with self._lock:
            return sum(self._segs(c).bytes for c in self._cams.values())

    def evict(self) -> int:
        """Older than the window: gone; then over max_bytes or under the free floor: the oldest first (_room)."""
        now = self.clock()
        freed = 0
        with self._disk:
            with self._lock:
                old = [(cid, key) for cid, c in self._cams.items() for key in self._segs(c).older(now - self.window_s)]
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
        """Camera id -> metres from the nearest open CAD event, for the cameras an event wants. Worked out again only
        when the open events or the cameras with video change (#446: every second, each event against every camera and
        the road picker, as events open through the day)."""
        out: dict[str, float] = {}
        from sizeup.roads import km
        try:
            events = self.incidents() or []
        except Exception as e:  # the CAD store busy or gone: no event cameras this round
            self.log(f"dvr: open events not read ({type(e).__name__})")
            events = []
        cams = [c for c in self.roads.merged("cameras") if c.get("video") and not c.get("off")]
        key = (tuple((ev.get("lat"), ev.get("lon"), ev.get("label", ""), ev.get("address", "")) for ev in events),
               tuple((c["id"], c["lat"], c["lon"]) for c in cams), self.near_m)
        if key == self._inc_cache[0]:
            return dict(self._inc_cache[1])
        whole = True
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
                along, whole = [], False
            for c in along:
                if c.get("rel") in ("at", "before", "after") and c.get("has_video"):
                    d = (c.get("km") or 0) * 1000
                    out[c["id"]] = min(out.get(c["id"], d), d)
        if whole:
            self._inc_cache = (key, dict(out))
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
        live = [c for c in self.roads.merged("cameras") if c.get("video") and not c.get("off") and CAM_RE.fullmatch(c["id"])]
        video = {c["id"] for c in live}
        why: dict[str, list[str]] = {}
        order: list[str] = []

        def add(cid: str, reason: str) -> None:
            if cid not in video or cid in self.parked:    # a parked camera's slot goes to the next one (#550)
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
        for cid in self.area_cameras(live):
            add(cid, "always")
        for cid in sorted(pins):
            add(cid, "pinned")
        for cid, _ in opened:
            add(cid, "open")
        return {cid: why[cid] for cid in order[:self.max_cameras]}

    def area_cameras(self, cams: list[dict] | None = None) -> list[str]:
        """The cameras with video recorded around the clock (#322), by id: the response area's (the camera wall's area,
        area_wall) or those inside the dvr_area override; none without either."""
        if self.area_wall:
            return self._wall_cameras()
        if not self.area:
            return []
        if cams is None:
            cams = [c for c in self.roads.merged("cameras") if c.get("video") and not c.get("off") and CAM_RE.fullmatch(c["id"])]
        return sorted(c["id"] for c in cams if c.get("lat") is not None and c.get("lon") is not None
                      and in_area(self.area, float(c["lat"]), float(c["lon"])))

    def _wall_cameras(self) -> list[str]:
        """The camera wall's area (Roads.area_rows: Settings > System decides it): its included cameras, and in "auto"
        those inside our fire districts or within its margin_m plus area_extra_m of them; never an excluded one. Only
        with live video. Worked out again every AREA_EVERY_S (a polygon distance per camera)."""
        now = self.clock()
        if now - self._area_at < AREA_EVERY_S:
            return self._area_ids
        rows_fn = getattr(self.roads, "area_rows", None)
        try:
            area, rows = rows_fn() if rows_fn is not None else ({}, [])
        except Exception as e:  # noqa: BLE001 - the area not readable now: the last one
            self.log(f"dvr: the response area not read ({type(e).__name__})")
            return self._area_ids
        reach = float(area.get("margin_m") or 0) + self.area_extra_m
        ids = []
        for r in rows:
            c = r["camera"]
            if not (c.get("video") and not c.get("off") and CAM_RE.fullmatch(c["id"])) or r["mode"] == "exclude":
                continue
            if r["mode"] == "include" or (r["dist_m"] is not None and r["dist_m"] <= reach):
                ids.append(c["id"])
        self._area_at, self._area_ids = now, sorted(ids)
        return self._area_ids

    # -- recording ---------------------------------------------------------------------------------------------------

    def _own_list(self, cid: str, cam: dict, c: dict, now: float) -> str | None:
        """This recorder's own session's chunklist: its master playlist asked once (again when a chunklist fails,
        at once); a camera that keeps failing waits, 15 s doubling to SESSION_RETRY_S."""
        if c.get("retry_at") and now < c["retry_at"]:
            return None
        base = cam["video"].rsplit("/", 1)[0] + "/"
        why, gone, short = "", False, ""
        for attempt in (1, 2):                            # the second: a new session at once
            code, master = None, False
            try:
                if not c.get("chunk") or attempt == 2:
                    master = True
                    code, _, body = self._get(cam["video"], 6.0, 1 << 20)
                    lines = [t.strip() for t in body.decode(errors="replace").splitlines()] if code == 200 else []
                    c["chunk"] = next((t for t in lines if t and not t.startswith("#")), "")
                    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}\.m3u8", c["chunk"] or ""):
                        raise ValueError("no chunklist named")
                master = False
                code, _, body = self._get(base + c["chunk"], 10.0, 1 << 20)
                if code != 200:
                    raise ValueError(f"HTTP {code}")
                c["fails"], c["retry_at"] = 0, 0.0
                c.pop("fail_since", None)
                c.pop("gone_since", None)
                return body.decode(errors="replace")
            except (HTTPError, URLError, OSError, ValueError) as e:
                c["chunk"] = ""
                why = f"{type(e).__name__}: {e}"
                gone = master and (getattr(e, "code", None) == 404 or code == 404)
                short = _short(e, code)
        # (#550) failing since; a master playlist that is a 404 every time since: gone, parked sooner
        c["fails"] = c.get("fails", 0) + 1
        since = c.setdefault("fail_since", now)
        if gone:
            c.setdefault("gone_since", now)
        else:
            c.pop("gone_since", None)
        if c["fails"] >= PARK_MIN_FAILS and (now - since >= PARK_AFTER_S or now - c.get("gone_since", now) >= PARK_404_S):
            self._park(cid, c, short, since, now)
            return None
        if c["fails"] >= FAILS_BEFORE_WAIT:
            wait_s = min(SESSION_RETRY_S, 15.0 * 2 ** (c["fails"] - FAILS_BEFORE_WAIT))
            c["retry_at"] = now + wait_s
            self.log(f"dvr: {cid}: no stream ({why}); {c['fails']} failures, trying again in {wait_s:.0f} s")
        return None

    def _get(self, url: str, timeout: float, limit: int) -> tuple[int, dict, bytes]:
        """This recorder's own session's fetches: over kept-alive connections (#446, sizeup/httpkeep.py) when the
        roads fetch with the plain client, else through the roads' own fetch (whatever was handed in: a test's)."""
        from sizeup.roads import http_get
        fetch = self.roads.fetch
        return httpkeep.get(url, timeout, limit) if fetch is http_get else fetch(url, timeout, limit)

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
                code, _, body = self._get(base + name, 10.0, 4 * 1024 * 1024)
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
        if cid in self.parked:                             # (#550) only its check asks it
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
            if self._stop.is_set() or (self.stopped and not self._room(1 << 20)) or not self.may_write():
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
        turn. The segments written. Nothing at all (no fetch, no write, no eviction) while this task may not write
        (shared storage: not the leader) or the index is still being read."""
        if not self.may_write():
            return 0
        if not self._scanned.is_set():
            return 0
        if self.shared and self._started_at is not None:
            now = self.clock()
            if now - self._started_at <= RESCAN_FOR_S and now - self._rescanned_at >= RESCAN_S:
                self._rescanned_at = now
                self._scan()                                   # what the old leader wrote (or evicted) since
        wanted = self.wanted()
        with self._lock:
            before = self._wanted
            self._wanted = wanted
        self._parked_tick()                                    # (#550) the parked cameras' checks, when due
        if set(wanted) != set(before):
            added, gone = set(wanted) - set(before), set(before) - set(wanted)
            with self._lock:
                said, self._back = self._back & added, self._back - added
                added, gone = added - said, gone - set(self.parked)   # (back, parked: said once already)
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
            self._started_at = self.clock()
            self._pool = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="dvr")
            self._thread = threading.Thread(target=self._run, name="dvr", daemon=True)
            self._thread.start()
            self.log(f"dvr: on, {self.window_s / 60:.0f} min per camera, at most {self.max_cameras} cameras and "
                     f"{self.max_bytes / 1024 ** 3:.1f} GB, in {self.root}"
                     + (f"; shared storage, {len(self.area_cameras())} cameras in the area always" if self.shared else ""))
        return self

    def scan_now(self) -> None:
        """Shared storage: read the index (the leader before this one's recording) before the first round."""
        if self._scanned.is_set():
            return
        t = time.monotonic()
        self._scan()
        self._rescanned_at = self.clock()
        self._scanned.set()
        with self._lock:
            n, cams = sum(len(c["segs"]) for c in self._cams.values()), len(self._cams)
        self.log(f"dvr: {n} segments of {cams} cameras found on shared storage in {time.monotonic() - t:.1f} s: appending")

    def stop(self) -> None:
        self._stop.set()
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = None

    def _run(self) -> None:
        if not self._scanned.is_set():
            try:
                self.scan_now()
            except Exception as e:  # pragma: no cover - an unreadable folder: recorded afresh
                self.log(f"dvr: the shared index not read ({type(e).__name__}: {e})")
                self._scanned.set()
        while not self._stop.is_set():
            t = self.clock()
            try:
                self.tick()
            except Exception as e:  # pragma: no cover
                self.log(f"dvr: {type(e).__name__}: {e}")
            self._stop.wait(1.0 if self._pool is not None else max(1.0, TICK_S - (self.clock() - t)))

    # -- parked (#550): a camera dead upstream, looked at now and then ------------------------------------------------

    def _park(self, cid: str, c: dict, why: str, since: float, now: float) -> None:
        """Failing since `since`: no more rounds, its slot freed, looked at every PARK_CHECK_S. One line (no exception's
        name in it: errwatch counts those, and a parked camera is not a hub fault)."""
        with self._lock:
            n = c.get("fails", 0)
            self.parked[cid] = {"why": why or "no stream", "since": since, "at": now, "checked": now,
                                "next": now + PARK_CHECK_S}
            self._back.discard(cid)
            for k in ("chunk", "retry_at", "fails", "fail_since", "gone_since"):
                c.pop(k, None)
        self.log(f"dvr: {cid} parked: {why or 'no stream'} for {(now - since) / 60:.0f} min ({n} tries); looked at "
                 f"every {PARK_CHECK_S / 60:.0f} min, its slot free meanwhile")
        self._save_parked()
        self.roads._changed()

    def _unpark(self, cid: str, how: str) -> None:
        with self._lock:
            p = self.parked.pop(cid, None)
            if p is None:
                return
            self._back.add(cid)
            c = self._cams.get(cid)
            for k in ("chunk", "retry_at", "fails", "fail_since", "gone_since"):
                (c or {}).pop(k, None)
        self.log(f"dvr: {cid} back ({how}) after {(self.clock() - p['at']) / 3600:.1f} h parked: recording when wanted")
        self._save_parked()
        self.roads._changed()

    def _parked_tick(self) -> None:
        """Each parked camera: back at once when someone watches it live and it plays; roads' own probe when it looked
        since we did (no second request to the provider); else its own check when due (on the pool when started)."""
        now = self.clock()
        with self._lock:
            parked = list(self.parked.items())
        saved = False
        for cid, p in parked:
            cam = self.roads.camera(cid)
            if cam is None or not cam.get("video"):
                continue                                       # not listed now (a source not read yet): as it is
            st = self.roads._streams.get(cid)
            if st is not None and st.get("list") and now - st.get("list_at", 0) < 10:
                self._unpark(cid, "watched live")
                continue
            probe = (getattr(self.roads, "_probe", None) or {}).get(cid)
            if probe is not None and probe["at"] > p["checked"]:
                if probe["ok"]:
                    self._unpark(cid, "roads' probe")
                    continue
                with self._lock:
                    p["checked"], p["next"] = probe["at"], max(p["next"], probe["at"] + PARK_CHECK_S)
                saved = True
            with self._lock:
                if now < p["next"] or cid in self._busy:
                    continue
                self._busy.add(cid)
                p["next"] = now + PARK_CHECK_S                 # (never twice while one is under way)
            if self._pool is not None:
                self._pool.submit(self._check, cid, cam)
            else:
                self._check(cid, cam)
        if saved:
            self._save_parked()

    def _check(self, cid: str, cam: dict) -> bool:
        """A parked camera looked at: its master playlist, then the chunklist it names (PROBE_TIMEOUT_S each, the same
        kept-alive fetches as its recording: _get; a connection that failed is closed there, never kept). True (and
        back) when both answer; else quietly the next check."""
        try:
            ok = False
            try:
                code, _, body = self._get(cam["video"], PROBE_TIMEOUT_S, 1 << 20)
                lines = [t.strip() for t in body.decode(errors="replace").splitlines()] if code == 200 else []
                chunk = next((t for t in lines if t and not t.startswith("#")), "")
                if re.fullmatch(r"[A-Za-z0-9_-]{1,80}\.m3u8", chunk):
                    code, _, body = self._get(cam["video"].rsplit("/", 1)[0] + "/" + chunk, PROBE_TIMEOUT_S, 1 << 20)
                    ok = code == 200 and b".ts" in (body or b"")
            except (HTTPError, URLError, OSError, ValueError):
                ok = False
            if ok:
                self._unpark(cid, "answered")
            else:
                with self._lock:
                    p = self.parked.get(cid)
                    if p is not None:
                        p["checked"] = self.clock()
                        p["next"] = p["checked"] + PARK_CHECK_S
                self._save_parked()
            return ok
        finally:
            with self._lock:
                self._busy.discard(cid)

    def _load_parked(self) -> None:
        """PARKED_FILE as the last leader left it: a restart keeps its parked cameras (none when there is none)."""
        try:
            raw = json.loads((self.root / PARKED_FILE).read_text())
        except (OSError, ValueError):
            return
        for cid, p in (raw.items() if isinstance(raw, dict) else ()):
            try:
                if CAM_RE.fullmatch(cid) and isinstance(p, dict):
                    self.parked[cid] = {"why": str(p.get("why") or "no stream")[:40],
                                        **{k: float(p[k]) for k in ("since", "at", "checked", "next")}}
            except (KeyError, TypeError, ValueError):
                continue

    def _save_parked(self) -> None:
        """PARKED_FILE written whole (a temp file renamed), only by the leader (may_write)."""
        if not self.may_write():
            return
        with self._lock:
            data = {cid: dict(p) for cid, p in self.parked.items()}
        f = self.root / PARKED_FILE
        tmp = self.root / f".parked.{os.getpid()}.tmp"
        with self._parked_lock:
            try:
                self.root.mkdir(parents=True, exist_ok=True)
                tmp.write_text(json.dumps(data, sort_keys=True))
                tmp.replace(f)
            except OSError as e:
                tmp.unlink(missing_ok=True)
                self.log(f"dvr: the parked list not written ({type(e).__name__})")

    def panel(self) -> dict:
        """Settings > Health's Camera DVR: cameras recording of the most, and the parked ones (oldest first)."""
        with self._lock:
            parked = sorted(({"id": cid, **p} for cid, p in self.parked.items()), key=lambda x: x["since"])
            n = len(self._wanted)
        return {"recording": 0 if self.stopped else n, "max": self.max_cameras, "stopped": self.stopped,
                "parked": parked, "check_min": PARK_CHECK_S / 60, "park_min": PARK_AFTER_S / 60,
                "park_404_min": PARK_404_S / 60}

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
            parked = self.parked.get(cid)
        gaps, prev = [], None
        for _, (t0, dur, _) in segs:
            if prev is not None and t0 - prev > GAP_S:
                gaps.append([round(prev, 3), round(t0, 3)])
            prev = t0 + dur
        return {"recording": bool(wanted) and not self.stopped, "why": wanted, "stopped": self.stopped if wanted else "",
                "pinned": pinned_at is not None, "pinned_until": round(pinned_at + PIN_S) if pinned_at else None,
                "from": round(segs[0][1][0], 3) if segs else None, "to": round(prev, 3) if prev else None,
                "segments": len(segs), "bytes": sum(s[2] for _, s in segs), "window_s": self.window_s,
                "gaps": gaps[-50:],
                "parked": {k: parked[k] for k in ("why", "since", "next")} if parked else None}

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

    def alias(self, cid: str) -> str:
        """The id the camera is listed under now (Roads.alias when the roads have it, #320/#324), else cid."""
        try:
            return self._alias(cid)
        except Exception:  # noqa: BLE001 - an id we can't map stays as it is
            return cid

    def hold(self, cid: str, on: bool) -> None:
        """A clip of this camera is being saved (sizeup/attachments.py): its folder must not be renamed meanwhile
        (a camera merge's follow(), #324, skips the cameras in self.holding)."""
        with self._lock:
            if on:
                self.holding[cid] = self.holding.get(cid, 0) + 1
            elif self.holding.get(cid, 0) > 1:
                self.holding[cid] -= 1
            else:
                self.holding.pop(cid, None)

    def _may_move(self, old: str, new: str) -> bool:
        """A recording folder may be renamed (a camera merge, #324): only by the leader, and never while a clip of
        either camera is being saved."""
        if not self.may_write():
            return False
        with self._lock:
            return not (self.holding.get(old) or self.holding.get(new))

    def read_segment(self, cid: str, key: int, fallback: Path | None = None) -> bytes:
        """A recorded segment's bytes, its file found when it is read (the camera's id may have moved meanwhile:
        alias), else fallback. OSError when it is gone."""
        for c in dict.fromkeys((self.alias(cid), cid)):
            f = self._file(c, key)
            if f is not None:
                try:
                    return f.read_bytes()
                except FileNotFoundError:
                    continue
        if fallback is None:
            raise FileNotFoundError(f"{cid}/{key}")
        return Path(fallback).read_bytes()

    def segments(self, cid: str, t_from: float, t_to: float) -> list[dict]:
        """The recorded segments that overlap [t_from, t_to], oldest first: {key, t0, dur, bytes, path} (a saved clip,
        sizeup/attachments.py: copied as they are)."""
        with self._lock:
            c = self._cams.get(cid)
            segs = sorted(c["segs"].items()) if c else []
        out = []
        for key, (t0, dur, size) in segs:
            if t0 + dur > t_from and t0 < t_to:
                f = self._file(cid, key)
                if f is not None:
                    out.append({"key": key, "t0": t0, "dur": dur, "bytes": size, "path": f,
                                "disc": self._new_stream(cid, key)})
        return out

    def _new_stream(self, cid: str, key: int) -> bool:
        """A segment that starts a new stream (a discontinuity even with no gap in time: its "_n" mark, #320)."""
        with self._lock:
            c = self._cams.get(cid)
            return bool(c) and key in c.get("breaks", ())

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


def _name(key: int, t0: float, dur: float, size: int, brk: bool = False) -> str:
    """A segment's file name: its key, start and duration (ms) and size (so the index is read without a stat), and
    "_n" when it starts a new stream (#320)."""
    return f"{key}_{int(t0 * 1000)}_{int(dur * 1000)}_{int(size)}{'_n' if brk else ''}.ts"


def _short(e: BaseException, code: int | None) -> str:
    """A failed try in a few words (a parked camera's line and Health): never an exception's name (errwatch counts
    those)."""
    if isinstance(e, TimeoutError) or isinstance(getattr(e, "reason", None), TimeoutError) or "timed out" in str(e):
        return "no answer"
    n = getattr(e, "code", None) or code
    if isinstance(n, int) and n != 200:
        return f"HTTP {n}"
    m = re.search(r"HTTP (\d{3})", str(e))
    return f"HTTP {m.group(1)}" if m else "no stream"
