"""An intel push's real outcome at ntfy (sizeup/notify.py Notifier.send's done -> sizeup/intel/events.py delivery): a fake
urlopen, never the network; stand-in topics only."""

from __future__ import annotations

import io
import json
from urllib.error import HTTPError

from sizeup import context_bundle, notify
from sizeup.intel import events as events_mod
from sizeup.intel.events import SqlEvents
from tests.test_cad import make_cad
from tests.test_intel import ctx_row, example_locate

TOPIC = "pcfd-xmpl-intel-thread"
SERVER = "https://ntfy.example"


class Resp:
    def __init__(self, body: dict):
        self.raw = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n=-1):
        return self.raw


def fake_urlopen(answers):
    """Each call takes the next answer: a dict (200 with that JSON) or an HTTP status (refused)."""
    calls = []

    def urlopen(req, timeout):
        calls.append(json.loads(req.data))
        a = answers.pop(0)
        if isinstance(a, int):
            raise HTTPError(req.full_url, a, "no", {}, io.BytesIO(json.dumps({"error": "nope"}).encode()))
        return Resp(a)
    return urlopen, calls


def notifier(monkeypatch, answers):
    urlopen, calls = fake_urlopen(answers)
    monkeypatch.setattr(notify, "urlopen", urlopen)
    waits = []
    return notify.Notifier(SERVER, TOPIC, log=lambda m: None, sleep=waits.append), calls, waits


def test_a_sent_push_reports_ntfys_id_and_status(monkeypatch):
    n, calls, _w = notifier(monkeypatch, [{"id": "AbC123", "time": 1}, {"id": "x"}])
    got = []
    assert n.send("t", "b", done=got.append) and n.flush(5)
    [d] = got
    assert d["ok"] is True and d["id"] == "AbC123" and d["status"] == 200 and d["at"] > 0
    assert n.send("plain", "no callback") and n.flush(5) and n.sent == 2      # other kinds: as before


def test_a_refused_push_reports_the_error_once_without_retrying(monkeypatch):
    n, calls, waits = notifier(monkeypatch, [400])
    got = []
    assert n.send("t", "b", done=got.append) and n.flush(5)
    [d] = got
    assert d["ok"] is False and d["status"] == 400 and "HTTP 400" in d["error"] and TOPIC not in d["error"]
    assert waits == [] and len(calls) == 1


def test_retries_then_success_report_the_final_outcome(monkeypatch):
    n, calls, waits = notifier(monkeypatch, [429, 503, {"id": "late1"}])
    got = []
    assert n.send("t", "b", done=got.append) and n.flush(5)
    assert got == [{"ok": True, "at": got[0]["at"], "status": 200, "id": "late1"}] and len(calls) == 3 and len(waits) == 2


def test_a_broken_callback_never_stops_the_notifier(monkeypatch):
    n, _calls, _w = notifier(monkeypatch, [{"id": "a"}, {"id": "b"}])
    assert n.send("t", "b", done=lambda d: 1 / 0) and n.send("t2", "b2") and n.flush(5) and n.sent == 2


def pushed_event(tmp_path, monkeypatch, answers):
    urlopen, calls = fake_urlopen(answers)
    monkeypatch.setattr(notify, "urlopen", urlopen)
    cad = make_cad(tmp_path)
    cad.comms.context_broadcast = True          # as on the hub behind Access: every reader signed in
    p = notify.Pushes(SERVER, "pcfd-xmpl-alerts", "", intel_topic=TOPIC, log=lambda m: None, sleep=lambda s: None)
    cad.pushes = p
    cad.intel.notifier, cad.intel.locate = p.notifier(notify.INTEL), example_locate
    cad.comms.record(ctx_row("c1-t0", "shots fired at Sample St and Test Rd", cad.clock()))
    cad.intel.consider("c1-t0")
    cad.intel.drain()
    assert cad.intel.notifier.flush(5)
    [ev] = cad.intel.events.open_events()
    assert ev.pushed_at and len(calls) >= 1
    return cad, ev


def test_the_event_records_ntfys_answer_and_the_pages_show_it(tmp_path, monkeypatch):
    cad, ev = pushed_event(tmp_path, monkeypatch, [{"id": "Msg42"}])
    [row] = cad.intel.events.rows("id = ?", (ev.id,), 1)
    d = row["delivery"]
    assert d["ok"] and d["id"] == "Msg42" and d["status"] == 200 and d["at"] >= ev.pushed_at
    assert cad.intel_admin.shown(row)["delivery"] == d
    b = context_bundle.build(cad, "intel", ev.id, admin=True)
    assert b["outcome"]["delivery"] == d
    cad.intel.events.save(ev)                             # the engine saving the event later keeps it
    assert cad.intel.events.rows("id = ?", (ev.id,), 1)[0]["delivery"] == d


def test_a_refused_push_is_recorded_on_the_event(tmp_path, monkeypatch):
    cad, ev = pushed_event(tmp_path, monkeypatch, [429, 429, 429, 429])
    d = cad.intel.events.rows("id = ?", (ev.id,), 1)[0]["delivery"]
    assert d["ok"] is False and d["status"] == 429 and "HTTP 429" in d["error"]


def test_an_older_table_gets_the_column(tmp_path):
    cad = make_cad(tmp_path)
    db = cad.store.db
    db.execute("DROP TABLE intel_events")
    old = events_mod.SCHEMA.replace("dismissed_by TEXT NOT NULL DEFAULT '',", "dismissed_by TEXT NOT NULL DEFAULT ''")
    old = "\n".join(x for x in old.splitlines() if not x.strip().startswith("delivery TEXT"))
    db.executescript(old)
    assert "delivery" not in {r[1] for r in db.execute("PRAGMA table_info(intel_events)")}
    SqlEvents(cad.store)
    cols = {r[1] for r in db.execute("PRAGMA table_info(intel_events)")}
    assert "delivery" in cols
    SqlEvents(cad.store)                                  # again: nothing to do


def test_no_url_or_topic_in_what_is_stored(tmp_path, monkeypatch):
    cad, ev = pushed_event(tmp_path, monkeypatch, [{"id": "Msg42"}])
    raw = cad.store.one("SELECT delivery FROM intel_events WHERE id = ?", (ev.id,))["delivery"]
    assert TOPIC not in raw and "ntfy.example" not in raw
