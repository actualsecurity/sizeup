"""Kenwood FleetSync PTT-ID decoder (sizeup/radio/fleetsync.py).

The real-recording vectors below are the first blocks decoded from sigidwiki.com's "Kenwood FleetSync" samples
(FleetSync_1200bd.mp3, FleetSync_2400bd.mp3, FleetSync_Preamble.mp3): every one passes the check and parity,
which is what makes the layout trustworthy. The recordings themselves aren't in the repo (not ours to copy);
fleetsync.decode() found the same blocks in them at 1200 and 2400 bit/s.

No recording of an emergency press was available: the emergency flag's bit is SDRTrunk's reverse engineering.
To confirm on a PCFD Kenwood: press its emergency button keyed on FG1 (with the department's OK and 60 Control
told it's a test, drill mode on in Settings), then check the station log for "FG1: EMERGENCY button
(FleetSync) from <fleet-unit>" and the radio's flags in `sizeup-station logs` (decode the kept .mdc.wav with
fleetsync.decode: kind "emergency")."""

import numpy as np
import pytest

from sizeup.radio import fleetsync as fs
from sizeup.radio import mdc1200

RATE = 16000
rng = np.random.default_rng(11)

# (64-bit first block as received, fleet-unit, kind)
REAL = [
    ("1111111010000000010001000000000000110000000000111111101011101101", "167-1002", "pre-id"),   # 1200 + 2400 bd, key-up
    ("1111110010000000010001000000000000110000000000110001111111011000", "167-1002", "post-id"),  # at release (EOT)
    ("1111111010000000111001110111110101010111110101011110011100001111", "330-3004", "pre-id"),
]


def bits(s):
    return [int(c) for c in s]


@pytest.mark.parametrize("block, fid, kind", REAL)
def test_real_blocks_pass_the_check_and_read_as_kenwood_ids(block, fid, kind):
    b = bits(block)
    assert fs.check_ok(b)
    p = fs.FleetSyncPacket(fs._num(b[:16]), fs._num(b[16:24]) + 99, fs._num(b[24:36]) + 999, 0, 0, 1200, 0.0)
    assert (p.id, p.kind, p.emergency) == (fid, kind, False)
    # our encoder makes the same block from the fields (the check computed the same way)
    assert fs.block_bits(p.flags, p.fleet, p.unit) == b
    for i in range(64):                       # any one bit wrong fails the check or the parity
        bad = list(b)
        bad[i] ^= 1
        assert not fs.check_ok(bad), i


def burst(fleet, unit, kind="pre-id", baud=1200, level=0.4, noise=0.03):
    a = fs.encode(fleet, unit, kind, baud, level=level)
    return np.concatenate([noise * rng.standard_normal(4000), a + noise * rng.standard_normal(len(a)),
                           noise * rng.standard_normal(4000)]).astype(np.float32)


@pytest.mark.parametrize("baud", [1200, 2400])
@pytest.mark.parametrize("kind", ["pre-id", "post-id", "emergency"])
def test_bursts_decode_at_both_speeds(baud, kind):
    [p] = fs.decode(burst(167, 1002, kind, baud))
    assert (p.id, p.kind, p.emergency, p.baud) == ("167-1002", kind, kind == "emergency", baud)


def test_a_weak_burst_under_voice_level_noise_still_decodes():
    [p] = fs.decode(burst(330, 3004, "emergency", 1200, level=0.15, noise=0.05))
    assert p.emergency and p.id == "330-3004"


def test_no_false_ids_from_voice_noise_tones_or_mdc():
    t = np.arange(RATE * 10) / RATE
    ph = 2 * np.pi * np.cumsum(130 + 20 * np.sin(2 * np.pi * 1.1 * t)) / RATE
    voice = 0.3 * (np.sin(2 * np.pi * 3 * t) > 0.2) * sum(np.sin(k * ph) / k for k in range(1, 12))
    for x in (voice, 0.4 * rng.standard_normal(RATE * 20), 0.4 * np.sin(2 * np.pi * 1200 * t),
              0.3 * (np.sin(2 * np.pi * 1200 * t) + np.sin(2 * np.pi * 1800 * t)),
              np.concatenate([np.zeros(2000), mdc1200.encode(0x01, 0x80, 0x6401), np.zeros(2000)]),
              np.concatenate([np.zeros(2000), mdc1200.encode(0x00, 0x80, 0x6401), np.zeros(2000)])):
        assert fs.decode(np.asarray(x, np.float32)) == []
    # and MDC never reads a FleetSync burst as an MDC ID
    assert mdc1200.decode(burst(167, 1002, "emergency")) == []


def test_mdc_emergency_and_its_acknowledge():
    [e] = mdc1200.decode(np.concatenate([np.zeros(2000), mdc1200.encode(0x00, 0x80, 0x6401), np.zeros(2000)]))
    [a] = mdc1200.decode(np.concatenate([np.zeros(2000), mdc1200.encode(0x20, 0x00, 0x6401), np.zeros(2000)]))
    assert (e.kind, e.unit_hex) == ("emergency", "6401") and a.kind == "emergency-ack"
