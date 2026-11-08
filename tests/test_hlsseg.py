"""Live camera video, whole and clean (#276): sizeup/hlsseg.py's checks and repair, and the hub's HLS proxy using them
(sizeup/roads.py Roads.stream / pump_once / _for_player).

The 511NY cameras' origin was watched for 16 min on 2026-11-07 (the Hutch at Exit 17: 352x240 Constrained Baseline,
15 fps, an IDR every 62 frames, one per segment): 13 of 52 segments arrived whole (Content-Length matched) but with
reference frames lost between the camera and the origin, often the mid-segment IDR; ffmpeg decoded them with "error
while decoding MB 13 12" and the picture smeared sideways until the next IDR. Those recordings are not in the repo
(the cameras' video is 511NY's to share); the segments here are built: a real SPS and real slice headers (frame_num,
first_mb_in_slice) in real MPEG-TS packets, with the losses the origin showed made on purpose.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from sizeup import hlsseg
from sizeup import roads as roads_mod
from sizeup.roads import http_get
from tests.test_roads import Clock, PumpFetch, make

VPID = 0x101


# ---- a small H.264 / MPEG-TS writer -------------------------------------------------------------------------------


class Bits:
    def __init__(self):
        self.bits: list[int] = []

    def u(self, n: int, v: int) -> Bits:
        self.bits += [(v >> (n - 1 - i)) & 1 for i in range(n)]
        return self

    def ue(self, v: int) -> Bits:
        v += 1
        n = v.bit_length()
        return self.u(n - 1, 0).u(n, v)

    def se(self, v: int) -> Bits:
        return self.ue(2 * v - 1 if v > 0 else -2 * v)

    def rbsp(self) -> bytes:
        b = self.bits + [1]
        b += [0] * (-len(b) % 8)
        raw = bytes(int("".join(map(str, b[i:i + 8])), 2) for i in range(0, len(b), 8))
        out, zeros = bytearray(), 0                   # emulation prevention
        for x in raw:
            if zeros >= 2 and x <= 3:
                out.append(3)
                zeros = 0
            out.append(x)
            zeros = zeros + 1 if x == 0 else 0
        return bytes(out)


def sps(w_mbs=22, h_mbs=15) -> bytes:
    body = (Bits().u(8, 66).u(8, 0xC0).u(8, 30).ue(0)        # Constrained Baseline, level 3.0, sps 0
            .ue(0)                                            # log2_max_frame_num 4: frame_num is 4 bits
            .ue(2).ue(1).u(1, 0)                              # POC type 2, one ref frame, no gaps allowed
            .ue(w_mbs - 1).ue(h_mbs - 1).u(1, 1).u(1, 1).u(1, 0).u(1, 0))
    return b"\x67" + body.rbsp()


def sps_high(lists=(0, 3, 6)) -> bytes:
    """A High-profile SPS with a scaling matrix (seq_scaling_list_present for lists): frame_num 4 bits, 480x272."""
    b = (Bits().u(8, 100).u(8, 0).u(8, 30).ue(0)               # High, level 3.0, sps 0
         .ue(1).ue(0).ue(0).u(1, 0).u(1, 1))                  # 4:2:0, 8-bit, no qpprime bypass, scaling matrix
    for i in range(8):
        b.u(1, i in lists)
        if i == 0 and i in lists:
            b.se(-8)                                           # nextScale 0 at once: the default list, no more deltas
        elif i in lists:
            size = 16 if i < 6 else 64
            for j in range(size):                              # a ramp up from 8, then down by -9 at the end
                b.se(1 if j < size - 1 else -9)
    b.ue(0).ue(2).ue(1).u(1, 0).ue(29).ue(16).u(1, 1).u(1, 1).u(1, 0).u(1, 0)
    return b"\x67" + b.rbsp()


def pps() -> bytes:
    return b"\x68" + Bits().ue(0).ue(0).u(1, 0).u(1, 0).ue(0).ue(0).ue(0).u(1, 0).u(2, 0).rbsp()


def slice_nal(idr: bool, frame_num: int, first_mb: int = 0, ref: bool = True) -> bytes:
    head = (Bits().ue(first_mb).ue(7 if idr else 5).ue(0).u(4, frame_num % 16))
    if idr:
        head.ue(0)                                            # idr_pic_id
    nal = (0x65 if idr else 0x41 if ref else 0x01)
    return bytes([nal]) + head.rbsp() + b"\x5a" * (600 if idr else 60)    # stand-in macroblock data


def frame(idr: bool, frame_num: int, first_mb: int = 0, ref: bool = True) -> bytes:
    nals = [b"\x09\xf0"]                                      # access unit delimiter
    if idr:
        nals += [sps(), pps()]
    nals.append(slice_nal(idr, frame_num, first_mb, ref))
    return b"".join(b"\x00\x00\x00\x01" + n for n in nals)


def ts_packets(pid: int, payload: bytes, cc: dict, bounded=True, pts=0) -> bytes:
    """One PES (PTS) of payload on pid, split into TS packets padded by adaptation-field stuffing."""
    hdr = b"\x00\x00\x01\xe0"
    p90 = int(pts * 90000)
    pts_b = bytes([0x21 | ((p90 >> 29) & 0x0E), (p90 >> 22) & 0xFF, 0x01 | ((p90 >> 14) & 0xFE), (p90 >> 7) & 0xFF,
                   0x01 | ((p90 << 1) & 0xFE)])
    ext = b"\x80\x80\x05" + pts_b
    length = len(ext) + len(payload) if bounded else 0
    pes = hdr + length.to_bytes(2, "big") + ext + payload
    out = b""
    first = True
    while pes:
        chunk = pes[:184]
        pes = pes[184:]
        stuff = 184 - len(chunk)
        c = cc.get(pid, -1) + 1 & 0x0F
        cc[pid] = c
        if stuff:
            af = bytes([stuff - 1]) + (b"\x00" + b"\xff" * (stuff - 2) if stuff >= 2 else b"")
            out += bytes([0x47, (0x40 if first else 0) | (pid >> 8), pid & 0xFF, 0x30 | c]) + af + chunk
        else:
            out += bytes([0x47, (0x40 if first else 0) | (pid >> 8), pid & 0xFF, 0x10 | c]) + chunk
        first = False
    return out


PAT = (b"\x47\x40\x00\x10" + b"\x00" + b"\x00\xb0\x0d\x00\x01\xc1\x00\x00\x00\x01\xe1\x00" + b"\x00\x00\x00\x00").ljust(188, b"\xff")
PMT = (b"\x47\x41\x00\x10" + b"\x00" + b"\x02\xb0\x12\x00\x01\xc1\x00\x00\xe1\x01\xf0\x00" + b"\x1b\xe1\x01\xf0\x00"
       + b"\x00\x00\x00\x00").ljust(188, b"\xff")


def segment(frames: list[tuple], bounded=True, pts: list[float] | None = None) -> bytes:
    """frames: (idr, frame_num[, first_mb[, ref]]) each, 15 fps (pts: each frame's own instead)."""
    cc: dict = {}
    out = PAT + PMT
    for i, f in enumerate(frames):
        out += ts_packets(VPID, frame(*f), cc, bounded, pts=pts[i] if pts else 10 + i / 15)
    return out


def gop(n=8, start=0) -> list[tuple]:
    return [(True, 0)] + [(False, (start + i) % 16) for i in range(1, n)]


def video_ccs(body: bytes) -> list[int]:
    return [body[i + 3] & 0x0F for i in range(0, len(body), 188)
            if ((body[i + 1] & 0x1F) << 8 | body[i + 2]) == VPID and body[i + 3] & 0x10]


# ---- the checks -----------------------------------------------------------------------------------------------------


def test_a_clean_segment_is_whole_starts_on_an_idr_and_is_served_as_it_came():
    seg = segment(gop(8) + gop(8))
    info = hlsseg.check(seg)
    assert (info["frames"], info["idr_first"], info["broken"], info["lost"], info["size"], info["h264"]) == (
        16, True, [], 0, "352x240", True)
    assert hlsseg.repair(seg, info) is seg
    # the SPS read back as written
    s = hlsseg.parse_sps(sps(44, 30))
    assert (s["width"], s["height"], s["log2_max_frame_num"], s["gaps_allowed"]) == (704, 480, 4, False)


def test_frame_num_wraps_and_non_reference_frames_are_no_loss():
    frames = [(True, 0)] + [(False, i % 16) for i in range(1, 20)]          # 1..15, 0, 1, 2, 3: the wrap
    frames[5] = (False, 5, 0, False)                                         # a non-reference frame ...
    frames[6] = (False, 5)                                                   # ... the next has the same frame_num
    frames[7:] = [(False, (6 + i) % 16) for i in range(13)]
    assert hlsseg.check(segment(frames))["broken"] == []


def test_a_lost_reference_frame_breaks_the_chain_from_the_loss_and_the_repair_leaves_those_frames_out():
    # as the origin served it: frame_num 3 never came (a gap, the H.264 way of saying a reference frame was lost);
    # frames 1 and 2 came whole and decode: they stay, the frames from the gap to the next IDR go
    frames = gop(8) + [(True, 0), (False, 1), (False, 2), (False, 4), (False, 5), (False, 6)] + gop(4)
    seg = segment(frames)
    info = hlsseg.check(seg)
    assert info["lost"] == 1 and info["broken"] == [11, 12, 13]
    fixed = hlsseg.repair(seg, info)
    again = hlsseg.check(fixed)
    assert again["frames"] == len(frames) - 3 and again["broken"] == [] and again["idr_first"]
    ccs = video_ccs(fixed)
    assert all((b - a) % 16 == 1 for a, b in zip(ccs, ccs[1:]))             # the continuity counters run on unbroken
    assert len(fixed) % 188 == 0 and len(fixed) < len(seg)


def test_the_idr_itself_lost_breaks_everything_until_the_next_one():
    # the mid-segment IDR gone (what the 8.267 s segments showed): its P-frames refer to a frame that never came
    frames = gop(6) + [(False, 1), (False, 2), (False, 3)] + gop(4)
    info = hlsseg.check(segment(frames))
    assert info["broken"] == [6, 7, 8] and info["lost"] == 11              # frame_num 5 -> 1: 6..15, 0 missing


def test_a_picture_missing_its_first_slice_or_a_segment_starting_mid_gop_is_broken():
    frames = gop(4) + [(False, 4, 30)] + [(False, 5), (False, 6)] + gop(3)
    assert hlsseg.check(segment(frames))["broken"] == [4, 5, 6]             # from the damaged picture to the next IDR
    # a TS packet of a frame lost mid-segment (its continuity counter skips): that frame and the rest of its GOP
    seg = bytearray(segment(gop(4) + [(True, 0), (False, 1), (False, 2)] + gop(2)))
    pk = [i for i in range(0, len(seg), 188) if (seg[i + 1] & 0x1F) << 8 | seg[i + 2] == VPID]
    idr2 = [i for i in pk if seg[i + 1] & 0x40][4]                           # the second IDR's first packet
    del seg[idr2 + 188:idr2 + 376]                                           # its second packet gone
    info = hlsseg.check(bytes(seg))
    assert 4 in info["broken"] and 5 in info["broken"] and 6 in info["broken"] and 7 not in info["broken"]
    info = hlsseg.check(segment([(False, 7), (False, 8)] + gop(4)))
    assert info["idr_first"] is False and info["broken"] == [0, 1]           # no IDR before them: never a start
    assert hlsseg.check(hlsseg.repair(segment([(False, 7), (False, 8)] + gop(4))))["idr_first"] is True


def test_frames_before_a_loss_are_never_dropped():
    # #276 follow-up, the pattern of a recorded 511NY segment (Hutch, 352x240 Baseline, 15 fps, 4-bit frame_num, the
    # P-frames nal_ref_idc 2): an IDR, 42 P-frames that ffmpeg decodes clean, then frame_num 10 -> 13 (two reference
    # frames never came; ffmpeg: "Frame num gap 13 11", concealed), 16 P-frames on. The repair kept 1 frame of 59
    # (the old rule took the GOP back to its IDR); now the 43 before the gap stay.
    fns = list(range(43))
    frames = [(True, 0)] + [(False, n % 16) for n in fns[1:]] + [(False, (13 + k) % 16) for k in range(16)]
    seg = segment(frames)
    info = hlsseg.check(seg)
    assert info["frames"] == 59 and info["broken"] == list(range(43, 59)) and info["lost"] == 2 and info["playable"] == 43
    fixed = hlsseg.repair(seg, info)
    assert hlsseg.check(fixed)["frames"] == 43 and hlsseg.check(fixed)["broken"] == []
    # its successor: three gaps (1 -> 4, 4 -> 7, 9 -> 13), the first at frame 18: 18 stay, every gap counted
    fns = list(range(18)) + [4, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25] + list(range(29, 45))
    nxt = [(True, 0)] + [(False, n % 16) for n in fns[1:]]
    info = hlsseg.check(segment(nxt))
    assert info["broken"][0] == 18 and len(info["broken"]) == len(nxt) - 18 and info["lost"] >= 4
    # a non-reference frame shares its frame_num with the next reference frame: no loss
    frames = [(True, 0), (False, 1), (False, 2, 0, False), (False, 2), (False, 3)]
    assert hlsseg.check(segment(frames))["broken"] == []


def test_the_carried_state_is_only_from_the_same_sessions_segment_just_before():
    clock = Clock()
    f = Origin()
    r = make(key="", urls={}, fetch=f, clock=clock, thruway=True)
    assert r.refresh("cameras")
    assert r.get("/api/cad/cameras/1ne01370s/stream/playlist.m3u8", "")[0] == 200
    head = segment(gop(4))                                                   # ends on frame_num 3, whole
    tail = segment([(False, 4), (False, 5), (True, 0)])                      # starts mid-GOP: needs frame 3
    r._keep_segment("1ne01370s", "media_wA_100.ts", {}, head, clock.t)
    assert r._keep_segment("1ne01370s", "media_wA_101.ts", {}, tail, clock.t)["dropped"] == 0     # the one before
    r._keep_segment("1ne01370s", "media_wA_200.ts", {}, head, clock.t)
    assert r._keep_segment("1ne01370s", "media_wB_201.ts", {}, tail, clock.t)["dropped"] == 2     # another session
    r._keep_segment("1ne01370s", "media_wA_300.ts", {}, head, clock.t)
    assert r._keep_segment("1ne01370s", "media_wA_302.ts", {}, tail, clock.t)["dropped"] == 2     # a gap between


def test_a_high_profile_sps_with_a_scaling_matrix_is_read_past_and_judged():
    # (#308) one recorded camera is High profile with a scaling matrix: its SPS used to read as None, so its frames
    # were never judged. The scaling lists are read past (16 and 64 deltas, one ending early), the rest kept
    for lists in ((0, 3, 6), (), tuple(range(8))):
        sp = hlsseg.parse_sps(sps_high(lists))
        assert sp == {"log2_max_frame_num": 4, "gaps_allowed": False, "separate_colour_plane": 0, "frame_mbs_only": 1,
                      "width": 480, "height": 272}, lists
    # log2_max_frame_num over 16 (the spec's limit): a malformed SPS, not judged by (no nonsense lost counts)
    bad = b"\x67" + (Bits().u(8, 66).u(8, 0xC0).u(8, 30).ue(0).ue(13).ue(2).ue(1).u(1, 0)
                     .ue(21).ue(14).u(1, 1).u(1, 1).u(1, 0).u(1, 0)).rbsp()
    assert hlsseg.parse_sps(bad) is None
    assert hlsseg.parse_sps(b"\x67" + (Bits().u(8, 66).u(8, 0xC0).u(8, 30).ue(0).ue(12).ue(2).ue(1).u(1, 0)
                                       .ue(21).ue(14).u(1, 1).u(1, 1).u(1, 0).u(1, 0)).rbsp())["log2_max_frame_num"] == 16
    cc: dict = {}
    body = PAT + PMT
    es = [b"\x00\x00\x00\x01" + n for n in (sps_high(), pps(), slice_nal(True, 0))]
    body += ts_packets(VPID, b"".join(es), cc, pts=10)
    for i, fn in enumerate((1, 2, 4, 5), 1):                                 # frame_num 3 lost
        body += ts_packets(VPID, b"\x00\x00\x00\x01" + slice_nal(False, fn), cc, pts=10 + i / 30)
    info = hlsseg.check(body)
    assert info["h264"] and info["size"] == "480x272" and info["lost"] == 1 and info["broken"] == [3, 4]


def test_fifteen_lost_reference_frames_wrapping_frame_num_onto_itself_are_a_loss():
    # (#308) frame_num is 4 bits: 15 reference frames lost after frame_num 5 bring the next one back to 5. Two
    # reference frames in a row never share a frame_num in progressive video, so it is a loss, not a repeat
    ts = [10 + i / 15 for i in range(6)] + [10 + (6 + 15) / 15 + i / 15 for i in range(4)]
    info = hlsseg.check(segment(gop(6) + [(False, 5), (False, 6), (False, 7), (False, 8)], pts=ts))
    assert info["lost"] == 15 and info["broken"] == [6, 7, 8, 9] and info["playable"] == 6
    # a non-reference frame keeps prev_ref's frame_num + 1, the reference frame after it the same: no loss
    ok = hlsseg.check(segment(gop(3) + [(False, 3, 0, False), (False, 3), (False, 4)]))
    assert ok["lost"] == 0 and ok["broken"] == []


def test_sixteen_lost_reference_frames_show_in_the_pts():
    # 16 lost: frame_num 5 -> 6 looks like the next frame; the PTS jumped 17 frame intervals
    ts = [10 + i / 15 for i in range(6)] + [10 + (6 + 16) / 15 + i / 15 for i in range(3)]
    info = hlsseg.check(segment(gop(6) + [(False, 6), (False, 7), (False, 8)], pts=ts))
    assert info["lost"] == 16 and info["broken"] == [6, 7, 8]
    # ... across a segment's cut too (the PTS and the frame interval carried)
    a = hlsseg.check(segment(gop(6), pts=ts[:6]))
    b = hlsseg.check(segment([(False, 6), (False, 7), (False, 8)], pts=ts[6:]), a["state"])
    assert b["lost"] == 16 and b["broken"] == [0, 1, 2]


def test_a_short_pts_gap_with_frame_num_unbroken_is_no_loss():
    # a PTS gap counts only when it is long enough to hide a frame_num wrap: a shorter one with frame_num unbroken
    # lost no reference frame (non-reference frames, or the timing), and a shorter loss of reference frames shows
    # in frame_num, as every longer PTS step in the recordings does
    ts, t = [], 10.0
    for i in range(20):
        ts.append(t)
        t += 0.1 if i in (4, 11) else 1 / 30
    info = hlsseg.check(segment(gop(20), pts=ts))
    assert info["lost"] == 0 and info["broken"] == []
    # nor a frame_num repeated by a non-reference frame after a reference one (it only follows prev_ref + 1 rules)
    assert hlsseg.check(segment(gop(4) + [(False, 4, 0, False), (False, 4, 0, False), (False, 4)]))["broken"] == []


def test_a_gap_is_counted_once_however_many_frames_follow_it():
    # (#308) frame_num 3 and 4 lost after 2: the non-reference frames after the gap name 5 too, as does the
    # reference frame after them. Two lost, not six
    info = hlsseg.check(segment(gop(3) + [(False, 5, 0, False), (False, 5, 0, False), (False, 5), (False, 6)]))
    assert info["lost"] == 2 and info["broken"] == [3, 4, 5, 6]
    # a second gap, after the next reference frame, counts again
    info = hlsseg.check(segment(gop(3) + [(False, 5), (False, 6), (False, 9, 0, False), (False, 9)]))
    assert info["lost"] == 2 + 2


@pytest.mark.parametrize("cut, why", [
    (lambda s: s[:-100], "not whole"),                                       # cut mid-packet
    (lambda s: s[:188 * 3] + b"\x00" + s[188 * 3 + 1:], "sync"),
    (lambda s: b"", "empty"),
    (lambda s: s[376:], "no video stream"),                                  # no PAT / PMT
    (lambda s: PAT + PMT, "no video"),
])
def test_a_segment_cut_short_or_not_mpeg_ts_is_refused(cut, why):
    with pytest.raises(hlsseg.Bad, match=why):
        hlsseg.check(cut(segment(gop(8) + [(True, 0)])))                    # ending on an IDR: a frame of 4 packets


def test_a_last_frame_cut_short_is_left_out_not_the_whole_segment():
    # cut at a packet boundary (an origin that closed early and said no length): the frame goes, the rest plays
    seg = segment(gop(8) + [(True, 0)])                                      # ending on an IDR: a frame of 4 packets
    info = hlsseg.check(seg[:-188 * 2])
    assert info["broken"] == [8] and info["playable"] == 8
    fixed = hlsseg.repair(seg[:-188 * 2], info)
    assert hlsseg.check(fixed)["broken"] == [] and hlsseg.check(fixed)["frames"] == 8


def test_a_lost_packet_just_before_the_next_frame_breaks_the_frame_it_ended():
    seg = bytearray(segment(gop(4) + gop(4), bounded=False))               # unbounded PES: only the counters tell
    pk = [i for i in range(0, len(seg), 188) if (seg[i + 1] & 0x1F) << 8 | seg[i + 2] == VPID]
    starts = [i for i in pk if seg[i + 1] & 0x40]
    last_of_first_idr = starts[1] - 188                                      # the IDR's last packet
    del seg[last_of_first_idr:last_of_first_idr + 188]
    info = hlsseg.check(bytes(seg))
    assert 0 in info["broken"] and info["broken"][:4] == [0, 1, 2, 3] and 4 not in info["broken"]


def test_the_pat_skips_the_network_program():
    pat = (b"\x47\x40\x00\x10" + b"\x00" + b"\x00\xb0\x11\x00\x01\xc1\x00\x00" + b"\x00\x00\xe0\x10"
           + b"\x00\x01\xe1\x00" + b"\x00\x00\x00\x00").ljust(188, b"\xff")             # program 0 (NIT) first
    seg = pat + segment(gop(4))[188:]
    assert hlsseg.check(seg)["frames"] == 4


def test_a_segment_starting_mid_gop_is_judged_by_the_one_before():
    a = segment(gop(4))
    b = segment([(False, 4), (False, 5), (True, 0), (False, 1)])
    sa = hlsseg.check(a)
    assert hlsseg.check(b)["broken"] == [0, 1]                               # by itself: nothing it refers to
    assert hlsseg.check(b, sa["state"])["broken"] == []                       # after a: frame 3 came, 4 follows it
    broken_a = hlsseg.check(segment(gop(2) + [(False, 3)]))                  # a ends broken (frame 2 lost) ...
    assert hlsseg.check(b, broken_a["state"])["broken"] == [0, 1]            # ... so b's lead-in is too


def test_nothing_left_is_never_served_as_a_repair_and_what_is_served_passes_the_check():
    with pytest.raises(hlsseg.Bad, match="no picture"):
        hlsseg.repair(segment([(False, 3), (False, 4)]))
    import random
    rng = random.Random(5)
    base = segment(gop(8) + [(True, 0), (False, 1), (False, 3)] + gop(4))
    for _ in range(400):                                                     # a small fuzz: the repair invariant
        b = bytearray(base)
        for _ in range(rng.randint(1, 5)):
            n = len(b) // 188
            k = rng.randrange(n) * 188
            if rng.random() < 0.5 and n > 2:
                del b[k:k + 188]
            else:
                b[k + 4 + rng.randrange(184)] = rng.randrange(256)
        try:
            info = hlsseg.check(bytes(b))
            fixed = hlsseg.repair(bytes(b), info)
        except hlsseg.Bad:
            continue
        again = hlsseg.check(fixed)
        assert again["playable"] > 0 and len(fixed) % 188 == 0


def test_an_unbounded_pes_can_only_be_judged_by_its_packets():
    seg = segment(gop(8), bounded=False)                                     # PES length 0 (as some packagers write)
    assert hlsseg.check(seg)["frames"] == 8
    with pytest.raises(hlsseg.Bad, match="not whole"):
        hlsseg.check(seg[:-7])


def test_http_get_refuses_a_body_shorter_than_its_content_length():
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "video/MP2T")
            self.send_header("Content-Length", "1000")
            self.end_headers()
            self.wfile.write(b"\x47" * 600)                                  # then the connection closes
            self.wfile.flush()
            self.close_connection = True

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with pytest.raises(ValueError, match="cut short: 600 of 1000 bytes"):
            http_get(f"http://127.0.0.1:{srv.server_port}/media_1.ts", 5, 1 << 20)
    finally:
        srv.shutdown()


