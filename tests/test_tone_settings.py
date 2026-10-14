"""Settings > Radios > Tones (#115): every pair in one table (the county list with edits kept over it, the pairs named
here), heard but unnamed, the suggestions, the station sync; the decoder pairing by length (the 2026-10-14 07:04
South 2 page); the safety net for a page naming our units whose tones aren't ours; re-reading a page's tones."""

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
from sizeup.radio.tones import ToneCode, ToneDetector, ToneEvent, TonePlan
from tests.test_app import FakeWhisper, cfg, tone, voice, wait
from tests.test_cad import make_cad
from tests.test_roles import COREY, JULIAN, VAL, call, people

ROOT = Path(__file__).resolve().parent.parent
LIST = load(ROOT / "config.example.toml").tones          # data/westchester_tones.csv
LARCH = "435.1/435.1@Larchmont FD"


def app_with_list(tmp_path, **kw):
    return App(replace(cfg(tmp_path), tones=LIST, **kw))


def tset(app, op="Dana", **body):
    return app.cad.settings.post("tones", dict(body, action=body.pop("action", "set"), operator=op))


def row(app, rid):
    return next(r for r in app.cad.settings.tones_page()["rows"] if r["id"] == rid)


def names(app, a, b):
    return app.plan.match_pair(a, b)


# -- the table, edits over the list -----------------------------------------------------------------------------


def test_every_pair_in_one_table_with_its_source(tmp_path):
    app = app_with_list(tmp_path)
    d = app.cad.settings.tones_page()
    rows = d["rows"]
    assert len(rows) == len({tonelist.list_key(c) for c in LIST}) == len({r["id"] for r in rows})   # PCFD's: list + config
    lar = row(app, LARCH)
    assert (lar["name"], lar["origin"], lar["source"], lar["fetched"], lar["state"]) == ("Larchmont FD", "list", "rr-wiki", "2026-10-13", "list")
    assert lar["type"] == "fire" and lar["confirmed"] and not lar["ours"] and lar["heard"] == 0 and lar["last"] is None
    sc = row(app, "928.1/810.2@Scarsdale EMS")
    assert (sc["origin"], sc["type"], sc["source"]) == ("heard", "ems", "air")
    est = row(app, "580.0/2382.0@Bedford EMS")
    assert not est["confirmed"] and est["type"] == "ems"
    pc = row(app, "349.1/311.1@Port Chester FD")
    assert pc["ours"] and pc["ours_locked"]
    assert d["types"] == ["fire", "ems", "police", "other"] and "Rye Brook FD" in d["locked"]


def test_a_list_row_edited_kept_over_the_list_and_reset(tmp_path):
    app = app_with_list(tmp_path)
    told = []
    app.channels_check_tones = lambda: told.append(1)                    # the hub's push to the station
    tset(app, a_hz=435.1, b_hz=435.1, list="Larchmont FD", name="Larchmont-Mamaroneck FD", type="fire", confirmed=False)
    ov = app.cad.settings.tone_overrides()[LARCH]
    assert ov == {"list": "Larchmont FD", "name": "Larchmont-Mamaroneck FD", "a_hz": 435.1, "b_hz": 435.1, "ours": False,
                  "type": "fire", "confirmed": False}
    assert names(app, 435.1, 435.1) == ("Larchmont-Mamaroneck FD (est.)", "Peekskill")    # the list's file: untouched
    r = row(app, LARCH)
    assert (r["state"], r["name"], r["confirmed"], r["list"]["name"], r["operator"]) == ("edited", "Larchmont-Mamaroneck FD", False, "Larchmont FD", "Dana")
    assert told and "Larchmont FD" in (ROOT / "data" / "westchester_tones.csv").read_text()
    audit = app.cad.settings.audit_log("radios")[0]
    assert audit["key"] == f"tones/{LARCH}" and audit["operator"] == "Dana"
    # Edited back to what the list says: no edit left
    tset(app, a_hz=435.1, b_hz=435.1, list="Larchmont FD", name="Larchmont FD", type="fire", confirmed=True)
    assert LARCH not in app.cad.settings.tone_overrides() and row(app, LARCH)["state"] == "list"
    # Taken out of the plan, then Reset to list
    tset(app, action="delete", key=LARCH)
    assert row(app, LARCH)["state"] == "deleted" and names(app, 435.1, 435.1) == ("Peekskill",)
    assert app.cad.settings.tone_overrides()[LARCH] == {"list": "Larchmont FD", "a_hz": 435.1, "b_hz": 435.1, "deleted": True}
    tset(app, action="reset", key=LARCH)
    assert row(app, LARCH)["state"] == "list" and names(app, 435.1, 435.1) == ("Larchmont FD", "Peekskill")
    with pytest.raises(CadError, match="no edit to"):
        tset(app, action="reset", key=LARCH)


