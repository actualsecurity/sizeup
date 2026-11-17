"""The writes of /api/v1 (#562, docs/mobile-api.md sections 3.2, 8 and 12): a unit's status, an alarm acknowledged and a
note on an incident.

Every one goes through the console's own code, so every rule, validation and audit line is the console's:

- who: the device's Principal (principal.py) becomes a person Identity (its email); a handler never reaches CadApi
  without one (the `permit` trap, section 1.4). The role is the console's (CadApi.permit, `needs`: unit status, alarm ack
  and notes are "dispatch"; a viewer is a 403), read per request, so a role changed in Settings acts at once.
- what: CadApi.post on the console's own path (so Cad.unit_status / Alarms.acknowledge / Cad.note, the operator's name
  from Settings and the activity row (CadApi.did) are the console's). Nothing of the console's logic is repeated here.
- audit: that activity row's "browser" is `via=ios app=1.2.0 device=<id> client_at=<unix>` (activity.client_tag).
- Idempotency-Key (idempotency.py): required; a retry replays the first answer, the same key with another body is a 422.
- If-Match <rev> (rev.py) on a unit's status: a stale rev is a 412 with the current object; on a queued action (one with
  `client_at`) it is a 409 `unit_changed` instead: "not applied". Notes are append-only: no If-Match.
- Offline: `client_at` is the app's own time of the action. The server orders by receive time, writes `client_at` in the
  audit (and a chronology line when it is more than a minute behind), and clamps one more than 5 minutes ahead of the
  server's clock (flagged `client_at_clamped`). An action that no longer makes sense is a 409 with a machine-readable
  `reason` and the current object (incident_closed, unit_changed, already_acknowledged): never applied late, never
  merged. A dispatch is not here.
Every error is the v1 envelope {"error": {"code", "message"}} (+ "reason" and "current" on a 409 / 412)."""
from __future__ import annotations

import json
import math
import re
from datetime import datetime
from urllib.parse import unquote

from sizeup.cad.activity import client_tag
from sizeup.cad.store import OPEN, TZ, CadError

from .idempotency import BUSY, KEY, MISMATCH, REPLAY, fingerprint
from .rev import rev_of
from . import routes
from .routes import Ctx, Route

FUTURE_S = 300.0            # a client_at further ahead than this is clamped (docs/mobile-api.md section 8)
LATE_S = 60.0               # a queued action this far behind is said so in the chronology
STORED = (200, 201, 409, 412)    # the answers a key keeps: decisions. A 400 / 403 / 404 is corrected and sent again
PLATFORMS = ("ios", "android", "web")

def fail(status: int, code: str, message: str, reason: str = "", current=None, retry_after_s: int | None = None,
         headers: dict | None = None):
    err: dict = {"code": code, "message": message}
    if reason:
        err["reason"] = reason
    if retry_after_s is not None:
        err["retry_after_s"] = retry_after_s
    obj: dict = {"error": err}
    if current is not None:
        obj["current"] = current
    h = dict(headers or {})
    if retry_after_s is not None:
        h["Retry-After"] = str(retry_after_s)
    return (status, obj, h) if h else (status, obj)


# -- the objects a write answers with (and a 409 / 412 shows): small, with the rev of their content --------------------

def unit_obj(u: dict) -> dict:
    o = {"code": u["name"], "status": u["status"], "status_at": u["status_at"], "incident_id": u["incident_id"],
         "incident_number": u["incident_number"], "board": u["board"]}
    return o | {"rev": rev_of(o)}


def incident_obj(i: dict) -> dict:
    o = {"id": i["id"], "number": i["number"], "status": i["status"], "closed_at": i["closed_at"],
         "disposition": i["disposition"]}
    return o | {"rev": rev_of(o)}


