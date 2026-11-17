"""Lossless FLAC for mono 16-bit audio, in numpy alone (no libFLAC / libsndfile / ffmpeg on the hub or the station).

    encode(pcm, rate) -> bytes     a standard FLAC stream (any decoder plays it: libFLAC, ffmpeg, AWS Transcribe)
    decode(data) -> (pcm, rate)    the samples back, bit-exact (checked against the stream's own MD5)

What it writes is a small, valid subset of FLAC: one STREAMINFO block (with the MD5 of the samples), fixed 1024-sample
blocks, and for each block the smallest of CONSTANT, VERBATIM or a FIXED predictor (order 0-4) with one Rice partition.
decode() reads that subset (any block size, Rice partition order and parameter, the escape code, wasted bits; mono
16-bit only) and refuses anything else (LPC subframes, other sample sizes, more channels): it is for our own blobs, the
station's speech-to-text input (sizeup/cloud/sttinput.py), not a general decoder.
"""

from __future__ import annotations

import hashlib
import struct
import numpy as np

BLOCK = 1024
MAX_ORDER = 4
MAX_RICE = 14                       # 4-bit Rice parameters; 15 is the escape code
RATE_CODES = {88200: 1, 176400: 2, 192000: 3, 8000: 4, 16000: 5, 22050: 6, 24000: 7, 32000: 8, 44100: 9,
              48000: 10, 96000: 11}


class FlacError(ValueError):
    pass


def _crc_table(poly: int, width: int) -> list[int]:
    top, mask = 1 << (width - 1), (1 << width) - 1
    out = []
    for b in range(256):
        c = b << (width - 8)
        for _ in range(8):
            c = ((c << 1) ^ poly) if c & top else (c << 1)
        out.append(c & mask)
    return out


_CRC8 = _crc_table(0x07, 8)
_CRC16 = _crc_table(0x8005, 16)
_POW16: np.ndarray = np.zeros(0, np.uint16)


def crc8(data: bytes) -> int:
    c = 0
    for b in data:
        c = _CRC8[c ^ b]
    return c


def crc16(data: bytes) -> int:
    """FLAC's frame CRC (x^16 + x^15 + x^2 + 1, no reflection, starting at 0). It is linear: the CRC of a message is
    the XOR of x^(16 + i) mod P over its set bits i (counted from the end), so numpy does it in one pass."""
    global _POW16
    bits = np.unpackbits(np.frombuffer(data, np.uint8))
    if len(bits) > len(_POW16):
        n = max(len(bits), 2 * len(_POW16), 1 << 15)
        out, c = [], 1
        for _ in range(16):                               # x^16 mod P
            c = ((c << 1) ^ 0x18005) if c & 0x8000 else (c << 1)
        for _ in range(n):
            out.append(c)
            c = ((c << 1) ^ 0x18005) if c & 0x8000 else (c << 1)
        _POW16 = np.array(out, np.uint16)
    on = np.flatnonzero(bits[::-1])
    return int(np.bitwise_xor.reduce(_POW16[on])) if len(on) else 0


