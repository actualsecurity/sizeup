"""The hub's speed (GitHub #284): per-request timing, the Radio view's indexes and reused answers, the tone table."""

import time
import urllib.request

from tests.test_cad import make_cad
from tests.test_cloud import HubRun, hub_cfg


def tx(cad, n, **kw):
    at = kw.pop("at", time.time() - 600 + n)
    t = {"id": f"tx-{n}", "start": at, "end": at + 3, "source": "Fire/EMS 15",
         "talkgroup": 307, "unit": "", "rig": "", "radio_id": "", "text": f"tx {n}", "status": "done", **kw}
    cad.record_traffic(t)
    return t


def test_every_answer_says_how_long_it_took(tmp_path):
    hub = HubRun(hub_cfg(tmp_path))
    try:
        with urllib.request.urlopen(hub.url + "/healthz", timeout=10) as r:
            st = r.headers.get("Server-Timing", "")
        assert st.startswith("app;dur=") and float(st.split("=")[1]) >= 0
    finally:
        hub.close()


def test_the_radio_views_queries_use_their_indexes(tmp_path):
    from sizeup.cad.comms import CHANNELS_SQL, RIGS_SQL, rig_rows_sql
    cad = make_cad(tmp_path)
    names = {r["name"] for r in cad.store.q("SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert {"radio_traffic_rig", "radio_traffic_tones", "radio_traffic_channel_tg_start"} <= names
    for sql, args, index in [(CHANNELS_SQL, (), "radio_traffic_channel_start"), (RIGS_SQL, (), "radio_traffic_rig"),
                             (rig_rows_sql(3), ("%,E60,%", "%,E63,%", "%,L31,%"), "radio_traffic_rig")]:
        plan = [r["detail"] for r in cad.store.q("EXPLAIN QUERY PLAN " + sql, args)]
        assert any(index in d for d in plan), (sql, plan)
        assert not any(d.strip() == "SCAN radio_traffic" for d in plan), (sql, plan)     # never the whole log


def test_facets_and_the_unit_filter_match_a_plain_scan(tmp_path):
    cad = make_cad(tmp_path)
    tx(cad, 1, rig="E60")
    tx(cad, 2, rig="E63, E60", source="FG1", talkgroup=None)
    tx(cad, 3, source="WCPD Ch 1", talkgroup=383)
    tx(cad, 4, rig="L31", source="FG1", talkgroup=None)
    f = cad.comms.facets()
    plain = [r["channel"] for r in cad.store.q("SELECT channel, MAX(start) AS m FROM radio_traffic GROUP BY channel "
                                               "ORDER BY m DESC")]
    assert f["channels"][:-1] == plain and f["channels"][-1] == "Notes"
    assert f["rigs"] == ["E60", "E63", "L31"]
    got = cad.comms_query({"unit": ["E60"]})
    assert sorted(r["id"] for r in got["rows"] if r["kind"] == "radio") == ["tx-1", "tx-2"]
    assert all(k not in got["rows"][0] for k in ("call_id", "turn", "updated_at"))     # never read by the page
    tx(cad, 5, rig="E64")                                    # a new transmission: new choices, a new answer
    assert "E64" in cad.comms.facets()["rigs"]
    assert any(r["rig"] == "E64" for r in cad.comms_query({})["rows"])


def test_the_same_question_at_the_same_rev_is_answered_once(tmp_path):
    cad = make_cad(tmp_path)
    tx(cad, 1)
    a = cad.comms_query({"channel": ["Fire/EMS 15"]})
    calls = []
    real = cad.comms.split
    cad.comms.split = lambda *x, **k: calls.append(1) or real(*x, **k)
    b = cad.comms_query({"channel": ["Fire/EMS 15"]})
    assert calls == [] and b["rows"] == a["rows"] and "talking" in b
    tx(cad, 2)
    assert len(cad.comms_query({"channel": ["Fire/EMS 15"]})["rows"]) == 2 and calls == [1]


def test_every_change_to_a_row_is_seen_at_once(tmp_path):
    """A reused answer never hides a change: a re-run transcript, a radio named, a link added or removed, an alarm."""
    from tests.test_alarms import tx as alarm_tx
    cad = make_cad(tmp_path)
    tx(cad, 1, source="FG1", talkgroup=None, unit="3B07", radio_id="3B07", text="")
    one = lambda: next(r for r in cad.comms_query({})["rows"] if r["id"] == "tx-1")
    assert one()["text"] == ""
    cad.comms.retext("tx-1", "Engine 63 on location", "done", "m")
    assert one()["text"] == "Engine 63 on location"
    cad.comms.relink("mdc", "3B07", "L31")
    assert one()["rig"] == "L31"
    iid = cad.create({"type": "STRU", "address": "1 Main St"})["id"]
    cad.link_traffic(iid, {"traffic_id": "tx-1", "operator": "Lt. Ruiz"})
    assert [e["id"] for e in one()["events"]] == [iid]
    cad.unlink_traffic(iid, {"traffic_id": "tx-1", "operator": "Lt. Ruiz"})
    assert one()["events"] == []
    assert one()["alarm_id"] is None
    cad.alarms.on_traffic(alarm_tx("tx-1", "Engine 63 mayday mayday mayday", rig="E63", radio_id="6301"))
    assert one()["alarm_id"] is not None


def test_pcfd_only_on_a_log_where_every_row_has_a_rig_stays_quick(tmp_path):
    """#284 review: every row with a rig (26 PCFD units OR'd) must not read the whole log once per section."""
    cad = make_cad(tmp_path)
    pcfd = [u["name"] for u in cad.radios.units if __import__("sizeup.cad.radios", fromlist=["is_pcfd"]).is_pcfd(u)]
    assert pcfd
    now = time.time()
    rows = [(f"x-{i}", now - 20000 + i * 0.1, now - 20000 + i * 0.1 + 2, ("FG1", "Fire/EMS 15", "WCPD Ch 1")[i % 3],
             (None, 307, 383)[i % 3], pcfd[i % len(pcfd)] if i % 4 == 0 else f"X{i % 300}", now) for i in range(30000)]
    with cad.store.tx() as db:
        db.executemany("INSERT INTO radio_traffic (id, start, end, channel, talkgroup, rig, updated_at) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    def took(q) -> float:                                              # the median of 3, never a reused answer
        ts = []
        for _ in range(3):
            t = time.perf_counter()
            assert cad.comms_query(dict(q, nc=[str(t)]))["rows"]
            ts.append(time.perf_counter() - t)
        return sorted(ts)[1]
    took({})                                                           # warm
    base = took({})                                                    # the unfiltered view, same log, same machine
    ratio = max(took(q) for q in ({"pcfd": ["1"]}, {"pcfd": ["1"], "channel": ["FG1"]}, {"unit": [pcfd[0]]},
                                  {"unit": ["X7"]})) / base
    assert ratio < 20, ratio                                           # ~5-10x; the regression was ~50x


def test_the_tone_table_counts_what_was_heard_near_each_pair():
    from sizeup.cad import tonelist
    from sizeup.radio.tones import ToneCode
    book = tonelist.Book([ToneCode("Port Chester Fire", 903.2, 810.2)], {}, set())
    heard = [tonelist.Heard("p1", 10.0, "South 2", 903.0, 810.0), tonelist.Heard("p2", 20.0, "South 2", 950.0, 810.0),
             tonelist.Heard("p3", 30.0, "South 2", 903.4, 811.0)]
    rows = [p for e in book.table(heard, {}) for p in e["pairs"] if p["a_hz"] == 903.2]
    assert rows and rows[0]["heard"] == 2 and rows[0]["last"] == 30.0
