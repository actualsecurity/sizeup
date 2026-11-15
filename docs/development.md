# Developing sizeup

Running sizeup on your own machine and running its tests, with only a clone of this repo: no radios, no station PC, no
AWS account, no Cloudflare Access, no production data. Anything that needs project access is in
[Maintainers only](#maintainers-only) at the end.

Contents: [what you get without access](#what-you-get-without-access) · [prerequisites](#prerequisites) ·
[setup](#setup) · [run it](#run-it) · [tests](#tests) · [known failures and flakes](#known-failures-and-flakes) ·
[making a change](#making-a-change) · [maintainers only](#maintainers-only)

## What you get without access

| missing | what happens locally |
|---|---|
| Radios (RTL-SDR dongles), rtl_fm, trunk-recorder | Nothing is received. The UI, the CAD and every API work. Tests fake the dongles (`tests/conftest.py`) and feed recorded or synthetic audio. |
| whisper.cpp (`whisper-server`) | Speech-to-text shows "down". Tests use `FakeWhisper` (`tests/test_app.py`). |
| AWS (Transcribe, Polly, S3, SSM, Bedrock) | Off unless configured: `[stt] aws` is off, no buckets are set, Polly is only the hub config's voice. Tests use local fakes (`tests/fake_transcribe.py`, `FakeS3`, `FakeSsm`, ...). `python -m tests.fake_transcribe 9431 9432` runs a fake Transcribe for a dev instance (`[stt] aws_endpoint` / `aws_api_endpoint`). |
| Cloudflare Access | All-in-one and a hub without `access_team` / `access_aud` don't check identity: `/api/cad/whoami` says `source: "none"`, `enforced: false`, and roles are not enforced: every page and Settings section is open (site documents treat you as a dispatcher). The shipped hub config has Access on, so every page answers 401 without a real Access token. |
| The private incident-source adapter | Not loaded (station only, and only when installed). Tests use `tests/fake_feed.py`. |
| ntfy topics | No pushes (no topic set). Tests use a local fake ntfy (`FakeNtfy` in `tests/test_alarms.py`); conftest keeps every push local. |
| Production data | Start empty, then fill it with invented calls: [tools/dev_seed.py](#a-populated-instance). |
| Private replay captures (`tests/test_replay_*.py`, `tests/test_intel_replay.py`) | Those tests skip and say why. |

The map layers (tiles, county GIS, trains, aircraft, weather, highway cameras) are public feeds, fetched over the
internet when the app runs; 511NY's layers wait for credentials and stay off. Tests never fetch them: conftest pins
every provider to local fixtures.

## Prerequisites

| | |
|---|---|
| Python | 3.14 to match CI and the hub image (`Dockerfile`: `python:3.14-slim`). The package needs 3.11+ (`pyproject.toml`). |
| Station Python | The station's venv is made from the distro's `python3`: 3.12 on Ubuntu 24.04, 3.11 on Debian 12 / Raspberry Pi OS. Code in `sizeup/` runs there too, so don't use anything newer than 3.11 (syntax or library). `sizeup/radio/adsbd.py`, `aisd.py`, `trunkrecd.py`, `sizeup/cloud/autoupdate.py`, `configupdate.py`, `logread.py` and `tools/stt_diag.py` run under the bare system `python3`: standard library only (`tests/test_system_python.py` checks both). |
| OS | macOS or Linux. Windows only through WSL2 (that is how the station runs). |
| Node.js (optional) | Any current version. About 30 test files evaluate the UI's JavaScript with `node`; without it those tests skip. CI has it. |
| Litestream 0.5.18+ (optional) | The real-replication tests (`tests/test_litestream_takeover.py` and others) need `litestream` on `PATH`, else they skip. macOS: `brew install benbjohnson/litestream/litestream`. CI downloads 0.5.18 from GitHub releases, sha256-checked (`.github/workflows/test.yml`), the same pin as the hub image. |
| Playwright + Chromium (optional) | For the browser tests; [below](#browser-tests). |
| No system packages | The suite needs no C libraries or radio tools: numpy, pypdf, PDFium and Pillow come as wheels. |

On a Mac with Homebrew: `brew install python@3.14 node benbjohnson/litestream/litestream`.

## Setup

```sh
git clone https://github.com/actualsecurity/sizeup.git && cd sizeup
python3.14 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

`[dev]` is what CI installs: pytest, pytest-xdist, and the site-document libraries (pypdf, pypdfium2, Pillow). Install
`-e .` alone and 19 tests in `tests/test_preplan_docs.py` fail. The `intel-llm` extra (the Bedrock client) is only
for the hub image; nothing in the suite needs it. CI does nothing else besides Litestream.

In a git worktree (`.claude/worktrees/<name>`) either make a venv there the same way or use the main checkout's, and
check it imports the worktree's code: `../../../.venv/bin/python -c 'import sizeup; print(sizeup.__file__)'`.

## Run it

One entry point, `python -m sizeup --config <file>`. The role comes from the config's `[cloud]` section.

| mode | config | port | what it is |
|---|---|---|---|
| all-in-one | `config.toml` from `config.example.toml`, no `[cloud]` | 8090 | CAD, UI and radios in one process. The main local mode. |
| hub | `deploy/hub/config.hub.toml` (Access on) or a local copy with it off | 8090 | The CAD of record, fed by a station. No radios. |
| station | `config.toml` with `[cloud] role = "station"` | 8091 (set it) | Radios and the link to a hub. Its page shows the station only; `/cad` redirects to the hub. |

Other ports: 8093 Kokoro voice server, 8179 whisper-server, 8089 the hub supervisor (production only).
`/healthz` answers in every mode; `SIZEUP_PORT` and `SIZEUP_HOST` override `[web]`.

### All-in-one

```sh
sed 's/^auto_assign = true/auto_assign = false/' config.example.toml > config.toml
.venv/bin/python -m sizeup --config config.toml       # http://127.0.0.1:8090/cad
```

- Keep `config.toml` in the checkout root: its relative paths (`data/`, `deploy/trunk-recorder/`) are resolved from
  the config file's folder. It is gitignored.
- The databases go to `state/` (gitignored); `SIZEUP_STATE_DIR=<dir>` puts them elsewhere. Delete the folder to start
  over.
- Starts in about 3 s. Without radios it logs that whisper and the voice are down, and that 511NY waits for
  credentials. All expected.
- `auto_assign = false`: see [shared machines](#radios-on-a-shared-machine).

### A populated instance

```sh
.venv/bin/python tools/dev_seed.py                    # with the app running on :8090
```

Three invented calls (a structure fire with three units on it and a note, an alarm closed as false, a pending MVA) on
invented streets, through the same HTTP API the pages use. Run it again for more. Against a local hub with the
station token it also posts FG1 transmissions with transcripts, as a station would, and they land on their calls'
radio logs:

```sh
.venv/bin/python tools/dev_seed.py --base http://127.0.0.1:8090 --token "$SIZEUP_STATION_TOKEN"
```

It refuses any host but this machine.

### Hub and station, both local

The shipped hub config turns Cloudflare Access on (every page 401 without a real Access token; CI's image job checks
exactly that). For a hub you can use, make a local copy without Access, Transcribe or Polly (`deploy/hub/config.toml`
is gitignored and resolves the same relative paths):

```sh
sed -e '/^access_team/d' -e '/^access_aud/d' -e 's/^aws = true/aws = false/' \
    -e 's/^tts_engine = "polly"/tts_engine = "none"/' deploy/hub/config.hub.toml > deploy/hub/config.toml
export SIZEUP_STATION_TOKEN=$(openssl rand -hex 32)
SIZEUP_STATE_DIR=/tmp/sizeup-hub SIZEUP_CACHE_S3=off .venv/bin/python -m sizeup --config deploy/hub/config.toml
```

A station feeding it, in a second shell with the same `SIZEUP_STATION_TOKEN`:

```sh
cp config.toml config.station.toml       # the all-in-one config above
printf '\n[cloud]\nrole = "station"\nhub_url = "http://127.0.0.1:8090"\n' >> config.station.toml
SIZEUP_STATE_DIR=/tmp/sizeup-station SIZEUP_PORT=8091 .venv/bin/python -m sizeup --config config.station.toml
```

The station logs `hub link: connected`, and the hub's Overview shows it. With no radios it sends no events; use
`tools/dev_seed.py --token` for traffic. Without `SIZEUP_STATION_TOKEN` a station on a Mac looks in the Keychain
(`sizeup-station-token`) and otherwise waits. A station never updates itself here: the auto-update needs the
installer's `/opt/sizeup` layout and `SIZEUP_UPDATE_DIR`.

### Radios on a shared machine

sizeup and shortfinal share RTL-SDR dongles through claim files in `~/.local/state/rtl-claims/<serial>.json`
(`sizeup/radio/claims.py`, [`handoff-shortfinal-dongles.md`](handoff-shortfinal-dongles.md)). On a machine with
dongles plugged in:

- Run with `[radio] auto_assign = false` (the command above): every dongle starts Off and sizeup opens none until you
  pick a mode for it in Settings > Radios. With `true` it takes every free dongle by itself.
- Don't list serials in `[radio] sdrs` or set `shared_device` unless the dongle is yours to use.
- Never open a dongle another app holds, and never delete another app's claim file. To hand one over, switch it Off
  in the app that holds it.
- Tests never touch real dongles or the real claims directory (conftest replaces both).

## Tests

```sh
.venv/bin/python -m pytest -n auto -q                                    # everything
.venv/bin/python -m pytest -q tests/test_cad.py                          # one file
.venv/bin/python -m pytest -q tests/test_cad.py::test_name               # one test
.venv/bin/python -m pytest -q -k "dispatch and not voice"                # by name
.venv/bin/python -m pytest -n auto -q -rs                                # and say why each skip skipped
```

- About 5,900 tests. With `-n auto` the suite takes about 3 minutes on a recent Mac, browser tests included (longer
  on a busy machine). On a clone, 13 to 45 tests skip, each saying why (`-rs`): private replay data, and whichever of
  Litestream, node and Playwright you haven't installed. CI runs `python -m pytest -q -p no:cacheprovider -n auto --dist loadfile` on
  Ubuntu, Python 3.14.
- Run it in parallel: serially on macOS, two tests fail late in the run with `filedescriptor out of range in
  select()` (#457).
- The only custom marker is `replay` (private captures; [below](#replay-tests)). Everything else is plain pytest.

**On a Mac, keep it awake and bounded.** A sleeping Mac stalls the run. `caffeinate -i` stops that, and `perl alarm`
is the timeout (macOS has no `timeout`):

```sh
caffeinate -i perl -e 'alarm 1800; exec @ARGV' .venv/bin/python -m pytest -n auto -q
```

On Linux: `timeout 1800 .venv/bin/python -m pytest -n auto -q`.

### Browser tests

`tests/test_settings_no_redraw.py` and `tests/test_intel_settings_layout.py` drive the real page in headless
Chromium. Without Playwright they skip (CI doesn't install it). To run them:

```sh
.venv/bin/pip install playwright && .venv/bin/playwright install chromium
.venv/bin/python -m pytest -q tests/test_settings_no_redraw.py tests/test_intel_settings_layout.py   # ~75 s
```

Both can serve another commit's page, to show a test fails on the code before a fix:
`SIZEUP_SETTINGS_REF=<commit>` (settings.js, settings.css) and `SIZEUP_INTEL_REF=<commit>` (intel.js, settings.js,
settings.css, style.css). Each file's docstring has the exact command.

`tools/ui_consistency.py` measures the rendered UI on every view against a running instance (fill it with
`tools/dev_seed.py` first): `python tools/ui_consistency.py --base http://127.0.0.1:8090`.

### Litestream tests

With `litestream` on `PATH` (0.5.18+), the takeover, reseed, replica-watch and startup tests run Litestream for real
against local folders standing in for S3. Nothing reaches AWS. Without it they skip.

### Node tests

The UI's JavaScript (`sizeup/web/static/*.js`) is tested by evaluating functions in `node` with a tiny DOM. Most of
these tests look for `node` on `PATH`, then in `/opt/homebrew/bin` and `/usr/local/bin`; a few only on `PATH`. They
skip without it. One focus-order test in `tests/test_new_call.py` also wants `jsdom` where node can `require` it, and
`tests/test_tts.py` wants the Kokoro voice set up (`tools/setup_tts.sh`, ~1.3 GB); both skip otherwise.

### Replay tests

`@pytest.mark.replay(...)` tests replay private captures from a cache outside the repo (`~/.cache/sizeup-replay` or
`$SIZEUP_REPLAY_DIR`). Without project access the cache is empty and they skip, which is the expected state on a
clone and in CI.

### Lint and the station install

CI's only lint is in the `station-install` workflow, which runs when `deploy/station/**`, `deploy/trunk-recorder/**`,
the station's update code or its workflow change:

```sh
.venv/bin/pip install shellcheck-py      # or: brew install shellcheck
.venv/bin/shellcheck -x deploy/station/install-linux.sh deploy/station/sizeup-station deploy/station/health-check.sh \
  deploy/station/start-station deploy/station/start-whisper deploy/trunk-recorder/unit-event.sh \
  .github/scripts/station-update.sh
```

Its PowerShell parse check needs `pwsh`. The install job itself (`install-linux.sh --ci`, then the services started
with no dongles and the auto-update end to end) needs a clean Ubuntu 24.04 with systemd and root: it creates the
`sizeup` user and writes `/opt/sizeup`, `/etc/sizeup` and systemd units. Use a throwaway VM
(`sudo deploy/station/install-linux.sh --ci --from-dir "$PWD"`) or let CI run it. It can't run on a Mac.

There is no Python linter or formatter in CI. Don't reformat files or reorder imports.

## Known failures and flakes

| what | cause | what to do |
|---|---|---|
| 19 failures in `tests/test_preplan_docs.py` | the venv lacks pypdf / Pillow | `pip install -e '.[dev]'` |
| 2 failures, `filedescriptor out of range in select()` (`test_squelch_stuck`, `test_vessels`) | a serial run on macOS passes 1024 open descriptors (#457) | run with `-n auto` |
| Timing asserts under load: `test_cloud::test_a_page_going_out_reaches_the_hub_pair_by_pair_and_ends` (#513), `test_transcribe` idle stream (#449), `test_roads` probe timeout (#468), `test_unit_follow` frame loop (#480), `test_perf` ratio (#481), `test_reception` / `test_fg2` (#500), reception history race (#503) | a slow or busy machine | rerun the file on its own. If it passes alone and on main, it is the known flake: name the issue in your PR. A failure not on this list is yours until shown otherwise; a new flake gets its own issue. |

## Making a change

- **Where things are:** the module map in [`architecture.md`](architecture.md#module-map) and the table in
  [`CLAUDE.md`](../CLAUDE.md#where-things-are). Module docstrings are long and current: read the one you change.
- **Tests:** `tests/test_<area>.py`, plain pytest functions. Build a real `Cad` over a `tmp_path` database
  (`tests/test_cad.py` `make_cad`), a real hub (`tests/test_cloud.py` `HubRun`) or a real web server, and fake only
  the edges (dongles, whisper, AWS, ntfy, network fetches). Use the shapes in `tests/fixtures/`; every name, address,
  topic and transcript in a test is invented.
- **Run what you touched, then everything:** `pytest -q tests/test_<area>.py`, then the full suite. A UI change also
  runs the node tests for that script (`grep -l <function> tests/`) and, for Settings, the browser tests.

### Before opening a PR

- [ ] The full suite passes (`-n auto`), not just the files you touched.
- [ ] **Startup writes nothing.** `tests/test_startup_writes.py` starts a hub on databases already at this build's
      schema and allows at most 16 WAL frames per file (`STEADY_FRAMES`). A migration that rewrites rows, rebuilds an
      index or recreates a table on every start fails it. On the hub every start is a takeover: rewrites slow it down,
      cost an S3 copy of the table each deploy and, done before Litestream replicates the file, were the cause of the
      2026-11 outage ([`cloud.md`](cloud.md) "Replicate before migrate"). A migration runs once and leaves nothing for
      the next start.
- [ ] A UI change: a screenshot in the PR (from your seeded local instance) and concise, data-first copy.
- [ ] A station change: `station-install` passes; new station config keys follow the `configupdate.py` rule in
      [`CLAUDE.md`](../CLAUDE.md#definition-of-done).
- [ ] **Public repo hygiene.** No real callsigns, tail numbers, addresses of incidents, transcripts, caller data,
      people's names or emails, ntfy topic names, credentials, account IDs or bucket names in code, tests, fixtures,
      screenshots, commits, issues or PRs. Nothing about the county's CAD application or the private adapter. Use
      invented values in the real shape. Grep your diff for any real string you looked at while working.
- [ ] No attribution lines in commits or PRs. Never force-push or push to main; merge `origin/main` to resolve
      conflicts. The rest of the rules: [`CLAUDE.md`](../CLAUDE.md).

## Maintainers only

Needs project access (AWS credentials, the station, the private replay bucket). Skip this section otherwise.

**Replay captures.** `tools/replay_sync.py pull` downloads them, read-only, into the cache outside the repo; then the
replay tests run. Details: [`../README.md`](../README.md#replay-fixtures) and `tests/replay.py`.

**Real data.** A restored production copy holds personal data: operators' emails and sign-in IPs, callers'
details, addresses, police and fire transcripts. Restore it only to debug or measure something the invented data
can't show, and:

- keep it outside the checkout (e.g. `~/sizeup-restore`, mode 700), never under the repo or `/tmp` on a shared
  machine;
- never commit it, paste from it into issues, PRs or chats, or use it in tests, fixtures or screenshots;
- delete it, with any `-wal` / `-shm` beside it, when done.

```sh
mkdir -m 700 ~/sizeup-restore
export LITESTREAM_BUCKET=<your-bucket> LITESTREAM_PATH=<path> LITESTREAM_REGION=<region>
export SIZEUP_STATE_DIR=/data SIZEUP_LITESTREAM_SYNC_S=30 LITESTREAM_SOCKET=/tmp/sizeup-ls.sock   # the config's ${...}
for db in cad hub audio; do
  litestream restore -config deploy/hub/litestream.yml -o ~/sizeup-restore/$db.sqlite /data/$db.sqlite
done
```

Read-only on S3. To run the hub on that copy, use the local hub config above with `[prealert] enabled = false` and no
`AWS_*`, `SIZEUP_*_BUCKET` or `SIZEUP_*_TOPIC` in the environment. The copy's own Settings may name real push topics,
and Settings win over the environment, so clear them in the copy first:
`sqlite3 ~/sizeup-restore/cad.sqlite "DELETE FROM settings_overrides WHERE section = 'notify'"`.

**Replay and evaluation tools.** `tools/intel_replay.py` (the radio-intel rules over a restored `cad.sqlite`) and
`tools/stt_eval.py` (speech-to-text against hand-corrected transcripts) need only the venv and a restored copy. They
open it read-only, refuse a live database (a file in `state/`, `/data`, `/var/lib/sizeup` or `$SIZEUP_STATE_DIR`),
refuse a `--json` path inside the repo, and print aggregates only:

```sh
.venv/bin/python tools/intel_replay.py --db ~/sizeup-restore/cad.sqlite --since 2026-11-01 --json ~/sizeup-restore/intel.json
.venv/bin/python tools/stt_eval.py --db ~/sizeup-restore/cad.sqlite --json ~/sizeup-restore/stt.json
```

**Deploys, the station, the replicas:** [`cloud.md`](cloud.md) and [`../deploy/station/README.md`](../deploy/station/README.md).
