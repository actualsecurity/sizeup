# Native code in sizeup: where it pays, measured

"Native" here means code compiled ahead of time (Go, Rust, C++), as opposed to Python. sizeup already runs native
programs it did not write: trunk-recorder, rtl_fm, whisper.cpp and readsb. This folder records, with measurements,
which of sizeup's **own** Python components would be worth moving to native code, which would not, and what has to
exist first. Nothing here is live: each move is decided in an ADR and switched only on the admin's go.

Status (2026-11-18): **Phase 1 (evidence) is done and its decisions are accepted: no component is ported now.**
Phase 2 (ADR 0004: schemas and conformance tests) is under way. Every
overrun and limit that was measured is caused by where the work runs or how often, not by the language, and each has
a Python fix that returns most of what a port would. The ADRs say what would change that.

| ADR | Decision | Status |
|---|---|---|
| [0001](adr/0001-radio-pipeline-stays-python.md) | The station's FM / AM squelch and segmenter stay in Python; take the per-transmission work off the receiver thread | accepted |
| [0002](adr/0002-no-native-station-agent-yet.md) | No Go station agent yet; split capture into its own service behind a spool contract | accepted |
| [0003](adr/0003-hub-fanout-stays-python.md) | The hub's state push stays in Python; compress once per push | accepted |
| [0004](adr/0004-contracts-first.md) | Versioned schemas and conformance tests for every station, hub and worker interface come before any port | accepted |

## Method

1. **Measure before porting.** A component is a candidate only when a benchmark or a production metric shows a cost
   that matters and that cheaper fixes do not remove.
2. **The honest alternative is measured too**, where it could be benchmarked without changing the tree: the same
   algorithm with the Python-level waste taken out. The rest is marked estimated.
3. **Contracts first.** A native part speaks a versioned, tested interface that the Python passes first, so old and
   new parts interoperate through an update and a rollback.
4. **Shadow, flag, rollback.** A native part runs beside the Python one on the same inputs, its outputs compared,
   behind a flag that is off by default.

Numbers below are tagged **[P]** measured in production (aggregates of logs and metrics only), **[L]** measured
locally on synthetic data (an Apple M5 Pro, shared and loaded: CPU time is used, and local figures are good to about
±30 %), **[E]** estimated (the reasoning is in the ADR). The station is an Intel i5-6400T (4 cores, 2016) under
WSL2; local CPU times are scaled by about ×4 for it [E]. The hub is one Python process on 0.5 vCPU of Fargate;
local CPU times are scaled by about ×3 for it [E].

## What was measured

### The station's radio pipeline (ADR 0001)

| | Measured locally [L] | On the station [E] |
|---|---|---|
| One 50 ms FM frame through the squelch and live tap, at real-time pace | 80 to 112 µs median, p99 0.2 to 0.3 ms | 0.3 to 0.45 ms: under 1 % of the frame |
| Worst recurring frame: one live ID decode step (8 per key-up that sends no ID) | 5.5 to 7.7 ms median, up to 10.8 ms; at pace up to 15 to 21 ms | 60 to 84 ms (70 to 80 ms was seen on the station) |
| Closing a traffic transmission, inline on the receiver thread: 30 s / 60 s / 2 min | 0.13 / 0.39 / 0.98 s | 0.5 / 1.6 / 3.9 s, against about 2 s of pipe |
| Closing a page: 60 s / 114 s | 0.24 / 0.76 s | 1.0 / 3.0 s |
| CPU for 1 / 4 / 8 / 16 channels, all busy | 1.3 / 4.0 / 7.2 / 16.4 % of one core | 5 / 16 / 29 / 65 % |
| The per-frame kernel at pace: Python as written / tight numpy / Rust | 92 / 68 / 7.6 µs | |
| Garbage collection over 600 s of busy audio | 5 collections, longest 1.2 ms | |
| An idle channel's frames while another thread runs pure Python (5 ms switch interval) | 8.2 ms late median, p99 47 ms; 0.9 ms median at 0.5 ms | |

