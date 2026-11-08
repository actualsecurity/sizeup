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
    """The stills by address (PNG, served now as ctype; each fetch takes `takes` seconds of the clock)."""

    def __init__(self):
        self.bodies: dict[str, bytes] = {}
        self.ctype = "image/png"
        self.calls: list[str] = []
        self.clock = None
        self.takes = 0.0

    def __call__(self, url, timeout, limit, headers=None):
        self.calls.append(url)
        if self.clock is not None:
            self.clock.t += self.takes
        if url in self.bodies:
            return 200, {"content-type": self.ctype}, self.bodies[url]
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
    f.clock = clock
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
    # the merged-away record isn't listed, but the sweep still looks at its still: now it names another camera; after
    # OVERLAY_SPLIT_READS sweeps in a row of that, the merge comes apart
    f.bodies[NORTH["url"]] = png(picture("CAM-I999nb-N_of_Ex9_OAK RD-mp4.9", seed=2))
    for k in range(r.OVERLAY_SPLIT_READS):
        assert ids(rd) == ["90200"], k
        clock.t += r.HEALTH_SWEEP_S + 1
        rd.sweep()
    assert ids(rd) == ["90200", "90201"] and rd.aliases() == {}


def jittered(img: np.ndarray, n: int = 19, seed: int = 7) -> np.ndarray:
    """img with n pixels at the edges of its overlay's glyphs flipped (the real bars' read-to-read jitter)."""
    out = img.copy()
    lit = out[:16, :, 0] > 128
    edge = np.argwhere(lit ^ np.roll(lit, 1, axis=1))
    for y, x in np.random.default_rng(seed).choice(edge, n, replace=False):
        out[y, x] = 255 - out[y, x]
    return out


def test_one_jittered_read_keeps_the_merge_and_three_in_a_row_split_it(tmp_path):
    clock = Clock()
    rd, f = hub([AT, NORTH], tmp_path, clock)
    f.bodies[AT["url"]] = png(picture(seed=1))
    f.bodies[NORTH["url"]] = png(picture(seed=2))
    rd.sweep()
    assert ids(rd) == ["90200"]
    rev, kept = rd.rev, rd._overlay["90201"]
    # one read a few edge pixels off: noise; the pattern kept, nothing regrouped or saved
    f.bodies[NORTH["url"]] = png(jittered(picture(seed=2)))
    assert not camoverlay.same(camoverlay.overlay(f.bodies[NORTH["url"]]), kept)
    clock.t += r.HEALTH_SWEEP_S + 1
    rd.sweep()
    assert ids(rd) == ["90200"] and rd._overlay["90201"] == kept and not rd._overlay_dirty
    assert all(x["id"] != "90201" for x in rd.public()["cameras"])
    # the clean read again: the count starts over
    f.bodies[NORTH["url"]] = png(picture(seed=2))
    clock.t += r.HEALTH_SWEEP_S + 1
    rd.sweep()
    assert ids(rd) == ["90200"] and "90201" not in rd._overlay_miss
    # three odd reads in a row: the merge comes apart
    for k in range(3):
        f.bodies[NORTH["url"]] = png(jittered(picture(seed=2), seed=k))
        clock.t += r.HEALTH_SWEEP_S + 1
        rd.sweep()
    assert ids(rd) == ["90200", "90201"] and rd.rev != rev


def test_a_pattern_no_other_camera_carries_changes_nothing():
    rd, f = hub([AT, NORTH])
    f.bodies[AT["url"]] = png(picture(seed=1))
    f.bodies[NORTH["url"]] = png(picture("CAM-I999nb-N_of_Ex9_OAK RD-mp4.9", seed=2))
    rev = rd.rev
    for t in ("03:18:22", "03:18:29", "03:18:36"):                 # (a bar read with its time in it: new each time)
        f.bodies[AT["url"]] = png(picture(time="11/08/26 " + t, seed=1, time_at=4 + 6 * len(NAME) + 6))
        rd._images.clear()
        rd.image("90200")
        rd.image("90201")
    assert rd.rev == rev and rd._overlay_rev == 0 and not rd._overlay_dirty and ids(rd) == ["90200", "90201"]


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
    rd.image("90200")                                               # alone: nothing to merge, nothing written
    assert not cache.exists()
    rd.image("90201")                                               # the same overlay: a merge, written at once
    assert set(json.loads(cache.read_text())["overlays"]) == {"90200", "90201"}
    # another pair within OVERLAY_SAVE_S: it waits ...
    c = cam("90220", "I-287 at Exit 5 Birch Ln", road="I-287", mile="5.0", stream="R0_220", lat=LAT + 9000 * M)
    d = cam("90221", "I-287 North of Exit 5 Birch Ln", road="I-287", mile="5.4", stream="R0_221", lat=LAT + 9600 * M)
    rd.carshub.data["cameras"] += [dict(x, source="511NY", provider="carshub") for x in (c, d)]
    rd._changed()
    f.bodies[c["url"]] = png(picture("CAM-I287eb-Ex5_BIRCH LN-mp5.0", seed=5))
    f.bodies[d["url"]] = png(picture("CAM-I287eb-Ex5_BIRCH LN-mp5.0", seed=6))
    rd.image("90220")
    rd.image("90221")
    assert set(json.loads(cache.read_text())["overlays"]) == {"90200", "90201"} and rd._overlay_dirty
    clock.t += r.OVERLAY_SAVE_S                                     # ... until it's due (the next still read)
    rd._images.clear()
    rd.image("90200")
    assert set(json.loads(cache.read_text())["overlays"]) == {"90200", "90201", "90220", "90221"} and not rd._overlay_dirty


