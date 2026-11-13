"""The hub's own error spikes to the status topic (#285, sizeup/errwatch.py), from synthetic log lines."""

import io
import json
from datetime import datetime

import pytest

from sizeup import errwatch, tz
from sizeup.errwatch import ErrorWatch, LineTee, classify
from tests.test_push_settings import Rig
from tests.test_roles import COREY, call

T0 = datetime(2026, 11, 7, 16, 13, 0, tzinfo=tz.TZ).timestamp()
OVERFLOW = "aircraft: adsb feed: OverflowError: cannot convert float infinity to integer (at aircraft.py:{n}; logged once)"
BLOCKING = "aws transcribe South 2: Connection: BlockingIOError: [Errno 35] Resource temporarily unavailable"


class FakeNotifier:
    def __init__(self, enabled=True):
        self.enabled, self.got = enabled, []

    def send(self, title, body, priority=3, tags=(), click="", who=None):
        self.got.append({"title": title, "body": body, "priority": priority, "tags": tags, "click": click})
        return True


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def watch(settings=None, enabled=True):
    clock, n, logs = Clock(), FakeNotifier(enabled), []
    w = ErrorWatch(n, settings=(lambda: settings or {}), clock=clock, log=logs.append, click="https://hub.example/cad")
    return w, clock, n, logs


def stamp(t, line):
    return f"{tz.hhmmss(t)} {line}"


