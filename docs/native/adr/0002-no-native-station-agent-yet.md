# ADR 0002: No native station agent yet; capture becomes its own service

Status: accepted (admin, 2026-11-18): no agent now; the capture split is to be designed with the radio code's maintainers · Date: 2026-11-18

## Context

The hypothesis was a Go agent on the station (process supervision, the outbox, the hub link, uploads, log shipping,
updates) for a lighter and more reliable station.

Production, 4.45 days of systemd and updater lines (aggregates only), and local benchmarks:

| | |
|---|---|
| The station's Python process with its DSP and `rtl_fm` children | 8.5 % of one core, 2.1 % of the box |
| trunk-recorder | 40 to 66 % of one core over the four runs of known length |
| whisper | 115 inferences an hour; its CPU was not measured |
| Everything an agent would take over | under 1 % of one core (estimated from local costs): a post costs 0.15 to 0.5 ms of CPU, log scrubbing 75 µs a line at about 14,000 lines a day, the outbox takes 389 clip events a second against a need of under 1 |
| Memory an agent would save | about none while the Python DSP stays; about 150 MB once no Python is left (estimate) |
| Memory of the station service | 117 MB idle locally with no radios; in production its peak is median 496 MB, max 1,536 MB (the control group, with `rtl_fm` and page cache: the make-up is not known); closing one 2-minute transmission takes 237 MB for a moment |
| Install weight | 398 apt packages and 1.1 GB (CI logs), nearly all for trunk-recorder and GNU Radio; the Python side is numpy |
| Restarts of the station process | 57 in 4.45 days (12.8 a day), all updates, 0 rollbacks. The window ended just before updates were coalesced into one switch per burst of deploys, so today's rate is lower |
| An update | 318 s median: 10 s of build, 300 s waiting for a quiet moment, then 8 s until the updater's poll shows the radios back (p95 11 s, max 52 s). That poll reads a radio as back once its receiver process is started, and a new squelch cannot open for 2 s, so the gap in the audio was not measured |
| Updates that gave up waiting and switched while busy | 40 of 57 |
| Updates that touched something the station process imports | 53 of the 55 whose commits could be compared (it imports 172 modules: it runs as all-in-one and also feeds the hub) |
| Of those 55: touched the 16 DSP modules / anything under `sizeup/radio/` / that folder, `app.py`, `stt.py` or the trunk-recorder deploy files | 7 / 11 / 30 |

Also found: P25 ingest marks every call already on disk as seen when it starts, so a call whose sidecar is written
while the app restarts is never ingested (an estimated 1 to 7 calls a day).

## Decision

1. **No Go agent now.** It would save under 1 % of a core and no deployment weight, and would add a second
   toolchain, an artifact path the updater does not have, a second log scrubber that must fail closed exactly like
   the first, and a rollback matrix across two implementations (about 11,700 lines in scope, pinned by at least
   7,300 lines of tests).
2. **Capture becomes its own service**, in Python first: `rtl_fm` readers, squelch, decoders and the writing of each
   finished segment (where exactly the line falls, including who owns the dongles and the alarm lane, is the
   subject of [`capture-split.md`](../capture-split.md)), restarted only when its own files or unit change (the pattern trunk-recorder's supervisor already uses). It hands
   finished segments to the app through one contract for every radio: a spool directory of finished segments (audio
   plus a JSON sidecar, written to a temporary name and renamed, with a durable reader cursor), a status file, a
   wish file, and a local stream for "talking now" and tone events.
3. **P25 ingest gets a durable cursor**, so calls that end during a restart are ingested, late and marked as late.

This ADR proposes the capture split and offers its contract (ADR 0004); the work belongs with the radio code.

## Alternatives

- **The Go agent.** As above.
- **Only coalescing updates** (one switch per burst of hub deploys, never mid-transmission). It reduces how often
  capture restarts, and is being done; it does not stop a hub-only change from restarting capture.

## Consequences

- Capture restarts fall from 55 to between about 7 and 30 over the measured window. The low end counts only today's
  16 DSP modules (a list that leaves out two modules the live tap needs and includes one that capture would not);
  the high end counts `app.py` and `stt.py` too, where the segment handlers, clip writing and voice filter live
  today. For the boundary the design proposes, a count over the commit history gives about 12. Coalescing of
  updates, now in place, lowers both sides of the comparison. A deploy can reach the rest of the station without waiting for radio silence.
- The spool contract is the seam for a native radio daemon: a Rust daemon would implement the same directory and
  files, and could be run in shadow against the Python service by comparing spools.
- A native agent is reconsidered when all three hold: a native radio daemon exists, a station class with 1 to 2 GB
  of memory or several unattended stations is planned, and the schemas of ADR 0004 exist. Then the agent, the
  three supervisors and the journal reader are the last Python on the box (about 190 MB of idle memory against an
  estimated 40 MB; the production peaks above are a separate question, and matter for a 1 to 2 GB box),
  and one static binary can replace them.

## What does not move in any case

The updater and its installer (stdlib only, 57 of 57 succeeded, tested end to end with rollback), the log scrubber
(security logic that fails closed), the station's FLAC encoder for speech-to-text input (150 to 180 times real
time; the hub accepts only its subset, and the hub's decoder is a possible native library later: see the README),
and the live state and jobs lane (coupled to the app object).
