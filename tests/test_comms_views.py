"""The Radio page's views on the server (GitHub #415): GET /api/cad/comms with ?view= ?system= ?group= ?source=
?service= ?tier= ?dept= ?safety= ?mute= ?before= ?changed= ?at= (Cad.comms_query -> Comms.page), the older parameters
answering as before, and GET /api/cad/comms/sources (the tree: Cad.comms_sources). Every view walks an index.

No radios, no network. Every transmission, unit, talkgroup number and address here is invented."""

from __future__ import annotations

import json
import re

import pytest

from sizeup import sourcekeys
from sizeup.cad.api import CadApi
from sizeup.cad.comms import CHANGES_MAX, KEEP_UNLINKED_S, PAGE, PAGE_MAX
from sizeup.cad.service import CadError
from sizeup.cloud.access import Identity
from tests.test_comms_changes import entry
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
    c.comms.context_broadcast = True          # as on the hub behind Access: every reader is a signed-in person (#419)
    c.radiolog.inline = True                  # (the units backfill on the test's thread: no write lands mid-test)
    t = clock.t
    c.record_traffic(tx("fg-1", t - 50, rig="E63", radio_id="4242098"))
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
    assert q(c, dept="No such department")["rows"] == []
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


def test_bad_parameters_are_refused(c):
    for bad in ({"view": "everything"}, {"service": "plumbing"}, {"before": "yesterday"}, {"at": "noon"},
                {"view": "all", "limit": "many"}, {"source": ",".join(f"fm:{i}" for i in range(40))}):
        with pytest.raises(CadError):
            q(c, **bad)
    code, raw, _ = CadApi(c).get("/api/cad/comms", "view=everything")
    assert code == 400 and "view" in json.loads(raw)["error"]


def test_changed_gives_everything_that_changed_since_the_cursor(c):
    first = q(c, view="police")
    assert q(c, view="police", changed=first["cursor"])["rows"] == []
    c.clockref.t += 5
    c.record_traffic(other("pd-3", c.clockref.t))
    c.record_traffic(tx("fg-2", c.clockref.t, rig="E63"))
    c.comms.retext("pd-1", "the transcript, arrived later", "done")             # the same clock tick: still seen
    got = q(c, view="police", changed=first["cursor"])
    assert sorted(ids(got)) == ["pd-1", "pd-3"] and got["reload"] is False
    assert got["gone"] == ["fg-2"]                                                # FG1's is another view's: not on this page
    assert next(r for r in got["rows"] if r["id"] == "pd-1")["text"] == "the transcript, arrived later"
    assert q(c, view="police", changed=got["cursor"])["rows"] == []
    assert ids(q(c, view="ours", changed=first["cursor"])) == ["fg-2"]
    assert "channels" not in got and "channels" in first                         # the choices: with a first page only
    # a link made and taken off, the alternative text, a link kept: each is a change of that row
    inc = _event_with(c, ["E63"], c.clockref)
    cur = q(c, view="police")["cursor"]
    assert c.comms.link("pd-2", inc["id"], "op").get("error") is None
    linked = q(c, view="police", changed=cur)
    assert ids(linked) == ["pd-2"] and [e["id"] for e in linked["rows"][0]["events"]] == [inc["id"]]
    c.comms.unlink("pd-2", inc["id"], "op")
    unlinked = q(c, view="police", changed=linked["cursor"])
    assert ids(unlinked) == ["pd-2"] and unlinked["rows"][0]["events"] == []
    c.comms.set_alt("pd-2", {"text": "another reading", "model": "m"})
    assert ids(q(c, view="police", changed=unlinked["cursor"])) == ["pd-2"]
    # an alarm raised and acknowledged on a row
    cur = q(c, view="ours")["cursor"]
    a = c.alarms.on_traffic(dict(tx("fg-9", c.clockref.t, text="mayday mayday mayday", rig="E63", radio_id="4242098"), at=c.clockref.t))
    c.record_traffic(tx("fg-9", c.clockref.t, text="mayday mayday mayday", rig="E63", radio_id="4242098"))
    raised = q(c, view="ours", changed=cur)
    assert "fg-9" in ids(raised) and next(r for r in raised["rows"] if r["id"] == "fg-9")["alarm_id"]
    c.alarms.acknowledge(a["id"], {"operator": "Lt. Sample"})
    seen = q(c, view="ours", changed=raised["cursor"])
    assert ids(seen) == ["fg-9"] and seen["rows"][0]["ack_at"]
    # a row deleted (pruned): a tombstone
    cur = q(c, view="police")["cursor"]
    c.clockref.t += KEEP_UNLINKED_S + 86400                  # past the retention: pruned (not pd-2: it was on an event)
    assert c.comms.prune() > 0
    assert "pd-3" in q(c, view="police", changed=cur)["gone"]
    c.clockref.t -= KEEP_UNLINKED_S + 86400
    # a note: ask afresh (ours and all carry notes; a police view doesn't)
    cur = q(c, view="ours")["cursor"]
    c.note(inc["id"], {"text": "E63 reports a made-up condition", "operator": "Lt. Sample", "units": ["E63"]})
    assert q(c, view="ours", changed=cur)["reload"] is True and q(c, view="police", changed=cur)["reload"] is False
    # too many, a cursor from elsewhere, a cursor older than what is kept: ask afresh
    cur = q(c, view="police")["cursor"]
    for i in range(CHANGES_MAX + 5):
        c.record_traffic(other(f"burst-{i}", c.clockref.t + 2))
    assert q(c, view="police", changed=cur)["reload"] is True
    assert q(c, view="police", changed=c.comms.cursor(entry(cur) + 10 ** 9, True))["reload"] is True
    import sizeup.cad.comms as comms_mod                        # older than what is kept: trimmed past it
    kept, comms_mod.CHANGES_KEPT = comms_mod.CHANGES_KEPT, 50
    try:
        c.comms._trim_changes()
    finally:
        comms_mod.CHANGES_KEPT = kept
    assert q(c, view="police", changed=cur)["reload"] is True