# ---- the proxy: never a partial segment, playback starting on a keyframe ----------------------------------------


class Origin(PumpFetch):
    """PumpFetch's moving chunklist with segments set per name (bytes, or an exception), else a clean one."""

    def __init__(self):
        super().__init__()
        self.segs: dict[str, list] = {}

    def __call__(self, url, timeout, limit):
        name = url.rsplit("/", 1)[1]
        if name in self.segs and self.segs[name]:
            self.urls.append(url)
            x = self.segs[name].pop(0)
            if isinstance(x, Exception):
                raise x
            return 200, {"content-type": "video/MP2T"}, x
        if name.endswith(".ts"):
            self.urls.append(url)
            return 200, {"content-type": "video/MP2T"}, segment(gop(8))
        return super().__call__(url, timeout, limit)


def opened(clock=None):
    clock = clock or Clock()
    f = Origin()
    r = make(key="", urls={}, fetch=f, clock=clock, thruway=True)
    assert r.refresh("cameras")
    assert r.get("/api/cad/cameras/1ne01370s/stream/playlist.m3u8", "")[0] == 200
    f.urls.clear()
    return r, f, clock


SEG = "/api/cad/cameras/1ne01370s/stream/"


def test_a_segment_cut_short_is_fetched_again_and_only_the_whole_one_is_served():
    r, f, _ = opened()
    whole = segment(gop(8) + [(True, 0)])
    f.segs["media_w1_1225.ts"] = [ValueError("cut short: 600 of 1000 bytes"), whole]   # http_get's answer, then whole
    code, body, ctype, _ = r.get(SEG + "media_w1_1225.ts", "")
    assert (code, body, ctype) == (200, whole, "video/MP2T") and len(f.urls) == 2
    f.segs["media_w1_1226.ts"] = [ValueError("cut short"), ValueError("cut short")]
    code, body, _, _ = r.get(SEG + "media_w1_1226.ts", "")
    assert code == 502 and "media_w1_1226.ts refused (cut short)" in "\n".join(r.logs)
    assert r._segment_locks == {}                                            # no lock left behind


