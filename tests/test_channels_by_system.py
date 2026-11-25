"""Settings > Radios > Channels with more than one P25 system (#409): every per-talkgroup choice is keyed by its system.

The bug: the hub's talkgroup rows are the station's TalkgroupPrefs.state(), which had no "tier_set", so the page drew
another system's talkgroup (MRRS "PD Dispatch", TG 9211) with the county's Record / Main / Transcribe switches, and a
switch sent {"tg": 9211} alone. A bare number is the county's: Main landed on a county talkgroup 9211 that doesn't
exist, an "Unknown — TG 9211 · MAIN · Not heard" card, and PD Dispatch's own Main stayed off.

The fix, checked here:
    the hub      tier_set on the station's rows (sizeup/cloud/hub.py), so another system's talkgroup has its tier
    the page     a talkgroup is its source ("p25:mrrs:9211"): its row, its pending switch, what it posts
                 (radio-controls.js); the county's are still sent and saved as before ("tg:307", talkgroups.json)
    the station  a bare number the county neither lists nor has heard but another system lists is refused
                 (App.set_talkgroup), and such a county choice saved before is dropped on loading
                 (TalkgroupPrefs.migrated); a county section's switch never touches another system's section
    the speech-to-text choices  under the qualified key on the hub and on the station (sizeup/channels.py, unchanged)
    names        a channel's display name from this page goes to the radio rail's store (#607 radio_folders names)
    by system    Analog FM, Airband, County P25, then each other P25 system, its groups under it

No radios, no network. Every talkgroup, name and number here is invented (MRRS's file is the shipped one)."""

from __future__ import annotations

import json
import threading
import urllib.request

import pytest

from sizeup import channels, sourcekeys
from sizeup.cad import radiofolders
from sizeup.cad.store import CadError
from sizeup.talkgroups import TalkgroupPrefs
from sizeup.web.server import WebServer
from tests.test_cad import make_cad
from tests.test_p25_systems import MRRS, PD, two  # noqa: F401 (fixture)
from tests.test_radio_emergency import NODE, STATIC

COUNTY = {307: {"name": "Fire/EMS 15", "group": "Fire/EMS", "encrypted": False},
          5021: {"name": "Made-up Ops", "group": "Unknown", "encrypted": False}}


def prefs(tmp_path, others=None) -> TalkgroupPrefs:
    return TalkgroupPrefs(COUNTY, {307}, tmp_path / "talkgroups.json", others=others or {"mrrs": MRRS.talkgroups})


# ---- the station: what a switch is saved under ---------------------------------------------------------------------


def test_main_and_record_by_number_alone_never_make_a_county_orphan(two):
    app, _said, _logs = two
    assert app.tg.elsewhere(PD) == ["mrrs"] and app.tg.elsewhere(307) == []
    for field in ({"main": True}, {"record": False}):
        err = app.set_talkgroup(PD, field.get("record"), field.get("main"))
        assert err and "mrrs" in err and f"p25:mrrs:{PD}" in err
    rows = app.talkgroups_state()
    assert [r["source"] for r in rows if r["tg"] == PD] == [f"p25:mrrs:{PD}"]        # no county 9211, no "Unknown" card
    pd = next(r for r in rows if r["tg"] == PD)
    assert (pd["tier_set"], pd["main"], pd["tier"]) == (True, False, "alert")
    # by its source: its own talkgroup (Record is its tier), never Main
    assert app.set_talkgroup(PD, False, None, None, "mrrs") is None
    assert app.tg.tier(PD, "mrrs") == "off" and app.tg.elsewhere(PD) == ["mrrs"]
    assert app.set_talkgroup(PD, None, True, None, "mrrs") is None and not app.tg.main(PD, "mrrs")
    # the county's, as always: by its number, saved under it
    assert app.set_talkgroup(307, None, False) is None and not app.tg.main(307)
    assert app.set_talkgroup(301, None, True) is None and app.tg.main(301)
    assert json.loads((app.clips_dir.parent / "talkgroups.json").read_text()) == {"307": {"main": False}, "301": {"main": True}}
    # the Channels setting's Record for a talkgroup goes the same way
    with pytest.raises(CadError, match="mrrs"):
        app.cad.settings_post("channels", {"action": "set", "key": f"tg:{PD}", "record": True, "operator": "Lt. Test"})
    app.cad.settings_post("channels", {"action": "set", "key": f"p25:mrrs:{PD}", "record": True, "operator": "Lt. Test"})
    assert app.tg.tier(PD, "mrrs") == "alert"
    # sent as the county's own source, the number is the county's even though another system lists it
    assert app.set_talkgroup(PD, False, None, None, sourcekeys.COUNTY) is None
    assert not app.tg.record(PD) and app.tg.tier(PD, "mrrs") == "alert"


