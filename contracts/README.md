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
| `capture-hello-answer` | designed | 1 | `station-hello-answer` plus `gen`, the hub's generation |
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
| D1 | low | `sizeup/cloud/client.py:130`, `sizeup/cloud/hub.py:619` | The station writes a float NaN as the bare token `NaN` and the hub reads it: not JSON. A reader that is not Python refuses the whole post; the hub's clock skew for the station becomes NaN. Fixed by #779 (merged 2026-11-18) | main session |
| D2 | medium | `sizeup/cloud/hub.py:1975-1979`, `1936`, `2285`, `1993`, `372`, `1349`, `2391` | No capability strings from station to hub: the hub infers what a station can do from the presence of status keys (`channels.squelch_ok`, `channels.deny_ok`, `p25_systems`, `stt_alert_rev`, `version`) and from the text `not allowed` in a job's error. Found by reading | main session, with native (the registry step) |
| D3 | medium | `sizeup/web/server.py:911` | `HEAD` is answered as `GET` without the body on `/api/station/` too. A `HEAD` on the job poll takes the waiting jobs, which are handed out only once. Fixed by #779 (merged 2026-11-18) | main session |
| D4 | low | `docs/nodes.md:245-254` against `sizeup/cloud/hub.py:574`, `576` | The design's route table has no row for `POST /api/station/stt` (each clip's speech-to-text input) and names only the long poll of the jobs, not `POST /api/station/jobs/<id>`. `routes.json` has `radio-receive` and `always`. Fixed by #779 (merged 2026-11-18). Found by reading | main session (the document's owner) |
| D5 | high | `sizeup/cloud/hub.py:642` (kept), `644`-`645` (raises: `sizeup/cloud/health.py:612`, `hub.py:392`, `1942`, `1976`, `342`); read again at `health.py:673`, `hub.py:411`, `267`, `277` | The hub does not handle a wrong type in a state post. `status.radios` that is no list of objects, a row's `label` that is no string, `status.channels` or `status.outbox` that is no object: the status is kept first, then the handler raises (the connection is dropped with no answer). Until the next good post `GET /api/station/status` and `GET /api/cad/health` (after `radios: 5`) or `GET /api/cad/state` (after `outbox: 5`) drop their connections too. A talker or a page with `ended` and an `until` that is no number is answered 200 and then drops `GET /api/cad/state` the same way. Fixed by #782 (merged 2026-11-18) (the normal 200 with `refused: ["status"]` or `["live"]` and a `reason`; the bad part is left out, the rest of the post is taken) | main session |
| D6 | high | `sizeup/cloud/station.py:222` (called at `582`), `274`-`280` | The station does not handle a wrong type in the state answer. `caps` that is a number or a boolean raises in `_set_caps` outside the thread's error handling: the hub-state thread ends, and with it the heartbeat, talking now, the status and the hub's build, until the station restarts. `caps` as a string becomes one capability per character. A `pool.ready` or `pool.max_s` that is no number makes `pool_has_room` raise in its caller. Fixed by #782 (merged 2026-11-18) (the field is ignored, its last good value kept) | main session |

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
These files follow the design as of `5f143a80`, which answered the first version's questions and the second and third reviews'.

| schema | the design's name (10) | from | what |
|---|---|---|---|
| `capture-spool-sidecar` | `spool-sidecar` | 4.1, 4.6 | the JSON that completes one spool item: a segment, a P25 call, a minute of metrics |
| `capture-spool-cursor` | `spool-cursor` | 4.1 | the reader's `cursor.app.json` |
| `capture-urgent-item` | `urgent-item` | 4.3 | an alarm or a live page as sent to the hub; `$defs/held`, as capture holds it; `$defs/data`, the row's `data` |
| `capture-urgent-request` | `urgent-request` | 4.3, 7.5 | the body of `POST /api/station/urgent` |
| `capture-urgent-response` | `urgent-response` | 4.3 | its answers: `applied`, `duplicate`, `stale`, `rejected` per item; the errors |
| `capture-hello-answer` | (11 H1a) | 4.3, 11 | today's answer to `GET /api/station/hello` plus `gen`: how an idle sender learns the hub lost what it answered for |
| `capture-socket-event` | `socket-event` | 4.2 | a frame from capture: `hello`, `snapshot`, `carrier`, `ids`, `urgent`, `talking`, `paging`, `ours`, `grant`, `spool`, `status`, `level` |
| `capture-socket-request` | `socket-request` | 4.2, 4.3 | a frame to capture: the notices, and a request with its `op` and `args` |
| `capture-socket-reply` | `socket-reply` | 4.2 | the answer to a request: `reason` (a word) and `error` (text); `$defs/urgent-since-result` |
| `capture-wish` | `wish` | 4.4 | `radio_wish.json`: desired state only |
| `capture-status` | `status` | 4.5 | `status.json`: observed state only |