def alarm_obj(a: dict) -> dict:
    o = {"id": a["id"], "level": a["level"], "created_at": a["created_at"], "incident_id": a["incident_id"],
         "ack_by": a["ack_by"], "ack_at": a["ack_at"], "clear_at": a.get("clear_at")}
    return o | {"rev": rev_of(o)}


# -- the request -----------------------------------------------------------------------------------------------------

def _via(headers) -> tuple[str, str]:
    """(via, app version) from `X-Client: <app>/<version> (<platform>/<os>; build)`; via "api" when it names none."""
    h = str(headers.get("X-Client") or "")
    ver = re.match(r"[^/\s]{1,40}/([\w.+-]{1,20})", h)
    plat = re.search(r"\(\s*([A-Za-z]+)\s*/", h)
    p = plat.group(1).lower() if plat else ""
    return (p if p in PLATFORMS else "api"), (ver.group(1) if ver else "")


class Req:
    """One v1 write, parsed: who, the audit tag, the body, client_at and the key. `refusal` is the answer when the
    request is not one (no CAD, a viewer, a bad body, no key): checked in that order, so the role comes first."""

    def __init__(self, ctx: Ctx, resolve):
        """resolve(cad) -> (the console's path for this write, the object it is on) or (None, the 404 answer): the
        target is looked up before the role, so a refusal is in the activity under the right route."""
        self.ctx, self.act, self.path, self.target = ctx, None, "", None
        self.refusal = None
        self.now = self.at = 0.0
        self.clamped = False
        self.client_at: float | None = None
        self.body: dict = {}
        self.act = ctx.gateway.acting(ctx.principal) if ctx.gateway is not None else None   # a person, or nothing
        if self.act is None:
            self.refusal = fail(503, "unavailable", "the CAD is not available here")
            return
        p, self.cad = ctx.principal, self.act.cad
        self.via, self.app = _via(ctx.headers)
        self.now = self.cad.store.clock()
        self.client = (ctx.peer, client_tag(self.via, p.device, self.app))
        self.path, self.target = resolve(self.cad)
        if self.path is None:
            self.refusal = self.target
            return
        # the console's role check, first: a viewer is refused whatever else is wrong (and it is in their activity)
        no = self.act.permit(self.path, self.client)
        if no is not None:
            self.refusal = fail(403, "forbidden", _message(json.loads(no[1])))
            return
        try:
            body = json.loads(ctx.body or b"{}")
        except ValueError:
            body = None
        if not isinstance(body, dict):
            self.refusal = fail(400, "invalid", "the body must be a JSON object")
            return
        self.body = body
        key = ctx.headers.get("Idempotency-Key")
        if not key:
            self.refusal = fail(400, "idempotency_key_required", "send an Idempotency-Key header (a UUID)")
            return
        if not KEY.fullmatch(key):
            self.refusal = fail(400, "invalid_idempotency_key", "Idempotency-Key: 8 to 128 letters, digits or - _ . :")
            return
        self.key = key
        raw = body.get("client_at")
        if raw is not None:
            if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw) or raw < 0:
                self.refusal = fail(400, "invalid", "client_at: unix seconds")
                return
            self.client_at = float(raw)
            self.at = self.client_at
            if self.at > self.now + FUTURE_S:          # a clock running ahead: never in the future of the record
                self.at, self.clamped = self.now, True
            self.client = (ctx.peer, client_tag(self.via, p.device, self.app, self.client_at, self.clamped))

    @property
    def late(self) -> bool:
        return self.client_at is not None and self.now - self.at > LATE_S

    def extra(self) -> dict:
        """What the answer says about client_at (only when the app sent one)."""
        if self.client_at is None:
            return {}
        return {"client_at": self.client_at, "client_at_clamped": self.clamped, "received_at": self.now}

    def post(self, console_path: str, body: dict):
        """The console's write. (status, object): the console's 400 {"error": text} is a v1 400 `invalid`."""
        code, raw, _ = self.act.post(console_path, body, self.client)
        try:
            out = json.loads(raw)
        except ValueError:
            out = {}
        if code == 200:
            return 200, out
        st = code if code in (403, 404) else 400
        return fail(st, {403: "forbidden", 404: "not_found"}.get(st, "invalid"), _message(out))

    def say_late(self, iid, what: str) -> None:
        """A queued action applied late: one chronology line saying when the app made it (nothing else changes)."""
        if self.late and iid:
            try:
                self.cad.store.note(iid, f"{what} (queued on the phone at {_hms(self.at)}, applied {_hms(self.now)})",
                        self.body_operator())
            except Exception as e:  # noqa: BLE001 - the write is done; a line that is missing never undoes it
                self.cad.log(f"api v1: late line: {type(e).__name__}")

    def body_operator(self) -> str:
        return self.cad.settings.standing(self.act.who.email)["operator"]


