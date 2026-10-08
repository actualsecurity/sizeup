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


REAL_PAGE = ("The show 30 Alpha 5 engine 25 42 42 Stratton Road in Regal Drive in Van Eck Drive. The 45-year-old "
             "female features. 30 Alpha 5 engine 25 42 42 Stratton Road in Regal Drive in Van Eck Drive. The "
             "45-year-old female features in 32-63.")  # South 2, 2026-10-01 15:32 (large-v3-turbo, indoor antenna)


def test_real_south2_page_format():
    d = parse(REAL_PAGE)
    assert (d.incident_type, d.category, d.code) == ("Traumatic injury", "ems", "30-A-5")
    assert d.units == ["Engine 25"]
    assert d.location == "42 stratton road"                       # "42 42" was the model repeating itself
    assert d.cross_street == "regal drive and van eck drive"      # "in ... in ..." = the two cross streets
    assert d.departments == []


def test_fire_code_and_units():
    d = parse("69 Delta 6, Engine 61, Ladder 31, 209 Westchester Avenue, Pearl Street and Gilbert Place")
    assert d.category == "fire" and d.code == "69-D-6" and d.units == ["Engine 61", "Ladder 31"]
    assert d.location == "209 westchester avenue" and d.cross_street == "pearl street and gilbert place"


def test_real_mutual_aid_page_with_time_out_at_the_end():
    # South 2, 2026-10-01 15:40, tones 928.1/810.2 (Scarsdale EMS).
    d = parse("1612 Scars LMS, mutual aid to Yonkers, 349 Helena Avenue between Alta Vista Drive and Canfield "
              "Avenue. A female unable to ambulate. Scars LMS, mutual aid to Yon kers, 349 Helena Avenue between "
              "Alta Vista Drive and Can field Avenue. A female unable to ambulate. 153965.")
    assert d.incident_type == "Mutual aid" and d.location == "349 helena avenue"
    assert d.cross_street == "alta vista drive and canfield avenue" and d.time_out == "15:39"


def test_real_page_with_bare_engine_numbers():
    # South 2, 2026-10-01 15:43, tones 832.5/1201.0: Fairview (Engines 174/175 + Ladder 1 in the roster).
    d = parse("It's Challenger, 174-175-Ladder 1-2122-443-443-Terrytown Road, Phoenix Salon, between Manhattan "
              "Avenue and Yellow stone Avenue, Commercial Line. Challenger, 174-175-Ladder 1-2122-443-443-Terrytown "
              "Road, Phoenix Salon, between Manhattan Avenue, Yellowstone Avenue , Commercial Line. 143-62.")
    assert d.units == ["Ladder 1"] and d.numbers[:2] == [174, 175]   # the app maps 174/175 via the roster
    assert d.incident_type == "Fire alarm" and d.location == "443 terrytown road"
    assert d.cross_street.startswith("manhattan avenue and yellow")


def test_real_highway_page_without_house_number():
    # South 2, 2026-10-01 16:00, tones 496.8/767.4 (Eastchester VAC, voice "EVAC").
    d = parse("6-2 Control EVAC, one ambulance requested to mutilate to Y onkers, the Springbrook Parkway "
              "southbound at the Bronx River Parkway interchange, mile marker 0. For a motor vehicle accident "
              "with injuries, you'll be the third ambulance.")
    assert d.incident_type == "MVA"
    assert d.location == "springbrook parkway" and d.cross_street == "bronx river parkway"


def test_run_number_is_not_read_as_engines():
    # South 2, 2026-10-08 15:12: a Rye page whose run number was read as Engines 16, 10, 191 and 22.
    d = parse("16-10-191-22, it's 22 Locust Avenue, between Mead Place and Haviland Lane, outside order of gas, "
              "kind of done seen. 16-10-191-22, 22 Locust Avenue, between Mead Place and H aviland Lane, "
              "outside order of gas, 1506-615.")
    assert d.numbers == [] and d.units == [] and d.location == "22 locust avenue"
