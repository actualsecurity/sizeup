"""Settings > System > Push notifications (sizeup/notify.py Pushes, sizeup/cad/settings.py _notify_op): the two ntfy
topics changeable from Settings, over env / SSM over the config file; validated like the config; live; audited; only
admins read or change them, and their names stay out of the audit log, the activity, the export and the logs."""
import json

import pytest

from sizeup import notify
from sizeup.cad.api import CadApi, needs
from sizeup.cloud.access import Identity
from sizeup.notify import ALERTS, STATUS, NotifyError, Pushes
from tests.test_cad import make_cad
from tests.test_roles import COREY, JULIAN, VAL, call, people

# Stand-ins shaped like the hub's real topics, which live only in SSM (this repository is public).
SSM_ALERTS, SSM_STATUS = "pcfd-xmpl-alerts", "pcfd-xmpl-status"
MINE = "pcfd-xmpl-status-from-settings"


class FakePublish:
    """publish() for the test push: records, or refuses like ntfy."""

    def __init__(self, error=""):
        self.got, self.error = [], error

    def __call__(self, server, topic, title, body, priority=3, tags=(), click="", timeout=10.0, who=None):
        if self.error:
            raise NotifyError(self.error)
        self.got.append({"server": server, "topic": topic, "title": title, "body": body, "priority": priority})
        return {"id": "m1"}


class Rig:
    def __init__(self, tmp_path, alerts=SSM_ALERTS, status=SSM_STATUS, source="ssm"):
        self.cad = make_cad(tmp_path)
        self.logs = []
        self.cad.log = self.logs.append
        self.api = CadApi(self.cad)
        self.ids = people(self.cad)
        self.p = Pushes("http://127.0.0.1:9", alerts, status, sources={ALERTS: source, STATUS: source},
                        settings=lambda: self.cad.settings.section(notify.SECTION),
                        also_forbidden=("firehouse-ring-xyz",), log=self.logs.append)
        self.p.publish = self.fake = FakePublish()
        self.cad.pushes = self.p

    def post(self, body, who=COREY):
        return call(self.api, "/api/cad/settings/notify", body, who)

    def page(self, who=COREY):
        code, raw, _ = self.api.get("/api/cad/notify", "", Identity(email=who))
        return code, json.loads(raw)

    def eff(self, kind):
        return self.p.effective(kind)


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def test_settings_win_over_ssm_which_wins_over_the_config_and_empty_means_the_default(tmp_path, monkeypatch, rig):
    from sizeup.config import load
    cfg = tmp_path / "config.toml"
    cfg.write_text('[alerts]\nntfy_topic = "pcfd-xmpl-alerts-file"\nstatus_topic = "pcfd-xmpl-status-file"\n')
    c = load(cfg)
    assert (c.alerts_ntfy_topic_source, c.alerts_status_topic_source) == ("config", "config")
    monkeypatch.setenv("SIZEUP_STATUS_TOPIC", SSM_STATUS)
    c = load(cfg)
    assert c.alerts_status_topic == SSM_STATUS and c.alerts_status_topic_source == "env"
    assert c.alerts_ntfy_topic == "pcfd-xmpl-alerts-file" and c.alerts_ntfy_topic_source == "config"
    # on the hub the env comes from SSM; Settings over that
    assert rig.eff(STATUS) == {"topic": SSM_STATUS, "source": "ssm"}
    assert rig.post({"action": "set", "key": "status", "topic": MINE})[0] == 200
    assert rig.eff(STATUS) == {"topic": MINE, "source": "settings"} and rig.eff(ALERTS)["source"] == "ssm"
    assert rig.post({"action": "set", "key": "status", "topic": ""})[0] == 200      # empty: back to the default
    assert rig.eff(STATUS) == {"topic": SSM_STATUS, "source": "ssm"}
    assert rig.post({"action": "set", "key": "status", "topic": MINE})[0] == 200
    assert rig.post({"action": "reset", "key": "status"})[0] == 200
    assert rig.eff(STATUS)["source"] == "ssm"


