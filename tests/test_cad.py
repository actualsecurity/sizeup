"""CAD: the lists data file, store, numbering, unit boards, run cards, dispatch wording and audio,
emergency / non-emergency routing, the bridge from the monitor, location ranking, CSV exports,
the v1 -> v2 migration, the HTTP API, and that nothing can transmit."""

import csv
import io
import json
import re
import shutil
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import wave
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

from sizeup.cad import dispatcher, export, lists, runcards
from sizeup.cad.api import CadApi
from sizeup.cad.service import Cad, Settings
from sizeup.cad.store import SCHEMA_VERSION, TZ, CadError, Store
from sizeup.cad.transmit import AudioPttTransmitter, NullTransmitter, make_transmitter
from sizeup.config import ConfigError, load
from sizeup.dispatch import parse
from sizeup.incidents import IncidentStore

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "cad_lists.json"
PAGE_TEXT = ("Port Chester Fire Department, respond to an activated fire alarm. 209 West Chester Avenue, "
             "cross of Pearl Street, Port Chester. Time out 1432.")
V1_SCHEMA = """
CREATE TABLE units (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL,
  station TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'available',
  status_at REAL NOT NULL,
  out_of_service TEXT NOT NULL DEFAULT '',
  spare INTEGER NOT NULL DEFAULT 0,
  notes TEXT NOT NULL DEFAULT '',
  sort INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE incidents (
  id INTEGER PRIMARY KEY,
  number TEXT UNIQUE,
  type TEXT NOT NULL DEFAULT '',
  priority INTEGER,
  address TEXT NOT NULL DEFAULT '',
  cross_street TEXT NOT NULL DEFAULT '',
  lat REAL, lon REAL, geo_label TEXT NOT NULL DEFAULT '',
  narrative TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL,
  source_ref TEXT UNIQUE,
  status TEXT NOT NULL,
  created_at REAL NOT NULL, accepted_at REAL, closed_at REAL
);
CREATE TABLE seq (year INTEGER PRIMARY KEY, last INTEGER NOT NULL);
CREATE TABLE assignments (
  id INTEGER PRIMARY KEY,
  incident_id INTEGER NOT NULL REFERENCES incidents(id),
  unit_id INTEGER NOT NULL REFERENCES units(id),
  dispatched_at REAL, enroute_at REAL, onscene_at REAL, cleared_at REAL
);
CREATE INDEX assignments_open ON assignments(unit_id) WHERE cleared_at IS NULL;
CREATE TABLE run_cards (
  id INTEGER PRIMARY KEY,
  type TEXT NOT NULL UNIQUE,
  spoken TEXT NOT NULL DEFAULT '',
  is_default INTEGER NOT NULL DEFAULT 0,
  sort INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE run_card_slots (
  card_id INTEGER NOT NULL REFERENCES run_cards(id) ON DELETE CASCADE,
  pos INTEGER NOT NULL,
  kind TEXT NOT NULL DEFAULT '',
  count INTEGER NOT NULL DEFAULT 1,
  unit TEXT NOT NULL DEFAULT ''
);
CREATE TABLE events (
  id INTEGER PRIMARY KEY,
  incident_id INTEGER REFERENCES incidents(id),
  unit_id INTEGER REFERENCES units(id),
  at REAL NOT NULL,
  kind TEXT NOT NULL,
  text TEXT NOT NULL,
  operator TEXT NOT NULL DEFAULT ''
);
CREATE INDEX events_incident ON events(incident_id);
CREATE TABLE dispatches (
  id INTEGER PRIMARY KEY,
  incident_id INTEGER NOT NULL REFERENCES incidents(id),
  at REAL NOT NULL,
  operator TEXT NOT NULL,
  text TEXT NOT NULL,
  unit_ids TEXT NOT NULL DEFAULT '[]',
  audio_path TEXT NOT NULL DEFAULT '',
  audio_note TEXT NOT NULL DEFAULT '',
  transmitted INTEGER NOT NULL DEFAULT 0,
  channel TEXT NOT NULL DEFAULT '',
  tx_note TEXT NOT NULL DEFAULT ''
);
"""


def local(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=TZ).timestamp()


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def make_cad(tmp_path=None, clock=None, geocoder=None, **kw):
    s = Settings(db=str(tmp_path / "cad.sqlite") if tmp_path else "", **kw)
    return Cad(s, geocoder=geocoder, log=lambda m: None, **({"clock": clock} if clock else {}))


def names(units):
    return [u["name"] for u in units]


def by_name(cad, name):
    return next(u for u in cad.store.units() if u["name"] == name)


def uid(cad, name):
    return by_name(cad, name)["id"]


def texts(cad, iid):
    return [e["text"] for e in cad.store.full(iid)["events"]]


def write_lists(tmp_path, **changes):
    d = json.loads(DATA.read_text())
    d.update(changes)
    p = tmp_path / "lists.json"
    p.write_text(json.dumps(d))
    return p


# -- the data file ------------------------------------------------------------------------------------

def test_lists_file_has_the_sheets_vocabulary():
    L = lists.load()
    assert L.type_codes == ("ALARM", "ALS", "BLS", "BRUSH", "DRILL", "HAZARD", "INVEST", "MA", "MVA", "MVF", "NOTIF",
                            "RECALL", "RESCUE", "SERVICE", "STRU")
    # the sheet's 51 subtypes are all there (the department has since added more, e.g. GAS-ODOR)
    assert len(L.subtype_codes) >= 51 and L.subtype_codes[:3] == ("CO-COMM", "CO-RESD", "COMM")
    assert len(L.disposition_codes) == 20 and "NOTIF MADE" in L.disposition_codes
    # The department's 17 status codes, in its order, then UNMANNED (kept: not in its list, to confirm).
    assert L.status_ids == ("AQ", "AV", "DP", "AK", "RS", "OL", "ST", "ET", "OA", "FR", "NR", "CB", "CN", "CE", "DO",
                            "IC", "OS", "UNMANNED")
    # Readable names shown, the sheet's codes stored; unclear codes stay codes.
    assert L.type_label("SERVICE", "BOAT") == "Service / Boat" and L.type_label("STRU") == "Structure fire"
    assert L.name("event_subtypes", "TREERD") == "Tree in road" and L.name("event_subtypes", "ECHO") == "ECHO"
    # Confirmed by PCFD: TRANS, CO-NO, CO-YES (named, said for any type; both CO calls stay emergency).
    assert (L.name("event_subtypes", "TRANS"), L.name("event_subtypes", "CO-NO"), L.name("event_subtypes", "CO-YES")) == (
        "Transformer", "CO \u2013 no symptoms", "CO \u2013 with symptoms")
    assert (L.phrase("HAZARD", "TRANS"), L.phrase("ALARM", "CO-NO"), L.phrase("ALARM", "CO-YES")) == (
        "a transformer", "a CO alarm, no symptoms", "a CO alarm with symptoms")
    assert L.phrase("ALARM", "COMM") == "a commercial fire alarm" and L.phrase("ALARM", "AIR") == "a fire alarm"
    assert L.is_emergency("ALARM", "CO-NO") and L.is_emergency("HAZARD", "CO-YES")
    assert [L.name("event_subtypes", c) for c in ("AIR", "MULTI", "NEXT", "CAD")] == ["AIR", "MULTI", "NEXT", "CAD"]
    assert L.name("dispositions", "TXALS") == "Transported ALS" and L.name("dispositions", "RMA") == "RMA"
    assert L.name("unit_statuses", "OL") == "On location" and L.name("unit_statuses", "CB") == "Call back of escalates"
    # The sheet's colors.
    assert {t["code"]: t["color"] for t in L.event_types}["SERVICE"] == "#8E7CC3"
    assert L.event_statuses["pending"]["bg"] == "#F2A51E" and L.event_statuses["active"]["bg"] == "#2D9E51"
    # Roles: what the store does with each status (a best guess, marked for the department to confirm).
    assert dict(zip(L.status_ids, (L.role(s) for s in L.status_ids))) == {
        "AQ": "free", "AV": "free", "DP": "dispatched", "AK": "", "RS": "enroute", "OL": "onscene", "ST": "",
        "ET": "enroute", "OA": "", "FR": "free", "NR": "free", "CB": "", "CN": "free", "CE": "free", "DO": "",
        "IC": "onscene", "OS": "oos", "UNMANNED": "oos"}
    assert L.status_for("free") == "AV" and L.status_for("dispatched") == "DP" and L.status_for("oos") == "OS"
    to_confirm = {s["code"] for s in L.unit_statuses if s.get("confirm")}
    assert to_confirm == {"AQ", "AK", "ST", "ET", "OA", "FR", "NR", "CB", "CN", "CE", "DO", "IC", "UNMANNED"}
    # The department's colors (CN, CE, DO are black in its sheet: a light grey here, readable on the dark theme).
    bg = {s["code"]: s["bg"] for s in L.unit_statuses}
    assert (bg["AV"], bg["DP"], bg["OL"], bg["ST"], bg["CB"], bg["OS"], bg["CN"]) == (
        "#4A78D0", "#F0A030", "#B84A30", "#66E044", "#8A2BE2", "#606060", "#A8ABB8")
    L.check_type("SERVICE", "LIFT")
    with pytest.raises(ValueError):
        L.check_type("FIRE")
    assert L.received_codes == ("PHONE", "WALK-IN", "RADIO", "60 CONTROL", "PD", "OTHER")


def test_unit_boards_in_the_sheets_order_with_gaps():
    L = lists.load()
    boards = {b["id"]: b for b in L.unit_boards}
    assert list(boards) == ["main", "ems", "ma-rye", "ma-purchase", "ma-harrison", "ma-west-harrison", "ma-requests",
                            "res-coned", "res-keyholder", "res-knox", "res-pd", "res-bat", "agencies"]
    main = boards["main"]
    # PCFD's codes in the sheet's order, then Rye Brook's; grouped by kind on the board, code-only ones last.
    assert main["units"][:10] == ["2391", "2392", "2393", "2394", "2395", "2396", "2397", "2398", "2399", "E58"]
    assert main["units"][26:30] == ["DPT239", "239EOC", "CHIEF239", "ROAD239"] and main["units"][-1] == "266RCL"
    assert len(main["units"]) == 41 and main["group_by"] == "kind" and not main.get("folded")
    assert main["rest_title"] == "Other PCFD & Rye Brook"
    # Everything else is folded until opened.
    assert all(b.get("folded") for k, b in boards.items() if k != "main")
    assert boards["ems"]["units"][:4] == ["7711", "7712", "7713", "77A1"] and len(boards["ems"]["units"]) == 19
    assert boards["ma-requests"]["units"][:2] == ["239FAST", "239_Cascd"] and "242_eNG" in boards["ma-requests"]["units"]
    assert boards["res-knox"]["units"] == ["KNOX_BOX", "KNOB_BOX1", "KNOX_BOX2", "KNOX_BOX3", "KNOX_BOX4", "KNOX_BOX5"]
    assert boards["res-pd"]["units"][:3] == ["PCPD", "PCPD1", "PCPD2"] and boards["res-pd"]["units"][-1] == "WCPD2"
    assert boards["res-bat"]["units"] == [f"BAT{n}" for n in range(10, 20)]
    assert boards["agencies"]["units"][:2] == ["BLDG_DPT", "BLDG_DPT1"] and "MAR_RPD" in boards["agencies"]["units"]
    codes = [c for c, _, _ in L.board_units()]
    assert len(codes) == len(set(codes)) == 250
    # Off the department's sheet: retired (kept with their history), not on a board.
    for c in ("CODE_ENFORCE", "60CAD", "60CAD1", "60CAD2"):
        assert c not in codes and L.units[c]["retired"]


