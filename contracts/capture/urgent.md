# The urgent lane: items, clocks, the route and its idempotency (designed)

**Status: designed, not implemented.** From `docs/native/capture-split.md` 4.3, 7.5 and 11 (#773, as of `e6bfc5f6`);
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
| `alarm`, analog, live | the live tap decodes an emergency packet | `<slug>-live-<int(carrier start * 1000)>`, the slug of the ids rule (spool.md): today's key for every label of letters, digits, `._-` and spaces | 0 |
| `alarm`, analog, at close | the whole-clip decode finds an emergency the tap did not raise | the transmission's own item id | 0 |
| `alarm`, P25 live (#775) | the emergency bit on a grant, on a transition only. **Not live until #775 lands** | `p25-<system>-<talkgroup>-<radio id>-<int(event_at * 1000)>`: the grant's own time as trunk-recorder stamps it, so one grant on one station always gives one key (a restart or a replay is a `duplicate`). Only when the grant carries no time, capture's `at`; `data.key_time` says which (`grant` \| `capture`). `system`, `talkgroup` and `key_time` are **required** on every P25 live alarm | 0 |
| `page` | every page `LivePaging` shows, ours or not: the first pair that is certain, then each change | `page:<channel>:<started, 2 decimals>` | 0, 1, 2, ... |

On the station an item's identity is `(key, rev)`; on the hub it is `(station, key, rev)`, the station from the
token. An alarm has one revision, 0. `n` is only a row's place in the file. `at` is set once, when capture makes the
item, before any insert, and is the same in the row, in every frame and in every request. **Every key is at most 200
characters**: a longer one (a page of a very long label) is cut to 191, then `~` and the first 8 hex digits of the
SHA-256 of the whole key. With the slug rule every key capture makes is valid.

## Limits

Every limit is exclusive on the inside: under the limit is inside, **exactly the limit is outside**. A slack written
"+-" is inclusive.

| Limit | At exactly the limit |
|---|---|
| a page revision's 120 s; an `ended` revision's 10 min | expired |
| the hub's cutoff for an emergency, on either lane: `max_age_s` x 1.1 (A7: 30 min recommended, so 1980 s) | stale (1979 s is applied) |
| `REPEAT_S`, 120 s: the same radio joining its open alarm | a second alarm |
| 10 min of the boot clock without the P25 bit (capture) | a new press |
| the hub's press window, 600 s of its own clock since the radio's last flag | a new press |
| the wall clock ahead of the boot clock by more than 60 s since the bit was last set (the frozen-clock rule) | exactly 60 s is not more |
| a wall step: the offset wall - boot clock changing by more than 5 s between samples at most 5 s apart | exactly 5 s is no step |
| the hub's 300 s for a live page with no `ended` | ended |
| the P25 match, the grant inside the call's span +-10 s | exactly 10 s outside the span still matches |

## Clocks

| Rule | |
|---|---|
| Within one boot | every age and deadline is on `CLOCK_BOOTTIME`; the boot id says whether two values are of one boot. Where there is no such clock (macOS, inline mode) `time.monotonic()` and an id made at process start stand in |
| An alarm's `age_s` | its boot-clock age, never the larger of two: a wall clock stepped forward must not make a fresh alarm look old. The production station's boot clock runs fast (#788), so this age over-counts by up to 5 %: the hub's cutoffs on an emergency carry a 10 % margin |
| A wall step | capture samples wall - boot clock on its tick; a step is a change of that offset by more than 5 s between two samples at most 5 s apart, and only that. Each is logged once, with its size. The routine backward corrections (up to about 2 s every 30 to 40 s) are not steps, and drift (minutes over hours) is never one. A step is what lets the P25 match fall back to the boot clock |
| Drift and the rate bound | the production boot clock runs 2 to 5 % fast (#788). Every window on the station is safe with a boot clock off by +-10 %: at +10 % `late` comes at about 55 real s, the P25 bit's 10 min ends at about 9.1 min, a page revision's 120 s at about 109 s, the resend window's 120 s at about 109 s; all on the safe side. Nothing compares wall times over hours |
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
| Delivered by the app | the app sends `urgent_delivered` (socket.md) only when the hub's answer to its take-over event says `raised`, `joined`, `duplicate` or `suppressed` for it ([`capture-events-answer`](../schemas/capture-events-answer.schema.json), given only after the event is durably written). For `seen`, `stale` or no outcome (an older hub) it sends nothing: the row stays, and capture sends it as usual. The notice sets the row's `state` to `app` (from `new` or `rejected`; a `sent` row stays `sent`), with `sent_at`, `sent_boot`, `sent_boot_id` from when the notice came and `hub_gen` from it. A memory item gets the mark in memory, and its row is written as `app`. Such a row is delivered (the banner clears, it leaves `unsent`) and is **never sent by capture**. In `send` mode it joins the resend rule like a `sent` row; with no `gen` it is in doubt and is sent again (a duplicate is harmless). In `write` mode nothing can resend it: an `app` row in the window of a `gen` change (learnt from the next `urgent_delivered`) gets a Health warning naming it for 24 h. So a station in `write` mode no longer keeps every alarm "not delivered" for good, and a later switch to `send` does not send that backlog |
| Lost alarms | the resend cap's leftovers get `lost_gen` (the gen they may have been lost under) and `lost_at` (when), and are named in a critical Health item; at start every row whose `lost_at` is in the last 24 h goes back into that warning: it is rebuilt from the rows, never kept only in memory |
| Socket request ids | the `requests` table: for an op with effects, the id recorded before the request is applied, the reply after (socket.md); reads are not recorded |
| Pruned | `sent` and `app` rows the app has taken over, an hour after `sent_at`, never inside the resend window and never within 24 h of `lost_at`; `expired` and `rejected` rows after 24 h; at most 10,000 rows, the oldest `sent` first. **An unsent alarm is never pruned** |
| Corrupt or unreadable at start | renamed `urgent.sqlite.bad-<epoch s>`; a new file with a new `urgent_epoch`; a CRITICAL log line; `urgent.resets` and `urgent.reset_at` in the status. The app's cursor starts at 0 for the new epoch; its taken set still holds |
| Never in it | a token, a body, a transcript. `last_error` holds a status and the hub's `reason` only |

## The sender

Two kept-alive connections: one carries alarms and nothing else, the other page items; a request never holds both.
At most 32 items and 65,536 bytes a request (the UTF-8 of the body as sent, the count of every size in this
contract), one request in flight per connection. **Alarms go in this order, in every request:** 0. an alarm made
alone by a batch refused whole, in a request of its own, at the head; 1. fresh: alarms the hub has never answered
for (no answer recorded in the row: `hub` and `sent_at` null) and made under 120 s ago on the boot clock (`at_boot`;
from another boot, by wall age), in `n` order. Fresh is read from the row, not from the process's memory, so an alarm
whose request got no answer stays fresh across a `gen` change and a restart; 2. the gen rule's resends, newest first;
3. the backlog, oldest first. Items that had an unknown word go after these, each in a request of its own. So a fresh
alarm waits at most for the one request already in flight, never behind resends or a backlog. **The byte limit is enforced
before sending**: items are added in that order while the encoded body stays within it. **Every item is at most
48 KB** (`ITEM_MAX_BYTES`, 49,152 bytes, the same as a frame's): a larger one is sent **trimmed**, never as an
oversize request and never dropped. `trimmed: true` is set, and only free text and extras are cut: **the trim never
touches a field used for identity or matching** (the item's `key`, `rev`, `kind`, `at`, `event_at`, `age_s`,
`age_src`; in `data` `source` and `channel`, so a 300-character label stays whole and the trimmed item still joins the
same radio's alarm within `REPEAT_S`, and `radio_id`, `system`, `talkgroup`, `key_time`, `start`, `end`, `started`,
`until`, `emergency`, `emergency_via`, `phase`, `ours`, `ended`). What is cut, in this order until it fits: `ids`
left out; `pairs` cut from the end; `unit` and `names` cut to 200 characters; any other key left out. A frame is
trimmed the same way. The row keeps the full `data`, and the finished transmission brings all its ids by the spool.
A trimmed item that still gets a `too_large` in the hub's shape is final: `rejected`, a critical Health item, and the
app's lane carries it.

**Checked before sending.** Each item is checked against
[`capture-urgent-item`](../schemas/capture-urgent-item.schema.json) and the route's caps (a key of 1 to 200
characters, `rev` an integer of at least 0) before it goes in a request. Keys are valid by construction, so this
should never fail. An item that does is never put in a request: it becomes `rejected` with `last_error: "invalid"`
and a critical Health item, and the app's lane carries it. One bad item can never make the hub refuse a batch.

**What is an answer.** A 200, 400, 413 or 422 counts only with a body in the hub's shape: for a 200, one that passes
[`capture-urgent-response`](../schemas/capture-urgent-response.schema.json) with `ok: true`; for the three 4xx, an
object with `error` and a `reason` of `bad_json`, `too_large` or `bad_batch`. Without it (an edge's or a proxy's
page, `200 {}`, a 200 that fails the schema, a 422 with no body) it is **no answer**: backed off as for no answer,
the item stays `new`, and no try is counted.

| The hub's answer | What the sender does |
|---|---|
| 200 in the hub's shape, the item named `applied`, `duplicate` or `stale` | the item is **sent**. Nothing else counts |
| 200 in the hub's shape, the item named `rejected` | never sent again on this lane (below) |
| 200 in the hub's shape, the item not named | not taken: sent again with the next request. No try |
| 200 in the hub's shape, the item named with a word the sender does not know | kept and tried again, at most 5 times, after the items that are healthy and in a request of its own, so it never stands in front of another alarm. Only this counts toward the 5. Then it counts as `rejected` |
| No answer, a timeout (5 s), 502, 503, 530, 429, any status not listed here, or a 200/400/413/422 not in the hub's shape | retried: 0.5 s doubling to 2 s for the first 3 min, then to 15 s; `Retry-After` when given. Nothing is dropped, no try is counted |
| 401, 403, 409 | kept and retried every 30 s; `urgent.state` is `refused`, with the hub's `reason` when the body is in its shape |
| 400, 413, 422 in the hub's shape | the hub refused the request whole and applied nothing of it. A `too_large` for a request holding one item not yet trimmed is not counted as a refusal: the item is trimmed and sent alone again. Otherwise the first such refusal of any request, of one item or many, marks each of its items **alone**: each is sent in a request of its own, after the healthy items, backed off. Only the refusal of a request holding exactly one item that was already alone makes that item `rejected`: a lone pending alarm needs two refusals of itself |
| 404, 405 | a hub without the route: `urgent.state` is `no_route`, tried again every 5 min |

| Rule | |
|---|---|
| An alarm that ends `rejected` | is never only that: the app's lane already carries it, a critical Health item names its key, and the station's page shows it as not delivered until the hub has it by the other lane (`urgent_delivered`) |
| The station's own signal | an alarm unsent for 10 s is shown by the app as "EMERGENCY not delivered to the hub" until the hub has it by either lane: capture's own answer, or `urgent_delivered` from the app, which is the only way in `write` mode |
| The send does not wait for the disk | the sender takes the item from memory and waits for its commit at most 50 ms. An item with no row is `durable: false`; a row whose answer came first is written as answered, never left `new` |
| The memory | at most 256 items: page items, and the copies of items that have a row (those are read back from the file). **An alarm without a row is never dropped to make room**: a page item goes, the oldest first. Alarms without rows are bounded only at 10,000; past that the oldest is written out as a CRITICAL log line and let go |
| At start | into the senders go every `new` row, the oldest alarm first (a page revision only if it is still sendable), and every alarm row (`sent` or `app`) answered at or after the last sighting of the hub's `gen` minus 120 s, as "answered under `hub_gen`". The lost-alarm warning is rebuilt from `lost_gen` |
| What stops the sending | any of three: `urgent-mode` not `send`; `hub.forward` false in the wish; a hub that does not list `urgent` in `hub.caps`. Then `urgent.state` is `off`. None of them stops the writing in service mode: the rows and frames are how the app learns of a live alarm at all |
| `urgent.state` | one word, the first that holds: `off`, `no_credentials`, `no_route`, `refused`, `retrying`, `ok`. A reset of the file is not a state: `resets`, `reset_at` |

### The resend rule

A hub crash can lose about 30 s of its newest writes, alarms it had answered `applied` for among them.

| Rule | |
|---|---|
| Every answer carries `gen` | the hub's fencing generation: in the route's 200, in `/api/station/events`' answer (for `urgent_delivered`), and in the answer to the 50 s `GET /api/station/hello` on the alarm connection, which therefore **always runs in `send` mode**: an idle sender learns of a restore within 50 s, not at its next alarm |
| What capture keeps, in the file | in `meta`: the last `gen` it saw and when it last saw it (wall, boot clock, boot id); on each row: the `gen` it was answered under (`hub_gen`) and when (`sent_at`, `sent_boot`, `sent_boot_id`). So the rule holds across capture's own restart |
| By value | 5 and "5" are one generation |
| Another `gen` | capture sends again every alarm (`sent`, or `app` with a known gen) that was answered under a different `gen` at or after **the last sighting of the old `gen` minus 120 s**. The window is anchored there, not at now: it holds however long the hub was away |
| Clocks | the boot clock within a boot; a row or a sighting from another boot uses wall time, and when that leaves doubt the row is sent: a duplicate is harmless |
| Bounds | 32 such alarms at most: the newest by when the hub answered for them (`sent_boot` within one boot, else `sent_at`), and between equal times the higher `n`. Any beyond the 32 get `lost_gen` and `lost_at` in their rows and are named in a critical Health item, rebuilt from the rows after a restart and kept 24 h from `lost_at`. Pages are never sent again |

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
| `suppressed` | either lane received and recorded it, but the admin's settings say no alarm for it (notices off on that channel): final. The urgent route answers `applied` for such an item | `duplicate` |
| `stale` | this lane judged the item too old and made the late notice; `stale_src` keeps which clock that age was on (`boot` \| `wall`) | `duplicate` |
| `seen` | the events lane met the key and raised nothing for a reason that can change: its own age gate said too old, a clock, or the event did not carry the emergency | **judged on its own**, as if the key were new: `applied` or `stale` by its `age_s` |

`raised` overwrites `suppressed` overwrites `stale` overwrites `seen`, never the other way. The events lane has only the station's wall time
against the hub's clock, which a stepped station clock makes wrong by the size of the step; this lane's age is
measured on a clock that does not step. So the events lane's age gate never shadows this lane (it writes `seen`).
The other way round holds **only for a boot-sourced age**: a key marked `stale` with `stale_src: boot` is not raised
by a later event whose wall time looks fresh (the event's row attaches to the late notice). A key `stale` with
`stale_src: wall` blocks nothing on the events lane: a wall-sourced age is two readings of a clock that may have
stepped across a reboot, so a later event is judged on its own, and if it raises, the key becomes `raised` with the
late notice beside the alarm. A retry of the urgent item is `duplicate` either way.

Every age cutoff on an emergency, on either lane, is `max_age_s` x 1.1 (1980 s for 30 min): the station's boot clock
over-counts ages by up to 5 % (#788) and the margin is twice that. "Open", for every join rule, is not ended or
cleared, acknowledged or not. Ended is final for a page: no later state post or revision makes an ended
`(station, key)` live again.

**The press window**, per P25 `(system, radio)`: `{alarm, last}`, `last` on the **hub's own clock**, the moment it
received the radio's last flag (a request or an event). Two station wall times are never compared for it. For each
P25 emergency flag received at hub time `t`: 1. a flag the existing rules tie to an alarm (`id`, `alarm_key`, the
dedupe store) updates it and sets the window to `{that alarm, t}`; 2. otherwise, if the window's alarm exists and is
open and `0 <= t - last < 600 s`, the flag joins it as "emergency still set" (no alarm, no push) and `last = t`, a
live P25 item skipping this step (the station judged it a new press); 3. otherwise it is judged on its own (the age
gate with its margin, notices off, the same-radio join, a new alarm and a push), and the window becomes
`{that alarm, t}` only if that raised or joined one. A negative gap is a new press. An All clear ends the window:
the next flag raises an alarm at any gap, **labelled "emergency still set after All clear"** (the radio maintainers'
wording) when it comes within 10 min, by the hub's receive time, of that radio's last flag before the clear; later
than that it is a new press and is labelled as one. The rule applies from H1 on, to end-of-call flags before #775
too: it can only join an open alarm, so it can never hide one.

**A call from before the All clear** (checked after step 2, before step 3): a flagged call of a radio whose window's
alarm was cleared, and whose **start** came before the clear, attaches to the cleared alarm with **no push**; its row
says "emergency flag on a call from before the All clear", and the window is left as it was. "Before" is judged
only on a clock the hub can trust, and only past its uncertainty `U`: the start, placed on the hub's clock, must be
at least `U` before the hub's time of the clear. The placing: (i) H1m's boot age (the call's `start_boot` against the
request's own boot time), only when the request and the event are of one boot (a reboot between the call and its
post leaves no boot age), `U` = 10 s + 10 % of the age; else (ii) the station's `start` corrected by `hub.skew`, only
when **all** of these hold: the station reports its boot id and `wall_steps` on every state post
([`capture-station-state`](../schemas/capture-station-state.schema.json)); the samples used are of the call's own
boot; one is at or before the call's start and one at or after it; every sample from the last one at or before the
start to the newest has the same `wall_steps` (a reboot always counts as a step); and the newest is under 60 s old.
`U` = 5 s + 2 x the skew's spread over those samples. A station that reports no boot id or `wall_steps` (every
station today) never uses (ii). With neither, or inside `U`, the hub is unsure and the call alarms by step 3, with the
label above.
A real new press starts after the clear, so it is never attached.

**Ages on the events lane** (H1m, required): the take-over `alarm` event carries the item's `event_boot` and `boot`,
a call's event its `start_boot`, and the forwarder adds the request's own boot time at sending; the hub has a
boot-clock age for the age gate, for the push, and for the rule above. Without one (an older station, a wall step)
the age is the hub's now minus the station's `end`, and the push says it is **approximate**. The alarm travels in
**a small post of its own**: the take-over `alarm` event, or any `traffic` event (analog or a P25 call) with an
emergency flag or an `alarm_key`, even when it is the only copy of the alarm,
alone and without clips (the clip follows in a normal batch), with a **total** deadline of 8 s (one monotonic budget
across connect, send and every receive; at it the post is aborted and counts as not delivered). Normal batches keep
today's timeouts. The answer carries `gen` and `outcomes: [{id, outcome}]` for each alarm-carrying event: `raised`,
`joined` (the same radio, still set, a call from before the All clear), `duplicate`, `suppressed`, `stale`, `seen`.

Each row is one or more scenarios of [`urgent-scenarios.json`](urgent-scenarios.json):

| # | Case | What the hub does |
|---|---|---|
| 1 | the first `(station, key)` of an alarm | `applied`: the alarm is raised and pushed |
| 2 | the same key again (a retry, a lost answer, a resend after `gen` changed) | `duplicate` |
| 3 | the app's take-over, an `alarm` event with `id` = `key` | the alarm is updated, not raised again |
| 4 | the finished transmission T with `alarm_key` = `key` | the alarm's `traffic_id` moves from `key` to T; the key stays in the store |
| 5 | the app's lane first, T took the alarm over, a person acknowledged it, then the item for `key` | `duplicate` |
| 6 | an alarm raised at the close with `key` = T, and the app's events for T, in either order | one alarm |
| 7 | late, under the cutoff (`max_age_s` x 1.1) | `applied`: raised and pushed with its real time and age |
| 8 | the app's lane first with a wall time that looks old (the station's clock stepped back 40 min), then the item with `age_s` 1.2 | the event raises nothing and the key is `seen`; the item is `applied`: one alarm, one push |
| 9 | a `traffic` event carrying the `alarm_key` with `emergency: false` first, then the item | `seen`, then `applied` |
| 10 | at or past the cutoff | `stale` with its `stale_src`: a late notice, no push. A retry is `duplicate`; a later event raises nothing when the age was boot-sourced, and is judged on its own when it was wall-sourced |
| 11 | a P25 live alarm and the end-of-call alarm of the same press (#775), in either order | one emergency, by `alarm_key`: capture matched the call to its live alarm on the station, and the hub compares no clocks. An event with no `alarm_key` (an older capture) is matched by the live item's own `data.start` inside the call's span +-10 s, station time against station time |
| 12 | the same radio's next call with the bit still set | the press window: it joins the radio's open alarm, no push, and the alarm says "emergency still set". After an All clear: a call that started before the clear (placed past `U`) attaches to the cleared alarm with no push; otherwise the next flag raises an alarm, labelled "emergency still set after All clear" within 10 min of the radio's last flag before the clear, else a new press |
| 13 | the same radio pressing again while its alarm is open, acknowledged or not | `applied`; it joins the open alarm: no second alarm, no second push |
| 14 | two stations hearing one press | two keys, one per station; the second item is `applied` and joins the open alarm (acknowledged or not) of that radio on that channel (P25: system and talkgroup) raised under `REPEAT_S` before, both placed on the hub's clock: one alarm, one push, two sources. Until multi-station lands the hub takes one station (409 `multi_station_off`) |
| 15 | a page item | the highest `rev` per `(station, key)` wins: higher `applied`, equal `duplicate`, lower `stale`. The entry's expiry is the hub's own: `until` only by its distance from the same request's `sent_at`, kept within 0 to 300 s; with none, 300 s after the last revision |
| 16 | the app's state post for the same page | merged by `key`; the later to arrive wins; ended is final |
| 17 | the finished page, with `live_key` | ends the live entry at once |
| 18 | an unknown `kind` | `rejected`, `reason: "kind"`; the rest of the request is still taken |

**The age limit is not decided.** What a late emergency becomes on the hub is the admin's open question A7. The
recommended rule (the radio maintainers' proposal, `alarms.MAX_AGE_S`) is 30 min, counted with the 10 % margin
(1980 s): under it an alarm and a push that says its age, at it and past it a late notice and no push. In the table and in the model it is the parameter
`max_age_s`; the scenarios that depend on it say so, and no other scenario changes with it.

## P25 live emergencies (#775), capture's side

| Rule | |
|---|---|
| Transition only | an alarm is raised when the bit is set for a `(system, radio)` that has not had it set within the last 10 min, **on the boot clock**: a wall clock stepped back cannot hide a real new press, and one stepped forward cannot re-alarm a stuck bit. Every sighting moves the last-set time on, so a bit that stays set never re-alarms |
| A frozen boot clock | the boot clock may stop while a WSL2 host sleeps, and would make a real press look recent. So, on the same boot, the row also counts as not set lately when the wall clock advanced by more than 60 s more than the boot clock since the bit was last set. A wall step forward of over 60 s looks the same and costs one repeated alarm; drift (5 % of 10 min is 30 s) never reaches it |
| Across restarts | the times are in the urgent file's `p25_bit` table (`last_set_boot`, `boot`), so capture's own restart does not re-alarm. Across a reboot the boot clock says nothing: the row counts as not set lately and the next set bit alarms, once |
| One alarm per press | when capture's ingest takes a finished call it looks in the urgent file for a live P25 alarm of the same system + talkgroup + unit whose grant time falls inside the call's span, 10 s either side (both the station's wall clock). Only when that fails and capture saw a wall step (over 5 s) since the grant does the boot clock decide: the grant under the call's length plus 70 s before the ingest took the call. On a match the call's sidecar carries `alarm_key` |
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
| The uplink is down for hours | the alarm stays `new` and is retried; when the hub answers, the cutoff decides (A7, 30 min x 1.1) |
| The station's wall clock steps | nothing: ages are on the boot clock |
| The station's wall clock stepped back 40 min, and the app's take-over event reaches the hub first | the event raises nothing; the key is only `seen`; capture's item is `applied`. One alarm |
| A P25 live alarm and its call, the station's clock a minute off the hub's | matched on the station, carried as `alarm_key`. One alarm |
| The same radio's bit still set on its next calls | no live alarm (no transition), no new end-of-call alarm: each flag joins the radio's open alarm (the press window); the row says "emergency still set" |
| The wall clock stepped back an hour, then a real new P25 press 30 min later | the boot clock shows 30 min since the last set bit: a transition, an alarm |
| The wall clock stepped forward an hour, a stuck bit 2 s later | the wall clock outran the boot clock by an hour: counted as not set (the frozen-clock rule), **one** repeated alarm. The next sighting: nothing |
| The host slept 20 min with the boot clock frozen, then a real new P25 press | the wall clock shows 20 min: an alarm |
| A P25 radio in emergency for 35 min with the app down; its calls arrive in a burst, ending 35, 28, 21, 14, 7, 1 min ago | the 35 min call is past the cutoff: nothing, and the window is not armed. The 28 min call raises one alarm and one push and arms it; the rest join as "still set" |
| A flag on a talkgroup whose notices are off, then the same radio on a dispatch talkgroup 5 min later | the first raises and arms nothing; the second is an alarm |
| An alarm, All clear, then 20 real min later a new press with the station's clock stepped 15 min behind | a new alarm, labelled "emergency still set after All clear" (by the hub's clock 20 min passed anyway) |
| The station reboots with its wall clock 1 h fast; the reloaded alarm is `stale` on a wall-sourced age; then the app's take-over arrives with its true times | `stale_src: wall` blocks nothing: one alarm, one push, the late notice beside it |
| A boot-clock age 4.7 % over: an alarm 31.5 real min old sent with `age_s` 1979 s | under 1980 s: `applied` |
| `SEQ` rolled back to 4 by a power cut, `items/` empty, the cursor at 10 | the next number is 11, above the cursor: handled. `SEQ` behind is reported (spool.md) |
| An edge answers a lone alarm with a bare 422, or `200 {}` five times | no answer: backed off, no try, nothing rejected |
| The hub refuses a lone alarm with its own 422 `bad_batch` | sent alone again; `rejected` only at the second refusal of it alone, with a critical Health item; the app's lane carries it |
| An alarm whose `data` is over 64 KB on its own | sent trimmed (`trimmed: true`), never as an oversize request, never dropped |
| The hub restores to a point where the key was `seen` and not yet upgraded to `raised` | the restore puts back the whole state; the resend is judged on its own: `applied` |
| Station 1's alarm acknowledged at 4 s, station 2's item for the same press at 9 s | the alarm is still open: it joins. One push |
| More than 32 alarms in the resend window | the newest 32 by answer time, then `n`; the rest named on Health |
| A page's `ended` revision delivered, then a state post still listing it | ended is final |
| The boot clock 5 % fast or slow, the wall clock corrected back about 1.5 s every 30 s | no step is seen, no alarm lost or doubled, every window within its +-10 % bound |
| Two stations hear one press | two items, one alarm, one push |
| A page lasts 130 s | each revision has its own 120 s and `ended` has 10 min: it is sent. If it never is, the hub ends the entry after 300 s |
| The hub answers an alarm with a word capture does not know, again and again | 5 tries, each after the healthy items, then `rejected` with a critical Health item; the app's lane carries the alarm |
| More than 256 items wait in memory | page items go first; an alarm without a row is never dropped for room |
| `urgent-mode write` (no sender), the station's wall clock stepped back more than 30 min | the app's lane carries a boot-clock age (H1m, required in the first version): not dropped. Without H1m it would be dropped, as today; `send` mode closes it too |
| A live P25 alarm; the radio's first call joins it; a "disregard" call (still in emergency) from +330 to +360; All clear at +380; that call's event at +420 | its boot age places its start 50 s before the clear, past `U`: attached to the cleared alarm, no push, "emergency flag on a call from before the All clear". One alarm, one push |
| The same with no boot age and a stale skew | the hub is unsure: an alarm, labelled "emergency still set after All clear". A person looks; nothing is lost |
| A real new press 3 s (or 20 s) after the All clear | its call starts after the clear: never attached. An alarm and a push |
| `urgent_since` with 200 rows of about 380 bytes | filled while the frame fits, `more: true`; the app asks again from the last `n` |
| 300 alarms in memory after the disk failed, then the app reconnects | memory first, page by page (`memory_after`), each page within the frame. Every alarm is taken over |
| `urgent-mode write`, one alarm whose `data` is over 48 KB | framed trimmed, never dropped: the app takes it over and its lane delivers it |
| `urgent-mode write`, the app up | the app sends `urgent_delivered` when the hub acks each take-over event; the row becomes `app`, the banner clears |
| A switch from `write` to `send` with 40 old alarms, then a fresh alarm | delivered ones are `app` rows, never sent; the fresh alarm is first in the first request, then resends, then the backlog |
| The hub answers `too_large` for one alarm alone | it is trimmed and sent alone again; not counted as a refusal |
| A channel label starting with `#` or `(`, or 150 or 300 characters long | the slug is mended with a short hash: a valid id and key |
| More than 32 alarms in the resend window, then capture restarts | the rows over the cap carry `lost_gen`; Health's lost-alarm warning is rebuilt from them |
| `reception_start` sent, capture restarts before replying, the same id again | recorded before applying: the first reply again, or `interrupted` if capture stopped mid-apply. Never two tests |
| An end-of-call alarm with no boot age by the events lane | its push age is the hub's now minus the station's end: labelled approximate |
| Two captures, or capture beside an inline app | cannot both hold `radio.lock` |
| A call's post comes after a station reboot (no boot age), the new boot's clock 40 s ahead; a real press 3 s after the All clear | the skew samples are of the new boot, not the call's: not placed. An alarm and a push, labelled |
| An older station (no boot age, no `wall_steps`), its clock stepped back 30 s after the skew sample and before a real press 3 s after the All clear | the skew path is never used: an alarm and a push |
| Capture's first send gets a 503; the app's take-over is answered with outcome `seen` | no `urgent_delivered`: the row stays `new`, capture's retry is `applied`: one alarm, one push. The same with an older hub that gives no outcome |
| Notices off on the channel: the app's take-over is answered `suppressed` | `urgent_delivered`: the row becomes `app`, and capture stops; no resend loop against a deliberate setting |
| A trimmed alarm with a 300-character label, the same radio within `REPEAT_S` | `source` is never cut: it joins the open alarm. One push |
| An alarm's request gets no answer, then the hub's `gen` changes, then capture restarts | the row has no answer: still fresh, first in the next request, ahead of the resends and the reloaded backlog |
| An `app` row with no `gen`, then a hub restore | in `send` mode it is sent again (one alarm); in `write` mode a Health warning names it for 24 h |
