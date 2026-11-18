# The urgent lane: items, clocks, the route and its idempotency (designed)

**Status: designed, not implemented.** From `docs/native/capture-split.md` 4.3, 7.5 and 11 (#773, as of `6a777c98`);
live P25 emergencies are #775 and the hub's handoff measurement #776. What cannot wait for the app (an emergency
alarm, a live page) is written by capture to its own SQLite file and sent by capture itself to
`POST /api/station/urgent`. The app still takes each item over into its own lane, so the hub sees it again with the
full row; the hub's dedupe makes that harmless. Two things must never happen, and every rule below serves them:
**an emergency is never lost, and nobody is pushed twice for one press.**

| Artifact | |
|---|---|
| [`capture-urgent-item`](../schemas/capture-urgent-item.schema.json) | one item as sent to the hub; `$defs/held`: the same item as capture holds it and hands it to the app; `$defs/data`: the row's `data` |
| [`capture-urgent-request`](../schemas/capture-urgent-request.schema.json), [`capture-urgent-response`](../schemas/capture-urgent-response.schema.json) | the route's body and its answers |
| [`capture-hello-answer`](../schemas/capture-hello-answer.schema.json) | the answer to `GET /api/station/hello`, with the hub's `gen` |
| [`urgent.sql`](urgent.sql) | the urgent file's DDL, the design's byte for byte |
| [`urgent-scenarios.json`](urgent-scenarios.json) | the hub's rules as a table a test runs |
| `tests/contracts_capture.py` `Hub`, `Lane`, `P25Bits`, `match_call`, `age` | reference models of the hub's dedupe and of capture's side. **Specification aids, not production code** |

What is still to confirm is listed in [`../README.md`](../README.md) "Capture split (designed)".

## Items

