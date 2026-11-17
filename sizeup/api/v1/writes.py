"""The writes of /api/v1 (#562, docs/mobile-api.md sections 3.2, 8 and 12): a unit's status, an alarm acknowledged and a
note on an incident.

Every one goes through the console's own code, so every rule, validation and audit line is the console's:

- who: the device's Principal (principal.py) becomes a person Identity (its email) inside gateway.Acting; a handler
  never reaches CadApi, the Cad service or SQL, and never without a person (the `permit` trap, section 1.4). The role
  is the console's (CadApi.permit, `needs`: unit status, alarm ack and notes are "dispatch"; a viewer is a 403), read
  per request, so a role changed in Settings acts at once.
- what: CadApi.post on the console's own path (so Cad.unit_status / Alarms.acknowledge / Cad.note, the operator's name
  from Settings and the activity row (CadApi.did) are the console's). Nothing of the console's logic is repeated here.
- audit: that activity row's "browser" (ua) column is `via=ios app=1.2.0 device=<id> client_at=<unix>`
  (activity.client_tag). The late chronology line of a queued action is a note through the same path, so it has a
  row of its own.
- Idempotency-Key (idempotency.py): required; a retry replays the first answer, the same key with another body is a 422.
- If-Match <rev> (rev.py) on a unit's status: a stale rev is a 412 with the current object; on a queued action (one with
  `client_at`) it is a 409 `unit_changed` instead: "not applied", and the If-Match is REQUIRED there (428), so an old
  queued status can never override a newer one. Notes are append-only: no If-Match. The check and the write are two
  steps (the console's methods do the write and take no expected rev): a change between them is not caught.
- Offline: `client_at` is the app's own time of the action. The server orders by receive time, writes `client_at` in the
  audit (and a chronology line when it is more than a minute behind), and clamps one more than 5 minutes ahead of the
  server's clock (flagged `client_at_clamped`) and refuses one older than 7 days. An action that no longer makes
  sense is a 409 with a machine-readable
  `reason` and the current object (incident_closed, unit_changed, already_acknowledged): never applied late, never
  merged. A dispatch is not here.
Every error is the v1 envelope {"error": {"code", "message"}} (+ "reason" and "current" on a 409 / 412)."""
from __future__ import annotations

import json
import math
import re
from urllib.parse import unquote

from . import routes
from .errors import envelope
from .idempotency import BUSY, KEY, MISMATCH, REPLAY, fingerprint
from .rev import rev_of
from .routes import Ctx, Route

FUTURE_S = 300.0            # a client_at further ahead than this is clamped (docs/mobile-api.md section 8)
PAST_S = 7 * 86400.0        # ... and one older than this is refused (a 0 would write a 1970 chronology line)
LATE_S = 60.0               # a queued action this far behind is said so in the chronology
STORED = (200, 201, 409, 412)    # the answers a key keeps: decisions. A 400 / 403 / 404 is corrected and sent again
PLATFORMS = ("ios", "android", "web")     # X-Client's platforms, as `via`; anything else (curl, no header) is "api"
EMAIL = re.compile(r"[^\s<>()]+@[^\s<>()]+")


def fail(status: int, code: str, message: str, reason: str = "", current=None, retry_after_s: int | None = None,
         headers: dict | None = None) -> tuple[int, dict, dict]:
    """A refusal, always (status, object, headers): the v1 envelope (errors.envelope) and, with retry_after_s, the
    Retry-After header too."""
    h = dict(headers or {})
    if retry_after_s is not None:
        h["Retry-After"] = str(retry_after_s)
    return status, envelope(code, message, retry_after_s, reason, current), h


# -- the objects a write answers with (and a 409 / 412 shows): small, with the rev of their content --------------------

def unit_obj(u: dict) -> dict:
    o = {"code": u["name"], "status": u["status"], "status_at": u["status_at"], "incident_id": u["incident_id"],
         "incident_number": u["incident_number"], "board": u["board"]}
    return o | {"rev": rev_of(o)}


def incident_obj(i: dict) -> dict:
    o = {"id": i["id"], "number": i["number"], "status": i["status"], "closed_at": i["closed_at"],
         "disposition": i["disposition"]}
    return o | {"rev": rev_of(o)}


