"""Several airband AM channels from one dongle (GitHub #275): rtl_sdr IQ -> FFT channelizer -> one envelope per channel.

rtl_fm demodulates one frequency per dongle. An RTL-SDR's capture is about 2 MHz wide, and some of HPN's frequencies
come in pairs that fit in one with room to spare at the edges: 126.4 + 124.65 (NY Approach), 120.8 + 121.825 (Approach
overnight and Ground). 118.575 + 120.8 (Tower + Approach overnight, 2.2 MHz) doesn't. So a dongle given more than one frequency runs this instead of rtl_fm, as its
own process (python -m sizeup.radio.channelize ...: the numpy work never holds sizeup's GIL), with rtl_sdr under it:

    capture   rtl_sdr -f <centre> -s <rate> (8-bit IQ) on stdout, read here. plan() picks the rate and the centre:
              the smallest of RATES with every channel within EDGE_FRAC (0.35) x rate of the centre, and at least
              WIDE_RATE (2.4 MS/s) for a pair more than WIDE_SPAN_HZ (1.2 MHz) apart. The edge is kept well in: the
              R820T's analog IF filter follows the rate (about 2.05 MHz wide at 2.048 MS/s), and past it only the
              RTL2832U's decimator limits the band, which rolls off and lets aliases in (PR #289's review). Then the
              centre starts between the frequencies and moves in 5 kHz steps until no channel is within DC_GUARD of
              it (the DC spike) and no channel sits within IMAGE_GUARD of another's mirror image (the tuner's IQ
              imbalance puts a weak copy of each signal at minus its offset: two channels symmetric about the centre
              would each hear the other).
                  124.65 + 126.4     1.75 MHz apart:  2.56 MS/s, centre 125.54, offsets -0.89 / +0.86 MHz
                  120.8 + 121.825    1.025 MHz apart: 1.6 MS/s, centre 121.3275
                  118.575 + 120.8    2.225 MHz apart: doesn't fit (two dongles: the Tower's and Approach's)
    channels  overlap-save: one FFT of each block, shared by every channel; per channel only the BINS bins round its
              offset, times the channel filter (a 63-tap Kaiser low-pass, flat to +-4.3 kHz, 80 dB down from 5.3 kHz),
              and a small inverse FFT, which is the channel filtered and decimated to 16 kHz in one go. The offset is
              rounded to the nearest bin (rate / N: 16 Hz); an AM envelope doesn't care.
    demod     the envelope |z| at 16 kHz, with its DC (the carrier level: CarrierSquelch's input, as from rtl_fm -M am,
              which sizeup upsamples from 12 to 16 kHz; here it comes out at 16 kHz).
    output    float32, frames of FRAME samples per channel, the channels' frames one after the other, on stdout.
              rtl_sdr's stderr is passed through (its "No matching devices" / "usb_claim_interface" are what the
              receiver reads to say absent / busy, as with rtl_fm).

A single-frequency dongle keeps rtl_fm (sizeup/radio/am.py): at 1 MS/s inside rtl_fm it is the cheaper of the two.
The carrier over the floor reads about 1 dB higher here than from rtl_fm -s 12000: the channel filter's noise bandwidth
is about 9.6 kHz against rtl_fm's 12 kHz.

On SIGTERM (the receiver stopping it: a retune, a switch) rtl_sdr is stopped and waited for before this exits, so the
dongle is closed (rtlsdr_close) before the next program opens it.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys

import numpy as np

OUT_RATE = 16000                       # sizeup's audio rate (radio/fm.py RATE)
FRAME = OUT_RATE // 20                 # 50 ms (radio/fm.py FRAME)
RATES = (1_600_000, 2_400_000, 2_560_000)   # each a multiple of 16 kHz; 2.56 MS/s is the highest an RTL-SDR is
                                       # generally run at without losing samples
EDGE_FRAC = 0.35                       # every channel within this x rate of the centre (70% of the way to Nyquist)
WIDE_SPAN_HZ, WIDE_RATE = 1_200_000, 2_400_000   # a pair wider than this: at least this rate (the R820T's IF)
DC_GUARD = 25_000.0                    # Hz from the centre (the DC spike)
IMAGE_GUARD = 30_000.0                 # Hz between a channel and another's mirror image
STEP = 5_000.0                         # the centre moves in these steps
MAX_SHIFT = 250_000.0
MAX_SPAN_HZ = 2 * EDGE_FRAC * RATES[-1] - 2 * STEP   # widest set of frequencies one dongle can take (1.78 MHz)
BINS = 1024                            # output samples per FFT block (per channel), before the overlap
TAPS = 63                              # the channel filter's length at 16 kHz
OVERLAP = 64                           # output samples dropped per block (>= TAPS - 1)
PASS_HZ, STOP_HZ = 4_300.0, 5_300.0    # the channel filter: flat to here, 80 dB down from here


def plan(mhzs, rates=RATES) -> tuple[float, int]:
    """(centre MHz, sample rate) for these frequencies (the module doc), or ValueError when they don't fit one dongle."""
    fs = sorted({round(float(f) * 1e6) for f in mhzs})
    if not fs:
        raise ValueError("no frequencies")
    mid = (fs[0] + fs[-1]) / 2
    shifts = [0.0]
    k = 1
    while k * STEP <= MAX_SHIFT:
        shifts += [k * STEP, -k * STEP]
        k += 1
    for rate in rates:
        if fs[-1] - fs[0] > WIDE_SPAN_HZ and rate < WIDE_RATE:
            continue
        for s in shifts:
            c = round(mid + s)
            off = [f - c for f in fs]
            if max(abs(o) for o in off) > EDGE_FRAC * rate:
                continue
            if any(abs(o) < DC_GUARD for o in off):
                continue
            if any(abs(off[i] + off[j]) < IMAGE_GUARD for i in range(len(off)) for j in range(i + 1, len(off))):
                continue
            return c / 1e6, rate
    raise ValueError(f"{', '.join(f'{f / 1e6:g}' for f in fs)} MHz don't fit one dongle "
                     f"({(fs[-1] - fs[0]) / 1e6:.3f} MHz apart; at most {MAX_SPAN_HZ / 1e6:.2f})")


