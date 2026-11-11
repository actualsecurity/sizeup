# Reception test (Settings > Radios)

Can this station hear another P25 system (MTA MRRS) with a dongle it already has? The test lends ONE dongle to the
system for a while, measures what trunk-recorder decodes, and gives the dongle back. Code:
`sizeup/radio/reception.py`. Issue: #410.

## Run it

1. Settings > Radios > Receivers > **Reception test** (on the hub or on the station's own page; admins only).
2. Pick the side, the dongle, the time, and type an antenna note (which antenna, where, pointing where).
3. **Start**. If the dongle runs a job, you are asked to pause it. **Stop** ends a test early; its numbers are kept.

| Field | What it is |
|---|---|
| Side | One 2.4 MHz dongle takes in one side of the system's span at a time: `770` (4 listed control channels) or `773` (3). **Both, in turn** runs one after the other on the same dongle, each for the time picked. |
| SDR | The dongle to lend. Greyed out: held by another app, unplugged, already on this system, or a fire dongle. |
| Time | 15 min, 1 h, 4 h a side, or until stopped (ends by itself after 24 h). |
| Sample | Minutes each captured call can be played before it is deleted (default 10, 0 = keep none, at most 60). |
| Fire dongles | Lets the county trunk, paging and fireground dongles be picked. Their job is **not heard** during the test. |

Start with 15 min on each side to find the control channel, then 1 h or more on the side that has it to count voice.

## The dongle

- A dongle another app holds (the shared claims, `docs/handoff-shortfinal-dongles.md`) is never taken. Switch it Off
  in that app first.
- The ADS-B receiver's dongle is never offered.
- A dongle that runs a job is lent only when you confirm pausing that job by name. The panel shows which job.
- A fire dongle (Fire/EMS 15, paging, a fireground) can't be picked unless **Fire dongles** is ticked, and then only
  after a second warning.
- The paused job comes back when the test ends, is stopped or fails (trunk-recorder not running for 45 s, the dongle
  given another job), and when sizeup starts and finds a test that never ended (a restart, an update, a power cut).
  While a test runs the dongle is saved under its own job, so any restart starts it there.
- If the paused job moved to another dongle meanwhile, the lent dongle goes Off instead.
- A test is refused while the system already runs on a dongle for good: its numbers are then live in the row above.

## The numbers, per side

| Row | Meaning |
|---|---|
| Control | The control channel found (MHz). Its mean decode rate: about 40 messages/s is full strength. **Decoded**: the share of 3 s windows with 10 messages/s or more (trunk-recorder's own "low" line). **Dropout**: the longest run under that. **Retunes**: how often trunk-recorder gave up and tried the next listed frequency. The small line is the rate over time. Hover for each frequency tried. |
| Site | The system ID, WACN, NAC, RFSS and site the control channel announced, against the site as listed in the system's `config.json`. "NOT as listed" = another site's control channel. |
| Signal | **Control**: its level over the noise floor, from an `rtl_power` sweep taken before trunk-recorder gets the dongle (left out when `rtl_power` isn't installed). **Voice SNR**: signal minus noise as trunk-recorder measured it on captured calls. Both are relative, uncalibrated dB: compare antennas with them, not stations. |
| Voice | Calls this dongle captured (its side only), per hour, their audio, and decode errors per second of audio. |
| Encrypted | Grants on encrypted talkgroups and on talkgroups not in the file. A count only. |
| Talkgroup table | **Grants**: key-ups the control channel announced, whichever side the voice is on. **Off side**: grants on a voice channel this dongle doesn't cover. **Calls**, **/h**, **Audio**, **Err/s**: the voice captured on this side. |

Grants without calls mean the talkgroup is active on RF but its voice channel is on the other side: test that side,
or expect to need the second dongle.

## The verdict

| Verdict | When |
|---|---|
| Good | The control channel decoded 90% of the time or more, the site is as listed (or wasn't announced), and voice, if any, has 5 errors/s or fewer. |
| Marginal | Decoded 5% to 90% of the time; or another site's control channel; or voice with more than 5 errors/s. |
| None | Decoded under 5% of the time, or trunk-recorder logged no rate at all. |

The hint under it says what to try, e.g. "Control channel decodes 40% of the time: try a directional (yagi) antenna
toward Harrison". The antenna advice and the watched talkgroups are the system's own
(`"sizeup"."reception"` in `deploy/trunk-recorder/systems/<name>/config.json`). A test of both sides takes its best
side's verdict. The thresholds are constants at the top of `sizeup/radio/reception.py`.

## What a test can't show

- **Some traffic is carried over LTE and is never on RF.** No antenna receives it; a scanner misses it too. If a
  watched talkgroup has no grant in an hour on a side with a good control channel, compare with a scanner over the
  same hour before buying anything.
- A control channel decode says the site is heard. It does not say every voice channel is: voice is weaker at the
  edge than control. Trust the Voice row over the Control row.
- One side at a time: a call whose voice channel is on the other side is counted as a grant, never heard.
- Reception changes with the weather, the time of day and what is parked in front of the antenna. Repeat a test
  before you conclude.

## What is kept

- Nothing of a test is transcribed, logged as radio traffic or sent to the hub as a transmission. No radio ID is kept.
- Captured calls stay on the station as a rolling sample (first 60 s of each, at most 40) for the minutes set, then
  they are deleted. All of them are deleted when sizeup starts. On the hub they are fetched from the station when
  played and never stored.
- The history (last 30 tests, with the antenna notes) is `state/reception_history.json` on the station. Notes can be
  edited afterwards.

## API

`POST /api/reception` (admins): `{"action": "start", "system", "jobs": [..], "sdr", "minutes": 15|60|240|0,
"keep_min", "note", "pause": "<the job on the dongle now>", "protected": true}`, `{"action": "stop"}`,
`{"action": "note", "id", "note"}`. State: `reception` in `/api/state`. A sample: `GET /api/reception/sample?id=`.
