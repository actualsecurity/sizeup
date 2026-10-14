"""Unit statuses suggested from the radio (#37, sizeup/cad/statushints.py): the phrases, who said it, what is never
suggested, expiry, accept / dismiss, who may, and what the chronology says."""

import csv
import io
import json

import pytest

from sizeup.cad.api import CadApi
from sizeup.cad.feed import load_cfg
from sizeup.cad.lists import load
from sizeup.cad.statushints import Phrases
from tests.test_radios import Clock, _event_with, cad, tx, uid

P = Phrases(load_cfg(load()).units)


def said(text):
    return [(f.intent, f.units, f.lead) for f in P.find(text)]


@pytest.mark.parametrize("text,intent,units", [
    ("Engine 60 on location, three story commercial, nothing showing, investigating.", "onscene", ["E60"]),
    ("60 Control copies, 2391 responding, 1434.", "enroute", ["2391"]),
    ("E-64 on scene", "onscene", ["E64"]),
    ("Engine 64 is on the scene", "onscene", ["E64"]),
    ("Engine 64 has arrived", "onscene", ["E64"]),
    ("Engine 64 responding with four", "enroute", ["E64"]),
    ("60 Control, Ladder 31 en route", "enroute", ["L31"]),
    ("Ladder 31 enroute", "enroute", ["L31"]),
    ("Engine 64 staged at King and Main", "staging", ["E64"]),
    ("Engine 64 in staging", "staging", ["E64"]),
    ("Engine 64 back in service", "free", ["E64"]),
    ("Engine 64 available", "free", ["E64"]),
    ("Engine 64 returning to quarters", "free", ["E64"]),
    ("Engine 64 clear", "free", ["E64"]),
    ("Engine 64 clearing the scene", "free", ["E64"]),
    ("Engine 64 back in quarters", "quarters", ["E64"]),
    ("Engine 64 out of service", "oos", ["E64"]),
    ("2391 establishing Westchester Avenue command", "command", ["2391"]),
    ("Engine 62 and Engine 64 on location", "onscene", ["E62", "E64"]),
    ("Rye Brook Engine 14 on location", "onscene", ["E14"]),
])
def test_phrases_naming_the_unit(text, intent, units):
    assert said(text)[-1][:2] == (intent, units)


@pytest.mark.parametrize("text,intent,lead", [
    ("Responding.", "enroute", "start"),
    ("Copy, on location", "onscene", "start"),
    ("2391 to 60 Control, responding.", "enroute", "start"),
    ("Engine 64 to 60 Control on location", "onscene", "control"),
    ("We're clear of the scene", "free", "first"),
    ("We're clear", "free", "first"),
    ("I'll be command", "command", "first"),
    ("We are placing ourselves out of service", "oos", "first"),
    ("We're staging on King Street", "staging", "first"),
])
def test_phrases_said_by_the_talker(text, intent, lead):
    assert said(text) == [(intent, [], lead)]


@pytest.mark.parametrize("text", [
    "We're not on location yet", "Engine 64 is not responding", "Engine 64 isn't on scene",
    "Is Engine 64 responding?", "Engine 64, are you on location", "Any units responding", "Confirm you're on location",
    "Advise when on location", "Once on scene advise", "Engine 64 on location in 2 minutes", "On scene in about five",
    "PD is on location", "Hydrant out of service at Main and King", "Patient has arrived at the hospital",
    "Engine 191 on location",                                     # Rye's: mutual aid, not ours
    "Primary search all clear", "Clear.", "Loud and clear", "Clear the building", "Clear to enter",
    "Staging on King Street", "Go to staging", "Report to level one staging", "All units staging on Main",
    "Switch to the command channel", "Who has command?", "Command post at the northeast corner",
    "All responding units be advised", "We're returning to the scene", "We'll be available on the cell",
    "Engine 64 stuck in Route 1 traffic", "Engine sixty-four", "",
])
def test_phrases_that_are_not_a_status(text):
    assert said(text) == []


def test_two_statuses_in_one_breath():
    assert said("23-91 on location, I'll be command") == [("onscene", ["2391"], "unit"), ("command", [], "first")]


# -- the CAD: heard, suggested, shown ---------------------------------------------------------------------------

def setup(tmp_path, units=("E64", "E62")):
    clock = Clock()
    c = cad(tmp_path, clock)
    inc = _event_with(c, list(units), clock)
    clock.t += 30
    return c, clock, inc


def hints(c):
    return c.state()["status_hints"]


def status(c, name):
    return next(u for u in c.store.units() if u["name"] == name)["status"]


