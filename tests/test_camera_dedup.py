"""One camera listed once (#320, sizeup/roads.py camera_match, Roads._merge_cameras).

Found 2026-11-07 in the real lists (the replay set, tests/test_replay_camera.py): the CARS-Hub lists some cameras twice,
an older record and a newer one with two ids and two stream names, the same name in other words, at the same route and
milepost (so the same point); and it puts every camera at its milepost, so two other cameras there share a point too
(two views a few metres apart, both southbound; a camera at an exit and one north of it). These cameras are made up in
those shapes: no real ids or addresses.
"""

from __future__ import annotations

from pathlib import Path

from sizeup import roads as r
from sizeup.dvr import Dvr
from sizeup.roads import Roads
from tests.test_dvr import CAM, setup, step

LAT, LON = 41.0, -73.7
M = 1 / 111_320                                           # a metre of latitude, in degrees


def cam(cid, name, lat=LAT, lon=LON, road="Hutch", dir="", mile="12.9", stream="", still="", cadence=60, off=False):
    video = f"https://s1.video.example/rtplive/{stream}/playlist.m3u8" if stream else ""
    url = still or (f"https://stills.example/cameras/{stream}.flv.png" if stream else f"https://stills.example/{cid}.jpg")
    return {"id": cid, "name": name, "road": road, "dir": dir, "mile": mile, "lat": lat, "lon": lon, "url": url,
            "video": video, "has_video": bool(video), "off": off, "cadence": cadence}


def roads(carshub=(), thruway=(), county=(), clock=None):
    rd = Roads("", {}, fetch=lambda *a, **k: (404, {}, b""), log=lambda m: None, clock=clock or (lambda: 1_800_000_000.0),
               gazetteer=Path("/nonexistent"), thruway=True, thruway_urls={}, county=True, county_urls={},
               carshub=True, carshub_url="")
    for p, cams in ((rd.carshub, carshub), (rd.thruway, thruway), (rd.county, county)):
        p.data["cameras"] = [dict(c, source=p.short, provider=p.name) for c in cams]
    rd._changed()
    return rd


def ids(rd):
    return [c["id"] for c in rd.merged("cameras")]


# ---- what makes two records one camera ----------------------------------------------------------------------------


def test_one_source_listing_a_camera_twice_by_the_same_name_in_other_words():
    old = cam("90001", "Exit 9 Elm Ave MM 9.41", stream="R0_901")
    new = cam("90002", "At Exit 9 on Elm Avenue", stream="R0_CAMX9001")
    assert r.camera_match(old, new, True) == "name"
    rd = roads(carshub=[new, old])
    assert ids(rd) == ["90002"] and rd.alias("90001") == "90002" and rd.alias("90002") == "90002"
    assert rd.camera("90002")["also"] == [{"source": "511NY", "name": "Exit 9 Elm Ave MM 9.41"}]
    assert rd.public()["cameras"][0]["also"] == [{"source": "511NY", "name": "Exit 9 Elm Ave MM 9.41"}]
    # a connector's name for the same view, and a route written two ways
    a = cam("90003", "At I-684 North of Oak Road", road="I-684", mile="0.7", stream="R0_903")
    b = cam("90004", "I-684/HRP Connector North of Oak Rd", road="I-684", mile="0.7", stream="R0_904")
    assert r.camera_match(a, b, True) == "name"


def test_the_same_stream_is_one_camera_on_any_host_and_any_id():
    thruway = cam("1xx00270e", "I-287 Just East of Interchange 5", road="I-287", dir="Eastbound", mile="2.7", stream="XX_906")
    thruway["video"] = "https://s9.video.example:443/rtplive/XX_906/playlist.m3u8"
    cars = cam("90010", "I-287 MP 002.70 EB Just East of Interchange 5", road="I-287", dir="Eastbound", mile="22.3",
               stream="XX_906", lat=LAT + 2000 * M)                        # placed 2 km off: the stream says it all
    assert r.stream_keys(thruway) == r.stream_keys(cars) == {"XX_906"}
    assert r.camera_match(thruway, cars, False) == "stream"
    # a still named for the stream counts; a numbered still ("12.jpg", "camera23.jpg") doesn't
    assert r.stream_keys(cam("x", "x", still="https://stills.example/thumbs/XX_906.flv.png")) == {"XX_906"}
    assert r.stream_keys(cam("x", "x", still="https://stills.example/other/12.jpg")) == set()
    assert r.stream_keys(cam("x", "x", still="https://county.example/cctv/camera23.jpg")) == set()
    rd = roads(carshub=[cars], thruway=[thruway])
    assert ids(rd) == ["1xx00270e"] and rd.alias("90010") == "1xx00270e"


