"""An adapter credential kept in AWS SSM (sizeup/cloud/adapterssm.py) and `sizeup-station adapter setup`'s SSM option:
a fake SSM (no network), synthetic keys and values only. Never a value in an error, a repr or setup's output."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from sizeup.cloud import adapterssm as ssm
from sizeup.cloud import autoupdate as au
from tests.test_station_adapter import PUB, _current_release_with_configupdate, station  # noqa: F401 - the fixture

KEY_ID = "AKIAEXAMPLEEXAMPLE00"                          # synthetic
SECRET = "exampleSECRETexampleSECRETexample/+EXMPL"     # synthetic, 40 characters
VALUE = "Zq9-synthetic-pass!word"                       # synthetic: must never appear in any text


ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def key_checks(monkeypatch):
    """setup's one signed GetParameter of a new key pair: answered here (no network). .answer: "" = it reads."""
    calls = []

    def check(self, key_id, secret, region, name):
        calls.append((key_id, secret, region, name))
        return check.answer
    check.answer = ""
    check.calls = calls
    monkeypatch.setattr(au.Updater, "ssm_check", check)
    return check


def reader(**over):
    vals = {ssm.KEY_ID: KEY_ID, ssm.SECRET: SECRET, ssm.REGION: "us-east-1", **over}
    return lambda name: vals.get(name, "")


class FakeSsm:
    """Answers GetParameter as SSM does: the parameters it has, ParameterNotFound for the rest."""

    def __init__(self, params=None, fail=None):
        self.params = params if params is not None else {}
        self.fail = fail
        self.calls = []

    def __call__(self, url, headers, body, timeout):
        self.calls.append((url, headers, json.loads(body)))
        if self.fail is not None:
            raise self.fail
        name = json.loads(body)["Name"]
        if name not in self.params:
            return 400, json.dumps({"__type": "ParameterNotFound"}).encode()
        value, version = self.params[name]
        return 200, json.dumps({"Parameter": {"Name": name, "Type": "SecureString", "Value": value, "Version": version,
                                              "LastModifiedDate": 1.7e9}}).encode()


def test_only_a_long_term_user_key_id_is_taken():
    for bad in ("ASIAEXAMPLEEXAMPLE00", "AKIAEXAMPLE", "AKIAexampleEXAMPLE00", "AKIAEXAMPLEEXAMPLE00X"):
        assert ssm.problem(ssm.KEY_ID, bad) and not au.SSM_CHECKS[au.SSM_KEY_ID][0].fullmatch(bad)
    assert not ssm.problem(ssm.KEY_ID, KEY_ID)


def test_the_updater_and_the_helper_agree_on_the_names():
    # autoupdate.py runs as root with -I and never imports sizeup, so it has its own copy
    assert (au.SSM_PREFIX, au.SSM_NAMES, au.SSM_DEFAULT_REGION) == (ssm.PREFIX, ssm.NAMES, ssm.DEFAULT_REGION)
    for name, (rx, _) in au.SSM_CHECKS.items():
        assert rx.pattern == {ssm.REGION: ssm.REGION_RE, ssm.KEY_ID: ssm.KEY_ID_RE, ssm.SECRET: ssm.SECRET_RE}[name].pattern
    assert ssm.PREFIX == "/sizeup-station/adapter/"


def test_get_signs_one_request_per_name_and_leaves_missing_ones_out():
    fake = FakeSsm({ssm.PREFIX + "FEED_PASS": (VALUE, 7)})
    got = ssm.get(["FEED_PASS", "FEED_USER"], read=reader(), send=fake)
    assert list(got) == ["FEED_PASS"]
    p = got["FEED_PASS"]
    assert (p.value, p.version, p.modified) == (VALUE, 7, 1.7e9)
    assert VALUE not in repr(p) and VALUE not in str(p) and "v7" in repr(p)
    url, headers, body = fake.calls[0]
    assert url == "https://ssm.us-east-1.amazonaws.com/"
    assert body == {"Name": "/sizeup-station/adapter/FEED_PASS", "WithDecryption": True}
    assert headers["X-Amz-Target"] == "AmazonSSM.GetParameter"
    assert headers["Authorization"].startswith(f"AWS4-HMAC-SHA256 Credential={KEY_ID}/")
    assert "/us-east-1/ssm/aws4_request" in headers["Authorization"]
    assert SECRET not in json.dumps(headers)                 # the secret signs; it is never sent
    assert "Host" not in headers                             # (urllib sets it)


