# Capture as its own service: `sizeup-radio` behind a spool contract (design)

**Status: boundary and details confirmed by the radio maintainers on 2026-11-18 (#773), and again at 6a777c98 with
their Y1/Y5 answers (the P25 key from the grant's own time; what a wall step is). Revised since, after a second
adversarial review of the models (#783) and the clock finding of #788, and confirmed again by the radio maintainers
at d05339e3 (§7.1, the row for the second review). Revised again after a third adversarial review of the models: the
socket's size limits, a call from before an All clear, what the app's lane delivered (§7.1, the row for the third
review), confirmed by the radio maintainers at 65bcd699. Revised once more after a final verification of the
models: the skew path's conditions, and `urgent_delivered` only for an alarm the hub has (§7.1, the row for the final
verification), confirmed by the radio maintainers and the main session at fa23a6ee, with the main session's
`suppressed` outcome. #765 is merged. Implementation has not started. Pending: the hub-side route and the measurement of its handoff window (§11, #776), and A7, the
admin's decision on a late emergency (a recommended answer is recorded).**

Design, no code. Revised twice after the radio maintainers' reviews (their decisions are in §7.1) and once after an
independent second read, which found the faults §4.3's "What can go wrong" and §4.7 now answer. Follows ADR 0002
(`docs/native/adr/0002-no-native-station-agent-yet.md`, #765): no native agent now; radio capture leaves the
`sizeup-station` process and becomes its own service, in Python first. The same contract is the seam a native radio
daemon would implement later. The radio code's maintainers own the boundary decision; this document is the boundary
they approved, and it marks what it does not know.

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
| 1 | Immediate paths stay in capture, never behind a spool poll: live MDC / FleetSync IDs and the emergency alarm (#690, #698, #767), page tones and the live page, "talking now", P25 live grants. A delivery path that works while the app restarts; under 100 ms from decode to durable (first written as "to the outbox"; decided since: capture's own file); no duplicates when the app also sees the segment | §3.2, §4.2, §4.3, §5 | Decided: capture's own fsync'd file and its own sender (§4.3). The hub's deploy handoff bounds the lane: accepted for the first version, to be measured (#776) |
| 2 | One owner of dongles: capture. Intent from the app over a versioned control channel with acks; per-channel running state back | §3.3, §4.2, §4.4, §4.5 | Decided: desired state in the wish, requests on the socket, observed state in the status; `check` is advisory and capture validates on apply |
| 3 | Spool: atomic, ordered per channel, idempotent ids, full metadata, `late`, at-least-once with dedupe, bounded with a Health alarm, survives both restarting; metrics the same way | §4.1 | Decided: `late` is age at the app's read, 60 s; emergencies never gated |
| 4 | P25: `CallIngest` and the #747 mark live where the spool lives; one owner of the mark; watched-talkgroup lines (#718) and split counts stay with the supervisors | §4.6 | Agreed assumption: a hard link to the call audio (Q8) |
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
| `sizeup/radio/reception.py`, `probes.py`, `systems.py` | `reception.Tests` takes the `App` and drives its dongle state | capture's closure, `reception.py` whole (decided, Q7): it is dongle lending and measurement, and the app shows its results from the status file. It cannot move as it is: the table below says what is cut first. `systems.py` splits: `load`, `discover`, `P25System` are config; `Activity` and tier logic need `TalkgroupPrefs` and stay in the app, so `sourcekeys.py` and `tiers.py` stay out of the closure |
| `sizeup/janitor.py` | the app's retention | capture's closure too: `reception.py` cleans a probe's capture dir with `janitor.clean_system`. Standard library only; no commit in the window |
| `sizeup/radio/page.py` | imported by `app.py`; in the evidence's 16 | **not** in capture's closure: `PageHold` runs in the app (decided, Q6) |
| `sizeup/cloud/client.py` (`HubClient`, `Backoff`, `HubError`), with `sizeup/cloud/__init__.py` | the app's hub link | capture's closure too, for the urgent sender (§4.3): one implementation of the bearer and Access headers, not a second copy. Standard library only |
| `App._mdc`, `_mdc_at` (#767), `_fm_emergency`, `_via`, `keyup_link`, `make_receiver`, `_wire_receiver` (#767), `_heard_elsewhere`, `_watch_fm_once` | `sizeup/app.py` | new `sizeup/radio/ids.py` (the decode), `radio/segmeta.py` (the sidecar's facts), `radio/radiod.py` (the service) |
| The dongle half of `App` (about 1,500 lines: `_load_modes` to `_set_group_mode`, `sdr()`, `_start_mode`, `_stop_mode`, the no-audio and probe methods, `set_antenna_groups`, `set_adsb_receiver`) | methods of `App` | new `sizeup/radio/dongles.py`, class `Dongles`, used in-process in inline mode and by `radiod.py` in service mode |
| New | | `radio/spool.py` (writer and reader), `radio/control.py` (wish, status, requests and replies), `radio/liveout.py` (the socket), `radio/urgent.py` (the file, the sender) |
| `metrics.Sampler` | `sizeup/cloud/metrics.py` | the sampling half moves to `radio/`; `Collector` and `MetricsLink` stay |

Capture must never import `sizeup.config`, `sizeup.app`, `sizeup.stt`, `sizeup.cad.*`, `sizeup.channels`, or
anything of `sizeup.cloud` but `client.py`. Everything it needs from the config arrives in the wish file (§4.4). Two
helpers it uses live in `config.py` today (`airband_owner`, `P25_JOB`) and move to a small module.

What `reception.py` reaches for today that capture may not import, each found by reading it, and what replaces it:

| Today in `reception.py` | Why it cannot stay | Replaced by |
|---|---|---|
| `from sizeup.app import P25_MODE`, `import sizeup.app as appmod` (`SWITCH_PAUSE_S`), both inside functions | capture never imports `sizeup.app` | the two constants move to `radio/dongles.py` |
| `app.assign`, `app.set_by`, `app._mode_lock`, `app.switching`, `app._start_mode`, `app._stop_mode`, `app._save_modes`, `app._dongle`, `app.modes`, `app.sdr` | they are the `App`'s dongle state | the `Dongles` object of step 7, which owns exactly these |
| `app.tg.record(tg)`, `app.tg.tier(tg, name)` (`TalkgroupPrefs`), `from sizeup import tiers` (`ALERT`, `RECORDED`) | talkgroup choices are Settings state in the app; `tiers.py` would join the closure | the wish carries, per system and talkgroup, the two facts the test needs, already resolved by the app: `alert` and `kept` (§4.4 `p25.talkgroups`). `tiers.py` stays out |
| `from sizeup import janitor`, `probes`, `recorder`, `sdr`, inside functions | no lazy imports in the service | imported at the top; `janitor.py` joins the closure |
| `app.cfg` | capture has no `Config` | the wish's `p25` block and capture's own read of the root-owned config (§4.3) |

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
| ... + `cloud/client.py`, `cloud/__init__.py` for the urgent sender, and `janitor.py` (**the proposal**) | 14 |
| ... if `reception.py` keeps importing `tiers.py` instead of getting the two facts through the wish | 15 |
| ... if `systems.py` is not split either, so `sourcekeys.py` comes too | 17 |
| The proposal without importing `trunkrecd.py` | 13 |
| `sizeup/app.py` alone | 34 |

So constraint 2 (capture owns the dongles and the reception test) costs about 5 more restarts in the window than
the bare DSP, and the sender's client 2 more: the proposal lands at about 14 where the station restarted 55 times
[E from G]. The review accepted 12 with the dongles; 14 is that plus the decision that capture sends. Moving
`reception.py` whole does not raise it, provided its reach-ins are cut as above; left as they are it would pull in
`sizeup.app` and with it 34 of the 106. Add the
dongle code that leaves `app.py` (it changed with #609 in the window; #702 and #710 merged within an hour after it)
and contract bumps. `trunkrecd.py` is the largest single contributor (5 commits, 1 of them not shared with another
listed file); importing only a small constants module from it would remove that.

### 3.4 What runs where after the split

| Thread or process | Unit |
|---|---|
| `rtl_fm`, `rtl_sdr`, the channelizer; `nfm-<label>`, `segment-<label>`, `sdr-watch`, `talking`, `p25-ingest`, `trunk-recorder-log`, `radio-metrics`; new `urgent`, `urgent-send`, `urgent-pages`, `live-out`, `control` | `sizeup-radio` |
| `spool-reader` (new), `segment-<label>` again for the handlers (§3.6), `keyup-pl`, everything else in §2.1 | `sizeup-station` |
| trunk-recorder and its supervisor, unchanged | `sizeup-trunk-recorder` |

### 3.5 The unit, its signature, start order, health

`deploy/station/systemd/sizeup-radio.service`, modelled on `sizeup-trunk-recorder.service`:

| Setting | Value | Why |
|---|---|---|
| `User`, `Group`, `SupplementaryGroups` | `sizeup`, `sizeup`, `plugdev` | as the station |
| `Environment` | `HOME=/var/lib/sizeup`, `SIZEUP_STATE_DIR=/var/lib/sizeup`, the `rtl-sdr` `PATH` and `LD_LIBRARY_PATH` | the claims directory follows `HOME` |
| `LoadCredential` | `station.env:/etc/sizeup/station.env` | the station token and the Access service token for the urgent senders, from the same root-0600 file the app uses; never an `EnvironmentFile`, so never in capture's environment (§4.3, §7.5). The file must exist, even empty: the installer sees to it (§4.3) |
| `ExecReload` | `+/usr/local/lib/sizeup/radio-credentials` | as root: hands the current values to capture over the socket. No receiver is touched (§4.3) |
| `ExecStart` | `/usr/local/lib/sizeup/start-radio`: resolves `/opt/sizeup/radio` (a link to a build directory), runs its venv's `python -m sizeup.radio.radiod` | the code under a running capture never changes; it is independent of `/opt/sizeup/current` |
| `RuntimeDirectory` | `sizeup-radio`, mode 0750 | `status.json`, `live.sock` |
| `Nice`, `CPUWeight`, `IOWeight` | -5, 500, 500 | constraint 7: above whisper (100), the updater (20) and the STT encoder (10) |
| `KillMode`, `TimeoutStopSec` | `control-group`, 45 s | `rtl_fm` is its child. 45 s is the station unit's own value, and its reason moves here with the reception test: a running test's dongle goes back on its job first, which can wait on the trunk-recorder service (#410). Then, on SIGTERM: stop receivers, flush open transmissions (`reason: "stopped"`), `drain()` the workers (#767, 10 s at most), give the senders 2 s, sync the spool, write a final status |
| `Restart`, `RestartSec` | `always`, 5 s | as every other station unit |
| `RestartPreventExitStatus` | 78 | capture exits 78, and is not restarted, when it should not run: the mode file does not say `service`, it could not get `radio.lock` within 60 s, or the current release does not ask for it (§4.7). The shadow instance of step 6 is a separate unit instance with its own lock and its own files; it runs only while the mode file says `shadow` |

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
| Health | `status.json` carries `at_boot`; a reader on the same boot treats one more than 15 s old as down (`trunkrecd.read_status`'s rule, on the boot clock). `health-check.sh` restarts `sizeup-radio` after 3 stale looks, as it restarts the station after 3 failed `/healthz`. Its rule "a unit that is not active was stopped on purpose" does not hold here: while the mode file says `service`, an inactive `sizeup-radio` is started (§4.7) |
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
| Drain on stop | on SIGTERM: stop the receivers, `drain()` each receiver and each set (10 s for all, inside `TimeoutStopSec` 45 s), close them. Drained means **every transmission that ended or was flushed is a complete spool item**. Then the final status and the claims released. What does not finish in time is logged and lost, as in #767 today | in `App.stop()`: stop `spool-reader`, `drain()` each set (10 s for all), write the cursor, then finalise the held pages, then `_stt_handoff`. Drained means **handled and acknowledged**. What does not finish stays unacknowledged in the spool and comes again at the next start: nothing is lost on this side |
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
of `p25_recorder.json` is not repeated); times are float seconds. A size in bytes is the UTF-8 length of the JSON exactly as it is sent (a frame's header, a request's body, or one item as encoded inside either); every byte limit in this contract is counted that way. A new field carries its unit in its name (`_s`,
`_db`, `_hz`, `_bytes`, `_boot`). Fields carried over from today's `Segment`, the call JSON and the state post keep
today's names, and their unit is in the table that lists them: `start`, `end`, `at`, `since`, `until`, `hold_until`
and a turn's `time` are Unix seconds, `q_open` and `q_close` dB, `breaks` seconds into the audio. A wall-clock time
is Unix seconds (under 10^11, never milliseconds); a name ending in `_boot` is seconds since boot (under 10^9). `v`
is the version of that one file or message; `contract`, in `hello`, the status and a sidecar's `capture`, is the
major version of the whole contract. They are separate numbers, both 1 today. Every limit in this contract is
exclusive on the inside: an age under the limit is inside, an age of exactly the limit is outside (60 s is late, 120
s is expired, an alarm exactly at the hub's cutoff, `MAX_AGE_S` × 1.1, is stale, a bit last set exactly 10 min ago
is a new press). A slack
written as "±" is inclusive. A change that only adds optional keys bumps neither. A bump of `contract` restarts both
sides.

Paths (station defaults; every path capture uses is given to it in the wish file):

| Path | Writer | Readers |
|---|---|---|
| `/var/lib/sizeup/state/radio_wish.json` | app | capture |
| `/run/sizeup-radio/status.json` | capture | app, the CLI, `health-check` |
| `/run/sizeup-radio/live.sock` | capture (listens) | app, tools |
| `/var/lib/sizeup/spool/` | capture (items), reader (its cursor file only) | app, the compare tool |
| `/var/lib/sizeup/state/urgent.sqlite` | the holder of `radio.lock`: capture, its only opener | nobody else; the app gets the rows over the socket |
| `/var/lib/sizeup/state/radio.lock` | whoever owns the dongles: capture in service mode, the app in inline mode (a lifetime `flock`) | - |
| `/etc/sizeup/station.env` | root (`sizeup-station secrets`), 0600, unchanged | systemd, for the app and (`LoadCredential=`) for capture |
| `/etc/sizeup/capture-mode`, `/etc/sizeup/urgent-mode` | root, one word each | capture, the app, the installer |

### 4.1 The spool

Layout. The spool is on the same filesystem as `captures/`, so audio moves by hard link, never by copy.

```
spool/
  EPOCH                         the spool_epoch: 16 hex, made with the directory; a new spool resets cursors
  SEQ                           capture's last sequence number
  tmp/                          writes in progress; wiped by capture at start
  items/
    000000012345-CHA-1790000000123.wav         audio, renamed first
    000000012345-CHA-1790000000123.mdc.wav     the cut-off ID bursts, optional
    000000012345-CHA-1790000000123.json        the sidecar, renamed LAST: it is the completion marker
  cursor.app.json               the reader's, written by the reader only
  quarantine/                   the reader's too: items whose handler kept failing (below)
```

| Rule | |
|---|---|
| Atomic write | every file is written under `tmp/` and renamed into `items/`. Audio first, sidecar last. A reader lists `*.json` only. Audio with no sidecar is debris from a crash; capture removes it at start |
| `fsync` (decided, Q9) | none per item. Every `spool.sync_s` (default 5 s) in which something was published, and at stop, capture syncs the files published since the last time and then `items/`. A crash or a power cut loses at most the last seconds of spooled audio; the urgent lane is durable on its own. The sidecar carries `audio.bytes`, so a short or empty file after a power cut is detected, logged and skipped, never parsed as audio |
| Ids | `id` is `<slug>-<int(start * 1000)>` for a transmission, where the slug is the channel's label with spaces removed (as `App._segment` does today) and any character outside `[A-Za-z0-9._-]` replaced by `_`. For every label made of those characters and spaces it is exactly today's id. **An id is always valid** (`CLIP_ID` in `app.py`: an ASCII letter or digit, then at most 120 of `[A-Za-z0-9._-]`). When that slug does not start with a letter or digit (a label starting with `#` or `(`) or is longer than 107 characters, the slug is instead: the plain slug without its leading characters that are not letters or digits, cut to 98 characters, then `_` and the first 8 hex digits of the SHA-256 of the label's UTF-8 bytes (`ch_` and the 8 hex digits when nothing is left). So any label the wish accepts gives a valid id of at most 121 characters. Today such labels give an id that fails `CLIP_ID`, and their clips cannot be served: a fault of today's code, noted for the radio maintainers. Capture refuses a channel set in which two labels give one slug (`refusals`, `what: "channel"`). A transmission on a channel that is not recorded gets its id all the same (today it has none, and no row): the sidecar needs one, and the app still makes no row. A P25 call's `id` is trunk-recorder's file stem (`Call.id`); a metrics item's is `metrics-<int(minute start * 1000)>`. A call's stem that fails `CLIP_ID` is mended the same way (cut to 112 characters, `_`, 8 hex digits). Every `id` matches `CLIP_ID`. The same transmission never gets two ids |
| Sequence | `seq` is one counter for the spool, from 1, at most 12 digits (the file name's width). It is taken under the publish lock **before the audio is renamed** (the audio's name carries it), and the lock is held until the sidecar's rename. It is persisted in `SEQ` (tmp + rename). **At every start** the next number is the largest of: `SEQ` (0 when missing or not a number), the highest number in any file name in `items/` (debris included) and in `quarantine/`, and the reader's cursor (read-only; its `seq` and the highest of its `done`, only when its `spool_epoch` is this spool's); plus 1. Always all of them, not only when `SEQ` is bad: `SEQ` is not fsynced at each publish (Q9), so a power cut can roll it back after items were pruned behind the cursor, and a number at or under the cursor would count as done and never be handled. A `SEQ` found below the cursor or a file name is logged once and counted (`spool.seq_behind`), and `check_spool` reports it. `SEQ` and the spool directory are fsynced with the periodic sync (every `sync_s` and at stop): cheap, but the start rule does not rely on it. If `EPOCH` is missing, it is a new spool: a new `spool_epoch`, and cursors start over |
| Ordering | per channel, `seq` rises in the order transmissions ended (the order of #767's worker). For P25 calls "per channel" is per system and talkgroup, in the order of `end`. Nothing is promised across channels |
| Delivery | at least once. The reader dedupes on `id` |

Sidecar, `kind: "segment"` (FM, paging, airband):

| Field | Type, unit | Meaning |
|---|---|---|
| `v` | int | 1 |
| `kind` | string | `segment` \| `call` \| `metrics`; an unknown kind is acknowledged and skipped |
| `id`, `seq`, `spool_epoch` | string, int, string | above |
| `channel` | `{label, kind, mhz, mod}` | `kind`: `traffic` \| `paging` \| `airband`; `mhz` as heard (4 decimals; airband: the frequency of this transmission, #260); `mod`: `nfm` \| `am` |
| `sdr` | string | the dongle's serial |
| `start`, `end` | float, epoch s | wall clock (`CLOCK_REALTIME`), stamped by the squelch from frame arrival, as `Segment.start` / `end` today |
| `closed_boot`, `published_boot`, `boot` | float s, float s, string | `CLOCK_BOOTTIME` at squelch close and at the sidecar's rename, and the kernel's boot id they belong to. `closed_boot` is what `late` is measured from (below); both give the latency figures of §5. Comparable only within one boot |
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
| `v`, `spool_epoch` | 1; the spool it belongs to. Another `spool_epoch`: start from 0 |
| `tries` | `{seq: n}`: how often an item not yet done has been handed to its handler (the poison rule, below) |
| `seq` | every item with a `seq` at or below this is done |
| `done` | up to 256 higher `seq` values finished out of order (channels are handled in parallel) |
| `at` | when written |

Written by the reader with tmp + rename, at most once a second and at stop, and only after a handler returned (the
rule of #753's mark). "Done" means the row exists and the reader holds its own hard link to the audio
(`clips/<id>.src.wav`).

| Topic | Rule |
|---|---|
| Who deletes | capture only. It removes items at or below the cursor on its next look (it is told on the socket, and looks every 5 s anyway). The reader never deletes in the spool. This ends today's three-way deleting under the P25 tree. The sidecar always goes first, then the audio, when an item is deleted and when its audio is dropped at a bound (the sidecar rewritten with `audio: null, audio_dropped: true`): no sidecar ever names audio that is gone |
| An emergency item | for the two rows below, an item is an emergency when any of these holds: an `ids` entry with `emergency: true`; a call's turn with `emergency: true`; the call-level `emergency: true`; a non-empty `alarm_key`. One test, the same in capture and in `check_spool` |
| Bounds | unacknowledged items older than `spool.max_age_h` (default 12, the clips' own `audio_keep_hours`) or beyond `spool.max_bytes` (default 2 GB) lose their audio first, oldest first, **never an emergency item** (it keeps its audio until the reader has it, whatever the bounds); the sidecar is rewritten with `audio: null, audio_dropped: true`. Sidecars go only past `spool.max_items` (default 50,000), and never an emergency item's. Both are counted in the status |
| Free-space reserve | the spool shares its filesystem with `urgent.sqlite` and the outbox. When the filesystem's free space falls under `spool.min_free_bytes` (default 256 MB), capture publishes sidecars without audio (`audio: null, audio_dropped: true`) until there is room again, and Health is critical. An emergency item is the exception: its audio is written inside the reserve too (a 2-minute clip is under 4 MB). Only a write that fails outright can leave it without audio; its sidecar then says `audio: null, audio_dropped: true, audio_lost: "write"`. The spool's own growth can then never be what fills the disk under the urgent file |
| Disk full all the same (something else filled it) | a failed spool write is counted (`spool.dropped`) and logged once a minute; capture keeps running. The live path needs no disk. The urgent lane needs it only to be durable: it still sends, from memory (§4.3) |
| Health | the status carries `spool: {pending, oldest_s, bytes, free_bytes, dropped, dropped_audio, quarantined}`; the app's health report adds: warning when `oldest_s` is over 60 with a reader attached, critical with no reader for 5 min, with `bytes` over 80 % of the bound, or under the reserve |
| A poison item | a handler that raises leaves the item unacknowledged, and it comes again. After 3 tries (counted in the cursor's `tries`, so across restarts) the reader hard-links it into `quarantine/`, raises a Health item naming its `id` and the error's type, and moves the cursor past it. The channel carries on. `quarantine/` is kept 7 days and at most 100 items; a person can put one back |
| Capture crashes | the transmission that was open is lost, as today. A clean stop flushes and drains first. At start capture cleans `items/`, matching the sidecar-first order of every removal: `tmp/` is wiped; any file whose `<seq>-<id>` has no sidecar is removed (audio, `.mdc.wav`, `.tr`: debris of a publish that did not finish, or of a deletion that removed the sidecar and stopped); audio whose sidecar says `audio: null` is removed (a drop at a bound that rewrote the sidecar and stopped); then `SEQ` is recovered |
| The app crashes | at start it reads the cursor and lists `items/` above it. An item handled before the crash but not yet in the cursor comes again and is dropped by `id` (as `App._late_dup` in #753 does for P25) |
| Both restart | the two rules above, independently |
| Late (decided, Q4) | by age at the app's read and nothing else. The age is the **larger** of two: the boot-clock age (the reader's `CLOCK_BOOTTIME` minus `closed_boot`, when `boot` is this boot) and the wall-clock age (`now - end`). Either clock can under-count (the boot clock if it stops while a WSL2 host sleeps, the wall clock if it steps back), so the larger is taken: an error makes a row late, never stale traffic live. The production station's boot clock runs fast (#788): its age over-counts by up to 5 %, so a row is late from about 57 real seconds. That is the safe side. Across boots only the wall age exists; a negative one counts as old. Under `LIVE_MAX_AGE_S` (60 s) is live, with every live consumer; exactly 60 s and older is `late`, with #753's rules: the radio log, STT and the hub, but no ATC, intel, hints or incoming (`_atc`, `Comms.on_text`). With a spool, reading a segment a few seconds late is the normal path, so an app restart of 4 s makes nothing late |
| Emergencies | **never gated by age on the station**, by either clock: not by `late`, not by "negative counts as old", not across a reboot. Capture always sends an alarm (§4.3) and the finished transmission's `_alarm_now` always runs; it attaches the row and its clip to the alarm already there. Whether an old alarm still alarms is decided on the hub alone (`MAX_AGE_S`, A7) |
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
| `hello` | `contract`, `impl`, `build`, `pid`, `started`, `spool_epoch`, `urgent_epoch`, `boot` | - |
| `snapshot` | `carriers`, `talking`, `paging`, `status_at`, `spool_seq` | first frame after `hello`, and on request |
| `carrier` | `ch`, `state` (`up` \| `keyed` \| `down`), `since`, `continuing`, `holding`, `quieting_db`, `queued` | squelch open; `NoiseSquelch.keyed` (held `min_open_s`: only then is it "talking"); close. `continuing` and `queued` (closed, waiting for capture's worker) are what `App._release_pages` reads (§3.6) |
| `ids` | `ch`, `since`, `ids` | `LiveTalk.fm_ids` from the tap |
| `urgent` | `urgent_epoch`, `n`, `key`, `rev`, `kind`, `at`, `event_at`, `event_boot`, `boot`, `data`, `durable` | an urgent item (an alarm from `tap.on_emergency` or the close; a live page), whole: the app takes it over from the frame (§4.3). An item's identity is `(key, rev)`; `n` is only its place in the file. `durable: false`: no row yet, `n` is null. If the row is written later, a second frame for the same `(key, rev)` follows with its `n` and `durable: true` |
| `reply` | `id`, `ok`, `error`, `confirm`, `result` | the answer to a request, below |
| `talking` | `entries`: `key`, `system`, `channel`, `tg` (null for an analog channel), `radio_id`, `alias`, `dispatcher`, `since`, `talk_since`, `ended`, `until`, `id_on_release` | `LiveTalk.state()`, without `rig`, `label`, `main`: the app adds those from the CAD. `dispatcher` stays: `LiveTalk` works it out from the alias alone (false for an analog channel). `channel` and `tg` are both there, as in today's state post. The whole list, latest wins |
| `paging` | `entries`: `key`, `channel`, `started`, `pairs` (`a_hz`, `b_hz`, `names`, `ours`, `at`), `phase`, `ours`, `ended`, `until`, `page_id` | `LivePaging.state()`. The contract says `a_hz`, `b_hz` everywhere; `LivePaging` and the state post say `a`, `b` today, and the app maps |
| `ours` | `ch`, `pair` (one pair, as in a page) | `LivePaging.on_ours` (the app calls `sources_poll_now`) |
| `grant` | `system`, `tg`, `src` (string), `what` (an open word) | a unit event of another P25 system, for the app's `Activity` |
| `spool` | `seq`, `id`, `kind`, `ch` (absent for an item with no channel) | an item is complete |
| `status` | `at` (the file's own `at`) | the status file changed |
| `level` | `ch`, `open`, `quieting_db`, `hiss_db` | once a second per channel, only to a client whose `hello` said `subscribe: ["level"]` |

In a `carrier` frame `holding` is `NoiseSquelch.holding` and `queued` a count. In an `urgent` frame `at` is the
item's own `at` (§4.3), the only time the frame has. A frame's header is at most 65536 bytes (#709's limit), and capture never sends a
longer one. **One item, in any frame or reply, is at most `ITEM_MAX_BYTES` = 49,152 bytes (48 KB)** as encoded. A
larger one is framed **trimmed**, by the rule of the hub request (§4.3, "What is sent"), with `data.trimmed: true`,
and never dropped. 48 KB fits both a frame and a hub request (65,536 bytes each) with 16 KB to spare for their own
fields. A version 1 writer sends no payload, and a version 1 reader skips one of up to 1 MiB and closes the connection on a
longer one.

Client to capture, notices (no answer; each one's own fields are required, `at` is optional on a client's frame):
`hello` (`role`: `reader` or `viewer`, `contract`, `subscribe`), `poke` (`what: "wish"`), `cursor` (the spool cursor
moved), `urgent_taken` (`urgent_epoch`, `n`: the app has taken that file's rows up to `n` over; both required, and
capture ignores a notice for another epoch, so an `n` from before a reset can never mark new rows as taken),
`urgent_delivered` (`key`, `rev` required, `gen` optional: the hub answered 200 for the app's take-over event that
carries that key **with the outcome `raised`, `joined` or `duplicate`** for it (H1a), and `gen` is the generation in
that answer. For `seen` or `stale`, or a hub that gives no outcome, the app sends nothing, and capture sends the item
as usual; all-in-one, the app's `cad.alarms` took it. It is
the only way capture learns what the app's lane delivered; `urgent_taken` only says the app took an item over. A key
capture does not hold is ignored), `paging_finished` (`ch`, `page_id`, `end`: today's `LivePaging.finished`; without it the live page ends on its own
hold timeout, as it does today when nothing finishes it).

Client to capture, requests (decided in review, Q5: what is imperative and needs an answer). A request is
`{v, t: "request", id, op, args, by, at, expires_boot}`; the answer is `{v, t: "reply", id, ok, reason?, error?,
confirm?, result?, at}` on the same connection. With `ok: false` there are two fields, as the station routes'
answers have: **`reason`**, one word for the client to act on, and `error`, text for a person, which no client
parses. The words: `stale`, `expired`, `unsupported`, `forbidden`, `parked`, `taken`, `held`, `busy`, `invalid`, `interrupted`,
`confirm`; a word a client does not know is a plain refusal. `confirm` is a bool: true (with `reason: "confirm"`)
means the refusal only needs the person's yes, and the question is `error`. The yes is a **new request, under a new
`id`**, with `args.confirm: true`: the same `id` would only get the first reply again.

| `op` | `args` | Today's call | Answers besides `ok` |
|---|---|---|---|
| `pick` | `sdr`, `job` (a channel, the P25 job, a system job, `Off`), `confirm` (bool) | `App.set_mode`, from the web or the hub's job | `parked`; `taken` (another app holds it); `confirm` (a fire job, a group rule) |
| `squelch_apply` | `label`, `open_db`, `close_db`, `min_open_s` (all four needed), `expect` (`open_db`, `close_db`) | the Apply of a suggestion (#685, #700) | `stale`: what runs now is not `expect`. Without `expect` it is a plain set |
| `reception_start`, `reception_stop` | the body of `reception_post`, as it is today and not typed by this contract; the test's `id` | `App.reception_post` | a test is running; no dongle to lend; unknown test |
| `probe` | `sdr` | the no-audio probe, now (`App._probe`) | not idle; held; `result`: audio or none |
| `airband_ab` | `label`, `minutes`, `phase_s` | `App.airband_ab` | not an airband channel; not running |
| `check` | `what` (`groups` \| `adsb`), `value`, `confirm` (bool) | the refusals of `App.set_antenna_groups` and `set_adsb_receiver`, asked before the app writes the wish. **Advisory only** (decided): it changes nothing, and capture validates again when it applies the wish (§4.4) | `invalid`; `confirm`; `held` |
| `urgent_since` | `urgent_epoch`, `n`, `limit` (default 200, at most 500), `memory_after` (default 0) | the app's take-over after a reconnect (`urgent_epoch` and `n` needed) | `result`: `{urgent_epoch, rows, memory, memory_last, more}`. `memory`: the items capture holds that have no row (yet), in the order capture made them, those after `memory_after` (each memory item has `m`, a counter of the capture process; a new `pid` in `hello` starts it again, and after a capture restart, seen as a new `pid`, the app **MUST** ask from `memory_after: 0`; `socket.md` says so as a MUST). `rows`: the rows above that cursor, in `n` order, at most `limit`. **The reply is filled only while its encoded frame stays within 65,536 bytes**: memory items first (they are the ones at risk), then rows; each item at most 48 KB (trimmed past that). `memory_last`: the `m` of the last memory item given. `more: true` when any row or memory item was left out: ask again from the last `n` given, with `memory_after: memory_last`. 200 rows of a few hundred bytes each are about 76 KB, so the default page is often cut short: that is normal |
| `credentials` | `token`, `access_id`, `access_secret` (all three, `""` for none) | the root reload helper (§4.3) | **accepted only from a peer with uid 0**; anyone else: `forbidden`. Never kept in the memory of answered requests |
| `snapshot` | `{}` | - | `result`: the snapshot |

| Rule | |
|---|---|
| Ids | chosen by the client, unique, 1 to 64 characters. Capture applies each `id` once. For an op with effects (`pick`, `squelch_apply`, `reception_start`, `reception_stop`, `probe`, `airband_ab`) it records the `id` in `urgent.sqlite` (table `requests`, fsync'd) **before** it applies the request, and the reply after; it keeps the last 256 for 10 min (on the boot clock; one from another boot by its wall time). So a repeat gets the first reply again and changes nothing, even after its deadline and **even after capture restarted**. A repeat that finds the `id` recorded with no reply (capture stopped while applying it) gets `ok: false, reason: "interrupted"`: the person looks at the state and asks again under a new `id`. Reads (`check`, `urgent_since`, `snapshot`) are not recorded: a repeat is simply answered again. `credentials` is never recorded |
| `expires_boot` | a deadline on `CLOCK_BOOTTIME` (both ends are on one host). A request that reaches capture after it is answered `reason: "expired"` and not applied: a click answered late is never applied late, and a stepped wall clock cannot expire one early |
| No reply within the client's timeout (capture restarting) | the client sends the same `id` again when it reconnects, or gives up and tells the person. Either is safe, because the `id` outlives capture's restart (row above): an op that is not idempotent by itself, such as `reception_start`, is never applied twice |
| After `ok` | capture has applied it. The app then records it in the wish file (§4.4), which is what makes it outlive a restart of capture |
| An unknown `op` | `ok: false, reason: "unsupported"` |
| Who may ask | a client that said `role: "reader"`; a `viewer` gets `reason: "forbidden"`. The role is self-declared and both are the same uid: it keeps a status tool from changing anything by mistake, and is no defence against a process of that user (§7.5). `credentials` is the exception: uid 0 only |

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
| `alarm`, analog | the live tap decodes an emergency packet (`tap.on_emergency`) | `<slug>-live-<int(carrier start * 1000)>`, with the slug of the ids rule (§4.1): today's `_fm_emergency` key for every label of letters, digits, `._-` and spaces | 0 |
| `alarm`, analog, at close | the whole-clip decode finds an emergency the tap did not raise (sent at release, or missed) | the transmission's own `id` | 0 |
| `alarm`, P25 live | the emergency bit on a grant, on a **transition** only (below). **Not live until #775 lands** | `p25-<system>-<talkgroup>-<radio id>-<int(event_at * 1000)>`, where `event_at` is the grant's own time as trunk-recorder stamps it (decided by the radio maintainers, 2026-11-18): the same grant on the same station always gives the same key, so a capture restart or a replay that reads it again is a `duplicate`. `data.key_time`, **required on every P25 live alarm**, says which time made the key: `grant`, or `capture` when the grant carried no time and capture's `at` was used instead. Two stations' copies never share a key (their clocks differ); the hub's same-radio rule joins them | 0 |
| `page` | `LivePaging` shows a page, **every page**, not only ours (decided: mutual-aid pages matter to pre-alerts; the hub filters): the first pair that is certain, then each change (a pair added or settled, `phase` to `voice`, ended) | `LivePaging`'s own key, `page:<channel>:<started, 2 decimals>` | 0, 1, 2, ... one per change |

**P25 today: an emergency reaches the hub when its call ends.** Nothing sizeup reads from trunk-recorder while a
call is up carries an emergency bit. `unit-event.sh` is run with `<shortName> <radioID> <event> [talkgroup]
[patches]` and prints only `call` events; `live.py`, `recorder.py` and `trunkrecd.py` have no emergency pattern; the
repo's sample log (`tests/fixtures/trunk-recorder-sample.log`) has no such line. The flag arrives only in the
finished call's JSON (`emergency`, `srcList[].emergency`), which `App._call` reads. With the split that still means:
at the end of the call if the app is up, and when the app is back if it was down.

Decided in review (Q-B), the radio maintainers' own follow-up, #775: the station's trunk-recorder patch will say the
grant's emergency bit (a patch hunk with a C++ harness test against the pinned commit, as #647 has). Capture then
puts it in the urgent lane under these rules:

| Rule | |
|---|---|
| Transition only | an alarm is raised when the bit is set for a (system, radio) that has not had it set within the last 10 min. The 10 min are on the **boot clock** (`p25_bit.last_set_boot`), so a wall clock stepped back cannot hide a real new press and one stepped forward cannot re-alarm a stuck bit. Capture keeps the row in the urgent file, so its own restart does not re-alarm a bit that was already set. Across a reboot the boot clock says nothing: the row counts as "not set lately" and the next set bit alarms, once. Erring that way costs one repeated alarm per reboot for a radio whose bit is stuck, never a missed press. **A boot clock that froze** (it may stop while a WSL2 host sleeps, not verified) would make a real new press look 10 min recent. So, on the same boot, the row also counts as "not set lately" when the wall clock advanced by more than 60 s more than the boot clock since the last set bit: `(now − last_set) − (boottime − last_set_boot) > 60`. The bit then alarms, once. A wall clock stepped forward by more than 60 s looks the same and also costs one repeated alarm; the measured drift (5 % of 10 min is 30 s) and the small backward corrections never reach 60 s |
| One alarm per press | **capture makes the match, on the station, where both times are on one clock.** When its ingest takes a finished call it looks in the urgent file for a live P25 alarm of the same system + talkgroup + unit whose grant time (`event_at`) falls inside the call's span (`start` to `end`), with 10 s of slack at each end (decided, Q-I); both are the station's wall clock. If the wall clock stepped in between, so that test fails, the boot clock decides: the grant's `event_boot` within the call's length plus 70 s before the moment the ingest took the call (10 s of slack and the ingest's settle wait). On a match the call's sidecar carries `alarm_key` = the live alarm's key, and `App._call`'s end-of-call alarm rides the hub's existing take-over by `alarm_key`. The hub compares no clocks for this. A live bit with no matching call within 10 min stands on its own. The transition rule above, not this match, is what covers a stuck bit across calls |
| A stuck bit never re-alarms | on the station: a radio whose bit stays set across grants raises nothing after its first transition; each set bit moves the last-set time on, so no new transition is seen until 10 min pass without one (or one of the exceptions above, each costing one repeated alarm). On the hub the end-of-call flags of that radio join its open alarm for 10 min, timed on the hub's clock, and mark the row "emergency still set" (the route's table, below; H1d). The hub's rule only ever joins an alarm that exists and is open, so it can never hide one |
| Health | a counter of transitions an hour, to flag a misbehaving radio or decoder |

Until #775 lands, the P25 row above is a slot in the contract and not a working path.

Every key is at most 200 characters: a longer one (a page of a very long label, say) is cut to 191 characters, then
`~` and the first 8 hex digits of the SHA-256 of the whole key. With the slug rule, every key capture makes is valid.

#### Clocks

Station wall clocks under WSL2 drift and step (#720; `docs/nodes.md` §2.4). So no age in this contract is taken from
the wall clock alone:

| Rule | |
|---|---|
| Within one boot | every age and deadline is on `CLOCK_BOOTTIME` (seconds since boot, counting suspend; `time.clock_gettime(time.CLOCK_BOOTTIME)`). Both processes are on one host, so the values compare. The kernel's boot id (`/proc/sys/kernel/random/boot_id`) says whether they are the same boot |
| Not Linux (macOS, development and inline mode) | there is no `CLOCK_BOOTTIME`: `time.monotonic()` stands in, and a random id made at process start stands in for the boot id. Inline mode is one process, so that is enough |
| Across boots | wall time, the only clock there is. For `late` a negative age counts as old; for an emergency no age counts at all (next row) |
| Emergencies | no clock on the station ever holds one back or drops it (§4.1). An alarm's `age_s` is its **boot-clock** age within a boot, never the larger of two: a wall clock stepped forward must not make a fresh alarm look old to the hub. The known error is the other way: the production station's boot clock runs fast (#788), so a boot-clock age **over-counts** by up to 5 % and an alarm looks slightly older than it is. The hub therefore applies every age cutoff on an emergency with a 10 % margin (the hub's table below, H1i). A boot clock that stopped through a host sleep (not seen yet) would under-count: the alarm looks fresher, the safe side |
| `late`, and a page item's 120 s | the **larger** of the boot-clock age and the wall-clock age (§4.1): for these the safe side is "old". So a wall clock stepped forward can make a row late or expire a page item early; stepped back it changes nothing; neither ever touches an alarm |
| Where it applies | `late` (§4.1: `closed_boot` in the sidecar); a page item's 120 s; a request's `expires_boot` (§4.2); the status file's 15 s staleness (`at_boot`, §4.5); the spool's `max_age_h` |
| Towards the hub | each item is sent with `age_s` and `age_src`. `age_src: "boot"`: the item's age on the boot clock at the moment of sending. The hub places the item at its own `now - age_s` and never at the station's `event_at`. `sent_at` only lets the hub note how far the station's clock is from its own (as `hub.skew` does from the state post's `at`), for Health |
| An item sent after a reboot | its `event_boot` belongs to another boot, so the boot clock says nothing about it. It is sent all the same, with `age_src: "wall"` and `age_s` = the wall-clock age (`now - event_at`), clamped at 0. `age_s` is never null and an item is never held back for want of a good clock. A wall-sourced age cannot be checked or corrected by the hub: it is two readings of the station's one clock, so a constant offset cancels, and a step of that clock across the reboot is invisible. The hub takes it as given, bounded as every `age_s` is (§7.5), and labels the alarm's age as approximate. An `age_src` the hub does not know is taken as `wall` |
| A wall step (revised after #788; the 2 s of 2026-11-18 is replaced) | capture samples the offset wall − `CLOCK_BOOTTIME` on its existing tick. A step is a change of that offset by **more than 5 s** between two samples at most 5 s apart, and only that. Each step is logged once, with its size, and counted: `wall_steps`, a counter since boot, goes in the status and on the app's state post beside the boot id, for the hub's skew path (H1d). The station's routine corrections (below: backward steps of up to about 2 s) are not steps and are not logged. Why 5 s: the P25 match has ±10 s of slack, so corrections of about 2 s cannot break it, and a 2 s threshold would fire every few minutes. A step is what lets the P25 match fall back to the boot clock ("One alarm per press"). `docs/nodes.md` §2.2's lease uses this same definition |
| Drift (measured, #788) | on the production station (WSL2) the boot clock (`CLOCK_BOOTTIME`, and the monotonic clock with it) runs **fast**, by 2 to 5 %, and the rate wanders through the day (4.7 % one afternoon). The wall clock stays within about 2 s of AWS only because it is corrected in small backward steps: each at most about 2 s, every 30 to 40 s or more often; no jump above about 2 s in 22 h of data. Consequences: a boot-clock age **over-counts** by up to 5 %, so an item looks slightly older than it is (3 s on the 60 s `late` limit, 6 s on a page's 120 s, 90 s on 30 min); the offset wall − boot grows by minutes over hours, so nothing compares wall times over hours. The station-side fix (clocksource and chrony) needs the admin on the box and is not scheduled; the margins below stay after it |
| Rate bound | every window on the station is designed to be safe with a boot clock off by ±10 %. Effects at +10 % (fast): the 60 s `late` limit is reached at about 55 real s (57 s at the measured 4.7 %): more rows late, safe. The P25 bit's 10 min ends at about 9.1 min (9.5 min at 4.7 %): more alarms, safe. A page revision's 120 s ends at about 109 s (114 s): the app's lane still carries the page. The resend window's 120 s is about 109 s (114 s), still far over the replica's ~30 s of loss. A request's `expires_boot` and the status file's 15 s staleness come about 5 to 10 % early: harmless. A slow clock (−10 %) lengthens each window by as much: the P25 frozen-clock rule above and the larger-of-two-ages rule cover the cases where that would matter |
| Not verified | whether `CLOCK_BOOTTIME` keeps counting under WSL2 while the Windows host sleeps. The rules above are chosen so that either answer is safe. The stepped-clock tests of §6 run on fake clocks |

#### The file

`/var/lib/sizeup/state/urgent.sqlite` (the path is `paths.urgent_db` in the wish file).

```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;            -- every commit is fsync'd
PRAGMA wal_autocheckpoint = 0;        -- checkpoints are explicit: never on an alarm's insert
CREATE TABLE IF NOT EXISTS urgent (
  n          INTEGER PRIMARY KEY AUTOINCREMENT,
  key        TEXT    NOT NULL,        -- the idempotency key, above
  rev        INTEGER NOT NULL DEFAULT 0,
  kind       TEXT    NOT NULL,        -- 'alarm' | 'page'; a reader leaves an unknown kind alone
  at         REAL    NOT NULL,        -- wall clock when capture made the item, epoch s
  at_boot    REAL    NOT NULL,        -- the same moment on CLOCK_BOOTTIME, s
  event_at   REAL    NOT NULL,        -- when it happened on the air, wall clock, epoch s
  event_boot REAL    NOT NULL,        -- the same moment on CLOCK_BOOTTIME, s
  boot       TEXT    NOT NULL,        -- the boot id at_boot and event_boot belong to
  data       TEXT    NOT NULL,        -- JSON object, the item's data (below)
  state      TEXT    NOT NULL DEFAULT 'new',   -- 'new' | 'sent' | 'app' | 'rejected' | 'expired' (pages only)
  sent_at    REAL,                    -- when the hub answered for it, wall clock, epoch s
  sent_boot  REAL,                    -- the same moment on CLOCK_BOOTTIME, s
  sent_boot_id TEXT,                  -- the boot id sent_boot belongs to
  hub        TEXT,                    -- the hub's word: 'applied' | 'duplicate' | 'stale' | 'rejected'
  hub_gen    TEXT,                    -- the hub generation that answered, as decimal digits (the resend rule)
  tries      INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,                    -- a status and a reason, never a body, never a token
  lost_gen   TEXT,                    -- the hub gen this alarm may have been lost under and was not re-sent (over the cap)
  lost_at    REAL,                    -- when lost_gen was set, wall clock, epoch s: the warning lasts 24 h from here
  UNIQUE (key, rev)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
  -- 'v' = 1, 'urgent_epoch' = 16 hex; the resend rule's: 'gen', 'gen_seen_at', 'gen_seen_boot', 'gen_seen_boot_id'
CREATE TABLE IF NOT EXISTS p25_bit (             -- #775: when each radio's emergency bit was last seen set
  system TEXT NOT NULL, radio TEXT NOT NULL,
  last_set REAL NOT NULL,                        -- wall clock, epoch s
  last_set_boot REAL NOT NULL, boot TEXT NOT NULL,   -- the same moment on CLOCK_BOOTTIME, and its boot id
  PRIMARY KEY (system, radio)
);
CREATE TABLE IF NOT EXISTS requests (            -- socket request ids: one reply per id, across a restart (§4.2)
  id TEXT PRIMARY KEY, op TEXT NOT NULL,
  reply TEXT,                                    -- the reply's JSON; NULL while the request is being applied
  at REAL NOT NULL, at_boot REAL NOT NULL, boot TEXT NOT NULL
);
```

| Rule | |
|---|---|
| One opener | **only the holder of `radio.lock` (§4.7) ever opens this file**: capture, in service mode. One connection, one process, so there is no second writer, no reader to hold a checkpoint back, and no lock to wait for. None of the outbox's clip commits (p99 132 to 430 ms [L]) can sit in front of an insert |
| How the app gets the rows | from capture, not from the file: each `urgent` frame carries the item, and after a reconnect the app asks `urgent_since`, page by page (§4.2). It keeps two things, both in `outbox.sqlite` (all-in-one: the CAD's own store) and both moved in the transaction that queues the take-over event: the cursor **`(urgent_epoch, n)`**, which only says where to ask from, and the set of `(key, rev)` it has taken (kept 24 h). A different `urgent_epoch` (the file was reset) starts the cursor at 0; the taken set still holds |
| Take-over is by `(key, rev)` | an item is taken once, whichever way it arrives: a frame with `n`, a frame without one, `urgent_since`'s `rows` or its `memory`. An item without a row is taken at once and moves no cursor. When its row appears later, the second frame finds `(key, rev)` already taken and only moves the cursor |
| Insert | `INSERT OR IGNORE` on `(key, rev)`, on the `urgent` thread. The pipe reader only hands the item over, in memory |
| An item's `at` | set once, when capture makes the item, before any insert. It is the same value in the row, in every frame and in every request, whether or not the row is ever written |
| The send does not wait for the disk | the sender takes the item **from memory**. It waits for that item's commit for at most 50 ms, then sends whether or not the row exists. A failed insert (a full disk, an I/O error) or a stalled `fsync` therefore delays the send by 50 ms at most and never stops it. Such an item is `durable: false`: it lives in memory (at most 256 items, alarms kept before pages), the frame to the app carries it, and the status counts it. The writer tries the insert again every 5 s while the item is in memory. The 256 bound is on page items and on the memory copies of items that have a row (those are read back from the file when their turn comes). **An alarm without a row is never dropped to make room**: when the memory is full a page item goes, the oldest first. Alarms without rows are bounded only at 10,000 (a dead disk and no hub for a very long time): past that the oldest is written out as a CRITICAL log line (key, channel, radio id, time) and let go |
| Delivered by the app (`urgent_delivered`, §4.2) | sent by the app only when the hub's answer to its take-over event says the alarm exists: `raised`, `joined` or `duplicate` (H1a). So is `suppressed` (recorded; the admin's settings say no alarm, e.g. notices off): resending against a deliberate setting would only loop. A 200 whose outcome is `seen` (a wall age that looked old) or `stale`, or that has no outcome (an older hub), is not delivery: the row stays as it is, and capture sends it as usual. Then capture sets the row's `state` to `app` (from `new` or `rejected`; a `sent` row stays `sent`), with `sent_at`, `sent_boot`, `sent_boot_id` from when the notice came and `hub_gen` from it. A memory item gets the mark in memory, and its row is written as `app`. Such a row counts as delivered (the banner clears, it leaves `unsent`), is **never sent by capture**, and is pruned as a `sent` row is. In `send` mode it joins the resend rule like a `sent` row; with no `gen`, it is in doubt and is sent again (a duplicate is harmless). In `write` mode nothing can resend it: an `app` row in the resend window of a `gen` change (capture learns the new `gen` from the next `urgent_delivered`) gets a Health warning naming it for 24 h. So a station in `write` mode no longer keeps every alarm "not delivered" for good, and a later switch to `send` does not send that backlog |
| The hub's answer and the row | the sender keeps each item's answer in memory by `(key, rev)`. If the row exists it is updated; if the commit lands after the answer, the row is written as `sent` with the hub's word, never left `new`. "Unsent" in the status, and so the station's banner, comes from the senders' own view, not from row states |
| At start | capture loads into the senders, before it starts a receiver's worker: every `new` row, oldest alarm first (a page revision only if it is still sendable); and, **for the resend rule**, every alarm row answered at or after the last sighting of the hub's `gen` minus 120 s, as "answered under `hub_gen`" (`sent` and `app` rows). A row marked `sent` or `app` is sent again only by that rule. Every row whose `lost_at` is in the last 24 h goes back into Health's lost-alarm warning: the warning is rebuilt from the rows, never kept only in memory |
| Checkpoints | by capture only, `PASSIVE`, on its prune tick, and only while no item is waiting for its commit; `TRUNCATE` at start. The soak watches the `-wal` file's size |
| `data`, an alarm | `source` (the channel label; for P25 the name the finished call's row will carry, so the same-radio rule sees one channel), `start`, `end` (optional: a key-up alarm has none yet), `radio_id` (several ids joined with `, `, as today), `ids` (as the sidecar's), `unit` (the alias, from the wish's map), `emergency: true`, `emergency_via` (`MDC` \| `FleetSync` \| `P25`); P25 adds `system`, `talkgroup` and `key_time`, all three required. No `rig`: the hub derives it (`hub._traffic_of`). These are the fields `_fm_emergency` builds today |
| `data`, a page | `channel`, `started`, `pairs` (list of `{a_hz, b_hz, names, ours, at}`), `phase` (`tones` \| `voice`), `ours`, `ended`, `until` |
| Bound | rows are a few hundred bytes. Pruned: `sent` and `app` rows the app has taken over, an hour after `sent_at` (never inside the resend window, below, and never within 24 h of `lost_at`); `expired` and `rejected` rows after 24 h; at most 10,000 rows, the oldest `sent` first. **An unsent alarm is never pruned** |
| Corrupt or unreadable at start | renamed `urgent.sqlite.bad-<epoch s>`, a new file with a new `urgent_epoch`, a CRITICAL log line, `urgent.resets` and `reset_at` in the status |

#### The sender

Two kept-alive connections in capture, each with its own thread (`sizeup/cloud/client.py` `HubClient`: the bearer
token, the Cloudflare Access service-token headers, one quiet retry on a connection the edge closed):
`urgent-send` carries alarms and nothing else; `urgent-pages` carries page items. A page request that hangs for its
whole timeout can therefore never stand in front of an alarm.

| Topic | Rule |
|---|---|
| What is sent | one request in flight per connection; at most 32 items and 64 KB (65,536 bytes of body, counted as §4 says) a request. For a page, only the highest unsent `rev` of each `key` (older revisions become `expired`). **Alarms go in this order, in every request:** 0. an alarm made alone by a batch refused whole, in a request of its own, at the head; 1. fresh: alarms the hub has never answered for (no answer recorded in the row: `hub` and `sent_at` null) and made under 120 s ago on the boot clock (`at_boot`; from another boot, by wall age), in `n` order. This is read from the row, not from the process's memory: an alarm whose own request got no answer stays fresh across a `gen` change, and a restart does not put new presses behind reloaded rows; 2. resends of the `gen` rule (below), newest first; 3. the backlog: every other unsent alarm, oldest first. Items that had an unknown word go after these, in requests of their own. So a fresh alarm waits at most for the one request already in flight, never behind resends or a backlog. The byte limit is enforced before sending: items are added in that order while the encoded body stays within it. **Every item is at most 48 KB** (`ITEM_MAX_BYTES`, 49,152 bytes, §4.2): a larger one is sent **trimmed**, never as an oversize request and never dropped. `trimmed: true` is set, and only free text and extras are cut. **The trim never touches a field used for identity or matching**: the item's `key`, `rev`, `kind`, `at`, `event_at`, `age_s`, `age_src`, and in `data` `source` and `channel` (a 300-character label stays whole, so the trimmed item still joins the same radio's alarm within `REPEAT_S`), `radio_id`, `system`, `talkgroup`, `key_time`, `start`, `end`, `started`, `until`, `emergency`, `emergency_via`, `phase`, `ours`, `ended`. What is cut, in this order until it fits: `ids` left out, `pairs` cut from the end, `unit` and `names` cut to 200 characters, any other key left out. The same trim is used for a frame (§4.2), so the app's lane carries the trimmed alarm too; the row keeps the full `data`, and the finished transmission brings all its ids by the spool. A trimmed item that still gets a `too_large` in the hub's shape is final: `rejected`, a critical Health item, and the app's lane carries it |
| Checked before sending | each item is checked against `urgent-item.schema.json` and the route's caps (a key of 1 to 200 characters, `rev` an integer of at least 0) before it goes in a request. Keys are valid by construction (§4.1, the key rule above), so this should never fail. An item that does is never put in a request: it becomes `rejected` with `last_error: "invalid"` and a critical Health item, and the app's lane carries it. One bad item can therefore never make the hub refuse a batch and split it into one-alarm requests |
| When | as soon as the item's commit returns, or 50 ms after it was handed over, whichever is first |
| What is an answer | a 200, 400, 413 or 422 counts only with a body in the hub's shape: for a 200, one that passes `urgent-response.schema.json` (`ok: true`, `gen`, `results` with `key`, `rev`, `status` each); for the three 4xx, a JSON object with `error` and a `reason` of the 400/413/422 row below. Without it (an edge's or a proxy's page, `200 {}`, a 200 that fails the schema, a 422 with no body) it **counts as no answer**: retried with the no-answer backoff below, the item stays `new`, and it is **not counted as a try**. Every other status is acted on by its status, as the rows below say; none of them ends an item |
| "Sent" | a 200 in the hub's shape whose `results` names the item `(key, rev)` with `applied`, `duplicate` or `stale`. The row becomes `sent`. Nothing else counts: not a 2xx without the item, not a 200 that fails the schema, not a timeout after the body went |
| An item missing from `results` | not taken: sent again with the next request. Not counted as a try |
| `rejected` in a 200 | the hub will never take this item (malformed): the row becomes `rejected`, is never sent again, is counted, and Health shows it |
| A `status` word capture does not know, for an item in a 200 in the hub's shape | the item is kept and tried again, at most 5 times, after the items that are healthy and in a request of its own, so it never stands in front of another alarm. After 5 it is treated as `rejected` (next row). Only this, of all answers, counts toward the 5 |
| An alarm that ends `rejected` (a `rejected` in a 200, 5 unknown words, or the refusal of the 400/413/422 row when it is final) | is never sent again on this lane, **and is never only that**: the app's lane already carries it (the take-over does not depend on the sender), a critical Health item names its key, and the station's page shows it as not delivered until the hub has it by the other lane |
| No answer, a timeout (5 s), 502, 503, 530, 429, any status not listed here | retried: 0.5 s, doubling to 2 s for the first 3 min of a failure, then to 15 s; the hub's `Retry-After` is used when given (`client.Backoff`'s rule). Nothing is dropped, and no try is counted |
| 401, 403, 409 (`token_unknown`, `id_mismatch`, `multi_station_off`, ...) | kept and retried every 30 s; `urgent.state = "refused"` with the hub's `reason` when the body is in the hub's shape; a critical Health item. Never the token in a log line |
| 400, 413, 422 in the hub's shape, with `reason` `bad_json`, `too_large` or `bad_batch` | the hub refused the request whole and applied nothing of it. The first such refusal of any request, of one item or many, marks each of its items **alone**: each alarm is then sent in a request of its own, at the head of the queue ("What is sent", item 0), backed off as for no answer (the rule of #762); a page item so marked goes after the healthy pages. A `too_large` for a request holding one item not yet trimmed is not counted as a refusal: the item is trimmed and sent alone again. Only when a request holding exactly one item that was already alone is refused this way does that item become `rejected`. So a lone pending alarm needs two such refusals of itself to be final. The same statuses without the hub's shape or with another `reason` are no answer (row above) |
| 404, 405 | a hub without the route: `urgent.state = "no_route"`, tried again every 5 min. The app's lane still carries everything |
| **An alarm never expires here** | capture gives up on an alarm by no clock of its own. It keeps sending until the hub answers for it. The hub decides what an old one is. The recommended rule (the radio maintainers' proposal, the same as #753 and `alarms.MAX_AGE_S`; A7 awaits the admin): an alarm is stale only when its `age_s` reaches `MAX_AGE_S` × 1.1 (33 min for 30 min; the 10 % covers a boot clock that over-counts, Clocks). Under that it is still raised **and pushed**, labelled with its real time and its age ("from 12 min ago"), answer `applied` (every push carries the item's age; from what age it is shown as late is the hub's); at or over it, a late notice on Health and the row with no push, answer `stale` |
| A page item | is live state, and its clock runs **per revision**: a revision unsent 120 s after capture made it (`at_boot`; the larger of its two ages) becomes `expired`. So a change late in a long page is as sendable as the first. An `ended` revision is the exception: it stays sendable for 10 min, so the hub is told a page is over even after a gap. If no `ended` ever arrives, the hub's own timeout ends the entry (the route's table). The finished page comes by the app's lane |
| The station's own signal | an alarm unsent for 10 s is shown on the station's page by the app, forwarding or not, as "EMERGENCY not delivered to the hub", with its channel, unit and time, until the hub has it by either lane: capture's own answer, or `urgent_delivered` from the app (§4.2), which is the only way in `write` mode. The app learns it from the status (`urgent.unsent`) |
| Offline | items wait (in the file, or in memory if it could not be written); the retry runs at 15 s; when the hub answers, alarms go first, in the order of "What is sent" (fresh, then resends, then the backlog oldest first), each with its `age_s` |
| A hub that lost what it had answered for (the resend rule) | a hub crash can lose about 30 s of its newest writes (`docs/cloud.md`), alarms it had answered `applied` for among them. Every answer on this route carries the hub's fencing generation `gen`, and so does the answer to the 50 s `hello`, which is therefore **not optional in `send` mode**: an idle sender learns of a restore within 50 s, not at its next alarm. Capture keeps, in the file's `meta`, the last `gen` it saw and when it last saw it (wall, boot clock, boot id), and on each row the `gen` it was answered under and when. Gens are compared **by value** (5 and "5" are one). When an answer carries another `gen`, capture sends again every alarm that was answered under a different `gen` at or after **the last sighting of the old `gen` minus 120 s**: what the hub can have lost is what it answered in the 30 s before it went down, and it went down after that last sighting; 120 s is four times the documented loss. The window is anchored there, not at "now", so it holds however long the hub was away. Times are compared on the boot clock within a boot; a row or a sighting from another boot uses wall time, and when that leaves doubt the row is sent (a duplicate is harmless). At most 32 such alarms are sent: the newest 32 by when the hub answered for them (`sent_boot` within one boot, else `sent_at`), and between equal times the higher `n` is the newer. Any beyond the 32 get `lost_gen` and `lost_at` set in their rows and are named in a critical Health item, rebuilt from the rows after a restart and kept 24 h from `lost_at` (not from `sent_at`). Pages are never re-sent. A deploy's handoff also raises `gen` and costs the same few repeats |
| What stops the sending | any of three: `/etc/sizeup/urgent-mode` not `send`; `hub.forward` false in the wish (all-in-one, or the hub link switched off); a hub that does not advertise `urgent` (`hub.caps`). None of them stops the **writing** in service mode: there the rows and frames are how the app learns of a live alarm at all (the table below) |
| The 50 s `hello` | `GET /api/station/hello` every 50 s on the alarm connection, **always in `send` mode** (decided on by default; the resend rule now needs it). It keeps the connection warm, so the first alarm does not pay a TLS handshake through the edge (100 to 200 ms by `client.py`'s own note), and its answer carries `gen`. About 1,700 small requests a day. Step 8b measures the first alarm's time to the hub with a warm and a cold connection |
| Health | the status file's `urgent` block (§4.5). The app's health report turns it into rows: the sender's state, the oldest unsent alarm, items not durable, counts of `rejected` |

#### The two mode files together

Each file holds one word; surrounding whitespace is dropped, and a missing or empty file or a word a build does not
know counts as the default (`inline`, `off`). `urgent-mode` is `off` (the default), `write` (rows and frames, the
app takes over, nothing sent by the urgent senders) or `send`. In service mode only capture runs the live tap, so
`off` there would leave a key-up alarm with nobody to send it. Therefore: **service mode requires `write` or `send`;
capture treats `off` as `write` in service mode** and says so in the status (`urgent.mode`), and `sizeup-station`
refuses to set `capture-mode service` while
`urgent-mode` is `off`.

| `capture-mode` | `urgent-mode` | Decodes the key-up | Writes the item | Sends a live alarm to the hub | With the app down |
|---|---|---|---|---|---|
| `inline` | `off` | the app | nobody: today's path, `_fm_emergency` -> `_alarm_now` | the app's outbox lane | nothing is decoded |
| `inline` | `write` | the app | the app (it holds `radio.lock` and the file) | the app's outbox lane, by take-over | nothing is decoded |
| `inline` | `send` | the app | the app | the urgent senders, hosted by the app, and the outbox lane | nothing is decoded |
| `shadow` | any | the app, as in the `inline` row of the same `urgent-mode`; the shadow instance also decodes | the shadow instance writes `urgent-shadow.sqlite`, for the compare tool only | as `inline`; the shadow instance never sends | nothing is decoded |
| `service` | `off` | capture | capture: **treated as `write`** | the app's outbox lane, by take-over | written; sent when the app is back |
| `service` | `write` | capture | capture | the app's outbox lane, by take-over | written; sent when the app is back |
| `service` | `send` | capture | capture | capture's own senders, and the app's lane | **sent by capture** |

Seven rows cover the nine combinations: the `shadow` row stands for three, each behaving as the `inline` row of the
same `urgent-mode`. In every one someone decodes, and someone who is running sends: no combination leaves a live
alarm with neither. A test walks all nine combinations with the app up and asserts one alarm at the stub hub for
one key-up, and walks the three `service` combinations with the app down and asserts the last column.

#### The route: `POST /api/station/urgent`

Hub work (§11). Opened by an `sz1` credential with the `radio-receive` role (decided, Q-G); a station's `sz1`
token stands for the role until the nodes registry exists; `sw1` never. The hub's older shared station token, which
the other station routes still take, does not open it: a station still on the shared token is given its own `sz1`
token before `urgent-mode send`, and runs in `write` until then. Request, `Content-Type: application/json`,
read by `Content-Length`, at most 64 KB:

| Field | Type | Meaning |
|---|---|---|
| `v` | int | 1 |
| `station` | string | the station id capture was told (`hub.station` in the wish: the id the hub assigned, `StationLink.adopt`). Checked against the token (`id_mismatch`), as on every station route. An item's identity on the hub is `(station, key, rev)`: the station from the token, never from an item. `key` is at most 200 characters |
| `urgent_epoch` | string | the urgent file's epoch; informative, not a cursor |
| `sent_at` | float, epoch s | the station's wall clock when this request was made; for the hub's skew measure only |
| `items` | list, 1 to 32 | `{key, rev, kind, at, event_at, age_s, age_src, data}`: the row's columns, and the item's age at sending: `age_s` (float s, at least 0, never null) and `age_src` (`boot` \| `wall`: which clock it was measured on; `wall` only for an item from before a reboot) (Clocks, above) |

Answers:

| Status | Body | Meaning for the sender |
|---|---|---|
| 200 | `{ok: true, v: 1, station, gen, results: [{key, rev, status, reason?}]}`; `status`: `applied` \| `duplicate` \| `stale` \| `rejected` | per item, above. An item missing from `results` was not taken: sent again. `gen`: the hub's fencing generation, an integer up to 2^53 - 1 or a string of decimal digits; capture compares it for change only |
| 400 `bad_json`, 413 `too_large`, 422 `bad_batch` | `{error, reason}`. A request that is not well formed is refused whole: nothing of it is applied | cut to singles |
| 401, 403, 409 | `{error, reason}`, as `Hub._refuse` and `_other_station` answer today | `refused` |
| 503 + `Retry-After` | the hub is not the leader (a deploy's handoff, `fence.refuse`): **nothing was applied** | retried |

Idempotency, on the hub. An item is `(station, key, rev)`, and the route is idempotent by all three. The dedupe
store is keyed by `(station, key)` and holds the highest `rev` it has taken: for an alarm `rev` is always 0, so the
key alone is its identity; for a page the key names the page and `rev` orders its changes. The sender never puts an
alarm and a page in one request (two connections), but the hub takes a request that holds both.

Each alarm key in the store is in one of three states, and only two of them stop this lane:

| State | Written when | An urgent item for that key then gets |
|---|---|---|
| `raised` | an alarm exists for the key, raised by either lane | `duplicate` |
| `stale` | this lane judged the item too old and made the late notice. The entry also keeps which clock that age was on: `stale_src` `boot` or `wall` | `duplicate` |
| `seen` | the events lane met the key and raised nothing: its own age gate said too old, or the event did not carry the emergency (`emergency: false` with an `alarm_key`), or notices are off for it | **judged on its own**, as if the key were new: `applied` or `stale` by its `age_s` |

`raised` overwrites `stale` overwrites `seen`, never the other way. Whose age wins when the lanes disagree: this
lane's, when its `age_src` is `boot`. That age is measured on a clock that does not step and is placed on the
hub's own clock; the events lane has only the station's wall time (`end`) against the hub's `now`
(`alarms._on_traffic`), which a stepped station clock makes wrong by the size of the step. So the events lane's age
gate never shadows this lane: it writes `seen`, not `stale`. The other way round holds **only for a boot-sourced
age**: a key marked `stale` with `stale_src: boot` is not raised by a later event whose wall time looks fresh; the
event's row attaches to the late notice. A key marked `stale` with `stale_src: wall` blocks nothing on the events
lane: a wall-sourced age is two readings of a clock that may have stepped across a reboot, so a later event is
judged on its own (its age gate, notices, the same-radio rule), and if it raises, the key becomes `raised` and the
late notice stays beside the alarm. A retry of the urgent item is still `duplicate` either way.

The store is written by **both** lanes: this route
records each item's `key`, and `/api/station/events` records the `id` of every `alarm` event and every `alarm_key`
it sees on an `alarm` or `traffic` event. It is asked before anything is raised.

| Case | What the hub does |
|---|---|
| The first `(station, key)` of an alarm, here | records the key, builds the transmission as `_apply_alarm` does (`id` = `key`, no text, `emergency: true`) and raises the alarm: `applied` |
| The same alarm `key` again (capture retried; the answer was lost; a resend after `gen` changed) | `duplicate`; no second alarm, no second push |
| The app's take-over: an `alarm` event with `id` = `key` in `/api/station/events` | the same `traffic_id`: the alarm is updated, not raised again (today's rule). The key is recorded |
| The app's event for the finished transmission, `id` T with `alarm_key` = `key` | `alarms._on_traffic` finds the alarm by `alarm_key` and **moves its `traffic_id` from `key` to T** (today's code). The key stays in the dedupe store |
| **The app's lane first, T took the alarm over, a person acknowledged it, then capture's item for `key` arrives** (a slow or retried send) | `duplicate`, from the dedupe store. Without the store's entry from the events lane this would raise a second alarm and a second push: the alarm's row no longer carries `key`, and the alarm code's same-radio rule only covers an unacknowledged alarm of the last `REPEAT_S` (120 s) |
| An alarm raised at close with `key` = T, then the app's event for T | the same `traffic_id` |
| The age cutoff, on either lane | every age cutoff the hub applies to an emergency has a 10 % margin: an alarm is too old only when its age reaches `MAX_AGE_S` × 1.1 (1980 s for 30 min; exactly 1980 s is too old, by §4's edge rule). The production station's boot clock over-counts ages by up to 5 % (#788); the margin is twice that. The events lane's own gate (`alarms._on_traffic`) takes the same margin |
| An alarm item that arrives late but under the cutoff (by the hub's own clock and `age_s`) | `applied`: raised and pushed, labelled with its real time and age, as #753 labels a late call's alarm. Recommended; A7 awaits the admin |
| The app's lane first, with a wall time that looks old (the station's clock stepped back 40 min: the event says it ended 40 min ago), then this lane's item with `age_s` 1.2 | the event raises nothing (the alarm code's age gate) and the key is `seen`. The item is judged on its own age: `applied`, one alarm, one push. Recording the key before the gate and answering `duplicate` for it would lose the alarm |
| A `traffic` event carrying `alarm_key` with `emergency: false` first, then this lane's item | the same: `seen`, then `applied` |
| An alarm item at or over the cutoff | `stale`: a late notice on Health and on the row, no push. Its key is recorded `stale` with its `stale_src`, so a retry is `duplicate`, and the app's take-over raises nothing when the age was boot-sourced (above). The limit is a parameter on the hub (30 min recommended), not a constant of this contract. Today's code only logs such a transmission ("too old to raise now"). Recommended; A7 awaits the admin |
| A P25 live alarm (#775) and the end-of-call alarm from `App._call`, in either order | one emergency, by `alarm_key`: capture matched the call to the live alarm on the station (system + talkgroup + unit, the grant inside the call's span ±10 s, one clock) and the call's event carries the live alarm's key, so today's take-over applies. The hub never compares its own placing of the live item (`now - age_s`) with the call's span, which is station wall time: a station 60 s off would make two alarms. An end-of-call event with no `alarm_key` (an older capture) is matched on the hub by the same rule against the live item's own `data.start`, station time against station time |
| The same radio's next call with the bit still set (300 s later, say) | **it joins that radio's open alarm**: no second alarm, no push, the row marked "emergency still set". The hub's rule (revised; H1d), per (system, radio), a **press window** `{alarm, last}`, where `last` is a time on the **hub's own clock**: the moment the hub received the flag (the request or the event). Two station wall times are never compared for it. For each P25 emergency flag of that radio (a flagged call's event, or a live P25 item), received at hub time `t`: **1.** A flag the existing rules tie to an alarm (its `id` or `alarm_key`, the dedupe store) updates that alarm, and arms the window: `{that alarm, t}`. **2.** Otherwise, if the window exists, its alarm exists and is open (not ended or cleared; acknowledged or not), and `0 ≤ t − last < 600 s` (`STUCK_WITHIN_S`): the flag joins that alarm as "still set", and `last = t`. A live P25 item skips this step: the station already judged it a new press. **3.** Otherwise the flag is **judged on its own**: the age gate with its margin, notices off, the same-radio join, then a new alarm and a push. If that raised or joined an alarm, the window becomes `{that alarm, t}`. If it raised nothing (too old, notices off), the window is left as it was: such a flag never arms it. A negative `t − last` (the hub's clock stepped) is not "within": step 3. An All clear ends the window (its alarm is no longer open), so a bit still stuck after it alarms again on its next flagged call: one repeated alarm, the safe side. That alarm and its push read **"emergency still set after All clear"**, not as a new press, but only when the flagged call comes within 10 min (`STUCK_WITHIN_S`, by the hub's receive time) of that radio's last flag before the All clear: a radio still in emergency after someone declared All clear needs a person to look (the radio maintainers' wording). Later than that it is a new press and is labelled as one, so a real new emergency hours later is never presented as a stuck radio. The rule applies from H1 on, to every P25 flag on either lane, before #775 too (then only end-of-call flags exist): it can only join an alarm that is open, so it can never hide one. **A call from before the All clear** (checked after step 2 and before step 3): a flagged call of a radio whose window's alarm was cleared, and whose **start** came before the All clear, is not a new press. It attaches to the cleared alarm with **no push**, its row is marked "emergency flag on a call from before the All clear", and the window is left as it was. "Before" is judged only on a clock the hub can trust, and only past that clock's uncertainty `U`: the call's start, placed on the hub's clock, must be at least `U` before the hub's time of the All clear. The placing, in order of preference: (i) the boot-clock age of H1m (the call's `start_boot` against the request's own boot time; the start is placed at the request's receipt minus that age), `U` = 10 s + 10 % of the age (the rate bound, up to about 2 s of wall corrections inside the call, and a request's transit, which the total 8 s deadline of H1m's small alarm post bounds); (i) is used only when the request's boot id equals the event's `boot`: a reboot between the call and its post leaves no boot age. (ii) else the call's station `start` corrected by the measured `hub.skew`, and only when **all** of these hold: the station reports its boot id and its count of wall steps (`wall_steps`, §4.3 Clocks) on every state post, so every skew sample carries both; the samples used are of the **same boot as the call** (the event's `boot` equals the boot id the skew was measured under); there is a sample at or before the call's start and one at or after it (by the station's own `at` against the call's `start`, valid because of the next condition); every sample from the last one at or before the call's start to the newest has the same `wall_steps` (no wall step since the call started; **a reboot always counts as a step**, so a new boot never qualifies); and the newest sample is under 60 s old. `U` = 5 s + 2 × the spread of the skew over those samples, from the call's start to now. On a WSL2 station the wall clock is nudged back in steps under 2 s every 30 to 40 s (#788); those are not steps, but they add up to about 2 s of jitter to the samples, which the spread includes and the 5 s floor covers on its own (the main session's review). A station that reports no boot id or no `wall_steps` (an older one) never uses (ii). With neither, or with the start inside `U` of the clear, the hub is unsure, and the call alarms by step 3 with the label above. A real new press starts after the All clear, so it is never attached: every error in `U` pushes the placing towards an alarm |
| The same radio pressing again | the alarm code's own same-radio rule: the item is `applied` (it was taken) and joins the open alarm, with no second alarm and no second push. On this route "open" means not ended or cleared, acknowledged or not, as in the next row. `duplicate` stays the word for a key already in the store |
| Two stations hearing one press (`overlap`, `docs/nodes.md` §2.4) | two keys, one per station. Merging urgent items across stations by more than the rule in this row is out of scope for the first version; the hub takes one station until multi-station lands (`multi_station_off`). **What the hub does meanwhile, so that it never pushes twice:** an alarm item whose radio id is that of an alarm already open (not ended or cleared, **acknowledged or not**: station 1's alarm acknowledged at 4 s and station 2's item at 9 s are one push), on the same channel label (for P25: system and talkgroup), raised within `REPEAT_S` (120 s) from any station, is `applied` and **joins that alarm**: its key is recorded `raised` under the same alarm, the second station is noted as a second source, and there is no second alarm and no second push. Both alarms are placed on the hub's clock (`now - age_s`), so the 120 s compare without either station's clock. This is the alarm code's same-radio rule, which has no station in it today. It needs the two stations to use one label for the channel, which `docs/nodes.md`'s channel sets give. The alarm row's `traffic_id` must be station-qualified by then: two stations can produce the same `<label>-live-<ms>` |
| A page item | the hub keeps the highest `rev` per `(station, key)`: a higher one is `applied` (its live paging state for that station is set from it), an equal one `duplicate`, a lower one `stale`. Its expiry is the hub's own: `until` is the station's wall clock, so the hub uses only its distance from the same request's `sent_at` (its receipt time plus `until - sent_at`, kept within 0 to 300 s); with `until` null, 300 s after the last revision. So a live page whose `ended` revision never comes (capture died, or the uplink was down past the 10 min) ends on the hub by itself |
| `/api/station/state` `paging` from the app, for the same page | merged by `key`: the newer of the two, which is the later to arrive at the hub, wins. With the app up both lanes say the same thing. **Ended is final**: once the hub has a page as ended (an `ended` revision, a state entry with `ended`, or the finished `page` event's `live_key`), no later state post or revision makes that `(station, key)` live again |
| The finished page from the app's `PageHold` | arrives as a `page` event as today; it carries `live_key` so the hub can end the live entry at once (today `LivePaging.finished` does this on the station) |
| An unknown `kind` | `rejected`, `reason: "kind"`; the rest of the request is still taken |

Not "seen": like logs and metrics, a post to this route comes from another process than the link and must not hold
the station's "connected" up. The hub keeps its own `urgent_seen` time for Health.

#### Credentials

The same credential store as the app, as decided (Q1): `/etc/sizeup/station.env`, root 0600. Nothing about that
file changes, and no second copy of the secrets is made on disk.

| | |
|---|---|
| What capture needs | `SIZEUP_STATION_TOKEN` (the station's own `sz1` token) and, where the hub sits behind Cloudflare Access, `SIZEUP_ACCESS_CLIENT_ID` / `_SECRET`. The same identity as the app: no new token, no new Access application |
| At start | `LoadCredential=station.env:/etc/sizeup/station.env` in `sizeup-radio.service`: systemd, as root, copies the file into the unit's credentials directory, readable by the unit only. This is the pattern the feed adapter's credentials already use (#301: `/etc/credstore`, `ImportCredential=` / `LoadCredential=`; `deploy/station/README.md`). Capture reads the three names from it into memory, ignores anything else in it, and takes `CREDENTIALS_DIRECTORY` out of its environment, as `procenv.take_credentials` does |
| The file must exist | unlike the station unit's `EnvironmentFile=-`, a `LoadCredential=` whose source is missing **fails the unit's start** (`autoupdate.py` notes the same trap for the adapter's credentials). So the installer creates `/etc/sizeup/station.env` empty, root 0600, when it is not there, at install and in `activate()` before it starts or restarts `sizeup-radio`; `sizeup-station doctor` checks it. A capture that is already running is not affected by the file going away |
| With no credentials (the file empty, or no token in it) | capture starts and does everything but send: it receives, spools, writes urgent rows and frames (as in `write`), and the app's lane carries the alarms. `urgent.state = "no_credentials"`, a Health item. This is the station unit's own rule: "missing: the station still runs, records and queues" |
| Not `EnvironmentFile=` | the values are never in capture's environment, so they are in no child's environment or command line. (Today `child_env()` leaves the hub token in the environment of the station's `rtl_fm` children.) |
| Children (decided, Q-J) | capture starts every child (`rtl_fm`, `rtl_sdr`, the channelizer, probes) with `child_env()`, with `CREDENTIALS_DIRECTORY` and any token variable removed. The token is read into memory at start and on a reload, lives there only, and is never in a command line. trunk-recorder is its own unit and gets none of this |
| What that does not give | isolation from capture's own children: they run as the same user, and a process of that user can read the unit's credentials directory if it goes looking. Accepted for sizeup's own fixed binaries, and noted in §7.5. The app's token is as reachable for them today |
| Rotation without a restart (decided, Q-H) | `ExecReload=+/usr/local/lib/sizeup/radio-credentials`: the `+` runs it as root. It reads `station.env` and hands the three values to capture over the live socket as a `credentials` request. Capture accepts that `op` **only from a peer whose uid is 0** (`SO_PEERCRED`), swaps the two senders' headers, closes their connections so the next request uses the new ones, sets `urgent.creds_at_boot` in the status and logs that it did (never a value). **No receiver, squelch or queue is touched** |
| The helper checks who is listening | `/run/sizeup-radio` belongs to the `sizeup` user, so a process of that user could put its own socket at `live.sock` and wait to be handed the secrets. Before it writes anything the helper reads the listener's credentials from the connected socket (`SO_PEERCRED`) and goes on only if the pid is the unit's main pid (`$MAINPID`, which systemd gives an `ExecReload`) and the uid is the unit's user. Otherwise it sends nothing and exits non-zero: the reload fails where a person sees it |
| Why not a file capture re-reads | that would need the file readable by the `sizeup` group. `station.env` is root 0600, and `sizeup-station doctor` notes a `root:sizeup 640` one and says `secrets` puts it back to root 600 |
| Why not `RefreshOnReload=` | it would refresh the loaded credential on a reload, but it is newer than the station's systemd. `deploy/station/README.md` gives the station's as 255 (Ubuntu 24.04 / WSL2). That the setting arrived in a later release (257) is from memory of systemd's notes, not checked here |
| SIGHUP | ignored from the first line of `radiod.py`, before any import that takes time. The reload uses the helper, not a signal, and a stray signal must never end capture |
| A net under a forgotten reload | a 401 `token_unknown` or `token_revoked` shows as `refused` on Health with the hub's reason. Capture cannot fetch the new value itself: only root can read the file |
| Rotation, step by step | 1. An admin rotates the station's token on the hub (the old one stays valid 10 min). 2. `sudo sizeup-station secrets` on the station writes `station.env`. 3. It restarts `sizeup-station`, as today, and runs `systemctl reload sizeup-radio`. 4. The sender's next request carries the new token; the 10 min cover the seconds between 1 and 3. The radios never stop |
| Where the token may be sent | the hub's URL is read by capture from `[cloud] hub_url` in `/etc/sizeup/config.toml` (root-owned, `root:sizeup` 0640), with `tomllib`, as `trunkrecd.py` reads its own keys. It is **not** taken from the wish file, which the `sizeup` user can write: a wish must not be able to redirect the token. The same for what capture executes: `[radio] rtl_fm` and `[p25] recorder` come from the config |
| Never | in the wish file, the status file, a frame other than the root helper's request, the spool, `urgent.sqlite` or a log line. `last_error` holds a status and the hub's `reason` only |

#### The hub's hot/cold handoff

During a hub deploy the old task drains and the new one takes the lock; a station write in between is refused
before anything is done: 503 with `Retry-After` (`fence.refuse`; `docs/cloud.md` "Deploys: the hot/cold handoff").

| | |
|---|---|
| What the sender does | keeps the item, retries at the hub's `Retry-After` (0.5 s at least), and sends it the moment the new task serves |
| How long that is | not measured for this document. `docs/cloud.md` gives two figures for what a person sees, "about 35–60 s" and an expected 35 to 45 s that it qualifies (a smaller task size, before the takeover also waited for the replica) |
| How often | not counted. What exists is the count of link errors in the station's own log over the window: 57 answers of `503` [P], from Phase 1's station notes (not in the repository, and not in #765), which read them as one per hub deploy. The hub's deploys themselves were not counted |
| What it means | **the lane's decode-to-hub time is bounded by the hub's handoff, not by capture.** Capture removes the station's own gap; an emergency keyed during a hub handoff still waits for the hub |
| Decided for the first version (Q-A) | the handoff window is accepted as the urgent lane's bound. The bound that counts is the **write-refusal window**: how long, in one handoff, the hub answers a station's write with 503. The hub's maintainers measure it when they build the route (#776), and the figure is written here then. Until it is, 35 to 60 s stays what it is: `docs/cloud.md`'s figure for what a person sees, unmeasured for this purpose |
| The follow-up | a standby hub taking urgent items before it leads is built only if that measurement is bad. It is not part of the first version |
| Safety | a 503 applied nothing, so the retry is the first delivery. A timeout that did reach the hub is covered by the dedupe store |

#### What can go wrong in the lane

| Failure | What happens to an alarm |
|---|---|
| Capture is killed between the decode and the commit | if the request had not gone out either, **the alarm is lost**: it existed only in memory, and the transmission it came from is lost with the process. The window is the commit (a few ms [E]) or 50 ms, whichever is shorter. systemd starts capture again in 5 s |
| ... after the request went out, before the commit | the hub has it; capture does not know. The finished transmission is lost with the process, so nothing attaches to the alarm: it stands alone, as a key-up alarm on an unrecorded channel does today |
| The disk is full, or the insert fails | sent from memory, `durable: false`; lost only if capture also dies before the hub answers. The spool's free-space reserve (§4.1) is there so this does not happen through the spool's own growth |
| An `fsync` stalls | the send goes after 50 ms; the row follows when the disk does |
| The app is down | nothing changes for the send. The take-over waits |
| The hub answers `applied`, then crashes and comes back from a replica 30 s old | the `gen` rule: the alarm is sent again |
| The uplink is down for hours | the alarm stays `new` and is retried every 15 s; the station's page shows it as not delivered; when the hub answers, it raises it late with its age if under the cutoff (30 min × 1.1), else says `stale` (A7's recommended rule) |
| The station's wall clock steps | nothing: ages are on the boot clock |
| Two captures, or capture beside an inline app | cannot both hold `radio.lock` (§4.7) |
| The station's wall clock stepped back 40 min, and the app's take-over event reaches the hub first | the event looks 40 min old and raises nothing; the key is only `seen`; capture's item, with its boot-clock age, is `applied`. One alarm |
| Capture restarts after an alarm was answered, and the hub then comes back from an older replica | the row's `hub_gen` and answer time are in the file and are loaded at start; the next answer (an alarm's, or the 50 s `hello`'s) shows the new `gen`; the alarm is sent again |
| The hub restores while the senders are idle | the 50 s `hello` shows the new `gen`; the window is counted from the last sighting of the old one, so the alarm is sent again however long ago "now" it was |
| A P25 live alarm and its end-of-call flag, the station's clock a minute off the hub's | matched on the station, carried as `alarm_key`. One alarm |
| The same radio's bit still set on its next calls | no live alarm (no transition), no new end-of-call alarm: each flag joins the radio's open alarm (the hub's press window); the row says "emergency still set" |
| The wall clock stepped back an hour, then a real new P25 press 30 min later | the boot clock shows 30 min since the last set bit: a transition, an alarm |
| The wall clock stepped forward an hour, a stuck bit 2 s later | the wall clock advanced 60 min more than the boot clock: counted as not set (the frozen-clock rule), **one** repeated alarm, the safe side. The next sighting is 2 s on both clocks: nothing |
| The host slept 20 min with the boot clock frozen (same boot), then a real new P25 press | the boot clock shows seconds since the last set bit, the wall clock 20 min: counted as not set, an alarm |
| A P25 radio in emergency for 35 min while the app is down; its calls arrive in a burst when the app is back, ending 35, 28, 21, 14, 7 and 1 min ago | the 35 min call is past the cutoff: nothing raised, and the press window is not armed. The 28 min call is judged on its own: raised and pushed, labelled "from 28 min ago", and it arms the window. The others arrive seconds later by the hub's clock and join it as "still set". One alarm, one push. (As first written, the 35 min flag armed "still set" and every later call joined an alarm that did not exist: none) |
| A flag on a talkgroup whose notices are off, then the same radio on a dispatch talkgroup 5 min later | the first raises nothing and arms nothing; the second is judged on its own: one alarm |
| A live P25 alarm raised at 0; the radio's first call joins it; its next call ("disregard, accidental activation", still in emergency mode) runs from +330 to +360; the dispatcher clears at +380; that call's event reaches the hub at +420 | the call started 50 s before the clear; with H1m's boot age (about 90 s, `U` about 19 s) it is placed before the clear by more than `U`: it attaches to the cleared alarm, no push, the row says "emergency flag on a call from before the All clear". One alarm, one push. (As first written: a second alarm and push, "still set after All clear") |
| The same, but the call's event has no boot age and the skew is stale | the hub is unsure: the call alarms, labelled "emergency still set after All clear". A person looks; nothing is lost |
| A call's post comes after a reboot of the station (no boot age), and the new boot's clock is 40 s ahead; it is a real press 3 s after the All clear | the skew samples are of the new boot, not the call's: the skew path may not place it. The hub is unsure: an alarm and a push, labelled "emergency still set after All clear". (After the third revision it was attached with no push) |
| An older station (no boot age, no `wall_steps`); the clock steps back 30 s after the skew was measured and before a real press 3 s after the All clear | the skew path is not used: an alarm and a push, with the label |
| Capture's first send gets a 503; the app's take-over is answered 200 with outcome `seen` (a wall age that looked old, or notices off) | no `urgent_delivered`: the row stays `new`, capture's retry is `applied`, one alarm and one push, and the banner stays up until then. (After the third revision the row became `app` and nothing reached the hub) |
| The same with an older hub that gives no outcome | the app never sends `urgent_delivered`; capture sends the alarm |
| A trimmed alarm with a 300-character channel label | `source` is never cut: it joins the same radio's open alarm within `REPEAT_S` |
| A trimmed alarm that still gets a `too_large` in the hub's shape | final: `rejected`, a critical Health item; the app's lane carries it |
| An alarm's request gets no answer, then the hub's `gen` changes, then capture restarts | the row has no answer: it is still fresh (under 120 s), goes first, ahead of the resends and of the reloaded backlog |
| An `app` row with no `gen`, then a hub restore | in `send` mode it is sent again (in doubt); in `write` mode a Health warning names it for 24 h |
| A real new press 20 s after the All clear | its call starts after the clear: never attached. An alarm and a push |
| `urgent_since` with the default 200 rows, about 76 KB | the reply is filled while its frame stays within 64 KB and says `more: true`; the app asks again from the last `n`. (As first written the frame was over the limit and the take-over stalled) |
| 300 alarms in memory after the disk failed, then the app reconnects | memory goes first, page by page (`memory_after`), each page within the frame. Every alarm is taken over |
| `urgent-mode write`, one alarm whose `data` is over 48 KB (a garbled list of ids) | framed trimmed (`data.trimmed: true`), never dropped: the app takes it over and its lane delivers it |
| `urgent-mode write`, the app up: every alarm reaches the hub by the app's lane | the app sends `urgent_delivered` when the hub acks each take-over event; the row becomes `app` and the banner clears. (As first written every alarm kept "EMERGENCY not delivered to the hub" for good) |
| A switch from `write` to `send` with 40 old alarms delivered by the app, then a fresh alarm | the 40 are `app` rows: never sent by capture. The fresh alarm is in the first request. With 40 old alarms the app never delivered (it was down), the fresh one still goes first: fresh, then resends, then the backlog. (As first written it missed the first request, and 32 late notices came back) |
| The hub answers `too_large` for one alarm alone | the alarm is trimmed and sent alone again; not counted as a refusal |
| A channel label starting with `#`, or 150 characters long | the slug is mended with a short hash: a valid id and key. Today's code gives an id that fails `CLIP_ID` |
| More than 32 alarms in the resend window, then capture restarts | the rows over the cap carry `lost_gen`; Health's lost-alarm warning is rebuilt from them at start |
| `reception_start` sent, capture restarts before replying, the app sends the same `id` again | the `id` was recorded before applying: the first reply is given again, or `interrupted` if capture stopped mid-apply. Never two tests |
| An end-of-call alarm with no boot age reaches the hub by the events lane | its push age is the hub's `now` minus the station's `end`: labelled approximate |
| A P25 alarm raised, then All clear; 20 real min later a new press, with the station's clock now stepped 15 min behind (the calls' own times 5 min apart) | the All clear ended the window, and by the hub's receive times 20 min passed anyway: judged on its own, a new alarm. The same without the All clear: 20 min ≥ 10 min, a new alarm |
| The station reboots with its wall clock 1 h fast; the reloaded alarm goes with `age_src: "wall"`, age 3700 s, and is answered `stale`; then the app's take-over and the finished transmission arrive with their true times, 2 min old | the key is `stale` with `stale_src: wall`, which blocks nothing: the events are judged on their own and raise the alarm. One alarm, one push, and the late notice beside it |
| A boot-clock age over-counts by 4.7 % (#788): an alarm 31.5 real min old (1890 s) is sent with `age_s` 1979 s | under the cutoff of 1980 s (30 min × 1.1): `applied`, raised and pushed with its age |
| A power cut rolls `SEQ` back to 4 after items up to 10 were handled and pruned (`items/` empty, the cursor at 10) | at start the next number is max(4, none on disk, 10) + 1 = 11. The next item, an emergency or not, is above the cursor and handled; `SEQ` behind the cursor is logged and counted. (As first written it got 5, at or under the cursor: counted as done and never handled) |
| An edge or a proxy answers a lone pending alarm with a 422 and no hub body | not the hub's shape: no answer. Backed off, not a try, sent again. (As first written a lone 422 was final) |
| The hub itself refuses a lone pending alarm with 422 `bad_batch` | the alarm is marked alone and sent again on its own after the backoff; a second refusal of it alone makes it `rejected`, with a critical Health item, and the app's lane carries it |
| `200 {}` five times with three alarms pending; or a 200 that fails the response schema | each is no answer: all three stay `new`, no try is counted, nothing is marked `sent`. They go when the hub answers in its shape |
| One alarm whose `data` is over 64 KB on its own (a garbled list of ids) | never sent as an oversize request: sent trimmed (`trimmed: true`; identity and matching fields whole, `ids` left out); the app's lane carries it too, trimmed the same way, and the finished transmission brings its ids. Never dropped |
| The hub restores from a replica that kept the key as `seen` but lost the `raised` upgrade and the alarm | the resend rule sends the item again; the store says `seen`, so it is judged on its own: `applied`, one alarm again (and a second push: the first alarm is gone) |
| Station 1's alarm is acknowledged at 4 s; station 2's item for the same press arrives at 9 s | the alarm is still open (acknowledged, not cleared): station 2's item joins it. One push |
| More than 32 alarms in the resend window | the newest 32 by answer time (then by `n`) are sent again; the rest are named in a critical Health item |
| A spool item whose only emergency sign is the call-level `emergency: true`, with the spool over its bounds | an emergency item: its audio is never dropped |
| A page's `ended` revision reached the hub; then a state post that still shows the page | ended is final: the page stays ended |
| Two stations hear one press | two items, one alarm, one push: the second joins the first by radio id and channel within 120 s |
| A page lasts 130 s; its `ended` revision is made after the first tone's 120 s | each revision has its own 120 s, and `ended` has 10 min: it is sent. If it never is, the hub ends the entry itself after 300 s |
| The hub answers an alarm with a word capture does not know, again and again | 5 tries, each after the healthy items, then `rejected` with a critical Health item; the app's lane carries the alarm |
| More than 256 items wait in memory | page items go first; an alarm without a row is never dropped for room |
| `urgent-mode write` (no sender), the station's wall clock stepped back more than 30 min | the alarm reaches the hub only by the app's lane, whose event looks too old: **dropped, as it would be today**, unless H1m is in. H1m is now required in the first version (§11), so this is closed; `send` mode closes it too |

#### The app's take-over

| Kind | With the hub link on | All-in-one, or the link off |
|---|---|---|
| `alarm` | an `alarm` event with `id` = `key` into the outbox (what `_alarm_now` sends today), in the transaction that moves the cursor `(urgent_epoch, n)` | `cad.alarms.on_traffic`, as `_alarm_now` branches today |
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
| `paths` | object | data directories only, each optional with the station's default: `spool`, `urgent_db`, `clips`, `captures`, `state`, `recorder_dir`. What capture executes (`rtl_fm`, `rtl_sdr`, the recorder) is not here: it reads those from the root-owned config (§4.3) |
| `hub` | object | `forward` (bool: the link is on), `station` (the id the hub assigned), `caps` (list; `urgent` among them when the hub has the route). No secret, and no URL: capture reads the hub's URL from the root-owned config (§4.3), and ignores a key named `url`, `hub_url`, `token`, `secret`, `access_id` or `access_secret` here, saying so in `wish_problem` |
| `channels` | list | the channel set. Per `[[radio.channels]]` entry: `label`, `kind`, `mhz`, `gain_db`, `device`, `open_db`, `drop_db`, `hang_s`, `continue_s`, `keyups`, `split_s`, `jump_db`, `schedule`, `also`, `record` (bool) |
| `airband` | object | `multichannel`; the owner rule for a frequency two dongles hear (`config.airband_owner` today; its shape, like the `p25` block's apart from `talkgroups`, is fixed when the code that needs it is written, not here) |
| `squelch` | `{label: {open_db, close_db, min_open_s}}` | the values to run, already fitted by `channels.fit_squelch`. Applied live (`set_squelch`), no restart |
| `tones` | `{plan: [{a_hz, b_hz, names}], ours: [name]}` | for `LivePaging`: the names a live page shows, and which are ours |
| `aliases` | `{radio id: name}` | for an alarm's `unit` |
| `dongles.jobs` | `{serial: {job, by}}` | the job each dongle should run: a channel label, the P25 job, a system job, or `Off`. `by` is `person` here; what auto-assign and the groups decide is capture's own (below) |
| `dongles.groups` | list of `{name, sdrs, jobs, parked}` | the antenna groups as saved (`groups.parse`'s shape), parks included |
| `dongles.reserved` | `{adsb, ais}` | the serials set aside for the ADS-B and AIS decoders; never given a job |
| `dongles` (rest) | | from the `[radio]` settings (`sizeup/config.py`): `listed` (`sdrs` when it is a list, and the single `device` of a one-dongle bench), `auto` (`sdrs = "auto"`), `ignore` (`sdr_ignore`), `auto_assign` (`auto_assign`), `order` (`auto_assign_order`), `start_mode` (`start_mode`) |
| `p25` | object | the county recorder and each system's directory, name, label, sources and jobs; talkgroup labels and unit aliases for `parse_call`; `talkgroups`: `{system: {talkgroup number as digits: {alert, kept, encrypted}}}`, the facts the reception test needs, resolved by the app (§3.3) |
| `metrics` | bool | sample `floor` and `cc` records |
| `spool` | object | `max_age_h` (12), `max_bytes` (2^31), `max_items` (50,000), `min_free_bytes` (2^28), `sync_s` (5): the bounds of §4.1 with their defaults |

| Rule | |
|---|---|
| Missing, unreadable or not an object | capture keeps running what it last accepted (it keeps a copy, `radio_wish.applied.json`), says so in the status (`wish_problem`), and never reads a broken file as "everything off", which is what `trunkrecd.wanted` does with its own. With no accepted wish at all it idles |
| A wish that cannot be met yet | is not an error. A job whose dongle another app holds, or that is parked, simply shows as `held`, `shed` or `waiting` in the status until it can run |
| A wish that fails validation (decided: `check` is advisory, capture validates on apply) | facts can change between the check and the write, so capture validates every part of every wish it applies: the groups with `groups.parse` and the pinned fire order, a reserved dongle, a job that does not exist, a squelch out of range. A part that fails is **not applied**: capture keeps running what it ran for that part, applies the rest, and lists it in the status as `refusals: [{what, key, reason, wish_seq}]` (`what`: `job` \| `groups` \| `adsb` \| `squelch` \| `channel`). The entry stays until a wish without the fault is applied. The app shows it where the edit was made and as a Health item; it never retries by itself |
| Who writes what | the app is the only writer of the wish. A person's pick is asked on the socket first (`pick`); capture answers and, on yes, applies it at once; the app then writes it into `dongles.jobs`, which is what makes it survive a restart. A squelch Apply goes the same way (`squelch_apply`, then `squelch`). Group edits and the ADS-B dongle differ: `check` changes nothing, and it is the wish write that applies them |

What capture decides itself is not in the wish. It keeps it in its own files, in today's formats, and reports it in
the status: auto-assigned jobs, a group's stand-ins (`antenna_standins.json`), dongles out for no audio
(`antenna_noaudio.json`), flap holds, a reception test's loans (`reception_*.json`), the wish files it writes for
`trunkrecd` (`p25_recorder*.json`). The app keeps `sdr_modes.json`, `antenna_groups.json` and `adsb_receiver.json`
as its own record of the desired state, in today's formats, and (decided, Q-E) mirrors the jobs capture assigned
itself from the status into `sdr_modes.json` with `_set_by: "auto"`. That is what makes a switch back to inline mode
safe: the inline app finds every dongle on the job it last ran.

### 4.5 Control, observed state: the status file

`/run/sizeup-radio/status.json`, compact JSON, tmp + rename, every 0.5 s and at once when something changes. It holds
what capture **observes**, never a reply (those are on the socket).

| Field | Meaning |
|---|---|
| `v`, `at`, `at_boot`, `boot`, `pid`, `impl`, `build`, `contract`, `started`, `wall_steps` | `wall_steps`: the wall steps capture counted since boot (§4.3 Clocks); the app puts it and `boot` on its state post. A reader takes capture as down, and adds `stale: true`, when its own `CLOCK_BOOTTIME` is more than 15 s past `at_boot`, or when `boot` is not its own boot (a file left from before a reboot). `at` (wall) is for people |
| `wish_seq`, `wish_problem` | the wish it runs (0 while none was accepted); why a newer file could not be read at all |
| `refusals` | list of `{what, key, reason, wish_seq}`: the parts of the applied wish that failed validation and were not applied (§4.4). Empty when the wish runs as written |
| `reader` | `{attached, since}`: an app holds the socket as `reader` |
| `channels` | per channel: `label`, `kind`, `mhz`, `sdr`, `run`, `state`, `detail`, `restarts`, `open`, `open_s`, `stuck`, `last_heard`, `queued`, `quieting_db`, `hiss_db`, `floor_db`, `squelch` (`open_db`, `close_db`, `min_open_s` as running), `short_per_min`, `now` (airband), `ab` |
| `channels[].run` | `running` \| `waiting` (no dongle, by choice or for lack of one) \| `out` (a dongle streaming nothing, #710) \| `held` (another app) \| `shed` (to a higher job of its group) \| `not_in_set` \| `local`: exactly the words of `docs/nodes.md` (#758), which reports them as a node's capability. `state` beside it keeps today's row word (`ok`, `off`, `down`, `absent`, ...) |
| `sdrs` | per dongle, the rows of `App.sdr()`: `serial`, `name`, `product`, `present`, `job` (today's `mode`), `set_by` (`person` \| `auto`), `held_by` (`app`, `mode`), `noaudio`, `ambiguous`, `lent`, the group fields |
| `groups`, `groups_problem`, `no_park`, `duplicates`, `adsb` | as `App.sdr()` |
| `p25` | a map by system short name; per system: `state`, `detail`, `cc` (`ControlChannel.status()`), `last_call`, `mark`, `seen` (the size of the ingest's set, for the soak test) |
| `reception` | `Tests.state()`: the running test and the history. The app shows it; it runs nothing of the test (Q7) |
| `spool` | `{spool_epoch, seq, pending, oldest_s, bytes, free_bytes, dropped, dropped_audio, quarantined, synced_at, seq_behind}`; `seq_behind`: starts at which `SEQ` was found below the cursor or a file name (§4.1) |
| `urgent` | `{urgent_epoch, mode, state, reason, pending, pending_alarms, unsent, undurable, oldest_alarm_s, last_sent_at, last_ms, sent, delivered_app, rejected, expired, lost, last_error, hub_gen, creds_at_boot, p25_transitions_1h, resets, reset_at}`. `mode`: the `urgent-mode` in effect, `write` or `send`, never `off` (the file's `off` is `write` in service mode). `state` is the senders' condition, **one word, the first of these that holds**: `off` (the mode is not `send`, `hub.forward` is false, or the hub does not advertise `urgent`), `no_credentials`, `no_route`, `refused`, `retrying`, `ok`. A reset of the file is not a state, since it can hold beside any of them: `resets` counts them since capture started and `reset_at` is the last one's time (null for none); Health shows a reset for 24 h. `unsent`: the alarms the hub has not had by either lane (no answer to capture, no `urgent_delivered`), each `{key, channel, unit, event_at, age_s}`, for the station's page; `delivered_app` counts rows marked `app`; `lost`: the alarms whose `lost_at` is in the last 24 h, each `{key, channel, unit, event_at}`; `expired` counts page items only |

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
`apply_grants`; `time` is Unix seconds; `via` is `Turn.via` of `sizeup/radio/p25.py`: `srclist` \| `grant` (the
talker named by a live grant) \| `split` (cut at a grant)), `grants_applied` (bool), `tr` (the file name of
trunk-recorder's own JSON, a hard link, kept for `captures/emergency/`; it is linked as `<seq>-<id>.tr`, **not**
`.json`, because a reader takes every `*.json` in `items/` for a sidecar), `alarm_key` (the key of the live P25
alarm capture matched this call to, or `""`: §4.3, #775), `start_boot` (the call's start on `CLOCK_BOOTTIME`:
`closed_boot` minus the wall time from `start` to the ingest; left out when a wall step came in between; for H1m),
and `audio` with trunk-recorder's own rate (`rate_hz` as
written; the reader resamples as `read_wav_16k` does). A call has no squelch: its `closed_boot` is the moment
capture's ingest took the finished call, and the wall age `now - end` covers the wait before that, since
`late` takes the larger.

The three flaws the evidence named, two more met on the way, and their fix here:

| Flaw | Fix |
|---|---|
| No completion marker: the JSON can appear before the WAV is whole; the guard is "same size on two polls 1 s apart" | capture does that wait once, then publishes a sidecar by rename. The app sees only complete items. A marker from trunk-recorder itself would remove the wait; whether its per-call script hook fires after both files are closed was not checked against the pinned build (Q8) |
| Seen-at-start: calls written while the reader is down are marked seen and lost (#747) | the mark of #753 for capture; the spool cursor for the app |
| Regex over free log text | not removed, but confined: `trunkrecd.py` already parses every line (`Watchdog`, `Shipper`). It can also write what it parsed (decode rate, call start and end, unit events) as JSON lines beside its status, and capture reads that instead of a second set of regexes in `live.py`, `recorder.py`, `systems.py`, `reception.py`. One parser of third-party text, in the supervisor. It changes `tr_sig` once. A separate PR, not needed for the split |
| One malformed sidecar discards the other calls of the same poll (`scan()` raising out) | per-call `try` in capture's ingest |
| `CallIngest._seen` and `_pending` grow for the life of the process (one entry per call), which a restart 12.8 times a day hid | pruned of entries whose files are gone: ingest deletes them (decided, Q12; step 3 of §6) |

P25 emergencies (decided, Q3 and Q-B). The end-of-call flag (`emergency`, `srcList[].emergency` in the call's JSON)
stays with `App._call`: that decision needs the emergency windows, the stuck-flag rule and "is it ours", which are
CAD state. The live path is the grant's emergency bit from the trunk-recorder patch, on a transition only, in the
urgent lane, deduped with the end-of-call flag (§4.3; #775). **Until #775 lands a P25 emergency reaches the hub when
its call ends**, and one that ends while the app is down is raised when the app is back, labelled late, as #753
proposes.

### 4.7 Version skew and rollback

| Case | Behaviour |
|---|---|
| The app is newer than capture (the usual update) | unknown keys in the wish are ignored; a request with an unknown `op` is answered `unsupported`. The app reads `contract` from the status and from `hello` |
| Capture is newer | unknown sidecar and frame keys are ignored; an unknown `kind` or `t` is skipped (and acknowledged, for the spool) |
| The contract's major version differs | the app raises a critical Health item and does not read the spool; capture keeps capturing and the spool fills to its bound. The updater bumps both together, so this is a failed half-update |
| Capture's code changed | `radio_sig` differs: the updater restarts it. The app does not need to restart for that |
| Two owners of the dongles | cannot happen: `radio.lock`, a lifetime `flock` on `/var/lib/sizeup/state/radio.lock`, is taken before anything else is touched: the spool's write side, `urgent.sqlite`, a claim, a dongle. Capture takes it in service mode, the app in inline mode. The app that cannot get it starts no receivers and says so on its page and Health. Capture waits for it up to 60 s (an app that is restarting lets go within seconds), then exits 78. A second capture started by hand meets the same lock. The shadow instance takes a lock of its own (`radio-shadow.lock`) and never this one |
| Capture started while the mode file does not say `service` (it says `inline` or `shadow`, or is missing) | exit 78, before the lock. It also looks at the mode file on every wish look: when it no longer says `service`, capture drains, releases and exits 78 |
| The switch from inline to service, in order | one command, `sizeup-station capture service`, does it: 1. it refuses unless `urgent-mode` is `write` or `send`. 2. It writes `capture-mode`. 3. It restarts `sizeup-station`: the app stops its receivers, lets go of `radio.lock`, comes back in service mode with no receivers, and writes the wish. 4. It starts `sizeup-radio` (after `systemctl reset-failed`, since an earlier exit 78 leaves the unit failed), which takes the lock and the dongles. The gap is the app's stop plus capture's start, a few seconds, once. Done the other way round (capture first) it also ends well: capture waits its 60 s for the lock |
| Left half-switched | the mode says `service`, the app is in service mode, and capture is not running (it exited 78, or step 4 never ran): **no radio is being received.** The app sees no fresh status file: a critical Health item and a banner on the station's page, "capture is not running", and the hub's station row says the same. The health check starts an inactive `sizeup-radio` while the mode file says `service` (§3.5), so this state lasts at most its three looks. The other half-state, the mode says `service` but the app was never restarted and still receives inline: capture exits 78 after 60 s, the app reports the mismatch (`capture: mode file service, running inline`) as a warning, and nothing is lost |
| Rollback to a release from before the split | that release's installer knows nothing of the unit, the lock or the mode file. Capture compares the release behind `/opt/sizeup/current` on each wish look: when that release has no `radio=` line in `BUILDS`, capture releases its dongles and exits 78 (not restarted). The old app then claims them as today. Meanwhile `claims.claim` already refuses a dongle held by another live `sizeup` pid, so the two never open one dongle |
| A hub without the urgent route (older, or rolled back) | it does not advertise `urgent`; the app leaves it out of `hub.caps`; capture writes the rows and does not send; the app's lane carries everything, as today. A 404 from a hub that did advertise it: `no_route`, tried again every 5 min |
| The mode is switched back to inline | 1. `capture-mode` is set to `inline`. 2. `sizeup-radio` is stopped: it drains every worker into the spool, gives its senders 2 s, syncs, releases the lock. 3. `sizeup-station` is restarted. Having the lock, it first **drains what capture left**: it reads the spool to its end with the service-mode reader (every item handled and acknowledged, late or not), and opens `urgent.sqlite` itself (it is now the one opener) to take over every row past its cursor and to send, through its own lane, any alarm still `new`. Only then does it start its own receivers. The same state files are the app's again. (`capture-mode` is a one-word file, not a `config.toml` key: the app's config reader refuses unknown keys, which would break an older release) |

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
| Durable | `Outbox.add`, text only, `synchronous = NORMAL` (no fsync per commit): 0.035 ms median, p95 0.23, p99 4.5, max 297 ms [L, loaded machine], on the reader thread, in a file the clip commits share | one insert in `urgent.sqlite`, `synchronous = FULL`, on the `urgent` thread, the file's only user. Locally 0.11 ms median [L], but macOS `fsync` does not flush the drive: on the station's disk an fsync'd commit is a few ms [E]. Not measured there |
| **Decode to durable. Budget: under 100 ms** | under 1 ms median, not fsync'd [L] | a few ms, fsync'd [E]; nothing can queue in front of it. If the disk stalls or fails, the item is not durable, and the send goes anyway after 50 ms. Step 8b measures it in production before any switch |
| To the hub | wake `hub-forward`, `POST /api/station/events`: one round trip behind whatever the outbox already holds | `urgent-send` takes the item from memory, waits for its commit 50 ms at most, `POST /api/station/urgent` on the alarms' own kept-alive connection: one round trip, behind nothing but another alarm. A cold connection adds a TLS handshake through the edge, 100 to 200 ms by `client.py`'s note; the keep-warm is there to avoid it |
| **Decode to hub. Budget: under 1 s** (the review's test) | the round trip, when the app is up and the outbox is empty | the decode step + a few ms + one round trip. The round trip itself is not in the evidence (0.3 ms is the client's own share against a local stub [L]); step 8b logs it per item (`last_ms`) |
| **App restarting** | not decoded: capture is down with it | the two rows above, unchanged: capture does not need the app |
| Hub in a deploy's handoff | waits in the outbox; sent when the new task serves | waits in capture; 503 + `Retry-After`, sent on the first retry after the new task serves. `docs/cloud.md` puts the handoff at 35 to 60 s; it was not measured for this document, nor how often it happens |
| Uplink or hub down | waits in the outbox; an alarm that arrives more than 30 min late raises nothing on the hub (logged) | waits in capture and is retried every 15 s, for as long as it takes; the station's page shows it as not delivered after 10 s; the hub raises one under its cutoff (30 min × 1.1) with its age, and answers `stale` for an older one (A7's recommended rule) |

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
on the way to the hub, one fsync'd commit, both unmeasured on the station. `closed_boot` and `published_boot` in
every sidecar, a timing line in the reader and the sender's `last_ms` make them measurable before anything is
switched (steps 6 and 8b of §6).

## 6. Migration

Small PRs, each shippable alone, each reversible by a switch that is off by default. Two one-word files, neither a
`config.toml` key: `/etc/sizeup/capture-mode` (`inline`, the default and what unit tests use; `shadow`; `service`)
and `/etc/sizeup/urgent-mode` (`off`, the default; `write`; `send`; service mode needs `write` or `send`, §4.3).
Owner: **R** the radio session (code), **N** the native session (schemas, conformance, compare tooling), **U** the
updater's owner, **H** the hub's maintainers.

Land first; none is part of this plan:

| PR | Why |
|---|---|
| #767 | the per-channel workers this design uses on both sides |
| #753 | the P25 mark and the `late` rules |
| #762 | the outbox's quarantine: the take-over event rides that lane |
| #765, #774 | `docs/native/` and the ADRs; `contracts/` with `routes.json` and its conformance harness, which §10's files join |
| The nodes registry (`docs/nodes.md` §5, steps 1 and 2) | for H1's role check: the route is opened by an `sz1` credential with the `radio-receive` role (decided). Until roles exist a station's `sz1` token stands for it, as on every station route today |

| # | PR | Switch | Tests | Owner |
|---|---|---|---|---|
| 1 | The schemas and vectors of §10, in `contracts/capture/` | none | the vectors validate; every example in this document is one | N |
| 2 | Move `App._mdc`, `_mdc_at`, `_via`, `keyup_link` and the close-time facts into `radio/ids.py` and `radio/segmeta.py`; `App._segment` calls them. No behaviour change | none | the existing radio suites unchanged; a test that the facts equal what the handler computed before, on the synthetic key-up scenarios of `test_keyups.py` | R |
| 3 | `CallIngest`: prune `_seen` and `_pending` of entries whose files are gone (ingest deletes them); a bad sidecar no longer discards the rest of its poll (Q12) | none | the set stays bounded over 100,000 synthetic calls; a call still on disk is never taken twice; `test_p25*` unchanged | R |
| 4 | `radio/spool.py`: writer, reader, cursor, pruning, the periodic sync, the free-space reserve, the quarantine of a poison item. Unused | none | atomicity (kill between the renames), recovery of `SEQ` and `spool_epoch`; `SEQ` rolled back by a power cut (`SEQ` 4, `items/` empty, the cursor at 10): the next item is 11 and is handled, and `check_spool` reports `SEQ` behind; per-channel order, redelivery and dedupe, bounds, an emergency item by each of its four signs (an `ids` entry, a turn, the call-level flag, an `alarm_key`) never losing its audio, the reserve reached, the sync at `sync_s` and at stop, a handler that always raises (quarantined after 3 tries, the cursor moves on, the next item is handled); the schema vectors of PR 1 | R, N for conformance |
| 5 | Inline tee: in `inline` mode with a debug switch, the worker also writes each segment to a spool, and a test reader runs the handlers from it | `capture-mode inline` + a debug flag | handler output from the spool equals handler output from the `Segment`, row for row, over the radio suites' scenarios (this is also where s16 against float32 shows, if it shows). `late` by age on the boot clock: 59 s live, 61 s late, an emergency at any age, and the larger of the two ages decides; the wall clock stepped back an hour changes nothing, stepped forward makes rows late and never drops or delays an emergency; the boot clock frozen for an hour makes rows late by the wall clock | R |
| 6 | PCM tee and `radiod.py` in shadow: the in-process receiver copies each chunk it read, with its arrival time, to a socket; `sizeup-radio --shadow` runs the same receivers from that instead of `rtl_fm`, owns no dongle, writes `spool-shadow/`, a status and the live socket, and sends nothing | `capture-mode shadow` | `tools/spool_compare.py`: per channel, the same transmissions (start and end equal, since the tee carries the clock), bit-equal audio, equal IDs, levels and reasons. On the station it logs counts and differences only, never content | R; compare tool N |
| 7 | `radio/dongles.py`: the dongle half of `App` as a class the `App` delegates to, `reception.py` with it, its `App` reach-ins cut as §3.3 lists (Q7). No behaviour change. Splittable: modes and auto-assign; groups; no-audio, probes, USB reset; the reception test | none | `test_antenna_groups`, `test_sdr_auto_assign`, `test_app`, `test_reception*`, `test_sdr_serial`, `test_claims` unchanged | R |
| H1 | The hub: `POST /api/station/urgent`, the dedupe store with its three states written by both lanes, `gen` in its answers and in `hello`'s, the `urgent` capability, the allow-list row promoted to served (§11). **Before 8b**, or in one PR with it | the capability is advertised only when a hub setting says so | §11 | H |
| 8a | `radio/urgent.py`, the part that never speaks to the hub: the file, the clocks, the frames, the app's take-over. In-process first: in `inline` mode the `App` hosts the same code. No string of the new route exists under `sizeup/` yet | `urgent-mode`: `off`, then `write` | the nine combinations of the two mode files (§4.3), one alarm each; an item whose insert failed is taken over once, and once more is not when its row appears; `urgent_since` paged, with the in-memory items; rows loaded at start; the memory bound: with 300 items waiting, pages go and no alarm without a row does; `urgent_since` paged within the 64 KB frame: 200 rows of about 380 bytes give a short page with `more: true`, 300 memory items come over several pages by `memory_after`, and no frame is over 65,536 bytes; an alarm over 48 KB framed trimmed, never dropped; `urgent_delivered` makes the row `app`, clears `unsent` and the banner, and such a row is never sent after a switch to `send`; an unknown key in `urgent_delivered` changes nothing; ids and keys for labels starting with `#` or `(` and for a 150-character label are valid; a P25 live alarm always carries `key_time`; the lost-alarm warning rebuilt from `lost_gen` after a restart; a call's `start_boot`, and none across a wall step; `wall_steps` counted once per step, and reset with the boot id; the trim keeps every identity and matching field whole (a 300-character `source` is never cut); a trimmed item refused `too_large` again is final with a critical Health item; `lost_at` set with `lost_gen`, the warning gone 24 h after it; the requests table holds only ops with effects; a new `pid`, and the app asks from `memory_after: 0`; a page's revisions each with their own 120 s, an `ended` one made at 130 s still sendable; the P25 bit's transition on the boot clock (a real press 30 min after a wall step back alarms; a stuck bit 2 s after a step forward of an hour alarms once, by the frozen-clock rule, and not at its next sighting; a real press after the boot clock froze for 20 min on the same boot alarms; the measured drift of 5 % over 10 min does not; across a reboot, once); the wall-step rule: offset changes of 2 s every 30 s are not steps, 5.1 s within 5 s is one step, logged once with its size; capture's match of a call to its live alarm, and through a wall step by the boot clock; `test_radio_emergency`, `test_fg_emergency`, `test_alarms` unchanged | R |
| 8b | The two senders, the 50 s `hello`, the resend rule. **After H1, or in one PR with it**: a client string `/api/station/urgent` under `sizeup/` fails the fail-closed route test until `contracts/routes.json` lists the route as served, and listing it as served fails until the hub serves it. With its **own** `HubClient` credentials in `inline` mode (the app has them in its environment already; the unit's credential handling is step 11's), so both budgets are measured in production while there is still one process | `urgent-mode send`. **Exit criteria before `send` is left on:** both budgets measured on the station, and the hub's write-refusal window measured over real deploys and written into §4.3 (#776) | §4.3's idempotency table case by case against a stub hub, the "app's lane first, T took over, acknowledged, then the urgent item" case among them; each failure class (503 with `Retry-After`, a timeout after the body went, 401, 404, a 413 cut to singles); the insert failing and the `fsync` stalled: the item is sent within 50 ms plus the round trip, `durable: false`; an alarm unsent for 29 min is still sent, answered `applied`, and raised and pushed with its age; one with `age_s` 1979 s too (the 10 % margin); one with `age_s` 1980 s is still sent, answered `stale`, and becomes a late notice with no push (A7's recommended rule, both sides); the station's page showed each from 10 s on; an alarm reloaded after a reboot is sent with `age_src: "wall"` and a wall-clock age, never held back; a page request hung for its whole timeout does not delay an alarm; the resend rule, each of: an alarm answered, capture restarted, the hub back from an older replica: sent again; the hub restored while the senders were idle for 200 s: the `hello` shows the new `gen` and the alarm is sent again; `gen` 5 and "5" are one; a row from another boot in doubt is sent; an answer with an unknown word 5 times: `rejected`, a critical Health item, never in front of another alarm; a lone 422 in the hub's shape (`bad_batch`): sent alone again, and `rejected` only at the second such refusal, the app's lane still carrying it; a lone 400, 413 or 422 with no hub body, or another `reason`: no answer, backed off, no try counted; five `200 {}` answers with three alarms pending: all three still `new`, no try counted; a 200 that fails the response schema: nothing marked `sent`; an item missing from a good 200: sent again, no try counted; one alarm over 64 KB alone: sent trimmed (`trimmed: true`), never as an oversize request, never dropped; a request never encodes over 65,536 bytes; the resend cap: 40 alarms in the window, the newest 32 by answer time sent, ties by `n`, the other 8 on Health; the order in a request: 40 backlog alarms and 10 resends waiting, then a fresh alarm: the fresh one is in the next request, first; fresh is read from the row: an alarm whose request got no answer is still fresh after a `gen` change and after a restart; an alarm of a batch refused whole is retried alone at the head; `urgent_delivered` only on `raised`, `joined`, `duplicate` or `suppressed`: for `seen`, `stale` or no outcome the row stays `new` and capture sends it; an `app` row with no `gen` resent after a restore; a lone `too_large` in the hub's shape: trimmed and sent again, not counted; an item that fails the local check: never in a batch, `rejected` as `invalid`, the other alarms sent together; every size counted as UTF-8 bytes of the JSON as sent; the app's lane first with a wall time 40 min old (the clock stepped back), then the item: one alarm; a `traffic` event with `alarm_key` and `emergency: false` first, then the item: one alarm; the wall clock stepped back an hour: no page item expires; stepped forward an hour: a page item may expire early (the larger of its two ages), an alarm never does, and an alarm's `age_s` is unchanged either way; the three things that stop the sending, each alone; a commit landing after the hub's answer leaves no false "not delivered"; the first alarm's time to the hub on a warm and on a cold connection | R |
| 9 | `radio/control.py`: the wish file (desired state), the socket's requests and replies, the status file (observed state). In `inline` mode the `App` talks to its own `Dongles` through the same code (a loopback), so the request path is exercised before there are two processes | `capture-mode inline` | every refusal of today's `set_mode` comes back as a reply ("parked", "taken", `confirm: true`); `squelch_apply` with a stale `expect` is refused, as #685 and #700 refuse it; a repeated `id` gets the first reply and is applied once; a request past `expires_boot` is not applied, whatever the wall clock did; a broken wish keeps the last good one; a wish that cannot be met shows as `held` or `waiting`; a wish that fails validation after a passing `check` is not applied, shows in `refusals`, and the rest of it is applied; a `credentials` request from a peer that is not uid 0 is refused; a `reception_start` repeated under its `id` after capture restarted gets the first reply and starts no second test; one recorded with no reply gets `interrupted`; unknown keys and ops | R, N for conformance |
| 10 | The soak (Q12): a week of synthetic traffic, accelerated (the fake `rtl_fm` and fake call files on a fast clock), through `radiod.py` in service mode with a reader attached | - | resident memory flat after warm-up; these stay bounded: `CallIngest._seen` and `_pending`, the tap's per-carrier `last`, the `SegmentWorkers` queues, `LiveTalk`'s grant log, the socket's client queues, `urgent.sqlite`'s rows and its `-wal` file, the spool's items and its quarantine; no growth in open file descriptors or threads. It must pass before step 11 | R, N for the harness |
| 11 | The switch: the unit with `LoadCredential=` and the root reload helper, `start-radio`, `radio.lock`, `radio_sig`, the build directory, `health-check`, `sdr-serial`; in `service` mode the app starts no receivers and runs `spool-reader` into its own worker sets (§3.6). One station, by hand, with the admin's go | `capture-mode service` | the end-to-end harness below; the secrets never in `os.environ`, in a child's environment or command line, the status, the file or the log; `systemctl reload` swaps the token with every `rtl_fm` pid unchanged and no frame late; a SIGHUP is ignored; the reload helper hands nothing to a listener that is not the unit's main pid; the unit starts with `station.env` empty and runs in `no_credentials`; a second capture exits 78 after its wait; capture started with the mode file saying `inline` exits 78; the switch to service in order and in the wrong order; left half-switched, Health is critical and the health check starts capture; the switch back drains the spool (§4.7); both drains of §3.6; `station-install` CI with the new unit; `.github/scripts/station-update.sh` extended: an app-only update leaves capture's pid, a capture change restarts it, a rollback stops it | R, U |
| 12 | The updater: app-only updates stop waiting for carriers; `radios_back` unchanged | `capture-mode service` | `test_autoupdate` cases for both kinds of update | U |
| 13 | Cleanup: the shadow tee stays as the native daemon's test bench; inline mode stays for tests, macOS and all-in-one installs. Nothing else is removed until a second station has run `service` | - | - | R |

The end-to-end harness (constraint 9), synthetic audio only: a fake `rtl_fm` (as `tests/test_squelch_stuck.py` has)
feeding a real `radiod.py`, a real spool, socket, wish file and `urgent.sqlite`, and a real `App` in service mode,
both against a stub hub that implements `/api/station/urgent` and `/api/station/events` with the dedupe rules of
§4.3. The stub's restore (a hub back from a replica `lost_s` behind) puts back the hub's **whole state as it was at
`now − lost_s`**: each dedupe entry whole (its state, `stale_src`, `rev` and alarm, so a `seen` that was later
upgraded to `raised` comes back as `seen`), the alarms, the live pages and the press windows. It does not keep a key
by its first sighting. Pushes already made stay made, and `gen` rises.

| Scenario | Asserts |
|---|---|
| **The app down during an emergency** (asked for in review) | capture in service mode, the app stopped. An MDC emergency key-up. The stub hub has the `urgent` item within 1 s of the burst's end. Then the app starts: its take-over event and the finished transmission (with `alarm_key`) arrive, the row and the clip attach, and the hub holds **exactly one alarm and made one push** |
| ... with the hub in a handoff | the hub answers 503 + `Retry-After` for 3 s first: delivered on the first retry after it serves; still one alarm |
| ... with the first answer lost | the hub applied it but the answer never arrived: the retry gets `duplicate`; one alarm |
| ... with the station's wall clock stepped back 40 min and the app's lane first | the app's event looks 40 min old and raises nothing; capture's item, `age_s` about 1 s, is `applied`: exactly one alarm and one push. The same with a `traffic` event that carries the `alarm_key` and `emergency: false` first |
| ... then capture restarted, and the hub back from a replica 30 s behind | the alarm was answered `applied` before the restart. After it, the next answer (the 50 s `hello`'s is enough) carries a new `gen`: the alarm is sent again and the hub has it. The same with no restart and the senders idle for 200 s |
| A P25 live alarm and its call, the station's clock 60 s ahead of the hub's, in both orders of arrival (#775) | one alarm, one push: the call's event carries the live alarm's key |
| The same radio's call 300 s later with the bit still set | nothing new raised on either lane: it joins the open alarm, and the row says "emergency still set". At 601 s by the hub's clock without a flag, the next one is a new press. After an All clear, the next flag raises an alarm at any gap: labelled "emergency still set after All clear" when it comes within 10 min of the radio's last flag before the All clear, else as a new press |
| A P25 radio in emergency for 35 min with the app stopped; the app starts and its calls (ends 35, 28, 21, 14, 7, 1 min ago) reach the hub within seconds | one alarm (the 28 min call's), one push, labelled with its age; the others "still set" on it; the 35 min call raised nothing |
| A flag on a talkgroup whose notices are off, then the same radio on a dispatch talkgroup 5 min later | one alarm, one push |
| A P25 alarm, All clear, then 20 min later (the stub's clock) a new press with the station's wall clock stepped back 15 min | a second alarm and a second push. The same without the All clear |
| The station rebooted with its wall clock 1 h fast and the app stopped; the reloaded alarm is answered `stale` (wall-sourced, 3700 s); then the app starts | its take-over and the finished transmission, 2 min old by their true times, raise one alarm and one push; the late notice stays |
| The hub answered `applied`, and the events lane's `seen` had been upgraded to `raised`; the stub restores to before the upgrade but after the `seen` | the resend is `applied`: one alarm again |
| Two stations, station 1's alarm acknowledged at 4 s, station 2's item at 9 s | one alarm, one push, two sources |
| A page's `ended` revision delivered, then a state post that still lists the page | the page stays ended on the stub |
| A live P25 alarm; the radio's next call ("disregard", still in emergency mode) from +330 to +360; All clear at +380; the call's event at +420 | one alarm, one push; the call attached to the cleared alarm, marked "emergency flag on a call from before the All clear" |
| `urgent-mode write`, the app up, an MDC emergency | the hub has the alarm by the app's lane; `urgent_delivered` arrives; the station's banner is gone within a second of the hub's ack; switched to `send` afterwards, capture sends nothing for it |
| `urgent-mode write`, the app stopped, 40 alarms; then `send` and a fresh alarm | the fresh alarm is in the first request; the backlog follows, oldest first |
| A P25 radio's real press 3 s after an All clear, its post after a station reboot with the clock 40 s ahead | one more alarm and push, labelled "emergency still set after All clear" |
| `send` mode, capture's first send answered 503, the app's take-over answered `seen` (the station's clock stepped back 40 min) | one alarm, one push, from capture's retry; the banner clears only then |
| An analog emergency transmission whose event is the only copy, behind a batch of clips | the event goes in the small alarm post at once; the clip follows in a batch |
| Two stations hear one MDC press 2 s apart | two items, each `applied`; one alarm, one push; the second station is on the alarm as a second source |
| A page of 130 s, the app stopped | every revision reaches the hub, the `ended` one too. With the `ended` one withheld by the stub, the hub's entry ends itself after 300 s |
| ... with the app's lane first | the app is up and fast: its `alarm` event and the finished transmission arrive and the alarm is acknowledged; capture's item, held back 3 s by the stub, then arrives: `duplicate`, still one alarm and one push |
| ... with the uplink down for 29 min, and for 34 min | in both the alarm is **not** given up: it is `new` throughout and the station's page shows "not delivered" from 10 s on. On reconnect, at 29 min the stub answers `applied` and makes one alarm and one push labelled with the real time and "29 min ago"; at 34 min (past 30 min × 1.1) it answers `stale`, records a late notice and pushes nothing. These are the two sides of A7's recommended rule; the test changes with the admin's answer. The same at 31.5 min with the fake boot clock running 4.7 % fast: `applied` |
| ... with the disk full | the item is sent from memory within 50 ms plus the round trip; the frame says `durable: false`; Health is critical; the spool has stopped taking audio at its reserve |
| ... with the wall clock stepped back an hour mid-transmission | one alarm, on time; the transmission is live, not late; no request expired |
| A live page of ours with the app stopped | the hub's live paging state shows each pair as it decodes; the finished page arrives when the app is back and ends the live entry |
| A key-up with an ID; a page with a continuation | rows as in inline mode |
| A transmission read 61 s late | `late`: logged, transcribed, sent; no ATC or intel pre-alert. An emergency in it still attaches to its alarm |
| A spool item whose handler always raises | quarantined after 3 tries, a Health item, the next item handled |
| Capture restarted mid-transmission; both restarted | the open transmission is flushed and spooled before exit; nothing is handled twice |
| A pick refused because another pid holds the claim; a squelch Apply with a stale `expect` | the reply says why; nothing changed |
| The spool at its bound | audio dropped oldest first, counted; the live path unaffected, the urgent lane still sending |

Reversal at every step is the two mode files and a restart of both units; the switch back from `service` follows
§4.7's drain rule. Steps 2, 3, 4 and 7 are refactors with no switch; they are reversible by revert.

## 7. Risks and open questions

### 7.1 Decided in review, and still open

The radio maintainers' decisions (their two comments on #773). The boundary is approved.

| # | Question | Decision | Where |
|---|---|---|---|
| Q1 | Must capture send the emergency lane itself? | **Yes.** Its own fsync'd SQLite file, not the outbox; its own sender to a new idempotent route with the station's token. The app still takes each item over | §4.3, §11 |
| Q2 | A live page as a durable event? | **Yes**, in the same lane, deduped by its page key | §4.3 |
| Q3, Q-B | P25 emergencies | The end-of-call flag stays in `App._call`. The live path is the grant's emergency bit from the trunk-recorder patch: an alarm on a transition only (not set for that radio in the last 10 min), one per press, deduped with `srcList`; a stuck bit never re-alarms. Their follow-up, #775. Until then: at call end | §4.3, §4.6 |
| Q4 | What is `late`? | **Age at the app's read**: under `LIVE_MAX_AGE_S` (60 s) live, older late with #753's rules. Emergencies are never gated | §4.1 |
| Q5, Q-C | Requests in files or on the socket? | **Both, by kind**: the wish file is desired state, the socket carries requests that need an answer, the status file is observed state. `check` is advisory: capture validates on apply and reports a refusal in the status | §4.2, §4.4, §4.5 |
| Q6 | `PageHold` | **The app** | §3.1 |
| Q7 | `reception.py` | **Whole into capture**; the app shows results from the status | §3.3, §4.5 |
| Q9 | Audio format and `fsync` | **s16 WAV; no `fsync` per item**, the spool synced every few seconds and at stop | §4.1 |
| Q12 | Anything relying on the daily restart? | **Yes, bound it**: prune `CallIngest._seen`; the rest is bounded or persisted already. A week-long accelerated soak before `service` | §6 steps 3, 10 |
| Q-D | Keep-warm | **On by default**; since the resend rule needs its answer, the 50 s `hello` always runs in `send` mode. Step 8b measures the first alarm on a warm and a cold connection | §4.3 |
| Q-E | Auto-assigned jobs and rollback | **Yes**: the app mirrors them into `sdr_modes.json` | §4.4 |
| Q-F | Which live pages | **Every page**; the hub filters | §4.3 |
| Q-H | A rotated token | **Handed over without a restart**: of the two ways the review named, the socket request, made by a root reload helper (`systemctl reload sizeup-radio`). A credential change never restarts the radios | §4.3, §7.5 |
| Q-A | The hub's handoff as the urgent lane's worst case | **Accepted for the first version**, as a measured bound: the hub's maintainers measure the real write-refusal window when the route is built (#776); a standby taking urgent items early is a follow-up only if the measurement is bad | §4.3, §6, §11 |
| Q-G | Who may open the route | **An `sz1` credential with the `radio-receive` role**, not `sz1` always | §4.3, §7.5, §10, §11 |
| Q-I | Which window dedupes a live P25 alarm against the end-of-call flag | **System + talkgroup + unit, the live grant inside the finished call's span ±10 s.** A live bit with no matching call within 10 min stands alone; #775's per-unit 10 min transition rule covers a stuck bit | §4.3, §11 |
| Q-J | Capture's children and the credentials | **`child_env()` without `CREDENTIALS_DIRECTORY` or any token variable; the token in memory only, never in a command line.** The same-uid residual risk is accepted and noted | §4.3, §7.5 |
| - | The eight changes after the second read | All confirmed by the radio maintainers at `2e9e8b10` | §4.3, §4.7, §3.3 |
| - | The rules for the sequences that lost or doubled an alarm (6a777c98), the P25 key from the grant's own time, the wall-step rule | All confirmed by the radio maintainers on 2026-11-18, with no disagreement | §4.3 |
| - | The second adversarial review of the models (#783) and the clock finding of #788 | **Confirmed by the radio maintainers on 2026-11-18, all of it, no disagreement** (they asked that the repeated alarm after an All clear read "emergency still set after All clear"). Items 1 to 6. 1: "still set" is armed only by a flag that raised or joined an alarm, and only joins an alarm that exists and is open; any other flag is judged on its own. 2: the press window is timed on the hub's receive time, never on two station wall times; a negative gap is a new press; an All clear ends it; it applies before #775 too. 3: only a boot-sourced `stale` blocks a later event. 4: the next `seq` is always the largest of `SEQ`, the file names and the cursor, plus 1; `SEQ` is fsynced with the periodic sync but not relied on. 5: only a hub-shaped 400/413/422 refusal counts, and is final only for an item already split down to itself; a non-hub body or a 200 that fails the schema is no answer and no try; the 64 KB limit is enforced before sending, an oversize alarm is trimmed, never dropped. 6: the stub hub's restore puts back the whole state as of the restore point. Also decided here: an open alarm for the join rule is one not ended or cleared, acknowledged or not; the resend cap keeps the newest 32 by answer time, then `n`; an emergency item for the spool has four signs; the P25 bit counts as not set when the wall clock outran the boot clock by over 60 s; ended pages stay ended. The clock rules of #788 (a step is over 5 s; the ±10 % rate bound; the hub's 10 % age margin) are agreed by the radio maintainers and the main session | §4.1, §4.3, §6, §11 |
| - | The third adversarial review of the models | **Confirmed by the radio maintainers on 2026-11-18, all of it, no disagreement** (they write `start_boot` in capture's ingest; today's `CLIP_ID` fault on labels starting with `#` or `(` is their own issue, with the same slug + 8-hex rule). 1: every frame and reply stays within the 64 KB frame: `urgent_since` is filled while its encoded frame fits (`more: true`), memory is paged by `memory_after`, and an item over 48 KB (`ITEM_MAX_BYTES`, for frames and hub requests alike) is trimmed, never dropped. 2: a flagged call whose start came before the All clear, by more than the uncertainty of H1m's boot age or of the skew-corrected station time, attaches to the cleared alarm with no push; when unsure it alarms; H1m is required. 3: `urgent_delivered` from the app marks a row `app`: the banner clears, capture never sends it; in every request fresh alarms go first, then resends, then the backlog. Also: one byte count (UTF-8 of the JSON as sent), a lone `too_large` trims before it counts; an events-lane age from the station's wall clock is labelled approximate; ids and keys always valid (a short hash for odd or long labels), checked before sending; the lost-alarm warning rebuilt from `lost_gen`; request ids recorded in `urgent.sqlite` across a restart (`interrupted` when cut mid-apply); `key_time` required on a P25 live alarm | §4, §4.1, §4.2, §4.3, §6, §11 |
| - | The final verification of the models | **Confirmed by the radio maintainers and the main session on 2026-11-18.** The radio maintainers add the boot id and `wall_steps` to today's station state post in a small PR of their own, before the capture split; until a station reports them, the skew path is unavailable and the call alarms with the label. The main session added the `suppressed` outcome and that an outcome is given only after the event is durably written. The skew path places a call only with skew samples of the call's own boot (state posts carry the boot id and `wall_steps`), one at or after its start, no wall step since it started, and a reboot always a step; the boot-age path only when the request and the event share a boot; otherwise the call alarms with the label. `/api/station/events` answers each alarm event's outcome (`raised`, `joined`, `duplicate`, `suppressed`, `stale`, `seen`); `urgent_delivered` only for the first four, and never with an older hub. Also: the trim never cuts identity or matching fields; a trimmed item refused `too_large` again is final; fresh is read from the row (never answered, under 120 s on the boot clock), and an alarm of a batch refused whole is retried alone at the head; the requests table holds only ops with effects, and after a new `pid` the app MUST ask from `memory_after: 0`; `lost_at`, the warning 24 h from it; an `app` row with no `gen` is resent after a restore in `send` mode, warned about in `write` mode; any traffic event with an emergency flag or `alarm_key` goes in the small alarm post | §4.2, §4.3, §6, §11 |
| - | The restart count | About 12 with the dongles is accepted; the sender's client makes it about 14 | §3.3 |
| - | `SegmentWorkers` on both sides | Agreed: per label, shared across receiver instances, drained on stop, in capture | §3.6 |

Agreed assumptions (the review let them stand):

| # | Assumption |
|---|---|
| Q8 | P25 audio in the spool is a hard link to trunk-recorder's WAV; the reader resamples. Whether a per-call hook of trunk-recorder can replace the size-stable wait was not checked |
| Q10 | The tap's reads of the squelch's private state (`_frames`, `_hold`, `_start`, `keyed`) stay inside capture |
| Q11 | Capture's log wording is unchanged and only the unit name changes. Readers that match by unit name are found when the unit is added (step 11), not listed here |

Still open:

| # | Question | For | What this document assumes |
|---|---|---|---|
| A7 | What a late emergency becomes on the hub, now that capture never gives one up (§7.2). Awaiting the admin | the admin | The radio maintainers' proposal, recommended: under 30 min old (`alarms.MAX_AGE_S`) with a 10 % margin for the station's fast boot clock (stale only at `age_s` ≥ 33 min), an alarm and a push labelled with its real time and age; older, a late notice with no push |

### 7.2 For the admin

| # | Question |
|---|---|
| A1 | The station page during an app restart: radios keep recording, and emergency alarms and live pages still reach the hub (§4.3), but the station's own page and "talking now" are away for about 4 s, and transcripts for longer. Today everything is away for about 8 s and the audio is lost. Is that the trade wanted? |
| A2 | One more unit to watch (`sizeup-radio`), one more row in `sizeup-station status`, more Health items (spool growth, a quarantined item, the urgent sender), two one-word switch files. No new secrets file: `sizeup-station secrets` writes `station.env` as today and then tells capture to take the new values; the radios do not restart for a new token. The first switch is by hand on the one station |
| A3 | Disk: the spool adds one sidecar (a few KB) per transmission and no second copy of the audio, if the app adopts the spool's file as `<id>.src.wav` by hard link. At 115 speech-to-text clips an hour [P] and an assumed 6 s each that file is about 0.5 GB a day [E], which is written today too. Under WSL2 it lands in the same virtual disk as now. The spool keeps a free-space reserve (256 MB by default) so its own growth cannot fill the disk the urgent file is on |
| A4 | The other app that shares the dongles sees `app: "sizeup"` claims under capture's pid, which changes about a quarter as often (§3.3). A capture restart still drops every claim for a few seconds, as each station restart does today. Its side needs no change (`sizeup/radio/claims.py`) |
| A5 | WSL2: nothing new is required of it. Unix sockets, `/run`, hard links, `flock` and systemd units are already used by `sizeup-stt-encode` and `sizeup-trunk-recorder`. USB attach order after a Windows restart is as today; capture inherits `SDR_SETTLE_S`. Not verified there: `CLOCK_BOOTTIME` across a sleep of the Windows host |
| A6 | For information, decided with the hub's maintainers (Q-A): an emergency keyed during a hub deploy still waits for the hub's handoff. The split removes the station's gap, not the hub's. How long that window really is will be measured when the route is built (#776); `docs/cloud.md`'s 35 to 60 s is not that measurement |
| **A7** | **What should a late emergency become? Awaiting your decision.** An emergency button that reaches the hub more than 30 min after it was pressed (the uplink was down) raises nothing today: the alarm code logs "too old to raise now". Capture now keeps sending it for as long as it takes, and the station's own page shows it as not delivered meanwhile. **Recommended (the radio maintainers' proposal, the same rule as #753 and `alarms.MAX_AGE_S`):** up to 30 min old (counted with a 10 % margin, so 33 min, because the station's clock can make an alarm look up to 5 % older than it is), still an alarm **and a push**, labelled with its real time and age ("from 12 min ago"); older, a late notice on Health and on the row, with no push. The alternatives: nothing but a log line for the old ones (today), or a distinct push for those too |

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
| A second process holds the hub's credentials and speaks to the hub | one route, one kind of request, the secrets never in its environment and never in a second file, §7.5 |
| The urgent lane and the app's lane both deliver every alarm, in either order | the dedupe store on the hub, written by both lanes and asked first; the idempotency table of §4.3 is the hub's test list |
| An alarm exists only in memory for a few ms | stated in §4.3 ("What can go wrong"); the send does not wait for the disk beyond 50 ms |
| A live P25 emergency path does not exist yet | said in §4.3 and §4.6; #775 |
| An fsync'd commit on the station's disk is slower than assumed | it can no longer hold a send back; measured in step 8b with one process |
| The station's wall clock steps | ages on the boot clock (§4.3 "Clocks"); the stepped-clock tests |
| Capture runs for days where it restarted 12.8 times a day | `CallIngest._seen` pruned; the soak of step 10 |
| One bad item stops a channel's handling for good | the quarantine after 3 tries (§4.1) |
| The closure grows back | the list, the test, the build directory (§3.3) |
| A stale spool after a long outage fires old alerts | `late` by age; the alarm code's own age rule |
| The evidence's restart counts predate #712 | the gain is smaller than 55 to 7 suggests; it is still every hub-only change |

### 7.5 Security

| Item | Entry |
|---|---|
| **The new hub route**, `POST /api/station/urgent` | bearer resolved before the body is read (`Hub._principal`); the payload's `station` must be the token's (`id_mismatch`); the leader only (503 otherwise); a body of at most 64 KB and 32 items, read by `Content-Length`; every string field length-capped before use; the rig derived on the hub, never taken from the station; `age_s` is used (the hub places an item at its own `now - age_s`; one with `age_src: "wall"` cannot be checked and is labelled approximate) but bounded: a value that is not a number, negative, or over 24 h is clamped into [0, 24 h], and a boot-sourced one that differs from `sent_at - event_at` by more than a few seconds is logged (the station's wall clock stepped). `sent_at` only feeds the skew measure. A station that lies about an age can only make its own alarm look fresher or older, and it can raise alarms anyway |
| What the route adds for someone holding the station's token | nothing new: the same token can already raise an alarm with an `alarm` event and set the live page with `/api/station/state`. It adds a second door to the same room, so it gets the same lock: the allow-list |
| The allow-list (`docs/nodes.md` §3, #758) | decided: the route is opened by an `sz1` credential **with the `radio-receive` role**, as `/api/station/events` is; never by `sz1` alone once roles exist, never by `sw1`. A node that loses the role loses the route at its next request, like every role. That table gets the row: `/api/station/urgent` (alarms and live pages ahead of the app's lane). Its own invariant applies: a test lists every `/api/station/*` route and fails when one has no row. `Hub.STATION_ROUTES` gains the route. Until the nodes registry gives stations roles, a station's `sz1` token stands for the role, as on every station route today |
| `contracts/routes.json` | the route is added there with its credential (`sz1`), role, body cap, "not seen" and "idempotent by `station`, `key`, `rev`". The file arrives with #774 |
| Cloudflare Access | no change: the existing `/api/station/*` application and the station's own service token. No new Access token, no wider policy |
| **The urgent sender's credentials** | the station's `sz1` token and its Access service token, from the same store as the app: `/etc/sizeup/station.env`, root 0600, unchanged. systemd hands the file to the unit (`LoadCredential=`, the #301 pattern); capture reads three names from it into memory. Never in its environment, so in no child's environment or command line; never in the wish, the status, the spool, `urgent.sqlite` or a log line (§4.3) |
| What that does not cover (accepted, Q-J) | capture's children are started without `CREDENTIALS_DIRECTORY` or any token variable, and the token is never in a command line; but they run as the same user and could still read the unit's credentials directory if they looked. They are sizeup's own fixed, pinned builds (`rtl_fm`, `rtl_sdr`, the channelizer). No other unit can read it. A residual risk, stated and accepted |
| Rotation | `sizeup-station secrets` writes `station.env`, restarts `sizeup-station` and runs `systemctl reload sizeup-radio`. The reload is a root helper (`ExecReload=+`) that hands the values to capture over the socket; capture takes the `credentials` request only from a peer with uid 0. No receiver is touched, no file's mode is loosened, SIGHUP is ignored. The hub's 10 min Rotate grace covers the gap. A test pins that a reload changes no `rtl_fm` pid, and that the same request from the `sizeup` uid is refused |
| A false listener | the reload helper hands the values only to the unit's main pid, checked on the connected socket; a socket put at `live.sock` by another process of the `sizeup` user gets nothing (§4.3) |
| Where the token can go | one host, read from the root-owned `config.toml` (`[cloud] hub_url`), not from the wish, which the `sizeup` user can write; two paths (`/api/station/urgent`, and `/api/station/hello` for the keep-warm). A test pins that the senders build no other request. What capture executes (`rtl_fm`, `rtl_sdr`, the recorder) also comes from the root-owned config |
| The live socket | 0660 in a 0750 directory, peer uid checked: `sizeup` or root, nobody else. **`role` is self-declared**: any process of the `sizeup` user can connect as `reader` and make requests. That lets it move a dongle to another job or Off, change a squelch, start a reception test or ask for a probe, which can make the station deaf on a channel. It is the trust the app has today, and the same process could do worse by other means (it can signal capture). It cannot obtain a secret (no request returns one), set one (uid 0 only), or run a command (no request takes a path or a command line). Every accepted request is logged with the peer's pid |
| Files | the wish, the status, the spool and `urgent.sqlite` are the `sizeup` user's, not world-readable. `radio.lock` decides who may open the urgent file and write the spool. The spool holds raw radio audio under the clips' own retention; nothing in it leaves the station except by the app's lanes |
| What an urgent item carries | a channel label, radio IDs, a unit alias, tone names: what the `alarm` event and the live page carry today, over the same TLS and Access path. No audio, no transcript |
| Logs | capture's lines reach the hub through the app's shipper and its scrubber, which already holds the same secrets. `last_error` is a status and the hub's `reason` |
| Replay | a request sent twice is a `duplicate`; an alarm item the hub judges past its cutoff (30 min × 1.1) is `stale` and raises no alarm |

## 8. What a native radio daemon would implement

Only the contract of §4. It would not need Python, `config.toml` or the CAD.

| It must | From |
|---|---|
| Read the wish file; keep the last good one | §4.4 |
| Write the status file every 0.5 s | §4.5 |
| Publish spool items: audio, then the sidecar by rename; keep `SEQ` and `EPOCH`; sync; prune behind the cursor; hold the bounds | §4.1 |
| Serve the live socket with #709's framing: frames out, requests in, replies | §4.2 |
| Hold `radio.lock`; insert `urgent` rows, fsync'd; hand each item to the sender in memory | §4.3, §4.7 |
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

What the native session writes from this document (ADR 0004); its first version is #783. The schemas are
`contracts/schemas/capture-<name>.schema.json` and their vectors `contracts/vectors/capture-<name>/`, where #774's
harness finds them; what a schema cannot say (`spool.md`, `socket.md`, `urgent.md`, `urgent.sql`, the mode table)
is in `contracts/capture/`. The file names below are the `<name>`s. Every schema allows additional properties
(unknown keys are ignored), and every list of words (a `kind`, a frame's `t`, an `op`, a state, a `reason`) is open:
the fields of each known word are required when that word is there, and a word a reader does not know validates and
is skipped or refused as §4 says, never an error. "Required" is what a reader may rely on. Types and units are in
the section named. Where this document and #783's "Choices" table differ, the sentence here was changed to agree,
except the points listed under the table.

| Artifact | File | Fields from | Required |
|---|---|---|---|
| Spool sidecar, `kind: "segment"` | `spool-sidecar.schema.json` (by `kind`) | §4.1 | `v`, `kind`, `id`, `seq`, `spool_epoch`, `channel.label`, `channel.kind`, `start`, `end`, `closed_boot`, `boot`, `published`, `reason`, `levels`, `ids`, `alarm_key`, `audio` (object or null); with `audio`: `file`, `format`, `encoding`, `rate_hz`, `channels`, `samples`, `bytes` |
| Spool sidecar, `kind: "call"` | the same file | §4.1, §4.6 | the common ones (`v`, `kind`, `id`, `seq`, `spool_epoch`, `published`), `start`, `end`, `closed_boot`, `boot`, `audio`, and `system`, `talkgroup`, `turns`, `emergency`, `encrypted`, `tr`. Inside: an `ids` entry needs `id`, `system`, `emergency`, `from`; `keyup` needs `split`, `parent`, `prev`; a turn needs `src`, `pos_s`, `emergency`, `via` |
| Spool sidecar, `kind: "metrics"` | the same file | §4.1 | `v`, `kind`, `id`, `seq`, `spool_epoch`, `published`, `records` |
| Spool cursor | `spool-cursor.schema.json` | §4.1 | `v`, `spool_epoch`, `seq` |
| Spool layout and behaviour | `spool.md` with vectors: the names, audio before sidecar, `EPOCH`, `SEQ` recovery, who deletes, the sync, the reserve, the quarantine | §4.1 | - |
| Urgent file | `urgent.sql` (the DDL of §4.3, byte for byte) | §4.3 | - |
| Urgent item (a row's `data`, and an item of the request) | `urgent-item.schema.json` (by `kind`) | §4.3 | the item, all required, none nullable: `key`, `rev`, `kind`, `at`, `event_at`, `age_s` (number, at least 0), `age_src` (`boot` \| `wall`), `data`. `data` of an alarm: `source`, `start`, `radio_id`, `emergency`, `emergency_via`; with `emergency_via: "P25"` also `system`, `talkgroup`, `key_time`. `data` of a page: `channel`, `started`, `pairs`, `phase`, `ended`. Optional in either: `trimmed` (bool, default false); a trimmed item still has every required field |
| `POST /api/station/urgent`, request | `urgent-request.schema.json` | §4.3 | `v`, `station`, `items` (1 to 32) |
| `POST /api/station/urgent`, answers | `urgent-response.schema.json` | §4.3 | 200: `ok`, `gen`, `results` (each `key`, `rev`, `status`); 4xx: `error`, `reason` |
| Socket, capture to client | `socket-event.schema.json` (by `t`) | §4.2 | `v`, `t`, `at`, and per `t` the fields of its row |
| Socket, client to capture | `socket-request.schema.json` (by `t` and `op`) | §4.2 | notices: `v`, `t` and each one's own fields (`urgent_taken`: `urgent_epoch` and `n`, both; `urgent_delivered`: `key` and `rev`); requests: `v`, `t: "request"`, `id`, `op`, `args`, `expires_boot` |
| Socket, replies | `socket-reply.schema.json` | §4.2 | `v`, `t: "reply"`, `id`, `ok`; with `ok: false`: `reason` (a word), `error` (text), `confirm` (bool). `urgent_since`'s `result`: `urgent_epoch`, `rows`, `memory`, `memory_last`, `more`; no count limit on `memory`, the frame's 65,536 bytes bound it |
| Socket framing | `socket.md` with byte vectors: `>II`, the header and payload limits | §4.2, `sizeup/cloud/sttencode.py` | - |
| Wish file | `wish.schema.json` | §4.4 | `v`, `seq`, `mode`, `paths`, `channels`, `dongles` |
| Status file | `status.schema.json` | §4.5 | `v`, `at`, `at_boot`, `boot`, `pid`, `contract`, `wish_seq`, `channels`, `sdrs`, `refusals`, `spool`, `urgent`; a channel row's `label`, `kind`, `run`, `state`; in `spool` and `urgent` the counters a Health row reads |

Where this document differs from #783 as first written, so that #783 follows:

| # | #783's choice | This document |
|---|---|---|
| C24 / X6 | `urgent_taken` may carry `urgent_epoch` | it must: `urgent_epoch` and `n` are both required (§4.2) |
| C26 / X7 | one string, `error`, holding either text or a word | two fields: `reason` (the word) and `error` (the text) (§4.2) |
| X10 | a turn's `via` knows four words | three, the code's: `srclist`, `grant`, `split`. Nothing produces `freqlist` (§4.6) |
| X16 / C31 | `urgent.state` has seven words, `reset` among them | six, with a stated precedence; a reset is `resets` and `reset_at` beside it (§4.5) |
| `urgent.sql` | the DDL byte for byte | changed since: `at`'s comment (X3); `at_boot`; `sent_boot`, `sent_boot_id`; `hub_gen` as text; the resend keys in `meta`; `last_set_boot` and `boot` in `p25_bit` |
| Sidecar | - | a call's `alarm_key`; `audio_lost` beside `audio_dropped` |
| Urgent scenarios, `Hub`, `Lane`, `P25Bits` | the models of #783 | the three key states and which lane's age wins; the resend rule anchored at the last sighting of the old `gen`, surviving a restart, `gen` by value; the P25 match made on the station and carried as `alarm_key`; "still set" on the hub; the bit's transition on the boot clock; two stations, one alarm; a page's clock per revision and `ended`'s 10 min; 5 tries for an unknown word; the edges |
| After the second review and #788 (confirmed) | `Hub`: "still set" from any flag, by the calls' own times; any `stale` blocks; `restore` keeps a key by its first sighting; the join rule only for unacknowledged alarms; a state post can make an ended page live. `Lane`: a lone 400/413/422 is final; a 200 that fails the schema or names no item counts as a try. `Spool`: `SEQ` is trusted when it is a number. `P25Bits`: the boot clock alone. `CLOCK_STEP_S` 2 s; the hub's cutoff without a margin | the press window `{alarm, last}` on the hub's receive time, armed only by a flag that raised or joined, joining only an open alarm, ended by an All clear, live P25 items skipping the join step; `stale_src`; `restore` to the whole state at `now − lost_s`; open = not ended or cleared; ended is final; only a hub-shaped refusal, final only alone and already alone; not-the-hub's-shape is no answer and no try; the 64 KB check before sending and `data.trimmed`; the resend cap by answer time then `n`; the next `seq` from the largest of `SEQ`, the names and the cursor, and `spool.seq_behind`; an emergency item's four signs; the frozen-clock rule (over 60 s); a step over 5 s; every cutoff on an emergency × 1.1 |
| After the third review (confirmed) | `urgent_since` filled to `limit` whatever its size; `memory` all on the first page, at most 256; an oversize item framed as is; rows in write mode `new` for good; requests in `n` order; ids from labels that fail `CLIP_ID`; request ids forgotten at a restart (`socket.md`: "Either is safe"); `Hub`: a call from before the All clear alarms again; `key_time` optional | `ITEM_MAX_BYTES` 49,152 and one byte count (UTF-8 of the JSON as sent) for frames and requests; `urgent_since` filled while the frame fits, `memory_after`, `memory_last`, `more`; `urgent_delivered` and the row state `app` (`urgent.sql`); fresh, resends, backlog in every request; a lone `too_large` trims first; ids and keys mended with a hash, keys at most 200, the local check before sending; `lost_gen` (`urgent.sql`) and the warning rebuilt at start; the `requests` table (`urgent.sql`) and `interrupted`, with `socket.md` saying why a repeat is safe; `Hub`: the call-before-All-clear rule with `U`, the approximate label for a wall-sourced events age, `gen` in the events answer; a call's `start_boot` in the sidecar; `key_time` required on a P25 live alarm |
| After the final verification (confirmed) | `Hub`: the skew path with any recent skew sample, whatever its boot, and no sample needed after the call's start; the boot-age path across a reboot; the events answer with no outcome; `urgent_delivered` on any 200. `Lane`: trim cuts every string to 200 (`source` included); a trimmed item refused again goes round; "fresh" from process memory (never in a request); an alarm of a refused batch behind the backlog; an `app` row with no `gen` never resent. `socket.md`: every request recorded, reads included; `memory_after` after a new pid only a SHOULD. `urgent.sql`: `lost_gen` without `lost_at` | the skew path's four conditions (same boot, a sample at or after the start, no `wall_steps` change since, a reboot is a step) and `boot` and `wall_steps` on the state post (`station-state` schema); the boot-age path only on one boot; `outcomes: [{id, outcome}]` in the events answer, and `urgent_delivered` only for `raised`, `joined`, `duplicate`; the trim's untouchable fields and its order of cuts; a trimmed `too_large` final; fresh from the row (`hub` and `sent_at` null, `at_boot` under 120 s); alone items at the head; `app` rows in doubt resent in `send` mode, a warning in `write` mode; the `requests` table for ops with effects only; MUST ask from `memory_after: 0` on a new pid; `lost_at` in `urgent.sql`; every traffic event with an emergency flag or `alarm_key` in the small alarm post |

Outside `contracts/capture/`:

| Where | Change |
|---|---|
| `contracts/routes.json` | add `POST /api/station/urgent`: credential `sz1`, role `radio-receive`, body cap 64 KB, 32 items, not "seen", idempotent by (`station`, `key`, `rev`), 503 on a non-leader. It is listed as designed and not served until the hub has it. The file arrives with #774 (open when this was written) |
| `docs/nodes.md` §3, the route allow-list | a row for the route: `sz1` + role `radio-receive` (decided); `sw1` never. Its "every `/api/station/*` route has a row" test then covers it |

Vectors to start from (all invented):

```json
{"v": 1, "kind": "segment", "id": "CHA-1900000000123", "seq": 12345, "spool_epoch": "3f9a1c0b7d2e4a61",
 "channel": {"label": "CH A", "kind": "traffic", "mhz": 450.0125, "mod": "nfm"}, "sdr": "sdr-a",
 "start": 1900000000.123, "end": 1900000006.473, "published": 1900000006.52,
 "closed_boot": 5120.44, "published_boot": 5120.49, "boot": "b1946ac9-2f0e-4c1d-9a77-0d1e2f3a4b5c", "replayed": false,
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
{"v": 1, "station": "st-north", "urgent_epoch": "9c2d7e10a4b35f68", "sent_at": 1900000000.41,
 "items": [{"key": "CHA-live-1900000000123", "rev": 0, "kind": "alarm", "at": 1900000000.39,
            "event_at": 1900000000.123, "age_s": 0.29, "age_src": "boot",
            "data": {"source": "CH A", "start": 1900000000.123, "radio_id": "7001",
                     "ids": [{"id": "7001", "system": "mdc", "emergency": true, "from": "live"}],
                     "unit": "UNIT A", "emergency": true, "emergency_via": "MDC"}}]}
```

The alarm connection's request, above; a page goes in a request of its own, on the other connection:

```json
{"v": 1, "station": "st-north", "urgent_epoch": "9c2d7e10a4b35f68", "sent_at": 1900000104.91,
 "items": [{"key": "page:PAGE 1:1900000100.25", "rev": 2, "kind": "page", "at": 1900000104.9,
            "event_at": 1900000100.25, "age_s": 4.66, "age_src": "boot",
            "data": {"channel": "PAGE 1", "started": 1900000100.25, "phase": "voice", "ours": true, "ended": false,
                     "until": null,
                     "pairs": [{"a_hz": 600.9, "b_hz": 716.1, "names": ["Example Co 1"], "ours": true,
                                "at": 1900000100.25}]}}]}
```

```json
{"ok": true, "v": 1, "station": "st-north", "gen": 412,
 "results": [{"key": "CHA-live-1900000000123", "rev": 0, "status": "applied"}]}
```

```json
{"v": 1, "t": "request", "id": "r-5f3a9c12", "op": "pick", "by": "person", "at": 1900000200.0,
 "expires_boot": 5330.0, "args": {"sdr": "sdr-b", "job": "CH A", "confirm": false}}
```

```json
{"v": 1, "t": "reply", "id": "r-5f3a9c12", "ok": false, "reason": "parked", "error": "refused: parked", "confirm": false,
 "at": 1900000200.02}
```

## 11. Work for the hub's maintainers

Separate from the radio work, and needed before step 8b: the hub's route lands first, or in one PR with capture's
senders. The route test is fail-closed both ways: a client string for a route that `contracts/routes.json` does not
list as served fails, and a route listed as served that the hub does not serve fails. Until a hub advertises `urgent`,
capture's senders idle and the app's lane carries everything, as today.

| # | Piece | What |
|---|---|---|
| H1a | The route | `POST /api/station/urgent` in `sizeup/cloud/hub.py` (`Hub.handle`, `STATION_ROUTES`): `_principal`, the leader check, `_claim` on `station`, the caps of §4.3, the answer shapes of §4.3 with `gen`. Not `_seen()`; its own `urgent_seen` time. `GET /api/station/hello` also answers with `gen`: the idle sender's only way to learn of a restore. `/api/station/events` answers with `gen` too, so the app's `urgent_delivered` can carry it (agreed by the main session: a small additive field that old stations ignore; on #776's list). **Beside `gen`, it answers each alarm-carrying event's outcome**: `outcomes: [{id, outcome}]`, the outcome one of `raised` (it raised an alarm), `joined` (it joined or attached to one: same radio, still set, a call from before the All clear), `duplicate` (the key's alarm was raised already), `stale` (too old: a late notice, no alarm), `suppressed` (received and recorded, but the admin's settings say no alarm for it, e.g. notices off on that channel: final), `seen` (nothing raised for a reason that can change: its age gate, a clock, no emergency). The app sends `urgent_delivered` only for `raised`, `joined`, `duplicate` or `suppressed`: resending against a deliberate setting would only loop. An app whose hub gives no outcome (an older hub) never sends it. **An outcome is given only after the event is durably written**, as for the ack; the `gen` restore rule covers a hub that dies after answering (the main session's review) |
| H1b | The dedupe store | a table in `hub.sqlite`, made at its first write (never at start: `tests/test_startup_writes.py`): `(station, key)` -> `kind`, highest `rev`, first seen, the alarm or page it became, and for an alarm key its **state**: `raised`, `suppressed`, `stale` or `seen` (§4.3). **Written by both lanes**: the urgent route writes `raised`, `suppressed` or `stale` (it answers `applied` for a `suppressed` item: received and recorded, no alarm by the admin's setting); `/api/station/events` writes `raised` when it raises or updates an alarm for an `alarm` event's `id` or an `alarm_key`, `suppressed` when the admin's settings say no alarm for it (notices off), and `seen` when it meets one and raises nothing for a reason that can change (its age gate, `emergency: false`). `raised` over `suppressed` over `stale` over `seen`; a `stale` entry keeps `stale_src` (`boot` or `wall`, the item's `age_src`). An urgent item is `duplicate` only for `raised`, `suppressed` and `stale`; the events lane raises nothing for a key that is `stale` with `stale_src: boot`, and judges an event on its own when it is `stale` with `stale_src: wall`. Asked before anything is raised, on both lanes, and written **after** each lane's own decision, never before its age gate. Kept at least 24 h. It is replicated with the rest, so it survives a handoff |
| H1c | Alarms | an alarm item goes through `_apply_alarm`'s path with `id` = `key`. `alarms._on_traffic` moves an alarm's `traffic_id` from `key` to the finished transmission's id; after that only the dedupe store knows `key`, which is why H1b is written by both lanes. The item's time is the hub's `now - age_s` |
| H1d | P25 live against end of call (#775), and "still set" | one emergency per press, by `alarm_key`: capture matches the call to its live alarm on the station and the call's event carries the key, so the existing take-over applies in either order of arrival. The hub never compares its own placing of the live item with the call's span. The fallback for an event with no `alarm_key`: system + talkgroup + unit, the live item's `data.start` inside the call's span ±10 s, station time against station time. **Still set (revised), per (system, radio), a press window `{alarm, last}`** with `last` on the hub's clock, the moment the hub received the flag. A flag tied to an alarm by the existing rules updates it and sets the window to `{that alarm, t}`. Else, when the window's alarm exists and is open (not ended or cleared, acknowledged or not) and `0 ≤ t − last < STUCK_WITHIN_S` (600 s), the flag joins it: no alarm, no push, the row marked "emergency still set", `last = t`; a live P25 item skips this step. Else the flag is judged on its own (age gate with its margin, notices, the same-radio join, a new alarm); the window is set to `{alarm, t}` only if it raised or joined one, and left alone if it raised nothing. A negative gap is a new press. An All clear ends the window. **A flagged call whose start came before the All clear**, placed on the hub's clock by H1m's boot age or by the measured `hub.skew` and at least `U` before the clear (§4.3's table: `U` = 10 s + 10 % of the age, only when the request and the event are on one boot; or 5 s + 2 × the skew's spread from the call's start to now, only with skew samples of the call's own boot that bracket its start with no wall step since, a reboot counting as one), attaches to the cleared alarm with no push, marked "emergency flag on a call from before the All clear"; when unsure it alarms as "emergency still set after All clear". Never decided from two station wall times. **It applies from H1 on, before #775 too**, to end-of-call flags on the events lane and to live P25 items: it can only join an open alarm. The transitions-per-hour counter on Health |
| H1e | Live pages | a page item sets the station's live paging state by `key` and `rev`, with `until` as its expiry; `/api/station/state`'s `paging` merges by `key`, newer wins; a `page` event carrying `live_key` ends the live entry. Ended is final per `(station, key)`: no later state post or revision makes it live again. Capture sends every page, ours or not (decided): the hub filters. Pair fields arrive as `a_hz`, `b_hz` (the state post's are `a`, `b`). What a live page of ours triggers on the hub (a push, a pre-alert row) is the hub's to decide: today it is display only |
| H1f | The capability | `urgent` in the state answer's `caps`, behind a hub setting, so the app can tell capture (the wish's `hub.caps`) and the lane can be turned off from the hub's side |
| H1g | The allow-list | decided: `sz1` with the `radio-receive` role. The row in `docs/nodes.md` §3 and in `contracts/routes.json` (§10); the route-table test; a test that a station without the role gets 403, and that the shared station token does not open the route. Until the nodes registry exists a station's own `sz1` token stands for the role |
| H1h | Health | the station's `urgent` block from its status (the senders' state, the oldest unsent alarm, items not durable, `rejected`); the hub's own count of items by status; an alert when the sender is `refused` or an alarm arrived more than a few seconds after it was pressed |
| H1i | A late emergency (A7, awaiting the admin) | the recommended rule: an alarm item is stale only when its `age_s` reaches `MAX_AGE_S` × 1.1 (30 min, so 1980 s): the 10 % margin covers a station boot clock that over-counts by up to 5 % (#788). Under that it is raised and pushed, labelled with its real time and age, answer `applied`; at or over it, a late notice on Health and on the row with no push, answer `stale`, in place of today's log line. The same margin on every age cutoff for an emergency, the events lane's gate in `alarms._on_traffic` included. A push's age on the events lane is H1m's boot age; where there is none (an older station, a wall step), it is the hub's `now` minus the station's `end`, and the push labels it approximate, as for a wall-sourced urgent age. Built so the admin's answer can change it in one place |
| H1j | Two stations, one press | the dedupe store is keyed by station from the start. Out of scope for the first version beyond this rule, which holds from the start so that the hub never pushes twice: an alarm item whose radio id matches an alarm already open (not ended or cleared, acknowledged or not) on the same channel label (P25: system and talkgroup), raised within `REPEAT_S` from **any** station, both placed on the hub's clock, is `applied` and joins that alarm: key recorded `raised` under it, the station noted as a second source, no second alarm, no second push. The alarm row's `traffic_id` must be station-qualified before a second station is taken: two stations can produce the same `<label>-live-<ms>`. Merging the audio and the rows of the two copies is `docs/nodes.md` §2.4's work |
| H1k | Tests | each row of §4.3's idempotency table, **the app's lane first, the transmission took the alarm over, it was acknowledged, then the urgent item: `duplicate`** among them; a request during a handoff applies nothing; a hub that comes back from an older replica answers with a new `gen`; 32 items, and the 33rd refused; an oversized body; another station's token; a `sw1` token (403); a page `rev` out of order; an item with a wild `sent_at`; the stub hub the station's tests use is generated from the same rules, its restore putting back the whole state as of the restore point (§6); a restore that keeps `seen` and loses the `raised` upgrade and the alarm: the resend is `applied`; the press window: a P25 radio's calls 35, 28, 21, 14, 7, 1 min old in one burst (one alarm, the 28 min one), a flag with notices off then a dispatch flag 5 min later (one alarm), a new press 20 min after an All clear with the station's clock 15 min behind (an alarm), a negative gap (a new press); a wall-sourced `stale` then a fresh event (one alarm); a boot-sourced age of 1979 s (`applied`) and 1980 s (`stale`); two stations with the first alarm acknowledged (one push); an ended page and a later state post (still ended); a hub 422 refusal of one item answered with the hub's body; the three states of a key: the events lane first with an old wall time, then the item (`applied`); a `traffic` event with `alarm_key` and `emergency: false`, then the item (`applied`); the item `stale` on a boot-sourced age, then an event that looks fresh (nothing raised); `gen` in `hello`'s answer; the same radio's flagged call at 300 s (nothing, "still set") and after 601 s (a new press); a P25 pair with the station 60 s off, both orders (one alarm); two stations' items for one press (one alarm, one push); a page with no `ended` (ends at 300 s); every limit at its edge (exactly `MAX_AGE_S` × 1.1 is `stale`); a call from before the All clear (started 50 s before, arriving 40 s after, with a boot age: attached, no push), the same with no boot age and a stale skew (an alarm, "still set after All clear"), a call started inside `U` of the clear (an alarm), a real new press 20 s after the clear (an alarm); an events-lane push with no boot age (labelled approximate); `gen` in the events answer; the skew path refused for a post after a reboot (new boot, clock 40 s ahead, a press 3 s after the clear: an alarm), with a `wall_steps` change since the call started (an alarm), with no sample at or after the call's start (an alarm), and for a station that reports no `wall_steps` (an alarm); each outcome of the events answer: `raised`, `joined` (same radio, still set, before the All clear), `duplicate`, `stale`, `seen` |
| H1m | The events lane's boot-clock ages (**required** in the first version) | needed for `urgent-mode write`, where the app's lane is the only one, and for the All clear rule of H1d. The take-over `alarm` event carries the item's `event_boot` and `boot`; a call's event carries its `start_boot` (§4.6); the forwarder adds the request's own boot-clock time at sending. **The alarm travels in a small post of its own**: the take-over `alarm` event, or any `traffic` event, analog or a P25 call, that carries an emergency flag or an `alarm_key`, alone and without clips (its clip follows in a normal batch; so even when it is the only copy of the alarm it never waits behind clips), with a **total** deadline of 8 s enforced by the client: one monotonic budget across connect, send and every receive, after which the post is aborted and treated as not delivered (sent again as usual). `http.client`'s socket timeout is per operation, not a total, so it cannot be this deadline. Normal batches keep today's timeouts (events 60 s, the empty-batch probe 30 s, speech-to-text input 60 s, state 10 s, job results 30 s): a batch with clips can take well over 8 s on the station's uplink, and a short timeout there would cause retries and duplicates and stall the lane. The 8 s is on the station's monotonic clock, which runs up to 5 % fast (#788): at least about 7.6 s real, which is fine for an upper bound (the main session's review). The hub then has a boot-clock age for that event (the two on one boot) in place of the station's wall time against its own clock: for the age gate, for the push's age, and for placing a call's start against an All clear. Without it a station clock stepped back by more than 30 min makes the hub drop a real alarm that arrives only by the events lane, as it does today |
| H1l | The handoff (#776) | decided for the first version: the handoff window is accepted as the lane's bound, **and measured**. When the route is built, measure the write-refusal window: over several real deploys, the time from the first 503 a station's write gets to the first 200, as the station sees it. The figure goes into §4.3 and `docs/cloud.md`, and it is an exit criterion of step 8b (§6). The handoff test stays (a request during a handoff applies nothing; the first retry after it is the first delivery). A standby taking urgent items early is a follow-up only if the figure is bad |
