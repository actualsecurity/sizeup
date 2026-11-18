"""Out of service is a person's to end (sizeup/cad/oos.py): no automation (the radio's status hints taken in 60 Control
mode, the county feed's units and times, a page's units) changes an out-of-service unit's status or puts it on a call;
each refusal is one notice (deduped) in the unit log and on the call, with "In service?" for a person. Synthetic only:
the scripted feed and radio lines of tests/test_dispatch_modes.py, an invented P25 radio ID."""

from __future__ import annotations

import json

import pytest

from sizeup.cad import oos
from sizeup.cad.api import CadApi, needs
from sizeup.cad.store import PERSON, CadError, OosLocked
from sizeup.cloud.access import Identity
from tests.test_dispatch_modes import AutoRig
from tests.test_incident_sources import T0
from tests.test_radios import tx

RADIO = "9990001"            # invented: a portable carried on E62


@pytest.fixture
def rig(tmp_path):
    return AutoRig(tmp_path)


def oos_unit(rig, name: str, note: str = "pump") -> None:
    rig.cad.unit_status(rig.uid(name), {"status": "OS", "note": note, "operator": "Lt. Test"})


def notices(rig) -> list[dict]:
    return rig.cad.store.q("SELECT * FROM oos_notices ORDER BY id")


def unit_log(rig, name: str) -> list[str]:
    return [e["text"] for e in rig.cad.store.unit_log() if e["unit_id"] == rig.uid(name)]


def heard(rig, tid: str, at: float, text: str, radio_id: str = RADIO, **kw) -> None:
    """A Fire/EMS 15 transmission from a P25 radio, its rig from the registry as the app gives it (App: rig_p25)."""
    rig.radio(tid, at, text, rig=rig.cad.radios.rig_p25([(radio_id, "")]), radio_id=radio_id, **kw)


def test_a_radio_responding_never_puts_an_out_of_service_rig_back(rig):
    rig.cad.save_radio({"system": "p25", "radio_id": RADIO, "unit": "E62", "operator": "Lt. Test"})
    oos_unit(rig, "E62")
    rig.created(units=("E60",))
    heard(rig, "307-3-call_3-t0", T0 + 40, "Engine 62 responding")
    assert rig.status("E62") == "OS" and rig.on("E62") is None                    # not put on the call, not in service
    t = rig.cad.store.one("SELECT rig, radio_id FROM radio_traffic WHERE id = '307-3-call_3-t0'")
    assert (t["rig"], t["radio_id"]) == ("E62", RADIO)                              # still E62's transmission
    [n] = notices(rig)
    assert (n["unit"], n["source"], n["reason"], n["incident_id"], n["traffic_id"], n["count"]) == \
        ("E62", "radio", "radio:enroute", rig.iid(), "307-3-call_3-t0", 1)
    assert unit_log(rig, "E62")[0] == "E62 out of service: radio heard “responding”, not changed"
    assert "E62 out of service: radio heard “responding”, not put on this call" in [e["text"] for e in rig.inc()["events"]]
    assert not any(s["unit"] == "E62" for s in rig.inc()["auto"]["steps"])        # no automatic step to undo
    [shown] = rig.cad.state()["oos_notices"]
    assert (shown["unit"], shown["what"], shown["incident_number"]) == ("E62", "radio heard “responding”", rig.inc()["number"])
    [h] = rig.cad.status_hints()                                                   # still a suggestion for a person
    assert h["unit"] == "E62" and h["oos"] is True

    rig.cad.hint(h["id"], "dismiss", {"operator": "Lt. Test"})                    # not so; said again 2 min later
    heard(rig, "307-4-call_4-t0", T0 + 160, "Engine 62 responding")
    assert rig.status("E62") == "OS" and rig.on("E62") is None
    [n] = notices(rig)                                                             # one notice, counted twice
    assert n["count"] == 2 and n["traffic_id"] == "307-4-call_4-t0"
    assert len(unit_log(rig, "E62")) == 2                                          # OS, then one notice line

    rig.clock.t = T0 + 160 + oos.NOTICE_S + 60                                     # past the window: a new notice
    with pytest.raises(OosLocked):
        rig.cad.store.set_unit_status(rig.uid("E62"), "RS", "Radio · auto", by="radio",
                                      cause=oos.cause("radio", "radio heard “responding”", "enroute"))
    assert len(notices(rig)) == 2