def channel_filter() -> np.ndarray:
    """The channel low-pass at OUT_RATE (TAPS taps, unity gain at DC)."""
    n = np.arange(TAPS) - (TAPS - 1) / 2
    fc = (PASS_HZ + STOP_HZ) / 2 / OUT_RATE
    h = 2 * fc * np.sinc(2 * fc * n) * np.kaiser(TAPS, 8.0)
    return h / h.sum()


class Channelizer:
    """feed(complex IQ at rate) -> [envelope at OUT_RATE per channel] (overlap-save, the module doc). Offsets in Hz."""

    def __init__(self, rate: int, offsets_hz, complex_out: bool = False):
        if rate % OUT_RATE:
            raise ValueError(f"{rate} isn't a multiple of {OUT_RATE}")
        self.rate, self.dec = int(rate), int(rate) // OUT_RATE
        self.n = BINS * self.dec                        # the FFT size (input samples per block)
        self.keep = (BINS - OVERLAP) * self.dec         # new input samples per block
        self.complex_out = complex_out
        bins = np.round(np.asarray(offsets_hz, float) / (self.rate / self.n)).astype(int)
        small = np.fft.fftfreq(BINS) * BINS             # the small IFFT's bin order: 0..BINS/2-1, -BINS/2..-1
        self.idx = [(int(b) + small.astype(int)) % self.n for b in bins]
        self.offsets = [float(b) * self.rate / self.n for b in bins]   # what is actually tuned (to the nearest bin)
        h = np.zeros(BINS)
        h[:TAPS] = channel_filter()
        self.H = (np.fft.fft(h) * (BINS / self.n)).astype(np.complex64)   # with the IFFT's scaling: unity gain
        self.buf = np.zeros(self.n - self.keep, np.complex64)  # the overlap carried from the block before
        self.pending = np.zeros(0, np.complex64)

    def feed(self, iq: np.ndarray) -> list[np.ndarray]:
        x = np.concatenate((self.pending, np.asarray(iq, np.complex64)))
        outs: list[list[np.ndarray]] = [[] for _ in self.idx]
        pos = 0
        while len(x) - pos >= self.keep:
            block = np.concatenate((self.buf, x[pos:pos + self.keep]))
            self.buf = block[self.keep:]
            pos += self.keep
            X = np.fft.fft(block)
            for k, idx in enumerate(self.idx):
                y = np.fft.ifft(X[idx] * self.H)[OVERLAP:]
                outs[k].append(y if self.complex_out else np.abs(y).astype(np.float32))
        self.pending = x[pos:]
        dt = np.complex64 if self.complex_out else np.float32
        return [np.concatenate(o) if o else np.zeros(0, dt) for o in outs]