def run(w, clock, minutes, per_min, line, start=None):
    """per_min lines a minute for minutes, ticking every 30 s like the hub."""
    for _ in range(int(minutes * 2)):
        for i in range(per_min // 2):
            w.feed(stamp(clock.t, line.format(n=i)))
        clock.t += 30
        w.tick()


# -- what is an error line, and its signature ----------------------------------------------------------------------

@pytest.mark.parametrize("line,sig", [
    ("16:13:02 " + OVERFLOW.format(n=221), ("aircraft", "OverflowError",
                                            "cannot convert float infinity to integer (at aircraft.py:; logged once)")),
    ("16:13:02 " + BLOCKING, ("aws transcribe", "BlockingIOError", "[Errno ] Resource temporarily unavailable")),
    ("16:13:02 hub: cleanup error: KeyError: 'x'", ("hub", "KeyError", "'x'")),
    ("16:13:02 transcribe: ERROR stream 12 broke", ("transcribe", "error", "ERROR stream broke")),
    ("16:13:02 weather: cache w.json not read (FileNotFoundError); fetching afresh",
     ("weather", "FileNotFoundError", "cache w.json not read (FileNotFoundError); fetching afresh")),
    ("OverflowError: int too large", ("hub", "OverflowError", "int too large")),
])
def test_error_lines_and_their_signatures(line, sig):
    assert classify(line) == sig


@pytest.mark.parametrize("line", [
    "16:13:02 aircraft: 12 aircraft, 3 with a position",
    "16:13:02 http GET /api/cad/incident/12 500 ip=1.2.3.4 a@b.c ray=x ua=Mozilla",     # the access log: requests
    "16:13:02 errors: Hub errors: aircraft OverflowError ×11 in 1 min · since 16:13",   # our own notice
    "16:13:02 notify: not sent (can't reach ntfy.sh: timed out): Hub errors",
    "16:13:02 atc: WARNING the pre-alert rules are old",
    "",
])
def test_not_error_lines(line):
    assert classify(line) is None


def test_digits_are_stripped_so_one_fault_is_one_signature():
    w, *_ = watch()
    for n in range(50):
        w.feed(stamp(T0, OVERFLOW.format(n=n * 37)))
        w.feed(stamp(T0, f"hub: KeyError: 'E{n}' at 0x{n:08x}"))
    w.drain()
    assert len(w.sigs) == 2 and w.total == 100
    assert w.sigs[("hub", "KeyError")].sample == "'E' at x"


def test_a_traceback_is_one_error_named_by_its_module_and_type():
    w, clock, *_ = watch()
    out, err = LineTee(io.StringIO(), w, "stdout"), LineTee(io.StringIO(), w, "stderr")
    err.write("Exception in thread aircraft-poll:\nTraceback (most recent call last):\n")
    err.write('  File "/usr/lib/python3.12/threading.py", line 1075, in _bootstrap_inner\n    self.run()\n')
    out.write("16:13:02 aircraft: 12 aircraft\n")                        # stdout goes on meanwhile
    err.write('  File "/app/sizeup/aircraft.py", line 221, in _num\n    return int(v)\n           ^^^^^^\n')
    err.write("OverflowError: cannot convert float infinity to integer\n")
    w.drain()
    assert w.sigs == {}                                                  # its last line waits for a chained one
    clock.t += errwatch.PENDING_S
    w.drain()
    assert [(s.key, s.total, s.sample) for s in w.sigs.values()] == [
        (("aircraft", "OverflowError"), 1, "cannot convert float infinity to integer")]
    err.write("Traceback (most recent call last):\n"
              '  File "/app/sizeup/cloud/hub.py", line 9, in apply\n    x()\n'
              "sizeup.cad.store.CadError: no such incident 26-000015\n"
              "16:13:03 hub: next line\n")                              # the next line: counted at once
    w.drain()
    assert w.sigs[("hub", "CadError")].total == 1


def test_the_tee_writes_everything_through_and_feeds_whole_lines_only():
    w, *_ = watch()
    sink = io.StringIO()
    t = LineTee(sink, w, "stdout")
    t.write("16:13:02 aircraft: Overflow")
    w.drain()
    assert w.total == 0
    t.write("Error: x\n16:13:03 aircraft: ok\n16:13:04 hub: ValueError: y")
    w.drain()
    assert w.total == 1
    t.write("\n")
    w.drain()
    assert w.total == 2 and sink.getvalue().count("\n") == 3 and t.getvalue() == sink.getvalue()


# -- when it pushes ------------------------------------------------------------------------------------------------

def test_the_overflow_spike_one_notice_an_hour_then_back_to_normal():
    w, clock, n, logs = watch()
    run(w, clock, 25, 60, OVERFLOW)                                     # ~1,500 in 25 min, as on the day
    assert len(n.got) == 1
    first = n.got[0]
    assert first["title"].startswith("Hub errors: aircraft OverflowError ×") and first["title"].endswith("· since 16:13")
    assert first["priority"] == 4 and first["click"] == "https://hub.example/cad" and "Settings > Station > Health" in first["body"]
    assert any(m.startswith("errors: Hub errors: aircraft OverflowError") for m in logs)
    p = w.panel()
    assert p["top"][0]["source"] == "aircraft" and p["top"][0]["count"] == 1500 and p["alerting"]
    run(w, clock, 14, 0, OVERFLOW)                                      # quiet, not yet 15 min
    assert len(n.got) == 1
    run(w, clock, 2, 0, OVERFLOW)
    assert len(n.got) == 2
    back = n.got[1]
    assert back["title"] == "Hub errors back to normal: aircraft OverflowError" and back["priority"] == 3
    assert "×1,500 in 24 min, last 16:37" in back["body"] and not w.panel()["alerting"]
    run(w, clock, 5, 60, OVERFLOW)                                      # back within the hour of its notice: silent
    assert len(n.got) == 2
    run(w, clock, 20, 60, OVERFLOW)                                     # an hour on, still failing: again
    assert len(n.got) == 3 and n.got[2]["title"].startswith("Hub errors: aircraft OverflowError ×")


def test_the_spike_notice_says_the_count_the_span_and_since():
    w, clock, n, _ = watch({"per_signature": 10000, "total": 50000})
    run(w, clock, 25, 60, OVERFLOW)
    assert n.got == []
    w.sigs[next(iter(w.sigs))].count = 1480                                # the day's count
    w.settings = lambda: {"total": 50000}
    w.tick()
    assert n.got[0]["title"] == "Hub errors: aircraft OverflowError ×1,480 in 25 min · since 16:13"


def test_one_off_and_a_few_errors_never_push():
    w, clock, n, _ = watch()
    for i in range(10):                                                    # K = 10: "more than" ten in 5 min
        w.feed(stamp(clock.t, BLOCKING))
        w.feed(stamp(clock.t, f"hub: KeyError: 'u{i}'"))
        clock.t += 25
        w.tick()
    for _ in range(60):                                                    # one a minute for an hour: 5 in any 5 min
        w.feed(stamp(clock.t, OVERFLOW.format(n=1)))
        clock.t += 60
        w.tick()
    assert n.got == [] and w.total == 20 + 60


def test_many_small_errors_over_the_total_push_once():
    w, clock, n, _ = watch({"total": 50})
    mods = sorted(m for m in errwatch.MODULES if m not in errwatch.IGNORE)[:30]
    for i in range(60):                                                    # 60 different errors, each one-off
        w.feed(stamp(clock.t, f"{mods[i // 2]}: {('ValueError', 'KeyError')[i % 2]}: bad"))
    w.tick()
    w.tick()
    assert len(n.got) == 1 and n.got[0]["title"].startswith("Hub errors: 60 in 5 min · top ")
    clock.t += errwatch.QUIET_S + 1
    w.tick()
    assert n.got[1]["title"] == "Hub errors back to normal"


def test_a_second_signature_gets_its_own_notice():
    w, clock, n, _ = watch({"total": 100000})
    run(w, clock, 3, 20, OVERFLOW)
    run(w, clock, 3, 20, BLOCKING)
    assert [g["title"].split(" ×")[0] for g in n.got] == ["Hub errors: aircraft OverflowError",
                                                          "Hub errors: aws transcribe BlockingIOError"]


def test_off_in_settings_counts_but_never_pushes_and_no_topic_is_the_same():
    for settings, enabled, why in (({"on": False}, True, "off in Settings"), ({}, False, "no status topic")):
        w, clock, n, logs = watch(settings, enabled)
        run(w, clock, 6, 60, OVERFLOW)
        assert n.got == [] and w.panel()["last5"] == 300 and w.panel()["on"] is (settings == {})
        assert any(m.endswith(f"(not pushed: {why})") for m in logs)


def test_the_notice_carries_no_message_text_only_the_module_and_type():
    w, clock, n, _ = watch()
    secret = "cad: CadError: caller 914-555-0100 at 100 S Ridge St, incident 26-000015, token=sk-abc123"
    run(w, clock, 20, 60, secret)
    shown = json.dumps(n.got)
    assert n.got and "CadError" in shown
    for bit in ("914", "Ridge", "26-000015", "sk-abc", "token", "caller", "incident"):
        assert bit not in shown


def test_settings_bad_values_fall_back_to_the_defaults():
    w, *_ = watch({"on": "yes", "per_signature": 1, "total": True})
    assert w.config() == errwatch.DEFAULTS


# -- Settings > System > Hub error alerts ----------------------------------------------------------------------------

def test_settings_set_reset_audit_and_the_watch_reads_them_live(tmp_path):
    rig = Rig(tmp_path)
    w = ErrorWatch(FakeNotifier(), settings=lambda: rig.cad.settings.section(errwatch.SECTION))
    page = rig.cad.settings_get()["hub_errors"]
    assert {k: page[k]["value"] for k in errwatch.DEFAULTS} == errwatch.DEFAULTS
    post = lambda body: call(rig.api, "/api/cad/settings/hub_errors", body, COREY)       # noqa: E731
    code, out = post({"action": "set", "key": "per_signature", "value": 25})
    assert code == 200 and out["hub_errors"]["per_signature"] == {**page["per_signature"], "value": 25, "edited": True}
    assert post({"action": "set", "key": "on", "value": False})[0] == 200
    assert w.config() == {"on": False, "per_signature": 25, "total": 100}
    for body in ({"action": "set", "key": "total", "value": 1}, {"action": "set", "key": "total", "value": 2.5},
                 {"action": "set", "key": "on", "value": 1}, {"action": "set", "key": "nope", "value": 1}):
        assert post(body)[0] == 400
    assert post({"action": "reset", "key": "per_signature"})[0] == 200
    assert w.config() == {"on": False, "per_signature": 10, "total": 100}
    audit = rig.cad.settings_audit_log({"section": ["system"]})["audit"]
    assert [(a["key"], a["old"], a["new"]) for a in reversed(audit) if a["key"].startswith("hub_errors/")] == [
        ("hub_errors/per_signature", "10", "25"), ("hub_errors/on", "on", "off"), ("hub_errors/per_signature", "25", "10")]
    ex = rig.cad.settings_export()
    assert ex["overrides"]["hub_errors"] == {"on": False}
    ex["overrides"]["hub_errors"] = {"total": 0}
    code, out = call(rig.api, "/api/cad/settings/import", {"action": "apply", "settings": ex}, COREY)
    assert code == 400 and "hub_errors" in out["error"]


def test_the_hub_wires_it_to_the_status_notifier_and_settings_health(tmp_path):
    from sizeup.cloud.hub import HubApp
    from tests.test_cloud import hub_cfg
    app = HubApp(hub_cfg(tmp_path), log=lambda m: None)
    try:
        assert app.health.errors is app.error_watch
        assert app.error_watch.notifier is app.pushes.notifier("status")
        app.error_watch.feed(stamp(T0, OVERFLOW.format(n=1)))
        e = app.health.panel()["errors"]
        assert e["total"] == 1 and e["top"][0]["type"] == "OverflowError" and e["per_signature"] == 10
    finally:
        app.stop()


# -- what a notice may say: fixed lists only (the status topic is public) --------------------------------------------

PATH = "/Jane_Doe_at_12_Elm_St_CallerError"


@pytest.mark.parametrize("line", [
    f"12:00:01 http GET {PATH} 404 ip=1.2.3.4 no-token ray=x ua=curl/8",
    "12:00:01 http GET /x 404 ip=1.2.3.4 no-token ray=x ua=Mozilla/5.0 (FooError; Jane Doe 12 Elm St)",
    f"http: GET {PATH} 404",
])
def test_the_access_log_is_never_counted_whatever_its_path_or_user_agent(line):
    assert classify(line) is None
    w, clock, n, _ = watch()
    for _ in range(50):
        w.feed(line)
    w.tick()
    assert w.total == 0 and n.got == []


@pytest.mark.parametrize("line,sig", [
    ("smith john: OverflowError: x", ("hub", "OverflowError")),
    ("caller jane: KeyError: x", ("hub", "KeyError")),
    ("elm: ValueError: x", ("hub", "ValueError")),
    ("aircraft: Jane_DoeError: x", ("aircraft", "error")),               # not an exception we know: "error"
    (f"hub: GET {PATH} failed: error: 404", ("hub", "error")),
    ("aws transcribe Jane Doe: Connection: BlockingIOError: x", ("aws transcribe", "BlockingIOError")),
    ("CAD: settings: CadError: x", ("cad", "CadError")),                  # sizeup's own exception
])
def test_source_and_type_come_only_from_fixed_lists(line, sig):
    assert classify(line)[:2] == sig


def test_a_notice_from_odd_lines_names_nothing_from_them():
    w, clock, n, _ = watch()
    run(w, clock, 20, 60, "jane doe 12 elm st: Jane_Doe_Elm_StError: caller x")
    run(w, clock, 20, 60, f"http GET {PATH}Error 500 ua=JaneError")
    assert n.got and all(g["title"].startswith("Hub errors: hub error ×") or g["title"].startswith("Hub errors back")
                         for g in n.got)
    shown = json.dumps(n.got).lower()
    for bit in ("jane", "doe", "elm", "caller"):
        assert bit not in shown


def test_a_traceback_names_only_a_sizeup_module_and_a_real_class():
    w, clock, *_ = watch()
    err = LineTee(io.StringIO(), w, "stderr")
    for last in ("Jane_DoeError: x", "botocore.exceptions.ClientError: An error occurred",
                 "sizeup.cloud.hub.HubPostError: x", "thirdparty.Jane_DoeError: x", "sizeup.notify.NotifyError: x"):
        err.write("Traceback (most recent call last):\n"
                  '  File "/app/sizeup/jane_doe.py", line 1, in f\n    x()\n'
                  '  File "/app/sizeup/aircraft.py", line 2, in g\n    y()\n'
                  f"{last}\n\n")
        clock.t += 5
        w.drain()
    clock.t += 5
    w.drain()
    assert sorted((k, s.total) for k, s in w.sigs.items()) == [
        (("aircraft", "ClientError"), 1), (("aircraft", "NotifyError"), 1), (("aircraft", "error"), 3)]           # no sizeup.cloud.hub.HubPostError


def test_a_chained_traceback_is_one_error():
    w, clock, *_ = watch()
    err = LineTee(io.StringIO(), w, "stderr")
    err.write("Traceback (most recent call last):\n"
              '  File "/app/sizeup/aircraft.py", line 2, in g\n    y()\n'
              "KeyError: 'x'\n\nDuring handling of the above exception, another exception occurred:\n\n"
              "Traceback (most recent call last):\n"
              '  File "/app/sizeup/aircraft.py", line 5, in g\n    z()\n'
              "OverflowError: too big\n")
    w.drain()
    clock.t += 5
    w.drain()
    assert {k: s.total for k, s in w.sigs.items()} == {("aircraft", "OverflowError"): 1}


# -- the log never waits on the watcher -----------------------------------------------------------------------------

def _no_deadlock(fn, timeout=5.0):
    import threading
    t = threading.Thread(target=fn, daemon=True)
    t.start()
    t.join(timeout)
    assert not t.is_alive(), "deadlocked"


def test_a_write_from_inside_a_write_or_under_the_watchers_lock_never_deadlocks():
    w, *_ = watch()

    class Reentrant(io.StringIO):           # a finalizer / the unraisable hook writing to stderr mid-write
        def write(self, s):
            if not getattr(self, "inside", False):
                self.inside = True
                tee.write("12:00:00 hub: ValueError: from a finalizer\n")
                self.inside = False
            return super().write(s)

    sink = Reentrant()
    tee = LineTee(sink, w, "stderr")

    def go():
        tee.write("12:00:00 hub: KeyError: x\n")
        with w._lock, w._drain_lock, tee._lock:      # whatever the watcher holds, a write goes through
            tee.write("12:00:01 hub: KeyError: y\n")
    _no_deadlock(go)
    assert sink.getvalue().count("\n") == 4
    w.drain()
    assert w.sigs[("hub", "KeyError")].total == 2 and w.sigs[("hub", "ValueError")].total == 2


def test_install_with_many_threads_reads_every_line_whole(monkeypatch):
    import threading
    import sys
    w, clock, *_ = watch()
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    errwatch.install(w)
    assert isinstance(sys.stdout, LineTee) and isinstance(sys.stderr, LineTee)

    def worker(i):
        for c in range(200):
            print(f"12:00:00 http GET /x/{c} 200 ua=FooError")
            print(f"12:00:00 aircraft: OverflowError: {i} {c}")      # print: the text, then the newline
            if c % 50 == 0:
                print(f"12:00:00 hub: KeyError: {c}", file=sys.stderr)
    ts = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    _no_deadlock(lambda: ([t.start() for t in ts], [t.join() for t in ts]), timeout=30)
    errwatch.install(w)                                              # again: the same tees, not one on another
    assert not isinstance(sys.stdout._stream, LineTee)
    monkeypatch.setattr(sys, "stdout", None)
    errwatch.install(w)                                              # no console: left alone
    w.drain()
    assert {k: s.total for k, s in w.sigs.items()} == {("aircraft", "OverflowError"): 4000, ("hub", "KeyError"): 80}
    assert out.getvalue().count("\n") == 8000


# -- back to normal, kept across a redeploy, the settings ------------------------------------------------------------

def test_a_trickle_under_k_is_back_to_normal():
    w, clock, n, _ = watch()
    for _ in range(30):
        w.feed(stamp(clock.t, OVERFLOW.format(n=1)))
        clock.t += 5
    w.tick()
    for _ in range(36):                                     # then one every 10 min for 6 h: under K
        clock.t += 600
        w.feed(stamp(clock.t, OVERFLOW.format(n=1)))
        w.tick()
    assert [g["title"].split(" ×")[0] for g in n.got] == ["Hub errors: aircraft OverflowError",
                                                          "Hub errors back to normal: aircraft OverflowError"]
    assert not w.panel()["alerting"]


def test_a_trickle_of_one_doesnt_hold_back_the_others():
    w, clock, n, _ = watch({"total": 100000})
    run(w, clock, 2, 60, OVERFLOW)
    run(w, clock, 2, 60, BLOCKING)
    for _ in range(40):                                     # BlockingIOError trickles on, under K
        w.feed(stamp(clock.t, BLOCKING))
        clock.t += 60
        w.tick()
    assert sum(g["title"].startswith("Hub errors back to normal") for g in n.got) == 1


class FakeDb:
    def __init__(self):
        self.d = {}

    def get(self, key, default=""):
        return self.d.get(key, default)

    def put(self, key, value):
        self.d[key] = value


def test_a_redeploy_neither_pushes_a_running_fault_again_nor_forgets_to_say_back_to_normal():
    db, clock, n = FakeDb(), Clock(), FakeNotifier()
    w = ErrorWatch(n, clock=clock, log=lambda m: None, db=db)
    run(w, clock, 5, 60, OVERFLOW)
    assert len(n.got) == 1 and "errwatch" in db.d
    w2 = ErrorWatch(n, clock=clock, log=lambda m: None, db=db)            # the new task, the fault still going
    run(w2, clock, 20, 60, OVERFLOW)
    assert len(n.got) == 1
    run(w2, clock, 16, 0, OVERFLOW)
    assert n.got[-1]["title"] == "Hub errors back to normal: aircraft OverflowError"
    db.d["errwatch"] = '{"sigs": [{"source": "jane doe", "type": "JaneError", "alerting": true}]}'
    assert ErrorWatch(n, clock=clock, log=lambda m: None, db=db).sigs == {}     # nothing off the lists, even saved


def test_unreadable_settings_keep_what_they_last_were():
    state = {"ov": {"on": False, "per_signature": 50}}

    def settings():
        if state["ov"] is None:
            raise OSError("db locked")
        return state["ov"]
    w = ErrorWatch(FakeNotifier(), settings=settings)
    assert w.config() == {"on": False, "per_signature": 50, "total": 100}
    state["ov"] = None
    assert w.config() == {"on": False, "per_signature": 50, "total": 100}


def test_the_windows_hold_more_than_the_largest_thresholds():
    assert errwatch.MAX_ALL > errwatch.LIMITS["total"][1] and errwatch.MAX_TIMES > errwatch.LIMITS["per_signature"][1]


def test_a_flood_of_one_error_past_its_window_cap_is_still_one_notice():
    w, clock, n, _ = watch({"total": 50})
    for _ in range(errwatch.MAX_TIMES + 5000):
        w.feed(stamp(clock.t, OVERFLOW.format(n=1)))
    w.tick()
    w.tick()
    assert [g["title"].split(" ×")[0] for g in n.got] == ["Hub errors: aircraft OverflowError"]


def test_the_tee_never_queues_the_access_log_or_our_own_lines():
    w, *_ = watch()
    t = LineTee(io.StringIO(), w, "stdout")
    t.write(f"12:00:01 http GET {PATH} 404 ua=FooError\nerrors: Hub errors: x\nnotify: not sent (KeyError)\n")
    assert w._q.qsize() == 0
    t.write("12:00:01 hub: KeyError: x\n")
    assert w._q.qsize() == 1


def test_a_health_request_reads_only_a_little_of_the_queue():
    w, *_ = watch()
    for _ in range(errwatch.PANEL_DRAIN + 500):
        w.feed(OVERFLOW.format(n=1))
    assert w.panel()["total"] == errwatch.PANEL_DRAIN and w._q.qsize() == 500
    w.drain()
    assert w.total == errwatch.PANEL_DRAIN + 500


def test_the_hubs_startup_path_tees_its_log(tmp_path, monkeypatch):
    import sys
    from sizeup.app import start_app
    from tests.test_cloud import hub_cfg
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    app = start_app(hub_cfg(tmp_path))
    try:
        assert isinstance(sys.stdout, LineTee) and isinstance(sys.stderr, LineTee)
        assert sys.stdout._watch is app.error_watch
    finally:
        app.stop()