def test_in_service_from_the_notice_is_a_person_and_works(rig):
    oos_unit(rig, "E62")
    rig.created(units=("E60",))
    rig.radio("307-3-call_3-t0", T0 + 40, "Engine 62 responding", rig="E62", unit="E62")
    [n] = rig.cad.state()["oos_notices"]
    rig.at(60)
    out = rig.cad.oos_notice(n["id"], "service", {"operator": "Lt. Test"})
    assert out["status"] == "AV" and rig.status("E62") == "AV" and rig.on("E62") is None   # in service, on no call
    h = rig.cad.store.q("SELECT * FROM unit_history WHERE unit_id = ? ORDER BY id DESC LIMIT 1", (rig.uid("E62"),))[0]
    assert (h["old_status"], h["new_status"], h["operator"]) == ("OS", "AV", "Lt. Test")
    assert rig.cad.state()["oos_notices"] == []
    assert notices(rig)[0]["outcome"] == "in_service" and notices(rig)[0]["operator"] == "Lt. Test"
    with pytest.raises(CadError, match="already in service"):
        rig.cad.oos_notice(n["id"], "service", {"operator": "Lt. Test"})
    rig.radio("307-5-call_5-t0", T0 + 90, "Engine 62 responding", rig="E62", unit="E62")
    assert rig.on("E62")["status"] == "RS"                                        # in service: automation as before


def test_dismiss_keeps_it_out_of_service_and_a_board_change_ends_the_notice(rig):
    oos_unit(rig, "E62")
    oos_unit(rig, "E64", "no driver")
    rig.created(units=("E60",))
    rig.radio("307-3-call_3-t0", T0 + 40, "Engine 62 responding", rig="E62", unit="E62")
    [n] = rig.cad.state()["oos_notices"]
    rig.cad.oos_notice(n["id"], "dismiss", {"operator": "Lt. Test"})
    assert rig.status("E62") == "OS" and rig.cad.state()["oos_notices"] == []
    with pytest.raises(OosLocked):
        rig.cad.store.assign(rig.iid(), [rig.uid("E64")], "County feed · auto")      # named nobody: refused (fail closed)
    assert [x["unit"] for x in rig.cad.state()["oos_notices"]] == ["E64"]
    rig.cad.unit_status(rig.uid("E64"), {"status": "AV", "operator": "Lt. Test"})  # a person, on the board
    assert rig.status("E64") == "AV" and rig.cad.state()["oos_notices"] == []


def test_the_county_feed_never_dispatches_an_out_of_service_unit(rig):
    oos_unit(rig, "E60")
    rig.created()                                                                  # lists E60 and L31
    assert rig.status("E60") == "OS" and rig.on("E60") is None and rig.on("L31")["status"] == "DP"
    [n] = notices(rig)
    assert (n["unit"], n["source"], n["reason"], n["incident_id"]) == ("E60", "county", "county:dispatched", rig.iid())
    assert "E60 out of service: County feed lists it, not put on this call" in [e["text"] for e in rig.inc()["events"]]
    rig.at(60)
    rig.change(unit_times={"E60": {"responding": T0 + 55}, "L31": {"responding": T0 + 58}})
    assert rig.status("E60") == "OS" and rig.status("L31") == "RS"                # the other unit: as before
    rig.at(900)
    rig.change(times={"in_service": T0 + 890})
    assert rig.inc()["status"] == "closed" and rig.status("E60") == "OS"         # the close never touches it
    assert len(notices(rig)) == 1