def test_a_batch_refused_part_way_says_so_and_audits_what_was_applied(two):
    app, _said, _logs = two
    with pytest.raises(CadError, match=f"tg:{PD}: .*mrrs.*1 before it applied"):
        app.cad.settings_post("channels", {"action": "set_many", "keys": ["tg:301", f"tg:{PD}", "tg:307"], "record": False,
                                           "operator": "Lt. Test"})
    assert not app.tg.record(301) and app.tg.record(307)                    # the one before applied, none after
    a = [(r["key"], r["new"]) for r in app.cad.settings.audit_log("radios") if r["key"].startswith("channel/")]
    assert [(k, json.loads(v)) for k, v in a] == [("channel/tg:301", {"record": False})]
    # all applied: all audited (a talkgroup's Record was never on the record before)
    app.cad.settings_post("channels", {"action": "set_many", "keys": ["tg:301", "tg:307"], "record": True, "operator": "Lt. Test"})
    assert app.tg.record(301) and app.tg.record(307)
    assert len([r for r in app.cad.settings.audit_log("radios") if r["key"].startswith("channel/")]) == 3


def test_a_county_talkgroup_heard_but_not_listed_is_still_the_countys(tmp_path):
    p = prefs(tmp_path)
    p.seen(4242)                                   # heard on the county system, not in its file, on no other system
    assert p.elsewhere(4242) == []
    p.set(4242, main=True)
    assert p.main(4242) and p.main(4242, sourcekeys.COUNTY)


def test_a_number_on_both_systems_is_two_talkgroups(tmp_path):
    # (config.load refuses a system that reuses a county number; the keys hold even so)
    east = {307: {"name": "East Ops", "group": "Fire/EMS", "encrypted": False, "tier": "text"},
            9001: {"name": "East Fire", "group": "Fire/EMS", "encrypted": False, "tier": "record"}}
    p = prefs(tmp_path, {"east": east})
    p.set(307, main=False)
    p.set(307, record=False, system="east")
    assert (p.main(307), p.record(307), p.tier(307)) == (False, True, None)               # the county's
    assert (p.tier(307, "east"), p.record(307, "east"), p.main(307, "east")) == ("off", False, False)
    rows = {r["source"]: r for r in p.state()}
    assert rows["p25:westchester:307"]["key"] == "tg:307" and rows["p25:east:307"]["key"] == "p25:east:307"
    # a county section's switch: the county's talkgroups only, never another system's section of the same name
    assert p.set_group("Fire/EMS", record=False) == 1
    assert not p.record(307) and p.tier(9001, "east") == "record" and p.tier(307, "east") == "off"
    assert p.set_group("Fire/EMS", record=False, system="east") == 2 and p.tier(9001, "east") == "off"
    assert json.loads((tmp_path / "talkgroups.json").read_text()) == {"307": {"main": False, "record": False}}


