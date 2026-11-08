"""The camera list stays put (#276): the 511NY CARS-Hub cameras (#249) appeared, went, or never showed.

What was found (2026-11-07, the live feeds read once each, the cameras' stills once each): the CCTV inventory read
fine every time (53 cameras in our area, the same ids and video addresses on every read) and the merge with the
Thruway's and the county's kept the 24 that are new; but 511NY's stills (public.carsprogram.org) are re-taken every
few minutes only (1 to 14 min old), and a still older than 300 s marked the camera down: dim, or hidden ("Hide down
cameras"), out of the nearest-camera lists and folded away in the panels, and its live video refused, until a later
sweep caught a newer still. Now: a camera with video is judged by its stream; a still's age is judged per source; a
camera a read leaves out stays listed, stale; a list that can't be read says so on its cameras; and a new hub process
never answers "same" to an old process's rev.
"""

from __future__ import annotations

import json
import threading
from email.utils import formatdate
from urllib.error import HTTPError

from sizeup import roads as r
from sizeup.roads import Roads
from tests.test_ny511 import HUB, NOW, STILLS, Clock, Fetch, county_fetch, fx, make


class Stills(Fetch):
    """Fetch with the stills' age set (Last-Modified, seconds before now) and the HLS origin refusing or answering."""

    def __init__(self, clock):
        super().__init__()
        self.clock, self.age, self.hls = clock, 60.0, True
        self.gate: threading.Event | None = None       # set: the HLS origin answers only once it is set

    def __call__(self, url, timeout, limit, headers=None):
        if url.startswith((STILLS, "https://www.westchestergov.com/cctv/", "https://nyssnapshot.com/", "https://511ny.org/map/Cctv/")):
            self.calls.append((url, dict(headers or {})))
            if self.age is None:
                raise OSError("timed out")
            return 200, {"content-type": "image/png", "last-modified": formatdate(self.clock() - self.age, usegmt=True)}, b"\x89PNG"
        if "skyvdn.com" in url:
            self.calls.append((url, {}))
            if self.gate is not None:
                self.gate.wait(5)
            if not self.hls:
                raise OSError("connection refused")
            if "chunklist_" in url:
                return 200, {"content-type": "application/vnd.apple.mpegurl"}, b"#EXTM3U\n#EXTINF:4.0,\nmedia_w1_1.ts\n"
            return 200, {"content-type": "application/vnd.apple.mpegurl"}, b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nchunklist_w1.m3u8\n"
        return super().__call__(url, timeout, limit, headers)


def hub(clock=None, **kw):
    clock = clock or Clock()
    f = county_fetch(Stills(clock))
    rd = make(f, clock=clock, hub=HUB, thruway=True, county=True, **kw)
    for p in ("thruway", "county"):
        assert rd.refresh("cameras", p)
    assert rd.refresh("cameras", rd.carshub)
    return rd, f, clock


def cam(rd, cid):
    return next((c for c in rd.public()["cameras"] if c["id"] == cid), None)


def test_a_511ny_still_minutes_old_is_how_its_stills_are_not_a_camera_down():
    rd, f, clock = hub()
    f.age = 850                                         # 14 min: the oldest seen on a working camera
    assert rd.image("35992")[0] == 200 and not rd.health("35992")["down"]
    assert cam(rd, "35992")["state"] == "ok" and "35992" in [c["camera"] for c in rd.search_places("exit 16 purchase camera")]
    assert rd.stream("35992", "playlist.m3u8")[0] == 200                     # its live video is never refused for it
    # the 300 s that took it down before still holds for the county's 8 s pictures
    assert r.STALE_IMAGE_S == 300 and Roads.stale_s({"source": "County"}) == 300 and Roads.stale_s({"source": "511NY"}) == 1800


def test_a_live_video_camera_with_no_current_still_is_judged_by_its_stream():
    rd, f, clock = hub()
    f.age = 2 * 86400                                   # a still two days old (seen on one camera)
    f.gate = threading.Event()                          # the origin slow to answer the probe
    n = len(f.calls)
    for _ in range(3):
        clock.t += 60
        code, body, _, _ = rd.image("35992")
        assert code == 404 and json.loads(body)["error"] == "no current image"
    # its stream not looked at yet: listed, stale, and not offered by the pickers until it is
    c = cam(rd, "35992")
    assert not c["down"] and (c["state"], c["why"]) == ("stale", "Still old · video not checked")
    lat, lon = c["lat"], c["lon"]
    assert "35992" not in [x["id"] for x in rd.nearest(lat, lon)]
    # (#308) the still going down set off a probe at once (two small GETs, in the background), not at the next sweep
    f.gate.set()
    assert rd.probes_done()
    assert any("35992" in u or "R8_039" in u for u, _ in f.calls[n:] if "skyvdn" in u)
    c = cam(rd, "35992")
    assert not c["down"] and c["state"] == "ok" and "35992" in [x["id"] for x in rd.nearest(lat, lon)]
    # the next sweep finds no stream either: offline, with the reason, and the rev moved so the pages hear it
    f.hls = False
    rev = rd.rev
    clock.t += r.HEALTH_SWEEP_S + 1
    rd.sweep()
    assert rd.probes_done()
    c = cam(rd, "35992")
    assert c["down"] and c["state"] == "offline" and c["why"] == "No video or image" and rd.rev != rev
    assert "35992" not in [x["id"] for x in rd.nearest(lat, lon)]
    assert rd.stream("35992", "playlist.m3u8")[0] == 503                     # a viewer: snapshots at once
    # a fresh still with the stream still refused: listed, stale, "No live video"
    f.age = 30
    clock.t += 60
    assert rd.image("35992")[0] == 200
    c = cam(rd, "35992")
    assert not c["down"] and (c["state"], c["why"]) == ("stale", "No live video")
    clock.t += r.STREAM_RETRY_S + 1
    assert cam(rd, "35992")["state"] == "ok"