def test_a_page_never_dispatches_an_out_of_service_unit(rig):
    oos_unit(rig, "E60")
    rig.created(units=("L31",))
    rig.at(20)
    rig.page()                                                                     # names Engine 60 and Ladder 31
    assert rig.status("E60") == "OS" and rig.on("E60") is None
    [n] = notices(rig)
    assert (n["unit"], n["source"], n["what"]) == ("E60", "page", "named on the page")


def test_the_gate_fails_closed_and_a_person_always_passes(rig):
    rig.created(units=("L31",))
    s = rig.cad.store
    oos_unit(rig, "E60")
    for write in (lambda **k: s.set_unit_status(rig.uid("E60"), "AV", **k),
                  lambda **k: s.set_unit_status_on(rig.uid("E60"), "RS", rig.iid(), **k),
                  lambda **k: s.assign(rig.iid(), [rig.uid("E60")], **k),
                  lambda **k: s.assign(rig.iid(), [rig.uid("E60")], attach=True, **k),
                  lambda **k: s.return_to_incident(rig.iid(), rig.uid("E60"), "RS", **k)):
        with pytest.raises(OosLocked):
            write()                                                                # no by: an automation
        assert rig.status("E60") == "OS" and rig.on("E60") is None
    assert [n["reason"] for n in notices(rig)] == ["automation:AV", "automation:RS", "automation:DP", "automation:assign"]
    assert s.set_unit_status(rig.uid("L31"), "RS", by="county")["status"] == "RS"   # in service: unaffected
    s.set_unit_status_on(rig.uid("E60"), "RS", rig.iid(), "Lt. Test", by=PERSON)   # a person: from the board
    assert rig.status("E60") == "RS" and rig.on("E60")["status"] == "RS"


def test_a_radio_moved_to_another_unit_follows_that_unit(rig):
    """The admin moved a portable from an out-of-service engine to the member's own unit (Settings > Radios): its
    transmissions are that unit's, and work as any other."""
    rig.cad.save_radio({"system": "p25", "radio_id": RADIO, "unit": "E62", "operator": "Lt. Test"})
    oos_unit(rig, "E62")
    rig.cad.save_radio({"system": "p25", "radio_id": RADIO, "unit": "E64", "operator": "Lt. Test"})
    rig.created(units=("E60",))
    heard(rig, "307-3-call_3-t0", T0 + 40, "Engine 64 responding")
    assert rig.on("E64")["status"] == "RS" and rig.status("E62") == "OS"
    assert notices(rig) == []


def test_an_undo_never_puts_back_a_unit_put_out_of_service_since(rig):
    rig.created(units=("E60",))
    rig.at(60)
    rig.change(unit_times={"E60": {"responding": T0 + 55, "in_service": T0 + 58}})  # the feed cleared it
    assert rig.status("E60") == "AV"
    oos_unit(rig, "E60")
    s = next(x for x in rig.inc()["auto"]["steps"] if x["unit"] == "E60" and x["status"] == "AV")
    with pytest.raises(CadError, match="out of service now"):
        rig.cad.auto_undo(rig.iid(), {"step": s["id"], "operator": "Lt. Test"})
    assert rig.status("E60") == "OS" and rig.here("E60") is None


def test_the_one_tap_is_for_dispatchers_and_admins_and_in_their_activity(tmp_path):
    from tests.test_roles import JORDAN, VAL, people
    rig = AutoRig(tmp_path)
    api = CadApi(rig.cad)
    people(rig.cad)
    assert needs("/api/cad/oos-notice/3/service") == "dispatch" and needs("/api/cad/oos-notice/3/dismiss") == "dispatch"
    oos_unit(rig, "E62")
    rig.created(units=("E60",))
    rig.radio("307-3-call_3-t0", T0 + 40, "Engine 62 responding", rig="E62", unit="E62")
    [n] = rig.cad.state()["oos_notices"]

    def post(path, body, email):
        code, raw, _ = api.post(path, json.dumps(body).encode(), Identity(email=email))
        return code, json.loads(raw)
    assert post(f"/api/cad/oos-notice/{n['id']}/service", {}, VAL)[0] == 403
    assert rig.status("E62") == "OS"
    code, out = post(f"/api/cad/oos-notice/{n['id']}/service", {}, JORDAN)
    assert code == 200 and out["status"] == "AV" and rig.status("E62") == "AV"
    assert "Put E62 in service (radio heard “responding”)" in [r["summary"] for r in rig.cad.activity.rows(JORDAN, "action", None, 5)]
    assert notices(rig)[0]["operator"] == "Jordan <jordan@example.org>"