def test_a_county_choice_saved_under_another_systems_number_is_dropped_on_loading(tmp_path):
    # what the page wrote before the fix: Main on PD Dispatch, sent as {"tg": 9211}; and a county talkgroup the county's
    # file doesn't list, heard on the county, that has a number MRRS lists too (config.load allows that)
    other = 9212
    (tmp_path / "talkgroups.json").write_text(json.dumps({"307": {"record": False}, str(PD): {"main": True},
                                                          str(other): {"record": False}, "5021": {"main": True},
                                                          "4242": {"record": False}}))
    p = prefs(tmp_path)
    assert p.migrated == [] and str(PD) in json.loads((tmp_path / "talkgroups.json").read_text())   # nothing yet
    p.seed(other, 1000.0)                               # the history: a county call on it
    p.seed(PD, 1000.0, "mrrs")                          # (the other system's own calls say nothing of the county's)
    assert p.drop_orphans() == [(PD, "mrrs", {"main": True})] == p.migrated
    assert json.loads((tmp_path / "talkgroups.json").read_text()) == {           # the county's kept, under their numbers
        "307": {"record": False}, str(other): {"record": False}, "5021": {"main": True}, "4242": {"record": False}}
    kept = json.loads((tmp_path / "talkgroups_migrated.json").read_text())       # what went, kept to put back by hand
    assert kept["version"] == 1 and [(k["tg"], k["system"], k["prefs"]) for k in kept["dropped"]] == [(PD, "mrrs", {"main": True})]
    assert not list(tmp_path.glob("*.tmp"))
    assert [r["source"] for r in p.state() if r["tg"] == PD] == [f"p25:mrrs:{PD}"]
    assert not p.record(307) and not p.record(other) and p.main(5021) and not p.record(4242)
    again = prefs(tmp_path)
    again.seed(other, 1000.0)
    assert again.drop_orphans() == [] and not again.record(other)                # the heard one stays the county's
    assert json.loads((tmp_path / "talkgroups_migrated.json").read_text()) == kept


def test_it_runs_once_per_station_never_again(tmp_path):
    p = prefs(tmp_path)
    assert p.drop_orphans() == []                       # nothing to drop: still marked done
    assert json.loads((tmp_path / "talkgroups_migrated.json").read_text())["dropped"] == []
    # later: a county talkgroup the file doesn't list, with a number MRRS lists, quiet past the history: kept
    p.set(9212, record=False)
    again = prefs(tmp_path)
    assert again.drop_orphans() == [] and not again.record(9212)
    assert json.loads((tmp_path / "talkgroups.json").read_text()) == {"9212": {"record": False}}


def test_a_sidecar_that_cant_be_written_drops_nothing(tmp_path, monkeypatch):
    import sizeup.talkgroups as tgmod
    (tmp_path / "talkgroups.json").write_text(json.dumps({str(PD): {"main": True}}))
    p = prefs(tmp_path)

    def refuse(*a):
        raise OSError("read-only")
    monkeypatch.setattr(tgmod.os, "replace", refuse)
    assert p.drop_orphans() == [] and p.migrated == []
    assert json.loads((tmp_path / "talkgroups.json").read_text()) == {str(PD): {"main": True}}
    assert not (tmp_path / "talkgroups_migrated.json").exists()
    monkeypatch.undo()                                  # the next start: it can, so it does
    assert prefs(tmp_path).drop_orphans() == [(PD, "mrrs", {"main": True})]


def test_a_corrupt_sidecar_is_moved_aside_and_nothing_dropped(tmp_path):
    (tmp_path / "talkgroups.json").write_text(json.dumps({str(PD): {"main": True}}))
    (tmp_path / "talkgroups_migrated.json").write_text("{not json")
    p = prefs(tmp_path)
    assert p.drop_orphans() == [] and p.main(PD)                                  # it ran before: done
    aside = list(tmp_path.glob("talkgroups_migrated.json.corrupt-*"))
    assert len(aside) == 1 and aside[0].read_text() == "{not json"
    assert not (tmp_path / "talkgroups_migrated.json").exists()


def test_with_no_county_talkgroups_file_nothing_is_dropped(tmp_path):
    # the config always has the [p25] talkgroups (307 ...): what counts is whether the county's file gave rows
    from sizeup.config import load
    from tests.test_p25_systems import ROOT
    assert load(ROOT / "config.example.toml").p25_tg_file_rows > 0
    (tmp_path / "talkgroups.json").write_text(json.dumps({str(PD): {"record": False}}))
    p = prefs(tmp_path)
    assert p.drop_orphans(county_listed=False) == [] and not p.record(PD)
    assert not (tmp_path / "talkgroups_migrated.json").exists()                  # not marked: tried again with a file


def test_the_app_says_what_it_dropped(two, tmp_path):
    app, _said, logs = two
    assert not any("dropped" in m for m in logs)
    (app.clips_dir.parent / "talkgroups.json").write_text(json.dumps({str(PD): {"main": True}}))
    import sizeup.app as appmod
    from dataclasses import replace
    again = appmod.App(replace(app.cfg, p25_tg_file_rows=2))
    try:
        assert any(f"county choice for {PD} dropped (main=True; kept in talkgroups_migrated.json): {PD} is mrrs's" in m
                   for m in logs)
        assert all(r["system"] == "mrrs" for r in again.talkgroups_state() if r["tg"] == PD)
    finally:
        again.stop()


