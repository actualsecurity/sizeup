# The live socket: framing and rules (designed)

**Status: designed, not implemented.** From `docs/native/capture-split.md` 4.2, 4.3 "Credentials" and 7.5 (#773).
`/run/sizeup-radio/live.sock` is a Unix stream socket that capture listens on: mode 0660 in a 0750 directory, the
peer's uid checked on every connection (`SO_PEERCRED`: the `sizeup` user or root, nobody else). Frames out are
[`capture-socket-event`](../schemas/capture-socket-event.schema.json), frames in are
[`capture-socket-request`](../schemas/capture-socket-request.schema.json), and a request's answer is
[`capture-socket-reply`](../schemas/capture-socket-reply.schema.json). Byte vectors are in
[`socket-frames.json`](socket-frames.json). Choices are marked **(C*n*)** and listed in
[`../README.md`](../README.md) "Capture split (designed)".

## Framing

The framing is #709's (`sizeup/cloud/sttencode.py`), unchanged:

```
>II      header length, payload length      two unsigned 32-bit big-endian integers, in bytes
header   one JSON object, UTF-8
payload  raw bytes                          unused in version 1 (there for audio frames if a live listen is ever built)
```

| Rule | |
|---|---|
| Header | one JSON object (RFC 8259: no `NaN`, no `Infinity`), at most 65536 bytes (#709's `MAX_HEADER`). A reader refuses a longer one from the lengths alone, before it reads or allocates anything |
| Payload | a version 1 writer sends none (length 0). A version 1 reader skips a payload of up to 1 MiB and closes the connection on a longer one **(C22)** |
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
| `urgent` frames | carry the item whole; the app takes an item over once, by `(key, rev)`. `durable: false` means no row yet and `n` null; if the row is written later a second frame for the same `(key, rev)` follows with its `n` and `durable: true`, and only moves the cursor |

## Notices (no answer)

`hello`, `poke` (`what: "wish"`), `cursor` (the spool cursor moved), `urgent_taken` (`n`, and the `urgent_epoch` it
counts in **(C24)**), `paging_finished` (`ch`, `page_id`, `end`).

## Requests and replies

A request is `{v, t: "request", id, op, args, by, at, expires_boot}`; the answer is one `reply` frame with the same
`id`, on the same connection: `{v, t: "reply", id, ok, error?, confirm?, result?, at}`.

| Rule | |
|---|---|
| Ids | chosen by the client, unique, 1 to 64 characters **(C23)**. Capture applies each id once and remembers the last 256 for 10 min: a repeat gets the first reply again and changes nothing |
| `expires_boot` | a deadline on `CLOCK_BOOTTIME` (both ends are on one host). A request that reaches capture after it is answered `expired` and not applied. A stepped wall clock expires nothing |
| A repeat and the deadline | the id memory is asked first: a repeat of an id that was applied gets its first reply even after the deadline **(C23)** |
| No reply in time | the client sends the same id again when it reconnects, or gives up and tells the person. Either is safe |
| `ok: true` | capture has applied it. The app then records it in the wish file, which is what makes it outlive a restart of capture |
| `ok: false` | nothing was changed. `error` says why; `confirm: true` means the refusal only needs the person's yes, and `error` is the question |
| The person's yes | the client asks again with `args.confirm: true` **under a new id** **(C23)**: under the old id it would get the refusal again |
| Words in `error` a client acts on | `stale` (a `squelch_apply` whose `expect` is not what runs), `expired`, `unsupported` (an op capture does not know), `forbidden`. Anything else is text for a person **(C26)** |
| Who may ask | a client that said `role: "reader"`; a `viewer` gets `forbidden`. The role is self-declared and both are the same uid: it keeps a status tool from changing anything by mistake, and is no defence against a process of that user |
| What no request can do | return a secret, take a path, take a command line |

| `op` | `args` | Answers besides `ok` |
|---|---|---|
| `pick` | `sdr`, `job`, `confirm` | `refused: parked`, `refused: taken`; `confirm: true` with the question |
| `squelch_apply` | `label`, `open_db`, `close_db`, `min_open_s`, `expect` | `stale`. Without `expect` it is a plain set |
| `reception_start`, `reception_stop` | the body of today's `reception_post`; the test's `id` | a test is running; no dongle to lend; unknown test |
| `probe` | `sdr` | not idle; held; `result`: `audio` or `none` |
| `airband_ab` | `label`, `minutes`, `phase_s` | not an airband channel; not running |
| `check` | `what` (`groups`, `adsb`), `value`, `confirm` | advisory only: it changes nothing, and capture validates again when it applies the wish |
| `urgent_since` | `urgent_epoch`, `n`, `limit` (200, at most 500) | `result`: `{urgent_epoch, rows, more, memory}`: the rows above the cursor in `n` order; `more`: ask again from the last `n`; `memory`: on the first page, the items that have no row (yet), in full |
| `credentials` | `token`, `access_id`, `access_secret` | **uid 0 only**, below |
| `snapshot` | none (`{}`) | `result`: the snapshot |

## The `credentials` request and SO_PEERCRED

Rotating the station's token must not restart the radios. `systemctl reload sizeup-radio` runs a root helper
(`ExecReload=+`), which reads the root-0600 credential file and hands the three values to capture as a `credentials`
request on this socket. Two checks of the peer, one on each side, both from `SO_PEERCRED` on the connected socket:

| Side | Check |
|---|---|
| Capture | accepts `credentials` **only from a peer whose uid is 0**, whatever role the peer declared. Anyone else: `ok: false`, `error: "forbidden"`, and nothing is changed |
| The helper | before it writes anything, reads the listener's credentials and goes on only if the pid is the unit's main pid (`$MAINPID`) and the uid is the unit's user. A socket put at `live.sock` by another process of that user gets nothing; the helper exits non-zero |

On an accepted request capture swaps its two senders' headers, closes their connections so the next request uses
the new values, sets `urgent.creds_at_boot` in the status and logs that it did, never a value. No receiver, squelch
or queue is touched. The request is the only frame of the contract that carries a secret: it is not kept in the id
memory **(C23)**, never logged with its arguments, and no reply repeats it. `SIGHUP` is ignored.
