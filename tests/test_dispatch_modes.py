"""Dispatch modes (#311, sizeup/cad/automode.py) and the per-incident radio log (sizeup/cad/radiolog.py). Synthetic only:
a scripted external CAD feed (tests/fake_feed.py: created -> dispatched -> responding -> on location -> in service),
page tones, radio status lines and unrelated traffic, with made-up places, units' words and times."""

from __future__ import annotations

import json

import pytest

from sizeup.cad.api import CadApi, needs
from sizeup.cad.radiolog import address_hit, addresses_in, place_hit
from sizeup.cloud.access import Identity
from tests.test_incident_sources import T0, Rig
from tests.test_radios import tx

ADDR = "12 Example Ave"


class AutoRig(Rig):
    """The incident sources' rig, the way the app applies a feed's event (Cad.source_event: sources, then the mode)."""

    def sink(self, kind: str, d: dict) -> None:
        self.sent.append((kind, d))
        (self.cad.source_event if kind == "source_event" else self.cad.sources.on_health)(d)

    def iid(self, eid: str = "A1") -> int:
        return self.cad.store.one("SELECT incident_id FROM source_reports WHERE source = 'county' AND ref = ?", (eid,))["incident_id"]

    def inc(self, eid: str = "A1") -> dict:
        return self.cad.full(self.iid(eid))

    def status(self, name: str) -> str:
        return self.cad.store.one("SELECT status FROM units WHERE name = ?", (name,))["status"]

    def on(self, name: str, eid: str = "A1") -> dict | None:
        return next((a for a in self.inc(eid)["assignments"] if a["name"] == name), None)

    def steps(self, eid: str = "A1") -> list[tuple]:
        return [(s["kind"], s["source"], s["unit"], s["status"], s["undone"]) for s in self.inc(eid)["auto"]["steps"]]

    def step(self, kind: str, unit: str = "", eid: str = "A1") -> dict:
        return next(s for s in self.inc(eid)["auto"]["steps"] if s["kind"] == kind and s["unit"] == unit and not s["undone"])

    def radio(self, tid: str, start: float, text: str, tg: int | None = 307, source: str = "Fire/EMS 15", **kw) -> list[int]:
        self.clock.t = max(self.clock.t, start + 1)
        self.cad.record_traffic(tx(tid, start, source=source, tg=tg, text=text, **kw))
        return [r["incident_id"] for r in self.cad.store.q("SELECT incident_id FROM radio_links WHERE traffic_id = ?", (tid,))]

    def links(self, iid: int) -> dict[str, tuple[str, str]]:
        return {r["traffic_id"]: (r["why"], r["unit"]) for r in self.cad.store.q(
            "SELECT * FROM radio_links WHERE incident_id = ?", (iid,))}

    def mode(self, m: str) -> None:
        self.cad.dispatch_mode({"mode": m, "operator": "Lt. Test"})

    def created(self, **kw) -> None:
        """The feed's event as first reported: dispatched now with E60 and L31 at 12 Example Ave."""
        self.feed.put("A1", **{"type": "ALARM/COMM", "location": ADDR, "units": ("E60", "L31"),
                               "times": {"dispatched": self.clock.t}, **kw})
        self.poll()

    def change(self, **kw) -> None:
        self.feed.change("A1", **kw)
        self.poll()


@pytest.fixture
def rig(tmp_path):
    return AutoRig(tmp_path)


# -- 60 Control dispatching -------------------------------------------------------------------------------------------


