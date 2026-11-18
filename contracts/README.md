# Contracts

A **contract** here is one interface between a station (or any node) and the hub, written down so that a second
implementation can speak it: a JSON Schema, example bodies that must and must not validate, and tests that run the
schema against what today's Python really sends. The decision and the order of work are in ADR 0004
(`docs/native/adr/0004-contracts-first.md`, #765; this is step 1 of #758); the node fields come from
[`docs/nodes.md`](../docs/nodes.md).

A schema says what a **writer** sends. A **reader** takes everything a schema allows, treats every key as optional
(an older or a newer build may leave it out) and ignores what it does not know. Today's Python readers do ignore a
**key** they do not know, at every depth (tested). They do **not** handle a known key of the wrong **type**: some
are skipped, others raise (D5, D6 below). A writer must send the types the schema says.

## The one compatibility rule

1. **What a reader does not know, it ignores, both ways.** A key it does not know never makes it refuse a body. The
   same holds for a word: a state, a kind, a class it does not know is taken as unknown (that item is left out or
   keeps its last value), never as an error. A writer may add an optional key or a new word at any time.
2. **New behaviour is switched on by a named capability string, both ways.** Adding a key changes nothing by itself:
   the other side must have said it understands. Hub to station this is `caps` in the state answer. (So a station
   must tolerate `refused` and `reason` in a state answer, which arrive with #782: a station from before them
   ignores them, as the unknown-keys test shows.)
3. **A version field means something.** If a body carries one, its reader reads it and acts on it. No version field
   is added for show.
4. **An older build must read what a newer one wrote.** A station switches back to its previous release in seconds,
   and a hub deploy runs two builds side by side.

A change that these four cannot carry (a key removed, renamed, or given another meaning or type) needs a new name: a
new key, or a new schema, behind a capability string.

## Schemas

| schema | status | version | what |
|---|---|---|---|
| `station-state` | as built | 1 | the body of `POST /api/station/state`: who is talking, the page going out, and about every 5 s the station's status |
| `station-state-answer` | as built | 1 | the hub's 200 answer: `ok`, `build`, `caps`, `pool`, `services`, `station` |
| `station-hello-answer` | as built | 1 | the hub's 200 answer to `GET /api/station/hello` |
| `station-error` | as built | 1 | the body of a 4xx / 5xx for a station's token: `error`, `reason` |
| `stt-worker-hello` | as built | 1 | the body of `POST /api/station/stt/hello` (a pool worker connects) |
| `stt-worker-hello-answer` | as built | 1 | the hub's 200 answer: `ok`, `worker`, `poll_s`, `pool_on`, `classes` |
| `stt-worker-state` | as built | 1 | the body of `POST /api/station/stt/state` (a pool worker's heartbeat) |
| `stt-worker-state-answer` | as built | 1 | the hub's 200 answer: `ok`, `pool_on` |
| `node` | designed | 1 | a node of the registry: `id`, `name`, `kind`, roles, capabilities |
| `roles` | designed | 1 | a node's roles, `{role: options}`, with each role's options |
| `capabilities` | designed | 1 | what a node reports about its hardware and programs |
| `lease` | designed | 1 | the grant of an exactly-one role: `{role, holder, gen, ttl_s}` |
| `node-state` | designed | 1 | `station-state` plus `capabilities`, `capabilities_rev`, `roles_applied` |
| `node-state-answer` | designed | 1 | `station-state-answer` plus `capabilities_wanted`, `roles_rev`, `roles`, `leases` |
| `node-worker-hello` | designed | 1 | `stt-worker-hello` plus `capabilities`, `capabilities_rev` |
| `capture-spool-sidecar` | designed | 1 | the sidecar that completes a spool item: a segment, a P25 call, a minute of metrics |
| `capture-spool-cursor` | designed | 1 | the spool reader's cursor |
| `capture-urgent-item` | designed | 1 | an urgent item: an emergency alarm or a live page, `(key, rev)`, with its age |
| `capture-urgent-request` | designed | 1 | the body of `POST /api/station/urgent` |
| `capture-urgent-response` | designed | 1 | the hub's answers to it: per item `applied`, `duplicate`, `stale`, `rejected` |
| `capture-socket-event` | designed | 1 | a frame capture writes on its live socket |
| `capture-socket-request` | designed | 1 | a frame a client writes: a notice, or a request |
| `capture-socket-reply` | designed | 1 | capture's answer to a request |
| `capture-wish` | designed | 1 | the wish file: the desired state of capture |
| `capture-status` | designed | 1 | the status file: the observed state of capture |

**As built** is what the tests prove, no more:

- Two real stations post to a real hub over HTTP: a plain one (two FM channels, no P25), and one with everything the
  repo's fakes can give it (shared dongles in an antenna group, an AM channel, the county P25 system with
  talkgroups, a second P25 system, a past reception test). The real GPU worker client and a station's whisper say
  hello and their state to the same hub. Every body on the wire must be strict JSON, validate, and carry no key the
  schema does not name wherever the schema lists an object's keys (`x-complete`).
- A change in the code (a key gone, a key added, a type changed) is caught by those live tests: they check what the
  real code sends. `MUTATIONS` in `tests/test_contracts_station_state.py` is the check on the check: it edits a
  recorded post, not the code, the way such a change would, and each edit must fail.
- What the tests do not reach is under Known gaps.

**Designed** is `docs/nodes.md`, not implemented yet. The tests hold these schemas to the design's own tables and to
the constants it points at in the code, and check that every body valid today stays valid with the new keys absent
and that today's hub, station and worker client ignore the new keys.

## Layout

```
contracts/
  schemas/<name>.schema.json           JSON Schema 2020-12
  vectors/<name>/valid/*.json          bodies a writer may send
  vectors/<name>/invalid/*.json        bodies a writer must never send, each for exactly one rule
  vectors/<name>/*/*.patch.json        a vector as another one and the changes to it
  vectors/lease/order.json             pairs of lease ids and how they compare
  routes.json                          every /api/station/ route
tests/contracts.py                     the helper: schemas, vectors, strict JSON, lease ids, routes read from the code,
                                       a real hub with a station and a worker
tests/test_contracts_schemas.py        the harness: every schema and vector
tests/test_contracts_station_state.py  the state post, its answer, hello, errors; the pinned deviations
tests/test_contracts_workers.py        a worker's hello and state
tests/test_contracts_nodes.py          the designed schemas against docs/nodes.md
tests/test_contracts_routes.py         routes.json against the hub's dispatchers
```

An invalid vector is refused by the schema for exactly one rule, or is not JSON at all (`NaN`).

## Vectors: recorded bases and patches

A vector is a plain `.json` file (the body), or a `.patch.json` file: `{"base": "<a plain vector's path under
vectors/>", "patch": [...]}`, the base with an RFC 6902 JSON Patch applied (`add`, `remove`, `replace`). A body that
differs from another in a value or two is always a patch, never a copy. A patch that no longer fits its base (it
removes or replaces what is not there) fails.

The large bodies are **recorded** from the real code, never written by hand:

| base | recorded from |
|---|---|
| `station-state/valid/with-status.json` | the first post of the station with everything |
| `station-state/valid/with-status-plain.json` | the first post of the plain station |
| `station-state/valid/talking-only.json` | a post of the station with everything while two radios talk and a page goes out |
| `stt-worker-hello/valid/online-with-gpu.json`, `paused-no-gpu.json`, `stt-worker-hello-answer/valid/answer.json` | the GPU worker client's hello and the hub's answer |
| `stt-worker-state/valid/gpu-worker-online.json`, `gpu-worker-paused.json`, `a-stations-whisper-paused.json`, `stt-worker-state-answer/valid/answer.json` | the heartbeats of the GPU worker client and of a station's whisper |

One command records them all again, from the tests that drive the real code:

```sh
.venv/bin/python -m tests.contracts record
```

A recording is **scrubbed** before it is written (`tests/contracts.py` `scrub`), so that a vector says nothing about
the machine or the moment it was recorded on and a new recording differs only where the code does:

- every point in time is moved so that the post's `at` is 1794990000 (to the whole second); `health.started` is an
  hour and `health.boot` a day before it; the start time inside a talker's and a page's `key` follows;
- the machine's numbers in `status.health` are fixed: `host`, `cpu_pct`, `load1`, `cpus`, `mem_pct`, `mem_total`,
  `temp_c`, `disk_path`, `disk_pct`, `disk_free`, `disk_total`; so is `load.cpu_pct` of a station's whisper;
- what depends on the run is fixed: a `detail` that names the test's temporary folder, the Voice row (it depends on
  the OS), the metrics link's counters, the speech-to-text queue's `ours_ago_s`, the pool worker's first word, the
  worker's `model`;
