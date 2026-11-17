"""A source's service chosen by the admins (fire | police | ems | transit | air | other, "automatic": none chosen): the
settings section radio_services, over the talkgroups file's "Service" column and the group's words
(sizeup/sourcekeys.py). Set from Settings > Radios > Channels and the radio rail's Edit, admins only, audited as Radios.

    the CAD of record    settings radio_services -> sourcekeys.set_service_overrides: every row recorded from then on
                         (Comms.record, App._row_source), the Radio page's tree (Cad.comms_sources: service, and
                         service_auto for the "Auto (...)" choice)
    re-tag               a change re-tags that source's rows of the last RETAG_DAYS, in batches, off the request's
                         thread; the count audited; back to automatic recomputes them
    the station          the hub's answer to POST /api/station/state carries the choices; the station tags its rows and
                         picks its speech-to-text profile by them, keeps them beside the outbox, and never takes its own
                         CAD's over them

No radios, no network. Every talkgroup, name and number here is invented, or already a test's (tests/test_p25_systems.py)."""

from __future__ import annotations

import json
import time

import pytest

from sizeup import sourcekeys
from sizeup.cad import service as service_mod
from sizeup.cad.api import CadApi, needs
from sizeup.cad.store import CadError
from sizeup.cloud.access import Identity
from tests.test_cad import make_cad
from tests.test_p25_systems import PD, a_call, rows, two, wait  # noqa: F401 (fixture)
from tests.test_radios import Clock, cad as clocked_cad, tx

DAY = 86400
MINE = f"p25:mrrs:{PD}"              # another system's talkgroup (its file: Police)
SAME = f"p25:westchester:{PD}"       # the county's talkgroup with the same number (invented)


def post(cad, **body):
    return cad.settings_post("radio_services", {"operator": "Lt. Test", **body})


def p25_row(tid, start, tg, system, service, group="Brigade"):
    return dict(tx(tid, start, source="Fire Brigade", tg=tg), system=system, source_key=sourcekeys.p25(system, tg),
                group=group, service=service, tier="record")


def services(cad, key):
    return [r["service"] for r in cad.store.q("SELECT service FROM radio_traffic WHERE source = ? ORDER BY start", (key,))]


# ---- the rule ------------------------------------------------------------------------------------------------------


def test_the_admins_service_wins_over_the_file_column_and_the_words():
    # automatic: the file's column first, else the words
    assert sourcekeys.service("Fire Brigade", "transit") == "transit"
    assert sourcekeys.service("Fire Brigade") == "fire"
    sourcekeys.set_service_overrides({MINE: "fire", "tg:307": "ems", "fm:FG1": "other", "air:Tower": "nonsense"})
    assert sourcekeys.service_overrides() == {"fm:FG1": "other", MINE: "fire", "p25:westchester:307": "ems"}
    assert sourcekeys.service("Fire Brigade", "transit", key=MINE) == "fire"           # over the column
    assert sourcekeys.service("County Police", key="p25:westchester:307") == "ems"     # over the words
    assert sourcekeys.service(kind="traffic", key="fm:FG1") == "other"                  # over the kind
    assert sourcekeys.overridden("tg:307", "fire") == "ems"                             # the older key is the county's
    assert sourcekeys.overridden("air:Tower", "air") == "air" and sourcekeys.overridden("", "x") == "x"
    # the same number on another system is another source
    assert sourcekeys.service("Police", "police", key=SAME) == "police"


def test_a_station_takes_the_hubs_and_never_its_own_cads_after():
    sourcekeys.set_service_overrides({MINE: "fire"}, hub=True)
    sourcekeys.set_service_overrides({})                         # the station's own CAD at its start: not over the hub's
    assert sourcekeys.service_overrides() == {MINE: "fire"}
    sourcekeys.set_service_overrides({}, hub=True)               # the hub put it back
    assert sourcekeys.service_overrides() == {}


# ---- the setting ---------------------------------------------------------------------------------------------------