def test_the_speech_to_text_choices_are_under_the_qualified_key_to_the_station(two):
    app, _said, _logs = two
    app.cad.settings_post("channels", {"action": "set", "key": f"p25:mrrs:{PD}", "engine": "local", "operator": "Lt. Test"})
    app.cad.settings_post("channels", {"action": "set", "key": "tg:307", "engine": "local", "operator": "Lt. Test"})
    ov = app.cad.settings.channel_overrides()
    assert ov[f"p25:mrrs:{PD}"] == {"engine": "local"} and ov["tg:307"] == {"engine": "local"} and f"tg:{PD}" not in ov
    sent = channels.for_station(ov)
    assert channels.check_map(sent) == sent and f"p25:mrrs:{PD}" in sent
    assert app.channel_pref(f"p25:mrrs:{PD}")["engine"] == "local" and app.channel_pref("tg:307")["engine"] == "local"


# ---- the hub: the station's rows -------------------------------------------------------------------------------------


def test_the_hub_marks_the_stations_rows_of_another_system(tmp_path):
    from sizeup.cloud.hub import HubApp
    from tests.test_cloud import hub_cfg
    hub = HubApp(hub_cfg(tmp_path))
    rows = prefs(tmp_path).state()                     # what a station sends (StationLink._state: TalkgroupPrefs.state())
    assert all("tier_set" not in r for r in rows)
    hub.hub._state(json.dumps({"at": 0, "status": {"id": "st", "talkgroups": rows}}).encode())
    got = {r["source"]: r for r in hub.hub.talkgroups}
    assert got[f"p25:mrrs:{PD}"]["tier_set"] is True and got["p25:westchester:307"]["tier_set"] is False
    hub.hub._state(json.dumps({"at": 0, "status": {"id": "st", "talkgroups": [dict(rows[0], tier_set=False)]}}).encode())
    assert hub.hub.talkgroups[0]["tier_set"] is False                             # a station's own word stays


# ---- the endpoint: a source goes with its system, the county's as before -------------------------------------------


def test_the_switch_endpoint_sends_the_system_and_the_countys_as_before():
    calls = []

    def spy(*a):
        calls.append(a)
        return None
    srv = WebServer("127.0.0.1", 0, {}, lambda: {}, lambda c: None, lambda i: False, set_talkgroup=spy)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.httpd.server_address[1]}/api/talkgroup"
    try:
        for body in ({"source": f"p25:mrrs:{PD}", "main": True}, {"source": "p25:westchester:307", "record": False},
                     {"source": f"p25:mrrs:{PD}", "tier": "off"}, {"tg": 307, "main": True}):
            r = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(r, timeout=10) as res:
                assert res.status == 200
    finally:
        srv.shutdown()
    assert calls == [(PD, None, True, None, "mrrs"), (307, False, None, None, "westchester"), (PD, None, None, "off", "mrrs"),
                     (307, None, True)]


def test_the_hub_sends_the_countys_source_bare_to_a_station_from_before_systems(tmp_path):
    from sizeup.cloud.hub import HubApp
    from tests.test_cloud import hub_cfg
    hub = HubApp(hub_cfg(tmp_path))
    sent = []
    hub._switch = lambda *a: sent.append(a)
    hub.hub.status = {"id": "st"}
    hub.set_talkgroup(307, False, None, None, sourcekeys.COUNTY)
    hub.hub.status = {"id": "st", "p25_systems": []}
    hub.set_talkgroup(307, False, None, None, sourcekeys.COUNTY)
    hub.set_talkgroup(PD, None, True, None, "mrrs")
    assert sent == [("set_talkgroup", 307, False, None), ("set_talkgroup", 307, False, None, None, "westchester"),
                    ("set_talkgroup", PD, None, True, None, "mrrs")]


# ---- a display name from Settings > Radios > Channels: the radio rail's store --------------------------------------


