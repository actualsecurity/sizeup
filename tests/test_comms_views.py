"""The Radio page's views on the server (GitHub #415): GET /api/cad/comms with ?view= ?system= ?group= ?source=
?service= ?tier= ?dept= ?safety= ?mute= ?before= ?changed= ?at= (Cad.comms_query -> Comms.page), the older parameters
answering as before, and GET /api/cad/comms/sources (the tree: Cad.comms_sources). Every view walks an index.

No radios, no network. Every transmission, unit, talkgroup number and address here is invented."""

from __future__ import annotations

import json

import pytest

from sizeup.cad.api import CadApi
from sizeup.cad.comms import PAGE, PAGE_MAX
from sizeup.cad.service import CadError
from sizeup.cloud.access import Identity
from tests.test_radios import Clock, _event_with, cad, tx

PD, TRANSIT, MAIN = 9001, 9002, 307


def other(tid, start, tg=PD, channel="PD Dispatch", service="police", group="Police", tier="text", dept="", text="words",
          system="metro"):
    """A transmission of a second P25 system, as the app hands it to the CAD (sizeup/app.py)."""
    return dict(tx(tid, start, channel, unit="Car 7", tg=tg, text=text), system=system, source_key=f"p25:{system}:{tg}",
                group=group, service=service, tier=tier, dept=dept)


def ids(out):
    return [r["id"] for r in out["rows"]]


@pytest.fixture
def c(tmp_path):
    clock = Clock()
    c = cad(tmp_path, clock, airband=("Tower",))
    c.clockref = clock
    t = clock.t
    c.record_traffic(tx("fg-1", t - 50, rig="E63", radio_id="6301"))
    c.record_traffic(dict(tx(f"{MAIN}-1-call_1-t0", t - 40, "Fire/EMS 15", "E60", "E60", MAIN), ours=True, service="fire",
                          group="Fire"))
    c.record_traffic(dict(tx("555-1-call_2-t0", t - 35, "County Other", tg=555), group="Public Works"))
    c.record_traffic(dict(tx("twr-1", t - 30, "Tower"), mhz=119.7))
    c.record_traffic(other("pd-1", t - 20, dept="North PD"))
    c.record_traffic(other("pd-2", t - 10, dept="South PD", text="made-up words about a parade"))
    c.record_traffic(other("tr-1", t - 5, TRANSIT, "Rail Ops", "transit", "Transit", "record"))
    return c


def q(c, **kw):
    return c.comms_query({k: [str(v)] if not isinstance(v, list) else v for k, v in kw.items()})


def test_the_old_parameters_answer_as_before(c):
    out = c.comms_query({})
    assert {"rows", "rev", "counts", "limits", "channels", "rigs", "events", "talking"} <= set(out)
    assert "older" not in out and "cursor" not in out
    assert [r["id"] for r in out["rows"] if r["main"]] == [f"{MAIN}-1-call_1-t0", "fg-1"]
    assert out["counts"] == {"main": 2, "other": 5}
    assert ids(q(c, channel="FG1")) == ["fg-1"] and "limits" in q(c, channel="FG1")


def test_our_calls_is_main_and_never_another_systems_talkgroup(c):
    t = c.clockref.t
    # a second system reusing our Main talkgroup's number: a talkgroup number alone is never read
    c.record_traffic(other("clash-1", t - 1, MAIN, "Clash", "police", "Police"))
    out = q(c, view="ours")
    assert ids(out) == [f"{MAIN}-1-call_1-t0", "fg-1"]
    assert all(r["main"] for r in out["rows"]) and out["view"] == "ours" and out["older"] is False
    assert "clash-1" in ids(q(c, view="all")) and "clash-1" in ids(q(c, view="police"))
    assert not next(r for r in q(c, view="all")["rows"] if r["id"] == "clash-1")["main"]


