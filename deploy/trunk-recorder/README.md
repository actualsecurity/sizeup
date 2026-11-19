# trunk-recorder for Fire/EMS 15

Westchester County P25 Phase II (system A66 / WACN BEE00), South simulcast site. trunk-recorder writes one
WAV + JSON per call into `captures/p25/`, which sizeup ingests (`[p25] capture_dir`).

| File | What |
|---|---|
| `config.json` | The county system: one source, the control channels, `shortName` `westchester`. |
| `talkgroups.csv` | The county talkgroups (80), in trunk-recorder's format. Fire/EMS 15 is 307, 60 Control 301, Airport Incident 333. |
| `unit-event.sh` | The unit script: prints every voice grant for sizeup ([below](#who-is-talking-live-sizeups-talking-highlight)). |
| `systems/<name>/` | Another P25 system with its own trunk-recorder ([below](#more-systems-409)). `systems/mrrs/` is MTA MRRS. |

- `center` 477.19375 MHz at 2.4 MS/s covers the control-channel candidates 476.075 / 476.1125 /
  476.2125 / 476.2375 and the voice channels 478.1875 / 478.3125 (span 2.24 MHz, at the edge of one
  RTL-SDR). Bench check 2026-10-01 on the Mac's V4: 476.2375 +28 dB, 476.1125 +24 dB, 476.075
  +21 dB continuous carriers.
- `modulation: qpsk` = CQPSK/LSM for the simulcast site.
- `device`: `rtl=v4` in the file. sizeup never passes it as written: it runs trunk-recorder on a copy,
  `config.runtime.json`, whose source says `rtl=<index>`, the current index of the dongle that was given Fire/EMS 15
  in Settings > Radios (looked up by its exact serial right before each start).
- `recordUnknown: true`: trunk-recorder records every talkgroup. **sizeup decides what is kept**: each talkgroup has
  a Record switch (Settings > Radios; off = its calls are dropped on arrival). By default every talkgroup is recorded
  except encrypted ones, and the Main ones are `[p25] incident_talkgroups` (307 and 333 in the station's config).

## How it runs

| Where | Who runs trunk-recorder |
|---|---|
| The Linux station | `sizeup-trunk-recorder.service` (`sizeup/radio/trunkrecd.py`), on the dongle sizeup names. It keeps running through a restart or a Python-only update of sizeup. [deploy/station/README.md](../station/README.md#trunk-recorder-its-own-service). |
| A Mac or a bench | sizeup itself, as its child (`[p25] recorder` and `recorder_dir`), started and stopped with the dongle's job. |

On the station `install-linux.sh` copies `talkgroups.csv` and `unit-event.sh` to `/var/lib/sizeup/trunk-recorder/` and
makes `config.json` there from this one at every install and update (capture paths absolute, under
`/var/lib/sizeup/captures`). Change the files here, not there. A change to `config.json` or `talkgroups.csv` restarts
the trunk-recorder service at the next update; nothing else does.

By hand, from this directory (paths in `config.json` are relative to it), with your own build of trunk-recorder
5.2.0:

```sh
trunk-recorder --config=config.json
```

`rtl=v4` then means the dongle with serial `v4`: edit it to your dongle's serial or index. On a one-dongle bench,
stop sizeup's paging/fireground receivers first (they use the same dongle).

## One row per key-up (issue #62)

trunk-recorder keeps a talkgroup's back-to-back transmissions in one call WAV until `callTimeout`
(3 s) of quiet, but it ends a *transmission* on every P25 terminator (the TDU at PTT release), and
5.2.0's call JSON lists every transmission in order: one `srcList` entry (`src`, `time`, `pos`, `tag`,
...) and one `freqList` entry (`pos`, `len`, `error_count`, ...) each, at the same `pos` in the WAV.
`src` is `-1` when no radio ID was decoded for that transmission. sizeup (`sizeup/radio/p25.py`)
cuts the call at every entry: the same radio keying twice is two rows, a `-1` is its own row with
the talker unknown (named by the live grant that opened it, if there was one), and a transmission
that ran into the next talker's (no terminator decoded: trunk-recorder credits it all to the later
radio) is cut where the live grants say the next radio keyed. Each piece is its own clip
(`<call>-t0`, `-t1`, ...), transcript and comms row.

**The station's trunk-recorder is patched for that last case (#638).** In 5.2.0 a transmission ends only on a
terminator: Phase 1's TDU, or on Phase 2 (MRRS) `MAC_HANGTIME` (it leaves `MAC_END_PTT` alone). When the terminator
isn't decoded and the next radio keys up on the same voice channel, stock 5.2.0 goes on writing the same
transmission and the new radio's ID (from the voice channel's link control, or Phase 2's `MAC_PTT`) only replaces the
old one, so both talkers end up in one `srcList` entry credited to the later radio, and the live grants can't help
when the next radio keyed without one. `install-linux.sh` (`tr_patch`) patches `gr_blocks/transmission_sink.*`; what
it does is set per system, in that system's `config.json`:

```json
"sizeup": {"split": "log"}
```

| `split` | trunk-recorder |
|---|---|
| `off` | stock 5.2.0 |
| `log` (default) | stock, and it logs `sizeup split (log): would split: prev <id> -> new <id>, <n> samples into transmission` where it would cut, plus `ID flip` / `ID alias` where one key-up showed two IDs |
| `on` | ends the transmission where the new talker's ID came in: the call JSON lists each talker at its own `pos`, and it logs `Unit ID changed on Voice Channel, ext: <id>` (sizeup names that radio as talking at once) |

A new ID counts as a new talker only when the current talker's ID came from the voice channel (a grant alone
doesn't count: grant and voice IDs can differ), at least 1 s into its turn, and the new ID is heard again (or 1 s
passes) with no flip back to the current talker's ID. A flip back is one radio's two IDs (a roaming radio's home
and local IDs on Phase 2), never a cut. sizeup's supervisor passes the mode as `SIZEUP_TR_SPLIT` when it starts that
system's trunk-recorder, so a change takes effect at the next update that restarts it. CI tests the patch on the
real sink (`.github/scripts/tr_split_test.cc`) and switches between patched and stock builds.

Rollout (#638): `log` on every system for about a week. Then count, per system, the `would split` lines, the short
leading pieces (a cut under ~1.5 s into a transmission) and the `ID flip` / `ID alias` pairs. If they look like real
turns, MRRS goes `on` first, then the county. A bench's own unpatched build ignores the setting: such transmissions
stay merged unless a grant names the next radio.

Those lines are info level and name radios, so they stay in `trunk-recorder.log`; the station log (and CloudWatch)
gets their counts instead (#647), one line per system an hour and when trunk-recorder stops, counts only:
`p25 mrrs: split (log) last hour: 3 would split, 1 ID flips, 2 aliases, in 120 calls (2.5% of calls)` (the % is the
calls with at least one cut; with `on`, `N splits`). Settings > Radios shows the same counts since sizeup started in
the system's hover.

**The grant split (#819), sizeup's own.** A turn can still hold several talkers when the next radio's ID is never
decoded on the voice channel: the patch has nothing to cut at. The control channel still grants it, and
trunk-recorder logs `Unit ID externally set from GRANT: <id> ... doesn't match current: <id>` (a grant for the
talkgroup naming another radio while it records the call). sizeup cuts the turn there (`sizeup/radio/p25.py`
`plan_grant_cuts`) when the grant is at least 0.5 s into the current talker's turn and no voice-ID cut (a turn's
start, a `Unit ID changed on Voice Channel` or `would split` line) is within 1 s of it: at the quietest run of 0.15 s
or more from 1.5 s before the grant to 0.5 s after it, else 0.3 s before the grant. The new part is the granted
radio's, and its row has `cut: "grant"`. Grant updates repeating the current talker are ignored, grants for one radio
within 1 s are one, no part is shorter than 0.4 s, and nothing is cut within 1 s of the call's end. The unit script's
grants count only for a radio coming back (A, B, A: the GRANT line never names the call's first radio again), and a
GRANT line with `state: idle` or `samples: 0` (the sink's stale talker) is left out. It is set per system and read by
sizeup-station only, at its next start. What a change restarts: for another system (`systems/<name>/config.json`) the
supervisor's digest leaves out the two grant keys, so changing their values restarts no trunk-recorder, but adding or
removing them (rather than `"off"`) changes the digest and restarts that system's trunk-recorder once; for the county,
`install-linux.sh` `tr_sig` hashes its whole `config.json`, so any change to its grant keys restarts the whole
`sizeup-trunk-recorder` service (the county's and the other systems') at the update. The deploy that brought the grant
split (#821) restarted both: MRRS's trunk-recorder (the old supervisor hashed the rewritten file) and, with
`trunkrecd.py` changed, the whole service once.

```json
"sizeup": {"grant_split": "log", "grant_split_tune": {"before_s": 1.5, "after_s": 0.5, "latency_s": 0.3, "min_gap_s": 0.15}}
```

`off` (the default) does nothing. `log` (MRRS) only logs each cut it would make, with no radio ID, and with what tuning
needs: the gap's length, level and kind (digital silence, or a talker's pause), or the no-gap fallback, and the grant's
source (`p25 MRRS: grant split (log): PD Dispatch: would cut at 3.1 of 6.0 s, gap 0.25 s at -99 dBFS (silence), -0.4 s
from the grant (log line)`); it adds them to the hourly line (`..., in 120 calls (2.5% of calls), 3 grant cuts (log)`).
`on` makes each part its own row, like the patch's. `grant_split_tune` is optional (`before_s` up to 3 s). A call
written while sizeup was down (#747) gets its grants from `trunk-recorder.log`.

Known limit, to tune from the `log` data before `on`: when a terminator is missed, the idle time before the next radio
keys is not in the WAV, so its grant maps late in the audio by that pause. With a pause of 1.5 s or more the cut lands
1.2-2.2 s into the next talker, and past about 3.5 s it is missed. `before_s` (up to 3.0) widens the search back.

`pos` (both lists) is the sum of the earlier transmissions' lengths, each from its sample count: exactly where the
transmission starts in the call WAV, which ffmpeg makes by concatenating them. 5.2.0 runs the call WAV through
loudnorm by default (a short call then comes out at 192 kHz); that changes its level, never its length. sizeup cuts
the WAV at `pos` as it is.

Settings, kept as they are on purpose:

- `minTransmissionDuration: 0` (the default, written out): no key-up is dropped for being short.
  sizeup decides (a piece under 0.3 s with no ID and no sound is merged into the one before it; one
  under 0.3 s with no voice is kept, not transcribed).
- `transmissionArchive: false` (the default): each transmission's own WAV is not kept. The call
  JSON already places every transmission in the call WAV to 10 ms, so the extra files (with no
  sidecar of their own, and timestamps only in their names) add nothing.
- `callTimeout` stays 3 s: a shorter one splits a conversation into more calls but still puts a
  quick reply in the same call, and it ends calls on control-channel gaps.

## Who is talking, live (sizeup's "talking" highlight)

The talker is named at the keypress: trunk-recorder's unit_script plugin runs `unit-event.sh` for
every voice grant (and every re-grant with a new source on a call in progress, i.e. the next
key-up), on every talkgroup of the system:

    unit-event.sh <shortName> <radioID> call <talkgroup> [patches]

The script prints `SIZEUP-UNIT westchester <radioID> call 307` to trunk-recorder's stdout, which sizeup
reads line by line (`sizeup/radio/live.py`; on the station, from `trunk-recorder.log`, which the service writes): the radio ID is resolved to its rig (CAD radio
registry) and pushed to the pages and the hub at once (bench: ~8 ms from the grant to "talking").

sizeup turns it on by itself: the `config.runtime.json` it generates from `config.json` sets
`"unitScript"` on each system to this directory's `unit-event.sh` by absolute path (`./unit-event.sh`
if that path is too long for the plugin's 200-byte command line; `"unitScript": ""` in config.json
turns it off). It takes effect when trunk-recorder (re)starts: switch the dongle Off and back to
Fire/EMS 15 (on the station a restart of sizeup alone leaves trunk-recorder running). trunk-recorder's log should then list
`- [westchester]: /…/unit-event.sh` under PLUGINS, and sizeup's log
`live p25: Fire/EMS 15 (TG 307) radio … talking via grant: published N ms after the grant` per
key-up on a main talkgroup.

Without it, the talker comes from trunk-recorder's log only: the alias line 3-4 s into the call, or
the "Transmission src" lines when the call ends (station log, 2026-10-08: median 3.9 s on Fire/EMS 15,
a third of the calls named only at the end). Those lines are still read (they fill in aliases, and
a talker never shown live is flashed as "just transmitted"); the finished call's JSON stays the
record of who said what, checked against the grants seen live (above). The plugin forks a tiny
shell per unit event on the system (registrations and affiliations too); only grants print.

## More systems (#409)

`systems/<name>/` is another P25 system with its own trunk-recorder, its own log and its own capture directory
(`captures/p25-<name>`). The county's trunk-recorder is never restarted for its sake. `systems/mrrs/` is Metro-25 /
MTA MRRS, Zone 3 Simulcast: two sources, 770.521875 and 772.721875 MHz at 2.4 MS/s, covering the seven listed
frequencies and the voice channels heard (771.13–773.22 MHz) with at least 150 kHz to spare from each dongle's edge
(docs/cloud.md, "Tuning"). The jobs keep their names, `MRRS 770` and `MRRS 773`.

| File | What |
|---|---|
| `config.json` | trunk-recorder's config for the system, plus a `"sizeup"` block trunk-recorder never sees: `label`, `name`, `jobs` (one dongle job per source, in order: `MRRS 770`, `MRRS 773`), `site` (the site as listed) and `reception` (the reception test's: `watch`, `antenna`, `caveat`; a `watch` talkgroup's grant that isn't recorded (encrypted, no recorder, off, no source) is said in the station log, at most a line a talkgroup a minute, and every grant is counted in Settings > Radios). |
| `talkgroups.csv` | trunk-recorder's columns plus `Tier`, `Service` and `Department`, which sizeup leaves out of the copy it hands trunk-recorder. |

Adding a talkgroup is one line in the csv; adding a system is one directory (`sizeup/radio/systems.py`).

**How it runs.** Each source is a job a dongle can be given in Settings > Radios, by a person only (never by
auto-assign). The system runs only the sources that have a dongle, with the control channels those cover, and
nothing while none has. On the station it is a second trunk-recorder under `sizeup-trunk-recorder.service`, at a
lower CPU priority than the county's.

**Tiers** (`sizeup/tiers.py`): the `Tier` column is each talkgroup's default; Settings > Radios changes it.

| Tier | Recorded | Transcribed | Notes |
|---|---|---|---|
| `alert` | yes | yes | May raise a pre-alert. Under every class of ours in the whisper queue: fire traffic never waits behind it. |
| `text` | yes | yes | No alerts. In the lowest live class of the station's whisper queue: skipped when it waits 15 min. |
| `record` | yes | no | The default for a talkgroup with no tier. |
| `off` | no | no | Dropped on arrival. |
| `activity` | no | no | Encrypted: never decoded or recorded. Only that it was active is kept. An encrypted talkgroup is only ever `activity` or `off`. |

**Rules for every such system:**

- Its traffic is context: never Main, never on our incident, never an incident source.
- Its radio IDs are never looked up in our radio registry.
- A talkgroup ID must be unique across the systems here (checked when the config loads).
- A talkgroup granted on the system that isn't in the file is counted as unknown, not recorded
  (`"recordUnknown": false`).

**Source keys** (`sizeup/sourcekeys.py`): a talkgroup is named with its system, `p25:<system>:<tg>`
(`p25:mrrs:9211`). The county's keep their older form too, `tg:307`, which is what its settings are saved under.

Details and who may see another system's traffic: [docs/cloud.md](../../docs/cloud.md), "More P25 systems".

Before giving MRRS dongles for good, try it with one you have: Settings > Radios > Receivers > **Reception test**
([docs/reception-test.md](../../docs/reception-test.md), #410, #436). MRRS's `config.json` sets
`"controlWarnRate": -1`, so trunk-recorder logs the control channel's decode rate every 3 s (the test and the status
read it). The county's own system can be tested too, from another dongle: sizeup makes a probe-only copy of its
config (`systems/probe-<site>/` in the recorder's working directory, never in this repo) that never touches the
live recorder.
