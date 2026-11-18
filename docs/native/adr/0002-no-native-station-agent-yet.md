# ADR 0002: No native station agent yet; capture becomes its own service

Status: proposed · Date: 2026-11-18

## Context

The hypothesis was a Go agent on the station (process supervision, the outbox, the hub link, uploads, log shipping,
updates) for a lighter and more reliable station.

Production, 4.45 days of systemd and updater lines (aggregates only), and local benchmarks:

| | |
|---|---|
| The station's Python process with its DSP and `rtl_fm` children | 8.5 % of one core, 2.1 % of the box |
| trunk-recorder | 40 to 66 % of one core |
| Everything an agent would take over | under 1 % of one core: a post costs 0.15 to 0.5 ms of CPU, log scrubbing 75 µs a line at 14,100 lines a day, the outbox takes 389 clip events a second against a need of under 1 |
| Memory an agent would save | about none while the Python DSP stays; about 150 MB once no Python is left (estimate) |
| Install weight | 398 apt packages and 1.1 GB, nearly all for trunk-recorder and GNU Radio; the Python side is numpy |
| Restarts of the station process | 57 in 4.45 days (12.8 a day), all updates, 0 rollbacks |
| An update | 318 s median: 10 s of build, 300 s waiting for a quiet moment, then about 8 s until the radios are back |
| Updates that gave up waiting and switched while busy | 40 of 57 |
| Updates that touched something the station process imports | 53 of 55 (it imports 172 modules: it runs as all-in-one and also feeds the hub) |
| Updates that touched the 16 modules the capture path needs | 7 of 55 |

Also found: P25 ingest marks every call already on disk as seen when it starts, so a call whose sidecar is written
while the app restarts is never ingested (an estimated 1 to 7 calls a day).

## Decision

1. **No Go agent now.** It would save under 1 % of a core and no deployment weight, and would add a second
   toolchain, an artifact path the updater does not have, a second log scrubber that must fail closed exactly like
   the first, and a rollback matrix across two implementations (about 11,700 lines in scope, pinned by at least
   7,300 lines of tests).
2. **Capture becomes its own service**, in Python first: `rtl_fm` readers, squelch, decoders and clip writing,
   restarted only when its own files or unit change (the pattern trunk-recorder's supervisor already uses). It hands
   finished segments to the app through one contract for every radio: a spool directory of finished segments (audio
   plus a JSON sidecar, written to a temporary name and renamed, with a durable reader cursor), a status file, a
   wish file, and a local stream for "talking now" and tone events.
3. **P25 ingest gets a durable cursor**, so calls that end during a restart are ingested, late and marked as late.

The capture split is the radio session's area; this ADR proposes it and offers the contract (ADR 0004).

## Alternatives

- **The Go agent.** As above.
- **Only coalescing updates** (one switch per burst of hub deploys, never mid-transmission). It reduces how often
  capture restarts, and is being done; it does not stop a hub-only change from restarting capture.

## Consequences

- Capture restarts fall from 55 to about 7 to 11 over the measured window. A deploy can reach the rest of the
  station without waiting for radio silence.
- The spool contract is the seam for a native radio daemon: a Rust daemon would implement the same directory and
  files, and could be run in shadow against the Python service by comparing spools.
- A native agent is reconsidered when all three hold: a native radio daemon exists, a station class with 1 to 2 GB
  of memory or several unattended stations is planned, and the schemas of ADR 0004 exist. Then the agent, the
  three supervisors and the journal reader are the last Python on the box (about 190 MB against an estimated 40 MB),
  and one static binary can replace them.

## What does not move in any case

The updater and its installer (stdlib only, 57 of 57 succeeded, tested end to end with rollback), the log scrubber
(security logic that fails closed), the FLAC codec for speech-to-text input (150 to 180 times real time, and the
hub accepts only its subset), and the live state and jobs lane (coupled to the app object).
