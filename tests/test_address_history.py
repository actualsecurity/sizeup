"""CAD query and address history (#403): the address and building keys (sizeup/cad/addrkey.py), their migration and
backfill, History here, the Search page with its FTS index and audit row, roles, and premise notes
(sizeup/cad/premise.py). Invented addresses and calls only."""

import difflib
import json
import re
import sqlite3
import time

import pytest

from sizeup.cad import addrkey, lists
from sizeup.cad.api import CadApi
from sizeup.cad.history import ADDR_COLUMNS, fts_query, wait_idle
from sizeup.cad.premise import rank
from sizeup.cloud.access import Identity
from tests.test_cad import make_cad, uid
from tests.test_roles import COREY, JULIAN, VAL, call, people

ALIASES = lists.load().geo_town_aliases
# An invented gazetteer: street -> (as written, town, lat of number 0, lon), each number 10 m further north
STREETS = {"example ave": ("Example Ave", "Port Chester", 41.0, -73.66),
           "w example ave": ("W Example Ave", "Port Chester", 41.03, -73.66),        # a direction's own street
           "e example ave": ("E Example Ave", "Port Chester", 41.04, -73.66),
           "n example ave": ("N Example Ave", "Port Chester", 41.09, -73.66),
           "s example ave": ("S Example Ave", "Port Chester", 41.10, -73.66),
           "sample st": ("Sample St", "Port Chester", 41.01, -73.67),
           "specimen rd": ("Specimen Rd", "Rye Brook", 41.02, -73.68),
           "front st": ("Front St", "Port Chester", 41.05, -73.66),                  # streets named with unit words
           "upper ridge rd": ("Upper Ridge Rd", "Port Chester", 41.06, -73.66),
           "rear lot rd": ("Rear Lot Rd", "Port Chester", 41.07, -73.66),
           "at rd": ("At Rd", "Port Chester", 41.08, -73.66)}
DAY = 86400


def fake_geocode(q: str) -> list[dict]:
    """Like Cad.geocoder (sizeup/app.py _geocode_text): a house number + street read with a confidence from how close
    the street is spelt; a street alone as a street."""
    s = " ".join(q.replace("_", " ").split(",")[0].split()).lower()
    m = re.match(r"(\d+)[a-z]?\s+(.*)$", s)
    num, rest = (m.group(1), m.group(2)) if m else ("", s)
    key = addrkey.street_key(rest)
    best = max(STREETS, key=lambda k: difflib.SequenceMatcher(None, k, key).ratio())
    conf = round(difflib.SequenceMatcher(None, best, key).ratio(), 2)
    name, town, lat, lon = STREETS[best]
    if num:
        return [{"kind": "address", "street": name, "house_number": num, "municipality": town, "confidence": conf,
                 "lat": lat + int(num) * 0.00009, "lon": lon, "label": f"{num} {name}, {town}", "precision": "exact"}]
    return [{"kind": "street", "street": name, "house_number": "", "municipality": town, "confidence": conf,
             "lat": lat, "lon": lon, "label": f"{name}, {town}", "precision": ""}]


def key(text, geocode=fake_geocode, **inc):
    return addrkey.key_incident({"address": text, **inc}, ALIASES, lambda m: m, geocode)


# -- the address key: the variants the department sees -------------------------------------------------------------

@pytest.mark.parametrize("text, addr, bldg, how", [
    ("100 Example Ave, Port Chester", "100|example ave|port chester", "100|example ave|port chester", "gazetteer"),
    ("100 EXAMPLE AVE", "100|example ave|port chester", "100|example ave|port chester", "gazetteer"),          # caps
    ("100 Example Avenue", "100|example ave|port chester", "100|example ave|port chester", "gazetteer"),       # suffix
    ("100 Example Ave.", "100|example ave|port chester", "100|example ave|port chester", "gazetteer"),
    ("100 EXAMPLE AVE APT 2B, PORT_CHESTER", "100|example ave|port chester|2b", "100|example ave|port chester", "gazetteer"),
    ("100 Example Ave #2B", "100|example ave|port chester|2b", "100|example ave|port chester", "gazetteer"),
    ("100 Example Ave Unit 2B", "100|example ave|port chester|2b", "100|example ave|port chester", "gazetteer"),
    ("100 EXAMPLE AVE 3RD FL", "100|example ave|port chester|fl 3", "100|example ave|port chester", "gazetteer"),
    ("100-104 EXAMPLE AVE", "100-104|example ave|port chester", "100-104|example ave|port chester", "gazetteer"),  # range
    ("100A Example Ave", "100a|example ave|port chester", "100a|example ave|port chester", "gazetteer"),        # its own
    ("100 1/2 Example Ave", "100 1/2|example ave|port chester", "100 1/2|example ave|port chester", "gazetteer"),
    ("100 Example Ave Rear", "100|example ave|port chester|rear", "100|example ave|port chester", "gazetteer"),
    ("100 Example Ave Bldg 3 Apt 2B", "100|example ave|port chester|bldg 3|2b", "100|example ave|port chester|bldg 3",
     "gazetteer"),                                                         # a complex: BLDG is a building
    ("12 W EXAMPLE AVE", "12|w example ave|port chester", "12|w example ave|port chester", "gazetteer"),       # direction
    ("12W EXAMPLE AVE", "12|w example ave|port chester", "12|w example ave|port chester", "gazetteer"),
    ("7 Front St", "7|front st|port chester", "7|front st|port chester", "gazetteer"),                          # street
    ("7 Upper Ridge Rd", "7|upper ridge rd|port chester", "7|upper ridge rd|port chester", "gazetteer"),       # names,
    ("7 Rear Lot Rd", "7|rear lot rd|port chester", "7|rear lot rd|port chester", "gazetteer"),                # not units
    ("7 At Rd", "7|at rd|port chester", "7|at rd|port chester", "gazetteer"),
    ("100 Exampel Ave", "100|example ave|port chester", "100|example ave|port chester", "gazetteer"),          # misspelt
    ("@SAMPLE SCHOOL (100 EXAMPLE AVE)", "100|example ave|port chester", "100|example ave|port chester", "gazetteer"),
    ("100 Example Ave @ Sample Market", "100|example ave|port chester", "100|example ave|port chester", "gazetteer"),
    ("7 SPECIMEN RD RYE_BROOK", "7|specimen rd|rye brook", "7|specimen rd|rye brook", "gazetteer"),
    ("@ Sample School", "", "", ""),                                       # a place only, no pin: nothing to key
    ("Example Ave / Sample St", "", "", ""),                               # an intersection
])
def test_address_key_normalization(text, addr, bldg, how):
    k = key(text)
    assert (k.addr_key, k.bldg_key, k.how) == (addr, bldg, how)