def test_a_source_id_inside_the_others_id_or_address():
    thruway = cam("1xx01370s", "I-95 at Interchange 21", road="I-95", dir="Southbound", mile="13.8")
    cars = cam("NYSDOT-1xx01370s", "I-95 MP 013.80 SB at Interchange 21", road="I-95", dir="Southbound", mile="22.2",
               lat=LAT + 900 * M)
    assert r.camera_match(cars, thruway, False) == "id"
    numbered = cam("90020", "Elsewhere", still="https://stills.example/90021.jpg")
    assert r.camera_match(numbered, cam("90021", "Elsewhere too", lat=LAT + 0.1), False) == ""   # digits alone: no


def test_the_same_name_within_300_m_on_the_same_road_and_milepost():
    a = cam("90030", "Exit 9 Elm Ave", stream="R0_930")
    assert r.camera_match(a, cam("90031", "At Exit 9 on Elm Avenue", lat=LAT + 250 * M, stream="R0_931"), True) == "name"
    assert r.camera_match(a, cam("90032", "At Exit 9 on Elm Avenue", lat=LAT + 400 * M, stream="R0_932"), True) == ""
    assert r.camera_match(a, cam("90033", "At Exit 9 on Elm Avenue", mile="13.9", stream="R0_933"), True) == ""
    assert r.camera_match(a, cam("90034", "At Exit 9 on Elm Avenue", road="I-287", stream="R0_934"), True) == ""
    # two sources' mileposts are two systems: the place says it
    assert r.camera_match(a, cam("1xx00099n", "Exit 9 Elm Ave", mile="1.2", lat=LAT + 250 * M), False) == "name"


def test_cameras_side_by_side_stay_two():
    # the northbound and southbound views at one milepost, named alike
    nb = cam("90040", "NB Exit 9 Elm Ave", dir="Northbound", stream="R0_940")
    sb = cam("90041", "SB Exit 9 Elm Ave", dir="Southbound", stream="R0_941")
    assert r.camera_match(nb, sb, True) == "" and r.camera_match(nb, sb, False) == ""
    # one source's two cameras at one milepost (so one point): two views north of one lane, a camera at an exit and
    # one north of it; a place alone never makes one source's records one camera
    v24 = cam("90042", "At I-684 North of Birch Lane", road="I-684", mile="2.8", stream="R0_942")
    v24a = cam("90043", "I684 SB North of Birchwood Lane MM 2.9", road="I-684", mile="2.8", stream="R0_943")
    at = cam("90044", "At I-684 Exit 2 NY120", road="I-684", mile="4.9", stream="R0_944")
    north = cam("90045", "At I-684 North of Exit 2 NY120 County Airport", road="I-684", mile="4.9", stream="R0_945")
    for a, b in ((v24, v24a), (at, north)):
        assert r.camera_match(a, b, True) == ""
    # an intersection's approaches, one source, one point
    king = cam("wc901", "SB King Approach - King St/Willett Ave", road="King St", dir="Southbound", mile="")
    willett = cam("wc902", "WB Willett Approach - King St/Willett Ave", road="Willett Ave", dir="Westbound", mile="")
    rd = roads(carshub=[nb, sb, v24, v24a, at, north], county=[king, willett])
    assert sorted(ids(rd)) == sorted(["90040", "90041", "90042", "90043", "90044", "90045", "wc901", "wc902"])
    assert rd.aliases() == {}


# ---- which one is kept ------------------------------------------------------------------------------------------------


def test_live_video_then_the_fresher_still_then_ours_first():
    county = cam("wc903", "Elm Ave & Main St", road="Elm Ave", mile="", cadence=8)
    cars = cam("90050", "Elm Ave & Main St", road="Elm Ave", mile="", lat=LAT + 5 * M)
    rd = roads(carshub=[cars], county=[county])
    assert ids(rd) == ["wc903"]                                     # no video either: the county's 8 s stills
    cars_video = dict(cars, video="https://s1.video.example/rtplive/R0_950/playlist.m3u8", has_video=True)
    rd = roads(carshub=[cars_video], county=[county])
    assert ids(rd) == ["90050"] and rd.alias("wc903") == "90050"    # live video wins
    assert rd.camera("90050")["also"] == [{"source": "County", "name": "Elm Ave & Main St"}]
    # out of service: the other one, video or not
    rd = roads(carshub=[dict(cars_video, off=True)], county=[county])
    assert ids(rd) == ["wc903"]
    # both stills fetched: the one CAMERA_FRESHER_S newer
    a, b = cam("90051", "Exit 9 Elm Ave", stream="R0_951"), cam("90052", "At Exit 9 on Elm Avenue", stream="R0_952")
    rd = roads(carshub=[a, b])
    assert ids(rd) == ["90051"]                                     # a tie: the first listed
    rd._health["90051"] = {"last_image": 1000.0, "down_since": None, "fails": 0}
    rd._health["90052"] = {"last_image": 1000.0 + r.CAMERA_FRESHER_S + 60, "down_since": None, "fails": 0}
    rd._cam_kept.clear()
    rd._changed()
    assert ids(rd) == ["90052"]