def test_pairs_named_here_added_edited_deleted_and_ours_by_name(tmp_path):
    app = app_with_list(tmp_path)
    tset(app, a_hz=788.5, b_hz=953.7, name="Harrison EMS", type="ems", page="S2-9")
    r = row(app, "788.5/953.7")
    assert (r["origin"], r["state"], r["type"], r["confirmed"], r["page"]) == ("named", "named", "ems", True, "S2-9")
    assert names(app, 788.5, 953.7) == ("Harrison EMS",)
    tset(app, a_hz=788.5, b_hz=953.7, name="Harrison EMS", type="ems", confirmed=False)     # an edit keeps its page
    assert app.cad.settings.tone_overrides()["788.5/953.7"]["page"] == "S2-9" and names(app, 788.5, 953.7) == ("Harrison EMS (est.)",)
    # Ours goes by the name: a second pair named the same is ours too; unticked on one, on all
    tset(app, a_hz=741.3, b_hz=623.7, name="Purchase", ours=True)
    assert row(app, "364.9/349.1@Purchase")["ours"] and app.paging.is_ours(("Purchase",))
    tset(app, a_hz=364.9, b_hz=349.1, list="Purchase", name="Purchase", ours=False)
    assert not app.cad.settings.tone_overrides()["741.3/623.7"]["ours"] and not app.paging.is_ours(("Purchase",))
    # PCFD's name is ours whatever the box says
    tset(app, a_hz=767.4, b_hz=928.1, name="Port Chester FD", ours=False)
    assert app.cad.settings.tone_overrides()["767.4/928.1"]["ours"] is True
    tset(app, action="delete", key="788.5/953.7")
    assert names(app, 788.5, 953.7) == () and "788.5/953.7" not in {r["id"] for r in app.cad.settings.tones_page()["rows"]}
    for bad in ({"a_hz": 700, "b_hz": 800, "name": "X", "type": "fd"}, {"a_hz": 700, "b_hz": 800, "name": "X", "confirmed": "yes"},
                {"a_hz": 700, "b_hz": 800, "deleted": True}, {"a_hz": 700, "b_hz": 800, "name": "X (heard)", "list": "X"}):
        with pytest.raises(CadError):
            tset(app, **bad)


def test_viewers_and_dispatchers_read_admins_edit(tmp_path):
    api = CadApi(make_cad(tmp_path))
    api.cad.log = lambda m: None
    people(api.cad)
    body = {"action": "set", "a_hz": 788.5, "b_hz": 953.7, "name": "Harrison EMS", "type": "ems"}
    for who in (VAL, JULIAN):
        code, out = call(api, "/api/cad/settings/tones", body, who)
        assert code == 403 and "Admins only" in out["error"]
        code, raw, _ = api.get("/api/cad/settings", "", Identity(email=who))
        assert code == 200 and "rows" in json.loads(raw)["tones"]
    assert call(api, "/api/cad/settings/tones", body, COREY)[0] == 200
    assert api.cad.settings.tone_overrides()["788.5/953.7"]["type"] == "ems"
    assert call(api, "/api/cad/settings/tones", {"action": "reread", "page": "x"}, JULIAN)[0] == 403


