"""Replay: real 511NY CARS-Hub camera data through the hub's camera list and HLS proxy (GitHub #283; sizeup/roads.py).

The data is private and not in git: tests/replay.py (tools/replay_sync.py pull, or tools/capture_fixture.py camera).
Each set holds the CARS-Hub's CCTV inventory trimmed to our area, the Thruway's and the county's camera lists as
served the same minute, and for a few cameras the master playlist, chunklist and segments exactly as served.

  - every CARS-Hub camera in our area with live video stays on the list (itself, or the same camera from another
    source that has video too): 511NY cameras vanished from the list once;
  - no camera is listed twice (#320): no two listed cameras name the same stream, and a record merged away names
    (Roads.alias) one that is listed;
  - every recorded segment is whole MPEG-TS (188-byte packets, each with the sync byte; the PAT, the PMT and video
    frames in it), and the hub's proxy never serves one cut short: byte for byte, or (#276) repaired, its broken
    frames left out and the rest whole; the first segment asked for directly (a truncated first segment was a real
    bug) and the ones its pump fetches ahead.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.error import HTTPError

import pytest

from sizeup import hlsseg
from sizeup import roads as r
from sizeup.roads import BasicAuth, Roads

HUB = "https://hub.replay.invalid/hub/data/cctv.xml"
THRUWAY = "https://thruway.replay.invalid/cameras"
COUNTY = "https://county.replay.invalid/cameras"

pytestmark = pytest.mark.replay("camera")


class Clock:
    def __init__(self, t: float = 1_800_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


class ReplayFetch:
    """The set's files by URL: the feeds at stand-in addresses, each stream at its own real address."""

    def __init__(self, s):
        self.bodies: dict[str, tuple[dict, bytes]] = {HUB: ({"content-type": "application/xml"}, s.file("cctv_area.xml").read_bytes())}
        for name, url in (("thruway_cameras.xml", THRUWAY), ("county_cameras.json", COUNTY)):
            if s.file(name).is_file():
                self.bodies[url] = ({"content-type": "application/octet-stream"}, s.file(name).read_bytes())
        self.streams = json.loads(s.file("streams.json").read_text())
        for st in self.streams:
            base = st["video"].rsplit("/", 1)[0] + "/"
            for f in st.get("files", []):
                body = s.file(f"streams/{st['id']}/{f['name']}").read_bytes()
                url = st["video"] if f["name"] == "playlist.m3u8" else base + f["name"]
                ctype = "video/MP2T" if f["name"].endswith(".ts") else "application/vnd.apple.mpegurl"
                self.bodies[url] = ({"content-type": ctype, "content-length": str(len(body))}, body)
        self.urls: list[str] = []

    def __call__(self, url, timeout, limit, headers=None):
        self.urls.append(url)
        base = url.split("?")[0]
        if base not in self.bodies and base.endswith(".ts"):
            # a segment the chunklist names that the capture didn't keep (a set may hold only its last two): the
            # origin's answer once a segment has rolled off; the pump skips it
            raise HTTPError(base, 404, "not recorded", None, None)
        if base not in self.bodies:
            raise AssertionError("a fetch the recording doesn't have")       # (the address not printed: private)
        h, body = self.bodies[base]
        over = len(body) > limit
        assert not over, f"a recorded body of {len(body)} bytes is over the limit {limit}"
        return 200, dict(h), body


def make(fetch: ReplayFetch, with_ours: bool, cache: Path | None = None, read: bool = True) -> Roads:
    """A hub's Roads with the CARS-Hub on (and, with_ours, the Thruway's and the county's lists when recorded),
    each camera list read from the recording; read=False: nothing read (a restart: only the disk cache)."""
    rd = Roads("", {}, fetch=fetch, cache=cache, log=lambda m: None, clock=Clock(), gazetteer=Path("/nonexistent"),
               thruway=with_ours and THRUWAY in fetch.bodies, thruway_urls={"cameras": THRUWAY},
               county=with_ours and COUNTY in fetch.bodies, county_urls={"cameras": COUNTY},
               auth=BasicAuth("replay-user", "replay-pass"), carshub=True, carshub_url=HUB)
    rd.pump_threads = False
    for p in rd.providers if read else ():
        if p.feed_on("cameras"):
            assert rd.refresh("cameras", p), f"{p.name}: the recorded camera list didn't read"
    return rd


def ts_whole(body: bytes) -> list[str]:
    """What is wrong with an MPEG-TS segment, [] when it is whole."""
    bad = []
    if not body:
        return ["empty"]
    if len(body) % 188:
        bad.append(f"{len(body)} bytes: not whole 188-byte packets ({len(body) % 188} over)")
    if any(body[i] != 0x47 for i in range(0, len(body) - len(body) % 188, 188)):
        bad.append("a packet without the 0x47 sync byte")
    pids = {((body[i + 1] & 0x1F) << 8) | body[i + 2] for i in range(0, len(body) - 187, 188)}
    if 0 not in pids:
        bad.append("no PAT")
    if r.ts_frames(body) < 1:
        bad.append("no video frame")
    return bad