def test_an_admin_setting_per_system_audited_and_exported(tmp_path):
    cad = make_cad(tmp_path)
    cad.comms.context_broadcast = True                 # as on the hub behind Access: every reader is signed in (#419)
    assert needs("/api/cad/settings/radio_services") == "admin"
    assert cad.state()["radio_services"] == ""
    out = post(cad, action="set", key=MINE, service="fire")
    assert out["radio_services"] == {MINE: "fire"} and cad.radio_services == {MINE: "fire"}
    rev = cad.state()["radio_services"]
    assert rev and f".{rev}." in cad.comms_sources()["rev"]          # both pages ask again when it moves
    # the same number on the county: its own choice, the other kept
    post(cad, action="set", key=f"tg:{PD}", service="other")
    assert cad.radio_services == {MINE: "fire", SAME: "other"}
    post(cad, action="set", key=SAME, service="automatic")
    assert cad.radio_services == {MINE: "fire"} and sourcekeys.service_overrides() == {MINE: "fire"}
    a = [(r["key"], r["old"], r["new"], r["operator"]) for r in cad.settings.audit_log("radios")
         if r["key"].startswith("radio_services/") and not r["key"].endswith("/retag")]
    assert sorted(a) == sorted([(f"radio_services/{MINE}", "automatic", "fire", "Lt. Test"),
                                (f"radio_services/{SAME}", "automatic", "other", "Lt. Test"),
                                (f"radio_services/{SAME}", "other", "automatic", "Lt. Test")])
    n = len(cad.settings.audit_log("radios"))
    post(cad, action="set", key=MINE, service="fire")                               # the same: nothing written
    assert len(cad.settings.audit_log("radios")) == n
    for body, why in (({"action": "set", "key": MINE, "service": "fyre"}, "service"),
                      ({"action": "set", "key": "9211", "service": "fire"}, "key"),
                      ({"action": "set_many", "services": []}, "services"), ({"action": "rename"}, "action")):
        with pytest.raises(CadError, match=why):
            post(cad, **body)
    # several at once (the rail's Save)
    post(cad, action="set_many", services={"fm:FG1": "other", MINE: "automatic", "air:Tower": "air"})
    assert cad.radio_services == {"air:Tower": "air", "fm:FG1": "other"}
    # exported, checked again on the way in, an import brings them back
    exp = cad.settings_export()
    assert exp["overrides"]["radio_services"] == {"air:Tower": "air", "fm:FG1": "other"}
    bad = json.loads(json.dumps(exp))
    bad["overrides"]["radio_services"] = {"tg:307": "fire"}                          # never the older key
    with pytest.raises(CadError, match="radio_services"):
        cad.settings_post("import", {"action": "dry_run", "settings": bad, "operator": "x"})
    bad["overrides"]["radio_services"] = {"fm:FG1": "rail"}
    with pytest.raises(CadError, match="radio_services"):
        cad.settings_post("import", {"action": "dry_run", "settings": bad, "operator": "x"})
    post(cad, action="set_many", services={"fm:FG1": "automatic", "air:Tower": "automatic"})
    assert cad.radio_services == {}
    cad.settings_post("import", {"action": "apply", "settings": exp, "operator": "Lt. Test"})
    assert cad.radio_services == {"air:Tower": "air", "fm:FG1": "other"}
    # a CAD started again reads them back
    again = make_cad(tmp_path)
    assert again.radio_services == {"air:Tower": "air", "fm:FG1": "other"}


def test_only_admins_and_another_systems_only_signed_in(tmp_path):
    cad = make_cad(tmp_path)                               # all-in-one, nobody verified: no context at all
    with pytest.raises(CadError, match="another system"):
        post(cad, action="set", key=MINE, service="fire")
    post(cad, action="set", key="fm:FG1", service="other")
    # through the API on a hub with roles: a dispatcher is refused, an admin is not (the same role as a rename)
    from tests.test_roles import CASEY, JORDAN, people
    people(cad)
    api = CadApi(cad)
    assert needs("/api/cad/settings/radio_services") == needs("/api/cad/settings/radio_folders") == "admin"
    body = json.dumps({"action": "set", "key": "fm:FG1", "service": "fire"}).encode()
    code, raw, _ = api.post("/api/cad/settings/radio_services", body, Identity(email=JORDAN))
    assert code == 403 and json.loads(raw)["error"] == "Admins only (you: dispatcher)"
    assert cad.radio_services == {"fm:FG1": "other"}
    code, raw, _ = api.post("/api/cad/settings/radio_services", body, Identity(email=CASEY))
    assert code == 200 and cad.radio_services == {"fm:FG1": "fire"}
    # a reader who may not see another system is told none of its choices
    cad.comms.context_broadcast = True
    post(cad, action="set", key=MINE, service="fire")
    cad.comms.context_broadcast = False
    code, raw, _ = api.get("/api/cad/comms/sources", "", None)
    assert json.loads(raw)["services"] == {"fm:FG1": "fire"}


# ---- the rows ------------------------------------------------------------------------------------------------------