def test_named_unit_on_fg1_suggests_on_location_on_its_event(tmp_path):
    c, clock, inc = setup(tmp_path)
    c.record_traffic(tx("FG1-1", clock.t, text="Engine 64 on location, nothing showing"))
    clock.t += 5
    [h] = hints(c)
    assert (h["unit"], h["status"], h["status_name"], h["incident_id"], h["who"]) == ("E64", "OL", "on location", inc["id"], "named")
    assert h["said"] == "Engine 64 on location" and h["text"] == "Engine 64 on location, nothing showing"
    assert h["traffic_id"] == "FG1-1" and h["onto"] is False and h["channel"] == "FG1"
    assert status(c, "E64") == "DP"                      # a suggestion: nothing changed


def test_the_talker_from_its_radio_and_the_caller_from_the_text(tmp_path):
    c, clock, inc = setup(tmp_path)
    c.record_traffic(tx("307-a-call_1-t0", clock.t, "Fire/EMS 15", rig="E62", unit="E62", tg=307, text="Responding."))
    c.record_traffic(tx("FG1-2", clock.t + 1, text="Engine 64 to 60 Control, on location."))   # no ID on FG1: the caller
    by = {h["unit"]: h for h in hints(c)}
    assert (by["E62"]["status"], by["E62"]["who"]) == ("RS", "talker")
    assert (by["E64"]["status"], by["E64"]["who"]) == ("OL", "caller")


def test_the_sentence_naming_a_unit_wins_over_the_talker(tmp_path):
    c, clock, inc = setup(tmp_path, ("E64", "E62", "2391"))
    c.record_traffic(tx("FG1-1", clock.t, rig="2391", text="Engine 62 on location"))
    [h] = hints(c)
    assert (h["unit"], h["who"]) == ("E62", "named")


def test_a_dispatcher_echo_suggests_the_unit_named_once(tmp_path):
    c, clock, inc = setup(tmp_path, ("2391",))
    c.record_traffic(tx("307-b-call_2-t0", clock.t, "Fire/EMS 15", rig="2391", unit="2391", tg=307, text="2391 to 60 Control, responding."))
    c.record_traffic(tx("307-b-call_2-t1", clock.t + 3, "Fire/EMS 15", unit="60 Ctrl OP 7", tg=307,
                        text="60 Control copies, 2391 responding, 1434."))
    [h] = hints(c)
    assert (h["unit"], h["status"], h["traffic_id"]) == ("2391", "RS", "307-b-call_2-t0")


def test_nothing_from_a_dispatcher_alone_or_someone_elses_radio(tmp_path):
    c, clock, inc = setup(tmp_path)
    c.record_traffic(tx("307-c-call_3-t0", clock.t, "Fire/EMS 15", unit="60 Ctrl OP 7", tg=307, text="Responding."))
    c.record_traffic(tx("FG1-3", clock.t + 1, rig="E191", text="On location."))
    assert hints(c) == []


def test_only_our_fire_channels(tmp_path):
    c, clock, inc = setup(tmp_path)
    c.record_traffic(tx("309-d-call_4", clock.t, "Fire/EMS 18", unit="", tg=309, text="Engine 64 on location"))
    c.record_traffic(tx("SOUTH2-1", clock.t + 1, "South 2", text="Engine 64 on location"))
    assert hints(c) == []
    c.record_traffic(tx("333-e-call_5", clock.t + 2, "Airport Incident", unit="", tg=333, text="Engine 64 on location"))
    assert [h["unit"] for h in hints(c)] == ["E64"]


def test_data_file_channels(tmp_path):
    from tests.test_feed import lists_with
    p = lists_with(tmp_path, {})
    d = json.loads(p.read_text())
    d["status_hints"] = {"channels": ["PC repeater"], "talkgroups": [307]}
    p.write_text(json.dumps(d))
    clock = Clock()
    c = cad(tmp_path, clock, lists=str(p))
    _event_with(c, ["E64"], clock)
    clock.t += 30
    c.record_traffic(tx("FG1-1", clock.t, text="Engine 64 on location"))
    assert hints(c) == []
    c.record_traffic(tx("PCR-1", clock.t + 1, "PC repeater", text="Engine 64 on location"))
    assert [h["traffic_id"] for h in hints(c)] == ["PCR-1"]


def test_no_op_and_backwards_are_not_suggested(tmp_path):
    c, clock, inc = setup(tmp_path)
    e64 = uid(c, "E64")
    c.store.set_unit_status(e64, "OL", "Lt. Ruiz")
    clock.t += 10
    c.record_traffic(tx("FG1-1", clock.t, text="Engine 64 on location"))          # already so
    c.record_traffic(tx("FG1-2", clock.t + 1, text="Engine 64 responding"))       # backwards
    assert hints(c) == []
    c.record_traffic(tx("FG1-3", clock.t + 2, text="Engine 64 now responding to the second address"))   # said so
    assert [(h["unit"], h["status"]) for h in hints(c)] == [("E64", "RS")]
    # available while available (any free status): nothing
    c.store.set_unit_status(uid(c, "E62"), "AV", "Lt. Ruiz")
    clock.t += 10
    c.record_traffic(tx("FG1-4", clock.t, text="Engine 62 back in service"))
    assert all(h["unit"] != "E62" for h in hints(c))


