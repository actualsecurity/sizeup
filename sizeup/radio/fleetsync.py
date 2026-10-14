"""Kenwood FleetSync PTT-ID decoder (and a test encoder), numpy only: the Kenwood radios' unit ID and
emergency on FG1 (Motorola APX radios send MDC-1200 instead: radio/mdc1200.py; the department has both).

FleetSync is FSK on the audio channel, NRZ: 1200 bit/s with 1200 Hz = 1 and 1800 Hz = 0 (FleetSync I, and
FleetSync II at its low speed), or 2400 bit/s with 1200 Hz = 1 and 2400 Hz = 0 (FleetSync II). A burst is

    bit reversals ...01010 | sync 0x23EB (16 bits) | 64-bit block [| more 64-bit blocks: GPS, long messages]

and the first block (all a PTT ID needs) is, MSB first:

    bits  0-15  flags. Set = 1 except where noted "(0 = set)":
                 1 emergency (0 = set)   3 lone worker (0 = set)   5 paging (0 = set)
                 6 end of transmission (0 = set: the ID sent at release)
                 7 manual   8 ANI (the PTT ID)   9 status   10 acknowledge   14 GPS follows   15 extended fleet
    bits 16-23  fleet - 99          (fleets 100 ... 354)
    bits 24-35  from unit - 999     (units 1000 ... 5094): "fleet-unit", as the radios show it: 167-1002
    bits 36-47  to unit - 999
    bits 48-62  check: XOR of a 15-bit value per set data bit, from 1 (CHECK below)
    bit  63     parity: the 64 bits have an even number of ones

The layout follows the reverse engineering in SDRTrunk (Dennis Sheirer, GPLv3: module/decode/fleetsync2,
edac/CRCFleetsync.java, bits/SyncPattern.java FLEETSYNC2), read for reference; this code is our own. It was
checked against real recordings (sigidwiki.com "Kenwood FleetSync": FleetSync_1200bd.mp3, _2400bd.mp3,
_Preamble.mp3): every burst in them decodes with a valid check and parity, at 1200 and at 2400 bit/s
(167-1002 at key-up and at release with the end-of-transmission flag; 330-3004). No recording of an
emergency press was available: the emergency flag's place is SDRTrunk's (a normal PTT ID in the recordings
has it clear, as it should). A test press on a PCFD Kenwood confirms it (tests/test_fleetsync.py says how).

Demodulation: band-pass the audio, mix each tone to DC and average over one bit (the tones' energies),
decide 1 / 0 by which is stronger, sample on a 1/8-bit grid, find the reversals + sync, and keep a block
only when its check and parity hold (a 15-bit check: noise and voice can't make an ID).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

SYNC = (0, 1, 0, 1, 0) + tuple(int(c) for c in f"{0x23EB:016b}")     # the last reversals + the sync word
BLOCK = 64
SPEEDS = ((1200, 1200.0, 1800.0), (2400, 1200.0, 2400.0))           # (bit/s, the 1 tone, the 0 tone)
GRID = 8                                                             # bit-clock phases tried per bit
CHECK = (0x740A, 0x3A05, 0x6908, 0x3484, 0x1A42, 0x0D21, 0x729A, 0x394D, 0x68AC, 0x3456, 0x1A2B, 0x791F,
         0x4885, 0x5048, 0x2824, 0x1412, 0x0A09, 0x710E, 0x3887, 0x6849, 0x402E, 0x2017, 0x6401, 0x460A,
         0x2305, 0x6588, 0x32C4, 0x1962, 0x0CB1, 0x7252, 0x3929, 0x689E, 0x344F, 0x6E2D, 0x431C, 0x218E,
         0x10C7, 0x7C69, 0x4A3E, 0x251F, 0x6685, 0x4748, 0x23A4, 0x11D2, 0x08E9, 0x707E, 0x383F, 0x6815)
ID_RE = r"\d{3}-\d{4}"                                               # "167-1002" (the registry's FleetSync IDs)


@dataclass(frozen=True)
class FleetSyncPacket:
    flags: int                       # bits 0-15 as sent
    fleet: int
    unit: int
    to_fleet: int
    to_unit: int
    baud: int
    start_s: float                   # where the block starts in the audio
    id: str = field(init=False)      # "167-1002"
    kind: str = field(init=False)    # "emergency" | "pre-id" | "post-id" | "status" | "ack" | "other"

    def flag(self, bit: int) -> bool:
        return bool(self.flags >> (15 - bit) & 1)

    @property
    def emergency(self) -> bool:
        return not self.flag(1)

    @property
    def end_of_transmission(self) -> bool:
        return not self.flag(6)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", f"{self.fleet}-{self.unit}")
        if self.emergency:
            kind = "emergency"
        elif self.flag(10):
            kind = "ack"
        elif self.flag(9):
            kind = "status"
        elif self.flag(8):
            kind = "post-id" if self.end_of_transmission else "pre-id"
        else:
            kind = "other"
        object.__setattr__(self, "kind", kind)

    def __str__(self) -> str:
        return f"FleetSync {self.id} -> {self.to_fleet}-{self.to_unit} {self.kind} flags={self.flags:04X} {self.baud} bd @{self.start_s:.3f}s"


def check_ok(bits) -> bool:
    """The block's check (bits 48-62) and even parity over all 64."""
    if len(bits) != BLOCK or sum(bits) % 2:
        return False
    c = 1
    for i in range(48):
        if bits[i]:
            c ^= CHECK[i]
    v = 0
    for b in bits[48:63]:
        v = v << 1 | b
    return c == v