def test_60_control_moves_the_call_through_by_itself(rig):
    assert rig.cad.auto.mode() == "60control" and rig.cad.state()["dispatch_mode"]["short"] == "60 Control"
    rig.created()
    inc = rig.inc()
    assert inc["status"] == "active" and inc["number"]                          # accepted at once, units on it
    assert {a["name"]: (a["status"], a["dispatched_at"]) for a in inc["assignments"]} == {"E60": ("DP", T0), "L31": ("DP", T0)}
    assert rig.steps() == [("accept", "county", "", "", False), ("assign", "county", "E60", "DP", False),
                           ("assign", "county", "L31", "DP", False)]
    ev = {e["id"]: e for e in inc["events"]}
    assert all(ev[s["event_id"]]["operator"] == "County feed · auto" for s in inc["auto"]["steps"])

    rig.at(20)
    rig.page()                                                                     # the tones: paged at the tone time
    assert rig.inc()["auto"]["paged_at"] == T0 + 20
    assert ("paged", "radio", "", "", False) in rig.steps()

    rig.at(60)
    rig.change(unit_times={"E60": {"responding": T0 + 55}})                        # per unit, from the feed
    assert rig.status("E60") == "RS" and rig.on("E60")["enroute_at"] == T0 + 55
    assert rig.status("L31") == "DP"                                               # the other unit: not moved
    assert rig.step("status", "E60")["source"] == "county"

    assert rig.iid() in rig.radio("307-1-call_1-t0", T0 + 70, "Ladder 31 responding")   # from the radio, per unit
    assert rig.status("L31") == "RS" and rig.step("status", "L31")["source"] == "radio"
    assert rig.on("L31")["enroute_at"] == T0 + 70
    assert rig.cad.status_hints() == []                                            # taken, not left as a suggestion

    rig.at(240)
    rig.change(unit_times={"E60": {"responding": T0 + 55, "on_location": T0 + 230}})
    assert rig.status("E60") == "OL" and rig.on("E60")["onscene_at"] == T0 + 230
    rig.radio("307-2-call_2-t0", T0 + 245, "Ladder 31 on location")
    assert rig.status("L31") == "OL"

    rig.at(900)
    rig.change(times={"in_service": T0 + 890})                                     # the feed's in service: closed
    inc = rig.inc()
    assert inc["status"] == "closed" and inc["disposition"] == "COMPLETE"
    assert rig.status("E60") == "AV" and rig.status("L31") == "AV"
    assert rig.steps()[-1] == ("close", "county", "", "", False)


def test_incident_times_move_a_unit_only_when_it_is_the_only_one(rig):
    rig.created(units=("E60",))
    rig.at(60)
    rig.change(times={"responding": T0 + 50})
    assert rig.status("E60") == "RS"
    rig.feed.put("B1", type="ALARM/COMM", location="400 Sample St", units=("E62", "L31"), times={"dispatched": rig.clock.t})
    rig.poll()
    rig.feed.change("B1", times={"responding": T0 + 70, "on_location": T0 + 80})
    rig.poll()
    assert rig.status("E62") == "DP" and rig.status("L31") == "DP"                 # which unit? not said: no move


def test_a_unit_on_another_call_is_not_moved_and_one_out_of_service_is_not_sent(rig):
    other = rig.cad.create({"type": "STRU", "address": "400 Sample St", "operator": "Lt. Test"})
    rig.cad.dispatch(other["id"], {"unit_ids": [rig.uid("L31")], "operator": "Lt. Test"})
    rig.cad.unit_status(rig.uid("E60"), {"status": "OS", "note": "pump", "operator": "Lt. Test"})
    rig.created()
    assert rig.inc()["assignments"] == [] and rig.status("L31") == "DP"
    assert any("L31 on" in e["text"] and "not moved" in e["text"] for e in rig.inc()["events"])


def test_a_radio_responding_puts_one_of_ours_on_the_one_new_tracked_call(rig):
    rig.created(units=("E60",))
    rig.radio("307-3-call_3-t0", T0 + 40, "Engine 62 responding")
    assert rig.on("E62")["status"] == "RS" and rig.step("status", "E62")["source"] == "radio"
    rig.radio("307-4-call_4-t0", T0 + 50, "Engine 64 on location")                # on location with no call: never guessed
    assert rig.on("E64") is None and rig.status("E64") == "AV"


