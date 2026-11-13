"""sizeup/notify.py: pushes through ntfy, against a local fake ntfy; never Tapout's topics."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from sizeup import notify
from sizeup.notify import Notifier, NotifyError, Who, compose, publish, topic_problem, who_line

TOPIC = "sizeup-health-test-6f1c2a9e"


class FakeNtfy:
    """ntfy's JSON publish on this machine: records each message; can refuse or fail."""

    def __init__(self):
        self.got, self.status = [], 200
        me = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                me.got.append({"path": self.path, "body": body, "ctype": self.headers.get("Content-Type")})
                out = json.dumps({"id": f"m{len(me.got)}", "time": 1} if me.status == 200 else {"error": "nope"}).encode()
                self.send_response(me.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def ntfy():
    f = FakeNtfy()
    yield f
    f.close()


def test_publish_sends_ntfys_json_with_title_priority_tags_and_click(ntfy):
    out = publish(ntfy.url, TOPIC, "Station silent", "no word since 14:32 · ünïcode ok", priority="high",
                  tags=("warning", "sizeup"), click="https://hub.example/cad#settings/system/health")
    assert out["id"] == "m1"
    (m,) = ntfy.got
    assert m["path"] == "/" and m["ctype"] == "application/json"
    assert m["body"] == {"topic": TOPIC, "title": "Station silent", "message": "no word since 14:32 · ünïcode ok",
                         "priority": 4, "tags": ["warning", "sizeup"], "click": "https://hub.example/cad#settings/system/health"}


def test_who_goes_into_the_title_the_same_way_for_every_caller(ntfy):
    mayday = Who(units=(("E64 P1", "Engine 64"),), channel="FG1", incident="26-000015", address="100 S Ridge St")
    assert who_line(mayday) == "E64 P1 (Engine 64) on FG1 · 26-000015 100 S Ridge St"
    assert compose("MAYDAY", mayday) == "MAYDAY · E64 P1 (Engine 64) on FG1 · 26-000015 100 S Ridge St"
    assert compose("Station silent 4 min", {"station": "Port Chester FD", "detail": "last heard 14:32"}) == \
        "Station silent 4 min · Port Chester FD · last heard 14:32"
    assert compose("FG1 not running", Who(dongle="SDR 00000102", channel="FG1")) == "FG1 not running · SDR 00000102 on FG1"
    assert compose("Talker", {"talker": "1234 (Chief)", "channel": "Fire/EMS 15"}) == "Talker · 1234 (Chief) on Fire/EMS 15"
    assert compose("Plain", None) == "Plain" and who_line({}) == ""
    assert Who.of({"units": [["E64", "Engine 64"], "L35"], "channel": "FG1", "ignored": 1}).units == (("E64", "Engine 64"), ("L35",))
    publish(ntfy.url, TOPIC, "MAYDAY", "Emergency button", priority=5, who=mayday)
    assert ntfy.got[0]["body"]["title"] == "MAYDAY · E64 P1 (Engine 64) on FG1 · 26-000015 100 S Ridge St"


@pytest.mark.parametrize("topic", ["pcfd-phone-prealert", "pcfd-status", "PCFD-Phone-Prealert", " pcfd-status "])
def test_tapouts_topics_are_refused_before_any_request(ntfy, topic):
    assert "Tapout" in topic_problem(topic)
    with pytest.raises(NotifyError, match="Tapout"):
        publish(ntfy.url, topic, "x", "y")
    n = Notifier(ntfy.url, topic, log=lambda m: None)
    assert not n.enabled and not n.send("x", "y")
    assert ntfy.got == []


def test_bad_topics_and_servers_are_refused():
    assert topic_problem("short") and topic_problem("has space in it") and topic_problem("a/b-path-like-topic")
    assert topic_problem("my-own-topic", also_forbidden=["my-own-topic"])
    assert topic_problem(TOPIC) == ""
    with pytest.raises(NotifyError, match="https"):
        publish("http://ntfy.example.org", TOPIC, "x", "y")


def test_a_refusal_says_why_and_the_notifier_retries_only_what_may_pass(ntfy):
    ntfy.status = 400
    with pytest.raises(NotifyError, match="HTTP 400 \\(nope\\)"):
        publish(ntfy.url, TOPIC, "x", "y")
    logs, waits = [], []
    n = Notifier(ntfy.url, TOPIC, log=logs.append, sleep=waits.append)
    assert n.send("t", "b")
    assert n.flush(5)
    assert n.failed == 1 and waits == [] and len(ntfy.got) == 2       # a 400 isn't tried again
    ntfy.status = 503
    n.send("t2", "b")
    assert n.flush(5)
    assert waits == list(notify.RETRIES) and len(ntfy.got) == 2 + 1 + len(notify.RETRIES)
    assert any("not sent" in m for m in logs)


def test_the_notifier_sends_in_order_from_its_own_thread(ntfy):
    n = Notifier(ntfy.url, TOPIC, log=lambda m: None)
    for i in range(3):
        assert n.send(f"t{i}", f"b{i}", who={"channel": "FG1"})
    assert n.flush(5) and n.sent == 3
    assert [m["body"]["title"] for m in ntfy.got] == ["t0 · FG1", "t1 · FG1", "t2 · FG1"]
    off = Notifier(ntfy.url, "", log=lambda m: None)
    assert not off.enabled and not off.send("x", "y")


# -- two topics, by kind (route(), notifiers()) -------------------------------------------------------------------
# Stand-ins shaped like the hub's real topics (which live only in SSM: ntfy topics are readable by name, and this
# repository is public): "status" / "alerts" inside a longer name of our own.
OUR_ALERTS, OUR_STATUS = "pcfd-xmpl-alerts", "pcfd-xmpl-status"
OUR_INTEL = "pcfd-xmpl-intel-thread"


def test_each_kind_goes_to_its_own_topic_and_one_topic_takes_both():
    A, S, I = notify.ALERTS, notify.STATUS, notify.INTEL
    assert notify.route(OUR_ALERTS, OUR_STATUS) == {A: OUR_ALERTS, S: OUR_STATUS, I: OUR_ALERTS}
    assert notify.route(OUR_ALERTS, "") == {A: OUR_ALERTS, S: OUR_ALERTS, I: OUR_ALERTS}   # as before
    assert notify.route("", OUR_STATUS) == {A: OUR_STATUS, S: OUR_STATUS, I: OUR_STATUS}
    assert notify.route("", " ") == {A: "", S: "", I: ""}                                    # no pushes
    assert notify.route(OUR_ALERTS, OUR_STATUS, OUR_INTEL)[I] == OUR_INTEL


def test_the_startup_line_says_where_each_kind_goes_never_the_names():
    def line(a, st, src="ssm"):
        return notify.Pushes("https://ntfy.sh", a, st, sources={notify.ALERTS: src, notify.STATUS: src}).line()
    both = line(OUR_ALERTS, OUR_STATUS)
    intel = ", intel \u2192 the alerts topic (no intel topic set)"
    assert both == "notify: alerts \u2192 topic set (SSM), status \u2192 topic set (SSM)" + intel
    assert line(OUR_ALERTS, "", "config") == \
        "notify: alerts \u2192 topic set (config file), status \u2192 the alerts topic (no status topic set)" + intel
    assert line("", OUR_STATUS) == ("notify: alerts \u2192 the status topic (no alerts topic set), "
                                    "status \u2192 topic set (SSM)" + intel)
    assert line("", "").startswith("notify: no pushes")
    for out in (both, line(OUR_ALERTS, ""), line("", OUR_STATUS)):
        assert OUR_ALERTS not in out and OUR_STATUS not in out


def test_each_kind_has_its_notifier_and_one_topic_serving_both_shares_one_queue(ntfy):
    p = notify.Pushes(ntfy.url, OUR_ALERTS, OUR_STATUS, log=lambda m: None)
    a, s = p.notifier(notify.ALERTS), p.notifier(notify.STATUS)
    assert a.topic == OUR_ALERTS and s.topic == OUR_STATUS and a.current() is not s.current()
    a.send("MAYDAY", "Emergency button", priority=5)
    s.send("Station silent 4 min", "no word since 14:32")
    assert a.flush(5) and s.flush(5)
    assert sorted((m["body"]["topic"], m["body"]["title"]) for m in ntfy.got) == [
        (OUR_ALERTS, "MAYDAY"), (OUR_STATUS, "Station silent 4 min")]
    assert a.sent == 1 and s.sent == 1 and a.current().last_ok and a.current().last_at
    for only in ((OUR_ALERTS, ""), ("", OUR_STATUS)):
        one = notify.Pushes(ntfy.url, *only, log=lambda m: None)
        x, y = one.notifier(notify.ALERTS), one.notifier(notify.STATUS)
        assert x.current() is y.current() and x.topic == y.topic == (only[0] or only[1])
    none = notify.Pushes(ntfy.url, "", "", log=lambda m: None)
    assert not none.notifier(notify.ALERTS).enabled and not none.notifier(notify.STATUS).send("x", "y")


@pytest.mark.parametrize("ours", [OUR_STATUS, OUR_ALERTS, "pcfd-sizeup-health", "x-pcfd-status", "pcfd-status-2",
                                  "pcfd-phone-prealert-test", "my-firehouse-ring-xyz"])
def test_tapouts_topics_are_refused_by_exact_name_not_by_a_word_in_ours(ours):
    assert topic_problem(ours, ("firehouse-ring-xyz",)) == ""
    for theirs in ("pcfd-status", "pcfd-phone-prealert", "firehouse-ring-xyz"):
        assert "Tapout" in topic_problem(theirs, ("firehouse-ring-xyz",))


# -- the third kind: radio-intelligence pre-alerts (INTEL) ----------------------------------------------------------

def test_intel_falls_back_to_the_alerts_topic_and_never_the_other_way():
    assert notify.KINDS == (notify.ALERTS, notify.STATUS, notify.INTEL)
    assert notify.FALLBACK == {notify.STATUS: notify.ALERTS, notify.ALERTS: notify.STATUS, notify.INTEL: notify.ALERTS}
    assert notify.other(notify.INTEL) == notify.ALERTS and notify.other(notify.ALERTS) == notify.STATUS
    p = notify.Pushes("https://ntfy.sh", OUR_ALERTS, OUR_STATUS, log=lambda m: None)
    assert p.effective(notify.INTEL) == {"topic": OUR_ALERTS, "source": "fallback"}
    # only a status topic: the alerts share it, and intel follows the alerts there
    p = notify.Pushes("https://ntfy.sh", "", OUR_STATUS, log=lambda m: None)
    assert p.effective(notify.INTEL) == {"topic": OUR_STATUS, "source": "fallback"}
    # an intel topic of its own: the alerts never fall back to it
    p = notify.Pushes("https://ntfy.sh", "", "", intel_topic=OUR_INTEL, sources={notify.INTEL: "env"}, log=lambda m: None)
    assert p.effective(notify.INTEL) == {"topic": OUR_INTEL, "source": "env"}
    assert p.effective(notify.ALERTS)["topic"] == "" and p.effective(notify.STATUS)["topic"] == ""
    assert notify.Pushes("https://ntfy.sh", "", "", log=lambda m: None).effective(notify.INTEL)["topic"] == ""


def test_intel_follows_the_alerts_off_and_its_own_topic_splits_it_off(ntfy):
    ov = {}
    p = notify.Pushes(ntfy.url, OUR_ALERTS, OUR_STATUS, settings=lambda: ov, log=lambda m: None)
    n = p.notifier(notify.INTEL)
    n.send("PD \u00b7 Smoke reported \u00b7 Example Town", "Example Ave \u00b7 14:32")
    assert n.flush(5) and [m["body"]["topic"] for m in ntfy.got] == [OUR_ALERTS]
    ov[notify.INTEL] = {"topic": OUR_INTEL}                     # Settings: the "intel thread", no restart
    n.send("PD \u00b7 MVA \u00b7 Example Town", "Sample St")
    assert n.flush(5) and ntfy.got[-1]["body"]["topic"] == OUR_INTEL
    ov[notify.INTEL] = {"off": True}
    assert not n.enabled and not n.send("x", "y")
    del ov[notify.INTEL]
    ov[notify.ALERTS] = {"off": True}                           # the alerts off: intel with no topic of its own too
    assert p.effective(notify.INTEL) == {"topic": "", "source": "none"} and not n.enabled
    assert p.effective(notify.STATUS)["topic"] == OUR_STATUS


def test_intel_topic_from_the_config_and_env_refuses_tapouts(tmp_path, monkeypatch):
    from sizeup.config import ConfigError, load
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[alerts]\nntfy_topic = "{OUR_ALERTS}"\nintel_topic = "{OUR_INTEL}"\n')
    c = load(cfg)
    assert (c.alerts_intel_topic, c.alerts_intel_topic_source) == (OUR_INTEL, "config")
    monkeypatch.setenv("SIZEUP_INTEL_TOPIC", OUR_INTEL + "-env")
    c = load(cfg)
    assert (c.alerts_intel_topic, c.alerts_intel_topic_source) == (OUR_INTEL + "-env", "env")
    monkeypatch.delenv("SIZEUP_INTEL_TOPIC")
    for bad in ("pcfd-phone-prealert", "pcfd-status", "short"):
        cfg.write_text(f'[alerts]\nintel_topic = "{bad}"\n')
        with pytest.raises(ConfigError, match="intel_topic"):
            load(cfg)