def test_the_notice_lapses_after_five_minutes_and_the_unit_stays_as_it_was(rig):
    assert oos.NOTICE_S == 300
    oos_unit(rig, "E62")
    rig.created(units=("E60",))
    rig.radio("307-3-call_3-t0", T0 + 40, "Engine 62 responding", rig="E62", unit="E62")
    [n] = rig.cad.state()["oos_notices"]
    rig.clock.t = T0 + 41 + oos.NOTICE_S - 1
    assert [x["id"] for x in rig.cad.state()["oos_notices"]] == [n["id"]]        # 4:59 on: still shown
    rig.clock.t = T0 + 41 + oos.NOTICE_S + 1
    assert rig.cad.state()["oos_notices"] == [] and rig.cad.status_hints() == []    # lapsed, and the suggestion expired
    assert rig.status("E62") == "OS" and rig.on("E62") is None                    # exactly as it was
    with pytest.raises(CadError, match="lapsed"):
        rig.cad.oos_notice(n["id"], "service", {"operator": "Lt. Test"})
    assert rig.status("E62") == "OS"


def test_a_notice_with_no_call_lapses_after_five_minutes(rig):
    oos_unit(rig, "E62")
    with pytest.raises(OosLocked):
        rig.cad.store.set_unit_status(rig.uid("E62"), "AV", by="radio", cause=oos.cause("radio", "radio heard “available”", "free"))
    [n] = rig.cad.state()["oos_notices"]
    assert n["incident_id"] is None
    rig.clock.t += oos.NOTICE_S + 1
    assert rig.cad.state()["oos_notices"] == [] and rig.status("E62") == "OS"


def test_the_notice_goes_when_its_call_closes(rig):
    oos_unit(rig, "E62")
    rig.created(units=("E60",))
    rig.radio("307-3-call_3-t0", T0 + 40, "Engine 62 responding", rig="E62", unit="E62")
    [n] = rig.cad.state()["oos_notices"]
    rig.cad.close(rig.iid(), {"disposition": "COMPLETE", "operator": "Lt. Test"})
    assert rig.cad.state()["oos_notices"] == []                                    # its call closed: gone at once
    assert rig.status("E62") == "OS"
    with pytest.raises(CadError, match="lapsed"):
        rig.cad.oos_notice(n["id"], "dismiss", {"operator": "Lt. Test"})


def test_a_person_accepting_the_suggestion_is_asked_first(rig):
    oos_unit(rig, "E62")
    rig.created(units=("E60",))
    rig.radio("307-3-call_3-t0", T0 + 40, "Engine 62 responding", rig="E62", unit="E62")
    [h] = rig.cad.status_hints()
    assert h["oos"] and h["onto"]
    with pytest.raises(CadError, match="E62 is out of service: put it on the call anyway"):
        rig.cad.hint(h["id"], "accept", {"operator": "Lt. Test"})                 # not confirmed: nothing changes
    assert rig.status("E62") == "OS" and rig.on("E62") is None
    out = rig.cad.hint(h["id"], "accept", {"operator": "Lt. Test", "confirm": True})
    assert out["status"] == "RS" and rig.here("E62")["status"] == "RS"           # the person's say-so: on the call
    assert rig.cad.state()["oos_notices"] == []                                    # no longer out of service