def iq_u8(raw: bytes | np.ndarray) -> np.ndarray:
    """rtl_sdr's interleaved unsigned 8-bit I/Q -> complex64, full scale +-1."""
    u = np.frombuffer(raw, np.uint8) if isinstance(raw, (bytes, bytearray, memoryview)) else raw
    u = u[: len(u) & ~1]
    f = (u.astype(np.float32) - 127.4) * (1 / 128.0)
    return f.view(np.complex64)


def argv_for(python: str, rtl_sdr: str, centre_mhz: float, rate: int, offsets_hz, gain_db, dev) -> list[str]:
    """The receiver's command line for this module (radio/am.py)."""
    cmd = [python, "-m", "sizeup.radio.channelize", "--rtl-sdr", rtl_sdr, "-f", f"{centre_mhz:.6f}", "-s", str(rate)]
    if gain_db is not None:
        cmd += ["-g", f"{gain_db:g}"]
    if dev:
        cmd += ["-d", str(dev)]
    for o in offsets_hz:
        cmd += ["--offset", f"{o:.0f}"]
    return cmd


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="several AM airband channels from one RTL-SDR (sizeup #275)")
    ap.add_argument("--rtl-sdr", default="rtl_sdr")
    ap.add_argument("-f", type=float, required=True, help="centre, MHz")
    ap.add_argument("-s", type=int, required=True, help="sample rate")
    ap.add_argument("-g", type=float, default=None)
    ap.add_argument("-d", default=None)
    ap.add_argument("--offset", type=float, action="append", required=True, help="a channel's offset from the centre, Hz")
    a = ap.parse_args(argv)
    cmd = [a.rtl_sdr, "-f", str(round(a.f * 1e6)), "-s", str(a.s)]
    if a.g is not None:
        cmd += ["-g", f"{a.g:g}"]
    if a.d:
        cmd += ["-d", str(a.d)]
    cmd.append("-")
    ch = Channelizer(a.s, a.offset)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)          # stderr: ours (the receiver reads it)

    def stop(*_):
        _stop(proc)
        sys.exit(0)
    signal.signal(signal.SIGTERM, stop)
    out = sys.stdout.buffer
    fd = proc.stdout.fileno()
    chunk = 2 * ch.keep                                            # one block of 8-bit I/Q
    have: list[np.ndarray] = [np.zeros(0, np.float32) for _ in a.offset]
    raw = b""
    try:
        while True:
            b = os.read(fd, chunk - len(raw) if len(raw) < chunk else chunk)
            if not b:
                break
            raw += b
            if len(raw) < chunk:
                continue
            ys = ch.feed(iq_u8(raw))
            raw = b""
            have = [np.concatenate((h, y)) for h, y in zip(have, ys)]
            n = min(len(h) for h in have) // FRAME
            if n:
                frames = []
                for i in range(n):
                    for h in have:
                        frames.append(h[i * FRAME:(i + 1) * FRAME])
                out.write(np.concatenate(frames).astype("<f4").tobytes())
                out.flush()
                have = [h[n * FRAME:] for h in have]
    except (BrokenPipeError, KeyboardInterrupt):
        pass
    finally:
        _stop(proc)
    return proc.wait() or 0


def _stop(proc: subprocess.Popen) -> None:
    """rtl_sdr stopped and gone (the dongle closed) before this process exits."""
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)


if __name__ == "__main__":
    sys.exit(main())
