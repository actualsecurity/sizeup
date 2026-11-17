"""The read resources of /api/v1 (#560, docs/mobile-api.md sections 1.2, 3.2, 8): the snapshot (the screens' first
load), incidents (active and recent, cursor pages), one incident, units and alarms. Read only: nothing here calls a
CAD method that writes, and the API layer never opens a database (everything goes through the Cad's own methods).

Same rules as the console, for the person the device token names (principal.py):
  - a person's role is Settings.standing(email)["may"] (viewer < dispatcher < admin), as the console's `_may`; every
    read here is the least role, "read", so a viewer gets all of it, and what a role may not see is left out where
    the console leaves it out: premise notes and pre-plan points by category/kind (`for_incident(inc, may)`), another
    P25 system's radio rows unless sourcekeys.may_see_context (the thread's request context is the person's);
  - site documents (admin only in the console) and the radio log itself are not served here at all, nor is anything
    the console's ADMIN_READS keeps to admins; nothing a viewer could not see is built into the shared caches: the
    cached objects are the role-independent ones (summaries, units, alarms), the role-dependent parts (premise,
    pre-plan, radio) are built per request, for one incident only.

Cost (#446, the hub is CPU-constrained): the lists come from caches keyed on the CAD's own change counter
(store.rev, bumped on every write) with a maximum age, rebuilt by one thread at a time, so a burst of devices costs
one rebuild, not one per request; every list is bounded (the CAD's own windows: open calls, the last day's closed
ones, the units, 40 alarms) and a page is a slice of the cache. The console's cached state (Cad.state) is not used: it
is built for one shared stream and for the Broadcaster's context, and calling it from a request thread could put that
person's request context into the cache every console client shares."""
from __future__ import annotations

import re
import threading

from sizeup import sourcekeys

from . import paging
from .paging import BadRequest
from .rev import rev_of
from .serialize import RADIO_MAX, alarm, incident_detail, incident_summary, radio_rows, unit, with_rev

OPEN = ("queued", "pending", "active")
SNAPSHOT_MAX = 200                    # open incidents in the snapshot (open calls are few; this is a guard)
UNITS_DEFAULT, UNITS_MAX = 100, 500
INCIDENT_AGE_S = 30.0                 # the cache's maximum age: the "closed in the last day" window moves with time
ALARM_AGE_S = 1.0                     # alarms age out by the clock (notices, the just acknowledged), like the console's 1 s


class Unavailable(Exception):
    """The CAD could not answer (a fault the CAD itself swallowed): 503, never an empty list the app would trust."""


class _Cached:
    """One list, rebuilt when the CAD's change counter moves or it is older than max_age; one rebuild at a time."""

    def __init__(self, build, max_age: float, clock):
        self.build, self.max_age, self.clock = build, max_age, clock
        self._lock = threading.Lock()
        self._at, self._rev, self._value = 0.0, None, None

    def get(self, rev):
        with self._lock:
            now = self.clock()
            if self._value is None or rev != self._rev or now - self._at >= self.max_age:
                self._value = self.build()
                self._rev, self._at = rev, now
            return self._value


