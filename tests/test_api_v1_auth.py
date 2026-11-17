"""The app's device sign-in (#558, docs/mobile-api.md section 3.3): Access hands off to hub-issued per-device tokens.

The values are invented (example.org, made-up ids and secrets). The service runs against a real hub.sqlite (MonitorDb)
and, for the console's half, a real hub app with real operators. Cloudflare Access is a fake that names the person in
X-Test-User (its verifier is tested in test_cloud_access.py)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import re
import secrets
import sqlite3
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

from sizeup.api.v1 import auth as auth_mod
from sizeup.api.v1.auth import AuthConfig, AuthService, Client
from sizeup.api.v1.devices import ABSOLUTE_S, IDLE_S, MAX_DEVICES, DeviceDb
from sizeup.api.v1.handler import V1
from sizeup.api.v1.limits import Limits
from sizeup.api.v1.people import People
from sizeup.api.v1.principal import Principal
from sizeup.api.v1.routes import ROUTES, Route
from sizeup.api.v1.switches import Switches
from sizeup.api.v1.tokens import ACCESS_TTL_S, ENV, Keys, TokenError
from sizeup.api.v1.wire import make_auth
from sizeup.cloud.access import Identity
from sizeup.cloud.hubstore import MonitorDb
from sizeup.web.server import WebServer

SECRET = "k1-0123456789abcdefghijklmnopqrstuvwxyz-example"          # invented: 32+ characters
SECRET2 = "k2-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-example"
CLIENT = "sizeup-ios"
LINK = "https://hub.example.org/app/callback"
SCHEME = "org.example.sizeup:/callback"
CASEY, RIVA, OTIS, EVE = ("casey@example.org", "riva@example.org", "otis@example.org", "eve@example.org")
UA = "sizeup/1.2.0 (iPhone; iOS 18.1) CFNetwork"


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeAccess:
    """Cloudflare Access as far as authorize goes: the person X-Test-User names, with an Access session id."""

    def identify(self, headers):
        e = headers.get("X-Test-User")
        return Identity(email=e, session="sess-1") if e else None


class Directory:
    """Settings > Users as the service reads it: {email: {name, role, active}} and the settings rev."""

    def __init__(self):
        self.rows = {CASEY: {"name": "Casey Rivera", "role": "dispatcher", "active": 1},
                     RIVA: {"name": "Riva Stone", "role": "admin", "active": 1},
                     OTIS: {"name": "Otis Lane", "role": "viewer", "active": 1}}
        self.rev = 1

    def set(self, email, **kw):
        self.rows[email] = {**self.rows.get(email, {"name": email, "role": "dispatcher", "active": 1}), **kw}
        self.rev += 1


class Rig:
    """A service over a real in-memory hub.sqlite, a clock, a directory and an activity list."""

    def __init__(self, auth_on=True, clients=None):
        self.clock, self.dir, self.activity, self.logs = Clock(), Directory(), [], []
        self.pace = Clock(0.0)               # the rate limits' clock: each call moves it on (see `walk`), unless a test holds it
        self.walk = 10.0
        self.flags = {"auth": auth_on}
        self.switches = Switches(lambda: True, lambda: self.flags)
        self.mdb = MonitorDb(":memory:")
        self.db = DeviceDb(self.mdb, self.clock)
        self.keys = Keys({"k1": SECRET})
        self.config = AuthConfig(clients or (Client(CLIENT, "ios", (LINK, SCHEME)),
                                             Client("sizeup-android", "android", ("https://hub.example.org/app/android",))),
                                 "ABCDE12345", ("org.example.sizeup",), "org.example.sizeup",
                                 ("AA:" * 31 + "AA",))
        self.svc = AuthService(self.db, self.keys, People(lambda e: self.dir.rows.get(e), lambda: self.dir.rev),
                               FakeAccess(), self.config, self.switches,
                               lambda *a: self.activity.append(a), self.logs.append, self.clock,
                               _limit=auth_mod.AddrLimit(self.pace))
        self.v1 = V1(switches=self.switches, auth=self.svc, log=self.logs.append, limits=Limits(8, 16, self.pace))

    # -- one request through the real handler ---------------------------------------------------------------------

    def call(self, method, path, query="", headers=None, body=None, peer="198.51.100.7"):
        h = {"User-Agent": UA, "Cf-Connecting-Ip": peer, **(headers or {})}
        data = body if isinstance(body, bytes) else urllib.parse.urlencode(body or {}).encode()
        self.pace.t += self.walk
        status, raw, ctype, out = self.v1.respond(method, path, query, h, peer, lambda limit: data if len(data) <= limit else None)
        return status, raw, ctype, out

    def authorize(self, who=CASEY, verifier=None, state="st-1", redirect=LINK, client=CLIENT, **extra):
        verifier = verifier or secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        q = {"response_type": "code", "client_id": client, "redirect_uri": redirect, "code_challenge": challenge,
             "code_challenge_method": "S256", "state": state, "device_name": "Casey's phone", **extra}
        s, raw, ctype, out = self.call("GET", "/auth/v1/authorize", urllib.parse.urlencode(q),
                                       {"X-Test-User": who} if who else {})
        return s, raw, out, verifier

    def code_of(self, out) -> tuple[str, str]:
        loc = urllib.parse.urlsplit(out["Location"])
        qs = urllib.parse.parse_qs(loc.query)
        return qs["code"][0], qs["state"][0]

    def exchange(self, code, verifier, redirect=LINK, client=CLIENT, **extra):
        s, raw, ctype, out = self.call("POST", "/auth/v1/token", body={
            "grant_type": "authorization_code", "code": code, "code_verifier": verifier, "client_id": client,
            "redirect_uri": redirect, "app_version": "1.2.0", **extra})
        return s, json.loads(raw), out

    def refresh(self, token, **extra):
        s, raw, ctype, out = self.call("POST", "/auth/v1/token", body={"grant_type": "refresh_token",
                                                                       "refresh_token": token, **extra})
        return s, json.loads(raw), out

    def sign_in(self, who=CASEY, **kw) -> dict:
        s, raw, out, verifier = self.authorize(who, **kw)
        assert s == 302, raw
        code, _ = self.code_of(out)
        status, tok, _ = self.exchange(code, verifier)
        assert status == 200, tok
        return tok

    def api(self, path="/api/v1/me", token=None, headers=None):
        h = {**({"Authorization": f"Bearer {token}"} if token else {}), **(headers or {})}
        s, raw, ctype, out = self.call("GET", path, headers=h)
        return s, json.loads(raw)


@pytest.fixture
def rig():
    return Rig()


def code(r):
    return r[1]["error"]["code"] if "error" in r[1] and isinstance(r[1]["error"], dict) else r[1].get("error")


# -- the tokens ------------------------------------------------------------------------------------------------------

def forge(header: dict, payload: dict, secret=SECRET) -> str:
    """A token as an attacker with the algorithm wrong but a MAC that would be right for HS256 builds it."""
    e = lambda o: base64.urlsafe_b64encode(json.dumps(o, separators=(",", ":")).encode()).rstrip(b"=").decode()
    h, p = e(header), e(payload)
    sig = base64.urlsafe_b64encode(hmac.new(secret.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest()).rstrip(b"=")
    return f"{h}.{p}.{sig.decode()}"


def test_a_token_has_kid_sub_did_iat_exp_and_verifies_within_its_lifetime():
    keys, now = Keys({"k1": SECRET}), 1_800_000_000.0
    t = keys.sign(CASEY, "dv0123456789abcdef", now)
    h, p, s = t.split(".")
    pad = lambda x: x + "=" * (-len(x) % 4)
    assert json.loads(base64.urlsafe_b64decode(pad(h))) == {"alg": "HS256", "kid": "k1", "typ": "at+jwt"}
    assert json.loads(base64.urlsafe_b64decode(pad(p))) == {"sub": CASEY, "did": "dv0123456789abcdef", "iat": int(now),
                                                            "exp": int(now) + ACCESS_TTL_S}
    assert ACCESS_TTL_S == 900
    assert keys.verify(t, now + 899)["sub"] == CASEY
    with pytest.raises(TokenError) as e:
        keys.verify(t, now + 900 + 31)
    assert e.value.expired
    assert keys.verify(t, now + 900 + 29)["did"] == "dv0123456789abcdef"            # the leeway is under a minute
    assert keys.verify(t, now + 5000, allow_expired=True)["sub"] == CASEY            # a logout still proves the device


def test_the_algorithm_is_fixed_whatever_the_header_says():
    now = 1_800_000_000.0
    payload = {"sub": CASEY, "did": "dv0123456789abcdef", "iat": int(now), "exp": int(now) + 900}
    keys = Keys({"k1": SECRET})
    good = forge({"alg": "HS256", "kid": "k1", "typ": "at+jwt"}, payload)
    assert keys.verify(good, now)["sub"] == CASEY                       # (the forge helper makes the real thing)
    for alg in ("none", "HS512", "RS256", "hs256", "HS384", ""):
        with pytest.raises(TokenError):
            keys.verify(forge({"alg": alg, "kid": "k1", "typ": "at+jwt"}, payload), now)
    with pytest.raises(TokenError):                                      # alg none with no signature at all
        keys.verify(good.rsplit(".", 1)[0].replace(good.split(".")[0], base64.urlsafe_b64encode(
            b'{"alg":"none","kid":"k1","typ":"at+jwt"}').rstrip(b"=").decode()) + ".", now)
    with pytest.raises(TokenError):                                      # no alg, extra header fields
        keys.verify(forge({"kid": "k1", "typ": "at+jwt"}, payload), now)
    with pytest.raises(TokenError):
        keys.verify(forge({"alg": "HS256", "kid": "k1", "typ": "at+jwt", "jku": "https://example.org/k"}, payload), now)


def test_tampered_wrong_kid_wrong_key_and_malformed_tokens_are_refused():
    now = 1_800_000_000.0
    keys = Keys({"k1": SECRET, "k2": SECRET2})
    t = keys.sign(CASEY, "dv0123456789abcdef", now)
    h, p, s = t.split(".")
    other = base64.urlsafe_b64encode(json.dumps({"sub": OTIS, "did": "dv0123456789abcdef", "iat": int(now),
                                                 "exp": int(now) + 900}).encode()).rstrip(b"=").decode()
    for bad in (f"{h}.{other}.{s}", f"{h}.{p}.{s[:-2]}AA", f"{h}.{p}", "", "a.b.c", "x" * 2000, f"{h}.{p}.{s}.x",
                f"{h}.{p}.", f"{h}.!!!.{s}", None, 12):
        with pytest.raises(TokenError):
            keys.verify(bad, now)
    assert Keys({"k2": SECRET}).verify(forge({"alg": "HS256", "kid": "k2", "typ": "at+jwt"},
                                              {"sub": CASEY, "did": "d", "iat": int(now), "exp": int(now) + 9}), now)["sub"]
    with pytest.raises(TokenError):                                      # the right MAC under another key id
        Keys({"k1": SECRET}).verify(forge({"alg": "HS256", "kid": "k9", "typ": "at+jwt"},
                                          {"sub": CASEY, "did": "d", "iat": int(now), "exp": int(now) + 9}), now)
    with pytest.raises(TokenError):                                      # signed with another secret
        Keys({"k1": SECRET}).verify(forge({"alg": "HS256", "kid": "k1", "typ": "at+jwt"},
                                          {"sub": CASEY, "did": "d", "iat": int(now), "exp": int(now) + 9}, SECRET2), now)
    with pytest.raises(TokenError):                                      # issued in the future
        keys.verify(keys.sign(CASEY, "d", now + 3600), now)
    for claims in ({"sub": "not-an-email", "did": "d"}, {"sub": CASEY, "did": ""}, {"sub": CASEY, "did": "d", "exp": True}):
        with pytest.raises(TokenError):
            keys.verify(forge({"alg": "HS256", "kid": "k1", "typ": "at+jwt"},
                              {"iat": int(now), "exp": int(now) + 9, **claims}), now)


def test_two_keys_are_valid_during_a_rotation_and_the_first_signs():
    env = {ENV: f"k2:{SECRET2}, k1:{SECRET}"}
    new = Keys.load(env)
    assert new.current == "k2" and new.kids() == ["k2", "k1"]
    old_token = Keys({"k1": SECRET}).sign(CASEY, "dv0123456789abcdef", 1_800_000_000.0)
    assert new.verify(old_token, 1_800_000_001.0)["sub"] == CASEY
    assert new.sign(CASEY, "d", 1.0).startswith(base64.urlsafe_b64encode(b'{"alg":"HS256","kid":"k2","typ":"at+jwt"}').rstrip(b"=").decode())


def test_the_key_must_be_set_and_good_or_the_sign_in_stays_off():
    logs = []
    for env in ({}, {ENV: ""}, {ENV: "k1:short"}, {ENV: "no-colon"}, {ENV: f"k1:{SECRET},k1:{SECRET2}"},
                {ENV: f"bad kid!:{SECRET}"}, {ENV: f":{SECRET}"}):
        assert Keys.load(env, logs.append) is None, env
    assert logs and SECRET not in " ".join(logs)                         # said, never the value


# -- /api/v1: the door ------------------------------------------------------------------------------------------------

def bad_credentials(rig: Rig, device_token: str) -> dict:
    """Everything that is not a good device token, as a (headers) dict, with the code each must answer."""
    now = rig.clock()
    payload = {"sub": CASEY, "did": "dv0123456789abcdef", "iat": int(now), "exp": int(now) + 900}
    head = {"alg": "HS256", "kid": "k1", "typ": "at+jwt"}
    h, p, s = device_token.split(".")
    expired = Keys({"k1": SECRET}).sign(CASEY, device_token and rig.svc.keys.verify(device_token, now)["did"], now - 3600)
    return {
        "no token": ({}, "unauthorized"),
        "expired": ({"Authorization": f"Bearer {expired}"}, "token_expired"),
        "tampered": ({"Authorization": f"Bearer {h}.{p}.{s[:-3]}AAA"}, "unauthorized"),
        "wrong kid": ({"Authorization": "Bearer " + forge({**head, "kid": "k9"}, payload)}, "unauthorized"),
        "alg none": ({"Authorization": "Bearer " + forge({**head, "alg": "none"}, payload)}, "unauthorized"),
        "alg HS512": ({"Authorization": "Bearer " + forge({**head, "alg": "HS512"}, payload)}, "unauthorized"),
        "alg RS256": ({"Authorization": "Bearer " + forge({**head, "alg": "RS256"}, payload)}, "unauthorized"),
        "signed with another key": ({"Authorization": "Bearer " + forge(head, payload, SECRET2)}, "unauthorized"),
        "a station bearer": ({"Authorization": "Bearer sz1.pcfd-hq." + "A" * 43}, "unauthorized"),
        "a feed-style bearer": ({"Authorization": "Bearer " + secrets.token_urlsafe(32)}, "unauthorized"),
        "basic auth": ({"Authorization": "Basic " + base64.b64encode(b"casey:pw").decode()}, "unauthorized"),
        "an Access cookie": ({"Cookie": "CF_Authorization=eyJhbGciOiJSUzI1NiJ9.e30.c2ln", "X-Test-User": CASEY}, "unauthorized"),
        "an Access header": ({"Cf-Access-Jwt-Assertion": "eyJhbGciOiJSUzI1NiJ9.e30.c2ln", "X-Test-User": CASEY}, "unauthorized"),
        "the token in a cookie": ({"Cookie": f"token={device_token}"}, "unauthorized"),
        "an empty bearer": ({"Authorization": "Bearer "}, "unauthorized"),
    }


def device_routes():
    return [r for r in ROUTES if r.need == "device"]


def test_a_good_token_reaches_me_with_the_role_the_settings_have_now(rig):
    tok = rig.sign_in(CASEY)
    s, body = rig.api(token=tok["access_token"])
    assert s == 200 and body == {"email": CASEY, "name": "Casey Rivera", "role": "dispatcher", "device_id": tok["device_id"]}
    rig.dir.set(CASEY, role="viewer", name="Casey R.")
    assert rig.api(token=tok["access_token"])[1]["role"] == "viewer"      # the very next request


def test_every_device_route_is_a_401_for_every_bad_credential(rig):
    tok = rig.sign_in(CASEY)
    cases = bad_credentials(rig, tok["access_token"])
    for route in device_routes():
        path = route.path.replace("{id}", "x")
        for name, (headers, want) in cases.items():
            s, body = rig.api(path, headers=headers)
            assert (s, body["error"]["code"]) == (401, want), (route.path, name)
        s, body = rig.api(path, tok["access_token"])
        assert s == 200, route.path                                          # and the good one still works


def test_a_revoked_device_and_a_deactivated_user_are_a_401_on_every_device_route(rig):
    revoked, gone = rig.sign_in(CASEY), rig.sign_in(OTIS)
    rig.svc.revoke_device(revoked["device_id"], RIVA)
    rig.dir.set(OTIS, active=0)
    for route in device_routes():
        path = route.path.replace("{id}", "x")
        assert rig.api(path, revoked["access_token"])[1]["error"]["code"] == "device_revoked"
        assert rig.api(path, gone["access_token"])[1]["error"]["code"] == "user_inactive"
        assert rig.api(path, revoked["access_token"])[0] == rig.api(path, gone["access_token"])[0] == 401


def test_a_revoke_or_a_deactivation_takes_effect_on_the_next_request(rig):
    a, b = rig.sign_in(CASEY), rig.sign_in(CASEY, device_name="tablet")
    assert rig.api(token=a["access_token"])[0] == rig.api(token=b["access_token"])[0] == 200
    rig.svc.revoke_device(a["device_id"], CASEY, "signed out")
    assert rig.api(token=a["access_token"]) [1]["error"]["code"] == "device_revoked"        # same instant, same token
    assert rig.api(token=b["access_token"])[0] == 200                                       # the other device is not
    rig.dir.set(CASEY, active=0)
    assert rig.api(token=b["access_token"])[1]["error"]["code"] == "user_inactive"
    assert [d["active"] for d in rig.db.devices(CASEY)] == [False, False]                   # and the net revoked it
    rig.dir.set(CASEY, active=1)
    assert rig.api(token=b["access_token"])[1]["error"]["code"] == "device_revoked"        # coming back does not revive it


def test_a_token_for_an_email_that_left_the_list_or_a_changed_email_is_refused(rig):
    t = rig.sign_in(CASEY)
    del rig.dir.rows[CASEY]
    rig.dir.rev += 1
    assert rig.api(token=t["access_token"])[1]["error"]["code"] == "user_inactive"          # never re-added, never a role


def test_a_device_whose_token_names_another_person_is_refused(rig):
    t = rig.sign_in(CASEY)
    did = rig.svc.keys.verify(t["access_token"], rig.clock())["did"]
    other = rig.keys.sign(OTIS, did, rig.clock())                                          # the right MAC, the wrong owner
    assert rig.api(token=other)[1]["error"]["code"] == "unauthorized"


def test_a_role_below_the_routes_is_refused_and_never_runs_the_handler(rig):
    ran = []

    def handler(ctx):
        ran.append(ctx.principal.email)
        return 200, {"ok": True}
    routes = [Route("GET", "/api/v1/disp", handler, "device", "auth", "dispatch"),
              Route("GET", "/api/v1/adm", handler, "device", "auth", "admin"),
              Route("GET", "/api/v1/rd", handler, "device", "auth", "read")]
    v1 = V1(routes, switches=rig.switches, resolver=rig.svc.resolve)
    tok = {e: rig.sign_in(e)["access_token"] for e in (OTIS, CASEY, RIVA)}
    want = {OTIS: (403, 403, 200), CASEY: (200, 403, 200), RIVA: (200, 200, 200)}
    for email, expect in want.items():
        got = tuple(v1.respond("GET", p, "", {"Authorization": f"Bearer {tok[email]}"})[0]
                    for p in ("/api/v1/disp", "/api/v1/adm", "/api/v1/rd"))
        assert got == expect, email
    assert ran.count(OTIS) == 1 and "forbidden" in v1.respond("GET", "/api/v1/adm", "", {"Authorization": f"Bearer {tok[OTIS]}"})[1].decode()
    # a resolver that sets no role gives a viewer (fail closed)
    v1 = V1(routes, switches=rig.switches, resolver=lambda h: Principal("dv0123456789abcdef", CASEY))
    assert v1.respond("GET", "/api/v1/disp", "", {})[0] == 403 and v1.respond("GET", "/api/v1/rd", "", {})[0] == 200


# -- authorize and token ---------------------------------------------------------------------------------------------

def test_authorize_redirects_to_the_app_with_a_code_and_the_state(rig):
    s, raw, out, verifier = rig.authorize(CASEY, state="abc 123&x=1")
    assert s == 302 and raw == b"" and out["Cache-Control"] == "no-store"
    code, state = rig.code_of(out)
    assert out["Location"].startswith(LINK + "?") and state == "abc 123&x=1" and re.fullmatch(r"[A-Za-z0-9_-]{43}", code)
    s, raw, out, _ = rig.authorize(CASEY, redirect=SCHEME)
    assert s == 302 and out["Location"].startswith(SCHEME + "?code=")
    with rig.mdb._lock:                                                                        # stored hashed
        stored = rig.mdb.db.execute("SELECT hash FROM mobile_codes").fetchall()
    assert len(stored) == 2 and code not in json.dumps(stored)


def test_authorize_refuses_with_a_page_never_a_redirect(rig):
    good, _, _, _ = rig.authorize(CASEY)
    assert good == 302
    cases = {
        "no Access person": rig.authorize(None)[:2],
        "unknown user": rig.authorize(EVE)[:2],
        "an unknown client": rig.authorize(CASEY, client="evil-app")[:2],
        "a redirect not on the list": rig.authorize(CASEY, redirect="https://evil.example.com/cb")[:2],
        "a prefix of a good redirect": rig.authorize(CASEY, redirect=LINK + "/../x")[:2],
        "a redirect with a suffix": rig.authorize(CASEY, redirect=LINK + "?next=https://evil.example.com")[:2],
        "http": rig.authorize(CASEY, redirect=LINK.replace("https", "http"))[:2],
        "a no-S256 challenge": rig.authorize(CASEY, code_challenge_method="plain")[:2],
        "a short challenge": rig.authorize(CASEY, code_challenge="abc")[:2],
        "no state": rig.authorize(CASEY, state="")[:2],
        "a huge state": rig.authorize(CASEY, state="s" * 600)[:2],
        "token flow": rig.authorize(CASEY, response_type="token")[:2],
    }
    for name, (s, raw) in cases.items():
        assert s in (400, 401, 403) and raw.startswith(b"<!doctype html>"), name
    out = rig.call("GET", "/auth/v1/authorize", "")                                            # nothing at all
    assert out[0] == 400 and "Location" not in out[3]
    rig.dir.set(OTIS, active=0)
    s, raw, out, _ = rig.authorize(OTIS)
    assert s == 403 and b"deactivated" in raw and "Location" not in out
    assert b"evil" not in b"".join(r for _, r in cases.values())                               # nothing the request said is echoed
    assert out["Content-Security-Policy"].startswith("default-src 'none'") and out["X-Frame-Options"] == "DENY"
    assert rig.call("POST", "/auth/v1/authorize")[0] == 405
    refused = [a for a in rig.activity if a[1] == "refused"]
    assert [a[0] for a in refused] == [EVE, OTIS] and all(a[3] == "198.51.100.7" and a[4] == UA and not a[5] for a in refused)


def test_the_whole_flow_gives_a_device_and_tokens(rig):
    s, raw, out, verifier = rig.authorize(CASEY)
    c, _ = rig.code_of(out)
    status, tok, h = rig.exchange(c, verifier)
    assert status == 200 and h["Cache-Control"] == "no-store" and h["Pragma"] == "no-cache"
    assert tok["token_type"] == "Bearer" and tok["expires_in"] == 900 and re.fullmatch(r"dv[0-9a-f]{16}", tok["device_id"])
    claims = rig.keys.verify(tok["access_token"], rig.clock())
    assert (claims["sub"], claims["did"]) == (CASEY, tok["device_id"])
    d = rig.db.device(tok["device_id"])
    assert (d["email"], d["name"], d["platform"], d["app_version"], d["client_id"]) == (CASEY, "Casey's phone", "ios", "1.2.0", CLIENT)
    enrolled = [a for a in rig.activity if a[1] == "device_enrolled"]
    assert len(enrolled) == 1 and tok["device_id"] in enrolled[0][2] and enrolled[0][3:5] == ("198.51.100.7", UA)
    assert enrolled[0][7] == "sess-1"                                                           # the Access session
    with rig.mdb._lock:
        rows = rig.mdb.db.execute("SELECT hash FROM mobile_refresh").fetchall()
    assert tok["refresh_token"] not in json.dumps(rows) and len(rows[0][0]) == 64               # sha256 only


def test_a_json_body_works_too_and_junk_does_not(rig):
    s, raw, out, verifier = rig.authorize(CASEY)
    c, _ = rig.code_of(out)
    body = json.dumps({"grant_type": "authorization_code", "code": c, "code_verifier": verifier, "client_id": CLIENT,
                       "redirect_uri": LINK}).encode()
    s, raw, _, _ = rig.call("POST", "/auth/v1/token", headers={"Content-Type": "application/json"}, body=body)
    assert s == 200
    for junk in (b"\xff\xfe", b"[1,2]", b"grant_type", b"a=1&a=2&" + b"x=1&" * 40):
        s, raw, _, _ = rig.call("POST", "/auth/v1/token", body=junk)
        assert s == 400 and json.loads(raw)["error"] == "invalid_request", junk
    assert rig.call("POST", "/auth/v1/token", body=b"x" * 5000)[0] == 400                        # over 4 KB
    s, raw, _, _ = rig.call("POST", "/auth/v1/token", body={"grant_type": "password"})
    assert (s, json.loads(raw)["error"]) == (400, "unsupported_grant_type")


def test_a_code_replay_is_refused_and_revokes_the_device_it_made(rig):
    s, raw, out, verifier = rig.authorize(CASEY)
    c, _ = rig.code_of(out)
    status, tok, _ = rig.exchange(c, verifier)
    assert status == 200 and rig.api(token=tok["access_token"])[0] == 200
    status, err, _ = rig.exchange(c, verifier)
    assert (status, err["error"]) == (400, "invalid_grant")
    assert rig.api(token=tok["access_token"])[1]["error"]["code"] == "device_revoked"          # the first one's tokens are dead
    assert rig.refresh(tok["refresh_token"])[1]["error"] in ("device_revoked", "invalid_grant")
    assert any(a[1] == "revoked" for a in rig.activity) and any(a[1] == "refused" for a in rig.activity)


def test_a_wrong_pkce_verifier_a_wrong_redirect_and_a_wrong_client_are_refused(rig):
    for tweak in ({"verifier": secrets.token_urlsafe(48)}, {"redirect": SCHEME}, {"redirect": "https://evil.example.com/cb"},
                  {"client": "sizeup-android"}, {"client": "nope"}):
        s, raw, out, verifier = rig.authorize(CASEY)
        c, _ = rig.code_of(out)
        status, err, _ = rig.exchange(c, tweak.get("verifier", verifier), tweak.get("redirect", LINK), tweak.get("client", CLIENT))
        assert status in (400, 401) and err["error"] in ("invalid_grant", "invalid_request", "invalid_client"), tweak
        assert "access_token" not in err
        status, err, _ = rig.exchange(c, verifier)                                              # the code is spent, even for the owner
        assert (status, err["error"]) == (400, "invalid_grant"), tweak
    assert rig.db.devices(CASEY) == []                                                          # no device was ever made
    s, raw, out, verifier = rig.authorize(CASEY)                                                # a PKCE verifier too short or odd
    c, _ = rig.code_of(out)
    for v in ("short", "x" * 200, "a b" * 20):
        assert rig.exchange(c, v)[1]["error"] == "invalid_request"


def test_an_expired_code_is_refused(rig):
    s, raw, out, verifier = rig.authorize(CASEY)
    c, _ = rig.code_of(out)
    rig.clock.t += 61
    status, err, _ = rig.exchange(c, verifier)
    assert (status, err["error"]) == (400, "invalid_grant") and rig.db.devices(CASEY) == []
    s, raw, out, verifier = rig.authorize(CASEY)                                                # 59 s is fine
    c, _ = rig.code_of(out)
    rig.clock.t += 59
    assert rig.exchange(c, verifier)[0] == 200


def test_a_made_up_code_is_refused(rig):
    status, err, _ = rig.exchange(secrets.token_urlsafe(32), secrets.token_urlsafe(48))
    assert (status, err["error"]) == (400, "invalid_grant")
    assert rig.exchange("short", secrets.token_urlsafe(48))[1]["error"] == "invalid_request"


def test_the_fourth_device_is_refused_with_a_clear_code_and_a_revoke_frees_a_slot(rig):
    toks = [rig.sign_in(CASEY, device_name=f"phone {i}") for i in range(MAX_DEVICES)]
    s, raw, out, verifier = rig.authorize(CASEY)
    assert s == 403 and b"3 devices" in raw and "Location" not in out                           # refused at the door ...
    rig.db.put_code(c := secrets.token_urlsafe(32), CASEY, "s", base64.urlsafe_b64encode(hashlib.sha256(b"v" * 50).digest()).rstrip(b"=").decode(),
                    CLIENT, LINK, "fourth")                                                      # ... and again at the token
    status, err, _ = rig.exchange(c, "v" * 50)
    assert (status, err["error"]) == (403, "device_limit") and len(rig.db.devices(CASEY)) == 3
    assert rig.sign_in(RIVA)                                                                    # another person has their own 3
    rig.svc.revoke_device(toks[0]["device_id"], RIVA)                                           # an admin's revoke frees one
    assert rig.sign_in(CASEY, device_name="the new one")["device_id"] not in {t["device_id"] for t in toks}
    s, raw, out, verifier = rig.authorize(CASEY)
    assert s == 403


def test_signing_in_again_with_the_same_device_keeps_its_slot(rig):
    toks = [rig.sign_in(CASEY, device_name=f"phone {i}") for i in range(MAX_DEVICES)]
    s, raw, out, verifier = rig.authorize(CASEY, device_id=toks[1]["device_id"])               # at its 180-day limit, say
    assert s == 302
    c, _ = rig.code_of(out)
    status, tok, _ = rig.exchange(c, verifier, device_id=toks[1]["device_id"])
    assert status == 200 and tok["device_id"] == toks[1]["device_id"] and len(rig.db.devices(CASEY)) == 3
    assert rig.refresh(toks[1]["refresh_token"])[1]["error"] == "invalid_grant"                  # the old tokens are gone
    s, raw, out, verifier = rig.authorize(CASEY, device_id=toks[1]["device_id"])
    c, _ = rig.code_of(out)
    assert rig.exchange(c, verifier, device_id=rig.sign_in(RIVA)["device_id"])[1]["error"] == "device_limit"   # not another's


# -- refresh ---------------------------------------------------------------------------------------------------------

def test_a_refresh_rotates_the_token_and_the_new_pair_works(rig):
    t0 = rig.sign_in(CASEY)
    rig.clock.t += 800
    status, t1, h = rig.refresh(t0["refresh_token"], app_version="1.3.0")
    assert status == 200 and t1["refresh_token"] != t0["refresh_token"] and t1["device_id"] == t0["device_id"]
    assert rig.api(token=t1["access_token"])[0] == 200 and rig.db.device(t0["device_id"])["app_version"] == "1.3.0"
    status, t2, _ = rig.refresh(t1["refresh_token"])
    assert status == 200 and len({t0["refresh_token"], t1["refresh_token"], t2["refresh_token"]}) == 3
    rig.clock.t += 200                                                                          # 1000 s after t0 was issued
    assert rig.api(token=t0["access_token"])[1]["error"]["code"] == "token_expired"            # the 15 minutes are real
    assert rig.api(token=t2["access_token"])[0] == 200


def test_a_refresh_token_used_twice_revokes_the_device(rig):
    t0 = rig.sign_in(CASEY)
    t1 = rig.refresh(t0["refresh_token"])[1]
    status, err, _ = rig.refresh(t0["refresh_token"])                                           # the stolen copy, or a replay
    assert (status, err["error"]) == (400, "invalid_grant")
    assert rig.refresh(t1["refresh_token"])[1]["error"] == "invalid_grant"                       # the app's own is dead too
    assert rig.api(token=t1["access_token"])[1]["error"]["code"] == "device_revoked"
    d = rig.db.device(t0["device_id"])
    assert not d["active"] and d["revoked_why"] == "refresh token reused"
    with rig.mdb._lock:
        assert rig.mdb.db.execute("SELECT COUNT(*) FROM mobile_refresh WHERE device_id = ?", (t0["device_id"],)).fetchone()[0] == 0
    kinds = [(a[1], a[5]) for a in rig.activity]
    assert ("refused", False) in kinds and ("revoked", True) in kinds


def test_a_refresh_token_idles_out_after_30_days_and_ends_after_180(rig):
    t0 = rig.sign_in(CASEY)
    rig.clock.t += IDLE_S + 1
    assert rig.refresh(t0["refresh_token"])[1]["error"] == "invalid_grant"                       # idle: sign in again
    t0 = rig.sign_in(CASEY, device_name="other")
    t, last = 0, t0
    while t < ABSOLUTE_S - IDLE_S:                                                              # a person who uses it every 29 days
        rig.clock.t += 29 * 86400
        t += 29 * 86400
        last = rig.refresh(last["refresh_token"])[1]
        assert "refresh_token" in last
    rig.clock.t = rig.db.device(t0["device_id"])["created"] + ABSOLUTE_S + 1
    assert rig.refresh(last["refresh_token"])[1]["error"] == "invalid_grant"                     # 180 days from the sign-in, used or not


def test_a_refresh_for_a_deactivated_person_or_revoked_device_is_a_clear_401(rig):
    a, b = rig.sign_in(CASEY), rig.sign_in(OTIS)
    rig.svc.revoke_device(a["device_id"], RIVA)
    status, err, _ = rig.refresh(a["refresh_token"])                       # its refresh tokens were deleted with it
    assert (status, err["error"]) == (400, "invalid_grant")
    rig.dir.set(OTIS, active=0)
    status, err, _ = rig.refresh(b["refresh_token"])
    assert (status, err["error"]) == (401, "user_inactive") and not rig.db.device(b["device_id"])["active"]
    assert rig.refresh("nonsense")[1]["error"] == "invalid_grant"
    assert rig.refresh(secrets.token_urlsafe(32))[1]["error"] == "invalid_grant"
    assert rig.refresh(a["refresh_token"], client_id="sizeup-android")[1]["error"] == "invalid_grant"


def test_an_app_below_the_minimum_version_is_told_to_upgrade_without_losing_its_token(rig, monkeypatch):
    t0 = rig.sign_in(CASEY)
    monkeypatch.setitem(auth_mod.MIN_APP, "ios", "2.0.0")
    status, err, _ = rig.refresh(t0["refresh_token"], app_version="1.9.9")
    assert (status, err["error"]) == (426, "upgrade_required")
    assert rig.refresh(t0["refresh_token"], app_version="2.0.0")[0] == 200                       # the token was not spent
    s, raw, out, verifier = rig.authorize(CASEY, device_name="x")
    c, _ = rig.code_of(out)
    assert rig.exchange(c, verifier)[0] == 426                                                   # 1.2.0 at sign-in too


def test_the_refresh_is_in_the_activity_log_once_a_day_not_each_time(rig):
    t = rig.sign_in(CASEY)
    for _ in range(5):
        t = rig.refresh(t["refresh_token"])[1]
    assert [a[1] for a in rig.activity].count("refresh") == 1
    rig.clock.t += 86400 + 1
    rig.refresh(t["refresh_token"])
    assert [a[1] for a in rig.activity].count("refresh") == 2


def test_refresh_does_not_write_last_seen_on_every_request_but_touch_is_throttled(rig):
    t = rig.sign_in(CASEY)
    first = rig.db.device(t["device_id"])["last_seen"]
    rig.clock.t += 10
    rig.api(token=rig.refresh(t["refresh_token"])[1]["access_token"])
    rig.clock.t += 400
    rig.api(token=rig.sign_in(OTIS)["access_token"])
    d = rig.db.device(t["device_id"])
    assert d["last_seen"] > first


# -- revoke (RFC 7009) -----------------------------------------------------------------------------------------------

def revoke(rig, token, **kw):
    s, raw, ctype, out = rig.call("POST", "/auth/v1/revoke", body={"token": token, **kw})
    return s, json.loads(raw)


def test_revoke_signs_the_device_out_by_either_token_and_always_says_200(rig):
    a, b = rig.sign_in(CASEY), rig.sign_in(CASEY, device_name="tablet")
    assert revoke(rig, a["refresh_token"]) == (200, {})
    assert rig.api(token=a["access_token"])[1]["error"]["code"] == "device_revoked"
    assert rig.refresh(a["refresh_token"])[1]["error"] in ("device_revoked", "invalid_grant")
    assert revoke(rig, b["access_token"]) == (200, {})
    assert rig.api(token=b["access_token"])[1]["error"]["code"] == "device_revoked"
    for junk in (secrets.token_urlsafe(32), "garbage", "a.b.c", a["refresh_token"], "", forge(
            {"alg": "none", "kid": "k1", "typ": "at+jwt"}, {"sub": OTIS, "did": rig.sign_in(OTIS)["device_id"], "iat": 1, "exp": 2})):
        assert revoke(rig, junk) == (200, {})
    assert rig.api(token=rig.sign_in(OTIS)["access_token"])[0] == 200                              # nobody else was touched
    assert [a[1] for a in rig.activity].count("revoked") == 2
    assert rig.call("POST", "/auth/v1/revoke", body=b"x" * 9000)[0] == 400


def test_revoking_an_expired_access_token_still_signs_the_device_out(rig):
    t = rig.sign_in(CASEY)
    rig.clock.t += 5000
    assert revoke(rig, t["access_token"]) == (200, {}) and not rig.db.device(t["device_id"])["active"]


def test_a_revoke_runs_the_hooks_of_the_push_endpoint_and_a_failing_hook_changes_nothing(rig):
    seen = []
    rig.svc.on_revoke.append(lambda d: seen.append(d["id"]))
    rig.svc.on_revoke.append(lambda d: 1 / 0)
    t = rig.sign_in(CASEY)
    rig.svc.revoke_device(t["device_id"], RIVA)
    assert seen == [t["device_id"]] and not rig.db.device(t["device_id"])["active"]
    assert rig.svc.revoke_device(t["device_id"], RIVA) is None                                      # already gone: nothing again


def test_the_token_endpoints_are_limited_per_address(rig):
    rig.walk = 0.0
    outcomes = [rig.call("POST", "/auth/v1/token", body={"grant_type": "x"}, peer="203.0.113.9")[0] for _ in range(40)]
    assert 429 in outcomes and outcomes[0] == 400
    s, raw, _, out = rig.call("POST", "/auth/v1/token", body={"grant_type": "x"}, peer="203.0.113.9")
    assert s == 429 and int(out["Retry-After"]) >= 1                       # v1's own per-address bucket, first
    own = AuthService(rig.db, rig.keys, rig.svc.people, FakeAccess(), rig.config, rig.switches, _limit=auth_mod.AddrLimit(rig.pace))
    ctx = auth_mod.Ctx({}, {}, {}, None, {}, "POST", "203.0.113.50", lambda n: b"grant_type=x")
    got = [own.token(ctx) for _ in range(12)]                              # the service's: 10 a minute, RFC 6749 shape
    assert [g[0] for g in got].count(429) == 2 and got[-1][1].headers["Retry-After"]
    assert json.loads(got[-1][1].body)["error"] == "rate_limited" and json.loads(got[0][1].body)["error"] == "unsupported_grant_type"
    assert rig.call("POST", "/auth/v1/token", body={"grant_type": "x"}, peer="203.0.113.77")[0] == 400   # another address is not


# -- switches, flags, config -----------------------------------------------------------------------------------------

AUTH_PATHS = [("GET", "/auth/v1/authorize"), ("POST", "/auth/v1/token"), ("POST", "/auth/v1/revoke"),
              ("GET", "/.well-known/apple-app-site-association"), ("GET", "/.well-known/assetlinks.json"),
              ("GET", "/api/v1/me")]


def test_every_auth_route_is_a_404_while_the_switch_or_the_flag_is_off_and_runs_nothing(rig):
    rig.flags["auth"] = False
    for method, path in AUTH_PATHS:
        s, raw, ctype, out = rig.call(method, path, headers={"X-Test-User": CASEY, "Authorization": "Bearer x"})
        assert (s, json.loads(raw)["error"]["code"]) == (404, "not_found"), path
        assert ctype == "application/json"
    off = V1(switches=Switches(lambda: False, lambda: {"auth": True}), auth=rig.svc)
    for method, path in AUTH_PATHS:
        assert off.respond(method, path, "", {"X-Test-User": CASEY})[0] == 404, path
    with rig.mdb._lock:
        assert not rig.mdb.db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'mobile_%'").fetchall()
    rig.flags["auth"] = True                                                                       # and the flag brings it back
    assert rig.authorize(CASEY)[0] == 302


def test_without_the_service_every_auth_path_is_a_404_and_no_token_ever_verifies():
    v1 = V1(switches=Switches(lambda: True, lambda: {"auth": True}), auth=None)
    for method, path in AUTH_PATHS:
        s, raw, _, _ = v1.respond(method, path, "", {"X-Test-User": CASEY, "Authorization": "Bearer " + Keys({"k1": SECRET}).sign(CASEY, "d", 1.0)})
        assert s == (401 if path == "/api/v1/me" else 404), path           # /api/v1/me has no resolver but none_yet: a 401
    for path in ("/auth/v1/nope", "/auth/v1/", "/auth/v1"):
        assert v1.handles(path) == path.startswith("/auth/v1/") and (v1.respond("GET", path, "", {})[0] == 404 or path == "/auth/v1")
    assert not v1.handles("/.well-known/security.txt") and not v1.handles("/.well-known/")
    token = Keys({"k1": SECRET}).sign(CASEY, "dv0123456789abcdef", __import__("time").time())
    assert v1.respond("GET", "/api/v1/me", "", {"Authorization": f"Bearer {token}"})[0] == 401   # nobody resolves: none_yet


def test_the_route_table_declares_who_what_flag_and_role_for_every_route(rig):
    served = rig.svc.routes()
    for r in ROUTES + served:
        if (r.method, r.path) == ("GET", "/api/v1/meta"):
            continue
        assert r.flag == "auth" and (r.need == "public") == (not r.path.startswith("/api/v1/")), (r.method, r.path)
        assert r.role in ("read", "dispatch", "admin") if r.need == "device" else r.role == "", (r.method, r.path)
    assert {(r.method, r.path) for r in served} == {(m, p) for m, p in AUTH_PATHS if p != "/api/v1/me"}
    me = next(r for r in ROUTES if r.path == "/api/v1/me")
    assert (me.need, me.flag, me.role) == ("device", "auth", "read")


def test_the_association_files_come_from_config_and_are_404_until_set(rig):
    s, raw, ctype, out = rig.call("GET", "/.well-known/apple-app-site-association")
    aasa = json.loads(raw)
    assert s == 200 and ctype == "application/json" and aasa["applinks"]["details"] == [
        {"appIDs": ["ABCDE12345.org.example.sizeup"], "components": [{"/": "/app/callback"}]}]
    assert aasa["webcredentials"] == {"apps": ["ABCDE12345.org.example.sizeup"]}
    assert "max-age" in out["Cache-Control"]
    s, raw, ctype, out = rig.call("GET", "/.well-known/assetlinks.json")
    al = json.loads(raw)
    assert s == 200 and al[0]["target"] == {"namespace": "android_app", "package_name": "org.example.sizeup",
                                            "sha256_cert_fingerprints": ["AA:" * 31 + "AA"]}
    empty = Rig(clients=(Client(CLIENT, "ios", (LINK,)),))
    empty.svc.config = AuthConfig((Client(CLIENT, "ios", (LINK,)),))
    for path in ("/.well-known/apple-app-site-association", "/.well-known/assetlinks.json"):
        s, raw, _, _ = empty.call("GET", path)
        assert (s, json.loads(raw)["error"]["code"]) == (404, "not_found"), path


# -- a real server: the console's side stays shut ---------------------------------------------------------------------

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


def http(url, path, method="GET", headers=None, data=None):
    req = urllib.request.Request(url + path, method=method, headers=headers or {}, data=data)
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=10) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


@pytest.fixture
def served(rig):
    srv = WebServer("127.0.0.1", 0, {}, lambda: {}, lambda c: None, lambda i: False, access=FakeAccess(), role="hub",
                    mobile=rig.switches, mobile_auth=rig.svc)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield rig, f"http://127.0.0.1:{srv.httpd.server_address[1]}"
    srv.shutdown()


def test_the_flow_over_http_with_the_servers_real_hooks(served):
    rig, url = served
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    q = urllib.parse.urlencode({"response_type": "code", "client_id": CLIENT, "redirect_uri": LINK, "code_challenge": challenge,
                                "code_challenge_method": "S256", "state": "s1", "device_name": "Casey's phone"})
    assert http(url, "/auth/v1/authorize?" + q)[0] == 401                                      # no Access person: a page
    status, headers, _ = http(url, "/auth/v1/authorize?" + q, headers={"X-Test-User": CASEY})
    assert status == 302
    code = urllib.parse.parse_qs(urllib.parse.urlsplit(headers["Location"]).query)["code"][0]
    form = urllib.parse.urlencode({"grant_type": "authorization_code", "code": code, "code_verifier": verifier,
                                   "client_id": CLIENT, "redirect_uri": LINK, "app_version": "1.2.0"}).encode()
    status, headers, body = http(url, "/auth/v1/token", "POST", {"Content-Type": "application/x-www-form-urlencoded"}, form)
    tok = json.loads(body)
    assert status == 200 and headers["Cache-Control"] == "no-store"
    status, _, body = http(url, "/api/v1/me", headers={"Authorization": f"Bearer {tok['access_token']}"})
    assert status == 200 and json.loads(body)["email"] == CASEY
    status, _, body = http(url, "/api/v1/me", headers={"X-Test-User": CASEY, "Cookie": "CF_Authorization=x"})     # Access is not the door
    assert (status, json.loads(body)["error"]["code"]) == (401, "unauthorized")
    status, _, body = http(url, "/.well-known/apple-app-site-association")                    # no Access, no token
    assert status == 200 and "applinks" in json.loads(body)
    assert http(url, "/auth/v1/token", "POST", data=b"")[0] == 400


def test_every_console_route_refuses_a_device_bearer(served):
    rig, url = served
    tok = rig.sign_in(CASEY)["access_token"]
    from tests.test_console_pin import route_table
    paths = sorted({r.split(": ", 1)[1] for r in route_table() if r.split(": ", 1)[1].startswith("/api/cad")})
    assert len(paths) > 50, "the scan found nothing: the pattern is stale"
    for path in paths:
        for method in ("GET", "POST"):
            for headers in ({"Authorization": f"Bearer {tok}"}, {"Authorization": f"Bearer {tok}", "X-Test-User": RIVA}):
                status, _, body = http(url, path, method, {**headers, "Content-Type": "application/json"},
                                       b"{}" if method == "POST" else None)
                assert status == 401 and json.loads(body) == {"error": "a device token is not for the console"}, (method, path)


def test_no_v1_path_reaches_the_console_api_without_a_person(served):
    rig, url = served
    for path in ("/api/v1/me", "/api/v1/cad/state", "/api/v1/../cad/state", "/auth/v1/../cad/whoami"):
        status, _, body = http(url, path)
        assert status in (401, 404) and b"state" not in body and b"whoami" not in body, path


# -- startup writes nothing, the wiring ---------------------------------------------------------------------------------

def tables(path):
    db = sqlite3.connect(path)
    try:
        return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'mobile_%'")}
    finally:
        db.close()


@pytest.fixture
def hub(tmp_path):
    from sizeup.app import make_app
    from tests.test_cloud import hub_cfg
    cfg = hub_cfg(tmp_path, mobile_enabled=True, mobile_clients=((CLIENT, "ios", (LINK,)),))
    app = make_app(cfg).start()
    yield cfg, app, tmp_path
    app.stop()


def test_building_the_service_writes_nothing_and_the_tables_appear_on_the_first_sign_in(hub):
    cfg, app, tmp = hub
    svc = make_auth(cfg, app, FakeAccess(), Switches(lambda: True, lambda: {"auth": True}), lambda m: None,
                    {ENV: f"k1:{SECRET}"})
    assert svc is not None and app.cad.devices is svc
    path = tmp / "hub" / "hub.sqlite"
    assert tables(path) == set()                                                                # nothing at start
    assert svc.db.devices() == [] and svc.db.device("dv0123456789abcdef") is None and tables(path) == set()   # reads neither
    assert svc.resolve({"Authorization": "Bearer x"}) is None and tables(path) == set()
    svc.db.put_code(secrets.token_urlsafe(32), CASEY, "s", "c" * 43, CLIENT, LINK, "n")
    assert tables(path) == {"mobile_devices", "mobile_refresh", "mobile_codes"}


def test_the_key_missing_or_the_switch_off_means_no_service_and_nothing_loaded(hub, tmp_path):
    cfg, app, _ = hub
    logs = []
    sw = Switches(lambda: True, lambda: {"auth": True})
    assert make_auth(cfg, app, FakeAccess(), sw, logs.append, {}) is None
    assert any(ENV in line and "not set" in line for line in logs)
    assert make_auth(cfg, app, FakeAccess(), sw, logs.append, {ENV: "k1:tooshort"}) is None
    assert make_auth(cfg, app, None, sw, logs.append, {ENV: f"k1:{SECRET}"}) is None             # no Access: no authorize
    from dataclasses import replace
    assert make_auth(replace(cfg, mobile_enabled=False), app, FakeAccess(), sw, logs.append, {ENV: f"k1:{SECRET}"}) is None
    assert make_auth(replace(cfg, cloud_role=""), app, FakeAccess(), sw, logs.append, {ENV: f"k1:{SECRET}"}) is None
    assert SECRET not in " ".join(logs)
    assert getattr(app.cad, "devices", None) is None


def test_settings_users_devices_over_a_real_hub(hub):
    """The console's half with real operators: list, revoke (admin: anyone's; others: their own), the deactivation hook,
    the activity rows and the role the sign-in reads from the real Settings."""
    from sizeup.cad.api import CadApi
    cfg, app, _ = hub
    cad = app.cad
    sw = Switches(lambda: True, lambda: {"auth": True})
    svc = make_auth(cfg, app, FakeAccess(), sw, lambda m: None, {ENV: f"k1:{SECRET}"})
    for name, email, role in (("Riva Stone", RIVA, "admin"), ("Casey Rivera", CASEY, "dispatcher"), ("Otis Lane", OTIS, "viewer")):
        cad.settings.post("users", {"action": "add", "name": name, "email": email, "role": role})
    v1 = V1(switches=sw, auth=svc)

    def sign_in(email, name):
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        q = urllib.parse.urlencode({"response_type": "code", "client_id": CLIENT, "redirect_uri": LINK, "code_challenge": challenge,
                                    "code_challenge_method": "S256", "state": "s", "device_name": name})
        s, raw, _, out = v1.respond("GET", "/auth/v1/authorize", q, {"X-Test-User": email, "User-Agent": UA}, "198.51.100.7")
        assert s == 302, raw
        c = urllib.parse.parse_qs(urllib.parse.urlsplit(out["Location"]).query)["code"][0]
        body = urllib.parse.urlencode({"grant_type": "authorization_code", "code": c, "code_verifier": verifier, "client_id": CLIENT,
                                       "redirect_uri": LINK, "app_version": "1.2.0"}).encode()
        s, raw, _, _ = v1.respond("POST", "/auth/v1/token", "", {"User-Agent": UA}, "198.51.100.7", lambda n: body)
        assert s == 200, raw
        return json.loads(raw)

    casey, otis = sign_in(CASEY, "Casey's phone"), sign_in(OTIS, "Otis's phone")
    s, raw, _, _ = v1.respond("GET", "/api/v1/me", "", {"Authorization": f"Bearer {casey['access_token']}"})
    assert json.loads(raw)["role"] == "dispatcher"                                              # from the real Settings
    api = CadApi(cad)
    j = lambda r: json.loads(r[1])
    admin = j(api.get("/api/cad/devices", "", Identity(email=RIVA)))
    assert admin["all"] and {d["email"] for d in admin["devices"]} == {CASEY, OTIS} and admin["max"] == 3
    assert not any(k in json.dumps(admin) for k in ("refresh", "hash", "token"))
    mine = j(api.get("/api/cad/devices", "", Identity(email=CASEY)))
    assert not mine["all"] and [d["email"] for d in mine["devices"]] == [CASEY]
    assert (mine["devices"][0]["name"], mine["devices"][0]["platform"], mine["devices"][0]["app_version"]) == ("Casey's phone", "ios", "1.2.0")
    # self-service: Casey revokes their own, not Otis's; an admin revokes anyone's
    r = api.post("/api/cad/devices", json.dumps({"action": "revoke", "id": otis["device_id"]}).encode(), Identity(email=CASEY))
    assert r[0] == 403
    assert v1.respond("GET", "/api/v1/me", "", {"Authorization": f"Bearer {otis['access_token']}"})[0] == 200
    r = api.post("/api/cad/devices", json.dumps({"action": "revoke", "id": casey["device_id"]}).encode(), Identity(email=CASEY))
    assert r[0] == 200
    assert json.loads(v1.respond("GET", "/api/v1/me", "", {"Authorization": f"Bearer {casey['access_token']}"})[1])["error"]["code"] == "device_revoked"
    r = api.post("/api/cad/devices", json.dumps({"action": "revoke", "id": otis["device_id"]}).encode(), Identity(email=RIVA))
    assert r[0] == 200
    assert api.post("/api/cad/devices", b'{"action":"revoke","id":"dv0000000000000000"}', Identity(email=RIVA))[0] == 400
    assert api.post("/api/cad/devices", b'{"action":"nope"}', Identity(email=RIVA))[0] == 400
    assert api.get("/api/cad/devices", "", None)[0] == 404                                      # no person: not for them
    kinds = [(r["kind"], r["email"]) for r in cad.activity.rows(limit=50)]
    assert ("device_enrolled", CASEY) in kinds and ("revoked", CASEY) in kinds and ("revoked", OTIS) in kinds
    rows = [r for r in cad.activity.rows(kind="device_enrolled")]
    assert rows and rows[0]["ip"] == "198.51.100.7" and rows[0]["ua"].startswith("sizeup/1.2.0")
    assert not [r for r in cad.activity.rows(email=CASEY, kind="action") if "devices" in r["route"]]    # one row, not two
    # a deactivated person's devices are revoked at once, by Settings itself
    third = sign_in(CASEY, "Casey's tablet")
    op = next(o for o in cad.settings.operators() if o["email"] == CASEY)
    cad.settings.post("users", {"action": "deactivate", "id": op["id"]})
    assert not svc.db.device(third["device_id"])["active"]
    assert json.loads(v1.respond("GET", "/api/v1/me", "", {"Authorization": f"Bearer {third['access_token']}"})[1])["error"]["code"] \
        == "device_revoked"
    s, raw, _, _ = v1.respond("GET", "/auth/v1/authorize", urllib.parse.urlencode({
        "response_type": "code", "client_id": CLIENT, "redirect_uri": LINK, "code_challenge": "c" * 43,
        "code_challenge_method": "S256", "state": "s"}), {"X-Test-User": CASEY}, "")
    assert s == 403                                                                             # and cannot enroll again
    # the sign-in never adds an operator: an unknown email on the list is not created by a token or an authorize
    s, raw, _, _ = v1.respond("GET", "/auth/v1/authorize", urllib.parse.urlencode({
        "response_type": "code", "client_id": CLIENT, "redirect_uri": LINK, "code_challenge": "c" * 43,
        "code_challenge_method": "S256", "state": "s"}), {"X-Test-User": EVE}, "")
    assert s == 403 and not [o for o in cad.settings.operators() if o["email"] == EVE]


def test_the_devices_api_is_the_hubs_only(tmp_path):
    from sizeup.app import make_app
    from sizeup.cad.api import CadApi
    from tests.test_cloud import base
    app = make_app(base(cad_db=str(tmp_path / "cad.sqlite")))                                   # all-in-one: no sign-in
    r = CadApi(app.cad).get("/api/cad/devices", "", Identity(email=RIVA))
    assert r[0] == 404 and json.loads(r[1])["error"].startswith("the app's devices")
    assert CadApi(app.cad).post("/api/cad/devices", b"{}", Identity(email=RIVA))[0] in (403, 404)


# -- config ----------------------------------------------------------------------------------------------------------

SIGNIN = f'''[cloud]
role = "hub"
[mobile]
enabled = true
apple_team_id = "ABCDE12345"
apple_bundle_ids = ["org.example.sizeup"]
android_package = "org.example.sizeup"
android_sha256 = ["{"AA:" * 31}AA"]
clients = [
  {{ id = "sizeup-ios", platform = "ios", redirect_uris = ["{LINK}", "{SCHEME}"] }},
  {{ id = "sizeup-android", platform = "android", redirect_uris = ["https://hub.example.org/app/android"] }},
]
'''


def conf(tmp_path, text):
    from sizeup.config import load
    p = tmp_path / "c.toml"
    p.write_text(text)
    return load(p)


def test_the_signin_config_is_read_exactly(tmp_path):
    c = conf(tmp_path, SIGNIN)
    assert c.mobile_clients == (("sizeup-ios", "ios", (LINK, SCHEME)),
                                ("sizeup-android", "android", ("https://hub.example.org/app/android",)))
    assert (c.mobile_apple_team_id, c.mobile_apple_bundle_ids, c.mobile_android_package) == (
        "ABCDE12345", ("org.example.sizeup",), "org.example.sizeup")
    assert c.mobile_android_sha256 == ("AA:" * 31 + "AA",)
    empty = conf(tmp_path, "[mobile]\nenabled = true\n")
    assert (empty.mobile_clients, empty.mobile_apple_team_id, empty.mobile_android_sha256) == ((), "", ())


@pytest.mark.parametrize("old, new", [
    ('platform = "ios"', 'platform = "windows"'),
    ('id = "sizeup-ios"', 'id = "Has Space"'),
    ('id = "sizeup-android"', 'id = "sizeup-ios"'),                                # a duplicate client id
    (f'"{LINK}"', '"http://hub.example.org/app/callback"'),                       # http is never a return address
    (f'"{LINK}"', '"https://hub.example.org/app/*"'),                             # no wildcard
    (f'"{LINK}"', '"https://hub.example.org/app/callback#frag"'),
    (f'"{LINK}"', '"https://hub.example.org"'),                                   # no path: not an exact link
    (f'"{SCHEME}"', '"myapp:/cb"'),                                               # not a reverse-domain scheme
    (f'"{SCHEME}"', '"javascript:alert(1)"'),
    ('redirect_uris = ["https://hub.example.org/app/android"]', "redirect_uris = []"),
    ('apple_team_id = "ABCDE12345"', 'apple_team_id = "abc"'),
    ('apple_bundle_ids = ["org.example.sizeup"]', 'apple_bundle_ids = "org.example.sizeup"'),
    ('android_package = "org.example.sizeup"', 'android_package = 5'),
    ('android_sha256 = ["' + "AA:" * 31 + 'AA"]', 'android_sha256 = ["aa:bb"]'),
    ('android"] }', 'android"], secret = "x" }'),
])
def test_the_signin_config_refuses_anything_loose(tmp_path, old, new):
    from sizeup.config import ConfigError
    assert old in SIGNIN, old
    with pytest.raises(ConfigError, match="mobile"):
        conf(tmp_path, SIGNIN.replace(old, new, 1))


def test_the_shipped_configs_document_the_signin_keys_without_turning_it_on():
    from pathlib import Path
    from sizeup.config import load
    root = Path(__file__).resolve().parent.parent
    for f in ("config.example.toml", "deploy/hub/config.hub.toml"):
        text = (root / f).read_text()
        assert "# clients = [" in text and "SIZEUP_MOBILE_TOKEN_KEYS" in text, f
        assert load(root / f).mobile_clients == ()


def test_the_signin_spec_is_valid_and_is_the_service_routes_both_ways(rig):
    from pathlib import Path
    import yaml
    from openapi_spec_validator import validate
    doc = yaml.safe_load((Path(__file__).resolve().parent.parent / "api" / "openapi" / "v1" / "auth.openapi.yaml").read_text())
    validate(doc)
    promised = {(m.upper(), p) for p, item in doc["paths"].items() for m in item}
    assert promised == {(r.method, r.path) for r in rig.svc.routes()}
    assert all(doc["paths"][p][m.lower()].get("security") == [] for m, p in promised)       # none needs a bearer or Access here
