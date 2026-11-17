"""The app's sign-in (#558, docs/mobile-api.md section 3.3, option B): Cloudflare Access hands off to hub-issued per-device
tokens. OAuth 2.0 authorization code + PKCE (RFC 6749, 7636, 7009; the native-app rules of RFC 8252), so AppAuth works
unchanged.

    GET  /auth/v1/authorize   the ONE place a person meets Access: the hub verifies Access's own JWT (the console's
                              AccessVerifier), resolves the person (Settings > Users: unknown or deactivated = a page,
                              never a redirect), checks client_id and redirect_uri against the config (exact match),
                              stores a 60 s single-use code bound to the PKCE challenge and the Access session, and
                              answers 302 <redirect_uri>?code=&state=
    POST /auth/v1/token       authorization_code + code_verifier (a device is made, at most 3 active per person), and
                              refresh_token (ROTATED on every use; a token already exchanged revokes the device)
    POST /auth/v1/revoke      RFC 7009: the device of the refresh or access token presented is revoked, always 200
    GET  /.well-known/apple-app-site-association, /.well-known/assetlinks.json   from config (public values), else 404

token and revoke are the later Cloudflare Bypass's (an Access change a person makes after these tests are proven: they
carry no Access identity, only the code or token itself). Their errors are RFC 6749's ({"error": "invalid_grant",
"error_description"}) because AppAuth parses that shape; every /api/v1 error is the v1 envelope. Access's JWT is read once
at authorize and never given to the app. The service is built only with a token key (tokens.ENV), a hub database and the
`[mobile] enabled` switch; the `auth` flag then gates every route (routes.py), per request.

Nothing here reads a database at import or construction: the first write creates the tables (devices.py)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import parse_qs, urlencode, urlsplit

from .devices import MAX_DEVICES, NAME_MAX, VERSION_MAX, DeviceDb, DeviceLimit
from .people import People
from .principal import Principal, Refusal
from .routes import MIN_APP, Ctx, Raw, Route
from .tokens import ACCESS_TTL_S, Keys, TokenError, bearer, opaque, well_formed

CHALLENGE = re.compile(r"[A-Za-z0-9_-]{43}")                 # base64url(sha256), unpadded
VERIFIER = re.compile(r"[A-Za-z0-9._~-]{43,128}")             # RFC 7636 4.1
STATE_MAX = 512
VERSION = re.compile(r"[0-9A-Za-z._+-]{1,%d}" % VERSION_MAX)
DEVICE_ID = re.compile(r"dv[0-9a-f]{16}")
BODY_MAX = 4096
WELL_KNOWN_MAX_AGE = 300
REFRESH_LOG_EVERY_S = 86400          # the activity log says "refresh" once a day per device, not each time
REFUSED_LOG_EVERY_S = 3600           # ... and a revoked device's refused calls once an hour
TOKEN_RATE, TOKEN_BURST = 10 / 60, 10.0     # /auth/v1/token and /revoke: 10 a minute per address (the design)
MAX_ADDRS = 2048

PAGE_HEADERS = {"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'",
                "X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
                "Cache-Control": "no-store"}
PAGE = ("<!doctype html><meta charset=utf-8><meta name=viewport content=\"width=device-width,initial-scale=1\">"
        "<title>sizeup</title><style>body{font:16px/1.4 system-ui,sans-serif;max-width:30em;margin:3em auto;padding:0 16px;"
        "color:#1c1c1e;background:#fff}@media(prefers-color-scheme:dark){body{color:#f2f2f7;background:#1c1c1e}}"
        "h1{font-size:1.2em}p{margin:.6em 0}</style><h1>%s</h1><p>%s</p>")


@dataclass(frozen=True)
class Client:
    id: str
    platform: str                    # ios | android
    redirect_uris: tuple[str, ...]


@dataclass(frozen=True)
class AuthConfig:
    """The allow-lists and the link-association values (config, public, never secret: [mobile] in the hub's config)."""
    clients: tuple[Client, ...] = ()
    apple_team_id: str = ""
    apple_bundle_ids: tuple[str, ...] = ()
    android_package: str = ""
    android_sha256: tuple[str, ...] = ()

    def client(self, cid: str) -> Client | None:
        return next((c for c in self.clients if hmac.compare_digest(c.id.encode(), cid.encode())), None)


def page(status: int, title: str, text: str) -> tuple[int, Raw]:
    """An HTML page for the person in the sign-in sheet. Fixed text only: nothing a request said is ever put in it."""
    return status, Raw((PAGE % (title, text)).encode(), "text/html; charset=utf-8", dict(PAGE_HEADERS))


def oauth_error(status: int, code: str, text: str = "", retry_after: int | None = None) -> tuple[int, Raw]:
    """RFC 6749 5.2: {"error": <code>, "error_description": <text>}; never cached."""
    h = {"Pragma": "no-cache"}
    if retry_after:
        h["Retry-After"] = str(retry_after)
    body = {"error": code, **({"error_description": text} if text else {})}
    return status, Raw(json.dumps(body, separators=(",", ":")).encode(), headers=h)


def _version(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:4])