def test_turn_off_stops_that_kind_only_and_one_topic_still_serves_both(tmp_path):
    r = Rig(tmp_path, status="")
    assert r.eff(STATUS) == {"topic": SSM_ALERTS, "source": "fallback"}            # only one set: both go there
    assert r.post({"action": "off", "key": "status"})[0] == 200
    assert r.eff(STATUS) == {"topic": "", "source": "off"} and r.eff(ALERTS)["topic"] == SSM_ALERTS
    assert not r.p.notifier(STATUS).enabled and not r.p.notifier(STATUS).send("Station silent", "x")
    assert r.post({"action": "off", "key": "alerts"})[0] == 200
    assert r.p.line() == "notify: no pushes"
    assert r.post({"action": "reset", "key": "alerts"})[0] == 200 and r.p.notifier(ALERTS).enabled


def test_a_change_takes_effect_on_the_next_push_without_a_restart(tmp_path):
    from tests.test_notify import FakeNtfy
    ntfy = FakeNtfy()
    try:
        r = Rig(tmp_path)
        r.p.default_server = (ntfy.url, "config")
        n = r.p.notifier(STATUS)                     # what station health holds for the life of the app
        n.send("Station silent 4 min", "one")
        assert n.flush(5)
        r.post({"action": "set", "key": "status", "topic": MINE})
        n.send("Station silent 9 min", "two")
        assert n.flush(5)
        assert [(m["body"]["topic"], m["body"]["title"]) for m in ntfy.got] == [
            (SSM_STATUS, "Station silent 4 min"), (MINE, "Station silent 9 min")]
        assert n.topic == MINE and n.sent == 1 and n.enabled
    finally:
        ntfy.close()


@pytest.mark.parametrize("bad, why", [("pcfd-status", "Tapout"), ("pcfd-phone-prealert", "Tapout"), ("PCFD-Status", "Tapout"),
                                      ("firehouse-ring-xyz", "Tapout"), ("short", "at least 12"),
                                      ("has spaces in it", "letters, digits"), ("x" * 65, "at most 64")])
def test_settings_refuse_what_the_config_refuses_with_the_reason(rig, bad, why):
    code, out = rig.post({"action": "set", "key": "alerts", "topic": bad})
    assert code == 400 and why in out["error"] and rig.eff(ALERTS)["source"] == "ssm"


def test_exact_match_only_a_name_of_ours_with_status_in_it_is_fine(rig):
    for name in ("pcfd-xmpl-status", "pcfd-status-of-ours", "x-pcfd-phone-prealert"):
        assert rig.post({"action": "set", "key": "status", "topic": name})[0] == 200
        assert rig.eff(STATUS) == {"topic": name, "source": "settings"}


def test_viewers_and_dispatchers_can_neither_read_nor_change_the_topics(rig):
    assert needs("/api/cad/settings/notify") == "admin"
    for who in (JULIAN, VAL):
        code, out = rig.page(who)
        assert code == 403 and "Admins only" in out["error"] and SSM_ALERTS not in json.dumps(out)
        for body in ({"action": "set", "key": "status", "topic": MINE}, {"action": "off", "key": "alerts"},
                     {"action": "test", "key": "status"}, {"action": "server", "key": "server", "value": "https://ntfy.example"}):
            code, out = rig.post(body, who)
            assert code == 403, (who, body)
    assert rig.eff(STATUS)["source"] == "ssm" and rig.fake.got == []
    code, out = rig.page(COREY)
    assert code == 200 and out["configured"]
    by = {k["kind"]: k for k in out["kinds"]}
    assert by["status"]["topic"] == SSM_STATUS and by["status"]["source"] == "ssm" and by["alerts"]["topic"] == SSM_ALERTS
    assert [k["kind"] for k in out["kinds"]] == ["status", "alerts"]


