"""The same camera by the overlay on its still (#320 follow-up; sizeup/camoverlay.py, Roads._same_overlay).

Found 2026-11-08: the CARS-Hub lists one I-684 camera twice, "at" an exit and "north of" it, 0.4 mile and about 700 m
apart, with two stream names and nothing else in common; both stills carry one overlay bar (the camera's DOT name). The
stills here are made up in that shape: a black bar across the top with white marks for the name on the left and the
time on the right, over a grey picture. No real ids, names or addresses.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import numpy as np

from sizeup import camoverlay
from sizeup.roads import BasicAuth, Roads
from tests.test_camera_dedup import LAT, M, cam

W, H = 480, 270


def picture(name: int, time: int = 0, seed: int = 0, bar: bool = True) -> np.ndarray:
    """An RGB still: a grey scene (noise from seed), and with bar the overlay: a name made of white marks from the
    number name on the left, the time's marks on the right."""
    rng = np.random.default_rng(seed)
    img = rng.integers(60, 200, (H, W, 3), dtype=np.uint8)
    if bar:
        img[:18] = 0
        bits = [(name >> k) & 1 for k in range(24)]
        for k, b in enumerate(bits):
            if b:
                img[4:14, 6 + 11 * k:13 + 11 * k] = 255
        for k in range(8):
            if (time >> k) & 1:
                img[4:14, 330 + 15 * k:340 + 15 * k] = 255
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


def test_one_overlay_at_two_times_and_two_overlays():
    a = camoverlay.overlay(png(picture(0x5A3C91, time=3, seed=1)))
    b = camoverlay.overlay(png(picture(0x5A3C91, time=200, seed=2)))         # seconds later: another picture, time
    c = camoverlay.overlay(png(picture(0x5A3C93, time=3, seed=1)))           # one mark more: another camera
    assert a and b and c and camoverlay.same(a, b) and not camoverlay.same(a, c)
    # the bar is read the same whatever the PNG's row filters
    for filters in ((1,), (2,), (3,), (4,), (0, 1, 2, 3, 4)):
        assert camoverlay.overlay(png(picture(0x5A3C91, time=3, seed=1), filters)) == a
    # no bar, a JPEG, a broken PNG, another image size: nothing to go by
    assert camoverlay.overlay(png(picture(0x5A3C91, bar=False))) is None
    assert camoverlay.overlay(png(np.zeros((H, W, 3), np.uint8))) is None              # a black placeholder
    assert camoverlay.overlay(b"\xff\xd8\xff\xe0 a jpeg") is None
    assert camoverlay.overlay(png(picture(1))[:60]) is None
    small = camoverlay.overlay(png(picture(0x5A3C91, seed=1)[:240, :352]))
    assert not camoverlay.same(a, small) and not camoverlay.same(a, None)


class Stills:
    """The stills by address (PNG, served now)."""

    def __init__(self):
        self.bodies: dict[str, bytes] = {}

    def __call__(self, url, timeout, limit, headers=None):
        if url in self.bodies:
            return 200, {"content-type": "image/png"}, self.bodies[url]
        return 404, {}, b""


def hub(cams, tmp_path=None):
    f = Stills()
    rd = Roads("", {}, fetch=f, log=lambda m: None, clock=lambda: 1_800_000_000.0, gazetteer=Path("/nonexistent"),
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
    f.bodies[AT["url"]] = png(picture(0x7123AB, time=1, seed=1))
    f.bodies[NORTH["url"]] = png(picture(0x7123AB, time=4, seed=2))
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
    f.bodies[AT["url"]] = png(picture(0x7123AB, seed=1))
    f.bodies[other["url"]] = png(picture(0x7123AF, seed=2))           # another camera's name
    for c in (sb, nb, road, far):
        f.bodies[c["url"]] = png(picture(0x6000F1, seed=3))
    for c in (AT, other, sb, nb, road, far):
        rd.image(c["id"])
    # sb and nb carry one overlay but face different ways; the I-287 one is on another road; far is 2.8 km beyond the others
    assert sorted(ids(rd)) == ["90200", "90202", "90203", "90204", "90205", "90206"] and rd.aliases() == {}


def test_a_still_without_its_bar_keeps_what_was_seen():
    rd, f = hub([AT, NORTH])
    f.bodies[AT["url"]] = png(picture(0x7123AB, seed=1))
    f.bodies[NORTH["url"]] = png(picture(0x7123AB, seed=2))
    rd.image("90200")
    rd.image("90201")
    assert ids(rd) == ["90200"]
    f.bodies[AT["url"]] = png(np.zeros((H, W, 3), np.uint8))           # a placeholder now and then
    rd._images.clear()
    rd.image("90200")
    seen = rd._overlay_rev
    assert ids(rd) == ["90200"] and rd._overlay_rev == seen and rd._overlay["90200"]