def test_another_systems_rows_do_not_exist_for_a_viewer_who_may_not_see_them(c):
    """#419's one gate decides who may (sourcekeys.may_see_context: a request from a signed-in person); here: what
    the views and the tree hold when it says no."""
    c.has_clip = lambda cid: True
    api = CadApi(c)
    who = Identity(email="someone@example.org")

    def get(path, query, w=None):
        code, raw, _ = api.get(path, query, w)
        assert code == 200
        return json.loads(raw)
    for query in ("view=police", "view=all", "", "source=p25:metro:9001", "system=metro", "dept=South+PD",
                  "view=all&q=parade", "q=parade", "view=police&at=1&limit=5", "view=all&changed=0"):
        out = get("/api/cad/comms", query)
        assert not [r for r in out["rows"] if r.get("system") == "metro"], query        # no id, time, talker or radio ID
        assert "parade" not in json.dumps(out) and "PD Dispatch" not in json.dumps(out) and "Car 7" not in json.dumps(out), query
    assert get("/api/cad/comms", "view=all&q=parade")["rows"] == []                       # the words can't be probed
    assert ids(get("/api/cad/comms", "view=all&q=parade", who)) == ["pd-2"]
    assert len(get("/api/cad/comms", "view=all")["rows"]) == 4 and len(get("/api/cad/comms", "view=all", who)["rows"]) == 7
    assert "PD Dispatch" in get("/api/cad/comms", "", who)["channels"]
    tree = get("/api/cad/comms/sources", "")
    assert not [s for s in tree["sources"] if s["system"] == "metro"] and "metro" not in json.dumps(tree)
    assert [s["key"] for s in get("/api/cad/comms/sources", "", who)["sources"] if s["system"] == "metro"]
    assert ids(get("/api/cad/comms", "view=ours")) == ids(get("/api/cad/comms", "view=ours", who))   # fire: as ever
    # a context row a person put on an incident: on the incident's list for a signed-in reader only, and never in
    # an event filter's answer for anyone else (the audio by id and the shared state: #419's own tests)
    inc = _event_with(c, ["E63"], c.clockref)
    assert c.comms.link("pd-2", inc["id"], "op").get("error") is None
    number = c.store.incident(inc["id"])["number"]
    assert ids(get("/api/cad/comms", f"view=all&event={number}", who)) == ["pd-2"]
    assert get("/api/cad/comms", f"view=all&event={number}")["rows"] == [] and get("/api/cad/comms", f"event={number}")["rows"] == []
    with sourcekeys.request_context(None):
        assert c.comms.for_incidents([inc["id"]])[inc["id"]] == []
    with sourcekeys.request_context(who):
        assert [r["id"] for r in c.comms.for_incidents([inc["id"]])[inc["id"]]] == ["pd-2"]


def test_mute_never_hides_our_calls_or_an_emergency(c):
    t = c.clockref.t
    c.record_traffic(dict(other("pd-em", t - 1), emergency=True))
    got = ids(q(c, view="all", mute="p25:metro:9001,fm:FG1,p25:westchester:307,air:Tower"))
    assert got == ["pd-em", "tr-1", "555-1-call_2-t0", f"{MAIN}-1-call_1-t0", "fg-1"]       # ours stay; the emergency stays


def test_a_long_p25_id_pages(c):
    t = c.clockref.t
    long_id = "9001-1790954731.970_476112500.0-call_1234567-t12-" + "x" * 40
    for i in range(3):
        c.record_traffic(other(f"{long_id}{i}", t + 50 + i))
    out = q(c, view="police", limit=2)
    assert len(out["next"]) > 60 and ids(q(c, view="police", limit=2, before=out["next"]))[0] == f"{long_id}0"
    with pytest.raises(CadError):
        q(c, view="police", before="1," + "x" * 300)


def test_a_group_or_tier_is_resolved_to_its_sources_first(c):
    assert ids(q(c, group="Police")) == ["pd-2", "pd-1"] and ids(q(c, tier="record")) == ["tr-1"]
    calls = []
    real = c.store.q
    c.store.q = lambda sql, args=(): (calls.append(sql), real(sql, args))[1]
    assert q(c, group="No such group")["rows"] == [] and q(c, tier="alert")["rows"] == []
    assert not [x for x in calls if "FROM radio_traffic t" in x]                 # nothing can match: the log isn't read
    c.store.q = real