def test_a_segment_refused_by_http_twice_is_asked_again_not_a_gap_for_good():
    # (#308) two transient HTTP failures: a 502 now, but not marked bad (no GAP in the players' lists); the pump and
    # the player ask again, and the whole one is served
    r, f, clock = opened()
    whole = segment(gop(8) + [(True, 0)])
    f.segs["media_w1_1226.ts"] = [ValueError("HTTP 503"), ValueError("HTTP 503"), whole]
    assert r.get(SEG + "media_w1_1226.ts", "")[0] == 502
    assert "media_w1_1226.ts" not in (r._streams["1ne01370s"].get("bad") or {})
    assert "#EXT-X-GAP" not in r.get(SEG + "chunklist_w1849562454.m3u8", "")[1].decode()
    # not asked for again at once (the pump and the player hammering a failing origin): a 502 from the hub
    n = len(f.urls)
    code, body, _, _ = r.get(SEG + "media_w1_1226.ts", "")
    assert code == 502 and b"asked again in 10 s" in body and len(f.urls) == n
    clock.t += roads_mod.STREAM_SEG_RETRY_S
    r.pump_once("1ne01370s")
    assert r._segments["1ne01370s/media_w1_1226.ts"]["body"] == whole
    assert r.get(SEG + "media_w1_1226.ts", "")[1] == whole
    # not video once, then whole: served, and not marked bad either
    r._segments.pop("1ne01370s/media_w1_1227.ts", None)                     # (the pump's copy)
    f.segs["media_w1_1227.ts"] = [b"\x47" * 1000, whole]
    assert r.get(SEG + "media_w1_1227.ts", "")[1] == whole
    assert "media_w1_1227.ts" not in (r._streams["1ne01370s"].get("bad") or {})
    # only not video BOTH times is a GAP for good: once not video and once an HTTP error, either way round, is not
    for name, tries in (("media_w1_1240.ts", [b"\x47" * 1000, ValueError("HTTP 503")]),
                        ("media_w1_1241.ts", [ValueError("HTTP 503"), b"\x47" * 1000])):
        f.segs[name] = tries
        assert r.get(SEG + name, "")[0] == 502
        assert name not in (r._streams["1ne01370s"].get("bad") or {})


