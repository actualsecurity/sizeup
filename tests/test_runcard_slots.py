"""Run-card slots beyond a kind and a named unit (sizeup/cad/runcards.py):

  {"all": "Port Chester FD"}   the full department: every available PCFD apparatus not already on the card
                               (user, 2026-10-08: "in the run cards we need full department response to be
                               an option for PCFD units")
  {"kind": "any", "count": 1}  any apparatus, whatever kind is free ("Service calls: 1 × any")
"""

import json

import pytest

from sizeup.cad import lists, runcards
from sizeup.cad.store import CadError
from tests.test_cad import make_cad, names, server, uid, write_lists  # noqa: F401  (server: the HTTP fixture)
from tests.test_settings import post

ALL = {"all": "Port Chester FD"}
# Every PCFD apparatus "All PCFD" ticks, in card order: regulars in board order, then the spares (E59, R41).
PCFD = ["2391", "2392", "2393", "2394", "2395", "2396", "2397", "2398", "2399", "E58", "E60", "E61", "E62", "E63",
        "E64", "L31", "TL2", "R40", "U65", "U66", "U67", "U239", "M40", "M60", "E59", "R41"]
NOT_PCFD = {"2661", "2662", "E14", "L30", "U42", "DPT239", "DPT266", "E191", "L25", "242_eNG", "77A1", "PCPD", "CONED-G"}


def stru(cad, **kw):
    return cad.create(dict({"type": "STRU", "address": "209 Westchester Ave"}, **kw))["id"]


# -- validation ---------------------------------------------------------------------------------------------

def test_slots_validate():
    cad = make_cad()
    c = runcards.save(cad.store, "STRU", [{"kind": "any", "count": 2}, dict(ALL), {"unit": "2398"}])
    assert c["slots"] == [{"kind": "any", "count": 2}, ALL, {"unit": "2398"}]
    assert runcards.describe(ALL) == "full department" and runcards.describe({"kind": "any", "count": 1}) == "1 × any"
    for slots, why in (([ALL, ALL], "one full department per card"),
                       ([{"all": "Rye FD"}], "slots.0..all: Port Chester FD"),
                       ([{"all": ""}], "slots.0..all"),
                       ([{"kind": "any|engine", "count": 1}], "slots.0..kind: any, or one or more of"),
                       ([{"kind": "any", "count": 0}], "1 to 10")):
        with pytest.raises(CadError, match=why):
            runcards.save(cad.store, "STRU", slots)
    assert cad.lists.any_kinds == ("engine", "ladder", "tower", "rescue", "utility", "marine")   # not chief


def test_the_data_file_takes_them_and_checks_them(tmp_path):
    cards = [{"type": "STRU", "slots": [ALL, {"unit": "E14"}]}, {"type": "SERVICE", "slots": [{"kind": "any", "count": 1}]}]
    L = lists.load(write_lists(tmp_path, run_cards=cards))
    assert L.run_cards[0]["slots"][0] == ALL and L.public()["home_department"] == "Port Chester FD"
    with pytest.raises(lists.ListsError, match="one full department per card"):
        lists.load(write_lists(tmp_path, run_cards=[{"type": "STRU", "slots": [ALL, ALL]}]))
    with pytest.raises(lists.ListsError, match="the only full department"):
        lists.load(write_lists(tmp_path, run_cards=[{"type": "STRU", "slots": [{"all": "Rye Brook"}]}]))
    d = json.loads(lists.DEFAULT_PATH.read_text())
    with pytest.raises(lists.ListsError, match="'any' is taken"):
        lists.load(write_lists(tmp_path, unit_kinds=d["unit_kinds"] + [{"id": "any", "label": "Any"}]))
    cad = make_cad(lists=str(write_lists(tmp_path, run_cards=cards)))     # seeded into the database as they are
    assert [c["slots"] for c in runcards.cards(cad.store)] == [c["slots"] for c in cards]


# -- full department ------------------------------------------------------------------------------------------

