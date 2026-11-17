"""The hub's hook (sizeup/app.py make_server): build the PushService once and hand it to the pushes (the fan-out), the
app's routes (registration) and, later, the sign-in's Revoke (`service.drop_device`, not called yet: #558). Nothing is
read or written here: the table is created by the first registration, the platform applications come from optional
environment variables (sns.py), and the service sends nothing while the master switch or the `push` feature is off.

#558 wires `device_active` (a revoked device of an active person gets nothing) and calls `drop_device` on Revoke. At
integration, in one line each: `device_active=auth.device_active` here, and `auth.add_revoke_hook(drop_device)` (PR 2's
`AuthService.device_active(device_id)` checks revoked AND person active; `add_revoke_hook(fn(device))` runs on
Revoke)."""
from __future__ import annotations

from pathlib import Path

from sizeup.cad.settings import OPERATOR_ROLES
from sizeup.push import service, sns
from sizeup.push.devices import PushDevices


def role_for(store, email: str) -> str | None:
    """admin | dispatcher | viewer for an active person, None for an unknown or deactivated one. Read only on purpose:
    it never adds an operator the way a console sign-in does (Settings.person). The app's sign-in (#558 / PR 2)
    refuses anyone without an `operators` row, so every signed-in person has one. It differs from Settings.standing deliberately: a
    deactivated person is a viewer there but gets NO push here; an unmapped role is a viewer; with no active admin yet
    ("bootstrap", where standing makes everyone an admin) nobody is elevated, so `status` reaches nobody (fails closed)
    while alerts and intel still reach every active person. The email is matched lowercased, as person() stores it."""
    r = store.one("SELECT role, active FROM operators WHERE email = ?", (str(email).strip().lower(),))
    if not r or not r["active"]:
        return None
    return r["role"] if r["role"] in OPERATOR_ROLES else "viewer"


def unit_for(store, code: str) -> str | None:
    """The CAD list's own name for a unit code ("Engine 64"), "" for a unit with none, None for a code that is not on the
    list (the same table and test as Alarms.unit_name, which cannot tell the two apart)."""
    r = store.one("SELECT spoken, title FROM units WHERE name = ?", (str(code).strip(),))
    return None if r is None else (r["spoken"] or r["title"] or "")


def intel_labels(intel) -> frozenset:
    """Every part an intel push title is built from (sizeup/intel/push.py compose: the pack's label, the category's label
    or key, the town): the loaded packs' labels and the CAD's town names. Read at every intel push (packs reload)."""
    from sizeup.intel.rules import town_names
    out = set(town_names().values())
    for p in getattr(intel, "packs", None) or []:
        out.add(p.label)
        for key, cat in (p.categories or {}).items():
            out.update({str(key), str((cat or {}).get("label") or "")})
    out.discard("")
    return frozenset(out)


def wire(pushes, state_dir: str | Path, switches, store, may_write=None, log=print, env=None, device_active=None,
         intel=None):
    """Build and install the service. pushes: notify.Pushes. state_dir: where hub.sqlite lives. switches: the app's
    api.v1 Switches (the `push` flag). store: the CAD store, read for a person's role. Returns the PushService."""
    def devices_policy() -> dict:
        d = (pushes.settings() or {}).get("devices")
        return d if isinstance(d, dict) else {}
    devices = PushDevices(Path(state_dir) / "hub.sqlite", may_write=may_write, log=log)
    svc = service.PushService(devices, sns.Apps.from_env(env), settings=devices_policy,
                              active=lambda: switches.allows("push"), role_of=lambda e: role_for(store, e),
                              device_active=device_active, log=log, unit_of=lambda c: unit_for(store, c),
                              intel_labels=lambda: intel_labels(intel))
    if device_active is None:
        log("push: device_active not wired (#558): revoked devices keep receiving pushes; push stays off until the "
            "`push` flag has a source and the sign-in wires it")
    pushes.devices = svc
    service.install(svc)
    return svc