| `kind` | When | `key` | `rev` |
|---|---|---|---|
| `alarm`, analog, live | the live tap decodes an emergency packet | `<label without spaces>-live-<int(carrier start * 1000)>` | 0 |
| `alarm`, analog, at close | the whole-clip decode finds an emergency the tap did not raise | the transmission's own item id | 0 |
| `alarm`, P25 live (#775) | the emergency bit on a grant, on a transition only. **Not live until #775 lands** | `p25-<system>-<talkgroup>-<radio id>-<int(at * 1000)>` | 0 |
| `page` | every page `LivePaging` shows, ours or not: the first pair that is certain, then each change | `page:<channel>:<started, 2 decimals>` | 0, 1, 2, ... |

On the station an item's identity is `(key, rev)`; on the hub it is `(station, key, rev)`, the station from the
token. An alarm has one revision, 0. `n` is only a row's place in the file. `at` is set once, when capture makes the
item, before any insert, and is the same in the row, in every frame and in every request.

## Limits

Every limit is exclusive on the inside: under the limit is inside, **exactly the limit is outside**. A slack written
"+-" is inclusive.

| Limit | At exactly the limit |
|---|---|
| a page revision's 120 s; an `ended` revision's 10 min | expired |
| the hub's age limit for an alarm (`max_age_s`, A7: 30 min recommended) | stale |
| `REPEAT_S`, 120 s: the same radio joining its open alarm | a second alarm |
| 10 min without the P25 bit (capture) or without a flag (the hub) | a new press |
| the hub's 300 s for a live page with no `ended` | ended |
| the P25 match, the grant inside the call's span +-10 s | exactly 10 s outside the span still matches |

## Clocks

| Rule | |
|---|---|
| Within one boot | every age and deadline is on `CLOCK_BOOTTIME`; the boot id says whether two values are of one boot. Where there is no such clock (macOS, inline mode) `time.monotonic()` and an id made at process start stand in |
| An alarm's `age_s` | its boot-clock age, never the larger of two: a wall clock stepped forward must not make a fresh alarm look old |
| After a reboot | the item's `event_boot` is of another boot. It is sent all the same with `age_src: "wall"` and `age_s` = now minus `event_at`, clamped at 0. `age_s` is never null and an item is never held back for want of a good clock. The hub cannot check a wall-sourced age: it takes it as given, bounded, and labels the alarm's age as approximate |
| An alarm never expires on the station | capture gives up on an alarm by no clock of its own. It keeps sending until the hub answers for it |
| A page's clock runs per revision | a revision unsent 120 s after capture made it (`at_boot`; the **larger** of its boot-clock and wall-clock ages) is `expired`; an `ended` one stays sendable for 10 min. So a wall clock stepped forward can expire a revision early; stepped back it changes nothing; neither ever touches an alarm |
| On the hub | an item is placed at the hub's own now minus `age_s`, never at a station's time. `age_s` is clamped into [0, 24 h]. `sent_at` only shows how far the station's clock is off |

## The file

`/var/lib/sizeup/state/urgent.sqlite` (`paths.urgent_db` in the wish); its DDL is [`urgent.sql`](urgent.sql).

| Rule | |
|---|---|
| One opener | only the holder of `radio.lock` opens it: capture in service mode, the app in inline mode. No second writer, no reader |
| How the app gets the rows | from capture, never from the file: each `urgent` frame carries the item, and after a reconnect the app asks `urgent_since`, page by page. The app keeps the cursor `(urgent_epoch, n)` and the set of `(key, rev)` it has taken (24 h). Take-over is by `(key, rev)`: an item is taken once, however it arrives |
| Insert | `INSERT OR IGNORE` on `(key, rev)`. A failed insert is tried again every 5 s while the item is in memory |
| Checkpoints | by capture only, `PASSIVE`, never while an item waits for its commit; `TRUNCATE` at start |
| Pruned | `sent` rows the app has taken over, an hour after `sent_at`, never inside the resend window; `expired` and `rejected` rows after 24 h; at most 10,000 rows, the oldest `sent` first. **An unsent alarm is never pruned** |
| Corrupt or unreadable at start | renamed `urgent.sqlite.bad-<epoch s>`; a new file with a new `urgent_epoch`; a CRITICAL log line; `urgent.resets` and `urgent.reset_at` in the status. The app's cursor starts at 0 for the new epoch; its taken set still holds |
| Never in it | a token, a body, a transcript. `last_error` holds a status and the hub's `reason` only |

## The sender

Two kept-alive connections: one carries alarms and nothing else, the other page items; a request never holds both.
At most 32 items and 64 KiB a request, one request in flight per connection.

| The hub's answer | What the sender does |
|---|---|
| 200, the item named `applied`, `duplicate` or `stale` | the item is **sent**. Nothing else counts |
| 200, the item named `rejected` | never sent again on this lane (below) |
| 200, the item not named, or named with a word the sender does not know | kept and tried again, at most 5 times, after the items that are healthy and in a request of its own, so it never stands in front of another alarm. Then it counts as `rejected` |
| No answer, a timeout (5 s), 502, 503, 530, 429 | retried: 0.5 s doubling to 2 s for the first 3 min, then to 15 s; `Retry-After` when given. Nothing is dropped |
| 401, 403, 409 | kept and retried every 30 s; `urgent.state` is `refused`, with the hub's `reason` |
| 400, 413, 422 | the request was refused whole; it is cut to single items. An item refused alone is `rejected`: a lone 422 is final |
| 404, 405 | a hub without the route: `urgent.state` is `no_route`, tried again every 5 min |

| Rule | |
|---|---|
| An alarm that ends `rejected` | is never only that: the app's lane already carries it, a critical Health item names its key, and the station's page shows it as not delivered until the hub has it by the other lane |
| The send does not wait for the disk | the sender takes the item from memory and waits for its commit at most 50 ms. An item with no row is `durable: false`; a row whose answer came first is written as answered, never left `new` |
| The memory | at most 256 items: page items, and the copies of items that have a row (those are read back from the file). **An alarm without a row is never dropped to make room**: a page item goes, the oldest first. Alarms without rows are bounded only at 10,000; past that the oldest is written out as a CRITICAL log line and let go |
| At start | into the senders go every `new` row, the oldest alarm first (a page revision only if it is still sendable), and every alarm row answered at or after the last sighting of the hub's `gen` minus 120 s, as "answered under `hub_gen`" |
| What stops the sending | any of three: `urgent-mode` not `send`; `hub.forward` false in the wish; a hub that does not list `urgent` in `hub.caps`. Then `urgent.state` is `off`. None of them stops the writing in service mode: the rows and frames are how the app learns of a live alarm at all |
| `urgent.state` | one word, the first that holds: `off`, `no_credentials`, `no_route`, `refused`, `retrying`, `ok`. A reset of the file is not a state: `resets`, `reset_at` |

### The resend rule

A hub crash can lose about 30 s of its newest writes, alarms it had answered `applied` for among them.

| Rule | |
|---|---|
| Every answer carries `gen` | the hub's fencing generation: in the route's 200, and in the answer to the 50 s `GET /api/station/hello` on the alarm connection, which therefore **always runs in `send` mode**: an idle sender learns of a restore within 50 s, not at its next alarm |
| What capture keeps, in the file | in `meta`: the last `gen` it saw and when it last saw it (wall, boot clock, boot id); on each row: the `gen` it was answered under (`hub_gen`) and when (`sent_at`, `sent_boot`, `sent_boot_id`). So the rule holds across capture's own restart |
| By value | 5 and "5" are one generation |
| Another `gen` | capture sends again every alarm that was answered under a different `gen` at or after **the last sighting of the old `gen` minus 120 s**. The window is anchored there, not at now: it holds however long the hub was away |
| Clocks | the boot clock within a boot; a row or a sighting from another boot uses wall time, and when that leaves doubt the row is sent: a duplicate is harmless |
| Bounds | the 32 newest such alarms at most. Pages are never sent again |

## The route

`POST /api/station/urgent`, listed in [`../routes.json`](../routes.json) under `designed`: a station's own `sz1`
token with the `radio-receive` role, never `sw1`, and not the hub's older shared station token (a station still on
it runs in `urgent-mode write`); a body of at most 65536 bytes and 32 items; idempotent by `(station, key, rev)`;
not "seen"; 503 from a hub that is not the leader, which then applied nothing; 400 `bad_json`, 413 `too_large`,
422 `bad_batch` for a request refused whole.