def test_full_department_is_every_available_pcfd_apparatus_not_already_on_the_card():
    cad = make_cad()
    runcards.save(cad.store, "STRU", [{"kind": "ladder|tower", "count": 1}, dict(ALL), {"unit": "2398"}, {"unit": "E14"}])
    iid = stru(cad)
    r = runcards.recommend(cad.store, "STRU", "", iid)
    # the ladder slot took L31 first; 2398 and E14 are left for their own slots; no Rye Brook, mutual aid,
    # EMS, PD, agency or code-only unit
    assert names(r["units"]) == ["L31"] + [n for n in PCFD if n not in ("L31", "2398")] + ["2398", "E14"]
    assert not NOT_PCFD - {"E14"} & set(names(r["units"]))
    assert [u["slot"] for u in r["units"]][:3] == ["1 × ladder/tower", "full department", "full department"]
    assert r["full"] == "Port Chester FD" and r["unfilled"] == []
    # out of service, committed elsewhere or already on this event: not recommended again
    other = cad.create({"type": "SERVICE"})["id"]
    cad.unit_status(uid(cad, "E62"), {"status": "OS"})
    cad.unit_status(uid(cad, "L31"), {"status": "OS"})
    cad.store.assign(other, [uid(cad, "E61")])
    cad.store.assign(iid, [uid(cad, "E60"), uid(cad, "R40")])
    r = runcards.recommend(cad.store, "STRU", "", iid)
    assert names(r["units"]) == ["TL2"] + [n for n in PCFD if n not in ("TL2", "2398", "E60", "E61", "E62", "L31", "R40")] \
        + ["2398", "E14"]
    assert r["already"] == ["E60", "R40"] and r["unfilled"] == []
    # nobody left: said, as for a kind
    for n in PCFD:
        if n not in ("E60", "R40", "E61"):
            cad.unit_status(uid(cad, n), {"status": "OS"})
    r = runcards.recommend(cad.store, "STRU", "", stru(cad))
    assert names(r["units"]) == ["L30", "E14"]          # Rye Brook's ladder by kind once PCFD's are out (as always)
    assert r["unfilled"] == ["no Port Chester FD apparatus available", "2398 not available (out of service)"]


def test_a_kind_after_the_full_department_gets_what_is_left():
    cad = make_cad()
    runcards.save(cad.store, "STRU", [dict(ALL), {"kind": "engine", "count": 1}])
    r = runcards.recommend(cad.store, "STRU", "", stru(cad))
    assert names(r["units"]) == PCFD + ["E14"]          # Rye Brook's engine, by kind after PCFD's (as always)


def test_full_department_keeps_the_nearest_first_setting():
    from tests.test_proximity import WP, call, make
    c = make()
    c.save_run_card({"type": "STRU", "slots": [dict(ALL)]})
    near = names(c.recommend(call(c, {"lat": WP["ll"][1], "lon": WP["ll"][0]}), {})["units"])
    assert near[0] == "E64" and sorted(near) == sorted(PCFD) and near != PCFD     # Washington Park's engine first
    post(c, "dispatch", action="set", key="order", value="card")
    assert names(c.recommend(call(c, {"lat": WP["ll"][1], "lon": WP["ll"][0]}), {})["units"]) == PCFD


def test_full_department_dispatch_says_full_department_response():
    cad = make_cad()
    cad.save_run_card({"type": "STRU", "slots": [dict(ALL)]})
    iid = stru(cad)
    r = cad.recommend(iid, {})
    t = cad.dispatch(iid, {"unit_ids": [u["id"] for u in r["units"]], "operator": "JD", "full": True})["text"]
    assert t.startswith("Port Chester Fire, full department response, respond to 209 Westchester Avenue")
    assert "Engine" not in t and "23 9" not in t


# -- any apparatus ----------------------------------------------------------------------------------------------