def test_the_test_push_is_marked_goes_to_the_topic_in_effect_and_is_audited(rig):
    code, out = rig.post({"action": "test", "key": "status"})
    assert code == 200 and out["test"]["ok"]
    (m,) = rig.fake.got
    assert m["topic"] == SSM_STATUS and m["title"] == "sizeup test: status topic" and "test push" in m["body"].lower()
    rig.post({"action": "set", "key": "alerts", "topic": "pcfd-xmpl-alerts-2"})
    rig.post({"action": "test", "key": "alerts"})
    assert rig.fake.got[-1]["topic"] == "pcfd-xmpl-alerts-2" and rig.fake.got[-1]["title"] == "sizeup test: alerts topic"
    rig.fake.error = "ntfy refused it: HTTP 403"
    code, out = rig.post({"action": "test", "key": "status"})
    assert code == 200 and not out["test"]["ok"] and "403" in out["test"]["error"]
    by = {k["kind"]: k for k in rig.page()[1]["kinds"]}
    assert by["status"]["test"]["ok"] is False and by["alerts"]["test"]["ok"] is True
    audit = rig.cad.settings_audit_log({"section": ["system"]})["audit"]
    tests = [a for a in audit if a["key"].endswith("/test")]
    assert [a["key"] for a in tests] == ["notify/status/test", "notify/alerts/test", "notify/status/test"]
    assert all("Corey" in a["operator"] and a["at"] for a in tests)
    assert tests[0]["new"].startswith("test push not sent") and tests[2]["new"] == "test push sent"
    rig.post({"action": "off", "key": "status"})
    code, out = rig.post({"action": "test", "key": "status"})
    assert code == 400 and "no status topic in effect" in out["error"]


def test_every_change_is_audited_with_who_and_when_never_the_name(rig):
    rig.post({"action": "set", "key": "status", "topic": MINE})
    rig.post({"action": "off", "key": "alerts"})
    rig.post({"action": "reset", "key": "status"})
    rig.post({"action": "server", "key": "server", "value": "https://ntfy.example.org"})
    audit = rig.cad.settings_audit_log({"section": ["system"]})["audit"]
    rows = [(a["key"], a["old"], a["new"]) for a in reversed(audit) if a["key"].startswith("notify/")]
    fp = notify.fingerprint
    assert rows == [
        ("notify/status", f"default (SSM: {fp(SSM_STATUS)})", f"Settings: {fp(MINE)}"),
        ("notify/alerts", f"default (SSM: {fp(SSM_ALERTS)})", "off"),
        ("notify/status", f"Settings: {fp(MINE)}", f"default (SSM: {fp(SSM_STATUS)})"),
        ("notify/server", "default http://127.0.0.1:9", "https://ntfy.example.org")]
    assert all("Corey" in a["operator"] and a["at"] for a in audit if a["key"].startswith("notify/"))
    shown = json.dumps(audit) + json.dumps(rig.cad.activity.rows(COREY, "", None, 50)) + "\n".join(rig.logs)
    for name in (MINE, SSM_STATUS, SSM_ALERTS):
        assert name not in shown
    assert any(m.startswith("notify: alerts → off (Settings), status → topic set (SSM)") for m in rig.logs)


def test_the_names_stay_out_of_the_export_and_an_import_keeps_them(rig):
    rig.post({"action": "set", "key": "status", "topic": MINE})
    ex = rig.cad.settings_export()
    assert notify.SECTION not in ex["overrides"] and MINE not in json.dumps(ex)
    ex["overrides"][notify.SECTION] = {"status": {"topic": "pcfd-xmpl-imported-1"}}     # a file can't set them either
    code, out = call(rig.api, "/api/cad/settings/import", {"action": "apply", "settings": ex}, COREY)
    assert code == 200, out
    assert rig.eff(STATUS) == {"topic": MINE, "source": "settings"}


def test_the_hub_status_endpoint_and_the_health_panel_never_name_a_topic(tmp_path, monkeypatch, capsys):
    from sizeup.cloud.hub import HubApp
    from tests.test_cloud import hub_cfg
    app = HubApp(hub_cfg(tmp_path, alerts_ntfy_topic=SSM_ALERTS, alerts_status_topic=SSM_STATUS,
                         alerts_ntfy_topic_source="env", alerts_status_topic_source="env"), log=print)
    try:
        assert app.cad.pushes.effective(STATUS) == {"topic": SSM_STATUS, "source": "ssm"}   # the hub's env is SSM's
        shown = json.dumps(app.health.status()) + json.dumps(app.health.panel()) + capsys.readouterr().out
        shown += json.dumps(app.cad.alarms_page()) + json.dumps(app.cad.settings_get())
        assert SSM_ALERTS not in shown and SSM_STATUS not in shown
        assert "notify: alerts → topic set (SSM), status → topic set (SSM)" in shown
    finally:
        app.stop()