def served_as_recorded(rd: Roads, s, cid: str, name: str, body: bytes, label: str) -> None:
    """A served segment is never cut short: whole MPEG-TS, and the recorded bytes exactly unless the hub repaired it
    (#276, sizeup/hlsseg.py: frames whose reference chain the origin broke are left out, the rest kept whole); a
    repaired one is still complete by hlsseg.check, and the hub's record says how many frames it dropped."""
    want = s.file(f"streams/{cid}/{name}").read_bytes()
    bad = ts_whole(body)
    assert not bad, f"{label} {name}: served {bad}"
    hit = rd._segments.get(f"{cid}/{name}") or {}
    if body == want:
        return
    repaired = hit.get("dropped", 0) > 0 and not hit.get("noplay")
    assert repaired, f"{label} {name}: served {len(body)} bytes, not the {len(want)} recorded, and not a repair"
    info = hlsseg.check(body)
    shorter = len(body) < len(want)
    assert shorter and info["frames"] > 0, f"{label} {name}: a repair of {len(want)} bytes served as {len(body)}"


def test_every_carshub_camera_with_video_stays_on_the_list(replay_sets, tmp_path):
    for s in replay_sets("camera"):
        fetch = ReplayFetch(s)
        area = r.parse_carshub_cctv(fetch.bodies[HUB][1], r.DEFAULT_BBOX)
        with_video = [c for c in area if c["video"]]
        assert with_video, f"{s.name}: no camera with video in the recording"
        for with_ours in (False, True):
            cache = tmp_path / f"{s.path.name}-{with_ours}.json"
            fresh = make(fetch, with_ours, cache)
            # ... and after a restart, from the hub's disk cache alone (nothing fetched)
            offline = ReplayFetch(s)
            restarted = make(offline, with_ours, cache, read=False)
            fetched = len(offline.urls)
            assert fetched == 0, f"{fetched} fetches after a restart (from the disk cache alone: none)"
            for when, rd in (("read", fresh), ("restarted", restarted)):
                listed = rd.merged("cameras")
                ids = {c["id"] for c in listed}
                kept = {c["id"]: c for c in listed}
                gone = [c["id"] for c in with_video
                        if c["id"] not in ids and not (kept.get(rd.alias(c["id"])) or {}).get("has_video")
                        and not any(o.get("has_video") and r.same_camera(c, o) for o in listed)]
                assert not gone, (f"{s.name} ({when}, ours too: {with_ours}): CARS-Hub cameras with video missing "
                                  f"from the list: {gone}")
                if not with_ours:
                    ours = [c["id"] for c in area if rd.alias(c["id"]) in ids]
                    assert len(ours) == len(area), f"{s.name} ({when}): {len(ours)} CARS-Hub cameras listed of {len(area)}"


def test_no_two_listed_cameras_share_a_stream(replay_sets):
    """#320: one camera listed twice (two sources' records, or one source's two) is two tiles, two streams open and two
    of the DVR's slots. Every listed camera's stream (its HLS folder, or a still named for one) is its own."""
    for s in replay_sets("camera"):
        fetch = ReplayFetch(s)
        for with_ours in (False, True):
            rd = make(fetch, with_ours)
            listed = rd.merged("cameras")
            seen: dict[str, str] = {}
            twice = []
            for c in listed:
                for k in r.stream_keys(c):
                    if k in seen:
                        twice.append((seen[k], c["id"]))
                    seen[k] = c["id"]
            assert not twice, f"{s.name} (ours too: {with_ours}): {len(twice)} pairs of listed cameras share a stream"
            # nor one source's two records of a camera: the same name in other words at one route and milepost (#320:
            # the CARS-Hub's older and newer records, each with its own stream)
            places: dict[tuple, str] = {}
            same = 0
            for c in listed:
                key = (c.get("source"), r.name_key(c["name"]), r.road_key(c), c.get("mile"))
                if len(key[1]) >= 2 and key[3]:
                    same += key in places
                    places[key] = c["id"]
            assert not same, f"{s.name} (ours too: {with_ours}): {same} cameras listed twice by one source"
            ids = [c["id"] for c in listed]
            assert len(ids) == len(set(ids)), f"{s.name}: an id listed twice"
            for gone, kept in rd.aliases().items():
                assert kept in ids, f"{s.name}: a merged-away camera names one not listed"