- the two names that are the code's own are replaced: the county system's becomes `countynet`, its dongle job
  `County P25`.

Everything else in a recorded vector is what the test set up, with made-up names (`st-north`, `Paging 1`, `east`,
serials `00000101`...). The hand-written vectors use made-up values only.

### The status gained a key: what to do

1. **What fails.** The two live tests in `tests/test_contracts_station_state.py` (a plain station, a station with
   everything) say `the code sends keys the schema does not name` and give the key's path. The `test_the_recorded_*`
   tests give the path where a recorded base and the code now differ.
2. **Edit** the schema: add the key with its type, unit and description (to `required` if it is always sent), and
   raise `x-version` if the schema is on main.
3. **Run** `python -m tests.contracts record`. `git diff contracts/vectors` then shows the new key in the bases and
   nothing else. (A base recorded for the first time is a new file: the run says so; `git add` it.)
4. **Run the contract tests.** Every patched vector now sits on the new base. A patch that touched the changed part
   fails by its name: `test_an_invalid_vector_is_refused_for_exactly_one_rule` (it must still break one rule, not
   two) or the patch itself (it removes or replaces what is gone).

A key that went away, or changed its type, is the same four steps; a key that is no longer always sent leaves
`required` in step 2.

## Conventions in a schema

- `$id` is the file's raw address in this repository; a `$ref` to another schema is its file name. The tests resolve
  them from disk, never over the network.
