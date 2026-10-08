"""Cloudflare Access identity on the hub (sizeup/cloud/access.py): the JWT verified against the team's
keys (a 2048-bit RSA key made here, served as a JWKS by a local server), the hub's gate, and the
signed-in person as the operator on every write."""

import base64
import hashlib
import hmac
import json
import random
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from sizeup.app import access_verifier
from sizeup.cad.api import CadApi
from sizeup.cloud import access as access_mod
from sizeup.cloud.access import SHA256_PREFIX, AccessError, AccessVerifier, rsa_pkcs1_sha256_verify
from sizeup.config import ConfigError, load
from tests.test_cad import make_cad
from tests.test_cloud import TOKEN, HubRun, base, hub_cfg

TEAM = "yoda.test-access.example"
ISS = f"https://{TEAM}"
AUD = "a" * 64


# -- a real RSA key, made here (Miller-Rabin), and PKCS#1 v1.5 SHA-256 signing ------------------------------

def _probable_prime(bits: int, rnd: random.Random) -> int:
    small = [p for p in range(3, 2000, 2) if all(p % q for q in range(3, int(p ** 0.5) + 1, 2))]
    while True:
        n = rnd.getrandbits(bits) | (1 << (bits - 1)) | (1 << (bits - 2)) | 1
        if any(n % p == 0 for p in small):
            continue
        d, r = n - 1, 0
        while d % 2 == 0:
            d, r = d // 2, r + 1
        for _ in range(40):
            x = pow(rnd.randrange(2, n - 2), d, n)
            if x in (1, n - 1):
                continue
            for _ in range(r - 1):
                x = pow(x, 2, n)
                if x == n - 1:
                    break
            else:
                break
        else:
            return n


