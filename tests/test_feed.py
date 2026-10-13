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


def test_every_page_is_in_the_feed_only_ours_as_an_event(tmp_path):
    """#80: South 2 pages are feed rows beside the talkgroup's; page says what each was. Only a page with our
    tones is "talking to us" (pc = the department) and carries the event it opened."""
    clock = Clock()
    c = cad(tmp_path, clock)
    t = clock.t
    c.record_traffic(tx("307-a-call_1-t0", t + 1, "Fire/EMS 15", "", "60 Ctrl OP 7", 307, "Port Chester, stage."))

    def page(pid, at, tones, text, voice_at=4.2):
        return dict(tx(pid, at, "South 2", "", "60 Control" if tones else "", None, text), tones=tones, voice_at=voice_at if tones else None)
    c.record_traffic(page("S2-ours", t + 2, [{"a_hz": 349.1, "b_hz": 311.1, "names": ["Port Chester FD"], "ours": True}],
                          "Port Chester, 209 Westchester Avenue, fire alarm"))
    c.record_traffic(page("S2-other", t + 3, [{"a_hz": 832.5, "b_hz": 1201.0, "names": ["Fairview (heard)"], "ours": False}],
                          "Fairview, 34 Longdale Avenue, smoke in the building, Port Chester"))
    c.record_traffic(page("S2-unknown", t + 4, [{"a_hz": 741.3, "b_hz": 623.7, "names": [], "ours": False}], "Engine 61, 1 Main Street"))
    c.record_traffic(page("S2-none", t + 5, [], "Port Chester, respond to 1 Main Street"))
    c.record_traffic(tx("FG1-1", t + 6, rig="E63", text="E63 on location"))
    iid = c.store.create_incident(type="ALARM", address="209 Westchester Ave", source="60 Control page", source_ref="monitor:x", status="queued")
    c.comms.link_pages(iid, ["S2-ours"])
    rows = {r["id"]: r for r in c.feed.rows(limit=20)}
    assert list(rows) == ["S2-none", "S2-unknown", "S2-other", "S2-ours", "307-a-call_1-t0"]     # FG1 isn't in the feed
    assert {k: r["page"] for k, r in rows.items()} == {"S2-none": "none", "S2-unknown": "unknown", "S2-other": "other",
                                                        "S2-ours": "ours", "307-a-call_1-t0": ""}
    assert rows["S2-ours"]["pc"] == "Port Chester FD" and [e["why"] for e in rows["S2-ours"]["events"]] == ["page"]
    assert rows["S2-other"]["pc"] == "" and rows["S2-other"]["events"] == []       # "Port Chester" in the text doesn't count
    assert rows["S2-unknown"]["pc"] == "" and rows["S2-none"]["pc"] == ""   # even naming us in the voice: a page, not an event
    assert rows["S2-ours"]["voice_at"] == 4.2 and rows["S2-none"]["voice_at"] is None
    s = c.state()["feed"]
    assert s["channel"] == "Fire/EMS 15" and s["rows"][0]["id"] == "S2-none"       # the title's channel is the talkgroup's
    api = CadApi(c)
    assert [r["page"] for r in json.loads(api.get("/api/cad/feed", "limit=3")[1])["rows"]] == ["none", "unknown", "other"]
    # the Radio view's rows say the same
    assert {r["id"]: r["page"] for r in c.comms.query(limit=10)}["S2-other"] == "other"


def test_naming_a_pair_retags_every_stored_page_with_it(tmp_path):
    """#80: a pair named from one row names it on every page row that has it (older unknown ones too)."""
    clock = Clock()
    c = cad(tmp_path, clock)
    t = clock.t
    for k in range(3):
        c.record_traffic(dict(tx(f"S2-{k}", t + k, "South 2", "", "60 Control", None, "words"),
                              tones=[{"a_hz": 741.3 + 0.1 * k, "b_hz": 623.7, "names": [], "ours": False}], voice_at=4.0))
    c.record_traffic(dict(tx("S2-x", t + 9, "South 2", "", "60 Control", None, "words"),
                          tones=[{"a_hz": 349.1, "b_hz": 311.1, "names": ["Port Chester FD"], "ours": True}], voice_at=4.0))
    match = lambda a, b: ["Purchase"] if abs(a - 741.3) <= 0.015 * 741.3 else ["Port Chester FD"] if abs(a - 349.1) < 1 else []
    assert c.comms.retag_pages(match, lambda names: "Port Chester FD" in names) == 3        # the three (within tolerance), not the known one
    rows = {r["id"]: r for r in c.feed.rows(limit=20)}
    assert all(rows[f"S2-{k}"]["page"] == "other" and rows[f"S2-{k}"]["tones"][0]["names"] == ["Purchase"] for k in range(3))
    assert rows["S2-x"]["page"] == "ours"
    assert c.comms.retag_pages(match, lambda names: "Port Chester FD" in names) == 0        # nothing left to change
