"""Who is at the console, from Cloudflare Access (docs/cloud.md "Who is signed in").

user, 2026-10-07: "can we get the auth info from the cloudflare access auth to identify email that
was authed with one time code to set the operators name dynamically?"

The hub sits behind Cloudflare Access (one-time PIN by email). Access hands every request it lets
through to the origin with a signed JWT in the `Cf-Access-Jwt-Assertion` header (and the browser
keeps the same token in the `CF_Authorization` cookie). Verified here, stdlib only:

    signature   RS256 (RSA PKCS#1 v1.5, SHA-256) against the team's keys,
                https://<team>/cdn-cgi/access/certs: cached an hour, fetched again for a key id it
                hasn't seen (at most every 30 s), the cached keys kept when a fetch fails.
                Any other alg (none, HS256, ...) is refused: the key type decides, never the token.
    aud         contains the Access application's AUD tag ([cloud] access_aud)
    iss         https://<team> ([cloud] access_team)
    exp / nbf   with 60 s leeway

Identity = the token's `email` claim, lower-cased, and which login it is from (session: jti / nonce /
iat, so a person's activity log has one "signed in" per login, not per request). A service token's JWT (the station's, on the
`/api/station/` Access app) has no email, only `common_name` = the client id: that is a machine,
never an operator. The `Cf-Access-Authenticated-User-Email` header is not used: anyone who reached
the origin some other way could send it; the signed token can't be made up.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import threading
import time
import urllib.request
from dataclasses import dataclass
from http.cookies import CookieError, SimpleCookie
from typing import Callable

HEADER = "Cf-Access-Jwt-Assertion"
COOKIE = "CF_Authorization"
LEEWAY_S = 60
KEYS_TTL_S = 3600            # the team's keys are cached this long
REFETCH_GAP_S = 30           # an unknown key id fetches again, at most this often (random kids can't hammer it)
RETRY_NO_KEYS_S = 5          # no keys at all yet (the first fetch failed): try again this often
FETCH_TIMEOUT_S = 5
MAX_TOKEN = 8192
MIN_RSA_BITS = 2048
# DER DigestInfo for SHA-256 (RFC 8017 9.2 note 1): what the signature's padding wraps around the hash.
SHA256_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")
B64URL = re.compile(r"[A-Za-z0-9_-]*")
TEAM = re.compile(r"[a-z0-9-]+(\.[a-z0-9-]+)+")


class AccessError(ValueError):
    """Why a token isn't accepted (logged, never shown in detail to the client)."""


@dataclass(frozen=True)
class Identity:
    email: str = ""              # a person who signed in ("" = a service token: a machine)
    service: str = ""            # a service token's client id (common_name)
    sub: str = ""
    session: str = ""            # which Access login this token is from: its jti / nonce if it has one,
                                 # else when it was issued (iat): one "signed in" each (sizeup/cad/activity.py)

    @property
    def person(self) -> bool:
        return bool(self.email)


def team_host(v: str) -> str:
    """"yoda.cloudflareaccess.com" from that or https://yoda.cloudflareaccess.com/ ("" if not a host name)."""
    v = v.strip().lower().removeprefix("https://").rstrip("/")
    return v if TEAM.fullmatch(v) else ""


def _b64(s: str) -> bytes:
    if not B64URL.fullmatch(s):
        raise AccessError("not base64url")
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _int(s: str) -> int:
    return int.from_bytes(_b64(s), "big")


def rsa_pkcs1_sha256_verify(n: int, e: int, message: bytes, sig: bytes) -> bool:
    """RSASSA-PKCS1-v1_5 with SHA-256 (RFC 8017 8.2.2): sig^e mod n must be exactly
    00 01 FF..FF 00 || DigestInfo(sha256(message)), compared in constant time."""
    k = (n.bit_length() + 7) // 8
    if len(sig) != k:
        return False
    s = int.from_bytes(sig, "big")
    if s >= n:
        return False
    em = pow(s, e, n).to_bytes(k, "big")
    t = SHA256_PREFIX + hashlib.sha256(message).digest()
    if k < len(t) + 11:
        return False
    want = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    return hmac.compare_digest(em, want)


def _fetch(url: str) -> dict:
    with urllib.request.urlopen(urllib.request.Request(url, headers={"Accept": "application/json"}),
                                timeout=FETCH_TIMEOUT_S) as r:
        return json.loads(r.read(1 << 20))