def test_the_one_kept_stays_kept_until_another_beats_it():
    a, b = cam("90060", "Exit 9 Elm Ave", stream="R0_960"), cam("90061", "At Exit 9 on Elm Avenue", stream="R0_961")
    rd = roads(carshub=[a, b])
    assert ids(rd) == ["90060"]
    rd.carshub.data["cameras"].reverse()                            # the next read lists them the other way round
    rd._changed()
    assert ids(rd) == ["90060"] and rd.alias("90061") == "90060"
    # its stream gone from the list: the other, with video, is kept now, and the old id names it
    rd.carshub.data["cameras"][1].update(video="", has_video=False)
    rd._changed()
    assert ids(rd) == ["90061"] and rd.alias("90060") == "90061" and rd.alias("90061") == "90061"
    # the camera's state stays its own (offline, stale): the one kept doesn't change with it
    rd._health["90061"] = {"last_image": None, "down_since": 1.0, "fails": 9}
    rd._changed()
    assert ids(rd) == ["90061"]


def test_the_cameras_paths_take_a_merged_away_id(tmp_path):
    rd, d, f, clock = setup(tmp_path)
    twin = dict(rd.camera(CAM), id="NYSDOT-90070", name="I-95 SB at Interchange 21 (second record)")
    rd.thruway.data["cameras"].append(twin)
    rd._changed()
    assert "NYSDOT-90070" not in ids(rd) and rd.alias("NYSDOT-90070") == CAM
    code, body, ctype, _ = rd.get("/api/cad/cameras/NYSDOT-90070/stream/playlist.m3u8", "")
    assert code == 200 and rd.streams_open() == 1 and CAM in rd._streams


def test_a_keep_and_a_recording_follow_the_camera_to_the_id_kept_for_it(tmp_path):
    rd, d, f, clock = setup(tmp_path)
    saved = {}
    d.save_pins = lambda pins: saved.update(pins=pins)
    d.pin(CAM, True)
    step(d, f, clock, 2)
    before = d.status(CAM)
    assert before["segments"] >= 3
    # a second record of the same camera (the same stream) appears, and the first is marked out of service
    twin = dict(rd.camera(CAM), id="NYSDOT-90080", name="I-95 SB at Interchange 21 (second record)")
    rd.thruway.data["cameras"].append(twin)
    next(c for c in rd.thruway.data["cameras"] if c["id"] == CAM).update(off=True)
    rd._changed()
    assert rd.alias(CAM) == "NYSDOT-90080"
    step(d, f, clock)
    assert d.pins == {"NYSDOT-90080": before["pinned_until"] - 24 * 3600} and saved["pins"] == d.pins
    after = d.status("NYSDOT-90080")
    assert after["pinned"] and after["recording"] and after["from"] == before["from"]
    assert after["segments"] > before["segments"] and not (tmp_path / "dvr" / CAM).exists()
    assert any(f"{CAM}'s recording moved to NYSDOT-90080" in m for m in d.logs)
    # the old id's paths find the camera kept for it
    code, body, _, _ = rd.get(f"/api/cad/cameras/{CAM}/dvr/index.m3u8", "")
    assert code == 200 and body.count(b".ts") == after["segments"]
    # after a restart too (the folder is the new id's)
    again = Dvr(rd, tmp_path / "dvr", clock=clock, log=lambda m: None, free_floor=0)
    assert again.status("NYSDOT-90080")["segments"] == after["segments"]


def test_two_of_one_source_are_never_one_camera_through_a_third():
    # the Thruway's camera is near both 511NY records; those two don't match each other (other names, one point)
    thruway = cam("1xx00500e", "I-287 at Interchange 5", road="I-287", dir="Eastbound", mile="5.0", stream="XX_950")
    a = cam("90090", "I-287 MP 005.00 EB at Interchange 5", road="I-287", dir="Eastbound", mile="25.0", stream="XX_950")
    b = cam("90091", "I287 EB North of Birch Lane MM 5.1", road="I-287", dir="Eastbound", mile="25.0", lat=LAT + 3 * M,
            stream="R0_991")
    assert r.camera_match(b, thruway, False) == "near" and r.camera_match(a, b, True) == ""
    rd = roads(carshub=[a, b], thruway=[thruway])
    assert ids(rd) == ["1xx00500e", "90091"] and rd.alias("90090") == "1xx00500e"