def _num(bits) -> int:
    v = 0
    for b in bits:
        v = v << 1 | int(b)
    return v


def block_bits(flags: int, fleet: int, unit: int, to_unit: int | None = None) -> list[int]:
    """The 64 bits of a first block (the encoder's half; the decoder checks it)."""
    if not 100 <= fleet <= 354 or not 1000 <= unit <= 5094:
        raise ValueError("fleet 100-354, unit 1000-5094")
    to = unit if to_unit is None else to_unit
    data = ([int(c) for c in f"{flags & 0xFFFF:016b}"] + [int(c) for c in f"{fleet - 99:08b}"]
            + [int(c) for c in f"{unit - 999:012b}"] + [int(c) for c in f"{to - 999:012b}"])
    c = 1
    for i, b in enumerate(data):
        if b:
            c ^= CHECK[i]
    bits = data + [int(x) for x in f"{c:015b}"]
    return bits + [sum(bits) % 2]


def flags_for(kind: str = "pre-id") -> int:
    """The flags of a PTT ID as Kenwood radios send it (the recordings: 0xFE80 at key-up, 0xFC80 at release),
    with the emergency bit cleared for "emergency"."""
    f = 0xFE80
    if kind == "post-id":
        f &= ~(1 << (15 - 6))
    if kind == "emergency":
        f &= ~(1 << (15 - 1))
    return f & 0xFFFF


def encode(fleet: int, unit: int, kind: str = "pre-id", baud: int = 1200, rate: int = 16000,
           reversals: int = 24, level: float = 0.5) -> np.ndarray:
    """One burst as audio (phase-continuous FSK), for tests."""
    _b, one, zero = next(s for s in SPEEDS if s[0] == baud)
    bits = [k % 2 for k in range(reversals - len(SYNC[:5]))] + list(SYNC) + block_bits(flags_for(kind), fleet, unit) + [1, 0, 1, 0]
    spb = rate / baud
    n = int(round(len(bits) * spb))
    idx = np.minimum((np.arange(n) / spb).astype(int), len(bits) - 1)
    f = np.where(np.asarray(bits)[idx] == 1, one, zero)
    return (level * np.sin(2 * np.pi * np.cumsum(f) / rate)).astype(np.float32)


def _energy(x: np.ndarray, rate: int, f: float, n: int) -> np.ndarray:
    z = x * np.exp(-2j * np.pi * f * np.arange(len(x)) / rate)
    c = np.concatenate([[0], np.cumsum(z)])
    out = np.zeros(len(x))
    h = n // 2
    lo = np.clip(np.arange(len(x)) - h, 0, len(x))
    hi = np.clip(np.arange(len(x)) - h + n, 0, len(x))
    out[:] = np.abs(c[hi] - c[lo])
    return out


def decode(audio: np.ndarray, rate: int = 16000) -> list[FleetSyncPacket]:
    """Every FleetSync first block in the audio with a valid check, in time order, once each."""
    x = np.asarray(audio, dtype=np.float64)
    if len(x) < rate * (len(SYNC) + BLOCK) / 2400:
        return []
    x = x - x.mean()
    spec = np.fft.rfft(x)
    fr = np.fft.rfftfreq(len(x), 1 / rate)
    spec[(fr < 700) | (fr > 3200)] = 0
    x = np.fft.irfft(spec, len(x))
    out: list[FleetSyncPacket] = []
    sync = "".join(map(str, SYNC))
    for baud, one, zero in SPEEDS:
        spb = rate / baud
        n = max(2, int(round(spb)))
        d = _energy(x, rate, one, n) - _energy(x, rate, zero, n)
        for ph in range(GRID):
            pos = ph * spb / GRID + np.arange(int((len(d) - ph * spb / GRID) / spb)) * spb
            idx = pos.astype(int)
            idx = idx[idx < len(d)]
            bits = (d[idx] > 0).astype(np.int8)
            s = "".join("1" if b else "0" for b in bits)
            k = s.find(sync)
            while k >= 0:
                blk = [int(c) for c in s[k + len(SYNC):k + len(SYNC) + BLOCK]]
                if check_ok(blk):
                    p = FleetSyncPacket(_num(blk[:16]), _num(blk[16:24]) + 99, _num(blk[24:36]) + 999, _num(blk[16:24]) + 99,
                                        _num(blk[36:48]) + 999, baud, float(idx[k + len(SYNC)] / rate))
                    if not any(q.id == p.id and q.flags == p.flags and abs(q.start_s - p.start_s) < 0.03 for q in out):
                        out.append(p)
                k = s.find(sync, k + 1)
    return sorted(out, key=lambda p: p.start_s)
