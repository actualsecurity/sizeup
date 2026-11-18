# ADR 0004: Versioned schemas and conformance tests come before any port

Status: accepted (admin, 2026-11-18): Phase 2 · Date: 2026-11-18

## Context

Every interface a native component would speak was mapped from the code: 11 station-to-hub routes (with their
authentication and clip-audio encoding), the GPU worker's job API, the stream to consoles, the objects they carry, and 22 local
files and pipes on the station. The mapping was read from the code and spot-checked, not executed against it.

- **No station interface has a schema or validation.** A version field `v` is sent on five routes and read on none.
  The only negotiated capabilities are two strings in the hub's answer.
- The `traffic` event is "whatever attributes the object has" (about 50 keys, 10 of them declared); the hub stores
  any lower-case key.
- Rules a second implementation would have to copy exactly today, none of them written down: the events cursor
  (`epoch`, `seq`), one silent re-send of a POST on a dropped keep-alive, bodies read by `Content-Length` only, a
  FLAC subset the hub's decoder accepts, integers above 2^53 compared for equality in two local files, restart
  signatures that hash file bytes, and four local files that refuse an added key.
- Gaps that matter before several stations: the hub keeps **one** events cursor for all stations; a batch the hub
  always refuses is retried for good and blocks the outbox; an event the hub fails to apply is acknowledged and
  skipped; jobs are delivered at most once; the worker's `complete` is not idempotent.

Several stations, the worker pool and the nodes design (roles, capabilities reported by each node, a lease for the
one feed adapter) all add fields to these same interfaces.

## Decision

Before any component is ported, and independently of whether one ever is:

1. **Schemas.** One JSON Schema (or OpenAPI document, or byte-level vector set) per interface, in a `contracts/`
   folder, in this order: station events (the envelope and one sub-schema per event type), station state and its
   answer (including node capabilities, roles and leases), station jobs, the outbox file (DDL and behaviour:
   `epoch`, acknowledgement, drop order), speech-to-text input and the FLAC subset, logs / metrics / aircraft /
   vessels, the worker API, the capture spool (ADR 0002), the local wish and status files.
2. **Conformance tests the current Python passes.** Golden vectors recorded from the Python implementation and
   replayed against any implementation; the repo's existing OpenAPI contract tests for `/api/v1` are the harness.
3. **One compatibility rule, stated once:** unknown keys are ignored both ways; new behaviour is switched on by
   named capabilities in both directions; a version field means something; an older build can read a file a newer
   build wrote (a station rolls back in seconds).
4. **The gaps above are fixed in Python as part of this**, each with its test, by whoever maintains that code.

## Alternatives

- **Port first, document as needed.** Every rule above would be rediscovered by a production incident.
- **A generated protocol (protobuf, gRPC).** A larger change to a working HTTP + JSON link through Cloudflare, for
  no measured gain. JSON Schema describes what exists.

## Consequences

- Several stations and nodes get a tested wire contract whether or not native code follows.
- A native component, when one is justified, is built against the same vectors and run in shadow.
- Cost: the schemas and vectors must be kept in step with the code; the conformance tests make a drift fail CI.