def test_any_takes_whatever_apparatus_is_free_never_a_chief():
    cad = make_cad()
    cad.save_run_card({"type": "SERVICE", "slots": [{"kind": "any", "count": 2}]})
    r = runcards.recommend(cad.store, "SERVICE")
    assert names(r["units"]) == ["E58", "E60"] and [u["slot"] for u in r["units"]] == ["2 × any", "2 × any"]
    assert r["full"] == ""
    # skips what an earlier slot took and units named on the card
    cad.save_run_card({"type": "SERVICE", "slots": [{"kind": "engine", "count": 1}, {"kind": "any", "count": 1}, {"unit": "E60"}]})
    assert names(runcards.recommend(cad.store, "SERVICE")["units"]) == ["E58", "E61", "E60"]
    # no engine free: the next apparatus of another kind (the board's next, regulars before spares)
    for n in ("E58", "E60", "E61", "E62", "E63", "E64"):
        cad.unit_status(uid(cad, n), {"status": "OS"})
    cad.save_run_card({"type": "SERVICE", "slots": [{"kind": "any", "count": 1}]})
    assert names(runcards.recommend(cad.store, "SERVICE")["units"]) == ["L31"]
    cad.save_run_card({"type": "SERVICE", "slots": [{"kind": "any", "count": 10}]})
    got = names(runcards.recommend(cad.store, "SERVICE")["units"])
    assert got == ["L31", "TL2", "R40", "U65", "U66", "U67", "U239", "M40", "M60", "E59"]
    # all gone: said as apparatus, never "any"
    iid = cad.create({"type": "SERVICE"})["id"]
    cad.store.assign(iid, [uid(cad, n) for n in PCFD if n not in ("E58", "E60", "E61", "E62", "E63", "E64")]
                     + [uid(cad, n) for n in ("E14", "L30", "U42")])
    cad.save_run_card({"type": "SERVICE", "slots": [{"kind": "any", "count": 1}]})
    r = runcards.recommend(cad.store, "SERVICE")
    assert r["units"] == [] and r["unfilled"] == ["no apparatus available"]


def test_any_is_said_as_the_unit_it_picked():
    cad = make_cad()
    cad.save_run_card({"type": "SERVICE", "slots": [{"kind": "any", "count": 1}]})
    iid = cad.create({"type": "SERVICE", "address": "209 Westchester Ave"})["id"]
    r = cad.recommend(iid, {})
    t = cad.dispatch(iid, {"unit_ids": [u["id"] for u in r["units"]], "operator": "JD"})["text"]
    assert t.startswith("Port Chester Fire, Engine 58, respond to") and "any" not in t.lower()


# -- the API and settings export / import ------------------------------------------------------------------------

def test_api_saves_and_lists_them(server):  # noqa: F811
    code, c = server.j("POST", "/api/cad/runcards", {"type": "STRU", "operator": "JD",
                                                     "slots": [{"kind": "any", "count": 1}, dict(ALL), {"unit": "2398"}]})
    assert code == 200 and c["slots"] == [{"kind": "any", "count": 1}, ALL, {"unit": "2398"}]
    code, d = server.j("GET", "/api/cad/runcards")
    assert next(x for x in d["run_cards"] if x["type"] == "STRU")["slots"][1] == ALL
    code, e = server.j("POST", "/api/cad/runcards", {"type": "STRU", "operator": "JD", "slots": [dict(ALL), dict(ALL)]})
    assert code == 400 and "one full department" in e["error"]


def test_settings_export_import_round_trips_them(tmp_path):
    a = make_cad(tmp_path / "a")
    a.save_run_card({"type": "STRU", "slots": [{"kind": "engine", "count": 2}, dict(ALL), {"unit": "E14"}]})
    a.save_run_card({"type": "SERVICE", "slots": [{"kind": "any", "count": 1}]})
    data = json.loads(json.dumps(a.settings_export()))
    cards = {c["type"]: c["slots"] for c in data["run_cards"]}
    assert cards["STRU"][1] == ALL and cards["SERVICE"] == [{"kind": "any", "count": 1}]
    b = make_cad(tmp_path / "b")
    dry = post(b, "import", action="dry_run", settings=data)
    assert {(c["key"], c["change"]) for c in dry["changes"] if c["section"] == "run cards"} == {("STRU", "changed"), ("SERVICE", "changed")}
    post(b, "import", action="apply", settings=data)
    assert b.settings_export()["run_cards"] == data["run_cards"]
    assert post(b, "import", action="dry_run", settings=data)["changes"] == []
    assert runcards.recommend(b.store, "STRU")["full"] == "Port Chester FD"
    for bad, why in (([dict(ALL), dict(ALL)], "one full department per card"), ([{"all": "Rye FD"}], "all: Port Chester FD"),
                     ([{"kind": "any|engine", "count": 1}], "a fire apparatus kind")):
        d = json.loads(json.dumps(data))
        d["run_cards"] = [{"type": "STRU", "slots": bad}]
        with pytest.raises(CadError, match=why):
            post(b, "import", action="dry_run", settings=d)