## Idempotency, on the hub

The dedupe store is keyed by `(station, key)` and holds the highest `rev` taken. It is written by **both** lanes,
each **after** its own decision, and asked before anything is raised. An alarm key is in one of three states:

| State | Written when | An urgent item for that key then gets |
|---|---|---|
| `raised` | an alarm exists for the key, raised by either lane | `duplicate` |
| `stale` | this lane judged the item too old and made the late notice | `duplicate` |
| `seen` | the events lane met the key and raised nothing: its own age gate said too old, or the event did not carry the emergency, or notices are off for it | **judged on its own**, as if the key were new: `applied` or `stale` by its `age_s` |

`raised` overwrites `stale` overwrites `seen`, never the other way. The events lane has only the station's wall time
against the hub's clock, which a stepped station clock makes wrong by the size of the step; this lane's age is
measured on a clock that does not step. So the events lane's age gate never shadows this lane (it writes `seen`),
and a key this lane marked `stale` is not raised by a later event whose wall time looks fresh.

Each row is one or more scenarios of [`urgent-scenarios.json`](urgent-scenarios.json):

| # | Case | What the hub does |
|---|---|---|
| 1 | the first `(station, key)` of an alarm | `applied`: the alarm is raised and pushed |
| 2 | the same key again (a retry, a lost answer, a resend after `gen` changed) | `duplicate` |
| 3 | the app's take-over, an `alarm` event with `id` = `key` | the alarm is updated, not raised again |
| 4 | the finished transmission T with `alarm_key` = `key` | the alarm's `traffic_id` moves from `key` to T; the key stays in the store |
| 5 | the app's lane first, T took the alarm over, a person acknowledged it, then the item for `key` | `duplicate` |
| 6 | an alarm raised at the close with `key` = T, and the app's events for T, in either order | one alarm |
| 7 | late, under the age limit | `applied`: raised and pushed with its real time and age |
| 8 | the app's lane first with a wall time that looks old (the station's clock stepped back 40 min), then the item with `age_s` 1.2 | the event raises nothing and the key is `seen`; the item is `applied`: one alarm, one push |
| 9 | a `traffic` event carrying the `alarm_key` with `emergency: false` first, then the item | `seen`, then `applied` |
| 10 | at or past the age limit | `stale`: a late notice, no push. A retry is `duplicate`, and no later event raises it |
| 11 | a P25 live alarm and the end-of-call alarm of the same press (#775), in either order | one emergency, by `alarm_key`: capture matched the call to its live alarm on the station, and the hub compares no clocks. An event with no `alarm_key` (an older capture) is matched by the live item's own `data.start` inside the call's span +-10 s, station time against station time |
| 12 | the same radio's next call with the bit still set | nothing is raised and the alarm says "emergency still set": a flagged call is a new press only when no flag of that radio was seen in the 10 min before it. An All clear does not clear this |
| 13 | the same radio pressing again while its alarm is open | `applied`; it joins the open alarm: no second alarm, no second push |
| 14 | two stations hearing one press | two keys, one per station; the second item is `applied` and joins the open alarm of that radio on that channel (P25: system and talkgroup) raised under `REPEAT_S` before, both placed on the hub's clock: one alarm, one push, two sources. Until multi-station lands the hub takes one station (409 `multi_station_off`) |
| 15 | a page item | the highest `rev` per `(station, key)` wins: higher `applied`, equal `duplicate`, lower `stale`. The entry's expiry is the hub's own: `until` only by its distance from the same request's `sent_at`, kept within 0 to 300 s; with none, 300 s after the last revision |
| 16 | the app's state post for the same page | merged by `key`; the later to arrive wins |
| 17 | the finished page, with `live_key` | ends the live entry at once |
| 18 | an unknown `kind` | `rejected`, `reason: "kind"`; the rest of the request is still taken |

**The age limit is not decided.** What a late emergency becomes on the hub is the admin's open question A7. The
recommended rule (the radio maintainers' proposal, `alarms.MAX_AGE_S`) is 30 min: under it an alarm and a push that
says its age, at it and past it a late notice and no push. In the table and in the model it is the parameter
`max_age_s`; the scenarios that depend on it say so, and no other scenario changes with it.

## P25 live emergencies (#775), capture's side

| Rule | |
|---|---|
| Transition only | an alarm is raised when the bit is set for a `(system, radio)` that has not had it set within the last 10 min, **on the boot clock**: a wall clock stepped back cannot hide a real new press, and one stepped forward cannot re-alarm a stuck bit. Every sighting moves the last-set time on, so a bit that stays set never re-alarms |
| Across restarts | the times are in the urgent file's `p25_bit` table (`last_set_boot`, `boot`), so capture's own restart does not re-alarm. Across a reboot the boot clock says nothing: the row counts as not set lately and the next set bit alarms, once |
| One alarm per press | when capture's ingest takes a finished call it looks in the urgent file for a live P25 alarm of the same system + talkgroup + unit whose grant time falls inside the call's span, 10 s either side (both the station's wall clock; through a step of that clock, the boot clock: the grant under the call's length plus 70 s before the ingest took the call). On a match the call's sidecar carries `alarm_key` |
| Health | a counter of transitions an hour (`urgent.p25_transitions_1h`) |

Until #775 lands a P25 emergency reaches the hub when its call ends.

## What can go wrong in the lane

Each row of the design's table is a test (`tests/test_contracts_capture.py`) or a scenario:

| Failure | What happens to an alarm |
|---|---|
| Capture is killed between the decode and the commit | if the request had not gone out either, **the alarm is lost**: it existed only in memory |
| ... after the request went out, before the commit | the hub has it; capture does not know; nothing attaches to the alarm |
| The disk is full, or the insert fails | sent from memory, `durable: false`; lost only if capture also dies before the hub answers |
| An `fsync` stalls | the send goes; the row follows when the disk does, already answered |
| The app is down | nothing changes for the send. The take-over waits |
| The hub answers `applied`, then comes back from a replica 30 s old | the resend rule: the alarm is sent again |
| Capture restarts after the answer, and then the hub comes back from an older replica | the row's `hub_gen` and answer time are in the file: the next answer shows the new `gen`, and the alarm is sent again |
| The hub restores while the senders are idle | the 50 s hello shows the new `gen`; the window is counted from the last sighting of the old one |
| The uplink is down for hours | the alarm stays `new` and is retried; when the hub answers, the age limit decides (A7) |
| The station's wall clock steps | nothing: ages are on the boot clock |
| The station's wall clock stepped back 40 min, and the app's take-over event reaches the hub first | the event raises nothing; the key is only `seen`; capture's item is `applied`. One alarm |
| A P25 live alarm and its call, the station's clock a minute off the hub's | matched on the station, carried as `alarm_key`. One alarm |
| The same radio's bit still set on its next calls | no live alarm (no transition), no end-of-call alarm (the hub's 10 min rule): the row says "emergency still set" |
| The wall clock stepped back an hour, then a real new P25 press 30 min later | the boot clock shows 30 min since the last set bit: a transition, an alarm |
| The wall clock stepped forward an hour, a stuck bit 2 s later | the boot clock shows 2 s: no alarm |
| Two stations hear one press | two items, one alarm, one push |
| A page lasts 130 s | each revision has its own 120 s and `ended` has 10 min: it is sent. If it never is, the hub ends the entry after 300 s |
| The hub answers an alarm with a word capture does not know, again and again | 5 tries, each after the healthy items, then `rejected` with a critical Health item; the app's lane carries the alarm |
| More than 256 items wait in memory | page items go first; an alarm without a row is never dropped for room |
| `urgent-mode write` (no sender), the station's wall clock stepped back more than 30 min | the alarm reaches the hub only by the app's lane, whose event looks too old: **dropped, as it would be today**. Closed by `send` mode and by the design's H1m. Stated, not hidden |
| Two captures, or capture beside an inline app | cannot both hold `radio.lock` |