def too_old(platform: str, version: str) -> bool:
    """Below the oldest app this platform may run (routes.MIN_APP: 0.0.0 refuses nothing yet, #566 raises it)."""
    floor = _version(MIN_APP.get(platform, "0.0.0"))
    return any(floor) and (_version(version) or (0,)) < floor


class AddrLimit:
    """A token bucket per client address for the sign-in's POSTs, in memory and bounded."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock, self._b, self._lock = clock, {}, threading.Lock()

    def take(self, addr: str) -> int:
        """0: go on. Else the seconds to wait."""
        now = self.clock()
        with self._lock:
            if len(self._b) >= MAX_ADDRS and addr not in self._b:
                self._b = {k: v for k, v in self._b.items() if now - v[1] < 120} if len(self._b) >= MAX_ADDRS else self._b
                if len(self._b) >= MAX_ADDRS:
                    self._b.clear()
            tokens, at = self._b.get(addr, (TOKEN_BURST, now))
            tokens = min(TOKEN_BURST, tokens + (now - at) * TOKEN_RATE)
            if tokens >= 1.0:
                self._b[addr] = (tokens - 1.0, now)
                return 0
            self._b[addr] = (tokens, now)
            return max(1, int((1.0 - tokens) / TOKEN_RATE) + 1)


def _clean(v: str, n: int) -> str:
    return "".join(ch for ch in (v or "") if ch.isprintable()).strip()[:n]


@dataclass
class AuthService:
    db: DeviceDb
    keys: Keys
    people: People
    access: object                                     # sizeup.cloud.access.AccessVerifier (identify(headers))
    config: AuthConfig
    switches: object                                   # Switches: the resolver answers nothing while `auth` is off
    activity: Callable[..., None] | None = None        # (email, kind, summary, ip, ua, ok, route, session)
    log: Callable[[str], None] = print
    clock: Callable[[], float] = time.time
    on_revoke: list = field(default_factory=list)      # fn(device dict): the push endpoint of a device dies with it
    _limit: AddrLimit = field(default_factory=AddrLimit)
    _logged: dict = field(default_factory=dict)

    # -- plumbing -------------------------------------------------------------------------------------------------

    def routes(self) -> list[Route]:
        """The sign-in's routes (their flag is `auth`; the handler's catch-all and limits are v1's)."""
        return [Route("GET", "/auth/v1/authorize", self.authorize, "public", "auth"),
                Route("POST", "/auth/v1/token", self.token, "public", "auth"),
                Route("POST", "/auth/v1/revoke", self.revoke, "public", "auth"),
                Route("GET", "/.well-known/apple-app-site-association", self.apple, "public", "auth"),
                Route("GET", "/.well-known/assetlinks.json", self.android, "public", "auth")]

    def _note(self, email: str, kind: str, summary: str, ctx_or_headers=None, peer: str = "", ok: bool = True,
              route: str = "", session: str = "", throttle: str = "", every: float = 0) -> None:
        """A line in the person's activity (sizeup/cad/activity.py): device id, app version, address and browser are in
        the summary and columns. Never in the way of the request. throttle: a key said at most every `every` seconds."""
        if self.activity is None or not email:
            return
        now = self.clock()
        if throttle:
            if now - self._logged.get(throttle, -1e18) < every:
                return
            if len(self._logged) > 4096:
                self._logged.clear()
            self._logged[throttle] = now
        h = ctx_or_headers
        ip = (h.get("Cf-Connecting-Ip") if h is not None else "") or peer or ""
        ua = (h.get("User-Agent") if h is not None else "") or ""
        try:
            self.activity(email, kind, summary, ip, ua, ok, route, session)
        except Exception as e:                      # noqa: BLE001 - a log line never breaks a sign-in
            self.log(f"mobile sign-in: activity not recorded ({type(e).__name__})")

    def _revoked(self, d: dict, how: str, headers=None, peer: str = "", by: str = "") -> None:
        self._note(d["email"], "revoked", f"Device {d['name'] or d['id']} ({d['id']}) revoked: {how}", headers, peer,
                   route="auth")
        for fn in list(self.on_revoke):              # the push endpoint (a later pull request): dies with the device
            try:
                fn(d)
            except Exception as e:                  # noqa: BLE001
                self.log(f"mobile sign-in: a revoke hook failed ({type(e).__name__})")

    # -- the resolver (replaces none_yet) -------------------------------------------------------------------------

    def resolve(self, headers):
        """A Principal for a valid bearer of an active device of an active person, a Refusal with the v1 code when the
        token is ours but the device or person is not good (or it has expired), else None (a 401). The device and the
        person are read on EVERY request (the person through the settings rev's cache), so a revoke or deactivation
        takes effect on the next call, not at token expiry. Only the Authorization header is read: an Access cookie,
        a station's token or a feed's are not ours and fail the signature."""
        if not self.switches.allows("auth"):
            return None
        token = bearer(headers)
        if not token:
            return None
        try:
            claims = self.keys.verify(token, self.clock())
        except TokenError as e:
            return Refusal("token_expired", "the access token has expired: refresh it") if e.expired else None
        d = self.db.device(claims["did"])
        if d is None or d["email"] != claims["sub"]:
            return None
        if not d["active"]:
            self._note(d["email"], "refused", f"Revoked device {d['id']} used its token", headers, route="api/v1",
                       ok=False, throttle=f"r:{d['id']}", every=REFUSED_LOG_EVERY_S)
            return Refusal("device_revoked", "this device was signed out: sign in again")
        p = self.people.get(d["email"])
        if p is None or not p.active:
            self._deactivated(d["email"])
            return Refusal("user_inactive", "this account is not active")
        self.db.touch(d["id"])
        return Principal(d["id"], d["email"], p.role, p.name)

    def _deactivated(self, email: str) -> None:
        """An inactive or unknown person's devices are revoked (the Settings hook does it at once; this is the net)."""
        for d in self.db.revoke_all(email, "", "user not active"):
            self._revoked(d, "the person is not active")

    def user_deactivated(self, email: str) -> None:
        """Settings > Users deactivated this person (or changed their email): their devices are revoked (#558)."""
        if email:
            self._deactivated(email.strip().lower())

    # -- authorize ------------------------------------------------------------------------------------------------

    def authorize(self, ctx: Ctx):
        if ctx.method != "GET":
            return page(405, "Not allowed", "Open this from the sizeup app.")

        def q(k: str) -> str:
            return (ctx.query.get(k) or [""])[0]
        client, redirect = self.config.client(q("client_id")), q("redirect_uri")
        # an app or a return address we do not know is never answered with a redirect (RFC 6749 4.1.2.1)
        if client is None or redirect not in client.redirect_uris:
            return page(400, "Cannot sign in", "This app is not set up for this hub.")
        state, challenge = q("state"), q("code_challenge")
        if (q("response_type") != "code" or q("code_challenge_method") != "S256" or not CHALLENGE.fullmatch(challenge)
                or not 1 <= len(state) <= STATE_MAX):
            return page(400, "Cannot sign in", "The app's sign-in request is not valid: update the app and try again.")
        who = self.access.identify(ctx.headers) if self.access is not None else None
        if who is None or not getattr(who, "person", False):
            return page(401, "Sign in required", "Sign in through Cloudflare Access, then try again from the app.")
        email = who.email
        p = self.people.get(email)
        if p is None or not p.active:
            self._note(email, "refused", "Device sign-in refused: " + ("unknown user" if p is None else "deactivated"),
                       ctx.headers, ctx.peer, False, "GET /auth/v1/authorize", who.session)
            return page(403, "Not allowed", "This account cannot sign in to the app. Open the console once, or ask an "
                        "admin." if p is None else "This account is deactivated. Ask an admin.")
        own = q("device_id")
        known = DEVICE_ID.fullmatch(own) and (d := self.db.device(own)) and d["active"] and d["email"] == email
        if not known and self.db.active_count(email) >= MAX_DEVICES:
            self._note(email, "refused", f"Device sign-in refused: {MAX_DEVICES} devices already", ctx.headers,
                       ctx.peer, False, "GET /auth/v1/authorize", who.session)
            return page(403, "Too many devices", f"{MAX_DEVICES} devices are signed in already. Sign one out in "
                        "Settings > Users on the web, or in the app on that device, then try again.")
        code = opaque()
        self.db.put_code(code, email, who.session, challenge, client.id, redirect, _clean(q("device_name"), NAME_MAX))
        sep = "&" if "?" in redirect else "?"
        where = f"{redirect}{sep}{urlencode({'code': code, 'state': state})}"      # redirect: the allow-list's, exactly
        return 302, Raw(b"", "text/plain; charset=utf-8", {"Location": where})

    # -- token ----------------------------------------------------------------------------------------------------

    def _form(self, ctx: Ctx) -> dict | None:
        """The POST's parameters (form-encoded as RFC 6749 says, or a JSON object), strings only; None: unusable."""
        raw = ctx.read(BODY_MAX) if ctx.read else None
        if raw is None:
            return None
        try:
            if "json" in (ctx.headers.get("Content-Type") or "").lower():
                obj = json.loads(raw or b"{}")
                return {k: v for k, v in obj.items() if isinstance(v, str)} if isinstance(obj, dict) else None
            return {k: v[0] for k, v in parse_qs(raw.decode(), keep_blank_values=True, strict_parsing=bool(raw),
                                                  max_num_fields=20).items()}
        except (ValueError, UnicodeDecodeError, AttributeError):
            return None

    def _tokens(self, d: dict, refresh: str) -> tuple[int, Raw]:
        body = {"access_token": self.keys.sign(d["email"], d["id"], self.clock()), "token_type": "Bearer",
                "expires_in": ACCESS_TTL_S, "refresh_token": refresh, "device_id": d["id"]}
        return 200, Raw(json.dumps(body, separators=(",", ":")).encode(), headers={"Pragma": "no-cache"})

    def token(self, ctx: Ctx):
        wait = self._limit.take(ctx.peer or "-")
        if wait:
            return oauth_error(429, "rate_limited", "too many requests: slow down", wait)
        form = self._form(ctx)
        if form is None:
            return oauth_error(400, "invalid_request", "the body: form-encoded or JSON, under 4 KB")
        grant = form.get("grant_type", "")
        if grant == "authorization_code":
            return self._code_grant(form, ctx)
        if grant == "refresh_token":
            return self._refresh_grant(form, ctx)
        return oauth_error(400, "unsupported_grant_type", "authorization_code or refresh_token")

    def _code_grant(self, f: dict, ctx: Ctx):
        client, code, verifier = self.config.client(f.get("client_id", "")), f.get("code", ""), f.get("code_verifier", "")
        if not (well_formed(code) and VERIFIER.fullmatch(verifier)):
            return oauth_error(400, "invalid_request", "code and code_verifier")
        # spent now, whatever else is wrong with the request: a code is tried once (a wrong client, redirect or
        # verifier does not leave it to be tried again)
        status, row = self.db.take_code(code)
        if status == "replay":
            if row and row["device_id"]:
                self._revoked({"id": row["device_id"], "email": row["email"], "name": ""}, "authorization code replayed",
                              ctx.headers, ctx.peer)
            self._note(row["email"] if row else "", "refused", "Authorization code replayed", ctx.headers, ctx.peer,
                       False, "POST /auth/v1/token")
        if client is None:
            return oauth_error(401, "invalid_client", "unknown client")
        if f.get("redirect_uri") not in client.redirect_uris:
            return oauth_error(400, "invalid_grant", "the code is not valid")
        if status != "ok" or row is None:
            return oauth_error(400, "invalid_grant", "the code is not valid")
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        if (not hmac.compare_digest(challenge, row["challenge"]) or row["client_id"] != client.id
                or row["redirect_uri"] != f["redirect_uri"]):
            self._note(row["email"], "refused", "Device sign-in refused: the code did not match its request", ctx.headers,
                       ctx.peer, False, "POST /auth/v1/token", row["session"])
            return oauth_error(400, "invalid_grant", "the code is not valid")
        p = self.people.get(row["email"])
        if p is None or not p.active:
            return oauth_error(401, "user_inactive", "this account is not active")
        version = f.get("app_version", "") if VERSION.fullmatch(f.get("app_version", "")) else ""
        if too_old(client.platform, version):
            return oauth_error(426, "upgrade_required", "update the app")
        own = f.get("device_id", "")
        try:
            d, refresh = self.db.enroll(code, row["email"], row["device_name"] or f"{client.platform} device",
                                        client.platform, version, client.id, own if DEVICE_ID.fullmatch(own) else "")
        except DeviceLimit:
            self._note(row["email"], "refused", f"Device sign-in refused: {MAX_DEVICES} devices already", ctx.headers,
                       ctx.peer, False, "POST /auth/v1/token", row["session"])
            return oauth_error(403, "device_limit", f"{MAX_DEVICES} devices are signed in: sign one out first")
        self._note(d["email"], "device_enrolled", f"Device {d['name']} ({d['platform']} {d['app_version'] or '?'}) "
                   f"enrolled as {d['id']}", ctx.headers, ctx.peer, route="POST /auth/v1/token", session=row["session"])
        return self._tokens(d, refresh)

    def _refresh_grant(self, f: dict, ctx: Ctx):
        token = f.get("refresh_token", "")
        if not well_formed(token):
            return oauth_error(400, "invalid_grant", "the refresh token is not valid")
        did = self.db.device_of_refresh(token)
        d = self.db.device(did) if did else None
        if d is None or (f.get("client_id") and f["client_id"] != d["client_id"]):
            return oauth_error(400, "invalid_grant", "the refresh token is not valid")
        if not d["active"]:
            return oauth_error(401, "device_revoked", "this device was signed out: sign in again")
        p = self.people.get(d["email"])
        if p is None or not p.active:                  # checked before the token is spent: nothing is left half done
            self._deactivated(d["email"])
            return oauth_error(401, "user_inactive", "this account is not active")
        version = f.get("app_version", "") if VERSION.fullmatch(f.get("app_version", "")) else ""
        if too_old(d["platform"], version or d["app_version"]):
            return oauth_error(426, "upgrade_required", "update the app")
        status, d2, new = self.db.rotate(token, version)
        if status == "reuse":
            self._note(d["email"], "refused", f"Refresh token reused: device {d['id']} revoked", ctx.headers, ctx.peer,
                       False, "POST /auth/v1/token")
            self._revoked(d2 or d, "refresh token reused", ctx.headers, ctx.peer)
            return oauth_error(400, "invalid_grant", "the refresh token is not valid")
        if status == "revoked":
            return oauth_error(401, "device_revoked", "this device was signed out: sign in again")
        if status != "ok" or d2 is None:
            return oauth_error(400, "invalid_grant", "the refresh token is not valid: sign in again")
        self._note(d2["email"], "refresh", f"Device {d2['name']} ({d2['id']}) refreshed its session", ctx.headers,
                   ctx.peer, route="POST /auth/v1/token", throttle=f"f:{d2['id']}", every=REFRESH_LOG_EVERY_S)
        return self._tokens(d2, new)

    # -- revoke (RFC 7009) ----------------------------------------------------------------------------------------

    def revoke(self, ctx: Ctx):
        """The device of a refresh or access token the caller holds is signed out. Always 200: whether the token was
        good is not told (RFC 7009 2.2)."""
        wait = self._limit.take(ctx.peer or "-")
        if wait:
            return oauth_error(429, "rate_limited", "too many requests: slow down", wait)
        f = self._form(ctx)
        if f is None:
            return oauth_error(400, "invalid_request", "the body: form-encoded or JSON, under 4 KB")
        token, did = f.get("token", ""), ""
        if well_formed(token):
            did = self.db.device_of_refresh(token)
        elif token.count(".") == 2:
            try:
                c = self.keys.verify(token, self.clock(), allow_expired=True)
                d = self.db.device(c["did"])
                did = d["id"] if d and d["email"] == c["sub"] else ""
            except TokenError:
                did = ""
        if did and (gone := self.db.revoke(did, "", "signed out")):
            self._revoked(gone, "signed out by the app", ctx.headers, ctx.peer)
        return 200, Raw(b"{}", headers={"Pragma": "no-cache"})

    # -- the link-association files -------------------------------------------------------------------------------

    def _public_json(self, obj) -> tuple[int, Raw]:
        return 200, Raw(json.dumps(obj, separators=(",", ":")).encode(),
                        headers={"Cache-Control": f"public, max-age={WELL_KNOWN_MAX_AGE}"})

    @staticmethod
    def _absent() -> tuple[int, Raw]:
        from .errors import body
        return 404, Raw(body("not_found", "no such endpoint"))

    def apple(self, ctx: Ctx):
        c = self.config
        if not (c.apple_team_id and c.apple_bundle_ids):
            return self._absent()
        apps = [f"{c.apple_team_id}.{b}" for b in c.apple_bundle_ids]
        paths = sorted({urlsplit(u).path for cl in c.clients if cl.platform == "ios" for u in cl.redirect_uris
                        if u.startswith("https://") and urlsplit(u).path})
        return self._public_json({"applinks": {"details": [{"appIDs": apps, "components": [{"/": p} for p in paths]}]},
                                  "webcredentials": {"apps": apps}})

    def android(self, ctx: Ctx):
        c = self.config
        if not (c.android_package and c.android_sha256):
            return self._absent()
        return self._public_json([{"relation": ["delegate_permission/common.handle_all_urls"],
                                   "target": {"namespace": "android_app", "package_name": c.android_package,
                                              "sha256_cert_fingerprints": list(c.android_sha256)}}])

    # -- Settings > Users: devices (sizeup/api/v1/devadmin.py) ----------------------------------------------------

    def list_devices(self, email: str = "") -> list[dict]:
        return self.db.devices(email)

    def revoke_device(self, did: str, by: str, why: str = "revoked in Settings") -> dict | None:
        d = self.db.revoke(did, by, why)
        if d is not None:
            self._revoked(d, f"{why} by {by}" if by else why)
        return d
