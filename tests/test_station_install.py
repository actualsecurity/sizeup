"""The Linux / Windows station install (deploy/station/): its config template, a station with no
voice (tts_engine "none"), the /healthz the health timer polls, and static checks on the scripts
(no secret is ever echoed, PowerShell files stay ASCII for Windows PowerShell 5.1)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import urllib.request
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from sizeup.app import make_app, make_server
from sizeup.cad import dispatcher
from sizeup.cad.service import Cad, Settings
from sizeup.cloud.station import StationLink
from sizeup.config import ConfigError, load
from sizeup.tts import TtsService, VoiceCfg

ROOT = Path(__file__).resolve().parent.parent
STATION = ROOT / "deploy" / "station"
TEMPLATE = STATION / "config.station.toml"
SECRETS = ("SIZEUP_STATION_TOKEN", "SIZEUP_ACCESS_CLIENT_ID", "SIZEUP_ACCESS_CLIENT_SECRET")


def station_config(tmp_path, monkeypatch, text: str | None = None):
    """The template as installed (/etc/sizeup/config.toml, SIZEUP_STATE_DIR=/var/lib/sizeup), with
    trunk-recorder's talkgroups.csv this checkout's and the state directory at tmp_path."""
    text = (text if text is not None else TEMPLATE.read_text()).replace(
        "/var/lib/sizeup/trunk-recorder/talkgroups.csv", f"{ROOT}/deploy/trunk-recorder/talkgroups.csv")
    etc = tmp_path / "etc"
    etc.mkdir(exist_ok=True)
    (etc / "config.toml").write_text(text)
    monkeypatch.setenv("SIZEUP_STATE_DIR", str(tmp_path / "state"))
    return load(etc / "config.toml")


# -- the template ---------------------------------------------------------------------------------------

def test_station_template_is_a_linux_station(tmp_path, monkeypatch):
    c = station_config(tmp_path, monkeypatch)
    st = tmp_path / "state"
    assert (c.cloud_role, c.cloud_hub_url) == ("station", "https://sizeup.actualsecurity.org")
    assert (c.host, c.port) == ("127.0.0.1", 8091)
    assert c.rtl_fm == "/opt/sizeup/rtl-sdr/bin/rtl_fm" and c.sdr_auto and c.start_mode == "South 2"
    assert [(ch.label, ch.mhz, ch.kind) for ch in c.channels] == [("South 2", 470.2, "paging"), ("FG1", 453.0375, "traffic")]
    assert (c.stt_server, c.stt_model, c.stt_port) == ("/opt/sizeup/whisper/bin/whisper-server",
                                                       "/opt/sizeup/models/whisper.bin", 8179)
    assert c.p25_dir == "/var/lib/sizeup/captures/p25" and c.p25_recorder_dir == "/var/lib/sizeup/trunk-recorder"
    assert c.p25_recorder.split()[0] == "/opt/sizeup/trunk-recorder/bin/trunk-recorder"
    assert c.p25_talkgroups[307] and c.p25_incident_talkgroups == frozenset({307, 333})
    # storage under SIZEUP_STATE_DIR, not next to the config in /etc
    assert c.cad_db == str((st / "state" / "cad.sqlite").resolve())
    assert c.clips_dir == str((st / "captures" / "clips").resolve())
    assert c.cloud_outbox == str((st / "state" / "outbox.sqlite").resolve())
    assert c.cloud_state == str((st / "state" / "cloud.json").resolve())
    # no voice here (the hub's Polly), no phone pre-alert (the hub's), and no secret in the file
    assert c.dispatch_tts_engine == "none" and not c.prealert_enabled
    assert (c.cloud_token, c.cloud_access_id, c.cloud_access_secret) == ("", "", "")
    assert c.unit_departments and c.tones          # data/ found through the package (PYTHONPATH=the release)


def test_station_template_has_no_mac_paths_or_secrets():
    text = TEMPLATE.read_text()
    for bad in ("/Users/", "homebrew", "~/", ".local/src", "Keychain", "token =", "access_client"):
        assert bad not in text, bad


def test_station_template_channels_match_the_hub():
    """The hub lists the station's channels for their labels and kinds: keep the two the same."""
    def chans(path):
        import tomllib
        return [(c["label"], c["mhz"], c["kind"]) for c in tomllib.loads(path.read_text())["radio"]["channels"]]
    assert chans(TEMPLATE) == chans(ROOT / "deploy" / "hub" / "config.hub.toml")


def test_station_template_makes_a_forwarding_station_without_a_voice(tmp_path, monkeypatch):
    c = replace(station_config(tmp_path, monkeypatch), sdr_auto=False)   # no dongle lookups in a test
    app = make_app(c)
    assert app.station is not None and app.station.forward and app.link is not None
    assert app.tts is None
    voice = next(r for r in app.radio_status() if r["label"] == "Voice")
    assert voice["state"] == "off" and "hub" in voice["detail"]
    assert app.cad.state()["audio"]["tts"] is False


