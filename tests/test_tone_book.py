"""The tone book (#122, over #115's Settings > Radios > Tones): agencies as places + a service with several pairs, each
with a role; read from the county list without rewriting it (merged, aliases kept, assumptions reported), conflicts,
matching with roles, ours on the entity, move / split / merge, export / import of old and new overrides, the station."""

import json
import time
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from sizeup.app import App
from sizeup.cad import tonelist
from sizeup.cad.api import CadApi
from sizeup.cad.store import CadError
from sizeup.cloud.access import Identity
from sizeup.config import load
from sizeup.radio.fm import RATE, Channel, Segment
from sizeup.radio.tones import ToneCode, base_name
from tests.test_app import FakeWhisper, cfg, tone, voice, wait
from tests.test_cad import make_cad
from tests.test_roles import COREY, JULIAN, VAL, call, people
from tests.test_tone_settings import page_row

ROOT = Path(__file__).resolve().parent.parent
LIST = load(ROOT / "config.example.toml").tones          # data/westchester_tones.csv
LOCKED = {"Port Chester FD", "Rye Brook FD"}


def code(name, a, b, source="rr-wiki"):
    return ToneCode(name, a, b, source=source, fetched="2026-10-13")


# The cases from the issue: Archville and Armonk twice, "Chappaqua FD" / "Chappaqua Fire", one pair as Bedford EMS and
# Bedford Fire; PCVAC (several places), a name with the service in it, "Peekskill Volunteer", a pair two departments list.
FIXTURE = [code("Archville", 951.4, 1430.7), code("Archville (est.)", 2855.0, 1430.0, "rr-wiki-est-2015"),
           code("Armonk (est.)", 2856.0, 643.0, "rr-wiki-est"), code("Armonk (est.)", 2855.0, 537.0, "rr-wiki-est-2015"),
           code("Chappaqua FD", 949.5, 1779.6), code("Chappaqua Fire (est.)", 2844.0, 1779.0, "rr-wiki-est"),
           code("Chappaqua VAC", 747.3, 408.2), code("Bedford", 581.4, 796.7),
           code("Bedford Fire (est.)", 580.0, 2382.0, "rr-wiki-est"), code("Bedford EMS (est.)", 580.0, 2382.0, "rr-wiki-est"),
           code("PCVAC", 810.2, 1285.8), code("Mamaroneck Village FD", 1005.5, 209.0),
           code("Mamaroneck Village EMS", 1091.4, 435.1), code("Peekskill", 435.1, 435.1), code("Peekskill Volunteer", 457.1, 435.1),
           code("Larchmont FD", 435.1, 435.1), code("Scarsdale EMS (heard)", 928.1, 810.2, "air"),
           code("Port Chester FD", 349.1, 311.1), ToneCode("Port Chester FD", 349.1, 311.1, "PCFD", source="config")]


def app_with(tmp_path, tones=LIST, **kw):
    return App(replace(cfg(tmp_path), tones=tuple(tones), **kw))


def tset(app, op="Dana", **body):
    return app.cad.settings.post("tones", dict(body, action=body.pop("action", "set"), operator=op))


def ent(app, name):
    return next(e for e in app.cad.settings.tones_page()["entities"] if e["name"] == name)


def match(app, a, b):
    return app.plan.match_pair(a, b)


# -- reading the list ------------------------------------------------------------------------------------------------


