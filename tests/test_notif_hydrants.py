"""Notifications: their sub-type and the hydrants out of service they name (sizeup/cad/notif.py, static/notif.js).

The admins' rules (Settings > Lists > Notification rules) read a Notification's remarks, feed comments and notes: the
first that matches sets its sub-type ("Notification · Hydrants OOS"); a hydrants rule's numbers are matched to the
hydrant list and proposed (Mark OOS, Link when already out of service, Confirm one found elsewhere, Add one not listed).
Applied by an admin (audited as a hydrant edit), a linked hydrant keeps its notification open until it is back in
service. The repo holds no rule: every pattern, comment, number and place here is invented ("HYD 9001, 9002 OOS"); the
hydrants are tests/test_hydrants.py's invented ones."""

from __future__ import annotations

import json
import re

import pytest

from sizeup.cad import notif as nf
from sizeup.cad.api import CadApi, needs
from sizeup.cad.store import CadError
from tests.test_cad import Clock, local
from tests.test_dispatch_modes import AutoRig
from tests.test_hydrants import DLAT, LAT0, LON0, fake_gis, get, served, by_key, wire
from tests.test_incident_sources import T0
from tests.test_roles import CASEY, JORDAN, VAL, call, people
from tests.test_cad import make_cad

OOS_RULE = {"name": "Hydrants OOS", "match": r"\bHYD(?:RANTS?)?\s*#?\s*(?P<nums>[0-9][0-9,&\s]*?)\s*(?:ARE\s+)?OOS\b",
            "kind": "HYD-OOS", "hydrants": "oos"}
IN_RULE = {"name": "Hydrants back", "match": r"\bHYD(?:RANTS?)?\s*#?\s*(?P<nums>[0-9][0-9,&\s]*?)\s*(?:BACK\s+)?IN\s+SERVICE\b",
           "kind": "HYD-IS", "hydrants": "in"}
ROAD_RULE = {"name": "Road", "match": r"\bROAD CLOSED\b", "kind": "ROAD"}


def post(cad, section, **body):
    return cad.settings_post(section, dict(body, operator=body.get("operator", "Casey <casey@example.org>")))


def rules(cad, *rs, auto_close=None):
    for r in rs:
        post(cad, nf.SECTION, action="set", rule=dict(r))
    if auto_close is not None:
        post(cad, nf.SECTION, action="auto_close", value=auto_close)


@pytest.fixture
def cad(tmp_path):
    c = make_cad(tmp_path, clock=Clock(local(2026, 11, 20, 9, 0)))
    wire(c, fake_gis())
    return c


def notification(cad, remarks="", lat=LAT0 + 0.5 * DLAT, lon=LON0, **kw):
    return cad.create({"type": "NOTIF", "address": "10 Sample St", "lat": lat, "lon": lon, "remarks": remarks,
                       "operator": "JD", **kw})["id"]


def state_inc(cad, iid):
    cad._state = None
    return next(i for i in cad.state()["incidents"] if i["id"] == iid)


def rows(cad, iid):
    return {r["number"]: r for r in state_inc(cad, iid)["notif"]["rows"]}


def texts(cad, iid):
    return [e["text"] for e in cad.store.full(iid)["events"]]


# -- the rules: validation, Settings, audit ------------------------------------------------------------------------------

@pytest.mark.parametrize("rule, err", [
    ({**OOS_RULE, "match": "HYD ("}, "match:"),                                   # not a regular expression
    ({**OOS_RULE, "match": "x*"}, "too broad"),                                   # matches empty text: everything
    ({**OOS_RULE, "name": ""}, "name:"),
    ({**OOS_RULE, "kind": "NOSUCH"}, "not a notification sub-type"),
    ({**OOS_RULE, "hydrants": "maybe"}, "hydrants:"),
    ({"name": "x", "match": "abc"}, "a sub-type, hydrants, or both"),
    ({**ROAD_RULE, "auto_apply": True}, "only for a hydrants rule"),
    ({**OOS_RULE, "numbers": "("}, "numbers:"),
    ({**OOS_RULE, "match": "A" * 400}, "at most 300"),
    ({**OOS_RULE, "match": r"HYD (\d+,?)+ OOS"}, "could take forever"),          # nested repeats: backtracking
    ({**OOS_RULE, "numbers": r"(\d+,)+\d"}, "could take forever"),
])
def test_settings_refuses_a_bad_rule(cad, rule, err):
    with pytest.raises(CadError, match=re.escape(err)):
        post(cad, nf.SECTION, action="set", rule=rule)
    assert cad.settings.section(nf.SECTION) == {}


