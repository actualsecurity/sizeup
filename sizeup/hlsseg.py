"""MPEG-TS / H.264 segment checks for the live camera video (#276), pure Python (the hub has no ffmpeg).

What the 511NY cameras' HLS origin serves (measured 2026-11-07 on the Hutch at Exit 17: 352x240 Constrained Baseline,
15 fps, an IDR every 62 frames, Wowza cutting a segment on each keyframe): about one segment in four arrives with
frames lost between the camera and the origin. The bytes are all there (Content-Length matches), but a reference frame
is missing, often the mid-segment IDR. Every P-frame after the loss refers to a picture the decoder never had, so the
picture smears and shifts sideways ("wrapped": the right half shows the left part, the timestamp bar split) and parts
stay frozen until the next IDR, up to two segments later. A browser can't tell; the hub can:

  check(body)   a segment's structure: whole 188-byte packets, each on its sync byte, a PAT / PMT naming an H.264
                video stream, every video PES complete (none cut short); then each frame (access unit): IDR or not,
                frame_num, nal_ref_idc and the slices' first macroblock. A reference frame whose frame_num skips one
                (H.264 7.4.3, gaps not allowed by the SPS) or whose first slice doesn't start at macroblock 0 breaks
                the chain: from there to the next IDR the frames are "broken".
  repair(body)  the segment with its broken frames left out (their TS packets dropped, the video PID's continuity
                counters renumbered): the player holds the last good picture until the next IDR instead of decoding
                garbage.

A segment the hub serves is complete (check ok), and the first segment a player is given starts on an IDR (Roads.stream
trims the chunklist's head to one).
"""

from __future__ import annotations

TS = 188
H264 = 0x1B
VIDEO_TYPES = (0x1B, 0x24, 0x02)       # H.264, HEVC, MPEG-2 video (only H.264 is looked inside)


class Bad(ValueError):
    """A segment that must not be served (truncated, not MPEG-TS, no video)."""


def _packets(body: bytes):
    """(offset, pid, pusi, cc, payload start) for each packet; Bad if the bytes aren't whole TS packets."""
    if not body:
        raise Bad("empty")
    if len(body) % TS:
        raise Bad(f"{len(body)} bytes: not whole {TS}-byte packets (cut short)")
    for i in range(0, len(body), TS):
        if body[i] != 0x47:
            raise Bad(f"no sync byte at packet {i // TS}")
        pid = ((body[i + 1] & 0x1F) << 8) | body[i + 2]
        pusi = (body[i + 1] >> 6) & 1
        afc = (body[i + 3] >> 4) & 3
        cc = body[i + 3] & 0x0F
        off = 4
        if afc & 2:
            off += 1 + body[i + 4]
        if not afc & 1 or off >= TS:
            off = TS                                  # no payload
        yield i, pid, pusi, cc, off


def _psi_section(body: bytes, i: int, off: int) -> bytes:
    p = i + off
    p += 1 + body[p]                                  # pointer field
    return body[p:i + TS]


def _streams(body: bytes) -> tuple[int | None, int | None]:
    """(video PID, its stream type) from the PAT and PMT."""
    pmt = None
    for i, pid, pusi, _, off in _packets(body):
        if off >= TS or not pusi:
            continue
        try:
            if pid == 0 and pmt is None:
                s = _psi_section(body, i, off)
                sl = ((s[1] & 0x0F) << 8) | s[2]
                for j in range(8, min(3 + sl - 4, len(s) - 3), 4):
                    if (s[j] << 8 | s[j + 1]) != 0:   # program 0 names the network PID, not a PMT
                        pmt = ((s[j + 2] & 0x1F) << 8) | s[j + 3]
                        break
            elif pmt is not None and pid == pmt:
                s = _psi_section(body, i, off)
                sl = ((s[1] & 0x0F) << 8) | s[2]
                pil = ((s[10] & 0x0F) << 8) | s[11]
                j, end = 12 + pil, 3 + sl - 4
                while j + 5 <= min(end, len(s)):
                    stype, epid = s[j], ((s[j + 1] & 0x1F) << 8) | s[j + 2]
                    if stype in VIDEO_TYPES:
                        return epid, stype
                    j += 5 + (((s[j + 3] & 0x0F) << 8) | s[j + 4])
        except IndexError:
            continue
    return None, None