def _hms(t: float) -> str:
    return datetime.fromtimestamp(t, TZ).strftime("%H:%M:%S")


def _message(obj) -> str:
    e = obj.get("error") if isinstance(obj, dict) else None
    return str(e)[:300] if isinstance(e, str) else "refused"


def _once(req: Req, work):
    """work() under the idempotency key: its answer is kept (decisions only), a retry gets it back, the same key
    with another request is a 422, one still running is a 409 `in_progress`."""
    idem = req.ctx.gateway.idempotency()
    principal = req.ctx.principal.device
    fp = fingerprint("POST", req.path, req.body)
    state, kept = idem.claim(principal, req.key, fp)
    if state == MISMATCH:
        return fail(422, "idempotency_key_reused", "this Idempotency-Key was used for another request: send a new key")
    if state == BUSY:
        return fail(409, "in_progress", "the first request with this key is still running: retry shortly",
                    reason="in_progress", retry_after_s=1)
    if state == REPLAY:
        return kept[0], json.loads(kept[1]), {"Idempotent-Replayed": "true"}
    try:
        out = work()
    except BaseException:                 # FencedWrite (a 503), a crash: the key is free for the retry
        idem.release(principal, req.key)
        raise
    status, obj = out[0], out[1]
    if status in STORED:
        idem.finish(principal, req.key, status, json.dumps(obj, separators=(",", ":")))
    else:
        idem.release(principal, req.key)
    return out


def _int(s: str) -> int | None:
    return int(s) if s.isdigit() and len(s) <= 9 else None


# -- POST /api/v1/units/{code}/status --------------------------------------------------------------------------------

def unit_status(ctx: Ctx):
    code = unquote(ctx.params["code"])

    def resolve(cad):
        u = next((u for u in cad.store.units() if u["name"].upper() == code.upper()), None)
        return (f"/api/cad/unit/{u['id']}/status", u) if u else (None, fail(404, "no_such_unit", "no such unit"))
    req = Req(ctx, resolve)
    if req.refusal:
        return req.refusal
    store, unit, path = req.cad.store, req.target, req.path

    def work():
        u = next(x for x in store.units() if x["id"] == unit["id"])
        now = unit_obj(u)
        status = req.body.get("status")
        if not isinstance(status, str) or not status.strip():
            return fail(400, "invalid", "status: required")
        iid = req.body.get("incident_id")
        if iid is not None and (isinstance(iid, bool) or not isinstance(iid, int) or iid < 0):
            return fail(400, "invalid", "incident_id: a number (0 = no call)")
        want = (ctx.headers.get("If-Match") or "").strip()
        want = re.sub(r'^(?:W/)?"?|"$', "", want)
        if want and want != "*" and want != now["rev"]:
            if req.client_at is not None:           # queued: the unit moved on while the app was away
                return fail(409, "conflict", "not applied: this unit changed after you queued this",
                            reason="unit_changed", current=now)
            return fail(412, "precondition_failed", "this unit changed: refresh and try again", current=now)
        body = {"status": status.strip()}
        if isinstance(req.body.get("note"), str):
            body["note"] = req.body["note"]
        if iid is not None:
            if iid:
                inc = store.one("SELECT * FROM incidents WHERE id = ?", (iid,))
                if inc is None:
                    return fail(404, "no_such_incident", "no such incident")
                if inc["status"] not in OPEN:
                    return fail(409, "conflict", f"not applied: the incident is {inc['status']}",
                                reason="incident_closed", current=now)
            body["incident_id"] = iid
        was = u["incident_id"]
        code_, out = req.post(path, body)
        if code_ != 200:
            return code_, out
        after = next(x for x in store.units() if x["id"] == unit["id"])
        req.say_late(after["incident_id"] or was, f"{u['name']} set to {status.strip()}")
        return 200, {"unit": unit_obj(after)} | req.extra()
    return _once(req, work)