def test_a_status_set_after_the_transmission_was_made_is_not_suggested(tmp_path):
    c, clock, inc = setup(tmp_path)
    heard = clock.t
    clock.t += 20                                  # the transcript comes late; the desk set it meanwhile
    c.store.set_unit_status(uid(c, "E64"), "OL", "Lt. Ruiz")
    c.record_traffic(tx("FG1-1", heard, text="Engine 64 on location"))
    assert hints(c) == []


def test_on_location_needs_a_call(tmp_path):
    clock = Clock()
    c = cad(tmp_path, clock)
    clock.t += 30
    c.record_traffic(tx("FG1-1", clock.t, text="Engine 64 on location"))
    assert hints(c) == []
    c.record_traffic(tx("FG1-2", clock.t + 1, text="Engine 64 out of service"))     # a status off any call: fine
    [h] = hints(c)
    assert (h["unit"], h["status"], h["incident_id"]) == ("E64", "OS", None)


def test_a_unit_not_on_the_call_heard_on_its_channel_goes_on_it_when_accepted(tmp_path):
    c, clock, inc = setup(tmp_path, ("E62",))
    c.record_traffic(tx("FG1-1", clock.t, text="Engine 64 on location"))     # FG1, no ID, one call: linked to it
    [h] = hints(c)
    assert (h["unit"], h["incident_id"], h["incident_number"], h["onto"]) == ("E64", inc["id"], inc["number"], True)
    c.hint(h["id"], "accept", {"operator": "Lt. Ruiz"})
    u = next(u for u in c.store.units() if u["name"] == "E64")
    assert (u["status"], u["incident_id"]) == ("OL", inc["id"])


def test_expiry_and_a_status_changed_by_any_means(tmp_path):
    c, clock, inc = setup(tmp_path)
    c.record_traffic(tx("FG1-1", clock.t, text="Engine 64 on location"))
    c.record_traffic(tx("FG1-2", clock.t, text="Engine 62 on location"))
    assert len(hints(c)) == 2
    clock.t += 60
    c.store.set_unit_status(uid(c, "E62"), "RS", "Lt. Ruiz")       # the desk set something else: gone
    assert [h["unit"] for h in hints(c)] == ["E64"]
    clock.t += 300                                                  # five minutes from when it was heard
    assert hints(c) == []
    rows = {r["unit"]: r["outcome"] for r in c.store.q("SELECT unit, outcome FROM status_hints")}
    assert rows == {"E64": "expired", "E62": "stale"}
    # and an old transmission (the startup backfill, a late re-run) never suggests anything
    c.record_traffic(tx("FG1-3", clock.t - 400, text="Engine 64 responding"))
    assert hints(c) == []


def test_accept_sets_the_status_with_where_it_came_from(tmp_path):
    c, clock, inc = setup(tmp_path)
    c.record_traffic(tx("FG1-1", clock.t, text="Engine 64 on location, nothing showing"))
    [h] = hints(c)
    out = c.hint(h["id"], "accept", {"operator": "Lt. Ruiz"})
    assert out["accepted"] and out["status"] == "OL"
    assert status(c, "E64") == "OL"
    ev = c.store.full(inc["id"])["events"][-1]
    assert (ev["kind"], ev["operator"]) == ("status", "Lt. Ruiz")
    assert ev["text"] == "E64 on location. Status from radio (heard 15:07:10 “Engine 64 on location”)"
    a = next(a for a in c.store.full(inc["id"])["assignments"] if a["name"] == "E64")
    assert a["onscene_at"] == clock.t
    assert hints(c) == []
    with pytest.raises(Exception, match="already accepted"):
        c.hint(h["id"], "accept", {})
    r = c.store.one("SELECT outcome, operator, prev_status FROM status_hints WHERE id = ?", (h["id"],))
    assert r == {"outcome": "accepted", "operator": "Lt. Ruiz", "prev_status": "DP"}


def test_command_accepted_takes_command(tmp_path):
    c, clock, inc = setup(tmp_path, ("E64", "2391"))
    c.store.set_unit_status(uid(c, "2391"), "OL", "Lt. Ruiz")
    clock.t += 10
    c.record_traffic(tx("FG1-1", clock.t, rig="2391", text="2391 on location, I'll be Westchester Avenue command"))
    [h] = hints(c)
    assert (h["unit"], h["status"]) == ("2391", "IC")
    c.hint(h["id"], "accept", {"operator": "Lt. Ruiz"})
    assert status(c, "2391") == "IC"
    assert c.store.full(inc["id"])["events"][-1]["text"].startswith("Command: 2391. Status from radio (heard ")