def test_spoken_unit_names_kinds_and_spares():
    L = lists.load()
    said = {c: L.unit(c)["spoken"] for c in ("E60", "L31", "TL2", "R40", "U65", "M40", "M60", "2398", "2662", "L30",
                                              "E14", "L25", "E191", "77A1", "77M2", "PCPD", "CONED-G", "E58", "U66",
                                              "2394", "2661", "TL24", "R30", "M39", "U159", "2501", "7712", "77U1",
                                              "DPT239", "266RCL", "MA3", "242_eNG", "KNOB_BOX1")}
    assert said == {"E60": "Engine 60", "L31": "Ladder 31", "TL2": "Tower Ladder 2", "R40": "Rescue 40",
                    "U65": "Utility 65", "M40": "Marine 40", "M60": "Marine 60", "2398": "23 98", "2662": "26 62",
                    "L30": "Ladder 30", "E14": "Engine 14", "L25": "Ladder 25", "E191": "Engine 191",
                    "77A1": "77 Alpha 1", "77M2": "77 Medic 2", "PCPD": "PCPD", "CONED-G": "CONED-G",
                    "E58": "Engine 58", "U66": "Utility 66", "2394": "23 94", "2661": "26 61", "TL24": "Tower Ladder 24",
                    "R30": "Rescue 30", "M39": "Marine 39", "U159": "Utility 159", "2501": "25 01", "7712": "77 12",
                    # no meaning invented: shown and said as the code
                    "77U1": "77U1", "DPT239": "DPT239", "266RCL": "266RCL", "MA3": "MA3", "242_eNG": "242_eNG",
                    "KNOB_BOX1": "KNOB_BOX1"}
    named = {c for c in ("E58", "2394", "77A1", "7712", "77U1", "DPT239", "239EOC", "CHIEF239", "ROAD239", "MA3",
                         "242_eNG", "CONED-G", "BAT10", "SPCON") if L.unit(c)["named"]}
    assert named == {"E58", "2394", "77A1", "7712"}
    assert (L.unit("2398")["title"], L.unit("2394")["title"], L.unit("2421")["title"]) == ("Duty Officer", "", "")
    kinds = {c: L.unit(c)["kind"] for c in ("E60", "L31", "TL2", "R40", "U239", "M40", "2391", "2662", "77A3", "7711",
                                             "RBPD1", "DPW", "60CAD", "DPT239", "MA3", "239_Eng", "WCPD2", "BAT10",
                                             "DPT7700", "2165")}
    assert kinds == {"E60": "engine", "L31": "ladder", "TL2": "tower", "R40": "rescue", "U239": "utility",
                     "M40": "marine", "2391": "chief", "2662": "chief", "77A3": "ems", "7711": "ems", "RBPD1": "pd",
                     "DPW": "agency", "60CAD": "agency", "DPT239": "other", "MA3": "other", "239_Eng": "ma-request",
                     "WCPD2": "pd", "BAT10": "agency", "DPT7700": "ems", "2165": "chief"}
    assert L.unit("E59")["spare"] and L.unit("R41")["spare"] and not L.unit("E60")["spare"]
    # Rye Brook's units answer jointly: direct, not mutual aid. Rye's are mutual aid.
    for c in ("2661", "2662", "2663", "L30", "E14", "U42", "DPT266", "266RCL"):
        assert (L.unit(c)["department"], L.unit(c)["mutual_aid"], L.unit(c)["joint"]) == ("Rye Brook", False, "Rye Brook")
    assert L.unit("L25")["mutual_aid"] and L.unit("E191")["mutual_aid"] and L.unit("E191")["joint"] == ""
    # The four mutual-aid departments and the MA requests: mutual aid (sent only when named).
    for c, dept in (("E191", "Rye FD"), ("E238", "Purchase FD"), ("TL24", "Harrison FD"), ("E265", "West Harrison FD"),
                    ("2421", "Rye FD"), ("DPT250", "West Harrison FD")):
        assert (L.unit(c)["department"], L.response(c)) == (dept, "mutual_aid")
    assert L.response("239_Eng") == L.response("7700_ma") == "mutual_aid"
    # Port Chester EMS and the misc codes: other agencies (attach only).
    assert {L.response(c) for c in ("77A1", "7713", "CHIEF7700", "CONED-E5", "KEYHOLDER", "BAT19", "SPCON", "PCPD2")} == {"agency"}
    assert L.response("DPT239") == L.response("E58") == "fire"
    assert not L.unit("E60")["mutual_aid"] and L.unit("E60")["joint"] == "" and L.unit("U42")["spoken"] == "Utility 42"
    assert L.unit("NEW1") == {"kind": "agency", "spoken": "NEW1", "title": "", "spare": False, "department": "",
                              "mutual_aid": False, "response": "agency", "joint": "", "named": False}


def test_lists_file_errors_name_the_file_and_the_problem(tmp_path):
    with pytest.raises(lists.ListsError, match="cannot read"):
        lists.load(tmp_path / "nope.json")
    (tmp_path / "bad.json").write_text("{")
    with pytest.raises(lists.ListsError, match="not JSON"):
        lists.load(tmp_path / "bad.json")
    d = json.loads(DATA.read_text())
    for change, msg in ((dict(event_types=["ALARM", "ALARM"]), "twice"),
                        (dict(run_cards=[{"type": "FIRE", "slots": []}]), "run_cards"),
                        (dict(unit_statuses=[{"code": "AV", "role": "free"}]), "role 'oos'"),
                        (dict(type_colors=None, event_types=[{"code": "STRU", "color": "red"}]), "#rrggbb"),
                        (dict(dispatch_wording=dict(d["dispatch_wording"], new="{department} {nope}")), "placeholder"),
                        (dict(event_class={"*/NOPE": "non-emergency"}), "event_class")):
        p = write_lists(tmp_path, **change)
        with pytest.raises(lists.ListsError, match=re.escape(msg)):
            lists.load(p)
    # Plain strings are fine (code = name): an export can be dropped in as-is.
    L = lists.load(write_lists(tmp_path, dispositions=["COMPLETE", "FALSE"]))
    assert L.dispositions == ({"code": "COMPLETE", "name": "COMPLETE"}, {"code": "FALSE", "name": "FALSE"})


# -- store: numbering, fields, statuses ------------------------------------------------------------------

def test_numbers_yy_nnnnnn_per_local_year_and_queued_unnumbered(tmp_path):
    clock = Clock(local(2026, 12, 31, 23, 30))       # 04:30 UTC on Jan 1: still 2026 in Port Chester
    st = Store(tmp_path / "cad.sqlite", clock=clock)
    a = st.create_incident(type="SERVICE")
    p = st.create_incident(type="ALARM", source="60 Control page", source_ref="monitor:x", status="queued")
    b = st.create_incident(type="SERVICE")
    assert st.incident(a)["number"] == "26-000001" and st.incident(b)["number"] == "26-000002"
    assert st.incident(p)["number"] is None and st.incident(p)["status"] == "queued"
    clock.t = local(2027, 1, 1, 0, 30)
    assert st.accept(p)["number"] == "27-000001"                      # numbered when accepted
    assert st.incident(p)["status"] == "pending"
    assert st.incident(st.create_incident(type="SERVICE"))["number"] == "27-000002"
    with pytest.raises(CadError):
        st.accept(p)                                                   # already accepted
    st2 = Store(tmp_path / "cad.sqlite", clock=clock)
    assert st2.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 10
    assert st2.incident(st2.create_incident(type="SERVICE"))["number"] == "27-000003"
    with pytest.raises(sqlite3.IntegrityError):
        st2.create_incident(type="ALARM", source="60 Control page", source_ref="monitor:x", status="queued")


def test_event_info_fields_and_chronology_wording():
    cad = make_cad()
    inc = cad.create({"type": "SERVICE", "subtype": "LIFT", "address": "209 Westchester Ave", "caller_name": "COREY",
                      "caller_address": "1 Main St", "phone": "914-555-0100", "remarks": "boat taking on water",
                      "received_via": "PHONE", "operator": "JD"})
    assert (inc["type"], inc["subtype"], inc["caller_name"], inc["caller_address"], inc["phone"], inc["remarks"],
            inc["received_via"], inc["status"], inc["emergency"]) == (
        "SERVICE", "LIFT", "COREY", "1 Main St", "914-555-0100", "boat taking on water", "PHONE", "pending", 1)
    assert texts(cad, inc["id"]) == ["Event created (pending): Service / Lift at 209 Westchester Ave; received via Phone",
                                     "Caller info: COREY · 1 Main St · 914-555-0100"]
    for bad, msg in (({"type": "FIRE"}, "not an event type"), ({"type": "SERVICE", "subtype": "NOPE"}, "subtype"),
                     ({"type": ""}, "Pick a type"), ({"type": "SERVICE", "received_via": "FAX"}, "received_via"),
                     ({"type": "SERVICE", "emergency": "yes"}, "true or false")):
        with pytest.raises(CadError, match=msg):
            cad.create(bad)
    cad.update(inc["id"], {"type": "NOTIF", "subtype": "WATER", "phone": "", "operator": "JD"})
    assert texts(cad, inc["id"])[-1] == "Edited type: Notification / Water; now a non-emergency event; phone: (none)"


def test_close_needs_a_disposition_and_clears_units(tmp_path):
    clock = Clock(local(2026, 10, 7, 14, 0))
    cad = make_cad(tmp_path, clock=clock)
    iid = cad.create({"type": "MVA", "address": "Boston Post Rd"})["id"]
    e60 = uid(cad, "E60")
    cad.dispatch(iid, {"unit_ids": [e60], "operator": "JD"})
    clock.t += 60
    cad.unit_status(e60, {"status": "RS", "operator": "JD"})
    clock.t += 120
    cad.unit_status(e60, {"status": "OL"})
    a = cad.store.full(iid)["assignments"][0]
    assert (a["enroute_at"] - a["dispatched_at"], a["onscene_at"] - a["dispatched_at"]) == (60, 180)
    with pytest.raises(CadError):
        cad.unit_status(e60, {"status": "ON SCENE"})                   # not the sheet's word
    for body, msg in (({}, "Pick a disposition"), ({"disposition": "DONE"}, "not one of")):
        with pytest.raises(CadError, match=msg):
            cad.close(iid, body)
    cad.close(iid, {"disposition": "COMPLETE", "operator": "JD"})
    u = by_name(cad, "E60")
    assert u["status"] == "AV" and u["incident_id"] is None
    inc = cad.store.full(iid)
    assert inc["status"] == "closed" and inc["disposition"] == "COMPLETE"
    t = texts(cad, iid)
    assert t[-1] == "Event closed: Complete; cleared E60" and "E60 responding" in t and "E60 on location" in t
    assert "Event status: active" in t and "E60 dispatched" in t
    # Off any event, a status change goes to the unit log; unmanned can't be dispatched.
    cad.unit_status(e60, {"status": "OS", "note": "pump repair"})
    assert cad.store.unit_log()[0]["text"] == "E60 out of service (pump repair)"
    cad.unit_status(uid(cad, "E61"), {"status": "UNMANNED"})
    other = cad.create({"type": "STRU"})["id"]
    with pytest.raises(CadError, match="E61"):
        cad.dispatch(other, {"unit_ids": [uid(cad, "E61")], "operator": "JD"})
    hist = cad.store.q("SELECT old_status, new_status FROM unit_history WHERE unit_id = ? ORDER BY id", (e60,))
    assert [(h["old_status"], h["new_status"]) for h in hist] == [
        ("AV", "DP"), ("DP", "RS"), ("RS", "OL"),
        ("OL", "AV"), ("AV", "OS")]


def test_units_follow_the_boards_and_sync_keeps_status(tmp_path):
    cad = make_cad(tmp_path)
    units = cad.store.units()
    assert names(units)[:6] == ["2391", "2392", "2393", "2394", "2395", "2396"] and len(units) == 250
    assert {u["status"] for u in units} == {"AV"}                       # a new unit starts in free_status
    assert by_name(cad, "77A1")["spoken"] == "77 Alpha 1" and by_name(cad, "CONED-G")["board"] == "res-coned"
    assert by_name(cad, "242_eNG")["board"] == "ma-requests" and by_name(cad, "L25")["board"] == "ma-rye"
    cad.unit_status(uid(cad, "E62"), {"status": "OS"})
    # A new data file: E62 kept with its status, a new unit added, a dropped unit off the boards.
    d = json.loads(DATA.read_text())
    d["unit_boards"][0]["units"] = [c for c in d["unit_boards"][0]["units"] if c != "U42"] + ["E65"]
    p = tmp_path / "lists.json"
    p.write_text(json.dumps(d))
    cad2 = make_cad(tmp_path, lists=str(p))
    assert by_name(cad2, "E62")["status"] == "OS" and by_name(cad2, "E65")["kind"] == "agency"
    assert by_name(cad2, "U42")["board"] == "" and names(cad2.store.units())[-1] == "U42"


