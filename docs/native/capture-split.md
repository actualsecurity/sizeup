# Capture as its own service: `sizeup-radio` behind a spool contract (design)

Status: design, no code (2026-11-18). Follows ADR 0002 (`docs/native/adr/0002-no-native-station-agent-yet.md`, #765):
no native agent now; radio capture leaves the `sizeup-station` process and becomes its own service, in Python first.
The same contract is the seam a native radio daemon would implement later. The radio code's maintainers own the
boundary decision; this document proposes it and marks what it does not know.

Read from the code on `main` at `b50ea36c`, plus three open PRs it must sit on: #767 (per-channel segment workers
owned by the `App`, cheaper live ID decode; reviewed and about to merge, read here at its final diff), #753 (the P25
replay mark for #747; "as proposed in #753" below, it is not merged) and #762 (outbox quarantine). #712 (one switch
per burst of hub deploys) is merged. Nothing here was run: the numbers are the
Phase 1 evidence's, and where this document adds one it says how it was counted.

Tags: **[P]** measured in production (aggregates of systemd and updater lines, 2026-11-13 16:39 to 2026-11-18 03:31
UTC), **[L]** measured locally on synthetic data, **[E]** estimated, **[G]** counted from git for this document.
All names in examples are invented (`CH A`, `sdr-a`, `examplesys`).

## 0. Constraints from the radio maintainers

