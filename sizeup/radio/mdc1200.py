"""Motorola MDC-1200 PTT-ID decoder (and a test encoder), numpy only.

MDC-1200 is 1200 bit/s "XOR-precoded" MSK on the audio channel: each bit period carries
one cycle of 1200 Hz when the bit equals the previous bit and 1.5 cycles of 1800 Hz when it
differs, phase-continuous.  A burst is

    preamble (usually 7 x 0x55; some radios send 0x00) | sync 0x07092A446F (40 bits)
    | 112-bit block [| second 112-bit block for "double packets", no new sync]

Each block is 14 bytes: op, arg, unit-ID hi, unit-ID lo, CRC lo, CRC hi, status (0),
then 7 parity bytes from a systematic rate-1/2 convolutional code
(parity bit = d[n] ^ d[n-2] ^ d[n-5] ^ d[n-6], bits taken LSB-first across bytes 0..6).
The 112 bits are interleaved 7 x 16 (stream bit s, LSB-first per byte, is sent at position
(s % 7) * 16 + s // 7) and transmitted MSB-first.  The CRC is CRC-16 poly 0x1021 over the
first four bytes with bit-reflected input and output, init 0, final xor 0xFFFF.

All of the above follows Matthew Kaufman's MDC encoder/decoder library (GPLv2),
https://github.com/atmatthewat/mdc-encode-decode -- mdc_encode.c (_enc_leader, _enc_str,
_enc_get_samp), mdc_decode.c (_procbits, _gofix single-error syndrome fixer, sync
0x07092A446F / inverse), mdc_common.c (_docrc).  Op/arg meanings follow the notes in the
OneOfEleven UV-K5 firmware (mdc1200.h): op 0x01 arg 0x80 = PTT ID (pre-ID, on key-up),
op 0x01 arg 0x00 = POST ID (on release), op 0x00 = emergency, 0x35/0x55 = double packets.

Demodulation here differs from the reference (which slices the audio level at a few clock
phases): we band-pass 900-2300 Hz with an FFT mask, take the analytic signal, and measure
the phase advance over each bit window -- 2*pi for a 1200 Hz bit, 3*pi for 1800 Hz -- which
is independent of audio polarity, amplitude, DC and the 100 Hz CTCSS tone.  The bit clock
is searched on a 1/16-bit grid by correlating against the sync word's tone pattern; each
sync hit is then decoded two ways (coherent: sign of the carrier phase at bit boundaries,
which does not propagate errors; and differential: cumulative XOR of tone decisions) plus
a few low-confidence tone flips, and only CRC-valid packets are returned.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

BAUD = 1200.0
SYNC = 0x07092A446F
SYNC_BITS = 40
BLOCK_BITS = 112
DOUBLE_OPS = (0x35, 0x55)
BAND = (900.0, 2300.0)     # pass band for the analytic signal
OVERSAMPLE = 16            # timing grid points per bit
SYNC_MAX_ERR = 6           # tone mismatches tolerated in the 40-tone sync pattern
BAUD_SCALES = (1.0, 0.992, 1.008)  # bit-clock offsets tried when the nominal clock finds nothing
FLIP_TRIES = 6             # least-confident tones tried flipped (single + pairs) on CRC failure

KINDS = {(0x01, 0x80): "pre-id", (0x01, 0x00): "post-id"}


@dataclass(frozen=True)
class MdcPacket:
    op: int
    arg: int
    unit_id: int
    crc_ok: bool
    start_s: float                    # time of the sync word's first bit
    extra: tuple[int, ...] | None = None  # 4 bytes of the second block (double packets)
    unit_hex: str = field(init=False)     # "0611" -- how radios / consoles display the ID
    kind: str = field(init=False)         # "pre-id" | "post-id" | "emergency" | "other"

    def __post_init__(self) -> None:
        object.__setattr__(self, "unit_hex", f"{self.unit_id:04X}")
        kind = "emergency" if self.op == 0x00 else KINDS.get((self.op, self.arg), "other")
        object.__setattr__(self, "kind", kind)

    def __str__(self) -> str:
        s = f"MDC {self.unit_hex} op={self.op:02X} arg={self.arg:02X} {self.kind} @{self.start_s:.3f}s"
        if self.extra is not None:
            s += " extra=" + "".join(f"{b:02X}" for b in self.extra)
        return s if self.crc_ok else s + " (bad CRC)"


# ---------------------------------------------------------------- protocol (Kaufman lib)

def _flip(v: int, nbits: int) -> int:
    return int(f"{v:0{nbits}b}"[::-1], 2)


def crc16(data: bytes | list[int]) -> int:
    """mdc_common.c _docrc()."""
    crc = 0
    for byte in data:
        c = _flip(byte, 8)
        j = 0x80
        while j:
            bit = crc & 0x8000
            crc = (crc << 1) & 0xFFFF
            if c & j:
                bit ^= 0x8000
            if bit:
                crc ^= 0x1021
            j >>= 1
    return _flip(crc, 16) ^ 0xFFFF


def _conv_parity(data7: list[int]) -> list[int]:
    csr = [0] * 7
    out = []
    for i in range(7):
        p = 0
        for j in range(8):
            csr = [(data7[i] >> j) & 1] + csr[:6]
            p |= ((csr[0] + csr[2] + csr[5] + csr[6]) & 1) << j
        out.append(p)
    return out


# position in the transmitted block of stream bit s (s = byte*8 + bit, LSB first)
_INTERLEAVE = np.array([(s % 7) * 16 + s // 7 for s in range(BLOCK_BITS)])


def _block_bits(four: tuple[int, int, int, int]) -> np.ndarray:
    """mdc_encode.c _enc_str(): 4 payload bytes -> 112 transmitted bits."""
    data = list(four)
    c = crc16(data)
    data += [c & 0xFF, c >> 8, 0]
    data += _conv_parity(data)
    stream = np.array([(data[i] >> j) & 1 for i in range(14) for j in range(8)], dtype=np.uint8)
    tx = np.zeros(BLOCK_BITS, dtype=np.uint8)
    tx[_INTERLEAVE] = stream
    return tx


def _gofix(data: list[int]) -> int:
    """mdc_decode.c _gofix(): in-place single-error correction; returns corrections made."""
    csr = [0] * 7
    syn = 0
    fixes = 0
    for i in range(7):
        for j in range(8):
            csr = [(data[i] >> j) & 1] + csr[:6]
            b = csr[0] + csr[2] + csr[5] + csr[6]
            syn = (syn << 1) & 0xFF
            if (b & 1) ^ ((data[i + 7] >> j) & 1):
                syn |= 1
            ec = bool(syn & 0x80) + bool(syn & 0x20) + bool(syn & 0x04) + bool(syn & 0x02)
            if ec >= 3:
                syn ^= 0xA6
                fi, fj = i, j - 7
                if fj < 0:
                    fi -= 1
                    fj += 8
                if fi >= 0:
                    data[fi] ^= 1 << fj
                    fixes += 1
    return fixes


def _unblock(tx: np.ndarray) -> tuple[list[int], bool]:
    """112 received bits -> (14 corrected bytes, crc ok)."""
    stream = tx[_INTERLEAVE]
    w = (1 << np.arange(8)).astype(np.int64)
    data = [int(v) for v in (stream.reshape(14, 8).astype(np.int64) * w).sum(axis=1)]
    _gofix(data)
    return data, crc16(data[:4]) == (data[5] << 8 | data[4])


def _bytes_bits(bs) -> list[int]:
    return [(b >> (7 - j)) & 1 for b in bs for j in range(8)]


# tone pattern of the sync word: tone = bit ^ previous bit (last preamble bit of 0x55 is 1;
# tones are what we measure, so the pattern is the same whatever the preamble polarity)
_SYNC_BITS = _bytes_bits(SYNC.to_bytes(5, "big"))
_SYNC_TONES = np.array([b ^ p for b, p in zip(_SYNC_BITS, [1] + _SYNC_BITS[:-1])][1:], dtype=bool)
# (the first sync tone depends on the preamble; we match tones 1..39)


# ---------------------------------------------------------------- encoder (for tests)

def encode(op: int, arg: int, unit_id: int, rate: int = 16000, *,
           extra: tuple[int, int, int, int] | None = None,
           preamble: bytes = b"\x55" * 7, amplitude: float = 0.5) -> np.ndarray:
    """MDC-1200 burst audio, as mdc_encode.c generates it (phase-continuous MSK)."""
    bits = _bytes_bits(preamble) + _SYNC_BITS
    bits += list(_block_bits((op, arg, (unit_id >> 8) & 0xFF, unit_id & 0xFF)))
    if extra is not None:
        bits += list(_block_bits(tuple(extra)))
    n = int(np.ceil(len(bits) * rate / BAUD))
    k = np.minimum((np.arange(n) * BAUD / rate).astype(int), len(bits) - 1)
    b = np.array(bits)
    prev = np.concatenate([[0], b[:-1]])
    freq = np.where(b != prev, 1800.0, 1200.0)[k]
    phase = 2 * np.pi * np.cumsum(freq) / rate
    return amplitude * np.sin(phase - phase[0])


# ---------------------------------------------------------------- decoder

def _analytic(x: np.ndarray, rate: int) -> np.ndarray:
    n = len(x)
    nfft = 1 << int(np.ceil(np.log2(max(n, 16))))
    spec = np.fft.rfft(x, nfft)
    f = np.fft.rfftfreq(nfft, 1.0 / rate)
    lo, hi = BAND
    edge = 150.0
    mask = np.clip((f - (lo - edge)) / edge, 0, 1) * np.clip(((hi + edge) - f) / edge, 0, 1)
    mask = 0.5 - 0.5 * np.cos(np.pi * mask)          # raised-cosine edges
    full = np.zeros(nfft, dtype=complex)
    full[: len(spec)] = 2 * spec * mask
    return np.fft.ifft(full)[:n]


def _decide(soft_tones: np.ndarray, coh: np.ndarray | None, prev_bit: int):
    """Yield candidate 112-bit decisions for one block, most likely first.

    soft_tones: tone soft values (>0 = 1800 Hz = bit differs from previous) for the bit
    before the block followed by the block's bits; coh: coherent soft bits for the same
    span (>0 = 1), or None; prev_bit: known value of the bit before the block.
    """
    if coh is not None:
        yield (coh[1:] > 0).astype(np.uint8)
    tones = soft_tones[1:] > 0
    base = (np.cumsum(tones) + prev_bit) % 2
    yield base.astype(np.uint8)
    order = np.argsort(np.abs(soft_tones[1:]))[:FLIP_TRIES]
    for a in range(len(order)):
        for b in range(a, len(order)):
            t = tones.copy()
            t[order[a]] ^= True
            if b != a:
                t[order[b]] ^= True
            yield ((np.cumsum(t) + prev_bit) % 2).astype(np.uint8)


def decode(audio: np.ndarray, rate: int = 16000, *, only_valid: bool = True) -> list[MdcPacket]:
    """Find MDC-1200 bursts in discriminator / audio samples; returns packets in time order.

    Only CRC-valid packets are returned unless only_valid=False (then each sync hit that
    fails the CRC yields one packet with crc_ok=False).
    """
    x = np.asarray(audio, dtype=np.float64)
    if x.ndim != 1 or len(x) < rate * (SYNC_BITS + BLOCK_BITS) / BAUD:
        return []
    x = x - x.mean()
    z = _analytic(x, rate)
    if not np.abs(z).any():
        return []
    phase = np.unwrap(np.angle(z))
    packets: list[MdcPacket] = []
    for scale in BAUD_SCALES:
        for pk in _decode_clock(z, phase, rate, rate / BAUD * scale, only_valid):
            if not any(_same(pk, q) for q in packets):
                packets.append(pk)
        if packets and all(p.crc_ok for p in packets):
            break                     # nominal clock worked; skip the off-nominal retries
    return sorted(packets, key=lambda p: p.start_s)


def _same(a: MdcPacket, b: MdcPacket) -> bool:
    return abs(a.start_s - b.start_s) < 0.05 and (a.op, a.arg, a.unit_id, a.extra, a.crc_ok) == (
        b.op, b.arg, b.unit_id, b.extra, b.crc_ok)


def _decode_clock(z: np.ndarray, phase: np.ndarray, rate: int, spb: float,
                  only_valid: bool) -> list[MdcPacket]:
    """Sync search + block decode assuming spb samples per bit."""
    K = OVERSAMPLE
    step = spb / K
    n = len(z)
    grid_t = np.arange(0, n - 1, step)
    ph = np.interp(grid_t, np.arange(n), phase)
    # tone soft value for the bit window starting at each grid point: +0.5 1800 Hz, -0.5 1200 Hz
    tone = (ph[K:] - ph[:-K]) / np.pi - 2.5
    hard = tone > 0
    nwin = len(tone) - (SYNC_BITS - 1) * K
    if nwin <= 0:
        return []
    mism = np.zeros(nwin, dtype=np.int16)
    for k in range(1, SYNC_BITS):
        mism += hard[k * K: k * K + nwin] != _SYNC_TONES[k - 1]
    cand = np.flatnonzero(mism <= SYNC_MAX_ERR)
    if not len(cand):
        return []

    # unit phasors at the grid points, for coherent decisions
    zr = np.interp(grid_t, np.arange(n), z.real)
    zi = np.interp(grid_t, np.arange(n), z.imag)
    zg = (zr + 1j * zi) / np.maximum(np.hypot(zr, zi), 1e-12)

    packets: list[MdcPacket] = []
    used_until = -1
    # one sync hit spans a few neighbouring grid points: cluster them
    clusters = np.split(cand, np.flatnonzero(np.diff(cand) > K // 2) + 1)
    for cl in clusters:
        if cl[0] <= used_until:
            continue
        # best timing first: fewest mismatches, then largest soft margin
        score = np.zeros(len(cl))
        for k in range(1, SYNC_BITS):
            s = tone[cl + k * K]
            score += np.where(_SYNC_TONES[k - 1], s, -s)
        order = np.lexsort((-score, mism[cl]))
        for m in cl[order[:3]]:
            pk = _decode_at(int(m), tone, zg, step / rate, only_valid)
            if pk is not None:
                packets.append(pk)
                used_until = m + (SYNC_BITS + BLOCK_BITS * (2 if pk.extra else 1)) * K
                break
    return packets


def _decode_at(m: int, tone: np.ndarray, zg: np.ndarray, grid_s: float,
               only_valid: bool) -> MdcPacket | None:
    """Decode the block(s) after a sync whose first bit window starts at grid index m."""
    K = OVERSAMPLE
    start_s = m * grid_s
    sync_b = np.array(_SYNC_BITS)

    def block(first_bit: int, prev_bit: int):
        # first_bit: bit index (relative to sync start) of the block's first bit
        idx = m + np.arange(first_bit - 1, first_bit + BLOCK_BITS) * K
        if idx[-1] >= len(tone):
            return None
        soft_t = tone[idx]
        # coherent: carrier phase at the *end* of each bit (start of the next) encodes it
        ends = idx + K
        coh = None
        if ends[-1] < len(zg):
            sync_ends = m + (np.arange(SYNC_BITS) + 1) * K
            ref = np.sum(zg[sync_ends] * np.where(sync_b > 0, -1.0, 1.0))  # phase of a 0 bit
            rot = zg[ends] * np.conj(ref) / max(abs(ref), 1e-12)
            coh = -rot.real
        best = None
        for bits in _decide(soft_t, coh, prev_bit):
            data, ok = _unblock(bits)
            if ok:
                return data, True
            if best is None:
                best = data
        return best, False

    res = block(SYNC_BITS, _SYNC_BITS[-1])
    if res is None:
        return None
    data, ok = res
    if not ok and only_valid:
        return None
    extra = None
    if ok and data[0] in DOUBLE_OPS:
        res2 = block(SYNC_BITS + BLOCK_BITS, int(_block_bits(tuple(data[:4]))[-1]))
        if res2 is not None and res2[1]:
            extra = tuple(res2[0][:4])
        elif only_valid:
            return None
    return MdcPacket(op=data[0], arg=data[1], unit_id=(data[2] << 8) | data[3],
                     crc_ok=ok, start_s=round(start_s, 4), extra=extra)