def _pes(body: bytes, vpid: int) -> list[dict]:
    """The video PES packets in order: {packets: [offsets], data: payload bytes}."""
    out, cur, last_cc = [], None, None
    for i, pid, pusi, cc, off in _packets(body):
        if pid != vpid:
            continue
        if off < TS:                                  # a packet with a payload: its counter must follow the last
            if last_cc is not None and cc not in ((last_cc + 1) & 0x0F, last_cc) and cur is not None:
                cur["gap"] = True                     # a packet of this frame went missing (its tail, when this starts the next)
            last_cc = cc
        if pusi:
            cur = {"packets": [], "data": bytearray()}
            out.append(cur)
        if cur is None:                               # continuation of a PES from the segment before: keep as is
            cur = {"packets": [], "data": bytearray(), "orphan": True}
            out.append(cur)
        cur["packets"].append(i)
        if off < TS:
            cur["data"] += body[i + off:i + TS]
    return out


def _ts(b: bytes, p: int) -> float:
    v = ((b[p] >> 1) & 0x07) << 30 | b[p + 1] << 22 | (b[p + 2] >> 1) << 15 | b[p + 3] << 7 | b[p + 4] >> 1
    return v / 90000.0


def _pes_es(pes: dict) -> tuple[bytes, float | None, bool]:
    """(elementary stream bytes, pts, complete) of one PES. complete: its declared length is all there (0: unbounded)."""
    d = bytes(pes["data"])
    if pes.get("orphan") or len(d) < 9 or d[:3] != b"\x00\x00\x01":
        return b"", None, not pes.get("orphan")
    plen = (d[4] << 8) | d[5]
    hlen = d[8]
    pts = _ts(d, 9) if d[7] & 0x80 and len(d) >= 14 else None
    es = d[9 + hlen:]
    if plen:
        want = plen - 3 - hlen
        if len(es) < want:
            return es, pts, False
        es = es[:want]
    return es, pts, not pes.get("gap")


def nal_units(es: bytes) -> list[bytes]:
    """Annex B: the NAL units between start codes (00 00 01 / 00 00 00 01)."""
    out, i, n = [], 0, len(es)
    starts = []
    while True:
        j = es.find(b"\x00\x00\x01", i)
        if j < 0:
            break
        starts.append(j + 3)
        i = j + 3
    for k, s in enumerate(starts):
        e = starts[k + 1] - 3 if k + 1 < len(starts) else n
        while e > s and es[e - 1] == 0:              # the next start code's leading zero / trailing zeros
            e -= 1
        if e > s:
            out.append(es[s:e])
    return out


def _rbsp(nal: bytes, limit: int = 64) -> bytes:
    """The first bytes of a NAL's payload with emulation prevention (00 00 03) taken out."""
    out, zeros = bytearray(), 0
    for b in nal[1:limit + 1]:
        if zeros >= 2 and b == 3:
            zeros = 0
            continue
        out.append(b)
        zeros = zeros + 1 if b == 0 else 0
    return bytes(out)


class _Bits:
    def __init__(self, b: bytes):
        self.b, self.p = b, 0

    def u(self, n: int) -> int:
        v = 0
        for _ in range(n):
            if self.p >= len(self.b) * 8:
                raise IndexError("out of bits")
            v = (v << 1) | ((self.b[self.p >> 3] >> (7 - (self.p & 7))) & 1)
            self.p += 1
        return v

    def ue(self) -> int:
        z = 0
        while self.u(1) == 0:
            z += 1
            if z > 31:
                raise IndexError("bad exp-Golomb")
        return (1 << z) - 1 + self.u(z)