def test_new_rows_and_the_tree_take_the_admins_service(tmp_path):
    clock = Clock()
    cad = clocked_cad(tmp_path, clock)
    cad.comms.context_broadcast = True
    cad.source_systems = lambda: {"westchester": "County", "mrrs": "MRRS"}
    # what the station lists: the file says transit for the brigade's talkgroup
    cad.source_rows = lambda: [{"source": MINE, "system": "mrrs", "name": "Fire Brigade", "group": "Brigade",
                                "service": "transit", "service_auto": "transit", "tier": "record", "last": clock.t}]
    cad.record_traffic(p25_row("b-1", clock.t - 60, PD, "mrrs", "transit"))
    src = {s["key"]: s for s in cad.comms_sources()["sources"]}
    assert (src[MINE]["service"], src[MINE]["service_auto"]) == ("transit", "transit")
    assert [r["id"] for r in cad.comms_query({"view": ["transit"]})["rows"]] == ["b-1"]
    post(cad, action="set", key=MINE, service="fire")
    cad.retag_thread.join(10)
    # a station that hasn't the hub's choice yet still sends transit: the row is fire
    cad.record_traffic(p25_row("b-2", clock.t - 30, PD, "mrrs", "transit"))
    assert services(cad, MINE) == ["fire", "fire"]
    src = {s["key"]: s for s in cad.comms_sources()["sources"]}
    assert (src[MINE]["service"], src[MINE]["service_auto"]) == ("fire", "transit")
    assert cad.comms_sources()["services"] == {MINE: "fire"}
    # the views and filters: in Fire now, not in Transit
    assert [r["id"] for r in cad.comms_query({"view": ["transit"]})["rows"]] == []
    assert {r["id"] for r in cad.comms_query({"service": ["fire"]})["rows"]} >= {"b-1", "b-2"}
    # an FM channel too: its kind says fire, the admins say other
    post(cad, action="set", key="fm:FG1", service="other")
    cad.retag_thread.join(10)
    cad.record_traffic(tx("fg-1", clock.t - 10))
    assert services(cad, "fm:FG1") == ["other"]


def test_a_change_retags_the_last_30_days_in_batches_and_back_to_automatic(tmp_path, monkeypatch):
    clock = Clock()
    cad = clocked_cad(tmp_path, clock)
    cad.comms.context_broadcast = True
    cad.source_rows = lambda: [{"source": MINE, "system": "mrrs", "group": "Brigade", "service": "transit",
                                "service_auto": "transit"}]
    old = clock.t - (service_mod.RETAG_DAYS + 1) * DAY
    cad.record_traffic(p25_row("b-old", old, PD, "mrrs", "transit"))
    for i in range(7):
        cad.record_traffic(p25_row(f"b-{i}", clock.t - i * DAY, PD, "mrrs", "transit"))
    cad.record_traffic(p25_row("same-1", clock.t - 5, PD, "westchester", "police", group="Police"))   # the county's PD
    monkeypatch.setattr(type(cad.comms), "retag", lambda self, s, v, since, batch=3, pause=0.0,
                        _real=type(cad.comms).retag: _real(self, s, v, since, batch, pause))         # 3 a batch
    batches = []
    real_tx = cad.store.tx
    cad.store.tx = lambda: (batches.append(1), real_tx())[1]
    rev = cad.comms.rev
    post(cad, action="set", key=MINE, service="fire")
    cad.retag_thread.join(10)
    assert services(cad, MINE) == ["transit"] + ["fire"] * 7                  # older than 30 days: as it was
    assert services(cad, SAME) == ["police"]                                  # the same number elsewhere: untouched
    assert cad.comms.rev > rev and len(batches) >= 3
    (a,) = [r for r in cad.settings.audit_log("radios") if r["key"] == f"radio_services/{MINE}/retag"]
    assert json.loads(a["new"]) == {"service": "fire", "rows": 7, "days": 30} and a["operator"] == "Lt. Test"
    # back to automatic: recomputed (the station's row: its file's transit)
    post(cad, action="set", key=MINE, service="automatic")
    cad.retag_thread.join(10)
    assert services(cad, MINE) == ["transit"] * 8
    got = [json.loads(r["new"]) for r in cad.settings.audit_log("radios") if r["key"] == f"radio_services/{MINE}/retag"]
    assert got[0] == {"service": "transit", "rows": 7, "days": 30}


def pending(cad):
    return [r["key"] for r in cad.store.q("SELECT key FROM radio_retag_pending ORDER BY key")]