def alarm_obj(a: dict, ack_by: str) -> dict:
    """ack_by: the name only (Acting.ack_name): the console's operator string carries the e-mail address."""
    o = {"id": a["id"], "level": a["level"], "created_at": a["created_at"], "incident_id": a["incident_id"],
         "ack_by": ack_by, "ack_at": a["ack_at"], "clear_at": a.get("clear_at")}
    return o | {"rev": rev_of(o)}


# -- the request -----------------------------------------------------------------------------------------------------

def _via(headers) -> tuple[str, str]:
    """(via, app version) from `X-Client: <app>/<version> (<platform>/<os>; build)`; via "api" when it names none."""
    h = str(headers.get("X-Client") or "")
    ver = re.match(r"[^/\s]{1,40}/([\w.+-]{1,20})", h)
    plat = re.search(r"\(\s*([A-Za-z]+)\s*/", h)
    p = plat.group(1).lower() if plat else ""
    return (p if p in PLATFORMS else "api"), (ver.group(1) if ver else "")


def _message(obj) -> str:
    """The console's error text for the app: never an e-mail address (a deactivated account's "ask <admins>"), at most
    300 characters (the console's texts are short; this bounds one that is not)."""
    e = obj.get("error") if isinstance(obj, dict) else None
    return EMAIL.sub("an admin", e)[:300] if isinstance(e, str) else "refused"


class Req:
    """One v1 write, parsed. `refusal` is the answer when the request is not one: the checks run in this order, so the
    role comes before anything about the body (a viewer is a 403 whatever else is wrong)."""

    def __init__(self, ctx: Ctx, resolve):
        """resolve(act) -> (the console's path for this write, the object it is on) or (None, the 404 answer): the
        target is looked up before the role, so a refusal is in the activity under the right route."""
        self.ctx, self.resolve = ctx, resolve
        self.act = None                     # the gateway's Acting: the signed-in person
        self.path, self.target = "", None   # the console's path for this write, and what it is on
        self.body: dict = {}
        self.key = ""
        self.via = self.app = ""
        self.client: tuple[str, str] = ("", "")      # (address, the audit tag) for the activity row
        self.now = self.at = 0.0            # receive time; the action's time (client_at, clamped)
        self.client_at: float | None = None
        self.clamped = False
        self.refusal = self._check()

    def _check(self):
        for step in (self._person, self._target, self._role, self._json, self._key, self._client_at):
            refusal = step()
            if refusal is not None:
                return refusal
        return None

    def _person(self):
        self.act = self.ctx.gateway.acting(self.ctx.principal) if self.ctx.gateway is not None else None
        if self.act is None:                # never the CAD without a person
            return fail(503, "unavailable", "the CAD is not available here")
        self.via, self.app = _via(self.ctx.headers)
        self.now = self.act.now()
        self.client = (self.ctx.peer, self.act.client_tag(self.via, self.app))
        return None

    def _target(self):
        self.path, self.target = self.resolve(self.act)
        return self.target if self.path is None else None

    def _role(self):
        refused = self.act.permit(self.path, self.client)
        return None if refused is None else fail(403, "forbidden", _message(json.loads(refused[1])))

    def _json(self):
        try:
            body = json.loads(self.ctx.body or b"{}")
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return fail(400, "invalid", "the body must be a JSON object")
        self.body = body
        return None

    def _key(self):
        key = self.ctx.headers.get("Idempotency-Key")
        if not key:
            return fail(400, "idempotency_key_required", "send an Idempotency-Key header (a UUID)")
        if not KEY.fullmatch(key):
            return fail(400, "invalid_idempotency_key", "Idempotency-Key: 8 to 128 letters, digits or - _ . :")
        self.key = key
        return None

    def _client_at(self):
        raw = self.body.get("client_at")
        if raw is None:
            return None
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw) or raw < 0:
            return fail(400, "invalid", "client_at: unix seconds")
        if raw < self.now - PAST_S:
            return fail(400, "invalid", "client_at: older than 7 days")
        self.client_at = self.at = float(raw)
        if self.at > self.now + FUTURE_S:   # a clock running ahead: never in the future of the record
            self.at, self.clamped = self.now, True
        self.client = (self.ctx.peer, self.act.client_tag(self.via, self.app, self.client_at, self.clamped))
        return None

    @property
    def late(self) -> bool:
        return self.client_at is not None and self.now - self.at > LATE_S

    def extra(self) -> dict:
        """What the answer says about client_at (only when the app sent one)."""
        if self.client_at is None:
            return {}
        return {"client_at": self.client_at, "client_at_clamped": self.clamped, "received_at": self.now}

    def post(self, body: dict) -> tuple[dict | None, tuple | None]:
        """The console's write: (its answer, None), or (None, the refusal): the console's 400 {"error": text} is a v1
        400 `invalid`."""
        status, out = self.act.post(self.path, body, self.client)
        if status == 200:
            return out, None
        st = status if status in (403, 404) else 400
        return None, fail(st, {403: "forbidden", 404: "not_found"}.get(st, "invalid"), _message(out))

    def say_late(self, iid, what: str) -> None:
        """A queued action applied late: one chronology line saying when the app made it (nothing else changes)."""
        if self.late and iid:
            said = f"{what} (queued on the phone at {self.act.hms(self.at)}, applied {self.act.hms(self.now)})"
            self.act.note_late(iid, said, self.client)