| # | Constraint | Where it is met | Open choice left |
|---|---|---|---|
| 1 | Immediate paths stay in capture, never behind a spool poll: live MDC / FleetSync IDs and the emergency alarm (#690, #698, #767), page tones and the live page, "talking now", P25 live grants. A delivery path that works while the app restarts; under 100 ms from decode to the outbox; no duplicates when the app also sees the segment | §3.2, §4.2, §4.3, §5 | Which file holds the `urgent` table (§4.3); whether a second step lets capture send by itself (§4.3, Q1) |
| 2 | One owner of dongles: capture. Intent from the app over a versioned control channel with acks; per-channel running state back | §3.3, §4.4, §4.5 | Requests in the wish file or on the socket (Q5) |
| 3 | Spool: atomic, ordered per channel, idempotent ids, full metadata, `late`, at-least-once with dedupe, bounded with a Health alarm, survives both restarting; metrics the same way | §4.1 | What counts as late after a short app restart (Q4) |
| 4 | P25: `CallIngest` and the #747 mark live where the spool lives; one owner of the mark; watched-talkgroup lines (#718) and split counts stay with the supervisors | §4.6 | Hard link or copy of the call audio (Q8) |
| 5 | Live feeds for the station page on a low-latency local socket | §4.2 | none |
| 6 | Version skew: versioned, unknown fields ignored both ways; capture restarts only for its own files or the contract; drains on stop (#767's workers) | §3.5, §3.6, §4.7 | none |
| 7 | CPU: capture keeps Nice -5, CPUWeight 500 | §3.5 | none |
| 8 | Airband multichannel, ATC and `airport_watch` are maintained by others | §7.3 | none |
| 9 | Inline mode stays for unit tests; a synthetic end-to-end harness through the real spool and control channel, with an app restart and an emergency during it | §6 | none |
| 10 | Boundary: DSP plus segment handling up to "segment with metadata on the spool"; `App._segment` row building, STT and CAD stay in the app; the alarm lane moves rather than `app.py` handlers | §3.1, §3.2 | Page assembly side (Q6) |

## 1. Problem and numbers

| | | |
|---|---|---|
| Restarts of `sizeup-station` | 57 in 4.45 days = 12.8 a day; all automatic updates, 0 rollbacks, 0 by the health check | [P] |
| Radios back after a switch (the updater's own poll) | 8 s median, p95 11 s, max 52 s: about 7.6 min over the window | [P]; see the caveat below |
| Switched while still busy | 40 of 57 hit the 300 s cap of the quiet wait | [P] |
| Stop to start (systemd) / start to serving / start to hub link connected | 0.56 s / 3.16 s / 3.53 s median (p95 1.30 / 3.83 / 4.53 s; link max 27.9 s) | [P] |
| Of 55 updates whose commits could be compared: touched the 16-module capture closure | 7 | [L on P] |
| ... anything under `sizeup/radio/` | 11 | [L on P] |
| ... that folder, `app.py`, `stt.py` or `deploy/trunk-recorder/` | 30 | [L on P] |
| ... anything the station process imports (172 modules) or its build | 53 | [L on P] |
| P25 calls whose sidecar lands during a restart | never ingested: `CallIngest.start()` calls `prime()`, which marks every `*.json` on disk as seen (`sizeup/radio/p25.py`). About 1 to 7 calls a day | code read; rate [E] |
| The updater's "nothing the station runs changed: no restart" path (`autoupdate.py` `runtime_changes`, `NOT_RUNTIME`) | fired 0 times | [P] |

Caveats, from the code:

- The 8 s is not an audio-level gap. `Updater.update()` takes its start time before `activate()` (so the figure
  includes installer steps during which the old process still captured), and `radios_back()` polls `/api/state` once
  a second for rows whose `state` is `ok`. `NfmReceiver._run` sets `ok` right after it spawns `rtl_fm`, before the
  first byte, and a fresh `NoiseSquelch` cannot open for `LEARN_FRAMES` = 40 frames (2 s). So the true gap can be
  shorter or longer than 8 s. It was not measured.
- A restart also resets things capture learned: the live tap's history, each squelch's hiss floor, `LiveTalk`'s grant
  log (kept 30 min for finished calls), and it starts two grace periods in `app.py`: `NOAUDIO_GRACE_S` (120 s in
  which a dongle that streams nothing is not counted, #710) and `SDR_SETTLE_S` (60 s before Off dongles are assigned).
- #712 merged minutes after the window: one switch per burst of deploys, never mid-page, a 45 min ceiling. Restarts
  per day will be fewer than 12.8 from here on. How many is not measured yet. It does not stop a hub-only change
  from restarting capture.

## 2. Today's architecture

### 2.1 What runs in the one `sizeup-station` process

| Group | Threads | Where |
|---|---|---|
| Capture | `nfm-<label>` per channel (FM and AM), a stderr drain per `rtl_fm`, `sdr-watch`, `talking`, `keyup-pl`, `p25-ingest` (one per P25 system), `trunk-recorder-log`; with #767 also `segment-<label>` | `sizeup/radio/fm.py`, `am.py`, `p25.py`, `recorder.py`, `sizeup/app.py` |
| Speech-to-text | `whisper`, `stt`, `stt-catchup` | `sizeup/stt.py`, `sizeup/sttcatchup.py` |
| Hub link | `hub-forward`, `hub-state`, `hub-jobs`, `hub-stt`, `hub-logs`, `hub-metrics`, `station-metrics`, `hub-aircraft` | `sizeup/cloud/station.py`, `metrics.py` |
| CAD and web | the HTTP server, `broadcast`, `janitor`, history, sources, intel | `sizeup/web/`, `sizeup/cad/` |

Other units already split off: `sizeup-trunk-recorder` (restart signature `tr_sig`), `sizeup-whisper`, `sizeup-adsb`,
`sizeup-ais`, `sizeup-logread`, and since #709 (merged with #701) `sizeup-stt-encode` (a Unix socket in a `RuntimeDirectory`, peer uid
checked, framed JSON header + payload, a code-hash hello: the nearest precedent for §4.2).

### 2.2 The inline call graph, pipe read to outbox

Traffic channel (NFM), as on `main`. #767 moves the marked step to a per-channel worker and gives the tap
`App._mdc_at` (each ID with where its sync begins) in place of `App._mdc`:

```
rtl_fm stdout (s16le, 16 kHz)
  NfmReceiver._pump            os.read, 50 ms frames (800 samples)
    TapSquelch.feed
      NoiseSquelch.feed        -> Segment | None
      LiveTap.frame            traffic: fm_open / _mdc_step -> App._mdc -> LiveTalk.fm_ids
                                         emergency -> App._fm_emergency -> App._alarm_now -> App._emit("alarm")
                               paging:  LivePaging.feed -> on_change -> StationLink.live_changed
                                                           on_ours   -> App.sources_poll_now
    NfmReceiver._deliver       (#767: App._seg_workers[label].put; the rest on thread segment-<label>)
      App._segment
        _mdc(*bursts, audio)   whole-clip MDC-1200 + FleetSync decode
        tap.ids / tap.alarm_key / tap.burst
        _units, _id_note, speech_seconds, _metric_tx, _heard, live.fm_segment, _keyup (own pool)
        _keep_bursts, _keep_clip (stt.radio_voice + WAV), _keep_source (WAV)
        _alarm_now (if emergency) -> _emit("alarm", clip)
        _stt                   queue only
  ... Transcriber -> App._transcribed -> _emit("traffic", clip) -> StationLink.emit -> Outbox.add
```

Paging: `App._page_segment` -> `PageHold.add` (tone decode over the clip, page split, speech mask) -> `_keep_clip` ->
`_show_page`; `_page_done` when the hold window has passed (`_release_pages`, called from the `talking` thread every
0.2 s) -> `_emit("page", clip)` -> `_stt`. Airband: `CarrierSquelch._make` (AGC, tone notch; deferred to the worker
by #767) -> `App._airband_segment`. P25: `CallIngest._run` (1 s poll) -> `scan` -> `apply_grants` -> `App._call` or
`App._call_system` -> clips, `_stt`, `_alarm_now`, `_drop_recorder_files`.

### 2.3 What crosses between capture and the rest, both ways

Capture to app:

| Call or attribute | Carries | Timing |
|---|---|---|
| `on_segment(Channel, Segment)` -> `App._segment` / `_page_segment` / `_airband_segment` | the finished transmission: audio (float32, 16 kHz), `start`, `end`, `quieting_db`, `bursts`, `reason`, `cont`, `hold_until`, `breaks`, `q_open`, `q_close`, `hiss_db`, `floor_db`, `split`, `prev_start`, `first_start`; AM adds `weak`, `marginal`, `preroll_s`, `tones`, `carrier_dbfs`, `snr_db`, `opened` as plain attributes | at squelch close |
| `tap.ids(label, start, end)`, `tap.alarm_key(...)`, `tap.burst(...)` | the IDs decoded live for that carrier, the alarm already raised for it, "a burst was there but did not decode" (#163). Kept `KEEP_S` = 30 s (120 s with #767) | read by the segment handler |
| `tap.on_emergency(label, since, found)` -> `App._fm_emergency` | the emergency button at the key-up; returns the alarm's key | immediate |
| `tap.mdc` = `App._mdc` (`App._mdc_at` with #767) | the decoder the tap calls (lives in `app.py` today) | every 4th frame of the first 1.5 s |
| `receiver.workers` = `App._seg_workers[label]` (#767), `receiver.pending(label)`, `receiver.drain()` | the channel's worker set, made and owned by the `App` (`_wire_receiver`), one per label, handed to every receiver made of that channel so a dongle move keeps the order; `_release_pages` reads `pending`; `App.stop` drains | at close; at stop |
| `LiveTalk.fm_open / fm_ids / fm_close / fm_segment`, `LiveTalk.feed_line` | "talking now", analog and P25; `on_change` -> `StationLink.live_changed` and the web push | immediate |
| `LiveTalk.grants(system, tg, t0, t1)`, `LiveTalk.aliases` | live grants for a finished call's key-ups (`CallIngest.grants`, `alias_of`) | at call end |
| `LivePaging.feed / close / state`, `on_change`, `on_ours` | a page going out: pairs as they decode, then "voice" | immediate |
| `CallIngest.on_call(Call)` -> `App._call` / `_call_system` | a finished P25 call | 1 s poll + settle |
| `Runtime._line` -> `Activity.feed_line`, `reception.Probe.feed_line` | another system's grant lines: unknown and encrypted talkgroups, watched counts, reception tests | per log line |
| `receiver.status()`, `recorder.status()`, `ControlChannel.status()`, `CallIngest.status()` | rows for `App.radio_status()`: state, detail, restarts, open, `open_s`, `stuck`, `last_heard`, `quieting_db`, `hiss_db`, `floor_db`, the running squelch, `short_per_min`, airband `now` | polled (5 s status post, each `/api/state`) |
| `receiver.squelch.open / keyed / _start / continuing / quieting_db / hiss_db` | read directly by `App._watch_fm_once` (0.2 s), `App._release_pages`, `metrics.Sampler.tick` (1 s) | polled |
| `receiver.on_stall`, `on_audio` | a dongle that opens and streams nothing (#710) | from the receiver thread |
| `Collector.tx(label, kind, seg)` from the handlers; `Sampler` reading squelches | signal metrics (#605, #685): `tx`, `floor`, `cc`, `ev` records | per transmission, per minute |
| `on_event` = `app.log` | every log line of the receivers | any time |

App to capture:

| Call | From | What |
|---|---|---|
| `make_receiver(cfg, c, device, ...)` | config | channel, frequency, gain, squelch constants, key-up cuts, airband schedule and extra frequencies |
| `receiver.set_squelch(open, close, min_open)` via `App._apply_squelch` | the hub's job `set_channel_prefs(prefs, aws, tones, squelch)`; the `expect` check of #685 runs before it, in `cad/settings.py` `_squelch_op`, against the running squelch in `radio_status()` | live, no restart |
| `App._apply_named_tones` -> `TonePlan`, `LivePaging.ours` | the same job's `tones` | live |
| `App.set_mode`, `_start_mode`, `_stop_mode`, `_fresh_receiver`, `recorder.start/stop`, `SystemRecorder.give/take` | a person's pick or Off (web, or the hub's job `set_mode`), auto-assign, antenna groups | dongle moves |
| `App.set_antenna_groups`, `set_adsb_receiver` | hub jobs of the same names | persisted in `antenna_groups.json`, `adsb_receiver.json` |
| `App.reception_post`, `reception_sample_b64` | hub jobs; `reception.Tests` reaches into `app.assign`, `set_by`, `_mode_lock`, `_start_mode`, `_stop_mode`, `_save_modes` | lends a dongle |
| `App.airband_ab` | `POST /api/airband-ab` | an on-air A/B on one airband dongle |
| `channel_pref(label)["record"]` | Settings | read by the handler: a channel that is not recorded keeps nothing |
| `LivePaging.finished(label, page_id, end)` | `App._page_heard` | the live page hands over to the finished row |
| `claims.claim / release / other / holder`, `claims.helpers` | `app.py`, `reception.py` | `~/.local/state/rtl-claims`, shared with another app |

There is no job named "set squelch" or "set radio role": the hub's 15 calls are the `CALLS` tuple in
`sizeup/cloud/station.py`. No live audio is served to listeners today (`sizeup/web/server.py` serves finished clips
only); if one is added it belongs on the socket of §4.2.

State files that are capture's by content but written by `App` today: `captures/sdr_modes.json`,
`sdr_products.json`, `antenna_groups.json`, `antenna_standins.json`, `antenna_noaudio.json`, `reception_*.json`,
`state/adsb_receiver.json`, `state/p25_recorder*.json`.

## 3. The proposed split

### 3.1 The boundary in one table

| Moves to `sizeup-radio` | Stays in `sizeup-station` (the app) |
|---|---|
| `rtl_fm` / `rtl_sdr` children, `NfmReceiver`, `AmReceiver`, the channelizer, both squelches | Building rows: `Traffic`, `_units` (alias names), `_id_note` (from facts in the sidecar), `rig` |
| The live tap, the decoder it calls (`App._mdc` / `_mdc_at`, moved out of `app.py`), `LivePaging`, `LiveTalk` (raw entries) | Decorating live entries with rig, label, talkgroup name (`cad.radios.resolve`, `TalkgroupPrefs`) and pushing them to the web and the hub |
| The close-time capture work, on #767's per-channel worker: AM finish (`fm.finish`), the whole-clip ID decode, the burst facts, `speech_seconds`, the key-up link | The listening clip: `stt.radio_voice`, `_keep_clip`, the in-memory clip cache, clip serving |
| Raising the emergency alarm into the `urgent` lane (§4.3) | Taking it over: `_alarm_now`, `cad.alarms`, emergency windows, All clear |
| Writing each finished transmission to the spool | STT: `_stt`, queueing, routing, hand-off at stop, catch-up, STT input (FLAC) |
| Dongles: assignment, auto-assign, antenna groups (#609, #702), no-audio and probes and USB reset (#710), claims, the reception test's dongle moves | Page assembly: `PageHold`, `page.py`, the tone plan's names, re-evaluation (Q6) |
| The trunk-recorder side: wish files for `trunkrecd`, the log tail, `ControlChannel`, `CallIngest`, the #753 mark | `App._call`, `_call_system`: talkgroup tiers, emergency windows, stuck flags, `split_turns` |
| The metrics sampler (`floor`, `cc` records) | `Collector`, `MetricsLink`, `tx` records built from sidecars |
| | The outbox, the hub link, jobs, the CAD, the web server, the janitor for clips |

This is the "about 7 restarts" end of ADR 0002's range, not the "about 30" end. What decides it: `app.py` changed in
34 of the 106 first-parent commits on `main` in the window, `stt.py` in 4, `config.py` in 14 [G]. Anything that
imports one of those restarts with it.

### 3.2 What stays in the app, and why

| Decision | Reason |
|---|---|
| The voice filter and the listening clip stay in the app | `radio_voice` lives in `stt.py`, which the STT session changes often (#761 this week). The clip is derived data: the spool's audio is the exact input (`<id>.src.wav` today), so the app can make the clip again at any time. ADR 0002's text says "clip writing" moves; this design moves the raw audio write and keeps the filtered clip, at the maintainers' preference (constraint 10) |
| STT queueing stays | It needs the channel prefs, the AWS state, the pool, the term lists: all app state. #712's hand-off at stop stays as it is |
| Row building stays | It reads the CAD (`radios.rig_mdc`, aliases, tiers). Capture ships facts; names are joined in the app |
| The whole-clip ID decode moves | It is the only way an emergency sent at release, or missed live, alarms while the app is down. `mdc1200.py` and `fleetsync.py` are already in capture's closure. It is also most of the handler's CPU (32.7 of 43.1 ms for a 10 s clip [L]), which then never competes with the app |
| The alarm lane moves instead of handlers | Constraint 10. `_fm_emergency` needs only the IDs, the channel and the alias map; the hub works the rig out itself (`hub._traffic_of`) |

Consequence, stated plainly: while the app is down nothing is transcribed, no clip is filtered and nothing but the
`urgent` lane reaches the hub. Segments wait in the spool and are handled, marked late, when the app is back.

### 3.3 Modules and the import closure

| Module | Today | Proposal |
|---|---|---|
| `sizeup/radio/fm.py`, `am.py`, `amtones.py`, `channelize.py`, `tones.py`, `mdc1200.py`, `fleetsync.py`, `tap.py`, `sdr.py`, `claims.py`, `usbreset.py`, `sizeup/procenv.py`, `sizeup/tz.py`, `sizeup/atc_hours.py` | imported by `app.py` | capture's closure. The app still imports some for its own use (`fm.RATE`, `ctcss_estimate` for the key-up diagnostic, `tones.py` for `page.py`) |
| `sizeup/radio/live.py`, `livepage.py` | imported by `app.py`; not in the evidence's 16-module list, though the tap is useless without them | capture's closure |
| `sizeup/radio/groups.py`, `recorder.py`, `trunkrecd.py` (constants and `read_status`), `p25.py` (`CallIngest`, `parse_call`, `apply_grants`) | imported by `app.py` | capture's closure. `split_call` and `Piece` stay importable by the app |
| `sizeup/radio/reception.py`, `probes.py`, `systems.py` | `reception.Tests` takes the `App` and drives its dongle state | capture's closure for the dongle moves (Q7). `systems.py` splits: `load`, `discover`, `P25System` are config; `Activity` and tier logic need `TalkgroupPrefs` and stay in the app, so `sourcekeys.py` and `tiers.py` stay out of the closure |
| `sizeup/radio/page.py` | imported by `app.py`; in the evidence's 16 | **not** in capture's closure: `PageHold` runs in the app (Q6) |
| `App._mdc`, `_mdc_at` (#767), `_fm_emergency`, `_via`, `keyup_link`, `make_receiver`, `_wire_receiver` (#767), `_heard_elsewhere`, `_watch_fm_once` | `sizeup/app.py` | new `sizeup/radio/ids.py` (the decode), `radio/segmeta.py` (the sidecar's facts), `radio/radiod.py` (the service) |
| The dongle half of `App` (about 1,500 lines: `_load_modes` to `_set_group_mode`, `sdr()`, `_start_mode`, `_stop_mode`, the no-audio and probe methods, `set_antenna_groups`, `set_adsb_receiver`) | methods of `App` | new `sizeup/radio/dongles.py`, class `Dongles`, used in-process in inline mode and by `radiod.py` in service mode |
| New | | `radio/spool.py` (writer and reader), `radio/control.py` (wish, status, requests), `radio/liveout.py` (the socket), `radio/urgent.py` |
| `metrics.Sampler` | `sizeup/cloud/metrics.py` | the sampling half moves to `radio/`; `Collector` and `MetricsLink` stay |

Capture must never import `sizeup.config`, `sizeup.app`, `sizeup.stt`, `sizeup.cad.*`, `sizeup.cloud.*` or
`sizeup.channels`. Everything it needs from the config arrives in the wish file (§4.4). Two helpers it uses live in
`config.py` today (`airband_owner`, `P25_JOB`) and move to a small module.

How the closure is kept small:

| Guard | What it does |
|---|---|
| `sizeup/radio/radiod.closure` | the list of files, in the repo |
| A test that starts `python -m sizeup.radio.radiod --check` in a subprocess | fails when a `sizeup.*` module outside the list is in `sys.modules`, the same way the evidence's `station_modules.txt` was made |
| The service runs from its own build directory (§3.5) holding only the listed files | an import outside the list fails the installer's preflight instead of silently widening the restart set |
| No lazy imports in the service | a release pruned from disk can never break a running capture |

How often would it have restarted? A proxy, because the evidence's 55 update pairs were not kept: first-parent
commits on `main` in the same window, 106 of them [G]. The proxy agrees with the evidence where both exist (16-module
closure: 6 commits against 7 updates; `sizeup/radio/`: 11 against 11).

| Closure | Commits touching it, of 106 [G] |
|---|---|
| The evidence's 16 modules | 6 |
| DSP + tap + `live.py` + `livepage.py`, without `page.py` | 7 |
| ... + `usbreset.py`, `groups.py`, `recorder.py`, `trunkrecd.py`, `p25.py` | 11 |
| ... + `reception.py`, `probes.py`, `systems.py` (**the proposal**) | 12 |
| ... if `systems.py` is not split, so `sourcekeys.py` and `tiers.py` come too | 15 |
| The proposal without importing `trunkrecd.py` | 11 |
| `sizeup/app.py` alone | 34 |

So constraint 2 (capture owns the dongles and the reception test's moves) costs about 5 more restarts in the window
than the bare DSP, and the proposal lands at about 12 where the station restarted 55 times [E from G]. Add the
dongle code that leaves `app.py` (it changed with #609 in the window; #702 and #710 merged within an hour after it)
and contract bumps. `trunkrecd.py` is the largest single contributor (5 commits, 1 of them not shared with another
listed file); importing only a small constants module from it would remove that.

### 3.4 What runs where after the split

| Thread or process | Unit |
|---|---|
| `rtl_fm`, `rtl_sdr`, the channelizer; `nfm-<label>`, `segment-<label>`, `sdr-watch`, `talking`, `p25-ingest`, `trunk-recorder-log`, `radio-metrics`; new `urgent`, `live-out`, `control` | `sizeup-radio` |
| `spool-reader` (new), `segment-<label>` again for the handlers (§3.6), `keyup-pl`, everything else in §2.1 | `sizeup-station` |
| trunk-recorder and its supervisor, unchanged | `sizeup-trunk-recorder` |

### 3.5 The unit, its signature, start order, health

`deploy/station/systemd/sizeup-radio.service`, modelled on `sizeup-trunk-recorder.service`:

| Setting | Value | Why |
|---|---|---|
| `User`, `Group`, `SupplementaryGroups` | `sizeup`, `sizeup`, `plugdev` | as the station |
| `Environment` | `HOME=/var/lib/sizeup`, `SIZEUP_STATE_DIR=/var/lib/sizeup`, the `rtl-sdr` `PATH` and `LD_LIBRARY_PATH` | the claims directory follows `HOME`; no hub secrets, no `station.env` |
| `ExecStart` | `/usr/local/lib/sizeup/start-radio`: resolves `/opt/sizeup/radio` (a link to a build directory), runs its venv's `python -m sizeup.radio.radiod` | the code under a running capture never changes; it is independent of `/opt/sizeup/current` |
| `RuntimeDirectory` | `sizeup-radio`, mode 0750 | `status.json`, `live.sock` |
| `Nice`, `CPUWeight`, `IOWeight` | -5, 500, 500 | constraint 7: above whisper (100), the updater (20) and the STT encoder (10) |
| `KillMode`, `TimeoutStopSec` | `control-group`, 20 s | `rtl_fm` is its child. On SIGTERM: stop receivers, flush open transmissions (`reason: "stopped"`), `drain()` the workers (#767, 10 s at most), write a final status |
| `Restart`, `RestartSec` | `always`, 2 s | as today's backoff start |
| `RestartPreventExitStatus` | 78 | capture exits 78 when the current release does not ask for it (§4.7), so a rollback to a release from before the split does not leave two owners of the dongles |

`sizeup-station.service` gains `Wants=sizeup-radio.service` and keeps `KillMode=control-group`; in service mode it
has no `rtl_fm` children.

Restart signature, in `install-linux.sh` beside `tr_sig`:

```
radio_sig = sha256( the files in radiod.closure, in list order
                    + sizeup-radio.service + start-radio
                    + readlink -f /opt/sizeup/rtl-sdr
                    + the numpy version of the release venv
                    + the contract's major version )            first 16 hex
```

The build directory is `/opt/sizeup/builds/radio-<radio_sig>/` (the listed files and a `cp -al` venv), made at stage
time when the signature is new and named in the release's `BUILDS` file as `radio=<dir>`, like `trunk-recorder=`.
`activate()` moves the `/opt/sizeup/radio` link and restarts `sizeup-radio` only when the signature differs from the
running one. Unlike `tr_sig` it hashes no generated file, so a new writer cannot cause a restart by re-serialising.

| | |
|---|---|
| Start order at boot | `sizeup-usbfs`, `sizeup-trunk-recorder`, `sizeup-radio`, then `sizeup-station`. None waits for another: capture starts from the last wish file on disk; with none it idles |
| Start order at an update | `trunk_recorder_service`, then `sizeup-radio` if its signature changed, then `sizeup-station`. Capture first, so the new app finds the new contract |
| Health | `status.json` carries `at`; a reader treats one older than 15 s as down (`trunkrecd.read_status`'s rule). `health-check.sh` restarts `sizeup-radio` after 3 stale looks, as it restarts the station after 3 failed `/healthz` |
| The updater's `radios_back()` | unchanged: it reads the app's `/api/state`, which mirrors capture's rows. After an app-only update the rows never leave `ok` |
| `station_hold()` (`stationmode.py`) | for an app-only update, "carrier open", "talking" and "page on" stop being reasons to wait: the restart no longer cuts them. The STT and emergency-window holds stay. A later PR, with the updater's owner |
| `sizeup-station sdr-serial` | `sdrserial.SERVICES` gains `sizeup-radio.service`; it rewrites `sdr_modes.json` and `antenna_groups.json` with capture stopped, as it does with the station stopped today |
| Logs | `SyslogIdentifier=sizeup-radio`. `sizeup-logread` already reads `sizeup-*`. Readers that match text by unit must be checked (Q11) |

### 3.6 #767's segment workers after the split

In #767 the worker sets are the `App`'s: `App._seg_workers` holds one `SegmentWorkers` per channel label, made in
`App._wire_receiver` with `App._segment` as its handler and handed to every receiver the `App` makes of that channel
(`r.workers`). A receiver replaced at a dongle move (`_fresh_receiver`) therefore queues behind the one it replaces:
in order, never two at once. Inside a set there is one queue and one `segment-<label>` thread per channel label (an
airband dongle's channels each get their own). `App.stop()` stops the receivers, then `drain()`s each receiver (its
reader ends and its last flush is queued) and each worker set, 10 s for all of them, closes the sets, and only then
finalises the held pages.

The split keeps that class and that ownership rule (the long-lived owner of the receivers owns the workers) and uses
it twice, once on each side:

| | Capture's workers | The app's workers |
|---|---|---|
| Owner | the object that makes and replaces receivers in `sizeup-radio` (`Dongles`, taking over `_wire_receiver`); one set per label, shared across receiver instances, as in #767 | the `App`, as in #767: `App._seg_workers`, one set per label |
| Fed by | the receiver's reader: `NfmReceiver._deliver` -> `put` | `spool-reader`: `put(channel, segment read from the spool)`, in `seq` order per channel |
| Handler | publish: `fm.finish` (the AM AGC and notch), the whole-clip ID decode and the sidecar's facts, the `urgent` row if an emergency was not raised live, `LiveTalk.fm_segment`, then the spool write | `App._segment` / `_page_segment` / `_airband_segment`, unchanged in what they call after the facts |
| Full queue (64) | the reader waits, as in #767: never a drop | `spool-reader` waits; the items simply stay in the spool |
| Behind (4 waiting) | logged, as in #767 | logged; also visible as `spool.oldest_s` |
| `pending(label)` for `_release_pages` | reported: `channels[].queued` in the status and `queued` in the `carrier` frame (a continuation closed but not yet published) | the app's own `pending(label)`, plus capture's `queued`, plus `continuing` from the `carrier` frame |
| Drain on stop | on SIGTERM: stop the receivers, `drain()` each receiver and each set (10 s for all, inside `TimeoutStopSec` 20 s), close them. Drained means **every transmission that ended or was flushed is a complete spool item**. Then the final status and the claims released. What does not finish in time is logged and lost, as in #767 today | in `App.stop()`: stop `spool-reader`, `drain()` each set (10 s for all), write the cursor, then finalise the held pages, then `_stt_handoff`. Drained means **handled and acknowledged**. What does not finish stays unacknowledged in the spool and comes again at the next start: nothing is lost on this side |
| Inline mode | does not exist: #767 as merged, one stage, handler `App._segment` | the same sets |

Consequences:

- An app restart no longer needs the receivers to drain at all. The 10 s in `App.stop()` then covers only handlers
  already running.
- Order per channel holds end to end: capture's worker publishes in close order, `seq` rises in that order, and the
  app's worker for that label takes items in `seq` order.
- `tap.ids()`, `alarm_key()` and `burst()` are read by capture's handler, in the same process as the tap. #767's
  `KEEP_S` = 120 s then only has to cover capture's own queue, not the app's.

## 4. The contract

One rule for every file and message (ADR 0004): a `v` field; unknown keys are ignored by every reader and kept out
of the way of every writer; a missing optional key has a stated default; no integer above 2^53 (the nanosecond `seq`
of `p25_recorder.json` is not repeated); times are float seconds; a field's unit is in its name. A change that only
adds optional keys does not bump `v`. A bump restarts both sides.

Paths (station defaults; every path capture uses is given to it in the wish file):

| Path | Writer | Readers |
|---|---|---|
| `/var/lib/sizeup/state/radio_wish.json` | app | capture |
| `/run/sizeup-radio/status.json` | capture | app, the CLI, `health-check` |
| `/run/sizeup-radio/live.sock` | capture (listens) | app, tools |
| `/var/lib/sizeup/spool/` | capture (items), reader (its cursor file only) | app, the compare tool |
| table `urgent` in `/var/lib/sizeup/state/outbox.sqlite` | capture | app |

### 4.1 The spool

Layout. The spool is on the same filesystem as `captures/`, so audio moves by hard link, never by copy.

```
spool/
  EPOCH                         16 hex, made with the directory (the outbox's idea): a new spool resets cursors
  SEQ                           capture's last sequence number
  tmp/                          writes in progress; wiped by capture at start
  items/
    000000012345-CHA-1790000000123.wav         audio, renamed first
    000000012345-CHA-1790000000123.mdc.wav     the cut-off ID bursts, optional
    000000012345-CHA-1790000000123.json        the sidecar, renamed LAST: it is the completion marker
  cursor.app.json               the reader's, written by the reader only
```

| Rule | |
|---|---|
| Atomic write | every file is written under `tmp/` and renamed into `items/`. Audio first, sidecar last. A reader lists `*.json` only. Audio with no sidecar is debris from a crash; capture removes it at start |
| No `fsync` by default | like every other station file today. The sidecar carries `audio.bytes`; after a power cut a short or empty file is detected, logged and skipped, never parsed as audio. `fsync` per item is a setting; its cost on the station's disk is not measured (Q9) |
| Ids | `id` is today's: `<label without spaces>-<int(start * 1000)>` for a transmission (`App._segment`), trunk-recorder's file stem for a P25 call (`Call.id`). It matches `CLIP_ID` in `app.py`. The same transmission never gets two ids |
| Sequence | `seq` is one counter for the spool, taken under a lock at the sidecar's rename, persisted in `SEQ` (tmp + rename) and recovered as `max(SEQ, highest in items/) + 1` |
| Ordering | per channel, `seq` rises in the order transmissions ended (the order of #767's worker). Nothing is promised across channels |
| Delivery | at least once. The reader dedupes on `id` |

Sidecar, `kind: "segment"` (FM, paging, airband):

| Field | Type, unit | Meaning |
|---|---|---|
| `v` | int | 1 |
| `kind` | string | `segment` \| `call` \| `metrics`; an unknown kind is acknowledged and skipped |
| `id`, `seq`, `epoch` | string, int, string | above |
| `channel` | `{label, kind, mhz, mod}` | `kind`: `traffic` \| `paging` \| `airband`; `mhz` as heard (4 decimals; airband: the frequency of this transmission, #260); `mod`: `nfm` \| `am` |
| `sdr` | string | the dongle's serial |
| `start`, `end` | float, epoch s | wall clock (`CLOCK_REALTIME`), stamped by the squelch from frame arrival, as `Segment.start` / `end` today |
| `closed_mono`, `published_mono` | float s | `CLOCK_MONOTONIC` at squelch close and at the sidecar's rename, for the latency figures of §5; comparable only within one boot (`boot` holds the kernel's boot id) |
| `published` | float, epoch s | when the sidecar was renamed |
| `attended` | bool | a reader held the live socket at that moment |
| `late` | bool | capture itself knows it was not live (a P25 call replayed from the mark, §4.6). Default false |
| `reason` | string, open set | `quiet`, `max length`, `steady`, `stall`, `stopped`, `rtl_fm ended`, `key-up`, ... as `Segment.reason` |
| `partial` | bool | cut by something other than the transmission ending: `stall`, `stopped`, `rtl_fm ended` |
| `cont`, `hold_until`, `breaks` | bool, float epoch s, `[[s, s]]` | paging continuation, as `Segment` (#61) |
| `keyup` | `{split, first_start, prev_start, parent, prev}` or null | a piece cut at a key-up (#645): `split` `""` \| `gap` \| `id` \| `level`; `parent` and `prev` are the ids `app.keyup_link` builds today |
| `levels` | object | FM: `quieting_db`, `q_open`, `q_close` (dB of quieting), `hiss_db` (relative dB), `floor_db` (a background carrier) or null. AM: `quieting_db` (carrier over the floor, dB), `carrier_dbfs`, `floor_dbfs`, `snr_db`, `opened` (`carrier` \| `sustained`), `weak`, `marginal`, `preroll_s`, `notched_hz` (list of Hz) |
| `speech_s` | float s | `fm.speech_seconds(audio)` |
| `ids` | list of `{id, system, emergency, from}` | radio IDs, in today's merge order: the live tap's first (`from: "live"` or `"lookback"`), then the segment's own (`"burst"`, `"audio"`). `system`: `mdc` \| `fleetsync` |
| `id_burst_seen`, `mdc_crc_failed`, `bursts` | bool, int, int | the facts `App._id_note` reads: a burst in the key-up window or at either edge, MDC sync hits that failed the CRC, bursts cut off the edges |
| `alarm_key` | string | the key of the alarm capture raised for this carrier (§4.3); `""` for none |
| `tones_live` | list of `{a_hz, b_hz, at}` | paging: the pairs the live decode showed, without names. Informative: the page's own tone decode stays `page.decode_tones` over the audio |
| `audio` | object or null | `{file, format: "wav", encoding: "s16le", rate_hz: 16000, channels: 1, samples, bytes}`; the float audio clipped to +-1 and scaled by 32767, exactly `<id>.src.wav` today. null when the wish says the channel is not recorded: the sidecar still carries IDs, levels and the alarm |
| `bursts_audio` | string or null | file name of the `.mdc.wav` |
| `capture` | `{impl, build, contract}` | `python` or a native daemon's name; for the compare tool |

`kind: "call"` is §4.6. `kind: "metrics"` is one item a minute with no audio: `records`, a list of the `floor` and
`cc` records of `sizeup/cloud/metrics.py` as they are today. The app hands them to its `Collector`; the `tx` record
it builds itself from each segment's `levels`. Metrics then survive an app restart, which they do not today.

The reader's cursor, `cursor.app.json`:

| Field | Meaning |
|---|---|
| `v`, `epoch` | 1; the spool's `EPOCH` it belongs to. Another epoch: start from 0 |
| `seq` | every item with a `seq` at or below this is done |
| `done` | up to 256 higher `seq` values finished out of order (channels are handled in parallel) |
| `at` | when written |

Written by the reader with tmp + rename, at most once a second and at stop, and only after a handler returned (the
rule of #753's mark). "Done" means the row exists and the reader holds its own hard link to the audio
(`clips/<id>.src.wav`).

| Topic | Rule |
|---|---|
| Who deletes | capture only. It removes items at or below the cursor on its next look (it is told on the socket, and looks every 5 s anyway). The reader never deletes in the spool. This ends today's three-way deleting under the P25 tree |
| Bounds | unacknowledged items older than `spool.max_age_h` (default 12, the clips' own `audio_keep_hours`) or beyond `spool.max_bytes` (default 2 GB) lose their audio first, oldest first, never an item with an emergency ID; the sidecar is rewritten with `audio: null, audio_dropped: true`. Sidecars go only past `spool.max_items` (default 50,000). Both are counted in the status |
| Disk full | a failed write is counted (`spool.dropped`) and logged once a minute; capture keeps running, and the live and `urgent` paths do not depend on the spool |
| Health | the status carries `spool: {pending, oldest_s, bytes, dropped, dropped_audio}`; the app's health report adds: warning when `oldest_s` is over 60 with a reader attached, critical with no reader for 5 min or `bytes` over 80 % of the bound |
| Capture crashes | the transmission that was open is lost, as today. `tmp/` is wiped, `SEQ` recovered. A clean stop flushes and drains first |
| The app crashes | at start it reads the cursor and lists `items/` above it. An item handled before the crash but not yet in the cursor comes again and is dropped by `id` (as `App._late_dup` in #753 does for P25) |
| Both restart | the two rules above, independently |
| Late | the reader marks a row `late` when the sidecar says `late`, or `attended` is false, or the item is older than `late_after_s` at hand-off (default 30 s: a reader that was up but behind). A late row follows #753's semantics: an emergency still alarms, labelled with its own time; nothing else that alerts fires (`_atc`, `Comms.on_text`, intel) |
| Replay bound | everything unacknowledged is ingested, however old: unlike the P25 directory scan, the cursor says exactly what was done. Whether an old emergency still pushes is the alarm code's own age rule |
| No reader, but the app is up | would make every row late and silence pre-alerts: capture's status shows `reader: {attached, since}`, and the app raises a critical Health item when it is itself not attached |

### 4.2 The live event stream

A Unix stream socket, `/run/sizeup-radio/live.sock`, mode 0660 in a 0750 directory, peer uid checked
(`SO_PEERCRED`). Framing is #709's: `>II` header length and payload length, a JSON header, raw payload bytes (unused
in v1; there for audio frames if a live listen is ever built).

| Why this transport | |
|---|---|
| Stream, not datagram | a connection tells capture that a reader is attached (the `attended` fact), a new client gets a snapshot, and order holds |
| Not a file tail | a poll is exactly what constraint 1 rules out, and it would wear the disk |
| Never blocks capture | the reader thread only appends to an in-memory queue. One sender thread writes non-blocking; a client 256 frames behind is disconnected and gets a snapshot when it comes back |
| Nothing is queued for an absent client | live state is latest-wins, as `/api/station/state` is. What must not be lost is in the spool or the `urgent` table |

Capture to client (every header has `v`, `t`, `at`):

| `t` | Fields | From today's |
|---|---|---|
| `hello` | `contract`, `impl`, `build`, `pid`, `started`, `epoch` | - |
| `snapshot` | `carriers`, `talking`, `paging`, `status_at`, `spool_seq` | first frame after `hello`, and on request |
| `carrier` | `ch`, `state` (`up` \| `keyed` \| `down`), `since`, `continuing`, `holding`, `quieting_db`, `queued` | squelch open; `NoiseSquelch.keyed` (held `min_open_s`: only then is it "talking"); close. `continuing` and `queued` (closed, waiting for capture's worker) are what `App._release_pages` reads (§3.6) |
| `ids` | `ch`, `since`, `ids` | `LiveTalk.fm_ids` from the tap |
| `alarm` | `key`, `ch`, `since`, `ids`, `via`, `n`, `durable` | `tap.on_emergency`; `n` is its row in `urgent`, `durable: false` if that insert failed |
| `talking` | `entries`: `key`, `system`, `channel` or `tg`, `radio_id`, `alias`, `since`, `talk_since`, `ended`, `until`, `id_on_release` | `LiveTalk.state()` without `rig`, `label`, `main`: the app adds those. The whole list, latest wins |
| `paging` | `entries`: `key`, `channel`, `started`, `pairs` (`a`, `b`, `names`, `ours`), `phase`, `ours`, `ended`, `until`, `page_id` | `LivePaging.state()` |
| `ours` | `ch`, `pair` | `LivePaging.on_ours` (the app calls `sources_poll_now`) |
| `grant` | `system`, `tg`, `src`, `what` | a unit event of another P25 system, for the app's `Activity` |
| `spool` | `seq`, `id`, `kind`, `ch` | an item is complete |
| `status` | `at` | the status file changed (a request's result is there) |
| `level` | `ch`, `open`, `quieting_db`, `hiss_db` | once a second per channel, only to a client that subscribed |

Client to capture: `hello` (`role`: `reader` or `viewer`, `contract`, `subscribe`), `poke` (`what: "wish"`),
`cursor` (the cursor file moved), `paging_finished` (`ch`, `page_id`, `end`: today's `LivePaging.finished`; without
it the live page ends on its own hold timeout, as it does today when nothing finishes it).

When the app is down capture keeps decoding, keeps its live state and drops frames nobody reads. The hub then sees
no "talking" and no live page until the app is back and has read the snapshot: about 4 s at the median [P]. Today it
sees nothing for the whole restart and the audio is gone too.

### 4.3 The alarm lane: `urgent`

**Recommendation: capture writes the alarm into a table of the station's outbox database, and the app moves it into
the hub lane. Capture does not get its own hub sender in this step.**

```sql
CREATE TABLE IF NOT EXISTS urgent (
  n    INTEGER PRIMARY KEY AUTOINCREMENT,
  at   REAL NOT NULL,          -- wall clock at insert
  kind TEXT NOT NULL,          -- 'alarm' in v1; a reader leaves an unknown kind in place
  key  TEXT NOT NULL UNIQUE,   -- the event's id; INSERT OR IGNORE makes a repeat harmless
  data TEXT NOT NULL           -- JSON: the data of the hub's existing 'alarm' event
);
```

| Rule | |
|---|---|
| One writer per table | capture is the only writer of `urgent`. The app is the only writer of `outbox`, `stt_inputs`, `meta` and #762's `quarantine` |
| The move | one transaction on the app's connection: copy the rows above its cursor into `outbox` (they get a `seq` like any `_emit`) and store the cursor as `meta.urgent_n`. Capture prunes its own rows at or below `meta.urgent_n` once they are an hour old |
| Not forwarding (all-in-one, or the hub link switched off) | the app reads the same rows and calls `cad.alarms.on_traffic` instead, as `App._alarm_now` branches today |
| Wake | the `alarm` frame on the socket; the app also looks every second |
| Capture's connection | WAL, `synchronous = NORMAL` as the outbox, `busy_timeout` 250 ms, `wal_autocheckpoint = 0` (capture never pays for a checkpoint of the app's clip pages). Inserts run on their own `urgent` thread: the pipe reader only queues |
| Event data | `id` = `key`, `source` (the channel), `start`, `end`, `radio_id`, `unit` (the alias from the wish file's map), `emergency: true`, `emergency_via` (`MDC` \| `FleetSync`). No `rig`: the hub derives it. The same fields `_fm_emergency` builds today |

Why not a sender in capture now:

| | Table in the outbox (recommended) | Own hub sender |
|---|---|---|
| Durable in under 100 ms | yes (§5) | yes, if it also writes a table |
| Sent while the app is down | **no**: `hub-forward` is a thread of the app. The alarm waits for it: 0.56 s + 3.53 s at the median, p95 1.30 + 4.53 s, worst seen 27.9 s to connect [P] | yes |
| Hub changes | none: an `alarm` event in the same cursor | a new route. `POST /api/station/events` keeps one `(epoch, seq)` cursor (`hub._events`); a second sender with its own epoch would reset it on every post |
| Secrets in capture | none | the station token and the Access service token, in the process that parses radio input |
| What a native daemon must implement | one SQL insert | TLS, the hub's auth, backoff, the adopted station id |
| Capture's closure | unchanged | plus `cloud/client.py` |

The gap that remains is real and should be decided on a number, not here: an emergency keyed in the roughly 4 s
after the updater judged the station quiet waits those seconds. Today the same key-up is not decoded at all.
`station_hold` already keeps the updater from switching during an open carrier, an emergency window or an emergency
event of the last 5 min. Step 8 of §6 measures how many `urgent` rows were inserted with no forwarder running. If
that is not acceptable, the smaller of two follow-ups is the own sender for this one lane, on a new idempotent hub
route (alarms are already keyed by `traffic_id` on the hub); the larger is moving `hub-forward` itself to the
long-lived side, which needs the outbox's in-memory byte counters reworked for two processes (Q1).

Which file: the maintainers' wording is the shared outbox file, and that is the default. The risk is the write lock:
the app's `Outbox.add` holds it for a whole clip (96 KB: p95 9.1 ms, p99 132 ms; a 480 KB page: p95 91 ms, p99 430
ms [L, a loaded laptop]) and an insert that meets one waits. The path is a wish-file field (`urgent_db`), so the
same table in a file of its own is a setting, not a contract change. Step 8 measures both.

No duplicates:

| Case | Why it is one alarm |
|---|---|
| The key-up alarm, then the finished transmission | the key is `<label>-live-<int(carrier start * 1000)>` (today's). The sidecar carries it as `alarm_key`; `App._segment` sets `t.alarm_key` and sends its `alarm` event with the clip; `alarms._on_traffic` finds the alarm by that key and the transmission takes it over. Today's rule, unchanged |
| An emergency found only in the whole-clip decode (sent at release, or missed live) | capture raises it at close with the transmission's own `id` as the key. The app's later `alarm` event for that `id` updates the same alarm (`traffic_id`) |
| The app is down at the close | the row waits in `urgent`; the segment waits in the spool; the two cases above then run in order |
| Capture restarts, or inserts twice | `key` is unique |
| The app moves a row, then crashes | the move and its cursor are one transaction |
| The outbox resends | the hub's `(epoch, seq)` cursor, then `traffic_id` |
| A channel that is not recorded | no audio in the spool; the alarm still goes, as `_fm_emergency` does today |

A live page of ours is not an outbox event today: it reaches the hub only in `/api/station/state` (`paging`), and
`on_ours` only triggers a poll of the station's own feed adapters. `urgent` can carry it (`kind: "paging"`), but the
hub has no such event type. Left open (Q2). P25 emergencies are not in this lane either (§4.6).

### 4.4 Control: the wish file

`state/radio_wish.json`, written by the app with tmp + rename whenever it changes, and read by capture on a `poke`
and every 0.5 s. It is the whole desired state, so a capture that starts alone runs what the last app asked for.

| Field | Type | Meaning |
|---|---|---|
| `v`, `seq`, `at`, `pid`, `build` | int, int, float, int, string | `seq` is a counter the app keeps in the file itself (previous + 1) |
| `mode` | `service` \| `shadow` | §6 |
| `paths` | object | `spool`, `urgent_db`, `clips`, `captures`, `state`, `rtl_fm`, `rtl_sdr`, `recorder`, `recorder_dir` |
| `channels` | list | per `[[radio.channels]]` entry: `label`, `kind`, `mhz`, `gain_db`, `device`, `open_db`, `drop_db`, `hang_s`, `continue_s`, `keyups`, `split_s`, `jump_db`, `schedule`, `also`, `record` (bool) |
| `airband` | object | `multichannel`; the owner rule for a frequency two dongles hear (`config.airband_owner` today) |
| `squelch` | `{label: {open_db, close_db, min_open_s}}` | the overrides, already fitted by `channels.fit_squelch`. Applied live (`set_squelch`), no restart |
| `tones` | `{plan: [{a_hz, b_hz, names}], ours: [name]}` | for `LivePaging` only |
| `aliases` | `{radio id: name}` | for the alarm's `unit` |
| `dongles` | object | `listed`, `auto`, `auto_assign`, `order`, `ignore`, `reserved` (`adsb`, `ais`), `start_mode` |
| `p25` | object | the county recorder and each system's directory, name, label, sources and jobs; talkgroup labels and unit aliases for `parse_call` |
| `metrics` | bool | sample `floor` and `cc` records |
| `spool`, `late_after_s` | object, float | the bounds of §4.1 |
| `requests` | list | below |

A wish file that is missing, unreadable or not an object: capture keeps running what it last accepted (it keeps a
copy, `radio_wish.applied.json`), says so in the status (`wish_problem`), and never reads a broken file as
"everything off", which is what `trunkrecd.wanted` does with its own. With no accepted wish at all it idles.

Requests are the intents that can be refused. Each is `{id, op, args, by, at, expires, expect}`. Capture applies each
`id` once (it remembers the last 256), refuses one past `expires` (a click answered late is not applied late) and
answers in the status file's `results`.

| `op` | `args` | Today's call | Typical refusal |
|---|---|---|---|
| `set_mode` | `sdr`, `job` (a channel, the P25 job, a system job, `Off`), `confirm` | `App.set_mode` | held by another app; `confirm: ...` (a fire job, a group rule) |
| `set_groups` | `groups`, `confirm` | `App.set_antenna_groups`, parks included | invalid; `confirm: ...` |
| `set_adsb` | `serial` or null | `App.set_adsb_receiver` | held; not plugged in |
| `reception` | the body of `reception_post` | `App.reception_post` | a test is running; no dongle to lend |
| `airband_ab` | `label`, `minutes`, `phase_s` | `App.airband_ab` | not an airband channel; not running |
| `squelch` | `label`, `open_db`, `close_db`, `min_open_s`, `expect` | optional hardening of #685 | `stale`: the running squelch is not `expect` |

The `expect` check of #685 stays where it is (the hub, against the running squelch the station reports), so it is
still refused when stale as long as the status carries the running values, which it does (§4.5). The `squelch`
request adds a second check at the point of application; it is optional.

Capture owns and persists, in today's files and formats: `sdr_modes.json`, `sdr_products.json`,
`antenna_groups.json`, `antenna_standins.json`, `antenna_noaudio.json`, `adsb_receiver.json`, `reception_*.json`,
`p25_recorder*.json`. Keeping the formats is what makes a switch back to inline mode safe.

### 4.5 Control: the status file

`/run/sizeup-radio/status.json`, compact JSON, tmp + rename, every 0.5 s and at once after a request.

| Field | Meaning |
|---|---|
| `v`, `at`, `pid`, `impl`, `build`, `contract`, `started` | `at` older than 15 s: the reader takes capture as down and adds `stale: true` |
| `wish_seq`, `wish_problem` | the wish it runs; why a newer one was not accepted |
| `reader` | `{attached, since}` |
| `channels` | per channel: `label`, `kind`, `mhz`, `sdr`, `run`, `state`, `detail`, `restarts`, `open`, `open_s`, `stuck`, `last_heard`, `queued`, `quieting_db`, `hiss_db`, `floor_db`, `squelch` (`open_db`, `close_db`, `min_open_s` as running), `short_per_min`, `now` (airband), `ab` |
| `channels[].run` | `running` \| `waiting` (no dongle) \| `out` (a dongle streaming nothing, #710) \| `held` (another app) \| `shed` (to a higher job of its group) \| `not_in_set` \| `local` \| `off`: the words of `docs/nodes.md` (#758), which reports them as a node's capability |
| `sdrs` | per dongle, the rows of `App.sdr()`: `serial`, `name`, `product`, `present`, `job`, `set_by`, `held_by` (`app`, `mode`), `noaudio`, `ambiguous`, `lent`, the group fields |
| `groups`, `groups_problem`, `no_park`, `duplicates`, `adsb` | as `App.sdr()` |
| `p25` | per system: `state`, `detail`, `cc` (`ControlChannel.status()`), `last_call`, `mark` |
| `reception` | `Tests.state()` |
| `spool`, `urgent` | `{epoch, seq, pending, oldest_s, bytes, dropped, dropped_audio}`; `{last_n, queued, failed}` |
| `results` | `{request id: {ok, error, at}}`, kept 10 min |

`App.radio_status()`, `App.sdr()`, `squelch_state()`, `station_metrics()` and the status post to the hub are then
built from this file plus the app's own rows (STT, voice, hub). Their shapes do not change.

### 4.6 P25: the existing hand-off on the same contract

trunk-recorder and `trunkrecd.py` stay as they are, in their own unit. What changes is who stands on the other side:

| Piece | Today | Proposal |
|---|---|---|
| The wish files `p25_recorder*.json`, reading `status*.json` | the app (`UnitRecorder`, `SystemUnitRecorder`) | capture, as the owner of the dongles. Formats unchanged |
| The log tail (`UnitRecorder._tail`), `LiveTalk.feed_line`, `ControlChannel` | the app | capture: live grants are an immediate path. Its grant log (30 min) then survives app restarts |
| `CallIngest` (poll, settle, `parse_call`, `apply_grants`) | the app | capture. Each call it takes becomes a spool item |
| The replay mark, as proposed in #753 (`p25_ingest_mark.json`, one per capture dir) | the app | **capture owns it and is its only writer.** It covers the time capture itself was down, about a quarter as often as today (§3.3). Calls it replays carry `late: true`; grants are not applied to them, as in #753. The time the app was down is covered by the spool cursor instead |
| `App._call`, `_call_system`, `_drop_recorder_files` | the app | the app, fed from the spool. It no longer deletes under the capture dir |
| Watched-talkgroup lines (#718), split counts (the #647 patch; #770 open) | `trunkrecd.py` (`Shipper`, `Watchdog`) | unchanged: the `sizeup-trunk-recorder` service, neither capture nor app |
| `systems.Activity` (unknown and encrypted talkgroups, watched counts in Settings) | the app, from the tail | the app, from `grant` frames on the socket. Counts miss the seconds the app is down; today they restart from zero |

Sidecar, `kind: "call"`, beyond the common fields: `system`, `talkgroup` (int), `freq_hz`, `signal_db`, `noise_db`,
`emergency`, `encrypted`, `turns` (list of `{src, pos_s, len_s, time, tag, emergency, errors, via}` after
`apply_grants`; `via`: `srclist` \| `grant` \| `freqlist`), `grants_applied`, `tr` (file name of trunk-recorder's own
JSON, a hard link, kept for `captures/emergency/`), and `audio` with trunk-recorder's own rate (`rate_hz` as written;
the reader resamples as `read_wav_16k` does).

The three flaws the evidence named, and their fix here:

| Flaw | Fix |
|---|---|
| No completion marker: the JSON can appear before the WAV is whole; the guard is "same size on two polls 1 s apart" | capture does that wait once, then publishes a sidecar by rename. The app sees only complete items. A marker from trunk-recorder itself would remove the wait; whether its per-call script hook fires after both files are closed was not checked against the pinned build (Q8) |
| Seen-at-start: calls written while the reader is down are marked seen and lost (#747) | the mark of #753 for capture; the spool cursor for the app |
| Regex over free log text | not removed, but confined: `trunkrecd.py` already parses every line (`Watchdog`, `Shipper`). It can also write what it parsed (decode rate, call start and end, unit events) as JSON lines beside its status, and capture reads that instead of a second set of regexes in `live.py`, `recorder.py`, `systems.py`, `reception.py`. One parser of third-party text, in the supervisor. It changes `tr_sig` once. A separate PR, not needed for the split |
| One malformed sidecar discards the other calls of the same poll (`scan()` raising out) | per-call `try` in capture's ingest |

P25 emergencies stay with `App._call`: the decision needs the emergency windows, the stuck-flag rule and "is it
ours", which are CAD state. A flagged call that ends while the app is down waits in the spool and alarms, late, when
the app is back, as #753 proposes. Putting them on the `urgent` lane would mean copying the stuck-flag rule into
capture (Q3).

### 4.7 Version skew and rollback

| Case | Behaviour |
|---|---|
| The app is newer than capture (the usual update) | unknown keys in the wish are ignored; a request with an unknown `op` is answered `unsupported`. The app reads `contract` from the status and from `hello` |
| Capture is newer | unknown sidecar and frame keys are ignored; an unknown `kind` or `t` is skipped (and acknowledged, for the spool) |
| The contract's major version differs | the app raises a critical Health item and does not read the spool; capture keeps capturing and the spool fills to its bound. The updater bumps both together, so this is a failed half-update |
| Capture's code changed | `radio_sig` differs: the updater restarts it. The app does not need to restart for that |
| Rollback to a release from before the split | that release's installer knows nothing of the unit. Capture compares the release behind `/opt/sizeup/current` on each wish look: when that release has no `radio=` line in `BUILDS`, capture releases its dongles and exits 78 (not restarted). The old app then claims them as today. Meanwhile `claims.claim` already refuses a dongle held by another live `sizeup` pid, so the two never open one dongle |
| The mode is switched back to inline | `/etc/sizeup/capture-mode` (a one-word file, not a `config.toml` key: the app's config reader refuses unknown keys, which would break an older release) set to `inline`, both units restarted. The app owns the same state files again |

## 5. Latency budget

Station figures are local CPU times scaled by about 4 [E]. "#767" figures are that PR's own local benchmarks, in
its final form (the look-back decoded on every key-up; the quieting gate of its first version was removed in review).

Emergency button at a key-up, traffic channel:

| Step | Today, in-process | Split |
|---|---|---|
| Frame read | 50 ms frames from `rtl_fm`'s pipe | the same code and thread |
| Wait for a decode step | every 4th frame (0.2 s) of the first 1.5 s (`tap.py`) | the same |
| The decode step | 5.5 to 7.7 ms [L], 60 to 84 ms on the station [E]; with #767 the worst frame is 2.1 to 4.0 ms [L], about 8 to 16 ms [E], and a key-up with no ID costs 14.4 ms in all, from 42.3 [L] | the same |
| The reader stalled by a close before it | 155 / 389 / 978 ms for 30 s / 60 s / 2 min [L]; with #767 9 / 14 / 16 ms [L] | #767's figures: capture's worker takes the close, the reader goes back to the pipe |
| Build the alarm | `_fm_emergency`: a `Traffic`, one registry lookup | a dict; no lookup |
| Durable | `Outbox.add`, text only: 0.035 ms median, p95 0.23, p99 4.5, max 297 ms [L, loaded machine], on the reader thread | the same insert on the `urgent` thread; behind an app clip commit it waits for it (p95 9.1 to 91 ms, p99 132 to 430 ms by clip size [L]) |
| **Decode to durable** | under 1 ms median [L] | under 1 ms median, under 100 ms at p99 when no clip commit is in the way [L]; the tail on the station is not measured |
| Wake the forwarder | an in-process `Event` | a socket frame, then one small transaction (0.035 ms median [L]). The hop is not measured; under 1 ms idle, up to one GIL switch (1 ms with #767) [E] |
| POST to the hub | 0.3 ms of client time against a local stub [L]; the real round trip is not in the evidence | the same |
| **App restarting** | not decoded: capture is down with it | durable as above; sent when `hub-forward` is back, about 4 s at the median [P] |

A page:

| Step | Today | Split |
|---|---|---|
| A pair shown live | one FFT per frame (72 to 92 µs [L]); certain after A plus 1.5 s of B (`livepage.py`); `on_change` -> the state post, after a 50 ms wait that merges bursts (`StationLink._state`) | plus one socket hop, under 1 ms [E], inside that 50 ms |
| `on_ours` -> the feed adapters poll | a direct call | plus one socket hop |
| The finished page | squelch close, then the hold window plus 0.5 s (`HOLD_SLACK_S`), looked at every 0.2 s; then the `page` event and STT (about 16 s a clip, the README's figure) | plus the spool round trip: WAV encode and write 0.3 to 3 ms [L], wake, read back: single-digit ms [E] |
| Close work for a 60 s page | `PageHold.add` 63.6 ms + `_keep_clip` 171 ms [L], on the receiver thread (the worker, with #767) | the same work, in the app: it can no longer delay any receiver |

A traffic transmission of 10 s: 43.1 ms of handler today [L], of which 32.7 ms ID decode (to capture's worker) and
8.6 ms voice filter (the app's). The spool adds the same single-digit ms. Speech-to-text dominates either way.

Nothing on the immediate paths gets a poll in front of it. What is added is one socket hop and one small
transaction, both unmeasured on the station. `closed_mono` and `published_mono` in every sidecar and a timing line in
the reader make them measurable in shadow mode before anything is switched.

## 6. Migration

Small PRs, each shippable alone, each reversible by a flag that is off by default. The mode is the one-word file
`/etc/sizeup/capture-mode`: `inline` (default, today's code path, and what unit tests use), `shadow`, `service`.
Owner: **R** the radio session (code), **N** the native session (schemas, conformance, compare tooling), **U** the
updater's owner. #767 and #753 land first; they are not part of this plan.

| # | PR | Mode it needs | Tests | Owner |
|---|---|---|---|---|
| 1 | Schemas for the sidecar, cursor, wish, status, frames and the `urgent` row, with example vectors, in `contracts/capture/` | none | the vectors validate; every example in this document is one | N |
| 2 | Move `App._mdc`, `_mdc_at`, `_via`, `keyup_link` and the close-time facts into `radio/ids.py` and `radio/segmeta.py`; `App._segment` calls them. No behaviour change | none | the existing radio suites unchanged; a test that the facts equal what the handler computed before, on the synthetic key-up scenarios of `test_keyups.py` | R |
| 3 | `radio/spool.py`: writer, reader, cursor, pruning. Unused | none | atomicity (kill between the renames), recovery of `SEQ` and `EPOCH`, per-channel order, redelivery and dedupe, bounds, a full disk; the schema vectors of PR 1 | R, N for conformance |
| 4 | Inline tee: in `inline` mode with a debug switch, the worker also writes each segment to a spool, and a test reader runs the handlers from it | `inline` | handler output from the spool equals handler output from the `Segment`, row for row, over the radio suites' scenarios. This proves the sidecar is complete | R |
| 5 | PCM tee and `radiod.py` in shadow: the in-process receiver copies each chunk it read, with its arrival time, to a socket; `sizeup-radio --shadow` runs the same receivers from that instead of `rtl_fm`, owns no dongle, writes `spool-shadow/`, a status and the live socket, and raises nothing | `shadow` | `tools/spool_compare.py`: per channel, the same transmissions (start and end equal, since the tee carries the clock), bit-equal audio, equal IDs, levels and reasons. On the station it logs counts and differences only, never content | R; compare tool N |
| 6 | `radio/dongles.py`: the dongle half of `App` as a class the `App` delegates to. No behaviour change. Splittable: modes and auto-assign; groups; no-audio, probes, USB reset; the reception test's moves | none | `test_antenna_groups`, `test_sdr_auto_assign`, `test_app`, `test_reception*`, `test_sdr_serial`, `test_claims` unchanged | R |
| 7 | `radio/control.py`: wish, status, requests. In `inline` mode the `App` talks to its own `Dongles` through the same code (a loopback), so the request path is exercised before there are two processes | `inline` | every refusal of today's `set_mode` and `set_antenna_groups` comes back as a result; an expired request is not applied; unknown keys and ops; a broken wish keeps the last good one | R, N for conformance |
| 8 | `radio/urgent.py` and the app's mover, in-process first: `tap.on_emergency` goes through the table. Logs decode-to-durable and durable-to-outbox times, and whether a forwarder was running | `inline` | the dedupe table of §4.3 case by case; `test_radio_emergency`, `test_fg_emergency`, `test_alarms` unchanged; a held write lock delays the insert and never the reader | R |
| 9 | The switch: the unit, `start-radio`, `radio_sig`, the build directory, `health-check`, `sdr-serial`; in `service` mode the app starts no receivers and runs `spool-reader` into its own worker sets (§3.6). One station, by hand, with the admin's go | `service` | the end-to-end harness below; both drains of §3.6 (capture stopped with a transmission queued: it is in the spool; the app stopped with one queued: it comes again, once); `station-install` CI with the new unit; `.github/scripts/station-update.sh` extended: an app-only update leaves capture's pid, a capture change restarts it, a rollback stops it | R, U |
| 10 | The updater: app-only updates stop waiting for carriers; `radios_back` unchanged | `service` | `test_autoupdate` cases for both kinds of update | U |
| 11 | Cleanup: the shadow tee stays as the native daemon's test bench; inline mode stays for tests, macOS and all-in-one installs. Nothing else is removed until a second station has run `service` | - | - | R |

The end-to-end harness (constraint 9), synthetic audio only: a fake `rtl_fm` (as `tests/test_squelch_stuck.py` has)
feeding a real `radiod.py`, a real spool, socket, wish and `urgent` table, and a real `App` in service mode against a
stub hub. Scenarios: a key-up with an ID; a page with tones of ours and a continuation; an emergency button while
the app is stopped, then the app started (the alarm is in the stub hub once, the transmission arrives late and takes
it over, no pre-alert fires); capture restarted mid-transmission; both restarted; a `set_mode` refused because
another pid holds the claim; a squelch change with a stale `expect`; the spool at its bound.

Reversal at every step is the mode file and a restart of both units. Steps 2, 3, 6 are refactors with no flag; they
are reversible by revert.

## 7. Risks and open questions

### 7.1 For the radio maintainers

| # | Question | What this document assumes |
|---|---|---|
| Q1 | Is "durable at once, sent when the app's forwarder is back (about 4 s)" enough for the emergency lane, or must capture send by itself? | Enough for the first step; measured in step 8 |
| Q2 | Should a live page of ours also be a durable event? It is display state plus a local poll today; the hub has no event type for it | No, until the hub wants one |
| Q3 | P25 emergencies wait for the app (windows, stuck flags). Acceptable? | Yes, with #753's late alarm |
| Q4 | `late`: #753 treats anything from while sizeup was down as late. With a 4 s app restart that silences pre-alerts for transmissions a few seconds old, which speech-to-text would have delayed longer anyway. Keep the strict rule, or treat `attended: false` items younger than some seconds as live? | The strict rule |
| Q5 | Requests in the wish file with results in the status, or request and reply on the socket? Files are easier to pin with vectors and survive a capture restart; the socket is simpler to time out | Files, with the socket as the wake-up |
| Q6 | `PageHold` in the app or in capture? In the app, a restart during a hold window finalises the page as it is and its continuation becomes a voice-only row (today the continuation is simply lost). In capture, page assembly survives app restarts but needs the tone plan's names and brings `page.py` back into the closure | The app |
| Q7 | The reception test: `reception.py` is 1,575 lines and was read here only at its interface (what it touches on `App`). Move it whole into capture's closure, or split the dongle moves (`lend`, `sweep`, `restore` as requests) from the bookkeeping? | Whole, for now |
| Q8 | P25 audio in the spool: a hard link to trunk-recorder's WAV (no copy; the reader resamples) or a 16 kHz copy (one format)? And is there a per-call hook that could replace the size-stable wait? | Hard link; not checked |
| Q9 | Spool audio as s16 WAV. The live STT pass gets float32 today and re-runs get the s16 `.src.wav`; with the spool both get s16. Does that move any transcript? `fsync` per item? | s16, no `fsync`; step 5 can compare |
| Q10 | The tap reads the squelch's private state (`_frames`, `_hold`, `_start`, `keyed`). That stays inside capture, so the split does not make it harder. Agreed? | Yes |
| Q11 | Readers of capture's log text: `metrics.py` picks `stall` against `down` by `"stalled" in detail`; `adsbd.py` reads `rtl_fm`'s argv from `/proc`. Which others match by unit name or wording? | Wording unchanged; the unit name changes |
| Q12 | Two more things a restart resets today and capture would now keep for days: the live tap's and the squelch's learned state, and `_seen` / `_pending` in `CallIngest`, which grow for the life of the process. Any that rely on a daily restart? | None known |

### 7.2 For the admin

| # | Question |
|---|---|
| A1 | The station page during an app restart: radios keep recording, but the page itself, "talking now" and the live page are away for about 4 s, and transcripts for longer. Today everything is away for about 8 s and the audio is lost. Is that the trade wanted? |
| A2 | One more unit to watch (`sizeup-radio`), one more row in `sizeup-station status`, one more Health item (spool growth). The first switch is by hand on the one station |
| A3 | Disk: the spool adds one sidecar (a few KB) per transmission and no second copy of the audio, if the app adopts the spool's file as `<id>.src.wav` by hard link. At 115 speech-to-text clips an hour [P] and an assumed 6 s each that file is about 0.5 GB a day [E], which is written today too. Under WSL2 it lands in the same virtual disk as now |
| A4 | The other app that shares the dongles sees `app: "sizeup"` claims under capture's pid, which changes about a quarter as often (§3.3). A capture restart still drops every claim for a few seconds, as each station restart does today. Its side needs no change (`sizeup/radio/claims.py`) |
| A5 | WSL2: nothing new is required of it. Unix sockets, `/run`, hard links and systemd units are already used by `sizeup-stt-encode` and `sizeup-trunk-recorder`. USB attach order after a Windows restart is as today; capture inherits `SDR_SETTLE_S` |

### 7.3 Stakeholders outside the radio session

| Area | In capture's DSP | Maintained by | What changes for them |
|---|---|---|---|
| Airband multichannel (`channelize.py`, `am.py` `AmReceiver`, the A/B test) | yes | the airband work (#275, #307) | the A/B becomes a request; its numbers come from the status |
| ATC (`atc_hours.py` schedules, `_atc` pre-alerts, the ATC vocabulary) | the schedule only | the ATC work (#233, #260) | pre-alerts stay in the app and follow `late` |
| `airport_watch.py`, `cad/airport_alerts.py` | no; they consume airband rows | the airport work | none, beyond `late` |
| Signal metrics (#605, #685) | the sampler | the metrics work | `floor` and `cc` arrive through the spool; `tx` from sidecars |
| Nodes (#758) | no | the nodes design | `channels[].run` is the per-channel state it asks for |

### 7.4 Risks

| Risk | Mitigation |
|---|---|
| The dongle code leaving `app.py` (step 6) is the largest and least mechanical move | no behaviour change, split in four, behind the existing suites, before any process boundary exists |
| Two processes, one SQLite file | one writer per table, capture's connection never checkpoints, inserts off the reader thread, measured in step 8, a separate file one setting away |
| A reader that silently is not attached marks everything late | status and a critical Health item (§4.1) |
| The closure grows back | the list, the test, the build directory (§3.3) |
| A stale spool after a long outage fires old alerts | `late`; emergency age is the alarm code's rule |
| The evidence's restart counts predate #712 | the gain is smaller than 55 to 7 suggests; it is still every hub-only change |

## 8. What a native radio daemon would implement

Only the contract of §4. It would not need Python, `config.toml`, the CAD, or a hub credential.

| It must | From |
|---|---|
| Read the wish file; keep the last good one | §4.4 |
| Write the status file every 0.5 s | §4.5 |
| Publish spool items: audio, then the sidecar by rename; keep `SEQ` and `EPOCH`; prune behind the cursor; hold the bounds | §4.1 |
| Serve the live socket with #709's framing | §4.2 |
| Insert `urgent` rows | §4.3 |
| Honour and write dongle claims | `sizeup/radio/claims.py` |
| Pass the conformance vectors of PR 1 | §6 |

Two things make that smaller than it sounds, and one makes it larger:

- The dongle policy (antenna groups, stand-ins, no-audio, the reception test) is the bulk of §4.4. It can stay a
  Python supervisor inside `sizeup-radio` that hands a native receiver "this channel on this device index", the way
  `trunkrecd.py` supervises trunk-recorder. The inner seam would be a second, smaller wish. Not designed here.
- trunk-recorder stays what it is; the daemon would not take over P25.
- The squelch is the hard part: about 60 tuned constants and 278 test cases (ADR 0001). The spool does not make that
  smaller; it makes it testable.

Shadow test: the PCM tee of step 5 is the bench. The native daemon reads the same chunks with the same arrival
times as the Python service, owns no dongle, and writes its own spool. `tools/spool_compare.py` then compares two
spools item by item: the same transmissions with the same `start` and `end`, bit-equal audio, the same `ids` and
`alarm_key`, the same `reason` and `keyup`, levels within a stated tolerance. Recorded tees (synthetic, or kept on
the station and never copied off it) replay the same comparison in CI. A daemon is switched in only after the
admin's go, by the mode file, and switched back the same way.

## 9. Where the evidence and the code disagree

| Evidence | Code on `main` |
|---|---|
| "Capture deaf about 8 s per restart", and the notes' "the true gap is at most that" | the figure is the updater's poll of row states from before `activate()`; a row reads `ok` before the first audio, and the squelch then needs 2 s (§1). Neither an upper nor a lower bound |
| The capture closure is 16 modules | a capture that runs the live tap also needs `live.py` and `livepage.py` (injected into `LiveTap`, so no import shows it); with page assembly in the app it does not need `page.py` |
| "Capture restarts 55 to 7" | 7 is the DSP alone. With the dongles owned by capture the proxy gives about 12 (§3.3) |
| ADR 0002: capture does "clip writing" | here it writes the raw audio; the filtered clip stays in the app (§3.2) |
| The unit inventory | lacks `sizeup-stt-encode` (#709, merged with #701 a few hours after the evidence's commit) |
| The hub's jobs call "13 functions" | `CALLS` has 15; none is named "set squelch" or "set radio role" (squelch rides in `set_channel_prefs`) |
| "The 300 s quiet wait can go" | #712 already replaced it (settle 10 min, holds, a 45 min ceiling); §3.5 says which holds the split makes unnecessary |
| Line numbers | drifted throughout (`App._segment`, `CallIngest.prime`, the updater); this document cites functions |