def test_rules_are_added_edited_moved_deleted_and_audited(cad):
    out = post(cad, nf.SECTION, action="set", rule=OOS_RULE)
    post(cad, nf.SECTION, action="set", rule=ROAD_RULE)
    page = out[nf.SECTION]
    assert [r["id"] for r in page["rules"]] == ["r1"] and page["auto_close"] is False
    got = cad.settings.page()[nf.SECTION]
    assert [(r["id"], r["name"]) for r in got["rules"]] == [("r1", "Hydrants OOS"), ("r2", "Road")]
    post(cad, nf.SECTION, action="move", id="r2", dir=-1)
    assert [r["id"] for r in cad.settings.page()[nf.SECTION]["rules"]] == ["r2", "r1"]
    post(cad, nf.SECTION, action="set", rule={**ROAD_RULE, "id": "r2", "name": "Road closed"})
    post(cad, nf.SECTION, action="delete", id="r1")
    post(cad, nf.SECTION, action="auto_close", value=True)
    assert cad.notif.cfg()["auto_close"] is True and [r["name"] for r in cad.notif.cfg()["rules"]] == ["Road closed"]
    audit = cad.store.q("SELECT key, old, new, operator FROM settings_audit WHERE key LIKE 'notif_rules/%' ORDER BY id")
    assert [a["key"] for a in audit] == ["notif_rules/r1", "notif_rules/r2", "notif_rules/_order", "notif_rules/r2",
                                         "notif_rules/r1", "notif_rules/auto_close"]
    assert audit[4]["new"] in ("", None) and json.loads(audit[0]["new"])["match"] == OOS_RULE["match"]
    assert all(a["operator"].startswith("Casey") for a in audit)
    with pytest.raises(CadError, match="no rule"):
        post(cad, nf.SECTION, action="delete", id="r9")


def test_the_test_box_reads_a_text_and_writes_nothing(cad):
    rules(cad, OOS_RULE, ROAD_RULE)
    n = cad.store.one("SELECT COUNT(*) AS n FROM settings_audit")["n"]
    out = post(cad, nf.SECTION, action="test", text="ROAD CLOSED. HYD 9001, 9002 & 0042 OOS")
    assert (out["kind"], out["kind_name"], out["rule"]) == ("HYD-OOS", "Hydrants OOS", "Hydrants OOS")
    assert [h["number"] for h in out["hydrants"]] == ["9001", "9002", "0042"]
    assert cad.store.one("SELECT COUNT(*) AS n FROM settings_audit")["n"] == n


def test_a_settings_file_with_rules_is_checked(cad):
    ok = nf.check_section({"rules": [{**OOS_RULE, "id": "r1"}], "auto_close": True})
    assert ok["rules"][0]["id"] == "r1" and ok["auto_close"] is True
    for bad in ({"rules": [{**OOS_RULE}]}, {"rules": [{**OOS_RULE, "id": "r1"}, {**ROAD_RULE, "id": "r1"}]},
                {"rules": [{**OOS_RULE, "id": "r1", "match": "("}]}, {"auto_close": "yes"}, {"other": 1}):
        with pytest.raises(ValueError):
            nf.check_section(bad)
    rules(cad, OOS_RULE)
    data = cad.settings.export()
    assert data["overrides"][nf.SECTION]["rules"][0]["name"] == "Hydrants OOS"
    post(cad, "import", action="dry_run", settings=json.loads(json.dumps(data)))       # as exported: fine
    data["overrides"][nf.SECTION]["rules"][0]["match"] = "("
    with pytest.raises(CadError, match="notif_rules"):
        post(cad, "import", action="dry_run", settings=data)


def test_no_rule_in_the_repo_and_the_seeded_sub_types(cad):
    assert cad.notif.cfg()["rules"] == []                                         # the admins enter them
    assert [k["name"] for k in cad.lists.notification_kinds] == [
        "Hydrants OOS", "Hydrants back in service", "Road closure", "Water main", "Other"]
    lst = cad.settings.page()["lists"]["notification_kinds"]["items"]
    assert [x["code"] for x in lst] == ["HYD-OOS", "HYD-IS", "ROAD", "WATER-MAIN", "OTHER"]
    post(cad, "notification_kinds", action="add", code="GAS-MAIN", item={"name": "Gas main"})
    assert cad.lists.name("notification_kinds", "GAS-MAIN") == "Gas main"
    assert "notification_kinds" in cad.lists.public()


# -- reading the numbers --------------------------------------------------------------------------------------------------

