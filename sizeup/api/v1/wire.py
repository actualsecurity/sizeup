"""Builds the app's sign-in service from the hub's config and objects (sizeup/app.py make_server, #558). The one place
that knows both sides, so the rest of this package never imports the CAD or the hub.

Off (None, and every /auth/v1 and /.well-known path a 404) unless ALL of: this is the hub, `[mobile] enabled`, a hub
database, Cloudflare Access configured (authorize needs a verified person) and a usable token key in
SIZEUP_MOBILE_TOKEN_KEYS (tokens.py). A missing key is said once in the log and nothing is signed or verified: the
hub fails closed. Building reads nothing from a database and writes nothing."""
from __future__ import annotations

import os
import time
from typing import Mapping

from .auth import AuthConfig, AuthService, Client
from .devices import DeviceDb
from .people import People
from .tokens import ENV, Keys


def make_auth(cfg, app, access, switches, log=print, env: Mapping[str, str] | None = None) -> AuthService | None:
    if getattr(cfg, "cloud_role", "") != "hub" or not getattr(cfg, "mobile_enabled", False):
        return None
    mdb, cad = getattr(app, "mdb", None), getattr(app, "cad", None)
    if mdb is None or cad is None:
        return None
    if access is None:
        log("mobile sign-in: off: it needs Cloudflare Access ([cloud] access_team and access_aud)")
        return None
    keys = Keys.load(os.environ if env is None else env, log)
    if keys is None:
        log(f"mobile sign-in: off: {ENV} is not set (kid:secret): no token is issued or accepted")
        return None
    config = AuthConfig(tuple(Client(i, p, tuple(u)) for i, p, u in cfg.mobile_clients), cfg.mobile_apple_team_id,
                        tuple(cfg.mobile_apple_bundle_ids), cfg.mobile_android_package, tuple(cfg.mobile_android_sha256))
    people = People(lambda email: cad.store.one("SELECT name, role, active FROM operators WHERE email = ?", (email,)),
                    lambda: cad.store.rev)

    def activity(email, kind, summary, ip, ua, ok, route, session):
        cad.activity.record(email, kind, route, summary, None, ok, ip, ua, session)
    svc = AuthService(DeviceDb(mdb, time.time), keys, people, access, config, switches, activity, log)
    cad.devices = svc                                   # Settings > Users: devices (CadApi /api/cad/devices)
    cad.settings.on_deactivate = svc.user_deactivated   # a deactivated person's devices are revoked at once
    log(f"mobile sign-in: on (key ids {', '.join(keys.kids())}, {len(config.clients)} app(s))")
    return svc
