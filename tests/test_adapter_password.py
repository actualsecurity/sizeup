"""The station adapter's password set from the hub (Settings > Health, sizeup/cloud/adapterpass.py): write-only to SSM,
admins only, at most one save per 5 min, a confirm while the adapter is stopped on refused log-ons, and the value never
in a log, the audit, the activity, an answer, an error page or an exception, whatever AWS answers.

Synthetic names only: the parameter, the key and the password below are made up."""

from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from sizeup import aws
from sizeup.cad.api import ADMIN_READS, CadApi, needs, summarize
from sizeup.cloud import adapterpass
from sizeup.cloud.access import Identity
from sizeup.cloud.adapterpass import AdapterPassword, Hidden, SaveRefused, problem, station_sources
from sizeup.web.server import WebServer
from tests.test_cad import make_cad
from tests.test_roles import COREY, JULIAN, VAL, people

ROOT = Path(__file__).resolve().parent.parent
NAME = "/sizeup-station/adapter/TEST_FEED_PASS"
KEY = "1234abcd-12ab-34cd-56ef-1234567890ab"
SECRET = 'Synth3tic-"pw\\9f!x é'          # a quote, a backslash and a non-ASCII letter: every way JSON escapes it
FORMS = (SECRET, json.dumps(SECRET)[1:-1], json.dumps(SECRET, ensure_ascii=False)[1:-1], "Synth3tic")


def leaks(*texts) -> list[str]:
    """Any form of SECRET in these (the raw value, its JSON escapes, a recognisable piece of it)."""
    blob = "\n".join(t.decode("utf-8", "replace") if isinstance(t, (bytes, bytearray)) else str(t) for t in texts)
    return [f for f in FORMS if f and f in blob]


class FakeSsm:
    """SSM as the hub sees it: send(url, headers, body, timeout) -> (status, body). Keeps every request."""

    def __init__(self):
        self.calls: list[dict] = []
        self.version = 4
        self.modified = 0.0
        self.put_answer = None          # (status, body) to answer the next PutParameter with, else a new version
        self.raise_on_put: Exception | None = None

    def __call__(self, url, headers, body, timeout):
        doc = json.loads(bytes(body))
        target = headers["X-Amz-Target"]
        self.calls.append({"target": target, "doc": doc, "headers": dict(headers), "url": url})
        if target == "AmazonSSM.DescribeParameters":
            assert doc == {"ParameterFilters": [{"Key": "Name", "Option": "Equals", "Values": [NAME]}]}
            return 200, json.dumps({"Parameters": [{"Name": NAME, "Type": "SecureString", "KeyId": KEY,
                                                    "Version": self.version, "LastModifiedDate": self.modified,
                                                    "LastModifiedUser": "arn:aws:iam::000000000000:user/x"}]}).encode()
        assert target == "AmazonSSM.PutParameter"
        if self.raise_on_put is not None:
            raise self.raise_on_put
        if self.put_answer is not None:
            ans, self.put_answer = self.put_answer, None
            return ans
        self.version += 1
        return 200, json.dumps({"Version": self.version, "Tier": "Standard"}).encode()

    def puts(self):
        return [c for c in self.calls if c["target"] == "AmazonSSM.PutParameter"]


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class Access:
    """Cloudflare Access as far as the server goes: the person named in X-Test-User (none: not signed in)."""

    def identify(self, headers):
        e = headers.get("X-Test-User")
        return Identity(email=e) if e else None


@pytest.fixture
def hub(tmp_path):
    cad = make_cad(tmp_path)
    people(cad)
    api = CadApi(cad)
    ssm, clock, logs, access_log = FakeSsm(), Clock(), [], []
    ap = AdapterPassword(NAME, KEY, region="us-east-1", send=ssm, clock=clock, log=logs.append,
                         creds=lambda: aws.Credentials("AKIDEXAMPLE", "wJalrXUtnFEMIexample"))
    srv = WebServer("127.0.0.1", 0, {}, lambda: {}, lambda c: None, lambda i: False, cad=api, access=Access(),
                    access_log=access_log.append, role="hub", adapter_password=ap)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.httpd.server_address[1]}"
    h = type("Hub", (), {"url": url, "cad": cad, "api": api, "ssm": ssm, "clock": clock, "logs": logs,
                         "access_log": access_log, "ap": ap, "srv": srv})()
    yield h
    srv.shutdown()


