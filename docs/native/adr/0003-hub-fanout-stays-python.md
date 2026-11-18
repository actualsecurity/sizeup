# ADR 0003: The hub's state push stays in Python

Status: proposed · Date: 2026-11-18

## Context

The hypothesis was a Go service for the hub's real-time state push (the per-second broadcast to consoles over SSE),
because the hub is one Python process on 0.5 vCPU and several stations, more consoles and mobile clients are
planned.

Production (the hub's own `perf` lines, those of the day before and the 10 after the state-push cache of #699; ECS metrics; the access
log's timings: numbers only). The 10 "after" samples are each the first five minutes of a new task in the two hours
after the deploy, and the same deploys carried two changes to how often the Settings page reloads, which also
lower the request figures:

| | before | after |
|---|---|---|
| The hub process, share of 0.5 vCPU | 38.0 % | 14.8 % |
| Broadcast threads | 24.5 % | 3.9 % |
| All SSE stream threads together (1 to 4 consoles) | | at most 0.4 % |
| All requests: p95 / p99 | 106 / 411 ms | 48 / 157 ms |

Local, on a synthetic CAD (CPU time; Fargate is about 3 times slower, an estimate):

| | |
|---|---|
| A state rebuild and publish, 3 / 10 / 100 open incidents | 22 / 54 / 85 ms; one regex pass (the status-word repair) is 70 % of it: memoised, 56 ms becomes 18 ms |
| A stored transmission | about 2 ms, then one full rebuild |
| A tiny request | 0.15 ms, plus 0.14 ms for the Access token check; Go's `net/http` 0.03 ms |
| SSE, 200 clients, a 550 KB change every second, stream threads | today 678 ms/s; one compression per push 49 ms/s; an asyncio prototype 6.2 ms/s; a Go reference 6.7 ms/s |
| SSE, 1,000 clients | asyncio prototype 13.1 ms/s, p50 74 ms; Go 20.2 ms/s, p50 55 ms |

A model built from these (estimates) says where 0.5 vCPU runs out with today's code: in a working incident, at one
station with 25 consoles (100 %) or three stations with 5 (114 %). It takes Fargate as 3 times slower than the
local machine (range 2 to 4.5; one production cross-check suggests about 5), two mobile clients polling per console,
and assumed rates for a working incident (0.5 transmissions a second per station, 0.4 incident changes a second). The split there is 161 ms/s of rebuilds and
ingest, 114 of per-client compression and 148 of the consoles' own requests. With four Python fixes, three stations
with 25 consoles is 76 % (over 100 % if Fargate is 5 times slower). A Go fan-out changes those cells by 0 to 3
points up to 25 consoles, whatever the factor.

## Decision

The state push, and everything else on the hub's real-time path, stays in Python. The fixes that return the gain, in
order (the first three measured, the fourth estimated):

1. Compress each push once and share the bytes among clients (today each client compresses its own copy). At 200
   streams this is 94 % of what Go saves; a thread per client in Python still costs 49 ms/s there against 6.7.
2. Cache the status-word repair per radio row.
3. Cache the verified Access token until it expires.
4. Do not rebuild the CAD state for a transmission that touches no incident.

Then: per-incident sections so a change sends one incident (about 55 KB, not 553 KB, by division), and a 1 vCPU task before
several stations (it doubles the headroom and ends the throttling of bursts over 50 ms; a second core adds little
once compression is shared, because one interpreter uses one core).

These were handed, with the benchmarks, to whoever maintains hub performance.

## Alternatives

- **A Go fan-out service.** It would own the client connections, resume, compression and backpressure, fed by
  Python with already-encoded sections over a Unix socket (prototyped: 0.05 ms per idle push and 0.4 ms per 550 KB
  change on the Python side). It must also take over the Cloudflare Access check, later the device tokens, a back
  channel for the activity log, and a place in the hot/cold handover. Estimated at 700 to 1,000 lines of Go, one to
  two weeks, and a second runtime on a hub that is one process by design, to save from nothing to a few tens of
  milliseconds of CPU a second over the fixed Python. Rejected for now.
- **An asyncio event loop in Python** for the streams. A standalone prototype (not wired into the hub) matches Go's
  CPU to 1,000 clients. Not needed at today's client counts; it is the first thing to try if streams reach the
  hundreds.

## Consequences

- To check after the fixes: stream threads' share at the same client count, and the rebuild rate per stored
  transmission.
- Reconsidered when several hundred clients hold streams at once (mobile on streams, not polling): a thread per
  client costs about 0.5 MB each and the hand-off among them starts to matter. The choice then is asyncio or Go on
  the same seam; CPU is equal, so memory, operations and where the Access check lives decide.
- What limits the hub is CAD logic (Python loops and regex over rows under the store's lock). That is removed by
  caching and fewer rebuilds, not by a port.
