"""An adapter credential kept in AWS SSM (sizeup/cloud/adapterssm.py) and `sizeup-station adapter setup`'s SSM option:
a fake SSM (no network), synthetic keys and values only. Never a value in an error, a repr or setup's output."""

from __future__ import annotations

import json
import stat
import tomllib

import pytest

from sizeup.cloud import adapterssm as ssm
from sizeup.cloud import autoupdate as au
from tests.test_station_adapter import PUB, _current_release_with_configupdate, station  # noqa: F401 - the fixture

KEY_ID = "AKIAEXAMPLEEXAMPLE00"                          # synthetic
SECRET = "exampleSECRETexampleSECRETexample/+EXMPL"     # synthetic, 40 characters
VALUE = "Zq9-synthetic-pass!word"                       # synthetic: must never appear in any text


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
    answers = iter(["", "", "", "", ""])
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
