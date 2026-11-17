# CLAUDE.md

Read this before you work on sizeup. Many agents work on this repo in parallel, and the repo is **public**.

## What sizeup is

A self-dispatch CAD and radio monitor for Port Chester FD. It hears the radio (two-tone paging, the county P25 system,
fireground channels, HPN ATC, and a second P25 system, MTA MRRS, as context), transcribes it and keeps the CAD up to
date with the call. Incidents come from ranked sources: external CAD feeds first, paging as the backup. Pre-alerts
(the phone, the airport, radio intel) never make an incident. One codebase and one entry point
(`python -m sizeup --config ...`). `[cloud] role` picks what it runs as:

- **Hub** (`role = "hub"`): the CAD of record, on AWS ECS Fargate (one arm64 task). People reach it only through
  Cloudflare Tunnel and Cloudflare Access. It has no radios. Dispatch voice is Amazon Polly, and the main channels are
  transcribed with Amazon Transcribe. Three SQLite databases (`cad.sqlite`, `hub.sqlite`, `audio.sqlite`) are
  replicated to S3 by Litestream; audio, attachments and site documents are S3 objects. Deploys are **hot/cold**: the
  new task starts as a standby that follows the replica, then takes an S3 leader lock (`sizeup/leaderlock.py`) once
  the old task drains and releases it. There is never more than one writer: a task that lost the lock refuses every
  write (`sizeup/fence.py`). Litestream confirms each promoted file before the app migrates it. The replicas are
  checked all the time (`sizeup/replicawatch.py`). A merge to main runs tests, then deploys
  (`.github/workflows/deploy.yml`: the SSM parameter check, the image, the ECS roll, the replica check, the station
  check).
- **Station** (`role = "station"`): the PC with the RTL-SDR dongles at a firehouse, on Windows with WSL2 + Ubuntu
  24.04 (`deploy/station/install-windows.ps1` -> `install-linux.sh`, run by systemd). It runs rtl_fm, trunk-recorder,
  MDC-1200 / FleetSync decoding, whisper.cpp, readsb (ADS-B), optionally AIS-catcher, and the private incident-source
  adapters (`sizeup/sources/`; the hub never loads one). Its own page (localhost:8091) shows only the station: it has
  no CAD.
  **Auto-update:** the station runs exactly the git sha that the hub reports (`sizeup/cloud/autoupdate.py`). It builds
  that sha beside the running release, preflights it, switches during a quiet moment and switches back if
  `/healthz` doesn't come up.
- **How they talk:** the connection is always outbound HTTPS from the station, with a bearer token plus a Cloudflare
  Access service token. Events (pages, transmissions, re-runs, alarms, incident-source reports) go through a durable
  outbox (`POST /api/station/events`), live state goes by `POST /api/station/state` and the hub sends jobs back through
  a long poll (`GET /api/station/jobs`). Aircraft, vessels and the station's scrubbed logs have their own posts
  (`/api/station/aircraft`, `/vessels`, `/logs`). The hub answers with its build, and the station follows that build.
- With no `[cloud]` section it runs all-in-one (local dev).

Details: `docs/architecture.md` (the system analysis and module map), `docs/development.md` (running it locally and
the tests), `docs/cloud.md` (flows, handoff, health, every layer), `deploy/station/README.md`, `docs/ROADMAP.md`,
`docs/cad-plan.md`. Work is tracked in GitHub issues, labelled `now` / `next` / `later` / `waiting`.

## Where things are