def test_a_feed_type_nobody_maps_waits_for_a_person_then_goes_on(rig):
    rig.created(type="ZZZ UNKNOWN THING")
    assert rig.inc()["status"] == "queued" and rig.inc()["assignments"] == []
    rig.cad.accept(rig.iid(), {"type": "ALARM", "subtype": "COMM", "operator": "Lt. Test"})
    rig.at(60)
    rig.change(unit_times={"E60": {"responding": T0 + 55}})
    assert rig.on("E60")["status"] == "RS"                                          # tracked: assigned, then responding


# -- PCFD dispatching ------------------------------------------------------------------------------------------------


def test_pcfd_mode_takes_in_only_and_a_person_runs_the_call(rig):
    rig.mode("pcfd")
    rig.created()
    rig.at(20)
    rig.page()
    rig.at(60)
    rig.change(unit_times={"E60": {"responding": T0 + 55}})
    rig.radio("307-5-call_5-t0", T0 + 70, "Ladder 31 responding")
    rig.at(900)
    rig.change(times={"in_service": T0 + 890})
    inc = rig.inc()
    assert inc["status"] == "queued" and inc["assignments"] == [] and inc["auto"]["steps"] == []
    assert inc["auto"]["tracked"] is False and inc["auto"]["paged_at"] is None
    assert rig.status("E60") == "AV" and rig.status("L31") == "AV"
    assert [h["unit"] for h in rig.cad.status_hints()] == []                     # L31 on no call: nothing to suggest
    rig.cad.accept(rig.iid(), {"operator": "Lt. Test"})                            # by hand, as before
    rig.cad.dispatch(rig.iid(), {"unit_ids": [rig.uid("E60")], "operator": "Lt. Test"})
    rig.cad.unit_status(rig.uid("E60"), {"status": "OL", "operator": "Lt. Test"})
    rig.cad.close(rig.iid(), {"disposition": "FALSE", "operator": "Lt. Test"})
    assert rig.inc()["status"] == "closed" and rig.inc()["auto"]["steps"] == []


def test_pcfd_mode_leaves_a_radio_status_as_a_suggestion(rig):
    rig.mode("pcfd")
    rig.created()
    rig.cad.accept(rig.iid(), {"operator": "Lt. Test"})
    rig.cad.dispatch(rig.iid(), {"unit_ids": [rig.uid("E60")], "operator": "Lt. Test"})
    rig.radio("307-6-call_6-t0", T0 + 70, "Engine 60 responding")
    assert rig.status("E60") == "DP" and [h["unit"] for h in rig.cad.status_hints()] == ["E60"]


# -- a person's change wins --------------------------------------------------------------------------------------------


def test_a_person_wins_over_the_feed_for_that_unit(rig):
    rig.created()
    rig.cad.unit_status(rig.uid("E60"), {"status": "AV", "operator": "Lt. Test"})  # a person cleared E60 off it
    rig.cad.dispatch(rig.iid(), {"unit_ids": [rig.uid("E62")], "operator": "Lt. Test"})   # and sent E62 by hand
    rig.at(60)
    rig.change(units=("E60", "L31", "E62"), unit_times={"E60": {"responding": T0 + 50}, "E62": {"responding": T0 + 55},
                                                        "L31": {"responding": T0 + 58}})
    assert rig.status("E60") == "AV" and rig.on("E60")["cleared_at"]               # not put back
    assert rig.status("E62") == "DP"                                               # the person's: left as they set it
    assert rig.status("L31") == "RS"                                               # the feed's own: still moves
    rig.radio("307-7-call_7-t0", T0 + 80, "Engine 62 on location")
    assert rig.status("E62") == "DP" and [h["unit"] for h in rig.cad.status_hints()] == ["E62"]