def test_names_read_as_places_and_a_service():
    p = tonelist.parse_name
    assert (p("Mamaroneck Village FD").places, p("Mamaroneck Village FD").service) == (("Mamaroneck Village",), "fire")
    assert (p("Bedford EMS").places, p("Bedford EMS").service, p("Bedford EMS").sure) == (("Bedford",), "ems", True)
    assert p("PCVAC").places == ("Port Chester", "Rye", "Rye Brook") and p("PCVAC").service == "ems"
    assert p("Port Chester-Rye-Rye Brook EMS").places == ("Port Chester", "Rye", "Rye Brook")
    assert p("Larchmont-Mamaroneck VAC").places == ("Larchmont", "Mamaroneck Town") and not p("Larchmont-Mamaroneck VAC").sure
    assert p("Greenburgh Police EMS").places == ("Greenburgh",) and p("Greenburgh Police EMS").service == "ems"
    assert p("Westchester County Police").service == "police" and p("Westchester County Police").places == ("Westchester County",)
    assert p("Croton FD").places == ("Croton-on-Hudson",) and p("Briarcliff").places == ("Briarcliff Manor",)
    vol = p("Peekskill Volunteer")
    assert (vol.places, vol.service, vol.role, vol.sure) == (("Peekskill",), "fire", "Volunteer", False)
    bare = p("Archville")
    assert bare.service == "fire" and not bare.sure and "Fire assumed" in bare.why
    assert p("Port Chester · Rye · EMS · Chiefs") == tonelist.Parsed(("Port Chester", "Rye"), "ems", "Chiefs")
    assert base_name("Port Chester · Fire · Chiefs (est.)") == "Port Chester · Fire"
    assert base_name("Port Chester · Rye · Rye Brook · EMS") == "Port Chester · Rye · Rye Brook · EMS"
    assert base_name("Rye Brook FD (est.)") == "Rye Brook FD"


def test_the_list_becomes_entities_with_every_pair_kept_and_a_report():
    b = tonelist.Book(FIXTURE, {}, LOCKED)
    by = {e.name: e for e in b.entities.values()}
    assert sorted(by) == ["Archville · Fire", "Armonk · Fire", "Bedford · EMS", "Bedford · Fire", "Chappaqua · EMS",
                          "Chappaqua · Fire", "Larchmont · Fire", "Mamaroneck Village · EMS", "Mamaroneck Village · Fire",
                          "Peekskill · Fire", "Port Chester · Fire", "Port Chester · Rye · Rye Brook · EMS", "Scarsdale · EMS"]
    assert len(by["Archville · Fire"].pairs) == 2 and len(by["Armonk · Fire"].pairs) == 2
    assert by["Chappaqua · Fire"].aliases == ["Chappaqua FD", "Chappaqua Fire"]
    assert [p.role for p in by["Peekskill · Fire"].pairs] == ["", "Volunteer"]
    assert by["Port Chester · Fire"].ours and by["Port Chester · Fire"].ours_locked and len(by["Port Chester · Fire"].pairs) == 1
    assert not by["Port Chester · Rye · Rye Brook · EMS"].ours                 # user: PC-Rye-Rye Brook EMS isn't ours
    assert sum(len(e.pairs) for e in by.values()) == len({tonelist.list_key(c) for c in FIXTURE})   # nothing lost
    r = b.report()
    merged = {m["entity"]: m["names"] for m in r["merged"]}
    assert merged["Chappaqua · Fire"] == ["Chappaqua FD", "Chappaqua Fire"] and merged["Bedford · Fire"] == ["Bedford", "Bedford Fire"]
    assert "Archville · Fire" in merged and "Chappaqua · EMS" not in merged
    assert "'Archville': no service word, Fire assumed" in r["unsure"]
    assert any(u.startswith("'Peekskill Volunteer'") for u in r["unsure"])
    assert r["conflicts"] == ["Bedford · EMS / Bedford · Fire: 580.0/2382.0", "Larchmont · Fire / Peekskill · Fire: 435.1/435.1"]
    codes = {c.name for c in b.codes()}
    assert "Chappaqua · Fire (est.)" in codes and "Scarsdale · EMS (heard)" in codes and "Peekskill · Fire · Volunteer" in codes


def test_the_county_list_reads_without_losing_a_pair():
    b = tonelist.Book(LIST, {}, LOCKED)
    assert sum(len(e.pairs) for e in b.entities.values()) == len({tonelist.list_key(c) for c in LIST})
    places = tonelist.places(b)
    assert len([p for p in places if p.casefold() == "mamaroneck village"]) == 1 and "Rye" in places and "Rye Brook" in places
    assert all("·" not in p for p in places)


