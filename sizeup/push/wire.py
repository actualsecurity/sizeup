"""The hub's hook (sizeup/app.py make_server): build the PushService once and hand it to the pushes (the fan-out), the
app's routes (registration) and, later, the sign-in's Revoke (`service.drop_device`, not called yet: #558). Nothing is
read or written here: the table is created by the first registration, the platform applications come from optional
environment variables (sns.py), and the service sends nothing while the master switch or the `push` feature is off.

#558 wires `device_active` (a revoked device of an active person gets nothing) and calls `drop_device` on Revoke."""
from __future__ import annotations

from pathlib import Path

from sizeup.cad.settings import OPERATOR_ROLES
from sizeup.push import service, sns
from sizeup.push.devices import PushDevices


def role_for(store, email: str) -> str | None:
    """admin | dispatcher | viewer for an active person, None for an unknown or deactivated one. Read only: it never
    adds an operator the way a console sign-in does (Settings.person), and it differs from Settings.standing on purpose:
    a deactivated person is a viewer there but gets NO push here; an unmapped role is a viewer; with no active admin yet
    ("bootstrap", where standing makes everyone an admin) nobody is elevated, so `status` reaches nobody (fails closed)
    while alerts and intel still reach every active person. The email is matched lowercased, as person() stores it.

    NOTE for #558: the operators row is made by Settings.person(), which only a console request (CadApi) calls. A
    person who signs in to the app but never opened the console has no row here and gets no pushes until the sign-in
    makes one."""
    r = store.one("SELECT role, active FROM operators WHERE email = ?", (str(email).strip().lower(),))
    if not r or not r["active"]:
        return None
    return r["role"] if r["role"] in OPERATOR_ROLES else "viewer"


def wire(pushes, state_dir: str | Path, switches, store, may_write=None, log=print, env=None, device_active=None):
    """Build and install the service. pushes: notify.Pushes. state_dir: where hub.sqlite lives. switches: the app's
    api.v1 Switches (the `push` flag). store: the CAD store, read for a person's role. Returns the PushService."""
    def devices_policy() -> dict:
        d = (pushes.settings() or {}).get("devices")
        return d if isinstance(d, dict) else {}
    devices = PushDevices(Path(state_dir) / "hub.sqlite", may_write=may_write, log=log)
    svc = service.PushService(devices, sns.Apps.from_env(env), settings=devices_policy,
                              active=lambda: switches.allows("push"), role_of=lambda e: role_for(store, e),
                              device_active=device_active, log=log)
    pushes.devices = svc
    service.install(svc)
    return svc
