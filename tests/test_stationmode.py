"""Settings > System > Station: station forwarding on and off while the app runs, its persistence,
the secrets (environment, config, macOS Keychain) and Test connection (sizeup/cloud/stationmode.py).
Real HTTP on ephemeral ports; the Keychain is always a fake `security` (tests/conftest.py)."""

import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import sizeup.cloud.stationmode as sm
from sizeup.app import App, make_app, make_server
from sizeup.cloud.stationmode import StationMode
from tests.test_cloud import TOKEN, HubRun, base, call, free_port, hub_cfg
from tests.test_app import wait

ACCESS_ID = "0123abcd.access"
ACCESS_SECRET = "access-secret-value-9876543210"


def cfg_(tmp_path, **kw):
    return base(cad_db=str(tmp_path / "state" / "cad.sqlite"), cloud_state=str(tmp_path / "state" / "cloud.json"), **kw)


def fake_security(values: dict, calls: list | None = None):
    """A `security find-generic-password -s <service> -w`: values by service, 44 (not found) otherwise."""
    def run(args, **kw):
        assert args[:2] == [sm.SECURITY, "find-generic-password"] and "-w" in args
        service = args[args.index("-s") + 1]
        if calls is not None:
            calls.append(service)
        if service in values:
            return subprocess.CompletedProcess(args, 0, values[service] + "\n", "")
        return subprocess.CompletedProcess(args, 44, "", "security: SecKeychainSearchCopyNext: not found")
    return run


@pytest.fixture
def keychain(monkeypatch):
    """Pretend this is a Mac with a Keychain (also on Linux CI); returns a dict to fill in."""
    values: dict[str, str] = {}
    calls: list[str] = []
    monkeypatch.setattr(sm, "keychain_available", lambda: True)
    monkeypatch.setattr(sm, "RUN", fake_security(values, calls))
    values["_calls"] = calls          # (never a service name)
    return values


def spy(monkeypatch, obj, name: str, calls: list) -> None:
    """Receivers / speech-to-text / voice: nothing here may be started or stopped by the switch."""
    for fn in ("start", "stop"):
        monkeypatch.setattr(obj, fn, lambda *a, _n=f"{name}.{fn}", **k: calls.append(_n), raising=False)


def serve(app):
    server = make_server(app)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.httpd.server_address[1]}"


# -- on / off while running ------------------------------------------------------------------------------


def test_forwarding_turns_on_and_off_at_runtime_without_touching_the_radios(tmp_path, monkeypatch):
    monkeypatch.setenv("SIZEUP_STATION_TOKEN", TOKEN)
    hub = HubRun(hub_cfg(tmp_path / "hub"))
    app = make_app(cfg_(tmp_path))
    touched: list[str] = []
    assert app.receivers
    for k, r in enumerate(app.receivers):
        spy(monkeypatch, r, f"receiver{k}", touched)
    spy(monkeypatch, app.whisper, "whisper", touched)
    spy(monkeypatch, app.stt, "stt", touched)
    app.station.start()                                   # (App.start, without the radios)
    try:
        assert app.station.forward is False and app.link is None and app.events is None
        assert app.cad.state()["cloud"] is None and "Hub" not in [r["label"] for r in app.radio_status()]

        on = app.station.set({"forward": True, "hub_url": hub.url, "operator": "Lt. Ruiz"})
        assert on["forward"] is True and on["running"] is True and app.events is not None
        link = app.link
        assert wait(lambda: app.station.status()["state"] == "ok", 10)
        assert app.station.state()["status"]["text"] == "Connected"
        assert app.cad.state()["cloud"]["role"] == "station"            # the "hub is the CAD of record" banner
        assert [r for r in app.radio_status() if r["label"] == "Hub"][0]["state"] == "ok"
        assert wait(lambda: hub.app.hub.connected(), 10)

        off = app.station.set({"forward": False, "operator": "Lt. Ruiz"})
        assert off["forward"] is False and off["running"] is False and off["status"]["state"] == "off"
        assert app.link is None and app.events is None and link.stopped
        assert app.cad.state()["cloud"] is None and "Hub" not in [r["label"] for r in app.radio_status()]
        assert wait(lambda: not any(t.is_alive() for t in link._threads if t.name != "hub-jobs"), 10)
        app._emit("traffic", {"id": "x"}, None)                       # all-in-one again: a no-op

        audit = app.cad.settings.audit_log("system")
        assert [(r["key"], r["operator"], r["old"], r["new"]) for r in reversed(audit)] == [
            ("station forwarding", "Lt. Ruiz", "off", "on"), ("station hub URL", "Lt. Ruiz", "", hub.url),
            ("station forwarding", "Lt. Ruiz", "on", "off")]
        assert touched == []                                            # radios, STT, voice: untouched
    finally:
        app.station.stop()
        hub.close()


