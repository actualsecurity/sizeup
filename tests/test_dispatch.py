from sizeup.dispatch import parse


def test_bench_transcript():
    # large-v3-turbo output for a spoken test dispatch (bench, 2026-10-01).
    d = parse("Port Chester Fire Department, respond to an activated fire alarm. 209 West Chester Avenue, "
              "cross of Pearl Street, Port Chester. Time out 1432.")
    assert d.departments == ["Port Chester FD"]
    assert (d.incident_type, d.category) == ("Fire alarm", "fire")
    assert d.location == "209 west chester avenue"
    assert d.cross_street == "pearl street"
    assert d.time_out == "14:32"


def test_spoken_numbers_and_other_types():
    d = parse("Rye Brook fire, respond for a report of an odor of gas, nine forty King Street, "
              "time out one five zero five")
    assert d.departments == ["Rye Brook FD"] and d.incident_type == "Gas leak"
    assert d.time_out == "15:05" and "king street" in d.location
    d = parse("Port Chester, 10-75, working fire, 51 Grace Church Street")
    assert d.incident_type == "Working fire" and d.location.endswith("grace church street")


def test_unknown_type_is_not_invented():
    d = parse("Port Chester fire department, 123 Main Street")
    assert d.incident_type == "Unknown" and d.category == "unknown"