| path | what |
|---|---|
| `sizeup/app.py` | the App: receivers -> STT -> state; the cloud seams (`_emit`, `_transcribed`, `make_app`); `main()` and the gated hub start |
| `sizeup/config.py` | config loading (unknown keys refused); `config.example.toml`, `deploy/station/config.station.toml`, `deploy/hub/config.hub.toml` |
| `sizeup/radio/` | SDRs (`sdr.py`, `sdrserial.py`), dongle claims (`claims.py`), FM and squelch (`fm.py`, `tap.py`), paging/tones, P25 (`p25.py`, `recorder.py`, `trunkrecd.py`), more P25 systems (`systems.py`), MDC/FleetSync, AM airband (`am.py`, `channelize.py`), ADS-B and AIS services (`adsbd.py`, `aisd.py`), the reception test (`reception.py`, `probes.py`) |
| `sizeup/stt.py`, `transcribe.py` (also `Display`: vocabulary phrases without AWS's hyphens), `stt_vocab.py`, `sttcatchup.py`, `channels.py` | speech-to-text: the whisper queue, Amazon Transcribe (budgets, watchdog, breaker), vocabularies, shed clips caught up, per-channel engine choice |
| `sizeup/talkgroups.py`, `tiers.py`, `sourcekeys.py` | talkgroup switches, tiers of other P25 systems, one key per radio source and who may see context rows |
| `sizeup/cad/` | the CAD: `store.py` (SQLite, `tx()`), `service.py`, `api.py`, dispatcher, run cards, alarms, settings, activity log, the airport |
| `sizeup/cad/sources.py`, `pagematch.py`, `automode.py`, `sizeup/sources/` | incident sources ranked and matched (a page scored against the feed's incident: `pagematch.py`), dispatch modes, the station's adapter interface and runner |
| `sizeup/cad/comms.py`, `radiolog.py`, `statushints.py` | the radio log (rows, links, change log, views), the per-incident radio log, status suggestions |
| `sizeup/cad/history.py`, `premise.py`, `preplan.py`, `preplan_docs.py`, `sizeup/attachments.py` | address history and search, premise notes, pre-plan points, site documents, incident attachments |
| `sizeup/cad/routes.py`, `mapview.py`, `sizeup/roadgraph.py` | rig positions and probable routes on the road graph |
| `sizeup/intel/` | radio intelligence: rule packs (`data/intel/`), engine, events, place (the locator: intersections, blocks, exits), push policy and the scrubbed push, `listen.py` (presigned Listen links), `infer.py` (the LLM stage on Bedrock: off by default, capped, backs the rules, never pushes alone) |
| `sizeup/cloud/` | `station.py` (link), `outbox.py`, `hub.py`, `hubstore.py`, `sttinput.py` (each clip's exact speech-to-text input, station -> hub, pre-alert channels first; `flac.py` its codec; `sttarrival.py` its arrival delay on the hub), `stations.py` (each station's token, who a request is), `autoupdate.py`, `configupdate.py`, `health.py`, `access.py`, `stationmode.py`, `adapters.py`, `adapterssm.py`, `adapterpass.py` (the adapter's password written from the hub, never read), the station's logs (`logread.py`, `logship.py`, `cwlogs.py`), `aircraftlink.py`, `vessellink.py` |
| `sizeup/handoff.py`, `leaderlock.py`, `fence.py`, `replicawatch.py` | the hub's hot/cold deploy supervisor, its S3 lock, the app's side of fencing, the replica checks and failure pushes |
| `sizeup/audiostore.py`, `docstore.py`, `docfiles.py`, `pdfcheck.py`, `cachesync.py` | S3: clip audio, site documents and their checks, the map caches' copies |
| `sizeup/notify.py`, `prealert.py`, `errwatch.py` | ntfy pushes (alerts, status, intel), the phone pre-alert (read only; its topics are in Settings, not the repo), the protected-topic set (derived, fails closed; a static backstop of hashed names under it), the hub's error spikes |
| `sizeup/dispatch.py`, `polly.py`, `tts.py`, `tts_server.py` | page parsing; the dispatch voice (Amazon Polly, Kokoro) |
| `sizeup/geo/`, `gis.py`, `roads.py`, `roadprojects.py`, `dvr.py` | gazetteer and geocoding, county GIS layers, highway layers and cameras, road closures, the camera DVR |
| `sizeup/aircraft.py`, `atc*.py`, `airport_watch.py`, `trains.py`, `traintime.py`, `railassets.py`, `vessels.py`, `weather.py` | map layers, ATC transcription and airport watches |
| `sizeup/perf.py`, `logscrub.py`, `procenv.py`, `aws.py` | per-thread CPU accounting and the profiler, log scrubbing, the station's process environment, AWS signing (no boto3) |
| `sizeup/web/server.py`, `sizeup/web/static/` | HTTP server and the UI (plain JS/CSS; design system in `style.css`); `cad.html` (Dispatch, Overview, Radio, Search, Settings; `/monitor`) and `station.html` |
| `deploy/station/`, `deploy/trunk-recorder/`, `deploy/hub/`, `deploy/aws/` | station installer and units, trunk-recorder configs (the county's and `systems/<name>/`), hub entrypoint/Litestream/connector, ECS task definition |
| `data/` | the gazetteer, CAD lists, county departments / apparatus / tones, ATC rules, intel packs, STT profiles |
| `tests/`, `tests/fixtures/` | pytest; `conftest.py` fakes the dongles, the claims dir and the Keychain; `replay.py` and `test_replay_*.py` use private captures |
| `tools/` | data fetchers, `deploy_check.py`, `taskdef_params.py`, `reseed_replica.py`, `ltx.py`, `stt_cost.py`, `stt_diag.py`, `ui_consistency.py` (the browser half of `tests/test_design.py`), `dev_seed.py` (invented calls for a local instance) |

`autoupdate.py`, `configupdate.py`, `logread.py`, `radio/adsbd.py`, `radio/aisd.py` and `radio/trunkrecd.py` run under
the station's **system** python3 (3.11+). They must use the standard library only (`tests/test_system_python.py`).

## Running tests

```sh
caffeinate -i perl -e 'alarm 1800; exec @ARGV' .venv/bin/python -m pytest -n auto -q
```

- `caffeinate -i` stops the suite from stalling when the Mac sleeps. The `perl alarm` is the timeout, because there is
  no `timeout` command on the Mac. `-n auto` (pytest-xdist): a serial run on macOS fails two tests late on (#457).
- Setup, running the app locally (all-in-one, hub, station; `tools/dev_seed.py` for invented data), the optional
  test tools (Playwright, Litestream, node), the lint and the known flakes: [`docs/development.md`](docs/development.md).
- **Worktrees have no `.venv`.** Use the main checkout's venv from inside the worktree, e.g.
  `../../../.venv/bin/python -m pytest -q` from `.claude/worktrees/<name>`. First check that it imports **your**
  code: `../../../.venv/bin/python -c 'import sizeup; print(sizeup.__file__)'` must print the worktree's path.
- CI (`.github/workflows/test.yml`) runs pytest on Python 3.14 and builds the hub image, then checks `/healthz`.
- Changes to `deploy/station/**`, `deploy/trunk-recorder/**`, `autoupdate.py`, `configupdate.py`, `adsbd.py` or
  `trunkrecd.py` also run **station-install** (`.github/workflows/station-install.yml`). It runs a real install on
  Ubuntu 24.04, shellcheck and a PowerShell parse check. Then it runs the auto-update end to end
  (`.github/scripts/station-update.sh`): a side-by-side switch, a switch back from a release that doesn't come up, a
  preflight failure and a manual rollback. It is slow; wait for it before you call station work done.

## Definition of done

- [ ] **The full suite passes**, not just the tests you touched. Never call a failure "flaky" unless you have proved
      it (e.g. it fails on main too, or fails intermittently with the cause identified). If it really is flaky, file an
      issue for it.
- [ ] **Regression tests use realistic data**: real captured or recorded shapes from `tests/fixtures/` or `tools/record_*`.
      Never test only with data you invented to match your own assumption. Once the replay fixtures exist (#283), add a
      replay test built from real captures.
- [ ] **Any UI change ships with a screenshot in the PR.**
- [ ] **UI copy is concise and data-first:** one line per fact, no "how it works" sentences. Explanations go in
      tooltips, and source and freshness go in a small footer.
- [ ] **Station changes keep switching back to the previous release safe.** A station's `/etc/sizeup/config.toml`
      is never overwritten, and an older release refuses config keys it doesn't know, so a failed switch back would
      leave the station down. To add or change a station config key, add a one-time config update in
      `sizeup/cloud/configupdate.py` with its deferral rule: apply it only when the release being left
      (`--leaving`) can read the key. Otherwise it reports "deferred" and waits for the next update.
      Add tests like `tests/test_tones_47.py` / `test_station_south_end.py`.
- [ ] **Never open an RTL-SDR dongle held by another app.** sizeup and shortfinal share the dongles through claim
      files in `~/.local/state/rtl-claims/<serial>.json` (`sizeup/radio/claims.py`,
      `docs/handoff-shortfinal-dongles.md`). Never delete another app's live claim, and never touch real dongles or
      the real claims dir from tests (conftest already isolates them).

## Public repo rules

- No real callsigns, tail numbers, incident addresses, transcripts, caller data or ntfy topic names in code, tests,
  fixtures, commit messages, issues or PRs. Use made-up or anonymised values that keep the real shape.
- Credentials live only in AWS SSM (the hub) or the macOS Keychain / `/etc/sizeup/station.env` (the station).
  Never put them in the repo, logs, the UI or test output. Show only "set (Keychain)" / "missing".
- No AWS account details beyond what the repo already shows, and no details of the county's CAD application.
- **No attribution lines**: no `Co-Authored-By`, no "Generated with ..." in commits, PRs or comments.

## Git

- Work in a worktree on a branch:
  `git worktree add -b <branch> .claude/worktrees/<branch> origin/main` (fetch first).
- Open a PR whose body starts with `Fixes #N.`. **Never merge, never force-push, never push to main.**
- If SSH hangs, push over HTTPS with the gh credential helper:
  `git -c credential.helper='!/opt/homebrew/bin/gh auth git-credential' push https://github.com/actualsecurity/sizeup.git <branch>`
  (fetch the same way).
- Changes under `.github/workflows/` are refused over HTTPS (the gh token has no `workflow` scope). Push them over SSH,
  or say so in the PR and leave the push to a person.

## Working in parallel

- Keep your edits to your own area. Don't touch unrelated files, and don't do drive-by refactors.
- Don't reformat files or reorder imports. Keep diffs minimal so other branches still merge.
- To resolve conflicts, merge `origin/main` into your branch. Don't rebase, because rebasing needs a force-push.
- Shared docs (`docs/cloud.md`, `deploy/station/README.md`): add to your own section. Don't rewrite other sections.