def test_a_segment_broken_twice_is_a_502_and_never_cached():
    r, f, _ = opened()
    f.segs["media_w1_1226.ts"] = [b"\x47" * 1000, b"\x47" * 1000]
    code, body, _, _ = r.get(SEG + "media_w1_1226.ts", "")
    assert code == 502 and b"broken segment" in body and "1ne01370s/media_w1_1226.ts" not in r._segments
    assert "media_w1_1226.ts" in r._streams["1ne01370s"]["bad"]              # not video twice: a GAP for good
    code, body, _, _ = r.get(SEG + "media_w1_1226.ts", "")                   # asked again: a fresh (clean) fetch
    assert code == 200 and hlsseg.check(body)["broken"] == []


def test_a_damaged_segment_is_served_repaired_and_the_status_counts_it():
    r, f, _ = opened()
    damaged = segment(gop(8) + [(True, 0), (False, 1), (False, 3), (False, 4)])
    f.segs["media_w1_1227.ts"] = [damaged]
    code, body, _, _ = r.get(SEG + "media_w1_1227.ts", "")
    info = hlsseg.check(body)
    assert code == 200 and info["frames"] == 10 and info["broken"] == [] and body != damaged
    assert "1 reference frames lost at the source; 2 of 12 frames left out" in "\n".join(r.logs)
    st = json.loads(r.get(SEG + "status", "")[1])
    assert st["damaged"] == 1 and st["checked"] == 1
    n = len(f.urls)
    assert r.get(SEG + "media_w1_1227.ts", "")[1] == body and len(f.urls) == n       # the cached, repaired copy


