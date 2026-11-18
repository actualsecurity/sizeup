# The urgent lane: items, clocks, the route and its idempotency (designed)

**Status: designed, not implemented.** From `docs/native/capture-split.md` 4.3, 7.5 and 11 (#773); live P25
emergencies are #775 and the hub's handoff measurement #776. What cannot wait for the app (an emergency alarm, a
live page) is written by capture to its own SQLite file and sent by capture itself to
`POST /api/station/urgent`. The app still takes each item over into its own lane, so the hub sees it again with the
full row; the hub's dedupe makes that harmless.

| Artifact | |
|---|---|
| [`capture-urgent-item`](../schemas/capture-urgent-item.schema.json) | one item as sent to the hub; `$defs/held`: the same item as capture holds it and hands it to the app; `$defs/data`: the row's `data` |
| [`capture-urgent-request`](../schemas/capture-urgent-request.schema.json), [`capture-urgent-response`](../schemas/capture-urgent-response.schema.json) | the route's body and its answers |
| [`urgent.sql`](urgent.sql) | the urgent file's DDL, the design's byte for byte |
| [`urgent-scenarios.json`](urgent-scenarios.json) | the hub's idempotency rules as a table a test runs |
| `tests/contracts_capture.py` `Hub`, `Lane`, `P25Bits`, `age` | reference models of the hub's dedupe and of capture's sender. **Specification aids, not production code** |

Choices are marked **(C*n*)** and listed in [`../README.md`](../README.md) "Capture split (designed)".

## Items

| `kind` | When | `key` | `rev` |
|---|---|---|---|
| `alarm`, analog, live | the live tap decodes an emergency packet | `<label without spaces>-live-<int(carrier start * 1000)>` | 0 |
| `alarm`, analog, at close | the whole-clip decode finds an emergency the tap did not raise | the transmission's own item id | 0 |
| `alarm`, P25 live (#775) | the emergency bit on a grant, on a transition only. **Not live until #775 lands** | `p25-<system>-<talkgroup>-<radio id>-<int(event_at * 1000)>` | 0 |
| `page` | every page `LivePaging` shows, ours or not: the first pair that is certain, then each change | `page:<channel>:<started, 2 decimals>` | 0, 1, 2, ... |

An item's identity is `(key, rev)`. `n` is only its row's place in the file. `at` is set once, when capture makes
the item, and is the same in the row, in every frame and in every request **(C13)**.

## Clocks

| Rule | |
|---|---|
| Within one boot | every age and deadline is on `CLOCK_BOOTTIME`; the boot id says whether two values are of one boot. Where there is no such clock (macOS, inline mode) `time.monotonic()` and an id made at process start stand in |
| An alarm's `age_s` | its boot-clock age, never the larger of two: a wall clock stepped forward must not make a fresh alarm look old |
| After a reboot | the item's `event_boot` is of another boot. It is sent all the same with `age_src: "wall"` and `age_s` = now minus `event_at`, clamped at 0. `age_s` is never null and an item is never held back for want of a good clock |
| An alarm never expires on the station | capture gives up on an alarm by no clock of its own. It keeps sending until the hub answers for it |
| A page item's 120 s | the **larger** of its boot-clock and wall-clock ages: past it the item is `expired` and not sent (the finished page comes by the app's lane) |
| On the hub | an item is placed at the hub's own now minus `age_s`. `sent_at` only feeds the skew measure. `age_s` is clamped into [0, 24 h] |

## The sender

Two kept-alive connections: one carries alarms and nothing else, the other page items. At most 32 items and 64 KiB a
request, one request in flight per connection.

| The hub's answer | What the sender does |
|---|---|
| 200, the item named `applied`, `duplicate` or `stale` | the item is **sent**. Nothing else counts |
| 200, the item named `rejected` | never sent again; counted; Health shows it |
| 200, the item not named, or named with a word the sender does not know | not taken: sent again **(C15)** |
| No answer, a timeout (5 s), 502, 503, 530, 429 | retried: 0.5 s doubling to 2 s for the first 3 min, then to 15 s; `Retry-After` when given. Nothing is dropped |
| 401, 403, 409 | kept and retried every 30 s; `urgent.state = "refused"` with the hub's `reason` |
| 400, 413, 422 | the request is cut to single items; an item refused alone becomes `rejected` |
| 404, 405 | a hub without the route: `urgent.state = "no_route"`, tried again every 5 min |
| A `gen` that differs from the last answer's | every alarm marked sent in the last 120 s is sent again |