def test_a_save_builds_and_writes_its_document_under_one_lock(tmp_path):
    rd, f = hub([AT, NORTH], tmp_path)
    events = []

    class Lock:
        def __enter__(self):
            events.append("lock")

        def __exit__(self, *a):
            events.append("unlock")
    real_clock = rd.clock
    rd.clock = lambda: events.append("build") or real_clock()      # (the document's time: read while building it)
    rd._save_lock = Lock()
    rd._save()
    assert events[0] == "lock" and events[-1] == "unlock" and "build" in events[1:-1]
    assert (tmp_path / "roads.json").exists()


def test_a_png_labelled_image_jpeg_is_read_by_its_bytes():
    # the still server labels some PNG stills image/jpeg: the overlay goes by the PNG signature, not the label
    rd, f = hub([AT, NORTH])
    f.ctype = "image/jpeg"
    f.bodies[AT["url"]] = png(picture(seed=1))
    f.bodies[NORTH["url"]] = png(picture(seed=2))
    rd.image("90200")
    rd.image("90201")
    assert ids(rd) == ["90200"] and rd.alias("90201") == "90200"


def test_wide_spaces_in_a_name_are_inside_the_name():
    # 352 px stills (a 14-row bar): four spaces make a gap wider than 1.5 bar heights inside the name; the cut is at
    # the widest gap, before the time at the right edge, so two cameras sharing the part before the spaces stay two
    a = camoverlay.overlay(png(picture("CAM-I99nb-Ex9    OAK RD-mp4.3", "11/08 03:18", seed=1, w=352, h=240)))
    b = camoverlay.overlay(png(picture("CAM-I99nb-Ex9    ELM ST-mp2.1", "11/08 03:18", seed=2, w=352, h=240)))
    c = camoverlay.overlay(png(picture("CAM-I99nb-Ex9    OAK RD-mp4.3", "11/09 21:40", seed=3, w=352, h=240)))
    assert a and b and not camoverlay.same(a, b) and camoverlay.same(a, c)
    # no time at the right edge: the whole text is the name, wide gaps and all
    d = camoverlay.overlay(png(picture("CAM-I99nb-Ex9    OAK RD", "", seed=1, w=352, h=240)))
    e = camoverlay.overlay(png(picture("CAM-I99nb-Ex9    ELM ST", "", seed=1, w=352, h=240)))
    assert d and e and not camoverlay.same(d, e)


def test_the_sweep_rechecks_only_the_records_an_overlay_ties():
    clock = Clock()
    by_stream = cam("90210", "At Exit 7 on Elm Street", road="I-684", mile="2.0", stream="R0_210", lat=LAT + 5000 * M)
    twin = dict(by_stream, id="90211", name="I-684 Exit 7 Elm St second record", url="https://stills.example/R0_211.png")
    rd, f = hub([AT, NORTH, by_stream, twin], clock=clock)
    f.takes = 5.0                                                   # each still takes a few seconds
    for c in (AT, NORTH, by_stream, twin):
        f.bodies[c["url"]] = png(picture(seed=1) if c in (AT, NORTH) else picture("CAM-I999-Ex7_ELM ST-mp2.0", seed=4))
    rd.sweep()
    assert set(rd.aliases()) == {"90201", "90211"} and rd._overlay_tied == {"90200", "90201"}
    # the next sweeps, HEALTH_SWEEP_S apart from the start of the last: the overlay-tied record's still each time,
    # never the one merged by its stream
    for _ in range(2):
        f.calls.clear()
        clock.t = rd._swept + r.HEALTH_SWEEP_S                      # (the last sweep's fetches took 20 s of that)
        rd.sweep()
        assert NORTH["url"] in f.calls and twin["url"] not in f.calls