def test_the_player_and_the_pump_asking_at_once_make_one_fetch():
    r, f, _ = opened()
    gate, started = threading.Event(), threading.Event()
    orig = f.__call__

    def slow(url, timeout, limit):
        if url.endswith("media_w1_1225.ts"):
            started.set()
            gate.wait(5)
        return orig(url, timeout, limit)
    r.fetch = slow
    out = []
    t = threading.Thread(target=lambda: out.append(r.get(SEG + "media_w1_1225.ts", "")[0]))
    t.start()
    started.wait(5)
    t2 = threading.Thread(target=lambda: out.append(r.get(SEG + "media_w1_1225.ts", "")[0]))
    t2.start()
    gate.set()
    t.join(5), t2.join(5)
    assert out == [200, 200] and [u for u in f.urls if u.endswith("media_w1_1225.ts")] == [f.urls[0]]


def test_playback_starts_on_a_segment_with_a_keyframe_to_show():
    r, f, clock = opened()
    # the chunklist's first segment has no IDR at all: nothing left after the repair
    f.segs["media_w1_1225.ts"] = [segment([(False, 3), (False, 4), (False, 5)])]
    assert r.pump_once("1ne01370s") == 3
    assert r._segments["1ne01370s/media_w1_1225.ts"]["kept"] == 0
    code, body, _, _ = r.get(SEG + "chunklist_w1849562454.m3u8", "v=dlgpage0")      # a page's first: trimmed
    text = body.decode()
    assert code == 200 and "media_w1_1225.ts" not in text and "#EXT-X-MEDIA-SEQUENCE:1226" in text
    assert text.index("media_w1_1226.ts") < text.index("media_w1_1227.ts")
    # every later list: the segment stays in its place, marked a gap (the player skips it, the sequence holds)
    text = r.get(SEG + "chunklist_w1849562454.m3u8", "v=dlgpage0")[1].decode()
    assert "#EXT-X-MEDIA-SEQUENCE:1225" in text and "#EXT-X-GAP\nmedia_w1_1225.ts" in text
    assert "#EXT-X-GAP\nmedia_w1_1226" not in text
    # asked for anyway: served as it came (no repair could make a picture of it)
    code, body, _, _ = r.get(SEG + "media_w1_1225.ts", "")
    assert code == 200 and body == r._segments["1ne01370s/media_w1_1225.ts"]["body"]
    # a page's first list (where its playback starts) also skips the ones that came damaged; later lists don't
    f.segs["media_w1_1226.ts"] = [segment(gop(4) + [(True, 0), (False, 2), (False, 3)])]
    r._segments.pop("1ne01370s/media_w1_1226.ts")
    r.pump_once("1ne01370s")
    first = r.get(SEG + "chunklist_w1849562454.m3u8", "v=dlgpage1")[1].decode()
    again = r.get(SEG + "chunklist_w1849562454.m3u8", "v=dlgpage1")[1].decode()
    assert "#EXT-X-MEDIA-SEQUENCE:1227" in first and "media_w1_1226.ts" not in first
    assert "#EXT-X-MEDIA-SEQUENCE:1225" in again and "#EXT-X-GAP\nmedia_w1_1225.ts" in again and "media_w1_1226.ts" in again
    # never trimmed to nothing: the last stays
    assert r._for_player("1ne01370s", b"#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:1225\n#EXTINF:4.0,\nmedia_w1_1225.ts\n").count(b".ts") == 1


