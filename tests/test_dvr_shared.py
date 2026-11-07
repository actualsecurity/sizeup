"""The camera DVR on shared storage (#322, sizeup/dvr.py): our area's cameras 24/7 with a 24 h window on EFS, written only
by the lease-holding leader; a new leader reads the index the old one left and appends to it, while the old one may still
be draining. Without shared storage: as before.

No network: the Thruway's camera fixtures with a moving Wowza-like chunklist (tests/test_hlsseg.py Origin). The area's
real camera count: a replay test over the CARS-Hub's captured camera lists (tests/replay.py).
"""

from __future__ import annotations

import os
import re
import time

import pytest

from sizeup import dvr as dvr_mod
from sizeup.config import ConfigError
from sizeup.dvr import Dvr, area_polygon, in_area, shared_mount
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