def test_get_uses_the_configured_region():
    fake = FakeSsm()
    assert ssm.get(["FEED_PASS"], read=reader(**{ssm.REGION: "us-west-2"}), send=fake) == {}
    assert fake.calls[0][0] == "https://ssm.us-west-2.amazonaws.com/"


@pytest.mark.parametrize("fail, code", [(OSError("no route to host"), "network"), (TimeoutError(), "network")])
def test_network_problems_are_a_code_only(fail, code):
    with pytest.raises(ssm.SsmError) as e:
        ssm.get(["FEED_PASS"], read=reader(), send=FakeSsm(fail=fail))
    assert str(e.value) == code and e.value.__cause__ is None and e.value.__suppress_context__


def test_an_aws_refusal_is_its_type_never_its_message():
    msg = "User: arn:aws:iam::000000000000:user/sizeup-station-adapter is not authorized"

    def denied(url, headers, body, timeout):
        return 400, json.dumps({"__type": "com.amazon.coral.service#AccessDeniedException", "message": msg}).encode()

    with pytest.raises(ssm.SsmError) as e:
        ssm.get(["FEED_PASS"], read=reader(), send=denied)
    assert str(e.value) == "AccessDeniedException" and e.value.status == 400
    assert "arn:" not in str(e.value)

    with pytest.raises(ssm.SsmError) as e:
        ssm.get(["FEED_PASS"], read=reader(), send=lambda *a: (503, b"<html>" + VALUE.encode()))
    assert str(e.value) == "HTTP 503"


def test_a_bad_answer_never_carries_the_body():
    with pytest.raises(ssm.SsmError) as e:
        ssm.get(["FEED_PASS"], read=reader(), send=lambda *a: (200, b'{"Parameter": {"Value": "' + VALUE.encode()))
    assert str(e.value) == "bad answer" and VALUE not in repr(e.value)
    with pytest.raises(ssm.SsmError) as e:
        ssm.get(["FEED_PASS"], read=reader(), send=FakeSsm({ssm.PREFIX + "FEED_PASS": (VALUE + "\nx", 2)}))
    assert str(e.value) == "bad value"


def test_without_a_key_nothing_is_asked():
    fake = FakeSsm()
    assert not ssm.configured(reader(**{ssm.KEY_ID: ""}))
    assert ssm.configured(reader())
    with pytest.raises(ssm.SsmError, match="no key"):
        ssm.get(["FEED_PASS"], read=reader(**{ssm.SECRET: ""}), send=fake)
    with pytest.raises(ssm.SsmError, match="bad region"):
        ssm.get(["FEED_PASS"], read=reader(**{ssm.REGION: "x/../evil"}), send=fake)
    with pytest.raises(ssm.SsmError, match="bad name"):
        ssm.get(["../FEED"], read=reader(), send=fake)
    assert fake.calls == []


def test_the_hub_has_no_key(monkeypatch):
    # the hub never has a credentials directory (sizeup/procenv.py): credential() is "" there, so never configured
    from sizeup import procenv
    monkeypatch.setattr(procenv, "_dir", None)
    assert not ssm.configured()
    with pytest.raises(ssm.SsmError, match="no key"):
        ssm.get(["FEED_PASS"], send=FakeSsm())


# -- sudo sizeup-station adapter setup: the SSM option --------------------------------------------------------------

def _key(key):
    key.write_text("PRIVATE\n")
    key.with_name(key.name + ".pub").write_text(PUB + "\n")