def test_one_click_undo_and_the_feed_does_not_redo_it(rig):
    rig.created()
    rig.at(60)
    rig.change(unit_times={"E60": {"responding": T0 + 55}})
    s = rig.step("status", "E60")
    rig.cad.auto_undo(rig.iid(), {"step": s["id"], "operator": "Lt. Test"})
    assert rig.status("E60") == "DP" and rig.on("E60")["enroute_at"] is None
    rig.at(200)
    rig.change(unit_times={"E60": {"responding": T0 + 55, "on_location": T0 + 190}})
    assert rig.status("E60") == "DP"                                               # the person's now
    a = rig.step("assign", "L31")
    rig.cad.auto_undo(rig.iid(), {"step": a["id"], "operator": "Lt. Test"})
    assert rig.on("L31") is None and rig.status("L31") == "AV"
    rig.change(unit_times={"L31": {"responding": T0 + 195}})
    assert rig.on("L31") is None                                                   # not sent again
    rig.at(900)
    rig.change(times={"in_service": T0 + 890})
    assert rig.inc()["status"] == "closed"
    c = rig.step("close")
    rig.cad.auto_undo(rig.iid(), {"step": c["id"], "operator": "Lt. Test"})
    assert rig.inc()["status"] == "active"
    rig.feed.comment("A1", T0 + 905, "units clearing")
    rig.change(closed=True)
    assert rig.inc()["status"] == "active"                                         # a person reopened it: theirs
    assert sum(1 for e in rig.inc()["events"] if e["text"].startswith("Undone: ")) == 3
    with pytest.raises(Exception, match="already undone"):
        rig.cad.auto_undo(rig.iid(), {"step": c["id"], "operator": "Lt. Test"})
    with pytest.raises(Exception, match="stays accepted"):
        rig.cad.auto_undo(rig.iid(), {"step": rig.step("accept")["id"], "operator": "Lt. Test"})


def test_a_radio_step_undone_and_a_person_reopening(rig):
    rig.created()
    rig.radio("307-8-call_8-t0", T0 + 70, "Engine 60 responding")
    s = rig.step("status", "E60")
    assert s["source"] == "radio"
    rig.cad.auto_undo(rig.iid(), {"step": s["id"], "operator": "Lt. Test"})
    assert rig.status("E60") == "DP"
    rig.radio("307-9-call_9-t0", T0 + 90, "Engine 60 on location")
    assert rig.status("E60") == "DP" and [h["unit"] for h in rig.cad.status_hints()] == ["E60"]


# -- switching the mode mid-incident ---------------------------------------------------------------------------------


def test_switching_affects_only_what_comes_after(rig):
    rig.created()
    rig.mode("pcfd")
    rig.at(60)
    rig.change(unit_times={"E60": {"responding": T0 + 55}})
    assert rig.status("E60") == "DP"                                               # PCFD: nothing automatic
    rig.mode("60control")
    rig.at(70)
    rig.change(comments=((T0 + 65, "caller on scene"),))                            # nothing new for the units
    assert rig.status("E60") == "DP" and rig.on("E60")["enroute_at"] is None       # responding not replayed
    rig.at(200)
    rig.change(unit_times={"E60": {"responding": T0 + 55, "on_location": T0 + 190}})
    assert rig.status("E60") == "OL" and rig.on("E60")["onscene_at"] == T0 + 190  # after the switch: applied


def test_an_incident_taken_in_under_pcfd_stays_manual(rig):
    rig.mode("pcfd")
    rig.created()
    rig.mode("60control")
    rig.at(60)
    rig.change(unit_times={"E60": {"responding": T0 + 55}})
    assert rig.inc()["status"] == "queued" and rig.inc()["auto"]["tracked"] is False
    rig.feed.put("B1", type="ALARM/COMM", location="400 Sample St", units=("E62",), times={"dispatched": rig.clock.t})
    rig.poll()
    assert rig.inc("B1")["status"] == "active"                                    # a new one: 60 Control's


# -- the switch: who, and the audit ------------------------------------------------------------------------------------