# -- conflicts, matching, ours --------------------------------------------------------------------------------------


def test_a_conflict_matches_both_until_resolved_and_the_page_says_so(tmp_path):
    app = app_with(tmp_path, FIXTURE)
    assert match(app, 580.0, 2382.0) == ("Bedford · Fire (est.)", "Bedford · EMS (est.)")
    e = ent(app, "Bedford · EMS")
    assert e["conflict"] and e["pairs"][0]["conflicts"] == ["Bedford · Fire"]
    page_row(app, "pb", time.time(), [(580.0, 2382.0)])
    row = app.cad.feed.rows(limit=5)[0]
    assert len(row["tones"][0]["names"]) == 2                                 # both, the row's tag marks the conflict
    # Resolved: the EMS listing taken out
    tset(app, action="delete", key="580.0/2382.0@Bedford EMS")
    assert match(app, 580.0, 2382.0) == ("Bedford · Fire (est.)",) and not ent(app, "Bedford · Fire")["conflict"]
    assert app.cad.store.q("SELECT tones FROM radio_traffic WHERE id = 'pb'")[0]["tones"].count("Bedford") == 1


def test_matching_gives_entity_and_role_and_ours_is_the_entitys(tmp_path):
    app = app_with(tmp_path, FIXTURE)
    tset(app, a_hz=1153.4, b_hz=669.9, places=["Port Chester"], service="fire", role="Chiefs")
    tset(app, a_hz=349.1, b_hz=311.1, list="Port Chester FD", places=["Port Chester"], service="fire", role="All call")
    assert match(app, 1153.4, 669.9) == ("Port Chester · Fire · Chiefs",)
    assert match(app, 349.1, 311.1) == ("Port Chester · Fire · All call",)
    pc = ent(app, "Port Chester · Fire")
    assert [p["role"] for p in pc["pairs"]] == ["All call", "Chiefs"] and pc["ours"]
    assert app.paging.is_ours(["Port Chester · Fire · Chiefs"]) and app.store.is_ours(["Port Chester · Fire · Chiefs (est.)"])

    class Inc:
        tone_departments = ["Port Chester · Fire · Chiefs"]
    assert app.cad._ours(Inc())                                              # the CAD queues PCFD's, whatever role
    Inc.tone_departments = ["Port Chester · Rye · Rye Brook · EMS"]
    assert not app.cad._ours(Inc()) and not app.paging.is_ours(Inc.tone_departments)
    # Ours set on another entity: its pages are ours to the monitor (not queued by the CAD: PCFD's and Rye Brook's only)
    tset(app, action="entity", key="archville|fire", ours=True, notes="mutual aid partner")
    assert app.paging.is_ours(["Archville · Fire"]) and ent(app, "Archville · Fire")["notes"] == "mutual aid partner"
    Inc.tone_departments = ["Archville · Fire"]
    assert not app.cad._ours(Inc())
    tset(app, action="entity", key="port chester|fire", ours=False)          # locked: still ours
    assert ent(app, "Port Chester · Fire")["ours"] and app.paging.is_ours(["Port Chester · Fire"])
    tset(app, action="reset", key="entity:archville|fire")
    assert not app.paging.is_ours(["Archville · Fire"])


# -- move, split, merge -----------------------------------------------------------------------------------------------