def test_setup_keeps_the_station_key_in_the_credstore(station):  # noqa: F811
    repo, u, ad = station
    _current_release_with_configupdate(u)
    asked, hidden, said = [], [], []
    answers = iter(["git@github.com:example-org/feed-adapter.git", "", "FEED_USER, FEED_PASS", "y", ""])
    secrets = iter(["not-a-key", KEY_ID, SECRET, "local-user", ""])
    rc = au.adapter_setup(u, ask=lambda p: (asked.append(p), next(answers))[1],
                          secret=lambda p: (hidden.append(p), next(secrets))[1], out=said.append, make_key=_key,
                          check=False)
    assert rc == 0
    text = "\n".join(said + asked + hidden)
    for v in (KEY_ID, SECRET, "local-user", "not-a-key"):
        assert v not in text                                  # never echoed, not even a wrong one
    assert "/sizeup-station/adapter/FEED_USER" in text and "/sizeup-station/adapter/FEED_PASS" in text
    assert any("hidden" in p and "SIZEUP_SSM_KEY_ID" in p for p in hidden)
    assert any("hidden" in p and "SIZEUP_SSM_SECRET" in p for p in hidden)
    assert "  SIZEUP_SSM_KEY_ID: an access key id (AKIA...)" in said     # asked again
    assert u.cred_file(au.SSM_KEY_ID).read_text() == KEY_ID
    assert u.cred_file(au.SSM_SECRET).read_text() == SECRET
    assert u.cred_file(au.SSM_REGION).read_text() == "us-east-1"
    assert stat.S_IMODE(u.cred_file(au.SSM_SECRET).stat().st_mode) == 0o600
    assert u.cred_file("FEED_USER").read_text() == "local-user"         # the local fallback
    assert not u.cred_file("FEED_PASS").exists()                         # Enter: SSM only
    assert tomllib.loads(u.p.config.read_text())["adapter"]["env"] == [
        "FEED_USER", "FEED_PASS", "SIZEUP_SSM_KEY_ID", "SIZEUP_SSM_SECRET", "SIZEUP_SSM_REGION"]
    assert KEY_ID not in u.p.config.read_text() and SECRET not in u.p.config.read_text()

    # again: SSM is the default now, Enter keeps the key; the names prompt doesn't offer SSM's own
    asked.clear()
    answers = iter(["", "", "", "", ""])                     # (FEED_USER isn't offered for removal: not a password)
    assert au.adapter_setup(u, ask=lambda p: (asked.append(p), next(answers))[1], secret=lambda p: "",
                            out=said.append, make_key=_key, check=False) == 0
    assert "Credential names, comma-separated [FEED_USER,FEED_PASS]: " in asked
    assert any("[Y/n]" in p for p in asked)
    assert u.cred_file(au.SSM_SECRET).read_text() == SECRET

    # and n: back to local only, the key out of the credstore
    answers = iter(["", "", "", "n"])
    assert au.adapter_setup(u, ask=lambda p: next(answers), secret=lambda p: "", out=said.append, make_key=_key,
                            check=False) == 0
    assert not u.cred_file(au.SSM_KEY_ID).exists() and not u.cred_file(au.SSM_SECRET).exists()
    assert tomllib.loads(u.p.config.read_text())["adapter"]["env"] == ["FEED_USER", "FEED_PASS"]


def test_setup_gives_up_on_a_key_that_never_fits(station):  # noqa: F811
    repo, u, ad = station
    _current_release_with_configupdate(u)
    said = []
    answers = iter(["git@github.com:example-org/feed-adapter.git", "", "FEED_PASS", "y", "us-east-1"])
    assert au.adapter_setup(u, ask=lambda p: next(answers), secret=lambda p: "nope", out=said.append, make_key=_key,
                            check=False) == 2
    assert "  SSM not set up: nothing changed" in said
    assert not u.cred_file(au.SSM_KEY_ID).exists() and "adapter" not in tomllib.loads(u.p.config.read_text())


def _ssm_setup(u, answers, secrets, said=None, asked=None):
    answers, secrets = iter(answers), iter(secrets)
    said = [] if said is None else said
    asked = [] if asked is None else asked
    return au.adapter_setup(u, ask=lambda p: (asked.append(p), next(answers))[1], secret=lambda p: next(secrets),
                            out=said.append, make_key=_key, check=False)


def _with_adapter_installed(u):
    rel = u.p.releases / u.current()[0]
    (rel / au.ADAPTER_FILE).write_text(json.dumps({"commit": "a" * 40}))