def test_a_name_from_the_channels_page_is_the_rails_audited_and_display_only(tmp_path):
    cad = make_cad(tmp_path)
    cad.comms.context_broadcast = True
    cad.settings_post("radio_folders", {"action": "name", "key": f"p25:mrrs:{PD}", "name": "  PD   North ", "operator": "Lt. Test"})
    assert cad.radio_folders == {"folders": [], "names": {f"p25:mrrs:{PD}": "PD North"}}     # no folders: the automatic grouping
    assert cad.comms_sources()["folders"]["names"] == {f"p25:mrrs:{PD}": "PD North"}
    a = [r for r in cad.settings.audit_log("radios") if r["key"].startswith("radio_folders/names/")]
    assert [(r["key"], r["old"], r["new"], r["operator"]) for r in a] == [
        (f"radio_folders/names/p25:mrrs:{PD}", "default", "PD North", "Lt. Test")]
    # the county's older key is its qualified one; an FM channel, an airband channel
    cad.settings_post("radio_folders", {"action": "name", "key": "tg:307", "name": "County Fire", "operator": "Lt. Test"})
    cad.settings_post("radio_folders", {"action": "name", "key": "fm:FG1", "name": "Fireground 1", "operator": "Lt. Test"})
    cad.settings_post("radio_folders", {"action": "name", "key": "air:Tower", "name": "Tower cab", "operator": "Lt. Test"})
    assert cad.radio_folders["names"] == {"air:Tower": "Tower cab", "fm:FG1": "Fireground 1",
                                          f"p25:mrrs:{PD}": "PD North", "p25:westchester:307": "County Fire"}
    # the rail's folders stay as an admin left them
    folders = [{"id": "u:fg01", "name": "Fireground", "section": "", "items": ["fm:FG1"]}]
    cad.settings_post("radio_folders", {"action": "set", "base": radiofolders.rev(cad.radio_folders), "folders": folders,
                                        "names": cad.radio_folders["names"], "operator": "Lt. Test"})
    cad.settings_post("radio_folders", {"action": "name", "key": "fm:FG1", "name": "FG One", "operator": "Lt. Test"})
    assert cad.radio_folders["folders"] == folders and cad.radio_folders["names"]["fm:FG1"] == "FG One"
    # checked: at most 40 characters, a source key
    with pytest.raises(CadError, match="at most 40"):
        cad.settings_post("radio_folders", {"action": "name", "key": "fm:FG1", "name": "A" * 41, "operator": "Lt. Test"})
    with pytest.raises(CadError, match="key"):
        cad.settings_post("radio_folders", {"action": "name", "key": "9211", "name": "X", "operator": "Lt. Test"})
    # blank: its own name again; nothing left: the setting is gone
    cad.settings_post("radio_folders", {"action": "reset", "base": radiofolders.rev(cad.radio_folders), "operator": "Lt. Test"})
    cad.settings_post("radio_folders", {"action": "name", "key": "fm:FG1", "name": "Fireground 1", "operator": "Lt. Test"})
    cad.settings_post("radio_folders", {"action": "name", "key": "fm:FG1", "name": "  ", "operator": "Lt. Test"})
    assert cad.radio_folders is None and cad.comms_sources()["folders"] is None
    assert [r["new"] for r in cad.settings.audit_log("radios") if r["key"] == "radio_folders/names/fm:FG1"][0] == "default"
    # the same again: nothing written
    n = len(cad.settings.audit_log("radios"))
    cad.settings_post("radio_folders", {"action": "name", "key": "fm:FG1", "name": "", "operator": "Lt. Test"})
    assert len(cad.settings.audit_log("radios")) == n


def test_without_a_sign_in_another_systems_talkgroup_cant_be_named(tmp_path):
    cad = make_cad(tmp_path)                       # all-in-one, nobody verified: no context at all
    with pytest.raises(CadError, match="another system"):
        cad.settings_post("radio_folders", {"action": "name", "key": f"p25:mrrs:{PD}", "name": "X", "operator": "Lt. Test"})
    cad.settings_post("radio_folders", {"action": "name", "key": "fm:FG1", "name": "Fireground 1", "operator": "Lt. Test"})
    assert cad.radio_folders["names"] == {"fm:FG1": "Fireground 1"}


# ---- the page (node) ---------------------------------------------------------------------------------------------------