def test_a_state_that_changes_with_time_moves_the_rev():
    rd, f, clock = hub()
    f.hls = False
    assert rd.stream("35992", "playlist.m3u8")[0] == 503                     # refused: "No live video" for 5 min
    rd.state_tick()
    rev = rd.rev
    assert rd.state_tick() is False and rd.rev == rev                       # nothing changed: no churn
    clock.t += r.STREAM_RETRY_S + 1                                          # the refusal runs out with no request
    assert rd.state_tick() is True and rd.rev != rev


def test_a_snapshot_camera_goes_down_with_its_reason():
    rd, f, clock = hub()
    f.age = None                                        # every still times out
    for _ in range(r.DOWN_FAILS):
        clock.t += 60
        rd.image("wc23")
    c = cam(rd, "wc23")
    assert c["down"] and (c["state"], c["why"]) == ("offline", "Not answering") and c["since"] == clock.t


def test_a_camera_left_out_of_a_read_stays_listed_stale_and_goes_only_after_a_while(tmp_path):
    clock = Clock()
    f = county_fetch(Stills(clock))
    rd = make(f, clock=clock, hub=HUB, thruway=True, county=True, tmp_path=tmp_path)
    for p in ("thruway", "county", "carshub"):
        rd.refresh("cameras", p)
    n = len(rd.public()["cameras"])
    # the next read leaves 35992 out (a partial or odd read; nothing the hub can tell from a good one)
    xml = fx("cars_cctv_recorded.xml")
    start = xml.index(b"<inventory-item>", xml.index(b"<device-id>35992") - 400)
    end = xml.index(b"</inventory-item>", start) + len(b"</inventory-item>")
    f.bodies[HUB] = xml[:start] + xml[end:]
    clock.t += 3600
    assert rd.refresh("cameras", rd.carshub)
    c = cam(rd, "35992")
    assert len(rd.public()["cameras"]) == n and (c["state"], c["why"], c["since"]) == ("stale", "Not in 511NY's list", clock.t)
    assert rd.carshub.status["cameras"]["n"] == 5                                   # the source's own count
    assert any("1 not in the latest list, kept (stale)" in m for m in rd.logs)
    # kept across a restart (the disk cache), and still stale
    again = make(f, clock=clock, hub=HUB, thruway=True, county=True, tmp_path=tmp_path)
    assert cam(again, "35992")["state"] == "stale"
    # back in the list: fresh again
    clock.t += 3600
    f.bodies[HUB] = xml
    rd.refresh("cameras", rd.carshub)
    assert cam(rd, "35992")["state"] == "ok"
    # left out for longer than CAMERA_KEEP_S: gone
    f.bodies[HUB] = xml[:start] + xml[end:]
    for _ in range(int(r.CAMERA_KEEP_S // 3600) + 2):
        clock.t += 3600
        rd.refresh("cameras", rd.carshub)
    assert cam(rd, "35992") is None


def test_an_empty_read_never_empties_the_map():
    rd, f, clock = hub()
    before = {c["id"] for c in rd.public()["cameras"]}
    f.bodies[HUB] = b"<cctvInventories></cctvInventories>"
    clock.t += 3600
    assert rd.refresh("cameras", rd.carshub)
    assert {c["id"] for c in rd.public()["cameras"]} == before
    assert {c["state"] for c in rd.public()["cameras"] if c["source"] == "511NY"} == {"stale"}


def test_a_list_that_cant_be_read_keeps_its_cameras_and_says_so_when_it_has_been_a_while():
    rd, f, clock = hub()
    before = {c["id"] for c in rd.public()["cameras"]}
    good = f.bodies.pop(HUB)

    def failing(url, timeout, limit, headers=None):
        if url == HUB:
            raise HTTPError(url, 500, "Server Error", None, None)
        return Stills.__call__(f, url, timeout, limit, headers)
    rd.carshub.fetch = failing
    clock.t += 3600
    assert not rd.refresh("cameras", rd.carshub)
    assert {c["id"] for c in rd.public()["cameras"]} == before and cam(rd, "35992")["state"] == "ok"   # an hour: fine
    clock.t += 2 * 3600
    rd._changed()
    c = cam(rd, "35992")
    assert (c["state"], c["why"]) == ("stale", "511NY list not updated") and c["since"] == NOW
    assert cam(rd, "wc23")["state"] == "ok"                                          # the county's list is fine
    f.bodies[HUB] = good


def test_the_carshub_count_is_made_again_when_ours_arrive_after_it():
    clock = Clock()
    f = county_fetch(Stills(clock))
    rd = make(f, clock=clock, hub=HUB, thruway=True, county=True)
    rd.refresh("cameras", rd.carshub)                   # first, as on a hub's first start (511NY's are read first)
    assert rd.camera_compare["known"] == 0
    rd.refresh("cameras", "thruway")
    rd.refresh("cameras", "county")
    assert (rd.camera_compare["known"], rd.camera_compare["new"]) == (3, 3)


def test_a_new_hub_process_never_says_same_to_an_old_ones_rev():
    old = make(clock=Clock(NOW))
    for _ in range(5):
        old._changed()
    new = make(clock=Clock(NOW + 120))                  # deployed two minutes later
    assert new.rev > old.rev
    assert json.loads(new.get("/api/cad/roads", f"rev={old.rev}")[1]).get("same") is None