def test_numbers_from_the_group_the_first_group_or_the_whole_match():
    cfg = nf.config({"rules": [{**OOS_RULE, "id": "r1"}]})
    got = nf.read(cfg, ["Water co: HYD 9001, 9002 & 0042 OOS until further notice", "hyd 9001 oos"])
    assert [(n, a) for n, a, _ in got["found"]] == [("9001", "oos"), ("9002", "oos"), ("0042", "oos")]   # once each
    assert nf.num_key("0042") == "42" and nf.num_key("p-1") == "P1"
    one = nf.config({"rules": [{"id": "r1", "name": "x", "match": r"OUT: (\S+)", "hydrants": "oos"}]})
    assert [n for n, *_ in nf.read(one, ["OUT: 77A"])["found"]] == ["77A"]
    whole = nf.config({"rules": [{"id": "r1", "name": "x", "match": r"FH-\d+", "hydrants": "oos", "numbers": r"\d+"}]})
    assert [n for n, *_ in nf.read(whole, ["FH-12 and FH-34 down"])["found"]] == ["12", "34"]
    off = nf.config({"rules": [{**OOS_RULE, "id": "r1", "on": False}]})
    assert nf.read(off, ["HYD 1 OOS"]) == {"kind": "", "rule": "", "found": []}


# -- a notification read by the rules -------------------------------------------------------------------------------------

def test_a_notification_gets_its_sub_type_and_the_numbers_proposed(cad):
    rules(cad, OOS_RULE, ROAD_RULE)
    iid = notification(cad, "Water company: HYD 9001, 9003 OOS")
    inc = state_inc(cad, iid)
    assert (inc["notif"]["kind"], inc["notif"]["kind_name"], inc["notif"]["by"]) == ("HYD-OOS", "Hydrants OOS", "rule")
    assert inc["notif"]["oos"] == 0 and not inc["notif"]["all_back"]
    r = rows(cad, iid)
    assert (r["9001"]["match"], r["9001"]["next"], r["9001"]["h"]["key"]) == ("near", "mark", "c990001")
    assert r["9003"]["h"]["dist_m"] < nf.NEAR_M
    assert "Sub-type: Hydrants OOS (Hydrants OOS)" in texts(cad, iid)
    assert "Hydrants OOS: 9001, 9003 (proposed)" in texts(cad, iid)
    # not a notification, or none of the rules: nothing
    other = cad.create({"type": "STRU", "address": "1 Sample St", "remarks": "HYD 9001 OOS", "operator": "JD"})["id"]
    assert "notif" not in state_inc(cad, other)
    plain = notification(cad, "street fair")
    assert "notif" not in state_inc(cad, plain)
    # a later note on the same notification adds what it names, once
    cad.note(iid, {"text": "also HYD 9002 OOS, and HYD 9001 OOS again", "operator": "JD"})
    assert sorted(rows(cad, iid)) == ["9001", "9002", "9003"]


def test_a_sub_type_set_by_hand_stands(cad):
    rules(cad, ROAD_RULE, OOS_RULE)
    iid = notification(cad, "ROAD CLOSED for work")
    assert state_inc(cad, iid)["notif"]["kind"] == "ROAD"
    cad.notif.set_kind(iid, {"kind": "WATER-MAIN", "operator": "JD"})
    cad.note(iid, {"text": "ROAD CLOSED still", "operator": "JD"})
    n = state_inc(cad, iid)["notif"]
    assert (n["kind"], n["kind_name"], n["by"]) == ("WATER-MAIN", "Water main", "hand")
    with pytest.raises(CadError, match="not a notification sub-type"):
        cad.notif.set_kind(iid, {"kind": "NOSUCH", "operator": "JD"})
    cad.notif.set_kind(iid, {"kind": "", "operator": "JD"})                       # back to the rules'
    assert state_inc(cad, iid)["notif"]["kind"] == "ROAD"
    stru = cad.create({"type": "STRU", "address": "1 Sample St", "operator": "JD"})["id"]
    with pytest.raises(CadError):
        cad.notif.set_kind(stru, {"kind": "ROAD", "operator": "JD"})


def test_a_county_comment_is_read_like_a_note(tmp_path):
    rig = AutoRig(tmp_path)
    wire(rig.cad, fake_gis())
    rules(rig.cad, OOS_RULE)
    rig.feed.put("A1", type="NOTIF", location="10 SAMPLE ST", units=(), times={"dispatched": T0},
                 comments=((T0, "WATER CO ADVISES HYD 9002 & 9003 OOS"),))
    rig.poll()
    iid = rig.iid()
    n = state_inc(rig.cad, iid)["notif"]
    assert n["kind"] == "HYD-OOS" and sorted(r["number"] for r in n["rows"]) == ["9002", "9003"]


# -- matching each number to the hydrant list -------------------------------------------------------------------------------