def test_the_choice_survives_a_restart_and_wins_over_config_toml(tmp_path, monkeypatch):
    monkeypatch.setenv("SIZEUP_STATION_TOKEN", TOKEN)
    url = f"http://127.0.0.1:{free_port()}"
    cfg = cfg_(tmp_path, cloud_role="station", cloud_hub_url=url)
    app = make_app(cfg)
    assert app.station.forward is True and app.station.state()["from"] == "config" and app.link is not None
    app.station.set({"forward": False, "operator": "Corey"})
    assert json.loads(Path(cfg.cloud_state).read_text())["forward"] is False

    again = make_app(cfg)                                 # config.toml still says station: the UI's off wins
    assert again.station.forward is False and again.link is None and again.station.state()["from"] == "settings"
    again.station.set({"forward": True, "hub_url": url + "/", "operator": "Corey"})

    third = make_app(cfg_(tmp_path))                      # config.toml all-in-one: the UI's on wins
    assert third.station.forward is True and third.station.hub_url == url and third.events is not None


def test_hub_url_is_validated(tmp_path):
    app = make_app(cfg_(tmp_path))
    for body, why in (({"forward": True}, "hub URL"), ({"forward": True, "hub_url": "http://hub.example.org"}, "https"),
                      ({"forward": "yes"}, "true or false"), ({"hub_url": 7}, "hub_url"),
                      ({"forward": True, "hub_url": "hub.example.org"}, "https")):
        with pytest.raises(ValueError, match=why):
            app.station.set(body)
    assert app.station.forward is False and not Path(app.cfg.cloud_state).exists()
    app.station.set({"hub_url": "https://hub.example.org/", "operator": "x"})        # a URL ready, still off
    assert (app.station.forward, app.station.hub_url) == (False, "https://hub.example.org")


# -- secrets ------------------------------------------------------------------------------------------------


def test_secrets_come_from_the_keychain_when_the_environment_has_none(tmp_path, keychain):
    keychain.update({"sizeup-station-token": TOKEN, "sizeup-access-id": ACCESS_ID, "sizeup-access-secret": ACCESS_SECRET})
    app = make_app(cfg_(tmp_path, cloud_role="station", cloud_hub_url="https://hub.example.org"))
    assert app.link is not None and app.station.problem == ""
    assert (app.link.cfg.cloud_token, app.link.cfg.cloud_access_id, app.link.cfg.cloud_access_secret) == (
        TOKEN, ACCESS_ID, ACCESS_SECRET)
    sec = app.station.state()["secrets"]
    assert {k: (v["set"], v["source"]) for k, v in sec.items()} == {
        "token": (True, "Keychain"), "access_id": (True, "Keychain"), "access_secret": (True, "Keychain")}
    assert sec["token"]["keychain"] == "sizeup-station-token"


def test_custom_keychain_names(tmp_path, keychain):
    keychain.update({"pcfd-token": TOKEN})
    app = make_app(cfg_(tmp_path, cloud_role="station", cloud_hub_url="https://hub.example.org",
                        cloud_keychain_token="pcfd-token"))
    assert app.station.problem == "" and "pcfd-token" in keychain["_calls"]