def test_export_import_round_trip_with_new_fields_and_84_entries(tmp_path):
    a = make_cad(tmp_path / "a")
    legacy = {"name": "Fairview", "a_hz": 832.5, "b_hz": 1201.0, "ours": False, "page": "S2-1"}      # #84's shape
    a.settings_post("tones", dict(legacy, action="set", operator="Dana"))
    a.settings_post("tones", {"action": "set", "a_hz": 435.1, "b_hz": 435.1, "list": "Larchmont FD", "name": "Larchmont FD",
                              "type": "ems", "confirmed": False, "ours": False, "operator": "Dana"})
    exp = a.settings_export()
    assert exp["overrides"]["tones"]["832.5/1201.0"] == legacy                   # untouched: the same signature as before
    assert exp["overrides"]["tones"][LARCH]["type"] == "ems"
    b = make_cad(tmp_path / "b")
    r = b.settings_post("import", {"action": "apply", "settings": exp, "operator": "Dana"})
    assert r["ok"] and b.settings.tone_overrides() == a.settings.tone_overrides()
    for k, v in (("1/2", legacy), (LARCH, {**legacy, "list": "Larchmont FD"}),
                 ("832.5/1201.0", {**legacy, "type": "fd"}), ("832.5/1201.0@X", {"list": "X", "a_hz": 832.5, "b_hz": 1201.0}),
                 ("832.5/1201.0", {"a_hz": 832.5, "b_hz": 1201.0, "deleted": True})):
        bad = json.loads(json.dumps(exp))
        bad["overrides"]["tones"] = {k: v}
        with pytest.raises(CadError):
            b.settings_post("import", {"action": "dry_run", "settings": bad, "operator": "Dana"})


def test_delete_on_a_cad_without_the_list_is_an_error(tmp_path):
    a = make_cad(tmp_path)
    with pytest.raises(CadError, match="no named tones"):
        a.settings_post("tones", {"action": "delete", "key": "1031.8/763.3@Ardsley", "operator": "Dana"})


# -- suggestions -------------------------------------------------------------------------------------------------


def test_the_dialogs_suggestions_include_ems_agencies_and_every_name_in_the_plan(tmp_path):
    app = app_with_list(tmp_path)
    tset(app, a_hz=788.5, b_hz=953.7, name="Our Lady EMS", type="ems")
    got = app.tone_names()
    sug = {x["name"]: x["type"] for x in got["suggest"]}
    for n in ("Harrison EMS", "Port Chester-Rye-Rye Brook EMS", "Armonk FD/EMS", "Westchester EMS", "Empress EMS",
              "Westchester EMS (45 Medics)", "Scarsdale EMS", "Katonah-Bedford Hills VAC", "Ossining PD/EMS", "Our Lady EMS"):
        assert sug.get(n) == "ems", n
    for n in ("Port Chester FD", "Rye Brook FD", "Larchmont FD", "Mamaroneck Town", "Briarcliff Manor", "Yonkers"):
        assert sug.get(n) == "fire", n
    assert sug["Westchester County Police"] == "police" and sug["Westchester County DES"] == "other"
    assert got["names"] == [x["name"] for x in got["suggest"]] and got["types"] == list(tonelist.TYPES)
    assert len({n.lower() for n in got["names"]}) == len(got["names"])                # once each


# -- heard but unnamed -------------------------------------------------------------------------------------------


def page_row(app, pid, start, pairs, text=""):
    app.cad.record_traffic({"id": pid, "start": start, "end": start + 10, "source": "South 2", "talkgroup": None,
                            "unit": "60 Control", "rig": "", "radio_id": "", "text": text, "status": "done",
                            "tones": [{"a_hz": a, "b_hz": b, "names": list(app.plan.match_pair(a, b)),
                                       "ours": app.paging.is_ours(app.plan.match_pair(a, b))} for a, b in pairs], "voice_at": 4.5})


def test_heard_but_unnamed_from_the_pages_and_the_unknown_tones_log(tmp_path):
    app = app_with_list(tmp_path)
    t0 = 1_790_000_000.0
    page_row(app, "p1", t0, [(741.3, 623.7), (928.1, 810.2)])                # unknown, stacked with Scarsdale EMS
    page_row(app, "p2", t0 + 600, [(741.6, 623.4)])                           # the same pair, a decode a little off
    page_row(app, "p3", t0 + 900, [(928.0, 810.3)])
    (tmp_path / "unknown_tones.jsonl").write_text(
        json.dumps({"at": t0 - 86400, "page": "old1", "channel": "South 2", "pairs": [[741.0, 624.0]], "named": []}) + "\n"
        + json.dumps({"at": t0, "page": "p1", "channel": "South 2", "pairs": [[741.3, 623.7]], "named": []}) + "\n"  # in both
        + json.dumps({"at": t0 + 30, "page": "old2", "channel": "South 2", "pairs": [[1500.0, None]], "named": []}) + "\n"
        + "not json\n")
    d = app.cad.settings.tones_page()
    un = d["unnamed"]
    assert [(u["a_hz"], u["b_hz"], u["count"], u["page"]) for u in un] == [(741.6, 623.4, 3, "p2"), (1500.0, None, 1, "old2")]
    assert un[0]["first"] == t0 - 86400 and un[0]["last"] == t0 + 600 and un[0]["channel"] == "South 2"
    sc = row(app, "928.1/810.2@Scarsdale EMS")
    assert (sc["heard"], sc["last"]) == (2, t0 + 900)
    # Named from the list: gone from it, the rows say the name
    tset(app, a_hz=741.6, b_hz=623.4, name="New Rochelle FD", page="p2")
    d = app.cad.settings.tones_page()
    assert [u["a_hz"] for u in d["unnamed"]] == [1500.0]
    assert row(app, "741.6/623.4")["heard"] == 3
    assert app.cad.store.q("SELECT tones FROM radio_traffic WHERE id = 'p1'")[0]["tones"].count("New Rochelle FD") == 1