def test_recorded_segments_are_whole_and_the_proxy_never_serves_one_cut_short(replay_sets):
    checked = 0
    for s in replay_sets("camera"):
        fetch = ReplayFetch(s)
        rd = make(fetch, with_ours=False)
        for st in fetch.streams:
            names = [f["name"] for f in st.get("files", [])]
            segs = [n for n in names if n.endswith(".ts")]
            if not segs:
                continue
            label = f"{s.name} {st['id']}"
            for n in segs:
                body = s.file(f"streams/{st['id']}/{n}").read_bytes()
                bad = ts_whole(body)
                assert not bad, f"{label} {n}: {bad}"
                recorded = next(f for f in st["files"] if f["name"] == n)["what"]
                if "content-length " in recorded and recorded.rsplit(" ", 1)[-1].isdigit():
                    same = int(recorded.rsplit(" ", 1)[-1]) == len(body)
                    assert same, f"{label} {n}: not its Content-Length"
            assert rd.camera(st["id"]) is not None, f"{label}: not on the list"
            code, master, _, _ = rd.stream(st["id"], "playlist.m3u8", "v1")
            assert code == 200, f"{label}: master playlist HTTP {code}"
            chunk = next(t.split("?")[0] for t in master.decode().splitlines() if t and not t.startswith("#"))
            code, chunklist, _, _ = rd.stream(st["id"], chunk)
            assert code == 200, f"{label}: chunklist HTTP {code}"
            listed = [t for t in chunklist.decode().splitlines() if t and not t.startswith("#")]
            same = listed[-len(segs):] == segs
            assert same, f"{label}: the hub's chunklist names other segments"
            # the first segment, asked for directly (before the pump has it), then the pump's fetches ahead
            first = segs[0]
            code, body, ctype, _ = rd.stream(st["id"], first)
            assert code == 200 and ctype == "video/MP2T", f"{label} {first}: {code} {ctype}"
            served_as_recorded(rd, s, st["id"], first, body, label)
            rd.pump_once(st["id"])
            for n in segs:
                code, body, _, _ = rd.stream(st["id"], n)
                assert code == 200, f"{label} {n}: HTTP {code}"
                served_as_recorded(rd, s, st["id"], n, body, label)
            status = rd.stream_status(st["id"])
            ok = status["open"] and status["target_s"] and status["target_s"] >= 1
            assert ok, f"{label}: stream status open={status['open']} target_s={status['target_s']}"
            checked += 1
    assert checked, "no recorded stream with segments"


def _frame_nums(body: bytes) -> list[tuple[bool, int, int, float | None]]:
    """(idr, nal_ref_idc, frame_num, pts) of each picture, read with hlsseg's own parsers (its SPS carried in)."""
    vpid, _ = hlsseg._streams(body)
    out, sps = [], None
    for p in hlsseg._pes(body, vpid):
        es, pts, _ = hlsseg._pes_es(p)
        for n in hlsseg.nal_units(es):
            t = n[0] & 0x1F
            if t == 7:
                sps = hlsseg.parse_sps(n) or sps
            elif t in (1, 5) and sps:
                h = hlsseg._slice_head(n, sps)
                if h:
                    out.append((t == 5, (n[0] >> 5) & 3, h[2], pts))
                break
    return out


def test_long_pts_steps_are_frame_num_gaps_and_counted_once(replay_sets):
    """(#308) frame_num is 4 bits, so check also reads a PTS gap long enough to hide a wrap as lost frames. In the
    recordings every longer PTS step (a 30 fps camera's 100 ms ones) lines up with a frame_num gap: the timing is
    even, the steps are lost frames. Checked in order, the state carried, each stream counts exactly the frame_num
    gaps (once per reference frame before them), no more."""
    uneven = 0
    for s in replay_sets("camera"):
        fetch = ReplayFetch(s)
        for st in fetch.streams:
            segs = sorted((f["name"] for f in st.get("files", []) if f["name"].endswith(".ts")),
                          key=lambda n: int(n.rsplit("_", 1)[1][:-3]))
            state, prev_ref, counted = None, None, False
            for n in segs:
                body = s.file(f"streams/{st['id']}/{n}").read_bytes()
                info = hlsseg.check(body, state)
                state = info["state"]
                want, mx = 0, 1 << (info["sps"] or {}).get("log2_max_frame_num", 4)
                pics = _frame_nums(body)
                steps = sorted(b[3] - a[3] for a, b in zip(pics, pics[1:]) if a[3] is not None and b[3] is not None)
                mid = steps[len(steps) // 2] if steps else 0
                for a, b in zip(pics, pics[1:]):
                    if a[3] is not None and b[3] is not None and b[3] - a[3] > 1.5 * mid and not b[0]:
                        # a long PTS step is lost frames that frame_num shows too (the camera's timing is even)
                        uneven += 1
                        assert b[2] not in (a[2], (a[2] + 1) % mx), (
                            f"{s.name} {st['id']} {n}: a {b[3] - a[3]:.3f} s step with frame_num {a[2]} -> {b[2]}")
                for idr, ref, fn, _ in pics:
                    if idr:
                        prev_ref, counted = fn, False
                        continue
                    if prev_ref is not None and fn not in (prev_ref, (prev_ref + 1) % mx) and not counted:
                        want += (fn - prev_ref - 1) % mx
                        counted = True
                    if ref:
                        prev_ref, counted = fn, False
                assert info["lost"] == want, f"{s.name} {st['id']} {n}: lost {info['lost']}, frame_num gaps {want}"
    assert uneven, "no recorded segment with a long PTS step"
