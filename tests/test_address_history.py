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
           "sample st": ("Sample St", "Port Chester", 41.01, -73.67),
           "specimen rd": ("Specimen Rd", "Rye Brook", 41.02, -73.68)}
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
    ("100-104 EXAMPLE AVE", "100|example ave|port chester", "100|example ave|port chester", "gazetteer"),       # a range
    ("100A Example Ave", "100|example ave|port chester", "100|example ave|port chester", "gazetteer"),          # 100A: the lot
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


def test_parse_and_street_text():
    p = addrkey.parse("12-14 EXAMPLE AVE REAR, RYE_BROOK", ALIASES)
    assert (p.number, p.street, p.unit, p.town) == ("12", "EXAMPLE AVE", "rear", "Rye Brook")
    assert addrkey.street_text("575 Example Rd @ Sample Market") == "575 Example Rd"
    assert addrkey.street_text("@ Sample School") == ""
    assert addrkey.parse("Example Ave", ALIASES) is None


# -- the migration: keys, indexes, triggers, backfill ------------------------------------------------------------------

def test_an_older_database_gets_the_columns_and_is_backfilled(tmp_path):
    c = make_cad(tmp_path, geocoder=fake_geocode)
    ids = [c.store.create_incident(type="ALARM", subtype="COMM", address=f"{n} Example Ave") for n in (100, 100, 102)]
    c.store.db.close()
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
    mk({"where": "102 Example Ave", "text": "Dog", "category": "animals"})                    # next door
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