def test_already_oos_elsewhere_and_not_listed(cad):
    rules(cad, OOS_RULE)
    cad.hydrants.edit("c990002", {"status": "oos", "status_reason": "Leaking", "operator": "Casey"})   # by hand
    iid = notification(cad, "HYD 9001, 9002, 9005, 9099 OOS", lat=LAT0 + 30 * DLAT)      # by the far 9001 (c990004)
    r = rows(cad, iid)
    assert (r["9001"]["match"], r["9001"]["h"]["key"], r["9001"]["next"]) == ("near", "c990004", "mark")
    # (a) marked OOS by hand already: Link, nothing re-marked
    # (b) not near this one: the whole list searched, where it is shown, a person confirms
    assert (r["9002"]["match"], r["9002"]["next"]) == ("elsewhere", "confirm")
    c = r["9002"]["cands"][0]
    assert c["key"] == "c990002" and c["dist_m"] > 2000 and c["town"] == "Sampletown" and c["status"] == "oos"
    assert (r["9099"]["match"], r["9099"]["next"]) == ("none", "add")             # not in the hydrant list: Add
    # a number shown on the overlay counts, and the asset id
    cad.hydrants.edit("c990003", {"asset_id": "99-0077"})
    assert [it["key"] for it in cad.hydrants.by_number("990077")] == ["c990003"]
    assert [it["key"] for it in cad.hydrants.by_number("09003")] == ["c990003"]
    # confirmed: one of those offered only
    with pytest.raises(CadError, match="not one offered"):
        cad.notif.apply(iid, {"items": [{"id": r["9002"]["id"], "key": "c990003"}], "operator": "Casey"})
    with pytest.raises(CadError, match="pick the hydrant"):
        cad.notif.apply(iid, {"items": [{"id": r["9002"]["id"]}], "operator": "Casey"})
    out = cad.notif.apply(iid, {"items": [{"id": r["9002"]["id"], "key": "c990002"}], "operator": "Casey"})
    assert out["done"] == [{"id": r["9002"]["id"], "number": "9002", "key": "c990002", "did": "link"}]
    assert cad.hydrants.one("c990002")["hydrant"]["status_reason"] == "Leaking"        # linked, never re-marked


def test_apply_marks_them_oos_with_the_notification_as_the_reason_and_audits(cad):
    rules(cad, OOS_RULE)
    cad.hydrants.edit("c990002", {"status": "oos", "status_reason": "Leaking", "operator": "Casey"})
    iid = notification(cad, "HYD 9001, 9002, 9099 OOS")
    number = cad.store.incident(iid)["number"]
    out = cad.notif.apply(iid, {"operator": "Casey <casey@example.org>"})        # everything that needs no choice
    assert sorted((d["number"], d["did"]) for d in out["done"]) == [("9001", "mark"), ("9002", "link")]
    h = cad.hydrants.one("c990001")
    assert (h["hydrant"]["status"], h["hydrant"]["status_reason"]) == ("oos", f"Notification {number}")
    assert h["hydrant"]["status_since"] == "2026-11-20"
    assert json.loads(h["history"][0]["new"])["status"] == "oos" and h["history"][0]["by"] == "Casey"
    assert h["notifications"] == [{"id": iid, "number": number, "status": "pending", "since": cad.clock()}]
    assert cad.hydrants.one("c990002")["notifications"][0]["id"] == iid
    assert len(cad.hydrants.one("c990002")["history"]) == 1                       # the hand edit only
    inc = state_inc(cad, iid)
    assert inc["notif"]["oos"] == 2 and inc["notif"]["linked"] == ["c990001", "c990002"]
    assert inc["hydrants"]["linked"] == ["c990001", "c990002"]                    # (c) the call's list marks them
    ov = next(o for o in cad.state()["overview"] if o["id"] == iid)               # the wall board's Non-emergency list
    assert ov["notif"] == {"kind_name": "Hydrants OOS", "oos": 2}
    assert {r["number"]: r["state"] for r in inc["notif"]["rows"]} == {"9001": "linked", "9002": "linked", "9099": "proposed"}
    t = texts(cad, iid)
    assert "Hydrants 9001: Marked OOS" in t and "Hydrants 9002: Linked (already OOS)" in t
    doc, _ = served(cad.gis)
    assert by_key(doc)["c990001"]["properties"]["_st"] == "oos"                   # the map: the OOS mark
    assert cad.notif.apply(iid, {"operator": "Casey"}) == {"ok": True, "done": []}   # nothing left without a choice


def test_a_hydrant_marked_oos_from_the_calls_list_is_linked(cad):
    iid = notification(cad, "water main break")
    with pytest.raises(CadError, match="in service: mark it OOS first"):
        cad.notif.link(iid, {"key": "c990003", "operator": "Casey"})
    cad.hydrants.edit("c990003", {"status": "oos", "operator": "Casey"})
    cad.notif.link(iid, {"key": "c990003", "operator": "Casey"})
    cad.notif.link(iid, {"key": "c990003", "operator": "Casey"})                  # twice: once
    n = state_inc(cad, iid)["notif"]
    assert n["oos"] == 1 and n["linked"] == ["c990003"]


# -- keeping it open ------------------------------------------------------------------------------------------------------

def linked(cad, remarks="HYD 9001, 9002 OOS", **kw):
    rules(cad, OOS_RULE)
    iid = notification(cad, remarks, **kw)
    cad.notif.apply(iid, {"operator": "Casey"})
    return iid