def test_choosing_ssm_removes_the_local_password_by_default_and_restarts(station_ssm):
    repo, u, ad = station_ssm
    _current_release_with_configupdate(u)
    _with_adapter_installed(u)
    assert _ssm_setup(u, ["git@github.com:example-org/feed-adapter.git", "", "FEED_USER, FEED_PASS", ""],
                      ["local-user", VALUE]) == 0                         # local only, first
    assert u.cred_file("FEED_PASS").read_text() == VALUE
    u.systemctls.clear()
    asked, said = [], []
    # SSM: y, region Enter, then "remove the local values?" Enter = yes
    assert _ssm_setup(u, ["", "", "", "y", "", ""], [KEY_ID, SECRET, ""], said, asked) == 0
    assert any(p.startswith("Remove the copy kept here of FEED_PASS (SSM has it)") and "[Y/n]" in p for p in asked)
    assert not any("FEED_USER" in p for p in asked if p.startswith("Remove"))
    assert not u.cred_file("FEED_PASS").exists()
    assert u.cred_file("FEED_USER").read_text() == "local-user"          # the user stays: SSM has only the password
    assert ("try-restart", "sizeup-station.service") in u.systemctls        # the new key reaches the adapter
    assert u.dropin_problem() == ""
    # "from AWS SSM" only for what the adapter last got from SSM (its ssm.json: names only)
    r = u.report()["adapter"]
    assert ("FEED_PASS", False) in r["env"] and ("FEED_USER", True) in r["env"]
    (u.p.sources_state / "cadfeed").mkdir(parents=True)
    (u.p.sources_state / "cadfeed" / "ssm.json").write_text(json.dumps({"names": ["FEED_PASS"], "at": 1.7e9}))
    r = u.report()["adapter"]
    assert ("FEED_PASS", "ssm") in r["env"] and ("FEED_USER", True) in r["env"] and (au.SSM_KEY_ID, True) in r["env"]
    assert r["ssm_key"] == KEY_ID[-4:] and KEY_ID not in json.dumps(r)
    doc = "\n".join(au.doctor_lines(u.report()))
    assert "ok    FEED_PASS: from AWS SSM (/sizeup-station/adapter/FEED_PASS)" in doc and "FAIL  FEED_PASS" not in doc
    for v in (VALUE, KEY_ID, SECRET, "local-user"):
        assert v not in "\n".join(said + asked)


def test_keeping_the_local_value_is_an_explicit_no(station):  # noqa: F811
    repo, u, ad = station
    _current_release_with_configupdate(u)
    assert _ssm_setup(u, ["git@github.com:example-org/feed-adapter.git", "", "FEED_PASS", ""], [VALUE]) == 0
    assert _ssm_setup(u, ["", "", "", "y", "", "n"], [KEY_ID, SECRET, ""]) == 0
    assert u.cred_file("FEED_PASS").read_text() == VALUE


def test_a_new_station_key_alone_restarts_the_adapter(station):  # noqa: F811
    repo, u, ad = station
    _current_release_with_configupdate(u)
    _with_adapter_installed(u)
    assert _ssm_setup(u, ["git@github.com:example-org/feed-adapter.git", "", "FEED_PASS", "y", ""],
                      [KEY_ID, SECRET, ""]) == 0
    u.systemctls.clear()
    assert _ssm_setup(u, ["", "", "", "", ""], ["", "", "", ""]) == 0         # Enter everywhere: nothing changed
    assert ("try-restart", "sizeup-station.service") not in u.systemctls
    new_id = "AKIAEXAMPLEEXAMPLE11"
    assert _ssm_setup(u, ["", "", "", "", ""], [new_id, "", ""]) == 0          # only the key id: restarted
    assert ("try-restart", "sizeup-station.service") in u.systemctls
    assert u.report()["adapter"]["ssm_key"] == "LE11"


@pytest.fixture
def station_ssm(station, tmp_path):  # noqa: F811
    repo, u, ad = station
    u.p.sources_state = tmp_path / "var-lib-sizeup" / "sources"
    return station


def test_setup_refuses_a_key_pair_that_doesnt_read(station_ssm, key_checks):
    repo, u, ad = station_ssm
    _current_release_with_configupdate(u)
    key_checks.answer = "InvalidSignatureException"
    said = []
    assert _ssm_setup(u, ["git@github.com:example-org/feed-adapter.git", "", "FEED_USER, FEED_PASS", "y", ""],
                      [KEY_ID, SECRET], said) == 2
    assert key_checks.calls == [(KEY_ID, SECRET, "us-east-1", "FEED_PASS")]      # the password's parameter
    text = "\n".join(said)
    assert "InvalidSignatureException" in text and "Nothing changed" in text
    assert KEY_ID not in text and SECRET not in text
    assert not u.cred_file(au.SSM_KEY_ID).exists() and "adapter" not in tomllib.loads(u.p.config.read_text())
    # it reads: saved; then Enter everywhere: no new pair, no check
    key_checks.answer = ""
    assert _ssm_setup(u, ["git@github.com:example-org/feed-adapter.git", "", "FEED_USER, FEED_PASS", "y", ""],
                      [KEY_ID, SECRET, "local-user", ""], said) == 0
    assert "  SSM key ok: it reads /sizeup-station/adapter/FEED_PASS" in said
    n = len(key_checks.calls)
    assert _ssm_setup(u, ["", "", "", "", ""], ["", "", "", ""]) == 0
    assert len(key_checks.calls) == n
    # a new secret alone is checked with the stored id
    key_checks.answer = "AccessDeniedException"
    assert _ssm_setup(u, ["", "", "", "", ""], ["", "exampleSECRETexampleSECRETexample/+OTHER"]) == 2
    assert key_checks.calls[-1][0] == KEY_ID and u.cred_file(au.SSM_SECRET).read_text() == SECRET


