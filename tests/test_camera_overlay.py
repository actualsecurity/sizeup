"""The same camera by the overlay on its still (#320 follow-up; sizeup/camoverlay.py, Roads._same_overlay).

Found 2026-11-08: the CARS-Hub lists one I-684 camera twice, "at" an exit and "north of" it, 0.4 mile and about 700 m
apart, with two stream names and nothing else in common; both stills carry one overlay bar (the camera's DOT name). The
stills here are made up in that shape: a black bar across the top, a made-up name in made-up 5x8 glyphs (the real bars'
character size) on the left and the date and time on the right, over a grey picture. No real ids, names or addresses.
"""

from __future__ import annotations

import json
import struct
import zlib
from pathlib import Path

import numpy as np

from sizeup import camoverlay
from sizeup import roads as r
from sizeup.roads import BasicAuth, Roads
from tests.test_camera_dedup import LAT, M, cam

W, H = 480, 270
NAME = "CAM-I999nb-at_Ex9_OAK RD-mp4.3"


def glyph(ch: str) -> np.ndarray:
    """A made-up 5x8 glyph for ch (a space: none), the size of the real bars' characters: 15 to 25 lit pixels."""
    if ch == " ":
        return np.zeros((8, 5), bool)
    rng = np.random.default_rng(ord(ch))
    while True:
        g = rng.random((8, 5)) < 0.5
        if 15 <= g.sum() <= 25:
            return g


def picture(name: str = NAME, time: str = "11/08/26 03:18:22", seed: int = 0, bar: bool = True, w: int = W,
            h: int = H, name_at: int = 4, time_at: int | None = None) -> np.ndarray:
    """An RGB still: a grey scene (noise from seed), and with bar the overlay: a 16-row black bar, the name in white
    5x8 glyphs (a 6 px pitch) from name_at, the date and time at the right (time_at, else flush right)."""
    rng = np.random.default_rng(seed)
    img = rng.integers(60, 200, (h, w, 3), dtype=np.uint8)
    if bar:
        img[:16] = 0
        time_at = w - 6 * len(time) - 4 if time_at is None else time_at
        for x0, text in ((name_at, name), (time_at, time)):
            for k, ch in enumerate(text):
                img[4:12, x0 + 6 * k:x0 + 6 * k + 5][glyph(ch)] = 255
    return img


def png(img: np.ndarray, filters=(0,)) -> bytes:
    """img as an RGB PNG, each row with the next of filters (0 none, 1 sub, 2 up, 3 average, 4 Paeth)."""
    h, w, ch = img.shape
    rows, prev = [], np.zeros(w * ch, np.int32)
    for y in range(h):
        cur = img[y].reshape(-1).astype(np.int32)
        f = filters[y % len(filters)]
        left = np.concatenate([np.zeros(ch, np.int32), cur[:-ch]])
        upleft = np.concatenate([np.zeros(ch, np.int32), prev[:-ch]])
        if f == 0:
            out = cur
        elif f == 1:
            out = cur - left
        elif f == 2:
            out = cur - prev
        elif f == 3:
            out = cur - (left + prev) // 2
        else:
            p = left + prev - upleft
            pa, pb, pc = abs(p - left), abs(p - prev), abs(p - upleft)
            out = cur - np.where((pa <= pb) & (pa <= pc), left, np.where(pb <= pc, prev, upleft))
        rows.append(bytes([f]) + (out & 255).astype(np.uint8).tobytes())
        prev = cur

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b""))


def test_one_name_at_two_times_and_names_one_character_apart():
    a = camoverlay.overlay(png(picture(time="11/08/26 03:18:22", seed=1)))
    b = camoverlay.overlay(png(picture(time="11/09/26 14:02:57", seed=2)))     # another day and time, another scene
    assert a and b and camoverlay.same(a, b)
    # one character apart: the direction, a digit of the milepost, the exit; each is another camera
    for other in ("CAM-I999sb-at_Ex9_OAK RD-mp4.3", "CAM-I999nb-at_Ex9_OAK RD-mp4.8", "CAM-I999nb-at_Ex8_OAK RD-mp4.3"):
        assert not camoverlay.same(a, camoverlay.overlay(png(picture(other, seed=1)))), other
    # the bar is read the same whatever the PNG's row filters
    for filters in ((1,), (2,), (3,), (4,), (0, 1, 2, 3, 4)):
        assert camoverlay.overlay(png(picture(seed=1), filters)) == a
    # no bar, a JPEG, a broken PNG, another image size, a still too big to read cheaply: nothing to go by
    assert camoverlay.overlay(png(picture(bar=False))) is None
    assert camoverlay.overlay(png(np.zeros((H, W, 3), np.uint8))) is None              # a black placeholder
    assert camoverlay.overlay(b"\xff\xd8\xff\xe0 a jpeg") is None
    assert camoverlay.overlay(png(picture())[:60]) is None
    small = camoverlay.overlay(png(picture(seed=1, w=352, h=240)))
    assert small and not camoverlay.same(a, small) and not camoverlay.same(a, None)
    assert camoverlay.overlay(png(picture(w=2400, h=300))) is None                     # over MAX_SIDE
    assert camoverlay.overlay(png(picture(w=1900, h=1070))) is None                    # a bar over MAX_BYTES
    # a name and its time with no wide gap between: a new pattern at each still, never the same
    run_on = lambda t: camoverlay.overlay(png(picture(time=t, seed=1, time_at=4 + 6 * len(NAME) + 6)))
    assert not camoverlay.same(run_on("11/08/26 03:18:22"), run_on("11/08/26 03:18:29"))