# -- run cards --------------------------------------------------------------------------------------

def test_recommend_follows_card_skipping_busy_and_unavailable():
    cad = make_cad()
    iid = cad.create({"type": "STRU", "address": "209 Westchester Ave"})["id"]
    r = runcards.recommend(cad.store, "STRU", "", iid)
    assert names(r["units"]) == ["E58", "E60", "E61", "L31", "R40", "2398", "E14", "L30"] and r["unfilled"] == []
    other = cad.create({"type": "SERVICE"})["id"]
    cad.unit_status(uid(cad, "L31"), {"status": "OS"})
    cad.store.assign(other, [uid(cad, n) for n in ("TL2", "E61", "2398")])
    r = runcards.recommend(cad.store, "STRU", "", iid)
    assert names(r["units"]) == ["E58", "E60", "E62", "R40", "E14", "L30"]   # L30 kept for its own (named) slot
    assert r["unfilled"] == ["no ladder/tower available", f"2398 not available (committed to {cad.store.incident(other)['number']})"]
    # Spares after regular apparatus; Rye Brook's engine is named on this card, so not taken by kind.
    cad.store.assign(iid, [uid(cad, n) for n in ("E58", "E60", "E62", "E63", "R40")])
    cad.store.assign(other, [uid(cad, "E64")])
    third = cad.create({"type": "STRU"})["id"]
    r = runcards.recommend(cad.store, "STRU", "", third)
    assert [(u["name"], u["spare"]) for u in r["units"]] == [("E59", True), ("R41", True), ("E14", False), ("L30", False)]
    assert r["unfilled"][:2] == ["only 1 of 3 engine available", "no ladder/tower available"]


def test_run_card_resolution_type_subtype_beats_type():
    cad = make_cad()
    cards = {c["type"]: c for c in runcards.cards(cad.store)}
    assert set(cards) == {"STRU", "ALARM/COMM", "ALARM/RESD", "ALARM/CO-COMM", "ALARM/CO-RESD", "HAZARD/GAS",
                          "HAZARD/WIRES", "BRUSH", "MVA", "MVA/EXTRI", "RESCUE/WATER", "SERVICE", "ALS", "BLS", "MA",
                          "INVEST"}
    assert all(c["is_default"] for c in cards.values()) and cards["ALARM/COMM"]["phrase"] == "a commercial fire alarm"
    assert runcards.resolve(cad.store, "MVA", "EXTRI")["type"] == "MVA/EXTRI"
    assert runcards.resolve(cad.store, "MVA", "CAR")["type"] == "MVA"          # no MVA/CAR card: the type's
    assert runcards.resolve(cad.store, "ALARM", "TROUB") is None               # no ALARM card at all
    r = runcards.recommend(cad.store, "ALARM", "TROUB")
    assert r["units"] == [] and r["note"] == "No run card for Alarm / Trouble: add units by hand"
    cad.save_run_card({"type": "ALARM", "slots": [{"kind": "engine", "count": 1}]})
    assert runcards.resolve(cad.store, "ALARM", "TROUB")["type"] == "ALARM"
    assert runcards.resolve(cad.store, "ALARM", "COMM")["type"] == "ALARM/COMM"
    for bad in ([], [{"kind": "helicopter"}], [{"unit": "E99"}], [{"kind": "engine", "count": 0}], "x"):
        with pytest.raises(CadError):
            cad.save_run_card({"type": "ALARM/RESD", "slots": bad})
    with pytest.raises(CadError, match="event type"):
        cad.save_run_card({"type": "FIRE", "slots": [{"kind": "engine"}]})
    c = cad.save_run_card({"type": "ALARM/RESD", "slots": [{"kind": "engine", "count": 2}, {"unit": "2398"}]})
    assert c["slots"] == [{"kind": "engine", "count": 2}, {"unit": "2398"}] and not c["is_default"]
    cad.delete_run_card({"type": "ALARM/RESD"})
    assert runcards.resolve(cad.store, "ALARM", "RESD")["type"] == "ALARM"
    assert runcards.seed(cad.store) == 0                                # never re-seeded over edits


def test_page_types_map_to_sheet_types_or_leave_a_note():
    assert runcards.from_page("Fire alarm", "fire", PAGE_TEXT) == ("ALARM", "", "commercial or residential not stated on the page")
    assert runcards.from_page("Fire alarm", "fire", "residential fire alarm, 5 King St") == ("ALARM", "RESD", "")
    assert runcards.from_page("Fire alarm", "fire", "commercial alarm") == ("ALARM", "COMM", "")
    assert runcards.from_page("CO alarm", "hazard", "co alarm, residential") == ("ALARM", "CO-RESD", "")
    assert runcards.from_page("CO alarm", "hazard", "carbon monoxide")[:2] == ("ALARM", "")
    assert runcards.from_page("Working fire", "fire", "") == ("STRU", "", "")
    assert runcards.from_page("MVA", "rescue", "mva with entrapment") == ("MVA", "EXTRI", "")
    assert runcards.from_page("Wires down", "hazard", "") == ("HAZARD", "WIRES", "")
    assert runcards.from_page("Breathing problem", "ems", "6 delta 1") == (
        "", "", "EMS (page: breathing problem): ALS or BLS not stated on the page")
    assert runcards.from_page("EMS assist", "ems", "assist the ALS unit") == ("ALS", "", "")
    assert runcards.from_page("Unknown", "unknown", "") == ("", "", "type not recognised (page)")


# -- emergency / non-emergency ----------------------------------------------------------------------------

def test_emergency_class_from_type_with_override():
    L = lists.load()
    assert not L.is_emergency("NOTIF") and not L.is_emergency("SERVICE", "WATER") and not L.is_emergency("HAZARD", "NOTIF-PD")
    assert L.is_emergency("RESCUE", "WATER") and L.is_emergency("STRU") and L.is_emergency("SERVICE", "BOAT")
    cad = make_cad()
    road = cad.create({"type": "NOTIF", "address": "Westchester Ave", "remarks": "road closed for paving",
                       "until_at": local(2026, 10, 10, 18, 0)})
    assert road["emergency"] == 0 and road["until_at"] == local(2026, 10, 10, 18, 0) and road["number"]
    forced = cad.create({"type": "NOTIF", "emergency": True})
    assert forced["emergency"] == 1
    assert cad.create({"type": "STRU", "emergency": False})["emergency"] == 0


def test_accepting_a_queued_page_routes_by_class():
    cad = make_cad()
    _, inc = monitor_incident("Port Chester Fire, respond to a residential fire alarm at 5 King Street")
    p = cad.from_monitor(inc)
    assert (p["status"], p["number"], p["received_via"]) == ("queued", None, "60 CONTROL")
    a = cad.accept(p["id"], {"operator": "JD"})
    assert (a["status"], a["emergency"]) == ("pending", 1) and re.fullmatch(r"\d{2}-000001", a["number"])
    # A page the operator files as a notification: accepted straight into Non-emergency events.
    _, inc2 = monitor_incident("Port Chester Fire, be advised, hydrant out of service at 10 Grove Street")
    q = cad.from_monitor(inc2)
    with pytest.raises(CadError, match="Pick a type"):
        cad.accept(q["id"], {})
    b = cad.accept(q["id"], {"type": "NOTIF", "operator": "JD"})
    assert (b["status"], b["emergency"]) == ("pending", 0)
    assert texts(cad, q["id"])[-1].startswith("Accepted as") and "(non-emergency): Notification" in texts(cad, q["id"])[-1]


def test_non_emergency_events_get_no_dispatch_but_can_have_units():
    cad = make_cad()
    iid = cad.create({"type": "NOTIF", "subtype": "WATER", "address": "10 Grove St", "remarks": "hydrant OOS"})["id"]
    r = cad.recommend(iid, {})
    assert r["units"] == [] and r["note"].startswith("Non-emergency event")
    e60 = uid(cad, "E60")
    for fn in (cad.dispatch, cad.preview):
        with pytest.raises(CadError, match="Non-emergency"):
            fn(iid, {"unit_ids": [e60], "operator": "JD"})
    cad.attach(iid, {"unit_ids": [e60], "operator": "JD"})
    inc = cad.store.full(iid)
    assert inc["status"] == "active" and inc["dispatches"] == [] and texts(cad, iid)[-1] == "Attached E60"
    assert by_name(cad, "E60")["status"] == "AV" and by_name(cad, "E60")["incident_number"] == inc["number"]
    cad.note(iid, {"text": "DPW notified", "operator": "JD"})
    cad.close(iid, {"disposition": "NOTIF MADE", "operator": "JD"})
    assert by_name(cad, "E60")["incident_id"] is None


def test_open_non_emergency_events_survive_restarts_however_old(tmp_path):
    clock = Clock(local(2026, 10, 4, 9, 0))
    cad = make_cad(tmp_path, clock=clock)
    iid = cad.create({"type": "NOTIF", "remarks": "Main St closed", "until_at": local(2026, 10, 9, 17, 0)})["id"]
    closed = cad.create({"type": "NOTIF"})["id"]
    cad.close(closed, {"disposition": "COMPLETE"})
    clock.t = local(2026, 10, 7, 15, 0)                  # three days later, a restart
    cad2 = make_cad(tmp_path, clock=clock)
    listed = {i["id"]: i for i in cad2.store.incidents()}
    assert iid in listed and listed[iid]["emergency"] is False and listed[iid]["until_at"] == local(2026, 10, 9, 17, 0)
    assert closed not in listed                          # closed ones drop off after 12 h


# -- dispatch wording and audio ------------------------------------------------------------------------------

def test_dispatch_wording_new_add_on_upgrade_full():
    inc = {"address": "169 North Main St, Port Chester"}
    at = local(2026, 10, 7, 15, 13)
    kw = dict(department="Port Chester Fire", incident=inc, at=at)
    assert dispatcher.compose("new", units=["Engine 60", "Ladder 31"], phrase="a commercial fire alarm", **kw) == (
        "Port Chester Fire, Engine 60, Ladder 31, respond to 169 North Main Street, for a commercial fire alarm. "
        "Time out 15:13.")
    assert dispatcher.compose("new", units=["Engine 60"], phrase="a structure fire", full=True, **kw) == (
        "Port Chester Fire, full department response, respond to 169 North Main Street, for a structure fire. "
        "Time out 15:13.")
    t = dispatcher.compose("add_on", units=["Engine 61"], phrase="a commercial fire alarm",
                           already=[("responding", ["Engine 60"]), ("on location", ["Ladder 31", "2398"])], **kw)
    assert t == ("Port Chester Fire, Engine 61, added to the assignment. Engine 60 responding, Ladder 31 and 2398 on "
                 "location. 169 North Main Street, commercial fire alarm. Time out 15:13.")
    assert dispatcher.compose("add_on", units=["Engine 61", "Rescue 40"], phrase="a fire alarm",
                              department="Port Chester Fire", incident={}, at=at) == (
        "Port Chester Fire, Engine 61 and Rescue 40, added to the assignment. Fire alarm. Time out 15:13.")
    up = dict(phrase="a commercial fire alarm", info="smoke in the structure", **kw)
    assert dispatcher.compose("upgrade", units=[], new_phrase="a structure fire", full=True, **up) == (
        "Port Chester Fire, upgrade on your last alarm, 169 North Main Street. Smoke in the structure. "
        "Now a structure fire, full department response. Time out 15:20.".replace("15:20", "15:13"))
    assert dispatcher.compose("upgrade", units=["Engine 62", "Rescue 40"], new_phrase="a structure fire", **up) == (
        "Port Chester Fire, upgrade on your last alarm, 169 North Main Street. Smoke in the structure. "
        "Now a structure fire. Engine 62, Rescue 40, respond. Time out 15:13.")
    # The wording is the data file's: a department edit changes it without code.
    w = dict(lists.load().wording, full_response="all companies")
    assert "all companies, respond" in dispatcher.compose("new", units=[], full=True, wording=w, **kw)
    # Spoken the dispatch way (sizeup/cad/speech.py), wherever the location sits.
    assert dispatcher.speech(t, dispatcher.where(inc)).endswith(
        "and twenty-three ninety-eight on location. One sixty-nine North Main Street, commercial fire alarm. "
        "Time out fifteen thirteen.")
    assert dispatcher.spoken_street("St Marys Pl, Rye Brook") == "St Marys Place, Rye Brook"   # other towns said
    assert dispatcher.spoken_street("12 N Main St") == "12 North Main Street"