def test_a_suggestion_nobody_takes_never_changes_an_out_of_service_unit(rig):
    rig.mode("pcfd")                                                               # no automation at all: a suggestion only
    oos_unit(rig, "E62")
    rig.created()
    rig.cad.accept(rig.iid(), {"operator": "Lt. Test"})
    rig.radio("307-3-call_3-t0", T0 + 40, "Engine 62 responding", rig="E62", unit="E62")
    [h] = rig.cad.status_hints()
    assert h["oos"] and notices(rig) == []
    rig.mode("60control")
    rig.radio("307-4-call_4-t0", T0 + 60, "Engine 62 on location", rig="E62", unit="E62")   # automation on: refused
    assert rig.status("E62") == "OS" and rig.on("E62") is None
    rig.clock.t = T0 + 60 + 301
    assert rig.cad.status_hints() == [] and rig.status("E62") == "OS" and rig.on("E62") is None


def refuse(rig, name: str, what: str = "radio heard “responding”", reason: str = "enroute", iid=None) -> None:
    with pytest.raises(OosLocked):
        rig.cad.store.set_unit_status(rig.uid(name), "RS", by="radio", cause=oos.cause("radio", what, reason, incident_id=iid))


def test_a_tap_ends_every_notice_of_the_unit_and_a_new_spell_starts_fresh(rig):
    oos_unit(rig, "E62")
    rig.created(units=("E60",))
    refuse(rig, "E62", iid=rig.iid())
    refuse(rig, "E62", "County feed lists it", "dispatched", rig.iid())
    assert len(rig.cad.state()["oos_notices"]) == 2
    first = rig.cad.state()["oos_notices"][0]
    rig.clock.t += 10
    rig.cad.oos_notice(first["id"], "service", {"operator": "Lt. Test"})         # one ✓: both end
    assert rig.cad.state()["oos_notices"] == [] and {n["outcome"] for n in notices(rig)} == {"in_service"}
    rig.clock.t += 10
    oos_unit(rig, "E62", "pump again")                                             # a new out-of-service spell
    assert rig.cad.state()["oos_notices"] == []                                    # the old ones never come back
    lines = len(unit_log(rig, "E62"))
    refuse(rig, "E62", iid=rig.iid())
    [n] = rig.cad.state()["oos_notices"]
    assert n["count"] == 1 and n["id"] > max(x["id"] for x in notices(rig)[:2])    # a fresh notice, not folded
    assert len(unit_log(rig, "E62")) == lines + 1                                  # with its own line


def test_dismiss_quiets_the_unit_for_five_minutes(rig):
    oos_unit(rig, "E62")
    rig.created(units=("E60",))
    refuse(rig, "E62", iid=rig.iid())
    refuse(rig, "E62", "County feed lists it", "dispatched", rig.iid())
    n = rig.cad.state()["oos_notices"][0]
    rig.cad.oos_notice(n["id"], "dismiss", {"operator": "Lt. Test"})               # one ✕: both end
    assert rig.cad.state()["oos_notices"] == [] and {x["outcome"] for x in notices(rig)} == {"dismissed"}
    lines = len(unit_log(rig, "E62"))
    rig.clock.t += 30
    refuse(rig, "E62", iid=rig.iid())                                              # 30 s on: still refused, nothing shown
    assert rig.cad.state()["oos_notices"] == [] and len(unit_log(rig, "E62")) == lines and len(notices(rig)) == 2
    rig.clock.t += oos.NOTICE_S
    refuse(rig, "E62", iid=rig.iid())                                              # past the quiet: a new notice
    assert [x["unit"] for x in rig.cad.state()["oos_notices"]] == ["E62"] and len(unit_log(rig, "E62")) == lines + 1
    assert rig.status("E62") == "OS"


def test_the_quiet_ends_when_the_dismissed_notices_call_closes(rig):
    oos_unit(rig, "E62")
    rig.created(units=("E60",))
    refuse(rig, "E62", iid=rig.iid())
    rig.cad.oos_notice(rig.cad.state()["oos_notices"][0]["id"], "dismiss", {"operator": "Lt. Test"})
    rig.cad.close(rig.iid(), {"disposition": "COMPLETE", "operator": "Lt. Test"})
    rig.clock.t += 30
    refuse(rig, "E62")                                                             # its call closed: not quiet any more
    assert [x["unit"] for x in rig.cad.state()["oos_notices"]] == ["E62"]
