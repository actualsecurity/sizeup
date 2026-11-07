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


def test_the_radio_views_indexes_are_made(tmp_path):
    cad = make_cad(tmp_path)
    names = {r["name"] for r in cad.store.q("SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert {"radio_traffic_rig", "radio_traffic_tones", "radio_traffic_channel_tg_start"} <= names
    plan = " ".join(r["detail"] for r in cad.store.q(
        "EXPLAIN QUERY PLAN SELECT DISTINCT rig FROM radio_traffic WHERE rig != '' AND rig > 'A'"))
    assert "radio_traffic_rig" in plan


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


def test_the_tone_table_counts_what_was_heard_near_each_pair():
    from sizeup.cad import tonelist
    from sizeup.radio.tones import ToneCode
    book = tonelist.Book([ToneCode("Port Chester Fire", 903.2, 810.2)], {}, set())
    heard = [tonelist.Heard("p1", 10.0, "South 2", 903.0, 810.0), tonelist.Heard("p2", 20.0, "South 2", 950.0, 810.0),
             tonelist.Heard("p3", 30.0, "South 2", 903.4, 811.0)]
    rows = [p for e in book.table(heard, {}) for p in e["pairs"] if p["a_hz"] == 903.2]
    assert rows and rows[0]["heard"] == 2 and rows[0]["last"] == 30.0