def test_service_dispatch_add_on_and_upgrade():
    clock = Clock(local(2026, 10, 7, 15, 13))
    cad = make_cad(clock=clock)
    iid = cad.create({"type": "ALARM", "subtype": "COMM", "address": "169 North Main St"})["id"]
    first = cad.dispatch(iid, {"unit_ids": [uid(cad, "E60"), uid(cad, "L31"), uid(cad, "2398")], "operator": "JD"})
    assert first["kind"] == "dispatch" and first["text"].startswith(
        "Port Chester Fire, Engine 60, Ladder 31, 23 98, respond to 169 North Main Street, for a commercial fire alarm.")
    cad.unit_status(uid(cad, "E60"), {"status": "RS"})
    cad.unit_status(uid(cad, "L31"), {"status": "OL"})
    cad.unit_status(uid(cad, "2398"), {"status": "OL"})
    pv = cad.preview(iid, {"unit_ids": [uid(cad, "E61")]})
    add = cad.dispatch(iid, {"unit_ids": [uid(cad, "E61")], "operator": "JD"})
    assert pv["text"] == add["text"] == ("Port Chester Fire, Engine 61, added to the assignment. Engine 60 responding, "
                                         "Ladder 31 and 23 98 on location. 169 North Main Street, commercial fire alarm. "
                                         "Time out 15:13.")
    assert add["kind"] == "add-on" and "E61 added to the assignment" in texts(cad, iid)
    with pytest.raises(CadError, match="Nothing to upgrade"):
        cad.upgrade(iid, {"operator": "JD"})
    # The upgrade recommends only what the new card adds over what's there.
    rec = cad.recommend(iid, {"type": "STRU"})
    assert names(rec["units"]) == ["E58", "R40", "E14", "L30"]
    clock.t = local(2026, 10, 7, 15, 20)
    body = {"type": "STRU", "info": "smoke in the structure", "full": True, "unit_ids": [u["id"] for u in rec["units"]],
            "operator": "JD"}
    pv = cad.upgrade(iid, dict(body, preview=True))
    assert cad.store.incident(iid)["type"] == "ALARM"                   # a preview changes nothing
    up = cad.upgrade(iid, body)
    assert pv["text"] == up["text"] == ("Port Chester and Rye Brook Fire, upgrade on your last alarm, 169 North Main "   # E14, L30 go
                                        "Street. Smoke in the structure. Now a structure fire, full department response. "
                                        "Time out 15:20.")
    inc = cad.store.full(iid)
    assert (inc["type"], inc["subtype"]) == ("STRU", "") and [d["kind"] for d in inc["dispatches"]] == ["dispatch", "add-on", "upgrade"]
    t = texts(cad, iid)
    assert "Upgrade: Alarm / Commercial → Structure fire; smoke in the structure; full department response" in t
    assert "E58 dispatched (upgrade)" in t and by_name(cad, "R40")["status"] == "DP"
    fresh = cad.create({"type": "STRU"})["id"]
    with pytest.raises(CadError, match="Dispatch first"):
        cad.upgrade(fresh, {"info": "x", "operator": "JD"})


# -- subtype detail, dispatch remarks, the lead-in and each department's tones ---------------------------------

def test_subtype_detail_is_always_said():
    L = lists.load()
    assert L.phrase("MVA", "MULTINJU") == "a motor vehicle accident with multiple injuries"
    assert (L.phrase("MVA", "INJURIES"), L.phrase("MVA", "NOINJ")) == (
        "a motor vehicle accident with injuries", "a motor vehicle accident, no injuries")
    assert L.phrase("MVA", "EXTRI") == "a motor vehicle accident with entrapment"      # TYPE/SUBTYPE wins
    assert (L.phrase("MVF", "CAR"), L.phrase("ALARM", "SPRINK"), L.phrase("ALARM", "TROUB")) == (
        "a car fire", "a sprinkler alarm", "a fire alarm, trouble signal")
    assert L.phrase("HAZARD", "TREERD") == "a hazardous condition, tree down in the road"
    assert L.phrase("SERVICE", "WATER") == "a service call, water condition" and L.phrase("RESCUE", "WATER") == "a water rescue"
    assert L.phrase("HAZARD", "CO-RESD") == "a residential carbon monoxide alarm"
    # Unconfirmed codes have no phrase: the type's is said.
    for code in ("ECHO", "AIR", "MULTI", "NEXT", "CAD"):
        assert L.phrase("ALARM", code) == "a fire alarm" and f"*/{code}" not in L.spoken
    for code in ("INJURIES", "MULTINJU", "NOINJ", "EXTRI", "CAR", "TRUCK", "BOAT", "WIRES", "TREE", "TREERD", "GAS",
                 "INSIDE", "OUTSIDE", "SPRINK", "ELEVATOR", "LIFT", "WATER", "ANIMAL", "APPLIANC", "HEAT", "LOST",
                 "VEHICLE", "WEATHER", "STRUCTURAL", "COMM", "RESD", "TROUB", "CO-COMM", "CO-RESD"):
        assert f"*/{code}" in L.spoken, code
    # A type with no phrase of its own: its name stands in once.
    assert L.phrase("DRILL", "MULTINJU") == "a drill with multiple injuries"
    # A run card's phrase for the type stands for the type's; the subtype is still said.
    cad = make_cad()
    cad.save_run_card({"type": "MVA", "spoken": "an MVA", "slots": [{"kind": "engine", "count": 1}]})
    assert cad._phrase("MVA", "MULTINJU") == "an MVA with multiple injuries" and cad._phrase("MVA", "") == "an MVA"
    with pytest.raises(lists.ListsError, match="placeholder"):
        tmp = Path(__import__("tempfile").mkdtemp())
        lists.load(write_lists(tmp, spoken={"MVA": "{type} crash"}))


def test_remarks_cleaned_for_speech():
    c = dispatcher.clean_remarks
    assert c("  two vehicles,\n one on its side   fluid leaking ") == "two vehicles, one on its side fluid leaking."
    assert c("pt trapped w/ head injury, car vs pole & tree") == "pt trapped with head injury, car versus pole and tree."
    assert c("2 cars W/O injuries") == "2 cars without injuries."
    assert c("smoke showing!") == "smoke showing!" and c("caller says,") == "caller says." and c("   ") == ""
    assert c("ave w/b and 1/2 ton truck") == "ave w/b and 1/2 ton truck."          # not shorthand: left alone


def test_remarks_wording_for_each_dispatch_kind():
    inc = {"address": "209 Westchester Ave, Port Chester", "cross_street": "Pearl St"}
    kw = dict(department="Port Chester Fire", incident=inc, at=local(2026, 10, 7, 15, 13),
              phrase="a motor vehicle accident with multiple injuries")
    r = "two vehicles, one on its side,  fluid leaking"
    assert dispatcher.compose("new", units=["Engine 60", "Rescue 40"], remarks=r, **kw) == (
        "Port Chester Fire, Engine 60, Rescue 40, respond to 209 Westchester Avenue, cross of Pearl Street, for a motor "
        "vehicle accident with multiple injuries. Reported: two vehicles, one on its side, fluid leaking. Time out 15:13.")
    assert dispatcher.compose("new", units=["Engine 60"], **kw) == (
        "Port Chester Fire, Engine 60, respond to 209 Westchester Avenue, cross of Pearl Street, for a motor vehicle "
        "accident with multiple injuries. Time out 15:13.")
    assert dispatcher.compose("new", units=["Engine 60"], full=True, remarks="people trapped", **kw).endswith(
        "full department response, respond to 209 Westchester Avenue, cross of Pearl Street, for a motor vehicle "
        "accident with multiple injuries. Reported: people trapped. Time out 15:13.")
    assert dispatcher.compose("new", units=["Engine 60"], remarks="car fire", **dict(kw, incident={})) == (
        "Port Chester Fire, Engine 60, respond for a motor vehicle accident with multiple injuries. Reported: car fire. "
        "Time out 15:13.")
    assert dispatcher.compose("add_on", units=["Engine 61"], already=[("responding", ["Engine 60"])], remarks=r, **kw) == (
        "Port Chester Fire, Engine 61, added to the assignment. Engine 60 responding. 209 Westchester Avenue, cross of "
        "Pearl Street, motor vehicle accident with multiple injuries. Reported: two vehicles, one on its side, fluid "
        "leaking. Time out 15:13.")
    up = dict(kw, info="car on fire")
    assert dispatcher.compose("upgrade", units=[], **up) == (
        "Port Chester Fire, upgrade on your last alarm, 209 Westchester Avenue, cross of Pearl Street. Car on fire. "
        "Time out 15:13.")
    assert dispatcher.compose("upgrade", units=["Engine 62"], new_phrase="a vehicle fire", remarks="exposure to a house",
                              **up) == (
        "Port Chester Fire, upgrade on your last alarm, 209 Westchester Avenue, cross of Pearl Street. Car on fire. "
        "Now a vehicle fire. Engine 62, respond. Reported: exposure to a house. Time out 15:13.")


def test_remarks_lead_in_is_the_data_files():
    kw = dict(department="Port Chester Fire", incident={"address": "1 King St"}, units=["Engine 60"], phrase="a brush fire",
              at=local(2026, 10, 7, 15, 13), remarks="near the tracks")
    assert lists.load().wording["remarks"] == "Reported: {remarks}"
    w = dict(lists.load().wording, remarks="Caller reports {remarks}")
    assert dispatcher.compose("new", wording=w, **kw) == (
        "Port Chester Fire, Engine 60, respond to 1 King Street, for a brush fire. Caller reports near the tracks. "
        "Time out 15:13.")
    # A template written before remarks existed still says them, before the time out.
    w = dict(w, new="{department}, {units}, respond to {location}, for {type}. Time out {time}.")
    assert dispatcher.compose("new", wording=w, **kw).endswith("brush fire. Caller reports near the tracks. Time out 15:13.")
    with pytest.raises(lists.ListsError, match="placeholder"):
        d = json.loads(DATA.read_text())
        tmp = Path(__import__("tempfile").mkdtemp())
        lists.load(write_lists(tmp, dispatch_wording=dict(d["dispatch_wording"], remarks="Reported: {text}")))


