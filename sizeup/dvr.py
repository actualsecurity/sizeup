"""Camera DVR (#277): the live cameras' HLS segments kept on the hub's disk for a rolling window, so the viewer can
scrub back. Copies, no re-encoding: each segment as the hub serves it live (checked whole and repaired, #276,
sizeup/hlsseg.py), one file per segment.

What is recorded, on demand only (Dvr.wanted, at most max_cameras at once, in this order):
  always    [roads] dvr_always: camera ids recorded all the time
  pinned    a dispatcher's "Keep recording" in the viewer (POST /api/cad/cameras/<id>/dvr/pin): until unpinned
            (kept in the CAD store's meta, so a deploy keeps the pins)
  incident  an open CAD event's cameras: the ones just before / at / after it along the road (Roads.cameras_for)
            and any within dvr_near_m as the crow flies
  open      a camera someone is watching live, and for linger_s after the last viewer closed
Only cameras with live video; a snapshot-only camera (the county's) has nothing to copy.

The window: window_s (default 2 h) per camera; older segments go. And a size cap over every camera (max_bytes,
default 6 GB) and a floor of free disk (FREE_FLOOR_BYTES): the oldest go first, whichever camera. Measured
2026-11-07 on the 511NY cameras: about 200 kbit/s, so 175 MB per camera per 2 h; 12 cameras at once, about 2.1 GB.

Where a segment came from: the camera's own HLS session, or the live stream's when one is open (Roads._streams: the
same segments, fetched once). Segments are named by the stream's media sequence (Wowza's counter runs on across
sessions), so neither a new session nor the two sources ever double one. Its time: when it first appears in the
chunklist (Wowza lists a segment once it is whole), less the durations of those after it, carried on from the one
before when they follow each other.

HTTP (behind Cloudflare Access with the rest of /api/cad; nothing here is public, no share links):
  GET  /api/cad/cameras/<id>/dvr                   {recording, why: [always | pinned | incident | open], pinned,
                                                    from, to, segments, bytes, window_s, gaps: [[from, to], ...]}
  GET  /api/cad/cameras/<id>/dvr/index.m3u8         the recording as a VOD playlist: EXT-X-PROGRAM-DATE-TIME on every
                                                    segment (the viewer's timestamp), EXT-X-DISCONTINUITY at a gap
  GET  /api/cad/cameras/<id>/dvr/<seq>.ts           a recorded segment
  POST /api/cad/cameras/<id>/dvr/pin {on}           (sizeup/cad/api.py, dispatchers and admins) keep recording / stop

A deploy (sizeup/handoff.py): the recordings are on the task's ephemeral disk, so a new task starts with none. The old
task records until it drains; the new one starts recording when its app serves (TAKEOVER): the window restarts
empty and there's a gap of the handoff's length. The pins are in the CAD store (Litestream): they carry over.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError

from sizeup import hlsseg

WINDOW_S = 7200.0
MAX_BYTES = 6 * 1024 ** 3
FREE_FLOOR_BYTES = 3 * 1024 ** 3     # never leave less free on the disk (the SQLite files, their standby copies)
NEAR_M = 1000.0
LINGER_S = 1800.0                    # an opened camera records on this long after its last viewer closed
MAX_CAMERAS = 12
TICK_S = 4.0                         # each recorded camera's chunklist: this often (a viewer's player: about the same)
GAP_S = 1.5                          # segments further apart than this: a discontinuity in the playlist
SESSION_RETRY_S = 300.0              # a camera whose stream refused: not asked again for this long
SEQ_RE = re.compile(r"_(\d{1,12})\.ts$")
FILE_RE = re.compile(r"(\d{1,12})_(\d{10,16})_(\d{1,9})\.ts")      # seq, start (ms), duration (ms)
DVR_PATH = re.compile(r"/api/cad/cameras/([A-Za-z0-9_.:-]{1,64})/dvr(?:/(index\.m3u8|\d{1,12}\.ts))?")
JSON = "application/json"
REASONS = ("always", "pinned", "incident", "open")


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
                 free_floor: int = FREE_FLOOR_BYTES):
        self.roads, self.root = roads, Path(root)
        self.window_s, self.max_bytes, self.near_m = float(window_s), int(max_bytes), float(near_m)
        self.always, self.linger_s, self.max_cameras = tuple(always), float(linger_s), int(max_cameras)
        self.clock, self.log, self.free_floor = clock, log, int(free_floor)
        # the app hands in: open CAD events [{lat, lon, address, label}], and where the pins are kept
        self.incidents: Callable[[], list[dict]] = list
        self.load_pins: Callable[[], list[str]] = list
        self.save_pins: Callable[[list[str]], None] = lambda pins: None
        self.pins: set[str] = set()
        self._lock = threading.Lock()
        self._cams: dict[str, dict] = {}          # cid -> {segs: {seq: (t0, dur, bytes)}, last: seq, end, base, chunk,...}
        self._opened: dict[str, float] = {}       # cid -> last time someone watched it live
        self._wanted: dict[str, list[str]] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._scan()

    # -- the disk ----------------------------------------------------------------------------------------------------

    def _dir(self, cid: str) -> Path:
        return self.root / cid

    def _scan(self) -> None:
        """What is already on disk (the app restarted within the same task): the index from the file names."""
        if not self.root.is_dir():
            return
        for d in self.root.iterdir():
            if not d.is_dir() or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", d.name):
                continue
            segs = {}
            for f in d.iterdir():
                m = FILE_RE.fullmatch(f.name)
                if m:
                    segs[int(m.group(1))] = (int(m.group(2)) / 1000, int(m.group(3)) / 1000, f.stat().st_size)
                elif f.suffix == ".tmp":
                    f.unlink(missing_ok=True)
            if segs:
                last = max(segs)
                self._cams[d.name] = {"segs": segs, "last": last, "end": segs[last][0] + segs[last][1]}

    def _file(self, cid: str, seq: int) -> Path | None:
        c = self._cams.get(cid)
        s = c and c["segs"].get(seq)
        if not s:
            return None
        return self._dir(cid) / f"{seq}_{int(s[0] * 1000)}_{int(s[1] * 1000)}.ts"

    def _write(self, cid: str, seq: int, t0: float, dur: float, body: bytes) -> None:
        d = self._dir(cid)
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{seq}_{int(t0 * 1000)}_{int(dur * 1000)}.ts"
        tmp = f.with_suffix(".tmp")
        tmp.write_bytes(body)
        tmp.replace(f)                              # whole or not at all: a reader never sees a part
        with self._lock:
            c = self._cams.setdefault(cid, {"segs": {}, "last": None, "end": None})
            c["segs"][seq] = (t0, dur, len(body))
            if c["last"] is None or seq > c["last"]:
                c["last"], c["end"] = seq, t0 + dur

    def _remove(self, cid: str, seq: int) -> int:
        f = self._file(cid, seq)
        with self._lock:
            c = self._cams.get(cid)
            s = c["segs"].pop(seq, None) if c else None
        if f is not None:
            f.unlink(missing_ok=True)
        return s[2] if s else 0

    def total_bytes(self) -> int:
        with self._lock:
            return sum(s[2] for c in self._cams.values() for s in c["segs"].values())

    def evict(self) -> int:
        """Older than the window: gone. Then over max_bytes, or the disk under its free floor: the oldest first,
        whichever camera. The bytes freed."""
        now = self.clock()
        freed = 0
        with self._lock:
            every = sorted((s[0], cid, seq) for cid, c in self._cams.items() for seq, s in c["segs"].items())
        for t0, cid, seq in every:
            if t0 < now - self.window_s:
                freed += self._remove(cid, seq)
        total = self.total_bytes()
        with self._lock:
            every = sorted((s[0], cid, seq) for cid, c in self._cams.items() for seq, s in c["segs"].items())
        for t0, cid, seq in every:
            if total <= self.max_bytes and self._free() >= self.free_floor:
                break
            n = self._remove(cid, seq)
            total -= n
            freed += n
        with self._lock:
            for cid in [cid for cid, c in self._cams.items() if not c["segs"]]:
                self._cams.pop(cid)
                try:
                    self._dir(cid).rmdir()
                except OSError:
                    pass
        return freed

    def _free(self) -> int:
        try:
            return shutil.disk_usage(self.root if self.root.exists() else self.root.parent).free
        except OSError:
            return 1 << 62

    # -- what to record ----------------------------------------------------------------------------------------------

    def pin(self, cid: str, on: bool) -> dict:
        cam = self.roads.camera(cid)
        if cam is None:
            raise KeyError(cid)
        if on and not cam.get("video"):
            raise ValueError("this camera has no live video to record")
        changed = (cid in self.pins) != bool(on)
        (self.pins.add if on else self.pins.discard)(cid)
        if changed:
            self.save_pins(sorted(self.pins))
            self.log(f"dvr: {cid} {'pinned' if on else 'unpinned'}")
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
        """Camera id -> why it is recorded, at most max_cameras: always, pinned, then the events' (nearest first),
        then the ones watched."""
        now = self.clock()
        for cid, st in list(self.roads._streams.items()):
            if now - st.get("last", 0) <= 30:
                self._opened[cid] = now
        for cid in [c for c, t in self._opened.items() if now - t > self.linger_s]:
            self._opened.pop(cid)
        video = {c["id"] for c in self.roads.merged("cameras") if c.get("video") and not c.get("off")}
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
            add(cid, "always")
        for cid in sorted(self.pins):
            add(cid, "pinned")
        for cid, _ in sorted(self._incident_cams().items(), key=lambda x: x[1]):
            add(cid, "incident")
        for cid, _ in sorted(self._opened.items(), key=lambda x: -x[1]):
            add(cid, "open")
        keep = order[:self.max_cameras]
        return {cid: why[cid] for cid in keep}

    # -- recording ---------------------------------------------------------------------------------------------------

    def _list(self, cid: str, cam: dict) -> tuple[str, str] | None:
        """(the stream's folder, its chunklist text): the live stream's when one is open (the pump's copy), else this
        recorder's own session (its master playlist asked once)."""
        st = self.roads._streams.get(cid)
        now = self.clock()
        base = cam["video"].rsplit("/", 1)[0] + "/"
        if st is not None and st.get("list") and now - st.get("list_at", 0) < 10:
            return st["base"], st["list"].decode(errors="replace")
        c = self._cams.setdefault(cid, {"segs": {}, "last": None, "end": None})
        if c.get("refused") and now - c["refused"] < SESSION_RETRY_S:
            return None
        try:
            if not c.get("chunk"):
                code, _, body = self.roads.fetch(cam["video"], 6.0, 1 << 20)
                lines = [t.strip() for t in body.decode(errors="replace").splitlines()] if code == 200 else []
                c["chunk"] = next((t for t in lines if t and not t.startswith("#")), "")
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}\.m3u8", c["chunk"] or ""):
                    raise ValueError("no chunklist named")
            code, _, body = self.roads.fetch(base + c["chunk"], 10.0, 1 << 20)
            if code != 200:
                raise ValueError(f"HTTP {code}")
        except (HTTPError, URLError, OSError, ValueError) as e:
            c["chunk"] = ""
            c["refused"] = now
            self.log(f"dvr: {cid}: no stream ({type(e).__name__}); trying again in {SESSION_RETRY_S:.0f} s")
            return None
        c.pop("refused", None)
        return base, body.decode(errors="replace")

    def record_once(self, cid: str) -> int:
        """One round for one camera: its chunklist, then every segment in it not yet kept. The number written."""
        cam = self.roads.camera(cid)
        if cam is None or not cam.get("video"):
            return 0
        got = self._list(cid, cam)
        if got is None:
            return 0
        base, text = got
        first, items = _chunklist(text)
        if not items:
            return 0
        now = self.clock()
        c = self._cams.setdefault(cid, {"segs": {}, "last": None, "end": None})
        # each one's end: now, less the durations of those after it (a segment is listed once it is whole)
        ends, t = [], now
        for _, dur in reversed(items):
            ends.append(t)
            t -= dur
        ends.reverse()
        n = 0
        for k, (name, dur) in enumerate(items):
            m = SEQ_RE.search(name)
            seq = int(m.group(1)) if m else first + k
            if seq in c["segs"] or (c["last"] is not None and seq <= c["last"]):
                continue
            t0 = ends[k] - dur
            if c["last"] is not None and seq == c["last"] + 1 and c["end"] is not None and abs(c["end"] - t0) < 3 * dur + 2:
                t0 = c["end"]                                  # the one before ends where this begins
            try:
                hit = self.roads._segment(cid, base, name)
            except (HTTPError, URLError, OSError, ValueError) as e:     # (hlsseg.Bad too): a hole, not a bad copy
                self.log(f"dvr: {cid}: {name} not kept ({type(e).__name__}: {e})")
                continue
            if hit.get("kept", 1) == 0:
                continue
            self._write(cid, seq, t0, dur, hit["body"])
            n += 1
        return n

    def tick(self) -> int:
        """One round over every camera wanted; then the eviction. The segments written."""
        wanted = self.wanted()
        if set(wanted) != set(self._wanted):
            added, gone = set(wanted) - set(self._wanted), set(self._wanted) - set(wanted)
            if added:
                self.log(f"dvr: recording {', '.join(f'{c} ({'/'.join(wanted[c])})' for c in sorted(added))}")
            if gone:
                self.log(f"dvr: stopped {', '.join(sorted(gone))} (the recording stays for its window)")
            self._wanted = wanted
            self.roads._changed()                              # the map's "rec" marks
        self._wanted = wanted
        n = 0
        for cid in wanted:
            if self._stop.is_set():
                break
            try:
                n += self.record_once(cid)
            except Exception as e:  # pragma: no cover - one camera's surprise never stops the others
                self.log(f"dvr: {cid}: {type(e).__name__}: {e}")
        self.evict()
        return n

    def start(self) -> Dvr:
        self.pins = {p for p in (self.load_pins() or []) if isinstance(p, str)}
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="dvr", daemon=True)
            self._thread.start()
            self.log(f"dvr: on, {self.window_s / 60:.0f} min per camera, at most {self.max_cameras} cameras and "
                     f"{self.max_bytes / 1024 ** 3:.1f} GB, in {self.root}")
        return self

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            t = self.clock()
            try:
                self.tick()
            except Exception as e:  # pragma: no cover
                self.log(f"dvr: {type(e).__name__}: {e}")
            self._stop.wait(max(1.0, TICK_S - (self.clock() - t)))

    # -- what the pages get ------------------------------------------------------------------------------------------

    def recording(self, cid: str) -> list[str]:
        return list(self._wanted.get(cid, []))

    def status(self, cid: str) -> dict:
        with self._lock:
            c = self._cams.get(cid)
            segs = sorted(c["segs"].items()) if c else []
        gaps, prev = [], None
        for seq, (t0, dur, _) in segs:
            if prev is not None and t0 - prev > GAP_S:
                gaps.append([round(prev, 3), round(t0, 3)])
            prev = t0 + dur
        return {"recording": cid in self._wanted, "why": self.recording(cid), "pinned": cid in self.pins,
                "from": round(segs[0][1][0], 3) if segs else None, "to": round(prev, 3) if prev else None,
                "segments": len(segs), "bytes": sum(s[2] for _, s in segs), "window_s": self.window_s,
                "gaps": gaps[-50:]}

    def playlist(self, cid: str) -> bytes | None:
        with self._lock:
            c = self._cams.get(cid)
            segs = sorted(c["segs"].items()) if c else []
        if not segs:
            return None
        target = max(1, int(max(s[1] for _, s in segs) + 0.999))
        out = ["#EXTM3U", "#EXT-X-VERSION:3", f"#EXT-X-TARGETDURATION:{target}", "#EXT-X-PLAYLIST-TYPE:VOD",
               f"#EXT-X-MEDIA-SEQUENCE:{segs[0][0]}"]
        prev = None
        for seq, (t0, dur, _) in segs:
            if prev is not None and abs(t0 - prev) > GAP_S:
                out.append("#EXT-X-DISCONTINUITY")
            pdt = datetime.fromtimestamp(t0, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            out += [f"#EXT-X-PROGRAM-DATE-TIME:{pdt}", f"#EXTINF:{dur:.3f},", f"{seq}.ts"]
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
        if self.roads.camera(cid) is None and cid not in self._cams:
            return 404, b'{"error":"no such camera"}', JSON, hdr
        if not what:
            return 200, json.dumps(self.status(cid)).encode(), JSON, hdr
        if what == "index.m3u8":
            body = self.playlist(cid)
            if body is None:
                return 404, b'{"error":"nothing recorded"}', JSON, hdr
            return 200, body, "application/vnd.apple.mpegurl", hdr
        f = self._file(cid, int(what[:-3]))
        try:
            body = f.read_bytes() if f is not None else None
        except OSError:
            body = None
        if body is None:
            return 404, b'{"error":"not recorded (or past the window)"}', JSON, hdr
        return 200, body, "video/MP2T", {"Cache-Control": "private, max-age=3600"}