def test_a_missing_token_is_explained_and_events_still_queue(tmp_path, keychain):
    app = make_app(cfg_(tmp_path, cloud_role="station", cloud_hub_url="https://hub.example.org"))
    app.station.start()
    st = app.station.state()
    assert st["forward"] is True and st["running"] is False and st["status"]["state"] == "blocked"
    assert "no token" in st["status"]["text"] and "sizeup-station-token" in st["status"]["text"]
    assert st["secrets"]["token"] == {"set": False, "source": "", "env": "SIZEUP_STATION_TOKEN",
                                      "keychain": "sizeup-station-token", "short": False}
    hub_row = [r for r in app.radio_status() if r["label"] == "Hub"][0]
    assert hub_row["state"] == "down" and hub_row["word"] == "not sending"
    assert app.cad.state()["cloud"]["blocked"] is True
    app._heard("p25", "1701", "E63")
    assert app.station.outbox().pending()[0] == 1          # nothing lost while it waits for the token
    keychain["sizeup-station-token"] = TOKEN
    r = app.station.test({})                               # Test connection looks again (and starts it)
    assert app.station.problem == "" and app.link is not None and app.link.running
    assert r["ok"] is False                                # (no hub at hub.example.org from a test)
    app.station.stop()


def test_without_a_keychain_it_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(sm, "keychain_available", lambda: False)
    monkeypatch.setattr(sm, "RUN", lambda *a, **k: pytest.fail("no `security` off macOS"))
    app = make_app(cfg_(tmp_path, cloud_role="station", cloud_hub_url="https://hub.example.org"))
    st = app.station.state()
    assert st["keychain"] is False and st["status"]["text"].startswith("Not sending: no token (SIZEUP_STATION_TOKEN)")


def test_the_environment_wins_over_the_keychain(tmp_path, keychain, monkeypatch):
    keychain.update({"sizeup-station-token": "keychain-token-0000000000", "sizeup-access-id": ACCESS_ID})
    monkeypatch.setenv("SIZEUP_STATION_TOKEN", TOKEN)
    app = make_app(cfg_(tmp_path, cloud_role="station", cloud_hub_url="https://hub.example.org"))
    assert app.link.cfg.cloud_token == TOKEN and app.link.cfg.cloud_access_id == ACCESS_ID
    sec = app.station.state()["secrets"]
    assert (sec["token"]["source"], sec["access_id"]["source"], sec["access_secret"]["set"]) == ("env", "Keychain", False)
    assert "sizeup-station-token" not in keychain["_calls"]


def test_a_short_token_is_refused(tmp_path, keychain):
    keychain.update({"sizeup-station-token": "short"})
    app = make_app(cfg_(tmp_path, cloud_role="station", cloud_hub_url="https://hub.example.org"))
    assert "too short" in app.station.problem and app.station.state()["secrets"]["token"]["short"] is True


# -- the hub can't be switched ------------------------------------------------------------------------------


def test_the_hub_refuses_station_mode(tmp_path):
    hub = HubRun(hub_cfg(tmp_path))
    try:
        assert hub.app.station is None
        assert call(hub.url + "/api/station-mode", token=None)[0] == 409
        code, body = call(hub.url + "/api/station-mode", "POST", {"forward": True, "hub_url": "https://x.org"}, token=None)
        assert code == 409 and "hub" in body["error"]
        assert call(hub.url + "/api/station-mode/test", "POST", {}, token=None)[0] == 409
    finally:
        hub.close()


# -- Test connection -------------------------------------------------------------------------------------------