def _once(req: Req, work):
    """work() under the idempotency key: its answer is kept (decisions only), a retry gets it back, the same key
    with another request is a 422, one still running is a 409 `in_progress`."""
    idem = req.ctx.gateway.idempotency()
    principal = req.ctx.principal.device
    state, got = idem.claim(principal, req.key, fingerprint("POST", req.path, req.body))
    if state == MISMATCH:
        return fail(422, "idempotency_key_reused", "this Idempotency-Key was used for another request: send a new key")
    if state == BUSY:
        return fail(409, "in_progress", "the first request with this key is still running: retry shortly",
                    reason="in_progress", retry_after_s=1)
    if state == REPLAY:
        return got[0], json.loads(got[1]), {"Idempotent-Replayed": "true"}
    try:
        out = work()
    except BaseException:                 # FencedWrite (a 503), a crash: the key is free for the retry
        idem.release(principal, req.key, got)
        raise
    if out[0] in STORED:
        idem.finish(principal, req.key, got, out[0], json.dumps(out[1], separators=(",", ":")))
    else:
        idem.release(principal, req.key, got)
    return out


def _int(s: str) -> int | None:
    return int(s) if s.isdigit() and len(s) <= 9 else None


# -- POST /api/v1/units/{code}/status --------------------------------------------------------------------------------

def _if_match_conflict(req: Req, ctx: Ctx, current: dict):
    """The refusal for a stale If-Match (412; a queued action's is a 409 `unit_changed`, "not applied"), a queued action
    with none (428), else None."""
    # `If-Match: "abc"` and `W/"abc"` (a proxy's weak ETag) both mean the rev abc: drop the prefix and the quotes
    want = re.sub(r'^(?:W/)?"?|"$', "", (ctx.headers.get("If-Match") or "").strip())
    if not want and req.client_at is not None:
        return fail(428, "precondition_required", "a queued action needs If-Match: the unit's rev when it was queued")
    if want and want != "*" and want != current["rev"]:
        if req.client_at is not None:
            return fail(409, "conflict", "not applied: this unit changed after you queued this",
                        reason="unit_changed", current=current)
        return fail(412, "precondition_failed", "this unit changed: refresh and try again", current=current)
    return None


def _check_incident(req: Req, iid, current: dict):
    """The refusal for a status "on a call" that does not exist (404) or is closed (409), else None."""
    if not iid:
        return None
    inc = req.act.incident(iid)
    if inc is None:
        return fail(404, "no_such_incident", "no such incident")
    if not req.act.is_open(inc):
        return fail(409, "conflict", f"not applied: the incident is {inc['status']}", reason="incident_closed",
                    current=current)
    return None