# -- POST /api/v1/alarms/{id}/ack ------------------------------------------------------------------------------------

def alarm_ack(ctx: Ctx):
    aid = _int(ctx.params["id"])

    def get(cad):
        try:
            return cad.alarms.get(aid) if aid else None
        except CadError:
            return None
    req = Req(ctx, lambda cad: (f"/api/cad/alarm/{aid}/ack", aid) if get(cad) else
              (None, fail(404, "no_such_alarm", "no such alarm")))
    if req.refusal:
        return req.refusal

    def work():
        a = get(req.cad)
        if a["ack_at"] is not None:
            return fail(409, "conflict", f"not applied: already acknowledged by {a['ack_by']}",
                        reason="already_acknowledged", current=alarm_obj(a))
        code_, out = req.post(req.path, {})
        if code_ != 200:
            if get(req.cad)["ack_at"] is not None:      # another dispatcher got there between the read and the write
                return fail(409, "conflict", "not applied: already acknowledged", reason="already_acknowledged",
                            current=alarm_obj(get(req.cad)))
            return code_, out
        a = get(req.cad)
        req.say_late(a["incident_id"], f"Alarm {aid} acknowledged")
        return 200, {"alarm": alarm_obj(a)} | req.extra()
    return _once(req, work)


# -- POST /api/v1/incidents/{id}/notes -------------------------------------------------------------------------------

def incident_note(ctx: Ctx):
    iid = _int(ctx.params["id"])
    req = Req(ctx, lambda cad: (f"/api/cad/incident/{iid}/note", iid) if iid and cad.store.one(
        "SELECT 1 AS x FROM incidents WHERE id = ?", (iid,)) else
        (None, fail(404, "no_such_incident", "no such incident")))
    if req.refusal:
        return req.refusal
    store = req.cad.store

    def work():
        inc = store.incident(iid)
        if inc["status"] not in OPEN:                    # a note on a call that closed while the app was away
            return fail(409, "conflict", f"not applied: the incident is {inc['status']}", reason="incident_closed",
                        current=incident_obj(inc))
        text = req.body.get("text")
        if not isinstance(text, str):
            return fail(400, "invalid", "text: required")
        body = {"text": text.strip() + (f" [written {_hms(req.at)}]" if req.late else "")}
        for k in ("units", "places"):
            if k in req.body:
                body[k] = req.body[k]
        code_, out = req.post(req.path, body)
        if code_ != 200:
            return code_, out
        return 201, {"incident_id": iid, "units": out.get("units", []), "places": out.get("places", [])} | req.extra()
    return _once(req, work)


ROUTES: list[Route] = [
    Route("POST", "/api/v1/units/{code}/status", unit_status, "device", "writes", "dispatch"),
    Route("POST", "/api/v1/alarms/{id}/ack", alarm_ack, "device", "writes", "dispatch"),
    Route("POST", "/api/v1/incidents/{id}/notes", incident_note, "device", "writes", "dispatch"),
]

routes.ROUTES.extend(ROUTES)          # whichever of routes.py and this module loads first (routes.py imports this)