HARNESS = r"""
const [CADJS, RC, INF] = process.argv.slice(2);
const cadjs = fs.readFileSync(CADJS, "utf8");
eval(cadjs.slice(cadjs.indexOf("function syncList("), cadjs.indexOf("\nfunction flash(")).replace("function syncList(", "global.syncList = function ("));
Object.defineProperty(N.prototype, "options", { get() { return this.children; } });
const _el = global.el;
global.el = (...a) => { const e = _el(...a); e.style.setProperty = () => {}; return e; };
const IN = JSON.parse(fs.readFileSync(INF, "utf8"));
const asked = [];
let names = IN.names;
global.fetch = async (url, o = {}) => {
  asked.push([url, o.body ? JSON.parse(o.body) : null]);
  if (url === "/api/talkgroup" || url === "/api/talkgroup-group") return { ok: true, status: 200, json: async () => ({ ok: true }) };
  return { ok: false, status: 503, headers: { get: () => "" }, json: async () => ({}) };
};
// settings.js S_RC_NAMES: the radio rail's names (GET /api/cad/comms/sources, POST /api/cad/settings/radio_folders)
const NAMES = {
  load: async () => ({ rev: "r1", folders: [], names }),
  save: async (key, name) => {
    asked.push(["name", { key, name }]);
    names = { ...names, [key]: name };
    if (!name) delete names[key];
    return { rev: "r2", folders: [], names };
  },
};
eval(fs.readFileSync(RC, "utf8"));
const tick = () => new Promise((ok) => setImmediate(ok));
(async () => {
  const out = {};
  const box = new N("div");
  RadioControls.mount(box, { reception: true, tab: "channels", names: NAMES });
  await tick(); await tick();
  const st = RadioControls._rx.st;
  st.state = IN.state;
  RadioControls._tg.render();
  const groups = box.querySelector("div.tg-groups");
  const rows = () => groups.querySelectorAll("li.tg-row");
  const vis = (n) => { while (n) { if (n.hidden) return false; n = n.parentNode; } return true; };
  out.layout = groups.children.map((c) => c.classList.contains("tg-sys") ? `# ${c.children[0].textContent} (${c.children[1].textContent})`
    : `${vis(c.querySelector("h3.tg-ghead")) ? c.querySelector("span.tg-gchip").textContent : "-"}: `
      + c.querySelectorAll("li.tg-row").map((li) => li.dataset.source || li.dataset.nkey).join(", "));
  out.rows = rows().map((li) => ({ key: li.dataset.key, source: li.dataset.source, nkey: li.dataset.nkey,
    name: li.querySelector("b.tg-name").textContent, title: li.querySelector("b.tg-name").title,
    main: vis(li.querySelector('button[data-field="main"]')), record: vis(li.querySelector('button[data-field="record"]')),
    tier: vis(li.querySelector("select.tg-tier")), pen: vis(li.querySelector("button.tg-ren")) }));
  const row = (src) => rows().find((li) => li.dataset.source === src || li.dataset.nkey === src);
  // the switches: by the source
  asked.length = 0;
  row("p25:westchester:307").querySelector('button[data-field="main"]').click();
  await tick(); await tick();
  row("p25:east:307").querySelector('button[data-field="record"]').click();      // (hidden: another system's)
  await tick(); await tick();
  out.posted = asked.filter(([u]) => u === "/api/talkgroup").map(([, b]) => b);
  out.pending = [...st.pending.keys()];
  // a section's switch: the county's section only
  asked.length = 0;
  groups.querySelectorAll("button.gsw").filter(vis)[0].click();
  await tick(); await tick();
  out.group = asked.filter(([u]) => u === "/api/talkgroup-group").map(([, b]) => b);
  out.groupPending = [...st.pending.keys()].sort();
  // rename: the pencil, a name, Enter
  asked.length = 0;
  row("p25:mrrs:9211").querySelector("button.tg-ren").click();
  let li = row("p25:mrrs:9211");
  const inp = li.querySelector("input.tg-rename");
  out.editing = { input: vis(inp), name: vis(li.querySelector("b.tg-name")), value: inp.value, placeholder: inp.placeholder };
  inp.value = "  PD   North  ";
  inp.fire("keydown", { key: "Enter" });
  await tick(); await tick();
  li = row("p25:mrrs:9211");
  out.renamed = { posted: asked.filter(([u]) => u === "name").map(([, b]) => b),
                  name: li.querySelector("b.tg-name").textContent, title: li.querySelector("b.tg-name").title,
                  input: vis(li.querySelector("input.tg-rename")) };
  // blank: its own name again; Escape: nothing sent
  asked.length = 0;
  row("fm:FG1").querySelector("button.tg-ren").click();
  li = row("fm:FG1");
  li.querySelector("input.tg-rename").fire("keydown", { key: "Escape" });
  await tick();
  out.escape = asked.length;
  row("fm:FG1").querySelector("button.tg-ren").click();
  li = row("fm:FG1");
  li.querySelector("input.tg-rename").value = "";
  li.querySelector("input.tg-rename").fire("blur");
  await tick(); await tick();
  out.blank = { posted: asked.filter(([u]) => u === "name").map(([, b]) => b),
                name: row("fm:FG1").querySelector("b.tg-name").textContent };
  // the filter finds a channel by its display name and by its system
  st.n.filter.value = "pd north";
  RadioControls._tg.render();
  out.filtered = rows().map((li) => li.dataset.source || li.dataset.nkey);
  st.n.filter.value = "mrrs";
  RadioControls._tg.render();
  out.bySystem = rows().map((li) => li.dataset.source || li.dataset.nkey);
  // a viewer (not an admin): no pencil
  st.n.filter.value = "";
  global.canDo = () => false;
  RadioControls._tg.render();
  out.viewerPens = rows().filter((li) => vis(li.querySelector("button.tg-ren"))).length;
  // the station's own page: mounted without names (no CAD there): none, no pencil
  global.canDo = () => true;
  const station = new N("div");
  RadioControls.mount(station);
  RadioControls._rx.st.state = IN.state;
  RadioControls._tg.render();
  out.stationPens = station.querySelectorAll("li.tg-row").filter((li) => vis(li.querySelector("button.tg-ren"))).length;
  out.stationNames = station.querySelectorAll("b.tg-name").map((b) => b.textContent).slice(0, 1);
  process.stdout.write(JSON.stringify(out) + "\n", () => process.exit(0));
})().catch((e) => { console.error(e.stack || String(e)); process.exit(1); });
"""