def test_retag_runs_off_the_request_is_marked_with_the_setting_and_resumed_by_the_next_leader(tmp_path):
    clock = Clock()
    cad = clocked_cad(tmp_path, clock)
    cad.comms.context_broadcast = True
    cad.source_rows = lambda: [{"source": MINE, "service": "transit", "service_auto": "transit"}]
    cad.record_traffic(p25_row("b-1", clock.t - 60, PD, "mrrs", "transit"))
    # the setting and its mark in one transaction; the process gone before its re-tag ran
    cad.retag_services = lambda *a, **k: None
    post(cad, action="set", key=MINE, service="fire")
    assert services(cad, MINE) == ["transit"] and pending(cad) == [MINE]
    # a task that lost the lock leaves the mark
    del cad.retag_services
    cad.may_write = lambda: False
    cad.retag_services(wait=True)
    assert services(cad, MINE) == ["transit"] and pending(cad) == [MINE]
    assert not [r for r in cad.settings.audit_log("radios") if r["key"].endswith("/retag")]
    # the next start of the CAD of record (a restart, a task that took over): what is marked runs, then the mark goes
    again = clocked_cad(tmp_path, clock)
    again.comms.context_broadcast = True
    again.source_rows = cad.source_rows
    assert again.radio_services == {MINE: "fire"}
    again.retag_resume().join(10)
    assert services(again, MINE) == ["fire"] and pending(again) == []
    (a,) = [r for r in again.settings.audit_log("radios") if r["key"] == f"radio_services/{MINE}/retag"]
    assert json.loads(a["new"])["rows"] == 1 and a["operator"] == "Lt. Test"
    assert again.retag_resume() is None                               # nothing marked: nothing started
    # off the request's thread
    started = []
    again.comms.retag = lambda *a, **k: (started.append(1), time.sleep(0.3), 0)[2]
    t0 = time.monotonic()
    post(again, action="set", key=MINE, service="ems")
    assert time.monotonic() - t0 < 0.3                   # the request didn't wait for it
    again.retag_thread.join(10)
    assert started == [1] and pending(again) == []


def test_back_to_automatic_waits_for_the_stations_talkgroups_never_guesses(tmp_path):
    """A hub just started (or its station away) has no talkgroup rows: the words of "Brigade" would say fire, the
    station's file says transit. The re-tag waits, marked, until the rows come; the tree shows no Auto label."""
    clock = Clock()
    cad = clocked_cad(tmp_path, clock)
    cad.comms.context_broadcast = True
    cad.source_systems = lambda: {"westchester": "County", "mrrs": "MRRS"}
    rows: list[dict] = []
    cad.source_rows = lambda: rows
    logs = []
    cad.log = logs.append
    cad.record_traffic(p25_row("b-1", clock.t - 60, PD, "mrrs", "transit"))
    post(cad, action="set", key=MINE, service="ems")
    cad.retag_thread.join(10)
    assert services(cad, MINE) == ["ems"]                     # a choice needs no automatic service
    post(cad, action="set", key=MINE, service="automatic")
    cad.retag_thread.join(10)
    assert services(cad, MINE) == ["ems"] and pending(cad) == [MINE]
    assert any("waits for the station's talkgroups" in m for m in logs)
    src = {s["key"]: s for s in cad.comms_sources()["sources"]}
    assert src[MINE]["service_auto"] == ""                    # "Auto", no guess
    assert cad.retag_resume(rows_came=False) is not None      # (a start: tried again, still waits)
    cad.retag_thread.join(10)
    assert pending(cad) == [MINE]
    # the station's rows come (the hub's _state): what waited runs
    rows.append({"source": MINE, "system": "mrrs", "group": "Brigade", "service": "transit", "service_auto": "transit"})
    cad.retag_resume(rows_came=True).join(10)
    assert services(cad, MINE) == ["transit"] and pending(cad) == []
    assert cad.retag_resume(rows_came=True) is None           # nothing waited since


def test_an_import_retags_the_rows_and_a_file_without_choices_keeps_them(tmp_path):
    clock = Clock()
    cad = clocked_cad(tmp_path, clock)
    cad.comms.context_broadcast = True
    cad.source_rows = lambda: [{"source": MINE, "service": "transit", "service_auto": "transit"}]
    cad.record_traffic(p25_row("b-1", clock.t - 60, PD, "mrrs", "transit"))
    old = cad.settings_export()                                     # (no radio_services: a file from before them)
    assert "radio_services" not in old["overrides"]
    post(cad, action="set", key=MINE, service="fire")
    cad.retag_thread.join(10)
    exp = cad.settings_export()
    cad.settings_post("import", {"action": "apply", "settings": old, "operator": "Lt. Test"})
    assert cad.radio_services == {MINE: "fire"} and services(cad, MINE) == ["fire"]     # kept, not cleared
    # a file that says otherwise: the choice and the rows follow it
    other = json.loads(json.dumps(exp))
    other["overrides"]["radio_services"] = {MINE: "police"}
    cad.settings_post("import", {"action": "apply", "settings": other, "operator": "Lt. Test"})
    cad.retag_thread.join(10)
    assert cad.radio_services == {MINE: "police"} and services(cad, MINE) == ["police"] and pending(cad) == []


def test_another_systems_retag_tells_only_readers_who_may_see_it(tmp_path):
    clock = Clock()
    cad = clocked_cad(tmp_path, clock)
    cad.comms.context_broadcast = True
    cad.record_traffic(p25_row("b-1", clock.t - 60, PD, "mrrs", "transit"))
    ours, sees = cad.comms.rev_for(False), cad.comms.rev_for(True)
    assert cad.comms.retag(MINE, "fire", 0) == 1
    assert cad.comms.rev_for(False) == ours and cad.comms.rev_for(True) != sees
    assert cad.comms.retag(MINE, "fire", 0) == 0                                # nothing differs: nothing written


