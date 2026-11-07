# CLAUDE.md

Read this before you work on sizeup. Many agents work on this repo in parallel, and the repo is **public**.

## What sizeup is

A fire dispatch CAD and situational display for Port Chester FD. It hears the radio (two-tone paging, the county P25
system, fireground channels, HPN ATC), transcribes it and keeps the CAD up to date with the call. One codebase and one
entry point (`python -m sizeup --config ...`). `[cloud] role` picks what it runs as:

- **Hub** (`role = "hub"`): the CAD of record, on AWS ECS Fargate (one arm64 task). People reach it only through
  Cloudflare Tunnel and Cloudflare Access. It has no radios. Dispatch voice is Amazon Polly, and the main channels are
  transcribed with Amazon Transcribe. SQLite is replicated to S3 by Litestream. Deploys are **hot/cold**: the new task
  starts as a standby that follows the replica, then takes an S3 leader lock (`sizeup/leaderlock.py`) once the old task
  drains and releases it. There is never more than one writer. A merge to main runs tests, then deploys
  (`.github/workflows/deploy.yml`).
- **Station** (`role = "station"`): the PC with the RTL-SDR dongles at a firehouse, on Windows with WSL2 + Ubuntu
  24.04 (`deploy/station/install-windows.ps1` -> `install-linux.sh`, run by systemd). It runs rtl_fm, trunk-recorder,
  MDC-1200 decoding and whisper.cpp. Its own page (localhost:8091) shows only the station: it has no CAD.
  **Auto-update:** the station runs exactly the git sha that the hub reports (`sizeup/cloud/autoupdate.py`). It builds
  that sha beside the running release, preflights it, switches during a quiet moment and switches back if
  `/healthz` doesn't come up.
- **How they talk:** the connection is always outbound HTTPS from the station, with a bearer token plus a Cloudflare
  Access service token. Events (pages, transmissions, re-runs, alarms) go through a durable outbox
  (`POST /api/station/events`), live state goes by `POST /api/station/state` and the hub sends jobs back through a long
  poll (`GET /api/station/jobs`). The hub answers with its build, and the station follows that build.
- With no `[cloud]` section it runs all-in-one (local dev).

Details: `docs/cloud.md` (architecture, flows, handoff, health), `deploy/station/README.md`, `docs/ROADMAP.md`,
`docs/cad-plan.md`. Work is tracked in GitHub issues, labelled `now` / `next` / `later` / `waiting`.

## Where things are

| path | what |
|---|---|
| `sizeup/app.py` | the App: receivers -> STT -> state; the cloud seams (`_emit`, `_transcribed`, `make_app`) |
| `sizeup/config.py` | config loading; `config.example.toml`, `deploy/station/config.station.toml`, `deploy/hub/config.hub.toml` |
| `sizeup/radio/` | SDRs (`sdr.py`, `sdrserial.py`), dongle claims (`claims.py`), paging/tones, P25, MDC/FleetSync, AM airband, trunk-recorder and ADS-B services |
| `sizeup/cad/` | the CAD: store, API, dispatcher, run cards, alarms, settings, activity log, the airport |
| `sizeup/cloud/` | `station.py` (link, outbox), `hub.py`, `autoupdate.py`, `configupdate.py`, `health.py`, `access.py`, `stationmode.py` |
| `sizeup/handoff.py`, `leaderlock.py`, `fence.py` | the hub's hot/cold deploy supervisor, its S3 lock, the app's side of fencing |
| `sizeup/geo/`, `gis.py`, `roads.py` | gazetteer and geocoding, county GIS layers, highway layers |
| `sizeup/aircraft.py`, `atc*.py`, `trains.py`, `weather.py` | map layers and ATC transcription |
| `sizeup/web/server.py`, `sizeup/web/static/` | HTTP server and the UI (plain JS/CSS; design system in `style.css`) |
| `deploy/station/`, `deploy/hub/`, `deploy/aws/` | station installer and units, hub entrypoint/Litestream/connector, ECS task definition |
| `tests/`, `tests/fixtures/` | pytest; `conftest.py` fakes the dongles, the claims dir and the Keychain |
| `tools/` | data fetchers, `deploy_check.py`, `ui_consistency.py` (the browser half of `tests/test_design.py`) |

`autoupdate.py`, `configupdate.py`, `radio/adsbd.py` and `radio/trunkrecd.py` run under the station's **system**
python3 (3.11+). They must use the standard library only (`tests/test_system_python.py`).

## Running tests

```sh
caffeinate -i perl -e 'alarm 1500; exec @ARGV' .venv/bin/python -m pytest -q
```

- `caffeinate -i` stops the suite from stalling when the Mac sleeps. The `perl alarm` is the timeout, because there is
  no `timeout` command on the Mac.
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