def test_the_health_timer_gets_healthz_from_the_station(tmp_path, monkeypatch):
    c = replace(station_config(tmp_path, monkeypatch), sdr_auto=False, port=0)
    app = make_app(c)
    server = make_server(app)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.httpd.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as r:
            body = json.loads(r.read())
        assert r.status == 200 and body["ok"] is True and body["role"] == "station"
    finally:
        server.shutdown()
    # what the timer runs polls exactly that, on the config's [web] port
    hc = (STATION / "health-check.sh").read_text()
    assert "/healthz" in hc and "systemctl restart" in hc and '"web"' in hc and "LIMIT=3" in hc


# -- tts_engine = "none" ----------------------------------------------------------------------------------

def test_tts_engine_none_config(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[dispatch]\ntts_engine = "none"\n')
    c = load(p)
    assert c.dispatch_tts_engine == "none"
    p.write_text('[dispatch]\ntts_engine = "nothing"\n')
    with pytest.raises(ConfigError, match='"none"'):
        load(p)


def test_tts_engine_none_renders_nothing(tmp_path):
    v = VoiceCfg("none", "Samantha")
    assert v.label() == "Voice: none"
    with pytest.raises(dispatcher.AudioUnavailable, match="no voice"):
        dispatcher.render_dispatch("Engine 60, respond.", [[349.1, 1.0]], tmp_path, v)
    assert not list(tmp_path.glob("*.wav"))
    cad = Cad(Settings(tts_engine="none"), log=lambda m: None)
    p = cad.voice_preview()
    assert p["audio_url"] is None and "no voice" in p["audio_note"]
    assert cad.state()["audio"]["tts"] is False and cad.state()["audio"]["label"] == "Voice: none"
    row = TtsService.none_status()
    assert row["label"] == "Voice" and row["state"] == "off"


def test_station_without_a_voice_refuses_the_hubs_tts_job():
    link = SimpleNamespace(cfg=SimpleNamespace(dispatch_tts_engine="none"))
    assert StationLink._tts(link, {"text": "Engine 60, respond.", "voice": {"engine": "kokoro"}}) == {
        "ok": False, "error": "station has no voice"}


def test_hub_goes_text_only_when_the_station_has_no_voice(tmp_path):
    from tests.test_cloud import HubRun, call, hub_cfg, station, station_cfg, uid, wait
    hub = HubRun(hub_cfg(tmp_path, cloud_offline_s=5.0))
    st = station(station_cfg(tmp_path, hub.url, dispatch_tts_engine="none"), {})
    try:
        c = hub.app.cad
        inc = c.create({"type": "STRU", "address": "209 Westchester Ave", "operator": "Lt. Ruiz"})
        assert wait(lambda: hub.app.hub.connected(), 5)
        code, p = call(hub.url + f"/api/cad/incident/{inc['id']}/preview", "POST",
                       {"unit_ids": [uid(c, "E63")]}, token=None)
        assert code == 200 and p["audio_url"] is None
        assert "station has no voice" in p["audio_note"] and "text only" in p["audio_note"]
    finally:
        st.link.stop()
        hub.close()


# -- the scripts --------------------------------------------------------------------------------------------

def scripts() -> list[Path]:
    return [STATION / "install-linux.sh", STATION / "sizeup-station", STATION / "health-check.sh",
            STATION / "start-station", STATION / "start-whisper",
            STATION / "install-windows.ps1", STATION / "uninstall-windows.ps1",
            STATION / "windows" / "usb-attach.ps1", STATION / "windows" / "wsl-keepalive.ps1"]


SHOW = re.compile(r"\b(echo|printf|say|warn|die|cat|tee|logger|Write-Host|Write-Output|Write-SizeupLog|Write-Verbose)\b")
SECRET_VALUE = re.compile(r"\$\{?(secret_\w+|" + "|".join(SECRETS) + r")\b|\$env:(" + "|".join(SECRETS) + r")\b")


def test_scripts_never_show_a_secret():
    """A secret's value may only be written into the env file (printf ... >> "$envtmp"); never
    echoed, logged or passed to anything that prints. No xtrace, hidden input for every secret."""
    for path in scripts():
        assert path.is_file(), path
        text = path.read_text()
        assert not re.search(r"^\s*set\s+-[a-z]*x", text, re.M), f"{path.name}: set -x would log secrets"
        for n, line in enumerate(text.splitlines(), 1):
            code = line.split(" #", 1)[0]
            if SECRET_VALUE.search(code) and SHOW.search(code):
                assert re.search(r'>>\s*"\$envtmp"\s*$', code) and code.lstrip().startswith("printf "), \
                    f"{path.name}:{n} shows a secret: {line.strip()}"
            m = re.search(r"\bread\b(.*)\bsecret_\w+", code)
            if m:
                assert re.search(r"\s-[a-z]*s", m.group(1)), f"{path.name}:{n}: read a secret without -s"
        if path.suffix == ".ps1":
            assert not SECRET_VALUE.search(text), f"{path.name}: the Windows side never handles the hub secrets"


def test_powershell_files_are_ascii():
    """Windows PowerShell 5.1 reads a BOM-less script as the ANSI code page: ASCII only."""
    for path in scripts():
        if path.suffix == ".ps1":
            bad = [(i, ch) for i, ch in enumerate(path.read_text()) if ord(ch) > 127]
            assert not bad, f"{path.name}: non-ASCII {bad[:3]}"


def test_units_restart_forever_and_load_the_secrets():
    svc = (STATION / "systemd" / "sizeup-station.service").read_text()
    for want in ("User=sizeup", "Restart=always", "RestartSec=5", "StartLimitIntervalSec=0",
                 "EnvironmentFile=-/etc/sizeup/station.env", "Environment=SIZEUP_STATE_DIR=/var/lib/sizeup",
                 "ExecStart=/usr/local/lib/sizeup/start-station", "Environment=SIZEUP_STT_EXTERNAL=1",
                 "Environment=SIZEUP_UPDATE_DIR=/run/sizeup", "RuntimeDirectory=sizeup\n", "RuntimeDirectoryPreserve=yes",
                 "WantedBy=multi-user.target", "Wants=network-online.target sizeup-whisper.service", "LimitNOFILE="):
        assert want.replace("\\n", "\n") in svc, want
    assert "/opt/sizeup/app" not in svc and "build.env" not in svc
    # the release that runs is resolved once, at start: a switch never changes code under a running process
    start = (STATION / "start-station").read_text()
    for want in ('rel=$(readlink -f /opt/sizeup/current)', 'PYTHONPATH="$rel"', 'SIZEUP_BUILD=$(head -n 1 "$rel/BUILD"',
                 'exec "$rel/venv/bin/python" -m sizeup --config /etc/sizeup/config.toml'):
        assert want in start, want
    timer = (STATION / "systemd" / "sizeup-health.timer").read_text()
    assert "OnUnitActiveSec=60s" in timer and "WantedBy=timers.target" in timer
    assert "SystemMaxUse=200M" in (STATION / "systemd" / "journald-sizeup.conf").read_text()


def test_installer_installs_what_is_in_the_folder():
    """Every deploy/station file the installers copy exists (a missing one would only fail on the box)."""
    sh = (STATION / "install-linux.sh").read_text()
    found = re.findall(r'"\$sys/([^"$]+)"', sh)
    assert "start-station" in found and "start-whisper" in found
    for rel in found:
        assert (STATION / rel).resolve().is_file(), rel
    for rel in re.findall(r'"\$rel/(deploy/[^"$]+)"', sh):
        assert (ROOT / rel).exists(), rel
    units = re.search(r"for t in (sizeup-[^;]+);", sh).group(1).replace("\\", " ").split()
    on_disk = sorted(p.name for p in (STATION / "systemd").iterdir() if p.suffix in (".service", ".timer", ".path"))
    assert sorted(units) == on_disk                       # every unit installed, none forgotten
    ps = (STATION / "install-windows.ps1").read_text()
    for rel in re.findall(r'"(windows\\[\w.-]+\.ps1)"', ps):
        assert (STATION / rel.replace("\\", "/")).is_file(), rel
    # the model names the Windows installer offers are the ones the Linux installer knows
    offered = re.search(r'ValidateSet\(([^)]*)\)', ps).group(1)
    for name in re.findall(r'"([^"]+)"', offered):
        assert f"    {name})" in sh, name


# -- side-by-side releases and the auto-update (issue #58) ---------------------------------------------------

def test_update_units_watch_the_want_file_and_run_as_root():
    path = (STATION / "systemd" / "sizeup-update.path").read_text()
    assert "PathChanged=/run/sizeup/update-wanted" in path and "Unit=sizeup-update.service" in path
    assert "WantedBy=paths.target" in path
    svc = (STATION / "systemd" / "sizeup-update.service").read_text()
    assert "Type=oneshot" in svc and "ExecStart=/usr/bin/python3 -I /usr/local/lib/sizeup/autoupdate.py run" in svc
    assert "User=" not in svc                              # root: the sizeup user only writes the want file
    timer = (STATION / "systemd" / "sizeup-update.timer").read_text()
    assert "OnUnitInactiveSec=" in timer and "WantedBy=timers.target" in timer
    whisper = (STATION / "systemd" / "sizeup-whisper.service").read_text()
    for want in ("User=sizeup", "ExecStart=/usr/local/lib/sizeup/start-whisper", "RuntimeDirectory=sizeup-whisper",
                 "Restart=always", "WantedBy=multi-user.target"):
        assert want in whisper, want


def test_installer_builds_beside_and_switches_by_rename():
    sh = (STATION / "install-linux.sh").read_text()
    for want in ("--stage) STAGE_SHA=", "--activate) ACTIVATE_NAME=", 'RELEASES=$OPT/releases', 'CURRENT=$OPT/current',
                 'archive --format=tar "$sha"', 'cp -al "$donor" "$tmp/venv"', 'mv -Tf "$tmp" "$2"',
                 'swap_link "releases/$name" "$CURRENT"', 'BUILDS=$OPT/builds', '-DCMAKE_INSTALL_PREFIX="$TR_B"',
                 "compileall", "--ci needs --from-dir or a local --repo",
                 'sizeup --config "$conf" --init-state', "systemd-analyze verify", '"$tr/bin/trunk-recorder" --version',
                 "clone -q --bare --no-local", "DEPLOY_KEY=$ETC/deploy_key"):
        assert want in sh, want
    # stage: nothing the running station uses is touched; activate is the only switch, after a preflight
    stage = sh[sh.index('if [ -n "$STAGE_SHA" ]; then\n  # --'):sh.index('if [ -n "$ACTIVATE_NAME" ]; then\n')]
    assert "preflight" in stage and "activate" not in stage and "systemctl" not in stage
    main = sh[sh.index("# -- install / repair / update"):]
    assert main.index('preflight "$REL"') < main.index('activate "$NAME"')
    # whisper-server is restarted only when its command line changed
    assert 'want=$("$LIB/start-whisper" --print' in sh and "have=$(cat /run/sizeup-whisper/argv" in sh


def test_start_whisper_runs_what_sizeup_would_have(tmp_path):
    """sizeup-whisper.service runs whisper-server with the same model / port / threads / beam as
    WhisperService would from the same config (so nothing changes when it moved out of sizeup)."""
    from sizeup.app import WhisperService
    text = (STATION / "start-whisper").read_text()
    py = text[text.index("<<'PY'\n") + 7:text.index("\nPY\n")]
    conf = tmp_path / "config.toml"
    conf.write_text(TEMPLATE.read_text().replace("beam_size = 0", "beam_size = 5").replace(
        "/var/lib/sizeup/trunk-recorder/talkgroups.csv", f"{ROOT}/deploy/trunk-recorder/talkgroups.csv"))
    got = subprocess.run([sys.executable, "-c", py, str(conf)], capture_output=True, text=True, check=True).stdout.split("\n")
    c = load(conf)
    want = WhisperService(c.stt_server, c.stt_model, c.stt_port, c.stt_threads, c.stt_beam_size).argv
    assert [x for x in got if x] == [os.path.realpath(want[0]), want[1], os.path.realpath(want[2]), *want[3:]]


def test_cli_has_the_update_commands():
    cli = (STATION / "sizeup-station").read_text()
    for want in ("autoupdate)", "on|off)", "now)", "rollback)", 'python3 -I "$UPDATER" "$sub"',
                 'exec python3 -I "$UPDATER" rollback', "systemctl is-active --quiet sizeup-update.service",
                 'python3 -I "$UPDATER" doctor', 'python3 -I "$UPDATER" status', "APP=/opt/sizeup/current"):
        assert want in cli, want


def test_the_template_follows_the_hub():
    import tomllib
    assert tomllib.loads(TEMPLATE.read_text())["station"]["auto_update"] is True


def test_the_hub_image_is_built_with_its_git_sha():
    """The station follows the hub's build: CI must pass the full sha it builds from."""
    wf = (ROOT / ".github" / "workflows" / "deploy.yml").read_text()
    assert "SIZEUP_BUILD=${{ env.SHA }}" in wf and "SHA: ${{ github.event.workflow_run.head_sha || github.sha }}" in wf
    assert "ARG SIZEUP_BUILD" in (ROOT / "Dockerfile").read_text()


def test_nothing_points_into_the_old_checkout():
    """/opt/sizeup/app (before side-by-side releases) goes away after the switch: the template, the
    units and the launchers never name it (the installer moves an old config's talkgroups_file)."""
    for path in [TEMPLATE, *(STATION / "systemd").iterdir(), STATION / "start-station", STATION / "start-whisper",
                 STATION / "health-check.sh"]:
        assert "/opt/sizeup/app" not in path.read_text(), path.name
        assert "/opt/sizeup/venv" not in path.read_text(), path.name
    sh = (STATION / "install-linux.sh").read_text()
    assert 'sed -i "s#$LEGACY_APP/deploy/trunk-recorder/talkgroups.csv#$TR_DIR/talkgroups.csv#" "$CONF"' in sh