# ---- the station and the hub agree ---------------------------------------------------------------------------------


def test_the_station_tags_by_the_hubs_choice_and_its_stt_profile_follows(two):
    app, _said, _logs = two
    sourcekeys.set_service_overrides({MINE: "ems"}, hub=True)            # what the hub's answer carried
    rt = app.systems["mrrs"]
    app._call_system(rt, a_call(app, "1-call_1", PD, "mrrs", label="PD Dispatch"))
    assert wait(lambda: rows(app).get(f"{PD}-1-call_1", {}).get("status") not in (None, "pending"))
    assert rows(app)[f"{PD}-1-call_1"]["service"] == "ems"
    t = next(t for t in app.store.loose if t.id == f"{PD}-1-call_1")
    assert t.service == "ems"                                               # (its speech-to-text profile's service)
    tg = {r["source"]: r for r in app.talkgroups_state()}
    assert (tg[MINE]["service"], tg[MINE]["service_auto"]) == ("ems", "police")
    assert (tg["p25:westchester:307"]["service"], tg["p25:westchester:307"]["service_auto"]) == ("fire", "fire")


def test_the_hub_answers_with_its_choices_and_the_station_keeps_them(tmp_path):
    from sizeup.cloud.hub import HubApp
    from sizeup.cloud.station import StationLink
    from tests.test_cloud import hub_cfg
    hub = HubApp(hub_cfg(tmp_path / "hub"))
    hub.cad.comms.context_broadcast = True
    hub.cad.settings_post("radio_services", {"action": "set", "key": MINE, "service": "fire", "operator": "Lt. Test"})
    code, raw, _ = hub.hub._state(json.dumps({"at": 0}).encode())
    answer = json.loads(raw)
    assert code == 200 and answer["services"] == {MINE: "fire"}
    # the station: the answer's choices, kept beside its outbox, read back at the next start
    sourcekeys._service_over, sourcekeys._service_from_hub = {}, False      # (another process)
    link = StationLink.__new__(StationLink)
    link.log = lambda m: None
    link._services_file = tmp_path / "hub_services.json"
    link._set_services(answer["services"])
    assert sourcekeys.service_overrides() == {MINE: "fire"} and sourcekeys._service_from_hub
    assert json.loads(link._services_file.read_text()) == {MINE: "fire"}
    link._set_services(None)                                                # a hub from before this: kept
    assert sourcekeys.service_overrides() == {MINE: "fire"}
    sourcekeys._service_over, sourcekeys._service_from_hub = {}, False
    link._set_services(json.loads(link._services_file.read_text()), keep=False)
    assert sourcekeys.service_overrides() == {MINE: "fire"}
    src = (sourcekeys.__file__.replace("sourcekeys.py", "cloud/station.py"))
    s = open(src).read()
    assert 'with_name("hub_services.json")' in s and 'self._set_services(r.get("services"))' in s


def test_an_older_stations_row_is_tagged_by_the_hubs_choice(tmp_path):
    from sizeup.cloud.hub import HubApp
    from tests.test_cloud import hub_cfg
    hub = HubApp(hub_cfg(tmp_path))
    hub.cad.comms.context_broadcast = True
    hub.cad.settings_post("radio_services", {"action": "set", "key": MINE, "service": "fire", "operator": "Lt. Test"})
    from sizeup.incidents import Traffic
    t = Traffic(f"{PD}-9-call_9", "Fire Brigade", time.time() - 5, time.time() - 3, "", talkgroup=PD)
    t.system, t.source_key, t.group, t.service, t.tier = "mrrs", MINE, "Brigade", "transit", "record"
    assert hub._row_source(t)["service"] == "fire"


# ---- Settings > Radios > Channels (node) ---------------------------------------------------------------------------