def test_service_remarks_stored_shown_and_logged(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)              # text only: no `say` needed
    clock = Clock(local(2026, 10, 7, 15, 13))
    cad = make_cad(clock=clock)
    iid = cad.create({"type": "MVA", "subtype": "MULTINJU", "address": "209 Westchester Ave", "cross_street": "Pearl St",
                      "remarks": "two vehicles, one on its side"})["id"]
    body = {"unit_ids": [uid(cad, "E60"), uid(cad, "R40")], "remarks": " two vehicles,  one on its side, fluid leaking "}
    pv = cad.preview(iid, body)
    d = cad.dispatch(iid, dict(body, operator="JD"))
    want = ("Port Chester Fire, Engine 60, Rescue 40, respond to 209 Westchester Avenue, cross of Pearl Street, for a "
            "motor vehicle accident with multiple injuries. Reported: two vehicles, one on its side, fluid leaking. "
            "Time out 15:13.")
    assert pv["text"] == d["text"] == want and pv["remarks"] == d["remarks"] == "two vehicles, one on its side, fluid leaking."
    clock.t += 60
    add = cad.dispatch(iid, {"unit_ids": [uid(cad, "E61")], "operator": "JD", "remarks": "w/ fluid on the road"})
    assert add["text"].endswith("motor vehicle accident with multiple injuries. Reported: with fluid on the road. Time out 15:14.")
    clock.t += 60
    up = cad.upgrade(iid, {"info": "one trapped", "unit_ids": [uid(cad, "L31")], "operator": "JD"})
    assert "Reported" not in up["text"] and up["remarks"] == ""
    up2 = cad.upgrade(iid, {"remarks": "second patient", "operator": "JD"})
    assert up2["text"].endswith("upgrade on your last alarm, 209 Westchester Avenue, cross of Pearl Street. Reported: "
                                "second patient. Time out 15:15.")
    full = cad.store.full(iid)
    assert [(x["kind"], x["remarks"]) for x in full["dispatches"]] == [
        ("dispatch", "two vehicles, one on its side, fluid leaking."), ("add-on", "with fluid on the road."),
        ("upgrade", ""), ("upgrade", "second patient.")]
    assert all(x["tones"] == "Port Chester" for x in full["dispatches"])
    t = texts(cad, iid)
    assert t.count("Dispatch remarks: two vehicles, one on its side, fluid leaking.") == 1
    assert "Dispatch remarks: with fluid on the road." in t and "Dispatch remarks: second patient." in t
    assert sum(x.startswith("Dispatch remarks:") for x in t) == 3
    assert t.index(f"Dispatch, not transmitted ({d['detail']}): {want}") + 1 == t.index(
        "Dispatch remarks: two vehicles, one on its side, fluid leaking.")
    # No remarks, no sentence.
    other = cad.create({"type": "BRUSH", "address": "1 King St"})["id"]
    assert "Reported" not in cad.dispatch(other, {"unit_ids": [uid(cad, "E62")], "operator": "JD", "remarks": "  "})["text"]
    assert cad.store.full(other)["dispatches"][0]["remarks"] == ""
    with pytest.raises(CadError, match="remarks: at most 500 characters"):
        cad.preview(iid, {"unit_ids": [uid(cad, "E63")], "remarks": "x" * 501})


def lead(cad, iid, codes, **body):
    return cad.preview(iid, dict(body, unit_ids=[uid(cad, c) for c in codes]))


def test_lead_in_says_rye_brook_when_their_units_go(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    cad = make_cad(clock=Clock(local(2026, 10, 7, 15, 13)))
    iid = cad.create({"type": "STRU", "address": "122 South Regent St"})["id"]
    assert lead(cad, iid, ["E63"])["text"].startswith("Port Chester Fire, Engine 63, respond to 122 South Regent Street")
    assert lead(cad, iid, ["E63", "L30"])["text"].startswith(
        "Port Chester and Rye Brook Fire, Engine 63, Ladder 30, respond to 122 South Regent Street")
    assert lead(cad, iid, ["E14", "L30"])["text"].startswith("Rye Brook Fire, Engine 14, Ladder 30, respond to 122 South")
    assert lead(cad, iid, ["E14"], full=True)["text"].startswith(
        "Port Chester and Rye Brook Fire, full department response, respond")
    # Add-on: what's on the assignment counts too.
    cad.dispatch(iid, {"unit_ids": [uid(cad, "E60"), uid(cad, "L30")], "operator": "JD"})
    assert lead(cad, iid, ["E61"])["text"].startswith("Port Chester and Rye Brook Fire, Engine 61, added to the assignment")
    rb = cad.create({"type": "BRUSH", "address": "940 King St, Rye Brook"})["id"]
    first = cad.dispatch(rb, {"unit_ids": [uid(cad, "E14")], "operator": "JD"})
    assert first["text"].startswith("Rye Brook Fire, Engine 14, respond to 940 King Street, Rye Brook, for a brush fire.")
    assert lead(cad, rb, ["U42"])["text"].startswith("Rye Brook Fire, Utility 42, added to the assignment.")
    assert lead(cad, rb, ["E62"])["text"].startswith("Port Chester and Rye Brook Fire, Engine 62, added to the assignment.")
    # Full department response on an event Rye Brook is already on.
    up = cad.upgrade(rb, {"full": True, "preview": True})
    assert up["text"].startswith("Port Chester and Rye Brook Fire, upgrade on your last alarm") and "Full department" in up["text"]
    # The lead-in is logged as part of the dispatch text.
    assert any(t.startswith("Dispatch, not transmitted") and ": Rye Brook Fire, Engine 14" in t for t in texts(cad, rb))
    w = dispatcher.lead_in
    assert w("Port Chester Fire", []) == "Port Chester Fire" and w("Port Chester Fire", ["Rye Brook"], own=False) == (
        "Port Chester and Rye Brook Fire")                                # no joint_only text: the joint lead-in


PC = [(349.1, 1.0), (311.1, 3.0)]


def test_tone_plan_by_department(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    cad = make_cad(clock=Clock(local(2026, 10, 7, 15, 13)))
    iid = cad.create({"type": "STRU", "address": "122 South Regent St"})["id"]
    p = cad._plan(cad.store.incident(iid), [by_name(cad, "E63")], "new", False)[1]
    assert p == {"groups": [PC], "label": "Port Chester", "warnings": []}
    p = cad._plan(cad.store.incident(iid), [by_name(cad, "E63"), by_name(cad, "L30")], "new", False)[1]
    assert p["groups"] == [PC, PC]
    assert p["label"] == "Port Chester + Rye Brook (not set → PC)"
    assert p["warnings"] == ["Rye Brook tones not set: played Port Chester's"]
    pv = lead(cad, iid, ["E14", "L30"])
    assert pv["tones"] == "Rye Brook (not set → PC)" and pv["tone_warnings"] == ["Rye Brook tones not set: played Port Chester's"]
    assert cad._plan(cad.store.incident(iid), [by_name(cad, "E14")], "new", False)[1]["groups"] == [PC]   # never silent
    # Add-on: only the departments of the units added.
    cad.dispatch(iid, {"unit_ids": [uid(cad, "E60")], "operator": "JD"})
    assert lead(cad, iid, ["L30"])["tones"] == "Rye Brook (not set → PC)"
    d = cad.dispatch(iid, {"unit_ids": [uid(cad, "E61")], "operator": "JD"})
    assert d["tones"] == "Port Chester" and d["tone_warnings"] == []
    cad.dispatch(iid, {"unit_ids": [uid(cad, "L30")], "operator": "JD"})
    # Upgrade and full department response: everyone on the event.
    assert cad.upgrade(iid, {"info": "fire showing", "preview": True})["tones"] == "Port Chester + Rye Brook (not set → PC)"
    other = cad.create({"type": "STRU", "address": "1 King St"})["id"]
    assert lead(cad, other, ["E14"], full=True)["tones"] == "Port Chester + Rye Brook (not set → PC)"
    # Recorded with the dispatch and on the timeline.
    full = cad.store.full(iid)
    assert [x["tones"] for x in full["dispatches"]] == [
        "Port Chester", "Port Chester", "Rye Brook (not set → PC). Rye Brook tones not set: played Port Chester's"]
    assert "Tones: Rye Brook (not set → PC). Rye Brook tones not set: played Port Chester's" in texts(cad, iid)
    with pytest.raises(lists.ListsError, match="dispatch_tones"):
        lists.load(write_lists(Path(__import__("tempfile").mkdtemp()), dispatch_tones={"Rye": [[600, 1]]}))


def test_joint_tones_follow_ours_in_the_audio(tmp_path, monkeypatch):
    monkeypatch.setattr(dispatcher, "tts", lambda text, voice="", rate_wpm=0: np.zeros(1600, np.float32))
    p = write_lists(tmp_path, dispatch_tones={"Rye Brook": [[600.0, 0.5], [800.0, 0.5]]})
    cad = make_cad(tmp_path, lists=str(p), clock=Clock(local(2026, 10, 7, 15, 13)))
    iid = cad.create({"type": "STRU", "address": "122 South Regent St"})["id"]
    d = cad.dispatch(iid, {"unit_ids": [uid(cad, "E63"), uid(cad, "L30")], "operator": "JD"})
    assert d["tones"] == "Port Chester + Rye Brook" and d["tone_warnings"] == []
    with wave.open(io.BytesIO(cad.dispatch_audio(d["dispatch_id"]))) as w:
        x = np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32) / 32768

    def at(a, b):
        return peak_hz(x[int(a * 16000):int(b * 16000)])
    assert abs(at(0.1, 0.9) - 349.1) < 2 and abs(at(1.2, 3.8) - 311.1) < 2
    assert np.max(np.abs(x[int(4.05 * 16000):int(4.45 * 16000)])) < 0.01               # the gap between departments
    assert abs(at(4.55, 4.95) - 600) < 3 and abs(at(5.05, 5.45) - 800) < 3


def peak_hz(x, rate=16000):
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    return np.fft.rfftfreq(len(x), 1 / rate)[np.argmax(spec)]


@pytest.mark.skipif(not (shutil.which("say") and shutil.which("afconvert")), reason="macOS say/afconvert not here")
def test_dispatch_audio_tones_then_voice_twice(tmp_path):
    tones = [(349.1, 1.0), (311.1, 3.0)]
    path = dispatcher.render_audio("Port Chester Fire, Engine 60, respond to 209 Westchester Avenue.", tones, tmp_path,
                                   voice="Samantha")
    with wave.open(str(path)) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2)
        x = np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32) / 32768
    assert abs(peak_hz(x[1000:15000]) - 349.1) < 2 and abs(peak_hz(x[17000:63000]) - 311.1) < 2
    voice = x[int((4 + dispatcher.GAP_S) * 16000):]
    assert len(voice) / 16000 > 4 and np.sqrt(np.mean(voice ** 2)) > 0.02      # the voice, read twice
    assert np.max(np.abs(x[64500:int(4.5 * 16000)])) < 0.01                    # the gap is quiet
    again = dispatcher.render_audio("Port Chester Fire, Engine 60, respond to 209 Westchester Avenue.", tones, tmp_path,
                                    voice="Samantha")
    assert again == path                                                        # cached: preview = dispatch