def unit_status(ctx: Ctx):
    code = unquote(ctx.params["code"])

    def resolve(act):
        u = act.unit(code)
        return (f"/api/cad/unit/{u['id']}/status", u) if u else (None, fail(404, "no_such_unit", "no such unit"))
    req = Req(ctx, resolve)
    if req.refusal:
        return req.refusal

    def work():
        u = req.act.unit_by_id(req.target["id"])
        current = unit_obj(u)
        status, iid = req.body.get("status"), req.body.get("incident_id")
        if not isinstance(status, str) or not status.strip():
            return fail(400, "invalid", "status: required")
        if iid is not None and (isinstance(iid, bool) or not isinstance(iid, int) or iid < 0):
            return fail(400, "invalid", "incident_id: a number (0 = no call)")
        refusal = _if_match_conflict(req, ctx, current) or _check_incident(req, iid, current)
        if refusal:
            return refusal
        body = {"status": status.strip()}
        if isinstance(req.body.get("note"), str):
            body["note"] = req.body["note"]
        if iid is not None:
            body["incident_id"] = iid
        out, refusal = req.post(body)
        if refusal:
            return refusal
        after = req.act.unit_by_id(u["id"])
        req.say_late(after["incident_id"] or u["incident_id"], f"{u['name']} set to {status.strip()}")
        return 200, {"unit": unit_obj(after)} | req.extra(), {}
    return _once(req, work)


# -- POST /api/v1/alarms/{id}/ack ------------------------------------------------------------------------------------

def alarm_ack(ctx: Ctx):
    aid = _int(ctx.params["id"])

    def resolve(act):
        return (f"/api/cad/alarm/{aid}/ack", act.alarm(aid)) if act.alarm(aid) else (
            None, fail(404, "no_such_alarm", "no such alarm"))
    req = Req(ctx, resolve)
    if req.refusal:
        return req.refusal

    def already(a: dict):
        mine = alarm_obj(a, req.act.ack_name(a))
        return fail(409, "conflict", f"not applied: already acknowledged by {mine['ack_by'] or 'someone'}",
                    reason="already_acknowledged", current=mine)

    def work():
        a = req.act.alarm(aid)
        if a["ack_at"] is not None:
            return already(a)
        out, refusal = req.post({})
        if refusal:
            a = req.act.alarm(aid)
            return already(a) if a["ack_at"] is not None else refusal     # another dispatcher got there first
        a = req.act.alarm(aid)
        req.say_late(a["incident_id"], f"Alarm {aid} acknowledged")
        return 200, {"alarm": alarm_obj(a, req.act.ack_name(a))} | req.extra(), {}
    return _once(req, work)


# -- POST /api/v1/incidents/{id}/notes -------------------------------------------------------------------------------

def incident_note(ctx: Ctx):
    iid = _int(ctx.params["id"])

    def resolve(act):
        return (f"/api/cad/incident/{iid}/note", act.incident(iid)) if act.incident(iid) else (
            None, fail(404, "no_such_incident", "no such incident"))
    req = Req(ctx, resolve)
    if req.refusal:
        return req.refusal

    def work():
        inc = req.act.incident(iid)
        if not req.act.is_open(inc):                    # a note on a call that closed while the app was away
            return fail(409, "conflict", f"not applied: the incident is {inc['status']}", reason="incident_closed",
                        current=incident_obj(inc))
        text = req.body.get("text")
        if not isinstance(text, str):
            return fail(400, "invalid", "text: required")
        body = {"text": text.strip() + (f" [written {req.act.hms(req.at)}]" if req.late else "")}
        for k in ("units", "places"):
            if k in req.body:
                body[k] = req.body[k]
        out, refusal = req.post(body)
        if refusal:
            return refusal
        # 201 (a created entry), where the console answers 200
        return 201, {"incident_id": iid, "units": out.get("units", []), "places": out.get("places", [])} \
            | req.extra(), {}
    return _once(req, work)


ROUTES: list[Route] = [
    Route("POST", "/api/v1/units/{code}/status", unit_status, "device", "writes", "dispatch"),
    Route("POST", "/api/v1/alarms/{id}/ack", alarm_ack, "device", "writes", "dispatch"),
    Route("POST", "/api/v1/incidents/{id}/notes", incident_note, "device", "writes", "dispatch"),
]

# whichever of routes.py and this module loads first (routes.py imports this one at its end: a circular import that is
# safe because neither reads the other's names until both are loaded)
routes.ROUTES.extend(ROUTES)