The frame path is not the problem: a frame uses under 1 % of its budget. The overruns are **per-transmission
work run inline on the thread that reads the radio's pipe**: the live ID decode re-decoding a growing buffer, and
the close handlers (ID decode over the whole clip, voice filtering, tone decode). A Rust kernel is 12 times cheaper
per frame at pace, which frees about 0.7 % of one station core per channel [E].

### The station as a system (ADR 0002)

| | |
|---|---|
| The station's Python process, its DSP and its rtl_fm children | 307 CPU-s/h = 8.5 % of one core = 2.1 % of the box [P] |
| trunk-recorder (already native) | 40 to 66 % of one core over the four runs of known length, 236 to 391 MB [P] |
| whisper (already native) | 115 inferences an hour; its CPU was not measured [P] |
| What a native agent would take over (outbox, hub client, log shipping, uploads, updates) | under 1 % of one core together [E, from local per-operation costs] |
| Idle memory of the station's Python process, no radios | 117 MB, 29 threads, 172 modules [L] |
| Memory peak of the station service in production | median 496 MB, p95 1,126 MB, max 1,536 MB (its control group: includes the rtl_fm children and page cache; the make-up is not known) [P] |
| Transient memory while closing one 2-minute transmission | 237 MB [L] |
| Restarts of the station process | 57 in 4.45 days (12.8 a day), all automatic updates, no rollbacks; measured before updates were coalesced into one switch per burst of deploys [P] |
| Radios reported back after a restart | 8 s median (p95 11 s, max 52 s), by the updater's own poll of each radio's state [P]. The gap in the audio itself was not measured: a radio reads "ok" once its receiver process is started, before audio flows, and a new squelch cannot open for 2 s |
| Updates that switched while the station was still busy | 40 of 57 [P] |
| Of the 55 updates whose commits could be compared: touched the 16 DSP modules / anything under `sizeup/radio/` / that folder, `app.py`, `stt.py` or the trunk-recorder deploy files | 7 / 11 / 30 |
| From-scratch install | 398 apt packages, 1.1 GB, nearly all for trunk-recorder; the Python side is numpy (CI logs) |
| Outbox | 5,058 events/s text, 389 events/s with 96 KB clips; production needs under 1 a second [L] |

The measured load is trunk-recorder; whisper's CPU was not measured in this window, only its inference count. What
hurts is **capture restarting with every update**, and that is a process boundary, not a language. A capture
service restarted only when its own files change would have restarted between about 7 times (only today's 16 DSP
modules) and 30 times (everything under `sizeup/radio/`, `app.py` and `stt.py`, where the segment handlers and clip
writing live today) instead of 55. Where it lands depends on how the handlers are split out: for the boundary
proposed in [`capture-split.md`](capture-split.md) (capture also owns the dongles; row building, the voice filter
and speech-to-text stay in the app) a count over the commit history gives about 12.

### The hub's real-time path (ADR 0003)