def test_move_split_and_merge(tmp_path):
    app = app_with(tmp_path, FIXTURE)
    # Move: Bedford EMS's listing of 580/2382 under Bedford · Fire (the conflict is gone: one entity, two listings)
    tset(app, action="move", key="580.0/2382.0@Bedford EMS", places=["Bedford"], service="fire")
    assert not ent(app, "Bedford · Fire")["conflict"] and match(app, 580.0, 2382.0) == ("Bedford · Fire (est.)",)
    assert "Bedford · EMS" not in {e["name"] for e in app.cad.settings.tones_page()["entities"]}
    tset(app, action="reset", key="580.0/2382.0@Bedford EMS")
    assert ent(app, "Bedford · EMS")["count"] == 1
    # Split: one of Archville's pairs into a new entity (a new place)
    tset(app, action="move", key="2855.0/1430.0@Archville", places=["Archville Station 2"], service="fire")
    assert ent(app, "Archville Station 2 · Fire")["count"] == 1 and ent(app, "Archville · Fire")["count"] == 1
    # Merge: Chappaqua · EMS into a combined entity that doesn't exist yet; its aliases follow
    tset(app, action="merge", key="chappaqua|ems", into={"places": ["Chappaqua", "New Castle"], "service": "ems"})
    m = ent(app, "Chappaqua · New Castle · EMS")
    assert m["count"] == 1 and "Chappaqua · EMS" in m["aliases"] and "Chappaqua VAC" in m["aliases"]
    assert match(app, 747.3, 408.2) == ("Chappaqua · New Castle · EMS",)
    with pytest.raises(CadError, match="another entity"):
        tset(app, action="merge", key="archville|fire", into={"places": ["Archville"], "service": "fire"})
    tset(app, action="reset", key="entity:chappaqua|ems")
    assert match(app, 747.3, 408.2) == ("Chappaqua · EMS",)
    audit = [a["key"] for a in app.cad.settings.audit_log("radios")]
    assert "tones/entity:chappaqua|ems" in audit and "tones/580.0/2382.0@Bedford EMS" in audit


def test_viewers_and_dispatchers_read_admins_edit(tmp_path):
    api = CadApi(make_cad(tmp_path))
    api.cad.log = lambda m: None
    people(api.cad)
    body = {"action": "set", "a_hz": 788.5, "b_hz": 953.7, "places": ["Harrison"], "service": "ems", "role": "Rescue"}
    for who in (VAL, JULIAN):
        for b in (body, {"action": "merge", "key": "harrison|ems", "into": {"places": ["Rye"], "service": "ems"}},
                  {"action": "entity", "key": "harrison|ems", "ours": True}):
            code_, out = call(api, "/api/cad/settings/tones", b, who)
            assert code_ == 403 and "Admins only" in out["error"]
        code_, raw, _ = api.get("/api/cad/settings", "", Identity(email=who))
        assert code_ == 200 and "entities" in json.loads(raw)["tones"]
    assert call(api, "/api/cad/settings/tones", body, COREY)[0] == 200
    assert api.cad.settings.tone_overrides()["788.5/953.7"] == {"places": ["Harrison"], "service": "ems", "a_hz": 788.5,
                                                               "b_hz": 953.7, "role": "Rescue"}


# -- export / import -----------------------------------------------------------------------------------------------


