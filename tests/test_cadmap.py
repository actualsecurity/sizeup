"""The CAD maps and Overview: where each rig is drawn (sizeup/cad/mapview.py), home stations (data
file + a Settings units override set from the map, audited), the AVL position hook, and the overview's
summaries."""

import json
import time
from pathlib import Path

import pytest

from sizeup.cad import lists, mapview
from sizeup.cad.api import CadApi
from sizeup.cad.mapview import place
from sizeup.cad.service import Cad, Settings

ROOT = Path(__file__).resolve().parent.parent
HQ = {"name": "PCFD Headquarters", "ll": [-73.66692, 41.00253]}
INC = {"id": 7, "number": "26-000007", "lat": 41.0, "lon": -73.67}
E60 = {"id": 1, "name": "E60", "kind": "engine", "status": "AVAILABLE", "board": "main"}
MEDIC = {"id": 2, "name": "77A4", "kind": "ems", "status": "AVAILABLE", "board": "other"}


class Clock:
    def __init__(self, t=1_791_400_000.0):
        self.t = t

    def __call__(self):
        return self.t


def cad(tmp_path=None, clock=None, **kw):
    return Cad(Settings(db=str(tmp_path / "cad.sqlite") if tmp_path else "", **kw), log=lambda m: None,
               clock=clock or time.time)


def uid(c, name):
    return next(u["id"] for u in c.store.units() if u["name"] == name)


def marker(state, name):
    return next((m for m in state["map"]["markers"] if m["unit"] == name), None)


# -- placement (pure) ---------------------------------------------------------------------------------

def test_available_and_out_of_service_rigs_sit_at_their_station():
    m = place(E60, "free", HQ)
    assert (m["place"], m["look"], m["group"], m["line_to"]) == ("station", "available", "station:PCFD Headquarters", None)
    assert [m["lon"], m["lat"]] == HQ["ll"] and m["station"] == "PCFD Headquarters"
    oos = place(dict(E60, status="OUT OF SERVICE"), "oos", HQ)
    assert (oos["place"], oos["look"]) == ("station", "oos")


def test_dispatched_and_responding_rigs_stay_at_the_station_with_a_line_to_the_call():
    for role, look in (("dispatched", "dispatched"), ("", "dispatched"), ("enroute", "responding")):
        m = place(E60, role, HQ, INC)
        assert (m["place"], m["look"], m["incident_id"]) == ("station", look, 7)
        assert m["line_to"] == [INC["lon"], INC["lat"]]
    nopin = place(E60, "enroute", HQ, dict(INC, lat=None, lon=None))
    assert nopin["place"] == "station" and nopin["line_to"] is None          # its event has no map point


def test_on_location_rigs_are_fanned_at_the_incident():
    m = place(E60, "onscene", HQ, INC)
    assert (m["place"], m["group"], m["ring"], m["line_to"]) == ("incident", "incident:7", 0, None)
    assert (m["lon"], m["lat"]) == (INC["lon"], INC["lat"])


def test_no_station_not_drawn_unless_on_a_call():
    assert place(E60, "free", None) is None
    assert place(E60, "oos", None) is None
    m = place(E60, "enroute", None, INC)                 # going, no home to start from: outer ring at the call
    assert (m["place"], m["ring"], m["look"]) == ("incident", 1, "responding")
    assert place(E60, "onscene", None, INC)["ring"] == 0


def test_ems_police_and_agencies_only_when_on_an_event():
    assert place(MEDIC, "free", None) is None
    assert place(MEDIC, "free", HQ) is None              # even with a station: not ours to show
    m = place(MEDIC, "onscene", None, INC)
    assert (m["place"], m["group"], m["ring"]) == ("incident", "incident:7", 0)
    attached = place(MEDIC, "free", None, INC)           # attached to a non-emergency event, status unchanged
    assert attached["place"] == "incident" and attached["ring"] == 1