def test_a_trimmed_head_carries_its_discontinuity_into_the_sequence():
    r, f, _ = opened()
    r._streams["1ne01370s"]["bad"] = {"media_w1_9.ts": "cut short"}
    cl = (b"#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXT-X-MEDIA-SEQUENCE:9\n#EXT-X-DISCONTINUITY-SEQUENCE:54\n"
          b"#EXT-X-DISCONTINUITY\n#EXTINF:4.0,\nmedia_w1_9.ts\n#EXTINF:4.0,\nmedia_w1_10.ts\n")
    out = r._for_player("1ne01370s", cl, start=True).decode()
    assert "#EXT-X-MEDIA-SEQUENCE:10" in out and "#EXT-X-DISCONTINUITY-SEQUENCE:55" in out
    assert "#EXT-X-DISCONTINUITY\n" not in out and out.startswith("#EXTM3U\n#EXT-X-TARGETDURATION:4\n")
    later = r._for_player("1ne01370s", cl).decode()                        # not a start: a gap in its place
    assert "#EXT-X-MEDIA-SEQUENCE:9" in later and "#EXT-X-DISCONTINUITY\n#EXTINF:4.0,\n#EXT-X-GAP\nmedia_w1_9.ts" in later
    assert r._for_player("1ne01370s", cl.replace(b"media_w1_9", b"media_w1_8")) == cl.replace(b"media_w1_9", b"media_w1_8")


def test_a_gap_in_the_middle_of_every_list():
    r, f, _ = opened()
    r._streams["1ne01370s"]["bad"] = {"media_w1_10.ts": "no video"}
    cl = (b"#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:9\n#EXTINF:4.0,\nmedia_w1_9.ts\n#EXTINF:4.0,\nmedia_w1_10.ts\n"
          b"#EXTINF:4.0,\nmedia_w1_11.ts\n")
    for start in (True, False):
        out = r._for_player("1ne01370s", cl, start=start).decode()
        assert "#EXT-X-MEDIA-SEQUENCE:9" in out and out.count(".ts") == 3 and "#EXTINF:4.0,\n#EXT-X-GAP\nmedia_w1_10.ts" in out


def test_the_check_is_cheap_enough_for_the_hub():
    import time
    seg = segment([f for _ in range(16) for f in gop(62)])                   # ~1000 frames: a minute of video
    t = time.perf_counter()
    hlsseg.repair(seg, hlsseg.check(seg))
    assert time.perf_counter() - t < 2.0 and roads_mod.STREAM_SEG_KEEP == 40