def post(h, body, who=COREY, headers=None, raw=None):
    hdr = {"Content-Type": "application/json", "X-Sizeup-Action": "adapter-password"} if headers is None else headers
    if who:
        hdr["X-Test-User"] = who
    data = raw if raw is not None else json.dumps(body).encode()
    req = urllib.request.Request(h.url + "/api/cad/adapter-password", method="POST", data=data, headers=hdr)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            out = r.read()
            return r.status, json.loads(out), out
    except urllib.error.HTTPError as e:
        out = e.read()
        try:
            return e.code, json.loads(out), out
        except ValueError:
            return e.code, {}, out


def get(h, who=COREY):
    req = urllib.request.Request(h.url + "/api/cad/adapter-password", headers={"X-Test-User": who} if who else {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            out = r.read()
            return r.status, json.loads(out), out
    except urllib.error.HTTPError as e:
        out = e.read()
        return e.code, (json.loads(out) if out[:1] == b"{" else {}), out


def everything(h) -> list:
    """What the hub kept or said: its logs, the access log, every table a request could write to."""
    db = h.cad.store
    return [*h.logs, *h.access_log, json.dumps(db.q("SELECT * FROM settings_audit")),
            json.dumps(db.q("SELECT * FROM user_activity")), json.dumps(db.q("SELECT * FROM meta"))]


def stop_source(h, reason="login (manual reset needed); credential v4 from SSM (key …WXYZ)"):
    h.cad.sources.on_health({"source": "feed", "label": "County feed", "rank": 1, "state": "failing",
                             "reason": reason, "at": h.clock() - 5})


# -- the value itself ------------------------------------------------------------------------------------------------

def test_hidden_never_shows_the_value():
    s = Hidden(SECRET)
    assert repr(s) == str(s) == f"{s}" == "<hidden>" and "%s" % s == "<hidden>"
    assert not leaks(repr([s]), repr({"k": s}))
    with pytest.raises(TypeError):
        import pickle
        pickle.dumps(s)
    with pytest.raises(TypeError):
        json.dumps({"v": s})
    assert Hidden("a") == Hidden("a") and Hidden("a") != Hidden("b") and Hidden("a") != "a"
    s.drop()
    assert s.reveal() == ""


def test_what_a_password_may_be():
    assert problem(SECRET) == ""
    assert problem("") == "Empty"
    assert problem("x" * 4097) .startswith("Too long")
    assert problem("é" * 2049).startswith("Too long")           # bytes, not characters
    for bad in ("a\nb", "a\rb", "a\0b", "a\tb"):
        assert problem(bad) == "No line breaks or control characters"
    assert problem("a\ud800b") == "Not valid text"
    assert not leaks(*(problem(x + SECRET) for x in ("", "\n", "x" * 5000, "\ud800")))


def test_the_names_must_be_the_adapters():
    assert AdapterPassword(lookup=False).why_not == "Not configured"
    assert AdapterPassword(NAME, KEY).configured
    assert AdapterPassword("/sizeup-hub/station-token", KEY).why_not.startswith("Not configured (the parameter")   # never
    assert AdapterPassword(NAME, "not a key").why_not.startswith("Not configured (not a KMS")    # anything but the adapter's
    for k in ("alias/some-key", f"arn:aws:kms:us-east-1:000000000000:key/{KEY}", "mrk-" + "a" * 32):
        assert AdapterPassword(NAME, k).configured, k
    src = (ROOT / "sizeup" / "cloud" / "adapterpass.py").read_text() + (ROOT / "sizeup" / "app.py").read_text()
    assert "SIZEUP_" + "COUNTY" not in src                           # (the real name is the department's, never here)


def test_the_task_definition_knows_nothing_of_it():
    """Optional: a deploy never depends on it (a task whose secret is missing doesn't start)."""
    text = (ROOT / "deploy" / "aws" / "taskdef.json").read_text()
    assert "adapter-password" not in text and "ADAPTER_PASSWORD" not in text


class LookupSsm(FakeSsm):
    """FakeSsm plus the hub's two name parameters (GetParameter, never with decryption)."""

    def __init__(self, names: dict | None = None, error: str = ""):
        super().__init__()
        self.names, self.error = names, error

    def __call__(self, url, headers, body, timeout):
        if headers["X-Amz-Target"] == "AmazonSSM.GetParameter":
            doc = json.loads(bytes(body))
            self.calls.append({"target": "AmazonSSM.GetParameter", "doc": doc, "headers": dict(headers), "url": url})
            assert set(doc) == {"Name"}                                 # never WithDecryption
            if self.error:
                return 400, json.dumps({"__type": self.error, "message": "no"}).encode()
            if not self.names or doc["Name"] not in self.names:
                return 400, json.dumps({"__type": "ParameterNotFound"}).encode()
            return 200, json.dumps({"Parameter": {"Name": doc["Name"], "Type": "String", "Value": self.names[doc["Name"]],
                                                  "Version": 1}}).encode()
        return super().__call__(url, headers, body, timeout)


def lookup_ap(ssm, clock, logs=None):
    return AdapterPassword(region="us-east-1", send=ssm, clock=clock, log=(logs if logs is not None else []).append,
                           creds=lambda: aws.Credentials("AKIDEXAMPLE", "x"))


def test_the_names_are_looked_up_lazily_cached_and_retried():
    clock, ssm = Clock(), LookupSsm({adapterpass.LOOKUP_PARAM: NAME, adapterpass.LOOKUP_KEY: KEY})
    logs = []
    ap = lookup_ap(ssm, clock, logs)
    assert ssm.calls == []                                              # nothing at the start
    assert ap.configured and ap.name == NAME and ap.key == KEY
    assert [c["doc"]["Name"] for c in ssm.calls] == [adapterpass.LOOKUP_PARAM, adapterpass.LOOKUP_KEY]
    assert logs == ["adapter password: configured from SSM"]           # (never the names)
    ap.configured
    assert len(ssm.calls) == 2                                          # cached
    clock.t += adapterpass.RESOLVED_S + 1
    ssm.error = "InternalServerError"                                   # a hiccup: the names that worked stay
    assert ap.configured and len(ssm.calls) == 3
    clock.t += adapterpass.RESOLVED_S + 1
    ssm.error = "AccessDeniedException"                                 # taken away (step 20 undone): not configured
    assert not ap.configured and ap.status(make_cad().store)["detail"] == "AccessDeniedException"


def test_without_the_parameters_the_hub_runs_the_card_says_not_configured_and_nothing_is_written(tmp_path, capfd):
    """No parameters (step 20 never ran): the hub starts as ever, the card says so, a save is refused, no AWS write."""
    from tests.test_cloud import HubRun, hub_cfg
    hub = HubRun(hub_cfg(tmp_path))                     # the real hub, as make_server builds it (no AWS credentials)
    try:
        from sizeup.app import adapter_password
        made = adapter_password(hub.app.cfg)
        assert isinstance(made, AdapterPassword) and not made._fixed
        assert hub.get("/healthz")["ok"] is True
        code, st, _ = get(hub)                          # (no Access here: nobody to check, as all-in-one)
        assert code == 403                              # never without a verified person
    finally:
        hub.close()
    clock, ssm = Clock(), LookupSsm(None)               # SSM says ParameterNotFound
    cad = make_cad(tmp_path)
    people(cad)
    ap = lookup_ap(ssm, clock)
    srv = WebServer("127.0.0.1", 0, {}, lambda: {}, lambda c: None, lambda i: False, cad=CadApi(cad), access=Access(),
                    role="hub", adapter_password=ap)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    h = type("Hub", (), {"url": f"http://127.0.0.1:{srv.httpd.server_address[1]}"})()
    try:
        code, st, _ = get(h)
        assert code == 200 and st["configured"] is False and st["why_not"] == "Not configured" and st["detail"] == "absent"
        code, out, _ = post(h, {"password": SECRET, "again": SECRET})
        assert code == 404 and out["error"] == "Not configured"
        assert {c["target"] for c in ssm.calls} == {"AmazonSSM.GetParameter"}   # looked up once, nothing written
        n = len(ssm.calls)
        post(h, {"password": SECRET, "again": SECRET})
        assert len(ssm.calls) == n                      # asked again only after RETRY_S
        clock.t += adapterpass.RETRY_S + 1
        ssm.names = {adapterpass.LOOKUP_PARAM: NAME, adapterpass.LOOKUP_KEY: KEY}   # step 20 ran: it comes on by itself
        code, st, _ = get(h)
        assert st["configured"] and st["version"] == 4
    finally:
        srv.shutdown()
    io = capfd.readouterr()
    assert not leaks(io.out, io.err)


# -- the endpoint ----------------------------------------------------------------------------------------------------

def test_an_admin_saves_a_new_version_and_only_ssm_ever_sees_it(hub, capfd):
    code, out, raw = post(hub, {"password": SECRET, "again": SECRET})
    assert code == 200 and out == {"ok": True, "version": 5, "at": hub.clock()}
    put = hub.ssm.puts()[0]
    assert put["doc"] == {"Name": NAME, "Value": SECRET, "Type": "SecureString", "KeyId": KEY, "Overwrite": True,
                          "Description": adapterpass.DESCRIPTION}
    assert put["url"] == "https://ssm.us-east-1.amazonaws.com/"
    assert put["headers"]["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/")
    assert "/us-east-1/ssm/aws4_request" in put["headers"]["Authorization"]
    assert not leaks(json.dumps(put["headers"]))                # (the signature is over it, the value isn't in it)
    # the audit: who, when, the version; the person's activity: what they did; the log: the version and who
    audit = hub.cad.store.q("SELECT * FROM settings_audit WHERE key = 'station adapter password'")
    assert [(a["operator"], a["old"], a["new"], a["at"]) for a in audit] == [(f"Corey <{COREY}>", "v4", "v5", hub.clock())]
    acts = hub.cad.store.q("SELECT summary, route FROM user_activity WHERE kind = 'action'")
    assert {"summary": "Saved the station adapter password (v5)", "route": "POST /api/cad/adapter-password"} in acts
    assert any(m == f"adapter password: v5 saved by {COREY}" for m in hub.logs)
    # the page: the version, when, who (from the hub's own audit), the next save in 5 min
    code, st, sraw = get(hub)
    assert code == 200 and st["version"] == 5 and st["saved_here"] and st["saved_by"] == f"Corey <{COREY}>" and st["wait_s"] == 300
    assert "LastModifiedUser" not in sraw.decode() and "000000000000" not in sraw.decode()
    io = capfd.readouterr()
    assert not leaks(raw, sraw, io.out, io.err, *everything(hub))


def test_the_status_never_reads_the_value(hub):
    hub.ssm.modified = hub.clock() - 3600
    code, st, raw = get(hub)
    assert code == 200 and st["version"] == 4 and not st["saved_here"] and st["wait_s"] == 0
    assert [c["target"] for c in hub.ssm.calls] == ["AmazonSSM.DescribeParameters"]      # metadata only
    get(hub)
    assert len(hub.ssm.calls) == 1                                                        # cached a minute
    src = (ROOT / "sizeup" / "cloud" / "adapterpass.py").read_text()
    assert "WithDecryption" not in src and "GetParameterHistory" not in src and "GetParameters" not in src
    # its only GetParameter: the hub's two name parameters (plain Strings), never the password's
    assert src.count('"AmazonSSM.GetParameter"') == 1 and 'for n in (LOOKUP_PARAM, LOOKUP_KEY):' in src


def test_admins_only_and_refused_before_the_body_is_read(hub):
    assert needs("/api/cad/adapter-password") == "admin" and "/api/cad/adapter-password" in ADMIN_READS
    for who in (JULIAN, VAL):
        code, out, raw = post(hub, {"password": SECRET, "again": SECRET}, who=who)
        assert code == 403 and "Admins only" in out["error"]
        assert get(hub, who)[0] == 403
    assert post(hub, {"password": SECRET, "again": SECRET}, who=None)[0] == 401        # never without a person
    assert get(hub, None)[0] == 401
    # without the header (a form, a page elsewhere: a CORS preflight would be needed) or as a form: refused
    assert post(hub, {"password": SECRET, "again": SECRET}, headers={"Content-Type": "application/json"})[0] == 400
    form = {"Content-Type": "application/x-www-form-urlencoded", "X-Sizeup-Action": "adapter-password"}
    assert post(hub, None, headers=form, raw=b"password=x&again=x")[0] == 400
    assert hub.ssm.calls == []
    assert not leaks(*everything(hub))


def test_refusals_before_aws(hub):
    code, out, raw = post(hub, {"password": SECRET, "again": SECRET + "x"})
    assert code == 400 and out["error"] == "They differ"
    assert post(hub, {"password": "", "again": ""})[1]["error"] == "Empty"
    assert post(hub, {"password": "a\nb", "again": "a\nb"})[1]["error"] == "No line breaks or control characters"
    assert post(hub, {"password": 5, "again": 5})[1]["error"] == "Bad request"
    code, out, raw2 = post(hub, None, raw=b'{"password": "' + SECRET.encode() + b'", "again": ')   # broken JSON
    assert code == 400 and out["error"] == "Bad JSON"
    code, out, raw3 = post(hub, None, raw=b'{"password": "\xff' + SECRET.encode() + b'"}')       # not UTF-8
    assert code == 400 and out["error"] == "Bad JSON"
    code, _, _ = post(hub, None, raw=b"x" * 70000)
    assert code == 400
    assert hub.ssm.puts() == []
    assert not leaks(raw, raw2, raw3, *everything(hub))


def test_one_save_per_5_min_counting_the_cli_too(hub):
    assert post(hub, {"password": SECRET, "again": SECRET})[0] == 200
    hub.clock.t += 120
    code, out, raw = post(hub, {"password": SECRET, "again": SECRET})
    assert code == 429 and out["error"] == "Next save in 3 min" and out["wait_s"] == 180
    assert len(hub.ssm.puts()) == 1
    refused = hub.cad.store.q("SELECT summary FROM user_activity WHERE kind = 'refused'")
    assert refused and "Next save in 3 min" in refused[-1]["summary"]
    hub.clock.t += 181
    assert post(hub, {"password": SECRET, "again": SECRET})[0] == 200
    # the CLI saved a minute ago (SSM's LastModifiedDate): the hub waits for it too
    hub.clock.t += 600
    hub.ssm.modified = hub.clock() - 60
    assert post(hub, {"password": SECRET, "again": SECRET})[0] == 429
    assert not leaks(*everything(hub))


def test_a_stopped_adapter_needs_the_explicit_confirm(hub):
    stop_source(hub)
    code, st, _ = get(hub)
    assert st["stopped"] and st["manual"] and st["sources"][0]["label"] == "County feed"
    code, out, raw = post(hub, {"password": SECRET, "again": SECRET})
    assert code == 409 and "confirm" in out["error"] and hub.ssm.puts() == []
    assert post(hub, {"password": SECRET, "again": SECRET, "confirm": "yes"})[0] == 409    # true only
    assert post(hub, {"password": SECRET, "again": SECRET, "confirm": True})[0] == 200
    # stale reports count too (the last thing known), any "login" stop does, other failures don't
    assert station_sources(hub.cad.store)[0]["stopped"]
    stop_source(hub, "waiting for SSM (AccessDeniedException)")
    assert not station_sources(hub.cad.store)[0]["stopped"]
    stop_source(hub, "login rate")
    assert station_sources(hub.cad.store)[0]["stopped"] and not station_sources(hub.cad.store)[0]["manual"]


@pytest.mark.parametrize("answer", [
    (400, {"__type": "ValidationException", "message": f"1 validation error detected: Value '{SECRET}' at 'value' failed"}),
    (400, {"__type": "AccessDeniedException", "message": f"User: arn:aws:sts::000000000000:assumed-role/x is not authorized {SECRET}"}),
    (400, {"__type": "com.amazon#KMSAccessDeniedException", "message": SECRET}),
])
def test_aws_refusing_never_echoes_the_value_and_isnt_counted(hub, capfd, answer):
    hub.ssm.put_answer = (answer[0], json.dumps(answer[1]).encode())
    code, out, raw = post(hub, {"password": SECRET, "again": SECRET})
    assert code == 502 and re.fullmatch(r"AWS: [A-Za-z]+", out["error"]) and not out.get("maybe_saved")
    assert "000000000000" not in raw.decode()
    assert hub.cad.store.q("SELECT * FROM settings_audit WHERE key = 'station adapter password'") == []
    assert post(hub, {"password": SECRET, "again": SECRET})[0] == 200          # it didn't go: no wait
    io = capfd.readouterr()
    assert not leaks(raw, io.out, io.err, *everything(hub))


def test_no_answer_may_have_saved_so_it_is_audited_and_counted(hub, capfd):
    hub.ssm.raise_on_put = TimeoutError(f"timed out sending {SECRET}")
    code, out, raw = post(hub, {"password": SECRET, "again": SECRET})
    assert code == 504 and out == {"error": "No answer from AWS", "maybe_saved": True}
    hub.ssm.raise_on_put = None
    audit = hub.cad.store.q("SELECT new FROM settings_audit WHERE key = 'station adapter password'")
    assert audit[0]["new"] == "no answer (network): check the version"
    assert post(hub, {"password": SECRET, "again": SECRET})[0] == 429
    hub.clock.t += 301
    hub.ssm.put_answer = (500, json.dumps({"__type": "InternalServerError", "message": SECRET}).encode())
    code, out, raw2 = post(hub, {"password": SECRET, "again": SECRET})
    assert code == 502 and out["maybe_saved"] and out["error"] == "AWS: InternalServerError"
    st = get(hub)[1]
    assert st["saves"][0]["result"].startswith("no answer (InternalServerError)")
    io = capfd.readouterr()
    assert not leaks(raw, raw2, io.out, io.err, *everything(hub))


def test_an_unexpected_failure_is_a_fixed_500_and_its_text_goes_nowhere(hub, capfd, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError(f"broke on {a[0].reveal()}")          # the worst case: a bug that puts it in its text
    monkeypatch.setattr(hub.ap, "save", boom)
    code, out, raw = post(hub, {"password": SECRET, "again": SECRET})
    assert code == 500 and out == {"error": "Failed"}
    io = capfd.readouterr()
    assert "adapter password: save failed: RuntimeError" in io.out
    assert not leaks(raw, io.out, io.err, *everything(hub))


def test_aws_credentials_missing_or_ssm_unconfigured(hub, tmp_path):
    def none():
        raise aws.AwsError("no AWS credentials", "NoCredentials")
    hub.ap.creds = none
    code, out, _ = post(hub, {"password": SECRET, "again": SECRET})
    assert code == 502 and out["error"] == "No AWS credentials"
    hub.ap._name = ""
    code, st, _ = get(hub)
    assert not st["configured"] and st["why_not"] == "Not configured"
    assert post(hub, {"password": SECRET, "again": SECRET})[0] == 404


def test_one_save_at_a_time():
    gate, done = threading.Event(), threading.Event()
    ssm = FakeSsm()

    def slow(url, headers, body, timeout):
        if headers["X-Amz-Target"].endswith("PutParameter"):
            gate.wait(5)
        return ssm(url, headers, body, timeout)
    cad = make_cad()
    ap = AdapterPassword(NAME, KEY, send=slow, log=lambda m: None, creds=lambda: aws.Credentials("AKIDEXAMPLE", "x"))
    t = threading.Thread(target=lambda: (ap.save(Hidden("a"), Hidden("a"), False, COREY, "Corey", cad.store), done.set()))
    t.start()
    for _ in range(100):
        if ap._busy:
            break
        threading.Event().wait(0.01)
    with pytest.raises(SaveRefused) as e:
        ap.save(Hidden("b"), Hidden("b"), False, COREY, "Corey", cad.store)
    assert e.value.status == 409
    gate.set()
    t.join(5)
    assert done.is_set() and len(ssm.puts()) == 1


def test_the_body_buffer_is_zeroed_after_sending():
    seen = []

    def keep(url, headers, body, timeout):
        seen.append(body)
        return 200, b'{"Version": 9}'
    cad = make_cad()
    ap = AdapterPassword(NAME, KEY, send=keep, log=lambda m: None, creds=lambda: aws.Credentials("AKIDEXAMPLE", "x"))
    new = Hidden(SECRET)
    assert ap.save(new, Hidden(SECRET), False, COREY, "Corey", cad.store)["version"] == 9
    put = seen[-1]
    assert isinstance(put, bytearray) and set(put) == {0} and new.reveal() == ""


def test_the_activity_summary_never_takes_the_body():
    said = summarize("/api/cad/adapter-password", {"password": SECRET, "again": SECRET}, {"version": 3}, None)
    assert said == ("Saved the station adapter password (v3)", "") and not leaks(said)


# -- the page --------------------------------------------------------------------------------------------------------

def test_the_card_is_write_only_and_keeps_nothing():
    js = (ROOT / "sizeup" / "web" / "static" / "adapterpass.js").read_text()
    html = (ROOT / "sizeup" / "web" / "static" / "cad.html").read_text()
    assert '<script src="/static/adapterpass.js"></script>' in html
    assert html.index('src="/static/adapterpass.js"') < html.index('src="/static/health.js"')
    assert 'i.type = "password"' in js and '"new-password"' in js
    assert '"form"' not in js and "<form" not in js                    # never a form that could GET it into a URL
    assert 'pw1.value = ""; pw2.value = "";' in js                     # emptied as the request goes
    assert "X-Sizeup-Action\": \"adapter-password\"" in js
    for bad in ("localStorage", "sessionStorage", "console.", "store("):
        assert bad not in js, bad
    assert "1 log-on attempt on the shared county account" in js       # the warning on screen
    assert "AdapterPasswordCard.mount(node, perfCard)" in (ROOT / "sizeup" / "web" / "static" / "health.js").read_text()