def test_text_keys_without_a_gazetteer_normalise_the_same_way():
    a = key("100 EXAMPLE AVE APT 2B, PORT_CHESTER", geocode=None)
    b = key("100 Example Avenue, Port Chester", geocode=None)
    assert a.how == b.how == "text" and a.bldg_key == b.bldg_key == "100|example ave|port chester"
    assert a.addr_key == "100|example ave|port chester|2b" and b.label == "100 Example Ave, Port Chester"
    assert key("100 Example Ave", geocode=None, geo_label="100 Example Ave, Port Chester", lat=41.0, lon=-73.66).bldg_key \
        == "100|example ave|"                                            # the location as typed comes first


def test_a_misheard_street_is_keyed_by_its_pin_not_snapped():
    k = key("100 Zqxv Blvd", lat=41.005, lon=-73.661)
    assert (k.how, k.bldg_key, k.addr_key) == ("geo", "", "@41.00500,-73.66100")
    assert key("100 Zqxv Blvd").how == "text"                          # no pin either: the text, as typed
    assert key("Example Ave / Sample St", lat=41.005, lon=-73.661).how == "geo"     # an intersection with a pin


def test_a_reading_far_from_the_pin_is_not_trusted():
    assert key("100 Example Ave", lat=41.0 + 100 * 0.00009, lon=-73.66).how == "gazetteer"
    assert key("100 Example Ave", lat=41.05, lon=-73.66).how == "geo"        # ~4 km away: the pin wins


@pytest.mark.parametrize("a, b", [("12 N Example Ave", "12 S Example Ave"), ("12 E Example Ave", "12 W Example Ave"),
                                  ("12 N EXAMPLE AVE", "12 EXAMPLE AVE"), ("12W EXAMPLE AVE", "12 EXAMPLE AVE"),
                                  ("12 Example Ave", "112 Example Ave")])
def test_direction_pairs_are_never_one_building(a, b):
    for geo in (fake_geocode, None):
        assert key(a, geocode=geo).bldg_key != key(b, geocode=geo).bldg_key, (a, b, geo)


def test_the_text_names_the_town_with_no_pin():
    assert key("7 Specimen Rd, Port Chester").how == "text"     # the reading is in Rye Brook: not this address
    assert key("7 Specimen Rd, Rye Brook").how == "gazetteer"


def test_parse_and_street_text():
    p = addrkey.parse("12-14 EXAMPLE AVE REAR, RYE_BROOK", ALIASES)
    assert (p.number, p.street, p.unit, p.town, p.ends()) == ("12-14", "EXAMPLE AVE", "rear", "Rye Brook", ("12", "14"))
    p = addrkey.parse("12 EXAMPLE AVE BLDG 3 APT 2B PORT_CHESTER", ALIASES)
    assert (p.number, p.street, p.bldg, p.unit, p.town) == ("12", "EXAMPLE AVE", "bldg 3", "2b", "Port Chester")
    assert addrkey.parse("12 Lot Rd", ALIASES).street == "Lot Rd" and addrkey.parse("12 Example Rd Lot 7", ALIASES).unit == "lot 7"
    assert addrkey.street_text("575 Example Rd @ Sample Market") == "575 Example Rd"
    assert addrkey.street_text("@ Sample School") == ""
    assert addrkey.parse("Example Ave", ALIASES) is None


# -- the migration: keys, indexes, triggers, backfill ------------------------------------------------------------------