RC_SCENARIO = r"""
const [CADJS, RC, INF] = process.argv.slice(2);
const cadjs = fs.readFileSync(CADJS, "utf8");
eval(cadjs.slice(cadjs.indexOf("function syncList("), cadjs.indexOf("\nfunction flash(")).replace("function syncList(", "global.syncList = function ("));
Object.defineProperty(N.prototype, "options", { get() { return this.children; } });
const _el = global.el;
global.el = (...a) => { const e = _el(...a); e.style.setProperty = () => {}; return e; };
const IN = JSON.parse(fs.readFileSync(INF, "utf8"));
const asked = [];
global.fetch = async () => ({ ok: false, status: 503, headers: { get: () => "" }, json: async () => ({}) });
let services = { "p25:mrrs:9300": "fire" }, refuse = false;
const SERVICES = {                                  // settings.js S_RC_SERVICES, its hub a stand-in
  load: async () => ({ ...services }),
  save: async (key, service) => {
    asked.push({ key, service });
    if (refuse) throw new Error("Admins only");
    services = { ...services };
    if (service === "automatic") delete services[key]; else services[key] = service;
    return { ...services };
  },
};
const NAMES = { load: async () => ({ rev: "", folders: [], names: {} }), save: async () => ({ rev: "", folders: [], names: {} }) };
eval(fs.readFileSync(RC, "utf8"));
const tick = () => new Promise((ok) => setImmediate(ok));
(async () => {
  const out = {};
  const box = new N("div");
  RadioControls.mount(box, { reception: true, tab: "channels", names: NAMES, services: SERVICES });
  await tick(); await tick();
  const st = RadioControls._rx.st;
  st.state = IN.state;
  RadioControls._tg.render();
  const groups = box.querySelector("div.tg-groups");
  const vis = (n) => { while (n) { if (n.hidden) return false; n = n.parentNode; } return true; };
  const row = (k) => groups.querySelectorAll("li.tg-row").find((li) => li.dataset.nkey === k);
  const sel = (k) => row(k).querySelector("select.tg-svc");
  const look = (k) => ({ shown: vis(sel(k)), value: sel(k).value, auto: sel(k).options[0].textContent, title: sel(k).title,
                         labels: sel(k).options.map((o) => o.textContent) });
  out.before = Object.fromEntries(["p25:mrrs:9300", "p25:mrrs:9211", "p25:westchester:307", "fm:FG1", "air:Tower"].map((k) => [k, look(k)]));
  // beside the pencil
  const ids = row("p25:mrrs:9211").querySelector("div.tg-id").children.map((c) => c.className.split(" ").find((x) => x.startsWith("tg-")) || "");
  out.order = ids;
  // chosen: Fire for the brigade's talkgroup (its file says transit), then back to Auto
  const pick = async (k, v) => { const s = sel(k); s.value = v; s.fire("change", { bubbles: true }); await tick(); await tick(); };
  await pick("p25:mrrs:9211", "other");
  out.set = { asked: asked.slice(), value: sel("p25:mrrs:9211").value, services: { ...st.services } };
  asked.length = 0;
  await pick("p25:mrrs:9300", "");
  out.auto = { asked: asked.slice(), value: sel("p25:mrrs:9300").value };
  // refused: said, and the select back to what is saved
  refuse = true; asked.length = 0;
  await pick("fm:FG1", "police");
  out.refused = { error: st.error, value: sel("fm:FG1").value };
  refuse = false;
  // a viewer: no select; the station's own page (no CAD): none either
  global.canDo = () => false;
  RadioControls._tg.render();
  out.viewer = groups.querySelectorAll("select.tg-svc").filter(vis).length;
  global.canDo = () => true;
  const station = new N("div");
  RadioControls.mount(station);
  RadioControls._rx.st.state = IN.state;
  RadioControls._tg.render();
  out.station = station.querySelectorAll("select.tg-svc").filter(vis).length;
  process.stdout.write(JSON.stringify(out) + "\n", () => process.exit(0));
})().catch((e) => { console.error(e.stack || String(e)); process.exit(1); });
"""