def tg_row(tg, name, group, system, tier=None, main=False, count=0, encrypted=False):
    """A row as the hub has it: the station's TalkgroupPrefs.state() (no tier_set)."""
    county = system == "westchester"
    return {"tg": tg, "name": name, "group": group, "encrypted": encrypted, "record": True, "main": main, "count": count,
            "last": 1000.0 if count else None, "dropped": 0, "system": system, "source": f"p25:{system}:{tg}",
            "key": f"tg:{tg}" if county else f"p25:{system}:{tg}", "ours": main, "service": "", "department": "",
            "tier": tier}


STATE = {
    "talkgroups": [tg_row(307, "Fire/EMS 15", "Fire/EMS", "westchester", main=True, count=4),
                   tg_row(5021, "Made-up Ops", "Fire/EMS", "westchester"),
                   tg_row(9211, "PD Dispatch", "Police", "mrrs", tier="alert", count=2),
                   tg_row(9300, "Made-up Bus", "Transit", "mrrs", tier="record"),
                   tg_row(307, "East Ops", "Fire/EMS", "east", tier="text")],
    "p25_systems": [{"name": "mrrs", "label": "MRRS", "state": "running", "jobs": [], "title": "", "unknown": []},
                    {"name": "east", "label": "East", "state": "running", "jobs": [], "title": "", "unknown": []}],
    "channels": {"fm": [{"key": "FG1", "kind": "traffic", "mhz": 453.0375}, {"key": "South 2", "kind": "paging", "mhz": 46.08},
                        {"key": "Tower", "kind": "airband", "mhz": 118.575}],
                 "prefs": {}, "aws_channels": [], "record_only": [], "aws_ok": False, "sync": {"state": "local"}},
}