def test_naming_from_the_list_as_ours_queues_its_page(tmp_path):
    """The dialog's flow from Settings: the latest page with the pair is read again and, ours, queued."""
    app = App(replace(cfg(tmp_path), tones=LIST, unit_departments={"Engine 61": "Port Chester FD"}))
    app._kind = {"South 2": "paging"}
    app.stt.client = FakeWhisper({6: "10 Charlie 2, Engine 61, 209 Westchester Avenue, Pearl Street and Gilbert Place"})
    app.stt.start()
    audio = np.concatenate([tone(669.9, 1.0), tone(1092.4, 3.0), np.zeros(int(0.2 * RATE)), voice(6.0)]).astype(np.float32)
    now = time.time()
    app._segment(Channel(470.2, "South 2"), Segment(now, now + len(audio) / RATE, audio, 25.0))
    pid = app.state()["pages"][0]["id"]
    assert wait(lambda: any(r["id"] == pid and r["text"] for r in app.cad.feed.rows(limit=20)))
    un = app.cad.settings.tones_page()["unnamed"]
    assert len(un) == 1 and un[0]["page"] == pid and un[0]["has_audio"] and app.cad.store.incidents() == []
    seq = app.cad.feed.rows(limit=20)[0]["tone_seq"]
    assert [(round(x["hz"]), x["role"]) for x in seq] == [(670, "A"), (1092, "B")]
    tset(app, a_hz=un[0]["a_hz"], b_hz=un[0]["b_hz"], name="Rye Brook FD", type="fire", page=un[0]["page"])
    ev = app.cad.store.incidents()
    assert len(ev) == 1 and ev[0]["status"] == "queued"
    assert app.cad.settings.tones_page()["unnamed"] == []


# -- the station ---------------------------------------------------------------------------------------------------


def test_the_station_takes_the_new_fields_with_the_channel_push(tmp_path):
    hub = app_with_list(tmp_path / "hub")
    tset(hub, a_hz=435.1, b_hz=435.1, list="Larchmont FD", name="Larchmont FD", type="ems", confirmed=False)
    tset(hub, action="delete", key="349.1/387.0@West Harrison")
    tset(hub, a_hz=788.5, b_hz=953.7, name="Harrison EMS", type="ems")
    sent = dict(hub._tone_ov)
    assert sent[LARCH]["type"] == "ems" and sent["349.1/387.0@West Harrison"]["deleted"]
    st = app_with_list(tmp_path / "st")
    res = st.set_channel_prefs({}, {"ok": False, "why": ""}, sent)
    assert st.cad.settings.tone_overrides() == sent and res["sig"] == hub.channels_applied()["sig"]   # the same signature
    assert st.plan.match_pair(435.1, 435.1) == ("Larchmont FD (est.)", "Peekskill")    # the live paging display's plan
    assert st.plan.match_pair(349.1, 387.0) == () and st.plan.match_pair(788.5, 953.7) == ("Harrison EMS",)
    assert st.cad.settings.audit_log("radios")[0]["operator"] == "hub"
    n = len(st.cad.settings.audit_log("radios"))
    assert st.set_channel_prefs({}, {"ok": False, "why": ""}, sent)["tones"] == 3      # the same again: nothing changes
    assert len(st.cad.settings.audit_log("radios")) == n


# -- the decoder: pairs by length (South 2, 2026-10-14 07:04) --------------------------------------------------------