def test_an_until_never_closes_it_while_hydrants_are_oos(cad):
    iid = linked(cad, until_at=local(2026, 11, 20, 10, 0))
    cad.clock.t = local(2026, 11, 20, 10, 0) + 5
    done = cad.expire_until()
    assert done[0]["closed"] is False and cad.store.incident(iid)["status"] == "pending"
    assert "Until 10:00 passed: open while 2 hydrants OOS" in texts(cad, iid)
    cad.clock.t += 60
    assert cad.expire_until() == []                                             # said once
    cad.notif.back(iid, {"operator": "Casey"})
    cad.clock.t += 60
    assert cad.expire_until()[0]["closed"] is True                                # then the Until closes it


def test_a_person_closing_it_confirms(cad):
    iid = linked(cad)
    with pytest.raises(CadError, match="2 hydrants still OOS on this notification: confirm to close"):
        cad.close(iid, {"disposition": "COMPLETE", "operator": "JD"})
    cad.close(iid, {"disposition": "COMPLETE", "operator": "JD", "confirm": True})
    assert cad.store.incident(iid)["status"] == "closed"


def test_back_in_service_from_the_hydrant_card_releases_it_and_it_asks(cad):
    iid = linked(cad)
    cad.hydrants.edit("c990001", {"status": "in_service", "operator": "Casey"})
    n = state_inc(cad, iid)["notif"]
    assert n["oos"] == 1 and not n["all_back"]
    assert "Hydrant 9001: back in service" in texts(cad, iid)
    cad.notif.back(iid, {"operator": "Casey"})                                    # the event's action: the rest
    n = state_inc(cad, iid)["notif"]
    assert n["oos"] == 0 and n["all_back"] is True                               # ask: Close
    assert cad.hydrants.one("c990002")["hydrant"]["status"] == "in_service"
    assert cad.store.incident(iid)["status"] == "pending"                        # auto close off: still open
    cad.close(iid, {"disposition": "COMPLETE", "operator": "JD"})                 # no confirm needed now


def test_a_csv_import_putting_it_back_releases_it(cad):
    iid = linked(cad, "HYD 9001 OOS")
    cad.hydrants.import_csv({"action": "apply", "csv": "key,status\nc990001,in_service\n", "operator": "Casey"})
    assert state_inc(cad, iid)["notif"]["oos"] == 0


def test_auto_close_when_the_last_is_back(cad):
    iid = linked(cad)
    post(cad, nf.SECTION, action="auto_close", value=True)
    cad.notif.back(iid, {"ids": [rows(cad, iid)["9001"]["id"]], "operator": "Casey"})
    assert cad.store.incident(iid)["status"] == "pending"
    cad.hydrants.edit("c990002", {"status": "in_service", "operator": "Casey"})
    inc = cad.store.incident(iid)
    assert (inc["status"], inc["disposition"]) == ("closed", "COMPLETE")
    assert any(e["operator"] == nf.AUTO for e in cad.store.full(iid)["events"] if e["kind"] == "closed")


def test_a_back_in_service_notification_proposes_and_releases_the_first(cad):
    rules(cad, OOS_RULE, IN_RULE)
    first = notification(cad, "HYD 9001, 9002 OOS")
    cad.notif.apply(first, {"operator": "Casey"})
    later = notification(cad, "HYD 9001 BACK IN SERVICE")                        # at the same place
    inc = state_inc(cad, later)
    assert inc["notif"]["kind"] == "HYD-IS"
    r = rows(cad, later)["9001"]
    assert (r["action"], r["match"], r["h"]["key"], r["next"]) == ("in", "linked", "c990001", "back")
    # one placed far off never takes the held one by itself: a person confirms (the held one offered first)
    far = notification(cad, "HYD 9002 BACK IN SERVICE", lat=LAT0 + 60 * DLAT)
    f = rows(cad, far)["9002"]
    assert (f["match"], f["next"], f["cands"][0]["key"]) == ("elsewhere", "confirm", "c990002")
    assert cad.notif.apply(far, {"operator": "Casey"}) == {"ok": True, "done": []}
    cad.notif.apply(later, {"operator": "Casey"})
    assert cad.hydrants.one("c990001")["hydrant"]["status"] == "in_service"
    assert rows(cad, later)["9001"]["state"] == "done"
    assert state_inc(cad, first)["notif"]["oos"] == 1                            # 9002 still holds the first