@pytest.fixture(scope="module")
def page(tmp_path_factory):
    if NODE is None:
        pytest.skip("node not installed")
    from tests.test_reception_ui import DOM
    d = tmp_path_factory.mktemp("rc")
    (d / "h.js").write_text(DOM.split("const IN = JSON.parse(")[0] + HARNESS)     # the reception card's fake DOM
    (d / "in.json").write_text(json.dumps({"state": STATE, "names": {"fm:FG1": "Fireground 1"}}))
    import subprocess
    p = subprocess.run([NODE, str(d / "h.js"), str(STATIC / "cad.js"), str(STATIC / "radio-controls.js"), str(d / "in.json")],
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


def test_the_page_is_by_system_then_group(page):
    assert page["layout"] == [
        "# Analog FM (2 ch)", "-: fm:FG1, fm:South 2",
        "# Airband (1 ch)", "-: air:Tower",
        "# County P25 (2 TGs · 1 heard)", "Fire/EMS: p25:westchester:307, p25:westchester:5021",
        "# MRRS P25 (2 TGs · 1 heard)", "Police: p25:mrrs:9211", "Transit: p25:mrrs:9300",
        "# East P25 (1 TG)", "Fire/EMS: p25:east:307"]


def test_another_systems_talkgroup_has_its_tier_never_main(page):
    rows = {r["source"] or r["nkey"]: r for r in page["rows"]}
    assert len(page["rows"]) == 8 and len({r["key"] for r in page["rows"]}) == 8      # 307 twice: two rows
    for src in ("p25:mrrs:9211", "p25:mrrs:9300", "p25:east:307"):
        assert (rows[src]["tier"], rows[src]["main"], rows[src]["record"]) == (True, False, False), src
    for src in ("p25:westchester:307", "p25:westchester:5021"):
        assert (rows[src]["tier"], rows[src]["main"], rows[src]["record"]) == (False, True, True), src
    assert not rows["fm:FG1"]["main"] and rows["fm:FG1"]["record"]


def test_a_switch_is_sent_by_its_source_and_affects_only_that_talkgroup(page):
    # county 307's Main: its source (the server sends the county's the old way); east 307's hidden Record: nothing
    assert page["posted"] == [{"source": "p25:westchester:307", "main": False}]
    assert page["pending"] == ["p25:westchester:307"]
    # the county section's Record: the county's rows only, never east's Fire/EMS
    assert page["group"] == [{"group": "Fire/EMS", "record": False}]
    assert page["groupPending"] == ["p25:westchester:307", "p25:westchester:5021"]


def test_rename_in_place_shows_in_both_places_with_the_original_in_the_hover(page):
    rows = {r["source"] or r["nkey"]: r for r in page["rows"]}
    assert (rows["fm:FG1"]["name"], rows["fm:FG1"]["title"]) == ("Fireground 1", "FG1 · 453.0375 MHz")   # the rail's name
    assert (rows["p25:mrrs:9211"]["name"], rows["p25:mrrs:9211"]["title"]) == ("PD Dispatch", "PD Dispatch · TG 9211")
    assert all(r["pen"] for r in page["rows"])
    assert page["editing"] == {"input": True, "name": False, "value": "", "placeholder": "PD Dispatch"}
    assert page["renamed"] == {"posted": [{"key": "p25:mrrs:9211", "name": "PD North"}],
                               "name": "PD North", "title": "PD Dispatch · TG 9211", "input": False}
    assert page["escape"] == 0
    assert page["blank"] == {"posted": [{"key": "fm:FG1", "name": ""}], "name": "FG1"}
    assert page["filtered"] == ["p25:mrrs:9211"] and page["bySystem"] == ["p25:mrrs:9211", "p25:mrrs:9300"]
    assert page["viewerPens"] == 0
    assert page["stationPens"] == 0 and page["stationNames"] == ["FG1"]


def test_the_settings_page_hands_the_names_store_to_the_channels_card():
    js = (STATIC / "settings.js").read_text()
    assert "RadioControls.mount(rc, { reception: true, tab: S.rtab, names: S_RC_NAMES, services: S_RC_SERVICES," in js
    assert '"/api/cad/settings/radio_folders", { action: "name", key, name' in js and '"/api/cad/comms/sources"' in js
    rc = (STATIC / "radio-controls.js").read_text()
    assert "/api/cad/comms/sources" not in rc and "radio_folders\"" not in rc        # never from the station's page
