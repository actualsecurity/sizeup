from sizeup.dispatch import parse
from sizeup.incidents import IncidentStore, Traffic

PAGE = ("Port Chester Fire Department, respond to an activated fire alarm. 209 West Chester Avenue, "
        "cross of Pearl Street, Port Chester. Time out 1432.")


def store(clock):
    return IncidentStore({"Port Chester FD", "Rye Brook FD"},
                         geocode=lambda d: {"lat": 41.0025, "lon": -73.6669, "label": d.location,
                                            "kind": "address", "confidence": 0.9},
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


def test_other_departments_do_not_open_incidents():
    clock = {"t": 0.0}
    s = store(clock)
    assert s.on_page("p1", ["Rye FD"], parse("Rye fire department respond to a car fire Purchase Street")) is None
    assert s.on_page("p2", [], parse(PAGE)) is not None  # tones not decoded, but the voice names PCFD