def test_dismiss_is_remembered_for_that_transmission(tmp_path):
    c, clock, inc = setup(tmp_path)
    c.record_traffic(tx("FG1-1", clock.t, text="Engine 64 on location"))
    [h] = hints(c)
    assert c.hint(h["id"], "dismiss", {"operator": "Lt. Ruiz"})["dismissed"]
    assert hints(c) == [] and status(c, "E64") == "DP"
    c.comms.retext("FG1-1", "Engine 64 on location.", "done")      # a re-run of the same transmission: not again
    c.record_traffic(tx("FG1-1", clock.t, text="Engine 64 on location"))
    assert hints(c) == []
    c.record_traffic(tx("FG1-2", clock.t + 5, text="Engine 64 on location"))      # said again: a new suggestion
    assert [h["traffic_id"] for h in hints(c)] == ["FG1-2"]


def test_a_rerun_that_says_something_else_replaces_or_withdraws(tmp_path):
    c, clock, inc = setup(tmp_path)
    c.record_traffic(tx("FG1-1", clock.t, text="Engine 64 responding"))
    assert [h["status"] for h in hints(c)] == ["RS"]
    c.comms.retext("FG1-1", "Engine 64 on location", "done")
    assert [h["status"] for h in hints(c)] == ["OL"]
    c.comms.retext("FG1-1", "Engine 64 copy", "done")
    assert hints(c) == []


def test_a_newer_suggestion_for_the_unit_supersedes(tmp_path):
    c, clock, inc = setup(tmp_path)
    c.record_traffic(tx("FG1-1", clock.t, text="Engine 64 responding"))
    c.record_traffic(tx("FG1-2", clock.t + 30, text="Engine 64 on location"))
    assert [(h["status"], h["traffic_id"]) for h in hints(c)] == [("OL", "FG1-2")]
    assert c.store.one("SELECT outcome FROM status_hints WHERE traffic_id = 'FG1-1'")["outcome"] == "superseded"


def test_api_permissions_and_activity(tmp_path):
    from sizeup.cloud.access import Identity
    from tests.test_roles import JULIAN, VAL, people
    c, clock, inc = setup(tmp_path)
    api = CadApi(c)
    people(c)
    c.record_traffic(tx("FG1-1", clock.t, text="Engine 64 on location"))
    c.record_traffic(tx("FG1-2", clock.t, text="Engine 62 on location"))
    by = {h["unit"]: h["id"] for h in hints(c)}

    def post(path, email):
        code, raw, _ = api.post(path, b"{}", Identity(email=email))
        return code, json.loads(raw)
    code, out = post(f"/api/cad/hint/{by['E64']}/accept", VAL)              # a viewer sees it, can't take it
    assert code == 403 and status(c, "E64") == "DP"
    assert sorted(h["unit"] for h in json.loads(api.get("/api/cad/state", "")[1])["status_hints"]) == ["E62", "E64"]
    code, out = post(f"/api/cad/hint/{by['E64']}/accept", JULIAN)
    assert code == 200 and status(c, "E64") == "OL"
    assert c.store.full(inc["id"])["events"][-1]["operator"] == "Julian <julian@example.org>"
    code, out = post(f"/api/cad/hint/{by['E62']}/dismiss", JULIAN)
    assert code == 200 and status(c, "E62") == "DP"
    acts = [a["summary"] for a in c.activity.rows(JULIAN, "action", None, 10)]
    assert f"Accepted the radio's E64 OL on {inc['number']}" in acts and "Dismissed the radio's E62 OL" in acts
    code, out = post("/api/cad/hint/9999/accept", JULIAN)
    assert code == 400 and out["error"] == "no suggestion 9999"


def test_the_precision_log(tmp_path):
    c, clock, inc = setup(tmp_path)
    c.record_traffic(tx("FG1-1", clock.t, text="Engine 64 on location"))
    c.record_traffic(tx("FG1-2", clock.t, text="Engine 62 on location"))
    by = {h["unit"]: h["id"] for h in hints(c)}
    c.hint(by["E64"], "accept", {"operator": "Lt. Ruiz"})
    c.hint(by["E62"], "dismiss", {"operator": "Lt. Ruiz"})
    rows = list(csv.DictReader(io.StringIO(c.export("status-hints.csv").decode("utf-8-sig"))))
    assert sorted((r["Unit"], r["Suggested"], r["Was"], r["Outcome"], r["Who"]) for r in rows) == [
        ("E62", "OL", "DP", "dismissed", "named"), ("E64", "OL", "DP", "accepted", "named")]
    assert rows[0]["Incident #"] == inc["number"] and rows[0]["Phrase"] == "on location"
