"""The pinned 60 Control feed (sizeup/cad/feed.py): the "talking to us" matcher, the feed slice in
the state and its API, the data-file settings, and "+ event" (a manual link to the open event)."""

import json

import pytest

from sizeup.cad.api import CadApi
from sizeup.cad.feed import DEFAULT_PHRASES, Matcher, load_cfg
from sizeup.cad.lists import ListsError, load
from sizeup.cad.lists import DEFAULT_PATH
from tests.test_radios import Clock, _event_with, cad, tx

DATA = json.loads(DEFAULT_PATH.read_text())


def lists_with(tmp_path, feed):
    d = dict(DATA)
    if feed is None:
        d.pop("comms_feed", None)
    else:
        d["comms_feed"] = feed
    p = tmp_path / "lists.json"
    p.write_text(json.dumps(d))
    return p


def test_data_file_phrases_and_defaults(tmp_path):
    cfg = load_cfg(load())
    assert cfg.talkgroups == (307,) and cfg.limit == 6 and cfg.title == "60 Control · Fire/EMS 15"
    assert cfg.phrases == ("Port Chester", "Rye Brook", "PC dispatch", "Port Chester dispatch", "PCFD", "headquarters")
    # PCFD's and Rye Brook's fire units, not Rye's mutual aid, PD, EMS or agencies
    assert {"E60", "L31", "TL2", "R40", "2391", "2398", "2662", "E14", "L30"} <= set(cfg.units)
    assert not {"E191", "L25", "77A4", "PCPD", "60CAD", "DPW"} & set(cfg.units)
    # missing section: the defaults; the Airport incident talkgroup added in the file
    assert load_cfg(load(lists_with(tmp_path, None))).phrases == DEFAULT_PHRASES
    two = load_cfg(load(lists_with(tmp_path, {"talkgroups": [307, 333], "phrases": ["Port Chester", "Kingsland"]})))
    assert two.talkgroups == (307, 333) and two.phrases == ("Port Chester", "Kingsland")
    for bad in ({"talkgroups": []}, {"talkgroups": ["307"]}, {"limit": 0}, {"phrases": "Port Chester"},
                {"phrases": [""]}, [307]):
        with pytest.raises(ListsError, match="comms_feed"):
            load_cfg(load(lists_with(tmp_path, bad)))


@pytest.mark.parametrize("text,why", [
    ("60 Control to Port Chester", "Port Chester"),
    ("Portchester, 60 Control", "Port Chester"),
    ("Rye Brook, you have a medical", "Rye Brook"),
    ("PC dispatch, 60 Control", "PC dispatch"),
    ("Contact P.C.F.D. on the landline", "PCFD"),
    ("60 Control to headquarters", "headquarters"),
    ("Engine 60 responding", "E60"),
    ("E-60 on location", "E60"),
    ("Engine 6-0, 60 Control", "E60"),
    ("Tower Ladder 2 in service", "TL2"),
    ("L31 available", "L31"),
    ("23-91 to 60 Control", "2391"),
    ("23 98 is on scene", "2398"),
    ("2662 responding", "2662"),
    ("Engine 14, Ladder 30 respond", "E14"),
    ("Rescue 40 out", "R40"),
    ("60 Control, 77 Alpha 1.", ""),
    ("Engine 191 from Rye", ""),
    ("Engine 6 responding", ""),
    ("Rescue 4 in quarters", ""),
    ("Harrison headquarter building", ""),
])
def test_matcher_phrases_unit_codes_and_spoken_forms(text, why):
    cfg = load_cfg(load())
    assert Matcher(cfg.phrases, cfg.units).match(text) == why


def test_matcher_talker_and_rig():
    cfg = load_cfg(load())
    m = Matcher(cfg.phrases, cfg.units)
    assert m.match("in service", "E63", "E63") == "E63"
    assert m.match("in service", "PCFD 2391", "") == "PCFD"
    assert m.match("in service", "77A8", "77A8") == ""
    assert m.match("in service", "60 Ctrl OP 7", "") == ""