What a schema cannot say is in `contracts/capture/`, and in reference aids the tests run
(`tests/contracts_capture.py`: each a specification aid, clearly labelled, not production code):

| file | from | what | reference aid |
|---|---|---|---|
| `spool.md` | 4.1 | the spool directory: names, tmp + rename, audio first and the sidecar last, `EPOCH`, `SEQ` and its recovery, order per channel, who deletes and in what order, the bounds, `late` | `check_spool` (every rule a directory breaks), `Spool` (a writer that keeps them, with the points a crash can stop it at), `recover_seq`, `is_emergency`, `segment_id`, `is_late` |
| `socket.md`, `socket-frames.json` | 4.2, 4.3, 7.5 | the framing (#709's `>II`) with byte vectors, the first exchange, the request rules, the `credentials` request and its `SO_PEERCRED` rule | `frame`, `unframe`; `Requests` (an id applied once, the deadline on the boot clock, who may ask) |
| `urgent.md`, `urgent.sql`, `urgent-scenarios.json` | 4.3, 11 | the items and their keys, which clock an age is on, the sender's rules, the urgent file's DDL (the design's, byte for byte), and the hub's idempotency rules as a table of scenarios | `Hub` (the dedupe store with its three states and `stale_src`, the alarms, the press window, the live pages, a restore of the whole state), `Lane` (capture's sender: what counts as an answer, the resend rule and its cap, the tries, the byte limit and `trimmed`, the memory bound), `P25Bits`, `WallSteps`, `match_call` and `p25_key` (#775, capture's side), `age` |
| `modes.json` | 4.3, 6 | `/etc/sizeup/capture-mode` (`inline`, `shadow`, `service`) and `/etc/sizeup/urgent-mode` (`off`, `write`, `send`): all nine combinations, each with who decodes, who writes and who sends | `combination`, `read_mode` |

`POST /api/station/urgent` is in `routes.json` under `designed`: decided and not served. The day the hub serves the
path, `tests/test_contracts_capture.py` and `tests/test_contracts_routes.py` both fail until the row is moved to
`routes`; until then a client string for it under `sizeup/` is accepted because the row is there.

Rules for every file and message of this contract (the design's 4, ADR 0004): a `v`; unknown keys are ignored by
every reader; a word a reader does not know (a kind, a frame, an op, a state, a reason) is taken as unknown, never
as an error; no integer above 2^53 - 1; times are float seconds, Unix seconds on the wall clock and seconds since
boot in a name ending in `_boot`; a file is written whole under a temporary name and renamed (the wish by the app,
the status and the spool by capture, the cursor by the reader); every limit is exclusive on the inside (exactly the
limit is outside: 60 s is late, 120 s is expired, an alarm exactly `max_age_s` x 1.1 old is stale) and a slack written
"+-" is inclusive.

Still open in the design itself, and how it shows here: **A7** (what a late emergency becomes on the hub) is
not decided by the admin. The recommended 30 min, counted with the design's 10 % margin (1980 s), is the parameter
`max_age_s` of the reference model and of `urgent-scenarios.json`, never a constant; the scenarios that depend on it
say so. Live P25 emergencies wait for #775: their item, key and rules are a slot in the contract and not a working
path.

### Still to confirm, the top decisions first

The first version of these files listed 33 choices (C1 to C33) for the radio maintainers to confirm and 18 places
where the design did not fit itself (X1 to X18). The design answered them (`79b788a8`, `6a777c98`). A second
adversarial review of the models found sequences that lost an emergency or pushed twice; the fixes went into the
design first (`d05339e3`), and the radio maintainers confirmed all of them (`225b06ff`, with one wording: the alarm
after an All clear reads "emergency still set after All clear", and only within 10 min of the radio's last flag
before the clear, `3fcec010`). A third review found three more (the socket's sizes, a call from before an All
clear, what the app's lane delivered); they went into the design (`65bcd699`), were confirmed by the radio
maintainers, and the alarm's own small post on the events lane was settled by the main session (`5f143a80`). These
files follow it everywhere. The six with the most weight are the design's decisions now; what is left to confirm is
that these files hold them as meant:

| was | decision | where it is held |
|---|---|---|
| C29 | The route is opened by a station's own `sz1` token only. The hub's older shared station token does not open it: a station still on it runs in `urgent-mode write` until it has its own token | `routes.json` `designed`; `capture-urgent-request` |
| C18 | An alarm key in the hub's dedupe store has one of three states. `raised` and `stale` answer an urgent item `duplicate`; `seen` (the events lane met the key and raised nothing) does not: the item is judged on its own age. A `stale` entry keeps `stale_src`, and only a boot-sourced one stops a later event. A restore puts back the whole state as of its point. Every cutoff on an emergency is `max_age_s` x 1.1 | `Hub`; scenarios "stepped back 40 min", "carries the alarm_key and no emergency", "older than the age limit", "1 h fast", "before its upgrade to raised" |
| C20 | A live P25 alarm and its call are matched on the station, by capture's ingest (system + talkgroup + unit, the grant inside the call's span +-10 s; the boot clock only after a wall step of over 5 s), and carried as the call's `alarm_key`: the hub compares no clocks. The key is made from the grant's own time | `match_call`, `WallSteps`, `p25_key`; scenarios "60 s ahead" |
| C19 | The same radio pressing again, from this station or another, joins its open alarm (not cleared, acknowledged or not) raised under `REPEAT_S` before: one alarm, one push. P25 flags go through the press window on the hub's receive time: armed only by a flag that raised or joined an alarm, joining only an open one under 600 s, ended by an All clear. A call that started before the clear (placed past `U` by a boot age or a fresh skew) attaches to the cleared alarm with no push; when unsure it alarms | `Hub`; scenarios "a disregard call from before the All clear", "inside U", "pressing again", "two stations hear one press", "acknowledged at 4 s", "300 s later", "ending 35, 28, 21, 14, 7, 1 min ago", "notices are off", "15 min behind" |
| C15 | Only an answer in the hub's shape counts. A word the sender does not know gets 5 tries, each after the healthy items and alone, then `rejected`; an item missing from a 200, `200 {}`, an edge's 422 cost no try. The hub's own 400/413/422 splits the request, and is final only for an item already alone; a lone `too_large` trims first. One byte count (UTF-8 of the JSON as sent); every item at most 48 KB, trimmed past that, in frames and requests alike; `urgent_since` filled while its frame fits. Fresh alarms first, then resends, then the backlog. `urgent_delivered` makes a row `app`: never sent by capture. A rejected alarm still rides the app's lane, with a critical Health item | `Lane`; `test_urgent_since_is_filled_...`, `test_urgent_delivered_...`, `test_in_every_request_fresh_...`, `test_an_answer_capture_cannot_read_...`, `test_what_is_not_the_hubs_answer_...`, `test_a_hub_refusal_...`, `test_an_alarm_over_the_byte_limit_...` |
| C11 + C8 | The sidecar always goes first when an item is deleted or its audio dropped, and capture's start removes what such a removal left half done. `seq` is taken before the audio's rename; the next number at every start is the largest of `SEQ`, the file names (`items/`, `quarantine/`) and the cursor, plus 1. An emergency item (four signs) never loses its audio at a bound | `spool.md`; `check_spool`, `Spool`, `recover_seq`, `is_emergency` |

Then what these files chose where the design, as of `5f143a80`, says nothing. Each is a proposal for the radio
maintainers (S9, S10: for whoever writes the sender and the hub's route) until confirmed.

| # | where | chosen here |
|---|---|---|
| S1 | every integer | a maximum on each: 2^53 - 1, but a talkgroup 65535, a `pid` 4194304, a `rev` 2^31 - 1, `contract` 65535, an audio's `channels` 8 and `rate_hz` 384000 |
| S2 | the sidecar, 4.1 "an emergency item" | `audio_lost` is an open word (`write`); `audio_dropped` without it is refused for any of the four signs |
| S3 | `GET /api/station/hello` with `gen` (11 H1a) | a schema of its own, `capture-hello-answer`: #774's as-built `station-hello-answer` plus the optional `gen`, as the node schemas extend theirs. The as-built schema is not touched |
| S4 | the wish, 4.4 "ignores a key named `url`, `hub_url`, `token`, `secret`, `access_id` or `access_secret`" | a writer may not send them, and the schema is wider than the six names: no key at any depth with `token`, `secret` or `password` in it, in any case; `url`, `hub_url`, `access_id` under `hub` |
| S5 | required keys inside objects the design names only as a whole | a wish's channel entry: `label`, `kind`, `mhz`; a tone-plan entry: `a_hz`, `b_hz`, `names`; a group: `name`, `sdrs`, `jobs`; a dongle's job: `job`; a pair: `a_hz`, `b_hz`, `names`, `ours`; the status's `unsent` entries and `refusals` entries: all their keys; `reader`: `attached` |
| S6 | a reply's `at` | required, by 4.2's "every header has `v`, `t`, `at`"; 10's row lists `v`, `t`, `id`, `ok` |
| S7 | the route's 503 | its body is today's, `{error, updating}` (10 lists the 4xx bodies only) |
| S8 | `shadow`, 4.3's table | the app writes as in the `inline` row of the same `urgent-mode`, and the shadow instance writes its own file beside it |
| S9 | the sender, where the reference had to pick a mechanism | the resend is made durable by putting the row back to `new`; "in doubt" across boots is a wall time that lies in the future; the 256 bound frees page items first, then the copies of alarms that have a row; the resend cap sorts by `sent_boot` when every candidate is of one boot and by `sent_at` otherwise; items are added to a request in order and the first that does not fit waits for the next request; `SEQ` one behind a file name is no fault while a publish is under way; "never yet put in a request" is this process's memory (after a restart every unsent alarm under 120 s is fresh again); `m` counts every item the process puts in its memory, those loaded at start too; a lost-alarm warning's 24 h run from the row's `sent_at` |
| S10 | the hub, likewise | the press window's flag of an alarm that was cleared keeps the window ended; the label "emergency still set after All clear" is on a live P25 item's alarm too; "at least `U` before" is inclusive; a call's age for the gate, from `start_boot`, is the start's age less the call's length; the events answer carries `gen` only on a 200 |
| S11 | `tests/test_contracts_routes.py` | #774's wider net accepts a path that has a row under `designed`, so capture's sender (step 8b) can merge with or after the hub's route; the test that a served path must be promoted stays. (A patch that no longer fits its base is #774's `Unfit`: this branch adds nothing to `tests/contracts.py`) |
| S12 | an urgent item's `data` | the keys of every kind are typed in one object (`$defs/data`), and only which are required follows `kind`: a kind added later must not reuse one of these names with another type. `key_time` and `trimmed` are typed there; the three P25 keys are required when `emergency_via` is `P25` |
| S13 | the status, the socket | `delivered_app` and `lost` are optional keys of the `urgent` block (the design lists them; older readers need not have them); a memory item's `m` is optional in `$defs/held`; `urgent_delivered`'s `gen` is a `counter`; an empty mended call stem is `call_` and 8 hex digits |
| S14 | the forwarder (11 H1m) | an "alarm event" that travels alone is an event of type `alarm`, or an event with a `system` (a P25 call) and an emergency flag or an `alarm_key`; an analog finished transmission with an `alarm_key` goes in the normal batch with its clip |

### Still ambiguous in the design

Section, text, and what does not fit, as of `5f143a80`. The schemas and models follow the reading given. (Answered
since the first version: the P25 key's time and what a wall step is, by the radio maintainers; at `65bcd699` an id
for any label, what tells capture that the app's lane delivered (`urgent_delivered`), and how long the "after All
clear" label lasts, `3fcec010`.)

| # | section | the text | the problem | read here as |
|---|---|---|---|---|
| Y1 | 4, 4.5, 4.2 | "every limit ... is exclusive on the inside"; "more than 15 s past `at_boot`"; "a request that reaches capture after it"; "remembers ... for 10 min" | the three sentences read as inclusive of the limit | the general rule: a status exactly 15 s old is down, a request exactly at `expires_boot` is expired, an id exactly 10 min old is forgotten |
| Y2 | 4.1 | "Capture crashes": debris is removed, "then `SEQ` is recovered"; "Sequence": recovered from the file names, "debris included" | once the debris is removed its number is no longer on disk | the highest number is read before anything is removed |
| Y3 | 4.2 | the frame table's `reply` row: `id`, `ok`, `error`, `confirm`, `result` | no `reason`, which the text below it and 10 require | with `reason` |
| Y4 | 4.3, 11 H1j | the two-station join rule "holds from the start"; "the hub takes one station until multi-station lands" | while it takes one station the second one's request is a 409: there is nothing to join | the rule is live once a second station is taken |
| Y5 | 4.3 | `seen` because "notices are off for it"; the item is then "judged on its own": `applied` | what `applied` raises on a channel whose notices are off is the alarm code's levels | not modelled |
| Y6 | 6 step 8b, 11 | "a client string `/api/station/urgent` under `sizeup/` fails the fail-closed route test until `contracts/routes.json` lists the route as served" | S11 accepts a `designed` row, so that the sender can merge with or after the hub's route | as S11; the promotion test stays |
| Y7 | 4.3 | the P25 match through a step: "the grant's `event_boot` within the call's length plus 70 s before the moment the ingest took the call (10 s of slack and the ingest's settle wait)" | the 70 s holds the "+-10 s" slack, which is inclusive, and the settle wait, which is a limit: the edge could be either | a limit: exactly the call's length plus 70 s is outside |
| Y8 | 4.3 "Clocks" | a step is an offset change of over 5 s "between two samples at most 5 s apart" | samples further apart (capture stalled, or a host asleep) never show a step, so the P25 match cannot fall back to the boot clock across one | as written: no step; the match then rests on the wall test alone |
| Y9 | 4.3 the sender | the resend cap's order: "`sent_boot` within one boot, else `sent_at`" | candidates of two boots: which clock orders them | `sent_at` for all of them (S9) |
| Y10 | 4.3 "The file", the DDL | "every row with `lost_gen` set in the last 24 h"; "never while `lost_gen` is set, kept 24 h" | the row has no time for when `lost_gen` was set | from the row's `sent_at` (S9) |
| Y11 | 4.3 the sender | fresh alarms are those "never yet put in a request" | whether that survives capture's restart: the file has no column for it | this process's memory (S9) |
| Y12 | 4.3, 11 H1d | `U` for the boot age covers "a request's transit, which H1m's 8 s timeout bounds" | at `5f143a80` the 8 s is a total deadline on the alarm post only; a call event without an emergency flag or `alarm_key` goes in a normal batch (60 s), yet the rule may place its start | the rule is applied only to flagged calls, which travel in the alarm post |
| Y13 | 11 H1m | the alarm post holds "a call event that carries an emergency flag or an `alarm_key`" | an analog finished transmission with an `alarm_key` (a `traffic` event) is not named | it goes in the normal batch with its clip (S14) |

### Running

```sh
caffeinate -i .venv/bin/python -m pytest -q -n auto --dist loadfile tests/test_contracts_capture.py \
    tests/test_contracts_schemas.py tests/test_contracts_nodes.py tests/test_contracts_routes.py
```