class Key:
    def __init__(self, kid: str, seed: int, bits: int = 2048):
        rnd, e = random.Random(seed), 65537
        while True:
            p, q = _probable_prime(bits // 2, rnd), _probable_prime(bits // 2, rnd)
            phi = (p - 1) * (q - 1)
            if p != q and (p * q).bit_length() == bits and phi % e:
                break
        self.kid, self.n, self.e, self.d = kid, p * q, e, pow(e, -1, phi)

    def jwk(self) -> dict:
        b = lambda i: _b64(i.to_bytes((i.bit_length() + 7) // 8, "big"))
        return {"kid": self.kid, "kty": "RSA", "alg": "RS256", "use": "sig", "e": b(self.e), "n": b(self.n)}

    def sign(self, msg: bytes) -> bytes:
        k = (self.n.bit_length() + 7) // 8
        t = SHA256_PREFIX + hashlib.sha256(msg).digest()
        em = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
        return pow(int.from_bytes(em, "big"), self.d, self.n).to_bytes(k, "big")


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def jwt(key: Key | None, claims: dict | None = None, alg="RS256", kid=None, **over) -> str:
    now = time.time()
    c = {"aud": [AUD], "iss": ISS, "email": "Corey@Example.org", "sub": "u-1", "iat": int(now),
         "exp": int(now) + 3600, "nbf": int(now) - 5, "type": "app", **(claims or {})}
    c.update(over)
    c = {k: v for k, v in c.items() if v is not None}
    h = _b64(json.dumps({"alg": alg, "kid": kid or (key.kid if key else "k1"), "typ": "JWT"}).encode())
    p = _b64(json.dumps(c).encode())
    if alg == "RS256":
        sig = key.sign(f"{h}.{p}".encode())
    elif alg == "HS256":       # the old trick: HMAC with the public key as the secret
        sig = hmac.new(key.n.to_bytes(256, "big"), f"{h}.{p}".encode(), hashlib.sha256).digest()
    else:
        sig = b""
    return f"{h}.{p}.{_b64(sig)}"


@pytest.fixture(scope="module")
def keys():
    return Key("k1", 1), Key("k2", 2)


class Jwks:
    """The team's certs endpoint, on this machine: serves .keys, counts fetches, can fail."""

    def __init__(self, keys):
        self.keys, self.hits, self.fail = list(keys), 0, False
        me = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                me.hits += 1
                body = b"oops" if me.fail else json.dumps({"keys": [k.jwk() for k in me.keys]}).encode()
                self.send_response(500 if me.fail else 200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/cdn-cgi/access/certs"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def jwks(keys):
    j = Jwks(keys[:1])
    yield j
    j.close()


class Clock:
    def __init__(self):
        self.t = time.time()

    def __call__(self):
        return self.t


def verifier(jwks, clock=None) -> AccessVerifier:
    v = AccessVerifier(TEAM, AUD, clock=clock or time.time, log=lambda m: None)
    v.certs_url = jwks.url                    # a test server instead of https://<team>/cdn-cgi/access/certs
    return v


# -- the token ------------------------------------------------------------------------------------------------

def test_rsa_signature_check_matches_the_signer(keys):
    k = keys[0]
    sig = k.sign(b"hello")
    assert rsa_pkcs1_sha256_verify(k.n, k.e, b"hello", sig)
    assert not rsa_pkcs1_sha256_verify(k.n, k.e, b"hellO", sig)
    assert not rsa_pkcs1_sha256_verify(k.n, k.e, b"hello", sig[:-1] + bytes([sig[-1] ^ 1]))
    assert not rsa_pkcs1_sha256_verify(k.n, k.e, b"hello", sig[1:])               # wrong length
    assert not rsa_pkcs1_sha256_verify(keys[1].n, keys[1].e, b"hello", sig)       # another key


def test_a_good_token_is_the_person_lower_cased(keys, jwks):
    v = verifier(jwks)
    who = v.identity(jwt(keys[0]))
    assert who.person and who.email == "corey@example.org" and who.sub == "u-1"
    assert v.identity(jwt(keys[0], aud=AUD)).email == "corey@example.org"         # aud as a string
    assert jwks.hits == 1
    v.identity(jwt(keys[0]))
    assert jwks.hits == 1                                                         # the keys are cached


@pytest.mark.parametrize("why, make", [
    ("wrong audience", lambda k: jwt(k, aud=["b" * 64])),
    ("wrong issuer", lambda k: jwt(k, iss="https://evil.cloudflareaccess.com")),
    ("expired", lambda k: jwt(k, exp=int(time.time()) - 120)),
    ("no exp", lambda k: jwt(k, exp=None)),
    ("not valid yet", lambda k: jwt(k, nbf=int(time.time()) + 600)),
    ("bad signature", lambda k: (lambda t: t[:-6] + ("AAAAAA" if not t.endswith("AAAAAA") else "BBBBBB"))(jwt(k))),
    ("claims changed", lambda k: (lambda h, p, s: ".".join((h, _b64(json.dumps(
        dict(json.loads(base64.urlsafe_b64decode(p + "==")), email="boss@example.org")).encode()), s)))(*jwt(k).split("."))),
    ("alg none", lambda k: jwt(k, alg="none")),
    ("alg HS256", lambda k: jwt(k, alg="HS256")),
    ("unknown key", lambda k: jwt(k, kid="k9")),
    ("garbage", lambda k: "not.a.jwt"),
])
def test_bad_tokens_are_refused(keys, jwks, why, make):
    v = verifier(jwks)
    with pytest.raises(AccessError):
        v.verify(make(keys[0]))


def test_an_unknown_key_id_fetches_the_keys_again_but_not_on_every_request(keys, jwks):
    clock = Clock()
    v = verifier(jwks, clock)
    assert v.identity(jwt(keys[0])).email and jwks.hits == 1
    jwks.keys = [keys[0], keys[1]]                               # Cloudflare rotates: a new key appears
    clock.t += 5
    with pytest.raises(AccessError, match="unknown key"):       # fetched again only after 30 s
        v.verify(jwt(keys[1]))
    assert jwks.hits == 1
    clock.t += 30
    assert v.identity(jwt(keys[1])).email == "corey@example.org" and jwks.hits == 2


def test_the_cached_keys_are_kept_when_a_fetch_fails(keys, jwks):
    clock = Clock()
    v = verifier(jwks, clock)
    v.identity(jwt(keys[0]))
    jwks.fail = True
    clock.t += 2 * 3600                                          # stale: fetched again, which fails
    tok = jwt(keys[0], exp=int(clock.t) + 600, iat=int(clock.t), nbf=int(clock.t))
    assert v.identity(tok).email == "corey@example.org" and jwks.hits == 2


def test_a_service_token_is_a_machine_never_a_person(keys, jwks):
    who = verifier(jwks).identity(jwt(keys[0], email=None, common_name="abc123.access", sub=""))
    assert not who.person and who.service == "abc123.access"


def test_header_first_then_the_cookie_and_never_the_email_header(keys, jwks):
    v = verifier(jwks)
    good = jwt(keys[0])
    assert v.identify({"Cf-Access-Jwt-Assertion": good}).email == "corey@example.org"
    assert v.identify({"Cookie": f"other=1; CF_Authorization={good}"}).email == "corey@example.org"
    assert v.identify({"Cf-Access-Authenticated-User-Email": "boss@example.org"}) is None
    assert v.identify({"Cf-Access-Jwt-Assertion": jwt(keys[0], alg="none")}) is None
    assert v.identify({}) is None


# -- the hub ------------------------------------------------------------------------------------------------

def req(url, method="GET", body=None, headers=None):
    r = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                               headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            raw, ctype = resp.read(), resp.headers.get("Content-Type", "")
            return resp.status, json.loads(raw) if "json" in ctype else raw
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, json.loads(raw) if "json" in (e.headers.get("Content-Type") or "") else raw


@pytest.fixture
def hub(tmp_path, jwks):
    h = HubRun(hub_cfg(tmp_path, cloud_access_team=TEAM, cloud_access_aud=AUD))
    assert h.server.access is not None
    h.server.access.certs_url = jwks.url
    h.server.access.log = lambda m: None
    yield h
    h.close()


def test_hub_with_access_refuses_pages_and_api_without_a_token(keys, hub):
    for path in ("/cad", "/", "/monitor", "/comms", "/static/cad.js", "/api/cad/state", "/api/state", "/api/cad/whoami"):
        code, body = req(hub.url + path)
        assert code == 401, path
        assert (b"Cloudflare Access" in body) if isinstance(body, bytes) else "Cloudflare Access" in body["error"]
    code, _ = req(hub.url + "/api/cad/incident", "POST", {"type": "STRU", "operator": "Mallory"})
    assert code == 401 and hub.app.cad.store.q("SELECT * FROM incidents") == []
    for bad in ({"Cf-Access-Authenticated-User-Email": "corey@example.org"},          # a hint, not proof
                {"Cf-Access-Jwt-Assertion": jwt(keys[0], email=None, common_name="x.access")},   # a machine
                {"Cf-Access-Jwt-Assertion": jwt(keys[0], aud=["b" * 64])}):              # another Access app
        assert req(hub.url + "/cad", headers=bad)[0] == 401
    # not for people: the health check and the station (its own bearer token)
    assert req(hub.url + "/healthz")[0] == 200
    assert req(hub.url + "/api/station/jobs?wait=0", headers={"Authorization": f"Bearer {TOKEN}"})[0] == 200
    assert req(hub.url + "/api/station/jobs?wait=0")[0] == 401
    # signed in: the page
    code, page = req(hub.url + "/cad", headers={"Cookie": f"CF_Authorization={jwt(keys[0])}"})
    assert code == 200 and b"operator" in page


def test_the_signed_in_person_is_the_operator_whatever_the_page_sends(keys, hub):
    h = {"Cf-Access-Jwt-Assertion": jwt(keys[0])}
    code, me = req(hub.url + "/api/cad/whoami", headers=h)
    assert code == 200 and me == {"email": "corey@example.org", "name": "corey@example.org", "callsign": "", "source": "access",
                                  "role": "dispatcher", "active": True, "may": "admin", "bootstrap": True, "admins": [],
                                  "enforced": True}       # no admin yet: anyone signed in (tests/test_roles.py)
    cad = hub.app.cad
    ops = cad.settings.operators()
    assert [(o["name"], o["email"]) for o in ops] == [("corey@example.org", "corey@example.org")]   # added, not named yet
    # not named yet: recorded as the email
    code, inc = req(hub.url + "/api/cad/incident", "POST", {"type": "STRU", "address": "209 Westchester Ave",
                                                             "operator": "Mallory"}, h)
    assert code == 200
    # an admin names them (the edit itself is recorded as them too)
    code, _ = req(hub.url + "/api/cad/settings/users", "POST", {"action": "edit", "id": ops[0]["id"], "name": "Corey",
                                                                "callsign": "2398", "operator": "Mallory"}, h)
    assert code == 200
    assert {k: v for k, v in req(hub.url + "/api/cad/whoami", headers=h)[1].items() if k in ("email", "name", "callsign", "source")} == {
        "email": "corey@example.org", "name": "Corey", "callsign": "2398", "source": "access"}
    code, _ = req(hub.url + f"/api/cad/incident/{inc['id']}/note", "POST", {"text": "Smoke showing", "operator": "Mallory"}, h)
    assert code == 200
    code, _ = req(hub.url + "/api/cad/incident/{}/note".format(inc["id"]), "POST", {"text": "No operator sent"}, h)
    assert code == 200
    ev = {e["text"]: e["operator"] for e in cad.store.q("SELECT text, operator FROM events WHERE incident_id = ?", (inc["id"],))}
    assert ev["Smoke showing"] == ev["No operator sent"] == "Corey <corey@example.org>"
    assert any(op == "corey@example.org" for op in ev.values())                       # created before the name
    assert "Mallory" not in json.dumps(cad.store.q("SELECT * FROM events")) + json.dumps(cad.settings.audit_log())
    audit = cad.settings.audit_log("users")
    assert audit[0]["operator"] == "corey@example.org" and json.loads(audit[0]["new"])["name"] == "Corey"
    assert audit[-1]["key"] == "corey@example.org" and "Cloudflare Access" in audit[-1]["new"]   # first seen
    # a second person is told apart from the first
    h2 = {"Cf-Access-Jwt-Assertion": jwt(keys[0], email="pat@example.org")}
    assert req(hub.url + "/api/cad/whoami", headers=h2)[1]["name"] == "pat@example.org"
    assert len(cad.settings.operators()) == 2


def test_users_keep_one_email_each_and_round_trip_it(tmp_path):
    from tests.test_settings import post
    cad = make_cad(tmp_path / "a")
    a = post(cad, "users", action="add", name="Corey", email="Corey@Example.org", callsign="2398")
    assert (a["email"], a["callsign"]) == ("corey@example.org", "2398")
    b = post(cad, "users", action="add", name="Pat")
    with pytest.raises(Exception, match="already another operator's email"):
        post(cad, "users", action="edit", id=b["id"], email="corey@example.org")
    with pytest.raises(Exception, match="email"):
        post(cad, "users", action="edit", id=b["id"], email="not-an-email")
    assert cad.settings.person("COREY@example.org")["operator"] == "Corey <corey@example.org>"
    data = json.loads(json.dumps(cad.settings_export()))
    other = make_cad(tmp_path / "b")
    post(other, "import", action="apply", settings=data)
    assert [(o["name"], o["email"], o["callsign"]) for o in other.settings.operators()] == [
        ("Corey", "corey@example.org", "2398"), ("Pat", "", "")]


def test_all_in_one_is_unchanged(tmp_path):
    cfg = base(cad_db=str(tmp_path / "cad.sqlite"), cloud_access_team=TEAM, cloud_access_aud=AUD)
    assert access_verifier(cfg) is None                                  # only the hub checks Access
    assert access_verifier(hub_cfg(tmp_path)) is None                    # a hub without them: as before
    api = CadApi(make_cad(tmp_path / "x"))
    code, body, _ = api.get("/api/cad/whoami", "")
    assert code == 200 and json.loads(body) == {"email": "", "name": "", "callsign": "", "source": "none", "role": "",
                                                "enforced": False}
    code, body, _ = api.post("/api/cad/incident", json.dumps({"type": "STRU", "operator": "Mallory"}).encode())
    assert code == 200
    assert api.cad.store.q("SELECT operator FROM events")[0]["operator"] == "Mallory"


def test_access_config(tmp_path, monkeypatch):
    monkeypatch.delenv("SIZEUP_ACCESS_TEAM", raising=False)
    monkeypatch.delenv("SIZEUP_ACCESS_AUD", raising=False)
    p = tmp_path / "c.toml"
    p.write_text('[cloud]\nrole = "hub"\naccess_team = "https://Yoda.cloudflareaccess.com/"\naccess_aud = ""\n')
    cfg = load(p)
    assert (cfg.cloud_access_team, cfg.cloud_access_aud) == ("yoda.cloudflareaccess.com", "")
    assert access_verifier(cfg) is None                                  # no AUD tag yet: off, said in the log
    monkeypatch.setenv("SIZEUP_ACCESS_AUD", "AB" * 32)
    cfg = load(p)
    assert cfg.cloud_access_aud == "ab" * 32 and access_verifier(cfg) is not None
    monkeypatch.setenv("SIZEUP_ACCESS_AUD", "not a tag")
    with pytest.raises(ConfigError, match="access_aud"):
        load(p)
    monkeypatch.delenv("SIZEUP_ACCESS_AUD")
    p.write_text('[cloud]\nrole = "hub"\naccess_team = "yoda cloudflare"\n')
    with pytest.raises(ConfigError, match="access_team"):
        load(p)
    hub_toml = load(access_mod.__file__.replace("sizeup/cloud/access.py", "deploy/hub/config.hub.toml"))
    assert hub_toml.cloud_access_team == "yoda.cloudflareaccess.com"
