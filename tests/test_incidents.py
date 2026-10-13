from sizeup.dispatch import parse
from sizeup.incidents import IncidentStore, Traffic

PAGE = ("Port Chester Fire Department, respond to an activated fire alarm. 209 West Chester Avenue, "
        "cross of Pearl Street, Port Chester. Time out 1432.")


def store(clock, town="Port Chester"):
    return IncidentStore({"Port Chester FD", "Rye Brook FD"},
                         geocode=lambda d: {"lat": 41.0025, "lon": -73.6669, "label": d.location,
                                            "kind": "address", "town": town, "confidence": 0.9},
                         clock=lambda: clock["t"])


def test_page_opens_repeat_merges_traffic_attaches_then_closes():
    clock = {"t": 1000.0}
    s = store(clock)
    inc = s.on_page("p1", ["Port Chester FD"], parse(PAGE))
    assert inc.incident_type == "Fire alarm" and inc.geo["lat"] == 41.0025 and inc.status == "active"
    clock["t"] += 40
    assert s.on_page("p2", ["Port Chester FD"], parse(PAGE)) is inc and inc.pages == ["p1", "p2"]
    clock["t"] += 60
    s.on_traffic(Traffic("t1", "Fire/EMS 15", clock["t"], clock["t"] + 3, "2391 on location, nothing showing"))
    assert [t.id for t in inc.traffic] == ["t1"]
    clock["t"] += 3700
    assert s.active() is None and inc.status == "closed"
    s.on_traffic(Traffic("t2", "FG1", clock["t"], clock["t"] + 2, "radio check"))
    assert s.state()["loose_traffic"][0]["id"] == "t2"


def test_only_our_tones_open_an_incident():
    # #80, 2026-10-13 16:02: a page with Fairview's tones at "34 Longdale Avenue" was taken as ours because the
    # geocoder weakly read the street as Port Chester's. Only PCFD's or Rye Brook's tones open one now.
    clock = {"t": 0.0}
    assert store(clock, town="Rye").on_page("p1", ["Rye FD"], parse("Rye fire department respond to a car fire Purchase Street")) is None
    s = store(clock)                                                             # every address is in Port Chester here
    assert s.on_page("p2", ["Fairview"], parse("Fairview, 34 Longdale Avenue, smoke in the building")) is None   # another department's tones
    assert s.on_page("p3", [], parse(PAGE)) is None                              # no tones decoded: not even "Port Chester Fire Department" in the voice
    assert s.on_page("p4", [], parse("60 Control, Engine 60, 75 Grasslands Road, party passed out")) is None   # a unit named: not enough
    assert s.on_page("p5", ["Port Chester FD"], parse(PAGE)) is not None         # our tones: ours
    elsewhere = store(clock, town="Valhalla")
    assert elsewhere.on_page("p6", ["Port Chester FD"], parse(PAGE)) is not None   # our tones: ours anywhere (mutual aid)
    assert elsewhere.on_page("p7", ["Rye Brook FD (est.)"], parse(PAGE)) is not None   # an estimated code still names them
    assert elsewhere.on_page("p8", ["Fairview (heard)", "Port Chester FD"], parse(PAGE)) is not None   # stacked with ours
    assert s.is_ours(["Fairview"]) is False and s.is_ours(["Port Chester FD (heard)"]) is True