def test_a_live_position_overrides_station_and_incident():
    now = 1_791_400_000.0
    fix = {"lat": 41.01, "lon": -73.66, "at": now - 20}
    m = place(E60, "enroute", HQ, INC, fix, now)
    assert (m["place"], m["lat"], m["lon"], m["group"]) == ("position", 41.01, -73.66, "unit:E60")
    assert m["look"] == "responding" and m["line_to"] == [INC["lon"], INC["lat"]]
    assert place(E60, "onscene", HQ, INC, fix, now)["line_to"] is None
    assert place(E60, "free", None, None, fix, now)["place"] == "position"   # no station needed
    assert place(MEDIC, "free", None, None, fix, now) is None                # still only on an event
    stale = dict(fix, at=now - mapview.POSITION_MAX_AGE_S - 1)
    assert place(E60, "free", HQ, None, stale, now)["place"] == "station"
    for bad in ({"lat": "41", "lon": -73.6, "at": now}, {"lat": 141, "lon": -73.6, "at": now}, {"lat": 41, "lon": -73.6}, None):
        assert place(E60, "free", HQ, None, bad, now)["place"] == "station"


def test_map_slice_groups_slots_unset_list_and_pins():
    units = [dict(E60), dict(E60, id=3, name="E61"), dict(E60, id=4, name="L31", kind="ladder", station="PCFD Headquarters"),
             dict(E60, id=5, name="E14", station="PCFD Headquarters", status="ON LOCATION", incident_id=7),
             dict(MEDIC, incident_id=7, status="ON LOCATION"), dict(MEDIC, id=6, name="77A5"),
             dict(E60, id=8, name="L25", kind="ladder", mutual_aid=True)]
    for u in units[:2]:
        u["station"] = ""
    units[0]["station"] = "PCFD Headquarters"
    incs = [dict(INC, status="active", type="STRU", created_at=1), dict(id=8, number="26-000008", lat=None, lon=None,
                                                                      status="pending", created_at=2),
            dict(id=9, number="26-000003", lat=41.0, lon=-73.6, status="closed", created_at=0)]
    roles = {"AVAILABLE": "free", "ON LOCATION": "onscene"}
    s = mapview.map_slice(units, incs, roles.get, [HQ], now=0, joint=lambda c: "RB" if c == "E14" else "")
    at = {m["unit"]: m for m in s["markers"]}
    assert set(at) == {"E60", "L31", "E14", "77A4"}
    assert (at["E60"]["slot"], at["L31"]["slot"], at["E60"]["of"]) == (0, 1, 2)
    assert (at["E14"]["group"], at["E14"]["tag"], at["E14"]["slot"], at["77A4"]["slot"]) == ("incident:7", "RB", 0, 1)
    assert not at["77A4"]["fire"] and at["E60"]["fire"]
    assert [u["unit"] for u in s["unset"]] == ["E61"]    # mutual aid and EMS are never listed
    assert [p["id"] for p in s["incidents"]] == [7]      # open and on the map only
    assert s["stations"] == [HQ]


# -- home stations ----------------------------------------------------------------------------------

def test_station_unset_by_default_and_listed():
    c = cad()
    st = c.state()
    assert all(u["station"] == "" for u in st["units"])
    unset = [u["unit"] for u in st["map"]["unset"]]
    assert {"E60", "L31", "2398", "E14"} <= set(unset) and "77A4" not in unset and "L25" not in unset
    assert st["map"]["markers"] == [] and len(st["map"]["stations"]) == 5
    assert {f["name"] for f in st["map"]["stations"]} >= {"PCFD Headquarters", "Rye Brook Firehouse"}


