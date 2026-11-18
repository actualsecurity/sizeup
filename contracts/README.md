# Contracts

A **contract** here is one interface between a station (or any node) and the hub, written down so that a second
implementation can speak it: a JSON Schema, example bodies that must and must not validate, and tests that run the
schema against what today's Python really sends. The decision and the order of work are in ADR 0004
(`docs/native/adr/0004-contracts-first.md`, #765; this is step 1 of #758);
the node fields come from [`docs/nodes.md`](../docs/nodes.md).

A schema says what a **writer** sends. A **reader** takes everything a schema allows, treats every key as optional
(an older or a newer build may leave it out) and ignores what it does not know. Today's Python readers are more
lenient still: a value of the wrong type is dropped, not refused.

## The one compatibility rule

1. **Unknown keys are ignored, both ways.** A reader never refuses a body for a key it does not know. A writer may add
   an optional key at any time.
2. **New behaviour is switched on by a named capability string, both ways.** Adding a key changes nothing by itself:
   the other side must have said it understands. Hub to station this is `caps` in the state answer.
3. **A version field means something.** If a body carries one, its reader reads it and acts on it. No version field
   is added for show.
4. **An older build must read what a newer one wrote.** A station switches back to its previous release in seconds,
   and a hub deploy runs two builds side by side.

A change that these four cannot carry (a key removed, renamed, or given another meaning) needs a new name: a new key,
or a new schema, behind a capability string.

## Schemas

| schema | status | version | what |
|---|---|---|---|
| `station-state` | as built | 1 | the body of `POST /api/station/state`: who is talking, the page going out, and about every 5 s the station's status |
| `station-state-answer` | as built | 1 | the hub's 200 answer: `ok`, `build`, `caps`, `pool`, `services`, `station` |
| `station-hello-answer` | as built | 1 | the hub's 200 answer to `GET /api/station/hello` |
| `station-error` | as built | 1 | the body of a 4xx / 5xx on the station's routes: `error`, `reason` |
| `node` | designed | 1 | a node of the registry: `id`, `name`, `kind`, roles, capabilities |
| `roles` | designed | 1 | a node's roles, `{role: options}`, with each role's options |
| `capabilities` | designed | 1 | what a node reports about its hardware and programs |
| `lease` | designed | 1 | the grant of an exactly-one role: `{role, holder, gen, ttl_s}` |
| `node-state` | designed | 1 | `station-state` plus `capabilities`, `capabilities_rev`, `roles_applied` |
| `node-state-answer` | designed | 1 | `station-state-answer` plus `capabilities_wanted`, `roles_rev`, `roles`, `leases` |

- **As built** describes today's code exactly. The tests start a real hub and a real station, record every state
  post and answer on the wire, and fail when one does not validate or carries a key the schema does not name.
- **Designed** is `docs/nodes.md`, not implemented yet. The tests hold these schemas to the design's own tables and to
  the constants it points at in the code, and check that every body valid today stays valid with the new keys absent
  and that today's hub and station ignore the new keys.

## Layout

```
contracts/
  schemas/<name>.schema.json           JSON Schema 2020-12
  vectors/<name>/valid/*.json          bodies a writer may send
  vectors/<name>/invalid/*.json        bodies a writer must never send
  vectors/lease/order.json             pairs of lease ids and how they compare
  routes.json                          every /api/station/ route
tests/contracts.py                     the helper: schemas, vectors, strict JSON, lease ids, routes read from the code
tests/test_contracts_*.py              the tests
```

An invalid vector is refused by the schema, or is not JSON at all (`NaN`). Every id, name and value in the vectors is
made up.

## Conventions in a schema

- `$id` is the file's raw address in this repository; a `$ref` to another schema is its file name. The tests resolve
  them from disk, never over the network.
- Every field has a `description`, with its unit: times are Unix seconds unless the name ends in `_s` (a length in
  seconds) or `_ms`; sizes are bytes unless the name ends in `_gb` (GiB).
- Objects take unknown keys (`additionalProperties: true`). None of the code under these schemas refuses one; a place
  that does would be modelled as it is and listed under Known deviations.
- `x-status`: `as-built` or `designed`. `x-version`: goes up by one with every change to the schema. Only additions
  of optional keys keep the name; see the rule above.
- `x-complete: true` on an object: the schema names every key the code sends there. The tests fail on a key it does
  not name. Objects without it belong to another module and list the keys that are always there or that the reader
  uses.
- `x-known`: the values the code sends today where the type is an open string (`caps`, `reason`). The tests compare it
  with the code's own list.
- Transport: plain JSON (RFC 8259: no `NaN`, no `Infinity`), with a `Content-Length`; the hub does not decode chunked
  bodies. An integer that may pass 2^53 - 1 is sent as a string of decimal digits (`lease.schema.json` `counter`).

## Adding a schema

1. Write `schemas/<name>.schema.json` with `$id`, `title`, `description`, `x-status`, `x-version` and a description
   per field.
2. Add `vectors/<name>/valid/` and `invalid/`, one of the valid ones with a key from a newer build
   (`from_the_future`). Made-up values only.
3. As built: add a test that drives the real writer and the real reader (`tests/contracts.py` `Live` is a hub and a
   station over HTTP), validates what was sent, and sends each reader a key it does not know.
4. Add its row to the table above and its name to the route's row in `routes.json`.
5. If the code deviates from the rule, do not change the code in the same PR: model what it does, add a row under
   Known deviations and a test that pins it.

## Routes

`routes.json` lists every route under `/api/station/`: method, path, the kinds of token the code takes today, the
role that opens it once roles exist (`docs/nodes.md` 3), and the request and answer schemas. The test reads the hub's
three dispatchers (`Hub.handle`, `WorkerApi.handle`, the LLM lane's `handle`) and fails when a route in the code has
no row, a row is not in the code, or a real hub answers a row differently from what it says (fail closed).

## Known deviations

Where today's code departs from the rule or from the design. Not fixed here. Each is pinned by a test
(`test_known_deviation_*`), which fails the day it is fixed so that the row is removed, unless its row says it was
found by reading. Line numbers are those of `b50ea36c`.

| # | where | what | who |
|---|---|---|---|
| D1 | `sizeup/cloud/client.py:130`, `sizeup/cloud/hub.py:619` | The station writes a float NaN as the bare token `NaN` and the hub reads it: not JSON. A reader that is not Python refuses the whole post; the hub's clock skew for the station becomes NaN | main session |
| D2 | `sizeup/cloud/hub.py:1975-1979`, `1936`, `2285`, `1993`, `372`, `1349`, `2391` | No capability strings from station to hub: the hub infers what a station can do from the presence of status keys (`channels.squelch_ok`, `channels.deny_ok`, `p25_systems`, `stt_alert_rev`, `version`) and from the text `not allowed` in a job's error. Found by reading | main session, with native (the registry step) |
| D3 | `sizeup/web/server.py:911` | `HEAD` is answered as `GET` without the body on `/api/station/` too. A `HEAD` on the job poll takes the waiting jobs, which are handed out only once | main session |
| D4 | `docs/nodes.md:245-254` against `sizeup/cloud/hub.py:574`, `576` | The design's route table has no row for `POST /api/station/stt` (each clip's speech-to-text input) and names only the long poll of the jobs, not `POST /api/station/jobs/<id>`. `routes.json` proposes `radio-receive` and `always`. Found by reading | main session (the document's owner) |

As built and worth knowing:

- An answer without `caps` clears the station's whole set, on disk too (pinned by a test).
- The hub reads `paging` only when `talking` is in the same post (pinned by a test).
- No reader of `status.metrics` was found on the hub (by reading).

## Names proposed here

`docs/nodes.md` gives `capabilities`, `capabilities_rev`, `capabilities_wanted`, `roles_rev`, `roles_applied` and the
lease's `{role, holder, gen, ttl_s}`. What it does not name, these schemas propose:

| name | where | proposed |
|---|---|---|
| `roles` | `node-state-answer`, `node` | the key that carries a node's roles in full; an object `{role: options}` |
| `leases` | `node-state-answer` | the key that carries the leases a node holds; a list, one per exactly-one role |
| `gen` as `[leader generation, lease generation]` | `lease` | the pair as a two-item array; each a number up to 2^53 - 1 or a string of digits |
| a rev as 8 to 64 lower-case hex characters | `node-state` | the hash and what exactly it covers are not chosen here |
| the node fields at the top level of the post | `node-state` | not inside `status`, which is on some posts only |
| `capabilities_wanted` as a boolean | `node-state-answer` | |
| `channels[].{job, coverage}`, `priority`, `pool`, `classes`, `llm_jobs`, `models`, `dongle` | `roles` | the per-role options (`fast` and `max_s` are the design's) |
| `channels[].{job, state}`, `net.{hub_rtt_ms, up_kbps}` | `capabilities` | |
| the roles of `POST /api/station/stt` and `POST /api/station/jobs/{id}` | `routes.json` | see D4 |

Still open: the `caps` strings that switch the node fields and the lease on; a list of capability strings from node
to hub (D2); where "load on every post" goes (today it is `status.health`).

## Running

```sh
caffeinate -i .venv/bin/python -m pytest tests/test_contracts_schemas.py tests/test_contracts_nodes.py \
    tests/test_contracts_station_state.py tests/test_contracts_routes.py -q
```

They need `jsonschema` from the dev extras, like `/api/v1`'s contract tests.