def test_a_direction_on_one_side_only_is_not_the_same_name():
    a = cam("90095", "Exit 9 Elm Ave", stream="R0_995")
    assert r.camera_match(a, cam("90096", "SB Exit 9 Elm Ave", stream="R0_996"), True) == ""
    assert r.camera_match(a, cam("90097", "At Exit 9 on Elm Avenue", dir="Southbound", stream="R0_997"), True) == ""


def test_the_groups_are_made_again_only_when_the_lists_change():
    a, b = cam("90100", "Exit 9 Elm Ave", stream="R0_100"), cam("90101", "At Exit 9 on Elm Avenue", stream="R0_101")
    rd = roads(carshub=[a, b])
    assert ids(rd) == ["90100"]
    calls = []
    real = r.camera_match
    try:
        r.camera_match = lambda *x, **k: calls.append(1) or real(*x, **k)
        rd._changed()                                               # a rev for something else (a still, a DVR mark)
        assert ids(rd) == ["90100"] and not calls
        rd.carshub.data["cameras"][1] = dict(rd.carshub.data["cameras"][1], name="Exit 10 Oak St")
        rd._changed()
        assert ids(rd) == ["90100", "90101"] and calls
    finally:
        r.camera_match = real


def test_the_kept_record_retired_by_its_source_names_the_one_left():
    # the newer record kept; later its source drops it (stale for a while, then gone): the older one is listed, and
    # the retired id names it all along
    old, new = cam("90110", "Exit 9 Elm Ave MM 9.41", stream="R0_110"), cam("90111", "At Exit 9 on Elm Avenue", stream="R0_111")
    rd = roads(carshub=[new, old])
    assert ids(rd) == ["90111"]
    rd.carshub.data["cameras"][0]["missing_since"] = 1.0           # left out of a read: still there, stale
    rd._changed()
    assert ids(rd) == ["90110"] and rd.alias("90111") == "90110"
    rd.carshub.data["cameras"].pop(0)                               # gone (CAMERA_KEEP_S later)
    rd._changed()
    assert ids(rd) == ["90110"] and rd.alias("90111") == "90110" and not rd.camera("90110").get("also")
    # straight from listed to gone (no stale spell) too
    rd = roads(carshub=[new, old])
    assert ids(rd) == ["90111"]
    rd.carshub.data["cameras"].pop(0)
    rd._changed()
    assert ids(rd) == ["90110"] and rd.alias("90111") == "90110"
    # and once the camera is gone altogether, nothing names it
    rd.carshub.data["cameras"].clear()
    rd._changed()
    assert ids(rd) == [] and rd.aliases() == {}


def test_a_keep_and_a_recording_follow_when_the_kept_record_is_retired(tmp_path):
    rd, d, f, clock = setup(tmp_path)
    twin = dict(rd.camera(CAM), id="NYSDOT-90120", name="I-95 SB at Interchange 21 (second record)")
    rd.thruway.data["cameras"].append(twin)
    rd._changed()
    assert rd.alias("NYSDOT-90120") == CAM
    d.pin(CAM, True)
    step(d, f, clock, 2)
    before = d.status(CAM)
    rd.thruway.data["cameras"] = [c for c in rd.thruway.data["cameras"] if c["id"] != CAM]    # the kept one retired
    rd._changed()
    assert rd.camera(CAM) is None and rd.alias(CAM) == "NYSDOT-90120"
    step(d, f, clock)
    assert set(d.pins) == {"NYSDOT-90120"}
    after = d.status("NYSDOT-90120")
    assert after["recording"] and after["from"] == before["from"] and after["segments"] > before["segments"]
    code, _, _ = rd.get(f"/api/cad/cameras/{CAM}/dvr/index.m3u8", "")[:3]
    assert code == 200


def test_a_recording_waits_to_move_while_its_segments_are_read(tmp_path):
    rd, d, f, clock = setup(tmp_path)
    d.pins = {CAM}
    step(d, f, clock, 2)
    twin = dict(rd.camera(CAM), id="NYSDOT-90130", name="I-95 SB at Interchange 21 (second record)")
    rd.thruway.data["cameras"].append(twin)
    next(c for c in rd.thruway.data["cameras"] if c["id"] == CAM).update(off=True)
    rd._changed()
    d.may_move = lambda old, new: False                             # a clip is being cut from it
    assert d.follow() == [] and (tmp_path / "dvr" / CAM).is_dir()
    d.may_move = lambda old, new: True
    assert d.follow() == [CAM] and (tmp_path / "dvr" / "NYSDOT-90130").is_dir()