def test_station_api_sets_audits_and_survives_restart(tmp_path):
    clock = Clock()
    c = cad(tmp_path, clock)
    api = CadApi(c)
    e60 = uid(c, "E60")
    code, body, _ = api.post(f"/api/cad/unit/{e60}/station", json.dumps({"station": "Station 9", "operator": "Lt. Ruiz"}).encode())
    assert code == 400 and "Washington Park" in json.loads(body)["error"]
    code, body, _ = api.post(f"/api/cad/unit/{e60}/station",
                             json.dumps({"station": "Washington Park", "operator": "Lt. Ruiz"}).encode())
    assert code == 200 and json.loads(body)["station"] == "Washington Park"
    m = marker(c.state(), "E60")
    assert (m["place"], m["station"], m["look"]) == ("station", "Washington Park", "available")
    assert "E60" not in [u["unit"] for u in c.state()["map"]["unset"]]
    api.post(f"/api/cad/unit/{e60}/station", json.dumps({"station": "Washington Park", "operator": "x"}).encode())   # no change
    c2 = cad(tmp_path, clock)
    assert next(u for u in c2.store.units() if u["name"] == "E60")["station"] == "Washington Park"
    page = c2.settings_get()["units"]                     # a Settings units override: Settings > Units shows it
    assert next(r for r in page["rows"] if r["code"] == "E60")["station"] == "Washington Park"
    assert "Rye Brook Firehouse" in page["stations"]
    assert c2.settings.overrides()["units"]["E60"]["station"] == "Washington Park"
    clock.t += 5
    assert CadApi(c2).post(f"/api/cad/unit/{e60}/station", b'{"station": "", "operator": "Capt. Ito"}')[0] == 200
    audit = json.loads(CadApi(c2).get("/api/cad/settings/audit", "section=units")[1])["audit"]
    assert [(a["operator"], a["key"], a["old"], a["new"]) for a in audit] == [
        ("Capt. Ito", "E60/station", "Washington Park", ""), ("Lt. Ruiz", "E60/station", "", "Washington Park")]
    assert marker(c2.state(), "E60") is None
    assert api.post("/api/cad/unit/99999/station", b'{"station": ""}')[0] == 400


def test_station_from_the_data_file_and_the_override_wins(tmp_path):
    d = json.loads((ROOT / "data" / "cad_lists.json").read_text())
    d["units"]["L31"]["station"] = "South End (Putnam & Mellor)"
    p = tmp_path / "lists.json"
    p.write_text(json.dumps(d))
    c = cad(lists=str(p))
    assert marker(c.state(), "L31")["station"] == "South End (Putnam & Mellor)"
    c.unit_station(uid(c, "L31"), {"station": "PCFD Headquarters"})
    assert marker(c.state(), "L31")["station"] == "PCFD Headquarters"
    d["units"]["L31"]["station"] = 5
    p.write_text(json.dumps(d))
    with pytest.raises(lists.ListsError, match="station"):
        lists.load(p)


def test_rigs_move_with_their_status_through_a_call():
    c = cad()
    for name, st in (("E60", "PCFD Headquarters"), ("L31", "PCFD Headquarters"), ("E14", "Rye Brook Firehouse")):
        c.unit_station(uid(c, name), {"station": st})
    inc = c.create({"type": "STRU", "address": "209 Westchester Ave", "lat": 41.0, "lon": -73.67, "operator": "x"})
    c.dispatch(inc["id"], {"unit_ids": [uid(c, "E60"), uid(c, "L31"), uid(c, "E14")], "operator": "x"})
    c.unit_status(uid(c, "E60"), {"status": "RESPONDING"})
    c.unit_status(uid(c, "L31"), {"status": "ON LOCATION"})
    st = c.state()
    e60, l31, e14 = (marker(st, n) for n in ("E60", "L31", "E14"))
    assert (e60["place"], e60["look"], e60["line_to"]) == ("station", "responding", [-73.67, 41.0])
    assert (l31["place"], l31["look"], l31["group"]) == ("incident", "onscene", f"incident:{inc['id']}")
    assert (e14["place"], e14["look"], e14["tag"]) == ("station", "dispatched", "RB")
    assert [p["id"] for p in st["map"]["incidents"]] == [inc["id"]]
    c.unit_status(uid(c, "E60"), {"status": "OUT OF SERVICE", "note": "pump"})
    assert (marker(c.state(), "E60")["look"], marker(c.state(), "E60")["place"]) == ("oos", "station")