def test_every_view_walks_an_index(c):
    """EXPLAIN QUERY PLAN of what Comms.page asks: a view never scans the log."""
    def plan(where, args=(), order="t.start DESC, t.id DESC", hint=""):
        rows = c.store.db.execute(f"EXPLAIN QUERY PLAN SELECT t.* FROM radio_traffic t {hint} WHERE {where} "
                                  f"ORDER BY {order} LIMIT 100", args).fetchall()
        return " | ".join(str(r[3]) for r in rows)
    assert "radio_traffic_service_start" in plan("t.service = ?", ("police",))
    assert "radio_traffic_system_start" in plan("t.system = ?", ("metro",))
    assert "radio_traffic_source_start" in plan("t.source = ?", ("fm:FG1",))
    assert "radio_traffic_dept_start" in plan("t.dept != '' AND t.dept = ?", ("South PD",))
    assert "radio_traffic_tg_start" in plan("t.talkgroup = ? AND t.system IN ('', ?)", (MAIN, "westchester"))
    assert "radio_traffic_tg_start" in plan("t.talkgroup IS NULL")
    assert "radio_traffic_safety" in plan("t.safety != '' AND t.safety != 'dismissed' AND +t.talkgroup IS NULL", (),
                                          hint="INDEXED BY radio_traffic_safety")
    for p in (plan("t.service = ?", ("police",)), plan("t.source = ?", ("fm:FG1",)), plan("t.talkgroup IS NULL")):
        assert "TEMP B-TREE" not in p.replace("USE TEMP B-TREE FOR LAST TERM OF ORDER BY", "")   # start comes sorted
    by_id = " ".join(str(r[3]) for r in c.store.db.execute(
        "EXPLAIN QUERY PLAN SELECT t.* FROM json_each(?) j CROSS JOIN radio_traffic t ON t.id = j.value WHERE t.service = ?",
        ("[]", "police")))
    assert "sqlite_autoindex_radio_traffic_1" in by_id                          # ?changed=: by id, whatever the view
    assert "PRIMARY KEY" in " ".join(str(r[3]) for r in c.store.db.execute(
        "EXPLAIN QUERY PLAN SELECT DISTINCT traffic_id FROM radio_changes WHERE seq > ? AND seq <= ?", (1, 2)))
    gated = " ".join(str(r[3]) for r in c.store.db.execute(
        "EXPLAIN QUERY PLAN SELECT DISTINCT c.traffic_id FROM radio_changes c WHERE c.oseq > ? AND c.oseq <= ? "
        "AND NOT EXISTS (SELECT 1 FROM radio_changes x WHERE x.traffic_id = c.traffic_id AND x.oseq IS NULL)", (1, 2)))
    assert "radio_changes_oseq" in gated and "radio_changes_id" in gated        # the gate on changes: by oseq, then by id
    assert "radio_activity_minute" in " ".join(str(r[3]) for r in c.store.db.execute(
        "EXPLAIN QUERY PLAN SELECT source, minute, calls FROM radio_activity WHERE minute > ? AND minute <= ?", (1, 2)))