@pytest.fixture(scope="module")
def channels_page(tmp_path_factory):
    from tests.test_channels_by_system import STATE, tg_row
    from tests.test_radio_emergency import NODE, STATIC
    from tests.test_reception_ui import DOM
    if NODE is None:
        pytest.skip("node not installed")
    state = json.loads(json.dumps(STATE))
    state["talkgroups"][2]["service"] = state["talkgroups"][2]["service_auto"] = "police"        # PD Dispatch
    brigade = tg_row(9300, "Made-up Brigade", "Brigade", "mrrs", tier="record")
    state["talkgroups"][3] = dict(brigade, service="fire", service_auto="transit")             # the file says transit
    state["talkgroups"][0]["service"] = "fire"                                                 # a station before this
    d = tmp_path_factory.mktemp("svc")
    (d / "h.js").write_text(DOM.split("const IN = JSON.parse(")[0] + RC_SCENARIO)
    (d / "in.json").write_text(json.dumps({"state": state}))
    import subprocess
    p = subprocess.run([NODE, str(d / "h.js"), str(STATIC / "cad.js"), str(STATIC / "radio-controls.js"), str(d / "in.json")],
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


def test_the_channels_page_has_a_service_select_beside_the_pencil(channels_page):
    b = channels_page["before"]
    assert b["p25:mrrs:9300"]["shown"] and b["p25:mrrs:9300"]["value"] == "fire"
    assert b["p25:mrrs:9300"]["auto"] == "Auto (Transit)"                       # automatic: what its file says
    assert b["p25:mrrs:9211"] | {"title": ""} == {"shown": True, "value": "", "auto": "Auto (Police)", "title": "",
                                                    "labels": ["Auto (Police)", "Fire", "Police", "EMS", "Transit", "Air", "Other"]}
    assert b["p25:westchester:307"]["auto"] == "Auto (Fire)"                     # a station before this: its service
    assert (b["fm:FG1"]["auto"], b["air:Tower"]["auto"]) == ("Auto (Fire)", "Auto (Air)")
    # terse: the explanation only in the hover
    assert b["p25:mrrs:9211"]["title"].startswith("Service: ") and "30 days" in b["p25:mrrs:9211"]["title"]
    o = channels_page["order"]
    assert o.index("tg-svc") == o.index("tg-ren") + 1


def test_the_channels_page_sends_the_source_key_and_automatic(channels_page):
    assert channels_page["set"] == {"asked": [{"key": "p25:mrrs:9211", "service": "other"}], "value": "other",
                                    "services": {"p25:mrrs:9211": "other", "p25:mrrs:9300": "fire"}}
    assert channels_page["auto"] == {"asked": [{"key": "p25:mrrs:9300", "service": "automatic"}], "value": ""}
    assert channels_page["refused"]["value"] == "" and "Admins only" in channels_page["refused"]["error"]
    assert channels_page["viewer"] == 0 and channels_page["station"] == 0


def test_the_settings_page_hands_the_services_store_to_the_channels_card():
    from tests.test_radio_emergency import STATIC
    js = (STATIC / "settings.js").read_text()
    assert "RadioControls.mount(rc, { reception: true, tab: S.rtab, names: S_RC_NAMES, services: S_RC_SERVICES })" in js
    assert '"/api/cad/settings/radio_services", { action: "set", key, service' in js
    rc = (STATIC / "radio-controls.js").read_text()
    assert "radio_services\"" not in rc                                          # never from the station's page


# ---- the radio rail's Edit (node) ----------------------------------------------------------------------------------

NAV_SCENARIO = r"""
(async () => {
  const posted = [];
  let current = null, services = { "p25:metro:9100": "fire" };
  const SRC2 = SRC.map((s) => (s.key === "p25:metro:9100" ? { ...s, service: "fire", service_auto: "transit" } : s));
  const real = global.api;
  global.api = (method, path, body) => {
    if (method === "POST" && path.endsWith("/radio_services")) {
      posted.push([path, body]);
      for (const [k, v] of Object.entries(body.services)) { if (v === "automatic") delete services[k]; else services[k] = v; }
      return Promise.resolve({ ok: true, radio_services: { ...services } });
    }
    if (method === "POST") { posted.push([path, body]); current = body.action === "reset" ? null : { rev: "r2", folders: body.folders };
                             return Promise.resolve({ ok: true, radio_folders: current }); }
    if (path.startsWith("/api/cad/comms/sources")) { calls.push(path); return Promise.resolve({ rev: "s" + calls.length, now: NOW, minutes: 60, live: [],
      sources: SRC2, folders: current, services: { ...services } }); }
    return real(method, path, body);
  };
  global.canDo = (need) => need === "admin";
  N.open(""); await drain();
  const walk = (n) => [n, ...(n.children || []).flatMap(walk)];
  const tree = () => walk($("#rn-tree"));
  const click = (b) => $("#rn-rail").handlers.click[0]({ target: b });
  const btn = (ed) => tree().find((c) => c.dataset && c.dataset.ed === ed);
  $("#rn-edit").fire("click");
  const svc = (k) => tree().find((c) => c.dataset && c.dataset.chsvc === k);
  out.field = { rail: [svc("p25:metro:9100").value, svc("p25:metro:9100").children[0].text, svc("p25:metro:9100").title],
                pd: [svc("p25:metro:9001").value, svc("p25:metro:9001").children.map((o) => o.text)],
                rowOrder: tree().find((c) => c.className === "rn-edi" && c.dataset.k === "fm:FG1").children.map((c) => c.className) };
  const pick = (k, v) => { const n = svc(k); n.value = v; $("#rn-tree").fire("change", { target: n }); };
  pick("p25:metro:9001", "other"); pick("p25:metro:9100", ""); pick("fm:FG1", "fire"); pick("fm:FG1", "");
  const n0 = posted.length;
  click(btn("save")); await drain();
  out.saved = { posts: posted.slice(n0).map(([p, b]) => [p, b.action, b.services || null]), editing: st.edit, services: st.services };
  // nothing changed: only the folders go
  $("#rn-edit").fire("click");
  const n1 = posted.length;
  click(btn("save")); await drain();
  out.unchanged = posted.slice(n1).map(([p]) => p);
  // the push says the services moved: the tree asked again, once
  const asked0 = calls.filter((c) => c.includes("/sources")).length;
  cad.state.radio_services = "a1"; cad.hooks.forEach((h) => h()); await drain();
  cad.state.radio_services = "a2"; cad.hooks.forEach((h) => h()); await drain(); cad.hooks.forEach((h) => h()); await drain();
  out.pushed = calls.filter((c) => c.includes("/sources")).length - asked0;
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e.stack); process.exit(1); });
"""


@pytest.fixture(scope="module")
def nav(tmp_path_factory):
    import subprocess
    from tests.test_radio_emergency import NODE, STATIC
    from tests.test_radio_nav import HARNESS
    if NODE is None:
        pytest.skip("node not installed")
    head, _ = HARNESS.split("(async () => {", 1)
    h = tmp_path_factory.mktemp("svcnav") / "nav.js"
    h.write_text(head + NAV_SCENARIO)
    p = subprocess.run([NODE, str(h), str(STATIC / "radio-nav.js"), "-", "all"], capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


def test_the_rails_edit_has_each_channels_service(nav):
    value, auto, title = nav["field"]["rail"]
    assert (value, auto) == ("fire", "Auto (Transit)") and title.startswith("Service: ") and "30 days" in title
    assert nav["field"]["pd"] == ["", ["Auto (Police)", "Fire", "Police", "EMS", "Transit", "Air", "Other"]]
    assert nav["field"]["rowOrder"][:2] == ["rn-grip", "rn-edch"] and nav["field"]["rowOrder"][-1] == "rn-edsvc"   # after Move to


def test_the_rails_save_sends_only_what_changed_before_the_folders(nav):
    s = nav["saved"]
    assert s["posts"] == [["/api/cad/settings/radio_services", "set_many", {"p25:metro:9001": "other", "p25:metro:9100": "automatic"}],
                          ["/api/cad/settings/radio_folders", "reset", None]]
    assert s["editing"] is None and s["services"] == {"p25:metro:9001": "other"}
    assert nav["unchanged"] == ["/api/cad/settings/radio_folders"]
    assert nav["pushed"] >= 1


def test_a_row_queued_under_a_choice_since_put_back_is_recorded_automatic(tmp_path, two):
    """The station tags a row under the hub's choice (fire) and queues it; the admin puts the source back to automatic
    before it is delivered. The row carries service_auto (its file's), and the hub records the choice it has then."""
    from sizeup.cloud.hub import HubApp
    from tests.test_cloud import hub_cfg
    app, _said, _logs = two
    sourcekeys.set_service_overrides({MINE: "fire"}, hub=True)              # the station: the hub's choice then
    app._call_system(app.systems["mrrs"], a_call(app, "1-call_1", PD, "mrrs", label="PD Dispatch"))
    assert wait(lambda: rows(app).get(f"{PD}-1-call_1", {}).get("status") not in (None, "pending"))
    t = next(t for t in app.store.loose if t.id == f"{PD}-1-call_1")
    sent = json.loads(json.dumps(t.as_dict()))                              # the outbox's event, as it went
    assert (sent["service"], sent["service_auto"]) == ("fire", "police")
    # the hub, where the choice is gone by the time the event arrives (another process: its own map)
    sourcekeys._service_over, sourcekeys._service_from_hub = {}, False
    hub = HubApp(hub_cfg(tmp_path / "hub"))
    hub.cad.comms.context_broadcast = True
    hub.apply("traffic", sent, None)
    got = hub.cad.store.one("SELECT service FROM radio_traffic WHERE id = ?", (sent["id"],))
    assert got["service"] == "police"
    # the same row recorded again (its words re-read: Comms.record's update of a row it has): automatic still
    ht = next(x for x in hub.store.loose if x.id == sent["id"])
    hub._to_cad(ht, replace=True)
    assert hub.cad.store.one("SELECT service FROM radio_traffic WHERE id = ?", (sent["id"],))["service"] == "police"
    # and with a choice on the hub, the hub's choice
    hub.cad.settings_post("radio_services", {"action": "set", "key": MINE, "service": "ems", "operator": "Lt. Test"})
    hub.cad.retag_thread.join(10)
    hub._to_cad(ht, replace=True)
    assert hub.cad.store.one("SELECT service FROM radio_traffic WHERE id = ?", (sent["id"],))["service"] == "ems"
    # a station from before this sends its service alone (it never had a choice): taken as automatic
    old = {k: v for k, v in sent.items() if k != "service_auto"} | {"id": f"{PD}-2-call_2", "service": "police"}
    hub.apply("traffic", old, None)
    assert hub.cad.store.one("SELECT service FROM radio_traffic WHERE id = ?", (old["id"],))["service"] == "ems"