def test_the_avl_hook_places_a_unit_and_a_broken_feed_is_ignored():
    clock = Clock()
    c = cad(clock=clock)
    c.positions = lambda: {"E61": {"lat": 41.02, "lon": -73.68, "at": clock.t - 5}, "E62": {"lat": 41.0, "lon": -73.6, "at": 1.0}}
    st = c.state()
    assert next(u for u in st["units"] if u["name"] == "E61")["position"] == {"lat": 41.02, "lon": -73.68, "at": clock.t - 5}
    assert "position" not in next(u for u in st["units"] if u["name"] == "E62")      # stale
    assert (marker(st, "E61")["place"], marker(st, "E61")["lat"]) == ("position", 41.02)
    assert marker(st, "E62") is None
    c.positions = lambda: 1 / 0
    assert marker(c.state(), "E61") is None


# -- overview -------------------------------------------------------------------------------------

def test_overview_summarises_each_open_incident():
    clock = Clock()
    c = cad(clock=clock)
    a = c.create({"type": "STRU", "address": "209 Westchester Ave", "lat": 41.0, "lon": -73.67, "operator": "x"})
    clock.t += 30
    b = c.create({"type": "ALARM", "subtype": "COMM", "address": "1 Main St", "operator": "x"})
    clock.t += 1
    n = c.create({"type": "NOTIF", "address": "Hydrant at King St", "operator": "x"})
    c.dispatch(a["id"], {"unit_ids": [uid(c, "E60"), uid(c, "L31")], "operator": "x"})
    c.unit_status(uid(c, "L31"), {"status": "ON LOCATION"})
    clock.t += 5
    c.record_traffic({"id": "FG1-1", "start": clock.t, "end": clock.t + 3, "source": "FG1", "talkgroup": None, "unit": "",
                      "rig": "L31", "radio_id": "", "text": "Ladder 31 on location, two story frame, nothing showing  ",
                      "status": "done"})
    c.note(a["id"], {"text": "E60 stated that hydrant is out", "units": ["E60"], "operator": "Lt. Ruiz"})
    ov = c.state()["overview"]
    assert [o["id"] for o in ov] == [a["id"], b["id"], n["id"]]                  # oldest first
    o = ov[0]
    assert (o["number"], o["type"], o["status"], o["emergency"], o["address"]) == (a["number"], "STRU", "active", True,
                                                                                 "209 Westchester Ave")
    assert [(u["name"], u["status"]) for u in o["units"]] == [("E60", "DISPATCHED"), ("L31", "ON LOCATION")]
    assert (o["radio"]["rig"], o["radio"]["channel"]) == ("L31", "FG1")
    assert o["radio"]["text"] == "Ladder 31 on location, two story frame, nothing showing"
    assert (o["note"]["kind"], o["note"]["text"], o["note"]["units"]) == ("note", "E60 stated that hydrant is out", ["E60"])
    assert o["channels"] == ["FG1"] and (o["lat"], o["lon"]) == (41.0, -73.67)
    assert ov[1]["radio"] is None and ov[1]["units"] == [] and ov[1]["channels"] == []
    assert ov[1]["note"]["kind"] == "created"                                    # no note yet: the newest entry
    assert ov[2]["emergency"] is False
    c.close(a["id"], {"disposition": c.lists.disposition_codes[0]})
    assert [o["id"] for o in c.state()["overview"]] == [b["id"], n["id"]]


def test_overview_snippet_is_cut():
    inc = {"id": 1, "status": "active", "created_at": 0, "assignments": [], "events": [],
           "radio": [{"rig": "E60", "channel": "FG1", "start": 1, "text": "word " * 100}]}
    r = mapview.overview([inc])[0]["radio"]
    assert len(r["text"]) <= mapview.SNIPPET and r["text"].endswith("…")
