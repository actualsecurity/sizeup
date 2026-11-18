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
| `station-error` | as built | 2 | the body of a 4xx / 5xx for a station's token: `error`, `reason` (and the events post's `max_bytes`) |
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
| `station-events` | as built | 1 | the body of `POST /api/station/events`: `{epoch, station, events: [{seq, at, type, data, audio, audio_dropped}]}`, with one sub-schema per event `type` (12) |
| `station-events-answer` | as built | 1 | the hub's 200 answer: `ok`, `acked` (its cursor), `applied`, and `rejected` (the events it did not take: `{i, seq, type, reason, error}`) when there is one |
| `station-jobs` | as built | 1 | the hub's 200 answer to `GET /api/station/jobs?wait=`: `jobs`, each `{id, type, args}`; the arguments of `tts`, `sources_reset` and of each of the 16 functions of `call` |
| `station-job-result` | as built | 1 | the body of `POST /api/station/jobs/<id>`: `ok`, `error`, `result`, `label`, `audio`; what `result` is per function |
| `station-job-result-answer` | as built | 1 | the hub's 200 answer to a job's result: `ok` |

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
| E1 | high | `sizeup/cloud/hub.py:834-843`, `849`, `855`; `sizeup/cloud/station.py:494` | An event the hub fails to apply (a bug on the hub, a shape it does not take, base64 it cannot decode) is counted, logged, skipped and **acked**: the answer's `acked` covers it and has no word about it, so the station deletes it. A page, a transmission or new words are gone on both sides, and only the hub's log says so. The one exception is a `source_event` that fails for a reason of the hub's own: left unacked for three deliveries. Fixed by #762 (merged 2026-11-18) (the cursor still moves past the event, but the answer lists it under `rejected` as `{i, seq, type, reason, error}`, reason `malformed` or `apply_failed`, and the station moves the row to a quarantine instead of deleting it; a page, its text, an alarm and a source_event are sent again later as new events). Left by #762: a transmission, new words and every other kind are kept on the station and never sent again by themselves; the hub's list is in its memory, so a retry after the hub restarted is acked with no `rejected` and deleted; a station or a hub from before #762 on either side is today's loss | main session |
| E2 | high | `sizeup/cloud/station.py:487-493`, `sizeup/cloud/outbox.py:231-244`, `sizeup/cloud/hub.py:806-810` | The station takes a 400 or a 413 on the events post like an outage: the same oldest rows again, for ever. A batch always starts with the oldest row, so one event the hub will never take (one clip over 32 MiB once in base64, a body it cannot read) keeps every other event on the station, the ones before it in the same batch too, until someone deletes the outbox. Fixed by #762 (merged 2026-11-18) (on a 400 / 413 / 422 the station halves the batch until the refused event is alone, asks the hub once with an empty batch whether it takes anything at all, moves that event to its quarantine and sends the rest in order; a single event too large with its clip goes without the clip). Left by #762: E5 | main session, with native (the station's forwarder) |
| E3 | medium | `sizeup/cloud/hub.py:320-321`, `813-818`, `850-851` (one cursor); `550-553` (what keeps a second station out today) | The hub has one `(epoch, seq)` cursor for everything posted to it, not one per station or per outbox. Two outboxes that post to one hub reset it for each other: a retry of a batch the hub had applied is applied a second time (an `activity` is counted twice; new words, radio ids and source events are applied again), and two outbox files with the same epoch (a copied state folder) lose events instead, skipped as delivered before and acked. One post is enough for the first of these: a post without `epoch` sets the cursor back to the start, and the retry after it is applied again (`events/cursor.json`, `a-post-without-an-epoch-moves-the-cursor`). Today only one station may post events (409 `multi_station_off` for another), so otherwise it takes two outboxes of that one station: a move to another machine, or two programs sharing its identity. Fixed: a cursor per (station, epoch) | main session (multi-station, design step A3) |
| E4 | low | `sizeup/cloud/hub.py:164`, `1204`; `sizeup/incidents.py:58-60` | For `traffic` and `alarm` the hub does not ignore a key it does not know: every key named `[a-z][a-z0-9_]{0,30}` (but `has_audio`) becomes an attribute of its copy of the transmission and a key of the row every console gets. A key a station adds changes what consoles receive without the hub having said it understands it (rule 2), and one named like an attribute the hub's own code sets lands on it. Fixed: the hub takes the keys the schema names | main session |
| E5 | medium | `sizeup/web/server.py:901-907`, `935-938`; `sizeup/cloud/client.py:140-150` | The hub answers a body over a route's limit (32 MiB of events, 16 MiB of a job's result) with 413 without reading it, and closes the connection. A client still sending, as it is with a body that size, gets a broken pipe or a reset before it can read the answer: the station's client sends once more, fails the same way and reports that the hub closed the connection (an outage), never the 413. So over a direct connection the one real 413 of E2, a single event whose clip is over 24 MiB, is sent again for ever with #762 too (its split needs the 413 to arrive; a proxy that reads the whole request first may hand it on). Fixed: the station's client gets status 413 and the hub's reason | main session |
| J1 | medium | `sizeup/cloud/hub.py:216`, `426`-`427`, `583`-`584`; `sizeup/cloud/station.py:614`-`615` | A job is delivered at most once, both ways. It is marked taken when a poll's answer is written: if that answer never reaches the station (a dropped connection, a restart just then) no later poll gets it, and the hub's caller waits out its 10 to 30 s and says the station didn't answer. A result that comes after that wait is answered 404 and thrown away although the work was done | main session |
| J2 | low | `sizeup/cloud/station.py:633`-`640` | The station posts a job's result three times at most (1 s, then 2 s apart), then forgets it: nothing is kept for when the hub is back | main session |
| J3 | high | `sizeup/cloud/station.py:614` (outside the handling of `605`-`610`), `635` | The station does not handle a wrong type in the poll's answer. `jobs` that is a number or a boolean, or an answer that is no object, raises in the thread that polls: it ends, and the station takes no job (no switch, no dispatch voice, no re-run) until it restarts, while it still looks connected. An entry of `jobs` that is no object ends only its own thread. Fixed by #782 (merged 2026-11-18) (the answer is left out, the polling goes on) | main session |
| J4 | medium | `sizeup/cloud/station.py:672`, `sizeup/cloud/hub.py:465` | The arguments of `call` are positional, so a station cannot ignore one it does not know: one argument too many is answered `ok: false` with Python's TypeError and nothing is done. This is why the hub guesses a station's build from its status before it adds an argument (D2). The schema has the name of every position (`x-names`) for a form with named arguments | main session, with native |
| J5 | medium | `sizeup/cloud/station.py:669`, `625`; `sizeup/app.py:2322` | A missing key has a meaning of its own: a `call` whose `args` is absent, null or empty runs the function with its defaults, and for `retranscribe` that is every recording on the station's disk; a `sources_reset` whose `args` is no object resets every source | main session |
| J6 | medium | `sizeup/cloud/hub.py:456`-`458`, `2399` | The hub does not handle a wrong type in a job's result in two callers: `audio` of a `tts` result that is no string raises TypeError out of `Hub.render_voice` and `Cad._render_remote` (their callers expect text only when the voice fails: a person's preview of a dispatch with audio gets no answer, the connection is dropped); `queued` of a `catch_up` result that is no list raises out of the catch-up's hand-over. Not covered by #782 | main session |
| O1 | high | `sizeup/cloud/outbox.py:86-87`, `54`, `94-98`; `sizeup/cloud/hub.py:822-823`, `sizeup/cloud/station.py:494` | An outbox file set back to an earlier state keeps its epoch and hands out seqs the hub already has: the next events are skipped by the hub (seq at or under its cursor), acked, and deleted on the station, without a word. A power cut sets the file back: in WAL mode with `synchronous = NORMAL` SQLite does not sync a commit, so the last adds and the acks' deletes can be gone after the restart. Restoring a copy of the file does the same. Fixed: a seq once handed out is never handed out again under the same epoch (the commit synced before the row is sent, or a new epoch after a stop that was not in order) | main session |
| O2 | low | `sizeup/cloud/outbox.py:211`, `132-133`, read back at `241` | D1 again, in the file: a float NaN or infinity in an event's data is stored as the bare token `NaN` / `-Infinity`. A second implementation reading the row with a strict reader fails on it; Python reads it back and the forwarder sends it on as it is. Fixed: stored as null | main session |
| O3 | medium | `sizeup/cloud/outbox.py:318`, `sizeup/cloud/station.py:715` (data); `698`, `725` (audio), at `040596d3` | The station does not handle a row of the wrong type in its own file. `data` that is not JSON raises in `Outbox.batch()`, outside the forwarder's own error handling: since #782 the thread is started again and counted instead of ending, and raises again on the same row, which is still the oldest. `audio` stored as text raises TypeError in the base64 of the post: since #762 (merged 2026-11-18) that is inside the forwarder's error handling (`_post_events`), which takes it as a failed post, says "a bytes-like object is required" as the hub link's error and sends the same batch again after the backoff; not quarantined (the quarantine is for what the hub refuses). Either way no event goes up, across restarts of the station too. Only a second writer of the file or a damaged row can make one. Fixed: the row is set aside, said once in the log, and the events behind it go up | main session |
| O4 | medium | `sizeup/cloud/outbox.py:85-88`, `sizeup/cloud/stationmode.py:238`, `sizeup/app.py:5803` | The outbox is opened while the station starts, outside any error handling: a file SQLite cannot open (damaged, or using a newer SQLite's features) raises `DatabaseError` there and the station does not start at all, over a file that only queues what goes to the hub. Fixed: the file is moved aside with a line in the log, a new outbox (a new epoch) is made, the station starts | main session |
| O5 | high | `sizeup/cloud/outbox.py:428`, `sizeup/cloud/station.py:693`, `sizeup/cad/sources.py:1794` | A switch back to a build from before #762 and forward again puts an incident source's old data back on an incident, silently. A snapshot S1 the hub refused is quarantined on the station; the older build queues the feed's newer snapshot S2 without noting it in `source_latest` (it has no such table) and the hub applies S2; back on today's code `requeue_due` finds nothing newer noted and sends S1 again, and the hub applies it, since `on_event` skips an older snapshot only when it is more than `SNAPSHOT_SKEW_S` (120 s) older. The incident's type, location and units are S1's until the feed changes the event again. Fixed by #791 (in review, with the O1 / J4-J6 / O3 / O4 fixes): the station marks a re-sent copy (`qid` on the event), the hub marks that source_event `resent`, and `on_event` drops a re-sent copy whose `seen_at` is older than the applied report's, however small the gap, said once in the log; a first send keeps the 120 s tolerance. Not chosen: the station reconciling `source_latest` from its outbox when it opens a file | main session |

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

## The durable lane: events, jobs, the outbox

What must not be lost between a station and the hub goes through three interfaces: the events post
(`station-events`), the jobs the hub hands a station on its long poll (`station-jobs`, `station-job-result`), and the
file the events wait in on the station (`contracts/outbox/`). Their deviations are E (events), J (jobs) and O
(outbox) in the table above: pinned in `tests/test_contracts_events.py`, `tests/test_contracts_events_cursor.py`,
`tests/test_contracts_jobs.py` and `tests/test_contracts_outbox.py`, each behind a switch at the top of its file
(`test_each_deviation_of_the_durable_lane_is_pinned_by_a_test_or_says_it_was_read` holds the rows to the tests, as
`tests/test_contracts_schemas.py` does for D1 to D6). With these, "Routes with no schema" under Known gaps no longer
lists events and jobs.

```
contracts/
  events/cursor.json                 the cursor rules of the events post as golden sequences
  outbox/ddl.sql, columns.json       the station's outbox file as a new one is made (recorded)
  outbox/sequences/*.json            operations on the outbox and what the file holds after each
  outbox/history/<n>-<what>.sql      the file as each older build made it, and every statement that build ran
tests/contracts_lane.py              the helper: a hub that keeps every events post, the drivers of real transmissions,
                                     an event's data held to the sub-schema of its type, the recorder's scrub
tests/test_contracts_events.py       the events post and its answer; which keys the hub reads; E4
tests/test_contracts_events_cursor.py  the golden sequences against a real hub; the station's side; E1, E2, E3, E5
tests/test_contracts_jobs.py         the job poll's answer and a job's result; J1-J6
tests/test_contracts_outbox.py       the outbox file: the recordings, the sequences, the quarantine, an update, a switch
                                     back; O1-O5
```

### Events

`station-events` is the envelope and, under `$defs`, one sub-schema per event `type`. The schema picks the
sub-schema with `if` / `then` on `type`, so an event of a type it does not know validates (the rule), and a wrong
`data` is refused for the one rule it breaks. `x-known` of `type` is held to the code both ways: every type some call
in `sizeup/` queues (`App._emit`, the incident sources' runner) and every type `HubApp.apply` acts on.

| type | keys of `data` (always there / in all) | what | clip |
|---|---|---|---|
| `page` | 22 / 27 | a page off a paging channel, once its recording is complete | yes |
| `page_text` | 22 / 27 | the same document again with its transcript | no |
| `traffic` | 16 / 49 | a finished transmission: the Traffic object's attributes, with `attach` and `pairs` | yes |
| `alarm` | 12 / 48 | an emergency button, ahead of the words: the same object as it is then, without `attach` | when there is one |
| `retext` | 5 / 9 | new words for a clip the hub has | no |
| `rerun` | 3 / 3 | a re-run ended: `changed`, `same`, `failed: why` | no |
| `alt_text` | 3 / 3 | the other engine's words, to compare | no |
| `heard` | 5 / 5 | a radio id heard on the air | no |
| `activity` | 4 / 4 | a minute of key-ups on an encrypted talkgroup of another P25 system | no |
| `source_event` | 6 / 6 (its `event`: 11) | an incident source's new or changed event | no |
| `source_health` | 8 / 8 | an incident source's health | no |
| `source_seen` | 6 / 6 | the event ids an incident source still lists | no |

**As built** here is one run (`tests/contracts_lane.py` `everything`): the station with everything the fakes can give
it is put through every path that queues an event, and what went over the wire to a real hub is checked post by
post. Real: the app's own handlers from the finished segment or call on, speech-to-text's queue, the outbox, the
link, the hub. Standing in: whisper, the live tap's memory of an alarm raised at a key-up, whisper's queue length (for
the hand-over to the pool), and the hub's word that Amazon Transcribe is up. That run sends all 12 types and every
key of every sub-schema but five of a page's (below), which is what `x-complete` on them rests on.

**A transmission's keys.** `traffic` and `alarm` carry "whatever attributes the Traffic object has": ten declared
fields and 39 that one path or another sets. The schema names all 49 (`$defs/transmission`), with type and unit, and
the run reaches each: an FM transmission (`low_energy`, the speech-to-text keys), a piece cut at a key-up (`keyup`,
`id_note`), an MDC emergency packet (`emergency`, `emergency_via`, `alarm_key`), a P25 call (`fm`, `call_id`, `turn`,
`pairs`, `ours`), a P25 emergency with trunk-recorder's record (`evidence`), a flag found stuck on a talkgroup whose
Record is off (`kept`, `flag_stuck`), the airband (`mhz`, `carrier_db`, `carrier_dbfs`, `snr_db`, `opened`,
`notched_hz`, `floor_dbfs`), another P25 system (`system`, `tier`, `source_key`, `group`, `service_auto`, `service`,
`dept`), and the hub's engines (`stt`, `transcript_pending`, `stt_class`). Under the run there is a wider net: every
attribute assigned on a transmission anywhere in `sizeup/app.py` and `sizeup/stt.py` must be one the schema names.

`x-hub` on each of the 49 says what the hub does with it, and `test_which_keys_of_a_transmission_the_hub_reads` holds
it to the hub: each recorded transmission is applied on a real hub as it is, without each key, and with each key
changed.

| `x-hub` | keys | means |
|---|---|---|
| `required` | 5: `id`, `source`, `start`, `end`, `text` | the event fails without it (acked, and listed under `rejected`: E1) |
| `read` | 36 | the hub acts on it by name: changing it changes the radio log's row or the speech-to-text run (24), or the code reads it where that comparison cannot show (12, listed in the test with file and line) |
| `replaced` | 2: `rig`, `ours` | the hub writes its own value over it |
| `kept` | 6: `low_energy`, `carrier_dbfs`, `snr_db`, `opened`, `notched_hz`, `floor_dbfs` | nothing on the hub reads it; it stays on the row the consoles get |

**The clip.** `audio` is base64 (RFC 4648, the standard alphabet, padded, no line breaks) of a whole RIFF / WAVE
file: PCM, mono, 16-bit, 8000 frames a second (the station's 16 kHz clip, every other sample), so it always starts
`UklGR`; null for an event without one. Not gzipped. What is checked: on the station only that a 16 kHz mono 16-bit
WAV is halved (any other WAV, and bytes that are no WAV, go as they are); on the hub nothing (base64 decoded without
validation, the bytes stored as they came, no WAV check). Base64 it cannot decode fails the event (`apply_failed`, E1).

**The cursor.** `contracts/events/cursor.json` has the rules as 19 golden sequences, each replayed against a real hub
over HTTP on a hub that has never had a post: every answer, the cursor after every post, how many times each event
was applied, how many failed.

- `seq` starts at 1 and is never used twice within an epoch; the hub applies a post in `seq` order and answers
  `acked`, the highest `seq` it has dealt with. The station deletes every row up to `acked` and sends what is left;
  any other answer (not 2xx, no usable `acked`) is the same oldest rows again after the backoff.
- `seq` at or under the cursor is skipped as delivered before, and acked: a retry is applied once, a `seq` used
  twice is lost, and so is what arrives late inside a gap (a gap itself is not noticed).
- Another `epoch` than the last post's sets the cursor to 0. The cursor is kept in the hub's database, once per
  post, and survives a restart.
- An event that fails is acked and skipped, and listed under `rejected` (`malformed` or `apply_failed`): the station
  moves it to its quarantine (E1, fixed by #762). A `source_event` the hub fails for a reason of its own: not acked,
  nothing behind it looked at, three deliveries at most.
- A post the hub refuses whole (400 `bad_json` / `bad_batch`, 413 `too_large`, 422) is split by the station until the
  refused event is alone, which goes to the quarantine (E2, fixed by #762); but a 413 for a body that is really over
  the limit does not even reach the station (E5). One cursor serves every sender (E3).
- The station's client sends a request once more, silently, when its connection is closed before an answer: every
  POST of a station can arrive twice. Events are safe by the cursor.

As built and worth knowing (each pinned by a test or a sequence):

- An event's `at` and `audio_dropped` are sent and never read by the hub. A transmission's `text` must be there and
  is then replaced by `raw`. A page's `departments`, `read_twice` and `rerun_result` are sent and never read.
- An item of `events` that is not an object, or an event without a `seq` the hub takes, is not applied and is listed
  under `rejected` as `malformed` by its index (`seq` null), the cursor not moving for it; an unknown `type` is
  acked, counted in `applied`, and no error. So a new type needs a capability string first, as
  `activity` and another P25 system's rows wait for `p25_systems` (held back, not queued, without it).
- The hub takes as `seq` only an integer from 1: a string of digits, a float or a boolean is `malformed` (`seq`
  null), one under 1 is `malformed` with its value; the rest of the post is applied (#762). A post without `epoch` is
  epoch `""` and moves the cursor like any other. A post with no events answers the cursor.
- The hub takes any size of `seq`: `acked` can pass 2^53 if a writer lets it. The schema stops at 2^53 - 1.

**#762 on main** (merged 2026-11-18; its fixed branches were written against its head `9542d08f`, and what landed
was checked to match): E1 and E2 are flipped in `tests/test_contracts_events_cursor.py`, and seven of the 19
sequences replay their `from_762` form from `cursor.json` (the answer's `rejected` list; a `seq` that is no integer
from 1 is `malformed` instead of taken or a 400; a 400 says `bad_batch`): `an-event-that-fails-is-acked-and-skipped`,
`what-is-skipped-without-a-word`, `a-source-event-the-hub-fails-is-sent-again`,
`a-malformed-source-event-is-acked-and-skipped`, `seq-as-a-string-or-a-float`,
`a-seq-that-is-no-number-refuses-the-whole-post`, `a-body-that-is-no-events-post`. What else #762 changed, written
down here: `station-events-answer` describes `rejected` as today's; `station-error` (version 2) has the events post's
reasons `bad_json`, `bad_batch`, `too_large` and the key `max_bytes`; `station-jobs` has a 16th function of `CALLS`,
`quarantine_dismiss(kind)` (Health's Dismiss, `Hub.dismiss_rejected`); `station-state` has `status.outbox.quarantine`
(#789); the outbox file has the `quarantine` and `source_latest` tables, `outbox.qid` and its index (below).

Not covered here: the 503 of a hub that is not the leader (`tests/test_fenced_reads.py` has it); a page's `alt_text`,
`alt_model`, `keyup`, `radio_id`, `unit` (typed from `App._page_doc`, which a test calls with all of them, never on
the wire in the run); the inside of `evidence` (trunk-recorder's own format) and of a feed event's `extra`.

### Jobs

`station-jobs` is what the long poll answers, `station-job-result` what the station posts back, and
`station-job-result-answer` the hub's `{ok: true}` to that (a 404 / 400 / 413 there is the `station-error` envelope,
`error` alone). All are proven on the wire: every job in `tests/test_contracts_jobs.py` is made by the hub's own
callers and run by a real station over HTTP, and each poll answer and result is compared, value for value, with a
recorded vector (`station-jobs/valid/<form>.json`, `station-job-result/valid/<form>.json`; 23 forms). Stand-ins: the
station's voice (a short WAV), its whisper, and one incident-source adapter that reads nothing, in a real
`SourceRunner`.

Driven through the hub's own code: `tts` (`Cad._render_remote`), `sources_reset` (`Hub.reset_source`), and all 16
functions of `call`. `catch_up` goes through the catch-up driver's `tick()` on a clip that was really shed;
`transcribe_now` through `_aws_failed`; `set_channel_prefs` through Settings posts for channels, tones and squelch;
`set_stt_terms` from a real `TermStore`; `set_stt_alert_keys` from `alert_keys_check`, with the intel engine's source
set placed in the app's cache; `quarantine_dismiss` through `Hub.dismiss_rejected` on a station whose quarantine
holds a late alarm (dropped) and one still sent again (kept). The three-argument `set_channel_prefs` and the three-argument county `set_talkgroup`
(an older station) are driven against a station that only polls, with a status that lacks `channels.squelch_ok` /
`p25_systems`.

`call` is positional on the wire. `x-names` gives the App parameter each position lands on (checked against
`inspect.signature`), `x-forms` the list lengths the hub sends today, `maxItems` the last parameter (one more is a
TypeError, J4). `fn` x-known is held to `CALLS` in `sizeup/cloud/station.py`.

| fn | parameters (`x-names`) | lengths sent | result |
|---|---|---|---|
| `set_mode` | mode, sdr, by, confirm | 2, 4 | null or why not |
| `set_talkgroup` | tg, record, main, tier, system | 3, 5 | null or why not |
| `set_talkgroup_group` | group, record, main | 3 | null or why not |
| `retranscribe` | ids, as_alt | 1, 2 | count |
| `set_channel_prefs` | prefs, aws, tones, squelch | 3, 4 | the `channels` status object |
| `emergency_all_clear` | tg, radio_ids | 2 | boolean |
| `transcribe_now` | ids | 1 | count |
| `catch_up` | specs | 1 | `{queued, refused, hold}` |
| `set_adsb_receiver` | serial | 1 | null or why not |
| `set_antenna_groups` | raw, confirm | 1, 2 | null, why not, or `confirm: <question>` |
| `sources_poll_now` | reason | 1 | count |
| `reception_post` | body, by, tid | 3 | null or why not |
| `reception_sample_b64` | sid | 1 | base64 WAV or "" |
| `set_stt_alert_keys` | keys, rev | 2 | `{ok, rev}` |
| `set_stt_terms` | payload | 1 | the `stt_terms` status object |
| `quarantine_dismiss` | kind | 1 | count (the stuck and late quarantined events of that type it dropped) |

A `tts` result carries `audio`: standard base64 of the WAV as the station's voice wrote it. The hub decodes it with
`validate=True` and checks only the first four bytes (RIFF). `wait` is decimal seconds cut to 0..30; missing or not a
number is 0.

As built and worth knowing: an unknown job `type` is answered `this station doesn't know job '<type>'`, an unknown
`fn` `not allowed from the hub: '<fn>'` (the hub looks for `not allowed`: D2, shown here in the two fallbacks that
depend on it). The station ignores a key it does not know in the answer, in a job, in `args` and in each object
inside; the hub ignores one in a result. A body over 16 MiB is 413, but a client still sending it usually sees the
connection close first. The hub also sends `set_channel_prefs`, `set_stt_alert_keys` and `set_stt_terms` by itself on
any state post whose status differs. D3 (a HEAD on the poll takes the jobs) is #774's.

Not typed (open objects): the tone entries in `set_channel_prefs`, the body of `reception_post` beyond `action`, the
antenna group beyond `name`, `sdrs`, `jobs`.

### The outbox file

`state/outbox.sqlite` is the one file a second implementation of the station must open across an update and a
switch back, so its contract is the file itself, not a JSON Schema.

- `outbox/ddl.sql` is a new file as the real Outbox makes it: the header pragmas (WAL, UTF-8, no `user_version`,
  no `application_id`), `synchronous = NORMAL` per connection, and `sqlite_master`. Those six settings are the ones
  pinned (`journal_mode`, `encoding`, `auto_vacuum`, `application_id`, `user_version`, `synchronous`): `page_size`
  and a connection's other settings (`busy_timeout`, `foreign_keys`) are not, and a reader takes any. It is a script: run on an empty
  SQLite file it makes an outbox the station opens as its own (tested). `outbox/columns.json` says the same column
  by column (name, type, NOT NULL, default, primary key; which tables count with AUTOINCREMENT; the index): a file
  made another way, an older build's after its ALTERs for one, is held to that, since its `sqlite_master` text
  differs. Both are recorded by `python -m tests.contracts record`.
- `outbox/sequences/*.json` are steps (`open`, `close`, `remove`, `add`, `batch`, `ack`, `add_stt`, `stt_batch`,
  `stt_ack`, `sql`, and the quarantine's `quarantine`, `requeue_due`, `released`, `late`, `drop`, `dismiss`,
  `strip_audio`, `summary`), each with what it `returns` and what can be seen `after` it: the Outbox's totals, and the
  `outbox`, `stt_inputs`, `quarantine` and `source_latest` tables whole plus `sqlite_sequence`, read by another
  connection (written only when the step changed them). Rows are `[seq, at, kind, data as stored, audio,
  audio_dropped, context, qid]`, `[n, at, data, flac, meta, prio]`, `[id, seq, at, kind, data, audio, audio_dropped,
  context, reason, error, first_at, last_at, tries, critical, next_at, state]` and `[key, seq, at]`; bytes are `{"bytes": n, "fill": "a1"}` (n times one byte) or `{"hex": ...}`;
  an epoch is `epoch-1`, `epoch-2` in the order first seen. The steps are written by hand, `returns` and `after`
  are recorded, and a test per file states in words what it shows:
  `add-batch-ack` (seq from 1, never handed out twice, also after an ack of everything and a reopen; the batch
  limits; ack is safe to repeat and to overshoot; data as compact ASCII JSON text, audio a BLOB),
  `epoch-and-reopen` (the epoch is the file's, a removed file starts over at seq 1 with a new one; rows and the
  `audio_dropped` mark are kept, totals are counted again, the `dropped_*` counters are per run; a lower limit
  drops nothing until the next add), `drop-order` (speech-to-text inputs first, lowest priority then oldest; then
  context audio; then the oldest audio; never an event's row or data), `stt-lane` (priority, fresh first within
  300 s for pre-alert and fire, `stt_ack`), `quarantine` (#762: a refused row moves out of the outbox; a page, its
  text, an alarm and a source_event not called `malformed` are critical and go again as a copy at the end of the
  outbox carrying `qid`, after 600 s, then 1800 s, then hourly, never while a copy waits, `stuck` after 24 h or 8
  refusals; a source_event the feed has a newer snapshot of is dropped instead; `released`, `late`, `dismiss`,
  `drop` with the copy on its way; `strip_audio`; the rows survive a restart, the counters are per run).
- `outbox/history/` is the file as the four earlier builds made it (#3, #419, #671, #701: the last one the day
  before #762) with every statement each ran. **Update**: a file built from each opens under today's code with its rows, epoch and counters and ends with
  today's columns. **Switch back**: every statement of each older build runs on a file today's code wrote, and
  where the checkout has the git history the older `outbox.py` itself is run on it (skipped where it has not). What
  an older build does not see: before #671 the `stt_inputs` rows stay in the file unsent and uncounted until a newer
  build is back; before #419 context audio has no budget of its own; before #701 the inputs go oldest first
  whatever their priority; before #762 the quarantine is left alone (both directions work, tested with the #701
  build's own code): a copy waiting in the outbox goes up as any event and is deleted on the ack, so today's code
  sends it once more when it is back (harmless: the hub keys pages, their text and alarms by id); a newer snapshot
  of an incident source's event that build queues is not noted in `source_latest`, so a quarantined older one goes
  again after it and the hub applies it (O5).

The file's own rules, as built (each tested):

- A column a newer build adds must be nullable or have a default; a table, an index, a `meta` row or a
  `user_version` it adds is left alone by an older build. A `NOT NULL` column without a default makes every older
  build's `add()` fail.
- The file has no version (`user_version` 0, no version row): compatibility is by the columns that are there.
- The next `seq` and `n` come from `sqlite_sequence`, never from `MAX + 1`: a file rebuilt without it would hand
  out seqs the hub has already had (O1).
- While open the outbox is three files. A committed row is in `outbox.sqlite-wal`; the main file copied alone
  from a running or a killed station has none of it.
- Nothing gives the file one owner: a second program can open it beside the station. SQLite keeps seq unique,
  but each opener keeps its own totals, so the limit is each one's own. A second implementation takes the file
  over when the first has stopped.
- The epoch is the outbox's identity towards the hub: never copied to another file (E3).
- What `json` cannot write is stored as `str()` of it, with no error; a numpy scalar as its number.

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
