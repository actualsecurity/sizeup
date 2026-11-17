"""Settings > Users: devices (#558): the console's side of the app's sign-in, served by CadApi at /api/cad/devices
(a person signed in through Cloudflare Access; the hub only). An admin sees and revokes anyone's; anyone else only
their own ("My devices"). Revoke deletes the device's refresh tokens (and, through the hooks of
AuthService.add_revoke_hook, its push endpoint) and writes the activity log; the app's next call is a 401
device_revoked. No token or hash ever leaves here.
"""

from __future__ import annotations

from .devices import MAX_DEVICES

PUBLIC = (
    "id",
    "email",
    "name",
    "platform",
    "app_version",
    "created",
    "last_seen",
    "revoked_at",
    "revoked_why",
    "active",
)


def listing(svc, email: str, admin: bool) -> dict:
    """GET /api/cad/devices: {devices, max, all}. email: the signed-in person."""
    rows = svc.list_devices("" if admin else email)
    return {"devices": [{k: d[k] for k in PUBLIC} for d in rows], "max": MAX_DEVICES, "all": admin}


def act(svc, body: dict, email: str, admin: bool) -> dict:
    """POST /api/cad/devices {action: "revoke", id}: ValueError (a 400) for a bad request or no such device,
    PermissionError (a 403) for a non-admin asking about someone else's device."""
    if body.get("action") != "revoke":
        raise ValueError("action: revoke")
    did = body.get("id")
    if not isinstance(did, str) or not did:
        raise ValueError("id: the device's id")
    mine = next((d for d in svc.list_devices(email) if d["id"] == did), None)
    if not admin and mine is None:
        raise PermissionError("Admins revoke other people's devices")
    d = svc.revoke_device(did, email)
    if d is None:
        raise ValueError("no such active device")
    return {"ok": True, "device": {k: d[k] for k in PUBLIC}}
