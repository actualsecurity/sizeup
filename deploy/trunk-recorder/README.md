# trunk-recorder for Fire/EMS 15

Westchester County P25 Phase II (system A66 / WACN BEE00), South simulcast site. Records only
talkgroups 307 (Fire/EMS 15) and 301 (60 Control) as one WAV + JSON per call into
`captures/p25/`, which sizeup ingests (`[p25] capture_dir`).

- `center` 477.19375 MHz at 2.4 MS/s covers the control-channel candidates 476.075 / 476.1125 /
  476.2125 / 476.2375 and the voice channels 478.1875 / 478.3125 (span 2.24 MHz, at the edge of one
  RTL-SDR). Bench check 2026-10-01 on the Mac's V4: 476.2375 +28 dB, 476.1125 +24 dB, 476.075
  +21 dB continuous carriers.
- `modulation: qpsk` = CQPSK/LSM for the simulcast site.
- `device`: the dongle serial (bench: `rtl=00000001`; deployment: `rtl=fire-p25`).

Run from this directory (paths are relative to it):

```sh
~/.local/src/trunk-recorder/build/trunk-recorder --config=config.json
```

On a one-dongle bench, stop sizeup's paging/fireground receivers first (they use the same dongle).

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

The script prints `SIZEUP-UNIT westchester 41228 call 307` to trunk-recorder's stdout, which sizeup
reads line by line (`sizeup/radio/live.py`): the radio ID is resolved to its rig (CAD radio
registry) and pushed to the pages and the hub at once (bench: ~8 ms from the grant to "talking").

sizeup turns it on by itself: the `config.runtime.json` it generates from `config.json` sets
`"unitScript"` on each system to this directory's `unit-event.sh` by absolute path (`./unit-event.sh`
if that path is too long for the plugin's 200-byte command line; `"unitScript": ""` in config.json
turns it off). It takes effect when sizeup (re)starts trunk-recorder: switch the dongle Off and back to
Fire/EMS 15, or restart sizeup. trunk-recorder's log should then list
`- [westchester]: /…/unit-event.sh` under PLUGINS, and sizeup's log
`live p25: Fire/EMS 15 (TG 307) radio … talking via grant: published N ms after the grant` per
key-up on a main talkgroup.

Without it, the talker comes from trunk-recorder's log only: the alias line 3-4 s into the call, or
the "Transmission src" lines when the call ends (station log, 2026-10-08: median 3.9 s on Fire/EMS 15,
a third of the calls named only at the end). Those lines are still read (they fill in aliases, and
a talker never shown live is flashed as "just transmitted"); the finished call's JSON stays the
record of who said what, checked against the grants seen live (above). The plugin forks a tiny
shell per unit event on the system (registrations and affiliations too); only grants print.