def test_the_county_in_service_never_closes_it_while_held(tmp_path):
    rig = AutoRig(tmp_path)
    wire(rig.cad, fake_gis())
    rules(rig.cad, OOS_RULE)
    rig.feed.put("A1", type="NOTIF", location="10 SAMPLE ST", units=("E60",), times={"dispatched": T0},
                 comments=((T0, "HYD 9001 OOS"),))
    rig.poll()
    iid = rig.iid()
    assert rig.cad.store.incident(iid)["status"] == "pending"                     # accepted (60 Control mode)
    r = rows(rig.cad, iid)["9001"]                                                # no map point: both offered
    assert r["match"] == "elsewhere" and [c["key"] for c in r["cands"]] == ["c990001", "c990004"]
    rig.cad.notif.apply(iid, {"items": [{"id": r["id"], "key": "c990001"}], "operator": "Casey"})
    rig.at(600)
    rig.feed.change("A1", times={"dispatched": T0, "in_service": T0 + 590})
    rig.poll()
    assert rig.cad.store.incident(iid)["status"] == "pending"
    assert any("kept open (1 hydrant OOS)" in t for t in texts(rig.cad, iid))
    assert rig.cad.auto.close_pending(iid) is None                               # no "close?" while held


def test_auto_apply_takes_only_the_one_near_it(cad):
    rules(cad, {**OOS_RULE, "auto_apply": True})
    iid = notification(cad, "HYD 9001, 9099 OOS")
    assert cad.hydrants.one("c990001")["hydrant"]["status"] == "oos"
    r = rows(cad, iid)
    assert r["9001"]["state"] == "linked" and r["9099"]["state"] == "proposed"
    assert cad.store.q("SELECT operator FROM settings_audit WHERE section = 'hydrants'")[0]["operator"] == "Hydrants OOS · auto"


def test_a_row_is_applied_once_and_a_set_aside_one_never(cad):
    rules(cad, OOS_RULE)
    iid = notification(cad, "HYD 9001, 9003 OOS")
    r = rows(cad, iid)
    cad.notif.apply(iid, {"items": [{"id": r["9001"]["id"]}], "operator": "Casey"})
    with pytest.raises(CadError, match="linked already"):
        cad.notif.apply(iid, {"items": [{"id": r["9001"]["id"]}], "operator": "Casey"})
    cad.notif.dismiss(iid, {"id": r["9003"]["id"], "operator": "Casey"})
    with pytest.raises(CadError, match="dismissed already"):
        cad.notif.apply(iid, {"items": [{"id": r["9003"]["id"]}], "operator": "Casey"})
    with pytest.raises(CadError, match="dismissed already"):
        cad.notif.dismiss(iid, {"id": r["9003"]["id"], "operator": "Casey"})
    assert texts(cad, iid).count("Hydrants 9001: Marked OOS") == 1
    assert cad.hydrants.one("c990003")["hydrant"]["status"] == "unknown"


def test_a_queued_notification_holding_hydrants_is_dismissed_only_when_confirmed(cad):
    rules(cad, {**OOS_RULE, "auto_apply": True})
    iid = cad.store.create_incident(type="NOTIF", address="10 Sample St", lat=LAT0, lon=LON0, remarks="HYD 9001 OOS",
                                    source="CAD feed", status="queued")
    iid = iid["id"] if isinstance(iid, dict) else iid
    cad.notif.scan(iid)
    assert cad.notif.held(iid) == 1
    with pytest.raises(CadError, match="confirm"):
        cad.dismiss(iid, {"operator": "JD"})
    cad.dismiss(iid, {"operator": "JD", "confirm": True})
    assert cad.store.incident(iid)["status"] == "dismissed"
    assert cad.hydrants.one("c990001")["notifications"][0]["status"] == "dismissed"   # still why it is out of service


def test_dismiss_sets_a_row_aside_or_unlinks(cad):
    iid = linked(cad, "HYD 9001, 9099 OOS")
    r = rows(cad, iid)
    cad.notif.dismiss(iid, {"id": r["9099"]["id"], "operator": "Casey"})
    cad.notif.dismiss(iid, {"id": r["9001"]["id"], "operator": "Casey"})
    assert state_inc(cad, iid)["notif"]["oos"] == 0
    assert cad.hydrants.one("c990001")["hydrant"]["status"] == "oos"              # the hydrant itself unchanged


# -- who may ----------------------------------------------------------------------------------------------------------------

def test_hydrant_actions_are_admins_and_the_sub_type_dispatchers(tmp_path):
    a = CadApi(make_cad(tmp_path))
    wire(a.cad, fake_gis())
    people(a.cad)
    rules(a.cad, OOS_RULE)
    iid = notification(a.cad, "HYD 9001 OOS")
    assert needs(f"/api/cad/incident/{iid}/hydrants") == "admin" and needs(f"/api/cad/incident/{iid}/notif") == "dispatch"
    for email in (JORDAN, VAL):
        assert call(a, f"/api/cad/incident/{iid}/hydrants", {"action": "apply"}, email)[0] == 403
    assert call(a, f"/api/cad/incident/{iid}/notif", {"kind": "ROAD"}, VAL)[0] == 403
    assert call(a, f"/api/cad/incident/{iid}/notif", {"kind": "ROAD"}, JORDAN)[0] == 200
    code, out = call(a, f"/api/cad/incident/{iid}/hydrants", {"action": "apply"}, CASEY)
    assert code == 200 and out["done"][0]["did"] == "mark"
    code, card = get(a, "/api/cad/hydrants/c990001", VAL)                       # the card: anyone signed in
    assert code == 200 and card["notifications"][0]["id"] == iid
    acts = [x["summary"] for x in a.cad.activity.rows(CASEY, "action", None, 50)]
    assert "Notification hydrants: apply (1)" in acts
    code, out = call(a, f"/api/cad/incident/{iid}/hydrants", {"action": "nope"}, CASEY)
    assert code == 400


