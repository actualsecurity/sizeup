# Reception test (Settings > Radios)

Can this station hear another P25 system (MTA MRRS) with a dongle it already has? The test lends ONE dongle to the
system for a while, measures what trunk-recorder decodes, and gives the dongle back. Code:
`sizeup/radio/reception.py`. Issue: #410.

**Not yet run on a real radio.** Do the [first real run](#first-real-run) below before you trust a number.

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

Start with 15 min on each side to find the control channel, then 1 h or more on the side that has it to count voice.

## The dongle

- **Fire dongles are never lent**: the county trunk (Fire/EMS 15), paging and the firegrounds. They are greyed out
  and the station refuses them ("protected: not lendable"), whatever the request says. Lend a spare or an airband
  dongle.
- A dongle another app holds (the shared claims, `docs/handoff-shortfinal-dongles.md`) is never taken. Switch it Off
  in that app first. sizeup's claim on the lent dongle is kept from the job it ran, through the test, to the job it
  gets back: there is no moment in which the other app could take it. (On the Linux station the trunk-recorder
  service holds the claim while the test runs; when it lets go at the end there is about a second before the paused
  job has it again.)
- The ADS-B receiver's dongle is never offered.
- A dongle that runs a job is lent only when you confirm pausing that job by name. While the test runs the header
  says `<job> PAUSED`, and the dongle's row above says "lent to reception test · <job> paused" with its buttons locked.
- **Switching the lent dongle** (Off, or anything) from Settings > Radios or the hub counts as Stop: the test ends and
  the paused job comes back. The dongle is never left Off.
- The paused job comes back when the test ends, is stopped or fails (trunk-recorder not running for 45 s), and when
  **sizeup stops** (an update, a restart, a rollback: the test ends before sizeup exits, so the next release, an
  older one too, finds the job already saved and nothing of the test left).
- After a **crash or a power cut** the next start ends the test: it stops the system's trunk-recorder, checks the
  claim is gone, and starts the dongle on its own job. While a test runs the dongle is saved under its own job, so
  any release starts it there.
- If sizeup never comes back, the trunk-recorder service stops the test's system by itself 2 min after the side's
  end (`"until"` in the system's state file) and lets the dongle go.
- A job that can't be given back (the other app got the dongle first) is **owed**: tried again every 5 s until it is
  back. Meanwhile the panel and Settings > Health say so, and it is pushed. Picking another job for that dongle
  yourself ends the retrying.
- If the paused job moved to another dongle meanwhile, the lent dongle goes Off instead.
- A test is refused while the system already runs on a dongle for good, and the system can't be given another dongle
  while a test runs.

## The numbers, per side

| Row | Meaning |
|---|---|
| Control | The control channel found (MHz). Its mean decode rate: about 40 messages/s is full strength. **Decoded**: the share of 3 s windows with 10 messages/s or more (trunk-recorder's own "low" line), out of the windows there should have been. **Dropout**: the longest run under that. **Retunes**: how often trunk-recorder gave up and tried the next listed frequency. The small line is the rate over time. Hover for each frequency tried. |
| Site | The system ID, WACN, NAC, RFSS and site the control channel announced, against the site as listed in the system's `config.json`. "NOT as listed" = another site's control channel. |
| Signal | **Control**: its level over the noise floor, from an `rtl_power` sweep of every side taken before trunk-recorder gets the dongle (left out when `rtl_power` isn't installed). **Voice SNR**: signal minus noise as trunk-recorder measured it on captured calls. Both are relative, uncalibrated dB: compare antennas with them, not stations. |
| Voice | Calls this dongle captured (its side only), per hour, their audio, and decode errors per second of audio. |
| Encrypted | Grants on encrypted talkgroups and on talkgroups not in the file. A count only. |
| Talkgroup table | **Grants**: key-ups the control channel announced, whichever side the voice is on. **Off side**: grants on a voice channel this dongle doesn't cover. **Calls**, **/h**, **Audio**, **Err/s**: the voice captured on this side. |

Grants without calls mean the talkgroup is active on RF but its voice channel is on the other side: test that side,
or expect to need the second dongle.

## The verdict

| Verdict | When |
|---|---|
| Good | The control channel decoded 90% of the time or more **at a mean of 30 messages/s or more**, the site is as listed (or wasn't announced), and voice, if any, has 5 errors/s or fewer. |
| Marginal | Decoded 5% to 90% of the time, or under 30 messages/s; or another site's control channel; or voice with more than 5 errors/s. |
| None | Decoded under 5% of the time, and nothing else shows a control channel. |

**The thresholds are uncalibrated**: first guesses (90%, 5%, 30 messages/s, 5 errors/s), constants at the top of
`sizeup/radio/reception.py`. Set them against a scanner on the first real runs.

"Decode rate not logged": trunk-recorder logged the rate for under half of the test (it logs every window only with
`"controlWarnRate": -1`, which MRRS's `config.json` sets; a build that ignores it logs only the bad ones). Then the
rate can't be judged and the grade comes from the site and the grants: both seen = Good, one = Marginal, never None.

The hint under the verdict says what to try, e.g. "Control channel decodes 40% of the time: try a directional (yagi)
antenna toward Harrison". The antenna advice and the watched talkgroups are the system's own
(`"sizeup"."reception"` in `deploy/trunk-recorder/systems/<name>/config.json`). A test of both sides takes its best
side's verdict.

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
- A call that settles after the test ended is the test's too: dropped (anything that started before the end + 2 min,
  while the system has no dongle of its own), and what trunk-recorder left in the system's capture directory is
  deleted when the test ends.
- Captured calls stay on the station as a rolling sample (first 60 s of each, at most 40) for the minutes set, then
  they are deleted. Never a talkgroup switched Off, never an encrypted call. All samples are deleted when sizeup
  stops and when it starts. On the hub they are fetched from the station when played and never stored.
- The state (the running test, the samples, the history) is `GET /api/reception`, admins only. It is not in the
  state everyone gets; only "lent to reception test" on the dongle's row is.
- The history (last 30 tests, with the antenna notes) is `state/reception_history.json` on the station. Notes can be
  edited afterwards.
- The activity log has a row for each start (the dongle and the paused job), each refused attempt, and each end with
  what became of the dongle. A Stop from the hub answers at once; its outcome is the end row.

## First real run

Nothing here has run on a radio yet. Do this once, with someone at the station PC.

Set-up:
- A spare dongle, or an airband one (HPN Tower suggested). Never a fire dongle (they can't be picked).
- The 770 side, 15 min.
- From the **station's own page**, not the hub.
- The other app (shortfinal) closed.
- Not near an auto-update: check Settings > System that the station runs the hub's build first.

Watch, in this order:

1. `trunk-recorder.log` in `systems/mrrs/` gets a `Control Channel Message Decode Rate` line every 3 s, good or bad.
   None while the panel shows a site: the build ignores `controlWarnRate: -1` (the verdict then says "decode rate not
   logged").
2. Site: "as listed". "NOT as listed" with NAC, RFSS or site off by a format (hex against decimal) is a bug to report.
3. The `rtl_power` sweep: the Signal row shows "control +N dB over floor", and the station log has no "sweep … failed".
   It must exit by itself within 30 s (the test waits for it).
4. The first decode-rate line within 45 s of Start. Later: the test fails ("trunk-recorder absent/down").
5. After the end (or Stop): the paused job is running again within 30 s; `~/.local/state/rtl-claims/<serial>.json`
   names that job; `sdr_modes.json` names it; `state/reception_test.json` is gone.
6. Start again and restart `sizeup-station` mid-test: the history says "interrupted", the job is back, MRRS is off.
7. No radio log row, no clip and no push from MRRS around the end of the test (the two minutes after it too).
8. Voice SNR in the Signal row: only when trunk-recorder writes a non-zero `signal` / `noise` in the call JSON. If it
   stays "n/a" with calls captured, that build doesn't measure it.
9. From the hub: Stop answers at once ("Stopping …"), no error, and the end shows up in the history and the activity log.
10. The captured-calls list is empty once the Sample minutes have passed after the last call.

## API

`POST /api/reception` (admins): `{"action": "start", "system", "jobs": [..], "sdr", "minutes": 15|60|240|0,
"keep_min", "note", "pause": "<the job on the dongle now>"}`, `{"action": "stop"}`, `{"action": "note", "id", "note"}`.
State: `GET /api/reception`. A sample: `GET /api/reception/sample?id=`.