def test_the_switch_is_for_dispatchers_and_admins_and_audited(tmp_path):
    from tests.test_roles import JULIAN, VAL, people
    rig = AutoRig(tmp_path)
    api = CadApi(rig.cad)
    people(rig.cad)
    assert needs("/api/cad/dispatch-mode") == "dispatch" and needs("/api/cad/incident/3/auto-undo") == "dispatch"

    def post(path, body, email):
        code, raw, _ = api.post(path, json.dumps(body).encode(), Identity(email=email))
        return code, json.loads(raw)
    code, out = post("/api/cad/dispatch-mode", {"mode": "pcfd"}, VAL)
    assert code == 403 and rig.cad.auto.mode() == "60control"
    code, out = post("/api/cad/dispatch-mode", {"mode": "pcfd"}, JULIAN)
    assert code == 200 and out["mode"] == "pcfd" and rig.cad.auto.mode() == "pcfd"
    code, out = post("/api/cad/dispatch-mode", {"mode": "auto"}, JULIAN)
    assert code == 400
    a = rig.cad.settings.audit_log("dispatch")[0]
    assert (a["key"], json.loads(a["old"]), json.loads(a["new"]), a["operator"]) == \
        ("dispatch_mode", "60 Control dispatching", "PCFD dispatching", "Julian <julian@example.org>")
    st = rig.cad.state()["dispatch_mode"]
    assert st["mode"] == "pcfd" and st["by"] == "Julian <julian@example.org>"
    assert "Dispatch mode: PCFD dispatching" in [r["summary"] for r in rig.cad.activity.rows(JULIAN, "action", None, 5)]
    post("/api/cad/dispatch-mode", {"mode": "60control"}, JULIAN)
    assert rig.cad.auto.mode() == "60control" and len(rig.cad.settings.audit_log("dispatch")) == 2
    assert rig.cad.state()["dispatch_mode"]["by"] == "Julian <julian@example.org>"


def test_the_mode_is_not_a_setting_to_export_or_import(rig):
    rig.mode("pcfd")
    exp = rig.cad.settings_export()
    assert "dispatch_mode" not in exp["overrides"]
    rig.cad.settings_post("import", {"action": "apply", "settings": exp, "operator": "Lt. Test"})
    assert rig.cad.auto.mode() == "pcfd"                                           # kept through an import


# -- the per-incident radio log (both modes) -------------------------------------------------------------------------


def test_address_and_place_words():
    assert address_hit("Engine 60 on location 12 Example Avenue, nothing showing", "12 Example Ave")
    assert address_hit("at 12 North Example Ave", "12 N Example Ave, Port Chester")
    assert not address_hit("Example Avenue is closed", "12 Example Ave")           # the street alone
    assert not address_hit("14 Example Avenue", "12 Example Ave")
    assert not address_hit("12 units on Example Avenue", "12 Example Ave")
    assert address_hit("Sample Street and Example Avenue", "Sample St & Example Ave")
    assert not address_hit("Sample Street", "Sample St & Example Ave")
    assert place_hit("units to the Example Mall loading dock", "Example Mall")
    assert not place_hit("the mall", "Mall")
    assert addresses_in("Engine 62 respond to 400 Sample Street for an alarm") == ["400 sample st"]


