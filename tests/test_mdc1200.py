import time
import wave
from pathlib import Path

import numpy as np
import pytest

from sizeup.radio.mdc1200 import MdcPacket, crc16, decode, encode

RATE = 16000
FIXTURES = sorted((Path(__file__).parent / "fixtures" / "mdc").glob("fg1_*.wav"))
rng = np.random.default_rng(1200)


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path)) as w:
        assert w.getframerate() == RATE and w.getnchannels() == 1 and w.getsampwidth() == 2
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float64)


def channel(audio: np.ndarray, noise: float = 0.1, invert: bool = False, scale: float = 8000) -> np.ndarray:
    """Discriminator-like audio: 100 Hz CTCSS, DC offset + drift, white noise, int16 range."""
    t = np.arange(len(audio)) / RATE
    x = audio + 0.15 * np.sin(2 * np.pi * 100 * t + 0.3) + 0.3 + 0.2 * t + noise * rng.standard_normal(len(t))
    x = -x if invert else x
    return np.clip(x * scale, -32768, 32767).astype(np.int16)


def voice_like(s: float) -> np.ndarray:
    t = np.arange(int(RATE * s)) / RATE
    f0 = 140 + 30 * np.sin(2 * np.pi * 1.3 * t)
    ph = 2 * np.pi * np.cumsum(f0) / RATE
    x = sum(np.sin(k * ph) / k for k in range(1, 20))
    return 0.3 * x * (0.5 + 0.5 * np.abs(np.sin(2 * np.pi * 3 * t)))


def pad(s: float) -> np.ndarray:
    return np.zeros(int(RATE * s))


def test_crc_matches_reference():
    # mdc_common.c _docrc: CRC-16 0x1021, reflected in/out, init 0, xorout 0xFFFF
    # = CRC-16/KERMIT (check value 0x2189) with a final inversion
    assert crc16(b"123456789") == 0x2189 ^ 0xFFFF


@pytest.mark.parametrize("unit", [0x0001, 0x0611, 0x1234, 0x5A0F, 0xFFFF])
@pytest.mark.parametrize("invert", [False, True])
def test_round_trip(unit, invert):
    audio = channel(np.concatenate([pad(0.1), encode(0x01, 0x80, unit), pad(0.1)]), invert=invert)
    pkts = decode(audio, RATE)
    assert len(pkts) == 1
    p = pkts[0]
    assert (p.op, p.arg, p.unit_id, p.crc_ok) == (0x01, 0x80, unit, True)
    assert p.unit_hex == f"{unit:04X}" and p.kind == "pre-id"
    assert p.start_s == pytest.approx(0.1 + 56 / 1200, abs=0.002)   # sync after 7-byte preamble


def test_post_id_after_voice_and_pre_id_before_voice():
    audio = np.concatenate([
        pad(0.05), encode(0x01, 0x80, 0x2468), voice_like(1.0),
        encode(0x01, 0x00, 0x2468, preamble=b"\x00" * 3), pad(0.05),
    ])
    pkts = decode(channel(audio, noise=0.15), RATE)
    assert [(p.kind, p.unit_hex) for p in pkts] == [("pre-id", "2468"), ("post-id", "2468")]
    assert pkts[0].start_s < pkts[1].start_s


def test_emergency_and_double_packet():
    audio = np.concatenate([pad(0.1), encode(0x00, 0x81, 0x0042), pad(0.2),
                            encode(0x55, 0x34, 0x5678, extra=(0x0A, 0x0B, 0x0C, 0x0D)), pad(0.1)])
    pkts = decode(channel(audio), RATE)
    assert [(p.op, p.kind, p.unit_id, p.extra) for p in pkts] == [
        (0x00, "emergency", 0x0042, None),
        (0x55, "other", 0x5678, (0x0A, 0x0B, 0x0C, 0x0D)),
    ]


@pytest.mark.parametrize("rate", [8000, 22050, 48000])
def test_other_sample_rates(rate):
    audio = np.concatenate([np.zeros(rate // 10), encode(0x01, 0x00, 0xBEEF, rate=rate), np.zeros(rate // 10)])
    audio = audio + 0.05 * rng.standard_normal(len(audio))
    assert [p.unit_hex for p in decode(audio, rate)] == ["BEEF"]


def test_bit_clock_offset_tolerated():
    for off in (0.99, 1.01):  # transmitter clock 1 % off
        audio = np.concatenate([pad(0.1), encode(0x01, 0x80, 0x0611, rate=round(RATE * off)), pad(0.1)])
        assert [p.unit_hex for p in decode(channel(audio), RATE)] == ["0611"]


def test_noise_and_voice_give_no_packets():
    assert decode(channel(pad(5.0), noise=0.5), RATE) == []
    assert decode(channel(voice_like(5.0), noise=0.05), RATE) == []
    assert decode(np.zeros(RATE), RATE) == []
    assert decode(np.zeros(100), RATE) == []


def wipe(burst: np.ndarray, first_bit: int, nbits: int) -> np.ndarray:
    """Overwrite nbits bit periods (counted from the burst start) with a 1500 Hz tone."""
    a, n = int(first_bit * RATE / 1200), int(nbits * RATE / 1200)
    out = burst.copy()
    out[a: a + n] = 0.5 * np.sin(2 * np.pi * 1500 * np.arange(n) / RATE)
    return out


def test_error_burst_corrected_by_interleave_and_fec():
    burst = wipe(encode(0x01, 0x80, 0x1234), 56 + 40 + 30, 16)   # 16 data bits destroyed
    pkts = decode(channel(np.concatenate([pad(0.1), burst, pad(0.1)]), noise=0.02), RATE)
    assert [(p.unit_hex, p.crc_ok) for p in pkts] == [("1234", True)]


def test_corrupted_burst_is_rejected_unless_asked():
    burst = wipe(encode(0x01, 0x80, 0x1234), 56 + 40 + 10, 90)
    audio = channel(np.concatenate([pad(0.1), burst, pad(0.1)]), noise=0.02)
    assert decode(audio, RATE) == []
    bad = decode(audio, RATE, only_valid=False)
    assert bad and not any(p.crc_ok for p in bad)


def test_fast():
    audio = channel(np.concatenate([pad(0.15), encode(0x01, 0x80, 0x1234), pad(0.15)]))[: RATE // 2]
    decode(audio, RATE)
    t = time.perf_counter()
    for _ in range(10):
        decode(audio, RATE)
    assert (time.perf_counter() - t) / 10 < 0.05


# ---- real captures: Port Chester FD fireground (453.0375 MHz), rtl_fm 16 kHz discriminator

def test_fixtures_present():
    assert len(FIXTURES) == 5


@pytest.mark.parametrize("path", FIXTURES, ids=[p.stem for p in FIXTURES])
def test_real_fixture(path):
    pkts = decode(read_wav(path), RATE)
    print(path.name, [str(p) for p in pkts])
    assert len(pkts) == 1
    p = pkts[0]
    assert isinstance(p, MdcPacket) and p.crc_ok
    # all five are post-IDs (key release) from the same bench-test handheld
    assert (p.op, p.arg, p.unit_hex, p.kind) == (0x01, 0x00, "0611", "post-id")
    assert 0.1 <= p.start_s <= 0.25
