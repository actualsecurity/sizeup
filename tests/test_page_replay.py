"""Replaying a page (#64): every South 2 page is a row in the CAD's communications log (Radio view, with play
and its tone names), and the event a 60 Control page opens is linked to it (why "page"), so the page plays
from the event, before and after Accept, and after a restart. Also: unknown pairs logged once per page."""

import json
import time
from dataclasses import replace

import numpy as np

from sizeup.app import App
from sizeup.cad.api import CadApi
from sizeup.cloud.access import Identity
from sizeup.radio.fm import RATE, Channel, Segment
from tests.test_app import PAGE_TEXT, FakeWhisper, cfg, tone, voice, wait
from tests.test_cad import make_cad


def page_audio(*pairs, voice_s=6.0):
    parts = [tone(h, d) for a, b in pairs for h, d in ((a, 1.0), (b, 3.0))]
    return np.concatenate(parts + [np.zeros(int(0.2 * RATE)), voice(voice_s)]).astype(np.float32)


def send_page(app, audio, at=None):
    now = time.time() if at is None else at
    app._segment(Channel(470.2, "South 2"), Segment(now, now + len(audio) / RATE, audio, 25.0))
    return app.state()["pages"][0]["id"]


def queued(app):
    return [i for i in app.cad.state()["incidents"] if i["status"] == "queued"]


def test_a_page_is_a_radio_row_and_plays_from_the_event_it_opened(tmp_path):
    c = replace(cfg(tmp_path), cad_db=str(tmp_path / "cad.sqlite"))
    app = App(c)
    app._kind = {"South 2": "paging"}
    app.stt.client = FakeWhisper({6: PAGE_TEXT})
    app.stt.start()
    pid = send_page(app, page_audio((349.1, 311.1), (741.3, 623.7)))
    # In Radio at once (Transcribing), with its tone pairs; the unknown pair has no name.
    rows = app.cad.comms_query({})["rows"]
    row = next(r for r in rows if r["id"] == pid)
    assert row["channel"] == "South 2" and row["talker"] == "60 Control" and row["has_audio"] and row["main"]
    assert [t["names"] for t in row["tones"]] == [["Port Chester · Fire"], []]
    assert row["tones"][1]["a_hz"] == np.round(row["tones"][1]["a_hz"], 1) and abs(row["tones"][1]["a_hz"] - 741.3) < 2
    assert 8.0 < row["voice_at"] < 8.5                                       # "voice only" starts after the tones
    assert wait(lambda: queued(app))
    [ev] = queued(app)
    radio = ev["radio"]
    assert [(r["id"], r["why"]) for r in radio] == [(pid, "page")] and radio[0]["has_audio"]
    assert radio[0]["text"].startswith("Port Chester Fire Department")      # the transcript reached the log
    assert app.clip(pid)[:4] == b"RIFF"                                       # /api/audio/<page id>.wav
    app.cad.accept(ev["id"], {"operator": "Lt. Ruiz"})
    ev = next(i for i in app.cad.state()["incidents"] if i["id"] == ev["id"])
    assert ev["status"] != "queued" and [(r["id"], r["why"]) for r in ev["radio"]] == [(pid, "page")]   # kept after Accept
    # The repeat of the page goes on the same event, accepted or not.
    pid2 = send_page(app, page_audio((349.1, 311.1)), at=time.time() + 30)
    assert wait(lambda: len(next(i for i in app.cad.state()["incidents"] if i["id"] == ev["id"])["radio"]) == 2)
    # A restart: the link is in cad.sqlite, the page's row and audio too.
    again = App(c)
    ev2 = next(i for i in again.cad.state()["incidents"] if i["id"] == ev["id"])
    assert {(r["id"], r["why"]) for r in ev2["radio"]} == {(pid, "page"), (pid2, "page")}
    assert all(r["has_audio"] for r in ev2["radio"]) and again.clip(pid)[:4] == b"RIFF"