- Every field has a `description`, with its unit. A point in time is Unix seconds (a fraction allowed), never
  milliseconds, and is bounded so that milliseconds do not validate; a name ending in `_s` is a length in seconds,
  `_ms` milliseconds, `_db` decibels, `_pct` percent, `_mhz` MHz. Sizes are bytes unless the name ends in `_mb`
  (MiB) or `_gb` (GiB).
- Objects take unknown keys (`additionalProperties: true`). None of the code under these schemas refuses one; a place
  that does would be modelled as it is and listed under Known deviations.
- A word (a state, a kind, a class) is an open string, never a closed `enum`. `x-known` lists the values the code
  sends today, and the tests compare it with the code's own list. A key or a word that a change in review adds is
  declared optional ahead of it, with "From #<number> on" in its description, so that the same schema holds before
  and after that change is on main.
- `x-status`: `as-built` or `designed`. `x-version`: goes up by one with every change to the schema once it is on
  main. Only additions of optional keys keep the name; see the rule above.
- `x-complete: true` on an object: the schema names every key the code sends there. The tests fail on a key it does
  not name. A map of free keys (per job, per channel, per reason) says so with `additionalProperties` and a schema.
- Transport: plain JSON (RFC 8259: no `NaN`, no `Infinity`), with a `Content-Length`; the hub does not decode chunked
  bodies. An integer that may pass 2^53 - 1 is sent as a string of decimal digits (`lease.schema.json` `counter`).

## Adding a schema

1. Write `schemas/<name>.schema.json` with `$id`, `title`, `description`, `x-status`, `x-version` and a description
   per field.
2. Add `vectors/<name>/valid/` and `invalid/`, one of the valid ones with a key from a newer build
   (`from_the_future`), each invalid one breaking exactly one rule. A large body is recorded (add it to the test
   that records, with `c.recorded`), the ones near it are patches. Made-up values only.
3. As built: add a test that drives the real writer and the real reader (`tests/contracts.py` `Live` is a hub with a
   station and a worker over HTTP), validates what was sent, and sends each reader a key it does not know in every
   object (`with_unknown_keys`).
4. Add its row to the table above and its name to the route's row in `routes.json`.
5. If the code deviates from the rule, do not change the code in the same PR: model what it does, add a row under
   Known deviations and a test that pins it behind a switch.

## Routes