class AccessVerifier:
    """One per hub ([cloud] access_team + access_aud). identify(headers) -> Identity | None."""

    def __init__(self, team: str, aud: str, clock: Callable[[], float] = time.time,
                 fetch: Callable[[str], dict] = _fetch, log: Callable[[str], None] = print):
        self.team = team_host(team)
        if not self.team or not aud.strip():
            raise ValueError("Cloudflare Access: a team domain and an application AUD tag")
        self.aud = aud.strip()
        self.issuer = f"https://{self.team}"
        self.certs_url = f"{self.issuer}/cdn-cgi/access/certs"
        self.clock, self.fetch, self.log = clock, fetch, log
        self._keys: dict[str, tuple[int, int]] = {}
        self._fetched_at = 0.0          # last successful fetch
        self._tried_at = -1e18          # last attempt (successful or not)
        self._lock = threading.Lock()

    # -- the team's keys ------------------------------------------------------------------------------

    def _refresh(self) -> None:
        self._tried_at = self.clock()
        try:
            doc = self.fetch(self.certs_url)
            keys = {}
            for k in doc.get("keys") or []:
                if not isinstance(k, dict) or k.get("kty") != "RSA" or k.get("alg", "RS256") != "RS256" \
                        or k.get("use", "sig") != "sig" or not isinstance(k.get("kid"), str):
                    continue
                n, e = _int(k["n"]), _int(k["e"])
                if n.bit_length() >= MIN_RSA_BITS and e in (3, 65537):
                    keys[k["kid"]] = (n, e)
            if not keys:
                raise AccessError("no RS256 keys in the answer")
        except Exception as ex:          # network, JSON, a bad key: keep what we had
            self.log(f"cloudflare access: fetching {self.certs_url} failed ({type(ex).__name__}: {ex}); "
                     f"{'keeping the ' + str(len(self._keys)) + ' cached keys' if self._keys else 'no keys yet'}")
            return
        self._keys, self._fetched_at = keys, self._tried_at

    def key(self, kid: str) -> tuple[int, int] | None:
        with self._lock:
            now = self.clock()
            if now - self._fetched_at > KEYS_TTL_S or kid not in self._keys:
                gap = REFETCH_GAP_S if self._keys else RETRY_NO_KEYS_S
                if now - self._tried_at >= gap:
                    self._refresh()
            return self._keys.get(kid)

    # -- tokens ---------------------------------------------------------------------------------------

    def verify(self, token: str) -> dict:
        """The token's claims, or AccessError."""
        if not token or len(token) > MAX_TOKEN or token.count(".") != 2:
            raise AccessError("not a JWT")
        h64, p64, s64 = token.split(".")
        try:
            header, claims, sig = json.loads(_b64(h64)), json.loads(_b64(p64)), _b64(s64)
        except (ValueError, UnicodeDecodeError) as e:
            raise AccessError(f"malformed: {e}") from None
        if not isinstance(header, dict) or not isinstance(claims, dict):
            raise AccessError("malformed")
        if header.get("alg") != "RS256":
            raise AccessError(f"alg {header.get('alg')!r}: only RS256")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise AccessError("no key id")
        key = self.key(kid)
        if key is None:
            raise AccessError(f"unknown key id {kid[:40]!r}")
        if not rsa_pkcs1_sha256_verify(*key, f"{h64}.{p64}".encode(), sig):
            raise AccessError("bad signature")
        aud = claims.get("aud")
        if not (aud == self.aud or (isinstance(aud, list) and self.aud in aud)):
            raise AccessError("wrong audience (another Access application)")
        if claims.get("iss") != self.issuer:
            raise AccessError(f"wrong issuer {str(claims.get('iss'))[:80]!r}")
        now = self.clock()
        exp, nbf = claims.get("exp"), claims.get("nbf")
        if not isinstance(exp, (int, float)) or isinstance(exp, bool) or now > exp + LEEWAY_S:
            raise AccessError("expired")
        if nbf is not None and (not isinstance(nbf, (int, float)) or now + LEEWAY_S < nbf):
            raise AccessError("not valid yet")
        return claims

    def identity(self, token: str) -> Identity:
        c = self.verify(token)
        email = c.get("email")
        if isinstance(email, str) and "@" in email.strip():
            session = next((str(c[k]) for k in ("jti", "nonce", "iat") if c.get(k) not in (None, "")), "")
            return Identity(email=email.strip().lower()[:254], sub=str(c.get("sub") or ""), session=session[:64])
        return Identity(service=str(c.get("common_name") or ""), sub=str(c.get("sub") or ""))

    def identify(self, headers) -> Identity | None:
        """The verified identity on a request (the header, else the cookie), None without a good token."""
        token = (headers.get(HEADER) or "").strip()
        if not token:
            try:
                jar = SimpleCookie(headers.get("Cookie") or "")
                token = jar[COOKIE].value if COOKIE in jar else ""
            except CookieError:
                token = ""
        if not token:
            return None
        try:
            return self.identity(token)
        except AccessError as e:
            self.log(f"cloudflare access: token refused: {e}")
            return None
