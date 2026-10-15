"""FG2, the county's Fireground 2 (GitHub #119: "i also want the ability to record FG2 ... lets add just FG2 (for
now)"): 458.0375 MHz NFM (county fireground list 2024-11-06, docs/PLAN.md), received exactly like FG1 through the
same path, nothing special-cased:

    config      a [[radio.channels]] traffic channel in the example, the station template and the hub, with FG1's
                squelch settings; [stt] aws_channels has it, so it defaults to Amazon Transcribe as FG1 does
    dongles     a job on the station page's / hub's dongle switches; auto-assign takes it last (FG1, P25, South 2,
                FG2), so only a 4th free dongle gets it and the first three's jobs don't move
    radio IDs   MDC-1200 / FleetSync at the key-up (the live tap) and from the transmission, "heard" rows, the
                attach rule (a known ID on a unit of the event only) and the new-ID prompt, as on FG1 (#163)
    alarms      a fire channel by default: mayday words and the emergency button alarm (#128)
    stations    an installed station's config gains it once at the switch (sizeup/cloud/configupdate.py), and a
                hub and a station that disagree about FG2 (one updated before the other) both keep working.
Fakes only: no dongle is opened (librtlsdr lookups faked), claims live in a temp dir (conftest.py)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import tomllib
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

import sizeup.radio.claims as claims
from sizeup import channels
from sizeup.app import App
from sizeup.cad.service import Cad
from sizeup.cloud import configupdate
from sizeup.config import AUTO_ORDER, ChannelCfg, Config, load
from sizeup.radio.fm import Channel, Segment
from sizeup.radio.mdc1200 import encode as mdc_encode
from tests.test_app import FakeWhisper, cfg, voice, wait
from tests.test_cloud import CHANNELS, HubRun, all_traffic, call, hub_cfg, station, station_cfg
from tests.test_sdr_auto_assign import DONGLES, P25, jobs, rig  # noqa: F401 (rig: the fixture)

ROOT = Path(__file__).resolve().parent.parent
STATION = ROOT / "deploy" / "station"
FG1 = ChannelCfg("FG1", 453.0375, "traffic", "fire-fg")
FG2 = ChannelCfg("FG2", 458.0375, "traffic", "fire-fg2")
THREE = (ChannelCfg("South 2", 470.2, "paging", "fire-page"), FG1, FG2)


def installed_config(tmp_path, monkeypatch, text: str | None = None) -> Config:
    from tests.test_station_install import station_config
    return station_config(tmp_path, monkeypatch, text)


# -- config -------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["config.example.toml", "deploy/station/config.station.toml",
                                  "deploy/hub/config.hub.toml"])
def test_fg2_is_a_traffic_channel_set_up_like_fg1_everywhere(path, tmp_path, monkeypatch):
    c = installed_config(tmp_path, monkeypatch) if "station" in path else load(ROOT / path)
    ch = {x.label: x for x in c.channels}
    fg1, fg2 = ch["FG1"], ch["FG2"]
    assert (fg2.mhz, fg2.kind) == (458.0375, "traffic")
    same = ("kind", "gain_db", "squelch_db", "drop_db", "hang_s", "continue_s")
    assert [getattr(fg2, k) for k in same] == [getattr(fg1, k) for k in same]
    assert [x.label for x in c.channels].index("FG2") == [x.label for x in c.channels].index("FG1") + 1
    assert "FG2" in c.stt_aws_channels and "FG1" in c.stt_aws_channels          # Amazon Transcribe by default, as FG1
    if "hub" not in path:
        assert c.sdr_auto_order == ("FG1", P25, "South 2", "FG2")


def test_the_default_order_puts_fg2_last_and_skips_it_without_the_channel(tmp_path):
    assert AUTO_ORDER == ("FG1", P25, "South 2", "FG2") and Config.__dataclass_fields__["sdr_auto_order"].default == AUTO_ORDER
    p = tmp_path / "c.toml"
    p.write_text('[radio]\ntones_file = ""\n[[radio.channels]]\nlabel = "FG1"\nmhz = 453.0375\nkind = "traffic"\n')
    assert load(p).sdr_auto_order == AUTO_ORDER                       # no auto_assign_order: the default
    assert channels.DEFAULT_AWS == ("South 2", "FG1", "FG2", "tg:307")
    assert channels.pref("FG2", {})["engine"] == "aws" and channels.pref("PC repeater", {})["engine"] == "local"
    assert {"Fireground 1", "Fireground 2"} <= set(channels.vocabulary_suggestions({}, []))


# -- dongles: a choice on every dongle, auto-assigned last ------------------------------------------------------


@pytest.mark.parametrize("n, want", [
    (3, ["FG1", P25, "South 2"]),                                # three dongles: exactly as before FG2
    (4, ["FG1", P25, "South 2", "FG2"]),                         # a 4th free dongle takes FG2
    (5, ["FG1", P25, "South 2", "FG2", "Off"]),                  # all four covered: the 5th stays Off
])
def test_fg2_is_auto_assigned_only_to_a_fourth_free_dongle(rig, n, want):
    plugged, logs, make = rig
    plugged[:] = (DONGLES + [(4, "RTL2838UHIDIR", "five")])[:n]
    app = make(channels=THREE, sdr_auto_order=AUTO_ORDER)
    assert [app.assign.get(s, "Off") for _i, _p, s in plugged] == want
    assert app.state()["sdr"]["modes"] == ["South 2", "FG1", "FG2", P25, "Off"]   # every dongle offers FG2
    if "FG2" in want:
        assert getattr(app._receiver("FG2").device, "serial", None) == "spare" and "sdr: spare → FG2 (auto)" in logs
        assert json.loads((claims.DIR / "spare.json").read_text())["mode"] == "FG2"
    else:
        assert app._program_on("FG2") is None
    if n == 5:
        assert "sdr: five → Off (auto: FG1, Fire/EMS 15, South 2, FG2 already covered)" in logs


def test_fg2_picked_by_hand_and_never_on_a_dongle_another_app_holds(rig):
    plugged, logs, make = rig
    claims.DIR.mkdir(parents=True, exist_ok=True)
    (claims.DIR / "spare.json").write_text(json.dumps({"app": "shortfinal", "pid": os.getppid(), "mode": "TWR",
                                                       "since": 1.0}))
    plugged[:] = DONGLES
    app = make(channels=THREE, sdr_auto_order=AUTO_ORDER)
    assert app.assign.get("spare", "Off") == "Off" and "sdr: spare in use by shortfinal (TWR), not auto-assigned" in logs
    assert "in use by shortfinal" in (app.set_mode("FG2", "spare") or "")          # not by hand either
    assert json.loads((claims.DIR / "spare.json").read_text())["app"] == "shortfinal"   # untouched
    # three dongles, FG2 wanted: a person moves South 2's dongle to it, as between FG1 / P25 / South 2 today
    assert app.set_mode("FG2", "adsb") is None
    assert jobs(app)["adsb"] == ("FG2", "person") and app._program_on("South 2") is None
    assert getattr(app._receiver("FG2").device, "serial", None) == "adsb"
    assert json.loads((claims.DIR / "adsb.json").read_text())["mode"] == "FG2"


def test_a_station_without_fg2_refuses_it_by_name(rig):
    plugged, logs, make = rig
    plugged[:] = DONGLES[:1]
    app = make()                                                  # South 2 and FG1 only (an older config)
    assert "FG2" not in app.modes
    assert app.set_mode("FG2", "00000001") == "unknown mode 'FG2'; one of South 2, FG1, Fire/EMS 15, Off"
    assert app.assign == {"00000001": "FG1"}


# -- radio IDs, attach rule, new-ID prompt, alarms: FG1's path ------------------------------------------------


@pytest.fixture
def app(tmp_path):
    a = App(replace(cfg(tmp_path), channels=THREE))
    a.stt.client = FakeWhisper({3: "Engine 63 to command, on location", 4: "Engine 64 mayday mayday mayday"})
    a.stt.start()
    yield a


def test_the_cad_treats_fg2_as_a_fireground_channel(app):
    c = app.cad
    assert c.s.tactical == ("FG1", "FG2") and c.comms.tactical == {"FG1", "FG2"}
    assert c.alarms.default_channels()[:2] == ["FG1", "FG2"] and c.hints.default_channels == ("FG1", "FG2")
    assert [x["key"] for x in app.channels_state()["fm"]] == ["South 2", "FG1", "FG2"]
    hub = Cad.from_config(replace(load(ROOT / "deploy" / "hub" / "config.hub.toml"), cad_db=""), log=lambda m: None)
    assert hub.s.tactical == ("FG1", "FG2") and "FG2" in hub.alarms.default_channels()


def test_fg2_ids_attach_only_a_known_radio_on_the_call_and_ask_about_new_ones(app):
    c = app.cad
    inc = c.create({"type": "STRU", "address": "209 Westchester Ave", "operator": "Lt. Ruiz"})
    c.dispatch(inc["id"], {"unit_ids": [u["id"] for u in c.store.units() if u["name"] == "E63"], "operator": "Lt. Ruiz"})
    ch, t0 = Channel(458.0375, "FG2"), time.time() + 1               # after E63 was put on the call
    burst = lambda rid: [mdc_encode(0x01, 0x00, rid).astype(np.float32)]
    app._segment(ch, Segment(t0, t0 + 3, voice(3.0), 35.0, burst(0x6301)))        # E63's MDC ID, E63 on the call
    app._segment(ch, Segment(t0 + 10, t0 + 13, voice(3.0), 35.0, burst(0x7F21)))  # an ID nobody has named
    app._segment(ch, Segment(t0 + 20, t0 + 23, voice(3.0), 35.0))                 # no ID at all
    assert wait(lambda: len(c.comms.query(channel="FG2")) == 3 and all(r["text"] for r in c.comms.query(channel="FG2")))
    rows = {r["radio_id"]: r for r in c.comms.query(channel="FG2")}
    assert (rows["6301"]["rig"], [e["id"] for e in rows["6301"]["events"]]) == ("E63", [inc["id"]])
    assert rows["7F21"]["events"] == [] and rows[""]["events"] == [] and rows[""]["id_note"] == "none"
    asked = c.state()["radio_ids"]
    assert [(x["system"], x["radio_id"], x["channel"]) for x in asked] == [("mdc", "7F21", "FG2")]
    heard = app.radios.get("mdc", "7F21")
    assert (heard["source"], heard["last_channel"]) == ("heard", "FG2")
    stats = {x["channel"]: (x["n"], x["with_id"]) for x in c.radio_registry()["id_stats"]}
    assert stats == {"FG2": (3, 2)}


def test_a_mayday_and_the_emergency_button_on_fg2_alarm_as_on_fg1(app):
    from tests.test_alarms import tx
    al = app.cad.alarms
    a = al.on_traffic(tx("FG2-1", "Engine 64 mayday mayday mayday", source="FG2"))
    assert a["level"] == "alarm" and a["keywords"] == ["mayday"] and a["channel"] == "FG2"
    b = al.on_traffic(tx("FG2-2", source="FG2", radio_id="7F21", emergency=True))     # an unknown radio: still ours
    assert b["level"] == "alarm" and b["what"] == "emergency button"
    assert al.on_traffic(tx("PC-1", "mayday mayday", source="PC repeater")) is None    # not a fire channel (yet)


def test_the_emergency_button_on_fg2_alarms_at_the_key_up(app):
    from tests.test_fg_emergency import alarms, carrier_with, finish, press
    from tests.test_livetap import Rig, quiet
    r = Rig(label="FG2")                                          # test_fg_emergency's rig, on FG2
    r.live, r.tap = app.live, app.tap
    r.clock.t = time.time() - 20
    r.t0 = r.clock.t
    x = carrier_with(np.concatenate([mdc_encode(0x00, 0x80, 0x6401), quiet(0.05), mdc_encode(0x00, 0x80, 0x6401)]),
                     talk=4.0)
    seen = press(r, app, x)
    assert seen is not None and r.sq.open
    [a] = alarms(app)["open"]
    assert (a["what"], a["radio_id"], a["rig"], a["channel"], a["level"]) == ("emergency button (MDC)", "6401", "E64",
                                                                              "FG2", "alarm")
    assert a["traffic_id"].startswith("FG2-live-")
    finish(r, app, x, seen)
    assert wait(lambda: alarms(app)["open"][0]["text"] == "Engine 64 mayday mayday mayday", 10)
    assert len(alarms(app)["open"]) == 1


def test_the_live_tap_names_the_fg2_talker_before_the_carrier_drops():
    from tests.test_fg_emergency import hiss, keyed          # their own rng: test_livetap's drives its tone phases
    from tests.test_livetap import Rig, quiet, voice as tap_voice
    r = Rig(label="FG2")
    x = np.concatenate([hiss(3.0), keyed(np.concatenate([quiet(0.1), mdc_encode(0x01, 0x80, 0x6301), tap_voice(3.0)])),
                        hiss(1.0)])
    assert r.feed(x, until=lambda r: any(e["rig"] == "E63" for e in r.live.state())) is not None and r.sq.open
    [e] = r.live.state()
    assert (e["channel"], e["radio_id"], e["system"]) == ("FG2", "6301", "fm")


# -- a hub and a station that disagree about FG2 (one updated first) ------------------------------------------


def fg2_segment(app, text_s=3.0):
    now = time.time()
    app._segment(Channel(458.0375, "FG2"), Segment(now, now + text_s, voice(text_s), 20.0))
    return f"FG2-{int(now * 1000)}"


def test_a_hub_with_fg2_and_a_station_without_it(tmp_path):
    hub = HubRun(hub_cfg(tmp_path, channels=THREE))
    st = None
    try:
        st = station(station_cfg(tmp_path, hub.url), {3: "Engine 63 on location"})         # South 2 and FG1 only
        assert call(hub.url + "/api/cad/settings/channels", "POST",
                    {"action": "set", "key": "FG2", "engine": "local"}, token=None)[0] == 200
        assert wait(lambda: hub.get("/api/state")["channels"]["sync"]["state"] == "applied", 10)
        assert st.channel_pref("FG2")["engine"] == "local" and "FG2" not in [c.label for c in st.cfg.channels]
        assert [x["key"] for x in hub.get("/api/state")["channels"]["fm"]] == ["South 2", "FG1", "FG2"]
    finally:
        if st is not None:
            st.link.stop()
        hub.close()


def test_a_station_with_fg2_and_a_hub_without_it(tmp_path):
    hub = HubRun(hub_cfg(tmp_path))                                                        # South 2 and FG1 only
    st = None
    try:
        st = station(station_cfg(tmp_path, hub.url, channels=CHANNELS + (FG2,)), {3: "Engine 63 on location"})
        assert wait(lambda: hub.app.hub.connected(), 5)
        tid = fg2_segment(st)
        assert wait(lambda: any(t["id"] == tid and t["source"] == "FG2" and t["text"]
                                for t in all_traffic(hub.get("/api/state"))), 15)
        row = next(r for r in hub.get("/api/cad/comms")["rows"] if r["id"] == tid)
        assert row["channel"] == "FG2" and row["events"] == []          # kept and shown; nothing to attach by
        st._heard("mdc", "7F21", at=time.time(), channel="FG2")
        assert wait(lambda: (hub.app.radios.get("mdc", "7F21") or {}).get("last_channel") == "FG2", 10)
    finally:
        if st is not None:
            st.link.stop()
        hub.close()


# -- an installed station's config gains FG2 once (sizeup/cloud/configupdate.py) ----------------------------------


def old_template() -> str:
    """The station template as it was before FG2 (what /etc/sizeup/config.toml was made from)."""
    t = (STATION / "config.station.toml").read_text()
    t = re.sub(r"\n\[\[radio\.channels\]\]\nlabel = \"FG2\".*?\n\n", "\n\n", t, flags=re.S)
    t = t.replace('auto_assign_order = ["fg1", "p25", "south2", "fg2"]', 'auto_assign_order = ["fg1", "p25", "south2"]')
    assert [c["label"] for c in tomllib.loads(t)["radio"]["channels"]] == ["South 2", "FG1"] and '"fg2"' not in t
    return t


def installed(tmp_path, monkeypatch, text: str) -> Path:
    monkeypatch.setenv("SIZEUP_STATE_DIR", str(tmp_path / "state"))
    p = tmp_path / "etc" / "config.toml"
    p.parent.mkdir(exist_ok=True)
    p.write_text(text.replace("/var/lib/sizeup/trunk-recorder/talkgroups.csv", f"{ROOT}/deploy/trunk-recorder/talkgroups.csv"))
    os.chmod(p, 0o640)
    return p


def test_an_installed_station_config_gains_fg2_once_and_keeps_everything_else(tmp_path, monkeypatch):
    own = old_template().replace('sdr_names = { v4 = "V4"', 'sdr_names = { spare = "V3 #3", v4 = "V4"')   # an edit
    conf = installed(tmp_path, monkeypatch, own)
    marks = tmp_path / "etc" / "config-updates"
    before = load(conf)
    assert [c.label for c in before.channels] == ["South 2", "FG1"] and before.sdr_auto_order == ("FG1", P25, "South 2")
    said = configupdate.run(conf, marks)
    assert len(said) == 1 and said[0].startswith("fg2: FG2 (458.0375 MHz, traffic) added") and '"fg2"' in said[0]
    text, kept = conf.read_text(), conf.with_name("config.toml.before-fg2").read_text()
    assert kept == own.replace("/var/lib/sizeup/trunk-recorder/talkgroups.csv", f"{ROOT}/deploy/trunk-recorder/talkgroups.csv")
    assert text.replace(', "fg2"]', "]") == kept + configupdate.FG2_BLOCK                         # only added to
    assert 'auto_assign_order = ["fg1", "p25", "south2", "fg2"]' in text and oct(conf.stat().st_mode & 0o777) == "0o640"
    after = load(conf)
    assert [(c.label, c.mhz, c.kind) for c in after.channels] == [("South 2", 470.2, "paging"),
                                                                  ("FG1", 453.0375, "traffic"), ("FG2", 458.0375, "traffic")]
    assert after.sdr_auto_order == ("FG1", P25, "South 2", "FG2") and after.sdr_names["spare"] == "V3 #3"
    assert replace(after, channels=before.channels, sdr_auto_order=before.sdr_auto_order) == before   # nothing else
    assert marks.read_text() == "fg2\n"
    # once: taken out by hand, it stays out at the next update
    conf.write_text(conf.with_name("config.toml.before-fg2").read_text())
    assert configupdate.run(conf, marks) == [] and [c.label for c in load(conf).channels] == ["South 2", "FG1"]


def test_the_template_and_odd_configs_are_left_alone(tmp_path, monkeypatch):
    marks = tmp_path / "marks"
    conf = installed(tmp_path, monkeypatch, (STATION / "config.station.toml").read_text())   # a new install: has it
    same = conf.read_text()
    assert configupdate.run(conf, marks) == ["fg2: already has 'FG2' on 458.0375 MHz: nothing added"]
    assert conf.read_text() == same and marks.read_text() == "fg2\n"
    # no radios (a hub-like config): nothing; no auto_assign_order: the channel only (the code's order ends with it)
    assert configupdate.add_fg2('[web]\nport = 8091\n')[1] == "fg2: no [[radio.channels]] here (no radios): nothing added"
    text, said = configupdate.add_fg2('[radio]\nsdrs = "auto"\n[[radio.channels]]\nlabel = "FG1"\nmhz = 453.0375\n')
    assert "auto_assign_order" not in said and tomllib.loads(text)["radio"]["channels"][-1]["label"] == "FG2"
    # a channel on 458.0375 under another name counts as FG2
    assert "already has" in configupdate.add_fg2('[[radio.channels]]\nlabel = "Fireground 2"\nmhz = 458.0375\n')[1]
    # an order it can't edit safely, or a config that doesn't parse: nothing written, tried again next time
    odd = installed(tmp_path, monkeypatch, old_template().replace('auto_assign_order = ["fg1", "p25", "south2"]',
                                                                  'auto_assign_order = [\n  "fg1",\n  "p25",\n]'))
    m2 = tmp_path / "marks2"
    keep = odd.read_text()
    [said] = configupdate.run(odd, m2)
    assert said.startswith("fg2: not applied:") and "by hand" in said and odd.read_text() == keep and not m2.exists()
    odd.write_text("[radio\n")
    assert configupdate.run(odd, m2)[0].startswith("fg2: not applied:") and not m2.exists()


def test_the_installer_runs_it_with_the_system_python_at_the_switch(tmp_path, monkeypatch):
    sh = (STATION / "install-linux.sh").read_text()
    body = re.search(r"^activate\(\) \{(.*?)^\}", sh, re.S | re.M).group(1)
    assert 'python3 -I "$rel/sizeup/cloud/configupdate.py" "$CONF" "$ETC/config-updates"' in body
    assert body.index("configupdate.py") < body.index('swap_link "releases/$name" "$CURRENT"')   # before the restart
    src = (ROOT / "sizeup" / "cloud" / "configupdate.py").read_text()
    imports = {m.split(".")[0] for m in re.findall(r"^(?:from|import) ([\w.]+)", src, re.M)}
    assert imports <= {"__future__", "os", "re", "sys", "tomllib", "pathlib"}             # stdlib only
    conf = installed(tmp_path, monkeypatch, old_template())
    r = subprocess.run([sys.executable, "-I", str(ROOT / "sizeup" / "cloud" / "configupdate.py"), str(conf),
                        str(tmp_path / "etc" / "config-updates")], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and r.stdout.startswith("fg2: FG2 (458.0375 MHz, traffic) added") and r.stderr == ""
    assert [c.label for c in load(conf).channels] == ["South 2", "FG1", "FG2"]
