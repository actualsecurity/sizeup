"""The camera DVR on shared storage (#322, sizeup/dvr.py): our area's cameras 24/7 with a 24 h window on EFS, written only
by the lease-holding leader; a new leader reads the index the old one left and appends to it, while the old one may still
be draining. Without shared storage: as before.

No network: the Thruway's camera fixtures with a moving Wowza-like chunklist (tests/test_hlsseg.py Origin). The area's
real camera count: a replay test over the CARS-Hub's captured camera lists (tests/replay.py).
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime

import pytest

from sizeup import dvr as dvr_mod
from sizeup import dvrstate
from sizeup.config import ConfigError
from sizeup.dvr import Dvr, area_polygon, in_area, shared_mount
from sizeup.dvrstate import STATE_FILE
from tests.test_dvr import CAM, setup, step
from tests.test_roads import load_cfg


def around(r, cid, d=0.01):
    c = r.camera(cid)
    return [c["lon"] - d, c["lat"] - d, c["lon"] + d, c["lat"] + d]


def test_the_response_areas_cameras_are_always_recorded_on_shared_storage_only(tmp_path):
    """The camera wall's area (Settings > System) decides it: inside our area or within its margin plus dvr_extra_m,
    its includes, never its excludes; only cameras with video. On our own disk: as before (none)."""
    r, d, f, clock = setup(tmp_path, area=None)
    assert d.area_cameras() == [] and CAM not in d.wanted()                        # local disk: as before
    r, d, f, clock = setup(tmp_path, shared=True, area_wall=True, area_extra_m=0)
    d.scan_now()
    _, rows = r.area_rows()
    video = {c["id"] for c in r.merged("cameras") if c.get("video") and not c.get("off")}
    on_wall = sorted(x["camera"]["id"] for x in rows if x["in"] and x["camera"]["id"] in video)
    assert on_wall and d.area_cameras() == on_wall                                  # the wall's cameras with video
    assert all(d.wanted()[cid] == ["always"] for cid in on_wall)
    far = sorted((x for x in rows if not x["in"] and x["camera"]["id"] in video and x["dist_m"] is not None),
                 key=lambda x: x["dist_m"])
    nearest_out = far[0]
    # a little beyond the wall's margin: dvr_extra_m
    d2 = Dvr(r, tmp_path / "x2", clock=clock, log=lambda m: None, free_floor=0, shared=True, area_wall=True,
             area_extra_m=nearest_out["dist_m"] - r.wall_config()["margin_m"] + 1)
    assert nearest_out["camera"]["id"] in d2.area_cameras()
    # the same Settings govern both: an exclude leaves it out, an include brings one in, the margin moves it
    inc = far[-1]["camera"]["id"]
    r.wall_settings = lambda: {"margin_m": 0, "include": [inc], "exclude": [on_wall[0]]}
    clock.t += dvr_mod.AREA_EVERY_S + 1
    got = d.area_cameras()
    assert inc in got and on_wall[0] not in got
    assert all(x["dist_m"] == 0 for x in r.area_rows()[1] if x["camera"]["id"] in got and x["camera"]["id"] != inc)
    # an override polygon wins over the wall's area
    r, d, f, clock = setup(tmp_path / "y", shared=True, area_wall=True, area=around(r, CAM, 0.001))
    assert d.area_cameras() == [CAM]
    # an open event's cameras still come first in the cap
    others = [c for c in video if c != CAM]
    d.max_cameras = 1
    cam = r.camera(others[0])
    d.incidents = lambda: [{"lat": cam["lat"], "lon": cam["lon"], "address": ""}]
    assert list(d.wanted()) == [others[0]]


def test_a_polygon_and_a_bbox_are_the_same_ring():
    assert area_polygon([-1, -1, 1, 1]) == ((-1.0, -1.0), (1.0, -1.0), (1.0, 1.0), (-1.0, 1.0))
    ring = area_polygon([[0, 0], [4, 0], [4, 4], [2, 1], [0, 4]])                   # concave: the notch is out
    assert in_area(ring, 0.5, 1) and not in_area(ring, 3, 2) and not in_area(ring, 5, 1)
    assert area_polygon([]) == () and area_polygon(None) == ()


def test_only_the_leader_writes_or_deletes(tmp_path):
    lease = {"ok": False}
    area = around(setup(tmp_path / "x")[0], CAM)
    r, d, f, clock = setup(tmp_path, window_s=20, area=area, shared=True, may_write=lambda: lease["ok"])
    d.scan_now()
    old = tmp_path / "dvr" / CAM / "1000_1791000000000_4000_10.ts"                 # well past the window
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_bytes(b"0123456789")
    stale = tmp_path / "dvr" / CAM / ".x.1.tmp"
    stale.write_bytes(b"half")
    os.utime(stale, (time.time() - dvr_mod.STALE_TMP_S - 5,) * 2)
    d._scan()
    f.urls.clear()
    step(d, f, clock, 3)
    assert f.urls == [] and old.exists() and stale.exists()                        # the standby: nothing fetched, kept, evicted
    lease["ok"] = True
    d._scan()
    step(d, f, clock, 2)
    assert not old.exists() and not stale.exists() and d.status(CAM)["segments"] > 0


def test_a_new_leader_reads_the_index_in_the_background_and_appends(tmp_path):
    r, d, f, clock = setup(tmp_path, area=None)
    old = Dvr(r, tmp_path / "efs", clock=clock, log=lambda m: None, free_floor=0, area=around(r, CAM), shared=True)
    old.scan_now()
    step(old, f, clock, 3)
    before = old.status(CAM)
    assert before["segments"] >= 3
    new = Dvr(r, tmp_path / "efs", clock=clock, log=lambda m: None, free_floor=0, area=around(r, CAM), shared=True)
    assert not new.scanned() and new.status(CAM)["segments"] == 0 and new.tick() == 0   # nothing until it has read it
    new.scan_now()
    assert new.status(CAM)["segments"] == before["segments"] and new.status(CAM)["from"] == before["from"]
    last = max(new._cams[CAM]["segs"])
    f.urls.clear()
    step(new, f, clock, 1)
    keys = sorted(new._cams[CAM]["segs"])
    assert keys[-1] == last + 1 and len(keys) == len(set(keys))                     # appended after the last, none twice
    names = os.listdir(tmp_path / "efs" / CAM)
    assert len(names) == len(keys) and all(re.fullmatch(r"\d+_\d{13}_\d+_\d+(_n)?\.ts", n) for n in names)  # size: no stat
    # this task's own session is a new stream (#320's mark): a discontinuity, kept through a rescan
    assert new._file(CAM, last + 1).name.endswith("_n.ts") and new._new_stream(CAM, last + 1)
    pl = new.playlist(CAM).decode()
    assert pl.count(".ts") == len(keys) and "#EXT-X-DISCONTINUITY" in pl
    again = Dvr(r, tmp_path / "efs", clock=clock, log=lambda m: None, free_floor=0, shared=True)
    again.scan_now()
    assert again._new_stream(CAM, last + 1) and not again._new_stream(CAM, last)


def test_the_old_leader_draining_while_the_new_one_reads(tmp_path, monkeypatch):
    r, d, f, clock = setup(tmp_path, area=None)
    old = Dvr(r, tmp_path / "efs", clock=clock, log=lambda m: None, free_floor=0, area=around(r, CAM), shared=True)
    old.scan_now()
    step(old, f, clock, 3)
    new = Dvr(r, tmp_path / "efs", clock=clock, log=lambda m: None, free_floor=0, area=around(r, CAM), shared=True)
    # its temp file being written right now, and a segment it evicts while the new one lists the folder
    writing = tmp_path / "efs" / CAM / ".9999_1_4000.77.tmp"
    writing.write_bytes(b"in flight")
    gone = sorted((tmp_path / "efs" / CAM).glob("*.ts"))[1]
    real = os.scandir

    def racing(p):
        entries = list(real(p))
        if str(p).endswith(CAM):
            gone.unlink()                                                           # evicted between the listing and the stat
        return iter(entries)
    monkeypatch.setattr(dvr_mod.os, "scandir", racing)
    new.scan_now()
    monkeypatch.setattr(dvr_mod.os, "scandir", real)
    assert writing.exists()                                                         # the other writer's: left alone
    n = new.status(CAM)["segments"]
    # the old one writes on (it drains) and evicts one more after the first look: the rescans take both in
    step(old, f, clock, 1)
    first = min(old._cams[CAM]["segs"])
    old._remove(CAM, first)
    new._started_at, new._rescanned_at = clock.t, clock.t - dvr_mod.RESCAN_S
    new.tick(wait_all=True)
    keys = set(new._cams[CAM]["segs"])
    assert first not in keys and max(old._cams[CAM]["segs"]) in keys
    assert all(new._file(CAM, k).exists() for k in keys) and n >= 2
    # past RESCAN_FOR_S: no more listing of the folder
    calls = []
    monkeypatch.setattr(new, "_scan", lambda: calls.append(1))
    clock.t += dvr_mod.RESCAN_FOR_S + 1
    new.tick()
    assert calls == []


def test_a_restart_on_its_own_disk_is_as_before(tmp_path):
    r, d, f, clock = setup(tmp_path)
    d.pins = {CAM}
    d.tick()
    (tmp_path / "dvr" / CAM / "1300_1.tmp").write_bytes(b"half")
    legacy = tmp_path / "dvr" / CAM / "1200_1791914000000_4000.ts"                 # a name from before the size was in it
    legacy.write_bytes(b"x" * 188)
    marked = tmp_path / "dvr" / CAM / "1201_1791914004000_4000_n.ts"               # #320's mark, no size
    marked.write_bytes(b"y" * 376)
    both = tmp_path / "dvr" / CAM / "1202_1791914008000_4000_564_n.ts"             # size and mark
    both.write_bytes(b"z" * 564)
    again = Dvr(r, tmp_path / "dvr", clock=clock, log=lambda m: None, free_floor=0)
    assert again.scanned() and not (tmp_path / "dvr" / CAM / "1300_1.tmp").exists()
    segs = again._cams[CAM]["segs"]
    assert (segs[1200][2], segs[1201][2], segs[1202][2]) == (188, 376, 564)
    assert (again._file(CAM, 1200), again._file(CAM, 1201), again._file(CAM, 1202)) == (legacy, marked, both)
    assert not again._new_stream(CAM, 1200) and again._new_stream(CAM, 1201) and again._new_stream(CAM, 1202)
    import re as _re
    old_re = _re.compile(r"(\d{1,15})_(\d{10,16})_(\d{1,9})(_n)?\.ts")             # an older dvr.py (#324) meeting new names
    assert old_re.fullmatch(both.name) is None and old_re.fullmatch(dvr_mod._name(1, 1791914000, 4, 99)) is None


def test_shared_mount_reads_the_mount_table(tmp_path):
    mounts = tmp_path / "mounts"
    mounts.write_text("overlay / overlay rw 0 0\n"
                      "127.0.0.1:/ /data/dvr nfs4 rw,vers=4.1 0 0\n"
                      "tmpfs /run/sizeup-shared tmpfs rw 0 0\n")
    assert shared_mount("/data/dvr", str(mounts)) == "nfs4"
    assert shared_mount("/data/dvr/1ne01370s", str(mounts)) == "nfs4"
    assert shared_mount("/data", str(mounts)) == "" and shared_mount("/data/dvrx", str(mounts)) == ""
    assert shared_mount("/data/dvr", str(tmp_path / "none")) == ""


def test_the_config(tmp_path):
    c = load_cfg(tmp_path, "")
    assert (c.roads_dvr_shared, c.roads_dvr_area, c.roads_dvr_extra_m, c.roads_dvr_shared_window_s,
            c.roads_dvr_shared_max_bytes, c.roads_dvr_shared_max_cameras) == ("auto", None, 1000.0, 43200.0,
                                                                              25 * 1024 ** 3, 20)
    assert (c.roads_dvr_window_s, c.roads_dvr_max_bytes, c.roads_dvr_max_cameras) == (7200.0, 6 * 1024 ** 3, 12)
    c = load_cfg(tmp_path, "[roads]\ndvr_shared = true\ndvr_area = [-73.7, 40.9, -73.6, 41.0]\ndvr_shared_window_min = 1440\n"
                           "dvr_extra_m = 0\n")
    assert c.roads_dvr_shared == "on" and c.roads_dvr_area == area_polygon([-73.7, 40.9, -73.6, 41.0])
    assert c.roads_dvr_shared_window_s == 86400.0 and c.roads_dvr_extra_m == 0.0
    c = load_cfg(tmp_path, "[roads]\ndvr_shared = false\ndvr_area = [[-73.7, 40.9], [-73.6, 40.9], [-73.65, 41.0]]\n")
    assert c.roads_dvr_shared == "off" and len(c.roads_dvr_area) == 3
    assert load_cfg(tmp_path, "[roads]\ndvr_area = []\n").roads_dvr_area == ()
    for bad in ('dvr_shared = "maybe"', "dvr_area = [1, 2]", "dvr_area = [-73.6, 40.9, -73.7, 41.0]",
                "dvr_area = [[0, 0], [1, 1]]", "dvr_area = [-80, 30, -70, 40]", "dvr_shared_window_min = 5000",
                "dvr_shared_max_cameras = 99", "dvr_extra_m = -1", "dvr_extra_m = 9000"):
        with pytest.raises(ConfigError, match="roads.dvr"):
            load_cfg(tmp_path, f"[roads]\n{bad}\n")


def test_the_standby_never_makes_a_dvr(tmp_path):
    """prewarm (the standby's warm app) builds the map providers but never the DVR: that is App.__init__'s, after the
    takeover, with the fence."""
    import sizeup.app as app_mod
    cfg = load_cfg(tmp_path, "")
    prov = app_mod.make_providers(cfg, None)
    assert prov["roads"].dvr is None


@pytest.mark.replay("camera")
def test_the_response_area_holds_our_cameras_and_the_ones_just_out(replay_sets):
    """The CARS-Hub's, the Thruway's and the county's camera lists as captured, against our fire district (the county
    GIS layer, tests/fixtures/wcgis): with the camera wall's default margin and the default dvr_extra_m, the cameras
    with video in Port Chester / Rye Brook and just outside them (I-95 to exit 21, the Hutch to exit 16, I-287 east of
    exit 9), not the approaches out to Mamaroneck or White Plains."""
    import json
    from pathlib import Path
    from tests.test_replay_camera import ReplayFetch, make
    fd = json.loads((Path(__file__).parent / "fixtures" / "wcgis" / "fire_districts.geojson").read_text())
    ours = [f["geometry"] for f in fd["features"] if f["properties"]["FIRENAME"] == "PORT CHESTER FD"]
    for s in replay_sets("camera"):
        rd = make(ReplayFetch(s), True)
        rd.area_shapes = lambda: {"basis": "fire_districts", "names": ["PORT CHESTER FD"], "note": "", "shapes": ours}
        d = Dvr(rd, Path("/nonexistent/dvr"), log=lambda m: None, free_floor=0, shared=True, area_wall=True,
                area_extra_m=1000)
        got = [rd.camera(c) for c in d.area_cameras()]
        assert 6 <= len(got) <= 10
        assert {"I-95", "Hutch"} <= {c["road"] for c in got}
        assert not any("Mamaroneck" in c["name"] or "Interchange 7" in c["name"] or "Interchange 8" in c["name"]
                       for c in got)


@pytest.mark.parametrize("order", ["listed", "reversed"])
def test_a_segment_both_leaders_wrote_is_kept_once(tmp_path, monkeypatch, order):
    """Whatever order the folder lists them in: the indexed file stays and only the other goes; not indexed (a new
    leader's first look), the newest stays. Never both."""
    real = os.scandir
    if order == "reversed":
        monkeypatch.setattr(dvr_mod.os, "scandir", lambda p: iter(sorted(real(p), key=lambda e: e.name, reverse=True)))
    else:
        monkeypatch.setattr(dvr_mod.os, "scandir", lambda p: iter(sorted(real(p), key=lambda e: e.name)))
    r, d, f, clock = setup(tmp_path, area=None)
    new = Dvr(r, tmp_path / "efs", clock=clock, log=lambda m: None, free_floor=0, area=around(r, CAM), shared=True)
    new.scan_now()
    step(new, f, clock, 2)
    key = max(new._cams[CAM]["segs"])
    mine = new._file(CAM, key)
    t0, dur, size = new._cams[CAM]["segs"][key]
    for dt in (-0.5, 0.5):                                                          # sorts before / after ours
        twin = mine.with_name(dvr_mod._name(key, t0 + dt, dur, size))               # the old leader's, timed apart
        twin.write_bytes(mine.read_bytes())
        new._scan()
        assert mine.exists() and not twin.exists() and new._cams[CAM]["segs"][key] == (t0, dur, size)
    # not indexed yet (a new leader's first look): the newest of the two stays, the other goes
    older = mine.with_name(dvr_mod._name(key, t0 - 0.5, dur, size))
    older.write_bytes(mine.read_bytes())
    os.utime(older, (time.time() - 60,) * 2)
    third = Dvr(r, tmp_path / "efs", clock=clock, log=lambda m: None, free_floor=0, shared=True)
    third.scan_now()
    assert mine.exists() and not older.exists() and third._file(CAM, key) == mine
    newer = mine.with_name(dvr_mod._name(key, t0 + 0.5, dur, size))
    newer.write_bytes(mine.read_bytes())
    os.utime(mine, (time.time() - 60,) * 2)
    fourth = Dvr(r, tmp_path / "efs", clock=clock, log=lambda m: None, free_floor=0, shared=True)
    fourth.scan_now()
    assert newer.exists() and not mine.exists() and fourth._file(CAM, key) == newer


def test_a_folder_that_cant_be_listed_keeps_its_index(tmp_path, monkeypatch):
    r, d, f, clock = setup(tmp_path, area=None)
    dv = Dvr(r, tmp_path / "efs", clock=clock, log=lambda m: None, free_floor=0, area=around(r, CAM), shared=True)
    dv.scan_now()
    step(dv, f, clock, 2)
    n = dv.status(CAM)["segments"]
    real = os.scandir

    def flaky(p):
        if str(p).endswith(CAM):
            raise OSError("ESTALE")
        return real(p)
    monkeypatch.setattr(dvr_mod.os, "scandir", flaky)
    dv._scan()
    assert dv.status(CAM)["segments"] == n


def test_a_camera_merge_moves_a_folder_only_on_the_leader_and_never_under_a_clip(tmp_path):
    """#324's follow(): a recording renamed to the id its camera is kept under now, asked through may_move: never by a
    task that may not write (a draining leader on EFS), never while a clip of either camera is being saved."""
    lease = {"ok": True}
    r, d, f, clock = setup(tmp_path, may_write=lambda: lease["ok"])
    d.pins = {CAM}
    step(d, f, clock, 2)
    r.aliases = lambda: {CAM: "NEWX"}
    lease["ok"] = False
    assert d.follow() == [] and (tmp_path / "dvr" / CAM).is_dir()
    lease["ok"] = True
    d.hold(CAM, True)
    assert d.follow() == [] and (tmp_path / "dvr" / CAM).is_dir()
    d.hold(CAM, False)
    assert d.follow() == [CAM] and (tmp_path / "dvr" / "NEWX").is_dir() and not (tmp_path / "dvr" / CAM).exists()
    key = min(d._cams["NEWX"]["segs"])
    assert d._file("NEWX", key).exists()                                            # its file names kept with it


# -- a new leader records at once (#596) -------------------------------------------------------------------------------

def leader(r, tmp_path, clock, **kw):
    kw.setdefault("area", around(r, CAM))
    logs = []
    d = Dvr(r, tmp_path / "efs", clock=clock, log=logs.append, free_floor=0, shared=True, **kw)
    d.logs = logs
    return d


def recorded(tmp_path, n=4):
    """The leader before: n rounds recorded on the shared folder, then a clean stop (its last checkpoint)."""
    r, d, f, clock = setup(tmp_path, area=None)
    old = leader(r, tmp_path, clock)
    old.scan_now()
    step(old, f, clock, n)
    old.stop()
    return r, f, clock, old


def index_of(d, cid=CAM):
    c = d._cams[cid]
    return dict(c["segs"]), {k: d._file(cid, k).name for k in c["segs"]}, set(c.get("breaks", ()))


def test_a_new_leader_records_before_the_index_is_read_and_the_index_ends_up_whole(tmp_path, monkeypatch):
    r, f, clock, old = recorded(tmp_path)
    last = old._cams[CAM]["last"]
    state = json.loads((tmp_path / "efs" / STATE_FILE).read_text())
    assert state["cameras"][CAM]["last"] == last and state["cameras"][CAM]["end"] == round(old._cams[CAM]["end"], 3)
    f.advance()
    clock.t += 4
    new = leader(r, tmp_path, clock)
    gate, reading = threading.Event(), threading.Event()
    real = Dvr._scan

    def slow(self, only=None, first=False):                   # the whole folder's listing: held until let go
        if first:
            reading.set()
            assert gate.wait(10)
        return real(self, only, first)
    monkeypatch.setattr(Dvr, "_scan", slow)
    new.start()
    try:
        until = time.monotonic() + 10
        while not new._cams.get(CAM, {}).get("segs") and time.monotonic() < until:
            time.sleep(0.02)
        assert reading.is_set() and new.scanned() and not new._indexed.is_set()   # recording, the index unread
        assert sorted(new._cams[CAM]["segs"]) == [last + 1]                                      # appended at once
        assert any("resumed 1 cameras from the state" in m for m in new.logs)
        assert not any("found on shared storage" in m for m in new.logs)
        gate.set()
        assert new._indexed.wait(10)
    finally:
        gate.set()
        new.stop()
    full = leader(r, tmp_path, clock)                            # the same folder read whole, as before
    full.scan_now()
    assert index_of(new) == index_of(full) and last + 1 in full._cams[CAM]["segs"]
    assert len(full._cams[CAM]["segs"]) == len(old._cams[CAM]["segs"]) + 1                  # nothing lost, none twice


def test_before_the_index_is_read_old_video_plays_and_nothing_is_evicted(tmp_path):
    r, f, clock, old = recorded(tmp_path)
    before = old.status(CAM)
    first_key = min(old._cams[CAM]["segs"])
    t0 = old._cams[CAM]["segs"][first_key][0]
    files = sorted(os.listdir(tmp_path / "efs" / CAM))
    one = max(s[2] for s in old._cams[CAM]["segs"].values())
    new = leader(r, tmp_path, clock, window_s=5, max_bytes=one * len(files))   # the old ones: past the window, at the cap
    assert new.resume()
    # a recording's older part: its own folder read when it is asked for (playback, a clip, Keep)
    st = new.status(CAM)
    assert st["from"] == before["from"] and st["segments"] == before["segments"]
    assert f"{first_key}.ts?s={int(t0 * 1000)}" in new.playlist(CAM).decode()
    assert [s["key"] for s in new.segments(CAM, t0, t0 + 1)] == [first_key]
    assert new.get(f"/api/cad/cameras/{CAM}/dvr/{first_key}.ts", f"s={int(t0 * 1000)}")[0] == 200
    # nothing evicted while the index isn't whole, and the cap weighed with what the state counted: no write over it
    other = leader(r, tmp_path, clock, window_s=5, max_bytes=one * len(files))
    assert other.resume()
    f.advance()
    clock.t += 4
    assert other.evict() == 0 and other.tick() == 0 and sorted(os.listdir(tmp_path / "efs" / CAM)) == files
    assert other.stopped == "Over the recording cap"
    other.max_bytes = 10 ** 9                                    # room again (never by evicting): written
    f.advance()
    clock.t += 4
    assert other.tick() == 2 and len(os.listdir(tmp_path / "efs" / CAM)) == len(files) + 2
    other.index_rest()                                           # whole: the window applies
    other.tick()
    assert all(s[0] >= clock.t - 5 for s in other._cams[CAM]["segs"].values())


def test_the_state_is_written_whole_and_only_by_the_leader(tmp_path, monkeypatch):
    lease = {"ok": False}
    r, d, f, clock = setup(tmp_path, area=None)
    d = leader(r, tmp_path, clock, may_write=lambda: lease["ok"])
    d.scan_now()
    step(d, f, clock, 2)
    d.stop()
    path = tmp_path / "efs" / STATE_FILE
    assert not path.exists()                                     # the standby: never
    lease["ok"] = True
    d._stop.clear()
    step(d, f, clock, 2)
    d.stop()                                                     # (a clean stop: its last one at once)
    d._stop.clear()
    assert json.loads(path.read_text())["cameras"][CAM]["last"] == d._cams[CAM]["last"]
    good = path.read_bytes()
    # a write that fails half way leaves the one before as it was, and no temp file
    real_replace = os.replace
    monkeypatch.setattr(dvrstate.os, "replace", lambda a, b: (_ for _ in ()).throw(OSError("EIO")))
    step(d, f, clock, 2)
    assert path.read_bytes() == good and not [p for p in path.parent.iterdir() if p.name.endswith(".tmp")]
    monkeypatch.setattr(dvrstate.os, "replace", real_replace)
    # the lease lost while it is being written: never put in place
    real_fsync = os.fsync

    def losing(fd):
        lease["ok"] = False
        return real_fsync(fd)
    monkeypatch.setattr(dvrstate.os, "fsync", losing)
    d.checkpoint(force=True)
    assert path.read_bytes() == good and not [p for p in path.parent.iterdir() if p.name.endswith(".tmp")]


@pytest.mark.parametrize("how", ["missing", "stale", "broken", "another release"])
def test_no_state_or_a_stale_one_reads_the_index_first_as_before(tmp_path, how):
    r, f, clock, old = recorded(tmp_path)
    path = tmp_path / "efs" / STATE_FILE
    if how == "missing":
        path.unlink()
    elif how == "stale":
        clock.t += dvrstate.MAX_AGE_S + 1
    elif how == "broken":
        path.write_text('{"v": 1, "at": ')
    else:
        path.write_text(json.dumps({"v": 99, "at": clock.t, "cameras": {}}))
    new = leader(r, tmp_path, clock)
    assert not new.resume() and not new.scanned() and new.tick() == 0
    new._stop.set()                                              # _run: the index first (scan_now), then it stops
    new._run()
    assert new.scanned() and new._indexed.is_set() and any("found on shared storage" in m for m in new.logs)
    assert new.status(CAM)["segments"] == old.status(CAM)["segments"]


def test_a_killed_writers_temp_files_go_once_they_are_stale(tmp_path):
    r, f, clock, old = recorded(tmp_path)
    folder = tmp_path / "efs" / CAM
    dead = folder / f".{1300}_1_4000.7.tmp"                     # the leader before's, killed mid-write (its pid: 7)
    dead.write_bytes(b"half")
    mine = folder / f".1301_1_4000.{dvrstate.TOKEN}.tmp"        # this process's own: never the scan's to delete
    mine.write_bytes(b"in flight")
    new = leader(r, tmp_path, clock)
    assert new.resume()
    new.index_rest()
    assert dead.exists()                                         # (seconds old: maybe still being written)
    for p in (dead, mine):
        os.utime(p, (time.time() - dvr_mod.STALE_TMP_S - 5,) * 2)
    new._started_at, new._rescanned_at = clock.t, clock.t - dvr_mod.RESCAN_S
    new.tick(wait_all=True)                                      # the rescans: off the recording's thread
    assert not dead.exists() and mine.exists()


def test_a_camera_merge_never_moves_a_folder_while_a_rescan_reads_it(tmp_path, monkeypatch):
    """The rescan lists the old id's folder, follow() would move it and its record, then the rescan's merge would make
    the old id again from its listing: twice the bytes, a playlist of files that aren't there. follow() waits."""
    r, d, f, clock = setup(tmp_path, area=None)
    d = leader(r, tmp_path, clock)
    d.scan_now()
    step(d, f, clock, 2)
    r.aliases = lambda: {CAM: "NEWX"}
    real, during = d._read_dir, []

    def listing_then_move(path):
        out = real(path)
        if path.name == CAM:
            during.append(d.follow())                       # the camera merge, between the listing and the merge
        return out
    monkeypatch.setattr(d, "_read_dir", listing_then_move)
    d._scan()
    assert during == [[]] and CAM in d._cams                 # not under the rescan
    monkeypatch.setattr(d, "_read_dir", real)
    assert d.follow() == [CAM]
    d._scan()
    assert CAM not in d._cams and d.total_bytes() == d._segs(d._cams["NEWX"]).bytes
    assert all(d._file("NEWX", k).exists() for k in d._cams["NEWX"]["segs"])


def test_bytes_of_a_camera_gone_from_the_index_never_hold_up_a_takeover(tmp_path):
    r, f, clock, old = recorded(tmp_path)
    path = tmp_path / "efs" / STATE_FILE
    state = json.loads(path.read_text())
    have = state["cameras"][CAM]["bytes"]
    state["cameras"]["GONE1"] = {"last": 5, "end": clock.t, "offset": 0, "bytes": 10 ** 6}   # moved away, no folder
    path.write_text(json.dumps(state))
    new = leader(r, tmp_path, clock, max_bytes=have + 200_000)
    assert new.resume()
    f.advance()
    clock.t += 4
    assert new.tick() == 1 and new.stopped == ""             # its bytes not counted: there's no folder of it
    # once the index is whole, the state counts nothing for a camera not in it; a moved camera's record goes
    new.index_rest()
    new.checkpoint(force=True)
    assert json.loads(path.read_text())["cameras"]["GONE1"]["bytes"] == 0
    r.aliases = lambda: {CAM: "NEWX"}
    assert new.follow() == [CAM]
    new.checkpoint(force=True)
    cams = json.loads(path.read_text())["cameras"]
    assert CAM not in cams and cams["NEWX"]["last"] == new._cams["NEWX"]["last"]


def test_another_segment_under_a_key_is_never_deleted_and_nothing_is_written_after_the_last_checkpoint(tmp_path):
    """The folder ran ahead of the state across an origin restart: the same key holds other, older video. Two files
    of a key a segment or more apart are both kept; the same segment twice (starts close) is kept once."""
    r, f, clock, old = recorded(tmp_path)
    key = max(old._cams[CAM]["segs"])
    t0, dur, size = old._cams[CAM]["segs"][key]
    mine = old._file(CAM, key)
    other = mine.with_name(dvr_mod._name(key, t0 - 60, dur, size))
    other.write_bytes(mine.read_bytes())
    twin = mine.with_name(dvr_mod._name(key, t0 + 0.4, dur, size))
    twin.write_bytes(mine.read_bytes())
    old._scan()
    assert mine.exists() and other.exists() and not twin.exists()
    assert any("two segments under key" in m for m in old.logs)
    # stopped: a round still under way writes nothing after the last checkpoint
    assert old._stop.is_set() and old._write(CAM, key + 1, clock.t, 4.0, b"\x47" * 188) is False
    assert not list((tmp_path / "efs" / CAM).glob(f"{key + 1}_*"))


def test_many_viewers_while_indexing_read_a_folder_once_and_one_that_cant_be_listed_is_asked_again(tmp_path, monkeypatch):
    r, f, clock, old = recorded(tmp_path)
    new = leader(r, tmp_path, clock)
    assert new.resume()
    real, calls = new._read_dir, []

    def failing(path):
        calls.append(path.name)
        return None
    monkeypatch.setattr(new, "_read_dir", failing)
    assert new.status(CAM)["segments"] == 0 and CAM not in new._indexed_cams      # not listable: not taken as read
    gate = threading.Event()

    def slow(path):
        calls.append(path.name)
        assert gate.wait(10)
        return real(path)
    monkeypatch.setattr(new, "_read_dir", slow)
    calls.clear()
    seen = []
    threads = [threading.Thread(target=lambda: seen.append(new.status(CAM)["segments"])) for _ in range(5)]
    for t in threads:
        t.start()
    time.sleep(0.2)
    gate.set()
    for t in threads:
        t.join(10)
    assert calls == [CAM] and seen == [old.status(CAM)["segments"]] * 5


def test_the_state_is_written_one_at_a_time_kept_fresh_and_a_leaked_temp_goes(tmp_path):
    r, f, clock, old = recorded(tmp_path)
    path = tmp_path / "efs" / STATE_FILE
    leaked = path.with_name(f".{STATE_FILE}.7-dead.tmp")
    leaked.write_text("{")
    os.utime(leaked, (time.time() - dvr_mod.STALE_TMP_S - 5,) * 2)
    new = leader(r, tmp_path, clock)
    assert new.resume()
    new.index_rest()
    assert not leaked.exists()
    errors = []

    def many():
        try:
            for _ in range(30):
                new.checkpoint(force=True)
        except Exception as e:  # noqa: BLE001
            errors.append(e)
    threads = [threading.Thread(target=many) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert errors == [] and json.loads(path.read_text())["v"] == 1
    assert not [p for p in path.parent.iterdir() if p.name.endswith(".tmp")]
    # a quiet DVR (nothing recorded, nothing changed): written again within a minute, so a takeover finds it fresh
    at = json.loads(path.read_text())["at"]
    new.wanted = lambda: {}
    clock.t += dvrstate.REFRESH_S + 1
    new.tick()
    assert json.loads(path.read_text())["at"] > at


# -- each camera's sequence offset outlives the task (#597) ------------------------------------------------------------

def pdts(d, cid=CAM):
    """The VOD playlist's segments: [(start, duration)], in its order."""
    out, t = [], None
    for line in d.playlist(cid).decode().splitlines():
        if line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            t = datetime.fromisoformat(line.split(":", 1)[1].replace("Z", "+00:00")).timestamp()
        elif line.startswith("#EXTINF:"):
            out.append((t, float(line[8:].split(",")[0])))
    return out


def started_over(tmp_path, offset=None):
    """The leader before: recording, the origin restarted (its counter from 10: an offset), recording on, a clean stop.
    offset: that offset set from the start instead."""
    r, d, f, clock = setup(tmp_path, area=None)
    old = leader(r, tmp_path, clock)
    old.scan_now()
    if offset is not None:
        old._cam(CAM)["offset"] = offset
    step(old, f, clock, 3)
    if offset is None:
        f.seq = 10
        step(old, f, clock, 6)                                   # (its timeline back on the clock: below)
        assert sum("started over" in m for m in old.logs) == 1
    old.stop()
    return r, f, clock, old


def take_over(r, tmp_path, clock, how):
    new = leader(r, tmp_path, clock)
    if how == "resume":
        assert new.resume()
        new.index_rest()
    else:                                                        # no state to resume from (stale): the folder first
        clock.t += dvrstate.MAX_AGE_S + 1
        assert not new.resume()
        new.scan_now()
    return new


@pytest.mark.parametrize("how", ["resume", "full scan"])
def test_a_short_handover_after_the_origin_started_over_records_nothing_twice(tmp_path, how):
    """The handover shorter than the chunklist's span: the new leader's first chunklist still lists what the leader
    before wrote. With its offset known: none of it again, no "started over", the playlist's dates only going on."""
    r, f, clock, old = started_over(tmp_path)
    keys_before = set(old._cams[CAM]["segs"])
    f.advance()                                                  # one new segment; the other two: written already
    new = take_over(r, tmp_path, clock, how)
    clock.t += 4
    new.tick()
    step(new, f, clock, 2)
    assert not any("started over" in m for m in new.logs)
    segs = sorted(new._cams[CAM]["segs"].items())
    added = [k for k, _ in segs if k not in keys_before]
    assert added == [max(keys_before) + 1, max(keys_before) + 2, max(keys_before) + 3]
    listed = pdts(new)
    assert len(listed) == len(segs) and all(b[0] > a[0] for a, b in zip(listed, listed[1:]))   # the dates only go on
    since = listed[len(keys_before) - 1:]                        # from the last one the leader before wrote: no overlap
    assert all(b[0] >= a[0] + a[1] - 1e-3 for a, b in zip(since, since[1:]))
    assert {p.name for p in (tmp_path / "efs" / CAM).iterdir()} == {new._file(CAM, k).name for k, _ in segs}


@pytest.mark.parametrize("how", ["resume", "full scan", "own disk"])
def test_an_offset_of_a_few_is_kept_so_nothing_is_skipped(tmp_path, how):
    if how == "own disk":
        r, d, f, clock = setup(tmp_path)
        d.pins = {CAM}
        d._cam(CAM)["offset"] = 2
        step(d, f, clock, 3)
        d.stop()
        last = d._cams[CAM]["last"]
        new = Dvr(r, tmp_path / "dvr", clock=clock, log=lambda m: None, free_floor=0)
        new.pins = {CAM}
    else:
        r, f, clock, old = started_over(tmp_path, offset=2)
        last = old._cams[CAM]["last"]
        new = take_over(r, tmp_path, clock, how)
    assert new._cams[CAM]["offset"] == 2
    step(new, f, clock, 1)
    assert max(new._cams[CAM]["segs"]) == last + 1                  # the new one, not taken for one already kept


@pytest.mark.parametrize("dt", [4.4, 3.6])
def test_the_timeline_stays_within_a_round_of_the_clock(tmp_path, dt):
    """The stream's segments last longer (or shorter) than its chunklist says: each one carried on from the one before
    would drift off the clock without bound; it is kept between the earliest and the latest each can have begun."""
    r, d, f, clock = setup(tmp_path)
    d.pins = {CAM}
    d.tick()
    for _ in range(40):
        step(d, f, clock, 1, dt=dt)
        to = d.status(CAM)["to"]
        assert clock.t - dt - 1e-3 <= to <= clock.t + dvr_mod.SNAP_BACK_S + dvr_mod.SNAP_AHEAD_S + 1e-3
    listed = pdts(d)
    assert all(b[0] > a[0] for a, b in zip(listed, listed[1:]))


def test_with_no_state_at_all_what_the_leader_before_wrote_is_still_not_recorded_again(tmp_path):
    """No state (a release before it, a lost file): the offset isn't known, the counter looks started over. Only the
    entries that begin after the recording's end are new: nothing written twice, the dates only going on."""
    r, f, clock, old = started_over(tmp_path)
    (tmp_path / "efs" / STATE_FILE).unlink(missing_ok=True)
    keys_before, end = set(old._cams[CAM]["segs"]), old._cams[CAM]["end"]
    new = leader(r, tmp_path, clock)
    assert not new.resume()
    new.scan_now()
    step(new, f, clock, 2)                                       # a short handover: two written already still listed
    added = sorted(set(new._cams[CAM]["segs"]) - keys_before)
    assert added == [max(keys_before) + 1, max(keys_before) + 2]
    assert all(new._cams[CAM]["segs"][k][0] >= end - dvr_mod.SNAP_BACK_S for k in added)
    listed = pdts(new)
    assert all(b[0] > a[0] for a, b in zip(listed, listed[1:]))


def test_a_segment_whose_span_is_recorded_already_is_never_written_again(tmp_path):
    """Keys that jump past the last one (an offset that no longer fits) but whose time is recorded already: not
    written; the first that ends after the recording's end is."""
    r, d, f, clock = setup(tmp_path)
    d.pins = {CAM}
    d.tick()
    step(d, f, clock, 2)
    last, end = d._cams[CAM]["last"], d._cams[CAM]["end"]
    f.seq += 4                                                  # the next keys skip one, no time has passed
    assert d.tick() == 0 and d._cams[CAM]["last"] == last
    step(d, f, clock, 1)
    added = sorted(set(d._cams[CAM]["segs"]) - set(range(last + 1)))
    assert added == [last + 5] and d._cams[CAM]["segs"][last + 5][0] >= end - 1e-3
    listed = pdts(d)
    assert all(b[0] >= a[0] + a[1] - 1e-3 for a, b in zip(listed, listed[1:]))


def test_a_live_viewers_chunklist_is_timed_when_it_was_read(tmp_path):
    """The live pump's copy can be up to 10 s old: its segments' times come from when it was read, not from now."""
    r, d, f, clock = setup(tmp_path)
    assert r.get(f"/api/cad/cameras/{CAM}/stream/playlist.m3u8", "")[0] == 200
    r.pump_once(CAM)                                            # read now ...
    read_at = r._streams[CAM]["list_at"]
    clock.t += 8                                                # ... used 8 s later (the pump stalled)
    assert d.tick() == 3
    assert d.status(CAM)["to"] == pytest.approx(read_at, abs=1e-3)


def test_a_handover_on_the_same_stream_is_no_discontinuity(tmp_path):
    r, f, clock, old = recorded(tmp_path)
    last = old._cams[CAM]["last"]
    f.advance()
    clock.t += 4
    new = leader(r, tmp_path, clock)
    assert new.resume()
    new.tick()
    assert sorted(k for k in new._cams[CAM]["segs"]) == [last + 1] and not new._new_stream(CAM, last + 1)


def test_a_parked_cameras_folder_is_read_only_by_its_trial_round_and_its_file_is_left_alone(tmp_path, monkeypatch):
    """(#550 with #596) a parked camera: no round, so its folder isn't read for one; its check's trial round is a real
    round (its folder read first). The parked list and its temp file beside the recordings: never ours to clean."""
    r, f, clock, old = recorded(tmp_path)
    parked = tmp_path / "efs" / dvr_mod.PARKED_FILE
    parked.write_text("{}")
    their_tmp = tmp_path / "efs" / ".parked.7.tmp"
    their_tmp.write_text("{")
    for p in (parked, their_tmp):
        os.utime(p, (time.time() - 3600,) * 2)
    new = leader(r, tmp_path, clock)
    assert new.resume()
    (tmp_path / "efs" / STATE_FILE).unlink()                 # (not in the state: its folder read before a round)
    new._resumed.clear()
    read = []
    real = new._ensure
    monkeypatch.setattr(new, "_ensure", lambda cid: (read.append(cid), real(cid)))
    new.parked[CAM] = {"why": "no stream", "since": clock.t, "at": clock.t, "checked": clock.t, "next": clock.t}
    assert new.record_once(CAM) == 0 and read == []
    f.advance()
    clock.t += 4
    assert new.record_once(CAM, trial=True) == 1 and read == [CAM]
    new.parked.clear()
    new.index_rest()
    new._started_at, new._rescanned_at = clock.t, clock.t - dvr_mod.RESCAN_S
    new.tick(wait_all=True)
    assert parked.exists() and their_tmp.exists()