def test_a_name_longer_than_most_of_the_bar_is_read_whole():
    # 65 characters (390 px of 480): the end of the name tells two cameras apart; the time after a wide gap doesn't
    long = "CAM-I999nb-North of Exit 9 at Oak Rd Overpass Westbound Ramp-mp14."
    a = camoverlay.overlay(png(picture(long + "3", time="03:18:22", seed=1, time_at=430)))
    b = camoverlay.overlay(png(picture(long + "3", time="09:41:07", seed=2, time_at=430)))
    c = camoverlay.overlay(png(picture(long + "8", time="03:18:22", seed=1, time_at=430)))
    assert 6 * len(long) > 0.6 * W
    assert a and camoverlay.same(a, b) and not camoverlay.same(a, c)


class Stills:
    """The stills by address (PNG, served now)."""

    def __init__(self):
        self.bodies: dict[str, bytes] = {}

    def __call__(self, url, timeout, limit, headers=None):
        if url in self.bodies:
            return 200, {"content-type": "image/png"}, self.bodies[url]
        return 404, {}, b""


class Clock:
    def __init__(self, t: float = 1_800_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def hub(cams, tmp_path=None, clock=None):
    f = Stills()
    rd = Roads("", {}, fetch=f, log=lambda m: None, clock=clock or Clock(), gazetteer=Path("/nonexistent"),
               thruway=False, county=False, carshub=True, carshub_url="https://hub.example/hub/data/cctv.xml",
               cache=(tmp_path / "roads.json") if tmp_path else None, auth=BasicAuth("made-up-user", "made-up-pass"))
    rd.carshub.data["cameras"] = [dict(c, source="511NY", provider="carshub") for c in cams]
    rd.carshub.status["cameras"]["at"] = 1_800_000_000.0
    rd._changed()
    return rd, f


def ids(rd):
    return [c["id"] for c in rd.merged("cameras")]


AT = cam("90200", "At I-684 at Exit 2 Oak Rd", road="I-684", mile="4.5", stream="R0_200")
NORTH = cam("90201", "At I-684 North of Exit 2 Oak Rd County Park", road="I-684", mile="4.9", lat=LAT + 700 * M,
            stream="R0_201")


def test_one_camera_listed_at_an_exit_and_north_of_it_is_one_once_both_stills_are_seen(tmp_path):
    rd, f = hub([AT, NORTH], tmp_path)
    assert ids(rd) == ["90200", "90201"]                             # nothing in the lists ties them
    f.bodies[AT["url"]] = png(picture(time="11/08/26 03:18:22", seed=1))
    f.bodies[NORTH["url"]] = png(picture(time="11/08/26 03:18:25", seed=2))
    assert rd.image("90200")[0] == 200 and rd.image("90201")[0] == 200
    assert ids(rd) == ["90200"] and rd.alias("90201") == "90200"
    assert rd.camera("90200")["also"] == [{"source": "511NY", "name": NORTH["name"]}]
    # a restart (the disk cache) knows it before any still is fetched again
    rd._save()
    again, _ = hub([AT, NORTH], tmp_path)
    again._load()
    assert ids(again) == ["90200"]


def test_cameras_with_other_overlays_or_facing_or_roads_stay_two():
    def like(c, cid, **kw):
        return dict(c, id=cid, url=f"https://stills.example/cameras/R0_{cid}.flv.png",
                    video=f"https://s1.video.example/rtplive/R0_{cid}/playlist.m3u8", **kw)
    other = like(NORTH, "90202")
    sb = like(NORTH, "90203", dir="Southbound")
    nb = like(AT, "90204", dir="Northbound")
    road = like(NORTH, "90205", road="I-287", name="I-287 at Exit 2")
    far = like(NORTH, "90206", lat=LAT + 3500 * M)
    rd, f = hub([AT, other, sb, nb, road, far])
    f.bodies[AT["url"]] = png(picture(seed=1))
    f.bodies[other["url"]] = png(picture("CAM-I999nb-at_Ex9_OAK RD-mp4.8", seed=2))           # another camera's name
    for c in (sb, nb, road, far):
        f.bodies[c["url"]] = png(picture("CAM-I999-at_Ex7_ELM ST-mp2.1", seed=3))
    for c in (AT, other, sb, nb, road, far):
        rd.image(c["id"])
    # sb and nb carry one overlay but face different ways; the I-287 one is on another road; far is 2.8 km beyond the others
    assert sorted(ids(rd)) == ["90200", "90202", "90203", "90204", "90205", "90206"] and rd.aliases() == {}


def test_a_still_without_its_bar_keeps_what_was_seen():
    rd, f = hub([AT, NORTH])
    f.bodies[AT["url"]] = png(picture(seed=1))
    f.bodies[NORTH["url"]] = png(picture(seed=2))
    rd.image("90200")
    rd.image("90201")
    assert ids(rd) == ["90200"]
    f.bodies[AT["url"]] = png(np.zeros((H, W, 3), np.uint8))           # a placeholder now and then
    rd._images.clear()
    rd.image("90200")
    seen = rd._overlay_rev
    assert ids(rd) == ["90200"] and rd._overlay_rev == seen and rd._overlay["90200"]


def test_a_merge_by_overlay_is_checked_again_and_comes_apart_when_the_overlays_differ(tmp_path):
    clock = Clock()
    rd, f = hub([AT, NORTH], tmp_path, clock)
    f.bodies[AT["url"]] = png(picture(seed=1))
    f.bodies[NORTH["url"]] = png(picture(seed=2))
    rd.sweep()
    assert ids(rd) == ["90200"] and rd.alias("90201") == "90200"
    # the merged-away record isn't listed, but the sweep still looks at its still: now it names another camera
    f.bodies[NORTH["url"]] = png(picture("CAM-I999nb-N_of_Ex9_OAK RD-mp4.9", seed=2))
    clock.t += r.HEALTH_SWEEP_S + 1
    rd.sweep()
    assert ids(rd) == ["90200", "90201"] and rd.aliases() == {}


def test_overlays_not_read_for_a_day_are_forgotten_and_gone_ids_are_not_kept(tmp_path):
    clock = Clock()
    rd, f = hub([AT, NORTH], tmp_path, clock)
    f.bodies[AT["url"]] = png(picture(seed=1))
    f.bodies[NORTH["url"]] = png(picture(seed=2))
    rd.sweep()
    assert ids(rd) == ["90200"]
    f.bodies.clear()                                                # no still at all for a day: nothing read again
    for _ in range(int(r.OVERLAY_KEEP_S // r.HEALTH_SWEEP_S) + 2):
        clock.t += r.HEALTH_SWEEP_S + 1
        rd.sweep()
    assert rd._overlay == {} and ids(rd) == ["90200", "90201"]
    # an id its list no longer has: its overlay goes at the next sweep, and is never written to (or read from) the cache
    f.bodies[AT["url"]] = png(picture(seed=1))
    f.bodies[NORTH["url"]] = png(picture(seed=2))
    clock.t += r.HEALTH_SWEEP_S + 1
    rd.sweep()
    assert set(rd._overlay) == {"90200", "90201"}
    rd.carshub.data["cameras"] = [c for c in rd.carshub.data["cameras"] if c["id"] != "90201"]
    rd._changed()
    rd._save()
    assert "90201" not in json.loads((tmp_path / "roads.json").read_text())["overlays"]
    clock.t += r.HEALTH_SWEEP_S + 1
    rd.sweep()
    assert set(rd._overlay) == {"90200"}


def test_a_new_overlay_is_saved_soon_without_waiting_for_the_next_list(tmp_path):
    clock = Clock()
    rd, f = hub([AT, NORTH], tmp_path, clock)
    cache = tmp_path / "roads.json"
    f.bodies[AT["url"]] = png(picture(seed=1))
    f.bodies[NORTH["url"]] = png(picture(seed=2))
    rd.image("90200")                                               # the first: written at once
    assert set(json.loads(cache.read_text())["overlays"]) == {"90200"}
    rd.image("90201")                                               # the next within OVERLAY_SAVE_S: waits
    assert set(json.loads(cache.read_text())["overlays"]) == {"90200"} and rd._overlay_dirty
    clock.t += r.OVERLAY_SAVE_S
    rd._images.clear()
    rd.image("90200")                                               # (the same overlay: nothing new, but it's due)
    assert set(json.loads(cache.read_text())["overlays"]) == {"90200", "90201"} and not rd._overlay_dirty
