"""The camera DVR (#277, sizeup/dvr.py): live cameras' HLS segments kept on the hub's disk on demand, a rolling window
per camera and a size cap over all, played back as a VOD playlist with the time of every segment.

No network: the Thruway's camera fixtures with a moving Wowza-like chunklist (tests/test_hlsseg.py Origin), the
segments built there (a real SPS, slice headers, TS packets).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from sizeup import dvr as dvr_mod
from sizeup.cad.api import CadApi, needs
from sizeup.dvr import Dvr
from tests.test_cad import make_cad
from tests.test_hlsseg import Origin, gop, segment
from tests.test_roads import Clock, load_cfg, make
from tests.test_roles import COREY, JULIAN, VAL, call, people

CAM = "1ne01370s"                       # the Thruway's I-95 exit 21 camera (HLS)
SNAP_ONLY = "1cw00250w"


def setup(tmp_path, **kw):
    clock = Clock()
    f = Origin()
    r = make(key="", urls={}, fetch=f, clock=clock, thruway=True)
    assert r.refresh("cameras")
    logs = []
    d = Dvr(r, tmp_path / "dvr", clock=clock, log=logs.append, free_floor=0, **kw)
    d.logs = logs
    r.dvr = d
    return r, d, f, clock


def step(d, f, clock, n=1, dt=4.0):
    for _ in range(n):
        f.advance()
        clock.t += dt
        d.tick()


def test_a_camera_someone_opens_is_recorded_from_the_same_segments_the_viewer_gets(tmp_path):
    r, d, f, clock = setup(tmp_path)
    assert d.tick() == 0 and d.wanted() == {}                          # nobody watching, nothing pinned: nothing
    assert r.get(f"/api/cad/cameras/{CAM}/stream/playlist.m3u8", "")[0] == 200
    r.pump_once(CAM)                                                    # the live stream's chunklist and segments
    f.urls.clear()
    assert d.tick() == 3 and d.wanted() == {CAM: ["open"]}
    assert not any(u.endswith(".ts") for u in f.urls)                   # the live pump's copies: fetched once
    st = d.status(CAM)
    assert st["recording"] and st["why"] == ["open"] and st["segments"] == 3 and st["to"] == clock.t
    assert st["to"] - st["from"] == pytest.approx(12.0) and st["gaps"] == []
    files = sorted(p.name for p in (tmp_path / "dvr" / CAM).iterdir())
    assert len(files) == 3 and all(re.fullmatch(r"\d+_\d{13}_4000_\d+\.ts", x) for x in files)
    # what is kept is what the viewer was served: checked and repaired
    seg = r.get(f"/api/cad/cameras/{CAM}/dvr/1226.ts", "")
    assert seg[0] == 200 and seg[2] == "video/MP2T" and seg[1] == r._segments[f"{CAM}/media_w1_1226.ts"]["body"]
    assert r.public()["cameras"][[c["id"] for c in r.public()["cameras"]].index(CAM)]["rec"] is True


def test_the_playlist_dates_every_segment_and_marks_a_gap(tmp_path):
    r, d, f, clock = setup(tmp_path)
    d.pins = {CAM}
    d.tick()
    step(d, f, clock, 2)
    clock.t += 120                                                      # the stream went away two minutes ...
    f.seq += 30
    d.tick()                                                            # ... and came back
    code, body, ctype, _ = r.get(f"/api/cad/cameras/{CAM}/dvr/index.m3u8", "")
    text = body.decode()
    assert code == 200 and ctype == "application/vnd.apple.mpegurl"
    assert "#EXT-X-PLAYLIST-TYPE:VOD" in text and text.rstrip().endswith("#EXT-X-ENDLIST")
    assert text.count("#EXT-X-PROGRAM-DATE-TIME:") == text.count(".ts") == 8
    assert text.count("#EXT-X-DISCONTINUITY") == 1 and "#EXT-X-MEDIA-SEQUENCE:1225" in text
    assert re.search(r"#EXT-X-PROGRAM-DATE-TIME:\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", text)
    st = json.loads(r.get(f"/api/cad/cameras/{CAM}/dvr", "")[1])
    assert len(st["gaps"]) == 1 and st["gaps"][0][1] - st["gaps"][0][0] > 100


def test_segments_follow_on_in_time_and_none_is_kept_twice(tmp_path):
    r, d, f, clock = setup(tmp_path)
    d.pins = {CAM}
    d.tick()
    step(d, f, clock, 3)
    segs = sorted(d._cams[CAM]["segs"].items())
    assert [s for s, _ in segs] == list(range(1225, 1231))
    assert all(abs(a[1][0] + a[1][1] - b[1][0]) < 1e-6 for a, b in zip(segs, segs[1:]))   # each begins where the last ended
    n = len(f.urls)
    clock.t += 1
    assert d.tick() == 0 and sum(u.endswith(".ts") for u in f.urls[n:]) == 0           # nothing new: nothing fetched


def test_its_own_session_when_nobody_watches_and_a_refusing_source_is_left_alone(tmp_path):
    r, d, f, clock = setup(tmp_path)
    d.pins = {CAM}
    f.urls.clear()
    assert d.tick() == 3
    assert f.urls[0].endswith("/playlist.m3u8") and f.urls[1].endswith("/chunklist_w1849562454.m3u8")
    assert not any(k.startswith(f"{CAM}/") for k in r._segments)            # never in the live viewers' cache
    # one chunklist failure: a new session at once (master, then chunklist), no hole
    real = Origin.__call__
    fail = {"n": 1}

    def flaky(url, timeout, limit):
        if "chunklist" in url and fail["n"]:
            fail["n"] -= 1
            raise OSError("timed out")
        return real(f, url, timeout, limit)
    r.fetch = flaky
    step(d, f, clock)
    assert d.status(CAM)["segments"] == 4 and d._cams[CAM]["fails"] == 0
    # the source refuses every time: three rounds, then it waits (15 s doubling), and says so once per wait
    r.fetch = lambda url, timeout, limit: (_ for _ in ()).throw(OSError("refused")) if "skyvdn" in url else real(f, url, timeout, limit)
    step(d, f, clock, 3)
    assert d._cams[CAM]["fails"] == 3 and d._cams[CAM]["retry_at"] == clock.t + 15
    assert sum("no stream" in m for m in d.logs) == 1
    n = len(d.logs)
    step(d, f, clock)                                                       # within the wait: not asked
    assert len(d.logs) == n


def test_a_broken_segment_is_a_hole_never_a_bad_copy(tmp_path):
    r, d, f, clock = setup(tmp_path)
    d.pins = {CAM}
    f.segs["media_w1_1226.ts"] = [b"\x47" * 1000, b"\x47" * 1000]
    assert d.tick() == 2 and 1226 not in d._cams[CAM]["segs"]
    assert any("media_w1_1226.ts not kept" in m for m in d.logs)


def test_an_opened_camera_records_on_a_while_after_its_viewer_closes(tmp_path):
    r, d, f, clock = setup(tmp_path, linger_s=600)
    r.get(f"/api/cad/cameras/{CAM}/stream/playlist.m3u8", "v=dlgabc")
    d.tick()
    r.close_stream(CAM, "dlgabc")
    clock.t += 300
    assert d.wanted() == {CAM: ["open"]}
    clock.t += 301
    assert d.wanted() == {}
    d.tick()
    assert d.status(CAM)["recording"] is False and d.status(CAM)["segments"] > 0      # kept for its window


def test_an_open_events_cameras_and_the_cap(tmp_path):
    r, d, f, clock = setup(tmp_path, near_m=500, max_cameras=2)
    cam = r.camera(CAM)
    d.incidents = lambda: [{"lat": cam["lat"] + 0.002, "lon": cam["lon"], "address": "I-95"}]   # ~220 m
    assert d.wanted() == {CAM: ["incident"]}
    d.incidents = lambda: [{"lat": cam["lat"] + 0.05, "lon": cam["lon"], "address": ""}]        # 5.5 km: no
    assert CAM not in d.wanted()
    d.incidents = lambda: 1 / 0                                        # the store busy: no event cameras this round
    assert d.wanted() == {} and any("open events not read" in m for m in d.logs)
    # the cap: always first, then an open event's (before the ones kept), then kept
    vids = [c["id"] for c in r.merged("cameras") if c.get("video")][:4]
    d.always = (vids[0],)
    d.pins = {vids[1], vids[2]}
    w = d.wanted()
    assert list(w) == [vids[0], vids[1]] and w[vids[0]] == ["always"]
    d.incidents = lambda: [{"lat": cam["lat"] + 0.002, "lon": cam["lon"], "address": ""}]
    assert list(d.wanted())[1] == CAM and d.wanted()[CAM][0] == "incident"
    # never a camera without video
    d.always = (SNAP_ONLY,) if not r.camera(SNAP_ONLY) or not r.camera(SNAP_ONLY).get("video") else ()
    assert SNAP_ONLY not in d.wanted()


def test_the_window_and_the_size_cap_and_the_free_disk(tmp_path):
    r, d, f, clock = setup(tmp_path, window_s=20)
    d.pins = {CAM}
    d.tick()
    step(d, f, clock, 3)
    segs = d._cams[CAM]["segs"]
    assert all(t0 >= clock.t - 20 for t0, _, _ in segs.values())       # older than the window: gone
    n = len(segs)
    one = next(iter(segs.values()))[2]
    d.max_bytes = one * 2
    d.evict()
    assert len(segs) == 2 and d.total_bytes() <= one * 2 and n > 2
    oldest = min(segs)
    assert not list((tmp_path / "dvr" / CAM).glob(f"{oldest - 1}_*.ts"))                # the files go too
    d.max_bytes, d.free_floor = 10 ** 12, 10 ** 15                      # a disk short of its floor: everything goes
    d.evict()
    assert d.total_bytes() == 0 and not list((tmp_path / "dvr" / CAM).glob("*.ts"))


def test_room_is_made_before_each_write_and_a_disk_under_its_floor_stops_recording(tmp_path):
    import shutil
    # a real floor above what the disk has free: nothing is written, and the status says why
    free = shutil.disk_usage(tmp_path).free
    r, d, f, clock = setup(tmp_path)
    d.free_floor = free + 10 ** 9
    d.pins = {CAM}
    assert d.tick() == 0 and d.total_bytes() == 0 and not list(tmp_path.glob("dvr/*/*.ts"))
    st = d.status(CAM)
    assert not st["recording"] and st["stopped"].startswith("Disk full") and d.recording(CAM) == []
    assert any("recording stopped: Disk full" in m for m in d.logs)
    n = len(f.urls)
    step(d, f, clock)
    assert not any(u.endswith(".ts") for u in f.urls[n:])                   # stopped: nothing fetched either
    d.free_floor = 0                                                         # room again
    step(d, f, clock)
    assert d.status(CAM)["recording"] and d.status(CAM)["segments"] > 0 and d.status(CAM)["stopped"] == ""
    # the cap: the oldest go before the write, never after
    one = max(s[2] for s in d._cams[CAM]["segs"].values())
    d.max_bytes = 2 * one
    seen = []
    orig = d._write
    d._write = lambda *a: (seen.append(d.total_bytes() + len(a[4])), orig(*a))[1]
    step(d, f, clock, 3)
    assert d.total_bytes() <= d.max_bytes and len(d._cams[CAM]["segs"]) <= 2


def test_a_disk_that_cant_be_read_writes_nothing_and_a_failed_write_leaves_no_temp_file(tmp_path, monkeypatch):
    import shutil
    r, d, f, clock = setup(tmp_path)
    d.free_floor = 1
    d.pins = {CAM}
    monkeypatch.setattr(shutil, "disk_usage", lambda p: (_ for _ in ()).throw(OSError("gone")))
    assert d._free() == 0 and d.tick() == 0 and d.total_bytes() == 0       # fail closed
    monkeypatch.undo()
    d.free_floor = 0
    import pathlib
    monkeypatch.setattr(pathlib.Path, "replace", lambda self, target: (_ for _ in ()).throw(OSError("disk error")))
    assert d.tick() == 0 and not list(tmp_path.glob("dvr/*/*.tmp")) and any("not written" in m for m in d.logs)


def test_a_sequence_that_starts_over_is_carried_on(tmp_path):
    r, d, f, clock = setup(tmp_path)
    d.pins = {CAM}
    d.tick()
    step(d, f, clock, 2)
    last = d._cams[CAM]["last"]
    f.seq = 10                                                              # the origin restarted: its counter too
    step(d, f, clock, 3)
    keys = sorted(d._cams[CAM]["segs"])
    assert keys[-1] > last and len(keys) == len(set(keys)) and d.status(CAM)["recording"]
    assert any("sequence started over" in m for m in d.logs)
    text = d.playlist(CAM).decode()
    assert text.count(".ts") == len(keys)
    # the new stream begins after a discontinuity (its timestamps start again: hls.js stalls scrubbing across it
    # otherwise), though it follows on in time; and the mark is on disk, so a restart keeps it
    first_new = min(k for k in keys if k > last)
    lines = text.splitlines()
    at = next(i for i, x in enumerate(lines) if x.startswith(f"{first_new}.ts?s="))
    assert text.count("#EXT-X-DISCONTINUITY") == 1 and "#EXT-X-DISCONTINUITY" in lines[at - 3:at]
    assert (tmp_path / "dvr" / CAM / d._file(CAM, first_new).name).name.endswith("_n.ts")
    # its URL (#321: ?s=<start ms>) finds the marked file; another start under that key is refused
    query = lines[at].split("?", 1)[1]
    assert r.get(f"/api/cad/cameras/{CAM}/dvr/{first_new}.ts", query)[0] == 200
    assert r.get(f"/api/cad/cameras/{CAM}/dvr/{first_new}.ts", "s=1")[0] == 404
    again = Dvr(r, tmp_path / "dvr", clock=clock, log=lambda m: None, free_floor=0)
    assert again.playlist(CAM).decode() == text
    # and a camera's first segments after a restart of the hub: nothing known of the one before, so a mark too
    again.pins = {CAM}
    clock.t += 4
    f.advance()
    again.tick()
    assert again.playlist(CAM).decode().count("#EXT-X-DISCONTINUITY") == 2


def test_cameras_are_recorded_side_by_side(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    r, d, f, clock = setup(tmp_path, max_cameras=3)
    vids = [c["id"] for c in r.merged("cameras") if c.get("video")][:3]
    d.pins = set(vids)
    d._pool = ThreadPoolExecutor(max_workers=3)
    try:
        assert d.tick(wait_all=True) == 9 and all(d.status(v)["segments"] == 3 for v in vids)
        assert d.tick(wait_all=True) == 0                                    # each camera's next round: TICK_S on
        assert d._busy == set()
    finally:
        d._pool.shutdown()


def test_a_restart_finds_what_is_on_disk(tmp_path):
    r, d, f, clock = setup(tmp_path)
    d.pins = {CAM}
    d.tick()
    (tmp_path / "dvr" / CAM / "1300_1.tmp").write_bytes(b"half")       # a write cut short by the restart
    again = Dvr(r, tmp_path / "dvr", clock=clock, log=lambda m: None, free_floor=0)
    assert again.status(CAM)["segments"] == 3 and again.status(CAM)["from"] == d.status(CAM)["from"]
    assert not (tmp_path / "dvr" / CAM / "1300_1.tmp").exists()


def test_pins_are_kept_where_the_app_says_and_only_for_video_cameras(tmp_path):
    r, d, f, clock = setup(tmp_path)
    saved = {}
    d.load_pins = lambda: saved.get("pins", [])
    d.save_pins = lambda pins: saved.update(pins=pins)
    st = d.pin(CAM, True)
    assert st["pinned"] and st["recording"] and st["why"] == ["pinned"] and saved["pins"] == {CAM: clock.t}
    assert st["pinned_until"] == clock.t + dvr_mod.PIN_S
    again = Dvr(r, tmp_path / "dvr", clock=clock, log=lambda m: None, free_floor=0)
    again.load_pins = d.load_pins
    again.start().stop()
    assert again.pins == {CAM: clock.t}                                 # a new process (a deploy): the pins carry over
    saved["pins"] = [CAM]                                               # the first shape (ids only) still reads
    again.start().stop()
    assert set(again.pins) == {CAM}
    # a Keep runs out after PIN_S
    clock.t += dvr_mod.PIN_S + 1
    assert CAM not in d.wanted() and saved["pins"] == {} and any("Keep ran out" in m for m in d.logs)
    d.pin(CAM, True)
    assert not d.pin(CAM, False)["pinned"] and saved["pins"] == {}
    with pytest.raises(KeyError):
        d.pin("nope", True)
    r.thruway.data["cameras"][1]["video"] = ""
    r._changed()
    with pytest.raises(ValueError, match="no live video"):
        d.pin(r.thruway.data["cameras"][1]["id"], True)


def test_the_paths_take_only_what_they_name(tmp_path):
    r, d, f, clock = setup(tmp_path)
    assert r.handles(f"/api/cad/cameras/{CAM}/dvr") and r.handles(f"/api/cad/cameras/{CAM}/dvr/12.ts")
    assert not r.handles(f"/api/cad/cameras/{CAM}/dvr/..%2F..%2Fx.ts") and not r.handles(f"/api/cad/cameras/{CAM}/dvr/a.ts")
    assert not r.handles("/api/cad/cameras/../dvr") and not r.handles("/api/cad/cameras/./dvr/index.m3u8")
    with pytest.raises(ValueError):
        d._dir("..")
    (tmp_path / "dvr").mkdir(exist_ok=True)
    (tmp_path / "dvr" / ".hidden").mkdir()
    (tmp_path / "dvr" / ".hidden" / "1_1791915000000_4000.ts").write_bytes(b"x")
    assert ".hidden" not in Dvr(r, tmp_path / "dvr", clock=clock, log=lambda m: None)._cams
    assert r.get(f"/api/cad/cameras/{CAM}/dvr/index.m3u8", "")[0] == 404                 # nothing recorded yet
    assert r.get(f"/api/cad/cameras/{CAM}/dvr/1.ts", "")[0] == 404
    assert r.get("/api/cad/cameras/nope/dvr", "")[0] == 404


def test_only_dispatchers_and_admins_keep_a_camera_recording_and_it_is_in_their_activity(tmp_path):
    a = CadApi(make_cad(tmp_path))
    a.cad.log = lambda m: None
    a.ids = people(a.cad)
    asked = []
    a.cad.dvr_pin = lambda cid, on: asked.append((cid, on)) or {"pinned": on}
    path = f"/api/cad/cameras/{CAM}/dvr/pin"
    assert needs(path) == "dispatch"
    code, out = call(a, path, {"on": True}, VAL)
    assert code == 403 and asked == []
    code, out = call(a, path, {"on": True}, JULIAN)
    assert code == 200 and out["dvr"] == {"pinned": True} and asked == [(CAM, True)]
    assert any(x["summary"] == f"Kept camera {CAM} recording" for x in a.cad.activity.rows(JULIAN, "", None, 20))
    code, out = call(a, path, {"on": "yes"}, COREY)
    assert code == 400 and "true or false" in out["error"]
    a.cad.dvr_pin = None
    assert call(a, path, {"on": False}, COREY)[0] == 400


def test_the_config(tmp_path):
    cfg = load_cfg(tmp_path, "")
    assert (cfg.roads_dvr, cfg.roads_dvr_window_s, cfg.roads_dvr_max_bytes, cfg.roads_dvr_max_cameras) == (
        True, 7200.0, 6 * 1024 ** 3, 12)
    cfg = load_cfg(tmp_path, '[roads]\ndvr_window_min = 60\ndvr_max_gb = 2.5\ndvr_always = ["1ne01370s"]\ndvr_near_m = 300\n')
    assert (cfg.roads_dvr_window_s, cfg.roads_dvr_max_bytes, cfg.roads_dvr_always, cfg.roads_dvr_near_m) == (
        3600.0, int(2.5 * 1024 ** 3), ("1ne01370s",), 300.0)
    from sizeup.config import ConfigError
    for bad in ("dvr = 1", "dvr_window_min = 5", "dvr_max_gb = 0", 'dvr_always = ["../x"]', "dvr_max_cameras = 99"):
        with pytest.raises(ConfigError, match="roads.dvr"):
            load_cfg(tmp_path, f"[roads]\n{bad}\n")
    assert dvr_mod.MAX_BYTES == 6 * 1024 ** 3 and dvr_mod.WINDOW_S == 7200


def test_scrubbing_seeks_while_dragging_from_segments_the_browser_may_keep(tmp_path):
    """#321: the viewer seeks as the handle moves (maplayers.js dvrScrub). It maps the handle's clock time to the player's
    time through the playlist's dates, and it fetches the segments next to the handle ahead so hls.js can take them
    from the browser's cache: the playlist must never be cached, and each segment must be cacheable."""
    r, d, f, clock = setup(tmp_path)
    d.pins = {CAM}
    d.tick()
    step(d, f, clock, 2)
    code, body, _, hdr = r.get(f"/api/cad/cameras/{CAM}/dvr/index.m3u8", "")
    assert code == 200 and hdr["Cache-Control"] == "no-store"
    names = re.findall(r"^(\d+)\.ts\?s=(\d{13})$", body.decode(), re.M)
    assert names and body.decode().count("#EXT-X-PROGRAM-DATE-TIME:") == len(names)
    for n, start in names:
        assert int(start) == int(d._cams[CAM]["segs"][int(n)][0] * 1000)
        code, seg, ctype, hdr = r.get(f"/api/cad/cameras/{CAM}/dvr/{n}.ts", f"s={start}")
        assert code == 200 and ctype == "video/MP2T" and seg[0] == 0x47
        assert hdr["Cache-Control"] == "private, max-age=3600"             # the same URL from cache, never public
    # a key the counter reused after a restart is another segment: the old URL (its start) is refused, never served
    n, start = names[0]
    assert r.get(f"/api/cad/cameras/{CAM}/dvr/{n}.ts", f"s={int(start) - 60000}")[0] == 404
    assert r.get(f"/api/cad/cameras/{CAM}/dvr/{n}.ts", "")[0] == 200
    ml = (Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static" / "maplayers.js").read_text()
    body = ml[ml.index("  // Scrubbing (#321)"):ml.index("  async function dvrKeep()")]
    assert "const SCRUB_MS = 150" in body and "requestAnimationFrame(" in body          # coalesced, throttled
    assert "v.seeking && since < 2000" in body                                          # none while one is in flight
    assert 'typeof v.fastSeek === "function"' in body                                   # the nearest keyframe where it can
    assert "autoStartLoad: false" in body and "enableInterstitialPlayback: false" in body and "hls.startLoad(m ? m.at" in body
    assert "maxMaxBufferLength = SCRUB_BUF" in body and "maxMaxBufferLength = 30" in body
    assert 'atEnd ? "Live" : clock(t)' in body and "return dvrToLive()" in body        # the end: Live
    assert "holdFrame()" in body and "startRefresh" not in body.split("function dvrToLive")[0]   # no snapshots while scrubbing
    assert '"touchmove", touchAt, { passive: false }' in ml and '"touchend", () => dvrScrubEnd()' in ml
    assert "r.width - 2 * inset" in ml                                                  # a finger: the thumb's inset
    assert '"pointerdown"' in ml and "setTimeout(dvrScrubEnd)" in ml                    # let go where it began: ended
    assert "PageUp: 60" in ml and "ArrowRight: 10" in ml and '"aria-valuetext"' in body
    assert "if (!viewer.dvrMode) { dvrReset(); dvrDraw(); return; }" in body             # Live while live: nothing new
    assert "viewer.dvrStarting !== go" in body                                          # a Live tap cancels a start
    live = ml[ml.index("  function startLive(id) {"):]
    assert live.index("viewer.hls.destroy()") < live.index("new Hls(")                   # one player at a time