rng = np.random.default_rng(115)


def t(hz, s):
    return 0.5 * np.sin(2 * np.pi * hz * np.arange(int(RATE * s)) / RATE + rng.uniform(0, 6))


def gap(s):
    return np.zeros(int(RATE * s))


def decode(*parts):
    a = np.concatenate(parts)
    a = a + 0.02 * rng.standard_normal(len(a))
    d = ToneDetector(RATE)
    ev = d.feed(a) + d.flush()
    return [(e.a_hz, e.b_hz) for e in ev], d.sequence()


PAIRS = [(767.4, 928.1), (903.2, 810.2)]


def same(got, want=PAIRS):
    return len(got) == len(want) and all(abs(a - wa) <= 0.01 * wa and abs(b - wb) <= 0.01 * wb for (a, b), (wa, wb) in zip(got, want))


@pytest.mark.parametrize("g", [0.0, 0.05, 0.2, 0.5])
def test_the_0704_stack_decodes_as_two_pairs_in_order(g):
    got, seq = decode(gap(0.3), t(767.4, 1), gap(g), t(928.1, 3), gap(g), t(903.2, 1), gap(g), t(810.2, 3), gap(0.5))
    assert same(got), got
    assert [x["role"] for x in seq] == ["A", "B", "A", "B"]
    assert [round(x["dur"]) for x in seq] == [1, 3, 1, 3]


@pytest.mark.parametrize("fade", [0.15, 0.25])
def test_a_fade_in_b_no_longer_splits_the_stack_across_departments(fade):
    """By order alone, a fade in B1 made its second half the next pair's A: 767.4/928.1 + 928.1/903.2 + 810.2/810.2."""
    got, seq = decode(gap(0.3), t(767.4, 1), t(928.1, 1.4), gap(fade), t(928.1, 1.6 - fade), t(903.2, 1), t(810.2, 3), gap(0.5))
    assert same(got), got
    assert not any(abs(a - 928.1) < 9 and b and abs(b - 903.2) < 9 for a, b in got)


def test_broken_pairs_are_reported_not_forced():
    # a short tone then another short one: the first is in no pair (by order it was "767.4/928.1")
    got, seq = decode(gap(0.3), t(767.4, 1), t(928.1, 1), t(903.2, 3), gap(0.5))
    assert same(got, [(928.1, 903.2)]) and [x["role"] for x in seq] == ["", "A", "B"]
    # a long tone with no short one before it (its A lost): not an A=B code
    got, seq = decode(gap(0.3), t(810.2, 3), gap(0.5))
    assert got == [] and [x["role"] for x in seq] == [""]
    # an A=B code (Larchmont): A 1 s + B 3 s at one frequency, one run of ~4 s
    got, seq = decode(gap(0.3), t(435.1, 1), t(435.1, 3), gap(0.5))
    assert same(got, [(435.1, 435.1)]) and [x["role"] for x in seq] == ["AB"]


def test_the_0704_pairs_against_the_list_and_scarsdale_ems():
    """(c) the plan's tolerance is +/-1.5 % (about half the spacing of neighbouring Motorola codes); (d) 928.1 and
    810.2 are Scarsdale EMS's A and B, but in the order heard they are B1 and B2 of two pairs: only a pair whose A
    and B are those two matches Scarsdale EMS."""
    plan = TonePlan(list(LIST))
    assert plan.tolerance == 0.015
    assert plan.match_pair(767.4, 928.1) == () and plan.match_pair(903.2, 810.2) == ()
    assert plan.match_pair(928.1, 810.2) == ("Scarsdale EMS (heard)",)
    assert plan.match_pair(349.1 * 1.014, 311.1 * 0.986) == ("Port Chester FD",)
    assert plan.match_pair(349.1 * 1.02, 311.1) == ()