# -- the console: the scripts wired --------------------------------------------------------------------------------------------

def test_the_scripts_are_wired():
    from pathlib import Path
    static = Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static"
    html = (static / "cad.html").read_text()
    assert 'id="d-notif"' in html and '<script src="/static/notif.js"></script>' in html
    assert html.index('src="/static/cad.js"') < html.index("/static/notif.js")
    src = (static / "notif.js").read_text()
    assert 'post(inc, "hydrants"' in src and 'post(inc, "notif"' in src and '"/api/cad/incident/${inc.id}/${path}"' not in src
    assert "`/api/cad/incident/${inc.id}/${path}`" in src and 'may("admin")' in src
    for line in src.splitlines():
        assert not re.search(r"console\.\w+\(", line), line


# -- in the browser (headless Chromium; skipped without Playwright). SIZEUP_SHOTS=<dir> keeps the screenshots ---------------

def test_the_incident_card_and_the_hydrants_on_the_map_in_a_browser(tmp_path):
    import os
    import threading
    sync_api = pytest.importorskip("playwright.sync_api", reason="needs Playwright")
    from sizeup.web.server import WebServer
    c = make_cad(tmp_path)
    g = fake_gis()
    wire(c, g)
    rules(c, OOS_RULE, IN_RULE)
    h = c.hydrants
    h.edit("c990001", {"flow_gpm": 1650})
    h.edit("c990002", {"status": "oos", "status_reason": "Leaking", "flow_gpm": 1100})          # marked OOS by hand
    h.create({"lat": LAT0 + 25 * DLAT, "lon": LON0 + 0.001, "number": "9077", "location": "Example Ave"})   # 2 km off
    iid = notification(c, "Water co advises HYD 9001, 9002, 9077, 9099 OOS", lat=LAT0 + 0.6 * DLAT, lon=LON0 + 0.0003)
    number = c.store.incident(iid)["number"]
    shots = os.environ.get("SIZEUP_SHOTS", "")
    fonts = "https://tiles.openfreemap.org/fonts/"
    scene = {"timezone": "America/New_York", "home": {"center": [LON0 + 0.0008, LAT0 + 0.00135], "zoom": 17.0},
             "glyphs": fonts + "{fontstack}/{range}.pbf"}
    ws = WebServer("127.0.0.1", 0, scene, lambda: {"time": 0}, lambda cid: None, lambda i: False, cad=CadApi(c), gis=g)
    threading.Thread(target=ws.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{ws.httpd.server_address[1]}"
    shot = (lambda loc, name: loc.screenshot(path=os.path.join(shots, name))) if shots else (lambda loc, name: None)
    pw = sync_api.sync_playwright().start()
    try:
        browser = pw.chromium.launch()
    except Exception as e:  # noqa: BLE001
        pw.stop()
        ws.shutdown()
        pytest.skip(f"no Chromium for Playwright: {str(e).splitlines()[0]}")
    try:
        pg = browser.new_page(viewport={"width": 1500, "height": 950})
        pg.set_default_timeout(15000)
        pg.route(lambda url: not url.startswith(base) and not (shots and url.startswith(fonts)), lambda route: route.abort())
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(base + "/cad")
        pg.fill("#operator", "JD")
        card = f"#q-nonemerg li[data-key='{iid}']"
        pg.wait_for_selector(card)
        assert "Notification · Hydrants OOS" in pg.inner_text(f"{card} .qt")          # the sub-type after the type
        assert "4 hydrants OOS proposed" in pg.inner_text(card)
        pg.evaluate(f"() => selectIncident({iid})")
        pg.wait_for_selector("#d-notif:not([hidden]) .nf-row")
        assert pg.inner_text("#d-type") == "Notification · Hydrants OOS"
        rws = pg.evaluate("() => [...document.querySelectorAll('#d-notif .nf-row, #d-notif .nf-cand')]"
                          ".map((r) => r.innerText.replace(/\\s+/g, ' ').trim())")
        assert len(rws) == 5, rws
        assert "9001 in service · 65 m" in rws[0] and rws[0].endswith("Mark OOS ✕")
        assert "9002 already OOS" in rws[1] and rws[1].endswith("Link ✕")
        assert "9077 not near: confirm" in rws[2] and "9077 · 2.4 km · Exampleville · Example Ave" in rws[3]
        assert rws[3].endswith("Confirm")
        assert "9099 not in the hydrant list" in rws[4] and "+ Add" in rws[4]
        assert "Apply (2)" in pg.inner_text("#d-notif .nf-foot")
        assert pg.input_value("#d-notif select.nf-kind") == "HYD-OOS"
        shot(pg.locator("#d-notif"), "notif-proposed.png")
        pg.click("#d-notif button:has-text('Apply (2)')")
        pg.wait_for_selector(f"{card} .nf-line:has-text('2 hydrants OOS')")
        pg.wait_for_selector("#d-notif .chip:has-text('2 hydrants OOS')")
        assert h.one("c990001")["hydrant"]["status_reason"] == f"Notification {number}"
        # the elsewhere one: confirmed from where it is
        pg.click("#d-notif .nf-cand button:has-text('Confirm')")
        pg.wait_for_selector(f"{card} .nf-line:has-text('3 hydrants OOS')")
        shot(pg.locator(card), "notif-queue-card.png")
        # (c) the call's Hydrants list marks the linked ones
        pg.wait_for_selector("#d-hydrants .hy-linked .chip:has-text('linked')")
        assert pg.locator("#d-hydrants .hy-linked").count() == 2                       # 9001, 9002 near; 9077 2 km off
        pg.wait_for_function("() => document.querySelector('#d-notif').innerText.includes('OOS · linked')")
        shot(pg.locator("#d-notif"), "notif-linked.png")
        shot(pg.locator("#d-hydrants"), "notif-call-hydrants.png")
        # the map: the OOS mark, and the card's link back to the notification
        pg.wait_for_selector("#d-map canvas.maplibregl-canvas", state="attached")
        pg.wait_for_function("() => window.GisLayers && GisLayers.catalog() && [...GisLayers.maps].length > 0")
        pg.evaluate("() => MapLayers.setLayer('gis:hydrants', true)")
        pg.evaluate("""() => { const gm = [...GisLayers.maps].find((x) => x.map.getContainer().closest('#d-map'));
            gm.map.jumpTo({ center: cad.scene.home.center, zoom: cad.scene.home.zoom }); }""")
        pg.wait_for_function("""() => { const gm = [...GisLayers.maps].find((x) => x.map.getContainer().closest('#d-map'));
            return gm && gm.map.getLayer('gis-hydrants-pt') && gm.map.querySourceFeatures('gis-hydrants')
              .some((f) => f.properties._k === 'c990001' && f.properties._st === 'oos'); }""")
        img = pg.evaluate("""() => { const gm = [...GisLayers.maps].find((x) => x.map.getContainer().closest('#d-map'));
            const f = gm.map.querySourceFeatures('gis-hydrants').find((x) => x.properties._k === 'c990001');
            return Hydrants.imageFor(f.properties); }""")
        assert img == "hy-down"
        pg.evaluate("""() => { const gm = [...GisLayers.maps].find((x) => x.map.getContainer().closest('#d-map'));
            const f = gm.map.querySourceFeatures('gis-hydrants').find((x) => x.properties._k === 'c990001');
            Hydrants.show(gm, f.properties); }""")
        pg.wait_for_selector(f".ml-info:not([hidden]) .hy-notif button:has-text('Notification {number}')")
        pg.wait_for_timeout(400)
        shot(pg.locator("#d-map"), "notif-hydrant-map-card.png")
        # back in service from the event: all of them, then it asks to close
        pg.evaluate("() => { const gm = [...GisLayers.maps].find((x) => x.map.getContainer().closest('#d-map')); gm.control.closeInfo(); }")
        pg.click("#d-notif button:has-text('All back in service')")
        pg.click(".dialog button:has-text('Back in service')")
        pg.wait_for_selector("#d-notif .nf-foot:has-text('All hydrants back in service') button:has-text('Close')")
        assert "All hydrants back in service" in pg.inner_text(card)
        shot(pg.locator("#d-notif"), "notif-all-back.png")
        assert h.one("c990001")["hydrant"]["status"] == "in_service"
        # Settings > Lists > Notification rules
        pg.goto(base + "/cad#settings/lists")
        pg.evaluate("() => { S.list = 'notif_rules'; sRender(true); }")
        pg.wait_for_selector(".nf-rule")
        assert pg.locator(".nf-rule").count() == 2
        pg.fill(".nf-test input", "HYD 12, 34 OOS")
        pg.click(".nf-test button")
        pg.wait_for_selector(".nf-test-out:has-text('Hydrants OOS · 12 OOS, 34 OOS')")
        shot(pg.locator("#settings .s-card:has(.nf-rule)"), "notif-rules-settings.png")
        assert errors == []
    finally:
        browser.close()
        pw.stop()
        ws.shutdown()