def test_no_tts_means_text_only_dispatch(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    cad = make_cad()
    iid = cad.create({"type": "ALARM", "subtype": "CO-RESD", "address": "1 King St"})["id"]
    r = cad.dispatch(iid, {"unit_ids": [uid(cad, "E60")], "operator": "JD"})
    assert r["audio_url"] is None and "say" in r["audio_note"] and r["transmitted"] is False
    assert cad.store.full(iid)["dispatches"][0]["audio_note"] == r["audio_note"]


# -- the bridge from the monitor ------------------------------------------------------------------------

def monitor_incident(text=PAGE_TEXT, depts=("Port Chester FD",), geo=None):
    store = IncidentStore({"Port Chester FD", "Rye Brook FD"}, geocode=lambda d: geo)
    return store, store.on_page("p1", list(depts), parse(text), at=local(2026, 10, 7, 14, 32))


def test_page_for_port_chester_becomes_one_queued_event():
    cad = make_cad()
    geo = {"lat": 41.0025, "lon": -73.6669, "label": "209 Westchester Ave, Port Chester", "kind": "address",
           "confidence": 0.91}
    store, inc = monitor_incident(geo=geo)
    p = cad.from_monitor(inc)
    assert (p["status"], p["number"], p["source"], p["source_ref"]) == ("queued", None, "60 Control page", f"monitor:{inc.id}")
    assert (p["type"], p["subtype"], p["type_note"]) == ("ALARM", "", "commercial or residential not stated on the page")
    assert (p["address"], p["cross_street"], p["lat"]) == ("209 Westchester Ave, Port Chester", "Pearl Street", 41.0025)
    assert "60 Control page 14:32: " + PAGE_TEXT in p["remarks"] and "location as heard: 209 West Chester Avenue" in p["remarks"]
    inc2 = store.on_page("p2", ["Port Chester FD"], parse(PAGE_TEXT), at=local(2026, 10, 7, 14, 33))
    assert inc2 is inc
    cad.from_monitor(inc2)
    queued = [i for i in cad.store.incidents() if i["status"] == "queued"]
    assert len(queued) == 1 and queued[0]["remarks"].count(PAGE_TEXT) == 2
    with pytest.raises(CadError, match="Accept first"):
        cad.dispatch(p["id"], {"unit_ids": [1], "operator": "JD"})
    cad.accept(p["id"], {"subtype": "COMM", "operator": "JD"})
    acc = cad.store.incident(p["id"])
    assert (acc["status"], acc["subtype"], acc["type_note"]) == ("pending", "COMM", "")
    assert cad.from_monitor(inc)["status"] == "pending"
    _, inc3 = monitor_incident("Port Chester Fire, Engine 61, 2398, respond to a structure fire at 5 King Street")
    inc3.units = ["Engine 61", "2398 Duty Officer"]
    p3 = cad.from_monitor(inc3)
    assert "units named on the page: Engine 61, 2398 Duty Officer" in p3["remarks"] and p3["type"] == "STRU"
    assert by_name(cad, "E61")["incident_id"] is None and p3["lat"] is None


def test_rye_brook_only_page_is_not_ours_to_dispatch():
    cad = make_cad()
    _, inc = monitor_incident("Rye Brook Fire, respond to an activated fire alarm, 940 King Street", depts=("Rye Brook FD",))
    assert cad.from_monitor(inc) is None and cad.store.incidents() == []


def test_app_page_opens_queued_cad_event(tmp_path):
    from tests.test_app import FakeWhisper, cfg, tone, voice, wait
    from sizeup.app import App
    from sizeup.radio.fm import RATE, Channel, Segment
    app = App(replace(cfg(tmp_path), cad_db=str(tmp_path / "state" / "cad.sqlite")))
    app._kind = {"South 2": "paging"}
    app.stt.client = FakeWhisper({6: PAGE_TEXT})
    app.stt.start()
    audio = np.concatenate([tone(349.1, 1.0), tone(311.1, 3.0), np.zeros(int(0.2 * RATE)), voice(6.0)]).astype(np.float32)
    now = time.time()
    app._segment(Channel(470.2, "South 2"), Segment(now, now + len(audio) / RATE, audio, 25.0))
    assert wait(lambda: any(i["status"] == "queued" for i in app.cad.store.incidents()))
    p = app.cad.store.incidents()[0]
    assert p["type"] == "ALARM" and p["source_ref"] == f"monitor:{app.store.active().id}"
    assert (tmp_path / "state" / "cad.sqlite").is_file() and isinstance(app.cad.transmitter, NullTransmitter)


# -- location suggestions ---------------------------------------------------------------------------------

def stub_geocoder(*ms):
    return lambda q: [dict(label=l, municipality=m, confidence=c, lat=41.0, lon=-73.67, kind="address", precision="exact")
                      for l, m, c in ms]


def test_location_suggestions_rank_our_towns_first_and_say_the_town(tmp_path):
    # Stub: the same number and street in Rye scoring a bit higher than in Port Chester.
    geo = stub_geocoder(("20 Grace Church St, Rye", "Rye", 0.86), ("Grace Church St", "Harrison", 0.5),
                        ("20 Grace Church St, Port Chester", "Port Chester", 0.80),
                        ("20 Grace Church St, Rye Brook", "Rye Brook", 0.79), ("20 Grace Ave, Mount Kisco", "Mount Kisco", 0.84))
    cad = make_cad(geocoder=geo)
    labels = [m["label"] for m in cad.geocode("20 grace church st")["matches"]]
    assert labels == ["20 Grace Church St, Port Chester", "20 Grace Church St, Rye Brook", "20 Grace Church St, Rye",
                      "20 Grace Ave, Mount Kisco", "Grace Church St, Harrison"]     # every one says its town
    # A town typed in the box wins.
    assert cad.geocode("20 grace church st rye")["matches"][0]["town"] == "Rye"
    assert cad.geocode("20 grace church st, rye brook")["matches"][0]["town"] == "Rye Brook"
    # The order is the data file's.
    p = write_lists(tmp_path, geo_town_order=["Rye", "Port Chester"])
    cad2 = make_cad(geocoder=geo, lists=str(p))
    assert cad2.geocode("20 grace church st")["matches"][0]["town"] == "Rye"


@pytest.mark.skipif(not (ROOT / "data" / "gazetteer.sqlite").is_file(), reason="no gazetteer data")
def test_location_suggestions_real_duplicate_port_chester_first():
    from sizeup.geo.gazetteer import Gazetteer
    g = Gazetteer()
    geo = lambda q: [{k: getattr(m, k) for k in ("label", "lat", "lon", "kind", "municipality", "precision")}
                     | {"confidence": round(float(m.confidence), 2)} for m in g.candidates(q)[:20]]
    cad = make_cad(geocoder=geo)
    ms = cad.geocode("20 grace church st")["matches"]       # 20 Grace Church St is in Port Chester and in Rye
    assert [m["label"] for m in ms[:2]] == ["20 Grace Church St, Port Chester", "20 Grace Church St, Rye"]
    assert cad.geocode("20 grace church st rye")["matches"][0]["label"] == "20 Grace Church St, Rye"


# -- records: CSV exports and admin ----------------------------------------------------------------------

def read_csv(data: bytes) -> list[list[str]]:
    return list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))


def test_csv_exports_have_the_sheets_columns_and_codes():
    clock = Clock(local(2026, 10, 7, 18, 58))
    cad = make_cad(clock=clock)
    iid = cad.create({"type": "SERVICE", "subtype": "LIFT", "address": "209 Westchester Ave", "geo_label":
                      "209 Westchester Ave, Port Chester", "lat": 41.0, "lon": -73.67, "caller_name": "COREY",
                      "remarks": "boat adrift", "operator": "JD"})["id"]
    cad.dispatch(iid, {"unit_ids": [uid(cad, "E63")], "operator": "JD"})
    clock.t += 90
    cad.unit_status(uid(cad, "E63"), {"status": "RS", "operator": "JD"})
    clock.t += 3600
    cad.close(iid, {"disposition": "SERVICE", "operator": "JD"})
    rows = read_csv(cad.export("incidents.csv"))
    assert rows[0] == export.INCIDENT_COLUMNS == ["Incident #", "Unused", "Event Type", "Event Sub-Type", "Location",
                                                 "Municipality", "Received", "Status", "Assigned Units", "Dispatch",
                                                 "First Enroute", "First On Scene", "Clear", "Duration", "Disposition",
                                                 "Cross Streets", "Narrative Summary", "Closed?"]
    assert rows[1] == ["26-000001", "", "SERVICE", "LIFT", "209 Westchester Ave", "Port Chester", "10/07/2026 18:58:00",
                       "CLOSED", "E63", "10/07/2026 18:58:00", "10/07/2026 18:59:30", "", "10/07/2026 19:59:30",
                       "1:01:30", "SERVICE", "", "boat adrift", "TRUE"]
    calls = read_csv(cad.export("calls.csv"))
    assert calls[0] == ["Incident #", "Event Type", "Event Sub-Type", "Location", "Received", "Clear", "Duration",
                        "Disposition", "Units"] and calls[1][0] == "26-000001" and calls[1][-1] == "E63"
    uh = read_csv(cad.export("unit-history.csv"))
    assert uh[0] == ["Timestamp", "Unit", "Old Status", "New Status", "Incident #", "Operator", "Note"]
    assert uh[1:] == [["10/07/2026 18:58:00", "E63", "AV", "DP", "26-000001", "JD", ""],
                      ["10/07/2026 18:59:30", "E63", "DP", "RS", "26-000001", "JD", ""],
                      ["10/07/2026 19:59:30", "E63", "RS", "AV", "26-000001", "JD", ""]]
    log = read_csv(cad.export("event-log.csv"))
    assert log[0] == ["Timestamp", "Incident #", "Unit", "Event Type", "Event / Comment", "Operator"]
    kinds = [r[3] for r in log[1:]]
    assert kinds[:3] == ["EVENT", "SYSTEM", "UNIT STATUS"] and "DISPATCH" in kinds
    assert cad.export("nope.csv") is None
    a = cad.admin()
    assert a["next_number"] == "26-000002" and a["counts"]["Incident database"] == 1 and a["counts"]["Unit history"] == 3


# -- migration from schema v1 ---------------------------------------------------------------------------------

def test_migrates_a_v1_database(tmp_path):
    path = tmp_path / "cad.sqlite"
    db = sqlite3.connect(path)
    db.executescript(V1_SCHEMA + "PRAGMA user_version = 1;")
    t0 = local(2026, 10, 7, 12, 0)
    for i, (name, kind, status) in enumerate((("2398 Duty Officer", "chief", "dispatched"), ("Engine 60", "engine", "onscene"),
                                              ("Marine 40", "boat", "out_of_service"), ("Tower Ladder 2", "tower", "available-in-quarters"),
                                              ("Rescue 40", "rescue", "transporting"))):
        db.execute("INSERT INTO units (id, name, kind, status, status_at, sort) VALUES (?, ?, ?, ?, ?, ?)",
                   (i + 1, name, kind, status, t0, i))
    db.executemany("INSERT INTO incidents (id, number, type, address, narrative, source, source_ref, status, created_at, closed_at) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   [(1, "PCFD-2026-0007", "Structure fire", "5 King St", "smoke", "manual", None, "active", t0, None),
                    (2, None, "Fire alarm – commercial", "9 Main St", "page", "60 Control page", "monitor:9", "pending", t0, None),
                    (3, "PCFD-2026-0008", "EMS assist", "", "", "manual", None, "active", t0, None),
                    (4, "PCFD-2026-0006", "Other", "", "", "manual", None, "closed", t0, t0 + 60)])
    db.executemany("INSERT INTO assignments (incident_id, unit_id, dispatched_at) VALUES (?, ?, ?)", [(1, 1, t0), (1, 2, t0)])
    db.execute("INSERT INTO seq (year, last) VALUES (2026, 8)")
    db.execute("INSERT INTO run_cards (id, type, spoken, is_default, sort) VALUES (1, 'Structure fire', 'a structure fire', 1, 0)")
    db.execute("INSERT INTO run_cards (id, type, spoken, is_default, sort) VALUES (2, 'CO alarm', 'a CO alarm', 0, 1)")
    db.execute("INSERT INTO run_cards (id, type, spoken, is_default, sort) VALUES (3, 'Lockout', '', 0, 2)")
    db.executemany("INSERT INTO run_card_slots (card_id, pos, kind, count, unit) VALUES (?, ?, ?, ?, ?)",
                   [(1, 0, "engine", 3, ""), (2, 0, "engine", 1, ""), (2, 1, "", 1, "2398 Duty Officer"),
                    (2, 2, "boat", 1, ""), (3, 0, "engine", 1, "")])
    db.execute("INSERT INTO dispatches (incident_id, at, operator, text) VALUES (1, ?, 'JD', 'Port Chester Fire...')", (t0,))
    db.commit()
    db.close()
    cad = make_cad(tmp_path)
    st = cad.store
    assert st.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert "schema v1 -> v2" in st.migrated[0] and any("'Lockout' removed" in m for m in st.migrated)
    rows = {r["id"]: r for r in st.q("SELECT * FROM incidents")}
    assert (rows[1]["number"], rows[1]["type"], rows[1]["status"], rows[1]["remarks"]) == ("26-000007", "STRU", "active", "smoke")
    assert (rows[2]["number"], rows[2]["type"], rows[2]["subtype"], rows[2]["status"], rows[2]["received_via"]) == (
        None, "ALARM", "COMM", "queued", "60 CONTROL")
    assert (rows[3]["type"], rows[3]["status"], rows[3]["type_note"]) == ("", "pending", "was 'EMS assist' before the sheet's types: pick the type")
    assert rows[4]["status"] == "closed" and rows[4]["disposition"] == ""
    units = {u["name"]: u for u in st.units()}
    assert units["2398"]["status"] == "DP" and units["2398"]["incident_number"] == "26-000007"
    assert (units["E60"]["status"], units["M40"]["status"], units["M40"]["kind"], units["TL2"]["status"], units["R40"]["status"]) == (
        "OL", "OS", "marine", "AV", "DP")
    assert len(units) == 250 and units["2398"]["spoken"] == "23 98"          # the rest of the boards added
    cards = {c["type"]: c for c in runcards.cards(st)}
    assert cards["ALARM"]["slots"] == [{"kind": "engine", "count": 1}, {"unit": "2398"}, {"kind": "marine", "count": 1}]
    assert not cards["ALARM"]["is_default"] and cards["STRU"]["is_default"] and "Lockout" not in cards
    assert st.full(1)["dispatches"][0]["kind"] == "dispatch"
    assert st.incident(st.create_incident(type="SERVICE"))["number"] == "26-000009"     # the sequence carries on
    cad.close(1, {"disposition": "FIRE"})