HIGH_PROFILES = (100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135)


def parse_sps(nal: bytes) -> dict | None:
    """{width, height, log2_max_frame_num, gaps_allowed, separate_colour_plane, frame_mbs_only} or None when it
    can't be read (scaling matrices: not needed for the cameras we have; None means "don't judge")."""
    try:
        r = _Bits(_rbsp(nal, 128))
        profile = r.u(8)
        r.u(16)                                       # constraint flags, level
        r.ue()                                        # sps id
        sep = 0
        if profile in HIGH_PROFILES:
            chroma = r.ue()
            if chroma == 3:
                sep = r.u(1)
            r.ue(), r.ue(), r.u(1)                    # bit depths, qpprime
            if r.u(1):                                # seq_scaling_matrix_present: not parsed
                return None
        log2_mfn = r.ue() + 4
        poc_type = r.ue()
        if poc_type == 0:
            r.ue()
        elif poc_type == 1:
            r.u(1), r.ue(), r.ue()
            for _ in range(r.ue()):
                r.ue()
        r.ue()                                        # max_num_ref_frames
        gaps = r.u(1)
        w_mbs, h_map = r.ue() + 1, r.ue() + 1
        fmo = r.u(1)
        return {"log2_max_frame_num": log2_mfn, "gaps_allowed": bool(gaps), "separate_colour_plane": sep,
                "frame_mbs_only": fmo, "width": w_mbs * 16, "height": h_map * 16 * (2 - fmo)}
    except IndexError:
        return None


def _slice_head(nal: bytes, sps: dict) -> tuple[int, int, int] | None:
    """(first_mb_in_slice, slice_type % 5, frame_num) of a slice NAL (types 1, 5)."""
    try:
        r = _Bits(_rbsp(nal, 32))
        first_mb, stype = r.ue(), r.ue() % 5
        r.ue()                                        # pps id
        if sps["separate_colour_plane"]:
            r.u(2)
        return first_mb, stype, r.u(sps["log2_max_frame_num"])
    except IndexError:
        return None