class Reads:
    """One per Cad: the caches and the reads."""

    def __init__(self, cad, clock=None):
        # the CAD's own clock: the console's `time`, and what its tests move
        self._cad, self.clock = cad, clock or cad.clock
        self._incidents = _Cached(self._build_incidents, INCIDENT_AGE_S, self.clock)
        self._units = _Cached(self._build_units, INCIDENT_AGE_S, self.clock)
        self._alarms = _Cached(self._build_alarms, ALARM_AGE_S, self.clock)

    # -- the cached, role-independent lists ---------------------------------------------------------------------

    def _build_incidents(self) -> list[tuple[tuple, dict]]:
        """[((created_at, id), summary with rev)], newest first: open ones however old, closed ones of the last day."""
        out = [((i["created_at"], i["id"]), with_rev(incident_summary(i))) for i in self._cad.store.incidents()]
        out.sort(key=lambda kv: kv[0], reverse=True)
        return out

    def _build_units(self) -> list[tuple[tuple, dict]]:
        out = [((u.get("sort") or 0, u["id"]), with_rev(unit(u))) for u in self._cad.store.units()]
        out.sort(key=lambda kv: kv[0])
        return out

    def _build_alarms(self) -> list[dict]:
        st = self._cad.alarms_state()
        if st is None:
            raise Unavailable
        out = [alarm(a, name) for key, name in (("open", "open"), ("notices", "notice"), ("recent", "recent"))
               for a in st.get(key) or []]
        out.sort(key=lambda a: (a["at"] or 0, a["id"]), reverse=True)
        return [with_rev(a) for a in out]

    def incidents_all(self):
        return self._incidents.get(self._cad.store.rev)

    def units_all(self):
        return self._units.get(self._cad.store.rev)

    def alarms_all(self) -> list[dict]:
        return self._alarms.get(self._cad.store.rev)

    # -- the person -----------------------------------------------------------------------------------------------

    def may(self, principal) -> str:
        """The person's standing as the console reads it (CadApi._may): viewer | dispatcher | admin."""
        try:
            return self._cad.settings.standing(principal.email)["may"]
        except Exception as e:  # noqa: BLE001 - CadError and the like: the least, as the console does
            self._cad.log(f"api v1: standing: {type(e).__name__}")
            return "viewer"

    # -- the reads ------------------------------------------------------------------------------------------------

    def snapshot(self, headers) -> tuple:
        incs = [s for _, s in self.incidents_all() if s["status"] in OPEN][:SNAPSHOT_MAX]
        units = [u for _, u in self.units_all()][:UNITS_MAX]
        alarms = self.alarms_all()
        rev = rev_of({"incidents": [i["rev"] for i in incs], "units": [u["rev"] for u in units],
                      "alarms": [a["rev"] for a in alarms]})
        body = {"incidents": incs, "units": units, "alarms": alarms, "server_time": self.clock(), "epoch": None,
                "rev": rev}
        return paging.answer(headers, body, rev)

    def incidents(self, query: dict, headers) -> tuple:
        limit = paging.limit_of(query)
        want = paging.one(query, "state")
        if want not in ("", "open", "recent"):
            raise BadRequest("bad_request", "state: open or recent")
        after = paging.one(query, "after")
        key = paging.decode(after, "incidents", (float, int)) if after else None
        rows = [(k, s) for k, s in self.incidents_all()
                if not want or (s["status"] in OPEN) == (want == "open")]
        if key is not None:
            rows = [(k, s) for k, s in rows if k < key]                 # newest first: what is older than the cursor
        page, more = rows[:limit], len(rows) > limit
        nxt = paging.encode("incidents", list(page[-1][0])) if more else None
        items = [s for _, s in page]
        rev = paging.list_rev([s["rev"] for s in items], nxt)
        return paging.answer(headers, {"items": items, "next": nxt, "server_time": self.clock(), "rev": rev}, rev)

    def incident(self, raw_id: str, principal, headers) -> tuple:
        if not re.fullmatch(r"[0-9]{1,12}", raw_id):
            return _err(404, "not_found", "no such incident")
        iid = int(raw_id)
        try:
            full = self._cad.store.full(iid)
        except Exception as e:  # noqa: BLE001
            if type(e).__name__ == "CadError":                          # "no event N"
                return _err(404, "not_found", "no such incident")
            raise
        may = self.may(principal)
        with sourcekeys.request_context(principal):                     # a signed-in person: the console's gate
            sees = sourcekeys.may_see_context()
            rows = self._cad.comms.for_incidents([iid], RADIO_MAX)[iid]
            for r in rows:
                r["has_audio"] = bool(self._cad.has_clip(r["id"]))
        premise = self._safe(lambda: self._cad.premise.for_incident(full, may))
        preplan = self._safe(lambda: self._cad.preplan.for_incident(full, may))
        body = incident_detail(full, premise, preplan, radio_rows(rows, sees))
        body = with_rev(body)
        return paging.answer(headers, {**body, "server_time": self.clock()}, body["rev"])

    def units(self, query: dict, headers) -> tuple:
        limit = paging.limit_of(query, UNITS_DEFAULT, UNITS_MAX)
        after = paging.one(query, "after")
        key = paging.decode(after, "units", (int, int)) if after else None
        rows = [(k, u) for k, u in self.units_all() if key is None or k > key]
        page, more = rows[:limit], len(rows) > limit
        nxt = paging.encode("units", list(page[-1][0])) if more else None
        items = [u for _, u in page]
        rev = paging.list_rev([u["rev"] for u in items], nxt)
        return paging.answer(headers, {"items": items, "next": nxt, "server_time": self.clock(), "rev": rev}, rev)

    def alarms(self, headers) -> tuple:
        items = self.alarms_all()
        rev = paging.list_rev([a["rev"] for a in items], None)
        return paging.answer(headers, {"items": items, "next": None, "server_time": self.clock(), "rev": rev}, rev)

    def _safe(self, fn) -> list:
        """What a building's notes/points add never breaks the call (the console's own rule); a fault is logged by
        class only (a note's text never reaches a log)."""
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            self._cad.log(f"api v1: building notes: {type(e).__name__}")
            return []


# -- the route handlers (routes.py) ---------------------------------------------------------------------------------

def _err(status: int, code: str, message: str, retry_after_s: int | None = None) -> tuple:
    """An error from a handler: the same envelope as errors.error (the handler's tuple form: status, object, headers)."""
    e = {"code": code, "message": message, **({} if retry_after_s is None else {"retry_after_s": retry_after_s})}
    return status, {"error": e}, ({} if retry_after_s is None else {"Retry-After": str(retry_after_s)})


def _serve(ctx, fn):
    """Run one read: no CAD here (a station's page has none): 503; the query wrong: 400; the CAD's own fault: 503."""
    if ctx.reads is None:
        return _err(503, "unavailable", "the CAD is not available here", 5)
    try:
        return fn(ctx.reads)
    except BadRequest as e:
        return _err(400, e.code, e.message)
    except Unavailable:
        return _err(503, "unavailable", "the CAD could not answer: try again", 2)


def snapshot(ctx):
    return _serve(ctx, lambda r: r.snapshot(ctx.headers))


def incidents(ctx):
    return _serve(ctx, lambda r: r.incidents(ctx.query, ctx.headers))


def incident(ctx):
    return _serve(ctx, lambda r: r.incident(ctx.params["id"], ctx.principal, ctx.headers))


def units(ctx):
    return _serve(ctx, lambda r: r.units(ctx.query, ctx.headers))


def alarms(ctx):
    return _serve(ctx, lambda r: r.alarms(ctx.headers))