def test_feed_slice_filters_talkgroup_newest_first_and_limit(tmp_path):
    clock = Clock()
    c = cad(tmp_path, clock)
    t = clock.t
    c.record_traffic(tx("307-a-call_1-t0", t + 1, "Fire/EMS 15", "77A8", "77A8", 307, "77 Alpha 8, back in."))
    c.record_traffic(tx("307-a-call_1-t1", t + 3, "Fire/EMS 15", "", "60 Ctrl OP 7", 307, "In service 1504."))
    c.record_traffic(tx("309-b-call_2", t + 4, "Fire/EMS 18", "", "60 Ctrl OP 2", 309, "Not ours"))
    c.record_traffic(tx("FG1-1", t + 5, rig="E63", text="E63 on location"))
    c.record_traffic(tx("333-c-call_3", t + 6, "Airport Incident", "", "60 Ctrl OP 3", 333, "Port Chester, stage"))
    for k in range(8):
        c.record_traffic(tx(f"307-d-call_9-t{k}", t + 10 + k, "Fire/EMS 15", "", "60 Ctrl OP 7", 307, f"turn {k}"))
    s = c.state()["feed"]
    assert s["title"] == "60 Control · Fire/EMS 15" and s["talkgroups"] == [307] and s["channel"] == "Fire/EMS 15"
    assert [r["id"] for r in s["rows"]] == [f"307-d-call_9-t{k}" for k in (7, 6, 5, 4, 3, 2)]   # newest 6
    api = CadApi(c)
    get = lambda q: json.loads(api.get("/api/cad/feed", q)[1])
    rows = get("limit=20")["rows"]
    assert [r["id"] for r in rows][-2:] == ["307-a-call_1-t1", "307-a-call_1-t0"] and len(rows) == 10
    assert all(r["talkgroup"] == 307 for r in rows) and rows[0]["has_audio"] is False
    both = get("limit=20&tg=307,333")["rows"]
    assert "333-c-call_3" in [r["id"] for r in both] and "309-b-call_2" not in [r["id"] for r in both]
    assert next(r for r in both if r["id"] == "333-c-call_3")["pc"] == "Port Chester"
    assert api.get("/api/cad/feed", "limit=many")[0] == 400
    # a new transmission changes the state that is pushed
    rev = c.state()["rev"]
    c.record_traffic(tx("307-e-call_10", t + 30, "Fire/EMS 15", "", "60 Ctrl OP 7", 307, "60 Control to Port Chester"))
    s2 = c.state()
    assert s2["rev"] != rev and s2["feed"]["rows"][0]["id"] == "307-e-call_10" and s2["feed"]["rows"][0]["pc"] == "Port Chester"


def test_neighbouring_60_control_turn_is_talking_to_us(tmp_path):
    clock = Clock()
    c = cad(tmp_path, clock)
    t = clock.t
    # 60 Control calls, E60 answers (by rig), 60 Control acknowledges, then an unrelated EMS unit in another call
    c.record_traffic(tx("307-x-call_5-t0", t, "Fire/EMS 15", "", "60 Ctrl OP 7", 307, "Go ahead."))
    c.record_traffic(tx("307-x-call_5-t1", t + 2, "Fire/EMS 15", "E60", "E60", 307, "On location, investigating."))
    c.record_traffic(tx("307-x-call_5-t2", t + 4, "Fire/EMS 15", "", "60 Ctrl OP 7", 307, "Received, 1504."))
    c.record_traffic(tx("307-x-call_5-t3", t + 6, "Fire/EMS 15", "77A8", "77A8", 307, "77 Alpha 8 in service."))
    c.record_traffic(tx("307-y-call_6-t0", t + 8, "Fire/EMS 15", "77A1", "77A1", 307, "77 Alpha 1 responding."))
    c.record_traffic(tx("307-y-call_6-t1", t + 9, "Fire/EMS 15", "", "60 Ctrl OP 7", 307, "1509."))
    # a unit named in the transcript (no rig) makes its neighbouring dispatcher turn ours too
    c.record_traffic(tx("307-z-call_7-t0", t + 12, "Fire/EMS 15", "", "PCFD 2398", 307, "Duty officer to 60 Control"))
    c.record_traffic(tx("307-z-call_7-t1", t + 14, "Fire/EMS 15", "", "60 Ctrl OP 7", 307, "Go ahead."))
    pc = {r["id"]: r["pc"] for r in c.feed.rows(limit=20)}
    assert pc == {"307-x-call_5-t0": "calling E60", "307-x-call_5-t1": "E60", "307-x-call_5-t2": "answering E60",
                  "307-x-call_5-t3": "", "307-y-call_6-t0": "", "307-y-call_6-t1": "",
                  "307-z-call_7-t0": "PCFD", "307-z-call_7-t1": "answering PCFD"}