# -- HTTP API -----------------------------------------------------------------------------------------

@pytest.fixture
def server(monkeypatch):
    from sizeup.web.server import WebServer
    monkeypatch.setattr(dispatcher, "tts", lambda text, voice="", rate_wpm=0: np.full(8000, 0.1, np.float32))
    geocoder = stub_geocoder(("209 Westchester Ave, Port Chester", "Port Chester", 0.93))
    cad = Cad(Settings(), geocoder=geocoder, log=lambda m: None)
    ws = WebServer("127.0.0.1", 0, {"timezone": "America/New_York"}, lambda: {"time": 0}, lambda cid: None,
                   lambda iid: False, cad=CadApi(cad))
    threading.Thread(target=ws.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{ws.httpd.server_address[1]}"

    def call(method, path, body=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(base + path, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read(), r.headers.get("Content-Type")
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers.get("Content-Type")

    def j(method, path, body=None):
        code, data, _ = call(method, path, body)
        return code, json.loads(data)
    yield type("S", (), {"call": staticmethod(call), "j": staticmethod(j), "cad": cad, "base": base})
    ws.shutdown()


def test_api_event_lifecycle(server):
    req = urllib.request.Request(server.base + "/", method="GET")
    class NoFollow(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    try:
        urllib.request.build_opener(NoFollow).open(req, timeout=10)
        assert False, "/ should redirect"
    except urllib.error.HTTPError as e:
        assert e.code == 302 and e.headers["Location"] == "/cad"          # the CAD is the front page
    for path, ctype in (("/cad", "text/html"), ("/static/cad.js", "text/javascript"), ("/api/cad/stream", "text/event-stream")):
        with urllib.request.urlopen(urllib.request.Request(server.base + path, method="HEAD"), timeout=5) as r:
            assert r.status == 200 and r.headers["Content-Type"].startswith(ctype) and r.read() == b""   # Netskope checks with HEAD
    code, page, _ = server.call("GET", "/monitor")
    assert code == 200 and b"cad.js" in page and b"board.js" in page      # the wall board: the CAD page, read-only
    opener = urllib.request.build_opener(NoFollow)
    for old, where in (("/comms", "/cad#radio"), ("/comms.html", "/cad#radio"),
                       ("/comms?event=26-000012&unit=E63", "/cad#radio?event=26-000012&unit=E63")):
        try:
            opener.open(urllib.request.Request(server.base + old), timeout=10)
            assert False, f"{old} should redirect"
        except urllib.error.HTTPError as e:
            assert e.code == 302 and e.headers["Location"] == where, old     # Comms is the CAD's Radio view
    for gone in ("/static/comms.js", "/static/app.js", "/static/index.html"):
        assert server.call("GET", gone)[0] == 404
    code, page, ctype = server.call("GET", "/cad")
    assert code == 200 and b"cad.js" in page and ctype.startswith("text/html")
    code, st = server.j("GET", "/api/cad/state")
    assert code == 200 and len(st["units"]) == 250 and st["incidents"] == [] and st["run_cards_default"]
    assert st["lists"]["source"] == "data/cad_lists.json" and st["lists"]["unit_boards"][1]["folded"] is True
    assert st["transmit"] == {"enabled": False, "transmitter": "null", "channels": ["FG1", "PC repeater"],
                              "note": "Transmit is off: logged and played here only"}
    code, g = server.j("GET", "/api/cad/geocode?q=209+westchester")
    assert code == 200 and g["matches"][0]["label"] == "209 Westchester Ave, Port Chester" and g["matches"][0]["town"] == "Port Chester"
    assert server.j("POST", "/api/cad/incident", {"type": "Volcano"}) == (400, {"error": "type: 'Volcano' is not an event type"})
    assert server.j("POST", "/api/cad/incident", {"type": "SERVICE", "lat": 41.0})[0] == 400
    assert server.call("POST", "/api/cad/incident", raw=b"{nope")[0] == 400
    code, inc = server.j("POST", "/api/cad/incident", {"type": "SERVICE", "subtype": "LIFT", "address": "209 Westchester Avenue",
                                                       "caller_name": "COREY", "received_via": "PHONE", "operator": "JD"})
    assert code == 200 and inc["status"] == "pending" and re.fullmatch(r"\d{2}-000001", inc["number"])
    assert inc["lat"] == 41.0 and inc["geo_label"] == "209 Westchester Ave, Port Chester"   # placed: gazetteer sure
    iid = inc["id"]
    assert server.j("POST", f"/api/cad/incident/{iid}/accept", {})[0] == 400                 # not queued
    code, rec = server.j("POST", f"/api/cad/incident/{iid}/recommend", {})
    assert code == 200 and names(rec["units"]) == ["E58"] and rec["card"] == "SERVICE"
    ids = [u["id"] for u in rec["units"]]
    code, pv = server.j("POST", f"/api/cad/incident/{iid}/preview", {"unit_ids": ids})
    assert code == 200 and pv["audio_url"].startswith("/api/cad/audio/") and pv["kind"] == "new"
    assert server.call("GET", pv["audio_url"])[1][:4] == b"RIFF"
    assert server.cad.store.full(iid)["assignments"] == []                                   # preview assigns nothing
    assert server.j("POST", f"/api/cad/incident/{iid}/dispatch", {"unit_ids": ids})[1] == {"error": "operator: required"}
    assert server.j("POST", f"/api/cad/incident/{iid}/dispatch", {"unit_ids": ["x"], "operator": "JD"})[0] == 400
    assert server.j("POST", f"/api/cad/incident/{iid}/dispatch", {"unit_ids": ids, "operator": "JD", "channel": "Ch 9"})[0] == 400
    code, d = server.j("POST", f"/api/cad/incident/{iid}/dispatch", {"unit_ids": ids, "operator": "JD", "channel": "PC repeater"})
    assert code == 200 and d["transmitted"] is False and d["channel"] == "PC repeater"
    assert d["text"].startswith("Port Chester Fire, Engine 58, respond to 209 Westchester Avenue, for a lift assist.")
    code, wav, ctype = server.call("GET", d["audio_url"])
    assert code == 200 and ctype == "audio/wav" and wave.open(io.BytesIO(wav)).getframerate() == 16000
    assert server.call("GET", "/api/cad/dispatch/999.wav")[0] == 404
    assert server.j("POST", f"/api/cad/unit/{ids[0]}/status", {"status": "napping"})[0] == 400
    code, u = server.j("POST", f"/api/cad/unit/{ids[0]}/status", {"status": "OL", "operator": "JD"})
    assert code == 200 and u["status"] == "OL"
    code, up = server.j("POST", f"/api/cad/incident/{iid}/upgrade", {"info": "boat sinking", "full": True, "operator": "JD"})
    assert code == 200 and "upgrade on your last alarm" in up["text"] and up["kind"] == "upgrade"
    assert server.j("POST", f"/api/cad/incident/{iid}/note", {"text": ""})[0] == 400
    assert server.j("POST", f"/api/cad/incident/{iid}/note", {"text": "Smoke showing", "operator": "JD"})[0] == 200
    assert server.j("POST", f"/api/cad/incident/{iid}", {"cross_street": "Pearl St"})[0] == 200
    assert server.j("POST", f"/api/cad/incident/{iid}/close", {"operator": "JD"})[0] == 400        # no disposition
    assert server.j("POST", f"/api/cad/incident/{iid}/close", {"disposition": "RESCUE", "operator": "JD"})[0] == 200
    code, full = server.j("GET", f"/api/cad/incident/{iid}")
    assert code == 200 and full["status"] == "closed" and full["disposition"] == "RESCUE"
    kinds = [e["kind"] for e in full["events"]]
    assert kinds[:2] == ["created", "system"] and kinds[-1] == "closed" and {"dispatch", "status", "note", "edit", "upgrade"} <= set(kinds)
    assert server.j("GET", "/api/cad/incident/999")[0] == 400
    code, h = server.j("GET", "/api/cad/history")
    assert code == 200 and h["incidents"][0]["number"] == inc["number"] and h["incidents"][0]["units"] == ["E58"]
    code, a = server.j("GET", "/api/cad/admin")
    assert code == 200 and a["counts"]["Call history"] == 1
    code, data, ctype = server.call("GET", "/api/cad/export/incidents.csv")
    assert code == 200 and ctype.startswith("text/csv") and read_csv(data)[1][0] == inc["number"]
    assert server.call("GET", "/api/cad/export/secrets.csv")[0] == 404
    code, rc = server.j("POST", "/api/cad/runcards", {"type": "SERVICE/LIFT", "slots": [{"kind": "engine", "count": 1}]})
    assert code == 200 and rc["type"] == "SERVICE/LIFT" and rc["label"] == "Service / Lift"
    assert server.j("POST", "/api/cad/runcards/delete", {"type": "SERVICE/LIFT"})[0] == 200
    assert server.call("POST", "/api/cad/nope", {})[0] == 404


def test_api_stream_pushes_cad_state(server):
    with urllib.request.urlopen(server.base + "/api/cad/stream", timeout=5) as r:
        for _ in range(6):                     # the first push can be the empty one from before the first tick
            line = r.readline()
            if line.startswith(b"data: ") and b"department" in line:
                break
    st = json.loads(line[len(b"data: "):])
    assert st["department"] == "Port Chester Fire" and "units" in st


# -- nothing transmits ---------------------------------------------------------------------------------

def test_only_the_null_transmitter_exists_in_m1(tmp_path):
    assert isinstance(make_transmitter(False), NullTransmitter)
    with pytest.raises(ValueError):
        make_transmitter(True)
    with pytest.raises(NotImplementedError):
        AudioPttTransmitter()
    with pytest.raises(ValueError):
        Cad(Settings(transmit=True), log=lambda m: None)
    toml = (ROOT / "config.example.toml").read_text().replace("transmit = false", "transmit = true")
    (tmp_path / "c.toml").write_text(toml)
    with pytest.raises(ConfigError, match="transmit"):
        load(tmp_path / "c.toml")
    # No code that could open a radio interface: serial ports, GPIO/HID, audio output.
    src = "\n".join(p.read_text() for p in (ROOT / "sizeup").rglob("*.py"))
    for mod in ("serial", "sounddevice", "pyaudio", "hid", "gpiozero", "RPi", "simpleaudio", "pygame"):
        assert not re.search(rf"^\s*(import|from)\s+{mod}\b", src, re.M), mod
    assert "afplay" not in src
    cad = make_cad()
    iid = cad.create({"type": "ALARM", "subtype": "CO-RESD"})["id"]
    cad.s.tones = ()
    r = cad.dispatch(iid, {"unit_ids": [uid(cad, "E60")], "operator": "JD"})
    assert r["transmitted"] is False and cad.transmitter.sent[-1][1] == r["text"]


def test_config_cad_sections(tmp_path):
    c = load(ROOT / "config.example.toml")
    assert c.cad_db == str((ROOT / "state" / "cad.sqlite").resolve()) and c.dispatch_tones == ((349.1, 1.0), (311.1, 3.0))
    assert c.dispatch_channels == ("FG1", "PC repeater") and c.dispatch_transmit is False
    assert c.cad_lists == str(DATA.resolve())
    for bad in ('[dispatch]\nvolume = 3\n', '[dispatch]\ntones = [[10, 1]]\n', '[cad]\nnope = 1\n', '[cad]\nlists = "nope.json"\n'):
        (tmp_path / "c.toml").write_text(bad)
        with pytest.raises(ConfigError):
            load(tmp_path / "c.toml")
    (tmp_path / "c.toml").write_text('[cad]\nroster_department = "Port Chester"\n')     # old configs keep working
    assert load(tmp_path / "c.toml").cad_department == "Port Chester Fire"


def test_cad_page_buttons_have_handlers():
    html = (ROOT / "sizeup" / "web" / "static" / "cad.html").read_text()
    js = (ROOT / "sizeup" / "web" / "static" / "cad.js").read_text()
    js_all = js + "".join((ROOT / "sizeup" / "web" / "static" / f).read_text()
                          for f in ("settings.js", "cadmap.js", "radio-view.js", "radio-controls.js", "board.js"))
    for bid, rest in re.findall(r'<button id="([\w-]+)"([^>]*)>', html):
        assert (f'$("#{bid}").addEventListener' in js_all or f'RadioLive.button($("#{bid}")' in js_all
                or 'type="submit"' in rest), f"#{bid} has no handler"
    for fid in re.findall(r'<form id="([\w-]+)"', html):
        assert f'$("#{fid}").addEventListener("submit"' in js_all, f"#{fid} has no submit handler"
    # every element the scripts look up by id is on the page
    for ref in set(re.findall(r'\$\("#([\w-]+)"\)', js_all)):
        assert f'id="{ref}"' in html or f'.id = "{ref}"' in js_all, f"#{ref} is not on the page"
    called = set(re.findall(r"^\s+([A-Za-z_]\w*)\(\);?$", re.search(r"^function render\(\) \{(.*?)^\}", js, re.S | re.M).group(1), re.M))
    defined = set(re.findall(r"^(?:async\s+)?function\s+([A-Za-z_]\w*)\s*\(", js, re.M))
    assert called and not (called - defined)
    css = (ROOT / "sizeup" / "web" / "static" / "cad.css").read_text()
    assert "text-transform: uppercase" not in css and "text-transform:uppercase" not in css      # readable casing


# -- out of service / unmanned: the reason ------------------------------------------------------------------

def test_out_of_service_reason_stored_shown_and_in_unit_history():
    cad = make_cad()
    e60, l31 = uid(cad, "E60"), uid(cad, "L31")
    cad.unit_status(e60, {"status": "OS", "note": "pump repair", "operator": "JD"})
    cad.unit_status(l31, {"status": "UNMANNED", "note": "no crew until 1800", "operator": "JD"})
    units = {u["name"]: u for u in cad.state()["units"]}            # what the board shows under the unit
    assert units["E60"]["out_of_service"] == "pump repair" and units["L31"]["out_of_service"] == "no crew until 1800"
    log = [e["text"] for e in cad.state()["unit_log"]]
    assert log[:2] == ["L31 unmanned (no crew until 1800)", "E60 out of service (pump repair)"]
    hist = cad.store.q("SELECT u.name, h.new_status, h.note, h.operator FROM unit_history h JOIN units u ON u.id = h.unit_id "
                       "ORDER BY h.id")
    assert [tuple(h.values()) for h in hist] == [("E60", "OS", "pump repair", "JD"),
                                                 ("L31", "UNMANNED", "no crew until 1800", "JD")]
    uh = read_csv(cad.export("unit-history.csv"))
    assert uh[1][1] == "E60" and uh[1][-1] == "pump repair"
    # Back in service: the reason goes from the board; history keeps it; a reason on a free status is ignored.
    cad.unit_status(e60, {"status": "AV", "note": "ignored", "operator": "JD"})
    assert by_name(cad, "E60")["out_of_service"] == ""
    assert cad.store.q("SELECT note FROM unit_history ORDER BY id DESC LIMIT 1")[0]["note"] == ""
    assert cad.store.unit_log()[0]["text"] == "E60 available"
    # No reason given: none shown, none stored.
    cad.unit_status(e60, {"status": "OS"})
    assert by_name(cad, "E60")["out_of_service"] == "" and cad.store.unit_log()[0]["text"] == "E60 out of service"


def test_migrates_a_v2_database_keeping_its_data(tmp_path):
    cad = make_cad(tmp_path)
    iid = cad.create({"type": "STRU", "address": "209 Westchester Ave"})["id"]
    cad.dispatch(iid, {"unit_ids": [uid(cad, "E60")], "operator": "JD"})
    runcards.save(cad.store, "STRU", [{"kind": "engine", "count": 5}, {"kind": "ladder|tower", "count": 2},
                                      {"kind": "rescue", "count": 1}])        # an operator's edit survives
    db = cad.store.db
    db.execute("ALTER TABLE unit_history DROP COLUMN note")         # as a v2 file was
    db.execute("PRAGMA user_version = 2")
    db.commit()
    cards_before = runcards.cards(cad.store)
    cad2 = make_cad(tmp_path)
    st = cad2.store
    assert st.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 10
    assert any("v2 -> v3" in m for m in st.migrated)
    assert st.q("SELECT new_status, note FROM unit_history") == [{"new_status": "DP", "note": ""}]
    assert st.full(iid)["number"] == "26-000001" and by_name(cad2, "E60")["incident_id"] == iid
    assert runcards.cards(st) == cards_before
    cad2.unit_status(uid(cad2, "E61"), {"status": "OS", "note": "tires"})
    assert st.q("SELECT note FROM unit_history ORDER BY id DESC LIMIT 1")[0]["note"] == "tires"


# -- Rye Brook: joint response --------------------------------------------------------------------------------

def test_rye_brook_units_are_direct_and_fill_after_pcfd():
    cad = make_cad()
    L = cad.lists
    assert L.joint_response["Rye Brook"]["units"] == ("2661", "2662", "2663", "E14", "L30", "U42", "DPT266", "266EOC",
                                                     "CHIEF266", "ROAD266", "266RCL")
    assert L.joint_response["Rye Brook"]["quick_add"] == ("E14", "L30", "2662") and L.joint_response["Rye Brook"]["tag"] == "RB"
    pub = cad.state()["lists"]["joint_response"]
    assert pub["Rye Brook"]["tag"] == "RB"
    st = {u["name"]: u for u in cad.state()["units"]}
    assert not any(st[c]["mutual_aid"] for c in ("2662", "L30", "E14", "U42")) and st["L25"]["mutual_aid"]
    # The default structure fire card names Rye Brook's engine and ladder (first dispatch).
    cards = {c["type"]: c for c in runcards.cards(cad.store)}
    assert cards["STRU"]["slots"][-2:] == [{"unit": "E14"}, {"unit": "L30"}]
    # By kind: PCFD's own fill first (spares included), then Rye Brook's; Rye's (mutual aid) never.
    runcards.save(cad.store, "SERVICE", [{"kind": "engine", "count": 9}])
    iid = cad.create({"type": "SERVICE", "address": "1 Main St"})["id"]
    r = runcards.recommend(cad.store, "SERVICE", "", iid)
    assert names(r["units"]) == ["E58", "E60", "E61", "E62", "E63", "E64", "E59", "E14"]
    assert r["unfilled"] == ["only 8 of 9 engine available"] and r["joint_call"] == ""
    runcards.save(cad.store, "MVA", [{"kind": "ladder|tower", "count": 3}, {"kind": "chief", "count": 10},
                                     {"kind": "chief", "count": 3}])
    r = runcards.recommend(cad.store, "MVA", "", cad.create({"type": "MVA"})["id"])
    assert names(r["units"]) == ["L31", "TL2", "L30", "2391", "2392", "2393", "2394", "2395", "2396", "2397", "2398",
                                 "2399", "2661", "2662", "2663"]
    assert r["unfilled"] == ["only 2 of 3 chief available"]          # the mutual-aid departments' chiefs: never by kind


def test_an_event_in_rye_brook_is_their_call():
    cad = make_cad()
    runcards.save(cad.store, "SERVICE", [{"kind": "engine", "count": 2}, {"kind": "ladder|tower", "count": 1},
                                         {"kind": "utility", "count": 1}])
    rb = cad.create({"type": "SERVICE", "address": "800 Westchester Ave", "lat": 41.03, "lon": -73.69,
                     "geo_label": "800 Westchester Ave, Rye Brook"})
    inc = next(i for i in cad.state()["incidents"] if i["id"] == rb["id"])
    assert (inc["town"], inc["joint_call"]) == ("Rye Brook", "Rye Brook")
    r = cad.recommend(rb["id"], {})
    assert names(r["units"]) == ["E14", "E58", "L30", "U42"] and r["joint_call"] == "Rye Brook"
    # Typed, not on the map: the town in the location still counts.
    typed = cad.create({"type": "SERVICE", "address": "940 King St, Rye Brook"})["id"]
    assert names(cad.recommend(typed, {})["units"])[:1] == ["E14"]
    # In Port Chester (or anywhere else): PCFD's first.
    pc = cad.create({"type": "SERVICE", "address": "209 Westchester Ave", "lat": 41.0, "lon": -73.66,
                     "geo_label": "209 Westchester Ave, Port Chester"})["id"]
    inc = next(i for i in cad.state()["incidents"] if i["id"] == pc)
    assert (inc["town"], inc["joint_call"]) == ("Port Chester", "")
    assert names(cad.recommend(pc, {})["units"]) == ["E58", "E60", "L31", "U65"]
    rye = cad.create({"type": "SERVICE", "address": "1 Purchase St, Rye"})["id"]
    assert names(cad.recommend(rye, {})["units"]) == ["E58", "E60", "L31", "U65"]


def test_town_order_and_aliases_for_location_suggestions():
    L = lists.load()
    assert L.geo_town_order == ("Port Chester", "Rye Brook", "Rye", "Harrison", "Greenwich", "Mamaroneck", "Larchmont",
                                "Town of Mamaroneck")
    assert L.towns_typed("5 Palmer Ave, Larchmont") == ["Larchmont"]
    assert L.towns_typed("740 W Boston Post Rd, Town of Mamaroneck") == ["Town of Mamaroneck"]
    assert L.towns_typed("123 Mamaroneck Ave, Mamaroneck") == ["Mamaroneck"]


def test_gas_calls_are_said_by_what_they_are():
    # User 2026-10-07: "we need things like odor, atmospheric gas alarm, etc".
    from sizeup.cad.lists import load
    L = load()
    assert L.phrase("HAZARD", "GAS-ODOR") == "an odor of gas"
    assert L.phrase("ALARM", "GAS-ATMOS") == "an atmospheric gas alarm"
    assert L.phrase("HAZARD", "GAS-LEAK") == "a gas leak"
    assert L.phrase("HAZARD", "GAS-LINE") == "a struck gas line"
    assert L.phrase("INVEST", "GAS-OUT") == "an odor of gas outside"
    assert L.phrase("ALARM", "GAS") == "an atmospheric gas alarm"      # the old plain GAS follows the type
    assert L.phrase("HAZARD", "GAS") == "an odor of gas"
    assert L.phrase("MVA", "EXTRI").endswith("with entrapment")
    assert L.name("event_subtypes", "EXTRI") == "Extrication / entrapment"


def test_time_out_is_said_once_after_the_second_reading(tmp_path, monkeypatch):
    # User 2026-10-07: "the time out on dispatch should only be after the second repeated message, not twice".
    from sizeup.cad import dispatcher as D
    assert D.split_time_out("Port Chester Fire, Engine 60, respond to 1 Main Street. Time out fifteen thirteen.") == (
        "Port Chester Fire, Engine 60, respond to 1 Main Street.", "Time out fifteen thirteen.")
    assert D.split_time_out("No time out here.") == ("No time out here.", "")
    said = []
    monkeypatch.setattr(D, "tts", lambda text, voice="", rate=0: (said.append(text), np.ones(D.RATE // 10 * len(text.split()), np.float32))[1])
    path, note = D.render_dispatch("Port Chester Fire, Engine 60, respond to 1 Main Street. Time out 15:13.",
                                   [[349.1, 1.0]], tmp_path, D.VoiceCfg("say", "Samantha"))
    assert said[0].endswith("Main Street.") and "Time out" not in said[0] and said[1].startswith("Time out")
    import wave as W
    with W.open(str(path)) as w:
        n = w.getnframes()
    body, tail = len(said[0].split()) * D.RATE // 10, len(said[1].split()) * D.RATE // 10
    expect = int(1.0 * D.RATE) + int(D.GAP_S * D.RATE) + 2 * body + int(D.REPEAT_GAP_S * D.RATE) + int(D.TAIL_GAP_S * D.RATE) + tail
    assert abs(n - expect) < D.RATE // 20                    # tones, body twice, the time out once