def test_an_older_database_gets_the_columns_and_is_backfilled(tmp_path):
    c = make_cad(tmp_path, geocoder=fake_geocode)
    ids = [c.store.create_incident(type="ALARM", subtype="COMM", address=f"{n} Example Ave") for n in (100, 100, 102)]
    c.shutdown()
    db = sqlite3.connect(tmp_path / "cad.sqlite")            # as it was before #403: none of it
    for t in ("addr_queue", "here_rev", "fts_radio_queue"):
        db.execute(f"DROP TABLE {t}")
    for t in ("fts_incidents", "fts_events", "fts_radio"):
        db.execute(f"DROP TABLE {t}")
    for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type IN ('trigger', 'index') AND "
                              "(name LIKE 'fts_%' OR name LIKE 'addr_%' OR name LIKE 'here_%' OR name LIKE 'incidents_%')").fetchall():
        db.execute(f"DROP {'TRIGGER' if name.startswith(('fts_', 'addr_queue', 'here_')) else 'INDEX'} IF EXISTS {name}")
    for col in ADDR_COLUMNS:
        db.execute(f"ALTER TABLE incidents DROP COLUMN {col}")
    db.execute("DELETE FROM meta WHERE key = 'search_fts'")
    db.commit()
    db.close()
    c = make_cad(tmp_path, geocoder=fake_geocode)
    assert any("address keys" in m for m in c.store.migrated)
    assert c.addrs.left()["keys"] == 3                     # queued: the background thread keys them
    wait_idle(c.addrs)
    assert [c.store.incident(i)["bldg_key"] for i in ids] == ["100|example ave|port chester"] * 2 + ["102|example ave|port chester"]
    idx = {r["name"] for r in c.store.q("SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert {"incidents_bldg", "incidents_street", "incidents_point", "assignments_incident", "assignments_unit"} <= idx
    plan = " ".join(r["detail"] for r in c.store.q("EXPLAIN QUERY PLAN SELECT id FROM incidents WHERE bldg_key = ? "
                                                    "AND number IS NOT NULL", ("x",)))
    assert "incidents_bldg" in plan                        # History here never scans
    make_cad(tmp_path, geocoder=fake_geocode)              # opened again: nothing to do twice


def test_a_moved_address_is_keyed_again_before_the_push():
    c = make_cad(geocoder=fake_geocode)
    iid = c.store.create_incident(type="ALARM", subtype="COMM", address="100 Example Ave")
    inc = next(i for i in c.state()["incidents"] if i["id"] == iid)
    assert inc["bldg_key"] == "100|example ave|port chester"
    c.update(iid, {"address": "7 Sample St"})
    inc = next(i for i in c.state()["incidents"] if i["id"] == iid)
    assert (inc["bldg_key"], inc["addr_label"]) == ("7|sample st|port chester", "7 Sample St, Port Chester")


# -- History here ------------------------------------------------------------------------------------------------------

class Clock:
    def __init__(self, t=1_790_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def call_at(c, clock, days_ago, address, type="ALARM", subtype="COMM", disposition="COMPLETE", units=(), **kw):
    t = clock.t
    clock.t = t - days_ago * DAY
    iid = c.store.create_incident(type=type, subtype=subtype, address=address, **kw)
    if units:
        c.store.assign(iid, [uid(c, u) for u in units])
    if disposition:
        c.store.close(iid, disposition)
    clock.t = t
    return iid


@pytest.fixture
def hist():
    clock = Clock()
    c = make_cad(geocoder=fake_geocode, clock=clock)
    c.clock_ = clock
    return c


def test_history_here_lists_the_building_newest_first(hist):
    c, clock = hist, hist.clock_
    old = call_at(c, clock, 500, "100 Example Ave", disposition="FALSE")                    # older than 12 months
    f1 = call_at(c, clock, 40, "100 EXAMPLE AVE APT 2B, PORT_CHESTER", disposition="FALSE", units=("E60",))
    f2 = call_at(c, clock, 20, "100 Example Avenue", disposition="UNFOUNDED")
    ems = call_at(c, clock, 10, "100 Example Ave #3", type="BLS", subtype="", disposition="TXBLS")
    call_at(c, clock, 5, "102 Example Ave", disposition="FALSE")                              # next door: not here
    s1 = call_at(c, clock, 3, "100 Example Ave", type="STRU", subtype="RESD", disposition="FIRE", units=("E60", "L31"))
    for n in range(2):
        call_at(c, clock, 2 - n * 0.1, "100 Example Ave", type="SERVICE", subtype="", disposition="SERVICE")
    now = c.store.create_incident(type="ALARM", subtype="COMM", address="100 Example Ave")
    h = c.addrs.here(now)
    assert h["key"]["bldg_key"] == "100|example ave|port chester"
    assert h["total"] == 7 and len(h["recent"]) == 5
    assert [r["created_at"] for r in h["recent"]] == sorted((r["created_at"] for r in h["recent"]), reverse=True)
    assert [r["id"] for r in h["recent"]][2:] == [s1, ems, f2]
    assert h["recent"][2]["units"] == ["E60", "L31"] and h["recent"][2]["disposition"] == "FIRE"
    assert h["year"]["n"] == 6
    assert h["year"]["by_type"] == [{"type": "ALARM", "n": 2}, {"type": "SERVICE", "n": 2}, {"type": "BLS", "n": 1},
                                    {"type": "STRU", "n": 1}]
    assert h["false_alarms"]["n"] == 2 and {x["id"] for x in h["false_alarms"]["calls"]} == {f1, f2}
    assert old not in [r["id"] for r in h["recent"]] and now not in [r["id"] for r in h["recent"]]


def test_history_is_cached_and_invalidated_on_write(hist):
    c, clock = hist, hist.clock_
    a = call_at(c, clock, 5, "100 Example Ave", disposition="")
    now = c.store.create_incident(type="ALARM", subtype="COMM", address="100 Example Ave")
    c.addrs.here(now)
    queries = []
    c.store.db.set_trace_callback(queries.append)
    h = c.addrs.here(now)                                      # cached: only the counters are read
    c.store.db.set_trace_callback(None)
    assert not any("FROM incidents WHERE bldg_key" in q for q in queries)
    assert h["recent"][0]["disposition"] == ""
    c.store.close(a, "FALSE")                                  # a write there: recomputed
    assert c.addrs.here(now)["recent"][0]["disposition"] == "FALSE"
    c.store.assign(now, [uid(c, "E60")])
    other = c.store.create_incident(type="BLS", address="100 Example Ave")
    assert c.addrs.here(other)["recent"][0]["units"] == ["E60"]


def test_point_only_calls_group_within_20_m(hist):
    c, clock = hist, hist.clock_
    lat, lon = 41.0 + 100 * 0.00009, -73.66                     # where 100 Example Ave is
    near = call_at(c, clock, 3, "Zqxv Blvd / Sample St", lat=lat + 0.0001, lon=lon)        # ~11 m: a misheard page
    far = call_at(c, clock, 3, "Zqxv Blvd / Sample St", lat=lat + 0.0004, lon=lon)         # ~44 m
    keyed = call_at(c, clock, 2, "100 Example Ave", lat=lat, lon=lon)
    nextdoor = call_at(c, clock, 2, "101 Example Ave", lat=lat + 0.00009, lon=lon)         # 10 m away, its own key
    now = c.store.create_incident(type="ALARM", subtype="COMM", address="100 Example Ave", lat=lat, lon=lon)
    ids = {r["id"] for r in c.addrs.here(now)["recent"]}
    assert near in ids and keyed in ids and far not in ids and nextdoor not in ids
    geo = c.store.create_incident(type="ALARM", subtype="COMM", address="Zqxv Blvd", lat=lat + 0.00005, lon=lon)
    ids = {r["id"] for r in c.addrs.here(geo)["recent"]}
    assert {near, keyed, nextdoor} <= ids and far not in ids


# -- Search ------------------------------------------------------------------------------------------------------------

def search(c, **q):
    return c.addrs.search(c.addrs.parse_query({k: [str(v)] for k, v in q.items()}))


@pytest.fixture
def calls(hist):
    c, clock = hist, hist.clock_
    ids = {
        "alarm": call_at(c, clock, 30, "100 Example Ave Apt 2B", disposition="FALSE", units=("E60",),
                         remarks="caller reports burnt toast"),
        "ems": call_at(c, clock, 20, "100 Example Ave", type="BLS", subtype="", disposition="TXBLS"),
        "fire": call_at(c, clock, 10, "7 Sample St", type="STRU", subtype="RESD", disposition="FIRE", units=("E60", "L31")),
        "rb": call_at(c, clock, 5, "7 Specimen Rd, Rye Brook", disposition="COMPLETE", units=("E61",)),
    }
    c.store.note(ids["ems"], "patient on home oxygen, knox box at side door")
    c.record_traffic({"id": "clip-1", "start": clock.t - 10 * DAY, "end": clock.t - 10 * DAY + 4, "channel": "FG1",
                      "text": "Ladder 31 is fine, heavy smoke second floor"})
    c.store.db.execute("INSERT INTO radio_links (traffic_id, incident_id, why) VALUES ('clip-1', ?, 'manual')", (ids["fire"],))
    return c, ids


def test_search_filters(calls):
    c, ids = calls
    assert search(c)["total"] == 4
    assert {r["id"] for r in search(c, where="100 EXAMPLE AVE")["rows"]} == {ids["alarm"], ids["ems"]}     # the building
    assert {r["id"] for r in search(c, where="example avenue")["rows"]} == {ids["alarm"], ids["ems"]}      # the street
    assert {r["id"] for r in search(c, where="specimen")["rows"]} == {ids["rb"]}                          # words in it
    assert {r["id"] for r in search(c, type="ALARM")["rows"]} == {ids["alarm"], ids["rb"]}
    assert {r["id"] for r in search(c, type="STRU/RESD")["rows"]} == {ids["fire"]}
    assert {r["id"] for r in search(c, unit="E60")["rows"]} == {ids["alarm"], ids["fire"]}
    assert {r["id"] for r in search(c, disposition="FALSE")["rows"]} == {ids["alarm"]}
    t = c.clock()
    assert {r["id"] for r in search(c, **{"from": t - 15 * DAY})["rows"]} == {ids["fire"], ids["rb"]}
    assert {r["id"] for r in search(c, **{"to": t - 15 * DAY})["rows"]} == {ids["alarm"], ids["ems"]}
    assert {r["id"] for r in search(c, unit="E60", where="100 Example Ave")["rows"]} == {ids["alarm"]}
    r = search(c, where="7 Sample St")["rows"][0]
    assert (r["number"], r["units"], r["disposition"], r["addr_label"]) == (
        c.store.incident(ids["fire"])["number"], ["E60", "L31"], "FIRE", "7 Sample St, Port Chester")
    assert [r["id"] for r in search(c)["rows"]] == [ids["rb"], ids["fire"], ids["ems"], ids["alarm"]]   # newest first


def test_search_pages(calls):
    c, ids = calls
    p0, p1 = search(c, size=3), search(c, size=3, page=1)
    assert (p0["total"], len(p0["rows"]), len(p1["rows"])) == (4, 3, 1)
    assert p1["rows"][0]["id"] == ids["alarm"]


def test_search_text_over_remarks_comments_and_transcripts(calls):
    c, ids = calls
    assert {r["id"] for r in search(c, text="toast")["rows"]} == {ids["alarm"]}                  # remarks
    hit = search(c, text="oxyg")["rows"]                                                       # a comment, by prefix
    assert [r["id"] for r in hit] == [ids["ems"]] and "[oxygen]" in hit[0]["hit"]
    assert {r["id"] for r in search(c, text='"heavy smoke"')["rows"]} == {ids["fire"]}           # a transcript, a phrase
    assert search(c, text='"smoke heavy"')["total"] == 0
    assert search(c, text="toast", type="STRU")["total"] == 0
    c.store.reopen(ids["rb"])
    c.store.update_incident(ids["rb"], {"remarks": "panel in basement"})                     # remarks edited
    assert {r["id"] for r in search(c, text="basement")["rows"]} == {ids["rb"]}
    c.record_traffic({"id": "clip-1", "start": c.clock() - 10 * DAY, "end": c.clock() - 10 * DAY + 4, "channel": "FG1",
                      "text": "Ladder 31 on location, nothing showing"})                         # re-transcribed
    assert search(c, text="nothing showing")["total"] == 1 and search(c, text="heavy")["total"] == 0


def test_repaired_status_words_are_searchable(calls):
    c, ids = calls

    def fix(rows):
        for r in rows:
            if "is fine" in r["text"]:
                r["fixed_text"] = r["text"].replace("is fine", "en route")
        return rows
    c.hints.decorate = fix
    c.store.db.execute("UPDATE radio_traffic SET text = text || '.' WHERE id = 'clip-1'")       # queued again
    assert {r["id"] for r in search(c, text='"en route"')["rows"]} == {ids["fire"]}
    assert {r["id"] for r in search(c, text='"is fine"')["rows"]} == {ids["fire"]}               # as heard, too


def test_fts_query():
    assert fts_query('smoke "en route" E-60') == '"smoke"* "en route" "E"* "60"*'
    assert fts_query('"') == "" and fts_query("") == ""
    assert fts_query('a" OR b') == '"a"* "OR"* "b"*'


# -- the API: roles, the CSV, the audit row ------------------------------------------------------------------------------

@pytest.fixture
def api(calls):
    c, ids = calls
    a = CadApi(c)
    a.ids = people(c)
    a.calls = ids
    return a


def get(api, path, query="", email=None):
    code, raw, ctype, *_ = api.get(path, query, Identity(email=email) if email else None)
    return code, (json.loads(raw) if "json" in ctype else raw)


def test_search_is_audited_with_who_filters_and_count(api):
    code, out = get(api, "/api/cad/search", "where=100+Example+Ave&type=ALARM&text=toast", VAL)
    assert code == 200 and out["total"] == 1
    (r,) = api.cad.activity.rows(kind="search")
    assert r["email"] == VAL and r["route"] == "GET /api/cad/search"
    assert r["summary"] == "Searched where=100 Example Ave, type=ALARM, text=toast: 1 result"
    get(api, "/api/cad/search", "where=100+Example+Ave&type=ALARM&text=toast&page=1", VAL)     # the next page: not again
    assert len(api.cad.activity.rows(kind="search")) == 1


def test_viewers_search_admins_export(api):
    assert get(api, "/api/cad/search", "", VAL)[0] == 200
    code, out = get(api, "/api/cad/search.csv", "", VAL)
    assert code == 403 and "Admins only" in out["error"]
    assert get(api, "/api/cad/search.csv", "", JULIAN)[0] == 403
    code, raw = get(api, "/api/cad/search.csv", "unit=E60", COREY)
    lines = raw.decode("utf-8-sig").splitlines()
    assert code == 200 and lines[0].startswith("Number,Received,Closed,Type") and len(lines) == 3
    assert [r["summary"] for r in api.cad.activity.rows(kind="search", email=COREY)] == ["CSV: Searched unit=E60: 2 results"]


def test_history_here_api(api):
    code, out = get(api, f"/api/cad/incident/{api.calls['ems']}/here", "", VAL)
    assert code == 200 and [r["id"] for r in out["recent"]] == [api.calls["alarm"]]
    assert out["false_alarms"]["n"] == 1


# -- premise notes ---------------------------------------------------------------------------------------------------------

def test_notes_crud_audit_and_roles(api):
    iid = api.calls["ems"]
    code, out = call(api, "/api/cad/premise", {"incident_id": iid, "text": "Knox box at side door", "category": "access"}, VAL)
    assert code == 403                                                    # viewers only look
    code, out = call(api, "/api/cad/premise", {"incident_id": iid, "text": "Knox box at side door", "category": "access"},
                     JULIAN)
    assert code == 200
    n = out["note"]
    assert (n["label"], n["category"], n["priority"], n["author_name"], n["active"]) == (
        "100 Example Ave, Port Chester", "access", 2, "Julian", True)
    code, out = call(api, f"/api/cad/premise/{n['id']}", {"text": "Knox box at rear door", "priority": 1}, JULIAN)
    assert code == 200 and out["note"]["text"] == "Knox box at rear door" and out["note"]["priority"] == 1
    code, out = call(api, f"/api/cad/premise/{n['id']}", {"action": "expire"}, COREY)
    assert code == 200 and out["note"]["active"] is False
    assert get(api, "/api/cad/premise", "", VAL)[1]["notes"] == []
    assert len(get(api, "/api/cad/premise", "all=1&where=100+example+ave", VAL)[1]["notes"]) == 1
    audit = api.cad.store.q("SELECT * FROM settings_audit WHERE section = 'premise' ORDER BY id")
    assert [json.loads(a["new"] or "null") for a in audit][1:] == [{"text": "Knox box at rear door", "priority": 1}, {"active": 0}]
    assert [a["operator"].split(" <")[0] for a in audit] == ["Julian", "Julian", "Corey"]
    acts = [r["summary"] for r in api.cad.activity.rows(kind="action")]
    assert f"Expired address note {n['id']}" in acts and not any("Knox" in s for s in acts)   # never the text
    code, out = call(api, "/api/cad/premise", {"where": "Example Ave / Sample St", "text": "x"}, JULIAN)
    assert code == 400


def test_notes_surface_on_open_incidents_ranked_with_acks(hist):
    c, clock = hist, hist.clock_
    mk = c.premise.create
    a = mk({"where": "100 Example Ave", "text": "O2 in use, 2nd floor", "category": "medical", "operator": "A <a@x.org>"})["note"]
    clock.t += 1
    b = mk({"where": "100 EXAMPLE AVE APT 4", "text": "Hoarding in basement", "category": "hoarding"})["note"]
    clock.t += 1
    h = mk({"where": "100 Example Ave", "text": "Propane tanks in yard", "category": "hazard", "priority": 1})["note"]
    mk({"where": "106 Example Ave", "text": "Dog", "category": "animals"})                    # 60 m down the street
    clock.t += 1
    stru = c.store.create_incident(type="STRU", subtype="RESD", address="100 Example Ave")
    ems = c.store.create_incident(type="BLS", address="100 Example Ave Apt 2")
    elsewhere = c.store.create_incident(type="BLS", address="7 Sample St")
    by = {i["id"]: i for i in c.state()["incidents"]}
    assert [n["id"] for n in by[stru]["premise"]] == [h["id"], b["id"], a["id"]]            # priority, then hazard/hoarding
    assert [n["id"] for n in by[ems]["premise"]] == [h["id"], a["id"], b["id"]]              # ... then medical first
    assert by[elsewhere]["premise"] == []
    out = c.premise.ack(h["id"], {"incident_id": stru, "operator": "Julian <julian@example.org>"})
    assert out["ack"]["by"] == "Julian"
    by = {i["id"]: i for i in c.state()["incidents"]}
    assert by[stru]["premise"][0]["ack"]["by"] == "Julian" and by[ems]["premise"][0]["ack"] is None   # per incident
    clock.t += 5
    c.premise.edit(h["id"], {"text": "Propane tanks moved to garage"})                         # edited: asked again
    by = {i["id"]: i for i in c.state()["incidents"]}
    assert by[stru]["premise"][0]["ack"] is None
    c.update(elsewhere, {"address": "100 Example Ave"})                                         # moved there: shown
    by = {i["id"]: i for i in c.state()["incidents"]}
    assert len(by[elsewhere]["premise"]) == 3


def test_note_expiry_and_review(hist):
    c, clock = hist, hist.clock_
    n = c.premise.create({"where": "100 Example Ave", "text": "Scaffolding on front", "category": "hazard",
                          "expires_at": clock.t + 2 * DAY, "review_at": clock.t + DAY})["note"]
    iid = c.store.create_incident(type="STRU", subtype="RESD", address="100 Example Ave")
    shown = next(i for i in c.state()["incidents"] if i["id"] == iid)["premise"]
    assert [x["id"] for x in shown] == [n["id"]] and shown[0]["review_due"] is False
    clock.t += 1.5 * DAY
    c.store.note(iid, "tick")                                   # any change: the state is built again
    assert next(i for i in c.state()["incidents"] if i["id"] == iid)["premise"][0]["review_due"] is True
    clock.t += DAY
    c.store.note(iid, "tick")
    assert next(i for i in c.state()["incidents"] if i["id"] == iid)["premise"] == []
    c.premise.edit(n["id"], {"expires_at": None})
    assert len(c.addrs.here(iid)["notes"]) == 1
    c.premise.edit(n["id"], {"action": "expire"})
    assert c.addrs.here(iid)["notes"] == []
    c.premise.edit(n["id"], {"action": "restore"})
    assert len(c.addrs.here(iid)["notes"]) == 1


def test_rank_is_deterministic():
    notes = [{"id": i, "priority": p, "category": cat, "updated_at": u} for i, p, cat, u in (
        (1, 2, "other", 10), (2, 2, "hazard", 5), (3, 1, "other", 1), (4, 2, "hazard", 9), (5, 2, "hazard", 9))]
    assert [n["id"] for n in rank(notes, "STRU")] == [3, 5, 4, 2, 1]
    assert [n["id"] for n in rank(notes, "UNKNOWN-TYPE")] == [3, 5, 4, 2, 1]
    assert [n["id"] for n in rank(list(reversed(notes)), "STRU")] == [3, 5, 4, 2, 1]


def test_note_validation(hist):
    c = hist
    from sizeup.cad.store import CadError
    for body, msg in (({"where": "100 Example Ave"}, "text"), ({"where": "100 Example Ave", "text": "x", "category": "pets"},
                      "category"), ({"where": "100 Example Ave", "text": "x", "priority": 7}, "priority"),
                      ({"where": "100 Example Ave", "text": "x" * 501}, "500"), ({"where": "", "text": "x"}, "where"),
                      ({"where": "100 Example Ave", "text": "x", "review_at": "soon"}, "review_at")):
        with pytest.raises(CadError, match=msg):
            c.premise.create(body)
    iid = c.store.create_incident(type="SERVICE", address="")
    with pytest.raises(CadError, match="location"):
        c.premise.create({"incident_id": iid, "text": "x"})


# -- review fixes (#421) -------------------------------------------------------------------------------------------------

def test_ranges_and_lettered_numbers_in_history(hist):
    c, clock = hist, hist.clock_
    base = 41.01

    def pt(n, dlat=0.0, dlon=0.0):                                       # where Sample St's numbers are
        return {"lat": base + n * 0.00009 + dlat, "lon": -73.67 + dlon}
    rng = call_at(c, clock, 9, "10-12 Sample St", **pt(10))
    ten = call_at(c, clock, 8, "10 Sample St", **pt(10))
    twelve = call_at(c, clock, 7, "12 Sample St", **pt(12))
    eleven = call_at(c, clock, 6, "11 Sample St", **pt(11))
    near_a = call_at(c, clock, 5, "12A Sample St", **pt(12, 0.00005))        # ~5 m from 12's pin
    far_b = call_at(c, clock, 4, "12B Sample St", **pt(12, 0.0002))          # ~22 m
    half = call_at(c, clock, 3, "12 1/2 Sample St", **pt(12, 0.0002, 0.005))
    now = c.store.create_incident(type="ALARM", subtype="COMM", address="12 Sample St", **pt(12))
    ids = {r["id"] for r in c.addrs.here(now)["recent"]} | {x["id"] for x in c.addrs.here(now)["false_alarms"]["calls"]}
    inc = c.store.incident(now)
    ids = {r["id"] for r in c.addrs.building(inc["bldg_key"], inc["lat"], inc["lon"], inc["addr_street"])}
    assert {rng, twelve, near_a} <= ids and not {ten, eleven, far_b, half} & ids
    r = c.store.incident(rng)
    ids = {x["id"] for x in c.addrs.building(r["bldg_key"], r["lat"], r["lon"], r["addr_street"])}
    assert {ten, twelve} <= ids and eleven not in ids                    # a range: each of its numbers


def test_the_address_box_matches_keys_not_substrings(calls):
    c, ids = calls
    for n in (112, 1100):                                              # "100 Example Ave" is in "1100 Example Ave"
        call_at(c, c.clock_, 2, f"{n} Example Ave")
    assert {r["id"] for r in search(c, where="100 Example Ave")["rows"]} == {ids["alarm"], ids["ems"]}
    assert search(c, where="12 Example Ave")["rows"] == []               # not 112
    sql, args = c.addrs._where_sql("100 Example Ave")
    plan = " ".join(r["detail"] for r in c.store.q(f"EXPLAIN QUERY PLAN SELECT id FROM incidents WHERE {sql}", args))
    assert "SCAN incidents" not in plan                                  # on the keys' indexes
    call_at(c, c.clock_, 1, "100-102 Example Ave")
    assert search(c, where="102 Example Ave")["total"] == 1             # a range: either number finds it
    place = call_at(c, c.clock_, 1, "@ Zqxv School")
    assert [r["id"] for r in search(c, where="zqxv school")["rows"]] == [place]   # no key: the words, whole
    assert search(c, where="Zqxv Scho")["total"] == 0


def test_free_text_counts_every_hit_newest_first_and_snippets_only_the_page(hist):
    c, clock = hist, hist.clock_
    made = [call_at(c, clock, 60 - i, f"{100 + i} Example Ave", remarks=f"smoke detector {i}") for i in range(60)]
    out = search(c, text="smoke", size=10)
    assert out["total"] == 60 and [r["id"] for r in out["rows"]] == made[::-1][:10]
    assert all("[smoke]" in r["hit"] for r in out["rows"])
    seen = []
    c.store.db.set_trace_callback(seen.append)
    search(c, text="smoke", size=10, page=1)                            # the hits reused; snippets for 10 rows
    c.store.db.set_trace_callback(None)
    assert sum("snippet(" in s for s in seen) <= 10
    assert not any("MATCH" in s and "snippet(" not in s for s in seen)  # the match itself: from the cache


def test_search_reads_off_the_store_lock(tmp_path):
    import threading
    c = make_cad(tmp_path, geocoder=fake_geocode)
    c.store.create_incident(type="ALARM", subtype="COMM", address="100 Example Ave", remarks="smoke")
    wait_idle(c.addrs)
    out = []
    with c.store.lock:                                                   # the console mid-write
        t = threading.Thread(target=lambda: out.append(search(c, text="smoke")))
        t.start()
        t.join(5)
    assert out and out[0]["total"] == 1


def test_key_open_reads_nothing_with_an_empty_queue(hist):
    c = hist
    c.store.create_incident(type="ALARM", subtype="COMM", address="100 Example Ave")
    c.state()
    seen = []
    c.store.db.set_trace_callback(seen.append)
    assert c.addrs.key_open() == 0
    c.store.db.set_trace_callback(None)
    assert seen and all("addr_queue" in s and "incidents" not in s for s in seen)


def test_the_point_cache_follows_nearby_changes(hist):
    c, clock = hist, hist.clock_
    lat, lon = 41.0 + 100 * 0.00009, -73.66
    keyed = call_at(c, clock, 3, "100 Example Ave", disposition="", lat=lat, lon=lon)
    geo = c.store.create_incident(type="ALARM", subtype="COMM", address="Zqxv Blvd", lat=lat + 0.00005, lon=lon)
    assert c.addrs.here(geo)["recent"][0]["disposition"] == ""
    c.store.close(keyed, "FALSE")                                        # a keyed call nearby changes
    assert c.addrs.here(geo)["recent"][0]["disposition"] == "FALSE"


def test_search_audit_keeps_filters_out_of_the_server_log(api):
    logged = []
    api.cad.activity.log = logged.append
    get(api, "/api/cad/search", "", VAL)                                # the tab opened: no filter, no row
    assert api.cad.activity.rows(kind="search") == []
    get(api, "/api/cad/search", "where=100+Example+Ave&text=toast", VAL)
    assert len(api.cad.activity.rows(kind="search")) == 1
    line = next(m for m in logged if "Searched" in m)
    assert "Example" not in line and "toast" not in line and "Searched: 1 result" in line


def test_a_typed_note_address_must_be_sure(hist):
    c = hist
    from sizeup.cad.store import CadError
    with pytest.raises(CadError, match="map knows"):
        c.premise.create({"where": "100 Zqxv Blvd", "text": "Dog"})
    lat, lon = 41.005, -73.661
    n = c.premise.create({"where": "100 Zqxv Blvd", "lat": lat, "lon": lon, "text": "Dog", "category": "animals"})["note"]
    geo = c.store.create_incident(type="ALARM", subtype="COMM", address="Zqxv Blvd / Sample St", lat=lat + 0.0001, lon=lon)
    assert [x["id"] for x in next(i for i in c.state()["incidents"] if i["id"] == geo)["premise"]] == [n["id"]]
    k = c.premise.create({"where": "100 Example Ave", "text": "Knox box", "category": "access"})["note"]
    pin = c.store.create_incident(type="BLS", address="Zqxv Blvd", lat=41.0 + 100 * 0.00009 + 0.00005, lon=-73.66)
    assert k["id"] in [x["id"] for x in next(i for i in c.state()["incidents"] if i["id"] == pin)["premise"]]


def test_category_visibility_is_configurable(api, monkeypatch):
    from sizeup.cad import premise
    c, iid = api.cad, api.calls["ems"]
    c.premise.create({"incident_id": iid, "text": "On home oxygen", "category": "medical"})
    c.premise.create({"incident_id": iid, "text": "Knox box", "category": "access"})
    c.store.reopen(iid)
    monkeypatch.setattr(premise, "VISIBLE_TO", {"medical": "dispatcher"})
    pushed = next(i for i in c.state()["incidents"] if i["id"] == iid)["premise"]
    assert [n["category"] for n in pushed] == ["access"]                 # the push is everyone's
    assert [n["category"] for n in get(api, f"/api/cad/incident/{iid}/here", "", VAL)[1]["notes"]] == ["access"]
    here = get(api, f"/api/cad/incident/{iid}/here", "", JULIAN)[1]["notes"]
    assert sorted(n["category"] for n in here) == ["access", "medical"]
    assert [n["category"] for n in get(api, "/api/cad/premise", "", VAL)[1]["notes"]] == ["access"]


def test_migration_messages_are_logged(tmp_path):
    from sizeup.cad.service import Cad, Settings
    logged = []
    c = make_cad(tmp_path, geocoder=fake_geocode)
    c.shutdown()
    db = sqlite3.connect(tmp_path / "cad.sqlite")
    db.execute("DROP TRIGGER here_rev_inc2")
    db.execute("DROP TRIGGER here_rev_asg2")
    db.execute("DROP INDEX incidents_bldg")
    db.execute("DROP INDEX incidents_street")
    for col in ADDR_COLUMNS:
        db.execute(f"ALTER TABLE incidents DROP COLUMN {col}")
    db.commit()
    db.close()
    Cad(Settings(db=str(tmp_path / "cad.sqlite")), geocoder=fake_geocode, log=logged.append)
    assert any("address keys" in m for m in logged)


def test_a_broken_gazetteer_never_breaks_the_push():
    def boom(q):
        raise RuntimeError("gazetteer down")
    c = make_cad(geocoder=boom)
    iid = c.store.create_incident(type="ALARM", subtype="COMM", address="100 Example Ave")
    assert next(i for i in c.state()["incidents"] if i["id"] == iid)["addr_how"] == "text"


def test_csv_cells_never_run_as_formulas():
    from sizeup.cad.export import _csv
    out = _csv(["a", "b", "c"], [["=1+1", "-73.66", "@SUM(A1)"]]).decode("utf-8-sig").splitlines()[1]
    assert out == "'=1+1,-73.66,'@SUM(A1)"


# -- re-review fixes (#421) ----------------------------------------------------------------------------------------------

def notes_on(c, iid):
    return next(i for i in c.state()["incidents"] if i["id"] == iid)["premise"]


def test_notes_never_spill_onto_a_neighbouring_building(hist):
    c = hist
    n = c.premise.create({"where": "100 Example Ave", "text": "On home oxygen", "category": "medical"})["note"]
    here = c.store.create_incident(type="BLS", address="100 Example Ave Apt 3")
    for num in (101, 102):                                             # 10 m and 20 m away, each its own address
        assert notes_on(c, c.store.create_incident(type="BLS", address=f"{num} Example Ave")) == []
    (same,) = notes_on(c, here)
    assert same["id"] == n["id"] and same["nearby"] is False
    pin = c.store.create_incident(type="BLS", address="Zqxv Blvd / Sample St", lat=41.0 + 100 * 0.00009 + 0.0001, lon=-73.66)
    (near,) = notes_on(c, pin)                                         # a call known only by its point: shown, nearby
    assert near["nearby"] is True and near["label"] == "100 Example Ave, Port Chester"


def test_a_request_catch_up_is_bounded_and_keys_outside_the_lock(hist):
    import threading
    c = hist
    free = []

    def geocode(q):                                                    # the gazetteer: is the store free meanwhile?
        t = threading.Thread(target=lambda: free.append(c.store.lock.acquire(timeout=0.5) and (c.store.lock.release() or True)))
        t.start()
        t.join()
        return fake_geocode(q)
    c.geocoder = geocode
    sizes = []
    orig = c.addrs._index_radio
    c.addrs._index_radio = lambda limit=300: (sizes.append(limit), orig(limit))[1]
    for i in range(40):
        c.record_traffic({"id": f"t{i}", "start": 1.0 + i, "end": 2.0 + i, "channel": "FG1", "text": f"engine {i}"})
    c.store.create_incident(type="ALARM", subtype="COMM", address="100 Example Ave")
    c.addrs._bounded_catch_up(transcripts=True)
    assert sizes and max(sizes) <= 25 and sum(sizes) <= 25 + 25
    assert free and all(free)


def test_note_visibility_is_an_audited_admin_setting(api):
    c, iid = api.cad, api.calls["ems"]
    med = c.premise.create({"incident_id": iid, "text": "On home oxygen", "category": "medical"})["note"]
    code, out = call(api, "/api/cad/settings/premise_visibility", {"action": "set", "key": "medical", "value": "admin"}, JULIAN)
    assert code == 403                                                 # admins only
    code, out = call(api, "/api/cad/settings/premise_visibility", {"action": "set", "key": "medical", "value": "admin"}, COREY)
    assert code == 200 and out["premise_visibility"]["medical"]["value"] == "admin"
    assert call(api, "/api/cad/settings/premise_visibility", {"action": "set", "key": "medical", "value": "x"}, COREY)[0] == 400
    (a,) = c.store.q("SELECT * FROM settings_audit WHERE key = 'premise_visibility/medical'")
    assert (a["section"], a["old"], a["new"]) == ("system", "everyone", "admin")
    assert '"premise_visibility": {"medical": "admin"}' in json.dumps(c.settings_export())      # in the settings file
    # a dispatcher may no longer see, edit or acknowledge it, and gets no text back
    code, out = call(api, f"/api/cad/premise/{med['id']}", {"text": "changed"}, JULIAN)
    assert code == 400 and "no note" in out["error"]
    code, out = call(api, f"/api/cad/premise/{med['id']}/ack", {"incident_id": iid}, JULIAN)
    assert code == 400 and "no note" in out["error"]
    code, out = call(api, f"/api/cad/premise/{med['id']}", {"priority": 1}, COREY)
    assert code == 200 and out["note"]["text"] == "On home oxygen"
    last = c.store.q("SELECT * FROM settings_audit WHERE section = 'premise' ORDER BY id DESC LIMIT 1")[0]
    assert last["key"] == str(med["id"]) and "oxygen" not in (last["old"] + last["new"]) and "Example" not in last["key"]
    assert json.loads(last["new"]) == {"category": "medical", "changed": ["priority"]}
    code, out = call(api, "/api/cad/settings/premise_visibility", {"action": "reset", "key": "medical"}, COREY)
    assert out["premise_visibility"]["medical"]["value"] == "everyone"
    assert [n["id"] for n in get(api, f"/api/cad/incident/{iid}/here", "", VAL)[1]["notes"]] == [med["id"]]


def test_the_settings_audit_hides_older_rows_of_a_newly_restricted_category(api):
    c, iid = api.cad, api.calls["ems"]
    med = c.premise.create({"incident_id": iid, "text": "On home oxygen", "category": "medical"})["note"]
    call(api, f"/api/cad/premise/{med['id']}", {"text": "On home oxygen, 2nd floor"}, JULIAN)    # written in the clear
    assert call(api, "/api/cad/settings/premise_visibility", {"action": "set", "key": "medical", "value": "admin"},
                COREY)[0] == 200

    def rows(email):
        code, out = get(api, "/api/cad/settings/audit", "section=premise", email)
        assert code == 200
        return [r for r in out["audit"] if r["section"] == "premise"]
    for who_ in (JULIAN, VAL):
        seen = rows(who_)
        assert len(seen) == 2 and not any("oxygen" in (r["old"] or "") + (r["new"] or "") + r["key"] for r in seen)
        assert {r["key"] for r in seen} == {str(med["id"])}
        assert json.loads(seen[0]["new"]) == {"category": "medical", "changed": ["text"]}
    assert any("oxygen" in (r["new"] or "") for r in rows(COREY))       # an admin still reads it all
    assert any("oxygen" in (r["new"] or "") for r in c.store.q("SELECT new FROM settings_audit WHERE section = 'premise'"))


def test_snippets_bind_the_page_rowids(calls):
    c, ids = calls
    seen = []
    c.store.db.set_trace_callback(seen.append)
    out = search(c, text="oxygen")
    c.store.db.set_trace_callback(None)
    assert out["rows"][0]["hit"].count("[oxygen]") == 1
    assert not any("MATCH" in s and "IN (SELECT" in s for s in seen)


def test_the_backfill_rests_at_least_twice_its_cpu():
    from sizeup.cad.history import PACE_S, pace
    assert pace(0.01) == PACE_S and pace(1.0) == 2.0


def test_cold_history_reads_only_its_house_number(hist):
    c, clock = hist, hist.clock_
    for n in range(1, 40):
        call_at(c, clock, 3, f"{n} Sample St")
    now = c.store.create_incident(type="ALARM", subtype="COMM", address="12 Sample St")
    c.addrs.key_one(now)
    inc = c.store.incident(now)
    seen = []
    c.store.db.set_trace_callback(seen.append)
    c.addrs._same_building(inc["bldg_key"], inc["addr_street"], None)
    c.store.db.set_trace_callback(None)
    (sql,) = [s for s in seen if "bldg_key GLOB" in s]
    plan = " ".join(r["detail"] for r in c.store.q("EXPLAIN QUERY PLAN " + sql))
    assert "SCAN incidents" not in plan


def test_single_letter_streets_and_letter_numbers():
    def strict(q):                                                     # an exact gazetteer: no fuzzy street
        s = q.split(",")[0].lower()
        m = re.match(r"(\d+)\s+(.*)$", s)
        if not m or addrkey.street_key(m.group(2)) not in STREETS | {"x st": ("X St", "Port Chester", 41.2, -73.6)}:
            return []
        name, town, lat, lon = (STREETS | {"x st": ("X St", "Port Chester", 41.2, -73.6)})[addrkey.street_key(m.group(2))]
        return [{"kind": "address", "street": name, "house_number": m.group(1), "municipality": town, "confidence": 1.0,
                 "lat": lat, "lon": lon}]
    assert key("1 X ST", geocode=strict).bldg_key == "1|x st|port chester"
    assert key("Example Ave x Sample St", geocode=strict).how == ""            # "x" between two streets: a crossing
    assert key("12 B Example Ave", geocode=strict).bldg_key == "12b|example ave|port chester"   # no "B Example Ave"
    assert key("12 W Example Ave", geocode=strict).bldg_key == "12|w example ave|port chester"


def test_a_failing_gazetteer_still_saves_a_mapped_note():
    calls_ = []

    def flaky(q):
        calls_.append(q)
        if len(calls_) > 1:
            raise RuntimeError("down")
        return fake_geocode(q)
    c = make_cad(geocoder=flaky)
    n = c.premise.create({"where": "100 Example Ave", "text": "Knox box", "category": "access"})["note"]
    assert n["label"] == "100 Example Ave, Port Chester"


def test_another_p25_systems_transcript_is_never_searchable(calls):
    """#419 x #403: a context transcript (another P25 system's, linked to a call by hand) is never in the search index:
    no hit, no snippet, through a re-transcription, a second engine's text and a full reindex. Ours stays searchable."""
    c, ids = calls
    t = c.clock() - 9 * DAY
    row = {"id": "9211-p1", "start": t, "end": t + 3, "channel": "PD Dispatch", "talkgroup": 9211, "system": "mrrs",
           "source_key": "p25:mrrs:9211", "text": "suspect zebra fled northbound"}
    c.record_traffic(row)
    c.store.db.execute("INSERT INTO radio_links (traffic_id, incident_id, why) VALUES ('9211-p1', ?, 'manual')", (ids["fire"],))
    # (the same row as ours would be found: the link and the index work)
    c.store.db.execute("UPDATE radio_traffic SET system = 'westchester', text = text || '.' WHERE id = '9211-p1'")
    assert search(c, text="zebra")["total"] == 1
    c.store.db.execute("UPDATE radio_traffic SET system = 'mrrs', text = text || '.' WHERE id = '9211-p1'")
    c.addrs._hits.clear()
    assert search(c, text="zebra")["total"] == 0
    c.record_traffic(dict(row, text="suspect giraffe fled"))                         # re-transcribed
    assert search(c, text="giraffe")["total"] == 0 and search(c, text="zebra")["total"] == 0
    c.store.db.execute("UPDATE radio_traffic SET alt_text = 'okapi' WHERE id = '9211-p1'")   # a second engine's text
    assert search(c, text="okapi")["total"] == 0
    c.store.meta_put("search_fts", "0")                                               # a full reindex
    c.addrs.ensure()
    assert search(c, text="giraffe")["total"] == 0
    assert c.store.db.execute("SELECT COUNT(*) FROM fts_radio WHERE fts_radio MATCH 'giraffe OR okapi OR zebra'").fetchone()[0] == 0
    got = search(c, text='"heavy smoke"')                                             # ours, on the same call
    assert got["total"] == 1 and all("giraffe" not in (r.get("hit") or "") for r in got["rows"])