def test_export_import_old_and_new(tmp_path):
    a = make_cad(tmp_path / "a")
    # an export from before (#84 + #115): names, a type, a list edit by name
    old = a.settings_export()
    old["overrides"]["tones"] = {
        "832.5/1201.0": {"name": "Fairview", "a_hz": 832.5, "b_hz": 1201.0, "ours": False, "page": "S2-1"},
        "788.5/953.7": {"name": "Harrison EMS", "a_hz": 788.5, "b_hz": 953.7, "ours": False, "type": "ems"},
        "435.1/435.1@Larchmont FD": {"list": "Larchmont FD", "name": "Larchmont FD", "a_hz": 435.1, "b_hz": 435.1,
                                     "ours": False, "type": "fire", "confirmed": False},
        "349.1/387.0@West Harrison": {"list": "West Harrison", "a_hz": 349.1, "b_hz": 387.0, "deleted": True}}
    b = app_with(tmp_path / "b", LIST)
    assert b.cad.settings.post("import", {"action": "apply", "settings": old, "operator": "Dana"})["ok"]
    assert b.cad.settings.tone_overrides() == old["overrides"]["tones"]       # kept as they are, read as entities
    assert match(b, 788.5, 953.7) == ("Harrison · EMS",)
    assert match(b, 832.5, 1201.0) == ("Fairview · Fire (heard)",)          # #84's Fairview and the list's: one entity
    assert match(b, 435.1, 435.1)[0] == "Larchmont · Fire (est.)" and match(b, 349.1, 387.0) == ()
    # the new shapes: a role, a move, an entity with ours and notes, a merge
    tset(b, a_hz=1153.4, b_hz=669.9, places=["Port Chester"], service="fire", role="Chiefs")
    tset(b, action="move", key="1031.8/763.3@Ardsley", places=["Ardsley", "Greenburgh"], service="fire")
    tset(b, action="entity", key="archville|fire", ours=True, notes="mutual aid", aliases=["Archville FD"])
    tset(b, action="merge", key="chappaqua|ems", into={"places": ["Chappaqua"], "service": "fire"})
    exp = json.loads(json.dumps(b.cad.settings_export()))
    c = app_with(tmp_path / "c", LIST)
    dry = c.cad.settings.post("import", {"action": "dry_run", "settings": exp, "operator": "Dana"})
    assert any(x["key"] == "entity:chappaqua|ems" for x in dry["changes"])
    c.cad.settings.post("import", {"action": "apply", "settings": exp, "operator": "Dana"})
    assert c.cad.settings.tone_overrides() == b.cad.settings.tone_overrides()
    assert match(c, 1153.4, 669.9) == ("Port Chester · Fire · Chiefs",) and match(c, 1031.8, 763.3) == ("Ardsley · Greenburgh · Fire",)
    assert c.paging.is_ours(["Archville · Fire"]) and match(c, 747.3, 408.2) == ("Chappaqua · Fire",)
    for k, v in (("entity:x|fire", {"places": ["Y"], "service": "fire"}),                       # the key isn't its places
                 ("entity:x|fire", {"places": ["X"], "service": "fd"}),
                 ("entity:x|fire", {"places": ["X · Fire"], "service": "fire"}),
                 ("entity:x|fire", {"places": ["X"], "service": "fire", "merged_into": {"places": ["X"], "service": "fire"}}),
                 ("700.0/800.0", {"a_hz": 700.0, "b_hz": 800.0, "places": [], "service": "fire"}),
                 ("700.0/800.0", {"a_hz": 700.0, "b_hz": 800.0, "places": ["X"], "service": "fire", "role": "A · B"})):
        bad = json.loads(json.dumps(exp))
        bad["overrides"]["tones"] = {k: v}
        with pytest.raises(CadError):
            c.cad.settings.post("import", {"action": "dry_run", "settings": bad, "operator": "Dana"})


# -- the dialog's choices, heard but unnamed, the station ----------------------------------------------------------


def test_the_dialog_picks_places_then_a_service(tmp_path):
    app = app_with(tmp_path, LIST)
    got = app.tone_names()
    ch = {c["name"]: c for c in got["choices"]}
    pc = ch["Port Chester · Rye · Rye Brook · EMS"]
    assert pc["places"] == ["Port Chester", "Rye", "Rye Brook"] and "PCVAC" in pc["aliases"] and not pc["ours"]
    assert "Port Chester-Rye-Rye Brook EMS" in pc["aliases"]
    assert ch["Harrison · EMS"]["service"] == "ems" and ch["Rye Brook · Fire"]["ours"]   # Rye Brook: no pair yet, ours
    assert got["ours"] == ["Port Chester · Fire", "Rye Brook · Fire"]
    assert [s for s, _l in got["services"]] == ["fire", "ems", "police", "other"]
    assert len([p for p in got["places"] if p == "Mamaroneck Village"]) == 1 and "Continental Village" in got["places"]
    assert not any(p.endswith((" FD", " EMS", " VAC")) for p in got["places"])


