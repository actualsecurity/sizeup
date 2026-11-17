"""The hub's hook (sizeup/app.py make_server): build the PushService once and hand it to the pushes (the fan-out), the
app's routes (registration) and the sign-in's Revoke (`service.drop_device`). Nothing is read or written here: the table
is created by the first registration, the platform applications come from optional environment variables (sns.py), and
the service sends nothing while the master switch or the `push` feature is off."""
from __future__ import annotations

from pathlib import Path

from sizeup.push import service, sns
from sizeup.push.devices import PushDevices


def wire(pushes, state_dir: str | Path, switches, store, may_write=None, log=print, env=None):
    """Build and install the service. pushes: notify.Pushes. state_dir: where hub.sqlite lives. switches: the app's
    api.v1 Switches (the `push` flag). store: the CAD store, read for a person's role. Returns the PushService."""
    def role_of(email: str):
        """admin | dispatcher | viewer for an active person, None for an unknown or deactivated one (read only: it never
        adds an operator the way a sign-in does)."""
        r = store.one("SELECT role, active FROM operators WHERE email = ?", (str(email).strip().lower(),))
        return (r["role"] if r["role"] in ("admin", "dispatcher", "viewer") else "viewer") if r and r["active"] else None

    def devices_policy() -> dict:
        d = (pushes.settings() or {}).get("devices")
        return d if isinstance(d, dict) else {}
    devices = PushDevices(Path(state_dir) / "hub.sqlite", may_write=may_write, log=log)
    svc = service.PushService(devices, sns.Apps.from_env(env), settings=devices_policy,
                              active=lambda: switches.allows("push"), role_of=role_of, log=log)
    pushes.devices = svc
    service.install(svc)
    return svc