def test_an_event_without_a_link_has_no_page_to_play_and_a_page_can_be_added_by_hand(tmp_path):
    cad = make_cad(tmp_path)
    old = cad.store.create_incident(type="", address="12 Main St", source="60 Control page", source_ref="monitor:old",
                                    status="queued")
    assert cad.full(old)["radio"] == []                                        # from before #64: nothing guessed
    cad.record_traffic({"id": "pg1", "start": time.time(), "end": time.time() + 12, "source": "South 2", "talkgroup": None,
                        "unit": "60 Control", "text": "Port Chester Fire Department ...", "status": "done",
                        "tones": [{"a_hz": 349.1, "b_hz": 311.1, "names": ["Port Chester FD"]}], "voice_at": 4.2})
    assert cad.link_traffic(old, {"traffic_id": "pg1", "operator": "Lt. Ruiz"})["linked"]   # the "⋯" menu's Add to event
    [r] = cad.full(old)["radio"]
    assert (r["id"], r["why"], r["voice_at"], r["tones"][0]["names"]) == ("pg1", "manual", 4.2, ["Port Chester FD"])
    # A person took the page off the event: a later page link does not put it back.
    cad.unlink_traffic(old, {"traffic_id": "pg1", "operator": "Lt. Ruiz"})
    assert cad.comms.link_pages(old, ["pg1"]) == 1                            # manual removal: just gone (not excluded)
    cad.unlink_traffic(old, {"traffic_id": "pg1", "operator": "Lt. Ruiz"})     # an auto (page) link removed: remembered
    assert cad.comms.link_pages(old, ["pg1"]) == 0 and cad.full(old)["radio"] == []
    assert cad.comms.link_pages(old, ["no-such-page"]) == 0


def test_a_viewer_sees_the_page_on_the_event(tmp_path):
    from tests.test_roles import VAL, people
    api = CadApi(make_cad(tmp_path))
    people(api.cad)
    iid = api.cad.store.create_incident(type="", address="1 Main St", source="60 Control page", source_ref="monitor:x",
                                        status="queued")
    api.cad.record_traffic({"id": "pg9", "start": time.time(), "end": time.time() + 9, "source": "South 2", "talkgroup": None,
                            "unit": "60 Control", "text": "", "status": "pending", "voice_at": 4.0,
                            "tones": [{"a_hz": 741.3, "b_hz": 623.7, "names": []}]})
    api.cad.comms.link_pages(iid, ["pg9"])
    code, raw, _ = api.get("/api/cad/state", "", Identity(email=VAL))
    assert code == 200
    inc = next(i for i in json.loads(raw)["incidents"] if i["id"] == iid)
    assert [(r["id"], r["why"]) for r in inc["radio"]] == [("pg9", "page")]


def test_unknown_pairs_are_logged_once_per_page_with_time_and_channel(tmp_path):
    from sizeup.radio.tones import ToneCode
    c = cfg(tmp_path)
    app = App(replace(c, tones=c.tones + (ToneCode("Scarsdale EMS (heard)", 928.1, 810.2),)))
    app._kind = {"South 2": "paging"}
    app.stt.client = FakeWhisper({k: "Sixty Control, mutual aid to Yonkers" for k in range(1, 30)})
    app.stt.start()
    pid = send_page(app, page_audio((928.1, 810.2), (741.3, 623.7), (1700.0, 1100.0)))
    log = tmp_path / "unknown_tones.jsonl"
    assert wait(lambda: log.is_file())
    rows = [json.loads(x) for x in log.read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["page"] == pid and rows[0]["channel"] == "South 2" and rows[0]["time"]
    assert [[round(a), round(b)] for a, b in rows[0]["pairs"]] == [[741, 624], [1700, 1100]]
    assert rows[0]["named"] == ["Scarsdale · EMS (heard)"]                    # who it was stacked with
    send_page(app, page_audio((349.1, 311.1)), at=time.time() + 60)            # a known page: no line
    time.sleep(0.2)
    assert len(log.read_text().splitlines()) == 1
