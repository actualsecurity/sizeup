# ADR 0001: The station's FM / AM squelch and segmenter stay in Python

Status: proposed · Date: 2026-11-18

## Context

The station missed its 50 ms frame budget on some FM frames (a worst frame of 70 to 80 ms was seen on the station's
i5-6400T), with about 2 s of pipe between `rtl_fm` and the Python reader. The hypothesis was that the per-frame DSP
(`sizeup/radio/fm.py`, `am.py`, `tap.py`) needs to be native.

Measured on synthetic signals (thread CPU time on an M5 Pro; the station is about 4 times slower, an estimate that
matches the station's own 70 to 80 ms):

| | Local, measured | Station, estimated |
|---|---|---|
| One frame through `NoiseSquelch.feed` and the live tap, at real-time pace | 80 to 112 µs median | 0.3 to 0.45 ms |
| One live ID decode step (`tap.py` `_mdc_step`), every 4th frame of the first 1.5 s of a carrier | 6.3 to 7.7 ms; up to 15 to 21 ms at pace | 60 to 84 ms |
| Close handlers, inline in the reader's loop (`fm.py` `_deliver`), for 30 s / 60 s / 2 min of traffic | 0.13 / 0.39 / 0.98 s | 0.5 / 1.6 / 3.9 s |
| The same for a 60 s / 114 s page | 0.24 / 0.76 s | 1.0 / 3.0 s |
| AM close (`CarrierSquelch._make`: AGC and tone search) | 5.4 to 6.0 ms per second of audio | about 22 ms per second |
| 16 channels, all busy, in one process | 16.4 % of one core, 0.1 % of frames over 50 ms late | 65 % of one core |
| 16 channels as 16 processes | 20.4 %, 0.2 % late, 1.6 GB | |

- A frame uses under 1 % of its budget. There is no per-sample Python loop in the frame path: it is one 800-point
  FFT and a few reductions in numpy.
- The overruns are the ID decode step, which decodes the whole growing buffer again each time (about 12 s of audio
  decoded for a 1.7 s window when no ID is sent), and the close handlers, which run on the thread that reads the
  pipe: a 2-minute transmission stalls the reader for an estimated 3.9 s while the carrier is still up.
- The per-frame kernel in Rust (rustfft) is 7.6 µs at pace against 92 µs: 12 times cheaper. That frees an estimated
  0.7 % of one station core per channel: about 5 % for 7 channels.
- Garbage collection is not a factor (5 collections in 600 s, longest 1.2 ms). Threads against processes makes no
  difference to lateness.
- The station runs Python's default 5 ms GIL switch interval (the hub sets 1 ms). While any other thread runs pure
  Python, an idle channel's frames were 8.2 ms late at the median, p99 47 ms; at 0.5 ms, 0.9 ms.

The FM squelch has about 60 tuned constants and is pinned by 102 test functions (278 cases); the AM one by 41 (92).

## Decision

The squelch, segmenter, tone decode, page assembly, AM tone notch and the P25 supervisors stay in Python. The
overruns are fixed where they are caused:

1. Finished segments and the AM close work go to a per-channel ordered worker, so the reader never stalls.
2. The live ID decode comes off the receiver thread, or decodes only the new audio.
3. FleetSync's bit string is built from bytes: 2.5 to 2.7 times faster, identical packets on 5 cases and 120 random
   buffers.
4. The voice filter's line notch looks only at the bins round each line: 10 to 48 times faster, identical output.
5. The station sets the 1 ms switch interval the hub already uses.

These belong to the sessions that own the radio and speech-to-text code, and were handed to them with the
benchmarks.

## Alternatives

- **Port the squelch to Rust.** The gain is about 5 % of one station core at 7 channels; the cost is re-proving 278
  test cases of behaviour calibrated on real captures, exposing the squelch's private state to the live tap, and a
  second toolchain on a station whose only binary dependency of ours today is numpy. Rejected for now.
- **A process per channel.** No latency gain, about 95 MB each. Rejected.
- **Tight numpy** (preallocated buffers): 1.3 times at pace. Not worth the churn in tuned code.

## Consequences

- After the fixes, the reader's worst case should be one frame's work. To check on the station: no frame later than
  50 ms attributable to a decode or a close, and a 2-minute transmission closing without the pipe filling.
- A Raspberry Pi 5 class board becomes workable for the analog channels; a Pi 4 only with the fixes (estimates).
- The squelch remains the largest piece of tuned DSP in Python. A port is reconsidered when a station needs 16 or
  more busy analog channels on a small board.

## If native code is wanted here later

Two parts have a clean contract and provable parity: the MDC-1200 and FleetSync decoders as a library called from
Python (pure functions, CRC-checked output; a decode step of about 7 ms locally should fall well under 1 ms, which
would allow decoding every frame), and the airband channelizer (already its own process with a byte-stream
contract, 1.2 to 2.4 % of a local core per dongle). Both gains are inferred from profiles, not measured.