class FakeHub:
    """/healthz and /api/station/hello, recording each request's headers."""

    def __init__(self, hello=200, role="hub", healthz=200):
        self.seen: list[tuple[str, dict]] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.seen.append((self.path, {k.lower(): v for k, v in self.headers.items()}))
                code, body = ((healthz, {"ok": True, "role": role, "version": "9.9"}) if self.path == "/healthz"
                              else (hello, {"ok": True} if hello == 200 else {"error": "no"}))
                raw = json.dumps(body).encode() if code < 300 or code >= 400 else b"<html>Sign in</html>"
                self.send_response(code)
                if 300 <= code < 400:
                    self.send_header("Location", "https://team.cloudflareaccess.com/cdn-cgi/access/login")
                self.send_header("Content-Type", "application/json" if raw[:1] == b"{" else "text/html")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def test_test_connection_sends_the_bearer_token_and_the_access_headers(tmp_path, keychain, monkeypatch):
    monkeypatch.setenv("SIZEUP_STATION_TOKEN", TOKEN)
    keychain.update({"sizeup-access-id": ACCESS_ID, "sizeup-access-secret": ACCESS_SECRET})
    fake = FakeHub()
    app = make_app(cfg_(tmp_path))
    server, url = serve(app)
    try:
        code, r = call(url + "/api/station-mode/test", "POST", {"hub_url": fake.url}, token=None)
        assert code == 200 and r["ok"] is True and "token accepted" in r["text"] and "9.9" in r["text"]
        assert [p for p, _ in fake.seen] == ["/healthz", "/api/station/hello"]
        for _, h in fake.seen:
            assert h["authorization"] == f"Bearer {TOKEN}"
            assert (h["cf-access-client-id"], h["cf-access-client-secret"]) == (ACCESS_ID, ACCESS_SECRET)
        assert app.station.forward is False                    # a test changes nothing
    finally:
        server.shutdown()
        fake.close()


def test_test_connection_reports_what_went_wrong(tmp_path, monkeypatch):
    monkeypatch.setenv("SIZEUP_STATION_TOKEN", TOKEN)
    app = make_app(cfg_(tmp_path))
    refused, not_hub = FakeHub(hello=401), FakeHub(role="all-in-one")
    people_only, login = FakeHub(healthz=302), FakeHub(healthz=302, hello=302)
    try:
        r = app.station.test({"hub_url": people_only.url})     # /healthz behind the people's Access app
        assert r["ok"] is True and "token accepted" in r["text"] and "302" in r["text"]
        r = app.station.test({"hub_url": login.url})            # no service token through Access
        assert (r["ok"], r["status"]) == (False, 302) and "Access id and secret" in r["text"]
        r = app.station.test({"hub_url": refused.url})
        assert (r["ok"], r["status"]) == (False, 401) and "refused the token" in r["text"]
        r = app.station.test({"hub_url": not_hub.url})
        assert r["ok"] is False and "not a hub" in r["text"]
        r = app.station.test({"hub_url": f"http://127.0.0.1:{free_port()}"})
        assert (r["ok"], r["status"]) == (False, 0) and r["text"].startswith("Can't reach it")
        assert app.station.test({"hub_url": "http://hub.example.org"})["text"].startswith("Hub URL")
    finally:
        for f in (refused, not_hub, people_only, login):
            f.close()


def test_test_connection_against_a_real_hub(tmp_path, monkeypatch):
    hub = HubRun(hub_cfg(tmp_path / "hub"))
    app = make_app(cfg_(tmp_path))
    try:
        monkeypatch.setenv("SIZEUP_STATION_TOKEN", TOKEN)
        assert app.station.test({"hub_url": hub.url})["ok"] is True
        assert not hub.app.hub.connected()                     # /hello isn't "the station is here"
        monkeypatch.setenv("SIZEUP_STATION_TOKEN", "another-token-0123456789")
        r = app.station.test({"hub_url": hub.url})
        assert (r["ok"], r["status"]) == (False, 401)
    finally:
        hub.close()


# -- the outbox ---------------------------------------------------------------------------------------------