def test_service_views_and_the_rows_own_fields(c):
    assert ids(q(c, view="police")) == ["pd-2", "pd-1"]
    assert ids(q(c, view="transit")) == ["tr-1"]
    assert ids(q(c, view="air")) == ["twr-1"]
    assert ids(q(c, view="ems")) == []
    assert ids(q(c, service="police")) == ["pd-2", "pd-1"]
    assert ids(q(c, system="metro")) == ["tr-1", "pd-2", "pd-1"]
    assert ids(q(c, system="metro", group="Transit")) == ["tr-1"]
    assert ids(q(c, tier="record")) == ["tr-1"]
    assert ids(q(c, dept="South PD")) == ["pd-2"]
    assert ids(q(c, view="police", q="parade")) == ["pd-2"]                 # the older filters still narrow a view
    assert len(q(c, view="all")["rows"]) == 7
    row = q(c, dept="South PD")["rows"][0]
    assert (row["source"], row["system"], row["group"], row["service"], row["tier"], row["dept"]) == (
        "p25:metro:9001", "metro", "Police", "police", "text", "South PD")
    assert {"call_id", "turn", "updated_at"}.isdisjoint(row) and "has_audio" in row


def test_sources_and_mute(c):
    assert ids(q(c, source="p25:metro:9002")) == ["tr-1"]
    assert ids(q(c, source=["p25:metro:9002", "fm:FG1"])) == ["tr-1", "fg-1"]
    assert ids(q(c, source="p25:metro:9002,air:Tower")) == ["tr-1", "twr-1"]
    assert ids(q(c, view="ours", source="fm:FG1,p25:metro:9001")) == ["fg-1"]      # a source inside a view
    got = ids(q(c, view="all", mute="p25:metro:9001,air:Tower"))
    assert got == ["tr-1", "555-1-call_2-t0", f"{MAIN}-1-call_1-t0", "fg-1"]
    assert ids(q(c, source="p25:metro:9001", mute="p25:metro:9001")) == ["pd-2", "pd-1"]   # asked for by name: shown


