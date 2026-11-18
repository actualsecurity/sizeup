# The live socket: framing and rules (designed)

**Status: designed, not implemented.** From `docs/native/capture-split.md` 4.2, 4.3 "Credentials" and 7.5 (#773, as of `5f143a80`).
`/run/sizeup-radio/live.sock` is a Unix stream socket that capture listens on: mode 0660 in a 0750 directory, the
peer's uid checked on every connection (`SO_PEERCRED`: the `sizeup` user or root, nobody else). Frames out are
[`capture-socket-event`](../schemas/capture-socket-event.schema.json), frames in are
[`capture-socket-request`](../schemas/capture-socket-request.schema.json), and a request's answer is
[`capture-socket-reply`](../schemas/capture-socket-reply.schema.json). Byte vectors are in
[`socket-frames.json`](socket-frames.json). What is still to confirm is listed in [`../README.md`](../README.md)
"Capture split (designed)".

## Framing

The framing is #709's (`sizeup/cloud/sttencode.py`), unchanged:

```
>II      header length, payload length      two unsigned 32-bit big-endian integers, in bytes
header   one JSON object, UTF-8
payload  raw bytes                          unused in version 1 (there for audio frames if a live listen is ever built)
```

| Rule | |
|---|---|
| Header | one JSON object (RFC 8259: no `NaN`, no `Infinity`), at most 65536 bytes (#709's `MAX_HEADER`), and capture never sends a longer one. A reader refuses a longer one from the lengths alone, before it reads or allocates anything |
| A size | the UTF-8 length of the JSON exactly as it is sent (compact, not ASCII-escaped). Every byte limit of the contract is counted that way: a frame's header, a hub request's body, one item inside either |
| One item | in any frame or reply, at most `ITEM_MAX_BYTES` = 49,152 bytes (48 KB) as encoded. A larger one is framed **trimmed**, by the hub request's rule (`urgent.md`), with `data.trimmed: true`, never dropped. 48 KB fits both a frame and a hub request (65,536 bytes each) with 16 KB to spare for their own fields |
| Payload | a version 1 writer sends none (length 0). A version 1 reader skips a payload of up to 1 MiB and closes the connection on a longer one |
| A bad frame | a length over its limit, a header that is not JSON or not an object: the reader closes the connection. A client comes back and gets a snapshot |
| A stream | frames follow each other with nothing between them, and a read may end anywhere inside one |
| Unknown | a reader ignores a header key it does not know and skips a frame whose `t` it does not know |

The hello of a client is 56 bytes on the wire:

```
00000030 00000000 7b2276223a312c2274223a2268656c6c6f222c22726f6c65223a22726561646572222c22636f6e7472616374223a317d
                  {"v":1,"t":"hello","role":"reader","contract":1}
```

## A connection

1. Capture sends `hello`: `contract`, `impl`, `build`, `pid`, `started`, `spool_epoch`, `urgent_epoch`, `boot`.
2. The client sends its `hello`: `role` (`reader` or `viewer`), `contract`, and `subscribe` for the optional frames
   (`level`).
3. Capture sends a `snapshot`, then frames as things happen. Every header has `v`, `t` and `at`.

| Rule | |
|---|---|
| Never blocks capture | one sender thread writes non-blocking; a client 256 frames behind is disconnected and gets a snapshot when it comes back |
| Nothing is queued for an absent client | live state is latest-wins. What must not be lost is in the spool or the urgent file |
| A reader is attached | while a client that said `role: "reader"` is connected; the status file reports it (`reader`) |
| Epochs | a `spool_epoch` that is not the cursor's resets the spool cursor; an `urgent_epoch` that is not the app's starts its urgent cursor at 0 (its set of taken `(key, rev)` still holds) |
| `urgent` frames | carry the item whole (trimmed past 48 KB); the app takes an item over once, by `(key, rev)`. `durable: false` means no row yet, `n` null, and `m` (the memory counter of this capture process); if the row is written later a second frame for the same `(key, rev)` follows with its `n` and `durable: true`, and only moves the cursor |

## Notices (no answer)

Each one's own fields are required; `at` is optional on a client's frame. `hello` (`role`, `contract`, `subscribe`),
`poke` (`what: "wish"`), `cursor` (the spool cursor moved), `urgent_taken` (`urgent_epoch` and `n`, both: capture
ignores a notice for another epoch, so an `n` from before a reset can never mark new rows as taken),
`urgent_delivered` (`key`, `rev` required, `gen` optional: the hub answered 200 for the app's take-over event that
carries that key, and `gen` is the generation in that answer; all-in-one, the app's `cad.alarms` took it. It is the
only way capture learns what the app's lane delivered; `urgent_taken` only says the app took an item over. The row
becomes `app`: the banner clears and capture never sends it. A key capture does not hold is ignored),
`paging_finished` (`ch`, `page_id`, `end`).

## Requests and replies

A request is `{v, t: "request", id, op, args, by, at, expires_boot}`; the answer is one `reply` frame with the same
`id`, on the same connection: `{v, t: "reply", id, ok, reason?, error?, confirm?, result?, at}`. With `ok: false`
there are two fields, as the station routes' answers have: **`reason`**, one word for the client to act on, and
`error`, text for a person, which no client parses.

| Rule | |
|---|---|
| Ids | chosen by the client, unique, 1 to 64 characters. Capture applies each id once. It records the id in `urgent.sqlite` (table `requests`, fsync'd) **before** it applies the request, and the reply after; it keeps the last 256 for 10 min (on the boot clock; one from another boot by its wall time). So a repeat gets the first reply again and changes nothing, even after its deadline and **even after capture restarted**. A repeat that finds the id recorded with no reply (capture stopped while applying it) gets `ok: false, reason: "interrupted"`: the person looks at the state and asks again under a new id. `credentials` is never recorded |
| `expires_boot` | a deadline on `CLOCK_BOOTTIME` (both ends are on one host). A request that reaches capture at or after it is answered `reason: "expired"` and not applied. A stepped wall clock expires nothing |
| No reply in time (capture restarting) | the client sends the same id again when it reconnects, or gives up and tells the person. Either is safe, because the id outlives capture's restart (row above): an op that is not idempotent by itself, such as `reception_start`, is never applied twice |
| `ok: true` | capture has applied it. The app then records it in the wish file, which is what makes it outlive a restart of capture |
| `ok: false` | nothing was changed. `reason` says why in a word, `error` in text; `confirm: true` (with `reason: "confirm"`) means the refusal only needs the person's yes, and `error` is the question |
| The person's yes | a new request, **under a new id**, with `args.confirm: true`: the same id would only get the first reply again |
| The `reason` words | `stale` (a `squelch_apply` whose `expect` is not what runs), `expired`, `unsupported` (an op capture does not know), `forbidden`, `parked`, `taken` (another app holds the dongle), `held`, `busy`, `invalid`, `interrupted` (capture stopped while applying this id), `confirm`. A word a client does not know is a plain refusal |
| Who may ask | a client that said `role: "reader"`; a `viewer` gets `reason: "forbidden"`. The role is self-declared and both are the same uid: it keeps a status tool from changing anything by mistake, and is no defence against a process of that user |
| What no request can do | return a secret, take a path, take a command line |

| `op` | `args` | Answers besides `ok` |
|---|---|---|
| `pick` | `sdr`, `job`, `confirm` | `parked`; `taken`; `confirm` with the question |
| `squelch_apply` | `label`, `open_db`, `close_db`, `min_open_s` (all four), `expect` | `stale`. Without `expect` it is a plain set |
| `reception_start`, `reception_stop` | the body of today's `reception_post`, not typed by this contract; the test's `id` | a test is running; no dongle to lend; unknown test |
| `probe` | `sdr` | not idle; held; `result`: `audio` or `none` |
| `airband_ab` | `label`, `minutes`, `phase_s` | not an airband channel; not running |
| `check` | `what` (`groups`, `adsb`), `value`, `confirm` | `invalid`; `confirm`; `held`. Advisory only: it changes nothing, and capture validates again when it applies the wish |
| `urgent_since` | `urgent_epoch`, `n` (both needed), `limit` (200, at most 500), `memory_after` (0) | `result`: `{urgent_epoch, rows, memory, memory_last, more}`. `memory`: the items that have no row (yet), after `memory_after`, in the order capture made them (each with its `m`; a new `pid` in `hello` starts the counter again, and the app then asks from 0). `rows`: the rows above the cursor in `n` order, at most `limit`. **The reply is filled only while its encoded frame stays within 65,536 bytes**: memory first (the items at risk), then rows, each at most 48 KB. `memory_last`: the `m` of the last memory item given. `more: true` when anything was left out: ask again from the last `n` given, with `memory_after: memory_last`. 200 rows of a few hundred bytes are about 76 KB, so the default page is often cut short: that is normal |
| `credentials` | `token`, `access_id`, `access_secret` (all three, `""` for none) | **uid 0 only**, below |
| `snapshot` | none (`{}`) | `result`: the snapshot |

## The `credentials` request and SO_PEERCRED

Rotating the station's token must not restart the radios. `systemctl reload sizeup-radio` runs a root helper
(`ExecReload=+`), which reads the root-0600 credential file and hands the three values to capture as a `credentials`
request on this socket. Two checks of the peer, one on each side, both from `SO_PEERCRED` on the connected socket:

| Side | Check |
|---|---|
| Capture | accepts `credentials` **only from a peer whose uid is 0**, whatever role the peer declared. Anyone else: `ok: false`, `reason: "forbidden"`, and nothing is changed |
| The helper | before it writes anything, reads the listener's credentials and goes on only if the pid is the unit's main pid (`$MAINPID`) and the uid is the unit's user. A socket put at `live.sock` by another process of that user gets nothing; the helper exits non-zero |

On an accepted request capture swaps its two senders' headers, closes their connections so the next request uses
the new values, sets `urgent.creds_at_boot` in the status and logs that it did, never a value. No receiver, squelch
or queue is touched. The request is the only frame of the contract that carries a secret: it is never kept in the
memory of answered requests, never logged with its arguments, and no reply repeats it. It has a deadline
(`expires_boot`) like every request. `SIGHUP` is ignored.