`routes.json` lists every route under `/api/station/`: method, path, the kinds of token the code takes today, the
role that opens it once roles exist (`docs/nodes.md` 3), and the request and answer schemas. The test reads the hub's
three dispatchers (`Hub.handle`, `WorkerApi.handle`, the LLM lane's `handle`) and fails when a route in the code has
no row, a row is not in the code, or a real hub answers a row differently from what it says. It also fails on any
use of `path` in a dispatcher that it cannot read as a route (a slice, a split, a copy, a table keyed by a part of
it), so a route cannot be added in a form it would not see (fail closed).

## Known deviations

Where today's code departs from the rule or from the design. Not fixed here. Each is pinned by a test
(`test_known_deviation_*` in `tests/test_contracts_station_state.py`, behind a switch at the top of that file),
unless its row says it was found by reading. Each test states today's behaviour and the fixed one; when a fix is on
main, its switch goes from `True` to `False` (one line) and its row here is marked fixed. Line numbers are those of
`b50ea36c`.

| # | severity | where | what | who |
|---|---|---|---|---|
| D1 | low | `sizeup/cloud/client.py:130`, `sizeup/cloud/hub.py:619` | The station writes a float NaN as the bare token `NaN` and the hub reads it: not JSON. A reader that is not Python refuses the whole post; the hub's clock skew for the station becomes NaN. Fix in review: #779 | main session |
| D2 | medium | `sizeup/cloud/hub.py:1975-1979`, `1936`, `2285`, `1993`, `372`, `1349`, `2391` | No capability strings from station to hub: the hub infers what a station can do from the presence of status keys (`channels.squelch_ok`, `channels.deny_ok`, `p25_systems`, `stt_alert_rev`, `version`) and from the text `not allowed` in a job's error. Found by reading | main session, with native (the registry step) |
| D3 | medium | `sizeup/web/server.py:911` | `HEAD` is answered as `GET` without the body on `/api/station/` too. A `HEAD` on the job poll takes the waiting jobs, which are handed out only once. Fix in review: #779 | main session |
| D4 | low | `docs/nodes.md:245-254` against `sizeup/cloud/hub.py:574`, `576` | The design's route table has no row for `POST /api/station/stt` (each clip's speech-to-text input) and names only the long poll of the jobs, not `POST /api/station/jobs/<id>`. `routes.json` has `radio-receive` and `always`. Fix in review: #779. Found by reading | main session (the document's owner) |
| D5 | high | `sizeup/cloud/hub.py:642` (kept), `644`-`645` (raises: `sizeup/cloud/health.py:612`, `hub.py:392`, `1942`, `1976`, `342`); read again at `health.py:673`, `hub.py:411`, `267`, `277` | The hub does not handle a wrong type in a state post. `status.radios` that is no list of objects, a row's `label` that is no string, `status.channels` or `status.outbox` that is no object: the status is kept first, then the handler raises (the connection is dropped with no answer). Until the next good post `GET /api/station/status` and `GET /api/cad/health` (after `radios: 5`) or `GET /api/cad/state` (after `outbox: 5`) drop their connections too. A talker or a page with `ended` and an `until` that is no number is answered 200 and then drops `GET /api/cad/state` the same way. Fix in review: #782 (the normal 200 with `refused: ["status"]` or `["live"]` and a `reason`; the bad part is left out, the rest of the post is taken) | main session |
| D6 | high | `sizeup/cloud/station.py:222` (called at `582`), `274`-`280` | The station does not handle a wrong type in the state answer. `caps` that is a number or a boolean raises in `_set_caps` outside the thread's error handling: the hub-state thread ends, and with it the heartbeat, talking now, the status and the hub's build, until the station restarts. `caps` as a string becomes one capability per character. A `pool.ready` or `pool.max_s` that is no number makes `pool_has_room` raise in its caller. Fix in review: #782 (the field is ignored, its last good value kept) | main session |

As built and worth knowing:

- An answer without `caps` clears the station's whole set, on disk too (pinned by a test).
- The hub reads `paging` only when `talking` is in the same post (pinned by a test).
- The hub takes any number as `at`: milliseconds give a clock skew of some 50,000 years (pinned by a test; the schema
  refuses them).
- No reader of `status.metrics` was found on the hub (by reading).

## Known gaps

What these schemas and tests do not cover yet. Follow-up: #781.