def test_heard_but_unnamed_and_naming_as_ours_queues_its_page(tmp_path):
    app = app_with(tmp_path, LIST, unit_departments={"Engine 61": "Port Chester FD"})
    t0 = 1_790_000_000.0
    page_row(app, "p1", t0, [(741.3, 623.7), (928.1, 810.2)])
    page_row(app, "p2", t0 + 600, [(741.6, 623.4)])
    d = app.cad.settings.tones_page()
    assert [(u["a_hz"], u["count"], u["page"]) for u in d["unnamed"]] == [(741.6, 2, "p2")]
    sc = ent(app, "Scarsdale · EMS")
    assert sc["heard"] == 1 and sc["last"] == t0 and sc["pairs"][0]["heard"] == 1
    tset(app, a_hz=741.6, b_hz=623.4, places=["New Rochelle"], service="fire", role="Station 2", page="p2")
    assert app.cad.settings.tones_page()["unnamed"] == []
    tones = json.loads(app.cad.store.q("SELECT tones FROM radio_traffic WHERE id = 'p1'")[0]["tones"])
    assert tones[0]["names"] == ["New Rochelle · Fire · Station 2"]
    # From the air: a Rye Brook pair, named as Rye Brook · Fire, queues its page
    app._kind = {"South 2": "paging"}
    app.stt.client = FakeWhisper({6: "10 Charlie 2, Engine 61, 209 Westchester Avenue, Pearl Street and Gilbert Place"})
    app.stt.start()
    audio = np.concatenate([tone(669.9, 1.0), tone(1092.4, 3.0), np.zeros(int(0.2 * RATE)), voice(6.0)]).astype(np.float32)
    now = time.time()
    app._segment(Channel(470.2, "South 2"), Segment(now, now + len(audio) / RATE, audio, 25.0))
    pid = app.state()["pages"][0]["id"]
    assert wait(lambda: any(r["id"] == pid and r["text"] for r in app.cad.feed.rows(limit=20)))
    un = app.cad.settings.tones_page()["unnamed"]
    assert un[0]["page"] == pid and app.cad.store.incidents() == []
    tset(app, a_hz=un[0]["a_hz"], b_hz=un[0]["b_hz"], places=["Rye Brook"], service="fire", ours=True, page=pid)
    ev = app.cad.store.incidents()
    assert len(ev) == 1 and ev[0]["status"] == "queued"
    assert app.state()["pages"][0]["departments"] == ["Rye Brook · Fire"]
    assert "entity:rye brook|fire" not in app.cad.settings.tone_overrides()     # locked: nothing to keep


def test_the_station_takes_the_book_with_the_channel_push(tmp_path):
    hub = app_with(tmp_path / "hub", LIST)
    tset(hub, a_hz=1153.4, b_hz=669.9, places=["Port Chester"], service="fire", role="Chiefs")
    tset(hub, action="move", key="580.0/2382.0@Bedford EMS", places=["Bedford"], service="fire")
    tset(hub, action="merge", key="chappaqua|ems", into={"places": ["Chappaqua", "New Castle"], "service": "ems"})
    tset(hub, action="entity", key="archville|fire", ours=True)
    sent = dict(hub._tone_ov)
    st = app_with(tmp_path / "st", LIST)
    res = st.set_channel_prefs({}, {"ok": False, "why": ""}, sent)
    assert st.cad.settings.tone_overrides() == sent and res["sig"] == hub.channels_applied()["sig"]
    assert st.plan.match_pair(1153.4, 669.9) == ("Port Chester · Fire · Chiefs",)      # the live paging display's names
    assert st.plan.match_pair(747.3, 408.2) == ("Chappaqua · New Castle · EMS",)
    assert st.paging.is_ours(st.plan.match_pair(1153.4, 669.9)) and st.paging.is_ours(["Archville · Fire"])
    assert st.plan.match_pair(580.0, 2382.0) == ("Bedford · Fire (est.)",)
    n = len(st.cad.settings.audit_log("radios"))
    st.set_channel_prefs({}, {"ok": False, "why": ""}, sent)
    assert len(st.cad.settings.audit_log("radios")) == n