@pytest.mark.parametrize("mode", ["60control", "pcfd"])
def test_radio_log_attaches_what_is_about_the_incident(rig, mode):
    rig.mode(mode)
    rig.created()
    if mode == "pcfd":
        rig.cad.accept(rig.iid(), {"operator": "Lt. Test"})
        rig.cad.dispatch(rig.iid(), {"unit_ids": [rig.uid("E60"), rig.uid("L31")], "operator": "Lt. Test"})
    iid = rig.iid()
    assert rig.radio("307-10-call_10-t0", T0 + 30, "60 Control to Engine 60, copy your on location") == [iid]
    assert rig.radio("307-11-call_11-t0", T0 + 40, "Units operating at 12 Example Avenue, use caution") == [iid]
    # unrelated traffic: a unit not on it, another address, another channel, a page for another call
    assert rig.radio("307-12-call_12-t0", T0 + 50, "60 Control to Engine 62, respond to 400 Sample Street") == []
    assert rig.radio("307-13-call_13-t0", T0 + 55, "Engine 60 respond to 400 Sample Street, second call") == []
    assert rig.radio("999-1-call_1-t0", T0 + 60, "Engine 60 on location", tg=999, source="PD 1") == []
    assert rig.radio("p9", T0 + 65, "Port Chester Engine 60, respond to 400 Sample Street", tg=None, source="South 2") == []
    assert rig.radio("307-14-call_14-t0", T0 + 70, "Example Avenue is closed at the bridge") == []
    assert rig.radio("307-15-call_15-t0", T0 + 75, "Engine 64 returning to quarters") == []
    assert rig.links(iid)["307-10-call_10-t0"] == ("mention", "E60")
    assert rig.links(iid)["307-11-call_11-t0"] == ("mention", "address")
    shown = {r["id"]: r for r in rig.inc()["radio"]}
    assert shown["307-10-call_10-t0"]["text"].startswith("60 Control") and shown["307-10-call_10-t0"]["why"] == "mention"
    # one click off; it never comes back, even when the transcript is re-run
    rig.cad.unlink_traffic(iid, {"traffic_id": "307-10-call_10-t0", "operator": "Lt. Test"})
    rig.cad.comms.retext("307-10-call_10-t0", "60 Control to Engine 60, copy on location", "done")
    assert "307-10-call_10-t0" not in rig.links(iid)


def test_radio_log_only_while_open_and_never_two_incidents_for_one_address(rig):
    rig.created()
    iid = rig.iid()
    rig.at(900)
    rig.change(times={"in_service": T0 + 890})
    assert rig.radio("307-20-call_20-t0", T0 + 950, "Engine 60 back in quarters") == []
    a = rig.cad.create({"type": "STRU", "address": "40 Other Rd", "operator": "Lt. Test"})
    b = rig.cad.create({"type": "ALARM", "subtype": "COMM", "address": "40 Other Rd", "operator": "Lt. Test"})
    assert rig.radio("307-21-call_21-t0", T0 + 960, "something at 40 Other Road") == []
    assert iid not in rig.radio("307-22-call_22-t0", T0 + 970, "12 Example Avenue")
    assert a["id"] != b["id"]


def test_fireground_channel_only_when_known(rig):
    rig.created(units=("E63",))
    iid = rig.iid()
    rig.clock.t = T0 + 30
    rig.cad.record_traffic(tx("FG1-1", T0 + 30, rig="E63", radio_id="6301", text="Engine 63 on location"))
    assert rig.links(iid)["FG1-1"][0] in ("unit", "mention")
    rig.clock.t = T0 + 40
    rig.cad.record_traffic(tx("FG1-2", T0 + 40, text="Engine 63, water on the fire"))   # no ID, not known: #163
    assert "FG1-2" not in rig.links(iid)
    rig.change(extra={"fireground": "FG1"})                                       # the feed names its fireground
    rig.clock.t = T0 + 50
    rig.cad.record_traffic(tx("FG1-3", T0 + 50, text="primary search negative"))
    assert rig.links(iid)["FG1-3"] == ("fireground", "FG1")
    rig.feed.put("B1", type="STRUCTURE FIRE", location="400 Sample St", units=("E61",), times={"dispatched": rig.clock.t},
                 extra={"fireground": "FG1"})
    rig.poll()
    rig.clock.t = T0 + 60
    rig.cad.record_traffic(tx("FG1-4", T0 + 60, text="secondary search negative"))   # two calls on FG1: neither
    assert "FG1-4" not in rig.links(iid) and "FG1-4" not in rig.links(rig.iid("B1"))
