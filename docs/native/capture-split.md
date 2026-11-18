# Capture as its own service: `sizeup-radio` behind a spool contract (design)

Status: design, no code (2026-11-18); revised after the radio maintainers' review of #773, whose decisions are
in §7.1. Follows ADR 0002 (`docs/native/adr/0002-no-native-station-agent-yet.md`, #765):
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
| 1 | Immediate paths stay in capture, never behind a spool poll: live MDC / FleetSync IDs and the emergency alarm (#690, #698, #767), page tones and the live page, "talking now", P25 live grants. A delivery path that works while the app restarts; under 100 ms from decode to the outbox; no duplicates when the app also sees the segment | §3.2, §4.2, §4.3, §5 | Decided: capture's own fsync'd file and its own sender (§4.3). The hub's handoff now bounds it (Q-A) |
| 2 | One owner of dongles: capture. Intent from the app over a versioned control channel with acks; per-channel running state back | §3.3, §4.2, §4.4, §4.5 | Decided: desired state in the wish, requests on the socket, observed state in the status. `check` for group edits (Q-C) |
| 3 | Spool: atomic, ordered per channel, idempotent ids, full metadata, `late`, at-least-once with dedupe, bounded with a Health alarm, survives both restarting; metrics the same way | §4.1 | Decided: `late` is age at the app's read, 60 s; emergencies never gated |
| 4 | P25: `CallIngest` and the #747 mark live where the spool lives; one owner of the mark; watched-talkgroup lines (#718) and split counts stay with the supervisors | §4.6 | Hard link or copy of the call audio (Q8, not answered) |
| 5 | Live feeds for the station page on a low-latency local socket | §4.2 | none |
| 6 | Version skew: versioned, unknown fields ignored both ways; capture restarts only for its own files or the contract; drains on stop (#767's workers) | §3.5, §3.6, §4.7 | none |
| 7 | CPU: capture keeps Nice -5, CPUWeight 500 | §3.5 | none |
| 8 | Airband multichannel, ATC and `airport_watch` are maintained by others | §7.3 | none |
| 9 | Inline mode stays for unit tests; a synthetic end-to-end harness through the real spool and control channel, with an app restart and an emergency during it | §6 | none |
| 10 | Boundary: DSP plus segment handling up to "segment with metadata on the spool"; `App._segment` row building, STT and CAD stay in the app; the alarm lane moves rather than `app.py` handlers | §3.1, §3.2 | Decided: page assembly in the app |

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
`sizeup-ais`, `sizeup-logread`, and since #709 (merged with #701) `sizeup-stt-encode` (a Unix socket in a
`RuntimeDirectory`, peer uid checked, framed JSON header + payload, a code-hash hello: the nearest precedent for
§4.2).

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
| The urgent lane (§4.3): emergency alarms and live pages into capture's own file, and sent to the hub by capture | Taking each item over into the normal lane: `_alarm_now`, `cad.alarms`, emergency windows, All clear |
| Writing each finished transmission to the spool | STT: `_stt`, queueing, routing, hand-off at stop, catch-up, STT input (FLAC) |
| Dongles: assignment, auto-assign, antenna groups (#609, #702), no-audio and probes and USB reset (#710), claims, the reception test, whole | Page assembly: `PageHold`, `page.py`, the tone plan's names, re-evaluation |
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
| The alarm lane moves instead of handlers, and capture sends it | Constraint 10 and the review's Q1. `_fm_emergency` needs only the IDs, the channel and the alias map; the hub works the rig out itself (`hub._traffic_of`). An alarm that waited for the app's forwarder (about 4 s at the median, 27.9 s at worst [P]) would not meet "works while the app restarts" |

Consequence, stated plainly: while the app is down nothing is transcribed, no clip is filtered, and only the urgent
lane (alarms, live pages) reaches the hub. Segments wait in the spool and are handled when the app is back: as live
ones if they are under 60 s old, as late ones if older (§4.1).

### 3.3 Modules and the import closure

| Module | Today | Proposal |
|---|---|---|
| `sizeup/radio/fm.py`, `am.py`, `amtones.py`, `channelize.py`, `tones.py`, `mdc1200.py`, `fleetsync.py`, `tap.py`, `sdr.py`, `claims.py`, `usbreset.py`, `sizeup/procenv.py`, `sizeup/tz.py`, `sizeup/atc_hours.py` | imported by `app.py` | capture's closure. The app still imports some for its own use (`fm.RATE`, `ctcss_estimate` for the key-up diagnostic, `tones.py` for `page.py`) |
| `sizeup/radio/live.py`, `livepage.py` | imported by `app.py`; not in the evidence's 16-module list, though the tap is useless without them | capture's closure |
| `sizeup/radio/groups.py`, `recorder.py`, `trunkrecd.py` (constants and `read_status`), `p25.py` (`CallIngest`, `parse_call`, `apply_grants`) | imported by `app.py` | capture's closure. `split_call` and `Piece` stay importable by the app |
| `sizeup/radio/reception.py`, `probes.py`, `systems.py` | `reception.Tests` takes the `App` and drives its dongle state | capture's closure, `reception.py` whole (decided, Q7): it is dongle lending and measurement, and the app shows its results from the status file. `systems.py` splits: `load`, `discover`, `P25System` are config; `Activity` and tier logic need `TalkgroupPrefs` and stay in the app, so `sourcekeys.py` and `tiers.py` stay out of the closure |
| `sizeup/radio/page.py` | imported by `app.py`; in the evidence's 16 | **not** in capture's closure: `PageHold` runs in the app (decided, Q6) |
| `sizeup/cloud/client.py` (`HubClient`, `Backoff`, `HubError`), with `sizeup/cloud/__init__.py` | the app's hub link | capture's closure too, for the urgent sender (§4.3): one implementation of the bearer and Access headers, not a second copy. Standard library only |
| `App._mdc`, `_mdc_at` (#767), `_fm_emergency`, `_via`, `keyup_link`, `make_receiver`, `_wire_receiver` (#767), `_heard_elsewhere`, `_watch_fm_once` | `sizeup/app.py` | new `sizeup/radio/ids.py` (the decode), `radio/segmeta.py` (the sidecar's facts), `radio/radiod.py` (the service) |
| The dongle half of `App` (about 1,500 lines: `_load_modes` to `_set_group_mode`, `sdr()`, `_start_mode`, `_stop_mode`, the no-audio and probe methods, `set_antenna_groups`, `set_adsb_receiver`) | methods of `App` | new `sizeup/radio/dongles.py`, class `Dongles`, used in-process in inline mode and by `radiod.py` in service mode |
| New | | `radio/spool.py` (writer and reader), `radio/control.py` (wish, status, requests and replies), `radio/liveout.py` (the socket), `radio/urgent.py` (the file, the sender) |
| `metrics.Sampler` | `sizeup/cloud/metrics.py` | the sampling half moves to `radio/`; `Collector` and `MetricsLink` stay |

Capture must never import `sizeup.config`, `sizeup.app`, `sizeup.stt`, `sizeup.cad.*`, `sizeup.channels`, or
anything of `sizeup.cloud` but `client.py`. Everything it needs from the config arrives in the wish file (§4.4). Two
helpers it uses live in `config.py` today (`airband_owner`, `P25_JOB`) and move to a small module.

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
| ... + `reception.py`, `probes.py`, `systems.py` (the first draft's proposal) | 12 |
| ... + `cloud/client.py`, `cloud/__init__.py` for the urgent sender (**the proposal**) | 14 |
| ... if `systems.py` is not split, so `sourcekeys.py` and `tiers.py` come too | 17 |
| The proposal without importing `trunkrecd.py` | 13 |
| `sizeup/app.py` alone | 34 |

So constraint 2 (capture owns the dongles and the reception test) costs about 5 more restarts in the window than
the bare DSP, and the sender's client 2 more: the proposal lands at about 14 where the station restarted 55 times
[E from G]. The review accepted 12 with the dongles; 14 is that plus the decision that capture sends. Add the
dongle code that leaves `app.py` (it changed with #609 in the window; #702 and #710 merged within an hour after it)
and contract bumps. `trunkrecd.py` is the largest single contributor (5 commits, 1 of them not shared with another
listed file); importing only a small constants module from it would remove that.

### 3.4 What runs where after the split

| Thread or process | Unit |
|---|---|
| `rtl_fm`, `rtl_sdr`, the channelizer; `nfm-<label>`, `segment-<label>`, `sdr-watch`, `talking`, `p25-ingest`, `trunk-recorder-log`, `radio-metrics`; new `urgent`, `urgent-send`, `live-out`, `control` | `sizeup-radio` |
| `spool-reader` (new), `segment-<label>` again for the handlers (§3.6), `keyup-pl`, everything else in §2.1 | `sizeup-station` |
| trunk-recorder and its supervisor, unchanged | `sizeup-trunk-recorder` |

### 3.5 The unit, its signature, start order, health

`deploy/station/systemd/sizeup-radio.service`, modelled on `sizeup-trunk-recorder.service`:

| Setting | Value | Why |
|---|---|---|
| `User`, `Group`, `SupplementaryGroups` | `sizeup`, `sizeup`, `plugdev` | as the station |
| `Environment` | `HOME=/var/lib/sizeup`, `SIZEUP_STATE_DIR=/var/lib/sizeup`, the `rtl-sdr` `PATH` and `LD_LIBRARY_PATH` | the claims directory follows `HOME` |
| `EnvironmentFile` | `-/etc/sizeup/radio.env` | the station token and the Access service token for the urgent sender, and nothing else; not `station.env` (§4.3, §7.5) |
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
| `/var/lib/sizeup/state/urgent.sqlite` | capture, the only writer | app, read-only |
| `/etc/sizeup/radio.env` | root (`sizeup-station secrets`) | systemd, for capture |

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
| `fsync` (decided, Q9) | none per item. Every `spool.sync_s` (default 5 s) in which something was published, and at stop, capture syncs the files published since the last time and then `items/`. A crash or a power cut loses at most the last seconds of spooled audio; the urgent lane is durable on its own. The sidecar carries `audio.bytes`, so a short or empty file after a power cut is detected, logged and skipped, never parsed as audio |
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
| `replayed` | bool | a P25 call capture took from the #753 mark after its own restart (§4.6). Informative: lateness is decided by age, below. Default false |
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
| `audio` | object or null | `{file, format: "wav", encoding: "s16le", rate_hz: 16000, channels: 1, samples, bytes}`; s16 (decided, Q9: what the clips already are): the float audio clipped to +-1 and scaled by 32767, exactly `<id>.src.wav` today. null when the wish says the channel is not recorded: the sidecar still carries IDs, levels and the alarm |
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
| Late (decided, Q4) | by age at the app's read and nothing else: `now - end` under `LIVE_MAX_AGE_S` (60 s) is live, with every live consumer; older is `late`, with #753's rules: the radio log, STT and the hub, but no ATC, intel, hints or incoming (`_atc`, `Comms.on_text`). With a spool, reading a segment a few seconds late is the normal path, so an app restart of 4 s makes nothing late |
| Emergencies | never gated by `late`. They reach the hub by the urgent lane (§4.3); the finished transmission's `_alarm_now` runs whatever its age and only attaches the row and its clip to the alarm already there. The alarm code's own `MAX_AGE_S` (30 min) is unchanged |
| Replay bound | everything unacknowledged is ingested, however old: unlike the P25 directory scan, the cursor says exactly what was done. Whether an old emergency still pushes is the alarm code's own age rule |
| No reader | items age in the spool and turn late after 60 s. Capture's status shows `reader: {attached, since}` and `spool.oldest_s`; an app that runs and is not attached raises a critical Health item |

### 4.2 The live event stream

A Unix stream socket, `/run/sizeup-radio/live.sock`, mode 0660 in a 0750 directory, peer uid checked
(`SO_PEERCRED`). Framing is #709's: `>II` header length and payload length, a JSON header, raw payload bytes (unused
in v1; there for audio frames if a live listen is ever built).

| Why this transport | |
|---|---|
| Stream, not datagram | a connection tells capture that a reader is attached (`reader` in the status), a new client gets a snapshot, a request gets its reply on the same connection, and order holds |
| Not a file tail | a poll is exactly what constraint 1 rules out, and it would wear the disk |
| Never blocks capture | the reader thread only appends to an in-memory queue. One sender thread writes non-blocking; a client 256 frames behind is disconnected and gets a snapshot when it comes back |
| Nothing is queued for an absent client | live state is latest-wins, as `/api/station/state` is. What must not be lost is in the spool or the urgent file |

Capture to client (every header has `v`, `t`, `at`):

| `t` | Fields | From today's |
|---|---|---|
| `hello` | `contract`, `impl`, `build`, `pid`, `started`, `epoch` | - |
| `snapshot` | `carriers`, `talking`, `paging`, `status_at`, `spool_seq` | first frame after `hello`, and on request |
| `carrier` | `ch`, `state` (`up` \| `keyed` \| `down`), `since`, `continuing`, `holding`, `quieting_db`, `queued` | squelch open; `NoiseSquelch.keyed` (held `min_open_s`: only then is it "talking"); close. `continuing` and `queued` (closed, waiting for capture's worker) are what `App._release_pages` reads (§3.6) |
| `ids` | `ch`, `since`, `ids` | `LiveTalk.fm_ids` from the tap |
| `urgent` | `n`, `key`, `rev`, `kind`, `durable` | a row was committed to `urgent.sqlite` (an alarm from `tap.on_emergency` or the close; a live page): the app takes it over (§4.3). `durable: false`: the insert failed; the frame then carries `data` and is the only local copy |
| `reply` | `id`, `ok`, `error`, `confirm`, `result` | the answer to a request, below |
| `talking` | `entries`: `key`, `system`, `channel` or `tg`, `radio_id`, `alias`, `since`, `talk_since`, `ended`, `until`, `id_on_release` | `LiveTalk.state()` without `rig`, `label`, `main`: the app adds those. The whole list, latest wins |
| `paging` | `entries`: `key`, `channel`, `started`, `pairs` (`a`, `b`, `names`, `ours`), `phase`, `ours`, `ended`, `until`, `page_id` | `LivePaging.state()` |
| `ours` | `ch`, `pair` | `LivePaging.on_ours` (the app calls `sources_poll_now`) |
| `grant` | `system`, `tg`, `src`, `what` | a unit event of another P25 system, for the app's `Activity` |
| `spool` | `seq`, `id`, `kind`, `ch` | an item is complete |
| `status` | `at` | the status file changed |
| `level` | `ch`, `open`, `quieting_db`, `hiss_db` | once a second per channel, only to a client that subscribed |

Client to capture, notices (no answer): `hello` (`role`: `reader` or `viewer`, `contract`, `subscribe`), `poke`
(`what: "wish"`), `cursor` (the spool cursor moved), `urgent_taken` (`n`: the app has taken rows up to it over),
`paging_finished` (`ch`, `page_id`, `end`: today's `LivePaging.finished`; without it the live page ends on its own
hold timeout, as it does today when nothing finishes it).

Client to capture, requests (decided in review, Q5: what is imperative and needs an answer). A request is
`{v, t: "request", id, op, args, by, at, expires}`; the answer is `{v, t: "reply", id, ok, error?, confirm?,
result?, at}` on the same connection.

| `op` | `args` | Today's call | Answers besides `ok` |
|---|---|---|---|
| `pick` | `sdr`, `job` (a channel, the P25 job, a system job, `Off`), `confirm` | `App.set_mode`, from the web or the hub's job | `refused: parked`, `refused: taken` (another app holds it), `confirm: ...` (a fire job, a group rule: the UI asks and sends again with `confirm: true`) |
| `squelch_apply` | `label`, `open_db`, `close_db`, `min_open_s`, `expect` (`open_db`, `close_db`) | the Apply of a suggestion (#685, #700) | `stale`: what runs now is not `expect`. Without `expect` it is a plain set |
| `reception_start`, `reception_stop` | the body of `reception_post`; the test's `id` | `App.reception_post` | a test is running; no dongle to lend; unknown test |
| `probe` | `sdr` | the no-audio probe, now (`App._probe`) | not idle; held; `result`: audio or none |
| `airband_ab` | `label`, `minutes`, `phase_s` | `App.airband_ab` | not an airband channel; not running |
| `check` | `what` (`groups` \| `adsb`), `value`, `confirm` | the refusals of `App.set_antenna_groups` and `set_adsb_receiver`, asked before the app writes the wish (Q-C) | invalid; `confirm: ...`; held |
| `snapshot` | - | - | `result`: the snapshot |

| Rule | |
|---|---|
| Ids | chosen by the client, unique. Capture applies each `id` once and remembers the last 256 for 10 min: a repeat gets the first reply again and changes nothing |
| `expires` | a request that reaches capture after it is answered `expired` and not applied: a click answered late is never applied late |
| No reply within the client's timeout (capture restarting) | the client sends the same `id` again when it reconnects, or gives up and tells the person. Either is safe |
| After `ok` | capture has applied it. The app then records it in the wish file (§4.4), which is what makes it outlive a restart of capture |
| An unknown `op` | `ok: false, error: "unsupported"` |
| Who may ask | a client that said `role: "reader"`; a `viewer` gets `forbidden`. Both are the same uid (§7.5); the role keeps a status tool from changing anything by mistake |

When the app is down capture keeps decoding, keeps its live state, and drops frames nobody reads. Alarms and live
pages still reach the hub, by the urgent lane (§4.3). "Talking now" does not: it is display state on the app's state
post, and the hub shows none until the app is back and has read the snapshot, about 4 s at the median [P]. Today
nothing reaches the hub for the whole restart and the audio is gone too.

### 4.3 The urgent lane: capture's own file and its own sender

Decided in review (Q1, Q2, Q3): what cannot wait for the app is written by capture to **its own SQLite file** and
**sent by capture itself** to a new idempotent hub route. The app still takes each item over into the normal lane, so
the hub sees it again with the full row; the hub's dedupe makes that harmless.

What goes in the lane:

| `kind` | When | `key` | `rev` |
|---|---|---|---|
| `alarm`, analog | the live tap decodes an emergency packet (`tap.on_emergency`) | `<label without spaces>-live-<int(carrier start * 1000)>`, today's `_fm_emergency` key | 0 |
| `alarm`, analog, at close | the whole-clip decode finds an emergency the tap did not raise (sent at release, or missed) | the transmission's own `id` | 0 |
| `alarm`, P25 live | an emergency bit on a grant or unit event. **No such signal exists today**: see below | `p25-<system>-<talkgroup>-<radio id>-<int(at * 1000)>` | 0 |
| `page` | `LivePaging` shows a page: the first pair that is certain, then each change (a pair added or settled, `phase` to `voice`, ended) | `LivePaging`'s own key, `page:<channel>:<started, 2 decimals>` | 0, 1, 2, ... one per change |

P25, stated plainly: nothing sizeup reads from trunk-recorder while a call is up carries an emergency bit.
`unit-event.sh` is run with `<shortName> <radioID> <event> [talkgroup] [patches]` and prints only `call` events;
`live.py`, `recorder.py` and `trunkrecd.py` have no emergency pattern; the repo's sample log
(`tests/fixtures/trunk-recorder-sample.log`) has no such line. The flag arrives only in the finished call's JSON
(`emergency`, `srcList[].emergency`), which `App._call` reads. So the P25 row above is a slot in the contract, not a
working path. Filling it needs trunk-recorder to say the grant's emergency bit when it is granted, most likely a
line added by the station's existing patch to the pinned build; that was not checked against the pinned source (Q-B).
Until then a P25 emergency reaches the hub at the end of its call through `App._call`, as today.

#### The file

`/var/lib/sizeup/state/urgent.sqlite` (the path is `paths.urgent_db` in the wish file).

```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;            -- every commit is fsync'd
CREATE TABLE IF NOT EXISTS urgent (
  n          INTEGER PRIMARY KEY AUTOINCREMENT,
  key        TEXT    NOT NULL,        -- the idempotency key, above
  rev        INTEGER NOT NULL DEFAULT 0,
  kind       TEXT    NOT NULL,        -- 'alarm' | 'page'; a reader leaves an unknown kind alone
  at         REAL    NOT NULL,        -- wall clock at insert, epoch s
  event_at   REAL    NOT NULL,        -- when it happened on the air (carrier start, the tone's start), epoch s
  data       TEXT    NOT NULL,        -- JSON object, the item's data (below)
  state      TEXT    NOT NULL DEFAULT 'new',   -- 'new' | 'sent' | 'rejected' | 'expired'
  sent_at    REAL,                    -- when the hub answered for it
  hub        TEXT,                    -- the hub's word: 'applied' | 'duplicate' | 'stale' | 'rejected'
  tries      INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,                    -- a status and a reason, never a body, never a token
  UNIQUE (key, rev)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);   -- 'v' = 1, 'epoch' = 16 hex
```

| Rule | |
|---|---|
| One writer | **capture is the only process that opens this file read-write**: it inserts, marks rows sent and prunes. One writer, so no lock is ever waited for, and none of the outbox's clip commits (p99 132 to 430 ms [L]) can sit in front of an insert |
| The app reads it | read-only (`file:...?mode=ro`), woken by the `urgent` frame. Its take-over cursor is its own: `meta.urgent_n` in `outbox.sqlite`, moved in the same transaction that queues the take-over event. With no outbox (all-in-one) the cursor is in the CAD's own store. It tells capture on the socket (`urgent_taken`), which only matters for pruning |
| Insert | `INSERT OR IGNORE` on `(key, rev)`, on the `urgent` thread. The pipe reader only puts the item on that thread's queue |
| `data`, an alarm | `source` (the channel label), `start`, `end`, `radio_id`, `ids` (as the sidecar's), `unit` (the alias, from the wish's map), `emergency: true`, `emergency_via` (`MDC` \| `FleetSync` \| `P25`); P25 adds `system`, `talkgroup`. No `rig`: the hub derives it (`hub._traffic_of`). These are the fields `_fm_emergency` builds today |
| `data`, a page | `channel`, `started`, `pairs` (list of `{a, b, names, ours, at}`, as `LivePaging` keeps them), `phase` (`tones` \| `voice`), `ours`, `ended`, `until` |
| Bound | rows are a few hundred bytes. Pruned: `sent` rows the app has taken over, an hour after `sent_at`; any row after 24 h; at most 10,000 rows, the oldest `sent` first. An unsent alarm younger than its expiry is never pruned |
| Corrupt or unreadable at start | renamed `urgent.sqlite.bad-<epoch s>`, a new file with a new `epoch`, a CRITICAL log line, `urgent.state = "reset"` in the status |

#### The sender

One thread in capture, `urgent-send`, with one kept-alive connection (`sizeup/cloud/client.py` `HubClient`: the
bearer token, the Cloudflare Access service-token headers, one quiet retry on a connection the edge closed).

| Topic | Rule |
|---|---|
| What is sent | every `new` row, alarms first, in `n` order; for a page only the highest unsent `rev` of each `key` (older revisions are marked `expired`). At most 32 items and 64 KB a request; one request in flight |
| When | at once after the row's commit: durable first, then sent |
| "Sent" | a 200 whose `results` names the item `(key, rev)` with `applied`, `duplicate` or `stale`. The row becomes `sent`. Nothing else counts: not a 2xx without the item, not a timeout after the body went |
| `rejected` in a 200 | the hub will never take this item (malformed): the row becomes `rejected`, is never sent again, is counted, and Health shows it |
| No answer, a timeout (5 s), 502, 503, 530, 429 | retried: 0.5 s, doubling to 2 s for the first 3 min of a failure, then to 15 s; the hub's `Retry-After` is used when given (`client.Backoff`'s rule). Nothing is dropped |
| 401, 403, 409 (`token_unknown`, `id_mismatch`, `multi_station_off`, ...) | kept and retried every 30 s; `urgent.state = "refused"` with the hub's `reason`; a critical Health item. Never the token in a log line |
| 400, 413, 422 for a whole request | the request is cut to single items (the rule of #762); an item refused alone becomes `rejected` |
| 404, 405 | a hub without the route: `urgent.state = "no_route"`, tried again every 5 min. The app's lane still carries everything. Capture sends only when the wish says the hub advertises `urgent` (`hub.caps`) |
| Expiry | an alarm is given up 30 min after `event_at` (`alarms.MAX_AGE_S`: the hub's alarm code raises nothing older); a page item 120 s after `event_at` (a live page that old is over; the finished page comes by the app's lane). Both become `expired`, counted |
| Offline | rows wait; the retry runs at 15 s; when the hub answers, what has not expired goes, oldest alarm first, each with its `event_at` so the hub can label it as raised late (as #753 does) |
| The link is off (all-in-one, or Settings > Station > Hub link off) | `hub.forward` is false in the wish: the sender idles (`urgent.state = "off"`). The rows are still written, for the app to raise locally |
| Keep-warm | optional: `GET /api/station/hello` every 50 s, so the first alarm does not pay a TLS handshake through the edge (100 to 200 ms by `client.py`'s own note). About 1,700 small requests a day. A setting, off by default (Q-D) |
| Health | the status file's `urgent` block (§4.5). The app's health report turns it into rows: the sender's state, the oldest unsent alarm, counts of `rejected` and `expired` |

#### The route: `POST /api/station/urgent`

Hub work (§11). Request, `Content-Type: application/json`, read by `Content-Length`, at most 64 KB:

| Field | Type | Meaning |
|---|---|---|
| `v` | int | 1 |
| `station` | string | the station id capture was told (`hub.station` in the wish: the id the hub assigned, `StationLink.adopt`). Checked against the token (`id_mismatch`), as on every station route |
| `epoch` | string | the urgent file's `epoch`; informative, not a cursor |
| `sent_at` | float, epoch s | when this request was made |
| `items` | list, 1 to 32 | `{key, rev, kind, at, event_at, data}`, the row's columns |

Answers:

| Status | Body | Meaning for the sender |
|---|---|---|
| 200 | `{ok: true, v: 1, station, results: [{key, rev, status, reason?}]}`; `status`: `applied` \| `duplicate` \| `stale` \| `rejected` | per item, above. An item missing from `results` was not taken: sent again |
| 400, 413, 422 | `{error, reason}` (`bad_json`, `bad_batch`, `too_large`) | cut to singles |
| 401, 403, 409 | `{error, reason}`, as `Hub._refuse` and `_other_station` answer today | `refused` |
| 503 + `Retry-After` | the hub is not the leader (a deploy's handoff, `fence.refuse`): **nothing was applied** | retried |

Idempotency, on the hub:

| Case | What the hub does |
|---|---|
| The first `(station, key)` of an alarm | records the key in its dedupe store, builds the transmission as `_apply_alarm` does (`id` = `key`, no text, `emergency: true`) and raises the alarm: `applied` |
| The same alarm `key` again (capture retried; the answer was lost) | `duplicate`; no second alarm, no second push. The dedupe store is asked first, so this holds even after the alarm's row has moved to the finished transmission |
| The app's take-over: an `alarm` event with `id` = `key` in `/api/station/events` | the same `traffic_id`: the alarm is updated, not raised again (today's rule) |
| The app's event for the finished transmission, `id` T with `alarm_key` = `key` | `alarms._on_traffic` finds the alarm by `alarm_key` and the transmission takes it over: one alarm, now with its clip and later its text (today's rule) |
| An alarm raised at close with `key` = T, then the app's event for T | the same `traffic_id` |
| A P25 live alarm, then the end-of-call alarm from `App._call` | one emergency: matched on system + talkgroup + radio id within the alarm code's repeat window (`REPEAT_S`), the end-of-call row taking the live one over. New on the hub, and unused until a live signal exists |
| The same radio pressing again | the alarm code's own same-radio rule, unchanged |
| A page item | the hub keeps the highest `rev` per `(station, key)`: a higher one is `applied` (its live paging state for that station is set from it, with `until` as its expiry), an equal one `duplicate`, a lower one `stale` |
| `/api/station/state` `paging` from the app, for the same page | merged by `key`: the newer of the two wins. With the app up both lanes say the same thing |
| The finished page from the app's `PageHold` | arrives as a `page` event as today; it carries `live_key` so the hub can end the live entry at once (today `LivePaging.finished` does this on the station) |
| An unknown `kind` | `rejected`, `reason: "kind"`; the rest of the request is still taken |

Not "seen": like logs and metrics, a post to this route comes from another process than the link and must not hold
the station's "connected" up. The hub keeps its own `urgent_seen` time for Health.

#### Credentials

| | |
|---|---|
| What capture needs | `SIZEUP_STATION_TOKEN` (the station's own `sz1` token) and, where the hub sits behind Cloudflare Access, `SIZEUP_ACCESS_CLIENT_ID` / `_SECRET`. The same identity as the app: no new token, no new Access application (`/api/station/*` already admits the station's service token) |
| Where from | `/etc/sizeup/radio.env`, root 0600, read by systemd (`EnvironmentFile=` in `sizeup-radio.service`). It holds those three variables and nothing else. `sizeup-station secrets` writes it next to `station.env`. Not `station.env` itself: that file also carries the incident-source adapter's credentials (#301), which capture has no use for |
| In the process | read once at start into memory and **removed from `os.environ` at once**, as `procenv.take_credentials` does for the credentials directory. No child inherits them: not `rtl_fm`, `rtl_sdr`, the channelizer or a probe. (Today `child_env()` leaves the hub token in the environment of the station's `rtl_fm` children.) |
| Never | in the wish file, the status file, a socket frame, the spool, `urgent.sqlite` or a log line. `last_error` holds a status and the hub's `reason` only |
| Rotation | `sizeup-station secrets` restarts `sizeup-station` today; it then also restarts `sizeup-radio` (a capture gap of seconds, by an operator's hand). The hub's Rotate keeps the old token valid 10 min, which covers it |
| Exposure, compared with today | one more process of the same user holds the same two secrets, and it is the one that parses radio input. It is smaller than the app (no web server, no feed adapters, no CAD) and makes one kind of outbound request, to one host. The unit keeps the station unit's `NoNewPrivileges`, `ProtectSystem`, `ProtectHome` and `PrivateTmp` |

#### The hub's hot/cold handoff

During a hub deploy the old task drains and the new one takes the lock; a station write in between is refused
before anything is done: 503 with `Retry-After` (`fence.refuse`; `docs/cloud.md` "Deploys: the hot/cold handoff").

| | |
|---|---|
| What the sender does | keeps the item, retries at the hub's `Retry-After` (0.5 s at least), and sends it the moment the new task serves |
| How long that is | `docs/cloud.md` expects about 35 to 45 s for the drain plus the takeover. It is not in the Phase 1 evidence |
| How often | 57 hub deploys in the 4.45 days [P]: at that rate the hub is not taking station writes for roughly 7 to 10 min a day in all [E] |
| What it means | the lane's decode-to-hub time is bounded by the hub's handoff, not by the station. Capture removes the station's own gap; an emergency keyed during a hub handoff still waits for the hub. That is the hub maintainers' to shorten or to accept (Q-A) |
| Safety | a 503 applied nothing, so the retry is the first delivery. A timeout that did reach the hub is covered by the dedupe store |

#### The app's take-over

| Kind | With the hub link on | All-in-one, or the link off |
|---|---|---|
| `alarm` | an `alarm` event with `id` = `key` into the outbox (what `_alarm_now` sends today), in the transaction that moves `meta.urgent_n` | `cad.alarms.on_traffic`, as `_alarm_now` branches today |
| `page` | nothing queued: the app's live state post already carries it, and the finished page comes from `PageHold`. The cursor moves | the local display, from the socket's `paging` frame |

Then the finished transmission arrives through the spool with `alarm_key`, and `App._segment` calls `_alarm_now`
with the clip, whatever its age (§4.1).

### 4.4 Control, desired state: the wish file

Decided in review (Q5): the wish file holds **desired state only**. It is level-triggered and survives restarts: a
capture that starts alone runs what it says. Anything that needs an answer goes over the socket (§4.2).

`state/radio_wish.json`, written by the app with tmp + rename whenever it changes, read by capture on a `poke` and
every 0.5 s.

| Field | Type | Meaning |
|---|---|---|
| `v`, `seq`, `at`, `pid`, `build` | int, int, float, int, string | `seq` is a counter the app keeps in the file itself (previous + 1), well under 2^53 |
| `mode` | `service` \| `shadow` | §6 |
| `paths` | object | `spool`, `urgent_db`, `clips`, `captures`, `state`, `rtl_fm`, `rtl_sdr`, `recorder`, `recorder_dir` |
| `hub` | object | `forward` (bool: the link is on), `url`, `station` (the id the hub assigned), `caps` (list; `urgent` among them when the hub has the route). No secret |
| `channels` | list | the channel set. Per `[[radio.channels]]` entry: `label`, `kind`, `mhz`, `gain_db`, `device`, `open_db`, `drop_db`, `hang_s`, `continue_s`, `keyups`, `split_s`, `jump_db`, `schedule`, `also`, `record` (bool) |
| `airband` | object | `multichannel`; the owner rule for a frequency two dongles hear (`config.airband_owner` today) |
| `squelch` | `{label: {open_db, close_db, min_open_s}}` | the values to run, already fitted by `channels.fit_squelch`. Applied live (`set_squelch`), no restart |
| `tones` | `{plan: [{a_hz, b_hz, names}], ours: [name]}` | for `LivePaging`: the names a live page shows, and which are ours |
| `aliases` | `{radio id: name}` | for an alarm's `unit` |
| `dongles.jobs` | `{serial: {job, by}}` | the job each dongle should run: a channel label, the P25 job, a system job, or `Off`. `by` is `person` here; what auto-assign and the groups decide is capture's own (below) |
| `dongles.groups` | list of `{name, sdrs, jobs, parked}` | the antenna groups as saved (`groups.parse`'s shape), parks included |
| `dongles.reserved` | `{adsb, ais}` | the serials set aside for the ADS-B and AIS decoders; never given a job |
| `dongles` (rest) | | `listed`, `auto`, `auto_assign`, `order`, `ignore`, `start_mode`: the `[radio]` settings of the same names |
| `p25` | object | the county recorder and each system's directory, name, label, sources and jobs; talkgroup labels and unit aliases for `parse_call` |
| `metrics` | bool | sample `floor` and `cc` records |
| `spool` | object | `max_age_h`, `max_bytes`, `max_items`, `sync_s` (§4.1) |

| Rule | |
|---|---|
| Missing, unreadable or not an object | capture keeps running what it last accepted (it keeps a copy, `radio_wish.applied.json`), says so in the status (`wish_problem`), and never reads a broken file as "everything off", which is what `trunkrecd.wanted` does with its own. With no accepted wish at all it idles |
| A wish that cannot be met | is not an error. A job whose dongle another app holds, or that is parked, simply shows as `held`, `shed` or `waiting` in the status until it can run |
| Who writes what | the app is the only writer of the wish. A person's pick is asked on the socket first (`pick`); capture answers and, on yes, applies it at once; the app then writes it into `dongles.jobs`, which is what makes it survive a restart. The same for a squelch Apply and for group edits |

What capture decides itself is not in the wish. It keeps it in its own files, in today's formats, and reports it in
the status: auto-assigned jobs, a group's stand-ins (`antenna_standins.json`), dongles out for no audio
(`antenna_noaudio.json`), flap holds, a reception test's loans (`reception_*.json`), the wish files it writes for
`trunkrecd` (`p25_recorder*.json`). The app keeps `sdr_modes.json`, `antenna_groups.json` and `adsb_receiver.json`
as its own record of the desired state, in today's formats, which is what makes a switch back to inline mode safe.

### 4.5 Control, observed state: the status file

`/run/sizeup-radio/status.json`, compact JSON, tmp + rename, every 0.5 s and at once when something changes. It holds
what capture **observes**, never a reply (those are on the socket).

| Field | Meaning |
|---|---|
| `v`, `at`, `pid`, `impl`, `build`, `contract`, `started` | `at` older than 15 s: the reader takes capture as down and adds `stale: true` |
| `wish_seq`, `wish_problem` | the wish it runs; why a newer one was not accepted |
| `reader` | `{attached, since}`: an app holds the socket as `reader` |
| `channels` | per channel: `label`, `kind`, `mhz`, `sdr`, `run`, `state`, `detail`, `restarts`, `open`, `open_s`, `stuck`, `last_heard`, `queued`, `quieting_db`, `hiss_db`, `floor_db`, `squelch` (`open_db`, `close_db`, `min_open_s` as running), `short_per_min`, `now` (airband), `ab` |
| `channels[].run` | `running` \| `waiting` (no dongle) \| `out` (a dongle streaming nothing, #710) \| `held` (another app) \| `shed` (to a higher job of its group) \| `not_in_set` \| `local` \| `off`: the words of `docs/nodes.md` (#758), which reports them as a node's capability |
| `sdrs` | per dongle, the rows of `App.sdr()`: `serial`, `name`, `product`, `present`, `job`, `set_by` (`person` \| `auto`), `held_by` (`app`, `mode`), `noaudio`, `ambiguous`, `lent`, the group fields |
| `groups`, `groups_problem`, `no_park`, `duplicates`, `adsb` | as `App.sdr()` |
| `p25` | per system: `state`, `detail`, `cc` (`ControlChannel.status()`), `last_call`, `mark`, `seen` (the size of the ingest's set, for the soak test) |
| `reception` | `Tests.state()`: the running test and the history. The app shows it; it runs nothing of the test (Q7) |
| `spool` | `{epoch, seq, pending, oldest_s, bytes, dropped, dropped_audio, synced_at}` |
| `urgent` | `{state, reason, pending, pending_alarms, oldest_alarm_s, last_sent_at, last_ms, sent, rejected, expired, last_error}`; `state`: `ok` \| `retrying` \| `refused` \| `no_route` \| `off` \| `reset` |

`App.radio_status()`, `App.sdr()`, `squelch_state()`, `reception_state()`, `station_metrics()` and the status post
to the hub are then built from this file plus the app's own rows (STT, voice, hub). Their shapes do not change.

### 4.6 P25: the existing hand-off on the same contract

trunk-recorder and `trunkrecd.py` stay as they are, in their own unit. What changes is who stands on the other side:

| Piece | Today | Proposal |
|---|---|---|
| The wish files `p25_recorder*.json`, reading `status*.json` | the app (`UnitRecorder`, `SystemUnitRecorder`) | capture, as the owner of the dongles. Formats unchanged |
| The log tail (`UnitRecorder._tail`), `LiveTalk.feed_line`, `ControlChannel` | the app | capture: live grants are an immediate path. Its grant log (30 min) then survives app restarts |
| `CallIngest` (poll, settle, `parse_call`, `apply_grants`) | the app | capture. Each call it takes becomes a spool item |
| The replay mark, as proposed in #753 (`p25_ingest_mark.json`, one per capture dir) | the app | **capture owns it and is its only writer.** It covers the time capture itself was down, about a quarter as often as today (§3.3). Calls it replays carry `replayed: true`; grants are not applied to them, as in #753; whether they are late is their age (§4.1). The time the app was down is covered by the spool cursor instead |
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
| `CallIngest._seen` and `_pending` grow for the life of the process (one entry per call), which a restart 12.8 times a day hid | pruned of entries whose files are gone: ingest deletes them (decided, Q12; step 3 of §6) |

P25 emergencies (decided, Q3). The end-of-call flag (`emergency`, `srcList[].emergency` in the call's JSON) stays
with `App._call`: that decision needs the emergency windows, the stuck-flag rule and "is it ours", which are CAD
state. A live emergency bit on a grant or unit event would go into the urgent lane at once, and the end-of-call
alarm would be deduped against it by system + talkgroup + radio within the repeat window (§4.3, §11). **There is no
such live signal today** (§4.3 says where that was checked): until trunk-recorder is made to say it, a P25 emergency
is raised at the end of its call, and one that ends while the app is down is raised when the app is back, labelled
late, as #753 proposes.

### 4.7 Version skew and rollback

| Case | Behaviour |
|---|---|
| The app is newer than capture (the usual update) | unknown keys in the wish are ignored; a request with an unknown `op` is answered `unsupported`. The app reads `contract` from the status and from `hello` |
| Capture is newer | unknown sidecar and frame keys are ignored; an unknown `kind` or `t` is skipped (and acknowledged, for the spool) |
| The contract's major version differs | the app raises a critical Health item and does not read the spool; capture keeps capturing and the spool fills to its bound. The updater bumps both together, so this is a failed half-update |
| Capture's code changed | `radio_sig` differs: the updater restarts it. The app does not need to restart for that |
| Rollback to a release from before the split | that release's installer knows nothing of the unit. Capture compares the release behind `/opt/sizeup/current` on each wish look: when that release has no `radio=` line in `BUILDS`, capture releases its dongles and exits 78 (not restarted). The old app then claims them as today. Meanwhile `claims.claim` already refuses a dongle held by another live `sizeup` pid, so the two never open one dongle |
| A hub without the urgent route (older, or rolled back) | it does not advertise `urgent`; the app leaves it out of `hub.caps`; capture writes the rows and does not send; the app's lane carries everything, as today. A 404 from a hub that did advertise it: `no_route`, tried again every 5 min |
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
| Durable | `Outbox.add`, text only, `synchronous = NORMAL` (no fsync per commit): 0.035 ms median, p95 0.23, p99 4.5, max 297 ms [L, loaded machine], on the reader thread, in a file the clip commits share | one insert in `urgent.sqlite`, `synchronous = FULL`, on the `urgent` thread, with no other writer. Locally 0.11 ms median [L], but macOS `fsync` does not flush the drive: on the station's disk an fsync'd commit is a few ms [E]. Not measured there |
| **Decode to durable. Budget: under 100 ms** | under 1 ms median, not fsync'd [L] | a few ms, fsync'd [E]; nothing can queue in front of it. Step 8 measures it in production before any switch |
| To the hub | wake `hub-forward`, `POST /api/station/events`: one round trip behind whatever the outbox already holds | `urgent-send` wakes on the commit, `POST /api/station/urgent` on a kept-alive connection: one round trip, alarms first. A cold connection adds a TLS handshake through the edge, 100 to 200 ms by `client.py`'s note |
| **Decode to hub. Budget: under 1 s** (the review's test) | the round trip, when the app is up and the outbox is empty | the decode step + a few ms + one round trip. The round trip itself is not in the evidence (0.3 ms is the client's own share against a local stub [L]); step 8 logs it per item (`last_ms`) |
| **App restarting** | not decoded: capture is down with it | the two rows above, unchanged: capture does not need the app |
| Hub in a deploy's handoff | waits in the outbox; sent when the new task serves | waits in `urgent.sqlite`; 503 + `Retry-After`, sent on the first retry after the new task serves. The handoff is expected to take 35 to 45 s (`docs/cloud.md`); 57 deploys in the window [P] |
| Uplink or hub down | waits in the outbox | waits in `urgent.sqlite`; an alarm for 30 min, then only the app's lane carries it |

A page:

| Step | Today | Split |
|---|---|---|
| A pair shown live | one FFT per frame (72 to 92 µs [L]); certain after A plus 1.5 s of B (`livepage.py`); `on_change` -> the state post, after a 50 ms wait that merges bursts (`StationLink._state`) | to the app: one socket hop, under 1 ms [E], inside that 50 ms. To the hub directly: a `page` item, a few ms to commit [E] and one round trip, at each change of the page (a handful per page) |
| **App restarting** | nothing: no live page, and the page's audio is lost | the hub still gets the live page from capture; the finished page follows when the app is back |
| `on_ours` -> the feed adapters poll | a direct call | one socket hop. The adapters run in the app, so this waits for it |
| The finished page | squelch close, then the hold window plus 0.5 s (`HOLD_SLACK_S`), looked at every 0.2 s; then the `page` event and STT (about 16 s a clip, the README's figure) | plus the spool round trip: WAV encode and write 0.3 to 3 ms [L], wake, read back: single-digit ms [E] |
| Close work for a 60 s page | `PageHold.add` 63.6 ms + `_keep_clip` 171 ms [L], on the receiver thread (the worker, with #767) | the same work, in the app: it can no longer delay any receiver |

A traffic transmission of 10 s: 43.1 ms of handler today [L], of which 32.7 ms ID decode (to capture's worker) and
8.6 ms voice filter (the app's). The spool adds the same single-digit ms. Speech-to-text dominates either way.

Nothing on the immediate paths gets a poll in front of it. What is added on the way to the app is one socket hop;
on the way to the hub, one fsync'd commit, both unmeasured on the station. `closed_mono` and `published_mono` in
every sidecar, a timing line in the reader and the sender's `last_ms` make them measurable before anything is
switched (steps 6 and 8 of §6).

## 6. Migration

Small PRs, each shippable alone, each reversible by a flag that is off by default. The mode is the one-word file
`/etc/sizeup/capture-mode`: `inline` (default, today's code path, and what unit tests use), `shadow`, `service`.
Owner: **R** the radio session (code), **N** the native session (schemas, conformance, compare tooling), **U** the
updater's owner, **H** the hub's maintainers. #767 and #753 land first; they are not part of this plan.

| # | PR | Mode it needs | Tests | Owner |
|---|---|---|---|---|
| 1 | The schemas and vectors of §10, in `contracts/capture/` | none | the vectors validate; every example in this document is one | N |
| 2 | Move `App._mdc`, `_mdc_at`, `_via`, `keyup_link` and the close-time facts into `radio/ids.py` and `radio/segmeta.py`; `App._segment` calls them. No behaviour change | none | the existing radio suites unchanged; a test that the facts equal what the handler computed before, on the synthetic key-up scenarios of `test_keyups.py` | R |
| 3 | `CallIngest`: prune `_seen` and `_pending` of entries whose files are gone (ingest deletes them); a bad sidecar no longer discards the rest of its poll (Q12) | none | the set stays bounded over 100,000 synthetic calls; a call still on disk is never taken twice; `test_p25*` unchanged | R |
| 4 | `radio/spool.py`: writer, reader, cursor, pruning, the periodic sync. Unused | none | atomicity (kill between the renames), recovery of `SEQ` and `EPOCH`, per-channel order, redelivery and dedupe, bounds, a full disk, the sync at `sync_s` and at stop; the schema vectors of PR 1 | R, N for conformance |
| 5 | Inline tee: in `inline` mode with a debug switch, the worker also writes each segment to a spool, and a test reader runs the handlers from it | `inline` | handler output from the spool equals handler output from the `Segment`, row for row, over the radio suites' scenarios (this is also where s16 against float32 shows, if it shows). `late` by age: 59 s live, 61 s late, an emergency at any age | R |
| 6 | PCM tee and `radiod.py` in shadow: the in-process receiver copies each chunk it read, with its arrival time, to a socket; `sizeup-radio --shadow` runs the same receivers from that instead of `rtl_fm`, owns no dongle, writes `spool-shadow/`, a status and the live socket, and sends nothing | `shadow` | `tools/spool_compare.py`: per channel, the same transmissions (start and end equal, since the tee carries the clock), bit-equal audio, equal IDs, levels and reasons. On the station it logs counts and differences only, never content | R; compare tool N |
| 7 | `radio/dongles.py`: the dongle half of `App` as a class the `App` delegates to, `reception.py` with it, whole (Q7). No behaviour change. Splittable: modes and auto-assign; groups; no-audio, probes, USB reset; the reception test | none | `test_antenna_groups`, `test_sdr_auto_assign`, `test_app`, `test_reception*`, `test_sdr_serial`, `test_claims` unchanged | R |
| 8 | `radio/urgent.py`: the file, the sender, the app's take-over. In-process first: in `inline` mode the `App` hosts the same writer and sender, so both budgets are measured in production while there is still one process | `inline`, and a hub with H1 | §4.3's idempotency table case by case against a stub hub; each failure class (503 with `Retry-After`, a timeout after the body went, 401, 404, a 413 cut to singles); expiry; offline, then back; the link off; the secrets absent from `os.environ` after start, from every child's environment, the status, the file and the log; `test_radio_emergency`, `test_fg_emergency`, `test_alarms` unchanged | R |
| H1 | The hub: `POST /api/station/urgent`, its dedupe store, the `urgent` capability, the allow-list row (§11) | - | §11 | H |
| 9 | `radio/control.py`: the wish file (desired state), the socket's requests and replies, the status file (observed state). In `inline` mode the `App` talks to its own `Dongles` through the same code (a loopback), so the request path is exercised before there are two processes | `inline` | every refusal of today's `set_mode` comes back as a reply ("parked", "taken", `confirm`); `squelch_apply` with a stale `expect` is refused, as #685 and #700 refuse it; a repeated `id` gets the first reply and is applied once; an expired request is not applied; a broken wish keeps the last good one; a wish that cannot be met shows as `held` or `waiting`; unknown keys and ops | R, N for conformance |
| 10 | The soak (Q12): a week of synthetic traffic, accelerated (the fake `rtl_fm` and fake call files on a fast clock), through `radiod.py` in service mode with a reader attached | - | resident memory flat after warm-up; these stay bounded: `CallIngest._seen` and `_pending`, the tap's per-carrier `last`, the `SegmentWorkers` queues, `LiveTalk`'s grant log, the socket's client queues, `urgent.sqlite`'s rows, the spool's items; no growth in open file descriptors or threads. It must pass before step 11 | R, N for the harness |
| 11 | The switch: the unit, `start-radio`, `radio.env`, `radio_sig`, the build directory, `health-check`, `sdr-serial`; in `service` mode the app starts no receivers and runs `spool-reader` into its own worker sets (§3.6). One station, by hand, with the admin's go | `service` | the end-to-end harness below; both drains of §3.6 (capture stopped with a transmission queued: it is in the spool; the app stopped with one queued: it comes again, once); `station-install` CI with the new unit; `.github/scripts/station-update.sh` extended: an app-only update leaves capture's pid, a capture change restarts it, a rollback stops it | R, U |
| 12 | The updater: app-only updates stop waiting for carriers; `radios_back` unchanged | `service` | `test_autoupdate` cases for both kinds of update | U |
| 13 | Cleanup: the shadow tee stays as the native daemon's test bench; inline mode stays for tests, macOS and all-in-one installs. Nothing else is removed until a second station has run `service` | - | - | R |

The end-to-end harness (constraint 9), synthetic audio only: a fake `rtl_fm` (as `tests/test_squelch_stuck.py` has)
feeding a real `radiod.py`, a real spool, socket, wish file and `urgent.sqlite`, and a real `App` in service mode,
both against a stub hub that implements `/api/station/urgent` and `/api/station/events` with the dedupe rules of
§4.3.

| Scenario | Asserts |
|---|---|
| **The app down during an emergency** (asked for in review) | capture in service mode, the app stopped. An MDC emergency key-up. The stub hub has the `urgent` item within 1 s of the burst's end. Then the app starts: its take-over event and the finished transmission (with `alarm_key`) arrive, the row and the clip attach, and the hub holds **exactly one alarm and made one push** |
| ... with the hub in a handoff | the hub answers 503 + `Retry-After` for 3 s first: delivered on the first retry after it serves; still one alarm |
| ... with the first answer lost | the hub applied it but the answer never arrived: the retry gets `duplicate`; one alarm |
| ... with the uplink down for 31 min | the alarm row is `expired` in capture; the app's lane carries the transmission; the hub's own age rule decides |
| A live page of ours with the app stopped | the hub's live paging state shows each pair as it decodes; the finished page arrives when the app is back and ends the live entry |
| A key-up with an ID; a page with a continuation | rows as in inline mode |
| A transmission read 61 s late | `late`: logged, transcribed, sent; no ATC or intel pre-alert. An emergency in it still attaches to its alarm |
| Capture restarted mid-transmission; both restarted | the open transmission is flushed and spooled before exit; nothing is handled twice |
| A pick refused because another pid holds the claim; a squelch Apply with a stale `expect` | the reply says why; nothing changed |
| The spool at its bound; the disk full | audio dropped oldest first, counted; the live and urgent paths unaffected |

Reversal at every step is the mode file and a restart of both units. Steps 2, 3, 4 and 7 are refactors with no flag;
they are reversible by revert.

## 7. Risks and open questions

### 7.1 Decided in review, and still open

The radio maintainers' answers to the first draft's questions (their comment on #773):

| # | Question | Decision | Where |
|---|---|---|---|
| Q1 | Must capture send the emergency lane itself? | **Yes.** Its own fsync'd SQLite file, not the outbox; its own sender to a new idempotent route with the station's token. The app still takes each item over | §4.3, §11 |
| Q2 | A live page as a durable event? | **Yes**, in the same lane, deduped by its page key | §4.3 |
| Q3 | P25 emergencies waiting for `App._call`? | **Not for a live signal**: a grant's or unit event's emergency bit goes urgent at once; the end-of-call `srcList` one stays in `App._call`, deduped. No live signal exists today, which this document now says | §4.3, §4.6 |
| Q4 | What is `late`? | **Age at the app's read**: under `LIVE_MAX_AGE_S` (60 s) live, older late with #753's rules. Emergencies are never gated | §4.1 |
| Q5 | Requests in files or on the socket? | **Both, by kind**: the wish file is desired state, the socket carries requests that need an answer, the status file is observed state | §4.2, §4.4, §4.5 |
| Q6 | `PageHold` | **The app** | §3.1 |
| Q7 | `reception.py` | **Whole into capture**; the app shows results from the status | §3.3, §4.5 |
| Q9 | Audio format and `fsync` | **s16 WAV; no `fsync` per item**, the spool synced every few seconds and at stop | §4.1 |
| Q12 | Anything relying on the daily restart? | **Yes, bound it**: prune `CallIngest._seen`; the rest is bounded or persisted already. A week-long accelerated soak before `service` | §6 steps 3, 10 |
| - | The restart count | About 12 with the dongles is accepted; the sender's client makes it about 14 | §3.3 |
| - | `SegmentWorkers` on both sides | Agreed: per label, shared across receiver instances, drained on stop, in capture | §3.6 |

Not answered in the review, kept as assumed: Q8 (a hard link to trunk-recorder's WAV; whether a per-call hook can
replace the size-stable wait is unchecked), Q10 (the tap's reads of the squelch's private state stay inside capture),
Q11 (capture's log wording unchanged, the unit name changes; other readers by unit name not yet listed).

Still open, raised by the decisions:

| # | Question | For | What this document assumes |
|---|---|---|---|
| Q-A | The urgent lane's worst case is now the hub's deploy handoff (expected 35 to 45 s, 12.8 deploys a day in the window). Accept it, shorten the handoff, or let a standby hub take urgent items before it leads? | hub, admin | Accepted for now; stated in §4.3 |
| Q-B | A live P25 emergency signal does not exist. Add a line for the grant's emergency bit to the station's trunk-recorder patch? Not checked against the pinned source. A radio with a stuck emergency bit would then flag every grant | radio | The lane has the slot; capture would send at most one per system, talkgroup and radio per `REPEAT_S`, and the hub's stuck rule decides |
| Q-C | Group edits and the ADS-B dongle are desired state (Q5), but today `set_antenna_groups` and `set_adsb_receiver` can refuse (`confirm:`, a dongle another app holds) from facts only capture has. This document adds a `check` request the app makes before it writes the wish | radio | `check` exists; an edit that later cannot be met shows in the status |
| Q-D | A keep-warm request every 50 s so the first alarm never pays a TLS handshake? | radio, hub | Off by default; step 8's `last_ms` decides |
| Q-E | Jobs that capture assigns itself (auto-assign, stand-ins) are its own state. For a switch back to inline the app mirrors them from the status into `sdr_modes.json` | radio | The mirror |
| Q-F | Live page items: every page `LivePaging` shows (what `/state` carries today), or only ours? | radio, hub | Every page |
| Q-G | The route's row in `docs/nodes.md` §3: `sz1` with role `radio-receive` (as `/api/station/events`), or `sz1` always, so a node whose role was just removed can still deliver an alarm it already decoded? | nodes design, hub | `radio-receive` |
| Q-H | `sizeup-station secrets` restarting `sizeup-radio` to hand it a rotated token: acceptable, or should capture re-read `radio.env` on a signal? | radio, admin | A restart |

### 7.2 For the admin

| # | Question |
|---|---|
| A1 | The station page during an app restart: radios keep recording, and emergency alarms and live pages still reach the hub (§4.3), but the station's own page and "talking now" are away for about 4 s, and transcripts for longer. Today everything is away for about 8 s and the audio is lost. Is that the trade wanted? |
| A2 | One more unit to watch (`sizeup-radio`), one more row in `sizeup-station status`, two more Health items (spool growth, the urgent sender), one more secrets file (`/etc/sizeup/radio.env`, written by `sizeup-station secrets`). The first switch is by hand on the one station |
| A6 | An emergency keyed during a hub deploy still waits for the hub's handoff, expected 35 to 45 s, about 13 times a day at the window's rate (Q-A). The split removes the station's gap, not the hub's |
| A3 | Disk: the spool adds one sidecar (a few KB) per transmission and no second copy of the audio, if the app adopts the spool's file as `<id>.src.wav` by hard link. At 115 speech-to-text clips an hour [P] and an assumed 6 s each that file is about 0.5 GB a day [E], which is written today too. Under WSL2 it lands in the same virtual disk as now |
| A4 | The other app that shares the dongles sees `app: "sizeup"` claims under capture's pid, which changes about a quarter as often (§3.3). A capture restart still drops every claim for a few seconds, as each station restart does today. Its side needs no change (`sizeup/radio/claims.py`) |
| A5 | WSL2: nothing new is required of it. Unix sockets, `/run`, hard links and systemd units are already used by `sizeup-stt-encode` and `sizeup-trunk-recorder`. USB attach order after a Windows restart is as today; capture inherits `SDR_SETTLE_S` |

### 7.3 Stakeholders outside the radio session

| Area | In capture's DSP | Maintained by | What changes for them |
|---|---|---|---|
| Airband multichannel (`channelize.py`, `am.py` `AmReceiver`, the A/B test) | yes | the airband work (#275, #307) | the A/B becomes a request; its numbers come from the status |
| ATC (`atc_hours.py` schedules, `_atc` pre-alerts, the ATC vocabulary) | the schedule only | the ATC work (#233, #260) | pre-alerts stay in the app and follow `late` (60 s) |
| The hub (`sizeup/cloud/hub.py`, `cad/alarms.py`) | no | the hub's maintainers | the new route and its dedupe store (§11) |
| `airport_watch.py`, `cad/airport_alerts.py` | no; they consume airband rows | the airport work | none, beyond `late` |
| Signal metrics (#605, #685) | the sampler | the metrics work | `floor` and `cc` arrive through the spool; `tx` from sidecars |
| Nodes (#758) | no | the nodes design | `channels[].run` is the per-channel state it asks for |

### 7.4 Risks

| Risk | Mitigation |
|---|---|
| The dongle code leaving `app.py` (step 7) is the largest and least mechanical move | no behaviour change, split in four, behind the existing suites, before any process boundary exists |
| A second process holds the hub's credentials and speaks to the hub | one route, one kind of request, the secrets out of the environment at start, §7.5 |
| The urgent lane and the app's lane both deliver every alarm | the dedupe store on the hub, asked first; the idempotency table of §4.3 is the hub's test list |
| An fsync'd commit on the station's disk is slower than assumed | measured in step 8 with one process; the budget is 100 ms and the estimate a few ms |
| Capture runs for days where it restarted 12.8 times a day | `CallIngest._seen` pruned; the soak of step 10 |
| The closure grows back | the list, the test, the build directory (§3.3) |
| A stale spool after a long outage fires old alerts | `late` by age; the alarm code's own age rule |
| The evidence's restart counts predate #712 | the gain is smaller than 55 to 7 suggests; it is still every hub-only change |

### 7.5 Security

| Item | Entry |
|---|---|
| **The new hub route**, `POST /api/station/urgent` | bearer resolved before the body is read (`Hub._principal`); the payload's `station` must be the token's (`id_mismatch`); the leader only (503 otherwise); a body of at most 64 KB and 32 items, read by `Content-Length`; every string field length-capped before use; the rig derived on the hub, never taken from the station |
| What the route adds for someone holding the station's token | nothing new: the same token can already raise an alarm with an `alarm` event and set the live page with `/api/station/state`. It adds a second door to the same room, so it gets the same lock: the allow-list |
| The allow-list (`docs/nodes.md` §3, #758) | a new row is needed there: `/api/station/urgent` (alarms and live pages ahead of the app's lane): `sz1` + role `radio-receive`; `sw1` never (Q-G). That table's own invariant applies: a test lists every `/api/station/*` route and fails when one has no row. `Hub.STATION_ROUTES` gains the route |
| `contracts/routes.json` | the route is added there with its credential (`sz1`), role, body cap, "not seen" and "idempotent by `key`, `rev`". That file is ADR 0004's; it is not on `main` or in #765 yet |
| Cloudflare Access | no change: the existing `/api/station/*` application and the station's own service token. No new Access token, no wider policy |
| **The urgent sender's credentials** | the station's `sz1` token and its Access service token, from `/etc/sizeup/radio.env` (root 0600, those three variables only), taken out of `os.environ` at start so no child inherits them; never in the wish, the status, a frame, the spool, `urgent.sqlite` or a log line (§4.3). `sizeup-radio.service` otherwise reads no secret, and not `station.env` |
| What capture may send | one host (the wish's `hub.url`), two paths (`/api/station/urgent`, and `/api/station/hello` if keep-warm is on). A test pins that the sender builds no other request |
| The live socket | 0660 in a 0750 directory, peer uid checked. Requests change dongle state, so only the `sizeup` uid may make them: the same trust as the app has today. No request takes a path or a command line |
| Files | the wish, the status, the spool and `urgent.sqlite` are the `sizeup` user's, not world-readable. The spool holds raw radio audio under the clips' own retention; nothing in it leaves the station except by the app's lanes |
| What an urgent item carries | a channel label, radio IDs, a unit alias, tone names: what the `alarm` event and the live page carry today, over the same TLS and Access path. No audio, no transcript |
| Logs | capture's lines reach the hub through the app's shipper and its scrubber, which already holds the same secrets. `last_error` is a status and the hub's `reason` |
| Replay | a request sent twice is a `duplicate`; an alarm item older than 30 min raises nothing (`alarms.MAX_AGE_S`) |

## 8. What a native radio daemon would implement

Only the contract of §4. It would not need Python, `config.toml` or the CAD.

| It must | From |
|---|---|
| Read the wish file; keep the last good one | §4.4 |
| Write the status file every 0.5 s | §4.5 |
| Publish spool items: audio, then the sidecar by rename; keep `SEQ` and `EPOCH`; sync; prune behind the cursor; hold the bounds | §4.1 |
| Serve the live socket with #709's framing: frames out, requests in, replies | §4.2 |
| Insert `urgent` rows, fsync'd | §4.3 |
| Honour and write dongle claims | `sizeup/radio/claims.py` |
| Pass the conformance vectors of PR 1 | §10 |

Three things make that smaller than it sounds, and one makes it larger:

- The urgent **sender** need not be native. It is a loop over a SQLite table and one HTTPS route; it can stay a
  small Python part of `sizeup-radio` beside a native receiver, which keeps TLS, the hub's auth and the two secrets
  out of the new code. The native part only inserts rows.
- The dongle policy (antenna groups, stand-ins, no-audio, the reception test) is the bulk of §4.2's requests and
  §4.4. It can stay a Python supervisor inside `sizeup-radio` that hands a native receiver "this channel on this
  device index", the way `trunkrecd.py` supervises trunk-recorder. The inner seam would be a second, smaller wish.
  Not designed here.
- trunk-recorder stays what it is; the daemon would not take over P25.
- The squelch is the hard part: about 60 tuned constants and 278 test cases (ADR 0001). The spool does not make that
  smaller; it makes it testable.

Shadow test: the PCM tee of step 6 is the bench. The native daemon reads the same chunks with the same arrival
times as the Python service, owns no dongle, sends nothing, and writes its own spool and its own urgent file.
`tools/spool_compare.py` then compares the two item by item: the same transmissions with the same `start` and `end`,
bit-equal audio, the same `ids` and `alarm_key`, the same `reason` and `keyup`, levels within a stated tolerance,
and the same urgent rows (`key`, `rev`, `kind`, `data`). Recorded tees (synthetic, or kept on the station and never
copied off it) replay the same comparison in CI. A daemon is switched in only after the admin's go, by the mode
file, and switched back the same way.

## 9. Where the evidence and the code disagree

| Evidence | Code on `main` |
|---|---|
| "Capture deaf about 8 s per restart", and the notes' "the true gap is at most that" | the figure is the updater's poll of row states from before `activate()`; a row reads `ok` before the first audio, and the squelch then needs 2 s (§1). Neither an upper nor a lower bound |
| The capture closure is 16 modules | a capture that runs the live tap also needs `live.py` and `livepage.py` (injected into `LiveTap`, so no import shows it); with page assembly in the app it does not need `page.py` |
| "Capture restarts 55 to 7" | 7 is the DSP alone. With the dongles owned by capture the proxy gives about 12, and about 14 with the urgent sender's client (§3.3) |
| ADR 0002: capture does "clip writing" | here it writes the raw audio; the filtered clip stays in the app (§3.2) |
| The unit inventory | lacks `sizeup-stt-encode` (#709, merged with #701 a few hours after the evidence's commit) |
| The hub's jobs call "13 functions" | `CALLS` has 15; none is named "set squelch" or "set radio role" (squelch rides in `set_channel_prefs`) |
| "The 300 s quiet wait can go" | #712 already replaced it (settle 10 min, holds, a 45 min ceiling); §3.5 says which holds the split makes unnecessary |
| Line numbers | drifted throughout (`App._segment`, `CallIngest.prime`, the updater); this document cites functions |

## 10. Contract artifacts

What the native session writes from this document (ADR 0004), in `contracts/capture/`. Every schema allows
additional properties (unknown keys are ignored); "required" is what a reader may rely on. Types and units are in
the section named.

| Artifact | File | Fields from | Required |
|---|---|---|---|
| Spool sidecar, `kind: "segment"` | `spool-sidecar.schema.json` (`oneOf` by `kind`) | §4.1 | `v`, `kind`, `id`, `seq`, `epoch`, `channel.label`, `channel.kind`, `start`, `end`, `published`, `reason`, `levels`, `ids`, `alarm_key`, `audio` (object or null); with `audio`: `file`, `format`, `encoding`, `rate_hz`, `channels`, `samples`, `bytes` |
| Spool sidecar, `kind: "call"` | the same file | §4.1, §4.6 | the common ones, and `system`, `talkgroup`, `turns`, `emergency`, `encrypted`, `tr` |
| Spool sidecar, `kind: "metrics"` | the same file | §4.1 | `v`, `kind`, `id`, `seq`, `epoch`, `published`, `records` |
| Spool cursor | `spool-cursor.schema.json` | §4.1 | `v`, `epoch`, `seq` |
| Spool layout and behaviour | `spool.md` with vectors: the names, audio before sidecar, `EPOCH`, `SEQ` recovery, who deletes, the sync | §4.1 | - |
| Urgent file | `urgent.sql` (the DDL of §4.3, byte for byte) | §4.3 | - |
| Urgent item (a row's `data`, and an item of the request) | `urgent-item.schema.json` (`oneOf` by `kind`) | §4.3 | the item: `key`, `rev`, `kind`, `at`, `event_at`, `data`. `data` of an alarm: `source`, `start`, `radio_id`, `emergency`, `emergency_via`. `data` of a page: `channel`, `started`, `pairs`, `phase`, `ended` |
| `POST /api/station/urgent`, request | `urgent-request.schema.json` | §4.3 | `v`, `station`, `items` (1 to 32) |
| `POST /api/station/urgent`, answers | `urgent-response.schema.json` | §4.3 | 200: `ok`, `results` (each `key`, `rev`, `status`); 4xx: `error`, `reason` |
| Socket, capture to client | `socket-event.schema.json` (`oneOf` by `t`) | §4.2 | `v`, `t`, `at`, and per `t` the fields of its row |
| Socket, client to capture | `socket-request.schema.json` (`oneOf` by `t` / `op`) | §4.2 | notices: `v`, `t`; requests: `v`, `t: "request"`, `id`, `op`, `args`, `expires` |
| Socket, replies | `socket-reply.schema.json` | §4.2 | `v`, `t: "reply"`, `id`, `ok`; with `ok: false`: `error` |
| Socket framing | `socket.md` with byte vectors: `>II`, the header and payload limits | §4.2, `sizeup/cloud/sttencode.py` | - |
| Wish file | `wish.schema.json` | §4.4 | `v`, `seq`, `mode`, `paths`, `channels`, `dongles` |
| Status file | `status.schema.json` | §4.5 | `v`, `at`, `pid`, `contract`, `wish_seq`, `channels`, `sdrs`, `spool`, `urgent` |

Outside `contracts/capture/`:

| Where | Change |
|---|---|
| `contracts/routes.json` | add `POST /api/station/urgent`: credential `sz1`, role `radio-receive`, body cap 64 KB, 32 items, not "seen", idempotent by (`key`, `rev`), 503 on a non-leader. The file is ADR 0004's and does not exist on `main` or in #765 yet |
| `docs/nodes.md` §3, the route allow-list | a row for the route: `sz1` + role `radio-receive`; `sw1` never (Q-G). Its "every `/api/station/*` route has a row" test then covers it |

Vectors to start from (all invented):

```json
{"v": 1, "kind": "segment", "id": "CHA-1900000000123", "seq": 12345, "epoch": "3f9a1c0b7d2e4a61",
 "channel": {"label": "CH A", "kind": "traffic", "mhz": 450.0125, "mod": "nfm"}, "sdr": "sdr-a",
 "start": 1900000000.123, "end": 1900000006.473, "published": 1900000006.52,
 "closed_mono": 5120.44, "published_mono": 5120.49, "boot": "b1946ac9", "replayed": false,
 "reason": "quiet", "partial": false, "cont": false, "hold_until": 0, "breaks": [],
 "keyup": null,
 "levels": {"quieting_db": 31.2, "q_open": 30.8, "q_close": 29.9, "hiss_db": -41.5, "floor_db": null},
 "speech_s": 4.1,
 "ids": [{"id": "7001", "system": "mdc", "emergency": true, "from": "live"}],
 "id_burst_seen": true, "mdc_crc_failed": 0, "bursts": 1,
 "alarm_key": "CHA-live-1900000000123", "tones_live": [],
 "audio": {"file": "000000012345-CHA-1900000000123.wav", "format": "wav", "encoding": "s16le",
           "rate_hz": 16000, "channels": 1, "samples": 101600, "bytes": 203244},
 "bursts_audio": "000000012345-CHA-1900000000123.mdc.wav",
 "capture": {"impl": "python", "build": "0123abc", "contract": 1}}
```

```json
{"v": 1, "station": "st-north", "epoch": "9c2d7e10a4b35f68", "sent_at": 1900000000.41,
 "items": [{"key": "CHA-live-1900000000123", "rev": 0, "kind": "alarm", "at": 1900000000.39,
            "event_at": 1900000000.123,
            "data": {"source": "CH A", "start": 1900000000.123, "end": 1900000000.39, "radio_id": "7001",
                     "ids": [{"id": "7001", "system": "mdc", "emergency": true, "from": "live"}],
                     "unit": "UNIT A", "emergency": true, "emergency_via": "MDC"}},
           {"key": "page:PAGE 1:1900000100.25", "rev": 2, "kind": "page", "at": 1900000104.9,
            "event_at": 1900000100.25,
            "data": {"channel": "PAGE 1", "started": 1900000100.25, "phase": "voice", "ours": true, "ended": false,
                     "until": null,
                     "pairs": [{"a": 600.9, "b": 716.1, "names": ["Example Co 1"], "ours": true,
                                "at": 1900000100.25}]}}]}
```

```json
{"ok": true, "v": 1, "station": "st-north",
 "results": [{"key": "CHA-live-1900000000123", "rev": 0, "status": "applied"},
             {"key": "page:PAGE 1:1900000100.25", "rev": 2, "status": "applied"}]}
```

```json
{"v": 1, "t": "request", "id": "r-5f3a9c12", "op": "pick", "by": "person", "at": 1900000200.0,
 "expires": 1900000210.0, "args": {"sdr": "sdr-b", "job": "CH A", "confirm": false}}
```

```json
{"v": 1, "t": "reply", "id": "r-5f3a9c12", "ok": false, "error": "refused: parked", "confirm": false,
 "at": 1900000200.02}
```

## 11. Work for the hub's maintainers

Separate from the radio work, and needed before step 8 can reach a real hub. Until a hub advertises `urgent`,
capture's sender idles and the app's lane carries everything, as today.

| # | Piece | What |
|---|---|---|
| H1a | The route | `POST /api/station/urgent` in `sizeup/cloud/hub.py` (`Hub.handle`, `STATION_ROUTES`): `_principal`, the leader check, `_claim` on `station`, the caps of §4.3, the answer shapes of §4.3. Not `_seen()`; its own `urgent_seen` time |
| H1b | The dedupe store | a table in `hub.sqlite`, made at its first write (never at start: `tests/test_startup_writes.py`): `(station, key)` -> `kind`, highest `rev`, first seen, the alarm or page it became. Asked before anything is raised. Kept at least 24 h. It must survive a handoff, so it is replicated with the rest |
| H1c | Alarms | an alarm item goes through `_apply_alarm`'s path with `id` = `key`. The take-over by `alarm_key` must leave the dedupe store able to answer `duplicate` for the old key afterwards. `event_at` labels an alarm raised late, as #753 proposes for late calls |
| H1d | P25 live against end of call | one emergency for system + talkgroup + radio within `REPEAT_S`, the end-of-call row taking the live one over. Unused until a live signal exists (Q-B); the stuck-flag rule must see live items too |
| H1e | Live pages | a page item sets the station's live paging state by `key` and `rev`, with `until` as its expiry; `/api/station/state`'s `paging` merges by `key`, newer wins; a `page` event carrying `live_key` ends the live entry. What a live page of ours triggers on the hub (a push, a pre-alert row) is the hub's to decide: today it is display only |
| H1f | The capability | `urgent` in the state answer's `caps`, so the app can tell capture (the wish's `hub.caps`) |
| H1g | The allow-list | the row in `docs/nodes.md` §3 and in `contracts/routes.json` (§10); the route-table test |
| H1h | Health | the station's `urgent` block from its status (the sender's state, the oldest unsent alarm, `rejected`, `expired`); the hub's own count of items by status; an alert when the sender is `refused` or an alarm arrived more than a few seconds after its `event_at` |
| H1i | Tests | each row of §4.3's idempotency table; a duplicate after the take-over; a request during a handoff applies nothing; 32 items, and the 33rd refused; an oversized body; another station's token; a `sw1` token (403); a page `rev` out of order; the stub hub the station's tests use is generated from the same rules |
| H1j | The handoff | whether urgent items can be taken sooner than the rest during a deploy (Q-A). Not needed for the first version |