def check(body: bytes, state: dict | None = None) -> dict:
    """A segment's structure and frames. Bad only when it isn't MPEG-TS with video at all (empty, not whole 188-byte
    packets, a lost sync byte, no PAT / PMT, no video PES): such a body is never served. Else {frames, idr_first,
    broken (indexes of the frames to leave out), lost (reference frames missing before them), playable (frames with
    a picture that stay), sps, size: "WxH", h264: whether the frames were looked inside, state}. A frame cut short
    (the last one too: an origin that closed early) or with a packet missing is broken, not Bad.
    state: the stream's after the segment before (check's "state": its SPS, the last reference frame_num, whether
    the chain was broken), so a segment that starts mid-GOP is judged by what came before it; None: by itself."""
    state = dict(state or {})
    sps = state.get("sps")
    vpid, stype = _streams(body)
    if vpid is None:
        raise Bad("no video stream (no PAT / PMT)")
    pes = _pes(body, vpid)
    if not pes:
        raise Bad("no video in the segment")
    frames = []
    for k, p in enumerate(pes):
        es, pts, whole = _pes_es(p)
        frames.append({"es": es, "pts": pts, "whole": whole, "pes": k})
    out = {"frames": len(frames), "idr_first": False, "broken": [], "lost": 0, "playable": 0, "sps": sps, "size": "",
           "h264": stype == H264, "state": {"sps": sps, "prev_ref": None, "broken": True}}
    if stype != H264:
        out["idr_first"] = True                       # not looked inside: trusted as the origin cut it
        out["broken"] = [f["pes"] for f in frames if not f["whole"]]
        out["playable"] = len(frames) - len(out["broken"])
        return out
    prev_ref = state.get("prev_ref")
    broken = bool(state.get("broken", prev_ref is None))
    first = True
    gop: list[int] = []                               # this GOP's frames so far, after its IDR
    pictures: list[int] = []
    for f in frames:
        nals = nal_units(f["es"])
        slices = []
        idr = has_slice = False
        ref_idc = 0
        for n in nals:
            t = n[0] & 0x1F
            if t == 7:
                sp = parse_sps(n)
                if sp:
                    out["sps"] = sps = sp
            elif t in (1, 5):
                has_slice = True
                idr = idr or t == 5
                ref_idc = max(ref_idc, (n[0] >> 5) & 3)
                if sps:
                    h = _slice_head(n, sps)
                    if h:
                        slices.append(h)
        if not has_slice:                             # SEI, parameter sets, a frame's lost tail: no picture
            if not f["whole"]:
                out["broken"].append(f["pes"])
            continue
        pictures.append(f["pes"])
        if first:
            out["idr_first"] = idr and f["whole"]
            first = False
        if idr:
            broken = bool(slices) and slices[0][0] != 0          # an IDR missing its first slice
            gop = [f["pes"]] if broken else []
            prev_ref = slices[0][2] if slices else 0
        elif prev_ref is None or broken:
            broken = True                             # nothing it refers to came whole: broken until the next IDR
        elif slices:
            fn = slices[0][2]
            mx = 1 << sps["log2_max_frame_num"]
            if slices[0][0] != 0:                     # the picture's first slice is missing
                broken = True
            elif not sps["gaps_allowed"] and fn not in (prev_ref, (prev_ref + 1) % mx):
                out["lost"] += (fn - prev_ref - 1) % mx
                broken = True
            if ref_idc:
                prev_ref = fn
        elif sps is None:
            out["h264"] = False                       # slices but no SPS to read them by: not judged
        if not f["whole"]:                            # a frame with packets missing: damaged, and what follows it
            broken = True
            if idr:
                gop = [f["pes"]]
        if not idr:
            gop.append(f["pes"])
        if broken:
            # A loss is rarely whole frames only: the frames before it in its GOP are often cut short too (seen:
            # a P-frame 18 frames before the first frame_num gap). The whole GOP goes but its IDR: the player holds
            # that picture (hls.js stretches a frame over the gap) until the next IDR.
            out["broken"].extend(gop)
            gop = []
    out["broken"] = sorted(set(out["broken"]))
    dropped = set(out["broken"])
    out["playable"] = sum(1 for k in pictures if k not in dropped)
    out["state"] = {"sps": sps, "prev_ref": prev_ref, "broken": broken}
    if sps:
        out["size"] = f"{sps['width']}x{sps['height']}"
    return out


def repair(body: bytes, info: dict | None = None) -> bytes:
    """The segment with its broken frames (check's "broken") left out: their video packets dropped, the video PID's
    continuity counters renumbered so the rest reads as one unbroken run. Bad when no picture is left (a segment
    with no IDR in a broken chain): what is served always passes check, or isn't served."""
    info = info if info is not None else check(body)
    if not info["playable"]:
        raise Bad("no picture left after the repair")
    if not info["broken"]:
        return body
    vpid, _ = _streams(body)
    pes = _pes(body, vpid)
    drop = {i for k in info["broken"] for i in pes[k]["packets"]}
    out = bytearray()
    cc = None
    for i, pid, _, _, off in _packets(body):
        if i in drop:
            continue
        pkt = bytearray(body[i:i + TS])
        if pid == vpid:
            if off < TS:                              # the counter counts packets with a payload
                cc = (pkt[3] & 0x0F) if cc is None else (cc + 1) & 0x0F
            if cc is not None:
                pkt[3] = (pkt[3] & 0xF0) | cc
        out += pkt
    fixed = bytes(out)
    again = check(fixed)                              # Bad here (no video left): the caller's "not served"
    if not again["playable"]:
        raise Bad("no picture left after the repair")
    return fixed