def test_reread_tones_runs_the_decoder_again_and_queues_a_page_of_ours(tmp_path):
    app = App(replace(cfg(tmp_path), unit_departments={"Engine 61": "Port Chester FD"}))
    app._kind = {"South 2": "paging"}
    app.stt.client = FakeWhisper({6: "Engine 61, 209 Westchester Avenue, structure fire"})
    app.stt.start()
    tset(app, a_hz=903.2, b_hz=810.2, name="Port Chester FD", ours=True)
    audio = np.concatenate([tone(767.4, 1.0), tone(928.1, 3.0), tone(903.2, 1.0), tone(810.2, 3.0),
                            np.zeros(int(0.2 * RATE)), voice(6.0)]).astype(np.float32)
    now = time.time()
    seg = Segment(now, now + len(audio) / RATE, audio, 25.0)
    app._segment(Channel(470.2, "South 2"), seg)
    pid = app.state()["pages"][0]["id"]
    assert wait(lambda: len(app.cad.store.incidents()) == 1)          # PCFD's pair: queued from the air
    page = next(p for p in app.pages if p.id == pid)
    # As if decoded before the fix: split across (928.1/903.2), nothing of ours, no event
    page.tones = [ToneEvent("two_tone", 767.4, 928.1, 0, 4), ToneEvent("two_tone", 928.1, 903.2, 4, 6)]
    page.tone_seq = []
    app.store.incidents.clear()
    out = tset(app, action="reread", page=pid)
    assert same([(x["a_hz"], x["b_hz"]) for x in out["pairs"]]) and out["pairs"][1]["names"] == ["Port Chester FD"]
    assert [x["role"] for x in out["sequence"]] == ["A", "B", "A", "B"]
    assert len(app.store.incidents) == 1 and wait(lambda: len(app.cad.store.incidents()) == 2)   # read again: queued
    r = next(r for r in app.cad.feed.rows(limit=20) if r["id"] == pid)
    assert r["page"] == "ours" and len(r["tone_seq"]) == 4
    with pytest.raises(CadError, match="no page"):
        tset(app, action="reread", page="nope")


# -- the safety net: a page naming our units whose tones aren't ours -----------------------------------------------

TEXT_0704 = ("6462 to rescue 40. Number 2 Hanger Road, Westchester County Airport, hangar A. airport rows and a commercial "
             "arm. 64 622 rescue 402 hangar Road, Westchester County Airport, Hangar A. Airport the commercial alarm 0704690.")


def test_a_page_naming_our_units_with_tones_not_ours_is_flagged_with_the_ring_before_it(tmp_path):
    from sizeup.prealert import PreAlerts
    from tests.test_prealert import RING, STATUS, ring_msg
    app = app_with_list(tmp_path)
    pa = PreAlerts(app.cad, "http://127.0.0.1:9", RING, STATUS, log=lambda m: None)
    t0 = time.time() - 3600
    pa.ring(ring_msg("r1", at=t0 - 77))
    pa.tick(t0 + 600)                                                     # nothing came: expired
    page_row(app, "p0704", t0, [(767.4, 928.1), (903.2, 810.2)], TEXT_0704)
    page_row(app, "pfv", t0 + 60, [(832.5, 1201.0)], "Fairview, 34 Longdale Avenue, smoke in the building")
    page_row(app, "pmine", t0 + 120, [(349.1, 311.1)], "Engine 64, 209 Westchester Avenue")
    rows = {r["id"]: r for r in app.cad.feed.rows(limit=10)}
    r = rows["p0704"]
    assert r["page"] == "unknown" and r["names_ours"] == ["E64", "E62", "R40"] and r["events"] == []
    assert r["prealert"]["status"] == "expired" and abs(r["prealert"]["at"] - (t0 - 77)) < 1
    assert rows["pfv"]["names_ours"] == [] and "prealert" not in rows["pfv"]
    assert rows["pmine"]["names_ours"] == []                               # ours: an event, nothing to flag
    radio = {x["id"]: x for x in app.cad.comms.query(limit=10, kind="radio")}        # the Radio view's rows too
    assert radio["p0704"]["names_ours"] == ["E64", "E62", "R40"] and radio["pfv"]["names_ours"] == []
    assert app.cad.store.incidents() == []                                 # still not an event (#80)


def test_units_named_as_said(tmp_path):
    app = app_with_list(tmp_path)
    f = app.cad.feed.names_ours
    assert f(TEXT_0704) == ["E64", "E62", "R40"]
    assert f("Engine 64, Ladder 30, 23 91 respond") == ["E64", "L30", "2391"]
    assert f("64, 62 to 1 Main Street") == ["E64", "E62"]
    for none in ("64 King Street, structure fire", "Sixty Control, 60 Control to all units", "time out 1432",
                 "Fairview 34 Longdale Avenue", "Engine 25, 42 Stratton Road"):
        assert f(none) == [], none