def test_the_outbox_is_kept_while_off_and_drains_when_back_on(tmp_path, monkeypatch):
    monkeypatch.setenv("SIZEUP_STATION_TOKEN", TOKEN)
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    app = make_app(cfg_(tmp_path))
    app.station.start()
    app.station.set({"forward": True, "hub_url": url, "operator": "x"})
    app._heard("p25", "1701", "E63")
    ob = app.station.outbox()
    assert wait(lambda: app.station.status()["state"] == "down", 10)      # no hub there
    assert ob.pending()[0] == 1 and "Offline since" in app.station.status()["text"]
    app.station.set({"forward": False, "operator": "x"})
    app._heard("p25", "1702", "E64")                                       # off: not queued
    assert ob.pending()[0] == 1
    st = app.station.state()["status"]
    assert st["state"] == "off" and st["waiting"] == 1 and st["text"] == "Off · 1 waiting"
    assert Path(tmp_path / "state" / "outbox.sqlite").is_file()

    again = make_app(cfg_(tmp_path))                                       # also after a restart while off
    assert again.station.state()["status"]["waiting"] == 1
    again.station.outbox().close()

    hub = HubRun(hub_cfg(tmp_path / "hub", port=port))
    try:
        app.station.set({"forward": True, "operator": "x"})
        assert wait(lambda: ob.pending()[0] == 0, 15)
        assert hub.app.hub.seq >= 1 and app.station.outbox() is ob             # the same outbox, drained
    finally:
        app.station.stop()
        hub.close()


# -- never a secret ---------------------------------------------------------------------------------------


def test_secrets_are_never_logged_or_answered(tmp_path, keychain, monkeypatch):
    keychain.update({"sizeup-station-token": TOKEN, "sizeup-access-id": ACCESS_ID, "sizeup-access-secret": ACCESS_SECRET})
    fake = FakeHub(hello=401)
    lines: list[str] = []
    app = App(cfg_(tmp_path))
    StationMode(app, app.cfg, log=lines.append)
    server, url = serve(app)
    try:
        answers = [call(url + "/api/station-mode", token=None),
                   call(url + "/api/station-mode", "POST", {"forward": True, "hub_url": fake.url, "operator": "x"}, token=None),
                   call(url + "/api/station-mode/test", "POST", {}, token=None),
                   call(url + "/api/station-mode", "POST", {"hub_url": "https://hub.example.org"}, token=None),
                   call(url + "/api/station-mode", "POST", {"forward": False, "operator": "x"}, token=None),
                   call(url + "/api/cad/settings/audit?section=system", token=None)]
        assert [c for c, _ in answers] == [200] * 6
        assert answers[2][1]["status"] == 401
        app.station.start()
        app.station.set({"forward": True, "hub_url": fake.url, "operator": "x"})
        wait(lambda: app.station.status()["state"] == "down", 10)
        answers.append(call(url + "/api/station-mode", token=None))
        app.station.stop()
        text = json.dumps([b for _, b in answers]) + "\n".join(lines) + Path(app.cfg.cloud_state).read_text()
        text += json.dumps(app.cad.state()) + json.dumps(app.radio_status())
        assert lines and "Keychain" in json.dumps(answers[0][1])
        for secret in (TOKEN, ACCESS_ID, ACCESS_SECRET):
            assert secret not in text
    finally:
        server.shutdown()
        fake.close()


def test_get_station_mode_on_an_all_in_one_machine(tmp_path):
    app = make_app(cfg_(tmp_path))
    server, url = serve(app)
    try:
        code, d = call(url + "/api/station-mode", token=None)
        assert code == 200 and d["forward"] is False and d["from"] == "config" and d["config_role"] == "all-in-one"
        assert d["status"]["state"] == "off" and d["secrets"]["token"]["set"] is False
        code, d = call(url + "/api/station-mode", "POST", {"forward": True, "hub_url": "http://hub.example.org"}, token=None)
        assert code == 400 and "https" in d["error"]
    finally:
        server.shutdown()
