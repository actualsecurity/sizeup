# The capture spool: layout and rules (designed)

**Status: designed, not implemented.** From `docs/native/capture-split.md` 4.1 and 4.6 (#773). The spool is how
`sizeup-radio` (capture) hands every finished transmission to the station app: a directory on the same filesystem as
`captures/` and the clips, so audio moves by hard link and never by copy. The sidecar's fields are
[`capture-spool-sidecar`](../schemas/capture-spool-sidecar.schema.json), the reader's cursor is
[`capture-spool-cursor`](../schemas/capture-spool-cursor.schema.json). `tests/contracts_capture.py` `check_spool` is
the reference check of the rules below and `Spool` a writer that keeps them, both specification aids for the tests
and not production code. Where this page had to choose, the choice is marked **(C*n*)** and listed for the radio
maintainers in [`../README.md`](../README.md) "Capture split (designed)".

## Layout

```
spool/
  EPOCH                                      the spool_epoch: 16 hex characters and a newline; made with the directory
  SEQ                                        capture's last sequence number, decimal digits and a newline
  tmp/                                       writes in progress; wiped by capture at start
  items/
    000000012345-CHA-1900000000123.wav       the audio, renamed first
    000000012345-CHA-1900000000123.mdc.wav   the cut-off id bursts, optional
    000000012345-CHA-1900000000123.json      the sidecar, renamed LAST: the completion marker
  cursor.app.json                            the reader's, written by the reader only
  quarantine/                                the reader's too: items whose handler kept failing
```

| Name | Rule |
|---|---|
| A file in `items/` | `<seq>-<id><suffix>`: `seq` as 12 decimal digits with leading zeros, the item's `id`, then `.json` (the sidecar), `.wav` (`audio.file`), `.mdc.wav` (`bursts_audio`) or `.tr` (`tr`, a P25 call: a hard link to trunk-recorder's own JSON). Nothing else belongs there |
| Only a sidecar ends in `.json` | a reader lists `items/*.json` and nothing else. That is why trunk-recorder's JSON is linked as `.tr` **(C9)** |
| `id` | a segment: `<channel label without spaces>-<int(start * 1000)>`, today's clip id; a call: trunk-recorder's file stem; a metrics item: `metrics-<int(minute start * 1000)>` **(C7)**. It matches `CLIP_ID` (`sizeup/app.py`) and has no `..`. The same transmission never gets two ids, and an id is on the spool once |
| A sidecar names its own files | `audio.file`, `bursts_audio` and `tr` are names in `items/`, never paths, and carry the sidecar's own `seq` and `id` |

## Publishing one item

1. Write the audio (and the bursts file, and the link to trunk-recorder's JSON) under `tmp/`.
2. Take the publish lock. Take the next sequence number. **(C8)**
3. Rename the audio files into `items/`. Audio first.
4. Write the sidecar under `tmp/` (it carries `seq`, the file names, `published`, `published_boot`) and rename it into
   `items/`. **The sidecar is renamed last: it is the completion marker.**
5. Rewrite `SEQ` (tmp + rename). Release the lock.

| Rule | |
|---|---|
| Atomic | every file is written whole under `tmp/` and renamed into place; nothing in `items/` is ever written in place. A rewritten sidecar (below) is written under `tmp/` and renamed over the old one |
| No item without its audio | a sidecar whose `audio` is not null names a file that is there and is `audio.bytes` long. Audio with no sidecar is debris from a crash: no reader looks at it, and capture removes it at start |
| A short file | after a power cut a file can be shorter than its sidecar says, or empty (there is no `fsync` per item). The reader compares the size on disk with `audio.bytes`, logs and skips the item; it never parses the file as audio |
| A segment's audio | RIFF/WAVE, signed 16-bit little-endian, 16000 Hz, 1 channel: a 44-byte header and 2 bytes a sample, so `bytes` = 44 + 2 x `samples`. The float audio clipped to +-1 and scaled by 32767: exactly today's `<id>.src.wav`. A call's audio is trunk-recorder's own WAV at its own rate, by hard link |
| `fsync` | none per item. Every `spool.sync_s` (5 s) in which something was published, and at stop, capture syncs the files published since and then `items/`. A crash or a power cut loses at most the last seconds of spooled audio; the urgent lane is durable on its own |
| Sequence | one counter for the whole spool, taken under the publish lock, 1 for the first item, at most 12 digits. After a start it is `max(SEQ, the highest number in items/) + 1`, where every file in `items/` counts, debris too: a number that was ever on disk is never given out again **(C8)** |
| Order | per channel, `seq` rises in the order the transmissions ended (`end`). "Per channel" is the channel label for a segment and the system and talkgroup for a call **(C10)**. Nothing is promised across channels |
| At start | capture takes `radio.lock`, wipes `tmp/`, recovers `SEQ`, and removes every file in `items/` that no sidecar completes. The transmission that was open when it died is lost |
| At stop | capture flushes open transmissions (`reason: "stopped"`, `partial: true`), drains its workers, syncs. Drained means every transmission that ended or was flushed is a complete item |

While capture runs, `items/` may hold the audio of **one** item whose sidecar is not renamed yet (the newest sequence
number) and `tmp/` may hold writes in progress. After a start or a stop neither is there. `check_spool(root,
settled=False)` and `check_spool(root)` are the two cases.

## Delivery, the cursor, and who deletes

| Topic | Rule |
|---|---|
| Delivery | at least once. The reader dedupes on `id` |
| The cursor | `cursor.app.json`, written by the reader only (tmp + rename, at most once a second and at stop, only after a handler returned). `seq`: everything at or below is done; `done`: up to 256 higher numbers finished out of order; `tries`: how often an item not yet done was handed to its handler. A cursor of another `spool_epoch` counts for nothing: the reader starts from 0 |
| Done | the row exists and the reader holds its own hard link to the audio |
| Who deletes | **capture only.** It removes the items the cursor says are done, on a `cursor` notice and every 5 s anyway. The reader never deletes in the spool |
| Deleting an item | the sidecar first, then its files: at no moment does a sidecar name audio that is gone **(C11)** |
| An unknown `kind` | acknowledged and skipped by the reader |
| A poison item | a handler that raises leaves the item unacknowledged. After 3 tries (the cursor's `tries`, so across restarts) the reader hard-links it into `quarantine/`, raises a Health item, and moves the cursor past it. `quarantine/` is kept 7 days and at most 100 items |

## Bounds

| Topic | Rule |
|---|---|
| Age and size | unacknowledged items older than `spool.max_age_h` (12) or beyond `spool.max_bytes` (2 GiB) lose their audio first, oldest first, **never an item with an emergency id**. Sidecars go only past `spool.max_items` (50,000) |
| Dropping an item's audio | the sidecar is rewritten first with `audio: null`, `bursts_audio: null`, `audio_dropped: true`; then the audio files are removed **(C11)** |
| The free-space reserve | under `spool.min_free_bytes` (256 MiB) free on the filesystem, capture publishes sidecars without audio (`audio: null, audio_dropped: true`) until there is room again, and Health is critical |
| Disk full all the same | a failed spool write is counted (`spool.dropped` in the status) and logged once a minute; capture keeps running |

## Late

Decided by age at the reader's read and nothing else (`is_late` in the reference): the **larger** of the boot-clock
age (the reader's `CLOCK_BOOTTIME` minus `closed_boot`, when `boot` is the reader's boot id) and the wall-clock age
(now minus `end`). 60 s or more is `late`. Across boots only the wall age exists, and a negative one counts as old.
An emergency is never gated by this, by either clock: its alarm went by the urgent lane, and the late copy attaches
to it.
