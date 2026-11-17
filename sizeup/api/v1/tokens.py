"""The tokens of the app's sign-in (#558, docs/mobile-api.md section 3.3). Stdlib only, no database.

Access token: a compact, JWT-shaped, HMAC-SHA256 token, 15 minutes.
    header   {"alg": "HS256", "kid": <key id>, "typ": "at+jwt"}
    payload  {"sub": <email>, "did": <device id>, "iat": <s>, "exp": <s>}
The algorithm is FIXED here: a header that says anything else is refused before any key is touched (the key and the
check decide, never the token: no `none`, no `HS512`, no `RS256` confusion). The MAC is compared in constant time
BEFORE the payload is read, so a forged token learns nothing (not even "expired"). exp has at most LEEWAY_S of leeway.
The key id must be one the hub knows: two are valid during a rotation (the first signs, all verify).

The keys: SIZEUP_MOBILE_TOKEN_KEYS, an OPTIONAL environment variable holding `kid:secret` entries separated by commas
or whitespace (for example `k1:<43+ random characters>`). Unset or malformed: Keys.load() returns None and the app's
sign-in refuses to start (every token fails, every auth route answers 404): the hub fails closed. The variable is set
from an SSM secret by a person (docs/mobile-api.md "Manual steps"); nothing in the task definition requires it.

Refresh tokens, authorization codes: opaque, 32 random bytes (43 URL-safe characters), stored only as sha256 (a stolen
hub.sqlite or its replica holds hashes nobody can turn back: the secret is 256 random bits, nothing to guess)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
from typing import Mapping

ENV = "SIZEUP_MOBILE_TOKEN_KEYS"
ACCESS_TTL_S = 900               # 15 minutes
LEEWAY_S = 30                    # exp is honoured this long past (the design: at most 60 s)
FUTURE_S = 60                    # an iat further in the future than this is refused
MAX_TOKEN = 1024
MIN_SECRET = 32                  # characters
KID = re.compile(r"[A-Za-z0-9_-]{1,16}")
SECRET = re.compile(r"[A-Za-z0-9_+/=.-]{%d,128}" % MIN_SECRET)
B64URL = re.compile(r"[A-Za-z0-9_-]+")
OPAQUE = re.compile(r"[A-Za-z0-9_-]{43}")
HEADER_FIXED = {"alg": "HS256", "typ": "at+jwt"}


class TokenError(ValueError):
    """Why an access token is not accepted. expired: a genuine token of ours, past its time (the one case an app
    answers with a silent refresh); anything else is an `unauthorized`."""

    def __init__(self, why: str, expired: bool = False):
        super().__init__(why)
        self.expired = expired


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64d(s: str) -> bytes:
    if not B64URL.fullmatch(s):
        raise TokenError("not base64url")
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def opaque() -> str:
    """A new refresh token or authorization code: 256 random bits."""
    return secrets.token_urlsafe(32)


def digest(token: str) -> str:
    """What is stored for an opaque token: sha256 hex."""
    return hashlib.sha256(token.encode()).hexdigest()


def well_formed(token: object) -> bool:
    return isinstance(token, str) and OPAQUE.fullmatch(token) is not None


class Keys:
    """The signing keys by kid. keys: {kid: secret}; `current` signs."""

    def __init__(self, keys: Mapping[str, str], current: str | None = None):
        if not keys:
            raise ValueError("no keys")
        for kid, secret in keys.items():
            if not KID.fullmatch(kid) or not SECRET.fullmatch(secret):
                raise ValueError("a key id (1-16 of A-Z a-z 0-9 _ -) and a secret of at least 32 characters")
        self._keys = {k: v.encode() for k, v in keys.items()}
        self.current = current or next(iter(keys))
        if self.current not in self._keys:
            raise ValueError("the signing key id is not among the keys")

    @classmethod
    def load(cls, env: Mapping[str, str], log=None) -> "Keys | None":
        """The keys from the environment, or None (unset, empty or malformed: said in the log, never the value)."""
        raw = (env.get(ENV) or "").strip()
        if not raw:
            return None
        keys: dict[str, str] = {}
        try:
            for entry in re.split(r"[,\s]+", raw):
                if not entry:
                    continue
                kid, sep, secret = entry.partition(":")
                if not sep or kid in keys:
                    raise ValueError("an entry is `kid:secret`, each kid once")
                keys[kid] = secret
            return cls(keys)
        except ValueError as e:
            if log:
                log(f"mobile sign-in: {ENV} is not usable ({e}): the app's sign-in stays off")
            return None

    def kids(self) -> list[str]:
        return list(self._keys)

    def _mac(self, kid: str, signing_input: bytes) -> bytes:
        return hmac.new(self._keys[kid], signing_input, hashlib.sha256).digest()

    def sign(self, sub: str, did: str, now: float) -> str:
        header = {"alg": "HS256", "kid": self.current, "typ": "at+jwt"}
        payload = {"sub": sub, "did": did, "iat": int(now), "exp": int(now) + ACCESS_TTL_S}
        h = _b64e(json.dumps(header, separators=(",", ":")).encode())
        p = _b64e(json.dumps(payload, separators=(",", ":")).encode())
        return f"{h}.{p}.{_b64e(self._mac(self.current, f'{h}.{p}'.encode()))}"

    def verify(self, token: str, now: float, allow_expired: bool = False) -> dict:
        """The payload ({sub, did, iat, exp}) of a token we signed, else TokenError. allow_expired: for a logout, where
        an expired token still proves the device (the MAC is checked all the same)."""
        try:
            return self._verify(token, now, allow_expired)
        except TokenError:
            raise
        except (ValueError, TypeError):          # bad JSON, bad base64, bad UTF-8: not ours
            raise TokenError("malformed") from None

    def _verify(self, token: str, now: float, allow_expired: bool) -> dict:
        if not isinstance(token, str) or not token or len(token) > MAX_TOKEN or token.count(".") != 2:
            raise TokenError("not a token")
        h64, p64, s64 = token.split(".")
        header = json.loads(_b64d(h64)) if B64URL.fullmatch(h64) else None
        if not isinstance(header, dict) or not isinstance(header.get("kid"), str):
            raise TokenError("malformed header")
        if {k: v for k, v in header.items() if k != "kid"} != HEADER_FIXED:
            raise TokenError("not our algorithm")                   # none, HS512, RS256 ...: the alg is fixed
        kid = header["kid"]
        if kid not in self._keys:
            raise TokenError("unknown key id")
        if not hmac.compare_digest(self._mac(kid, f"{h64}.{p64}".encode()), _b64d(s64)):
            raise TokenError("bad signature")
        claims = json.loads(_b64d(p64))
        sub, did, iat, exp = (claims.get(k) for k in ("sub", "did", "iat", "exp")) if isinstance(claims, dict) \
            else (None,) * 4
        if not (isinstance(sub, str) and "@" in sub and isinstance(did, str) and did
                and all(isinstance(n, (int, float)) and not isinstance(n, bool) for n in (iat, exp))):
            raise TokenError("malformed claims")
        if iat > now + FUTURE_S:
            raise TokenError("issued in the future")
        if now > exp + LEEWAY_S and not allow_expired:
            raise TokenError("expired", expired=True)
        return {"sub": sub, "did": did, "iat": iat, "exp": exp}


def bearer(headers) -> str:
    """The token of an `Authorization: Bearer <token>` header, else "" (the only place a token is read from: never a
    cookie, never a query string)."""
    v = (headers.get("Authorization") or "").strip() if headers is not None else ""
    return v[7:].strip() if v[:7].lower() == "bearer " else ""