def _pack(vals: np.ndarray, widths: np.ndarray) -> bytes:
    """Big-endian bit fields -> bytes, zero-padded to a byte. Each value fits its width; a field may be wider than its
    value (a Rice code: its unary zeros, then the 1 and the low bits)."""
    widths = widths.astype(np.int64)
    total = int(widths.sum())
    bits = np.zeros((total + 7) // 8 * 8, np.uint8)
    vals = vals.astype(np.uint64)
    top = int(vals.max()).bit_length() if len(vals) else 0
    if top:
        j = np.arange(top, dtype=np.uint64)
        on = ((vals[:, None] >> j[None, :]) & np.uint64(1)).astype(bool)
        pos = np.cumsum(widths)[:, None] - 1 - j[None, :].astype(np.int64)
        bits[pos[on]] = 1
    return np.packbits(bits).tobytes()


def _utf8(n: int) -> list[tuple[int, int]]:
    """A frame number in FLAC's UTF-8-like coding, as (value, bits) bytes."""
    if n < 0x80:
        return [(n, 8)]
    for k in range(2, 8):
        if n < 1 << (5 * k + 1):
            head = ((0xFF << (8 - k)) & 0xFF) | (n >> (6 * (k - 1)))
            return [(head, 8)] + [(0x80 | ((n >> (6 * i)) & 0x3F), 8) for i in range(k - 2, -1, -1)]
    raise FlacError("frame number too large")


_KS = np.arange(MAX_RICE + 1, dtype=np.int64)


def _rice_cost(u: np.ndarray) -> tuple[int, int]:
    """(best Rice parameter, its bits) for these folded residuals, one partition."""
    if not len(u):
        return 0, 0
    bits = len(u) * (_KS + 1) + (u[None, :] >> _KS[:, None]).sum(axis=1)
    k = int(np.argmin(bits))
    return k, int(bits[k])


def _fold(r: np.ndarray) -> np.ndarray:
    return np.where(r >= 0, r << 1, ((-r) << 1) - 1).astype(np.int64)


def _subframe(x: np.ndarray) -> tuple[list[tuple[int, int]], np.ndarray | None, np.ndarray | None]:
    """The cheapest subframe for one block: (header fields, residual values, residual widths)."""
    n = len(x)
    if n and (x == x[0]).all():
        return [(0, 1), (0b000000, 6), (0, 1), (int(x[0]) & 0xFFFF, 16)], None, None
    best = (16 * n, None)                                   # VERBATIM
    for order in range(min(MAX_ORDER, n - 1) + 1):
        r = np.diff(x, n=order) if order else x
        u = _fold(r)
        k, bits = _rice_cost(u)
        bits += 16 * order + 2 + 4 + 4
        if bits < best[0]:
            best = (bits, (order, k, u))
    if best[1] is None:
        return ([(0, 1), (0b000001, 6), (0, 1)] + [(int(v) & 0xFFFF, 16) for v in x]), None, None
    order, k, u = best[1]
    head = [(0, 1), (0b001000 | order, 6), (0, 1)] + [(int(v) & 0xFFFF, 16) for v in x[:order]]
    head += [(0b00, 2), (0, 4), (k, 4)]                     # Rice, partition order 0, its parameter
    q = u >> k
    vals = (np.int64(1) << k) | (u & ((np.int64(1) << k) - 1))
    return head, vals, q + 1 + k


def encode(pcm: np.ndarray, rate: int) -> bytes:
    """Mono int16 samples -> a FLAC stream."""
    x = np.asarray(pcm)
    if x.dtype != np.int16 or x.ndim != 1:
        raise FlacError("mono int16 samples only")
    if not 0 < rate < 1 << 20:
        raise FlacError(f"rate {rate}")
    x = x.astype(np.int64)
    frames: list[bytes] = []
    for fi, at in enumerate(range(0, len(x), BLOCK)):
        blk = x[at:at + BLOCK]
        n = len(blk)
        if n == BLOCK:
            bs_code, bs_extra = 0b1010, []                  # 1024
        elif n <= 256:
            bs_code, bs_extra = 0b0110, [(n - 1, 8)]
        else:
            bs_code, bs_extra = 0b0111, [(n - 1, 16)]
        fields = [(0b11111111111110, 14), (0, 1), (0, 1), (bs_code, 4), (RATE_CODES.get(rate, 0), 4),
                  (0b0000, 4), (0b100, 3), (0, 1)] + _utf8(fi) + bs_extra
        head = _pack(np.array([v for v, _ in fields], np.uint64), np.array([w for _, w in fields]))
        head += bytes([crc8(head)])
        shead, rvals, rwidths = _subframe(blk)
        hv = np.array([v for v, _ in shead], np.uint64)
        hw = np.array([w for _, w in shead], np.int64)
        if rvals is not None:
            hv, hw = np.concatenate([hv, rvals.astype(np.uint64)]), np.concatenate([hw, rwidths])
        frame = head + _pack(hv, hw)
        frames.append(frame + struct.pack(">H", crc16(frame)))
    sizes = [len(f) for f in frames] or [0]
    total = len(x)
    md5 = hashlib.md5(x.astype("<i2").tobytes()).digest()
    info = (struct.pack(">HH", BLOCK, BLOCK) + max(min(sizes), 0).to_bytes(3, "big") + max(sizes).to_bytes(3, "big")
            + ((rate << 44) | (0 << 41) | (15 << 36) | total).to_bytes(8, "big") + md5)
    return b"fLaC" + bytes([0x80, 0, 0, len(info)]) + info + b"".join(frames)


class _Reader:
    def __init__(self, data: bytes):
        self.bits = np.unpackbits(np.frombuffer(data, np.uint8))
        self.pos = 0

    def read(self, n: int) -> int:
        if n == 0:
            return 0
        if self.pos + n > len(self.bits):
            raise FlacError("truncated")
        v = 0
        for b in self.bits[self.pos:self.pos + n].tolist():
            v = (v << 1) | b
        self.pos += n
        return v

    def signed(self, n: int) -> int:
        v = self.read(n)
        return v - (1 << n) if n and v >> (n - 1) else v

    def signed_array(self, count: int, n: int) -> np.ndarray:
        if self.pos + count * n > len(self.bits):
            raise FlacError("truncated")
        b = self.bits[self.pos:self.pos + count * n].reshape(count, n).astype(np.int64)
        self.pos += count * n
        v = b @ (np.int64(1) << np.arange(n - 1, -1, -1, dtype=np.int64)) if n else np.zeros(count, np.int64)
        return np.where(v >= (np.int64(1) << (n - 1)), v - (np.int64(1) << n), v) if n else v

    def align(self) -> None:
        self.pos = (self.pos + 7) // 8 * 8

    def rice(self, count: int, k: int) -> np.ndarray:
        """count Rice codes with parameter k -> their signed values. Vectorized: in a window of the stream, f(p) is
        where the code starting at bit p ends + 1 (its unary run's 1, then k bits); the codes' starts are f's orbit
        from here, found by doubling (f, f∘f, f∘f∘f∘f, ...). A window too short for them is made bigger."""
        if count == 0:
            return np.zeros(0, np.int64)
        n_all = len(self.bits)
        width = max(256, count * (k + 4))
        while True:
            lo, hi = self.pos, min(n_all, self.pos + width)
            seg = self.bits[lo:hi]
            size = hi - lo
            at = np.where(seg == 1, np.arange(size), size)
            nxt = np.append(np.minimum.accumulate(at[::-1])[::-1], size)     # the first 1 at or after p (size: none)
            f = np.minimum(nxt + 1 + k, size)                               # size: out of the window (stays there)
            starts = np.zeros(count, np.int64)
            jump, m = f, 1
            while m < count:
                take = min(m, count - m)
                starts[m:m + take] = jump[starts[:take]]
                if m + take >= count:
                    break
                jump, m = jump[jump], m * 2
            ends = nxt[starts]
            if ends[-1] + 1 + k <= size:
                break
            if hi == n_all:
                raise FlacError("truncated")
            width *= 4
        u = (ends - starts) << k
        for b in range(k):
            u |= seg[ends + 1 + b].astype(np.int64) << (k - 1 - b)
        self.pos = lo + int(ends[-1]) + 1 + k
        return np.where(u & 1, -((u + 1) >> 1), u >> 1)


def _residual(r: _Reader, n: int, order: int) -> np.ndarray:
    method = r.read(2)
    if method > 1:
        raise FlacError("reserved residual coding")
    pbits, esc = (4, 15) if method == 0 else (5, 31)
    porder = r.read(4)
    parts = 1 << porder
    if n % parts or (n >> porder) < order:
        raise FlacError("bad partition order")
    out = []
    for p in range(parts):
        cnt = (n >> porder) - (order if p == 0 else 0)
        k = r.read(pbits)
        if k == esc:
            w = r.read(5)
            out.append(r.signed_array(cnt, w))
        else:
            out.append(r.rice(cnt, k))
    return np.concatenate(out) if out else np.zeros(0, np.int64)


def _fixed(warm: np.ndarray, res: np.ndarray, order: int) -> np.ndarray:
    """Rebuild x from its first `order` samples and the order-th differences after them."""
    x = np.asarray(warm, np.int64)
    if order == 0:
        return res.astype(np.int64)
    # d[m] = the m-th difference of x; we know d[order] (res) and x's first `order` samples (so d[m]'s first
    # order - m values); integrate one level at a time: d[m][i] = d[m][i-1] + d[m+1][i-1].
    heads = [x]
    for _ in range(order):
        heads.append(np.diff(heads[-1]))
    d = res.astype(np.int64)
    for m in range(order - 1, -1, -1):
        h = heads[m]                                       # d[m]'s first (order - m) values
        d = np.concatenate([h, h[-1] + np.cumsum(d[len(h) - 1:])])     # (d[m+1]'s own warm-up values are in h's)
    return d


def decode(data: bytes) -> tuple[np.ndarray, int]:
    """A FLAC stream (the subset encode() writes, see above) -> (int16 samples, rate)."""
    if data[:4] != b"fLaC":
        raise FlacError("not FLAC")
    at, info = 4, None
    while True:
        if at + 4 > len(data):
            raise FlacError("truncated metadata")
        last, kind, size = data[at] >> 7, data[at] & 0x7F, int.from_bytes(data[at + 1:at + 4], "big")
        if kind == 0:
            info = data[at + 4:at + 4 + size]
        at += 4 + size
        if last:
            break
    if info is None or len(info) < 34:
        raise FlacError("no STREAMINFO")
    packed = int.from_bytes(info[10:18], "big")
    rate, chans, bps, total = packed >> 44, ((packed >> 41) & 7) + 1, ((packed >> 36) & 31) + 1, packed & ((1 << 36) - 1)
    if chans != 1 or bps != 16:
        raise FlacError(f"{chans} channel(s), {bps} bits: mono 16-bit only")
    r = _Reader(data[at:])
    out: list[np.ndarray] = []
    got = 0
    nbits = len(r.bits)
    while got < total and r.pos + 32 <= nbits:
        start = r.pos
        if r.read(15) != 0b111111111111100:
            raise FlacError("lost frame sync")
        r.read(1)                                          # blocking strategy
        bs_code, rate_code = r.read(4), r.read(4)
        chan, size_code = r.read(4), r.read(3)
        r.read(1)
        if chan != 0 or size_code not in (0, 0b100):
            raise FlacError("mono 16-bit frames only")
        b = r.read(8)                                      # the frame / sample number (UTF-8 coding)
        for _ in range(8 - (~b & 0xFF).bit_length() - 1 if b >= 0xC0 else 0):
            r.read(8)
        if bs_code == 1:
            n = 192
        elif 2 <= bs_code <= 5:
            n = 576 << (bs_code - 2)
        elif bs_code == 6:
            n = r.read(8) + 1
        elif bs_code == 7:
            n = r.read(16) + 1
        elif bs_code >= 8:
            n = 256 << (bs_code - 8)
        else:
            raise FlacError("reserved block size")
        if rate_code == 12:
            r.read(8)
        elif rate_code in (13, 14):
            r.read(16)
        head = np.packbits(r.bits[start:r.pos]).tobytes()
        if r.read(8) != crc8(head):
            raise FlacError("frame header CRC")
        if r.read(1):
            raise FlacError("bad subframe padding")
        kind = r.read(6)
        wasted = 0
        if r.read(1):
            wasted = 1
            while not r.read(1):
                wasted += 1
        w = 16 - wasted
        if kind == 0:
            x = np.full(n, r.signed(w), np.int64)
        elif kind == 1:
            x = r.signed_array(n, w)
        elif 8 <= kind <= 12:
            order = kind & 7
            warm = r.signed_array(order, w)
            x = _fixed(warm, _residual(r, n, order), order)
        else:
            raise FlacError("LPC or reserved subframe: not written by encode()")
        x = x << wasted
        r.align()
        r.read(16)                                         # CRC-16 (the MD5 below checks the whole stream)
        out.append(x)
        got += n
    pcm = np.concatenate(out) if out else np.zeros(0, np.int64)
    if len(pcm) != total:
        raise FlacError(f"{len(pcm)} samples, the header says {total}")
    if pcm.min(initial=0) < -32768 or pcm.max(initial=0) > 32767:
        raise FlacError("samples out of range")
    pcm = pcm.astype("<i2")
    if info[18:34] != bytes(16) and hashlib.md5(pcm.tobytes()).digest() != info[18:34]:
        raise FlacError("MD5 mismatch")
    return pcm.astype(np.int16), int(rate)