| | |
|---|---|
| The hub process after the state-push cache (#699) | 14.8 % of 0.5 vCPU, from 38.0 % (ten samples, each the first five minutes of a new task, in the two hours after the deploy) [P] |
| Broadcast threads, before and after | 24.5 % to 3.9 % of 0.5 vCPU [P] |
| All SSE stream threads together, 1 to 4 consoles | at most 0.4 % of 0.5 vCPU [P] |
| A state rebuild and publish, 3 / 10 / 100 open incidents | 22 / 54 / 85 ms CPU; one regex pass is 70 % of it [L] |
| SSE fan-out, 200 clients, a 550 KB change every second | today 678 ms/s; one compression per push 49; an asyncio prototype 6.2; a Go reference 6.7 [L] |
| The same at 1,000 clients | asyncio prototype 13.1 ms/s; Go 20.2 ms/s [L] |
| Where 0.5 vCPU runs out with today's code, in a working incident | 1 station with 25 consoles (100 %), or 3 stations with 5 (114 %) [E] |
| The same with four Python fixes | 3 stations with 25 consoles: 76 % [E] |
| What those two rows assume | Fargate 3 times slower than the local machine (range 2 to 4.5; one production cross-check suggests about 5, at which 76 % is over 100 %); two mobile clients polling per console; assumed rates for a working incident |

The fan-out costs nothing today. At 200 clients its cost is real, and 94 % of what Go saves comes from compressing
each push once instead of once per client, which Python can do. What limits the hub first is the full state rebuild
on every stored transmission and the consoles' own requests: CAD logic, not plumbing.

## What this changes

The starting hypothesis was three native daemons: the station's radio pipeline in Rust, a station agent in Go and
the hub's fan-out in Go. The measurements do not support any of the three today.

What they support instead, in order:

1. **Python fixes, handed to whoever maintains that code** (the measured ones are listed in each ADR): the
   per-transmission work off the receiver thread, two output-identical speed-ups (×2.3 to ×2.8 on one decoder, ×9.5
   to ×42 on one filter step: ×2.4 to ×8.3 on the whole voice filter), a shorter GIL switch interval on the station, one compression per push, a cached status-word repair, a cached Access check, and
   a durable cursor for P25 ingest so calls that end during a restart are not lost.
2. **The capture split** (ADR 0002): capture as its own service behind a spool contract. It removes between about half and most restarts
   of capture, and it is the seam a native radio daemon would later implement.
3. **Contracts** (ADR 0004): schemas and conformance tests, needed in any case for several stations and for nodes.
4. **A larger hub task** before several stations (1 vCPU doubles the headroom; the second core adds little).

## When native code becomes worth it

| Candidate | Trigger |
|---|---|
| MDC-1200 and FleetSync decoders as a Rust library called from Python | Decoding every frame instead of every fourth is wanted, or the decode still overruns after it is off the receiver thread. Pure functions with CRC-checked output, so parity can be proven on fixtures |
| The airband channelizer in Rust | Several airband channels per dongle are switched on (it is off by default). Already its own process with a byte-stream contract |
| A native station agent (Go) | A native radio daemon exists, a station class with 1 to 2 GB of memory or several unattended stations is planned, and the schemas exist. Then the agent is the last Python on the box |
| A native fan-out (Go, or asyncio in Python) | Several hundred clients hold streams at once (mobile on streams, not polling). The CPU is equal; it would be decided by memory, operations and where the Access check lives |
| A native squelch (Rust) | A station with 16 or more busy analog channels on a small ARM board, after the Python fixes. 278 test cases pin its behaviour today |
| The hub's FLAC decoder for speech-to-text input as a native library | Its decode shows in the hub's `perf` lines as clip volume grows: decoding a 2-minute clip is 759 ms of local CPU, an estimated 2.3 s on the hub, and is not in the hub model. A pure function with bit-exact vectors: the cleanest library candidate |

## What was not measured

- Whisper's CPU on the station, and how late frames run while whisper is busy on the same four cores.
- Any frame timing on the station itself: station figures are local figures scaled.
- How long capture is actually deaf across a restart, at the audio level.
- What `rtl_fm` does when its pipe is full.
- Real radio audio: every signal was synthetic. The voice filter's cost varies about three times with how many
  spectral lines a clip has.
- Any ARM board.
- The two worker changes of ADR 0001 (they were not built), slow SSE clients, a browser on a shared-compression
  stream, and TLS, Access and the tunnel in front of the fan-out.
- The hub model at scale: nothing was run on Fargate with more stations or consoles than today.
- The P25 ingest path's own cost (bounded by the station process's 8.5 % of one core), and how the hub's camera DVR
  scales with cameras (3.4 % of 0.5 vCPU now).

## Benchmarks

The scripts that produced these numbers (per-frame and per-transmission radio benchmarks, a Rust reference kernel,
the hub's state, request and fan-out benchmarks with a Go reference, the station's start, outbox and link
benchmarks, and the production aggregate queries) are kept outside the repository for now. They move into
`native/bench/` in a follow-up, once they are checked for anything that should not be public.