def test_plus_event_is_a_manual_link_with_the_operator(tmp_path):
    clock = Clock()
    c = cad(tmp_path, clock)
    a = _event_with(c, ["E63"], clock)
    clock.t += 5
    c.record_traffic(tx("307-m-call_1", clock.t, "Fire/EMS 15", "", "60 Ctrl OP 7", 307,
                        "60 Control to Port Chester, PD requesting you at 209 Westchester."))
    api = CadApi(c)
    post = lambda path, body: (lambda r: (r[0], json.loads(r[1])))(api.post(path, json.dumps(body).encode()))
    code, r = post(f"/api/cad/incident/{a['id']}/link", {"traffic_id": "307-m-call_1", "operator": "Lt. Ruiz"})
    assert code == 200 and r["linked"] is True and r["number"] == a["number"]
    full = c.full(a["id"])
    row = next(x for x in full["radio"] if x["id"] == "307-m-call_1")
    assert row["why"] == "manual" and row["link_unit"] == "Lt. Ruiz"
    ev = [e for e in full["events"] if e["kind"] == "radio"]
    assert len(ev) == 1 and ev[0]["operator"] == "Lt. Ruiz" and "60 Ctrl OP 7 on Fire/EMS 15" in ev[0]["text"]
    assert c.feed.rows()[0]["events"][0]["why"] == "manual"
    # again: left as it was; a transmission already linked by unit keeps its reason
    code, r = post(f"/api/cad/incident/{a['id']}/link", {"traffic_id": "307-m-call_1", "operator": "x"})
    assert code == 200 and r == {"linked": False, "why": "manual", "incident_id": a["id"], "number": a["number"]}
    c.record_traffic(tx("FG1-9", clock.t + 1, rig="E63"))
    assert post(f"/api/cad/incident/{a['id']}/link", {"traffic_id": "FG1-9"})[1]["why"] == "unit"
    assert post(f"/api/cad/incident/{a['id']}/link", {"traffic_id": "nope"})[0] == 400
    assert post(f"/api/cad/incident/{a['id']}/link", {})[0] == 400
    assert post("/api/cad/incident/9999/link", {"traffic_id": "307-m-call_1"})[0] == 400
    # survives a restart
    c2 = cad(tmp_path, clock)
    assert {x["id"]: x["why"] for x in c2.full(a["id"])["radio"]}["307-m-call_1"] == "manual"


def test_units_matched_follow_the_lists_settings_edits():
    import dataclasses
    c = cad()
    assert c.feed.matcher.match("Engine 66 responding") == ""
    c.lists = dataclasses.replace(c.lists, units={**c.lists.units, "E66": {"kind": "engine", "spoken": "Engine 66"}})
    c.record_traffic(tx("307-n-call_1", 1_791_400_000.0, "Fire/EMS 15", "", "60 Ctrl OP 7", 307, "Engine 66 responding"))
    assert c.state()["feed"]["rows"][0]["pc"] == "E66"