The send does not wait for the disk: the sender takes the item from memory and waits for its commit at most 50 ms.
An item with no row is `durable: false`; a row whose answer came first is written as `sent`, never left `new`.
At start capture loads every `new` row, the oldest alarm first, and a page item only if it is under 120 s old.

## The route

`POST /api/station/urgent`, listed in [`../routes.json`](../routes.json) under `designed`: an `sz1` credential with
the `radio-receive` role, never `sw1`; a body of at most 65536 bytes and 32 items; idempotent by
`(station, key, rev)`; not "seen"; 503 from a hub that is not the leader, which then applied nothing.

## Idempotency, on the hub

The dedupe store is keyed by `(station, key)` and written by **both** lanes: this route records each item's `key`;
`/api/station/events` records the `id` of every `alarm` event and every `alarm_key` it sees on an `alarm` or
`traffic` event. It is asked before anything is raised. Each row is one or more scenarios of
[`urgent-scenarios.json`](urgent-scenarios.json):

| # | Case | Answer |
|---|---|---|
| 1 | the first `(station, key)` of an alarm | `applied`: the alarm is raised and pushed |
| 2 | the same key again (a retry, a lost answer, a resend after `gen` changed) | `duplicate` |
| 3 | the app's take-over, an `alarm` event with `id` = `key` | the alarm is updated, not raised again |
| 4 | the finished transmission T with `alarm_key` = `key` | the alarm's `traffic_id` moves from `key` to T; the key stays in the store |
| 5 | the app's lane first, T took the alarm over, a person acknowledged it, then the item for `key` | `duplicate`, from the store's entry written by the events lane |
| 6 | an alarm raised at the close with `key` = T, and the app's events for T, in either order | one alarm |
| 7 | late, under the age limit | `applied`: raised and pushed with its real time and age |
| 8 | older than the age limit | `stale`: a late notice, no push. Its key is recorded: a retry is `duplicate` **(C18)** |
| 9 | a P25 live alarm and the end-of-call alarm of the same press (#775) | one emergency: matched by system + talkgroup + unit when the live grant's time is inside the call's span +-10 s, in either order **(C20)**. With no matching call the live alarm stands on its own |
| 10 | the same radio pressing again while its alarm is open | `applied`; the alarm code's own same-radio rule joins it to the open alarm **(C19)** |
| 11 | two stations hearing one press | two keys, one per station; until multi-station lands the hub takes one station (409 `multi_station_off`) |
| 12 | a page item | the highest `rev` per `(station, key)` wins: higher `applied`, equal `duplicate`, lower `stale` |
| 13 | the app's state post for the same page | merged by `key`, the newer wins **(C20)** |
| 14 | the finished page, with `live_key` | ends the live entry at once |
| 15 | an unknown `kind` | `rejected`, `reason: "kind"`; the rest of the request is still taken |

**The age limit is not decided.** What a late emergency becomes on the hub is the admin's open question A7. The
recommended rule (the radio maintainers' proposal, `alarms.MAX_AGE_S`) is 30 min: up to it an alarm and a push that
says its age, past it a late notice and no push. In the table and in the model it is the parameter `max_age_s`
**(C21)**; the scenarios that depend on it say so, and no other scenario changes with it.

## P25 live emergencies (#775), capture's side

An alarm is raised when the emergency bit is set for a `(system, radio)` that has not had it set within the last
10 min. Every sighting moves the last-set time on, so a bit that stays set never re-alarms; the times are in the
urgent file's `p25_bit` table, so capture's own restart does not re-alarm either. A counter of transitions an hour
is on Health (`urgent.p25_transitions_1h`). Until #775 lands a P25 emergency reaches the hub when its call ends.

## What can go wrong in the lane

Each row of the design's table is a test (`test_lane_*` in `tests/test_contracts_capture.py`):

| Failure | What happens to an alarm |
|---|---|
| Capture is killed between the decode and the commit | if the request had not gone out either, **the alarm is lost**: it existed only in memory |
| ... after the request went out, before the commit | the hub has it; capture does not know; nothing attaches to the alarm |
| The disk is full, or the insert fails | sent from memory, `durable: false`; lost only if capture also dies before the hub answers |
| An `fsync` stalls | the send goes; the row follows when the disk does, already `sent` |
| The app is down | nothing changes for the send. The take-over waits |
| The hub answers `applied`, then comes back from a replica 30 s old | the `gen` rule: the alarm is sent again |
| The uplink is down for hours | the alarm stays `new` and is retried; when the hub answers, the age limit decides (A7) |
| The station's wall clock steps | nothing: ages are on the boot clock |
| Two captures, or capture beside an inline app | cannot both hold `radio.lock` |