def test_the_default_ssm_path_keeps_the_user_and_drops_the_password(station_ssm):
    """What 15-station-adapter.sh then sudo sizeup-station adapter setup do: SSM has the password only; the user
    stays here, so the adapter has both and logs on."""
    repo, u, ad = station_ssm
    _current_release_with_configupdate(u)
    assert _ssm_setup(u, ["git@github.com:example-org/feed-adapter.git", "", "SIZEUP_COUNTY_USER, SIZEUP_COUNTY_PASS",
                          ""], ["synthetic-user", VALUE]) == 0
    asked = []
    # Enter for repo, branch, names; y; Enter region; Enter = yes, remove the password kept here
    assert _ssm_setup(u, ["", "", "", "y", "", ""], [KEY_ID, SECRET, "", ""], asked=asked) == 0
    assert u.cred_file("SIZEUP_COUNTY_USER").read_text() == "synthetic-user"
    assert not u.cred_file("SIZEUP_COUNTY_PASS").exists()
    # the adapter's view: the user from here, the password from SSM (the helper, with a fake SSM)
    files = {p.name[len("sizeup-adapter."):]: p.read_text() for p in u.p.credentials.iterdir()}
    fake = FakeSsm({ssm.PREFIX + "SIZEUP_COUNTY_PASS": (VALUE, 2)})
    got = ssm.get(["SIZEUP_COUNTY_PASS", "SIZEUP_COUNTY_USER"], read=lambda n: files.get(n, ""), send=fake)
    assert list(got) == ["SIZEUP_COUNTY_PASS"] and got["SIZEUP_COUNTY_PASS"].value == VALUE
    assert files["SIZEUP_COUNTY_USER"] == "synthetic-user"                # both halves of the login are there


def test_check_reads_the_version_only():
    fake = FakeSsm({ssm.PREFIX + "FEED_PASS": (VALUE, 4)})
    assert ssm.check(KEY_ID, SECRET, "us-east-1", "FEED_PASS", send=fake) == 4
    with pytest.raises(ssm.SsmError, match="ParameterNotFound"):
        ssm.check(KEY_ID, SECRET, "us-east-1", "FEED_USER", send=fake)


def test_the_check_runs_as_the_updater_runs_it(tmp_path):
    """autoupdate.py's way: the system interpreter, -I -S, the pair on stdin, one line out; never the pair."""
    code = (f"import sys; sys.path.insert(0, {str(ROOT)!r}); "
            "from sizeup.cloud.adapterssm import _main; sys.exit(_main())")
    r = subprocess.run([sys.executable, "-I", "-S", "-c", code], cwd="/", capture_output=True, text=True, timeout=60,
                       input=json.dumps({"key_id": KEY_ID, "secret": SECRET, "region": "us-east-1", "name": "FEED_PASS",
                                         "endpoint": "http://127.0.0.1:9"}))
    assert (r.returncode, r.stdout.strip()) == (1, "error network")
    assert SECRET not in r.stdout + r.stderr and KEY_ID not in r.stdout + r.stderr


def test_the_updater_runs_the_check_from_the_release(station_ssm, monkeypatch):
    repo, u, ad = station_ssm
    monkeypatch.undo()                                  # the real Updater.ssm_check (key_checks patched it)
    monkeypatch.setenv(au.ALLOW_LOCAL, "1")
    rel = Path(os.path.realpath(u.p.current))
    assert u.ssm_check(KEY_ID, SECRET, "us-east-1", "FEED_PASS").startswith("not checked")
    (rel / "sizeup").mkdir(exist_ok=True)
    (rel / "sizeup" / "cloud").mkdir(exist_ok=True)
    for f in ("__init__.py", "aws.py", "procenv.py", "cloud/__init__.py", "cloud/adapterssm.py"):
        (rel / "sizeup" / f).write_text((ROOT / "sizeup" / f).read_text())
    # no network in tests: a region that resolves nowhere would still try DNS, so point the helper at a closed port
    src = (rel / "sizeup" / "cloud" / "adapterssm.py").read_text()
    (rel / "sizeup" / "cloud" / "adapterssm.py").write_text(
        src.replace('endpoint=d.get(\'endpoint\', \'\')', 'endpoint="http://127.0.0.1:9"'))
    assert u.ssm_check(KEY_ID, SECRET, "us-east-1", "FEED_PASS") == "network"