def test_older_pages_by_keyset_lose_and_repeat_nothing(c):
    t = c.clockref.t
    for i in range(250):                                  # pairs at the same instant: the id breaks the tie
        c.record_traffic(other(f"bulk-{i:03d}", t + 100 + i // 2))
    seen, before, pages = [], None, 0
    while True:
        out = q(c, view="police", **({"before": before} if before else {}))
        seen += ids(out)
        pages += 1
        if not out["older"]:
            break
        assert len(out["rows"]) == PAGE
        before = out["next"]
    assert pages == 3 and len(seen) == len(set(seen)) == 252
    assert seen[:3] == ["bulk-249", "bulk-248", "bulk-247"] and seen[-1] == "pd-1"
    assert len(q(c, view="police", limit=5000)["rows"]) == PAGE_MAX and len(q(c, view="police", limit=7)["rows"]) == 7


def test_changed_gives_only_what_is_new_or_changed_since_the_cursor(c):
    first = q(c, view="police")
    assert q(c, view="police", changed=first["cursor"])["rows"] == []
    c.clockref.t += 5
    c.record_traffic(other("pd-3", c.clockref.t))
    c.record_traffic(tx("fg-2", c.clockref.t, rig="E63"))
    c.clockref.t += 1
    c.comms.retext("pd-1", "the transcript, arrived later", "done")
    got = q(c, view="police", changed=first["cursor"])
    assert sorted(ids(got)) == ["pd-1", "pd-3"] and got["reload"] is False       # FG1's is another view's
    assert next(r for r in got["rows"] if r["id"] == "pd-1")["text"] == "the transcript, arrived later"
    assert q(c, view="police", changed=got["cursor"])["rows"] == []
    assert ids(q(c, view="ours", changed=first["cursor"])) == ["fg-2"]
    assert "channels" not in got and "channels" in first                         # the choices: with a first page only
    c.clockref.t += 1
    for i in range(PAGE_MAX + 5):
        c.record_traffic(other(f"burst-{i}", c.clockref.t + 2))
    assert q(c, view="police", changed=got["cursor"])["reload"] is True


def test_at_gives_a_page_around_a_time(c):
    t = c.clockref.t
    for i in range(40):
        c.record_traffic(other(f"m-{i:02d}", t + 1000 + i * 10))
    out = q(c, view="police", at=t + 1000 + 200, limit=10)
    assert ids(out) == [f"m-{i:02d}" for i in range(24, 14, -1)]
    assert out["older"] is True and out["newer"] is True
    assert q(c, view="police", at=t + 1000 + 395, limit=10)["newer"] is False


def test_safety_rows_only(c):
    c.store.db.execute("UPDATE radio_traffic SET safety = 'suggested' WHERE id = 'pd-1'")
    c.store.db.execute("UPDATE radio_traffic SET safety = 'dismissed' WHERE id = 'pd-2'")
    assert ids(q(c, safety=1)) == ["pd-1"]
    assert ids(q(c, view="ours", safety=1)) == []


def test_notes_ride_with_our_calls_and_all_only(c):
    clock = c.clockref
    inc = _event_with(c, ["E63"], clock)
    clock.t += 1
    c.note(inc["id"], {"text": "E63 reports a made-up condition", "operator": "Lt. Sample", "units": ["E63"]})
    assert any(r["kind"] == "note" for r in q(c, view="ours")["rows"])
    assert any(r["kind"] == "note" for r in q(c, view="all")["rows"])
    assert not any(r["kind"] == "note" for r in q(c, view="police")["rows"])
    assert not any(r["kind"] == "note" for r in q(c, source="fm:FG1")["rows"])


def test_police_words_and_audio_only_to_someone_signed_in(c):
    c.has_clip = lambda cid: True
    api = CadApi(c)
    for query in ("view=police", ""):                                       # the views and the older answer alike
        code, raw, _ = api.get("/api/cad/comms", query, None)
        rows = {r["id"]: r for r in json.loads(raw)["rows"]}
        assert code == 200 and rows["pd-2"]["text"] == "" and rows["pd-2"]["redacted"] and not rows["pd-2"]["has_audio"]
        code, raw, _ = api.get("/api/cad/comms", query, Identity(email="someone@example.org"))
        rows = {r["id"]: r for r in json.loads(raw)["rows"]}
        assert rows["pd-2"]["text"] == "made-up words about a parade" and rows["pd-2"]["has_audio"]
        assert "redacted" not in rows["pd-2"]
    code, raw, _ = api.get("/api/cad/comms", "view=ours", None)                # fire: as ever
    assert json.loads(raw)["rows"][0]["text"] == "words"


def test_bad_parameters_are_refused(c):
    for bad in ({"view": "everything"}, {"service": "plumbing"}, {"before": "yesterday"}, {"at": "noon"},
                {"view": "all", "limit": "many"}, {"source": ",".join(f"fm:{i}" for i in range(40))}):
        with pytest.raises(CadError):
            q(c, **bad)
    code, raw, _ = CadApi(c).get("/api/cad/comms", "view=everything")
    assert code == 400 and "view" in json.loads(raw)["error"]


def test_every_view_walks_an_index(c):
    """EXPLAIN QUERY PLAN of what Comms.page asks: a view never scans the log."""
    def plan(where, args=(), order="t.start DESC, t.id DESC", hint=""):
        rows = c.store.db.execute(f"EXPLAIN QUERY PLAN SELECT t.* FROM radio_traffic t {hint} WHERE {where} "
                                  f"ORDER BY {order} LIMIT 100", args).fetchall()
        return " | ".join(str(r[3]) for r in rows)
    assert "radio_traffic_service_start" in plan("t.service = ?", ("police",))
    assert "radio_traffic_system_start" in plan("t.system = ?", ("metro",))
    assert "radio_traffic_source_start" in plan("t.source = ?", ("fm:FG1",))
    assert "radio_traffic_tg_start" in plan("t.talkgroup = ? AND t.system IN ('', ?)", (MAIN, "westchester"))
    assert "radio_traffic_tg_start" in plan("t.talkgroup IS NULL")
    assert "radio_traffic_safety" in plan("t.safety != '' AND t.safety != 'dismissed'")
    assert "radio_traffic_updated" in plan("t.updated_at > ?", (0,), "t.updated_at", "INDEXED BY radio_traffic_updated")
    for p in (plan("t.service = ?", ("police",)), plan("t.source = ?", ("fm:FG1",)), plan("t.talkgroup IS NULL")):
        assert "TEMP B-TREE" not in p.replace("USE TEMP B-TREE FOR LAST TERM OF ORDER BY", "")   # start comes sorted
    assert "radio_activity_minute" in " ".join(str(r[3]) for r in c.store.db.execute(
        "EXPLAIN QUERY PLAN SELECT source, minute, calls FROM radio_activity WHERE minute > ? AND minute <= ?", (1, 2)))


def test_the_source_tree_with_last_heard_live_and_counts(c):
    t = c.clockref.t
    c.main_tg = lambda tg: tg == MAIN
    c.source_systems = lambda: {"westchester": "County", "metro": "Metro"}
    c.source_rows = lambda: [
        {"source": "p25:metro:9001", "system": "metro", "name": "PD Dispatch (shared)", "group": "Police", "service": "police",
         "tier": "text", "encrypted": False, "department": ""},
        {"source": "p25:metro:9003", "system": "metro", "name": "PD Secure", "group": "Police", "service": "police",
         "tier": "activity", "encrypted": True},
        {"source": "p25:metro:9004", "system": "metro", "name": "Off TG", "group": "Police", "tier": "off"},
        {"source": "p25:westchester:777", "system": "westchester", "name": "Never heard", "group": "Other"}]
    c.comms.activity("p25:metro:9003", int(t // 60) - 2, 3, ["1234"])          # encrypted: key-ups only
    c.live = lambda: [{"system": "p25", "tg": MAIN, "channel": "Fire/EMS 15"}, {"channel": "FG1", "ended": True}]
    out = c.comms_sources()
    by = {s["key"]: s for s in out["sources"]}
    assert out["minutes"] == 60 and "p25:metro:9004" not in by and "p25:westchester:777" not in by
    assert [s["key"] for s in out["sources"]][:2] == [f"p25:westchester:{MAIN}", "p25:westchester:555"]   # ours first
    fire = by[f"p25:westchester:{MAIN}"]
    assert (fire["ours"], fire["live"], fire["system_label"], fire["name"], fire["talkgroup"]) == (
        True, True, "County", "Fire/EMS 15", MAIN)
    assert by["fm:FG1"]["ours"] and not by["fm:FG1"]["live"] and by["fm:FG1"]["system"] == "analog"
    assert by["air:Tower"]["service"] == "air" and not by["air:Tower"]["ours"]
    pd = by["p25:metro:9001"]
    assert (pd["name"], pd["service"], pd["tier"], pd["ours"], pd["system_label"]) == (
        "PD Dispatch (shared)", "police", "text", False, "Metro")
    assert pd["last"] == t - 10 and sum(pd["counts"]) == 2 and len(pd["counts"]) == 60
    enc = by["p25:metro:9003"]
    assert enc["encrypted"] and enc["tier"] == "activity" and sum(enc["counts"]) == 3 and enc["counts"][-3] == 3
    assert enc["last"] == (int(t // 60) - 2) * 60
    assert not any("text" in s or "radios" in s for s in out["sources"])       # nothing of what was said, or by whom
    again = c.comms_sources()
    assert again["sources"] == out["sources"]
    code, raw, _ = CadApi(c).get("/api/cad/comms/sources", "")
    assert code == 200 and len(json.loads(raw)["sources"]) == len(out["sources"])