def test_the_source_tree_with_last_heard_live_counts_and_emergencies(c):
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
    c.comms.activity("p25:metro:9003", int(t // 60) - 2, 3, ["4100012"])          # encrypted: key-ups only
    c.live = lambda: [{"system": "p25", "tg": MAIN, "channel": "Fire/EMS 15"}, {"channel": "FG1", "ended": True}]
    out = c.comms_sources()
    by = {s["key"]: s for s in out["sources"]}
    assert out["minutes"] == 60 and "p25:metro:9004" not in by and "p25:westchester:777" not in by
    assert [s["key"] for s in out["sources"]][:2] == [f"p25:westchester:{MAIN}", "p25:westchester:555"]   # ours first
    fire = by[f"p25:westchester:{MAIN}"]
    assert (fire["ours"], fire["system_label"], fire["name"], fire["talkgroup"]) == (True, "County", "Fire/EMS 15", MAIN)
    assert out["live"] == [f"p25:westchester:{MAIN}"]                           # FG1's has ended
    assert by["fm:FG1"]["ours"] and by["fm:FG1"]["system"] == "analog"
    assert by["air:Tower"]["service"] == "air" and not by["air:Tower"]["ours"]
    pd = by["p25:metro:9001"]
    assert (pd["name"], pd["service"], pd["tier"], pd["ours"], pd["system_label"]) == (
        "PD Dispatch (shared)", "police", "text", False, "Metro")
    assert pd["last"] == t - 10 and sum(n for _, n in pd["counts"]) == 2
    assert all(0 <= ago < 60 and n > 0 for ago, n in pd["counts"])              # only the minutes with calls
    enc = by["p25:metro:9003"]
    assert enc["encrypted"] and enc["tier"] == "activity" and enc["counts"] == [[2, 3]]
    assert enc["last"] == (int(t // 60) - 2) * 60
    assert not any("text" in s or "radios" in s for s in out["sources"])       # nothing of what was said, or by whom
    assert by["fm:South 2"]["counts"] == [] if "fm:South 2" in by else True
    # asked again with its rev: nothing but who is live
    again = c.comms_sources({"rev": [out["rev"]]})
    assert again == {"unchanged": True, "rev": out["rev"], "live": out["live"]}
    # an emergency nobody has acknowledged marks its source; acknowledged: no longer
    assert not any(s["emergency"] for s in out["sources"])
    a = c.alarms.on_traffic(dict(tx("fg-9", t, text="mayday mayday mayday", rig="E63", radio_id="4242098"), at=t))
    c.record_traffic(tx("fg-9", t, text="mayday mayday mayday", rig="E63", radio_id="4242098"))
    # a bare emergency flag with no alarm row marks nothing (it could never be cleared), nor does another system's row
    c.record_traffic(dict(tx("555-9-call_9-t0", t, "County Other", tg=555), emergency=True, group="Public Works"))
    c.record_traffic(dict(other("pd-em", t), emergency=True))
    by = {s["key"]: s for s in c.comms_sources()["sources"]}
    assert by["fm:FG1"]["emergency"] == "ours"
    assert by["p25:westchester:555"]["emergency"] == "" and by["p25:metro:9001"]["emergency"] == ""
    # someone else's emergency button the CAD raised a notice for: marked, as not ours
    n = c.alarms.on_traffic(dict(tx("555-9-call_9-t0", t, "County Other", tg=555, radio_id="4242396"), emergency=True, at=t))
    assert n["level"] == "notice"
    assert {s["key"]: s for s in c.comms_sources()["sources"]}["p25:westchester:555"]["emergency"] == "other"
    # seen but not cleared: still open, still marked; All clear: no longer
    c.alarms.acknowledge(a["id"], {"operator": "Lt. Sample"})
    assert {s["key"]: s for s in c.comms_sources()["sources"]}["fm:FG1"]["emergency"] == "ours"
    c.alarms.all_clear(a["id"], {"operator": "Lt. Sample"})
    assert {s["key"]: s for s in c.comms_sources()["sources"]}["fm:FG1"]["emergency"] == ""
    code, raw, _ = CadApi(c).get("/api/cad/comms/sources", "", Identity(email="someone@example.org"))
    assert code == 200 and len(json.loads(raw)["sources"]) == len(by)


def test_a_reader_who_may_not_see_another_system_is_told_nothing_of_its_changes(c):
    """`gone` and `reload` too: a context row's id says its talkgroup, time and frequency."""
    api, who = CadApi(c), Identity(email="someone@example.org")

    def changed(view, cursor, w=None):
        if cursor is None:                                      # everything since this start, in this reader's sequence
            cursor = c.comms.cursor(c.comms.changes_floor, True) if w else c.comms.cursor(c.comms.changes_floor_o, False)
        code, raw, _ = api.get("/api/cad/comms", f"view={view}&changed={cursor}", w)
        assert code == 200
        return json.loads(raw)
    start = None
    for view in ("all", "ours", "police"):
        out = changed(view, start)                              # ... for nobody signed in
        blob = json.dumps(out)
        assert "pd-" not in blob and "tr-1" not in blob and "9001" not in blob, view
        assert out["reload"] is False
    assert sorted(changed("all", start)["gone"] + ids(changed("all", start))) == sorted(["555-1-call_2-t0", f"{MAIN}-1-call_1-t0", "fg-1", "twr-1"])
    seen = changed("ours", start, who)
    assert {"pd-1", "pd-2", "tr-1"} <= set(seen["gone"])       # a signed-in reader: off this view, by id
    cur, cur_s = changed("all", start)["cursor"], changed("all", start, who)["cursor"]
    # a context row's transcript, a link on it, its deletion: nothing, for any view
    inc = _event_with(c, ["E63"], c.clockref)
    c.comms.retext("pd-1", "changed words", "done")
    c.comms.link("pd-2", inc["id"], "op")
    with c.store.tx() as db:                                # as prune() deletes a row: noted with its system
        c.comms.touch("tr-1", "metro", gone=True)
        db.execute("DELETE FROM radio_traffic WHERE id = 'tr-1'")
    for view in ("all", "ours", "police"):
        out = changed(view, cur)
        assert out["rows"] == [] and out["gone"] == [] and out["reload"] is False, view
    assert sorted(changed("all", cur_s, who)["gone"]) == ["tr-1"] and sorted(ids(changed("all", cur_s, who))) == ["pd-1", "pd-2"]
    assert changed("all", cur, who)["reload"] is True and changed("all", cur_s)["reload"] is True     # the other's sequence
    # so many context changes that a signed-in reader is told to reload: nobody else is
    cur, cur_s = changed("all", start)["cursor"], changed("all", start, who)["cursor"]
    for i in range(CHANGES_MAX + 5):
        c.record_traffic(other(f"burst-{i}", c.clockref.t + 2))
    assert changed("all", cur_s, who)["reload"] is True
    assert changed("all", cur) == {**changed("all", cur), "rows": [], "gone": [], "reload": False}


def test_the_change_log_is_trimmed_as_it_grows(c, monkeypatch):
    import sizeup.cad.comms as comms_mod
    monkeypatch.setattr(comms_mod, "CHANGES_KEPT", 50)
    monkeypatch.setattr(comms_mod, "CHANGES_TRIM_EVERY", 20)
    for i in range(100):
        c.record_traffic(tx(f"fg-{i + 10}", c.clockref.t + i))
    n = c.store.one("SELECT COUNT(*) AS n FROM radio_changes")["n"]
    assert 50 <= n <= 50 + 20 * 3                               # held near what is kept, without a restart


def test_who_is_talking_is_gated_like_the_rows(c):
    c.live = lambda: [{"system": "p25", "tg": MAIN, "channel": "Fire/EMS 15"},
                      {"system": "p25", "tg": PD, "p25_system": "metro", "channel": "PD Dispatch", "rig": "Car 7"},
                      {"source": "p25:metro:9002", "channel": "Rail Ops"}]
    api, who = CadApi(c), Identity(email="someone@example.org")

    def get(path, query, w=None):
        return json.loads(api.get(path, query, w)[1])
    for query in ("", "view=all", "view=police"):
        assert [e["channel"] for e in get("/api/cad/comms", query)["talking"]] == ["Fire/EMS 15"], query
        assert len(get("/api/cad/comms", query, who)["talking"]) == 3
    rev = get("/api/cad/comms", "view=all")["rev"]
    assert [e["channel"] for e in get("/api/cad/comms", f"view=all&rev={rev}")["talking"]] == ["Fire/EMS 15"]   # unchanged too
    assert get("/api/cad/comms/sources", "")["live"] == [f"p25:westchester:{MAIN}"]
    assert "p25:metro:9001" in get("/api/cad/comms/sources", "", who)["live"]
    from sizeup import sourcekeys as sk
    sk.set_context_talkgroups([PD])                             # a talker told only by its number, of another system's
    try:
        c.live = lambda: [{"system": "p25", "tg": PD, "channel": "PD Dispatch"}]
        assert get("/api/cad/comms", "view=all")["talking"] == []
    finally:
        sk.set_context_talkgroups([])


def test_every_filter_and_the_tree_tell_a_reader_who_is_not_signed_in_nothing_of_another_system(c):
    """The merge of main (#419's gate) with the views (#415): each of the views' filters, the paging (before, at,
    changed), the event choices and the source tree go through sourcekeys.may_see_context. Nobody signed in (the station's
    own page, an all-in-one, a hub without Access): no context row, name or count, whatever is asked. A signed-in
    person on a hub behind Access: all of it. Invented data only: the radio ID is a made-up one."""
    t = c.clockref.t
    c.record_traffic(dict(other("pd-9", t - 3, dept="South PD", text="made-up words about a kite"), radio_id="4100042"))
    inc = _event_with(c, ["E63"], c.clockref)
    assert c.comms.link("pd-9", inc["id"], "op").get("error") is None             # a context row put on an incident
    c.source_systems = lambda: {"westchester": "County", "metro": "Metro"}
    c.source_rows = lambda: [
        {"source": f"p25:metro:{PD}", "system": "metro", "name": "PD Dispatch", "group": "Police", "service": "police",
         "tier": "text", "department": "South PD"},
        {"source": f"p25:metro:{TRANSIT}", "system": "metro", "name": "Rail Ops", "group": "Transit", "service": "transit",
         "tier": "record"}]
    api, who = CadApi(c), Identity(email="someone@example.org")
    needles = ("metro", "Metro", "PD Dispatch", "Rail Ops", "South PD", "North PD", "kite", "parade", "Car 7", "4100042",
               str(PD), str(TRANSIT), "pd-", "tr-1")

    def get(path, query, w=None):
        code, raw, _ = api.get(path, query, w)
        assert code == 200, (path, query, raw)
        return json.loads(raw)

    signed = get("/api/cad/comms", "view=police&limit=1", who)                   # a page's next and cursor, at a context row
    assert ids(signed) == ["pd-9"] and signed["older"]
    asks = ["system=metro", "group=Police", "group=Transit", "service=police", "service=transit", "view=police",
            "view=transit", "view=all", "tier=text", "tier=record", "dept=South+PD", "dept=North+PD",
            f"source=p25:metro:{PD}", f"source=p25:metro:{PD},p25:metro:{TRANSIT}", "view=all&safety=1",
            f"view=all&mute=p25:metro:{TRANSIT}", "view=police&q=kite", "service=police&tier=text&dept=South+PD",
            f"view=police&before={signed['next']}", f"view=all&before={signed['next']}", f"view=police&at={int(t)}",
            f"view=all&at={int(t - 20)}&limit=3", f"view=police&changed={c.comms.cursor(c.comms.changes_floor_o, False)}",
            f"view=all&changed={c.comms.cursor(c.comms.changes_floor_o, False)}",
            f"view=all&changed={c.comms.cursor(c.comms.changes_floor, True)}", f"view=all&event={c.store.incident(inc['id'])['number']}"]
    for query in asks:
        blob = json.dumps(get("/api/cad/comms", query))
        assert not [n for n in needles if n in blob], (query, [n for n in needles if n in blob])
    # the event choices: the incident's only transmission is another system's
    assert inc["id"] not in [e["id"] for e in get("/api/cad/comms", "view=all")["events"]]
    assert inc["id"] not in [e["id"] for e in get("/api/cad/comms", "")["events"]]
    tree = get("/api/cad/comms/sources", "")
    blob = json.dumps(tree)
    assert not [n for n in needles if n in blob] and "Police" not in blob and "Transit" not in blob
    assert not [s for s in tree["sources"] if s["system"] != "westchester" and s["system"] not in ("analog", "airband")]
    # ... and the same outside any request when the app was not told it may (the station's page: no broadcast)
    c.comms.context_broadcast = False
    assert not [n for n in needles if n in json.dumps(c.comms_sources())]
    assert not [n for n in needles if n in json.dumps(q(c, service="police"))]
    c.comms.context_broadcast = True
    # signed in on a hub behind Access: every filter answers with the other system's rows, names and counts
    assert ids(get("/api/cad/comms", "system=metro", who)) == ["pd-9", "tr-1", "pd-2", "pd-1"]
    assert ids(get("/api/cad/comms", "group=Transit", who)) == ["tr-1"]
    assert ids(get("/api/cad/comms", "service=police", who)) == ["pd-9", "pd-2", "pd-1"]
    assert ids(get("/api/cad/comms", "tier=record", who)) == ["tr-1"]
    assert ids(get("/api/cad/comms", "dept=South+PD", who)) == ["pd-9", "pd-2"]
    assert ids(get("/api/cad/comms", f"view=police&before={signed['next']}", who)) == ["pd-2", "pd-1"]
    assert "pd-9" in ids(get("/api/cad/comms", f"view=police&changed={c.comms.cursor(c.comms.changes_floor, True)}", who))
    assert inc["id"] in [e["id"] for e in get("/api/cad/comms", "view=all", who)["events"]]
    by = {s["key"]: s for s in get("/api/cad/comms/sources", "", who)["sources"]}
    pd = by[f"p25:metro:{PD}"]
    assert (pd["name"], pd["group"], pd["department"], pd["system_label"]) == ("PD Dispatch", "Police", "South PD", "Metro")
    assert sum(n for _, n in pd["counts"]) == 3 and by[f"p25:metro:{TRANSIT}"]["name"] == "Rail Ops"


def test_main_s_batched_prune_notes_each_row_gone_and_a_big_clear_out_as_one_reset(c, monkeypatch):
    """main's prune (#414: per retention group, PRUNE_BATCH rows a transaction) with the views' change log: each deleted
    row noted gone (after its batch committed), past CHANGES_MAX one reset instead; a record or a linked row stays."""
    import sizeup.cad.comms as comms_mod
    t = c.clockref.t
    for i in range(6):
        c.record_traffic(tx(f"old-{i}", t + i, text="made-up words"))
    monkeypatch.setattr(c.comms, "PRUNE_BATCH", 2)
    cur = q(c, view="ours")["cursor"]
    c.clockref.t += KEEP_UNLINKED_S + 86400
    assert c.comms.prune() == 6 + 7                     # (and the fixture's seven, on no event)
    gone = q(c, view="ours", changed=cur)["gone"]
    assert {f"old-{i}" for i in range(6)} <= set(gone) and not c.comms.changes_broken
    for i in range(6):
        c.record_traffic(tx(f"older-{i}", c.clockref.t + i, text="made-up words"))
    monkeypatch.setattr(comms_mod, "CHANGES_MAX", 3)
    cur = q(c, view="ours")["cursor"]
    c.clockref.t += KEEP_UNLINKED_S + 86400
    assert c.comms.prune() >= 6
    assert q(c, view="ours", changed=cur)["reload"] is True
    assert c.store.one("SELECT COUNT(*) AS n FROM radio_changes WHERE traffic_id = 'reset'")["n"] >= 1


def test_no_answer_to_any_cursor_of_a_reader_not_signed_in_moves_with_another_systems_traffic(c):
    """#441 review blocker: the change cursor was an oracle (changed=cursor+k said reload exactly past the newest entry
    of ANY system, so it counted another system's transmissions and, polled, timed them). Now such a reader's cursor,
    its bounds and the trim are all in oseq: every answer, for every k, is the same before and after."""
    api = CadApi(c)

    def get(query):
        code, raw, _ = api.get("/api/cad/comms", query, None)
        assert code == 200
        return json.loads(raw)

    def answers():
        first = get("view=ours")
        cur = first["cursor"]
        return [first["cursor"], first["rev"], get("view=all")["cursor"]] + [
            get(f"view={v}&changed={c.comms.cursor(entry(cur) + k, False)}") for v in ("ours", "all", "police")
            for k in range(-3, 40)]
    before = answers()
    t = c.clockref.t
    for i in range(25):                                         # another system: rows, a transcript, key-ups, a prune
        c.record_traffic(other(f"pd-n{i}", t + 100 + i, text="made-up words"))
    c.comms.retext("pd-n0", "other made-up words", "done")
    c.comms.activity("p25:metro:9003", int(t // 60), 6, ["4100031"])
    assert answers() == before
    c.record_traffic(tx("fg-z", t + 200, rig="E63"))            # one of ours: the cursor moves by one entry, no more
    after = get("view=ours")
    assert entry(after["cursor"]) == entry(before[0]) + 1 and after["rev"] != before[1]
    assert ids(get(f"view=ours&changed={before[0]}")) == ["fg-z"]


def test_an_incident_edit_moves_the_rev_of_a_reader_not_signed_in(c):
    """Rows carry their incidents' number, type, address and status: an edit moves every reader's rev (the station
    board asks again), and the row comes back with the new address."""
    inc = _event_with(c, ["E63"], c.clockref)
    c.record_traffic(tx("fg-a", c.clockref.t + 1, rig="E63", radio_id="4242098"))
    api = CadApi(c)
    get = lambda q: json.loads(api.get("/api/cad/comms", q, None)[1])     # noqa: E731
    pushed = lambda: c.state()["comms_rev"]                               # noqa: E731
    c.comms.context_broadcast = False                                     # the station's own page
    first = get("type=radio")
    assert pushed() == first["rev"]
    c.update(inc["id"], {"address": "1 Invented Way", "operator": "Lt. Sample"})
    again = get(f"type=radio&rev={first['rev']}")
    assert "unchanged" not in again and again["rev"] != first["rev"] and pushed() == again["rev"]
    row = next(r for r in again["rows"] if r["id"] == "fg-a")
    assert [e["address"] for e in row["events"]] == ["1 Invented Way"]


def test_a_reader_not_signed_in_walks_none_of_another_systems_rows(c):
    """A view, service, group or tier with no source of ours answers without walking the rows it can't have (how
    long it took would tell how many there are): no query reads radio_traffic's rows."""
    api = CadApi(c)
    c.comms.sources_seen()                                      # (the per-rev source list: warm, as any page has it)
    seen = []
    c.store.db.set_trace_callback(seen.append)
    try:
        for query in ("view=police", "service=police", "view=transit", "group=Police", "group=Transit", "tier=text",
                      "tier=record", "system=metro", f"source=p25:metro:{PD}", "service=police&tier=text"):
            del seen[:]
            code, raw, _ = api.get("/api/cad/comms", query, None)
            assert code == 200 and json.loads(raw)["rows"] == [], query
            walks = [s for s in seen if re.search(r"FROM (json_each\(\?\) j CROSS JOIN )?radio_traffic t\b", s)]
            assert walks == [], (query, walks)
        # a department is a row's own: walked along our sources only (never radio_traffic_dept_start, which holds
        # another system's rows of it too)
        for query in ("dept=South+PD", "dept=North+PD", "view=all&dept=South+PD"):
            del seen[:]
            code, raw, _ = api.get("/api/cad/comms", query, None)
            assert code == 200 and json.loads(raw)["rows"] == [], query
            walks = [x for x in seen if re.search(r"FROM radio_traffic t\b", x)]
            assert walks and all("t.source = " in x for x in walks), (query, walks)
            plans = " ".join(str(r[3]) for x in walks for r in c.store.db.execute("EXPLAIN QUERY PLAN " + x))
            assert "radio_traffic_dept_start" not in plans and "radio_traffic_source_start" in plans, (query, plans)
    finally:
        c.store.db.set_trace_callback(None)
    # signed in: the same asks do read them
    who = Identity(email="someone@example.org")
    assert ids(json.loads(api.get("/api/cad/comms", "group=Police", who)[1])) == ["pd-2", "pd-1"]
    assert ids(json.loads(api.get("/api/cad/comms", "dept=South+PD", who)[1])) == ["pd-2"]


def test_a_cursor_from_before_a_start_is_told_to_reload(c, tmp_path):
    """The log's seq outlives a restart, so a page held across one would be answered from it, missing what ran at the
    start (the migrations' rewrites, the alarms' retention) and any entry the run before couldn't write: the floor is
    the log's newest entry at the start (read only: a start writes nothing)."""
    cur = q(c, view="ours")["cursor"]
    with sourcekeys.request_context(None):
        anon = q(c, view="ours")["cursor"]
    c.record_traffic(tx("fg-late", c.clockref.t + 1, rig="E63"))        # logged before the stop
    last = q(c, view="ours")["cursor"]                                   # ... and a cursor at the very last entry
    c.store.db.close()
    again = cad(tmp_path, c.clockref, airband=("Tower",))
    again.comms.context_broadcast = True
    assert q(again, view="ours", changed=cur)["reload"] is True
    assert q(again, view="ours", changed=last)["reload"] is True         # at the floor itself: another run's
    with sourcekeys.request_context(None):
        assert q(again, view="ours", changed=anon)["reload"] is True
        fresh_anon = q(again, view="ours")["cursor"]
    fresh = q(again, view="ours")["cursor"]
    again.record_traffic(tx("fg-after", c.clockref.t + 5, rig="E63"))
    out = q(again, view="ours", changed=fresh)
    assert out["reload"] is False and ids(out) == ["fg-after"]
    with sourcekeys.request_context(None):
        out = q(again, view="ours", changed=fresh_anon)
        assert out["reload"] is False and ids(out) == ["fg-after"]


def test_fire_channels_of_ours_are_never_muted(c):
    """Fire channels can't be muted: Our calls, and any fire source of ours (a county fire talkgroup not marked Main),
    in All too; the tree says which can't (mutable false)."""
    t = c.clockref.t
    c.record_traffic(dict(tx("7001-1-call_7-t0", t - 2, "Fire Ops 9", tg=7001), service="fire", group="Fire"))
    out = q(c, view="all", mute="p25:westchester:7001,p25:westchester:555,fm:FG1")
    assert "7001-1-call_7-t0" in ids(out) and "fg-1" in ids(out) and "555-1-call_2-t0" not in ids(out)
    by = {s["key"]: s for s in c.comms_sources()["sources"]}
    assert by["p25:westchester:7001"]["mutable"] is False and by["fm:FG1"]["mutable"] is False
    assert by["p25:westchester:555"]["mutable"] is True and by[f"p25:metro:{PD}"]["mutable"] is True


def test_the_state_push_tells_who_is_talking_through_the_same_gate(c):
    c.live = lambda: [{"system": "p25", "tg": MAIN, "channel": "Fire/EMS 15"},
                      {"system": "p25", "tg": PD, "p25_system": "metro", "channel": "PD Dispatch", "rig": "Car 7"}]
    c.comms.context_broadcast = False                          # the station's own page, an all-in-one
    assert [e["channel"] for e in c.state()["talking"]] == ["Fire/EMS 15"]
    with sourcekeys.request_context(Identity(email="someone@example.org")):
        assert len(c.state()["talking"]) == 2


def test_cursors_say_their_sequence_and_run(c):
    """A cursor is "s<seq>-<run>" for a reader who may see everything, "o<oseq>-<run>" for one who may not: one from the
    other sequence, another run, or none of these is told to reload (never answered from a different count)."""
    signed = q(c, view="ours")["cursor"]
    with sourcekeys.request_context(None):
        anon = q(c, view="ours")["cursor"]
        assert anon.startswith("o") and q(c, view="ours", changed=anon)["reload"] is False
        for bad in (signed, str(entry(anon)), f"o{entry(anon)}-00000000", "x", "o-1-" + anon.split("-")[1]):
            assert q(c, view="ours", changed=bad)["reload"] is True, bad
    assert signed.startswith("s") and q(c, view="ours", changed=signed)["reload"] is False
    assert q(c, view="ours", changed=anon)["reload"] is True


def test_another_systems_traffic_trims_prunes_and_resets_move_nothing_a_reader_not_signed_in_is_told(c, monkeypatch):
    """The oracle test again with a small log (CHANGES_KEPT, CHANGES_TRIM_EVERY, CHANGES_MAX): another system's rows
    crossing the trim, its transcripts, key-ups, a no-op transaction after a trim, and a real prune of its old rows and
    old key-up minutes past CHANGES_MAX (its own reset): a reader not signed in gets the same answer to every cursor, the
    same rev and the same tree rev. A signed-in reader is told to reload by that reset."""
    import sizeup.cad.comms as comms_mod
    monkeypatch.setattr(comms_mod, "CHANGES_KEPT", 5)
    monkeypatch.setattr(comms_mod, "CHANGES_TRIM_EVERY", 3)
    monkeypatch.setattr(comms_mod, "CHANGES_MAX", 4)
    t = c.clockref.t
    for i in range(4):
        c.record_traffic(tx(f"fg-o{i}", t + 1 + i, rig="E63"))
    c.comms.prune()                                             # (a scheduled prune: the log's steady state, before)
    api, who = CadApi(c), Identity(email="someone@example.org")

    def get(path, query, w=None):
        code, raw, _ = api.get(path, query, w)
        assert code == 200
        return json.loads(raw)

    def answers():
        first = get("/api/cad/comms", "view=ours")
        cur = entry(first["cursor"])
        return [first["cursor"], first["rev"], get("/api/cad/comms", "type=radio")["rev"],
                get("/api/cad/comms/sources", "")["rev"].rsplit(".", 2)[0]] + [
            get("/api/cad/comms", f"view={v}&changed={c.comms.cursor(cur + k, False)}")
            for v in ("ours", "all") for k in range(-12, 6)]
    before = answers()
    signed_cur = get("/api/cad/comms", "view=all", who)["cursor"]
    for i in range(25):                                         # crosses the trim of the other system's entries
        c.record_traffic(other(f"pd-y{i}", t + 100 + i, text="made-up words"))
    c.comms.retext("pd-y1", "other made-up words", "done")
    c.comms.set_alt("pd-y1", {"text": "made-up alt", "model": "m"})
    c.comms.rerun_done("pd-y1", "same")
    c.comms.activity("p25:metro:9003", int(t // 60), 4, ["4100012"])
    with c.store.tx():
        pass                                                    # a no-op transaction after the log's own trims
    # its rows and its key-up minutes grown old (as time would make them): a real prune, past CHANGES_MAX of them
    with c.store.tx() as db:
        db.execute("UPDATE radio_traffic SET start = ? WHERE id LIKE 'pd-y%'", (t - KEEP_UNLINKED_S - 3 * 86400,))
        c.comms.quiet()
    c.comms.activity("p25:metro:9003", int((t - comms_mod.ACTIVITY_KEEP_S) // 60) - 5, 2, ["4100012"])
    assert c.comms.prune() == 25
    assert answers() == before
    assert get("/api/cad/comms", f"view=all&changed={signed_cur}", who)["reload"] is True     # their reset
    # the log's own trim is never a change: a no-op after it moves no rev
    rev = get("/api/cad/comms", "view=ours")["rev"]
    c.comms._trim_changes()
    with c.store.tx():
        pass
    assert get("/api/cad/comms", "view=ours")["rev"] == rev
    # one of our key-up minutes going old does move it (a minute of ours went)
    c.comms.activity("p25:westchester:4100099", int((t - comms_mod.ACTIVITY_KEEP_S) // 60) - 5, 2, ["4100013"])
    rev = get("/api/cad/comms", "view=ours")["rev"]
    c.comms.prune()
    assert get("/api/cad/comms", "view=ours")["rev"] != rev


def test_an_older_change_log_gains_oseq_and_keeps_its_seq(c, tmp_path):
    """The log of the build before oseq: the column added at the start (not the table made again), so seq only grows
    and no cursor from before can match a new entry; its entries are nobody's but a reader's who may see everything."""
    db = c.store.db
    db.execute("DROP INDEX radio_changes_oseq")
    db.execute("ALTER TABLE radio_changes DROP COLUMN oseq")
    db.executemany("INSERT INTO radio_changes (traffic_id, system, gone) VALUES (?, '', 0)", [(f"old-{i}",) for i in range(30)])
    db.commit()
    top = db.execute("SELECT MAX(seq) FROM radio_changes").fetchone()[0]
    old = q(c, view="ours")["cursor"]
    db.close()
    again = cad(tmp_path, c.clockref, airband=("Tower",))
    again.comms.context_broadcast = True
    cols = {r[1] for r in again.store.db.execute("PRAGMA table_info(radio_changes)")}
    assert "oseq" in cols and not again.comms.changes_broken
    assert again.store.db.execute("SELECT MAX(seq) FROM radio_changes").fetchone()[0] == top          # kept, not made again
    assert again.comms.changes_floor == top and q(again, view="ours", changed=old)["reload"] is True
    fresh = q(again, view="ours")["cursor"]
    again.record_traffic(tx("fg-new", c.clockref.t + 9, rig="E63"))
    assert again.store.db.execute("SELECT seq FROM radio_changes WHERE traffic_id = 'fg-new'").fetchone()[0] > top
    assert ids(q(again, view="ours", changed=fresh)) == ["fg-new"]
    with sourcekeys.request_context(None):
        fresh_o = q(again, view="ours")["cursor"]
        again.record_traffic(tx("fg-new2", c.clockref.t + 10, rig="E63"))
        assert ids(q(again, view="ours", changed=fresh_o)) == ["fg-new2"]