- **Objects left untyped** in `station-state` (their keys are not listed, so nothing is checked inside):
  `status.radios[].keyups` and `.ab`; `status.sdr.adsb`; `status.health.stt_queue`, `.adsb`, `.reception`;
  the values in `status.version.update`; `status.channels.prefs`, `.aws`, `.squelch` (the hub's own words back);
  `status.p25_systems[].site`, `.unknown[]`, `.encrypted[]`, `.watched[]`; `status.reception.systems[]`,
  `.dongles[]`, `.active`, `.samples[]`, `.history[].halves[]`, `.history[].verdict`.
- **Typed from reading the code, never on the wire in the tests** (the fakes do not reach these states): a radio
  row's `word`, `schedule`, `multichannel`, `tower`, `capture`, `fallback_until`, `hold_until`; a dongle's `lent`,
  `held_by`, `noaudio` when not null and its `own`, `for`, `on`; rows of `duplicates`; a group's `parked`; the
  `health` and `logship` error forms (in vectors only); a job's `hold_until`; rows of `version.adapter.health`.
- **Routes with no schema**: events, jobs, the speech-to-text input, logs, metrics, aircraft, vessels, status, and
  of the workers' routes everything but hello and state. Wrong types on the workers' routes were not probed.

## Names accepted

`docs/nodes.md` gives `capabilities`, `capabilities_rev`, `capabilities_wanted`, `roles_rev`, `roles_applied` and the
lease's `{role, holder, gen, ttl_s}`. What it does not name, these schemas proposed. All of it was accepted by the
main session 2026-11-18:

| name | where | what |
|---|---|---|
| `roles` | `node-state-answer`, `node` | the key that carries a node's roles in full; an object `{role: options}` |
| `leases` | `node-state-answer` | the key that carries the leases a node holds; a list, one per exactly-one role |
| `gen` as `[leader generation, lease generation]` | `lease` | the pair as a two-item array; each a number up to 2^53 - 1 or a string of digits. Where `docs/nodes.md` writes one number (`active (gen 14)`, `idempotent per (node, gen)`) it means this pair |
| a rev as 8 to 64 lower-case hex characters | `node-state` | the hash and what exactly it covers are not chosen here |
| the node fields at the top level of the post | `node-state` | not inside `status`, which is on some posts only |
| `capabilities_wanted` as a boolean | `node-state-answer` | |
| `channels[].{job, coverage}`, `priority`, `pool`, `classes`, `llm_jobs`, `models`, `dongle` | `roles` | the per-role options (`fast` and `max_s` are the design's) |
| `channels[].{job, state}`, `net.{hub_rtt_ms, up_kbps}` | `capabilities` | |
| `capabilities` and `capabilities_rev` in the worker's hello | `node-worker-hello` | |
| the roles of `POST /api/station/stt` and `POST /api/station/jobs/{id}` | `routes.json` | see D4 |

Still open: the `caps` strings that switch the node fields and the lease on; a list of capability strings from node
to hub (D2); where "load on every post" goes (today it is `status.health`).

## Running

```sh
caffeinate -i .venv/bin/python -m pytest -q -n auto --dist loadfile tests/test_contracts_schemas.py \
    tests/test_contracts_nodes.py tests/test_contracts_station_state.py tests/test_contracts_workers.py \
    tests/test_contracts_routes.py
```

They need `jsonschema` from the dev extras, like `/api/v1`'s contract tests.

## Capture split (designed)

The contract a future `sizeup-radio` service (radio capture as its own process, ADR 0002; in Python first, possibly a
native daemon later) and the station app will speak. The design is `docs/native/capture-split.md` (#773), approved as
the boundary by the radio maintainers; **nothing of it is implemented**, so everything here is `designed`: schemas,
hand-written vectors with made-up values, and tests of the vectors and of the rules. No code under `sizeup/` changes.

| schema | the design's file (10) | from | what |
|---|---|---|---|
| `capture-spool-sidecar` | `spool-sidecar.schema.json` | 4.1, 4.6 | the JSON that completes one spool item: a segment, a P25 call, a minute of metrics |
| `capture-spool-cursor` | `spool-cursor.schema.json` | 4.1 | the reader's `cursor.app.json` |
| `capture-urgent-item` | `urgent-item.schema.json` | 4.3 | an alarm or a live page as sent to the hub; `$defs/held`, as capture holds it; `$defs/data`, the row's `data` |
| `capture-urgent-request` | `urgent-request.schema.json` | 4.3, 7.5 | the body of `POST /api/station/urgent` |
| `capture-urgent-response` | `urgent-response.schema.json` | 4.3 | its answers: `applied`, `duplicate`, `stale`, `rejected` per item; the errors |
| `capture-socket-event` | `socket-event.schema.json` | 4.2 | a frame from capture: `hello`, `snapshot`, `carrier`, `ids`, `urgent`, `talking`, `paging`, `ours`, `grant`, `spool`, `status`, `level` |
| `capture-socket-request` | `socket-request.schema.json` | 4.2, 4.3 | a frame to capture: the notices, and a request with its `op` and `args` |
| `capture-socket-reply` | `socket-reply.schema.json` | 4.2 | the answer to a request; `$defs/urgent-since-result` |
| `capture-wish` | `wish.schema.json` | 4.4 | `radio_wish.json`: desired state only |
| `capture-status` | `status.schema.json` | 4.5 | `status.json`: observed state only |

What a schema cannot say is in `contracts/capture/`, and in reference aids the tests run
(`tests/contracts_capture.py`: each a specification aid, clearly labelled, not production code):

| file | from | what | reference aid |
|---|---|---|---|
| `spool.md` | 4.1 | the spool directory: names, tmp + rename, audio first and the sidecar last, `EPOCH`, `SEQ` and its recovery, order per channel, who deletes, the bounds, `late` | `check_spool` (every rule a directory breaks), `Spool` (a writer that keeps them, with the points a crash can stop it at), `recover_seq`, `is_late` |
| `socket.md`, `socket-frames.json` | 4.2, 4.3, 7.5 | the framing (#709's `>II`) with byte vectors, the first exchange, the request rules, the `credentials` request and its `SO_PEERCRED` rule | `frame`, `unframe`; `Requests` (an id applied once, the deadline on the boot clock, who may ask) |
| `urgent.md`, `urgent.sql`, `urgent-scenarios.json` | 4.3, 11 | the items and their keys, which clock an age is on, the sender's rules, the urgent file's DDL (the design's, byte for byte), and the hub's idempotency rules as a table of scenarios | `Hub` (the hub's dedupe: the store both lanes write, the alarms, the live pages), `Lane` (capture's sender), `P25Bits` (#775's transition rule), `age` |
| `modes.json` | 4.3, 6 | `/etc/sizeup/capture-mode` (`inline`, `shadow`, `service`) and `/etc/sizeup/urgent-mode` (`off`, `write`, `send`): all nine combinations, each with who decodes, who writes and who sends | `combination`, `read_mode` |

`POST /api/station/urgent` is in `routes.json` under `designed`: decided (an `sz1` credential with the
`radio-receive` role) and not served. `tests/test_contracts_capture.py` holds the row and fails the day the hub
serves the path, as `tests/test_contracts_routes.py` does: the row then moves to `routes`.

Rules for every file and message of this contract (the design's 4, ADR 0004): a `v`; unknown keys are ignored by
every reader; a word a reader does not know (a kind, a frame, an op, a state) is taken as unknown, never as an
error; no integer above 2^53 - 1; times are float seconds, Unix seconds on the wall clock and seconds since boot in
a name ending in `_boot`; a file is written whole under a temporary name and renamed (the wish by the app, the
status and the spool by capture, the cursor by the reader).

Still open in the design itself, and how it shows here: **A7** (what a late emergency becomes on the hub) is
not decided by the admin. The recommended 30 min is the parameter `max_age_s` of the reference model and of
`urgent-scenarios.json`, never a constant; the scenarios that depend on it say so. Live P25 emergencies wait for
#775: their item, key and rules are a slot in the contract and not a working path.

### Choices, for the radio maintainers to confirm

Where the design is ambiguous or silent, and what these files chose. Each is a proposal until confirmed.

| # | where | the design | chosen here |
|---|---|---|---|
| C1 | 10 | puts every artifact in `contracts/capture/` | the schemas are `contracts/schemas/capture-*.schema.json` and the vectors `contracts/vectors/capture-*/`, where #774's harness finds them; `contracts/capture/` holds the rest |
| C2 | 10 | "`oneOf` by `kind`" (by `t`, by `op`); closed lists of words | each word's fields under `if` word `then` fields, and every word an open string with `x-known`: an unknown kind, frame or op validates, as 4.1, 4.2 and 4.7 want it skipped or answered. A closed `oneOf` would refuse it |
| C3 | 4, 4.2, 4.5 | a `v` in every file and message; a `contract` in `hello`, the status and `capture.contract` | `v` is the version of that one shape (1); `contract` is the major version of the whole contract (1). The design does not say they are one number |
| C4 | 4.3 Clocks | which clock a field is on | in its bounds too: a wall-clock time is under 10^11 (never milliseconds, as #774); a `_boot` field is under 10^9, so a Unix time does not validate where seconds since boot belong |
| C5 | 10 | a call requires "the common ones" | the common ones are `v`, `kind`, `id`, `seq`, `spool_epoch`, `published`; a call also requires `start`, `end`, `closed_boot`, `boot`, `audio`. Inside objects the table names only as a whole: an id entry requires `id`, `system`, `emergency`, `from`; `keyup` requires `split`, `parent`, `prev`; a turn requires `src`, `pos_s`, `emergency`, `via`; `grants_applied` is a boolean |
| C6 | 4.1 | `closed_boot` is "at squelch close" | for a call, which has no squelch: when capture's ingest took the finished call. The wall age `now - end` covers the wait before that, since `late` takes the larger |
| C7 | 4.1 | a metrics item has an `id`, not said which; `seq` has no stated range | `metrics-<int(minute start * 1000)>`; `seq` starts at 1 and has at most 12 digits (the file name's width) |
| C8 | 4.1 | `SEQ` is recovered as `max(SEQ, highest in items/) + 1` | every file in `items/` counts, debris too, so a number is never given out twice; the publish lock is held from taking `seq` to the sidecar's rename (X1) |
| C9 | 4.6 | `tr`: "file name of trunk-recorder's own JSON, a hard link" | linked as `<seq>-<id>.tr`, not `.json` (X2) |
| C10 | 4.1 | "per channel, `seq` rises in the order transmissions ended" | for calls "per channel" is per system and talkgroup; the order is by `end` |
| C11 | 4.1 | audio is dropped at a bound and items are deleted behind the cursor; no order given | the sidecar goes first both times (rewritten, or deleted), then the audio: no sidecar ever names audio that is gone. `audio_dropped`, named only in the Bounds row, is a field of the schema |
| C12 | 4.1 | "under 60 s is live, older is late" | exactly 60 s is late |
| C13 | 4.3 | the row's `at` is "wall clock at insert" | `at` is set once, when capture makes the item, and is the same in the row, every frame and every request (X3) |
| C14 | 4.3 | `gen` (int), the hub's fencing generation | `lease.schema.json`'s `counter`: an integer up to 2^53 - 1 or a string of decimal digits, compared by value, for change only. The same for `urgent.hub_gen` in the status |
| C15 | 4.3 | says nothing of a `status` or an `age_src` word a build does not know | the sender keeps and retries an item answered with an unknown word (never drops an alarm on it); the hub takes an unknown `age_src` as `wall` |
| C16 | 7.5 | "every string field length-capped before use", no numbers | only `key` (200 characters), the station id (a node id) and a request's `id` (64) are capped here. The other caps are the hub's to choose |
| C17 | 4.3 | 400, 413, 422 with `bad_json`, `bad_batch`, `too_large`, not paired; nothing on a request that is half good | 400 `bad_json`, 413 `too_large`, 422 `bad_batch`; a request that is not well formed is refused whole and nothing of it is applied |
| C18 | 4.3 | an alarm past the age limit is `stale`; nothing on its key | the key is recorded all the same: a retry is `duplicate`, and the app's take-over raises nothing |
| C19 | 4.3 | "the same radio pressing again: the alarm code's own same-radio rule" | the item is `applied` (it was taken) and joins the open alarm: no second alarm, no second push. `duplicate` stays the word for a key already in the store |
| C20 | 4.3, 11 | the P25 match for "live, then end of call"; "the newer of the two wins" for a page; an alarm "labelled with its age" | the P25 match holds in either order of arrival; newer is the later to arrive at the hub; every push carries `age_s` (from what age it is shown as late is the hub's) |
| C21 | 7.1 A7 | open: awaiting the admin | `max_age_s` is a parameter (1800 recommended), never a constant |
| C22 | 10 | "the header and payload limits", no numbers | a header of at most 65536 bytes (#709's); a version 1 writer sends no payload, a version 1 reader skips one of up to 1 MiB and closes the connection on a longer one |
| C23 | 4.2 | request ids "unique", remembered "for 10 min" | 1 to 64 characters; the 10 min are on the boot clock; a repeat is answered from the memory even after its deadline; the person's yes goes under a **new** id (X8); `credentials` is never kept in the memory |
| C24 | 4.2, 10 | notices require `v`, `t`; `urgent_taken` carries `n` | each notice's own fields are required too (`role` and `contract`; `what`; `n`; `ch`, `page_id`, `end`); `urgent_taken` may carry `urgent_epoch`, and capture ignores a notice for another epoch (X6); `at` is optional on a client's frame |
| C25 | 4.2 | frame fields without types | `holding` is `NoiseSquelch.holding`; `queued` a count; a talker has `channel` and `tg` both, as today's state post, and `dispatcher` (X11); `ours.pair` is a pair as in a page; `grant.src` a string, `grant.what` open; `spool.ch` absent for an item with no channel; a `status` frame's `at` is the file's; `level` is asked for with `subscribe: ["level"]` |
| C26 | 4.2 | `error` is "text for a person" | one string: text, or one of the words a client acts on (`stale`, `expired`, `unsupported`, `forbidden`), named in its description (X7) |
| C27 | 4.2 | `args` per op, nothing on which are needed | `squelch_apply`: `label`, `open_db`, `close_db`, `min_open_s`; `credentials`: all three, "" for none; `urgent_since`: `urgent_epoch`, `n`; `snapshot`: `{}`; `reception_start` is not typed |
| C28 | 4.3 | an alarm's `data` for P25: `system`, `talkgroup` added | `source` is the name the finished call's row carries, so the same-radio rule sees one channel; `radio_id` is several ids joined with `, ` as today; `end` is optional |
| C29 | 4.3, 10 | "an `sz1` credential with the `radio-receive` role" | `credential: ["sz1"]` in `routes.json`: the hub's shared station token, which every other station route takes today, is not listed. The row is under a new `designed` list: `reserved` is for prefixes no station may ever open |
| C30 | 4.4 | the wish's fields, some without a shape | `paths` entries are optional with the station defaults; the airband owner rule and the `p25` block but `talkgroups` are not typed (their keys are not named); `talkgroups` is `{system: {talkgroup digits: {alert, kept, encrypted}}}`; `spool.min_free_bytes` is there (X9), and the defaults "2 GB" and "256 MB" are read as 2^31 and 2^28 bytes; `hub` may not carry a key named `url`, `hub_url`, `token`, `secret`, `access_id` or `access_secret` |
| C31 | 4.5 | the status's fields, some without a shape | `wish_seq` 0 while no wish was accepted; `p25` is a map by system short name; `urgent.mode` is `write` or `send`, never `off`; a dongle's `job` is today's `mode`; a status of another boot is a capture that is down; inside `spool` and `urgent` the counters a Health row reads are required, and a channel row's `label`, `kind`, `run`, `state` |
| C32 | 4.3, 6 | two one-word files | surrounding whitespace is dropped; a missing or empty file and a word a build does not know are the default (`inline`, `off`). In `shadow` the app writes as in the `inline` row of the same `urgent-mode` and the shadow instance writes its own file beside it |
| C33 | - | - | two one-line changes to #774's tests, without which no schema designed outside `docs/nodes.md` can exist: `tests/test_contracts_schemas.py` takes a description that starts `Designed (docs/`, and `tests/test_contracts_nodes.py` counts the schemas designed in `docs/nodes.md` by that start |

### Found in the design while writing the schemas

Section, text, and what does not fit. Not changed here: the schemas follow the reading given.

| # | section | the text | the problem | read here as |
|---|---|---|---|---|
| X1 | 4.1 | "`seq` ... taken under a lock at the sidecar's rename" | the audio file's name carries `seq` and is renamed first, so `seq` exists before the sidecar's rename | taken before the audio's rename, the lock held until the sidecar's (C8) |
| X2 | 4.6, 4.1 | `tr`: trunk-recorder's own JSON, a hard link; "a reader lists `*.json` only" | a linked JSON in `items/` would be listed as a sidecar | `.tr` (C9) |
| X3 | 4.3, 4.2, 10 | the row's `at`: "wall clock at insert"; "every header has `v`, `t`, `at`"; the `urgent` frame lists `at` among the item's fields; the item's `at` is required and never null | an item sent from memory has no insert, and the frame's own time and the row's column are one key | the item's `at`, set when it is made (C13) |
| X4 | 4.3 Clocks, 6 step 8 | "a page item's 120 s: the larger of the boot-clock age and the wall-clock age"; the test list: "the wall clock stepped an hour either way: no page item expires" | stepped forward, the wall age is an hour and the larger-of-two rule expires the item | the rule of 4.3: a step forward expires a page item, never an alarm |
| X5 | 4.3 | a page item sets the hub's live paging state "with `until` as its expiry" | `until` is the station's wall clock, which the same section says the hub never trusts for an age | kept as the design has it; the hub needs a rule (its now plus `until` minus `sent_at`, say) |
| X6 | 4.2, 4.3 | `urgent_taken` (`n`); the cursor is "`(urgent_epoch, n)`" | after the file is reset, an `n` of the old file marks rows of the new one as taken | an optional `urgent_epoch` (C24) |
| X7 | 4.2 | "`error` is text for a person" | the client must tell `stale`, `expired`, `unsupported` and `forbidden` apart, and with `confirm` the question is in `error` | C26. A `reason` word beside the text, as the station routes have, would be cleaner |
| X8 | 4.2 | "the client sends the request again with `args.confirm: true`"; "a repeat gets the first reply again" | sent again under its id, the confirmed request gets the refusal again | a new id (C23) |
| X9 | 4.1, 4.4 | `spool.min_free_bytes` (default 256 MB); the wish's `spool`: "`max_age_h`, `max_bytes`, `max_items`, `sync_s`" | the reserve is a setting with no place in the wish | added to the wish (C30) |
| X10 | 4.6 | a turn's `via`: `srclist` \| `grant` \| `freqlist` | `Turn.via` in `sizeup/radio/p25.py` is `srclist` \| `grant` \| `split`; nothing produces `freqlist` | all four are known words |
| X11 | 4.2 | `talking` entries: "`LiveTalk.state()` without `rig`, `label`, `main`" and a field list without `dispatcher` | `LiveTalk.state()` works `dispatcher` out from the alias alone (`sizeup/radio/live.py`) | optional in the entry (C25) |
| X12 | 4 | "a field's unit is in its name" | `levels.q_open` and `q_close` are dB, `breaks` seconds, a turn's `time` Unix seconds | the design's names kept; the unit is in each description |
| X13 | 10, 4.3 | the example request carries an alarm and a page | the sender has one connection for alarms "and nothing else" and one for pages: it never builds that request | the vector is kept as written; a hub takes both in one |
| X14 | 4.4 | `listed`, `auto`, `auto_assign`, `order`, `ignore`, `start_mode`: "the `[radio]` settings of the same names" | the `[radio]` keys are `sdrs`, `sdr_ignore`, `auto_assign`, `auto_assign_order`, `start_mode` (`sizeup/config.py`) | each description names its setting |
| X15 | 4.3 Clocks, 7.5 | a wall-sourced age "is corrected by the hub's skew measure first" | the age is `now - event_at`, two readings of one clock: a constant skew cancels. What is corrected is not said | not modelled |
| X16 | 4.5 | `urgent.state`: one word of seven | `reset`, `no_credentials` and `off` can hold at once; which wins, and how long `reset` stays, is not said | not decided here |
| X17 | 10, 4.3 | the route is "idempotent by (`key`, `rev`)"; the store is "keyed by `(station, key)`" | two stations can send one key | `routes.json` says `station`, `key`, `rev` |
| X18 | 4.1 | "`id` ... matches `CLIP_ID`" | only spaces are taken out of a label: one with another character outside `[A-Za-z0-9._-]` gives an id that does not match. And today a transmission on a channel that is not recorded has no id (`sizeup/app.py`), while its sidecar needs one | the schema holds `id` to `CLIP_ID`; what capture does with such a label is open |

### Running

```sh
caffeinate -i .venv/bin/python -m pytest -q -n auto --dist loadfile tests/test_contracts_capture.py \
    tests/test_contracts_schemas.py tests/test_contracts_nodes.py tests/test_contracts_routes.py
```
